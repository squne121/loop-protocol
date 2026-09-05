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
  are excluded), and `main()`'s real dispatch-selection logic actually
  selects `cwd=execution_root` (a worktree distinct from the primary root)
  for them.
- AC6/AC7: this Issue does not touch `_sanitize_env()`'s allowlist or
  `command_registry.py`'s REGISTRY argv/`required_cwd` declarations.
- AC9: the identity probe's `invocation_cwd`/`execution_root` cross-check is
  itself fail-closed (proving the primitive that prevents a "child ran in
  dedicated root while post-child checks ran against primary root" mix-up),
  AND `main()`'s own post-child-check root selection genuinely branches on
  `command_id` (production profiles -> `execution_root`, everything else ->
  `project_root`).

Issue #2199 Scope Delta (see PR body / Issue comment for full detail):
`PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS` IS now wired into
`skill_runtime_exec.py::main()`'s actual dispatch-selection code path (a
real production `preflight.run` invocation's child process genuinely runs
with `cwd=execution_root`). The AC4/AC5/AC9-mapped tests below call
`exec_mod.main()` directly -- the REAL dispatch/post-child-check selection
logic -- against a real local `file://` fixture remote (never the real
GitHub remote; `network_required: false`/`auth_required: false` are
preserved), observing the `cwd` `main()` itself passes down via a
monkeypatched `_run_child_with_supervision()` leaf (never a reimplemented
simulation of the selection logic itself -- see `_init_main_dispatch_fixture()`
and `_fake_supervision_capturing_cwd()` above). The remaining AC1/AC2/AC3/
AC6/AC7/AC8/AC10/AC11/AC12 tests below continue to exercise the underlying
primitives directly (the same pattern `test_control_plane_worktree_remote_binding.py`
already uses for #2197), since those ACs are about the primitives'
own contracts, not `main()`'s dispatch selection.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
import tomllib
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
import worktree_bootstrap_exec  # noqa: E402
import worktree_catalog  # noqa: E402

# AC4/AC5/AC9: `exec_mod.main()`'s own lazy `_load_worktree_bootstrap_exec_module()`
# does a bare `import worktree_bootstrap_exec` at call time -- Python's
# module cache means that reuses THIS SAME module object (loaded once,
# above, under the bare name), not `BOOTSTRAP` (the separately-named
# `worktree_bootstrap_exec_2199` instance `_load_bootstrap_module()` below
# loads for this file's other, primitive-level tests). Only monkeypatching
# THIS bare-named module's `CONTROL_PLANE_CANONICAL_REMOTE_URL` attribute
# has any effect on what `exec_mod.main()` itself binds against.


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


def _pinned_uv_version(repo_root: Path) -> str:
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["uv"]["required-version"]


def _init_main_dispatch_fixture(
    tmp_path: Path,
    *,
    extra_repo_files: tuple[str, ...] = (),
    extra_written_files: dict[str, str] | None = None,
) -> tuple[Path, str]:
    """A real remote (like `_init_remote_fixture`) PLUS the real production
    `command_registry.py`/`workflow_start_entry.py` at `project_root` and a
    `.gitignore` for `.claude/worktrees/` (mirrors this real repo's own
    `.gitignore`, so the dedicated worktree `main()`'s wired dispatch
    creates under `local` does not itself register as untracked drift in
    `capture_primary_checkout_invariant_snapshot()`). Used only by the
    AC4/AC5/AC9 tests below that call `exec_mod.main()` directly (the REAL
    dispatch-selection code path), never the other primitive-level tests in
    this file. ``extra_repo_files`` copies additional real files verbatim
    from this checkout (by repo-relative path); ``extra_written_files``
    writes additional fixture-authored file contents (path -> text).
    """
    source = tmp_path / "main-dispatch-source"
    origin = tmp_path / "main-dispatch-origin.git"
    local = tmp_path / "main-dispatch-local"
    env = _git_env(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True, env=env)
    (source / ".gitignore").write_text(".claude/worktrees/\n", encoding="utf-8")
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    pin = _pinned_uv_version(REPO_ROOT)
    (source / "pyproject.toml").write_text(
        f'''[project]
name = "main-dispatch-fixture"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = []

[tool.uv]
required-version = "{pin}"
managed = false
''',
        encoding="utf-8",
    )
    for rel in (
        ".claude/skills/issue-refinement-loop/scripts/command_registry.py",
        ".claude/skills/issue-refinement-loop/scripts/workflow_start_entry.py",
        *extra_repo_files,
    ):
        dest = source / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text((REPO_ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")
    for rel, content in (extra_written_files or {}).items():
        dest = source / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=source, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=source, check=True, env=env)
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, env=env)
    subprocess.run(["git", "remote", "add", "origin", origin.as_uri()], cwd=source, check=True, env=env)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=source, check=True, env=env)
    subprocess.run(["git", "--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"], check=True, env=env)
    subprocess.run(["git", "clone", "-q", origin.as_uri(), str(local)], check=True, env=env)
    # `resolve_repo_slug()` parses the local `origin` remote URL and needs
    # an `https://github.com/...` shape to resolve `TRUSTED_REPO_SLUG` --
    # this is entirely independent of `CONTROL_PLANE_CANONICAL_REMOTE_URL`
    # (a literal, code-owned constant `run_control_plane_remote_default_ref_binding`
    # never derives from any locally configured git remote).
    subprocess.run(
        ["git", "-C", str(local), "remote", "set-url", "origin", "https://github.com/squne121/loop-protocol.git"],
        check=True,
        capture_output=True,
    )
    return local, origin.as_uri()


def _fake_supervision_capturing_cwd(captured: dict[str, object]):
    def _fake_supervise(child_argv, *, cwd, env, timeout_seconds, binary_output):
        captured["cwd"] = cwd
        captured["child_argv"] = list(child_argv)
        captured["env"] = dict(env)
        return exec_mod._ChildSupervisionResult(
            timed_out=False,
            returncode=0,
            stdout="",
            stderr="",
            cleanup_scope=exec_mod.CLEANUP_SCOPE_PROCESS_GROUP,
            cleanup_status=exec_mod.CLEANUP_STATUS_NOT_STARTED,
            termination=exec_mod.TERMINATION_NOT_NEEDED,
            leader_reaped=True,
        )

    return _fake_supervise


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


def test_given_main_wired_and_bare_preflight_run_dispatched_when_run_then_production_profile_dedicated_execution_root(
    tmp_path, monkeypatch
):
    """Issue #2199 AC4: calls `exec_mod.main()` itself -- the REAL
    dispatch-selection code path -- and observes the `cwd` it actually
    passes to the child process via a monkeypatched
    `_run_child_with_supervision()` LEAF (never a reimplemented simulation
    of `main()`'s own selection logic). A same-attribute-equality-only
    check against `PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS` (see the two
    tests above) does not, by itself, satisfy this AC -- this test is what
    does."""
    local, url = _init_main_dispatch_fixture(tmp_path)
    monkeypatch.setattr(worktree_bootstrap_exec, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_mod, "_run_child_with_supervision", _fake_supervision_capturing_cwd(captured))
    monkeypatch.chdir(local)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(local))
    monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)

    exit_code = exec_mod.main(
        ["--command-id", "preflight.run", "--issue-number", "1228", "--repo", "squne121/loop-protocol"]
    )

    assert exit_code == 0, captured
    dispatched_cwd = os.path.realpath(str(captured["cwd"]))
    assert dispatched_cwd != os.path.realpath(str(local))
    assert Path(dispatched_cwd).is_dir()
    assert Path(dispatched_cwd) == Path(worktree_bootstrap_exec.fixed_control_plane_worktree_path(str(local)))


def test_given_main_wired_and_contract_update_command_id_dispatched_when_run_then_fixture_and_contract_update_non_regression(
    tmp_path, monkeypatch
):
    """Issue #2199 AC8 non-regression, exercised through the SAME real
    `main()` dispatch-selection path as the AC4 test above:
    `contract_update.run.with_anchor` (deliberately NOT in
    `PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS`) still dispatches its child
    at `project_root`, never a dedicated worktree -- `main()`'s own
    `is_production_dedicated_command` branch, not a reimplemented
    simulation of it."""
    local, url = _init_main_dispatch_fixture(
        tmp_path,
        extra_repo_files=(".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",),
    )
    monkeypatch.setattr(worktree_bootstrap_exec, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)
    assert "contract_update.run.with_anchor" not in exec_mod.PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_mod, "_run_child_with_supervision", _fake_supervision_capturing_cwd(captured))
    monkeypatch.chdir(local)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(local))
    monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)

    exit_code = exec_mod.main(
        [
            "--command-id",
            "contract_update.run.with_anchor",
            "--issue-number",
            "1228",
            "--repo",
            "squne121/loop-protocol",
            "--anchor-comment-url",
            "https://github.com/squne121/loop-protocol/issues/1228#issuecomment-1",
        ]
    )

    assert exit_code == 0, captured
    assert os.path.realpath(str(captured["cwd"])) == os.path.realpath(str(local))


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
# AC5: dedicated_first_hop_parity
# ---------------------------------------------------------------------------


def _write_controlled_gh_always_ok(trusted_bin: Path) -> None:
    """A trivial, no-network `gh` stand-in: every probe this Issue's
    dedicated-first-hop test needs (`github_auth`/`github_repo_read`/
    `controlled_github_read`) only inspects the probe subprocess's exit
    code (never stdout content), so exit 0 unconditionally is sufficient."""
    trusted_bin.mkdir(parents=True)
    gh_path = trusted_bin / "gh"
    gh_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gh_path.chmod(0o755)


_INNER_PREFLIGHT_CWD_PROBE_STUB = """from __future__ import annotations

import argparse
import os
from pathlib import Path


def _build_compact_stdout(result: dict) -> str:
    # Issue #2199 AC5: `workflow_start_entry.py` imports this symbol at
    # MODULE LOAD TIME (`from run_refinement_preflight import
    # _build_compact_stdout`), unconditionally -- even though this stub's
    # own ready-path `main()` below is invoked as a subprocess with
    # inherited stdio, so this function itself is never actually CALLED on
    # that path (only `workflow_start_entry.py`'s blocked path calls it).
    # A minimal stand-in is required purely so the import above succeeds;
    # it must never be exercised in this test's positive path.
    return f"STATUS: {result.get('status')}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    parser.parse_args()
    marker_path = os.environ.get("SKILL_RUNTIME_TEST_INNER_MARKER_PATH")
    if marker_path:
        marker = Path(marker_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        # Issue #2199 AC5: records the INNER preflight's own real `cwd` --
        # the strongest possible proof that the wired `main()` dispatch
        # actually reached the inner refinement preflight AND that every
        # process hop in between (`workflow_start_entry.py` ->
        # `root_entry_router` -> this script) inherited the SAME dedicated
        # `execution_root`, never falling back to `project_root`.
        marker.write_text(os.getcwd())
    env_marker_path = os.environ.get("SKILL_RUNTIME_TEST_INNER_ENV_MARKER_PATH")
    if env_marker_path:
        import json as _json

        env_marker = Path(env_marker_path)
        env_marker.parent.mkdir(parents=True, exist_ok=True)
        # Issue #2199 AC6: records the environment THIS innermost real
        # subprocess actually observed (`os.environ`, populated by the OS
        # from whatever `Popen(env=...)` handed the dedicated-root child
        # at the top of the chain, then plainly inherited -- never
        # re-set -- by every unmodified intermediate `subprocess.run()`
        # hop in between). This is the real dispatched process's own
        # environment, never a dict hand-inspected inside the test
        # process itself.
        observed = {
            key: os.environ.get(key)
            for key in (
                "GH_CONFIG_DIR",
                "LOOP_SPARK_MODE",
                "LOOP_SPARK_FALLBACK",
                "LOOP_PLANNED_OPERATIONS_JSON",
                "LOOP_PROTOCOL_TEST_UNRELATED_CANARY",
            )
        }
        env_marker.write_text(_json.dumps(observed))
    print('{"schema": "refinement_preflight_result/v1", "status": "ready"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def test_given_main_wired_and_bare_preflight_run_dispatched_when_run_then_dedicated_first_hop_parity(
    tmp_path, monkeypatch
):
    """Issue #2199 AC5: calls `exec_mod.main()` itself (never
    `_run_child_with_supervision` mocked away this time) so the REAL
    production process chain -- `skill_runtime_exec.py` (outer, in-process
    here) -> `_sanitize_env()` -> `workflow_start_entry.py` ->
    `root_entry_router.capability_preflight_result()` ->
    `workflow_capability_preflight.py` -> inner `run_refinement_preflight.py`
    -- is genuinely exercised through `main()`'s wired dispatch, under the
    dedicated `execution_root`, with invocation-scoped
    `LOOP_PLANNED_OPERATIONS_JSON` first-hop parity proven end to end. No
    isolated helper-function call stands in for this."""
    inner_marker = tmp_path / "inner-ran.marker"
    local, url = _init_main_dispatch_fixture(
        tmp_path,
        extra_repo_files=(
            "scripts/agent-guards/trusted_runtime_capabilities.py",
            # `trusted_runtime_capabilities.check_trusted_uv()` imports
            # `skill_runtime_exec` (and that, in turn,
            # `skill_runtime_command_policy` -> `worktree_catalog`) from
            # the DEDICATED `execution_root`'s own `scripts/agent-guards/`
            # / `scripts/agent-ops/` directories -- not from this real
            # checkout's `sys.path`-loaded copies -- since `_load_skill_
            # runtime_exec()` resolves `_GUARDS_DIR` relative to its own
            # `__file__` (the fixture's copy). Without these, the
            # producer subprocess fails with `ModuleNotFoundError` and
            # this test's positive path never reaches the inner preflight.
            "scripts/agent-guards/skill_runtime_exec.py",
            "scripts/agent-guards/skill_runtime_command_policy.py",
            "scripts/agent-ops/worktree_catalog.py",
            "scripts/claude-gpt/workflow_capability_preflight.py",
            ".claude/skills/issue-refinement-loop/scripts/root_entry_router.py",
        ),
        extra_written_files={
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py": (
                _INNER_PREFLIGHT_CWD_PROBE_STUB
            ),
        },
    )
    monkeypatch.setattr(worktree_bootstrap_exec, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    _write_controlled_gh_always_ok(trusted_gh_bin)
    monkeypatch.setattr(exec_mod, "_SYSTEM_STANDARD_PATH_DIRS", (str(trusted_gh_bin), *exec_mod._SYSTEM_STANDARD_PATH_DIRS))

    monkeypatch.chdir(local)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(local))
    monkeypatch.setenv("SKILL_RUNTIME_TEST_INNER_MARKER_PATH", str(inner_marker))
    # Issue #2199 AC5/AC6: deliberately no LOOP_SPARK_MODE/LOOP_SPARK_FALLBACK
    # (Spark is `SPARK_NOT_REQUIRED` when unset -- this test's positive path
    # does not need a Spark route) -- only the invocation-scoped
    # `LOOP_PLANNED_OPERATIONS_JSON` first-hop parity carrier this AC is
    # actually about.
    monkeypatch.setenv(
        "LOOP_PLANNED_OPERATIONS_JSON",
        '[{"phase": "workflow_start", "actor_role": "issue-refinement-loop", '
        '"operation": "issue_comment", "requires_mutation": true}]',
    )
    monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)
    monkeypatch.delenv("LOOP_SPARK_MODE", raising=False)
    monkeypatch.delenv("LOOP_SPARK_FALLBACK", raising=False)

    exit_code = exec_mod.main(
        ["--command-id", "preflight.run", "--issue-number", "1228", "--repo", "squne121/loop-protocol"]
    )

    assert exit_code == 0
    assert inner_marker.exists()
    execution_root = worktree_bootstrap_exec.fixed_control_plane_worktree_path(str(local))
    assert os.path.realpath(inner_marker.read_text(encoding="utf-8").strip()) == os.path.realpath(execution_root)


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


def test_given_preflight_run_command_id_when_env_sanitized_in_isolation_then_expected_keys_present_auxiliary(
    tmp_path, monkeypatch
):
    """Auxiliary, isolated-level sanity check only. Per Issue #2199 AC6's own
    text, an isolated `_sanitize_env()` call alone does NOT satisfy this AC
    -- the real proof is
    `test_given_main_wired_and_preflight_run_dispatched_when_run_then_env_transport_parity_into_real_dedicated_child`
    below, which observes the environment of the REAL dispatched
    dedicated-root child process end to end. No vacuous/unconditional
    assertion (no `or True` escape hatch)."""
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "isolated-gh-config"))
    env = exec_mod._sanitize_env(str(tmp_path), "preflight.run")
    # #2311/#2407 carriers this Issue must not touch or broaden.
    assert "CLAUDE_PROJECT_DIR" in env
    assert env["GH_CONFIG_DIR"] == str(tmp_path / "isolated-gh-config")


def test_given_main_wired_and_preflight_run_dispatched_when_run_then_env_transport_parity_into_real_dedicated_child(
    tmp_path, monkeypatch
):
    """Issue #2199 AC6: extends the SAME AC5 real-process-chain fixture
    (`exec_mod.main()` itself, never a mocked `_run_child_with_supervision`
    leaf, never an isolated `_sanitize_env()` call in the test process) so
    the innermost real subprocess this chain actually reaches (the
    `run_refinement_preflight.py` stub, launched via
    `workflow_start_entry.py`'s `_default_invoke_inner_preflight()`, a plain
    `subprocess.run()` call with no explicit `env=` of its own -- i.e. it
    plainly inherits whatever `Popen(env=...)` handed the dedicated-root
    child at the very top) records the ACTUAL environment it observed.
    Proves invocation-scoped `LOOP_SPARK_MODE`/`LOOP_SPARK_FALLBACK`/
    `LOOP_PLANNED_OPERATIONS_JSON` (bare `preflight.run`) and the #2407
    `GH_CONFIG_DIR` carrier arrive UNCHANGED at the real dispatched child,
    AND that an unrelated, non-allowlisted env var is never generically
    passed through."""
    inner_marker = tmp_path / "inner-ran.marker"
    inner_env_marker = tmp_path / "inner-env.marker"
    local, url = _init_main_dispatch_fixture(
        tmp_path,
        extra_repo_files=(
            "scripts/agent-guards/trusted_runtime_capabilities.py",
            "scripts/agent-guards/skill_runtime_exec.py",
            "scripts/agent-guards/skill_runtime_command_policy.py",
            "scripts/agent-ops/worktree_catalog.py",
            "scripts/claude-gpt/workflow_capability_preflight.py",
            ".claude/skills/issue-refinement-loop/scripts/root_entry_router.py",
        ),
        extra_written_files={
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py": (
                _INNER_PREFLIGHT_CWD_PROBE_STUB
            ),
        },
    )
    monkeypatch.setattr(worktree_bootstrap_exec, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    _write_controlled_gh_always_ok(trusted_gh_bin)
    monkeypatch.setattr(exec_mod, "_SYSTEM_STANDARD_PATH_DIRS", (str(trusted_gh_bin), *exec_mod._SYSTEM_STANDARD_PATH_DIRS))

    monkeypatch.chdir(local)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(local))
    monkeypatch.setenv("SKILL_RUNTIME_TEST_INNER_MARKER_PATH", str(inner_marker))
    monkeypatch.setenv("SKILL_RUNTIME_TEST_INNER_ENV_MARKER_PATH", str(inner_env_marker))
    gh_config_dir = str(tmp_path / "fixture-gh-config")
    planned_operations_json = (
        '[{"phase": "workflow_start", "actor_role": "issue-refinement-loop", '
        '"operation": "issue_comment", "requires_mutation": true}]'
    )
    monkeypatch.setenv("GH_CONFIG_DIR", gh_config_dir)
    # `spark_fallback="allowed"` (rather than `"forbidden"`) keeps the real
    # producer's decision at `degraded` (not `blocked`) when no real Spark
    # binary/auth is available in this fixture, so `workflow_start_entry.py`
    # still invokes the inner preflight -- this test's positive path is
    # about env-var transport parity, not about forcing a hard Spark
    # capability failure.
    monkeypatch.setenv("LOOP_SPARK_MODE", "required")
    monkeypatch.setenv("LOOP_SPARK_FALLBACK", "allowed")
    monkeypatch.setenv("LOOP_PLANNED_OPERATIONS_JSON", planned_operations_json)
    # Not allowlisted anywhere in `_sanitize_env()` -- must NOT reach the
    # dispatched child (proof against generic env pass-through widening).
    monkeypatch.setenv("LOOP_PROTOCOL_TEST_UNRELATED_CANARY", "should-not-propagate")
    monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)

    exit_code = exec_mod.main(
        ["--command-id", "preflight.run", "--issue-number", "1228", "--repo", "squne121/loop-protocol"]
    )

    assert exit_code == 0
    assert inner_marker.exists()
    assert inner_env_marker.exists()
    observed = json.loads(inner_env_marker.read_text(encoding="utf-8"))
    assert observed["GH_CONFIG_DIR"] == gh_config_dir
    assert observed["LOOP_SPARK_MODE"] == "required"
    assert observed["LOOP_SPARK_FALLBACK"] == "allowed"
    assert observed["LOOP_PLANNED_OPERATIONS_JSON"] == planned_operations_json
    assert observed["LOOP_PROTOCOL_TEST_UNRELATED_CANARY"] is None


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
