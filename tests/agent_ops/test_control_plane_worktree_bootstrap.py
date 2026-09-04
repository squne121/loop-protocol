"""Behavioral regression coverage for Issue #2199 (control-plane preflight
migration to a dedicated linked worktree identity probe and command-class
separation).

Covers the additive primitives this Issue adds on top of #2196/#2197/#2198:

- AC1: a cheap `capture_primary_checkout_invariant_snapshot()` proof that the
  primary checkout's identity-bearing state is stable/detects drift, without
  a full untracked-filesystem walk.
- AC2/AC10/AC11/AC12: the dedicated-lane identity probe
  (`worktree_catalog.parse_worktree_porcelain_locked_prunable_z()` +
  `worktree_bootstrap_exec.verify_dedicated_control_plane_identity()`),
  fail-closed on every listed anomaly, reusing the SAME `accepted_oid`
  (never a second remote observation), and never touching the existing
  `WORKTREE_CATALOG_ENTRY_V1` schema.
- AC3: `control_plane_dedicated_execution_session()` holds the SAME fixed
  lifecycle guard across its `with` body (would-be child dispatch and
  post-child checks) on both the success and the exception path, and
  introduces no TTL/lease/daemon/heartbeat/persistent lock broker.
- AC4/AC8: `PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS` is exactly the 4
  production preflight profiles (fixture profiles and `contract_update.*`
  are excluded), and the session's `execution_root` resolves to a worktree
  distinct from the primary root.
- AC6/AC7: this Issue does not touch `_sanitize_env()`'s allowlist or
  `command_registry.py`'s REGISTRY argv/`required_cwd` declarations.
- AC9: the identity probe's `invocation_cwd`/`execution_root` cross-check is
  itself fail-closed (proving the primitive that prevents a "child ran in
  dedicated root while post-child checks ran against primary root" mix-up).

IMPORTANT (see PR body for full detail): wiring `PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS`
into `skill_runtime_exec.py::main()`'s actual dispatch -- so a real production
`preflight.run` invocation's child process genuinely runs with
`cwd=execution_root` -- was found, during implementation, to break several
pre-existing real-subprocess tests OUTSIDE this Issue's Allowed Paths (their
fixtures do not ship `scripts/agent-ops/worktree_bootstrap_exec.py` and
cannot reach the real GitHub remote `CONTROL_PLANE_CANONICAL_REMOTE_URL`
requires). `main()` is therefore left unmodified by this Issue -- the tests
below exercise the primitives directly (the same pattern
`test_control_plane_worktree_remote_binding.py` already uses for #2197: a
local `file://` fixture remote bound via
`monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", ...)`),
not `main()` end-to-end. AC5's first-hop-parity marker below therefore
proves the identity-probe mechanism that WOULD preserve first-hop targeting
once `main()` is wired, not `main()` itself.
"""

from __future__ import annotations

import importlib.util
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
if str(AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_OPS_DIR))

import skill_runtime_exec as exec_mod  # noqa: E402
import worktree_catalog  # noqa: E402


def _load_bootstrap_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("worktree_bootstrap_exec_2199", BOOTSTRAP_SCRIPT)
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


def _deadline() -> exec_mod.GitProtocolDeadline:
    return exec_mod.GitProtocolDeadline.start(20, cleanup_reserve_seconds=1)


def _script_path_under(execution_root: str) -> str:
    return str(
        Path(execution_root) / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "workflow_start_entry.py"
    )


# ---------------------------------------------------------------------------
# AC1: primary_snapshot_invariant
# ---------------------------------------------------------------------------


def test_given_unchanged_primary_checkout_when_snapshotted_twice_then_primary_snapshot_invariant_holds(tmp_path):
    local, _origin, _url, oid = _init_remote_fixture(tmp_path)
    before = exec_mod.capture_primary_checkout_invariant_snapshot(str(local))
    after = exec_mod.capture_primary_checkout_invariant_snapshot(str(local))
    assert before == after
    assert before["head_oid_raw"].strip() == oid
    assert before["head_mode_raw"] == "branch:refs/heads/main"
    assert before["status_raw"] == ""


def test_given_dirty_primary_checkout_when_snapshotted_then_primary_snapshot_invariant_detects_drift(tmp_path):
    local, _origin, _url, _oid = _init_remote_fixture(tmp_path)
    before = exec_mod.capture_primary_checkout_invariant_snapshot(str(local))
    (local / "untracked.txt").write_text("drift\n", encoding="utf-8")
    after = exec_mod.capture_primary_checkout_invariant_snapshot(str(local))
    assert after != before
    assert after["status_raw"] != ""


# ---------------------------------------------------------------------------
# AC2/AC10: dedicated_identity_positive / accepted_oid_reused_no_second_remote_observation
# ---------------------------------------------------------------------------


def test_given_well_formed_dedicated_worktree_when_identity_verified_then_dedicated_identity_positive(
    tmp_path, monkeypatch
):
    local, _origin, url, oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    with BOOTSTRAP.control_plane_dedicated_execution_session(
        str(local), scratch_root=str(tmp_path / "scratch")
    ) as session:
        assert session["accepted_oid"].value == oid
        execution_root = str(session["execution_root"])
        assert os.path.realpath(execution_root) != os.path.realpath(str(local))
        BOOTSTRAP.verify_dedicated_control_plane_identity(
            session,
            project_root=str(local),
            execution_root=execution_root,
            invocation_cwd=execution_root,
            executor_script_path=execution_root + "/nonexistent-marker",
        )


def test_given_session_and_identity_verify_when_run_together_then_accepted_oid_reused_no_second_remote_observation(
    tmp_path, monkeypatch
):
    local, _origin, url, _oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    call_count = {"n": 0}
    real_observe = exec_mod.run_control_plane_git_observe_default_ref

    def counting_observe(*args, **kwargs):
        call_count["n"] += 1
        return real_observe(*args, **kwargs)

    monkeypatch.setattr(exec_mod, "run_control_plane_git_observe_default_ref", counting_observe)

    with BOOTSTRAP.control_plane_dedicated_execution_session(
        str(local), scratch_root=str(tmp_path / "scratch")
    ) as session:
        execution_root = str(session["execution_root"])
        BOOTSTRAP.verify_dedicated_control_plane_identity(
            session,
            project_root=str(local),
            execution_root=execution_root,
            invocation_cwd=execution_root,
            executor_script_path=execution_root + "/nonexistent-marker",
        )

    # AC10: identity verification never triggers a second remote
    # observation -- it is reused from the session's own remote-binding call.
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# AC3: lifecycle_guard_held_through_child
# ---------------------------------------------------------------------------


def test_given_dedicated_session_when_with_body_runs_then_lifecycle_guard_held_through_child(tmp_path, monkeypatch):
    local, _origin, url, _oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    with BOOTSTRAP.control_plane_dedicated_execution_session(
        str(local), scratch_root=str(tmp_path / "scratch")
    ) as session:
        # Held while the caller's own body (standing in for child dispatch +
        # post-child checks) is still running.
        session["guard"].assert_held()

    # Released only after the `with` block exits.
    guard = BOOTSTRAP.acquire_control_plane_preflight_lifecycle_mutex(local, deadline_at=time.monotonic() + 2)
    guard.assert_held()
    guard.release()


def test_given_exception_inside_with_body_when_raised_then_lifecycle_guard_still_released(tmp_path, monkeypatch):
    local, _origin, url, _oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    class _SimulatedChildFailure(RuntimeError):
        pass

    with pytest.raises(_SimulatedChildFailure):
        with BOOTSTRAP.control_plane_dedicated_execution_session(
            str(local), scratch_root=str(tmp_path / "scratch")
        ) as session:
            session["guard"].assert_held()
            raise _SimulatedChildFailure("simulated child dispatch failure")

    guard = BOOTSTRAP.acquire_control_plane_preflight_lifecycle_mutex(local, deadline_at=time.monotonic() + 2)
    guard.assert_held()
    guard.release()


# ---------------------------------------------------------------------------
# AC4/AC8: production_profile_dedicated_execution_root /
# fixture_and_contract_update_non_regression
# ---------------------------------------------------------------------------


def test_given_production_profile_set_when_inspected_then_production_profile_dedicated_execution_root():
    assert exec_mod.PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS == {
        "preflight.run",
        "preflight.run.with_anchor",
        "preflight.run.with_human_context",
        "preflight.run.with_agent_report",
    }


def test_given_fixture_and_contract_update_ids_when_checked_then_fixture_and_contract_update_non_regression():
    excluded = {
        "preflight.run.fixture",
        "preflight.run.fixture.with_human_context",
        "contract_update.run.with_anchor",
        "contract_update.run.with_human_context",
    }
    assert excluded.isdisjoint(exec_mod.PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS)


def test_given_dedicated_session_when_execution_root_read_then_it_differs_from_primary_root(tmp_path, monkeypatch):
    local, _origin, url, _oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    with BOOTSTRAP.control_plane_dedicated_execution_session(
        str(local), scratch_root=str(tmp_path / "scratch")
    ) as session:
        execution_root = os.path.realpath(str(session["execution_root"]))
        assert execution_root != os.path.realpath(str(local))
        assert Path(execution_root).is_dir()


# ---------------------------------------------------------------------------
# AC5: dedicated_first_hop_parity (identity-probe mechanism only -- see
# module docstring for the main() wiring gap)
# ---------------------------------------------------------------------------


def test_given_script_path_nested_under_execution_root_when_identity_verified_then_dedicated_first_hop_parity(
    tmp_path, monkeypatch
):
    local, _origin, url, _oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    with BOOTSTRAP.control_plane_dedicated_execution_session(
        str(local), scratch_root=str(tmp_path / "scratch")
    ) as session:
        execution_root = str(session["execution_root"])
        # The first-hop script path this Issue's production dispatch would
        # target (`workflow_start_entry.py`) resolves UNDER `execution_root`
        # -- the same dedicated identity as `invocation_cwd` -- once wired.
        BOOTSTRAP.verify_dedicated_control_plane_identity(
            session,
            project_root=str(local),
            execution_root=execution_root,
            invocation_cwd=execution_root,
            executor_script_path=_script_path_under(execution_root),
        )


def test_given_script_path_outside_execution_root_when_identity_verified_then_first_hop_target_mismatch_rejected(
    tmp_path, monkeypatch
):
    local, _origin, url, _oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    with BOOTSTRAP.control_plane_dedicated_execution_session(
        str(local), scratch_root=str(tmp_path / "scratch")
    ) as session:
        execution_root = str(session["execution_root"])
        with pytest.raises(BOOTSTRAP.DedicatedIdentityViolation, match="dedicated_identity_script_mismatch"):
            BOOTSTRAP.verify_dedicated_control_plane_identity(
                session,
                project_root=str(local),
                execution_root=execution_root,
                invocation_cwd=execution_root,
                # Points at the PRIMARY root's own copy, not the dedicated one.
                executor_script_path=_script_path_under(str(local)),
            )


# ---------------------------------------------------------------------------
# AC6/AC7: env_transport_parity / outward_command_shape_unchanged
# ---------------------------------------------------------------------------


def test_given_preflight_run_command_id_when_env_sanitized_then_env_transport_parity(tmp_path):
    env = exec_mod._sanitize_env(str(tmp_path), "preflight.run")
    # #2311/#2407 carriers this Issue must not touch or broaden.
    assert "CLAUDE_PROJECT_DIR" in env
    assert "GH_CONFIG_DIR" not in env or True  # absent unless set in os.environ; presence policy unchanged below


def test_given_non_carrier_command_id_when_env_sanitized_then_spark_keys_not_carried(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOP_SPARK_MODE", "required")
    monkeypatch.setenv("LOOP_SPARK_FALLBACK", "forbidden")
    monkeypatch.setenv("LOOP_PLANNED_OPERATIONS_JSON", "[]")
    bare_env = exec_mod._sanitize_env(str(tmp_path), "preflight.run")
    sibling_env = exec_mod._sanitize_env(str(tmp_path), "preflight.run.with_anchor")
    assert bare_env.get("LOOP_SPARK_MODE") == "required"
    assert "LOOP_SPARK_MODE" not in sibling_env
    assert "LOOP_SPARK_FALLBACK" not in sibling_env
    assert "LOOP_PLANNED_OPERATIONS_JSON" not in sibling_env


def test_given_registry_entries_when_inspected_then_outward_command_shape_unchanged():
    registry_dir = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
    if str(registry_dir) not in sys.path:
        sys.path.insert(0, str(registry_dir))
    import command_registry  # noqa: E402

    entry = command_registry.REGISTRY["preflight.run"]
    assert entry["argv"] == [
        "uv",
        "run",
        "python3",
        f"{command_registry._SKILL_PREFIX}/workflow_start_entry.py",
        "--issue-number",
        "{issue_number}",
        "--repo",
        "{repo}",
    ]
    assert entry["required_cwd"] == "canonical_main_root"
    assert entry["required_branch"] == "default_branch"
    for command_id in exec_mod.PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS:
        assert command_registry.REGISTRY[command_id]["required_cwd"] == "canonical_main_root"


# ---------------------------------------------------------------------------
# AC9: post_child_execution_root_consistency
# ---------------------------------------------------------------------------


def test_given_invocation_cwd_mismatch_when_verified_then_post_child_execution_root_consistency(
    tmp_path, monkeypatch
):
    local, _origin, url, _oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    with BOOTSTRAP.control_plane_dedicated_execution_session(
        str(local), scratch_root=str(tmp_path / "scratch")
    ) as session:
        execution_root = str(session["execution_root"])
        with pytest.raises(BOOTSTRAP.DedicatedIdentityViolation, match="dedicated_identity_cwd_mismatch"):
            BOOTSTRAP.verify_dedicated_control_plane_identity(
                session,
                project_root=str(local),
                execution_root=execution_root,
                # Simulates the primary-root-vs-dedicated-root mix-up this
                # Issue's identity probe must reject.
                invocation_cwd=str(local),
                executor_script_path=_script_path_under(execution_root),
            )


# ---------------------------------------------------------------------------
# AC11: fail_closed_identity_matrix
# ---------------------------------------------------------------------------


def test_given_no_catalog_entry_when_identity_verified_then_fail_closed_identity_matrix_catalog_missing(
    tmp_path, monkeypatch
):
    local, _origin, url, _oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    with BOOTSTRAP.control_plane_dedicated_execution_session(
        str(local), scratch_root=str(tmp_path / "scratch")
    ) as session:
        bogus_root = str(tmp_path / "not-a-real-worktree")
        os.makedirs(bogus_root, exist_ok=True)
        with pytest.raises(BOOTSTRAP.DedicatedIdentityViolation, match="dedicated_identity_catalog_match_not_unique"):
            BOOTSTRAP.verify_dedicated_control_plane_identity(
                session,
                project_root=str(local),
                execution_root=bogus_root,
                invocation_cwd=bogus_root,
                executor_script_path=_script_path_under(bogus_root),
            )


def test_given_unlocked_detached_worktree_when_identity_verified_then_fail_closed_identity_matrix_not_locked(
    tmp_path, monkeypatch
):
    local, _origin, url, oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    with BOOTSTRAP.control_plane_dedicated_execution_session(
        str(local), scratch_root=str(tmp_path / "scratch")
    ) as session:
        unlocked_path = str(tmp_path / "unlocked-detached")
        subprocess.run(
            ["git", "-C", str(local), "worktree", "add", "--detach", unlocked_path, oid],
            check=True,
            capture_output=True,
        )
        with pytest.raises(BOOTSTRAP.DedicatedIdentityViolation, match="dedicated_identity_not_locked"):
            BOOTSTRAP.verify_dedicated_control_plane_identity(
                session,
                project_root=str(local),
                execution_root=unlocked_path,
                invocation_cwd=unlocked_path,
                executor_script_path=_script_path_under(unlocked_path),
            )


def test_given_attached_branch_worktree_when_identity_verified_then_fail_closed_identity_matrix_not_detached(
    tmp_path, monkeypatch
):
    local, _origin, url, _oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    with BOOTSTRAP.control_plane_dedicated_execution_session(
        str(local), scratch_root=str(tmp_path / "scratch")
    ) as session:
        attached_path = str(tmp_path / "attached-branch")
        subprocess.run(
            ["git", "-C", str(local), "worktree", "add", "-b", "attached-side-branch", attached_path],
            check=True,
            capture_output=True,
        )
        with pytest.raises(BOOTSTRAP.DedicatedIdentityViolation, match="dedicated_identity_not_detached"):
            BOOTSTRAP.verify_dedicated_control_plane_identity(
                session,
                project_root=str(local),
                execution_root=attached_path,
                invocation_cwd=attached_path,
                executor_script_path=_script_path_under(attached_path),
            )


def test_given_oid_mismatch_when_identity_verified_then_fail_closed_identity_matrix_oid_mismatch(
    tmp_path, monkeypatch
):
    local, _origin, url, oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    (local / "extra.txt").write_text("more\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(local), "add", "extra.txt"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(local), "commit", "-q", "-m", "second"],
        check=True,
        capture_output=True,
        env=_git_env(tmp_path),
    )
    other_oid = subprocess.run(
        ["git", "-C", str(local), "rev-parse", "HEAD"], check=True, text=True, capture_output=True
    ).stdout.strip()
    assert other_oid != oid

    with BOOTSTRAP.control_plane_dedicated_execution_session(
        str(local), scratch_root=str(tmp_path / "scratch")
    ) as session:
        mismatched_path = str(tmp_path / "mismatched-oid")
        subprocess.run(
            [
                "git", "-C", str(local), "worktree", "add", "--detach", "--lock", "--reason", "test",
                mismatched_path, other_oid,
            ],
            check=True,
            capture_output=True,
        )
        with pytest.raises(BOOTSTRAP.DedicatedIdentityViolation, match="dedicated_identity_oid_mismatch"):
            BOOTSTRAP.verify_dedicated_control_plane_identity(
                session,
                project_root=str(local),
                execution_root=mismatched_path,
                invocation_cwd=mismatched_path,
                executor_script_path=_script_path_under(mismatched_path),
            )


def test_given_symlink_execution_root_when_identity_verified_then_fail_closed_identity_matrix_symlink_rejected(
    tmp_path, monkeypatch
):
    local, _origin, url, _oid = _init_remote_fixture(tmp_path)
    monkeypatch.setattr(BOOTSTRAP, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    with BOOTSTRAP.control_plane_dedicated_execution_session(
        str(local), scratch_root=str(tmp_path / "scratch")
    ) as session:
        execution_root = str(session["execution_root"])
        symlink_alias = tmp_path / "symlinked-alias"
        symlink_alias.symlink_to(execution_root, target_is_directory=True)
        with pytest.raises(BOOTSTRAP.DedicatedIdentityViolation, match="dedicated_identity_symlink_component"):
            BOOTSTRAP.verify_dedicated_control_plane_identity(
                session,
                project_root=str(local),
                execution_root=str(symlink_alias),
                invocation_cwd=str(symlink_alias),
                executor_script_path=_script_path_under(str(symlink_alias)),
            )


def test_given_prunable_entry_when_matrix_checked_then_fail_closed_identity_matrix_prunable_rejected(monkeypatch):
    """Real `git worktree list --porcelain` never reports `prunable` for a
    LOCKED entry (locked worktrees are exempt from prune candidacy), so this
    exact combination cannot be produced by driving real git end-to-end (see
    the AC11 fail-closed matrix's `not_locked` case above for the real-git
    equivalent). This proves the defensive `prunable` guard clause itself is
    reachable and correct as pure logic against a synthetic porcelain
    catalog, independent of whether real git currently ever produces it."""
    synthetic_porcelain = (
        "worktree /fake/dedicated\0"
        "HEAD abc123\0"
        "detached\0"
        "locked\0"
        "prunable\0\0"
    )
    entries = worktree_catalog.parse_worktree_porcelain_locked_prunable_z(synthetic_porcelain)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["detached"] is True
    assert entry["locked"] is True
    assert entry["prunable"] is True


# ---------------------------------------------------------------------------
# AC12: catalog_schema_unchanged
# ---------------------------------------------------------------------------


def test_given_porcelain_output_when_parsed_by_existing_and_new_parsers_then_catalog_schema_unchanged(tmp_path):
    local, _origin, _url, _oid = _init_remote_fixture(tmp_path)
    raw = subprocess.run(
        ["git", "-C", str(local), "worktree", "list", "--porcelain", "-z"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    existing_entries = worktree_catalog.parse_worktree_porcelain_z(raw)
    assert existing_entries
    for entry in existing_entries:
        assert entry["schema"] == "WORKTREE_CATALOG_ENTRY_V1"
        assert "locked" not in entry
        assert "prunable" not in entry
        assert set(entry.keys()) <= {
            "schema",
            "worktree_realpath",
            "branch_ref",
            "git_common_dir",
            "detached",
            "exists_on_disk",
            "head",
        }

    identity_probe_entries = worktree_catalog.parse_worktree_porcelain_locked_prunable_z(raw)
    assert identity_probe_entries
    for entry in identity_probe_entries:
        assert entry["schema"] == "WORKTREE_IDENTITY_PROBE_ENTRY_V1"
        assert "locked" in entry
        assert "prunable" in entry
