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
``--workflow-run-id`` / ``--workflow-run-attempt`` are REQUIRED, and evidence
is only accepted from an attempt-scoped Actions Jobs snapshot already bound
to that exact run/attempt/head identity.

Issue #2631: this script no longer performs its own commit-scoped GitHub
CheckRuns fetch or re-derives run binding via a ``details_url`` substring
heuristic. Input is one already-validated ``ci_job_snapshot_v1`` file (Issue
#2631; produced by ``scripts/ci/ci_job_snapshot.py`` from the documented
``GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs``
endpoint, identity-anchored to the exact-attempt endpoint) -- the SAME shared
snapshot ``.claude/skills/pr-review-judge/scripts/ci_verdict_summary_v2.py``
consumes, so both reach identical identity/provenance conclusions. No live
network calls are made from this script (the CI job that invokes it is
responsible for building the snapshot).

  --job-snapshot-json   path to the attempt-scoped ci_job_snapshot_v1 file

Exit 0 = every required check name has a matching, current-attempt,
successful job. Exit 2 = any invariant violated (fail-closed). Exit 3 =
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
_CI_JOB_SNAPSHOT_PATH = REPO_ROOT / "scripts" / "ci" / "ci_job_snapshot.py"

# AC9: the exact set of check names that must be verified against real, current-head,
# same-workflow-run job evidence. "python-test" here is the REQUIRED AGGREGATE
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


def _load_ci_job_snapshot_module() -> ModuleType:
    """Dynamically load the canonical shared job-snapshot helper (Issue
    #2631, not reimplemented -- the SAME helper
    ``.claude/skills/pr-review-judge/scripts/ci_verdict_summary_v2.py``
    loads)."""
    spec = importlib.util.spec_from_file_location("ci_job_snapshot", _CI_JOB_SNAPSHOT_PATH)
    if spec is None or spec.loader is None:
        raise OperationalError(f"unable to load ci_job_snapshot module from {_CI_JOB_SNAPSHOT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify(
    *,
    snapshot: dict[str, Any],
    expected_repository: str,
    expected_head_sha: str,
    workflow_run_id: int,
    workflow_run_attempt: int | None,
    bench_mode: bool,
    _ci_job_snapshot: Any = None,
) -> dict[str, Any]:
    violations: list[str] = []

    module = _ci_job_snapshot
    if module is None:
        module = _load_ci_job_snapshot_module()

    # AC2: this consumer independently re-checks the snapshot's own baked-in
    # identity against ITS trusted inputs rather than blindly trusting the
    # snapshot file's content -- defense in depth on top of
    # ci_job_snapshot.build_snapshot's own identity binding.
    if snapshot.get("expected_repository") != expected_repository:
        violations.append(
            f"AC2: job snapshot expected_repository={snapshot.get('expected_repository')!r} "
            f"!= expected {expected_repository!r}"
        )
    if snapshot.get("workflow_run_id") != workflow_run_id:
        violations.append(
            f"AC2: job snapshot workflow_run_id={snapshot.get('workflow_run_id')!r} "
            f"!= expected {workflow_run_id!r}"
        )
    if snapshot.get("expected_head_sha") != expected_head_sha:
        violations.append(
            f"AC2: job snapshot expected_head_sha={snapshot.get('expected_head_sha')!r} "
            f"!= expected {expected_head_sha!r}"
        )
    if (
        workflow_run_attempt is not None
        and snapshot.get("workflow_run_attempt") is not None
        and snapshot.get("workflow_run_attempt") != workflow_run_attempt
    ):
        violations.append(
            f"AC2: job snapshot workflow_run_attempt={snapshot.get('workflow_run_attempt')!r} "
            f"!= expected {workflow_run_attempt!r}"
        )

    checks_report: dict[str, Any] = {}

    if not violations:
        for name in sorted(REQUIRED_CHECK_NAMES):
            candidates = module.find_jobs_by_name(snapshot, name)
            if not candidates:
                violations.append(
                    f"AC9: no job named {name!r} in the attempt-scoped snapshot "
                    f"bound to workflow_run_id={workflow_run_id} at head_sha={expected_head_sha!r}"
                )
                checks_report[name] = {"found": False}
                continue
            if len(candidates) > 1:
                # AC8: duplicate job name is a deterministic reject -- never
                # disambiguated by e.g. picking the highest id.
                violations.append(
                    f"AC8: multiple jobs named {name!r} in the attempt-scoped snapshot "
                    "(ambiguous evidence, deterministic reject)"
                )
                checks_report[name] = {"found": True, "duplicate": True}
                continue

            job = candidates[0]
            status = job.get("status")
            conclusion = job.get("conclusion")
            checks_report[name] = {
                "found": True,
                "status": status,
                "conclusion": conclusion,
                "job_id": job.get("id"),
                "check_run_url": job.get("check_run_url"),
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
    parser.add_argument(
        "--job-snapshot-json",
        required=True,
        help="path to an attempt-scoped ci_job_snapshot_v1 file (Issue #2631)",
    )
    parser.add_argument("--repository", required=True, help="owner/repo this snapshot must be bound to")
    parser.add_argument("--expected-head-sha", required=True, help="trusted head SHA to match jobs against")
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
        help="the Actions workflow_run_attempt (cross-checked against the snapshot's "
        "own declared run_attempt when present)",
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
        ci_job_snapshot = _load_ci_job_snapshot_module()
        snapshot = ci_job_snapshot.load_snapshot(args.job_snapshot_json)
    except OperationalError as exc:
        print(json.dumps({"schema": SCHEMA, "ok": False, "operational_error": str(exc)}, indent=2))
        return 3
    except ci_job_snapshot.SnapshotError as exc:
        print(json.dumps({"schema": SCHEMA, "ok": False, "operational_error": str(exc)}, indent=2))
        return 3

    try:
        report = verify(
            snapshot=snapshot,
            expected_repository=args.repository,
            expected_head_sha=args.expected_head_sha,
            workflow_run_id=args.workflow_run_id,
            workflow_run_attempt=args.workflow_run_attempt,
            bench_mode=args.bench_mode,
            _ci_job_snapshot=ci_job_snapshot,
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
