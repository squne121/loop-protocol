#!/usr/bin/env python3
"""Verify real CI check-run conclusions (Issue #1760).

Issue #2161 (native Codex CLI retirement): the former codex-execpolicy job,
its AC6 sentinel artifact (``codex_execpolicy_matrix_status_v1.json``), and
the ``--codex-sentinel-json`` verification this script performed against it
were removed with the job. The remaining AC9 contract still requires runtime
evidence for the same head SHA -- not a string search over ``ci.yml`` -- that
``actionlint`` / ``python-test-core`` / ``python-test`` (required aggregate) /
``node-backed-hook-tests`` all completed with an acceptable conclusion.

Issue #1824 P1-3 review: a check-run candidate set that is only grouped by
(name, head_sha) can accept a MIXED-PROVENANCE result set when the same commit
has several Actions runs (e.g. a manual rerun) -- picking "the highest id" per
name does not guarantee every accepted row belongs to the SAME workflow run.
``--workflow-run-id`` / ``--workflow-run-attempt`` are now REQUIRED, and every
accepted check-run row's ``details_url`` must reference that exact run id (the
same binding rule ``ci_verdict_summary_v2.filter_check_runs_by_workflow_run``
already applies -- reused here, not reimplemented, to avoid drift).

Input is one already-fetched JSON document (no live network calls from this
script -- the CI job that invokes it is responsible for the ``gh api`` call,
matching the existing ``ci_verdict_summary_v2.py`` pattern):

  --check-runs-api-json   raw ``GET /repos/{owner}/{repo}/commits/{sha}/check-runs``
                           response body (the same file the ci-verdict-summary job
                           already produces as ``ci_verdict_summary_v2_check_runs.json``)

Exit 0 = every required check name has a matching, current-head, SAME-RUN,
successful check run. Exit 2 = any invariant violated (fail-closed). Exit 3 =
operational failure (missing/unparseable input file, missing required CLI
argument).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

SCHEMA = "ci_check_conclusions_verification_v1"

REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_VERDICT_SUMMARY_V2_PATH = (
    REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "scripts" / "ci_verdict_summary_v2.py"
)

# AC9: the exact set of check names that must be verified against real, current-head,
# same-workflow-run check-run evidence. "python-test" here is the REQUIRED AGGREGATE
# job (AC5), distinct from "python-test-core".
REQUIRED_CHECK_NAMES = {
    "actionlint",
    "python-test-core",
    "python-test",
    "node-backed-hook-tests",
}

ACCEPTABLE_CONCLUSIONS = {"success"}

# Issue #2161: codex-execpolicy (the sole BENCH_MODE_SKIPPABLE check name) was
# removed with native Codex CLI retirement, so no required check name is
# currently bench-mode-skippable. --bench-mode is retained as an accepted CLI
# flag for call-site compatibility; it no longer changes verification outcome.
BENCH_MODE_SKIPPABLE: set[str] = set()


class OperationalError(RuntimeError):
    pass


def _load_json(path: Path, *, label: str) -> Any:
    if not path.is_file():
        raise OperationalError(f"{label} file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OperationalError(f"{label} is not valid JSON: {exc}") from exc


def _load_ci_verdict_summary_v2_module() -> ModuleType:
    """Dynamically load the canonical producer module for
    ``filter_check_runs_by_workflow_run`` (shared, not reimplemented -- Issue #1824
    P1-3 review)."""
    spec = importlib.util.spec_from_file_location(
        "ci_verdict_summary_v2", _CI_VERDICT_SUMMARY_V2_PATH
    )
    if spec is None or spec.loader is None:
        raise OperationalError(
            f"unable to load ci_verdict_summary_v2 module from {_CI_VERDICT_SUMMARY_V2_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify(
    *,
    check_runs_payload: Any,
    expected_head_sha: str,
    workflow_run_id: int,
    workflow_run_attempt: int | None,
    bench_mode: bool,
    _filter_check_runs_by_workflow_run: Any = None,
) -> dict[str, Any]:
    violations: list[str] = []

    filter_fn = _filter_check_runs_by_workflow_run
    if filter_fn is None:
        module = _load_ci_verdict_summary_v2_module()
        filter_fn = module.filter_check_runs_by_workflow_run

    try:
        same_run_rows = filter_fn(check_runs_payload, workflow_run_id=workflow_run_id)
    except ValueError as exc:
        raise OperationalError(f"check-runs-api-json invalid: {exc}") from exc

    # Only rows bound to (a) this exact workflow run AND (b) the expected head SHA
    # are eligible evidence.
    same_run_same_head = [r for r in same_run_rows if r.get("head_sha") == expected_head_sha]

    by_name: dict[str, list[dict[str, Any]]] = {}
    for run in same_run_same_head:
        name = run.get("name")
        if isinstance(name, str):
            by_name.setdefault(name, []).append(run)

    checks_report: dict[str, Any] = {}
    for name in sorted(REQUIRED_CHECK_NAMES):
        candidates = by_name.get(name, [])
        if not candidates:
            violations.append(
                f"AC9: no check run named {name!r} bound to workflow_run_id={workflow_run_id} "
                f"at head_sha={expected_head_sha!r}"
            )
            checks_report[name] = {"found": False}
            continue
        if len(candidates) > 1:
            violations.append(
                f"AC9: multiple same-run check runs named {name!r} bound to "
                f"workflow_run_id={workflow_run_id} (ambiguous evidence)"
            )
        # Same-run binding already guarantees a single logical attempt per name;
        # take the highest id defensively if duplicates still slip through.
        candidates = sorted(candidates, key=lambda r: r.get("id", 0))
        run = candidates[-1]
        status = run.get("status")
        conclusion = run.get("conclusion")
        checks_report[name] = {
            "found": True,
            "status": status,
            "conclusion": conclusion,
            "check_run_id": run.get("id"),
            "details_url": run.get("details_url") or run.get("detailsUrl"),
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

    ok = not violations
    return {
        "schema": SCHEMA,
        "ok": ok,
        "expected_head_sha": expected_head_sha,
        "workflow_run_id": workflow_run_id,
        "workflow_run_attempt": workflow_run_attempt,
        "bench_mode": bench_mode,
        "checks": checks_report,
        "violations": violations,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-runs-api-json", required=True, help="path to the fetched check-runs API JSON")
    parser.add_argument("--expected-head-sha", required=True, help="trusted head SHA to match check runs against")
    parser.add_argument(
        "--workflow-run-id",
        required=True,
        type=int,
        help="the Actions workflow_run_id this verification is bound to (required, "
        "Issue #1824 P1-3: prevents mixed-provenance evidence from a different rerun "
        "of the same commit)",
    )
    parser.add_argument(
        "--workflow-run-attempt",
        required=False,
        type=int,
        default=None,
        help="the Actions workflow_run_attempt (cross-checked against the sentinel "
        "artifact's own declared run_attempt when present)",
    )
    parser.add_argument(
        "--bench-mode",
        action="store_true",
        help="treat this as a python_test_bench workflow_dispatch run "
        "(retained for call-site compatibility; no required check name is "
        "currently bench-mode-skippable, see BENCH_MODE_SKIPPABLE)",
    )
    parser.add_argument("--output", default=None, help="optional path to also write the JSON report")
    args = parser.parse_args(argv)

    try:
        check_runs_payload = _load_json(Path(args.check_runs_api_json), label="check-runs-api-json")
    except OperationalError as exc:
        print(json.dumps({"schema": SCHEMA, "ok": False, "operational_error": str(exc)}, indent=2))
        return 3

    try:
        report = verify(
            check_runs_payload=check_runs_payload,
            expected_head_sha=args.expected_head_sha,
            workflow_run_id=args.workflow_run_id,
            workflow_run_attempt=args.workflow_run_attempt,
            bench_mode=args.bench_mode,
        )
    except OperationalError as exc:
        print(json.dumps({"schema": SCHEMA, "ok": False, "operational_error": str(exc)}, indent=2))
        return 3

    output_text = json.dumps(report, indent=2)
    print(output_text)
    if args.output:
        Path(args.output).write_text(output_text + "\n", encoding="utf-8")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
