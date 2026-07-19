# Issue #1603: default-branch fast-forward sync lane (sibling to the Issue
# #1589 active-branch remote-head lane in test_rtk_ff_only_merge_policy.py).
#
# Uses temporary local Git repositories plus bare origin remotes (pytest
# tmp_path), fully isolated from external network, real GitHub credentials,
# and the local global Git config.
#
# Every test that exercises execute_verified_default_branch_ff_merge_transaction
# uses a REAL git worktree add linked worktree (not a plain git init repo),
# because the transaction's authorization is specifically about
# linked-worktree-vs-root-checkout identity and per-worktree operation-state
# resolution -- a plain repo root checkout would silently exercise the wrong
# code path.

from __future__ import annotations

import os
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
    COMMAND_CLASS_RTK_GIT_MERGE_DEFAULT_BRANCH_FF_ONLY,
    DEFAULT_BRANCH_FF_STATUS_DENIED,
    DEFAULT_BRANCH_FF_STATUS_EXECUTION_NOT_STARTED,
    DEFAULT_BRANCH_FF_STATUS_MERGED_AND_VERIFIED,
    DEFAULT_BRANCH_FF_STATUS_TRANSPORT_ERROR_AMBIGUOUS,
    DEFAULT_BRANCH_FF_STATUS_TRANSPORT_ERROR_MERGED_VERIFIED,
    DEFAULT_BRANCH_FF_STATUS_TRANSPORT_ERROR_NO_MERGE,
    classify_rtk_git_mutation,
    execute_verified_default_branch_ff_merge_transaction,
)
import git_mutation_command_policy as _policy_mod  # noqa: E402
from local_main_branch_guard import evaluate  # noqa: E402

ISSUE_NUMBER = "1603"
ISSUE_BRANCH = "worktree-issue-1603-default-branch-ff"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(repo, branch):
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)


def _commit(repo, path, body):
    target = repo / path
    target.write_text(body)
    subprocess.run(["git", "add", path], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", body], cwd=repo, check=True)
    return _rev_parse(repo, "HEAD")


def _rev_parse(repo, ref):
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _set_canonical_env(monkeypatch, remote):
    monkeypatch.setenv("LOOP_CANONICAL_REPO_URL_PATTERN", "^" + re.escape(str(remote)) + chr(92) + chr(90))


def _make_main_and_linked_worktree(tmp_path, monkeypatch, branch=ISSUE_BRANCH, advance_main=True):
    """Build a bare `remote.git` whose HEAD symref is explicitly set to
    `refs/heads/main`, an initial `main_repo` checkout that publishes
    `refs/heads/main`, and a REAL linked worktree checked out onto an
    issue-worktree-shaped branch that is behind `main`'s live head."""
    main_repo = tmp_path / "main"
    remote = tmp_path / "remote.git"
    main_repo.mkdir()
    _init_repo(main_repo, "main")
    base_sha = _commit(main_repo, "tracked.txt", "base")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    _git("remote", "add", "origin", str(remote), cwd=main_repo)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=main_repo)
    # A plain `git init --bare` repo does NOT auto-update its own HEAD symref
    # on push (that auto-set-HEAD-on-first-push behavior is GitHub-server-side
    # logic, not plain git). Set it explicitly so `git ls-remote --symref`
    # against this bare repo reports `HEAD` as `ref: refs/heads/main` --
    # exactly the live identity the transaction under test verifies.
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)

    worktree = tmp_path / "worktree"
    _git("worktree", "add", "-q", "-b", branch, str(worktree), cwd=main_repo)
    _git("push", "-q", "origin", "HEAD:refs/heads/" + branch, cwd=worktree)

    main_ahead_sha = base_sha
    if advance_main:
        main_ahead_sha = _commit(main_repo, "tracked.txt", "main-ahead")
        _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=main_repo)

    _set_canonical_env(monkeypatch, remote)
    return worktree, remote, base_sha, main_ahead_sha


# AC1: the shape classifier is PURE (no execution) and the trusted
# transaction fetches and merges the live default-branch head.
def test_transaction_fetches_and_merges_live_default_branch_head(tmp_path, monkeypatch):
    worktree, _remote, base_sha, main_ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)

    command = "rtk git merge --ff-only origin/main"
    classify_result = classify_rtk_git_mutation(command, cwd=str(worktree), require_active_branch_push=True)
    assert classify_result is not None
    assert classify_result.status == "allow"
    assert classify_result.command_class == COMMAND_CLASS_RTK_GIT_MERGE_DEFAULT_BRANCH_FF_ONLY
    assert classify_result.target_branch == "main"
    # No subprocess side effect from classification alone.
    assert _rev_parse(worktree, "HEAD") == base_sha

    result = execute_verified_default_branch_ff_merge_transaction(
        str(worktree), "main", expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == DEFAULT_BRANCH_FF_STATUS_MERGED_AND_VERIFIED
    assert result.reason_code == "verified_default_branch_ff_merge_completed"
    assert result.live_default_branch_oid == main_ahead_sha
    assert result.post_head == main_ahead_sha
    assert _rev_parse(worktree, "HEAD") == main_ahead_sha
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=worktree, capture_output=True, text=True, check=True
    )
    assert status.stdout.strip() == ""


# AC2: the transaction requires a clean, attached, non-default linked
# worktree bound to the active Issue, canonical origin, live-remote/fetched-
# ref match, and forward ancestry -- confirming active branch, HEAD, and
# clean state postcondition on success.
def test_transaction_requires_clean_linked_issue_worktree_and_verified_ancestry(tmp_path, monkeypatch):
    worktree, _remote, base_sha, main_ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)
    main_repo = tmp_path / "main"

    # Dirty worktree denies before any merge.
    (worktree / "tracked.txt").write_text("dirty")
    dirty_result = execute_verified_default_branch_ff_merge_transaction(
        str(worktree), "main", expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert dirty_result.status == DEFAULT_BRANCH_FF_STATUS_DENIED
    assert dirty_result.reason_code == "worktree_dirty"
    _git("checkout", "-q", "--", "tracked.txt", cwd=worktree)

    # Root checkout (the primary, non-linked worktree) is denied even with
    # canonical origin/clean state, because it is not a LINKED worktree.
    root_result = execute_verified_default_branch_ff_merge_transaction(
        str(main_repo), "main", expected_worktree_realpath=str(main_repo), active_issue_number=ISSUE_NUMBER
    )
    assert root_result.status == DEFAULT_BRANCH_FF_STATUS_DENIED
    assert root_result.reason_code == "expected_worktree_is_root_checkout"

    # cwd not matching the expected worktree realpath denies.
    other_dir = tmp_path / "unrelated"
    other_dir.mkdir()
    cwd_mismatch_result = execute_verified_default_branch_ff_merge_transaction(
        str(worktree), "main", expected_worktree_realpath=str(other_dir), active_issue_number=ISSUE_NUMBER
    )
    assert cwd_mismatch_result.status == DEFAULT_BRANCH_FF_STATUS_DENIED
    assert cwd_mismatch_result.reason_code == "cwd_not_expected_worktree"

    # Branch/issue-number mismatch denies.
    mismatch_result = execute_verified_default_branch_ff_merge_transaction(
        str(worktree), "main", expected_worktree_realpath=str(worktree), active_issue_number="9999"
    )
    assert mismatch_result.status == DEFAULT_BRANCH_FF_STATUS_DENIED
    assert mismatch_result.reason_code == "branch_issue_number_mismatch"

    # Valid transaction merges and confirms postconditions.
    result = execute_verified_default_branch_ff_merge_transaction(
        str(worktree), "main", expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == DEFAULT_BRANCH_FF_STATUS_MERGED_AND_VERIFIED
    assert result.active_branch == ISSUE_BRANCH
    assert result.verified_local_head == base_sha
    assert result.post_head == main_ahead_sha


# Only shapes with EXACTLY 2 tokens where the first is `--ff-only` and the
# second already starts with `origin/` reach
# `_classify_rtk_git_merge_default_branch` at all (`classify_rtk_git_mutation`
# routes on that precondition). These are the shapes that reach it and are
# denied by its OWN candidate-shape validation (invalid ref grammar).
_MALFORMED_DEFAULT_BRANCH_COMMANDS = [
    "invalid_candidate_shape", "rtk git merge --ff-only origin/../main",
    "invalid_candidate_slash", "rtk git merge --ff-only origin/feature/x",
    "invalid_candidate_empty", "rtk git merge --ff-only origin/",
]


# AC3: non-canonical shapes and unverified live states are rejected before
# any merge is attempted, fail-closed.
def test_rejects_noncanonical_default_branch_sync_shapes_and_unverified_states(tmp_path, monkeypatch):
    worktree, remote, base_sha, main_ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)

    it = iter(_MALFORMED_DEFAULT_BRANCH_COMMANDS)
    for _label, command in zip(it, it):
        result = classify_rtk_git_mutation(command, cwd=str(worktree), require_active_branch_push=True)
        assert result is not None, command
        assert result.status == "deny", command
        assert result.command_class == COMMAND_CLASS_RTK_GIT_MERGE_DEFAULT_BRANCH_FF_ONLY, command
        assert result.reason_code == "default_branch_sync_shape_requires_exact_ff_only_origin_ref", command
        assert _rev_parse(worktree, "HEAD") == base_sha, command

    # Shapes that violate the routing precondition itself (no `origin/`
    # prefix, `--ff-only` not the first/only-other token, flag reordered,
    # extra option, `--no-ff`, a bare branch name/short SHA) fall through to
    # the sibling classifier (Issue #1589), which also denies them --
    # fail-closed either way, just under the other command class.
    for other_class_command in (
        "rtk git merge --ff-only main",
        "rtk git merge origin/main --ff-only",
        "rtk git merge origin/main",
        "rtk git merge --no-ff origin/main",
        "rtk git merge --ff-only origin/main --no-edit",
    ):
        other_result = classify_rtk_git_mutation(
            other_class_command, cwd=str(worktree), require_active_branch_push=True
        )
        assert other_result is not None, other_class_command
        assert other_result.status == "deny", other_class_command

    # A candidate that is NOT the live default branch is denied via the
    # live identity probe (the classifier itself cannot know this, only the
    # trusted transaction can).
    not_default_result = execute_verified_default_branch_ff_merge_transaction(
        str(worktree), "not-a-real-branch", expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert not_default_result.status == DEFAULT_BRANCH_FF_STATUS_DENIED
    assert not_default_result.reason_code == "live_default_branch_identity_mismatch"
    assert _rev_parse(worktree, "HEAD") == base_sha

    # Non-fast-forward: local HEAD (still on ISSUE_BRANCH, at base_sha)
    # diverges from the live default branch via a sibling commit -- local
    # HEAD is NOT an ancestor of main's live head after this commit.
    diverged_sha = _commit(worktree, "other.txt", "diverged")
    _git("push", "-q", "-f", "origin", diverged_sha + ":refs/heads/" + ISSUE_BRANCH, cwd=worktree)
    non_ff_result = execute_verified_default_branch_ff_merge_transaction(
        str(worktree), "main", expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert non_ff_result.status == DEFAULT_BRANCH_FF_STATUS_DENIED
    assert non_ff_result.reason_code == "target_not_descendant_of_head"
    assert _rev_parse(worktree, "HEAD") == diverged_sha

    # Raw (non-rtk) git is out of this policy's scope entirely.
    raw_result = classify_rtk_git_mutation(
        "git merge --ff-only origin/main", cwd=str(worktree), require_active_branch_push=True
    )
    assert raw_result is None


def _patch_merge_subprocess(monkeypatch, handler):
    original_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if argv[:3] == ["git", "merge", "--ff-only"]:
            return handler(argv, kwargs, original_run)
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(_policy_mod.subprocess, "run", fake_run)


def test_transaction_classifies_oserror_as_execution_not_started(tmp_path, monkeypatch):
    worktree, _remote, base_sha, _main_ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)

    def handler(argv, kwargs, original_run):
        raise OSError("git executable not found")

    _patch_merge_subprocess(monkeypatch, handler)
    result = execute_verified_default_branch_ff_merge_transaction(
        str(worktree), "main", expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == DEFAULT_BRANCH_FF_STATUS_EXECUTION_NOT_STARTED
    assert result.reason_code == "merge_execution_not_started"
    assert _rev_parse(worktree, "HEAD") == base_sha


def test_transaction_classifies_timeout_after_real_merge_as_merged_and_verified(tmp_path, monkeypatch):
    worktree, _remote, base_sha, main_ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)

    def handler(argv, kwargs, original_run):
        original_run(argv, **kwargs)
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 30))

    _patch_merge_subprocess(monkeypatch, handler)
    result = execute_verified_default_branch_ff_merge_transaction(
        str(worktree), "main", expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == DEFAULT_BRANCH_FF_STATUS_TRANSPORT_ERROR_MERGED_VERIFIED
    assert result.reason_code == "merge_execution_timeout_but_merged_and_verified"
    assert _rev_parse(worktree, "HEAD") == main_ahead_sha


def test_transaction_classifies_timeout_with_no_merge_as_no_merge_observed(tmp_path, monkeypatch):
    worktree, _remote, base_sha, _main_ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)

    def handler(argv, kwargs, original_run):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 30))

    _patch_merge_subprocess(monkeypatch, handler)
    result = execute_verified_default_branch_ff_merge_transaction(
        str(worktree), "main", expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == DEFAULT_BRANCH_FF_STATUS_TRANSPORT_ERROR_NO_MERGE
    assert result.reason_code == "merge_execution_timeout_no_merge_observed"
    assert _rev_parse(worktree, "HEAD") == base_sha


def _git_path(cwd, relative):
    result = subprocess.run(
        ["git", "rev-parse", "--git-path", relative], cwd=cwd, check=True, capture_output=True, text=True
    )
    out = result.stdout.strip()
    return out if os.path.isabs(out) else os.path.join(str(cwd), out)


def test_transaction_classifies_timeout_with_operation_residue_as_ambiguous(tmp_path, monkeypatch):
    worktree, _remote, base_sha, _main_ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)

    def handler(argv, kwargs, original_run):
        marker = Path(_git_path(worktree, "MERGE_HEAD"))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(base_sha + chr(10))
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 30))

    _patch_merge_subprocess(monkeypatch, handler)
    result = execute_verified_default_branch_ff_merge_transaction(
        str(worktree), "main", expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == DEFAULT_BRANCH_FF_STATUS_TRANSPORT_ERROR_AMBIGUOUS
    assert result.reason_code == "merge_execution_timeout_state_ambiguous"


# AC4: Claude worktree guard, shared local-main guard, and Codex execpolicy
# routing parity -- local root/main checkout is denied by the shared guard
# regardless of the new command class, and Codex allows only the dedicated
# executor's exact shape.
@pytest.mark.parametrize("hook_flavor", ["claude", "codex"])
def test_claude_codex_and_local_main_routing_parity(tmp_path, monkeypatch, hook_flavor):
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _init_repo(repo, "main")
    _commit(repo, "tracked.txt", "base")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=repo)

    merge_result = evaluate("rtk git merge --ff-only origin/main", cwd=str(repo), hook_flavor=hook_flavor)
    assert merge_result["status"] == "block"
    reset_result = evaluate("rtk git reset --hard", cwd=str(repo), hook_flavor=hook_flavor)
    assert reset_result["status"] == "block"
    force_push_result = evaluate(
        "rtk git push --force origin HEAD:refs/heads/" + ISSUE_BRANCH, cwd=str(repo), hook_flavor=hook_flavor
    )
    assert force_push_result["status"] == "block"


_REPO_ROOT = _GUARDS_DIR.parent.parent
_DEFAULT_RULES = _REPO_ROOT / ".codex" / "rules" / "default.rules"
_CODEX_BIN = None
for _candidate_dir in os.environ.get("PATH", "").split(os.pathsep):
    _candidate = os.path.join(_candidate_dir, "codex")
    if os.path.isfile(_candidate) and os.access(_candidate, os.X_OK):
        _CODEX_BIN = _candidate
        break


def _execpolicy_decision(argv_tail):
    result = subprocess.run(
        [_CODEX_BIN, "execpolicy", "check", "--rules", str(_DEFAULT_RULES), "--"] + argv_tail,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    import json as _json

    return _json.loads(result.stdout).get("decision", "no_match")


@pytest.mark.skipif(_CODEX_BIN is None, reason="codex CLI not available in this environment")
def test_codex_execpolicy_allows_only_the_dedicated_executor_shape():
    allow_argv = [
        "uv", "run", "--locked", "--no-sync", "python3",
        "scripts/agent-ops/verified_default_branch_ff_merge_exec.py", "--candidate-branch", "main",
    ]
    assert _execpolicy_decision(allow_argv) == "allow"

    prompt_cases = [
        ["rtk", "git", "merge", "--ff-only", "origin/main"],
        ["rtk", "git", "merge", "origin/main"],
        ["rtk", "git", "merge", "--ff-only", "origin/main", "--no-edit"],
        ["rtk", "git", "merge", "feature-branch"],
        ["bash", "-c", "rtk git merge --ff-only origin/main"],
    ]
    for argv_tail in prompt_cases:
        assert _execpolicy_decision(argv_tail) != "allow", argv_tail
