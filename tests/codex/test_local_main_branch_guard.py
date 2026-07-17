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

from local_main_branch_guard import (  # noqa: E402
    evaluate,
    REASON_DRIFT,
    REASON_RECOVERY,
    REASON_NOT_LOCAL_ROOT,
    REASON_LINKED_ISSUE_WORKTREE_CONTEXT,
    REASON_READONLY,
    REASON_UNPARSEABLE,
    REASON_GH_API,
    REASON_DETERMINISTIC_CHECKER,
    REASON_CONTROLLED_SKILL_MUTATION_EXECUTOR,
    REASON_GITHUB_REMOTE_OPS,
    REASON_GH_MUTATION,
    REASON_SKILL_RUNTIME_EXECUTOR,
    REASON_UNKNOWN_ALLOWED,
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

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "hooks"

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
    ("issue_body.update", "artifacts/1291/issue-metadata/issue_body.update/input.json"),
    ("issue_comment.publish", "artifacts/1291/issue-metadata/issue_comment.publish/input.json"),
    (
        "contract_snapshot.publish",
        "artifacts/1291/issue-metadata/contract_snapshot.publish/input.json",
    ),
]


def _load_fixture_json(filename: str) -> dict:
    return json.loads((FIXTURE_DIR / filename).read_text())


def _load_fixture_text(filename: str) -> str:
    return (FIXTURE_DIR / filename).read_text().strip()


# ─── Fixtures ─────────────────────────────────────────────────────────────────


def make_pretool_codex(command: str, cwd: str, event: str = "PreToolUse") -> dict:
    """Build a minimal Codex PreToolUse / PermissionRequest JSON payload."""
    return {
        "event": event,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": cwd,
    }


def eval_codex(
    command: str,
    cwd: str,
    event: str = "PreToolUse",
    env_override: dict[str, str] | None = None,
) -> dict:
    """
    Evaluate a Codex hook input.
    Sets CLAUDE_PROJECT_DIR so is_local_root_context returns True for cwd.
    """
    old = os.environ.get("CLAUDE_PROJECT_DIR", "")
    previous: dict[str, str | None] = {}
    try:
        os.environ["CLAUDE_PROJECT_DIR"] = cwd
        if env_override:
            for key, value in env_override.items():
                previous[key] = os.environ.get(key)
                os.environ[key] = value
        result = evaluate(command=command, cwd=cwd, hook_flavor="codex", event_kind=event)
    finally:
        if old:
            os.environ["CLAUDE_PROJECT_DIR"] = old
        elif "CLAUDE_PROJECT_DIR" in os.environ:
            del os.environ["CLAUDE_PROJECT_DIR"]
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return result


def run_guard_script(payload: dict, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run local_main_branch_guard.py as hook stdin script and return process result."""
    script = REPO_ROOT / "scripts" / "agent-guards" / "local_main_branch_guard.py"
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(cwd),
    )


def seed_controlled_executor_stub(tmp_git_repo: Path) -> None:
    executor = tmp_git_repo / "scripts" / "agent-guards" / "controlled_skill_mutation_exec.py"
    executor.parent.mkdir(parents=True, exist_ok=True)
    executor.write_text("# stub\n")


@pytest.fixture
def tmp_git_repo() -> Path:
    """Temporary git repo on 'main' branch."""
    tmpdir = tempfile.mkdtemp(prefix="lmbg_codex_test_")
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
        subprocess.run(["git", "-C", tmpdir, "branch", "issue-981-codex-test"], check=True, capture_output=True)
        yield Path(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def tmp_linked_worktree(tmp_git_repo: Path) -> Path:
    """Linked worktree under .claude/worktrees for exact executor parity tests."""
    wt_path = tmp_git_repo / ".claude" / "worktrees" / "issue-981-codex-test"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(tmp_git_repo), "worktree", "add", str(wt_path), "issue-981-codex-test"],
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
        bash_entry = next((e for e in pretool if e.get("matcher") == "^Bash$"), None)
        assert bash_entry is not None, "PreToolUse must have ^Bash$ matcher"
        commands = [h.get("command", "") for h in bash_entry.get("hooks", [])]
        assert any("local_main_branch_guard" in cmd for cmd in commands), (
            "PreToolUse ^Bash$ must include local_main_branch_guard hook"
        )

        # Check PermissionRequest
        perm_req = hooks_root.get("PermissionRequest", [])
        bash_perm = next((e for e in perm_req if e.get("matcher") == "^Bash$"), None)
        assert bash_perm is not None, "PermissionRequest must have ^Bash$ matcher"
        perm_commands = [h.get("command", "") for h in bash_perm.get("hooks", [])]
        assert any("local_main_branch_guard" in cmd for cmd in perm_commands), (
            "PermissionRequest ^Bash$ must include local_main_branch_guard hook"
        )

    def test_codex_hook_script_exists(self):
        """Codex hook script .codex/hooks/local_main_branch_guard.sh must exist."""
        script_path = REPO_ROOT / ".codex" / "hooks" / "local_main_branch_guard.sh"
        assert script_path.exists(), f"Codex hook script not found: {script_path}"

    def test_guard_script_exists(self):
        """Shared guard script scripts/agent-guards/local_main_branch_guard.py must exist."""
        guard_path = REPO_ROOT / "scripts" / "agent-guards" / "local_main_branch_guard.py"
        assert guard_path.exists(), f"Guard script not found: {guard_path}"


class TestIssue1198RawAndCommandFixtures:
    """AC8~AC16: Issue #1198 に追加した raw fixture / command fixture の回帰."""

    def test_pretooluse_raw_issue_comment_fragment_is_still_allowed(self, tmp_git_repo: Path):
        fixture = _load_fixture_json("issue1198-pretooluse-issue-comment.json")
        fixture["cwd"] = str(tmp_git_repo)
        result = run_guard_script(fixture, cwd=tmp_git_repo)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""

    def test_permissionrequest_raw_gh_api_get_is_allowed(self, tmp_git_repo: Path):
        fixture = _load_fixture_json("issue1198-permissionrequest-gh-api-get.json")
        fixture["cwd"] = str(tmp_git_repo)
        result = run_guard_script(fixture, cwd=tmp_git_repo)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""

    def test_stop_raw_payload_without_tool_input_is_allowed(self, tmp_git_repo: Path):
        payload = _load_fixture_json("issue1198-stop-no-command.json")
        payload["cwd"] = str(tmp_git_repo)
        result = run_guard_script(payload, cwd=tmp_git_repo)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""

    @pytest.mark.parametrize(
        ("entry", "expected_status", "expected_reason"),
        [
            (entry, "allow", entry["reason"])
            for entry in _load_fixture_json("issue1198-command-matrix.json").get("allow", [])
        ]
        + [
            (entry, "block", entry["reason"])
            for entry in _load_fixture_json("issue1198-command-matrix.json").get("block", [])
        ],
    )
    def test_issue1198_command_matrix_contracts(
        self, tmp_git_repo: Path, entry: dict, expected_status: str, expected_reason: str
    ):
        if "gh issue edit" in entry["command"] or "gh issue comment" in entry["command"]:
            expected_status = "block"
            expected_reason = REASON_GH_MUTATION
        result = eval_codex(
            command=entry["command"],
            cwd=str(tmp_git_repo),
            event=entry.get("event", "PreToolUse"),
        )
        assert result["status"] == expected_status
        assert result["reason_code"] == expected_reason
        if "rule_id" in entry:
            assert result["rule_id"] == entry["rule_id"]

    def test_issue1198_blocked_raw_field_redacts_secret(self, tmp_git_repo: Path):
        result = eval_codex(
            "gh api --raw-field body=SECRET_VALUE repos/squne121/loop-protocol/issues/comments/4814545233",
            str(tmp_git_repo),
        )
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_GH_API
        assert "SECRET_VALUE" not in str(result.get("argv_redacted", []))
        assert "SECRET_VALUE" not in str(result.get("inner_argv_redacted", []))
        result = eval_codex(
            "gh api -f body=SECRET_VALUE repos/squne121/loop-protocol/issues/comments/4814545233",
            str(tmp_git_repo),
        )
        assert result["status"] == "block"
        assert "SECRET_VALUE" not in str(result.get("argv_redacted", []))
        result = eval_codex(
            "gh api -F body=SECRET_VALUE repos/squne121/loop-protocol/issues/comments/4814545233",
            str(tmp_git_repo),
        )
        assert result["status"] == "block"
        assert "SECRET_VALUE" not in str(result.get("argv_redacted", []))

    def test_run_hook_block_stderr_contains_ac7_fields(self, tmp_git_repo: Path):
        payload = {
            "event": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git switch issue-1198-block"},
            "cwd": str(tmp_git_repo),
        }
        result = run_guard_script(payload, cwd=tmp_git_repo)
        assert result.returncode == 2
        output = result.stderr
        assert "hook_name: local_main_branch_guard" in output
        assert "event_kind: PreToolUse" in output
        assert "decision: block" in output
        assert "reason_code: local_root_branch_drift" in output
        assert "rule_id: git_branch_mutation_block" in output
        assert "command_kind: git_branch_mutation" in output
        assert "parser_stage: branch_mutation_block" in output
        assert "argv_redacted:" in output
        assert "recovery:" in output


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
        assert script_path.exists(), f"Startup preflight script not found: {script_path}"

    def test_check_codex_agent_config_validates_preflight(self):
        """check_codex_agent_config.py --assert-local-main-branch-guard must pass."""
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "check_codex_agent_config.py"),
                "--assert-local-main-branch-guard",
            ],
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
                    1 for h in entry.get("hooks", []) if "local_main_branch_guard" in h.get("command", "")
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
    @pytest.mark.parametrize(
        "cmd",
        [
            # gh issue close/comment/reopen are now allow via is_github_remote_ops_command (#1120)
            "gh issue edit 123 --title new",
            "gh issue delete 123",
            "gh issue lock 123",
            "gh issue unlock 123",
        ],
    )
    def test_gh_issue_mutations_use_gh_mutation_denied(self, tmp_git_repo: Path, cmd: str):
        """GIVEN gh issue mutation (outside github_remote_ops allowlist) WHEN evaluated THEN reason_code is gh_mutation_denied."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_GH_MUTATION, (
            f"Expected gh_mutation_denied for {cmd!r}, got {result['reason_code']!r}"
        )

    # AC3: gh pr checkout/edit/comment/merge/update-branch/review
    @pytest.mark.parametrize(
        "cmd",
        [
            # gh pr comment --body and gh pr edit <N> are now allow via is_github_remote_ops_command (#1120)
            "gh pr checkout 456",
            "gh pr merge 456",
            "gh pr update-branch 456",
            "gh pr review 456",
        ],
    )
    def test_gh_pr_mutations_use_gh_mutation_denied(self, tmp_git_repo: Path, cmd: str):
        """GIVEN gh pr mutation (outside github_remote_ops allowlist) WHEN evaluated THEN reason_code is gh_mutation_denied."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_GH_MUTATION, (
            f"Expected gh_mutation_denied for {cmd!r}, got {result['reason_code']!r}"
        )

    # AC4: readonly commands still allow
    @pytest.mark.parametrize(
        "cmd",
        [
            "gh issue view 123",
            "gh issue list",
            "gh pr view 456",
            "gh pr list",
            "gh pr status",
        ],
    )
    def test_gh_readonly_still_allowed(self, tmp_git_repo: Path, cmd: str):
        """GIVEN gh readonly command WHEN evaluated THEN status is allow."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "allow", f"Expected allow for readonly {cmd!r}, got {result['status']!r}"

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
        hint_line = [line for line in captured.err.splitlines() if "recovery:" in line]
        assert hint_line, "Expected a recovery: line in stderr"
        hint = hint_line[0].lower()
        assert any(kw in hint for kw in ("approved", "rtk", "workflow")), (
            f"recovery hint should mention approved/rtk/workflow, got: {hint!r}"
        )

    def test_emit_block_stderr_contains_ac7_fields(self, capsys):
        """_emit_block_stderr includes AC7 machine-readable fields."""
        from local_main_branch_guard import (
            _emit_block_stderr,
            REASON_GITHUB_REMOTE_OPS,
            COMMAND_KIND_GITHUB_ARTIFACT_EXPORT,
        )

        _emit_block_stderr(
            reason_code=REASON_GITHUB_REMOTE_OPS,
            current_branch_kind="default",
            current_is_default=True,
            target_branch_kind=None,
            hook_flavor="codex",
            event_kind="PermissionRequest",
            decision="block",
            command_kind=COMMAND_KIND_GITHUB_ARTIFACT_EXPORT,
            parser_stage="readonly_artifact_export",
            rule_id="gh_issue_view_to_tmp_allowed",
            argv_redacted=["gh", "issue", "view", "123", "--json", "body"],
        )
        captured = capsys.readouterr()
        output = captured.err
        assert "hook_name: local_main_branch_guard" in output
        assert "event_kind: PermissionRequest" in output
        assert "decision: block" in output
        assert "rule_id: gh_issue_view_to_tmp_allowed" in output
        assert "command_kind: readonly_artifact_export" in output
        assert "parser_stage: readonly_artifact_export" in output
        assert "argv_redacted:" in output


class TestExactAllowlist:
    """AC5, AC6, AC12: exact allowlist, publisher deny, deterministic_checker reason_code."""

    def test_exact_allowlist_skill_runtime_executor(self, tmp_linked_worktree: Path):
        repo_root = tmp_linked_worktree.parent.parent.parent
        result = eval_codex(
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 981 --repo squne121/loop-protocol",
            str(repo_root),
            env_override={"LOOP_ISSUE_NUMBER": "981"},
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_SKILL_RUNTIME_EXECUTOR

    def test_exact_allowlist_skill_runtime_executor_without_issue_env_or_worktree(self, tmp_git_repo: Path):
        result = eval_codex(
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 777 --repo squne121/loop-protocol",
            str(tmp_git_repo),
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_SKILL_RUNTIME_EXECUTOR

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

    def test_deterministic_checker_command_reason_code(self, tmp_linked_worktree: Path):
        repo_root = tmp_linked_worktree.parent.parent.parent
        result = eval_codex(
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 981 --repo squne121/loop-protocol",
            str(repo_root),
            env_override={"LOOP_ISSUE_NUMBER": "981"},
        )
        assert result["reason_code"] == REASON_SKILL_RUNTIME_EXECUTOR
        assert result["reason_code"] != REASON_READONLY

    def test_skill_runtime_executor_blocks_noncanonical_lexical_form(self, tmp_linked_worktree: Path):
        repo_root = tmp_linked_worktree.parent.parent.parent
        result = eval_codex(
            "python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 981 --repo squne121/loop-protocol",
            str(repo_root),
            env_override={"LOOP_ISSUE_NUMBER": "981"},
        )
        assert result["status"] == "block"

    def test_rtk_wrapped_skill_runtime_executor_is_allowed(self, tmp_git_repo: Path):
        result = eval_codex(
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
        result = eval_codex(command, str(tmp_git_repo))
        assert result["status"] == "block"

    def test_issue_refinement_direct_repair_hint_uses_exact_executor(self, tmp_git_repo: Path):
        payload = make_pretool_codex(
            (
                "uv run python3 .claude/skills/issue-refinement-loop/scripts/"
                "run_refinement_preflight.py --issue-number 981 --repo squne121/loop-protocol"
            ),
            str(tmp_git_repo),
        )
        result = run_guard_script(payload, tmp_git_repo)
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


def _anchor_command(
    issue_number: str = "981",
    repo: str = "squne121/loop-protocol",
    url: str = _ANCHOR_VALID_URL,
) -> str:
    return (
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
        "--command-id preflight.run.with_anchor "
        f"--issue-number {issue_number} --repo {repo} --anchor-comment-url {url}"
    )


_ANCHOR_NEGATIVE_MATRIX: list[tuple[str, str]] = [
    (
        "missing_anchor_flag",
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
        "--command-id preflight.run.with_anchor --issue-number 981 "
        "--repo squne121/loop-protocol",
    ),
    (
        "anchor_on_preflight_run",
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
        "--command-id preflight.run --issue-number 981 "
        "--repo squne121/loop-protocol --anchor-comment-url " + _ANCHOR_VALID_URL,
    ),
    (
        "duplicate_distinct_anchor_flags",
        _anchor_command()
        + " --anchor-comment-url "
        "https://github.com/squne121/loop-protocol/issues/981#issuecomment-2",
    ),
    (
        "duplicate_identical_anchor_flags",
        _anchor_command() + " --anchor-comment-url " + _ANCHOR_VALID_URL,
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
        _anchor_command() + " --extra x",
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
]


class TestAnchorProfileCodexParity:
    """Issue #1498 AC6: Claude/Codex parity for `preflight.run.with_anchor`
    (Positive/Negative Test Matrix #24)."""

    def test_exact_allowlist_anchor_profile_codex(self, tmp_git_repo: Path):
        """Matrix #2: correct single anchor URL is allowed (Codex flavor)."""
        result = eval_codex(
            _anchor_command(),
            str(tmp_git_repo),
            env_override={"LOOP_ISSUE_NUMBER": "981"},
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_SKILL_RUNTIME_EXECUTOR

    @pytest.mark.parametrize(("name", "command"), _ANCHOR_NEGATIVE_MATRIX)
    def test_anchor_profile_negative_matrix_codex(self, tmp_git_repo: Path, name: str, command: str):
        result = eval_codex(command, str(tmp_git_repo))
        assert result["status"] == "block", f"{name}: expected block, got {result}"

    @pytest.mark.parametrize(
        "command",
        [_anchor_command()] + [c for _, c in _ANCHOR_NEGATIVE_MATRIX],
    )
    def test_claude_codex_parity_anchor_profile(self, tmp_git_repo: Path, command: str):
        """Matrix #24: Claude allow/Codex deny parity mismatch must not exist."""
        claude_result = eval_codex(
            command,
            str(tmp_git_repo),
            env_override={"LOOP_ISSUE_NUMBER": "981"},
        )
        # eval_codex always sets hook_flavor via the `event`/flavor default in
        # evaluate(); call evaluate() directly for the two flavors so the
        # only variable between the two calls is hook_flavor itself.
        import os as _os

        old = _os.environ.get("CLAUDE_PROJECT_DIR", "")
        old_issue = _os.environ.get("LOOP_ISSUE_NUMBER")
        try:
            _os.environ["CLAUDE_PROJECT_DIR"] = str(tmp_git_repo)
            _os.environ["LOOP_ISSUE_NUMBER"] = "981"
            claude_flavor_result = evaluate(command=command, cwd=str(tmp_git_repo), hook_flavor="claude")
            codex_flavor_result = evaluate(command=command, cwd=str(tmp_git_repo), hook_flavor="codex")
        finally:
            if old:
                _os.environ["CLAUDE_PROJECT_DIR"] = old
            elif "CLAUDE_PROJECT_DIR" in _os.environ:
                del _os.environ["CLAUDE_PROJECT_DIR"]
            if old_issue is not None:
                _os.environ["LOOP_ISSUE_NUMBER"] = old_issue
            elif "LOOP_ISSUE_NUMBER" in _os.environ:
                del _os.environ["LOOP_ISSUE_NUMBER"]
        assert claude_flavor_result["status"] == codex_flavor_result["status"], (
            f"parity mismatch for {command!r}: "
            f"claude={claude_flavor_result['status']} codex={codex_flavor_result['status']}"
        )
        assert claude_result["status"] == claude_flavor_result["status"]


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

    @pytest.mark.parametrize(
        "cmd",
        [
            # gh issue subcommands NOT in the minimal allowlist
            "gh issue create --title x --body y",  # B3: removed from allowlist
            "gh issue edit 123 --title new",  # B3: removed from allowlist
            "gh issue develop 123 --base main",
            "gh issue develop 123 --checkout",
            "gh issue transfer 123 other/repo",
            "gh issue pin 123",
            "gh issue unpin 123",
            # gh pr subcommands NOT in the minimal allowlist
            "gh pr create --title x --body y",  # B3: removed from allowlist
            "gh pr revert 123",
            "gh pr lock 123",
            "gh pr unlock 123",
        ],
    )
    def test_unlisted_gh_mutations_are_blocked(self, tmp_git_repo: Path, cmd: str):
        """GIVEN gh mutation not in readonly allowlist or minimal gh ops allowlist WHEN evaluated THEN blocked (allowlist-closed)."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_GH_MUTATION

    @pytest.mark.parametrize(
        "cmd",
        [
            "gh issue close 1089",
            "gh issue reopen 123",
            "gh pr comment 456 --body hello",
            "gh pr edit 456 --title new",
        ],
    )
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

    @pytest.mark.parametrize(
        "cmd,expected",
        [
            # must-allow: 最小集合
            ("gh issue close 1089", "allow"),
            ("gh issue reopen 456", "allow"),
            ("gh pr comment 789 --body text", "allow"),
            ("gh pr edit 101 --title new", "allow"),
        ],
    )
    def test_minimal_allowlist_allowed(self, tmp_git_repo: Path, cmd: str, expected: str):
        """GIVEN minimal allowlist command WHEN evaluated THEN allowed with github_remote_ops_command reason."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == expected
        assert result["reason_code"] == REASON_GITHUB_REMOTE_OPS

    @pytest.mark.parametrize(
        "cmd",
        [
            # must-block: 最小集合外
            "gh issue create --title x --body y",  # B3: not in minimal set
            "gh issue edit 123 --title new",  # B3: not in minimal set (interactive possible)
            "gh pr create --title x --body y",  # B3: not in minimal set
            "gh issue comment 123",  # B2: --body なし → interactive
            "gh pr comment 456",  # B2: --body なし → interactive
            "gh issue close",  # B1: 番号なし
            "gh issue reopen",  # B1: 番号なし
            "gh pr edit",  # B1: 番号なし → branch 依存
            "gh issue comment 123 --delete-last",  # B2: destructive flag
            "gh issue comment 123 --editor",  # B2: interactive flag
            "gh issue comment 123 --web",  # B2: interactive flag
        ],
    )
    def test_minimal_allowlist_blocked(self, tmp_git_repo: Path, cmd: str):
        """GIVEN command outside minimal allowlist WHEN evaluated THEN blocked."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block"


# ─── AC8〜AC15: Issue #1124 GitHub remote ops 5 分類 ────────────────────────


class TestGithubIssueMutationCommand:
    """
    AC8: raw gh issue edit/comment are blocked after deprecation.
    AC9: gh issue create with --repo + --body-file tmp/ remains allowed.
    AC10: bare gh issue create / gh issue edit 123 (no --body-file) → block
    AC13: gh issue create/edit/comment/close/reopen is NOT readonly_command
    """

    @pytest.mark.parametrize("cmd", RAW_ISSUE_EDIT_COMMANDS)
    def test_issue_metadata_mutation_parity(self, tmp_git_repo: Path, cmd: str):
        """AC8: GIVEN raw gh issue edit WHEN evaluated THEN block in Codex too."""
        assert not is_github_issue_mutation_command(cmd), f"Expected False for: {cmd!r}"
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block", f"Expected block for: {cmd!r}"
        assert result["reason_code"] == REASON_GH_MUTATION

    # AC9: gh issue create --repo squne121/loop-protocol --body-file tmp/foo.md → allow
    @pytest.mark.parametrize(
        "cmd",
        [
            "gh issue create --repo squne121/loop-protocol --title foo --body-file tmp/foo.md",
            "gh issue create --repo squne121/loop-protocol --body-file tmp/body.md --title new-issue",
        ],
    )
    def test_issue_create_still_allowed(self, tmp_git_repo: Path, cmd: str):
        """AC9: GIVEN gh issue create with --repo + --body-file tmp/ WHEN evaluated THEN allow."""
        assert is_github_issue_mutation_command(cmd), f"Expected True for: {cmd!r}"
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "allow", f"Expected allow for: {cmd!r}"
        assert result["reason_code"] == REASON_GITHUB_REMOTE_OPS

    # AC10: bare gh issue create / gh issue edit 123 (no --body-file) → block
    @pytest.mark.parametrize(
        "cmd",
        [
            "gh issue create",  # bare create
            "gh issue edit 123",  # bare edit, no --body-file
            "gh issue create --repo squne121/loop-protocol",  # no --body-file
            "gh issue edit 123 --repo squne121/loop-protocol",  # no --body-file
            "gh issue create --body-file tmp/foo.md",  # no --repo
            "gh issue edit 123 --body-file tmp/foo.md",  # no --repo
            "gh issue create --repo squne121/loop-protocol --body-file tmp/foo.md --editor",  # interactive
            "gh issue edit 123 --repo squne121/loop-protocol --body-file tmp/foo.md --web",  # interactive
            "gh issue create --repo squne121/loop-protocol --body-file -",  # stdin
            "gh issue edit 123 --repo squne121/loop-protocol --body-file /tmp/foo.md",  # /tmp not tmp/
            "gh issue edit 123 --repo other-org/other-repo --body-file tmp/foo.md",  # wrong repo
        ],
    )
    def test_ac10_bare_gh_issue_create_or_edit_blocked(self, tmp_git_repo: Path, cmd: str):
        """AC10: GIVEN bare gh issue create/edit or without required flags WHEN evaluated THEN block."""
        assert not is_github_issue_mutation_command(cmd), f"Expected False for: {cmd!r}"
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block", f"Expected block for: {cmd!r}"

    # AC13: gh issue create/edit/comment/close/reopen are NOT readonly_command
    @pytest.mark.parametrize(
        "cmd",
        [
            "gh issue create --repo squne121/loop-protocol --title t --body-file tmp/foo.md",
            "gh issue edit 123 --repo squne121/loop-protocol --body-file tmp/foo.md",
            "gh issue close 123",
            "gh issue comment 123 --body hello",
            "gh issue reopen 123",
        ],
    )
    def test_ac13_gh_issue_mutations_not_readonly_command(self, tmp_git_repo: Path, cmd: str):
        """AC13: GIVEN gh issue mutation WHEN evaluated THEN reason_code is NOT readonly_command."""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["reason_code"] != REASON_READONLY, (
            f"gh issue mutation {cmd!r} must not have readonly_command reason_code, got: {result['reason_code']!r}"
        )


class TestReadonlyArtifactExportCommand:
    """
    AC11: gh issue view ... > tmp/issue_123.md → allow (readonly_artifact_export_command)
    AC12: gh issue view 123 > src/foo.md / > docs/foo.md / > .env / >> tmp/foo.md → block
    """

    # AC11: gh issue view ... > tmp/issue_123.md → allow
    @pytest.mark.parametrize(
        "cmd",
        [
            "gh issue view 123 --repo squne121/loop-protocol --json body --jq .body > tmp/issue_123.md",
            "gh issue view 456 > tmp/issue_456.md",
            "gh issue view 1 --json body > tmp/out.md",
        ],
    )
    def test_ac11_gh_issue_view_to_tmp_allowed(self, tmp_git_repo: Path, cmd: str):
        """AC11: GIVEN gh issue view ... > tmp/... WHEN evaluated THEN allow."""
        assert is_readonly_artifact_export_command(cmd), f"Expected True for: {cmd!r}"
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "allow", f"Expected allow for: {cmd!r}"
        assert result["reason_code"] == REASON_READONLY

    # AC12: blocked destinations
    @pytest.mark.parametrize(
        "cmd",
        [
            "gh issue view 123 > src/foo.md",  # src/ destination
            "gh issue view 123 > docs/foo.md",  # docs/ destination
            "gh issue view 123 > .env",  # .env destination
            "gh issue view 123 >> tmp/foo.md",  # append redirect
            "gh issue view 123 > /tmp/foo.md",  # /tmp absolute (not tmp/ relative)
            "gh issue view 123 > foo.md",  # no directory prefix
            "gh issue view 1124 > tmp/../docs/foo.md",  # path traversal
            "gh issue view 1124 --web > tmp/foo.md",  # --web flag (browser open)
        ],
    )
    def test_ac12_gh_issue_view_to_blocked_dest_blocked(self, tmp_git_repo: Path, cmd: str):
        """AC12: GIVEN gh issue view with blocked destination WHEN evaluated THEN block."""
        assert not is_readonly_artifact_export_command(cmd), f"Expected False for: {cmd!r}"
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block", f"Expected block for: {cmd!r}"


class TestGhDestructiveCommandsBlocked:
    """AC14: gh pr merge / gh pr checkout / gh pr update-branch remain blocked."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "gh pr merge 123",
            "gh pr checkout 123",
            "gh pr update-branch 456",
        ],
    )
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
            assert term in content, f"hook-boundaries.md must contain 5-class term: {term!r}"

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
            assert term in content, f"agent-skill-boundaries.md must contain 5-class term: {term!r}"

    def test_ac15_codex_default_rules_has_trusted_repo_mention(self):
        """AC15: .codex/rules/default.rules mentions managed skill context for gh issue create/edit."""
        rules_path = REPO_ROOT / ".codex" / "rules" / "default.rules"
        assert rules_path.exists(), f"default.rules not found: {rules_path}"
        content = rules_path.read_text()
        # The rules file should mention that managed skill context allows gh issue create/edit
        assert "github_issue_mutation" in content or "managed skill" in content or "body-file" in content, (
            ".codex/rules/default.rules must reference managed skill / body-file context for gh issue mutations"
        )


# ─── B1-B4 Review Blocker Fixes (Codex flavor) ────────────────────────────────


class TestB1B4ReviewBlockerFixes:
    """
    B1: gh issue create requires --title with value.
    B2: --body-file canonical tmp/ path validation (no path traversal, no absolute).
    B3: gh issue view --web/-w blocked.
    B4: stricter checks (--body without value, -e, --edit-last, --base, /tmp body-file).
    """

    # B1: --title required for gh issue create
    def test_b1_gh_issue_create_without_title_blocked(self, tmp_git_repo: Path):
        """B1: gh issue create without --title is blocked."""
        result = eval_codex(
            "gh issue create --repo squne121/loop-protocol --body-file tmp/foo.md",
            str(tmp_git_repo),
        )
        assert result["status"] == "block", "gh issue create without --title must be blocked"

    def test_b1_gh_issue_create_with_empty_title_blocked(self, tmp_git_repo: Path):
        """B1: gh issue create with bare --title (no value) is blocked."""
        assert not is_github_issue_mutation_command(
            "gh issue create --repo squne121/loop-protocol --body-file tmp/foo.md --title"
        ), "gh issue create with bare --title must be False"

    def test_b1_gh_issue_create_with_title_allowed(self, tmp_git_repo: Path):
        """B1: gh issue create with --title value is allowed."""
        result = eval_codex(
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

    def test_b2_body_file_canonical_tmp_allowed(self, tmp_git_repo: Path):
        """B2: --body-file tmp/body.txt (canonical relative) is allowed."""
        assert is_github_issue_mutation_command(
            "gh issue create --repo squne121/loop-protocol --title foo --body-file tmp/body.txt"
        ), "canonical tmp/ path in --body-file must be allowed"

    # B3: --web/-w blocked for gh issue/pr view
    def test_b3_gh_issue_view_web_blocked(self, tmp_git_repo: Path):
        """B3: gh issue view --web opens browser, must be blocked."""
        result = eval_codex("gh issue view 123 --web", str(tmp_git_repo))
        assert result["status"] == "block", "gh issue view --web must be blocked"

    def test_b3_gh_issue_view_w_flag_blocked(self, tmp_git_repo: Path):
        """B3: gh issue view -w (short form) opens browser, must be blocked."""
        result = eval_codex("gh issue view 123 -w", str(tmp_git_repo))
        assert result["status"] == "block", "gh issue view -w must be blocked"

    def test_b3_gh_issue_view_without_web_allowed(self, tmp_git_repo: Path):
        """B3: gh issue view without --web/-w must still be allowed (readonly)."""
        result = eval_codex("gh issue view 123", str(tmp_git_repo))
        assert result["status"] == "allow", "gh issue view (no --web) must be allowed"

    # B4: stricter checks
    def test_b4_gh_issue_comment_body_without_value_blocked(self, tmp_git_repo: Path):
        """B4: gh issue comment with bare --body (no value) is blocked."""
        result = eval_codex("gh issue comment 123 --body", str(tmp_git_repo))
        assert result["status"] == "block", "gh issue comment with bare --body must be blocked"

    def test_b4_gh_issue_comment_edit_last_blocked(self, tmp_git_repo: Path):
        """B4: gh issue comment --edit-last is blocked (destructive)."""
        result = eval_codex("gh issue comment 123 --edit-last", str(tmp_git_repo))
        assert result["status"] == "block", "gh issue comment --edit-last must be blocked"

    def test_b4_gh_issue_comment_e_flag_blocked(self, tmp_git_repo: Path):
        """B4: gh issue comment -e is blocked (interactive editor)."""
        result = eval_codex("gh issue comment 123 -e", str(tmp_git_repo))
        assert result["status"] == "block", "gh issue comment -e must be blocked"

    def test_b4_gh_pr_edit_base_main_blocked(self, tmp_git_repo: Path):
        """B4: gh pr edit --base main is blocked (changes base branch)."""
        result = eval_codex("gh pr edit 123 --base main", str(tmp_git_repo))
        assert result["status"] == "block", "gh pr edit --base main must be blocked"

    def test_b4_gh_pr_edit_body_file_absolute_tmp_blocked(self, tmp_git_repo: Path):
        """B4: gh pr edit --body-file /tmp/... (absolute path) is blocked."""
        result = eval_codex("gh pr edit 123 --body-file /tmp/body.txt", str(tmp_git_repo))
        assert result["status"] == "block", "gh pr edit --body-file /tmp/... must be blocked"

    def test_b4_gh_pr_edit_body_file_canonical_tmp_allowed(self, tmp_git_repo: Path):
        """B4: gh pr edit --body-file tmp/body.txt (canonical) is allowed."""
        result = eval_codex("gh pr edit 123 --body-file tmp/body.txt", str(tmp_git_repo))
        assert result["status"] == "allow", "gh pr edit --body-file tmp/... must be allowed"
        assert result["reason_code"] == REASON_GITHUB_REMOTE_OPS

    @pytest.mark.parametrize(("cmd", "expected_reason"), REVIEWER_GH_MUTATION_REGRESSIONS)
    def test_b4_reviewer_gh_mutation_regressions(
        self, tmp_git_repo: Path, cmd: str, expected_reason: str
    ):
        """Reviewer 指摘の raw mutation-like gh commands は Codex parity でも block される。"""
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == expected_reason

    @pytest.mark.parametrize("cmd", [
        "gh issue edit 1198 --repo squne121/loop-protocol --body-file tmp/x.md --add-label foo",
        "gh issue edit 1198 --repo squne121/loop-protocol --body-file tmp/x.md --milestone M1",
        "gh issue edit 1198 --repo squne121/loop-protocol --body-file tmp/x.md --title changed",
    ])
    def test_b4_gh_issue_edit_additional_mutation_flags_blocked(self, tmp_git_repo: Path, cmd: str):
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_GH_MUTATION

    @pytest.mark.parametrize("cmd", [
        "gh api --hostname evil.example.com repos/squne121/loop-protocol/issues/comments/4814545233",
        "gh api repos/{owner}/{repo}/issues/comments/4814545233",
        "gh api --paginate repos/squne121/loop-protocol/issues/comments/4814545233",
    ])
    def test_issue1198_gh_api_negative_boundaries_blocked(self, tmp_git_repo: Path, cmd: str):
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "block"
        assert result["reason_code"] == REASON_GH_API

    def test_issue1198_unknown_non_branch_command_uses_non_review_reason(self, tmp_git_repo: Path):
        result = eval_codex(
            "echo https://github.com/squne121/loop-protocol/issues/1170#issuecomment-4814071263",
            str(tmp_git_repo),
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_UNKNOWN_ALLOWED


class TestIssue1291IssueMetadataMutationCodex:
    """Issue #1291 parity coverage for Codex hook inputs."""

    @pytest.mark.parametrize("cmd", RAW_ISSUE_COMMENT_COMMANDS)
    def test_issue_comment_deprecated(self, tmp_git_repo: Path, cmd: str):
        result = eval_codex(cmd, str(tmp_git_repo))
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
        result = eval_codex(cmd, str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_GITHUB_REMOTE_OPS

    @pytest.mark.parametrize("cmd", RAW_ISSUE_EDIT_COMMANDS + RAW_ISSUE_COMMENT_COMMANDS)
    def test_must_block_raw_issue_metadata_commands_via_hook_script(self, tmp_git_repo: Path, cmd: str):
        payload = make_pretool_codex(cmd, str(tmp_git_repo))
        result = run_guard_script(payload, cwd=tmp_git_repo)
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
        payload = make_pretool_codex(
            (
                "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py "
                f"--command-id {command_id} --issue-number 1291 "
                f"--input-file {input_file} --repo squne121/loop-protocol --dry-run"
            ),
            str(tmp_git_repo),
        )
        eval_result = eval_codex(payload["tool_input"]["command"], str(tmp_git_repo))
        assert eval_result["status"] == "allow"
        # NOTE: PR #1299 (Issue #1289) split the shared deterministic_checker
        # reason_code so controlled_skill_mutation_exec.py invocations report
        # their own dedicated reason_code (see REASON_CONTROLLED_SKILL_MUTATION_EXECUTOR).
        assert eval_result["reason_code"] == REASON_CONTROLLED_SKILL_MUTATION_EXECUTOR
        assert eval_result["parser_stage"] == "controlled_skill_mutation"
        assert eval_result["rule_id"] == "controlled_skill_mutation"
        result = run_guard_script(payload, cwd=tmp_git_repo)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""


# ─── Issue #1137: cleanup arbitration + Claude/Codex reason_code parity ────────
# The cleanup decision core is worktree_scope_guard.build_decision, wired into
# BOTH .claude/settings.json (Claude) and .codex/hooks.json (Codex). The two
# runtimes therefore share one decision core and one reason-code vocabulary
# (SHARED_CLEANUP_REASON_CODES). local_main_branch_guard defers the exact cleanup
# commands to that core instead of double-deciding them.

import importlib.util as _ilu  # noqa: E402
import json as _json  # noqa: E402
import time as _time  # noqa: E402

_AGENT_OPS_DIR = REPO_ROOT / "scripts" / "agent-ops"
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))

import cleanup_contract_v3 as _cc3  # noqa: E402
from local_main_branch_guard import (  # noqa: E402
    REASON_CLEANUP_DEFERRED,
    is_cleanup_class_command,
)

_GUARD_PY = REPO_ROOT / ".claude" / "hooks" / "worktree_scope_guard.py"


def _load_cleanup_core():
    spec = _ilu.spec_from_file_location("worktree_scope_guard", str(_GUARD_PY))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git_q(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _repo_with_worktree_and_v3(tmp: Path, *, bad_hash: bool = False) -> dict:
    main = tmp / "repo"
    main.mkdir()
    _git_q("init", "-q", "-b", "main", cwd=main)
    _git_q("config", "user.email", "t@t.com", cwd=main)
    _git_q("config", "user.name", "T", cwd=main)
    (main / "README.md").write_text("seed\n")
    _git_q("add", "README.md", cwd=main)
    _git_q("commit", "-q", "-m", "seed", cwd=main)
    wt = main / ".claude" / "worktrees" / "issue-1050-x"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git_q("worktree", "add", "-q", "-b", "issue-1050-x", str(wt), "main", cwd=main)

    wt_real = os.path.realpath(str(wt))
    now = int(_time.time())
    from datetime import datetime, timezone

    def _iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    nonce = "a" * 32
    chash = _cc3.canonical_command_hash(
        _cc3.expected_argv(_cc3.OP_WORKTREE_REMOVE, wt_real, "issue-1050-x"),
        _cc3.OP_WORKTREE_REMOVE,
        os.path.realpath(str(main)),
        nonce,
    )
    if bad_hash:
        chash = "0" * 64
    contract = {
        "schema": _cc3.SCHEMA_V3,
        "pr_number": 1,
        "linked_issue_number": 1050,
        "worktree_path": wt_real,
        "branch_name": "issue-1050-x",
        "require_clean": True,
        "operation": _cc3.OP_WORKTREE_REMOVE,
        "command_hash": chash,
        "nonce": nonce,
        "issued_at": _iso(now),
        "expires_at": _iso(now + 300),
    }
    target = main / "artifacts" / "agent-ops" / "cleanup_contract.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_json.dumps(contract))
    os.chmod(target, 0o600)
    return {"root": main, "worktree": wt, "worktree_real": wt_real}


def _core_decide(mod, command: str, repo: dict) -> dict:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo["worktree"])}
    old = os.environ.copy()
    os.environ["CLAUDE_PROJECT_DIR"] = str(repo["root"])
    os.environ["LOOP_ISSUE_NUMBER"] = "1050"
    os.environ.pop("CLAUDE_WORKTREE_CLEANUP_CONTRACT", None)
    try:
        return mod.build_decision(payload)
    finally:
        os.environ.clear()
        os.environ.update(old)


class TestCleanupArbitrationParity:
    """Issue #1137: local guard defers cleanup; cleanup core uses shared reasons."""

    def test_local_guard_defers_worktree_remove(self, tmp_git_repo: Path):
        assert is_cleanup_class_command("git worktree remove /x/y")
        result = eval_codex("git worktree remove /x/y", str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_CLEANUP_DEFERRED

    def test_local_guard_defers_branch_delete(self, tmp_git_repo: Path):
        assert is_cleanup_class_command("git branch -d issue-1-x")
        result = eval_codex("git branch -d issue-1-x", str(tmp_git_repo))
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_CLEANUP_DEFERRED

    def test_local_guard_does_not_defer_force(self, tmp_git_repo: Path):
        assert not is_cleanup_class_command("git branch -D issue-1-x")
        assert not is_cleanup_class_command("git worktree remove --force /x/y")
        assert not is_cleanup_class_command("git branch -d a b")

    def test_cleanup_core_command_hash_mismatch_shared_reason(self, tmp_path: Path):
        """The shared cleanup core denies a tampered hash with cleanup_command_hash_mismatch."""
        mod = _load_cleanup_core()
        repo = _repo_with_worktree_and_v3(tmp_path, bad_hash=True)
        d = _core_decide(mod, f"git worktree remove {repo['worktree_real']}", repo)
        assert d["decision"] == "deny"
        assert d["reason"] == "cleanup_command_hash_mismatch"
        assert d["reason"] in _cc3.SHARED_CLEANUP_REASON_CODES

    def test_cleanup_core_valid_contract_allow(self, tmp_path: Path):
        mod = _load_cleanup_core()
        repo = _repo_with_worktree_and_v3(tmp_path)
        d = _core_decide(mod, f"git worktree remove {repo['worktree_real']}", repo)
        assert d["decision"] == "allow", d

    def test_shared_reason_codes_single_vocabulary(self):
        for code in (
            "cleanup_command_hash_mismatch",
            "cleanup_contract_expired",
            "cleanup_operation_mismatch",
            "root_drift_active_worktree_mismatch",
        ):
            assert code in _cc3.SHARED_CLEANUP_REASON_CODES
        mod = _load_cleanup_core()
        assert mod._cc3.SHARED_CLEANUP_REASON_CODES == _cc3.SHARED_CLEANUP_REASON_CODES


class TestIssue1215RawFixturesAndWorktreeContext:
    """Issue #1215: linked issue worktree context and git-add raw fixture parity."""

    def test_issue1215_worktree_context(self, tmp_linked_worktree: Path):
        fixture = _load_fixture_json("issue1215-pretooluse-dedicated-worktree-git-add.json")
        result = eval_codex(
            command=fixture["tool_input"]["command"],
            cwd=str(tmp_linked_worktree),
            event=fixture["event"],
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_LINKED_ISSUE_WORKTREE_CONTEXT
        assert result["hook_flavor"] == "codex"

    def test_linked_issue_worktree_context_permissionrequest(self, tmp_linked_worktree: Path):
        fixture = _load_fixture_json("issue1215-permissionrequest-dedicated-worktree-git-add.json")
        result = eval_codex(
            command=fixture["tool_input"]["command"],
            cwd=str(tmp_linked_worktree),
            event=fixture["event"],
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_LINKED_ISSUE_WORKTREE_CONTEXT
        assert result["hook_flavor"] == "codex"

    def test_issue1215_no_git_add_exception(self, tmp_linked_worktree: Path):
        result = eval_codex("git add .", str(tmp_linked_worktree))
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_LINKED_ISSUE_WORKTREE_CONTEXT

# ─── Issue #1197: probe scripts deterministic_checker allow ─────────────────


class TestProbeScriptsDeterministicChecker:
    """Issue #1197: probe scripts must be allowed as deterministic_checker from root."""

    def test_git_ref_probe_exact_cmd_allowed(self, tmp_git_repo: Path):
        """GIVEN exact uv run python3 git_ref_probe.py --branch main --json
        WHEN evaluated THEN allowed as deterministic_checker_command."""
        result = eval_codex(
            "uv run python3 scripts/agent-ops/git_ref_probe.py --branch main --json",
            str(tmp_git_repo),
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_DETERMINISTIC_CHECKER

    def test_git_worktree_probe_exact_cmd_allowed(self, tmp_git_repo: Path):
        """GIVEN exact uv run python3 git_worktree_probe.py --json
        WHEN evaluated THEN allowed as deterministic_checker_command."""
        result = eval_codex(
            "uv run python3 scripts/agent-ops/git_worktree_probe.py --json",
            str(tmp_git_repo),
        )
        assert result["status"] == "allow"
        assert result["reason_code"] == REASON_DETERMINISTIC_CHECKER

    def test_git_ref_probe_deterministic_checker_allowlist(self):
        """git_ref_probe.py must be in DETERMINISTIC_CHECKER_ALLOWLIST."""
        from local_main_branch_guard import DETERMINISTIC_CHECKER_ALLOWLIST

        assert "scripts/agent-ops/git_ref_probe.py" in DETERMINISTIC_CHECKER_ALLOWLIST

    def test_git_worktree_probe_deterministic_checker_allowlist(self):
        """git_worktree_probe.py must be in DETERMINISTIC_CHECKER_ALLOWLIST."""
        from local_main_branch_guard import DETERMINISTIC_CHECKER_ALLOWLIST

        assert "scripts/agent-ops/git_worktree_probe.py" in DETERMINISTIC_CHECKER_ALLOWLIST

    def test_is_deterministic_checker_denies_unknown_flag(self):
        """B1: unknown flags must be denied by is_deterministic_checker_command."""
        from local_main_branch_guard import is_deterministic_checker_command

        cmd = "uv run python3 scripts/agent-ops/git_ref_probe.py --branch main --unknown-flag"
        assert not is_deterministic_checker_command(cmd)

    def test_is_deterministic_checker_denies_missing_required_branch(self):
        """B1: missing required --branch must be denied by is_deterministic_checker_command."""
        from local_main_branch_guard import is_deterministic_checker_command

        cmd = "uv run python3 scripts/agent-ops/git_ref_probe.py --json"
        assert not is_deterministic_checker_command(cmd)

    def test_is_deterministic_checker_denies_flag_equals_value(self):
        """B1: --flag=value form must be denied by is_deterministic_checker_command."""
        from local_main_branch_guard import is_deterministic_checker_command

        cmd = "uv run python3 scripts/agent-ops/git_ref_probe.py --branch=main"
        assert not is_deterministic_checker_command(cmd)

    def test_is_deterministic_checker_allows_valid_probe_command(self):
        """B1: valid probe command must be allowed after argv validation."""
        from local_main_branch_guard import is_deterministic_checker_command

        cmd = "uv run python3 scripts/agent-ops/git_ref_probe.py --branch main --json"
        assert is_deterministic_checker_command(cmd)

    def test_is_deterministic_checker_allows_worktree_probe(self):
        """B1: git_worktree_probe.py without args must be allowed (no required flags)."""
        from local_main_branch_guard import is_deterministic_checker_command

        cmd = "uv run python3 scripts/agent-ops/git_worktree_probe.py"
        assert is_deterministic_checker_command(cmd)


class TestIssue1543CodexClaudeParity:
    """Issue #1543 AC6: Claude hook wrapper invocation and Codex event fixture
    invocation must produce the same allow/block verdict for the same input
    command (argv-aware issue_refinement_direct classifier)."""

    @pytest.mark.parametrize(
        ("cmd", "expected_status"),
        [
            (
                "uv run python3 .claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py "
                "--allowed-paths '[\"scripts/agent-guards/local_main_branch_guard.py\", "
                "\".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py\"]'",
                "allow",
            ),
            (
                "uv run python3 .claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py "
                "--issue-number 985 --repo squne121/loop-protocol",
                "block",
            ),
        ],
    )
    def test_codex_and_claude_agree_on_issue_refinement_classifier_verdict(
        self, tmp_git_repo: Path, cmd: str, expected_status: str
    ):
        codex_result = eval_codex(cmd, str(tmp_git_repo))
        assert codex_result["status"] == expected_status

        claude_hook_script = REPO_ROOT / ".claude" / "hooks" / "local_main_branch_guard.sh"
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "cwd": str(tmp_git_repo),
        }
        claude_proc = subprocess.run(
            [str(claude_hook_script)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=str(tmp_git_repo),
        )
        claude_status = "block" if claude_proc.returncode == 2 else "allow"
        assert claude_status == expected_status
        assert claude_status == codex_result["status"]
