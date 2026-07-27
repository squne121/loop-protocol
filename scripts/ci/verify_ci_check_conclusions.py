#!/usr/bin/env python3
"""Verify real CI check-run conclusions + AC6 sentinel artifact content (Issue #1760).

AC9 requires runtime evidence for the same head SHA -- not a string search over
``ci.yml`` -- that ``actionlint`` / ``python-test-core`` / ``codex-execpolicy`` /
``python-test`` (required aggregate) / ``node-backed-hook-tests`` all completed with
an acceptable conclusion, AND that the AC6 sentinel artifact
(``codex_execpolicy_matrix_status_v1.json``) reports a real terminal status (not
merely "started"/absent).

Inputs are two already-fetched JSON documents (no live network calls from this
script -- the CI job that invokes it is responsible for the ``gh api`` call and the
artifact download, matching the existing ``ci_verdict_summary_v2.py`` pattern):

  --check-runs-api-json   raw ``GET /repos/{owner}/{repo}/commits/{sha}/check-runs``
                           response body (the same file the ci-verdict-summary job
                           already produces as ``ci_verdict_summary_v2_check_runs.json``)
  --codex-sentinel-json    the downloaded ``codex_execpolicy_matrix_status_v1.json``
                           artifact content produced by the codex-execpolicy job

Exit 0 = every required check name has a matching, current-head, successful check
run AND the sentinel reports a terminal status. Exit 2 = any invariant violated
(fail-closed). Exit 3 = operational failure (missing/unparseable input file).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCHEMA = "ci_check_conclusions_verification_v1"

# AC9: the exact set of check names that must be verified against real, current-head
# check-run evidence. "python-test" here is the REQUIRED AGGREGATE job (AC5), distinct
# from "python-test-core".
REQUIRED_CHECK_NAMES = {
    "actionlint",
    "python-test-core",
    "codex-execpolicy",
    "python-test",
    "node-backed-hook-tests",
}

ACCEPTABLE_CONCLUSIONS = {"success"}

# codex-execpolicy is skipped (by its own `if:` condition) during a python_test_bench
# workflow_dispatch run; a "skipped" conclusion for that ONE check name is acceptable
# only when the caller explicitly declares bench mode (see --bench-mode).
BENCH_MODE_SKIPPABLE = {"codex-execpolicy"}

SENTINEL_TERMINAL_STATUSES = {"completed"}


class OperationalError(RuntimeError):
    pass


def _load_json(path: Path, *, label: str) -> Any:
    if not path.is_file():
        raise OperationalError(f"{label} file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OperationalError(f"{label} is not valid JSON: {exc}") from exc


def _check_runs(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("check_runs"), list):
        return payload["check_runs"]
    if isinstance(payload, list):
        return payload
    raise OperationalError("check-runs-api-json must be a GitHub check-runs response object or a list")


def verify(
    *,
    check_runs_payload: Any,
    sentinel_payload: Any,
    expected_head_sha: str,
    bench_mode: bool,
) -> dict[str, Any]:
    violations: list[str] = []
    runs = _check_runs(check_runs_payload)

    # Group by name -> list of runs (a name can legitimately appear more than once
    # across reruns; take the most recently started one for this head SHA).
    by_name: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        name = run.get("name")
        if isinstance(name, str):
            by_name.setdefault(name, []).append(run)

    checks_report: dict[str, Any] = {}
    for name in sorted(REQUIRED_CHECK_NAMES):
        candidates = [
            r for r in by_name.get(name, []) if r.get("head_sha") == expected_head_sha
        ]
        if not candidates:
            violations.append(f"AC9: no check run named {name!r} found at head_sha={expected_head_sha!r}")
            checks_report[name] = {"found": False}
            continue
        # Prefer the run with the highest id (most recent) if there are several.
        candidates.sort(key=lambda r: r.get("id", 0))
        run = candidates[-1]
        status = run.get("status")
        conclusion = run.get("conclusion")
        checks_report[name] = {
            "found": True,
            "status": status,
            "conclusion": conclusion,
            "check_run_id": run.get("id"),
        }
        if status != "completed":
            violations.append(f"AC9: check {name!r} status={status!r} (expected 'completed')")
            continue
        if conclusion in ACCEPTABLE_CONCLUSIONS:
            continue
        if bench_mode and name in BENCH_MODE_SKIPPABLE and conclusion == "skipped":
            continue
        violations.append(
            f"AC9: check {name!r} conclusion={conclusion!r} (expected one of {sorted(ACCEPTABLE_CONCLUSIONS)}"
            + (" or 'skipped' in bench_mode" if name in BENCH_MODE_SKIPPABLE else "")
            + ")"
        )

    sentinel_status = None
    if isinstance(sentinel_payload, dict):
        sentinel_status = sentinel_payload.get("status")
    if bench_mode:
        # In bench mode codex-execpolicy never ran, so no sentinel is expected.
        sentinel_ok = True
    else:
        sentinel_ok = sentinel_status in SENTINEL_TERMINAL_STATUSES
        if not sentinel_ok:
            violations.append(
                f"AC9/AC6: codex_execpolicy_matrix_status_v1.json status={sentinel_status!r} "
                f"(expected one of {sorted(SENTINEL_TERMINAL_STATUSES)})"
            )

    ok = not violations
    return {
        "schema": SCHEMA,
        "ok": ok,
        "expected_head_sha": expected_head_sha,
        "bench_mode": bench_mode,
        "checks": checks_report,
        "sentinel_status": sentinel_status,
        "violations": violations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-runs-api-json", required=True, help="path to the fetched check-runs API JSON")
    parser.add_argument(
        "--codex-sentinel-json",
        required=False,
        default=None,
        help="path to the downloaded codex_execpolicy_matrix_status_v1.json artifact "
        "(omit only with --bench-mode)",
    )
    parser.add_argument("--expected-head-sha", required=True, help="trusted head SHA to match check runs against")
    parser.add_argument(
        "--bench-mode",
        action="store_true",
        help="treat this as a python_test_bench workflow_dispatch run "
        "(codex-execpolicy 'skipped' is acceptable; sentinel is not required)",
    )
    parser.add_argument("--output", default=None, help="optional path to also write the JSON report")
    args = parser.parse_args(argv)

    try:
        check_runs_payload = _load_json(Path(args.check_runs_api_json), label="check-runs-api-json")
        sentinel_payload: Any = None
        if args.codex_sentinel_json is not None:
            sentinel_payload = _load_json(Path(args.codex_sentinel_json), label="codex-sentinel-json")
        elif not args.bench_mode:
            raise OperationalError("--codex-sentinel-json is required unless --bench-mode is set")
    except OperationalError as exc:
        print(json.dumps({"schema": SCHEMA, "ok": False, "operational_error": str(exc)}, indent=2))
        return 3

    report = verify(
        check_runs_payload=check_runs_payload,
        sentinel_payload=sentinel_payload,
        expected_head_sha=args.expected_head_sha,
        bench_mode=args.bench_mode,
    )
    output_text = json.dumps(report, indent=2)
    print(output_text)
    if args.output:
        Path(args.output).write_text(output_text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
