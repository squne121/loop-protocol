"""Issue #2790 AC9: `/task` `decision: block` command-failure messages must
match their actual cause -- `hook_entry.py`'s `no_binding_for_session`
message previously told the user to "provide an explicit target" even when
they had already supplied a fully-qualified one (Issue #2790 Background:
`/task 2782` / `/task squne121/loop-protocol#2782` both hit exactly this,
misleadingly). This suite unit-tests the per-`reason_code` message mapping
directly, and end-to-end (real subprocess) confirms the exact previously-
misleading scenario now self-heals via bootstrap instead of ever reaching
that message."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import hook_entry
import task_context_config as config
import task_context_db as db
import task_context_service as service

_REPO_ROOT = pathlib.Path(config.__file__).resolve().parents[2]
_HOOK_ENTRY = _REPO_ROOT / ".claude" / "hooks" / "task_context" / "hook_entry.py"


def test_task_error_message_matches_cause():
    """Each distinct `/task` command-failure reason_code gets its own
    accurate message -- never the same generic "provide an explicit
    target" text regardless of cause."""
    missing_target_message = hook_entry._task_command_failure_message("slash_task_missing_target")
    no_binding_message = hook_entry._task_command_failure_message("no_binding_for_session")
    missing_session_message = hook_entry._task_command_failure_message("missing_session_id")

    assert missing_target_message != no_binding_message
    assert missing_target_message != missing_session_message
    assert no_binding_message != missing_session_message

    # `no_binding_for_session` must describe the actual cause (no Binding),
    # not merely repeat the "target missing" instruction verbatim as if the
    # user's target input itself were the problem.
    assert "Binding" in no_binding_message
    assert "bootstrap" in no_binding_message

    # `missing_session_id` must describe the actual (session identity)
    # cause, not the target -- re-entering a target can never fix it.
    assert "Claude session id" in missing_session_message

    # An unrecognized reason_code still degrades to a safe, generic
    # instruction (defensive default -- never raises, never silently empty).
    fallback_message = hook_entry._task_command_failure_message("some_future_reason_code")
    assert "some_future_reason_code" in fallback_message
    assert "Provide an explicit target" in fallback_message


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


def test_given_fully_qualified_target_and_no_prior_session_start_when_task_expanded_then_bootstrap_succeeds(
    state_root,
):
    """Issue #2790 Background regression: an explicit, fully-qualified
    `/task owner/repo#N` on a Herdr-tracked session that never had a
    SessionStart-created Binding (the exact fork-shaped dead-end reported)
    must now succeed via bootstrap -- exit 0, no misleading stderr message
    at all -- instead of the pre-#2790 `no_binding_for_session` / "Provide
    an explicit target" exit 2."""
    env_extra = {"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"}
    result = _run_hook(
        "UserPromptExpansion",
        {
            "session_id": "s-never-started",
            "command_name": "task",
            "command_args": "owner/repo#2782",
            "cwd": str(state_root),
        },
        state_root=state_root,
        env_extra=env_extra,
    )
    assert result.returncode == 0, result.stderr
    assert "/task failed" not in result.stderr

    conn = db.connect(config.db_path())
    try:
        binding = service.get_binding_by_current_session(conn, "s-never-started")
        assert binding is not None
        live = service.find_live_claim(conn, "owner/repo", "issue", 2782)
        assert live is not None
        assert live["task_id"] is not None
    finally:
        conn.close()


def test_given_no_target_and_no_binding_when_task_expanded_then_message_describes_missing_binding_and_target(
    state_root,
):
    """The one remaining case that still reaches `no_binding_for_session`
    (no current Binding AND no target at all) must show a message that
    accurately says a target is needed *and* that it will bootstrap a new
    Binding -- not a bare "re-enter your target" non-sequitur."""
    env_extra = {"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"}
    result = _run_hook(
        "UserPromptExpansion",
        {"session_id": "s-no-target-no-binding", "command_name": "task", "command_args": "", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=env_extra,
    )
    assert result.returncode == 2
    assert "no_binding_for_session" in result.stderr
    assert "bootstrap" in result.stderr
