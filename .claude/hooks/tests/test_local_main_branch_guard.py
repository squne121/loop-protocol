"""
test_local_main_branch_guard.py

Tests for local_main_branch_guard.py (Claude Code integration).
Covers AC1–AC7, AC9–AC16.

All tests use subprocess fixtures with temporary git repos to test
the actual guard logic in isolation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path
from typing import Generator

import pytest

# Add scripts/agent-guards to path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))

from local_main_branch_guard import (
    evaluate,
    is_local_root_context,
    get_current_branch,
    resolve_default_branch,
    classify_branch,
    classify_root_state,
    has_inline_env_override,
    is_manual_override_active,
    is_readonly_command,
    is_path_restore_command,
    is_compound_or_wrapped,
    _is_branch_mutation_command,
    _extract_target_branch,
    _is_blocked_when_drifted,
    _has_leading_env_assignment,
    _normalize_git_global_opts,
    _is_allowed_when_drifted,
    REASON_NOT_LOCAL_ROOT,
    REASON_READONLY,
    REASON_RECOVERY,
    REASON_DRIFT,
    REASON_ALREADY_DRIFTED,
    REASON_DETACHED_OR_UNKNOWN,
    REASON_UNPARSEABLE,
    REASON_INLINE_OVERRIDE,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_git_repo() -> Generator[Path, None, None]:
    """Create a temporary git repo on 'main' branch with one commit."""
    tmpdir = tempfile.mkdtemp(prefix="lmbg_test_")
    try:
        subprocess.run(["git", "init", "-b", "main", tmpdir], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.email", "t@t.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.name", "T"], check=True, capture_output=True)
        (Path(tmpdir) / "README.md").write_text("test")
        subprocess.run(["git", "-C", tmpdir, "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "commit", "-m", "init"], check=True, capture_output=True)
        # Create a branch to switch to (but stay on main)
        subprocess.run(["git", "-C", tmpdir, "branch", "issue-981-test"], check=True, capture_output=True)
        yield Path(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def tmp_git_repo_drifted(tmp_git_repo: Path) -> Path:
    """A repo where local root is already on a non-default branch."""
    subprocess.run(
        ["git", "-C", str(tmp_git_repo), "switch", "issue-981-test"],
        check=True, capture_output=True
    )
    return tmp_git_repo


@pytest.fixture
def tmp_linked_worktree(tmp_git_repo: Path) -> Generator[Path, None, None]:
    """Create a linked worktree under .claude/worktrees/issue-981-test/."""
    wt_path = tmp_git_repo / ".claude" / "worktrees" / "issue-981-test"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(tmp_git_repo), "worktree", "add",
         str(wt_path), "issue-981-test"],
        check=True, capture_output=True
    )
    yield wt_path
    # cleanup: remove worktree
    subprocess.run(
        ["git", "-C", str(tmp_git_repo), "worktree", "remove", "--force", str(wt_path)],
        capture_output=True
    )


def make_pretool_input(command: str, cwd: str) -> str:
    """Build a minimal PreToolUse JSON payload."""
    return json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": cwd,
    })


def eval_in_local_root(command: str, cwd: str, env_override: dict | None = None) -> dict:
    """
    Call evaluate() with CLAUDE_PROJECT_DIR set to cwd so that
    is_local_root_context() correctly identifies cwd as primary root.
    """
    import os
    old_env = os.environ.copy()
    try:
        os.environ["CLAUDE_PROJECT_DIR"] = cwd
        if env_override:
            for k, v in env_override.items():
                os.environ[k] = v
        result = evaluate(command=command, cwd=cwd, hook_flavor="claude")
    finally:
        # Restore env
        for k in list(os.environ.keys()):
            if k not in old_env:
                del os.environ[k]
            else:
                os.environ[k] = old_env[k]
    return result


# ─── AC1: Claude Code blocks root branch drift ────────────────────────────────

class TestAC1BlockRootBranchDrift:
    """AC1: Claude Code blocks root branch drift."""

    def test_git_switch_issue_branch_is_blocked(self, tmp_git_repo: Path):
        result = eval_in_local_root("git switch issue-981-test", str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_DRIFT

    def test_git_checkout_issue_branch_is_blocked(self, tmp_git_repo: Path):
        result = eval_in_local_root("git checkout issue-981-test", str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_DRIFT

    def test_git_checkout_b_issue_branch_is_blocked(self, tmp_git_repo: Path):
        result = eval_in_local_root("git checkout -b issue-999-new", str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_DRIFT

    def test_gh_pr_checkout_is_blocked(self, tmp_git_repo: Path):
        result = eval_in_local_root("gh pr checkout 988", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_bash_lc_git_switch_is_blocked(self, tmp_git_repo: Path):
        """bash -lc 'git switch issue-*' must be blocked as unparseable compound."""
        result = eval_in_local_root("bash -lc 'git switch issue-981-test'", str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_UNPARSEABLE

    def test_target_branch_kind_is_issue(self, tmp_git_repo: Path):
        result = eval_in_local_root("git switch issue-981-test", str(tmp_git_repo))
        assert result["target_branch_kind"] in ("issue", "worktree_issue", "unknown")


# ─── AC2: Claude Code allows recovery to default branch ───────────────────────

class TestAC2AllowRecovery:
    """AC2: Claude Code allows recovery to default branch."""

    def test_git_switch_main_is_allowed(self, tmp_git_repo_drifted: Path):
        result = eval_in_local_root("git switch main", str(tmp_git_repo_drifted))
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_RECOVERY

    def test_git_checkout_main_is_allowed(self, tmp_git_repo_drifted: Path):
        result = eval_in_local_root("git checkout main", str(tmp_git_repo_drifted))
        assert result["status"] == "allow"

    def test_git_status_is_allowed(self, tmp_git_repo: Path):
        result = eval_in_local_root("git status", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_git_branch_show_current_is_allowed(self, tmp_git_repo: Path):
        result = eval_in_local_root("git branch --show-current", str(tmp_git_repo))
        assert result["status"] == "allow"


# ─── AC3: Issue worktree does not trigger this guard ──────────────────────────

class TestAC3LinkedWorktreeBypass:
    """AC3: Inside linked worktree, guard allows and defers to worktree_scope_guard."""

    def test_linked_worktree_git_switch_is_allowed(self, tmp_linked_worktree: Path):
        """In a linked worktree, branch commands are allowed (not local root context)."""
        import os
        old = os.environ.get("CLAUDE_PROJECT_DIR", "")
        try:
            # Do NOT set CLAUDE_PROJECT_DIR to the linked worktree path
            # (it should be the primary root, but cwd != primary root)
            if "CLAUDE_PROJECT_DIR" in os.environ:
                del os.environ["CLAUDE_PROJECT_DIR"]
            result = evaluate(
                command="git switch main",
                cwd=str(tmp_linked_worktree),
                hook_flavor="claude",
            )
            # Should be allow because cwd != primary root
            assert result["status"] == "allow"
            assert result["reason_code"] == REASON_NOT_LOCAL_ROOT
        finally:
            if old:
                os.environ["CLAUDE_PROJECT_DIR"] = old


# ─── AC4: git checkout -- <path> is not overblocked ─────────────────────────

class TestAC4PathRestoreNotBlocked:
    """AC4: git checkout -- <path> and variants are not blocked as branch switches."""

    def test_git_checkout_double_dash_path(self, tmp_git_repo: Path):
        result = eval_in_local_root("git checkout -- README.md", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_git_checkout_head_double_dash_path(self, tmp_git_repo: Path):
        result = eval_in_local_root("git checkout HEAD -- README.md", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_git_restore_path(self, tmp_git_repo: Path):
        result = eval_in_local_root("git restore README.md", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_git_checkout_pathspec_from_file(self, tmp_git_repo: Path):
        result = eval_in_local_root("git checkout --pathspec-from-file=files.txt", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_git_checkout_patch_flag(self, tmp_git_repo: Path):
        result = eval_in_local_root("git checkout -p -- README.md", str(tmp_git_repo))
        assert result["status"] == "allow"


# ─── AC5: bounded stderr / no leak ───────────────────────────────────────────

class TestAC5BoundedStderr:
    """AC5: block stderr is bounded (max 10 lines) and has no leak."""

    def test_block_stderr_max_10_lines(self, tmp_git_repo: Path, capsys):
        """Verify that block message goes to stderr and is bounded."""
        from local_main_branch_guard import _emit_block_stderr, REASON_DRIFT
        _emit_block_stderr(
            reason_code=REASON_DRIFT,
            current_branch_kind="default",
            current_is_default=True,
            target_branch_kind="issue",
            hook_flavor="claude",
        )
        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if l.strip()]
        assert len(lines) <= 10

    def test_block_stderr_no_raw_command(self, tmp_git_repo: Path, capsys):
        """Block stderr must not contain the raw command."""
        from local_main_branch_guard import _emit_block_stderr, REASON_DRIFT
        _emit_block_stderr(
            reason_code=REASON_DRIFT,
            current_branch_kind="default",
            current_is_default=True,
            target_branch_kind="issue",
            hook_flavor="claude",
        )
        captured = capsys.readouterr()
        # Should not dump any tool_input JSON or raw command
        assert "tool_input" not in captured.err
        assert "git switch" not in captured.err
        # stdout must be empty on block
        assert captured.out == ""


# ─── AC6: hook order is enforced ─────────────────────────────────────────────

class TestAC6HookOrder:
    """AC6: secret_boundary_guard before local_main_branch_guard before worktree_scope_guard."""

    def test_hook_order_in_settings_json(self):
        """Verify PreToolUse hook order in .claude/settings.json."""
        settings_path = REPO_ROOT / ".claude" / "settings.json"
        if not settings_path.exists():
            pytest.skip(".claude/settings.json not found")
        settings = json.loads(settings_path.read_text())
        pre_tool_use = settings.get("hooks", {}).get("PreToolUse", [])

        # Build ordered list of handler script names
        order = []
        for entry in pre_tool_use:
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                name = Path(cmd).stem
                order.append(name)

        assert "secret_boundary_guard" in order, "secret_boundary_guard must be in PreToolUse"
        assert "local_main_branch_guard" in order, "local_main_branch_guard must be in PreToolUse"
        assert "worktree_scope_guard" in order, "worktree_scope_guard must be in PreToolUse"

        idx_secret = order.index("secret_boundary_guard")
        idx_local = order.index("local_main_branch_guard")
        idx_worktree = order.index("worktree_scope_guard")

        assert idx_secret < idx_local, (
            f"secret_boundary_guard (index {idx_secret}) must come before "
            f"local_main_branch_guard (index {idx_local})"
        )
        assert idx_local < idx_worktree, (
            f"local_main_branch_guard (index {idx_local}) must come before "
            f"worktree_scope_guard (index {idx_worktree})"
        )


# ─── AC7: hook-boundaries manifest drift check ───────────────────────────────

class TestAC7HookBoundariesManifest:
    """AC7: docs/dev/hook-boundaries.md contains local_main_branch_guard entry."""

    def test_hook_boundaries_contains_local_main(self):
        """hook-boundaries.md must list local_main_branch_guard."""
        hb_path = REPO_ROOT / "docs" / "dev" / "hook-boundaries.md"
        if not hb_path.exists():
            pytest.skip("hook-boundaries.md not found")
        content = hb_path.read_text()
        assert "local_main_branch_guard" in content, (
            "hook-boundaries.md must include local_main_branch_guard"
        )

    def test_check_hook_boundaries_passes(self):
        """scripts/check_hook_boundaries.py must pass with no drift."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check_hook_boundaries.py")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"check_hook_boundaries.py failed:\n{result.stderr}\n{result.stdout}"
        )


# ─── AC9: startup preflight ──────────────────────────────────────────────────

class TestAC9StartupPreflight:
    """AC9: check_local_main_branch_state.py exits non-zero when drifted."""

    def test_preflight_passes_on_main(self, tmp_git_repo: Path):
        """Preflight exits 0 when root is on default branch."""
        result = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "check_local_main_branch_state.py"),
             "--json", "--cwd", str(tmp_git_repo)],
            capture_output=True,
            text=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_git_repo)},
        )
        data = json.loads(result.stdout)
        state = data["LOCAL_MAIN_BRANCH_STATE_RESULT_V1"]
        # Either ok (local root on main) or not local root
        assert state["status"] in ("ok", "unknown")

    def test_preflight_fails_when_drifted(self, tmp_git_repo_drifted: Path):
        """Preflight exits non-zero when root is drifted."""
        result = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "check_local_main_branch_state.py"),
             "--json", "--cwd", str(tmp_git_repo_drifted)],
            capture_output=True,
            text=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_git_repo_drifted)},
        )
        data = json.loads(result.stdout)
        state = data["LOCAL_MAIN_BRANCH_STATE_RESULT_V1"]
        # Should be drifted, and exit non-zero
        if state["is_local_root"]:
            assert state["status"] == "drifted"
            assert result.returncode != 0


# ─── AC10: branch mutation grammar is complete ────────────────────────────────

class TestAC10BranchMutationGrammar:
    """AC10: Block git switch -C, --detach, --orphan, --guess; checkout -B, --detach, etc."""

    @pytest.mark.parametrize("cmd", [
        "git switch -C issue-999",
        "git switch --detach HEAD",
        "git switch --orphan orphan-branch",
        "git switch --guess origin/issue-999",
        "git checkout -B issue-999",
        "git checkout --detach HEAD",
        "git checkout --orphan orphan-branch",
        "git checkout --ignore-other-worktrees issue-999",
        "git branch -m issue-999",
        "git branch -M issue-999",
    ])
    def test_blocked_branch_mutations(self, tmp_git_repo: Path, cmd: str):
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block", f"Expected block for: {cmd!r}"


# ─── AC11: git checkout path restore forms not overblocked ───────────────────

class TestAC11PathRestoreNotOverblocked:
    """AC11: git checkout HEAD -- path, --pathspec-from-file, -p are not blocked."""

    @pytest.mark.parametrize("cmd", [
        "git checkout HEAD -- README.md",
        "git checkout --pathspec-from-file=files.txt",
        "git checkout -p -- README.md",
    ])
    def test_allowed_path_restore(self, tmp_git_repo: Path, cmd: str):
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "allow", f"Expected allow for: {cmd!r}"


# ─── AC12: already-drifted state uses explicit allowlist ─────────────────────

class TestAC12AlreadyDriftedAllowlist:
    """AC12: mutating gh commands are blocked in already-drifted state."""

    @pytest.mark.parametrize("cmd", [
        "gh issue edit 123 --title new",
        "gh issue comment 123 --body text",
        "gh issue close 123",
        "gh pr checkout 988",
        "gh pr edit 988 --title new",
        "gh pr comment 988 --body text",
        "gh pr merge 988",
        "gh pr update-branch 988",
        "hub pr checkout 988",
        "uv run pytest",
        "pnpm test",
        "npm test",
    ])
    def test_blocked_when_drifted(self, tmp_git_repo_drifted: Path, cmd: str):
        result = eval_in_local_root(cmd, str(tmp_git_repo_drifted))
        assert result["status"] == "block", f"Expected block for drifted root: {cmd!r}"

    @pytest.mark.parametrize("cmd", [
        "gh issue view 123",
        "gh issue list",
        "gh pr view 988",
        "gh pr list",
        "gh pr status",
        "git status",
        "git branch --show-current",
    ])
    def test_allowed_readonly_when_drifted(self, tmp_git_repo_drifted: Path, cmd: str):
        result = eval_in_local_root(cmd, str(tmp_git_repo_drifted))
        assert result["status"] == "allow", f"Expected allow for readonly when drifted: {cmd!r}"


# ─── AC13: project root detection uses worktree catalog ──────────────────────

class TestAC13ProjectRootDetection:
    """AC13: is_local_root_context uses git worktree list catalog, not rev-parse alone."""

    def test_linked_worktree_is_not_local_root(self, tmp_linked_worktree: Path, tmp_git_repo: Path):
        """A linked worktree cwd must NOT be identified as local root context."""
        import os
        # Without CLAUDE_PROJECT_DIR, use worktree catalog
        old = os.environ.pop("CLAUDE_PROJECT_DIR", None)
        try:
            result = is_local_root_context(str(tmp_linked_worktree))
            # Linked worktree must not be local root
            assert result is False, "Linked worktree should not be local root context"
        finally:
            if old is not None:
                os.environ["CLAUDE_PROJECT_DIR"] = old

    def test_primary_root_is_local_root(self, tmp_git_repo: Path):
        """Primary worktree root with CLAUDE_PROJECT_DIR is identified as local root."""
        import os
        old = os.environ.get("CLAUDE_PROJECT_DIR", "")
        os.environ["CLAUDE_PROJECT_DIR"] = str(tmp_git_repo)
        try:
            result = is_local_root_context(str(tmp_git_repo))
            assert result is True, "Primary root should be local root context"
        finally:
            if old:
                os.environ["CLAUDE_PROJECT_DIR"] = old
            elif "CLAUDE_PROJECT_DIR" in os.environ:
                del os.environ["CLAUDE_PROJECT_DIR"]


# ─── AC14: default branch resolution ─────────────────────────────────────────

class TestAC14DefaultBranchResolution:
    """AC14: default branch resolution priority: LOOP_DEFAULT_BRANCH > origin/HEAD > main."""

    def test_loop_default_branch_env_takes_priority(self, tmp_git_repo: Path):
        import os
        old = os.environ.get("LOOP_DEFAULT_BRANCH", "")
        os.environ["LOOP_DEFAULT_BRANCH"] = "trunk"
        try:
            default = resolve_default_branch(cwd=str(tmp_git_repo))
            assert default == "trunk"
        finally:
            if old:
                os.environ["LOOP_DEFAULT_BRANCH"] = old
            elif "LOOP_DEFAULT_BRANCH" in os.environ:
                del os.environ["LOOP_DEFAULT_BRANCH"]

    def test_fallback_to_main(self, tmp_git_repo: Path):
        import os
        old = os.environ.pop("LOOP_DEFAULT_BRANCH", None)
        try:
            default = resolve_default_branch(cwd=str(tmp_git_repo))
            # No origin/HEAD set, so should fall back to main
            assert default == "main"
        finally:
            if old is not None:
                os.environ["LOOP_DEFAULT_BRANCH"] = old

    def test_classify_default_branch(self):
        assert classify_branch("main", "main") == "default"

    def test_classify_issue_branch(self):
        assert classify_branch("issue-981-test", "main") == "issue"

    def test_classify_worktree_issue_branch(self):
        assert classify_branch("worktree-issue-981-test", "main") == "worktree_issue"

    def test_classify_pr_branch(self):
        assert classify_branch("pr/988", "main") == "pr"

    def test_classify_unknown_branch(self):
        assert classify_branch("feature/something", "main") == "unknown"


# ─── AC15: inline env override in Bash command is blocked ────────────────────

class TestAC15InlineEnvOverrideBlocked:
    """AC15: LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE=1 in command string is blocked."""

    def test_inline_env_override_is_blocked(self, tmp_git_repo: Path):
        """Inline env override within the command string must be blocked."""
        cmd = "LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE=1 git switch issue-981-test"
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_INLINE_OVERRIDE

    def test_has_inline_env_override_detects(self):
        cmd = "LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE=1 git switch issue-*"
        assert has_inline_env_override(cmd) is True

    def test_has_inline_env_override_normal_cmd(self):
        cmd = "git switch main"
        assert has_inline_env_override(cmd) is False

    def test_hook_process_env_allows_override(self, tmp_git_repo: Path):
        """Hook process env with both vars set allows override."""
        result = eval_in_local_root(
            "git switch issue-981-test",
            str(tmp_git_repo),
            env_override={
                "LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE": "1",
                "LOOP_LOCAL_ROOT_BRANCH_CHANGE_REASON": "manual recovery: test",
            },
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == "manual_override_accepted"

    def test_hook_process_env_missing_reason_blocks(self, tmp_git_repo: Path):
        """Override without reason should NOT be accepted."""
        result = eval_in_local_root(
            "git switch issue-981-test",
            str(tmp_git_repo),
            env_override={
                "LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE": "1",
                # Missing LOOP_LOCAL_ROOT_BRANCH_CHANGE_REASON
            },
        )
        assert result["status"] == "block"


# ─── AC16: unparseable compound commands are fail-closed ─────────────────────

class TestAC16UnparseableCompoundCommands:
    """AC16: compound shell commands that may mutate local root are fail-closed."""

    @pytest.mark.parametrize("cmd", [
        "cd /repo && git checkout issue-981-test",
        "command git switch issue-981-test",
        "env FOO=bar git switch issue-981-test",
        "bash -c 'git switch issue-981-test'",
        "sh -c 'git checkout issue-981-test'",
        "git status; git switch issue-981-test",
        "git status || git switch issue-981-test",
    ])
    def test_compound_commands_are_fail_closed(self, tmp_git_repo: Path, cmd: str):
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block", (
            f"Expected block (fail-closed) for compound command: {cmd!r}"
        )

    def test_is_compound_or_wrapped(self):
        assert is_compound_or_wrapped("cd repo && git checkout issue-*") is True
        assert is_compound_or_wrapped("command git switch issue-*") is True
        assert is_compound_or_wrapped("env FOO=bar git switch issue-*") is True
        assert is_compound_or_wrapped("bash -c 'git switch issue-*'") is True
        assert is_compound_or_wrapped("git status") is False
        assert is_compound_or_wrapped("git switch main") is False


# ─── B1: cwd subdirectory of primary worktree is local root context ──────────

def eval_in_local_root_subdir(command: str, repo_root: str, cwd: str, env_override: dict | None = None) -> dict:
    """
    Like eval_in_local_root but sets CLAUDE_PROJECT_DIR to repo_root (not cwd).
    Used for B1 tests where cwd is a subdirectory of the primary worktree.
    """
    import os
    old_env = os.environ.copy()
    try:
        os.environ["CLAUDE_PROJECT_DIR"] = repo_root
        if env_override:
            for k, v in env_override.items():
                os.environ[k] = v
        result = evaluate(command=command, cwd=cwd, hook_flavor="claude")
    finally:
        for k in list(os.environ.keys()):
            if k not in old_env:
                del os.environ[k]
            else:
                os.environ[k] = old_env[k]
    return result


class TestB1SubdirIsLocalRootContext:
    """B1: cwd in scripts/ or docs/ under primary worktree should be blocked."""

    def test_subdir_scripts_is_blocked(self, tmp_git_repo: Path):
        """cwd=<repo>/scripts + git switch issue-* => block."""
        scripts_dir = tmp_git_repo / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        result = eval_in_local_root_subdir(
            "git switch issue-981-test",
            repo_root=str(tmp_git_repo),
            cwd=str(scripts_dir),
        )
        assert result["status"] == "block", (
            "Expected block from scripts/ subdirectory of primary worktree"
        )

    def test_subdir_docs_is_blocked(self, tmp_git_repo: Path):
        """cwd=<repo>/docs + git checkout issue-* => block."""
        docs_dir = tmp_git_repo / "docs"
        docs_dir.mkdir(exist_ok=True)
        result = eval_in_local_root_subdir(
            "git checkout issue-981-test",
            repo_root=str(tmp_git_repo),
            cwd=str(docs_dir),
        )
        assert result["status"] == "block", (
            "Expected block from docs/ subdirectory of primary worktree"
        )

    def test_linked_worktree_subdir_is_not_blocked(self, tmp_linked_worktree: Path):
        """cwd inside linked worktree => not local root context => allow."""
        result = eval_in_local_root(
            "git switch main",
            str(tmp_linked_worktree),
        )
        # The guard should allow because linked worktree != primary root
        assert result["status"] == "allow", (
            "Linked worktree cwd must not be treated as local root context"
        )


# ─── B2: git -C <path> global option bypass ──────────────────────────────────

class TestB2GitGlobalOptionBypass:
    """B2: git -C . switch / git -C scripts checkout => block (fail-closed)."""

    def test_git_dash_C_switch_is_blocked(self, tmp_git_repo: Path):
        """git -C . switch issue-1014-x => block."""
        result = eval_in_local_root(
            "git -C . switch issue-981-test",
            str(tmp_git_repo),
        )
        assert result["status"] == "block", (
            "git -C . switch must be blocked (git global option bypass)"
        )

    def test_git_dash_C_scripts_checkout_is_blocked(self, tmp_git_repo: Path):
        """git -C scripts checkout issue-1014-x => block."""
        result = eval_in_local_root(
            "git -C scripts checkout issue-981-test",
            str(tmp_git_repo),
        )
        assert result["status"] == "block", (
            "git -C scripts checkout must be blocked (git global option bypass)"
        )

    def test_git_c_advice_switch_detach_is_blocked(self, tmp_git_repo: Path):
        """git -c advice.detachedHead=false switch --detach HEAD~1 => block."""
        result = eval_in_local_root(
            "git -c advice.detachedHead=false switch --detach HEAD~1",
            str(tmp_git_repo),
        )
        assert result["status"] == "block", (
            "git -c <config> switch --detach must be blocked"
        )

    def test_normalize_git_global_opts_failclosed(self):
        """_normalize_git_global_opts detects -C and sets fail_closed=True."""
        tokens = ["git", "-C", ".", "switch", "issue-981"]
        _, fail_closed = _normalize_git_global_opts(tokens)
        assert fail_closed is True

    def test_normalize_git_global_opts_regular(self):
        """_normalize_git_global_opts: plain git switch has no fail-closed opts."""
        tokens = ["git", "switch", "issue-981"]
        remaining, fail_closed = _normalize_git_global_opts(tokens)
        assert fail_closed is False
        assert remaining == ["git", "switch", "issue-981"]


# ─── B3: leading NAME=value env assignment bypass ────────────────────────────

class TestB3LeadingEnvAssignmentBypass:
    """B3: FOO=bar git switch / LOOP_DEFAULT_BRANCH=... git switch => block."""

    def test_foo_bar_git_switch_is_blocked(self, tmp_git_repo: Path):
        """FOO=bar git switch issue-1014-x => block."""
        result = eval_in_local_root(
            "FOO=bar git switch issue-981-test",
            str(tmp_git_repo),
        )
        assert result["status"] == "block", (
            "Leading env assignment must be fail-closed in local root context"
        )

    def test_loop_default_branch_env_switch_is_blocked(self, tmp_git_repo: Path):
        """LOOP_DEFAULT_BRANCH=issue-1014-x git switch issue-1014-x => block."""
        result = eval_in_local_root(
            "LOOP_DEFAULT_BRANCH=issue-981-test git switch issue-981-test",
            str(tmp_git_repo),
        )
        assert result["status"] == "block", (
            "LOOP_DEFAULT_BRANCH= env assignment in command must be blocked"
        )

    def test_has_leading_env_assignment_detects(self):
        """_has_leading_env_assignment correctly detects leading env."""
        assert _has_leading_env_assignment("FOO=bar git switch issue-*") is True
        assert _has_leading_env_assignment("LOOP_DEFAULT_BRANCH=main git switch x") is True
        assert _has_leading_env_assignment("git switch main") is False
        assert _has_leading_env_assignment("git switch FOO=bar") is False


# ─── B4: already-drifted root uses explicit allowlist ────────────────────────

class TestB4DriftedRootExplicitAllowlist:
    """B4: already-drifted mode blocks rtk wrapper and uses explicit allowlist."""

    def test_rtk_pnpm_test_is_blocked_when_drifted(self, tmp_git_repo_drifted: Path):
        """current_branch=issue-* + rtk pnpm test => block."""
        result = eval_in_local_root(
            "rtk pnpm test",
            str(tmp_git_repo_drifted),
        )
        assert result["status"] == "block", (
            "rtk pnpm test must be blocked when root is drifted"
        )

    def test_rtk_gh_pr_review_is_blocked_when_drifted(self, tmp_git_repo_drifted: Path):
        """current_branch=issue-* + rtk gh pr review 1041 => block."""
        result = eval_in_local_root(
            "rtk gh pr review 1041",
            str(tmp_git_repo_drifted),
        )
        assert result["status"] == "block", (
            "rtk gh pr review must be blocked when root is drifted"
        )

    def test_rtk_test_is_blocked_when_drifted(self, tmp_git_repo_drifted: Path):
        """rtk test => block when drifted."""
        result = eval_in_local_root("rtk test", str(tmp_git_repo_drifted))
        assert result["status"] == "block"

    def test_rtk_pytest_is_blocked_when_drifted(self, tmp_git_repo_drifted: Path):
        """rtk pytest => block when drifted."""
        result = eval_in_local_root("rtk pytest", str(tmp_git_repo_drifted))
        assert result["status"] == "block"

    def test_rtk_gh_issue_edit_is_blocked_when_drifted(self, tmp_git_repo_drifted: Path):
        """rtk gh issue edit => block when drifted."""
        result = eval_in_local_root("rtk gh issue edit 1014 --title x", str(tmp_git_repo_drifted))
        assert result["status"] == "block"

    def test_rtk_gh_pr_merge_is_blocked_when_drifted(self, tmp_git_repo_drifted: Path):
        """rtk gh pr merge => block when drifted."""
        result = eval_in_local_root("rtk gh pr merge 1041", str(tmp_git_repo_drifted))
        assert result["status"] == "block"

    def test_allowlist_is_closed(self):
        """_is_allowed_when_drifted only permits known-safe commands."""
        # These should be allowed
        assert _is_allowed_when_drifted("git status") is True
        assert _is_allowed_when_drifted("git branch --show-current") is True
        assert _is_allowed_when_drifted("git rev-parse HEAD") is True
        assert _is_allowed_when_drifted("git worktree list") is True
        assert _is_allowed_when_drifted("gh issue view 123") is True
        assert _is_allowed_when_drifted("gh pr view 1041") is True
        assert _is_allowed_when_drifted("gh pr list") is True
        assert _is_allowed_when_drifted("gh pr status") is True
        # These should NOT be allowed
        assert _is_allowed_when_drifted("rtk pnpm test") is False
        assert _is_allowed_when_drifted("rtk gh pr review 1041") is False
        assert _is_allowed_when_drifted("pnpm test") is False
        assert _is_allowed_when_drifted("uv run pytest") is False
        assert _is_allowed_when_drifted("gh issue edit 123") is False
        assert _is_allowed_when_drifted("gh pr merge 1041") is False


# ─── B1: detached HEAD is treated as detached_or_unknown (not default allow) ─

@pytest.fixture
def tmp_git_repo_detached(tmp_git_repo: Path) -> Path:
    """A repo where local root is in detached HEAD state."""
    subprocess.run(
        ["git", "-C", str(tmp_git_repo), "switch", "--detach", "HEAD"],
        check=True, capture_output=True
    )
    return tmp_git_repo


class TestB1DetachedHeadIsNotDefaultAllow:
    """B1: detached HEAD must be treated as detached_or_unknown, not as default/allow."""

    def test_classify_root_state_detached(self, tmp_git_repo_detached: Path):
        """classify_root_state with current_branch=None returns detached_or_unknown."""
        state = classify_root_state(None, "main")
        assert state == "detached_or_unknown"

    def test_classify_root_state_drifted(self):
        """classify_root_state with non-default branch returns drifted."""
        assert classify_root_state("issue-1014-test", "main") == "drifted"

    def test_classify_root_state_default(self):
        """classify_root_state with default branch returns default."""
        assert classify_root_state("main", "main") == "default"

    def test_detached_head_pnpm_test_is_blocked(self, tmp_git_repo_detached: Path):
        """detached HEAD + pnpm test => block."""
        result = eval_in_local_root("pnpm test", str(tmp_git_repo_detached))
        assert result["status"] == "block", "pnpm test must be blocked in detached HEAD state"
        assert result["reason_code"] == REASON_DETACHED_OR_UNKNOWN

    def test_detached_head_git_switch_main_is_allowed(self, tmp_git_repo_detached: Path):
        """detached HEAD + git switch main => allow (recovery)."""
        result = eval_in_local_root("git switch main", str(tmp_git_repo_detached))
        assert result["status"] == "allow", "git switch main must be allowed from detached HEAD"
        assert result["reason_code"] == REASON_RECOVERY

    def test_detached_head_git_switch_issue_is_blocked(self, tmp_git_repo_detached: Path):
        """detached HEAD + git switch issue-1014-test => block."""
        result = eval_in_local_root("git switch issue-1014-test", str(tmp_git_repo_detached))
        assert result["status"] == "block", "git switch non-default must be blocked in detached HEAD"

    def test_detached_head_git_status_is_allowed(self, tmp_git_repo_detached: Path):
        """detached HEAD + git status => allow (readonly is always safe)."""
        result = eval_in_local_root("git status", str(tmp_git_repo_detached))
        assert result["status"] == "allow"


# ─── B2: git -c alias bypass ─────────────────────────────────────────────────

class TestB2GitDashCAliasBypass:
    """B2: git -c alias.* must be fail-closed (can alias subcommands to bypass guard)."""

    def test_git_c_alias_switch_is_blocked(self, tmp_git_repo: Path):
        """git -c alias.sw='switch issue-981-test' sw => block."""
        result = eval_in_local_root(
            "git -c alias.sw='switch issue-981-test' sw",
            str(tmp_git_repo),
        )
        assert result["status"] == "block", (
            "git -c alias.* must be fail-closed to prevent subcommand aliasing bypass"
        )

    def test_git_c_advice_switch_is_blocked(self, tmp_git_repo: Path):
        """git -c advice.detachedHead=false switch issue-981-test => block."""
        result = eval_in_local_root(
            "git -c advice.detachedHead=false switch issue-981-test",
            str(tmp_git_repo),
        )
        assert result["status"] == "block", (
            "git -c <config> switch must be blocked (B2 fix)"
        )

    def test_normalize_git_global_opts_c_is_failclosed(self):
        """_normalize_git_global_opts: -c must be in fail-closed set."""
        tokens = ["git", "-c", "alias.sw=switch issue-981", "sw"]
        _, fail_closed = _normalize_git_global_opts(tokens)
        assert fail_closed is True, "-c must trigger fail_closed=True"


# ─── B3: drifted root blocks path restore ────────────────────────────────────

class TestB3DriftedRootBlocksPathRestore:
    """B3: already_drifted/detached_or_unknown roots must block path restore commands."""

    def test_drifted_root_git_restore_is_blocked(self, tmp_git_repo_drifted: Path):
        """drifted root + git restore README.md => block."""
        result = eval_in_local_root("git restore README.md", str(tmp_git_repo_drifted))
        assert result["status"] == "block", (
            "git restore must be blocked in drifted root (B3 fix)"
        )

    def test_drifted_root_git_checkout_double_dash_is_blocked(self, tmp_git_repo_drifted: Path):
        """drifted root + git checkout -- README.md => block."""
        result = eval_in_local_root("git checkout -- README.md", str(tmp_git_repo_drifted))
        assert result["status"] == "block", (
            "git checkout -- <path> must be blocked in drifted root (B3 fix)"
        )

    def test_drifted_root_git_checkout_p_is_blocked(self, tmp_git_repo_drifted: Path):
        """drifted root + git checkout -p => block."""
        result = eval_in_local_root("git checkout -p", str(tmp_git_repo_drifted))
        assert result["status"] == "block", (
            "git checkout -p must be blocked in drifted root (B3 fix)"
        )

    def test_main_root_git_restore_is_allowed(self, tmp_git_repo: Path):
        """main root + git restore README.md => allow (path restore on default branch is fine)."""
        result = eval_in_local_root("git restore README.md", str(tmp_git_repo))
        assert result["status"] == "allow", (
            "git restore must be allowed on main (default) branch"
        )

    def test_main_root_git_checkout_double_dash_is_allowed(self, tmp_git_repo: Path):
        """main root + git checkout -- README.md => allow."""
        result = eval_in_local_root("git checkout -- README.md", str(tmp_git_repo))
        assert result["status"] == "allow", (
            "git checkout -- <path> must be allowed on default branch"
        )


# ─── B5: _emit_block_stderr does not output raw branch names ─────────────────

class TestB5NoRawBranchInStderr:
    """B5: hook stderr must not contain raw branch names."""

    def test_emit_block_stderr_no_raw_branch(self, capsys):
        """_emit_block_stderr must not emit raw branch names."""
        from local_main_branch_guard import _emit_block_stderr, REASON_DRIFT
        _emit_block_stderr(
            reason_code=REASON_DRIFT,
            current_branch_kind="issue_like",
            current_is_default=False,
            target_branch_kind="issue",
            hook_flavor="claude",
        )
        captured = capsys.readouterr()
        # Must not contain raw branch names — only abstracted kinds
        assert "issue-1014" not in captured.err
        assert "worktree-issue" not in captured.err
        # Must contain abstracted kind fields
        assert "current_branch_kind" in captured.err
        assert "current_is_default" in captured.err

    def test_emit_block_stderr_contains_kind_fields(self, capsys):
        """_emit_block_stderr emits current_branch_kind and current_is_default."""
        from local_main_branch_guard import _emit_block_stderr, REASON_ALREADY_DRIFTED
        _emit_block_stderr(
            reason_code=REASON_ALREADY_DRIFTED,
            current_branch_kind="other",
            current_is_default=False,
            target_branch_kind=None,
            hook_flavor="codex",
        )
        captured = capsys.readouterr()
        assert "current_branch_kind: other" in captured.err
        assert "current_is_default: false" in captured.err

    def test_emit_block_stderr_max_10_lines(self, capsys):
        """_emit_block_stderr output is bounded to max 10 lines."""
        from local_main_branch_guard import _emit_block_stderr, REASON_DRIFT
        _emit_block_stderr(
            reason_code=REASON_DRIFT,
            current_branch_kind="issue_like",
            current_is_default=False,
            target_branch_kind="issue",
            hook_flavor="claude",
        )
        captured = capsys.readouterr()
        lines = [l for l in captured.err.splitlines() if l.strip()]
        assert len(lines) <= 10
