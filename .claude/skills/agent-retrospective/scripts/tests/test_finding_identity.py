#!/usr/bin/env python3
"""Tests for finding_contract.identity (FINDING_IDENTITY_V1) determinism and
tamper-resistance (Issue #2288 AC2).

Covers compute_finding_identity() / validate_finding_identity() in
validate_retrospective_schema.py:

  (a) an identity value copied from a different key ("stale"/copied/fabricated ID)
      is rejected by validate_finding_identity()
  (b) dict key order does not change the computed identity value
  (c) list/array element order (within an identity key) does not change the
      computed identity value (arrays are treated as sets)
  (d) a different `algorithm` value produces a different digest namespace
  (e) identity is independent of source_run_ref/base_sha/source_set_digest/
      timestamps -- these are simply not part of the identity key
  (f) claim_class/subject_ref/rule_id changes each produce a different identity
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import validate_retrospective_schema as vrs  # noqa: E402

BASE_KEY = {
    "repository_id": "squne121/loop-protocol",
    "claim_class": "runtime_behavior",
    "subject_ref": {"kind": "repository_path", "value": "scripts/foo.py"},
    "rule_id": "example_rule",
}


def _load_valid_finding_candidate():
    return vrs.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_compute_finding_identity_deterministic_same_input():
    value_1 = vrs.compute_finding_identity(copy.deepcopy(BASE_KEY))
    value_2 = vrs.compute_finding_identity(copy.deepcopy(BASE_KEY))
    assert value_1 == value_2
    assert value_1.startswith("sha256:")
    assert len(value_1) == len("sha256:") + 64


def test_compute_finding_identity_format_matches_pattern():
    value = vrs.compute_finding_identity(BASE_KEY)
    digest = value.removeprefix("sha256:")
    assert len(digest) == 64
    int(digest, 16)  # raises ValueError if not hex


# ---------------------------------------------------------------------------
# (a) stale / copied identity is rejected
# ---------------------------------------------------------------------------


def test_validate_finding_identity_rejects_copied_stale_id():
    candidate = copy.deepcopy(_load_valid_finding_candidate())
    other_key = copy.deepcopy(BASE_KEY)
    other_key["rule_id"] = "a_completely_different_rule"
    candidate["finding_contract"]["identity"]["value"] = vrs.compute_finding_identity(other_key)
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_finding_identity(candidate)


def test_validate_finding_identity_accepts_correctly_recomputed_id():
    candidate = copy.deepcopy(_load_valid_finding_candidate())
    vrs.validate_finding_identity(candidate)  # no error


def test_validate_finding_identity_is_noop_for_legacy_candidate():
    legacy = vrs.load_fixture("agent_improvement_candidate_v1.valid.json")
    assert "finding_contract" not in legacy
    vrs.validate_finding_identity(legacy)  # no error, no-op


# ---------------------------------------------------------------------------
# (b) object key order independence
# ---------------------------------------------------------------------------


def test_compute_finding_identity_key_order_independent():
    key_a = {
        "repository_id": "squne121/loop-protocol",
        "claim_class": "runtime_behavior",
        "subject_ref": {"kind": "repository_path", "value": "scripts/foo.py"},
        "rule_id": "example_rule",
    }
    key_b = {
        "rule_id": "example_rule",
        "subject_ref": {"value": "scripts/foo.py", "kind": "repository_path"},
        "claim_class": "runtime_behavior",
        "repository_id": "squne121/loop-protocol",
    }
    assert vrs.compute_finding_identity(key_a) == vrs.compute_finding_identity(key_b)


# ---------------------------------------------------------------------------
# (c) array element order independence (arrays treated as sets)
# ---------------------------------------------------------------------------


def test_compute_finding_identity_array_order_independent():
    key_a = {**BASE_KEY, "related_tags": ["alpha", "beta", "gamma"]}
    key_b = {**BASE_KEY, "related_tags": ["gamma", "alpha", "beta"]}
    assert vrs.compute_finding_identity(key_a) == vrs.compute_finding_identity(key_b)


def test_compute_finding_identity_array_content_change_changes_id():
    key_a = {**BASE_KEY, "related_tags": ["alpha", "beta"]}
    key_b = {**BASE_KEY, "related_tags": ["alpha", "beta", "gamma"]}
    assert vrs.compute_finding_identity(key_a) != vrs.compute_finding_identity(key_b)


# ---------------------------------------------------------------------------
# (d) algorithm version produces a different namespace
# ---------------------------------------------------------------------------


def test_compute_finding_identity_algorithm_change_changes_namespace():
    value_v1 = vrs.compute_finding_identity(BASE_KEY, algorithm="sha256-jcs-v1")
    value_v2 = vrs.compute_finding_identity(BASE_KEY, algorithm="sha256-jcs-v2-hypothetical")
    assert value_v1 != value_v2


# ---------------------------------------------------------------------------
# (e) run/occurrence-varying inputs are not part of the identity key
# ---------------------------------------------------------------------------


def test_finding_identity_independent_of_run_and_timestamp_fields():
    candidate_1 = copy.deepcopy(_load_valid_finding_candidate())
    candidate_2 = copy.deepcopy(_load_valid_finding_candidate())

    # Mutate run/occurrence-varying fields that are NOT part of the identity key.
    candidate_2["source_run_ref"]["base_sha"] = "0" * 40
    candidate_2["source_run_ref"]["source_set_digest"] = "1" * 64
    candidate_2["created_at"] = "2030-01-01T00:00:00Z"
    candidate_2["updated_at"] = "2030-01-01T00:00:00Z"
    candidate_2["finding_contract"]["evaluations"][0]["evaluated_run_ref"]["base_sha"] = "2" * 40
    candidate_2["finding_contract"]["evaluations"][0]["classified_at"] = "2030-01-01T00:00:00Z"

    assert (
        candidate_1["finding_contract"]["identity"]["value"]
        == candidate_2["finding_contract"]["identity"]["value"]
    )
    # both are still self-consistent (identity unaffected by the mutation)
    vrs.validate_finding_identity(candidate_1)
    vrs.validate_finding_identity(candidate_2)


# ---------------------------------------------------------------------------
# (f) claim_class / subject_ref / rule_id changes produce a different identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutation",
    [
        lambda key: {**key, "claim_class": "external_fact"},
        lambda key: {**key, "subject_ref": {"kind": "issue", "value": "1234"}},
        lambda key: {**key, "rule_id": "a_different_rule"},
        lambda key: {**key, "repository_id": "some/other-repo"},
    ],
)
def test_compute_finding_identity_changes_with_key_component(mutation):
    mutated_key = mutation(copy.deepcopy(BASE_KEY))
    assert vrs.compute_finding_identity(BASE_KEY) != vrs.compute_finding_identity(mutated_key)
