#!/usr/bin/env python3
"""pr_reviewer_guard.py -- agent-scoped hook for the `pr-reviewer` SubAgent.

Issue #1881: repo-local agent-scoped canonical mutation guardrail.

This hook is wired **only** from `.claude/agents/pr-reviewer.md` frontmatter
(`hooks.PreToolUse`), not from the project-level `.claude/settings.json`.
Command classification (which Bash invocations count as "git commit",
"gh pr review", etc.) is delegated entirely to Claude Code's native
permission-rule matcher syntax (`Bash(git commit *)` and friends) via the
frontmatter `matcher`/`if` fields -- this script does **not** implement its
own shell/command parser. By the time this script runs, Claude Code has
already decided that the current PreToolUse event matches one of the
canonical mutation command families; this script's only remaining job is to
confirm `agent_type == "pr-reviewer"` and then deny.

Subcommands
-----------
deny
    PreToolUse deny handler. Reads the PreToolUse hook JSON payload from
    stdin. Denies (exit 2, fixed reason on stderr) only when
    ``hook_event_name == "PreToolUse"`` and the payload's ``agent_type``
    (or nested ``agent``/``persona`` identity field -- see
    ``_extract_agent_type``) equals ``"pr-reviewer"``. In every other case
    (wrong event, wrong/missing agent_type, malformed JSON) this exits 0
    silently -- the frontmatter-level `matcher`/`if` scoping is what limits
    invocation to `pr-reviewer` mutation attempts in the first place; this
    is a defense-in-depth re-check, not the primary authorization boundary.

observe-identity
    Optional runtime-probe observability channel. Only emits output when
    ``LOOP_PR_REVIEWER_RUNTIME_PROBE=1`` is set in the environment (normal
    review sessions never set this and get zero output). Emits a single
    sanitized marker line to stdout containing only the ``agent_type``
    value -- never session id, transcript path, HOME, or command text.

observe-reference-read
    Same opt-in gate as ``observe-identity``. Emits a sanitized marker only
    when the PreToolUse/PostToolUse payload's ``tool_name`` is ``Read`` and
    ``tool_input.file_path`` matches the exact canonical reference path
    ``.claude/skills/pr-review-judge/references/allowed-paths-gate.md``.

Exit codes
----------
0   allow / no-op (default; observability subcommands with the opt-in
    env var unset also exit 0 after producing no output)
2   deny (PreToolUse block, ``deny`` subcommand only, agent_type match)
"""

from __future__ import annotations

import json
import os
import sys

RUNTIME_PROBE_ENV = "LOOP_PR_REVIEWER_RUNTIME_PROBE"

PR_REVIEWER_AGENT_TYPE = "pr-reviewer"

CANONICAL_REFERENCE_PATH = (
    ".claude/skills/pr-review-judge/references/allowed-paths-gate.md"
)

DENY_MARKER = "reviewer-deny"
IDENTITY_MARKER = "reviewer-identity-observed"
REFERENCE_READ_MARKER = "reviewer-reference-read-ok"

DENY_REASON = (
    "pr-reviewer is not authorized to perform Git/GitHub mutation "
    "commands (git commit/push/worktree, gh pr review/comment/merge, "
    "gh issue edit/comment/close). Verdict publication is the "
    "orchestrator's responsibility (Issue #1881)."
)


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

    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    file_path = None
    if isinstance(tool_input, dict):
        file_path = tool_input.get("file_path") or tool_input.get("filePath")
    if not isinstance(file_path, str):
        return 0

    if file_path.endswith(CANONICAL_REFERENCE_PATH):
        agent_type = _extract_agent_type(payload)
        print(f"{REFERENCE_READ_MARKER} agent_type={agent_type or 'unknown'}")
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
