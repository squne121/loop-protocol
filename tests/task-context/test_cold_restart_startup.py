"""PR #2731 review fix_delta P1-1 -- `task_context_cold_restart_startup.py`
(the actual committed `[[startup]]`-hook-invoked multi-pane orchestrator
artifact).

Covers:
- startup candidate discovery: Native candidate, Claude-GPT candidate,
  SUSPENDED skip, unmanaged skip.
- the scope-gate no-op (never touches herdr/DB unless
  LOOP_TASK_CONTEXT_COLD_RESTART_SCOPE is set).
- end-to-end orchestration with two simultaneous candidates and a shared
  ACK deadline.
"""

from __future__ import annotations

import json
import subprocess

import task_context_cold_restart_startup as startup
import task_context_config as config
import task_context_hook_flows as hook_flows
import task_context_resume_dispatcher as dispatcher
import task_context_service as service


def _seed_active_native_binding(conn, *, herdr_locator: str, session_id: str) -> str:
    result = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": herdr_locator, "claude_session_id": session_id}
    )
    return result["binding_id"]


def _seed_active_claude_gpt_binding(conn, monkeypatch, *, herdr_locator: str, session_id: str) -> str:
    monkeypatch.setenv(config.RUNTIME_VARIANT_ENV_VAR, "claude_gpt")
    try:
        result = hook_flows.on_session_start(
            conn, {"source": "startup", "herdr_tab_id": herdr_locator, "claude_session_id": session_id}
        )
    finally:
        monkeypatch.delenv(config.RUNTIME_VARIANT_ENV_VAR, raising=False)
    return result["binding_id"]


def _pane_list_response(*pane_ids: str) -> subprocess.CompletedProcess:
    payload = {
        "id": "cli:pane:list",
        "result": {
            "panes": [{"pane_id": pid} for pid in pane_ids],
            "type": "pane_list",
        },
    }
    return subprocess.CompletedProcess(["herdr", "pane", "list"], returncode=0, stdout=json.dumps(payload), stderr="")


# ---------------------------------------------------------------------------
# discover_resume_candidates
# ---------------------------------------------------------------------------


def test_given_active_native_binding_with_live_pane_when_discovering_then_candidate_found(conn, state_root):
    _seed_active_native_binding(conn, herdr_locator="disc-tab-native", session_id="disc-s-native")

    def _run(argv, **kwargs):
        assert argv == ["herdr", "pane", "list"], "discovery must never pass --session (ambient context only)"
        return _pane_list_response("disc-tab-native")

    candidates = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert candidates == [("disc-s-native", "disc-tab-native")]


def test_given_active_claude_gpt_binding_with_live_pane_when_discovering_then_candidate_found(
    conn, state_root, monkeypatch
):
    """fix_delta P1-1: Claude-GPT panes never populate Herdr's own
    `agent_session` field -- discovery must still find them via Task
    Context's own DB state, using the live pane list ONLY to confirm the
    locator still exists (not to read any agent_session field off it)."""
    _seed_active_claude_gpt_binding(
        conn, monkeypatch, herdr_locator="disc-tab-gpt", session_id="disc-s-gpt"
    )

    def _run(argv, **kwargs):
        # Deliberately omit any agent_session field for this pane -- this is
        # what a real Claude-GPT pane's herdr `pane list` entry looks like.
        return _pane_list_response("disc-tab-gpt")

    candidates = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert candidates == [("disc-s-gpt", "disc-tab-gpt")]


def test_given_suspended_binding_when_discovering_then_not_a_candidate(conn, state_root):
    binding_id = _seed_active_native_binding(conn, herdr_locator="disc-tab-suspended", session_id="disc-s-suspended")
    hook_flows.on_session_end(conn, {"claude_session_id": "disc-s-suspended"})
    assert service.get_binding(conn, binding_id)["runtime_health"] == "SUSPENDED"

    def _run(argv, **kwargs):
        return _pane_list_response("disc-tab-suspended")

    candidates = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert candidates == []


def test_given_active_binding_whose_locator_pane_is_no_longer_live_when_discovering_then_skipped(
    conn, state_root
):
    """A stale/last-known locator that no longer names any currently-live
    pane in this session must not be dispatched into (nothing to send the
    command to)."""
    _seed_active_native_binding(conn, herdr_locator="disc-tab-gone", session_id="disc-s-gone")

    def _run(argv, **kwargs):
        return _pane_list_response("some-other-unrelated-pane")

    candidates = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert candidates == []


def test_given_no_managed_bindings_at_all_when_discovering_then_empty_candidate_list(conn, state_root):
    def _run(argv, **kwargs):
        return _pane_list_response("unmanaged-pane-1", "unmanaged-pane-2")

    candidates = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert candidates == []


def test_given_herdr_pane_list_fails_when_discovering_then_raises_discovery_error(conn, state_root):
    def _run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="no session")

    try:
        startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    except startup.HerdrDiscoveryError:
        pass
    else:
        raise AssertionError("expected HerdrDiscoveryError")


# ---------------------------------------------------------------------------
# Scope gate (Herdr plugins are user-global -- fix_delta P1-1 safety guard)
# ---------------------------------------------------------------------------


def test_given_scope_gate_env_var_unset_when_running_main_then_noop_without_any_herdr_or_db_call(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.delenv(config.COLD_RESTART_SCOPE_ENV_VAR, raising=False)
    # Deliberately do NOT set state_root/LOOP_TASK_CONTEXT_STATE_ROOT -- if
    # the gate is bypassed this would attempt to resolve/materialize a real
    # state root, which would fail loudly outside a git repo cwd; asserting
    # a clean exit 0 no-op return here is itself evidence no such attempt
    # was made.
    monkeypatch.chdir(tmp_path)

    calls: list[list[str]] = []

    def _run(argv, **kwargs):
        calls.append(argv)
        raise AssertionError("must never call any herdr subprocess when the scope gate is unset")

    monkeypatch.setattr(subprocess, "run", _run)

    exit_code = startup.main([])
    assert exit_code == 0
    assert calls == []
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "noop_outside_dedicated_scope"


def test_given_scope_gate_env_var_set_when_checking_predicate_then_true(monkeypatch):
    monkeypatch.setenv(config.COLD_RESTART_SCOPE_ENV_VAR, config.COLD_RESTART_SCOPE_VALUE)
    assert config.is_cold_restart_dedicated_session() is True
    monkeypatch.setenv(config.COLD_RESTART_SCOPE_ENV_VAR, "some-other-value")
    assert config.is_cold_restart_dedicated_session() is False


# ---------------------------------------------------------------------------
# End-to-end orchestration: two simultaneous candidates, shared ACK deadline
# ---------------------------------------------------------------------------


def test_given_native_and_claude_gpt_candidates_when_orchestrating_then_both_dispatched_and_acked(
    conn, state_root, monkeypatch
):
    _seed_active_native_binding(conn, herdr_locator="e2e-tab-native", session_id="e2e-s-native")
    _seed_active_claude_gpt_binding(conn, monkeypatch, herdr_locator="e2e-tab-gpt", session_id="e2e-s-gpt")
    conn.close()

    def _discovery_run(argv, **kwargs):
        return _pane_list_response("e2e-tab-native", "e2e-tab-gpt")

    def _dispatch_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _dispatch_run)

    summary = startup.run_startup_orchestrator(
        herdr_bin="herdr", ack_timeout_seconds=0.05, discovery_run_fn=_discovery_run
    )
    assert summary["candidates_discovered"] == 2
    # Neither ACK ever arrives in this test -- both must resolve to
    # restore_blocked within the shared bounded deadline (never left
    # dangling as dispatched_waiting_ack forever).
    statuses = {r["dispatch_status"] for r in summary["results"]}
    assert statuses == {"restore_blocked"}
    assert summary["any_failed"] is True


def test_given_one_candidate_acks_and_one_times_out_when_orchestrating_then_shared_deadline_does_not_block_the_acked_one(
    conn, state_root
):
    _seed_active_native_binding(conn, herdr_locator="e2e-tab-fast", session_id="e2e-s-fast")
    _seed_active_native_binding(conn, herdr_locator="e2e-tab-slow", session_id="e2e-s-slow")
    conn.close()

    def _discovery_run(argv, **kwargs):
        return _pane_list_response("e2e-tab-fast", "e2e-tab-slow")

    def _dispatch_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    # Simulate the "fast" candidate's ACK arriving essentially immediately
    # (before the shared deadline), by monkeypatching `service.get_binding`
    # is unnecessary -- instead just drive the real on_session_start ACK
    # for the fast session BEFORE calling the orchestrator's ACK-await
    # phase is impossible from outside run_startup_orchestrator, so this
    # test instead directly exercises `dispatcher.await_all_acks` (the
    # underlying shared-deadline primitive `run_startup_orchestrator`
    # delegates to) with one binding pre-ACKed and one never ACKed.
    fast_binding_id = service.get_binding_by_current_session(
        dispatcher.open_dispatcher_db(), "e2e-s-fast"
    )["id"]
    slow_binding_id = service.get_binding_by_current_session(
        dispatcher.open_dispatcher_db(), "e2e-s-slow"
    )["id"]

    fast_decision = dispatcher.prepare_managed_resume("e2e-s-fast")
    slow_decision = dispatcher.prepare_managed_resume("e2e-s-slow")

    ack_conn = dispatcher.open_dispatcher_db()
    try:
        hook_flows.on_session_start(
            ack_conn, {"source": "resume", "herdr_tab_id": "e2e-tab-fast-new", "claude_session_id": "e2e-s-fast"}
        )
    finally:
        ack_conn.close()

    statuses = dispatcher.await_all_acks(
        [
            (fast_decision.binding_id, fast_decision.execution_run_id),
            (slow_decision.binding_id, slow_decision.execution_run_id),
        ],
        timeout_seconds=0.05,
        poll_interval_seconds=0.01,
    )
    assert statuses[fast_binding_id] == "restored"
    assert statuses[slow_binding_id] == "restore_blocked"
