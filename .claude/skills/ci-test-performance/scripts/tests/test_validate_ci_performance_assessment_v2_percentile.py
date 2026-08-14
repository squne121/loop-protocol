"""
.claude/skills/ci-test-performance/scripts/tests/test_validate_ci_performance_assessment_v2_percentile.py

Issue #2159 AC8 (P0-9): `validate_ci_performance_assessment_v2.py` must
recompute P50/P95 from `runtime_delta.<cohort>.run_details[].duration_seconds`
raw samples and cross-check against the cohort's self-reported
`p50_seconds` / `p95_seconds`, rather than trusting the self-reported
aggregate values alone (the pre-existing `_recompute_delta` check only
cross-checks the DELTA between two already-aggregated p50/p95 values, it
never verifies those aggregate values themselves were honestly derived
from raw per-run samples).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
VALIDATOR_PATH = SCRIPT_DIR / "validate_ci_performance_assessment_v2.py"
FIXTURE_DIR = SCRIPT_DIR / "fixtures"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_ci_performance_assessment_v2", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


validator = _load_validator()


def _base_assessment() -> dict:
    return json.loads(
        (FIXTURE_DIR / "valid_insufficient_samples_structurally_valid_gate_blocked.json").read_text(
            encoding="utf-8"
        )
    )


def _run_details(durations: list[float], commit_sha: str = "a" * 40) -> list[dict]:
    return [
        {
            "run_id": f"run-{i}",
            "workflow_run_id": 9000 + i,
            "run_attempt": 1,
            "commit_sha": commit_sha,
            "conclusion": "success",
            "duration_seconds": duration,
        }
        for i, duration in enumerate(durations)
    ]


def test_percentile_recomputed_from_raw_samples():
    """GIVEN a cohort whose `run_details[].duration_seconds` raw samples
    recompute (via nearest_rank_v1) to EXACTLY the declared p50_seconds /
    p95_seconds WHEN validated THEN no percentile_recomputation_mismatch_*
    error is raised."""
    assessment = _base_assessment()
    # 20 raw samples: nearest_rank_v1(50%, n=20) -> rank=10 (0-indexed 9);
    # nearest_rank_v1(95%, n=20) -> rank=19 (0-indexed 18).
    durations = [100.0 + i for i in range(20)]  # 100..119
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_details"] = _run_details(durations)
    assessment["performance_evidence"]["runtime_delta"]["before"]["p50_seconds"] = 109.0
    assessment["performance_evidence"]["runtime_delta"]["before"]["p95_seconds"] = 118.0

    errors: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(
        assessment["performance_evidence"], errors
    )
    assert errors == []


def test_percentile_recomputation_mismatch_p50_is_rejected():
    """GIVEN a cohort whose declared p50_seconds does NOT match the raw
    sample recomputation WHEN validated THEN
    percentile_recomputation_mismatch_p50 is reported (self-reported
    aggregate alone is never trusted)."""
    assessment = _base_assessment()
    durations = [100.0 + i for i in range(20)]
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_details"] = _run_details(durations)
    # Real nearest_rank_v1(50%) is 109.0; declare something clearly wrong.
    assessment["performance_evidence"]["runtime_delta"]["before"]["p50_seconds"] = 999.0
    assessment["performance_evidence"]["runtime_delta"]["before"]["p95_seconds"] = 118.0

    errors: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(
        assessment["performance_evidence"], errors
    )
    assert "percentile_recomputation_mismatch_p50: before" in errors


def test_percentile_recomputation_mismatch_p95_is_rejected():
    assessment = _base_assessment()
    durations = [100.0 + i for i in range(20)]
    assessment["performance_evidence"]["runtime_delta"]["after"]["run_details"] = _run_details(durations)
    assessment["performance_evidence"]["runtime_delta"]["after"]["p50_seconds"] = 109.0
    assessment["performance_evidence"]["runtime_delta"]["after"]["p95_seconds"] = 5.0

    errors: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(
        assessment["performance_evidence"], errors
    )
    assert "percentile_recomputation_mismatch_p95: after" in errors


def test_missing_run_details_does_not_penalize_cohort():
    """GIVEN a cohort with no `run_details` (optional field, per
    schemas/ci_runtime_delta_v2.schema.json) WHEN validated THEN no
    percentile_recomputation_mismatch_* error is raised -- backward
    compatible with assessments that only report pre-aggregated
    statistics."""
    assessment = _base_assessment()
    assert "run_details" not in assessment["performance_evidence"]["runtime_delta"]["before"]

    errors: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(
        assessment["performance_evidence"], errors
    )
    assert errors == []


def test_percentile_mismatch_makes_assessment_semantically_invalid_end_to_end(tmp_path):
    """GIVEN a full assessment JSON on disk with a percentile mismatch
    WHEN run through the top-level `validate_assessment` entrypoint THEN
    the exit code is EXIT_INVALID and the mismatch error is present."""
    assessment = _base_assessment()
    durations = [100.0 + i for i in range(20)]
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_details"] = _run_details(durations)
    assessment["performance_evidence"]["runtime_delta"]["before"]["p50_seconds"] = 999.0

    assessment_path = tmp_path / "percentile_mismatch_assessment.json"
    assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

    exit_code, decision = validator.validate_assessment(str(assessment_path))
    assert exit_code == validator.EXIT_INVALID
    assert "percentile_recomputation_mismatch_p50: before" in decision["errors"]


def test_nearest_rank_percentile_matches_documented_semantics():
    """GIVEN a known sample set WHEN `_nearest_rank_percentile` is called
    THEN it matches the documented 1-indexed nearest-rank formula
    (rank = ceil(pct/100 * n))."""
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert validator._nearest_rank_percentile(values, 50) == 30.0  # rank=3
    assert validator._nearest_rank_percentile(values, 95) == 50.0  # rank=5 (ceil(4.75)=5)
    with pytest.raises(ValueError):
        validator._nearest_rank_percentile([], 50)
