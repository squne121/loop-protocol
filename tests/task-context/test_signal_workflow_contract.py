"""Trusted workflow signal contract regression coverage for Issue #2565."""
from __future__ import annotations

import task_context_service as service
import task_context_workflow_signals as signals


REPO = "squne121/loop-protocol"
SHA64 = "a" * 64
SHA40 = "b" * 40


def _origin(conn, kind="implementation", session="session-1"):
    task = service.create_task(conn, title="workflow")
    activity = service.transition_activity(conn, task["id"], kind)
    binding = service.create_binding(conn)
    run = service.start_execution_run(
        conn,
        run_kind="native_operator",
        task_id=task["id"],
        activity_id=activity["id"],
        binding_id=binding["id"],
        claude_session_id=session,
    )
    service.set_binding_session(conn, binding["id"], session, execution_run_id=run["id"])
    return task, activity, binding, run


def _implementation_payload():
    return {
        "signal_kind": "implementation_pr_observed",
        "source": "open-pr",
        "source_schema_version": "v1",
        "evidence": {"repo": REPO, "issue_number": 20, "pr_number": 21},
    }


def test_given_valid_implementation_fact_when_applied_then_claims_attach_atomically_and_fact_is_idempotent(conn):
    task, activity, _, _ = _origin(conn)
    result = signals.apply_workflow_signal(conn, _implementation_payload(), origin_session_id="session-1")
    assert result["disposition"] == "applied"
    assert service.find_live_claim(conn, REPO, "issue", 20)["task_id"] == task["id"]
    assert service.find_live_claim(conn, REPO, "pr", 21)["task_id"] == task["id"]
    assert service.get_activity(conn, activity["id"])["status"] == "ACTIVE"
    duplicate = signals.apply_workflow_signal(conn, _implementation_payload(), origin_session_id="session-1")
    assert duplicate["disposition"] == "duplicate_noop"


def test_given_unbound_origin_when_fact_applied_then_it_is_deferred_before_dedupe_or_claim_mutation(conn):
    _origin(conn)
    result = signals.apply_workflow_signal(conn, _implementation_payload(), origin_session_id="closed-session")
    assert result == {"disposition": "deferred", "reason_code": "unbound"}
    assert service.find_live_claim(conn, REPO, "issue", 20) is None


def test_given_forbidden_identity_or_bad_evidence_when_validated_then_rejection_is_non_mutating(conn):
    _origin(conn)
    forbidden = dict(_implementation_payload(), task_id="caller-picked")
    assert signals.apply_workflow_signal(conn, forbidden, origin_session_id="session-1")["reason_code"] == "FORBIDDEN_CALLER_IDENTITY"
    invalid = _implementation_payload()
    invalid["evidence"] = {"repo": REPO, "issue_number": "20", "pr_number": 21}
    assert signals.apply_workflow_signal(conn, invalid, origin_session_id="session-1")["disposition"] == "rejected_evidence"
    assert service.find_live_claim(conn, REPO, "issue", 20) is None


def test_given_accepted_merge_when_cleanup_begins_and_completes_then_cleanup_pending_is_derived(conn):
    task, _, _, _ = _origin(conn)
    assert signals.apply_workflow_signal(conn, _implementation_payload(), origin_session_id="session-1")["disposition"] == "applied"
    merged = {
        "signal_kind": "pr_merged_observed",
        "source": "post-merge-cleanup",
        "source_schema_version": "v1",
        "evidence": {"repo": REPO, "issue_number": 20, "pr_number": 21, "merge_commit_oid": SHA40},
    }
    assert signals.apply_workflow_signal(conn, merged, origin_session_id="session-1")["disposition"] == "applied"
    assert signals.cleanup_pending_for_task(conn, task["id"])
    lifecycle = signals.begin_cleanup_lifecycle(conn, origin_session_id="session-1", repo=REPO, issue_number=20, pr_number=21, merge_identity=SHA40)
    assert lifecycle["disposition"] == "selected"
    completed = {
        "signal_kind": "cleanup_completed",
        "source": "post-merge-cleanup",
        "source_schema_version": "v1",
        "evidence": {"repo": REPO, "issue_number": 20, "pr_number": 21, "merge_identity": SHA40},
    }
    assert signals.apply_workflow_signal(conn, completed, origin_session_id="session-1")["disposition"] == "applied"
    assert not signals.cleanup_pending_for_task(conn, task["id"])


def test_given_duplicate_public_member_when_strict_parser_runs_then_envelope_rejection_is_reported():
    raw = '{"signal_kind":"implementation_pr_observed","signal_kind":"implementation_pr_observed","source":"open-pr","source_schema_version":"v1","evidence":{}}'
    _, rejection = signals.parse_public_signal(raw)
    assert rejection == {"disposition": "rejected_envelope", "reason_code": "DUPLICATE_TOP_LEVEL_MEMBER"}
