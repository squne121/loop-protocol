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
        `workflow_run_head_sha` SOLELY from it (see
        `_derive_workflow_run_head_sha`) -- a caller-supplied
        `workflow_run_head_sha` field, if present on a record, is NEVER
        consulted and can never override/bypass this derivation (fix_delta
        after OWNER adversarial review of PR #2493,
        issuecomment-5540651128: honoring a caller-supplied override was a
        provenance-laundering path around `merge_sha`)>"
    }

#2184 AC4(iii): a record for job `e2e-core`/`e2e-responsive-matrix`
(`LEGACY_AMBIGUOUS_HEAD_SHA_JOBS`) carrying NEITHER `measured_head_sha`
nor a `workflow_run_head_sha`-derivable `merge_sha` is never promoted
into the trusted cohort by inference/synthesis -- `_collect_arm` excludes
it with the explicit `legacy_ambiguous_head_sha` evidence_errors reason.
Scoped to those two job names only (see `LEGACY_AMBIGUOUS_HEAD_SHA_JOBS`'s
docstring): the permanently-retired pre-split `e2e` job never carries
either field, past or future, and is unaffected.

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
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Callable

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SCHEMA_PATH = os.path.join(REPO_ROOT, "schemas", "e2e_performance_benchmark_manifest_v1.schema.json")

# Issue #2422: `e2e_performance_benchmark_manifest_v2` supersedes v1's
# `before_sha`/`after_sha`/`pre_split`/`post_split` hybrid semantics (a fixed
# historical commit compared against a mutable current workflow) for the
# dedicated `benchmark_layout=monolith|split` A/B/A/B dispatch route. v1 and
# its schema/functions above are left unmodified (other, unrelated consumers
# of this module's rerun-attempt/live-API-verification building blocks are
# out of this Issue's scope) -- v2 is purely additive, defined below.
SCHEMA_PATH_V2 = os.path.join(REPO_ROOT, "schemas", "e2e_performance_benchmark_manifest_v2.schema.json")
MANIFEST_SCHEMA_V2 = "e2e_performance_benchmark_manifest_v2"

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

# #2184 AC4(iii) (fix_delta after pr-reviewer REQUEST_CHANGES on PR #2493):
# jobs whose `ci_runtime_baseline_v1` producer step is the ONLY one this
# Issue's In Scope actually modifies to unconditionally emit
# `measured_head_sha` (`.github/workflows/ci.yml`'s "Collect
# ci_runtime_baseline_v1 artifact" step for `e2e-core` / `e2e-responsive-
# matrix`, #2184 AC1). The pre-#2137-split `e2e` job (`DEFAULT_BEFORE_
# JOB_NAMES` above) is a permanently-retired producer this Issue never
# touches -- it will NEVER emit `measured_head_sha`/carry a `merge_sha`-
# derived `workflow_run_head_sha` going forward, past or future, so
# labeling its records "ambiguous" would be a category error, not a
# genuine legacy-migration signal. The hard-exclude below (AC4(iii)) is
# therefore scoped to exactly this tuple, never applied job-name-
# agnostically to `_collect_arm`'s shared/hermetic selection pipeline.
LEGACY_AMBIGUOUS_HEAD_SHA_JOBS = ("e2e-core", "e2e-responsive-matrix")

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
    """#2184 AC2 (narrowed by fix_delta after OWNER adversarial review of
    PR #2493, issuecomment-5540651128): derives `workflow_run_head_sha`
    SOLELY from the raw `ci_runtime_baseline_v1` artifact's EXISTING
    `merge_sha` field (`.github/workflows/ci.yml`'s `GH_SHA: ${{
    github.sha }}`, unchanged by this Issue) -- no new producer env var or
    `gh api` call is introduced (AC2). A caller-supplied `workflow_run_head_
    sha` field on `record` is NEVER consulted here and can never override
    `merge_sha` -- honoring it would let a caller launder an artifact whose
    real `merge_sha` provenance disagrees with a self-declared value past
    the live-API self-consistency check in `verify_run_record_against_live_api`
    (#2184 AC2 requires `workflow_run_head_sha` be *derived from* the raw
    artifact's `merge_sha`, not independently suppliable). Returns `None`
    when `merge_sha` is absent (a pre-#2159 record shape that never carried
    `merge_sha` at all)."""
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
    this additive-only design is a deliberate compatibility decision.

    #2184 AC4(iii) (fix_delta after pr-reviewer REQUEST_CHANGES on PR
    #2493): the hard, unconditional trusted-cohort exclusion of a record
    carrying NEITHER `measured_head_sha` NOR a derivable
    `workflow_run_head_sha` (the Outcome section's `legacy_ambiguous_
    head_sha` illustration) is NOT performed here (this function stays
    pure shape/regex validation, unaware of job identity) -- it is
    applied by `_collect_arm`, scoped to `LEGACY_AMBIGUOUS_HEAD_SHA_JOBS`
    (`e2e-core`/`e2e-responsive-matrix`, the only jobs #2184's In Scope
    actually makes emit `measured_head_sha`). That job-name scoping is
    what keeps this additive-only design compatible with
    `scripts/ci/tests/e2e_performance_benchmark/test_collect_e2e_performance_benchmark_rerun_attempt.py`
    (#2179/#2182's rerun-attempt regression suite, outside Issue #2184's
    Allowed Paths): every one of its fixtures that reaches a trusted
    `usable` cohort with neither field present uses the generic `e2e`
    job name (the permanently-retired pre-split job,
    `DEFAULT_BEFORE_JOB_NAMES`, which #2184 never touches and which will
    never carry either field, past or future) -- never `e2e-core`/
    `e2e-responsive-matrix`, so it is unaffected by the job-scoped
    exclusion. See `_collect_arm` and this Issue's PR body for the full
    rationale."""
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
    head_sha`) is compared for SELF-CONSISTENCY against `record`'s
    `workflow_run_head_sha`, derived SOLELY from the raw artifact's
    existing `merge_sha` field (#2184 AC2, `_derive_workflow_run_head_sha`
    -- a caller-supplied `workflow_run_head_sha` field on `record` is
    never consulted, fix_delta after OWNER adversarial review of PR #2493,
    issuecomment-5540651128) -- NEVER against `expected_head_sha`
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
            # #2184 AC4(iii): a record for a `LEGACY_AMBIGUOUS_HEAD_SHA_JOBS`
            # job that carries NEITHER `measured_head_sha` NOR a derivable
            # `workflow_run_head_sha` (no `measured_head_sha` key AND no
            # `merge_sha`/`workflow_run_head_sha` to derive one from, #2184
            # AC2) is never promoted into the trusted cohort by inferring/
            # synthesizing either field -- it is excluded with the explicit
            # `legacy_ambiguous_head_sha` reason, mirroring
            # `legacy_unverified_run_attempt` above. Scoped to
            # `LEGACY_AMBIGUOUS_HEAD_SHA_JOBS` only (see that constant's
            # docstring): a record for any OTHER job (e.g. the
            # permanently-retired pre-split `e2e` job, `DEFAULT_BEFORE_
            # JOB_NAMES`) was never expected to carry these #2184 fields in
            # the first place and is unaffected by this exclusion.
            if (
                job in LEGACY_AMBIGUOUS_HEAD_SHA_JOBS
                and record.get("measured_head_sha") is None
                and _derive_workflow_run_head_sha(record) is None
            ):
                evidence_errors.append(
                    {
                        "arm": arm_name,
                        "reason": "legacy_ambiguous_head_sha",
                        "detail": (
                            f"workflow_run_id={record.get('workflow_run_id')!r} job={job!r}"
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


# =============================================================================
# Issue #2422: `benchmark_layout=monolith|split` A/B/A/B bounded-orchestrator
# manifest v2. Additive to everything above (v1's before/after collector
# stays available/unmodified for any other in-repo consumer); this section
# is the ONLY route that produces `e2e_performance_benchmark_manifest_v2`.
# =============================================================================

BENCHMARK_LAYOUTS = ("monolith", "split")
MEASURED_PROVIDER_JOBS = ("e2e-core", "e2e-responsive-matrix")
GATE_READY_JOB_NAME = "e2e"
# AC7: only these job names may start on a `benchmark_layout != ''` dispatch
# -- the measured provider jobs plus the single minimal gate-ready job
# #2423's production close-grade materializer requires.
ALLOWED_V2_JOB_NAMES = MEASURED_PROVIDER_JOBS + (GATE_READY_JOB_NAME,)

_RUNNER_IMAGE_PLACEHOLDER_VALUES = frozenset({"", "unknown", "unknown/unknown", "n/a"})


class OperationalErrorV2(OperationalError):
    """Alias kept distinct in name (not behavior) so v2 call sites read
    unambiguously; both are caught identically by v1 CLI error handling."""


def compute_experiment_run_set_digest(runs: list[dict]) -> str:
    """Issue #2422 AC5: `runs` is a list of per-run identity dicts, each
    carrying ONLY `block_id` / `benchmark_layout` / `workflow_run_id` /
    `run_attempt` -- NEVER `conclusion`/outcome (this digest identifies the
    frozen dispatch root run set independent of whether any individual run
    succeeded or failed, so a later re-run of the SAME plan against the
    SAME root run set produces the SAME digest regardless of results).
    Canonicalized via `json.dumps(sort_keys=True, separators=(",", ":"))`
    over a list SORTED by `(block_id, benchmark_layout, workflow_run_id,
    run_attempt)` -- the result is independent of the input list's order."""
    identity_tuples = [
        {
            "block_id": r["block_id"],
            "benchmark_layout": r["benchmark_layout"],
            "workflow_run_id": r["workflow_run_id"],
            "run_attempt": r["run_attempt"],
        }
        for r in runs
    ]
    identity_tuples.sort(
        key=lambda d: (d["block_id"], d["benchmark_layout"], d["workflow_run_id"], d["run_attempt"])
    )
    canonical = json.dumps(identity_tuples, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_workflow_digest_from_commit_bytes(
    workflow_sha: str,
    repo: str,
    path: str = ".github/workflows/ci.yml",
    api_call: Callable[[str], Any] = _default_gh_api_call,
) -> str:
    """Issue #2422 AC1/AC3: fetches `path`'s bytes AT the commit
    `workflow_sha` via the GitHub Contents API
    (`GET /repos/{repo}/contents/{path}?ref={workflow_sha}`), and returns
    `sha256:<hex>` of those EXACT decoded bytes. Deliberately NOT a
    `sha256sum` of a post-checkout local working-tree file -- the checked-out
    ref a benchmark dispatch runs under can differ from `workflow_sha` (the
    dispatch `ref` stays on the current/default branch tip while
    `workflow_sha` records the commit the workflow definition was actually
    AT when the run was dispatched, see docs/dev/e2e-performance-benchmark.md
    "workflow_digest / workflow_sha の既知の限界"), so computing from a local
    checkout would silently conflate whatever happens to be on disk with the
    claimed `workflow_sha` provenance pair -- exactly the known limitation
    #2184 deferred to this Issue."""
    if not _is_valid_sha(workflow_sha):
        raise OperationalErrorV2(f"invalid_workflow_sha: {workflow_sha!r}")
    response = api_call(f"repos/{repo}/contents/{path}?ref={workflow_sha}")
    if (
        not isinstance(response, dict)
        or response.get("encoding") != "base64"
        or not isinstance(response.get("content"), str)
    ):
        raise LiveAPIError(
            f"contents_api_malformed_response: repo={repo} path={path} ref={workflow_sha}"
        )
    raw_bytes = base64.b64decode(response["content"])
    return "sha256:" + hashlib.sha256(raw_bytes).hexdigest()


def verify_workflow_digest_matches_commit_bytes(
    workflow_sha: str,
    claimed_workflow_digest: str,
    repo: str,
    api_call: Callable[[str], Any] = _default_gh_api_call,
) -> list[str]:
    """Issue #2422 AC3: independently RECOMPUTES `workflow_digest` from
    `workflow_sha`'s own commit bytes (never trusting the claimed value) and
    compares it to `claimed_workflow_digest`. This is what rejects the
    false-green case AC3 explicitly calls out: two arms could each
    independently claim the SAME (wrong-commit) digest -- a cross-arm
    required-equal check ALONE would incorrectly PASS that case (equal, but
    equally wrong) -- so this recomputation-from-source-of-truth check is
    REQUIRED IN ADDITION TO, never a replacement for, the cross-arm
    equality check (see `verify_cross_arm_required_equal` below)."""
    try:
        recomputed = compute_workflow_digest_from_commit_bytes(workflow_sha, repo, api_call=api_call)
    except (LiveAPIError, OperationalErrorV2) as exc:
        return [f"workflow_digest_recomputation_failed: {exc}"]
    if recomputed != claimed_workflow_digest:
        return [
            f"workflow_digest_mismatch_vs_commit_bytes: workflow_sha={workflow_sha!r} "
            f"claimed={claimed_workflow_digest!r} recomputed={recomputed!r}"
        ]
    return []


def verify_cross_arm_required_equal(runs: list[dict]) -> list[str]:
    """Issue #2422 AC1/AC3: `workflow_sha` and `workflow_digest` must be
    IDENTICAL across every run in `runs` (both `benchmark_layout` arms) --
    `benchmark_layout` is the ONLY intended treatment; anything else
    differing would confound the comparison. Returns a list of violation
    strings (empty == every run agrees)."""
    violations: list[str] = []
    for field in ("workflow_sha", "workflow_digest"):
        values = {r.get(field) for r in runs if field in r}
        if len(values) > 1:
            violations.append(f"cross_arm_fingerprint_mismatch_{field}: values={sorted(values, key=str)!r}")
    return violations


def _is_placeholder_runner_image_value(value: object) -> bool:
    return not isinstance(value, str) or value.strip().lower() in _RUNNER_IMAGE_PLACEHOLDER_VALUES


def verify_exact_runner_image(image: object) -> list[str]:
    """Issue #2422 AC4: `image` must be an object with non-empty, non-
    placeholder `name`/`version` string fields -- rejects `unknown`, empty
    string, `None`, a bare OS/architecture-only value (e.g.
    `"unknown/unknown"`, this module's own `host_runner_image` fallback
    shape for a DIFFERENT, run-level concept, never conflated with this
    job-level exact identity), and a non-object value outright."""
    if not isinstance(image, dict):
        return ["exact_runner_image_not_object"]
    violations: list[str] = []
    for field in ("name", "version"):
        if _is_placeholder_runner_image_value(image.get(field)):
            violations.append(f"exact_runner_image_missing_or_placeholder_{field}")
    return violations


# #2422 AC8 fix_delta (live smoke dispatch verification against real
# `gh api repos/{repo}/actions/jobs/{id}/logs` output, PR #2501): the
# pre-fix_delta regexes below were modeled on a HYPOTHETICAL `Image
# Version:` line that does not exist in a real GitHub-hosted runner job
# log's `##[group]Runner Image ... ##[endgroup]` section -- the ACTUAL
# format is a separate bare `Version:` line immediately following `Image:`
# (confirmed against 6 real job logs from the `blocks=2` AC8 smoke dispatch,
# e.g. workflow_job_id=101248519729). A blind `^Version:` search (without
# scoping to the `Runner Image` group) would silently pick up the WRONG
# `Version:` line -- the log ALSO carries an earlier, unrelated
# `##[group]Runner Image Provisioner` section with its own `Version:` line
# (the Hosted Compute Agent's own version, e.g. `20260828.587`) BEFORE the
# real `##[group]Runner Image` section (whose `Version:` line, e.g.
# `20260831.293.1`, is the genuine runner-image version). `_RUNNER_IMAGE_GROUP_RE`
# isolates ONLY the real `##[group]Runner Image` body (never `...
# Provisioner`, disambiguated by requiring the literal `Runner Image` marker
# be immediately followed by a newline) before searching for `Image:`/
# `Version:` within it -- this is what prevents the Provisioner's decoy
# `Version:` line from ever being mistaken for the exact runner image
# version. Falls back to searching the full `log_text` when no such group
# marker is present (e.g. a minimal/synthetic log excerpt with no decoy
# `Version:` line to disambiguate against).
_RUNNER_IMAGE_GROUP_RE = re.compile(r"##\[group\]Runner Image\r?\n(?P<body>.*?)##\[endgroup\]", re.DOTALL)
_SET_UP_JOB_IMAGE_RE = re.compile(r"^Image:\s*(?P<name>\S.*?)\s*$", re.MULTILINE)
_SET_UP_JOB_IMAGE_VERSION_RE = re.compile(r"^Version:\s*(?P<version>\S.*?)\s*$", re.MULTILINE)


def extract_exact_runner_image_from_job_log(log_text: str) -> dict | None:
    """Issue #2422 AC4 (fix_delta after #2422 AC8 live smoke dispatch,
    PR #2501 -- see the module-level comment above `_RUNNER_IMAGE_GROUP_RE`
    for the real-log-format defect this replaced): parses the real
    `##[group]Runner Image ... ##[endgroup]` section GitHub Actions emits
    for a GitHub-hosted runner job, e.g.:

        ##[group]Runner Image
        Image: ubuntu-24.04
        Version: 20260901.1.0
        ##[endgroup]

    Returns `{"name": ..., "version": ...}`, or `None` if either line is
    absent/empty (e.g. a containerized job whose host runner section does
    not surface these lines the same way -- callers must treat `None` as a
    hard verification failure via `fetch_exact_runner_image_for_job`, never
    silently substitute an OS/architecture-only fallback). `log_text` is
    expected to already have any per-line GitHub Actions log timestamp
    prefix (e.g. `2026-09-05T04:29:18.1156490Z `) stripped by the caller's
    `log_fetch` -- this function's own line-anchored (`^`) regexes do not
    strip it themselves."""
    group_match = _RUNNER_IMAGE_GROUP_RE.search(log_text)
    scoped_text = group_match.group("body") if group_match else log_text
    name_match = _SET_UP_JOB_IMAGE_RE.search(scoped_text)
    version_match = _SET_UP_JOB_IMAGE_VERSION_RE.search(scoped_text)
    if not name_match or not version_match:
        return None
    name = name_match.group("name").strip()
    version = version_match.group("version").strip()
    if _is_placeholder_runner_image_value(name) or _is_placeholder_runner_image_value(version):
        return None
    return {"name": name, "version": version}


def fetch_exact_runner_image_for_job(
    workflow_job_id: int,
    repo: str,
    log_fetch: Callable[[int, str], str],
) -> dict:
    """Issue #2422 AC4: `log_fetch(workflow_job_id, repo) -> str` is
    dependency-injected -- the real implementation fetches ONLY this
    specific job's own log (e.g. `gh api
    repos/{repo}/actions/jobs/{workflow_job_id}/logs`), never a different
    "probe" job's log and never a run-level aggregate. Raises
    `LiveAPIError` (fail-closed) if the `Set up job` section cannot be
    parsed out of the fetched log."""
    log_text = log_fetch(workflow_job_id, repo)
    image = extract_exact_runner_image_from_job_log(log_text)
    if image is None:
        raise LiveAPIError(
            f"exact_runner_image_not_found_in_set_up_job_log: workflow_job_id={workflow_job_id!r}"
        )
    return image


def verify_exact_runner_image_required_equal_within_block(block: dict) -> list[str]:
    """Issue #2422 AC4: image-identity required-equal is scoped to the
    SAME `block_id`, compared ACROSS its two `benchmark_layout` runs for
    matching job names ONLY -- deliberately never asserted equal across
    DIFFERENT blocks (GitHub hosted-runner image rolling updates between
    blocks are expected and must never fail the whole experiment)."""
    violations: list[str] = []
    images_by_job_name: dict[str, list[tuple[str, dict]]] = {}
    for run in block.get("runs", []):
        layout = run.get("benchmark_layout")
        for job in run.get("provider_jobs", []):
            image = job.get("exact_runner_image")
            if not isinstance(image, dict):
                continue
            images_by_job_name.setdefault(job.get("job"), []).append((layout, image))
    for job_name, entries in images_by_job_name.items():
        if len(entries) < 2:
            continue
        canonical = json.dumps(entries[0][1], sort_keys=True)
        for layout, image in entries[1:]:
            if json.dumps(image, sort_keys=True) != canonical:
                violations.append(
                    f"exact_runner_image_mismatch_within_block: block_id={block.get('block_id')!r} "
                    f"job={job_name!r}"
                )
                break
    return violations


def verify_ab_alternating_order(blocks: list[dict]) -> list[str]:
    """Issue #2422 AC3/Out-of-Scope: every block's `runs` must be exactly
    `[monolith, split]` in THAT fixed order (never `[split, monolith]`,
    never AB/BA randomized order -- Out of Scope explicitly keeps the fixed
    A->B order + block_id matched-block design, deferring randomization to
    a future Issue)."""
    violations: list[str] = []
    for block in blocks:
        layouts = [r.get("benchmark_layout") for r in block.get("runs", [])]
        if layouts != ["monolith", "split"]:
            violations.append(
                f"ab_order_violation: block_id={block.get('block_id')!r} layouts={layouts!r} "
                "(expected exactly ['monolith', 'split'])"
            )
    return violations


def verify_block_ids_unique(blocks: list[dict]) -> list[str]:
    """Issue #2422 AC3: every `block_id` across the manifest must be
    unique -- a duplicate would silently merge two distinct matched-block
    dispatches into one identity."""
    seen: dict[str, int] = {}
    for block in blocks:
        block_id = block.get("block_id")
        seen[block_id] = seen.get(block_id, 0) + 1
    return [
        f"duplicate_block_id: block_id={block_id!r} count={count}"
        for block_id, count in sorted(seen.items(), key=str)
        if count > 1
    ]


def build_ab_block_plan(blocks: int) -> list[dict]:
    """Issue #2422 AC7/AC9: builds the A/B/A/B (monolith -> split, in that
    fixed order, repeated `blocks` times) dispatch PLAN for ANY positive
    integer `blocks` (including 22) -- pure computation, no live dispatch.
    `block_id` is `block-{index:04d}` (deterministic, 1-indexed, unique).
    Raises `OperationalErrorV2` for a non-positive-int `blocks` (fail-closed
    -- 0, negative, `bool`, and non-int are all rejected)."""
    if not isinstance(blocks, int) or isinstance(blocks, bool) or blocks < 1:
        raise OperationalErrorV2(f"invalid_blocks: {blocks!r} (must be a positive int)")
    return [{"block_id": f"block-{index:04d}", "layouts": ["monolith", "split"]} for index in range(1, blocks + 1)]


def dispatch_workflow_run(
    layout: str,
    block_id: str,
    frozen_source_sha: str,
    experiment_id: str,
    repo: str,
    workflow_file: str,
    ref: str,
    dispatch_call: Callable[..., Any],
) -> dict:
    """Issue #2422 AC7/Stop-Conditions: dispatches ONE `workflow_dispatch`
    with `benchmark_layout=layout`, and MUST request
    `return_run_details=True` from `dispatch_call` -- the response MUST
    carry an integer `workflow_run_id` (the 200 OK w/ return_run_details
    shape); a response lacking it (e.g. the 204 No Content GitHub returns
    when `return_run_details` is NOT requested) is fail-closed (raises
    `LiveAPIError`), NEVER accepted/inferred/polled-around via a
    `gh run list` post-hoc guess (this Issue's own Stop Conditions list
    forbids exactly that)."""
    if layout not in BENCHMARK_LAYOUTS:
        raise OperationalErrorV2(f"invalid_benchmark_layout: {layout!r}")
    # fix_delta (test-runner live AC8 dispatch, HTTP 422
    # "Unexpected inputs provided: [\"frozen_source_sha\"...]"): the
    # `workflow_dispatch.inputs` block in `.github/workflows/ci.yml` has no
    # `frozen_source_sha` key -- the pre-existing `target_sha` input is the
    # SAME "measured application-code commit" checkout selector (see its
    # description there), already consumed unconditionally by the
    # e2e-core / e2e-responsive-matrix checkout steps regardless of
    # `benchmark_layout`. Send it under the `target_sha` key the workflow
    # actually declares; the `frozen_source_sha` PARAMETER name here is kept
    # as-is (internal Python identifier only, not sent to the API).
    inputs = {
        "benchmark_layout": layout,
        "target_sha": frozen_source_sha,
        "block_id": block_id,
        "experiment_id": experiment_id,
    }
    response = dispatch_call(
        repo=repo,
        workflow_file=workflow_file,
        ref=ref,
        inputs=inputs,
        return_run_details=True,
    )
    if not isinstance(response, dict) or not isinstance(response.get("workflow_run_id"), int):
        raise LiveAPIError(
            "workflow_dispatch_response_missing_workflow_run_id: "
            f"block_id={block_id!r} layout={layout!r} response={response!r} "
            "(return_run_details=true must be honored by the dispatch response; "
            "a 204 No Content / gh run list post-hoc guess is never substituted)"
        )
    return {
        "workflow_run_id": response["workflow_run_id"],
        "run_url": response.get("html_url") or response.get("run_url"),
        "benchmark_layout": layout,
        "block_id": block_id,
    }


def run_bounded_experiment(
    blocks: int,
    frozen_source_sha: str,
    experiment_id: str,
    repo: str,
    workflow_file: str,
    ref: str,
    dispatch_call: Callable[..., Any],
) -> list[dict]:
    """Issue #2422 AC7/AC9: bounded orchestrator entrypoint -- dispatches
    the FULL A/B/A/B plan for `blocks` matched blocks (2*blocks total
    dispatches) and returns the dispatch ROOT RUN SET (fixed at dispatch
    time, before any outcome is known -- Issue #2422 AC6: callers must
    never later filter this list by outcome or dispatch additional runs to
    compensate for a failure)."""
    plan = build_ab_block_plan(blocks)
    root_run_set: list[dict] = []
    for block in plan:
        for layout in block["layouts"]:
            root_run_set.append(
                dispatch_workflow_run(
                    layout,
                    block["block_id"],
                    frozen_source_sha,
                    experiment_id,
                    repo,
                    workflow_file,
                    ref,
                    dispatch_call,
                )
            )
    return root_run_set


def _run_identity_tuples_from_blocks(blocks: list[dict]) -> list[dict]:
    """Issue #2422 AC5: `block_id` lives on the `Block`, not the `Run`
    (the schema's `Run` def intentionally omits it -- a run's block
    membership is unambiguous from its position inside `blocks[]`). This
    helper reconstructs the per-run `{block_id, benchmark_layout,
    workflow_run_id, run_attempt}` identity tuple `compute_experiment_run_
    set_digest` expects by pairing each run with its OWN block's
    `block_id`."""
    identity_tuples: list[dict] = []
    for block in blocks:
        block_id = block.get("block_id")
        for run in block.get("runs", []):
            identity_tuples.append(
                {
                    "block_id": block_id,
                    "benchmark_layout": run.get("benchmark_layout"),
                    "workflow_run_id": run.get("workflow_run_id"),
                    "run_attempt": run.get("run_attempt"),
                }
            )
    return identity_tuples


def build_manifest_v2(
    experiment_identity: str,
    frozen_source_sha: str,
    workflow_sha: str,
    workflow_digest: str,
    frozen_non_treatment: dict,
    blocks: list[dict],
    generated_at: str | None = None,
) -> dict:
    """Issue #2422 AC5: assembles an `e2e_performance_benchmark_manifest_v2`
    dict from already-collected `blocks` (each `{"block_id": str, "runs":
    [<Run>, <Run>]}`, `Run` matching the schema's `Run` def). Computes
    `experiment_run_set_digest` from every run's identity tuple. Semantic
    (cross-field) violations -- A/B/A/B ordering, duplicate block_id,
    cross-arm workflow_sha/workflow_digest mismatch, per-block runner-image
    mismatch -- are collected into `evidence_errors`, never silently
    dropped, and NEVER cause this function itself to raise (fail-closed
    detection is the CALLER's responsibility, matching this module's
    existing v1 `collect_benchmark_manifest`/`main()` split)."""
    evidence_errors: list[dict] = []
    all_runs: list[dict] = []
    for block in blocks:
        all_runs.extend(block.get("runs", []))

    for reason in verify_ab_alternating_order(blocks):
        evidence_errors.append({"block_id": "<plan>", "reason": "ab_order_violation", "detail": reason})
    for reason in verify_block_ids_unique(blocks):
        evidence_errors.append({"block_id": "<plan>", "reason": "duplicate_block_id", "detail": reason})
    for reason in verify_cross_arm_required_equal(all_runs):
        evidence_errors.append({"block_id": "<all>", "reason": "cross_arm_fingerprint_mismatch", "detail": reason})
    for block in blocks:
        for reason in verify_exact_runner_image_required_equal_within_block(block):
            evidence_errors.append(
                {
                    "block_id": block.get("block_id", "<unknown>"),
                    "reason": "exact_runner_image_mismatch",
                    "detail": reason,
                }
            )
        for run in block.get("runs", []):
            for job in run.get("provider_jobs", []):
                image_violations = verify_exact_runner_image(job.get("exact_runner_image"))
                for violation in image_violations:
                    evidence_errors.append(
                        {
                            "block_id": block.get("block_id", "<unknown>"),
                            "reason": "exact_runner_image_invalid",
                            "detail": (
                                f"job={job.get('job')!r} "
                                f"workflow_run_id={run.get('workflow_run_id')!r}: {violation}"
                            ),
                        }
                    )

    run_set_digest = compute_experiment_run_set_digest(_run_identity_tuples_from_blocks(blocks))

    return {
        "schema": MANIFEST_SCHEMA_V2,
        "schema_version": 2,
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "experiment_identity": experiment_identity,
        "experiment_run_set_digest": run_set_digest,
        "frozen_source_sha": frozen_source_sha,
        "workflow_sha": workflow_sha,
        "workflow_digest": workflow_digest,
        "frozen_non_treatment": frozen_non_treatment,
        "blocks": blocks,
        "evidence_errors": evidence_errors,
    }


def validate_manifest_v2_semantics(manifest: dict) -> list[str]:
    """Issue #2422 AC3/AC5: standalone re-derivation of the same semantic
    checks `build_manifest_v2` performs, importable for an independent
    consumer (mirrors this module's existing v1
    `validate_manifest_semantics` pattern) -- re-verifies A/B/A/B order,
    block_id uniqueness, cross-arm required-equal fingerprint, per-block
    runner-image required-equal, AND the `experiment_run_set_digest`
    recomputation (never trusting the manifest's own claimed digest)."""
    violations: list[str] = []
    blocks = manifest.get("blocks", [])
    all_runs: list[dict] = []
    for block in blocks:
        all_runs.extend(block.get("runs", []))

    violations.extend(f"ab_order_violation: {v}" for v in verify_ab_alternating_order(blocks))
    violations.extend(f"duplicate_block_id: {v}" for v in verify_block_ids_unique(blocks))
    violations.extend(f"cross_arm_fingerprint_mismatch: {v}" for v in verify_cross_arm_required_equal(all_runs))
    for block in blocks:
        violations.extend(
            f"exact_runner_image_mismatch: {v}" for v in verify_exact_runner_image_required_equal_within_block(block)
        )

    recomputed_digest = compute_experiment_run_set_digest(_run_identity_tuples_from_blocks(blocks))
    if manifest.get("experiment_run_set_digest") != recomputed_digest:
        violations.append(
            "experiment_run_set_digest_mismatch: "
            f"claimed={manifest.get('experiment_run_set_digest')!r} recomputed={recomputed_digest!r}"
        )

    for field in ("workflow_sha", "workflow_digest"):
        root_value = manifest.get(field)
        for run in all_runs:
            if run.get(field) != root_value:
                violations.append(f"run_{field}_mismatches_root: run={run.get('workflow_run_id')!r}")

    return violations


def _validate_against_schema_v2(manifest: dict) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise OperationalErrorV2(f"jsonschema_not_installed: {exc}") from exc

    with open(SCHEMA_PATH_V2, encoding="utf-8") as handle:
        schema = json.load(handle)
    Draft202012Validator.check_schema(schema)
    validator_instance = Draft202012Validator(schema)
    errors = sorted(validator_instance.iter_errors(manifest), key=lambda e: e.path)
    if errors:
        messages = [f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}" for err in errors]
        raise OperationalErrorV2(f"manifest_v2_failed_schema_validation: {messages}")


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


def _default_dispatch_call(
    repo: str,
    workflow_file: str,
    ref: str,
    inputs: dict,
    return_run_details: bool = True,
) -> Any:
    """Issue #2422 AC7: default `dispatch_call` transport for
    `dispatch_workflow_run`/`run_bounded_experiment` -- `gh api` POST to the
    workflow dispatches endpoint, ALWAYS passing `return_run_details: true`
    in the request body (this is what lets GitHub return `workflow_run_id`
    synchronously in a 200 OK response body instead of a 204 No Content
    with no run id, per this Issue's Stop Conditions)."""
    payload = {"ref": ref, "inputs": inputs, "return_run_details": return_run_details}
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{repo}/actions/workflows/{workflow_file}/dispatches",
                "--input",
                "-",
            ],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiveAPIError(f"gh_api_dispatch_transport_error: {exc}") from exc
    if result.returncode != 0:
        raise LiveAPIError(f"gh_api_dispatch_call_failed: {result.stderr.strip()}")
    stdout = result.stdout.strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise LiveAPIError(f"gh_api_dispatch_response_not_json: {exc}") from exc


def parse_run_experiment_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="collect_e2e_performance_benchmark.py run-experiment",
        description=(
            "Issue #2422 AC7/AC9: bounded orchestrator -- dispatches an "
            "A/B/A/B (monolith->split, `blocks` times) benchmark_layout "
            "experiment and writes the resulting dispatch root run set "
            "(fixed at dispatch time, before any outcome is known) as JSON."
        ),
    )
    parser.add_argument(
        "--blocks",
        type=int,
        required=True,
        help="Number of matched (monolith, split) blocks -- any positive int, e.g. 2 or 22",
    )
    parser.add_argument(
        "--frozen-source-sha",
        required=True,
        help="Frozen 40-hex application-code commit SHA measured by BOTH benchmark_layout arms",
    )
    parser.add_argument("--experiment-id", required=True, help="Free-form experiment identifier")
    parser.add_argument("--repo", required=True, help="owner/repo, e.g. squne121/loop-protocol")
    parser.add_argument("--workflow-file", default="ci.yml", help="Workflow file name (default ci.yml)")
    parser.add_argument(
        "--ref",
        default="main",
        help=(
            "Dispatch ref -- must stay on the current/default branch tip so "
            "github.workflow_sha remains current (default main)"
        ),
    )
    parser.add_argument("--output", required=True, help="Path to write the dispatched root run set JSON")
    return parser.parse_args(argv)


def main_run_experiment(argv: list[str] | None = None) -> int:
    args = parse_run_experiment_args(argv)
    try:
        root_run_set = run_bounded_experiment(
            args.blocks,
            args.frozen_source_sha,
            args.experiment_id,
            args.repo,
            args.workflow_file,
            args.ref,
            _default_dispatch_call,
        )
    except (OperationalErrorV2, LiveAPIError) as exc:
        sys.stderr.write(f"operational_failure: {exc}\n")
        return EXIT_OPERATIONAL_FAILURE

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(root_run_set, handle, indent=2)
        handle.write("\n")
    print(
        f"dispatched {len(root_run_set)} runs across {args.blocks} blocks "
        "(root run set fixed at dispatch time, outcome-independent)"
    )
    return EXIT_COMPLETE


if __name__ == "__main__":
    # Issue #2422 AC7/AC9: `run-experiment` is a distinct sub-invocation,
    # dispatched here (never inside `parse_args()`/`main()` above, which
    # stay byte-for-byte backward compatible for every existing v1 caller/
    # test that invokes `main(argv)` directly with the legacy
    # `--before-sha`/`--after-sha` flag shape).
    if len(sys.argv) > 1 and sys.argv[1] == "run-experiment":
        sys.exit(main_run_experiment(sys.argv[2:]))
    sys.exit(main())
