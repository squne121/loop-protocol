"""
Fixture-based tests for C12 product trace fields structure check.

Tests C12 deterministic check and non-blocking warnings (scope mismatch, VC anti-pattern).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "check_issue_contract.py"
)
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def run_checker(fixture_name: str) -> dict:
    """Run the checker script on a fixture file and return parsed JSON output."""
    fixture_path = FIXTURES_DIR / fixture_name
    assert fixture_path.exists(), f"Fixture file not found: {fixture_path}"

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--file", str(fixture_path), "--json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 1), (
        f"Script exited with unexpected code {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"Script output is not valid JSON: {e}\nstdout: {result.stdout}"
        ) from e


class TestC12ProductTraceFields:
    """C12: Product trace fields structure check."""

    def test_c12_missing_trace_fields_fails(self):
        """GIVEN c12_missing_trace_fields_issue.md (product_spec_id/requirement_id/source_task_id not all present)
        WHEN checker runs THEN C12_product_trace_fields_structure is fail."""
        output = run_checker("c12_missing_trace_fields_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C12_product_trace_fields_structure"] == "fail", (
            f"Expected C12 to fail, got {checks['C12_product_trace_fields_structure']}"
        )
        assert output["verdict"] == "needs-fix"

    def test_c12_not_applicable_issue(self):
        """GIVEN c12_not_applicable_issue.md (no Product Spec Context)
        WHEN checker runs THEN C12_product_trace_fields_structure is n/a."""
        output = run_checker("c12_not_applicable_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C12_product_trace_fields_structure"] == "n/a", (
            f"Expected C12 to be n/a, got {checks['C12_product_trace_fields_structure']}"
        )

    def test_c12_fixture_present(self):
        """GIVEN pass_issue.md WHEN checker runs THEN deterministic_checks has C12."""
        output = run_checker("pass_issue.md")
        checks = output["deterministic_checks"]
        assert "C12_product_trace_fields_structure" in checks, (
            "C12_product_trace_fields_structure not found in deterministic_checks"
        )


class TestVCUntrackFalseNegative:
    """vc_untracked_false_negative_pattern warning."""

    def test_vc_untracked_false_negative_detected(self):
        """GIVEN vc_untracked_false_negative_issue.md (git status | grep -v pattern)
        WHEN checker runs THEN non_blocking_improvements includes vc_untracked_false_negative_pattern."""
        output = run_checker("vc_untracked_false_negative_issue.md")
        warnings = output.get("non_blocking_improvements", [])
        codes = [w.get("code") for w in warnings]
        assert "vc_untracked_false_negative_pattern" in codes, (
            f"Expected vc_untracked_false_negative_pattern in warnings, got codes: {codes}"
        )


class TestVCNegativeGrepWithoutLiteral:
    """vc_negative_grep_without_literal_inventory warning."""

    def test_vc_negative_grep_without_literal_detected(self):
        """GIVEN vc_negative_grep_without_literal_issue.md (deletion + ! grep without literal list)
        WHEN checker runs THEN non_blocking_improvements includes vc_negative_grep_without_literal_inventory."""
        output = run_checker("vc_negative_grep_without_literal_issue.md")
        warnings = output.get("non_blocking_improvements", [])
        codes = [w.get("code") for w in warnings]
        assert "vc_negative_grep_without_literal_inventory" in codes, (
            f"Expected vc_negative_grep_without_literal_inventory in warnings, got codes: {codes}"
        )


class TestC1MissingSectionSentinel:
    """C1 missing section skeleton generation (sentinel marker)."""

    def test_c1_fail_has_blocking_issue(self):
        """GIVEN c1_missing_sections_issue.md (missing AC / VC / etc)
        WHEN checker runs THEN blocking_issues contains C1 failures."""
        output = run_checker("c1_missing_sections_issue.md")
        assert output["verdict"] == "needs-fix"
        blocking = output.get("blocking_issues", [])
        assert len(blocking) > 0, "Expected blocking_issues for missing sections"


class TestNonBlockingWarningsStructure:
    """non_blocking_improvements structure validation."""

    def test_non_blocking_warning_has_required_fields(self):
        """GIVEN a fixture with warnings
        WHEN checker runs THEN each warning has code, severity, evidence, suggested_action fields."""
        output = run_checker("vc_untracked_false_negative_issue.md")
        warnings = output.get("non_blocking_improvements", [])

        for w in warnings:
            assert "code" in w, "Warning missing 'code' field"
            assert "severity" in w, "Warning missing 'severity' field"
            assert "evidence" in w, "Warning missing 'evidence' field"
            assert "suggested_action" in w, "Warning missing 'suggested_action' field"
