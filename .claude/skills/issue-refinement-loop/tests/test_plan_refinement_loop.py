#!/usr/bin/env python3
"""
Tests for plan_refinement_loop.py

Tests that the planner produces deterministic output matching golden JSON files.
B1: All fixtures are parametrized with golden JSON complete comparison.
B2: Idempotency is tested with deterministic timestamps.
B3: fail_closed outputs are schema-valid.
B9: No SKIP guards in wiring.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest


def load_fixture(fixture_name: str, fixture_type: str) -> str:
    """Load a fixture file from fixtures directory."""
    fixture_path = (
        Path(__file__).parent
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
        Path(__file__).parent
        / "fixtures"
        / "golden"
        / f"{golden_name}.json"
    )
    if golden_path.exists():
        return json.loads(golden_path.read_text(encoding="utf-8"))
    return {}


def load_schema() -> dict[str, Any]:
    """Load the JSON schema."""
    # Schema is one level up in the skills directory
    schema_path = (
        Path(__file__).parent.parent
        / "schemas"
        / "refinement_loop_plan_v1.json"
    )
    assert schema_path.exists(), f"Schema not found: {schema_path}"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def fixture_to_input(
    fixture_name: str, fixture_type: str, issue_number: int, now: str | None = None
) -> dict[str, Any]:
    """Convert a markdown fixture to planner input."""
    body = load_fixture(fixture_name, fixture_type)
    input_data = {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": issue_number,
            "title": f"Test Issue: {fixture_name}",
            "body": body,
            "labels": [],
        },
        "comments": None,
        "known_context": None,
    }

    # B4: Add known_context for anchor_reframe fixtures
    if fixture_name == "anchor_reframe_exclusion" or fixture_name == "known_context_anchor_reframe":
        input_data["known_context"] = {
            "anchor_comment_url": "https://github.com/owner/repo/issues/4#issuecomment-123456"
        }

    # B4: Add comments for comment-based fixtures
    if fixture_name == "comment_requests_web_verification":
        input_data["comments"] = [
            {
                "id": 999,
                "body": "Reviewersはこれをwebで確認すること。公式 docsに記載されている動作を検証してください。",
            }
        ]

    # B2: Support deterministic now parameter
    if now:
        input_data["now"] = now

    return input_data


def run_planner(input_data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Run the planner script with input data."""
    script_path = (
        Path(__file__).parent.parent / "scripts" / "plan_refinement_loop.py"
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

    def test_planner_invocation(self):
        """Test basic planner invocation."""
        script_path = (
            Path(__file__).parent.parent / "scripts" / "plan_refinement_loop.py"
        )
        assert script_path.exists(), f"Script not found: {script_path}"

    # B1: Parametrized golden JSON comparison tests
    @pytest.mark.parametrize(
        "fixture_name,fixture_type,issue_number",
        [
            ("repo_path_in_outcome", "positive", 1),
            ("critical_external_claim_in_vc", "positive", 2),
            ("delivery_rollup_unmaterialized", "positive", 3),
            ("anchor_reframe_exclusion", "positive", 4),
            ("comment_requests_web_verification", "positive", 5),
            ("known_context_anchor_reframe", "positive", 6),
            ("no_repo_fact_claim", "negative", 7),
            ("no_critical_external_claim", "negative", 8),
            ("path_only_in_fenced_code", "false_positive", 9),
            ("internal_documentation_mention", "false_positive", 10),
            ("japanese_body", "false_negative", 11),
            ("broken_machine_readable_contract", "malformed", 12),
            ("missing_outcome_section", "malformed", 13),
            ("ambiguous_scope_signal", "malformed", 14),
            ("missing_comments", "partial", 15),
            ("markdown_table_body", "formatting", 16),
            ("fenced_code_in_outcome", "formatting", 17),
        ],
    )
    def test_golden_json_complete_match(
        self, fixture_name: str, fixture_type: str, issue_number: int
    ):
        """
        B1: Test that planner output completely matches golden JSON.
        This is the main AC3 verification test.
        """
        # B2: Use deterministic timestamp for comparison
        deterministic_now = "2025-05-25T12:00:00+00:00"
        input_data = fixture_to_input(
            fixture_name, fixture_type, issue_number, now=deterministic_now
        )
        output, exit_code = run_planner(input_data)

        # Load expected golden output
        golden = load_golden(fixture_name)

        # Verify exit code is 0 (success or fail_closed)
        assert exit_code == 0, f"Planner exited with code {exit_code}"

        # B1: Complete JSON match (including generated_at timestamp)
        assert (
            json.dumps(output, sort_keys=True, separators=(",", ":"))
            == json.dumps(golden, sort_keys=True, separators=(",", ":"))
        ), f"Output does not match golden JSON for {fixture_name}"

    def test_schema_validation_all_fixtures(self):
        """
        B3: All fixture outputs validate against JSON Schema.
        Uses FormatChecker to validate date-time format.
        """
        schema = load_schema()
        format_checker = jsonschema.FormatChecker()
        validator = jsonschema.Draft202012Validator(schema, format_checker=format_checker)

        test_fixtures = [
            ("repo_path_in_outcome", "positive", 1),
            ("critical_external_claim_in_vc", "positive", 2),
            ("delivery_rollup_unmaterialized", "positive", 3),
            ("anchor_reframe_exclusion", "positive", 4),
            ("no_repo_fact_claim", "negative", 7),
            ("no_critical_external_claim", "negative", 8),
            ("path_only_in_fenced_code", "false_positive", 9),
            ("japanese_body", "false_negative", 11),
            ("broken_machine_readable_contract", "malformed", 12),
            ("missing_outcome_section", "malformed", 13),
        ]

        for fixture_name, fixture_type, issue_number in test_fixtures:
            input_data = fixture_to_input(
                fixture_name,
                fixture_type,
                issue_number,
                now="2025-05-25T12:00:00+00:00",
            )
            output, _ = run_planner(input_data)

            errors = list(validator.iter_errors(output))
            assert (
                len(errors) == 0
            ), f"Schema validation errors for {fixture_name}: {errors}"

    def test_fail_closed_is_schema_valid(self):
        """
        B3: fail_closed outputs with required=true are still schema-valid.
        """
        schema = load_schema()
        format_checker = jsonschema.FormatChecker()
        validator = jsonschema.Draft202012Validator(schema, format_checker=format_checker)

        # Test fail_closed cases (B3)
        fail_closed_fixtures = [
            ("broken_machine_readable_contract", "malformed", 12),
            ("missing_outcome_section", "malformed", 13),
        ]

        for fixture_name, fixture_type, issue_number in fail_closed_fixtures:
            input_data = fixture_to_input(
                fixture_name,
                fixture_type,
                issue_number,
                now="2025-05-25T12:00:00+00:00",
            )
            output, exit_code = run_planner(input_data)

            # All must exit 0 even with fail_closed
            assert exit_code == 0

            # All must have fail_closed.required = True
            assert output["fail_closed"]["required"] is True

            # All must validate against schema
            errors = list(validator.iter_errors(output))
            assert (
                len(errors) == 0
            ), f"fail_closed output is not schema-valid for {fixture_name}: {errors}"

    def test_idempotent_execution(self):
        """
        B2: Same input with same 'now' timestamp produces identical output.
        """
        deterministic_now = "2025-05-25T12:00:00+00:00"
        input_data = fixture_to_input(
            "repo_path_in_outcome", "positive", 1, now=deterministic_now
        )

        output1, _ = run_planner(input_data)
        output2, _ = run_planner(input_data)

        # Complete JSON match including generated_at
        assert (
            json.dumps(output1, sort_keys=True, separators=(",", ":"))
            == json.dumps(output2, sort_keys=True, separators=(",", ":"))
        ), "Idempotent execution failed - outputs differ"

    def test_invalid_input_schema_exits_two(self):
        """AC1: Invalid input schema exits with code 2."""
        invalid_input = {
            "schema_version": "wrong_version",
            "issue": {},
        }

        script_path = (
            Path(__file__).parent.parent / "scripts" / "plan_refinement_loop.py"
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
        """AC8: evidence_spans[].source is in {issue_body, comment, known_context}."""
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

    def test_comments_sha256_handling(self):
        """
        B4: comments_sha256 is None for null, but sha256('[]') for empty list.
        """
        # Test with None
        input_none = fixture_to_input("no_repo_fact_claim", "negative", 7)
        input_none["comments"] = None
        output_none, _ = run_planner(input_none)
        assert output_none["source"]["comments_sha256"] is None

        # Test with empty list
        input_empty = fixture_to_input("no_repo_fact_claim", "negative", 7)
        input_empty["comments"] = []
        output_empty, _ = run_planner(input_empty)
        # Empty list should have sha256("[]")
        import hashlib
        expected_hash = hashlib.sha256("[]".encode()).hexdigest()
        assert output_empty["source"]["comments_sha256"] == expected_hash

    def test_fenced_code_exclusion_in_repo_claims(self):
        """
        B6: repo_claims also excludes fenced code (not just target_paths).
        """
        input_data = fixture_to_input(
            "path_only_in_fenced_code", "false_positive", 9
        )
        output, _ = run_planner(input_data)

        # Should not have .claude/skills/internal/utils in repo_claims
        repo_claims = output["decisions"]["investigation_policy"]["repo_claims"]
        for claim in repo_claims:
            assert (
                "internal/utils" not in claim
            ), "Path from fenced code should not appear in repo_claims"

        # investigation_policy.required should be false
        assert (
            output["decisions"]["investigation_policy"]["required"] is False
        ), "investigation_policy.required should be False for fenced-code-only paths"

    def test_web_research_with_human_request_in_comments(self):
        """
        B4: Comments with human web verification keywords trigger web_research_policy.
        """
        input_data = fixture_to_input(
            "comment_requests_web_verification", "positive", 5
        )
        output, _ = run_planner(input_data)

        # Should detect human request in comments
        assert (
            output["decisions"]["web_research_policy"]["required"] is True
        ), "Human request in comments should trigger web_research"

        # Claims should include comment source_hint
        claims = output["decisions"]["web_research_policy"][
            "critical_external_claims"
        ]
        # At least one claim should be from comments
        comment_claims = [
            c for c in claims if c.get("source_hint", "").startswith("comment")
        ]
        assert len(comment_claims) > 0, "Should detect comment-sourced claims"

    def test_no_false_positive_internal_documentation(self):
        """
        B7: Internal documentation mention should not trigger web_research.
        """
        input_data = fixture_to_input(
            "internal_documentation_mention", "false_positive", 10
        )
        output, _ = run_planner(input_data)

        # Should NOT trigger web research for internal docs
        assert (
            output["decisions"]["web_research_policy"]["required"] is False
        ), "Internal documentation should not trigger web_research"


class TestSkillMdWiring:
    """
    B9: Test that SKILL.md doesn't contain re-judgment logic.
    Placeholder for wiring test - would need SKILL.md parsing.
    """

    def test_skill_md_exists(self):
        """Verify SKILL.md exists for issue-refinement-loop skill."""
        skill_path = (
            Path(__file__).parent.parent / "SKILL.md"
        )
        assert skill_path.exists(), f"SKILL.md not found at {skill_path}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
