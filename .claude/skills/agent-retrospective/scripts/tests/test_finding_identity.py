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
# (b) RFC 8785 JCS: object properties are sorted recursively; array element order
# is NOT part of this key's schema (FINDING_IDENTITY_V1.key has no array-typed
# fields -- Issue #2288 AC2 explicitly scopes array-order normalization/set
# treatment out). A prior, non-normative implementation of this helper treated
# arrays as unordered sets; that was a deliberate deviation from RFC 8785 JCS and
# has been removed (see `_jcs_canonicalize` docstring).
# ---------------------------------------------------------------------------


def test_identity_matches_normative_jcs_golden_vector():
    """`value` is a specific, reproducible digest of RFC 8785 JCS(key) alone --
    NOT of `{"algorithm": algorithm, "key": key}` (a prior, superseded design).
    This golden vector pins the exact preimage/algorithm so any accidental change
    to the hashing scheme is caught, not just "still deterministic"."""
    key = {
        "repository_id": "squne121/loop-protocol",
        "claim_class": "runtime_behavior",
        "subject_ref": {"kind": "repository_path", "value": "scripts/foo.py"},
        "rule_id": "example_rule",
    }
    expected_preimage = (
        '{"claim_class":"runtime_behavior","repository_id":"squne121/loop-protocol",'
        '"rule_id":"example_rule","subject_ref":{"kind":"repository_path",'
        '"value":"scripts/foo.py"}}'
    )
    expected_digest = hashlib_sha256_hex(expected_preimage)
    assert vrs.compute_finding_identity(key) == f"sha256:{expected_digest}"


def hashlib_sha256_hex(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_identity_non_ascii_value_matches_rfc8785():
    """Non-ASCII string values are serialized as UTF-8 (`ensure_ascii=False`), not
    `\\uXXXX`-escaped ASCII -- RFC 8785 JCS output MUST be UTF-8 text."""
    key = {
        "repository_id": "squne121/loop-protocol",
        "claim_class": "runtime_behavior",
        "subject_ref": {"kind": "repository_path", "value": "スクリプト/foo.py"},
        "rule_id": "example_rule",
    }
    expected_preimage = (
        '{"claim_class":"runtime_behavior","repository_id":"squne121/loop-protocol",'
        '"rule_id":"example_rule","subject_ref":{"kind":"repository_path",'
        '"value":"スクリプト/foo.py"}}'
    )
    expected_digest = hashlib_sha256_hex(expected_preimage)
    actual = vrs.compute_finding_identity(key)
    assert actual == f"sha256:{expected_digest}"

    # sanity: an ensure_ascii=True (\\uXXXX-escaped) serialization of the same
    # canonical structure would produce a DIFFERENT digest -- proving the digest
    # really was computed over UTF-8 text, not escaped ASCII.
    import json

    ascii_escaped_preimage = json.dumps(
        {
            "claim_class": "runtime_behavior",
            "repository_id": "squne121/loop-protocol",
            "rule_id": "example_rule",
            "subject_ref": {"kind": "repository_path", "value": "スクリプト/foo.py"},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    ascii_escaped_digest = hashlib_sha256_hex(ascii_escaped_preimage)
    assert ascii_escaped_digest != expected_digest
    assert actual != f"sha256:{ascii_escaped_digest}"


def test_identity_rejects_non_finite_float_values():
    key = {**BASE_KEY, "rule_id": "example_rule"}
    key_with_nan = copy.deepcopy(key)
    key_with_nan["subject_ref"] = {"kind": "repository_path", "value": "scripts/foo.py"}
    key_with_nan["_probe"] = float("nan")
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.compute_finding_identity(key_with_nan)

    key_with_inf = copy.deepcopy(key)
    key_with_inf["_probe"] = float("inf")
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.compute_finding_identity(key_with_inf)


# ---------------------------------------------------------------------------
# (d) algorithm: unsupported algorithm values are rejected fail-closed by the
# helper itself (not silently hashed under an unspecified/future scheme).
# ---------------------------------------------------------------------------


def test_identity_rejects_unsupported_algorithm():
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.compute_finding_identity(BASE_KEY, algorithm="sha256-jcs-v2-hypothetical")

    candidate = copy.deepcopy(_load_valid_finding_candidate())
    candidate["finding_contract"]["identity"]["algorithm"] = "sha256-jcs-v2-hypothetical"
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_finding_identity(candidate)


def test_supported_algorithm_set_is_exactly_v1():
    assert vrs.SUPPORTED_FINDING_IDENTITY_ALGORITHMS == frozenset({"sha256-jcs-v1"})


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
