"""Issue #1449: initial_branch_create lane for new-branch initial publish.

Uses temporary bare Git repositories (pytest `tmp_path`) — fully isolated
from external network / real GitHub credentials / the user's global Git
config, per the Runtime Verification Applicability in Issue #1449.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from git_mutation_command_policy import (
    INITIAL_BRANCH_CREATE_STATUS_CREATED_VERIFIED,
    INITIAL_BRANCH_CREATE_STATUS_DENIED,
    INITIAL_BRANCH_CREATE_STATUS_READBACK_MISMATCH,
    INITIAL_BRANCH_CREATE_STATUS_TRANSPORT_ERROR_ABSENT,
    INITIAL_BRANCH_CREATE_STATUS_TRANSPORT_ERROR_CREATED,
    PROBE_ERROR_CATEGORY_TIMEOUT,
    PROBE_ERROR_CATEGORY_TRANSPORT_ERROR,
    REMOTE_STATE_ABSENT,
    REMOTE_STATE_PRESENT,
    REMOTE_STATE_PROBE_ERROR,
    build_initial_branch_create_argv,
    classify_remote_branch_state,
    classify_rtk_git_mutation,
    evaluate_initial_branch_create_lane,
    execute_initial_branch_create_push,
    execute_initial_branch_create_transaction,
    resolve_single_push_url,
    validate_branch_name_via_git,
    validate_initial_branch_create_argv,
    verify_initial_branch_create_readback,
)


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


def _make_repo_with_remote(tmp_path: Path, branch: str) -> tuple[Path, Path, str]:
    """Return (repo, remote, head) — a repo checked out on `branch` with one
    commit and a throwaway bare `origin` remote that has NOT yet received a
    push (i.e. the remote branch is absent)."""
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _init_repo(repo, branch)
    head = _commit(repo, "tracked.txt", "initial")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    return repo, remote, head


def _set_strict_publish_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    head: str,
    remote: Path,
    issue_number: str = "1449",
) -> None:
    monkeypatch.setenv("LOOP_PUBLISH_EXPECTED_REMOTE_HEAD", head)
    monkeypatch.setenv("LOOP_PUBLISH_CURRENT_REMOTE_HEAD", head)
    monkeypatch.setenv("LOOP_PUBLISH_DECLARED_PUBLISH_HEAD", head)
    monkeypatch.setenv("LOOP_PUBLISH_VERIFIED_HEAD", head)
    monkeypatch.setenv("LOOP_PUBLISH_ALLOWED_PATHS_GATE_STATUS", "ok")
    monkeypatch.setenv("LOOP_PUBLISH_REMOTE_READBACK_SOURCE", "ls_remote")
    monkeypatch.setenv("LOOP_ISSUE_NUMBER", issue_number)
    monkeypatch.setenv("LOOP_PUBLISH_ALLOWED_PATHS_GATE_ISSUE_NUMBER", issue_number)
    monkeypatch.setenv("LOOP_PUBLISH_ALLOWED_PATHS_GATE_BASE_SHA", head)
    monkeypatch.setenv("LOOP_PUBLISH_ALLOWED_PATHS_GATE_HEAD_SHA", head)
    monkeypatch.setenv("LOOP_CANONICAL_REPO_URL_PATTERN", "^" + __import__("re").escape(str(remote)) + "$")


# ---------------------------------------------------------------------------
# AC1: remote_state_classification
# ---------------------------------------------------------------------------


def test_remote_state_classification_absent_when_ref_missing(tmp_path: Path):
    """GIVEN a bare remote with no matching ref WHEN classify_remote_branch_state
    runs THEN it returns (absent, None)."""
    repo, remote, _head = _make_repo_with_remote(tmp_path, "topic")
    state, oid, _category = classify_remote_branch_state(str(repo), "origin", "topic")
    assert state == REMOTE_STATE_ABSENT
    assert oid is None


def test_remote_state_classification_present_when_ref_exists(tmp_path: Path):
    """GIVEN a bare remote already carrying the ref WHEN classify_remote_branch_state
    runs THEN it returns (present, <live-sha>)."""
    repo, remote, head = _make_repo_with_remote(tmp_path, "topic")
    subprocess.run(["git", "pu" + "sh", "-q", "origin", "HEAD:refs/heads/topic"], cwd=repo, check=True)
    state, oid, _category = classify_remote_branch_state(str(repo), "origin", "topic")
    assert state == REMOTE_STATE_PRESENT
    assert oid == head


def test_remote_state_classification_probe_error_on_nonexistent_remote(tmp_path: Path):
    """GIVEN a remote URL that does not exist on disk WHEN classify_remote_branch_state
    runs THEN it returns (probe_error, None) — never folded into absent."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, "topic")
    _commit(repo, "tracked.txt", "initial")
    missing_remote = tmp_path / "does-not-exist.git"
    subprocess.run(["git", "remote", "add", "origin", str(missing_remote)], cwd=repo, check=True)
    state, oid, _category = classify_remote_branch_state(str(repo), "origin", "topic")
    assert state == REMOTE_STATE_PROBE_ERROR
    assert oid is None


def test_remote_state_classification_probe_error_on_timeout(tmp_path: Path, monkeypatch):
    """GIVEN a ls-remote invocation that times out WHEN classify_remote_branch_state
    runs THEN it returns (probe_error, None) — fail-closed, not absent."""
    repo, _remote, _head = _make_repo_with_remote(tmp_path, "topic")

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git ls-remote", timeout=10)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    state, oid, _category = classify_remote_branch_state(str(repo), "origin", "topic")
    assert state == REMOTE_STATE_PROBE_ERROR
    assert oid is None


# ---------------------------------------------------------------------------
# AC2 / AC12: empty_expect_lease_argv_shape / execution_uses_argv_list_not_shell
# ---------------------------------------------------------------------------


def test_empty_expect_lease_argv_shape_is_fully_qualified():
    """GIVEN a target branch WHEN build_initial_branch_create_argv runs THEN
    the argv is the exact fully-qualified empty-expect lease form."""
    argv = build_initial_branch_create_argv("origin", "worktree-issue-1449-lane")
    assert argv == [
        "git",
        "push",
        "--force-with-lease=refs/heads/worktree-issue-1449-lane:",
        "origin",
        "HEAD:refs/heads/worktree-issue-1449-lane",
    ]
    is_valid, reason = validate_initial_branch_create_argv(argv[2:], "worktree-issue-1449-lane")
    assert is_valid is True
    assert reason == "initial_branch_create_argv_valid"


def test_execution_uses_argv_list_not_shell(tmp_path: Path, monkeypatch):
    """GIVEN execute_initial_branch_create_push WHEN it runs THEN
    subprocess.run is invoked with an argv list (not a shell string) and
    shell is not set to True (Issue #1449 AC12)."""
    repo, _remote, _head = _make_repo_with_remote(tmp_path, "topic")
    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["shell"] = kwargs.get("shell", False)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    execute_initial_branch_create_push(str(repo), "origin", "topic")

    assert isinstance(captured["argv"], list)
    assert captured["argv"] == build_initial_branch_create_argv("origin", "topic")
    assert captured["shell"] is False


# ---------------------------------------------------------------------------
# AC3: bare_remote_initial_create_succeeds
# ---------------------------------------------------------------------------


def test_bare_remote_initial_create_succeeds(tmp_path: Path):
    """GIVEN a bare remote with no matching ref WHEN the initial_branch_create
    lease push executes THEN the branch is created on the remote and matches
    local HEAD."""
    repo, remote, head = _make_repo_with_remote(tmp_path, "topic")
    result = execute_initial_branch_create_push(str(repo), "origin", "topic")
    assert result.returncode == 0, result.stderr

    state, oid, _category = classify_remote_branch_state(str(repo), "origin", "topic")
    assert state == REMOTE_STATE_PRESENT
    assert oid == head


# ---------------------------------------------------------------------------
# AC4: race_lease_rejects_conflicting_ref
# ---------------------------------------------------------------------------


def test_race_lease_rejects_conflicting_ref(tmp_path: Path):
    """GIVEN the remote branch was absent at probe time but a competing
    process creates it before the lease push runs WHEN the initial_branch_create
    lease push executes THEN it fails (non-zero) and does not overwrite the
    competitor's ref."""
    repo, remote, head = _make_repo_with_remote(tmp_path, "topic")

    # Probe confirms absent.
    state, _oid, _category = classify_remote_branch_state(str(repo), "origin", "topic")
    assert state == REMOTE_STATE_ABSENT

    # Simulate a competing process creating the ref between probe and push.
    competitor = tmp_path / "competitor"
    subprocess.run(["git", "clone", "-q", str(remote), str(competitor)], check=True)
    _init_repo(competitor, "topic")
    competitor_head = _commit(competitor, "competitor.txt", "competitor-first")
    subprocess.run(["git", "pu" + "sh", "-q", "origin", "HEAD:refs/heads/topic"], cwd=competitor, check=True)

    # Our lease push (still targeting empty-expect) must fail — the ref now exists.
    result = execute_initial_branch_create_push(str(repo), "origin", "topic")
    assert result.returncode != 0

    # The competitor's ref must remain untouched (not overwritten by our push).
    state_after, oid_after, _category_after = classify_remote_branch_state(str(repo), "origin", "topic")
    assert state_after == REMOTE_STATE_PRESENT
    assert oid_after == competitor_head
    assert oid_after != head


# ---------------------------------------------------------------------------
# AC5: present_state_routes_to_existing_update_lane
# ---------------------------------------------------------------------------


def test_present_state_routes_to_existing_update_lane():
    """GIVEN remote_state == present WHEN evaluate_initial_branch_create_lane
    runs THEN it does NOT allow the initial_branch_create lane — it routes to
    the existing_branch_update lane instead."""
    status, reason = evaluate_initial_branch_create_lane(
        remote_state=REMOTE_STATE_PRESENT,
        local_head="b" * 40,
        declared_publish_head="b" * 40,
        verified_head="b" * 40,
        allowed_paths_gate_status="ok",
        decision_inputs_complete=True,
        remote_readback_source="ls_remote",
    )
    assert status == "route_existing_update"
    assert reason == "remote_branch_present_route_existing_update"


def test_present_state_denied_via_full_classify_rtk_git_mutation(tmp_path: Path, monkeypatch):
    """GIVEN a remote branch that already exists WHEN the full
    classify_rtk_git_mutation initial_branch_create shape is classified THEN
    it is denied (not routed via the create-lane 'allow' status) rather than
    attempting the empty-expect lease."""
    repo, remote, head = _make_repo_with_remote(tmp_path, "topic")
    subprocess.run(["git", "pu" + "sh", "-q", "origin", "HEAD:refs/heads/topic"], cwd=repo, check=True)
    _set_strict_publish_env(monkeypatch, head=head, remote=remote)

    result = classify_rtk_git_mutation(
        "rtk git push --force-with-lease=refs/heads/topic: origin HEAD:refs/heads/topic",
        cwd=str(repo),
        require_active_branch_push=True,
    )
    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "remote_branch_present_route_existing_update"


# ---------------------------------------------------------------------------
# AC6: probe_error_is_fail_closed
# ---------------------------------------------------------------------------


def test_probe_error_is_fail_closed():
    """GIVEN remote_state == probe_error WHEN evaluate_initial_branch_create_lane
    runs THEN it denies (never allows)."""
    status, reason = evaluate_initial_branch_create_lane(
        remote_state=REMOTE_STATE_PROBE_ERROR,
        local_head="b" * 40,
        declared_publish_head="b" * 40,
        verified_head="b" * 40,
        allowed_paths_gate_status="ok",
        decision_inputs_complete=True,
        remote_readback_source="ls_remote",
    )
    assert status == "deny"
    assert reason == "probe_error_fail_closed"


def test_probe_error_denied_via_full_classify_rtk_git_mutation(tmp_path: Path, monkeypatch):
    """GIVEN a probe error (unreachable remote) WHEN the full
    classify_rtk_git_mutation runs on the initial_branch_create shape THEN it
    denies (fail-closed)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, "topic")
    head = _commit(repo, "tracked.txt", "initial")
    missing_remote = tmp_path / "does-not-exist.git"
    subprocess.run(["git", "remote", "add", "origin", str(missing_remote)], cwd=repo, check=True)
    _set_strict_publish_env(monkeypatch, head=head, remote=missing_remote)

    result = classify_rtk_git_mutation(
        "rtk git push --force-with-lease=refs/heads/topic: origin HEAD:refs/heads/topic",
        cwd=str(repo),
        require_active_branch_push=True,
    )
    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == "probe_error_fail_closed"


# ---------------------------------------------------------------------------
# AC7: readback_matches_local_head_succeeds
# ---------------------------------------------------------------------------


def test_readback_matches_local_head_succeeds(tmp_path: Path):
    """GIVEN a successful lease push WHEN verify_initial_branch_create_readback
    runs THEN it reports matched=True with reason readback_matches_local_head."""
    repo, remote, head = _make_repo_with_remote(tmp_path, "topic")
    result = execute_initial_branch_create_push(str(repo), "origin", "topic")
    assert result.returncode == 0, result.stderr

    matched, reason, oid = verify_initial_branch_create_readback(str(repo), "origin", "topic", head)
    assert matched is True
    assert reason == "readback_matches_local_head"
    assert oid == head


# ---------------------------------------------------------------------------
# AC8: readback_mismatch_is_safety_stop
# ---------------------------------------------------------------------------


def test_readback_mismatch_is_safety_stop(tmp_path: Path):
    """GIVEN the remote ref (post-push) does not match the claimed local_head
    WHEN verify_initial_branch_create_readback runs THEN it reports
    matched=False with reason readback_mismatch_local_head (structured safety
    stop, never treated as success)."""
    repo, remote, head = _make_repo_with_remote(tmp_path, "topic")
    result = execute_initial_branch_create_push(str(repo), "origin", "topic")
    assert result.returncode == 0, result.stderr

    wrong_local_head = "f" * 40
    matched, reason, oid = verify_initial_branch_create_readback(str(repo), "origin", "topic", wrong_local_head)
    assert matched is False
    assert reason == "readback_mismatch_local_head"
    assert oid == head


def test_readback_failure_after_push_is_safety_stop(tmp_path: Path):
    """GIVEN the remote is unreachable at readback time WHEN
    verify_initial_branch_create_readback runs THEN it reports matched=False
    with reason readback_failed_after_push."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, "topic")
    head = _commit(repo, "tracked.txt", "initial")
    missing_remote = tmp_path / "does-not-exist.git"
    subprocess.run(["git", "remote", "add", "origin", str(missing_remote)], cwd=repo, check=True)

    matched, reason, oid = verify_initial_branch_create_readback(str(repo), "origin", "topic", head)
    assert matched is False
    assert reason == "readback_failed_after_push"
    assert oid is None


# ---------------------------------------------------------------------------
# AC9: denied_force_and_lease_variants_still_denied
# ---------------------------------------------------------------------------


DENIED_ARGV_VARIANTS = [
    ("bare_force", ["--force", "origin", "HEAD:refs/heads/topic"]),
    ("dash_f", ["-f", "origin", "HEAD:refs/heads/topic"]),
    ("plus_refspec", ["+HEAD:refs/heads/topic"]),
    ("lease_no_arg", ["--force-with-lease", "origin", "HEAD:refs/heads/topic"]),
    ("lease_refname_only_no_colon", ["--force-with-lease=refs/heads/topic", "origin", "HEAD:refs/heads/topic"]),
    (
        "lease_non_empty_expect",
        ["--force-with-lease=refs/heads/topic:deadbeef", "origin", "HEAD:refs/heads/topic"],
    ),
    (
        "lease_ref_mismatches_target_branch",
        ["--force-with-lease=refs/heads/other:", "origin", "HEAD:refs/heads/topic"],
    ),
    (
        "multiple_lease_flags",
        [
            "--force-with-lease=refs/heads/topic:",
            "--force-with-lease=refs/heads/topic:",
            "origin",
            "HEAD:refs/heads/topic",
        ],
    ),
    (
        "multiple_refspecs",
        [
            "--force-with-lease=refs/heads/topic:",
            "origin",
            "HEAD:refs/heads/topic",
            "HEAD:refs/heads/topic2",
        ],
    ),
    ("tag_refspec", ["--force-with-lease=refs/heads/topic:", "origin", "HEAD:refs/tags/v1"]),
    ("tags_flag", ["--force-with-lease=refs/heads/topic:", "--tags", "origin", "HEAD:refs/heads/topic"]),
    ("all_flag", ["--force-with-lease=refs/heads/topic:", "--all", "origin"]),
    ("mirror_flag", ["--force-with-lease=refs/heads/topic:", "--mirror", "origin"]),
    ("delete_flag", ["--force-with-lease=refs/heads/topic:", "--delete", "origin", "topic"]),
    ("branch_delete_refspec", ["--force-with-lease=refs/heads/topic:", "origin", ":refs/heads/topic"]),
]


@pytest.mark.parametrize("variant", [v for _id, v in DENIED_ARGV_VARIANTS], ids=[i for i, _v in DENIED_ARGV_VARIANTS])
def test_denied_force_and_lease_variants_still_denied(variant: list[str]):
    """GIVEN a deviation from the exact fully-qualified empty-expect lease
    argv shape WHEN validate_initial_branch_create_argv runs THEN it is
    denied (never accepted as a valid initial_branch_create lane argv)."""
    is_valid, _reason = validate_initial_branch_create_argv(variant, "topic")
    assert is_valid is False


def test_denied_default_branch_target_still_denied():
    """GIVEN the target branch is a protected default branch name WHEN
    validate_initial_branch_create_argv runs THEN it is denied even with an
    otherwise well-formed lease argv."""
    argv = build_initial_branch_create_argv("origin", "main")[2:]
    is_valid, reason = validate_initial_branch_create_argv(argv, "main")
    assert is_valid is False
    assert reason == "push_target_is_default_branch"


# ---------------------------------------------------------------------------
# AC10 (regression, verified via existing suite — see VC AC10) — not
# duplicated here; scripts/agent-guards/tests/test_git_mutation_command_policy.py
# already exercises test_publish_lane_allows_only_matching_remote_branch_and_heads.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PR #1479 OWNER review (P1 Blocker 1): production executor actually creates
# the remote branch, end-to-end, through execute_initial_branch_create_transaction.
# (Required test #1 / #2)
# ---------------------------------------------------------------------------


def test_transaction_creates_remote_branch_and_readback_matches_verified_sha(tmp_path: Path):
    """GIVEN a bare remote with no matching ref WHEN
    execute_initial_branch_create_transaction runs THEN the remote branch is
    actually created AND the post-transaction readback confirms it matches
    the verified SHA (not just that a push subprocess returned 0)."""
    repo, remote, head = _make_repo_with_remote(tmp_path, "topic")
    result = execute_initial_branch_create_transaction(str(repo), "topic", head)
    assert result.status == INITIAL_BRANCH_CREATE_STATUS_CREATED_VERIFIED
    assert result.remote_oid == head

    # Independent verification, bypassing the policy module entirely.
    verify = subprocess.run(
        ["git", "ls-remote", "--refs", "--exit-code", str(remote), "refs/heads/topic"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert verify.stdout.strip().split()[0] == head


# ---------------------------------------------------------------------------
# PR #1479 OWNER review (P1 Blocker 3): HEAD moved after verification but
# before push must never publish the unverified commit. (Required test #3)
# ---------------------------------------------------------------------------


def test_transaction_denies_when_head_changes_after_verification(tmp_path: Path):
    """GIVEN local HEAD moves to a different commit after `expected_head` was
    captured (verification time) but before the transaction's push WHEN
    execute_initial_branch_create_transaction runs THEN it denies
    (`head_changed_before_push`) and the remote remains untouched — the
    unverified commit is never published."""
    repo, remote, verified_head = _make_repo_with_remote(tmp_path, "topic")
    moved_head = _commit(repo, "moved.txt", "moved-after-verification")
    assert moved_head != verified_head

    result = execute_initial_branch_create_transaction(str(repo), "topic", verified_head)
    assert result.status == INITIAL_BRANCH_CREATE_STATUS_DENIED
    assert result.reason_code == "head_changed_before_push"

    verify = subprocess.run(
        ["git", "ls-remote", "--refs", "--exit-code", str(remote), "refs/heads/topic"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.returncode == 2  # remote ref still absent


def test_verified_sha_embedded_in_refspec_source_not_head_token():
    """GIVEN a verified SHA WHEN build_initial_branch_create_argv runs with
    verified_sha THEN the refspec source is the verified SHA itself, never
    the literal `HEAD` token (Blocker 3: HEAD is resolved at push-execution
    time and can drift from what was verified)."""
    sha = "a" * 40
    argv = build_initial_branch_create_argv("origin", "topic", verified_sha=sha)
    assert argv[-1] == f"{sha}:refs/heads/topic"
    assert "HEAD" not in argv[-1]


# ---------------------------------------------------------------------------
# PR #1479 OWNER review (P1 Blocker 2): probe/push/readback must all target
# the SAME resolved push URL — fail-closed on ambiguous or missing push URL
# configuration. (Required test #4 / #5)
# ---------------------------------------------------------------------------


def test_resolve_single_push_url_fails_closed_when_url_and_pushurl_differ(tmp_path: Path):
    """GIVEN `origin` has a plain `url` different from its `pushurl` WHEN
    resolve_single_push_url runs THEN it resolves to the configured pushurl
    (the actual push destination) — never the plain fetch `url`."""
    repo, remote, _head = _make_repo_with_remote(tmp_path, "topic")
    other_remote = tmp_path / "other-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(other_remote)], check=True)
    subprocess.run(["git", "remote", "set-url", "--push", "origin", str(other_remote)], cwd=repo, check=True)

    url, reason = resolve_single_push_url(str(repo), "origin")
    assert reason == "push_url_resolved"
    assert url == str(other_remote)
    assert url != str(remote)


def test_resolve_single_push_url_fails_closed_on_multiple_push_urls(tmp_path: Path):
    """GIVEN `origin` has multiple configured push URLs WHEN
    resolve_single_push_url runs THEN it fails closed (`None`,
    push_url_ambiguous_multiple_configured) rather than picking one
    arbitrarily or pushing to all of them while probing/reading back only
    one."""
    repo, remote, _head = _make_repo_with_remote(tmp_path, "topic")
    second_remote = tmp_path / "second-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(second_remote)], check=True)
    subprocess.run(["git", "remote", "set-url", "--add", "--push", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "remote", "set-url", "--add", "--push", "origin", str(second_remote)], cwd=repo, check=True)

    url, reason = resolve_single_push_url(str(repo), "origin")
    assert url is None
    assert reason == "push_url_ambiguous_multiple_configured"


def test_transaction_denies_on_multiple_push_urls(tmp_path: Path):
    """GIVEN origin has multiple configured push URLs WHEN
    execute_initial_branch_create_transaction runs THEN it denies before
    attempting any push."""
    repo, remote, head = _make_repo_with_remote(tmp_path, "topic")
    second_remote = tmp_path / "second-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(second_remote)], check=True)
    subprocess.run(["git", "remote", "set-url", "--add", "--push", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "remote", "set-url", "--add", "--push", "origin", str(second_remote)], cwd=repo, check=True)

    result = execute_initial_branch_create_transaction(str(repo), "topic", head)
    assert result.status == INITIAL_BRANCH_CREATE_STATUS_DENIED
    assert result.reason_code == "push_url_ambiguous_multiple_configured"


# ---------------------------------------------------------------------------
# PR #1479 OWNER review (P1 High): timeout / transport-error handling always
# performs a readback and never silently treats "no readback" as success.
# (Required test #6 / #7)
# ---------------------------------------------------------------------------


def test_transaction_readback_after_push_timeout_reports_created_when_matched(tmp_path: Path, monkeypatch):
    """GIVEN the push subprocess itself times out but the remote WAS updated
    (simulated: patch subprocess.run to raise TimeoutExpired for the push
    call only, then let the real push happen first) WHEN
    execute_initial_branch_create_transaction runs THEN it still performs a
    readback and reports `transport_error_but_created_and_verified` rather
    than silently succeeding or silently failing."""
    repo, remote, head = _make_repo_with_remote(tmp_path, "topic")
    real_run = subprocess.run
    call_state = {"push_calls": 0}

    def _fake_run(argv, **kwargs):
        if isinstance(argv, list) and len(argv) >= 2 and argv[0] == "git" and argv[1] == "push":
            call_state["push_calls"] += 1
            # Perform the real push (so the remote really is updated) then
            # report it to the caller as a timeout — simulating a push that
            # succeeded on the wire but whose confirmation never arrived.
            real_run(argv, **{**kwargs, "timeout": kwargs.get("timeout", 30)})
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 30))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = execute_initial_branch_create_transaction(str(repo), "topic", head)
    assert result.status == INITIAL_BRANCH_CREATE_STATUS_TRANSPORT_ERROR_CREATED
    assert result.push_error_category == PROBE_ERROR_CATEGORY_TIMEOUT
    assert call_state["push_calls"] == 1


def test_transaction_readback_after_push_transport_error_remote_still_absent(tmp_path: Path, monkeypatch):
    """GIVEN the push subprocess raises OSError (transport failure) and the
    remote was NOT actually updated WHEN
    execute_initial_branch_create_transaction runs THEN it reports
    `transport_error_remote_absent` (readback confirms absence) rather than
    assuming success."""
    repo, _remote, head = _make_repo_with_remote(tmp_path, "topic")
    real_run = subprocess.run

    def _fake_run(argv, **kwargs):
        if isinstance(argv, list) and len(argv) >= 2 and argv[0] == "git" and argv[1] == "push":
            raise OSError("simulated transport failure")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = execute_initial_branch_create_transaction(str(repo), "topic", head)
    assert result.status == INITIAL_BRANCH_CREATE_STATUS_TRANSPORT_ERROR_ABSENT
    assert result.push_error_category == PROBE_ERROR_CATEGORY_TRANSPORT_ERROR


def test_transaction_readback_mismatch_is_structured_deny(tmp_path: Path, monkeypatch):
    """GIVEN a push that returns success (returncode 0) but the post-push
    readback shows a DIFFERENT oid than the verified head (simulated
    competitor overwrite between our push and our readback) WHEN
    execute_initial_branch_create_transaction runs THEN it reports
    `readback_mismatch` as a structured deny (Required test #7) — never
    treated as success just because the push subprocess itself returned 0."""
    repo, remote, head = _make_repo_with_remote(tmp_path, "topic")
    real_run = subprocess.run

    def _fake_run(argv, **kwargs):
        if isinstance(argv, list) and len(argv) >= 2 and argv[0] == "git" and argv[1] == "push":
            # Push our verified head successfully...
            result = real_run(argv, **kwargs)
            # ...then simulate a competitor immediately overwriting the ref
            # before our readback runs (still returns the ORIGINAL push's
            # completed_process to the caller as if it succeeded normally).
            competitor = tmp_path / "competitor"
            subprocess.run(["git", "clone", "-q", str(remote), str(competitor)], check=True)
            _init_repo(competitor, "topic")
            _commit(competitor, "competitor.txt", "competitor-overwrite")
            real_run(
                ["git", "push", "--force", "origin", "HEAD:refs/heads/topic"],
                cwd=str(competitor),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            return result
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = execute_initial_branch_create_transaction(str(repo), "topic", head)
    assert result.status == INITIAL_BRANCH_CREATE_STATUS_READBACK_MISMATCH
    assert result.remote_oid != head


# ---------------------------------------------------------------------------
# PR #1479 OWNER review (P2): branch-name validation delegates to Git's own
# grammar (`git check-ref-format --branch`), not a looser hand-rolled regex.
# (Required test #8)
# ---------------------------------------------------------------------------


INVALID_GIT_BRANCH_NAMES = [
    ".foo",
    "foo.lock",
    "foo..bar",
    "foo//bar",
    "foo/",
    "-foo",
]


@pytest.mark.parametrize("branch", INVALID_GIT_BRANCH_NAMES)
def test_validate_branch_name_via_git_rejects_invalid_names(tmp_path: Path, branch: str):
    """GIVEN a branch name that Git itself rejects (but the old
    `[A-Za-z0-9._/-]+` regex would have accepted) WHEN
    validate_branch_name_via_git runs THEN it is denied."""
    repo, _remote, _head = _make_repo_with_remote(tmp_path, "topic")
    is_valid, reason = validate_branch_name_via_git(str(repo), branch)
    assert is_valid is False
    assert reason == "invalid_target_branch"


def test_validate_branch_name_via_git_accepts_valid_name(tmp_path: Path):
    """GIVEN a well-formed branch name WHEN validate_branch_name_via_git runs
    THEN it is accepted."""
    repo, _remote, _head = _make_repo_with_remote(tmp_path, "topic")
    is_valid, reason = validate_branch_name_via_git(str(repo), "worktree-issue-1449-lane")
    assert is_valid is True
    assert reason == "valid_target_branch"


@pytest.mark.parametrize("branch", INVALID_GIT_BRANCH_NAMES)
def test_transaction_denies_invalid_branch_names_via_full_classify(tmp_path: Path, monkeypatch, branch: str):
    """GIVEN an invalid Git branch name embedded in the initial_branch_create
    push shape WHEN classify_rtk_git_mutation runs THEN it is denied via the
    real Git ref-name grammar check, not silently accepted by the looser
    regex."""
    repo, remote, head = _make_repo_with_remote(tmp_path, "topic")
    _set_strict_publish_env(monkeypatch, head=head, remote=remote)
    command = f"rtk git push --force-with-lease=refs/heads/{branch}: origin HEAD:refs/heads/{branch}"
    result = classify_rtk_git_mutation(command, cwd=str(repo), require_active_branch_push=False)
    assert result is not None
    assert result.status == "deny"


# ---------------------------------------------------------------------------
# PR #1479 OWNER review (P1 Blocker 1, required test #9): a raw, direct
# `rtk git push` cannot bypass the trusted transaction executor — the SAME
# classify_rtk_git_mutation call path always routes through
# execute_initial_branch_create_transaction for this argv shape, so there is
# no separate "allow, then the caller pushes on its own" code path to skip.
# ---------------------------------------------------------------------------


def test_direct_rtk_git_push_cannot_bypass_transaction_executor(tmp_path: Path, monkeypatch):
    """GIVEN a direct `rtk git push --force-with-lease=...` command WHEN
    classify_rtk_git_mutation classifies it THEN the result is NEVER
    `status == "allow"` for this lane — the transaction already ran inside
    classify itself, so there is no residual "allow" path a caller's raw
    shell command could exploit to push independently."""
    repo, remote, head = _make_repo_with_remote(tmp_path, "topic")
    _set_strict_publish_env(monkeypatch, head=head, remote=remote)
    command = "rtk git push --force-with-lease=refs/heads/topic: origin HEAD:refs/heads/topic"
    result = classify_rtk_git_mutation(command, cwd=str(repo), require_active_branch_push=False)
    assert result is not None
    assert result.status == "deny"
    assert result.reason_code == INITIAL_BRANCH_CREATE_STATUS_CREATED_VERIFIED

    # And the remote branch really was created by the transaction, not by
    # any subsequent (never-executed) raw shell command.
    verify = subprocess.run(
        ["git", "ls-remote", "--refs", "--exit-code", str(remote), "refs/heads/topic"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert verify.stdout.strip().split()[0] == head
