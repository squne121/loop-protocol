"""#1593: GraphQL inventory completeness and consumer contract regressions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))
sys.path.insert(0, str(REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "scripts"))

import run_scope_rollup_preflight as rsrp  # noqa: E402
import parse_scope_rollup_run_result as parser  # noqa: E402


def _node(kind: str, number: int) -> dict:
    base = {
        "id": f"{kind}-{number}",
        "number": number,
        "title": f"{kind} {number}",
        "body": "",
        "state": "OPEN",
        "url": f"https://example.invalid/{kind}/{number}",
        "labels": {"nodes": [{"name": "phase/implementation"}]},
    }
    if kind == "issue":
        base["stateReason"] = None
    else:
        base.update(
            {
                "changedFiles": 0,
                "files": {"nodes": []},
                "closingIssuesReferences": {"nodes": []},
            }
        )
    return base


def _paged_transport(inventory: dict[str, list[dict]]):
    def fake(gh_bin, query, fields, *, budget=None, item_count=0):
        kind = "issue" if fields["fetchIssues"] == "true" else "pr"
        page = int(fields.get("after", "c0")[1:])
        nodes = inventory[kind][page * rsrp.ITEMS_PER_PAGE : (page + 1) * rsrp.ITEMS_PER_PAGE]
        has_next = (page + 1) * rsrp.ITEMS_PER_PAGE < len(inventory[kind])
        if budget is not None:
            budget.before_page()
            budget.consume_page(json.dumps(nodes))
        return {
            "data": {
                "repository": {
                    "issues" if kind == "issue" else "pullRequests": {
                        "totalCount": len(inventory[kind]),
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": has_next, "endCursor": f"c{page + 1}" if has_next else None},
                    }
                }
            }
        }

    return fake


def test_inventory_over_500_pages_to_total_count_without_truncation(monkeypatch, tmp_path):
    """GIVEN 501 nodes per connection WHEN cursor pages terminate THEN planner gets all nodes."""
    inventory = {
        "issue": [_node("issue", number) for number in range(1, 502)],
        "pr": [_node("pr", number) for number in range(1, 502)],
    }
    monkeypatch.setattr(rsrp, "_run_gh_graphql", _paged_transport(inventory))
    monkeypatch.setattr(rsrp, "_resolve_trusted_gh_binary", lambda root: "/usr/bin/gh")
    monkeypatch.setattr(rsrp, "_gh_version", lambda _gh: "stub")
    monkeypatch.setattr(
        rsrp,
        "_fetch_issue_view",
        lambda *_args: ({"number": 1593, "title": "t", "state": "OPEN", "url": "https://example.invalid/1593"}, "{}"),
    )
    seen = {}

    def fake_planner(_root, issues_path, prs_path, *_args):
        seen["issues"] = len(json.loads(issues_path.read_text(encoding="utf-8")))
        seen["prs"] = len(json.loads(prs_path.read_text(encoding="utf-8")))
        return {
            "schema_version": 2,
            "input": {"completeness": "full"},
            "candidates": [],
            "self_validation": {"schema_name": "ISSUE_SCOPE_ROLLUP_PLAN_V2", "schema_version": 2, "payload_sha256": "x"},
        }

    monkeypatch.setattr(rsrp, "_run_planner", fake_planner)
    monkeypatch.setattr(rsrp, "_run_verifier", lambda _payload: None)
    private_dir = rsrp._PrivateInvocationDir()
    try:
        result = rsrp._run_transaction(str(tmp_path), 1593, "squne121/loop-protocol", private_dir, "v3", "2026-07-19T10:00:00Z")
    finally:
        private_dir.cleanup()
    assert seen == {"issues": 501, "prs": 501}
    assert result["manifest"]["issues"]["page_count"] == 6
    assert result["manifest"]["pull_requests"]["pagination_complete"] is True


def test_partial_graphql_error_or_stalled_cursor_fails_before_planner(monkeypatch):
    """GIVEN partial errors or a non-progressing cursor WHEN fetching THEN fail closed."""
    budget = rsrp._TransactionBudget.start()

    monkeypatch.setattr(
        rsrp,
        "_run_gh",
        lambda *_args, **_kwargs: (0, json.dumps({"errors": [{"message": "partial"}], "data": {"repository": {}}}), ""),
    )
    with pytest.raises(rsrp.ScopeRollupPreflightError, match="inventory_graphql_errors"):
        rsrp._fetch_inventory_connection("gh", "squne121/loop-protocol", "issue", budget)

    def stalled(_gh, _query, fields, **_kwargs):
        return {
            "data": {
                "repository": {
                    "issues": {
                        "totalCount": 2,
                        "nodes": [_node("issue", 1)],
                        "pageInfo": {"hasNextPage": True, "endCursor": fields.get("after", "c0")},
                    }
                }
            }
        }

    monkeypatch.setattr(rsrp, "_run_gh_graphql", stalled)
    with pytest.raises(rsrp.ScopeRollupPreflightError, match="inventory_cursor_stalled"):
        rsrp._fetch_inventory_connection("gh", "squne121/loop-protocol", "issue", rsrp._TransactionBudget.start())


def test_global_budget_counts_inventory_and_nested_pr_file_pages(monkeypatch):
    """GIVEN spent top-level pages WHEN nested files paginate THEN shared page cap applies."""
    monkeypatch.setattr(rsrp, "MAX_TRANSACTION_PAGES", 1)
    budget = rsrp._TransactionBudget.start()

    def files_page(_gh, _query, _fields, *, budget=None, item_count=0):
        assert budget is not None
        budget.before_page()
        budget.consume_page("{}")
        return {
            "data": {
                "repository": {
                    "pullRequest": {"files": {"nodes": [{"path": "a"}], "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}
                }
            }
        }

    monkeypatch.setattr(rsrp, "_run_gh_graphql", files_page)
    with pytest.raises(rsrp.ScopeRollupPreflightError, match="inventory_page_limit_exceeded"):
        rsrp._paginate_pr_files("gh", "squne121/loop-protocol", 1, [], budget=budget)


def test_marker_parser_rejects_manifest_missing_completeness_contract(tmp_path):
    """GIVEN a v2-like marker WHEN v3 completeness fields are absent THEN parser rejects it."""
    output = tmp_path / "assistant.md"
    output.write_text("x", encoding="utf-8")
    sidecar = tmp_path / "sidecar.yml"
    sidecar.write_text("SCOPE_ROLLUP_CAPTURE_RESULT_V1: {}\n", encoding="utf-8")
    marker = {
        "status": "ok",
        "repo": "squne121/loop-protocol",
        "current_issue": 1593,
        "invocation_id": "v3",
        "requested_at": "2026-07-19T10:00:00Z",
        "generated_at": "2026-07-19T10:00:01Z",
        "script_blob_sha256": "a" * 64,
        "inputs": {"query_schema_version": 2},
    }
    status, _cause, reason, _allowed = parser._validate_marker_payload(
        marker, output, sidecar, "squne121/loop-protocol", 1593, "v3", "a" * 64, "2026-07-19T10:00:00Z"
    )
    assert status == "rejected"
    assert reason == "inventory_completeness_contract_invalid"
