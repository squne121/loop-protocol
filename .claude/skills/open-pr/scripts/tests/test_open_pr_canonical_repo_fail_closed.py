#!/usr/bin/env python3
"""#1679 AC7: canonical repository resolution failure must be fail-closed
(EXIT_BLOCKED, `gh pr create` never invoked). This is a distinct safety
boundary (Issue #1470 canonical repository binding) that is kept after
peer OPEN Issue overlap preflight is removed from the production path
(#1679) -- it is independent of overlap preflight and always stops PR
publication."""

from __future__ import annotations

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


def _common_monkeypatches(monkeypatch: pytest.MonkeyPatch, linked_issue: int = 1458) -> None:
    monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
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
    monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)
    # AC7: simulate canonical repository resolution failure regardless of
    # WHY it failed (network error / gh missing / non-2xx response) -- the
    # producer already collapses all of these into `None`.
    monkeypatch.setattr(open_pr, "resolve_canonical_repository", lambda repo: None)


def _run_main(
    monkeypatch: pytest.MonkeyPatch, linked_issue: int, extra_args: list[str]
) -> tuple[int, list[str], bool]:
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    output_lines: list[str] = []
    create_called = {"value": False}

    def capture_print(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        output_lines.append(sep.join(str(a) for a in args))

    def fake_create_pr(*args, **kwargs):
        create_called["value"] = True
        return "https://github.com/squne121/loop-protocol/pull/9999"

    def fail_if_gh_pr_create_called(*args, **kwargs):
        create_called["value"] = True
        raise AssertionError("gh pr create should never be invoked (AC7 fail-closed)")

    try:
        monkeypatch.setattr(open_pr, "create_pr", fail_if_gh_pr_create_called)
        monkeypatch.setattr("builtins.print", capture_print)
        base_args = [
            "--pr-title", "feat: test",
            "--linked-issue", str(linked_issue),
            "--publish", "yes",
            "--pr-body-file", body_path,
        ]
        base_args.extend(extra_args)
        rc = open_pr.main(base_args)
        return rc, output_lines, create_called["value"]
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_canonical_repo_resolution_failure_blocks_with_overlap_gate_inactive(
    monkeypatch: pytest.MonkeyPatch,
):
    """GIVEN resolve_canonical_repository() returns None WHEN there is no
    peer OPEN Issue overlap preflight in the production path (#1679) THEN
    main() still fails closed -- canonical repository resolution / PR
    mutation target binding (Issue #1470) is an independent fail-closed
    safety boundary."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)

    rc, lines, create_called = _run_main(
        monkeypatch,
        1458,
        [],
    )

    assert rc == open_pr.EXIT_BLOCKED
    assert create_called is False
    assert any(
        line == f"ERROR={open_pr.E_CANONICAL_REPOSITORY_RESOLUTION_FAILED}" for line in lines
    ), lines
