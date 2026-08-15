"""
tests/ci/test_ci_performance_gate_post_filter_sample_floor.py

Issue #2159 P0-6 (fix_delta after adversarial review issuecomment-5295659213):
the >= MIN_COHORT_RUN_COUNT sample floor must be re-validated AFTER exact
pairing and duration/timestamp filtering, not only checked once on the raw
pre-filter per-job baseline counts. This file proves the specific attack the
review describes:

    1. core: 20 samples
    2. responsive: 20 samples
    3. all 20 workflow_run_ids pair exactly (no evidence_errors)
    4. 19 of the 20 pairs are missing a real `elapsed_ms` measurement on one
       side (e.g. a spec-shard captured "0 tests ran" or the phase never
       emitted)
    5. only 1 pair has a real duration on both sides
    6. WITHOUT a post-filter re-check, a P50/P95 would silently be computed
       from n=1 while claiming n>=20-backed evidence.

And the same attack shape for gate-ready latency (missing
run_started_at/check_completed_at after pairing looks like >= 20 raw
baselines but collapses to far fewer valid-timestamp samples).
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_MODULE_PATH = pathlib.Path(__file__).resolve().parent / "test_ci_performance_gate.py"


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("test_ci_performance_gate", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate_module()


def _baseline_with_duration(job: str, workflow_run_id: int, elapsed_ms: int | None) -> dict:
    measurements = [] if elapsed_ms is None else [{"phase_id": "test_e2e_ci", "elapsed_ms": elapsed_ms, "status": 0}]
    return {
        "schema": "ci_runtime_baseline_v1",
        "job": job,
        "workflow_run_id": workflow_run_id,
        "measurements": measurements,
    }


def _baseline_with_clock(role_id: int, run_started_at: str | None, check_completed_at: str | None) -> dict:
    baseline: dict = {"schema": "ci_runtime_baseline_v1", "job": "e2e", "workflow_run_id": role_id}
    if run_started_at is not None:
        baseline["run_started_at"] = run_started_at
    if check_completed_at is not None:
        baseline["check_completed_at"] = check_completed_at
    return baseline


def test_20_paired_but_1_valid_duration_fails_post_filter_floor():
    """GIVEN 20 core + 20 responsive baselines that all pair exactly by
    `workflow_run_id`, but only 1 of the 20 pairs has a real duration
    measurement on BOTH sides, WHEN the post-filter sample count is computed
    THEN it is 1 (NOT 20), and the AC11 hard-check raises
    EvidenceInsufficientError rather than silently reporting a
    >=20-backed P50/P95."""
    core = [_baseline_with_duration("e2e-core", i, 100_000) for i in range(20)]
    responsive = [
        _baseline_with_duration("e2e-responsive-matrix", i, 100_000 if i == 0 else None) for i in range(20)
    ]

    post_filter_count, evidence_errors = gate._provider_post_filter_sample_count(core, responsive)
    assert evidence_errors == [], "all 20 workflow_run_ids pair exactly; there must be no pairing errors"
    assert post_filter_count == 1, (
        "19 of the 20 exact pairs are missing a real duration measurement on the "
        "responsive side; the post-filter count must reflect that collapse, not "
        "the raw 20-pair count"
    )

    with pytest.raises(gate.EvidenceInsufficientError) as exc_info:
        gate._evidence_readiness_hard_check_post_filter(
            provider_post_filter_count=post_filter_count,
            provider_evidence_errors=evidence_errors,
            gate_ready_post_filter_counts={},
        )
    assert "provider_post_filter_sample_count" in str(exc_info.value)


def test_20_baselines_but_1_valid_timestamp_fails_post_filter_floor():
    """GIVEN 20 baselines for a gate-ready-latency arm, but only 1 has both
    `run_started_at` and `check_completed_at` populated, WHEN the post-filter
    sample count is computed THEN it is 1, and the AC11 hard-check raises
    EvidenceInsufficientError."""
    baselines = [
        _baseline_with_clock(i, "2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z")
        if i == 0
        else _baseline_with_clock(i, "2026-01-01T00:00:00Z", None)
        for i in range(20)
    ]

    post_filter_count = gate._gate_ready_post_filter_sample_count(baselines)
    assert post_filter_count == 1

    with pytest.raises(gate.EvidenceInsufficientError) as exc_info:
        gate._evidence_readiness_hard_check_post_filter(
            provider_post_filter_count=gate.MIN_COHORT_RUN_COUNT,
            provider_evidence_errors=[],
            gate_ready_post_filter_counts={"after": post_filter_count},
        )
    assert "gate_ready_post_filter_sample_count[after]" in str(exc_info.value)


def test_post_filter_floor_silent_when_all_counts_sufficient():
    """GIVEN post-filter counts that all meet MIN_COHORT_RUN_COUNT WHEN the
    hard-check runs THEN it returns None without raising (no false
    positives)."""
    assert (
        gate._evidence_readiness_hard_check_post_filter(
            provider_post_filter_count=gate.MIN_COHORT_RUN_COUNT,
            provider_evidence_errors=[],
            gate_ready_post_filter_counts={
                "before": gate.MIN_COHORT_RUN_COUNT,
                "after": gate.MIN_COHORT_RUN_COUNT,
            },
        )
        is None
    )


def test_post_filter_floor_rejects_nonempty_provider_evidence_errors_even_if_count_sufficient():
    """GIVEN a post-filter count that meets the floor but pairing produced
    evidence_errors (unpaired runs) WHEN the hard-check runs THEN it still
    raises -- a sufficient post-filter COUNT does not excuse leaving unpaired
    runs unexplained."""
    with pytest.raises(gate.EvidenceInsufficientError) as exc_info:
        gate._evidence_readiness_hard_check_post_filter(
            provider_post_filter_count=gate.MIN_COHORT_RUN_COUNT,
            provider_evidence_errors=[{"workflow_run_id": 999, "reason": "missing_pair_e2e-core"}],
            gate_ready_post_filter_counts={},
        )
    assert "provider_evidence_errors" in str(exc_info.value)
