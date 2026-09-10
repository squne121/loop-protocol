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
_PR_PREFIX_RE = re.compile(r"\b(pr|pull request)\s*$", re.IGNORECASE)

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


def _find_targets(authority_text: str, current_repo: str | None) -> list[Target]:
    seen: dict[tuple[str | None, str, int], Target] = {}

    for match in _GITHUB_URL_RE.finditer(authority_text):
        repo, kind_word, number = match.group(1), match.group(2), int(match.group(3))
        ref_kind = "pr" if kind_word.lower() == "pull" else "issue"
        key = (repo, ref_kind, number)
        seen.setdefault(key, Target(repo=repo, ref_kind=ref_kind, ref_number=number, explicit_repo=True))

    for match in _OWNER_REPO_HASH_RE.finditer(authority_text):
        repo, number = match.group(1), int(match.group(2))
        prefix = authority_text[: match.start()]
        ref_kind = "pr" if _PR_PREFIX_RE.search(prefix.split("/")[0][-20:] or "") else "issue"
        key = (repo, ref_kind, number)
        seen.setdefault(key, Target(repo=repo, ref_kind=ref_kind, ref_number=number, explicit_repo=True))

    for match in _BARE_HASH_RE.finditer(authority_text):
        # Skip bare "#N" occurrences that are actually the tail of an
        # "owner/repo#N" match already captured above.
        start = match.start()
        if start > 0 and authority_text[start - 1] == "/":
            continue
        number = int(match.group(1))
        prefix = authority_text[max(0, start - 20) : start]
        ref_kind = "pr" if _PR_PREFIX_RE.search(prefix) else "issue"
        key = (current_repo, ref_kind, number)
        seen.setdefault(key, Target(repo=current_repo, ref_kind=ref_kind, ref_number=number, explicit_repo=False))

    return list(seen.values())


def _has_reference_only_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _REFERENCE_ONLY_MARKERS)


def classify(prompt: str, *, current_repo: str | None = None) -> Classification:
    """Classify a raw UserPromptSubmit prompt string.

    Precedence (Issue #2564 In Scope, exactly): `/task` special-case first;
    then single high-confidence same/other-target; then REFERENCE_ONLY/NONE;
    then AMBIGUOUS (multiple distinct high-confidence targets -- silent
    rebind never happens for these, only PASS + advisory)."""
    if prompt is None:
        prompt = ""

    slash_match = _SLASH_TASK_RE.match(prompt)
    if slash_match:
        raw_target = (slash_match.group(1) or "").strip()
        return Classification(kind=KIND_SLASH_TASK, slash_task_raw_target=raw_target or None)

    authority_text = _strip_authority_exclusions(prompt)
    targets = _find_targets(authority_text, current_repo)

    if not targets:
        return Classification(kind=KIND_NONE)

    if len(targets) > 1:
        return Classification(kind=KIND_AMBIGUOUS, targets=tuple(targets))

    target = targets[0]
    if _has_reference_only_marker(authority_text):
        return Classification(kind=KIND_REFERENCE_ONLY, target=target, targets=(target,))

    kind = KIND_EXPLICIT if target.explicit_repo else KIND_INFERRED
    return Classification(kind=kind, target=target, targets=(target,))


_EXPLICIT_TARGET_URL_RE = _GITHUB_URL_RE
_EXPLICIT_TARGET_OWNER_REPO_RE = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")
_EXPLICIT_TARGET_BARE_RE = re.compile(r"^#?(\d+)$")
_EXPLICIT_TARGET_KIND_WORD_RE = re.compile(
    r"^(issue|pr|pull request)\s*#?(\d+)$", re.IGNORECASE
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
