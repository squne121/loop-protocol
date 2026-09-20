"""
test_skill_runtime_command_policy_anchor.py

Tests for `parse_exact_skill_runtime_anchor_command()` /
`is_exact_skill_runtime_anchor_executor_command()` (Issue #1498).

Covers AC3 and Positive/Negative Test Matrix items #1-#22.
"""

from __future__ import annotations

import importlib.util
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
    SKILL_RUNTIME_COMMAND_POLICY_V2,
    SKILL_RUNTIME_EXEC_REL,
    TRUSTED_REPO_SLUG,
    ExactSkillRuntimeCommand,
    command_allows_root_no_worktree,
    is_exact_skill_runtime_anchor_executor_command,
    is_exact_skill_runtime_contract_update_anchor_executor_command,
    parse_exact_skill_runtime_anchor_command,
    parse_exact_skill_runtime_contract_update_anchor_command,
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


def _contract_update_cmd(
    issue_number: str = "981",
    repo: str = TRUSTED_REPO_SLUG,
    url: str = _VALID_URL,
) -> str:
    return (
        f"uv run python3 {SKILL_RUNTIME_EXEC_REL} "
        "--command-id contract_update.run.with_anchor "
        f"--issue-number {issue_number} --repo {repo} --anchor-comment-url {url}"
    )


def _contract_update_with_human_context_cmd(
    issue_number: str = "981",
    repo: str = TRUSTED_REPO_SLUG,
    url: str = _VALID_URL,
) -> str:
    return (
        f"uv run python3 {SKILL_RUNTIME_EXEC_REL} "
        "--command-id contract_update.run.with_human_context "
        f"--issue-number {issue_number} --repo {repo} --anchor-comment-url {url} "
        f"--human-context-comment-url {url}"
    )


def _load_command_registry_module():
    """Issue #2678 AC1: load the REAL production `command_registry.py` (not
    a hand-copied stub) so `render_command()`'s actual argv template
    (including the new `investigation_evidence_transport_path`/
    `investigation_evidence_primary_root` optional_flag_pair placeholders)
    is exercised directly."""
    registry_path = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "command_registry.py"
    spec = importlib.util.spec_from_file_location(
        "command_registry_for_policy_anchor_test_2678", registry_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


class TestContractUpdateAnchorExecutorCommand:
    def test_allows_only_the_explicit_main_control_plane_phase(self, tmp_git_repo: Path):
        parsed = parse_exact_skill_runtime_contract_update_anchor_command(
            _contract_update_cmd(), str(tmp_git_repo)
        )
        assert parsed is not None
        assert parsed.command_id == "contract_update.run.with_anchor"
        assert is_exact_skill_runtime_contract_update_anchor_executor_command(
            _contract_update_cmd(), str(tmp_git_repo), str(tmp_git_repo)
        )
        # The preflight parser cannot reinterpret the mutation phase.
        assert parse_exact_skill_runtime_anchor_command(_contract_update_cmd(), str(tmp_git_repo)) is None

    def test_denies_contract_update_from_subdir_or_nondefault_branch(self, tmp_git_repo: Path):
        subdir = tmp_git_repo / "subdir"
        subdir.mkdir()
        assert not is_exact_skill_runtime_contract_update_anchor_executor_command(
            _contract_update_cmd(), str(subdir), str(tmp_git_repo)
        )
        subprocess.run(
            ["git", "-C", str(tmp_git_repo), "switch", "-c", "topic/contract-update-negative"],
            check=True,
            capture_output=True,
        )
        assert not is_exact_skill_runtime_contract_update_anchor_executor_command(
            _contract_update_cmd(), str(tmp_git_repo), str(tmp_git_repo)
        )


# ---------------------------------------------------------------------------
# Issue #2393 AC2: contract_update.run.with_human_context retains its
# required human-context argument binding and mutation-lane restrictions
# (the parser tests above are unaffected by this Issue), while its
# `network_effect` policy declaration is corrected and its
# root-no-worktree eligibility is preserved in lock-step.
# ---------------------------------------------------------------------------


class TestContractUpdateNetworkEffectCorrection:
    @pytest.mark.parametrize(
        "command_id",
        ["contract_update.run.with_anchor", "contract_update.run.with_human_context"],
    )
    def test_policy_network_effect_is_github_mutation(self, command_id: str):
        """Issue #2393: both `contract_update.run.*` profiles carry
        `mutation: True` in `command_registry.py` -- the pre-existing
        `github_read_only` policy declaration was factually wrong.
        `validate_registry_entry()` cross-checks this value against the
        registry's own declaration, so both must agree."""
        policy = SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"][command_id]
        assert policy["network_effect"] == "github_mutation"

    @pytest.mark.parametrize(
        "command_id",
        ["contract_update.run.with_anchor", "contract_update.run.with_human_context"],
    )
    def test_root_no_worktree_eligibility_survives_network_effect_correction(self, command_id: str):
        """Non-regression: correcting `network_effect` must not silently
        revoke root-no-worktree eligibility for either contract_update
        profile -- `command_allows_root_no_worktree()` compares every key
        (including `network_effect`) in `_ROOT_NO_WORKTREE_POLICY_INVARIANTS`
        against `eligible_command_ids`, so both tables were updated together."""
        parsed = ExactSkillRuntimeCommand(
            command_id=command_id,
            issue_number="981",
            repo=TRUSTED_REPO_SLUG,
            argv=(),
        )
        assert command_allows_root_no_worktree(parsed) is True


def test_parse_exact_anchor_command_rejects_negative_matrix():
    """AC3 entrypoint referenced by the Issue's Verification Commands."""
    repo_root = str(REPO_ROOT)
    negatives = [
        f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id preflight.run.with_anchor "
        f"--issue-number 981 --repo {TRUSTED_REPO_SLUG}",  # matrix #3
        f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id preflight.run "
        f"--issue-number 981 --repo {TRUSTED_REPO_SLUG} --anchor-comment-url {_VALID_URL}",  # matrix #4
        _cmd() + " --anchor-comment-url https://github.com/squne121/loop-protocol/issues/981#issuecomment-2",  # #5
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


# ---------------------------------------------------------------------------
# Issue #2678 AC1: `contract_update.run.with_human_context` (the mutation-
# phase counterpart of `preflight.run.with_human_context`) accepts the SAME
# optional `investigation_evidence_transport_path` / `investigation_evidence_
# primary_root` placeholder pair -- both at the registry-render level
# (command_registry.render_command()) and at the exact-command-parser level
# (parse_exact_skill_runtime_contract_update_anchor_command()).
# ---------------------------------------------------------------------------


def test_contract_update_with_human_context_render_accepts_investigation_evidence_transport_path(
    tmp_git_repo: Path,
) -> None:
    registry = _load_command_registry_module()

    # Transport-absent: byte-identical to the pre-#2678 argv shape (AC4).
    base_argv = registry.render_command(
        "contract_update.run.with_human_context",
        {"issue_number": 981, "repo": TRUSTED_REPO_SLUG, "anchor_comment_url": _VALID_URL},
    )
    assert base_argv == [
        "uv", "run", "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number", "981",
        "--repo", TRUSTED_REPO_SLUG,
        "--anchor-comment-url", _VALID_URL,
        "--human-context-comment-url", _VALID_URL,
        "--consume-contract-patch-plan",
    ]

    # Transport-present: the SAME two trailing optional pairs
    # `preflight.run.with_human_context` already renders, inserted before
    # `--consume-contract-patch-plan`.
    transport_argv = registry.render_command(
        "contract_update.run.with_human_context",
        {
            "issue_number": 981,
            "repo": TRUSTED_REPO_SLUG,
            "anchor_comment_url": _VALID_URL,
            "investigation_evidence_transport_path": ".claude/artifacts/issue-refinement-loop/981/manifest.json",
            "investigation_evidence_primary_root": str(tmp_git_repo),
        },
    )
    assert transport_argv == [
        "uv", "run", "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number", "981",
        "--repo", TRUSTED_REPO_SLUG,
        "--anchor-comment-url", _VALID_URL,
        "--human-context-comment-url", _VALID_URL,
        "--investigation-evidence-transport-path", ".claude/artifacts/issue-refinement-loop/981/manifest.json",
        "--investigation-evidence-primary-root", str(tmp_git_repo),
        "--consume-contract-patch-plan",
    ]

    # The exact-command parser (validating the OUTER skill_runtime_exec.py
    # invocation, not the child argv above) also accepts this command_id
    # with the transport+primary-root suffix.
    outer_command = (
        _contract_update_with_human_context_cmd(url=_VALID_URL)
        + " --investigation-evidence-transport-path .claude/artifacts/issue-refinement-loop/981/manifest.json"
        + f" --investigation-evidence-primary-root {tmp_git_repo}"
    )
    parsed = parse_exact_skill_runtime_contract_update_anchor_command(outer_command, str(tmp_git_repo))
    assert parsed is not None
    assert parsed.command_id == "contract_update.run.with_human_context"
    assert is_exact_skill_runtime_contract_update_anchor_executor_command(
        outer_command, str(tmp_git_repo), str(tmp_git_repo)
    )

    # Transport-only (no primary-root) suffix is also accepted (mirrors
    # `preflight.run.with_human_context`'s own 2-token optional suffix).
    transport_only_command = (
        _contract_update_with_human_context_cmd(url=_VALID_URL)
        + " --investigation-evidence-transport-path .claude/artifacts/issue-refinement-loop/981/manifest.json"
    )
    assert (
        parse_exact_skill_runtime_contract_update_anchor_command(transport_only_command, str(tmp_git_repo))
        is not None
    )

    # `contract_update.run.with_anchor` (no human-context lane flag) must
    # NEVER accept the transport suffix -- only the human-context lane may.
    generic_with_transport = (
        _contract_update_cmd()
        + " --investigation-evidence-transport-path .claude/artifacts/issue-refinement-loop/981/manifest.json"
    )
    assert parse_exact_skill_runtime_contract_update_anchor_command(generic_with_transport, str(tmp_git_repo)) is None


def test_contract_update_with_human_context_exact_parser_rejects_malformed_investigation_evidence_argv(
    tmp_git_repo: Path,
) -> None:
    """Issue #2678 AC3: missing, out-of-order, or extra investigation-
    evidence argv tokens on `contract_update.run.with_human_context` are all
    rejected by the exact-command parser -- no partial/duplicate/reordered
    variant is accepted."""
    base = _contract_update_with_human_context_cmd(url=_VALID_URL)
    transport_path = ".claude/artifacts/issue-refinement-loop/981/manifest.json"
    root = str(tmp_git_repo)

    malformed = [
        # Primary-root without transport-path (the second pair is only ever
        # meaningful alongside the first).
        base + f" --investigation-evidence-primary-root {root}",
        # Order reversed.
        base
        + f" --investigation-evidence-primary-root {root}"
        + f" --investigation-evidence-transport-path {transport_path}",
        # Flag with no value.
        base + " --investigation-evidence-transport-path",
        # `=` form rejected.
        base + f" --investigation-evidence-transport-path={transport_path}",
        # Duplicate transport-path flag.
        base
        + f" --investigation-evidence-transport-path {transport_path}"
        + f" --investigation-evidence-transport-path {transport_path}",
        # Primary-root value tampered to a directory other than the
        # already-verified root (must be pinned exactly to `root`).
        base
        + f" --investigation-evidence-transport-path {transport_path}"
        + " --investigation-evidence-primary-root /tmp",
        # Extra unknown trailing flag after the valid transport suffix.
        base + f" --investigation-evidence-transport-path {transport_path}" + " --extra x",
        # Unsafe (absolute) transport path.
        base + " --investigation-evidence-transport-path /etc/passwd",
        # Unsafe (traversal) transport path.
        base + " --investigation-evidence-transport-path ../../escape.json",
    ]
    for command in malformed:
        assert parse_exact_skill_runtime_contract_update_anchor_command(command, root) is None, command
        assert not is_exact_skill_runtime_contract_update_anchor_executor_command(command, root, root), command

    # Positive control: the well-formed variant IS accepted (guards against
    # an over-broad rejection that would make every negative above vacuous).
    well_formed = (
        base
        + f" --investigation-evidence-transport-path {transport_path}"
        + f" --investigation-evidence-primary-root {root}"
    )
    assert parse_exact_skill_runtime_contract_update_anchor_command(well_formed, root) is not None
