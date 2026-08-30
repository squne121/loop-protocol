#!/usr/bin/env python3
"""PreToolUse hook enforcing the agent-retrospective read-only investigation
Bash profile (Issue #2419).

Registered via a run-scoped ``--settings`` file (see
``run_retrospective.write_bash_guard_settings_file``) into every delegated
observer/evaluator ``claude -p --agent <name>`` subprocess. Claude Code
invokes this script once per attempted ``Bash`` tool use, before the command
executes, and reads its stdout JSON to decide whether to allow or deny it.

Only ``Bash`` tool_use events are inspected -- every other tool is passed
through unmodified (``--disallowedTools`` already covers
Write/Edit/MultiEdit/NotebookEdit/Agent/Skill at the CLI-argv level; see
``build_agent_invocation_argv``).

This script is a thin CLI wrapper: all decision logic lives in
``run_retrospective.DelegatedAgentPermissionPolicy.check_bash`` /
``run_retrospective.build_bash_guard_hook_decision``, so this hook and the
module's own unit tests can never drift (Issue #2419's root cause was
exactly that drift -- a policy object existed, but no real invocation path
ever called it).

Agent frontmatter's own ``hooks:`` field intentionally is NOT used for this
purpose: Claude Code only honors a project subagent's frontmatter hooks
after a workspace-trust dialog is accepted for that agent file's folder, and
a headless ``-p`` session never presents that dialog. A ``--settings``
file's hooks fire unconditionally in ``-p`` invocations, which is why this
script is wired in that way instead.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_retrospective import (  # noqa: E402
    DelegatedAgentPermissionPolicy,
    build_bash_guard_hook_decision,
)


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"agent-retrospective Bash guard: {reason}",
        }
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        json.dump(_deny("hook_input_not_json"), sys.stdout)
        return 0

    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        # Not a Bash tool_use event -- nothing for this hook to enforce.
        # Emitting no output leaves Claude Code's normal permission flow
        # for this tool call untouched.
        return 0

    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        json.dump(_deny("bash_command_missing_or_empty"), sys.stdout)
        return 0

    # `run_id` is irrelevant to `check_bash` itself (only `check_resume`
    # consults it); a fixed placeholder is fine for this per-tool-call,
    # stateless hook invocation.
    policy = DelegatedAgentPermissionPolicy(run_id="bash-guard-hook", read_only_investigation_enabled=True)
    decision = build_bash_guard_hook_decision(command, policy=policy)
    json.dump(decision, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
