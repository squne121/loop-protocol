"""Behavioral regression coverage for Issue #2378 closed Git builders."""

from __future__ import annotations

import inspect
import os
import signal
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
    oid = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, check=True, text=True, capture_output=True
    ).stdout.strip()
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

    def fake_run(operation, **kwargs):
        text = operation.kind != "probe_rewrite"
        args = tuple(
            exec_mod._exact_git_argv(
                operation,
                git_executable=kwargs["git_executable"],
                cwd=kwargs["cwd"],
                hooks_dir=kwargs["hooks_dir"],
            )
        )
        calls.append(args)
        if "config" in args:
            return subprocess.CompletedProcess(list(args), 1, b"" if not text else "", b"" if not text else "")
        if "--get-url" in args:
            return subprocess.CompletedProcess(list(args), 0, url + "\n", "")
        if "--show-object-format" in args:
            return subprocess.CompletedProcess(list(args), 0, "sha1\n", "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(exec_mod, "_run_closed_git_process", fake_run)
    assert (
        exec_mod.run_control_plane_git_effective_remote_url(
            url, cwd=str(tmp_path), project_root=str(tmp_path), deadline=deadline
        )
        == remote
    )
    assert (
        exec_mod.run_control_plane_git_observe_default_ref(
            remote, cwd=str(tmp_path), project_root=str(tmp_path), deadline=deadline
        ).returncode
        == 0
    )
    private = exec_mod.run_control_plane_git_fetch_default_ref(
        remote, ref, "a" * 16, cwd=str(tmp_path), project_root=str(tmp_path), deadline=deadline
    )
    fmt = exec_mod.run_control_plane_git_repository_object_format(
        cwd=str(tmp_path), project_root=str(tmp_path), deadline=deadline
    )
    oid = exec_mod.validate_repository_object_id("a" * 40, fmt)
    exec_mod.run_control_plane_git_delete_private_ref_cas(
        private, oid, cwd=str(tmp_path), project_root=str(tmp_path), deadline=deadline
    )
    semantic_calls = [call for call in calls if "config" not in call]
    assert any(call[-3:] == ("ls-remote", "--get-url", url) for call in semantic_calls)
    assert any(call[-5:] == ("ls-remote", "--exit-code", "--symref", url, "HEAD") for call in semantic_calls)
    fetch = next(call for call in semantic_calls if "fetch" in call)
    assert fetch[-6:-1] == ("fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", url)
    assert fetch[-1] == f"refs/heads/main:{private.value}"
    assert next(call for call in semantic_calls if "update-ref" in call)[-4:] == (
        "update-ref",
        "-d",
        private.value,
        oid.value,
    )


def test_real_fixture_proves_observe_fetch_commit_worktree_head_and_cas(tmp_path):
    local, url, expected_oid = _init_remote_fixture(tmp_path)
    scratch = tmp_path / "scratch"
    deadline = _deadline()
    remote = exec_mod.run_control_plane_git_effective_remote_url(
        url, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline
    )
    observed = exec_mod.run_control_plane_git_observe_default_ref(
        remote, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline
    )
    assert "refs/heads/main" in observed.stdout
    fmt = exec_mod.run_control_plane_git_repository_object_format(
        cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline
    )
    private = exec_mod.run_control_plane_git_fetch_default_ref(
        remote,
        validate_allowed_remote_ref("refs/heads/main"),
        "b" * 16,
        cwd=str(local),
        project_root=str(local),
        scratch_root=str(scratch),
        deadline=deadline,
    )
    fetched = exec_mod.run_control_plane_git_read_private_ref_oid(
        private, fmt, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline
    )
    assert fetched.value == expected_oid
    exec_mod.run_control_plane_git_require_commit_object(
        fetched, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline
    )
    destination = local / ".claude" / "worktrees" / "dedicated"
    path = validate_detached_worktree_path(str(destination), str(local))
    exec_mod.run_control_plane_git_add_detached_locked_worktree(
        path, fetched, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline
    )
    assert (
        exec_mod.run_control_plane_git_read_worktree_head(
            path, fmt, project_root=str(local), scratch_root=str(scratch), deadline=deadline
        )
        == fetched
    )
    exec_mod.run_control_plane_git_delete_private_ref_cas(
        private, fetched, cwd=str(local), project_root=str(local), scratch_root=str(scratch), deadline=deadline
    )
    assert (
        subprocess.run(["git", "rev-parse", "--verify", private.value], cwd=local, capture_output=True).returncode != 0
    )


def test_rewrite_rejection_happens_before_remote_operation(tmp_path):
    local, url, _ = _init_remote_fixture(tmp_path)
    subprocess.run(["git", "config", "--local", "url.https://evil.example/.insteadOf", url], cwd=local, check=True)
    with pytest.raises(GitSubprocessRewriteRejected):
        exec_mod.run_control_plane_git_effective_remote_url(
            url, cwd=str(local), project_root=str(local), scratch_root=str(tmp_path / "scratch"), deadline=_deadline()
        )


def test_deadline_reserve_refuses_terminal_spawn(monkeypatch, tmp_path):
    spawned = False

    def unexpected(*args, **kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("terminal process must not spawn")

    monkeypatch.setattr(exec_mod.subprocess, "Popen", unexpected)
    expired = exec_mod.GitProtocolDeadline(time.monotonic() + 0.01, 0.1)
    with pytest.raises(exec_mod.GitProtocolDeadlineExhausted):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git", cwd=str(tmp_path), project_root=str(tmp_path), deadline=expired
        )
    assert not spawned


def test_noncontained_platform_fails_closed_before_git_spawn(monkeypatch, tmp_path):
    monkeypatch.setattr(exec_mod, "_enable_linux_child_subreaper", lambda: False)
    monkeypatch.setattr(
        exec_mod.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("uncontained Git command must not spawn"),
    )
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed, match="containment_unavailable"):
        exec_mod._run_closed_git_process(
            exec_mod._GitOperation("repository_object_format"),
            git_executable="/usr/bin/git",
            cwd=str(tmp_path),
            env={},
            hooks_dir=str(tmp_path),
            deadline=_deadline(),
        )


def test_timeout_terminates_and_reaps_dedicated_process_group(monkeypatch, tmp_path):
    fake_git = tmp_path / "fake-git"
    child_pid = tmp_path / "child.pid"
    fake_git.write_text(
        f'''#!/bin/sh
case "$*" in
  *config*) exit 1 ;;
  *) (sleep 30) & echo $! > "{child_pid}"; sleep 30 ;;
esac
''',
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git",
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            deadline=exec_mod.GitProtocolDeadline.start(0.6, cleanup_reserve_seconds=0.3),
        )
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
    path.write_text(
        f"""#!/bin/sh
(sleep 30) >/dev/null 2>&1 &
echo $! > "{pid_file}"
exit 2
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_probe_failure_reaps_its_dedicated_process_group(monkeypatch, tmp_path):
    fake_git = tmp_path / "probe-failure-git"
    child_pid = tmp_path / "probe-child.pid"
    _write_group_leak_git(fake_git, child_pid)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git",
            cwd=str(tmp_path),
            project_root=str(tmp_path),
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
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed, match="cleanup_unconfirmed"):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git",
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            deadline=exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=1),
        )
    pid = int(child_pid.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_operation_runner_accepts_no_raw_tuple_or_list_argv_authority():
    for name in ("_run_closed_git_process", "_execute_semantic_git"):
        signature = inspect.signature(getattr(exec_mod, name))
        assert "argv" not in signature.parameters
        assert "arguments" not in signature.parameters
    source = inspect.getsource(exec_mod)
    assert "class _ClosedGitInvocation" not in source
    assert "def _closed_git_invocation" not in source


def test_forged_typed_payloads_and_global_option_injection_fail_before_spawn(monkeypatch, tmp_path):
    def unexpected_popen(*args, **kwargs):
        raise AssertionError("forged payload must fail before spawn")

    monkeypatch.setattr(exec_mod.subprocess, "Popen", unexpected_popen)
    deadline = _deadline()
    invalid_calls = (
        lambda: exec_mod.run_control_plane_git_observe_default_ref(
            exec_mod.LiteralRemoteUrl("--config-env=core.hooksPath=/tmp/evil"),
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            deadline=deadline,
        ),
        lambda: exec_mod.run_control_plane_git_fetch_default_ref(
            validate_literal_remote_url("file:///tmp/origin.git"),
            exec_mod.AllowedRemoteRef("refs/tags/v1"),
            "a" * 16,
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            deadline=deadline,
        ),
        lambda: exec_mod.run_control_plane_git_read_private_ref_oid(
            exec_mod.ControlPlanePrivateRef("refs/heads/main"),
            exec_mod.RepositoryObjectFormat("sha1"),
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            deadline=deadline,
        ),
        lambda: exec_mod.run_control_plane_git_require_commit_object(
            exec_mod.RepositoryObjectId("g" * 40),
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            deadline=deadline,
        ),
        lambda: exec_mod.run_control_plane_git_read_worktree_head(
            exec_mod.DetachedWorktreePath(str(tmp_path / "outside")),
            exec_mod.RepositoryObjectFormat("sha1"),
            project_root=str(tmp_path),
            deadline=deadline,
        ),
    )
    for call in invalid_calls:
        with pytest.raises((TypeError, ValueError)):
            call()


@pytest.mark.parametrize(
    "deadline",
    (
        exec_mod.GitProtocolDeadline(float("nan"), 1),
        exec_mod.GitProtocolDeadline(float("inf"), 1),
        exec_mod.GitProtocolDeadline(1, float("nan")),
        exec_mod.GitProtocolDeadline(1, float("inf")),
        exec_mod.GitProtocolDeadline(1, 0),
    ),
)
def test_forged_nonfinite_or_nonpositive_deadline_fails_before_spawn(monkeypatch, tmp_path, deadline):
    monkeypatch.setattr(
        exec_mod.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("invalid deadline must fail before spawn"),
    )
    with pytest.raises(ValueError):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git", cwd=str(tmp_path), project_root=str(tmp_path), deadline=deadline
        )


@pytest.mark.parametrize("value", (float("nan"), float("inf"), 0, -1))
def test_deadline_start_rejects_nonfinite_or_nonpositive_values(value):
    with pytest.raises(ValueError):
        exec_mod.GitProtocolDeadline.start(value)
    with pytest.raises(ValueError):
        exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=value)


def test_base_exception_with_unconfirmed_cleanup_fails_closed(monkeypatch, tmp_path):
    class ExplodingPopen:
        pid = 12345

        def communicate(self, **kwargs):
            raise RuntimeError("injected_post_spawn_failure")

    monkeypatch.setattr(exec_mod.subprocess, "Popen", lambda *args, **kwargs: ExplodingPopen())
    monkeypatch.setattr(exec_mod, "_terminate_git_process_group", lambda *args, **kwargs: False)
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed) as excinfo:
        exec_mod._run_closed_git_process(
            exec_mod._GitOperation("repository_object_format"),
            git_executable="/usr/bin/git",
            cwd=str(tmp_path),
            env={},
            hooks_dir=str(tmp_path),
            deadline=_deadline(),
        )
    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_pushinsteadof_rejection_is_preserved_by_typed_runner(tmp_path):
    local, url, _ = _init_remote_fixture(tmp_path)
    subprocess.run(
        ["git", "config", "--local", "url.https://evil.example/.pushInsteadOf", url],
        cwd=local,
        check=True,
    )
    with pytest.raises(GitSubprocessRewriteRejected):
        exec_mod.run_control_plane_git_effective_remote_url(
            url,
            cwd=str(local),
            project_root=str(local),
            scratch_root=str(tmp_path / "scratch"),
            deadline=_deadline(),
        )


def test_typed_runner_preserves_sanitized_hooks_credentials_askpass_and_path(monkeypatch, tmp_path):
    fake_git = tmp_path / "fake-git"
    capture = tmp_path / "capture"
    arguments_capture = tmp_path / "arguments"
    url = "file:///tmp/origin.git"
    fake_git.write_text(
        f"""#!/bin/sh
case "$*" in
  *config*) exit 1 ;;
esac
printf '%s\n' "$*" > "{arguments_capture}"
printf '%s\n' "$GIT_TERMINAL_PROMPT" > "{capture}"
printf '%s\n' "$GIT_ASKPASS" >> "{capture}"
printf '%s\n' "$SSH_ASKPASS" >> "{capture}"
printf '%s\n' "$GIT_NO_LAZY_FETCH" >> "{capture}"
printf '%s\n' "${{GIT_EXEC_PATH-unset}}" >> "{capture}"
printf '%s\n' "$PATH" >> "{capture}"
printf '%s\n' "{url}"
""",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    malicious = tmp_path / "malicious"
    malicious.mkdir()
    monkeypatch.setenv("PATH", f"{malicious}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GIT_EXEC_PATH", str(malicious))
    monkeypatch.setenv("GIT_ASKPASS", str(malicious / "askpass"))
    monkeypatch.setenv("SSH_ASKPASS", str(malicious / "askpass"))
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))
    assert (
        exec_mod.run_control_plane_git_effective_remote_url(
            url, cwd=str(tmp_path), project_root=str(tmp_path), deadline=_deadline()
        ).value
        == url
    )
    prompt, askpass, ssh_askpass, no_lazy, git_exec_path, child_path = capture.read_text(encoding="utf-8").splitlines()
    assert (prompt, askpass, ssh_askpass, no_lazy, git_exec_path) == ("0", "", "", "1", "unset")
    assert str(malicious) not in child_path
    command_text = arguments_capture.read_text(encoding="utf-8")
    assert "core.hooksPath=" in command_text
    assert "credential.helper=" in command_text
    assert "--no-replace-objects" in command_text


def test_typed_worktree_builder_suppresses_repository_hook(tmp_path):
    local, _, expected_oid = _init_remote_fixture(tmp_path)
    marker = tmp_path / "post-checkout.marker"
    hook = local / ".git" / "hooks" / "post-checkout"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n', encoding="utf-8")
    hook.chmod(0o755)
    deadline = _deadline()
    object_format = exec_mod.run_control_plane_git_repository_object_format(
        cwd=str(local), project_root=str(local), deadline=deadline
    )
    commit = exec_mod.validate_repository_object_id(expected_oid, object_format)
    path = validate_detached_worktree_path(str(local / ".claude" / "worktrees" / "hook-check"), str(local))
    exec_mod.run_control_plane_git_add_detached_locked_worktree(
        path, commit, cwd=str(local), project_root=str(local), deadline=deadline
    )
    assert not marker.exists(), "repository hook fired despite fixed empty hooksPath"



def test_internal_worktree_operation_revalidates_forged_paths_before_any_argv(monkeypatch, tmp_path):
    def unexpected_popen(*args, **kwargs):
        raise AssertionError("forged worktree path must not reach argv")

    monkeypatch.setattr(exec_mod.subprocess, "Popen", unexpected_popen)
    deadline = _deadline()
    commit = exec_mod.RepositoryObjectId("a" * 40)
    for path in (
        exec_mod.DetachedWorktreePath("--force"),
        exec_mod.DetachedWorktreePath(str(tmp_path.parent / "unconfined-worktree")),
    ):
        operation = exec_mod._GitOperation(
            "add_detached_locked_worktree", worktree_path=path, object_id=commit
        )
        with pytest.raises((TypeError, ValueError)):
            exec_mod._execute_semantic_git(
                operation,
                cwd=str(tmp_path),
                project_root=str(tmp_path),
                scratch_root=str(tmp_path / "scratch"),
                deadline=deadline,
            )


def _write_delayed_setsid_escape_git(path: Path, pid_file: Path, trace_file: Path, *, probe_fails: bool) -> None:
    """Create a fixture that defeats a point-in-time `/proc` tree snapshot.

    The `setsid` helper runs independently of the Git process group and does
    not publish its PID until after a delay. An unreadable `/proc` snapshot
    therefore leaves the executor unable to distinguish this live escape from
    absence, which must fail closed.
    """
    escape_script = path.parent / f"{path.name}.delayed-escape"
    escape_script.write_text(
        f"#!/bin/sh\nsleep 0.35\necho $$ > \"{pid_file}\"\nsleep 30\n",
        encoding="utf-8",
    )
    escape_script.chmod(0o755)
    delayed_escape = (
        f"printf 'setsid-parent-started\\n' > \"{trace_file}\"; "
        f"setsid \"{escape_script}\" >/dev/null 2>&1 & sleep 0.08"
    )
    path.write_text(
        f'''#!/bin/sh
case "$*" in
  *config*) {delayed_escape}; {'exit 2' if probe_fails else 'exit 1'} ;;
  *) {delayed_escape}; sleep 30 ;;
esac
''',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _assert_delayed_escape_trace_and_reap(pid_file: Path, trace_file: Path) -> None:
    """Prove the delayed escape occurred, then keep the test fixture clean."""
    deadline = time.monotonic() + 2
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert trace_file.read_text(encoding="utf-8") == "setsid-parent-started\n"
    assert pid_file.exists(), "delayed setsid descendant did not materialize"
    pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        # `setsid` makes this fixture PID its own process-group leader.  Kill
        # the complete fixture group and reap the adopted leader so the test
        # process's Linux subreaper role cannot retain a zombie.
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_successful_leader_with_readable_empty_snapshot_and_delayed_setsid_child_fails_closed(monkeypatch, tmp_path):
    # A leader-rooted empty snapshot cannot certify normal-success cleanup.
    # The fixture deliberately makes the legacy `/proc` observer return a
    # readable empty set. Its real `setsid` child is started before the leader
    # exits successfully and remains alive afterward. Linux subreaper
    # parentage, rather than a sleep or another snapshot count, makes that
    # child directly observable and permits cleanup before fail-closed return.
    fake_git = tmp_path / "setsid-success-git"
    child_pid = tmp_path / "setsid-success-child.pid"
    trace = tmp_path / "setsid-success.trace"
    fake_git.write_text(
        f'''#!/bin/sh
case "$*" in
  *config*) exit 1 ;;
  *)
    (sleep 0.02; setsid sh -c 'echo $$ > "{child_pid}"; sleep 30') >/dev/null 2>&1 &
    sleep 0.10
    echo leader-success > "{trace}"
    exit 0 ;;
esac
''',
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))
    # This models the review finding: `/proc` is readable but the old
    # leader-rooted observation races and sees no descendant.
    monkeypatch.setattr(exec_mod, "_observe_git_descendants", lambda _: set())
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed, match="descendant_leak"):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git",
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            deadline=exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=1),
        )
    assert trace.read_text(encoding="utf-8") == "leader-success\n"
    pid = int(child_pid.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_timeout_delayed_setsid_escape_fails_closed_not_snapshot_success(monkeypatch, tmp_path):
    fake_git = tmp_path / "setsid-timeout-git"
    child_pid = tmp_path / "setsid-timeout-child.pid"
    trace = tmp_path / "setsid-timeout.trace"
    _write_delayed_setsid_escape_git(fake_git, child_pid, trace, probe_fails=False)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))
    # Model an unreadable `/proc` snapshot. The real fixture still makes a
    # delayed `setsid` escape, so no cleanup verdict may be successful.
    monkeypatch.setattr(exec_mod, "_observe_git_descendants", lambda _: None)
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed, match="cleanup_unconfirmed"):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git",
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            deadline=exec_mod.GitProtocolDeadline.start(0.8, cleanup_reserve_seconds=0.4),
        )
    _assert_delayed_escape_trace_and_reap(child_pid, trace)


def test_probe_failure_delayed_setsid_escape_fails_closed_not_snapshot_success(monkeypatch, tmp_path):
    fake_git = tmp_path / "setsid-probe-git"
    child_pid = tmp_path / "setsid-probe-child.pid"
    trace = tmp_path / "setsid-probe.trace"
    _write_delayed_setsid_escape_git(fake_git, child_pid, trace, probe_fails=True)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))
    # Model an unreadable `/proc` snapshot. The real fixture still makes a
    # delayed `setsid` escape, so no cleanup verdict may be successful.
    monkeypatch.setattr(exec_mod, "_observe_git_descendants", lambda _: None)
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed, match="cleanup_unconfirmed"):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git",
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            deadline=exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=1),
        )
    _assert_delayed_escape_trace_and_reap(child_pid, trace)


def test_exception_delayed_setsid_escape_fails_closed_not_snapshot_success(monkeypatch, tmp_path):
    fake_git = tmp_path / "setsid-exception-git"
    child_pid = tmp_path / "setsid-exception-child.pid"
    trace = tmp_path / "setsid-exception.trace"
    _write_delayed_setsid_escape_git(fake_git, child_pid, trace, probe_fails=False)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))
    # An observation gap is indistinguishable from an unreadable snapshot.
    monkeypatch.setattr(exec_mod, "_observe_git_descendants", lambda _: None)
    real_popen = exec_mod.subprocess.Popen

    class ExplodingPopen:
        def __init__(self, *args, **kwargs):
            self._proc = real_popen(*args, **kwargs)
            self.pid = self._proc.pid
            self._is_probe = "config" in args[0]

        def communicate(self, **kwargs):
            if self._is_probe:
                return self._proc.communicate(**kwargs)
            time.sleep(0.1)
            raise RuntimeError("injected_delayed_setsid_post_spawn_failure")

        def __getattr__(self, name):
            return getattr(self._proc, name)

    monkeypatch.setattr(exec_mod.subprocess, "Popen", ExplodingPopen)
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed, match="cleanup_unconfirmed"):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git",
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            deadline=exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=1),
        )
    _assert_delayed_escape_trace_and_reap(child_pid, trace)
