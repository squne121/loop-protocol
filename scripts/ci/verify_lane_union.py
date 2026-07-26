#!/usr/bin/env python3
"""Verify the parallel/serial lane collection invariant (Issue #1064 review).

Collects node IDs three ways using the JSON-emitting plugin (no stdout parsing) and
asserts:

    parallel_nodeids ∪ serial_nodeids == scope_nodeids
    parallel_nodeids ∩ serial_nodeids == ∅

so the unified plan, the xdist parallel run and the serial lane never drift from the
ci_test_selection scope. Exit 0 on success, 2 on any collection failure or invariant
violation (fail-closed).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))

import python_test_plan as plan_mod  # noqa: E402


def _collect(argv: list[str]) -> set[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_HERE) + os.pathsep + env.get("PYTHONPATH", "")
    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as fh:
        out_path = fh.name
    env["COLLECT_NODEIDS_OUT"] = out_path
    proc = subprocess.run(
        ["uv", "run", "--locked", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "-p", "collect_nodeids_plugin", *argv],
        cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"collection failed (rc={proc.returncode}) for argv={argv}\n"
            f"{proc.stderr[-2000:]}\n"
        )
        raise SystemExit(2)
    data = json.loads(Path(out_path).read_text(encoding="utf-8"))
    return set(data["nodeids"])


def main() -> int:
    plan = plan_mod.load_plan()
    scope = plan_mod.scope_argv(plan)
    parallel = plan_mod.parallel_scope_argv(plan)
    # serial collection: parallel_exclude paths + inherited ignore/deselect (drop -n 0).
    serial = plan_mod.serial_lane_argv(plan)
    serial = [a for a in serial if a not in ("-n", "0")]

    scope_ids = _collect(scope)
    parallel_ids = _collect(parallel)
    serial_ids = _collect(serial) if serial else set()

    union = parallel_ids | serial_ids
    inter = parallel_ids & serial_ids

    ok = union == scope_ids and not inter
    print(json.dumps({
        "scope_count": len(scope_ids),
        "parallel_count": len(parallel_ids),
        "serial_count": len(serial_ids),
        "union_equals_scope": union == scope_ids,
        "parallel_serial_disjoint": not inter,
        "missing_from_union": sorted(scope_ids - union)[:20],
        "in_both_lanes": sorted(inter)[:20],
        "ok": ok,
    }, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
