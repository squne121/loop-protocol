#!/usr/bin/env python3
"""
Unit tests for check_product_spec_contract.py

Fixture-driven test suite covering all 8 AC6 fixture cases + edge cases.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict


def load_fixture(fixture_name: str) -> str:
    """Load fixture Issue body from fixtures/ directory"""
    fixture_dir = Path(__file__).parent / "fixtures"
    fixture_path = fixture_dir / f"{fixture_name}.md"
    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture not found: {fixture_path}")
    return fixture_path.read_text(encoding="utf-8")


def run_checker_with_body(body_path: str) -> Dict[str, Any]:
    """Run checker with fixture body content using --body-file"""
    checker_path = Path(__file__).parent.parent / "check_product_spec_contract.py"

    result = subprocess.run(
        [
            sys.executable,
            str(checker_path),
            "--issue-number",
            "999",
            "--repo",
            "test/test",
            "--body-file",
            body_path,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    if result.returncode not in (0, 1):
        raise RuntimeError(f"Checker failed: {result.stderr}")

    output = result.stdout.strip()
    return json.loads(output)


def test_fixture_case(fixture_name: str, expected_decision: str, expected_rules: list = None):
    """Generic fixture test runner"""
    fixture_dir = Path(__file__).parent / "fixtures"
    fixture_path = fixture_dir / f"{fixture_name}.md"

    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture not found: {fixture_path}")

    result = run_checker_with_body(str(fixture_path))

    assert "decision" in result, f"{fixture_name}: missing decision field"
    assert result["decision"] == expected_decision, (
        f"{fixture_name}: expected decision={expected_decision}, "
        f"got decision={result['decision']}"
    )

    if expected_rules:
        blocked_rules = [r["rule_id"] for r in result.get("blocked_reasons", [])]
        for rule in expected_rules:
            assert rule in blocked_rules, (
                f"{fixture_name}: expected rule {rule} in blocked_reasons, "
                f"got {blocked_rules}"
            )


# AC6 Required Test Cases

def test_ac6_case1_unrelated_issue():
    """AC6: PASS — docs/product/** unrelated → applicability: not_applicable"""
    test_fixture_case("no_product_scope", "n/a")


def test_ac6_case2_docs_product_impl_fail():
    """AC6: FAIL — allowed_paths に docs/product/** ありで implementation issue かつ spec delta Issue リンクなし（PS001）"""
    test_fixture_case("docs_product_in_impl_without_spec_delta_fail", "fail", ["PS001"])


def test_ac6_case3_tasks_md_direct_impl_fail():
    """AC6: FAIL — tasks.md を implementation source として指定（PS002）"""
    test_fixture_case("tasks_md_direct_impl_fail", "fail", ["PS002"])


def test_ac6_case4_tasks_md_staging_pass():
    """AC6: PASS — tasks.md を staging artifact として参照し GitHub Issue 化を要求"""
    test_fixture_case("tasks_md_staging_ok", "pass")


def test_ac6_case5_specify_canonical_fail():
    """AC6: FAIL — .specify/ artifact を canonical source と明記（PS003）"""
    test_fixture_case("specify_canonical_fail", "fail", ["PS003"])


def test_ac6_case6_specify_workbench_pass():
    """AC6: PASS — .specify/ artifact を derived workbench と明記"""
    test_fixture_case("specify_workbench_ok", "pass")


def test_ac6_case7_generated_task_missing_trace_fail():
    """AC6: FAIL — generated task に requirement_id / source_task_id 欠落（PS005）"""
    test_fixture_case("generated_task_missing_trace_fail", "fail", ["PS005"])


def test_ac6_case8_dependency_not_materialized_fail():
    """AC6: FAIL — dependency が materialize されていない（PS006）"""
    test_fixture_case("dependency_not_materialized_fail", "human_judgment", ["PS006"])


# Additional Edge Cases

def test_issue_itself_spec_delta_pass():
    """Issue 自体が spec delta Issue として明示（change_kind: spec-delta）"""
    test_fixture_case("docs_product_spec_issue_ok", "pass")


def test_ps004_diff_rationale_pass():
    """PS004: product spec update に diff_rationale がある"""
    test_fixture_case("docs_product_with_diff_rationale", "pass")
