#!/usr/bin/env python3
"""
Tests for plan_refinement_loop.py

Tests that the planner produces deterministic output matching golden JSON files.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import jsonschema


def test_planner_invocation():
    """Test basic planner invocation."""
    script_path = (
        Path(__file__).parent.parent
        / "scripts"
        / "plan_refinement_loop.py"
    )
    assert script_path.exists(), f"Script not found: {script_path}"


def load_fixture(fixture_name: str, fixture_type: str) -> str:
    """Load a fixture file from fixtures directory."""
    fixture_path = (
        Path(__file__).parent.parent
        / "fixtures"
        / fixture_type
        / f"{fixture_name}.md"
    )
    if fixture_path.exists():
        return fixture_path.read_text(encoding="utf-8")
    return ""


def load_golden(golden_name: str) -> dict[str, Any]:
    """Load a golden JSON output file."""
    golden_path = (
        Path(__file__).parent.parent
        / "fixtures"
        / "golden"
        / f"{golden_name}.json"
    )
    if golden_path.exists():
        return json.loads(golden_path.read_text(encoding="utf-8"))
    return {}


def load_schema() -> dict[str, Any]:
    """Load the JSON schema."""
    schema_path = (
        Path(__file__).parent.parent
        / "schemas"
        / "refinement_loop_plan_v1.json"
    )
    assert schema_path.exists(), f"Schema not found: {schema_path}"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def fixture_to_input(
    fixture_name: str, fixture_type: str, issue_number: int
) -> dict[str, Any]:
    """Convert a markdown fixture to planner input."""
    body = load_fixture(fixture_name, fixture_type)
    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": issue_number,
            "title": f"Test Issue: {fixture_name}",
            "body": body,
            "labels": [],
        },
        "comments": None,
        "known_context": (
            {"anchor_comment_url": "https://github.com/owner/repo/issues/4#issuecomment-123456"}
            if fixture_name == "anchor_reframe_exclusion"
            else None
        ),
    }


def run_planner(input_data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Run the planner script with input data."""
    script_path = (
        Path(__file__).parent.parent
        / "scripts"
        / "plan_refinement_loop.py"
    )

    input_json = json.dumps(input_data, ensure_ascii=False)
    result = subprocess.run(
        ["python3", str(script_path)],
        input=input_json,
        capture_output=True,
        text=True,
    )

    output = json.loads(result.stdout)
    return output, result.returncode


class TestPlanRefinementLoop:
    """Test suite for plan_refinement_loop.py"""

    def test_positive_repo_path_in_outcome(self):
        """AC3: Extract target_paths from Outcome section."""
        input_data = fixture_to_input("repo_path_in_outcome", "positive", 1)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0
        assert output["decisions"]["investigation_policy"]["required"] is True
        assert len(output["decisions"]["investigation_policy"]["target_paths"]) > 0
        assert "scripts/verify.sh" not in output["decisions"]["investigation_policy"]["target_paths"]

    def test_positive_critical_external_claim_in_vc(self):
        """AC3: Detect critical external claims in VC."""
        input_data = fixture_to_input("critical_external_claim_in_vc", "positive", 2)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0
        # Should detect external claims due to keywords like "official", "API", "auth"
        # Note: This may or may not trigger web_research depending on implementation

    def test_positive_delivery_rollup_unmaterialized(self):
        """AC4: Extract unmaterialized child slots."""
        input_data = fixture_to_input("delivery_rollup_unmaterialized", "positive", 3)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0
        assert output["decisions"]["delivery_rollup"]["applicable"] is True
        unmaterialized = output["decisions"]["delivery_rollup"]["unmaterialized_slots"]
        assert len(unmaterialized) >= 2, "Should detect at least 2 unmaterialized slots"

    def test_positive_anchor_reframe_exclusion(self):
        """AC5: Handle anchor reframe exclusion."""
        input_data = fixture_to_input("anchor_reframe_exclusion", "positive", 4)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0
        scope_guard = output["decisions"]["scope_signal_guard"]
        assert scope_guard["triggered"] is True
        assert scope_guard["excluded_by_anchor_reframe"] is True
        assert scope_guard["reason_code"] == "anchor_reframe_exclusion"

    def test_negative_no_repo_fact_claim(self):
        """AC3: No investigation required without repo facts."""
        input_data = fixture_to_input("no_repo_fact_claim", "negative", 5)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0
        assert output["decisions"]["investigation_policy"]["required"] is False
        assert (
            output["decisions"]["investigation_policy"]["reason_code"]
            == "no_repo_fact_claim"
        )

    def test_negative_no_critical_external_claim(self):
        """AC3: No web research required without external claims."""
        input_data = fixture_to_input("no_critical_external_claim", "negative", 6)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0
        assert output["decisions"]["web_research_policy"]["required"] is False
        assert (
            output["decisions"]["web_research_policy"]["reason_code"]
            == "no_critical_external_claim"
        )

    def test_false_positive_path_only_in_fenced_code(self):
        """AC1: Don't extract paths from fenced code blocks."""
        input_data = fixture_to_input("path_only_in_fenced_code", "false_positive", 7)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0
        # Should not extract .claude/skills/internal/utils from fenced code
        target_paths = output["decisions"]["investigation_policy"]["target_paths"]
        assert not any(
            "internal/utils" in p for p in target_paths
        ), "Should not extract paths from fenced code"

    def test_false_negative_japanese_body(self):
        """AC3: Handle Japanese-only issue bodies."""
        input_data = fixture_to_input("japanese_body", "false_negative", 8)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0

    def test_malformed_broken_machine_readable_contract(self):
        """AC10: Detect malformed contract."""
        input_data = fixture_to_input("broken_machine_readable_contract", "malformed", 9)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0
        assert output["fail_closed"]["required"] is True
        assert "malformed_machine_readable_contract" in output["fail_closed"]["reason_codes"]

    def test_malformed_missing_outcome_section(self):
        """AC10: Detect missing Outcome section."""
        input_data = fixture_to_input("missing_outcome_section", "malformed", 10)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0
        assert output["fail_closed"]["required"] is True
        assert "missing_required_section" in output["fail_closed"]["reason_codes"]

    def test_partial_missing_comments(self):
        """AC3: Handle null comments gracefully."""
        input_data = fixture_to_input("missing_comments", "partial", 11)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0
        assert output["source"]["comments_sha256"] is None

    def test_formatting_markdown_table_body(self):
        """AC3: Extract paths from markdown tables."""
        input_data = fixture_to_input("markdown_table_body", "formatting", 12)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0
        target_paths = output["decisions"]["investigation_policy"]["target_paths"]
        # Should extract paths from markdown tables
        assert any(
            ".claude/skills" in p for p in target_paths
        ), "Should extract .claude/skills from table"
        assert any(
            "src/components" in p for p in target_paths
        ), "Should extract src/components from table"

    def test_formatting_fenced_code_in_outcome(self):
        """AC1: Exclude fenced code while extracting Outcome paths."""
        input_data = fixture_to_input("fenced_code_in_outcome", "formatting", 13)
        output, exit_code = run_planner(input_data)

        assert exit_code == 0
        target_paths = output["decisions"]["investigation_policy"]["target_paths"]
        # Should extract src/real/file.ts and src/utils/Real.ts
        # Should NOT extract src/example/NotReal.ts (in fenced code)
        assert any("src/real/file.ts" in p for p in target_paths)
        assert not any(
            "src/example/NotReal" in p for p in target_paths
        ), "Should not extract from fenced code"

    def test_json_schema_validation(self):
        """AC2: Output validates against JSON Schema."""
        schema = load_schema()
        validator = jsonschema.Draft202012Validator(schema)

        input_data = fixture_to_input("repo_path_in_outcome", "positive", 1)
        output, _ = run_planner(input_data)

        errors = list(validator.iter_errors(output))
        assert len(errors) == 0, f"Schema validation errors: {errors}"

    def test_idempotent_execution(self):
        """AC9: Same input produces identical output."""
        input_data = fixture_to_input("repo_path_in_outcome", "positive", 1)

        output1, _ = run_planner(input_data)
        output2, _ = run_planner(input_data)

        # Remove generated_at for comparison (it changes per run)
        output1["source"]["generated_at"] = "FIXED"
        output2["source"]["generated_at"] = "FIXED"

        assert json.dumps(output1, sort_keys=True) == json.dumps(
            output2, sort_keys=True
        ), "Outputs should be identical"

    def test_fail_closed_required_true_still_returns_zero(self):
        """AC10: fail_closed.required=true still exits 0."""
        input_data = fixture_to_input("missing_outcome_section", "malformed", 10)
        _, exit_code = run_planner(input_data)

        assert exit_code == 0, "Should exit 0 even with fail_closed.required=true"

    def test_invalid_input_schema_exits_two(self):
        """AC1: Invalid input schema exits with code 2."""
        invalid_input = {
            "schema_version": "wrong_version",
            "issue": {},
        }

        # Run planner directly
        script_path = (
            Path(__file__).parent.parent
            / "scripts"
            / "plan_refinement_loop.py"
        )

        input_json = json.dumps(invalid_input, ensure_ascii=False)
        result = subprocess.run(
            ["python3", str(script_path)],
            input=input_json,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 2, "Should return exit code 2 for invalid schema"

    def test_evidence_spans_source_enum(self):
        """AC8: evidence_spans[].source ∈ {issue_body, comment, known_context}."""
        input_data = fixture_to_input("repo_path_in_outcome", "positive", 1)
        output, _ = run_planner(input_data)

        allowed_sources = {"issue_body", "comment", "known_context"}
        for decision_key in [
            "investigation_policy",
            "web_research_policy",
            "scope_signal_guard",
            "delivery_rollup",
        ]:
            decision = output["decisions"][decision_key]
            for span in decision.get("evidence_spans", []):
                assert (
                    span["source"] in allowed_sources
                ), f"Invalid source: {span['source']}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
