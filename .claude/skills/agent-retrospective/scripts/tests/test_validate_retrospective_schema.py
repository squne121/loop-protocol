#!/usr/bin/env python3
"""Tests for validate_retrospective_schema.py (Issue #2235).

Covers:
  - AC1/AC6/AC7: agent_retrospective_run_v1.schema.json shape (via fixture validation)
  - AC2: agent_improvement_candidate_v1.schema.json candidate_status enum
  - AC3: valid/invalid fixture pass/fail behavior
  - AC4: invalid candidate_status transition rejection
  - AC5: compute_source_set_digest() determinism (including array-order independence)
  - Post-review hardening (PR #2266 review, ADR 0007 sync): source_id/source_status
    per Child 3 (#2236) adapter, digest consistency (validate_run rejects a stale or
    fabricated run_identity.source_set_digest), format ("date-time") checking, and
    conditional invariants (partial_reason / rejection_reason / superseded_by /
    implementation_issue_ref).
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
    # missing source_id/source_status, missing fetch_started_at/fetch_completed_at on
    # a non-repository source)
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
# AC7 (Issue #2288): finding_contract invariant-violation invalid fixtures fail
# schema validation (regression alongside the pre-existing invalid fixture above)
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
    # GIVEN a fixture violating a finding_contract invariant (partial envelope,
    # malformed/stale identity, invalid claim_class, incomplete-coverage resolved
    # classification, evidence/baseline-less classified evaluation, raw evidence
    # field, or evaluation-history inconsistency)
    # WHEN validated THEN validation fails (ValidationError or RetrospectiveSchemaError)
    instance = vrs.load_fixture(fixture_name)
    with pytest.raises((jsonschema.exceptions.ValidationError, vrs.RetrospectiveSchemaError)):
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
        # Post-review hardening: rejected/superseded are terminal states reachable
        # from every non-terminal state (not just proposed/accepted).
        ("implementation_issue_created", "rejected"),
        ("implemented", "rejected"),
        ("validating", "rejected"),
        ("validated", "rejected"),
        ("validating", "superseded"),
    ],
)
def test_valid_transition_accepted(from_status, to_status):
    assert vrs.validate_transition(from_status, to_status) is True


def test_invalid_transition_unknown_status_raises():
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_transition("proposed", "in_review")
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_transition("in_review", "proposed")


def test_terminal_states_reachable_from_every_non_terminal_state():
    # GIVEN every non-terminal candidate_status WHEN checked THEN both rejected and
    # superseded are reachable direct transitions (matches the schema description's
    # normative claim, not just an implementation detail).
    non_terminal = {
        "proposed",
        "accepted",
        "implementation_issue_created",
        "implemented",
        "validating",
        "validated",
    }
    for status in non_terminal:
        assert vrs.validate_transition(status, "rejected") is True
        assert vrs.validate_transition(status, "superseded") is True


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
    mutated[1]["pagination_completeness"] = "partial"
    mutated[1]["partial_reason"] = "rate_limit"
    digest_mutated = vrs.compute_source_set_digest(mutated)

    assert digest_original != digest_mutated


def test_digest_deterministic_key_order_independent():
    a = [
        {
            "source_type": "repository",
            "source_id": "repository",
            "source_status": "complete",
            "pagination_completeness": "complete",
        }
    ]
    b = [
        {
            "pagination_completeness": "complete",
            "source_status": "complete",
            "source_id": "repository",
            "source_type": "repository",
        }
    ]
    assert vrs.compute_source_set_digest(a) == vrs.compute_source_set_digest(b)


def test_digest_deterministic_array_order_independent():
    # Post-review hardening: two adapters (Child 3 / #2236) racing to append their
    # observation in a different order must not change the digest of an otherwise
    # identical source set (dict-key-order independence alone is insufficient).
    run = vrs.load_fixture("agent_retrospective_run_v1.valid.json")
    observations = run["source_observations"]
    assert len(observations) >= 2
    reordered = list(reversed(observations))
    assert vrs.compute_source_set_digest(observations) == vrs.compute_source_set_digest(
        reordered
    )


# ---------------------------------------------------------------------------
# Post-review hardening: digest consistency (validate_run rejects stale/fabricated
# run_identity.source_set_digest)
# ---------------------------------------------------------------------------


def test_validate_run_rejects_stale_source_set_digest():
    instance = copy.deepcopy(vrs.load_fixture("agent_retrospective_run_v1.valid.json"))
    instance["run_identity"]["source_set_digest"] = "0" * 64
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_run(instance)
    assert vrs.is_valid_run(instance) is False


def test_validate_run_accepts_digest_recomputed_after_mutation():
    instance = copy.deepcopy(vrs.load_fixture("agent_retrospective_run_v1.valid.json"))
    instance["source_observations"][1]["pagination_completeness"] = "partial"
    instance["source_observations"][1]["partial_reason"] = "rate_limit"
    # stale digest from before the mutation must now be rejected
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_run(instance)
    # recomputing and updating the digest makes the instance valid again
    instance["run_identity"]["source_set_digest"] = vrs.compute_source_set_digest(
        instance["source_observations"]
    )
    vrs.validate_run(instance)


# ---------------------------------------------------------------------------
# Post-review hardening: source_id / source_status (Child 3 / #2236 adapter identity)
# ---------------------------------------------------------------------------


def test_run_schema_source_observation_requires_source_id_and_source_status():
    schema = vrs.load_run_schema()
    source_observation = schema["$defs"]["source_observation"]
    assert "source_id" in source_observation["required"]
    assert "source_status" in source_observation["required"]
    assert set(source_observation["properties"]["source_id"]["enum"]) == {
        "claude_code",
        "claude_gpt",
        "repository",
        "github",
        "web",
    }
    assert set(source_observation["properties"]["source_status"]["enum"]) == {
        "complete",
        "partial",
        "unavailable",
        "blocked",
    }


def test_run_valid_fixture_distinguishes_claude_code_and_claude_gpt_source_ids():
    # source_type alone collapses claude_code/claude_gpt into "runtime"; source_id
    # must disambiguate the two Child 3 (#2236) adapters.
    base = copy.deepcopy(vrs.load_fixture("agent_retrospective_run_v1.valid.json"))
    claude_code_obs = {
        "source_type": "runtime",
        "source_id": "claude_code",
        "source_status": "complete",
        "fetch_started_at": "2026-08-20T00:00:00Z",
        "fetch_completed_at": "2026-08-20T00:01:00Z",
        "pagination_completeness": "complete",
    }
    claude_gpt_obs = {
        "source_type": "runtime",
        "source_id": "claude_gpt",
        "source_status": "complete",
        "fetch_started_at": "2026-08-20T00:00:00Z",
        "fetch_completed_at": "2026-08-20T00:01:00Z",
        "pagination_completeness": "complete",
    }
    base["source_observations"] = base["source_observations"] + [
        claude_code_obs,
        claude_gpt_obs,
    ]
    base["run_identity"]["source_set_digest"] = vrs.compute_source_set_digest(
        base["source_observations"]
    )
    vrs.validate_run(base)  # no error


# ---------------------------------------------------------------------------
# Post-review hardening: format ("date-time") checking
# ---------------------------------------------------------------------------


def test_validate_run_rejects_malformed_date_time():
    instance = copy.deepcopy(vrs.load_fixture("agent_retrospective_run_v1.valid.json"))
    instance["run_identity"]["generated_at"] = "not-a-timestamp"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_run(instance)


def test_validate_candidate_rejects_malformed_date_time():
    instance = copy.deepcopy(vrs.load_fixture("agent_improvement_candidate_v1.valid.json"))
    instance["created_at"] = "not-a-timestamp"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(instance)


# ---------------------------------------------------------------------------
# Post-review hardening: conditional invariants (allOf/if/then)
# ---------------------------------------------------------------------------


def test_run_schema_partial_pagination_requires_partial_reason():
    instance = copy.deepcopy(vrs.load_fixture("agent_retrospective_run_v1.valid.json"))
    instance["source_observations"][1]["pagination_completeness"] = "partial"
    # partial_reason intentionally omitted
    instance["run_identity"]["source_set_digest"] = vrs.compute_source_set_digest(
        instance["source_observations"]
    )
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_run(instance)


@pytest.mark.parametrize(
    "candidate_status,missing_field",
    [
        ("implementation_issue_created", "implementation_issue_ref"),
        ("implemented", "implementation_issue_ref"),
        ("validating", "implementation_issue_ref"),
        ("validated", "implementation_issue_ref"),
        ("rejected", "rejection_reason"),
        ("superseded", "superseded_by"),
    ],
)
def test_candidate_schema_requires_status_specific_field(candidate_status, missing_field):
    instance = copy.deepcopy(
        vrs.load_fixture("agent_improvement_candidate_v1.valid.json")
    )
    instance["candidate_status"] = candidate_status
    assert missing_field not in instance
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(instance)


@pytest.mark.parametrize(
    "candidate_status,extra_fields",
    [
        (
            "implementation_issue_created",
            {"implementation_issue_ref": "https://github.com/squne121/loop-protocol/issues/9999"},
        ),
        (
            "rejected",
            {"rejection_reason": "duplicate of an existing candidate"},
        ),
        (
            "superseded",
            {"superseded_by": "cand-9999"},
        ),
    ],
)
def test_candidate_schema_accepts_status_with_required_field_present(
    candidate_status, extra_fields
):
    instance = copy.deepcopy(
        vrs.load_fixture("agent_improvement_candidate_v1.valid.json")
    )
    instance["candidate_status"] = candidate_status
    instance.update(extra_fields)
    vrs.validate_candidate(instance)  # no error


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
    assert "fetch_started_at" in source_observation["properties"]
    assert "fetch_completed_at" in source_observation["properties"]
