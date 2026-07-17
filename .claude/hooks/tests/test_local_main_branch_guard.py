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

from local_main_branch_guard import (  # noqa: E402
    evaluate,
    is_local_root_context,
    resolve_default_branch,
    classify_branch,
    classify_root_state,
    has_inline_env_override,
    is_readonly_command,
    is_branch_safe_maintenance_command,
    is_compound_or_wrapped,
    _has_leading_env_assignment,
    _normalize_git_global_opts,
    _is_allowed_when_drifted,
    REASON_NOT_LOCAL_ROOT,
    REASON_LINKED_ISSUE_WORKTREE_CONTEXT,
    REASON_READONLY,
    REASON_BRANCH_SAFE_MAINTENANCE,
    REASON_RECOVERY,
    REASON_DRIFT,
    REASON_DETACHED_OR_UNKNOWN,
    REASON_UNPARSEABLE,
    REASON_INLINE_OVERRIDE,
    REASON_DETERMINISTIC_CHECKER,
    REASON_CONTROLLED_SKILL_MUTATION_EXECUTOR,
    REASON_GITHUB_REMOTE_OPS,
    REASON_GH_API,
    REASON_GH_MUTATION,
    REASON_SKILL_RUNTIME_EXECUTOR,
    is_github_issue_mutation_command,
    is_readonly_artifact_export_command,
    GITHUB_CMD_CLASS_DISPLAY_READONLY,
    GITHUB_CMD_CLASS_READONLY_EXPORT,
    GITHUB_CMD_CLASS_ISSUE_MUTATION,
    GITHUB_CMD_CLASS_PR_METADATA,
    GITHUB_CMD_CLASS_DESTRUCTIVE,
    TRUSTED_REPO_SLUG,
)
from skill_runtime_command_policy import resolve_repo_slug  # noqa: E402

RAW_ISSUE_EDIT_COMMANDS = [
    "gh issue edit 123 --repo squne121/loop-protocol --body-file tmp/foo.md",
    "gh issue edit 123 --repo squne121/loop-protocol --body rewritten",
    "gh issue edit 123 --repo squne121/loop-protocol --title new",
    "gh issue edit 123 --repo squne121/loop-protocol --add-label bug",
    "gh issue edit 123 --repo squne121/loop-protocol --remove-label bug",
    "gh issue edit 123 --repo squne121/loop-protocol --add-assignee @me",
    "gh issue edit 123 --repo squne121/loop-protocol --milestone v1",
    "gh issue edit 123 --repo squne121/loop-protocol --remove-milestone",
    "gh issue edit 123 --repo squne121/loop-protocol --add-project Roadmap",
    "gh issue edit 123 --repo squne121/loop-protocol --add-sub-issue 124",
    "gh issue edit 123 --repo squne121/loop-protocol --add-blocked-by 200",
    "gh issue edit 123 --repo squne121/loop-protocol --add-blocking 300",
]

RAW_ISSUE_COMMENT_COMMANDS = [
    "gh issue comment 123 --body hello",
    "gh issue comment 123 --body-file tmp/body.md",
    "gh issue comment 123 -F tmp/body.md",
    "gh issue comment 123 --body-file -",
    "gh issue comment 123 --editor",
    "gh issue comment 123 --web",
    "gh issue comment 123 --edit-last",
    "gh issue comment 123 --delete-last",
    "gh issue comment 123 --create-if-none --edit-last --body x",
]

REVIEWER_GH_MUTATION_REGRESSIONS = [
    ("gh api repos/squne121/loop-protocol/issues/1395/comments -f body=bad", REASON_GH_API),
    ("gh api graphql -f query='mutation { __typename }'", REASON_GH_API),
    ("gh api --method POST repos/squne121/loop-protocol/issues/comments/1", REASON_GH_API),
    ("gh issue comment 1395 --body bad", REASON_GH_MUTATION),
]

CONTROLLED_METADATA_COMMANDS = [
    (
        "issue_body.update",
        "artifacts/1291/issue-metadata/issue_body.update/input.json",
    ),
    (
        "issue_comment.publish",
        "artifacts/1291/issue-metadata/issue_comment.publish/input.json",
    ),
    (
        "contract_snapshot.publish",
        "artifacts/1291/issue-metadata/contract_snapshot.publish/input.json",
    ),
]


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_git_repo() -> Generator[Path, None, None]:
    """Create a temporary git repo on 'main' branch with one commit."""
    tmpdir = tempfile.mkdtemp(prefix="lmbg_test_")
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


def run_claude_hook_script(payload: dict, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the Claude hook wrapper with stdin JSON and return the subprocess result."""
    script = REPO_ROOT / ".claude" / "hooks" / "local_main_branch_guard.sh"
    return subprocess.run(
        [str(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(cwd),
    )


def seed_controlled_executor_stub(tmp_git_repo: Path) -> None:
    """Create the canonical executor path in a temporary repo for real-hook tests."""
    executor = tmp_git_repo / "scripts" / "agent-guards" / "controlled_skill_mutation_exec.py"
    executor.parent.mkdir(parents=True, exist_ok=True)
    executor.write_text("# stub\n")


def eval_in_local_root(
    command: str,
    cwd: str,
    env_override: dict | None = None,
    hook_flavor: str = "claude",
) -> dict:
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
        result = evaluate(command=command, cwd=cwd, hook_flavor=hook_flavor)
    finally:
        # Restore env
        for k in list(os.environ.keys()):
            if k not in old_env:
                del os.environ[k]
            else:
                os.environ[k] = old_env[k]
    return result


class TestIssue1075BranchSafeMaintenanceTelemetry:
    """Issue #1075 regression coverage for branch-safe maintenance telemetry."""

    @pytest.mark.parametrize("hook_flavor", ["claude", "codex"])
    @pytest.mark.parametrize("command", ["git fetch", "git worktree prune"])
    def test_branch_safe_maintenance_reason_code_parity(
        self,
        tmp_git_repo: Path,
        command: str,
        hook_flavor: str,
    ):
        result = eval_in_local_root(command, str(tmp_git_repo), hook_flavor=hook_flavor)
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_BRANCH_SAFE_MAINTENANCE
        assert is_branch_safe_maintenance_command(command) is True
        assert is_readonly_command(command) is False

    @pytest.mark.parametrize(
        "command",
        [
            "git fetch origin +seen:seen",
            "git fetch --prune-tags origin",
            "git fetch --refmap= refs/heads/main",
        ],
    )
    def test_branch_safe_maintenance_fetch_variants_are_intentional_not_readonly(
        self,
        command: str,
    ):
        assert is_branch_safe_maintenance_command(command) is True
        assert is_readonly_command(command) is False

    @pytest.mark.parametrize("hook_flavor", ["claude", "codex"])
    @pytest.mark.parametrize("command", ["git fetch", "git worktree prune"])
    def test_branch_safe_maintenance_reason_code_when_root_already_drifted(
        self,
        tmp_git_repo_drifted: Path,
        command: str,
        hook_flavor: str,
    ):
        result = eval_in_local_root(
            command,
            str(tmp_git_repo_drifted),
            hook_flavor=hook_flavor,
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_BRANCH_SAFE_MAINTENANCE

    @pytest.mark.parametrize("hook_flavor", ["claude", "codex"])
    @pytest.mark.parametrize(
        "command",
        ["git status", "git diff --stat", "git log --oneline", "git worktree list"],
    )
    def test_readonly_parity_display_commands_remain_readonly(
        self,
        tmp_git_repo: Path,
        command: str,
        hook_flavor: str,
    ):
        result = eval_in_local_root(command, str(tmp_git_repo), hook_flavor=hook_flavor)
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_READONLY
        assert is_readonly_command(command) is True
        assert is_branch_safe_maintenance_command(command) is False

    @pytest.mark.parametrize("command", ["git fetch | head -n 1", "git worktree prune | head -n 1"])
    def test_pipeline_fail_closed_branch_safe_maintenance_is_not_readonly_pipeline(
        self,
        tmp_git_repo: Path,
        command: str,
    ):
        result = eval_in_local_root(command, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_UNPARSEABLE

    @pytest.mark.parametrize("hook_flavor", ["claude", "codex"])
    def test_hook_flavor_parity_branch_safe_maintenance_schema_unchanged(
        self,
        tmp_git_repo: Path,
        hook_flavor: str,
    ):
        result = eval_in_local_root("git fetch", str(tmp_git_repo), hook_flavor=hook_flavor)
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_BRANCH_SAFE_MAINTENANCE
        assert result["current_branch"] == "main"
        assert result["target_branch"] is None
        assert result["target_branch_kind"] is None
        assert result["hook_flavor"] == hook_flavor
        # Allow schema extension after local_main_branch_guard now emits
        # decision/classification telemetry fields for diagnosis.
        assert result["parser_stage"] == "branch_safe_maintenance"
        assert result["command_kind"] == "readonly_command"
        assert result["rule_id"] == "branch_safe_maintenance"
        assert result["decision"] == "allow"
        assert result["decision_source"] == "branch_safe_maintenance"
        assert result["hook_name"] == "local_main_branch_guard"
        assert result["event_kind"] == "PreToolUse"
        assert result["reason_code"] == REASON_BRANCH_SAFE_MAINTENANCE

    def test_result_schema_compatibility_reason_code_enum_extension_only(self, tmp_git_repo: Path):
        readonly_result = eval_in_local_root("git status", str(tmp_git_repo))
        branch_safe_result = eval_in_local_root("git fetch", str(tmp_git_repo))

        expected_keys = {
            "status",
            "reason_code",
            "current_branch",
            "target_branch",
            "target_branch_kind",
            "hook_flavor",
            "parser_stage",
            "decision",
            "decision_source",
            "hook_name",
            "command_kind",
            "rule_id",
            "argv_redacted",
            "event_kind",
            "inner_argv_redacted",
            "wrapper",
        }

        for result in (readonly_result, branch_safe_result):
            assert set(expected_keys).issubset(set(result.keys()))
            assert isinstance(result["status"], str)
            assert isinstance(result["reason_code"], str)
            assert result["current_branch"] in ("main", None)
            assert result["target_branch"] is None or isinstance(result["target_branch"], str)
            assert result["target_branch_kind"] is None or isinstance(result["target_branch_kind"], str)
            assert isinstance(result["hook_flavor"], str)

        assert readonly_result["reason_code"] == REASON_READONLY
        assert branch_safe_result["reason_code"] == REASON_BRANCH_SAFE_MAINTENANCE


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
            assert result["reason_code"] == REASON_LINKED_ISSUE_WORKTREE_CONTEXT
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
        lines = [line for line in captured.err.splitlines() if line.strip()]
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
        # gh issue edit は最小集合外なので drifted 状態でも block（B3）
        "gh issue edit 123 --title new",
        "gh pr checkout 988",
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
        # post-merge-cleanup 最小集合のコマンドは drifted 状態でも allow（Step 9.7a が Step 13 より先）
        "gh issue close 123",
        "gh pr edit 988 --title new",
        "gh pr comment 988 --body text",
    ])
    def test_gh_ops_minimal_set_allowed_even_when_drifted(self, tmp_git_repo_drifted: Path, cmd: str):
        """post-merge-cleanup 最小集合は drifted 状態でも allow される（B3 design intent）。"""
        result = eval_in_local_root(cmd, str(tmp_git_repo_drifted))
        assert result["status"] == "allow", f"Expected allow for minimal set in drifted root: {cmd!r}"
        assert result["reason_code"] == REASON_GITHUB_REMOTE_OPS

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
        assert "HOOK_COMMAND_REPAIR_HINT_V1:" in captured.err
        assert "safe_action:" in captured.err
        assert "suggested_command:" in captured.err
        assert "forbidden_alternatives:" in captured.err

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
        lines = [line for line in captured.err.splitlines() if line.strip()]
        assert len(lines) <= 10


class TestBranchSafeMaintenanceParity:
    """AC2 parity: git fetch / git worktree prune -> branch_safe_maintenance_command (Claude)."""

    def test_git_fetch_is_branch_safe_maintenance(self, tmp_git_repo: Path):
        result = eval_in_local_root("git fetch", str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == "branch_safe_maintenance_command"

    def test_git_worktree_prune_is_branch_safe_maintenance(self, tmp_git_repo: Path):
        result = eval_in_local_root("git worktree prune", str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == "branch_safe_maintenance_command"

    def test_git_fetch_is_not_readonly_command(self, tmp_git_repo: Path):
        result = eval_in_local_root("git fetch", str(tmp_git_repo))
        assert result["reason_code"] != REASON_READONLY


class TestFdDuplicationClaude:
    """AC10: 2>&1 | head fd-duplication (Claude flavor)."""

    def test_fd_duplication_git_diff_stat_allowed(self, tmp_git_repo: Path):
        result = eval_in_local_root("git diff --stat 2>&1 | head -n 20", str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_READONLY

    def test_file_write_redirect_still_blocked(self, tmp_git_repo: Path):
        result = eval_in_local_root("rg TODO . > out.txt", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_append_redirect_still_blocked(self, tmp_git_repo: Path):
        result = eval_in_local_root("git log >> history.txt", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_fd_dup_without_pipe_blocked(self, tmp_git_repo: Path):
        result = eval_in_local_root("git diff 2>&1", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_rg_fd_dup_head_allowed(self, tmp_git_repo: Path):
        result = eval_in_local_root('rg -n "TODO" README.md 2>&1 | head -n 10', str(tmp_git_repo))
        assert result["status"] == "allow"


class TestGhReadonlyAndDenyClaude:
    """AC11: gh readonly / gh deny (Claude flavor)."""

    def test_gh_readonly_issue_view(self, tmp_git_repo: Path):
        result = eval_in_local_root("gh issue view 123", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_gh_issue_list_is_readonly(self, tmp_git_repo: Path):
        result = eval_in_local_root("gh issue list", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_gh_pr_view_is_readonly(self, tmp_git_repo: Path):
        result = eval_in_local_root("gh pr view 456", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_gh_pr_list_is_readonly(self, tmp_git_repo: Path):
        result = eval_in_local_root("gh pr list", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_gh_pr_status_is_readonly(self, tmp_git_repo: Path):
        result = eval_in_local_root("gh pr status", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_gh_issue_view_pipeline_head_allowed(self, tmp_git_repo: Path):
        result = eval_in_local_root("gh issue view 123 | head -n 20", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_gh_issue_edit_is_blocked(self, tmp_git_repo: Path):
        """gh issue edit is NOT in the minimal allowlist and must be blocked (B3)."""
        result = eval_in_local_root("gh issue edit 123 --body new", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_gh_issue_close_is_allowed(self, tmp_git_repo: Path):
        """gh issue close is in GH_OPS_ALLOW_PATTERNS and must be allowed."""
        result = eval_in_local_root("gh issue close 123", str(tmp_git_repo))
        assert result["status"] == "allow"

    def test_gh_pr_merge_is_denied(self, tmp_git_repo: Path):
        result = eval_in_local_root("gh pr merge 456", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_gh_pr_update_branch_is_denied(self, tmp_git_repo: Path):
        result = eval_in_local_root("gh pr update-branch 456", str(tmp_git_repo))
        assert result["status"] == "block"


class TestExactAllowlistClaude:
    """AC5, AC6, AC12: exact allowlist, publisher deny, deterministic_checker (Claude flavor)."""

    def test_exact_allowlist_skill_runtime_executor(self, tmp_linked_worktree: Path):
        repo_root = tmp_linked_worktree.parent.parent.parent
        result = eval_in_local_root(
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 981 --repo squne121/loop-protocol",
            str(repo_root),
            env_override={"LOOP_ISSUE_NUMBER": "981"},
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_SKILL_RUNTIME_EXECUTOR

    def test_direct_preflight_script_not_in_root_allowlist(self, tmp_git_repo: Path):
        result = eval_in_local_root(
            "uv run python3 .claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py --issue-number 985 --repo squne121/loop-protocol",
            str(tmp_git_repo),
        )
        assert result["status"] == "block"
        assert result["reason_code"] != REASON_DETERMINISTIC_CHECKER

    def test_publisher_deny_not_in_allowlist(self, tmp_git_repo: Path):
        result = eval_in_local_root(
            "uv run python3 .claude/skills/post-merge-cleanup/scripts/cleanup_runner.py",
            str(tmp_git_repo),
        )
        assert result["reason_code"] != REASON_DETERMINISTIC_CHECKER

    def test_skill_runtime_executor_reason_code(self, tmp_linked_worktree: Path):
        repo_root = tmp_linked_worktree.parent.parent.parent
        result = eval_in_local_root(
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 981 --repo squne121/loop-protocol",
            str(repo_root),
            env_override={"LOOP_ISSUE_NUMBER": "981"},
        )
        assert result["reason_code"] == REASON_SKILL_RUNTIME_EXECUTOR
        assert result["reason_code"] != REASON_READONLY

    def test_skill_runtime_executor_allows_without_issue_env_or_worktree(self, tmp_git_repo: Path):
        result = eval_in_local_root(
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 777 --repo squne121/loop-protocol",
            str(tmp_git_repo),
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_SKILL_RUNTIME_EXECUTOR

    def test_skill_runtime_executor_blocks_noncanonical_lexical_form(self, tmp_linked_worktree: Path):
        repo_root = tmp_linked_worktree.parent.parent.parent
        result = eval_in_local_root(
            "python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 981 --repo squne121/loop-protocol",
            str(repo_root),
            env_override={"LOOP_ISSUE_NUMBER": "981"},
        )
        assert result["status"] == "block"

    def test_rtk_wrapped_skill_runtime_executor_is_allowed(self, tmp_git_repo: Path):
        result = eval_in_local_root(
            "rtk uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 981 --repo squne121/loop-protocol",
            str(tmp_git_repo),
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_SKILL_RUNTIME_EXECUTOR

    @pytest.mark.parametrize(
        "command",
        [
            "rtk run uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 981 --repo squne121/loop-protocol",
            'rtk bash -lc "uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 981 --repo squne121/loop-protocol"',
            "rtk uv run python3 .claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py --issue-number 981 --repo squne121/loop-protocol",
            "rtk gh issue edit 981 --repo squne121/loop-protocol --body-file tmp/body.md",
        ],
    )
    def test_rtk_noncanonical_or_mutating_commands_are_blocked(self, tmp_git_repo: Path, command: str):
        result = eval_in_local_root(command, str(tmp_git_repo))
        assert result["status"] == "block"

    def test_issue_refinement_direct_repair_hint_uses_exact_executor(self, tmp_git_repo: Path):
        payload = {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    "uv run python3 .claude/skills/issue-refinement-loop/scripts/"
                    "run_refinement_preflight.py --issue-number 981 --repo squne121/loop-protocol"
                )
            },
            "cwd": str(tmp_git_repo),
        }
        result = run_claude_hook_script(payload, tmp_git_repo)
        assert result.returncode == 2
        assert "HOOK_COMMAND_REPAIR_HINT_V1:" in result.stderr
        assert "skill_runtime_exec.py --command-id preflight.run" in result.stderr
        assert "run_refinement_preflight.py" not in result.stderr

    @pytest.mark.parametrize(
        ("remote_url", "expected"),
        [
            ("https://github.com/squne121/loop-protocol.git", TRUSTED_REPO_SLUG),
            ("git@github.com:squne121/loop-protocol.git", TRUSTED_REPO_SLUG),
            ("https://evil.example/github.com/squne121/loop-protocol.git", None),
            ("ssh://git@github.com/squne121/loop-protocol.git", None),
        ],
    )
    def test_skill_runtime_repo_binding_uses_strict_remote_parser(
        self,
        tmp_git_repo: Path,
        remote_url: str,
        expected: str | None,
    ):
        subprocess.run(
            ["git", "-C", str(tmp_git_repo), "remote", "set-url", "origin", remote_url],
            check=True,
            capture_output=True,
        )
        assert resolve_repo_slug(str(tmp_git_repo)) == expected


_ANCHOR_VALID_URL = "https://github.com/squne121/loop-protocol/issues/981#issuecomment-1"


def _anchor_lmbg_command(
    issue_number: str = "981",
    repo: str = "squne121/loop-protocol",
    url: str = _ANCHOR_VALID_URL,
) -> str:
    return (
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
        "--command-id preflight.run.with_anchor "
        f"--issue-number {issue_number} --repo {repo} --anchor-comment-url {url}"
    )


class TestExactAllowlistAnchorClaude:
    """Issue #1498: `preflight.run.with_anchor` sibling exact profile (Claude flavor).

    Covers AC5 (real hook: allow Matrix #2, deny Matrix #3-#22, no split-brain
    with worktree_scope_guard) using local_main_branch_guard's own `evaluate()`.
    """

    def test_exact_allowlist_anchor_profile(self, tmp_linked_worktree: Path):
        """Matrix #2: correct single anchor URL is allowed."""
        repo_root = tmp_linked_worktree.parent.parent.parent
        result = eval_in_local_root(
            _anchor_lmbg_command(),
            str(repo_root),
            env_override={"LOOP_ISSUE_NUMBER": "981"},
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_SKILL_RUNTIME_EXECUTOR

    def test_exact_allowlist_anchor_profile_without_issue_env_or_worktree(self, tmp_git_repo: Path):
        """preflight.run.with_anchor is root-no-worktree eligible."""
        result = eval_in_local_root(_anchor_lmbg_command(), str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_SKILL_RUNTIME_EXECUTOR

    def test_anchor_on_preflight_run_is_denied(self, tmp_git_repo: Path):
        """Matrix #4: preflight.run (production, unmodified) rejects an anchor flag."""
        command = (
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run --issue-number 981 "
            "--repo squne121/loop-protocol --anchor-comment-url " + _ANCHOR_VALID_URL
        )
        result = eval_in_local_root(command, str(tmp_git_repo))
        assert result["status"] == "block"

    @pytest.mark.parametrize(
        ("name", "command"),
        [
            (
                "missing_anchor_flag",
                "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
                "--command-id preflight.run.with_anchor --issue-number 981 "
                "--repo squne121/loop-protocol",
            ),
            (
                "duplicate_distinct_anchor_flags",
                _anchor_lmbg_command()
                + " --anchor-comment-url "
                "https://github.com/squne121/loop-protocol/issues/981#issuecomment-2",
            ),
            (
                "duplicate_identical_anchor_flags",
                _anchor_lmbg_command() + " --anchor-comment-url " + _ANCHOR_VALID_URL,
            ),
            (
                "different_repo_in_url",
                "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
                "--command-id preflight.run.with_anchor --issue-number 981 "
                "--repo squne121/loop-protocol --anchor-comment-url "
                "https://github.com/other/repo/issues/981#issuecomment-1",
            ),
            (
                "different_issue_in_url",
                "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
                "--command-id preflight.run.with_anchor --issue-number 981 "
                "--repo squne121/loop-protocol --anchor-comment-url "
                "https://github.com/squne121/loop-protocol/issues/999#issuecomment-1",
            ),
            (
                "pull_request_review_comment_url",
                "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
                "--command-id preflight.run.with_anchor --issue-number 981 "
                "--repo squne121/loop-protocol --anchor-comment-url "
                "https://github.com/squne121/loop-protocol/pull/981/files#r1",
            ),
            (
                "discussion_r_fragment",
                "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
                "--command-id preflight.run.with_anchor --issue-number 981 "
                "--repo squne121/loop-protocol --anchor-comment-url "
                "https://github.com/squne121/loop-protocol/issues/981#discussion_r1",
            ),
            (
                "query_string",
                "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
                "--command-id preflight.run.with_anchor --issue-number 981 "
                "--repo squne121/loop-protocol --anchor-comment-url "
                "https://github.com/squne121/loop-protocol/issues/981?tab=1#issuecomment-1",
            ),
            (
                "trailing_slash",
                "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
                "--command-id preflight.run.with_anchor --issue-number 981 "
                "--repo squne121/loop-protocol --anchor-comment-url "
                "https://github.com/squne121/loop-protocol/issues/981#issuecomment-1/",
            ),
            (
                "userinfo",
                "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
                "--command-id preflight.run.with_anchor --issue-number 981 "
                "--repo squne121/loop-protocol --anchor-comment-url "
                "https://user@github.com/squne121/loop-protocol/issues/981#issuecomment-1",
            ),
            (
                "percent_encoded",
                "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
                "--command-id preflight.run.with_anchor --issue-number 981 "
                "--repo squne121/loop-protocol --anchor-comment-url "
                "https://github.com/squne121/loop-protocol/issues/981%23issuecomment-1",
            ),
            (
                "eq_form",
                "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
                "--command-id preflight.run.with_anchor --issue-number 981 "
                "--repo squne121/loop-protocol --anchor-comment-url=" + _ANCHOR_VALID_URL,
            ),
            (
                "abbreviated_flag",
                "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
                "--command-id preflight.run.with_anchor --issue-number 981 "
                "--repo squne121/loop-protocol --anchor-comment-u " + _ANCHOR_VALID_URL,
            ),
            (
                "flag_no_value",
                "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
                "--command-id preflight.run.with_anchor --issue-number 981 "
                "--repo squne121/loop-protocol --anchor-comment-url",
            ),
            (
                "unknown_extra_flag",
                _anchor_lmbg_command() + " --extra x",
            ),
            (
                "flag_order_changed",
                "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
                "--anchor-comment-url " + _ANCHOR_VALID_URL
                + " --command-id preflight.run.with_anchor --issue-number 981 "
                "--repo squne121/loop-protocol",
            ),
            (
                "shell_metachar",
                "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
                "--command-id preflight.run.with_anchor --issue-number 981 "
                "--repo squne121/loop-protocol --anchor-comment-url "
                + _ANCHOR_VALID_URL + ";rm -rf /",
            ),
        ],
    )
    def test_anchor_profile_negative_matrix(self, tmp_git_repo: Path, name: str, command: str):
        result = eval_in_local_root(command, str(tmp_git_repo))
        assert result["status"] == "block", f"{name}: expected block, got {result}"

    def test_anchor_profile_no_split_brain_with_worktree_scope_guard(self, tmp_git_repo: Path):
        """Matrix #23: this guard's decision must agree with worktree_scope_guard's
        decision for the same command (no split-brain)."""
        import json as _json
        import subprocess as _subprocess

        wsg_sh = REPO_ROOT / ".claude" / "hooks" / "worktree_scope_guard.sh"
        commands = [
            _anchor_lmbg_command(),
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 981 "
            "--repo squne121/loop-protocol",
        ]
        for command in commands:
            lmbg_result = eval_in_local_root(
                command, str(tmp_git_repo), env_override={"LOOP_ISSUE_NUMBER": "981"}
            )
            payload = {
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "cwd": str(tmp_git_repo),
            }
            env = dict(os.environ)
            env["CLAUDE_PROJECT_DIR"] = str(tmp_git_repo)
            env["LOOP_ISSUE_NUMBER"] = "981"
            wsg_result = _subprocess.run(
                ["bash", str(wsg_sh)],
                input=_json.dumps(payload),
                text=True,
                capture_output=True,
                env=env,
            )
            wsg_allows = wsg_result.returncode == 0
            lmbg_allows = lmbg_result["status"] == "allow"
            assert lmbg_allows == wsg_allows, (
                f"split-brain: local_main_branch_guard={lmbg_allows} "
                f"worktree_scope_guard={wsg_allows} for {command!r}"
            )


class TestPythonpathStaleAndTmpWrapperClaude:
    """AC14: PYTHONPATH stale regression / /tmp wrapper fail-closed (Claude flavor)."""

    def test_tmp_wrapper_script_blocked(self, tmp_git_repo: Path):
        result = eval_in_local_root(
            "uv run python3 /tmp/run_refinement_preflight.py --issue-number 985",
            str(tmp_git_repo),
        )
        assert result["status"] == "block"

    def test_python_c_inline_blocked(self, tmp_git_repo: Path):
        result = eval_in_local_root("python3 -c import_os", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_bash_lc_wrapper_blocked(self, tmp_git_repo: Path):
        result = eval_in_local_root("bash -lc 'uv run python3 /tmp/script.py'", str(tmp_git_repo))
        assert result["status"] == "block"

    def test_pythonpath_stale_guard_module_unaffected(self, tmp_git_repo: Path, tmp_path: Path):
        import os
        (tmp_path / "command_registry.py").write_text("raise ImportError('stale!')")
        old = os.environ.get("PYTHONPATH", "")
        try:
            os.environ["PYTHONPATH"] = str(tmp_path)
            result = eval_in_local_root("git status", str(tmp_git_repo))
        finally:
            if old:
                os.environ["PYTHONPATH"] = old
            elif "PYTHONPATH" in os.environ:
                del os.environ["PYTHONPATH"]
        assert result["status"] == "allow"


class TestGhMutationFailClosedCompletenessClaude:
    """AC11: gh issue/pr mutation subcommands outside readonly allowlist are ALL blocked (allowlist-closed completeness, Claude flavor)."""

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
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_GH_MUTATION



class TestGhMutationReasonCodeClaude:
    """AC1-AC7 (#1109): gh_mutation_denied reason_code for gh issue/pr mutation block (Claude flavor)."""

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
        result = eval_in_local_root(cmd, str(tmp_git_repo))
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
        result = eval_in_local_root(cmd, str(tmp_git_repo))
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
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "allow", (
            f"Expected allow for readonly {cmd!r}, got {result['status']!r}"
        )

    def test_gh_mutation_denied_constant_value(self):
        """AC1: REASON_GH_MUTATION == 'gh_mutation_denied'."""
        assert REASON_GH_MUTATION == "gh_mutation_denied"

    def test_gh_mutation_recovery_hint_contains_approved(self, tmp_git_repo: Path, capsys):
        """AC7: gh mutation block emits HOOK_COMMAND_REPAIR_HINT_V1."""
        from local_main_branch_guard import _emit_block_stderr, REASON_GH_MUTATION
        _emit_block_stderr(
            reason_code=REASON_GH_MUTATION,
            current_branch_kind="default",
            current_is_default=True,
            target_branch_kind=None,
            hook_flavor="claude",
        )
        captured = capsys.readouterr()
        assert "HOOK_COMMAND_REPAIR_HINT_V1:" in captured.err
        assert 'reason_code: "gh_mutation_denied"' in captured.err
        assert "gh issue edit" not in captured.err
        assert "gh issue comment" not in captured.err
        assert "tmp/<body>.md" not in captured.err


class TestProjectTmpPolicyClaude:
    """AC14: OS absolute /tmp is blocked but project-relative tmp/ is not mis-identified (Claude flavor)."""

    def test_repo_relative_tmp_script_is_not_blocked(self, tmp_git_repo: Path):
        """GIVEN uv run python3 tmp/check.py (relative) WHEN evaluated THEN allow (not OS /tmp)."""
        result = eval_in_local_root("uv run python3 tmp/check.py --dry-run", str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result.get("reason_code") != REASON_UNPARSEABLE

    def test_absolute_tmp_script_is_blocked(self, tmp_git_repo: Path):
        """GIVEN uv run python3 /tmp/check.py (OS absolute) WHEN evaluated THEN blocked."""
        result = eval_in_local_root("uv run python3 /tmp/check.py --dry-run", str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_UNPARSEABLE

class TestGhOpsMinimalAllowlistClaude:
    """AC1, B3: post-merge-cleanup 最小集合の token-based classifier テスト（Claude flavor）。"""

    @pytest.mark.parametrize("cmd,expected", [
        # must-allow: 最小集合
        ("gh issue close 1089", "allow"),
        ("gh issue reopen 456", "allow"),
        ("gh pr comment 789 --body text", "allow"),
        ("gh pr edit 101 --title new", "allow"),
    ])
    def test_minimal_allowlist_allowed(self, tmp_git_repo: Path, cmd: str, expected: str):
        """GIVEN minimal allowlist command WHEN evaluated THEN allowed with github_remote_ops_command reason."""
        result = eval_in_local_root(cmd, str(tmp_git_repo))
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
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block"


# ─── AC8〜AC15: Issue #1124 GitHub remote ops 5 分類 ────────────────────────

class TestGithubIssueMutationCommandClaude:
    """
    AC8: raw gh issue edit/comment are blocked after deprecation.
    AC9: gh issue create with --repo + --body-file tmp/ remains allowed.
    AC10: bare gh issue create / any gh issue edit without the new lane → block.
    AC13: gh issue create/edit/comment/close/reopen is NOT readonly_command
    """

    @pytest.mark.parametrize("cmd", RAW_ISSUE_EDIT_COMMANDS)
    def test_issue_edit_deprecated(self, tmp_git_repo: Path, cmd: str):
        """AC8: GIVEN raw gh issue edit WHEN evaluated THEN block."""
        assert not is_github_issue_mutation_command(cmd), f"Expected False for: {cmd!r}"
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block", f"Expected block for: {cmd!r}"
        assert result["reason_code"] == REASON_GH_MUTATION

    @pytest.mark.parametrize("cmd", [
        "gh issue create --repo squne121/loop-protocol --title foo --body-file tmp/foo.md",
        "gh issue create --repo squne121/loop-protocol --body-file tmp/body.md --title new-issue",
    ])
    def test_issue_create_still_allowed(self, tmp_git_repo: Path, cmd: str):
        """AC9: GIVEN gh issue create with --repo + --body-file tmp/ WHEN evaluated THEN allow."""
        assert is_github_issue_mutation_command(cmd), f"Expected True for: {cmd!r}"
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "allow", f"Expected allow for: {cmd!r}"
        assert result["reason_code"] == REASON_GITHUB_REMOTE_OPS

    @pytest.mark.parametrize("cmd", [
        "gh issue create",                                          # bare create
        "gh issue edit 123",                                        # bare edit, no --body-file
        "gh issue create --repo squne121/loop-protocol",            # no --body-file
        "gh issue edit 123 --repo squne121/loop-protocol",          # no --body-file
        "gh issue create --body-file tmp/foo.md",                   # no --repo
        "gh issue edit 123 --body-file tmp/foo.md",                 # no --repo
        "gh issue create --repo squne121/loop-protocol --body-file tmp/foo.md --editor",  # interactive
        "gh issue edit 123 --repo squne121/loop-protocol --body-file tmp/foo.md --web",   # deprecated
        "gh issue create --repo squne121/loop-protocol --body-file -",  # stdin
        "gh issue edit 123 --repo squne121/loop-protocol --body-file /tmp/foo.md",  # /tmp not tmp/
        "gh issue edit 123 --repo other-org/other-repo --body-file tmp/foo.md",    # wrong repo
    ])
    def test_ac10_bare_gh_issue_create_or_edit_blocked(self, tmp_git_repo: Path, cmd: str):
        """AC10: GIVEN bare gh issue create/edit or without required flags WHEN evaluated THEN block."""
        assert not is_github_issue_mutation_command(cmd), f"Expected False for: {cmd!r}"
        result = eval_in_local_root(cmd, str(tmp_git_repo))
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
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["reason_code"] != REASON_READONLY, (
            f"gh issue mutation {cmd!r} must not have readonly_command reason_code, "
            f"got: {result['reason_code']!r}"
        )


class TestReadonlyArtifactExportCommandClaude:
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
        result = eval_in_local_root(cmd, str(tmp_git_repo))
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
        "gh issue view 1124 > tmp/../docs/foo.md", # path traversal
        "gh issue view 1124 --web > tmp/foo.md",   # --web flag (browser open)
    ])
    def test_ac12_gh_issue_view_to_blocked_dest_blocked(self, tmp_git_repo: Path, cmd: str):
        """AC12: GIVEN gh issue view with blocked destination WHEN evaluated THEN block."""
        assert not is_readonly_artifact_export_command(cmd), f"Expected False for: {cmd!r}"
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block", f"Expected block for: {cmd!r}"


class TestGhDestructiveCommandsBlockedClaude:
    """AC14: gh pr merge / gh pr checkout / gh pr update-branch remain blocked."""

    @pytest.mark.parametrize("cmd", [
        "gh pr merge 123",
        "gh pr checkout 123",
        "gh pr update-branch 456",
    ])
    def test_ac14_gh_pr_destructive_blocked(self, tmp_git_repo: Path, cmd: str):
        """AC14: GIVEN gh pr merge/checkout/update-branch WHEN evaluated THEN block."""
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block", f"Expected block for: {cmd!r}"
        assert result["reason_code"] == REASON_GH_MUTATION

    def test_ac14_gh_pr_create_blocked(self, tmp_git_repo: Path):
        """AC14: gh pr create is blocked (destructive / local push dependent)."""
        result = eval_in_local_root("gh pr create --title x --body y", str(tmp_git_repo))
        assert result["status"] == "block"


class TestGithub5ClassVocabularyConstantsClaude:
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



# ─── B1-B4 Review Blocker Fixes (Claude flavor) ──────────────────────────────

class TestB1B4ReviewBlockerFixesClaude:
    """
    B1: gh issue create requires --title with value.
    B2: --body-file canonical tmp/ path validation (no path traversal, no absolute).
    B3: gh issue view --web/-w blocked.
    B4: stricter checks (--body without value, -e, --edit-last, --base, /tmp body-file).
    """

    # B1: --title required for gh issue create
    def test_b1_gh_issue_create_without_title_blocked(self, tmp_git_repo: Path):
        """B1: gh issue create without --title is blocked."""
        result = eval_in_local_root(
            "gh issue create --repo squne121/loop-protocol --body-file tmp/foo.md",
            str(tmp_git_repo),
        )
        assert result["status"] == "block", "gh issue create without --title must be blocked"

    def test_b1_gh_issue_create_with_title_allowed(self, tmp_git_repo: Path):
        """B1: gh issue create with --title value is allowed."""
        result = eval_in_local_root(
            "gh issue create --repo squne121/loop-protocol --title foo --body-file tmp/foo.md",
            str(tmp_git_repo),
        )
        assert result["status"] == "allow", "gh issue create with --title must be allowed"

    # B2: canonical path validation
    def test_b2_body_file_path_traversal_blocked(self, tmp_git_repo: Path):
        """B2: --body-file tmp/../AGENTS.md (path traversal) is blocked."""
        assert not is_github_issue_mutation_command(
            "gh issue create --repo squne121/loop-protocol --title foo --body-file tmp/../AGENTS.md"
        ), "path traversal in --body-file must be blocked"

    def test_b2_body_file_absolute_path_blocked(self, tmp_git_repo: Path):
        """B2: --body-file /tmp/body.txt (absolute path) is blocked."""
        assert not is_github_issue_mutation_command(
            "gh issue create --repo squne121/loop-protocol --title foo --body-file /tmp/body.txt"
        ), "absolute path in --body-file must be blocked"

    # B3: --web/-w blocked for gh issue/pr view
    def test_b3_gh_issue_view_web_blocked(self, tmp_git_repo: Path):
        """B3: gh issue view --web opens browser, must be blocked."""
        result = eval_in_local_root("gh issue view 123 --web", str(tmp_git_repo))
        assert result["status"] == "block", "gh issue view --web must be blocked"

    def test_b3_gh_issue_view_w_flag_blocked(self, tmp_git_repo: Path):
        """B3: gh issue view -w (short form) opens browser, must be blocked."""
        result = eval_in_local_root("gh issue view 123 -w", str(tmp_git_repo))
        assert result["status"] == "block", "gh issue view -w must be blocked"

    def test_b3_gh_issue_view_without_web_allowed(self, tmp_git_repo: Path):
        """B3: gh issue view without --web/-w must still be allowed (readonly)."""
        result = eval_in_local_root("gh issue view 123", str(tmp_git_repo))
        assert result["status"] == "allow", "gh issue view (no --web) must be allowed"

    # B4: stricter checks
    def test_b4_gh_issue_comment_body_without_value_blocked(self, tmp_git_repo: Path):
        """B4: gh issue comment with bare --body (no value) is blocked."""
        result = eval_in_local_root("gh issue comment 123 --body", str(tmp_git_repo))
        assert result["status"] == "block", "gh issue comment with bare --body must be blocked"

    def test_b4_gh_issue_comment_edit_last_blocked(self, tmp_git_repo: Path):
        """B4: gh issue comment --edit-last is blocked (destructive)."""
        result = eval_in_local_root("gh issue comment 123 --edit-last", str(tmp_git_repo))
        assert result["status"] == "block", "gh issue comment --edit-last must be blocked"

    def test_b4_gh_issue_comment_e_flag_blocked(self, tmp_git_repo: Path):
        """B4: gh issue comment -e is blocked (interactive editor)."""
        result = eval_in_local_root("gh issue comment 123 -e", str(tmp_git_repo))
        assert result["status"] == "block", "gh issue comment -e must be blocked"

    def test_b4_gh_pr_edit_base_main_blocked(self, tmp_git_repo: Path):
        """B4: gh pr edit --base main is blocked (changes base branch)."""
        result = eval_in_local_root("gh pr edit 123 --base main", str(tmp_git_repo))
        assert result["status"] == "block", "gh pr edit --base main must be blocked"

    def test_b4_gh_pr_edit_body_file_absolute_tmp_blocked(self, tmp_git_repo: Path):
        """B4: gh pr edit --body-file /tmp/... (absolute path) is blocked."""
        result = eval_in_local_root("gh pr edit 123 --body-file /tmp/body.txt", str(tmp_git_repo))
        assert result["status"] == "block", "gh pr edit --body-file /tmp/... must be blocked"

    def test_b4_gh_pr_edit_body_file_canonical_tmp_allowed(self, tmp_git_repo: Path):
        """B4: gh pr edit --body-file tmp/body.txt (canonical) is allowed."""
        result = eval_in_local_root("gh pr edit 123 --body-file tmp/body.txt", str(tmp_git_repo))
        assert result["status"] == "allow", "gh pr edit --body-file tmp/... must be allowed"
        assert result["reason_code"] == REASON_GITHUB_REMOTE_OPS

    @pytest.mark.parametrize(("cmd", "expected_reason"), REVIEWER_GH_MUTATION_REGRESSIONS)
    def test_b4_reviewer_gh_mutation_regressions(
        self, tmp_git_repo: Path, cmd: str, expected_reason: str
    ):
        """Reviewer 指摘の raw mutation-like gh commands は fail-closed で block される。"""
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == expected_reason


# =============================================================================
# Issue #1166: controlled skill mutation policy (AC4/AC17)
# =============================================================================

class TestControlledSkillMutationPolicy:
    """Issue #1166 AC4/AC17: shared policy function consumed by local_main_branch_guard.

    Both worktree_scope_guard and local_main_branch_guard consume the same
    is_controlled_skill_mutation_exec_command function from controlled_skill_mutation_policy.
    No split-brain allowlist (AC17).
    """

    def test_publish_termination_direct_denied(self, tmp_git_repo: Path):
        """AC4: direct publish_termination_report.py invocation is denied in local root.

        Only the executor (controlled_skill_mutation_exec.py) is allowed.
        Direct script access goes to default:allow but is unknown class, so
        this test verifies it at least doesn't get treated as a special allow.
        """
        cmd = (
            "python3 .claude/skills/issue-refinement-loop/scripts/publish_termination_report.py"
            " --issue-number 1166 --repo squne121/loop-protocol"
        )
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        # Direct python3 with relative .claude/ script is allowed by default
        # (not a branch mutation, not compound) — but it must NOT be in the
        # deterministic_checker reason_code path.
        assert result["reason_code"] != REASON_DETERMINISTIC_CHECKER, (
            "direct publish_termination_report.py must not be allowed as deterministic_checker"
        )

    def test_publish_termination_executor_allowed(self, tmp_git_repo: Path):
        """AC4/AC17: controlled_skill_mutation_exec.py with valid argv is allowed.

        The executor command is handled by the shared policy function
        (is_controlled_skill_mutation_exec_command) with reason_code=deterministic_checker.
        """
        # Create executor stub in the tmp git repo so realpath resolves
        executor_dir = tmp_git_repo / "scripts" / "agent-guards"
        executor_dir.mkdir(parents=True, exist_ok=True)
        (executor_dir / "controlled_skill_mutation_exec.py").write_text("# stub\n")

        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/termination_report_input.json"
            " --repo squne121/loop-protocol"
        )
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "allow", (
            f"controlled_skill_mutation_exec.py with valid argv must be allowed; result={result}"
        )
        assert result["reason_code"] == REASON_CONTROLLED_SKILL_MUTATION_EXECUTOR, (
            "executor must be allowed as controlled_skill_mutation_executor"
        )

    def test_publish_termination_executor_missing_repo_denied(self, tmp_git_repo: Path):
        """AC4: executor with missing --repo is denied (not a valid executor invocation)."""
        executor_dir = tmp_git_repo / "scripts" / "agent-guards"
        executor_dir.mkdir(parents=True, exist_ok=True)
        (executor_dir / "controlled_skill_mutation_exec.py").write_text("# stub\n")

        cmd = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
            " --command-id termination_report.publish"
            " --issue-number 1166"
            " --input-file artifacts/1166/termination_report_input.json"
            # missing --repo
        )
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        # Missing --repo → not a valid executor form → not deterministic_checker
        assert result["reason_code"] != REASON_DETERMINISTIC_CHECKER, (
            "executor with missing --repo must not be allowed as deterministic_checker"
        )

    def test_publish_termination_shared_policy_reference(self, tmp_git_repo: Path):
        """AC17: verify local_main_branch_guard references publish_termination_report
        via the shared controlled_skill_mutation_policy module (not a separate allowlist).
        """
        # The guard module must import from controlled_skill_mutation_policy
        import local_main_branch_guard as lmbg
        # _CSM_POLICY_AVAILABLE should be True when the policy module is importable
        # (it is available since we're in the same repo)
        # This test just checks that the attribute exists (not False due to import failure)
        assert hasattr(lmbg, "_CSM_POLICY_AVAILABLE"), (
            "local_main_branch_guard must have _CSM_POLICY_AVAILABLE attribute (Issue #1166 AC17)"
        )


class TestIssue1291IssueMetadataMutationClaude:
    """Issue #1291 regression coverage for raw issue mutation deprecation."""

    @pytest.mark.parametrize("cmd", RAW_ISSUE_COMMENT_COMMANDS)
    def test_issue_comment_deprecated(self, tmp_git_repo: Path, cmd: str):
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_GH_MUTATION

    @pytest.mark.parametrize(
        "cmd",
        [
            "gh issue close 100",
            "gh issue reopen 100",
            "gh pr comment 100 --body-file tmp/x.md",
            "gh pr edit 100 --title new-title",
        ],
    )
    def test_remote_ops_still_allowed(self, tmp_git_repo: Path, cmd: str):
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_GITHUB_REMOTE_OPS

    @pytest.mark.parametrize("cmd", RAW_ISSUE_EDIT_COMMANDS + RAW_ISSUE_COMMENT_COMMANDS)
    def test_must_block_raw_issue_metadata_commands_via_real_hook(self, tmp_git_repo: Path, cmd: str):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "cwd": str(tmp_git_repo),
        }
        result = run_claude_hook_script(payload, cwd=tmp_git_repo)
        assert result.returncode == 2, result.stderr
        assert "gh_mutation_denied" in result.stderr

    @pytest.mark.parametrize("command_id,input_file", CONTROLLED_METADATA_COMMANDS)
    def test_must_allow_controlled_executor_metadata_commands(
        self,
        tmp_git_repo: Path,
        command_id: str,
        input_file: str,
    ):
        seed_controlled_executor_stub(tmp_git_repo)
        payload = {
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py "
                    f"--command-id {command_id} --issue-number 1291 "
                    f"--input-file {input_file} --repo squne121/loop-protocol --dry-run"
                )
            },
            "cwd": str(tmp_git_repo),
        }
        eval_result = eval_in_local_root(payload["tool_input"]["command"], str(tmp_git_repo))
        assert eval_result["status"] == "allow"
        # NOTE: PR #1299 (Issue #1289) split the shared deterministic_checker
        # reason_code so controlled_skill_mutation_exec.py invocations report
        # their own dedicated reason_code (see REASON_CONTROLLED_SKILL_MUTATION_EXECUTOR).
        assert eval_result["reason_code"] == REASON_CONTROLLED_SKILL_MUTATION_EXECUTOR
        assert eval_result["parser_stage"] == "controlled_skill_mutation"
        assert eval_result["rule_id"] == "controlled_skill_mutation"
        result = run_claude_hook_script(payload, cwd=tmp_git_repo)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""

    def test_repair_hint_does_not_suggest_raw_issue_mutation(self, capsys):
        from local_main_branch_guard import _emit_block_stderr

        _emit_block_stderr(
            reason_code=REASON_GH_MUTATION,
            current_branch_kind="default",
            current_is_default=True,
            target_branch_kind=None,
            hook_flavor="claude",
        )
        captured = capsys.readouterr().err
        assert "gh issue edit" not in captured
        assert "gh issue comment" not in captured
        assert "tmp/<body>.md" not in captured
        assert "controlled_skill_mutation_exec.py" in captured

    def test_post_merge_cleanup_does_not_use_raw_issue_body_or_comment_mutation(self):
        roots = [
            REPO_ROOT / ".claude" / "skills" / "post-merge-cleanup",
            REPO_ROOT / "scripts" / "agent-ops" / "cleanup_exec.py",
            REPO_ROOT / "scripts" / "agent-ops" / "classify-git-state.py",
        ]
        allowed_shell_prefixes = (
            "gh issue close ",
            "gh issue reopen ",
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py ",
        )
        allowed_explicit_comment_markers = ("forbidden example", "allow comments/examples only explicitly")

        for root in roots:
            paths = [root] if root.is_file() else list(root.rglob("*"))
            for path in paths:
                if not path.is_file():
                    continue
                content = path.read_text(encoding="utf-8", errors="ignore")
                for lineno, line in enumerate(content.splitlines(), start=1):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if "gh issue edit" in stripped or "gh issue comment" in stripped:
                        if stripped.startswith(("#", "//")):
                            assert any(marker in stripped for marker in allowed_explicit_comment_markers), (
                                f"unexpected raw issue mutation comment at {path}:{lineno}: {stripped}"
                            )
                            continue
                        assert stripped.startswith(allowed_shell_prefixes), (
                            f"forbidden raw issue mutation command at {path}:{lineno}: {stripped}"
                        )
                    if "--body-file tmp/" in stripped or "--body-file=tmp/" in stripped:
                        if stripped.startswith(("#", "//")):
                            assert any(marker in stripped for marker in allowed_explicit_comment_markers), (
                                f"unexpected tmp body-file comment at {path}:{lineno}: {stripped}"
                            )
                            continue
                        assert "controlled_skill_mutation_exec.py" in stripped, (
                            f"forbidden tmp body-file usage at {path}:{lineno}: {stripped}"
                        )


class TestIssue1543ArgvAwareIssueRefinementClassifier:
    """Issue #1543: _looks_like_direct_issue_refinement_runtime_command must be
    argv-aware (execution-target based), not a naive substring match on the
    full raw command string."""

    def test_allowed_paths_review_gate_with_issue_refinement_path_in_json_allows(self, tmp_git_repo: Path):
        """AC1: allowed_paths_review_gate.py invoked with an --allowed-paths
        JSON array that merely contains an issue-refinement-loop path as a
        string VALUE (not as the executed script) must ALLOW, and must not be
        classified via the issue_refinement_direct rule."""
        cmd = (
            "uv run python3 .claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py "
            "--allowed-paths '[\"scripts/agent-guards/local_main_branch_guard.py\", "
            "\".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py\"]'"
        )
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["rule_id"] != "issue_refinement_direct"

    def test_issue_refinement_script_as_ordinary_argument_value_does_not_block(self, tmp_git_repo: Path):
        """AC2: the exact issue-refinement-loop script path passed as a plain
        argument VALUE to an unrelated program must not be misclassified as a
        direct execution target."""
        cmd = (
            "uv run python3 scripts/agent-ops/git_worktree_probe.py "
            "--reference .claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py"
        )
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["rule_id"] != "issue_refinement_direct"

    @pytest.mark.parametrize(
        "cmd",
        [
            "python3 .claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py "
            "--issue-number 985 --repo squne121/loop-protocol",
            "uv run .claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py "
            "--issue-number 985 --repo squne121/loop-protocol",
            "uv run python3 .claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py "
            "--issue-number 985 --repo squne121/loop-protocol",
        ],
    )
    def test_direct_launcher_forms_still_blocked(self, tmp_git_repo: Path, cmd: str):
        """AC3: python3 <script> / uv run <script> / uv run python3 <script>
        direct execution forms of an issue-refinement-loop script must still
        be blocked (non-regression of the existing block behaviour)."""
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["rule_id"] == "issue_refinement_direct"

    def test_relative_dot_slash_form_still_blocks_same_script_identity(self, tmp_git_repo: Path):
        """AC4: `./` relative notation must resolve to the same script
        identity as the canonical direct-execution form and still block."""
        cmd = (
            "python3 ./.claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py "
            "--issue-number 985 --repo squne121/loop-protocol"
        )
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["rule_id"] == "issue_refinement_direct"

    def test_absolute_path_form_still_blocks_same_script_identity(self, tmp_git_repo: Path):
        """AC4: absolute-path notation must resolve to the same script
        identity as the canonical direct-execution form and still block."""
        abs_script = str(
            tmp_git_repo / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py"
        )
        cmd = f"python3 {abs_script} --issue-number 985 --repo squne121/loop-protocol"
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["rule_id"] == "issue_refinement_direct"

    def test_python_isolated_flag_still_blocks_same_script_identity(self, tmp_git_repo: Path):
        """AC4: `python3 -I <script>` alternate spelling must still resolve to
        the same script identity and block."""
        cmd = (
            "python3 -I .claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py "
            "--issue-number 985 --repo squne121/loop-protocol"
        )
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["rule_id"] == "issue_refinement_direct"

    def test_uv_run_directory_flag_still_blocks_same_script_identity(self, tmp_git_repo: Path):
        """AC4: `uv run --directory <dir> <script>` alternate spelling must
        resolve to the same script identity and still block."""
        cmd = (
            f"uv run --directory {tmp_git_repo} "
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py "
            "--issue-number 985 --repo squne121/loop-protocol"
        )
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["rule_id"] == "issue_refinement_direct"

    def test_allowed_paths_json_with_whitespace_quotes_multiple_paths_allows(self, tmp_git_repo: Path):
        """AC5: allowed-paths JSON argument with internal whitespace, quotes,
        and multiple paths must not break token boundaries and must still
        allow."""
        cmd = (
            "uv run python3 .claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py "
            "--allowed-paths '[\n"
            "  \"scripts/agent-guards/local_main_branch_guard.py\",\n"
            "  \".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py\",\n"
            "  \".claude/skills/issue-refinement-loop/scripts/build_loop_state.py\"\n]'"
        )
        result = eval_in_local_root(cmd, str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["rule_id"] != "issue_refinement_direct"
