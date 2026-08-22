#!/usr/bin/env python3
"""Tests for semantic_review_trigger.py (Issue #2296 AC2).

Covers each explicit signal's true/false contribution to
``semantic_review_applicable`` independently, and confirms the classifier
performs no before/after body comparison (no ``--previous-body-file``
option exists at all -- P0-2).
"""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "scripts")
)

import semantic_review_trigger as trig  # noqa: E402


def test_all_false_signals_not_applicable():
    """GIVEN all explicit signals are false/zero/empty
    WHEN evaluate_semantic_review_applicable runs
    THEN semantic_review_applicable is False and triggered_by is empty."""
    result = trig.evaluate_semantic_review_applicable({})
    assert result["semantic_review_applicable"] is False
    assert result["triggered_by"] == []


def test_user_requested_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable({"user_requested": True})
    assert result["semantic_review_applicable"] is True
    assert "user_requested" in result["triggered_by"]


def test_semantic_rewrite_requested_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable(
        {"semantic_rewrite_requested": True}
    )
    assert result["semantic_review_applicable"] is True
    assert "semantic_rewrite_requested" in result["triggered_by"]


def test_checker_gap_count_zero_does_not_trigger():
    result = trig.evaluate_semantic_review_applicable({"checker_gap_count": 0})
    assert result["semantic_review_applicable"] is False


def test_checker_gap_count_positive_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable({"checker_gap_count": 1})
    assert result["semantic_review_applicable"] is True
    assert "checker_gap_count" in result["triggered_by"]


def test_heuristic_concern_count_positive_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable(
        {"heuristic_concern_count": 3}
    )
    assert result["semantic_review_applicable"] is True
    assert "heuristic_concern_count" in result["triggered_by"]


def test_severity_tagged_anchor_findings_non_empty_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable(
        {"severity_tagged_anchor_findings": ["P0-1"]}
    )
    assert result["semantic_review_applicable"] is True
    assert "severity_tagged_anchor_findings" in result["triggered_by"]


def test_owner_decision_conflict_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable(
        {"owner_decision_conflict": True}
    )
    assert result["semantic_review_applicable"] is True
    assert "owner_decision_conflict" in result["triggered_by"]


def test_cross_contract_change_schema_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable(
        {"cross_contract_change": {"schema": True}}
    )
    assert result["semantic_review_applicable"] is True
    assert "cross_contract_change" in result["triggered_by"]


def test_cross_contract_change_protocol_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable(
        {"cross_contract_change": {"protocol": True}}
    )
    assert result["semantic_review_applicable"] is True


def test_cross_contract_change_orchestration_triggers_applicable():
    result = trig.evaluate_semantic_review_applicable(
        {"cross_contract_change": {"orchestration": True}}
    )
    assert result["semantic_review_applicable"] is True


def test_cross_contract_change_all_false_does_not_trigger():
    result = trig.evaluate_semantic_review_applicable(
        {
            "cross_contract_change": {
                "schema": False,
                "protocol": False,
                "orchestration": False,
            }
        }
    )
    assert result["semantic_review_applicable"] is False


def test_no_before_after_diff_cli_option_exists():
    """P0-2: the classifier must not implement a before/after comparison
    entrypoint (no --previous-body-file CLI argument)."""
    parser = trig._build_arg_parser()
    dest_names = {action.dest for action in parser._actions}
    assert "previous_body_file" not in dest_names


def test_multiple_signals_all_recorded_in_triggered_by():
    result = trig.evaluate_semantic_review_applicable(
        {"user_requested": True, "checker_gap_count": 2}
    )
    assert result["semantic_review_applicable"] is True
    assert set(result["triggered_by"]) == {"user_requested", "checker_gap_count"}


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
