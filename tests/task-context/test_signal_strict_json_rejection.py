"""AC2: strict JSON parser does not coerce duplicate evidence into a signal."""

from __future__ import annotations

import task_context_workflow_signals as signals


def test_given_duplicate_evidence_json_member_when_public_signal_is_parsed_then_no_normalized_payload_is_produced():
    payload, outcome = signals.parse_public_signal(
        '{"signal_kind":"implementation_pr_observed","source":"open-pr",'
        '"source_schema_version":"v1","evidence":{"repo":"squne121/loop-protocol",'
        '"issue_number":20,"pr_number":21,"pr_number":22}}'
    )
    assert payload is None
    assert outcome == {"disposition": "rejected_evidence", "reason_code": "DUPLICATE_EVIDENCE_MEMBER"}
