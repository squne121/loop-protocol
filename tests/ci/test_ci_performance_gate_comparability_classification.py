"""
tests/ci/test_ci_performance_gate_comparability_classification.py

Issue #2159 AC6 (P0-7/P1-3): `COMPARABILITY_FINGERPRINT_FIELDS` is
redesigned into three explicit classifications --
`WITHIN_COHORT_REQUIRED_EQUAL` / `CROSS_COHORT_REQUIRED_EQUAL` /
`INTENTIONAL_TREATMENT_DIFFERENCE` -- and host runner image /
Playwright container image digest provenance are separated into distinct
fields (`host_runner_image` / `playwright_container_image_digest`)
instead of a single collapsed `runner_image` string.

Fixture-driven unit tests; no live GitHub Actions history required.
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


def test_comparability_fingerprint_three_way_classification():
    """GIVEN the three classification tuples WHEN inspected THEN:
    - host_runner_image and playwright_container_image_digest are two
      DISTINCT fields (provenance separation, P1-3), and both appear in
      WITHIN_COHORT_REQUIRED_EQUAL and CROSS_COHORT_REQUIRED_EQUAL.
    - workflow_digest is a WITHIN_COHORT_REQUIRED_EQUAL field (all runs in
      one arm's cohort must share it) but ALSO an
      INTENTIONAL_TREATMENT_DIFFERENCE field (before vs after are expected
      to differ on it -- that IS the treatment being measured), and is
      correctly absent from CROSS_COHORT_REQUIRED_EQUAL.
    - The three classifications never silently collapse back into a
      single flat tuple that conflates "must match within a cohort" with
      "must match across cohorts" with "is the treatment itself"."""
    assert "host_runner_image" in gate.WITHIN_COHORT_REQUIRED_EQUAL
    assert "playwright_container_image_digest" in gate.WITHIN_COHORT_REQUIRED_EQUAL
    assert "host_runner_image" != "playwright_container_image_digest"

    assert "host_runner_image" in gate.CROSS_COHORT_REQUIRED_EQUAL
    assert "playwright_container_image_digest" in gate.CROSS_COHORT_REQUIRED_EQUAL

    assert "workflow_digest" in gate.WITHIN_COHORT_REQUIRED_EQUAL
    assert "workflow_digest" in gate.INTENTIONAL_TREATMENT_DIFFERENCE
    assert "workflow_digest" not in gate.CROSS_COHORT_REQUIRED_EQUAL

    # cross_cohort_required_equal must be a strict subset of
    # within_cohort_required_equal (anything required to match across
    # cohorts must a fortiori be required to match within one).
    assert set(gate.CROSS_COHORT_REQUIRED_EQUAL) <= set(gate.WITHIN_COHORT_REQUIRED_EQUAL)

    # intentional_treatment_difference and cross_cohort_required_equal are
    # disjoint by construction (a field cannot simultaneously be "must
    # match across arms" and "expected to differ across arms").
    assert set(gate.INTENTIONAL_TREATMENT_DIFFERENCE).isdisjoint(set(gate.CROSS_COHORT_REQUIRED_EQUAL))


def test_within_cohort_fingerprint_still_excludes_mismatched_runs():
    """GIVEN two runs in the same job with different host_runner_image
    WHEN building a comparable cohort THEN only the larger matching group
    is kept (existing majority-fingerprint-group cohort behavior, verified
    still functions after the three-way redesign)."""

    def baseline(workflow_run_id: int, host_runner_image: str) -> dict:
        b = {
            "schema": "ci_runtime_baseline_v1",
            "job": "e2e-core",
            "workflow_run_id": workflow_run_id,
            "measurements": [{"phase_id": "test_e2e_ci", "elapsed_ms": 100_000, "status": 0}],
        }
        for field in gate.WITHIN_COHORT_REQUIRED_EQUAL:
            b[field] = "shared-value"
        b["host_runner_image"] = host_runner_image
        return b

    majority = [baseline(i, "ubuntu-24.04/canonical") for i in range(1, 4)]
    minority = [baseline(100, "ubuntu-22.04/legacy")]

    cohort = gate._comparable_cohort(majority + minority, ("e2e-core",))
    result_ids = {b["workflow_run_id"] for b in cohort["e2e-core"]}
    assert result_ids == {1, 2, 3}


def test_cross_cohort_fields_used_to_compare_before_after_provenance():
    """GIVEN a before-arm cohort and an after-arm cohort WHEN checking
    cross-cohort comparability (infra should not silently change just
    because the workflow was split) THEN mismatched
    CROSS_COHORT_REQUIRED_EQUAL fields are detectable via simple field
    comparison (the classification exists precisely to make this check
    possible)."""
    before_fingerprint = {field: "v1" for field in gate.CROSS_COHORT_REQUIRED_EQUAL}
    after_fingerprint_matching = dict(before_fingerprint)
    after_fingerprint_drifted = dict(before_fingerprint)
    after_fingerprint_drifted["host_runner_image"] = "v2-drifted"

    mismatched_matching = [
        field
        for field in gate.CROSS_COHORT_REQUIRED_EQUAL
        if before_fingerprint[field] != after_fingerprint_matching[field]
    ]
    mismatched_drifted = [
        field
        for field in gate.CROSS_COHORT_REQUIRED_EQUAL
        if before_fingerprint[field] != after_fingerprint_drifted[field]
    ]

    assert mismatched_matching == []
    assert mismatched_drifted == ["host_runner_image"]
