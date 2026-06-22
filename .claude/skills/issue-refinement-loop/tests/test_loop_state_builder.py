#!/usr/bin/env python3
"""
test_loop_state_builder.py

Tests for build_loop_state.py (AC1, AC6, AC7, AC8, AC12).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).parent.parent
SCRIPT = SKILL_ROOT / "scripts" / "build_loop_state.py"
FIXTURE_DIR = SKILL_ROOT / "tests" / "fixtures" / "loop_state_builder"
SCHEMA_PATH = SKILL_ROOT / "schemas" / "loop_state.schema.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_builder(
    planner_file: Path,
    review_file: Path,
    issue_number: int,
    iteration: int,
    max_iterations: int = 3,
    out: Path | None = None,
    tmp_path: Path | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run build_loop_state.py and return the CompletedProcess."""
    if out is None and tmp_path is not None:
        out = tmp_path / "loop_state_out.json"
    assert out is not None, "Either out or tmp_path must be provided"
    argv = [
        sys.executable,
        str(SCRIPT),
        "--planner-result-file", str(planner_file),
        "--review-result-file", str(review_file),
        "--issue-number", str(issue_number),
        "--iteration", str(iteration),
        "--max-iterations", str(max_iterations),
        "--out", str(out),
    ]
    if extra_args:
        argv.extend(extra_args)
    return subprocess.run(argv, capture_output=True, text=True)


def load_build_result(result: subprocess.CompletedProcess) -> dict[str, Any]:
    """Parse stdout as JSON build result."""
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# AC1: Script exists and basic approve flow works
# ---------------------------------------------------------------------------


def test_script_exists():
    """AC1: build_loop_state.py script exists."""
    assert SCRIPT.exists(), f"Missing: {SCRIPT}"


def test_fixtures_exist():
    """AC1: fixture files exist."""
    for name in ["planner_approve.json", "review_approve.json"]:
        p = FIXTURE_DIR / name
        assert p.exists(), f"Missing fixture: {p}"


def test_build_basic_approve(tmp_path):
    """AC1: Basic approve flow builds a valid LOOP_STATE_V1."""
    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=FIXTURE_DIR / "planner_approve.json",
        review_file=FIXTURE_DIR / "review_approve.json",
        issue_number=1024,
        iteration=0,
        out=out,
    )
    assert result.returncode == 0, f"Build failed:\n{result.stdout}\n{result.stderr}"
    build_result = load_build_result(result)
    assert build_result["status"] == "ok"
    assert build_result["schema"] == "LOOP_STATE_BUILD_RESULT_V1"
    assert build_result["loop_state_sha256"] is not None
    assert out.exists(), "Output file was not created"

    loop_state = json.loads(out.read_text(encoding="utf-8"))
    assert loop_state["schema_version"] == "loop_state/v1"
    assert loop_state["issue_number"] == 1024
    assert loop_state["iteration"] == 0
    assert loop_state["last_verdict"] == "approve"


def test_build_basic_needs_fix(tmp_path):
    """AC1: needs_fix flow builds a valid LOOP_STATE_V1 with last_verdict=needs-fix."""
    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=FIXTURE_DIR / "planner_needs_fix.json",
        review_file=FIXTURE_DIR / "review_needs_fix.json",
        issue_number=1024,
        iteration=1,
        out=out,
    )
    assert result.returncode == 0, f"Build failed:\n{result.stdout}\n{result.stderr}"
    build_result = load_build_result(result)
    assert build_result["status"] == "ok"

    loop_state = json.loads(out.read_text(encoding="utf-8"))
    assert loop_state["last_verdict"] == "needs-fix"
    assert loop_state["iteration"] == 1


def test_output_has_required_fields(tmp_path):
    """AC1: Output LOOP_STATE_V1 contains all schema-required fields."""
    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=FIXTURE_DIR / "planner_approve.json",
        review_file=FIXTURE_DIR / "review_approve.json",
        issue_number=1024,
        iteration=0,
        out=out,
    )
    assert result.returncode == 0
    loop_state = json.loads(out.read_text(encoding="utf-8"))
    required_fields = [
        "schema_version", "issue_number", "iteration", "max_iterations",
        "last_verdict", "termination_reason", "scope_signal_guard",
        "delivery_rollup", "follow_up_materialization", "web_research_policy",
    ]
    for field in required_fields:
        assert field in loop_state, f"Missing required field: {field}"


# ---------------------------------------------------------------------------
# AC6: provenance fields
# ---------------------------------------------------------------------------


def test_provenance_fields(tmp_path):
    """AC6: provenance contains all required fields."""
    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=FIXTURE_DIR / "planner_approve.json",
        review_file=FIXTURE_DIR / "review_approve.json",
        issue_number=1024,
        iteration=0,
        out=out,
    )
    assert result.returncode == 0
    build_result = load_build_result(result)
    prov = build_result["provenance"]

    required_provenance_fields = [
        "planner_result_path",
        "planner_result_hash",
        "review_result_path",
        "review_result_hash",
        "issue_number",
        "iteration",
        "schema_path",
        "schema_hash",
    ]
    for field in required_provenance_fields:
        assert field in prov, f"Missing provenance field: {field}"

    assert prov["issue_number"] == 1024
    assert prov["iteration"] == 0
    assert prov["planner_result_hash"] is not None
    assert prov["planner_result_hash"].startswith("sha256:")
    assert prov["review_result_hash"] is not None
    assert prov["review_result_hash"].startswith("sha256:")


def test_provenance_paths_match_inputs(tmp_path):
    """AC6: provenance paths match input file paths."""
    planner_path = FIXTURE_DIR / "planner_approve.json"
    review_path = FIXTURE_DIR / "review_approve.json"
    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=planner_path,
        review_file=review_path,
        issue_number=1024,
        iteration=0,
        out=out,
    )
    assert result.returncode == 0
    build_result = load_build_result(result)
    prov = build_result["provenance"]
    assert prov["planner_result_path"] == str(planner_path)
    assert prov["review_result_path"] == str(review_path)


# ---------------------------------------------------------------------------
# AC7: no next_action in output
# ---------------------------------------------------------------------------


def test_no_next_action_in_output(tmp_path):
    """AC7: LOOP_STATE_V1 output does not contain next_action field."""
    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=FIXTURE_DIR / "planner_approve.json",
        review_file=FIXTURE_DIR / "review_approve.json",
        issue_number=1024,
        iteration=0,
        out=out,
    )
    assert result.returncode == 0
    loop_state = json.loads(out.read_text(encoding="utf-8"))
    assert "next_action" not in loop_state, (
        "LOOP_STATE_V1 must not contain next_action (AC7: only decide_next_loop_action.py determines it)"
    )


def test_no_approve_field_in_output(tmp_path):
    """AC7: LOOP_STATE_V1 does not contain approve/needs_fix decision fields."""
    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=FIXTURE_DIR / "planner_approve.json",
        review_file=FIXTURE_DIR / "review_approve.json",
        issue_number=1024,
        iteration=0,
        out=out,
    )
    assert result.returncode == 0
    loop_state = json.loads(out.read_text(encoding="utf-8"))
    # These routing fields must only come from decide_next_loop_action.py
    forbidden_fields = ["next_action", "action", "routing_decision"]
    for field in forbidden_fields:
        assert field not in loop_state, (
            f"LOOP_STATE_V1 must not contain '{field}' (routing is decide_next_loop_action.py's job)"
        )


# ---------------------------------------------------------------------------
# AC8: no raw JSON input accepted
# ---------------------------------------------------------------------------


def test_no_raw_json_input(tmp_path):
    """AC8: Script rejects raw JSON string input (argparse only accepts file paths)."""
    out = tmp_path / "loop_state.json"
    # Attempt to pass raw JSON as --planner-result-file (should fail since it's not a file path)
    raw_json = '{"schema_version": "refinement_loop_plan/v1"}'
    argv = [
        sys.executable,
        str(SCRIPT),
        "--planner-result-file", raw_json,  # raw JSON, not a file path
        "--review-result-file", str(FIXTURE_DIR / "review_approve.json"),
        "--issue-number", "1024",
        "--iteration", "0",
        "--out", str(out),
    ]
    result = subprocess.run(argv, capture_output=True, text=True)
    # Should fail because raw JSON is not a valid file path (file does not exist)
    assert result.returncode != 0, "Expected failure when passing raw JSON as file path"
    # Error should indicate file not found or similar
    build_result_text = result.stdout.strip()
    if build_result_text:
        build_result = json.loads(build_result_text)
        assert build_result["status"] != "ok"


def test_cli_uses_allow_abbrev_false(tmp_path):
    """AC8: CLI is built with allow_abbrev=False (verify via source inspection)."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "allow_abbrev=False" in source, (
        "build_loop_state.py must use argparse.ArgumentParser(allow_abbrev=False)"
    )


def test_no_inline_json_argument():
    """AC8: Script has no --loop-state-json or similar inline JSON argument."""
    source = SCRIPT.read_text(encoding="utf-8")
    # Should not have an inline JSON option for raw data
    forbidden_flags = ["--loop-state-json", "--planner-result-json", "--review-result-json"]
    for flag in forbidden_flags:
        assert flag not in source, (
            f"build_loop_state.py must not accept raw JSON input via {flag} (AC8)"
        )


# ---------------------------------------------------------------------------
# AC12: no gh mutation
# ---------------------------------------------------------------------------


def test_no_gh_mutation(tmp_path):
    """AC12: build_loop_state.py does not call gh commands."""
    source = SCRIPT.read_text(encoding="utf-8")
    # Check that no gh mutation commands appear in the source
    gh_mutation_patterns = [
        "gh issue edit",
        "gh issue comment",
        "gh issue create",
        "gh pr edit",
        "gh pr create",
        "gh pr merge",
    ]
    for pattern in gh_mutation_patterns:
        assert pattern not in source, (
            f"build_loop_state.py must not contain '{pattern}' (AC12: builder does no gh mutations)"
        )


def test_no_subprocess_gh_calls(tmp_path):
    """AC12: Running build_loop_state.py with approve fixtures does not invoke gh."""
    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=FIXTURE_DIR / "planner_approve.json",
        review_file=FIXTURE_DIR / "review_approve.json",
        issue_number=1024,
        iteration=0,
        out=out,
    )
    assert result.returncode == 0
    # No gh invocations in stdout or stderr
    assert "gh " not in result.stdout, "Unexpected gh invocation in stdout"
    assert "gh " not in result.stderr, "Unexpected gh invocation in stderr"


# ---------------------------------------------------------------------------
# AC2: Schema validation
# ---------------------------------------------------------------------------


def test_schema_validation_passes_for_valid_state(tmp_path):
    """AC2: Generated LOOP_STATE passes schema validation."""
    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=FIXTURE_DIR / "planner_approve.json",
        review_file=FIXTURE_DIR / "review_approve.json",
        issue_number=1024,
        iteration=0,
        out=out,
    )
    assert result.returncode == 0
    build_result = load_build_result(result)
    assert build_result["errors"] == [], f"Unexpected validation errors: {build_result['errors']}"


def test_output_json_is_deterministic(tmp_path):
    """AC2: Output JSON is stable (sort_keys=True)."""
    out1 = tmp_path / "out1.json"
    out2 = tmp_path / "out2.json"
    for out in [out1, out2]:
        run_builder(
            planner_file=FIXTURE_DIR / "planner_approve.json",
            review_file=FIXTURE_DIR / "review_approve.json",
            issue_number=1024,
            iteration=0,
            out=out,
        )
    assert out1.read_text(encoding="utf-8") == out2.read_text(encoding="utf-8"), (
        "Output JSON is not deterministic across runs"
    )


# ---------------------------------------------------------------------------
# Build result schema
# ---------------------------------------------------------------------------


def test_build_result_has_schema_field(tmp_path):
    """Build result has schema=LOOP_STATE_BUILD_RESULT_V1."""
    out = tmp_path / "loop_state.json"
    result = run_builder(
        planner_file=FIXTURE_DIR / "planner_approve.json",
        review_file=FIXTURE_DIR / "review_approve.json",
        issue_number=1024,
        iteration=0,
        out=out,
    )
    build_result = load_build_result(result)
    assert build_result["schema"] == "LOOP_STATE_BUILD_RESULT_V1"
    assert "loop_state_path" in build_result
    assert "loop_state_sha256" in build_result
    assert "errors" in build_result
    assert "warnings" in build_result
    assert "provenance" in build_result
