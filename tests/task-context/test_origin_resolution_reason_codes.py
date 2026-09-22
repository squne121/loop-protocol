"""Issue #2719 AC1/AC2: `_resolve_origin_tx()` unbound reason-code decomposition.

Covers the 7 distinguishable reason codes the previous single 6-predicate
ANDed SQL query collapsed into one opaque ``unbound`` outcome, and the
append-only ``events`` journal persistence of that specific reason code
(AC2), all without regressing the pre-existing Issue #2565/#2690/#2692
"unbound origin resolution is fully non-mutating" contract that
``test_signal_workflow_contract.py`` / ``test_signal_origin_first_precedence.py``
/ ``test_signal_cleanup_completed_commit_point.py`` already enforce for the
``origin_session_missing`` / ``origin_run_not_found`` causes specifically.
"""

from __future__ import annotations

import json

import task_context_service as service
import task_context_workflow_signals as signals
from workflow_signal_test_support import implementation_payload, mutation_counts


def _task_activity_binding(conn, *, kind: str = "implementation"):
    task = service.create_task(conn, title="origin-reason-code")
    activity = service.transition_activity(conn, task["id"], kind)
    binding = service.create_binding(conn)
    return task, activity, binding


def test_given_no_session_id_when_origin_resolved_then_reason_is_origin_session_missing(conn):
    for missing in (None, ""):
        origin, outcome = signals._resolve_origin_tx(conn, missing)
        assert origin is None
        assert outcome == {"disposition": "deferred", "reason_code": "origin_session_missing"}


def test_given_session_with_no_execution_run_when_origin_resolved_then_reason_is_origin_run_not_found(conn):
    origin, outcome = signals._resolve_origin_tx(conn, "no-such-session")
    assert origin is None
    assert outcome == {"disposition": "deferred", "reason_code": "origin_run_not_found"}


def test_given_ended_run_when_origin_resolved_then_reason_is_origin_run_ended(conn):
    task, activity, binding = _task_activity_binding(conn)
    run = service.start_execution_run(
        conn,
        run_kind="native_operator",
        task_id=task["id"],
        activity_id=activity["id"],
        binding_id=binding["id"],
        claude_session_id="s-ended",
    )
    service.set_binding_session(conn, binding["id"], "s-ended", execution_run_id=run["id"])
    service.end_execution_run(conn, run["id"])

    origin, outcome = signals._resolve_origin_tx(conn, "s-ended")

    assert origin is None
    assert outcome["reason_code"] == "origin_run_ended"
    assert outcome["execution_run_id"] == run["id"]
    assert outcome["task_id"] == task["id"]


def test_given_non_managed_run_kind_when_origin_resolved_then_reason_is_origin_run_kind_mismatch(conn):
    task, activity, binding = _task_activity_binding(conn)
    run = service.start_execution_run(
        conn,
        run_kind="subagent",
        task_id=task["id"],
        activity_id=activity["id"],
        binding_id=binding["id"],
        claude_session_id="s-kind-mismatch",
    )

    origin, outcome = signals._resolve_origin_tx(conn, "s-kind-mismatch")

    assert origin is None
    assert outcome["reason_code"] == "origin_run_kind_mismatch"
    assert outcome["execution_run_id"] == run["id"]


def test_given_run_with_no_task_when_origin_resolved_then_reason_is_origin_task_unattached(conn):
    binding = service.create_binding(conn)
    run = service.start_execution_run(
        conn,
        run_kind="native_operator",
        binding_id=binding["id"],
        claude_session_id="s-unattached",
    )

    origin, outcome = signals._resolve_origin_tx(conn, "s-unattached")

    assert origin is None
    assert outcome["reason_code"] == "origin_task_unattached"
    assert outcome["execution_run_id"] == run["id"]
    assert outcome["task_id"] is None


def test_given_binding_current_session_mismatch_when_origin_resolved_then_reason_is_origin_binding_session_mismatch(
    conn,
):
    task, activity, binding = _task_activity_binding(conn)
    run = service.start_execution_run(
        conn,
        run_kind="native_operator",
        task_id=task["id"],
        activity_id=activity["id"],
        binding_id=binding["id"],
        claude_session_id="s-binding-mismatch",
    )
    # Deliberately never call set_binding_session: the Binding's
    # current_claude_session_id stays NULL, which must never satisfy the
    # equality predicate against the given origin_session_id.

    origin, outcome = signals._resolve_origin_tx(conn, "s-binding-mismatch")

    assert origin is None
    assert outcome["reason_code"] == "origin_binding_session_mismatch"
    assert outcome["execution_run_id"] == run["id"]
    assert outcome["task_id"] == task["id"]


def test_given_two_fully_qualifying_candidates_when_classified_then_reason_is_origin_ambiguous():
    # `origin_ambiguous` (>1 candidate satisfying every predicate) is
    # unreachable through any real DB write today: the
    # `ux_execution_runs_open_managed_session` partial unique index already
    # forbids two simultaneously open+managed ExecutionRuns from sharing one
    # claude_session_id. Exercising the pure predicate classifier directly
    # (bypassing the SQL fetch) is the only way to give this defensive
    # branch its own deterministic regression coverage.
    candidates = [
        {
            "execution_run_id": "run-1",
            "task_id": "task-1",
            "activity_id": "activity-1",
            "binding_id": "binding-1",
            "ended_at": None,
            "run_kind": "native_operator",
            "binding_session_id": "s-ambiguous",
        },
        {
            "execution_run_id": "run-2",
            "task_id": "task-2",
            "activity_id": "activity-2",
            "binding_id": "binding-2",
            "ended_at": None,
            "run_kind": "claude_gpt",
            "binding_session_id": "s-ambiguous",
        },
    ]

    origin, outcome = signals._classify_origin_candidates(candidates, "s-ambiguous")

    assert origin is None
    assert outcome["reason_code"] == "origin_ambiguous"
    assert outcome["count"] == 2


def test_given_fully_bound_session_when_origin_resolved_then_it_still_resolves_successfully(conn):
    task, activity, binding = _task_activity_binding(conn)
    run = service.start_execution_run(
        conn,
        run_kind="native_operator",
        task_id=task["id"],
        activity_id=activity["id"],
        binding_id=binding["id"],
        claude_session_id="s-bound",
    )
    service.set_binding_session(conn, binding["id"], "s-bound", execution_run_id=run["id"])

    origin, outcome = signals._resolve_origin_tx(conn, "s-bound")

    assert outcome is None
    assert origin == {
        "execution_run_id": run["id"],
        "task_id": task["id"],
        "activity_id": activity["id"],
        "binding_id": binding["id"],
    }


def _events_with_type(conn, event_type: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM events WHERE event_type = ? ORDER BY occurred_at", (event_type,)).fetchall()
    return [dict(row) for row in rows]


def test_given_origin_run_ended_when_signal_applied_then_specific_reason_code_is_persisted_and_readable_via_select(
    conn,
):
    task, activity, binding = _task_activity_binding(conn)
    run = service.start_execution_run(
        conn,
        run_kind="native_operator",
        task_id=task["id"],
        activity_id=activity["id"],
        binding_id=binding["id"],
        claude_session_id="s-ended-signal",
    )
    service.set_binding_session(conn, binding["id"], "s-ended-signal", execution_run_id=run["id"])
    service.end_execution_run(conn, run["id"])

    result = signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="s-ended-signal")

    # The frozen public disposition is unchanged (Issue #2565 contract):
    # only the events-journal side channel carries the specific cause.
    assert result == {"disposition": "deferred", "reason_code": "unbound"}

    persisted = _events_with_type(conn, "workflow:origin_resolution_failed")
    assert len(persisted) == 1
    event = persisted[0]
    assert event["execution_run_id"] == run["id"]
    assert event["task_id"] == task["id"]
    metadata = json.loads(event["metadata_json"])
    assert metadata == {
        "reason_code": "origin_run_ended",
        "signal_kind": "implementation_pr_observed",
        "source": "open-pr",
    }
    # AC2 explicitly requires round-tripping through a real SELECT.
    row = conn.execute(
        "SELECT metadata_json FROM events WHERE id = ?", (event["id"],)
    ).fetchone()
    assert json.loads(row["metadata_json"])["reason_code"] == "origin_run_ended"


def test_given_origin_run_not_found_when_signal_applied_then_no_event_is_persisted(conn):
    before = mutation_counts(conn)

    result = signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="closed-session")

    assert result == {"disposition": "deferred", "reason_code": "unbound"}
    assert mutation_counts(conn) == before
    assert _events_with_type(conn, "workflow:origin_resolution_failed") == []
