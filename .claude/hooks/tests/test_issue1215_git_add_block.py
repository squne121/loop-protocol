#!/usr/bin/env python3
"""Issue #1215: Block coverage for git add non-exact pathspec usage.

Covers original wide-pathspec forms (AC baseline) plus Fix 1/2 regression:
- wrapper-aware git add (bash -lc, command, env, git -C)
- directory-wide pathspec detection
- magic pathspec notation (`:()`)
"""

import pytest

from worktree_scope_guard_testkit import _bash_payload, _make_repo_with_worktree, _run_guard


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


# ── Fix 1 (P0): wrapper-aware git add ──────────────────────────────────────


@pytest.mark.parametrize(
    "cmd",
    [
        "bash -lc 'git add .'",
        "command git add .",
        "env FOO=1 git add .",
    ],
)
def test_wrapper_git_add_broad_is_blocked(tmp_path, cmd):
    """Fix 1 (P0): wrapper-transparent git add broad pathspec must be blocked."""
    repo = _make_repo_with_worktree(tmp_path, issue="1215", slug="issue-1215-wrapper-block")
    payload = _bash_payload(cmd, str(repo["worktree"]))
    result = _run_guard(payload, repo["root"], issue="1215")
    assert result.returncode == 2, f"{cmd!r} should be blocked; stderr={result.stderr}"


def test_git_minus_c_add_broad_is_blocked(tmp_path):
    """Fix 1 (P0): git -C <worktree> add . must be blocked."""
    repo = _make_repo_with_worktree(tmp_path, issue="1215", slug="issue-1215-c-block")
    wt = repo["worktree"]
    cmd = f"git -C {wt} add ."
    payload = _bash_payload(cmd, str(wt))
    result = _run_guard(payload, repo["root"], issue="1215")
    assert result.returncode == 2, f"{cmd!r} should be blocked; stderr={result.stderr}"


# ── Fix 2 (P0): directory-wide pathspec detection ───────────────────────────


@pytest.mark.parametrize(
    "dirname",
    [
        "src",
        "scripts",
    ],
)
def test_directory_pathspec_is_blocked(tmp_path, dirname):
    """Fix 2 (P0): git add <existing-directory> must be blocked."""
    repo = _make_repo_with_worktree(tmp_path, issue="1215", slug="issue-1215-dir-block")
    wt = repo["worktree"]
    # Create the directory so the isdir check fires.
    (wt / dirname).mkdir(parents=True, exist_ok=True)
    cmd = f"git add {dirname}"
    payload = _bash_payload(cmd, str(wt))
    result = _run_guard(payload, repo["root"], issue="1215")
    assert result.returncode == 2, f"{cmd!r} (directory pathspec) should be blocked; stderr={result.stderr}"


@pytest.mark.parametrize(
    "cmd",
    [
        "git add :/",
        "git add ':(glob)**'",
        "git add --pathspec-from-file paths.txt",
    ],
)
def test_magic_and_file_pathspec_forms_blocked(tmp_path, cmd):
    """Fix 2 (P0): magic pathspec forms and --pathspec-from-file must be blocked."""
    repo = _make_repo_with_worktree(tmp_path, issue="1215", slug="issue-1215-magic-block")
    payload = _bash_payload(cmd, str(repo["worktree"]))
    result = _run_guard(payload, repo["root"], issue="1215")
    assert result.returncode == 2, f"{cmd!r} should be blocked; stderr={result.stderr}"


def test_git_minus_c_add_directory_is_blocked(tmp_path):
    """Fix 1+2 (P0): git -C <worktree> add <dir> must be blocked when dir exists."""
    repo = _make_repo_with_worktree(tmp_path, issue="1215", slug="issue-1215-c-dir-block")
    wt = repo["worktree"]
    (wt / "src").mkdir(parents=True, exist_ok=True)
    cmd = f"git -C {wt} add src"
    payload = _bash_payload(cmd, str(wt))
    result = _run_guard(payload, repo["root"], issue="1215")
    assert result.returncode == 2, f"{cmd!r} should be blocked; stderr={result.stderr}"
