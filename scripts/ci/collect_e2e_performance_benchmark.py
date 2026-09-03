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

#2184 AC1/AC2: two further OPTIONAL fields, when present, are recognized
and propagated to the output manifest's `RunRecord` (never inferred when
absent -- a record without them is a pre-#2184 legacy shape, see
`_verify_run_record` / `_optional_head_sha_provenance_fields`):

    {
      "measured_head_sha": "<40-hex commit sha the collecting step itself
        observed via an independent `git rev-parse HEAD`; DISTINCT from
        `head_sha` above (which resolves to `github.sha` -- the
        workflow_dispatch dispatch ref's tip -- not `target_sha`, on a
        fixed-SHA benchmark dispatch) and NEVER a direct copy of the
        `target_sha` dispatch input>",
      "merge_sha": "<the SAME 40-hex value the raw ci_runtime_baseline_v1
        artifact's `merge_sha` field already carries (`GH_SHA: ${{
        github.sha }}`, unchanged by this Issue); this collector derives
        `workflow_run_head_sha` from it (see
        `_derive_workflow_run_head_sha`) -- a caller MAY instead supply an
        already-derived `workflow_run_head_sha` field directly, which then
        takes precedence>"
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


def _classify_run_attempt(record: dict) -> tuple[int | None, str]:
    """#2182 P0-3 (OWNER adversarial review REQUEST_CHANGES, fix_delta
    after PR #2182 issuecomment-5302446086): classifies
    `record["run_attempt"]` into one of three DISTINCT states, replacing
    the pre-fix_delta `_normalize_run_attempt` that conflated "the key is
    entirely absent" with "this is a verified attempt 1" (provenance
    laundering -- a legacy record with no `run_attempt` field at all was
    silently promoted to `run_attempt: 1` and emitted into the manifest
    output indistinguishable from a genuinely live-API-verified attempt-1
    record). Returns `(value, status)`:

      status == "ok"      -- `run_attempt` is EXPLICITLY present and a
                              well-formed `int >= 1`. `value` is that int.
      status == "missing" -- the `run_attempt` key is entirely ABSENT
                              (a pre-#2179 record). `value` is `None`.
                              Readable for backward-compat SCHEMA parsing,
                              but NEVER eligible for the trusted/selected
                              cohort (see `_select_initial_attempt_records`).
      status == "invalid" -- the key IS present but malformed (explicit
                              `None`, a non-int such as the string `"2"`,
                              a bool, `0`, or a negative value). `value`
                              is `None`."""
    if "run_attempt" not in record:
        return None, "missing"
    value = record.get("run_attempt")
    if value is None or isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None, "invalid"
    return value, "ok"


def _normalize_run_attempt(record: dict) -> int | None:
    """#2179 AC9 (narrowed by #2182 fix_delta P0-3): thin wrapper over
    `_classify_run_attempt` that returns the normalized int ONLY when
    `run_attempt` is explicitly present and well-formed (status "ok").
    Unlike the pre-fix_delta version, a MISSING `run_attempt` key no
    longer defaults to 1 here -- callers that need trusted-cohort
    eligibility must treat "missing" exactly like "invalid" (both return
    `None` from this function; use `_classify_run_attempt` directly if the
    "missing" vs "invalid" distinction itself matters, e.g. for choosing
    an `evidence_errors` reason string)."""
    value, status = _classify_run_attempt(record)
    return value if status == "ok" else None


def _group_by_workflow_run_id(records: list[dict]) -> dict[int, list[dict]]:
    by_id: dict[int, list[dict]] = {}
    for record in records:
        workflow_run_id = record.get("workflow_run_id")
        if workflow_run_id is None:
            continue
        by_id.setdefault(workflow_run_id, []).append(record)
    return by_id


def _select_initial_attempt_records(records: list[dict]) -> dict[int, dict]:
    """#2179 AC1/AC3 (narrowed by #2182 fix_delta P0-3): pure
    `initial_attempt_only_v1` selection -- groups `records` by
    `workflow_run_id` first (never relying on insertion order), then keeps
    only the EXPLICIT `run_attempt == 1` candidate per id (status "ok"
    from `_classify_run_attempt`; a record with a MISSING `run_attempt`
    key is never eligible here -- see `_classify_run_attempt`'s "missing"
    status, #2182 P0-3). If more than one record qualifies as attempt 1
    for the same `workflow_run_id` and they are byte-for-byte identical
    (an idempotent duplicate), a deterministic tie-break is used; genuine
    content DISAGREEMENT among same-identity candidates is a collision
    handled upstream by `_detect_run_attempt_identity_collisions` (#2182
    P1) and never reaches this function (callers filter collisions out
    first)."""
    selected: dict[int, dict] = {}
    for workflow_run_id, group in _group_by_workflow_run_id(records).items():
        candidates = [r for r in group if _normalize_run_attempt(r) == 1]
        if not candidates:
            continue
        selected[workflow_run_id] = min(candidates, key=lambda r: json.dumps(r, sort_keys=True, default=str))
    return selected


def _identity_normalized_json(record: dict) -> str:
    """#2182 P1: canonical (sorted-keys) JSON view of `record`, used for
    byte-for-byte identity comparison -- two records compare equal under
    this function iff EVERY field matches (never merely artifact_id/
    artifact_digest, the pre-fix_delta narrower check)."""
    return json.dumps(record, sort_keys=True, default=str)


def _effective_attempt_for_collision_grouping(record: dict) -> int | None:
    """#2182 P1: groups a record into its `(workflow_run_id, job,
    run_attempt)` collision-detection identity slot -- `job` is already
    fixed by the caller (this function is always invoked on a
    single-job's record pool, see `_detect_run_attempt_identity_collisions`
    below and its caller in `_collect_arm`). A MISSING `run_attempt` key
    is grouped into the attempt-1 slot for COLLISION DETECTION purposes
    ONLY (it remains separately excluded from the trusted cohort via
    `_classify_run_attempt`'s "missing" status, #2182 P0-3) -- this is
    what makes a legacy no-`run_attempt`-field record collide with an
    EXPLICIT `run_attempt: 1` record claiming the same `workflow_run_id`:
    both claim the same identity slot, and if their content disagrees that
    is a genuine collision, not two independent samples. An explicitly
    INVALID `run_attempt` (malformed/zero/negative/non-int) never
    participates in collision grouping -- it is already unconditionally
    excluded from the trusted cohort with its own `evidence_errors`
    reason, and grouping it here would only produce redundant noise."""
    value, status = _classify_run_attempt(record)
    if status == "ok":
        return value
    if status == "missing":
        return 1
    return None


def _detect_run_attempt_identity_collisions(records: list[dict]) -> dict[int, list[dict]]:
    """#2182 P1 (fix_delta after OWNER adversarial review of PR #2182,
    issuecomment-5302446086): identity is fixed to `(workflow_run_id, job,
    run_attempt)` -- `job` is already fixed by the caller (one job's
    record pool). Records sharing that identity slot are treated as an
    idempotent, harmless duplicate ONLY if the ENTIRE normalized record is
    byte-for-byte identical (`_identity_normalized_json`); if even a
    SINGLE field differs (`head_sha` / `conclusion` / `artifact_id` /
    `artifact_digest` / `workflow_digest` / `workflow_sha` / any other
    field), the WHOLE `workflow_run_id` sample is excluded as a
    fail-closed collision -- this supersedes the pre-fix_delta version,
    which only compared `(artifact_id, artifact_digest)` and silently
    accepted a canonical-JSON `min()` tie-break for any OTHER field
    disagreement (the exact defect the OWNER review flagged: two records
    sharing a `workflow_run_id`/attempt but disagreeing on `head_sha`,
    `conclusion`, `workflow_digest`, `workflow_sha`, or measurement/
    fingerprint/policy fields would silently pick one via `min()` instead
    of being detected as a genuine content conflict)."""
    by_key: dict[tuple, list[dict]] = {}
    for record in records:
        workflow_run_id = record.get("workflow_run_id")
        if workflow_run_id is None:
            continue
        effective_attempt = _effective_attempt_for_collision_grouping(record)
        if effective_attempt != 1:
            continue
        by_key.setdefault((workflow_run_id, effective_attempt), []).append(record)

    collisions: dict[int, list[dict]] = {}
    for (workflow_run_id, _run_attempt), group in by_key.items():
        if len(group) < 2:
            continue
        normalized = {_identity_normalized_json(r) for r in group}
        if len(normalized) > 1:
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


def _derive_workflow_run_head_sha(record: dict) -> object:
    """#2184 AC2: derives `workflow_run_head_sha` from the raw
    `ci_runtime_baseline_v1` artifact's EXISTING `merge_sha` field
    (`.github/workflows/ci.yml`'s `GH_SHA: ${{ github.sha }}`, unchanged
    by this Issue) -- no new producer env var or `gh api` call is
    introduced (AC2). An explicit `workflow_run_head_sha` key on
    `record`, if a caller already computed one, takes precedence over
    deriving it from `merge_sha`. Returns `None` when neither is present
    (a pre-#2159 record shape that never carried `merge_sha` at all)."""
    explicit = record.get("workflow_run_head_sha")
    if explicit is not None:
        return explicit
    return record.get("merge_sha")


def _optional_head_sha_provenance_fields(record: dict) -> dict:
    """#2184 AC4(ii): builds the optional `measured_head_sha` /
    `workflow_run_head_sha` key/value pairs for a trusted record's final
    manifest `RunRecord` entry -- `measured_head_sha` is propagated
    verbatim (only a trusted record, already validated by
    `_verify_run_record`, ever reaches this helper); `workflow_run_head_sha`
    is derived via `_derive_workflow_run_head_sha` (#2184 AC2). A key is
    OMITTED entirely (never set to a synthesized/inferred value) when its
    source value is absent -- this is what keeps a legacy record's output
    RunRecord free of these two fields (schema `RunRecord.required` never
    lists them, #2184 AC3)."""
    fields: dict = {}
    measured_head_sha = record.get("measured_head_sha")
    if measured_head_sha is not None:
        fields["measured_head_sha"] = measured_head_sha
    workflow_run_head_sha = _derive_workflow_run_head_sha(record)
    if workflow_run_head_sha is not None:
        fields["workflow_run_head_sha"] = workflow_run_head_sha
    return fields


def _verify_run_record(record: dict, expected_head_sha: str) -> list[str]:
    """#2159 AC7: verifies artifact ID / artifact digest / head SHA / job
    are all present and well-formed for a single run record. Returns a
    list of violation reason strings (empty list == record is usable).

    #2184 AC1/AC4(i): `head_sha` and `measured_head_sha` are independent
    concepts -- `head_sha` resolves to `github.sha` (the workflow_dispatch
    dispatch ref's tip), NOT `target_sha`, on a fixed-SHA benchmark
    dispatch, so it is expected to differ from `expected_head_sha`
    (target_sha) for a genuine new-producer record. A record that carries
    `measured_head_sha` is therefore verified against `expected_head_sha`
    via `measured_head_sha` (a new, independent structural-binding check),
    NEVER via `head_sha`. A record WITHOUT `measured_head_sha` is a
    pre-#2184 legacy-shaped record: per AC4(i) ("既存の head_sha !=
    expected_head_sha 検証は変更せず、measured_head_sha を持たない legacy
    record にのみ引き続き適用する"), it continues to be verified via the
    UNCHANGED pre-#2184 `head_sha != expected_head_sha` check below --
    this additive-only design is a deliberate compatibility decision: a
    hard, unconditional trusted-cohort exclusion of every
    `measured_head_sha`-less record (the Outcome section's
    `legacy_ambiguous_head_sha` illustration) is intentionally NOT wired
    into this shared structural gate, because doing so would regress
    `scripts/ci/tests/e2e_performance_benchmark/test_collect_e2e_performance_benchmark_rerun_attempt.py`
    (#2179/#2182's rerun-attempt regression suite, entirely
    `measured_head_sha`-less by construction and outside Issue #2184's
    Allowed Paths) -- see this Issue's PR body for the full rationale."""
    violations: list[str] = []
    if not isinstance(record.get("workflow_run_id"), int) or record.get("workflow_run_id", 0) < 1:
        violations.append("missing_or_invalid_workflow_run_id")
    if not isinstance(record.get("job"), str) or not record.get("job"):
        violations.append("missing_or_invalid_job")
    has_measured_head_sha = record.get("measured_head_sha") is not None
    if not _is_valid_sha(record.get("head_sha")):
        violations.append("missing_or_invalid_head_sha")
    elif not has_measured_head_sha and record.get("head_sha") != expected_head_sha:
        violations.append("head_sha_mismatch")
    if has_measured_head_sha:
        if not _is_valid_sha(record.get("measured_head_sha")):
            violations.append("missing_or_invalid_measured_head_sha")
        elif record.get("measured_head_sha") != expected_head_sha:
            violations.append("measured_head_sha_mismatch")
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
    """#2179 AC8 (hardened by #2182 fix_delta P0-1, OWNER adversarial
    review issuecomment-5302446086): GitHub's artifact-listing API
    paginates (`total_count` + an `artifacts` array truncated to
    `per_page`). GitHub's page NUMBERING is relative to page SIZE -- the
    pre-fix_delta version issued the FIRST request WITHOUT an explicit
    `per_page` (GitHub defaults to `per_page=30` in that case) and only
    page 2+ requests used `per_page=100`. Switching page size from 30
    (implicit) to 100 mid-pagination means "page 2" under the two
    DIFFERENT page sizes refers to two DIFFERENT slices of the underlying
    artifact list -- artifacts 31-100 would be silently SKIPPED (page 1
    only returned items 1-30, "page 2 at per_page=100" starts at item
    101, not item 31). This version uses the SAME explicit
    `per_page=DEFAULT_ARTIFACTS_PER_PAGE` on EVERY request from page 1
    onward, and fails closed (raises `LiveAPIError`, never silently
    under-counts) on:
      - an empty page reached while `len(collected) < total_count`
        (a genuine gap -- GitHub would only ever return an empty page at
        or past the true end of the list for a STABLE total_count);
      - a duplicate artifact `id` appearing across pages (a paging
        consistency violation -- an artifact was double-counted, which
        would corrupt any per-page-boundary off-by-one detection);
      - `total_count` CHANGING between pages of the SAME fetch (the
        underlying artifact set mutated mid-fetch -- e.g. a concurrent
        upload/deletion -- and the already-collected pages are no longer
        a consistent snapshot)."""
    base_endpoint = f"repos/{repo}/actions/runs/{workflow_run_id}/artifacts"
    artifacts: list[dict] = []
    seen_artifact_ids: set = set()
    total_count: int | None = None
    page = 1

    while True:
        page_response = api_call(f"{base_endpoint}?per_page={DEFAULT_ARTIFACTS_PER_PAGE}&page={page}")
        page_total_count = page_response.get("total_count") if isinstance(page_response, dict) else None

        if total_count is None:
            total_count = page_total_count
        elif page_total_count != total_count:
            raise LiveAPIError(
                f"artifact_pagination_total_count_changed_mid_fetch: "
                f"workflow_run_id={workflow_run_id!r} page={page} "
                f"initial_total_count={total_count!r} page_total_count={page_total_count!r}"
            )

        page_artifacts = list(page_response.get("artifacts", [])) if isinstance(page_response, dict) else []

        if not isinstance(total_count, int):
            # No total_count reported at all (a minimal/malformed
            # response) -- treat this single page as the complete result,
            # matching the pre-#2182 single-page fallback behavior.
            artifacts.extend(page_artifacts)
            break

        if not page_artifacts:
            if len(artifacts) < total_count:
                raise LiveAPIError(
                    f"artifact_pagination_empty_page_before_total_count_reached: "
                    f"workflow_run_id={workflow_run_id!r} page={page} "
                    f"collected={len(artifacts)} total_count={total_count!r}"
                )
            break

        for artifact in page_artifacts:
            artifact_id = artifact.get("id") if isinstance(artifact, dict) else None
            if artifact_id is not None:
                if artifact_id in seen_artifact_ids:
                    raise LiveAPIError(
                        f"artifact_pagination_duplicate_artifact_id: "
                        f"workflow_run_id={workflow_run_id!r} page={page} artifact_id={artifact_id!r}"
                    )
                seen_artifact_ids.add(artifact_id)

        artifacts.extend(page_artifacts)
        if len(artifacts) >= total_count:
            break
        page += 1

    return artifacts


ARTIFACT_NAME_TEMPLATE = "ci-runtime-baseline-{job}-{run_attempt}"


def _expected_artifact_name(job: object, run_attempt: int) -> str | None:
    if not isinstance(job, str) or not job:
        return None
    return ARTIFACT_NAME_TEMPLATE.format(job=job, run_attempt=run_attempt)


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
    each artifact carries its own `id`, `name`, `digest` (`sha256:<hex>`,
    matching this module's `_DIGEST_RE`), and `workflow_run.{id,head_sha}`),
    now fully paginated (#2179 AC8, see `_fetch_all_artifacts_paginated`,
    hardened #2182 P0-1). Returns a list of violation strings (empty list
    == the record is LIVE-API-verified, not merely shape-verified). A
    fabricated record (real-looking but never-uploaded artifact ID, or an
    artifact ID that belongs to a DIFFERENT workflow run / head SHA than
    claimed) is rejected here even though `_verify_run_record` alone would
    have accepted it.

    #2182 P0-2 (fix_delta after OWNER adversarial review of PR #2182,
    issuecomment-5302446086): the pre-fix_delta live-binding check only
    verified `job.run_attempt == claimed_run_attempt` against the
    attempt-specific jobs API response -- it never checked `job.name ==
    record["job"]`, the job's own `head_sha`, `job.conclusion`, or that
    the artifact NAME itself corresponds to the specific job+attempt. That
    let an attacker relabel an attempt-2 artifact as attempt-1 as long as
    SOME (any) job existed in attempt-1's job list. The trusted live-
    binding identity is now the FULL tuple `(workflow_run_id, run_attempt,
    job_name, workflow_run_head_sha, job_conclusion, artifact_id,
    artifact_name, artifact_digest)`: the artifact's own `name` must
    exactly equal `ci-runtime-baseline-{record.job}-{record.run_attempt}`
    (`_expected_artifact_name`, matching this repo's own
    `.github/workflows/ci.yml` `actions/upload-artifact` naming
    convention), AND the attempt-specific jobs API must return a job
    matching ALL of `run_attempt`, `name == record["job"]`, `head_sha ==
    expected_head_sha`, and `conclusion == "success"` -- never merely "any
    job exists in that attempt's job list".

    Only performed when `record` carries an EXPLICIT, well-formed
    (`_classify_run_attempt` status `"ok"`) `run_attempt` -- #2182 P0-3:
    a record with a MISSING `run_attempt` key is never trusted-cohort
    eligible in the first place (see `_select_initial_attempt_records`),
    so this function preserves its pre-#2179 behavior/call signature for
    those (unbound, always-excluded-from-selection) callers: only the
    pre-existing artifacts-listing call is made, no attempt-jobs call.

    #2184 AC4(i): the live API's OWN `head_sha` (`api_workflow_run.
    head_sha` / `api_job.head_sha`) is compared for SELF-CONSISTENCY
    against `record`'s own claimed `workflow_run_head_sha` (an explicit
    field, or derived from the existing `merge_sha` field -- #2184 AC2,
    `_derive_workflow_run_head_sha`) -- NEVER against `expected_head_sha`
    (the measured/target_sha commit): `measured_head_sha` and
    `workflow_run_head_sha` are independent concepts and are EXPECTED to
    differ on a fixed-SHA benchmark dispatch, so comparing the live API's
    head SHA to `expected_head_sha` would reject every genuine new-style
    record. When `workflow_run_head_sha` cannot be derived (a pre-#2159
    record lacking `merge_sha` entirely), this falls back to the
    pre-#2184 `expected_head_sha` comparison, unchanged, to preserve
    exact backward compatibility for such records."""
    violations: list[str] = []
    workflow_run_id = record.get("workflow_run_id")
    artifact_id = record.get("artifact_id")
    job_name = record.get("job")
    run_attempt, run_attempt_status = _classify_run_attempt(record)
    workflow_run_head_sha = _derive_workflow_run_head_sha(record)
    live_head_sha_comparison_target = (
        workflow_run_head_sha if workflow_run_head_sha is not None else expected_head_sha
    )

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

    expected_artifact_name = (
        _expected_artifact_name(job_name, run_attempt) if run_attempt_status == "ok" else None
    )
    if expected_artifact_name is not None and api_artifact.get("name") != expected_artifact_name:
        violations.append(
            f"artifact_name_mismatch_vs_job_run_attempt: expected={expected_artifact_name!r} "
            f"api={api_artifact.get('name')!r}"
        )

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
    if api_workflow_run.get("head_sha") != live_head_sha_comparison_target:
        violations.append(
            f"artifact_head_sha_mismatch_vs_live_api: expected={live_head_sha_comparison_target!r} "
            f"api={api_workflow_run.get('head_sha')!r}"
        )
    if record.get("head_sha") != api_workflow_run.get("head_sha"):
        violations.append(
            "record_claimed_head_sha_mismatches_live_api_head_sha: "
            f"record={record.get('head_sha')!r} api={api_workflow_run.get('head_sha')!r}"
        )

    if run_attempt_status == "ok":
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
                j
                for j in attempt_jobs
                if isinstance(j, dict) and j.get("run_attempt") == run_attempt and j.get("name") == job_name
            ]
            if not matching_jobs:
                violations.append(
                    f"run_attempt_not_found_via_live_api: workflow_run_id={workflow_run_id!r} "
                    f"run_attempt={run_attempt!r} job={job_name!r}"
                )
            else:
                api_job = matching_jobs[0]
                if api_job.get("head_sha") != live_head_sha_comparison_target:
                    violations.append(
                        f"run_attempt_job_head_sha_mismatch_vs_live_api: "
                        f"expected={live_head_sha_comparison_target!r} "
                        f"api={api_job.get('head_sha')!r}"
                    )
                if api_job.get("conclusion") != "success":
                    violations.append(
                        f"run_attempt_job_conclusion_not_success_via_live_api: "
                        f"conclusion={api_job.get('conclusion')!r}"
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
        #
        # #2182 P0-3 (fix_delta after OWNER adversarial review of PR
        # #2182, issuecomment-5302446086): a `workflow_run_id` excluded
        # because EVERY one of its records has an entirely MISSING
        # `run_attempt` key gets a DISTINCT `legacy_unverified_run_attempt`
        # reason -- this is provenance-laundering-prevention evidence
        # (this sample predates the `run_attempt` field and was NEVER
        # live-API-bound, unlike a genuine `run_attempt: 1` selection), not
        # merely "malformed data" (`missing_or_invalid_initial_attempt_
        # excluded_from_sample`, kept for the explicitly-invalid case:
        # `None`, non-int, `0`, negative).
        verified_ids = {r.get("workflow_run_id") for r in job_records if r.get("workflow_run_id") is not None}
        excluded_ids = verified_ids - set(collisions) - set(selected)
        for workflow_run_id in sorted(excluded_ids):
            group = [r for r in job_records if r.get("workflow_run_id") == workflow_run_id]
            statuses = {_classify_run_attempt(r)[1] for r in group}
            reason = (
                "legacy_unverified_run_attempt"
                if statuses == {"missing"}
                else "missing_or_invalid_initial_attempt_excluded_from_sample"
            )
            evidence_errors.append(
                {
                    "arm": arm_name,
                    "reason": reason,
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
                    # #2184 AC4(ii): propagate the trusted record's
                    # `measured_head_sha` verbatim, and `workflow_run_head_sha`
                    # derived from the raw artifact's existing `merge_sha`
                    # field (#2184 AC2) -- ONLY when derivable; never
                    # inferred/synthesized for a legacy record that lacks
                    # them (#2184 Outcome).
                    **_optional_head_sha_provenance_fields(r),
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
