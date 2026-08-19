#!/usr/bin/env python3
"""Tests for validate_retrospective_schema.py (Issue #2235).

Covers:
  - AC1/AC6/AC7: agent_retrospective_run_v1.schema.json shape (via fixture validation)
  - AC2: agent_improvement_candidate_v1.schema.json candidate_status enum
  - AC3: valid/invalid fixture pass/fail behavior
  - AC4: invalid candidate_status transition rejection
  - AC5: compute_source_set_digest() determinism
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


# ---------------------------------------------------------------------------
# AC3: valid fixture -> pass, invalid fixture -> fail
# ---------------------------------------------------------------------------


def test_run_valid_fixture_passes_schema_validation():
    instance = vrs.load_fixture("agent_retrospective_run_v1.valid.json")
    vrs.validate_run(instance)  # GIVEN a valid run fixture WHEN validated THEN no error
    assert vrs.is_valid_run(instance) is True


def test_run_invalid_fixture_fails_schema_validation():
    instance = vrs.load_fixture("agent_retrospective_run_v1.invalid.json")
    # GIVEN an invalid run fixture (bad base_sha, missing source_set_digest,
    # observed_from/observed_until missing on a non-repository source)
    # WHEN validated THEN a ValidationError is raised
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_run(instance)
    assert vrs.is_valid_run(instance) is False


def test_candidate_valid_fixture_passes_schema_validation():
    instance = vrs.load_fixture("agent_improvement_candidate_v1.valid.json")
    vrs.validate_candidate(instance)
    assert vrs.is_valid_candidate(instance) is True


def test_candidate_invalid_fixture_fails_schema_validation():
    instance = vrs.load_fixture("agent_improvement_candidate_v1.invalid.json")
    # GIVEN a fixture with candidate_status outside the closed enum
    # WHEN validated THEN a ValidationError is raised
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(instance)
    assert vrs.is_valid_candidate(instance) is False


# ---------------------------------------------------------------------------
# AC2: candidate_status closed enum contains all 8 states
# ---------------------------------------------------------------------------


def test_candidate_status_enum_contains_all_eight_states():
    schema = vrs.load_candidate_schema()
    enum_values = set(schema["properties"]["candidate_status"]["enum"])
    expected = {
        "proposed",
        "accepted",
        "implementation_issue_created",
        "implemented",
        "validating",
        "validated",
        "rejected",
        "superseded",
    }
    assert enum_values == expected


# ---------------------------------------------------------------------------
# AC4: invalid candidate_status transitions are rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        ("rejected", "implemented"),
        ("superseded", "proposed"),
        ("proposed", "implemented"),
        ("validated", "proposed"),
        ("implemented", "proposed"),
    ],
)
def test_invalid_transition_rejected(from_status, to_status):
    # GIVEN a disallowed direct transition WHEN checked THEN validate_transition
    # returns False (schema-level enum alone cannot express this)
    assert vrs.validate_transition(from_status, to_status) is False


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        ("proposed", "accepted"),
        ("proposed", "rejected"),
        ("accepted", "implementation_issue_created"),
        ("implementation_issue_created", "implemented"),
        ("implemented", "validating"),
        ("validating", "validated"),
        ("validated", "superseded"),
    ],
)
def test_valid_transition_accepted(from_status, to_status):
    assert vrs.validate_transition(from_status, to_status) is True


def test_invalid_transition_unknown_status_raises():
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_transition("proposed", "in_review")
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_transition("in_review", "proposed")


# ---------------------------------------------------------------------------
# AC5: compute_source_set_digest() determinism
# ---------------------------------------------------------------------------


def test_digest_deterministic_same_input_same_digest():
    run = vrs.load_fixture("agent_retrospective_run_v1.valid.json")
    observations = run["source_observations"]
    digest_1 = vrs.compute_source_set_digest(observations)
    digest_2 = vrs.compute_source_set_digest(copy.deepcopy(observations))
    assert digest_1 == digest_2
    assert len(digest_1) == 64  # sha256 hex digest length


def test_digest_deterministic_differs_for_different_input():
    run = vrs.load_fixture("agent_retrospective_run_v1.valid.json")
    observations = run["source_observations"]
    digest_original = vrs.compute_source_set_digest(observations)

    mutated = copy.deepcopy(observations)
    mutated[0]["pagination_completeness"] = "partial"
    mutated[0]["partial_reason"] = "rate_limit"
    digest_mutated = vrs.compute_source_set_digest(mutated)

    assert digest_original != digest_mutated


def test_digest_deterministic_key_order_independent():
    a = [{"source_type": "repository", "pagination_completeness": "complete"}]
    b = [{"pagination_completeness": "complete", "source_type": "repository"}]
    assert vrs.compute_source_set_digest(a) == vrs.compute_source_set_digest(b)


# ---------------------------------------------------------------------------
# AC1/AC6/AC7: schema shape assertions (structural, independent of fixtures)
# ---------------------------------------------------------------------------


def test_run_schema_has_schema_keyword():
    schema = vrs.load_run_schema()
    assert "$schema" in schema


def test_run_schema_run_identity_required_fields():
    schema = vrs.load_run_schema()
    run_identity = schema["properties"]["run_identity"]
    assert set(run_identity["required"]) == {
        "base_sha",
        "generated_at",
        "runtime_version",
        "source_set_digest",
    }


def test_run_schema_source_observations_required_and_shaped():
    schema = vrs.load_run_schema()
    assert "source_observations" in schema["required"]
    item_schema = schema["properties"]["source_observations"]["items"]
    assert item_schema["$ref"] == "#/$defs/source_observation"
    source_observation = schema["$defs"]["source_observation"]
    assert "source_type" in source_observation["properties"]
    assert "observed_from" in source_observation["properties"]
    assert "observed_until" in source_observation["properties"]
