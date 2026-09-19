"""AC5/AC6: bounded GitHub closing-relation classifier regressions."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "skills" / "open-pr" / "scripts"))

import open_pr


def _snapshot(*, nodes: object = None, errors: object = None) -> dict:
    raw_nodes = [] if nodes is None else nodes
    if isinstance(raw_nodes, list):
        raw_nodes = [
            {**node, "repository": node.get("repository", {"nameWithOwner": "Squne121/Loop-Protocol"})}
            if isinstance(node, dict)
            else node
            for node in raw_nodes
        ]
    snapshot = {
        "data": {
            "repository": {
                "nameWithOwner": "Squne121/Loop-Protocol",
                "pullRequest": {
                    "number": 21,
                    "closingIssuesReferences": {"nodes": raw_nodes},
                },
            }
        }
    }
    if errors is not None:
        snapshot["errors"] = errors
    return snapshot


def test_given_matching_single_closing_issue_when_classified_then_only_validated_evidence_is_returned():
    disposition, reason, evidence = open_pr.classify_closing_issue_relation(_snapshot(nodes=[{"number": 20}]), 20)
    assert (disposition, reason) == ("matched", "MATCHED")
    assert evidence == {"repo": "squne121/loop-protocol", "issue_number": 20, "pr_number": 21}


def test_given_nonmatching_relation_shapes_when_classified_then_each_has_a_non_mutating_named_outcome():
    cases = (
        (_snapshot(nodes=[]), ("deferred", "NO_LINK")),
        (_snapshot(nodes=[{"number": 22}]), ("conflict", "RELATION_ISSUE_MISMATCH")),
        (_snapshot(nodes=[{"number": 20}, {"number": 21}]), ("conflict", "MULTIPLE_CLOSING_ISSUES")),
        (_snapshot(nodes=None), ("deferred", "NO_LINK")),
        (
            {
                "data": {
                    "repository": {
                        "nameWithOwner": "repo",
                        "pullRequest": {"number": 21, "closingIssuesReferences": None},
                    }
                }
            },
            ("deferred", "RELATION_UNAVAILABLE"),
        ),
        (_snapshot(nodes=[None]), ("deferred", "RELATION_UNAVAILABLE")),
    )
    for snapshot, expected in cases:
        disposition, reason, evidence = open_pr.classify_closing_issue_relation(snapshot, 20)
        assert (disposition, reason) == expected
        assert evidence is None


def test_given_same_issue_number_in_different_repository_when_classified_then_relation_is_non_mutating_mismatch():
    snapshot = _snapshot(nodes=[{"number": 20, "repository": {"nameWithOwner": "owner/other"}}])
    disposition, reason, evidence = open_pr.classify_closing_issue_relation(
        snapshot, 20, "squne121/loop-protocol"
    )
    assert (disposition, reason, evidence) == ("conflict", "RELATION_ISSUE_MISMATCH", None)


def test_given_top_level_graphql_errors_with_plausible_data_when_classified_then_relation_is_unavailable():
    snapshot = _snapshot(nodes=[{"number": 20}], errors=[{"message": "partial failure"}])
    disposition, reason, evidence = open_pr.classify_closing_issue_relation(snapshot, 20)
    assert (disposition, reason, evidence) == ("deferred", "RELATION_UNAVAILABLE", None)


def test_given_top_level_empty_graphql_errors_member_when_classified_then_relation_is_still_unavailable():
    disposition, reason, evidence = open_pr.classify_closing_issue_relation(
        _snapshot(nodes=[{"number": 20}], errors=[]), 20
    )
    assert (disposition, reason, evidence) == ("deferred", "RELATION_UNAVAILABLE", None)
