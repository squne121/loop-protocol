#!/usr/bin/env python3
"""#1679 Major 2 / AC16: canonical repository resolution positive
regression tests, restored without any dependency on the removed overlap
preflight hard gate.

These tests were previously exercised inside
``test_open_pr_overlap_gate.py`` (removed by #1679 In Scope item 4/5)
alongside the overlap preflight hard gate machinery. Canonical
repository resolution / PR mutation target binding (Issue #1470) is an
independent fail-closed safety boundary that is unaffected by the overlap
gate removal, so these behaviors are re-verified here as overlap-gate-free
tests against the current production `main()` control flow."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import open_pr


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pr_body"

# Captured before any monkeypatching so tests needing the REAL
# resolve_canonical_repository() (mixed-case / rename alias resolution) can
# restore it after generic monkeypatches install a default identity mock.
_REAL_RESOLVE_CANONICAL_REPOSITORY = open_pr.resolve_canonical_repository


class FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def write_temp_body(body: str) -> str:
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False)
    handle.write(body)
    handle.flush()
    handle.close()
    return handle.name


def _common_monkeypatches(monkeypatch: pytest.MonkeyPatch, linked_issue: int = 1458) -> None:
    monkeypatch.setattr(open_pr, "resolve_branch", lambda: f"worktree-issue-{linked_issue}-test")
    monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
    monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
    monkeypatch.setattr(
        open_pr,
        "_run_pr_body_validator",
        lambda body, changed_paths, linked_issue: {"status": "pass", "errors": []},
    )
    monkeypatch.setattr(
        open_pr,
        "_run_japanese_content_validator",
        lambda body_text, threshold=0.1: {
            "status": "pass",
            "failed_blocks": 0,
            "aggregate_ratio": 0.5,
            "threshold": 0.1,
            "body_sha256": "",
            "stderr": "",
        },
    )


def _run_main(monkeypatch: pytest.MonkeyPatch, linked_issue: int) -> tuple[int, list[str]]:
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    output_lines: list[str] = []

    def capture_print(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        output_lines.append(sep.join(str(a) for a in args))

    try:
        monkeypatch.setattr("builtins.print", capture_print)
        rc = open_pr.main(
            [
                "--pr-title", "feat: test",
                "--linked-issue", str(linked_issue),
                "--publish", "yes",
                "--pr-body-file", body_path,
            ]
        )
        return rc, output_lines
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_mixed_case_canonical_repo_resolution_used_for_pr_create(monkeypatch: pytest.MonkeyPatch):
    """GIVEN a requested repo with mixed-case owner/name segments WHEN
    resolved THEN the canonical lowercase full_name (from the GitHub API) is
    the repo passed to `create_pr`."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    monkeypatch.setattr(open_pr, "resolve_repo", lambda: "SQUNE121/LOOP-PROTOCOL")
    monkeypatch.setattr(open_pr, "resolve_canonical_repository", _REAL_RESOLVE_CANONICAL_REPOSITORY)
    monkeypatch.setattr(
        open_pr,
        "run_gh",
        lambda *args, **kwargs: FakeCompletedProcess(
            0, json.dumps({"full_name": "squne121/loop-protocol"}), ""
        ),
    )
    monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)

    observed_create_pr_repo = {"repo": None}

    def fake_create_pr(repo, title, body_file, branch, draft):
        observed_create_pr_repo["repo"] = repo
        return "https://github.com/squne121/loop-protocol/pull/9999"

    monkeypatch.setattr(open_pr, "create_pr", fake_create_pr)

    rc, lines = _run_main(monkeypatch, 1458)
    assert rc == 0, lines
    assert observed_create_pr_repo["repo"] == "squne121/loop-protocol"


def test_renamed_repository_alias_resolves_to_current_full_name(monkeypatch: pytest.MonkeyPatch):
    """GIVEN a requested repo that is a stale post-rename/transfer alias
    WHEN resolved THEN the GitHub API's current `full_name` is used as the
    canonical PR mutation target."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/old-repo-name")
    monkeypatch.setattr(open_pr, "resolve_canonical_repository", _REAL_RESOLVE_CANONICAL_REPOSITORY)
    monkeypatch.setattr(
        open_pr,
        "run_gh",
        lambda *args, **kwargs: FakeCompletedProcess(
            0, json.dumps({"full_name": "squne121/new-repo-name"}), ""
        ),
    )
    monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)

    observed_create_pr_repo = {"repo": None}

    def fake_create_pr(repo, title, body_file, branch, draft):
        observed_create_pr_repo["repo"] = repo
        return "https://github.com/squne121/new-repo-name/pull/9999"

    monkeypatch.setattr(open_pr, "create_pr", fake_create_pr)

    rc, lines = _run_main(monkeypatch, 1458)
    assert rc == 0, lines
    assert observed_create_pr_repo["repo"] == "squne121/new-repo-name"


def test_existing_pr_found_at_canonical_target_short_circuits_without_create_pr(
    monkeypatch: pytest.MonkeyPatch,
):
    """GIVEN a mixed-case requested repo WHEN an existing PR is found at the
    resolved canonical target THEN `main()` reports the existing PR and
    `create_pr` is never invoked."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    monkeypatch.setattr(open_pr, "resolve_repo", lambda: "SQUNE121/LOOP-PROTOCOL")
    monkeypatch.setattr(open_pr, "resolve_canonical_repository", _REAL_RESOLVE_CANONICAL_REPOSITORY)
    monkeypatch.setattr(
        open_pr,
        "run_gh",
        lambda *args, **kwargs: FakeCompletedProcess(
            0, json.dumps({"full_name": "squne121/loop-protocol"}), ""
        ),
    )

    observed_repos: list[str] = []

    def fake_find_existing_pr(repo, branch):
        observed_repos.append(repo)
        if repo == "squne121/loop-protocol":
            return {"url": "https://github.com/squne121/loop-protocol/pull/4242", "number": 4242}
        return None

    monkeypatch.setattr(open_pr, "find_existing_pr", fake_find_existing_pr)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("create_pr should not run once canonical existing PR is found")

    monkeypatch.setattr(open_pr, "create_pr", fail_if_called)

    rc, lines = _run_main(monkeypatch, 1458)
    assert rc == 0, lines
    assert any(line == "EXISTING=true" for line in lines), lines
    assert any(line == "PR_NUMBER=4242" for line in lines), lines
    assert "squne121/loop-protocol" in observed_repos, observed_repos


def test_gh_missing_makes_canonical_resolution_fail_closed(monkeypatch: pytest.MonkeyPatch):
    """GIVEN `gh` is not installed (run_gh raises FileNotFoundError, a
    subclass of OSError) WHEN `resolve_canonical_repository()` runs as part
    of `main()` THEN it returns None and `main()` fails closed without ever
    calling `create_pr` (overlap-gate-independent; canonical repository
    resolution alone enforces this)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
    monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)

    def raise_file_not_found(*args, **kwargs):
        raise FileNotFoundError("gh: command not found")

    monkeypatch.setattr(open_pr, "run_gh", raise_file_not_found)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("create_pr should not run when gh is missing")

    monkeypatch.setattr(open_pr, "create_pr", fail_if_called)

    rc, lines = _run_main(monkeypatch, 1458)
    assert rc == open_pr.EXIT_BLOCKED
    assert any(
        line == f"ERROR={open_pr.E_CANONICAL_REPOSITORY_RESOLUTION_FAILED}" for line in lines
    ), lines


def test_api_failure_response_makes_canonical_resolution_fail_closed(monkeypatch: pytest.MonkeyPatch):
    """GIVEN the GitHub Repository API returns a non-JSON / malformed
    response WHEN `resolve_canonical_repository()` runs THEN it returns None
    and `main()` fails closed without calling `create_pr`."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)
    monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
    monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)
    monkeypatch.setattr(
        open_pr,
        "run_gh",
        lambda *args, **kwargs: FakeCompletedProcess(0, "not-json", ""),
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("create_pr should not run on canonical API failure")

    monkeypatch.setattr(open_pr, "create_pr", fail_if_called)

    rc, lines = _run_main(monkeypatch, 1458)
    assert rc == open_pr.EXIT_BLOCKED
    assert any(
        line == f"ERROR={open_pr.E_CANONICAL_REPOSITORY_RESOLUTION_FAILED}" for line in lines
    ), lines
