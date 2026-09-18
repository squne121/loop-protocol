"""AC8/AC9: cleanup selection only resumes an eligible nonterminal instance."""

from __future__ import annotations

import task_context_service as service
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


def _accepted_cleanup(conn):
    task, _, _, _ = create_origin(conn)
    assert (
        signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-1")["disposition"]
        == "applied"
    )
    assert (
        signals.apply_workflow_signal(conn, merged_payload(), origin_session_id="session-1")["disposition"] == "applied"
    )
    selected = signals.begin_cleanup_lifecycle(
        conn, origin_session_id="session-1", repo=REPO, issue_number=20, pr_number=21, merge_identity=SHA40
    )
    assert selected["disposition"] == "selected"
    return task, selected["activity_id"]


def test_given_active_bound_cleanup_when_lifecycle_replayed_then_same_instance_is_selected_without_redispatch_state(
    conn,
):
    task, cleanup_id = _accepted_cleanup(conn)
    before = mutation_counts(conn)
    result = signals.begin_cleanup_lifecycle(
        conn, origin_session_id="session-1", repo=REPO, issue_number=20, pr_number=21, merge_identity=SHA40
    )
    assert result == {
        "disposition": "selected",
        "reason_code": "CLEANUP_ALREADY_SELECTED",
        "task_id": task["id"],
        "activity_id": cleanup_id,
    }
    assert mutation_counts(conn) == before


def test_given_done_bound_cleanup_when_lifecycle_replayed_then_terminal_instance_is_not_selected_or_resumed(conn):
    task, cleanup_id = _accepted_cleanup(conn)
    assert (
        signals.apply_workflow_signal(conn, cleanup_completed_payload(), origin_session_id="session-1")["disposition"]
        == "applied"
    )
    before = mutation_counts(conn)

    result = signals.begin_cleanup_lifecycle(
        conn, origin_session_id="session-1", repo=REPO, issue_number=20, pr_number=21, merge_identity=SHA40
    )

    assert result == {"disposition": "duplicate_noop", "reason_code": "activity_terminal", "task_id": task["id"]}
    assert mutation_counts(conn) == before
    assert service.get_activity(conn, cleanup_id)["status"] == "DONE"
