"""
.claude/skills/ci-test-performance/scripts/tests/test_validate_ci_performance_assessment_v2_build_from_cohorts.py

Issue #2159 AC10 (P0-8, fix_delta after adversarial review
issuecomment-5295659213): `build_assessment_from_percentile_cohorts` is the
REAL, non-no-op CI_TEST_PERFORMANCE_ASSESSMENT_V2 producer -- distinct from
the fixed `claim.kind: none` smoke-test payload every regular CI run emits.
It computes an actual claim from real before/after raw duration samples.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
VALIDATOR_PATH = SCRIPT_DIR / "validate_ci_performance_assessment_v2.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_ci_performance_assessment_v2", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


validator = _load_validator()


def _run_details(durations: list[float], start_id: int) -> list[dict]:
    return [
        {
            "run_id": f"run-{start_id + i}",
            "workflow_run_id": start_id + i,
            "run_attempt": 1,
            "commit_sha": "a" * 40,
            "conclusion": "success",
            "duration_seconds": duration,
        }
        for i, duration in enumerate(durations)
    ]


_COHORT_PROVENANCE = {
    "runner_image": "ubuntu-24.04/20260701.1",
    "workers": 4,
    "scheduler": "loadscope",
    "command_manifest_digest": "sha256:" + "a" * 64,
    "test_selection_digest": "sha256:" + "b" * 64,
}


_FUNCTIONAL_EVIDENCE = {
    "proof_level": "check_run_only",
    "coverage_bound": False,
    "ci_verdict_summary_ref": {
        "artifact_schema": "ci_verdict_summary_v2",
        "expected_head_sha": "b" * 40,
        "overall_status": "merge_ready",
        "selected_checks": [
            {
                "check_run_id": 1,
                "status": "completed",
                "conclusion": "success",
                "head_sha_match": True,
                "classification": "required",
            }
        ],
    },
}


def test_build_from_cohorts_computes_improvement_claim_from_real_shortening():
    """GIVEN before durations with P50=270s and after durations with
    P50=100s (>= 35% shortening) WHEN
    `build_assessment_from_percentile_cohorts` runs THEN it emits
    `claim.kind: improvement`, `observation.outcome: improved`, and the
    P50/P95 are the REAL nearest_rank_v1 recomputation of the raw samples
    (not a self-reported number)."""
    before = [270.0] * 20
    after = [100.0] * 20

    assessment = validator.build_assessment_from_percentile_cohorts(
        issue_number=2159,
        pr_number=2172,
        measured_at="2026-08-15T00:00:00Z",
        before_run_details=_run_details(before, 9000),
        after_run_details=_run_details(after, 19000),
        functional_evidence=_FUNCTIONAL_EVIDENCE,
        declared_impact="E2E provider critical path P50 shortened via lane split.",
        risk_acknowledgement={
            "reference": {"source_kind": "issue_comment", "source_id": "issuecomment-5295659213"},
            "verification_status": "unverified",
        },
        cohort_provenance=_COHORT_PROVENANCE,
    )

    assert assessment["claim"]["kind"] == "improvement"
    assert assessment["observation"]["outcome"] == "improved"
    assert assessment["performance_evidence"]["status"] == "complete"
    assert assessment["performance_evidence"]["runtime_delta"]["before"]["p50_seconds"] == 270.0
    assert assessment["performance_evidence"]["runtime_delta"]["after"]["p50_seconds"] == 100.0


def test_build_from_cohorts_computes_regression_claim_when_after_is_slower():
    before = [100.0] * 20
    after = [150.0] * 20

    assessment = validator.build_assessment_from_percentile_cohorts(
        issue_number=2159,
        pr_number=2172,
        measured_at="2026-08-15T00:00:00Z",
        before_run_details=_run_details(before, 9000),
        after_run_details=_run_details(after, 19000),
        functional_evidence=_FUNCTIONAL_EVIDENCE,
        declared_impact="regression fixture",
        risk_acknowledgement={
            "reference": {"source_kind": "issue_comment", "source_id": "issuecomment-5295659213"},
            "verification_status": "unverified",
        },
        cohort_provenance=_COHORT_PROVENANCE,
    )
    # The schema's Claim.kind enum has no "regression" value -- a
    # regression surfaces as a failed non_regression claim
    # (observation.outcome: regressed), never a fabricated claim kind.
    assert assessment["claim"]["kind"] == "non_regression"
    assert assessment["observation"]["outcome"] == "regressed"


def test_build_from_cohorts_raises_on_insufficient_raw_samples():
    """GIVEN fewer than MIN_RAW_SAMPLE_COUNT valid durations WHEN
    `build_assessment_from_percentile_cohorts` runs THEN it raises
    ValueError -- it never fabricates a claim from insufficient evidence,
    unlike the pre-P0-8 no-op producer which always emits SOMETHING
    regardless of what data (if any) actually exists."""
    import pytest

    before = [100.0] * 5  # below MIN_RAW_SAMPLE_COUNT=20
    after = [100.0] * 20

    with pytest.raises(ValueError, match="insufficient raw samples"):
        validator.build_assessment_from_percentile_cohorts(
            issue_number=2159,
            pr_number=2172,
            measured_at="2026-08-15T00:00:00Z",
            before_run_details=_run_details(before, 9000),
            after_run_details=_run_details(after, 19000),
            functional_evidence=_FUNCTIONAL_EVIDENCE,
            declared_impact="insufficient fixture",
            risk_acknowledgement={
                "reference": {"source_kind": "issue_comment", "source_id": "issuecomment-5295659213"},
                "verification_status": "unverified",
            },
            cohort_provenance=_COHORT_PROVENANCE,
        )


def test_build_from_cohorts_output_passes_full_structural_and_semantic_validation(tmp_path):
    """GIVEN a real-shaped assessment built by
    `build_assessment_from_percentile_cohorts` WHEN written to disk and run
    through the top-level `validate_assessment` entrypoint (structural +
    semantic + the P0-7 mandatory-run_details invariants, since
    `claim.kind != none`) THEN it is structurally valid AND passes every
    P0-7 raw-sample invariant (run_count/run_ids/duplicate/finite-positive-
    duration/percentile-recomputation) with zero errors -- this producer's
    own output is never rejected by this file's own P0-7 hardening."""
    before = [270.0 + i for i in range(20)]
    after = [100.0 + i for i in range(20)]

    assessment = validator.build_assessment_from_percentile_cohorts(
        issue_number=2159,
        pr_number=2172,
        measured_at="2026-08-15T00:00:00Z",
        before_run_details=_run_details(before, 9000),
        after_run_details=_run_details(after, 19000),
        functional_evidence=_FUNCTIONAL_EVIDENCE,
        declared_impact="end-to-end structural validation fixture",
        risk_acknowledgement={
            "reference": {"source_kind": "issue_comment", "source_id": "issuecomment-5295659213"},
            "verification_status": "unverified",
        },
        cohort_provenance=_COHORT_PROVENANCE,
    )

    import json

    assessment_path = tmp_path / "assessment.json"
    assessment_path.write_text(json.dumps(assessment), encoding="utf-8")

    exit_code, decision = validator.validate_assessment(str(assessment_path))
    assert exit_code == validator.EXIT_VALID, decision
    assert decision["errors"] == []
