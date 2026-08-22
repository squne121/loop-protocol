#!/usr/bin/env python3
"""Tests asserting candidate_status ALLOWED_TRANSITIONS is unchanged by
finding_contract (Issue #2288 AC4).

finding_contract.evaluations[].delta_status is a separate, independent axis from
candidate_status; adding finding_contract must not change candidate_status's
existing 8-state lifecycle or its exact ALLOWED_TRANSITIONS reachability graph.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import validate_retrospective_schema as vrs  # noqa: E402

# Exact mapping this test pins -- any change to validate_retrospective_schema.py's
# ALLOWED_TRANSITIONS must be a deliberate, reviewed change to this test too.
EXPECTED_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"accepted", "rejected", "superseded"},
    "accepted": {"implementation_issue_created", "rejected", "superseded"},
    "implementation_issue_created": {"implemented", "rejected", "superseded"},
    "implemented": {"validating", "rejected", "superseded"},
    "validating": {"validated", "implemented", "rejected", "superseded"},
    "validated": {"rejected", "superseded"},
    "rejected": set(),
    "superseded": set(),
}

EXPECTED_CANDIDATE_STATUSES = frozenset(EXPECTED_ALLOWED_TRANSITIONS)


def test_candidate_status_allowed_transitions_exact_mapping_unchanged():
    assert vrs.ALLOWED_TRANSITIONS == EXPECTED_ALLOWED_TRANSITIONS
    assert vrs.CANDIDATE_STATUSES == EXPECTED_CANDIDATE_STATUSES


def test_candidate_status_enum_in_schema_unchanged_by_finding_contract():
    schema = vrs.load_candidate_schema()
    enum_values = set(schema["properties"]["candidate_status"]["enum"])
    assert enum_values == EXPECTED_CANDIDATE_STATUSES


def test_finding_contract_evaluations_do_not_appear_in_allowed_transitions_keys():
    # delta_status values are a disjoint vocabulary from candidate_status values --
    # they must never leak into the ALLOWED_TRANSITIONS state machine.
    delta_status_values = {"new", "resolved", "recurrent", "regressed", "unchanged"}
    assert delta_status_values.isdisjoint(vrs.CANDIDATE_STATUSES)


def test_finding_contract_present_candidate_still_obeys_lifecycle_state_machine():
    candidate = copy.deepcopy(
        vrs.load_fixture("agent_improvement_candidate_v1.finding_contract.recurrent_regressed.valid.json")
    )
    # candidate_status transitions are validated independently of finding_contract.
    assert vrs.validate_transition("proposed", "accepted") is True
    assert vrs.validate_transition("rejected", "implemented") is False

    candidate["candidate_status"] = "rejected"
    candidate["rejection_reason"] = "duplicate finding"
    vrs.validate_candidate(candidate)  # finding_contract does not block a lifecycle transition
