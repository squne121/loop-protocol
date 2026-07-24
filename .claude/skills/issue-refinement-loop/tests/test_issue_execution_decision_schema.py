"""Static contract tests for ISSUE_EXECUTION_DECISION_V1."""

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
            "repository_id": "R_1",
            "repository_full_name": "owner/repo",
            "target_issue_number": 1675,
            "body_sha256": SHA,
            "updated_at": "2026-07-23T00:00:00Z",
        },
        "source_manifest": {
            "collected_at": "2026-07-23T00:00:00Z",
            "issues_complete": True,
            "pull_requests_complete": True,
            "dependencies_complete": True,
            "unresolved_references": [],
            "issues_sha256": SHA,
            "pull_requests_sha256": SHA,
            "dependencies_sha256": SHA,
        },
        "graph": {
            "canonical_sort": "issue_number_ascending_then_relation",
            "nodes": [{"issue_number": 1675}, {"issue_number": 1677}],
            "edges": [
                {
                    "source_issue_number": 1677,
                    "target_issue_number": 1675,
                    "relation": "depends_on",
                    "evidence_refs": ["issue:1677"],
                }
            ],
        },
        "execution": {"target_state": "selected", "predecessor_issue_numbers": [], "reason_codes": ["owner_priority"]},
        "integrity": {"canonicalization_id": "rfc8785", "decision_inputs_sha256": SHA, "artifact_sha256": SHA},
        "provenance": {
            "producer_name": "issue-refinement-loop",
            "producer_version": "v1",
            "policy_version": "v1",
            "policy_sha256": SHA,
        },
        "consumer_compatibility": {
            "consumer_contract_version": "v1",
            "supported_schema_versions": ["ISSUE_EXECUTION_DECISION_V1"],
            "projection_compatibility": "dual_write",
        },
    }


def validate_graph_invariants(instance: dict) -> None:
    graph = instance["graph"]
    nodes = [node["issue_number"] for node in graph["nodes"]]
    assert nodes == sorted(nodes) and len(nodes) == len(set(nodes))
    edges = graph["edges"]
    edge_keys = []
    adjacency = {node: [] for node in nodes}
    for edge in edges:
        source, target, relation = edge["source_issue_number"], edge["target_issue_number"], edge["relation"]
        assert source in adjacency and target in adjacency and source != target
        edge_key = (source, target, relation)
        assert edge_key not in edge_keys
        assert (target, source, relation) not in edge_keys
        edge_keys.append(edge_key)
        if relation == "depends_on":
            adjacency[source].append(target)
    visiting, visited = set(), set()

    def visit(node: int) -> None:
        assert node not in visiting, "depends_on cycle is invalid"
        if node not in visited:
            visiting.add(node)
            for neighbor in adjacency[node]:
                visit(neighbor)
            visiting.remove(node)
            visited.add(node)

    for node in nodes:
        visit(node)


def validate(instance: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(instance=instance, schema=schema)
    validate_graph_invariants(instance)


def test_given_valid_fixture_when_validated_then_accepts():
    validate(valid_fixture())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.pop("identity"),
        lambda item: item.__setitem__("unexpected", True),
        lambda item: item["execution"].__setitem__("target_state", "blocked"),
        lambda item: item["identity"].__setitem__("target_issue_number", "1675"),
    ],
)
def test_given_invalid_schema_fixture_when_validated_then_rejects(mutation):
    fixture = copy.deepcopy(valid_fixture())
    mutation(fixture)
    with pytest.raises(Exception):
        validate(fixture)


@pytest.mark.parametrize(
    "edge",
    [
        {"source_issue_number": 1675, "target_issue_number": 1675, "relation": "depends_on", "evidence_refs": []},
        {"source_issue_number": 1675, "target_issue_number": 9999, "relation": "depends_on", "evidence_refs": []},
    ],
)
def test_graph_invariants(edge):
    fixture = valid_fixture()
    fixture["graph"]["edges"] = [edge]
    with pytest.raises(AssertionError):
        validate(fixture)


def test_given_unsorted_or_contradictory_graph_when_validated_then_rejects():
    fixture = valid_fixture()
    fixture["graph"]["nodes"] = [{"issue_number": 1677}, {"issue_number": 1675}]
    with pytest.raises(AssertionError):
        validate(fixture)

    fixture = valid_fixture()
    fixture["graph"]["edges"].append(
        {"source_issue_number": 1675, "target_issue_number": 1677, "relation": "depends_on", "evidence_refs": []}
    )
    with pytest.raises(AssertionError):
        validate(fixture)


def test_given_depends_on_cycle_when_validated_then_rejects():
    fixture = valid_fixture()
    fixture["graph"]["edges"].append(
        {"source_issue_number": 1675, "target_issue_number": 1677, "relation": "depends_on", "evidence_refs": []}
    )
    with pytest.raises(AssertionError):
        validate(fixture)


def test_provenance_and_closed_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == "ISSUE_EXECUTION_DECISION_V1"
    validate(valid_fixture())
