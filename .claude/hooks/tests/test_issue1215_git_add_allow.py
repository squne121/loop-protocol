#!/usr/bin/env python3
"""Issue #1215: Allow coverage for explicit git add pathspecs in scope guard."""

from worktree_scope_guard_testkit import _bash_payload, _make_repo_with_worktree, _run_guard


def test_issue1215_git_add_allow_explicit_file(tmp_path):
    repo = _make_repo_with_worktree(tmp_path, issue="1215", slug="issue-1215-allow")
    payload = _bash_payload("git add src/file.txt", str(repo["worktree"]))
    result = _run_guard(payload, repo["root"], issue="1215")
    assert result.returncode == 0, f"expected allow, got={result.returncode}; stderr={result.stderr}"


def test_issue1215_git_add_allow_double_dash_explicit_pathspec(tmp_path):
    repo = _make_repo_with_worktree(tmp_path, issue="1215", slug="issue-1215-allow")
    payload = _bash_payload("git add -- src/file.txt", str(repo["worktree"]))
    result = _run_guard(payload, repo["root"], issue="1215")
    assert result.returncode == 0, f"expected allow, got={result.returncode}; stderr={result.stderr}"
