"""Fixed live trial plan for the local_asset_research route (Issue #2015 AC8).

This module runs a *pre-registered* trial plan against the live Serena MCP
integration -- no post-hoc trial exclusion is permitted. The plan is fixed
at:

- collector-only (``_collect_live_serena_read_only_evidence`` invoked
  directly against the checked-in repository): 2 cold trials (fresh
  ``uvx``-resolved subprocess with no local cache assumed) + 10 warm trials
  (subsequent invocations, cache-warm).
- full route (``scripts/agent-ops/run_agent_provider_route_smoke.py``,
  verification-only producer -- not modified by this Issue): 3 trials for
  each of ``claude_code`` / ``codex_cli``.

If the live environment does not provide a working ``agy`` CLI + Serena MCP
stack (no network / no auth / binary missing), this module fails closed via
``pytest.skip()`` -- it never substitutes a fabricated PASS for a genuine
live result, and it never silently treats an unrelated failure (AGY
provider auth, GH_TOKEN, etc, per Issue #2015 Stop Conditions) as a
Serena-specific outcome.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"

# Issue #2015 AC8: fixed trial plan, no post-hoc exclusion.
COLLECTOR_COLD_TRIALS = 2
COLLECTOR_WARM_TRIALS = 10
FULL_ROUTE_TRIALS_PER_RUNTIME = 3
FULL_ROUTE_RUNTIMES = ("claude_code", "codex_cli")

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts" / "local_asset_research_live_trial"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_gemini_headless", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _live_environment_available(module: Any) -> tuple[bool, str]:
    """Preflight: is a genuine live Serena MCP + agy CLI stack reachable?

    Returns (available, reason). This never fabricates availability -- a
    missing binary, missing/invalid MCP config, or missing pinned manifest
    all fail closed to "unavailable" rather than being probed further with
    a live subprocess (which would be its own, separately-timed, failure
    mode this Issue is trying to make observable, not hide).
    """
    if shutil.which("agy") is None:
        return False, "agy CLI not found on PATH (cli_missing)"
    settings_errors = module._validate_local_asset_research_settings(_REPO_ROOT)
    if settings_errors:
        return False, f"local_asset_research settings invalid: {settings_errors[0]}"
    try:
        manifest = module.load_serena_tool_manifest(_REPO_ROOT)
    except Exception as exc:  # noqa: BLE001 - preflight, report and skip
        return False, f"serena tool manifest unavailable: {exc}"
    if not manifest.get("pinned_ref"):
        return False, "serena tool manifest missing pinned_ref"
    return True, "ok"


def _run_collector_trial(module: Any, context_paths: list[Path], manifest: dict) -> dict[str, Any]:
    started = time.monotonic()
    try:
        documents, metadata = module._collect_live_serena_read_only_evidence(
            context_paths, _REPO_ROOT, manifest
        )
        return {
            "outcome": "succeeded",
            "elapsed_sec": time.monotonic() - started,
            "retrieval_status": metadata.get("retrieval_mode"),
            "evidence_record_count": len(documents),
            "failure_class": None,
        }
    except module.SerenaCollectorError as exc:
        return {
            "outcome": "failed",
            "elapsed_sec": time.monotonic() - started,
            "retrieval_status": None,
            "evidence_record_count": 0,
            "failure_class": exc.failure_class,
        }


def test_ac8_local_asset_research_collector_fixed_live_trial_plan() -> None:
    """AC8 (collector-only half of the fixed trial plan): 2 cold + 10 warm
    genuine live Serena MCP invocations against this repository. SKIPs
    (not PASS) when no live agy/Serena stack is available in this
    environment -- per Issue #2015 Stop Conditions, an unrelated
    availability gap is never reported as a Serena-specific PASS/FAIL."""
    module = _load_module()
    available, reason = _live_environment_available(module)
    if not available:
        pytest.skip(f"local_asset_research live trial unavailable: {reason}")

    manifest = module.load_serena_tool_manifest(_REPO_ROOT)
    context_paths = [_REPO_ROOT / "README.md"]

    cold_results = [_run_collector_trial(module, context_paths, manifest) for _ in range(COLLECTOR_COLD_TRIALS)]
    warm_results = [_run_collector_trial(module, context_paths, manifest) for _ in range(COLLECTOR_WARM_TRIALS)]
    all_results = cold_results + warm_results

    first_attempt_pass_count = sum(1 for r in all_results if r["outcome"] == "succeeded")
    final_pass_count = first_attempt_pass_count  # collector-only trials call the collector once each, no retry wrapper
    failure_class_distribution: dict[str, int] = {}
    for r in all_results:
        if r["failure_class"]:
            failure_class_distribution[r["failure_class"]] = failure_class_distribution.get(r["failure_class"], 0) + 1

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifact_path = ARTIFACT_DIR / "collector_only_trial_result.json"
    artifact_path.write_text(
        json.dumps(
            {
                "schema": "local_asset_research_live_trial_v1",
                "trial_kind": "collector_only",
                "cold_trials": COLLECTOR_COLD_TRIALS,
                "warm_trials": COLLECTOR_WARM_TRIALS,
                "first_attempt_pass_count": first_attempt_pass_count,
                "retry_recovered_pass_count": 0,
                "final_pass_count": final_pass_count,
                "failure_class_distribution": failure_class_distribution,
                "results": all_results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    assert final_pass_count == COLLECTOR_COLD_TRIALS + COLLECTOR_WARM_TRIALS, (
        f"local_asset_research collector live trial did not achieve genuine PASS on every fixed trial: "
        f"{failure_class_distribution}"
    )


def test_ac8_local_asset_research_full_route_fixed_live_trial_plan() -> None:
    """AC8 (full-route half of the fixed trial plan): 3 Claude Code + 3
    Codex CLI live executions of the local_asset_research route via the
    existing verification-only producer
    scripts/agent-ops/run_agent_provider_route_smoke.py (not modified by
    this Issue). SKIPs (not PASS) when unavailable."""
    module = _load_module()
    available, reason = _live_environment_available(module)
    if not available:
        pytest.skip(f"local_asset_research live trial unavailable: {reason}")

    import subprocess
    import sys as _sys
    import tempfile

    smoke_script = _REPO_ROOT / "scripts" / "agent-ops" / "run_agent_provider_route_smoke.py"
    if not smoke_script.exists():
        pytest.skip("run_agent_provider_route_smoke.py producer not present in this checkout")

    all_results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        for runtime in FULL_ROUTE_RUNTIMES:
            for _ in range(FULL_ROUTE_TRIALS_PER_RUNTIME):
                started = time.monotonic()
                proc = subprocess.run(
                    [
                        _sys.executable,
                        str(smoke_script),
                        "--runtime", runtime,
                        "--agent", "codebase-investigator",
                        "--profile", "local_asset_research",
                        "--output-dir", tmp_dir,
                        "--timeout-seconds", "180",
                    ],
                    cwd=str(_REPO_ROOT),
                    capture_output=True,
                    text=True,
                    timeout=200,
                )
                all_results.append(
                    {
                        "runtime": runtime,
                        "outcome": "succeeded" if proc.returncode == 0 else "failed",
                        "elapsed_sec": time.monotonic() - started,
                        "returncode": proc.returncode,
                        "stderr_tail": proc.stderr[-2000:],
                    }
                )

    first_attempt_pass_count = sum(1 for r in all_results if r["outcome"] == "succeeded")
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifact_path = ARTIFACT_DIR / "full_route_trial_result.json"
    artifact_path.write_text(
        json.dumps(
            {
                "schema": "local_asset_research_live_trial_v1",
                "trial_kind": "full_route",
                "trials_per_runtime": FULL_ROUTE_TRIALS_PER_RUNTIME,
                "runtimes": list(FULL_ROUTE_RUNTIMES),
                "first_attempt_pass_count": first_attempt_pass_count,
                "final_pass_count": first_attempt_pass_count,
                "results": all_results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    assert first_attempt_pass_count == FULL_ROUTE_TRIALS_PER_RUNTIME * len(FULL_ROUTE_RUNTIMES), (
        f"local_asset_research full-route live trial did not achieve genuine PASS on every fixed trial: {all_results}"
    )
