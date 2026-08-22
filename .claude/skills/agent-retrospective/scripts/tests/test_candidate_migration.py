#!/usr/bin/env python3
"""Tests for the finding_contract migration/backward-compatibility policy
(Issue #2288 AC8).

Legacy candidate records (no `finding_contract`) continue to pass schema
validation, are identifiable as `delta_capability: legacy_unavailable` via
`compute_delta_capability()`, and are never auto-generated/backfilled with a
`finding_contract`/identity by this module.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import validate_retrospective_schema as vrs  # noqa: E402


def test_legacy_candidate_without_finding_contract_passes_schema_validation():
    legacy = vrs.load_fixture("agent_improvement_candidate_v1.valid.json")
    assert "finding_contract" not in legacy
    vrs.validate_candidate(legacy)  # no error
    assert vrs.is_valid_candidate(legacy) is True


def test_legacy_candidate_classified_as_delta_capability_legacy_unavailable():
    legacy = vrs.load_fixture("agent_improvement_candidate_v1.valid.json")
    assert vrs.compute_delta_capability(legacy) == "legacy_unavailable"


def test_finding_contract_candidate_classified_as_evaluated():
    with_contract = vrs.load_fixture(
        "agent_improvement_candidate_v1.finding_contract.new.valid.json"
    )
    assert vrs.compute_delta_capability(with_contract) == "evaluated"


def test_compute_delta_capability_does_not_mutate_candidate():
    legacy = vrs.load_fixture("agent_improvement_candidate_v1.valid.json")
    before = copy.deepcopy(legacy)
    vrs.compute_delta_capability(legacy)
    assert legacy == before
    assert "finding_contract" not in legacy


def test_compute_delta_capability_does_not_auto_generate_identity():
    legacy = copy.deepcopy(vrs.load_fixture("agent_improvement_candidate_v1.valid.json"))
    vrs.compute_delta_capability(legacy)
    # calling the classifier never adds an identity/finding_contract key
    assert "finding_contract" not in legacy
    vrs.validate_finding_identity(legacy)  # still a no-op; nothing was backfilled


def test_legacy_candidate_still_rejects_true_schema_violations():
    # migration tolerance is specific to "finding_contract absent" -- it must not
    # relax any other existing schema constraint (regression check against AC7).
    invalid = vrs.load_fixture("agent_improvement_candidate_v1.invalid.json")
    assert vrs.is_valid_candidate(invalid) is False
    assert vrs.compute_delta_capability(invalid) == "legacy_unavailable"
