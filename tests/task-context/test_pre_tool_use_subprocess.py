"""Issue #2566 -- real subprocess invocation of `hook_entry.py PreToolUse`,
exactly as `.claude/settings.json` wires it. Covers the argument-aware
hot-path early exit and the actual Claude Code
`hookSpecificOutput.permissionDecision` JSON output contract."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import task_context_config as config
import task_context_hook_flows as hook_flows

_REPO_ROOT = pathlib.Path(config.__file__).resolve().parents[2]
_HOOK_ENTRY = _REPO_ROOT / ".claude" / "hooks" / "task_context" / "hook_entry.py"

_HERDR_ENV = {"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"}


def _run_hook(event, hook_input, *, state_root, env_extra=None, timeout=10):
    env = dict(os.environ)
    env.pop("HERDR_TAB_ID", None)
    env.pop("HERDR_PANE_ID", None)
    env["LOOP_TASK_CONTEXT_STATE_ROOT"] = str(state_root)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(_HOOK_ENTRY), event],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def test_given_non_herdr_bash_command_when_pre_tool_use_invoked_then_exit_zero_no_stdout(state_root):
    """Hot-path early exit: an ordinary (non-`herdr`) Bash command never
    reaches `ctl_client.call_hook` at all -- no state-root/DB is created,
    and no hookSpecificOutput JSON is printed."""
    proc = _run_hook(
        "PreToolUse",
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "git status"}},
        state_root=state_root,
        env_extra=_HERDR_ENV,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""
    assert not state_root.exists(), "non-herdr Bash must never materialize Task Context state"


def test_given_unguarded_herdr_subcommand_when_pre_tool_use_invoked_then_exit_zero_no_stdout(state_root):
    proc = _run_hook(
        "PreToolUse",
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "herdr session stop x"}},
        state_root=state_root,
        env_extra=_HERDR_ENV,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_given_send_message_to_unknown_session_when_pre_tool_use_invoked_then_ask_output(state_root):
    _run_hook(
        "SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=_HERDR_ENV
    )
    _run_hook(
        "UserPromptSubmit",
        {"session_id": "s1", "prompt": "work on owner/repo#1", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=_HERDR_ENV,
    )

    proc = _run_hook(
        "PreToolUse",
        {
            "session_id": "s1",
            "tool_name": "SendMessage",
            "tool_input": {"to": "unknown-peer", "message": "do not leak this text"},
        },
        state_root=state_root,
        env_extra=_HERDR_ENV,
    )
    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)
    assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "do not leak this text" not in proc.stdout
    assert "unknown-peer" not in proc.stdout


def test_given_send_message_no_herdr_env_when_pre_tool_use_invoked_then_observe_only_no_ask(state_root):
    """AC11-equivalent: non-Herdr canonical interactive Claude is
    observe-only -- the guard never asks/denies outside a Herdr Tab."""
    proc = _run_hook(
        "PreToolUse",
        {"session_id": "s1", "tool_name": "SendMessage", "tool_input": {"to": "unknown-peer"}},
        state_root=state_root,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_given_herdr_content_read_cross_task_when_pre_tool_use_invoked_then_ask_output(state_root):
    _run_hook(
        "SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=_HERDR_ENV
    )
    _run_hook(
        "UserPromptSubmit",
        {"session_id": "s1", "prompt": "work on owner/repo#1", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=_HERDR_ENV,
    )
    other_env = dict(_HERDR_ENV)
    other_env.update({"HERDR_TAB_ID": "wV:t8", "HERDR_PANE_ID": "wV:p8"})
    _run_hook(
        "SessionStart", {"session_id": "s2", "source": "startup"}, state_root=state_root, env_extra=other_env
    )
    _run_hook(
        "UserPromptSubmit",
        {"session_id": "s2", "prompt": "work on owner/repo#2", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=other_env,
    )

    proc = _run_hook(
        "PreToolUse",
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "herdr pane read wV:p8 --lines 20"},
        },
        state_root=state_root,
        env_extra=_HERDR_ENV,
    )
    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_given_herdr_discovery_command_when_pre_tool_use_invoked_then_exit_zero_no_stdout(state_root):
    _run_hook(
        "SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=_HERDR_ENV
    )
    proc = _run_hook(
        "PreToolUse",
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "herdr pane list"}},
        state_root=state_root,
        env_extra=_HERDR_ENV,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_given_list_agents_tool_when_pre_tool_use_invoked_then_never_blocked():
    """Issue #2566 In Scope: `ListAgents` is never blocked on Task Context
    grounds -- this guard does not even classify it (matcher scoping means
    it would never reach this adapter in real settings.json wiring, but the
    adapter itself is defensively a no-op for it too)."""
    # Pure function call (no DB) is enough here: `tool_name` isn't in the
    # guard's recognized set, so it returns the observability-only default
    # before ever touching the connection argument.
    result = hook_flows.on_pre_tool_use(None, {"tool_name": "ListAgents"})
    assert result["decision"] == "pass"
