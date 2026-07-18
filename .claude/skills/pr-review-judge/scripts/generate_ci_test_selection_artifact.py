#!/usr/bin/env python3
"""
Generate ci_test_selection/v1 artifact for G1 gate verification.

Uses pytest --collect-only to get actual discovered tests (via a JSON-emitting plugin,
not stdout parsing), then compares with changed test files from an explicit base..head
git diff.

Target set / ignore / deselect come from the python-test plan SSOT
(.github/ci/python-test-plan.json) via the scripts/ci/python_test_plan.py loader, so
the executed pytest target set and the artifact ``pytest_argv`` cannot drift
(Issue #1064).

Fail-closed (Issue #1064 review):
- Collection: a non-zero collect exit, a timeout, or zero collected nodeids records
  ``collection_status`` and fails CI.
- Change detection: the git diff is run against EXPLICIT base/head SHAs (never the
  branch name ``main``, which is absent in a shallow checkout). A non-zero / timed-out /
  unprovided diff records ``diff_status`` and fails CI — it is never silently treated as
  "no changed tests" (the old fail-open false-green).
- G1 requires BOTH ``collection_status.ok`` AND ``diff_status.ok``.

Runtime-verification-only exemption (Issue #1562):
- A changed test file that is not collected by the default pytest run may still be
  legitimately exempt from ``uncovered_changed_test_files`` when ALL of its tests are
  marked with one of ``plan.runtime_verification_only_markers`` (e.g. ``github_live``)
  -- markers that are deselected from the default CI run (see pyproject.toml addopts)
  because they require an execution environment (e.g. authenticated ``gh`` CLI write
  access) that is structurally unavailable on the CI runner. This is verified by
  actually re-running ``pytest --collect-only`` scoped to that single file, both under
  the default marker filter (expected: 0 nodeids) and under an explicit
  ``-m "<marker1> or <marker2>..."`` override (expected: >=1 nodeids) -- never by
  filename/declaration alone (fail-closed). Files exempted this way are recorded in
  ``runtime_verification_only_test_files`` and are NOT added to
  ``uncovered_changed_test_files``. This is deliberately distinct from
  ``secondary_coverage.dedicated_lanes``, which declares coverage by a different CI job
  that actually executes the file -- no such job exists for runtime-verification-only
  markers.
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

COLLECT_TIMEOUT_SECONDS = 120
DIFF_TIMEOUT_SECONDS = 30
RUNTIME_VERIFICATION_COLLECT_TIMEOUT_SECONDS = 60

# pytest's default test-file convention (python_files = test_*.py / *_test.py). A path is
# a changed *test* file only if its basename matches this — not merely if "test" appears
# somewhere in the path (which would mis-flag source files like python_test_plan.py).
_PY_TEST_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$")
_TS_TEST_FILE_RE = re.compile(r"(?:^|/)[^/]*\.(?:test|spec)\.(?:ts|tsx|js|jsx)$")
_SCRIPTS_CI = Path(__file__).resolve().parents[4] / "scripts" / "ci"


def _load_plan_module():
    """Import the scripts/ci/python_test_plan.py loader by file location."""
    loader_path = _SCRIPTS_CI / "python_test_plan.py"
    spec = importlib.util.spec_from_file_location("python_test_plan", loader_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load python-test plan loader at {loader_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_pytest_args(args) -> List[str]:
    """Resolve the pytest scope argv (explicit --pytest-args wins, else plan SSOT)."""
    if getattr(args, "pytest_args", None):
        return list(args.pytest_args)
    plan_module = _load_plan_module()
    plan = plan_module.load_plan(getattr(args, "plan", None))
    return plan_module.scope_argv(plan)


def _normalize_coverage_path(path: str) -> str:
    normalized = path.rstrip("/")
    if not normalized:
        raise ValueError("secondary coverage path must not be empty")
    if normalized.startswith("/") or normalized.startswith("-"):
        raise ValueError(f"secondary coverage path must be relative: {path!r}")
    if ".." in Path(normalized).parts:
        raise ValueError(f"secondary coverage path must not contain '..': {path!r}")
    return normalized


def _is_covered_by_paths(path: str, coverage_paths: set[str]) -> bool:
    return any(path == covered or path.startswith(covered + "/") for covered in coverage_paths)


def _get_secondary_coverage(args) -> Tuple[Dict[str, set[str]], str | None]:
    plan_arg = getattr(args, "plan", None)
    if not plan_arg:
        return {}, None

    try:
        plan_module = _load_plan_module()
        plan_obj = plan_module.load_plan(plan_arg)
        providers: Dict[str, set[str]] = {}

        metadata = plan_obj.get("secondary_coverage", {})
        if metadata and not isinstance(metadata, dict):
            raise ValueError("plan.secondary_coverage must be an object")

        if getattr(args, "pytest_args", None):
            provider_job = metadata.get("plan_targets_provider_job", "python-test")
            if not isinstance(provider_job, str) or not provider_job:
                raise ValueError("plan.secondary_coverage.plan_targets_provider_job must be a string")
            providers.setdefault(provider_job, set()).update(
                _normalize_coverage_path(target) for target in plan_obj.get("targets", [])
            )

        dedicated_lanes = metadata.get("dedicated_lanes", [])
        if dedicated_lanes and not isinstance(dedicated_lanes, list):
            raise ValueError("plan.secondary_coverage.dedicated_lanes must be a list")
        for lane in dedicated_lanes:
            if not isinstance(lane, dict):
                raise ValueError("plan.secondary_coverage.dedicated_lanes entries must be objects")
            provider_job = lane.get("provider_job")
            if not isinstance(provider_job, str) or not provider_job:
                raise ValueError(
                    "plan.secondary_coverage.dedicated_lanes[].provider_job must be a string"
                )
            paths = lane.get("paths")
            if not isinstance(paths, list) or not paths:
                raise ValueError("plan.secondary_coverage.dedicated_lanes[].paths must be a list")
            providers.setdefault(provider_job, set()).update(
                _normalize_coverage_path(path) for path in paths
            )

        return providers, None
    except Exception as exc:
        return {}, str(exc)


def _get_runtime_verification_only_markers(args) -> Tuple[List[str], str | None]:
    """Load ``plan.runtime_verification_only_markers`` (Issue #1562).

    Returns (markers, error). An empty/absent list is not an error (no exemption
    mechanism configured); a malformed value is reported via the error slot and
    treated as no markers (fail-closed: nothing gets exempted).
    """
    plan_arg = getattr(args, "plan", None)
    if not plan_arg:
        return [], None
    try:
        plan_module = _load_plan_module()
        plan_obj = plan_module.load_plan(plan_arg)
        markers = plan_obj.get("runtime_verification_only_markers", [])
        if not isinstance(markers, list):
            raise ValueError("plan.runtime_verification_only_markers must be a list")
        for marker in markers:
            if not isinstance(marker, str) or not marker:
                raise ValueError(
                    "plan.runtime_verification_only_markers entries must be non-empty strings"
                )
        return list(markers), None
    except Exception as exc:
        return [], str(exc)


def check_runtime_verification_only_coverage(
    changed_file: str, markers: List[str]
) -> Tuple[bool, Dict[str, Any]]:
    """Confirm ``changed_file`` is a runtime-verification-only exempt file (Issue #1562).

    Fail-closed: this NEVER trusts the marker declaration alone. It actually re-runs
    ``pytest --collect-only`` scoped to exactly this file twice:
      1. under the default marker filter (addopts) -- must collect ZERO nodeids
         (otherwise the file is genuinely collected by the default run and this
         function should not have been called for it in the first place, OR it is
         only partially marker-exempt, which is not a clean exemption).
      2. under an explicit ``-m "<marker1> or <marker2>..."`` override -- must
         collect AT LEAST ONE nodeid (proof the file's tests actually exist and are
         reachable under the runtime-verification-only markers, not merely absent
         from the plan target set for an unrelated reason).

    Returns (is_exempt, evidence). evidence is always attached to the artifact for
    transparency (whether or not the file ends up exempted).
    """
    evidence: Dict[str, Any] = {
        "file": changed_file,
        "markers": list(markers),
        "default_collect_nodeid_count": None,
        "default_collect_ok": None,
        "marker_collect_nodeid_count": None,
        "marker_collect_ok": None,
        "exempt": False,
        "error": None,
    }
    if not markers:
        evidence["error"] = "no runtime_verification_only_markers configured in plan"
        return False, evidence

    _, _, default_status = get_pytest_collected_tests(
        [changed_file], timeout_seconds=RUNTIME_VERIFICATION_COLLECT_TIMEOUT_SECONDS
    )
    evidence["default_collect_nodeid_count"] = default_status["nodeid_count"]
    # A returncode of 5 ("no tests collected") with 0 nodeids is the EXPECTED
    # shape here, not a collection failure -- only a non-5/non-0 returncode or a
    # timeout is treated as an unreadable/broken probe (fail-closed).
    default_probe_ok = (
        not default_status["timed_out"]
        and default_status["returncode"] in (0, 5)
    )
    if not default_probe_ok:
        evidence["error"] = (
            f"default collect-only probe failed: returncode={default_status['returncode']} "
            f"timed_out={default_status['timed_out']}"
        )
        return False, evidence
    if default_status["nodeid_count"] > 0:
        evidence["error"] = (
            "file is collected under the default marker filter; not a "
            "runtime-verification-only candidate"
        )
        return False, evidence

    marker_expr = " or ".join(markers)
    _, marker_nodeids, marker_status = get_pytest_collected_tests(
        [changed_file, "-m", marker_expr],
        timeout_seconds=RUNTIME_VERIFICATION_COLLECT_TIMEOUT_SECONDS,
    )
    evidence["marker_collect_nodeid_count"] = marker_status["nodeid_count"]
    # Same shape as the default probe above: returncode 5 ("no tests collected") is a
    # valid probe outcome (zero matches), not a broken probe; only a non-5/non-0
    # returncode or a timeout means the probe itself could not be trusted.
    marker_probe_ok = (
        not marker_status["timed_out"]
        and marker_status["returncode"] in (0, 5)
    )
    evidence["marker_collect_ok"] = marker_probe_ok
    if not marker_probe_ok:
        evidence["error"] = (
            f"marker collect-only probe failed: returncode={marker_status['returncode']} "
            f"timed_out={marker_status['timed_out']}"
        )
        return False, evidence
    if marker_status["nodeid_count"] == 0:
        evidence["error"] = (
            "no tests collected under runtime_verification_only_markers either "
            "-- not a valid exemption"
        )
        return False, evidence

    evidence["exempt"] = True
    return True, evidence


def get_pytest_collected_tests(
    pytest_args: List[str],
    timeout_seconds: int = COLLECT_TIMEOUT_SECONDS,
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """Collect node IDs via the JSON-emitting plugin (no stdout parsing).

    Returns (collected_files, collected_nodeids, collection_status). collection_status is
    fail-closed evidence (collect exit code, timeout flag, stderr tail, nodeid count).
    """
    collection_status: Dict[str, Any] = {
        "returncode": None,
        "timed_out": False,
        "error": None,
        "nodeid_count": 0,
        "stderr_tail": "",
        "ok": False,
    }
    collected_nodeids: List[str] = []

    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SCRIPTS_CI) + os.pathsep + env.get("PYTHONPATH", "")
    out_path = None
    try:
        with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as fh:
            out_path = fh.name
        env["COLLECT_NODEIDS_OUT"] = out_path
        result = subprocess.run(
            ["uv", "run", "--locked", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider", "-p", "collect_nodeids_plugin"] + pytest_args,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
        collection_status["returncode"] = result.returncode
        collection_status["stderr_tail"] = "\n".join(result.stderr.strip().splitlines()[-20:])
        if result.returncode == 0 and Path(out_path).exists():
            data = json.loads(Path(out_path).read_text(encoding="utf-8"))
            collected_nodeids = list(data.get("nodeids", []))
    except subprocess.TimeoutExpired:
        collection_status["timed_out"] = True
        collection_status["error"] = "pytest --collect-only timed out"
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        collection_status["error"] = f"collection error: {exc}"
    finally:
        if out_path and Path(out_path).exists():
            try:
                os.unlink(out_path)
            except OSError:
                pass

    collected_files = sorted({nid.split("::")[0] for nid in collected_nodeids})
    collected_nodeids = sorted(collected_nodeids)
    collection_status["nodeid_count"] = len(collected_nodeids)
    collection_status["ok"] = (
        collection_status["returncode"] == 0
        and not collection_status["timed_out"]
        and collection_status["nodeid_count"] > 0
    )
    return collected_files, collected_nodeids, collection_status


def get_changed_test_files(
    base_sha: str, head_sha: str
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """Changed test files between EXPLICIT base/head SHAs (never branch name ``main``).

    Returns (changed_test_files, scope_excluded_files, diff_status). diff_status is
    fail-closed evidence: an unprovided SHA, a non-zero git exit, or a timeout sets
    ok=False so the caller fails CI rather than reporting an empty (false-green) set.
    """
    diff_status: Dict[str, Any] = {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "returncode": None,
        "timed_out": False,
        "error": None,
        "stderr_tail": "",
        "ok": False,
    }
    changed_test_files: List[str] = []
    scope_excluded_files: List[str] = []

    if not base_sha or not head_sha:
        diff_status["error"] = "base_sha and head_sha are both required for change detection"
        return changed_test_files, scope_excluded_files, diff_status

    try:
        result = subprocess.run(
            ["git", "diff", f"{base_sha}...{head_sha}", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
            timeout=DIFF_TIMEOUT_SECONDS,
        )
        diff_status["returncode"] = result.returncode
        diff_status["stderr_tail"] = "\n".join(result.stderr.strip().splitlines()[-20:])
        if result.returncode == 0:
            for f in result.stdout.split("\n"):
                if not f:
                    continue
                if f.endswith(".py") and _PY_TEST_FILE_RE.search(f):
                    changed_test_files.append(f)
                elif _TS_TEST_FILE_RE.search(f):
                    # Non-Python test files: tracked separately (out of python scope).
                    scope_excluded_files.append(f)
            diff_status["ok"] = True
    except subprocess.TimeoutExpired:
        diff_status["timed_out"] = True
        diff_status["error"] = "git diff timed out"
    except FileNotFoundError as exc:
        diff_status["error"] = f"git not found: {exc}"

    return sorted(changed_test_files), sorted(scope_excluded_files), diff_status


def get_current_head_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "unknown"


def generate_artifact(args):
    """Generate ci_test_selection/v1 artifact (fail-closed on collection/diff failure)."""
    pytest_args = resolve_pytest_args(args)

    collected_test_files, collected_nodeids, collection_status = (
        get_pytest_collected_tests(pytest_args)
    )

    pr_head_sha = args.pr_head_sha or "unknown"
    checked_out_sha = args.checked_out_sha or get_current_head_sha()
    merge_sha = args.merge_sha or "unknown"
    base_sha = getattr(args, "base_sha", None)
    head_sha = getattr(args, "head_sha", None) or args.pr_head_sha

    changed_test_files, scope_excluded, diff_status = get_changed_test_files(base_sha, head_sha)
    secondary_coverage_sources, secondary_coverage_error = _get_secondary_coverage(args)
    coverage_provider_jobs = sorted(
        provider_job
        for provider_job, coverage_paths in secondary_coverage_sources.items()
        if any(
            f not in collected_test_files and _is_covered_by_paths(f, coverage_paths)
            for f in changed_test_files
        )
    )
    secondary_coverage_provider_job: str | None
    if not coverage_provider_jobs:
        secondary_coverage_provider_job = None
    elif len(coverage_provider_jobs) == 1:
        secondary_coverage_provider_job = coverage_provider_jobs[0]
    else:
        secondary_coverage_provider_job = "multiple"

    cross_job_covered_test_files = sorted(
        f for f in changed_test_files
        if f not in collected_test_files
        and any(_is_covered_by_paths(f, paths) for paths in secondary_coverage_sources.values())
    )

    # Issue #1562: candidates for the runtime-verification-only exemption are changed
    # test files that are neither collected by the default run NOR covered by a
    # dedicated secondary-coverage CI lane. Each candidate is independently re-probed
    # (never trusted from a declaration alone) via check_runtime_verification_only_coverage.
    runtime_verification_only_markers, runtime_verification_only_markers_error = (
        _get_runtime_verification_only_markers(args)
    )
    runtime_verification_only_evidence: List[Dict[str, Any]] = []
    runtime_verification_only_test_files: List[str] = []
    if runtime_verification_only_markers:
        for f in changed_test_files:
            if f in collected_test_files:
                continue
            if any(_is_covered_by_paths(f, paths) for paths in secondary_coverage_sources.values()):
                continue
            is_exempt, evidence = check_runtime_verification_only_coverage(
                f, runtime_verification_only_markers
            )
            runtime_verification_only_evidence.append(evidence)
            if is_exempt:
                runtime_verification_only_test_files.append(f)
    runtime_verification_only_test_files = sorted(runtime_verification_only_test_files)

    uncovered = [
        f for f in changed_test_files
        if f not in collected_test_files
        and not any(_is_covered_by_paths(f, paths) for paths in secondary_coverage_sources.values())
        and f not in runtime_verification_only_test_files
    ]

    artifact = {
        "schema_version": "ci_test_selection/v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "pr_head_sha": pr_head_sha,
        "head_sha": pr_head_sha,  # Backward compatibility alias
        "checked_out_sha": checked_out_sha,
        "merge_sha": merge_sha,
        "scope": "python",
        "workflow": args.workflow,
        "job": args.job,
        "pytest_argv": pytest_args,
        "collection_status": collection_status,
        "diff_status": diff_status,
        "collected_test_files": collected_test_files,
        "collected_nodeids": collected_nodeids,
        "changed_test_files": changed_test_files,
        "scope_excluded_changed_files": scope_excluded,
        "uncovered_changed_test_files": uncovered,
        "secondary_coverage_provider_job": secondary_coverage_provider_job,
        "cross_job_covered_test_files": cross_job_covered_test_files,
        "secondary_coverage_error": secondary_coverage_error,
        "runtime_verification_only_markers": runtime_verification_only_markers,
        "runtime_verification_only_test_files": runtime_verification_only_test_files,
        "runtime_verification_only_evidence": runtime_verification_only_evidence,
        "runtime_verification_only_markers_error": runtime_verification_only_markers_error,
        "ci_run_url": args.ci_run_url or "N/A",
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(artifact, f, indent=2)

    print(f"Artifact written to {output_path}")
    print(f"  Schema version: {artifact['schema_version']}")
    print(f"  PR head SHA: {pr_head_sha}")
    print(f"  Collected nodeids: {len(collected_nodeids)}")
    print(f"  Changed tests: {len(changed_test_files)}")
    print(f"  Uncovered tests: {len(uncovered)}")
    if runtime_verification_only_test_files:
        print(f"  Runtime-verification-only exempt tests: {len(runtime_verification_only_test_files)}")
    print(f"  collection_status.ok={collection_status['ok']} diff_status.ok={diff_status['ok']}")

    # Fail-closed: both collection AND change detection must succeed. A failed/empty
    # collection or a failed git diff (e.g. shallow checkout missing base/head) is a CI
    # failure, never a success artifact with an empty set (Issue #1064 review).
    if not collection_status["ok"]:
        print(
            "ERROR: pytest collection failed (fail-closed): "
            f"returncode={collection_status['returncode']} "
            f"timed_out={collection_status['timed_out']} "
            f"nodeid_count={collection_status['nodeid_count']} "
            f"error={collection_status['error']}",
            file=sys.stderr,
        )
        if collection_status["stderr_tail"]:
            print(collection_status["stderr_tail"], file=sys.stderr)
        return 2
    if not diff_status["ok"]:
        print(
            "ERROR: changed-test detection failed (fail-closed): "
            f"base_sha={diff_status['base_sha']} head_sha={diff_status['head_sha']} "
            f"returncode={diff_status['returncode']} timed_out={diff_status['timed_out']} "
            f"error={diff_status['error']}. A shallow checkout (fetch-depth!=0) or a "
            "missing base/head SHA prevents reliable change detection; this must fail CI "
            "rather than report an empty changed-test set.",
            file=sys.stderr,
        )
        if diff_status["stderr_tail"]:
            print(diff_status["stderr_tail"], file=sys.stderr)
        return 2

    return 0 if not uncovered else 1


def main():
    parser = argparse.ArgumentParser(
        description="Generate ci_test_selection/v1 artifact for G1 gate"
    )
    parser.add_argument("--output", "-o", required=True, help="Output path for artifact JSON")
    parser.add_argument(
        "--pytest-args",
        nargs="+",
        help="pytest scope argv override (default: derive from python-test plan SSOT)",
    )
    parser.add_argument(
        "--plan",
        help="path to python-test plan JSON (default: .github/ci/python-test-plan.json)",
    )
    parser.add_argument("--pr-head-sha", help="PR head SHA from GitHub (optional)")
    parser.add_argument(
        "--base-sha",
        help="Base SHA for change detection (e.g. github.event.pull_request.base.sha)",
    )
    parser.add_argument(
        "--head-sha",
        help="Head SHA for change detection (defaults to --pr-head-sha)",
    )
    parser.add_argument(
        "--checked-out-sha",
        help="Current checked-out SHA (optional, default: git rev-parse HEAD)",
    )
    parser.add_argument("--merge-sha", help="Merge commit SHA (e.g., github.sha) (optional)")
    parser.add_argument("--workflow", default="ci", help="Workflow name (default: ci)")
    parser.add_argument("--job", default="python-test", help="Job name (default: python-test)")
    parser.add_argument("--ci-run-url", help="CI run URL (optional)")

    args = parser.parse_args()
    return generate_artifact(args)


if __name__ == "__main__":
    sys.exit(main())
