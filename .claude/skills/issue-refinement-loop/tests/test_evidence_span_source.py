#!/usr/bin/env python3
"""
Test that evidence_spans[].source values are limited to issue_body, comment, known_context.

AC8: Verify that repo file evidence (commit_sha, permalink, excerpt_sha256) are NOT
used in evidence_spans. This ensures the responsibility boundary with #248 REPO_EVIDENCE_REF_V1.
"""

import json
import subprocess
from pathlib import Path
from typing import Any


def load_fixtures() -> list[tuple[str, str]]:
    """Load all fixture filenames."""
    fixtures_dir = Path(__file__).parent.parent / "fixtures"
    fixtures = []

    for category_dir in fixtures_dir.iterdir():
        if category_dir.is_dir() and category_dir.name != "golden":
            for fixture_file in category_dir.glob("*.md"):
                fixtures.append((category_dir.name, fixture_file.stem))

    return sorted(fixtures)


def fixture_to_input(
    fixture_name: str, fixture_type: str, issue_number: int
) -> dict[str, Any]:
    """Convert markdown fixture to input JSON."""
    fixture_path = (
        Path(__file__).parent.parent
        / "fixtures"
        / fixture_type
        / f"{fixture_name}.md"
    )

    body = fixture_path.read_text(encoding="utf-8") if fixture_path.exists() else ""

    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": issue_number,
            "title": f"Test: {fixture_name}",
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


def run_planner(input_data: dict[str, Any]) -> dict[str, Any]:
    """Run planner and return output."""
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

    return json.loads(result.stdout)


class TestEvidenceSpanSource:
    """Test that evidence_spans source enum is correctly limited."""

    @staticmethod
    def check_evidence_spans(output: dict[str, Any]) -> None:
        """Check all evidence spans in output."""
        # Skip if fail_closed.required is true (no decisions to check)
        if output.get("fail_closed", {}).get("required") is True:
            return

        allowed_sources = {"issue_body", "comment", "known_context"}
        forbidden_keys = {"commit_sha", "permalink", "excerpt_sha256", "file_path"}

        for decision_key in [
            "investigation_policy",
            "web_research_policy",
            "scope_signal_guard",
            "delivery_rollup",
        ]:
            decision = output["decisions"].get(decision_key, {})
            for span in decision.get("evidence_spans", []):
                # Check source enum
                assert (
                    span["source"] in allowed_sources
                ), f"Invalid source: {span['source']}. Allowed: {allowed_sources}"

                # Check that forbidden keys are not present
                for forbidden_key in forbidden_keys:
                    assert (
                        forbidden_key not in span
                    ), (
                        f"Forbidden key '{forbidden_key}' found in evidence span."
                        " This should be in REPO_EVIDENCE_REF_V1, not here."
                    )

        # Check follow-up candidates
        for candidate in output["decisions"].get("follow_up_materialization", {}).get("candidates", []):
            span = candidate.get("source_evidence", {})
            assert (
                span.get("source") in allowed_sources
            ), f"Invalid source in follow-up candidate: {span.get('source')}"

            for forbidden_key in forbidden_keys:
                assert (
                    forbidden_key not in span
                ), f"Forbidden key '{forbidden_key}' in follow-up candidate source_evidence"

    def test_all_fixtures_evidence_spans_valid(self):
        """AC8: All fixtures produce valid evidence_spans."""
        fixtures = load_fixtures()

        for issue_no, (fixture_type, fixture_name) in enumerate(fixtures, start=1):
            input_data = fixture_to_input(fixture_name, fixture_type, issue_no)
            output = run_planner(input_data)

            try:
                self.check_evidence_spans(output)
            except AssertionError as e:
                raise AssertionError(
                    f"Fixture {fixture_type}/{fixture_name}: {str(e)}"
                ) from e


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
