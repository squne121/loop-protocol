"""AC3: origin-first service ordering precedes claims, dedupe, and cleanup."""

from __future__ import annotations

import task_context_workflow_signals as signals
from workflow_signal_test_support import REPO, create_origin, implementation_payload, mutation_counts


def test_given_unbound_origin_and_valid_fact_when_applied_then_unbound_defers_before_claim_lookup_or_dedupe(conn):
    create_origin(conn)
    before = mutation_counts(conn)
    assert signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="closed-session") == {
        "disposition": "deferred",
        "reason_code": "unbound",
    }
    assert mutation_counts(conn) == before
    assert signals._claim_for_tx(conn, REPO, "issue", 20) is None


def test_given_same_fact_on_same_origin_when_replayed_then_dedupe_is_named_noop_without_second_event(conn):
    create_origin(conn)
    assert (
        signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-1")["disposition"]
        == "applied"
    )
    before = mutation_counts(conn)
    result = signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id="session-1")
    assert result["disposition"] == "duplicate_noop"
    assert result["reason_code"] == "SAME_TASK_SAME_FACT"
    assert mutation_counts(conn) == before
