#!/usr/bin/env python3
"""
test_loop_state_decide_integration.py

Integration tests: build_loop_state.py output fed to decide_next_loop_action.py (AC3, AC10).

Tests:
- approve → proceed_to_step_4_5
- needs-fix → continue_to_step_4
- scope_signal → human_escalation
- max_iterations → human_escalation
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SKILL_ROOT = Path(__file__).parent.parent
BUILD_SCRIPT = SKILL_ROOT / "scripts" / "build_loop_state.py"
DECIDE_SCRIPT = SKILL_ROOT / "scripts" / "decide_next_loop_action.py"
FIXTURE_DIR = SKILL_ROOT / "tests" / "fixtures" / "loop_state_builder"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_planner(issue_number: int = 1024, scope_signal: bool = False) -> dict[str, Any]:
    """Return a minimal REFINEMENT_LOOP_PLAN_V1 fixture."""
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
                "triggered": scope_signal,
                "reason_code": "new_in_scope_area" if scope_signal else "no_scope_signal",
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
    """Return a minimal ISSUE_REVIEW_RESULT_COMPACT_V1 fixture."""
    return {
        "STATUS": "ok",
        "VERDICT": verdict,
        "SUMMARY": "test summary",
        "BLOCKERS": "0",
        "NEXT_ACTION": "proceed" if verdict == "approve" else "request_changes",
        "MUST_READ": "",
        "EVIDENCE": "",
        "ARTIFACT": "",
    }


def build_loop_state(
    planner_data: dict[str, Any],
    review_data: dict[str, Any],
    issue_number: int,
    iteration: int,
    max_iterations: int,
    tmp_path: Path,
) -> tuple[Path, dict[str, Any]]:
    """Run build_loop_state.py and return (loop_state_path, build_result)."""
    planner_path = tmp_path / "planner.json"
    planner_path.write_text(json.dumps(planner_data), encoding="utf-8")
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review_data), encoding="utf-8")
    out_path = tmp_path / "loop_state.json"

    result = subprocess.run(
        [
            sys.executable, str(BUILD_SCRIPT),
            "--planner-result-file", str(planner_path),
            "--review-result-file", str(review_path),
            "--issue-number", str(issue_number),
            "--iteration", str(iteration),
            "--max-iterations", str(max_iterations),
            "--out", str(out_path),
        ],
        capture_output=True,
        text=True,
    )
    build_result = json.loads(result.stdout)
    assert build_result["status"] == "ok", (
        f"build_loop_state failed: {result.stdout}\n{result.stderr}"
    )
    return out_path, build_result


def run_decide(loop_state_path: Path, verdict: str) -> subprocess.CompletedProcess:
    """Run decide_next_loop_action.py with the given loop_state file."""
    return subprocess.run(
        [
            sys.executable, str(DECIDE_SCRIPT),
            "--loop-state-file", str(loop_state_path),
            "--review-result-verdict", verdict,
        ],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# AC3: decide_next_loop_action routing from fixture
# ---------------------------------------------------------------------------


def test_builder_integration_approve(tmp_path):
    """AC3/AC10: builder output with approve verdict → proceed_to_step_4_5."""
    loop_state_path, _ = build_loop_state(
        planner_data=_minimal_planner(),
        review_data=_minimal_review("approve"),
        issue_number=1024,
        iteration=0,
        max_iterations=3,
        tmp_path=tmp_path,
    )
    result = run_decide(loop_state_path, "approve")
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )
    assert "NEXT_ACTION: proceed_to_step_4_5" in result.stdout
    assert "STATUS: pass" in result.stdout


def test_builder_integration_needs_fix(tmp_path):
    """AC3/AC10: builder output with needs-fix verdict → continue_to_step_4."""
    loop_state_path, _ = build_loop_state(
        planner_data=_minimal_planner(),
        review_data=_minimal_review("needs-fix"),
        issue_number=1024,
        iteration=0,
        max_iterations=3,
        tmp_path=tmp_path,
    )
    result = run_decide(loop_state_path, "needs-fix")
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )
    assert "NEXT_ACTION: continue_to_step_4" in result.stdout
    assert "STATUS: pass" in result.stdout


def test_builder_integration_scope_signal(tmp_path):
    """AC3/AC10: builder output with scope_signal triggered → human_escalation."""
    loop_state_path, _ = build_loop_state(
        planner_data=_minimal_planner(scope_signal=True),
        review_data=_minimal_review("needs-fix"),
        issue_number=1024,
        iteration=0,
        max_iterations=3,
        tmp_path=tmp_path,
    )
    result = run_decide(loop_state_path, "needs-fix")
    assert result.returncode == 2, (
        f"Expected exit 2 for scope_signal, got {result.returncode}\nstdout:{result.stdout}"
    )
    assert "STATUS: human_escalation" in result.stdout
    assert "scope_signal_guard_triggered" in result.stdout


def test_builder_integration_max_iterations(tmp_path):
    """AC3/AC10: builder output with iteration at max_iterations → human_escalation."""
    loop_state_path, _ = build_loop_state(
        planner_data=_minimal_planner(),
        review_data=_minimal_review("needs-fix"),
        issue_number=1024,
        iteration=3,  # >= max_iterations of 3
        max_iterations=3,
        tmp_path=tmp_path,
    )
    result = run_decide(loop_state_path, "needs-fix")
    assert result.returncode == 2, (
        f"Expected exit 2 for max_iterations, got {result.returncode}\nstdout:{result.stdout}"
    )
    assert "STATUS: human_escalation" in result.stdout
    assert "max_iterations_exceeded" in result.stdout


# ---------------------------------------------------------------------------
# AC10: builder integration tests (using actual fixture files)
# ---------------------------------------------------------------------------


def test_builder_integration_approve_from_fixture(tmp_path):
    """AC10: builder approve fixtures → decide produces proceed_to_step_4_5."""
    out_path = tmp_path / "loop_state.json"
    build_result = subprocess.run(
        [
            sys.executable, str(BUILD_SCRIPT),
            "--planner-result-file", str(FIXTURE_DIR / "planner_approve.json"),
            "--review-result-file", str(FIXTURE_DIR / "review_approve.json"),
            "--issue-number", "1024",
            "--iteration", "0",
            "--max-iterations", "3",
            "--out", str(out_path),
        ],
        capture_output=True,
        text=True,
    )
    br = json.loads(build_result.stdout)
    assert br["status"] == "ok"

    decide_result = run_decide(out_path, "approve")
    assert decide_result.returncode == 0
    assert "NEXT_ACTION: proceed_to_step_4_5" in decide_result.stdout


def test_builder_integration_needs_fix_from_fixture(tmp_path):
    """AC10: builder needs-fix fixtures → decide produces continue_to_step_4."""
    out_path = tmp_path / "loop_state.json"
    build_result = subprocess.run(
        [
            sys.executable, str(BUILD_SCRIPT),
            "--planner-result-file", str(FIXTURE_DIR / "planner_needs_fix.json"),
            "--review-result-file", str(FIXTURE_DIR / "review_needs_fix.json"),
            "--issue-number", "1024",
            "--iteration", "0",
            "--max-iterations", "3",
            "--out", str(out_path),
        ],
        capture_output=True,
        text=True,
    )
    br = json.loads(build_result.stdout)
    assert br["status"] == "ok"

    decide_result = run_decide(out_path, "needs-fix")
    assert decide_result.returncode == 0
    assert "NEXT_ACTION: continue_to_step_4" in decide_result.stdout


def test_builder_integration_scope_signal_from_fixture(tmp_path):
    """AC10: builder scope_signal fixtures → decide produces human_escalation."""
    out_path = tmp_path / "loop_state.json"
    build_result = subprocess.run(
        [
            sys.executable, str(BUILD_SCRIPT),
            "--planner-result-file", str(FIXTURE_DIR / "planner_scope_signal.json"),
            "--review-result-file", str(FIXTURE_DIR / "review_scope_signal.json"),
            "--issue-number", "1024",
            "--iteration", "0",
            "--max-iterations", "3",
            "--out", str(out_path),
        ],
        capture_output=True,
        text=True,
    )
    br = json.loads(build_result.stdout)
    assert br["status"] == "ok"

    decide_result = run_decide(out_path, "needs-fix")
    assert decide_result.returncode == 2
    assert "STATUS: human_escalation" in decide_result.stdout
    assert "scope_signal_guard_triggered" in decide_result.stdout
