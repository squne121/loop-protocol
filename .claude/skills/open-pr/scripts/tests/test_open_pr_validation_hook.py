#!/usr/bin/env python3
"""Tests for open_pr.py validator integration."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import open_pr


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pr_body"


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def write_temp_body(body: str) -> str:
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False)
    handle.write(body)
    handle.flush()
    handle.close()
    return handle.name


def test_run_pr_body_validator_non_json_stdout(monkeypatch: pytest.MonkeyPatch):
    body = load_fixture("valid_not_schema_change.md")

    class FakeCP:
        returncode = 0
        stdout = "not json"
        stderr = ""

    monkeypatch.setattr(open_pr.subprocess, "run", lambda *args, **kwargs: FakeCP())
    result = open_pr._run_pr_body_validator(body, ["src/example.ts"], 330)
    assert result["status"] == "internal"


def test_resolve_changed_paths_autoresolve(monkeypatch: pytest.MonkeyPatch):
    class FakeCompleted:
        def __init__(self, stdout: str):
            self.stdout = stdout

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "merge-base", "main"]:
            return FakeCompleted("abc123\n")
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return FakeCompleted(".github/workflows/ci.yml\n.claude/skills/open-pr/scripts/open_pr.py\n")
        raise AssertionError(cmd)

    monkeypatch.setattr(open_pr.subprocess, "run", fake_run)
    paths = open_pr.resolve_changed_paths(None)
    assert paths == [".github/workflows/ci.yml", ".claude/skills/open-pr/scripts/open_pr.py"]
    assert any(cmd[:3] == ["git", "merge-base", "main"] for cmd in calls)
    assert any(cmd[:3] == ["git", "diff", "--name-only"] for cmd in calls)


def test_validator_fail_blocks_create(monkeypatch: pytest.MonkeyPatch):
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    create_called = {"value": False}
    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-330-validate-pr-body")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
        monkeypatch.setattr(
            open_pr,
            "_run_pr_body_validator",
            lambda body, changed_paths, linked_issue: {
                "status": "fail",
                "errors": [{"rule_id": "LP050"}],
                "message": "validation failed",
            },
        )
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)

        def fake_create_pr(*args, **kwargs):
            create_called["value"] = True
            raise AssertionError("create_pr should not be called")

        monkeypatch.setattr(open_pr, "create_pr", fake_create_pr)

        rc = open_pr.main(
            [
                "--pr-title",
                "feat: test",
                "--linked-issue",
                "330",
                "--publish",
                "yes",
                "--pr-body-file",
                body_path,
            ]
        )
        assert rc == 2
        assert create_called["value"] is False
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_non_json_validator_output(monkeypatch: pytest.MonkeyPatch):
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-330-validate-pr-body")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
        monkeypatch.setattr(
            open_pr,
            "_run_pr_body_validator",
            lambda body, changed_paths, linked_issue: {
                "status": "internal",
                "errors": [],
                "message": "Validator returned non-JSON output",
            },
        )
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)
        monkeypatch.setattr(open_pr, "create_pr", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no create")))
        rc = open_pr.main(
            [
                "--pr-title",
                "feat: test",
                "--linked-issue",
                "330",
                "--publish",
                "yes",
                "--pr-body-file",
                body_path,
            ]
        )
        assert rc == 2
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_validator_receives_final_body_with_closes_reference(monkeypatch: pytest.MonkeyPatch):
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    observed = {"body": ""}
    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-330-validate-pr-body")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])

        def fake_validator(body, changed_paths, linked_issue):
            observed["body"] = body
            return {"status": "pass", "errors": []}

        monkeypatch.setattr(open_pr, "_run_pr_body_validator", fake_validator)
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: {"number": 999, "url": "https://example.com/pr/999"})
        rc = open_pr.main(
            [
                "--pr-title",
                "feat: test",
                "--linked-issue",
                "330",
                "--publish",
                "yes",
                "--pr-body-file",
                body_path,
            ]
        )
        assert rc == 0
        assert "Closes #330" in observed["body"]
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_changed_paths_unavailable(monkeypatch: pytest.MonkeyPatch):
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-330-validate-pr-body")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: None)
        monkeypatch.setattr(
            open_pr,
            "_run_pr_body_validator",
            lambda body, changed_paths, linked_issue: {
                "status": "fail",
                "errors": [{"rule_id": "LP058"}],
                "message": "changed paths unavailable",
            },
        )
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)
        monkeypatch.setattr(open_pr, "create_pr", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no create")))
        rc = open_pr.main(
            [
                "--pr-title",
                "feat: test",
                "--linked-issue",
                "330",
                "--publish",
                "yes",
                "--pr-body-file",
                body_path,
            ]
        )
        assert rc == 2
    finally:
        Path(body_path).unlink(missing_ok=True)
