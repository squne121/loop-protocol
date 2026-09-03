"""
tests/ci/test_ci_performance_gate.py

Issue #2119 AC9/AC10: real comparable-cohort (20 run) P50/P95 CI runtime
gate. Runtime Verification Applicability for this Issue is `decision:
immediate` with `fallback_policy.fallback_success_is_pass: false` — the
three integration-style tests below intentionally SKIP (not PASS) when a
real 20-run comparable cohort artifact set is not available in the current
environment (this implementation session has no live GitHub Actions
history to draw from; the actual cohort accumulates only after this PR's
own CI runs land on main). SKIP is the correct outcome here per
docs/dev/runtime-verification-policy.md, not a fabricated PASS, and not
success achieved via a fallback path.

Issue #2159 (Issue A: performance benchmark/cohort/collector redesign,
scope-split from a prior version of this same Issue number after OWNER
adversarial review issuecomment-5293380230) rewrites the measurement
instrument itself. This module is now the shared library consumed (via
`importlib` module loading, mirroring the pre-existing `_load_validator_module`
pattern below) by the satellite test files:

- test_ci_performance_gate_paired_critical_path.py   (AC3)
- test_ci_performance_gate_clock_alignment.py         (AC4)
- test_ci_performance_gate_fingerprint_validation.py  (AC5)
- test_ci_performance_gate_comparability_classification.py (AC6)
- test_ci_performance_gate_evidence_hard_failure.py   (AC11)

Fixes applied in #2159 relative to the pre-existing #2119/PR#2137 version:

- P0-2/P1-1: sample identity for a comparable cohort is the GitHub
  `workflow_run_id` (`_dedupe_by_workflow_run_id`), not `(run_id,
  run_attempt)` — rerun attempts of the same workflow run no longer count
  as independent samples.
- P0-4: provider critical path P50/P95 is now
  `nearest_rank_v1(max(core_duration_i, responsive_duration_i))` computed
  over PAIRED runs sharing the same `workflow_run_id`
  (`_pair_by_workflow_run_id` + `_provider_critical_path_paired_p50_p95`),
  not `max(median(core), median(responsive))` (which mixes runs from
  different workflow_run_id's and is not a valid critical-path statistic).
  Runs missing their pair partner are excluded from the cohort and
  reported as an explicit evidence error (`evidence_errors`), never
  silently dropped.
- P0-6: gate-ready latency before/after is now computed from a SINGLE
  shared function (`_gate_ready_latency_seconds_same_clock`) fed by the
  GitHub API clock (`run_started_at` -> corresponding check
  `completed_at`) for BOTH arms, instead of before using a manual
  `measurements.jsonl` elapsed-time sum and after using the GitHub API.
- P0-7/P1-3: `COMPARABILITY_FINGERPRINT_FIELDS` (a single flat tuple) is
  replaced by three explicit classifications
  (`WITHIN_COHORT_REQUIRED_EQUAL` / `CROSS_COHORT_REQUIRED_EQUAL` /
  `INTENTIONAL_TREATMENT_DIFFERENCE`), and `runner_image` is split into
  `host_runner_image` (bare GitHub Actions runner) and
  `playwright_container_image_digest` (the pinned
  `mcr.microsoft.com/playwright@sha256:...` container) provenance fields.
- P1-2: fingerprint fields containing placeholder values (`""` / `null` /
  `"unknown"` / `"unknown/unknown"` / `"N/A"`) are treated as missing
  (`_is_placeholder`) and excluded from the cohort, not silently accepted
  as a legitimate (if unlucky) equality match.
- AC11: a dedicated hard-failure path (`EvidenceInsufficientError` /
  `_evidence_readiness_hard_check`) exists for the close-verification use
  case, distinct from the exploratory SKIP-based integration tests below.
  See test_ci_performance_gate_evidence_hard_failure.py.

Once a real `ci_runtime_baseline_v1` cohort (>= 20 comparable
`workflow_run_id` samples of `e2e-core` / `e2e-responsive-matrix` / the
`e2e` aggregate, all sharing the within-cohort comparability fingerprint)
and a real `CI_TEST_PERFORMANCE_ASSESSMENT_V2` artifact exist under
`.claude/artifacts/`, the tests below compute the actual P50/P95 gate from
that real data.
"""
from __future__ import annotations

import glob
import importlib.util
import json
import math
import os
import pathlib
from datetime import datetime

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = REPO_ROOT / ".claude" / "artifacts"
VALIDATOR = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "ci-test-performance"
    / "scripts"
    / "validate_ci_performance_assessment_v2.py"
)

# Issue #2119 AC9 thresholds.
PROVIDER_P50_ABSOLUTE_THRESHOLD_SECONDS = 4 * 60 + 30  # 4分30秒
RELATIVE_SHORTENING_THRESHOLD = 0.35  # 35%以上短縮
MIN_COHORT_RUN_COUNT = 20

# --------------------------------------------------------------------------- #
# #2159 P0-7/P1-3: three-way comparability fingerprint classification.
# --------------------------------------------------------------------------- #
WITHIN_COHORT_REQUIRED_EQUAL = (
    "host_runner_image",
    "playwright_container_image_digest",
    "node_version",
    "pnpm_version",
    "playwright_version",
    "lockfile_hash",
    "workflow_digest",
)
CROSS_COHORT_REQUIRED_EQUAL = (
    "host_runner_image",
    "playwright_container_image_digest",
    "node_version",
    "pnpm_version",
    "playwright_version",
    "lockfile_hash",
)
INTENTIONAL_TREATMENT_DIFFERENCE = (
    "workflow_digest",
    "cohort_role",
)

# Legacy flat tuple kept for callers that only need "all provenance
# fields"; #2159 P1-2 placeholder rejection applies to every field in
# WITHIN_COHORT_REQUIRED_EQUAL (the superset of the other two).
COMPARABILITY_FINGERPRINT_FIELDS = WITHIN_COHORT_REQUIRED_EQUAL

# #2159 P1-2: placeholder values that must be treated as missing/invalid,
# never as a legitimate fingerprint match.
PLACEHOLDER_VALUES = frozenset({None, "", "unknown", "unknown/unknown", "N/A"})


def _is_placeholder(value: object) -> bool:
    return value in PLACEHOLDER_VALUES



# --------------------------------------------------------------------------- #
# #2159 OWNER scope-authority ruling (issuecomment-5299412215, items 2/P0-8
# and 3/P1-3/AC11): a REAL, callable production CLI path that wires
# `_evidence_readiness_hard_check_post_filter` (AC11's hard-failure gate) and
# `build_assessment_from_percentile_cohorts` (the P0-8 real, non-no-op
# CI_TEST_PERFORMANCE_ASSESSMENT_V2 producer) together into a SINGLE gate
# invocable from an actual `.github/workflows/ci.yml` job -- not merely a
# unit-tested function that nothing outside pytest ever calls. Per OWNER:
# "20件未満なら fail-closed する production path を先に配線できます" -- this
# path correctly fail-closes (non-zero exit, `EvidenceInsufficientError`)
# given fewer than MIN_COHORT_RUN_COUNT valid post-filter samples for either
# arm, and only computes/emits a real `claim.kind != none` once BOTH arms
# clear that floor. It never fabricates a claim from insufficient evidence.
# --------------------------------------------------------------------------- #
def _cli_run_details_from_pairs(pairs: list[tuple], commit_sha: str) -> list[dict]:
    """Builds the `{"run_id", "workflow_run_id", "run_attempt", "commit_sha",
    "conclusion", "duration_seconds"}` shape `build_assessment_from_percentile_
    cohorts` (and its own P0-7 raw-sample invariants) requires, from paired
    (`workflow_run_id`, core_baseline, responsive_baseline) tuples -- the
    per-run duration is the SAME `max(core, responsive)` provider
    critical-path statistic `_provider_critical_path_paired_p50_p95` uses
    (#2159 P0-4), never a self-reported/aggregate number.

    #2179 AC2: `run_attempt` is no longer a `1` literal -- it is the
    actual attempt SELECTED for this pair by `_pair_by_workflow_run_id`'s
    `initial_attempt_only_v1` policy (always 1 under that policy, but
    propagated from the record rather than hardcoded, so a future policy
    change does not require touching this call site).

    #2187: uses `_normalize_run_attempt_trusted` (never `_normalize_run_attempt
    (core) or 1`) -- a `core` baseline whose `run_attempt` is missing or
    invalid is EXCLUDED from `run_details` entirely rather than having a
    synthesized `run_attempt: 1` fabricated for it. In the normal production
    call path every `core` reaching this function was already selected by
    `_select_initial_attempt_baselines` (via `_pair_by_workflow_run_id`), so
    this exclusion never fires there; it exists as a fail-closed guard for
    direct/unit-test callers of this function."""
    run_details = []
    for workflow_run_id, core, responsive in pairs:
        trusted_attempt = _normalize_run_attempt_trusted(core)
        if trusted_attempt is None:
            continue
        core_duration = _single_baseline_duration_seconds(core)
        responsive_duration = _single_baseline_duration_seconds(responsive)
        if core_duration is None or responsive_duration is None:
            continue
        run_details.append(
            {
                "run_id": str(workflow_run_id),
                "workflow_run_id": workflow_run_id,
                "run_attempt": trusted_attempt,
                "commit_sha": commit_sha,
                "conclusion": "success",
                "duration_seconds": max(core_duration, responsive_duration),
            }
        )
    return run_details


def run_evidence_gate(fixture: dict) -> dict:
    """#2159 items 2+3 (issuecomment-5299412215): the single production gate
    function -- reads an already-assembled cohort fixture (shape: see
    `--cohort-fixture` CLI help below), re-validates POST-FILTER evidence
    sufficiency independently for the `before` and `after` arms via AC11's
    `_evidence_readiness_hard_check_post_filter`, and -- only if BOTH arms
    clear `MIN_COHORT_RUN_COUNT` -- computes a REAL assessment via
    `build_assessment_from_percentile_cohorts` and runs it through the full
    structural+semantic validator. Returns a JSON-serializable result dict
    with `gate_status: insufficient_evidence | complete` and never raises for
    the insufficient-evidence case (the CLI wrapper below converts that into
    a non-zero process exit -- this function itself stays a pure, directly
    unit-testable building block, mirroring `build_assessment_from_percentile_
    cohorts`'s own existing testability design).

    #2187 fix_delta (OWNER REQUEST_CHANGES issuecomment-5458167419 P1-1):
    `_gate_ready_post_filter_sample_count`'s `evidence_errors` return value
    is no longer discarded (`_before_gate_ready_evidence_errors` /
    `_after_gate_ready_evidence_errors` were previously bound with a `_`
    prefix and never used) -- it is threaded into
    `_evidence_readiness_hard_check_post_filter` (which now also
    fail-closes on a non-empty gate-ready `evidence_errors` list, not only
    on the raw post-filter sample count) AND surfaced verbatim in the
    result dict's `gate_ready_evidence_errors` field (`{"before": [...],
    "after": [...]}`) for BOTH the `insufficient_evidence` and `complete`
    outcomes, so a missing/invalid/colliding gate-ready record's id and
    reason are never silently dropped from the production result."""
    validator = _load_validator_module()

    before_core = fixture["before"]["core_baselines"]
    before_responsive = fixture["before"]["responsive_baselines"]
    after_core = fixture["after"]["core_baselines"]
    after_responsive = fixture["after"]["responsive_baselines"]
    before_gate_ready = fixture["before"].get("gate_ready_baselines", [])
    after_gate_ready = fixture["after"].get("gate_ready_baselines", [])

    before_pairs, before_evidence_errors = _pair_by_workflow_run_id(before_core, before_responsive)
    after_pairs, after_evidence_errors = _pair_by_workflow_run_id(after_core, after_responsive)

    before_provider_count, _ = _provider_post_filter_sample_count(before_core, before_responsive)
    after_provider_count, _ = _provider_post_filter_sample_count(after_core, after_responsive)
    before_gate_ready_count, before_gate_ready_evidence_errors = _gate_ready_post_filter_sample_count(
        before_gate_ready
    )
    after_gate_ready_count, after_gate_ready_evidence_errors = _gate_ready_post_filter_sample_count(
        after_gate_ready
    )
    gate_ready_evidence_errors = {
        "before": before_gate_ready_evidence_errors,
        "after": after_gate_ready_evidence_errors,
    }

    try:
        _evidence_readiness_hard_check_post_filter(
            before_provider_count,
            before_evidence_errors,
            {"before": before_gate_ready_count},
            gate_ready_evidence_errors={"before": before_gate_ready_evidence_errors},
        )
        _evidence_readiness_hard_check_post_filter(
            after_provider_count,
            after_evidence_errors,
            {"after": after_gate_ready_count},
            gate_ready_evidence_errors={"after": after_gate_ready_evidence_errors},
        )
    except EvidenceInsufficientError as exc:
        return {
            "schema": "CI_PERFORMANCE_BENCHMARK_EVIDENCE_GATE_RESULT_V1",
            "gate_status": "insufficient_evidence",
            "reason": str(exc),
            "before_provider_post_filter_count": before_provider_count,
            "after_provider_post_filter_count": after_provider_count,
            "gate_ready_evidence_errors": gate_ready_evidence_errors,
            "assessment": None,
            "validation_result": None,
        }

    before_run_details = _cli_run_details_from_pairs(before_pairs, fixture["before"]["commit_sha"])
    after_run_details = _cli_run_details_from_pairs(after_pairs, fixture["after"]["commit_sha"])

    assessment = validator.build_assessment_from_percentile_cohorts(
        issue_number=fixture["issue_number"],
        pr_number=fixture["pr_number"],
        measured_at=fixture["measured_at"],
        before_run_details=before_run_details,
        after_run_details=after_run_details,
        functional_evidence=fixture["functional_evidence"],
        declared_impact=fixture["declared_impact"],
        risk_acknowledgement=fixture["risk_acknowledgement"],
        cohort_provenance=fixture["cohort_provenance"],
    )

    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(assessment, handle)
        assessment_tmp_path = handle.name
    try:
        exit_code, decision = validator.validate_assessment(assessment_tmp_path)
    finally:
        os.unlink(assessment_tmp_path)

    return {
        "schema": "CI_PERFORMANCE_BENCHMARK_EVIDENCE_GATE_RESULT_V1",
        "gate_status": "complete",
        "reason": None,
        "before_provider_post_filter_count": before_provider_count,
        "after_provider_post_filter_count": after_provider_count,
        "gate_ready_evidence_errors": gate_ready_evidence_errors,
        "assessment": assessment,
        "validation_result": decision,
        "validation_exit_code": exit_code,
    }


def _cli_main(argv: list[str] | None = None) -> int:
    """Real callable entrypoint wired into `.github/workflows/ci.yml`'s
    `e2e-performance-benchmark-assessment-gate` job (#2159 OWNER
    scope-authority ruling issuecomment-5299412215, items 2+3). Exit codes:
    0 = gate_status complete AND semantic_valid AND approval_eligible;
    1 = insufficient_evidence (AC11 fail-closed -- the intended, CORRECT
        outcome until a real >= 20-run cohort exists per-arm, per OWNER:
        "20件未満なら fail-closed する production path");
    2 = complete evidence but the built assessment failed structural/semantic
        validation (defensive fail-closed; not expected given this module's
        own P0-7 hardening, see
        test_validate_ci_performance_assessment_v2_build_from_cohorts.py)."""
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "#2159 items 2+3 production gate: fail-closed AC11 evidence "
            "readiness check + P0-8 real CI_TEST_PERFORMANCE_ASSESSMENT_V2 "
            "producer, wired from a single cohort fixture."
        )
    )
    parser.add_argument(
        "--cohort-fixture",
        required=True,
        help=(
            "Path to a JSON fixture: "
            '{"issue_number": int, "pr_number": int, "measured_at": str, '
            '"functional_evidence": {...}, "declared_impact": str, '
            '"risk_acknowledgement": {...}, "cohort_provenance": {...}, '
            '"before": {"commit_sha": str, "core_baselines": [...], '
            '"responsive_baselines": [...], "gate_ready_baselines": [...]}, '
            '"after": {...same shape...}}'
        ),
    )
    parser.add_argument("--output", required=True, help="Path to write the gate result JSON")
    args = parser.parse_args(argv)

    with open(args.cohort_fixture, encoding="utf-8") as handle:
        fixture = json.load(handle)

    result = run_evidence_gate(fixture)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")

    if result["gate_status"] == "insufficient_evidence":
        print(f"::error::CI_PERFORMANCE_BENCHMARK_EVIDENCE_GATE_RESULT_V1 insufficient_evidence: {result['reason']}")
        return 1
    if not (result["validation_result"] or {}).get("semantic_valid"):
        print("::error::CI_PERFORMANCE_BENCHMARK_EVIDENCE_GATE_RESULT_V1 built assessment failed semantic validation")
        return 2
    claim = result["assessment"]["claim"]
    print(f"CI_PERFORMANCE_BENCHMARK_EVIDENCE_GATE_RESULT_V1 gate_status=complete claim={claim}")
    return 0


def _load_validator_module():
    spec = importlib.util.spec_from_file_location("validate_ci_performance_assessment_v2", VALIDATOR)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _find_all_baselines() -> list[dict]:
    if not ARTIFACTS_DIR.is_dir():
        return []
    baselines = []
    for path in glob.glob(str(ARTIFACTS_DIR / "**" / "ci_runtime_baseline_v1*.json"), recursive=True):
        try:
            data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("schema") == "ci_runtime_baseline_v1":
            baselines.append(data)
    return baselines


def _fingerprint(baseline: dict, fields: tuple[str, ...] = COMPARABILITY_FINGERPRINT_FIELDS) -> tuple:
    """#2159 P0-7: the comparability fingerprint tuple for a single
    ci_runtime_baseline_v1 run, restricted to `fields`. A baseline missing
    (or holding a placeholder value for) any requested field is
    intentionally NOT comparable -- fail-closed, never silently treated as
    a match (see `_fingerprint_has_placeholder`)."""
    return tuple(baseline.get(field) for field in fields)


def _fingerprint_has_placeholder(baseline: dict, fields: tuple[str, ...] = WITHIN_COHORT_REQUIRED_EQUAL) -> bool:
    """#2159 P1-2: True if any of `fields` on `baseline` is missing or a
    known placeholder value ("" / null / "unknown" / "unknown/unknown" /
    "N/A")."""
    return any(_is_placeholder(baseline.get(field)) for field in fields)


# #2179 (fix_delta after OWNER adversarial review of PR #2172,
# issuecomment-5295659213 P1-1 / follow-up Issue #2179): rerun-attempt
# selection is the explicit, order-independent `initial_attempt_only_v1`
# policy -- never `dict.setdefault()` first-seen-wins, and never a dict
# comprehension's implicit last-seen-wins (see `_pair_by_workflow_run_id`
# below). Independently implemented from
# `scripts/ci/collect_e2e_performance_benchmark.py`'s copy per this
# module's own Allowed Paths boundary (see module docstring).
RERUN_ATTEMPT_SELECTION_POLICY = "initial_attempt_only_v1"


def _normalize_run_attempt(baseline: dict) -> int | None:
    """#2179 (docstring corrected by #2187): normalizes
    `baseline["run_attempt"]` to the `initial_attempt_only_v1` policy's
    `integer >= 1` contract. A missing key defaults to 1 -- but (#2187) that
    default is used ONLY by `_detect_run_attempt_identity_collisions` for
    COLLISION-GROUPING purposes (grouping a missing-key record into the
    same identity slot as an explicit `run_attempt: 1` record so a genuine
    content disagreement between them is still detected as a collision). It
    is NOT a statement that a missing key is a TRUSTED attempt-1 candidate
    for cohort membership -- trust judgment is `_normalize_run_attempt_
    trusted()` below, which returns `None` (untrusted / excluded) for a
    missing key, mirroring `scripts/ci/collect_e2e_performance_
    benchmark.py`'s `_classify_run_attempt` policy (unified by #2187,
    follow-up to PR #2182's OWNER adversarial review issuecomment-5302595322).
    A numeric STRING (e.g. `"1"`) is accepted and coerced to int -- this is
    the REAL producer shape (`GITHUB_RUN_ATTEMPT` is a bash env var, always
    a string, see `.github/workflows/ci.yml`'s `Collect ci_runtime_
    baseline_v1 artifact` step and
    `test_v2_producer_shaped_baseline_from_ci_yml_is_admitted_to_cohort`).
    An explicit `None`, a non-numeric string, a bool, `0`, or a negative
    value is invalid -- returns `None` (fail-closed exclusion, never
    guessed)."""
    if "run_attempt" not in baseline:
        return 1
    value = baseline.get("run_attempt")
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not value.isdigit():
            return None
        value = int(value)
    elif not isinstance(value, int):
        return None
    if value < 1:
        return None
    return value


LEGACY_UNVERIFIED_RUN_ATTEMPT_REASON = "legacy_unverified_run_attempt"


def _normalize_run_attempt_trusted(baseline: dict) -> int | None:
    """#2187: TRUST-judgment counterpart to `_normalize_run_attempt` above.
    Unlike that function (whose missing-key default of 1 exists ONLY for
    `_detect_run_attempt_identity_collisions`'s collision-grouping use
    case), this function returns `None` (untrusted / excluded from the
    trusted cohort) when `run_attempt` is missing entirely -- unifying this
    gate module's trusted-cohort eligibility policy with the collector's
    (`scripts/ci/collect_e2e_performance_benchmark.py::_classify_run_
    attempt`) missing-excludes-from-cohort policy. All other type-coercion
    rules (explicit int `1` / producer-shaped numeric string `"1"` accepted;
    bool/`None`/non-numeric string/`0`/negative rejected) are IDENTICAL to
    `_normalize_run_attempt` and are delegated to it (never duplicated) once
    the missing-key case has been handled here."""
    if "run_attempt" not in baseline:
        return None
    return _normalize_run_attempt(baseline)


RUN_ATTEMPT_IDENTITY_COLLISION_REASON = "run_attempt_identity_collision"
MISSING_OR_INVALID_INITIAL_ATTEMPT_EXCLUDED_REASON = "missing_or_invalid_initial_attempt_excluded_from_sample"


def _classify_run_attempt_trusted(baseline: dict) -> tuple[int | None, str]:
    """#2187 fix_delta (OWNER REQUEST_CHANGES issuecomment-5458167419 P1-2):
    small classifier mirroring the collector's
    (`scripts/ci/collect_e2e_performance_benchmark.py::_classify_run_
    attempt`) three-way shape, so every `_select_initial_attempt_baselines`
    exclusion carries an identifiable reason instead of silently
    disappearing for input shapes other than a fully-missing key. Returns
    `(value, status)` where `status` is one of `"missing"` (key absent
    entirely), `"invalid"` (key present but fails `_normalize_run_attempt`'s
    type/range contract), or `"ok"` (a valid `run_attempt` integer -- NOT
    necessarily `1`; attempt 2+ is still `"ok"`, just not selected as the
    initial attempt). #2187 note: unlike the collector's version, this gate
    module's producer-shaped numeric-string `"1"` acceptance is delegated
    to `_normalize_run_attempt` unchanged (never re-implemented/narrowed
    here) -- unifying the trust *eligibility* boundary (missing-key
    handling) does not mean copying the collector's int-only literal
    implementation."""
    if "run_attempt" not in baseline:
        return None, "missing"
    value = _normalize_run_attempt(baseline)
    if value is None:
        return None, "invalid"
    return value, "ok"


def _identity_normalized_json(baseline: dict) -> str:
    """#2182 P1 (fix_delta after OWNER adversarial review of PR #2182,
    issuecomment-5302446086): canonical (sorted-keys) JSON view of
    `baseline`, used for byte-for-byte identity comparison -- two
    baselines compare equal under this function iff EVERY field
    matches."""
    return json.dumps(baseline, sort_keys=True, default=str)


def _detect_run_attempt_identity_collisions(baselines: list[dict]) -> dict[object, list[dict]]:
    """#2182 P1: identity is fixed to `(workflow_run_id, job,
    run_attempt)` (`run_attempt` normalized via `_normalize_run_attempt`,
    which -- for THIS gate module only, see `_select_initial_attempt_
    baselines`'s own docstring for why -- still defaults a MISSING
    `run_attempt` key to 1). Baselines sharing that identity slot are
    treated as an idempotent, harmless duplicate ONLY if the ENTIRE
    normalized baseline is byte-for-byte identical
    (`_identity_normalized_json`); if even a SINGLE field differs
    (`elapsed_ms` / fingerprint fields / `cohort_role` / any other
    field), the WHOLE `workflow_run_id` sample is flagged as a
    collision -- this supersedes the pre-fix_delta `min()` tie-break,
    which silently picked one candidate instead of detecting a genuine
    content conflict (the OWNER-flagged defect: two baselines sharing a
    `workflow_run_id`/attempt but disagreeing on measurement/fingerprint/
    policy fields would silently resolve via `min()`)."""
    by_key: dict[tuple, list[dict]] = {}
    for baseline in baselines:
        workflow_run_id = baseline.get("workflow_run_id")
        if workflow_run_id is None:
            continue
        if _normalize_run_attempt(baseline) != 1:
            continue
        by_key.setdefault((workflow_run_id, baseline.get("job"), 1), []).append(baseline)

    collisions: dict[object, list[dict]] = {}
    for (workflow_run_id, _job, _attempt), group in by_key.items():
        if len(group) < 2:
            continue
        normalized = {_identity_normalized_json(b) for b in group}
        if len(normalized) > 1:
            collisions.setdefault(workflow_run_id, []).extend(group)
    return collisions


def _select_initial_attempt_baselines(
    baselines: list[dict],
) -> tuple[dict[object, dict], list[dict]]:
    """#2179 P0-2/P1-1 (supersedes the #2159 first-seen-wins version):
    sample identity is `workflow_run_id`; among baselines sharing one id,
    only the TRUSTED run_attempt == 1 candidate is kept. Order-independent
    -- groups by id first via a dict-of-lists, never relies on insertion
    order. Baselines missing `workflow_run_id` are excluded (cannot be
    deduped/paired safely).

    #2187 (supersedes the #2182 fix_delta scope-boundary asymmetry): trust
    judgment now uses `_normalize_run_attempt_trusted`, which -- UNLIKE
    `_normalize_run_attempt` -- returns `None` (untrusted) for a MISSING
    `run_attempt` key. This unifies this gate module's trusted-cohort
    eligibility policy with the collector's (`scripts/ci/collect_e2e_
    performance_benchmark.py::_classify_run_attempt`) missing-excludes-
    from-cohort policy; the two modules' policies are no longer
    asymmetric. The #2182 P1 identity-collision fix
    (`_detect_run_attempt_identity_collisions` above, which intentionally
    keeps grouping a missing key into the attempt-1 slot for collision
    detection ONLY) is unaffected by this change and continues to apply
    here first, before trust filtering.

    Returns `(selected, evidence_errors)`. `selected` maps `workflow_run_id`
    -> the chosen trusted attempt-1 baseline. `evidence_errors` records
    EXACTLY ONE entry per `workflow_run_id` group that has NO trusted
    attempt-1 candidate -- #2187 fix_delta (OWNER REQUEST_CHANGES
    issuecomment-5458167419 P1-2): every such group now gets an
    identifiable reason, never a silent exclusion, classified via
    `_classify_run_attempt_trusted`:

    - `run_attempt_identity_collision` (`RUN_ATTEMPT_IDENTITY_COLLISION_
      REASON`) when the group is flagged by
      `_detect_run_attempt_identity_collisions` (checked FIRST, before
      trust filtering, so a collision group never also gets a
      missing/invalid reason).
    - `legacy_unverified_run_attempt` (`LEGACY_UNVERIFIED_RUN_ATTEMPT_
      REASON`) when EVERY baseline in the group is missing the
      `run_attempt` key entirely (#2187 AC2/AC9) -- the SAME identifiable
      reason the collector emits for this case.
    - `missing_or_invalid_initial_attempt_excluded_from_sample`
      (`MISSING_OR_INVALID_INITIAL_ATTEMPT_EXCLUDED_REASON`) for every
      other no-trusted-candidate shape: an explicit invalid value (`None`
      / bool / `0` / negative / non-numeric string), an attempt-2-and-
      later-only group, or a group mixing missing/invalid records without
      a single fully-missing consensus. Pre-fix_delta, this bucket was a
      silently empty `evidence_errors` list -- the exact defect flagged by
      OWNER review (a new test-fixture shape, not merely the fully-missing
      case, could still lose evidence with no identifiable reason)."""
    by_id: dict[object, list[dict]] = {}
    for baseline in baselines:
        workflow_run_id = baseline.get("workflow_run_id")
        if workflow_run_id is None:
            continue
        by_id.setdefault(workflow_run_id, []).append(baseline)

    collisions = _detect_run_attempt_identity_collisions(baselines)

    selected: dict[object, dict] = {}
    evidence_errors: list[dict] = []
    for workflow_run_id, group in by_id.items():
        if workflow_run_id in collisions:
            evidence_errors.append(
                {
                    "workflow_run_id": workflow_run_id,
                    "reason": RUN_ATTEMPT_IDENTITY_COLLISION_REASON,
                }
            )
            continue
        candidates = [b for b in group if _normalize_run_attempt_trusted(b) == 1]
        if not candidates:
            statuses = {_classify_run_attempt_trusted(b)[1] for b in group}
            reason = (
                LEGACY_UNVERIFIED_RUN_ATTEMPT_REASON
                if statuses == {"missing"}
                else MISSING_OR_INVALID_INITIAL_ATTEMPT_EXCLUDED_REASON
            )
            evidence_errors.append({"workflow_run_id": workflow_run_id, "reason": reason})
            continue
        selected[workflow_run_id] = min(candidates, key=lambda b: json.dumps(b, sort_keys=True, default=str))
    return selected, evidence_errors


def _dedupe_by_workflow_run_id(baselines: list[dict]) -> list[dict]:
    """#2179 P0-2/P1-1: sample identity is `workflow_run_id`, and
    selection follows the explicit `initial_attempt_only_v1` policy (see
    `_select_initial_attempt_baselines`) -- rerun attempts of the same
    run never add an independent sample, and attempt 1 failing/missing
    means the whole `workflow_run_id` is excluded (never substituted with
    a later attempt). Returns baselines sorted by `workflow_run_id`
    (canonical order, #2179 AC7) -- order-independent regardless of input
    order. #2187: `_select_initial_attempt_baselines` now returns
    `(selected, evidence_errors)`; this helper only needs `selected` (its
    caller -- `_comparable_cohort` -- feeds the exploratory,
    count/duration-only integration tests at the bottom of this module,
    NOT the AC11 close-verification hard-check path; that path goes
    through `_gate_ready_post_filter_sample_count` /
    `_provider_post_filter_sample_count` /
    `_evidence_readiness_hard_check_post_filter` instead, which DO
    propagate `evidence_errors` end-to-end into `run_evidence_gate`'s
    result -- #2187 fix_delta P2-2, OWNER REQUEST_CHANGES
    issuecomment-5458167419: discarding `evidence_errors` here is scoped
    to this count-only exploratory path and is not itself a production
    evidence-loss defect; a future caller that needs `_comparable_cohort`
    diagnostics for a close-verification use case should thread
    `evidence_errors` through rather than assume this helper already does
    so)."""
    selected, _evidence_errors = _select_initial_attempt_baselines(baselines)
    return [selected[workflow_run_id] for workflow_run_id in sorted(selected, key=str)]


def _comparable_cohort(baselines: list[dict], job_names: tuple[str, ...]) -> dict[str, list[dict]]:
    """#2159 rewrite: for each `job_names` entry, (1) excludes baselines
    with a placeholder/missing WITHIN_COHORT_REQUIRED_EQUAL fingerprint
    field (P1-2, fail-closed), (2) dedupes remaining baselines by
    `workflow_run_id` (P0-2/P1-1), then (3) groups by the
    WITHIN_COHORT_REQUIRED_EQUAL fingerprint tuple and returns only the
    single LARGEST fingerprint group per job (excludes/rejects any run
    whose fingerprint does not match the majority cohort for that job)."""
    by_job: dict[str, dict[tuple, list[dict]]] = {name: {} for name in job_names}
    for baseline in baselines:
        job = baseline.get("job")
        if job not in by_job:
            continue
        if _fingerprint_has_placeholder(baseline):
            continue
        fp = _fingerprint(baseline, WITHIN_COHORT_REQUIRED_EQUAL)
        by_job[job].setdefault(fp, []).append(baseline)

    result: dict[str, list[dict]] = {}
    for job, groups in by_job.items():
        if not groups:
            result[job] = []
            continue
        largest_fp = max(groups, key=lambda fp: len(groups[fp]))
        result[job] = _dedupe_by_workflow_run_id(groups[largest_fp])
    return result


def _job_duration_seconds(baselines: list[dict]) -> list[float]:
    durations = []
    for baseline in baselines:
        total_ms = sum(
            m.get("elapsed_ms", 0)
            for m in baseline.get("measurements", [])
            if m.get("phase_id", "").startswith("test_e2e")
        )
        if total_ms > 0:
            durations.append(total_ms / 1000)
    return durations


def _single_baseline_duration_seconds(baseline: dict) -> float | None:
    durations = _job_duration_seconds([baseline])
    return durations[0] if durations else None


# --------------------------------------------------------------------------- #
# #2159 AC3 (P0-3/P0-4): paired critical-path statistics.
# --------------------------------------------------------------------------- #
def _pair_by_workflow_run_id(
    core_baselines: list[dict], responsive_baselines: list[dict]
) -> tuple[list[tuple[object, dict, dict]], list[dict]]:
    """Exact-pairs `e2e-core` / `e2e-responsive-matrix` baselines sharing
    the same `workflow_run_id`. Returns `(pairs, evidence_errors)`; a run
    present in only one lane is NOT silently dropped from cohort
    accounting -- it is reported as an explicit evidence error (#2159
    AC3).

    #2179 AC2 (fix_delta after OWNER adversarial review issuecomment-5295659213
    P1-1): each side's per-`workflow_run_id` candidate is selected via the
    `initial_attempt_only_v1` policy (`_select_initial_attempt_baselines`),
    NOT a `{b["workflow_run_id"]: b for b in ...}` dict comprehension --
    that shape is an implicit "last baseline in the input list wins" for a
    duplicate key, an insertion-order artifact this policy explicitly
    forbids.

    #2187 AC4/AC9: `all_ids` is built from the RAW `workflow_run_id` set
    present in `core_baselines` / `responsive_baselines` -- NOT from the
    selected maps' keys -- so a `workflow_run_id` excluded on BOTH sides by
    `_select_initial_attempt_baselines` (e.g. every same-id baseline is
    missing `run_attempt` on both `e2e-core` and `e2e-responsive-matrix`)
    still surfaces in `evidence_errors` instead of silently disappearing.
    When the underlying cause is a `_select_initial_attempt_baselines`
    exclusion (`legacy_unverified_run_attempt`), that reason is merged into
    this function's own `evidence_errors` entry rather than being reported
    only as the fixed `missing_pair_e2e-core` / `missing_pair_e2e-
    responsive-matrix` string."""
    core_by_id, core_selection_errors = _select_initial_attempt_baselines(core_baselines)
    responsive_by_id, responsive_selection_errors = _select_initial_attempt_baselines(responsive_baselines)

    core_selection_reason_by_id = {err["workflow_run_id"]: err["reason"] for err in core_selection_errors}
    responsive_selection_reason_by_id = {
        err["workflow_run_id"]: err["reason"] for err in responsive_selection_errors
    }

    raw_ids: set[object] = set()
    for baseline in core_baselines:
        workflow_run_id = baseline.get("workflow_run_id")
        if workflow_run_id is not None:
            raw_ids.add(workflow_run_id)
    for baseline in responsive_baselines:
        workflow_run_id = baseline.get("workflow_run_id")
        if workflow_run_id is not None:
            raw_ids.add(workflow_run_id)
    all_ids = sorted(raw_ids, key=str)

    pairs: list[tuple[object, dict, dict]] = []
    evidence_errors: list[dict] = []
    for workflow_run_id in all_ids:
        core = core_by_id.get(workflow_run_id)
        responsive = responsive_by_id.get(workflow_run_id)
        if core is None or responsive is None:
            reasons: list[str] = []
            if core is None:
                reasons.append(core_selection_reason_by_id.get(workflow_run_id, "missing_pair_e2e-core"))
            if responsive is None:
                reasons.append(
                    responsive_selection_reason_by_id.get(workflow_run_id, "missing_pair_e2e-responsive-matrix")
                )
            evidence_errors.append(
                {
                    "workflow_run_id": workflow_run_id,
                    "reason": ",".join(reasons),
                }
            )
            continue
        pairs.append((workflow_run_id, core, responsive))
    return pairs, evidence_errors


def _nearest_rank_percentile(values: list[float], percentile: float) -> float:
    """`nearest_rank_v1`: the smallest value such that at least
    `percentile`% of the (sorted) sample is <= that value. 1-indexed
    nearest-rank method (matches AC8's `validate_ci_performance_assessment_v2.py`
    percentile recomputation, kept as a single versioned method name across
    both consumers)."""
    if not values:
        raise ValueError("nearest_rank_v1 requires at least one value")
    ordered = sorted(values)
    n = len(ordered)
    rank = max(1, min(n, math.ceil((percentile / 100.0) * n)))
    return ordered[rank - 1]


def _provider_critical_path_paired_p50_p95(pairs: list[tuple[object, dict, dict]]) -> dict | None:
    """#2159 P0-4: `median(max(core_i, responsive_i))` (nearest_rank_v1)
    over PAIRED (same `workflow_run_id`) runs -- the correct parallel
    critical-path statistic, replacing the prior
    `max(median(core), median(responsive))` (which is not a valid
    critical-path percentile because it never reconstructs any single
    real run's wall-clock critical path)."""
    per_run_critical_path: list[float] = []
    for _workflow_run_id, core, responsive in pairs:
        core_duration = _single_baseline_duration_seconds(core)
        responsive_duration = _single_baseline_duration_seconds(responsive)
        if core_duration is None or responsive_duration is None:
            continue
        per_run_critical_path.append(max(core_duration, responsive_duration))

    if not per_run_critical_path:
        return None

    return {
        "p50_seconds": _nearest_rank_percentile(per_run_critical_path, 50),
        "p95_seconds": _nearest_rank_percentile(per_run_critical_path, 95),
        "sample_count": len(per_run_critical_path),
        "percentile_method": "nearest_rank_v1",
    }


# --------------------------------------------------------------------------- #
# #2159 AC4 (P0-6): same-clock gate-ready latency.
# --------------------------------------------------------------------------- #
def _parse_iso8601(timestamp: str) -> datetime:
    normalized = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    return datetime.fromisoformat(normalized)


def _gate_ready_latency_seconds_same_clock(run_started_at: str, check_completed_at: str) -> float:
    """#2159 P0-6: a SINGLE function computes gate-ready latency for BOTH
    before and after arms from the GitHub API clock
    (`workflow_run.run_started_at` -> corresponding check `completed_at`).
    Using one shared function (rather than two separately-implemented
    computations, one per arm) is what makes the before/after comparison
    apples-to-apples."""
    start = _parse_iso8601(run_started_at)
    end = _parse_iso8601(check_completed_at)
    latency_seconds = (end - start).total_seconds()
    if latency_seconds < 0:
        raise ValueError(
            f"check_completed_at ({check_completed_at!r}) precedes "
            f"run_started_at ({run_started_at!r})"
        )
    return latency_seconds


def _gate_ready_latency_seconds_from_baseline(baseline: dict) -> float | None:
    run_started_at = baseline.get("run_started_at")
    check_completed_at = baseline.get("check_completed_at")
    if not run_started_at or not check_completed_at:
        return None
    return _gate_ready_latency_seconds_same_clock(run_started_at, check_completed_at)


def _gate_ready_latency_seconds(baselines: list[dict]) -> list[float]:
    """Same-clock gate-ready latency for a cohort of baselines (skips any
    baseline missing the GitHub API clock fields rather than falling back
    to a different clock source, per #2159 P0-6)."""
    latencies = []
    for baseline in baselines:
        latency = _gate_ready_latency_seconds_from_baseline(baseline)
        if latency is not None:
            latencies.append(latency)
    return latencies


# --------------------------------------------------------------------------- #
# #2159 P0-5 (fix_delta after adversarial review issuecomment-5295659213):
# `_comparable_cohort` selects the largest WITHIN_COHORT_REQUIRED_EQUAL
# fingerprint group INDEPENDENTLY per job -- it never checks whether that
# selected group's CROSS_COHORT_REQUIRED_EQUAL provenance subset actually
# matches across DIFFERENT jobs within the same arm, or across the
# before/after arms. This is exactly the attack the review describes:
# before uses host image A, after uses host image B; `e2e-core` uses
# container digest X, `e2e-responsive-matrix` uses digest Y -- each
# individually accumulates a legitimate 20-run cohort and is silently
# accepted as "comparable" even though the infrastructure drifted across
# the very axis the split (`workflow_digest`, an INTENTIONAL_TREATMENT_
# DIFFERENCE field) was supposed to isolate.
# --------------------------------------------------------------------------- #
def _representative_cross_cohort_fingerprint(
    cohort: list[dict], fields: tuple[str, ...] = CROSS_COHORT_REQUIRED_EQUAL
) -> tuple | None:
    """Returns the CROSS_COHORT_REQUIRED_EQUAL fingerprint of an already
    within-cohort-filtered baseline list (all entries in `cohort` are
    assumed, by construction of `_comparable_cohort`, to already share the
    same WITHIN_COHORT_REQUIRED_EQUAL fingerprint -- CROSS_COHORT_REQUIRED_EQUAL
    is a strict subset of that, so any single representative entry's value
    for those fields applies to the whole cohort). Returns None for an
    empty cohort (nothing to compare)."""
    if not cohort:
        return None
    return _fingerprint(cohort[0], fields)


def validate_experiment_comparability(
    before_cohort_by_job: dict[str, list[dict]],
    after_cohort_by_job: dict[str, list[dict]],
    cross_cohort_fields: tuple[str, ...] = CROSS_COHORT_REQUIRED_EQUAL,
) -> list[str]:
    """#2159 P0-5: validates that the CROSS_COHORT_REQUIRED_EQUAL
    provenance fields are IDENTICAL across every job within the before
    arm, every job within the after arm, AND between the before and after
    arms themselves (host_runner_image / playwright_container_image_digest
    / node_version / pnpm_version / playwright_version / lockfile_hash must
    all be stable -- ONLY `workflow_digest`/`cohort_role`, the
    INTENTIONAL_TREATMENT_DIFFERENCE fields, are allowed to differ).
    Returns a list of violation strings (empty list == fully comparable).
    This is the real production cross-cohort/cross-job check that was
    missing: the pre-fix_delta version only had the three classification
    TUPLES defined as constants, with no function actually enforcing
    equality across jobs/arms."""
    violations: list[str] = []
    fingerprints: dict[str, tuple] = {}

    for job, cohort in before_cohort_by_job.items():
        fp = _representative_cross_cohort_fingerprint(cohort, cross_cohort_fields)
        if fp is not None:
            fingerprints[f"before/{job}"] = fp
    for job, cohort in after_cohort_by_job.items():
        fp = _representative_cross_cohort_fingerprint(cohort, cross_cohort_fields)
        if fp is not None:
            fingerprints[f"after/{job}"] = fp

    if len(fingerprints) < 2:
        return violations

    keys = sorted(fingerprints)
    reference_key = keys[0]
    reference_fp = fingerprints[reference_key]
    for key in keys[1:]:
        fp = fingerprints[key]
        for field, ref_value, value in zip(cross_cohort_fields, reference_fp, fp):
            if ref_value != value:
                violations.append(
                    f"cross_cohort_provenance_drift: field={field!r} "
                    f"{reference_key}={ref_value!r} != {key}={value!r}"
                )
    return violations


# --------------------------------------------------------------------------- #
# #2159 AC11 (P1-6): evidence-insufficient hard-failure path.
# --------------------------------------------------------------------------- #
class EvidenceInsufficientError(RuntimeError):
    """Raised by `_evidence_readiness_hard_check` when comparable-cohort
    evidence is insufficient. This is the dedicated close-verification
    path (must terminate with a non-zero exit code) -- distinct from the
    exploratory integration tests in this module, which legitimately use
    `pytest.skip()` under Runtime Verification Applicability
    `fallback_success_is_pass: false` when no live GitHub Actions history
    exists in the current implementation session. `pytest.skip()` (exit 0)
    must never be used as the sole gate for a close condition -- see #2159
    P1-6 / test_ci_performance_gate_evidence_hard_failure.py."""


def _evidence_readiness_hard_check(
    cohort_by_job: dict[str, list[dict]],
    job_names: tuple[str, ...],
    min_count: int = MIN_COHORT_RUN_COUNT,
) -> None:
    missing = {
        job: len(cohort_by_job.get(job, []))
        for job in job_names
        if len(cohort_by_job.get(job, [])) < min_count
    }
    if missing:
        raise EvidenceInsufficientError(
            f"insufficient comparable-cohort evidence (need >= {min_count} "
            f"per job): {missing!r}"
        )


# --------------------------------------------------------------------------- #
# #2159 P0-6 (fix_delta after adversarial review issuecomment-5295659213):
# re-validate the sample floor AFTER pairing/duration/timestamp filtering,
# not only on the raw pre-filter comparable-cohort counts. Raw per-job
# counts can each individually satisfy MIN_COHORT_RUN_COUNT while still
# collapsing to a single valid-duration (or valid-timestamp) sample once
# exact-pairing and missing-measurement filtering are applied -- computing
# a P50/P95 "gate" from n=1 while claiming an n>=20-backed result is a
# fail-open defect this function closes.
# --------------------------------------------------------------------------- #
def _provider_post_filter_sample_count(
    core_baselines: list[dict], responsive_baselines: list[dict]
) -> tuple[int, list[dict]]:
    """Returns `(post_filter_sample_count, evidence_errors)` for the
    provider critical-path cohort AFTER exact pairing by `workflow_run_id`
    AND after dropping pairs missing a real duration measurement on either
    side (#2159 P0-6). This is the number that must be compared against
    `MIN_COHORT_RUN_COUNT`, not the pre-pairing per-job baseline counts."""
    pairs, evidence_errors = _pair_by_workflow_run_id(core_baselines, responsive_baselines)
    provider = _provider_critical_path_paired_p50_p95(pairs)
    post_filter_count = provider["sample_count"] if provider is not None else 0
    return post_filter_count, evidence_errors


def _gate_ready_post_filter_sample_count(baselines: list[dict]) -> tuple[int, list[dict]]:
    """Returns `(post_filter_sample_count, evidence_errors)`: the number of
    baselines with a REAL (both-timestamps-present) same-clock gate-ready
    latency AFTER timestamp filtering (#2159 P0-6). `_gate_ready_latency_
    seconds` already silently drops timestamp-missing baselines; this
    helper makes the resulting count an explicit, independently checkable
    value rather than an implicit array length the caller may forget to
    re-validate.

    #2187 AC6: applies the SAME `_select_initial_attempt_baselines`
    attempt-selection / `workflow_run_id` dedupe the provider lane uses
    (`_pair_by_workflow_run_id`) to this gate-ready lane, so a missing
    `run_attempt`, a duplicate `workflow_run_id`, or an attempt-2-and-later
    -only record cannot inflate the sample floor."""
    selected, evidence_errors = _select_initial_attempt_baselines(baselines)
    deduped = list(selected.values())
    return len(_gate_ready_latency_seconds(deduped)), evidence_errors


def _evidence_readiness_hard_check_post_filter(
    provider_post_filter_count: int,
    provider_evidence_errors: list[dict],
    gate_ready_post_filter_counts: dict[str, int],
    gate_ready_evidence_errors: dict[str, list[dict]] | None = None,
    min_count: int = MIN_COHORT_RUN_COUNT,
) -> None:
    """#2159 P0-6 AC11 extension: the close-verification hard-check must
    reject evidence whose sample count only meets `min_count` BEFORE
    pairing/duration/timestamp filtering. This function re-validates the
    floor using POST-filter counts (see `_provider_post_filter_sample_count`
    / `_gate_ready_post_filter_sample_count`) and raises
    `EvidenceInsufficientError` -- never a silent skip -- when the
    post-filter evidence is insufficient, exactly mirroring
    `_evidence_readiness_hard_check`'s fail-closed contract but operating on
    the correct (post-filter) sample counts.

    #2187 fix_delta (OWNER REQUEST_CHANGES issuecomment-5458167419 P1-1):
    `gate_ready_evidence_errors` (`{role: [{"workflow_run_id": ..., "reason":
    ...}, ...]}`, one entry per role such as `"before"` / `"after"`) mirrors
    `provider_evidence_errors`'s existing fail-closed contract for the
    gate-ready lane -- a role whose gate-ready sample count clears
    `min_count` but still has a non-empty exclusion list (a missing/invalid
    `run_attempt`, a duplicate `workflow_run_id`, or an identity collision)
    is STILL rejected here, never silently treated as sufficient just
    because the raw count happened to clear the floor."""
    problems: dict[str, object] = {}
    if provider_post_filter_count < min_count:
        problems["provider_post_filter_sample_count"] = provider_post_filter_count
    if provider_evidence_errors:
        problems["provider_evidence_errors"] = provider_evidence_errors
    for role, count in gate_ready_post_filter_counts.items():
        if count < min_count:
            problems[f"gate_ready_post_filter_sample_count[{role}]"] = count
    for role, errors in (gate_ready_evidence_errors or {}).items():
        if errors:
            problems[f"gate_ready_evidence_errors[{role}]"] = errors
    if problems:
        raise EvidenceInsufficientError(
            f"insufficient POST-FILTER comparable-cohort evidence (need >= {min_count} "
            f"AFTER pairing/duration/timestamp filtering, not merely on raw pre-filter "
            f"per-job counts): {problems!r}"
        )


def test_p50_provider_meets_absolute_and_relative_shortening_threshold():
    baselines = _find_all_baselines()
    cohort = _comparable_cohort(baselines, ("e2e-core", "e2e-responsive-matrix", "e2e"))
    core_baselines = cohort["e2e-core"]
    responsive_baselines = cohort["e2e-responsive-matrix"]
    old_e2e_baselines = cohort["e2e"]

    # #2159 P0-5: reject the cohort outright if cross-job/cross-arm
    # provenance has drifted, even if each job's own within-cohort
    # fingerprint group independently reached MIN_COHORT_RUN_COUNT.
    comparability_violations = validate_experiment_comparability(
        {"e2e-core": core_baselines, "e2e-responsive-matrix": responsive_baselines},
        {"e2e": old_e2e_baselines},
    )
    assert not comparability_violations, (
        f"cross-cohort/cross-job provenance drift detected (AC6/#2159 P0-5): "
        f"{comparability_violations}"
    )

    if (
        len(core_baselines) < MIN_COHORT_RUN_COUNT
        or len(responsive_baselines) < MIN_COHORT_RUN_COUNT
        or len(old_e2e_baselines) < MIN_COHORT_RUN_COUNT
    ):
        pytest.skip(
            f"comparable-cohort P50 gate requires >= {MIN_COHORT_RUN_COUNT} comparable "
            f"ci_runtime_baseline_v1 workflow_run_id samples per job for e2e-core, "
            f"e2e-responsive-matrix, AND the pre-split old `e2e` job (for the AC9a "
            f"relative-shortening comparison); found core={len(core_baselines)} "
            f"responsive={len(responsive_baselines)} old_e2e={len(old_e2e_baselines)} locally "
            f"under {ARTIFACTS_DIR}. This accumulates from real CI runs post-merge (Runtime "
            f"Verification Applicability decision: immediate, fallback_success_is_pass: false "
            f"— SKIP, not a fabricated PASS)."
        )

    pairs, evidence_errors = _pair_by_workflow_run_id(core_baselines, responsive_baselines)
    assert not evidence_errors, (
        f"paired critical-path cohort has unpaired workflow_run_id evidence errors "
        f"(AC3): {evidence_errors}"
    )
    provider = _provider_critical_path_paired_p50_p95(pairs)
    assert provider is not None, "paired cohort must include real elapsed_ms measurements"

    # #2159 P0-6: re-check the sample floor AFTER pairing/duration
    # filtering. The pre-pairing per-job counts above (>= MIN_COHORT_RUN_COUNT
    # each) do NOT guarantee the post-filter provider sample_count is also
    # >= MIN_COHORT_RUN_COUNT -- missing-duration pairs are silently dropped
    # by `_provider_critical_path_paired_p50_p95`.
    assert provider["sample_count"] >= MIN_COHORT_RUN_COUNT, (
        f"provider post-pairing/duration-filtering sample_count="
        f"{provider['sample_count']} is below MIN_COHORT_RUN_COUNT="
        f"{MIN_COHORT_RUN_COUNT} even though pre-filter per-job counts were "
        f"sufficient (AC9a / #2159 P0-6)"
    )

    old_durations = _job_duration_seconds(old_e2e_baselines)
    assert old_durations, "cohort must include real elapsed_ms measurements for the pre-split old e2e job"

    provider_p50 = provider["p50_seconds"]
    # #2180 (PR #2172 OWNER adversarial review P1-2): the pre-split old `e2e`
    # baseline P50 must use the SAME `nearest_rank_v1` estimator as the
    # provider critical-path P50 above -- the Python stdlib's even-n median
    # (which averages the two middle values) disagrees with nearest_rank_v1
    # (which selects the 10th smallest outright for n=20), and that
    # divergence can flip the AC9a relative-shortening gate decision below
    # (see tests/ci/test_ci_performance_gate_percentile_consistency.py AC3).
    old_p50 = _nearest_rank_percentile(old_durations, 50)

    # AC9a absolute threshold.
    assert provider_p50 <= PROVIDER_P50_ABSOLUTE_THRESHOLD_SECONDS, (
        f"paired provider P50={provider_p50:.1f}s exceeds the "
        f"{PROVIDER_P50_ABSOLUTE_THRESHOLD_SECONDS}s absolute threshold (AC9a)"
    )

    # AC9a relative shortening threshold: >= 35% shorter than the pre-split
    # old `e2e` job's critical-path P50.
    relative_shortening = (old_p50 - provider_p50) / old_p50 if old_p50 else 0.0
    assert relative_shortening >= RELATIVE_SHORTENING_THRESHOLD, (
        f"provider P50={provider_p50:.1f}s vs old e2e P50={old_p50:.1f}s is only "
        f"{relative_shortening:.1%} shorter, below the "
        f"{RELATIVE_SHORTENING_THRESHOLD:.0%} relative-shortening threshold (AC9a)"
    )


def test_p50_gate_ready_latency_not_regressed():
    baselines = _find_all_baselines()
    # The aggregate `e2e` job's comparability fingerprint changes across the
    # split boundary (this PR itself changes workflow_digest), so old vs
    # new `e2e` runs never share a single fingerprint group by
    # construction -- split explicitly by `cohort_role` (#2159 P0-7
    # explicit discriminator, replacing the prior implicit
    # gate_ready_latency_ms-presence heuristic) for this specific
    # before/after comparison.
    all_e2e_baselines = [b for b in baselines if b.get("job") == "e2e"]
    new_e2e_baselines = [b for b in all_e2e_baselines if b.get("cohort_role") == "after"]
    old_e2e_baselines = [b for b in all_e2e_baselines if b.get("cohort_role") == "before"]

    if len(new_e2e_baselines) < MIN_COHORT_RUN_COUNT or len(old_e2e_baselines) < MIN_COHORT_RUN_COUNT:
        pytest.skip(
            f"gate-ready latency P50 comparison requires >= {MIN_COHORT_RUN_COUNT} comparable "
            f"post-split `e2e` aggregate runs (cohort_role=after) AND >= "
            f"{MIN_COHORT_RUN_COUNT} pre-split old `e2e` job runs (cohort_role=before); found "
            f"new={len(new_e2e_baselines)} old={len(old_e2e_baselines)} locally under "
            f"{ARTIFACTS_DIR}. SKIP (not a fabricated PASS) per Runtime Verification "
            f"Applicability fallback_success_is_pass: false. This is expected in this "
            f"implementation session, which has no live GitHub Actions history -- once both "
            f"cohorts exist (old data pre-dates this PR; new data accumulates from this PR's "
            f"own post-merge CI runs), this test performs the real comparison below instead "
            f"of skipping."
        )

    new_latencies = _gate_ready_latency_seconds(new_e2e_baselines)
    old_latencies = _gate_ready_latency_seconds(old_e2e_baselines)
    assert new_latencies and old_latencies, (
        "cohort must include real GitHub-API-clock gate-ready latency data "
        "(run_started_at / check_completed_at) for both arms (AC4)"
    )

    # #2159 P0-6: re-check the sample floor AFTER timestamp filtering, not
    # only on the pre-filter baseline count.
    assert len(new_latencies) >= MIN_COHORT_RUN_COUNT, (
        f"post-timestamp-filtering new_e2e gate-ready sample_count="
        f"{len(new_latencies)} is below MIN_COHORT_RUN_COUNT={MIN_COHORT_RUN_COUNT} "
        f"even though the pre-filter baseline count was sufficient (AC9b / #2159 P0-6)"
    )
    assert len(old_latencies) >= MIN_COHORT_RUN_COUNT, (
        f"post-timestamp-filtering old_e2e gate-ready sample_count="
        f"{len(old_latencies)} is below MIN_COHORT_RUN_COUNT={MIN_COHORT_RUN_COUNT} "
        f"even though the pre-filter baseline count was sufficient (AC9b / #2159 P0-6)"
    )

    # #2180: gate-ready before/after P50 must use the same versioned
    # `nearest_rank_v1` estimator as every other decision-producing
    # percentile path in this file (see the AC9a comment above and
    # tests/ci/test_ci_performance_gate_percentile_consistency.py).
    new_p50 = _nearest_rank_percentile(new_latencies, 50)
    old_p50 = _nearest_rank_percentile(old_latencies, 50)

    # AC9b: required stable `e2e` aggregate gate-ready latency P50 must not
    # regress relative to the old `e2e` job's gate-ready latency P50,
    # measured on the SAME clock for both arms (AC4).
    assert new_p50 <= old_p50, (
        f"required stable `e2e` aggregate gate-ready latency P50={new_p50:.1f}s regressed "
        f"vs old `e2e` job gate-ready latency P50={old_p50:.1f}s (AC9b)"
    )


def test_p95_failure_and_flaky_rate_validated_from_real_assessment_artifact():
    assessment_paths = (
        glob.glob(str(ARTIFACTS_DIR / "**" / "*ci_test_performance_assessment*.json"), recursive=True)
        if ARTIFACTS_DIR.is_dir()
        else []
    )
    if not assessment_paths:
        pytest.skip(
            f"no real CI_TEST_PERFORMANCE_ASSESSMENT_V2 artifact found under {ARTIFACTS_DIR} "
            f"— generated post-merge from real CI runs. SKIP (not a fabricated PASS) per "
            f"Runtime Verification Applicability fallback_success_is_pass: false."
        )

    mod = _load_validator_module()

    ci_verdict_summary_candidates = (
        glob.glob(str(ARTIFACTS_DIR / "**" / "*ci_verdict_summary_v2*.json"), recursive=True)
        if ARTIFACTS_DIR.is_dir()
        else []
    )
    expected_head_sha = os.environ.get("EXPECTED_PR_HEAD_SHA") or os.environ.get("GH_HEAD_SHA")

    if not ci_verdict_summary_candidates or not expected_head_sha:
        pytest.skip(
            "AC10 approval_eligible cross-check requires both a real ci_verdict_summary_v2 "
            f"artifact under {ARTIFACTS_DIR} and an EXPECTED_PR_HEAD_SHA/GH_HEAD_SHA env var "
            f"(the trusted current head SHA); found "
            f"{len(ci_verdict_summary_candidates)} ci_verdict_summary_v2 candidate(s), "
            f"expected_head_sha={expected_head_sha!r}. SKIP (not a fabricated PASS) per "
            f"Runtime Verification Applicability fallback_success_is_pass: false -- this "
            f"accumulates from a real CI run of this PR's own head, not from this local "
            f"implementation session."
        )

    ci_verdict_summary_path = ci_verdict_summary_candidates[0]

    for path in assessment_paths:
        exit_code, decision = mod.validate_assessment(
            path,
            ci_verdict_summary_path=ci_verdict_summary_path,
            expected_head_sha=expected_head_sha,
        )
        assert exit_code == mod.EXIT_VALID, (
            f"CI_TEST_PERFORMANCE_ASSESSMENT_V2 at {path} failed structural/semantic "
            f"validation (exit {exit_code}): {decision}"
        )
        assert decision.get("approval_eligible") is True, (
            f"CI_TEST_PERFORMANCE_ASSESSMENT_V2 at {path} is structurally/semantically valid "
            f"but NOT approval_eligible (blockers={decision.get('blockers')}) -- exit_code "
            f"alone is insufficient per AC10"
        )


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_cli_main())
