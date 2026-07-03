#!/usr/bin/env python3
"""pretool_fastpath_classifier.py — shared PreToolUse fast-path classifier.

Issue #1289.

This module is a **shared library**, not an independent PreToolUse hook. It is
imported by ``local_main_branch_guard.py`` / ``.claude/hooks/worktree_scope_guard.py``
(and their Bash wrappers) to compute a deterministic fast-path classification for
a Bash command, so that read-only commands and exact controlled-executor
invocations can skip redundant hook status output / duplicate telemetry without
changing the underlying fail-closed decision of either existing guard.

Classification vocabulary
--------------------------
- ``readonly_display``: the command is read-only per **both**
  ``local_main_branch_guard.is_readonly_command`` and
  ``worktree_scope_guard.classify_bash`` (intersection). If either guard would
  classify the command as unknown/mutating/cleanup/metadata-mutation, the
  command falls through to ``mutation_or_unknown``.
- ``exact_controlled_executor_authorized``: the command is an exact,
  canonical-path invocation of ``controlled_skill_mutation_exec.py`` (or
  ``skill_runtime_exec.py``) *and* satisfies the additional authorization
  conditions (command_id is a member of the registry's ``ALL_COMMAND_IDS``,
  explicit repo binding to the trusted repo slug, input namespace shape
  matches the registry entry, and a stable policy_hash is computable).
- ``exact_controlled_executor_shape``: the command matches the canonical
  executor path + exact argv shape, but does not satisfy every authorization
  condition above (e.g. an unrecognized command_id, wrong repo, or namespace
  mismatch). This is a **non-terminal, internal** classification: callers of
  ``classify()`` never see it directly — it always folds into
  ``mutation_or_unknown`` because the fast-path/telemetry-dedupe reduction is
  only available for the fully authorized case (Issue #1289 In Scope).
- ``mutation_or_unknown``: default. The command must go through the existing
  fail-closed hook chain unchanged.

Dependency note (Issue #1291): raw ``gh issue edit`` / ``gh issue comment``
allowlist commands are NOT controlled-executor invocations (they do not
reference ``controlled_skill_mutation_exec.py`` / ``skill_runtime_exec.py`` at
all), so they can never match the executor shape check and always fall through
to ``mutation_or_unknown`` here — this module does not need special-case logic
to exclude them, but the adversarial test suite verifies this explicitly so a
future edit to the shape matcher cannot silently widen it to swallow raw
allowlist commands.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from local_main_branch_guard import (  # noqa: E402
    is_readonly_command as _lmb_is_readonly_command,
    _parse_gh_api_command as _lmb_parse_gh_api_command,
)
from skill_runtime_command_policy import (  # noqa: E402
    is_exact_skill_runtime_executor_command,
    looks_like_skill_runtime_executor_command,
    parse_exact_skill_runtime_command,
    load_registry_entry,
    SKILL_RUNTIME_COMMAND_POLICY_V2,
    TRUSTED_REPO_SLUG,
)
from controlled_skill_mutation_policy import (  # noqa: E402
    is_controlled_skill_mutation_exec_command,
    CONTROLLED_SKILL_MUTATION_COMMAND_POLICY,
    ALL_COMMAND_IDS,
    TRUSTED_REPO as CSM_TRUSTED_REPO,
)

_CODEX_ROOT = os.path.dirname(os.path.dirname(_HERE))
_HOOKS_DIR = os.path.join(_CODEX_ROOT, ".claude", "hooks")
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)

try:
    from worktree_scope_guard import classify_bash as _wsg_classify_bash  # noqa: E402
except Exception:  # pragma: no cover - defensive import guard
    _wsg_classify_bash = None  # type: ignore[assignment]


# ── Classification vocabulary ──────────────────────────────────────────────

CLASS_READONLY_DISPLAY = "readonly_display"
CLASS_EXACT_AUTHORIZED = "exact_controlled_executor_authorized"
CLASS_EXACT_SHAPE = "exact_controlled_executor_shape"
CLASS_MUTATION_OR_UNKNOWN = "mutation_or_unknown"

# Only these two classifications are ever externally observable from classify().
TERMINAL_CLASSIFICATIONS = frozenset(
    {CLASS_READONLY_DISPLAY, CLASS_EXACT_AUTHORIZED, CLASS_MUTATION_OR_UNKNOWN}
)

_GH_API_RE = re.compile(r"^gh\s+api(\s|$)")


@dataclass(frozen=True)
class FastpathClassification:
    classification: str
    command_id: str | None = None
    policy_hash: str | None = None
    display_summary: str | None = None
    internal_shape_only: bool = field(default=False, compare=False)

    def to_telemetry_dict(self) -> dict[str, Any]:
        """Bounded telemetry payload. Never includes raw command body or
        secret-like values (Issue #1289 AC3)."""
        payload: dict[str, Any] = {"classification": self.classification}
        if self.command_id is not None:
            payload["command_id"] = self.command_id
        if self.policy_hash is not None:
            payload["policy_hash"] = self.policy_hash
        if self.display_summary is not None:
            payload["display_summary"] = self.display_summary
        return payload


# ── readonly_display: intersection of both guards ──────────────────────────


def _is_readonly_intersection(cmd: str) -> bool:
    """True iff both local_main_branch_guard and worktree_scope_guard agree the
    command is read-only. `gh api` is checked with a dedicated token-level
    intersection (both guards' independent gh-api parsers must agree)."""
    if _wsg_classify_bash is None:
        return False
    stripped = cmd.strip()
    if _GH_API_RE.match(stripped):
        return bool(_lmb_parse_gh_api_command(stripped)) and _wsg_classify_bash(cmd) == "read_only"
    return bool(_lmb_is_readonly_command(cmd)) and _wsg_classify_bash(cmd) == "read_only"


# Fixed, closed-vocabulary command-kind labels for the bounded telemetry
# summary. NEVER derived from raw command tokens (Issue #1289 Blocker 3):
# raw tokens can carry search queries (`rg <secret>`), file paths, issue
# body fragments, or secret-like values (e.g. `ghp_...`), all of which must
# never reach the summary/telemetry payload.
_SEARCH_BINARIES = frozenset({"rg", "grep", "egrep", "fgrep", "ag"})
_FILE_DISPLAY_BINARIES = frozenset({"cat", "head", "tail", "less", "more"})
_GH_SUBCOMMAND_LABELS = frozenset({"issue", "pr", "repo", "run", "workflow", "api", "release"})


def _bounded_summary(cmd: str) -> str:
    """A bounded, closed-vocabulary summary for telemetry dedupe (AC1).

    Built exclusively from a fixed command-kind label taxonomy — never from
    raw command tokens. Query strings, paths, issue body text, URL
    fragments, and secret-like values must never appear here (Issue #1289
    Blocker 3).
    """
    stripped = cmd.strip()
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    if not tokens:
        return "readonly_display"

    binary = os.path.basename(tokens[0])

    if binary == "git":
        subcmd = tokens[1] if len(tokens) > 1 else None
        if subcmd and re.fullmatch(r"[a-z][a-z0-9_-]*", subcmd):
            return f"readonly_display:git:{subcmd}"
        return "readonly_display:git"

    if binary == "gh":
        subcmd = tokens[1] if len(tokens) > 1 else None
        action = tokens[2] if len(tokens) > 2 else None
        if subcmd in _GH_SUBCOMMAND_LABELS:
            if action and re.fullmatch(r"[a-z][a-z0-9_-]*", action):
                return f"readonly_display:gh:{subcmd}:{action}"
            return f"readonly_display:gh:{subcmd}"
        return "readonly_display:gh"

    if binary in _SEARCH_BINARIES:
        return "readonly_display:search"

    if binary in _FILE_DISPLAY_BINARIES:
        return "readonly_display:file-display"

    return "readonly_display:other"


# ── exact_controlled_executor: shape vs authorized ─────────────────────────


def _extract_executor_flags(cmd: str) -> dict[str, str] | None:
    """Extract --flag value pairs from a (already shape-validated) executor
    invocation. Returns None if unparseable."""
    try:
        toks = shlex.split(cmd.strip())
    except ValueError:
        return None
    # Skip leading "uv run python3" / "python3" and the script path.
    if toks[:3] == ["uv", "run", "python3"]:
        rest = toks[3:]
    elif toks and os.path.basename(toks[0]) in ("python3", "python"):
        rest = toks[1:]
    else:
        return None
    if not rest:
        return None
    args = rest[1:]  # drop script token
    flags: dict[str, str] = {}
    i = 0
    while i < len(args):
        tok = args[i]
        if tok.startswith("--") and i + 1 < len(args) and not args[i + 1].startswith("--"):
            flags[tok] = args[i + 1]
            i += 2
            continue
        i += 1
    return flags


def _stable_policy_hash(entry: dict[str, Any]) -> str:
    canonical = json.dumps(entry, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _is_safe_namespaced_input_file(input_file: str | None, expected_prefix: str) -> bool:
    """Reject any --input-file value that is not a single, literal path
    segment directly under ``expected_prefix`` (Issue #1289 Blocker 2 fix).

    A raw ``str.startswith(expected_prefix)`` check is insufficient: a value
    like ``artifacts/1291/issue-metadata/issue_body.update/../../evil.json``
    also satisfies ``startswith(expected_prefix)`` as a plain string, but
    resolves *outside* the namespace once ``..`` segments are applied. This
    helper fails closed on absolute paths, backslashes, NUL bytes, the stdin
    marker ``-``, ``.``/``..`` path components, and any nested subdirectory
    beneath the expected namespace (i.e. the value must be exactly
    ``expected_prefix`` + one leaf filename, no more path segments).
    """
    if not input_file:
        return False
    if input_file == "-":
        return False
    if "\\" in input_file or "\x00" in input_file:
        return False
    if input_file.startswith("/"):
        return False
    if input_file.startswith("./") or input_file.startswith("../"):
        return False

    # Split on the raw string ourselves rather than via PurePosixPath: Path
    # objects silently collapse single "." segments (e.g.
    # "artifacts/1166/./x.json" -> parts without the "." at all), which would
    # let a disguised "./" traversal slip past a parts-based "." check.
    raw_parts = input_file.split("/")
    if any(part in (".", "..") for part in raw_parts):
        return False
    if not input_file.startswith(expected_prefix):
        return False

    prefix_parts = tuple(PurePosixPath(expected_prefix).parts)
    parts = tuple(PurePosixPath(input_file).parts)
    if parts[: len(prefix_parts)] != prefix_parts:
        return False
    # Exactly one path segment (the leaf filename) beyond the namespace
    # prefix — no nested subdirectories smuggled in under the prefix.
    if len(parts) != len(prefix_parts) + 1:
        return False
    return True


def _classify_controlled_skill_mutation(cmd: str, project_root: str) -> FastpathClassification | None:
    """Returns a classification if cmd matches the controlled_skill_mutation_exec.py
    shape (authorized or shape-only), else None (not this executor at all)."""
    if not is_controlled_skill_mutation_exec_command(cmd, project_root):
        return None

    flags = _extract_executor_flags(cmd)
    if not flags:
        return FastpathClassification(CLASS_EXACT_SHAPE, internal_shape_only=True)

    command_id = flags.get("--command-id")
    repo = flags.get("--repo")
    input_file = flags.get("--input-file")
    issue_number = flags.get("--issue-number")

    if command_id not in ALL_COMMAND_IDS:
        return FastpathClassification(CLASS_EXACT_SHAPE, internal_shape_only=True)
    if repo != CSM_TRUSTED_REPO:
        return FastpathClassification(CLASS_EXACT_SHAPE, command_id=command_id, internal_shape_only=True)

    entry = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY.get(command_id)
    if not entry:
        return FastpathClassification(CLASS_EXACT_SHAPE, command_id=command_id, internal_shape_only=True)

    namespace = entry.get("input_namespace")
    if namespace and issue_number:
        expected_prefix = namespace.format(issue_number=issue_number)
        if not _is_safe_namespaced_input_file(input_file, expected_prefix):
            return FastpathClassification(
                CLASS_EXACT_SHAPE, command_id=command_id, internal_shape_only=True
            )
    elif issue_number:
        # Legacy command id (termination_report.publish) has no explicit
        # input_namespace entry — require the artifacts/{issue_number}/ prefix.
        expected_prefix = f"artifacts/{issue_number}/"
        if not _is_safe_namespaced_input_file(input_file, expected_prefix):
            return FastpathClassification(
                CLASS_EXACT_SHAPE, command_id=command_id, internal_shape_only=True
            )
    else:
        return FastpathClassification(CLASS_EXACT_SHAPE, command_id=command_id, internal_shape_only=True)

    policy_hash = _stable_policy_hash(entry)
    return FastpathClassification(
        CLASS_EXACT_AUTHORIZED,
        command_id=command_id,
        policy_hash=policy_hash,
        display_summary=f"exact_controlled_executor_authorized:{command_id}",
    )


def _classify_skill_runtime(
    cmd: str, cwd: str, project_root: str, deadline: Any = None
) -> FastpathClassification | None:
    """Returns a classification if cmd matches the skill_runtime_exec.py shape
    (authorized or shape-only), else None (not this executor at all)."""
    if not looks_like_skill_runtime_executor_command(cmd):
        return None

    parsed = parse_exact_skill_runtime_command(cmd, project_root)
    if parsed is None:
        return FastpathClassification(CLASS_EXACT_SHAPE, internal_shape_only=True)

    if parsed.command_id not in SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]:
        return FastpathClassification(
            CLASS_EXACT_SHAPE, command_id=parsed.command_id, internal_shape_only=True
        )
    if parsed.repo != TRUSTED_REPO_SLUG:
        return FastpathClassification(
            CLASS_EXACT_SHAPE, command_id=parsed.command_id, internal_shape_only=True
        )

    if not is_exact_skill_runtime_executor_command(cmd, cwd, project_root, deadline):
        return FastpathClassification(
            CLASS_EXACT_SHAPE, command_id=parsed.command_id, internal_shape_only=True
        )

    try:
        entry = load_registry_entry(parsed.command_id, project_root)
    except Exception:
        entry = SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"][parsed.command_id]

    policy_hash = _stable_policy_hash(entry)
    return FastpathClassification(
        CLASS_EXACT_AUTHORIZED,
        command_id=parsed.command_id,
        policy_hash=policy_hash,
        display_summary=f"exact_controlled_executor_authorized:{parsed.command_id}",
    )


# ── Public entry point ─────────────────────────────────────────────────────


def classify(cmd: str, cwd: str, project_root: str, deadline: Any = None) -> FastpathClassification:
    """Deterministically classify a Bash command for PreToolUse fast-path
    purposes. Never independently registered as a PreToolUse hook (Issue #1289
    AC6) — always called from inside an existing guard."""
    if not cmd or not cmd.strip():
        return FastpathClassification(CLASS_MUTATION_OR_UNKNOWN)

    csm_result = _classify_controlled_skill_mutation(cmd, project_root)
    if csm_result is not None:
        if csm_result.classification == CLASS_EXACT_AUTHORIZED:
            return csm_result
        # exact_controlled_executor_shape without authorization always folds
        # into mutation_or_unknown (Issue #1289 In Scope / Out of Scope).
        return FastpathClassification(CLASS_MUTATION_OR_UNKNOWN)

    srt_result = _classify_skill_runtime(cmd, cwd, project_root, deadline)
    if srt_result is not None:
        if srt_result.classification == CLASS_EXACT_AUTHORIZED:
            return srt_result
        return FastpathClassification(CLASS_MUTATION_OR_UNKNOWN)

    if _is_readonly_intersection(cmd):
        return FastpathClassification(
            CLASS_READONLY_DISPLAY, display_summary=_bounded_summary(cmd)
        )

    return FastpathClassification(CLASS_MUTATION_OR_UNKNOWN)
