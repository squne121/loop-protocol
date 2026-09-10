"""Issue #2564 -- real subprocess invocation of the actual Claude-native hook
adapter entrypoint (`hook_entry.py`), exactly as `.claude/settings.json`
wires it (`python3 hook_entry.py <EventName>` with the Claude Code hook JSON
on stdin and `HERDR_TAB_ID`/`HERDR_PANE_ID` env vars). This is the
"isolated ... actual hooks" layer the Issue's Runtime Verification
Applicability calls for, short of a genuine live Herdr + Native Claude
runtime (which this suite deliberately never touches -- ``herdr`` itself is
never invoked here since ``hook_entry.py``'s synchronous path never calls
it; only the separate async ``projection_flush_entry.py`` does, covered by
``test_herdr_projection.py`` with a faked ``herdr`` binary instead of the
real one)."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import task_context_config as config
import task_context_db as db
import task_context_service as service

_REPO_ROOT = pathlib.Path(config.__file__).resolve().parents[2]
_HOOK_ENTRY = _REPO_ROOT / ".claude" / "hooks" / "task_context" / "hook_entry.py"


def _run_hook(event, hook_input, *, state_root, env_extra=None, timeout=10):
    env = dict(os.environ)
    # This test suite may itself be running inside a real live Herdr Tab
    # (HERDR_TAB_ID/HERDR_PANE_ID set in the ambient environment) -- always
    # start from a clean slate and only set them when a test explicitly
    # asks to, so "no Herdr env" tests are not accidentally polluted by the
    # ambient environment they happen to run in.
    env.pop("HERDR_TAB_ID", None)
    env.pop("HERDR_PANE_ID", None)
    env["LOOP_TASK_CONTEXT_STATE_ROOT"] = str(state_root)
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(_HOOK_ENTRY), event],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return proc


def _read_binding_by_location(state_root, locator):
    conn = db.connect(config.db_path())
    try:
        return service.get_binding_by_current_location(conn, locator)
    finally:
        conn.close()


def test_given_no_herdr_env_when_session_start_invoked_then_exit_zero_no_binding_created(state_root):
    proc = _run_hook("SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root)
    assert proc.returncode == 0, proc.stderr
    assert not state_root.exists(), "AC11: non-Herdr observe-only must not create any Task Context state"


def test_given_herdr_env_when_session_start_invoked_then_binding_created_for_pane_locator(state_root):
    proc = _run_hook(
        "SessionStart",
        {"session_id": "s1", "source": "startup"},
        state_root=state_root,
        env_extra={"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"},
    )
    assert proc.returncode == 0, proc.stderr
    binding = _read_binding_by_location(state_root, "wV:p9")
    assert binding is not None
    assert binding["current_claude_session_id"] == "s1"


def test_given_explicit_target_prompt_when_user_prompt_submit_invoked_then_autobind_and_exit_zero(
    state_root,
):
    env_extra = {"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"}
    start = _run_hook(
        "SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=env_extra
    )
    assert start.returncode == 0, start.stderr

    prompt = _run_hook(
        "UserPromptSubmit",
        {"session_id": "s1", "prompt": "please work on owner/repo#77", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=env_extra,
    )
    assert prompt.returncode == 0, prompt.stderr

    conn = db.connect(config.db_path())
    try:
        live = service.find_live_claim(conn, "owner/repo", "issue", 77)
    finally:
        conn.close()
    assert live is not None


def test_given_different_primary_target_while_active_when_user_prompt_submit_invoked_then_exit_two_block(
    state_root,
):
    env_extra = {"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"}
    _run_hook("SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=env_extra)
    _run_hook(
        "UserPromptSubmit",
        {"session_id": "s1", "prompt": "work on owner/repo#1", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=env_extra,
    )

    blocked = _run_hook(
        "UserPromptSubmit",
        {"session_id": "s1", "prompt": "actually switch to owner/other#2", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=env_extra,
    )
    assert blocked.returncode == 2
    assert "blocked" in blocked.stderr


def test_given_slash_task_after_block_scenario_when_invoked_then_exit_zero_rebind_succeeds(state_root):
    """AC6/AC12 at the real subprocess entrypoint layer -- `/task` is never
    itself blocked by the guard it supersedes."""
    env_extra = {"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"}
    _run_hook("SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=env_extra)
    _run_hook(
        "UserPromptSubmit",
        {"session_id": "s1", "prompt": "work on owner/repo#1", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=env_extra,
    )
    rebind = _run_hook(
        "UserPromptSubmit",
        {"session_id": "s1", "prompt": "/task owner/other#2", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=env_extra,
    )
    assert rebind.returncode == 0, rebind.stderr

    conn = db.connect(config.db_path())
    try:
        live = service.find_live_claim(conn, "owner/other", "issue", 2)
    finally:
        conn.close()
    assert live is not None


def test_given_cwd_changed_invoked_when_relocated_then_exit_zero_task_unaffected(state_root):
    env_extra = {"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"}
    _run_hook("SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=env_extra)
    result = _run_hook(
        "CwdChanged",
        {"session_id": "s1", "cwd": "/some/new/worktree"},
        state_root=state_root,
        env_extra={"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9-moved"},
    )
    assert result.returncode == 0, result.stderr
    binding = _read_binding_by_location(state_root, "wV:p9-moved")
    assert binding is not None


def test_given_missing_event_arg_when_invoked_then_exit_zero_never_blocks(state_root):
    env = dict(os.environ)
    env["LOOP_TASK_CONTEXT_STATE_ROOT"] = str(state_root)
    proc = subprocess.run(
        [sys.executable, str(_HOOK_ENTRY)], input="{}", capture_output=True, text=True, env=env, timeout=10
    )
    assert proc.returncode == 0


def test_given_malformed_stdin_when_invoked_then_fail_open_exit_zero(state_root):
    env = dict(os.environ)
    env["LOOP_TASK_CONTEXT_STATE_ROOT"] = str(state_root)
    env["HERDR_TAB_ID"] = "wV:t9"
    proc = subprocess.run(
        [sys.executable, str(_HOOK_ENTRY), "UserPromptSubmit"],
        input="not valid json{{{",
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0
