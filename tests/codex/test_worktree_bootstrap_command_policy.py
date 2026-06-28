from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))

from local_main_branch_guard import (  # noqa: E402
    REASON_RTK_PROXY,
    REASON_UNKNOWN_COMMAND,
    REASON_WORKTREE_BOOTSTRAP_EXECUTOR,
    evaluate,
)
from worktree_bootstrap_command_policy import (  # noqa: E402
    is_exact_worktree_bootstrap_executor_command,
    parse_exact_worktree_bootstrap_command,
)


@pytest.fixture()
def tmp_git_repo() -> Path:
    tmpdir = tempfile.mkdtemp(prefix="worktree_bootstrap_policy_")
    repo = Path(tmpdir)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/squne121/loop-protocol.git"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-m", "seed"], check=True, capture_output=True, env=env)
    try:
        yield repo
    finally:
        subprocess.run(["rm", "-rf", str(repo)], check=False, capture_output=True)


def _eval(command: str, cwd: Path) -> dict[str, object]:
    old = os.environ.get("CLAUDE_PROJECT_DIR", "")
    try:
        os.environ["CLAUDE_PROJECT_DIR"] = str(cwd)
        return evaluate(command=command, cwd=str(cwd), hook_flavor="codex")
    finally:
        if old:
            os.environ["CLAUDE_PROJECT_DIR"] = old
        else:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)


def test_given_canonical_command_when_policy_parses_then_exact_command_matches(tmp_git_repo: Path) -> None:
    command = (
        "uv run python3 scripts/agent-ops/worktree_bootstrap_exec.py "
        "--issue-number 1209 --slug worktree-bootstrap "
        "--branch-name worktree-issue-1209-worktree-bootstrap "
        "--worktree-path .claude/worktrees/issue-1209-worktree-bootstrap "
        "--base-ref main --json"
    )
    parsed = parse_exact_worktree_bootstrap_command(command, str(tmp_git_repo))
    assert parsed is not None
    assert parsed.wrapper is None
    assert is_exact_worktree_bootstrap_executor_command(command, str(tmp_git_repo), str(tmp_git_repo))
    result = _eval(command, tmp_git_repo)
    assert result["status"] == "allow"
    assert result["reason_code"] == REASON_WORKTREE_BOOTSTRAP_EXECUTOR


def test_given_rtk_wrapped_command_when_policy_parses_then_inner_command_is_allowed(tmp_git_repo: Path) -> None:
    command = (
        "rtk uv run python3 scripts/agent-ops/worktree_bootstrap_exec.py "
        "--issue-number 1209 --slug worktree-bootstrap "
        "--branch-name worktree-issue-1209-worktree-bootstrap "
        "--worktree-path .claude/worktrees/issue-1209-worktree-bootstrap "
        "--base-ref main --json"
    )
    parsed = parse_exact_worktree_bootstrap_command(command, str(tmp_git_repo))
    assert parsed is not None
    assert parsed.wrapper == "rtk"
    result = _eval(command, tmp_git_repo)
    assert result["status"] == "allow"
    assert result["reason_code"] == REASON_WORKTREE_BOOTSTRAP_EXECUTOR


def test_given_rtk_proxy_command_when_evaluated_then_proxy_is_still_blocked(tmp_git_repo: Path) -> None:
    command = (
        "rtk proxy uv run python3 scripts/agent-ops/worktree_bootstrap_exec.py "
        "--issue-number 1209 --slug worktree-bootstrap "
        "--branch-name worktree-issue-1209-worktree-bootstrap "
        "--worktree-path .claude/worktrees/issue-1209-worktree-bootstrap "
        "--base-ref main --json"
    )
    result = _eval(command, tmp_git_repo)
    assert result["status"] == "block"
    assert result["reason_code"] == REASON_RTK_PROXY


def test_given_raw_git_worktree_add_when_evaluated_then_command_remains_blocked(tmp_git_repo: Path) -> None:
    command = (
        "rtk git worktree add .claude/worktrees/issue-1209-worktree-bootstrap "
        "-b worktree-issue-1209-worktree-bootstrap main"
    )
    result = _eval(command, tmp_git_repo)
    assert result["status"] == "block"
    assert result["reason_code"] == REASON_UNKNOWN_COMMAND


def test_given_flag_value_form_when_policy_parses_then_command_is_rejected(tmp_git_repo: Path) -> None:
    command = (
        "uv run python3 scripts/agent-ops/worktree_bootstrap_exec.py "
        "--issue-number=1209 --slug worktree-bootstrap "
        "--branch-name worktree-issue-1209-worktree-bootstrap "
        "--worktree-path .claude/worktrees/issue-1209-worktree-bootstrap "
        "--base-ref main --json"
    )
    assert parse_exact_worktree_bootstrap_command(command, str(tmp_git_repo)) is None


# ---------------------------------------------------------------------------
# B5: Negative fixtures — commands that must be blocked
# ---------------------------------------------------------------------------

_CANONICAL_INNER = (
    "scripts/agent-ops/worktree_bootstrap_exec.py "
    "--issue-number 1209 --slug worktree-bootstrap "
    "--branch-name worktree-issue-1209-worktree-bootstrap "
    "--worktree-path .claude/worktrees/issue-1209-worktree-bootstrap "
    "--base-ref main --json"
)


def test_given_bash_lc_wrapper_when_evaluated_then_command_is_blocked(tmp_git_repo: Path) -> None:
    """B5: bash -lc 'uv run python3 ...' must be blocked — shell escape via -c is not allowed."""
    command = f"bash -lc \'uv run python3 {_CANONICAL_INNER}\'"
    result = _eval(command, tmp_git_repo)
    assert result["status"] == "block", f"Expected block, got: {result}"


def test_given_rtk_bash_lc_wrapper_when_evaluated_then_command_is_blocked(tmp_git_repo: Path) -> None:
    """B5: rtk bash -lc '...' must be blocked."""
    command = f"rtk bash -lc \'uv run python3 {_CANONICAL_INNER}\'"
    result = _eval(command, tmp_git_repo)
    assert result["status"] == "block", f"Expected block, got: {result}"


def test_given_leading_env_assignment_when_evaluated_then_command_is_blocked(tmp_git_repo: Path) -> None:
    """B5: env FOO=bar uv run python3 ... must be blocked — leading env vars not allowed."""
    command = f"env FOO=bar uv run python3 {_CANONICAL_INNER}"
    result = _eval(command, tmp_git_repo)
    assert result["status"] == "block", f"Expected block, got: {result}"


def test_given_duplicate_json_flag_when_parsed_then_command_is_rejected(tmp_git_repo: Path) -> None:
    """B5: Duplicate --json flag must be rejected by the policy parser."""
    base = "scripts/agent-ops/worktree_bootstrap_exec.py --issue-number 1209 --slug worktree-bootstrap --branch-name worktree-issue-1209-worktree-bootstrap --worktree-path .claude/worktrees/issue-1209-worktree-bootstrap --base-ref main"
    command = f"uv run python3 {base} --json --json"
    parsed = parse_exact_worktree_bootstrap_command(command, str(tmp_git_repo))
    assert parsed is None, "Duplicate --json must not parse"


def test_given_extra_positional_arg_when_parsed_then_command_is_rejected(tmp_git_repo: Path) -> None:
    """B5: Extra positional argument after --json must be rejected."""
    command = f"uv run python3 {_CANONICAL_INNER} EXTRA"
    parsed = parse_exact_worktree_bootstrap_command(command, str(tmp_git_repo))
    assert parsed is None, "Extra positional must not parse"


def test_given_rtk_with_unknown_flag_when_evaluated_then_command_is_blocked(tmp_git_repo: Path) -> None:
    """B5: rtk uv run python3 ... --unknown must be blocked."""
    base = "scripts/agent-ops/worktree_bootstrap_exec.py --issue-number 1209 --slug worktree-bootstrap --branch-name worktree-issue-1209-worktree-bootstrap --worktree-path .claude/worktrees/issue-1209-worktree-bootstrap --base-ref main --json"
    command = f"rtk uv run python3 {base} --unknown"
    result = _eval(command, tmp_git_repo)
    assert result["status"] == "block", f"Expected block, got: {result}"
