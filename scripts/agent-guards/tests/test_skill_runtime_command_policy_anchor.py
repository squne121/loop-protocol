"""
test_skill_runtime_command_policy_anchor.py

Tests for `parse_exact_skill_runtime_anchor_command()` /
`is_exact_skill_runtime_anchor_executor_command()` (Issue #1498).

Covers AC3 and Positive/Negative Test Matrix items #1-#22.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Generator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_GUARDS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_GUARDS_DIR))

from skill_runtime_command_policy import (  # noqa: E402
    SKILL_RUNTIME_EXEC_REL,
    TRUSTED_REPO_SLUG,
    is_exact_skill_runtime_anchor_executor_command,
    is_exact_skill_runtime_executor_command,
    parse_exact_skill_runtime_anchor_command,
    parse_exact_skill_runtime_command,
)


_VALID_URL = "https://github.com/squne121/loop-protocol/issues/981#issuecomment-1"


def _cmd(
    issue_number: str = "981",
    repo: str = TRUSTED_REPO_SLUG,
    url: str = _VALID_URL,
) -> str:
    return (
        f"uv run python3 {SKILL_RUNTIME_EXEC_REL} "
        "--command-id preflight.run.with_anchor "
        f"--issue-number {issue_number} --repo {repo} --anchor-comment-url {url}"
    )


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Generator[Path, None, None]:
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
# AC3: parse_exact_skill_runtime_anchor_command
# ---------------------------------------------------------------------------


class TestParseExactSkillRuntimeAnchorCommand:
    def test_matrix_2_valid_anchor_parses(self, tmp_git_repo: Path):
        parsed = parse_exact_skill_runtime_anchor_command(_cmd(), str(tmp_git_repo))
        assert parsed is not None
        assert parsed.command_id == "preflight.run.with_anchor"
        assert parsed.issue_number == "981"
        assert parsed.repo == TRUSTED_REPO_SLUG
        assert parsed.anchor_comment_url == _VALID_URL

    def test_preflight_run_still_parses_unaffected(self, tmp_git_repo: Path):
        """AC1/AC3: preflight.run's own 10-token parser is entirely unaffected."""
        command = (
            f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id preflight.run "
            f"--issue-number 981 --repo {TRUSTED_REPO_SLUG}"
        )
        parsed = parse_exact_skill_runtime_command(command, str(tmp_git_repo))
        assert parsed is not None
        assert parsed.command_id == "preflight.run"

    def test_matrix_3_missing_anchor_via_plain_parser_is_not_accepted(self, tmp_git_repo: Path):
        """Matrix #3: a 10-token preflight.run.with_anchor command (no anchor
        flag) must be rejected by BOTH the anchor parser (wrong token count)
        and the plain 10-token parser (execution_class mismatch guard)."""
        command = (
            f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id preflight.run.with_anchor "
            f"--issue-number 981 --repo {TRUSTED_REPO_SLUG}"
        )
        assert parse_exact_skill_runtime_anchor_command(command, str(tmp_git_repo)) is None
        assert parse_exact_skill_runtime_command(command, str(tmp_git_repo)) is None

    def test_matrix_4_anchor_on_preflight_run_rejected_by_plain_parser(self, tmp_git_repo: Path):
        """Matrix #4: preflight.run + anchor flag (13 tokens) rejected."""
        command = (
            f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id preflight.run "
            f"--issue-number 981 --repo {TRUSTED_REPO_SLUG} --anchor-comment-url {_VALID_URL}"
        )
        assert parse_exact_skill_runtime_command(command, str(tmp_git_repo)) is None
        assert parse_exact_skill_runtime_anchor_command(command, str(tmp_git_repo)) is None

    def test_matrix_5_duplicate_distinct_anchor_flags_rejected(self, tmp_git_repo: Path):
        command = _cmd() + " --anchor-comment-url https://github.com/squne121/loop-protocol/issues/981#issuecomment-2"
        assert parse_exact_skill_runtime_anchor_command(command, str(tmp_git_repo)) is None

    def test_matrix_6_duplicate_identical_anchor_flags_rejected(self, tmp_git_repo: Path):
        command = _cmd() + f" --anchor-comment-url {_VALID_URL}"
        assert parse_exact_skill_runtime_anchor_command(command, str(tmp_git_repo)) is None

    @pytest.mark.parametrize(
        ("name", "url"),
        [
            ("pull_request_review_comment", "https://github.com/squne121/loop-protocol/pull/981/files#r1"),
            ("discussion_r_fragment", "https://github.com/squne121/loop-protocol/issues/981#discussion_r1"),
            ("query_string", "https://github.com/squne121/loop-protocol/issues/981?tab=1#issuecomment-1"),
            ("trailing_slash", "https://github.com/squne121/loop-protocol/issues/981#issuecomment-1/"),
            ("userinfo", "https://user@github.com/squne121/loop-protocol/issues/981#issuecomment-1"),
            ("percent_encoded", "https://github.com/squne121/loop-protocol/issues/981%23issuecomment-1"),
            ("http_scheme", "http://github.com/squne121/loop-protocol/issues/981#issuecomment-1"),
            ("non_github_host", "https://evil.example.com/squne121/loop-protocol/issues/981#issuecomment-1"),
        ],
    )
    def test_matrix_9_to_14_url_shape_rejected(self, tmp_git_repo: Path, name: str, url: str):
        command = _cmd(url=url)
        assert parse_exact_skill_runtime_anchor_command(command, str(tmp_git_repo)) is None, name

    def test_matrix_15_eq_form_rejected(self, tmp_git_repo: Path):
        command = (
            f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id preflight.run.with_anchor "
            f"--issue-number 981 --repo {TRUSTED_REPO_SLUG} --anchor-comment-url={_VALID_URL}"
        )
        assert parse_exact_skill_runtime_anchor_command(command, str(tmp_git_repo)) is None

    def test_matrix_16_abbreviation_rejected(self, tmp_git_repo: Path):
        command = (
            f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id preflight.run.with_anchor "
            f"--issue-number 981 --repo {TRUSTED_REPO_SLUG} --anchor-comment-u {_VALID_URL}"
        )
        assert parse_exact_skill_runtime_anchor_command(command, str(tmp_git_repo)) is None

    def test_matrix_17_flag_no_value_rejected(self, tmp_git_repo: Path):
        command = (
            f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id preflight.run.with_anchor "
            f"--issue-number 981 --repo {TRUSTED_REPO_SLUG} --anchor-comment-url"
        )
        assert parse_exact_skill_runtime_anchor_command(command, str(tmp_git_repo)) is None

    def test_matrix_18_unknown_extra_flag_rejected(self, tmp_git_repo: Path):
        command = _cmd() + " --extra x"
        assert parse_exact_skill_runtime_anchor_command(command, str(tmp_git_repo)) is None

    def test_matrix_19_duplicate_flag_rejected(self, tmp_git_repo: Path):
        command = _cmd() + f" --anchor-comment-url {_VALID_URL}"
        assert parse_exact_skill_runtime_anchor_command(command, str(tmp_git_repo)) is None

    def test_matrix_20_flag_order_changed_rejected(self, tmp_git_repo: Path):
        command = (
            f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --anchor-comment-url {_VALID_URL} "
            f"--command-id preflight.run.with_anchor --issue-number 981 --repo {TRUSTED_REPO_SLUG}"
        )
        assert parse_exact_skill_runtime_anchor_command(command, str(tmp_git_repo)) is None

    @pytest.mark.parametrize(
        "bad_command",
        [
            _cmd() + ";rm -rf /",
            _cmd() + "&echo x",
            _cmd() + "|cat",
            _cmd() + "\nrm -rf /",
            _cmd() + "\x00",
        ],
    )
    def test_matrix_21_shell_metachar_rejected(self, tmp_git_repo: Path, bad_command: str):
        assert parse_exact_skill_runtime_anchor_command(bad_command, str(tmp_git_repo)) is None

    def test_matrix_22_repo_context_mismatch_rejected(self, tmp_git_repo: Path):
        command = _cmd(url="https://github.com/other/repo/issues/981#issuecomment-1")
        assert parse_exact_skill_runtime_anchor_command(command, str(tmp_git_repo)) is None

    def test_matrix_22_issue_context_mismatch_rejected(self, tmp_git_repo: Path):
        command = _cmd(
            url="https://github.com/squne121/loop-protocol/issues/999#issuecomment-1"
        )
        assert parse_exact_skill_runtime_anchor_command(command, str(tmp_git_repo)) is None


# ---------------------------------------------------------------------------
# AC3: is_exact_skill_runtime_anchor_executor_command safety boundary
# ---------------------------------------------------------------------------


class TestIsExactSkillRuntimeAnchorExecutorCommand:
    def test_allows_from_canonical_root_on_default_branch(self, tmp_git_repo: Path):
        assert is_exact_skill_runtime_anchor_executor_command(
            _cmd(), str(tmp_git_repo), str(tmp_git_repo)
        )

    def test_denies_when_cwd_is_not_project_root(self, tmp_git_repo: Path):
        subdir = tmp_git_repo / "subdir"
        subdir.mkdir()
        assert not is_exact_skill_runtime_anchor_executor_command(
            _cmd(), str(subdir), str(tmp_git_repo)
        )

    def test_denies_when_not_on_default_branch(self, tmp_git_repo: Path):
        subprocess.run(
            ["git", "-C", str(tmp_git_repo), "switch", "-c", "topic/anchor-negative"],
            check=True,
            capture_output=True,
        )
        assert not is_exact_skill_runtime_anchor_executor_command(
            _cmd(), str(tmp_git_repo), str(tmp_git_repo)
        )

    def test_denies_when_repo_slug_mismatch(self, tmp_git_repo: Path):
        subprocess.run(
            ["git", "-C", str(tmp_git_repo), "remote", "set-url", "origin", "https://github.com/other/other.git"],
            check=True,
            capture_output=True,
        )
        assert not is_exact_skill_runtime_anchor_executor_command(
            _cmd(), str(tmp_git_repo), str(tmp_git_repo)
        )

    def test_denies_malformed_command(self, tmp_git_repo: Path):
        assert not is_exact_skill_runtime_anchor_executor_command(
            _cmd() + " --extra x", str(tmp_git_repo), str(tmp_git_repo)
        )


def test_parse_exact_anchor_command_rejects_negative_matrix():
    """AC3 entrypoint referenced by the Issue's Verification Commands."""
    repo_root = str(REPO_ROOT)
    negatives = [
        f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id preflight.run.with_anchor "
        f"--issue-number 981 --repo {TRUSTED_REPO_SLUG}",  # matrix #3
        f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id preflight.run "
        f"--issue-number 981 --repo {TRUSTED_REPO_SLUG} --anchor-comment-url {_VALID_URL}",  # matrix #4
        _cmd() + f" --anchor-comment-url https://github.com/squne121/loop-protocol/issues/981#issuecomment-2",  # #5
        _cmd() + f" --anchor-comment-url {_VALID_URL}",  # #6
        f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id preflight.run.with_anchor "
        f"--issue-number 981 --repo {TRUSTED_REPO_SLUG} --anchor-comment-url={_VALID_URL}",  # #15
        f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id preflight.run.with_anchor "
        f"--issue-number 981 --repo {TRUSTED_REPO_SLUG} --anchor-comment-u {_VALID_URL}",  # #16
        f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id preflight.run.with_anchor "
        f"--issue-number 981 --repo {TRUSTED_REPO_SLUG} --anchor-comment-url",  # #17
        _cmd() + " --extra x",  # #18
        f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --anchor-comment-url {_VALID_URL} "
        f"--command-id preflight.run.with_anchor --issue-number 981 --repo {TRUSTED_REPO_SLUG}",  # #20
        _cmd() + ";rm -rf /",  # #21
        _cmd(url="https://github.com/other/repo/issues/981#issuecomment-1"),  # #22
    ]
    for command in negatives:
        assert parse_exact_skill_runtime_anchor_command(command, repo_root) is None, command
