#!/usr/bin/env python3
"""Tests for finding_contract.evaluations[].evidence_refs[] public-safety shape
(Issue #2288 AC5).

`evidence_ref` MUST be a typed object with no raw-evidence-carrying property (full
transcript text, secrets, absolute local paths, unredacted private content). Enforced
both by schema (`additionalProperties: false` field allowlist) and by this dedicated
pytest module (value-level redaction of resource_identity/projection_digest content
itself remains Issue #2239's responsibility -- not tested here).
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

EVIDENCE_REF_ALLOWED_FIELDS = frozenset(
    {"ref_type", "source_id", "resource_identity", "projection_digest"}
)

RAW_EVIDENCE_FIELD_NAMES = [
    "raw_transcript",
    "raw_text",
    "transcript",
    "secret",
    "absolute_path",
    "private_content",
    "prompt",
    "raw_evidence",
]


def _base_candidate():
    return copy.deepcopy(
        vrs.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")
    )


def test_evidence_ref_schema_field_allowlist_matches_public_safe_shape():
    schema = vrs.load_candidate_schema()
    evidence_ref_schema = schema["$defs"]["evidence_ref"]
    assert evidence_ref_schema["additionalProperties"] is False
    assert set(evidence_ref_schema["properties"].keys()) == EVIDENCE_REF_ALLOWED_FIELDS
    assert set(evidence_ref_schema["required"]) == EVIDENCE_REF_ALLOWED_FIELDS


def test_evidence_ref_schema_has_no_raw_evidence_property():
    schema = vrs.load_candidate_schema()
    evidence_ref_properties = set(schema["$defs"]["evidence_ref"]["properties"].keys())
    for raw_field in RAW_EVIDENCE_FIELD_NAMES:
        assert raw_field not in evidence_ref_properties


@pytest.mark.parametrize("raw_field", RAW_EVIDENCE_FIELD_NAMES)
def test_candidate_with_raw_evidence_field_rejected(raw_field):
    candidate = _base_candidate()
    candidate["finding_contract"]["evaluations"][0]["evidence_refs"][0][raw_field] = (
        "untrusted raw content that must never be schema-allowed"
    )
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_candidate_with_well_formed_evidence_ref_accepted():
    candidate = _base_candidate()
    vrs.validate_candidate(candidate)  # no error


@pytest.mark.parametrize(
    "invalid_ref_type",
    ["raw_transcript_dump", "full_page_screenshot", ""],
)
def test_evidence_ref_type_outside_enum_rejected(invalid_ref_type):
    candidate = _base_candidate()
    candidate["finding_contract"]["evaluations"][0]["evidence_refs"][0]["ref_type"] = invalid_ref_type
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_evidence_ref_projection_digest_must_be_sha256():
    candidate = _base_candidate()
    candidate["finding_contract"]["evaluations"][0]["evidence_refs"][0]["projection_digest"] = (
        "not-a-digest"
    )
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_evidence_ref_type_and_source_id_must_match():
    """Each `ref_type` has exactly one valid `source_id`; the schema's per-ref_type
    allOf/if/then branches must reject a mismatched combination (Issue #2288 P1-1),
    not merely validate ref_type/source_id independently against their own enums."""
    candidate = _base_candidate()
    evidence_ref = candidate["finding_contract"]["evaluations"][0]["evidence_refs"][0]
    assert evidence_ref["ref_type"] == "repository_blob"
    assert evidence_ref["source_id"] == "repository"
    # 'github' is individually a valid source_id enum value, but is not the source_id
    # that 'repository_blob' requires.
    evidence_ref["source_id"] = "github"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_evidence_ref_resource_identity_structure_by_ref_type():
    """`resource_identity`'s expected shape depends on `ref_type` (Issue #2288 P1-1):
    e.g. `external_primary_source` must be a URL; a bare non-URL token is rejected."""
    candidate = _base_candidate()
    evidence_ref = candidate["finding_contract"]["evaluations"][0]["evidence_refs"][0]
    evidence_ref["ref_type"] = "external_primary_source"
    evidence_ref["source_id"] = "web"
    evidence_ref["resource_identity"] = "not-a-url"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)

    evidence_ref["resource_identity"] = "https://example.com/some/primary/source"
    vrs.validate_candidate(candidate)  # no error
