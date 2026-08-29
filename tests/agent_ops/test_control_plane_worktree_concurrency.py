from __future__ import annotations

import importlib.util
import json
import os
import pickle
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "worktree_bootstrap_exec.py"


def _load_executor() -> ModuleType:
    spec = importlib.util.spec_from_file_location("worktree_bootstrap_exec_2198", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EXECUTOR = _load_executor()


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "control-plane-test",
        "GIT_AUTHOR_EMAIL": "control-plane-test@example.invalid",
        "GIT_COMMITTER_NAME": "control-plane-test",
        "GIT_COMMITTER_EMAIL": "control-plane-test@example.invalid",
    }
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )


@pytest.fixture()
def repo_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    primary = tmp_path / "primary"
    primary.mkdir()
    _git("init", "-b", "main", cwd=primary)
    (primary / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=primary)
    _git("commit", "-m", "seed", cwd=primary)
    linked = tmp_path / "linked"
    _git("worktree", "add", "-b", "linked", str(linked), cwd=primary)

    other = tmp_path / "other"
    other.mkdir()
    _git("init", "-b", "main", cwd=other)
    (other / "README.md").write_text("other\n", encoding="utf-8")
    _git("add", "README.md", cwd=other)
    _git("commit", "-m", "seed", cwd=other)
    return primary, linked, other


def _worker_code() -> str:
    return textwrap.dedent(
        """
        import errno
        import importlib.util
        import json
        import os
        import sys
        import time

        source, project_root, action, timeout_seconds, hold_seconds = sys.argv[1:]
        spec = importlib.util.spec_from_file_location("worker_executor", source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        try:
            if action == "release_after_contention":
                original_flock = module.fcntl.flock
                contention_state = {"reported": False}

                def report_first_contention(fd, operation):
                    try:
                        return original_flock(fd, operation)
                    except OSError as exc:
                        if (
                            not contention_state["reported"]
                            and operation & module.fcntl.LOCK_NB
                            and exc.errno in {errno.EACCES, errno.EAGAIN}
                        ):
                            contention_state["reported"] = True
                            print(json.dumps({"status": "contended"}), flush=True)
                        raise

                module.fcntl.flock = report_first_contention

            guard = module.acquire_control_plane_preflight_lifecycle_mutex(
                project_root, deadline_at=time.monotonic() + float(timeout_seconds)
            )
            child_pid = None
            if action == "fork_child_hold":
                ready_read, ready_write = os.pipe()
                child_pid = os.fork()
                if child_pid == 0:
                    os.close(ready_read)
                    try:
                        os.write(ready_write, b"ready")
                        time.sleep(float(hold_seconds))
                    finally:
                        os.close(ready_write)
                    os._exit(0)
                os.close(ready_write)
                try:
                    assert os.read(ready_read, 5) == b"ready"
                finally:
                    os.close(ready_read)

            payload = {"status": "acquired", "lock_path": str(guard.lock_path)}
            if child_pid is not None:
                payload["child_pid"] = child_pid
            print(json.dumps(payload), flush=True)
            if action in {"hold", "fork_child_hold"}:
                time.sleep(float(hold_seconds))
            guard.release()
        except module.ControlPlaneLifecycleLockBusy as exc:
            print(json.dumps({"status": exc.status, "reason_code": exc.reason_code}), flush=True)
        except BaseException as exc:
            print(json.dumps({"status": "error", "type": type(exc).__name__, "message": str(exc)}), flush=True)
            raise
        """
    )


def _start_worker(
    project_root: Path, *, action: str, timeout_seconds: float, hold_seconds: float = 0.0
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _worker_code(),
            str(SCRIPT),
            str(project_root),
            action,
            str(timeout_seconds),
            str(hold_seconds),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read_worker_result(process: subprocess.Popen[str]) -> dict[str, object]:
    assert process.stdout is not None
    line = process.stdout.readline()
    assert line, process.stderr.read() if process.stderr is not None else "worker emitted no result"
    return json.loads(line)


def _wait_worker(process: subprocess.Popen[str]) -> tuple[dict[str, object], str]:
    payload = _read_worker_result(process)
    _, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr
    return payload, stderr


def _open_fds_for(path: Path) -> set[str]:
    target = str(path.resolve())
    return {entry.name for entry in Path("/proc/self/fd").iterdir() if os.path.realpath(entry) == target}


def test_given_primary_or_linked_worktree_when_acquired_then_canonical_common_dir_and_fixed_key(
    repo_pair: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    primary, linked, _ = repo_pair
    expected_common_dir = Path(
        _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=primary).stdout.strip()
    ).resolve()
    observed: list[tuple[str, str]] = []
    real_executable = EXECUTOR.resolve_git_subprocess_executable
    real_environment = EXECUTOR.sanitized_git_subprocess_env

    probe_delay_seconds = 0.0

    def executable(project_root: str) -> str:
        observed.append(("executable", project_root))
        if probe_delay_seconds:
            time.sleep(probe_delay_seconds)
        return real_executable(project_root)

    def environment(project_root: str) -> dict[str, str]:
        observed.append(("environment", project_root))
        return real_environment(project_root)

    monkeypatch.setenv("GIT_DIR", str(primary / "not-the-repository"))
    monkeypatch.setattr(EXECUTOR, "resolve_git_subprocess_executable", executable)
    monkeypatch.setattr(EXECUTOR, "sanitized_git_subprocess_env", environment)

    with EXECUTOR.acquire_control_plane_preflight_lifecycle_mutex(
        primary, deadline_at=time.monotonic() + 2
    ) as primary_guard:
        assert primary_guard.canonical_common_dir == expected_common_dir
        assert (
            primary_guard.lock_path == expected_common_dir / "loop-protocol" / "locks" / "control-plane-preflight.lock"
        )
        assert primary_guard.lock_path.exists()
        primary_guard.assert_held()

    with EXECUTOR.acquire_control_plane_preflight_lifecycle_mutex(
        linked, deadline_at=time.monotonic() + 2
    ) as linked_guard:
        assert linked_guard.canonical_common_dir == expected_common_dir
        linked_guard.assert_held()

    subprocess_run_calls = 0

    def unexpected_subprocess_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        nonlocal subprocess_run_calls
        subprocess_run_calls += 1
        raise AssertionError("expired deadline must not invoke subprocess.run")

    monkeypatch.setattr(EXECUTOR.subprocess, "run", unexpected_subprocess_run)
    with pytest.raises(EXECUTOR.ControlPlaneLifecycleLockBusy):
        EXECUTOR.acquire_control_plane_preflight_lifecycle_mutex(primary, deadline_at=time.monotonic() - 0.01)
    assert subprocess_run_calls == 0

    observed_timeouts: list[float] = []
    deadline_at = time.monotonic() + 0.5
    probe_delay_seconds = 0.15

    def bounded_subprocess_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args
        timeout = kwargs["timeout"]
        assert isinstance(timeout, float)
        observed_timeouts.append(timeout)
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"{expected_common_dir}\n", stderr=""
        )

    monkeypatch.setattr(EXECUTOR.subprocess, "run", bounded_subprocess_run)
    with EXECUTOR.acquire_control_plane_preflight_lifecycle_mutex(primary, deadline_at=deadline_at) as guard:
        guard.assert_held()
    assert len(observed_timeouts) == 1
    assert 0 < observed_timeouts[0] <= 0.4
    assert observed == [
        ("executable", str(primary)),
        ("environment", str(primary)),
        ("executable", str(linked)),
        ("environment", str(linked)),
        ("executable", str(primary)),
        ("environment", str(primary)),
    ]


def test_given_competing_primary_and_linked_processes_when_acquired_then_competing_processes_by_common_dir(
    repo_pair: tuple[Path, Path, Path],
) -> None:
    primary, linked, other = repo_pair
    holder = _start_worker(primary, action="hold", timeout_seconds=3, hold_seconds=1.0)
    holder_payload = _read_worker_result(holder)
    assert holder_payload["status"] == "acquired"

    competing_payload, _ = _wait_worker(_start_worker(linked, action="release", timeout_seconds=0.2))
    independent_payload, _ = _wait_worker(_start_worker(other, action="release", timeout_seconds=0.2))
    _, holder_stderr = holder.communicate(timeout=10)

    assert holder.returncode == 0, holder_stderr
    assert competing_payload == {"status": "control_plane_unavailable", "reason_code": "lifecycle_lock_busy"}
    assert independent_payload["status"] == "acquired"


def test_given_busy_lock_when_owner_releases_after_observed_contention_then_bounded_timeout_and_explicit_release(
    repo_pair: tuple[Path, Path, Path],
) -> None:
    primary, linked, _ = repo_pair
    guard = EXECUTOR.acquire_control_plane_preflight_lifecycle_mutex(primary, deadline_at=time.monotonic() + 2)
    waiting = _start_worker(linked, action="release_after_contention", timeout_seconds=1.0)
    assert _read_worker_result(waiting) == {"status": "contended"}
    guard.release()
    waiting_payload, _ = _wait_worker(waiting)
    assert waiting_payload["status"] == "acquired"

    held = EXECUTOR.acquire_control_plane_preflight_lifecycle_mutex(primary, deadline_at=time.monotonic() + 2)
    try:
        timed_out_payload, _ = _wait_worker(_start_worker(linked, action="release", timeout_seconds=0.1))
    finally:
        held.release()
    assert timed_out_payload == {"status": "control_plane_unavailable", "reason_code": "lifecycle_lock_busy"}


def test_given_guard_when_foreign_released_or_key_tampered_then_owner_token_foreign_released_or_key_mismatch(
    repo_pair: tuple[Path, Path, Path],
) -> None:
    primary, linked, other = repo_pair
    guard = EXECUTOR.acquire_control_plane_preflight_lifecycle_mutex(primary, deadline_at=time.monotonic() + 2)
    try:
        with pytest.raises(EXECUTOR.ControlPlaneLifecycleReentrantAcquireError):
            EXECUTOR.acquire_control_plane_preflight_lifecycle_mutex(primary, deadline_at=time.monotonic() + 2)
        with pytest.raises(TypeError, match="not_serializable"):
            pickle.dumps(guard)

        key = str(guard._canonical_common_dir)
        with EXECUTOR._CONTROL_PLANE_GUARD_REGISTRY_LOCK:
            registered = EXECUTOR._HELD_CONTROL_PLANE_GUARDS[key]
            EXECUTOR._HELD_CONTROL_PLANE_GUARDS[key] = (object(), guard)
        try:
            with pytest.raises(EXECUTOR.ControlPlaneLifecycleGuardTokenMismatch):
                guard.assert_held()
        finally:
            with EXECUTOR._CONTROL_PLANE_GUARD_REGISTRY_LOCK:
                EXECUTOR._HELD_CONTROL_PLANE_GUARDS[key] = registered
        guard.assert_held()

        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            try:
                guard.assert_held()
            except EXECUTOR.ForeignProcessGuardError:
                try:
                    guard.release()
                except EXECUTOR.ForeignProcessGuardError:
                    os.write(write_fd, b"foreign-operations-rejected")
                    os._exit(0)
            os.write(write_fd, b"foreign-operation-accepted")
            os._exit(1)
        os.close(write_fd)
        child_result = os.read(read_fd, 64)
        _, child_status = os.waitpid(child_pid, 0)
        assert child_result == b"foreign-operations-rejected"
        assert os.waitstatus_to_exitcode(child_status) == 0

        still_contended, _ = _wait_worker(_start_worker(linked, action="release", timeout_seconds=0.1))
        assert still_contended == {"status": "control_plane_unavailable", "reason_code": "lifecycle_lock_busy"}

        original_common_dir = guard._canonical_common_dir
        guard._canonical_common_dir = EXECUTOR._canonical_existing_git_common_dir(
            other, deadline_at=time.monotonic() + 2
        )
        with pytest.raises(EXECUTOR.ControlPlaneLifecycleGuardKeyMismatch):
            guard.assert_held()
        guard._canonical_common_dir = original_common_dir
        guard.assert_held()
    finally:
        guard.release()

    with pytest.raises(EXECUTOR.ReleasedControlPlaneLifecycleGuardError):
        guard.assert_held()
    with pytest.raises(EXECUTOR.ReleasedControlPlaneLifecycleGuardError):
        guard.release()


def test_holder_death_reacquire_and_primary_invariant_with_long_lived_fork_child(
    repo_pair: tuple[Path, Path, Path],
) -> None:
    primary, linked, _ = repo_pair
    snapshot_before = (
        _git("branch", "--show-current", cwd=primary).stdout,
        _git("rev-parse", "HEAD", cwd=primary).stdout,
        _git("status", "--porcelain", cwd=primary).stdout,
    )
    holder = _start_worker(primary, action="fork_child_hold", timeout_seconds=3, hold_seconds=30)
    child_pid: int | None = None
    try:
        holder_payload = _read_worker_result(holder)
        assert holder_payload["status"] == "acquired"
        child_pid = int(holder_payload["child_pid"])
        lock_path = Path(str(holder_payload["lock_path"]))
        assert lock_path.exists()
        os.kill(child_pid, 0)

        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=10)
        assert holder.returncode == -signal.SIGKILL
        assert lock_path.exists()
        os.kill(child_pid, 0)

        reacquired_payload, _ = _wait_worker(_start_worker(linked, action="release", timeout_seconds=2))
        snapshot_after = (
            _git("branch", "--show-current", cwd=primary).stdout,
            _git("rev-parse", "HEAD", cwd=primary).stdout,
            _git("status", "--porcelain", cwd=primary).stdout,
        )
        assert reacquired_payload["status"] == "acquired"
        assert snapshot_after == snapshot_before
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_context_manager_exception_and_fd_close_for_same_process_timeout(
    repo_pair: tuple[Path, Path, Path],
) -> None:
    primary, linked, _ = repo_pair
    with pytest.raises(RuntimeError, match="body failure"):
        with EXECUTOR.acquire_control_plane_preflight_lifecycle_mutex(
            primary, deadline_at=time.monotonic() + 2
        ) as guard:
            lock_path = guard.lock_path
            assert lock_path.exists()
            assert _open_fds_for(lock_path)
            raise RuntimeError("body failure")
    assert lock_path.exists()
    assert not _open_fds_for(lock_path)

    external_holder = _start_worker(primary, action="hold", timeout_seconds=3, hold_seconds=1.0)
    holder_payload = _read_worker_result(external_holder)
    assert holder_payload["status"] == "acquired"
    assert Path(str(holder_payload["lock_path"])) == lock_path
    before_timeout_fds = _open_fds_for(lock_path)
    with pytest.raises(EXECUTOR.ControlPlaneLifecycleLockBusy):
        EXECUTOR.acquire_control_plane_preflight_lifecycle_mutex(linked, deadline_at=time.monotonic() + 0.1)
    assert _open_fds_for(lock_path) == before_timeout_fds
    _, holder_stderr = external_holder.communicate(timeout=10)
    assert external_holder.returncode == 0, holder_stderr
    assert lock_path.exists()
    assert not _open_fds_for(lock_path)
