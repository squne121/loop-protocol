#!/usr/bin/env python3
"""Issue #1215: Block coverage for git add non-exact pathspec usage."""

from test_worktree_scope_guard import _bash_payload, _make_repo_with_worktree, _run_guard


def test_issue1215_no_git_add_exception_wide_pathspecs(tmp_path):
    repo = _make_repo_with_worktree(tmp_path, issue="1215", slug="issue-1215-block")
    for command in [
        "git add .",
        "git add -A",
        "git add -u",
        "git add --all",
        "git add '*.txt'",
        "git add --pathspec-from-file=paths.txt",
    ]:
        payload = _bash_payload(command, str(repo["worktree"]))
        result = _run_guard(payload, repo["root"], issue="1215")
        assert result.returncode == 2, f"{command!r} should be blocked; stderr={result.stderr}"
