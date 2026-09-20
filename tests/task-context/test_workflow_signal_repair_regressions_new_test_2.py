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


def test_given_other_task_live_claim_without_accepted_event_when_activity_missing_then_conflict_is_non_mutating(conn):
    """AC3 (PR #2692 OWNER review, comment
    https://github.com/squne121/loop-protocol/pull/2692#issuecomment-5749549802):
    Task A holds a live claim on Issue #20 (via an accepted
    ``implementation_pr_observed`` fact for PR #22). Task B replays a novel
    dedupe key (PR #21, never accepted by anyone) for the same Issue while
    Task B has no implementation Activity at all (still in ``refine``).
    Before this fix, ``implementation is None`` returned
    ``deferred``/``activity_missing`` before Task A's live-claim ownership
    of Issue #20 was ever checked, so this test fails against that code."""
    task_a, _, _, _ = create_origin(conn, session="session-a")
    applied_a = signals.apply_workflow_signal(
        conn, implementation_payload(issue_number=20, pr_number=22), origin_session_id="session-a"
    )
    assert applied_a["disposition"] == "applied"

    task_b, _, _, _ = create_origin(conn, kind="refine", session="session-b")
    assert task_a["id"] != task_b["id"]
    before = _db_snapshot(conn)

    # Task B (no implementation Activity) replays a novel dedupe key (PR
    # #21) for the Issue that Task A already holds a live claim on.
    result = signals.apply_workflow_signal(
        conn, implementation_payload(issue_number=20, pr_number=21), origin_session_id="session-b"
    )

    assert result == {"disposition": "conflict", "reason_code": "FACT_TASK_IDENTITY_CONFLICT"}
    assert _db_snapshot(conn) == before
