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
import statistics
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


def _dedupe_by_workflow_run_id(baselines: list[dict]) -> list[dict]:
    """#2159 P0-2/P1-1: sample identity is `workflow_run_id`, not `(run_id,
    run_attempt)`. Baselines missing `workflow_run_id` are excluded (they
    cannot be deduped or paired safely). Keeps the FIRST baseline seen per
    `workflow_run_id` -- rerun attempts of the same run never add an
    independent sample."""
    seen: dict[object, dict] = {}
    for baseline in baselines:
        workflow_run_id = baseline.get("workflow_run_id")
        if workflow_run_id is None:
            continue
        seen.setdefault(workflow_run_id, baseline)
    return list(seen.values())


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
    AC3)."""
    core_by_id = {b["workflow_run_id"]: b for b in core_baselines if b.get("workflow_run_id") is not None}
    responsive_by_id = {
        b["workflow_run_id"]: b for b in responsive_baselines if b.get("workflow_run_id") is not None
    }
    all_ids = sorted(set(core_by_id) | set(responsive_by_id), key=str)

    pairs: list[tuple[object, dict, dict]] = []
    evidence_errors: list[dict] = []
    for workflow_run_id in all_ids:
        core = core_by_id.get(workflow_run_id)
        responsive = responsive_by_id.get(workflow_run_id)
        if core is None or responsive is None:
            evidence_errors.append(
                {
                    "workflow_run_id": workflow_run_id,
                    "reason": "missing_pair_e2e-core" if core is None else "missing_pair_e2e-responsive-matrix",
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


def test_p50_provider_meets_absolute_and_relative_shortening_threshold():
    baselines = _find_all_baselines()
    cohort = _comparable_cohort(baselines, ("e2e-core", "e2e-responsive-matrix", "e2e"))
    core_baselines = cohort["e2e-core"]
    responsive_baselines = cohort["e2e-responsive-matrix"]
    old_e2e_baselines = cohort["e2e"]

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

    old_durations = _job_duration_seconds(old_e2e_baselines)
    assert old_durations, "cohort must include real elapsed_ms measurements for the pre-split old e2e job"

    provider_p50 = provider["p50_seconds"]
    old_p50 = statistics.median(old_durations)

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

    new_p50 = statistics.median(new_latencies)
    old_p50 = statistics.median(old_latencies)

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
