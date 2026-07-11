from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import run_refinement_preflight as preflight  # noqa: E402


def _source_cache_paths(source: Path) -> set[Path]:
    cache = Path(importlib.util.cache_from_source(str(source)))
    return {cache, cache.parent}


def test_given_provenance_compile_when_building_proof_then_source_cache_is_unchanged(
    tmp_path: Path,
) -> None:
    """GIVEN a planner source outside the repository
    WHEN provenance performs its syntax check twice
    THEN it records an in-process proof and creates no __pycache__ / pyc."""
    planner = tmp_path / "planner.py"
    planner.write_text("value = 1\n", encoding="utf-8")
    before = {path: path.exists() for path in _source_cache_paths(planner)}

    first = preflight.build_py_compile_proof(planner, REPO_ROOT)
    second = preflight.build_py_compile_proof(planner, REPO_ROOT)

    assert first["py_compile_status"] == second["py_compile_status"] == "pass"
    assert first["command"] == ["in_process", "compile", str(planner.resolve())]
    assert {path: path.exists() for path in _source_cache_paths(planner)} == before


def test_given_invalid_source_when_building_proof_then_failure_is_recorded_without_cache(
    tmp_path: Path,
) -> None:
    """GIVEN invalid planner source
    WHEN provenance checks syntax
    THEN it fails in the proof without emitting a bytecode cache."""
    planner = tmp_path / "planner.py"
    planner.write_text("def broken(:\n", encoding="utf-8")

    proof = preflight.build_py_compile_proof(planner, REPO_ROOT)

    assert proof["py_compile_status"] == "fail"
    assert not Path(importlib.util.cache_from_source(str(planner))).exists()


def test_given_runtime_guard_source_when_static_policy_checked_then_cache_is_not_excluded() -> None:
    """GIVEN the privileged runtime executor
    WHEN its race-tolerant roots are inspected
    THEN neither source cache paths nor the skill scripts directory is ignored."""
    exec_source = (
        REPO_ROOT / "scripts" / "agent-guards" / "skill_runtime_exec.py"
    ).read_text(encoding="utf-8")
    assert "__pycache__" not in exec_source
    assert "*.pyc" not in exec_source
    assert "issue-refinement-loop/scripts" not in exec_source
