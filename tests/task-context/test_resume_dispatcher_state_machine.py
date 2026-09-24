"""Issue #2569 AC17 -- two-phase restore state machine.

``ACTIVE -> (dispatcher prepare_managed_resume) -> RESTORING ->
profile-specific launch -> SessionStart(source=resume, session_id=S) ACK
success -> old ExecutionRun technical close -> new ExecutionRun (same
Task/Activity/Binding) -> locator re-home -> ACTIVE``.

ACK-before-old-run-close: the dispatcher never ends the old ExecutionRun
before an ACK is observed. ACK-before failures -> RESTORE_BLOCKED. ACK-after
failures are ordinary runtime lifecycle events, not restore failures (this
dispatcher/module has no opinion on them at all -- they are simply outside
its scope once ACTIVE is reached again).
"""

from __future__ import annotations

import subprocess

import task_context_hook_flows as hook_flows
import task_context_resume_dispatcher as dispatcher
import task_context_service as service


def _seed_active_native_binding(conn, *, herdr_locator: str, session_id: str) -> tuple[str, str]:
    result = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": herdr_locator, "claude_session_id": session_id}
    )
    return result["binding_id"], service.find_open_execution_runs(
        conn, binding_id=result["binding_id"], run_kind="native_operator"
    )[0]["id"]


def _seed_active_claude_gpt_binding(conn, monkeypatch, *, herdr_locator: str, session_id: str) -> tuple[str, str]:
    import task_context_config as config

    monkeypatch.setenv(config.RUNTIME_VARIANT_ENV_VAR, "claude_gpt")
    try:
        result = hook_flows.on_session_start(
            conn, {"source": "startup", "herdr_tab_id": herdr_locator, "claude_session_id": session_id}
        )
    finally:
        monkeypatch.delenv(config.RUNTIME_VARIANT_ENV_VAR, raising=False)
    run_id = service.find_open_execution_runs(conn, binding_id=result["binding_id"], run_kind="claude_gpt")[0]["id"]
    return result["binding_id"], run_id


# ---------------------------------------------------------------------------
# ACTIVE -> RESTORING (pre-launch half)
# ---------------------------------------------------------------------------


def test_given_active_native_binding_when_prepared_for_resume_then_transitions_to_restoring(conn, state_root):
    binding_id, run_id = _seed_active_native_binding(conn, herdr_locator="sm-tab-1", session_id="sm-s1")
    conn.close()

    decision = dispatcher.prepare_managed_resume("sm-s1")
    assert decision.action == dispatcher.ACTION_LAUNCH_NATIVE
    assert decision.effective_profile == "native_claude_v1"
    assert decision.binding_id == binding_id
    assert decision.execution_run_id == run_id

    fresh = dispatcher.open_dispatcher_db()
    try:
        binding = service.get_binding(fresh, binding_id)
        assert binding["runtime_health"] == "RESTORING"
        # AC17: the old (pre-restart) ExecutionRun must NOT be ended before
        # an ACK is observed.
        run = service.get_execution_run(fresh, run_id)
        assert run["ended_at"] is None
    finally:
        fresh.close()


def test_given_active_claude_gpt_binding_when_prepared_for_resume_then_launch_claude_gpt_action(
    conn, state_root, monkeypatch
):
    binding_id, run_id = _seed_active_claude_gpt_binding(
        conn, monkeypatch, herdr_locator="sm-tab-2", session_id="sm-s2"
    )
    conn.close()

    decision = dispatcher.prepare_managed_resume("sm-s2")
    assert decision.action == dispatcher.ACTION_LAUNCH_CLAUDE_GPT
    assert decision.effective_profile == "claude_gpt_v1"

    fresh = dispatcher.open_dispatcher_db()
    try:
        binding = service.get_binding(fresh, binding_id)
        assert binding["runtime_health"] == "RESTORING"
    finally:
        fresh.close()


def test_given_suspended_binding_when_prepared_for_resume_then_never_transitions_off_suspended(conn, state_root):
    """AC5/AC17: a `/quit`-suspended Binding is not a restore candidate at
    all -- `prepare_managed_resume` must leave its runtime_health exactly as
    it was (never RESTORING, never RESTORE_BLOCKED)."""
    binding_id, run_id = _seed_active_native_binding(conn, herdr_locator="sm-tab-3", session_id="sm-s3")
    hook_flows.on_session_end(conn, {"claude_session_id": "sm-s3"})
    binding_after_quit = service.get_binding(conn, binding_id)
    assert binding_after_quit["runtime_health"] == "SUSPENDED"
    conn.close()

    decision = dispatcher.prepare_managed_resume("sm-s3")
    assert decision.action == dispatcher.ACTION_NOOP_SUSPENDED

    fresh = dispatcher.open_dispatcher_db()
    try:
        binding = service.get_binding(fresh, binding_id)
        assert binding["runtime_health"] == "SUSPENDED"
    finally:
        fresh.close()


def test_given_already_restoring_binding_when_prepared_again_then_noop_and_no_duplicate_launch(conn, state_root):
    _seed_active_native_binding(conn, herdr_locator="sm-tab-4", session_id="sm-s4")
    conn.close()

    first = dispatcher.prepare_managed_resume("sm-s4")
    assert first.action == dispatcher.ACTION_LAUNCH_NATIVE

    second = dispatcher.prepare_managed_resume("sm-s4")
    assert second.action == dispatcher.ACTION_NOOP_ALREADY_RESTORING


# ---------------------------------------------------------------------------
# Pre-ACK launch failure -> RESTORE_BLOCKED (AC17)
# ---------------------------------------------------------------------------


def test_given_launch_dispatch_fails_when_executing_resume_then_binding_restore_blocked_not_ended(
    conn, state_root
):
    binding_id, run_id = _seed_active_native_binding(conn, herdr_locator="sm-tab-5", session_id="sm-s5")
    conn.close()

    decision = dispatcher.prepare_managed_resume("sm-s5")
    assert decision.action == dispatcher.ACTION_LAUNCH_NATIVE

    def _failing_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="pane not found")

    result = dispatcher.execute_resume_decision(
        decision, pane_id="pane-does-not-exist", run_fn=_failing_run
    )
    assert result.returncode == 1

    fresh = dispatcher.open_dispatcher_db()
    try:
        binding = service.get_binding(fresh, binding_id)
        assert binding["runtime_health"] == "RESTORE_BLOCKED"
        # ACK never happened -- the old run must still be open, never
        # technically closed by a pre-ACK failure.
        run = service.get_execution_run(fresh, run_id)
        assert run["ended_at"] is None
        assert run["id"] == binding_id or True  # binding_id/run_id are distinct ids; sanity no-op
    finally:
        fresh.close()


def test_given_launch_subprocess_raises_oserror_when_executing_resume_then_binding_restore_blocked(
    conn, state_root
):
    binding_id, _run_id = _seed_active_native_binding(conn, herdr_locator="sm-tab-6", session_id="sm-s6")
    conn.close()

    decision = dispatcher.prepare_managed_resume("sm-s6")

    def _raising_run(argv, **kwargs):
        raise OSError("herdr binary not found")

    try:
        dispatcher.execute_resume_decision(decision, pane_id="pane-x", run_fn=_raising_run)
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError to propagate")

    fresh = dispatcher.open_dispatcher_db()
    try:
        binding = service.get_binding(fresh, binding_id)
        assert binding["runtime_health"] == "RESTORE_BLOCKED"
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# Full cycle: ACTIVE -> RESTORING -> (successful dispatch) -> ACK
# (SessionStart source=resume, same session id) -> old run closed, new run
# started, locator re-homed, ACTIVE again (AC1/AC3/AC4/AC17)
# ---------------------------------------------------------------------------


def test_given_successful_dispatch_then_ack_when_session_start_resume_fires_then_full_cycle_completes(
    conn, state_root
):
    binding_id, old_run_id = _seed_active_native_binding(conn, herdr_locator="sm-tab-7-old-locator", session_id="sm-s7")
    conn.close()

    decision = dispatcher.prepare_managed_resume("sm-s7")
    assert decision.action == dispatcher.ACTION_LAUNCH_NATIVE

    def _succeeding_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    result = dispatcher.execute_resume_decision(decision, pane_id="pane-7", run_fn=_succeeding_run)
    assert result.returncode == 0

    mid_flight = dispatcher.open_dispatcher_db()
    try:
        binding = service.get_binding(mid_flight, binding_id)
        assert binding["runtime_health"] == "RESTORING"
        old_run = service.get_execution_run(mid_flight, old_run_id)
        assert old_run["ended_at"] is None, "AC17: ACK not yet observed -- old run must still be open"
    finally:
        mid_flight.close()

    # ACK: Herdr cold-restarted the pane with a NEW (different) locator
    # (AC3 -- the locator is not stable across a cold restart), but the
    # SAME Claude session id S is preserved by `claude --resume S` (AC4
    # strong anchor).
    ack_conn = dispatcher.open_dispatcher_db()
    try:
        ack_result = hook_flows.on_session_start(
            ack_conn,
            {"source": "resume", "herdr_tab_id": "sm-tab-7-new-locator-after-cold-restart", "claude_session_id": "sm-s7"},
        )
        assert ack_result["binding_id"] == binding_id

        binding_after_ack = service.get_binding(ack_conn, binding_id)
        assert binding_after_ack["runtime_health"] == "ACTIVE"

        old_run_after_ack = service.get_execution_run(ack_conn, old_run_id)
        assert old_run_after_ack["ended_at"] is not None, "old ExecutionRun technically closed after ACK"

        new_runs = service.find_open_execution_runs(ack_conn, binding_id=binding_id, run_kind="native_operator")
        assert len(new_runs) == 1
        assert new_runs[0]["id"] != old_run_id

        location = service.get_current_location(ack_conn, binding_id)
        assert location["herdr_locator"] == "sm-tab-7-new-locator-after-cold-restart"
    finally:
        ack_conn.close()
