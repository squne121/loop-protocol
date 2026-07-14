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

Issue #1507 AC24 (Scope Delta): `make_phase_state()` now auto-provisions a
valid REVIEW_COMPACT_VALIDATION_RESULT_V1 fixture file (validation_status:
valid) and passes `--review-validation-result-path` whenever phase="review"
and source_kind="issue_review_result_compact_v1", since
build_refinement_phase_state.py now requires that argument for this exact
(phase, source_kind) combination (structural validator-first gate). This
keeps all pre-existing AC1-AC5/B2/B3/M1/M3 call sites in this file working
unchanged; only the shared helper and the one raw (non-helper) subprocess
call in test_validate_router_in_phase_parametrize needed updating.
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


def write_valid_review_validation_result() -> str:
    """Write a minimal valid REVIEW_COMPACT_VALIDATION_RESULT_V1 fixture
    (Issue #1507 AC24) and return its path."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as f:
        json.dump(
            {
                "schema": "REVIEW_COMPACT_VALIDATION_RESULT_V1",
                "schema_version": "1",
                "validation_status": "valid",
                "envelope_kind": "approve",
            },
            f,
        )
        return f.name


def make_phase_state(
    phase: str,
    source_kind: str = "refinement_preflight_result_v1",
    source_path: str | None = None,
    review_validation_result_path: str | None = None,
) -> dict[str, Any]:
    """Build a minimal ISSUE_REFINEMENT_PHASE_STATE_V1 via build_refinement_phase_state.py.

    source_path defaults to a temporary file created automatically (M1: source_path must exist).

    Issue #1507 AC24: when phase="review" and
    source_kind="issue_review_result_compact_v1", a valid
    REVIEW_COMPACT_VALIDATION_RESULT_V1 fixture is auto-provisioned (unless
    the caller explicitly supplies review_validation_result_path) so that
    existing (phase, source_kind) call sites in this file keep passing
    unchanged under the new structural validator-first gate.
    """
    # Create a real temp file for source_path (M1 requires source_path to exist)
    _source_file = None
    if source_path is None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as sf:
            sf.write("{}")
            source_path = sf.name
        _source_file = source_path

    if (
        phase == "review"
        and source_kind == "issue_review_result_compact_v1"
        and review_validation_result_path is None
    ):
        review_validation_result_path = write_valid_review_validation_result()

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as out_f:
        out_path = out_f.name

    argv = [
        sys.executable,
        str(BUILD_SCRIPT),
        "--phase", phase,
        "--source-kind", source_kind,
        "--source-path", source_path,
        "--output-path", out_path,
    ]
    if review_validation_result_path is not None:
        argv += ["--review-validation-result-path", review_validation_result_path]

    result = subprocess.run(
        argv,
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
    error_lines = [ln for ln in result.stdout.splitlines() if "ISSUE_REFINEMENT_ROUTER_ERROR_V1" in ln]
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
        "review_validation_result_path",
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


def test_phase_gate_blocks_decide_in_review_phase():
    """
    B2: review phase は pre-rewrite phase であるため decide_next_loop_action.py は forbidden。
    Router Rule に従い allowed_routers に含まれない。
    """
    loop_state = load_loop_state_fixture()
    loop_state["scope_signal_guard"]["triggered"] = False
    phase_state = make_phase_state(
        "review",
        source_kind="issue_review_result_compact_v1",
    )

    result = run_decide_with_phase_state(loop_state, phase_state, verdict="needs-fix")

    # Gate must block → ISSUE_REFINEMENT_ROUTER_ERROR_V1
    assert "ISSUE_REFINEMENT_ROUTER_ERROR_V1" in result.stdout, (
        f"Phase gate should fire in review phase (pre-rewrite, decide is forbidden):\n{result.stdout}"
    )
    assert "phase_not_allowed" in result.stdout, (
        f"Expected reason_code=phase_not_allowed:\n{result.stdout}"
    )
    assert result.returncode == 3, (
        f"Expected exit 3 for review phase gate block, got {result.returncode}:\n{result.stdout}"
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


# ---------------------------------------------------------------------------
# B2: review phase での decide_next_loop_action.py は forbidden
# ---------------------------------------------------------------------------


def test_review_phase_forbids_decide_next_loop_action():
    """B2: review phase では decide_next_loop_action.py が forbidden_routers に含まれる。"""
    phase_state = make_phase_state("review", source_kind="issue_review_result_compact_v1")
    assert "decide_next_loop_action.py" not in phase_state["allowed_routers"], (
        "review phase must NOT have decide_next_loop_action.py in allowed_routers (B2)"
    )
    assert "decide_next_loop_action.py" in phase_state["forbidden_routers"], (
        "review phase must have decide_next_loop_action.py in forbidden_routers (B2)"
    )


def test_review_phase_hard_stop_eligible_is_false():
    """B2: review phase は pre-rewrite phase であるため hard_stop_eligible: false。"""
    phase_state = make_phase_state("review", source_kind="issue_review_result_compact_v1")
    assert phase_state["scope_signal_semantics"]["hard_stop_eligible"] is False, (
        f"review phase hard_stop_eligible must be false (B2), got: "
        f"{phase_state['scope_signal_semantics']}"
    )
    assert phase_state["scope_signal_semantics"]["triggered_meaning"] == "continue_investigation"


# ---------------------------------------------------------------------------
# B3: allowlist gate — empty allowed_routers blocks all routers
# ---------------------------------------------------------------------------


def test_allowlist_gate_empty_allowed_routers_blocks_all():
    """B3: allowed_routers=[] → すべての router が blocked（fail-closed）。"""
    loop_state = load_loop_state_fixture()
    # Manually craft a phase_state with empty allowed_routers
    phase_state_with_empty_allowed = {
        "schema_version": "ISSUE_REFINEMENT_PHASE_STATE_V1",
        "phase": "terminate",
        "source_artifact": {"kind": "loop_state_v1", "path": "/tmp/x.json"},
        "loop_state_path": None,
        "planner_result_path": None,
        "review_result_path": None,
        "allowed_routers": [],
        "forbidden_routers": ["decide_next_loop_action.py"],
        "scope_signal_semantics": {
            "triggered_meaning": "ignored",
            "hard_stop_eligible": False,
        },
    }

    result = run_decide_with_phase_state(loop_state, phase_state_with_empty_allowed)

    assert result.returncode == 3, (
        f"Expected exit 3 when allowed_routers=[] (B3 fail-closed), got {result.returncode}\n"
        f"stdout: {result.stdout}"
    )
    assert "ISSUE_REFINEMENT_ROUTER_ERROR_V1" in result.stdout
    assert "phase_not_allowed" in result.stdout


def test_allowlist_gate_missing_from_forbidden_but_not_in_allowed_is_blocked():
    """
    B3: forbidden_routers に含まれなくても allowed_routers に含まれなければ blocked。
    旧 denylist ベースでは through できたが allowlist ベースでは blocked になる。
    """
    loop_state = load_loop_state_fixture()
    # decide_next_loop_action.py を forbidden_routers から除外し、
    # かつ allowed_routers にも含めない → allowlist gate で blocked になるべき
    phase_state_denylist_bypass = {
        "schema_version": "ISSUE_REFINEMENT_PHASE_STATE_V1",
        "phase": "rewrite",
        "source_artifact": {"kind": "loop_state_v1", "path": "/tmp/x.json"},
        "loop_state_path": None,
        "planner_result_path": None,
        "review_result_path": None,
        "allowed_routers": ["decide_rewrite_route.py"],  # decide_next_loop_action not here
        "forbidden_routers": [],  # also NOT in forbidden — denylist would pass this
        "scope_signal_semantics": {
            "triggered_meaning": "ignored",
            "hard_stop_eligible": False,
        },
    }

    result = run_decide_with_phase_state(loop_state, phase_state_denylist_bypass)

    # Allowlist gate: decide_next_loop_action.py not in allowed_routers → blocked
    assert result.returncode == 3, (
        f"Expected exit 3 (allowlist gate blocks non-allowed router), got {result.returncode}\n"
        f"stdout: {result.stdout}"
    )
    assert "ISSUE_REFINEMENT_ROUTER_ERROR_V1" in result.stdout


# ---------------------------------------------------------------------------
# M1: build_refinement_phase_state.py の source_path 存在チェック
# ---------------------------------------------------------------------------


def test_build_phase_state_missing_source_path_returns_error():
    """M1: source_path が存在しない場合は STATUS: error を返す。"""
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as out_f:
        out_path = out_f.name

    result = subprocess.run(
        [
            sys.executable,
            str(BUILD_SCRIPT),
            "--phase", "preflight",
            "--source-kind", "refinement_preflight_result_v1",
            "--source-path", "/tmp/nonexistent_source_path_12345.json",
            "--output-path", out_path,
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, (
        f"Expected exit 1 for missing source_path (M1), got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "STATUS: error" in result.stdout or "error" in result.stdout.lower(), (
        f"Expected error message:\n{result.stdout}"
    )


def test_build_phase_state_source_kind_phase_mismatch_returns_error(tmp_path):
    """M1: source_kind と phase の不整合でエラーを返す。"""
    # refinement_preflight_result_v1 は preflight / investigation でのみ有効
    # post_rewrite_check phase で使うと不整合
    source_file = tmp_path / "source.json"
    source_file.write_text("{}", encoding="utf-8")
    out_file = tmp_path / "out.json"

    result = subprocess.run(
        [
            sys.executable,
            str(BUILD_SCRIPT),
            "--phase", "post_rewrite_check",
            "--source-kind", "refinement_preflight_result_v1",
            "--source-path", str(source_file),
            "--output-path", str(out_file),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, (
        f"Expected exit 1 for source_kind/phase mismatch (M1), got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "error" in result.stdout.lower() or "STATUS: error" in result.stdout, (
        f"Expected error for kind/phase mismatch:\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# Issue #1507 AC24: review-phase validator-first structural gate
# ---------------------------------------------------------------------------


def test_build_phase_state_review_phase_requires_validation_result_path(tmp_path):
    """AC24: --phase review + --source-kind issue_review_result_compact_v1
    without --review-validation-result-path returns STATUS: error and
    writes no phase-state file."""
    source_file = tmp_path / "source.json"
    source_file.write_text("{}", encoding="utf-8")
    out_file = tmp_path / "out.json"

    result = subprocess.run(
        [
            sys.executable,
            str(BUILD_SCRIPT),
            "--phase", "review",
            "--source-kind", "issue_review_result_compact_v1",
            "--source-path", str(source_file),
            "--output-path", str(out_file),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, (
        f"Expected exit 1 when --review-validation-result-path is missing "
        f"(AC24), got {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert not out_file.exists(), "phase-state file must NOT be written (AC24 fail-closed)"


def test_build_phase_state_review_phase_rejects_invalid_validation_status(tmp_path):
    """AC24: a review-validation-result file whose validation_status is not
    'valid' also blocks phase-state generation."""
    source_file = tmp_path / "source.json"
    source_file.write_text("{}", encoding="utf-8")
    validation_file = tmp_path / "validation.json"
    validation_file.write_text(
        json.dumps({"validation_status": "invalid"}), encoding="utf-8"
    )
    out_file = tmp_path / "out.json"

    result = subprocess.run(
        [
            sys.executable,
            str(BUILD_SCRIPT),
            "--phase", "review",
            "--source-kind", "issue_review_result_compact_v1",
            "--source-path", str(source_file),
            "--review-validation-result-path", str(validation_file),
            "--output-path", str(out_file),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert not out_file.exists(), "phase-state file must NOT be written (AC24 fail-closed)"


def test_build_phase_state_review_phase_with_valid_validation_result_succeeds(tmp_path):
    """AC24: a valid REVIEW_COMPACT_VALIDATION_RESULT_V1 (validation_status:
    valid) allows phase-state generation to proceed as before."""
    source_file = tmp_path / "source.json"
    source_file.write_text("{}", encoding="utf-8")
    validation_file = tmp_path / "validation.json"
    validation_file.write_text(
        json.dumps({"validation_status": "valid"}), encoding="utf-8"
    )
    out_file = tmp_path / "out.json"

    result = subprocess.run(
        [
            sys.executable,
            str(BUILD_SCRIPT),
            "--phase", "review",
            "--source-kind", "issue_review_result_compact_v1",
            "--source-path", str(source_file),
            "--review-validation-result-path", str(validation_file),
            "--output-path", str(out_file),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["review_validation_result_path"] == str(validation_file)


def test_build_phase_state_post_rewrite_check_does_not_require_validation_result_path(tmp_path):
    """AC24 Out of Scope: post_rewrite_check phase (also allowed for
    issue_review_result_compact_v1 per _SOURCE_KIND_ALLOWED_PHASES) does NOT
    require --review-validation-result-path -- the gate applies only to the
    review phase."""
    source_file = tmp_path / "source.json"
    source_file.write_text("{}", encoding="utf-8")
    out_file = tmp_path / "out.json"

    result = subprocess.run(
        [
            sys.executable,
            str(BUILD_SCRIPT),
            "--phase", "post_rewrite_check",
            "--source-kind", "issue_review_result_compact_v1",
            "--source-path", str(source_file),
            "--output-path", str(out_file),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert out_file.exists()


# ---------------------------------------------------------------------------
# M3: validate_refinement_phase_transition.py の phase table テスト (parametrize)
# ---------------------------------------------------------------------------


VALIDATE_SCRIPT = SKILL_ROOT / "scripts" / "validate_refinement_phase_transition.py"


@pytest.mark.parametrize("phase,router,expected_allowed", [
    # preflight → decide_next_loop_action.py は forbidden
    ("preflight", "decide_next_loop_action.py", False),
    ("preflight", "run_refinement_preflight.py", True),
    ("preflight", "plan_refinement_loop.py", True),
    # investigation → decide_next_loop_action.py は forbidden
    ("investigation", "decide_next_loop_action.py", False),
    ("investigation", "run_refinement_preflight.py", True),
    # review → decide_next_loop_action.py は forbidden (B2)
    ("review", "decide_next_loop_action.py", False),
    ("review", "decide_rewrite_route.py", True),
    # post_rewrite_check → decide_next_loop_action.py は allowed
    ("post_rewrite_check", "decide_next_loop_action.py", True),
    ("post_rewrite_check", "decide_rewrite_route.py", True),
    # decide_next_action → decide_next_loop_action.py は allowed
    ("decide_next_action", "decide_next_loop_action.py", True),
    # rewrite → decide_next_loop_action.py は forbidden
    ("rewrite", "decide_next_loop_action.py", False),
    ("rewrite", "decide_rewrite_route.py", True),
    # publish → decide_next_loop_action.py は forbidden
    ("publish", "decide_next_loop_action.py", False),
    ("publish", "publish_termination_report.py", True),
    # terminate → すべて forbidden
    ("terminate", "decide_next_loop_action.py", False),
])
def test_validate_router_in_phase_parametrize(phase, router, expected_allowed, tmp_path):
    """M3: 全 phase の allowed/forbidden router に対する parametrized 検証テーブル。"""
    # Build source kind appropriate for the phase
    source_kind_map = {
        "preflight": "refinement_preflight_result_v1",
        "investigation": "refinement_preflight_result_v1",
        "review": "issue_review_result_compact_v1",
        "rewrite": "issue_author_result_compact_v1",
        "post_rewrite_check": "loop_state_v1",
        "decide_next_action": "loop_state_v1",
        "publish": "loop_state_v1",
        "terminate": "loop_state_v1",
    }
    source_kind = source_kind_map.get(phase, "loop_state_v1")
    source_file = tmp_path / "source.json"
    source_file.write_text("{}", encoding="utf-8")

    # Build phase state
    out_file = tmp_path / f"phase_state_{phase}.json"
    build_argv = [
        sys.executable,
        str(BUILD_SCRIPT),
        "--phase", phase,
        "--source-kind", source_kind,
        "--source-path", str(source_file),
        "--output-path", str(out_file),
    ]
    # Issue #1507 AC24: review phase + issue_review_result_compact_v1 requires
    # a valid REVIEW_COMPACT_VALIDATION_RESULT_V1 fixture.
    if phase == "review" and source_kind == "issue_review_result_compact_v1":
        validation_file = tmp_path / "review_validation.json"
        validation_file.write_text(
            json.dumps({"validation_status": "valid"}), encoding="utf-8"
        )
        build_argv += ["--review-validation-result-path", str(validation_file)]

    result_build = subprocess.run(
        build_argv,
        capture_output=True,
        text=True,
    )
    assert result_build.returncode == 0, (
        f"build failed for phase={phase!r}:\n{result_build.stdout}\n{result_build.stderr}"
    )

    # Validate router against phase state
    result_validate = subprocess.run(
        [
            sys.executable,
            str(VALIDATE_SCRIPT),
            "--phase-state-file", str(out_file),
            "--attempted-router", router,
        ],
        capture_output=True,
        text=True,
    )

    if expected_allowed:
        assert result_validate.returncode == 0, (
            f"Expected router {router!r} to be ALLOWED in phase {phase!r}, "
            f"but got exit {result_validate.returncode}:\n{result_validate.stdout}"
        )
        assert "STATUS: allowed" in result_validate.stdout
    else:
        assert result_validate.returncode == 1, (
            f"Expected router {router!r} to be FORBIDDEN in phase {phase!r}, "
            f"but got exit {result_validate.returncode}:\n{result_validate.stdout}"
        )
        assert "STATUS: forbidden" in result_validate.stdout
