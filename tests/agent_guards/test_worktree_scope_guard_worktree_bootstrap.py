from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "worktree_scope_guard.sh"


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
def repo_with_other_issue_worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)
    _git("branch", "issue-942-sample", cwd=repo)
    issue_worktree = repo / ".claude" / "worktrees" / "issue-942-sample"
    issue_worktree.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", str(issue_worktree), "issue-942-sample", cwd=repo)
    return repo


def _run_guard(command: str, repo: Path, active_issue: str = "942") -> subprocess.CompletedProcess[str]:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(repo),
    }
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(repo),
        "LOOP_ISSUE_NUMBER": active_issue,
    }
    return subprocess.run(
        ["bash", str(GUARD_SH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_given_active_issue_worktree_when_canonical_bootstrap_command_runs_then_guard_allows(repo_with_other_issue_worktree: Path) -> None:
    command = (
        "uv run python3 scripts/agent-ops/worktree_bootstrap_exec.py "
        "--issue-number 1209 --slug worktree-bootstrap "
        "--branch-name worktree-issue-1209-worktree-bootstrap "
        "--worktree-path .claude/worktrees/issue-1209-worktree-bootstrap "
        "--base-ref main --json"
    )
    result = _run_guard(command, repo_with_other_issue_worktree)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_given_active_issue_worktree_when_rtk_wrapped_bootstrap_command_runs_then_guard_allows(repo_with_other_issue_worktree: Path) -> None:
    command = (
        "rtk uv run python3 scripts/agent-ops/worktree_bootstrap_exec.py "
        "--issue-number 1209 --slug worktree-bootstrap "
        "--branch-name worktree-issue-1209-worktree-bootstrap "
        "--worktree-path .claude/worktrees/issue-1209-worktree-bootstrap "
        "--base-ref main --json"
    )
    result = _run_guard(command, repo_with_other_issue_worktree)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_given_active_issue_worktree_when_raw_git_worktree_add_runs_then_guard_blocks(repo_with_other_issue_worktree: Path) -> None:
    command = (
        "git worktree add .claude/worktrees/issue-1209-worktree-bootstrap "
        "-b worktree-issue-1209-worktree-bootstrap main"
    )
    result = _run_guard(command, repo_with_other_issue_worktree)
    assert result.returncode == 2
    assert "blocked" in result.stderr


# ---------------------------------------------------------------------------
# B5: Negative fixtures — commands that scope guard must block
# ---------------------------------------------------------------------------

_CANONICAL_INNER = (
    "uv run python3 scripts/agent-ops/worktree_bootstrap_exec.py "
    "--issue-number 1209 --slug worktree-bootstrap "
    "--branch-name worktree-issue-1209-worktree-bootstrap "
    "--worktree-path .claude/worktrees/issue-1209-worktree-bootstrap "
    "--base-ref main --json"
)


def test_given_bash_lc_wrapper_when_guard_runs_then_it_is_blocked(repo_with_other_issue_worktree: Path) -> None:
    """B5: bash -lc 'uv run python3 ...' must be blocked by scope guard."""
    command = "bash -lc '" + _CANONICAL_INNER + "'"
    result = _run_guard(command, repo_with_other_issue_worktree)
    assert result.returncode == 2, f"Expected block (exit 2), got {result.returncode}\nstderr={result.stderr}"


def test_given_env_prefix_when_guard_runs_then_it_is_blocked(repo_with_other_issue_worktree: Path) -> None:
    """B5: env FOO=bar uv run python3 ... must be blocked by scope guard."""
    command = "env FOO=bar " + _CANONICAL_INNER
    result = _run_guard(command, repo_with_other_issue_worktree)
    assert result.returncode == 2, f"Expected block (exit 2), got {result.returncode}\nstderr={result.stderr}"


def test_given_duplicate_json_flag_when_guard_runs_then_it_is_blocked(repo_with_other_issue_worktree: Path) -> None:
    """B5: uv run python3 ... --json --json must be blocked by scope guard (not exact match)."""
    base = (
        "uv run python3 scripts/agent-ops/worktree_bootstrap_exec.py "
        "--issue-number 1209 --slug worktree-bootstrap "
        "--branch-name worktree-issue-1209-worktree-bootstrap "
        "--worktree-path .claude/worktrees/issue-1209-worktree-bootstrap "
        "--base-ref main"
    )
    command = base + " --json --json"
    result = _run_guard(command, repo_with_other_issue_worktree)
    assert result.returncode == 2, f"Expected block (exit 2), got {result.returncode}\nstderr={result.stderr}"


def test_given_extra_positional_arg_when_guard_runs_then_it_is_blocked(repo_with_other_issue_worktree: Path) -> None:
    """B5: uv run python3 ... --json EXTRA must be blocked by scope guard."""
    command = _CANONICAL_INNER + " EXTRA"
    result = _run_guard(command, repo_with_other_issue_worktree)
    assert result.returncode == 2, f"Expected block (exit 2), got {result.returncode}\nstderr={result.stderr}"
