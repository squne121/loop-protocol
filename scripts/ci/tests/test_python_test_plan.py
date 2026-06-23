"""Tests for the python-test plan loader (Issue #1064)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LOADER_PATH = _REPO_ROOT / "scripts" / "ci" / "python_test_plan.py"
_PLAN_PATH = _REPO_ROOT / ".github" / "ci" / "python-test-plan.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("python_test_plan_under_test", _LOADER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _write_plan(tmp_path, plan):
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan), encoding="utf-8")
    return p


def _valid_plan():
    return {
        "schema_version": "python_test_plan/v1",
        "targets": ["schemas/tests/", "tests/context_mode/"],
        "ignore": ["a/b.py"],
        "deselect": ["a/b.py::test_x"],
        "xdist": {"workers": "auto", "dist": "worksteal"},
    }


# --- Real SSOT plan invariants (the file shipped in the repo) ---


def test_real_plan_loads_and_has_expected_schema():
    plan = mod.load_plan(_PLAN_PATH)
    assert plan["schema_version"] == "python_test_plan/v1"
    assert isinstance(plan["targets"], list) and plan["targets"]


def test_real_plan_includes_schemas_tests_exactly_once():
    """Issue #1064 AC7: schemas/tests/ must appear exactly once in the scope argv."""
    plan = mod.load_plan(_PLAN_PATH)
    argv = mod.scope_argv(plan)
    assert argv.count("schemas/tests/") == 1


def test_real_plan_scope_argv_carries_ignore_and_deselect_flags():
    plan = mod.load_plan(_PLAN_PATH)
    argv = mod.scope_argv(plan)
    assert any(a.startswith("--ignore=") for a in argv)
    assert any(a.startswith("--deselect=") for a in argv)
    # The hook-discovery contract (Issue #1064 AC8) is preserved in the plan.
    assert "--ignore=.claude/hooks/tests/test_secret_boundary_contract.py" in argv


def test_real_plan_excludes_codex_dedicated_lane():
    """tests/codex/ runs in its own codex execpolicy step, not the unified suite."""
    plan = mod.load_plan(_PLAN_PATH)
    argv = mod.scope_argv(plan)
    assert not any(a.startswith("tests/codex") for a in argv)


# --- run_argv modes ---


def test_run_argv_parallel_has_workers_and_scheduler(tmp_path):
    plan = mod.load_plan(_write_plan(tmp_path, _valid_plan()))
    argv = mod.run_argv(plan, mode="parallel")
    assert argv[:4] == ["-n", "auto", "--dist", "worksteal"]
    assert "schemas/tests/" in argv


def test_run_argv_serial_forces_single_process(tmp_path):
    plan = mod.load_plan(_write_plan(tmp_path, _valid_plan()))
    argv = mod.run_argv(plan, mode="serial")
    assert argv[:2] == ["-n", "0"]
    assert "--dist" not in argv


# --- parallel_exclude / serial lane ---


def test_parallel_run_ignores_parallel_exclude(tmp_path):
    plan = _valid_plan()
    plan["parallel_exclude"] = ["pkg/test_timing.py"]
    loaded = mod.load_plan(_write_plan(tmp_path, plan))
    argv = mod.run_argv(loaded, mode="parallel")
    assert "--ignore=pkg/test_timing.py" in argv


def test_serial_lane_argv_runs_excluded_with_n0(tmp_path):
    plan = _valid_plan()
    plan["parallel_exclude"] = ["pkg/test_timing.py"]
    loaded = mod.load_plan(_write_plan(tmp_path, plan))
    argv = mod.serial_lane_argv(loaded)
    assert argv == ["-n", "0", "pkg/test_timing.py"]


def test_serial_lane_argv_empty_when_no_exclusions(tmp_path):
    loaded = mod.load_plan(_write_plan(tmp_path, _valid_plan()))
    assert mod.serial_lane_argv(loaded) == []


def test_scope_argv_still_covers_parallel_excluded(tmp_path):
    """Excluded tests stay in the collection scope (full ci_test_selection coverage)."""
    plan = _valid_plan()
    plan["targets"] = ["pkg/test_timing.py", "schemas/tests/"]
    plan["parallel_exclude"] = ["pkg/test_timing.py"]
    loaded = mod.load_plan(_write_plan(tmp_path, plan))
    assert "pkg/test_timing.py" in mod.scope_argv(loaded)


def test_bad_parallel_exclude_type_raises(tmp_path):
    plan = _valid_plan()
    plan["parallel_exclude"] = [123]
    with pytest.raises(mod.PlanError):
        mod.load_plan(_write_plan(tmp_path, plan))


def test_real_plan_serial_lane_has_debounce():
    """The shipped SSOT excludes the timing-sensitive debounce test from xdist."""
    plan = mod.load_plan(_PLAN_PATH)
    lane = mod.serial_lane_argv(plan)
    assert lane[:2] == ["-n", "0"]
    assert any("session_manifest_debounce" in a for a in lane)
    # And the parallel run ignores it.
    par = mod.run_argv(plan, mode="parallel")
    assert any(a.startswith("--ignore=") and "session_manifest_debounce" in a for a in par)


def test_run_argv_rejects_unknown_mode(tmp_path):
    plan = mod.load_plan(_write_plan(tmp_path, _valid_plan()))
    with pytest.raises(mod.PlanError):
        mod.run_argv(plan, mode="bogus")


# --- fail-closed validation ---


def test_missing_file_raises(tmp_path):
    with pytest.raises(mod.PlanError):
        mod.load_plan(tmp_path / "nope.json")


def test_bad_schema_version_raises(tmp_path):
    plan = _valid_plan()
    plan["schema_version"] = "python_test_plan/v0"
    with pytest.raises(mod.PlanError):
        mod.load_plan(_write_plan(tmp_path, plan))


def test_empty_targets_raises(tmp_path):
    plan = _valid_plan()
    plan["targets"] = []
    with pytest.raises(mod.PlanError):
        mod.load_plan(_write_plan(tmp_path, plan))


def test_non_string_target_raises(tmp_path):
    plan = _valid_plan()
    plan["targets"] = ["ok", 123]
    with pytest.raises(mod.PlanError):
        mod.load_plan(_write_plan(tmp_path, plan))


def test_invalid_json_raises(tmp_path):
    p = tmp_path / "plan.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(mod.PlanError):
        mod.load_plan(p)


def test_bad_dist_type_raises(tmp_path):
    plan = _valid_plan()
    plan["xdist"]["dist"] = 5
    with pytest.raises(mod.PlanError):
        mod.load_plan(_write_plan(tmp_path, plan))


# --- CLI emission formats ---


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, str(_LOADER_PATH), *args],
        capture_output=True,
        text=True,
    )


def test_cli_nul_format_is_nul_separated():
    proc = _run_cli("--emit", "scope-argv", "--format", "nul")
    assert proc.returncode == 0
    parts = [p for p in proc.stdout.split("\0") if p]
    assert "schemas/tests/" in parts


def test_cli_json_format_round_trips():
    proc = _run_cli("--emit", "run-argv", "--mode", "parallel", "--format", "json")
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data[:4] == ["-n", "auto", "--dist", "worksteal"]


def test_cli_fails_closed_on_bad_plan(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    proc = _run_cli("--plan", str(bad), "--emit", "scope-argv")
    assert proc.returncode == 2
