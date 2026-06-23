#!/usr/bin/env python3
"""Loader for the python-test plan SSOT (.github/ci/python-test-plan.json).

This module is the single consumer of the machine-readable python-test plan. Both
the ``python-test`` job in ``.github/workflows/ci.yml`` and the ci_test_selection/v1
artifact generator (``generate_ci_test_selection_artifact.py``) import / invoke this
loader so the executed pytest target set and the artifact ``pytest_argv`` cannot
drift (Issue #1064).

Design constraints:
- No shell ``eval``. The CLI emits the scope argv as a NUL-separated stream
  (``--format nul``) or a JSON array (``--format json``) so the workflow can read it
  with ``mapfile -d ''`` without word-splitting or glob expansion.
- ``scope_argv`` (targets + ``--ignore=`` + ``--deselect=``) is the collection scope
  shared by the executed pytest run, the ``--collect-only`` guard, and the artifact.
- ``run_argv`` adds the xdist worker/scheduler knobs (``-n`` / ``--dist``) on top of
  the scope; ``mode=serial`` forces ``-n 0`` for the serial-equivalence proof.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

SCHEMA_VERSION = "python_test_plan/v1"

# Resolve the plan relative to the repo root (two levels up from scripts/ci/).
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLAN_PATH = _REPO_ROOT / ".github" / "ci" / "python-test-plan.json"


class PlanError(ValueError):
    """Raised when the plan file is missing required structure."""


def load_plan(path: Path | str | None = None) -> Dict[str, Any]:
    """Load and validate the python-test plan JSON.

    Fail-closed: any missing/empty required field, wrong type, or unexpected
    schema_version raises PlanError rather than silently returning a partial plan.
    """
    plan_path = Path(path) if path is not None else DEFAULT_PLAN_PATH
    if not plan_path.is_file():
        raise PlanError(f"python-test plan not found: {plan_path}")
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlanError(f"python-test plan is not valid JSON: {exc}") from exc

    if not isinstance(plan, dict):
        raise PlanError("python-test plan must be a JSON object")
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise PlanError(
            f"unexpected schema_version: {plan.get('schema_version')!r} "
            f"(expected {SCHEMA_VERSION!r})"
        )

    targets = plan.get("targets")
    if not isinstance(targets, list) or not targets:
        raise PlanError("plan.targets must be a non-empty list")
    for item in targets:
        if not isinstance(item, str) or not item:
            raise PlanError(f"plan.targets entries must be non-empty strings: {item!r}")

    for key in ("ignore", "deselect", "parallel_exclude"):
        value = plan.get(key, [])
        if not isinstance(value, list):
            raise PlanError(f"plan.{key} must be a list")
        for item in value:
            if not isinstance(item, str) or not item:
                raise PlanError(f"plan.{key} entries must be non-empty strings: {item!r}")

    xdist = plan.get("xdist", {})
    if not isinstance(xdist, dict):
        raise PlanError("plan.xdist must be an object")
    workers = xdist.get("workers", "auto")
    if not isinstance(workers, (str, int)):
        raise PlanError("plan.xdist.workers must be a string or int")
    dist = xdist.get("dist", "worksteal")
    if not isinstance(dist, str) or not dist:
        raise PlanError("plan.xdist.dist must be a non-empty string")

    return plan


def scope_argv(plan: Dict[str, Any]) -> List[str]:
    """Return the canonical pytest collection-scope argv.

    This is the SSOT shared by the executed pytest run, the ``--collect-only`` guard,
    and the ci_test_selection artifact ``pytest_argv``. It contains the targets and the
    ``--ignore=`` / ``--deselect=`` flags but no runtime (xdist) knobs.
    """
    argv: List[str] = list(plan["targets"])
    for ignore in plan.get("ignore", []):
        argv.append(f"--ignore={ignore}")
    for deselect in plan.get("deselect", []):
        argv.append(f"--deselect={deselect}")
    return argv


def run_argv(plan: Dict[str, Any], *, mode: str = "parallel") -> List[str]:
    """Return the full pytest argv including xdist knobs for the given mode.

    mode="serial"   -> ``-n 0`` over the full scope (xdist installed but single-process;
                       used for the collected-nodeid equivalence proof).
    mode="parallel" -> ``-n <workers> --dist <dist>`` over the scope MINUS
                       plan.parallel_exclude (those run in the serial lane instead).
    """
    if mode not in ("serial", "parallel"):
        raise PlanError(f"unknown mode: {mode!r}")
    argv: List[str] = []
    if mode == "serial":
        argv += ["-n", "0"]
        argv += scope_argv(plan)
        return argv
    xdist = plan.get("xdist", {})
    workers = xdist.get("workers", "auto")
    dist = xdist.get("dist", "worksteal")
    argv += ["-n", str(workers), "--dist", str(dist)]
    argv += scope_argv(plan)
    # Parallel-unsafe tests are ignored here and run in the serial lane (serial_lane_argv).
    for path in plan.get("parallel_exclude", []):
        argv.append(f"--ignore={path}")
    return argv


def serial_lane_argv(plan: Dict[str, Any]) -> List[str]:
    """Return the ``-n 0`` argv for the parallel-unsafe tests, or [] if none.

    These tests are excluded from the xdist parallel run (run_argv parallel) and executed
    in a dedicated single-process lane so timing-sensitive assertions are not subject to
    xdist CPU contention (Issue #1064).
    """
    excluded = plan.get("parallel_exclude", [])
    if not excluded:
        return []
    return ["-n", "0", *excluded]


def _emit(values: List[str], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(values)
    if fmt == "nul":
        # NUL-terminated stream; safe for ``mapfile -d ''`` in bash.
        return "".join(value + "\0" for value in values)
    if fmt == "lines":
        return "\n".join(values)
    raise PlanError(f"unknown format: {fmt!r}")


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="python-test plan loader (Issue #1064)")
    parser.add_argument("--plan", default=None, help="path to plan JSON (default: repo SSOT)")
    parser.add_argument(
        "--emit",
        choices=["scope-argv", "run-argv", "serial-lane-argv"],
        default="scope-argv",
        help="scope-argv: targets+ignore+deselect; run-argv: adds xdist knobs; "
        "serial-lane-argv: -n 0 over parallel_exclude tests",
    )
    parser.add_argument(
        "--mode",
        choices=["serial", "parallel"],
        default="parallel",
        help="run-argv mode (ignored for scope-argv)",
    )
    parser.add_argument(
        "--format",
        choices=["nul", "json", "lines"],
        default="nul",
        help="output format",
    )
    args = parser.parse_args(argv)

    try:
        plan = load_plan(args.plan)
        if args.emit == "scope-argv":
            values = scope_argv(plan)
        elif args.emit == "serial-lane-argv":
            values = serial_lane_argv(plan)
        else:
            values = run_argv(plan, mode=args.mode)
    except PlanError as exc:
        print(f"python_test_plan: {exc}", file=sys.stderr)
        return 2

    sys.stdout.write(_emit(values, args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())
