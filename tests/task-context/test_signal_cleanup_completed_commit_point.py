"""AC9/AC13: cleanup completion resolves the service-owned bound instance."""

from __future__ import annotations

import task_context_workflow_signals as signals
from workflow_signal_test_support import (
    REPO,
    SHA40,
    cleanup_completed_payload,
    create_origin,
    implementation_payload,
    merged_payload,
    mutation_counts,
)


def test_given_no_cleanup_instance_after_merge_when_completion_arrives_then_it_is_named_deferred_and_non_mutating(
    conn,
):
    _, _, _, _ = create_origin(conn)
    assert (
        signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-1")["disposition"]
        == "applied"
    )
    assert (
        signals.apply_workflow_signal(conn, merged_payload(), origin_session_id="session-1")["disposition"] == "applied"
    )
    before = mutation_counts(conn)
    result = signals.apply_workflow_signal(conn, cleanup_completed_payload(), origin_session_id="session-1")
    assert result == {"disposition": "deferred", "reason_code": "CLEANUP_NOT_ELIGIBLE"}
    assert mutation_counts(conn) == before


def test_given_bound_cleanup_and_unbound_origin_when_completion_arrives_then_origin_precedence_defers_before_mutation(
    conn,
):
    task, _, _, _ = create_origin(conn)
    assert (
        signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-1")["disposition"]
        == "applied"
    )
    assert (
        signals.apply_workflow_signal(conn, merged_payload(), origin_session_id="session-1")["disposition"] == "applied"
    )
    assert (
        signals.begin_cleanup_lifecycle(
            conn, origin_session_id="session-1", repo=REPO, issue_number=20, pr_number=21, merge_identity=SHA40
        )["disposition"]
        == "selected"
    )
    before = mutation_counts(conn)
    assert signals.apply_workflow_signal(conn, cleanup_completed_payload(), origin_session_id="closed-session") == {
        "disposition": "deferred",
        "reason_code": "unbound",
    }
    assert mutation_counts(conn) == before
    assert signals.cleanup_pending_for_task(conn, task["id"])
