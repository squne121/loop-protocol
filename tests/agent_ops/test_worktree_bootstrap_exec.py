from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "worktree_bootstrap_exec.py"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )


@pytest.fixture()
def temp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)
    return repo


def _run(repo: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    return result, payload


def test_given_valid_args_when_bootstrap_runs_then_worktree_is_created(temp_repo: Path) -> None:
    result, payload = _run(
        temp_repo,
        "--issue-number", "1209",
        "--slug", "worktree-bootstrap",
        "--branch-name", "worktree-issue-1209-worktree-bootstrap",
        "--worktree-path", ".claude/worktrees/issue-1209-worktree-bootstrap",
        "--base-ref", "main",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert payload["schema"] == "WORKTREE_BOOTSTRAP_RESULT_V1"
    assert payload["status"] == "ok_created"
    assert payload["branch"] == "worktree-issue-1209-worktree-bootstrap"
    assert payload["worktree_path"] == ".claude/worktrees/issue-1209-worktree-bootstrap"
    assert (temp_repo / ".claude" / "worktrees" / "issue-1209-worktree-bootstrap").is_dir()
    assert result.stderr == ""


def test_given_existing_matching_worktree_when_bootstrap_runs_then_ok_existing(temp_repo: Path) -> None:
    worktree = temp_repo / ".claude" / "worktrees" / "issue-1209-worktree-bootstrap"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git("branch", "worktree-issue-1209-worktree-bootstrap", cwd=temp_repo)
    _git("worktree", "add", str(worktree), "worktree-issue-1209-worktree-bootstrap", cwd=temp_repo)

    result, payload = _run(
        temp_repo,
        "--issue-number", "1209",
        "--slug", "worktree-bootstrap",
        "--branch-name", "worktree-issue-1209-worktree-bootstrap",
        "--worktree-path", ".claude/worktrees/issue-1209-worktree-bootstrap",
        "--base-ref", "refs/heads/main",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert payload["status"] == "ok_existing"
    assert payload["reason_code"] is None


def test_given_invalid_relative_escape_when_bootstrap_runs_then_path_is_rejected(temp_repo: Path) -> None:
    result, payload = _run(
        temp_repo,
        "--issue-number", "1209",
        "--slug", "worktree-bootstrap",
        "--branch-name", "worktree-issue-1209-worktree-bootstrap",
        "--worktree-path", ".claude/worktrees/../evil",
        "--base-ref", "main",
        "--json",
    )
    assert result.returncode == 1
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "invalid_path"


def test_given_invalid_branch_name_when_bootstrap_runs_then_branch_is_rejected(temp_repo: Path) -> None:
    result, payload = _run(
        temp_repo,
        "--issue-number", "1209",
        "--slug", "worktree-bootstrap",
        "--branch-name", "feature/not-allowed",
        "--worktree-path", ".claude/worktrees/issue-1209-worktree-bootstrap",
        "--base-ref", "main",
        "--json",
    )
    assert result.returncode == 1
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "invalid_branch"


def test_given_non_default_base_ref_when_bootstrap_runs_then_base_ref_is_rejected(temp_repo: Path) -> None:
    result, payload = _run(
        temp_repo,
        "--issue-number", "1209",
        "--slug", "worktree-bootstrap",
        "--branch-name", "worktree-issue-1209-worktree-bootstrap",
        "--worktree-path", ".claude/worktrees/issue-1209-worktree-bootstrap",
        "--base-ref", "HEAD~1",
        "--json",
    )
    assert result.returncode == 1
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "invalid_base_ref"


def test_given_existing_branch_on_other_path_when_bootstrap_runs_then_conflict_blocks(temp_repo: Path) -> None:
    other = temp_repo / ".claude" / "worktrees" / "issue-1209-other"
    other.parent.mkdir(parents=True, exist_ok=True)
    _git("branch", "worktree-issue-1209-worktree-bootstrap", cwd=temp_repo)
    _git("worktree", "add", str(other), "worktree-issue-1209-worktree-bootstrap", cwd=temp_repo)

    result, payload = _run(
        temp_repo,
        "--issue-number", "1209",
        "--slug", "worktree-bootstrap",
        "--branch-name", "worktree-issue-1209-worktree-bootstrap",
        "--worktree-path", ".claude/worktrees/issue-1209-worktree-bootstrap",
        "--base-ref", "main",
        "--json",
    )
    assert result.returncode == 1
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "existing_conflict"


def test_given_root_not_on_default_branch_when_bootstrap_runs_then_executor_blocks(temp_repo: Path) -> None:
    _git("switch", "-c", "feature/root-drift", cwd=temp_repo)
    result, payload = _run(
        temp_repo,
        "--issue-number", "1209",
        "--slug", "worktree-bootstrap",
        "--branch-name", "worktree-issue-1209-worktree-bootstrap",
        "--worktree-path", ".claude/worktrees/issue-1209-worktree-bootstrap",
        "--base-ref", "main",
        "--json",
    )
    assert result.returncode == 1
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "root_not_default_branch"


def test_given_worktrees_dir_is_symlink_when_bootstrap_runs_then_symlink_escape_is_rejected(tmp_path: Path) -> None:
    """B3: If .claude/worktrees is a symlink, executor must return blocked/invalid_path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)

    # Create .claude/worktrees as a symlink to an outside directory
    claude_dir = repo / ".claude"
    claude_dir.mkdir()
    worktrees_link = claude_dir / "worktrees"
    worktrees_link.symlink_to(outside)

    result, payload = _run(
        repo,
        "--issue-number", "1209",
        "--slug", "worktree-bootstrap",
        "--branch-name", "worktree-issue-1209-worktree-bootstrap",
        "--worktree-path", ".claude/worktrees/issue-1209-worktree-bootstrap",
        "--base-ref", "main",
        "--json",
    )
    assert result.returncode == 1, f"Expected blocked, got stdout={result.stdout}"
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "invalid_path"


def test_given_intermediate_symlink_escapes_repo_when_bootstrap_runs_then_realpath_guard_rejects(tmp_path: Path) -> None:
    """B3: If a symlink in the worktree path resolves outside project root, executor must block."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "escape_target"
    outside.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)

    # Create .claude/worktrees/issue-1209-worktree-bootstrap as a symlink pointing outside
    claude_dir = repo / ".claude"
    worktrees_dir = claude_dir / "worktrees"
    worktrees_dir.mkdir(parents=True)
    escape_link = worktrees_dir / "issue-1209-worktree-bootstrap"
    escape_link.symlink_to(outside)

    result, payload = _run(
        repo,
        "--issue-number", "1209",
        "--slug", "worktree-bootstrap",
        "--branch-name", "worktree-issue-1209-worktree-bootstrap",
        "--worktree-path", ".claude/worktrees/issue-1209-worktree-bootstrap",
        "--base-ref", "main",
        "--json",
    )
    assert result.returncode == 1, f"Expected blocked, got stdout={result.stdout}"
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "invalid_path"


def test_given_json_flag_omitted_when_bootstrap_runs_then_executor_blocks_with_invalid_args(tmp_path: Path) -> None:
    """B6: Omitting --json must return blocked/invalid_args."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)

    import subprocess
    result = subprocess.run(
        [sys.executable, str(SCRIPT),
         "--issue-number", "1209",
         "--slug", "worktree-bootstrap",
         "--branch-name", "worktree-issue-1209-worktree-bootstrap",
         "--worktree-path", ".claude/worktrees/issue-1209-worktree-bootstrap",
         "--base-ref", "main",
         # intentionally omit --json
         ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    import json
    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "invalid_args"
    assert any("--json" in e for e in payload["errors"])
