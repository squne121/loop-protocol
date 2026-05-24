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
from unittest import mock


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


def _run_fixture_case(fixture_name: str, expected_decision: str, expected_rules: list = None):
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
    _run_fixture_case("no_product_scope", "pass")


def test_ac6_case2_docs_product_impl_fail():
    """AC6: FAIL — allowed_paths に docs/product/** ありで implementation issue かつ spec delta Issue リンクなし（PS001）"""
    _run_fixture_case("docs_product_in_impl_without_spec_delta_fail", "fail", ["PS001"])


def test_ac6_case3_tasks_md_direct_impl_fail():
    """AC6: FAIL — tasks.md を implementation source として指定（PS002）"""
    _run_fixture_case("tasks_md_direct_impl_fail", "fail", ["PS002"])


def test_ac6_case4_tasks_md_staging_pass():
    """AC6: PASS — tasks.md を staging artifact として参照し GitHub Issue 化を要求"""
    _run_fixture_case("tasks_md_staging_ok", "pass")


def test_ac6_case5_specify_canonical_fail():
    """AC6: FAIL — .specify/ artifact を canonical source と明記（PS003）"""
    _run_fixture_case("specify_canonical_fail", "fail", ["PS003"])


def test_ac6_case6_specify_workbench_pass():
    """AC6: PASS — .specify/ artifact を derived workbench と明記"""
    _run_fixture_case("specify_workbench_ok", "pass")


def test_ac6_case7_generated_task_missing_trace_fail():
    """AC6: FAIL — generated task に requirement_id / source_task_id 欠落（PS005）"""
    _run_fixture_case("generated_task_missing_trace_fail", "fail", ["PS005"])


def test_ac6_case8_dependency_not_materialized_fail():
    """AC6: FAIL — dependency が materialize されていない（PS006）"""
    _run_fixture_case("dependency_not_materialized_fail", "human_judgment", ["PS006"])


# Additional Edge Cases

def test_issue_itself_spec_delta_pass():
    """Issue 自体が spec delta Issue として明示（change_kind: spec-delta）"""
    _run_fixture_case("docs_product_spec_issue_ok", "pass")


def test_ps004_diff_rationale_pass():
    """PS004: product spec update に diff_rationale がある"""
    _run_fixture_case("docs_product_with_diff_rationale", "pass")


# B3 新增 test: product spec update without evidence must fail
def test_b3_product_spec_update_without_rationale_fail():
    """B3: FAIL — spec delta Issue without diff_rationale / evidence（PS004）"""
    _run_fixture_case("product_spec_update_without_rationale_fail", "fail", ["PS004"])


# B4 新增 test: tasks.md direct implementation with source_task_id must still fail
def test_b4_tasks_md_direct_with_source_task_id_fail():
    """B4: FAIL — tasks.md direct implementation source with explicit language（PS002）"""
    _run_fixture_case("tasks_md_direct_with_source_task_id_fail", "fail", ["PS002"])


# B6 新増 test: change_kind: update only without product-spec context must fail
def test_b6_change_kind_update_only_fail():
    """B6: FAIL — change_kind: update without product-spec context（PS004）"""
    _run_fixture_case("change_kind_update_only_not_product_spec", "fail", ["PS004"])


# B1 新増 test: gh issue view --json field bug fix (mock test)
def test_b1_gh_api_json_fields():
    """B1: run_gh_api() must use correct --json fields (title,body,labels) without baseRefName"""
    import sys
    from pathlib import Path
    script_dir = Path(__file__).parent.parent
    sys.path.insert(0, str(script_dir))
    from check_product_spec_contract import run_gh_api

    mock_result = mock.Mock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({
        "title": "Test Issue",
        "body": "Test body",
        "labels": []
    })

    with mock.patch("subprocess.run", return_value=mock_result) as mock_run:
        result = run_gh_api(123, "test/repo")

        # Verify the call was made with correct JSON fields
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]

        # Check that baseRefName is NOT in the --json argument
        json_idx = call_args.index("--json")
        json_fields = call_args[json_idx + 1]
        assert "baseRefName" not in json_fields, f"baseRefName should not be in --json fields, got: {json_fields}"
        assert "title" in json_fields
        assert "body" in json_fields
        assert "labels" in json_fields

        assert result is not None
        assert result["title"] == "Test Issue"
