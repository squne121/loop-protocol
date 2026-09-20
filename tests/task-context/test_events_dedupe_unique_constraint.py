"""AC10: physical event dedupe protects transitions without partial claims."""

from __future__ import annotations

import task_context_service as service
import task_context_workflow_signals as signals
from workflow_signal_test_support import REPO, create_origin, implementation_payload, mutation_counts


def test_given_same_implementation_business_fact_when_applied_twice_then_one_physical_event_and_claim_pair_exist(conn):
    task, _, _, _ = create_origin(conn)
    payload = implementation_payload()
    assert signals.apply_workflow_signal(conn, payload, origin_session_id="session-1")["disposition"] == "applied"
    after_first = mutation_counts(conn)
    duplicate = signals.apply_workflow_signal(conn, payload, origin_session_id="session-1")
    assert duplicate == {"disposition": "duplicate_noop", "reason_code": "SAME_TASK_SAME_FACT", "task_id": task["id"]}
    assert mutation_counts(conn) == after_first
    assert (
        conn.execute("SELECT COUNT(*) FROM events WHERE dedupe_key = ?", (signals.dedupe_key_for(payload),)).fetchone()[
            0
        ]
        == 1
    )
    assert service.find_live_claim(conn, REPO, "issue", 20)["task_id"] == task["id"]
    assert service.find_live_claim(conn, REPO, "pr", 21)["task_id"] == task["id"]


def test_given_claim_owned_by_different_task_when_implementation_fact_arrives_then_conflict_leaves_no_partial_pr_claim(
    conn,
):
    first_task, _, _, _ = create_origin(conn, session="session-1")
    service.claim_task_ref(conn, first_task["id"], REPO, "issue", 20)
    second_task, _, _, _ = create_origin(conn, session="session-2")
    before = mutation_counts(conn)
    result = signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-2")
    assert result == {"disposition": "conflict", "reason_code": "FACT_TASK_IDENTITY_CONFLICT"}
    assert mutation_counts(conn) == before
    assert service.find_live_claim(conn, REPO, "pr", 21) is None
    assert second_task["id"] != first_task["id"]


def test_given_accepted_pr_fact_with_different_issue_when_replayed_then_conflict_precedes_claim_mutation(conn):
    task, _, _, _ = create_origin(conn)
    result = signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-1")
    assert result["disposition"] == "applied"
    changed_issue = implementation_payload(issue_number=22)
    before = mutation_counts(conn)

    result = signals.apply_workflow_signal(conn, changed_issue, origin_session_id="session-1")

    assert result == {"disposition": "conflict", "reason_code": "FACT_TASK_IDENTITY_CONFLICT"}
    assert mutation_counts(conn) == before
    assert service.find_live_claim(conn, REPO, "issue", 22) is None
    assert service.find_live_claim(conn, REPO, "issue", 20)["task_id"] == task["id"]
