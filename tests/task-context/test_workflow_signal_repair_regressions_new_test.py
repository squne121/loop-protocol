"""Focused regression for Issue #2690: cross-Task accepted-fact identity
must be judged before local Activity state (terminal branch)."""

from __future__ import annotations

import task_context_service as service
import task_context_workflow_signals as signals
from workflow_signal_test_support import create_origin, implementation_payload, merged_payload


def _db_snapshot(conn):
    tables = ("task_ref_claims", "activities", "events", "projection_outbox")
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
        for table in tables
    }


def test_given_cross_task_accepted_fact_when_implementation_activity_terminal_then_conflict_is_non_mutating(conn):
    """AC1: an accepted ``implementation_pr_observed`` fact for PR #21 owned
    by Task A must be classified as ``conflict``/``FACT_TASK_IDENTITY_CONFLICT``
    when replayed against Task B, even though Task B's own implementation
    Activity has already reached a terminal (DONE) status. Before Issue
    #2690's fix, local Activity terminal state was evaluated before
    cross-Task fact ownership, misclassifying this as
    ``duplicate_noop``/``activity_terminal`` and this test fails against
    that code (the accepted-fact ownership check never runs before the
    terminal-Activity short-circuit)."""
    task_a, _, _, _ = create_origin(conn, session="session-a")
    assert (
        signals.apply_workflow_signal(conn, implementation_payload(issue_number=20, pr_number=21), origin_session_id="session-a")[
            "disposition"
        ]
        == "applied"
    )

    task_b, implementation_b, _, _ = create_origin(conn, session="session-b")
    assert (
        signals.apply_workflow_signal(conn, implementation_payload(issue_number=30, pr_number=99), origin_session_id="session-b")[
            "disposition"
        ]
        == "applied"
    )
    assert (
        signals.apply_workflow_signal(conn, merged_payload(issue_number=30, pr_number=99), origin_session_id="session-b")[
            "disposition"
        ]
        == "applied"
    )
    assert service.get_activity(conn, implementation_b["id"])["status"] == "DONE"
    assert task_a["id"] != task_b["id"]
    before = _db_snapshot(conn)

    # Task B replays Task A's accepted PR #21 fact while Task B's own
    # implementation Activity is terminal.
    result = signals.apply_workflow_signal(
        conn, implementation_payload(issue_number=20, pr_number=21), origin_session_id="session-b"
    )

    assert result == {"disposition": "conflict", "reason_code": "FACT_TASK_IDENTITY_CONFLICT"}
    assert _db_snapshot(conn) == before
