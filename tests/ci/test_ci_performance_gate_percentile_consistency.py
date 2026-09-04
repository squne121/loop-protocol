"""
tests/ci/test_ci_performance_gate_percentile_consistency.py

Issue #2180 (PR #2172 OWNER adversarial review, deferred finding P1-2):
`tests/ci/test_ci_performance_gate.py` previously mixed two different
percentile estimators across its decision-producing paths -- the provider
critical-path P50/P95 already used `nearest_rank_v1`
(`_nearest_rank_percentile`), while the pre-split legacy `e2e` (before)
baseline and the gate-ready before/after latency comparison used Python's
`statistics.median()`. On even-n samples the two estimators disagree
(`nearest_rank_v1` selects the n/2-th smallest observed value outright;
`statistics.median()` averages the two middle values), and that divergence
can flip the AC9a 35%-relative-shortening gate decision depending on which
code path happened to compute the "before" P50.

This module is a deterministic, artifact-independent regression suite
(Runtime Verification Applicability: `not_applicable` -- percentile
semantics and threshold decisions are fully verifiable via fixture-based
pytest, no live GitHub API / Actions cohort required). It intentionally
does NOT just call `_nearest_rank_percentile()` directly with hand-picked
numbers in isolation (a circular test that would prove nothing about the
actual gate). Instead each golden vector is materialized as
`ci_runtime_baseline_v1`-shaped fixtures and run through the SAME
decision-producing functions the gate itself calls
(`_job_duration_seconds`, `_gate_ready_latency_seconds`,
`_pair_by_workflow_run_id` + `_provider_critical_path_paired_p50_p95`),
with `_nearest_rank_percentile` applied exactly as the (now-unified) gate
code does at each call site. `statistics.median()` is used ONLY inside
this test file, as a contrast value proving the two estimators actually
diverge on these fixtures -- it must NOT reappear in
`test_ci_performance_gate.py` itself (see AC1's `! rg` Verification
Command).

Golden vectors (Issue #2180 AC2/AC3/AC4):

- AC2 (even-n P50 divergence): `[100.0] * 10 + [200.0] * 10` (n=20) ->
  `nearest_rank_v1` P50 == 100.0, NOT `statistics.median`'s 150.0.
- AC3 (35% gate decision reversal regression):
  `old_samples = [100.0] * 10 + [200.0] * 10`,
  `provider_samples = [90.0] * 20` -> unified relative shortening ==
  `(100 - 90) / 100 == 10%`, which must NOT incorrectly PASS the AC9a 35%
  relative-shortening gate (the pre-fix mixed-estimator computation would
  have used `statistics.median(old_samples) == 150`, yielding a shortening
  of `(150 - 90) / 150 == 40%` that DOES incorrectly pass the same gate --
  this is the exact gate-decision-reversal bug from PR #2172's deferred
  finding).
- AC4 (P95 boundary): `[100.0] * 19 + [500.0]` -> `nearest_rank_v1` P95 ==
  100.0, with the estimator version explicitly declared as
  `nearest_rank_v1` in the decision-producing result payload.

This file also fixes the gate-side (`tests/ci/test_ci_performance_gate.py`)
and validator-side
(`.claude/skills/ci-test-performance/scripts/validate_ci_performance_assessment_v2.py`)
`_nearest_rank_percentile()` implementations' semantic parity across the
same golden vectors, since the two are deliberately duplicated (not a
shared import) across the Allowed Paths boundary between the gate and
validator Issue contracts.
"""
from __future__ import annotations

import importlib.util
import pathlib
import statistics
from datetime import datetime, timedelta, timezone

_GATE_MODULE_PATH = pathlib.Path(__file__).resolve().parent / "test_ci_performance_gate.py"


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("test_ci_performance_gate", _GATE_MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate_module()
validator = gate._load_validator_module()


def _job_duration_baseline(job: str, workflow_run_id: int, elapsed_ms: int) -> dict:
    """`ci_runtime_baseline_v1`-shaped fixture that `_job_duration_seconds()`
    (the real function feeding the legacy `old_p50` decision path) sums a
    `test_e2e*`-prefixed phase from."""
    return {
        "schema": "ci_runtime_baseline_v1",
        "job": job,
        "workflow_run_id": workflow_run_id,
        "run_attempt": 1,
        "measurements": [{"phase_id": "test_e2e_ci", "elapsed_ms": elapsed_ms, "status": 0}],
    }


def _gate_ready_baseline(job: str, workflow_run_id: int, latency_seconds: float) -> dict:
    """`ci_runtime_baseline_v1`-shaped fixture that
    `_gate_ready_latency_seconds()` (the real function feeding the
    gate-ready before/after P50 decision path) derives a same-clock
    latency from."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=latency_seconds)
    return {
        "schema": "ci_runtime_baseline_v1",
        "job": job,
        "workflow_run_id": workflow_run_id,
        "run_attempt": 1,
        "run_started_at": start.isoformat().replace("+00:00", "Z"),
        "check_completed_at": end.isoformat().replace("+00:00", "Z"),
    }


def _job_duration_fixture_durations(latencies: list[float]) -> list[float]:
    """Materializes `latencies` as `_job_duration_seconds()` output by
    round-tripping through real `ci_runtime_baseline_v1` fixtures (elapsed_ms
    in, seconds out) -- exercises the actual decision-producing conversion,
    not a bare literal list."""
    baselines = [
        _job_duration_baseline("e2e", 9000 + i, int(latency * 1000)) for i, latency in enumerate(latencies)
    ]
    return gate._job_duration_seconds(baselines)


def _gate_ready_fixture_latencies(latencies: list[float]) -> list[float]:
    baselines = [_gate_ready_baseline("e2e", 8000 + i, latency) for i, latency in enumerate(latencies)]
    return gate._gate_ready_latency_seconds(baselines)


# --------------------------------------------------------------------------- #
# AC2: even-n P50 divergence, exercised through the legacy e2e job-duration
# decision path (the same path `old_p50 = _nearest_rank_percentile(
# old_durations, 50)` in test_ci_performance_gate.py now uses).
# --------------------------------------------------------------------------- #
def test_ac2_legacy_e2e_p50_even_n_divergence_uses_nearest_rank_v1():
    """GIVEN 20 legacy e2e job-duration baselines whose `_job_duration_
    seconds()` output is `[100.0] * 10 + [200.0] * 10` WHEN the SAME
    `_nearest_rank_percentile()` call used at test_ci_performance_gate.py's
    old_p50 site is applied THEN it returns 100.0, not the 150.0
    `statistics.median()` would return for this even-n sample."""
    durations = _job_duration_fixture_durations([100.0] * 10 + [200.0] * 10)
    assert sorted(durations) == [100.0] * 10 + [200.0] * 10

    unified_p50 = gate._nearest_rank_percentile(durations, 50)
    assert unified_p50 == 100.0

    legacy_median_p50 = statistics.median(durations)
    assert legacy_median_p50 == 150.0
    assert unified_p50 != legacy_median_p50


# --------------------------------------------------------------------------- #
# AC2 (gate-ready arm): same even-n divergence, exercised through the
# same-clock gate-ready latency decision path (the path `new_p50`/`old_p50`
# in test_p50_gate_ready_latency_not_regressed() now uses).
# --------------------------------------------------------------------------- #
def test_ac2_gate_ready_p50_even_n_divergence_uses_nearest_rank_v1():
    """GIVEN 20 gate-ready baselines whose real-clock `_gate_ready_latency_
    seconds()` output is `[100.0] * 10 + [200.0] * 10` WHEN the SAME
    `_nearest_rank_percentile()` call used at test_ci_performance_gate.py's
    gate-ready old_p50/new_p50 sites is applied THEN it returns 100.0, not
    150.0."""
    latencies = _gate_ready_fixture_latencies([100.0] * 10 + [200.0] * 10)
    assert sorted(latencies) == [100.0] * 10 + [200.0] * 10

    unified_p50 = gate._nearest_rank_percentile(latencies, 50)
    assert unified_p50 == 100.0

    legacy_median_p50 = statistics.median(latencies)
    assert legacy_median_p50 == 150.0
    assert unified_p50 != legacy_median_p50


# --------------------------------------------------------------------------- #
# AC3: 35% gate decision reversal regression.
# --------------------------------------------------------------------------- #
def test_ac3_unified_estimator_does_not_incorrectly_pass_35_percent_gate():
    """GIVEN old_samples=[100.0]*10+[200.0]*10 (legacy e2e job-duration
    baselines) AND provider_samples=[90.0]*20 (paired e2e-core/e2e-
    responsive-matrix critical-path baselines) WHEN both P50s are computed
    through their real decision-producing paths using the unified
    nearest_rank_v1 estimator THEN the relative shortening is exactly 10%
    and the AC9a >= 35% relative-shortening gate is correctly NOT passed --
    reproducing (as a regression guard) the exact gate-decision-reversal bug
    from PR #2172's deferred OWNER review finding P1-2, where the old
    (mixed-estimator) computation used statistics.median(old_samples)==150
    and incorrectly computed a 40% shortening that WOULD have passed the
    same 35% gate."""
    old_durations = _job_duration_fixture_durations([100.0] * 10 + [200.0] * 10)

    core = [
        {
            "schema": "ci_runtime_baseline_v1",
            "job": "e2e-core",
            "workflow_run_id": 7000 + i,
            "run_attempt": 1,
            "measurements": [{"phase_id": "test_e2e_ci", "elapsed_ms": 90_000, "status": 0}],
        }
        for i in range(20)
    ]
    responsive = [
        {
            "schema": "ci_runtime_baseline_v1",
            "job": "e2e-responsive-matrix",
            "workflow_run_id": 7000 + i,
            "run_attempt": 1,
            "measurements": [{"phase_id": "test_e2e_ci", "elapsed_ms": 90_000, "status": 0}],
        }
        for i in range(20)
    ]
    pairs, evidence_errors = gate._pair_by_workflow_run_id(core, responsive)
    assert evidence_errors == []
    assert len(pairs) == 20

    provider = gate._provider_critical_path_paired_p50_p95(pairs)
    assert provider is not None
    assert provider["percentile_method"] == "nearest_rank_v1"
    provider_p50 = provider["p50_seconds"]
    assert provider_p50 == 90.0

    # Unified (fixed) computation: #2180 P1 fix_delta (OWNER REQUEST_CHANGES
    # on PR #2490, issuecomment-5532831822) -- this calls the SAME pure
    # decision helper (`_legacy_e2e_vs_provider_relative_shortening`) that
    # `test_ci_performance_gate.py`'s
    # `test_p50_provider_meets_absolute_and_relative_shortening_threshold`
    # calls at its `old_p50` site, rather than this test file reconstructing
    # the percentile-then-ratio-then-threshold computation independently via
    # a direct `_nearest_rank_percentile()` call. A future change to the
    # real gate's decision function is now guaranteed to be visible here.
    unified_shortening = gate._legacy_e2e_vs_provider_relative_shortening(old_durations, provider_p50)
    unified_old_p50 = unified_shortening["old_p50_seconds"]
    assert unified_old_p50 == 100.0

    unified_relative_shortening = unified_shortening["relative_shortening"]
    assert unified_relative_shortening == 0.10
    assert not unified_shortening["meets_relative_shortening_threshold"], (
        "unified nearest_rank_v1 computation must NOT incorrectly pass the "
        "35% relative-shortening gate (#2180 AC3)"
    )

    # Regression guard: prove the PRE-#2180 mixed-estimator computation
    # (statistics.median for old_p50, nearest_rank_v1 for provider_p50)
    # would have incorrectly passed the same gate -- this is the exact bug
    # this Issue fixes, not a hypothetical.
    legacy_mixed_old_p50 = statistics.median(old_durations)
    assert legacy_mixed_old_p50 == 150.0
    legacy_mixed_relative_shortening = (legacy_mixed_old_p50 - provider_p50) / legacy_mixed_old_p50
    assert legacy_mixed_relative_shortening == 0.40
    assert legacy_mixed_relative_shortening >= gate.RELATIVE_SHORTENING_THRESHOLD, (
        "sanity check: the pre-#2180 mixed-estimator computation must "
        "reproduce the gate-decision-reversal bug this regression test "
        "guards against"
    )


# --------------------------------------------------------------------------- #
# AC4: P95 boundary.
# --------------------------------------------------------------------------- #
def test_ac4_p95_boundary_nearest_rank_v1_legacy_e2e_path():
    """GIVEN 20 legacy e2e job-duration baselines whose `_job_duration_
    seconds()` output is `[100.0] * 19 + [500.0]` WHEN P95 is computed via
    the same `_nearest_rank_percentile()` estimator THEN it returns 100.0
    (the single 500.0 outlier only affects rank 20/the true max, not P95 at
    this sample size)."""
    durations = _job_duration_fixture_durations([100.0] * 19 + [500.0])
    assert sorted(durations) == [100.0] * 19 + [500.0]

    p95 = gate._nearest_rank_percentile(durations, 95)
    assert p95 == 100.0
    assert max(durations) == 500.0


def test_ac4_p95_boundary_nearest_rank_v1_provider_critical_path_declares_estimator_version():
    """GIVEN 20 paired e2e-core/e2e-responsive-matrix baselines whose
    per-run critical-path durations are `[100.0] * 19 + [500.0]` WHEN the
    provider critical-path P95 is computed THEN it returns 100.0 AND the
    decision-producing result payload explicitly declares
    `percentile_method == "nearest_rank_v1"` (#2180 AC4's estimator-version
    declaration requirement)."""
    core = [
        {
            "schema": "ci_runtime_baseline_v1",
            "job": "e2e-core",
            "workflow_run_id": 6000 + i,
            "run_attempt": 1,
            "measurements": [{"phase_id": "test_e2e_ci", "elapsed_ms": 100_000, "status": 0}],
        }
        for i in range(20)
    ]
    responsive = [
        {
            "schema": "ci_runtime_baseline_v1",
            "job": "e2e-responsive-matrix",
            "workflow_run_id": 6000 + i,
            "run_attempt": 1,
            "measurements": [
                {
                    "phase_id": "test_e2e_ci",
                    "elapsed_ms": 500_000 if i == 19 else 100_000,
                    "status": 0,
                }
            ],
        }
        for i in range(20)
    ]
    pairs, evidence_errors = gate._pair_by_workflow_run_id(core, responsive)
    assert evidence_errors == []
    assert len(pairs) == 20

    result = gate._provider_critical_path_paired_p50_p95(pairs)
    assert result is not None
    assert result["p95_seconds"] == 100.0
    assert result["percentile_method"] == "nearest_rank_v1"


# --------------------------------------------------------------------------- #
# #2180 P1 fix_delta (OWNER REQUEST_CHANGES on PR #2490,
# issuecomment-5532831822): AC9b gate-ready before/after non-regression
# decision reversal regression, mirroring AC3's legacy/provider shortening
# decision reversal above. Calls the SAME pure decision helper
# (`_gate_ready_before_after_non_regression`) that
# `test_ci_performance_gate.py`'s `test_p50_gate_ready_latency_not_
# regressed` calls at its new_p50/old_p50 sites.
# --------------------------------------------------------------------------- #
def test_gate_ready_non_regression_decision_reversal_uses_nearest_rank_v1():
    """GIVEN old gate-ready latencies=[100.0]*10+[200.0]*10 (even-n,
    nearest_rank_v1 P50=100.0, statistics.median P50=150.0) AND new
    gate-ready latencies=[120.0]*20 (P50=120.0 under either estimator) WHEN
    the AC9b non-regression decision is computed through the real gate's
    shared `_gate_ready_before_after_non_regression` helper THEN it
    correctly detects a REGRESSION (120.0 > 100.0), reproducing (as a
    regression guard) the same class of gate-decision-reversal bug AC3
    guards against: a mixed/legacy computation using
    `statistics.median(old_latencies) == 150.0` would have INCORRECTLY
    reported no regression (120.0 <= 150.0), silently hiding a real
    latency regression from the AC9b gate."""
    old_latencies = _gate_ready_fixture_latencies([100.0] * 10 + [200.0] * 10)
    new_latencies = _gate_ready_fixture_latencies([120.0] * 20)

    non_regression = gate._gate_ready_before_after_non_regression(old_latencies, new_latencies)
    assert non_regression["old_p50_seconds"] == 100.0
    assert non_regression["new_p50_seconds"] == 120.0
    assert non_regression["non_regressed"] is False, (
        "unified nearest_rank_v1 computation must correctly DETECT the "
        "gate-ready latency regression (#2180 P1 fix_delta)"
    )

    # Regression guard: prove the pre-#2180 mixed-estimator computation
    # (statistics.median for the old arm) would have incorrectly hidden
    # this exact regression.
    legacy_mixed_old_p50 = statistics.median(old_latencies)
    assert legacy_mixed_old_p50 == 150.0
    legacy_mixed_non_regressed = non_regression["new_p50_seconds"] <= legacy_mixed_old_p50
    assert legacy_mixed_non_regressed is True, (
        "sanity check: the pre-#2180 mixed-estimator computation must "
        "reproduce the gate-decision-reversal bug this regression test "
        "guards against"
    )


# --------------------------------------------------------------------------- #
# Gate-side / validator-side semantic parity (Issue #2180 In Scope: "gate 側
# / validator 側 ... の semantic parity も同ファイルで固定する").
# --------------------------------------------------------------------------- #
def test_gate_and_validator_nearest_rank_percentile_semantic_parity():
    """GIVEN the same golden-vector sample sets used above WHEN both the
    gate's (`tests/ci/test_ci_performance_gate.py`) and the validator's
    (`validate_ci_performance_assessment_v2.py`) independently-duplicated
    `_nearest_rank_percentile()` implementations are called THEN they
    return identical values for every sample/percentile pair -- the two
    Allowed-Paths-separated implementations must not silently drift apart."""
    golden_vectors = [
        [100.0] * 10 + [200.0] * 10,
        [90.0] * 20,
        [100.0] * 19 + [500.0],
        [42.5],
        [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0],
    ]
    for values in golden_vectors:
        for percentile in (50, 95):
            gate_value = gate._nearest_rank_percentile(values, percentile)
            validator_value = validator._nearest_rank_percentile(values, percentile)
            assert gate_value == validator_value, (
                f"gate/validator nearest_rank_v1 parity broke for "
                f"values={values} percentile={percentile}: "
                f"gate={gate_value} validator={validator_value}"
            )
