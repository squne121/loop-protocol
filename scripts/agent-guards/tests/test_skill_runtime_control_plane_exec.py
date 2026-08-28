"""Behavioral regression coverage for Issue #2378 closed Git builders."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import skill_runtime_exec as exec_mod  # noqa: E402
from skill_runtime_command_policy import (  # noqa: E402
    GitSubprocessRewriteRejected,
    make_control_plane_private_ref,
    validate_allowed_remote_ref,
    validate_detached_worktree_path,
    validate_literal_remote_url,
)


@pytest.fixture(autouse=True)
def _reset_git_cache():
    exec_mod._reset_git_subprocess_executable_cache_for_tests()
    yield
    exec_mod._reset_git_subprocess_executable_cache_for_tests()


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
        GIT_TERMINAL_PROMPT="0",
    )
    return env


def _init_remote_fixture(tmp_path: Path) -> tuple[Path, str, str]:
    source = tmp_path / "source"
    origin = tmp_path / "origin.git"
    local = tmp_path / "local"
    env = _git_env()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True, env=env)
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=source, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=source, check=True, env=env)
    oid = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, text=True, capture_output=True).stdout.strip()
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, env=env)
    subprocess.run(["git", "remote", "add", "origin", origin.as_uri()], cwd=source, check=True, env=env)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=source, check=True, env=env)
    subprocess.run(["git", "--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"], check=True, env=env)
    subprocess.run(["git", "clone", "-q", origin.as_uri(), str(local)], check=True, env=env)
    return local, origin.as_uri(), oid


def _deadline() -> exec_mod.GitProtocolDeadline:
    return exec_mod.GitProtocolDeadline.start(20, cleanup_reserve_seconds=1)


def test_closed_surface_has_no_generic_or_raw_argv_authority():
    forbidden = (
        "run_control_plane_git_ls_remote",
        "run_control_plane_git_fetch",
        "run_control_plane_git_cat_file",
        "run_control_plane_git_update_ref",
        "run_control_plane_git_worktree",
        "_run_sanitized_git_subprocess",
    )
    for name in forbidden:
        assert not hasattr(exec_mod, name), name
    for name in (
        "run_control_plane_git_effective_remote_url",
        "run_control_plane_git_observe_default_ref",
        "run_control_plane_git_fetch_default_ref",
        "run_control_plane_git_read_private_ref_oid",
        "run_control_plane_git_add_detached_locked_worktree",
        "run_control_plane_git_delete_private_ref_cas",
    ):
        assert "argv" not in inspect.signature(getattr(exec_mod, name)).parameters
    assert "*args" not in inspect.getsource(exec_mod.run_control_plane_git_add_detached_locked_worktree)


def test_exact_remote_commands_and_fixed_fetch_cas_shapes(monkeypatch, tmp_path):
    url = "file:///tmp/origin.git"
    remote = validate_literal_remote_url(url)
    ref = validate_allowed_remote_ref("refs/heads/main")
    deadline = _deadline()
    calls: list[tuple[str, ...]] = []

    def fake_run(invocation, *, text, **kwargs):
        args = invocation.arguments
        calls.append(args)
        if "config" in args:
            return subprocess.CompletedProcess(list(args), 1, b"" if not text else "", b"" if not text else "")
        if "--get-url" in args:
            return subprocess.CompletedProcess(list(args), 0, url + "\n", "")
        if "--show-object-format" in args:
            return subprocess.CompletedProcess(list(args), 0, "sha1\n", "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(exec_mod, "_run_closed_git_process", fake_run)
    assert exec_mod.run_control_plane_git_effective_remote_url(url, cwd=str(tmp_path), project_root=str(tmp_path), deadline=deadline) == remote
    assert exec_mod.run_control_plane_git_observe_default_ref(remote, cwd=str(tmp_path), project_root=str(tmp_path), deadline=deadline).returncode == 0
    private = exec_mod.run_control_plane_git_fetch_default_ref(remote, ref, "a" * 16, cwd=str(tmp_path), project_root=str(tmp_path), deadline=deadline)
    fmt = exec_mod.run_control_plane_git_repository_object_format(cwd=str(tmp_path), project_root=str(tmp_path), deadline=deadline)
    oid = exec_mod.validate_repository_object_id("a" * 40, fmt)
    exec_mod.run_control_plane_git_delete_private_ref_cas(private, oid, cwd=str(tmp_path), project_root=str(tmp_path), deadline=deadline)
    semantic_calls = [call for call in calls if "config" not in call]
    assert any(call[-3:] == ("ls-remote", "--get-url", url) for call in semantic_calls)
    assert any(call[-5:] == ("ls-remote", "--exit-code", "--symref", url, "HEAD") for call in semantic_calls)
    fetch = next(call for call in semantic_calls if "fetch" in call)
    assert fetch[-6:-1] == ("fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", url)
    assert fetch[-1] == f"refs/heads/main:{private.value}"
    assert next(call for call in semantic_calls if "update-ref" in call)[-4:] == ("update-ref", "-d", private.value, oid.value)


def test_real_fixture_proves_observe_fetch_commit_worktree_head_and_cas(tmp_path):
    local, url, expected_oid = _init_remote_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    deadline = _deadline()
    remote = exec_mod.run_control_plane_git_effective_remote_url(url, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline)
    observed = exec_mod.run_control_plane_git_observe_default_ref(remote, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline)
    assert f"refs/heads/main" in observed.stdout
    fmt = exec_mod.run_control_plane_git_repository_object_format(cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline)
    private = exec_mod.run_control_plane_git_fetch_default_ref(remote, validate_allowed_remote_ref("refs/heads/main"), "b" * 16, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline)
    fetched = exec_mod.run_control_plane_git_read_private_ref_oid(private, fmt, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline)
    assert fetched.value == expected_oid
    exec_mod.run_control_plane_git_require_commit_object(fetched, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline)
    destination = local / ".claude" / "worktrees" / "dedicated"
    path = validate_detached_worktree_path(str(destination), str(local))
    exec_mod.run_control_plane_git_add_detached_locked_worktree(path, fetched, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline)
    assert exec_mod.run_control_plane_git_read_worktree_head(path, fmt, project_root=str(local), scratch_root=str(scratch), deadline=deadline) == fetched
    exec_mod.run_control_plane_git_delete_private_ref_cas(private, fetched, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline)
    assert subprocess.run(["git", "rev-parse", "--verify", private.value], cwd=local, capture_output=True).returncode != 0


def test_rewrite_rejection_happens_before_remote_operation(tmp_path):
    local, url, _ = _init_remote_fixture(tmp_path)
    subprocess.run(["git", "config", "--local", "url.https://evil.example/.insteadOf", url], cwd=local, check=True)
    with pytest.raises(GitSubprocessRewriteRejected):
        exec_mod.run_control_plane_git_effective_remote_url(url, cwd=str(local), project_root=str(local), scratch_root=str(tmp_path / "scratch"), deadline=_deadline())


def test_deadline_reserve_refuses_terminal_spawn(monkeypatch, tmp_path):
    spawned = False

    def unexpected(*args, **kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("terminal process must not spawn")

    monkeypatch.setattr(exec_mod.subprocess, "Popen", unexpected)
    expired = exec_mod.GitProtocolDeadline(time.monotonic() + 0.01, 0.1)
    with pytest.raises(exec_mod.GitProtocolDeadlineExhausted):
        exec_mod.run_control_plane_git_effective_remote_url("file:///tmp/origin.git", cwd=str(tmp_path), project_root=str(tmp_path), deadline=expired)
    assert not spawned


def test_timeout_terminates_and_reaps_dedicated_process_group(monkeypatch, tmp_path):
    fake_git = tmp_path / "fake-git"
    child_pid = tmp_path / "child.pid"
    fake_git.write_text(f'''#!/bin/sh
case "$*" in
  *config*) exit 1 ;;
  *) (sleep 30) & echo $! > "{child_pid}"; sleep 30 ;;
esac
''', encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))
    with pytest.raises(exec_mod.GitProtocolTimeout):
        exec_mod.run_control_plane_git_effective_remote_url("file:///tmp/origin.git", cwd=str(tmp_path), project_root=str(tmp_path), deadline=exec_mod.GitProtocolDeadline.start(0.6, cleanup_reserve_seconds=0.3))
    pid = int(child_pid.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_private_ref_factory_is_not_caller_selected():
    private = make_control_plane_private_ref("c" * 16)
    assert private.value == "refs/loop-protocol/control-plane/default-ref/" + "c" * 16
    with pytest.raises(ValueError):
        make_control_plane_private_ref("refs/heads/main")


def test_sanitized_git_environment_forces_no_lazy_fetch(tmp_path):
    assert exec_mod.sanitized_git_subprocess_env(str(tmp_path))["GIT_NO_LAZY_FETCH"] == "1"



def _write_group_leak_git(path: Path, pid_file: Path) -> None:
    path.write_text(f"""#!/bin/sh
(sleep 30) >/dev/null 2>&1 &
echo $! > "{pid_file}"
exit 2
""", encoding="utf-8")
    path.chmod(0o755)


def test_probe_failure_reaps_its_dedicated_process_group(monkeypatch, tmp_path):
    fake_git = tmp_path / "probe-failure-git"
    child_pid = tmp_path / "probe-child.pid"
    _write_group_leak_git(fake_git, child_pid)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git", cwd=str(tmp_path), project_root=str(tmp_path),
            deadline=exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=1),
        )
    pid = int(child_pid.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_exception_after_spawn_reaps_its_dedicated_process_group(monkeypatch, tmp_path):
    fake_git = tmp_path / "exception-git"
    child_pid = tmp_path / "exception-child.pid"
    _write_group_leak_git(fake_git, child_pid)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))
    real_popen = exec_mod.subprocess.Popen

    class ExplodingPopen:
        def __init__(self, *args, **kwargs):
            self._proc = real_popen(*args, **kwargs)
            self.pid = self._proc.pid

        def communicate(self, **kwargs):
            for _ in range(100):
                if child_pid.exists():
                    break
                time.sleep(0.01)
            raise RuntimeError("injected_post_spawn_failure")

        def __getattr__(self, name):
            return getattr(self._proc, name)

    monkeypatch.setattr(exec_mod.subprocess, "Popen", ExplodingPopen)
    with pytest.raises(RuntimeError, match="injected_post_spawn_failure"):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git", cwd=str(tmp_path), project_root=str(tmp_path),
            deadline=exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=1),
        )
    pid = int(child_pid.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
