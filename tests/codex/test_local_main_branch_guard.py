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
    REASON_GITHUB_REMOTE_OPS,
    REASON_GH_MUTATION,
    is_github_issue_mutation_command,
    is_readonly_artifact_export_command,
    GITHUB_CMD_CLASS_DISPLAY_READONLY,
    GITHUB_CMD_CLASS_READONLY_EXPORT,
    GITHUB_CMD_CLASS_ISSUE_MUTATION,
    GITHUB_CMD_CLASS_PR_METADATA,
    GITHUB_CMD_CLASS_DESTRUCTIVE,
    TRUSTED_REPO_SLUG,
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

    def test_gh_issue_edit_is_blocked(self, tmp_git_repo: Path):
        """gh issue edit is NOT in the minimal allowlist and must be blocked (B3)."""
        result = eval_codex("gh issue edit 123 --body new", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_gh_issue_close_is_allowed(self, tmp_git_repo: Path):
        """gh issue close is in GH_OPS_ALLOW_PATTERNS and must be allowed (AC1)."""
        result = eval_codex("gh issue close 123", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_gh_pr_merge_is_denied(self, tmp_git_repo: Path):
        """gh pr merge affects local state and must remain blocked (AC3)."""
        result = eval_codex("gh pr merge 456", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_gh_pr_update_branch_is_denied(self, tmp_git_repo: Path):
        result = eval_codex("gh pr update-branch 456", str(tmp_git_repo))
        assert result["status"] == "block"


class TestGhMutationReasonCode:
    """AC1-AC7 (#1109): gh_mutation_denied reason_code for gh issue/pr mutation block."""

    # AC2: gh issue close/comment/edit/reopen/delete/lock/unlock
    @pytest.mark.parametrize("cmd", [
        # gh issue close/comment/reopen are now allow via is_github_remote_ops_command (#1120)
        "gh issue edit 123 --title new",
        "gh issue delete 123",
        "gh issue lock 123",
        "gh issue unlock 123",
    ])
    def test_gh_issue_mutations_use_gh_mutation_denied(self, tmp_git_repo: Path, cmd: str):
        """GIVEN gh issue mutation (outside github_remote_ops allowlist) WHEN evaluated THEN reason_code is gh_mutation_denied."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_GH_MUTATION, (
            f"Expected gh_mutation_denied for {cmd!r}, got {result['reason_code']!r}"
        )

    # AC3: gh pr checkout/edit/comment/merge/update-branch/review
    @pytest.mark.parametrize("cmd", [
        # gh pr comment --body and gh pr edit <N> are now allow via is_github_remote_ops_command (#1120)
        "gh pr checkout 456",
        "gh pr merge 456",
        "gh pr update-branch 456",
        "gh pr review 456",
    ])
    def test_gh_pr_mutations_use_gh_mutation_denied(self, tmp_git_repo: Path, cmd: str):
        """GIVEN gh pr mutation (outside github_remote_ops allowlist) WHEN evaluated THEN reason_code is gh_mutation_denied."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_GH_MUTATION, (
            f"Expected gh_mutation_denied for {cmd!r}, got {result['reason_code']!r}"
        )

    # AC4: readonly commands still allow
    @pytest.mark.parametrize("cmd", [
        "gh issue view 123",
        "gh issue list",
        "gh pr view 456",
        "gh pr list",
        "gh pr status",
    ])
    def test_gh_readonly_still_allowed(self, tmp_git_repo: Path, cmd: str):
        """GIVEN gh readonly command WHEN evaluated THEN status is allow."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "allow", (
            f"Expected allow for readonly {cmd!r}, got {result['status']!r}"
        )

    def test_gh_mutation_denied_constant_value(self):
        """AC1: REASON_GH_MUTATION == 'gh_mutation_denied'."""
        assert REASON_GH_MUTATION == "gh_mutation_denied"

    def test_gh_mutation_recovery_hint_contains_approved(self, tmp_git_repo: Path, capsys):
        """AC7: gh mutation block の recovery hint が GitHub mutation 文脈の文言を含む"""
        from local_main_branch_guard import _emit_block_stderr, REASON_GH_MUTATION
        _emit_block_stderr(
            reason_code=REASON_GH_MUTATION,
            current_branch_kind="default",
            current_is_default=True,
            target_branch_kind=None,
            hook_flavor="codex",
        )
        captured = capsys.readouterr()
        hint_line = [l for l in captured.err.splitlines() if "recovery:" in l]
        assert hint_line, "Expected a recovery: line in stderr"
        hint = hint_line[0].lower()
        assert any(kw in hint for kw in ("approved", "rtk", "workflow")), (
            f"recovery hint should mention approved/rtk/workflow, got: {hint!r}"
        )



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
    """AC11: gh issue/pr mutation subcommands outside readonly allowlist and GH_OPS_ALLOW_PATTERNS are ALL blocked (allowlist-closed completeness)."""

    @pytest.mark.parametrize("cmd", [
        # gh issue subcommands NOT in the minimal allowlist
        "gh issue create --title x --body y",    # B3: removed from allowlist
        "gh issue edit 123 --title new",          # B3: removed from allowlist
        "gh issue develop 123 --base main",
        "gh issue develop 123 --checkout",
        "gh issue transfer 123 other/repo",
        "gh issue pin 123",
        "gh issue unpin 123",
        # gh pr subcommands NOT in the minimal allowlist
        "gh pr create --title x --body y",        # B3: removed from allowlist
        "gh pr revert 123",
        "gh pr lock 123",
        "gh pr unlock 123",
    ])
    def test_unlisted_gh_mutations_are_blocked(self, tmp_git_repo: Path, cmd: str):
        """GIVEN gh mutation not in readonly allowlist or minimal gh ops allowlist WHEN evaluated THEN blocked (allowlist-closed)."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_GH_MUTATION

    @pytest.mark.parametrize("cmd", [
        "gh issue close 1089",
        "gh issue comment 123 --body hello",
        "gh issue comment 123 --body-file /tmp/body.txt",
        "gh issue reopen 123",
        "gh pr comment 456 --body hello",
        "gh pr edit 456 --title new",
    ])
    def test_gh_ops_allowlist_commands_are_allowed(self, tmp_git_repo: Path, cmd: str):
        """GIVEN gh issue/pr ops in post-merge-cleanup minimal set WHEN evaluated THEN allowed (AC1)."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_GITHUB_REMOTE_OPS


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

class TestGhOpsMinimalAllowlist:
    """AC1, B3: post-merge-cleanup 最小集合の token-based classifier テスト（Codex flavor）。"""

    @pytest.mark.parametrize("cmd,expected", [
        # must-allow: 最小集合
        ("gh issue close 1089", "allow"),
        ("gh issue comment 123 --body hello", "allow"),
        ("gh issue comment 123 --body-file /some/file.txt", "allow"),
        ("gh issue reopen 456", "allow"),
        ("gh pr comment 789 --body text", "allow"),
        ("gh pr edit 101 --title new", "allow"),
    ])
    def test_minimal_allowlist_allowed(self, tmp_git_repo: Path, cmd: str, expected: str):
        """GIVEN minimal allowlist command WHEN evaluated THEN allowed with github_remote_ops_command reason."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == expected
        assert result["reason_code"] == REASON_GITHUB_REMOTE_OPS

    @pytest.mark.parametrize("cmd", [
        # must-block: 最小集合外
        "gh issue create --title x --body y",   # B3: not in minimal set
        "gh issue edit 123 --title new",          # B3: not in minimal set (interactive possible)
        "gh pr create --title x --body y",        # B3: not in minimal set
        "gh issue comment 123",                   # B2: --body なし → interactive
        "gh pr comment 456",                      # B2: --body なし → interactive
        "gh issue close",                         # B1: 番号なし
        "gh issue reopen",                        # B1: 番号なし
        "gh pr edit",                             # B1: 番号なし → branch 依存
        "gh issue comment 123 --delete-last",     # B2: destructive flag
        "gh issue comment 123 --editor",          # B2: interactive flag
        "gh issue comment 123 --web",             # B2: interactive flag
    ])
    def test_minimal_allowlist_blocked(self, tmp_git_repo: Path, cmd: str):
        """GIVEN command outside minimal allowlist WHEN evaluated THEN blocked."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block"


# ─── AC8〜AC15: Issue #1124 GitHub remote ops 5 分類 ────────────────────────

class TestGithubIssueMutationCommand:
    """
    AC8: gh issue edit with --repo + --body-file tmp/ → allow (github_issue_mutation_command)
    AC9: gh issue create with --repo + --body-file tmp/ → allow (github_issue_mutation_command)
    AC10: bare gh issue create / gh issue edit 123 (no --body-file) → block
    AC13: gh issue create/edit/comment/close/reopen is NOT readonly_command
    """

    # AC8: gh issue edit <N> --repo squne121/loop-protocol --body-file tmp/foo.md → allow
    @pytest.mark.parametrize("cmd", [
        "gh issue edit 123 --repo squne121/loop-protocol --body-file tmp/foo.md",
        "gh issue edit 456 --repo squne121/loop-protocol --body-file tmp/body.md --label bug",
        "gh issue edit 1 --repo squne121/loop-protocol --body-file tmp/issue.md",
    ])
    def test_ac8_gh_issue_edit_with_repo_and_bodyfile_allowed(self, tmp_git_repo: Path, cmd: str):
        """AC8: GIVEN gh issue edit with --repo + --body-file tmp/ WHEN evaluated THEN allow."""
        assert is_github_issue_mutation_command(cmd), f"Expected True for: {cmd!r}"
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "allow", f"Expected allow for: {cmd!r}"
        assert result["reason_code"] == REASON_GITHUB_REMOTE_OPS

    # AC9: gh issue create --repo squne121/loop-protocol --body-file tmp/foo.md → allow
    @pytest.mark.parametrize("cmd", [
        "gh issue create --repo squne121/loop-protocol --title foo --body-file tmp/foo.md",
        "gh issue create --repo squne121/loop-protocol --title タイトル --body-file tmp/foo.md --label enhancement",
        "gh issue create --repo squne121/loop-protocol --body-file tmp/body.md --title new-issue",
    ])
    def test_ac9_gh_issue_create_with_repo_and_bodyfile_allowed(self, tmp_git_repo: Path, cmd: str):
        """AC9: GIVEN gh issue create with --repo + --body-file tmp/ WHEN evaluated THEN allow."""
        assert is_github_issue_mutation_command(cmd), f"Expected True for: {cmd!r}"
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "allow", f"Expected allow for: {cmd!r}"
        assert result["reason_code"] == REASON_GITHUB_REMOTE_OPS

    # AC10: bare gh issue create / gh issue edit 123 (no --body-file) → block
    @pytest.mark.parametrize("cmd", [
        "gh issue create",                                          # bare create
        "gh issue edit 123",                                        # bare edit, no --body-file
        "gh issue create --repo squne121/loop-protocol",            # no --body-file
        "gh issue edit 123 --repo squne121/loop-protocol",          # no --body-file
        "gh issue create --body-file tmp/foo.md",                   # no --repo
        "gh issue edit 123 --body-file tmp/foo.md",                 # no --repo
        "gh issue create --repo squne121/loop-protocol --body-file tmp/foo.md --editor",  # interactive
        "gh issue edit 123 --repo squne121/loop-protocol --body-file tmp/foo.md --web",   # interactive
        "gh issue create --repo squne121/loop-protocol --body-file -",  # stdin
        "gh issue edit 123 --repo squne121/loop-protocol --body-file /tmp/foo.md",  # /tmp not tmp/
        "gh issue edit 123 --repo other-org/other-repo --body-file tmp/foo.md",    # wrong repo
    ])
    def test_ac10_bare_gh_issue_create_or_edit_blocked(self, tmp_git_repo: Path, cmd: str):
        """AC10: GIVEN bare gh issue create/edit or without required flags WHEN evaluated THEN block."""
        assert not is_github_issue_mutation_command(cmd), f"Expected False for: {cmd!r}"
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block", f"Expected block for: {cmd!r}"

    # AC13: gh issue create/edit/comment/close/reopen are NOT readonly_command
    @pytest.mark.parametrize("cmd", [
        "gh issue create --repo squne121/loop-protocol --title t --body-file tmp/foo.md",
        "gh issue edit 123 --repo squne121/loop-protocol --body-file tmp/foo.md",
        "gh issue close 123",
        "gh issue comment 123 --body hello",
        "gh issue reopen 123",
    ])
    def test_ac13_gh_issue_mutations_not_readonly_command(self, tmp_git_repo: Path, cmd: str):
        """AC13: GIVEN gh issue mutation WHEN evaluated THEN reason_code is NOT readonly_command."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["reason_code"] != REASON_READONLY, (
            f"gh issue mutation {cmd!r} must not have readonly_command reason_code, "
            f"got: {result['reason_code']!r}"
        )


class TestReadonlyArtifactExportCommand:
    """
    AC11: gh issue view ... > tmp/issue_123.md → allow (readonly_artifact_export_command)
    AC12: gh issue view 123 > src/foo.md / > docs/foo.md / > .env / >> tmp/foo.md → block
    """

    # AC11: gh issue view ... > tmp/issue_123.md → allow
    @pytest.mark.parametrize("cmd", [
        "gh issue view 123 --repo squne121/loop-protocol --json body --jq .body > tmp/issue_123.md",
        "gh issue view 456 > tmp/issue_456.md",
        "gh issue view 1 --json body > tmp/out.md",
    ])
    def test_ac11_gh_issue_view_to_tmp_allowed(self, tmp_git_repo: Path, cmd: str):
        """AC11: GIVEN gh issue view ... > tmp/... WHEN evaluated THEN allow."""
        assert is_readonly_artifact_export_command(cmd), f"Expected True for: {cmd!r}"
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "allow", f"Expected allow for: {cmd!r}"
        assert result["reason_code"] == REASON_READONLY

    # AC12: blocked destinations
    @pytest.mark.parametrize("cmd", [
        "gh issue view 123 > src/foo.md",          # src/ destination
        "gh issue view 123 > docs/foo.md",         # docs/ destination
        "gh issue view 123 > .env",                # .env destination
        "gh issue view 123 >> tmp/foo.md",         # append redirect
        "gh issue view 123 > /tmp/foo.md",         # /tmp absolute (not tmp/ relative)
        "gh issue view 123 > foo.md",              # no directory prefix
    ])
    def test_ac12_gh_issue_view_to_blocked_dest_blocked(self, tmp_git_repo: Path, cmd: str):
        """AC12: GIVEN gh issue view with blocked destination WHEN evaluated THEN block."""
        assert not is_readonly_artifact_export_command(cmd), f"Expected False for: {cmd!r}"
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block", f"Expected block for: {cmd!r}"


class TestGhDestructiveCommandsBlocked:
    """AC14: gh pr merge / gh pr checkout / gh pr update-branch remain blocked."""

    @pytest.mark.parametrize("cmd", [
        "gh pr merge 123",
        "gh pr checkout 123",
        "gh pr update-branch 456",
    ])
    def test_ac14_gh_pr_destructive_blocked(self, tmp_git_repo: Path, cmd: str):
        """AC14: GIVEN gh pr merge/checkout/update-branch WHEN evaluated THEN block."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block", f"Expected block for: {cmd!r}"
        assert result["reason_code"] == REASON_GH_MUTATION

    def test_ac14_gh_pr_create_blocked(self, tmp_git_repo: Path):
        """AC14: gh pr create is blocked (destructive / local push dependent)."""
        result = eval_codex("gh pr create --title x --body y", str(tmp_git_repo))
        assert result["status"] == "block"


class TestGithub5ClassVocabularyConstants:
    """AC15: 5-class vocabulary constants are defined and consistent."""

    def test_github_cmd_class_constants_defined(self):
        """AC15: All 5 vocabulary constants must be defined."""
        assert GITHUB_CMD_CLASS_DISPLAY_READONLY == "display_readonly_command"
        assert GITHUB_CMD_CLASS_READONLY_EXPORT == "readonly_artifact_export_command"
        assert GITHUB_CMD_CLASS_ISSUE_MUTATION == "github_issue_mutation_command"
        assert GITHUB_CMD_CLASS_PR_METADATA == "github_pr_metadata_command"
        assert GITHUB_CMD_CLASS_DESTRUCTIVE == "github_destructive_command"

    def test_trusted_repo_slug_is_defined(self):
        """AC15: TRUSTED_REPO_SLUG constant is defined."""
        assert TRUSTED_REPO_SLUG == "squne121/loop-protocol"

    def test_ac15_hook_boundaries_doc_has_5class_vocabulary(self):
        """AC15: docs/dev/hook-boundaries.md contains the 5-class vocabulary terms."""
        doc_path = REPO_ROOT / "docs" / "dev" / "hook-boundaries.md"
        assert doc_path.exists(), f"hook-boundaries.md not found: {doc_path}"
        content = doc_path.read_text()
        for term in [
            "display_readonly_command",
            "readonly_artifact_export_command",
            "github_issue_mutation_command",
            "github_pr_metadata_command",
            "github_destructive_command",
        ]:
            assert term in content, (
                f"hook-boundaries.md must contain 5-class term: {term!r}"
            )

    def test_ac15_agent_skill_boundaries_doc_has_5class_vocabulary(self):
        """AC15: docs/dev/agent-skill-boundaries.md contains the 5-class vocabulary terms."""
        doc_path = REPO_ROOT / "docs" / "dev" / "agent-skill-boundaries.md"
        assert doc_path.exists(), f"agent-skill-boundaries.md not found: {doc_path}"
        content = doc_path.read_text()
        for term in [
            "display_readonly_command",
            "readonly_artifact_export_command",
            "github_issue_mutation_command",
        ]:
            assert term in content, (
                f"agent-skill-boundaries.md must contain 5-class term: {term!r}"
            )

    def test_ac15_codex_default_rules_has_trusted_repo_mention(self):
        """AC15: .codex/rules/default.rules mentions managed skill context for gh issue create/edit."""
        rules_path = REPO_ROOT / ".codex" / "rules" / "default.rules"
        assert rules_path.exists(), f"default.rules not found: {rules_path}"
        content = rules_path.read_text()
        # The rules file should mention that managed skill context allows gh issue create/edit
        assert "github_issue_mutation" in content or "managed skill" in content or "body-file" in content, (
            ".codex/rules/default.rules must reference managed skill / body-file context for gh issue mutations"
        )
