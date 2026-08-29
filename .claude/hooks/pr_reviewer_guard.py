#!/usr/bin/env python3
"""pr_reviewer_guard.py -- agent-scoped hook for the `pr-reviewer` SubAgent.

Issue #1881: repo-local agent-scoped canonical mutation guardrail.

This hook is wired **only** from `.claude/agents/pr-reviewer.md` frontmatter
(`hooks.PreToolUse`), not from the project-level `.claude/settings.json`.
The frontmatter `matcher`/`if` fields (`Bash(git commit *)` and friends) are
Claude Code's own best-effort permission-rule filter. Per the official docs,
that filter fires *conservatively* on ambiguous/unparseable Bash input
(``$()``, backticks, ``$VAR`` substitution, etc.) -- i.e. it still runs the
hook rather than silently skip it when it cannot classify the command. This
means the frontmatter `if` alone cannot be trusted as a hard filter (PR
#2385 review, P1-1): this script therefore performs its own small, anchored
regex check against the *actual* ``tool_input.command`` string it receives
(``_command_is_canonical_mutation``) before denying. This is a bounded
allowlist of literal command-family prefixes, not a general shell/command
parser (no quoting, pipe, redirection, or subshell handling) -- consistent
with the Issue's own constraint against implementing a custom shell parser.
When the actual command does not match one of these anchored patterns
(including any complex/ambiguous command the frontmatter `if` conservatively
fired on, or an event with no ``tool_input.command`` at all), this script
fails open (exit 0, allow) rather than deny.

Subcommands
-----------
deny
    PreToolUse deny handler. Reads the PreToolUse hook JSON payload from
    stdin. Denies (exit 2, fixed reason on stderr) only when
    ``hook_event_name == "PreToolUse"``, the payload's ``agent_type``
    (or nested ``agent``/``persona`` identity field -- see
    ``_extract_agent_type``) equals ``"pr-reviewer"``, AND
    ``tool_input.command`` actually matches one of the anchored canonical
    mutation command patterns (``_command_is_canonical_mutation``). In
    every other case (wrong event, wrong/missing agent_type, malformed
    JSON, or a command that does not match the anchored allowlist) this
    exits 0 silently (fail-open) -- the frontmatter-level `matcher`/`if`
    scoping only narrows *when* this script runs; it is this script's own
    command inspection that is the actual authorization boundary.

observe-identity
    Optional runtime-probe observability channel. Only emits output when
    ``LOOP_PR_REVIEWER_RUNTIME_PROBE=1`` is set in the environment (normal
    review sessions never set this and get zero output). Emits a single
    sanitized marker line to stdout containing only the ``agent_type``
    value -- never session id, transcript path, HOME, or command text.

observe-reference-read
    Same opt-in gate as ``observe-identity``. Emits a sanitized marker only
    when the PreToolUse/PostToolUse payload's ``tool_name`` is ``Read``,
    ``agent_type`` is exactly ``"pr-reviewer"``, AND the resolved
    ``tool_input.file_path`` is exactly equal (not a suffix/substring match)
    to the resolved canonical reference path
    ``.claude/skills/pr-review-judge/references/allowed-paths-gate.md``.

Exit codes
----------
0   allow / no-op (default; observability subcommands with the opt-in
    env var unset also exit 0 after producing no output)
2   deny (PreToolUse block, ``deny`` subcommand only, agent_type AND
    command both match)
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

RUNTIME_PROBE_ENV = "LOOP_PR_REVIEWER_RUNTIME_PROBE"

PR_REVIEWER_AGENT_TYPE = "pr-reviewer"

CANONICAL_REFERENCE_PATH = (
    ".claude/skills/pr-review-judge/references/allowed-paths-gate.md"
)

# This script's own repo root, resolved from its own on-disk location (the
# worktree's copy, since the frontmatter invokes it via
# "${CLAUDE_PROJECT_DIR}/.claude/hooks/pr_reviewer_guard.py") -- not the
# current working directory, which a Bash tool call could change.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CANONICAL_REFERENCE_ABS_PATH = (_REPO_ROOT / CANONICAL_REFERENCE_PATH).resolve()

DENY_MARKER = "reviewer-deny"
IDENTITY_MARKER = "reviewer-identity-observed"
REFERENCE_READ_MARKER = "reviewer-reference-read-ok"

DENY_REASON = (
    "pr-reviewer is not authorized to perform Git/GitHub mutation "
    "commands (git commit/push, git worktree add/remove/move/prune/"
    "repair/lock/unlock, gh pr review/comment/merge, gh issue "
    "edit/comment/close). Verdict publication is the orchestrator's "
    "responsibility (Issue #1881)."
)

# Issue #1881 PR #2385 review (P1-1/P1-2): anchored, literal command-family
# allowlist checked against the *actual* tool_input.command. Deliberately
# narrow (no quoting/pipe/redirection/subshell handling) -- this is a
# fail-open allowlist, not a shell parser. `git worktree` is scoped to the
# mutating subcommands only (P1-2); `git worktree list` (read-only, used as
# a harmless identity-check operation during review) is intentionally
# excluded and falls through to fail-open allow.
_MUTATION_COMMAND_PATTERNS = [
    re.compile(r"^\s*git\s+(?:commit|push)(?:\s|$)"),
    re.compile(r"^\s*git\s+worktree\s+(?:add|remove|move|prune|repair|lock|unlock)(?:\s|$)"),
    re.compile(r"^\s*gh\s+pr\s+(?:review|comment|merge)(?:\s|$)"),
    re.compile(r"^\s*gh\s+issue\s+(?:edit|comment|close)(?:\s|$)"),
]


def _extract_command(payload: dict) -> str | None:
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if isinstance(command, str) and command:
        return command
    return None


def _command_is_canonical_mutation(command: str | None) -> bool:
    """Fail-open by construction: returns False (allow) for None, for
    commands that do not match any anchored pattern, and for any
    complex/ambiguous command this script cannot confidently classify."""
    if not command:
        return False
    return any(pattern.match(command) for pattern in _MUTATION_COMMAND_PATTERNS)


def _read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _extract_agent_type(payload: dict) -> str | None:
    """Best-effort extraction of the requesting agent's identity.

    Different Claude Code hook event payloads may surface the invoking
    SubAgent persona under slightly different keys depending on event type
    and version. We check the documented/likely candidates without
    fabricating a value when none are present.
    """
    for key in ("agent_type", "agentType"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value

    nested = payload.get("agent")
    if isinstance(nested, dict):
        for key in ("type", "name", "agent_type"):
            value = nested.get(key)
            if isinstance(value, str) and value:
                return value
    elif isinstance(nested, str) and nested:
        return nested

    return None


def _extract_hook_event_name(payload: dict) -> str | None:
    for key in ("hook_event_name", "hookEventName"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _probe_enabled() -> bool:
    return os.environ.get(RUNTIME_PROBE_ENV) == "1"


def cmd_deny() -> int:
    payload = _read_stdin_json()
    hook_event_name = _extract_hook_event_name(payload)
    agent_type = _extract_agent_type(payload)

    if hook_event_name != "PreToolUse":
        return 0
    if agent_type != PR_REVIEWER_AGENT_TYPE:
        return 0

    command = _extract_command(payload)
    if not _command_is_canonical_mutation(command):
        # Fail-open (P1-1): the frontmatter `if` conservatively fired on an
        # event we cannot confidently classify as a canonical mutation
        # attempt (ambiguous command, no tool_input.command at all, or a
        # command outside the anchored allowlist, e.g. `git worktree list`).
        return 0

    if _probe_enabled():
        print(f"{DENY_MARKER} agent_type={agent_type}", file=sys.stderr)
    print(DENY_REASON, file=sys.stderr)
    return 2


def cmd_observe_identity() -> int:
    if not _probe_enabled():
        return 0
    payload = _read_stdin_json()
    agent_type = _extract_agent_type(payload)
    if agent_type:
        print(f"{IDENTITY_MARKER} agent_type={agent_type}")
    return 0


def cmd_observe_reference_read() -> int:
    if not _probe_enabled():
        return 0
    payload = _read_stdin_json()
    tool_name = payload.get("tool_name") or payload.get("toolName")
    if tool_name != "Read":
        return 0

    # P1-3 (PR #2385 review): only an exact `pr-reviewer` identity counts as
    # a positive-control observation -- never a missing/other agent_type.
    agent_type = _extract_agent_type(payload)
    if agent_type != PR_REVIEWER_AGENT_TYPE:
        return 0

    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    file_path = None
    if isinstance(tool_input, dict):
        file_path = tool_input.get("file_path") or tool_input.get("filePath")
    if not isinstance(file_path, str) or not file_path:
        return 0

    # P1-3: exact resolved-path identity, not a suffix/substring match.
    try:
        resolved = Path(file_path).resolve()
    except (OSError, ValueError):
        return 0
    if resolved != CANONICAL_REFERENCE_ABS_PATH:
        return 0

    print(f"{REFERENCE_READ_MARKER} agent_type={agent_type}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        return 0
    subcommand = argv[0]
    if subcommand == "deny":
        return cmd_deny()
    if subcommand == "observe-identity":
        return cmd_observe_identity()
    if subcommand == "observe-reference-read":
        return cmd_observe_reference_read()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
