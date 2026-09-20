from test_signal_workflow_contract import REPO, SHA64, _origin
import task_context_service as service
import task_context_workflow_signals as signals


def test_given_refine_activity_and_claim_when_approved_signal_then_only_refine_terminalizes(conn):
    task, refine, _, _ = _origin(conn, kind="refine")
    implementation = service.transition_activity(conn, task["id"], "implementation")
    # Restore refine as a historical active phase for this focused service test.
    conn.execute("UPDATE activities SET status = 'DONE' WHERE id = ?", (implementation["id"],))
    conn.execute("UPDATE activities SET status = 'ACTIVE', ended_at = NULL WHERE id = ?", (refine["id"],))
    service.claim_task_ref(conn, task["id"], REPO, "issue", 20)
    payload = {"signal_kind": "refinement_approved", "source": "issue-refinement-loop", "source_schema_version": "v1", "evidence": {"repo": REPO, "issue_number": 20, "approved_body_sha256": SHA64}}
    assert signals.apply_workflow_signal(conn, payload, origin_session_id="session-1")["disposition"] == "applied"
    assert service.get_activity(conn, refine["id"])["status"] == "DONE"
