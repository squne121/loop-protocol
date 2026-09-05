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

import private_audit_resolver as par  # noqa: E402
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


# ---------------------------------------------------------------------------
# Issue #2375 AC4: latitude_runtime_evidence/v1 + its runtime_receipt/runtime evidence_ref
# binding must never carry raw trace/prompt/message/tool I/O/stdout/stderr/authorization/
# token/secret/absolute-path content.
# ---------------------------------------------------------------------------

_LATITUDE_COLLECTOR_VERSION = "latitude-collector/v1"
_LATITUDE_COLLECTED_AT = "2026-08-29T00:00:00Z"
_LATITUDE_METRICS = {"trace_count": 2, "span_count": 6, "duration_ms": 120}


def _latitude_available_instance():
    ref = vrs.compute_latitude_evidence_ref(
        _LATITUDE_COLLECTOR_VERSION, dict(_LATITUDE_METRICS), _LATITUDE_COLLECTED_AT
    )
    identity = vrs.compute_latitude_evidence_identity(_LATITUDE_COLLECTOR_VERSION, ref, dict(_LATITUDE_METRICS))
    return {
        "schema_version": "latitude_runtime_evidence/v1",
        "availability": "available",
        "collected_at": _LATITUDE_COLLECTED_AT,
        "collector_version": _LATITUDE_COLLECTOR_VERSION,
        "evidence_identity": identity,
        "evidence_ref": ref,
        "metrics": dict(_LATITUDE_METRICS),
        "reason_code": None,
    }


@pytest.mark.parametrize("raw_field", RAW_EVIDENCE_FIELD_NAMES)
def test_latitude_runtime_evidence_rejects_raw_evidence_field(raw_field):
    instance = _latitude_available_instance()
    instance[raw_field] = "untrusted raw content that must never be schema-allowed"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_latitude_runtime_evidence(instance)


def test_latitude_runtime_evidence_schema_has_no_raw_evidence_property():
    schema = vrs.load_latitude_runtime_evidence_schema()
    top_level_properties = set(schema["properties"].keys())
    for raw_field in RAW_EVIDENCE_FIELD_NAMES:
        assert raw_field not in top_level_properties


def test_latitude_bound_evidence_ref_uses_existing_runtime_receipt_shape_and_validates():
    """The latitude evidence_ref binding (Issue #2375 AC3) reuses the EXISTING
    `runtime_receipt`/`runtime` ref_type/source_id pair -- no new evidence_ref shape/schema
    change was required, and the result passes the SAME public-safety schema gate as every
    other evidence_ref (`additionalProperties: false` field allowlist)."""
    candidate = _base_candidate()
    latitude_evidence = _latitude_available_instance()
    candidate["finding_contract"]["evaluations"][0]["evidence_refs"].append(
        {
            "ref_type": "runtime_receipt",
            "source_id": "runtime",
            "resource_identity": latitude_evidence["evidence_ref"],
            "projection_digest": latitude_evidence["evidence_identity"],
        }
    )
    vrs.validate_candidate(candidate)  # no error


@pytest.mark.parametrize("raw_field", RAW_EVIDENCE_FIELD_NAMES)
def test_latitude_bound_evidence_ref_rejects_raw_evidence_field(raw_field):
    candidate = _base_candidate()
    latitude_evidence = _latitude_available_instance()
    runtime_evidence_ref = {
        "ref_type": "runtime_receipt",
        "source_id": "runtime",
        "resource_identity": latitude_evidence["evidence_ref"],
        "projection_digest": latitude_evidence["evidence_identity"],
        raw_field: "untrusted raw content that must never be schema-allowed",
    }
    candidate["finding_contract"]["evaluations"][0]["evidence_refs"].append(runtime_evidence_ref)
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_latitude_evidence_ref_and_identity_never_contain_absolute_path_shape():
    """evidence_ref/evidence_identity are sha256 digests derived from allowlisted metrics only
    (Binding Rules) -- they structurally cannot resemble a local absolute filesystem path."""
    instance = _latitude_available_instance()
    for value in (instance["evidence_ref"], instance["evidence_identity"]):
        assert not value.startswith("/")
        assert "/home/" not in value
        assert "/Users/" not in value


# ---------------------------------------------------------------------------
# Issue #2376 (#1939 Workstream 5) AC7: retro_private_audit_index/v1 -- the
# LOCAL-ONLY private-audit manifest must never carry a raw-evidence-shaped
# field (closed schema key set) and its object_key must never resemble a
# local absolute filesystem path.
# ---------------------------------------------------------------------------


def _private_audit_index_instance():
    return copy.deepcopy(vrs.load_fixture("retro_private_audit_index_v1.valid.json"))


def test_private_audit_index_schema_is_closed():
    schema = par.load_manifest_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["run_identity"]["additionalProperties"] is False
    assert schema["properties"]["evidence_ref"]["additionalProperties"] is False
    # no free-form instruction field anywhere in this schema (AC7).
    for forbidden in ("instruction", "instructions", "prompt", "command"):
        assert forbidden not in schema["properties"]


def test_private_audit_index_schema_has_no_raw_evidence_property():
    schema = par.load_manifest_schema()
    top_level_properties = set(schema["properties"].keys())
    for raw_field in RAW_EVIDENCE_FIELD_NAMES:
        assert raw_field not in top_level_properties


@pytest.mark.parametrize("raw_field", RAW_EVIDENCE_FIELD_NAMES)
def test_private_audit_index_with_raw_evidence_field_rejected(raw_field):
    instance = _private_audit_index_instance()
    instance[raw_field] = "untrusted raw content that must never be schema-allowed"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        par._validate_manifest_schema(instance)  # noqa: SLF001


def test_private_audit_index_well_formed_fixture_accepted():
    instance = _private_audit_index_instance()
    par._validate_manifest_schema(instance)  # no error  # noqa: SLF001


def test_private_audit_index_object_key_never_resembles_absolute_path():
    instance = _private_audit_index_instance()
    object_key = instance["object_key"]
    assert not object_key.startswith("/")
    assert "/home/" not in object_key
    assert "/Users/" not in object_key
    assert ".." not in object_key.split("/")

    instance["object_key"] = "/etc/passwd"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        par._validate_manifest_schema(instance)  # noqa: SLF001
