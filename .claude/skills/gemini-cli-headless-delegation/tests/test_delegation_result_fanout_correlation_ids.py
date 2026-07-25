"""Hermetic regression tests for Issue #1753.

Covers AC1-AC6: `delegation_result/v1` top-level `parent_run_id` /
`subtask_id` / `attempt_id` propagation for `provider=agy` fan-out-correlated
requests, backward compatibility (`None` defaults) for standalone (non-fan-out)
requests, and `validate_agy_fanout_e2e_evidence.py` predicate_19
(`run_ids_consistent_across_all_artifacts`) read-side regression coverage via
`build_fanout_evidence_bundle.py`.

No live `agy` binary or network access is required: `_run_agy` is mocked
throughout (mirroring the pattern in `test_agy_provider.py` /
`test_agy_serena_fanout_correlation.py`).
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Module loading helpers (hermetic, no side-effects)
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
_RGH_PATH = _SCRIPTS_DIR / "run_gemini_headless.py"
_BUNDLER_PATH = _SCRIPTS_DIR / "build_fanout_evidence_bundle.py"
_VALIDATOR_PATH = _SCRIPTS_DIR / "validate_agy_fanout_e2e_evidence.py"
_REAL_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "references" / "serena-tool-manifest.json"


def _load_module(path: Path, register_name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(register_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[register_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


rgh = _load_module(_RGH_PATH, "run_gemini_headless_1753")
bundler = _load_module(_BUNDLER_PATH, "build_fanout_evidence_bundle_1753")
validator = _load_module(_VALIDATOR_PATH, "validate_agy_fanout_e2e_evidence_1753")


def _make_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=["agy", "-p", "test"], returncode=returncode, stdout=stdout, stderr=stderr)


def _agy_no_tools_request(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": "delegation_request_v1",
        "tool_profile": "no_tools",
        "provider": "agy",
        "prompt": "Return exactly: LOOP_AGY_SMOKE_OK",
        "objective": "Smoke test for fan-out correlation id propagation",
        "instructions": ["Return exactly: LOOP_AGY_SMOKE_OK", "Do not add any extra text"],
        "output_sections": ["response"],
        "context_files": [],
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# AC1: no_tools fan-out-correlated request -- top-level correlation ids
# ---------------------------------------------------------------------------


def test_run_delegation_agy_no_tools_propagates_correlation_ids() -> None:
    request = _agy_no_tools_request(
        parent_run_id="run-abc",
        subtask_id="no_tools",
        attempt_id="attempt-1",
    )
    with patch.object(rgh, "_run_agy", return_value=_make_completed(0, stdout="LOOP_AGY_SMOKE_OK")):
        result = rgh.run_delegation(request)

    assert result["schema"] == "delegation_result/v1"
    assert result["ok"] is True
    assert result["parent_run_id"] == "run-abc"
    assert result["subtask_id"] == "no_tools"
    assert result["attempt_id"] == "attempt-1"


# ---------------------------------------------------------------------------
# AC2: local_asset_research fan-out-correlated request -- top-level fields
# present independently of the existing nested local_asset_retrieval_metadata
# copy (Issue #1706).
# ---------------------------------------------------------------------------


def _setup_repo(tmp_path: Path, monkeypatch: Any) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setattr(rgh, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(rgh, "_validate_local_asset_research_settings", lambda: [])
    manifest_dest = repo_root / rgh.SERENA_TOOL_MANIFEST_RELATIVE_PATH
    manifest_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_REAL_MANIFEST_PATH, manifest_dest)
    return repo_root


def _write_alpha_file(repo_root: Path) -> Path:
    target_file = repo_root / "pkg" / "alpha_widget.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(
        "\n".join(["def alpha_widget():", "    return 'alpha widget result'", "", "def unrelated():", "    pass"])
        + "\n",
        encoding="utf-8",
    )
    return target_file


def test_run_delegation_local_asset_research_top_level_correlation_ids(tmp_path, monkeypatch) -> None:
    repo_root = _setup_repo(tmp_path, monkeypatch)
    _write_alpha_file(repo_root)

    request = {
        "schema": "delegation_request_v1",
        "tool_profile": "local_asset_research",
        "provider": "agy",
        "prompt": "Summarize the requested implementation lines.",
        "objective": "Investigate alpha_widget behaviour",
        "instructions": [
            "Summarize the requested implementation lines.",
            "Use only the provided target evidence.",
        ],
        "output_sections": ["response"],
        "evidence_targets": [
            {"path": "pkg/alpha_widget.py", "selector": {"kind": "line_range", "start_line": 1, "end_line": 2}}
        ],
        "parent_run_id": "run-abc",
        "subtask_id": "local_asset_research",
        "attempt_id": "attempt-1",
    }
    with patch.object(rgh, "_run_agy", return_value=_make_completed(0, stdout="LOOP_AGY_TARGETED_OK")):
        result = rgh.run_delegation(request, request_path=repo_root / "request.json")

    assert result["ok"] is True
    # Top-level (Issue #1753) -- independent of the nested copy below.
    assert result["parent_run_id"] == "run-abc"
    assert result["subtask_id"] == "local_asset_research"
    assert result["attempt_id"] == "attempt-1"
    # Pre-existing nested copy (Issue #1706) is untouched by this Issue.
    metadata = result["local_asset_retrieval_metadata"]
    assert metadata["parent_run_id"] == "run-abc"
    assert metadata["subtask_id"] == "local_asset_research"
    assert metadata["attempt_id"] == "attempt-1"


# ---------------------------------------------------------------------------
# AC3: standalone (non-fan-out) delegation request -- correlation ids default
# to None and all other existing keys/values are unchanged.
# ---------------------------------------------------------------------------


def test_run_delegation_standalone_request_correlation_ids_default_none() -> None:
    request = _agy_no_tools_request()  # No parent_run_id / subtask_id / attempt_id.
    assert "parent_run_id" not in request
    assert "subtask_id" not in request
    assert "attempt_id" not in request

    with patch.object(rgh, "_run_agy", return_value=_make_completed(0, stdout="LOOP_AGY_SMOKE_OK")):
        result = rgh.run_delegation(request)

    assert result["parent_run_id"] is None
    assert result["subtask_id"] is None
    assert result["attempt_id"] is None

    # Regression: the rest of the existing delegation_result/v1 key/value set
    # is unaffected by this Issue -- only the 3 new (None) keys are added.
    assert result["ok"] is True
    assert result["response_text"] == "LOOP_AGY_SMOKE_OK"
    assert result["provider"] == "agy"
    assert result["safety_mode"] == "degraded_wrapper_only"
    assert result["transport"] == "agy"
    assert result["tool_profile"] == "no_tools"
    assert result["exit_code"] == 0
    assert result["warnings"] == []
    assert result["model_chain"] == []
    assert result["model_downgrades"] == []
    assert result["attempts_by_model"] == {"agy-default": 1}


# ---------------------------------------------------------------------------
# AC4: _normalize_agy_result() exit_code != 0 branch also carries the
# correlation ids.
# ---------------------------------------------------------------------------


def test_normalize_agy_result_nonzero_exit_includes_correlation_ids() -> None:
    completed = _make_completed(returncode=1, stdout="", stderr="boom")
    result = rgh._normalize_agy_result(
        completed,
        tool_profile="no_tools",
        requested_model=None,
        parent_run_id="run-abc",
        subtask_id="grounded_research",
        attempt_id="attempt-2",
    )

    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert result["parent_run_id"] == "run-abc"
    assert result["subtask_id"] == "grounded_research"
    assert result["attempt_id"] == "attempt-2"


def test_normalize_agy_result_default_correlation_ids_are_none() -> None:
    """Callers that never pass the new keyword-only args (pre-#1753 direct
    callers, e.g. hermetic tests in test_agy_provider.py) still get None."""
    completed = _make_completed(returncode=0, stdout="ok")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)

    assert result["parent_run_id"] is None
    assert result["subtask_id"] is None
    assert result["attempt_id"] is None


# ---------------------------------------------------------------------------
# AC5 / AC6: build_fanout_evidence_bundle.py -> validate_agy_fanout_e2e_evidence.py
# predicate_19 (run_ids_consistent_across_all_artifacts) read-side regression.
# ---------------------------------------------------------------------------

_REQUIRED_PROFILES = ("no_tools", "grounded_research", "local_asset_research")


def _agy_delegation_result_fixture(*, parent_run_id: str, subtask_id: str, attempt_id: str) -> tuple[dict, dict]:
    """Build a real (non-fabricated) delegation_request_v1 / delegation_result/v1
    pair by running the actual run_gemini_headless.py code path (Issue #1753
    change under test) with a mocked `_run_agy` subprocess."""
    request = _agy_no_tools_request(
        parent_run_id=parent_run_id,
        subtask_id=subtask_id,
        attempt_id=attempt_id,
    )
    with patch.object(rgh, "_run_agy", return_value=_make_completed(0, stdout="LOOP_AGY_SMOKE_OK")):
        result = rgh.run_delegation(request)
    return request, result


def _build_bundle_for_predicate19(tmp_path: Path, *, parent_run_ids: dict[str, str]) -> dict[str, Any]:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "events.ndjson").write_text("", encoding="utf-8")

    manifest_results = []
    for profile in _REQUIRED_PROFILES:
        request, result = _agy_delegation_result_fixture(
            parent_run_id=parent_run_ids[profile],
            subtask_id=profile,
            attempt_id="attempt-1",
        )
        (run_dir / f"{profile}.request.json").write_text(json.dumps(request), encoding="utf-8")
        manifest_results.append({"subtask_id": profile, "result": result})

    manifest = {
        "schema": "delegation_fanout_result_v1",
        "parent_run_id": parent_run_ids["no_tools"],
        "results": manifest_results,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    original_fanout_request = {
        "schema": "delegation_fanout_request_v1",
        "max_workers": 3,
        "subtasks": [{"subtask_id": profile} for profile in _REQUIRED_PROFILES],
    }
    content = bundler.build_bundle_content(
        original_fanout_request=original_fanout_request,
        run_dir=run_dir,
    )
    # `_predicate_audit_and_correlation()`'s predicate_20 (unrelated to this
    # Issue's predicate_19 focus) reads `bundle["manifest"]`, which is only
    # populated by the full on-disk materialize/load round-trip. This test
    # calls `_predicate_audit_and_correlation()` directly on the in-memory
    # `build_bundle_content()` output (no materialization needed for
    # predicate_19), so a placeholder satisfies predicate_20's `len()` call
    # without asserting on its value.
    content["manifest"] = {}
    return content


def _predicate_19_result(bundle: dict[str, Any]):
    results = validator._predicate_audit_and_correlation(bundle)
    return next(r for r in results if r.predicate_id == "predicate_19")


def test_predicate_19_passes_with_consistent_correlation_ids(tmp_path) -> None:
    bundle = _build_bundle_for_predicate19(
        tmp_path,
        parent_run_ids={profile: "run-consistent" for profile in _REQUIRED_PROFILES},
    )
    p19 = _predicate_19_result(bundle)
    assert p19.status == "pass", p19.detail


def test_predicate_19_fails_with_inconsistent_correlation_ids(tmp_path) -> None:
    parent_run_ids = {profile: "run-consistent" for profile in _REQUIRED_PROFILES}
    # Negative control (tampering case): exactly one subtask carries a
    # different parent_run_id than the other two / the fanout request.
    parent_run_ids["grounded_research"] = "run-tampered"

    bundle = _build_bundle_for_predicate19(tmp_path, parent_run_ids=parent_run_ids)
    p19 = _predicate_19_result(bundle)
    assert p19.status == "fail", p19.detail
