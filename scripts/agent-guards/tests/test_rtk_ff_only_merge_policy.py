"""Issue #1589: verified fast-forward `rtk git merge --ff-only` lane.

Uses temporary local Git repositories + bare origin remotes (pytest
`tmp_path`) -- fully isolated from external network / real GitHub
credentials / the user's global Git config, per the Runtime Verification
Applicability in Issue #1589. Follows the fixture pattern established by
`test_initial_branch_publish_policy.py` (Issue #1449).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

_HOOKS_DIR = _GUARDS_DIR.parent / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

from git_mutation_command_policy import (  # noqa: E402
    COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY,
    MERGE_STATUS_DENIED,
    MERGE_STATUS_MERGED_AND_VERIFIED,
    classify_rtk_git_mutation,
    execute_verified_ff_merge_transaction,
)
from local_main_branch_guard import evaluate  # noqa: E402

ISSUE_BRANCH = "worktree-issue-1589-verified-ff-merge"


def _init_repo(repo: Path, branch: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)


def _commit(repo: Path, path: str, body: str) -> str:
    target = repo / path
    target.write_text(body)
    subprocess.run(["git", "add", path], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", body], cwd=repo, check=True)
    return (
        subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
        .stdout.strip()
    )


def _rev_parse(repo: Path, ref: str) -> str:
    return (
        subprocess.run(["git", "rev-parse", ref], cwd=repo, check=True, capture_output=True, text=True)
        .stdout.strip()
    )


def _set_canonical_env(monkeypatch: pytest.MonkeyPatch, remote: Path) -> None:
    monkeypatch.setenv("LOOP_CANONICAL_REPO_URL_PATTERN", "^" + re.escape(str(remote)) + "$")


def _make_worktree_repo_with_ahead_remote_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str = ISSUE_BRANCH
) -> tuple[Path, Path, str, str]:
    """Return (repo, remote, base_sha, ahead_sha).

    `repo` is checked out on `branch` at `base_sha`. `ahead_sha` is a
    descendant commit that already exists in the repo's local object
    database (created on a throwaway ref, never checked out) and has
    already been published to the bare `remote` as `branch`'s live head --
    i.e. exactly the "linked worktree is behind the verified remote head"
    scenario Issue #1589 targets."""
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _init_repo(repo, branch)
    base_sha = _commit(repo, "tracked.txt", "base")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=repo, check=True)

    # Build the "ahead" commit on a throwaway ref so it lands in the local
    # object database WITHOUT moving the checked-out branch pointer.
    subprocess.run(["git", "checkout", "-q", "-b", "_ahead_scratch"], cwd=repo, check=True)
    ahead_sha = _commit(repo, "tracked.txt", "ahead")
    subprocess.run(["git", "checkout", "-q", branch], cwd=repo, check=True)
    assert _rev_parse(repo, "HEAD") == base_sha

    # Publish the ahead commit to the remote branch ref directly (never
    # checking it out locally) -- simulates the remote already having moved.
    subprocess.run(["git", "push", "-q", "origin", f"{ahead_sha}:refs/heads/{branch}"], cwd=repo, check=True)

    _set_canonical_env(monkeypatch, remote)
    return repo, remote, base_sha, ahead_sha


# ---------------------------------------------------------------------------
# AC1: test_transaction_allows_live_verified_ancestor_target
# ---------------------------------------------------------------------------


def test_transaction_allows_live_verified_ancestor_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """GIVEN a clean linked-worktree-shaped repo whose live remote head is a
    verified local commit descending from local HEAD WHEN
    execute_verified_ff_merge_transaction runs THEN it fast-forwards to the
    target, reports merged_and_verified, and every postcondition holds
    (branch unchanged, HEAD == target, worktree/index clean, no residue)."""
    repo, _remote, base_sha, ahead_sha = _make_worktree_repo_with_ahead_remote_target(tmp_path, monkeypatch)

    result = execute_verified_ff_merge_transaction(str(repo), ahead_sha)

    assert result.status == MERGE_STATUS_MERGED_AND_VERIFIED
    assert result.reason_code == "verified_ff_merge_completed"
    assert result.active_branch == ISSUE_BRANCH
    assert result.verified_local_head == base_sha
    assert result.post_head == ahead_sha

    # Independent verification, bypassing the policy module entirely.
    assert _rev_parse(repo, "HEAD") == ahead_sha
    branch_out = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert branch_out == ISSUE_BRANCH
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert status.stdout.strip() == ""
    assert (repo / "tracked.txt").read_text() == "ahead"


def test_transaction_denies_non_fast_forward(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """GIVEN a target SHA that is NOT a descendant of local HEAD (diverged
    history -- the genuine non-fast-forward case) WHEN
    execute_verified_ff_merge_transaction runs THEN it denies before ever
    invoking `git merge` (target_not_descendant_of_head) -- the ancestor
    precondition check is what makes a real non-fast-forward impossible to
    reach `git merge --ff-only` in the first place."""
    repo, remote, base_sha, _ahead_sha = _make_worktree_repo_with_ahead_remote_target(tmp_path, monkeypatch)

    # Advance the LOCAL branch with a commit the remote does not have.
    local_only_sha = _commit(repo, "local_only.txt", "local-only")
    assert local_only_sha != base_sha

    # Build a genuinely diverged commit from `base_sha` (a sibling of
    # `local_only_sha`, NOT a descendant of it) and publish it as the live
    # remote head.
    subprocess.run(["git", "checkout", "-q", "-b", "_diverged"], cwd=repo, check=True)
    subprocess.run(["git", "reset", "-q", "--hard", base_sha], cwd=repo, check=True)
    diverged_sha = _commit(repo, "other.txt", "diverged")
    subprocess.run(["git", "checkout", "-q", ISSUE_BRANCH], cwd=repo, check=True)
    subprocess.run(
        ["git", "push", "-q", "-f", "origin", f"{diverged_sha}:refs/heads/{ISSUE_BRANCH}"], cwd=repo, check=True
    )

    result = execute_verified_ff_merge_transaction(str(repo), diverged_sha)
    assert result.status == MERGE_STATUS_DENIED
    assert result.reason_code == "target_not_descendant_of_head"
    assert _rev_parse(repo, "HEAD") == local_only_sha


# ---------------------------------------------------------------------------
# AC2: test_classify_always_denies_raw_command_after_transaction
# ---------------------------------------------------------------------------


def test_classify_always_denies_raw_command_after_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """GIVEN an exact `rtk git merge --ff-only <verified-sha>` command WHEN
    classify_rtk_git_mutation classifies it THEN the transaction ALREADY
    performed the real merge (HEAD really moved to the target) and the
    result is ALWAYS status == "deny" -- never "allow" -- so the caller's
    raw shell command is never independently re-run afterward (Issue #1589
    mirrors the execute_initial_branch_create_transaction pattern)."""
    repo, _remote, _base_sha, ahead_sha = _make_worktree_repo_with_ahead_remote_target(tmp_path, monkeypatch)

    command = f"rtk git merge --ff-only {ahead_sha}"
    result = classify_rtk_git_mutation(command, cwd=str(repo), require_active_branch_push=True)

    assert result is not None
    assert result.status == "deny"
    assert result.command_class == COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY
    assert result.reason_code == "verified_ff_merge_completed"

    # The merge really happened inside classify itself -- not via any
    # subsequent (never-executed) raw shell command.
    assert _rev_parse(repo, "HEAD") == ahead_sha


def test_classify_never_returns_allow_for_merge_ff_only_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """GIVEN the exact merge --ff-only shape in any outcome (success,
    rejection, or precondition failure) WHEN classify_rtk_git_mutation runs
    THEN status is NEVER "allow" for this command class -- there is no
    residual allow path a caller could exploit."""
    repo, _remote, base_sha, ahead_sha = _make_worktree_repo_with_ahead_remote_target(tmp_path, monkeypatch)
    del ahead_sha
    # Precondition failure case: target does not match the live remote head.
    unverified_sha = "b" * 40
    result = classify_rtk_git_mutation(
        f"rtk git merge --ff-only {unverified_sha}", cwd=str(repo), require_active_branch_push=True
    )
    assert result is not None
    assert result.status == "deny"
    assert result.command_class == COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY
    assert _rev_parse(repo, "HEAD") == base_sha


# ---------------------------------------------------------------------------
# AC3: test_rejects_noncanonical_merge_shapes_and_unverified_targets
# ---------------------------------------------------------------------------

_MALFORMED_MERGE_COMMANDS = [
    "short_sha",
    "rtk git merge --ff-only abc123",
    "non_hex_sha",
    "rtk git merge --ff-only " + ("g" * 40),
    "flag_reordered",
    "rtk git merge {sha} --ff-only",
    "extra_option",
    "rtk git merge --ff-only {sha} --no-edit",
    "no_ff_only_flag",
    "rtk git merge {sha}",
    "no_ff_flag",
    "rtk git merge --no-ff {sha}",
    "bare_branch_name",
    "rtk git merge feature-branch",
]


def test_rejects_noncanonical_merge_shapes_and_unverified_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """GIVEN short/non-hex SHAs, reordered flags, extra options, other merge
    shapes, raw (non-rtk) git, an absent live remote branch, and a live
    remote SHA mismatch WHEN classify_rtk_git_mutation runs THEN every case
    is denied BEFORE execute_verified_ff_merge_transaction performs any
    subprocess side effect (HEAD/remote never move for malformed shapes)."""
    repo, remote, base_sha, ahead_sha = _make_worktree_repo_with_ahead_remote_target(tmp_path, monkeypatch)

    it = iter(_MALFORMED_MERGE_COMMANDS)
    for _label, template in zip(it, it):
        command = template.format(sha=ahead_sha)
        result = classify_rtk_git_mutation(command, cwd=str(repo), require_active_branch_push=True)
        assert result is not None, command
        assert result.status == "deny", command
        assert result.command_class == COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY, command
        assert result.reason_code == "merge_shape_requires_exact_ff_only_sha", command
        # No subprocess side effect: HEAD never moved for a malformed shape.
        assert _rev_parse(repo, "HEAD") == base_sha, command

    # Raw (non-`rtk`) git is out of this policy's scope entirely -- no_match.
    raw_result = classify_rtk_git_mutation(
        f"git merge --ff-only {ahead_sha}", cwd=str(repo), require_active_branch_push=True
    )
    assert raw_result is None
    assert _rev_parse(repo, "HEAD") == base_sha

    # Live remote SHA mismatch: target differs from the live ls-remote head.
    unrelated_sha = "c" * 40
    mismatch_result = classify_rtk_git_mutation(
        f"rtk git merge --ff-only {unrelated_sha}", cwd=str(repo), require_active_branch_push=True
    )
    assert mismatch_result is not None
    assert mismatch_result.status == "deny"
    assert mismatch_result.reason_code in {"live_remote_head_mismatch", "target_not_local_commit_object"}
    assert _rev_parse(repo, "HEAD") == base_sha

    # Live remote branch absent: create a second branch never pushed to origin.
    subprocess.run(["git", "checkout", "-q", "-b", "worktree-issue-1589-unpublished"], cwd=repo, check=True)
    _commit(repo, "other2.txt", "unpublished")
    absent_case = classify_rtk_git_mutation(
        f"rtk git merge --ff-only {ahead_sha}", cwd=str(repo), require_active_branch_push=True
    )
    assert absent_case is not None
    assert absent_case.status == "deny"
    assert absent_case.reason_code == "live_remote_branch_absent"


# ---------------------------------------------------------------------------
# AC4: test_worktree_guard_rejects_dirty_root_and_unresolved_contexts
# ---------------------------------------------------------------------------


def test_worktree_guard_rejects_dirty_root_and_unresolved_contexts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """GIVEN a dirty worktree, a root/default-branch context, a non-issue
    (unresolved) branch shape, a detached HEAD, and an in-progress git
    operation residue WHEN execute_verified_ff_merge_transaction runs THEN
    every one of these contexts is denied before any merge is attempted --
    the trusted transaction is the single boundary worktree_scope_guard
    routes into for this command class (`.claude/hooks/worktree_scope_guard.py`
    dispatches ANY `rtk git ...` command through classify_rtk_git_mutation,
    so a deny here is a deny at the hook layer too)."""
    repo, _remote, base_sha, ahead_sha = _make_worktree_repo_with_ahead_remote_target(tmp_path, monkeypatch)

    # (a) dirty worktree: untracked file present.
    (repo / "untracked.txt").write_text("dirty")
    result = execute_verified_ff_merge_transaction(str(repo), ahead_sha)
    assert result.status == MERGE_STATUS_DENIED
    assert result.reason_code == "worktree_dirty"
    (repo / "untracked.txt").unlink()

    # (b) dirty index: staged but uncommitted change.
    (repo / "tracked.txt").write_text("staged-change")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    result = execute_verified_ff_merge_transaction(str(repo), ahead_sha)
    assert result.status == MERGE_STATUS_DENIED
    assert result.reason_code == "worktree_dirty"
    subprocess.run(["git", "reset", "-q", "--hard", base_sha], cwd=repo, check=True)

    # (c) root / default-branch context.
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=repo, check=True)
    result = execute_verified_ff_merge_transaction(str(repo), ahead_sha)
    assert result.status == MERGE_STATUS_DENIED
    assert result.reason_code == "merge_target_is_default_branch"
    subprocess.run(["git", "checkout", "-q", ISSUE_BRANCH], cwd=repo, check=True)
    subprocess.run(["git", "branch", "-q", "-D", "main"], cwd=repo, check=True)

    # (d) unresolved / non-issue-worktree branch shape.
    subprocess.run(["git", "checkout", "-q", "-b", "feature-not-an-issue-branch"], cwd=repo, check=True)
    result = execute_verified_ff_merge_transaction(str(repo), ahead_sha)
    assert result.status == MERGE_STATUS_DENIED
    assert result.reason_code == "active_branch_not_issue_worktree_branch"
    subprocess.run(["git", "checkout", "-q", ISSUE_BRANCH], cwd=repo, check=True)
    subprocess.run(["git", "branch", "-q", "-D", "feature-not-an-issue-branch"], cwd=repo, check=True)

    # (e) detached HEAD.
    subprocess.run(["git", "checkout", "-q", "--detach", base_sha], cwd=repo, check=True)
    result = execute_verified_ff_merge_transaction(str(repo), ahead_sha)
    assert result.status == MERGE_STATUS_DENIED
    assert result.reason_code == "detached_head_not_supported"
    subprocess.run(["git", "checkout", "-q", ISSUE_BRANCH], cwd=repo, check=True)

    # (f) in-progress git operation residue (simulated MERGE_HEAD marker).
    git_dir = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    merge_head_marker = (repo / git_dir / "MERGE_HEAD") if not git_dir.startswith("/") else Path(git_dir) / "MERGE_HEAD"
    merge_head_marker.write_text(base_sha + "\n")
    result = execute_verified_ff_merge_transaction(str(repo), ahead_sha)
    assert result.status == MERGE_STATUS_DENIED
    assert result.reason_code == "in_progress_git_operation"
    merge_head_marker.unlink()

    # Sanity: with all bad states cleared, the transaction succeeds again.
    result = execute_verified_ff_merge_transaction(str(repo), ahead_sha)
    assert result.status == MERGE_STATUS_MERGED_AND_VERIFIED


# ---------------------------------------------------------------------------
# AC5: test_local_main_and_codex_flavors_keep_root_and_destructive_denies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hook_flavor", ["claude", "codex"])
def test_local_main_and_codex_flavors_keep_root_and_destructive_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hook_flavor: str
):
    """GIVEN the local main/Codex shared guard (`local_main_branch_guard.
    evaluate`) in ROOT context (checked out on the default branch) WHEN an
    exact `rtk git merge --ff-only <sha>` command is evaluated THEN it is
    blocked for BOTH the claude and the codex hook_flavor -- the merge
    command class is never allowed from root/default branch. Also verifies
    the pre-existing `git reset --hard` / `git push --force` destructive
    denies are unaffected by adding the merge lane."""
    repo, _remote, base_sha, ahead_sha = _make_worktree_repo_with_ahead_remote_target(
        tmp_path, monkeypatch, branch="main"
    )
    del base_sha

    merge_result = evaluate(f"rtk git merge --ff-only {ahead_sha}", cwd=str(repo), hook_flavor=hook_flavor)
    assert merge_result["status"] == "block"

    # Pre-existing destructive-command denies (`rtk git reset` is not in
    # ALLOWED_RTK_GIT_SUBCOMMANDS at all; `rtk git push --force ...` is
    # denied via DENIED_PUSH_FLAGS) must remain unaffected by adding the
    # merge lane.
    reset_result = evaluate("rtk git reset --hard", cwd=str(repo), hook_flavor=hook_flavor)
    assert reset_result["status"] == "block"

    force_push_result = evaluate(
        f"rtk git push --force origin HEAD:refs/heads/{ISSUE_BRANCH}", cwd=str(repo), hook_flavor=hook_flavor
    )
    assert force_push_result["status"] == "block"
