"""
test_scope_signal_guard_termination.py

Regression tests for Issue #919:
scope_signal_guard 停止時の termination_cause 正規化を検証する。

AC coverage:
  AC1: scope_signal_guard.triggered=true, reason_code=new_allowed_path_layer
       -> termination report input の termination_cause が human_judgment_required になる
  AC2: scope_signal_guard.reason_code は BLOCKERS に残り blockers_summary に保持できる
  AC3: scope_signal_guard_triggered を termination_cause として renderer に直接渡す
       -> fail-closed。pytest 全体は PASS
  AC4: max_iterations_exceeded / needs_fix_at_iteration_limit / human_judgment_required
       の既存挙動が変わらない
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))

import render_termination_report as rtr

# Fixture path: uses the same fixture as test_decide_next_loop_action.py
_FIXTURE_PATH = SKILL_ROOT / "fixtures" / "loop_state_v1_fixture.json"


# ---------------------------------------------------------------------------
# Helpers: decide_next_loop_action subprocess runner
# ---------------------------------------------------------------------------

def _load_base_fixture() -> dict:
    """Load the canonical loop_state_v1 fixture (same as other decide_next_loop_action tests)."""
    assert _FIXTURE_PATH.exists(), f"Missing fixture: {_FIXTURE_PATH}"
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _run_decide(state: dict, verdict: str = "needs-fix") -> subprocess.CompletedProcess:
    """Run decide_next_loop_action.py as subprocess and return the result."""
    state_json = json.dumps(state)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "decide_next_loop_action.py"),
         "--loop-state-json", state_json,
         "--review-result-verdict", verdict],
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# AC1: scope_signal_guard.triggered=true, reason_code=new_allowed_path_layer
#       -> decide_next_loop_action emits TERMINATION_CAUSE: human_judgment_required
#       -> feeding that into render_termination_report produces publishable=true
# ---------------------------------------------------------------------------

class TestAC1ScopeSignalGuardTerminationCauseNormalization:

    def test_decide_emits_termination_cause_human_judgment_required(self):
        """AC1: scope_signal_guard.triggered=true → TERMINATION_CAUSE: human_judgment_required"""
        state = _load_base_fixture()
        state["scope_signal_guard"] = {
            "triggered": True,
            "excluded_by_anchor_reframe": False,
            "reason_code": "new_allowed_path_layer",
        }
        result = _run_decide(state, verdict="needs-fix")
        assert result.returncode == 2, f"Expected exit 2 (human_escalation), got {result.returncode}"
        assert "TERMINATION_CAUSE: human_judgment_required" in result.stdout, (
            f"Expected TERMINATION_CAUSE: human_judgment_required in stdout.\nActual stdout: {result.stdout!r}"
        )

    def test_termination_cause_from_decide_renders_successfully(self):
        """AC1: TERMINATION_CAUSE=human_judgment_required → render produces publishable=true"""
        # Simulate orchestrator using the TERMINATION_CAUSE from decide_next_loop_action
        render_input = {
            "termination_reason": "human_escalation",
            "termination_cause": "human_judgment_required",
            "issue_number": 919,
            "iteration": 0,
            "blockers_summary": [
                "scope_signal_guard_triggered",
                "scope_signal_guard_reason_code:new_allowed_path_layer",
            ],
        }
        result = rtr.render(render_input)
        assert result["publishable"] is True, (
            f"Expected publishable=true for human_judgment_required cause.\nResult: {result}"
        )
        assert result["termination_cause"] == "human_judgment_required"
        assert result["body"] is not None

    def test_termination_cause_human_judgment_required_is_valid(self):
        """AC1: human_judgment_required は VALID_TERMINATION_CAUSES に含まれる"""
        data, err = rtr._validate_input({
            "termination_reason": "human_escalation",
            "termination_cause": "human_judgment_required",
        })
        assert err == "", f"human_judgment_required should be valid, got error: {err}"
        assert data is not None


# ---------------------------------------------------------------------------
# AC2: scope_signal_guard.reason_code は BLOCKERS に残る
# ---------------------------------------------------------------------------

class TestAC2ReasonCodePreservedInBlockers:

    def test_reason_code_new_allowed_path_layer_in_blockers(self):
        """AC2: reason_code=new_allowed_path_layer は BLOCKERS に残る"""
        state = _load_base_fixture()
        state["scope_signal_guard"] = {
            "triggered": True,
            "excluded_by_anchor_reframe": False,
            "reason_code": "new_allowed_path_layer",
        }
        result = _run_decide(state, verdict="needs-fix")
        assert result.returncode == 2
        assert "scope_signal_guard_triggered" in result.stdout
        assert "new_allowed_path_layer" in result.stdout, (
            f"Expected reason_code new_allowed_path_layer in BLOCKERS.\nActual stdout: {result.stdout!r}"
        )

    def test_reason_code_without_scope_signal_guard_trigger_absent(self):
        """AC2: scope_signal_guard.triggered=false → reason_code は BLOCKERS に出ない"""
        state = _load_base_fixture()
        state["scope_signal_guard"] = {
            "triggered": False,
            "excluded_by_anchor_reframe": False,
            "reason_code": "new_allowed_path_layer",
        }
        state["iteration"] = 0
        state["max_iterations"] = 3
        result = _run_decide(state, verdict="approve")
        assert "new_allowed_path_layer" not in result.stdout

    def test_blockers_summary_can_hold_reason_code(self):
        """AC2: blockers_summary に reason_code を含めて render 可能"""
        render_input = {
            "termination_reason": "human_escalation",
            "termination_cause": "human_judgment_required",
            "issue_number": 919,
            "blockers_summary": [
                "scope_signal_guard_triggered",
                "scope_signal_guard_reason_code:new_allowed_path_layer",
            ],
        }
        result = rtr.render(render_input)
        assert result["publishable"] is True
        # blockers_summary entries appear in body
        assert "scope_signal_guard_triggered" in result["body"]

    def test_no_reason_code_still_has_scope_signal_guard_triggered(self):
        """AC2: reason_code なしでも scope_signal_guard_triggered は BLOCKERS に出る"""
        state = _load_base_fixture()
        state["scope_signal_guard"] = {
            "triggered": True,
            "excluded_by_anchor_reframe": False,
            "reason_code": None,
        }
        result = _run_decide(state, verdict="needs-fix")
        assert result.returncode == 2
        assert "scope_signal_guard_triggered" in result.stdout


# ---------------------------------------------------------------------------
# AC3: scope_signal_guard_triggered を termination_cause として renderer に直接渡す
#       -> fail-closed。pytest 自体は PASS
# ---------------------------------------------------------------------------

class TestAC3InvalidTerminationCauseFailClosed:

    def test_scope_signal_guard_triggered_as_cause_is_rejected(self):
        """AC3: scope_signal_guard_triggered は VALID_TERMINATION_CAUSES に含まれない"""
        data, err = rtr._validate_input({
            "termination_reason": "human_escalation",
            "termination_cause": "scope_signal_guard_triggered",
        })
        assert data is None, "scope_signal_guard_triggered must be rejected as termination_cause"
        assert err != "", f"Expected non-empty error, got: {err!r}"
        assert "scope_signal_guard_triggered" in err or "Invalid termination_cause" in err

    def test_renderer_fails_closed_with_invalid_cause(self):
        """AC3: render() が invalid termination_cause で InputValidationError を raise"""
        with pytest.raises(rtr.InputValidationError):
            rtr.render({
                "termination_reason": "human_escalation",
                "termination_cause": "scope_signal_guard_triggered",
            })

    def test_cli_renderer_returns_invalid_input_for_scope_signal_guard_triggered(self):
        """AC3: CLI 経由でも invalid termination_cause は exit 2 で fail-closed"""
        bad_input = json.dumps({
            "termination_reason": "human_escalation",
            "termination_cause": "scope_signal_guard_triggered",
        })
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "render_termination_report.py")],
            input=bad_input,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2, (
            f"Expected exit 2 for invalid termination_cause, got {result.returncode}.\n"
            f"stdout: {result.stdout!r}"
        )
        stdout_json = json.loads(result.stdout)
        assert stdout_json["publishable"] is False
        assert stdout_json["reason_code"] == "invalid_input"

    def test_valid_causes_still_accepted(self):
        """AC3: valid な termination_cause は fail-closed にならない"""
        valid_causes = [
            "human_judgment_required",
            "max_iterations_exceeded",
            "needs_fix_at_iteration_limit",
            None,
        ]
        for cause in valid_causes:
            data = {
                "termination_reason": "human_escalation",
                "termination_cause": cause,
            }
            validated, err = rtr._validate_input(data)
            assert err == "", f"Valid cause {cause!r} was rejected with error: {err}"


# ---------------------------------------------------------------------------
# AC4: 既存挙動が変わらない
# ---------------------------------------------------------------------------

class TestAC4ExistingBehaviorUnchanged:

    def test_max_iterations_exceeded_cause_still_valid(self):
        """AC4: max_iterations_exceeded は引き続き valid"""
        result = rtr.render({
            "termination_reason": "human_escalation",
            "termination_cause": "max_iterations_exceeded",
            "issue_number": 919,
            "iteration": 3,
        })
        assert result["publishable"] is True
        assert result["termination_cause"] == "max_iterations_exceeded"

    def test_needs_fix_at_iteration_limit_cause_still_valid(self):
        """AC4: needs_fix_at_iteration_limit は引き続き valid"""
        result = rtr.render({
            "termination_reason": "human_escalation",
            "termination_cause": "needs_fix_at_iteration_limit",
        })
        assert result["publishable"] is True
        assert result["termination_cause"] == "needs_fix_at_iteration_limit"

    def test_explicit_human_judgment_required_still_valid(self):
        """AC4: 明示的 human_judgment_required は引き続き valid"""
        result = rtr.render({
            "termination_reason": "human_escalation",
            "termination_cause": "human_judgment_required",
        })
        assert result["publishable"] is True
        assert result["termination_cause"] == "human_judgment_required"

    def test_decide_max_iterations_exceeded_still_works(self):
        """AC4: max_iterations 超過の既存挙動が変わらない"""
        state = _load_base_fixture()
        state["iteration"] = 2
        state["max_iterations"] = 3
        state["scope_signal_guard"] = {
            "triggered": False,
            "excluded_by_anchor_reframe": False,
            "reason_code": None,
        }
        result = _run_decide(state, verdict="needs-fix")
        assert result.returncode == 2
        assert "max_iterations_exceeded" in result.stdout
        # TERMINATION_CAUSE should be max_iterations_exceeded for this case
        assert "TERMINATION_CAUSE: max_iterations_exceeded" in result.stdout

    def test_decide_approve_still_works(self):
        """AC4: approve verdict の既存挙動が変わらない"""
        state = _load_base_fixture()
        state["iteration"] = 0
        state["max_iterations"] = 3
        state["scope_signal_guard"] = {
            "triggered": False,
            "excluded_by_anchor_reframe": False,
            "reason_code": None,
        }
        result = _run_decide(state, verdict="approve")
        assert result.returncode == 0
        assert "proceed_to_step_4_5" in result.stdout

    def test_decide_needs_fix_within_limit_still_works(self):
        """AC4: needs-fix かつ iteration 上限前の既存挙動が変わらない"""
        state = _load_base_fixture()
        state["iteration"] = 0
        state["max_iterations"] = 3
        state["scope_signal_guard"] = {
            "triggered": False,
            "excluded_by_anchor_reframe": False,
            "reason_code": None,
        }
        result = _run_decide(state, verdict="needs-fix")
        assert result.returncode == 0
        assert "continue_to_step_4" in result.stdout
