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
    (#2187 AC6, scenario 3/4).

    #2187 fix_delta (OWNER REQUEST_CHANGES issuecomment-5458167419 P1-2,
    test name preserved from the pre-fix_delta version but the assertion
    REVERSED): these records are NOT missing `run_attempt` (they have
    `run_attempt=2`), so they do NOT get the `legacy_unverified_run_attempt`
    reason (that reason is reserved for the genuinely-missing-key case,
    #2187 AC2/AC9) -- but pre-fix_delta this test asserted
    `evidence_errors == []`, i.e. this exact input shape silently lost
    evidence with NO identifiable reason at all, exactly the defect the
    OWNER review flagged. Each excluded `workflow_run_id` must now carry
    the identifiable `missing_or_invalid_initial_attempt_excluded_from_
    sample` reason instead."""
    baselines = [_gate_ready_baseline(6200 + i, run_attempt=2) for i in range(20)]

    count, evidence_errors = gate._gate_ready_post_filter_sample_count(baselines)

    assert count == 0
    assert len(evidence_errors) == 20
    assert all(
        e["reason"] == gate.MISSING_OR_INVALID_INITIAL_ATTEMPT_EXCLUDED_REASON for e in evidence_errors
    )
    assert {e["workflow_run_id"] for e in evidence_errors} == {6200 + i for i in range(20)}


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
# #2187 fix_delta P2-1 (OWNER REQUEST_CHANGES issuecomment-5458167419): the
# real `.github/workflows/ci.yml` built-in fixture reproduced immediately
# above genuinely has NO `run_attempt` key (confirmed via `git show
# HEAD:.github/workflows/ci.yml` -- the `arm()` helper embedded there
# builds `core_baselines` / `responsive_baselines` / `gate_ready_baselines`
# dicts without a `run_attempt` field at all), so
# `test_builtin_insufficient_evidence_fixture_remains_insufficient_evidence`
# above necessarily exercises BOTH the sample-floor check AND the
# run_attempt-trust exclusion simultaneously and must not be changed to add
# a key the real CI workflow does not emit (`.github/workflows/*.yml` is
# Out of Scope for this Issue). This variant fixture -- NOT a claim about
# ci.yml's actual shape -- isolates the sample-floor check alone by adding
# a trusted producer-shaped `run_attempt: "1"` to every record, so a
# reviewer can see the two failure causes exercised independently rather
# than conflated into a single always-`legacy_unverified_run_attempt`
# reason.
# --------------------------------------------------------------------------- #
def _builtin_insufficient_evidence_arm_with_trusted_run_attempt(commit_sha: str, count: int, start_id: int) -> dict:
    arm = _builtin_insufficient_evidence_arm(commit_sha, count, start_id)
    for baseline in arm["core_baselines"] + arm["responsive_baselines"] + arm["gate_ready_baselines"]:
        baseline["run_attempt"] = "1"
    return arm


def test_builtin_fixture_exercises_post_filter_sample_floor():
    """GIVEN the same 3-samples-per-arm shape as
    `test_builtin_insufficient_evidence_fixture_remains_insufficient_
    evidence` above but with a TRUSTED producer-shaped `run_attempt: "1"`
    added to every record WHEN `run_evidence_gate` runs THEN `gate_status`
    is still `insufficient_evidence`, but the failure is PURELY the
    sample-count floor -- NEITHER `legacy_unverified_run_attempt` NOR
    `missing_or_invalid_initial_attempt_excluded_from_sample` appears
    anywhere in the reason, and `gate_ready_evidence_errors` is empty for
    both arms -- proving the sample-floor re-validation itself is
    exercised in isolation from the run_attempt-trust exclusion path
    (#2187 fix_delta P2-1)."""
    fixture = {
        "issue_number": 2159,
        "pr_number": 2172,
        "measured_at": "2026-08-15T00:00:00Z",
        "functional_evidence": {"proof_level": "check_run_only", "coverage_bound": False},
        "declared_impact": (
            "sample-floor-only variant of the built-in insufficient-evidence smoke "
            "fixture, decoupled from the run_attempt-trust exclusion path (#2187 "
            "fix_delta P2-1)."
        ),
        "risk_acknowledgement": {
            "reference": {"source_kind": "issue_comment", "source_id": "issuecomment-5458167419"},
            "verification_status": "unverified",
        },
        "cohort_provenance": {
            "runner_image": "ubuntu-24.04",
            "workers": 1,
            "scheduler": "loadscope",
            "command_manifest_digest": "sha256:" + "0" * 64,
            "test_selection_digest": "sha256:" + "0" * 64,
        },
        "before": _builtin_insufficient_evidence_arm_with_trusted_run_attempt("0" * 40, 3, 9000),
        "after": _builtin_insufficient_evidence_arm_with_trusted_run_attempt("1" * 40, 3, 19000),
    }

    result = gate.run_evidence_gate(fixture)

    assert result["gate_status"] == "insufficient_evidence"
    assert result["assessment"] is None
    assert gate.LEGACY_UNVERIFIED_RUN_ATTEMPT_REASON not in result["reason"]
    assert gate.MISSING_OR_INVALID_INITIAL_ATTEMPT_EXCLUDED_REASON not in result["reason"]
    assert (
        "provider_post_filter_sample_count" in result["reason"]
        or "gate_ready_post_filter_sample_count" in result["reason"]
    )
    assert result["gate_ready_evidence_errors"]["before"] == []
    assert result["gate_ready_evidence_errors"]["after"] == []


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


# --------------------------------------------------------------------------- #
# #2187 fix_delta (OWNER REQUEST_CHANGES on PR #2383,
# issuecomment-5458167419): P1-1 (gate-ready `evidence_errors` silently
# discarded in `run_evidence_gate`'s production path) and P1-2 (the
# missing/invalid/later-attempt/collision classification was incomplete,
# and `test_gate_ready_attempt_2_never_substitutes_for_missing_attempt_1`
# above pinned a silent-exclusion shape as the "expected" outcome) fix
# verification. Reproduces the exact scenarios named in the OWNER comment.
# --------------------------------------------------------------------------- #
def test_gate_ready_attempt_2_only_reports_exclusion_reason():
    """GIVEN 3 gate-ready baselines with UNIQUE `workflow_run_id`s, each
    carrying ONLY an attempt-2 record, WHEN the post-filter sample count is
    computed THEN each excluded `workflow_run_id` carries the identifiable
    `missing_or_invalid_initial_attempt_excluded_from_sample` reason
    (dedicated small-scale counterpart to
    `test_gate_ready_attempt_2_never_substitutes_for_missing_attempt_1`
    above, named per the OWNER review's explicit regression-test list)."""
    baselines = [_gate_ready_baseline(6400 + i, run_attempt=2) for i in range(3)]

    count, evidence_errors = gate._gate_ready_post_filter_sample_count(baselines)

    assert count == 0
    assert len(evidence_errors) == 3
    assert all(
        e["reason"] == gate.MISSING_OR_INVALID_INITIAL_ATTEMPT_EXCLUDED_REASON for e in evidence_errors
    )


def test_gate_ready_invalid_attempt_reports_exclusion_reason():
    """GIVEN gate-ready baselines with EXPLICIT invalid `run_attempt`
    values (`None`, a bool, `0`, a negative int, a non-numeric string --
    all values `_normalize_run_attempt` rejects) on distinct
    `workflow_run_id`s WHEN the post-filter sample count is computed THEN
    every one is excluded with the identifiable
    `missing_or_invalid_initial_attempt_excluded_from_sample` reason -- an
    EXPLICIT invalid value must never be conflated with the
    `legacy_unverified_run_attempt` fully-missing-key case, but it must
    also never be silently dropped from `evidence_errors` (#2187 P1-2)."""
    invalid_values = [None, True, False, 0, -1, "not-a-number"]
    baselines = []
    for i, value in enumerate(invalid_values):
        baseline = _gate_ready_baseline(6500 + i, run_attempt=1)
        baseline["run_attempt"] = value
        assert "run_attempt" in baseline
        baselines.append(baseline)

    count, evidence_errors = gate._gate_ready_post_filter_sample_count(baselines)

    assert count == 0
    assert len(evidence_errors) == len(invalid_values)
    assert all(
        e["reason"] == gate.MISSING_OR_INVALID_INITIAL_ATTEMPT_EXCLUDED_REASON for e in evidence_errors
    )
    assert {e["workflow_run_id"] for e in evidence_errors} == {6500 + i for i in range(len(invalid_values))}


def test_gate_ready_collision_reports_collision_reason():
    """GIVEN two gate-ready baselines sharing the SAME `workflow_run_id`/
    trusted attempt-1 identity slot but disagreeing on content (a genuine
    identity collision, `_detect_run_attempt_identity_collisions`) WHEN
    the post-filter sample count is computed THEN the `workflow_run_id` is
    excluded with the identifiable `run_attempt_identity_collision` reason
    -- pre-fix_delta, a collision group was excluded from `selected` with
    NO `evidence_errors` entry at all (#2187 P1-2)."""
    baseline_a = _gate_ready_baseline(6600, run_attempt=1)
    baseline_b = dict(baseline_a, check_completed_at="2026-08-15T00:09:00Z")
    assert baseline_a != baseline_b

    count, evidence_errors = gate._gate_ready_post_filter_sample_count([baseline_a, baseline_b])

    assert count == 0
    assert len(evidence_errors) == 1
    assert evidence_errors[0]["workflow_run_id"] == 6600
    assert evidence_errors[0]["reason"] == gate.RUN_ATTEMPT_IDENTITY_COLLISION_REASON


# --------------------------------------------------------------------------- #
# #2187 fix_delta P1-1: `run_evidence_gate`'s PRODUCTION result must not
# silently drop a gate-ready `evidence_errors` entry, regardless of
# whether the overall sample floor is otherwise met.
# --------------------------------------------------------------------------- #
_FIX_DELTA_COHORT_FIXTURE_COMMON = {
    "issue_number": 2187,
    "pr_number": 2383,
    "measured_at": "2026-08-15T00:00:00Z",
    "functional_evidence": {"proof_level": "check_run_only", "coverage_bound": False},
    "declared_impact": "#2187 fix_delta P1-1 regression fixture (issuecomment-5458167419).",
    "risk_acknowledgement": {
        "reference": {"source_kind": "issue_comment", "source_id": "issuecomment-5458167419"},
        "verification_status": "unverified",
    },
    "cohort_provenance": {
        "runner_image": "ubuntu-24.04",
        "workers": 1,
        "scheduler": "loadscope",
        "command_manifest_digest": "sha256:" + "0" * 64,
        "test_selection_digest": "sha256:" + "0" * 64,
    },
}


def _fix_delta_arm_fixture(commit_sha: str, start_id: int, gate_ready_extra: list[dict] | None = None) -> dict:
    """20 trusted, uniquely-identified core/responsive/gate-ready samples
    (sufficient evidence on their own) -- callers add `gate_ready_extra`
    records to reproduce a specific surplus/poison scenario on the
    gate-ready lane without disturbing the (already-sufficient) provider
    lane."""
    core = [_baseline("e2e-core", start_id + i, 100_000 + i, run_attempt=1) for i in range(20)]
    responsive = [
        _baseline("e2e-responsive-matrix", start_id + i, 100_000 + i, run_attempt=1) for i in range(20)
    ]
    gate_ready = [_gate_ready_baseline(start_id + i, run_attempt=1) for i in range(20)]
    if gate_ready_extra:
        gate_ready = gate_ready + gate_ready_extra
    return {
        "commit_sha": commit_sha,
        "core_baselines": core,
        "responsive_baselines": responsive,
        "gate_ready_baselines": gate_ready,
    }


def test_run_evidence_gate_surfaces_gate_ready_missing_attempt_evidence_error_in_production_result():
    """GIVEN a `before` arm with 20 trusted gate-ready samples PLUS 1
    surplus sample missing `run_attempt` entirely WHEN `run_evidence_gate`
    (the production gate function) runs THEN the missing sample's
    `workflow_run_id` and `legacy_unverified_run_attempt` reason are
    PRESENT in `result["gate_ready_evidence_errors"]["before"]` -- proving
    the fix_delta's `before_gate_ready_evidence_errors` /
    `after_gate_ready_evidence_errors` are no longer discarded (pre-fix_delta
    these were bound to `_before_gate_ready_evidence_errors` /
    `_after_gate_ready_evidence_errors` with a leading underscore and never
    read again, #2187 fix_delta P1-1)."""
    fixture = dict(_FIX_DELTA_COHORT_FIXTURE_COMMON)
    poison = _gate_ready_baseline(7999, run_attempt=None)
    assert "run_attempt" not in poison
    fixture["before"] = _fix_delta_arm_fixture("a" * 40, start_id=8000, gate_ready_extra=[poison])
    fixture["after"] = _fix_delta_arm_fixture("b" * 40, start_id=9000)

    result = gate.run_evidence_gate(fixture)

    assert "gate_ready_evidence_errors" in result
    before_errors = result["gate_ready_evidence_errors"]["before"]
    assert any(
        e["workflow_run_id"] == 7999 and e["reason"] == gate.LEGACY_UNVERIFIED_RUN_ATTEMPT_REASON
        for e in before_errors
    ), f"the surplus missing-run_attempt evidence error must survive into the production result: {before_errors!r}"


def test_run_evidence_gate_surfaces_gate_ready_collision_evidence_error_in_production_result():
    """GIVEN a `before` arm with 20 trusted gate-ready samples PLUS a
    duplicate `workflow_run_id` disagreeing on content (identity collision)
    WHEN `run_evidence_gate` runs THEN the `run_attempt_identity_collision`
    reason for that `workflow_run_id` is PRESENT in
    `result["gate_ready_evidence_errors"]["before"]` -- proving a
    production-path collision is never silently dropped either (#2187
    fix_delta P1-1/P1-2)."""
    fixture = dict(_FIX_DELTA_COHORT_FIXTURE_COMMON)
    colliding_original = _gate_ready_baseline(8000, run_attempt=1)
    colliding_conflict = dict(colliding_original, check_completed_at="2026-08-15T00:09:00Z")
    fixture["before"] = _fix_delta_arm_fixture("a" * 40, start_id=8000, gate_ready_extra=[colliding_conflict])
    fixture["after"] = _fix_delta_arm_fixture("b" * 40, start_id=9000)

    result = gate.run_evidence_gate(fixture)

    assert "gate_ready_evidence_errors" in result
    before_errors = result["gate_ready_evidence_errors"]["before"]
    assert any(
        e["workflow_run_id"] == 8000 and e["reason"] == gate.RUN_ATTEMPT_IDENTITY_COLLISION_REASON
        for e in before_errors
    ), f"the colliding sample's evidence error must survive into the production result: {before_errors!r}"
