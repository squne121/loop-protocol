"""
tests/ci/test_ci_performance_gate.py

Issue #2119 AC9/AC10: real comparable-cohort (20 run) P50/P95 CI runtime
gate. Runtime Verification Applicability for this Issue is `decision:
immediate` with `fallback_policy.fallback_success_is_pass: false` — these
tests intentionally SKIP (not PASS) when a real 20-run comparable cohort
artifact set is not available in the current environment (this
implementation session has no live GitHub Actions history to draw from;
the actual cohort accumulates only after this PR's own CI runs land on
main). SKIP is the correct outcome here per
docs/dev/runtime-verification-policy.md, not a fabricated PASS, and not
success achieved via a fallback path.

PR #2137 human review (issuecomment-5273090534, P0) fixes applied here:

- `test_p50_provider_meets_absolute_and_relative_shortening_threshold` now
  ALSO computes the pre-split `e2e` job's P50 from the same comparable
  cohort and asserts the required >= 35% relative shortening (AC9a), not
  just the absolute <= 270s threshold.
- `test_p50_gate_ready_latency_not_regressed` now performs the real
  gate-ready-latency P50 comparison once both a valid 20-run cohort and a
  valid old-`e2e`-job baseline exist, instead of an unconditional
  `pytest.skip()` that could never PASS.
- The cohort loader (`_comparable_cohort`) now builds/verifies a full
  comparability fingerprint (runner image / Node / pnpm / Playwright /
  lockfile / workflow digest — see `.github/workflows/ci.yml` jobs.e2e-core
  / jobs.e2e-responsive-matrix `Collect ci_runtime_baseline_v1 artifact`
  steps) instead of runner image alone, and excludes non-matching runs.
- `test_p95_failure_and_flaky_rate_validated_from_real_assessment_artifact`
  now ALSO asserts `approval_eligible == true` from the validator's
  cross-checked (not self-reported) output, by supplying
  `--ci-verdict-summary` / `--expected-head-sha` out-of-band, per the
  validator's own contract (`validate_ci_performance_assessment_v2.py`
  docstring: "functional_evidence...selected_checks is a self-report and
  is NEVER sufficient on its own for approval_eligible").

Once a real `ci_runtime_baseline_v1` cohort (>= 20 comparable runs of
`e2e-core` / `e2e-responsive-matrix` / the `e2e` aggregate, all sharing
runner image / Node / pnpm / Playwright / lockfile / workflow digest) and a
real `CI_TEST_PERFORMANCE_ASSESSMENT_V2` artifact exist under
`.claude/artifacts/`, these tests compute the actual P50/P95 gate from that
real data.
"""
from __future__ import annotations

import glob
import importlib.util
import json
import os
import pathlib
import statistics

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

# Issue #2119: "同一runner image / Node / pnpm / Playwright / lockfile /
# workflow digest" comparability fingerprint fields recorded by
# `.github/workflows/ci.yml` jobs.e2e-core / jobs.e2e-responsive-matrix
# `Collect ci_runtime_baseline_v1 artifact` steps.
COMPARABILITY_FINGERPRINT_FIELDS = (
    "runner_image",
    "node_version",
    "pnpm_version",
    "playwright_version",
    "lockfile_hash",
    "workflow_digest",
)


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


def _fingerprint(baseline: dict) -> tuple:
    """Issue #2119 AC9: the full comparability fingerprint tuple for a
    single ci_runtime_baseline_v1 run. A baseline missing any fingerprint
    field is intentionally NOT comparable (returns a tuple containing None
    for that field, which will never equal another run's real value) --
    fail-closed, never silently treated as a match."""
    return tuple(baseline.get(field) for field in COMPARABILITY_FINGERPRINT_FIELDS)


def _comparable_cohort(baselines: list[dict], job_names: tuple[str, ...]) -> dict[str, list[dict]]:
    """Issue #2119 AC9 (PR #2137 human review issuecomment-5273090534 P0
    fix): groups baselines for `job_names` by their FULL comparability
    fingerprint (runner image / Node / pnpm / Playwright / lockfile /
    workflow digest — not runner image alone), and returns only the
    baselines belonging to the single LARGEST fingerprint group per job
    (i.e. excludes/rejects any run whose fingerprint does not match the
    majority cohort for that job)."""
    by_job: dict[str, dict[tuple, list[dict]]] = {name: {} for name in job_names}
    for baseline in baselines:
        job = baseline.get("job")
        if job not in by_job:
            continue
        fp = _fingerprint(baseline)
        by_job[job].setdefault(fp, []).append(baseline)

    result: dict[str, list[dict]] = {}
    for job, groups in by_job.items():
        if not groups:
            result[job] = []
            continue
        largest_fp = max(groups, key=lambda fp: len(groups[fp]))
        result[job] = groups[largest_fp]
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
            f"ci_runtime_baseline_v1 runs per job (same {', '.join(COMPARABILITY_FINGERPRINT_FIELDS)}) "
            f"for e2e-core, e2e-responsive-matrix, AND the pre-split old `e2e` job (for the "
            f"AC9a relative-shortening comparison); found core={len(core_baselines)} "
            f"responsive={len(responsive_baselines)} old_e2e={len(old_e2e_baselines)} locally "
            f"under {ARTIFACTS_DIR}. This accumulates from real CI runs post-merge (Runtime "
            f"Verification Applicability decision: immediate, fallback_success_is_pass: false "
            f"— SKIP, not a fabricated PASS)."
        )

    core_durations = _job_duration_seconds(core_baselines)
    responsive_durations = _job_duration_seconds(responsive_baselines)
    old_durations = _job_duration_seconds(old_e2e_baselines)
    assert core_durations and responsive_durations and old_durations, (
        "cohort must include real elapsed_ms measurements for e2e-core, "
        "e2e-responsive-matrix, and the pre-split old e2e job"
    )

    provider_p50 = max(statistics.median(core_durations), statistics.median(responsive_durations))
    old_p50 = statistics.median(old_durations)

    # AC9a absolute threshold.
    assert provider_p50 <= PROVIDER_P50_ABSOLUTE_THRESHOLD_SECONDS, (
        f"max(e2e-core, e2e-responsive-matrix) provider P50={provider_p50:.1f}s "
        f"exceeds the {PROVIDER_P50_ABSOLUTE_THRESHOLD_SECONDS}s absolute threshold (AC9a)"
    )

    # AC9a relative shortening threshold: >= 35% shorter than the pre-split
    # old `e2e` job's critical-path P50 (PR #2137 human review P0 fix --
    # this assertion previously did not exist at all).
    relative_shortening = (old_p50 - provider_p50) / old_p50 if old_p50 else 0.0
    assert relative_shortening >= RELATIVE_SHORTENING_THRESHOLD, (
        f"provider P50={provider_p50:.1f}s vs old e2e P50={old_p50:.1f}s is only "
        f"{relative_shortening:.1%} shorter, below the "
        f"{RELATIVE_SHORTENING_THRESHOLD:.0%} relative-shortening threshold (AC9a)"
    )


def _gate_ready_latency_seconds(baselines: list[dict]) -> list[float]:
    """For the pre-split old `e2e` job, gate-ready latency == the job's own
    total measured elapsed time (the monolithic job's own success WAS the
    aggregate gate signal). For the post-split aggregate `e2e` job, it is
    the `gate_ready_latency_ms` field recorded directly by
    `.github/workflows/ci.yml` jobs.e2e "Record gate-ready latency
    (ci_runtime_baseline_v1)" step (wall-clock from workflow-run start to
    aggregate conclusion, since the aggregate job now depends on `needs`
    rather than running the suite itself)."""
    latencies = []
    for baseline in baselines:
        if "gate_ready_latency_ms" in baseline and baseline["gate_ready_latency_ms"] is not None:
            latencies.append(baseline["gate_ready_latency_ms"] / 1000)
            continue
        total_ms = sum(m.get("elapsed_ms", 0) for m in baseline.get("measurements", []))
        if total_ms > 0:
            latencies.append(total_ms / 1000)
    return latencies


def test_p50_gate_ready_latency_not_regressed():
    baselines = _find_all_baselines()
    # The aggregate `e2e` job's comparability fingerprint changes across the
    # split boundary (this PR itself changes workflow_digest), so old vs
    # new `e2e` runs never share a single fingerprint group by
    # construction -- split explicitly by whether gate_ready_latency_ms is
    # present (new instrumentation added by this Issue) for this specific
    # before/after comparison, rather than by _comparable_cohort's
    # single-largest-fingerprint-group behavior.
    all_e2e_baselines = [b for b in baselines if b.get("job") == "e2e"]
    new_e2e_baselines = [b for b in all_e2e_baselines if b.get("gate_ready_latency_ms") is not None]
    old_e2e_baselines = [b for b in all_e2e_baselines if b.get("gate_ready_latency_ms") is None]

    if len(new_e2e_baselines) < MIN_COHORT_RUN_COUNT or len(old_e2e_baselines) < MIN_COHORT_RUN_COUNT:
        pytest.skip(
            f"gate-ready latency P50 comparison requires >= {MIN_COHORT_RUN_COUNT} comparable "
            f"post-split `e2e` aggregate runs (with gate_ready_latency_ms recorded) AND >= "
            f"{MIN_COHORT_RUN_COUNT} pre-split old `e2e` job runs; found "
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
    assert new_latencies and old_latencies, "cohort must include real gate-ready latency data"

    new_p50 = statistics.median(new_latencies)
    old_p50 = statistics.median(old_latencies)

    # AC9b: required stable `e2e` aggregate gate-ready latency P50 must not
    # regress relative to the old `e2e` job's gate-ready latency P50.
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

    # Issue #2119 AC10 (PR #2137 human review issuecomment-5273090534 P0
    # fix): approval_eligible must be cross-checked out-of-band against the
    # trusted ci_verdict_summary_v2 artifact and the expected head SHA --
    # the assessment's own self-reported selected_checks are never trusted
    # alone (see validate_ci_performance_assessment_v2.py module docstring).
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
        # exit_code == EXIT_VALID only proves structural_valid and
        # semantic_valid -- the validator's own contract explicitly allows
        # exit 0 with approval_eligible=false. AC10 additionally requires
        # approval_eligible=true, cross-checked against the trusted
        # ci_verdict_summary_v2 artifact above (not self-reported).
        assert decision.get("approval_eligible") is True, (
            f"CI_TEST_PERFORMANCE_ASSESSMENT_V2 at {path} is structurally/semantically valid "
            f"but NOT approval_eligible (blockers={decision.get('blockers')}) -- exit_code "
            f"alone is insufficient per AC10 (PR #2137 human review issuecomment-5273090534 "
            f"P0 fix)"
        )
