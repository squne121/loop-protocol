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
  locator-mismatch candidate loss (AC1), orphan RESTORING re-detection
  always reported as unresolved_target WITHOUT any DB mutation -- never
  auto-converged to RESTORE_BLOCKED (AC2, per the 2026-09-25 Contract
  Reconciliation over PR #2754's OWNER review), the normal-path
  regression guard (AC3), and duplicate-pane claim conflicts (AC4).
- Issue #2752 (follow-up to #2742's ACTIVE + live-handoff carve-out):
  tri-state `herdr pane process-info` liveness classification
  (PROVEN_ALIVE/PROVEN_ABSENT/UNKNOWN) for ACTIVE Bindings whose locator
  IS live, the new `live_runtime_preserved` expected-success result (AC1/
  AC3 case 2), `liveness_unresolved` for ambiguous/failed probes (AC4),
  `missing_current_runtime_location` for Bindings whose current
  `runtime_locations` row is missing entirely (AC5), and the AC8 guard
  that only structured pid-identity evidence -- never cwd/terminal
  title/process name alone -- ever drives the classification. AC6
  (regression guard): every pre-#2752 test below is kept and updated
  (never deleted) to route the new `pane process-info` probe explicitly.
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


def _process_info_response(
    pane_id: str,
    *,
    shell_pid: int = 1000,
    foreground_processes: list | None = None,
    foreground_process_group_id: int | None = None,
) -> subprocess.CompletedProcess:
    """Issue #2752: builds a successful, parseable ``herdr pane process-info
    --pane <pane_id>`` response, matching the response shape fact-checked
    live against an installed Herdr 0.9.1 server during this Issue's
    implementation (nested one level under ``result.process_info``, never
    directly under ``result``). Callers choose exactly one of
    ``foreground_processes`` (full per-process data) or
    ``foreground_process_group_id`` (coarser platform) -- omitting both
    simulates a platform that only ever publishes ``shell_pid``
    (``_classify_pane_process_liveness()``'s
    ``process_info_platform_no_foreground_data`` UNKNOWN branch)."""
    process_info: dict = {"pane_id": pane_id, "shell_pid": shell_pid}
    if foreground_processes is not None:
        process_info["foreground_processes"] = foreground_processes
    if foreground_process_group_id is not None:
        process_info["foreground_process_group_id"] = foreground_process_group_id
    payload = {"id": "cli:pane:process_info", "result": {"process_info": process_info, "type": "pane_process_info"}}
    return subprocess.CompletedProcess(
        ["herdr", "pane", "process-info", "--pane", pane_id], returncode=0, stdout=json.dumps(payload), stderr=""
    )


def _bare_shell_process_info_response(pane_id: str, *, shell_pid: int = 1000) -> subprocess.CompletedProcess:
    """AC3 case 2 / PROVEN_ABSENT: parseable, successful, and the only
    foreground entry IS the pane's own shell."""
    return _process_info_response(
        pane_id,
        shell_pid=shell_pid,
        foreground_processes=[{"pid": shell_pid, "name": "bash", "cwd": "/tmp"}],
    )


def _alive_process_info_response(
    pane_id: str, *, shell_pid: int = 1000, foreground_pid: int = 4242, name: str = "claude"
) -> subprocess.CompletedProcess:
    """AC1/AC2 / PROVEN_ALIVE: a distinct non-shell foreground process."""
    return _process_info_response(
        pane_id,
        shell_pid=shell_pid,
        foreground_processes=[
            {"pid": shell_pid, "name": "bash", "cwd": "/tmp"},
            {"pid": foreground_pid, "name": name, "cwd": "/tmp"},
        ],
    )


def _routed_run(pane_list_ids: tuple[str, ...], process_info_responses: dict[str, subprocess.CompletedProcess]):
    """Issue #2752: `discover_resume_candidates()` now issues TWO distinct
    kinds of herdr subprocess calls through the SAME injected `run_fn` --
    `herdr pane list` (once) and `herdr pane process-info --pane <pane_id>`
    (once per unique, non-duplicate-claimed live ACTIVE locator). This
    router dispatches on the argv shape and raises loudly on any UNEXPECTED
    probe (e.g. a Binding this test asserts must never be probed at all --
    RESTORING, duplicate-claimed, locator-not-live, or missing-location
    Bindings)."""

    def _run(argv, **kwargs):
        if argv[1:3] == ["pane", "list"]:
            return _pane_list_response(*pane_list_ids)
        if argv[1:3] == ["pane", "process-info"]:
            assert argv[3] == "--pane", f"expected --pane flag, got argv={argv!r}"
            pane_id = argv[4]
            if pane_id not in process_info_responses:
                raise AssertionError(f"unexpected `herdr pane process-info` probe for pane {pane_id!r}")
            return process_info_responses[pane_id]
        raise AssertionError(f"unexpected herdr argv: {argv}")

    return _run


# ---------------------------------------------------------------------------
# discover_resume_candidates
# ---------------------------------------------------------------------------


def test_given_active_native_binding_with_live_pane_when_discovering_then_candidate_found(conn, state_root):
    _seed_active_native_binding(conn, herdr_locator="disc-tab-native", session_id="disc-s-native")

    _run = _routed_run(("disc-tab-native",), {"disc-tab-native": _bare_shell_process_info_response("disc-tab-native")})

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == [("disc-s-native", "disc-tab-native")]
    assert discovery["unresolved_targets"] == []
    assert discovery["restore_blocked"] == []
    assert discovery["live_runtime_preserved"] == []


def test_given_active_claude_gpt_binding_with_live_pane_when_discovering_then_candidate_found(
    conn, state_root, monkeypatch
):
    """fix_delta P1-1: Claude-GPT panes never populate Herdr's own
    `agent_session` field -- discovery must still find them via Task
    Context's own DB state, using the live pane list ONLY to confirm the
    locator still exists (not to read any agent_session field off it)."""
    _seed_active_claude_gpt_binding(conn, monkeypatch, herdr_locator="disc-tab-gpt", session_id="disc-s-gpt")

    _run = _routed_run(("disc-tab-gpt",), {"disc-tab-gpt": _bare_shell_process_info_response("disc-tab-gpt")})

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == [("disc-s-gpt", "disc-tab-gpt")]
    assert discovery["unresolved_targets"] == []
    assert discovery["restore_blocked"] == []
    assert discovery["live_runtime_preserved"] == []


def test_given_suspended_binding_when_discovering_then_not_a_candidate(conn, state_root):
    binding_id = _seed_active_native_binding(conn, herdr_locator="disc-tab-suspended", session_id="disc-s-suspended")
    hook_flows.on_session_end(conn, {"claude_session_id": "disc-s-suspended"})
    assert service.get_binding(conn, binding_id)["runtime_health"] == "SUSPENDED"

    # SUSPENDED Bindings are excluded by discovery's own SQL WHERE clause --
    # `herdr pane process-info` must never be probed for one at all.
    _run = _routed_run(("disc-tab-suspended",), {})

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == []
    assert discovery["unresolved_targets"] == []
    assert discovery["restore_blocked"] == []
    assert discovery["live_runtime_preserved"] == []


# ---------------------------------------------------------------------------
# AC3: locator mismatch (case 1) -> unresolved_target (never silently
# dropped, never liveness-probed)
# ---------------------------------------------------------------------------


def test_locator_mismatch_binding_reported_as_unresolved_target_and_any_failed_true(
    conn, state_root, monkeypatch, capsys
):
    """A stale/last-known locator that no longer names any currently-live
    pane in this session must not be dispatched into (nothing to send the
    command to) -- but, unlike the pre-#2742 behaviour, must be reported
    as an explicit `unresolved_target` (never silently indistinguishable
    from "no candidates at all"), and must propagate to `any_failed=true`
    / a nonzero `main()` exit code. Issue #2752 AC3 case 1: the pane-
    process liveness probe is never reached for this case at all (locator
    itself is not live -- there is nothing to probe)."""
    binding_id = _seed_active_native_binding(conn, herdr_locator="disc-tab-gone", session_id="disc-s-gone")

    _run = _routed_run(("some-other-unrelated-pane",), {})

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == []
    assert discovery["restore_blocked"] == []
    assert discovery["live_runtime_preserved"] == []
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
    _run = _routed_run(("unmanaged-pane-1", "unmanaged-pane-2"), {})

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == []
    assert discovery["unresolved_targets"] == []
    assert discovery["restore_blocked"] == []
    assert discovery["live_runtime_preserved"] == []


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
# AC3 case 2: existing normal-path regression guard -- locator-matched
# ACTIVE Binding, bare-shell-only pane process (real cold restart) ->
# dispatched, ACK'd, reported `restored`
# ---------------------------------------------------------------------------


def test_given_active_binding_dispatched_and_acked_when_orchestrating_then_reported_restored_and_no_failure(
    conn, state_root, monkeypatch
):
    """Issue #2742 AC3: the pre-existing normal path (locator-matched
    ACTIVE Binding -> dispatched -> ACK observed -> `restored`) must not
    regress from either the AC1 (locator-mismatch classification) or AC2
    (RESTORING re-detection) changes to `discover_resume_candidates()`, nor
    from the AC1/AC2/AC4 `any_failed` widening in
    `run_startup_orchestrator()`. Issue #2752 AC3 case 2: the pane process
    is now ALSO probed and must show bare-shell-only (a real cold restart
    re-generated an empty pane at the same locator) for this to remain
    dispatchable."""
    _seed_active_native_binding(conn, herdr_locator="normal-tab-1", session_id="normal-s1")
    conn.close()

    _discovery_run = _routed_run(("normal-tab-1",), {"normal-tab-1": _bare_shell_process_info_response("normal-tab-1")})

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

    _discovery_run = _routed_run(
        ("e2e-tab-native", "e2e-tab-gpt"),
        {
            "e2e-tab-native": _bare_shell_process_info_response("e2e-tab-native"),
            "e2e-tab-gpt": _bare_shell_process_info_response("e2e-tab-gpt"),
        },
    )

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

    # This test drives `dispatcher.prepare_managed_resume`/`await_all_acks`
    # directly (see comment below) -- it never calls
    # `discover_resume_candidates`, so it is unaffected by the Issue #2752
    # liveness probe.
    fast_binding_id = service.get_binding_by_current_session(dispatcher.open_dispatcher_db(), "e2e-s-fast")["id"]
    slow_binding_id = service.get_binding_by_current_session(dispatcher.open_dispatcher_db(), "e2e-s-slow")["id"]

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


def test_restoring_binding_always_reported_as_unresolved_target_without_db_mutation(conn, state_root):
    """`ACTIVE -> RESTORING commit -> ACK前に停止 -> 再度cold restart`: a
    prior dispatch attempt left this Binding stuck at RESTORING (its
    pre-restore ExecutionRun never technically closed -- no ACK ever
    arrived). Contract Reconciliation (2026-09-25, PR #2754 OWNER review,
    issuecomment-5828693174): `mark_restore_blocked_if_pending()`'s guard
    condition (`runtime_health == "RESTORING" AND execution_run.ended_at IS
    NULL`) only confirms a pending restore has not yet been ACKed -- it
    does NOT prove the underlying process/pane is actually dead, since a
    live Herdr handoff can leave a Binding in this exact same DB state
    while the pane is still genuinely alive. `discover_resume_candidates()`
    therefore NEVER calls `mark_restore_blocked_if_pending()` and NEVER
    mutates DB state for a RESTORING Binding -- it always reports it as
    `unresolved_target` and leaves `runtime_health` at RESTORING. Running
    the exact same discovery again afterwards must produce the identical
    result and leave the DB in the identical state (idempotent). Issue
    #2752: RESTORING Bindings are never pane-process-liveness-probed
    either (the `pane list` response below is deliberately never routed
    through a `process-info` responder)."""
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

    # The pane the Binding's OLD locator named may or may not still be
    # live -- discovery never inspects the live pane list (or probes pane
    # process liveness) to decide a RESTORING Binding's fate; it is
    # unconditionally reported as unresolved_target regardless.
    _run = _routed_run(("stale-tab-1",), {})

    expected_discovery = {
        "candidates": [],
        "unresolved_targets": [
            {
                "binding_id": binding_id,
                "session_id": "stale-s1",
                "reason": startup.REASON_RESTORING_NOT_PROVABLY_STALE,
            }
        ],
        "restore_blocked": [],
        "live_runtime_preserved": [],
    }

    fresh = dispatcher.open_dispatcher_db()
    try:
        discovery = startup.discover_resume_candidates(fresh, herdr_bin="herdr", run_fn=_run)
    finally:
        fresh.close()

    assert discovery == expected_discovery

    after_first = dispatcher.open_dispatcher_db()
    try:
        assert service.get_binding(after_first, binding_id)["runtime_health"] == "RESTORING"
        assert service.get_execution_run(after_first, run_id)["ended_at"] is None
    finally:
        after_first.close()

    # Idempotency: run the exact same discovery again. Since discovery
    # never mutates DB state for RESTORING Bindings, this must reproduce
    # the identical result and leave the DB in the identical state.
    fresh2 = dispatcher.open_dispatcher_db()
    try:
        discovery_again = startup.discover_resume_candidates(fresh2, herdr_bin="herdr", run_fn=_run)
    finally:
        fresh2.close()

    assert discovery_again == expected_discovery

    after_second = dispatcher.open_dispatcher_db()
    try:
        assert service.get_binding(after_second, binding_id)["runtime_health"] == "RESTORING"
        assert service.get_execution_run(after_second, run_id)["ended_at"] is None
    finally:
        after_second.close()

    summary = startup.run_startup_orchestrator(herdr_bin="herdr", discovery_run_fn=_run)
    matching = [r for r in summary["results"] if r.get("binding_id") == binding_id]
    assert len(matching) == 1
    assert matching[0]["dispatch_status"] == "unresolved_target"
    assert summary["any_failed"] is True


# ---------------------------------------------------------------------------
# AC4 (locator-level): duplicate ACTIVE Binding claims on the same live pane
# ---------------------------------------------------------------------------


def test_duplicate_binding_claims_on_same_live_pane_are_all_unresolved_and_not_dispatched(conn, state_root):
    """`runtime_locations` only enforces `UNIQUE(binding_id) WHERE
    released_at IS NULL` -- it does NOT enforce locator-level uniqueness
    across different Bindings. If 2+ ACTIVE Bindings' current
    (unreleased) RuntimeLocation both record the SAME live `herdr_locator`
    (e.g. a prior relocate/re-home race left two Bindings pointed at one
    pane), discovery must never arbitrarily pick one to dispatch into --
    every claimant is reported as `unresolved_target` and NONE are
    dispatched. Issue #2752: liveness is never even probed here -- there
    is no single unambiguous claimant to probe/dispatch into regardless of
    what the pane's process turns out to be."""
    binding_id_1 = _seed_active_native_binding(conn, herdr_locator="shared-tab-1", session_id="dup-s1")
    binding_id_2 = _seed_active_native_binding(conn, herdr_locator="shared-tab-1-temp", session_id="dup-s2")

    # Force binding_id_2's current RuntimeLocation to also point at
    # "shared-tab-1" -- directly, at the DB layer, since no normal service
    # helper produces two simultaneously-unreleased locations for the same
    # locator (this is exactly the anomaly AC4 must defend against even
    # though the ordinary write path never intentionally creates it).
    conn.execute(
        "UPDATE runtime_locations SET herdr_locator = ? WHERE binding_id = ? AND released_at IS NULL",
        ("shared-tab-1", binding_id_2),
    )
    conn.commit()

    _run = _routed_run(("shared-tab-1",), {})

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == []
    assert discovery["restore_blocked"] == []
    assert discovery["live_runtime_preserved"] == []
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


# ---------------------------------------------------------------------------
# Issue #2752 AC1: live handoff -- ACTIVE Native Binding, PROVEN_ALIVE pane
# process -> `live_runtime_preserved`, zero dispatch, Binding untouched.
# ---------------------------------------------------------------------------


def test_given_active_native_binding_with_proven_alive_pane_process_when_discovering_then_live_runtime_preserved(
    conn, state_root
):
    binding_id = _seed_active_native_binding(conn, herdr_locator="live-tab-native", session_id="live-s-native")

    _run = _routed_run(("live-tab-native",), {"live-tab-native": _alive_process_info_response("live-tab-native")})

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == []
    assert discovery["unresolved_targets"] == []
    assert discovery["restore_blocked"] == []
    assert len(discovery["live_runtime_preserved"]) == 1
    entry = discovery["live_runtime_preserved"][0]
    assert entry["binding_id"] == binding_id
    assert entry["session_id"] == "live-s-native"
    assert entry["pane_id"] == "live-tab-native"
    assert entry["runtime_profile"] == "native_claude_v1"
    assert entry["launch_commands_dispatched"] == 0
    assert entry["mutations_applied"] == 0
    assert "liveness_evidence" in entry

    # Binding identity/state must be completely untouched -- discovery
    # itself never opens a write_transaction.
    binding = service.get_binding(conn, binding_id)
    assert binding["runtime_health"] == "ACTIVE"
    assert binding["current_claude_session_id"] == "live-s-native"

    conn.close()
    summary = startup.run_startup_orchestrator(herdr_bin="herdr", discovery_run_fn=_run)
    assert summary["candidates_discovered"] == 0
    assert summary["any_failed"] is False
    matching = [r for r in summary["results"] if r.get("binding_id") == binding_id]
    assert len(matching) == 1
    assert matching[0]["dispatch_status"] == "live_runtime_preserved"
    assert matching[0]["runtime_profile"] == "native_claude_v1"

    fresh = dispatcher.open_dispatcher_db()
    try:
        binding_after = service.get_binding(fresh, binding_id)
        assert binding_after["runtime_health"] == "ACTIVE"
        assert binding_after["current_claude_session_id"] == "live-s-native"
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# Issue #2752 AC4: UNKNOWN pane-process liveness -> `liveness_unresolved`,
# never dispatched, `any_failed = true`.
# ---------------------------------------------------------------------------


def test_given_active_binding_with_failed_process_info_call_when_discovering_then_liveness_unresolved(
    conn, state_root, monkeypatch
):
    binding_id = _seed_active_native_binding(conn, herdr_locator="ambig-tab", session_id="ambig-s1")

    def _run(argv, **kwargs):
        if argv[1:3] == ["pane", "list"]:
            return _pane_list_response("ambig-tab")
        if argv[1:3] == ["pane", "process-info"]:
            return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="pane not found")
        raise AssertionError(f"unexpected herdr argv: {argv}")

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == []
    assert discovery["live_runtime_preserved"] == []
    assert discovery["restore_blocked"] == []
    assert discovery["unresolved_targets"] == [
        {
            "binding_id": binding_id,
            "session_id": "ambig-s1",
            "reason": startup.REASON_LIVENESS_UNRESOLVED,
        }
    ]

    conn.close()
    summary = startup.run_startup_orchestrator(herdr_bin="herdr", discovery_run_fn=_run)
    assert summary["candidates_discovered"] == 0
    assert summary["any_failed"] is True
    matching = [r for r in summary["results"] if r.get("binding_id") == binding_id]
    assert len(matching) == 1
    assert matching[0]["dispatch_status"] == "unresolved_target"
    assert matching[0]["reason"] == startup.REASON_LIVENESS_UNRESOLVED

    monkeypatch.setattr(subprocess, "run", _run)
    exit_code = startup.main(["--force-run-outside-scope-gate", "--herdr-bin", "herdr"])
    assert exit_code == 1

    # Never mutated -- false-green never possible here either.
    fresh = dispatcher.open_dispatcher_db()
    try:
        assert service.get_binding(fresh, binding_id)["runtime_health"] == "ACTIVE"
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# Issue #2752 AC5: missing current `runtime_locations` row (LEFT JOIN, no
# longer silently dropped by the previous implicit INNER JOIN)
# ---------------------------------------------------------------------------


def test_active_binding_with_no_current_runtime_location_reported_as_missing_current_runtime_location(conn, state_root):
    binding_id = _seed_active_native_binding(conn, herdr_locator="missing-loc-tab", session_id="missing-loc-s1")

    # Simulate "no current (unreleased) runtime_locations row at all" --
    # release the Binding's only location row and never insert a
    # replacement (this is the general case `relocate_binding()`'s
    # momentary release->insert never leaves committed, but a Binding could
    # reach this state via data loss / a partial migration / manual repair).
    conn.execute(
        "UPDATE runtime_locations SET released_at = ? WHERE binding_id = ? AND released_at IS NULL",
        (service.now_iso(), binding_id),
    )
    conn.commit()

    def _run(argv, **kwargs):
        if argv[1:3] == ["pane", "list"]:
            return _pane_list_response("unrelated-live-pane")
        raise AssertionError(f"must never probe a Binding with no current runtime_location row: {argv}")

    discovery = startup.discover_resume_candidates(conn, herdr_bin="herdr", run_fn=_run)
    assert discovery["candidates"] == []
    assert discovery["restore_blocked"] == []
    assert discovery["live_runtime_preserved"] == []
    assert discovery["unresolved_targets"] == [
        {
            "binding_id": binding_id,
            "session_id": "missing-loc-s1",
            "reason": startup.REASON_MISSING_CURRENT_RUNTIME_LOCATION,
        }
    ]

    conn.close()
    summary = startup.run_startup_orchestrator(herdr_bin="herdr", discovery_run_fn=_run)
    assert summary["candidates_discovered"] == 0
    assert summary["any_failed"] is True
    matching = [r for r in summary["results"] if r.get("binding_id") == binding_id]
    assert len(matching) == 1
    assert matching[0]["dispatch_status"] == "unresolved_target"
    assert matching[0]["reason"] == startup.REASON_MISSING_CURRENT_RUNTIME_LOCATION

    # dispatch 0 / DB mutation 0 -- runtime_health untouched.
    fresh = dispatcher.open_dispatcher_db()
    try:
        assert service.get_binding(fresh, binding_id)["runtime_health"] == "ACTIVE"
    finally:
        fresh.close()


def test_restoring_binding_with_no_current_runtime_location_also_reported_as_missing(conn, state_root):
    """AC5 applies to RESTORING Bindings too, not only ACTIVE."""
    binding_id, _run_id = _seed_active_native_binding_with_run_id(
        conn, herdr_locator="missing-loc-restoring-tab", session_id="missing-loc-restoring-s1"
    )
    conn.close()

    dispatcher.prepare_managed_resume("missing-loc-restoring-s1")

    fresh = dispatcher.open_dispatcher_db()
    try:
        assert service.get_binding(fresh, binding_id)["runtime_health"] == "RESTORING"
        fresh.execute(
            "UPDATE runtime_locations SET released_at = ? WHERE binding_id = ? AND released_at IS NULL",
            (service.now_iso(), binding_id),
        )
        fresh.commit()
    finally:
        fresh.close()

    def _run(argv, **kwargs):
        if argv[1:3] == ["pane", "list"]:
            return _pane_list_response()
        raise AssertionError(f"must never probe a Binding with no current runtime_location row: {argv}")

    conn2 = dispatcher.open_dispatcher_db()
    try:
        discovery = startup.discover_resume_candidates(conn2, herdr_bin="herdr", run_fn=_run)
    finally:
        conn2.close()

    assert discovery["unresolved_targets"] == [
        {
            "binding_id": binding_id,
            "session_id": "missing-loc-restoring-s1",
            "reason": startup.REASON_MISSING_CURRENT_RUNTIME_LOCATION,
        }
    ]
    assert discovery["candidates"] == []
    assert discovery["live_runtime_preserved"] == []


# ---------------------------------------------------------------------------
# Issue #2752 -- `_classify_pane_process_liveness()` unit tests (AC4 / AC8)
# ---------------------------------------------------------------------------


def test_classify_pane_process_liveness_bare_shell_only_is_proven_absent():
    def _run(argv, **kwargs):
        return _process_info_response(
            "pane-1", shell_pid=100, foreground_processes=[{"pid": 100, "name": "bash", "argv": ["bash"], "cwd": "/x"}]
        )

    classification, evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_PROVEN_ABSENT
    assert evidence["shell_pid"] == 100


def test_classify_pane_process_liveness_empty_foreground_list_is_proven_absent():
    def _run(argv, **kwargs):
        return _process_info_response("pane-1", shell_pid=100, foreground_processes=[])

    classification, _evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_PROVEN_ABSENT


def test_classify_pane_process_liveness_non_shell_foreground_process_is_proven_alive():
    def _run(argv, **kwargs):
        return _process_info_response(
            "pane-1",
            shell_pid=100,
            foreground_processes=[
                {"pid": 100, "name": "bash", "argv": ["bash"], "cwd": "/x"},
                {"pid": 200, "name": "claude", "argv": ["claude"], "cwd": "/x"},
            ],
        )

    classification, evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_PROVEN_ALIVE
    assert evidence["foreground_processes"] == [{"pid": 200, "name": "claude", "argv": ["claude"], "cwd": "/x"}]


def test_classify_pane_process_liveness_ac8_process_named_bash_but_different_pid_is_still_proven_alive():
    """AC8: process `name` alone must never be trusted -- a process whose
    reported name happens to be a common shell name ("bash") but whose pid
    does NOT match the pane's own shell_pid is still a distinct (alive)
    foreground process."""

    def _run(argv, **kwargs):
        return _process_info_response(
            "pane-1",
            shell_pid=100,
            foreground_processes=[
                {"pid": 100, "name": "bash", "argv": ["bash"], "cwd": "/x"},
                {"pid": 999, "name": "bash", "argv": ["bash", "-c", "sneaky"], "cwd": "/x"},
            ],
        )

    classification, _evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_PROVEN_ALIVE


def test_classify_pane_process_liveness_ac8_cwd_alone_never_used_for_decision():
    """AC8: `cwd` differing between two entries must never itself decide
    liveness -- only pid identity relative to `shell_pid` does. Here the
    single foreground entry IS the shell (pid matches) despite reporting a
    cwd that looks like an active working directory -- still
    PROVEN_ABSENT."""

    def _run(argv, **kwargs):
        return _process_info_response(
            "pane-1",
            shell_pid=100,
            foreground_processes=[{"pid": 100, "name": "bash", "argv": ["bash"], "cwd": "/home/user/active-project"}],
        )

    classification, _evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_PROVEN_ABSENT


def test_classify_pane_process_liveness_foreground_process_group_id_matches_shell_is_proven_absent():
    def _run(argv, **kwargs):
        return _process_info_response("pane-1", shell_pid=100, foreground_process_group_id=100)

    classification, _evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_PROVEN_ABSENT


def test_classify_pane_process_liveness_foreground_process_group_id_differs_from_shell_is_proven_alive():
    def _run(argv, **kwargs):
        return _process_info_response("pane-1", shell_pid=100, foreground_process_group_id=555)

    classification, _evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_PROVEN_ALIVE


def test_classify_pane_process_liveness_call_failure_is_unknown():
    def _run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="no such pane")

    classification, evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_UNKNOWN
    assert evidence["reason"] == "process_info_call_failed"


def test_classify_pane_process_liveness_empty_stdout_is_unknown():
    def _run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    classification, evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_UNKNOWN
    assert evidence["reason"] == "process_info_empty_response"


def test_classify_pane_process_liveness_unparseable_json_is_unknown():
    def _run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=0, stdout="not json{{{", stderr="")

    classification, evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_UNKNOWN
    assert evidence["reason"] == "process_info_unparseable_json"


def test_classify_pane_process_liveness_platform_not_exposing_foreground_data_is_unknown():
    """AC8: `shell_pid` alone (no `foreground_processes`/`foreground_pid` at
    all -- a platform that does not publish foreground-process data) must
    never be treated as proof of either liveness or absence."""

    def _run(argv, **kwargs):
        return _process_info_response("pane-1", shell_pid=100)

    classification, evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_UNKNOWN
    assert evidence["reason"] == "process_info_platform_no_foreground_data"


def test_classify_pane_process_liveness_ambiguous_foreground_entry_is_unknown():
    def _run(argv, **kwargs):
        return _process_info_response("pane-1", shell_pid=100, foreground_processes=[{"name": "mystery-process"}])

    classification, evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_UNKNOWN
    assert evidence["reason"] == "process_info_ambiguous_foreground_process_identity"


def test_classify_pane_process_liveness_call_raises_oserror_is_unknown():
    def _run(argv, **kwargs):
        raise OSError("herdr binary not found")

    classification, evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_UNKNOWN
    assert evidence["reason"] == "process_info_call_raised"


def test_classify_pane_process_liveness_missing_result_key_is_unknown():
    def _run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps({"id": "x"}), stderr="")

    classification, evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_UNKNOWN
    assert evidence["reason"] == "process_info_missing_result"


def test_classify_pane_process_liveness_result_without_process_info_key_is_unknown():
    """The real Herdr 0.9.1 response nests the actual payload one level
    under `result.process_info` -- a `result` dict present but missing
    that inner key entirely must be treated the same as no result at all."""

    def _run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps({"result": {}}), stderr="")

    classification, evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_UNKNOWN
    assert evidence["reason"] == "process_info_missing_result"


def test_classify_pane_process_liveness_empty_result_is_unknown():
    def _run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps({"result": {"process_info": {}}}), stderr=""
        )

    classification, evidence = startup._classify_pane_process_liveness("pane-1", herdr_bin="herdr", run_fn=_run)
    assert classification == startup.LIVENESS_UNKNOWN
    assert evidence["reason"] == "process_info_empty_result"
