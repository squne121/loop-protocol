"""PR #2731 review fix_delta -- deterministic regression tests for:

- P1-2: ACK/dispatch success are conflated; no ACK timeout; a late/stale
  dispatch failure signal must never clobber an already-successful (ACTIVE)
  restore.
- P2-1: `prepare_managed_resume()`'s classify+transition race
  (TOCTOU) -- concurrent callers for the same session must yield at most
  one launchable result.
- P2-2: `--dry-run` must be genuinely read-only (never write
  ACTIVE -> RESTORING).
"""

from __future__ import annotations

import subprocess
import threading

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


# ---------------------------------------------------------------------------
# P1-2a: ACK timeout -> RESTORE_BLOCKED, no RESTORING left behind
# ---------------------------------------------------------------------------


def test_given_dispatch_succeeds_but_no_ack_arrives_when_awaiting_then_restore_blocked_within_deadline(
    conn, state_root
):
    binding_id, run_id = _seed_active_native_binding(conn, herdr_locator="ack-tab-1", session_id="ack-s1")
    conn.close()

    decision = dispatcher.prepare_managed_resume("ack-s1")
    assert decision.action == dispatcher.ACTION_LAUNCH_NATIVE

    def _succeeding_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    proc = dispatcher.execute_resume_decision(decision, pane_id="pane-ack-1", run_fn=_succeeding_run)
    assert proc.returncode == 0

    # No SessionStart(source=resume) ever arrives -- await_ack must resolve
    # within its bounded deadline instead of hanging/leaving RESTORING
    # forever.
    status = dispatcher.await_ack(
        decision.binding_id, decision.execution_run_id, timeout_seconds=0.05, poll_interval_seconds=0.01
    )
    assert status == "restore_blocked"

    fresh = dispatcher.open_dispatcher_db()
    try:
        binding = service.get_binding(fresh, binding_id)
        assert binding["runtime_health"] == "RESTORE_BLOCKED"
        run = service.get_execution_run(fresh, run_id)
        assert run["ended_at"] is None, "the old run was never touched by a timeout that never ACK'd"
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# P1-2b: ACK success racing a late dispatch failure -- ACTIVE must not be
# overwritten (the guarded mark_restore_blocked_if_pending).
# ---------------------------------------------------------------------------


def test_given_ack_already_succeeded_when_late_failure_signal_arrives_then_active_not_overwritten(
    conn, state_root
):
    binding_id, old_run_id = _seed_active_native_binding(conn, herdr_locator="ack-tab-2-old", session_id="ack-s2")
    conn.close()

    decision = dispatcher.prepare_managed_resume("ack-s2")
    assert decision.action == dispatcher.ACTION_LAUNCH_NATIVE

    # ACK: SessionStart(source=resume, session_id=S) arrives and succeeds
    # BEFORE the late failure signal below (this is exactly what
    # `task_context_hook_flows.on_session_start`'s session-id-first restore
    # path does out-of-process, driven by the resumed Claude session).
    ack_conn = dispatcher.open_dispatcher_db()
    try:
        ack_result = hook_flows.on_session_start(
            ack_conn, {"source": "resume", "herdr_tab_id": "ack-tab-2-new", "claude_session_id": "ack-s2"}
        )
        assert ack_result["binding_id"] == binding_id
        assert service.get_binding(ack_conn, binding_id)["runtime_health"] == "ACTIVE"
    finally:
        ack_conn.close()

    # A LATE dispatch-failure signal for the SAME (now-stale) pre-restore
    # execution_run_id arrives after the ACK already completed -- this must
    # be recognized as stale (the old run is already technically closed)
    # and must NOT downgrade the now-ACTIVE Binding.
    applied = dispatcher.mark_restore_blocked_if_pending(decision.binding_id, decision.execution_run_id)
    assert applied is False

    fresh = dispatcher.open_dispatcher_db()
    try:
        binding = service.get_binding(fresh, binding_id)
        assert binding["runtime_health"] == "ACTIVE", "a late/stale pre-ACK failure signal must never downgrade ACTIVE"
    finally:
        fresh.close()


def test_given_ack_already_succeeded_when_await_ack_polls_after_the_fact_then_reports_restored(
    conn, state_root
):
    """await_ack itself, called AFTER the ACK already landed, must report
    "restored" immediately rather than (incorrectly) timing out."""
    binding_id, _old_run_id = _seed_active_native_binding(
        conn, herdr_locator="ack-tab-3-old", session_id="ack-s3"
    )
    conn.close()

    decision = dispatcher.prepare_managed_resume("ack-s3")

    ack_conn = dispatcher.open_dispatcher_db()
    try:
        hook_flows.on_session_start(
            ack_conn, {"source": "resume", "herdr_tab_id": "ack-tab-3-new", "claude_session_id": "ack-s3"}
        )
    finally:
        ack_conn.close()

    status = dispatcher.await_ack(
        decision.binding_id, decision.execution_run_id, timeout_seconds=1.0, poll_interval_seconds=0.01
    )
    assert status == "restored"


# ---------------------------------------------------------------------------
# P1-2c: CLI exit code must be nonzero when the Herdr dispatch itself fails.
# ---------------------------------------------------------------------------


def test_given_herdr_dispatch_fails_when_running_cli_main_then_exit_code_nonzero(conn, state_root, monkeypatch, capsys):
    _seed_active_native_binding(conn, herdr_locator="ack-cli-tab-1", session_id="ack-cli-s1")
    conn.close()

    def _failing_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=7, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", _failing_run)

    exit_code = dispatcher.main(["--session-id", "ack-cli-s1", "--pane-id", "pane-cli-1"])
    assert exit_code != 0
    out = capsys.readouterr().out
    assert '"dispatch_status": "dispatch_failed"' in out


def test_given_successful_dispatch_and_no_await_ack_flag_when_running_cli_main_then_exit_zero(
    conn, state_root, monkeypatch, capsys
):
    _seed_active_native_binding(conn, herdr_locator="ack-cli-tab-2", session_id="ack-cli-s2")
    conn.close()

    def _succeeding_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _succeeding_run)

    exit_code = dispatcher.main(
        ["--session-id", "ack-cli-s2", "--pane-id", "pane-cli-2", "--no-await-ack"]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert '"dispatch_status": "dispatched_waiting_ack"' in out


# ---------------------------------------------------------------------------
# P2-1: duplicate prepare -> at most 1 launch authority (concurrency test)
# ---------------------------------------------------------------------------


def test_given_concurrent_prepare_managed_resume_calls_when_racing_then_at_most_one_launchable(
    conn, state_root
):
    _seed_active_native_binding(conn, herdr_locator="race-tab-1", session_id="race-s1")
    conn.close()

    results: list[dispatcher.ResumeDecision] = []
    lock = threading.Lock()

    def _call():
        decision = dispatcher.prepare_managed_resume("race-s1")
        with lock:
            results.append(decision)

    threads = [threading.Thread(target=_call) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    launchable = [r for r in results if r.action in dispatcher._LAUNCHABLE_ACTIONS]
    assert len(launchable) == 1, f"expected exactly 1 launchable result, got {len(launchable)}: {results}"
    already_restoring = [r for r in results if r.action == dispatcher.ACTION_NOOP_ALREADY_RESTORING]
    assert len(already_restoring) == len(results) - 1


# ---------------------------------------------------------------------------
# P2-2: --dry-run leaves DB logically unchanged; real prepare afterwards
# still succeeds normally.
# ---------------------------------------------------------------------------


def test_given_dry_run_when_classifying_then_db_state_unchanged(conn, state_root):
    binding_id, run_id = _seed_active_native_binding(conn, herdr_locator="dry-tab-1", session_id="dry-s1")
    before_binding = service.get_binding(conn, binding_id)
    before_run = service.get_execution_run(conn, run_id)
    conn.close()

    decision = dispatcher.classify_for_resume_readonly("dry-s1")
    assert decision.action == dispatcher.ACTION_LAUNCH_NATIVE

    fresh = dispatcher.open_dispatcher_db()
    try:
        after_binding = service.get_binding(fresh, binding_id)
        after_run = service.get_execution_run(fresh, run_id)
    finally:
        fresh.close()

    assert after_binding == before_binding, "dry-run classify must never mutate the Binding"
    assert after_run == before_run, "dry-run classify must never mutate the ExecutionRun"
    assert after_binding["runtime_health"] == "ACTIVE"


def test_given_dry_run_followed_by_real_prepare_when_run_then_real_prepare_succeeds_normally(
    conn, state_root
):
    _seed_active_native_binding(conn, herdr_locator="dry-tab-2", session_id="dry-s2")
    conn.close()

    dry = dispatcher.classify_for_resume_readonly("dry-s2")
    assert dry.action == dispatcher.ACTION_LAUNCH_NATIVE

    real = dispatcher.prepare_managed_resume("dry-s2")
    assert real.action == dispatcher.ACTION_LAUNCH_NATIVE, "a prior dry-run must never block the real prepare"


def test_given_cli_dry_run_flag_when_running_main_then_never_transitions_to_restoring(
    conn, state_root, capsys
):
    binding_id, _run_id = _seed_active_native_binding(conn, herdr_locator="dry-cli-tab-1", session_id="dry-cli-s1")
    conn.close()

    exit_code = dispatcher.main(["--session-id", "dry-cli-s1", "--pane-id", "pane-x", "--dry-run"])
    assert exit_code == 0

    fresh = dispatcher.open_dispatcher_db()
    try:
        binding = service.get_binding(fresh, binding_id)
        assert binding["runtime_health"] == "ACTIVE"
    finally:
        fresh.close()


def test_given_dry_run_when_state_root_not_yet_materialized_then_never_creates_db(state_root):
    """P2-2: dry-run must never create the DB file as a side effect --
    `db.connect_readonly` returns None when the file does not exist yet,
    and `classify_for_resume_readonly` must handle that without falling
    back to a write-capable open."""
    db_file = dispatcher.resolve_dispatcher_state_root() / __import__("task_context_config").DB_FILE_NAME
    assert not db_file.exists()

    decision = dispatcher.classify_for_resume_readonly("never-seen-session")
    assert decision.action == dispatcher.ACTION_NOOP_UNMANAGED
    assert not db_file.exists(), "dry-run must never materialize the state root/DB"
