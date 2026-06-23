#!/usr/bin/env python3
"""
Generate ci_test_selection/v1 artifact for G1 gate verification.

Uses pytest --collect-only to get actual discovered tests,
then compares with changed test files from git diff.

Target set / ignore / deselect come from the python-test plan SSOT
(.github/ci/python-test-plan.json) via the scripts/ci/python_test_plan.py loader, so
the executed pytest target set and the artifact ``pytest_argv`` cannot drift
(Issue #1064). Collection is fail-closed: a non-zero collect exit, a timeout, or zero
collected nodeids records ``collection_status`` in the artifact AND fails CI.
"""

import argparse
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

COLLECT_TIMEOUT_SECONDS = 120


def _load_plan_module():
    """Import the scripts/ci/python_test_plan.py loader by file location.

    Resolves the repo root from this file's path
    (.claude/skills/pr-review-judge/scripts/ -> repo root is parents[4]).
    """
    repo_root = Path(__file__).resolve().parents[4]
    loader_path = repo_root / "scripts" / "ci" / "python_test_plan.py"
    spec = importlib.util.spec_from_file_location("python_test_plan", loader_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load python-test plan loader at {loader_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_pytest_args(args) -> List[str]:
    """Resolve the pytest scope argv.

    Precedence: explicit --pytest-args (mainly for tests) wins; otherwise derive from
    the plan SSOT via the loader. This keeps the executed target set and the artifact
    pytest_argv tied to the same SSOT.
    """
    if getattr(args, "pytest_args", None):
        return list(args.pytest_args)
    plan_module = _load_plan_module()
    plan_path = getattr(args, "plan", None)
    plan = plan_module.load_plan(plan_path)
    return plan_module.scope_argv(plan)


def get_pytest_collected_tests(
    pytest_args: List[str],
) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """Get test files and node IDs from pytest --collect-only.

    Returns (collected_files, collected_nodeids, collection_status). collection_status
    is fail-closed evidence: it records the collect exit code, timeout flag, a stderr
    tail and the nodeid count so a failed/empty collection can never be silently
    reported as success.
    """
    collected_files: List[str] = []
    collected_nodeids: List[str] = []
    collection_status: Dict[str, Any] = {
        "returncode": None,
        "timed_out": False,
        "error": None,
        "nodeid_count": 0,
        "stderr_tail": "",
        "ok": False,
    }

    try:
        result = subprocess.run(
            ["uv", "run", "--locked", "pytest", "--collect-only", "-q"] + pytest_args,
            capture_output=True,
            text=True,
            timeout=COLLECT_TIMEOUT_SECONDS,
        )
        collection_status["returncode"] = result.returncode
        collection_status["stderr_tail"] = "\n".join(
            result.stderr.strip().splitlines()[-20:]
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                line = line.strip()
                if not line or line.startswith(" "):
                    continue
                # Stop at the pytest summary line (e.g. "123 tests collected in 1.2s").
                if line[0].isdigit() and (
                    "test" in line and ("collected" in line or "error" in line)
                ):
                    continue
                collected_nodeids.append(line)
                file_part = line.split("::")[0] if "::" in line else line
                if file_part and file_part not in collected_files:
                    collected_files.append(file_part)
    except subprocess.TimeoutExpired:
        collection_status["timed_out"] = True
        collection_status["error"] = "pytest --collect-only timed out"
    except FileNotFoundError as exc:
        collection_status["error"] = f"collect command not found: {exc}"

    collection_status["nodeid_count"] = len(collected_nodeids)
    # Fail-closed: collection is OK only when collect exited 0, did not time out, and
    # discovered at least one nodeid.
    collection_status["ok"] = (
        collection_status["returncode"] == 0
        and not collection_status["timed_out"]
        and collection_status["nodeid_count"] > 0
    )
    return sorted(collected_files), sorted(collected_nodeids), collection_status


def get_changed_test_files(base_ref="main") -> Tuple[List[str], List[str]]:
    """Get list of changed test files between base_ref and HEAD."""
    changed_test_files = []
    scope_excluded_files = []

    try:
        result = subprocess.run(
            ["git", "diff", base_ref + "...HEAD", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for f in result.stdout.split("\n"):
                if not f or "test" not in f.lower():
                    continue
                if f.endswith((".py",)):
                    changed_test_files.append(f)
                elif f.endswith((".ts", ".js")):
                    scope_excluded_files.append(f)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return sorted(changed_test_files), sorted(scope_excluded_files)


def get_current_head_sha() -> str:
    """Get current HEAD SHA."""
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
    """Generate ci_test_selection/v1 artifact (fail-closed on collection failure)."""
    pytest_args = resolve_pytest_args(args)

    collected_test_files, collected_nodeids, collection_status = (
        get_pytest_collected_tests(pytest_args)
    )

    changed_test_files, scope_excluded = get_changed_test_files()
    uncovered = [f for f in changed_test_files if f not in collected_test_files]

    pr_head_sha = args.pr_head_sha or "unknown"
    checked_out_sha = args.checked_out_sha or get_current_head_sha()
    merge_sha = args.merge_sha or "unknown"

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
        "collected_test_files": collected_test_files,
        "collected_nodeids": collected_nodeids,
        "changed_test_files": changed_test_files,
        "scope_excluded_changed_files": scope_excluded,
        "uncovered_changed_test_files": uncovered,
        "ci_run_url": args.ci_run_url or "N/A",
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(artifact, f, indent=2)

    print(f"Artifact written to {output_path}")
    print(f"  Schema version: {artifact['schema_version']}")
    print(f"  PR head SHA: {pr_head_sha}")
    print(f"  Collected tests: {len(collected_test_files)}")
    print(f"  Collected nodeids: {len(collected_nodeids)}")
    print(f"  Changed tests: {len(changed_test_files)}")
    print(f"  Scope excluded (TS/JS): {len(scope_excluded)}")
    print(f"  Uncovered tests: {len(uncovered)}")

    # Fail-closed: a failed/empty/timed-out collection is a CI failure, not a
    # success artifact with an empty collected set (Issue #1064 AC6).
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

    return 0 if not uncovered else 1


def main():
    parser = argparse.ArgumentParser(
        description="Generate ci_test_selection/v1 artifact for G1 gate"
    )
    parser.add_argument(
        "--output", "-o", required=True, help="Output path for artifact JSON"
    )
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
        "--checked-out-sha",
        help="Current checked-out SHA (optional, default: git rev-parse HEAD)",
    )
    parser.add_argument(
        "--merge-sha", help="Merge commit SHA (e.g., github.sha) (optional)"
    )
    parser.add_argument("--workflow", default="ci", help="Workflow name (default: ci)")
    parser.add_argument(
        "--job", default="python-test", help="Job name (default: python-test)"
    )
    parser.add_argument("--ci-run-url", help="CI run URL (optional)")

    args = parser.parse_args()
    return generate_artifact(args)


if __name__ == "__main__":
    sys.exit(main())
