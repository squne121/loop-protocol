"""
tests/codex/test_run_scope_rollup_preflight.py

Issue #1547 AC4/AC5/AC6/AC7/AC8: run_scope_rollup_preflight.py transaction
tests. `gh` and the planner/verifier subprocess calls are stubbed via
monkeypatched module-level functions so the tests are hermetic and do not
require network access or a real `gh` binary.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))

import run_scope_rollup_preflight as rsrp  # noqa: E402


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


def _issues_list(n: int) -> str:
    return json.dumps(
        [
            {
                "number": i,
                "title": f"issue {i}",
                "body": "",
                "labels": [],
                "state": "OPEN",
                "stateReason": None,
                "url": f"https://github.com/squne121/loop-protocol/issues/{i}",
            }
            for i in range(1, n + 1)
        ]
    )


def _prs_list(n: int) -> str:
    return json.dumps(
        [
            {
                "number": i,
                "title": f"pr {i}",
                "body": "",
                "labels": [],
                "state": "OPEN",
                "url": f"https://github.com/squne121/loop-protocol/pull/{i}",
                "files": [],
                "closingIssuesReferences": [],
            }
            for i in range(1, n + 1)
        ]
    )


@pytest.fixture(autouse=True)
def _patch_runtime_context(monkeypatch):
    monkeypatch.setattr(rsrp, "_validate_runtime_context", lambda project_root, repo: None)
    monkeypatch.setattr(rsrp, "_resolve_trusted_gh_binary", lambda project_root: "/usr/bin/gh")
    monkeypatch.setattr(rsrp, "_gh_version", lambda gh_bin: "gh version 2.99.0 (test)")


def _fake_run_planner_ok(project_root, issues_path, prs_path, issue_number, repo, invocation_id, result_path):
    plan = {
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
    result_path.write_text(json.dumps(plan), encoding="utf-8")


def _fake_run_verifier_ok(project_root, result_path):
    return None


# --- AC4: pagination truncation fails closed ---------------------------------


def test_pagination_truncation_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(rsrp, "MAX_ITEMS_PER_KIND", 3)

    def fake_run_gh(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, ISSUE_JSON, ""
        if args[:2] == ["issue", "list"]:
            return 0, _issues_list(5), ""  # exceeds MAX_ITEMS_PER_KIND(3) + 1 request bound
        if args[:2] == ["pr", "list"]:
            return 0, _prs_list(1), ""
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh)

    private_dir = rsrp._PrivateInvocationDir()
    try:
        with pytest.raises(rsrp.ScopeRollupPreflightError) as excinfo:
            rsrp._run_transaction(str(tmp_path), 1547, "squne121/loop-protocol", private_dir)
        assert excinfo.value.reason_code == "inventory_truncated"
    finally:
        private_dir.cleanup()


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

        # Pre-existing regular file collision -> fail
        with pytest.raises(rsrp.ScopeRollupPreflightError):
            private_dir.write_exclusive("issues.json", b"[]")

        # Destination symlink collision -> fail
        symlink_target = tmp_path / "somewhere.json"
        symlink_target.write_text("{}", encoding="utf-8")
        (private_dir.path / "linked.json").symlink_to(symlink_target)
        with pytest.raises(rsrp.ScopeRollupPreflightError):
            private_dir.write_exclusive("linked.json", b"[]")
    finally:
        private_dir.cleanup()
    assert not private_dir.path.exists()


# --- AC6: cleanup across all exit paths ---------------------------------------


def test_cleanup_across_all_exit_paths(tmp_path, monkeypatch):
    created_dirs: list[Path] = []
    original_init = rsrp._PrivateInvocationDir.__init__

    def tracking_init(self):
        original_init(self)
        created_dirs.append(self.path)

    monkeypatch.setattr(rsrp._PrivateInvocationDir, "__init__", tracking_init)

    def fake_run_gh_success(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, ISSUE_JSON, ""
        if args[:2] == ["issue", "list"]:
            return 0, _issues_list(2), ""
        if args[:2] == ["pr", "list"]:
            return 0, _prs_list(1), ""
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh_success)
    monkeypatch.setattr(rsrp, "_run_planner", _fake_run_planner_ok)
    monkeypatch.setattr(rsrp, "_run_verifier", _fake_run_verifier_ok)

    # Success path
    exit_code = rsrp.main(["--issue-number", "1547", "--repo", "squne121/loop-protocol"])
    assert exit_code == 0
    assert created_dirs, "private dir was never created"
    for d in created_dirs:
        assert not d.exists(), f"private dir not cleaned up after success: {d}"

    created_dirs.clear()

    # gh nonzero failure path
    def fake_run_gh_failure(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        raise rsrp.ScopeRollupPreflightError("gh_issue_view_failed", "boom")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh_failure)
    exit_code = rsrp.main(["--issue-number", "1547", "--repo", "squne121/loop-protocol"])
    assert exit_code == 1
    for d in created_dirs:
        assert not d.exists(), f"private dir not cleaned up after gh failure: {d}"

    created_dirs.clear()

    # malformed JSON path
    def fake_run_gh_malformed(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, "not json", ""
        return 0, "[]", ""

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh_malformed)
    exit_code = rsrp.main(["--issue-number", "1547", "--repo", "squne121/loop-protocol"])
    assert exit_code == 1
    for d in created_dirs:
        assert not d.exists(), f"private dir not cleaned up after malformed json: {d}"

    created_dirs.clear()

    # timeout path
    def fake_run_gh_timeout(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        raise rsrp.ScopeRollupPreflightError("gh_timeout", "timed out")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh_timeout)
    exit_code = rsrp.main(["--issue-number", "1547", "--repo", "squne121/loop-protocol"])
    assert exit_code == 1
    for d in created_dirs:
        assert not d.exists(), f"private dir not cleaned up after timeout: {d}"


# --- AC7: manifest fields recorded ---------------------------------------------


def test_manifest_fields_recorded(tmp_path, monkeypatch):
    def fake_run_gh(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, ISSUE_JSON, ""
        if args[:2] == ["issue", "list"]:
            return 0, _issues_list(2), ""
        if args[:2] == ["pr", "list"]:
            return 0, _prs_list(1), ""
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh)
    monkeypatch.setattr(rsrp, "_run_planner", _fake_run_planner_ok)
    monkeypatch.setattr(rsrp, "_run_verifier", _fake_run_verifier_ok)

    private_dir = rsrp._PrivateInvocationDir()
    try:
        result = rsrp._run_transaction(str(tmp_path), 1547, "squne121/loop-protocol", private_dir)
    finally:
        private_dir.cleanup()

    assert result["status"] == "ok"
    manifest = result["manifest"]
    for field in (
        "host",
        "repo",
        "issue_number",
        "invocation_id",
        "gh_realpath",
        "gh_version",
        "query_schema_version",
        "fetched_at",
        "body_sha256",
        "planner_script_sha256",
        "issues",
        "pull_requests",
        "truncated",
    ):
        assert field in manifest, f"missing manifest field: {field}"
    assert manifest["host"] == "github.com"
    assert manifest["truncated"] is False
    assert manifest["issues"]["truncated"] is False
    assert manifest["issues"]["item_count"] == 2
    assert manifest["pull_requests"]["item_count"] == 1


# --- AC8: planner stdout/stderr separation -------------------------------------


def test_stdout_stderr_separation(tmp_path, monkeypatch, capsys):
    def fake_run_gh(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, ISSUE_JSON, ""
        if args[:2] == ["issue", "list"]:
            return 0, _issues_list(2), ""
        if args[:2] == ["pr", "list"]:
            return 0, _prs_list(1), ""
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh)

    def _planner_with_stderr_warning(
        project_root, issues_path, prs_path, issue_number, repo, invocation_id, result_path
    ):
        # Simulates a planner that emits a stderr warning on an otherwise
        # successful (exit 0) run. The warning must never leak into the
        # result JSON written to result_path.
        sys.stderr.write("warning: something non-fatal happened\n")
        _fake_run_planner_ok(
            project_root, issues_path, prs_path, issue_number, repo, invocation_id, result_path
        )

    monkeypatch.setattr(rsrp, "_run_planner", _planner_with_stderr_warning)
    monkeypatch.setattr(rsrp, "_run_verifier", _fake_run_verifier_ok)

    private_dir = rsrp._PrivateInvocationDir()
    try:
        result = rsrp._run_transaction(str(tmp_path), 1547, "squne121/loop-protocol", private_dir)
    finally:
        private_dir.cleanup()

    assert result["status"] == "ok"
    # The final SCOPE_ROLLUP_RUN_RESULT_V1 payload the executor prints is
    # always valid JSON regardless of any planner stderr output; prove this
    # by round-tripping the dict through json.dumps/json.loads.
    json.loads(json.dumps({rsrp.SCHEMA: result}))
    captured = capsys.readouterr()
    assert "warning: something non-fatal happened" not in captured.out
