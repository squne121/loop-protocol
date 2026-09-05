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


def _init_remote_fixture(tmp_path: Path) -> tuple[Path, str, str]:
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
        # Issue #2197 additions -- same closed-executor discipline applies.
        "run_control_plane_git_read_worktree_status_porcelain",
        "run_control_plane_git_remove_existing_detached_locked_worktree",
        "run_control_plane_remote_default_ref_binding",
    ):
        assert "argv" not in inspect.signature(getattr(exec_mod, name)).parameters
    assert "*args" not in inspect.getsource(exec_mod.run_control_plane_git_add_detached_locked_worktree)
    assert "*args" not in inspect.getsource(exec_mod.run_control_plane_git_remove_existing_detached_locked_worktree)


def test_new_control_plane_wrappers_route_through_closed_executor_with_fixed_argv_shapes(monkeypatch, tmp_path):
    """Issue #2197: `read_worktree_status_porcelain` and
    `remove_existing_detached_locked_worktree` are narrow, single-purpose
    wrappers -- not a new generic force-remove/status API. Prove their exact
    fixed argv shape the same way `test_exact_remote_commands_and_fixed_fetch_cas_shapes`
    already proves it for the pre-existing wrappers."""
    calls: list[tuple[str, ...]] = []

    def fake_run(operation, **kwargs):
        args = tuple(
            exec_mod._exact_git_argv(
                operation,
                git_executable=kwargs["git_executable"],
                cwd=kwargs["cwd"],
                hooks_dir=kwargs["hooks_dir"],
            )
        )
        calls.append(args)
        return subprocess.CompletedProcess(list(args), 1 if "config" in args else 0, "", "")

    monkeypatch.setattr(exec_mod, "_run_closed_git_process", fake_run)

    from skill_runtime_command_policy import validate_existing_detached_worktree_path

    worktree_dir = tmp_path / ".claude" / "worktrees" / "existing"
    worktree_dir.mkdir(parents=True)
    existing_path = validate_existing_detached_worktree_path(str(worktree_dir), str(tmp_path))
    deadline = _deadline()

    exec_mod.run_control_plane_git_read_worktree_status_porcelain(
        existing_path, project_root=str(tmp_path), deadline=deadline
    )
    exec_mod.run_control_plane_git_remove_existing_detached_locked_worktree(
        existing_path, cwd=str(tmp_path), project_root=str(tmp_path), deadline=deadline
    )

    semantic_calls = [call for call in calls if "config" not in call]
    assert any(call[-3:] == ("status", "--porcelain", "--untracked-files=all") for call in semantic_calls)
    assert any(
        call[-5:] == ("worktree", "remove", "--force", "--force", existing_path.value) for call in semantic_calls
    )


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
    fmt = exec_mod.run_control_plane_git_repository_object_format(
        cwd=str(tmp_path), project_root=str(tmp_path), deadline=deadline
    )
    private = exec_mod.run_control_plane_git_fetch_default_ref(
        remote, ref, object_format=fmt, cwd=str(tmp_path), project_root=str(tmp_path), deadline=deadline
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
        object_format=fmt,
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


def test_effective_remote_rewrite_mismatch_fails_closed_before_remote_operation(tmp_path):
    local, url, _ = _init_remote_fixture(tmp_path)
    subprocess.run(["git", "config", "--local", "url.https://evil.example/.insteadOf", url], cwd=local, check=True)
    with pytest.raises(RuntimeError, match="effective_remote_url_mismatch"):
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
    assert _wait_until_pid_gone(pid), "delayed setsid fixture process was not reaped"


def test_private_ref_nonce_is_generated_inside_builder():
    signature = inspect.signature(exec_mod.run_control_plane_git_fetch_default_ref)
    assert "nonce" not in signature.parameters
    first = make_control_plane_private_ref()
    second = make_control_plane_private_ref()
    prefix = "refs/loop-protocol/control-plane/default-ref/"
    assert first.value.startswith(prefix)
    assert second.value.startswith(prefix)
    assert first != second
    assert len(first.value.removeprefix(prefix)) == 32


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
    assert _wait_until_pid_gone(pid), "delayed setsid fixture process was not reaped"


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
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed, match="(cleanup_unconfirmed|descendant_leak)"):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git",
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            deadline=exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=1),
        )
    pid = int(child_pid.read_text(encoding="utf-8"))
    assert _wait_until_pid_gone(pid), "delayed setsid fixture process was not reaped"


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
            object_format=exec_mod.RepositoryObjectFormat("sha1"),
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
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed):
        exec_mod._run_closed_git_process(
            exec_mod._GitOperation("repository_object_format"),
            git_executable="/usr/bin/git",
            cwd=str(tmp_path),
            env={},
            hooks_dir=str(tmp_path),
            deadline=_deadline(),
        )


def test_unrelated_insteadof_does_not_block_literal_remote(tmp_path):
    local, url, _ = _init_remote_fixture(tmp_path)
    subprocess.run(
        ["git", "config", "--local", "url.https://company-mirror.example/.insteadOf", "https://unrelated.example/"],
        cwd=local,
        check=True,
    )
    observed = exec_mod.run_control_plane_git_observe_default_ref(
        validate_literal_remote_url(url),
        cwd=str(local),
        project_root=str(local),
        scratch_root=str(tmp_path / "scratch"),
        deadline=_deadline(),
    )
    assert "refs/heads/main" in observed.stdout


def test_pushinsteadof_does_not_block_fetch_protocol(tmp_path):
    local, url, _ = _init_remote_fixture(tmp_path)
    subprocess.run(
        ["git", "config", "--local", "url.https://push-only.example/.pushInsteadOf", url],
        cwd=local,
        check=True,
    )
    observed = exec_mod.run_control_plane_git_observe_default_ref(
        validate_literal_remote_url(url),
        cwd=str(local),
        project_root=str(local),
        scratch_root=str(tmp_path / "scratch"),
        deadline=_deadline(),
    )
    assert "refs/heads/main" in observed.stdout


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
    # The invocation supervisor may terminate the delayed helper before it
    # materializes its setsid child. If it did materialize, clean the fixture
    # defensively and require eventual absence rather than reaping via the
    # long-lived test host.
    if not pid_file.exists():
        return
    pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    assert _wait_until_pid_gone(pid), "delayed setsid fixture process was not reaped"


def test_successful_leader_with_delayed_setsid_child_reaches_result_validation(monkeypatch, tmp_path):
    # A leader-rooted empty snapshot cannot certify normal-success cleanup.
    # The invocation supervisor adopts and reaps the real `setsid` child, then
    # returns to the semantic result validator; this fixture's output remains
    # deliberately invalid for effective-URL validation.
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
    with pytest.raises(RuntimeError, match="effective_remote_url_mismatch"):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git",
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            deadline=exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=1),
        )
    assert trace.read_text(encoding="utf-8") == "leader-success\n"
    pid = int(child_pid.read_text(encoding="utf-8"))
    assert _wait_until_pid_gone(pid), "delayed setsid fixture process was not reaped"


def test_timeout_delayed_setsid_escape_fails_closed_not_snapshot_success(monkeypatch, tmp_path):
    fake_git = tmp_path / "setsid-timeout-git"
    child_pid = tmp_path / "setsid-timeout-child.pid"
    trace = tmp_path / "setsid-timeout.trace"
    _write_delayed_setsid_escape_git(fake_git, child_pid, trace, probe_fails=False)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))
    # Model an unreadable `/proc` snapshot. The real fixture still makes a
    # delayed `setsid` escape, so no cleanup verdict may be successful.
    monkeypatch.setattr(exec_mod, "_observe_git_descendants", lambda _: None)
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed, match="(cleanup_unconfirmed|descendant_leak)"):
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
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed, match="(cleanup_unconfirmed|descendant_leak)"):
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
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed, match="(cleanup_unconfirmed|descendant_leak)"):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git",
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            deadline=exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=1),
        )
    _assert_delayed_escape_trace_and_reap(child_pid, trace)



def _current_process_is_linux_subreaper() -> bool:
    """Read, but never alter, the current test host's subreaper state."""
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux subreaper regression")
    libc = exec_mod.ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [
        exec_mod.ctypes.c_int,
        exec_mod.ctypes.c_ulong,
        exec_mod.ctypes.c_ulong,
        exec_mod.ctypes.c_ulong,
        exec_mod.ctypes.c_ulong,
    ]
    prctl.restype = exec_mod.ctypes.c_int
    enabled = exec_mod.ctypes.c_int()
    assert prctl(exec_mod._PR_GET_CHILD_SUBREAPER, exec_mod.ctypes.addressof(enabled), 0, 0, 0) == 0
    return bool(enabled.value)


def _wait_until_pid_gone(pid: int, timeout: float = 2.0) -> bool:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.01)
    return False


def test_first_descendant_observation_exception_still_performs_bounded_cleanup(monkeypatch, tmp_path):
    fake_git = tmp_path / "first-observation-exception-git"
    child_pid = tmp_path / "first-observation-child.pid"
    _write_group_leak_git(fake_git, child_pid)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))

    def fail_first_observation(_leader_pid):
        until = time.monotonic() + 1
        while not child_pid.exists() and time.monotonic() < until:
            time.sleep(0.01)
        raise RuntimeError("injected_first_descendant_observation_failure")

    monkeypatch.setattr(exec_mod, "_observe_git_descendants", fail_first_observation)
    with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed, match="cleanup_unconfirmed"):
        exec_mod.run_control_plane_git_effective_remote_url(
            "file:///tmp/origin.git",
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            deadline=exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=1),
        )
    pid = int(child_pid.read_text(encoding="utf-8"))
    assert _wait_until_pid_gone(pid), "first-observation exception leaked a Git child"


def test_invocation_supervisor_does_not_classify_or_reap_unrelated_host_child(monkeypatch, tmp_path):
    """A Git escape is cleaned without adopting an unrelated executor child."""
    before_subreaper = _current_process_is_linux_subreaper()
    fake_git = tmp_path / "escaped-git"
    escaped_pid_file = tmp_path / "escaped-git.pid"
    fake_git.write_text(
        f'''#!/bin/sh
case "$*" in
  *config*) exit 1 ;;
  *)
    (setsid sh -c 'echo $$ > "{escaped_pid_file}"; sleep 30') >/dev/null 2>&1 &
    sleep 0.05
    exit 0 ;;
esac
''',
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))
    host_child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        with pytest.raises(RuntimeError, match="effective_remote_url_mismatch"):
            exec_mod.run_control_plane_git_effective_remote_url(
                "file:///tmp/origin.git",
                cwd=str(tmp_path),
                project_root=str(tmp_path),
                deadline=exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=1),
            )
        if escaped_pid_file.exists():
            escaped_pid = int(escaped_pid_file.read_text(encoding="utf-8"))
            assert _wait_until_pid_gone(escaped_pid), "Git-derived escaped child was not cleaned"
        assert _current_process_is_linux_subreaper() is before_subreaper
        assert os.waitpid(host_child.pid, os.WNOHANG) == (0, 0)
        os.kill(host_child.pid, 0)
    finally:
        try:
            os.killpg(host_child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        host_child.wait(timeout=2)



def _write_parent_result_read_escape_git(path: Path, pid_file: Path) -> None:
    path.write_text(
        f'''#!/bin/sh
case "$*" in
  *config*) exit 1 ;;
  *)
    (setsid sh -c 'echo $$ > "{pid_file}"; sleep 30') >/dev/null 2>&1 &
    while [ ! -s "{pid_file}" ]; do sleep 0.01; done
    sleep 30
    ;;
esac
''',
        encoding="utf-8",
    )
    path.chmod(0o755)


def _assert_parent_result_read_cleanup_preserves_host_child(
    host_child: subprocess.Popen, escaped_pid_file: Path
) -> None:
    escaped_pid = int(escaped_pid_file.read_text(encoding="utf-8"))
    assert _wait_until_pid_gone(escaped_pid), "parent-side result failure leaked an escaped Git descendant"
    assert os.waitpid(host_child.pid, os.WNOHANG) == (0, 0)
    os.kill(host_child.pid, 0)


def test_parent_result_read_failure_performs_git_only_cleanup_before_fail_closed(monkeypatch, tmp_path):
    """A malformed/failed parent result read cannot strand the supervisor's Git tree."""
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux invocation-supervisor regression")
    fake_git = tmp_path / "parent-result-failure-git"
    escaped_pid_file = tmp_path / "parent-result-failure-escaped.pid"
    _write_parent_result_read_escape_git(fake_git, escaped_pid_file)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))

    real_result_read = exec_mod._read_invocation_supervisor_result
    reads = 0

    def fail_result_read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            return real_result_read(*args, **kwargs)
        until = time.monotonic() + 1
        while not escaped_pid_file.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert escaped_pid_file.exists(), "fixture did not start its escaped Git descendant"
        return None

    monkeypatch.setattr(exec_mod, "_read_invocation_supervisor_result", fail_result_read)
    host_child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed, match="cleanup_unconfirmed"):
            exec_mod.run_control_plane_git_effective_remote_url(
                "file:///tmp/origin.git",
                cwd=str(tmp_path),
                project_root=str(tmp_path),
                deadline=exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=1),
            )
        _assert_parent_result_read_cleanup_preserves_host_child(host_child, escaped_pid_file)
    finally:
        try:
            os.killpg(host_child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        host_child.wait(timeout=2)


def test_parent_keyboard_interrupt_waits_for_git_only_cleanup_before_propagating(monkeypatch, tmp_path):
    """KeyboardInterrupt propagates only after the supervisor proves no Git leak."""
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux invocation-supervisor regression")
    fake_git = tmp_path / "parent-keyboard-interrupt-git"
    escaped_pid_file = tmp_path / "parent-keyboard-interrupt-escaped.pid"
    _write_parent_result_read_escape_git(fake_git, escaped_pid_file)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))

    real_result_read = exec_mod._read_invocation_supervisor_result
    reads = 0

    def interrupt_result_read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            return real_result_read(*args, **kwargs)
        until = time.monotonic() + 1
        while not escaped_pid_file.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert escaped_pid_file.exists(), "fixture did not start its escaped Git descendant"
        raise KeyboardInterrupt("injected_parent_result_read_interrupt")

    monkeypatch.setattr(exec_mod, "_read_invocation_supervisor_result", interrupt_result_read)
    host_child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        with pytest.raises(exec_mod.GitProtocolProcessGroupCleanupFailed, match="cleanup_unconfirmed"):
            exec_mod.run_control_plane_git_effective_remote_url(
                "file:///tmp/origin.git",
                cwd=str(tmp_path),
                project_root=str(tmp_path),
                deadline=exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=1),
            )
        _assert_parent_result_read_cleanup_preserves_host_child(host_child, escaped_pid_file)
    finally:
        try:
            os.killpg(host_child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        host_child.wait(timeout=2)


def test_invocation_supervisor_allows_contained_git_helper_to_finish(monkeypatch, tmp_path):
    """A normal helper in the dedicated Git group is drained, not misclassified."""
    fake_git = tmp_path / "contained-helper-git"
    fake_git.write_text(
        """#!/bin/sh
case "$*" in
  *config*) exit 1 ;;
  *)
    (sleep 0.05) >/dev/null 2>&1 &
    printf '%s\n' 'file:///tmp/origin.git'
    exit 0 ;;
esac
""",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setattr(exec_mod, "resolve_git_subprocess_executable", lambda _: str(fake_git))
    assert exec_mod.run_control_plane_git_effective_remote_url(
        "file:///tmp/origin.git",
        cwd=str(tmp_path),
        project_root=str(tmp_path),
        deadline=exec_mod.GitProtocolDeadline.start(2, cleanup_reserve_seconds=1),
    ).value == "file:///tmp/origin.git"


@pytest.mark.parametrize(
    "url",
    (
        "https://github.com/squne121/loop-protocol.git",
        "ssh://git@github.com/squne121/loop-protocol.git",
        "git@github.com:squne121/loop-protocol.git",
        "file:///tmp/loop-protocol.git",
    ),
)
def test_accepts_ssh_uri_and_scp_like_github_origin_without_network(tmp_path, url):
    local, _, _ = _init_remote_fixture(tmp_path)
    assert (
        exec_mod.run_control_plane_git_effective_remote_url(
            url,
            cwd=str(local),
            project_root=str(local),
            scratch_root=str(tmp_path / "scratch"),
            deadline=_deadline(),
        ).value
        == url
    )


def _assert_worktree_absent(local: Path, destination: Path) -> None:
    assert not destination.exists()
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=local, text=True, check=True, capture_output=True
    ).stdout
    assert f"worktree {destination}" not in listed


def _worktree_rollback_fixture(tmp_path: Path) -> tuple[Path, object, object, Path]:
    local, _, expected_oid = _init_remote_fixture(tmp_path)
    deadline = _deadline()
    object_format = exec_mod.run_control_plane_git_repository_object_format(
        cwd=str(local), project_root=str(local), scratch_root=str(tmp_path / "scratch"), deadline=deadline
    )
    commit = exec_mod.validate_repository_object_id(expected_oid, object_format)
    destination = local / ".claude" / "worktrees" / "rollback"
    return local, commit, deadline, destination


def test_worktree_readback_failure_rolls_back_locked_worktree(monkeypatch, tmp_path):
    local, commit, deadline, destination = _worktree_rollback_fixture(tmp_path)
    path = validate_detached_worktree_path(str(destination), str(local))
    monkeypatch.setattr(
        exec_mod,
        "run_control_plane_git_read_worktree_head",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected_readback_failure")),
    )
    with pytest.raises(RuntimeError, match="injected_readback_failure"):
        exec_mod.run_control_plane_git_add_detached_locked_worktree(
            path,
            commit,
            cwd=str(local),
            project_root=str(local),
            scratch_root=str(tmp_path / "scratch"),
            deadline=deadline,
        )
    _assert_worktree_absent(local, destination)


def test_worktree_head_mismatch_rolls_back_locked_worktree(monkeypatch, tmp_path):
    local, commit, deadline, destination = _worktree_rollback_fixture(tmp_path)
    path = validate_detached_worktree_path(str(destination), str(local))
    monkeypatch.setattr(
        exec_mod,
        "run_control_plane_git_read_worktree_head",
        lambda *args, **kwargs: exec_mod.RepositoryObjectId("b" * len(commit.value)),
    )
    with pytest.raises(RuntimeError, match="detached_worktree_head_mismatch"):
        exec_mod.run_control_plane_git_add_detached_locked_worktree(
            path,
            commit,
            cwd=str(local),
            project_root=str(local),
            scratch_root=str(tmp_path / "scratch"),
            deadline=deadline,
        )
    _assert_worktree_absent(local, destination)


def test_worktree_deadline_exhaustion_after_add_rolls_back_locked_worktree(monkeypatch, tmp_path):
    local, commit, deadline, destination = _worktree_rollback_fixture(tmp_path)
    path = validate_detached_worktree_path(str(destination), str(local))
    monkeypatch.setattr(
        exec_mod,
        "run_control_plane_git_read_worktree_head",
        lambda *args, **kwargs: (_ for _ in ()).throw(exec_mod.GitProtocolDeadlineExhausted("injected_deadline")),
    )
    with pytest.raises(exec_mod.GitProtocolDeadlineExhausted, match="injected_deadline"):
        exec_mod.run_control_plane_git_add_detached_locked_worktree(
            path,
            commit,
            cwd=str(local),
            project_root=str(local),
            scratch_root=str(tmp_path / "scratch"),
            deadline=deadline,
        )
    _assert_worktree_absent(local, destination)


def test_git_243_partial_clone_does_not_claim_no_lazy_support(monkeypatch, tmp_path):
    """A no-capability promisor repository stops before real `git fetch`.

    The fixture uses real Git for `--get-url` and local promisor discovery;
    capability is forced to the Git 2.43 result. A fetch would create a private
    ref in this local bare-origin fixture, so its absence is behavioral proof
    that the remote operation never started.
    """
    local, url, _ = _init_remote_fixture(tmp_path)
    subprocess.run(
        ["git", "config", "--local", "remote.origin.promisor", "true"], cwd=local, check=True
    )
    monkeypatch.setattr(exec_mod, "_git_supports_no_lazy_fetch", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="no_lazy_fetch_not_supported"):
        exec_mod.run_control_plane_git_fetch_default_ref(
            validate_literal_remote_url(url),
            validate_allowed_remote_ref("refs/heads/main"),
            object_format=exec_mod.RepositoryObjectFormat("sha1"),
            cwd=str(local),
            project_root=str(local),
            scratch_root=str(tmp_path / "scratch"),
            deadline=_deadline(),
        )
    refs = subprocess.run(
        ["git", "for-each-ref", "refs/loop-protocol/control-plane/default-ref"],
        cwd=local,
        text=True,
        check=True,
        capture_output=True,
    )
    assert not refs.stdout


def test_supported_fetch_uses_fixed_no_lazy_fetch_global_option(monkeypatch, tmp_path):
    url = "file:///tmp/origin.git"
    remote = validate_literal_remote_url(url)
    ref = validate_allowed_remote_ref("refs/heads/main")
    argv: list[tuple[str, ...]] = []

    def fake_run(operation, **kwargs):
        args = tuple(
            exec_mod._exact_git_argv(
                operation,
                git_executable=kwargs["git_executable"],
                cwd=kwargs["cwd"],
                hooks_dir=kwargs["hooks_dir"],
            )
        )
        argv.append(args)
        if operation.kind == "effective_remote_url":
            return subprocess.CompletedProcess(list(args), 0, url + "\n", "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(exec_mod, "_run_closed_git_process", fake_run)
    exec_mod.run_control_plane_git_fetch_default_ref(
        remote,
        ref,
        object_format=exec_mod.RepositoryObjectFormat("sha1"),
        cwd=str(tmp_path),
        project_root=str(tmp_path),
        deadline=_deadline(),
    )
    fetch = next(args for args in argv if "fetch" in args)
    assert "--no-lazy-fetch" in fetch
    assert fetch[-6:-1] == ("fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", url)


def test_final_child_observation_none_is_cleanup_unconfirmed(monkeypatch):
    child = exec_mod._TrackedGitDescendant(123, "start")
    observations = iter(({child}, None))
    monotonic = iter((0.0, 0.0, 1.0, 1.0))
    monkeypatch.setattr(exec_mod, "_observe_invocation_supervisor_children", lambda: next(observations))
    monkeypatch.setattr(exec_mod, "_signal_invocation_child_trees", lambda *args: True)
    monkeypatch.setattr(exec_mod, "_reap_tracked_children", lambda *args: True)
    monkeypatch.setattr(exec_mod.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(exec_mod.time, "sleep", lambda *_: None)
    assert not exec_mod._terminate_invocation_supervisor_children(exec_mod.GitProtocolDeadline(1.0, 0.5))


def test_hooks_directory_is_removed_and_git_status_remains_clean(tmp_path):
    local, _, _ = _init_remote_fixture(tmp_path)
    exec_mod.run_control_plane_git_repository_object_format(
        cwd=str(local), project_root=str(local), deadline=_deadline()
    )
    assert not list(local.glob(".skill-runtime-git-hooks-*"))
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=local, text=True, check=True, capture_output=True
    )
    assert not status.stdout


# ---------------------------------------------------------------------------
# Issue #2199 AC2/AC10/AC12: minimal typed seam extension exposing
# `locked`/`prunable` porcelain fields for the dedicated-lane identity probe.
# ---------------------------------------------------------------------------


def test_list_worktrees_porcelain_locked_prunable_reports_the_primary_worktree(tmp_path):
    local, _url, _oid = _init_remote_fixture(tmp_path)
    raw = exec_mod.run_control_plane_git_list_worktrees_porcelain_locked_prunable(
        cwd=str(local), project_root=str(local), deadline=_deadline()
    )
    assert f"worktree {local}" in raw or f"worktree {os.path.realpath(local)}" in raw
    assert "\0" in raw


def test_list_worktrees_porcelain_locked_prunable_uses_no_optional_locks_and_z_form(tmp_path, monkeypatch):
    local, _url, _oid = _init_remote_fixture(tmp_path)
    captured: list[list[str]] = []
    real_run = exec_mod._run_closed_git_process

    def spy_run(operation, **kwargs):
        argv = exec_mod._exact_git_argv(
            operation,
            git_executable=exec_mod.resolve_git_subprocess_executable(str(local)),
            cwd=str(local),
            hooks_dir=str(tmp_path / "hooks"),
        )
        captured.append(argv)
        return real_run(operation, **kwargs)

    monkeypatch.setattr(exec_mod, "_run_closed_git_process", spy_run)
    exec_mod.run_control_plane_git_list_worktrees_porcelain_locked_prunable(
        cwd=str(local), project_root=str(local), deadline=_deadline()
    )
    assert captured
    argv = captured[0]
    assert "--no-optional-locks" in argv
    assert argv[-4:] == ["worktree", "list", "--porcelain", "-z"]
