"""Issue #2822 AC10 -- reader-side protection of managed-origin resolution.

`SubagentStart` now stores the hook's common `session_id` in
``execution_runs.claude_session_id`` for hook-origin SubAgent rows (the
parent/caller session, used to scope SendMessage addressability), while a
managed operator row keeps its own session there. Origin resolution must not
treat such a row as an origin candidate. Tests are writer -> reader
integrated: rows are written by the real `on_subagent_start` adapter flow and
read by the production `_resolve_origin_tx` / `diagnose_origin` /
`apply_workflow_signal`.
"""

from __future__ import annotations

import task_context_hook_flows as hook_flows
import task_context_service as service
import task_context_workflow_signals as signals
from workflow_signal_test_support import create_origin, implementation_payload, mutation_counts

SESSION = "session-origin-scope"


def _hook_subagent(conn, *, session=SESSION, agent_id="agent-scope-1"):
    return hook_flows.on_subagent_start(conn, {"claude_session_id": session, "agent_id": agent_id})


def _failure_events(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = ?", (signals._ORIGIN_RESOLUTION_FAILURE_EVENT_TYPE,)
    ).fetchone()[0]


def _classification(conn, session):
    origin, failure = signals._resolve_origin_tx(conn, session)
    diag = signals.diagnose_origin(conn, session)
    return origin, failure, diag


def test_origin_subagent_session_scope_ended_operator_plus_open_hook_subagent_stays_origin_run_ended(conn):
    _task, _activity, _binding, run = create_origin(conn, session=SESSION)
    service.end_execution_run(conn, run["id"])
    sub = _hook_subagent(conn)
    assert service.get_execution_run(conn, sub["execution_run_id"])["claude_session_id"] == SESSION

    origin, failure, diag = _classification(conn, SESSION)

    assert origin is None
    assert failure["reason_code"] == "origin_run_ended"
    assert failure["execution_run_id"] == run["id"]
    assert diag["reason_code"] == "origin_run_ended"


def test_origin_subagent_session_scope_valid_origin_plus_hook_subagent_still_resolves(conn):
    _task, _activity, _binding, run = create_origin(conn, session=SESSION)
    _hook_subagent(conn, agent_id="agent-a")
    _hook_subagent(conn, agent_id="agent-b")
    hook_flows.on_subagent_start(conn, {"claude_session_id": SESSION})

    origin, failure, diag = _classification(conn, SESSION)

    assert failure is None
    assert origin["execution_run_id"] == run["id"]
    assert diag["resolved"] is True and diag["execution_run_id"] == run["id"]


def test_origin_subagent_session_scope_hook_subagent_only_session_is_run_not_found_with_zero_events(conn):
    _hook_subagent(conn, agent_id="agent-only")
    hook_flows.on_subagent_start(conn, {"claude_session_id": SESSION})
    events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    origin, failure, diag = _classification(conn, SESSION)
    assert origin is None
    assert failure == {"disposition": "deferred", "reason_code": "origin_run_not_found"}
    assert diag["reason_code"] == "origin_run_not_found"

    assert _failure_events(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before


def test_origin_subagent_session_scope_apply_signal_on_hook_subagent_only_session_is_non_mutating(conn):
    _hook_subagent(conn)
    counts_before = mutation_counts(conn)

    result = signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id=SESSION)

    assert result["disposition"] == "deferred"
    assert result["reason_code"] == "unbound"
    assert _failure_events(conn) == 0
    assert mutation_counts(conn) == counts_before


def test_origin_subagent_session_scope_bound_subagent_row_keeps_kind_mismatch_contract(conn):
    """The protection keys on `binding_id IS NULL`; the #2719/#2790 fixture
    shape (subagent row WITH a binding) still classifies as kind mismatch."""
    task = service.create_task(conn, title="bound-subagent")
    activity = service.transition_activity(conn, task["id"], "implementation")
    binding = service.create_binding(conn)
    run = service.start_execution_run(
        conn,
        run_kind="subagent",
        task_id=task["id"],
        activity_id=activity["id"],
        binding_id=binding["id"],
        claude_session_id="s-bound-sub",
    )

    origin, failure, diag = _classification(conn, "s-bound-sub")

    assert origin is None
    assert failure["reason_code"] == "origin_run_kind_mismatch"
    assert failure["execution_run_id"] == run["id"]
    assert diag["reason_code"] == "origin_run_kind_mismatch"


def test_origin_subagent_session_scope_diagnose_and_resolver_classification_agree(conn):
    _task, _activity, _binding, run = create_origin(conn, session="s-agree-ended")
    service.end_execution_run(conn, run["id"])
    _hook_subagent(conn, session="s-agree-ended", agent_id="agree-1")
    create_origin(conn, session="s-agree-ok")
    _hook_subagent(conn, session="s-agree-ok", agent_id="agree-2")
    _hook_subagent(conn, session="s-agree-none", agent_id="agree-3")

    for session in ("s-agree-ended", "s-agree-ok", "s-agree-none", "s-agree-missing", None, ""):
        origin, failure = signals._resolve_origin_tx(conn, session)
        diag = signals.diagnose_origin(conn, session)
        assert diag["resolved"] == (origin is not None)
        assert diag["reason_code"] == (None if origin is not None else failure["reason_code"])


def test_origin_subagent_session_scope_classifier_excludes_hook_rows_but_not_bound_rows():
    hook_row = {
        "execution_run_id": "r1",
        "task_id": None,
        "activity_id": None,
        "binding_id": None,
        "ended_at": None,
        "run_kind": "subagent",
        "binding_session_id": None,
    }
    bound_row = {**hook_row, "execution_run_id": "r2", "binding_id": "b1", "task_id": "t1"}

    origin, failure = signals._classify_origin_candidates([hook_row], "s")
    assert origin is None and failure["reason_code"] == "origin_run_not_found"

    origin, failure = signals._classify_origin_candidates([hook_row, bound_row], "s")
    assert origin is None and failure["reason_code"] == "origin_run_kind_mismatch"
    assert failure["execution_run_id"] == "r2"
