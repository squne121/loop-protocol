"""Focused regression for Issue #2690: cross-Task accepted-fact identity
must be judged before local Activity state (missing branch)."""

from __future__ import annotations

import task_context_workflow_signals as signals
from workflow_signal_test_support import create_origin, implementation_payload


def _db_snapshot(conn):
    tables = ("task_ref_claims", "activities", "events", "projection_outbox")
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
        for table in tables
    }


def test_given_cross_task_accepted_fact_when_implementation_activity_missing_then_conflict_is_non_mutating(conn):
    """AC2: an accepted ``implementation_pr_observed`` fact for PR #21 owned
    by Task A must be classified as ``conflict``/``FACT_TASK_IDENTITY_CONFLICT``
    when replayed against Task B, even though Task B has never had an
    implementation Activity at all (still in ``refine``). Before Issue
    #2690's fix, ``implementation is None`` returned
    ``deferred``/``activity_missing`` before the accepted-fact ownership
    check ever ran, so this test fails against that code."""
    task_a, _, _, _ = create_origin(conn, session="session-a")
    assert (
        signals.apply_workflow_signal(conn, implementation_payload(issue_number=20, pr_number=21), origin_session_id="session-a")[
            "disposition"
        ]
        == "applied"
    )

    task_b, _, _, _ = create_origin(conn, kind="refine", session="session-b")
    assert task_a["id"] != task_b["id"]
    before = _db_snapshot(conn)

    # Task B (which has no implementation Activity at all) replays Task A's
    # accepted PR #21 fact.
    result = signals.apply_workflow_signal(
        conn, implementation_payload(issue_number=20, pr_number=21), origin_session_id="session-b"
    )

    assert result == {"disposition": "conflict", "reason_code": "FACT_TASK_IDENTITY_CONFLICT"}
    assert _db_snapshot(conn) == before
