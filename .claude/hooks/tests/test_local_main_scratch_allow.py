#!/usr/bin/env python3
"""test_local_main_scratch_allow.py — behavioral harness for LOCAL_MAIN_SCRATCH_ALLOW_V1 (Issue #974).

Tests the local main scratch allow exception in worktree_scope_guard.py.
Uses real git repos + real issue worktrees + subprocess invocation of the guard.

AC11: pytest behavioral harness with real git repo + real issue worktree creation,
subprocess-based guard invocation.

Design: The LOCAL_MAIN_SCRATCH_ALLOW_V1 exception is evaluated when an active issue
worktree exists but the write target is outside the expected worktree. The exception
allows writes to safe scratch prefixes (gitignored, untracked, non-sensitive, no-symlink).
Tests use LOOP_ISSUE_NUMBER + a real issue worktree to trigger the worktree guard,
then test whether the local main scratch exception fires correctly.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

# Anchor on __file__ for worktree isolation.
_THIS_FILE = Path(__file__).resolve()
REPO_ROOT = _THIS_FILE.parent.parent.parent.parent  # worktree root
GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "worktree_scope_guard.sh"
GUARD_PY = REPO_ROOT / ".claude" / "hooks" / "worktree_scope_guard.py"


# =============================================================================
# Harness helpers
# =============================================================================

def _git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def _make_repo_with_worktree(tmp_path: Path, issue: str = "974",
                              slug: str = "test") -> dict:
    """Create a git repo + a real issue worktree. Returns dict with paths."""
    main = tmp_path / "repo"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    (main / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=main)
    _git("commit", "-q", "-m", "seed", cwd=main)

    wt_parent = main / ".claude" / "worktrees"
    wt_parent.mkdir(parents=True, exist_ok=True)
    wt_path = wt_parent / f"issue-{issue}-{slug}"
    branch = f"issue-{issue}-{slug}"
    _git("worktree", "add", "-q", "-b", branch, str(wt_path), "main", cwd=main)

    return {"root": main, "worktree": wt_path}


def _add_gitignore(repo: Path, entries: list[str]) -> None:
    """Append entries to repo/.gitignore and commit."""
    gi_path = repo / ".gitignore"
    existing = gi_path.read_text() if gi_path.exists() else ""
    gi_path.write_text(existing + "\n".join(entries) + "\n")
    _git("add", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "add gitignore", cwd=repo)


def _run_guard(payload: dict, project_root: Path, issue: str,
               extra_env: dict | None = None):
    """Run the guard via subprocess. Returns CompletedProcess."""
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    env["LOOP_ISSUE_NUMBER"] = str(issue)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(GUARD_SH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
    )


def _write_payload(file_path: str, cwd: str, tool: str = "Write") -> dict:
    return {"tool_name": tool, "tool_input": {"file_path": file_path}, "cwd": cwd}


def _edit_payload(file_path: str, cwd: str) -> dict:
    return {"tool_name": "Edit", "tool_input": {"file_path": file_path}, "cwd": cwd}


# =============================================================================
# allow cases (AC2) — local main context + safe prefix + gitignored + untracked
# =============================================================================

def test_allow_local_main_write_to_repo_ignored_untracked_playwright_report(tmp_path):
    """AC2: Write to playwright-report/ (gitignored, untracked) from local main is allowed.

    Setup: issue worktree exists, cwd=project_root (main branch), target is
    playwright-report/ which is in repo .gitignore and not tracked.
    """
    repo = _make_repo_with_worktree(tmp_path, issue="974")
    _add_gitignore(repo["root"], ["playwright-report/", "playwright-report/**"])

    target = repo["root"] / "playwright-report" / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = _write_payload(str(target), str(repo["root"]))
    result = _run_guard(payload, repo["root"], issue="974")
    assert result.returncode == 0, (
        f"Expected allow (exit 0), got {result.returncode}. stderr={result.stderr}"
    )


def test_allow_local_main_write_to_repo_ignored_untracked_test_results(tmp_path):
    """AC2: Write to test-results/ (gitignored, untracked) from local main is allowed.

    Setup: issue worktree exists, cwd=project_root (main branch), target is
    test-results/ which is in repo .gitignore and not tracked.
    """
    repo = _make_repo_with_worktree(tmp_path, issue="974")
    _add_gitignore(repo["root"], ["test-results/", "test-results/**"])

    target = repo["root"] / "test-results" / "result.xml"
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = _write_payload(str(target), str(repo["root"]))
    result = _run_guard(payload, repo["root"], issue="974")
    assert result.returncode == 0, (
        f"Expected allow (exit 0), got {result.returncode}. stderr={result.stderr}"
    )


# =============================================================================
# block cases (AC3, AC4, AC5, AC6, AC7, AC8)
# =============================================================================

def test_block_safe_prefix_not_in_repo_gitignore(tmp_path):
    """AC3/AC8: Write to artifacts/ that is NOT in repo .gitignore is blocked.

    Even though artifacts/ is a safe prefix, it is not gitignored → block.
    """
    repo = _make_repo_with_worktree(tmp_path, issue="974")
    # artifacts/ is NOT added to .gitignore

    target = repo["root"] / "artifacts" / "output.txt"
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = _write_payload(str(target), str(repo["root"]))
    result = _run_guard(payload, repo["root"], issue="974")
    assert result.returncode == 2, (
        f"Expected block (exit 2), got {result.returncode}. stderr={result.stderr}"
    )


def test_block_global_gitignore_only_match(tmp_path):
    """AC8: If only global gitignore matches (not repo-local), Write is blocked.

    We test this by ensuring .gitignore does NOT contain the entry,
    so git check-ignore -v would not show a repo-local source.
    """
    repo = _make_repo_with_worktree(tmp_path, issue="974")
    # No repo-local .gitignore entry for artifacts/

    target = repo["root"] / "artifacts" / "report.html"
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = _write_payload(str(target), str(repo["root"]))
    result = _run_guard(payload, repo["root"], issue="974")
    assert result.returncode == 2, (
        f"Expected block (exit 2), got {result.returncode}. stderr={result.stderr}"
    )


def test_block_tracked_file_under_safe_prefix(tmp_path):
    """AC7: A tracked file under a safe prefix is blocked."""
    repo = _make_repo_with_worktree(tmp_path, issue="974")
    _add_gitignore(repo["root"], ["artifacts/", "artifacts/**"])

    # Create and force-track a file under artifacts/ (needs -f since artifacts/ is gitignored)
    artifacts_dir = repo["root"] / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    tracked = artifacts_dir / "tracked.txt"
    tracked.write_text("tracked content\n")
    _git("add", "-f", "artifacts/tracked.txt", cwd=repo["root"])
    _git("commit", "-q", "-m", "track artifact", cwd=repo["root"])

    payload = _write_payload(str(tracked), str(repo["root"]))
    result = _run_guard(payload, repo["root"], issue="974")
    assert result.returncode == 2, (
        f"Expected block (exit 2), got {result.returncode}. stderr={result.stderr}"
    )


def test_block_artifacts_prefix_sibling_artifacts_evil(tmp_path):
    """AC4: artifacts_evil/ is NOT under artifacts/ prefix (component boundary).

    'artifacts_evil' starts with 'artifacts' but is a sibling, not a child.
    Component-boundary check must reject it.
    """
    repo = _make_repo_with_worktree(tmp_path, issue="974")
    _add_gitignore(repo["root"], ["artifacts_evil/"])

    target = repo["root"] / "artifacts_evil" / "output.txt"
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = _write_payload(str(target), str(repo["root"]))
    result = _run_guard(payload, repo["root"], issue="974")
    assert result.returncode == 2, (
        f"Expected block (exit 2), got {result.returncode}. stderr={result.stderr}"
    )


def test_block_artifacts_dotdot_readme(tmp_path):
    """AC4: artifacts/../README.md resolves to README.md (not under artifacts/).

    Path traversal with .. must be blocked — realpath resolves to tracked README.
    """
    repo = _make_repo_with_worktree(tmp_path, issue="974")
    _add_gitignore(repo["root"], ["artifacts/"])

    # The path artifacts/../README.md resolves to README.md which is tracked
    target = str(repo["root"] / "artifacts" / ".." / "README.md")

    payload = _write_payload(target, str(repo["root"]))
    result = _run_guard(payload, repo["root"], issue="974")
    assert result.returncode == 2, (
        f"Expected block (exit 2), got {result.returncode}. stderr={result.stderr}"
    )


def test_block_symlink_safe_root_to_outside(tmp_path):
    """AC5: If the safe_root (e.g. artifacts/) itself is a symlink, block."""
    repo = _make_repo_with_worktree(tmp_path, issue="974")
    _add_gitignore(repo["root"], ["artifacts/"])

    # Create a real artifacts directory outside repo, then symlink it
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    symlink_target = repo["root"] / "artifacts"
    os.symlink(str(outside), str(symlink_target))

    target = repo["root"] / "artifacts" / "output.txt"

    payload = _write_payload(str(target), str(repo["root"]))
    result = _run_guard(payload, repo["root"], issue="974")
    assert result.returncode == 2, (
        f"Expected block (exit 2), got {result.returncode}. stderr={result.stderr}"
    )


def test_block_symlink_parent_component(tmp_path):
    """AC5: A symlink as a parent directory component under safe prefix is blocked."""
    repo = _make_repo_with_worktree(tmp_path, issue="974")
    _add_gitignore(repo["root"], ["artifacts/"])

    # Create artifacts/ normally, then add a symlink subdir
    artifacts_dir = repo["root"] / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    outside_dir = tmp_path / "outside-subdir"
    outside_dir.mkdir()
    symlink_subdir = artifacts_dir / "symlinked-subdir"
    os.symlink(str(outside_dir), str(symlink_subdir))

    target = symlink_subdir / "output.txt"

    payload = _write_payload(str(target), str(repo["root"]))
    result = _run_guard(payload, repo["root"], issue="974")
    assert result.returncode == 2, (
        f"Expected block (exit 2), got {result.returncode}. stderr={result.stderr}"
    )


def test_block_existing_target_symlink(tmp_path):
    """AC5: If the target itself is an existing symlink, block."""
    repo = _make_repo_with_worktree(tmp_path, issue="974")
    _add_gitignore(repo["root"], ["artifacts/"])

    artifacts_dir = repo["root"] / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    real_file = tmp_path / "real-file.txt"
    real_file.write_text("real\n")
    sym_target = artifacts_dir / "symlink-file.txt"
    os.symlink(str(real_file), str(sym_target))

    payload = _write_payload(str(sym_target), str(repo["root"]))
    result = _run_guard(payload, repo["root"], issue="974")
    assert result.returncode == 2, (
        f"Expected block (exit 2), got {result.returncode}. stderr={result.stderr}"
    )


def test_block_sensitive_dotenv_under_artifacts(tmp_path):
    """AC6: .env file under artifacts/ is blocked (sensitive path denylist)."""
    repo = _make_repo_with_worktree(tmp_path, issue="974")
    _add_gitignore(repo["root"], ["artifacts/", "artifacts/**"])

    target = repo["root"] / "artifacts" / ".env"
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = _write_payload(str(target), str(repo["root"]))
    result = _run_guard(payload, repo["root"], issue="974")
    assert result.returncode == 2, (
        f"Expected block (exit 2), got {result.returncode}. stderr={result.stderr}"
    )


def test_block_sensitive_npmrc_under_cache(tmp_path):
    """AC6: .npmrc file under .cache/ is blocked (sensitive path denylist)."""
    repo = _make_repo_with_worktree(tmp_path, issue="974")
    _add_gitignore(repo["root"], [".cache/", ".cache/**"])

    target = repo["root"] / ".cache" / ".npmrc"
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = _write_payload(str(target), str(repo["root"]))
    result = _run_guard(payload, repo["root"], issue="974")
    assert result.returncode == 2, (
        f"Expected block (exit 2), got {result.returncode}. stderr={result.stderr}"
    )


def test_block_write_when_cwd_inside_issue_worktree(tmp_path):
    """AC1: When cwd is inside an issue worktree (not project root), local main exception does NOT apply.

    Even if target is in a safe prefix of the project root, if cwd is NOT the
    project root, the local main context check fails.
    """
    repo = _make_repo_with_worktree(tmp_path, issue="974")
    _add_gitignore(repo["root"], ["artifacts/", "artifacts/**"])

    # target is in artifacts/ of repo root (a safe prefix)
    target = repo["root"] / "artifacts" / "output.txt"
    target.parent.mkdir(parents=True, exist_ok=True)

    # cwd is the issue worktree (not project root) — local main context fails AC1 cond1
    payload = _write_payload(str(target), str(repo["worktree"]))
    result = _run_guard(payload, repo["root"], issue="974")
    assert result.returncode == 2, (
        f"Expected block (exit 2), got {result.returncode}. stderr={result.stderr}"
    )


def test_block_write_when_branch_is_issue_branch_not_main(tmp_path):
    """AC1: When current branch is an issue branch (not main), local main exception does NOT apply.

    We create a repo with an issue branch as the main checkout branch.
    cwd=project_root but branch != main → local main context fails AC1 cond2.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "issue-99-test", cwd=repo)
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)

    # Add gitignore
    gi = repo / ".gitignore"
    gi.write_text("artifacts/\nartifacts/**\n")
    _git("add", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "gitignore", cwd=repo)

    # Create a worktree-style directory so guard can resolve worktree
    wt_parent = repo / ".claude" / "worktrees"
    wt_parent.mkdir(parents=True, exist_ok=True)
    wt_path = wt_parent / "issue-99-test"
    _git("worktree", "add", "-q", str(wt_path), "HEAD", cwd=repo)

    target = repo / "artifacts" / "output.txt"
    target.parent.mkdir(parents=True, exist_ok=True)

    # The main checkout is on issue branch, not main
    payload = _write_payload(str(target), str(repo))
    env = {"CLAUDE_PROJECT_DIR": str(repo), "LOOP_ISSUE_NUMBER": "99"}
    result = subprocess.run(
        ["bash", str(GUARD_SH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env={**os.environ, **env},
    )
    assert result.returncode == 2, (
        f"Expected block (exit 2), got {result.returncode}. stderr={result.stderr}"
    )
