"""
tests/ci/test_ci_performance_gate_rerun_attempt_selection.py

Issue #2179 (P1-1 rerun-attempt-selection follow-up to #2159/PR #2172,
OWNER adversarial review issuecomment-5295659213): regression tests for
`tests/ci/test_ci_performance_gate.py`'s gate-side `initial_attempt_only_v1`
rerun-attempt selection (`_dedupe_by_workflow_run_id` / `_pair_by_workflow_run_id`),
proving it is order-independent (AC2) and that a parametrized
insertion-order sweep matches the canonical (`workflow_run_id` ascending)
output order (AC7). Imports the shared helpers from the sibling
`test_ci_performance_gate.py` module via the same `importlib` module
loading pattern this file's siblings already use (mirrors
`test_ci_performance_gate_paired_critical_path.py`).
"""
from __future__ import annotations

import importlib.util
import pathlib
import random

import pytest

_MODULE_PATH = pathlib.Path(__file__).resolve().parent / "test_ci_performance_gate.py"


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("test_ci_performance_gate", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate_module()


def _baseline(job: str, workflow_run_id: int, elapsed_ms: int, run_attempt: object = None) -> dict:
    baseline: dict = {
        "schema": "ci_runtime_baseline_v1",
        "job": job,
        "workflow_run_id": workflow_run_id,
        "measurements": [{"phase_id": "test_e2e_ci", "elapsed_ms": elapsed_ms, "status": 0}],
    }
    if run_attempt is not None:
        baseline["run_attempt"] = run_attempt
    return baseline


# --------------------------------------------------------------------------- #
# AC2: gate-side order-independence for both _dedupe_by_workflow_run_id and
# _pair_by_workflow_run_id.
# --------------------------------------------------------------------------- #
def test_dedupe_and_pair_by_workflow_run_id_order_independent():
    """GIVEN attempt-1 and attempt-2 baselines for the SAME workflow_run_id,
    on BOTH the core and responsive lanes, presented forward and reversed,
    WHEN deduped/paired THEN the attempt-1 baseline is selected on both
    lanes regardless of input order (#2179 AC2) -- proves neither
    `_dedupe_by_workflow_run_id` nor `_pair_by_workflow_run_id`'s internal
    `core_by_id`/`responsive_by_id` selection depends on insertion order
    (the pre-#2179 `_pair_by_workflow_run_id` used a dict comprehension,
    an implicit last-seen-wins for a duplicate key)."""
    core_attempt_1 = _baseline("e2e-core", 600, elapsed_ms=100_000, run_attempt=1)
    core_attempt_2 = _baseline("e2e-core", 600, elapsed_ms=999_000, run_attempt=2)
    responsive_attempt_1 = _baseline("e2e-responsive-matrix", 600, elapsed_ms=200_000, run_attempt=1)
    responsive_attempt_2 = _baseline("e2e-responsive-matrix", 600, elapsed_ms=888_000, run_attempt=2)

    forward_deduped = gate._dedupe_by_workflow_run_id([core_attempt_1, core_attempt_2])
    reverse_deduped = gate._dedupe_by_workflow_run_id([core_attempt_2, core_attempt_1])
    assert len(forward_deduped) == 1 and len(reverse_deduped) == 1
    assert forward_deduped[0]["measurements"][0]["elapsed_ms"] == 100_000
    assert reverse_deduped[0]["measurements"][0]["elapsed_ms"] == 100_000

    forward_pairs, forward_errors = gate._pair_by_workflow_run_id(
        [core_attempt_1, core_attempt_2], [responsive_attempt_1, responsive_attempt_2]
    )
    reverse_pairs, reverse_errors = gate._pair_by_workflow_run_id(
        [core_attempt_2, core_attempt_1], [responsive_attempt_2, responsive_attempt_1]
    )
    assert forward_errors == [] and reverse_errors == []
    assert len(forward_pairs) == 1 and len(reverse_pairs) == 1
    _, forward_core, forward_responsive = forward_pairs[0]
    _, reverse_core, reverse_responsive = reverse_pairs[0]
    assert forward_core["measurements"][0]["elapsed_ms"] == 100_000
    assert reverse_core["measurements"][0]["elapsed_ms"] == 100_000
    assert forward_responsive["measurements"][0]["elapsed_ms"] == 200_000
    assert reverse_responsive["measurements"][0]["elapsed_ms"] == 200_000


def test_pair_excludes_run_when_attempt_1_missing_never_substitutes_attempt_2():
    """GIVEN only an attempt-2 core baseline (attempt 1 never uploaded/
    missing) for a workflow_run_id that DOES have an attempt-1 responsive
    baseline, WHEN paired THEN the pair is excluded (core side has no
    eligible attempt-1 candidate) -- attempt 2 is never silently promoted
    to stand in for a missing attempt 1 (#2179 P0-4).

    #2187 fix_delta (OWNER REQUEST_CHANGES issuecomment-5458167419 P1-2):
    the reason is now the identifiable
    `missing_or_invalid_initial_attempt_excluded_from_sample`
    (`_select_initial_attempt_baselines`'s own classification of an
    attempt-2-and-later-only group), NOT the generic fallback
    `missing_pair_e2e-core` string this test asserted pre-fix_delta --
    that fallback is reserved for a `workflow_run_id` with NO baseline at
    all on a given lane (see
    `test_evidence_errors_fixed_missing_pair_reason_preserved_when_not_
    attempt_caused` in test_ci_performance_gate_rerun_attempt_trust_
    boundary.py), not for one whose only baseline was excluded by
    attempt-trust filtering."""
    core_attempt_2_only = _baseline("e2e-core", 601, elapsed_ms=100_000, run_attempt=2)
    responsive_attempt_1 = _baseline("e2e-responsive-matrix", 601, elapsed_ms=200_000, run_attempt=1)

    pairs, evidence_errors = gate._pair_by_workflow_run_id([core_attempt_2_only], [responsive_attempt_1])
    assert pairs == []
    assert len(evidence_errors) == 1
    assert evidence_errors[0]["workflow_run_id"] == 601
    assert evidence_errors[0]["reason"] == gate.MISSING_OR_INVALID_INITIAL_ATTEMPT_EXCLUDED_REASON


def test_cli_run_details_propagates_selected_run_attempt_not_hardcoded():
    """GIVEN a paired (workflow_run_id, core, responsive) tuple whose core
    baseline carries an explicit `run_attempt` WHEN
    `_cli_run_details_from_pairs` builds the run-detail dicts THEN
    `run_attempt` reflects the record's OWN (normalized) value, not a `1`
    literal hardcoded regardless of input (#2179 AC2)."""
    core = _baseline("e2e-core", 602, elapsed_ms=100_000, run_attempt=1)
    responsive = _baseline("e2e-responsive-matrix", 602, elapsed_ms=200_000, run_attempt=1)
    run_details = gate._cli_run_details_from_pairs([(602, core, responsive)], "a" * 40)
    assert len(run_details) == 1
    assert run_details[0]["run_attempt"] == gate._normalize_run_attempt(core)
    assert run_details[0]["run_attempt"] == 1


# --------------------------------------------------------------------------- #
# AC7: parametrized order-independence + canonical output order.
# --------------------------------------------------------------------------- #
def _order_independence_fixture() -> tuple[list[dict], list[dict]]:
    ids = (610, 605, 620, 615, 600)
    core = [_baseline("e2e-core", run_id, elapsed_ms=100_000 + i, run_attempt=1) for i, run_id in enumerate(ids)]
    responsive = [
        _baseline("e2e-responsive-matrix", run_id, elapsed_ms=200_000 + i, run_attempt=1)
        for i, run_id in enumerate(ids)
    ]
    return core, responsive


@pytest.mark.parametrize(
    "shuffle_fn",
    [
        lambda records: records,
        lambda records: list(reversed(records)),
        lambda records: random.Random(7).sample(records, k=len(records)),
    ],
    ids=["original", "reversed", "shuffled"],
)
def test_order_independent_parametrized_matches_canonical_sort(shuffle_fn):
    """GIVEN the SAME underlying core/responsive baseline set presented in
    original, reversed, and shuffled order WHEN paired THEN the resulting
    pairs are sorted by `workflow_run_id` ascending (canonical order) in
    ALL three orderings, and the selected (workflow_run_id, core,
    responsive) tuples are identical across orderings (#2179 AC7)."""
    base_core, base_responsive = _order_independence_fixture()

    ordered_core = shuffle_fn(list(base_core))
    ordered_responsive = shuffle_fn(list(base_responsive))

    pairs, evidence_errors = gate._pair_by_workflow_run_id(ordered_core, ordered_responsive)
    assert evidence_errors == []
    run_ids = [workflow_run_id for workflow_run_id, _core, _responsive in pairs]
    assert run_ids == sorted(run_ids)
    assert run_ids == [600, 605, 610, 615, 620]

    canonical_pairs, canonical_errors = gate._pair_by_workflow_run_id(base_core, base_responsive)
    assert canonical_errors == []
    assert [wid for wid, _c, _r in pairs] == [wid for wid, _c, _r in canonical_pairs]
    for (wid_a, core_a, responsive_a), (wid_b, core_b, responsive_b) in zip(pairs, canonical_pairs, strict=True):
        assert wid_a == wid_b
        assert core_a == core_b
        assert responsive_a == responsive_b



# --------------------------------------------------------------------------- #
# #2182 P1 (fix_delta after OWNER adversarial review of PR #2182,
# issuecomment-5302446086): identity collisions must be detected on the
# ENTIRE normalized baseline (byte-for-byte), never merely `min()`-
# resolved -- mirrored from the collector's own #2182 P1 fix
# (`scripts/ci/collect_e2e_performance_benchmark.py`), independently
# implemented per this module's Allowed Paths boundary.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "extra_fields,differing_field,value_b",
    [
        ({}, "measurements", [{"phase_id": "test_e2e_ci", "elapsed_ms": 999_000, "status": 0}]),
        ({"host_runner_image": "ubuntu-24.04"}, "host_runner_image", "ubuntu-22.04"),
        (
            {"rerun_attempt_selection_policy": "initial_attempt_only_v1"},
            "rerun_attempt_selection_policy",
            "some_other_policy",
        ),
    ],
    ids=["measurement", "fingerprint", "policy_name"],
)
def test_identity_collision_on_any_disagreeing_field_excludes_whole_sample(extra_fields, differing_field, value_b):
    """GIVEN two baselines sharing `(workflow_run_id, job, run_attempt)`
    but disagreeing on a measurement / comparability-fingerprint / policy
    field (the pre-fix_delta `min()` tie-break's blind spot -- it only
    ever compared the canonical-JSON encoding as a TIE-BREAK KEY, never
    detected the disagreement itself as a genuine conflict) WHEN examined
    via `_detect_run_attempt_identity_collisions` THEN the
    `workflow_run_id` is flagged as a collision (#2182 P1)."""
    baseline_a = dict(_baseline("e2e-core", 990, elapsed_ms=100_000, run_attempt=1), **extra_fields)
    baseline_b = dict(baseline_a, **{differing_field: value_b})
    assert baseline_a[differing_field] != baseline_b[differing_field]

    collisions = gate._detect_run_attempt_identity_collisions([baseline_a, baseline_b])
    assert 990 in collisions
    assert len(collisions[990]) == 2

    selected, _evidence_errors = gate._select_initial_attempt_baselines([baseline_a, baseline_b])
    assert 990 not in selected


def test_byte_identical_duplicate_is_unchanged_not_a_collision():
    """GIVEN two BYTE-FOR-BYTE identical baselines sharing
    `(workflow_run_id, job, run_attempt)` (a harmless duplicate) WHEN
    selected THEN this is NOT a collision -- exactly one baseline is
    selected, with its original field values intact (#2182 P1: only
    content DISAGREEMENT is a collision)."""
    baseline_a = _baseline("e2e-core", 991, elapsed_ms=150_000, run_attempt=1)
    baseline_b = dict(baseline_a)

    collisions = gate._detect_run_attempt_identity_collisions([baseline_a, baseline_b])
    assert 991 not in collisions

    selected, _evidence_errors = gate._select_initial_attempt_baselines([baseline_a, baseline_b])
    assert selected[991]["measurements"][0]["elapsed_ms"] == 150_000


def test_missing_attempt_baseline_colliding_with_explicit_attempt_1_baseline_is_unaffected_by_grouping():
    """GIVEN one baseline with a MISSING `run_attempt` key and another
    with an EXPLICIT `run_attempt: 1`, sharing the same `workflow_run_id`/
    `job` but disagreeing on content, WHEN examined THEN this IS a
    collision under this gate module's collision grouping (missing keys
    are grouped into the attempt-1 slot for `_normalize_run_attempt`
    purposes here too, since gate's `_normalize_run_attempt` itself
    already treats a missing key as attempt 1 -- see
    `_select_initial_attempt_baselines`'s documented scope-boundary
    note); this mirrors the collector's equivalent
    `_effective_attempt_for_collision_grouping` behavior for this exact
    scenario, keeping both modules' collision semantics aligned even
    though their MISSING-key TRUSTED-cohort-eligibility policies
    intentionally differ (#2182 P0-3 scope boundary)."""
    explicit_attempt_1 = _baseline("e2e-core", 992, elapsed_ms=100_000, run_attempt=1)
    missing_attempt = _baseline("e2e-core", 992, elapsed_ms=999_000)
    assert "run_attempt" not in missing_attempt

    collisions = gate._detect_run_attempt_identity_collisions([explicit_attempt_1, missing_attempt])
    assert 992 in collisions

    selected, _evidence_errors = gate._select_initial_attempt_baselines([explicit_attempt_1, missing_attempt])
    assert 992 not in selected
