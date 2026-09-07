"""Behavioral coverage for Issue #2200 (control-plane preflight: dedicated
artifact cleanup, dirty/unknown-owner worktree force-remove prohibition, and
generation invalidation), layered on top of #2196/#2197/#2198/#2199/#2393
without redesigning the existing producer/consumer transport.

Covers:

- AC3: generation invalidation across the 3 cases the Issue enumerates --
  same-issue re-run at the same accepted OID (reused, artifacts survive),
  a different Issue's own artifact tree coexisting untouched in the SAME
  shared dedicated worktree, and an accepted-OID update with a clean
  working tree (refreshed -- the old generation's artifacts are gone,
  because `git worktree remove` + `git worktree add` recreates the whole
  checkout, never a selective artifact wipe).
- AC3 (dirty/unknown-owner prohibition, non-regression confirmation): a
  dirty dedicated worktree fails closed WITHOUT ever reaching
  `git worktree remove --force`, and a path occupied outside the worktree
  catalog fails closed as an unknown owner -- both pre-existing #2197
  behaviors this Issue must not weaken.
- AC5/AC6: the fixed lifecycle guard is held across dispatch AND the new
  confinement/cleanup checks, and is released via the existing `finally`
  semantics even when a confinement violation is detected (never a
  primary-root fallback, never a leaked lock).
- AC6 (2-process barrier-based integration test): two REAL OS processes
  racing for the SAME fixed lifecycle mutex are synchronized to attempt
  acquisition at the same instant via `multiprocessing.Barrier` (never a
  fixed `sleep`), and mutual exclusion is observed end-to-end via `flock`.
- PR #2557 review fix-delta (item 2): a real consumer-side E2E continuation
  of the AC3 generation-invalidation test above -- after the old
  generation's artifact is gone, the REAL, unmodified
  `run_repair_action_apply()` consumer fails closed (`not_attempted` /
  `secure_open_rejected`) against the now-removed path, never falling back
  to some same-named path elsewhere, and never invoking its
  `apply_transaction`/`fetch_current` injection seams.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import multiprocessing
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
# PR #2557 review fix-delta (item 2): the SAME real production consumer
# module `tests/agent_ops/test_control_plane_worktree_bootstrap.py` already
# imports this exact way (identical absolute `ISSUE_REFINEMENT_LOOP_SCRIPTS_DIR`)
# -- reused here, never a stub/reimplementation, so the generation-boundary
# continuation below proves the REAL `run_repair_action_apply()` fails
# closed, not a hand-rolled approximation of it.
ISSUE_REFINEMENT_LOOP_SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"

if str(AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_GUARDS_DIR))
if str(AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_OPS_DIR))
if str(ISSUE_REFINEMENT_LOOP_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_REFINEMENT_LOOP_SCRIPTS_DIR))

import skill_runtime_exec as exec_mod  # noqa: E402
import skill_runtime_command_policy as command_policy_mod  # noqa: E402
import worktree_catalog  # noqa: E402
import run_refinement_preflight as rrp  # noqa: E402


def _load_bootstrap_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("worktree_bootstrap_exec_2200", BOOTSTRAP_SCRIPT)
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
    # Mirrors this real repo's own `.gitignore` (`artifacts/` matches at any
    # depth, including `.claude/artifacts/`) -- without this, a written
    # preflight-result artifact would register as an untracked (dirty)
    # change under the dedicated worktree's OWN cleanliness check, which is
    # never the case in the real repository.
    (source / ".gitignore").write_text("artifacts/\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md", ".gitignore"], cwd=source, check=True, env=env)
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


def _commit_new_head(source_like: Path, env: dict[str, str], origin_uri: str) -> str:
    """Add a second commit and push it, returning the new HEAD OID -- used
    to simulate an accepted-OID update between two dispatches."""
    (source_like / "second.txt").write_text("second commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "second.txt"], cwd=source_like, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=source_like, check=True, env=env)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/main"], cwd=source_like, check=True, env=env)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source_like, check=True, text=True, capture_output=True
    ).stdout.strip()


def _deadline() -> exec_mod.GitProtocolDeadline:
    return exec_mod.GitProtocolDeadline.start(20, cleanup_reserve_seconds=1)


def _canonical_common_dir(local: Path) -> Path:
    return BOOTSTRAP._canonical_existing_git_common_dir(str(local), deadline_at=time.monotonic() + 10)


def _write_owned_artifact(
    dedicated_path: Path,
    issue_number: int,
    filename: str = "refinement_preflight_result_v1.json",
    *,
    owner_field_value: "int | None" = None,
) -> Path:
    """Write an artifact under `issue_number`'s own allowed root directory.
    `owner_field_value` (defaults to `issue_number`) lets a caller construct
    a path-confined-but-owner-mismatched artifact (Issue #2200 AC4's
    `stale_artifact` case), without also tripping the pre-existing
    allowed-root path check (a DIFFERENT, already-covered failure mode)."""
    artifact_dir = dedicated_path / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / filename
    declared_owner = issue_number if owner_field_value is None else owner_field_value
    artifact_path.write_text(
        json.dumps({"schema": "refinement_preflight_result/v1", "issue_number": declared_owner}), encoding="utf-8"
    )
    return artifact_path


# ---------------------------------------------------------------------------
# AC3: generation invalidation
# ---------------------------------------------------------------------------


def test_given_same_issue_rerun_same_oid_when_worktree_recovered_then_reused_and_artifacts_survive(tmp_path):
    local, _origin, _url, oid = _init_remote_fixture(tmp_path)
    object_format = command_policy_mod.validate_repository_object_format("sha1")
    deadline = _deadline()
    canonical_common_dir = _canonical_common_dir(local)
    accepted_oid = command_policy_mod.validate_repository_object_id(oid, object_format)

    first = BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        accepted_oid, object_format, project_root=str(local), canonical_common_dir=canonical_common_dir, deadline=deadline
    )
    assert first["state"] == "created"
    artifact_path = _write_owned_artifact(Path(first["worktree_path"]), 2200)

    second = BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        accepted_oid, object_format, project_root=str(local), canonical_common_dir=canonical_common_dir, deadline=deadline
    )
    assert second["state"] == "reused"
    assert second["worktree_path"] == first["worktree_path"]
    assert artifact_path.exists()


def test_given_different_issue_when_worktree_recovered_then_shared_worktree_reused_and_artifact_trees_isolated(tmp_path):
    local, _origin, _url, oid = _init_remote_fixture(tmp_path)
    object_format = command_policy_mod.validate_repository_object_format("sha1")
    deadline = _deadline()
    canonical_common_dir = _canonical_common_dir(local)
    accepted_oid = command_policy_mod.validate_repository_object_id(oid, object_format)

    first = BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        accepted_oid, object_format, project_root=str(local), canonical_common_dir=canonical_common_dir, deadline=deadline
    )
    dedicated_path = Path(first["worktree_path"])
    issue_a_artifact = _write_owned_artifact(dedicated_path, 1111)

    second = BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        accepted_oid, object_format, project_root=str(local), canonical_common_dir=canonical_common_dir, deadline=deadline
    )
    assert second["state"] == "reused"
    issue_b_artifact = _write_owned_artifact(dedicated_path, 2222)

    # Both issues' own artifact trees coexist untouched -- a shared FIXED
    # worktree identity is never itself a cross-issue generation boundary;
    # the per-issue directory scoping (`_allowed_artifact_roots`) is what
    # keeps them isolated.
    assert issue_a_artifact.exists()
    assert issue_b_artifact.exists()
    assert json.loads(issue_a_artifact.read_text())["issue_number"] == 1111
    assert json.loads(issue_b_artifact.read_text())["issue_number"] == 2222


def test_given_accepted_oid_updated_and_clean_tree_when_worktree_recovered_then_refreshed_and_old_generation_gone(tmp_path):
    local, source_origin, _url, oid = _init_remote_fixture(tmp_path)
    object_format = command_policy_mod.validate_repository_object_format("sha1")
    deadline = _deadline()
    canonical_common_dir = _canonical_common_dir(local)
    old_oid = command_policy_mod.validate_repository_object_id(oid, object_format)

    first = BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        old_oid, object_format, project_root=str(local), canonical_common_dir=canonical_common_dir, deadline=deadline
    )
    assert first["state"] == "created"
    old_generation_artifact = _write_owned_artifact(Path(first["worktree_path"]), 2200)
    assert old_generation_artifact.exists()

    # Simulate a fresh accepted-OID observation (Issue #2197's own remote
    # protocol -- not re-exercised here, only its OUTPUT: a new OID this
    # module must treat as a generation boundary).
    source_like = tmp_path / "source"
    new_oid_text = _commit_new_head(source_like, _git_env(tmp_path), source_origin.as_uri())
    new_oid = command_policy_mod.validate_repository_object_id(new_oid_text, object_format)
    # `recover_or_create_fixed_control_plane_worktree()` operates against
    # `local`'s OWN object database (mirroring #2197's real remote-binding
    # protocol, which fetches the accepted OID into a private ref before
    # ever calling this function) -- the new commit must be present there.
    subprocess.run(["git", "-C", str(local), "fetch", "-q", "origin", "main"], check=True)

    second = BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        new_oid, object_format, project_root=str(local), canonical_common_dir=canonical_common_dir, deadline=deadline
    )
    assert second["state"] == "refreshed"
    assert second["worktree_path"] == first["worktree_path"]
    # Generation invalidation contract (Issue #2200 Outcome): the OLD
    # generation's artifact is gone after the boundary -- it is never
    # required to survive, and it must never be silently reused by a
    # consumer that expects the NEW generation's own fresh state.
    assert not old_generation_artifact.exists()

    head_after = subprocess.run(
        ["git", "-C", second["worktree_path"], "rev-parse", "HEAD"], check=True, text=True, capture_output=True
    ).stdout.strip()
    assert head_after == new_oid.value


_GENERATION_ORIGINAL_BODY = "original body\n"
_GENERATION_REPAIRED_BODY = "repaired body\n"


def _write_dedicated_repair_candidate_needs_fix(dedicated_worktree: Path, issue_number: int) -> Path:
    """PR #2557 review fix-delta (item 2): mirrors the SAME needs_fix-shaped
    preflight-result artifact shape
    `tests/agent_ops/test_control_plane_worktree_bootstrap.py::_write_dedicated_repair_candidate()`
    already proves the REAL `run_repair_action_apply()` consumer reads (an
    `auto_apply_safe` repair_action with its own `candidate_body.md`
    sidecar), written under the dedicated worktree's own
    `.claude/artifacts/issue-refinement-loop/<issue>/` tree."""
    artifact_dir = dedicated_worktree / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = artifact_dir / "candidate_body.md"
    candidate_path.write_text(_GENERATION_REPAIRED_BODY)
    repair_action = {
        "schema_version": "repair_action/v1",
        "policy_version": "deterministic-issue-repair/v1",
        "disposition": "auto_apply_safe",
        "original_body_sha256": hashlib.sha256(_GENERATION_ORIGINAL_BODY.encode("utf-8")).hexdigest(),
        "repaired_body_sha256": hashlib.sha256(_GENERATION_REPAIRED_BODY.encode("utf-8")).hexdigest(),
        "diagnostics_artifact": None,
        "candidate_body_artifact": str(candidate_path),
        "repair_kinds": ["trailing_whitespace"],
        "reason_codes": ["trailing_whitespace_stripped"],
        "source_lane": "unanchored",
        "preflight_run_identity": "sha256:testrun",
        "original_updated_at": "2024-01-01T00:00:00Z",
        "source_refs_digest": None,
    }
    preflight_result = {
        "schema": "issue_refinement_preflight_result/v1",
        "repair_action": repair_action,
        "result_core_sha256": "sha256:testrun",
    }
    result_path = artifact_dir / "refinement_preflight_result_v1.json"
    result_path.write_text(json.dumps(preflight_result))
    return result_path


def test_given_needs_fix_artifact_and_generation_boundary_when_real_repair_consumer_runs_against_stale_path_then_not_attempted_and_zero_mutation_calls(
    tmp_path,
):
    """PR #2557 review fix-delta (item 2): a real consumer-side E2E
    continuation of
    `test_given_accepted_oid_updated_and_clean_tree_when_worktree_recovered_then_refreshed_and_old_generation_gone`
    above -- after the SAME accepted-OID refresh removes the OLD
    generation's `needs_fix`-shaped preflight-result artifact, the REAL,
    unmodified `run_repair_action_apply()` consumer
    (`.claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py`)
    must fail closed against the now-removed path -- `not_attempted` /
    `secure_open_rejected` (the SAME existing not_attempted/secure_open_rejected
    contract `test_repair_action_apply_stale_guard.py` already documents for
    this class of failure) -- rather than silently reading through to a
    same-named path in the primary checkout or elsewhere. This is a pure
    test addition: it requires no change to `run_repair_action_apply()`
    itself and adds no new consumer routing -- fresh-preflight restart
    routing stays explicitly OUT of Issue #2200's scope."""
    local, source_origin, _url, oid = _init_remote_fixture(tmp_path)
    object_format = command_policy_mod.validate_repository_object_format("sha1")
    deadline = _deadline()
    canonical_common_dir = _canonical_common_dir(local)
    old_oid = command_policy_mod.validate_repository_object_id(oid, object_format)

    first = BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        old_oid, object_format, project_root=str(local), canonical_common_dir=canonical_common_dir, deadline=deadline
    )
    assert first["state"] == "created"
    dedicated_path = Path(first["worktree_path"])
    old_generation_artifact = _write_dedicated_repair_candidate_needs_fix(dedicated_path, 2200)
    dedicated_relative = os.path.relpath(old_generation_artifact, local)
    assert old_generation_artifact.exists()

    # Same generation boundary as the test above: a fresh accepted-OID
    # observation with a clean working tree recreates the whole checkout.
    source_like = tmp_path / "source"
    new_oid_text = _commit_new_head(source_like, _git_env(tmp_path), source_origin.as_uri())
    new_oid = command_policy_mod.validate_repository_object_id(new_oid_text, object_format)
    subprocess.run(["git", "-C", str(local), "fetch", "-q", "origin", "main"], check=True)

    second = BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        new_oid, object_format, project_root=str(local), canonical_common_dir=canonical_common_dir, deadline=deadline
    )
    assert second["state"] == "refreshed"
    assert not old_generation_artifact.exists()

    apply_calls: list[tuple] = []
    fetch_calls: list[bool] = []

    def _apply_transaction(current_issue: dict, candidate_body: str) -> dict:
        apply_calls.append((current_issue, candidate_body))
        raise AssertionError("apply_transaction must never be invoked against a stale/removed artifact")

    def _fetch_current():
        fetch_calls.append(True)
        raise AssertionError("fetch_current must never be invoked against a stale/removed artifact")

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2200,
        preflight_result_path=dedicated_relative,
        repo_root=Path(local),
        fetch_current=_fetch_current,
        apply_transaction=_apply_transaction,
    )

    assert result["mutation_outcome"] == "not_attempted"
    assert result["failure_code"] == "secure_open_rejected"
    assert result["phase"] == "candidate_load"
    assert apply_calls == []
    assert fetch_calls == []


# ---------------------------------------------------------------------------
# AC3 (non-regression confirmation): dirty/unknown-owner worktree
# force-remove prohibition (git status --porcelain --untracked-files=all,
# never --ignored).
# ---------------------------------------------------------------------------


def test_given_dirty_dedicated_worktree_when_oid_changed_then_fail_closed_without_force_remove(tmp_path, monkeypatch):
    local, source_origin, _url, oid = _init_remote_fixture(tmp_path)
    object_format = command_policy_mod.validate_repository_object_format("sha1")
    deadline = _deadline()
    canonical_common_dir = _canonical_common_dir(local)
    old_oid = command_policy_mod.validate_repository_object_id(oid, object_format)

    first = BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
        old_oid, object_format, project_root=str(local), canonical_common_dir=canonical_common_dir, deadline=deadline
    )
    dedicated_path = Path(first["worktree_path"])
    (dedicated_path / "dirty_untracked.txt").write_text("uncommitted\n", encoding="utf-8")

    source_like = tmp_path / "source"
    new_oid_text = _commit_new_head(source_like, _git_env(tmp_path), source_origin.as_uri())
    new_oid = command_policy_mod.validate_repository_object_id(new_oid_text, object_format)

    remove_calls: list[str] = []
    original_remove = BOOTSTRAP.run_control_plane_git_remove_existing_detached_locked_worktree

    def _spy_remove(path, **kwargs):
        remove_calls.append(str(path))
        return original_remove(path, **kwargs)

    monkeypatch.setattr(BOOTSTRAP, "run_control_plane_git_remove_existing_detached_locked_worktree", _spy_remove)

    with pytest.raises(BOOTSTRAP.ControlPlaneUnavailable, match="fixed_worktree_dirty"):
        BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
            new_oid, object_format, project_root=str(local), canonical_common_dir=canonical_common_dir, deadline=deadline
        )

    # The dirty guard fails closed BEFORE ever reaching the remove step --
    # `git worktree remove --force` (double-force, for a locked worktree)
    # must never be attempted against a dirty tree.
    assert remove_calls == []
    assert (dedicated_path / "dirty_untracked.txt").exists()


def test_given_unregistered_path_occupying_fixed_slot_when_worktree_recovered_then_fail_closed_unknown_owner(tmp_path):
    local, _origin, _url, oid = _init_remote_fixture(tmp_path)
    object_format = command_policy_mod.validate_repository_object_format("sha1")
    deadline = _deadline()
    canonical_common_dir = _canonical_common_dir(local)
    accepted_oid = command_policy_mod.validate_repository_object_id(oid, object_format)

    fixed_path = Path(BOOTSTRAP.fixed_control_plane_worktree_path(str(local)))
    fixed_path.parent.mkdir(parents=True, exist_ok=True)
    fixed_path.mkdir()
    (fixed_path / "not_a_worktree.txt").write_text("squatter\n", encoding="utf-8")

    with pytest.raises(BOOTSTRAP.ControlPlaneUnavailable, match="fixed_worktree_unknown_owner"):
        BOOTSTRAP.recover_or_create_fixed_control_plane_worktree(
            accepted_oid, object_format, project_root=str(local), canonical_common_dir=canonical_common_dir, deadline=deadline
        )


# ---------------------------------------------------------------------------
# AC5/AC6: lifecycle lock held through the new confinement/cleanup checks,
# released via the existing `finally` semantics on a confinement violation.
# ---------------------------------------------------------------------------


def test_given_confinement_violation_inside_dedicated_session_when_detected_then_lock_still_released(tmp_path, monkeypatch):
    local, _origin, url, _oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    with BOOTSTRAP.control_plane_dedicated_execution_session(
        str(local), scratch_root=str(tmp_path / "scratch")
    ) as session:
        session["guard"].assert_held()
        dedicated_path = Path(str(session["execution_root"]))
        artifact_path = _write_owned_artifact(dedicated_path, 2200, owner_field_value=9999)
        reason, offending = exec_mod._validate_artifact_confinement_bounds(
            str(dedicated_path), "2200", [str(artifact_path)]
        )
        assert reason == exec_mod.ARTIFACT_CONFINEMENT_REASON_STALE_ARTIFACT
        assert offending == [str(artifact_path)]
        # The guard is STILL held while this confinement determination runs
        # (Issue #2200 In Scope: "lock保持をartifact検証完了まで延長する") --
        # never released early just because a violation was found.
        session["guard"].assert_held()

    # Released only after the `with` block exits, exactly like the existing
    # #2199 exception-path guarantee -- a confinement violation is handled
    # entirely inside the `with` body (as a returned reason_code, never a
    # raised exception that would bypass the body), and the SAME `finally`
    # release still runs.
    guard = BOOTSTRAP.acquire_control_plane_preflight_lifecycle_mutex(local, deadline_at=time.monotonic() + 2)
    guard.assert_held()
    guard.release()


def test_given_dispatch_confinement_failure_when_run_through_real_dispatch_then_publish_blocked_and_lock_released(
    tmp_path, monkeypatch, capsys
):
    """AC1/AC5 end-to-end: the REAL `_dispatch_child_and_check_postconditions()`
    entrypoint, invoked with `dispatch_root` set to a REAL dedicated worktree
    obtained from `control_plane_dedicated_execution_session()`, rejects a
    stale (owner-mismatched) artifact BEFORE stdout publication, and the
    lifecycle guard is still released once the session's `with` block exits."""
    local, _origin, url, _oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    with BOOTSTRAP.control_plane_dedicated_execution_session(
        str(local), scratch_root=str(tmp_path / "scratch")
    ) as session:
        dedicated_path = Path(str(session["execution_root"]))
        artifact_path = _write_owned_artifact(dedicated_path, 2200, owner_field_value=9999)
        stdout = f"STATUS: needs_fix\nARTIFACT:\n  refinement_preflight_result_v1: {artifact_path}\n"

        monkeypatch.setattr(
            exec_mod,
            "_run_child_with_supervision",
            lambda *a, **k: exec_mod._ChildSupervisionResult(
                timed_out=False,
                returncode=0,
                stdout=stdout,
                stderr="",
                cleanup_scope=exec_mod.CLEANUP_SCOPE_PROCESS_GROUP,
                cleanup_status=exec_mod.CLEANUP_STATUS_NOT_STARTED,
                termination=exec_mod.TERMINATION_NOT_NEEDED,
                leader_reaped=True,
            ),
        )

        exit_code = exec_mod._dispatch_child_and_check_postconditions(
            dispatch_root=str(dedicated_path),
            issue_number=2200,
            command_id="preflight.run",
            child_argv=["true"],
            env={},
            timeout_seconds=5.0,
            binary_output=False,
        )
        assert exit_code == 2
        captured = capsys.readouterr()
        assert "STATUS: needs_fix" not in captured.out
        assert "reason_code=stale_artifact" in captured.err
        session["guard"].assert_held()

    guard = BOOTSTRAP.acquire_control_plane_preflight_lifecycle_mutex(local, deadline_at=time.monotonic() + 2)
    guard.assert_held()
    guard.release()


# ---------------------------------------------------------------------------
# AC6: 2-process barrier-based integration test for the fixed lifecycle
# mutex (never a fixed `sleep` for synchronization).
# ---------------------------------------------------------------------------


_BARRIER_WORKER_HOLD_SECONDS = 1.0


def _barrier_worker(project_root: str, start_barrier, events: dict, role: str, timeout_seconds: float) -> None:
    """Top-level (picklable-by-fork) worker: waits on `start_barrier` so
    BOTH processes attempt acquisition of the SAME fixed lifecycle mutex at
    the same synchronized instant (this is the load-bearing synchronization
    primitive Issue #2200 requires in place of a fixed `sleep` guess for
    "when should the second process attempt its acquisition"). Which of the
    two actually wins the underlying `flock` race is deliberately NOT
    assumed in either direction (kernel scheduling fairness between two
    independent `flock` waiters is not this contract's concern -- only
    MUTUAL EXCLUSION is). The short post-acquire hold below is a
    non-synchronizing, purely observational delay (making the OTHER
    process's block on `flock` reliably observable instead of a timing
    coin-flip); it plays no role in coordinating the two processes'
    acquisition attempts, which `start_barrier` alone already does.
    """
    sys.path.insert(0, str(AGENT_GUARDS_DIR))
    sys.path.insert(0, str(AGENT_OPS_DIR))
    import importlib.util as _ilu

    spec = _ilu.spec_from_file_location(f"worktree_bootstrap_exec_2200_worker_{role}", BOOTSTRAP_SCRIPT)
    module = _ilu.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    start_barrier.wait(timeout=timeout_seconds)
    guard = module.acquire_control_plane_preflight_lifecycle_mutex(
        project_root, deadline_at=time.monotonic() + timeout_seconds
    )
    events[f"{role}_acquired_at"] = time.monotonic()
    time.sleep(_BARRIER_WORKER_HOLD_SECONDS)
    guard.release()
    events[f"{role}_released_at"] = time.monotonic()


def test_given_two_processes_when_racing_for_same_lifecycle_mutex_then_mutual_exclusion_holds(tmp_path):
    local, _origin, _url, _oid = _init_remote_fixture(tmp_path)
    ctx = multiprocessing.get_context("fork")
    manager = ctx.Manager()
    events = manager.dict()
    start_barrier = ctx.Barrier(2)

    first = ctx.Process(target=_barrier_worker, args=(str(local), start_barrier, events, "first", 20.0))
    second = ctx.Process(target=_barrier_worker, args=(str(local), start_barrier, events, "second", 20.0))
    first.start()
    second.start()

    first.join(timeout=25)
    second.join(timeout=25)
    assert first.exitcode == 0
    assert second.exitcode == 0

    assert "first_acquired_at" in events
    assert "second_acquired_at" in events
    assert "first_released_at" in events
    assert "second_released_at" in events

    # Determine winner/loser by acquisition order (whichever role wins is
    # NOT assumed in advance -- this test asserts MUTUAL EXCLUSION, never a
    # fixed fairness ordering): the loser could only acquire AFTER the
    # winner released.
    if events["first_acquired_at"] <= events["second_acquired_at"]:
        winner_released_at, loser_acquired_at = events["first_released_at"], events["second_acquired_at"]
    else:
        winner_released_at, loser_acquired_at = events["second_released_at"], events["first_acquired_at"]
    assert loser_acquired_at >= winner_released_at
    manager.shutdown()
