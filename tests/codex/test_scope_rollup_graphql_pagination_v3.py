"""#1593: GraphQL inventory completeness and consumer contract regressions."""

from __future__ import annotations

import json
import re
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
    }
    if kind == "issue":
        base["stateReason"] = None
    else:
        base.update(
            {
                "changedFiles": 0,
                "files": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []},
            }
        )
    return base


def _paged_transport(inventory: dict[str, list[dict]]):
    """A selection-aware GraphQL double.

    The response shape is derived from the query argument, not from the
    production query constant.  A production query that drops a required
    selection consequently receives a correspondingly incomplete response
    and must fail closed in the real fetch/normalization path.
    """
    def selection(query: str, kind: str) -> tuple[str, str, str]:
        start = query.index("issues:") if kind == "issue" else query.index("pullRequests:")
        end = query.index("pullRequests:") if kind == "issue" else len(query)
        connection = query[start:end]
        before_nodes, _, after_nodes = connection.partition("nodes")
        file_selection = after_nodes.partition("files(")[2]
        return before_nodes, after_nodes, file_selection

    def fake(gh_bin, query, fields, *, budget=None, item_count=0):
        kind = "issue" if fields["fetchIssues"] == "true" else "pr"
        connection_selection, node_selection, file_selection = selection(query, kind)
        page = int(fields.get("after", "c0")[1:])
        nodes = inventory[kind][page * rsrp.ITEMS_PER_PAGE : (page + 1) * rsrp.ITEMS_PER_PAGE]
        has_next = (page + 1) * rsrp.ITEMS_PER_PAGE < len(inventory[kind])
        if budget is not None:
            budget.before_page()
        projected_nodes = []
        for source in nodes:
            node = dict(source)
            for field in ("id", "number", "title", "body", "state", "url"):
                if not re.search(rf"\b{re.escape(field)}\b", node_selection):
                    node.pop(field, None)
            if kind == "issue":
                if "stateReason" not in node_selection:
                    node.pop("stateReason", None)
            else:
                if "changedFiles" not in node_selection:
                    node.pop("changedFiles", None)
                if "files(" not in node_selection:
                    node.pop("files", None)
                elif isinstance(node.get("files"), dict):
                    files = dict(node["files"])
                    if "pageInfo" not in file_selection:
                        files.pop("pageInfo", None)
                    elif isinstance(files.get("pageInfo"), dict):
                        page_info = dict(files["pageInfo"])
                        if "hasNextPage" not in file_selection:
                            page_info.pop("hasNextPage", None)
                        if "endCursor" not in file_selection:
                            page_info.pop("endCursor", None)
                        files["pageInfo"] = page_info
                    if not re.search(r"nodes\s*\{\s*path\b", file_selection):
                        files["nodes"] = [{} for _ in files.get("nodes", [])]
                    node["files"] = files
            projected_nodes.append(node)

        response_connection = {}
        if "totalCount" in connection_selection:
            response_connection["totalCount"] = len(inventory[kind])
        if "pageInfo" in connection_selection:
            page_info = {}
            if "hasNextPage" in connection_selection:
                page_info["hasNextPage"] = has_next
            if "endCursor" in connection_selection:
                page_info["endCursor"] = f"c{page + 1}" if has_next else None
            response_connection["pageInfo"] = page_info
        if node_selection:
            response_connection["nodes"] = projected_nodes
        response = {
            "data": {
                "repository": {
                    "issues" if kind == "issue" else "pullRequests": response_connection
                }
            }
        }
        if budget is not None:
            budget.consume_page(json.dumps(response), item_count=len(projected_nodes))
        return {
            **response
        }

    return fake


def test_inventory_query_omits_unused_connections():
    """GIVEN schema v4 WHEN inventory query is built THEN unused connections are absent."""
    assert "labels(" not in rsrp._INVENTORY_CONNECTION_QUERY
    assert "closingIssuesReferences" not in rsrp._INVENTORY_CONNECTION_QUERY


def test_inventory_query_selection_set():
    """GIVEN schema v4 WHEN inventory query is built THEN required fields remain explicit."""
    query = rsrp._INVENTORY_CONNECTION_QUERY
    assert "nodes { id number title body state stateReason url }" in query
    assert "id number title body state url changedFiles" in query
    assert "files(first: 100) { pageInfo { hasNextPage endCursor } nodes { path } }" in query
    assert "labels" not in query
    assert "closingIssuesReferences" not in query


@pytest.mark.parametrize(
    ("missing_selection", "kind", "requires_second_page"),
    [
        ("totalCount", "issue", False),
        ("hasNextPage", "issue", False),
        ("endCursor", "issue", True),
        ("id ", "issue", False),
        ("number ", "issue", False),
        ("stateReason", "issue", False),
        ("changedFiles", "pr", False),
        ("files.nodes.path", "pr", False),
        ("files.pageInfo", "pr", False),
    ],
)
def test_selection_aware_transport_rejects_queries_missing_required_fields(
    monkeypatch, missing_selection, kind, requires_second_page
):
    """AC4: each required selection omission must fail in real execution."""
    item_count = rsrp.ITEMS_PER_PAGE + 1 if requires_second_page else 1
    inventory = {
        "issue": [_node("issue", index) for index in range(1, item_count + 1)],
        "pr": [_node("pr", index) for index in range(1, item_count + 1)],
    }
    inventory["pr"][0]["changedFiles"] = 1
    inventory["pr"][0]["files"] = {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [{"path": "required.txt"}],
    }
    query = rsrp._INVENTORY_CONNECTION_QUERY
    if missing_selection == "files.nodes.path":
        query = query.replace("nodes { path }", "nodes { }")
    elif missing_selection == "files.pageInfo":
        query = query.replace(
            "files(first: 100) { pageInfo { hasNextPage endCursor } nodes { path } }",
            "files(first: 100) { nodes { path } }",
        )
    else:
        query = query.replace(missing_selection, "")
    monkeypatch.setattr(rsrp, "_INVENTORY_CONNECTION_QUERY", query)
    monkeypatch.setattr(rsrp, "_run_gh_graphql", _paged_transport(inventory))

    with pytest.raises(rsrp.ScopeRollupPreflightError):
        rsrp._fetch_inventory_connection(
            "gh", "squne121/loop-protocol", kind, rsrp._TransactionBudget.start()
        )


def test_inventory_normalization_omits_unused_connections():
    """GIVEN schema v4 DTOs WHEN normalized THEN planner input has no unused keys."""
    issue = rsrp._normalize_inventory_node("issue", _node("issue", 1))
    pull_request = rsrp._normalize_inventory_node("pr", _node("pr", 2))

    assert "labels" not in issue
    assert "labels" not in pull_request
    assert "closingIssuesReferences" not in pull_request


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
    manifest = result["manifest"]
    # P2: use the real transaction manifest as the runner marker input and
    # prove the schema-v4 alias/hash contract accepted by the parser.
    marker_inputs = {
        "current_issue_sha256": manifest["body_sha256"],
        "issues_all_sha256": manifest["issues"]["sha256"],
        "prs_all_sha256": manifest["pull_requests"]["sha256"],
        "issue_count": manifest["issues"]["item_count"],
        "pr_count": manifest["pull_requests"]["item_count"],
        "query_schema_version": manifest["query_schema_version"],
        "issues_completeness": manifest["issues"],
        "pull_requests_completeness": manifest["pull_requests"],
        "transaction_budget": manifest["budget"],
    }
    assert parser._has_valid_completeness_contract(marker_inputs) is True
    for kind, manifest_key in (("issue", "issues"), ("pr", "pull_requests")):
        normalized = [rsrp._normalize_inventory_node(kind, node) for node in inventory[kind]]
        canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert manifest[manifest_key]["sha256"] == rsrp.hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()


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
    """GIVEN a v3 marker WHEN completeness fields are absent THEN parser rejects it."""
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
        "inputs": {"query_schema_version": 3},
    }
    status, _cause, reason, _allowed = parser._validate_marker_payload(
        marker, output, sidecar, "squne121/loop-protocol", 1593, "v3", "a" * 64, "2026-07-19T10:00:00Z"
    )
    assert status == "rejected"
    assert reason == "inventory_completeness_contract_invalid"
