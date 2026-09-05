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
import hashlib
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


def run_evidence_gate(
    fixture: dict,
    *,
    ci_verdict_summary_path: str | None = None,
    expected_head_sha: str | None = None,
    expected_summary_file_sha256: str | None = None,
) -> dict:
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
    reason are never silently dropped from the production result.

    #2423 AC4 fix_delta (OWNER controlled-reframe issuecomment-5539310075):
    `ci_verdict_summary_path` / `expected_head_sha` /
    `expected_summary_file_sha256` are now threaded through to the
    validator's `validate_assessment()` call (previously this function
    called `validate_assessment(assessment_tmp_path)` with NO trusted
    binding arguments at all -- a real gap: the built assessment's
    `functional_evidence.ci_verdict_summary_ref` self-report was never
    cross-checked against a canonical `ci_verdict_summary_v2` artifact, so
    `approval_eligible` could never become `True` through this path, and
    conversely nothing here previously enforced that a caller supply a
    real trusted artifact at all). `expected_summary_file_sha256` maps onto
    the validator's `--expected-artifact-digest` parameter, which is the
    SHA-256 of the `ci_verdict_summary_v2` JSON FILE's own bytes -- see
    `docs/dev/e2e-performance-benchmark.md`'s digest-naming-distinction
    section for why this is never conflated with a GitHub Actions
    artifact-bundle-level digest (`github_artifact_digest` in the AC3
    receipt, which this function does not compute or validate)."""
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

    # #2423 fix_delta P0-2 (OWNER REQUEST_CHANGES issuecomment-5540705404):
    # a raw core/responsive/gate-ready record entirely missing
    # `workflow_run_id` is invisible to every `workflow_run_id`-keyed
    # bookkeeping path above -- see `_missing_workflow_run_id_raw_record_
    # count`'s own docstring. Fail-close the whole arm when this happens,
    # rather than letting the record silently vanish from both the sample
    # count and every evidence_errors list.
    before_raw_missing_workflow_run_id_count = _missing_workflow_run_id_raw_record_count(
        before_core, before_responsive, before_gate_ready
    )
    after_raw_missing_workflow_run_id_count = _missing_workflow_run_id_raw_record_count(
        after_core, after_responsive, after_gate_ready
    )

    try:
        _evidence_readiness_hard_check_post_filter(
            before_provider_count,
            before_evidence_errors,
            {"before": before_gate_ready_count},
            gate_ready_evidence_errors={"before": before_gate_ready_evidence_errors},
            raw_missing_workflow_run_id_counts={"before": before_raw_missing_workflow_run_id_count},
        )
        _evidence_readiness_hard_check_post_filter(
            after_provider_count,
            after_evidence_errors,
            {"after": after_gate_ready_count},
            gate_ready_evidence_errors={"after": after_gate_ready_evidence_errors},
            raw_missing_workflow_run_id_counts={"after": after_raw_missing_workflow_run_id_count},
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
        exit_code, decision = validator.validate_assessment(
            assessment_tmp_path,
            ci_verdict_summary_path=ci_verdict_summary_path,
            expected_head_sha=expected_head_sha,
            expected_artifact_digest=expected_summary_file_sha256,
        )
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
    `e2e-performance-benchmark-assessment-gate` steps (#2159 OWNER
    scope-authority ruling issuecomment-5299412215, items 2+3; exit-code
    semantics tightened by #2423 AC4). Exit codes:
    0 = gate_status complete AND semantic_valid AND approval_eligible;
    1 = insufficient_evidence (AC11 fail-closed -- the intended, CORRECT
        outcome until a real >= 20-run cohort exists per-arm, per OWNER:
        "20件未満なら fail-closed する production path");
    2 = complete evidence but the built assessment failed structural/semantic
        validation (defensive fail-closed; not expected given this module's
        own P0-7 hardening, see
        test_validate_ci_performance_assessment_v2_build_from_cohorts.py);
    3 = complete evidence AND semantic_valid, but NOT approval_eligible
        (#2423 AC4: previously this function only checked `semantic_valid`
        at this branch point and returned 0 regardless of
        `approval_eligible` -- a real gap, since a semantically valid
        assessment can still be `approval_eligible: false` e.g. because no
        trusted `ci_verdict_summary_v2` artifact was bound at all. A
        close-grade CLI that exits 0 without independently confirming
        `approval_eligible` is a false-green risk this AC closes);
    4 = --production-invocation was passed but --manifest-sha256 and/or
        --experiment-identity was omitted (#2422 AC10: the production
        invocation route must never reach the --cohort-fixture-file-sha256
        fallback -- this is checked and fails closed BEFORE any gate/receipt
        computation runs)."""
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "#2159 items 2+3 production gate: fail-closed AC11 evidence "
            "readiness check + P0-8 real CI_TEST_PERFORMANCE_ASSESSMENT_V2 "
            "producer, wired from a single cohort fixture. #2423 AC3/AC4/AC5: "
            "requires complete && semantic_valid && approval_eligible for "
            "exit 0, and can additionally emit the CI_PERFORMANCE_CLOSE_GRADE_"
            "RESULT_V1 receipt #2424 consumes."
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
            '"after": {...same shape...}}. #2423: this fixture\'s "before"/'
            '"after" arms are consumed as the root run set stand-in for the '
            'AC3 receipt\'s arms.monolith / arms.split respectively, until a '
            "real #2422 immutable manifest reference supersedes it."
        ),
    )
    parser.add_argument("--output", required=True, help="Path to write the gate result JSON")
    parser.add_argument(
        "--ci-verdict-summary",
        default=None,
        help=(
            "#2423 AC4: path to the trusted ci_verdict_summary_v2 artifact "
            "JSON, forwarded to validate_ci_performance_assessment_v2.py's "
            "--ci-verdict-summary. Required (together with --expected-head-sha) "
            "for approval_eligible=true, hence for exit code 0."
        ),
    )
    parser.add_argument(
        "--expected-head-sha",
        default=None,
        help="#2423 AC4: trusted PR head SHA, forwarded to the validator's --expected-head-sha.",
    )
    parser.add_argument(
        "--expected-ci-verdict-summary-file-sha256",
        default=None,
        help=(
            "#2423 AC4: expected sha256:<hex> digest of the --ci-verdict-summary "
            "FILE's own bytes (forwarded to the validator's --expected-artifact-digest). "
            "Distinct from --github-artifact-digest, which is the separate GitHub "
            "Actions upload-artifact bundle-level digest -- never conflated (see "
            "docs/dev/e2e-performance-benchmark.md)."
        ),
    )
    parser.add_argument(
        "--ci-verdict-summary-artifact-id",
        default=None,
        help=(
            "#2423 AC3/AC5: GitHub Actions artifact ID the --ci-verdict-summary "
            "file was downloaded from (receipt provenance only; not re-verified "
            "by the validator)."
        ),
    )
    parser.add_argument(
        "--github-artifact-digest",
        default=None,
        help=(
            "#2423 AC3/AC4: GitHub Actions upload-artifact bundle-level digest "
            "for the artifact --ci-verdict-summary-artifact-id points at (receipt "
            "provenance field trusted_functional_evidence.github_artifact_digest; "
            "distinct from --expected-ci-verdict-summary-file-sha256, the sha256 "
            "of the summary JSON file's own bytes)."
        ),
    )
    parser.add_argument(
        "--experiment-identity",
        default=None,
        help=(
            "#2423 AC3: stable identity string for the CI_PERFORMANCE_CLOSE_GRADE_"
            "RESULT_V1 receipt; derived from the fixture when omitted."
        ),
    )
    parser.add_argument(
        "--manifest-sha256",
        default=None,
        help=(
            "#2423 AC3: sha256:<hex> of the #2422 immutable dispatch root run "
            "set manifest this fixture was materialized from. Falls back to "
            "the --cohort-fixture file's own sha256 when omitted -- but ONLY "
            "outside --production-invocation (#2422 AC10); see that flag's "
            "help text."
        ),
    )
    parser.add_argument(
        "--receipt-output",
        default=None,
        help="#2423 AC3: optional path to additionally write the CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1 receipt JSON.",
    )
    parser.add_argument(
        "--production-invocation",
        action="store_true",
        help=(
            "Issue #2422 AC10: marks this invocation as the REAL production "
            "route (e.g. from .github/workflows/ci.yml), where the "
            "--manifest-sha256/--experiment-identity fallback-to-fixture-"
            "sha256 behavior below is STRUCTURALLY UNREACHABLE -- omitting "
            "--manifest-sha256 or --experiment-identity under this flag is a "
            "hard, fail-closed error (before any receipt/gate computation "
            "runs), never a silent fallback to the --cohort-fixture file's "
            "own sha256. Without this flag (the default), the fixture-sha256 "
            "fallback remains available exactly as before, for unit / "
            "fixture / exploratory-smoke invocations only."
        ),
    )
    args = parser.parse_args(argv)

    if args.production_invocation:
        missing = [
            name
            for name, value in (
                ("--manifest-sha256", args.manifest_sha256),
                ("--experiment-identity", args.experiment_identity),
            )
            if not value
        ]
        if missing:
            print(
                "::error::--production-invocation requires "
                f"{', '.join(missing)} as authoritative trusted input(s); "
                "the --cohort-fixture-file-sha256 fallback is unreachable "
                "from the production invocation route (#2422 AC10)."
            )
            return 4

    with open(args.cohort_fixture, encoding="utf-8") as handle:
        fixture_text = handle.read()
    fixture = json.loads(fixture_text)

    result = run_evidence_gate(
        fixture,
        ci_verdict_summary_path=args.ci_verdict_summary,
        expected_head_sha=args.expected_head_sha,
        expected_summary_file_sha256=args.expected_ci_verdict_summary_file_sha256,
    )

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")

    validation_decision = result.get("validation_result") or {}
    semantic_valid = bool(validation_decision.get("semantic_valid"))
    approval_eligible = bool(validation_decision.get("approval_eligible"))

    if result["gate_status"] == "insufficient_evidence":
        print(f"::error::CI_PERFORMANCE_BENCHMARK_EVIDENCE_GATE_RESULT_V1 insufficient_evidence: {result['reason']}")
        exit_code = 1
    elif not semantic_valid:
        print("::error::CI_PERFORMANCE_BENCHMARK_EVIDENCE_GATE_RESULT_V1 built assessment failed semantic validation")
        exit_code = 2
    elif not approval_eligible:
        print(
            "::error::CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1 semantic_valid but NOT "
            f"approval_eligible (AC4 fail-closed; blockers={validation_decision.get('blockers')})"
        )
        exit_code = 3
    else:
        claim = result["assessment"]["claim"]
        print(f"CI_PERFORMANCE_BENCHMARK_EVIDENCE_GATE_RESULT_V1 gate_status=complete claim={claim}")
        exit_code = 0

    if args.receipt_output:
        # Issue #2422 AC10: under --production-invocation, args.manifest_sha256
        # is ALREADY guaranteed non-empty by the fail-closed check above --
        # the `or cohort_fixture_sha256` fallback expression is intentionally
        # NEVER evaluated (a fixture-sha256 substitute must never launder as
        # a trusted manifest digest on the production route). Outside
        # --production-invocation (the default), the pre-existing fallback
        # behavior for unit/fixture/exploratory-smoke callers is unchanged.
        if args.production_invocation:
            manifest_sha256_value = args.manifest_sha256
        else:
            cohort_fixture_sha256 = "sha256:" + hashlib.sha256(fixture_text.encode("utf-8")).hexdigest()
            manifest_sha256_value = args.manifest_sha256 or cohort_fixture_sha256
        trusted_functional_evidence = {
            "ci_verdict_summary_artifact_id": args.ci_verdict_summary_artifact_id,
            "ci_verdict_summary_file_sha256": args.expected_ci_verdict_summary_file_sha256,
            "github_artifact_digest": args.github_artifact_digest,
            "expected_head_sha": args.expected_head_sha,
        }
        receipt = build_close_grade_receipt(
            fixture,
            manifest_sha256=manifest_sha256_value,
            trusted_functional_evidence=trusted_functional_evidence,
            validation_decision=validation_decision,
            exit_code=exit_code,
            gate_status=result["gate_status"],
            experiment_identity=args.experiment_identity,
        )
        os.makedirs(os.path.dirname(args.receipt_output) or ".", exist_ok=True)
        with open(args.receipt_output, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2)
            handle.write("\n")
        print(
            f"CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1 experiment_identity={receipt['experiment_identity']} "
            f"performance_assessment.complete={receipt['performance_assessment']['complete']}"
        )

    return exit_code


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
# #2423 fix_delta P0-2 (OWNER REQUEST_CHANGES issuecomment-5540705404):
# a raw baseline record that is ENTIRELY missing `workflow_run_id` has no
# identity any `workflow_run_id`-keyed bookkeeping (`_pair_by_workflow_
# run_id`'s `all_ids`, `_select_initial_attempt_baselines`'s `by_id`
# grouping, `_root_workflow_run_ids`) can reference -- every one of those
# call sites independently `continue`s past it, so it silently vanishes
# from BOTH the eligible count AND `evidence_errors` with no trace at all
# (a real false-green risk: an arm could clear MIN_COHORT_RUN_COUNT and
# report zero evidence_errors purely because a raw identity-less record
# was never counted either way). Per OWNER: never invent a fake
# `workflow_run_id` to give such a record a slot in the per-id
# bookkeeping; callers instead use this raw COUNT to fail-close the WHOLE
# arm/gate outright.
# --------------------------------------------------------------------------- #
def _missing_workflow_run_id_raw_record_count(*baseline_lists: list[dict]) -> int:
    """Counts raw baseline records across `baseline_lists` whose
    `workflow_run_id` key is missing or `None` -- BEFORE any
    pairing/attempt/fingerprint filtering."""
    return sum(
        1
        for baselines in baseline_lists
        for baseline in baselines
        if baseline.get("workflow_run_id") is None
    )


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
# #2180 P1 fix_delta (OWNER REQUEST_CHANGES on PR #2490,
# issuecomment-5532831822): the AC9a relative-shortening DECISION (legacy
# pre-split `e2e` job P50 via `nearest_rank_v1`, compared against the paired
# provider critical-path P50, against `RELATIVE_SHORTENING_THRESHOLD`) is
# extracted into this single pure helper so that BOTH the real,
# artifact-dependent gate test
# (`test_p50_provider_meets_absolute_and_relative_shortening_threshold`
# below) AND the artifact-independent golden-vector regression suite
# (`test_ci_performance_gate_percentile_consistency.py`'s AC3) call the SAME
# decision-producing function, rather than the golden test reconstructing
# the percentile-then-ratio-then-threshold computation by calling
# `_nearest_rank_percentile()` directly. A future change to this decision
# (e.g. a different percentile, a different ratio formula, or a different
# zero-division policy) is now guaranteed to be visible to the golden test
# instead of silently bypassing it.
# --------------------------------------------------------------------------- #
def _legacy_e2e_vs_provider_relative_shortening(
    old_durations: list[float], provider_p50_seconds: float
) -> dict:
    """Computes the legacy pre-split `e2e` job's `nearest_rank_v1` P50 from
    `old_durations` (the real `_job_duration_seconds()` output) and the
    AC9a relative-shortening ratio against `provider_p50_seconds` (the
    already-computed paired provider critical-path P50 from
    `_provider_critical_path_paired_p50_p95`). Returns a dict with
    `old_p50_seconds`, `provider_p50_seconds`, `relative_shortening`, and
    `meets_relative_shortening_threshold` (>= `RELATIVE_SHORTENING_THRESHOLD`)."""
    old_p50_seconds = _nearest_rank_percentile(old_durations, 50)
    relative_shortening = (
        (old_p50_seconds - provider_p50_seconds) / old_p50_seconds if old_p50_seconds else 0.0
    )
    return {
        "old_p50_seconds": old_p50_seconds,
        "provider_p50_seconds": provider_p50_seconds,
        "relative_shortening": relative_shortening,
        "meets_relative_shortening_threshold": relative_shortening >= RELATIVE_SHORTENING_THRESHOLD,
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
# #2180 P1 fix_delta (OWNER REQUEST_CHANGES on PR #2490,
# issuecomment-5532831822): mirrors `_legacy_e2e_vs_provider_relative_
# shortening` above for the AC9b gate-ready before/after DECISION (both
# arms' same-clock `nearest_rank_v1` P50, and the non-regression judgment).
# Shared by the real gate test
# (`test_p50_gate_ready_latency_not_regressed` below) and the golden-vector
# regression suite (`test_ci_performance_gate_percentile_consistency.py`),
# so the two never silently diverge on how "non-regressed" is computed.
# --------------------------------------------------------------------------- #
def _gate_ready_before_after_non_regression(old_latencies: list[float], new_latencies: list[float]) -> dict:
    """Computes both arms' same-clock gate-ready latency `nearest_rank_v1`
    P50 (from the real `_gate_ready_latency_seconds()` output for each arm)
    and the AC9b non-regression judgment (`new_p50_seconds <=
    old_p50_seconds`). Returns a dict with `old_p50_seconds`,
    `new_p50_seconds`, `non_regressed`."""
    old_p50_seconds = _nearest_rank_percentile(old_latencies, 50)
    new_p50_seconds = _nearest_rank_percentile(new_latencies, 50)
    return {
        "old_p50_seconds": old_p50_seconds,
        "new_p50_seconds": new_p50_seconds,
        "non_regressed": new_p50_seconds <= old_p50_seconds,
    }


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
    raw_missing_workflow_run_id_counts: dict[str, int] | None = None,
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
    because the raw count happened to clear the floor.

    #2423 fix_delta P0-2 (OWNER REQUEST_CHANGES issuecomment-5540705404):
    `raw_missing_workflow_run_id_counts` (`{role: count}`) additionally
    fail-closes a role whose RAW core/responsive/gate-ready input included
    ANY record entirely missing `workflow_run_id` -- such a record cannot
    appear in `provider_evidence_errors` / `gate_ready_evidence_errors`
    (see `_missing_workflow_run_id_raw_record_count`'s own docstring for
    why), so without this explicit count it would silently vanish from
    both the sample count AND every evidence_errors list, even though the
    post-filter counts and evidence_errors above are otherwise clean."""
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
    for role, count in (raw_missing_workflow_run_id_counts or {}).items():
        if count:
            problems[f"raw_missing_workflow_run_id_count[{role}]"] = count
    if problems:
        raise EvidenceInsufficientError(
            f"insufficient POST-FILTER comparable-cohort evidence (need >= {min_count} "
            f"AFTER pairing/duration/timestamp filtering, not merely on raw pre-filter "
            f"per-job counts): {problems!r}"
        )


# --------------------------------------------------------------------------- #
# #2423 AC1/AC2/AC3: close-grade receipt materializer. #2422 owns the
# immutable dispatch root run set / manifest v2 producer (this Issue
# consumes, never reimplements, that producer); #2423 is the performance
# eligibility PROJECTION owner of that root run set -- root members are
# never silently dropped, only projected into
# `performance_eligible_workflow_run_ids` or explained in `evidence_errors`.
#
# Deliberately distinct from `_comparable_cohort()` above (see that
# function's own docstring): `_comparable_cohort()` is an EXPLORATORY-only
# helper that keeps only the single LARGEST WITHIN_COHORT_REQUIRED_EQUAL
# fingerprint group per job and REJECTS every other run outright -- a
# majority-selection design that is correct for the exploratory
# integration tests below (which only care about "is there a big-enough
# comparable cohort to report a P50/P95 from") but would be a false-green
# risk for close-grade evidence (a minority fingerprint mismatch, a
# collision, or a missing pair would simply vanish from the largest-group
# selection with no record of why). The functions below reuse
# `_pair_by_workflow_run_id` / `_select_initial_attempt_baselines` /
# `_fingerprint` / `_fingerprint_has_placeholder` -- the SAME fail-closed
# building blocks `_comparable_cohort()` and the AC11 hard-check already
# use -- rather than duplicating their attempt-trust/dedupe/collision
# logic, but never apply `_comparable_cohort()`'s majority selection to
# decide close-grade eligibility.
# --------------------------------------------------------------------------- #
CLOSE_GRADE_MATERIALIZATION_POLICY = "close_grade_fail_closed_v1"


# --------------------------------------------------------------------------- #
# Issue #2422 fix_delta Blocker 7 (OWNER REQUEST_CHANGES on PR #2501,
# issuecomment-5549966497): `_materialize_close_grade_arm` (below) requires
# BOTH `core_baselines` and `responsive_baselines` to pair by `workflow_run_id`
# via the UNMODIFIED `_pair_by_workflow_run_id` -- a genuine `monolith` run
# (Issue #2422's own single-provider topology: core+responsive run
# sequentially inside ONE `e2e-core` job, `e2e-responsive-matrix` never
# starts at all) has NO `e2e-responsive-matrix` evidence to pair, so every
# monolith root member was previously excluded wholesale
# (`missing_pair_e2e-responsive-matrix`), even though the run itself
# executed correctly. `_manifest_v2_provider_jobs_to_baselines` below is the
# adapter/handoff fix this Issue's Allowed Paths scope permits: it reshapes
# ONE e2e_performance_benchmark_manifest_v2 `Run`'s `provider_jobs[]` into
# the per-lane baseline-dict shape `_pair_by_workflow_run_id`/
# `_materialize_close_grade_arm` already consume -- WITHOUT fabricating a
# fake responsive record (never duplicates the core record) and WITHOUT
# copying/modifying `_materialize_close_grade_arm`/`_pair_by_workflow_run_id`
# themselves (Stop Conditions: #2423's eligibility projection logic is
# never touched). A monolith run correctly and explainably still lands in
# `evidence_errors` (small-sample `insufficient_evidence` as the eventual
# gate outcome is an ACCEPTED result of this adapter, per this Issue's own
# fix_delta text -- not a defect).
# --------------------------------------------------------------------------- #
def _manifest_v2_provider_jobs_to_baselines(
    run: dict, shared_fingerprint_fields: dict
) -> tuple[list[dict], list[dict]]:
    """Returns `(core_baselines, responsive_baselines)` -- each either a
    single-element list (the provider job genuinely started) or an EMPTY
    list (the provider job is genuinely absent from `run["provider_jobs"]`
    -- e.g. `e2e-responsive-matrix` on a `monolith` run -- never a
    synthesized stand-in). `shared_fingerprint_fields` carries the
    WITHIN_COHORT_REQUIRED_EQUAL-adjacent values Issue #2422 AC1's own
    frozen non-treatment fingerprint design guarantees are IDENTICAL across
    both `benchmark_layout` arms (e.g. derived from the manifest's
    `frozen_non_treatment.lockfile_hash`/`toolchain_digest`) -- this adapter
    reshapes already-trusted manifest v2 fields, it invents no new
    provenance."""
    workflow_run_id = run.get("workflow_run_id")
    run_attempt = run.get("run_attempt", 1)
    provider_by_job = {job.get("job"): job for job in run.get("provider_jobs", []) if isinstance(job, dict)}

    def _baseline_for(job_name: str) -> list[dict]:
        provider_job = provider_by_job.get(job_name)
        if provider_job is None:
            return []
        image = provider_job.get("exact_runner_image") or {}
        host_runner_image = (
            f"{image.get('name')}/{image.get('version')}" if image.get("name") and image.get("version") else None
        )
        return [
            {
                "workflow_run_id": workflow_run_id,
                "run_attempt": run_attempt,
                "workflow_digest": run.get("workflow_digest"),
                "host_runner_image": host_runner_image,
                **shared_fingerprint_fields,
            }
        ]

    return _baseline_for("e2e-core"), _baseline_for("e2e-responsive-matrix")


def _root_workflow_run_ids(*baseline_lists: list[dict]) -> list[object]:
    """#2423 AC2: the root run set for one arm is every distinct
    `workflow_run_id` present anywhere in the arm's raw input lists (core /
    responsive / gate-ready), BEFORE any pairing/attempt/fingerprint
    filtering -- this is the set the AC2 invariant
    (`expected_root_run_ids == performance_eligible_run_ids ∪
    evidence_error_run_ids`) is checked against, so it must never itself be
    computed from an already-filtered collection."""
    ids: set[object] = set()
    for baselines in baseline_lists:
        for baseline in baselines:
            workflow_run_id = baseline.get("workflow_run_id")
            if workflow_run_id is not None:
                ids.add(workflow_run_id)
    return sorted(ids, key=str)


def _materialize_close_grade_arm(
    core_baselines: list[dict],
    responsive_baselines: list[dict],
    gate_ready_baselines: list[dict],
    arm_label: str,
) -> tuple[dict, list[dict]]:
    """#2423 AC1/AC2: fail-closed close-grade materializer for a single
    arm's root run set. Every root `workflow_run_id` lands in EXACTLY ONE
    of `performance_eligible_workflow_run_ids` or `evidence_errors` (never
    both, never neither -- enforced by `_assert_root_eligible_error_
    invariant` below) by construction: `evidence_errors` collects every
    exclusion reason (unpaired provider lane, run_attempt identity
    collision, missing/invalid initial attempt, provider fingerprint
    placeholder/mismatch, missing/invalid gate-ready timestamp, or absent
    gate-ready evidence entirely), and eligibility is simply "root member
    minus everything `evidence_errors` already explains" -- so the
    invariant holds by set-complement construction, not by a second,
    possibly-divergent bookkeeping pass."""
    root_ids = _root_workflow_run_ids(core_baselines, responsive_baselines, gate_ready_baselines)

    evidence_errors: list[dict] = []
    error_ids: set[object] = set()

    def _record(workflow_run_id: object, reason: str) -> None:
        evidence_errors.append(
            {"workflow_run_id": str(workflow_run_id), "arm": arm_label, "reason": reason}
        )
        error_ids.add(workflow_run_id)

    # AC1: the SAME canonical `_pair_by_workflow_run_id` materialization
    # result the provider sample-count/percentile path consumes -- no
    # independent raw-artifact re-scan.
    pairs, pair_errors = _pair_by_workflow_run_id(core_baselines, responsive_baselines)
    for err in pair_errors:
        if err["workflow_run_id"] not in error_ids:
            _record(err["workflow_run_id"], err["reason"])

    for workflow_run_id, core, responsive in pairs:
        if _fingerprint_has_placeholder(core) or _fingerprint_has_placeholder(responsive):
            _record(workflow_run_id, "fingerprint_placeholder_or_missing")
            continue
        if _fingerprint(core) != _fingerprint(responsive):
            _record(workflow_run_id, "fingerprint_mismatch_core_vs_responsive")

    # #2423 fix_delta P0-3 (OWNER REQUEST_CHANGES issuecomment-5540705404):
    # the per-pair check above only catches SAME-run core-vs-responsive
    # fingerprint disagreement. It does NOT catch two internally-consistent
    # -but-DIFFERENT runs within the SAME arm (run A core==responsive==X,
    # run B core==responsive==Y, X != Y) -- that is cross-run provenance
    # drift WITHIN one arm's root run set, and the per-pair check alone
    # silently accepts it. Across every pair that passed the per-run check
    # above, the WITHIN_COHORT_REQUIRED_EQUAL fingerprint must take exactly
    # ONE distinct value for the whole arm; if not, fail-close every one of
    # those pairs (never select a majority/largest fingerprint group --
    # `_comparable_cohort()`'s own majority-selection behavior is
    # intentionally left untouched and is NOT reused here, per this
    # module's #2423 AC1/AC2/AC3 section docstring above).
    clean_pair_fingerprints: dict[object, tuple] = {
        workflow_run_id: _fingerprint(core)
        for workflow_run_id, core, _responsive in pairs
        if workflow_run_id not in error_ids
    }
    if len(set(clean_pair_fingerprints.values())) > 1:
        for workflow_run_id in clean_pair_fingerprints:
            _record(workflow_run_id, "arm_wide_fingerprint_drift_across_runs")

    gate_ready_selected, gate_ready_selection_errors = _select_initial_attempt_baselines(gate_ready_baselines)
    for err in gate_ready_selection_errors:
        if err["workflow_run_id"] not in error_ids:
            _record(err["workflow_run_id"], f"gate_ready_{err['reason']}")
    for workflow_run_id, baseline in gate_ready_selected.items():
        if workflow_run_id in error_ids:
            continue
        if _gate_ready_latency_seconds_from_baseline(baseline) is None:
            _record(workflow_run_id, "gate_ready_timestamp_missing_or_invalid")

    # Close-grade evidence requires BOTH the paired provider lane AND
    # gate-ready lane -- a root member missing EITHER lane's evidence is
    # fail-closed excluded, never silently treated as eligible on partial
    # (provider-only or gate-ready-only) evidence.
    paired_ids = {workflow_run_id for workflow_run_id, _core, _responsive in pairs}
    gate_ready_ids = {
        baseline.get("workflow_run_id")
        for baseline in gate_ready_baselines
        if baseline.get("workflow_run_id") is not None
    }
    for workflow_run_id in root_ids:
        if workflow_run_id in error_ids:
            continue
        if workflow_run_id not in paired_ids:
            _record(workflow_run_id, "missing_provider_pairing_evidence")
            continue
        if workflow_run_id not in gate_ready_ids:
            _record(workflow_run_id, "missing_gate_ready_evidence")

    eligible_ids = [wid for wid in root_ids if wid not in error_ids]

    # #2423 fix_delta P0-2 (OWNER REQUEST_CHANGES issuecomment-5540705404):
    # a raw core/responsive/gate-ready record entirely missing
    # `workflow_run_id` cannot be given a slot in the per-id
    # evidence_errors bookkeeping above (see
    # `_missing_workflow_run_id_raw_record_count`'s own docstring for why
    # -- inventing a fake `workflow_run_id` to force it into that
    # bookkeeping is explicitly rejected per OWNER). Instead, treat the
    # WHOLE arm as fail-closed: every id that would otherwise have been
    # eligible is excluded with a shared reason, so this arm's
    # `performance_eligible_workflow_run_ids` becomes empty rather than
    # silently proceeding as if the raw identity gap did not exist.
    if _missing_workflow_run_id_raw_record_count(core_baselines, responsive_baselines, gate_ready_baselines):
        for workflow_run_id in eligible_ids:
            _record(workflow_run_id, "arm_fail_closed_raw_record_missing_workflow_run_id")
        eligible_ids = []

    arm_result = {
        "workflow_run_ids": [str(wid) for wid in root_ids],
        "performance_eligible_workflow_run_ids": [str(wid) for wid in eligible_ids],
    }
    return arm_result, evidence_errors


def _assert_root_eligible_error_invariant(arm_result: dict, arm_errors: list[dict], arm_label: str) -> None:
    """#2423 AC2 invariant, enforced at runtime (fail-closed, never a
    silently-emitted receipt that violates its own contract):
    `expected_root_run_ids == performance_eligible_run_ids ∪
    evidence_error_run_ids` AND the two sets are disjoint."""
    root = set(arm_result["workflow_run_ids"])
    eligible = set(arm_result["performance_eligible_workflow_run_ids"])
    error_ids = {entry["workflow_run_id"] for entry in arm_errors}
    union = eligible | error_ids
    if union != root:
        raise AssertionError(
            f"#2423 AC2 invariant violated for arm={arm_label!r}: "
            f"root={root!r} != eligible ∪ evidence_error_ids={union!r}"
        )
    overlap = eligible & error_ids
    if overlap:
        raise AssertionError(
            f"#2423 AC2 invariant violated for arm={arm_label!r}: "
            f"eligible and evidence_error sets are not disjoint: {overlap!r}"
        )


def test_manifest_v2_provider_jobs_adapter_hands_off_real_monolith_split_asymmetric_topology_without_crashing():
    """Issue #2422 fix_delta Blocker 7 (OWNER REQUEST_CHANGES on PR #2501,
    issuecomment-5549966497): proves `_manifest_v2_provider_jobs_to_baselines`
    can accept REAL monolith(1 provider)/split(2 providers) evidence and
    hand it off to the UNMODIFIED `_materialize_close_grade_arm` without
    crashing, fabricating evidence, or duplicating the core record as a
    fake responsive record. A small-sample `insufficient_evidence` FINAL
    gate outcome is an accepted result here (this Issue's fix_delta text
    explicitly says so) -- this test only proves the HANDOFF, not the
    close-grade gate decision."""
    shared_fields = {
        "playwright_container_image_digest": "sha256:" + "a" * 64,
        "node_version": "v20.0.0",
        "pnpm_version": "9.0.0",
        "playwright_version": "1.48.0",
        "lockfile_hash": "sha256:" + "b" * 64,
    }
    monolith_run = {
        "workflow_run_id": 1001,
        "run_attempt": 1,
        "workflow_digest": "sha256:" + "c" * 64,
        "provider_jobs": [
            {
                "job": "e2e-core",
                "workflow_job_id": 5001,
                "conclusion": "success",
                "exact_runner_image": {"name": "ubuntu-24.04", "version": "20260901.1.0"},
            },
        ],
    }
    split_run = {
        "workflow_run_id": 2001,
        "run_attempt": 1,
        "workflow_digest": "sha256:" + "c" * 64,
        "provider_jobs": [
            {
                "job": "e2e-core",
                "workflow_job_id": 6001,
                "conclusion": "success",
                "exact_runner_image": {"name": "ubuntu-24.04", "version": "20260901.1.0"},
            },
            {
                "job": "e2e-responsive-matrix",
                "workflow_job_id": 6002,
                "conclusion": "success",
                "exact_runner_image": {"name": "ubuntu-24.04", "version": "20260901.1.0"},
            },
        ],
    }

    monolith_core, monolith_responsive = _manifest_v2_provider_jobs_to_baselines(monolith_run, shared_fields)
    split_core, split_responsive = _manifest_v2_provider_jobs_to_baselines(split_run, shared_fields)

    assert len(monolith_core) == 1
    assert monolith_responsive == []  # genuinely absent -- never fabricated/duplicated
    assert len(split_core) == 1
    assert len(split_responsive) == 1
    assert split_core[0]["host_runner_image"] == split_responsive[0]["host_runner_image"]

    monolith_result, monolith_errors = _materialize_close_grade_arm(monolith_core, monolith_responsive, [], "monolith")
    split_result, split_errors = _materialize_close_grade_arm(split_core, split_responsive, [], "split")

    # No crash -- both arms produce a coherent, fully-explained result
    # (the #2423 AC2 root/eligible/error-set invariant holds for both).
    _assert_root_eligible_error_invariant(monolith_result, monolith_errors, "monolith")
    _assert_root_eligible_error_invariant(split_result, split_errors, "split")

    # The monolith run's genuinely-absent responsive provider is EXPLAINED
    # (not silently dropped, not fabricated into a fake pass) -- exactly
    # the pre-fix_delta defect this Blocker exists to document/handle.
    assert any("missing_pair_e2e-responsive-matrix" in err["reason"] for err in monolith_errors)
    assert monolith_result["performance_eligible_workflow_run_ids"] == []

    # The split run's genuine 2-provider evidence is NOT excluded by the
    # provider-pairing step (it may still be excluded by the SEPARATE
    # missing-gate-ready-evidence check, since this test passes `[]` for
    # gate_ready_baselines -- out of scope for this specific adapter fix).
    assert not any("missing_pair" in err["reason"] for err in split_errors)


def _run_set_digest(monolith_ids: list[str], split_ids: list[str]) -> str:
    """#2423 AC3: a deterministic `sha256:<hex>` digest of the two arms'
    root run set MEMBERSHIP (never their measured values), so #2424 can
    detect a silent root-set substitution between two receipts claiming
    the same `experiment_identity`."""
    payload = json.dumps(
        {"monolith": sorted(monolith_ids, key=str), "split": sorted(split_ids, key=str)},
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_close_grade_receipt(
    fixture: dict,
    *,
    manifest_sha256: str,
    trusted_functional_evidence: dict,
    validation_decision: dict | None,
    exit_code: int,
    gate_status: str,
    experiment_identity: str | None = None,
) -> dict:
    """#2423 AC3: assembles `CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1`, the
    machine-readable receipt #2424 reads as a byte-matched consumer
    contract (field names: `experiment_identity` / `manifest_sha256` /
    `run_set_digest` / `materialization_policy` /
    `arms.{monolith,split}.workflow_run_ids` /
    `arms.{monolith,split}.performance_eligible_workflow_run_ids` /
    `evidence_errors` / `performance_assessment.complete` /
    `trusted_functional_evidence.*` / `validation.{semantic_valid,
    approval_eligible}` / `exit_code`).

    #2423 scope note (live Issue Background / OWNER-authorized design
    decision, since #2422's real immutable-manifest producer does not
    exist yet at this Issue's implementation time): this receipt treats
    the `--cohort-fixture` input's "before"/"after" arms -- the only root
    run set input actually available today -- as the `arms.monolith` /
    `arms.split` stand-in respectively (mirroring this module's existing
    before==pre-split / after==post-split convention, e.g.
    `test_p50_gate_ready_latency_not_regressed`'s `cohort_role` usage). A
    real #2422 manifest reference supersedes this stand-in as a follow-up,
    without changing this function's OUTPUT field names/shape (#2424's own
    Stop Condition on that renegotiation).

    #2423 fix_delta P0-1 (OWNER REQUEST_CHANGES issuecomment-5540705404):
    `gate_status` (the caller's real `run_evidence_gate()` result's
    `gate_status` field, `"insufficient_evidence" | "complete"`) is now a
    REQUIRED keyword-only argument, and `performance_assessment.complete`
    is derived SOLELY from `gate_status == "complete"` -- this function no
    longer re-derives completeness locally from its own
    `_materialize_close_grade_arm` evidence_errors/eligible-id bookkeeping
    (the pre-fix_delta rule -- "evidence_errors empty AND each arm has
    >= 1 eligible run" -- ignored `MIN_COHORT_RUN_COUNT` entirely: a
    2-run/arm otherwise-clean fixture would have incorrectly reported
    `complete: True` here even though `run_evidence_gate`'s real
    `gate_status` was `insufficient_evidence`, i.e. exit code 1). The
    per-arm `evidence_errors` this function still computes remain the
    single source of truth for AC2's root/eligible-set invariant, but are
    deliberately NOT cross-checked against `gate_status` -- production
    `run_evidence_gate()` is the one and only authority for
    `performance_assessment.complete`."""
    before = fixture["before"]
    after = fixture["after"]

    monolith_result, monolith_errors = _materialize_close_grade_arm(
        before["core_baselines"],
        before["responsive_baselines"],
        before.get("gate_ready_baselines", []),
        "monolith",
    )
    split_result, split_errors = _materialize_close_grade_arm(
        after["core_baselines"],
        after["responsive_baselines"],
        after.get("gate_ready_baselines", []),
        "split",
    )

    _assert_root_eligible_error_invariant(monolith_result, monolith_errors, "monolith")
    _assert_root_eligible_error_invariant(split_result, split_errors, "split")

    evidence_errors = monolith_errors + split_errors
    run_set_digest = _run_set_digest(monolith_result["workflow_run_ids"], split_result["workflow_run_ids"])

    if experiment_identity is None:
        experiment_identity = (
            f"issue-{fixture.get('issue_number')}-pr-{fixture.get('pr_number')}-"
            f"{str(before.get('commit_sha', ''))[:12]}-{str(after.get('commit_sha', ''))[:12]}"
        )

    complete = gate_status == "complete"

    validation_decision = validation_decision or {}

    return {
        "schema": "CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1",
        "schema_version": 1,
        "experiment_identity": experiment_identity,
        "manifest_sha256": manifest_sha256,
        "run_set_digest": run_set_digest,
        "materialization_policy": CLOSE_GRADE_MATERIALIZATION_POLICY,
        "arms": {
            "monolith": monolith_result,
            "split": split_result,
        },
        "evidence_errors": evidence_errors,
        "performance_assessment": {"complete": complete},
        "trusted_functional_evidence": trusted_functional_evidence,
        "validation": {
            "semantic_valid": bool(validation_decision.get("semantic_valid")),
            "approval_eligible": bool(validation_decision.get("approval_eligible")),
        },
        "exit_code": exit_code,
    }


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
    # #2180 (PR #2172 OWNER adversarial review P1-2, extended by the #2180
    # P1 fix_delta issuecomment-5532831822): the pre-split old `e2e`
    # baseline P50 must use the SAME `nearest_rank_v1` estimator as the
    # provider critical-path P50 above -- the Python stdlib's even-n median
    # (which averages the two middle values) disagrees with nearest_rank_v1
    # (which selects the 10th smallest outright for n=20), and that
    # divergence can flip the AC9a relative-shortening gate decision below.
    # `_legacy_e2e_vs_provider_relative_shortening` is the single pure
    # decision helper shared with
    # tests/ci/test_ci_performance_gate_percentile_consistency.py AC3, so a
    # future change to this decision cannot silently bypass that golden
    # regression suite.
    shortening = _legacy_e2e_vs_provider_relative_shortening(old_durations, provider_p50)
    old_p50 = shortening["old_p50_seconds"]
    relative_shortening = shortening["relative_shortening"]

    # AC9a absolute threshold.
    assert provider_p50 <= PROVIDER_P50_ABSOLUTE_THRESHOLD_SECONDS, (
        f"paired provider P50={provider_p50:.1f}s exceeds the "
        f"{PROVIDER_P50_ABSOLUTE_THRESHOLD_SECONDS}s absolute threshold (AC9a)"
    )

    # AC9a relative shortening threshold: >= 35% shorter than the pre-split
    # old `e2e` job's critical-path P50.
    assert shortening["meets_relative_shortening_threshold"], (
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

    # #2180 (extended by the #2180 P1 fix_delta issuecomment-5532831822):
    # gate-ready before/after P50 must use the same versioned
    # `nearest_rank_v1` estimator as every other decision-producing
    # percentile path in this file. `_gate_ready_before_after_non_
    # regression` is the single pure decision helper shared with
    # tests/ci/test_ci_performance_gate_percentile_consistency.py, so a
    # future change to this decision cannot silently bypass that golden
    # regression suite.
    non_regression = _gate_ready_before_after_non_regression(old_latencies, new_latencies)
    new_p50 = non_regression["new_p50_seconds"]
    old_p50 = non_regression["old_p50_seconds"]

    # AC9b: required stable `e2e` aggregate gate-ready latency P50 must not
    # regress relative to the old `e2e` job's gate-ready latency P50,
    # measured on the SAME clock for both arms (AC4).
    assert non_regression["non_regressed"], (
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


# --------------------------------------------------------------------------- #
# #2423 AC1/AC2/AC3: close-grade receipt materializer tests. Deliberately
# artifact-independent (unlike the exploratory integration tests above,
# which SKIP without live `.claude/artifacts/` data) -- these exercise the
# fail-closed materializer/receipt logic directly against small synthetic
# fixtures, per repo policy on behavioral verification (docs/dev/
# runtime-verification-policy.md distinguishes cohort-dependent SKIP tests
# from cohort-independent logic tests, the latter of which must be real
# unit tests, not SKIP).
# --------------------------------------------------------------------------- #
def _close_grade_paired_baselines(
    job: str, run_ids: list[int], base_ms: int = 60_000, fingerprint_overrides: dict | None = None
) -> list[dict]:
    fingerprint_overrides = fingerprint_overrides or {}
    baselines = []
    for i, run_id in enumerate(run_ids):
        baseline = {
            "workflow_run_id": run_id,
            "job": job,
            "run_attempt": 1,
            "measurements": [{"phase_id": "test_e2e_core", "elapsed_ms": base_ms + i}],
            "host_runner_image": "Linux/X64",
            "playwright_container_image_digest": "sha256:" + "c" * 64,
            "node_version": "20.0.0",
            "pnpm_version": "9.0.0",
            "playwright_version": "1.40.0",
            "lockfile_hash": "sha256:" + "d" * 64,
            "workflow_digest": "sha256:" + "e" * 64,
        }
        baseline.update(fingerprint_overrides.get(run_id, {}))
        baselines.append(baseline)
    return baselines


def _close_grade_gate_ready_baselines(run_ids: list[int], overrides: dict | None = None) -> list[dict]:
    overrides = overrides or {}
    baselines = []
    for run_id in run_ids:
        baseline = {
            "workflow_run_id": run_id,
            "run_attempt": 1,
            "run_started_at": "2026-08-15T00:00:00Z",
            "check_completed_at": "2026-08-15T00:05:00Z",
        }
        baseline.update(overrides.get(run_id, {}))
        baselines.append(baseline)
    return baselines


def test_close_grade_materialization_canonical_result_feeds_both_eligible_ids_and_root_ids():
    """GIVEN a clean set of paired+gate-ready baselines WHEN the AC1/AC2
    close-grade materializer runs THEN every root workflow_run_id is
    eligible (no evidence_errors) and the eligible set is computed from the
    SAME canonical `_pair_by_workflow_run_id` result the provider
    sample-count path already consumes (AC1) -- no independent raw
    artifact scan."""
    run_ids = [9001, 9002, 9003]
    core = _close_grade_paired_baselines("e2e-core", run_ids)
    responsive = _close_grade_paired_baselines("e2e-responsive-matrix", run_ids)
    gate_ready = _close_grade_gate_ready_baselines(run_ids)

    arm_result, evidence_errors = _materialize_close_grade_arm(core, responsive, gate_ready, "monolith")

    assert evidence_errors == []
    assert arm_result["workflow_run_ids"] == [str(rid) for rid in run_ids]
    assert arm_result["performance_eligible_workflow_run_ids"] == [str(rid) for rid in run_ids]

    pairs, pair_errors = _pair_by_workflow_run_id(core, responsive)
    assert pair_errors == []
    assert {str(wid) for wid, _c, _r in pairs} == set(arm_result["performance_eligible_workflow_run_ids"])


def test_canonical_materialization_provider_sample_count_matches_close_grade_eligible_count():
    """AC1: the provider post-filter sample count and the close-grade
    eligible-id count are two views of the SAME canonical
    `_pair_by_workflow_run_id` materialization result, not two
    independently (and possibly divergently) computed numbers."""
    run_ids = list(range(20001, 20021))
    core = _close_grade_paired_baselines("e2e-core", run_ids)
    responsive = _close_grade_paired_baselines("e2e-responsive-matrix", run_ids)
    gate_ready = _close_grade_gate_ready_baselines(run_ids)

    count, evidence_errors = _provider_post_filter_sample_count(core, responsive)
    arm_result, close_grade_errors = _materialize_close_grade_arm(core, responsive, gate_ready, "split")

    assert evidence_errors == []
    assert close_grade_errors == []
    assert count == len(arm_result["performance_eligible_workflow_run_ids"]) == len(run_ids)


def test_close_grade_materialization_never_silently_drops_root_members_evidence_error_union():
    """AC2 invariant: expected_root_run_ids == performance_eligible_run_ids
    ∪ evidence_error_run_ids, and the two sets are disjoint, even when the
    input mixes fully-clean runs with a run present ONLY in the gate-ready
    lane (no provider pairing at all) -- that run must still surface
    somewhere, never vanish."""
    paired_ids = [31001, 31002]
    gate_ready_only_id = 31099

    core = _close_grade_paired_baselines("e2e-core", paired_ids)
    responsive = _close_grade_paired_baselines("e2e-responsive-matrix", paired_ids)
    gate_ready = _close_grade_gate_ready_baselines(paired_ids + [gate_ready_only_id])

    arm_result, evidence_errors = _materialize_close_grade_arm(core, responsive, gate_ready, "monolith")

    root = set(arm_result["workflow_run_ids"])
    eligible = set(arm_result["performance_eligible_workflow_run_ids"])
    error_ids = {entry["workflow_run_id"] for entry in evidence_errors}

    assert root == {str(rid) for rid in paired_ids + [gate_ready_only_id]}
    assert eligible | error_ids == root
    assert not (eligible & error_ids)
    assert str(gate_ready_only_id) in error_ids
    assert str(gate_ready_only_id) not in eligible

    _assert_root_eligible_error_invariant(arm_result, evidence_errors, "monolith")


def test_close_grade_materialization_identity_collision_recorded_not_dropped():
    """AC2: a duplicate `workflow_run_id` with disagreeing content
    (`_detect_run_attempt_identity_collisions`'s existing fail-closed
    semantics) is recorded in evidence_errors with reason
    `run_attempt_identity_collision`, never silently absorbed via
    `_comparable_cohort()`'s largest-fingerprint-group majority
    selection."""
    run_id = 41001
    core = [
        {
            "workflow_run_id": run_id,
            "job": "e2e-core",
            "run_attempt": 1,
            "measurements": [{"phase_id": "test_e2e_core", "elapsed_ms": 60000}],
        },
        {
            "workflow_run_id": run_id,
            "job": "e2e-core",
            "run_attempt": 1,
            "measurements": [{"phase_id": "test_e2e_core", "elapsed_ms": 99999}],  # disagreeing content
        },
    ]
    responsive = _close_grade_paired_baselines("e2e-responsive-matrix", [run_id])
    gate_ready = _close_grade_gate_ready_baselines([run_id])

    arm_result, evidence_errors = _materialize_close_grade_arm(core, responsive, gate_ready, "split")

    assert arm_result["performance_eligible_workflow_run_ids"] == []
    reasons = {entry["reason"] for entry in evidence_errors if entry["workflow_run_id"] == str(run_id)}
    assert "run_attempt_identity_collision" in reasons
    _assert_root_eligible_error_invariant(arm_result, evidence_errors, "split")


def test_close_grade_materialization_dedupe_by_workflow_run_id_never_double_counts():
    """AC2: a rerun attempt (same workflow_run_id, run_attempt 2) never
    inflates the eligible set -- dedupe by workflow_run_id via the
    existing `_select_initial_attempt_baselines` policy applies here too."""
    run_id = 51001
    core = _close_grade_paired_baselines("e2e-core", [run_id])
    core.append(
        {
            **core[0],
            "run_attempt": 2,
            "measurements": [{"phase_id": "test_e2e_core", "elapsed_ms": 61000}],
        }
    )
    responsive = _close_grade_paired_baselines("e2e-responsive-matrix", [run_id])
    gate_ready = _close_grade_gate_ready_baselines([run_id])

    arm_result, evidence_errors = _materialize_close_grade_arm(core, responsive, gate_ready, "monolith")

    assert arm_result["workflow_run_ids"] == [str(run_id)]
    assert arm_result["performance_eligible_workflow_run_ids"] == [str(run_id)]
    assert evidence_errors == []


def test_close_grade_materialization_fingerprint_mismatch_recorded_in_evidence_errors():
    """AC2: `_comparable_cohort()`'s majority-fingerprint selection is NOT
    used for close-grade eligibility -- a fingerprint mismatch between the
    paired e2e-core/e2e-responsive-matrix runs is recorded in
    evidence_errors and excluded, never silently accepted nor silently
    majority-voted away."""
    run_id = 61001
    core = _close_grade_paired_baselines("e2e-core", [run_id])
    responsive = _close_grade_paired_baselines(
        "e2e-responsive-matrix",
        [run_id],
        fingerprint_overrides={run_id: {"node_version": "18.0.0"}},
    )
    gate_ready = _close_grade_gate_ready_baselines([run_id])

    arm_result, evidence_errors = _materialize_close_grade_arm(core, responsive, gate_ready, "split")

    assert arm_result["performance_eligible_workflow_run_ids"] == []
    reasons = {entry["reason"] for entry in evidence_errors if entry["workflow_run_id"] == str(run_id)}
    assert "fingerprint_mismatch_core_vs_responsive" in reasons


def test_close_grade_materialization_fingerprint_placeholder_recorded_in_evidence_errors():
    """AC2: a placeholder fingerprint field (#2159 P1-2's `_is_placeholder`)
    excludes a root member with an explicit evidence_errors reason,
    mirroring `_fingerprint_has_placeholder`'s existing fail-closed
    contract."""
    run_id = 71001
    core = _close_grade_paired_baselines(
        "e2e-core", [run_id], fingerprint_overrides={run_id: {"lockfile_hash": "unknown"}}
    )
    responsive = _close_grade_paired_baselines("e2e-responsive-matrix", [run_id])
    gate_ready = _close_grade_gate_ready_baselines([run_id])

    arm_result, evidence_errors = _materialize_close_grade_arm(core, responsive, gate_ready, "monolith")

    assert arm_result["performance_eligible_workflow_run_ids"] == []
    reasons = {entry["reason"] for entry in evidence_errors if entry["workflow_run_id"] == str(run_id)}
    assert "fingerprint_placeholder_or_missing" in reasons


def test_close_grade_materialization_no_silent_drop_of_root_run_ids_missing_pair():
    """AC2: a run present only in the e2e-core lane (missing its
    e2e-responsive-matrix pair) is recorded in evidence_errors via
    `_pair_by_workflow_run_id`'s existing contract, never silently dropped
    from the root run set."""
    paired_id = 81001
    unpaired_id = 81002
    core = _close_grade_paired_baselines("e2e-core", [paired_id, unpaired_id])
    responsive = _close_grade_paired_baselines("e2e-responsive-matrix", [paired_id])
    gate_ready = _close_grade_gate_ready_baselines([paired_id, unpaired_id])

    arm_result, evidence_errors = _materialize_close_grade_arm(core, responsive, gate_ready, "split")

    assert str(unpaired_id) in arm_result["workflow_run_ids"]
    assert str(unpaired_id) not in arm_result["performance_eligible_workflow_run_ids"]
    error_ids = {entry["workflow_run_id"] for entry in evidence_errors}
    assert str(unpaired_id) in error_ids


def test_receipt_build_close_grade_result_v1_field_shape_matches_2424_consumer_contract():
    """AC3: `build_close_grade_receipt` produces a
    CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1 document with every field #2424's
    consumer contract requires, byte-matched field names."""
    run_ids_before = [91001, 91002]
    run_ids_after = [92001, 92002]
    fixture = {
        "issue_number": 2423,
        "pr_number": 9999,
        "before": {
            "commit_sha": "a" * 40,
            "core_baselines": _close_grade_paired_baselines("e2e-core", run_ids_before),
            "responsive_baselines": _close_grade_paired_baselines("e2e-responsive-matrix", run_ids_before),
            "gate_ready_baselines": _close_grade_gate_ready_baselines(run_ids_before),
        },
        "after": {
            "commit_sha": "b" * 40,
            "core_baselines": _close_grade_paired_baselines("e2e-core", run_ids_after),
            "responsive_baselines": _close_grade_paired_baselines("e2e-responsive-matrix", run_ids_after),
            "gate_ready_baselines": _close_grade_gate_ready_baselines(run_ids_after),
        },
    }

    receipt = build_close_grade_receipt(
        fixture,
        manifest_sha256="sha256:" + "f" * 64,
        trusted_functional_evidence={
            "ci_verdict_summary_artifact_id": "12345",
            "ci_verdict_summary_file_sha256": "sha256:" + "0" * 64,
            "github_artifact_digest": "sha256:" + "1" * 64,
            "expected_head_sha": "b" * 40,
        },
        validation_decision={"semantic_valid": True, "approval_eligible": True},
        exit_code=0,
        # #2423 fix_delta P0-1: `gate_status` is a REQUIRED explicit
        # caller-supplied value (production supplies the real
        # `run_evidence_gate()` result); this shape-only unit test passes
        # "complete" directly since it is not itself exercising the
        # single-source-of-truth wiring (see the dedicated P0-1 regression
        # tests in test_ci_performance_gate_evidence_hard_failure.py for
        # that full production-path proof).
        gate_status="complete",
    )

    assert receipt["schema"] == "CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1"
    for key in (
        "experiment_identity",
        "manifest_sha256",
        "run_set_digest",
        "materialization_policy",
        "arms",
        "evidence_errors",
        "performance_assessment",
        "trusted_functional_evidence",
        "validation",
        "exit_code",
    ):
        assert key in receipt, f"missing #2424 consumer-contract field: {key}"

    for arm in ("monolith", "split"):
        assert arm in receipt["arms"]
        assert "workflow_run_ids" in receipt["arms"][arm]
        assert "performance_eligible_workflow_run_ids" in receipt["arms"][arm]

    assert receipt["arms"]["monolith"]["workflow_run_ids"] == [str(rid) for rid in run_ids_before]
    assert receipt["arms"]["split"]["workflow_run_ids"] == [str(rid) for rid in run_ids_after]
    assert receipt["evidence_errors"] == []
    assert receipt["performance_assessment"]["complete"] is True
    assert receipt["trusted_functional_evidence"]["ci_verdict_summary_file_sha256"] == "sha256:" + "0" * 64
    assert receipt["trusted_functional_evidence"]["github_artifact_digest"] == "sha256:" + "1" * 64
    assert (
        receipt["trusted_functional_evidence"]["ci_verdict_summary_file_sha256"]
        != receipt["trusted_functional_evidence"]["github_artifact_digest"]
    ), "AC4: ci_verdict_summary_file_sha256 and github_artifact_digest must never be conflated"
    assert receipt["validation"] == {"semantic_valid": True, "approval_eligible": True}
    assert receipt["exit_code"] == 0


def test_close_grade_result_receipt_complete_sourced_from_gate_status_not_local_evidence_errors():
    """#2423 fix_delta P0-1 (OWNER REQUEST_CHANGES issuecomment-5540705404):
    `performance_assessment.complete` is driven SOLELY by the caller-
    supplied `gate_status`, never re-derived locally from this receipt's
    own `evidence_errors`/eligible-id bookkeeping. Proven two ways on the
    SAME evidence-errors-non-empty fixture: (1) `gate_status=
    "insufficient_evidence"` (the value production's `run_evidence_gate()`
    would realistically compute here, since an unpaired-responsive arm
    also fails AC11's post-filter hard check) -> complete is False; (2)
    `gate_status="complete"` passed explicitly -> complete is True EVEN
    THOUGH `evidence_errors` is still non-empty -- demonstrating this
    function performs no cross-check against its own evidence_errors at
    all, per OWNER's "do not re-derive performance completeness inside
    the builder" instruction."""
    run_id_before = 93001
    fixture = {
        "issue_number": 2423,
        "pr_number": 9999,
        "before": {
            "commit_sha": "a" * 40,
            "core_baselines": _close_grade_paired_baselines("e2e-core", [run_id_before]),
            "responsive_baselines": [],  # unpaired -> evidence_errors
            "gate_ready_baselines": _close_grade_gate_ready_baselines([run_id_before]),
        },
        "after": {
            "commit_sha": "b" * 40,
            "core_baselines": _close_grade_paired_baselines("e2e-core", [94001]),
            "responsive_baselines": _close_grade_paired_baselines("e2e-responsive-matrix", [94001]),
            "gate_ready_baselines": _close_grade_gate_ready_baselines([94001]),
        },
    }

    insufficient_receipt = build_close_grade_receipt(
        fixture,
        manifest_sha256="sha256:" + "0" * 64,
        trusted_functional_evidence={},
        validation_decision=None,
        exit_code=1,
        gate_status="insufficient_evidence",
    )
    assert insufficient_receipt["evidence_errors"], "expected a non-empty evidence_errors list"
    assert insufficient_receipt["performance_assessment"]["complete"] is False
    assert insufficient_receipt["validation"] == {"semantic_valid": False, "approval_eligible": False}

    complete_receipt = build_close_grade_receipt(
        fixture,
        manifest_sha256="sha256:" + "0" * 64,
        trusted_functional_evidence={},
        validation_decision={"semantic_valid": True, "approval_eligible": True},
        exit_code=0,
        gate_status="complete",
    )
    assert complete_receipt["evidence_errors"] == insufficient_receipt["evidence_errors"], (
        "the receipt's own materializer evidence_errors must be identical regardless of "
        "gate_status -- only performance_assessment.complete differs"
    )
    assert complete_receipt["performance_assessment"]["complete"] is True, (
        "P0-1: complete must follow the passed gate_status alone, decoupled from this "
        "receipt's own (still non-empty) evidence_errors"
    )


# =============================================================================
# Issue #2422 AC10: `_cli_main` --production-invocation hardening -- the
# --cohort-fixture-file-sha256 fallback for --manifest-sha256 must be
# structurally unreachable from the production invocation route.
# =============================================================================


def _minimal_insufficient_evidence_fixture() -> dict:
    """A fixture deliberately below MIN_COHORT_RUN_COUNT (3 baselines per
    arm) -- reaches `gate_status: insufficient_evidence` (exit 1), which is
    enough to prove `_cli_main` got PAST the --production-invocation
    fail-closed check and into real gate computation, without requiring a
    full 20-run comparable cohort."""

    def arm(commit_sha: str, count: int, start_id: int) -> dict:
        core = [
            {
                "workflow_run_id": start_id + i,
                "job": "e2e-core",
                "measurements": [{"phase_id": "test_e2e_core", "elapsed_ms": 60000 + i}],
            }
            for i in range(count)
        ]
        responsive = [
            {
                "workflow_run_id": start_id + i,
                "job": "e2e-responsive-matrix",
                "measurements": [{"phase_id": "test_e2e_core", "elapsed_ms": 60000 + i}],
            }
            for i in range(count)
        ]
        gate_ready = [
            {
                "workflow_run_id": start_id + i,
                "run_started_at": "2026-08-15T00:00:00Z",
                "check_completed_at": "2026-08-15T00:05:00Z",
            }
            for i in range(count)
        ]
        return {
            "commit_sha": commit_sha,
            "core_baselines": core,
            "responsive_baselines": responsive,
            "gate_ready_baselines": gate_ready,
        }

    return {
        "issue_number": 2422,
        "pr_number": 0,
        "measured_at": "2026-09-05T00:00:00Z",
        "functional_evidence": {"proof_level": "check_run_only", "coverage_bound": False},
        "declared_impact": "AC10 production-invocation hardening test fixture (deliberately insufficient).",
        "risk_acknowledgement": {
            "reference": {"source_kind": "issue_comment", "source_id": "test-fixture"},
            "verification_status": "unverified",
        },
        "cohort_provenance": {
            "runner_image": "ubuntu-24.04",
            "workers": 1,
            "scheduler": "loadscope",
            "command_manifest_digest": "sha256:" + "0" * 64,
            "test_selection_digest": "sha256:" + "0" * 64,
        },
        "before": arm("0" * 40, 3, 9000),
        "after": arm("1" * 40, 3, 19000),
    }


def test_cli_main_production_invocation_fails_closed_when_manifest_sha256_missing(tmp_path):
    """GIVEN --production-invocation WHEN --manifest-sha256 is omitted THEN
    _cli_main exits 4 WITHOUT ever opening --cohort-fixture (a nonexistent
    path proves this: opening it would raise FileNotFoundError, not
    return 4) -- Issue #2422 AC10."""
    output_path = tmp_path / "gate_result.json"
    exit_code = _cli_main(
        [
            "--production-invocation",
            "--cohort-fixture",
            str(tmp_path / "does-not-exist.json"),
            "--output",
            str(output_path),
            "--experiment-identity",
            "exp-1",
        ]
    )
    assert exit_code == 4
    assert not output_path.exists()


def test_cli_main_production_invocation_fails_closed_when_experiment_identity_missing(tmp_path):
    """Same as above but --experiment-identity is the field omitted --
    Issue #2422 AC10 requires BOTH trusted inputs, not manifest-sha256
    alone."""
    output_path = tmp_path / "gate_result.json"
    exit_code = _cli_main(
        [
            "--production-invocation",
            "--cohort-fixture",
            str(tmp_path / "does-not-exist.json"),
            "--output",
            str(output_path),
            "--manifest-sha256",
            "sha256:" + "a" * 64,
        ]
    )
    assert exit_code == 4
    assert not output_path.exists()


def test_cli_main_production_invocation_with_required_inputs_reaches_gate_computation_and_never_falls_back(tmp_path):
    """GIVEN --production-invocation WITH both --manifest-sha256 and
    --experiment-identity supplied WHEN _cli_main runs THEN it reaches real
    gate computation (exit 1, insufficient_evidence, for this deliberately-
    small fixture) AND the emitted receipt's `manifest_sha256` is the
    SUPPLIED value, never the --cohort-fixture file's own sha256 (Issue
    #2422 AC10 -- proves the fallback expression path was never taken)."""
    fixture_path = tmp_path / "cohort_fixture.json"
    fixture_path.write_text(json.dumps(_minimal_insufficient_evidence_fixture()), encoding="utf-8")
    output_path = tmp_path / "gate_result.json"
    receipt_path = tmp_path / "receipt.json"
    supplied_manifest_sha256 = "sha256:" + "7" * 64

    exit_code = _cli_main(
        [
            "--production-invocation",
            "--cohort-fixture",
            str(fixture_path),
            "--output",
            str(output_path),
            "--receipt-output",
            str(receipt_path),
            "--manifest-sha256",
            supplied_manifest_sha256,
            "--experiment-identity",
            "exp-production-1",
        ]
    )

    assert exit_code == 1  # insufficient_evidence -- reached real gate computation
    assert output_path.exists()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["manifest_sha256"] == supplied_manifest_sha256

    fixture_file_sha256 = "sha256:" + hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    assert receipt["manifest_sha256"] != fixture_file_sha256


def test_cli_main_non_production_invocation_still_falls_back_to_fixture_sha256(tmp_path):
    """GIVEN NO --production-invocation flag (the default -- unit/fixture/
    exploratory-smoke route) WHEN --manifest-sha256 is omitted THEN the
    pre-existing --cohort-fixture-file-sha256 fallback behavior is
    UNCHANGED (Issue #2422 AC10: fallback stays available on this route)."""
    fixture_path = tmp_path / "cohort_fixture.json"
    fixture_text = json.dumps(_minimal_insufficient_evidence_fixture())
    fixture_path.write_text(fixture_text, encoding="utf-8")
    output_path = tmp_path / "gate_result.json"
    receipt_path = tmp_path / "receipt.json"

    exit_code = _cli_main(
        [
            "--cohort-fixture",
            str(fixture_path),
            "--output",
            str(output_path),
            "--receipt-output",
            str(receipt_path),
        ]
    )

    assert exit_code == 1  # insufficient_evidence -- reached real gate computation
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    expected_fallback_sha256 = "sha256:" + hashlib.sha256(fixture_text.encode("utf-8")).hexdigest()
    assert receipt["manifest_sha256"] == expected_fallback_sha256


def test_ci_yml_production_gate_call_site_wires_production_invocation_flags():
    """Issue #2422 fix_delta Blocker 8 (OWNER REQUEST_CHANGES on PR #2501,
    issuecomment-5549966497): `_cli_main`'s `--production-invocation`
    hardening (proven by the three tests immediately above) is only a real
    fix if `.github/workflows/ci.yml`'s ACTUAL production call site (the
    `python-test-core` job's `Run AC11 evidence gate...` step) passes it --
    a unit test of `_cli_main` alone cannot prove that. This test parses
    the REAL workflow YAML (never a hand-copied string literal that could
    drift from the file) and asserts the step's `run:` text (a) still
    invokes `tests/ci/test_ci_performance_gate.py` and `--cohort-fixture`
    (unchanged base invocation) and (b) now ALSO references
    `--production-invocation` / `--manifest-sha256` / `--experiment-identity`
    bound to new `manifest_v2_sha256`/`manifest_v2_experiment_identity`
    workflow_dispatch inputs -- the wiring this fix_delta blocker adds."""
    yaml = pytest.importorskip("yaml")
    ci_yml_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    with open(ci_yml_path, encoding="utf-8") as handle:
        workflow = yaml.safe_load(handle)

    # PyYAML's default (YAML 1.1) resolver parses the bare `on:` workflow
    # trigger key as the boolean `True`, not the string `"on"` -- look up
    # whichever key form is actually present rather than assuming one.
    on_section = workflow.get("on", workflow.get(True))
    dispatch_inputs = on_section["workflow_dispatch"]["inputs"]
    assert "manifest_v2_sha256" in dispatch_inputs
    assert "manifest_v2_experiment_identity" in dispatch_inputs
    assert dispatch_inputs["manifest_v2_sha256"]["default"] == ""
    assert dispatch_inputs["manifest_v2_experiment_identity"]["default"] == ""

    python_test_core = workflow["jobs"]["python-test-core"]
    gate_steps = [
        step
        for step in python_test_core["steps"]
        if "tests/ci/test_ci_performance_gate.py" in (step.get("run") or "")
    ]
    assert len(gate_steps) == 1, "expected exactly one production gate invocation step"
    run_text = gate_steps[0]["run"]

    assert "--cohort-fixture" in run_text  # base invocation unchanged
    assert "--production-invocation" in run_text
    assert "--manifest-sha256" in run_text
    assert "--experiment-identity" in run_text
    assert "MANIFEST_V2_SHA256" in run_text
    assert "MANIFEST_V2_EXPERIMENT_IDENTITY" in run_text

    env = gate_steps[0].get("env", {})
    assert env.get("MANIFEST_V2_SHA256") == "${{ github.event.inputs.manifest_v2_sha256 }}"
    assert env.get("MANIFEST_V2_EXPERIMENT_IDENTITY") == "${{ github.event.inputs.manifest_v2_experiment_identity }}"


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_cli_main())
