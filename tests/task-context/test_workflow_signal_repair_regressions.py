"""Regression coverage for PR #2661 workflow-signal lifecycle repairs."""

from __future__ import annotations

import json

import task_context_hook_flows as hook_flows
import task_context_service as service
import task_context_workflow_signals as signals
from workflow_signal_test_support import REPO, SHA40, SHA64, create_origin, implementation_payload, merged_payload


def _db_snapshot(conn):
    tables = ("task_ref_claims", "activities", "events", "projection_outbox")
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
        for table in tables
    }


def _release_live_claims(conn, task_id: str):
    rows = conn.execute(
        "SELECT id FROM task_ref_claims WHERE task_id = ? AND released_at IS NULL ORDER BY claimed_at", (task_id,)
    ).fetchall()
    for row in rows:
        service.release_task_ref_claim(conn, row["id"])


def test_given_ordinary_hook_issue_start_when_refinement_and_pr_workflow_run_then_historical_phases_and_claim_are_connected(conn):
    started = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-1", "claude_session_id": "ordinary-session"}
    )
    bound = hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-1",
            "claude_session_id": "ordinary-session",
            "classification_kind": "EXPLICIT",
            "target_repo": REPO,
            "target_ref_kind": "issue",
            "target_ref_number": 20,
        },
    )
    assert service.get_activity(conn, bound["activity_id"])["kind"] == "refine"
    assert started["binding_id"]

    refined = signals.apply_workflow_signal(
        conn,
        {
            "signal_kind": "refinement_approved",
            "source": "issue-refinement-loop",
            "source_schema_version": "v1",
            "evidence": {"repo": REPO, "issue_number": 20, "approved_body_sha256": SHA64},
        },
        origin_session_id="ordinary-session",
    )
    assert refined["disposition"] == "applied"
    task_id, activity_id, _ = service.get_current_task_activity_for_binding(conn, started["binding_id"])
    assert task_id == bound["task_id"]
    assert service.get_activity(conn, activity_id)["kind"] == "implementation"

    observed = signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="ordinary-session")
    assert observed["disposition"] == "applied"
    assert service.find_live_claim(conn, REPO, "pr", 21)["task_id"] == bound["task_id"]


def test_given_active_task_for_issue_10_when_unrelated_issue_20_pr_21_is_observed_then_conflict_is_non_mutating(conn):
    task, _, _, _ = create_origin(conn)
    service.claim_task_ref(conn, task["id"], REPO, "issue", 10)
    before = _db_snapshot(conn)

    result = signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-1")

    assert result == {"disposition": "conflict", "reason_code": "FACT_TASK_IDENTITY_CONFLICT"}
    assert _db_snapshot(conn) == before
    assert service.find_live_claim(conn, REPO, "issue", 20) is None
    assert service.find_live_claim(conn, REPO, "pr", 21) is None


def test_given_cleanup_started_when_different_issue_prompt_arrives_then_current_cleanup_activity_prevents_rebind(conn):
    task, _, binding, _ = create_origin(conn)
    assert signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-1")["disposition"] == "applied"
    assert signals.apply_workflow_signal(conn, merged_payload(), origin_session_id="session-1")["disposition"] == "applied"
    selected = signals.begin_cleanup_lifecycle(
        conn, origin_session_id="session-1", repo=REPO, issue_number=20, pr_number=21, merge_identity=SHA40
    )
    _, current_activity_id, _ = service.get_current_task_activity_for_binding(conn, binding["id"])
    assert current_activity_id == selected["activity_id"]
    assert service.get_activity(conn, current_activity_id)["kind"] == "cleanup"
    before = _db_snapshot(conn)

    result = hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-1",
            "claude_session_id": "session-1",
            "classification_kind": "EXPLICIT",
            "target_repo": REPO,
            "target_ref_kind": "issue",
            "target_ref_number": 99,
        },
    )

    assert result["reason_code"] == "different_primary_target_active"
    after = _db_snapshot(conn)
    assert {key: value for key, value in after.items() if key != "events"} == {
        key: value for key, value in before.items() if key != "events"
    }
    assert service.get_current_task_activity_for_binding(conn, binding["id"])[0] == task["id"]


def test_given_accepted_merge_before_cleanup_begin_when_fresh_binding_resumes_then_no_native_activity_competes(conn):
    task, implementation, _, _ = create_origin(conn)
    assert signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-1")["disposition"] == "applied"
    assert signals.apply_workflow_signal(conn, merged_payload(), origin_session_id="session-1")["disposition"] == "applied"
    assert signals.cleanup_pending_for_task(conn, task["id"]) is True
    started = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-2", "claude_session_id": "session-2"}
    )

    rebound = hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-2",
            "claude_session_id": "session-2",
            "classification_kind": "EXPLICIT",
            "target_repo": REPO,
            "target_ref_kind": "issue",
            "target_ref_number": 20,
        },
    )

    assert rebound["task_id"] == task["id"]
    _, rebound_activity_id, _ = service.get_current_task_activity_for_binding(conn, started["binding_id"])
    assert rebound_activity_id == implementation["id"]
    assert not conn.execute(
        "SELECT 1 FROM activities WHERE task_id = ? AND kind = 'native_operator'", (task["id"],)
    ).fetchone()
    selected = signals.begin_cleanup_lifecycle(
        conn, origin_session_id="session-2", repo=REPO, issue_number=20, pr_number=21, merge_identity=SHA40
    )
    assert selected["disposition"] == "selected"
    assert service.get_current_task_activity_for_binding(conn, started["binding_id"])[1] == selected["activity_id"]


def test_given_merged_implementation_when_delayed_signal_for_different_issue_same_pr_arrives_then_it_is_identity_conflict_not_activity_terminal(
    conn,
):
    """fix_delta Finding 2 (PR #2661 OWNER review): the ``implementation``
    dedupe key identifies ``repo+PR`` only, not the Issue. Per AC3's
    precedence (claim/Task consistency -> same-fact dedupe -> phase/terminal
    judgment), a same-PR-but-different-Issue replay must be classified as
    ``conflict``/``FACT_TASK_IDENTITY_CONFLICT`` even once the
    implementation Activity has already reached DONE -- it must never be
    misclassified as ``duplicate_noop``/``activity_terminal``."""
    task, implementation, _, _ = create_origin(conn)
    assert (
        signals.apply_workflow_signal(conn, implementation_payload(issue_number=20, pr_number=21), origin_session_id="session-1")[
            "disposition"
        ]
        == "applied"
    )
    assert (
        signals.apply_workflow_signal(conn, merged_payload(issue_number=20, pr_number=21), origin_session_id="session-1")[
            "disposition"
        ]
        == "applied"
    )
    assert service.get_activity(conn, implementation["id"])["status"] == "DONE"
    before = _db_snapshot(conn)

    # A delayed signal for a *different* Issue (#22) on the *same* PR (#21).
    result = signals.apply_workflow_signal(
        conn, implementation_payload(issue_number=22, pr_number=21), origin_session_id="session-1"
    )

    assert result == {"disposition": "conflict", "reason_code": "FACT_TASK_IDENTITY_CONFLICT"}
    assert _db_snapshot(conn) == before
    assert service.get_activity(conn, implementation["id"])["status"] == "DONE"


def test_given_released_claims_when_same_task_or_other_task_replays_accepted_fact_then_no_state_is_written(conn):
    task_a, _, _, _ = create_origin(conn, session="session-a")
    assert signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-a")["disposition"] == "applied"
    _release_live_claims(conn, task_a["id"])
    same_task_before = _db_snapshot(conn)

    same_task = signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-a")

    assert same_task == {"disposition": "duplicate_noop", "reason_code": "SAME_TASK_SAME_FACT", "task_id": task_a["id"]}
    assert _db_snapshot(conn) == same_task_before

    task_b, _, _, _ = create_origin(conn, session="session-b")
    cross_task_before = _db_snapshot(conn)
    cross_task = signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-b")

    assert cross_task == {"disposition": "conflict", "reason_code": "FACT_TASK_IDENTITY_CONFLICT"}
    assert _db_snapshot(conn) == cross_task_before
    assert task_b["id"] != task_a["id"]
