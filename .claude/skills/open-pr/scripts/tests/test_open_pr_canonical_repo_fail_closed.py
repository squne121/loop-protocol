#!/usr/bin/env python3
"""#1851 AC6 / Blocker 2: canonical repository resolution failure must be
fail-closed (EXIT_BLOCKED, `gh pr create` never invoked), independently of
`overlap_gate_active`.

`resolve_canonical_repository()` returning `None` previously (incorrectly)
fell back to the raw requested `repo` with a warning-only continuation. This
is a distinct safety boundary (Issue #1470 canonical repository binding)
from the overlap preflight *evidence* advisory policy (#1851 Major 1) and
must always stop PR publication.
"""

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
    monkeypatch.setattr(
        open_pr, "fetch_current_linked_issue_labels", lambda repo, issue: ([], None)
    )
    # AC6: simulate canonical repository resolution failure regardless of
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
        raise AssertionError("gh pr create should never be invoked (AC6 fail-closed)")

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


def test_canonical_repo_resolution_failure_blocks_with_overlap_gate_active(
    monkeypatch: pytest.MonkeyPatch,
):
    """GIVEN resolve_canonical_repository() returns None WHEN overlap gate is
    active (--overlap-preflight-required) THEN main() fails closed before
    gh pr create."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)

    rc, lines, create_called = _run_main(
        monkeypatch,
        1458,
        ["--overlap-preflight-required"],
    )

    assert rc == open_pr.EXIT_BLOCKED
    assert create_called is False
    assert any(
        line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_SOURCE_FAILURE}" for line in lines
    ), lines


def test_canonical_repo_resolution_failure_blocks_with_overlap_gate_inactive(
    monkeypatch: pytest.MonkeyPatch,
):
    """GIVEN resolve_canonical_repository() returns None WHEN the overlap
    gate is NOT active (no --overlap-preflight-required, no forcing label)
    THEN main() still fails closed -- canonical repository resolution is
    independent of overlap_gate_active (#1851 fix_delta)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)

    rc, lines, create_called = _run_main(
        monkeypatch,
        1458,
        [],
    )

    assert rc == open_pr.EXIT_BLOCKED
    assert create_called is False
    assert any(
        line == f"ERROR={open_pr.E_OVERLAP_PREFLIGHT_SOURCE_FAILURE}" for line in lines
    ), lines
