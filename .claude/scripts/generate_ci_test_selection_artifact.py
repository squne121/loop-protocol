#!/usr/bin/env python3
"""
Generate ci_test_selection/v1 artifact for G1 gate verification.

Collects changed test files from git diff and outputs schema-compliant artifact.
"""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def get_changed_test_files(base_ref="main"):
    """Get list of changed test files between base_ref and HEAD."""
    try:
        result = subprocess.run(
            ["git", "diff", base_ref + "...HEAD", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            all_files = [f for f in result.stdout.split("\n") if f]
            test_files = [f for f in all_files if "test" in f.lower() and f.endswith((".py", ".ts", ".js"))]
            return test_files
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return []


def get_collected_test_files():
    """Get list of all test files that pytest would discover."""
    try:
        result = subprocess.run(
            ["find", ".", "-name", "*test*.py", "-type", "f"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return [f.lstrip("./") for f in result.stdout.split("\n") if f]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return []


def get_head_sha():
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
    head_sha = args.head_sha or get_head_sha()
    changed_test_files = get_changed_test_files()
    collected_test_files = get_collected_test_files()

    # Determine uncovered files: changed test files that are NOT in collected list
    uncovered = [f for f in changed_test_files if f not in collected_test_files]

    artifact = {
        "schema_version": "ci_test_selection/v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "head_sha": head_sha,
        "workflow": args.workflow,
        "job": args.job,
        "commands": ["pytest", ".claude/skills/pr-review-judge/scripts/tests/"],
        "collected_test_files": sorted(collected_test_files),
        "changed_test_files": sorted(changed_test_files),
        "uncovered_changed_test_files": sorted(uncovered),
        "ci_run_url": args.ci_run_url if args.ci_run_url else "N/A"
    }

    # Write artifact
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(artifact, f, indent=2)

    print(f"Artifact written to {output_path}")
    print(f"  Schema version: {artifact['schema_version']}")
    print(f"  Head SHA: {head_sha}")
    print(f"  Changed tests: {len(changed_test_files)}")
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
        "--head-sha",
        help="Override HEAD SHA (default: git rev-parse HEAD)"
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
