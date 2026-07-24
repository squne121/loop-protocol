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
- ``scope_argv`` (targets + ``--ignore=`` + ``--deselect=``) is the complete collection
  scope shared by the ``--collect-only`` guard and the artifact. ``parallel_scope_argv``
  derives its disjoint parallel partition from it.
- ``run_argv`` adds the xdist worker/scheduler knobs (``-n`` / ``--dist``) on top of
  the selected lane scope; ``mode=serial`` forces ``-n 0`` for the serial-equivalence proof.
- ``serial_lane_argv`` runs the ``parallel_exclude`` tests at ``-n 0`` and inherits the
  same ``--ignore`` / ``--deselect`` as the parallel run so the parallel ∪ serial
  collection equals the full scope and stays drift-free (Issue #1064 review).

Validation is fail-closed: ``load_plan`` enforces structural invariants (types, value
ranges, duplicates, path hygiene, lane-union pre-conditions). ``assert_plan_paths_exist``
adds filesystem existence checks for CI; the collected-nodeid union invariant is verified
at runtime by ``scripts/ci/verify_lane_union.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

SCHEMA_VERSION = "python_test_plan/v1"

# Valid pytest-xdist --dist schedulers (pytest-xdist >= 3.x).
VALID_DIST = {"load", "loadscope", "loadfile", "loadgroup", "worksteal", "no", "each"}

# Resolve the plan relative to the repo root (two levels up from scripts/ci/).
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLAN_PATH = _REPO_ROOT / ".github" / "ci" / "python-test-plan.json"


class PlanError(ValueError):
    """Raised when the plan file is missing required structure."""


def _path_part(entry: str) -> str:
    """Return the path portion of a target/ignore/deselect entry (before ``::``)."""
    return entry.split("::", 1)[0]


def _validate_path_hygiene(key: str, entry: str) -> None:
    """Reject absolute paths, ``..`` traversal, and option-like (leading ``-``) entries."""
    if entry.startswith("-"):
        raise PlanError(f"plan.{key} entry must not look like an option: {entry!r}")
    path = _path_part(entry)
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        raise PlanError(f"plan.{key} entry must be a relative path: {entry!r}")
    if ".." in Path(path).parts:
        raise PlanError(f"plan.{key} entry must not contain '..': {entry!r}")


def _require_no_duplicates(key: str, items: List[str]) -> None:
    seen = set()
    for item in items:
        if item in seen:
            raise PlanError(f"plan.{key} must not contain duplicates: {item!r}")
        seen.add(item)


def _target_dirs_and_files(targets: List[str]) -> tuple[list[str], set[str]]:
    dirs = [t if t.endswith("/") else t + "/" for t in targets if t.endswith("/")]
    files = {t for t in targets if not t.endswith("/")}
    return dirs, files


def _is_in_target_scope(path: str, targets: List[str]) -> bool:
    dirs, files = _target_dirs_and_files(targets)
    if path in files:
        return True
    norm = path if not path.endswith("/") else path
    return any(norm.startswith(d) for d in dirs)


def load_plan(path: Path | str | None = None) -> Dict[str, Any]:
    """Load and validate the python-test plan JSON (fail-closed, structural only).

    Enforces: schema_version, types, non-empty targets, per-list path hygiene
    (relative / no ``..`` / no leading ``-``) and de-duplication, ``xdist.workers``
    (``"auto"`` or int >= 1, ``bool`` rejected), ``xdist.dist`` enum,
    ``parallel_exclude`` ⊆ target scope, and ``parallel_exclude`` ∩ ``ignore`` == ∅.
    Filesystem existence is checked separately by ``assert_plan_paths_exist``.
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

    for key in ("targets", "ignore", "deselect", "parallel_exclude"):
        value = plan.get(key, [])
        if not isinstance(value, list):
            raise PlanError(f"plan.{key} must be a list")
        for item in value:
            if not isinstance(item, str) or not item:
                raise PlanError(f"plan.{key} entries must be non-empty strings: {item!r}")
            _validate_path_hygiene(key, item)
        _require_no_duplicates(key, value)

    xdist = plan.get("xdist", {})
    if not isinstance(xdist, dict):
        raise PlanError("plan.xdist must be an object")
    workers = xdist.get("workers", "auto")
    # bool is a subclass of int; reject it explicitly.
    if isinstance(workers, bool):
        raise PlanError("plan.xdist.workers must not be a boolean")
    if isinstance(workers, int):
        if workers < 1:
            raise PlanError("plan.xdist.workers (int) must be >= 1")
    elif workers != "auto":
        raise PlanError('plan.xdist.workers must be "auto" or an int >= 1')
    dist = xdist.get("dist", "worksteal")
    if dist not in VALID_DIST:
        raise PlanError(f"plan.xdist.dist must be one of {sorted(VALID_DIST)}: {dist!r}")

    # Lane-union pre-conditions: every parallel_exclude path must be collected by the
    # target scope (otherwise the serial lane runs nothing meaningful), and must not
    # also be ignored (ignored = never collected; contradictory with serial-lane run).
    ignore_set = set(plan.get("ignore", []))
    for pe in plan.get("parallel_exclude", []):
        if not _is_in_target_scope(_path_part(pe), targets):
            raise PlanError(f"plan.parallel_exclude entry not within target scope: {pe!r}")
        if pe in ignore_set or _path_part(pe) in ignore_set:
            raise PlanError(f"plan.parallel_exclude entry must not also be in ignore: {pe!r}")

    return plan


def assert_plan_paths_exist(plan: Dict[str, Any], repo_root: Path | str | None = None) -> None:
    """Verify every target / ignore / deselect / parallel_exclude path exists on disk.

    Used by CI (not by unit tests, which use synthetic plans). Raises PlanError on the
    first missing path.
    """
    root = Path(repo_root) if repo_root is not None else _REPO_ROOT
    for key in ("targets", "ignore", "deselect", "parallel_exclude"):
        for entry in plan.get(key, []):
            p = root / _path_part(entry)
            if not p.exists():
                raise PlanError(f"plan.{key} path does not exist: {_path_part(entry)} (entry {entry!r})")


def _ignore_deselect_flags(plan: Dict[str, Any]) -> List[str]:
    flags: List[str] = []
    for ignore in plan.get("ignore", []):
        flags.append(f"--ignore={ignore}")
    for deselect in plan.get("deselect", []):
        flags.append(f"--deselect={deselect}")
    return flags


def scope_argv(plan: Dict[str, Any]) -> List[str]:
    """Return the canonical pytest collection-scope argv.

    This is the SSOT shared by the executed pytest run, the ``--collect-only`` guard,
    and the ci_test_selection artifact ``pytest_argv``. It contains the targets and the
    ``--ignore=`` / ``--deselect=`` flags but no runtime (xdist) knobs.
    """
    return list(plan["targets"]) + _ignore_deselect_flags(plan)


def parallel_scope_argv(plan: Dict[str, Any]) -> List[str]:
    """Return the parallel lane's collection argv without xdist knobs.

    ``--ignore`` does not exclude a file when that same file is also an explicit
    positional pytest target.  Remove an exactly matching target before adding
    the ignore flags so ``parallel_exclude`` remains a true lane partition;
    directory targets stay in place and their contained excluded files are
    handled by ``--ignore``.
    """
    excluded = set(plan.get("parallel_exclude", []))
    targets = [target for target in plan["targets"] if target not in excluded]
    return targets + _ignore_deselect_flags(plan) + [
        f"--ignore={path}" for path in plan.get("parallel_exclude", [])
    ]


def resolved_workers(plan: Dict[str, Any]) -> Any:
    """Return the configured worker setting (``"auto"`` or a positive int)."""
    return plan.get("xdist", {}).get("workers", "auto")


def scheduler(plan: Dict[str, Any]) -> str:
    return plan.get("xdist", {}).get("dist", "worksteal")


def run_argv(plan: Dict[str, Any], *, mode: str = "parallel") -> List[str]:
    """Return the full pytest argv including xdist knobs for the given mode.

    mode="serial"   -> ``-n 0`` over the full scope (xdist installed but single-process;
                       used for the collected-nodeid equivalence proof).
    mode="parallel" -> ``-n <workers> --dist <dist>`` over the scope MINUS
                       plan.parallel_exclude (those run in the serial lane instead).
    """
    if mode not in ("serial", "parallel"):
        raise PlanError(f"unknown mode: {mode!r}")
    if mode == "serial":
        return ["-n", "0"] + scope_argv(plan)
    argv: List[str] = ["-n", str(resolved_workers(plan)), "--dist", scheduler(plan)]
    return argv + parallel_scope_argv(plan)


def serial_lane_argv(plan: Dict[str, Any]) -> List[str]:
    """Return the ``-n 0`` argv for the parallel-unsafe tests, or [] if none.

    Inherits the plan-wide ``--ignore`` / ``--deselect`` flags so the parallel ∪ serial
    collection stays equal to the full scope (no drift if a deselected nodeid happens to
    live inside a parallel-excluded file). The parallel_exclude paths themselves are NOT
    re-ignored here (they are exactly what this lane runs).
    """
    excluded = plan.get("parallel_exclude", [])
    if not excluded:
        return []
    return ["-n", "0", *excluded, *_ignore_deselect_flags(plan)]


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
        choices=["scope-argv", "run-argv", "serial-lane-argv", "workers", "scheduler"],
        default="scope-argv",
        help="scope-argv: targets+ignore+deselect; run-argv: adds xdist knobs; "
        "serial-lane-argv: -n 0 over parallel_exclude tests; workers/scheduler: scalar",
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
    parser.add_argument(
        "--check-paths",
        action="store_true",
        help="also assert every plan path exists on disk (CI use)",
    )
    args = parser.parse_args(argv)

    try:
        plan = load_plan(args.plan)
        if args.check_paths:
            assert_plan_paths_exist(plan)
        if args.emit == "scope-argv":
            values = scope_argv(plan)
        elif args.emit == "serial-lane-argv":
            values = serial_lane_argv(plan)
        elif args.emit == "workers":
            values = [str(resolved_workers(plan))]
        elif args.emit == "scheduler":
            values = [scheduler(plan)]
        else:
            values = run_argv(plan, mode=args.mode)
    except PlanError as exc:
        print(f"python_test_plan: {exc}", file=sys.stderr)
        return 2

    sys.stdout.write(_emit(values, args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())
