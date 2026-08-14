#!/usr/bin/env python3
"""
scripts/ci/collect_e2e_performance_benchmark.py

Issue #2159 AC1/AC2/AC7: cross-run collector that assembles a
`e2e_performance_benchmark_manifest_v1`-conformant immutable manifest for a
fixed before/after commit SHA e2e performance benchmark experiment (the
#1064 "fixed pre/post SHA" benchmark design applied to Issue #2119's E2E
lane split).

This script does NOT make live GitHub API calls itself. Consistent with
the existing pattern in this repo (see
`scripts/ci/verify_ci_check_conclusions.py`'s module docstring: "Inputs
are two already-fetched JSON documents (no live network calls from this
script...)"), the CI job (or a human operator via `gh api` /
`gh run list` / `gh api .../artifacts`) is responsible for fetching the
per-run workflow-run + artifact metadata for the fixed before/after SHA's
dedicated `workflow_dispatch` benchmark route, and supplies that already-
fetched data to this script as `--before-runs-json` / `--after-runs-json`.
This keeps the collector itself hermetic and unit-testable without a live
network dependency, and keeps a single point of truth
(`_verify_run_record` / `_dedupe_by_workflow_run_id`) for the
sample-identity and artifact-verification rules, shared conceptually
(same rules, independently implemented per the Allowed Paths boundary)
with `tests/ci/test_ci_performance_gate.py`.

Each element of the `--before-runs-json` / `--after-runs-json` input array
is expected to carry (at minimum):

    {
      "workflow_run_id": <int>,
      "job": "e2e-core" | "e2e-responsive-matrix" | "e2e",
      "head_sha": "<40-hex commit sha>",
      "artifact_id": <int>,
      "artifact_digest": "sha256:<hex>",
      "conclusion": "success" | "failure" | "cancelled" | "skipped" | "timed_out"
    }

Exit codes:
  0 = manifest written AND every job in every arm reached >= --min-runs
      deduped `workflow_run_id` samples (arms.*.complete == true).
  2 = manifest written but INCOMPLETE (insufficient evidence for at least
      one job) -- this is the AC11-consistent fail-closed signal; callers
      that require a complete benchmark (e.g. a close-verification step)
      must treat this as a hard failure, not silently proceed.
  3 = operational failure (missing/unparseable input file, missing schema,
      malformed CLI arguments).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SCHEMA_PATH = os.path.join(REPO_ROOT, "schemas", "e2e_performance_benchmark_manifest_v1.schema.json")

EXIT_COMPLETE = 0
EXIT_INCOMPLETE = 2
EXIT_OPERATIONAL_FAILURE = 3

DEFAULT_MIN_RUN_COUNT = 20
DEFAULT_JOB_NAMES = ("e2e-core", "e2e-responsive-matrix", "e2e")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class OperationalError(RuntimeError):
    pass


def _load_json_file(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise OperationalError(f"file_not_readable: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OperationalError(f"json_parse_error: {path}: {exc}") from exc


def _is_valid_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.match(value))


def _is_valid_digest(value: object) -> bool:
    return isinstance(value, str) and bool(_DIGEST_RE.match(value))


def _verify_run_record(record: dict, expected_head_sha: str) -> list[str]:
    """#2159 AC7: verifies artifact ID / artifact digest / head SHA / job
    are all present and well-formed for a single run record. Returns a
    list of violation reason strings (empty list == record is usable)."""
    violations: list[str] = []
    if not isinstance(record.get("workflow_run_id"), int) or record.get("workflow_run_id", 0) < 1:
        violations.append("missing_or_invalid_workflow_run_id")
    if not isinstance(record.get("job"), str) or not record.get("job"):
        violations.append("missing_or_invalid_job")
    if not _is_valid_sha(record.get("head_sha")):
        violations.append("missing_or_invalid_head_sha")
    elif record.get("head_sha") != expected_head_sha:
        violations.append("head_sha_mismatch")
    if not isinstance(record.get("artifact_id"), int) or record.get("artifact_id", 0) < 1:
        violations.append("missing_or_invalid_artifact_id")
    if not _is_valid_digest(record.get("artifact_digest")):
        violations.append("missing_or_invalid_artifact_digest")
    if record.get("conclusion") not in ("success", "failure", "cancelled", "skipped", "timed_out"):
        violations.append("missing_or_invalid_conclusion")
    return violations


def _dedupe_by_workflow_run_id(records: list[dict]) -> list[dict]:
    """#2159 AC2/P1-1: sample identity is `workflow_run_id`, never `(run_id,
    run_attempt)`. Keeps the first record seen per `workflow_run_id`."""
    seen: dict[int, dict] = {}
    for record in records:
        workflow_run_id = record.get("workflow_run_id")
        if workflow_run_id is None:
            continue
        seen.setdefault(workflow_run_id, record)
    return list(seen.values())


def _collect_arm(
    arm_name: str,
    commit_sha: str,
    raw_records: list[dict],
    job_names: tuple[str, ...],
    min_run_count: int,
    evidence_errors: list[dict],
) -> dict:
    usable_by_job: dict[str, list[dict]] = {job: [] for job in job_names}

    for index, record in enumerate(raw_records):
        if not isinstance(record, dict):
            evidence_errors.append(
                {"arm": arm_name, "reason": "non_object_record", "detail": f"index={index}"}
            )
            continue
        violations = _verify_run_record(record, commit_sha)
        if violations:
            evidence_errors.append(
                {
                    "arm": arm_name,
                    "reason": "run_record_verification_failed",
                    "detail": f"workflow_run_id={record.get('workflow_run_id')!r} violations={violations}",
                }
            )
            continue
        job = record["job"]
        if job not in usable_by_job:
            # Not one of the jobs this manifest tracks -- not an error,
            # simply out of scope for this benchmark's job set.
            continue
        usable_by_job[job].append(record)

    jobs: dict[str, dict] = {}
    complete = True
    for job in job_names:
        deduped = _dedupe_by_workflow_run_id(usable_by_job[job])
        run_count = len(deduped)
        if run_count < min_run_count:
            complete = False
        jobs[job] = {
            "job": job,
            "run_count": run_count,
            "sample_workflow_run_ids": sorted(r["workflow_run_id"] for r in deduped),
            "runs": [
                {
                    "workflow_run_id": r["workflow_run_id"],
                    "job": r["job"],
                    "head_sha": r["head_sha"],
                    "artifact_id": r["artifact_id"],
                    "artifact_digest": r["artifact_digest"],
                    "conclusion": r["conclusion"],
                }
                for r in deduped
            ],
        }

    return {
        "commit_sha": commit_sha,
        "jobs": jobs,
        "complete": complete,
    }


def collect_benchmark_manifest(
    before_sha: str,
    after_sha: str,
    before_records: list[dict],
    after_records: list[dict],
    job_names: tuple[str, ...] = DEFAULT_JOB_NAMES,
    min_run_count: int = DEFAULT_MIN_RUN_COUNT,
    generated_at: str | None = None,
) -> dict:
    if not _is_valid_sha(before_sha):
        raise OperationalError(f"invalid_before_sha: {before_sha!r}")
    if not _is_valid_sha(after_sha):
        raise OperationalError(f"invalid_after_sha: {after_sha!r}")

    evidence_errors: list[dict] = []
    before_arm = _collect_arm("before", before_sha, before_records, job_names, min_run_count, evidence_errors)
    after_arm = _collect_arm("after", after_sha, after_records, job_names, min_run_count, evidence_errors)

    return {
        "schema": "e2e_performance_benchmark_manifest_v1",
        "schema_version": 1,
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "before_sha": before_sha,
        "after_sha": after_sha,
        "min_run_count": min_run_count,
        "arms": {
            "before": before_arm,
            "after": after_arm,
        },
        "evidence_errors": evidence_errors,
    }


def _validate_against_schema(manifest: dict) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise OperationalError(f"jsonschema_not_installed: {exc}") from exc

    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    validator_instance = Draft202012Validator(schema)
    errors = sorted(validator_instance.iter_errors(manifest), key=lambda e: e.path)
    if errors:
        messages = [f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}" for err in errors]
        raise OperationalError(f"manifest_failed_schema_validation: {messages}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect a fixed before/after SHA e2e performance benchmark manifest "
            "(Issue #2159 AC1/AC2/AC7) from already-fetched workflow-run/artifact "
            "metadata JSON documents."
        )
    )
    parser.add_argument("--before-sha", required=True, help="Fixed 40-hex before commit SHA")
    parser.add_argument("--after-sha", required=True, help="Fixed 40-hex after commit SHA")
    parser.add_argument(
        "--before-runs-json",
        required=True,
        help="Path to already-fetched JSON array of before-arm run records",
    )
    parser.add_argument(
        "--after-runs-json",
        required=True,
        help="Path to already-fetched JSON array of after-arm run records",
    )
    parser.add_argument("--output", required=True, help="Path to write the manifest JSON")
    parser.add_argument(
        "--min-runs",
        type=int,
        default=DEFAULT_MIN_RUN_COUNT,
        help=f"Minimum deduped workflow_run_id sample count required per job (default {DEFAULT_MIN_RUN_COUNT})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        before_records = _load_json_file(args.before_runs_json)
        after_records = _load_json_file(args.after_runs_json)
        if not isinstance(before_records, list) or not isinstance(after_records, list):
            raise OperationalError("before/after runs JSON must each be a JSON array")

        manifest = collect_benchmark_manifest(
            args.before_sha,
            args.after_sha,
            before_records,
            after_records,
            min_run_count=args.min_runs,
        )
        _validate_against_schema(manifest)
    except OperationalError as exc:
        sys.stderr.write(f"operational_failure: {exc}\n")
        return EXIT_OPERATIONAL_FAILURE

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")

    complete = manifest["arms"]["before"]["complete"] and manifest["arms"]["after"]["complete"]
    return EXIT_COMPLETE if complete else EXIT_INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
