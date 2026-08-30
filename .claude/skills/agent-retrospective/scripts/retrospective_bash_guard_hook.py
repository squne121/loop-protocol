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

PR #2425 review fix_delta P1-a (#2425#issuecomment-5466916997): Claude
Code's own ``PreToolUse`` hooks reference
(https://code.claude.com/docs/en/hooks) documents that ONLY exit code ``2``
blocks a tool call -- any other non-zero exit (e.g. an uncaught Python
exception's default ``1``) is a "non-blocking error" whose tool call
proceeds anyway. The original ``main()`` body now lives in
``_main_impl()``, and ``main()`` wraps it end-to-end: ANY exception
``_main_impl()`` does not already turn into a deliberate deny decision
(JSON decode error, missing/empty command) is caught, a diagnostic is
printed to stderr, and the process exits ``2`` -- so a hook-internal bug
can never silently fail OPEN.

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
import traceback
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


def _main_impl() -> int:
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


def main() -> int:
    """PR #2425 review fix_delta P1-a: wraps ``_main_impl`` end-to-end so
    ANY exception it does not already turn into a deliberate deny decision
    (JSON decode error, missing/empty command -- both handled inside
    ``_main_impl`` and returned as exit ``0`` + a JSON deny decision, which
    Claude Code also honors as a block) results in exit code ``2`` --
    Claude Code's PreToolUse hook contract treats ONLY exit ``2`` as
    blocking; every other non-zero exit is a non-blocking error whose tool
    call proceeds anyway, which would make a hook-internal bug (import
    error, unexpected `AttributeError`/`KeyError`, ...) fail OPEN instead of
    closed."""
    try:
        return _main_impl()
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc(file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
