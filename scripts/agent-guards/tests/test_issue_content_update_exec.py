"""Deterministic contract tests for ``issue_content.update`` (Issue #1660)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

GUARDS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(GUARDS))

import controlled_skill_mutation_exec as executor  # noqa: E402
from controlled_skill_mutation_policy import (  # noqa: E402
    ALL_COMMAND_IDS,
    COMMAND_ID_ISSUE_CONTENT_UPDATE,
    CONTROLLED_SKILL_MUTATION_COMMAND_POLICY,
    INPUT_SCHEMA_BY_COMMAND,
    TRUSTED_REPO,
)


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def content_input(issue_number: int = 1660, **overrides: object) -> dict:
    data = {
        "schema": "ISSUE_CONTENT_UPDATE_INPUT_V1",
        "issue_number": issue_number,
        "repo": TRUSTED_REPO,
        "expected_previous_title": "old title",
        "expected_previous_body_sha256": sha("old body"),
        "expected_previous_updated_at": "2026-07-22T00:00:00Z",
        "new_title": "new title",
        "new_body": "new body",
        "new_body_sha256": sha("new body"),
        "operation_reason": "test content update",
        "idempotency_key": "test:1660:one",
    }
    data.update(overrides)
    return data


@pytest.fixture()
def tmp_project(tmp_path, monkeypatch):
    (tmp_path / "scripts" / "agent-guards").mkdir(parents=True)
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        [
            "git", "-C", str(tmp_path), "remote", "add", "origin",
            f"https://github.com/{TRUSTED_REPO}.git",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(executor, "PROJECT_ROOT", tmp_path)
    return tmp_path


def write_input(tmp_project: Path, data: dict) -> str:
    path = (
        tmp_project / "artifacts" / str(data["issue_number"])
        / "issue-metadata" / COMMAND_ID_ISSUE_CONTENT_UPDATE / "input.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path.relative_to(tmp_project))


def run(tmp_project: Path, data: dict, monkeypatch, capsys) -> tuple[int, dict]:
    input_file = write_input(tmp_project, data)
    monkeypatch.setattr(executor, "_find_gh_bin", lambda: ("/bin/gh", ""))
    monkeypatch.setattr(executor, "_verify_git_remote_origin", lambda *_: "")
    rc = executor.main([
        "--command-id", COMMAND_ID_ISSUE_CONTENT_UPDATE,
        "--issue-number", str(data["issue_number"]), "--input-file", input_file,
        "--repo", TRUSTED_REPO, "--json",
    ])
    return rc, json.loads(capsys.readouterr().out)


def test_issue_content_update_policy_schema_dispatch_parity():
    assert COMMAND_ID_ISSUE_CONTENT_UPDATE in ALL_COMMAND_IDS
    assert INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_ISSUE_CONTENT_UPDATE] == "ISSUE_CONTENT_UPDATE_INPUT_V1"
    mutation_policy = CONTROLLED_SKILL_MUTATION_COMMAND_POLICY[
        COMMAND_ID_ISSUE_CONTENT_UPDATE
    ]["github_mutation"]
    assert mutation_policy["fixed_fields"] == ["title", "body"]
    assert "_run_issue_content_update" in Path(executor.__file__).read_text(encoding="utf-8")


def test_title_and_body_are_sent_in_one_patch(tmp_project, monkeypatch, capsys):
    data = content_input()
    readbacks = iter([
        {"title": "old title", "body": "old body", "updatedAt": "2026-07-22T00:00:00Z"},
        {"title": "new title", "body": "new body", "updatedAt": "2026-07-22T00:00:01Z"},
    ])
    monkeypatch.setattr(executor, "_fetch_issue_content", lambda *_: (next(readbacks), ""))
    with patch.object(executor, "_patch_issue_content", return_value="") as patch_once:
        monkeypatch.setattr(executor, "_check_no_tracked_changes", lambda *_: [])
        rc, payload = run(tmp_project, data, monkeypatch, capsys)
    assert rc == 0
    assert payload["new_title"] == "new title"
    patch_once.assert_called_once_with(1660, TRUSTED_REPO, "new title", "new body", "/bin/gh")


def test_ambiguous_and_precondition_failures_do_not_retry_patch(tmp_project, monkeypatch, capsys):
    data = content_input()
    old_content = {
        "title": "old title",
        "body": "old body",
        "updatedAt": "2026-07-22T00:00:00Z",
    }
    monkeypatch.setattr(executor, "_fetch_issue_content", lambda *_: (old_content, ""))
    with patch.object(executor, "_patch_issue_content", return_value="gh_api_patch_failed: timeout") as patch_once:
        rc, payload = run(tmp_project, data, monkeypatch, capsys)
    assert rc == 1
    assert payload["status"] == "failed"
    patch_once.assert_called_once()

    stale = content_input(expected_previous_title="someone else")
    with patch.object(executor, "_patch_issue_content") as patch_never:
        rc, payload = run(tmp_project, stale, monkeypatch, capsys)
    assert rc == 1
    assert payload["reason"] == "stale_precondition_title_mismatch"
    patch_never.assert_not_called()


@pytest.mark.parametrize("title", ["", "  ", "bad\nvalue", "bad\x00value"])
def test_invalid_title_is_rejected_before_patch(tmp_project, monkeypatch, capsys, title):
    data = content_input(new_title=title)
    with patch.object(executor, "_patch_issue_content") as patch_never:
        rc, payload = run(tmp_project, data, monkeypatch, capsys)
    assert rc == 2
    assert payload["reason"].startswith("issue_content_update_")
    patch_never.assert_not_called()


def test_unknown_key_is_rejected_before_patch(tmp_project, monkeypatch, capsys):
    data = content_input(extra="no")
    with patch.object(executor, "_patch_issue_content") as patch_never:
        rc, payload = run(tmp_project, data, monkeypatch, capsys)
    assert rc == 2
    assert payload["reason"].startswith("issue_content_update_unknown_fields")
    patch_never.assert_not_called()
