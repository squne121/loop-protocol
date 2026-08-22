#!/usr/bin/env python3
"""Tests for the finding_contract.evaluations[] 5-axis delta judgement table
(Issue #2288 AC3, plus the delta-related portion of AC7).

This module does NOT implement or exercise a delta-*computation* algorithm (that is
Issue #2237's responsibility, see the module docstring of
validate_retrospective_schema.py) -- it only proves that already-produced evaluation
records representing each branch of the judgement table are representable in, and
correctly accepted/rejected by, the schema + validate_candidate()/
validate_finding_contract_history():

  - new
  - resolved (source_coverage complete)
  - recurrent + signal_delta regressed (simultaneous representation)
  - unchanged (distinguished by signal_delta value: unchanged vs improved)
  - indeterminate (partial source coverage does not resolve to 'resolved')
  - multi-evaluation history chained via previous_evaluation_ref
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import jsonschema
import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import validate_retrospective_schema as vrs  # noqa: E402


def _evaluation_of(fixture_name):
    candidate = vrs.load_fixture(fixture_name)
    return candidate, candidate["finding_contract"]["evaluations"][0]


# ---------------------------------------------------------------------------
# positive: each judgement-table branch fixture validates and has the expected
# axis values
# ---------------------------------------------------------------------------


def test_new_fixture_evaluation_axes():
    candidate, evaluation = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.new.valid.json"
    )
    vrs.validate_candidate(candidate)
    assert evaluation["presence_delta"] == "new"
    assert evaluation["delta_status"] == "new"
    assert evaluation["evaluation_status"] == "classified"
    assert evaluation["indeterminate_reason"] is None


def test_resolved_fixture_requires_complete_source_coverage():
    candidate, evaluation = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.resolved.valid.json"
    )
    vrs.validate_candidate(candidate)
    assert evaluation["presence_delta"] == "resolved"
    assert evaluation["delta_status"] == "resolved"
    assert evaluation["source_coverage"] == "complete"
    assert evaluation["observed"] is False


def test_recurrent_and_regressed_simultaneous_representation():
    candidate, evaluation = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.recurrent_regressed.valid.json"
    )
    vrs.validate_candidate(candidate)
    assert evaluation["presence_delta"] == "recurrent"
    assert evaluation["signal_delta"] == "regressed"
    # delta_status (single-enum summary) takes 'recurrent' precedence, but the
    # underlying regression is still separately observable via signal_delta.
    assert evaluation["delta_status"] == "recurrent"


@pytest.mark.parametrize(
    "fixture_name,expected_signal_delta",
    [
        ("agent_improvement_candidate_v1.finding_contract.unchanged.valid.json", "unchanged"),
        ("agent_improvement_candidate_v1.finding_contract.improved.valid.json", "improved"),
    ],
)
def test_unchanged_delta_status_distinguished_by_signal_delta(fixture_name, expected_signal_delta):
    candidate, evaluation = _evaluation_of(fixture_name)
    vrs.validate_candidate(candidate)
    assert evaluation["presence_delta"] == "active"
    assert evaluation["signal_delta"] == expected_signal_delta
    assert evaluation["delta_status"] == "unchanged"


def test_indeterminate_with_partial_source_coverage_is_not_resolved():
    candidate, evaluation = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.indeterminate.valid.json"
    )
    vrs.validate_candidate(candidate)
    assert evaluation["source_coverage"] == "partial"
    assert evaluation["evaluation_status"] == "indeterminate"
    assert "delta_status" not in evaluation
    assert evaluation["indeterminate_reason"] == "source_partial"


def test_history_chain_validates_and_links_previous_evaluation_ref():
    candidate = vrs.load_fixture(
        "agent_improvement_candidate_v1.finding_contract.history_chain.valid.json"
    )
    vrs.validate_candidate(candidate)
    evaluations = candidate["finding_contract"]["evaluations"]
    assert len(evaluations) == 2
    assert evaluations[0]["previous_evaluation_ref"] is None
    assert evaluations[1]["previous_evaluation_ref"] == evaluations[0]["evaluation_id"]


# ---------------------------------------------------------------------------
# negative: each schema-level mutual-exclusivity invariant is enforced
# ---------------------------------------------------------------------------


def test_indeterminate_evaluation_cannot_carry_a_delta_status():
    candidate, _ = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.indeterminate.valid.json"
    )
    candidate = copy.deepcopy(candidate)
    candidate["finding_contract"]["evaluations"][0]["delta_status"] = "resolved"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_classified_evaluation_requires_null_indeterminate_reason():
    candidate, _ = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.new.valid.json"
    )
    candidate = copy.deepcopy(candidate)
    candidate["finding_contract"]["evaluations"][0]["indeterminate_reason"] = "source_partial"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_classified_evaluation_requires_delta_status():
    candidate, _ = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.new.valid.json"
    )
    candidate = copy.deepcopy(candidate)
    del candidate["finding_contract"]["evaluations"][0]["delta_status"]
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_incomplete_source_coverage_forces_indeterminate_status():
    candidate, _ = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.resolved.valid.json"
    )
    candidate = copy.deepcopy(candidate)
    candidate["finding_contract"]["evaluations"][0]["source_coverage"] = "unavailable"
    # evaluation_status stays 'classified' while source_coverage is incomplete -- invalid
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_recurrent_presence_delta_forces_delta_status_recurrent_precedence():
    candidate, _ = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.recurrent_regressed.valid.json"
    )
    candidate = copy.deepcopy(candidate)
    # attempting to report delta_status='regressed' despite presence_delta='recurrent'
    # violates the fixed precedence rule (recurrent takes summary precedence).
    candidate["finding_contract"]["evaluations"][0]["delta_status"] = "regressed"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_classified_evaluation_without_evidence_or_baseline_rejected():
    candidate, _ = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.new.valid.json"
    )
    candidate = copy.deepcopy(candidate)
    candidate["finding_contract"]["evaluations"][0]["evidence_refs"] = []
    candidate["finding_contract"]["evaluations"][0]["baseline_signal"] = None
    candidate["finding_contract"]["evaluations"][0]["current_signal"] = None
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_evaluation_history_inconsistency_rejected():
    candidate = copy.deepcopy(
        vrs.load_fixture(
            "agent_improvement_candidate_v1.finding_contract.history_chain.valid.json"
        )
    )
    candidate["finding_contract"]["evaluations"][1]["previous_evaluation_ref"] = (
        "sha256:" + "9" * 64
    )
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_candidate(candidate)


def test_first_evaluation_with_non_null_previous_ref_rejected():
    candidate = copy.deepcopy(
        vrs.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")
    )
    candidate["finding_contract"]["evaluations"][0]["previous_evaluation_ref"] = (
        "sha256:" + "9" * 64
    )
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_candidate(candidate)


# ---------------------------------------------------------------------------
# regression fixtures: full invalid-fixture set loads and fails validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name",
    [
        "agent_improvement_candidate_v1.finding_contract.invalid_partial_envelope.json",
        "agent_improvement_candidate_v1.finding_contract.invalid_malformed_identity.json",
        "agent_improvement_candidate_v1.finding_contract.invalid_stale_identity.json",
        "agent_improvement_candidate_v1.finding_contract.invalid_claim_class.json",
        "agent_improvement_candidate_v1.finding_contract.invalid_incomplete_coverage_resolved.json",
        "agent_improvement_candidate_v1.finding_contract.invalid_no_evidence_classified.json",
        "agent_improvement_candidate_v1.finding_contract.invalid_raw_evidence.json",
        "agent_improvement_candidate_v1.finding_contract.invalid_history_inconsistency.json",
    ],
)
def test_finding_contract_invalid_fixtures_fail_schema_validation(fixture_name):
    instance = vrs.load_fixture(fixture_name)
    assert vrs.is_valid_candidate(instance) is False
    with pytest.raises((jsonschema.exceptions.ValidationError, vrs.RetrospectiveSchemaError)):
        vrs.validate_candidate(instance)
