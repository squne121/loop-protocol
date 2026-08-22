#!/usr/bin/env python3
"""Tests for finding_contract.claim_class exact enum (Issue #2288 AC6).

`claim_class` (both `finding_contract.claim_class` and `finding_contract.identity.
key.claim_class`) is constrained to the ADR 0007 Decision 2 9-value exact enum.
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

ADR_0007_CLAIM_CLASSES = frozenset(
    {
        "code_content",
        "code_authorship_timing",
        "internal_loop_review_verdict",
        "github_native_review_state",
        "review_comment",
        "mergeability",
        "issue_intent",
        "external_fact",
        "runtime_behavior",
    }
)


def test_claim_class_exact_enum_matches_adr_0007():
    assert vrs.CLAIM_CLASSES == ADR_0007_CLAIM_CLASSES
    assert len(vrs.CLAIM_CLASSES) == 9

    schema = vrs.load_candidate_schema()
    schema_claim_class_enum = set(schema["$defs"]["claim_class"]["enum"])
    assert schema_claim_class_enum == ADR_0007_CLAIM_CLASSES


@pytest.mark.parametrize("claim_class", sorted(ADR_0007_CLAIM_CLASSES))
def test_each_adr_0007_claim_class_accepted(claim_class):
    candidate = copy.deepcopy(
        vrs.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")
    )
    candidate["finding_contract"]["claim_class"] = claim_class
    candidate["finding_contract"]["identity"]["key"]["claim_class"] = claim_class
    candidate["finding_contract"]["identity"]["value"] = vrs.compute_finding_identity(
        candidate["finding_contract"]["identity"]["key"]
    )
    vrs.validate_candidate(candidate)  # no error


@pytest.mark.parametrize(
    "invalid_claim_class",
    ["not_a_real_claim_class", "code", "", "RUNTIME_BEHAVIOR", "runtime-behavior"],
)
def test_claim_class_outside_enum_rejected(invalid_claim_class):
    candidate = copy.deepcopy(
        vrs.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")
    )
    candidate["finding_contract"]["claim_class"] = invalid_claim_class
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_claim_class_invalid_in_identity_key_rejected():
    candidate = copy.deepcopy(
        vrs.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")
    )
    candidate["finding_contract"]["identity"]["key"]["claim_class"] = "not_a_real_claim_class"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_claim_class_envelope_and_key_must_match():
    """finding_contract.claim_class and finding_contract.identity.key.claim_class are
    independently-defined schema fields; the schema alone cannot express that they
    must agree. validate_claim_class_consistency() (validate_retrospective_schema.py)
    enforces this cross-field equality (Issue #2288 P0-5)."""
    candidate = copy.deepcopy(
        vrs.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")
    )
    assert (
        candidate["finding_contract"]["claim_class"]
        == candidate["finding_contract"]["identity"]["key"]["claim_class"]
    )
    # both are individually valid ADR 0007 enum values, but they now disagree.
    candidate["finding_contract"]["claim_class"] = "external_fact"
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_candidate(candidate)
