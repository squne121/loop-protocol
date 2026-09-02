"""Behavioral regression coverage for the Issue #2197 control-plane
remote-default-ref observe/fetch/pin/verify protocol and the fixed dedicated
worktree crash/rerun recovery it hands off to.

Covers AC1 (canonical HTTPS literal remote authority), AC2 (focused
`ls-remote --symref` parser), AC3 (S1/fetch/F/S2 state machine), AC4
(builder-owned CAS cleanup on every post-fetch terminal exception path), AC5
(fixed dedicated worktree crash/rerun recovery states), and AC7 (cleanup
failure fails closed as `control_plane_unavailable`, with no retention).
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENT_GUARDS_DIR = REPO_ROOT / "scripts" / "agent-guards"
AGENT_OPS_DIR = REPO_ROOT / "scripts" / "agent-ops"
BOOTSTRAP_SCRIPT = AGENT_OPS_DIR / "worktree_bootstrap_exec.py"

if str(AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_GUARDS_DIR))

# Loaded the same way every other focused test in this repo loads it (bare,
# unaliased `sys.modules["skill_runtime_exec"]`) so that
# `worktree_bootstrap_exec`'s own `from skill_runtime_exec import ...`
# resolves to this exact same cached module instance -- otherwise exception
# classes such as `ControlPlaneUnavailable` would not compare equal across
# the two.
import skill_runtime_exec as exec_mod  # noqa: E402
from skill_runtime_command_policy import (  # noqa: E402
    validate_allowed_remote_ref,
    validate_literal_remote_url,
)


def _load_bootstrap_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("worktree_bootstrap_exec_2197", BOOTSTRAP_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BOOTSTRAP = _load_bootstrap_module()


@pytest.fixture(autouse=True)
def _reset_git_cache():
    exec_mod._reset_git_subprocess_executable_cache_for_tests()
    yield
    exec_mod._reset_git_subprocess_executable_cache_for_tests()


def _git_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "fixture-home"
    xdg = home / "xdg"
    xdg.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(xdg),
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
        GIT_TERMINAL_PROMPT="0",
    )
    return env


def _init_remote_fixture(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Return (local_clone, origin_bare_dir, origin_url, initial_head_oid)."""
    source = tmp_path / "source"
    origin = tmp_path / "origin.git"
    local = tmp_path / "local"
    env = _git_env(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True, env=env)
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=source, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=source, check=True, env=env)
    oid = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, check=True, text=True, capture_output=True
    ).stdout.strip()
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, env=env)
    subprocess.run(["git", "remote", "add", "origin", origin.as_uri()], cwd=source, check=True, env=env)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=source, check=True, env=env)
    subprocess.run(["git", "--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"], check=True, env=env)
    subprocess.run(["git", "clone", "-q", origin.as_uri(), str(local)], check=True, env=env)
    return local, origin, origin.as_uri(), oid


def _push_new_commit(origin: Path, tmp_path: Path, marker: str) -> str:
    """Advance origin's `main` with one new commit; return the new HEAD oid."""
    env = _git_env(tmp_path)
    pusher = tmp_path / f"pusher-{marker}"
    subprocess.run(["git", "clone", "-q", origin.as_uri(), str(pusher)], check=True, env=env)
    (pusher / f"{marker}.txt").write_text("advance\n", encoding="utf-8")
    subprocess.run(["git", "add", f"{marker}.txt"], cwd=pusher, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", f"advance {marker}"], cwd=pusher, check=True, env=env)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=pusher, check=True, env=env)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=pusher, check=True, text=True, capture_output=True
    ).stdout.strip()


def _retarget_origin_head_to_new_branch(origin: Path, tmp_path: Path, branch: str) -> None:
    env = _git_env(tmp_path)
    pusher = tmp_path / f"retarget-{branch}"
    subprocess.run(["git", "clone", "-q", origin.as_uri(), str(pusher)], check=True, env=env)
    subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=pusher, check=True, env=env)
    subprocess.run(["git", "push", "-q", "origin", branch], cwd=pusher, check=True, env=env)
    subprocess.run(
        ["git", "--git-dir", str(origin), "symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True, env=env
    )


def _deadline() -> exec_mod.GitProtocolDeadline:
    return exec_mod.GitProtocolDeadline.start(20, cleanup_reserve_seconds=1)


def _remote_ref_exists(origin: Path, ref: str) -> bool:
    result = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
    )
    return result.returncode == 0


def _local_private_ref_exists(local: Path, private_ref: exec_mod.ControlPlanePrivateRef) -> bool:
    result = subprocess.run(
        ["git", "-C", str(local), "rev-parse", "--verify", "--quiet", private_ref.value],
        capture_output=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# AC1: canonical HTTPS literal remote authority
# ---------------------------------------------------------------------------


def test_given_trusted_repo_slug_when_canonical_remote_url_read_then_it_is_the_fixed_https_literal():
    assert exec_mod.CONTROL_PLANE_CANONICAL_REMOTE_URL == "https://github.com/squne121/loop-protocol.git"
    validate_literal_remote_url(exec_mod.CONTROL_PLANE_CANONICAL_REMOTE_URL)


def test_given_session_entry_point_when_signature_inspected_then_no_caller_remote_url_override_exists():
    parameters = inspect.signature(BOOTSTRAP.run_control_plane_preflight_session).parameters
    assert "remote_url" not in parameters
    assert "argv" not in parameters


def test_given_insteadof_rewrite_when_binding_starts_then_fails_closed_before_any_symref_observe(tmp_path):
    local, _origin, url, _oid = _init_remote_fixture(tmp_path)
    subprocess.run(["git", "config", "--local", "url.https://evil.example/.insteadOf", url], cwd=local, check=True)
    remote = validate_literal_remote_url(url)
    with pytest.raises(RuntimeError, match="effective_remote_url_mismatch"):
        exec_mod.run_control_plane_remote_default_ref_binding(
            remote,
            cwd=str(local),
            project_root=str(local),
            scratch_root=str(tmp_path / "scratch"),
            deadline=_deadline(),
        )


# ---------------------------------------------------------------------------
# AC2: focused `ls-remote --exit-code --symref` parser
# ---------------------------------------------------------------------------


def test_given_well_formed_symref_output_when_parsed_then_ref_and_oid_returned():
    oid = "a" * 40
    stdout = f"ref: refs/heads/main\tHEAD\n{oid}\tHEAD\n"
    assert exec_mod.parse_control_plane_default_ref_symref(stdout) == ("refs/heads/main", oid)


@pytest.mark.parametrize(
    "stdout",
    [
        "",  # unborn/no matching HEAD at all
        ("a" * 40) + "\tHEAD\n",  # direct (non-symbolic) HEAD: oid line only
        "ref: refs/heads/main\tHEAD\n",  # symref line only, no oid line
        "ref: refs/heads/main\tHEAD\n" + "ref: refs/heads/other\tHEAD\n" + ("a" * 40) + "\tHEAD\n",  # duplicate ref
        "ref: refs/heads/main\tHEAD\n" + ("a" * 40) + "\tHEAD\n" + ("b" * 40) + "\tHEAD\n",  # duplicate oid
        "ref: refs/heads/main\tHEAD\n" + ("a" * 40) + "\trefs/heads/main\n",  # not HEAD on the right
        "garbage-line-without-a-tab\n",  # malformed
        "\tHEAD\n" + ("a" * 40) + "\tHEAD\n",  # empty left side
    ],
)
def test_given_malformed_or_ambiguous_symref_shapes_when_parsed_then_value_error(stdout):
    with pytest.raises(ValueError):
        exec_mod.parse_control_plane_default_ref_symref(stdout)


def test_given_parsed_target_outside_refs_heads_when_semantically_validated_then_rejected():
    stdout = "ref: refs/tags/v1\tHEAD\n" + ("a" * 40) + "\tHEAD\n"
    raw_ref, _raw_oid = exec_mod.parse_control_plane_default_ref_symref(stdout)
    with pytest.raises(ValueError, match="remote_ref_not_allowed"):
        validate_allowed_remote_ref(raw_ref)


def test_given_abbreviated_oid_when_semantically_validated_then_rejected():
    stdout = "ref: refs/heads/main\tHEAD\n" + ("a" * 7) + "\tHEAD\n"
    _raw_ref, raw_oid = exec_mod.parse_control_plane_default_ref_symref(stdout)
    with pytest.raises(ValueError, match="repository_object_id_invalid"):
        exec_mod.validate_repository_object_id(raw_oid, exec_mod.validate_repository_object_format("sha1"))


# ---------------------------------------------------------------------------
# AC3/AC4: S1/fetch/F/S2 state machine + refetch prohibition + CAS cleanup
# ---------------------------------------------------------------------------


def test_given_stable_remote_when_bound_then_f_equals_oid1_accepts_on_first_observe_and_ref_survives(tmp_path):
    local, _origin, url, oid1 = _init_remote_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    remote = validate_literal_remote_url(url)
    private_ref, accepted_oid, object_format = exec_mod.run_control_plane_remote_default_ref_binding(
        remote, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=_deadline()
    )
    assert accepted_oid.value == oid1
    assert object_format.value == "sha1"
    # Success does not self-cleanup -- the private ref remains for the
    # caller (the fixed-worktree handoff / session orchestrator) to consume
    # and clean up itself.
    assert _local_private_ref_exists(local, private_ref)


def test_given_remote_advances_between_s1_and_fetch_when_f_differs_then_bounded_s2_corroborates_and_accepts(
    tmp_path, monkeypatch
):
    local, origin, url, oid1 = _init_remote_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    remote = validate_literal_remote_url(url)
    real_fetch = exec_mod.run_control_plane_git_fetch_default_ref
    fetch_calls = {"count": 0}
    advanced_oid_holder: dict[str, str] = {}

    def racy_fetch(remote_url, remote_ref, **kwargs):
        fetch_calls["count"] += 1
        advanced_oid_holder["oid"] = _push_new_commit(origin, tmp_path, "race")
        return real_fetch(remote_url, remote_ref, **kwargs)

    monkeypatch.setattr(exec_mod, "run_control_plane_git_fetch_default_ref", racy_fetch)

    private_ref, accepted_oid, _fmt = exec_mod.run_control_plane_remote_default_ref_binding(
        remote, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=_deadline()
    )
    assert fetch_calls["count"] == 1  # never refetches
    assert accepted_oid.value != oid1
    assert accepted_oid.value == advanced_oid_holder["oid"]
    assert _local_private_ref_exists(local, private_ref)


def test_given_symbolic_target_changes_between_fetch_and_s2_when_bound_then_rejected_and_private_ref_cleaned_up(
    tmp_path, monkeypatch
):
    local, origin, url, _oid1 = _init_remote_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    remote = validate_literal_remote_url(url)
    real_fetch = exec_mod.run_control_plane_git_fetch_default_ref
    fetch_calls = {"count": 0}
    captured_ref: dict[str, object] = {}

    def racy_fetch(remote_url, remote_ref, **kwargs):
        fetch_calls["count"] += 1
        _push_new_commit(origin, tmp_path, "before-retarget")
        _retarget_origin_head_to_new_branch(origin, tmp_path, "other")
        private_ref = real_fetch(remote_url, remote_ref, **kwargs)
        captured_ref["value"] = private_ref
        return private_ref

    monkeypatch.setattr(exec_mod, "run_control_plane_git_fetch_default_ref", racy_fetch)

    with pytest.raises(exec_mod.ControlPlaneUnavailable, match="remote_default_ref_state_machine_rejected"):
        exec_mod.run_control_plane_remote_default_ref_binding(
            remote, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=_deadline()
        )
    assert fetch_calls["count"] == 1
    private_ref = captured_ref["value"]
    assert not _local_private_ref_exists(local, private_ref)


def test_given_remote_keeps_advancing_past_fetch_when_s2_disagrees_with_f_then_rejected_and_cleaned_up(
    tmp_path, monkeypatch
):
    local, origin, url, _oid1 = _init_remote_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    remote = validate_literal_remote_url(url)
    real_fetch = exec_mod.run_control_plane_git_fetch_default_ref
    real_observe = exec_mod.run_control_plane_git_observe_default_ref
    observe_calls = {"count": 0}
    captured_ref: dict[str, object] = {}

    def racy_fetch(remote_url, remote_ref, **kwargs):
        _push_new_commit(origin, tmp_path, "advance-1")
        private_ref = real_fetch(remote_url, remote_ref, **kwargs)
        captured_ref["value"] = private_ref
        return private_ref

    def racy_observe(remote_url, **kwargs):
        observe_calls["count"] += 1
        if observe_calls["count"] == 2:
            # Between the fetch just above and this S2 observe, the remote
            # advances *again* -- S2 can never corroborate F in this case.
            _push_new_commit(origin, tmp_path, "advance-2")
        return real_observe(remote_url, **kwargs)

    monkeypatch.setattr(exec_mod, "run_control_plane_git_fetch_default_ref", racy_fetch)
    monkeypatch.setattr(exec_mod, "run_control_plane_git_observe_default_ref", racy_observe)

    with pytest.raises(exec_mod.ControlPlaneUnavailable, match="remote_default_ref_state_machine_rejected"):
        exec_mod.run_control_plane_remote_default_ref_binding(
            remote, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=_deadline()
        )
    assert observe_calls["count"] == 2  # exactly one S1 + one bounded S2, never more
    private_ref = captured_ref["value"]
    assert not _local_private_ref_exists(local, private_ref)


def test_given_cas_cleanup_itself_fails_when_state_machine_rejects_then_control_plane_unavailable_chained(
    tmp_path, monkeypatch
):
    local, origin, url, _oid1 = _init_remote_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    remote = validate_literal_remote_url(url)
    real_fetch = exec_mod.run_control_plane_git_fetch_default_ref

    def racy_fetch(remote_url, remote_ref, **kwargs):
        _push_new_commit(origin, tmp_path, "advance")
        _retarget_origin_head_to_new_branch(origin, tmp_path, "other")
        return real_fetch(remote_url, remote_ref, **kwargs)

    def failing_delete(*args, **kwargs):
        raise RuntimeError("delete_private_ref_cas_failed:simulated")

    monkeypatch.setattr(exec_mod, "run_control_plane_git_fetch_default_ref", racy_fetch)
    monkeypatch.setattr(exec_mod, "run_control_plane_git_delete_private_ref_cas", failing_delete)

    with pytest.raises(exec_mod.ControlPlaneUnavailable, match="private_ref_cleanup_failed") as excinfo:
        exec_mod.run_control_plane_remote_default_ref_binding(
            remote, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=_deadline()
        )
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "simulated" in str(excinfo.value.__cause__)


# ---------------------------------------------------------------------------
# AC5: fixed dedicated worktree crash/rerun recovery
# ---------------------------------------------------------------------------


def _bound_git(local: Path, url: str, scratch: Path) -> tuple:
    remote = validate_literal_remote_url(url)
    private_ref, accepted_oid, object_format = exec_mod.run_control_plane_remote_default_ref_binding(
        remote, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=_deadline()
    )
    return private_ref, accepted_oid, object_format


def test_given_absent_fixed_worktree_when_recovered_then_detached_locked_worktree_is_created(tmp_path):
    local, _origin, url, oid1 = _init_remote_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    _private_ref, accepted_oid, object_format = _bound_git(local, url, scratch)
    common_dir = BOOTSTRAP._canonical_existing_git_common_dir(local, deadline_at=time.monotonic() + 5)

    result = BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        accepted_oid,
        object_format,
        project_root=str(local),
        canonical_common_dir=common_dir,
        deadline=_deadline(),
        scratch_root=str(scratch),
    )
    assert result["state"] == "created"
    fixed_path = BOOTSTRAP.fixed_control_plane_worktree_path(str(local))
    assert os.path.realpath(result["worktree_path"]) == os.path.realpath(fixed_path)
    head = subprocess.run(
        ["git", "-C", fixed_path, "rev-parse", "HEAD"], check=True, text=True, capture_output=True
    ).stdout.strip()
    assert head == oid1


def test_given_verified_identity_and_same_accepted_oid_when_recovered_then_reused_without_mutation(tmp_path):
    local, _origin, url, _oid1 = _init_remote_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    common_dir = BOOTSTRAP._canonical_existing_git_common_dir(local, deadline_at=time.monotonic() + 5)

    _private_ref_1, accepted_oid_1, object_format = _bound_git(local, url, scratch)
    first = BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        accepted_oid_1,
        object_format,
        project_root=str(local),
        canonical_common_dir=common_dir,
        deadline=_deadline(),
        scratch_root=str(scratch),
    )
    assert first["state"] == "created"

    _private_ref_2, accepted_oid_2, _fmt = _bound_git(local, url, scratch)
    assert accepted_oid_2 == accepted_oid_1
    second = BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        accepted_oid_2,
        object_format,
        project_root=str(local),
        canonical_common_dir=common_dir,
        deadline=_deadline(),
        scratch_root=str(scratch),
    )
    assert second["state"] == "reused"
    assert second["worktree_path"] == first["worktree_path"]


def test_given_verified_identity_clean_and_different_oid_when_recovered_then_controlled_refresh_recreates(tmp_path):
    local, origin, url, oid1 = _init_remote_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    common_dir = BOOTSTRAP._canonical_existing_git_common_dir(local, deadline_at=time.monotonic() + 5)

    _ref1, accepted_oid_1, object_format = _bound_git(local, url, scratch)
    BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        accepted_oid_1,
        object_format,
        project_root=str(local),
        canonical_common_dir=common_dir,
        deadline=_deadline(),
        scratch_root=str(scratch),
    )
    new_oid = _push_new_commit(origin, tmp_path, "refresh")
    _ref2, accepted_oid_2, _fmt = _bound_git(local, url, scratch)
    assert accepted_oid_2.value == new_oid

    refreshed = BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        accepted_oid_2,
        object_format,
        project_root=str(local),
        canonical_common_dir=common_dir,
        deadline=_deadline(),
        scratch_root=str(scratch),
    )
    assert refreshed["state"] == "refreshed"
    head = subprocess.run(
        ["git", "-C", refreshed["worktree_path"], "rev-parse", "HEAD"], check=True, text=True, capture_output=True
    ).stdout.strip()
    assert head == new_oid


def test_given_verified_identity_but_dirty_when_recovered_then_fails_closed_regardless_of_oid(tmp_path):
    local, origin, url, _oid1 = _init_remote_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    common_dir = BOOTSTRAP._canonical_existing_git_common_dir(local, deadline_at=time.monotonic() + 5)

    _ref1, accepted_oid_1, object_format = _bound_git(local, url, scratch)
    created = BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        accepted_oid_1,
        object_format,
        project_root=str(local),
        canonical_common_dir=common_dir,
        deadline=_deadline(),
        scratch_root=str(scratch),
    )
    (Path(created["worktree_path"]) / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    new_oid = _push_new_commit(origin, tmp_path, "dirty-refresh")
    _ref2, accepted_oid_2, _fmt = _bound_git(local, url, scratch)
    assert accepted_oid_2.value == new_oid

    with pytest.raises(exec_mod.ControlPlaneUnavailable, match="fixed_worktree_dirty"):
        BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
            accepted_oid_2,
            object_format,
            project_root=str(local),
            canonical_common_dir=common_dir,
            deadline=_deadline(),
            scratch_root=str(scratch),
        )


def test_given_path_occupied_outside_worktree_catalog_when_recovered_then_unknown_owner_fails_closed(tmp_path):
    local, _origin, url, _oid1 = _init_remote_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    common_dir = BOOTSTRAP._canonical_existing_git_common_dir(local, deadline_at=time.monotonic() + 5)
    _ref, accepted_oid, object_format = _bound_git(local, url, scratch)

    fixed_path = Path(BOOTSTRAP.fixed_control_plane_worktree_path(str(local)))
    fixed_path.mkdir(parents=True)
    (fixed_path / "not-a-worktree.txt").write_text("occupied\n", encoding="utf-8")

    with pytest.raises(exec_mod.ControlPlaneUnavailable, match="fixed_worktree_unknown_owner"):
        BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
            accepted_oid,
            object_format,
            project_root=str(local),
            canonical_common_dir=common_dir,
            deadline=_deadline(),
            scratch_root=str(scratch),
        )


def test_given_catalog_entry_not_detached_when_recovered_then_linkage_mismatch_fails_closed(tmp_path):
    local, _origin, url, _oid1 = _init_remote_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    common_dir = BOOTSTRAP._canonical_existing_git_common_dir(local, deadline_at=time.monotonic() + 5)
    _ref, accepted_oid, object_format = _bound_git(local, url, scratch)

    fixed_path = BOOTSTRAP.fixed_control_plane_worktree_path(str(local))
    subprocess.run(
        ["git", "-C", str(local), "worktree", "add", "-b", "unexpected-branch", fixed_path, "main"],
        check=True,
    )

    with pytest.raises(exec_mod.ControlPlaneUnavailable, match="fixed_worktree_linkage_mismatch"):
        BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
            accepted_oid,
            object_format,
            project_root=str(local),
            canonical_common_dir=common_dir,
            deadline=_deadline(),
            scratch_root=str(scratch),
        )


def test_given_git_common_dir_mismatch_when_recovered_then_linkage_mismatch_fails_closed(tmp_path, monkeypatch):
    local, _origin, url, _oid1 = _init_remote_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    common_dir = BOOTSTRAP._canonical_existing_git_common_dir(local, deadline_at=time.monotonic() + 5)
    _ref, accepted_oid, object_format = _bound_git(local, url, scratch)

    fixed_path = BOOTSTRAP.fixed_control_plane_worktree_path(str(local))
    real_add = exec_mod.run_control_plane_git_add_detached_locked_worktree
    real_add(
        exec_mod.validate_detached_worktree_path(fixed_path, str(local)),
        accepted_oid,
        cwd=str(local),
        project_root=str(local),
        deadline=_deadline(),
        scratch_root=str(scratch),
    )

    real_list_worktrees = BOOTSTRAP.list_worktrees

    def tampered_list_worktrees(project_root, deadline=None):
        catalog = real_list_worktrees(project_root, deadline)
        for entry in catalog:
            if os.path.realpath(entry.get("worktree_realpath", "")) == os.path.realpath(fixed_path):
                entry["git_common_dir"] = str(Path(project_root) / "not-the-real-common-dir")
        return catalog

    monkeypatch.setattr(BOOTSTRAP, "list_worktrees", tampered_list_worktrees)

    with pytest.raises(exec_mod.ControlPlaneUnavailable, match="fixed_worktree_linkage_mismatch"):
        BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
            accepted_oid,
            object_format,
            project_root=str(local),
            canonical_common_dir=common_dir,
            deadline=_deadline(),
            scratch_root=str(scratch),
        )


# ---------------------------------------------------------------------------
# AC6/AC7: session seam holds the guard through cleanup, and reports cleanup
# failure as `control_plane_unavailable` with no retention.
# ---------------------------------------------------------------------------


def test_given_local_fixture_remote_when_full_session_runs_then_worktree_created_and_private_ref_left_clean(
    tmp_path, monkeypatch
):
    local, _origin, url, oid1 = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    result = BOOTSTRAP.run_control_plane_preflight_session(str(local), scratch_root=str(tmp_path / "scratch"))
    assert result["status"] == "ok"
    assert result["worktree_state"] == "created"
    assert result["accepted_oid"] == oid1

    refs = subprocess.run(
        ["git", "-C", str(local), "for-each-ref", "refs/loop-protocol/control-plane/default-ref/"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert refs.strip() == ""  # the temporary private ref is cleaned up after a successful handoff

    # The guard was released (bounded terminal cleanup already completed),
    # so a fresh acquisition on the very same repository succeeds again
    # instead of raising the non-reentrant-acquire error.
    guard = BOOTSTRAP.acquire_control_plane_preflight_lifecycle_mutex(local, deadline_at=time.monotonic() + 2)
    guard.assert_held()
    guard.release()


def test_given_worktree_handoff_fails_when_session_runs_then_private_ref_cleaned_up_and_guard_released(
    tmp_path, monkeypatch
):
    local, _origin, url, _oid1 = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    fixed_path = Path(BOOTSTRAP.fixed_control_plane_worktree_path(str(local)))
    fixed_path.mkdir(parents=True)
    (fixed_path / "not-a-worktree.txt").write_text("occupied\n", encoding="utf-8")

    with pytest.raises(BOOTSTRAP.ControlPlaneUnavailable, match="fixed_worktree_unknown_owner"):
        BOOTSTRAP.run_control_plane_preflight_session(str(local), scratch_root=str(tmp_path / "scratch"))

    refs = subprocess.run(
        ["git", "-C", str(local), "for-each-ref", "refs/loop-protocol/control-plane/default-ref/"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert refs.strip() == ""  # the private ref allocated before the failed handoff is still cleaned up

    guard = BOOTSTRAP.acquire_control_plane_preflight_lifecycle_mutex(local, deadline_at=time.monotonic() + 2)
    guard.assert_held()
    guard.release()


def test_given_final_private_ref_cleanup_itself_fails_when_session_succeeds_worktree_then_control_plane_unavailable(
    tmp_path, monkeypatch
):
    local, _origin, url, _oid1 = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    def failing_delete(*args, **kwargs):
        raise RuntimeError("delete_private_ref_cas_failed:simulated")

    monkeypatch.setattr(BOOTSTRAP, "run_control_plane_git_delete_private_ref_cas", failing_delete)

    with pytest.raises(BOOTSTRAP.ControlPlaneUnavailable, match="private_ref_cleanup_failed") as excinfo:
        BOOTSTRAP.run_control_plane_preflight_session(str(local), scratch_root=str(tmp_path / "scratch"))
    assert isinstance(excinfo.value.__cause__, RuntimeError)

    guard = BOOTSTRAP.acquire_control_plane_preflight_lifecycle_mutex(local, deadline_at=time.monotonic() + 2)
    guard.assert_held()
    guard.release()
