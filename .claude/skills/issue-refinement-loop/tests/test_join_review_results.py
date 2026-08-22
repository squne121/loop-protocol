#!/usr/bin/env python3
"""Tests for join_review_results.py (Issue #2296 AC2).

Covers the effective_verdict decision table:
- deterministic needs-fix always wins (semantic gate never evaluated)
- deterministic approve + semantic clear -> approve
- deterministic approve + severity blocker/high (open) -> needs-fix
- deterministic approve + severity medium/low only -> approve + warning
- deterministic approve + severity blocker/high but owner_disposition
  accepted/deferred -> approve + warning
- semantic_review_applicable=false (semantic_assessment=not_required) -> skip/approve
- transport missing/stale/error x best_effort (with retry) -> approve + warning
- transport missing/stale/error x required -> human_judgment_required
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import join_review_results as joiner  # noqa: E402


def test_deterministic_needs_fix_wins_regardless_of_semantic():
    result = joiner.join_review_results(
        deterministic_verdict="needs-fix",
        semantic_assessment="clear",
        transport_status="ok",
    )
    assert result["effective_verdict"] == "needs-fix"


def test_semantic_not_required_short_circuits_to_approve():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="not_required",
        transport_status="not_required",
    )
    assert result["effective_verdict"] == "approve"
    assert result["warnings"] == []


def test_semantic_clear_and_transport_ok_is_approve():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="clear",
        transport_status="ok",
    )
    assert result["effective_verdict"] == "approve"


def test_open_blocker_finding_routes_to_rewrite():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="ok",
        findings=[{"severity": "blocker", "summary": "structural gap"}],
    )
    assert result["effective_verdict"] == "needs-fix"


def test_open_high_finding_routes_to_rewrite():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="ok",
        findings=[{"severity": "high", "summary": "design gap"}],
    )
    assert result["effective_verdict"] == "needs-fix"


def test_medium_low_only_findings_are_approve_with_warning():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="ok",
        findings=[
            {"severity": "medium", "summary": "nit"},
            {"severity": "low", "summary": "style"},
        ],
    )
    assert result["effective_verdict"] == "approve"
    assert result["warnings"]


def test_owner_accepted_blocker_finding_is_approve_with_warning():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="ok",
        findings=[
            {
                "severity": "blocker",
                "summary": "known tradeoff",
                "owner_disposition": {"status": "accepted", "recorded_by": "owner"},
            }
        ],
    )
    assert result["effective_verdict"] == "approve"
    assert result["warnings"]


def test_owner_deferred_high_finding_is_approve_with_warning():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="ok",
        findings=[
            {
                "severity": "high",
                "summary": "later",
                "owner_disposition": {"status": "deferred", "recorded_by": "owner"},
            }
        ],
    )
    assert result["effective_verdict"] == "approve"


def test_transport_missing_best_effort_falls_back_to_approve_with_warning():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="missing",
        transport_policy="best_effort",
        retry_already_attempted=True,
    )
    assert result["effective_verdict"] == "approve"
    assert result["warnings"]


def test_transport_stale_best_effort_falls_back_to_approve_with_warning():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="stale",
        transport_policy="best_effort",
    )
    assert result["effective_verdict"] == "approve"


def test_transport_error_required_escalates_to_human_judgment():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="error",
        transport_policy="required",
    )
    assert result["effective_verdict"] == "human_judgment_required"


def test_transport_missing_required_escalates_to_human_judgment():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="findings",
        transport_status="missing",
        transport_policy="required",
    )
    assert result["effective_verdict"] == "human_judgment_required"


def test_unknown_transport_policy_raises():
    import pytest

    with pytest.raises(ValueError):
        joiner.join_review_results(
            deterministic_verdict="approve",
            semantic_assessment="findings",
            transport_status="ok",
            transport_policy="strict",
        )


def test_unknown_finding_policy_raises():
    import pytest

    with pytest.raises(ValueError):
        joiner.join_review_results(
            deterministic_verdict="approve",
            semantic_assessment="findings",
            transport_status="ok",
            finding_policy="something_else",
        )


def test_default_policies_are_best_effort_and_route_high_open_to_rewrite():
    result = joiner.join_review_results(
        deterministic_verdict="approve",
        semantic_assessment="not_required",
    )
    assert result["transport_policy"] == "best_effort"
    assert result["finding_policy"] == "route_high_open_to_rewrite"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
