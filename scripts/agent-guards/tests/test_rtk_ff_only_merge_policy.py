# Issue 1589 / 1609 fix_delta: verified fast-forward rtk git merge --ff-only lane.
#
# Uses temporary local Git repositories plus bare origin remotes (pytest
# tmp_path), fully isolated from external network, real GitHub credentials,
# and the local global Git config.
#
# Every test that exercises execute_verified_ff_merge_transaction uses a REAL
# git worktree add linked worktree (not a plain git init repo), because the
# fix_delta P0 / P1 fixes are specifically about linked-worktree-vs-root-checkout
# identity and per-worktree operation-state resolution -- a plain repo root
# checkout would silently exercise the wrong code path.

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
    COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY,
    MERGE_STATUS_DENIED,
    MERGE_STATUS_EXECUTION_NOT_STARTED,
    MERGE_STATUS_MERGED_AND_VERIFIED,
    MERGE_STATUS_TRANSPORT_ERROR_AMBIGUOUS,
    MERGE_STATUS_TRANSPORT_ERROR_MERGED_VERIFIED,
    MERGE_STATUS_TRANSPORT_ERROR_NO_MERGE,
    classify_rtk_git_mutation,
    execute_verified_ff_merge_transaction,
)
import git_mutation_command_policy as _policy_mod  # noqa: E402
from local_main_branch_guard import evaluate  # noqa: E402

ISSUE_NUMBER = "1589"
ISSUE_BRANCH = "worktree-issue-1589-verified-ff-merge"


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
def _make_main_and_linked_worktree(tmp_path, monkeypatch, branch=ISSUE_BRANCH, publish_ahead=True):
    main_repo = tmp_path / "main"
    remote = tmp_path / "remote.git"
    main_repo.mkdir()
    _init_repo(main_repo, "main")
    base_sha = _commit(main_repo, "tracked.txt", "base")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    _git("remote", "add", "origin", str(remote), cwd=main_repo)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=main_repo)

    worktree = tmp_path / "worktree"
    _git("worktree", "add", "-q", "-b", branch, str(worktree), cwd=main_repo)
    _git("push", "-q", "origin", "HEAD:refs/heads/" + branch, cwd=worktree)

    ahead_sha = base_sha
    if publish_ahead:
        _git("checkout", "-q", "-b", "_ahead_scratch", cwd=worktree)
        ahead_sha = _commit(worktree, "tracked.txt", "ahead")
        _git("checkout", "-q", branch, cwd=worktree)
        assert _rev_parse(worktree, "HEAD") == base_sha
        _git("push", "-q", "-f", "origin", ahead_sha + ":refs/heads/" + branch, cwd=worktree)

    _set_canonical_env(monkeypatch, remote)
    return worktree, remote, base_sha, ahead_sha


def _git_path(cwd, relative):
    result = subprocess.run(
        ["git", "rev-parse", "--git-path", relative], cwd=cwd, check=True, capture_output=True, text=True
    )
    out = result.stdout.strip()
    return out if os.path.isabs(out) else os.path.join(str(cwd), out)

# P0 Blocker regression: classify_rtk_git_mutation must be a PURE shape
# classifier for the merge lane -- it must never execute a real merge, and
# it must return status allow (pending caller authorization) for an exact
# valid shape, never deny and never a self-executed transaction outcome.
def test_classify_is_pure_shape_classifier_and_never_executes(tmp_path, monkeypatch):
    worktree, _remote, base_sha, ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)
    command = "rtk git merge --ff-only " + ahead_sha
    result = classify_rtk_git_mutation(command, cwd=str(worktree), require_active_branch_push=True)
    assert result is not None
    assert result.status == "allow"
    assert result.command_class == COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY
    assert result.target_sha == ahead_sha
    # No subprocess side effect: HEAD must not have moved -- classify never
    # calls execute_verified_ff_merge_transaction.
    assert _rev_parse(worktree, "HEAD") == base_sha


_MALFORMED_MERGE_COMMANDS = [
    "short_sha", "rtk git merge --ff-only abc123",
    "non_hex_sha", "rtk git merge --ff-only " + ("g" * 40),
    "uppercase_sha", None,
    "flag_reordered", "rtk git merge SHA --ff-only",
    "extra_option", "rtk git merge --ff-only SHA --no-edit",
    "no_ff_only_flag", "rtk git merge SHA",
    "no_ff_flag", "rtk git merge --no-ff SHA",
    "bare_branch_name", "rtk git merge feature-branch",
]


def test_classify_rejects_malformed_shapes_and_uppercase_sha(tmp_path, monkeypatch):
    worktree, _remote, base_sha, ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)
    it = iter(_MALFORMED_MERGE_COMMANDS)
    for label, template in zip(it, it):
        if label == "uppercase_sha":
            command = "rtk git merge --ff-only " + ahead_sha.upper()
        else:
            command = template.replace("SHA", ahead_sha)
        result = classify_rtk_git_mutation(command, cwd=str(worktree), require_active_branch_push=True)
        assert result is not None, command
        assert result.status == "deny", command
        assert result.command_class == COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY, command
        assert result.reason_code == "merge_shape_requires_exact_ff_only_sha", command
        assert _rev_parse(worktree, "HEAD") == base_sha, command
    # Raw (non-rtk) git is out of this policy scope entirely -- no_match.
    raw_result = classify_rtk_git_mutation(
        "git merge --ff-only " + ahead_sha, cwd=str(worktree), require_active_branch_push=True
    )
    assert raw_result is None
    assert _rev_parse(worktree, "HEAD") == base_sha

def test_transaction_allows_verified_ancestor_target_in_linked_worktree(tmp_path, monkeypatch):
    worktree, _remote, base_sha, ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)
    result = execute_verified_ff_merge_transaction(
        str(worktree), ahead_sha, expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == MERGE_STATUS_MERGED_AND_VERIFIED
    assert result.reason_code == "verified_ff_merge_completed"
    assert result.post_head == ahead_sha
    assert _rev_parse(worktree, "HEAD") == ahead_sha
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=worktree, capture_output=True, text=True, check=True
    )
    assert status.stdout.strip() == ""


def test_transaction_denies_non_fast_forward(tmp_path, monkeypatch):
    worktree, _remote, base_sha, _ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)
    local_only_sha = _commit(worktree, "local_only.txt", "local-only")
    assert local_only_sha != base_sha
    _git("checkout", "-q", "-b", "_diverged", cwd=worktree)
    _git("reset", "-q", "--hard", base_sha, cwd=worktree)
    diverged_sha = _commit(worktree, "other.txt", "diverged")
    _git("checkout", "-q", ISSUE_BRANCH, cwd=worktree)
    _git("push", "-q", "-f", "origin", diverged_sha + ":refs/heads/" + ISSUE_BRANCH, cwd=worktree)
    result = execute_verified_ff_merge_transaction(
        str(worktree), diverged_sha, expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == MERGE_STATUS_DENIED
    assert result.reason_code == "target_not_descendant_of_head"
    assert _rev_parse(worktree, "HEAD") == local_only_sha


def test_transaction_denies_root_checkout_even_when_all_else_valid(tmp_path, monkeypatch):
    main_repo = tmp_path / "main"
    remote = tmp_path / "remote.git"
    main_repo.mkdir()
    _init_repo(main_repo, "main")
    _commit(main_repo, "tracked.txt", "base")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    _git("remote", "add", "origin", str(remote), cwd=main_repo)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=main_repo)
    _git("checkout", "-q", "-b", ISSUE_BRANCH, cwd=main_repo)
    ahead_sha = _commit(main_repo, "tracked.txt", "ahead-in-root")
    _git("push", "-q", "origin", "HEAD:refs/heads/" + ISSUE_BRANCH, cwd=main_repo)
    _set_canonical_env(monkeypatch, remote)
    result = execute_verified_ff_merge_transaction(
        str(main_repo), ahead_sha, expected_worktree_realpath=str(main_repo), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == MERGE_STATUS_DENIED
    assert result.reason_code == "expected_worktree_is_root_checkout"


def test_transaction_denies_cwd_mismatch_with_expected_worktree(tmp_path, monkeypatch):
    worktree, _remote, _base_sha, ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)
    other_dir = tmp_path / "unrelated"
    other_dir.mkdir()
    result = execute_verified_ff_merge_transaction(
        str(worktree), ahead_sha, expected_worktree_realpath=str(other_dir), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == MERGE_STATUS_DENIED
    assert result.reason_code == "cwd_not_expected_worktree"


def test_transaction_denies_branch_issue_number_mismatch(tmp_path, monkeypatch):
    worktree, _remote, _base_sha, ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)
    result = execute_verified_ff_merge_transaction(
        str(worktree), ahead_sha, expected_worktree_realpath=str(worktree), active_issue_number="9999"
    )
    assert result.status == MERGE_STATUS_DENIED
    assert result.reason_code == "branch_issue_number_mismatch"

@pytest.mark.parametrize("marker_name,is_dir", [
    ("MERGE_HEAD", False),
    ("CHERRY_PICK_HEAD", False),
    ("REVERT_HEAD", False),
    ("BISECT_LOG", False),
    ("rebase-merge", True),
    ("rebase-apply", True),
])
def test_transaction_denies_in_progress_operation_in_real_linked_worktree(tmp_path, monkeypatch, marker_name, is_dir):
    worktree, _remote, base_sha, ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)
    marker_path = Path(_git_path(worktree, marker_name))
    # Confirm the marker resolves under the PER-WORKTREE admin dir, not the
    # shared common dir shared with the main checkout (P1 Blocker fix).
    common_dir = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"], cwd=worktree, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert str(marker_path) != os.path.join(common_dir, marker_name)
    if is_dir:
        marker_path.mkdir(parents=True, exist_ok=True)
    else:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(base_sha + chr(10))
    result = execute_verified_ff_merge_transaction(
        str(worktree), ahead_sha, expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == MERGE_STATUS_DENIED
    assert result.reason_code == "in_progress_git_operation"
    if is_dir:
        marker_path.rmdir()
    else:
        marker_path.unlink()
    result = execute_verified_ff_merge_transaction(
        str(worktree), ahead_sha, expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == MERGE_STATUS_MERGED_AND_VERIFIED

# P1 Blocker regression: the canonical-remote check must validate the
# resolved FETCH url (git remote get-url origin, no --push), and the SAME
# url must be used for the live remote probe -- never the push url, and
# never the bare remote name.
def test_canonical_check_uses_fetch_url_ignores_noncanonical_push_url(tmp_path, monkeypatch):
    worktree, remote, _base_sha, ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)
    decoy_pushurl = tmp_path / "decoy-push-target.git"
    subprocess.run(["git", "init", "--bare", "-q", str(decoy_pushurl)], check=True)
    _git("remote", "set-url", "--push", "origin", str(decoy_pushurl), cwd=worktree)
    result = execute_verified_ff_merge_transaction(
        str(worktree), ahead_sha, expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == MERGE_STATUS_MERGED_AND_VERIFIED


def test_canonical_check_denies_when_fetch_url_is_noncanonical_even_if_push_url_is_canonical(tmp_path, monkeypatch):
    worktree, remote, _base_sha, ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)
    fake_canonical = tmp_path / "fake-canonical-remote.git"
    monkeypatch.setenv("LOOP_CANONICAL_REPO_URL_PATTERN", "^" + re.escape(str(fake_canonical)) + chr(92) + chr(90))
    _git("remote", "set-url", "--push", "origin", str(fake_canonical), cwd=worktree)
    result = execute_verified_ff_merge_transaction(
        str(worktree), ahead_sha, expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == MERGE_STATUS_DENIED
    assert result.reason_code == "origin_remote_identity_mismatch"

def _is_merge_ff_only_argv(argv):
    return argv[:3] == ["git", "merge", "--ff-only"]


def _patch_merge_subprocess(monkeypatch, handler):
    original_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if _is_merge_ff_only_argv(argv):
            return handler(argv, kwargs, original_run)
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(_policy_mod.subprocess, "run", fake_run)

# P1 Blocker regression: an OSError spawning git merge means the process
# never started, distinct from a timeout, where it may have completed.
def test_transaction_classifies_oserror_as_execution_not_started(tmp_path, monkeypatch):
    worktree, _remote, base_sha, ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)

    def handler(argv, kwargs, original_run):
        raise OSError("git executable not found")

    _patch_merge_subprocess(monkeypatch, handler)
    result = execute_verified_ff_merge_transaction(
        str(worktree), ahead_sha, expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == MERGE_STATUS_EXECUTION_NOT_STARTED
    assert result.reason_code == "merge_execution_not_started"
    assert _rev_parse(worktree, "HEAD") == base_sha

# P1 Blocker regression: a timeout must NEVER be folded into a bare deny --
# if the merge actually completed before the transport timed out, an
# unconditional postcondition readback must observe and report that.
def test_transaction_classifies_timeout_after_real_merge_as_merged_and_verified(tmp_path, monkeypatch):
    worktree, _remote, base_sha, ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)

    def handler(argv, kwargs, original_run):
        original_run(argv, **kwargs)
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 30))

    _patch_merge_subprocess(monkeypatch, handler)
    result = execute_verified_ff_merge_transaction(
        str(worktree), ahead_sha, expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == MERGE_STATUS_TRANSPORT_ERROR_MERGED_VERIFIED
    assert result.reason_code == "merge_execution_timeout_but_merged_and_verified"
    assert _rev_parse(worktree, "HEAD") == ahead_sha

def test_transaction_classifies_timeout_with_no_merge_as_no_merge_observed(tmp_path, monkeypatch):
    worktree, _remote, base_sha, ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)

    def handler(argv, kwargs, original_run):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 30))

    _patch_merge_subprocess(monkeypatch, handler)
    result = execute_verified_ff_merge_transaction(
        str(worktree), ahead_sha, expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == MERGE_STATUS_TRANSPORT_ERROR_NO_MERGE
    assert result.reason_code == "merge_execution_timeout_no_merge_observed"
    assert _rev_parse(worktree, "HEAD") == base_sha


def test_transaction_classifies_timeout_with_operation_residue_as_ambiguous(tmp_path, monkeypatch):
    worktree, _remote, base_sha, ahead_sha = _make_main_and_linked_worktree(tmp_path, monkeypatch)

    def handler(argv, kwargs, original_run):
        marker = Path(_git_path(worktree, "MERGE_HEAD"))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(base_sha + chr(10))
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 30))

    _patch_merge_subprocess(monkeypatch, handler)
    result = execute_verified_ff_merge_transaction(
        str(worktree), ahead_sha, expected_worktree_realpath=str(worktree), active_issue_number=ISSUE_NUMBER
    )
    assert result.status == MERGE_STATUS_TRANSPORT_ERROR_AMBIGUOUS
    assert result.reason_code == "merge_execution_timeout_state_ambiguous"

@pytest.mark.parametrize("hook_flavor", ["claude", "codex"])
def test_local_main_and_codex_flavors_keep_root_and_destructive_denies(tmp_path, monkeypatch, hook_flavor):
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _init_repo(repo, "main")
    _commit(repo, "tracked.txt", "base")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=repo)
    ahead_sha = "a" * 40
    merge_result = evaluate("rtk git merge --ff-only " + ahead_sha, cwd=str(repo), hook_flavor=hook_flavor)
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


# P1 Blocker regression: the dedicated executor invocation shape must
# resolve to allow, and every rtk git merge shape (with or without --ff-only,
# with malformed suffixes, or wrapped in a shell) must stay at prompt --
# never allow, since only the dedicated executor performs its own
# authorization before the trusted transaction.
@pytest.mark.skipif(_CODEX_BIN is None, reason="codex CLI not available in this environment")
def test_codex_execpolicy_allows_only_the_dedicated_executor_shape():
    valid_sha = "8" * 40
    allow_argv = [
        "uv", "run", "--locked", "--no-sync", "python3",
        "scripts/agent-ops/verified_ff_merge_exec.py", "--target-sha", valid_sha,
    ]
    assert _execpolicy_decision(allow_argv) == "allow"

    prompt_cases = [
        ["rtk", "git", "merge", "--ff-only", valid_sha],
        ["rtk", "git", "merge", "--ff-only", valid_sha.upper()],
        ["rtk", "git", "merge", "--ff-only", valid_sha, "--no-edit"],
        ["rtk", "git", "merge", "feature-branch"],
        ["bash", "-c", "rtk git merge --ff-only " + valid_sha],
    ]
    for argv_tail in prompt_cases:
        assert _execpolicy_decision(argv_tail) != "allow", argv_tail
