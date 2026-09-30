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
# marker only demotes the references that sit in the SAME clause (see
# ``_clause_spans``); a reference in another clause stays a primary candidate.
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
_OWNER_REPO_HASH_RE = re.compile(r"\b([\w.-]+/[\w.-]+)#(\d+)\b")
_BARE_HASH_RE = re.compile(r"(?<![\w/])#(\d+)\b")
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

    for match in _BARE_HASH_RE.finditer(authority_text):
        # Skip bare "#N" occurrences that are actually the tail of an
        # "owner/repo#N" match already captured above.
        start = match.start()
        if start > 0 and authority_text[start - 1] == "/":
            continue
        number = int(match.group(1))
        prefix = authority_text[max(0, start - 20) : start]
        is_pr = bool(_PR_PREFIX_RE.search(prefix))
        ref_kind = "pr" if is_pr else "issue"
        form = REF_FORM_PREFIXED if (is_pr or _ISSUE_PREFIX_RE.search(prefix)) else REF_FORM_BARE
        target = Target(repo=current_repo, ref_kind=ref_kind, ref_number=number, explicit_repo=False)
        occurrences.append(_Occurrence(target, match.start(), match.end(), form))

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


def _primary_occurrences(authority_text: str, occurrences: list[_Occurrence]) -> list[_Occurrence]:
    """Drop the occurrences demoted by a Japanese reference-only marker: a
    reference is demoted only when it shares a marker-local segment (its clause
    split at `、` / `,`) with ``参考`` or ``関連資料``. References in other
    segments or clauses stay primary candidates."""
    spans = _clause_spans(authority_text)
    marker_segments: list[tuple[int, int]] = []
    for clause in spans:
        if not any(marker in authority_text[clause[0] : clause[1]] for marker in _JA_REFERENCE_ONLY_MARKERS):
            continue
        for segment in _marker_segment_spans(authority_text, clause):
            if any(marker in authority_text[segment[0] : segment[1]] for marker in _JA_REFERENCE_ONLY_MARKERS):
                marker_segments.append(segment)
    if not marker_segments:
        return list(occurrences)
    return [o for o in occurrences if not any(start <= o.start < end for start, end in marker_segments)]


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
    demote only the same-clause references before the primary count is taken;
    when every reference is demoted the prompt is REFERENCE_ONLY."""
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
        spans = _clause_spans(authority_text)
        for occurrence in primary:
            start, end = spans[_clause_index(spans, occurrence.start)]
            clause = authority_text[start:end]
            if _clause_has_target_phrase(clause) and not _clause_has_creation_or_reply_phrase(clause):
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
    for match in _BARE_HASH_RE.finditer(authority_text):
        start = match.start()
        if start > 0 and authority_text[start - 1] == "/":
            continue
        return True
    return False


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
