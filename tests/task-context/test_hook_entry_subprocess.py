"""Issue #2564 (advisory-only ACTIVE different-primary guard + `/task`
authority moved to `UserPromptExpansion`: Issue #2625) -- real subprocess
invocation of the actual Claude-native hook
adapter entrypoint (`hook_entry.py`), exactly as `.claude/settings.json`
wires it (`python3 hook_entry.py <EventName>` with the Claude Code hook JSON
on stdin and `HERDR_TAB_ID`/`HERDR_PANE_ID` env vars). This is the
"isolated ... actual hooks" layer the Issue's Runtime Verification
Applicability calls for, short of a genuine live Herdr + Native Claude
runtime (which this suite deliberately never touches -- the real ``herdr``
binary is never on ``PATH`` in this suite, so the detached projection-flush
child `hook_entry.py` launches after a DB-mutating event (PR #2615
fix_delta 2) fails closed at `resolve_current_tab_id` and performs no
Herdr I/O; ``test_herdr_projection.py`` covers the actual Herdr CLI
invocation shape with a faked ``herdr`` binary instead)."""

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


def _run_hook(event, hook_input, *, state_root, env_extra=None, timeout=10, add_provenance=True):
    if event == "UserPromptSubmit" and add_provenance:
        # Issue #2827: a real Claude Code `UserPromptSubmit` stdin carries
        # `hook_event_name` and a non-empty `prompt_id`; the adapter derives
        # `input_provenance` from them (fail-closed when absent). Seed stdin
        # in these tests therefore mirrors the real key set.
        hook_input = {"hook_event_name": "UserPromptSubmit", "prompt_id": "prompt-seed-1", **hook_input}
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


def test_given_different_primary_target_while_active_when_user_prompt_submit_invoked_then_exit_zero_advisory_only(
    state_root,
):
    """Issue #2625 AC1/AC3 (supersedes Issue #2564 AC4's hard block) at the
    real subprocess entrypoint layer: a different-primary-target mismatch on
    ordinary UserPromptSubmit is advisory-only -- exit 0, Claude prompt
    processing continues, current Task/Binding untouched (no silent
    rebind)."""
    env_extra = {"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"}
    _run_hook("SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=env_extra)
    first = _run_hook(
        "UserPromptSubmit",
        {"session_id": "s1", "prompt": "work on owner/repo#1", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=env_extra,
    )
    assert first.returncode == 0, first.stderr

    mismatch = _run_hook(
        "UserPromptSubmit",
        {"session_id": "s1", "prompt": "owner/other#2 looks related", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=env_extra,
    )
    assert mismatch.returncode == 0, mismatch.stderr
    assert "advisory" in mismatch.stderr

    conn = db.connect(config.db_path())
    try:
        # No silent rebind / claim for the mismatched target B.
        assert service.find_live_claim(conn, "owner/other", "issue", 2) is None
    finally:
        conn.close()


def test_given_slash_task_after_advisory_scenario_when_expanded_then_exit_zero_rebind_succeeds(state_root):
    """AC6 at the real subprocess entrypoint layer: `/task` state-changing
    authority lives exclusively in the `UserPromptExpansion` command
    lifecycle (`command_name == "task"`) -- it always supersedes whatever
    Task/Activity is currently ACTIVE, exactly as before, just via a
    different Claude Code hook event than the (now-advisory-only) ordinary
    UserPromptSubmit prompt."""
    env_extra = {"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"}
    _run_hook("SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=env_extra)
    _run_hook(
        "UserPromptSubmit",
        {"session_id": "s1", "prompt": "work on owner/repo#1", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=env_extra,
    )
    rebind = _run_hook(
        "UserPromptExpansion",
        {"session_id": "s1", "command_name": "task", "command_args": "owner/other#2", "cwd": str(state_root)},
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


def test_given_cwd_changed_in_real_git_worktree_when_invoked_then_worktree_and_branch_observed(
    state_root, tmp_path
):
    """fix_delta 7: `cwd` inside a real git checkout resolves `worktree` and
    `branch` on the RuntimeLocation observation, display-only (AC7)."""
    env_extra = {"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"}
    _run_hook("SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=env_extra)
    result = _run_hook(
        "CwdChanged",
        {"session_id": "s1", "cwd": str(_REPO_ROOT)},
        state_root=state_root,
        env_extra=env_extra,
    )
    assert result.returncode == 0, result.stderr
    binding = _read_binding_by_location(state_root, "wV:p9")
    assert binding is not None

    conn = db.connect(config.db_path())
    try:
        location = service.get_current_location(conn, binding["id"])
    finally:
        conn.close()
    assert location is not None
    assert location["cwd"] == str(_REPO_ROOT)
    assert location["worktree"], "expected a resolved git worktree toplevel"
    assert location["branch"], "expected a resolved git branch"


def test_given_slash_task_when_ctl_transport_fails_then_exit_two_never_fail_open(state_root):
    """AC6 (carried over from PR #2615 fix_delta 4, now scoped to
    UserPromptExpansion): `/task` is an explicit state-changing command -- a
    CLI transport error / invalid result envelope must surface as exit 2,
    never a silent fail-open pass (unlike every other command_name)."""
    broken_state_root = "relative/not/absolute/path"
    env = dict(os.environ)
    env.pop("HERDR_TAB_ID", None)
    env.pop("HERDR_PANE_ID", None)
    env["LOOP_TASK_CONTEXT_STATE_ROOT"] = broken_state_root
    env["HERDR_TAB_ID"] = "wV:t9"
    env["HERDR_PANE_ID"] = "wV:p9"
    proc = subprocess.run(
        [sys.executable, str(_HOOK_ENTRY), "UserPromptExpansion"],
        input=json.dumps(
            {"session_id": "s1", "command_name": "task", "command_args": "owner/repo#5", "cwd": str(state_root)}
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 2
    assert "/task failed" in proc.stderr


def test_given_normal_prompt_when_ctl_transport_fails_then_exit_zero_fail_open(state_root):
    """The fix_delta 4 fail-closed carve-out is specific to SLASH_TASK --
    every other prompt kind keeps the pre-existing fail-open default."""
    broken_state_root = "relative/not/absolute/path"
    env = dict(os.environ)
    env.pop("HERDR_TAB_ID", None)
    env.pop("HERDR_PANE_ID", None)
    env["LOOP_TASK_CONTEXT_STATE_ROOT"] = broken_state_root
    env["HERDR_TAB_ID"] = "wV:t9"
    env["HERDR_PANE_ID"] = "wV:p9"
    proc = subprocess.run(
        [sys.executable, str(_HOOK_ENTRY), "UserPromptSubmit"],
        input=json.dumps({"session_id": "s1", "prompt": "work on owner/repo#5", "cwd": str(state_root)}),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr


def test_given_slash_task_when_ctl_transport_fails_then_stderr_conveys_uncertainty_not_not_applied(state_root):
    """Regression for OWNER PR review P1 supplement: a CLI transport
    failure / timeout / malformed envelope for `/task` means the rebind's
    outcome could not be *confirmed* by this adapter -- the underlying
    service-side write is atomic and may have already committed inside the
    `task-contextctl` child process regardless of whether this adapter
    successfully read the response back. The message must never claim the
    rebind was "not applied" (that would be a false negative when it
    actually succeeded), and must not push the user toward an unconditional
    retry (which could create a duplicate ad-hoc Task if the first call
    actually committed). Exit code stays 2 (still a genuine, surfaced
    command failure)."""
    broken_state_root = "relative/not/absolute/path"
    env = dict(os.environ)
    env.pop("HERDR_TAB_ID", None)
    env.pop("HERDR_PANE_ID", None)
    env["LOOP_TASK_CONTEXT_STATE_ROOT"] = broken_state_root
    env["HERDR_TAB_ID"] = "wV:t9"
    env["HERDR_PANE_ID"] = "wV:p9"
    proc = subprocess.run(
        [sys.executable, str(_HOOK_ENTRY), "UserPromptExpansion"],
        input=json.dumps(
            {"session_id": "s1", "command_name": "task", "command_args": "owner/repo#5", "cwd": str(state_root)}
        ),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 2
    assert "/task failed" in proc.stderr
    assert "NOT applied" not in proc.stderr, "transport失敗時に'NOT applied'と断定してはならない(実際は不明)"
    assert "Retry `/task" not in proc.stderr, "無条件retryを促す文言があってはならない"
    assert "unknown" in proc.stderr or "unconfirmed" in proc.stderr or "could not be confirmed" in proc.stderr


def test_given_slash_task_missing_target_when_expanded_then_exit_two_block(state_root):
    """AC6 (carried over from PR #2615 fix_delta 4, now scoped to
    UserPromptExpansion): an explicit `/task` with no resolvable
    target/title is a validation failure the adapter surfaces as
    decision:block, not a silent no-op."""
    env_extra = {"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"}
    _run_hook("SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=env_extra)
    result = _run_hook(
        "UserPromptExpansion",
        {"session_id": "s1", "command_name": "task", "command_args": "", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=env_extra,
    )
    assert result.returncode == 2
    assert "/task failed" in result.stderr


def test_given_other_command_name_when_expanded_then_exit_zero_never_touches_task_context(state_root):
    """AC6: `command_name != "task"` is not this hook's concern at all --
    always a silent, non-mutating exit 0, regardless of Task Context state."""
    env_extra = {"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"}
    _run_hook("SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=env_extra)
    result = _run_hook(
        "UserPromptExpansion",
        {"session_id": "s1", "command_name": "some-other-skill", "command_args": "anything", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=env_extra,
    )
    assert result.returncode == 0, result.stderr


def test_given_subagent_start_and_stop_when_agent_id_supplied_then_forwarded_and_correlated(state_root):
    """fix_delta 5: the official Claude Code `agent_id` hook payload field is
    forwarded end-to-end so SubagentStop ends the exact run that started."""
    env_extra = {"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"}
    _run_hook("SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=env_extra)
    _run_hook(
        "UserPromptSubmit",
        {"session_id": "s1", "prompt": "work on owner/repo#1", "cwd": str(state_root)},
        state_root=state_root,
        env_extra=env_extra,
    )

    start_a = _run_hook(
        "SubagentStart", {"session_id": "s1", "agent_id": "agent-a"}, state_root=state_root, env_extra=env_extra
    )
    assert start_a.returncode == 0, start_a.stderr
    start_b = _run_hook(
        "SubagentStart", {"session_id": "s1", "agent_id": "agent-b"}, state_root=state_root, env_extra=env_extra
    )
    assert start_b.returncode == 0, start_b.stderr

    conn = db.connect(config.db_path())
    try:
        open_before_stop = service.find_open_execution_runs(conn, run_kind="subagent")
    finally:
        conn.close()
    assert len(open_before_stop) == 2

    stop_a = _run_hook(
        "SubagentStop", {"session_id": "s1", "agent_id": "agent-a"}, state_root=state_root, env_extra=env_extra
    )
    assert stop_a.returncode == 0, stop_a.stderr

    conn = db.connect(config.db_path())
    try:
        still_open = service.find_open_execution_runs(conn, run_kind="subagent")
    finally:
        conn.close()
    assert len(still_open) == 1
    assert still_open[0]["agent_id"] == "agent-b", "stopping agent-a must never end agent-b's run"


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


# ---------------------------------------------------------------------------
# Issue #2827: input_provenance (fail-closed, two values) and the ACTIVE-only
# projection at the real adapter / subprocess layer.
# ---------------------------------------------------------------------------

_ENV = {"HERDR_TAB_ID": "wV:t9", "HERDR_PANE_ID": "wV:p9"}


def _user_prompt_stdin(prompt="please work on owner/repo#77", **overrides):
    data = {
        "hook_event_name": "UserPromptSubmit",
        "prompt_id": "9e0c1b96-31ee-4217-bcc0-c902643e4f25",
        "session_id": "s1",
        "prompt": prompt,
        "cwd": "/unused",
    }
    data.update(overrides)
    return data


def test_provenance_requires_prompt_id_string_prompt_and_no_internal_envelope_marker(state_root):
    import hook_entry

    observed = "user_prompt_observed"
    internal = "internal_or_unknown"
    derive = hook_entry.derive_input_provenance

    assert derive(_user_prompt_stdin()) == observed
    # Every missing / mistyped required field is internal_or_unknown.
    missing_prompt_id = _user_prompt_stdin()
    del missing_prompt_id["prompt_id"]
    missing_event = _user_prompt_stdin()
    del missing_event["hook_event_name"]
    missing_prompt = _user_prompt_stdin()
    del missing_prompt["prompt"]
    for data in (
        missing_prompt_id,
        missing_event,
        missing_prompt,
        _user_prompt_stdin(prompt_id=""),
        _user_prompt_stdin(prompt_id=123),
        _user_prompt_stdin(prompt_id=None),
        _user_prompt_stdin(prompt=123),
        _user_prompt_stdin(prompt=None),
        _user_prompt_stdin(hook_event_name="UserPromptExpansion"),
        {},
        [],
        None,
        "not a dict",
    ):
        assert derive(data) == internal, data
    # Known internal envelope marker (even after leading whitespace) is internal.
    for prompt in (
        "<task-notification>\n<task-id>x</task-id> owner/repo#77 を対象に",
        "  \n\t<task-notification>done",
    ):
        assert derive(_user_prompt_stdin(prompt=prompt)) == internal
    # A marker mid-prompt is ordinary user text; prompt_id alone or a test-only flag prove nothing.
    assert derive(_user_prompt_stdin(prompt="text <task-notification> mentioned")) == observed
    assert derive({"actual_user": True, "prompt": "x", "hook_event_name": "UserPromptSubmit"}) == internal
    assert derive({"prompt_id": "abc", "prompt": "x"}) == internal

    # Subprocess: the fail-closed variants never autobind; the valid one does.
    env_extra = dict(_ENV)
    _run_hook("SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=env_extra)
    for variant in (
        {"session_id": "s1", "prompt": "please work on owner/repo#77", "cwd": "/unused"},  # nothing
        {"session_id": "s1", "prompt": "please work on owner/repo#77", "prompt_id": "only-id", "cwd": "/unused"},
        _user_prompt_stdin(prompt="<task-notification>\nowner/repo#77 を対象に作業開始"),
    ):
        proc = _run_hook("UserPromptSubmit", variant, state_root=state_root, env_extra=env_extra, add_provenance=False)
        assert proc.returncode == 0, proc.stderr
    conn = db.connect(config.db_path())
    try:
        assert service.find_live_claim(conn, "owner/repo", "issue", 77) is None
    finally:
        conn.close()
    ok = _run_hook("UserPromptSubmit", _user_prompt_stdin(), state_root=state_root, env_extra=env_extra)
    assert ok.returncode == 0, ok.stderr
    conn = db.connect(config.db_path())
    try:
        assert service.find_live_claim(conn, "owner/repo", "issue", 77) is not None
    finally:
        conn.close()


def test_input_provenance_is_forwarded_to_core(monkeypatch):
    import io

    import hook_entry

    seen = []

    def _capture(event, payload, timeout=None):
        seen.append((event, dict(payload)))
        return {"status": "ok", "data": {"decision": "pass"}}

    monkeypatch.setattr(hook_entry.ctl_client, "call_hook", _capture)
    monkeypatch.setattr(hook_entry, "_launch_detached_projection_flush", lambda *_a, **_k: None)
    for stdin, expected in (
        (_user_prompt_stdin(prompt="work on owner/repo#1"), "user_prompt_observed"),
        (_user_prompt_stdin(prompt="<task-notification>\nwork on owner/repo#1"), "internal_or_unknown"),
        ({"session_id": "s1", "prompt": "work on owner/repo#1"}, "internal_or_unknown"),
    ):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(stdin)))
        assert hook_entry.main(["hook_entry.py", "UserPromptSubmit"]) == 0
        event, payload = seen[-1]
        assert event == "UserPromptSubmit"
        assert payload["input_provenance"] == expected
        # The raw prompt never reaches core.
        assert "prompt" not in payload and "work on" not in json.dumps(payload)


_CAPTURED_PROMPT_HEAD = "<task-notification>\n"
# Bounded observations from the real Claude Code 2.1.284 capture (marker only, no prompt text).
_MATRIX_OBSERVATIONS = {
    "subagent_completion": {"capture": "captured", "prompt_head": _CAPTURED_PROMPT_HEAD, "prompt_id_present": True},
    "background_shell_completion": {
        "capture": "captured",
        "prompt_head": _CAPTURED_PROMPT_HEAD,
        "prompt_id_present": True,
    },
    "peer_teammate_message": {"capture": "not_capturable", "reason": "no peer in the isolated environment"},
    "scheduled_prompt": {"capture": "not_capturable", "reason": "no scheduler in the isolated environment"},
    "agent_sent_herdr_pane_text": {"capture": "not_capturable", "reason": "no live herdr pane"},
    "interactive_typed_prompt": {"capture": "captured", "prompt_head": "Issue #1 を対象に", "prompt_id_present": True},
}


def _adapter():
    import importlib.util
    import sys

    path = _REPO_ROOT / "scripts" / "task-context" / "verify_active_task_prompt_auto_rebind.py"
    name = "verify_active_task_prompt_auto_rebind_for_hook_entry_test"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_provenance_capture_matrix_covers_closed_class_list():
    adapter = _adapter()
    matrix = adapter.build_provenance_capture_matrix(_MATRIX_OBSERVATIONS)

    assert set(matrix["classes"]) == {
        "subagent_completion",
        "background_shell_completion",
        "peer_teammate_message",
        "scheduled_prompt",
        "agent_sent_herdr_pane_text",
        "interactive_typed_prompt",
    }
    assert len(matrix["classes"]) == 6
    kinds = {name: row["kind"] for name, row in matrix["classes"].items()}
    assert sorted(k for k in kinds.values() if k == "stop-blocking") == ["stop-blocking"] * 2
    assert sorted(k for k in kinds.values() if k == "accepted-residual") == ["accepted-residual"] * 3
    assert sorted(k for k in kinds.values() if k == "positive-control") == ["positive-control"]
    # Stop-blocking classes carry the envelope marker and derive internal_or_unknown.
    for name in ("subagent_completion", "background_shell_completion"):
        assert matrix["classes"][name]["marker_present"] is True
        assert matrix["classes"][name]["derived_provenance"] == "internal_or_unknown"
    # The positive control (typed user prompt) is user_prompt_observed.
    assert matrix["classes"]["interactive_typed_prompt"]["derived_provenance"] == "user_prompt_observed"
    # Accepted-residual results are recorded but never stop the run.
    assert matrix["status"] == "ok"

    # A class missing from the closed list is still reported (not silently dropped).
    partial = adapter.build_provenance_capture_matrix({"subagent_completion": _MATRIX_OBSERVATIONS["subagent_completion"]})
    assert set(partial["classes"]) == set(matrix["classes"])
    # Stop Condition: a stop-blocking class without a marker (looks like a user prompt).
    bad = dict(_MATRIX_OBSERVATIONS)
    bad["subagent_completion"] = {"capture": "captured", "prompt_head": "hello", "prompt_id_present": True}
    assert adapter.build_provenance_capture_matrix(bad)["status"] == "stop_condition"
    # ... or one that could not be captured at all.
    bad["subagent_completion"] = {"capture": "not_capturable"}
    assert adapter.build_provenance_capture_matrix(bad)["status"] == "stop_condition"


def test_unmutated_advisory_wording_matches_new_contract(state_root):
    env_extra = dict(_ENV)
    _run_hook("SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=env_extra)
    _run_hook("UserPromptSubmit", _user_prompt_stdin("work on owner/repo#1"), state_root=state_root, env_extra=env_extra)
    # Not an eligible primary (no target phrase): the Task is not switched and the advisory is printed.
    proc = _run_hook(
        "UserPromptSubmit", _user_prompt_stdin("owner/other#2 looks related"), state_root=state_root, env_extra=env_extra
    )
    assert proc.returncode == 0, proc.stderr
    assert "advisory" in proc.stderr
    assert "escape hatch" in proc.stderr and "/task <target>" in proc.stderr
    for old in ("no longer blocks ordinary prompts", "use `/task <target>` to explicitly switch"):
        assert old not in proc.stderr
    # `/task` is never described as a required first step.
    assert "must" not in proc.stderr.lower() and "先に" not in proc.stderr

    import hook_entry

    for reason in hook_entry._UNMUTATED_ADVISORY_REASON_CODES:
        message = hook_entry._unmutated_advisory_message(reason)
        assert reason in message and "escape hatch" in message
        assert "no longer blocks" not in message


def test_active_rebind_projection_keys_present_only_when_eligible(monkeypatch):
    import hook_entry

    monkeypatch.setattr(hook_entry, "_current_repo", lambda *_a, **_k: "owner/repo")
    eligible_keys = {
        "active_rebind_primary_eligible",
        "active_rebind_target_repo",
        "active_rebind_target_ref_kind",
        "active_rebind_target_ref_number",
        "active_rebind_ref_form",
    }

    def _fields(prompt):
        payload = {}
        hook_entry._apply_user_prompt_submit_fields(payload, _user_prompt_stdin(prompt))
        return payload

    eligible = _fields("Issue #12 を対象にレビューして")
    assert eligible_keys <= set(eligible)
    assert eligible["active_rebind_primary_eligible"] is True
    assert eligible["active_rebind_target_repo"] == "owner/repo"
    assert eligible["active_rebind_target_ref_kind"] == "issue"
    assert eligible["active_rebind_target_ref_number"] == 12
    assert eligible["active_rebind_ref_form"] == "prefixed"
    # ... identical to the legacy target the UNBOUND path uses.
    assert (eligible["target_repo"], eligible["target_ref_kind"], eligible["target_ref_number"]) == (
        "owner/repo",
        "issue",
        12,
    )

    for prompt in ("owner/other#2 looks related", "続けてください", "see also #2", "/task #2", "Issue #1 と Issue #2 を実装"):
        fields = _fields(prompt)
        assert fields["active_rebind_primary_eligible"] is False, prompt
        assert not (eligible_keys - {"active_rebind_primary_eligible"}) & set(fields), prompt


def test_given_eligible_primary_prompt_while_active_when_user_prompt_submit_invoked_then_rebind_without_slash_task(
    state_root,
):
    """Real subprocess end to end: ACTIVE A -> ordinary prompt B -> ACTIVE B, zero `/task`."""
    env_extra = dict(_ENV)
    _run_hook("SessionStart", {"session_id": "s1", "source": "startup"}, state_root=state_root, env_extra=env_extra)
    _run_hook("UserPromptSubmit", _user_prompt_stdin("work on owner/repo#1"), state_root=state_root, env_extra=env_extra)
    proc = _run_hook(
        "UserPromptSubmit",
        _user_prompt_stdin("switch to owner/other#2"),
        state_root=state_root,
        env_extra=env_extra,
    )
    assert proc.returncode == 0, proc.stderr
    assert "advisory" not in proc.stderr

    conn = db.connect(config.db_path())
    try:
        claim_b = service.find_live_claim(conn, "owner/other", "issue", 2)
        assert claim_b is not None
        binding = _read_binding_by_location(state_root, "wV:p9")
        task_id, _, _ = service.get_current_task_activity_for_binding(conn, binding["id"])
        assert task_id == claim_b["task_id"]
    finally:
        conn.close()
