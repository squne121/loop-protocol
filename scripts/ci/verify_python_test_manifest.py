#!/usr/bin/env python3
"""Verify the python-test JUnit artifacts represent the WHOLE suite (Issue #1064 review).

AC9 requires the distributed JUnit/durations artifact to cover all of python-test, not
just the xdist parallel invocation. The serial lane (parallel_exclude tests) is a
separate pytest invocation with its own JUnit, so this manifest asserts:

    parallel_junit_testcases + serial_junit_testcases == scope_collected_nodeids

i.e. the union of the two executed lanes equals the full plan collection scope. Emits a
manifest JSON and exits 2 (fail-closed) on any mismatch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
sys.path.insert(0, str(_HERE))

import python_test_plan as plan_mod  # noqa: E402


def _count_testcases(junit_path: str | None) -> int:
    if not junit_path or not Path(junit_path).is_file():
        return 0
    root = ET.parse(junit_path).getroot()
    return len(root.findall(".//testcase"))


def _scope_nodeid_count() -> int:
    plan = plan_mod.load_plan()
    argv = plan_mod.scope_argv(plan)
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
        sys.stderr.write(f"scope collection failed: {proc.stderr[-2000:]}\n")
        raise SystemExit(2)
    return json.loads(Path(out_path).read_text(encoding="utf-8"))["count"]


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="verify python-test JUnit manifest (Issue #1064)")
    ap.add_argument("--parallel-junit", required=True)
    ap.add_argument("--serial-junit", default=None)
    ap.add_argument("--manifest-out", default=None)
    args = ap.parse_args()

    parallel = _count_testcases(args.parallel_junit)
    serial = _count_testcases(args.serial_junit)
    scope = _scope_nodeid_count()
    total = parallel + serial
    ok = total == scope

    manifest = {
        "schema": "python_test_manifest/v1",
        "parallel_junit_testcases": parallel,
        "serial_junit_testcases": serial,
        "executed_total": total,
        "scope_collected_nodeids": scope,
        "union_equals_scope": ok,
    }
    text = json.dumps(manifest, indent=2)
    print(text)
    if args.manifest_out:
        Path(args.manifest_out).write_text(text, encoding="utf-8")
    if not ok:
        sys.stderr.write(
            f"::error::python-test JUnit manifest mismatch: parallel({parallel}) + "
            f"serial({serial}) = {total} != scope collected {scope}\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
