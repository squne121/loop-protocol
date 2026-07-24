"""Static JSON Schema coverage for ISSUE_EXECUTION_DECISION_V1.

Cross-field semantics (ordering, graph cycle, predecessor agreement, state and
completeness) are owned by Issue #1677's normative semantic validator. This
file only verifies the closed static contract published by Issue #1675.
"""

import copy
import json
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).parent.parent
SCHEMA_PATH = SKILL_ROOT / "schemas" / "issue_execution_decision_v1.schema.json"
SHA = "sha256:" + "a" * 64


def valid_fixture() -> dict:
    return {
        "schema_version": "ISSUE_EXECUTION_DECISION_V1",
        "identity": {
            "target_issue_number": 1675,
            "target_body_sha256": SHA,
            "generated_at": "2026-07-24T00:00:00Z",
            "collection_digest": SHA,
        },
        "nodes": [
            {"issue_number": 1675, "body_sha256": SHA},
            {"issue_number": 1677, "body_sha256": SHA},
        ],
        "relations": [
            {
                "source_issue_number": 1675,
                "target_issue_number": 1677,
                "relation_type": "coordinates",
                "evidence": ["issue:1675:scope"],
            }
        ],
        "execution": {
            "state": "selected",
            "target_issue_number": 1675,
            "predecessors": [],
            "defer_reason": None,
        },
        "downstream_policy": {
            "semantic_reclassification": "forbidden",
            "freshness_validation": "required",
            "stale_action": "rerun_issue_refinement",
        },
        "completeness": {
            "issues_complete": True,
            "dependencies_complete": True,
            "unresolved_references": [],
        },
    }


def validate(instance: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=instance, schema=schema)


def test_given_valid_fixture_when_validated_then_accepts():
    validate(valid_fixture())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.pop("identity"),
        lambda item: item.__setitem__("unexpected", True),
        lambda item: item["identity"].__setitem__("target_issue_number", "1675"),
        lambda item: item["identity"].__setitem__("collection_digest", "not-a-digest"),
        lambda item: item["execution"].__setitem__("state", "superseded"),
        lambda item: item["downstream_policy"].__setitem__("stale_action", "ignore"),
        lambda item: item["completeness"].__setitem__("issues_complete", "true"),
    ],
)
def test_given_invalid_schema_fixture_when_validated_then_rejects(mutation):
    fixture = copy.deepcopy(valid_fixture())
    mutation(fixture)
    with pytest.raises(Exception):
        validate(fixture)


@pytest.mark.parametrize(
    "relation",
    [
        {
            "source_issue_number": 1675,
            "target_issue_number": 1677,
            "relation_type": "absorbs",
            "evidence": ["legacy spelling"],
        },
        {
            "source_issue_number": 1675,
            "target_issue_number": 1677,
            "relation_type": "depends_on",
            "evidence": [],
        },
        {
            "source_issue_number": 1675,
            "target_issue_number": 1677,
            "relation": "depends_on",
            "evidence": ["legacy field"],
        },
    ],
)
def test_graph_invariants(relation):
    fixture = valid_fixture()
    fixture["relations"] = [relation]
    with pytest.raises(Exception):
        validate(fixture)


def test_given_normalized_execution_fields_when_validated_then_accepts():
    fixture = valid_fixture()
    fixture["execution"] = {
        "state": "blocked",
        "target_issue_number": 1675,
        "predecessors": [1677],
        "defer_reason": "predecessor remains open",
    }
    validate(fixture)


def test_provenance_and_closed_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == "ISSUE_EXECUTION_DECISION_V1"
    assert schema["properties"]["relations"]["items"]["$ref"] == "#/$defs/relation"
    assert schema["$defs"]["relation"]["properties"]["relation_type"]["enum"] == [
        "depends_on",
        "duplicate",
        "absorb",
        "supersedes",
        "coordinates",
    ]
    validate(valid_fixture())

