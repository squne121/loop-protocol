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


def test_given_task_a_accepted_merge_when_task_b_begins_same_cleanup_identity_then_it_is_a_non_mutating_identity_conflict(
    conn,
):
    task_a, _, _, _ = create_origin(conn, session="session-a")
    assert (
        signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-a")["disposition"]
        == "applied"
    )
    assert (
        signals.apply_workflow_signal(conn, merged_payload(), origin_session_id="session-a")["disposition"] == "applied"
    )
    task_b, activity_b, _, _ = create_origin(conn, session="session-b")
    before = mutation_counts(conn)

    result = signals.begin_cleanup_lifecycle(
        conn, origin_session_id="session-b", repo=REPO, issue_number=20, pr_number=21, merge_identity=SHA40
    )

    assert result == {"disposition": "conflict", "reason_code": "FACT_TASK_IDENTITY_CONFLICT"}
    assert mutation_counts(conn) == before
    assert service.get_activity(conn, activity_b["id"])["status"] == "ACTIVE"
    assert signals.cleanup_pending_for_task(conn, task_a["id"]) is True
    assert signals.cleanup_pending_for_task(conn, task_b["id"]) is False


def test_given_cleanup_already_completed_when_fresh_binding_normal_prompts_same_issue_then_done_implementation_is_not_reselected(
    conn,
):
    """fix_delta Finding 1 (PR #2661 OWNER review): the merge-gap fallback in
    ``_select_activity_for_binding_tx`` must resume the historical
    implementation Activity only while cleanup has NOT yet begun for the
    accepted merge fact. Once cleanup has begun *and completed*, the
    ``workflow:pr_merged_observed`` event remaining in history must not keep
    re-selecting the DONE implementation Activity for a fresh Binding's
    ordinary autobind -- it must fall through to a new native Activity."""
    task, implementation_id = _accepted_cleanup(conn)
    assert (
        signals.apply_workflow_signal(conn, cleanup_completed_payload(), origin_session_id="session-1")["disposition"]
        == "applied"
    )
    assert service.get_activity(conn, implementation_id)["status"] == "DONE"

    # Fresh Binding, ordinary autobind ("normal prompt on the same Issue") --
    # not a cleanup-lifecycle call and not the same Binding/session as above.
    fresh_binding = service.create_binding(conn)

    result = service.bind_target_to_binding(
        conn,
        binding_id=fresh_binding["id"],
        execution_run_id=None,
        repo=REPO,
        ref_kind="issue",
        ref_number=20,
        reason_code="autobind",
    )

    assert result["task_id"] == task["id"]
    assert result["activity_id"] != implementation_id, (
        "a fresh Binding's normal prompt must not re-attach to the DONE implementation Activity"
    )
    resumed = service.get_activity(conn, result["activity_id"])
    assert resumed["kind"] == "native_operator"
    assert resumed["status"] == "ACTIVE"
    # The DONE implementation Activity itself must stay untouched (no
    # resurrection/mutation), and cleanup must stay in its completed state.
    assert service.get_activity(conn, implementation_id)["status"] == "DONE"
