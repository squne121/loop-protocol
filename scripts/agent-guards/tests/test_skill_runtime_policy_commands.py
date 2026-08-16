"""
test_skill_runtime_policy_commands.py

Issue #2039 AC8: registry/policy/parser fixed-shape and negative-matrix
coverage for the `repair_action.apply` command class.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Generator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_GUARDS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_GUARDS_DIR))
sys.path.insert(0, str(REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"))

from skill_runtime_command_policy import (  # noqa: E402
    SKILL_RUNTIME_COMMAND_POLICY_V2,
    SKILL_RUNTIME_EXEC_REL,
    TRUSTED_REPO_SLUG,
    ROOT_NO_WORKTREE_ALLOWED_COMMAND_IDS,
    is_exact_skill_runtime_repair_action_apply_executor_command,
    parse_exact_skill_runtime_command,
    parse_exact_skill_runtime_repair_action_apply_command,
    validate_registry_entry,
)

import command_registry  # noqa: E402


_VALID_PATH = ".claude/artifacts/issue-refinement-loop/2039/preflight_result.json"


def _cmd(
    issue_number: str = "2039",
    repo: str = TRUSTED_REPO_SLUG,
    path: str = _VALID_PATH,
) -> str:
    return (
        f"uv run python3 {SKILL_RUNTIME_EXEC_REL} "
        "--command-id repair_action.apply "
        f"--issue-number {issue_number} --repo {repo} --apply-repair-action {path}"
    )


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Generator[Path, None, None]:
    import os
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/squne121/loop-protocol.git"],
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True, capture_output=True, env=env)
    yield repo


# ---------------------------------------------------------------------------
# AC8: command_registry.py `repair_action.apply` entry / policy parity
# ---------------------------------------------------------------------------


class TestRepairActionApplyRegistryEntry:
    def test_registry_entry_exists_and_matches_policy(self) -> None:
        entry = command_registry.REGISTRY["repair_action.apply"]
        assert entry["id"] == "repair_action.apply"
        # AC8 fixed-contract fields from the Issue's machine-readable
        # command_contract block.
        assert entry["cwd_policy"] == "repo_root"
        assert entry["required_cwd"] == "canonical_main_root"
        assert entry["network_effect"] == "github_mutation"
        assert entry["mutation"] is True
        assert entry["stdout_contract"] == "repair_apply_result/v1"
        assert entry["shell"] is False
        # Validates against skill_runtime_command_policy.py's own
        # eligible_command_ids declaration (execution_class / cwd / branch /
        # write-roots / argv-template / placeholder parity) -- this is the
        # exact check `skill_runtime_exec.py` runs before every real
        # dispatch.
        validate_registry_entry("repair_action.apply", entry, "2039")

    def test_argv_child_entrypoint_is_run_refinement_preflight(self) -> None:
        entry = command_registry.REGISTRY["repair_action.apply"]
        assert (
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py"
            in entry["argv"]
        )
        assert "--apply-repair-action" in entry["argv"]

    def test_entry_id_registered_in_eligible_command_ids(self) -> None:
        assert "repair_action.apply" in SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]

    def test_not_root_no_worktree_eligible(self) -> None:
        """repair_action.apply performs a real GitHub mutation and is bound
        to the issue's own active worktree, same boundary as
        authority_transport.consume -- it must not be reachable without an
        active-issue worktree resolving."""
        assert "repair_action.apply" not in ROOT_NO_WORKTREE_ALLOWED_COMMAND_IDS

    def test_render_command_produces_declared_argv(self) -> None:
        rendered = command_registry.render_command(
            "repair_action.apply",
            {"issue_number": 2039, "repo": TRUSTED_REPO_SLUG, "preflight_result_path": _VALID_PATH},
        )
        assert rendered[-1] == _VALID_PATH
        assert "--apply-repair-action" in rendered
        assert "gh" not in rendered


# ---------------------------------------------------------------------------
# AC8: parse_exact_skill_runtime_repair_action_apply_command
# ---------------------------------------------------------------------------


class TestParseExactSkillRuntimeRepairActionApplyCommand:
    def test_valid_command_parses(self, tmp_git_repo: Path):
        parsed = parse_exact_skill_runtime_repair_action_apply_command(_cmd(), str(tmp_git_repo))
        assert parsed is not None
        assert parsed.command_id == "repair_action.apply"
        assert parsed.issue_number == "2039"
        assert parsed.repo == TRUSTED_REPO_SLUG
        assert parsed.preflight_result_path == _VALID_PATH

    def test_preflight_run_unaffected(self, tmp_git_repo: Path):
        command = (
            f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id preflight.run "
            f"--issue-number 2039 --repo {TRUSTED_REPO_SLUG}"
        )
        parsed = parse_exact_skill_runtime_command(command, str(tmp_git_repo))
        assert parsed is not None
        assert parsed.command_id == "preflight.run"
        assert parse_exact_skill_runtime_repair_action_apply_command(command, str(tmp_git_repo)) is None

    def test_extra_argv_token_rejected(self, tmp_git_repo: Path):
        command = _cmd() + " --extra x"
        assert parse_exact_skill_runtime_repair_action_apply_command(command, str(tmp_git_repo)) is None

    def test_missing_apply_repair_action_flag_rejected(self, tmp_git_repo: Path):
        command = (
            f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id repair_action.apply "
            f"--issue-number 2039 --repo {TRUSTED_REPO_SLUG}"
        )
        assert parse_exact_skill_runtime_repair_action_apply_command(command, str(tmp_git_repo)) is None

    def test_alternate_script_rejected(self, tmp_git_repo: Path):
        command = _cmd().replace(SKILL_RUNTIME_EXEC_REL, "scripts/agent-guards/some_other_script.py")
        assert parse_exact_skill_runtime_repair_action_apply_command(command, str(tmp_git_repo)) is None

    def test_eq_form_rejected(self, tmp_git_repo: Path):
        command = (
            f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id repair_action.apply "
            f"--issue-number 2039 --repo {TRUSTED_REPO_SLUG} --apply-repair-action={_VALID_PATH}"
        )
        assert parse_exact_skill_runtime_repair_action_apply_command(command, str(tmp_git_repo)) is None

    def test_flag_order_changed_rejected(self, tmp_git_repo: Path):
        command = (
            f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --apply-repair-action {_VALID_PATH} "
            f"--command-id repair_action.apply --issue-number 2039 --repo {TRUSTED_REPO_SLUG}"
        )
        assert parse_exact_skill_runtime_repair_action_apply_command(command, str(tmp_git_repo)) is None

    def test_duplicate_flag_rejected(self, tmp_git_repo: Path):
        command = _cmd() + f" --apply-repair-action {_VALID_PATH}"
        assert parse_exact_skill_runtime_repair_action_apply_command(command, str(tmp_git_repo)) is None

    def test_repo_context_mismatch_rejected(self, tmp_git_repo: Path):
        command = _cmd(repo="other/repo")
        assert parse_exact_skill_runtime_repair_action_apply_command(command, str(tmp_git_repo)) is None

    def test_absolute_path_rejected(self, tmp_git_repo: Path):
        command = _cmd(path="/etc/hosts")
        assert parse_exact_skill_runtime_repair_action_apply_command(command, str(tmp_git_repo)) is None

    def test_path_traversal_rejected(self, tmp_git_repo: Path):
        command = _cmd(path="../../../outside.json")
        assert parse_exact_skill_runtime_repair_action_apply_command(command, str(tmp_git_repo)) is None

    def test_leading_dash_path_rejected(self, tmp_git_repo: Path):
        command = _cmd(path="--evil")
        assert parse_exact_skill_runtime_repair_action_apply_command(command, str(tmp_git_repo)) is None

    def test_shell_metachar_semicolon_rejected(self, tmp_git_repo: Path):
        bad_command = _cmd() + ";" + "false"
        assert parse_exact_skill_runtime_repair_action_apply_command(bad_command, str(tmp_git_repo)) is None

    def test_shell_metachar_ampersand_rejected(self, tmp_git_repo: Path):
        bad_command = _cmd() + "&" + "true"
        assert parse_exact_skill_runtime_repair_action_apply_command(bad_command, str(tmp_git_repo)) is None

    def test_shell_metachar_pipe_rejected(self, tmp_git_repo: Path):
        bad_command = _cmd() + "|" + "true"
        assert parse_exact_skill_runtime_repair_action_apply_command(bad_command, str(tmp_git_repo)) is None

    def test_shell_metachar_newline_rejected(self, tmp_git_repo: Path):
        bad_command = _cmd() + "\n" + "true"
        assert parse_exact_skill_runtime_repair_action_apply_command(bad_command, str(tmp_git_repo)) is None

    def test_shell_metachar_nul_rejected(self, tmp_git_repo: Path):
        bad_command = _cmd() + "\x00"
        assert parse_exact_skill_runtime_repair_action_apply_command(bad_command, str(tmp_git_repo)) is None

    def test_wrong_token_count_rejected(self, tmp_git_repo: Path):
        """A well-formed 10-token preflight.run-shaped command (no
        --apply-repair-action suffix at all) must never parse as
        repair_action.apply."""
        command = (
            f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id repair_action.apply "
            f"--issue-number 2039 --repo {TRUSTED_REPO_SLUG}"
        )
        assert parse_exact_skill_runtime_repair_action_apply_command(command, str(tmp_git_repo)) is None


# ---------------------------------------------------------------------------
# AC8: is_exact_skill_runtime_repair_action_apply_executor_command safety
# boundary (trusted repo / default branch / canonical root / active-issue
# worktree).
# ---------------------------------------------------------------------------


class TestIsExactSkillRuntimeRepairActionApplyExecutorCommand:
    def test_denies_when_cwd_is_not_project_root(self, tmp_git_repo: Path):
        subdir = tmp_git_repo / "subdir"
        subdir.mkdir()
        assert not is_exact_skill_runtime_repair_action_apply_executor_command(
            _cmd(), str(subdir), str(tmp_git_repo)
        )

    def test_denies_on_non_default_branch(self, tmp_git_repo: Path):
        import subprocess

        subprocess.run(
            ["git", "-C", str(tmp_git_repo), "checkout", "-q", "-b", "feature"], check=True, capture_output=True
        )
        assert not is_exact_skill_runtime_repair_action_apply_executor_command(
            _cmd(), str(tmp_git_repo), str(tmp_git_repo)
        )

    def test_denies_when_repo_slug_mismatches_remote(self, tmp_git_repo: Path):
        assert not is_exact_skill_runtime_repair_action_apply_executor_command(
            _cmd(repo="other/repo"), str(tmp_git_repo), str(tmp_git_repo)
        )

    def test_denies_without_active_issue_worktree(self, tmp_git_repo: Path):
        """Unlike root-no-worktree-eligible command classes,
        repair_action.apply requires resolve_active_issue() to find a real
        matching worktree entry; the fixture's default `worktree_catalog`
        (LOOP_ISSUE_NUMBER unset) never resolves one here."""
        assert not is_exact_skill_runtime_repair_action_apply_executor_command(
            _cmd(), str(tmp_git_repo), str(tmp_git_repo)
        )

    def test_malformed_command_denied(self, tmp_git_repo: Path):
        assert not is_exact_skill_runtime_repair_action_apply_executor_command(
            _cmd() + " --extra x", str(tmp_git_repo), str(tmp_git_repo)
        )
