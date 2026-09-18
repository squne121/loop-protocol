"""AC2: exact public-envelope rejection taxonomy and non-mutation."""

from __future__ import annotations

import task_context_workflow_signals as signals
from workflow_signal_test_support import create_origin, implementation_payload, mutation_counts


def test_given_invalid_envelope_shape_when_applied_then_fixed_rejected_envelope_reason_is_non_mutating(
    conn,
):
    create_origin(conn)
    valid = implementation_payload()
    cases = (
        (None, "NON_OBJECT_ROOT"),
        (dict(valid, extra="x"), "UNKNOWN_TOP_LEVEL_FIELD"),
        (dict(valid, source=1), "WRONG_TOP_LEVEL_TYPE"),
        (dict(valid, signal_kind="unknown"), "UNKNOWN_SIGNAL_KIND"),
        (dict(valid, source="unknown"), "UNKNOWN_SOURCE"),
        (dict(valid, source="post-merge-cleanup"), "SOURCE_SIGNAL_MISMATCH"),
        (dict(valid, source_schema_version="v2"), "UNSUPPORTED_SCHEMA_VERSION"),
        (dict(valid, task_id="caller-selected"), "FORBIDDEN_CALLER_IDENTITY"),
    )
    for payload, reason in cases:
        before = mutation_counts(conn)
        assert signals.apply_workflow_signal(conn, payload, origin_session_id="session-1") == {
            "disposition": "rejected_envelope",
            "reason_code": reason,
        }
        assert mutation_counts(conn) == before


def test_given_malformed_or_duplicate_json_when_parsed_then_decoder_rejects_before_schema_validation():
    malformed, malformed_outcome = signals.parse_public_signal("{")
    duplicate, duplicate_outcome = signals.parse_public_signal(
        '{"signal_kind":"implementation_pr_observed","signal_kind":"x","source":"open-pr","source_schema_version":"v1","evidence":{}}'
    )
    assert malformed is None
    assert malformed_outcome == {"disposition": "rejected_envelope", "reason_code": "MALFORMED_JSON"}
    assert duplicate is None
    assert duplicate_outcome == {"disposition": "rejected_envelope", "reason_code": "DUPLICATE_TOP_LEVEL_MEMBER"}
