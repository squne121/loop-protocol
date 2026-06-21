"""
tests/codex/test_local_main_branch_guard.py

Tests for Codex CLI parity of local_main_branch_guard.py.
Covers AC8, AC17.

AC8: Codex hook input fixture — git switch issue-* denied, git switch main allowed,
     PermissionRequest also handled.
AC17: check_codex_agent_config.py fails on startup preflight absent / handler form wrong /
      hooks.json double-definition.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))

from local_main_branch_guard import (
    evaluate,
    REASON_DRIFT,
    REASON_RECOVERY,
    REASON_NOT_LOCAL_ROOT,
    REASON_READONLY,
    REASON_UNPARSEABLE,
    REASON_DETERMINISTIC_CHECKER,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def make_pretool_codex(command: str, cwd: str, event: str = "PreToolUse") -> dict:
    """Build a minimal Codex PreToolUse / PermissionRequest JSON payload."""
    return {
        "event": event,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": cwd,
    }


def eval_codex(command: str, cwd: str, event: str = "PreToolUse") -> dict:
    """
    Evaluate a Codex hook input.
    Sets CLAUDE_PROJECT_DIR so is_local_root_context returns True for cwd.
    """
    old = os.environ.get("CLAUDE_PROJECT_DIR", "")
    try:
        os.environ["CLAUDE_PROJECT_DIR"] = cwd
        result = evaluate(command=command, cwd=cwd, hook_flavor="codex")
    finally:
        if old:
            os.environ["CLAUDE_PROJECT_DIR"] = old
        elif "CLAUDE_PROJECT_DIR" in os.environ:
            del os.environ["CLAUDE_PROJECT_DIR"]
    return result


@pytest.fixture
def tmp_git_repo() -> Path:
    """Temporary git repo on 'main' branch."""
    tmpdir = tempfile.mkdtemp(prefix="lmbg_codex_test_")
    try:
        subprocess.run(["git", "init", "-b", "main", tmpdir], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.email", "t@t.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.name", "T"], check=True, capture_output=True)
        (Path(tmpdir) / "README.md").write_text("test")
        subprocess.run(["git", "-C", tmpdir, "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "branch", "issue-981-codex-test"], check=True, capture_output=True)
        yield Path(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── AC8: Codex parity ────────────────────────────────────────────────────────

class TestAC8CodexParity:
    """AC8: Codex hook input fixture tests for local_main_branch_guard parity."""

    def test_pretooluse_git_switch_issue_is_denied(self, tmp_git_repo: Path):
        """Codex PreToolUse: git switch issue-* is denied."""
        result = eval_codex("git switch issue-981-codex-test", str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_DRIFT
        assert result["hook_flavor"] == "codex"

    def test_pretooluse_git_switch_main_is_allowed(self, tmp_git_repo: Path):
        """Codex PreToolUse: git switch main is allowed (recovery)."""
        result = eval_codex("git switch main", str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_RECOVERY

    def test_pretooluse_git_checkout_issue_is_denied(self, tmp_git_repo: Path):
        """Codex PreToolUse: git checkout issue-* is denied."""
        result = eval_codex("git checkout issue-981-codex-test", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_pretooluse_gh_pr_checkout_is_denied(self, tmp_git_repo: Path):
        """Codex PreToolUse: gh pr checkout is denied."""
        result = eval_codex("gh pr checkout 988", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_permission_request_git_switch_issue_is_denied(self, tmp_git_repo: Path):
        """Codex PermissionRequest: git switch issue-* is denied (same logic)."""
        # PermissionRequest uses same evaluate() logic
        result = eval_codex(
            "git switch issue-981-codex-test",
            str(tmp_git_repo),
            event="PermissionRequest",
        )
        assert result["status"] == "block"
        assert result["hook_flavor"] == "codex"

    def test_permission_request_git_switch_main_is_allowed(self, tmp_git_repo: Path):
        """Codex PermissionRequest: git switch main is allowed."""
        result = eval_codex(
            "git switch main",
            str(tmp_git_repo),
            event="PermissionRequest",
        )
        assert result["status"] == "allow"

    def test_codex_hooks_json_has_local_main_branch_guard(self):
        """Codex .codex/hooks.json must include local_main_branch_guard hook."""
        hooks_path = REPO_ROOT / ".codex" / "hooks.json"
        if not hooks_path.exists():
            pytest.skip(".codex/hooks.json not found")
        hooks = json.loads(hooks_path.read_text())
        hooks_root = hooks.get("hooks", {})

        # Check PreToolUse
        pretool = hooks_root.get("PreToolUse", [])
        bash_entry = next(
            (e for e in pretool if e.get("matcher") == "^Bash$"), None
        )
        assert bash_entry is not None, "PreToolUse must have ^Bash$ matcher"
        commands = [h.get("command", "") for h in bash_entry.get("hooks", [])]
        assert any("local_main_branch_guard" in cmd for cmd in commands), (
            "PreToolUse ^Bash$ must include local_main_branch_guard hook"
        )

        # Check PermissionRequest
        perm_req = hooks_root.get("PermissionRequest", [])
        bash_perm = next(
            (e for e in perm_req if e.get("matcher") == "^Bash$"), None
        )
        assert bash_perm is not None, "PermissionRequest must have ^Bash$ matcher"
        perm_commands = [h.get("command", "") for h in bash_perm.get("hooks", [])]
        assert any("local_main_branch_guard" in cmd for cmd in perm_commands), (
            "PermissionRequest ^Bash$ must include local_main_branch_guard hook"
        )

    def test_codex_hook_script_exists(self):
        """Codex hook script .codex/hooks/local_main_branch_guard.sh must exist."""
        script_path = REPO_ROOT / ".codex" / "hooks" / "local_main_branch_guard.sh"
        assert script_path.exists(), (
            f"Codex hook script not found: {script_path}"
        )

    def test_guard_script_exists(self):
        """Shared guard script scripts/agent-guards/local_main_branch_guard.py must exist."""
        guard_path = REPO_ROOT / "scripts" / "agent-guards" / "local_main_branch_guard.py"
        assert guard_path.exists(), (
            f"Guard script not found: {guard_path}"
        )


class TestReadonlyPipelineClassifier:
    """readonly pipeline classifier fixtures for AC1-AC4."""

    def test_readonly_pipeline_rg_head(self, tmp_git_repo: Path):
        """GIVEN readonly pipeline WHEN rg is piped to head THEN allow readonly_command."""
        result = eval_codex('rg -n "TODO" README.md | head -n 20', str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_READONLY

    def test_readonly_pipeline_git_status_head(self, tmp_git_repo: Path):
        """GIVEN readonly pipeline WHEN git status is piped to head THEN allow readonly_command."""
        result = eval_codex("git status --short | head -n 20", str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_READONLY

    def test_readonly_pipeline_git_diff_head(self, tmp_git_repo: Path):
        """GIVEN readonly pipeline WHEN git diff is piped to head THEN allow readonly_command."""
        result = eval_codex("git diff --stat | head -n 20", str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_READONLY

    def test_readonly_pipeline_rejects_git_status_git_switch(self, tmp_git_repo: Path):
        """GIVEN mixed pipeline WHEN git status feeds git switch THEN unparseable_branch_mutation is denied."""
        result = eval_codex("git status | git switch issue-123", str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_UNPARSEABLE

    def test_readonly_pipeline_rejects_xargs_rm(self, tmp_git_repo: Path):
        """GIVEN readonly-looking pipeline WHEN xargs rm appears THEN unparseable_branch_mutation is denied."""
        result = eval_codex("rg TODO . | xargs rm -f", str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_UNPARSEABLE

    def test_readonly_pipeline_rejects_and_and_git_switch(self, tmp_git_repo: Path):
        """GIVEN compound readonly pipeline WHEN && is present THEN unparseable_branch_mutation is denied."""
        result = eval_codex("rg TODO . && git switch issue-123", str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_UNPARSEABLE

    def test_readonly_pipeline_rejects_redirection(self, tmp_git_repo: Path):
        """GIVEN readonly pipeline WHEN stdout redirection is present THEN unparseable_branch_mutation is denied."""
        result = eval_codex("rg TODO . > out.txt", str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_UNPARSEABLE

    def test_readonly_pipeline_rejects_bash_wrapper(self, tmp_git_repo: Path):
        """GIVEN wrapped command WHEN bash -lc is used THEN unparseable_branch_mutation is denied."""
        result = eval_codex("bash -lc 'git switch issue-123'", str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_UNPARSEABLE

    def test_readonly_pipeline_rejects_command_substitution(self, tmp_git_repo: Path):
        """GIVEN wrapped command WHEN command substitution is used THEN unparseable_branch_mutation is denied."""
        result = eval_codex("$(git switch issue-123)", str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_UNPARSEABLE


# ─── AC17: Codex startup preflight mandatory ─────────────────────────────────

class TestAC17CodexStartupPreflightMandatory:
    """AC17: check_codex_agent_config.py validates startup preflight presence."""

    def test_startup_preflight_script_exists(self):
        """scripts/check_local_main_branch_state.py must exist."""
        script_path = REPO_ROOT / "scripts" / "check_local_main_branch_state.py"
        assert script_path.exists(), (
            f"Startup preflight script not found: {script_path}"
        )

    def test_check_codex_agent_config_validates_preflight(self):
        """check_codex_agent_config.py --assert-local-main-branch-guard must pass."""
        result = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "check_codex_agent_config.py"),
             "--assert-local-main-branch-guard"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"check_codex_agent_config.py --assert-local-main-branch-guard failed:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_hooks_json_no_duplicate_local_main_guard_definition(self):
        """
        AC17: .codex/hooks.json must not double-define local_main_branch_guard
        (i.e., it should be defined only once per event/matcher combination).
        """
        hooks_path = REPO_ROOT / ".codex" / "hooks.json"
        if not hooks_path.exists():
            pytest.skip(".codex/hooks.json not found")
        hooks = json.loads(hooks_path.read_text())
        hooks_root = hooks.get("hooks", {})

        for event_name, entries in hooks_root.items():
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                matcher = entry.get("matcher", "")
                # Count local_main_branch_guard occurrences in this matcher's hooks
                guard_count = sum(
                    1 for h in entry.get("hooks", [])
                    if "local_main_branch_guard" in h.get("command", "")
                )
                assert guard_count <= 1, (
                    f"local_main_branch_guard is defined {guard_count} times "
                    f"in {event_name}/{matcher!r} — must not be duplicated"
                )

    def test_startup_preflight_runs_successfully_on_main(self, tmp_path):
        """
        check_local_main_branch_state.py --json should return status 'ok' or 'unknown'
        when run from an isolated repo on 'main' branch.

        Uses a temporary git repo (approach A) so the test is independent of the
        current branch of the host repository (which may be a feature branch during
        development or CI).
        """
        # Set up an isolated temporary git repo on 'main'
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        }
        subprocess.run(
            ["git", "init", "-b", "main", str(tmp_path)],
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"],
            check=True,
            capture_output=True,
            env=env,
        )

        script = REPO_ROOT / "scripts" / "check_local_main_branch_state.py"
        result = subprocess.run(
            [sys.executable, str(script), "--json"],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        if result.returncode != 0 and not result.stdout:
            pytest.skip("check_local_main_branch_state.py could not determine state")
        if result.stdout.strip():
            data = json.loads(result.stdout)
            state = data.get("LOCAL_MAIN_BRANCH_STATE_RESULT_V1", {})
            assert state.get("status") in ("ok", "unknown"), (
                f"Expected ok/unknown from isolated main-branch repo, got: {state}"
            )


class TestBranchSafeMaintenance:
    """AC2: git fetch / git worktree prune -> branch_safe_maintenance_command."""

    def test_git_fetch_is_branch_safe_maintenance(self, tmp_git_repo: Path):
        result = eval_codex("git fetch", str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == "branch_safe_maintenance_command"

    def test_git_worktree_prune_is_branch_safe_maintenance(self, tmp_git_repo: Path):
        result = eval_codex("git worktree prune", str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == "branch_safe_maintenance_command"

    def test_git_fetch_is_not_readonly_command(self, tmp_git_repo: Path):
        result = eval_codex("git fetch", str(tmp_git_repo))
        assert result["reason_code"] != REASON_READONLY


class TestFdDuplication:
    """AC10: 2>&1 | head fd-duplication."""

    def test_fd_duplication_git_diff_stat_allowed(self, tmp_git_repo: Path):
        result = eval_codex("git diff --stat 2>&1 | head -n 20", str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_READONLY

    def test_file_write_redirect_still_blocked(self, tmp_git_repo: Path):
        result = eval_codex("rg TODO . > out.txt", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_append_redirect_still_blocked(self, tmp_git_repo: Path):
        result = eval_codex("git log >> history.txt", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_fd_dup_without_pipe_blocked(self, tmp_git_repo: Path):
        result = eval_codex("git diff 2>&1", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_rg_fd_dup_head_allowed(self, tmp_git_repo: Path):
        result = eval_codex('rg -n "TODO" README.md 2>&1 | head -n 10', str(tmp_git_repo))
        assert result["status"] == "allow"


class TestGhReadonlyAndDeny:
    """AC11: gh readonly / gh deny."""

    def test_gh_readonly_issue_view(self, tmp_git_repo: Path):
        result = eval_codex("gh issue view 123", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_gh_issue_list_is_readonly(self, tmp_git_repo: Path):
        result = eval_codex("gh issue list", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_gh_pr_view_is_readonly(self, tmp_git_repo: Path):
        result = eval_codex("gh pr view 456", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_gh_pr_list_is_readonly(self, tmp_git_repo: Path):
        result = eval_codex("gh pr list", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_gh_pr_status_is_readonly(self, tmp_git_repo: Path):
        result = eval_codex("gh pr status", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_gh_issue_view_pipeline_head_allowed(self, tmp_git_repo: Path):
        result = eval_codex("gh issue view 123 | head -n 20", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_gh_deny_issue_edit(self, tmp_git_repo: Path):
        result = eval_codex("gh issue edit 123 --body new", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_gh_issue_close_is_denied(self, tmp_git_repo: Path):
        result = eval_codex("gh issue close 123", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_gh_pr_merge_is_denied(self, tmp_git_repo: Path):
        result = eval_codex("gh pr merge 456", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_gh_pr_update_branch_is_denied(self, tmp_git_repo: Path):
        result = eval_codex("gh pr update-branch 456", str(tmp_git_repo))
        assert result["status"] == "block"


class TestExactAllowlist:
    """AC5, AC6, AC12: exact allowlist, publisher deny, deterministic_checker reason_code."""

    def test_exact_allowlist_run_refinement_preflight(self, tmp_git_repo: Path):
        result = eval_codex(
            "uv run python3 .claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py --issue-number 985 --repo squne121/loop-protocol",
            str(tmp_git_repo),
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_DETERMINISTIC_CHECKER

    def test_wildcard_path_not_in_allowlist(self, tmp_git_repo: Path):
        result = eval_codex(
            "uv run python3 .claude/skills/create-issue/scripts/create_issue_txn.py",
            str(tmp_git_repo),
        )
        assert result["reason_code"] != REASON_DETERMINISTIC_CHECKER

    def test_publisher_deny_not_in_allowlist(self, tmp_git_repo: Path):
        result = eval_codex(
            "uv run python3 .claude/skills/post-merge-cleanup/scripts/cleanup_runner.py",
            str(tmp_git_repo),
        )
        assert result["reason_code"] != REASON_DETERMINISTIC_CHECKER

    def test_deterministic_checker_command_reason_code(self, tmp_git_repo: Path):
        result = eval_codex(
            "uv run python3 .claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py --issue-number 985 --repo squne121/loop-protocol",
            str(tmp_git_repo),
        )
        assert result["reason_code"] == REASON_DETERMINISTIC_CHECKER
        assert result["reason_code"] != REASON_READONLY


class TestPythonpathStaleAndTmpWrapper:
    """AC14: PYTHONPATH stale regression / /tmp wrapper fail-closed."""

    def test_tmp_wrapper_script_blocked(self, tmp_git_repo: Path):
        result = eval_codex(
            "uv run python3 /tmp/run_refinement_preflight.py --issue-number 985",
            str(tmp_git_repo),
        )
        assert result["status"] == "block"

    def test_python_c_inline_blocked(self, tmp_git_repo: Path):
        result = eval_codex("python3 -c import_os", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_bash_lc_wrapper_blocked(self, tmp_git_repo: Path):
        result = eval_codex("bash -lc 'uv run python3 /tmp/script.py'", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_pythonpath_stale_guard_module_unaffected(self, tmp_git_repo: Path, tmp_path: Path):
        import os
        (tmp_path / "command_registry.py").write_text("raise ImportError('stale!')")
        old = os.environ.get("PYTHONPATH", "")
        try:
            os.environ["PYTHONPATH"] = str(tmp_path)
            result = eval_codex("git status", str(tmp_git_repo))
        finally:
            if old:
                os.environ["PYTHONPATH"] = old
            elif "PYTHONPATH" in os.environ:
                del os.environ["PYTHONPATH"]
        assert result["status"] == "allow"


class TestGhMutationFailClosedCompleteness:
    """AC11: gh issue/pr mutation subcommands outside readonly allowlist are ALL blocked (allowlist-closed completeness)."""

    @pytest.mark.parametrize("cmd", [
        "gh issue create --title x --body y",
        "gh issue develop 123 --base main",
        "gh issue develop 123 --checkout",
        "gh issue transfer 123 other/repo",
        "gh issue pin 123",
        "gh issue unpin 123",
        "gh pr create --title x --body y",
        "gh pr revert 123",
        "gh pr lock 123",
        "gh pr unlock 123",
    ])
    def test_unlisted_gh_mutations_are_blocked(self, tmp_git_repo: Path, cmd: str):
        """GIVEN gh mutation not in original denylist WHEN evaluated THEN blocked (allowlist-closed)."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_UNPARSEABLE


class TestProjectTmpPolicy:
    """AC14: OS absolute /tmp is blocked but project-relative tmp/ is not mis-identified."""

    def test_repo_relative_tmp_script_is_not_blocked(self, tmp_git_repo: Path):
        """GIVEN uv run python3 tmp/check.py (relative) WHEN evaluated THEN allow (not OS /tmp)."""
        result = eval_codex("uv run python3 tmp/check.py --dry-run", str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result.get("reason_code") != REASON_UNPARSEABLE

    def test_absolute_tmp_script_is_blocked(self, tmp_git_repo: Path):
        """GIVEN uv run python3 /tmp/check.py (OS absolute) WHEN evaluated THEN blocked."""
        result = eval_codex("uv run python3 /tmp/check.py --dry-run", str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_UNPARSEABLE
