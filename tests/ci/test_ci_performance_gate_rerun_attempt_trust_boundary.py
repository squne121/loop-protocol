"""
tests/ci/test_ci_performance_gate_rerun_attempt_trust_boundary.py

Issue #2187 (follow-up to PR #2182's OWNER adversarial review
issuecomment-5302595322 P0-3, and originating Issue #2179): unifies the
gate-side `tests/ci/test_ci_performance_gate.py::_normalize_run_attempt`
missing-`run_attempt` trust policy with the collector's
(`scripts/ci/collect_e2e_performance_benchmark.py::_classify_run_attempt`)
missing-excludes-from-cohort policy.

Regression tests collected here (repo VC contract convention: new test
functions go in a new file, not appended to existing satellite files):

- missing-`run_attempt` records are excluded from the trusted cohort with a
  `legacy_unverified_run_attempt` evidence_errors reason (AC2)
- a `workflow_run_id` excluded on BOTH lanes never disappears from
  `_pair_by_workflow_run_id`'s `evidence_errors` (AC4)
- `_cli_run_details_from_pairs` never synthesizes `run_attempt: 1` from a
  missing/invalid attempt (AC5)
- the gate-ready lane (`_gate_ready_post_filter_sample_count`) cannot have
  its sample floor inflated by a missing attempt, a duplicate
  `workflow_run_id`, or an attempt-2-and-later-only record (AC6, 4
  scenarios)
- the `.github/workflows/ci.yml` built-in insufficient-evidence smoke
  fixture (3 samples/arm, no `run_attempt` key) still resolves to
  `gate_status: insufficient_evidence` after this change, with the
  `run_attempt`-caused exclusion now visible in the reason (AC7)
- `_select_initial_attempt_baselines`'s `legacy_unverified_run_attempt`
  exclusion reason propagates into `_pair_by_workflow_run_id`'s
  `evidence_errors` (AC9)

Imports the shared helpers from the sibling `test_ci_performance_gate.py`
module via the same `importlib` module loading pattern this file's
siblings already use.
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


def _baseline(job: str, workflow_run_id: int, elapsed_ms: int, run_attempt: object = "__unset__") -> dict:
    """Mirrors the sibling files' `_baseline()` helper shape, but defaults
    to a TRUSTED `run_attempt: 1` (this file's whole purpose is exercising
    the missing/invalid `run_attempt` boundary explicitly, so each test
    opts INTO the missing/invalid shape it wants to assert against rather
    than inheriting an implicit default that could silently mask the
    exact scenario under test)."""
    baseline: dict = {
        "schema": "ci_runtime_baseline_v1",
        "job": job,
        "workflow_run_id": workflow_run_id,
        "measurements": [{"phase_id": "test_e2e_ci", "elapsed_ms": elapsed_ms, "status": 0}],
    }
    if run_attempt != "__unset__":
        if run_attempt is not None:
            baseline["run_attempt"] = run_attempt
    else:
        baseline["run_attempt"] = 1
    return baseline


def _missing_attempt_baseline(job: str, workflow_run_id: int, elapsed_ms: int) -> dict:
    baseline = _baseline(job, workflow_run_id, elapsed_ms, run_attempt=None)
    assert "run_attempt" not in baseline
    return baseline


# --------------------------------------------------------------------------- #
# AC2: missing run_attempt is excluded from the trusted cohort with a
# legacy_unverified_run_attempt evidence_errors reason.
# --------------------------------------------------------------------------- #
def test_missing_run_attempt_is_excluded_with_legacy_unverified_reason():
    """GIVEN a single baseline for a `workflow_run_id` that is entirely
    missing the `run_attempt` key WHEN `_select_initial_attempt_baselines`
    runs THEN the baseline is NOT in `selected`, and `evidence_errors`
    records that `workflow_run_id` with reason `legacy_unverified_run_
    attempt` (#2187 AC2 -- unifies with the collector's `_classify_run_
    attempt` missing-excludes-from-cohort policy)."""
    missing = _missing_attempt_baseline("e2e-core", 5001, 100_000)

    selected, evidence_errors = gate._select_initial_attempt_baselines([missing])

    assert 5001 not in selected
    assert len(evidence_errors) == 1
    assert evidence_errors[0]["workflow_run_id"] == 5001
    assert evidence_errors[0]["reason"] == gate.LEGACY_UNVERIFIED_RUN_ATTEMPT_REASON


def test_explicit_trusted_attempt_1_is_still_selected_not_regressed():
    """GIVEN a baseline with an EXPLICIT `run_attempt: 1` WHEN selected
    THEN it IS trusted and included in `selected` with no evidence_errors
    -- proves AC2's missing-key exclusion did not regress the explicit-1
    preservation contract (AC1)."""
    explicit = _baseline("e2e-core", 5002, 100_000, run_attempt=1)

    selected, evidence_errors = gate._select_initial_attempt_baselines([explicit])

    assert 5002 in selected
    assert evidence_errors == []


# --------------------------------------------------------------------------- #
# AC4: a workflow_run_id excluded on BOTH lanes must not disappear from
# _pair_by_workflow_run_id's evidence_errors.
# --------------------------------------------------------------------------- #
def test_both_lanes_missing_run_attempt_do_not_disappear_from_evidence_errors():
    """GIVEN a `workflow_run_id` whose core AND responsive baselines are
    BOTH missing `run_attempt` WHEN paired THEN `pairs` is empty for that
    id, but the `workflow_run_id` still appears in `evidence_errors`
    (#2187 AC4 -- `all_ids` is built from the raw baseline
    `workflow_run_id` set, not from the (now-empty-for-this-id) selected
    maps' keys)."""
    core_missing = _missing_attempt_baseline("e2e-core", 5003, 100_000)
    responsive_missing = _missing_attempt_baseline("e2e-responsive-matrix", 5003, 200_000)

    pairs, evidence_errors = gate._pair_by_workflow_run_id([core_missing], [responsive_missing])

    assert pairs == []
    assert len(evidence_errors) == 1
    assert evidence_errors[0]["workflow_run_id"] == 5003
    assert gate.LEGACY_UNVERIFIED_RUN_ATTEMPT_REASON in evidence_errors[0]["reason"]


# --------------------------------------------------------------------------- #
# AC5: _cli_run_details_from_pairs never synthesizes run_attempt: 1 from a
# missing/invalid attempt.
# --------------------------------------------------------------------------- #
def test_cli_run_details_never_synthesizes_missing_attempt_1():
    """GIVEN a (workflow_run_id, core, responsive) pair whose core baseline
    is missing `run_attempt` entirely (a scenario that should never reach
    this function in production, since `_pair_by_workflow_run_id` already
    filters it out -- this is a direct-call defense-in-depth regression
    test) WHEN `_cli_run_details_from_pairs` builds run-detail dicts THEN
    NO run_attempt: 1 is fabricated for it -- the pair is excluded from
    `run_details` entirely (#2187 AC5: no `_normalize_run_attempt(core) or
    1` fallback)."""
    core_missing = _missing_attempt_baseline("e2e-core", 5004, 100_000)
    responsive = _baseline("e2e-responsive-matrix", 5004, 200_000, run_attempt=1)

    run_details = gate._cli_run_details_from_pairs([(5004, core_missing, responsive)], "a" * 40)

    assert run_details == []


def test_cli_run_details_still_propagates_trusted_attempt_1():
    """GIVEN a pair whose core baseline carries an explicit trusted
    `run_attempt: 1` WHEN `_cli_run_details_from_pairs` runs THEN the
    run-detail entry IS produced with `run_attempt: 1` -- proves AC5's
    exclusion is specific to missing/invalid attempts, not a regression of
    the normal trusted path."""
    core = _baseline("e2e-core", 5005, 100_000, run_attempt=1)
    responsive = _baseline("e2e-responsive-matrix", 5005, 200_000, run_attempt=1)

    run_details = gate._cli_run_details_from_pairs([(5005, core, responsive)], "a" * 40)

    assert len(run_details) == 1
    assert run_details[0]["run_attempt"] == 1


# --------------------------------------------------------------------------- #
# AC6: gate-ready lane sample-floor inflation guards (4 scenarios).
# --------------------------------------------------------------------------- #
def _gate_ready_baseline(workflow_run_id: int, run_attempt: object = "__unset__") -> dict:
    baseline: dict = {
        "schema": "ci_runtime_baseline_v1",
        "job": "e2e",
        "workflow_run_id": workflow_run_id,
        "run_started_at": "2026-08-15T00:00:00Z",
        "check_completed_at": "2026-08-15T00:05:00Z",
    }
    if run_attempt != "__unset__":
        if run_attempt is not None:
            baseline["run_attempt"] = run_attempt
    else:
        baseline["run_attempt"] = 1
    return baseline


def test_gate_ready_missing_run_attempt_fails_closed():
    """GIVEN 20 gate-ready baselines each with a UNIQUE `workflow_run_id`
    but ALL missing `run_attempt` WHEN the post-filter sample count is
    computed THEN it is 0, not 20 -- a missing `run_attempt` cannot inflate
    the gate-ready sample floor (#2187 AC6, scenario 1/4)."""
    baselines = [
        {k: v for k, v in _gate_ready_baseline(6000 + i, run_attempt=None).items()} for i in range(20)
    ]
    assert all("run_attempt" not in b for b in baselines)

    count, evidence_errors = gate._gate_ready_post_filter_sample_count(baselines)

    assert count == 0
    assert len(evidence_errors) == 20
    assert all(e["reason"] == gate.LEGACY_UNVERIFIED_RUN_ATTEMPT_REASON for e in evidence_errors)


def test_gate_ready_duplicate_workflow_run_ids_do_not_inflate_sample_floor():
    """GIVEN 20 gate-ready baseline RECORDS that all share the SAME single
    `workflow_run_id` (a duplicate-upload / re-collection scenario) WHEN
    the post-filter sample count is computed THEN it is 1 (one unique
    `workflow_run_id`), not 20 -- duplicate `workflow_run_id`s cannot
    inflate the sample floor (#2187 AC6, scenario 2/4)."""
    baselines = [_gate_ready_baseline(6100, run_attempt=1) for _ in range(20)]

    count, evidence_errors = gate._gate_ready_post_filter_sample_count(baselines)

    assert count == 1
    assert evidence_errors == []


def test_gate_ready_attempt_2_never_substitutes_for_missing_attempt_1():
    """GIVEN 20 gate-ready baselines with UNIQUE `workflow_run_id`s, each
    carrying ONLY an attempt-2 record (attempt 1 never uploaded/missing)
    WHEN the post-filter sample count is computed THEN it is 0 -- attempt 2
    is never silently promoted to stand in for a missing attempt 1
    (#2187 AC6, scenario 3/4)."""
    baselines = [_gate_ready_baseline(6200 + i, run_attempt=2) for i in range(20)]

    count, evidence_errors = gate._gate_ready_post_filter_sample_count(baselines)

    assert count == 0
    # These records are NOT missing run_attempt (they have run_attempt=2),
    # so they are excluded from `selected` without a legacy_unverified_
    # run_attempt evidence_errors entry (that reason is reserved for the
    # genuinely-missing-key case, #2187 AC2/AC9).
    assert evidence_errors == []


def test_gate_ready_unique_explicit_attempt_1_records_meet_sample_floor():
    """GIVEN 20 gate-ready baselines, each with a UNIQUE `workflow_run_id`
    and an explicit trusted `run_attempt: 1` WHEN the post-filter sample
    count is computed THEN it is 20 -- the legitimate case is not
    penalized by the new trust/dedupe filtering (#2187 AC6, scenario 4/4)."""
    baselines = [_gate_ready_baseline(6300 + i, run_attempt=1) for i in range(20)]

    count, evidence_errors = gate._gate_ready_post_filter_sample_count(baselines)

    assert count == 20
    assert evidence_errors == []


# --------------------------------------------------------------------------- #
# AC7: the .github/workflows/ci.yml built-in insufficient-evidence smoke
# fixture (3 samples/arm, no run_attempt key) must remain
# gate_status: insufficient_evidence after this change.
# --------------------------------------------------------------------------- #
def _builtin_insufficient_evidence_arm(commit_sha: str, count: int, start_id: int) -> dict:
    """Reproduces `.github/workflows/ci.yml`'s
    `e2e-performance-benchmark-assessment-gate` job's built-in
    insufficient-evidence smoke fixture generator shape EXACTLY (no
    `run_attempt` key on any record, `count` < MIN_COHORT_RUN_COUNT) --
    this file does not edit `.github/workflows/ci.yml` (Allowed Paths
    exclude it); it reproduces the fixture shape as a fixture-driven unit
    test per this module's own Runtime Verification Applicability
    (`decision: not_applicable`)."""
    core = [
        {
            "workflow_run_id": start_id + i,
            "job": "e2e-core",
            "measurements": [{"phase_id": "test_e2e_core", "elapsed_ms": 60000 + i}],
        }
        for i in range(count)
    ]
    responsive = [
        {
            "workflow_run_id": start_id + i,
            "job": "e2e-responsive-matrix",
            "measurements": [{"phase_id": "test_e2e_core", "elapsed_ms": 60000 + i}],
        }
        for i in range(count)
    ]
    gate_ready = [
        {
            "workflow_run_id": start_id + i,
            "run_started_at": "2026-08-15T00:00:00Z",
            "check_completed_at": "2026-08-15T00:05:00Z",
        }
        for i in range(count)
    ]
    return {
        "commit_sha": commit_sha,
        "core_baselines": core,
        "responsive_baselines": responsive,
        "gate_ready_baselines": gate_ready,
    }


def test_builtin_insufficient_evidence_fixture_remains_insufficient_evidence():
    """GIVEN the `.github/workflows/ci.yml` built-in insufficient-evidence
    smoke fixture shape (3 samples/arm, deliberately below
    MIN_COHORT_RUN_COUNT, no `run_attempt` key on any record) WHEN
    `run_evidence_gate` runs THEN `gate_status` is still
    `insufficient_evidence` -- the exclusion reason is now ALSO
    `run_attempt`-caused (`legacy_unverified_run_attempt`), not merely
    sample-count-caused, but the gate_status itself does not regress
    (#2187 AC7)."""
    fixture = {
        "issue_number": 2159,
        "pr_number": 2172,
        "measured_at": "2026-08-15T00:00:00Z",
        "functional_evidence": {"proof_level": "check_run_only", "coverage_bound": False},
        "declared_impact": "built-in insufficient-evidence smoke fixture reproduction (#2187 AC7).",
        "risk_acknowledgement": {
            "reference": {"source_kind": "issue_comment", "source_id": "issuecomment-5299412215"},
            "verification_status": "unverified",
        },
        "cohort_provenance": {
            "runner_image": "ubuntu-24.04",
            "workers": 1,
            "scheduler": "loadscope",
            "command_manifest_digest": "sha256:" + "0" * 64,
            "test_selection_digest": "sha256:" + "0" * 64,
        },
        "before": _builtin_insufficient_evidence_arm("0" * 40, 3, 9000),
        "after": _builtin_insufficient_evidence_arm("1" * 40, 3, 19000),
    }

    result = gate.run_evidence_gate(fixture)

    assert result["gate_status"] == "insufficient_evidence"
    assert result["assessment"] is None
    assert gate.LEGACY_UNVERIFIED_RUN_ATTEMPT_REASON in result["reason"]


# --------------------------------------------------------------------------- #
# AC9: _select_initial_attempt_baselines's legacy_unverified_run_attempt
# exclusion reason propagates into _pair_by_workflow_run_id's
# evidence_errors (rather than the reason staying a fixed
# missing_pair_e2e-core string that does not explain WHY).
# --------------------------------------------------------------------------- #
def test_evidence_errors_carry_legacy_unverified_run_attempt_reason():
    """GIVEN a `workflow_run_id` whose core baseline is missing
    `run_attempt` (the responsive side has a real, trusted attempt-1
    baseline) WHEN paired THEN the resulting `evidence_errors` entry's
    `reason` identifiably includes `legacy_unverified_run_attempt` --
    not merely the generic fixed `missing_pair_e2e-core` string that
    would not explain the underlying cause (#2187 AC9)."""
    core_missing = _missing_attempt_baseline("e2e-core", 5006, 100_000)
    responsive_trusted = _baseline("e2e-responsive-matrix", 5006, 200_000, run_attempt=1)

    pairs, evidence_errors = gate._pair_by_workflow_run_id([core_missing], [responsive_trusted])

    assert pairs == []
    assert len(evidence_errors) == 1
    assert evidence_errors[0]["workflow_run_id"] == 5006
    assert evidence_errors[0]["reason"] == gate.LEGACY_UNVERIFIED_RUN_ATTEMPT_REASON


def test_evidence_errors_fixed_missing_pair_reason_preserved_when_not_attempt_caused():
    """GIVEN a `workflow_run_id` whose core baseline is fully absent (not
    merely missing `run_attempt` -- there is no core record at all for
    this id) WHEN paired THEN the `evidence_errors` reason is still the
    existing fixed `missing_pair_e2e-core` string -- proves AC9's reason
    enrichment is additive (only surfaces when a `_select_initial_
    attempt_baselines` exclusion actually occurred) and does not regress
    the pre-existing `test_pair_excludes_run_when_attempt_1_missing_
    never_substitutes_attempt_2`-style contract."""
    responsive_trusted = _baseline("e2e-responsive-matrix", 5007, 200_000, run_attempt=1)

    pairs, evidence_errors = gate._pair_by_workflow_run_id([], [responsive_trusted])

    assert pairs == []
    assert len(evidence_errors) == 1
    assert evidence_errors[0]["workflow_run_id"] == 5007
    assert evidence_errors[0]["reason"] == "missing_pair_e2e-core"
