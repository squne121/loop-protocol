#!/usr/bin/env python3
"""
test_decide_next_loop_action.py

AC3: decide_next_loop_action.py determines next action from compact review
result fixture, and iteration limit exceeded → human escalation (exit 2).
"""

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

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


def test_approve_verdict_at_max_iterations_proceeds_to_step_4_5():
    """
    Regression: approve verdict at max_iterations must NOT escalate.
    Priority 2b must only trigger for needs-fix, not approve.
    iteration=2, max_iterations=3 (iteration+1 == max_iterations) with approve
    → NEXT_ACTION: proceed_to_step_4_5, exit 0.
    """
    state = load_fixture()
    state["iteration"] = 2
    state["max_iterations"] = 3
    result = run_script(state, verdict="approve")
    assert result.returncode == 0, (
        f"Expected exit 0 for approve+max_iterations, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "STATUS: pass" in result.stdout
    assert "NEXT_ACTION: proceed_to_step_4_5" in result.stdout


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


# ---------------------------------------------------------------------------
# Major 2: Additional input path tests
# ---------------------------------------------------------------------------

def test_loop_state_file_input(tmp_path):
    """--loop-state-file reads state from a JSON file."""
    state = load_fixture()
    state["iteration"] = 0
    state["max_iterations"] = 3
    state_file = tmp_path / "loop_state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--loop-state-file",
            str(state_file),
            "--review-result-verdict",
            "approve",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "NEXT_ACTION: proceed_to_step_4_5" in result.stdout


def test_stdin_input_path():
    """stdin JSON input is accepted when no --loop-state-file or --loop-state-json."""
    state = load_fixture()
    state["iteration"] = 0
    state["max_iterations"] = 3
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--review-result-verdict",
            "approve",
        ],
        input=json.dumps(state),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "NEXT_ACTION: proceed_to_step_4_5" in result.stdout


def test_loop_state_file_is_read_only(tmp_path):
    """--loop-state-file input must not be modified by the script."""
    state = load_fixture()
    state["iteration"] = 0
    state["max_iterations"] = 3
    state_file = tmp_path / "loop_state.json"
    original_content = json.dumps(state, indent=2)
    state_file.write_text(original_content, encoding="utf-8")
    mtime_before = state_file.stat().st_mtime
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--loop-state-file",
            str(state_file),
            "--review-result-verdict",
            "approve",
        ],
        capture_output=True,
        text=True,
    )
    assert state_file.read_text(encoding="utf-8") == original_content, (
        "loop state file was mutated by the script"
    )
    assert state_file.stat().st_mtime == mtime_before, (
        "loop state file mtime changed (file was written)"
    )


# ---------------------------------------------------------------------------
# Blocker 3: fail-close schema validation tests
# ---------------------------------------------------------------------------

def test_missing_jsonschema_fails_closed(monkeypatch):
    """jsonschema import failure → validate_loop_state returns (False, ...) → exit 3."""
    # Patch builtins.__import__ to raise ImportError for jsonschema
    original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    import builtins
    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "jsonschema":
            raise ImportError("mocked: jsonschema not available")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", mock_import)

    # Import the module fresh with patched import
    import importlib
    import sys as _sys

    # Remove cached module if present
    module_name = "decide_next_loop_action"
    for key in list(_sys.modules.keys()):
        if "decide_next_loop_action" in key:
            del _sys.modules[key]

    _sys.path.insert(0, str(SCRIPT.parent))
    try:
        import decide_next_loop_action as dna
        importlib.reload(dna)
        state = load_fixture()
        valid, err = dna.validate_loop_state(state)
        assert not valid, "expected validation to fail when jsonschema unavailable"
        assert "jsonschema" in err.lower()
    finally:
        _sys.path.pop(0)
        for key in list(_sys.modules.keys()):
            if "decide_next_loop_action" in key:
                del _sys.modules[key]


def test_missing_schema_file_fails_closed(monkeypatch, tmp_path):
    """Schema file unreadable → validate_loop_state returns (False, ...) → fail closed."""
    import sys as _sys
    for key in list(_sys.modules.keys()):
        if "decide_next_loop_action" in key:
            del _sys.modules[key]
    _sys.path.insert(0, str(SCRIPT.parent))
    try:
        import decide_next_loop_action as dna
        import importlib
        importlib.reload(dna)

        # Patch _SCHEMA_PATH to a non-existent file
        monkeypatch.setattr(dna, "_SCHEMA_PATH", tmp_path / "nonexistent_schema.json")

        state = load_fixture()
        valid, err = dna.validate_loop_state(state)
        assert not valid, "expected validation to fail when schema file is missing"
        assert "unavailable" in err.lower() or "schema" in err.lower()
    finally:
        _sys.path.pop(0)
        for key in list(_sys.modules.keys()):
            if "decide_next_loop_action" in key:
                del _sys.modules[key]


# ---------------------------------------------------------------------------
# Major 1: last_verdict conflict with CLI verdict → exit 3
# ---------------------------------------------------------------------------

def test_last_verdict_conflict_with_cli_verdict_is_inconsistent():
    """
    LOOP_STATE.last_verdict != --review-result-verdict (both non-null) → exit 3.
    """
    state = load_fixture()
    # state fixture has last_verdict: "needs-fix"
    state["last_verdict"] = "needs-fix"
    result = run_script(state, verdict="approve")
    assert result.returncode == 3, (
        f"Expected exit 3 for verdict conflict, got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "inconsistent_state" in result.stdout
    assert "last_verdict_conflict" in result.stdout


# ---------------------------------------------------------------------------
# Blocker 2: iteration boundary tests (4 cases, 0-indexed, case B semantics)
# iteration + 1 >= max_iterations → escalate
# iteration + 1 <  max_iterations → continue
# ---------------------------------------------------------------------------

def test_iteration_boundary_0_max_1_escalates():
    """(iteration=0, max=1): 0+1 >= 1 → escalate (exit 2)."""
    state = load_fixture()
    state["iteration"] = 0
    state["max_iterations"] = 1
    result = run_script(state, verdict="needs-fix")
    assert result.returncode == 2, (
        f"Expected exit 2 for (iter=0, max=1), got {result.returncode}\n"
        f"stdout: {result.stdout}"
    )
    assert "human_escalation" in result.stdout


def test_iteration_boundary_1_max_1_escalates():
    """(iteration=1, max=1): 1+1 >= 1 → escalate (exit 2)."""
    state = load_fixture()
    state["iteration"] = 1
    state["max_iterations"] = 1
    result = run_script(state, verdict="needs-fix")
    assert result.returncode == 2, (
        f"Expected exit 2 for (iter=1, max=1), got {result.returncode}\n"
        f"stdout: {result.stdout}"
    )
    assert "human_escalation" in result.stdout


def test_iteration_boundary_1_max_2_escalates():
    """(iteration=1, max=2): 1+1 >= 2 → escalate (exit 2)."""
    state = load_fixture()
    state["iteration"] = 1
    state["max_iterations"] = 2
    result = run_script(state, verdict="needs-fix")
    assert result.returncode == 2, (
        f"Expected exit 2 for (iter=1, max=2), got {result.returncode}\n"
        f"stdout: {result.stdout}"
    )
    assert "human_escalation" in result.stdout


def test_iteration_boundary_2_max_2_escalates():
    """(iteration=2, max=2): 2+1 >= 2 → escalate (exit 2)."""
    state = load_fixture()
    state["iteration"] = 2
    state["max_iterations"] = 2
    result = run_script(state, verdict="needs-fix")
    assert result.returncode == 2, (
        f"Expected exit 2 for (iter=2, max=2), got {result.returncode}\n"
        f"stdout: {result.stdout}"
    )
    assert "human_escalation" in result.stdout


# ---------------------------------------------------------------------------
# AC4: scope_signal_guard tests WITH phase condition
# (existing tests preserved above; these add phase-sensitive variants)
# ---------------------------------------------------------------------------


def _make_phase_state_for_test(
    phase: str,
    source_kind: str = "loop_state_v1",
) -> dict[Any, Any]:
    """Build an ISSUE_REFINEMENT_PHASE_STATE_V1 for testing."""
    # Import the builder directly to avoid subprocess overhead in these tests
    import importlib
    import sys as _sys
    build_script = SKILL_ROOT / "scripts" / "build_refinement_phase_state.py"

    import tempfile
    import subprocess
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as out_f:
        out_path = out_f.name

    result = subprocess.run(
        [
            _sys.executable,
            str(build_script),
            "--phase", phase,
            "--source-kind", source_kind,
            "--source-path", "/tmp/fake_source.json",
            "--output-path", out_path,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"build_refinement_phase_state.py failed: {result.stdout} {result.stderr}"
    )
    import json
    from pathlib import Path
    return json.loads(Path(out_path).read_text(encoding="utf-8"))


def _run_script_with_phase_state(
    loop_state: dict[Any, Any],
    phase_state: dict[Any, Any],
    verdict: str = "needs-fix",
) -> "subprocess.CompletedProcess":
    """Run decide_next_loop_action.py with --phase-state-file."""
    import json
    import subprocess
    import sys
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(phase_state, f)
        phase_state_path = f.name

    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--loop-state-json", json.dumps(loop_state),
            "--review-result-verdict", verdict,
            "--phase-state-file", phase_state_path,
        ],
        capture_output=True,
        text=True,
    )


def test_scope_signal_guard_triggered_in_post_rewrite_check_phase_escalates():
    """
    AC4: post_rewrite_check phase での scope_signal_guard.triggered=true は
    引き続き TERMINATION_CAUSE: human_judgment_required を出す (#919 回帰維持)。
    """
    state = load_fixture()
    state["scope_signal_guard"] = {
        "triggered": True,
        "excluded_by_anchor_reframe": False,
        "reason_code": "new_in_scope_area",
    }
    phase_state = _make_phase_state_for_test("post_rewrite_check")

    result = _run_script_with_phase_state(state, phase_state, verdict="needs-fix")

    # Should escalate (gate passes, routing proceeds to scope_signal_guard check)
    assert result.returncode == 2, (
        f"Expected exit 2 for scope_signal_guard in post_rewrite_check, "
        f"got {result.returncode}\nstdout: {result.stdout}"
    )
    assert "STATUS: human_escalation" in result.stdout
    assert "scope_signal_guard_triggered" in result.stdout
    # AC4: reason_code is in BLOCKERS, not TERMINATION_CAUSE
    assert "TERMINATION_CAUSE: human_judgment_required" in result.stdout
    assert "scope_signal_guard_reason_code:new_in_scope_area" in result.stdout


def test_scope_signal_guard_triggered_in_decide_next_action_phase_escalates():
    """
    AC4: decide_next_action phase での scope_signal_guard.triggered=true は
    引き続き human_escalation を出す (#919 回帰維持)。
    """
    state = load_fixture()
    state["scope_signal_guard"] = {
        "triggered": True,
        "excluded_by_anchor_reframe": False,
        "reason_code": "new_allowed_path_layer",
    }
    phase_state = _make_phase_state_for_test("decide_next_action")

    result = _run_script_with_phase_state(state, phase_state, verdict="needs-fix")

    assert result.returncode == 2
    assert "STATUS: human_escalation" in result.stdout
    assert "scope_signal_guard_triggered" in result.stdout
    assert "TERMINATION_CAUSE: human_judgment_required" in result.stdout


def test_scope_signal_guard_triggered_in_preflight_phase_blocked_by_gate():
    """
    AC4: preflight phase では phase gate が先に作動して
    ISSUE_REFINEMENT_ROUTER_ERROR_V1 を返す。
    scope_signal_guard テストの phase 条件補完。
    """
    state = load_fixture()
    state["scope_signal_guard"] = {
        "triggered": True,
        "excluded_by_anchor_reframe": False,
        "reason_code": "new_in_scope_area",
    }
    phase_state = _make_phase_state_for_test("preflight", source_kind="refinement_preflight_result_v1")

    result = _run_script_with_phase_state(state, phase_state, verdict="needs-fix")

    # Gate fires — no human_escalation from routing logic
    assert "STATUS: human_escalation" not in result.stdout
    assert "ISSUE_REFINEMENT_ROUTER_ERROR_V1" in result.stdout
    assert result.returncode == 3


def test_scope_signal_guard_reason_code_in_blockers_not_termination_cause():
    """
    AC4 / #919 回帰: scope_signal_guard.reason_code は BLOCKERS に保持され
    TERMINATION_CAUSE には使われない。
    """
    state = load_fixture()
    state["scope_signal_guard"] = {
        "triggered": True,
        "excluded_by_anchor_reframe": False,
        "reason_code": "new_allowed_path_layer",
    }
    # No phase gate (baseline behavior)
    result = run_script(state, verdict="needs-fix")

    assert result.returncode == 2
    assert "TERMINATION_CAUSE: human_judgment_required" in result.stdout
    # reason_code must NOT appear as TERMINATION_CAUSE
    assert "TERMINATION_CAUSE: new_allowed_path_layer" not in result.stdout
    assert "TERMINATION_CAUSE: scope_signal_guard_triggered" not in result.stdout
    # reason_code must appear in BLOCKERS
    assert "BLOCKERS: scope_signal_guard_reason_code:new_allowed_path_layer" in result.stdout
