"""Fixed live trial plan for the local_asset_research route (Issue #2015 AC8).

This module runs a *pre-registered* trial plan against the live Serena MCP
integration -- no post-hoc trial exclusion is permitted. The plan is fixed
at:

- collector-only (``_collect_live_serena_read_only_evidence`` invoked
  directly against the checked-in repository): 2 cold trials (each given
  its own fresh, empty ``UV_CACHE_DIR`` so ``uvx`` genuinely re-resolves
  dependencies rather than merely being the first N invocations against
  whatever cache already happens to exist on the host) + 10 warm trials
  (subsequent invocations, sharing one cache directory).
- full route (``scripts/agent-ops/run_agent_provider_route_smoke.py``,
  verification-only producer -- not modified by this Issue): 3 trials for
  each of ``claude_code`` / ``codex_cli``, each validated with the
  canonical ``scripts/agent-ops/validate_agent_provider_route_smoke.py``
  (also not modified by this Issue) rather than trusting the producer
  subprocess's exit code alone -- the producer's own contract is that exit
  code 0 only means "every requested route produced a well-formed
  artifact", not that any individual route passed.

If the live environment does not provide a working ``agy`` CLI + Serena MCP
stack (no network / no auth / binary missing), this module fails closed:
it never substitutes a fabricated PASS for a genuine live result, and it
never silently treats an unrelated failure (AGY provider auth, GH_TOKEN,
etc, per Issue #2015 Stop Conditions) as a Serena-specific outcome. Per the
Issue #2015 OWNER REQUEST_CHANGES review on PR #2044
(https://github.com/squne121/loop-protocol/pull/2044#issuecomment-5229719867),
an unavailable environment is never allowed to look identical to a genuine
PASS: before ``pytest.skip()`` is used, an explicit
``"ac8_status": "unavailable"`` artifact is written (distinct from the
``"ac8_status": "achieved"`` artifact a genuine run produces), so CI/human
review can tell "this environment could not run the trial" apart from
"the trial ran and every fixed trial passed" even though pytest itself
reports both as non-failing.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"
_SMOKE_SCRIPT = _REPO_ROOT / "scripts" / "agent-ops" / "run_agent_provider_route_smoke.py"
_VALIDATOR_SCRIPT = _REPO_ROOT / "scripts" / "agent-ops" / "validate_agent_provider_route_smoke.py"

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


def _git_head_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except OSError:
        return None
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def _collector_live_environment_available(module: Any) -> tuple[bool, str]:
    """Preflight for the collector-only half of the plan.

    Issue #2015 P1 fix (OWNER review #2044): a bare Serena collector
    invocation (``_collect_live_serena_read_only_evidence``) never shells
    out to ``agy`` -- it launches the pinned Serena MCP server directly.
    Requiring ``agy`` on PATH here was an unrelated, over-broad
    availability gate that could report "unavailable" for reasons that
    have nothing to do with whether the collector itself can run.
    """
    settings_errors = module._validate_local_asset_research_settings(_REPO_ROOT)
    if settings_errors:
        return False, f"local_asset_research settings invalid: {settings_errors[0]}"
    try:
        manifest = module.load_serena_tool_manifest(_REPO_ROOT)
    except Exception as exc:  # noqa: BLE001 - preflight, report and skip
        return False, f"serena tool manifest unavailable: {exc}"
    if not manifest.get("pinned_ref"):
        return False, "serena tool manifest missing pinned_ref"
    try:
        serena = module._load_serena_from_mcp_config(_REPO_ROOT)
        command = str(serena["command"])
    except Exception as exc:  # noqa: BLE001 - preflight, report and skip
        return False, f"serena MCP command unavailable: {exc}"
    if shutil.which(command) is None:
        return False, f"serena MCP launch command not found on PATH (cli_missing): {command}"
    return True, "ok"


def _full_route_live_environment_available(module: Any) -> tuple[bool, str]:
    """Preflight for the full-route half of the plan.

    Unlike the collector-only half, the full route goes through
    ``run_agent_provider_route_smoke.py`` -> ``run_gemini_headless.py
    --provider agy``, which does require a working ``agy`` CLI.
    """
    available, reason = _collector_live_environment_available(module)
    if not available:
        return False, reason
    if shutil.which("agy") is None:
        return False, "agy CLI not found on PATH (cli_missing)"
    return True, "ok"


def _write_unavailable_artifact(artifact_path: Path, *, trial_kind: str, reason: str) -> None:
    """Issue #2015 P1 fix (OWNER review #2044): record unavailability as an
    explicit, distinct machine-readable outcome (never fabricated PASS,
    and never silently indistinguishable from "ran and passed") before the
    caller falls back to ``pytest.skip()``."""
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(
            {
                "schema": "local_asset_research_live_trial_v1",
                "trial_kind": trial_kind,
                "ac8_status": "unavailable",
                "reason": reason,
                "head_sha": _git_head_sha(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _run_collector_trial(module: Any, context_paths: list[Path], manifest: dict) -> dict[str, Any]:
    started = time.monotonic()
    try:
        documents, metadata = module._collect_live_serena_read_only_evidence(
            context_paths, _REPO_ROOT, manifest
        )
        return {
            "outcome": "succeeded",
            "elapsed_sec": time.monotonic() - started,
            # Issue #2015 P1 fix (OWNER review #2044): this field previously
            # held metadata.get("retrieval_mode") (a field/value mismatch --
            # "retrieval_status" and "retrieval_mode" are two different
            # concepts). "succeeded" is this trial's actual retrieval
            # status; retrieval_mode is recorded separately below.
            "retrieval_status": "succeeded",
            "retrieval_mode": metadata.get("retrieval_mode"),
            "evidence_record_count": len(documents),
            "failure_class": None,
        }
    except module.SerenaCollectorError as exc:
        return {
            "outcome": "failed",
            "elapsed_sec": time.monotonic() - started,
            "retrieval_status": "failed",
            "retrieval_mode": None,
            "evidence_record_count": 0,
            "failure_class": exc.failure_class,
        }


def test_ac8_local_asset_research_collector_fixed_live_trial_plan(tmp_path) -> None:
    """AC8 (collector-only half of the fixed trial plan): 2 cold (each with
    its own fresh, empty UV_CACHE_DIR) + 10 warm genuine live Serena MCP
    invocations against this repository. SKIPs (not PASS) when no live
    Serena stack is available in this environment -- per Issue #2015 Stop
    Conditions, an unrelated availability gap is never reported as a
    Serena-specific PASS/FAIL, and an explicit unavailable-status artifact
    is written first so the skip is never indistinguishable from a
    genuine achieved PASS (Issue #2015 P1 fix, OWNER review #2044)."""
    module = _load_module()
    available, reason = _collector_live_environment_available(module)
    artifact_path = ARTIFACT_DIR / "collector_only_trial_result.json"
    if not available:
        _write_unavailable_artifact(artifact_path, trial_kind="collector_only", reason=reason)
        pytest.skip(f"local_asset_research collector live trial unavailable: {reason}")

    manifest = module.load_serena_tool_manifest(_REPO_ROOT)
    context_paths = [_REPO_ROOT / "README.md"]

    original_uv_cache_dir = os.environ.get("UV_CACHE_DIR")
    cold_results: list[dict[str, Any]] = []
    try:
        for trial_index in range(COLLECTOR_COLD_TRIALS):
            # Issue #2015 P1 fix (OWNER review #2044): a dedicated, empty
            # cache directory per cold trial -- "cold" previously just
            # meant "the first 2 of 12 invocations", sharing whatever
            # dependency-resolution cache already existed on the host.
            cold_cache_dir = tmp_path / f"uv-cache-cold-{trial_index}"
            cold_cache_dir.mkdir(parents=True, exist_ok=True)
            os.environ["UV_CACHE_DIR"] = str(cold_cache_dir)
            cold_results.append(_run_collector_trial(module, context_paths, manifest))
    finally:
        if original_uv_cache_dir is None:
            os.environ.pop("UV_CACHE_DIR", None)
        else:
            os.environ["UV_CACHE_DIR"] = original_uv_cache_dir

    warm_cache_dir = tmp_path / "uv-cache-warm"
    warm_cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.environ["UV_CACHE_DIR"] = str(warm_cache_dir)
        warm_results = [
            _run_collector_trial(module, context_paths, manifest) for _ in range(COLLECTOR_WARM_TRIALS)
        ]
    finally:
        if original_uv_cache_dir is None:
            os.environ.pop("UV_CACHE_DIR", None)
        else:
            os.environ["UV_CACHE_DIR"] = original_uv_cache_dir

    all_results = cold_results + warm_results

    first_attempt_pass_count = sum(1 for r in all_results if r["outcome"] == "succeeded")
    final_pass_count = first_attempt_pass_count  # collector-only trials call the collector once each, no retry wrapper
    failure_class_distribution: dict[str, int] = {}
    for r in all_results:
        if r["failure_class"]:
            failure_class_distribution[r["failure_class"]] = failure_class_distribution.get(r["failure_class"], 0) + 1

    ac8_status = "achieved" if final_pass_count == COLLECTOR_COLD_TRIALS + COLLECTOR_WARM_TRIALS else "not_achieved"
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        json.dumps(
            {
                "schema": "local_asset_research_live_trial_v1",
                "trial_kind": "collector_only",
                "ac8_status": ac8_status,
                "head_sha": _git_head_sha(),
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


def _run_full_route_trial(runtime: str, output_dir: Path) -> dict[str, Any]:
    """Run one live full-route trial and independently validate it.

    Issue #2015 P1 fix (OWNER review #2044): the producer subprocess's own
    exit code only means "every requested route produced a well-formed
    artifact" (regardless of pass/fail/skip) -- aggregate pass/fail
    judgement requires the canonical validator
    (``scripts/agent-ops/validate_agent_provider_route_smoke.py``), and
    beyond schema validity this also directly asserts on the
    ``status`` / ``failure_class`` / ``subject.head_sha`` fields of the
    written artifact and the ``local_asset_retrieval_metadata`` inside the
    raw ``delegation_result.json`` evidence the route smoke harness
    captured (``retrieval_status`` / ``retrieval_mode`` /
    ``evidence_record_count``).
    """
    started = time.monotonic()
    # Issue #2015 P1 fix (control-plane live re-run, 2026-08-09): the smoke
    # harness's own route-level deadline was widened from 180s to 300s (see
    # run_agent_provider_route_smoke.py's DEFAULT_ROUTE_HARNESS_TIMEOUT_SEC /
    # INITIAL_ATTEMPT_MAX_BUDGET_FRACTION comments) -- 180s did not leave a
    # genuine bounded retry a meaningful chance once the initial attempt's
    # own bounded artifact-materialization poll legitimately consumed
    # essentially the whole prior budget. This test's own outer subprocess
    # timeout is kept comfortably above the harness's own 300s deadline plus
    # process-teardown/cleanup headroom.
    producer_proc = subprocess.run(
        [
            sys.executable,
            str(_SMOKE_SCRIPT),
            "--runtime", runtime,
            "--agent", "codebase-investigator",
            "--profile", "local_asset_research",
            "--output-dir", str(output_dir),
            "--timeout-seconds", "300",
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=340,
    )
    elapsed_sec = time.monotonic() - started

    record: dict[str, Any] = {
        "runtime": runtime,
        "elapsed_sec": elapsed_sec,
        "producer_returncode": producer_proc.returncode,
        "producer_stderr_tail": producer_proc.stderr[-2000:],
        "outcome": "failed",
        "status": None,
        "failure_class": None,
        "retrieval_status": None,
        "retrieval_mode": None,
        "evidence_record_count": None,
        "artifact_head_sha": None,
        "head_sha_matches": False,
        "validator_ok": False,
        "validator_output_tail": None,
    }

    if producer_proc.returncode != 0:
        record["failure_class"] = "producer_nonzero_exit"
        return record

    artifact_path = output_dir / f"{runtime}-codebase-investigator-local_asset_research.json"
    if not artifact_path.exists():
        record["failure_class"] = "producer_artifact_missing"
        return record

    # Canonical validator (schema-level check on this single-trial output
    # directory -- the batch close-gate flags are intentionally omitted
    # here since a single per-trial directory is not the full 6-route
    # batch that gate is designed to check).
    validator_proc = subprocess.run(
        [sys.executable, str(_VALIDATOR_SCRIPT), "--artifacts-dir", str(output_dir)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    record["validator_ok"] = validator_proc.returncode == 0
    record["validator_output_tail"] = (validator_proc.stdout + validator_proc.stderr)[-2000:]

    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        record["failure_class"] = f"artifact_unreadable: {exc}"
        return record

    record["status"] = artifact.get("status")
    record["failure_class"] = artifact.get("failure_class")
    record["artifact_head_sha"] = (artifact.get("subject") or {}).get("head_sha")
    record["head_sha_matches"] = record["artifact_head_sha"] == _git_head_sha()

    evidence_dirs = sorted(output_dir.glob(f"{runtime}-codebase-investigator-local_asset_research-*-evidence"))
    local_asset_retrieval_metadata: dict[str, Any] | None = None
    for evidence_dir in evidence_dirs:
        result_path = evidence_dir / "delegation_result.json"
        if not result_path.exists():
            continue
        try:
            delegation_result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        local_asset_retrieval_metadata = delegation_result.get("local_asset_retrieval_metadata")
        break
    if isinstance(local_asset_retrieval_metadata, dict):
        record["retrieval_status"] = local_asset_retrieval_metadata.get("retrieval_status")
        record["retrieval_mode"] = local_asset_retrieval_metadata.get("retrieval_mode")
        record["evidence_record_count"] = local_asset_retrieval_metadata.get("evidence_record_count")

    record["outcome"] = (
        "succeeded"
        if (
            record["validator_ok"]
            and record["status"] == "pass"
            and record["failure_class"] is None
            and record["retrieval_status"] == "succeeded"
            and record["retrieval_mode"] == "live_serena_mcp"
            and isinstance(record["evidence_record_count"], int)
            and record["evidence_record_count"] > 0
            and record["head_sha_matches"]
        )
        else "failed"
    )
    return record


def test_ac8_local_asset_research_full_route_fixed_live_trial_plan() -> None:
    """AC8 (full-route half of the fixed trial plan): 3 Claude Code + 3
    Codex CLI live executions of the local_asset_research route via the
    existing verification-only producer
    scripts/agent-ops/run_agent_provider_route_smoke.py (not modified by
    this Issue), each independently validated with
    scripts/agent-ops/validate_agent_provider_route_smoke.py (also not
    modified) plus direct field-level assertions on the raw
    delegation_result.json evidence. SKIPs (not PASS) when unavailable,
    recording an explicit unavailable-status artifact first (Issue #2015
    P1 fix, OWNER review #2044)."""
    module = _load_module()
    available, reason = _full_route_live_environment_available(module)
    artifact_path = ARTIFACT_DIR / "full_route_trial_result.json"
    if not available:
        _write_unavailable_artifact(artifact_path, trial_kind="full_route", reason=reason)
        pytest.skip(f"local_asset_research full-route live trial unavailable: {reason}")

    if not _SMOKE_SCRIPT.exists():
        _write_unavailable_artifact(
            artifact_path, trial_kind="full_route",
            reason="run_agent_provider_route_smoke.py producer not present in this checkout",
        )
        pytest.skip("run_agent_provider_route_smoke.py producer not present in this checkout")
    if not _VALIDATOR_SCRIPT.exists():
        _write_unavailable_artifact(
            artifact_path, trial_kind="full_route",
            reason="validate_agent_provider_route_smoke.py validator not present in this checkout",
        )
        pytest.skip("validate_agent_provider_route_smoke.py validator not present in this checkout")

    all_results: list[dict[str, Any]] = []
    # Issue #2015 P1 fix (OWNER review #2044, full-route trial finding #3):
    # a live full-route trial reproduced genuine spawn+completion evidence
    # (both channels fired) yet the delegated child's evidence directory
    # stayed completely empty -- the child's own Bash tool calls (building
    # and running the delegation request) never actually executed. Direct
    # reproduction traced this to the evidence directory living under the
    # SYSTEM temp dir (``tempfile.TemporaryDirectory()`` with no ``dir=``,
    # i.e. typically ``/tmp/...`` on Linux) -- OUTSIDE the worktree Claude
    # Code treats as this session's allowed working directory, which its
    # own Bash tool boundary can refuse to write into regardless of the
    # ``codebase-investigator`` agent's own ``permissionMode: dontAsk``.
    # Placing the trial's temp directory INSIDE the repo (under the same
    # ``.claude/artifacts/`` tree ``run_agent_provider_route_smoke.py``
    # itself already defaults its own output directory to -- see
    # ``ARTIFACTS_ROOT`` in ``validate_agent_provider_route_smoke.py``)
    # keeps every absolute path this trial hands the delegated child
    # inside the worktree boundary. ``artifacts/`` is already `.gitignore`d
    # at any depth, so this leaves no untracked residue.
    _live_trial_tmp_root = _REPO_ROOT / ".claude" / "artifacts" / "agent-provider-route-live-trial-tmp"
    _live_trial_tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="local-asset-research-live-trial-", dir=str(_live_trial_tmp_root)
    ) as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        for runtime in FULL_ROUTE_RUNTIMES:
            for trial_index in range(FULL_ROUTE_TRIALS_PER_RUNTIME):
                trial_dir = tmp_dir / f"{runtime}-{trial_index}"
                trial_dir.mkdir(parents=True, exist_ok=True)
                all_results.append(_run_full_route_trial(runtime, trial_dir))

        first_attempt_pass_count = sum(1 for r in all_results if r["outcome"] == "succeeded")
        ac8_status = (
            "achieved"
            if first_attempt_pass_count == FULL_ROUTE_TRIALS_PER_RUNTIME * len(FULL_ROUTE_RUNTIMES)
            else "not_achieved"
        )
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "local_asset_research_live_trial_v1",
                    "trial_kind": "full_route",
                    "ac8_status": ac8_status,
                    "head_sha": _git_head_sha(),
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
