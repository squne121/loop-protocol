"""AC4/AC13: historical phase selection cannot mutate after terminalization."""

from __future__ import annotations

import task_context_service as service
import task_context_workflow_signals as signals
from workflow_signal_test_support import REPO, create_origin, implementation_payload, mutation_counts


def test_given_terminal_implementation_when_pr_observed_then_outcome_precedes_claims_and_events(
    conn,
):
    task, implementation, _, _ = create_origin(conn)
    conn.execute(
        "UPDATE activities SET status = 'DONE', ended_at = ? WHERE id = ?", (service.now_iso(), implementation["id"])
    )
    before = mutation_counts(conn)

    result = signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-1")

    assert result == {"disposition": "duplicate_noop", "reason_code": "activity_terminal", "task_id": task["id"]}
    assert mutation_counts(conn) == before
    assert service.find_live_claim(conn, REPO, "issue", 20) is None
    assert service.find_live_claim(conn, REPO, "pr", 21) is None


def test_given_active_implementation_when_pr_observed_then_only_claims_and_evidence_record_without_terminalizing_phase(
    conn,
):
    _, implementation, _, _ = create_origin(conn)
    result = signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-1")
    assert result["disposition"] == "applied"
    assert service.get_activity(conn, implementation["id"])["status"] == "ACTIVE"
