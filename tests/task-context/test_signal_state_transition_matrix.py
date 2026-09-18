"""AC13: cleanup completion state matrix is deterministic and non-mutating."""

from __future__ import annotations

import task_context_workflow_signals as signals
from workflow_signal_test_support import (
    cleanup_completed_payload,
    create_origin,
    implementation_payload,
    mutation_counts,
)


def test_given_no_matching_merge_when_cleanup_completion_arrives_then_out_of_order_conflict_precedes_lookup(
    conn,
):
    create_origin(conn)
    assert (
        signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-1")["disposition"]
        == "applied"
    )
    before = mutation_counts(conn)
    result = signals.apply_workflow_signal(conn, cleanup_completed_payload(), origin_session_id="session-1")
    assert result == {"disposition": "conflict", "reason_code": "OUT_OF_ORDER_SIGNAL"}
    assert mutation_counts(conn) == before


def test_given_terminal_cleanup_replay_when_signal_arrives_then_same_fact_noop_preserves_state(
    conn,
):
    # Completion's full happy path is independently covered by the commit-point
    # test; this focused case verifies the terminal replay row in the table.
    import task_context_service as service
    from workflow_signal_test_support import REPO, SHA40, merged_payload

    task, _, _, _ = create_origin(conn)
    assert (
        signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-1")["disposition"]
        == "applied"
    )
    assert (
        signals.apply_workflow_signal(conn, merged_payload(), origin_session_id="session-1")["disposition"] == "applied"
    )
    cleanup = signals.begin_cleanup_lifecycle(
        conn, origin_session_id="session-1", repo=REPO, issue_number=20, pr_number=21, merge_identity=SHA40
    )
    assert (
        signals.apply_workflow_signal(conn, cleanup_completed_payload(), origin_session_id="session-1")["disposition"]
        == "applied"
    )
    before = mutation_counts(conn)
    replay = signals.apply_workflow_signal(conn, cleanup_completed_payload(), origin_session_id="session-1")
    assert replay == {"disposition": "duplicate_noop", "reason_code": "SAME_TASK_SAME_FACT", "task_id": task["id"]}
    assert mutation_counts(conn) == before
    assert service.get_activity(conn, cleanup["activity_id"])["status"] == "DONE"
