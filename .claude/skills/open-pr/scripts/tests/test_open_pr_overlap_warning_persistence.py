#!/usr/bin/env python3
"""#1851 AC8 / Major 1: overlap preflight *evidence* warnings must be
persisted as a PR comment (not just ephemeral stdout/stderr) once
`gh pr create` succeeds.

Evidence validation failures (invalid/missing/stale/drift -- excluding
canonical repository resolution and cross-repo repository-binding mismatch,
which stay fail-closed per AC6/AC7) remain advisory: PR creation continues,
but at least one PR comment containing the fixed HTML marker
`<!-- loop-protocol:overlap-preflight-warnings-v1 -->` must be posted.
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
    monkeypatch.setattr(open_pr, "resolve_canonical_repository", lambda repo: repo)
    # Evidence file is intentionally missing -> E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING
    # (advisory-only per #1851 Major 1). The online recheck subprocess must
    # never run in this scenario (evidence read fails before that point).
    monkeypatch.setattr(open_pr, "create_pr", lambda *a, **k: "https://github.com/squne121/loop-protocol/pull/9999")


def _run_main(monkeypatch: pytest.MonkeyPatch, linked_issue: int, extra_args: list[str]) -> tuple[int, list[str]]:
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    output_lines: list[str] = []

    def capture_print(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        output_lines.append(sep.join(str(a) for a in args))

    try:
        monkeypatch.setattr("builtins.print", capture_print)
        base_args = [
            "--pr-title", "feat: test",
            "--linked-issue", str(linked_issue),
            "--publish", "yes",
            "--pr-body-file", body_path,
        ]
        base_args.extend(extra_args)
        rc = open_pr.main(base_args)
        return rc, output_lines
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_overlap_evidence_invalid_posts_marker_comment_after_pr_create(
    monkeypatch: pytest.MonkeyPatch,
):
    """GIVEN missing overlap preflight evidence (advisory-only validation
    failure) WHEN `gh pr create` succeeds THEN main() posts a PR comment
    containing the fixed marker via `gh pr comment --body-file` (AC8)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)

    def fail_if_online_recheck_called(*args, **kwargs):
        raise AssertionError("online overlap recheck subprocess should not run")

    monkeypatch.setattr(open_pr.subprocess, "run", fail_if_online_recheck_called)

    posted_comments: list[dict] = []

    def fake_run_gh(*args, check: bool = True):
        if args[:2] == ("pr", "comment"):
            body_file_index = args.index("--body-file") + 1
            body_text = Path(args[body_file_index]).read_text(encoding="utf-8")
            posted_comments.append({"args": args, "body": body_text})

            class _FakeCompleted:
                stdout = ""
                stderr = ""

            return _FakeCompleted()
        raise AssertionError(f"unexpected gh invocation in this test: {args}")

    monkeypatch.setattr(open_pr, "run_gh", fake_run_gh)

    rc, lines = _run_main(
        monkeypatch,
        1458,
        [
            "--overlap-preflight-required",
            "--overlap-preflight-evidence-file", "/nonexistent/does-not-exist-1851.json",
            "--overlap-preflight-expected-evidence-sha256", "sha256:" + "a" * 64,
            "--overlap-preflight-expected-decision-inputs-sha256", "sha256:" + "b" * 64,
        ],
    )

    assert rc == 0, lines
    assert any(
        line == f"WARNING={open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_MISSING}" for line in lines
    ), lines
    assert len(posted_comments) == 1, posted_comments
    assert posted_comments[0]["args"][2] == "9999"
    assert open_pr.OVERLAP_PREFLIGHT_WARNING_COMMENT_MARKER in posted_comments[0]["body"]
    assert any(
        line == "OVERLAP_PREFLIGHT_WARNING_COMMENT_POSTED=true" for line in lines
    ), lines


def test_no_evidence_warnings_does_not_post_comment(monkeypatch: pytest.MonkeyPatch):
    """GIVEN no overlap preflight gate at all (gate inactive) WHEN
    `gh pr create` succeeds THEN no PR comment is posted (no warnings to
    persist)."""
    _common_monkeypatches(monkeypatch, linked_issue=1458)

    def fail_if_gh_called(*args, **kwargs):
        raise AssertionError("gh pr comment should not run when there are no overlap warnings")

    monkeypatch.setattr(open_pr, "run_gh", fail_if_gh_called)

    rc, lines = _run_main(monkeypatch, 1458, [])

    assert rc == 0, lines
    assert not any(line.startswith("OVERLAP_PREFLIGHT_WARNING_COMMENT_POSTED") for line in lines), lines
