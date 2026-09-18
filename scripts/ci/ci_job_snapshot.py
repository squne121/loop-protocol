#!/usr/bin/env python3
"""ci_job_snapshot.py -- shared attempt-scoped Actions Jobs snapshot helper.

Issue #2631: replaces the former commit-scoped GitHub CheckRuns acquisition
(`GET /repos/{owner}/{repo}/commits/{sha}/check-runs`) with an
attempt-scoped Jobs acquisition
(`GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt_number}/jobs`)
whose identity is anchored to the SAME exact attempt via the request-scoped
exact-attempt endpoint
(`GET /repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt_number}`).

This module performs NO network calls itself. The calling CI job is
responsible for the ``gh api --paginate --slurp`` (Jobs pages) and
``gh api`` (exact-attempt identity) calls; this module only validates,
normalizes, and atomically persists the resulting immutable shared
snapshot so every consumer step in the SAME job reads identical evidence
(never a second independent API fetch -- AC1/AC2/AC3).

Both known acquisition owners (the ``ci-verdict-summary`` normal PR route
and the ``ci-runtime-baseline-gate-ready`` benchmark dispatch route, see
``.github/workflows/ci.yml``) invoke this module's CLI once per job to
build their own immutable snapshot file. Both downstream Python consumers
(``.claude/skills/pr-review-judge/scripts/ci_verdict_summary_v2.py`` and
``scripts/ci/verify_ci_check_conclusions.py``) import this module's
functions directly (never reimplementing the identity/provenance
semantics) to read that snapshot.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

SCHEMA = "ci_job_snapshot_v1"
SCHEMA_VERSION = 1

_CHECK_RUN_URL_HOST = "https://api.github.com/repos/"


class SnapshotError(RuntimeError):
    """Raised for any structural, identity, or provenance failure while
    building, parsing, or consuming a job snapshot.

    Fail-closed: callers MUST NOT treat a caught ``SnapshotError`` as
    success evidence -- it always means the input could not be trusted.
    """


# ---------------------------------------------------------------------------
# Page validation / normalization (AC1)
# ---------------------------------------------------------------------------


def _validate_pages(pages: Any) -> list[dict[str, Any]]:
    """Validate the ``gh api --paginate --slurp`` page array for the Jobs
    endpoint and return the flattened list of raw job rows.

    A partial page fetch, an empty/malformed page array, or a
    ``total_count`` that disagrees with the number of rows actually
    collected is NEVER treated as success evidence (AC1).
    """
    if not isinstance(pages, list) or not pages:
        raise SnapshotError("job_pages_empty_or_invalid")

    flattened: list[Any] = []
    declared_total: int | None = None
    for page in pages:
        if not isinstance(page, dict):
            raise SnapshotError("job_pages_page_not_object")
        total_count = page.get("total_count")
        if not isinstance(total_count, int) or isinstance(total_count, bool):
            raise SnapshotError("job_pages_missing_total_count")
        if declared_total is None:
            declared_total = total_count
        elif total_count != declared_total:
            raise SnapshotError("job_pages_total_count_mismatch_across_pages")
        jobs = page.get("jobs")
        if not isinstance(jobs, list):
            raise SnapshotError("job_pages_missing_jobs_array")
        flattened.extend(jobs)

    if declared_total is None or len(flattened) != declared_total:
        raise SnapshotError("job_pages_incomplete_partial_acquisition")

    return flattened


# ---------------------------------------------------------------------------
# Identity SSOT (AC2)
# ---------------------------------------------------------------------------


def resolve_identity(
    identity_payload: Any,
    *,
    expected_repository: str,
    expected_run_id: int,
) -> dict[str, Any]:
    """Resolve the snapshot identity from the exact-attempt endpoint
    response, the SSOT for ``run_id`` / ``run_attempt`` / ``head_sha`` /
    ``repository`` (AC2). Missing or malformed identity fields are NEVER
    defaulted to a value such as ``1`` or ``latest``.
    """
    if not isinstance(identity_payload, dict):
        raise SnapshotError("identity_payload_not_object")

    run_id = identity_payload.get("id")
    run_attempt = identity_payload.get("run_attempt")
    head_sha = identity_payload.get("head_sha")
    run_started_at = identity_payload.get("run_started_at")
    repository = identity_payload.get("repository")
    repo_full_name = repository.get("full_name") if isinstance(repository, dict) else None

    if not isinstance(run_id, int) or isinstance(run_id, bool):
        raise SnapshotError("identity_missing_or_malformed_run_id")
    if not isinstance(run_attempt, int) or isinstance(run_attempt, bool):
        raise SnapshotError("identity_missing_or_malformed_run_attempt")
    if not isinstance(head_sha, str) or not head_sha:
        raise SnapshotError("identity_missing_or_malformed_head_sha")
    if not isinstance(run_started_at, str) or not run_started_at:
        raise SnapshotError("identity_missing_or_malformed_run_started_at")
    if not isinstance(repo_full_name, str) or not repo_full_name:
        raise SnapshotError("identity_missing_or_malformed_repository")

    if run_id != expected_run_id:
        raise SnapshotError("identity_run_id_mismatch")
    if repo_full_name != expected_repository:
        raise SnapshotError("identity_repository_mismatch")

    return {
        "run_id": run_id,
        "run_attempt": run_attempt,
        "head_sha": head_sha,
        "repository": repo_full_name,
        "run_started_at": run_started_at,
    }


def _normalize_job_row(row: Any, *, identity: dict[str, Any]) -> dict[str, Any]:
    """Normalize one Jobs API row. ``run_id`` and ``head_sha`` are a
    REQUIRED match against the identity SSOT; ``run_attempt`` is only
    cross-checked when the row actually carries it (AC2 -- a job row
    missing ``run_attempt`` is never invalid on that basis alone)."""
    if not isinstance(row, dict):
        raise SnapshotError("job_row_not_object")

    name = row.get("name")
    job_id = row.get("id")
    run_id = row.get("run_id")
    head_sha = row.get("head_sha")
    row_run_attempt = row.get("run_attempt")

    if not isinstance(name, str) or not name:
        raise SnapshotError("job_row_missing_name")
    if not isinstance(job_id, int) or isinstance(job_id, bool):
        raise SnapshotError(f"job_row_missing_id:{name}")
    if run_id != identity["run_id"]:
        raise SnapshotError(f"job_row_run_id_mismatch:{name}")
    if head_sha != identity["head_sha"]:
        raise SnapshotError(f"job_row_head_sha_mismatch:{name}")
    if row_run_attempt is not None and row_run_attempt != identity["run_attempt"]:
        raise SnapshotError(f"job_row_run_attempt_mismatch:{name}")

    return {
        "name": name,
        "id": job_id,
        "status": row.get("status"),
        "conclusion": row.get("conclusion"),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "check_run_url": row.get("check_run_url"),
        "run_id": run_id,
        "head_sha": head_sha,
        "run_attempt": row_run_attempt,
    }


def build_snapshot(
    *,
    pages: Any,
    identity_payload: Any,
    expected_repository: str,
    expected_run_id: int,
    expected_head_sha: str,
) -> dict[str, Any]:
    """Build the immutable ``ci_job_snapshot_v1`` envelope from the raw
    ``gh api --paginate --slurp`` page array and the exact-attempt identity
    response. Raises ``SnapshotError`` (fail-closed) for any structural,
    completeness, or identity violation -- never returns a partial or
    best-effort snapshot."""
    identity = resolve_identity(
        identity_payload,
        expected_repository=expected_repository,
        expected_run_id=expected_run_id,
    )
    if identity["head_sha"] != expected_head_sha:
        raise SnapshotError("identity_head_sha_mismatch")

    raw_jobs = _validate_pages(pages)
    jobs = [_normalize_job_row(row, identity=identity) for row in raw_jobs]

    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "expected_repository": expected_repository,
        "workflow_run_id": identity["run_id"],
        "workflow_run_attempt": identity["run_attempt"],
        "expected_head_sha": expected_head_sha,
        "run_started_at": identity["run_started_at"],
        "jobs": jobs,
    }


# ---------------------------------------------------------------------------
# tmp -> atomic rename persistence
# ---------------------------------------------------------------------------


def atomic_write_snapshot(snapshot: dict[str, Any], output_path: str | Path) -> None:
    """Write ``snapshot`` to a tmp file in the SAME directory as
    ``output_path`` and atomically rename it into place (``os.replace``).
    A failed write never leaves a partial file at ``output_path``."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=str(output_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2)
            fh.write("\n")
        os.replace(tmp_name, output_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_snapshot(path: str | Path) -> dict[str, Any]:
    """Load and structurally validate a previously written snapshot file."""
    path = Path(path)
    if not path.is_file():
        raise SnapshotError(f"snapshot_file_not_found:{path}")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"snapshot_file_invalid_json:{exc}") from exc
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SCHEMA:
        raise SnapshotError("snapshot_schema_mismatch")
    if not isinstance(snapshot.get("jobs"), list):
        raise SnapshotError("snapshot_jobs_missing")
    return snapshot


# ---------------------------------------------------------------------------
# Lookups (AC8: duplicate job name deterministic reject)
# ---------------------------------------------------------------------------


def find_jobs_by_name(snapshot: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [j for j in snapshot.get("jobs", []) if j.get("name") == name]


def resolve_unique_job(snapshot: dict[str, Any], name: str) -> dict[str, Any]:
    """AC8: a duplicate job name is deterministically REJECTED -- never
    disambiguated by e.g. picking the highest id."""
    candidates = find_jobs_by_name(snapshot, name)
    if not candidates:
        raise SnapshotError(f"job_not_found:{name}")
    if len(candidates) > 1:
        raise SnapshotError(f"duplicate_job_name:{name}")
    return candidates[0]


# ---------------------------------------------------------------------------
# check_run_url strict parse + conditional dereference (AC6/AC7)
# ---------------------------------------------------------------------------


def parse_check_run_url(url: Any, *, expected_owner_repo: str) -> int:
    """AC6: strict-parse a Jobs API ``check_run_url``.

    Only ``https://api.github.com/repos/{expected_owner_repo}/check-runs/<positive-int>``
    is accepted -- wrong host, wrong owner/repo, or any non-canonical
    suffix is rejected. Returns the parsed CheckRun id.
    """
    if not isinstance(url, str) or not url:
        raise SnapshotError("check_run_url_missing")
    prefix = f"{_CHECK_RUN_URL_HOST}{expected_owner_repo}/check-runs/"
    if not url.startswith(prefix):
        raise SnapshotError("check_run_url_wrong_host_or_repo")
    suffix = url[len(prefix) :]
    if not suffix or not suffix.isdigit():
        raise SnapshotError("check_run_url_noncanonical")
    check_run_id = int(suffix)
    if check_run_id <= 0:
        raise SnapshotError("check_run_url_noncanonical")
    return check_run_id


def verify_check_run_binding(
    job_row: dict[str, Any],
    *,
    expected_owner_repo: str,
    dereference_fn: Callable[[int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """AC6/AC7: strict-parse ``check_run_url`` and confirm it names the SAME
    CheckRun as this Jobs row's own ``id`` (both values identify the SAME
    underlying GitHub object per the documented Jobs API contract -- no
    dereference is needed in the ordinary case, AC7's "unneeded => zero
    calls"). A single ``checks: read`` Checks API dereference call is made
    ONLY when the URL-derived id disagrees with the job's own id (AC7's
    "needed => exactly one call"), and the dereferenced response's own
    ``id`` must then match BOTH the URL-derived id and the job's id.
    """
    job_id = job_row.get("id")
    if not isinstance(job_id, int) or isinstance(job_id, bool):
        raise SnapshotError("check_run_binding_missing_job_id")

    url_check_run_id = parse_check_run_url(
        job_row.get("check_run_url"), expected_owner_repo=expected_owner_repo
    )

    if url_check_run_id == job_id:
        return {"check_run_id": job_id, "dereferenced": False}

    if dereference_fn is None:
        raise SnapshotError("check_run_url_id_mismatch_undereferenced")

    response = dereference_fn(url_check_run_id)
    if not isinstance(response, dict):
        raise SnapshotError("check_run_dereference_invalid_response")
    response_id = response.get("id")
    if response_id != url_check_run_id or response_id != job_id:
        raise SnapshotError("check_run_dereference_id_mismatch")
    return {"check_run_id": response_id, "dereferenced": True}


# ---------------------------------------------------------------------------
# Bridge to ci_verdict_summary_v2's raw_checks shape
# ---------------------------------------------------------------------------

DEFAULT_EXCLUDED_JOB_NAMES: frozenset[str] = frozenset({"ci-verdict-summary"})


def job_snapshot_to_raw_checks(
    snapshot: dict[str, Any],
    *,
    workflow: str = "ci",
    exclude_job_names: frozenset[str] = DEFAULT_EXCLUDED_JOB_NAMES,
) -> list[dict[str, Any]]:
    """Bridge a validated job snapshot to the ``raw_checks`` shape consumed
    by ``ci_verdict_summary_v2.generate_verdict`` -- the SAME shared
    snapshot ``scripts/ci/verify_ci_check_conclusions.py`` also consumes,
    so both reach identical identity/provenance conclusions (AC2/AC3).

    ``exclude_job_names`` drops the caller's own in-progress job (it is not
    upstream input evidence). AC8: duplicate job names anywhere in the
    snapshot (including excluded ones) are deterministically rejected,
    never silently disambiguated.
    """
    jobs = snapshot.get("jobs", [])
    seen_names: dict[str, int] = {}
    for job in jobs:
        name = job.get("name")
        if not isinstance(name, str) or not name:
            raise SnapshotError("job_snapshot_row_missing_name")
        seen_names[name] = seen_names.get(name, 0) + 1

    duplicates = sorted(n for n, count in seen_names.items() if count > 1)
    if duplicates:
        raise SnapshotError(f"duplicate_job_name:{duplicates}")

    raw_checks: list[dict[str, Any]] = []
    for job in jobs:
        name = job["name"]
        if name in exclude_job_names:
            continue
        raw_checks.append(
            {
                "name": name,
                "workflow": workflow,
                "status": job.get("status"),
                "conclusion": job.get("conclusion"),
                "head_sha": job.get("head_sha"),
                "check_run_id": job.get("id"),
                "check_run_url": job.get("check_run_url"),
                "provenance": "github_actions_job_api",
            }
        )
    if not raw_checks:
        raise SnapshotError("job_snapshot_no_current_workflow_evidence")
    return raw_checks


# ---------------------------------------------------------------------------
# CLI (used identically by both acquisition owner jobs in ci.yml)
# ---------------------------------------------------------------------------


def _read_json_file(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pages-json",
        required=True,
        help="path to the 'gh api --paginate --slurp' Jobs endpoint page array",
    )
    parser.add_argument(
        "--identity-json",
        required=True,
        help="path to the exact-attempt endpoint (GET .../attempts/{n}) response JSON",
    )
    parser.add_argument("--expected-repository", required=True, help="owner/repo")
    parser.add_argument("--expected-run-id", required=True, type=int)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--output", required=True, help="atomic-write destination for the shared snapshot")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        pages = _read_json_file(args.pages_json)
        identity_payload = _read_json_file(args.identity_json)
        snapshot = build_snapshot(
            pages=pages,
            identity_payload=identity_payload,
            expected_repository=args.expected_repository,
            expected_run_id=args.expected_run_id,
            expected_head_sha=args.expected_head_sha,
        )
        atomic_write_snapshot(snapshot, args.output)
    except (OSError, json.JSONDecodeError, SnapshotError) as exc:
        print(json.dumps({"schema": SCHEMA, "ok": False, "error": str(exc)}, indent=2))
        return 1

    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "ok": True,
                "output": str(args.output),
                "job_count": len(snapshot["jobs"]),
                "workflow_run_id": snapshot["workflow_run_id"],
                "workflow_run_attempt": snapshot["workflow_run_attempt"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
