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
