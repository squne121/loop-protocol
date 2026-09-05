"""Regression tests for `timed_out` workflow-conclusion handling (#2507).

These tests exercise the public `validate_assessment()` entry point only.
They deliberately do NOT call `_canonical_classification()` directly as an
oracle for expected values, since that would let the implementation and the
test share the same bug. Instead, expected `failure` / `not_affected` values
are hand-derived from the fixture's known Playwright TestCase outcomes
(only `workflow_run_id`s 1001/1002 in the "before" arm carry a `flaky` /
`unexpected` TestCase; every other run has no matching TestCase).

`_make_assessment()` is imported (not duplicated) from the existing test
file `test_validate_ci_reliability_assessment_v1.py` via `importlib`, using a
unique module name to avoid `sys.modules` collisions in a shared pytest
session (see repo convention for same-directory test module reuse). The
existing test file is not modified.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys

SCRIPTS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, SCRIPTS_DIR)
import validate_ci_reliability_assessment_v1 as validator  # noqa: E402

_SIBLING_TEST_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "test_validate_ci_reliability_assessment_v1.py"
)
_SIBLING_MODULE_NAME = "_issue_2507_timed_out_regression_fixture_helpers"
_spec = importlib.util.spec_from_file_location(_SIBLING_MODULE_NAME, _SIBLING_TEST_PATH)
assert _spec is not None and _spec.loader is not None
_fixture_helpers = importlib.util.module_from_spec(_spec)
sys.modules[_SIBLING_MODULE_NAME] = _fixture_helpers
_spec.loader.exec_module(_fixture_helpers)
_make_assessment = _fixture_helpers._make_assessment


def _validate_document(document: dict, tmp_path) -> tuple[int, dict]:
    path = tmp_path / "assessment.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return validator.validate_assessment(str(path))


def _set_conclusion(document: dict, arm: str, workflow_run_id: int, conclusion: str) -> None:
    found = False
    for record in document["workflow_records"]:
        if record["arm"] == arm and record["workflow_run_id"] == workflow_run_id:
            record["conclusion"] = conclusion
            found = True
    assert found, f"workflow_run_id={workflow_run_id} not found in arm={arm}"


def _set_observation_classification(
    document: dict, arm: str, metric: str, workflow_run_id: int, classification: str
) -> None:
    observations = document["sample_provenance"][arm][metric]["observations"]
    found = False
    for observation in observations:
        if observation["workflow_run_id"] == workflow_run_id:
            observation["classification"] = classification
            found = True
    assert found, f"observation for workflow_run_id={workflow_run_id} not found in {arm}.{metric}"


def test_timed_out_classified_as_workflow_failure(tmp_path):
    # GIVEN a valid assessment whose "before" failure run (workflow_run_id=1000)
    # is a workflow that timed out instead of explicitly failing
    document = _make_assessment()
    _set_conclusion(document, "before", 1000, "timed_out")

    # WHEN validated through the public entry point
    exit_code, decision = _validate_document(document, tmp_path)

    # THEN it is accepted, with no eligibility rejection, and the recomputed
    # workflow_failure_rate numerator/denominator remain unchanged (1/22)
    assert decision["structural_valid"] is True
    assert decision["semantic_valid"] is True
    assert exit_code == 0
    assert not any("ineligible_workflow_run_sample" in error for error in decision["errors"])
    recomputed = decision["recomputed_reliability_metrics"]["before"]["workflow_failure_rate"]
    assert recomputed["numerator"] == 1
    assert recomputed["denominator"] == 22


def test_timed_out_workflow_run_is_eligible_sample(tmp_path):
    # GIVEN the same timed_out-substituted fixture
    document = _make_assessment()
    _set_conclusion(document, "before", 1000, "timed_out")

    # WHEN validated
    exit_code, decision = _validate_document(document, tmp_path)

    # THEN the timed_out workflow_run_id=1000 sample is never rejected as an
    # ineligible workflow-run sample, for any of the 3 shared metrics
    assert exit_code == 0
    assert not any(
        "ineligible_workflow_run_sample" in error and "workflow_run_id=1000" in error for error in decision["errors"]
    )


def test_timed_out_does_not_affect_playwright_classification(tmp_path):
    # GIVEN a timed_out workflow run (1000) that carries no Playwright
    # TestCase at all (its correct Playwright classification is
    # "not_affected" purely because no `flaky` / `unexpected` case exists)
    document = _make_assessment()
    _set_conclusion(document, "before", 1000, "timed_out")

    # WHEN validated as-is (provenance still correctly says not_affected)
    exit_code, decision = _validate_document(document, tmp_path)

    # THEN it is accepted — a timed_out conclusion alone does not force the
    # Playwright observation to "affected"
    assert exit_code == 0
    assert decision["semantic_valid"] is True

    # AND WHEN provenance is corrupted to falsely claim "affected" for that
    # same timed_out run (as if the workflow's timed_out conclusion alone
    # justified an "affected" Playwright classification)
    corrupted = copy.deepcopy(document)
    _set_observation_classification(corrupted, "before", "playwright_flaky_test_rate", 1000, "affected")
    exit_code, decision = _validate_document(corrupted, tmp_path)

    # THEN it is rejected: the canonical classification is still derived
    # purely from the official TestCase outcome (none present -> not_affected),
    # proving the timed_out conclusion does not by itself force "affected"
    assert exit_code == 2
    assert any(
        "canonical_classification_mismatch" in error
        and "playwright_flaky_test_rate" in error
        and "workflow_run_id=1000" in error
        for error in decision["errors"]
    )


def test_timed_out_canonical_classification_mismatch_rejected(tmp_path):
    # GIVEN the timed_out-substituted fixture, but with workflow_failure_rate
    # provenance deliberately corrupted to claim "success" instead of the
    # correct "failure" classification
    document = _make_assessment()
    _set_conclusion(document, "before", 1000, "timed_out")
    _set_observation_classification(document, "before", "workflow_failure_rate", 1000, "success")

    # WHEN validated
    exit_code, decision = _validate_document(document, tmp_path)

    # THEN it is rejected specifically for a canonical_classification_mismatch
    # (not merely the absence of ineligible_workflow_run_sample)
    assert exit_code == 2
    assert any(
        "canonical_classification_mismatch" in error
        and "workflow_failure_rate" in error
        and "workflow_run_id=1000" in error
        for error in decision["errors"]
    )


def test_timed_out_requires_three_metric_provenance(tmp_path):
    # GIVEN a timed_out, eligible workflow run (1000) whose Playwright
    # provenance observation has been deliberately removed entirely (not
    # merely misclassified) for one of the 3 shared metrics
    document = _make_assessment()
    _set_conclusion(document, "before", 1000, "timed_out")
    observations = document["sample_provenance"]["before"]["playwright_flaky_test_rate"]["observations"]
    document["sample_provenance"]["before"]["playwright_flaky_test_rate"]["observations"] = [
        observation for observation in observations if observation["workflow_run_id"] != 1000
    ]

    # WHEN validated
    exit_code, decision = _validate_document(document, tmp_path)

    # THEN the existing completeness/missing-provenance validation (unchanged
    # by this Issue) still rejects it, proving the eligible timed_out run is
    # required to have provenance for all 3 metrics, not just
    # workflow_failure_rate
    assert exit_code == 2
    assert any(
        "sample_provenance_missing_eligible_workflow_run" in error
        and "playwright_flaky_test_rate" in error
        and "workflow_run_id=1000" in error
        for error in decision["errors"]
    )


def test_cancelled_conclusion_still_ineligible(tmp_path):
    # GIVEN a "before" workflow run (1003, previously "success" with no
    # Playwright TestCase) whose conclusion is instead "cancelled"
    document = _make_assessment()
    _set_conclusion(document, "before", 1003, "cancelled")

    # WHEN validated
    exit_code, decision = _validate_document(document, tmp_path)

    # THEN it is still rejected as an ineligible workflow-run sample — the
    # `timed_out` eligibility extension must not widen eligibility to
    # `cancelled` (this boundary previously had zero test coverage)
    assert exit_code == 2
    assert any(
        "ineligible_workflow_run_sample" in error and "workflow_run_id=1003" in error for error in decision["errors"]
    )
