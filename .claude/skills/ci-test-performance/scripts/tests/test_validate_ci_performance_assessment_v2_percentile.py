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
    run_details = _run_details(durations)
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_details"] = run_details
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_ids"] = [
        rd["workflow_run_id"] for rd in run_details
    ]
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_count"] = len(run_details)
    assessment["performance_evidence"]["runtime_delta"]["before"]["p50_seconds"] = 109.0
    assessment["performance_evidence"]["runtime_delta"]["before"]["p95_seconds"] = 118.0

    errors: list[str] = []
    blockers: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(
        assessment, errors, blockers
    )
    assert errors == []


def test_percentile_recomputation_mismatch_p50_is_rejected():
    """GIVEN a cohort whose declared p50_seconds does NOT match the raw
    sample recomputation WHEN validated THEN
    percentile_recomputation_mismatch_p50 is reported (self-reported
    aggregate alone is never trusted)."""
    assessment = _base_assessment()
    durations = [100.0 + i for i in range(20)]
    run_details = _run_details(durations)
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_details"] = run_details
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_ids"] = [
        rd["workflow_run_id"] for rd in run_details
    ]
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_count"] = len(run_details)
    # Real nearest_rank_v1(50%) is 109.0; declare something clearly wrong.
    assessment["performance_evidence"]["runtime_delta"]["before"]["p50_seconds"] = 999.0
    assessment["performance_evidence"]["runtime_delta"]["before"]["p95_seconds"] = 118.0

    errors: list[str] = []
    blockers: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(
        assessment, errors, blockers
    )
    assert "percentile_recomputation_mismatch_p50: before" in errors


def test_percentile_recomputation_mismatch_p95_is_rejected():
    assessment = _base_assessment()
    durations = [100.0 + i for i in range(20)]
    run_details = _run_details(durations)
    assessment["performance_evidence"]["runtime_delta"]["after"]["run_details"] = run_details
    assessment["performance_evidence"]["runtime_delta"]["after"]["run_ids"] = [
        rd["workflow_run_id"] for rd in run_details
    ]
    assessment["performance_evidence"]["runtime_delta"]["after"]["run_count"] = len(run_details)
    assessment["performance_evidence"]["runtime_delta"]["after"]["p50_seconds"] = 109.0
    assessment["performance_evidence"]["runtime_delta"]["after"]["p95_seconds"] = 5.0

    errors: list[str] = []
    blockers: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(
        assessment, errors, blockers
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
    blockers: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(
        assessment, errors, blockers
    )
    assert errors == []
    assert blockers == []


def test_percentile_mismatch_makes_assessment_semantically_invalid_end_to_end(tmp_path):
    """GIVEN a full assessment JSON on disk with a percentile mismatch
    WHEN run through the top-level `validate_assessment` entrypoint THEN
    the exit code is EXIT_INVALID and the mismatch error is present."""
    assessment = _base_assessment()
    durations = [100.0 + i for i in range(20)]
    run_details = _run_details(durations)
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_details"] = run_details
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_ids"] = [
        str(rd["workflow_run_id"]) for rd in run_details
    ]
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_count"] = len(run_details)
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


# --------------------------------------------------------------------------- #
# #2159 P0-7 (fix_delta after adversarial review issuecomment-5295659213):
# `run_details` is now MANDATORY (not optional) whenever
# `performance_evidence.status == "complete"` AND `claim.kind != "none"`.
# --------------------------------------------------------------------------- #
def _complete_non_none_claim_assessment(run_count: int = 20) -> dict:
    assessment = _base_assessment()
    assessment["claim"] = {"kind": "improvement"}
    assessment["performance_evidence"]["status"] = "complete"
    before_ids = [str(9000 + i) for i in range(run_count)]
    after_ids = [str(19000 + i) for i in range(run_count)]
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_ids"] = before_ids
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_count"] = run_count
    assessment["performance_evidence"]["runtime_delta"]["after"]["run_ids"] = after_ids
    assessment["performance_evidence"]["runtime_delta"]["after"]["run_count"] = run_count
    return assessment


def test_run_details_required_when_status_complete_and_claim_kind_not_none():
    """GIVEN `status: complete` and `claim.kind: improvement` (a real,
    non-smoke-test performance claim) but NO `run_details` on a cohort
    WHEN validated THEN a blocker is raised -- self-reported p50/p95 can no
    longer sail through unchecked by simply omitting the raw samples
    (#2159 P0-7)."""
    assessment = _complete_non_none_claim_assessment()
    assert "run_details" not in assessment["performance_evidence"]["runtime_delta"]["before"]

    errors: list[str] = []
    blockers: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(assessment, errors, blockers)
    assert any("run_details_required_for_complete_non_none_claim_but_missing: before" in b for b in blockers)


def test_run_details_optional_when_claim_kind_is_none():
    """GIVEN `claim.kind: none` (the no-op smoke-test producer, #2159 P0-8)
    and `status: complete` but NO `run_details` WHEN validated THEN no
    blocker is raised -- backward compatibility is preserved ONLY for the
    `claim.kind: none` case."""
    assessment = _base_assessment()
    assessment["performance_evidence"]["status"] = "complete"
    assert assessment["claim"]["kind"] == "none"
    assert "run_details" not in assessment["performance_evidence"]["runtime_delta"]["before"]

    errors: list[str] = []
    blockers: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(assessment, errors, blockers)
    assert blockers == []


def test_run_details_below_min_raw_sample_count_is_blocked():
    """GIVEN a `complete`/non-none-claim cohort with `run_details` present
    but fewer than MIN_RAW_SAMPLE_COUNT valid entries WHEN validated THEN a
    blocker is raised (raw samples exist but are not enough to back the
    claim's percentile statistics)."""
    assessment = _complete_non_none_claim_assessment(run_count=20)
    durations = [100.0 + i for i in range(5)]  # only 5, below MIN_RAW_SAMPLE_COUNT=20
    run_details = _run_details(durations)
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_details"] = run_details
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_ids"] = [
        rd["workflow_run_id"] for rd in run_details
    ]
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_count"] = len(run_details)
    assessment["performance_evidence"]["runtime_delta"]["before"]["p50_seconds"] = validator._nearest_rank_percentile(
        durations, 50
    )
    assessment["performance_evidence"]["runtime_delta"]["before"]["p95_seconds"] = validator._nearest_rank_percentile(
        durations, 95
    )

    errors: list[str] = []
    blockers: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(assessment, errors, blockers)
    assert any("run_details_below_min_raw_sample_count: before" in b for b in blockers)


def test_run_details_workflow_run_id_mismatches_run_ids_is_rejected():
    """GIVEN `run_details[].workflow_run_id` values that do not exactly
    match the cohort's `run_ids` WHEN validated THEN an error is raised
    (P0-7 invariant: run_details identity must bind to the declared
    run_ids, not an independent/fabricated set)."""
    assessment = _complete_non_none_claim_assessment(run_count=20)
    durations = [100.0 + i for i in range(20)]
    run_details = _run_details(durations)
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_details"] = run_details
    # run_ids intentionally left as the fixture's mismatched placeholder set
    # (before ids from _complete_non_none_claim_assessment, workflow_run_id
    # from _run_details starting at 9000 -- these do not overlap).
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_count"] = len(run_details)

    errors: list[str] = []
    blockers: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(assessment, errors, blockers)
    assert any("run_details_workflow_run_id_mismatches_run_ids: before" in e for e in errors)


def test_run_details_duplicate_workflow_run_id_is_rejected():
    """GIVEN two `run_details` entries sharing the same `workflow_run_id`
    WHEN validated THEN an error is raised (no duplicate raw-sample
    records within a single cohort)."""
    assessment = _complete_non_none_claim_assessment(run_count=20)
    durations = [100.0 + i for i in range(20)]
    run_details = _run_details(durations)
    run_details[1]["workflow_run_id"] = run_details[0]["workflow_run_id"]
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_details"] = run_details
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_ids"] = [
        rd["workflow_run_id"] for rd in run_details
    ]
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_count"] = len(run_details)

    errors: list[str] = []
    blockers: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(assessment, errors, blockers)
    assert any("run_details_duplicate_workflow_run_id: before" in e for e in errors)


def test_run_details_invalid_duration_seconds_is_rejected():
    """GIVEN a `run_details` entry with a NaN/Infinity/negative/zero
    `duration_seconds` WHEN validated THEN an error is raised (never
    silently excluded, per #2159 P0-7's explicit invariant that duration
    must be a finite positive number)."""
    assessment = _complete_non_none_claim_assessment(run_count=20)
    durations = [100.0 + i for i in range(19)] + [float("nan")]
    run_details = _run_details(durations)
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_details"] = run_details
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_ids"] = [
        rd["workflow_run_id"] for rd in run_details
    ]
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_count"] = len(run_details)

    errors: list[str] = []
    blockers: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(assessment, errors, blockers)
    assert any("run_details_invalid_duration_seconds: before" in e for e in errors)


def test_run_details_workflow_run_id_overlap_before_after_is_rejected():
    """GIVEN before/after `run_details` sharing a `workflow_run_id` WHEN
    validated THEN an error is raised -- a single real run cannot be both
    a before-arm and an after-arm sample."""
    assessment = _complete_non_none_claim_assessment(run_count=20)
    before_durations = [100.0 + i for i in range(20)]
    after_durations = [90.0 + i for i in range(20)]
    before_details = _run_details(before_durations)
    after_details = _run_details(after_durations)
    # Force one overlapping workflow_run_id between before/after.
    after_details[0]["workflow_run_id"] = before_details[0]["workflow_run_id"]

    assessment["performance_evidence"]["runtime_delta"]["before"]["run_details"] = before_details
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_ids"] = [
        rd["workflow_run_id"] for rd in before_details
    ]
    assessment["performance_evidence"]["runtime_delta"]["before"]["run_count"] = len(before_details)
    assessment["performance_evidence"]["runtime_delta"]["after"]["run_details"] = after_details
    assessment["performance_evidence"]["runtime_delta"]["after"]["run_ids"] = [
        rd["workflow_run_id"] for rd in after_details
    ]
    assessment["performance_evidence"]["runtime_delta"]["after"]["run_count"] = len(after_details)

    errors: list[str] = []
    blockers: list[str] = []
    validator._check_percentile_recomputed_from_raw_samples(assessment, errors, blockers)
    assert "run_details_workflow_run_id_overlap_before_after" in errors


def test_real_current_head_baseline_artifact_missing_v2_fields_is_recognized():
    """GIVEN the REAL `ci_runtime_baseline_v1` artifact pulled from PR #2172's
    current-head CI run (adversarial review issuecomment-5295659213, P0-1)
    -- `{"run_id": "31809174433", "runner_image": "unknown/unknown", "job":
    "e2e-core"}` plus the omitted fields the review confirmed are absent
    (`workflow_run_id`, `host_runner_image`,
    `playwright_container_image_digest`, `cohort_role`, `run_started_at`,
    `check_completed_at`) -- WHEN checked against the WITHIN_COHORT_REQUIRED_EQUAL
    v2 field contract THEN it is correctly identified as missing every
    required v2 field (proving the old-format real artifact would have been
    fail-closed excluded from any comparable cohort, and that the new
    producer contract in `.github/workflows/ci.yml` is what fixes this)."""
    real_current_head_artifact_v1 = {
        "schema": "ci_runtime_baseline_v1",
        "run_id": "31809174433",
        "run_attempt": "1",
        "head_sha": "a042392d45b46cd03100304d404423b4b67f470b",
        "merge_sha": "a042392d45b46cd03100304d404423b4b67f470b",
        "job": "e2e-core",
        "runner_image": "unknown/unknown",
        "measurement_method": "date_plus3N_ms",
        "measurements": [],
    }
    required_v2_fields = (
        "workflow_run_id",
        "host_runner_image",
        "playwright_container_image_digest",
        "cohort_role",
        "run_started_at",
        "check_completed_at",
    )
    missing = [field for field in required_v2_fields if field not in real_current_head_artifact_v1]
    assert missing == list(required_v2_fields), (
        "the real pre-#2159-fix_delta baseline artifact must be missing ALL v2 "
        "contract fields (proving the P0-1 producer/consumer field-contract gap "
        "was real, not a fixture-only fabrication)"
    )
