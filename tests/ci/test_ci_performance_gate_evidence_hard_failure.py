"""
tests/ci/test_ci_performance_gate_evidence_hard_failure.py

Issue #2159 AC11 (P1-6): a dedicated close-verification path must exist
that produces a non-zero exit code when comparable-cohort evidence is
insufficient -- `pytest.skip()` (exit 0) must never be the sole mechanism
used for a close condition (SKIP is correct for the EXPLORATORY
integration tests in test_ci_performance_gate.py, which legitimately run
before real CI history exists; it is NOT correct for a final
close-readiness gate, which must fail loudly when evidence is missing).

This file demonstrates the mechanism two ways:
1. A direct pytest.raises() unit test proving `_evidence_readiness_hard_check`
   raises `EvidenceInsufficientError` (not a silent skip/pass) when
   evidence is insufficient, and does nothing when sufficient.
2. A subprocess-level test proving a real Python process invocation of the
   hard-check path terminates with a non-zero exit code (not 0, and not a
   pytest "no tests collected" exit 5) when evidence is insufficient --
   guarding against a hollow implementation where the exception exists but
   nothing outside the pytest process actually observes a hard failure.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

_MODULE_PATH = pathlib.Path(__file__).resolve().parent / "test_ci_performance_gate.py"


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("test_ci_performance_gate", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate_module()


def _cohort_with_counts(counts: dict[str, int]) -> dict[str, list[dict]]:
    return {job: [{"workflow_run_id": i} for i in range(count)] for job, count in counts.items()}


def test_evidence_incomplete_produces_hard_failure_not_skip():
    """GIVEN a cohort with fewer than MIN_COHORT_RUN_COUNT samples for a
    required job WHEN the AC11 hard-check runs THEN it raises
    `EvidenceInsufficientError` (a real exception the caller must handle
    or propagate as a non-zero exit) rather than the test silently
    skipping or passing."""
    insufficient_cohort = _cohort_with_counts({"e2e-core": 5, "e2e-responsive-matrix": 20})

    with pytest.raises(gate.EvidenceInsufficientError) as exc_info:
        gate._evidence_readiness_hard_check(
            insufficient_cohort, ("e2e-core", "e2e-responsive-matrix")
        )
    assert "e2e-core" in str(exc_info.value)


def test_evidence_complete_hard_check_is_silent():
    """GIVEN a cohort meeting MIN_COHORT_RUN_COUNT for every required job
    WHEN the AC11 hard-check runs THEN it returns None without raising."""
    sufficient_cohort = _cohort_with_counts(
        {"e2e-core": gate.MIN_COHORT_RUN_COUNT, "e2e-responsive-matrix": gate.MIN_COHORT_RUN_COUNT}
    )
    assert (
        gate._evidence_readiness_hard_check(
            sufficient_cohort, ("e2e-core", "e2e-responsive-matrix")
        )
        is None
    )


def test_evidence_incomplete_subprocess_exits_non_zero_not_skip_exit_code():
    """Subprocess-level proof (guards against a hollow in-process-only
    implementation, per repo policy on behavioral verification): a real
    `python3 -c` child process that invokes the AC11 hard-check with
    insufficient evidence and does not catch the exception terminates with
    a non-zero exit code that is neither 0 (success/SKIP-equivalent) nor
    pytest's own exit code 5 (no tests collected, which would be a false
    signal of hard-failure machinery)."""
    driver = f"""
import importlib.util
spec = importlib.util.spec_from_file_location(
    "test_ci_performance_gate", {str(_MODULE_PATH)!r}
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
cohort = {{"e2e-core": [{{"workflow_run_id": i}} for i in range(3)]}}
mod._evidence_readiness_hard_check(cohort, ("e2e-core",))
"""
    result = subprocess.run(
        [sys.executable, "-c", driver],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert result.returncode != 5
    assert "EvidenceInsufficientError" in result.stderr


# --------------------------------------------------------------------------- #
# #2159 OWNER scope-authority ruling (issuecomment-5299412215, items 2/P0-8
# and 3/P1-3/AC11): `run_evidence_gate` / `_cli_main` are the REAL,
# production-callable wiring of the AC11 hard-check + the P0-8
# `build_assessment_from_percentile_cohorts` producer -- these tests prove
# BOTH the in-process function AND a real subprocess invocation of
# `tests/ci/test_ci_performance_gate.py` as a CLI script (the same module
# `.github/workflows/ci.yml`'s `e2e-performance-benchmark-assessment-gate`
# job invokes) behave correctly for both the insufficient-evidence (current,
# expected state -- no real >= 20-run cohort exists yet) and
# sufficient-evidence (future, once #2155-era real data accumulates) cases.
# --------------------------------------------------------------------------- #
def _paired_baselines(count: int, job: str, start_id: int = 1, base_ms: int = 60_000) -> list[dict]:
    baselines = []
    for i in range(count):
        run_id = start_id + i
        baselines.append(
            {
                "workflow_run_id": run_id,
                "job": job,
                "measurements": [{"phase_id": "test_e2e_core", "elapsed_ms": base_ms + i}],
            }
        )
    return baselines


def _gate_ready_baselines(count: int, start_id: int = 1) -> list[dict]:
    baselines = []
    for i in range(count):
        baselines.append(
            {
                "workflow_run_id": start_id + i,
                "run_started_at": "2026-08-15T00:00:00Z",
                "check_completed_at": "2026-08-15T00:05:00Z",
            }
        )
    return baselines


def _arm_fixture(
    commit_sha: str, provider_count: int, gate_ready_count: int, start_id: int, base_ms: int = 60_000
) -> dict:
    return {
        "commit_sha": commit_sha,
        "core_baselines": _paired_baselines(provider_count, "e2e-core", start_id, base_ms=base_ms),
        "responsive_baselines": _paired_baselines(
            provider_count, "e2e-responsive-matrix", start_id, base_ms=base_ms
        ),
        "gate_ready_baselines": _gate_ready_baselines(gate_ready_count, start_id),
    }


_COHORT_FIXTURE_COMMON = {
    "issue_number": 2159,
    "pr_number": 2172,
    "measured_at": "2026-08-15T00:00:00Z",
    "functional_evidence": {
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
    },
    "declared_impact": "E2E provider critical path P50 shortened via lane split (#2159 evidence gate fixture).",
    "risk_acknowledgement": {
        "reference": {"source_kind": "issue_comment", "source_id": "issuecomment-5299412215"},
        "verification_status": "unverified",
    },
    "cohort_provenance": {
        "runner_image": "ubuntu-24.04/20260701.1",
        "workers": 4,
        "scheduler": "loadscope",
        "command_manifest_digest": "sha256:" + "a" * 64,
        "test_selection_digest": "sha256:" + "b" * 64,
    },
}


def test_evidence_gate_insufficient_samples_never_fabricates_a_claim():
    """GIVEN a cohort fixture with fewer than MIN_COHORT_RUN_COUNT
    post-filter samples for the `before` arm WHEN `run_evidence_gate` runs
    THEN it returns `gate_status: insufficient_evidence` with `assessment:
    None` -- the current, CORRECT state of this production path per OWNER
    ("20件未満なら fail-closed する"), since no real >= 20-run cohort exists
    in this implementation session."""
    fixture = dict(_COHORT_FIXTURE_COMMON)
    fixture["before"] = _arm_fixture("a" * 40, provider_count=5, gate_ready_count=5, start_id=9000)
    fixture["after"] = _arm_fixture("b" * 40, provider_count=20, gate_ready_count=20, start_id=19000)

    result = gate.run_evidence_gate(fixture)
    assert result["gate_status"] == "insufficient_evidence"
    assert result["assessment"] is None
    assert "before" in result["reason"] or "provider_post_filter" in result["reason"]


def test_evidence_gate_sufficient_samples_computes_real_claim():
    """GIVEN a cohort fixture with >= MIN_COHORT_RUN_COUNT post-filter
    samples for BOTH arms WHEN `run_evidence_gate` runs THEN it returns
    `gate_status: complete` with a REAL, non-none claim computed by
    `build_assessment_from_percentile_cohorts`, and that assessment passes
    full structural+semantic validation -- proving the P0-8 producer really
    is wired end-to-end into a callable path, not merely unit-tested in
    isolation."""
    fixture = dict(_COHORT_FIXTURE_COMMON)
    fixture["before"] = _arm_fixture("a" * 40, provider_count=20, gate_ready_count=20, start_id=9000, base_ms=270_000)
    fixture["after"] = _arm_fixture("b" * 40, provider_count=20, gate_ready_count=20, start_id=19000, base_ms=100_000)

    result = gate.run_evidence_gate(fixture)
    assert result["gate_status"] == "complete"
    assert result["assessment"] is not None
    assert result["assessment"]["claim"]["kind"] != "none"
    assert result["validation_result"]["semantic_valid"] is True
    assert result["validation_exit_code"] == 0


def test_evidence_gate_cli_subprocess_exits_non_zero_on_insufficient_evidence(tmp_path):
    """Subprocess-level proof (guarding against a hollow in-process-only
    implementation, mirroring `test_evidence_incomplete_subprocess_exits_
    non_zero_not_skip_exit_code` above): invoking
    `tests/ci/test_ci_performance_gate.py` as a real CLI script (the exact
    invocation `.github/workflows/ci.yml`'s
    `e2e-performance-benchmark-assessment-gate` job uses) with an
    insufficient-evidence fixture terminates with a non-zero exit code."""
    fixture = dict(_COHORT_FIXTURE_COMMON)
    fixture["before"] = _arm_fixture("a" * 40, provider_count=3, gate_ready_count=3, start_id=9000)
    fixture["after"] = _arm_fixture("b" * 40, provider_count=3, gate_ready_count=3, start_id=19000)

    fixture_path = tmp_path / "cohort_fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    output_path = tmp_path / "gate_result.json"

    result = subprocess.run(
        [sys.executable, str(_MODULE_PATH), "--cohort-fixture", str(fixture_path), "--output", str(output_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1, result.stderr
    assert result.returncode != 5
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["gate_status"] == "insufficient_evidence"


def test_evidence_gate_cli_subprocess_exits_zero_on_sufficient_evidence(tmp_path):
    """Subprocess-level proof that the SAME CLI invocation exits 0 and
    writes a `gate_status: complete` result with a real claim once evidence
    is sufficient -- the production path is not permanently hard-wired to
    fail regardless of the data it is given."""
    fixture = dict(_COHORT_FIXTURE_COMMON)
    fixture["before"] = _arm_fixture("a" * 40, provider_count=20, gate_ready_count=20, start_id=9000, base_ms=270_000)
    fixture["after"] = _arm_fixture("b" * 40, provider_count=20, gate_ready_count=20, start_id=19000, base_ms=100_000)

    fixture_path = tmp_path / "cohort_fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    output_path = tmp_path / "gate_result.json"

    result = subprocess.run(
        [sys.executable, str(_MODULE_PATH), "--cohort-fixture", str(fixture_path), "--output", str(output_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["gate_status"] == "complete"
    assert written["assessment"]["claim"]["kind"] != "none"
