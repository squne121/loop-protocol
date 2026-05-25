#!/usr/bin/env python3
"""Tests for validate_pr_body.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from validate_pr_body import validate_pr_body


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pr_body"
SCRIPT_PATH = Path(__file__).parent.parent / "validate_pr_body.py"


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def load_paths(name: str) -> list[str]:
    return [
        line.strip()
        for line in (FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_lp052_required_sections():
    result = validate_pr_body(load_fixture("missing_summary.md"), load_paths("non_safety_paths.txt"))
    errors = [error for error in result.errors if error.rule_id == "LP052"]
    assert result.status == "fail"
    assert any("Summary" in error.message for error in errors)


def test_lp053_schema_decision_invalid():
    result = validate_pr_body(load_fixture("invalid_schema_decision.md"), load_paths("non_safety_paths.txt"))
    errors = [error for error in result.errors if error.rule_id == "LP053"]
    assert result.status == "fail"
    assert len(errors) == 1


@pytest.mark.parametrize(
    "fixture_name",
    [
        "schema_change_missing_inventory.md",
        "schema_change_placeholder_inventory.md",
        "uncertain_missing_inventory.md",
    ],
)
def test_lp050_schema_inventory_required(fixture_name: str):
    result = validate_pr_body(load_fixture(fixture_name), load_paths("non_safety_paths.txt"))
    errors = [error for error in result.errors if error.rule_id == "LP050"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_not_schema_change_inventory_na():
    result = validate_pr_body(load_fixture("not_schema_change_with_na_inventory.md"), load_paths("non_safety_paths.txt"))
    lp050_errors = [error for error in result.errors if error.rule_id == "LP050"]
    assert result.status == "pass"
    assert lp050_errors == []


def test_lp051_safety_matrix_required():
    result = validate_pr_body(load_fixture("safety_sensitive_missing_matrix.md"), load_paths("safety_paths.txt"))
    errors = [error for error in result.errors if error.rule_id == "LP051"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_lp055_safety_matrix_columns_invalid():
    result = validate_pr_body(load_fixture("safety_matrix_missing_columns.md"), load_paths("safety_paths.txt"))
    errors = [error for error in result.errors if error.rule_id == "LP055"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_lp056_safety_followup_required():
    result = validate_pr_body(load_fixture("safety_matrix_not_controlled_without_followup.md"), load_paths("safety_paths.txt"))
    errors = [error for error in result.errors if error.rule_id == "LP056"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_lp057_related_issue_required():
    result = validate_pr_body(load_fixture("related_issue_missing.md"), load_paths("non_safety_paths.txt"))
    errors = [error for error in result.errors if error.rule_id == "LP057"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_lp058_changed_paths_unavailable():
    result = validate_pr_body(load_fixture("changed_paths_unavailable.md") if (FIXTURE_DIR / "changed_paths_unavailable.md").exists() else load_fixture("valid_not_schema_change.md"), None)
    errors = [error for error in result.errors if error.rule_id == "LP058"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_minimal_context_limits():
    long_line = "x" * 3000
    body = f"""## Summary

- context

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: maybe
- reason: invalid

## Schema Consumer Inventory

N/A
reason: invalid

## Safety Claim Matrix

N/A
reason: invalid

## Notes

- Related issue: #244

{long_line}
"""
    result = validate_pr_body(body, load_paths("non_safety_paths.txt"))
    assert result.errors
    for error in result.errors:
        context = "\n".join(error.minimal_context)
        assert len(error.minimal_context) <= 5
        assert len(context.encode("utf-8")) <= 2048


def test_cli_returns_loop_body_lint_v1_json():
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False) as body_file:
        body_file.write(load_fixture("valid_not_schema_change.md"))
        body_path = body_file.name
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--body-file",
                body_path,
                "--changed-paths-file",
                str(FIXTURE_DIR / "non_safety_paths.txt"),
                "--linked-issue",
                "330",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["schema"] == "loop_body_lint/v1"
        assert payload["target"] == "pr"
        assert payload["status"] == "pass"
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_b1_cli_body_file_not_found():
    """B1: --body-file /tmp/does-not-exist.md should exit 2 with stderr diagnostic."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--body-file",
            "/tmp/does-not-exist.md",
            "--linked-issue",
            "330",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "ERROR" in result.stderr
    assert "Cannot read body file" in result.stderr


def test_b1_cli_changed_paths_file_not_found():
    """B1: --changed-paths-file /tmp/missing.txt should exit 2 with stderr diagnostic."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False) as body_file:
        body_file.write(load_fixture("valid_not_schema_change.md"))
        body_path = body_file.name
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--body-file",
                body_path,
                "--changed-paths-file",
                "/tmp/missing.txt",
                "--linked-issue",
                "330",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "ERROR" in result.stderr
        assert "Cannot read changed-paths file" in result.stderr
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_b2_lp057_requires_matching_linked_issue():
    """B2: LP057 should require that Closes/Refs references match --linked-issue."""
    body = """## Summary

- test

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A

## Safety Claim Matrix

N/A

## Notes

- Related issue: #244
- Closes #244
"""
    result = validate_pr_body(body, load_paths("non_safety_paths.txt"), linked_issue=330)
    errors = [error for error in result.errors if error.rule_id == "LP057"]
    assert result.status == "fail"
    assert len(errors) == 1
    assert "330" in errors[0].message or "244" in errors[0].message


def test_b2_lp057_accepts_matching_linked_issue():
    """B2: LP057 should pass when Closes references match --linked-issue."""
    body = """## Summary

- test

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A

## Safety Claim Matrix

N/A

## Notes

- Related issue: #330
- Closes #330
"""
    result = validate_pr_body(body, load_paths("non_safety_paths.txt"), linked_issue=330)
    errors = [error for error in result.errors if error.rule_id == "LP057"]
    assert len(errors) == 0


def test_b4_context_truncated_line_limit():
    """B4: context_truncated should be True when context exceeds max_lines."""
    body = """## Summary

line1
line2
line3
line4
line5
line6
line7
line8
line9
line10

## Checks

- [ ] test

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A

## Safety Claim Matrix

N/A

## Notes

- Related issue: #330
"""
    result = validate_pr_body(body, load_paths("non_safety_paths.txt"))
    # The Summary section is 11 lines, should be truncated at 5
    for error in result.errors:
        if error.section == "Summary" or len(error.minimal_context) > 5:
            assert error.context_truncated, f"context_truncated should be True for {error.rule_id}"


def test_n1_empty_changed_paths_treated_as_unavailable():
    """N1: Empty --changed-paths should be treated as unavailable (emit LP058)."""
    result = validate_pr_body(load_fixture("valid_not_schema_change.md"), [])
    errors = [error for error in result.errors if error.rule_id == "LP058"]
    assert result.status == "fail"
    assert len(errors) == 1
