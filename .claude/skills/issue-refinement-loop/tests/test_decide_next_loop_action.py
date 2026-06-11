#!/usr/bin/env python3
"""
test_decide_next_loop_action.py

AC3: decide_next_loop_action.py determines next action from compact review
result fixture, and iteration limit exceeded → human escalation (exit 2).
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SKILL_ROOT = Path(__file__).parent.parent
SCRIPT = SKILL_ROOT / "scripts" / "decide_next_loop_action.py"
FIXTURE_PATH = SKILL_ROOT / "fixtures" / "loop_state_v1_fixture.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_fixture() -> dict[str, Any]:
    assert FIXTURE_PATH.exists(), f"Missing fixture: {FIXTURE_PATH}"
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def run_script(
    loop_state: dict[str, Any],
    verdict: str = "needs-fix",
    max_iterations: int | None = None,
) -> subprocess.CompletedProcess:
    """Run decide_next_loop_action.py with a loop state dict via --loop-state-json."""
    argv = [
        sys.executable,
        str(SCRIPT),
        "--loop-state-json",
        json.dumps(loop_state),
        "--review-result-verdict",
        verdict,
    ]
    if max_iterations is not None:
        argv += ["--max-iterations", str(max_iterations)]
    return subprocess.run(argv, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Script existence
# ---------------------------------------------------------------------------

def test_script_exists():
    """AC3: decide_next_loop_action.py exists."""
    assert SCRIPT.exists(), f"Missing script: {SCRIPT}"


def test_script_is_runnable():
    """Script can be invoked with python."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    # --help exits 0 and prints usage
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# AC3: next action from compact review result
# ---------------------------------------------------------------------------

def test_approve_verdict_proceeds_to_step_4_5():
    """approve verdict → NEXT_ACTION: proceed_to_step_4_5, exit 0."""
    state = load_fixture()
    state["iteration"] = 0
    state["max_iterations"] = 3
    result = run_script(state, verdict="approve")
    assert result.returncode == 0
    assert "STATUS: pass" in result.stdout
    assert "NEXT_ACTION: proceed_to_step_4_5" in result.stdout


def test_needs_fix_below_max_continues_to_step_4():
    """needs-fix with iteration < max_iterations → continue_to_step_4, exit 0."""
    state = load_fixture()
    state["iteration"] = 1
    state["max_iterations"] = 3
    result = run_script(state, verdict="needs-fix")
    assert result.returncode == 0
    assert "STATUS: pass" in result.stdout
    assert "NEXT_ACTION: continue_to_step_4" in result.stdout


def test_needs_fix_at_iteration_0_continues():
    """needs-fix at iteration=0, max=3 → continue_to_step_4."""
    state = load_fixture()
    state["iteration"] = 0
    state["max_iterations"] = 3
    result = run_script(state, verdict="needs-fix")
    assert result.returncode == 0
    assert "NEXT_ACTION: continue_to_step_4" in result.stdout


# ---------------------------------------------------------------------------
# AC3: iteration limit → human escalation (exit 2)
# ---------------------------------------------------------------------------

def test_needs_fix_at_max_iterations_escalates():
    """needs-fix with iteration >= max_iterations → human_escalation, exit 2."""
    state = load_fixture()
    state["iteration"] = 3
    state["max_iterations"] = 3
    result = run_script(state, verdict="needs-fix")
    assert result.returncode == 2, (
        f"Expected exit 2 for max_iterations exceeded, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "STATUS: human_escalation" in result.stdout
    assert "NEXT_ACTION: human_escalation" in result.stdout
    assert "max_iterations_exceeded" in result.stdout


def test_needs_fix_iteration_equals_max_escalates():
    """needs-fix at iteration == max_iterations → escalation."""
    state = load_fixture()
    state["iteration"] = 2
    state["max_iterations"] = 2
    result = run_script(state, verdict="needs-fix")
    assert result.returncode == 2
    assert "human_escalation" in result.stdout


def test_max_iterations_override_cli_flag():
    """--max-iterations CLI flag overrides state field."""
    state = load_fixture()
    state["iteration"] = 5
    state["max_iterations"] = 10
    result = run_script(state, verdict="needs-fix", max_iterations=5)
    assert result.returncode == 2
    assert "human_escalation" in result.stdout


# ---------------------------------------------------------------------------
# Inconsistent state → exit 3
# ---------------------------------------------------------------------------

def test_negative_iteration_is_inconsistent_state():
    """iteration < 0 → STATUS: inconsistent_state, exit 3."""
    state = load_fixture()
    state["iteration"] = -1
    result = run_script(state, verdict="needs-fix")
    assert result.returncode == 3
    assert "inconsistent_state" in result.stdout


def test_max_iterations_below_1_is_inconsistent_state():
    """max_iterations < 1 → STATUS: inconsistent_state, exit 3."""
    state = load_fixture()
    state["max_iterations"] = 0
    result = run_script(state, verdict="needs-fix")
    assert result.returncode == 3
    assert "inconsistent_state" in result.stdout


def test_invalid_json_is_inconsistent_state():
    """Invalid JSON input → exit 3."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--loop-state-json", "not-json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3


# ---------------------------------------------------------------------------
# scope_signal_guard hard stop → exit 2
# ---------------------------------------------------------------------------

def test_scope_signal_guard_triggered_escalates():
    """scope_signal_guard.triggered=true, not excluded → human_escalation."""
    state = load_fixture()
    state["scope_signal_guard"] = {
        "triggered": True,
        "excluded_by_anchor_reframe": False,
        "reason_code": "new_in_scope_area",
    }
    result = run_script(state, verdict="needs-fix")
    assert result.returncode == 2
    assert "scope_signal_guard_triggered" in result.stdout


def test_scope_signal_guard_excluded_by_anchor_does_not_escalate():
    """scope_signal_guard.triggered=true but excluded_by_anchor_reframe → continue."""
    state = load_fixture()
    state["scope_signal_guard"] = {
        "triggered": True,
        "excluded_by_anchor_reframe": True,
        "reason_code": "anchor_reframe_exclusion",
    }
    state["iteration"] = 0
    state["max_iterations"] = 3
    result = run_script(state, verdict="needs-fix")
    assert result.returncode == 0
    assert "continue_to_step_4" in result.stdout


# ---------------------------------------------------------------------------
# Already-terminated state → terminate
# ---------------------------------------------------------------------------

def test_already_terminated_returns_terminate():
    """termination_reason != null → NEXT_ACTION: terminate, exit 0."""
    state = load_fixture()
    state["termination_reason"] = "approved"
    result = run_script(state, verdict="approve")
    assert result.returncode == 0
    assert "NEXT_ACTION: terminate" in result.stdout


# ---------------------------------------------------------------------------
# stdout budget
# ---------------------------------------------------------------------------

def test_stdout_budget_under_2000_bytes():
    """stdout must be < 2000 bytes (OUTPUT_BUDGET_V1)."""
    state = load_fixture()
    state["iteration"] = 1
    result = run_script(state, verdict="needs-fix")
    assert len(result.stdout.encode("utf-8")) < 2000, (
        f"stdout exceeds 2000 bytes: {len(result.stdout.encode('utf-8'))} bytes"
    )


# ---------------------------------------------------------------------------
# AC4: uv run enforcement (checked via check_vc_scope.py — smoke test)
# ---------------------------------------------------------------------------

def test_script_does_not_reference_bare_python3_in_skill_md():
    """
    AC4: SKILL.md must not contain 'python3 .claude/skills/issue-refinement-loop/scripts/'
    without 'uv run' prefix.
    This is a structural smoke test that the script path pattern is not used bare.
    """
    skill_md = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    import re
    # Pattern: bare python3 (not prefixed by 'uv run') calling a script in this dir
    bad_pattern = re.compile(
        r"(?<!uv run )python3 \.claude/skills/issue-refinement-loop/scripts/"
    )
    bad_matches = bad_pattern.findall(skill_md)
    assert not bad_matches, (
        f"SKILL.md contains bare python3 invocation without 'uv run': {bad_matches}"
    )
