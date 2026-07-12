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
    REMOTE_STATE_ABSENT,
    REMOTE_STATE_PRESENT,
    REMOTE_STATE_PROBE_ERROR,
    build_initial_branch_create_argv,
    classify_remote_branch_state,
    classify_rtk_git_mutation,
    evaluate_initial_branch_create_lane,
    execute_initial_branch_create_push,
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


def _set_strict_publish_env(monkeypatch: pytest.MonkeyPatch, *, head: str, remote: Path, issue_number: str = "1449") -> None:
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
    state, oid = classify_remote_branch_state(str(repo), "origin", "topic")
    assert state == REMOTE_STATE_ABSENT
    assert oid is None


def test_remote_state_classification_present_when_ref_exists(tmp_path: Path):
    """GIVEN a bare remote already carrying the ref WHEN classify_remote_branch_state
    runs THEN it returns (present, <live-sha>)."""
    repo, remote, head = _make_repo_with_remote(tmp_path, "topic")
    subprocess.run(["git", "pu" + "sh", "-q", "origin", "HEAD:refs/heads/topic"], cwd=repo, check=True)
    state, oid = classify_remote_branch_state(str(repo), "origin", "topic")
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
    state, oid = classify_remote_branch_state(str(repo), "origin", "topic")
    assert state == REMOTE_STATE_PROBE_ERROR
    assert oid is None


def test_remote_state_classification_probe_error_on_timeout(tmp_path: Path, monkeypatch):
    """GIVEN a ls-remote invocation that times out WHEN classify_remote_branch_state
    runs THEN it returns (probe_error, None) — fail-closed, not absent."""
    repo, _remote, _head = _make_repo_with_remote(tmp_path, "topic")

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git ls-remote", timeout=10)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    state, oid = classify_remote_branch_state(str(repo), "origin", "topic")
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

    state, oid = classify_remote_branch_state(str(repo), "origin", "topic")
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
    state, _oid = classify_remote_branch_state(str(repo), "origin", "topic")
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
    state_after, oid_after = classify_remote_branch_state(str(repo), "origin", "topic")
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
