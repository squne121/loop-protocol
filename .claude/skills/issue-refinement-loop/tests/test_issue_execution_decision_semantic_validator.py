"""
Normative semantic validator tests for ISSUE_EXECUTION_DECISION_V1 (#1677 AC11).

validate_issue_execution_decision() (plan_refinement_loop.py) is the single
semantic-validation authority for cross-field graph invariants that the
closed JSON Schema (issue_execution_decision_v1.schema.json, #1675) cannot
express: ordering, uniqueness, endpoint existence, self-edges, conflicting
parallel edges, depends_on cycles, target/predecessor agreement, state
semantics, and completeness gating.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import plan_refinement_loop as prl  # noqa: E402


SHA = "sha256:" + "a" * 64


def _recompute_digest(decision: dict) -> str:
    """Mirror plan_refinement_loop's collection_digest formula (P0-3.3 scope)."""
    return prl._sha256_prefixed(
        prl._canonical_json(
            {
                "nodes": decision["nodes"],
                "relations": decision["relations"],
                "execution": decision["execution"],
                "completeness": decision["completeness"],
                "downstream_policy": decision["downstream_policy"],
            }
        )
    )


def _valid_decision() -> dict:
    decision = {
        "schema_version": "ISSUE_EXECUTION_DECISION_V1",
        "identity": {
            "target_issue_number": 1,
            "target_body_sha256": SHA,
            "generated_at": "2026-07-25T00:00:00Z",
            "collection_digest": SHA,
        },
        "nodes": [
            {"issue_number": 1, "body_sha256": SHA},
            {"issue_number": 2, "body_sha256": SHA},
        ],
        "relations": [
            {
                "source_issue_number": 1,
                "target_issue_number": 2,
                "relation_type": "depends_on",
                "evidence": ["issue:1:scope"],
            }
        ],
        "execution": {
            "state": "blocked",
            "target_issue_number": 1,
            "predecessors": [2],
            "defer_reason": "predecessor #2 is open",
        },
        "downstream_policy": dict(prl.ISSUE_EXECUTION_DECISION_DOWNSTREAM_POLICY),
        "completeness": {
            "issues_complete": True,
            "dependencies_complete": True,
            "unresolved_references": [],
        },
    }
    decision["identity"]["collection_digest"] = _recompute_digest(decision)
    return decision


def test_given_valid_decision_when_validated_then_no_violations():
    assert prl.validate_issue_execution_decision(_valid_decision()) == []


def test_given_selected_state_with_no_predecessors_then_valid():
    decision = _valid_decision()
    decision["relations"] = []
    decision["execution"] = {
        "state": "selected",
        "target_issue_number": 1,
        "predecessors": [],
        "defer_reason": None,
    }
    decision["identity"]["collection_digest"] = _recompute_digest(decision)
    assert prl.validate_issue_execution_decision(decision) == []


def test_self_edge_rejected():
    decision = _valid_decision()
    decision["relations"][0]["target_issue_number"] = 1  # source == target
    violations = prl.validate_issue_execution_decision(decision)
    assert any("self_edge" in v for v in violations)


def test_unknown_endpoint_rejected():
    decision = _valid_decision()
    decision["relations"][0]["target_issue_number"] = 999  # not in nodes
    violations = prl.validate_issue_execution_decision(decision)
    assert any("unknown_endpoint" in v for v in violations)


def test_duplicate_relation_rejected():
    decision = _valid_decision()
    decision["relations"] = decision["relations"] * 2
    violations = prl.validate_issue_execution_decision(decision)
    assert "duplicate_relation" in violations


def test_duplicate_node_rejected():
    decision = _valid_decision()
    decision["nodes"] = decision["nodes"] + [{"issue_number": 2, "body_sha256": SHA}]
    violations = prl.validate_issue_execution_decision(decision)
    assert "duplicate_node" in violations


def test_conflicting_parallel_edge_rejected():
    decision = _valid_decision()
    decision["relations"].append(
        {
            "source_issue_number": 1,
            "target_issue_number": 2,
            "relation_type": "duplicate",
            "evidence": ["conflict"],
        }
    )
    violations = prl.validate_issue_execution_decision(decision)
    assert any("conflicting_parallel_edge" in v for v in violations)


def test_three_node_depends_on_cycle_rejected():
    decision = _valid_decision()
    decision["nodes"] = [
        {"issue_number": 1, "body_sha256": SHA},
        {"issue_number": 2, "body_sha256": SHA},
        {"issue_number": 3, "body_sha256": SHA},
    ]
    decision["relations"] = [
        {"source_issue_number": 1, "target_issue_number": 2, "relation_type": "depends_on", "evidence": ["e"]},
        {"source_issue_number": 2, "target_issue_number": 3, "relation_type": "depends_on", "evidence": ["e"]},
        {"source_issue_number": 3, "target_issue_number": 1, "relation_type": "depends_on", "evidence": ["e"]},
    ]
    decision["execution"] = {
        "state": "blocked",
        "target_issue_number": 1,
        "predecessors": [2],
        "defer_reason": "cycle",
    }
    violations = prl.validate_issue_execution_decision(decision)
    assert "depends_on_cycle" in violations


def test_predecessors_mismatch_rejected():
    decision = _valid_decision()
    decision["execution"]["predecessors"] = [999]
    violations = prl.validate_issue_execution_decision(decision)
    assert "predecessors_do_not_match_depends_on_edges" in violations


def test_selected_state_with_predecessors_rejected():
    decision = _valid_decision()
    decision["execution"]["state"] = "selected"
    violations = prl.validate_issue_execution_decision(decision)
    assert "selected_state_with_predecessors" in violations


def test_selected_state_with_incomplete_evidence_rejected():
    decision = _valid_decision()
    decision["relations"] = []
    decision["execution"] = {
        "state": "selected",
        "target_issue_number": 1,
        "predecessors": [],
        "defer_reason": None,
    }
    decision["completeness"]["issues_complete"] = False
    violations = prl.validate_issue_execution_decision(decision)
    assert "selected_state_with_incomplete_evidence" in violations


def test_blocked_state_missing_defer_reason_rejected():
    decision = _valid_decision()
    decision["execution"]["defer_reason"] = None
    violations = prl.validate_issue_execution_decision(decision)
    assert "blocked_state_missing_defer_reason" in violations


def test_duplicate_state_without_duplicate_relation_rejected():
    decision = _valid_decision()
    decision["execution"]["state"] = "duplicate"
    decision["relations"][0]["relation_type"] = "coordinates"
    violations = prl.validate_issue_execution_decision(decision)
    assert "duplicate_state_without_duplicate_relation" in violations


def test_unknown_relation_type_rejected():
    decision = _valid_decision()
    decision["relations"][0]["relation_type"] = "absorbs"  # legacy misspelling
    violations = prl.validate_issue_execution_decision(decision)
    assert any(v.startswith("unknown_relation_type") for v in violations)


def test_unknown_execution_state_rejected():
    decision = _valid_decision()
    decision["execution"]["state"] = "superseded"
    violations = prl.validate_issue_execution_decision(decision)
    assert any(v.startswith("unknown_execution_state") for v in violations)


def test_nodes_not_sorted_rejected():
    decision = _valid_decision()
    decision["nodes"] = list(reversed(decision["nodes"]))
    violations = prl.validate_issue_execution_decision(decision)
    assert "nodes_not_sorted" in violations
