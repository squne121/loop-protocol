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


def test_real_plan_keeps_only_runtime_guard_in_dedicated_codex_lane():
    """The dedicated codex lane keeps only the runtime guard test isolated."""
    plan = mod.load_plan(_PLAN_PATH)
    argv = mod.scope_argv(plan)
    assert "tests/codex/test_execpolicy_matrix.py" in argv
    assert "tests/codex/test_local_main_branch_guard.py" not in argv


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


def _plan_with_exclude():
    plan = _valid_plan()
    plan["targets"] = ["pkg/", "schemas/tests/"]
    plan["ignore"] = ["pkg/skipme.py"]
    plan["deselect"] = ["pkg/test_a.py::test_x"]
    plan["parallel_exclude"] = ["pkg/test_timing.py"]
    return plan


def test_parallel_run_ignores_parallel_exclude(tmp_path):
    loaded = mod.load_plan(_write_plan(tmp_path, _plan_with_exclude()))
    argv = mod.run_argv(loaded, mode="parallel")
    assert "--ignore=pkg/test_timing.py" in argv


def test_serial_lane_argv_inherits_ignore_and_deselect(tmp_path):
    """Serial lane runs the excluded paths AND inherits plan ignore/deselect (no drift)."""
    loaded = mod.load_plan(_write_plan(tmp_path, _plan_with_exclude()))
    argv = mod.serial_lane_argv(loaded)
    assert argv[:3] == ["-n", "0", "pkg/test_timing.py"]
    assert "--ignore=pkg/skipme.py" in argv
    assert "--deselect=pkg/test_a.py::test_x" in argv


def test_serial_lane_argv_empty_when_no_exclusions(tmp_path):
    loaded = mod.load_plan(_write_plan(tmp_path, _valid_plan()))
    assert mod.serial_lane_argv(loaded) == []


def test_scope_argv_still_covers_parallel_excluded(tmp_path):
    """Excluded tests stay in the collection scope (covered by the 'pkg/' target)."""
    loaded = mod.load_plan(_write_plan(tmp_path, _plan_with_exclude()))
    # The parallel-excluded file is collected by the 'pkg/' directory target.
    assert "pkg/" in mod.scope_argv(loaded)
    assert mod._is_in_target_scope("pkg/test_timing.py", loaded["targets"])


def test_bad_parallel_exclude_type_raises(tmp_path):
    plan = _plan_with_exclude()
    plan["parallel_exclude"] = [123]
    with pytest.raises(mod.PlanError):
        mod.load_plan(_write_plan(tmp_path, plan))


def test_parallel_exclude_outside_target_scope_raises(tmp_path):
    plan = _valid_plan()
    plan["parallel_exclude"] = ["unrelated/test_x.py"]  # not under any target
    with pytest.raises(mod.PlanError):
        mod.load_plan(_write_plan(tmp_path, plan))


def test_parallel_exclude_overlapping_ignore_raises(tmp_path):
    plan = _plan_with_exclude()
    plan["ignore"] = ["pkg/test_timing.py"]  # same path as parallel_exclude
    with pytest.raises(mod.PlanError):
        mod.load_plan(_write_plan(tmp_path, plan))


def test_real_plan_serial_lane_has_debounce():
    """After #1141, debounce test is deterministic and stays in the parallel lane.

    Issue 1546 AC3 added
    scripts/agent-guards/tests/test_skill_runtime_exec_session_manifest.py to
    parallel_exclude to remove a real xdist race: its repo-tree snapshot window can
    be polluted by a concurrent worker running test_summarize_agent_transcript.py.
    That is the only entry expected in the real plan; the debounce test itself must
    NOT be excluded.
    """
    plan = mod.load_plan(_PLAN_PATH)
    lane = mod.serial_lane_argv(plan)
    assert lane == [
        "-n",
        "0",
        "scripts/agent-guards/tests/test_skill_runtime_exec_session_manifest.py",
        "--ignore=.claude/hooks/tests/test_secret_boundary_contract.py",
        "--deselect=.claude/hooks/tests/test_generate_session_manifest_from_hook.py::test_wrapper_stdout_is_silent_and_artifact_path_is_overridable",
        "--deselect=.claude/hooks/tests/test_generate_session_manifest_from_hook.py::test_wrapper_stderr_redacts_posix_windows_and_wsl_paths",
    ]
    par = mod.run_argv(plan, mode="parallel")
    assert not any(a.startswith("--ignore=") and "session_manifest_debounce" in a for a in par)
    assert "--ignore=scripts/agent-guards/tests/test_skill_runtime_exec_session_manifest.py" in par


def test_real_plan_uses_fixed_worker_count():
    """AC4 requires a fixed worker count (not -n auto, which is CPU-dependent)."""
    plan = mod.load_plan(_PLAN_PATH)
    assert mod.resolved_workers(plan) == 4
    par = mod.run_argv(plan, mode="parallel")
    assert par[:2] == ["-n", "4"]


def test_real_plan_paths_exist():
    plan = mod.load_plan(_PLAN_PATH)
    mod.assert_plan_paths_exist(plan, _REPO_ROOT)  # must not raise


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


def test_duplicate_target_raises(tmp_path):
    plan = _valid_plan()
    plan["targets"] = ["schemas/tests/", "schemas/tests/"]
    with pytest.raises(mod.PlanError):
        mod.load_plan(_write_plan(tmp_path, plan))


def test_invalid_json_raises(tmp_path):
    p = tmp_path / "plan.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(mod.PlanError):
        mod.load_plan(p)


@pytest.mark.parametrize("bad_dist", [5, "spread", "round-robin", True])
def test_bad_dist_raises(tmp_path, bad_dist):
    plan = _valid_plan()
    plan["xdist"]["dist"] = bad_dist
    with pytest.raises(mod.PlanError):
        mod.load_plan(_write_plan(tmp_path, plan))


@pytest.mark.parametrize("good_dist", ["load", "loadscope", "loadfile", "loadgroup", "worksteal"])
def test_valid_dist_accepted(tmp_path, good_dist):
    plan = _valid_plan()
    plan["xdist"]["dist"] = good_dist
    assert mod.load_plan(_write_plan(tmp_path, plan))["xdist"]["dist"] == good_dist


@pytest.mark.parametrize("bad_workers", [0, -1, True, False, "many", 1.5])
def test_bad_workers_raises(tmp_path, bad_workers):
    plan = _valid_plan()
    plan["xdist"]["workers"] = bad_workers
    with pytest.raises(mod.PlanError):
        mod.load_plan(_write_plan(tmp_path, plan))


@pytest.mark.parametrize("good_workers", ["auto", 1, 4, 16])
def test_valid_workers_accepted(tmp_path, good_workers):
    plan = _valid_plan()
    plan["xdist"]["workers"] = good_workers
    assert mod.load_plan(_write_plan(tmp_path, plan))["xdist"]["workers"] == good_workers


@pytest.mark.parametrize("bad_path", ["-p", "/abs/test_x.py", "../escape/test_x.py", "a/../b.py"])
def test_bad_target_path_raises(tmp_path, bad_path):
    plan = _valid_plan()
    plan["targets"] = ["schemas/tests/", bad_path]
    with pytest.raises(mod.PlanError):
        mod.load_plan(_write_plan(tmp_path, plan))


def test_assert_plan_paths_exist_raises_on_missing(tmp_path):
    plan = _valid_plan()
    plan["targets"] = ["definitely/missing/dir/"]
    loaded = mod.load_plan(_write_plan(tmp_path, plan))
    with pytest.raises(mod.PlanError):
        mod.assert_plan_paths_exist(loaded, _REPO_ROOT)


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
    # Real SSOT plan uses a fixed worker count + loadscope scheduler (AC4).
    assert data[:4] == ["-n", "4", "--dist", "loadscope"]


def test_cli_emit_workers_and_scheduler():
    assert _run_cli("--emit", "workers", "--format", "lines").stdout.strip() == "4"
    assert _run_cli("--emit", "scheduler", "--format", "lines").stdout.strip() == "loadscope"


def test_cli_check_paths_passes_on_real_plan():
    proc = _run_cli("--emit", "scope-argv", "--check-paths")
    assert proc.returncode == 0


def test_cli_fails_closed_on_bad_plan(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    proc = _run_cli("--plan", str(bad), "--emit", "scope-argv")
    assert proc.returncode == 2


def test_real_plan_includes_scope_rollup_graphql_pagination_v3_exactly_once():
    """Issue #1644: tests/codex/test_scope_rollup_graphql_pagination_v3.py must be
    registered in the python-test-plan SSOT exactly once (exact-one registration),
    both in the raw targets list and in the derived scope argv."""
    plan = mod.load_plan(_PLAN_PATH)
    path = "tests/codex/test_scope_rollup_graphql_pagination_v3.py"
    assert plan["targets"].count(path) == 1
    assert mod.scope_argv(plan).count(path) == 1


def test_real_plan_includes_pr_body_validator_exactly_once():
    plan = mod.load_plan(_PLAN_PATH)
    path = "scripts/tests/test_pr_body_validator.py"
    assert plan["targets"].count(path) == 1
    assert mod.scope_argv(plan).count(path) == 1
