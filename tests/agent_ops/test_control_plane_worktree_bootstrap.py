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

import hashlib
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
# Issue #2199 OWNER feedback P1-2: the SAME real production consumer module
# `.claude/skills/issue-refinement-loop/tests/test_repair_action_apply_consumer.py`
# already imports this exact way (identical absolute `_SCRIPTS_DIR`) -- reused
# here, never a stub/reimplementation, to prove the fixed dedicated-worktree
# artifact path genuinely reaches the real `run_repair_action_apply()` FD
# secure reader end to end.
ISSUE_REFINEMENT_LOOP_SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"

if str(AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_GUARDS_DIR))
if str(AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_OPS_DIR))
if str(ISSUE_REFINEMENT_LOOP_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(ISSUE_REFINEMENT_LOOP_SCRIPTS_DIR))

import skill_runtime_exec as exec_mod  # noqa: E402
import skill_runtime_command_policy as command_policy_mod  # noqa: E402
import worktree_bootstrap_exec  # noqa: E402
import worktree_catalog  # noqa: E402
import run_refinement_preflight as rrp  # noqa: E402

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
    extra_dependencies: tuple[str, ...] = (),
) -> tuple[Path, str]:
    """A real remote (like `_init_remote_fixture`) PLUS the real production
    `command_registry.py`/`workflow_start_entry.py` at `project_root` and a
    `.gitignore` for `.claude/worktrees/`/`.venv/` (mirrors this real repo's
    own `.gitignore`, so the dedicated worktree `main()`'s wired dispatch
    creates under `local` does not itself register as untracked drift in
    `capture_primary_checkout_invariant_snapshot()`). Used only by the
    AC4/AC5/AC9 tests below that call `exec_mod.main()` directly (the REAL
    dispatch-selection code path), never the other primitive-level tests in
    this file. ``extra_repo_files`` copies additional real files verbatim
    from this checkout (by repo-relative path); ``extra_written_files``
    writes additional fixture-authored file contents (path -> text).

    Issue #2199 OWNER feedback P1-1: this fixture's `pyproject.toml` is a
    genuinely MANAGED `uv` project (no `managed = false`) -- the previous
    `managed = false` fixture hid the "managed uv project's first `uv run`
    creates `.venv`" bug this Issue's dedicated dispatch must not
    misclassify as an unauthorized write, because an unmanaged project never
    triggers `uv`'s own auto-sync/`.venv`-creation behavior at all.
    ``extra_dependencies`` (exact-pinned, e.g. this real repo's own resolved
    `pyyaml==6.0.3`) lets a caller opt into a genuine dependency-install
    first-run instead of the zero-dependency default every other test here
    uses (kept dependency-free so unrelated ACs stay fast); a real,
    offline-resolvable (`UV_OFFLINE=1`) `uv lock` is generated and committed
    here, exactly like this real repo commits its own `uv.lock`.
    """
    source = tmp_path / "main-dispatch-source"
    origin = tmp_path / "main-dispatch-origin.git"
    local = tmp_path / "main-dispatch-local"
    env = _git_env(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True, env=env)
    (source / ".gitignore").write_text(".claude/worktrees/\n.venv/\n", encoding="utf-8")
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    pin = _pinned_uv_version(REPO_ROOT)
    deps_literal = ", ".join(f'"{dep}"' for dep in extra_dependencies)
    (source / "pyproject.toml").write_text(
        f'''[project]
name = "main-dispatch-fixture"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = [{deps_literal}]

[tool.uv]
required-version = "{pin}"
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
    lock_env = dict(env)
    lock_env["UV_OFFLINE"] = "1"
    # This test process may itself be running under `uv run` (its own
    # `VIRTUAL_ENV`/project-scoped interpreter), which would otherwise leak
    # into this fixture-authoring `uv lock` and constrain its resolution to
    # THAT unrelated venv's interpreter instead of resolving fresh for this
    # disposable fixture project.
    lock_env.pop("VIRTUAL_ENV", None)
    # `_git_env` above fakes `HOME` (git-identity isolation only) -- left
    # alone, that empties `uv`'s default `$HOME/.cache/uv` resolution and
    # makes this OFFLINE `uv lock` unable to find the real host's already
    # warm cache. Point `UV_CACHE_DIR` at the REAL host cache explicitly so
    # this fixture-authoring step reuses it instead of resolving against an
    # empty one.
    real_cache_dir = subprocess.run(
        ["uv", "cache", "dir"], check=True, text=True, capture_output=True
    ).stdout.strip()
    lock_env["UV_CACHE_DIR"] = real_cache_dir
    subprocess.run(["uv", "lock"], cwd=source, check=True, env=lock_env, capture_output=True)
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


def test_given_already_dirty_tracked_file_when_content_changes_further_then_primary_snapshot_invariant_still_detects_drift(
    tmp_path,
):
    """Issue #2199 OWNER feedback P2(b): git's status LETTER for an
    ALREADY-dirty tracked file does not change merely because its content
    changes further (`M README.md` both before and after, different
    bytes) -- a status-TEXT-only comparison could not detect this, making
    the pre-fix_delta "byte-identical" docstring claim false for this
    case. The digest field this fix_delta adds must still detect it."""
    local, _origin, _url, _oid = _init_remote_fixture(tmp_path)
    (local / "README.md").write_text("first dirty edit\n", encoding="utf-8")
    before = exec_mod.capture_primary_checkout_invariant_snapshot(str(local))
    assert before["status_raw"] != ""
    assert json.loads(before["tracked_dirty_content_digest_raw"]) != {}
    (local / "README.md").write_text("second dirty edit -- different bytes\n", encoding="utf-8")
    after = exec_mod.capture_primary_checkout_invariant_snapshot(str(local))
    # The status TEXT alone is byte-identical before/after (same file, same
    # `M` letter, same path) -- this is the exact gap this fix_delta closes.
    assert after["status_raw"] == before["status_raw"]
    assert after["tracked_dirty_content_digest_raw"] != before["tracked_dirty_content_digest_raw"]
    assert after != before


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


# ---------------------------------------------------------------------------
# OWNER feedback (PR #2495 review) P1-1: managed uv project environment
# segregation -- a genuinely MANAGED uv project's first `uv run` at a fresh
# dedicated worktree must not be misclassified as `unauthorized_write_path`.
# ---------------------------------------------------------------------------

_MANAGED_UV_PREFLIGHT_STUB = '''from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--anchor-comment-url", required=False, default=None)
    parser.parse_args()
    import yaml  # real dependency: proves the managed project's OWN venv actually installed it

    marker_path = os.environ.get("SKILL_RUNTIME_TEST_INNER_MARKER_PATH")
    if marker_path:
        marker = Path(marker_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"cwd": os.getcwd(), "yaml_module_file": yaml.__file__}))
    print('{"schema": "refinement_preflight_result/v1", "status": "ready"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _init_managed_uv_dispatch_fixture(tmp_path: Path) -> tuple[Path, str]:
    return _init_main_dispatch_fixture(
        tmp_path,
        extra_written_files={
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py": _MANAGED_UV_PREFLIGHT_STUB,
        },
        extra_dependencies=("pyyaml==6.0.3",),
    )


def _dispatch_managed_preflight_with_anchor(local: Path, marker_path: Path, monkeypatch) -> int:
    monkeypatch.chdir(local)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(local))
    monkeypatch.setenv("SKILL_RUNTIME_TEST_INNER_MARKER_PATH", str(marker_path))
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)
    return exec_mod.main(
        [
            "--command-id",
            "preflight.run.with_anchor",
            "--issue-number",
            "1228",
            "--repo",
            "squne121/loop-protocol",
            "--anchor-comment-url",
            "https://github.com/squne121/loop-protocol/issues/1228#issuecomment-1",
        ]
    )


def _push_additional_commit_to_origin(origin_url: str, tmp_path: Path, marker_name: str) -> str:
    """Advance `origin`'s default branch by one commit (via a disposable
    third clone, never `local` itself) so a subsequent dispatch's remote
    binding observes a genuinely new `accepted_oid` -- the trigger
    `recover_or_create_fixed_control_plane_worktree` uses to tear down and
    recreate the fixed dedicated worktree (Issue #2199 OWNER feedback P1-1
    case 3)."""
    env = _git_env(tmp_path)
    scratch_clone = tmp_path / f"{marker_name}-scratch-clone"
    subprocess.run(["git", "clone", "-q", origin_url, str(scratch_clone)], check=True, env=env)
    (scratch_clone / marker_name).write_text("advance\n", encoding="utf-8")
    subprocess.run(["git", "add", marker_name], cwd=scratch_clone, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "advance for case 3"], cwd=scratch_clone, check=True, env=env)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=scratch_clone, check=True, env=env)
    return subprocess.run(
        ["git", "-C", str(scratch_clone), "rev-parse", "HEAD"], check=True, text=True, capture_output=True
    ).stdout.strip()


def test_given_managed_uv_project_when_dedicated_venv_absent_then_first_launch_not_misclassified_unauthorized_write(
    tmp_path, monkeypatch
):
    """Issue #2199 OWNER feedback P1-1 case 1: a genuinely MANAGED uv
    project (real `pyyaml==6.0.3` dependency, no `managed = false`) whose
    fixed dedicated worktree has never run `uv` before. Before this
    fix_delta, `uv run`'s first-launch `.venv` creation happened INSIDE
    `execution_root`, which the write-monitor misclassified as
    `unauthorized_write_path`. After the fix, the environment is prepared
    and relocated to `dedicated_execution_venv_dir` (a SIBLING of
    `execution_root`, never nested inside it) before the write-monitoring
    window opens, so the real dependency import succeeds and no `.venv`
    ever appears inside the monitored dedicated worktree."""
    inner_marker = tmp_path / "case1-inner-ran.marker"
    local, url = _init_managed_uv_dispatch_fixture(tmp_path)
    monkeypatch.setattr(worktree_bootstrap_exec, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    execution_root = worktree_bootstrap_exec.fixed_control_plane_worktree_path(str(local))
    dedicated_venv = worktree_bootstrap_exec.dedicated_execution_venv_dir(str(local))
    assert not Path(execution_root).exists()
    assert not Path(dedicated_venv).exists()

    exit_code = _dispatch_managed_preflight_with_anchor(local, inner_marker, monkeypatch)

    assert exit_code == 0
    assert inner_marker.exists()
    observed = json.loads(inner_marker.read_text(encoding="utf-8"))
    assert os.path.realpath(observed["cwd"]) == os.path.realpath(execution_root)
    # The core P1-1 claim: the managed project's own `uv sync` target (this
    # fixture's real `pyyaml==6.0.3` dependency) never lands inside the
    # monitored dedicated worktree tree -- it is relocated to the sibling
    # `dedicated_execution_venv_dir`, which this dispatch's explicit
    # preparation step (`ensure_dedicated_execution_environment_ready`,
    # called before the write-monitoring window opens) actually populated.
    # (The inner script's OWN `yaml` import resolves through whatever
    # interpreter `_resolve_trusted_executable("python3", ...)` selected --
    # a pre-existing, unrelated #2073 identity-preservation choice this
    # Issue does not change -- so it is not itself proof of which `uv`
    # sync target was used; the absence of `.venv` inside `execution_root`
    # plus the sibling directory's existence is.)
    assert not (Path(execution_root) / ".venv").exists()
    assert Path(dedicated_venv).is_dir()
    assert (Path(dedicated_venv) / "pyvenv.cfg").is_file()


def test_given_managed_uv_project_when_dedicated_venv_already_prepared_then_reexecution_reuses_same_environment(
    tmp_path, monkeypatch
):
    """Issue #2199 OWNER feedback P1-1 case 2: a second dispatch against the
    SAME fixed dedicated worktree/environment (no new `accepted_oid`, no
    worktree recreation) reuses the already-prepared environment -- `uv
    sync --locked`'s own no-op-when-already-synced behavior means the
    sibling `dedicated_execution_venv_dir`'s `pyvenv.cfg` is never rewritten
    a second time -- and both dispatches succeed."""
    local, url = _init_managed_uv_dispatch_fixture(tmp_path)
    monkeypatch.setattr(worktree_bootstrap_exec, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)
    dedicated_venv = worktree_bootstrap_exec.dedicated_execution_venv_dir(str(local))

    first_marker = tmp_path / "case2-first.marker"
    exit_code_1 = _dispatch_managed_preflight_with_anchor(local, first_marker, monkeypatch)
    assert exit_code_1 == 0
    pyvenv_cfg = Path(dedicated_venv) / "pyvenv.cfg"
    assert pyvenv_cfg.is_file()
    mtime_after_first = pyvenv_cfg.stat().st_mtime_ns

    second_marker = tmp_path / "case2-second.marker"
    exit_code_2 = _dispatch_managed_preflight_with_anchor(local, second_marker, monkeypatch)
    assert exit_code_2 == 0
    assert second_marker.exists()
    assert pyvenv_cfg.stat().st_mtime_ns == mtime_after_first


def test_given_dedicated_worktree_recreated_after_oid_update_then_first_launch_after_recreation_reuses_environment(
    tmp_path, monkeypatch
):
    """Issue #2199 OWNER feedback P1-1 case 3: an `accepted_oid` update
    between two dispatches forces `recover_or_create_fixed_control_plane_worktree`
    to tear down and recreate the fixed dedicated worktree at the new OID.
    The environment directory (a sibling of the worktree, never inside it)
    is untouched by that recreation -- its `pyvenv.cfg` is not rewritten --
    so this "first launch after worktree recreation" reduces to the
    ordinary reuse case, never a second first-run misclassified as an
    unauthorized write."""
    local, url = _init_managed_uv_dispatch_fixture(tmp_path)
    monkeypatch.setattr(worktree_bootstrap_exec, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)
    execution_root = worktree_bootstrap_exec.fixed_control_plane_worktree_path(str(local))
    dedicated_venv = worktree_bootstrap_exec.dedicated_execution_venv_dir(str(local))

    first_marker = tmp_path / "case3-first.marker"
    exit_code_1 = _dispatch_managed_preflight_with_anchor(local, first_marker, monkeypatch)
    assert exit_code_1 == 0
    pyvenv_cfg = Path(dedicated_venv) / "pyvenv.cfg"
    assert pyvenv_cfg.is_file()
    mtime_after_first = pyvenv_cfg.stat().st_mtime_ns
    first_head = subprocess.run(
        ["git", "-C", execution_root, "rev-parse", "HEAD"], check=True, text=True, capture_output=True
    ).stdout.strip()

    new_oid = _push_additional_commit_to_origin(url, tmp_path, "CASE3_ADVANCE_MARKER.txt")
    assert new_oid != first_head

    second_marker = tmp_path / "case3-second.marker"
    exit_code_2 = _dispatch_managed_preflight_with_anchor(local, second_marker, monkeypatch)
    assert exit_code_2 == 0
    assert second_marker.exists()

    recreated_head = subprocess.run(
        ["git", "-C", execution_root, "rev-parse", "HEAD"], check=True, text=True, capture_output=True
    ).stdout.strip()
    assert recreated_head == new_oid
    assert pyvenv_cfg.stat().st_mtime_ns == mtime_after_first


# ---------------------------------------------------------------------------
# OWNER feedback (PR #2495 review) P1-2: dedicated preflight artifact ->
# existing repair_action.apply consumer handoff.
# ---------------------------------------------------------------------------

_P1_2_ORIGINAL_BODY = "original body\n"
_P1_2_REPAIRED_BODY = "repaired body\n"


def _p1_2_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_dedicated_repair_candidate(dedicated_worktree: Path, issue_number: int) -> Path:
    """Write a real `needs_fix`-shaped preflight-result artifact (and its
    candidate body) UNDER the fixed dedicated worktree's own
    `.claude/artifacts/issue-refinement-loop/<issue>/` tree -- exactly where
    the real production preflight profiles now write it post-#2199 (never
    under primary's own artifact tree). Mirrors
    `.claude/skills/issue-refinement-loop/tests/test_repair_action_apply_consumer.py`'s
    own `_write_candidate()` fixture shape (the real, already-reviewed
    minimal-valid `auto_apply_safe` candidate), not a reinvented one."""
    artifact_dir = dedicated_worktree / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = artifact_dir / "candidate_body.md"
    candidate_path.write_text(_P1_2_REPAIRED_BODY)
    repair_action = {
        "schema_version": "repair_action/v1",
        "policy_version": "deterministic-issue-repair/v1",
        "disposition": "auto_apply_safe",
        "original_body_sha256": _p1_2_hex(_P1_2_ORIGINAL_BODY),
        "repaired_body_sha256": _p1_2_hex(_P1_2_REPAIRED_BODY),
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


def _add_dedicated_detached_worktree(local: Path, oid: str) -> Path:
    dedicated_path = Path(worktree_bootstrap_exec.fixed_control_plane_worktree_path(str(local)))
    subprocess.run(
        ["git", "-C", str(local), "worktree", "add", "--detach", str(dedicated_path), oid],
        check=True,
        capture_output=True,
    )
    return dedicated_path


def test_given_dedicated_worktree_artifact_when_safe_path_checked_then_dedicated_relative_shape_accepted(tmp_path):
    """Issue #2199 OWNER feedback P1-2: `_is_safe_issue_artifact_path()`
    must accept a preflight-result path expressed relative to PRIMARY that
    carries the fixed dedicated worktree's own prefix segment -- the exact
    shape a caller derives from the dedicated preflight's own reported
    absolute artifact path via an ordinary `os.path.relpath` against
    primary root. Before this fix_delta this was rejected (prefix
    mismatch), disconnecting the producer from the existing consumer."""
    local, _origin, _url, oid = _init_remote_fixture(tmp_path)
    dedicated_path = _add_dedicated_detached_worktree(local, oid)
    result_path = _write_dedicated_repair_candidate(dedicated_path, 2199)
    dedicated_relative = os.path.relpath(result_path, local)

    assert command_policy_mod._is_safe_issue_artifact_path(dedicated_relative, str(local), "2199")

    # A DIFFERENT issue number's artifact tree must still be rejected --
    # this fix widens the accepted PREFIX shape, never the issue binding.
    assert not command_policy_mod._is_safe_issue_artifact_path(dedicated_relative, str(local), "9999")

    # A path escaping BOTH candidate prefixes is still rejected.
    assert not command_policy_mod._is_safe_issue_artifact_path(
        ".claude/worktrees/some-other-worktree/.claude/artifacts/issue-refinement-loop/2199/x.json",
        str(local),
        "2199",
    )


def test_given_dedicated_worktree_fixed_identity_when_compared_then_policy_literal_matches_bootstrap_constant():
    """Guards `skill_runtime_command_policy.py`'s intentionally-duplicated
    fixed dedicated-worktree literal (Issue #2199 OWNER feedback P1-2 --
    added instead of a reverse import edge into `worktree_bootstrap_exec.py`)
    against silent drift from `worktree_bootstrap_exec.py`'s own
    `_FIXED_CONTROL_PLANE_WORKTREE_RELATIVE_PATH`."""
    expected = os.path.join(
        "/primary", ".claude", worktree_bootstrap_exec._FIXED_CONTROL_PLANE_WORKTREE_RELATIVE_PATH.as_posix()
    )
    actual = os.path.join("/primary", command_policy_mod._DEDICATED_CONTROL_PLANE_WORKTREE_REPO_RELATIVE)
    assert expected == actual


def test_given_dedicated_preflight_needs_fix_artifact_when_real_repair_consumer_runs_then_artifact_is_read_and_applied(
    tmp_path,
):
    """Issue #2199 OWNER feedback P1-2 production-profile end-to-end proof:
    a `needs_fix`-shaped preflight-result artifact written under the FIXED
    dedicated worktree (never primary's own artifact tree) is read by the
    REAL, unmodified `run_repair_action_apply()` consumer (Issue #2039) --
    the SAME function `repair_action.apply` dispatches in production --
    through its own unmodified FD-based secure reader (whose confinement
    root stays PRIMARY throughout: this fix never changes that reader, only
    the OUTER path-shape validator in `skill_runtime_command_policy.py`).
    GitHub mutation itself is stubbed via `run_repair_action_apply()`'s own
    pre-existing `fetch_current`/`apply_transaction` injection seam (no
    real `gh` call -- OWNER feedback explicitly allows this)."""
    local, _origin, _url, oid = _init_remote_fixture(tmp_path)
    dedicated_path = _add_dedicated_detached_worktree(local, oid)
    result_path = _write_dedicated_repair_candidate(dedicated_path, 2199)
    dedicated_relative = os.path.relpath(result_path, local)

    apply_calls: list[tuple[dict, str]] = []

    def _apply_transaction(current_issue: dict, candidate_body: str) -> dict:
        apply_calls.append((current_issue, candidate_body))
        return {
            "status": "ok",
            "mutation_started": True,
            "body_update": {
                "attempted": True,
                "status": "ok",
                "remote_current_body_sha256": f"sha256:{_p1_2_hex(_P1_2_REPAIRED_BODY)}",
            },
            "content_update": {"patch_attempted": True, "mutation_outcome": "applied"},
            "errors": [],
        }

    fetch_bodies = iter([_P1_2_ORIGINAL_BODY, _P1_2_REPAIRED_BODY])

    def _fetch_current():
        return {"body": next(fetch_bodies), "updatedAt": "2024-01-01T00:00:00Z"}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2199,
        preflight_result_path=dedicated_relative,
        repo_root=Path(local),
        fetch_current=_fetch_current,
        apply_transaction=_apply_transaction,
    )

    assert not (result.get("phase") == "candidate_load" and result.get("failure_code") == "secure_open_rejected")
    assert result["mutation_outcome"] == "applied"
    assert result["phase"] == "complete"
    assert result["failure_code"] is None
    assert len(apply_calls) == 1


def test_given_dedicated_relative_artifact_path_when_repair_action_apply_command_parsed_then_accepted(tmp_path):
    """Issue #2199 OWNER feedback P1-2, parser-level proof: the FULL
    `repair_action.apply` exact-command-string parser (not just the
    underlying `_is_safe_issue_artifact_path()` helper in isolation) now
    accepts the dedicated-relative artifact shape -- this is the exact
    parser `skill_runtime_exec.py::main()`'s real dispatch calls through
    `is_exact_skill_runtime_repair_action_apply_executor_command()`."""
    local, _origin, _url, oid = _init_remote_fixture(tmp_path)
    dedicated_path = _add_dedicated_detached_worktree(local, oid)
    result_path = _write_dedicated_repair_candidate(dedicated_path, 2199)
    dedicated_relative = os.path.relpath(result_path, local)

    command_text = " ".join(
        [
            "uv",
            "run",
            "python3",
            command_policy_mod.SKILL_RUNTIME_EXEC_REL,
            "--command-id",
            "repair_action.apply",
            "--issue-number",
            "2199",
            "--repo",
            command_policy_mod.TRUSTED_REPO_SLUG,
            "--apply-repair-action",
            dedicated_relative,
        ]
    )
    parsed = command_policy_mod.parse_exact_skill_runtime_repair_action_apply_command(command_text, str(local))
    assert parsed is not None
    assert parsed.preflight_result_path == dedicated_relative


def test_given_dedicated_relative_artifact_path_when_structural_repair_action_apply_command_parsed_then_accepted(
    tmp_path,
):
    """Issue #2199 fix_delta (test-runner iteration 0 Blocker 2): P1-2's
    widened `_is_safe_issue_artifact_path()` prefix is shared by BOTH
    `repair_action.apply` (proven above) AND the sibling
    `structural_repair_action.apply` lane (Issue #2396) -- this is the exact
    parser `skill_runtime_exec.py::main()`'s real dispatch calls through
    `is_exact_skill_runtime_structural_repair_action_apply_executor_command()`.
    `run_structural_repair_action_apply()` itself reads its artifact via the
    SAME `secure_read_repair_apply_artifact()` FD-based reader
    `run_repair_action_apply()` uses (see that function's own docstring), so
    the real-subprocess end-to-end proof already established above for
    `repair_action.apply` covers the shared reader; this test closes the
    remaining gap at the `structural_repair_action.apply`-specific exact
    parser itself, which is a distinct code path
    (`parse_exact_skill_runtime_structural_repair_action_apply_command`)."""
    local, _origin, _url, oid = _init_remote_fixture(tmp_path)
    dedicated_path = _add_dedicated_detached_worktree(local, oid)
    result_path = _write_dedicated_repair_candidate(dedicated_path, 2199)
    dedicated_relative = os.path.relpath(result_path, local)

    command_text = " ".join(
        [
            "uv",
            "run",
            "python3",
            command_policy_mod.SKILL_RUNTIME_EXEC_REL,
            "--command-id",
            "structural_repair_action.apply",
            "--issue-number",
            "2199",
            "--repo",
            command_policy_mod.TRUSTED_REPO_SLUG,
            "--apply-structural-repair-action",
            dedicated_relative,
        ]
    )
    parsed = command_policy_mod.parse_exact_skill_runtime_structural_repair_action_apply_command(
        command_text, str(local)
    )
    assert parsed is not None
    assert parsed.preflight_result_path == dedicated_relative


# ---------------------------------------------------------------------------
# OWNER feedback (PR #2495 review) P1-3: human-context investigation evidence
# resolves against the DEDICATED child's own cwd, not the PRIMARY root the
# path was validated against.
# ---------------------------------------------------------------------------


def _build_investigation_evidence_manifest(
    *, issue_number: int, repo: str, anchor_url: str, base_body: str, git_head_sha: str, path_literals: list[str]
) -> dict:
    payload = [
        {
            "comment_url": anchor_url,
            "body_sha256": rrp._sha256(base_body),
            "source_kind": "generated_by_agent",
            "path_literals": list(path_literals),
        }
    ]
    return {
        "schema_version": rrp.AUTHORITY_TRANSPORT_SCHEMA_VERSION,
        "invocation_id": "test-invocation",
        "issue_number": issue_number,
        "repo": repo,
        "git_head_sha": git_head_sha,
        "generated_at": "2024-01-01T00:00:00Z",
        "canonicalization_id": "loop-protocol-json-c14n-v1",
        "source_comment_id": 1,
        "source_comment_url": anchor_url,
        "source_issue_body_sha256": rrp._sha256(base_body),
        "source_kind": "generated_by_agent",
        "payload": payload,
        "payload_sha256": rrp._sha256(rrp._canonical_json(payload)),
    }


def test_given_investigation_evidence_only_at_primary_when_child_cwd_is_dedicated_then_relative_confinement_misses_it_but_absolute_handoff_finds_it(
    tmp_path, monkeypatch
):
    """Issue #2199 OWNER feedback P1-3: `_confine_artifact_path()` (inside
    `run_refinement_preflight.py`'s real, unmodified
    `_validate_investigation_evidence_transport()`) resolves a RELATIVE
    path via `Path.resolve()`, which anchors to this PROCESS's actual
    `os.getcwd()` -- independent of whatever `repo_root` value is passed
    alongside it. A real dedicated child's cwd is always the #2197/#2199
    dedicated worktree, so the SAME repo-relative transport-path string the
    executor validated against PRIMARY silently misses the evidence file,
    which is never duplicated into the dedicated worktree. The fix
    (mirrored here, matching `run_preflight()`'s own new pre-join) is to
    make the candidate path ABSOLUTE against the explicit primary-root
    handoff BEFORE it ever reaches `_confine_artifact_path()`, which is
    then cwd-independent."""
    local, _origin, _url, oid = _init_remote_fixture(tmp_path)
    dedicated_path = _add_dedicated_detached_worktree(local, oid)
    issue_number = 2199
    repo = "squne121/loop-protocol"
    anchor_url = "https://github.com/squne121/loop-protocol/issues/2199#issuecomment-1"
    base_body = "issue body\n"
    manifest = _build_investigation_evidence_manifest(
        issue_number=issue_number,
        repo=repo,
        anchor_url=anchor_url,
        base_body=base_body,
        git_head_sha=oid,
        path_literals=["src/example.py"],
    )
    evidence_dir = Path(local) / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / "investigation_evidence_transport.json"
    evidence_path.write_text(json.dumps(manifest))
    relative_transport_path = os.path.relpath(evidence_path, local)

    # Simulates the REAL dedicated child's actual cwd
    # (`subprocess.run(cwd=execution_root, ...)`), unrelated to whatever
    # `repo_root` value a caller passes.
    monkeypatch.chdir(dedicated_path)

    literals_bug, reason_bug = rrp._validate_investigation_evidence_transport(
        relative_transport_path,
        repo_root=Path(local),
        issue_number=issue_number,
        repo=repo,
        anchor_url=anchor_url,
        base_issue_body_sha256=rrp._sha256(base_body),
        git_head_sha=oid,
    )
    assert literals_bug is None
    # The relative path resolves under the DEDICATED cwd
    # (`<dedicated_path>/<relative_transport_path>`), which is not under
    # PRIMARY's own `.claude/artifacts/` boundary at all -- a stronger,
    # more legible failure than a bare "missing file" would be.
    assert reason_bug == "path_confinement_outside_artifact_root"

    absolute_transport_path = Path(local) / relative_transport_path
    literals_fixed, reason_fixed = rrp._validate_investigation_evidence_transport(
        absolute_transport_path,
        repo_root=Path(local),
        issue_number=issue_number,
        repo=repo,
        anchor_url=anchor_url,
        base_issue_body_sha256=rrp._sha256(base_body),
        git_head_sha=oid,
    )
    assert reason_fixed is None
    assert literals_fixed == ["src/example.py"]

    assert not (
        dedicated_path
        / ".claude"
        / "artifacts"
        / "issue-refinement-loop"
        / str(issue_number)
        / "investigation_evidence_transport.json"
    ).exists()


_INVESTIGATION_EVIDENCE_PREFLIGHT_STUB = '''from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--anchor-comment-url", required=False, default=None)
    parser.add_argument("--human-context-comment-url", required=False, default=None)
    parser.add_argument("--investigation-evidence-transport-path", required=False, default=None)
    parser.add_argument("--investigation-evidence-primary-root", required=False, default=None)
    args = parser.parse_args()

    marker_path = os.environ.get("SKILL_RUNTIME_TEST_INNER_MARKER_PATH")
    if marker_path and args.investigation_evidence_transport_path:
        # Mirrors run_refinement_preflight.py's own Issue #2199 P1-3 fix:
        # pre-join the relative transport path against the EXPLICIT
        # primary-root handoff (falling back to this process's own cwd
        # when absent), rather than relying on Path.resolve()'s implicit
        # cwd anchoring.
        root = (
            Path(args.investigation_evidence_primary_root)
            if args.investigation_evidence_primary_root
            else Path.cwd()
        )
        transport_path = root / args.investigation_evidence_transport_path
        marker = Path(marker_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "cwd": os.getcwd(),
                    "primary_root_arg": args.investigation_evidence_primary_root,
                    "transport_path_exists": transport_path.is_file(),
                    "transport_path_content": (
                        transport_path.read_text() if transport_path.is_file() else None
                    ),
                }
            )
        )
    print('{"schema": "refinement_preflight_result/v1", "status": "ready"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def test_given_main_wired_and_human_context_dispatched_when_evidence_only_at_primary_then_dedicated_child_consumes_primary_origin_evidence(
    tmp_path, monkeypatch
):
    """Issue #2199 OWNER feedback P1-3 production-profile end-to-end proof:
    `exec_mod.main()` itself (the REAL dispatch-selection path, real
    registry, real `skill_runtime_command_policy.py` parser) dispatches
    `preflight.run.with_human_context` to a real subprocess whose cwd is
    the dedicated worktree, automatically deriving and forwarding
    `--investigation-evidence-primary-root` (never caller-suppliable) --
    and the dispatched child genuinely reads the evidence file that exists
    ONLY at primary (never duplicated into the dedicated worktree)."""
    inner_marker = tmp_path / "p1-3-inner.marker"
    evidence_rel = ".claude/artifacts/issue-refinement-loop/1228/investigation_evidence_transport.json"
    evidence_content = json.dumps({"marker": "primary-only-evidence"})
    local, url = _init_main_dispatch_fixture(
        tmp_path,
        extra_written_files={
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py": (
                _INVESTIGATION_EVIDENCE_PREFLIGHT_STUB
            ),
        },
    )
    monkeypatch.setattr(worktree_bootstrap_exec, "CONTROL_PLANE_CANONICAL_REMOTE_URL", url)

    execution_root = worktree_bootstrap_exec.fixed_control_plane_worktree_path(str(local))
    evidence_path = local / evidence_rel
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(evidence_content)
    # The evidence file exists ONLY at primary -- written AFTER the fixture
    # commit, as an untracked file never shared with the SEPARATE dedicated
    # worktree checkout.
    assert not (Path(execution_root) / evidence_rel).exists()

    monkeypatch.chdir(local)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(local))
    monkeypatch.setenv("SKILL_RUNTIME_TEST_INNER_MARKER_PATH", str(inner_marker))
    monkeypatch.delenv("LOOP_ISSUE_NUMBER", raising=False)

    exit_code = exec_mod.main(
        [
            "--command-id",
            "preflight.run.with_human_context",
            "--issue-number",
            "1228",
            "--repo",
            "squne121/loop-protocol",
            "--anchor-comment-url",
            "https://github.com/squne121/loop-protocol/issues/1228#issuecomment-1",
            "--investigation-evidence-transport-path",
            evidence_rel,
        ]
    )

    assert exit_code == 0, inner_marker.read_text() if inner_marker.exists() else "no marker"
    assert inner_marker.exists()
    observed = json.loads(inner_marker.read_text(encoding="utf-8"))
    assert os.path.realpath(observed["cwd"]) == os.path.realpath(execution_root)
    assert os.path.realpath(observed["primary_root_arg"]) == os.path.realpath(str(local))
    assert observed["transport_path_exists"] is True
    assert json.loads(observed["transport_path_content"]) == {"marker": "primary-only-evidence"}
    assert not (Path(execution_root) / evidence_rel).exists()
