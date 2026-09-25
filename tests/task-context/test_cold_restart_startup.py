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
- Issue #2742 (follow-up to #2570's runtime acceptance fact-check):
  locator-mismatch candidate loss (AC1), orphan RESTORING re-detection and
  its guarded convergence to RESTORE_BLOCKED (AC2), the normal-path
  regression guard (AC3), and duplicate-pane claim conflicts (AC4).
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


def _seed_active_native_binding_with_run_id(conn, *, herdr_locator: str, session_id: str) -> tuple[str, str]:
    """Issue #2742 AC2 tests need the pre-restore ExecutionRun id (to assert
    it is never technically closed by a pre-ACK failure/staleness
    convergence) alongside the binding id."""
    binding_id = _seed_active_native_binding(conn, herdr_locator=herdr_locator, session_id=session_id)
    run_id = service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="native_operator")[0]["id"]
    return binding_id, run_id


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

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == [("disc-s-native", "disc-tab-native")]
    assert discovery["unresolved_targets"] == []
    assert discovery["restore_blocked"] == []


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

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == [("disc-s-gpt", "disc-tab-gpt")]
    assert discovery["unresolved_targets"] == []
    assert discovery["restore_blocked"] == []


def test_given_suspended_binding_when_discovering_then_not_a_candidate(conn, state_root):
    binding_id = _seed_active_native_binding(conn, herdr_locator="disc-tab-suspended", session_id="disc-s-suspended")
    hook_flows.on_session_end(conn, {"claude_session_id": "disc-s-suspended"})
    assert service.get_binding(conn, binding_id)["runtime_health"] == "SUSPENDED"

    def _run(argv, **kwargs):
        return _pane_list_response("disc-tab-suspended")

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == []
    assert discovery["unresolved_targets"] == []
    assert discovery["restore_blocked"] == []


# ---------------------------------------------------------------------------
# AC1: locator mismatch -> unresolved_target (never silently dropped)
# ---------------------------------------------------------------------------


def test_locator_mismatch_binding_reported_as_unresolved_target_and_any_failed_true(
    conn, state_root, monkeypatch, capsys
):
    """A stale/last-known locator that no longer names any currently-live
    pane in this session must not be dispatched into (nothing to send the
    command to) -- but, unlike the pre-#2742 behaviour, must be reported
    as an explicit `unresolved_target` (never silently indistinguishable
    from "no candidates at all"), and must propagate to `any_failed=true`
    / a nonzero `main()` exit code."""
    binding_id = _seed_active_native_binding(conn, herdr_locator="disc-tab-gone", session_id="disc-s-gone")

    def _run(argv, **kwargs):
        return _pane_list_response("some-other-unrelated-pane")

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == []
    assert discovery["restore_blocked"] == []
    assert discovery["unresolved_targets"] == [
        {
            "binding_id": binding_id,
            "session_id": "disc-s-gone",
            "reason": startup.REASON_LOCATOR_NOT_LIVE,
        }
    ]

    conn.close()
    summary = startup.run_startup_orchestrator(herdr_bin="herdr", discovery_run_fn=_run)
    assert summary["candidates_discovered"] == 0
    assert summary["any_failed"] is True
    matching = [r for r in summary["results"] if r.get("binding_id") == binding_id]
    assert len(matching) == 1
    assert matching[0]["dispatch_status"] == "unresolved_target"
    assert matching[0]["reason"] == startup.REASON_LOCATOR_NOT_LIVE

    # AC1's own wording requires this to reach `main()`'s process exit code
    # (nonzero) too -- drive the real CLI entrypoint end to end (scope gate
    # forced open; `subprocess.run` monkeypatched module-wide since `main()`
    # never exposes a discovery-only `run_fn` knob of its own).
    monkeypatch.setattr(subprocess, "run", _run)
    exit_code = startup.main(["--force-run-outside-scope-gate", "--herdr-bin", "herdr"])
    assert exit_code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["any_failed"] is True


def test_given_no_managed_bindings_at_all_when_discovering_then_empty_candidate_list(conn, state_root):
    def _run(argv, **kwargs):
        return _pane_list_response("unmanaged-pane-1", "unmanaged-pane-2")

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == []
    assert discovery["unresolved_targets"] == []
    assert discovery["restore_blocked"] == []


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
# AC3: existing normal-path regression guard -- locator-matched ACTIVE
# Binding is discovered, dispatched, ACK'd, and reported `restored`
# ---------------------------------------------------------------------------


def test_given_active_binding_dispatched_and_acked_when_orchestrating_then_reported_restored_and_no_failure(
    conn, state_root, monkeypatch
):
    """Issue #2742 AC3: the pre-existing normal path (locator-matched
    ACTIVE Binding -> dispatched -> ACK observed -> `restored`) must not
    regress from either the AC1 (locator-mismatch classification) or AC2
    (RESTORING re-detection) changes to `discover_resume_candidates()`, nor
    from the AC1/AC2/AC4 `any_failed` widening in
    `run_startup_orchestrator()`."""
    _seed_active_native_binding(conn, herdr_locator="normal-tab-1", session_id="normal-s1")
    conn.close()

    def _discovery_run(argv, **kwargs):
        return _pane_list_response("normal-tab-1")

    def _dispatch_and_ack_run(argv, **kwargs):
        # The launch command "succeeds" (exit 0) and, exactly like a real
        # resumed Claude process would out-of-process, immediately drives
        # the SessionStart(source=resume, ...) ACK for the same session
        # before `execute_resume_decision()` even returns -- deterministic
        # stand-in for "the ACK arrives comfortably inside the shared
        # bounded deadline".
        ack_conn = dispatcher.open_dispatcher_db()
        try:
            hook_flows.on_session_start(
                ack_conn,
                {"source": "resume", "herdr_tab_id": "normal-tab-1-after-resume", "claude_session_id": "normal-s1"},
            )
        finally:
            ack_conn.close()
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    # `run_startup_orchestrator()` resolves the dispatch subprocess via
    # `subprocess.run` at call time (never its own injectable knob) --
    # discovery above is driven separately by its own `discovery_run_fn`.
    monkeypatch.setattr(subprocess, "run", _dispatch_and_ack_run)
    summary = startup.run_startup_orchestrator(
        herdr_bin="herdr",
        ack_timeout_seconds=1.0,
        discovery_run_fn=_discovery_run,
    )

    assert summary["candidates_discovered"] == 1
    assert summary["results"][0]["dispatch_status"] == "restored"
    assert summary["any_failed"] is False


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


# ---------------------------------------------------------------------------
# AC2: orphan RESTORING re-detection on a subsequent cold restart
# ---------------------------------------------------------------------------


def test_stale_restoring_binding_converges_to_restore_blocked_idempotently(conn, state_root):
    """`ACTIVE -> RESTORING commit -> ACK前に停止 -> 再度cold restart`: a
    prior dispatch attempt left this Binding stuck at RESTORING (its
    pre-restore ExecutionRun never technically closed -- no ACK ever
    arrived). A SUBSEQUENT `discover_resume_candidates()` call (simulating
    the next cold restart's `[[startup]]` hook firing) must re-detect it,
    confirm staleness via the existing guarded
    `mark_restore_blocked_if_pending()` primitive, and converge it to
    RESTORE_BLOCKED -- never send a duplicate resume command. Running the
    exact same discovery again afterwards must be a pure no-op (idempotent
    -- RESTORE_BLOCKED bindings are no longer selected by the query at
    all)."""
    binding_id, run_id = _seed_active_native_binding_with_run_id(
        conn, herdr_locator="stale-tab-1", session_id="stale-s1"
    )
    conn.close()

    # Leave this Binding stuck at RESTORING, exactly as an interrupted
    # cold-restart process would: prepare_managed_resume() commits
    # ACTIVE -> RESTORING but nothing ever dispatches/ACKs it (the prior
    # orchestrator process itself died first).
    decision = dispatcher.prepare_managed_resume("stale-s1")
    assert decision.action == dispatcher.ACTION_LAUNCH_NATIVE

    mid_flight = dispatcher.open_dispatcher_db()
    try:
        assert service.get_binding(mid_flight, binding_id)["runtime_health"] == "RESTORING"
        assert service.get_execution_run(mid_flight, run_id)["ended_at"] is None
    finally:
        mid_flight.close()

    def _run(argv, **kwargs):
        # The pane the Binding's OLD locator named may or may not still be
        # live -- staleness confirmation for a RESTORING Binding is
        # delegated entirely to `mark_restore_blocked_if_pending()`, never
        # decided by the live pane list.
        return _pane_list_response("stale-tab-1")

    fresh = dispatcher.open_dispatcher_db()
    try:
        discovery = startup.discover_resume_candidates(fresh, herdr_bin="herdr", run_fn=_run)
    finally:
        fresh.close()

    assert discovery["candidates"] == []
    assert discovery["unresolved_targets"] == []
    assert discovery["restore_blocked"] == [
        {
            "binding_id": binding_id,
            "session_id": "stale-s1",
            "reason": startup.REASON_STALE_RESTORING_CONVERGED,
        }
    ]

    after_first = dispatcher.open_dispatcher_db()
    try:
        assert service.get_binding(after_first, binding_id)["runtime_health"] == "RESTORE_BLOCKED"
        assert service.get_execution_run(after_first, run_id)["ended_at"] is None
    finally:
        after_first.close()

    # Idempotency: run the exact same discovery again. RESTORE_BLOCKED is
    # excluded from the discovery query entirely, so this must be a
    # complete no-op -- no re-emitted restore_blocked entry, no further DB
    # mutation, no duplicate resume command.
    fresh2 = dispatcher.open_dispatcher_db()
    try:
        discovery_again = startup.discover_resume_candidates(fresh2, herdr_bin="herdr", run_fn=_run)
    finally:
        fresh2.close()

    assert discovery_again == {"candidates": [], "unresolved_targets": [], "restore_blocked": []}

    after_second = dispatcher.open_dispatcher_db()
    try:
        assert service.get_binding(after_second, binding_id)["runtime_health"] == "RESTORE_BLOCKED"
        assert service.get_execution_run(after_second, run_id)["ended_at"] is None
    finally:
        after_second.close()


def test_restoring_binding_not_provably_stale_is_left_unchanged_as_unresolved_target(
    conn, state_root, monkeypatch
):
    """If staleness cannot be confirmed (the guarded primitive itself
    declines the RESTORE_BLOCKED transition -- e.g. a genuinely concurrent
    ACK lands for this exact Binding between discovery's own read and its
    call into `mark_restore_blocked_if_pending()`), the Binding must be
    left completely unchanged and reported as `unresolved_target` --
    never forced into RESTORE_BLOCKED, and never silently dropped."""
    binding_id, run_id = _seed_active_native_binding_with_run_id(
        conn, herdr_locator="race-tab-1", session_id="race-s1"
    )
    conn.close()

    decision = dispatcher.prepare_managed_resume("race-s1")
    assert decision.action == dispatcher.ACTION_LAUNCH_NATIVE

    real_mark_restore_blocked_if_pending = dispatcher.mark_restore_blocked_if_pending

    def _racing_ack_then_real_guard(bid, exec_run_id):
        # Inject a genuinely concurrent ACK for this exact Binding right
        # in the race window between discover_resume_candidates()'s own
        # SELECT (which observed RESTORING) and this guard call --
        # deterministically reproducing the exact condition
        # `mark_restore_blocked_if_pending()`'s own docstring documents as
        # a "late/stale...signal" that must never downgrade an
        # already-ACTIVE Binding.
        if bid == binding_id:
            ack_conn = dispatcher.open_dispatcher_db()
            try:
                hook_flows.on_session_start(
                    ack_conn,
                    {
                        "source": "resume",
                        "herdr_tab_id": "race-tab-1-new-after-ack",
                        "claude_session_id": "race-s1",
                    },
                )
            finally:
                ack_conn.close()
        return real_mark_restore_blocked_if_pending(bid, exec_run_id)

    monkeypatch.setattr(dispatcher, "mark_restore_blocked_if_pending", _racing_ack_then_real_guard)

    def _run(argv, **kwargs):
        return _pane_list_response("race-tab-1")

    fresh = dispatcher.open_dispatcher_db()
    try:
        discovery = startup.discover_resume_candidates(fresh, herdr_bin="herdr", run_fn=_run)
    finally:
        fresh.close()

    assert discovery["candidates"] == []
    assert discovery["restore_blocked"] == []
    assert discovery["unresolved_targets"] == [
        {
            "binding_id": binding_id,
            "session_id": "race-s1",
            "reason": startup.REASON_RESTORING_NOT_PROVABLY_STALE,
        }
    ]

    # The racing ACK itself (not discovery) is what advanced the Binding to
    # ACTIVE -- discovery's own guard call must be a pure no-op on top of
    # that (never itself mutate anything further).
    after = dispatcher.open_dispatcher_db()
    try:
        binding_after = service.get_binding(after, binding_id)
        assert binding_after["runtime_health"] == "ACTIVE"
        old_run_after = service.get_execution_run(after, run_id)
        assert old_run_after["ended_at"] is not None
    finally:
        after.close()

    summary = startup.run_startup_orchestrator(herdr_bin="herdr", discovery_run_fn=_run)
    matching = [r for r in summary["results"] if r.get("binding_id") == binding_id]
    assert len(matching) == 1
    assert matching[0]["dispatch_status"] == "unresolved_target"
    assert summary["any_failed"] is True


# ---------------------------------------------------------------------------
# AC4: duplicate ACTIVE Binding claims on the same live pane
# ---------------------------------------------------------------------------


def test_duplicate_binding_claims_on_same_live_pane_are_all_unresolved_and_not_dispatched(conn, state_root):
    """`runtime_locations` only enforces `UNIQUE(binding_id) WHERE
    released_at IS NULL` -- it does NOT enforce locator-level uniqueness
    across different Bindings. If 2+ ACTIVE Bindings' current
    (unreleased) RuntimeLocation both record the SAME live `herdr_locator`
    (e.g. a prior relocate/re-home race left two Bindings pointed at one
    pane), discovery must never arbitrarily pick one to dispatch into --
    every claimant is reported as `unresolved_target` and NONE are
    dispatched."""
    binding_id_1 = _seed_active_native_binding(conn, herdr_locator="shared-tab-1", session_id="dup-s1")
    binding_id_2 = _seed_active_native_binding(conn, herdr_locator="shared-tab-1-temp", session_id="dup-s2")

    # Force binding_id_2's current RuntimeLocation to also point at
    # "shared-tab-1" -- directly, at the DB layer, since no normal service
    # helper produces two simultaneously-unreleased locations for the same
    # locator (this is exactly the anomaly AC4 must defend against even
    # though the ordinary write path never intentionally creates it).
    conn.execute(
        "UPDATE runtime_locations SET herdr_locator = ? "
        "WHERE binding_id = ? AND released_at IS NULL",
        ("shared-tab-1", binding_id_2),
    )
    conn.commit()

    def _run(argv, **kwargs):
        return _pane_list_response("shared-tab-1")

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == []
    assert discovery["restore_blocked"] == []
    reported_binding_ids = {entry["binding_id"] for entry in discovery["unresolved_targets"]}
    assert reported_binding_ids == {binding_id_1, binding_id_2}
    for entry in discovery["unresolved_targets"]:
        assert entry["reason"] == startup.REASON_DUPLICATE_LOCATOR_CLAIM

    conn.close()
    summary = startup.run_startup_orchestrator(herdr_bin="herdr", discovery_run_fn=_run)
    assert summary["candidates_discovered"] == 0
    assert summary["any_failed"] is True
    dispatched = [r for r in summary["results"] if r["dispatch_status"] not in ("unresolved_target",)]
    assert dispatched == [], "neither duplicate claimant must ever be dispatched"
