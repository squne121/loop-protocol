#!/usr/bin/env python3
"""
test_loop_state_builder_negative.py

Negative fixture tests for build_loop_state.py (AC9).

Tests:
- required field missing → invalid
- additional property → invalid
- issue_number mismatch → blocked
- iteration regression (negative) → blocked
- unknown verdict → invalid
- scope_signal triggered → captured in loop state
- max_iterations boundary
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SKILL_ROOT = Path(__file__).parent.parent
SCRIPT = SKILL_ROOT / "scripts" / "build_loop_state.py"
FIXTURE_DIR = SKILL_ROOT / "tests" / "fixtures" / "loop_state_builder"
SCHEMA_PATH = SKILL_ROOT / "schemas" / "loop_state.schema.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_builder(
    planner_data: dict | None = None,
    review_data: dict | None = None,
    issue_number: int = 1024,
    iteration: int = 0,
    max_iterations: int = 3,
    extra_args: list[str] | None = None,
    tmp_path: Path | None = None,
    planner_path: Path | None = None,
    review_path: Path | None = None,
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run build_loop_state.py with given data. Returns (result, out_path)."""
    assert tmp_path is not None
    out = tmp_path / "loop_state.json"

    if planner_path is None:
        planner_path = tmp_path / "planner.json"
        planner_data = planner_data or _minimal_planner(issue_number)
        planner_path.write_text(json.dumps(planner_data), encoding="utf-8")

    if review_path is None:
        review_path = tmp_path / "review.json"
        review_data = review_data or _minimal_review("approve")
        review_path.write_text(json.dumps(review_data), encoding="utf-8")

    argv = [
        sys.executable, str(SCRIPT),
        "--planner-result-file", str(planner_path),
        "--review-result-file", str(review_path),
        "--issue-number", str(issue_number),
        "--iteration", str(iteration),
        "--max-iterations", str(max_iterations),
        "--out", str(out),
    ]
    if extra_args:
        argv.extend(extra_args)

    result = subprocess.run(argv, capture_output=True, text=True)
    return result, out


def _minimal_planner(issue_number: int = 1024) -> dict[str, Any]:
    return {
        "schema_version": "refinement_loop_plan/v1",
        "source": {
            "issue_number": issue_number,
            "issue_body_sha256": "a" * 64,
            "comments_sha256": None,
            "known_context_sha256": None,
            "generated_at": "2026-01-01T00:00:00+00:00",
        },
        "decisions": {
            "investigation_policy": {
                "required": False,
                "reason_code": "no_repo_fact_claim",
                "target_paths": [],
                "repo_claims": [],
                "evidence_spans": [],
                "confidence": "deterministic",
            },
            "web_research_policy": {
                "required": False,
                "reason_code": "no_critical_external_claim",
                "critical_external_claims": [],
                "evidence_spans": [],
                "confidence": "deterministic",
            },
            "scope_signal_guard": {
                "triggered": False,
                "reason_code": "no_scope_signal",
                "excluded_by_anchor_reframe": False,
                "evidence_spans": [],
            },
            "delivery_rollup": {
                "applicable": False,
                "unmaterialized_slots": [],
                "evidence_spans": [],
            },
            "follow_up_materialization": {"candidates": []},
        },
        "fail_closed": {"required": False, "reason_codes": [], "human_message": ""},
    }


def _minimal_review(verdict: str) -> dict[str, Any]:
    return {
        "STATUS": "ok",
        "VERDICT": verdict,
        "SUMMARY": "test",
        "BLOCKERS": "0",
        "NEXT_ACTION": "proceed" if verdict == "approve" else "request_changes",
        "MUST_READ": "",
        "EVIDENCE": "",
        "ARTIFACT": "",
    }


# ---------------------------------------------------------------------------
# AC9: negative fixtures
# ---------------------------------------------------------------------------


def test_required_field_missing(tmp_path):
    """AC9: When LOOP_STATE is missing required fields, status=invalid with errors."""
    # Manipulate schema to inject a required field that build_loop_state won't produce.
    # We do this by providing a custom schema with an extra required field.
    bad_schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    bad_schema["required"].append("nonexistent_required_field")
    schema_path = tmp_path / "bad_schema.json"
    schema_path.write_text(json.dumps(bad_schema), encoding="utf-8")

    result, out = run_builder(
        tmp_path=tmp_path,
        extra_args=["--schema-file", str(schema_path)],
    )
    # Should produce invalid status due to schema validation failure
    assert result.returncode != 0
    build_result = json.loads(result.stdout)
    assert build_result["status"] == "invalid"
    assert len(build_result["errors"]) > 0
    # Error should mention the missing field
    all_messages = " ".join(e["message"] for e in build_result["errors"])
    assert "nonexistent_required_field" in all_messages


def test_additional_property(tmp_path):
    """AC9: Schema with additionalProperties: false rejects extra fields in loop_state."""
    # The schema already has additionalProperties: false.
    # We test that the builder's output does NOT add extra fields.
    result, out = run_builder(tmp_path=tmp_path)
    assert result.returncode == 0
    build_result = json.loads(result.stdout)
    assert build_result["status"] == "ok"
    assert build_result["errors"] == []


def test_issue_number_mismatch(tmp_path):
    """AC9: issue_number mismatch between planner artifact and CLI → blocked."""
    planner = _minimal_planner(issue_number=9999)  # Different issue_number
    planner_path = tmp_path / "planner_mismatch.json"
    planner_path.write_text(json.dumps(planner), encoding="utf-8")

    review = _minimal_review("approve")
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")

    result, out = run_builder(
        tmp_path=tmp_path,
        planner_path=planner_path,
        review_path=review_path,
        issue_number=1024,  # CLI says 1024, planner says 9999
    )
    assert result.returncode != 0
    build_result = json.loads(result.stdout)
    assert build_result["status"] == "invalid"
    assert any("issue_number_mismatch" in e["message"] for e in build_result["errors"])


def test_iteration_regression(tmp_path):
    """AC9: Negative iteration → blocked."""
    result, out = run_builder(
        tmp_path=tmp_path,
        iteration=-1,  # negative iteration
    )
    assert result.returncode != 0
    build_result = json.loads(result.stdout)
    assert build_result["status"] == "invalid"
    assert any("iteration_regression" in e["message"] for e in build_result["errors"])


def test_unknown_verdict(tmp_path):
    """AC9: Unknown verdict in review result → blocked/invalid."""
    review = _minimal_review("approve")
    review["VERDICT"] = "unknown_verdict_xyz"  # invalid verdict
    review_path = tmp_path / "review_bad_verdict.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")

    planner = _minimal_planner()
    planner_path = tmp_path / "planner.json"
    planner_path.write_text(json.dumps(planner), encoding="utf-8")

    result, out = run_builder(
        tmp_path=tmp_path,
        planner_path=planner_path,
        review_path=review_path,
    )
    assert result.returncode != 0
    build_result = json.loads(result.stdout)
    assert build_result["status"] == "invalid"
    assert any("unknown_verdict" in e["message"] for e in build_result["errors"])


def test_scope_signal_triggered(tmp_path):
    """AC9: scope_signal triggered fixture produces valid LOOP_STATE with triggered=true."""
    result, out = run_builder(
        planner_path=FIXTURE_DIR / "planner_scope_signal.json",
        review_path=FIXTURE_DIR / "review_scope_signal.json",
        tmp_path=tmp_path,
    )
    # scope_signal is not itself a blocking error — it just sets the field
    assert result.returncode == 0, f"Unexpected failure:\n{result.stdout}\n{result.stderr}"
    build_result = json.loads(result.stdout)
    assert build_result["status"] == "ok"

    loop_state = json.loads(out.read_text(encoding="utf-8"))
    # Scope signal guard should reflect planner's decision
    assert loop_state["scope_signal_guard"]["triggered"] is True
    assert loop_state["scope_signal_guard"]["reason_code"] == "new_in_scope_area"


def test_max_iterations_boundary_at_one(tmp_path):
    """AC9: max_iterations=1 is the minimum valid boundary."""
    result, out = run_builder(
        tmp_path=tmp_path,
        iteration=0,
        max_iterations=1,
    )
    assert result.returncode == 0, f"Unexpected failure:\n{result.stdout}\n{result.stderr}"
    build_result = json.loads(result.stdout)
    assert build_result["status"] == "ok"

    loop_state = json.loads(out.read_text(encoding="utf-8"))
    assert loop_state["max_iterations"] == 1


def test_max_iterations_boundary_large(tmp_path):
    """AC9: max_iterations=10 (larger boundary) is valid."""
    result, out = run_builder(
        tmp_path=tmp_path,
        iteration=0,
        max_iterations=10,
    )
    assert result.returncode == 0
    loop_state = json.loads(out.read_text(encoding="utf-8"))
    assert loop_state["max_iterations"] == 10


def test_missing_planner_file(tmp_path):
    """Negative: Missing planner file returns error status."""
    out = tmp_path / "loop_state.json"
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(_minimal_review("approve")), encoding="utf-8")

    argv = [
        sys.executable, str(SCRIPT),
        "--planner-result-file", str(tmp_path / "nonexistent.json"),
        "--review-result-file", str(review_path),
        "--issue-number", "1024",
        "--iteration", "0",
        "--out", str(out),
    ]
    result = subprocess.run(argv, capture_output=True, text=True)
    assert result.returncode != 0
    if result.stdout.strip():
        build_result = json.loads(result.stdout)
        assert build_result["status"] == "invalid"


def test_missing_review_file(tmp_path):
    """Negative: Missing review file returns error status."""
    out = tmp_path / "loop_state.json"
    planner_path = tmp_path / "planner.json"
    planner_path.write_text(json.dumps(_minimal_planner()), encoding="utf-8")

    argv = [
        sys.executable, str(SCRIPT),
        "--planner-result-file", str(planner_path),
        "--review-result-file", str(tmp_path / "nonexistent.json"),
        "--issue-number", "1024",
        "--iteration", "0",
        "--out", str(out),
    ]
    result = subprocess.run(argv, capture_output=True, text=True)
    assert result.returncode != 0
    if result.stdout.strip():
        build_result = json.loads(result.stdout)
        assert build_result["status"] == "invalid"
