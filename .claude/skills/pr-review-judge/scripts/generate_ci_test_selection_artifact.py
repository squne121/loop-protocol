#!/usr/bin/env python3
"""
Generate ci_test_selection/v1 artifact for G1 gate verification.

Uses pytest --collect-only to get actual discovered tests,
then compares with changed test files from git diff.
"""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple


def get_pytest_collected_tests(pytest_args: List[str]) -> Tuple[List[str], List[str]]:
    """Get list of test files and node IDs from pytest --collect-only."""
    collected_files = []
    collected_nodeids = []

    try:
        result = subprocess.run(
            ["uv", "run", "--locked", "pytest", "--collect-only", "-q"] + pytest_args,
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            # Parse output: each line is a nodeid like "tests/foo.py::test_bar"
            for line in result.stdout.split("\n"):
                line = line.strip()
                if not line or line.startswith(" "):
                    continue
                collected_nodeids.append(line)
                # Extract file path (before ::)
                if "::" in line:
                    file_part = line.split("::")[0]
                else:
                    file_part = line
                if file_part and file_part not in collected_files:
                    collected_files.append(file_part)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return sorted(collected_files), sorted(collected_nodeids)


def get_changed_test_files(base_ref="main") -> Tuple[List[str], List[str]]:
    """Get list of changed test files between base_ref and HEAD."""
    changed_test_files = []
    scope_excluded_files = []

    try:
        result = subprocess.run(
            ["git", "diff", base_ref + "...HEAD", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            for f in result.stdout.split("\n"):
                if not f or "test" not in f.lower():
                    continue
                if f.endswith((".py",)):
                    changed_test_files.append(f)
                elif f.endswith((".ts", ".js")):
                    # Track non-Python test files separately
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
            timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "unknown"


def generate_artifact(args):
    """Generate ci_test_selection/v1 artifact."""
    # Get collected tests from pytest
    collected_test_files, collected_nodeids = get_pytest_collected_tests(args.pytest_args)

    # Get changed files from git
    changed_test_files, scope_excluded = get_changed_test_files()

    # Determine uncovered files: changed test files that are NOT in collected list
    uncovered = [f for f in changed_test_files if f not in collected_test_files]

    # Use provided SHAs or detect
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
        "pytest_argv": args.pytest_args,
        "collected_test_files": collected_test_files,
        "collected_nodeids": collected_nodeids,
        "changed_test_files": changed_test_files,
        "scope_excluded_changed_files": scope_excluded,
        "uncovered_changed_test_files": uncovered,
        "ci_run_url": args.ci_run_url or "N/A"
    }

    # Write artifact
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(artifact, f, indent=2)

    print(f"Artifact written to {output_path}")
    print(f"  Schema version: {artifact['schema_version']}")
    print(f"  PR head SHA: {pr_head_sha}")
    print(f"  Collected tests: {len(collected_test_files)}")
    print(f"  Changed tests: {len(changed_test_files)}")
    print(f"  Scope excluded (TS/JS): {len(scope_excluded)}")
    print(f"  Uncovered tests: {len(uncovered)}")

    return 0 if not uncovered else 1


def main():
    parser = argparse.ArgumentParser(
        description="Generate ci_test_selection/v1 artifact for G1 gate"
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Output path for artifact JSON"
    )
    parser.add_argument(
        "--pytest-args",
        nargs="+",
        required=True,
        help="pytest arguments (directories/files to collect from)"
    )
    parser.add_argument(
        "--pr-head-sha",
        help="PR head SHA from GitHub (optional)"
    )
    parser.add_argument(
        "--checked-out-sha",
        help="Current checked-out SHA (optional, default: git rev-parse HEAD)"
    )
    parser.add_argument(
        "--merge-sha",
        help="Merge commit SHA (e.g., github.sha) (optional)"
    )
    parser.add_argument(
        "--workflow",
        default="ci",
        help="Workflow name (default: ci)"
    )
    parser.add_argument(
        "--job",
        default="python-test",
        help="Job name (default: python-test)"
    )
    parser.add_argument(
        "--ci-run-url",
        help="CI run URL (optional)"
    )

    args = parser.parse_args()
    return generate_artifact(args)


if __name__ == "__main__":
    import sys
    sys.exit(main())
