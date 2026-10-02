"""Task Context v1 — deterministic UserPromptSubmit primary-target classifier.

Issue #2564 In Scope: "UserPromptSubmit の deterministic primary-target
classifier を配線する。fenced/inline code、blockquote、example/quoted text を
authority text から除外し、EXPLICIT/INFERRED/REFERENCE_ONLY/AMBIGUOUS/NONE を
分類する。LLM をhot pathに入れない。"

This module is pure text processing over the *raw* prompt string -- it never
touches the DB and is never given anything except the prompt text + the
current repo (for resolving bare ``#N`` shorthand). It never calls an LLM or
performs network I/O (Stop Condition: no LLM/network in the hot path).

The caller (``adapter.py``) is responsible for making sure only the
*structured result* of classification (kind + repo/ref_kind/ref_number) ever
reaches ``task-contextctl`` / the DB -- the raw prompt text itself must never
be persisted (AC7's ``events`` metadata allowlist).
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

KIND_SLASH_TASK = "SLASH_TASK"
KIND_EXPLICIT = "EXPLICIT"
KIND_INFERRED = "INFERRED"
KIND_REFERENCE_ONLY = "REFERENCE_ONLY"
KIND_AMBIGUOUS = "AMBIGUOUS"
KIND_NONE = "NONE"

# Deterministic "this is a reference, not a target to switch to" keyword
# list. Intentionally a fixed, documented allowlist (not fuzzy/NLP) so the
# classifier stays deterministic and LLM-free.
_REFERENCE_ONLY_MARKERS = (
    "related to",
    "see also",
    "similar to",
    "as in",
    "as done in",
    "as seen in",
    "cf.",
    "reference:",
    "for context",
    "for reference",
    "compare to",
    "compare with",
)

# Issue #2827: Japanese reference-only markers. A closed set of exactly two
# words (`参照` / `比較` are deliberately NOT markers: they appear in ordinary
# technical nouns such as `参照カウント` / `比較ロジック`). Unlike the English
# markers above (whole-prompt substring match, behavior unchanged), a Japanese
# marker only demotes the references that sit in the marker's own local
# segment: a clause is further split on `、` / ASCII `,`, and only references
# in the same segment as the marker are demoted. A reference in another
# segment/clause stays a primary candidate. The generic ``_clause_spans``
# semantics are unchanged (the extra split is marker-specific).
_JA_REFERENCE_ONLY_MARKERS = ("参考", "関連資料")

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# "example/quoted text" (deterministic, narrow interpretation): text enclosed
# in matched double quotes on a single line.
_DOUBLE_QUOTED_RE = re.compile(r'"[^"\n]*"')

_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/([\w.-]+/[\w.-]+)/(issues|pull)/(\d+)",
    re.IGNORECASE,
)
# Issue #2850: the number boundary is ASCII based. A reference number is the
# run of digits after ``#`` that is NOT directly followed by an ASCII word
# character (``[A-Za-z0-9_]``). Python's ``\b`` / ``\w`` are Unicode aware, so
# the old trailing ``\b`` rejected ``#12を実装して`` (a CJK character is ``\w``)
# while ``#12abc`` must stay rejected. A CJK character, a particle or
# punctuation after the digits therefore keeps the reference recognised, and the
# same boundary is shared by every ``#N`` regex below (and by
# ``needs_current_repo_resolution``). The digit semantics (``\d``, including
# full-width digits) are intentionally unchanged, so the trailing lookahead also
# rejects a following Unicode digit (``[\d...]``): otherwise ``#１２abc`` would
# backtrack to ``#１`` and succeed on a truncated number.
#
# Issue #2864 (W3): the owner/repo token is ASCII only. The character set is
# ``[A-Za-z0-9_.-]`` and the leading boundary is the ASCII word boundary
# ``(?<![A-Za-z0-9_])(?=[A-Za-z0-9_])`` (the Unicode ``\b`` / ``[\w.-]`` used
# before treated ``foo/あ`` / ``src/ファイル`` as an owner/repo form, which then
# overlapped the bare ``#N`` form and produced AMBIGUOUS). ``re.ASCII`` is NOT
# applied to the whole regex: ``\d`` (full-width digits, decided by #2850) and the
# trailing ``(?![\dA-Za-z_])`` stay exactly as they were.
_OWNER_REPO_HASH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?=[A-Za-z0-9_])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)(?![\dA-Za-z_])"
)
_BARE_HASH_RE = re.compile(r"(?<![A-Za-z0-9_/])#(\d+)(?![\dA-Za-z_])")
# Issue #2850 closed-prefix adjacency (OWNER-approved policy): ``Issue#12`` /
# ``PR#34`` (no space between the prefix word and ``#``) is accepted as a
# reference ONLY when the prefix word itself is one of the closed English
# prefixes (issue / pr / pull request) and is not glued to a preceding ASCII
# word character or ``/`` (``xIssue#12`` and any ``abc#12`` are NOT references).
# The Japanese counterparts (``イシュー#12`` / ``プルリク#34``) already pass the
# ASCII ``_BARE_HASH_RE`` lookbehind and are classified by the prefix regexes
# below.
_ADJACENT_PREFIX_HASH_RE = re.compile(
    r"(?<![A-Za-z0-9_/])((?ai:issue|pr|pull request))#(\d+)(?![\dA-Za-z_])"
)
# Issue #2827: the closed set of reference "prefix" words that may precede a
# bare ``#N`` (English is case-insensitive): Issue / PR / pull request /
# イシュー / プルリク / プルリクエスト. Longer Japanese forms are listed first.
_PR_PREFIX_RE = re.compile(r"(?:\b(?:pr|pull request)|プルリクエスト|プルリク)\s*$", re.IGNORECASE)
_ISSUE_PREFIX_RE = re.compile(r"(?:\bissue|イシュー)\s*$", re.IGNORECASE)

_SLASH_TASK_RE = re.compile(r"^\s*/task(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Target:
    repo: str | None
    ref_kind: str
    ref_number: int
    # True when the repo was explicitly named in the prompt text (full URL
    # or "owner/repo#N"); False when it was a bare "#N" resolved against
    # ``current_repo`` -- this distinguishes EXPLICIT from INFERRED even
    # after the bare-hash target's ``repo`` field has been filled in.
    explicit_repo: bool = False


@dataclass(frozen=True)
class Classification:
    kind: str
    target: Target | None = None
    targets: tuple[Target, ...] = field(default_factory=tuple)
    slash_task_raw_target: str | None = None


def _strip_authority_exclusions(text: str) -> str:
    """Remove fenced code blocks, inline code spans, blockquote lines, and
    double-quoted "example" text from ``text`` before scanning for GitHub
    reference targets (Issue #2564 In Scope)."""
    text = _FENCED_CODE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _DOUBLE_QUOTED_RE.sub(" ", text)
    lines = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    return "\n".join(lines)


REF_FORM_EXPLICIT = "explicit"
REF_FORM_PREFIXED = "prefixed"
REF_FORM_BARE = "bare"


@dataclass(frozen=True)
class _Occurrence:
    """One syntactic occurrence of a GitHub reference inside the authority
    text (Issue #2827). ``form`` is decided purely from syntax: ``explicit``
    (full GitHub URL / ``owner/repo#N``), ``prefixed`` (a prefix word from the
    closed set directly before ``#N``) or ``bare`` (a ``#N`` with no prefix)."""

    target: Target
    start: int
    end: int
    form: str


def _target_key(target: Target) -> tuple[str | None, str, int]:
    return (target.repo, target.ref_kind, target.ref_number)


def _inside_any_span(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


# Issue #2864 (W3): a ``#N`` that sits inside a path / URL-like token is not a
# GitHub Issue reference (a file path or an external URL fragment is not GitHub
# reference syntax), so it must never become a bare / adjacent-prefix reference
# of the CURRENT repo. A "token" is a whitespace-free run that is also not cut by
# a Japanese / ASCII sentence punctuation mark or a bracket; a ``#N`` is
# path-embedded when its token contains a ``/`` before the ``#``. A valid ASCII
# ``owner/repo#N`` and a GitHub URL are recognised by their own regexes (their
# ``#N`` tail is never a separate bare reference), so only the leftover
# non-ASCII / non-GitHub path forms (``src/ファイル#12``, ``https://x.com/あ#12``)
# are affected. Known limitation (accepted by the Issue): a Japanese prompt that
# glues a ``/`` word to a ``#N`` without whitespace or punctuation
# (``A/Bテストの#12を実装して``) is treated as path-embedded too.
# Complexity: one linear regex pass + ``str.find`` per token; the result is a
# sorted list of disjoint spans queried with ``bisect`` (O(log n) per lookup).
#
# Issue #2864 (W3 fix_delta 2): the token range has two lexical forms.
#   1. an external URL token (``http(s)://...``) in which ASCII ``?`` / ``!`` are
#      allowed, so a query string / fragment such as ``https://x.com/あ?あ#12``
#      stays inside ONE URL range (the ``#12`` is a URL fragment, not a reference);
#   2. an ordinary prose token in which ASCII ``!`` ``?`` ``[`` ``]`` are token
#      boundaries just like the Japanese punctuation (``src/ファイルの不具合です!#13``
#      and ``[src/foo.py]の修正は#13`` end the path context at ``!`` / ``]``). It
#      also stops before a URL start so an ``http(s)://`` glued after prose still
#      opens its own URL token.
# Known limitation (accepted): a scheme-less ``x.com/a?b#12`` is ordinary prose,
# not a URL, so ``?`` ends its path context and the ``#12`` stays a bare reference.
_PATH_TOKEN_RE = re.compile(
    r"https?://[^\s、。，．！？,;；「」『』（）()\[\]]+"
    r"|(?:(?!https?://)[^\s、。，．！？!?,;；「」『』（）()\[\]])+",
    re.IGNORECASE,
)


def _path_token_spans(text: str) -> list[tuple[int, int]]:
    """Sorted disjoint ``(slash_index, segment_end)`` spans: a ``#N`` whose start
    is inside such a span has a path ``/`` earlier in the same token.

    A ``/`` that belongs to an already recognised ASCII ``owner/repo#N`` or
    GitHub URL does not open a path context, and such a recognised span ends the
    path context (a ``#N`` glued after it by Japanese text, e.g.
    ``owner/repo#12と#13``, keeps the classification it had before W3). Each
    token is therefore cut into segments at the recognised spans and only a
    ``/`` in the segment that precedes the ``#N`` counts."""
    spans: list[tuple[int, int]] = []
    if "#" not in text or "/" not in text:
        return spans
    recognised = sorted(
        [m.span() for m in _GITHUB_URL_RE.finditer(text)] + [m.span() for m in _OWNER_REPO_HASH_RE.finditer(text)]
    )
    cursor = 0
    for token in _PATH_TOKEN_RE.finditer(text):
        while cursor < len(recognised) and recognised[cursor][1] <= token.start():
            cursor += 1
        seg_start = token.start()
        index = cursor
        while True:
            # Next recognised span that starts inside this token (if any).
            while index < len(recognised) and recognised[index][0] < seg_start:
                index += 1
            boundary = recognised[index] if index < len(recognised) and recognised[index][0] < token.end() else None
            seg_end = boundary[0] if boundary else token.end()
            slash = text.find("/", seg_start, seg_end)
            if slash != -1:
                spans.append((slash, seg_end))
            if boundary is None:
                break
            seg_start = boundary[1]
            index += 1
            if seg_start >= token.end():
                break
    return spans


def _inside_path_token(position: int, spans: list[tuple[int, int]]) -> bool:
    index = bisect_right(spans, (position, float("inf"))) - 1
    return index >= 0 and spans[index][0] < position < spans[index][1]


def _find_occurrences(authority_text: str, current_repo: str | None) -> list[_Occurrence]:
    occurrences: list[_Occurrence] = []

    for match in _GITHUB_URL_RE.finditer(authority_text):
        repo, kind_word, number = match.group(1), match.group(2), int(match.group(3))
        ref_kind = "pr" if kind_word.lower() == "pull" else "issue"
        target = Target(repo=repo, ref_kind=ref_kind, ref_number=number, explicit_repo=True)
        occurrences.append(_Occurrence(target, match.start(), match.end(), REF_FORM_EXPLICIT))

    for match in _OWNER_REPO_HASH_RE.finditer(authority_text):
        repo, number = match.group(1), int(match.group(2))
        prefix = authority_text[: match.start()]
        ref_kind = "pr" if _PR_PREFIX_RE.search(prefix.split("/")[0][-20:] or "") else "issue"
        target = Target(repo=repo, ref_kind=ref_kind, ref_number=number, explicit_repo=True)
        occurrences.append(_Occurrence(target, match.start(), match.end(), REF_FORM_EXPLICIT))

    path_spans = _path_token_spans(authority_text)
    for match in _BARE_HASH_RE.finditer(authority_text):
        # Skip bare "#N" occurrences that are actually the tail of an
        # "owner/repo#N" match already captured above.
        start = match.start()
        if start > 0 and authority_text[start - 1] == "/":
            continue
        # Issue #2864 (W3): a ``#N`` inside a path / URL-like token is not a
        # current-repo reference.
        if _inside_path_token(start, path_spans):
            continue
        number = int(match.group(1))
        prefix = authority_text[max(0, start - 20) : start]
        is_pr = bool(_PR_PREFIX_RE.search(prefix))
        ref_kind = "pr" if is_pr else "issue"
        form = REF_FORM_PREFIXED if (is_pr or _ISSUE_PREFIX_RE.search(prefix)) else REF_FORM_BARE
        target = Target(repo=current_repo, ref_kind=ref_kind, ref_number=number, explicit_repo=False)
        occurrences.append(_Occurrence(target, match.start(), match.end(), form))

    owner_repo_spans = [m.span() for m in _OWNER_REPO_HASH_RE.finditer(authority_text)]
    for match in _ADJACENT_PREFIX_HASH_RE.finditer(authority_text):
        # A prefix word inside an already recognised ``owner/repo#N`` span
        # (``owner/my-issue#12``) is part of the repo name, not a second ref.
        if _inside_any_span(match.start(), owner_repo_spans):
            continue
        # Issue #2864 (W3): ``src/あIssue#12`` is a path-embedded ``#N`` as well.
        if _inside_path_token(match.end(1), path_spans):
            continue
        ref_kind = "issue" if match.group(1).lower() == "issue" else "pr"
        target = Target(repo=current_repo, ref_kind=ref_kind, ref_number=int(match.group(2)), explicit_repo=False)
        occurrences.append(_Occurrence(target, match.start(), match.end(), REF_FORM_PREFIXED))

    return occurrences


def _dedupe_targets(occurrences: list[_Occurrence]) -> list[Target]:
    seen: dict[tuple[str | None, str, int], Target] = {}
    for occurrence in occurrences:
        seen.setdefault(_target_key(occurrence.target), occurrence.target)
    return list(seen.values())


def _find_targets(authority_text: str, current_repo: str | None) -> list[Target]:
    return _dedupe_targets(_find_occurrences(authority_text, current_repo))


# Issue #2827 clause boundaries (defined newly by this Issue): `。` `．` `？` `！`
# `?` `!` newline, and a `.` that is followed by whitespace or end of text.
_CLAUSE_DELIMITERS = frozenset("。．？！?!\n")


def _clause_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    length = len(text)
    for index, char in enumerate(text):
        if char in _CLAUSE_DELIMITERS or (char == "." and (index + 1 == length or text[index + 1].isspace())):
            spans.append((start, index + 1))
            start = index + 1
    if start < length:
        spans.append((start, length))
    return spans


def _clause_index(spans: list[tuple[int, int]], position: int) -> int:
    for index, (start, end) in enumerate(spans):
        if start <= position < end:
            return index
    return len(spans) - 1 if spans else 0


def _clause_index_from_ends(clause_ends: list[int], position: int) -> int:
    """``_clause_index()`` over a precomputed list of clause end positions,
    using ``bisect`` instead of a linear scan. ``_clause_spans`` spans are
    contiguous from 0, so ``start <= position < end`` holds for the first end
    greater than ``position``. Out-of-range (or negative) positions fall back
    to the last clause and an empty list to 0, exactly like ``_clause_index``."""
    if not clause_ends:
        return 0
    index = bisect_right(clause_ends, position) if position >= 0 else len(clause_ends)
    return index if index < len(clause_ends) else len(clause_ends) - 1


# Issue #2827 marker-local segments: a clause that contains a Japanese
# reference-only marker is split further at `、` and ASCII `,`. Only a
# reference in the same segment as the marker is demoted. This is deliberately
# separate from ``_CLAUSE_DELIMITERS`` (used by ``_clause_spans``): adding `、`
# there would also change ACTIVE target-phrase pairing and creation/reply
# exclusion, which must keep the generic clause semantics.
_MARKER_SEGMENT_DELIMITERS = frozenset("、,")


def _marker_segment_spans(text: str, clause: tuple[int, int]) -> list[tuple[int, int]]:
    start, end = clause
    spans: list[tuple[int, int]] = []
    segment_start = start
    for index in range(start, end):
        if text[index] in _MARKER_SEGMENT_DELIMITERS:
            spans.append((segment_start, index + 1))
            segment_start = index + 1
    if segment_start < end:
        spans.append((segment_start, end))
    return spans


# Issue #2850 marker-bound reference list (bounded rule). A Japanese
# reference-only marker also binds the reference LIST that directly follows it:
#   * an element is a marker-local segment (split at `、` / ASCII `,`) whose first
#     non-space token is a reference (bare ``#N`` / prefixed ``Issue #N`` /
#     ``owner/repo#N`` / full URL); the particle / verb phrase after the
#     reference (``を実装して`` ...) belongs to the same element;
#   * the list starts at the marker's own segment and continues segment by
#     segment, and ends at the first segment whose first token is NOT a reference,
#     or at the clause end (the generic ``_clause_spans``: `。` `．` `？` `！`
#     `?` `!`, newline, `.` + whitespace/end). A reference after that end is not
#     part of the list (``参考: #10、#11。Issue #12を実装して`` keeps #12 primary);
#   * references before the marker (a different clause or an earlier segment that
#     does not contain the marker) are never demoted.
# Boundary choice: the list is bounded by the existing clause delimiters plus the
# "first token is a reference" test instead of widening ``_CLAUSE_DELIMITERS``
# (which would change ACTIVE target-phrase pairing and creation/reply
# exclusion). Consequence of the literal rule: every reference-led segment right
# after a marker segment is a list element, so
# ``関連資料: #2826、Issue #2827 を対象にレビューして`` demotes #2827 too.
_REFERENCE_LEAD_PREFIX_RE = re.compile(r"(?:issue|pr|pull request|イシュー|プルリクエスト|プルリク)\s*", re.IGNORECASE)


# Issue #2864 (W2): which marker segment STARTS the list that propagates to the
# following segments. A marker segment always demotes the references that share
# its own segment (before or after the marker: ``Issue #3 を参考に実装して`` stays
# REFERENCE_ONLY), but the demotion only propagates to the next segments when
# that marker segment itself holds a valid reference that starts AFTER the marker
# (``参考: #10、#11`` / ``参考#1、#2``). ``参考に、#13を実装して`` and
# ``Issue #5を参考に、#13を実装して`` have no reference after the marker in the
# marker's own segment, so the following ``#13`` is the request target and is not
# demoted. No new terminator (``対象`` / ``レビュー`` / ``実装``) is added.
#
# Issue #2864 (W1): complexity of the marker binding. ``_find_occurrences()``
# returns occurrences in a kind-by-kind order that is NOT ascending in ``start``
# and that order is externally observable (``target`` / ``targets``), so it is
# never sorted or reordered. Instead a SEARCH-ONLY ascending list of the start
# positions is built once (O(n log n) sort) and both former quadratic paths use
# ``bisect`` on it / on the sorted marker-segment spans:
#   (a) "does this segment start with a reference" -> one ``bisect`` per segment
#       instead of scanning every occurrence per segment;
#   (b) the final "is this occurrence inside a marker segment" exclusion -> one
#       ``bisect`` per occurrence on the (already ascending, disjoint) marker
#       segment spans instead of scanning every marker segment per occurrence.
# Overall O(n log n) in the number of occurrences / segments (NOT linear); the
# remaining text scans (clause / segment splitting, ``str.find``) are linear.


def _has_start_in(starts: list[int], low: int, high: int) -> bool:
    """True when some occurrence start lies in ``[low, high)`` (``starts`` is the
    ascending search-only start index)."""
    index = bisect_left(starts, low)
    return index < len(starts) and starts[index] < high


def _first_marker_end(text: str, segment: tuple[int, int]) -> int | None:
    """End index of the earliest Japanese reference-only marker inside
    ``segment`` (``None`` when the segment holds no marker)."""
    start, end = segment
    ends = [
        found + len(marker) for marker in _JA_REFERENCE_ONLY_MARKERS if (found := text.find(marker, start, end)) != -1
    ]
    return min(ends) if ends else None


def _segment_starts_with_reference(text: str, segment: tuple[int, int], starts: list[int]) -> bool:
    start, end = segment
    position = start
    while position < end and text[position].isspace():
        position += 1
    if position >= end:
        return False
    # The reference may start right at the first non-space character, or right
    # after one closed-set prefix word (``Issue `` / ``PR`` / ``イシュー`` ...).
    if _has_start_in(starts, position, position + 1):
        return True
    lead = _REFERENCE_LEAD_PREFIX_RE.match(text, position, end)
    return lead is not None and lead.end() < end and _has_start_in(starts, lead.end(), lead.end() + 1)


def _primary_occurrences(authority_text: str, occurrences: list[_Occurrence]) -> list[_Occurrence]:
    """Drop the occurrences demoted by a Japanese reference-only marker: a
    reference is demoted when it shares a marker-local segment (its clause split
    at `、` / `,`) with ``参考`` or ``関連資料``, or when it is an element of the
    reference list that directly follows a marker segment holding a reference
    after the marker (Issue #2850 / #2864, see the comments above). References
    before the marker and after the list end stay primary candidates. The input
    order of ``occurrences`` is preserved."""
    spans = _clause_spans(authority_text)
    # Search-only index (never exposed): ascending occurrence start positions.
    starts = sorted(o.start for o in occurrences)
    marker_segments: list[tuple[int, int]] = []
    for clause in spans:
        if not any(marker in authority_text[clause[0] : clause[1]] for marker in _JA_REFERENCE_ONLY_MARKERS):
            continue
        in_list = False
        for segment in _marker_segment_spans(authority_text, clause):
            marker_end = _first_marker_end(authority_text, segment)
            if marker_end is not None:
                marker_segments.append(segment)
                # W2: propagate only when a reference starts after the marker.
                in_list = _has_start_in(starts, marker_end, segment[1])
            elif in_list and _segment_starts_with_reference(authority_text, segment, starts):
                marker_segments.append(segment)
            else:
                in_list = False
    if not marker_segments:
        return list(occurrences)
    # ``marker_segments`` is ascending and disjoint (clauses and their segments
    # are visited left to right), so one ``bisect`` per occurrence is enough.
    segment_starts = [start for start, _ in marker_segments]

    def demoted(occurrence: _Occurrence) -> bool:
        index = bisect_right(segment_starts, occurrence.start) - 1
        return index >= 0 and occurrence.start < marker_segments[index][1]

    return [o for o in occurrences if not demoted(o)]


def _has_reference_only_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _REFERENCE_ONLY_MARKERS)


def classify(prompt: str, *, current_repo: str | None = None) -> Classification:
    """Classify a raw UserPromptSubmit prompt string.

    Precedence (Issue #2564 In Scope, exactly): `/task` special-case first;
    then single high-confidence same/other-target; then REFERENCE_ONLY/NONE;
    then AMBIGUOUS (multiple distinct high-confidence targets -- silent
    rebind never happens for these, only PASS + advisory).

    Issue #2827: Japanese reference-only markers (``参考`` / ``関連資料``)
    demote only the references in the marker's own local segment (a clause
    further split on `、` / ASCII `,`; the generic ``_clause_spans`` semantics
    are unchanged) before the primary count is taken; when every reference is
    demoted the prompt is REFERENCE_ONLY."""
    if prompt is None:
        prompt = ""

    slash_match = _SLASH_TASK_RE.match(prompt)
    if slash_match:
        raw_target = (slash_match.group(1) or "").strip()
        return Classification(kind=KIND_SLASH_TASK, slash_task_raw_target=raw_target or None)

    authority_text = _strip_authority_exclusions(prompt)
    occurrences = _find_occurrences(authority_text, current_repo)
    targets = _dedupe_targets(occurrences)

    if not targets:
        return Classification(kind=KIND_NONE)

    primary_targets = _dedupe_targets(_primary_occurrences(authority_text, occurrences))

    if not primary_targets:
        return Classification(kind=KIND_REFERENCE_ONLY, target=targets[0], targets=tuple(targets))

    if len(primary_targets) > 1:
        return Classification(kind=KIND_AMBIGUOUS, targets=tuple(primary_targets))

    target = primary_targets[0]
    if _has_reference_only_marker(authority_text):
        return Classification(kind=KIND_REFERENCE_ONLY, target=target, targets=(target,))

    kind = KIND_EXPLICIT if target.explicit_repo else KIND_INFERRED
    return Classification(kind=kind, target=target, targets=(target,))


# ---------------------------------------------------------------------------
# Issue #2827: ACTIVE-only rebind projection.
#
# UNBOUND autobind / provisional absorb / terminal rebind keep using the
# legacy ``classification_kind`` + ``target_*`` fields unchanged. Only the
# ACTIVE different-primary branch of ``on_user_prompt_submit`` reads the
# ``active_rebind_*`` projection this function produces, which is stricter:
# a reference form from the closed set must be paired with a deterministic
# target phrase in the SAME clause (or be the entire prompt), and a
# create/reply verb phrase in that clause excludes it. No DB, no network, no
# LLM -- the adapter only reports syntax; live-claim resolution stays in core.
# ---------------------------------------------------------------------------

_JA_TARGET_PHRASES = ("対象", "を実装", "を改善", "をレビュー", "を修正", "作業開始", "に取り組", "に切り替")
_EN_TARGET_PHRASE_RE = re.compile(r"\b(?:work on|review|implement|refine|fix|switch to)\b", re.IGNORECASE)
_JA_CREATION_REPLY_PHRASES = (
    "を起票",
    "に返信",
    "へ返信",
    "にコメントして",
    "にコメントする",
    "にコメントを投稿",
    "を投稿",
)
_EN_CREATION_REPLY_RE = re.compile(
    r"\b(?:create an issue|file an issue|reply to|comment on|post a comment)\b", re.IGNORECASE
)

_TRAILING_PUNCTUATION_RE = re.compile(r"[\s。．.、,!！?？]+$")
_WHOLE_PROMPT_PREFIXED_REF_RE = re.compile(
    r"^(?:issue|pr|pull request|イシュー|プルリクエスト|プルリク)\s*#\d+$", re.IGNORECASE
)


def _clause_has_target_phrase(clause: str) -> bool:
    return any(phrase in clause for phrase in _JA_TARGET_PHRASES) or bool(_EN_TARGET_PHRASE_RE.search(clause))


def _clause_has_creation_or_reply_phrase(clause: str) -> bool:
    return any(phrase in clause for phrase in _JA_CREATION_REPLY_PHRASES) or bool(
        _EN_CREATION_REPLY_RE.search(clause)
    )


def _is_whole_prompt_single_explicit_reference(prompt: str) -> bool:
    """True when the prompt (minus surrounding whitespace and trailing
    punctuation) is exactly one reference form: a full GitHub URL,
    ``owner/repo#N`` or a prefix word from the closed set directly before
    ``#N``. A bare ``#N`` alone is not an explicit reference."""
    text = _TRAILING_PUNCTUATION_RE.sub("", prompt.strip())
    if not text:
        return False
    return bool(
        _GITHUB_URL_RE.fullmatch(text)
        or _EXPLICIT_TARGET_OWNER_REPO_RE.fullmatch(text)
        or _WHOLE_PROMPT_PREFIXED_REF_RE.fullmatch(text)
    )


def active_rebind_projection(
    prompt: str | None, classification: Classification, *, current_repo: str | None = None
) -> dict[str, object]:
    """Return the ACTIVE-only projection keys for ``prompt``.

    ``active_rebind_primary_eligible`` is always present. Only when it is
    ``True`` are ``active_rebind_target_repo`` / ``_ref_kind`` / ``_ref_number``
    / ``active_rebind_ref_form`` added. Guarantee: when eligible, the
    projection target equals the legacy ``classification.target`` and the
    legacy kind is EXPLICIT or INFERRED."""
    ineligible: dict[str, object] = {"active_rebind_primary_eligible": False}
    if classification.kind not in (KIND_EXPLICIT, KIND_INFERRED) or classification.target is None:
        return ineligible
    target = classification.target
    if not target.repo:
        return ineligible

    prompt = prompt or ""
    authority_text = _strip_authority_exclusions(prompt)
    occurrences = _find_occurrences(authority_text, current_repo)
    primary = [
        o for o in _primary_occurrences(authority_text, occurrences) if _target_key(o.target) == _target_key(target)
    ]
    if not primary:
        return ineligible

    ref_form: str | None = None
    if _is_whole_prompt_single_explicit_reference(prompt) and len(occurrences) == 1:
        if primary[0].form in (REF_FORM_EXPLICIT, REF_FORM_PREFIXED):
            ref_form = primary[0].form
    if ref_form is None:
        # Issue #2871: clause processing only (NOT the scanner front-end, NOT
        # classify() / the hook as a whole). With input length N, primary
        # occurrence count K and clause count C this part is
        # O(N + K log(C + 1)): the clause end positions are materialised once,
        # each occurrence's clause is located by ``bisect`` (same fallback as
        # ``_clause_index()``), and each clause is sliced and scanned by the
        # target / creation-reply predicates at most once per call. The cache
        # is invocation-local and keyed by clause index; hits are decided by
        # key presence so a cached ``False`` is reused. ``primary`` is walked in
        # its own list order (never sorted by position) so the first
        # occurrence in that order that qualifies still decides ``ref_form``.
        spans = _clause_spans(authority_text)
        clause_ends = [end for _, end in spans]
        clause_eligible: dict[int, bool] = {}
        for occurrence in primary:
            index = _clause_index_from_ends(clause_ends, occurrence.start)
            if index not in clause_eligible:
                start, end = spans[index]
                clause = authority_text[start:end]
                clause_eligible[index] = _clause_has_target_phrase(
                    clause
                ) and not _clause_has_creation_or_reply_phrase(clause)
            if clause_eligible[index]:
                ref_form = occurrence.form
                break
    if ref_form is None:
        return ineligible

    return {
        "active_rebind_primary_eligible": True,
        "active_rebind_target_repo": target.repo,
        "active_rebind_target_ref_kind": target.ref_kind,
        "active_rebind_target_ref_number": target.ref_number,
        "active_rebind_ref_form": ref_form,
    }


_EXPLICIT_TARGET_URL_RE = _GITHUB_URL_RE
_EXPLICIT_TARGET_OWNER_REPO_RE = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
_EXPLICIT_TARGET_BARE_RE = re.compile(r"^#?(\d+)$")
_EXPLICIT_TARGET_KIND_WORD_RE = re.compile(
    r"^(issue|pr|pull request)\s*#?(\d+)$", re.IGNORECASE
)


def needs_current_repo_resolution(prompt: str) -> bool:
    """PR #2615 fix_delta 6: cheap lexical pre-check -- does ``prompt``
    contain a pattern whose classification would actually consult
    ``current_repo``? Full GitHub URLs and explicit ``owner/repo#N``
    targets never need it (the repo is already spelled out), so the
    caller (``hook_entry.py``) can skip its ``git remote get-url origin``
    subprocess call entirely for those prompts instead of running it
    unconditionally on every ``UserPromptSubmit``.

    Only a syntactic (not full ``classify()``) check -- it may return
    ``True`` for a prompt that ``classify()`` later discards for other
    reasons (e.g. inside a reference-only marker), but it must never
    return ``False`` for a prompt that actually needs ``current_repo``."""
    if not prompt:
        return False

    slash_match = _SLASH_TASK_RE.match(prompt)
    if slash_match:
        raw_target = (slash_match.group(1) or "").strip()
        if not raw_target:
            return False
        if _EXPLICIT_TARGET_URL_RE.match(raw_target) or _EXPLICIT_TARGET_OWNER_REPO_RE.match(raw_target):
            return False
        # `parse_slash_task_target` only consults `current_repo` for a bare
        # number (`#N`/`N`) or a bare `issue|pr #N` kind-word target -- any
        # other raw_target falls through to the ad-hoc-title branch, which
        # never touches current_repo.
        return bool(
            _EXPLICIT_TARGET_BARE_RE.match(raw_target) or _EXPLICIT_TARGET_KIND_WORD_RE.match(raw_target)
        )

    authority_text = _strip_authority_exclusions(prompt)
    path_spans = _path_token_spans(authority_text)
    for match in _BARE_HASH_RE.finditer(authority_text):
        start = match.start()
        if start > 0 and authority_text[start - 1] == "/":
            continue
        # Issue #2864 (W3): keep in sync with ``_find_occurrences``.
        if _inside_path_token(start, path_spans):
            continue
        return True
    # Issue #2850: ``Issue#12`` / ``PR#34`` also resolve against ``current_repo``.
    owner_repo_spans = [m.span() for m in _OWNER_REPO_HASH_RE.finditer(authority_text)]
    return any(
        not _inside_any_span(match.start(), owner_repo_spans) and not _inside_path_token(match.end(1), path_spans)
        for match in _ADJACENT_PREFIX_HASH_RE.finditer(authority_text)
    )


def raw_target_needs_current_repo_resolution(raw_target: str) -> bool:
    """Issue #2625 fix_delta (OWNER PR review): the ``UserPromptExpansion``
    counterpart of ``needs_current_repo_resolution`` above, operating
    directly on an already-``/task``-prefix-stripped ``raw_target`` string
    (as produced by ``hook_entry._command_args_to_raw_target``) instead of
    the raw prompt text with the leading ``/task`` still attached.

    Only the bare-number (``#N``/``N``) and kind-word (``issue|pr #N``)
    shapes ever consult ``current_repo`` inside ``parse_slash_task_target``
    below -- a full URL or explicit ``owner/repo#N`` target already spells
    out the repo, and anything else falls through to the ad-hoc-title
    branch, which never touches ``current_repo`` either.

    Only a syntactic pre-check (mirrors the same invariant as
    ``needs_current_repo_resolution``): it must never return ``False`` for a
    ``raw_target`` that actually needs ``current_repo``."""
    raw_target = (raw_target or "").strip()
    if not raw_target:
        return False
    if _EXPLICIT_TARGET_URL_RE.match(raw_target) or _EXPLICIT_TARGET_OWNER_REPO_RE.match(raw_target):
        return False
    return bool(
        _EXPLICIT_TARGET_BARE_RE.match(raw_target) or _EXPLICIT_TARGET_KIND_WORD_RE.match(raw_target)
    )


def parse_slash_task_target(raw_target: str, *, current_repo: str | None) -> tuple[Target | None, str | None]:
    """Resolve the ``/task <target>`` raw target string into either a
    structured :class:`Target` (GitHub ref) or an ad-hoc task title.

    Returns ``(target, ad_hoc_title)`` -- exactly one of the two is
    non-``None`` (unless ``raw_target`` is empty, in which case both are
    ``None`` -- an explicit `/task` with no target is a validation failure
    the caller must surface, not silently ignore)."""
    raw_target = (raw_target or "").strip()
    if not raw_target:
        return None, None

    url_match = _EXPLICIT_TARGET_URL_RE.match(raw_target)
    if url_match:
        repo, kind_word, number = url_match.group(1), url_match.group(2), int(url_match.group(3))
        ref_kind = "pr" if kind_word.lower() == "pull" else "issue"
        return Target(repo=repo, ref_kind=ref_kind, ref_number=number), None

    owner_repo_match = _EXPLICIT_TARGET_OWNER_REPO_RE.match(raw_target)
    if owner_repo_match:
        repo, number = owner_repo_match.group(1), int(owner_repo_match.group(2))
        return Target(repo=repo, ref_kind="issue", ref_number=number), None

    kind_word_match = _EXPLICIT_TARGET_KIND_WORD_RE.match(raw_target)
    if kind_word_match:
        kind_word, number = kind_word_match.group(1).lower(), int(kind_word_match.group(2))
        ref_kind = "issue" if kind_word == "issue" else "pr"
        return Target(repo=current_repo, ref_kind=ref_kind, ref_number=number), None

    bare_match = _EXPLICIT_TARGET_BARE_RE.match(raw_target)
    if bare_match:
        return Target(repo=current_repo, ref_kind="issue", ref_number=int(bare_match.group(1))), None

    # Not a recognizable GitHub ref shorthand -- treat the raw target string
    # as an ad-hoc task title/label.
    return None, raw_target
