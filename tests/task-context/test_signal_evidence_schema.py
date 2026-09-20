"""AC2/AC12: duplicate and malformed evidence remains a typed non-mutation."""

from __future__ import annotations

import task_context_workflow_signals as signals
from workflow_signal_test_support import create_origin, implementation_payload, mutation_counts


def test_given_duplicate_evidence_member_when_decoded_then_typed_rejection_precedes_ordinary_validation(
    conn,
):
    create_origin(conn)
    raw = (
        '{"signal_kind":"implementation_pr_observed","source":"open-pr",'
        '"source_schema_version":"v1","evidence":{"repo":"squne121/loop-protocol",'
        '"issue_number":20,"issue_number":21,"pr_number":21}}'
    )
    payload, rejection = signals.parse_public_signal(raw)
    assert payload is None
    assert rejection == {"disposition": "rejected_evidence", "reason_code": "DUPLICATE_EVIDENCE_MEMBER"}


def test_given_invalid_strict_evidence_when_signal_applied_then_evidence_rejection_is_non_mutating(conn):
    create_origin(conn)
    payload = implementation_payload()
    payload["evidence"] = {"repo": "not a repository", "issue_number": "20", "pr_number": 0}
    before = mutation_counts(conn)
    assert signals.apply_workflow_signal(conn, payload, origin_session_id="session-1") == {
        "disposition": "rejected_evidence",
        "reason_code": "INVALID_REPOSITORY",
    }
    assert mutation_counts(conn) == before


def test_given_git_suffix_repository_evidence_when_signal_applied_then_rejected_without_mutation(conn):
    create_origin(conn)
    payload = implementation_payload()
    payload["evidence"]["repo"] = "squne121/loop-protocol.git"
    before = mutation_counts(conn)

    assert signals.apply_workflow_signal(conn, payload, origin_session_id="session-1") == {
        "disposition": "rejected_evidence",
        "reason_code": "INVALID_REPOSITORY",
    }
    assert mutation_counts(conn) == before
