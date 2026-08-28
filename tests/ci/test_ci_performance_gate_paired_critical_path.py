"""
tests/ci/test_ci_performance_gate_paired_critical_path.py

Issue #2159 AC3 (P0-3/P0-4): the post-split `e2e-core` / `e2e-responsive-matrix`
providers must be exact-paired by `workflow_run_id` and the critical-path
P50/P95 computed as `nearest_rank_v1(max(core_duration_i,
responsive_duration_i))` over those pairs -- not
`max(median(core), median(responsive))`, which mixes runs from different
`workflow_run_id`s and never reconstructs any single real run's wall-clock
critical path. Pair-missing runs must not be silently dropped from the
cohort; they must surface as explicit evidence errors.

These are fixture-driven unit tests (no live GitHub Actions history
required), consistent with Runtime Verification Applicability's
skip_conditions: cohort-dependent integration tests SKIP when no real
20-run cohort exists, but this logic itself is unit-tested with
fixture-based cohorts per the same policy's substitution instruction.
"""
from __future__ import annotations

import importlib.util
import pathlib

_MODULE_PATH = pathlib.Path(__file__).resolve().parent / "test_ci_performance_gate.py"


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("test_ci_performance_gate", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate_module()


def _baseline(job: str, workflow_run_id: int, elapsed_ms: int) -> dict:
    # #2187: `run_attempt` is set explicitly to the trusted attempt-1 value.
    # Under the gate-side missing-run_attempt trust rejection unified with
    # the collector in #2187, a fixture omitting this key would be silently
    # excluded from `_select_initial_attempt_baselines` / `_pair_by_
    # workflow_run_id`, breaking every existing assertion in this file that
    # this helper feeds (this file's own scope is paired-critical-path
    # statistics, not run_attempt trust semantics -- those are covered by
    # tests/ci/test_ci_performance_gate_rerun_attempt_trust_boundary.py).
    return {
        "schema": "ci_runtime_baseline_v1",
        "job": job,
        "workflow_run_id": workflow_run_id,
        "run_attempt": 1,
        "measurements": [{"phase_id": "test_e2e_ci", "elapsed_ms": elapsed_ms, "status": 0}],
    }


def test_provider_critical_path_paired_per_run_p50():
    """GIVEN 4 paired workflow_run_id samples of e2e-core/e2e-responsive-matrix
    WHEN the paired critical-path P50/P95 is computed
    THEN it equals nearest_rank_v1(max(core_i, responsive_i)) over the 4
    per-run maxima, NOT max(median(core), median(responsive))."""
    core = [
        _baseline("e2e-core", 1001, 100_000),
        _baseline("e2e-core", 1002, 300_000),
        _baseline("e2e-core", 1003, 100_000),
        _baseline("e2e-core", 1004, 300_000),
    ]
    responsive = [
        _baseline("e2e-responsive-matrix", 1001, 300_000),
        _baseline("e2e-responsive-matrix", 1002, 100_000),
        _baseline("e2e-responsive-matrix", 1003, 300_000),
        _baseline("e2e-responsive-matrix", 1004, 100_000),
    ]

    # Old (incorrect) formula: max(median(core), median(responsive))
    # median(core) = median([100,300,100,300]) = 200s
    # median(responsive) = median([300,100,300,100]) = 200s
    # old_provider_p50 = max(200, 200) = 200s
    #
    # Correct paired formula: median(max(core_i, responsive_i)) for i=1..4
    # per-run max = [max(100,300), max(300,100), max(100,300), max(300,100)]
    #             = [300, 300, 300, 300]
    # paired_p50 = nearest_rank_v1([300,300,300,300], 50) = 300s
    pairs, evidence_errors = gate._pair_by_workflow_run_id(core, responsive)
    assert evidence_errors == []
    assert len(pairs) == 4

    result = gate._provider_critical_path_paired_p50_p95(pairs)
    assert result is not None
    assert result["p50_seconds"] == 300.0
    assert result["p95_seconds"] == 300.0
    assert result["sample_count"] == 4
    assert result["percentile_method"] == "nearest_rank_v1"

    # Demonstrates the old formula would have (incorrectly) reported 200s,
    # a materially different (and wrong) critical-path P50.
    old_incorrect_p50 = 200.0
    assert result["p50_seconds"] != old_incorrect_p50


def test_provider_critical_path_missing_pair_reported_as_evidence_error_not_dropped():
    """GIVEN a core run with no matching responsive run for the same
    workflow_run_id WHEN pairing THEN the unpaired run is reported as an
    explicit evidence error (not silently excluded from cohort accounting)."""
    core = [
        _baseline("e2e-core", 2001, 100_000),
        _baseline("e2e-core", 2002, 100_000),
    ]
    responsive = [
        _baseline("e2e-responsive-matrix", 2001, 100_000),
        # 2002 missing on the responsive side.
    ]

    pairs, evidence_errors = gate._pair_by_workflow_run_id(core, responsive)
    assert len(pairs) == 1
    assert len(evidence_errors) == 1
    assert evidence_errors[0]["workflow_run_id"] == 2002
    assert evidence_errors[0]["reason"] == "missing_pair_e2e-responsive-matrix"


def test_provider_critical_path_p95_uses_nearest_rank_v1():
    """GIVEN 20 paired runs with one high-outlier run WHEN P95 is computed
    THEN it uses the nearest_rank_v1 method (not a naive interpolation)."""
    core = [_baseline("e2e-core", 3000 + i, 100_000) for i in range(20)]
    responsive = [_baseline("e2e-responsive-matrix", 3000 + i, 100_000) for i in range(19)]
    # One outlier run (workflow_run_id=3019) has a much longer responsive duration.
    responsive.append(_baseline("e2e-responsive-matrix", 3019, 500_000))

    pairs, evidence_errors = gate._pair_by_workflow_run_id(core, responsive)
    assert evidence_errors == []
    assert len(pairs) == 20

    result = gate._provider_critical_path_paired_p50_p95(pairs)
    assert result is not None
    per_run = sorted([100.0] * 19 + [500.0])
    assert result["p50_seconds"] == gate._nearest_rank_percentile(per_run, 50)
    assert result["p95_seconds"] == gate._nearest_rank_percentile(per_run, 95)
    # nearest_rank_v1(95%, n=20): rank = ceil(0.95 * 20) = 19 -> the 19th
    # smallest of the 20 sorted values (19x100 + 1x500) is still 100 -- the
    # single 500s outlier only affects rank 20 (the true max), not P95 at
    # this sample size. This assertion intentionally locks in that exact
    # nearest_rank_v1 semantic (not "P95 == max whenever there's an
    # outlier", a common percentile-method confusion).
    assert result["p95_seconds"] == 100.0
    assert max(per_run) == 500.0
