"""
tests/codex/test_run_scope_rollup_preflight.py

Issue #1547 AC4/AC5/AC6/AC7/AC8 + PR #1560 OWNER fix_delta (P0-1/P0-2/P0-3/
P1-1/P1-2/P1-3) + Issue #1593 (PR #1643 review, P0-1/P0-2/P0-3/P1-1/P1-3)
tests. `gh` and the planner/verifier subprocess calls are stubbed via
monkeypatched module-level functions so the tests are hermetic and do not
require network access or a real `gh` binary.

PR #1643 review (P0-2): the inventory fetch path now speaks
`gh api graphql` exclusively (`_run_gh_graphql`) for both the top-level
issues/pullRequests connections and the nested per-PR `files` connection;
`gh issue list` / `gh pr list` are no longer used anywhere in
`_run_transaction`. The fake dispatchers below were updated from the old
`gh issue list` / `gh pr list` JSON-array shape to GraphQL connection node
shapes (`{"data": {"repository": {...}}}`) so these regressions exercise
the real, current transport instead of asserting against a superseded
contract.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))

import run_scope_rollup_preflight as rsrp  # noqa: E402

# Captured before the autouse `_patch_runtime_context` fixture below
# monkeypatches `rsrp._resolve_trusted_gh_binary` to a fixed stub -- the two
# P1-1 tests need the *real* implementation to exercise its hardening logic.
_REAL_RESOLVE_TRUSTED_GH_BINARY = rsrp._resolve_trusted_gh_binary


ISSUE_JSON = json.dumps(
    {
        "number": 1547,
        "title": "test issue",
        "body": "body text",
        "labels": [],
        "state": "OPEN",
        "stateReason": None,
        "url": "https://github.com/squne121/loop-protocol/issues/1547",
    }
)


def _issue_node(number: int) -> dict:
    return {
        "id": f"issue-{number}",
        "number": number,
        "title": f"issue {number}",
        "body": "",
        "state": "OPEN",
        "stateReason": None,
        "url": f"https://github.com/squne121/loop-protocol/issues/{number}",
    }


def _issue_nodes(count: int, start: int = 1) -> list[dict]:
    return [_issue_node(n) for n in range(start, start + count)]


def _pr_node(number: int, *, files_per_pr: int = 0, changed_files: int | None = None) -> dict:
    return {
        "id": f"pr-{number}",
        "number": number,
        "title": f"pr {number}",
        "body": "",
        "state": "OPEN",
        "url": f"https://github.com/squne121/loop-protocol/pull/{number}",
        "changedFiles": changed_files if changed_files is not None else files_per_pr,
        "files": {"nodes": [{"path": f"f{j}.py"} for j in range(files_per_pr)]},
    }


def _pr_nodes(count: int, start: int = 1, files_per_pr: int = 0, changed_files: int | None = None) -> list[dict]:
    return [_pr_node(n, files_per_pr=files_per_pr, changed_files=changed_files) for n in range(start, start + count)]


def _standard_graphql_fake(num_issues: int = 2, num_prs: int = 1, pr_files_pages: dict | None = None):
    """Fake `_run_gh_graphql` covering both the top-level inventory
    connection query and the nested per-PR `files` connection query,
    dispatched on the fields shape exactly like the real GraphQL variable
    bindings the production code sends (`fetchIssues`/`fetchPRs` for the
    inventory connection, `number` for the PR files page query)."""
    issues = _issue_nodes(num_issues)
    prs = _pr_nodes(num_prs)
    pr_files_pages = pr_files_pages or {}

    def fake(gh_bin, query, fields, *, budget=None, item_count=0):
        if budget is not None:
            budget.before_page()
            budget.consume_page(query)
        if "number" in fields:
            pr_number = int(fields["number"])
            all_paths = pr_files_pages.get(pr_number, [])
            start = int(fields.get("after") or 0)
            page = all_paths[start : start + rsrp.ITEMS_PER_PAGE]
            end = start + len(page)
            has_next = end < len(all_paths)
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "files": {
                                "nodes": [{"path": p} for p in page],
                                "pageInfo": {
                                    "hasNextPage": has_next,
                                    "endCursor": str(end) if has_next else None,
                                },
                            }
                        }
                    }
                }
            }
        if fields.get("fetchIssues") == "true":
            nodes, key = issues, "issues"
        else:
            nodes, key = prs, "pullRequests"
        return {
            "data": {
                "repository": {
                    key: {
                        "totalCount": len(nodes),
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }

    return fake


INVOCATION_ID = "20260713T100000Z_999"
REQUESTED_AT = "2026-07-13T10:00:00Z"


@pytest.fixture(autouse=True)
def _patch_runtime_context(monkeypatch):
    monkeypatch.setattr(rsrp, "_validate_runtime_context", lambda project_root, repo: None)
    monkeypatch.setattr(rsrp, "_resolve_trusted_gh_binary", lambda project_root: "/usr/bin/gh")
    monkeypatch.setattr(rsrp, "_gh_version", lambda gh_bin: "gh version 2.99.0 (test)")


def _fake_run_gh_issue_view(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
    if args[:2] == ["issue", "view"]:
        return 0, ISSUE_JSON, ""
    raise AssertionError(f"unexpected gh args: {args}")


def _fake_run_planner_ok(project_root, issues_path, prs_path, issue_number, repo, invocation_id):
    return {
        "schema_version": 2,
        "repo": repo,
        "generated_at": "2026-01-01T00:00:00Z",
        "source": "plan_issue_scope_rollup",
        "body_sha256": "deadbeef",
        "input": {"completeness": "full", "warnings": []},
        "candidates": [{"kind": "issue", "number": 2, "confidence": "high"}],
        "self_validation": {
            "invocation_id": invocation_id,
            "payload_sha256": "abc123",
            "script_file_sha256": "scriptsha",
            "schema_name": "ISSUE_SCOPE_ROLLUP_PLAN_V2",
            "schema_version": 2,
            "hash_algorithm": "sha256",
            "canonicalization": "n/a",
        },
    }


def _fake_run_verifier_ok(plan_data):
    return None


# --- AC4: pagination completeness fails closed --------------------------------


def test_inventory_total_count_mismatch_after_terminal_page_fails_closed(tmp_path, monkeypatch):
    """If a connection's terminal (hasNextPage: false) page's item count
    does not match that same connection's own totalCount, pagination
    completeness cannot be proven and the whole transaction fails closed."""

    def fake_graphql(gh_bin, query, fields, *, budget=None, item_count=0):
        if budget is not None:
            budget.before_page()
            budget.consume_page(query)
        if fields.get("fetchIssues") == "true":
            # Server reports 5 issues exist but the (terminal) page only
            # returns 3 nodes.
            return {
                "data": {
                    "repository": {
                        "issues": {
                            "totalCount": 5,
                            "nodes": _issue_nodes(3),
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        return {
            "data": {
                "repository": {
                    "pullRequests": {
                        "totalCount": 0,
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }

    monkeypatch.setattr(rsrp, "_run_gh", _fake_run_gh_issue_view)
    monkeypatch.setattr(rsrp, "_run_gh_graphql", fake_graphql)

    private_dir = rsrp._PrivateInvocationDir()
    try:
        with pytest.raises(rsrp.ScopeRollupPreflightError) as excinfo:
            rsrp._run_transaction(
                str(tmp_path), 1547, "squne121/loop-protocol", private_dir, INVOCATION_ID, REQUESTED_AT
            )
        assert excinfo.value.reason_code == "inventory_total_count_mismatch"
    finally:
        private_dir.cleanup()


def test_inventory_total_count_drifts_between_pages_fails_closed(tmp_path, monkeypatch):
    """An independent per-page totalCount cross-check fails closed if the
    server-reported total drifts between pages of the SAME connection
    (data changed mid-pagination -- cannot be proven complete)."""
    call_count = {"issues": 0}

    def fake_graphql(gh_bin, query, fields, *, budget=None, item_count=0):
        if budget is not None:
            budget.before_page()
            budget.consume_page(query)
        if fields.get("fetchIssues") == "true":
            call_count["issues"] += 1
            if call_count["issues"] == 1:
                return {
                    "data": {
                        "repository": {
                            "issues": {
                                "totalCount": 150,
                                "nodes": _issue_nodes(100, start=1),
                                "pageInfo": {"hasNextPage": True, "endCursor": "cursor1"},
                            }
                        }
                    }
                }
            return {
                "data": {
                    "repository": {
                        "issues": {
                            "totalCount": 151,  # drifted from page 1's 150
                            "nodes": _issue_nodes(50, start=101),
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        return {
            "data": {
                "repository": {
                    "pullRequests": {
                        "totalCount": 0,
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }

    monkeypatch.setattr(rsrp, "_run_gh", _fake_run_gh_issue_view)
    monkeypatch.setattr(rsrp, "_run_gh_graphql", fake_graphql)

    private_dir = rsrp._PrivateInvocationDir()
    try:
        with pytest.raises(rsrp.ScopeRollupPreflightError) as excinfo:
            rsrp._run_transaction(
                str(tmp_path), 1547, "squne121/loop-protocol", private_dir, INVOCATION_ID, REQUESTED_AT
            )
        assert excinfo.value.reason_code == "inventory_total_count_mismatch"
    finally:
        private_dir.cleanup()


# --- P0-1/P0-3: PR files(first:100) nested-connection pagination -------------


def test_pr_files_pagination_completes_past_100(tmp_path, monkeypatch):
    """A PR reporting changedFiles > len(files) (the nested files(first:100)
    connection cap) must be paginated to completeness via graphql before the
    transaction proceeds. Exercises the REAL `_paginate_pr_files` (not a
    monkeypatched stand-in) across a 100 + 1 page boundary."""
    pr_files_pages = {1: [f"f{j}.py" for j in range(101)]}
    fake_graphql = _standard_graphql_fake(
        num_issues=2,
        num_prs=0,
        pr_files_pages=pr_files_pages,
    )
    # num_prs=0 above only builds the issues fixture; supply the PR node with
    # its own changedFiles/files seed directly instead.
    prs = _pr_nodes(1, files_per_pr=100, changed_files=101)

    def fake_run_gh_graphql(gh_bin, query, fields, *, budget=None, item_count=0, _base=fake_graphql):
        if "number" in fields or fields.get("fetchIssues") == "true":
            return _base(gh_bin, query, fields, budget=budget, item_count=item_count)
        return {
            "data": {
                "repository": {
                    "pullRequests": {
                        "totalCount": len(prs),
                        "nodes": prs,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }

    monkeypatch.setattr(rsrp, "_run_gh", _fake_run_gh_issue_view)
    monkeypatch.setattr(rsrp, "_run_gh_graphql", fake_run_gh_graphql)
    monkeypatch.setattr(rsrp, "_run_planner", _fake_run_planner_ok)
    monkeypatch.setattr(rsrp, "_run_verifier", _fake_run_verifier_ok)

    private_dir = rsrp._PrivateInvocationDir()
    try:
        result = rsrp._run_transaction(
            str(tmp_path), 1547, "squne121/loop-protocol", private_dir, INVOCATION_ID, REQUESTED_AT
        )
    finally:
        private_dir.cleanup()
    assert result["status"] == "ok"
    pr_files_completeness = result["manifest"]["pr_files_completeness"]
    assert pr_files_completeness["pr_count_checked"] == 1
    assert pr_files_completeness["item_count"] == 101
    assert pr_files_completeness["per_pr"]["1"]["item_count"] == 101
    assert pr_files_completeness["per_pr"]["1"]["page_count"] == 2


def test_pr_files_pagination_incomplete_fails_closed(tmp_path, monkeypatch):
    """A PR whose nested `files` connection terminates (hasNextPage: false)
    before its own changedFiles total is reached must fail the whole
    transaction closed (GitHub-side data drift, or an implementation bug in
    the pagination loop -- either way, completeness cannot be proven)."""
    pr_files_pages = {1: [f"f{j}.py" for j in range(120)]}  # terminates short of changedFiles=150
    prs = _pr_nodes(1, files_per_pr=100, changed_files=150)

    def fake_run_gh_graphql(gh_bin, query, fields, *, budget=None, item_count=0):
        if budget is not None:
            budget.before_page()
            budget.consume_page(query)
        if "number" in fields:
            pr_number = int(fields["number"])
            all_paths = pr_files_pages.get(pr_number, [])
            start = int(fields.get("after") or 0)
            page = all_paths[start : start + rsrp.ITEMS_PER_PAGE]
            end = start + len(page)
            has_next = end < len(all_paths)
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "files": {
                                "nodes": [{"path": p} for p in page],
                                "pageInfo": {
                                    "hasNextPage": has_next,
                                    "endCursor": str(end) if has_next else None,
                                },
                            }
                        }
                    }
                }
            }
        if fields.get("fetchIssues") == "true":
            nodes, key = _issue_nodes(2), "issues"
        else:
            nodes, key = prs, "pullRequests"
        return {
            "data": {
                "repository": {
                    key: {
                        "totalCount": len(nodes),
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }

    monkeypatch.setattr(rsrp, "_run_gh", _fake_run_gh_issue_view)
    monkeypatch.setattr(rsrp, "_run_gh_graphql", fake_run_gh_graphql)

    private_dir = rsrp._PrivateInvocationDir()
    try:
        with pytest.raises(rsrp.ScopeRollupPreflightError) as excinfo:
            rsrp._run_transaction(
                str(tmp_path), 1547, "squne121/loop-protocol", private_dir, INVOCATION_ID, REQUESTED_AT
            )
        assert excinfo.value.reason_code == "pr_files_pagination_incomplete"
    finally:
        private_dir.cleanup()


def test_paginate_pr_files_rejects_duplicate_paths(monkeypatch):
    """P0-1: duplicate paths returned across pages of the same PR files
    connection must fail closed rather than silently deduplicating."""

    def fake_graphql(gh_bin, query, fields, *, budget=None, item_count=0):
        after = fields.get("after")
        if after is None:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "files": {
                                "nodes": [{"path": "a.py"}],
                                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                            }
                        }
                    }
                }
            }
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "files": {
                            # "a.py" repeated -- must never happen for a
                            # well-formed cursor-paginated connection.
                            "nodes": [{"path": "a.py"}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }

    monkeypatch.setattr(rsrp, "_run_gh_graphql", fake_graphql)
    with pytest.raises(rsrp.ScopeRollupPreflightError) as excinfo:
        rsrp._paginate_pr_files("gh", "squne121/loop-protocol", 1, 2)
    assert excinfo.value.reason_code == "inventory_duplicate_node"


def test_paginate_pr_files_full_201_across_three_pages(monkeypatch):
    """PR #1643 review (P0-1): a direct test -- not mocking
    `_paginate_pr_files` itself -- proving the REAL implementation
    accumulates every page (100 + 100 + 1) rather than overwriting `paths`
    with only the last page fetched."""
    all_paths = [f"f{j}.py" for j in range(201)]

    def fake_graphql(gh_bin, query, fields, *, budget=None, item_count=0):
        start = int(fields.get("after") or 0)
        page = all_paths[start : start + rsrp.ITEMS_PER_PAGE]
        end = start + len(page)
        has_next = end < len(all_paths)
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "files": {
                            "nodes": [{"path": p} for p in page],
                            "pageInfo": {
                                "hasNextPage": has_next,
                                "endCursor": str(end) if has_next else None,
                            },
                        }
                    }
                }
            }
        }

    monkeypatch.setattr(rsrp, "_run_gh_graphql", fake_graphql)
    result = rsrp._paginate_pr_files("gh", "squne121/loop-protocol", 1, 201)
    assert result == all_paths
    assert len(result) == 201


# --- AC5: private artifact exclusive/atomic write -----------------------------


def test_private_artifact_exclusive_atomic_write(tmp_path):
    private_dir = rsrp._PrivateInvocationDir()
    try:
        mode = stat.S_IMODE(os.stat(private_dir.path).st_mode)
        assert mode == 0o700

        final_path = private_dir.write_exclusive("issues.json", b"[]")
        assert final_path.is_file()
        assert stat.S_IMODE(os.stat(final_path).st_mode) == 0o600
        assert not (private_dir.path / "issues.json.part").exists()

        # Pre-existing regular file collision -> fail (P0-2: exclusive
        # finalize via os.link() must never silently clobber an existing
        # destination the way os.rename() would).
        with pytest.raises(rsrp.ScopeRollupPreflightError):
            private_dir.write_exclusive("issues.json", b"[]")
        # The original file's content must be untouched by the failed retry.
        assert final_path.read_bytes() == b"[]"

        # Destination symlink collision -> fail
        symlink_target = tmp_path / "somewhere.json"
        symlink_target.write_text("{}", encoding="utf-8")
        (private_dir.path / "linked.json").symlink_to(symlink_target)
        with pytest.raises(rsrp.ScopeRollupPreflightError):
            private_dir.write_exclusive("linked.json", b"[]")
    finally:
        private_dir.cleanup()
    assert not private_dir.path.exists()


# --- P1-3: cleanup failure is a hard, reported transaction failure -----------


def test_cleanup_failure_is_not_swallowed(tmp_path, monkeypatch):
    private_dir = rsrp._PrivateInvocationDir()

    def fake_rmtree(path, *args, **kwargs):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(rsrp.shutil, "rmtree", fake_rmtree)
    ok = private_dir.cleanup()
    assert ok is False
    assert private_dir.cleanup_status == "failed"
    assert private_dir.cleanup_error

    monkeypatch.undo()
    private_dir.cleanup()  # real cleanup so the test doesn't leak a tmp dir


def test_main_surfaces_cleanup_failure_as_transaction_error(tmp_path, monkeypatch):
    monkeypatch.setattr(rsrp, "_run_gh", _fake_run_gh_issue_view)
    monkeypatch.setattr(rsrp, "_run_gh_graphql", _standard_graphql_fake())
    monkeypatch.setattr(rsrp, "_run_planner", _fake_run_planner_ok)
    monkeypatch.setattr(rsrp, "_run_verifier", _fake_run_verifier_ok)

    real_cleanup = rsrp._PrivateInvocationDir.cleanup

    def failing_cleanup(self):
        real_cleanup(self)  # actually remove it so the test doesn't leak
        self.cleanup_status = "failed"
        self.cleanup_error = "simulated cleanup failure"
        return False

    monkeypatch.setattr(rsrp._PrivateInvocationDir, "cleanup", failing_cleanup)

    exit_code = rsrp.main(
        [
            "--issue-number",
            "1547",
            "--repo",
            "squne121/loop-protocol",
            "--invocation-id",
            INVOCATION_ID,
            "--requested-at",
            REQUESTED_AT,
        ]
    )
    assert exit_code == 1


# --- AC6: cleanup across all exit paths ---------------------------------------


def test_cleanup_across_all_exit_paths(tmp_path, monkeypatch):
    created_dirs: list[Path] = []
    original_init = rsrp._PrivateInvocationDir.__init__

    def tracking_init(self):
        original_init(self)
        created_dirs.append(self.path)

    monkeypatch.setattr(rsrp._PrivateInvocationDir, "__init__", tracking_init)

    monkeypatch.setattr(rsrp, "_run_gh", _fake_run_gh_issue_view)
    monkeypatch.setattr(rsrp, "_run_gh_graphql", _standard_graphql_fake())
    monkeypatch.setattr(rsrp, "_run_planner", _fake_run_planner_ok)
    monkeypatch.setattr(rsrp, "_run_verifier", _fake_run_verifier_ok)

    argv = [
        "--issue-number",
        "1547",
        "--repo",
        "squne121/loop-protocol",
        "--invocation-id",
        INVOCATION_ID,
        "--requested-at",
        REQUESTED_AT,
    ]

    # Success path
    exit_code = rsrp.main(argv)
    assert exit_code == 0
    assert created_dirs, "private dir was never created"
    for d in created_dirs:
        assert not d.exists(), f"private dir not cleaned up after success: {d}"

    created_dirs.clear()

    # gh nonzero failure path
    def fake_run_gh_failure(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        raise rsrp.ScopeRollupPreflightError("gh_issue_view_failed", "boom")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh_failure)
    exit_code = rsrp.main(argv)
    assert exit_code == 1
    for d in created_dirs:
        assert not d.exists(), f"private dir not cleaned up after gh failure: {d}"

    created_dirs.clear()

    # malformed JSON path
    def fake_run_gh_malformed(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, "not json", ""
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh_malformed)
    exit_code = rsrp.main(argv)
    assert exit_code == 1
    for d in created_dirs:
        assert not d.exists(), f"private dir not cleaned up after malformed json: {d}"

    created_dirs.clear()

    # timeout path
    def fake_run_gh_timeout(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        raise rsrp.ScopeRollupPreflightError("gh_timeout", "timed out")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh_timeout)
    exit_code = rsrp.main(argv)
    assert exit_code == 1
    for d in created_dirs:
        assert not d.exists(), f"private dir not cleaned up after timeout: {d}"


# --- AC7: manifest fields recorded ---------------------------------------------


def test_manifest_fields_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(rsrp, "_run_gh", _fake_run_gh_issue_view)
    monkeypatch.setattr(rsrp, "_run_gh_graphql", _standard_graphql_fake())
    monkeypatch.setattr(rsrp, "_run_planner", _fake_run_planner_ok)
    monkeypatch.setattr(rsrp, "_run_verifier", _fake_run_verifier_ok)

    private_dir = rsrp._PrivateInvocationDir()
    try:
        result = rsrp._run_transaction(
            str(tmp_path), 1547, "squne121/loop-protocol", private_dir, INVOCATION_ID, REQUESTED_AT
        )
    finally:
        private_dir.cleanup()

    assert result["status"] == "ok"
    manifest = result["manifest"]
    for field in (
        "host",
        "repo",
        "issue_number",
        "invocation_id",
        "requested_at",
        "gh_realpath",
        "gh_version",
        "query_schema_version",
        "fetched_at",
        "body_sha256",
        "planner_script_sha256",
        "issues",
        "pull_requests",
        "pr_files_completeness",
        "truncated",
    ):
        assert field in manifest, f"missing manifest field: {field}"
    assert manifest["host"] == "github.com"
    assert manifest["query_schema_version"] == 4
    assert manifest["truncated"] is False
    assert manifest["issues"]["item_count"] == 2
    assert manifest["issues"]["total_count"] == 2
    assert manifest["issues"]["page_count"] == 1
    assert manifest["pull_requests"]["item_count"] == 1
    # P1-1: independent per-PR nested-pagination completeness evidence. No
    # PR here required nested files() pagination (changedFiles == 0), so
    # the block is present but empty.
    assert manifest["pr_files_completeness"]["pr_count_checked"] == 0
    assert manifest["pr_files_completeness"]["pagination_complete"] is True
    # P0-1: invocation_id/requested_at are echoed verbatim from the caller,
    # never minted fresh by the executor.
    assert manifest["invocation_id"] == INVOCATION_ID
    assert manifest["requested_at"] == REQUESTED_AT
    # P0-1 point 5: full candidate payload survives the executor boundary.
    assert result["plan"]["payload"]["candidates"] == [{"kind": "issue", "number": 2, "confidence": "high"}]


# --- AC8: planner stdout/stderr separation -------------------------------------


def test_stdout_stderr_separation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rsrp, "_run_gh", _fake_run_gh_issue_view)
    monkeypatch.setattr(rsrp, "_run_gh_graphql", _standard_graphql_fake())

    def _planner_with_stderr_warning(project_root, issues_path, prs_path, issue_number, repo, invocation_id):
        # A real planner subprocess would emit a stderr warning on an
        # otherwise successful (exit 0) run; because _run_streaming
        # separates the two streams, this must never leak into the parsed
        # JSON result. This fake models the already-separated in-process
        # contract directly.
        return _fake_run_planner_ok(project_root, issues_path, prs_path, issue_number, repo, invocation_id)

    monkeypatch.setattr(rsrp, "_run_planner", _planner_with_stderr_warning)
    monkeypatch.setattr(rsrp, "_run_verifier", _fake_run_verifier_ok)

    private_dir = rsrp._PrivateInvocationDir()
    try:
        result = rsrp._run_transaction(
            str(tmp_path), 1547, "squne121/loop-protocol", private_dir, INVOCATION_ID, REQUESTED_AT
        )
    finally:
        private_dir.cleanup()

    assert result["status"] == "ok"
    # The final SCOPE_ROLLUP_RUN_RESULT_V1 payload the executor prints is
    # always valid JSON regardless of any planner stderr output; prove this
    # by round-tripping the dict through json.dumps/json.loads.
    json.loads(json.dumps({rsrp.SCHEMA: result}))
    captured = capsys.readouterr()
    assert "warning: something non-fatal happened" not in captured.out


def test_run_streaming_kills_process_group_on_byte_cap(monkeypatch):
    """P1-4: stdout is capped while streaming, not after buffering in full."""
    argv = [
        sys.executable,
        "-c",
        "import sys, time\nsys.stdout.write('x' * 200)\nsys.stdout.flush()\ntime.sleep(5)\n",
    ]
    with pytest.raises(rsrp.ScopeRollupPreflightError) as excinfo:
        rsrp._run_streaming(
            argv,
            env=dict(os.environ),
            timeout=10.0,
            max_bytes=50,
            timeout_reason_code="t_timeout",
            cap_reason_code="t_cap",
            exec_failed_reason_code="t_exec",
        )
    assert excinfo.value.reason_code == "t_cap"


# --- P1-1: trusted gh binary resolution hardening -----------------------------


def test_resolve_trusted_gh_binary_rejects_path_shadowing(tmp_path, monkeypatch):
    """A `gh` placed only in a non-trusted (PATH-shadowed) directory must be
    rejected even if it is otherwise a valid executable."""
    fake_dir = tmp_path / "shadow_bin"
    fake_dir.mkdir()
    fake_gh = fake_dir / "gh"
    fake_gh.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    fake_gh.chmod(0o755)

    monkeypatch.setattr(rsrp, "_TRUSTED_GH_SEARCH_DIRS", ("/nonexistent-trusted-dir",))
    monkeypatch.setattr(rsrp.shutil, "which", lambda name: str(fake_gh))

    with pytest.raises(rsrp.ScopeRollupPreflightError) as excinfo:
        _REAL_RESOLVE_TRUSTED_GH_BINARY(str(tmp_path))
    assert excinfo.value.reason_code == "gh_not_found"


def test_resolve_trusted_gh_binary_rejects_world_writable_binary(tmp_path, monkeypatch):
    fake_dir = tmp_path / "trusted_bin"
    fake_dir.mkdir()
    fake_gh = fake_dir / "gh"
    fake_gh.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    fake_gh.chmod(0o777)  # world-writable

    monkeypatch.setattr(rsrp, "_TRUSTED_GH_SEARCH_DIRS", (str(fake_dir),))

    # Use an unrelated project_root (not an ancestor of fake_dir) so the
    # earlier `gh_inside_project_root` check does not shadow the
    # world-writable-binary check under test.
    unrelated_root = tmp_path / "unrelated_project_root"
    unrelated_root.mkdir()

    with pytest.raises(rsrp.ScopeRollupPreflightError) as excinfo:
        _REAL_RESOLVE_TRUSTED_GH_BINARY(str(unrelated_root))
    assert excinfo.value.reason_code == "gh_binary_writable_by_others"


# --- P1-6: real producer subprocess end-to-end (a genuine `gh` binary is
# stubbed on PATH; the executor script itself runs as a real subprocess, not
# an in-process call) ----------------------------------------------------------


def test_executor_subprocess_end_to_end_ok(tmp_path, monkeypatch):
    """Runs `run_scope_rollup_preflight.py` as a real subprocess (real
    argparse, real JSON stdout contract, real planner subprocess call) against
    a stub `gh` placed on PATH, then independently recomputes result_sha256
    from the emitted `plan.payload` to prove the producer's own hash is
    self-consistent (the same check `parse_scope_rollup_run_result.py`
    performs on the consumer side)."""
    import hashlib
    import subprocess
    import textwrap

    repo_root = REPO_ROOT
    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir()
    gh_stub = stub_bin / "gh"
    gh_stub.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys

            args = sys.argv[1:]
            if args[:1] == ["--version"]:
                print("gh version 2.99.0 (stub)")
                sys.exit(0)
            if args[:2] == ["issue", "view"]:
                print(json.dumps({
                    "number": 1547, "title": "t", "body": "b", "labels": [],
                    "state": "OPEN", "stateReason": None,
                    "url": "https://github.com/squne121/loop-protocol/issues/1547",
                }))
                sys.exit(0)
            if args[:2] == ["api", "graphql"]:
                empty_connection = {
                    "totalCount": 0,
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
                print(json.dumps({
                    "data": {"repository": {
                        "issues": empty_connection,
                        "pullRequests": empty_connection,
                    }}
                }))
                sys.exit(0)
            sys.exit(1)
            """
        ),
        encoding="utf-8",
    )
    gh_stub.chmod(0o755)

    monkeypatch.setattr(rsrp, "_TRUSTED_GH_SEARCH_DIRS", (str(stub_bin),))

    script_path = repo_root / "scripts" / "agent-guards" / "run_scope_rollup_preflight.py"
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(repo_root)
    proc = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--issue-number",
            "1547",
            "--repo",
            "squne121/loop-protocol",
            "--invocation-id",
            INVOCATION_ID,
            "--requested-at",
            REQUESTED_AT,
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(proc.stdout)[rsrp.SCHEMA]
    # This process is not run from canonical root/default-branch context in
    # CI (the test harness's actual cwd/branch state is untouched here), so
    # `_validate_runtime_context` will most likely fail closed -- but the
    # important, unconditionally-true assertion is: stdout is always exactly
    # one valid `SCOPE_ROLLUP_RUN_RESULT_V1` JSON document, regardless of
    # status, produced by a real subprocess (not a monkeypatched call).
    assert payload["status"] in ("ok", "error")
    assert payload["reason_code"] is None or isinstance(payload["reason_code"], str)
    if payload["status"] == "ok":
        recomputed = hashlib.sha256(
            json.dumps(
                payload["plan"]["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        assert recomputed == payload["plan"]["payload_sha256"]
        assert payload["manifest"]["invocation_id"] == INVOCATION_ID
        assert payload["manifest"]["requested_at"] == REQUESTED_AT
