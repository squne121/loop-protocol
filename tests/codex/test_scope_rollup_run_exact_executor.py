"""
tests/codex/test_scope_rollup_run_exact_executor.py

Issue #1547 AC1/AC2/AC3: real hook evaluator (subprocess-equivalent, via the
same `evaluate()` entry point local_main_branch_guard.sh's Bash wrapper
invokes) tests for the `scope_rollup.run` exact command class.

AC1: canonical root (pre-worktree) `scope_rollup.run` exact invocation is
     allowed; raw `gh ... > /tmp/...` is still blocked in the same context.
AC2: unknown flag / duplicate flag / `--flag=value` / URL operand / wrapper /
     env prefix / shell metacharacter / non-trusted repository are all
     fail-closed.
AC3: linked issue worktree fixture -- context routing
     (`linked_issue_worktree_context` / `not_local_root`) happens BEFORE the
     `scope_rollup.run` classifier is ever reached.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))

from local_main_branch_guard import (  # noqa: E402
    REASON_LINKED_ISSUE_WORKTREE_CONTEXT,
    REASON_NOT_LOCAL_ROOT,
    REASON_UNPARSEABLE,
    evaluate,
)
from skill_runtime_command_policy import SCOPE_ROLLUP_RUN_REASON_CODE  # noqa: E402

SCOPE_ROLLUP_RUN_CMD = (
    "uv run python3 scripts/agent-guards/run_scope_rollup_preflight.py "
    "--issue-number 1547 --repo squne121/loop-protocol"
)


def eval_with_project_dir(command: str, cwd: str) -> dict:
    """Evaluate a Bash command with CLAUDE_PROJECT_DIR pinned to `cwd`\'s
    primary worktree ancestor (mirrors tests/codex/test_local_main_branch_guard.py\'s
    eval_codex helper)."""
    old = os.environ.get("CLAUDE_PROJECT_DIR", "")
    try:
        os.environ["CLAUDE_PROJECT_DIR"] = cwd
        return evaluate(command=command, cwd=cwd, hook_flavor="codex", event_kind="PreToolUse")
    finally:
        if old:
            os.environ["CLAUDE_PROJECT_DIR"] = old
        elif "CLAUDE_PROJECT_DIR" in os.environ:
            del os.environ["CLAUDE_PROJECT_DIR"]


@pytest.fixture
def tmp_git_repo() -> Path:
    """Temporary git repo on \'main\' branch with the trusted origin remote."""
    tmpdir = tempfile.mkdtemp(prefix="scope_rollup_codex_test_")
    try:
        subprocess.run(["git", "init", "-b", "main", tmpdir], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.email", "t@t.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.name", "T"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", tmpdir, "remote", "add", "origin", "https://github.com/squne121/loop-protocol.git"],
            check=True,
            capture_output=True,
        )
        (Path(tmpdir) / "README.md").write_text("test")
        subprocess.run(["git", "-C", tmpdir, "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "branch", "issue-1547-codex-test"], check=True, capture_output=True)
        yield Path(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def tmp_linked_worktree(tmp_git_repo: Path) -> Path:
    """Linked worktree under .claude/worktrees for context-routing tests."""
    wt_path = tmp_git_repo / ".claude" / "worktrees" / "issue-1547-codex-test"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(tmp_git_repo), "worktree", "add", str(wt_path), "issue-1547-codex-test"],
        check=True,
        capture_output=True,
    )
    try:
        yield wt_path
    finally:
        subprocess.run(
            ["git", "-C", str(tmp_git_repo), "worktree", "remove", "--force", str(wt_path)],
            capture_output=True,
        )


# --- AC1 ---------------------------------------------------------------------


def test_canonical_root_scope_rollup_run_is_allowed(tmp_git_repo: Path):
    result = eval_with_project_dir(SCOPE_ROLLUP_RUN_CMD, str(tmp_git_repo))
    assert result["status"] == "allow"
    assert result["reason_code"] == SCOPE_ROLLUP_RUN_REASON_CODE


def test_canonical_root_raw_gh_redirect_still_blocked(tmp_git_repo: Path):
    raw_redirect = "gh issue view 1547 --repo squne121/loop-protocol > /tmp/scope_rollup_1547.json"
    result = eval_with_project_dir(raw_redirect, str(tmp_git_repo))
    assert result["status"] == "block"
    assert result["reason_code"] == REASON_UNPARSEABLE


# --- AC2 ---------------------------------------------------------------------

UNSAFE_VARIANTS = [
    (
        "unknown_flag",
        "uv run python3 scripts/agent-guards/run_scope_rollup_preflight.py "
        "--issue-number 1547 --repo squne121/loop-protocol --extra-flag",
    ),
    (
        "duplicate_flag",
        "uv run python3 scripts/agent-guards/run_scope_rollup_preflight.py "
        "--issue-number 1547 --issue-number 1548 --repo squne121/loop-protocol",
    ),
    (
        "equals_form",
        "uv run python3 scripts/agent-guards/run_scope_rollup_preflight.py "
        "--issue-number=1547 --repo squne121/loop-protocol",
    ),
    (
        "url_operand",
        "uv run python3 scripts/agent-guards/run_scope_rollup_preflight.py "
        "--issue-number https://github.com/squne121/loop-protocol/issues/1547 "
        "--repo squne121/loop-protocol",
    ),
    (
        "bash_wrapper",
        "bash -lc \'uv run python3 scripts/agent-guards/run_scope_rollup_preflight.py "
        "--issue-number 1547 --repo squne121/loop-protocol\'",
    ),
    (
        "leading_env_assignment",
        "GH_HOST=evil.example uv run python3 scripts/agent-guards/run_scope_rollup_preflight.py "
        "--issue-number 1547 --repo squne121/loop-protocol",
    ),
    (
        "semicolon_chain",
        "uv run python3 scripts/agent-guards/run_scope_rollup_preflight.py "
        "--issue-number 1547 --repo squne121/loop-protocol; echo pwned",
    ),
    (
        "untrusted_repo",
        "uv run python3 scripts/agent-guards/run_scope_rollup_preflight.py "
        "--issue-number 1547 --repo attacker/evil-repo",
    ),
    (
        "missing_repo",
        "uv run python3 scripts/agent-guards/run_scope_rollup_preflight.py --issue-number 1547",
    ),
    (
        "near_miss_script_path",
        "uv run python3 /tmp/run_scope_rollup_preflight.py --issue-number 1547 --repo squne121/loop-protocol",
    ),
]


@pytest.mark.parametrize("case_id,command", UNSAFE_VARIANTS, ids=[c[0] for c in UNSAFE_VARIANTS])
def test_scope_rollup_run_rejects_unsafe_variants(tmp_git_repo: Path, case_id: str, command: str):
    result = eval_with_project_dir(command, str(tmp_git_repo))
    assert result["status"] == "block", f"{case_id} unexpectedly allowed: {result}"


# --- AC3 ---------------------------------------------------------------------


def test_linked_worktree_context_precedes_classifier(tmp_linked_worktree: Path, tmp_git_repo: Path):
    """In a linked issue worktree, context routing must fire before the
    scope_rollup.run classifier is ever reached -- both a safe exact
    invocation and an unsafe raw-redirect variant resolve via context
    routing, not via the classifier itself."""
    old = os.environ.get("CLAUDE_PROJECT_DIR", "")
    try:
        os.environ["CLAUDE_PROJECT_DIR"] = str(tmp_git_repo)
        safe_result = evaluate(
            command=SCOPE_ROLLUP_RUN_CMD,
            cwd=str(tmp_linked_worktree),
            hook_flavor="codex",
            event_kind="PreToolUse",
        )
        unsafe_result = evaluate(
            command="gh issue view 1547 --repo squne121/loop-protocol > /tmp/scope_rollup_1547.json",
            cwd=str(tmp_linked_worktree),
            hook_flavor="codex",
            event_kind="PreToolUse",
        )
    finally:
        if old:
            os.environ["CLAUDE_PROJECT_DIR"] = old
        elif "CLAUDE_PROJECT_DIR" in os.environ:
            del os.environ["CLAUDE_PROJECT_DIR"]

    for result in (safe_result, unsafe_result):
        assert result["status"] == "allow"
        assert result["reason_code"] in (
            REASON_LINKED_ISSUE_WORKTREE_CONTEXT,
            REASON_NOT_LOCAL_ROOT,
        )
        assert result["parser_stage"] == "context_check"
        assert result["rule_id"] == result["reason_code"]
