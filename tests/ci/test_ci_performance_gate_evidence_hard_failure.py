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

import hashlib
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
    # #2187: `run_attempt: 1` set explicitly -- this file's fixtures feed
    # `run_evidence_gate` -> `_pair_by_workflow_run_id` ->
    # `_select_initial_attempt_baselines`, and the gate-side missing-
    # run_attempt trust rejection unified with the collector in #2187 would
    # otherwise silently exclude every record here (this file's own scope
    # is the AC11 hard-failure gate, not run_attempt trust semantics).
    baselines = []
    for i in range(count):
        run_id = start_id + i
        baselines.append(
            {
                "workflow_run_id": run_id,
                "job": job,
                "run_attempt": 1,
                "measurements": [{"phase_id": "test_e2e_core", "elapsed_ms": base_ms + i}],
            }
        )
    return baselines


def _gate_ready_baselines(count: int, start_id: int = 1) -> list[dict]:
    # #2187: `run_attempt: 1` set explicitly -- see `_paired_baselines`
    # comment above; `_gate_ready_post_filter_sample_count` now also
    # applies `_select_initial_attempt_baselines` dedupe/trust filtering.
    baselines = []
    for i in range(count):
        baselines.append(
            {
                "workflow_run_id": start_id + i,
                "run_attempt": 1,
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


# --------------------------------------------------------------------------- #
# #2423 AC4/AC5: trusted ci_verdict_summary_v2 binding fixtures. Written
# per-test into `tmp_path` (never a shared fixture file) so each test's
# trusted-artifact content stays scoped to its own assertions -- this file's
# Allowed Paths boundary does not include
# .claude/skills/ci-test-performance/scripts/fixtures/, so a real trusted
# artifact is constructed inline instead of reusing that directory's
# existing trusted_ci_verdict_summary_v2_artifact.json fixture.
# --------------------------------------------------------------------------- #
_TRUSTED_EXPECTED_HEAD_SHA = "b" * 40


def _write_trusted_ci_verdict_summary_artifact(tmp_path, expected_head_sha: str = _TRUSTED_EXPECTED_HEAD_SHA):
    """Writes a `ci_verdict_summary_v2` artifact whose shape satisfies
    `validate_ci_performance_assessment_v2.py::_check_trusted_functional_
    artifact` for the `functional_evidence.ci_verdict_summary_ref` declared
    in `_COHORT_FIXTURE_COMMON` above (`check_run_id: 1`, `classification:
    required`, `status: completed`, `conclusion: success`, `head_sha_match:
    True`). Returns `(path, file_sha256)` -- `file_sha256` is the sha256 of
    the FILE's own raw bytes (`ci_verdict_summary_file_sha256`), distinct
    from any GitHub Actions artifact-bundle-level digest (AC4)."""
    artifact = {
        "schema": "ci_verdict_summary_v2",
        "schema_version": 2,
        "generated_at": "2026-08-15T00:00:00+00:00",
        "repository": "squne121/loop-protocol",
        "workflow_run_id": 555000111,
        "workflow_run_attempt": 1,
        "event_name": "pull_request",
        "expected_head_sha": expected_head_sha,
        "head_sha": expected_head_sha,
        "overall_status": "merge_ready",
        "next_action": "none",
        "artifact_refs": [],
        "checks": [
            {
                "name": "typecheck",
                "workflow": "ci",
                "check_run_id": 1,
                "status": "completed",
                "conclusion": "success",
                "classification": "required",
                "head_sha": expected_head_sha,
                "expected_head_sha": expected_head_sha,
                "head_sha_match": True,
                "blocking_merge_ready": False,
                "failure_reason": "none",
                "artifact_refs": [],
            }
        ],
    }
    path = tmp_path / "trusted_ci_verdict_summary_v2.json"
    raw_bytes = json.dumps(artifact, indent=2).encode("utf-8")
    path.write_bytes(raw_bytes)
    file_sha256 = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    return path, file_sha256


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


def test_evidence_gate_cli_subprocess_exits_zero_on_sufficient_evidence_with_trusted_binding(tmp_path):
    """Subprocess-level proof that the SAME CLI invocation exits 0 and
    writes a `gate_status: complete` result with a real claim once evidence
    is sufficient AND the CLI is given a real trusted `ci_verdict_summary_v2`
    binding (#2423 AC4: exit 0 now additionally requires `approval_eligible`,
    which requires a real --ci-verdict-summary/--expected-head-sha/
    --expected-ci-verdict-summary-file-sha256 binding -- not merely
    sufficient sample counts) -- the production path is not permanently
    hard-wired to fail regardless of the data/binding it is given."""
    fixture = dict(_COHORT_FIXTURE_COMMON)
    fixture["before"] = _arm_fixture("a" * 40, provider_count=20, gate_ready_count=20, start_id=9000, base_ms=270_000)
    fixture["after"] = _arm_fixture("b" * 40, provider_count=20, gate_ready_count=20, start_id=19000, base_ms=100_000)

    fixture_path = tmp_path / "cohort_fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    output_path = tmp_path / "gate_result.json"
    receipt_path = tmp_path / "close_grade_receipt.json"
    trusted_summary_path, trusted_summary_file_sha256 = _write_trusted_ci_verdict_summary_artifact(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(_MODULE_PATH),
            "--cohort-fixture",
            str(fixture_path),
            "--output",
            str(output_path),
            "--receipt-output",
            str(receipt_path),
            "--ci-verdict-summary",
            str(trusted_summary_path),
            "--expected-head-sha",
            _TRUSTED_EXPECTED_HEAD_SHA,
            "--expected-ci-verdict-summary-file-sha256",
            trusted_summary_file_sha256,
            "--ci-verdict-summary-artifact-id",
            "555000111",
            "--github-artifact-digest",
            "sha256:" + "9" * 64,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["gate_status"] == "complete"
    assert written["assessment"]["claim"]["kind"] != "none"
    assert written["validation_result"]["approval_eligible"] is True

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["schema"] == "CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1"
    assert receipt["exit_code"] == 0
    assert receipt["validation"] == {"semantic_valid": True, "approval_eligible": True}
    assert receipt["trusted_functional_evidence"]["ci_verdict_summary_file_sha256"] == trusted_summary_file_sha256
    assert receipt["trusted_functional_evidence"]["github_artifact_digest"] == "sha256:" + "9" * 64


def test_close_grade_cli_exit_code_zero_requires_approval_eligible_not_only_semantic_valid(tmp_path):
    """#2423 AC4: sufficient evidence + a semantically valid built
    assessment is NOT enough for exit code 0 when no trusted
    `ci_verdict_summary_v2` binding is supplied -- `approval_eligible` must
    independently be true. Exit code 3 (semantic_valid but not
    approval_eligible) is distinct from exit codes 1 (insufficient_evidence)
    and 2 (semantic invalid)."""
    fixture = dict(_COHORT_FIXTURE_COMMON)
    fixture["before"] = _arm_fixture("a" * 40, provider_count=20, gate_ready_count=20, start_id=9000, base_ms=270_000)
    fixture["after"] = _arm_fixture("b" * 40, provider_count=20, gate_ready_count=20, start_id=19000, base_ms=100_000)

    fixture_path = tmp_path / "cohort_fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    output_path = tmp_path / "gate_result.json"

    # No --ci-verdict-summary/--expected-head-sha supplied at all (the
    # PRE-#2423 CLI invocation shape) -- approval_eligible must be false.
    result = subprocess.run(
        [sys.executable, str(_MODULE_PATH), "--cohort-fixture", str(fixture_path), "--output", str(output_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 3, result.stderr
    assert result.returncode not in (0, 1, 2, 5)
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["gate_status"] == "complete"
    assert written["validation_result"]["semantic_valid"] is True
    assert written["validation_result"]["approval_eligible"] is False


def test_close_grade_cli_approval_eligible_false_head_sha_mismatch_produces_exit_code_three(tmp_path):
    """#2423 AC4: a trusted artifact IS supplied but its `expected_head_sha`
    does not match the `--expected-head-sha` the caller asserts -- the
    validator's existing `functional_evidence_artifact_head_sha_mismatch`
    blocker must still translate into a non-zero close-grade CLI exit code,
    not a silently accepted exit 0."""
    fixture = dict(_COHORT_FIXTURE_COMMON)
    fixture["before"] = _arm_fixture("a" * 40, provider_count=20, gate_ready_count=20, start_id=9000, base_ms=270_000)
    fixture["after"] = _arm_fixture("b" * 40, provider_count=20, gate_ready_count=20, start_id=19000, base_ms=100_000)

    fixture_path = tmp_path / "cohort_fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    output_path = tmp_path / "gate_result.json"
    trusted_summary_path, trusted_summary_file_sha256 = _write_trusted_ci_verdict_summary_artifact(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(_MODULE_PATH),
            "--cohort-fixture",
            str(fixture_path),
            "--output",
            str(output_path),
            "--ci-verdict-summary",
            str(trusted_summary_path),
            "--expected-head-sha",
            "c" * 40,  # mismatches both the artifact and the ref's "b" * 40
            "--expected-ci-verdict-summary-file-sha256",
            trusted_summary_file_sha256,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 3, result.stderr
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["validation_result"]["approval_eligible"] is False
    assert "functional_evidence_artifact_head_sha_mismatch" in written["validation_result"]["blockers"]
