"""
test_owner_reaction_decide_flag_validation.py

Issue #2688 AC4: exact required/forbidden combination validation for
`--owner-user-id`, `--preview-binding-file` (production and fixture), and
the fixture-only `--gh-fixture-file`, enforced by
`scripts/agent-guards/skill_runtime_exec.py`'s outer dispatch.

Every scenario below invokes the REAL `skill_runtime_exec.py` as a real
subprocess (never a direct function call) -- both the negative (rejected)
combinations and the positive (accepted) combinations for each of the two
command profiles, so the boundary is proven "exact" in both directions, not
merely "some invalid combination is rejected".
"""

from __future__ import annotations

import json

from owner_reaction_dispatch_fixture import (
    install_fixture,
    make_repo,
    run_executor,
    write_gh_state,
    write_preview_binding,
)

ISSUE_NUMBER = 2689
OWNER_USER_ID = 4343
COMMENT_ID = 6001
ANCHOR_COMMENT_ID = 6002
COMMENT_BODY = "owner reacts here (flag validation)\n"
ANCHOR_BODY = "anchor options here (flag validation)\n"
ISSUE_BODY = "issue body snapshot (flag validation)\n"
REACTION_OPTION_MAP = {"+1": "option_a"}
OPTIONS = {"option_a": {"operation": "close", "target": "issue"}}
REACTIONS = [{"id": 1, "content": "+1", "user": {"id": OWNER_USER_ID}}]


def _artifact_dir(repo):
    return repo / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER)


def _seed(repo):
    binding_path = _artifact_dir(repo) / "preview_binding.json"
    write_preview_binding(
        binding_path,
        comment_id=COMMENT_ID,
        comment_body=COMMENT_BODY,
        anchor_comment_id=ANCHOR_COMMENT_ID,
        anchor_body=ANCHOR_BODY,
        issue_body=ISSUE_BODY,
        reaction_option_map=REACTION_OPTION_MAP,
        options=OPTIONS,
    )
    gh_fixture_path = _artifact_dir(repo) / "gh_fixture.json"
    write_gh_state(
        gh_fixture_path,
        repo="squne121/loop-protocol",
        issue_number=ISSUE_NUMBER,
        owner_user_id=OWNER_USER_ID,
        comment_id=COMMENT_ID,
        comment_body=COMMENT_BODY,
        anchor_comment_id=ANCHOR_COMMENT_ID,
        anchor_body=ANCHOR_BODY,
        issue_body=ISSUE_BODY,
        reactions=REACTIONS,
    )
    return (
        str(binding_path.relative_to(repo)),
        str(gh_fixture_path.relative_to(repo)),
    )


def _env():
    return {"LOOP_ISSUE_NUMBER": str(ISSUE_NUMBER)}


# ---------------------------------------------------------------------------
# Negative: missing required flags.
# ---------------------------------------------------------------------------


def test_owner_reaction_decide_rejects_missing_owner_user_id(tmp_path):
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")
    preview_binding_rel, _ = _seed(repo)
    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--preview-binding-file", preview_binding_rel,
        ],
        extra_env=_env(),
    )
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "--owner-user-id and --preview-binding-file are required" in result.stderr, result.stderr


def test_owner_reaction_decide_rejects_missing_preview_binding_file(tmp_path):
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")
    _seed(repo)
    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
        ],
        extra_env=_env(),
    )
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "--owner-user-id and --preview-binding-file are required" in result.stderr, result.stderr


def test_owner_reaction_decide_fixture_rejects_missing_gh_fixture_file(tmp_path):
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")
    preview_binding_rel, _ = _seed(repo)
    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide.fixture",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
        ],
        extra_env=_env(),
    )
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "--gh-fixture-file is required for owner_reaction.decide.fixture" in result.stderr, result.stderr


# ---------------------------------------------------------------------------
# Negative: forbidden flags for the wrong command_id / wrong profile.
# ---------------------------------------------------------------------------


def test_owner_reaction_decide_production_rejects_gh_fixture_file(tmp_path):
    """Production `owner_reaction.decide` must never accept the fixture-only
    `--gh-fixture-file` -- otherwise a caller could silently bypass the real
    `gh` network boundary from what looks like the production command_id."""
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")
    preview_binding_rel, gh_fixture_rel = _seed(repo)
    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
            "--gh-fixture-file", gh_fixture_rel,
        ],
        extra_env=_env(),
    )
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "--gh-fixture-file is only allowed for owner_reaction.decide.fixture" in result.stderr, result.stderr


def test_other_command_id_rejects_owner_user_id(tmp_path):
    """A command_id other than owner_reaction.decide/.fixture must never
    accept --owner-user-id / --preview-binding-file."""
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")
    preview_binding_rel, _ = _seed(repo)
    result = run_executor(
        repo,
        [
            "--command-id", "preflight.run",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
        ],
        extra_env=_env(),
    )
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert (
        "--owner-user-id/--preview-binding-file are only allowed for "
        "owner_reaction.decide/owner_reaction.decide.fixture" in result.stderr
    ), result.stderr


def test_other_command_id_rejects_gh_fixture_file(tmp_path):
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")
    _, gh_fixture_rel = _seed(repo)
    result = run_executor(
        repo,
        [
            "--command-id", "preflight.run",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--gh-fixture-file", gh_fixture_rel,
        ],
        extra_env=_env(),
    )
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "--gh-fixture-file is only allowed for owner_reaction.decide.fixture" in result.stderr, result.stderr


def test_owner_reaction_decide_rejects_cross_command_flags(tmp_path):
    """owner_reaction.decide must reject flags belonging to other command
    classes (e.g. --loop-state-file, which is decide.run's)."""
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")
    preview_binding_rel, _ = _seed(repo)
    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
            "--loop-state-file", "some/path.json",
        ],
        extra_env=_env(),
    )
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "only --owner-user-id/--preview-binding-file are allowed for owner_reaction.decide" in result.stderr, (
        result.stderr
    )


# ---------------------------------------------------------------------------
# Positive: the exact accepted combination for each profile is dispatched
# through the real skill_runtime_exec.py subprocess to a genuine result
# (never rejected by the flag-combination gate above).
# ---------------------------------------------------------------------------


def test_owner_reaction_decide_accepts_exact_production_combination(tmp_path):
    repo = make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    install_fixture(repo, trusted_gh_bin)
    preview_binding_rel, gh_fixture_rel = _seed(repo)
    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
        ],
        extra_env={
            **_env(),
            "SKILL_RUNTIME_TEST_OWNER_REACTION_GH_STATE_FILE": str(repo / gh_fixture_rel),
        },
    )
    assert "exact command class rejected" not in result.stderr, result.stderr
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["status"] == "selected", payload


def test_owner_reaction_decide_fixture_accepts_exact_fixture_combination(tmp_path):
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")
    preview_binding_rel, gh_fixture_rel = _seed(repo)
    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide.fixture",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", "squne121/loop-protocol",
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
            "--gh-fixture-file", gh_fixture_rel,
        ],
        extra_env=_env(),
    )
    assert "exact command class rejected" not in result.stderr, result.stderr
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["status"] == "selected", payload
