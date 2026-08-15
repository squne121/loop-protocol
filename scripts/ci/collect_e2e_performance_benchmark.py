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
      "conclusion": "success" | "failure" | "cancelled" | "skipped" | "timed_out",
      "workflow_digest": "<content digest of the workflow file that produced this run>",
      "workflow_sha": "<40-hex sha of the WORKFLOW DEFINITION;
        distinct from head_sha (#2159 issuecomment-5299412215 item 1)>"
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
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Callable

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SCHEMA_PATH = os.path.join(REPO_ROOT, "schemas", "e2e_performance_benchmark_manifest_v1.schema.json")

EXIT_COMPLETE = 0
EXIT_INCOMPLETE = 2
EXIT_OPERATIONAL_FAILURE = 3

DEFAULT_MIN_RUN_COUNT = 20
DEFAULT_JOB_NAMES = ("e2e-core", "e2e-responsive-matrix", "e2e")

# #2159 P0-4 (fix_delta after adversarial review issuecomment-5295659213):
# before/after are NOT symmetric topologies. `before` (pre-#2137-split) only
# ever produced the monolithic `e2e` job; `after` (post-#2137-split) only
# ever produces `e2e-core` / `e2e-responsive-matrix` (the aggregate `e2e`
# job name is reused post-split for a DIFFERENT purpose -- gate-ready
# latency tracking -- and is intentionally NOT required for `after`'s
# provider critical-path topology). Applying the flat DEFAULT_JOB_NAMES
# symmetrically to both arms made a legitimate `before` cohort permanently
# unable to reach `complete: true` (it can never produce 20 real
# `e2e-core`/`e2e-responsive-matrix` samples -- those jobs did not exist
# pre-split).
DEFAULT_BEFORE_JOB_NAMES = ("e2e",)
DEFAULT_AFTER_JOB_NAMES = ("e2e-core", "e2e-responsive-matrix")

# #2159 P0-4: only a "success" conclusion is eligible to count as a
# performance sample. failure/cancelled/skipped/timed_out runs are excluded
# from run_count/sample accounting and reported as an explicit evidence
# error instead of silently inflating the cohort with non-representative
# timing data (a failed run's elapsed_ms is not a valid performance
# observation).
ELIGIBLE_SAMPLE_CONCLUSIONS = frozenset({"success"})

# --------------------------------------------------------------------------- #
# #2179 (fix_delta after OWNER adversarial review of PR #2172,
# issuecomment-5295659213 P1-1 / follow-up Issue #2179): rerun-attempt
# selection must be a single, explicit, order-independent policy --
# `initial_attempt_only_v1` -- never `dict.setdefault()` first-seen-wins
# insertion-order semantics. A `workflow_run_id`'s sample is the record
# with `run_attempt == 1` (missing `run_attempt` defaults to 1, for
# backward compatibility with records collected before this field
# existed); if attempt 1 is missing/malformed/non-success, that
# `workflow_run_id`'s sample is excluded entirely -- a later successful
# attempt (2, 3, ...) is NEVER substituted in.
# --------------------------------------------------------------------------- #
RERUN_ATTEMPT_SELECTION_POLICY = "initial_attempt_only_v1"


def _normalize_run_attempt(record: dict) -> int | None:
    """#2179 AC9: normalizes `record["run_attempt"]` to the
    `initial_attempt_only_v1` policy's `integer >= 1` contract. The key
    being entirely ABSENT defaults to 1 (backward compatibility with
    records collected before this field existed). An explicit `None`
    value, a non-int (e.g. the string `"2"`), `0`, or a negative value are
    all treated as invalid -- returns `None`, which callers must treat as
    "not eligible as an attempt-1 candidate under any circumstance"
    (fail-closed exclusion, never guessed/coerced)."""
    if "run_attempt" not in record:
        return 1
    value = record.get("run_attempt")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1:
        return None
    return value


def _group_by_workflow_run_id(records: list[dict]) -> dict[int, list[dict]]:
    by_id: dict[int, list[dict]] = {}
    for record in records:
        workflow_run_id = record.get("workflow_run_id")
        if workflow_run_id is None:
            continue
        by_id.setdefault(workflow_run_id, []).append(record)
    return by_id


def _select_initial_attempt_records(records: list[dict]) -> dict[int, dict]:
    """#2179 AC1/AC3: pure `initial_attempt_only_v1` selection -- groups
    `records` by `workflow_run_id` first (never relying on insertion
    order), then keeps only the `run_attempt == 1` candidate per id. If
    more than one record explicitly (or by the missing-defaults-to-1
    fallback) qualifies as attempt 1 for the same `workflow_run_id`, a
    deterministic (content-derived, never insertion-order-dependent)
    tie-break is used -- this is the ordinary "rerun" fixture shape used
    by this module's own regression tests, not a reported collision (see
    `_detect_run_attempt_identity_collisions` for the EXPLICIT-conflict
    case that IS reported/excluded, #2179 AC9)."""
    selected: dict[int, dict] = {}
    for workflow_run_id, group in _group_by_workflow_run_id(records).items():
        candidates = [r for r in group if _normalize_run_attempt(r) == 1]
        if not candidates:
            continue
        selected[workflow_run_id] = min(candidates, key=lambda r: json.dumps(r, sort_keys=True, default=str))
    return selected


def _detect_run_attempt_identity_collisions(records: list[dict]) -> dict[int, list[dict]]:
    """#2179 AC9: within a single job's record pool, flags any
    `workflow_run_id` where >= 2 records EXPLICITLY claim the SAME
    `run_attempt` value but disagree on `artifact_id`/`artifact_digest` --
    a genuine identity collision (never a legitimate rerun, since a rerun
    always increments `run_attempt`). Records that omit `run_attempt`
    entirely (pre-#2179 fixtures, defaulting to attempt 1) are never
    flagged here -- this keeps existing fixtures/tests that share a
    `workflow_run_id` without ever mentioning `run_attempt` fully
    backward compatible (see `_select_initial_attempt_records`'s
    deterministic tie-break for that case)."""
    by_key: dict[tuple, list[dict]] = {}
    for record in records:
        workflow_run_id = record.get("workflow_run_id")
        if workflow_run_id is None:
            continue
        if "run_attempt" not in record or record.get("run_attempt") is None:
            continue
        by_key.setdefault((workflow_run_id, record.get("run_attempt")), []).append(record)

    collisions: dict[int, list[dict]] = {}
    for (workflow_run_id, _run_attempt), group in by_key.items():
        identities = {(r.get("artifact_id"), r.get("artifact_digest")) for r in group}
        if len(group) > 1 and len(identities) > 1:
            collisions.setdefault(workflow_run_id, []).extend(group)
    return collisions


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
    # #2159 P0-9: AC1 claims `workflow_digest` is recorded in the manifest;
    # require it on every run record (not just documented, structurally
    # enforced).
    if not isinstance(record.get("workflow_digest"), str) or not record.get("workflow_digest"):
        violations.append("missing_or_invalid_workflow_digest")
    # #2159 OWNER scope-authority ruling (issuecomment-5299412215, item
    # 1/P0-2): `workflow_sha` (GITHUB_WORKFLOW_SHA -- the workflow
    # DEFINITION's own commit) must be recorded as a field DISTINCT from
    # `head_sha` (the measured application code's commit) -- never
    # conflated, never asserted equal.
    if not _is_valid_sha(record.get("workflow_sha")):
        violations.append("missing_or_invalid_workflow_sha")
    return violations


# --------------------------------------------------------------------------- #
# #2159 P0-3 (fix_delta after adversarial review issuecomment-5295659213):
# `_verify_run_record` above is SHAPE/regex validation only -- it never
# confirms the artifact/run/job actually exist on GitHub, that the artifact
# was generated by the claimed workflow run, or that the archive digest
# matches. A caller who wants trusted (not merely well-formed) records must
# additionally call `verify_run_record_against_live_api` per record BEFORE
# passing them to `collect_benchmark_manifest`. This keeps
# `collect_benchmark_manifest`/`_collect_arm` themselves hermetic (no
# network calls, per the module docstring's existing design) while making
# genuine live-API verification available as an explicit, separate,
# dependency-injectable step -- `api_call` defaults to a real `gh api`
# subprocess invocation but tests inject a fake transport.
# --------------------------------------------------------------------------- #
class LiveAPIError(RuntimeError):
    """Raised when a live GitHub API call itself fails (network/auth/rate
    limit/transport) -- distinct from the call SUCCEEDING but the returned
    data failing verification (which is reported as a violation string,
    not an exception)."""


def _default_gh_api_call(endpoint: str) -> Any:
    """Default `api_call` transport: `gh api <endpoint>` via subprocess,
    parsed as JSON. Requires the `gh` CLI to be authenticated in the
    calling environment (true inside GitHub Actions via the default
    `GITHUB_TOKEN`, per this repo's existing `gh api` usage pattern
    elsewhere in `.github/workflows/ci.yml`)."""
    try:
        result = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiveAPIError(f"gh_api_transport_error: {endpoint}: {exc}") from exc
    if result.returncode != 0:
        raise LiveAPIError(f"gh_api_call_failed: {endpoint}: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LiveAPIError(f"gh_api_response_not_json: {endpoint}: {exc}") from exc


DEFAULT_ARTIFACTS_PER_PAGE = 100


def _fetch_all_artifacts_paginated(
    workflow_run_id: object,
    repo: str,
    api_call: Callable[[str], Any],
) -> list[dict]:
    """#2179 AC8: GitHub's artifact-listing API paginates (`total_count` +
    an `artifacts` array truncated to `per_page`, default 30/max 100). The
    pre-#2179 single `api_call(f"repos/{repo}/actions/runs/{id}/artifacts")`
    call only ever read page 1 -- a `workflow_run_id` with more artifacts
    than fit on one page (heavy rerun usage across many jobs/attempts)
    could report `artifact_not_found_via_live_api` for a genuinely-existing
    artifact that only appears on page 2+. Walks every page until every
    `total_count` artifact has been collected (or a page returns nothing,
    signalling the last page). The FIRST call intentionally omits any
    query string (preserves the pre-#2179 exact-endpoint shape) -- only
    when `total_count` indicates more artifacts than page 1 returned does
    this fetch page 2+."""
    base_endpoint = f"repos/{repo}/actions/runs/{workflow_run_id}/artifacts"
    first_page = api_call(base_endpoint)
    artifacts = list(first_page.get("artifacts", [])) if isinstance(first_page, dict) else []
    total_count = first_page.get("total_count") if isinstance(first_page, dict) else None

    if not isinstance(total_count, int) or total_count <= len(artifacts):
        return artifacts

    page = 2
    while len(artifacts) < total_count:
        page_response = api_call(f"{base_endpoint}?per_page={DEFAULT_ARTIFACTS_PER_PAGE}&page={page}")
        page_artifacts = list(page_response.get("artifacts", [])) if isinstance(page_response, dict) else []
        if not page_artifacts:
            break
        artifacts.extend(page_artifacts)
        page += 1
    return artifacts


def verify_run_record_against_live_api(
    record: dict,
    expected_head_sha: str,
    repo: str,
    api_call: Callable[[str], Any] = _default_gh_api_call,
) -> list[str]:
    """#2159 P0-3: cross-checks a single run record's claimed
    `workflow_run_id` / `artifact_id` / `artifact_digest` / `head_sha`
    against a LIVE GitHub Actions artifact-listing API response (the
    `GET /repos/{owner}/{repo}/actions/runs/{run_id}/artifacts` shape:
    each artifact carries its own `id`, `digest` (`sha256:<hex>`, matching
    this module's `_DIGEST_RE`), and `workflow_run.{id,head_sha}`), now
    fully paginated (#2179 AC8, see `_fetch_all_artifacts_paginated`).
    Returns a list of violation strings (empty list == the record is
    LIVE-API-verified, not merely shape-verified). A fabricated record
    (real-looking but never-uploaded artifact ID, or an artifact ID that
    belongs to a DIFFERENT workflow run / head SHA than claimed) is
    rejected here even though `_verify_run_record` alone would have
    accepted it.

    #2179 AC5/P0-3 (OWNER item 3): if `record` carries an EXPLICIT
    `run_attempt`, it is additionally cross-checked against the live
    attempt-specific jobs API (`GET .../actions/runs/{id}/attempts/
    {attempt}/jobs`) -- a claimed `run_attempt` that does not correspond
    to any real job for that run/attempt is rejected (trusted binding,
    never merely shape-validated). Records that omit `run_attempt`
    (pre-#2179 records, implicitly attempt 1) are NOT extended with this
    additional live call, preserving this function's pre-#2179 behavior
    and call signature for those callers."""
    violations: list[str] = []
    workflow_run_id = record.get("workflow_run_id")
    artifact_id = record.get("artifact_id")

    try:
        artifacts = _fetch_all_artifacts_paginated(workflow_run_id, repo, api_call)
    except LiveAPIError as exc:
        violations.append(f"live_api_artifacts_fetch_failed: {exc}")
        return violations

    matching = [a for a in artifacts if isinstance(a, dict) and a.get("id") == artifact_id]
    if not matching:
        violations.append(
            f"artifact_not_found_via_live_api: artifact_id={artifact_id!r} "
            f"workflow_run_id={workflow_run_id!r}"
        )
        return violations

    api_artifact = matching[0]

    if api_artifact.get("digest") != record.get("artifact_digest"):
        violations.append(
            f"artifact_digest_mismatch_vs_live_api: claimed={record.get('artifact_digest')!r} "
            f"api={api_artifact.get('digest')!r}"
        )

    api_workflow_run = api_artifact.get("workflow_run")
    api_workflow_run = api_workflow_run if isinstance(api_workflow_run, dict) else {}

    if api_workflow_run.get("id") != workflow_run_id:
        violations.append(
            f"artifact_workflow_run_id_mismatch_vs_live_api: claimed={workflow_run_id!r} "
            f"api={api_workflow_run.get('id')!r}"
        )
    if api_workflow_run.get("head_sha") != expected_head_sha:
        violations.append(
            f"artifact_head_sha_mismatch_vs_live_api: expected={expected_head_sha!r} "
            f"api={api_workflow_run.get('head_sha')!r}"
        )
    if record.get("head_sha") != api_workflow_run.get("head_sha"):
        violations.append(
            "record_claimed_head_sha_mismatches_live_api_head_sha: "
            f"record={record.get('head_sha')!r} api={api_workflow_run.get('head_sha')!r}"
        )

    if "run_attempt" in record and record.get("run_attempt") is not None:
        run_attempt = record.get("run_attempt")
        try:
            attempt_jobs_response = api_call(
                f"repos/{repo}/actions/runs/{workflow_run_id}/attempts/{run_attempt}/jobs"
            )
        except LiveAPIError as exc:
            violations.append(f"live_api_run_attempt_jobs_fetch_failed: {exc}")
        else:
            attempt_jobs = (
                attempt_jobs_response.get("jobs", []) if isinstance(attempt_jobs_response, dict) else []
            )
            matching_jobs = [
                j for j in attempt_jobs if isinstance(j, dict) and j.get("run_attempt") == run_attempt
            ]
            if not matching_jobs:
                violations.append(
                    f"run_attempt_not_found_via_live_api: workflow_run_id={workflow_run_id!r} "
                    f"run_attempt={run_attempt!r}"
                )

    return violations


def verify_records_against_live_api(
    records: list[dict],
    expected_head_sha: str,
    repo: str,
    api_call: Callable[[str], Any] = _default_gh_api_call,
) -> list[dict]:
    """#2159 P0-3: batch wrapper -- returns a list of
    `{"record": <record>, "violations": [...]}` entries for every record
    that FAILS live-API verification (empty list == every record is
    trusted). Intended as an explicit pre-filtering pass a CI job or
    operator runs BEFORE handing `--before-runs-json`/`--after-runs-json`
    to this script's hermetic `collect_benchmark_manifest`."""
    failures: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        violations = verify_run_record_against_live_api(record, expected_head_sha, repo, api_call=api_call)
        if violations:
            failures.append({"record": record, "violations": violations})
    return failures


def _dedupe_by_workflow_run_id(records: list[dict]) -> list[dict]:
    """#2179 AC1/AC3 (supersedes the #2159 AC2/P1-1 first-seen-wins
    version): sample identity is `workflow_run_id`, and selection follows
    the explicit `initial_attempt_only_v1` policy (see
    `_select_initial_attempt_records`) -- never `dict.setdefault()`
    first-seen-wins insertion-order semantics. Returns records sorted by
    `workflow_run_id` (canonical order, #2179 AC7) -- order-independent
    regardless of the input list's order."""
    selected = _select_initial_attempt_records(records)
    return [selected[workflow_run_id] for workflow_run_id in sorted(selected)]


def _collect_arm(
    arm_name: str,
    commit_sha: str,
    raw_records: list[dict],
    job_names: tuple[str, ...],
    min_run_count: int,
    evidence_errors: list[dict],
) -> dict:
    # #2179 P0-4: attempt-1-only selection MUST happen before any
    # conclusion-based filtering -- filtering non-success records out
    # first (the #2159 order) would silently let a later successful
    # attempt stand in for a failed/missing attempt 1, which is exactly
    # the non-deterministic substitution this policy forbids.
    verified_by_job: dict[str, list[dict]] = {job: [] for job in job_names}

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
        if job not in verified_by_job:
            # Not one of the jobs this manifest tracks -- not an error,
            # simply out of scope for this benchmark's job set.
            continue
        verified_by_job[job].append(record)

    jobs: dict[str, dict] = {}
    complete = True
    for job in job_names:
        job_records = verified_by_job[job]

        # #2179 AC9: genuine (workflow_run_id, job, run_attempt) identity
        # collisions are excluded and reported -- never silently
        # tie-broken like the ordinary "no run_attempt specified" case.
        collisions = _detect_run_attempt_identity_collisions(job_records)
        for workflow_run_id in sorted(collisions):
            group = collisions[workflow_run_id]
            evidence_errors.append(
                {
                    "arm": arm_name,
                    "reason": "run_attempt_identity_collision",
                    "detail": (
                        f"workflow_run_id={workflow_run_id!r} job={job!r} "
                        f"conflicting_identities="
                        f"{sorted({(r.get('artifact_id'), r.get('artifact_digest')) for r in group}, key=str)!r}"
                    ),
                }
            )

        non_colliding_records = [r for r in job_records if r.get("workflow_run_id") not in collisions]
        selected = _select_initial_attempt_records(non_colliding_records)

        usable: list[dict] = []
        for workflow_run_id in sorted(selected):
            record = selected[workflow_run_id]
            if record["conclusion"] not in ELIGIBLE_SAMPLE_CONCLUSIONS:
                # #2159 P0-4: a structurally-verified but non-successful run
                # is NOT a valid performance sample -- exclude it from the
                # cohort and surface it as an explicit evidence error rather
                # than silently counting it (or silently dropping it with
                # no trace). This is evaluated on the ALREADY-SELECTED
                # attempt-1 record only (#2179 P0-4).
                evidence_errors.append(
                    {
                        "arm": arm_name,
                        "reason": "non_successful_conclusion_excluded_from_sample",
                        "detail": (
                            f"workflow_run_id={record.get('workflow_run_id')!r} "
                            f"job={job!r} conclusion={record['conclusion']!r}"
                        ),
                    }
                )
                continue
            usable.append(record)

        # #2179 AC1/AC9: `workflow_run_id`s that had verified records but
        # NO eligible run_attempt==1 candidate (attempt 1 missing/malformed)
        # -- never silently dropped with no trace, and never substituted
        # with a later attempt.
        verified_ids = {r.get("workflow_run_id") for r in job_records if r.get("workflow_run_id") is not None}
        excluded_ids = verified_ids - set(collisions) - set(selected)
        for workflow_run_id in sorted(excluded_ids):
            evidence_errors.append(
                {
                    "arm": arm_name,
                    "reason": "missing_or_invalid_initial_attempt_excluded_from_sample",
                    "detail": f"workflow_run_id={workflow_run_id!r} job={job!r}",
                }
            )

        run_count = len(usable)
        if run_count < min_run_count:
            complete = False
        jobs[job] = {
            "job": job,
            "run_count": run_count,
            "sample_workflow_run_ids": sorted(r["workflow_run_id"] for r in usable),
            "runs": [
                {
                    "workflow_run_id": r["workflow_run_id"],
                    "job": r["job"],
                    "head_sha": r["head_sha"],
                    "artifact_id": r["artifact_id"],
                    "artifact_digest": r["artifact_digest"],
                    "conclusion": r["conclusion"],
                    "workflow_digest": r["workflow_digest"],
                    "workflow_sha": r["workflow_sha"],
                    # #2179 AC4: the SELECTED attempt (always 1 under this
                    # policy) and the policy identifier are recorded on
                    # every run entry, never inferred by a manifest
                    # consumer.
                    "run_attempt": _normalize_run_attempt(r) or 1,
                    "rerun_attempt_selection_policy": RERUN_ATTEMPT_SELECTION_POLICY,
                }
                for r in sorted(usable, key=lambda rec: rec["workflow_run_id"])
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
    job_names: tuple[str, ...] | None = None,
    before_job_names: tuple[str, ...] | None = None,
    after_job_names: tuple[str, ...] | None = None,
    min_run_count: int = DEFAULT_MIN_RUN_COUNT,
    generated_at: str | None = None,
) -> dict:
    """#2159 P0-4: `before_job_names`/`after_job_names` let the caller
    express the (intentionally asymmetric) pre-split vs post-split job
    topology. `job_names` (legacy, applies identically to both arms) is
    still accepted for callers that explicitly want a symmetric topology
    (e.g. same-schema unit fixtures); when neither `before_job_names` nor
    `after_job_names` is given, `job_names` defaults to `DEFAULT_JOB_NAMES`
    (the historical symmetric default). When `before_job_names`/
    `after_job_names` ARE given (or `job_names` is omitted entirely), the
    arm-specific `DEFAULT_BEFORE_JOB_NAMES`/`DEFAULT_AFTER_JOB_NAMES`
    topology is used."""
    if not _is_valid_sha(before_sha):
        raise OperationalError(f"invalid_before_sha: {before_sha!r}")
    if not _is_valid_sha(after_sha):
        raise OperationalError(f"invalid_after_sha: {after_sha!r}")

    if job_names is not None and before_job_names is None and after_job_names is None:
        resolved_before_job_names = job_names
        resolved_after_job_names = job_names
    else:
        resolved_before_job_names = before_job_names or DEFAULT_BEFORE_JOB_NAMES
        resolved_after_job_names = after_job_names or DEFAULT_AFTER_JOB_NAMES

    evidence_errors: list[dict] = []
    before_arm = _collect_arm(
        "before", before_sha, before_records, resolved_before_job_names, min_run_count, evidence_errors
    )
    before_arm["topology"] = "pre_split"
    after_arm = _collect_arm(
        "after", after_sha, after_records, resolved_after_job_names, min_run_count, evidence_errors
    )
    after_arm["topology"] = "post_split"

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


# --------------------------------------------------------------------------- #
# #2159 P0-9 (fix_delta after adversarial review issuecomment-5295659213):
# JSON Schema alone cannot express cross-field invariants (root SHA vs arm
# commit_sha, run_count vs len(runs), complete-but-empty, complete-but-
# has-evidence-errors, job-map-key vs internal job mismatch, pair-set
# mismatch between e2e-core/e2e-responsive-matrix). This semantic validator
# is run from BOTH the producer (`main()` below, fail-closed at collection
# time) and is importable for a consumer to re-run independently.
# --------------------------------------------------------------------------- #
def validate_manifest_semantics(manifest: dict) -> list[str]:
    """Returns a list of semantic-invariant violation strings (empty list
    == manifest is semantically consistent). Structural (JSON Schema)
    validity is a PREREQUISITE, not a substitute, for this check."""
    violations: list[str] = []

    for arm_name in ("before", "after"):
        arm = manifest.get("arms", {}).get(arm_name)
        if not isinstance(arm, dict):
            violations.append(f"missing_arm: {arm_name}")
            continue

        root_sha_key = f"{arm_name}_sha"
        root_sha = manifest.get(root_sha_key)
        if root_sha is not None and arm.get("commit_sha") != root_sha:
            violations.append(
                f"commit_sha_mismatches_root_{root_sha_key}: {arm_name} "
                f"(root={root_sha!r} arm={arm.get('commit_sha')!r})"
            )

        jobs = arm.get("jobs", {})
        if not isinstance(jobs, dict):
            violations.append(f"jobs_not_object: {arm_name}")
            continue

        topology = arm.get("topology")
        expected_jobs = None
        if topology == "pre_split":
            expected_jobs = set(DEFAULT_BEFORE_JOB_NAMES)
        elif topology == "post_split":
            expected_jobs = set(DEFAULT_AFTER_JOB_NAMES)
        if expected_jobs is not None and set(jobs.keys()) != expected_jobs:
            violations.append(
                f"job_topology_mismatch: {arm_name} topology={topology!r} "
                f"expected={sorted(expected_jobs)} actual={sorted(jobs.keys())}"
            )

        pair_sample_sets: dict[str, set] = {}
        for job_key, cohort in jobs.items():
            if not isinstance(cohort, dict):
                violations.append(f"job_cohort_not_object: {arm_name}/{job_key}")
                continue
            if cohort.get("job") != job_key:
                violations.append(
                    f"job_map_key_mismatches_internal_job: {arm_name}/{job_key} "
                    f"(internal={cohort.get('job')!r})"
                )
            runs = cohort.get("runs", [])
            run_count = cohort.get("run_count")
            if run_count is not None and run_count != len(runs):
                violations.append(
                    f"run_count_mismatches_len_runs: {arm_name}/{job_key} "
                    f"(run_count={run_count} len(runs)={len(runs)})"
                )
            run_ids_from_runs = {r.get("workflow_run_id") for r in runs if isinstance(r, dict)}
            sample_ids = set(cohort.get("sample_workflow_run_ids", []))
            if sample_ids != run_ids_from_runs:
                violations.append(
                    f"sample_workflow_run_ids_mismatches_runs: {arm_name}/{job_key}"
                )
            if job_key in ("e2e-core", "e2e-responsive-matrix"):
                pair_sample_sets[job_key] = run_ids_from_runs

            if arm.get("complete") is True and run_count == 0:
                violations.append(f"complete_true_with_zero_runs: {arm_name}/{job_key}")

        if arm.get("complete") is True:
            arm_evidence_errors = [
                err for err in manifest.get("evidence_errors", []) if err.get("arm") == arm_name
            ]
            if arm_evidence_errors:
                violations.append(f"complete_true_with_evidence_errors: {arm_name}")

        if {"e2e-core", "e2e-responsive-matrix"} <= pair_sample_sets.keys():
            core_ids = pair_sample_sets["e2e-core"]
            responsive_ids = pair_sample_sets["e2e-responsive-matrix"]
            if core_ids != responsive_ids:
                violations.append(f"pair_set_mismatch_e2e_core_e2e_responsive_matrix: {arm_name}")

    return violations


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
    parser.add_argument(
        "--before-job-names",
        default=",".join(DEFAULT_BEFORE_JOB_NAMES),
        help=(
            "Comma-separated pre-split job names required for the before arm "
            f"(#2159 P0-4; default {','.join(DEFAULT_BEFORE_JOB_NAMES)!r})"
        ),
    )
    parser.add_argument(
        "--after-job-names",
        default=",".join(DEFAULT_AFTER_JOB_NAMES),
        help=(
            "Comma-separated post-split job names required for the after arm "
            f"(#2159 P0-4; default {','.join(DEFAULT_AFTER_JOB_NAMES)!r})"
        ),
    )
    parser.add_argument(
        "--verify-against-live-api",
        action="store_true",
        help=(
            "#2159 P0-3: before collecting, verify every before/after record "
            "against a LIVE GitHub Actions artifact-listing API call (requires "
            "--repo and an authenticated `gh` CLI). Records that fail live "
            "verification are treated exactly like structural verification "
            "failures (excluded from the cohort, reported in evidence_errors)."
        ),
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="owner/repo for --verify-against-live-api (e.g. squne121/loop-protocol)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        before_records = _load_json_file(args.before_runs_json)
        after_records = _load_json_file(args.after_runs_json)
        if not isinstance(before_records, list) or not isinstance(after_records, list):
            raise OperationalError("before/after runs JSON must each be a JSON array")

        live_verification_evidence_errors: list[dict] = []
        if args.verify_against_live_api:
            if not args.repo:
                raise OperationalError("--verify-against-live-api requires --repo")
            for arm_name, records, expected_head_sha in (
                ("before", before_records, args.before_sha),
                ("after", after_records, args.after_sha),
            ):
                failures = verify_records_against_live_api(records, expected_head_sha, args.repo)
                failed_record_ids = {id(f["record"]) for f in failures}
                for failure in failures:
                    live_verification_evidence_errors.append(
                        {
                            "arm": arm_name,
                            "reason": "live_api_verification_failed",
                            "detail": (
                                f"workflow_run_id={failure['record'].get('workflow_run_id')!r} "
                                f"violations={failure['violations']}"
                            ),
                        }
                    )
                if arm_name == "before":
                    before_records = [r for r in records if id(r) not in failed_record_ids]
                else:
                    after_records = [r for r in records if id(r) not in failed_record_ids]

        manifest = collect_benchmark_manifest(
            args.before_sha,
            args.after_sha,
            before_records,
            after_records,
            before_job_names=tuple(args.before_job_names.split(",")),
            after_job_names=tuple(args.after_job_names.split(",")),
            min_run_count=args.min_runs,
        )
        # #2159 P0-3: live-API-rejected records are surfaced as explicit
        # evidence_errors (never a silent drop), consistent with the
        # structural-verification failure path above.
        manifest["evidence_errors"].extend(live_verification_evidence_errors)
        _validate_against_schema(manifest)
        semantic_violations = validate_manifest_semantics(manifest)
        if semantic_violations:
            raise OperationalError(f"manifest_failed_semantic_validation: {semantic_violations}")
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
