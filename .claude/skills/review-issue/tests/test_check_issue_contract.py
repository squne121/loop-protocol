"""
Fixture-based tests for check_issue_contract.py

Tests the C1-C11 deterministic checks using fixture Markdown files.
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


class TestPassCase:
    """GIVEN a well-formed implementation issue fixture WHEN checker runs THEN all checks pass."""

    def test_verdict_is_approve(self):
        """GIVEN pass_issue.md WHEN checker runs THEN verdict is approve."""
        output = run_checker("pass_issue.md")
        assert output["verdict"] == "approve", (
            f"Expected approve, got {output['verdict']}. "
            f"blocking_issues: {output.get('blocking_issues')}"
        )

    def test_all_deterministic_checks_pass(self):
        """GIVEN pass_issue.md WHEN checker runs THEN all C1-C11 are pass or n/a."""
        output = run_checker("pass_issue.md")
        checks = output["deterministic_checks"]
        allowed = {"pass", "n/a", "warn"}
        for check_id, result in checks.items():
            assert result in allowed, (
                f"Check {check_id} has unexpected result '{result}' for pass fixture"
            )

    def test_has_11_deterministic_checks(self):
        """GIVEN any fixture WHEN checker runs THEN deterministic_checks has exactly 11 keys."""
        output = run_checker("pass_issue.md")
        checks = output["deterministic_checks"]
        assert len(checks) == 11, (
            f"Expected 11 deterministic checks, got {len(checks)}: {list(checks.keys())}"
        )

    def test_no_blocking_issues(self):
        """GIVEN pass_issue.md WHEN checker runs THEN blocking_issues is empty."""
        output = run_checker("pass_issue.md")
        assert output["blocking_issues"] == [], (
            f"Expected no blocking issues, got: {output['blocking_issues']}"
        )


class TestC1Fail:
    """GIVEN a fixture missing required sections WHEN checker runs THEN C1 fails."""

    def test_verdict_is_needs_fix(self):
        """GIVEN c1_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c1_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )

    def test_c1_fails(self):
        """GIVEN c1_fail_issue.md WHEN checker runs THEN C1_required_sections is fail."""
        output = run_checker("c1_fail_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C1_required_sections"] == "fail", (
            f"Expected C1 to fail, got {checks['C1_required_sections']}"
        )

    def test_c1_blocking_issue_message(self):
        """GIVEN c1_fail_issue.md WHEN checker runs THEN blocking_issues contains C1 message."""
        output = run_checker("c1_fail_issue.md")
        blocking = output["blocking_issues"]
        assert any("必須セクション" in msg or "Runtime Verification" in msg for msg in blocking), (
            f"Expected C1 blocking message in {blocking}"
        )


class TestC7Fail:
    """GIVEN a fixture with workflow skills in Required Skills WHEN checker runs THEN C7 fails."""

    def test_verdict_is_needs_fix(self):
        """GIVEN c7_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c7_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )

    def test_c7_fails(self):
        """GIVEN c7_fail_issue.md WHEN checker runs THEN C7_required_skills_semantics is fail."""
        output = run_checker("c7_fail_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C7_required_skills_semantics"] == "fail", (
            f"Expected C7 to fail, got {checks['C7_required_skills_semantics']}"
        )

    def test_c7_blocking_message_mentions_workflow_skill(self):
        """GIVEN c7_fail_issue.md WHEN checker runs THEN blocking_issues mentions workflow skill."""
        output = run_checker("c7_fail_issue.md")
        blocking = output["blocking_issues"]
        assert any("ワークフロースキル" in msg or "implement-issue" in msg for msg in blocking), (
            f"Expected workflow skill message in {blocking}"
        )


class TestC9Fail:
    """GIVEN a fixture missing Runtime Verification Applicability WHEN checker runs THEN C9 fails."""

    def test_verdict_is_needs_fix(self):
        """GIVEN c9_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c9_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )

    def test_c9_fails(self):
        """GIVEN c9_fail_issue.md WHEN checker runs THEN C9_runtime_applicability_present is fail."""
        output = run_checker("c9_fail_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C9_runtime_applicability_present"] == "fail", (
            f"Expected C9 to fail, got {checks['C9_runtime_applicability_present']}"
        )

    def test_c9_blocking_message(self):
        """GIVEN c9_fail_issue.md WHEN checker runs THEN blocking_issues contains C9 message."""
        output = run_checker("c9_fail_issue.md")
        blocking = output["blocking_issues"]
        assert any("Runtime Verification" in msg or "Applicability" in msg for msg in blocking), (
            f"Expected C9 blocking message in {blocking}"
        )


class TestC11Fail:
    """GIVEN a fixture with decision: immediate but no runtime-verification tags WHEN checker runs THEN C11 fails."""

    def test_verdict_is_needs_fix(self):
        """GIVEN c11_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c11_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )

    def test_c11_fails(self):
        """GIVEN c11_fail_issue.md WHEN checker runs THEN C11_decision_tag_consistency is fail."""
        output = run_checker("c11_fail_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C11_decision_tag_consistency"] == "fail", (
            f"Expected C11 to fail, got {checks['C11_decision_tag_consistency']}"
        )

    def test_c11_blocking_message(self):
        """GIVEN c11_fail_issue.md WHEN checker runs THEN blocking_issues contains C11 message."""
        output = run_checker("c11_fail_issue.md")
        blocking = output["blocking_issues"]
        assert any("immediate" in msg or "runtime-verification" in msg for msg in blocking), (
            f"Expected C11 blocking message in {blocking}"
        )


class TestJsonOutputStructure:
    """GIVEN any fixture WHEN checker runs with --json THEN output matches expected schema."""

    def test_json_has_required_keys(self):
        """GIVEN pass_issue.md WHEN checker runs THEN JSON has verdict, deterministic_checks, etc."""
        output = run_checker("pass_issue.md")
        required_keys = {"verdict", "deterministic_checks", "blocking_issues", "non_blocking_improvements"}
        missing = required_keys - set(output.keys())
        assert not missing, f"JSON output missing keys: {missing}"

    def test_all_c1_to_c11_keys_present(self):
        """GIVEN pass_issue.md WHEN checker runs THEN all C1-C11 keys are in deterministic_checks."""
        output = run_checker("pass_issue.md")
        checks = output["deterministic_checks"]
        expected_keys = {
            "C1_required_sections",
            "C2_stop_conditions_6",
            "C3_ac_checkbox_format",
            "C4_vc_commands_present",
            "C5_ac_vc_number_alignment",
            "C6_no_subjective_phrasing",
            "C7_required_skills_semantics",
            "C8_outcome_concreteness",
            "C9_runtime_applicability_present",
            "C10_deferred_destination_present",
            "C11_decision_tag_consistency",
        }
        missing = expected_keys - set(checks.keys())
        assert not missing, f"deterministic_checks missing keys: {missing}"
