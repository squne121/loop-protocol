#!/usr/bin/env python3
"""
test_refinement_phase_gate.py

Tests for ISSUE_REFINEMENT_PHASE_STATE_V1 and the phase gate in
decide_next_loop_action.py.

AC1: preflight phase では decide_next_loop_action.py を通常 routing として実行できない。
AC2: decide_next_loop_action.py は phase gate を schema validation より先に評価する。
AC3: scope_signal_guard.triggered は phase-sensitive に扱う。
AC5: 失敗パターン fixture（preflight pass → scope_signal_guard.triggered: true → 誤った
     human_escalation）を再発防止。
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

SKILL_ROOT = Path(__file__).parent.parent
DECIDE_SCRIPT = SKILL_ROOT / "scripts" / "decide_next_loop_action.py"
BUILD_SCRIPT = SKILL_ROOT / "scripts" / "build_refinement_phase_state.py"
FIXTURE_PATH = SKILL_ROOT / "fixtures" / "loop_state_v1_fixture.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_loop_state_fixture() -> dict[str, Any]:
    assert FIXTURE_PATH.exists(), f"Missing fixture: {FIXTURE_PATH}"
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def make_phase_state(
    phase: str,
    source_kind: str = "refinement_preflight_result_v1",
    source_path: str = "/tmp/fake_source.json",
) -> dict[str, Any]:
    """Build a minimal ISSUE_REFINEMENT_PHASE_STATE_V1 via build_refinement_phase_state.py."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as out_f:
        out_path = out_f.name

    result = subprocess.run(
        [
            sys.executable,
            str(BUILD_SCRIPT),
            "--phase", phase,
            "--source-kind", source_kind,
            "--source-path", source_path,
            "--output-path", out_path,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"build_refinement_phase_state.py failed for phase={phase!r}:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    data = json.loads(Path(out_path).read_text(encoding="utf-8"))
    return data


def write_phase_state_to_tmp(phase_state: dict[str, Any]) -> str:
    """Write phase state dict to a temp file and return the path."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(phase_state, f)
        return f.name


def run_decide_with_phase_state(
    loop_state: dict[str, Any],
    phase_state: dict[str, Any],
    verdict: str = "needs-fix",
) -> subprocess.CompletedProcess:
    """Run decide_next_loop_action.py with a phase state file."""
    phase_state_path = write_phase_state_to_tmp(phase_state)
    return subprocess.run(
        [
            sys.executable,
            str(DECIDE_SCRIPT),
            "--loop-state-json", json.dumps(loop_state),
            "--review-result-verdict", verdict,
            "--phase-state-file", phase_state_path,
        ],
        capture_output=True,
        text=True,
    )


def run_decide_without_phase_state(
    loop_state: dict[str, Any],
    verdict: str = "needs-fix",
) -> subprocess.CompletedProcess:
    """Run decide_next_loop_action.py WITHOUT a phase state file (baseline)."""
    return subprocess.run(
        [
            sys.executable,
            str(DECIDE_SCRIPT),
            "--loop-state-json", json.dumps(loop_state),
            "--review-result-verdict", verdict,
        ],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Script existence
# ---------------------------------------------------------------------------


def test_decide_script_exists():
    assert DECIDE_SCRIPT.exists(), f"Missing script: {DECIDE_SCRIPT}"


def test_build_script_exists():
    assert BUILD_SCRIPT.exists(), f"Missing script: {BUILD_SCRIPT}"


# ---------------------------------------------------------------------------
# AC2: phase gate は schema validation より先に評価される
# ---------------------------------------------------------------------------


def test_phase_gate_evaluated_before_schema_validation():
    """
    AC2: preflight phase での decide_next_loop_action.py 呼び出しは
    LOOP_STATE_V1 schema validation より先に phase gate でブロックされる。
    """
    # Use invalid loop state (missing required fields) — gate should fire first
    invalid_loop_state = {"schema_version": "loop_state/v1", "iteration": 0}
    phase_state = make_phase_state("preflight")

    result = run_decide_with_phase_state(invalid_loop_state, phase_state)

    # Phase gate fires → exit 3 with router_error status
    assert result.returncode == 3, (
        f"Expected exit 3 (phase gate / inconsistent_state), got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "router_error" in result.stdout or "ISSUE_REFINEMENT_ROUTER_ERROR_V1" in result.stdout, (
        f"Expected ISSUE_REFINEMENT_ROUTER_ERROR_V1 in stdout:\n{result.stdout}"
    )
    assert "rebuild_phase_state" in result.stdout, (
        f"Expected NEXT_ACTION: rebuild_phase_state:\n{result.stdout}"
    )


def test_preflight_phase_gate_emits_router_error_v1():
    """
    AC2: ISSUE_REFINEMENT_ROUTER_ERROR_V1 を一回だけ出力し、
    NEXT_ACTION: rebuild_phase_state を返す。
    """
    loop_state = load_loop_state_fixture()
    phase_state = make_phase_state("preflight")

    result = run_decide_with_phase_state(loop_state, phase_state)

    assert result.returncode == 3
    assert "ISSUE_REFINEMENT_ROUTER_ERROR_V1" in result.stdout

    # Parse embedded JSON
    for line in result.stdout.splitlines():
        if line.startswith("ISSUE_REFINEMENT_ROUTER_ERROR_V1:"):
            payload_str = line[len("ISSUE_REFINEMENT_ROUTER_ERROR_V1:"):].strip()
            payload = json.loads(payload_str)
            assert payload["schema_version"] == "ISSUE_REFINEMENT_ROUTER_ERROR_V1"
            assert payload["status"] == "router_error"
            assert payload["reason_code"] == "phase_not_allowed"
            assert payload["phase"] == "preflight"
            assert payload["attempted_router"] == "decide_next_loop_action.py"
            assert payload["next_action"] == "rebuild_phase_state"
            break
    else:
        pytest.fail(f"ISSUE_REFINEMENT_ROUTER_ERROR_V1 line not found in:\n{result.stdout}")

    # Exactly one ISSUE_REFINEMENT_ROUTER_ERROR_V1 line
    error_lines = [l for l in result.stdout.splitlines() if "ISSUE_REFINEMENT_ROUTER_ERROR_V1" in l]
    assert len(error_lines) == 1, f"Expected exactly 1 error line, got {len(error_lines)}"


# ---------------------------------------------------------------------------
# AC1: preflight phase では human_escalation を返さず investigation/review へ進む
# ---------------------------------------------------------------------------


def test_preflight_phase_blocks_decide_not_human_escalation():
    """
    AC1: preflight phase では decide_next_loop_action.py が phase gate でブロックされるため、
    scope_signal_guard.triggered: true が含まれていても human_escalation は返さない。
    代わりに ISSUE_REFINEMENT_ROUTER_ERROR_V1 (router_error) を返す。
    """
    loop_state = load_loop_state_fixture()
    loop_state["scope_signal_guard"] = {
        "triggered": True,
        "excluded_by_anchor_reframe": False,
        "reason_code": "new_in_scope_area",
    }
    phase_state = make_phase_state("preflight")

    result = run_decide_with_phase_state(loop_state, phase_state)

    # Must NOT return human_escalation status (that would be the bug we're fixing)
    assert "STATUS: human_escalation" not in result.stdout, (
        f"preflight phase should NOT return human_escalation:\n{result.stdout}"
    )
    # Must return router_error via phase gate
    assert "ISSUE_REFINEMENT_ROUTER_ERROR_V1" in result.stdout
    assert "rebuild_phase_state" in result.stdout


def test_preflight_phase_allows_continuation_outside_decide_router():
    """
    AC1: preflight phase では investigation | web_research | review へ進む必要がある。
    decide_next_loop_action.py は呼ばれないはずで、phase_state の allowed_routers には
    run_refinement_preflight.py / plan_refinement_loop.py が含まれること。
    """
    phase_state = make_phase_state("preflight")
    assert "run_refinement_preflight.py" in phase_state["allowed_routers"]
    assert "plan_refinement_loop.py" in phase_state["allowed_routers"]
    assert "decide_next_loop_action.py" in phase_state["forbidden_routers"]


# ---------------------------------------------------------------------------
# AC3: build_refinement_phase_state.py が phase-sensitive な hard_stop_eligible を設定する
# ---------------------------------------------------------------------------


def test_build_phase_state_preflight_hard_stop_eligible_false():
    """AC3: preflight phase では hard_stop_eligible: false を設定する。"""
    phase_state = make_phase_state("preflight")
    assert phase_state["scope_signal_semantics"]["hard_stop_eligible"] is False, (
        f"Expected hard_stop_eligible=false for preflight, got: "
        f"{phase_state['scope_signal_semantics']}"
    )
    assert phase_state["scope_signal_semantics"]["triggered_meaning"] == "continue_investigation"


def test_build_phase_state_post_rewrite_check_hard_stop_eligible_true():
    """AC3: post_rewrite_check phase では hard_stop_eligible: true を設定する。"""
    phase_state = make_phase_state(
        "post_rewrite_check",
        source_kind="loop_state_v1",
    )
    assert phase_state["scope_signal_semantics"]["hard_stop_eligible"] is True, (
        f"Expected hard_stop_eligible=true for post_rewrite_check, got: "
        f"{phase_state['scope_signal_semantics']}"
    )
    assert phase_state["scope_signal_semantics"]["triggered_meaning"] == "hard_stop_candidate"


def test_build_phase_state_decide_next_action_hard_stop_eligible_true():
    """AC3: decide_next_action phase では hard_stop_eligible: true を設定する。"""
    phase_state = make_phase_state(
        "decide_next_action",
        source_kind="loop_state_v1",
    )
    assert phase_state["scope_signal_semantics"]["hard_stop_eligible"] is True
    assert phase_state["scope_signal_semantics"]["triggered_meaning"] == "hard_stop_candidate"


def test_build_phase_state_investigation_hard_stop_eligible_false():
    """AC3: investigation phase では hard_stop_eligible: false を設定する。"""
    phase_state = make_phase_state("investigation")
    assert phase_state["scope_signal_semantics"]["hard_stop_eligible"] is False
    assert phase_state["scope_signal_semantics"]["triggered_meaning"] == "continue_investigation"


def test_build_phase_state_schema_version():
    """build_refinement_phase_state.py が正しい schema_version を出力する。"""
    phase_state = make_phase_state("preflight")
    assert phase_state["schema_version"] == "ISSUE_REFINEMENT_PHASE_STATE_V1"
    assert phase_state["phase"] == "preflight"


def test_build_phase_state_has_required_fields():
    """ISSUE_REFINEMENT_PHASE_STATE_V1 が必須フィールドをすべて持つ。"""
    phase_state = make_phase_state("review", source_kind="issue_review_result_compact_v1")
    required_fields = [
        "schema_version", "phase", "source_artifact",
        "loop_state_path", "planner_result_path", "review_result_path",
        "allowed_routers", "forbidden_routers", "scope_signal_semantics",
    ]
    for field in required_fields:
        assert field in phase_state, f"Missing field: {field!r}"

    assert "triggered_meaning" in phase_state["scope_signal_semantics"]
    assert "hard_stop_eligible" in phase_state["scope_signal_semantics"]


# ---------------------------------------------------------------------------
# AC5: 失敗パターン fixture — preflight pass → scope_signal_guard.triggered: true
#       → 誤った human_escalation の再発防止
# ---------------------------------------------------------------------------


def test_ac5_failure_pattern_preflight_pass_then_scope_signal_no_human_escalation():
    """
    AC5: 失敗パターン再現テスト。
    シナリオ: preflight が pass を返し、その結果に scope_signal_guard.triggered: true が
    含まれていたとき、オーケストレーターが誤って decide_next_loop_action.py を呼ぶと
    human_escalation になってしまうバグを防ぐ。

    Phase gate が正しく機能すれば、decide_next_loop_action.py は preflight phase で
    呼ばれた瞬間に ISSUE_REFINEMENT_ROUTER_ERROR_V1 を返し、
    human_escalation は返さない。
    """
    # Simulate loop state after preflight: scope_signal_guard triggered
    loop_state = load_loop_state_fixture()
    loop_state["scope_signal_guard"] = {
        "triggered": True,
        "excluded_by_anchor_reframe": False,
        "reason_code": "new_allowed_path_layer",
    }
    loop_state["iteration"] = 0
    loop_state["max_iterations"] = 3

    # Phase is "preflight" — decide_next_loop_action.py should NOT be called here
    phase_state = make_phase_state("preflight")

    # This is the bug scenario: calling decide_next_loop_action.py in preflight phase
    result = run_decide_with_phase_state(loop_state, phase_state)

    # The old bug: STATUS: human_escalation would appear here
    assert "STATUS: human_escalation" not in result.stdout, (
        "BUG REGRESSION: preflight phase returned human_escalation for "
        "scope_signal_guard.triggered=true. Phase gate should have blocked this.\n"
        f"stdout: {result.stdout}"
    )

    # The fix: phase gate fires → ISSUE_REFINEMENT_ROUTER_ERROR_V1
    assert "ISSUE_REFINEMENT_ROUTER_ERROR_V1" in result.stdout, (
        f"Expected ISSUE_REFINEMENT_ROUTER_ERROR_V1 in stdout:\n{result.stdout}"
    )
    assert "phase_not_allowed" in result.stdout, (
        f"Expected reason_code=phase_not_allowed:\n{result.stdout}"
    )
    assert "rebuild_phase_state" in result.stdout


def test_ac5_without_phase_gate_scope_signal_triggers_human_escalation():
    """
    AC5: phase gate なし（旧来の呼び出し方）では、preflight pass 後の
    scope_signal_guard.triggered: true が human_escalation を引き起こす
    (これが修正前の問題のある挙動)。
    Phase gate を付けると防げることの対比テスト。
    """
    loop_state = load_loop_state_fixture()
    loop_state["scope_signal_guard"] = {
        "triggered": True,
        "excluded_by_anchor_reframe": False,
        "reason_code": "new_allowed_path_layer",
    }

    # Without phase gate: old behavior — returns human_escalation
    result_without_gate = run_decide_without_phase_state(loop_state)
    assert result_without_gate.returncode == 2
    assert "STATUS: human_escalation" in result_without_gate.stdout

    # With phase gate in preflight: phase gate blocks
    phase_state = make_phase_state("preflight")
    result_with_gate = run_decide_with_phase_state(loop_state, phase_state)
    assert "STATUS: human_escalation" not in result_with_gate.stdout
    assert "ISSUE_REFINEMENT_ROUTER_ERROR_V1" in result_with_gate.stdout


# ---------------------------------------------------------------------------
# Phase gate does NOT block in allowed phases
# ---------------------------------------------------------------------------


def test_phase_gate_allows_decide_in_review_phase():
    """review phase では decide_next_loop_action.py が allowed_routers に含まれるため通過。"""
    loop_state = load_loop_state_fixture()
    loop_state["scope_signal_guard"]["triggered"] = False
    phase_state = make_phase_state(
        "review",
        source_kind="issue_review_result_compact_v1",
    )

    result = run_decide_with_phase_state(loop_state, phase_state, verdict="needs-fix")

    # Gate should pass → normal routing
    assert "ISSUE_REFINEMENT_ROUTER_ERROR_V1" not in result.stdout, (
        f"Phase gate should not fire in review phase:\n{result.stdout}"
    )
    assert result.returncode in (0, 1, 2), (
        f"Unexpected exit code {result.returncode}:\n{result.stdout}"
    )


def test_phase_gate_allows_decide_in_post_rewrite_check_phase():
    """post_rewrite_check phase では decide_next_loop_action.py が allowed_routers に含まれる。"""
    loop_state = load_loop_state_fixture()
    loop_state["scope_signal_guard"]["triggered"] = False
    phase_state = make_phase_state(
        "post_rewrite_check",
        source_kind="loop_state_v1",
    )

    result = run_decide_with_phase_state(loop_state, phase_state, verdict="approve")

    assert "ISSUE_REFINEMENT_ROUTER_ERROR_V1" not in result.stdout
    assert result.returncode == 0
    assert "proceed_to_step_4_5" in result.stdout


def test_phase_gate_allows_decide_in_decide_next_action_phase():
    """decide_next_action phase では decide_next_loop_action.py が allowed_routers に含まれる。"""
    loop_state = load_loop_state_fixture()
    loop_state["scope_signal_guard"]["triggered"] = False
    phase_state = make_phase_state(
        "decide_next_action",
        source_kind="loop_state_v1",
    )

    result = run_decide_with_phase_state(loop_state, phase_state, verdict="needs-fix")

    assert "ISSUE_REFINEMENT_ROUTER_ERROR_V1" not in result.stdout
    assert result.returncode in (0, 1, 2)


# ---------------------------------------------------------------------------
# Phase gate also blocks in investigation phase
# ---------------------------------------------------------------------------


def test_investigation_phase_also_blocks_decide():
    """investigation phase でも decide_next_loop_action.py は forbidden。"""
    loop_state = load_loop_state_fixture()
    phase_state = make_phase_state("investigation")

    result = run_decide_with_phase_state(loop_state, phase_state)

    assert "ISSUE_REFINEMENT_ROUTER_ERROR_V1" in result.stdout
    assert "phase_not_allowed" in result.stdout
    assert result.returncode == 3
