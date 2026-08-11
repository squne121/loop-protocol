"""Real-subprocess tests for `skill_runtime_exec.py`'s outer-child supervisor
(Issue #2075).

These tests exercise `_run_child_with_supervision()` -- the `Popen`-based
supervisor that replaced the previous `subprocess.run(...,
timeout=timeout_seconds)` + `except TimeoutExpired` pattern -- directly
against real, hermetic fixture subprocesses spawned and controlled entirely
by the test itself (no external service, network, or production runtime
process is touched; see the Issue's `Runtime Verification Applicability:
not_applicable` decision).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_GUARDS_DIR = REPO_ROOT / "scripts" / "agent-guards"
sys.path.insert(0, str(AGENT_GUARDS_DIR))

import skill_runtime_exec as real_exec  # noqa: E402 -- real production module under test

PY = sys.executable

# Keep the cleanup budget small and deterministic for fast, non-flaky tests
# (the production default of 5.0s / 2.0s is unnecessarily slow for a test
# suite; individual tests still exercise the *shape* of the staged
# escalation, not the specific default durations).
_FAST_CLEANUP_GRACE_SECONDS = 2.0
_FAST_TERM_GRACE_SECONDS = 0.5


@pytest.fixture(autouse=True)
def _fast_cleanup_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(real_exec, "_CLEANUP_GRACE_SECONDS", _FAST_CLEANUP_GRACE_SECONDS)
    monkeypatch.setattr(real_exec, "_TERM_GRACE_SECONDS", _FAST_TERM_GRACE_SECONDS)


def _sleeper_argv(seconds: float) -> list[str]:
    return [PY, "-c", f"import time; time.sleep({seconds})"]


def _ignore_sigterm_sleeper_argv(seconds: float) -> list[str]:
    return [
        PY,
        "-c",
        f"import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep({seconds})",
    ]


def _leader_with_grandchild_argv(marker_path: Path, seconds: float) -> list[str]:
    """A leader that forks a grandchild (never calling `setsid`/`setpgid`
    itself, so it inherits the leader's process group) and records the
    grandchild's PID to `marker_path` before both sleep."""
    code = (
        "import os, time\n"
        f"pid = os.fork()\n"
        "if pid == 0:\n"
        f"    time.sleep({seconds})\n"
        "    os._exit(0)\n"
        "else:\n"
        f"    with open({str(marker_path)!r}, 'w') as fh:\n"
        "        fh.write(str(pid))\n"
        f"    time.sleep({seconds})\n"
    )
    return [PY, "-c", code]


def _reap_stray(pid: int | None, timeout: float = 3.0) -> None:
    """Best-effort reap of a leftover direct-child PID so a test never
    leaves a zombie behind, regardless of test assertions/outcome."""
    if pid is None:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if reaped_pid == pid:
            return
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# AC1
# ---------------------------------------------------------------------------


def test_outer_child_uses_popen_start_new_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN skill_runtime_exec's outer-child supervisor
    WHEN it launches a child command
    THEN it launches the child via `subprocess.Popen(...,
    start_new_session=True)`, holding the child's PID/PGID from the moment
    it starts, and never via `subprocess.run` (Issue #2075 AC1)."""
    captured: dict[str, object] = {}
    real_popen = real_exec.subprocess.Popen

    def _spy_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["start_new_session"] = kwargs.get("start_new_session")
        return real_popen(argv, **kwargs)

    def _forbidden_run(*_args, **_kwargs):
        raise AssertionError(
            "subprocess.run must not be used for the outer child launch (Issue #2075 AC1)"
        )

    monkeypatch.setattr(real_exec.subprocess, "Popen", _spy_popen)
    monkeypatch.setattr(real_exec.subprocess, "run", _forbidden_run)

    result = real_exec._run_child_with_supervision(
        [PY, "-c", "print('ok')"],
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
        timeout_seconds=10,
    )

    try:
        assert captured["start_new_session"] is True, captured
        assert result.timed_out is False
        assert result.returncode == 0
        assert result.stdout == "ok\n"
    finally:
        _reap_stray(result.pid)


# ---------------------------------------------------------------------------
# AC2
# ---------------------------------------------------------------------------


def test_timeout_process_group_isolation(tmp_path: Path) -> None:
    """GIVEN a leader that forks a sleeping grandchild in the same PGID
    WHEN the global deadline is exceeded
    THEN the executor-owned process group's absence is confirmed (Issue
    #2075 AC2)."""
    marker = tmp_path / "grandchild_pid"
    result = real_exec._run_child_with_supervision(
        _leader_with_grandchild_argv(marker, 30.0),
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
        timeout_seconds=0.3,
    )

    try:
        assert result.timed_out is True
        assert result.cleanup_scope == real_exec.CLEANUP_SCOPE_PROCESS_GROUP
        assert result.cleanup_status == real_exec.CLEANUP_STATUS_CONFIRMED_ABSENT
        assert result.leader_reaped is True
        assert marker.exists()
    finally:
        _reap_stray(result.pid)


# ---------------------------------------------------------------------------
# AC3
# ---------------------------------------------------------------------------


def test_sigterm_escalates_to_sigkill() -> None:
    """GIVEN a leader that ignores SIGTERM
    WHEN the bounded grace period after SIGTERM elapses
    THEN SIGKILL escalation actually happens and the group is confirmed
    gone (Issue #2075 AC3)."""
    result = real_exec._run_child_with_supervision(
        _ignore_sigterm_sleeper_argv(30.0),
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
        timeout_seconds=0.2,
    )

    try:
        assert result.timed_out is True
        assert result.termination == real_exec.TERMINATION_TERM_THEN_KILL
        assert result.cleanup_status == real_exec.CLEANUP_STATUS_CONFIRMED_ABSENT
        assert result.leader_reaped is True
    finally:
        _reap_stray(result.pid)


# ---------------------------------------------------------------------------
# AC4
# ---------------------------------------------------------------------------


def test_normal_success_semantics_unchanged() -> None:
    """GIVEN a child that exits normally well within the timeout
    WHEN the supervisor runs it
    THEN stdout/stderr/returncode match the pre-#2075
    `subprocess.run(capture_output=True, text=True)` semantics exactly, and
    no cleanup is ever attempted (Issue #2075 AC4)."""
    code = (
        "import sys\n"
        "sys.stdout.write('hello-stdout\\n')\n"
        "sys.stderr.write('hello-stderr\\n')\n"
        "sys.exit(7)\n"
    )
    result = real_exec._run_child_with_supervision(
        [PY, "-c", code],
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
        timeout_seconds=10,
    )

    try:
        assert result.timed_out is False
        assert result.returncode == 7
        assert result.stdout == "hello-stdout\n"
        assert result.stderr == "hello-stderr\n"
        assert result.cleanup_status == real_exec.CLEANUP_STATUS_NOT_STARTED
        assert result.termination == real_exec.TERMINATION_NOT_NEEDED
    finally:
        _reap_stray(result.pid)


# ---------------------------------------------------------------------------
# AC5
# ---------------------------------------------------------------------------


def test_direct_child_reaped() -> None:
    """GIVEN a timeout that triggers cleanup
    WHEN the supervisor completes
    THEN the direct child (leader) has actually been `wait()`ed -- a second
    `os.waitpid(pid, os.WNOHANG)` proves no zombie remains (Issue #2075
    AC5)."""
    result = real_exec._run_child_with_supervision(
        _sleeper_argv(30.0),
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
        timeout_seconds=0.2,
    )

    assert result.timed_out is True
    assert result.leader_reaped is True
    assert result.pid is not None

    with pytest.raises(ChildProcessError):
        os.waitpid(result.pid, os.WNOHANG)


# ---------------------------------------------------------------------------
# AC6
# ---------------------------------------------------------------------------


def test_cleanup_deadline_exhaustion_unconfirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN a cleanup budget that is already exhausted the instant cleanup
    begins (deadline exhaustion injected via `_CLEANUP_GRACE_SECONDS`)
    WHEN the supervisor's timeout cleanup runs
    THEN `cleanup_status` is `unconfirmed` -- never silently promoted to
    `confirmed_absent` -- regardless of the child's true fate (Issue #2075
    AC6)."""
    monkeypatch.setattr(real_exec, "_CLEANUP_GRACE_SECONDS", -1.0)
    monkeypatch.setattr(real_exec, "_TERM_GRACE_SECONDS", -1.0)

    result = real_exec._run_child_with_supervision(
        _sleeper_argv(30.0),
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
        timeout_seconds=0.2,
    )

    try:
        assert result.timed_out is True
        assert result.cleanup_status == real_exec.CLEANUP_STATUS_UNCONFIRMED
    finally:
        # The already-exhausted deadline means SIGTERM was sent but SIGKILL
        # may never have been (the escalation guard also checks the
        # deadline); make sure nothing leaks past this test regardless.
        if result.pid is not None:
            try:
                os.killpg(os.getpgid(result.pid), 9)
            except (ProcessLookupError, PermissionError):
                pass
        _reap_stray(result.pid)


# ---------------------------------------------------------------------------
# AC7
# ---------------------------------------------------------------------------


def test_partial_stdout_stderr_not_leaked(capsys: pytest.CaptureFixture[str]) -> None:
    """GIVEN a child that writes substantial output and then hangs past the
    timeout
    WHEN the supervisor times out and cleans it up
    THEN the child's partial stdout/stderr is not surfaced in the
    supervision result, and `_emit_timeout_failure()`'s stderr telemetry
    never leaks that partial content (Issue #2075 AC7)."""
    secret_marker = "PARTIAL_OUTPUT_MARKER_SHOULD_NOT_LEAK"
    code = (
        "import sys, time\n"
        f"sys.stdout.write('{secret_marker}-stdout\\n')\n"
        f"sys.stderr.write('{secret_marker}-stderr\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stderr.flush()\n"
        "time.sleep(30)\n"
    )
    result = real_exec._run_child_with_supervision(
        [PY, "-c", code],
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
        timeout_seconds=0.3,
    )

    try:
        assert result.timed_out is True
        assert result.stdout == ""
        assert result.stderr == ""

        exit_code = real_exec._emit_timeout_failure(
            999999,
            0.3,
            cleanup_scope=result.cleanup_scope,
            cleanup_status=result.cleanup_status,
            termination=result.termination,
            leader_reaped=result.leader_reaped,
        )
        captured = capsys.readouterr()
        assert exit_code == 2
        assert secret_marker not in captured.err
        assert secret_marker not in captured.out
    finally:
        _reap_stray(result.pid)


# ---------------------------------------------------------------------------
# AC8
# ---------------------------------------------------------------------------


def test_non_posix_fallback_not_false_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN a simulated non-POSIX environment (`start_new_session` /
    `os.killpg` / `os.setsid` unavailable)
    WHEN a child times out
    THEN cleanup never claims `confirmed_absent` -- there is no
    process-group guarantee to confirm on this platform, so it is always
    `unconfirmed` (Issue #2075 AC8)."""
    monkeypatch.setattr(real_exec, "_POSIX_PROCESS_GROUP_SUPPORTED", False)

    result = real_exec._run_child_with_supervision(
        _sleeper_argv(30.0),
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
        timeout_seconds=0.2,
    )

    try:
        assert result.timed_out is True
        assert result.cleanup_status == real_exec.CLEANUP_STATUS_UNCONFIRMED
        assert result.cleanup_scope == real_exec.CLEANUP_SCOPE_PROCESS_GROUP
    finally:
        _reap_stray(result.pid)


# ---------------------------------------------------------------------------
# AC9
# ---------------------------------------------------------------------------


def test_cleanup_status_telemetry_enum(capsys: pytest.CaptureFixture[str]) -> None:
    """GIVEN a timeout failure
    WHEN `_emit_timeout_failure()` reports it
    THEN the closed-enum telemetry fields (`cleanup_scope`, `cleanup_status`,
    `termination`, `leader_reaped`) are all present in the stderr output
    with valid enum values (Issue #2075 AC9)."""
    exit_code = real_exec._emit_timeout_failure(
        424242,
        60,
        cleanup_scope=real_exec.CLEANUP_SCOPE_PROCESS_GROUP,
        cleanup_status=real_exec.CLEANUP_STATUS_CONFIRMED_ABSENT,
        termination=real_exec.TERMINATION_TERM_THEN_KILL,
        leader_reaped=True,
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "reason_code=child_process_timeout" in captured.err
    assert "cleanup_scope=process_group" in captured.err
    assert "cleanup_status=confirmed_absent" in captured.err
    assert "termination=term_then_kill" in captured.err
    assert "leader_reaped=true" in captured.err

    # Enum closure: only the documented values are ever legal.
    assert real_exec.CLEANUP_SCOPE_PROCESS_GROUP == "process_group"
    assert {
        real_exec.CLEANUP_STATUS_CONFIRMED_ABSENT,
        real_exec.CLEANUP_STATUS_UNCONFIRMED,
        real_exec.CLEANUP_STATUS_NOT_STARTED,
    } == {"confirmed_absent", "unconfirmed", "not_started"}
    assert {
        real_exec.TERMINATION_TERM,
        real_exec.TERMINATION_TERM_THEN_KILL,
        real_exec.TERMINATION_NOT_NEEDED,
    } == {"term", "term_then_kill", "not_needed"}
