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


# ---------------------------------------------------------------------------
# P1-1 / P2-2(1): leader exits on SIGTERM, descendant ignores SIGTERM --
# escalation must be driven by process-group liveness, not leader liveness.
# ---------------------------------------------------------------------------


def _term_compliant_leader_with_term_ignoring_grandchild_argv(
    marker_path: Path, seconds: float
) -> list[str]:
    """A leader that uses the default SIGTERM disposition (exits promptly on
    SIGTERM) but forks a grandchild that explicitly ignores SIGTERM and
    outlives it in the same process group."""
    code = (
        "import os, time\n"
        f"pid = os.fork()\n"
        "if pid == 0:\n"
        "    import signal\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"    time.sleep({seconds})\n"
        "    os._exit(0)\n"
        "else:\n"
        f"    with open({str(marker_path)!r}, 'w') as fh:\n"
        "        fh.write(str(pid))\n"
        f"    time.sleep({seconds})\n"
    )
    return [PY, "-c", code]


def test_leader_exits_but_term_ignoring_descendant_still_escalated_to_kill(
    tmp_path: Path,
) -> None:
    """GIVEN a leader that dies promptly from SIGTERM (default disposition)
    but a descendant in the same process group that ignores SIGTERM and
    keeps running
    WHEN the global deadline is exceeded
    THEN escalation to SIGKILL still happens (the leader's early exit must
    never be mistaken for the whole group being gone), and the group's
    absence is independently confirmed via `killpg(pgid, 0)` afterward
    (Issue #2075 P1-1)."""
    marker = tmp_path / "descendant_pid"
    result = real_exec._run_child_with_supervision(
        _term_compliant_leader_with_term_ignoring_grandchild_argv(marker, 30.0),
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
        timeout_seconds=0.3,
    )

    try:
        assert result.timed_out is True
        assert result.termination == real_exec.TERMINATION_TERM_THEN_KILL
        assert result.cleanup_status == real_exec.CLEANUP_STATUS_CONFIRMED_ABSENT
        assert result.leader_reaped is True
        assert marker.exists()

        # Independent re-verification, outside of the result telemetry: the
        # recorded pgid (== leader pid, Issue #2075 P2-1) must now raise
        # ProcessLookupError on `killpg(pgid, 0)`.
        with pytest.raises(ProcessLookupError):
            os.killpg(result.pid, 0)
    finally:
        _reap_stray(result.pid)


# ---------------------------------------------------------------------------
# P1-2 / P2-2(2): leader exits normally, descendant still holds the
# stdout/stderr pipe open -- must still time out, not silently succeed.
# ---------------------------------------------------------------------------


def _leader_exits_descendant_holds_pipes_argv(seconds: float) -> list[str]:
    """A leader that forks a grandchild inheriting its stdout/stderr write
    ends, then exits immediately itself while the grandchild keeps those
    pipes open and sleeps."""
    code = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        f"    time.sleep({seconds})\n"
        "    os._exit(0)\n"
        "else:\n"
        "    os._exit(0)\n"
    )
    return [PY, "-c", code]


def test_leader_exit_with_descendant_holding_pipes_still_times_out() -> None:
    """GIVEN a leader that exits immediately but a descendant inherits and
    keeps the stdout/stderr pipe write ends open past the execution deadline
    WHEN the supervisor runs it
    THEN the call still times out -- exactly like the pre-#2075
    `subprocess.run(..., timeout=...)` behavior, which blocks on pipe EOF,
    not on leader exit -- instead of being misreported as a normal success
    (Issue #2075 P1-2, direct AC4-regression reproduction)."""
    result = real_exec._run_child_with_supervision(
        _leader_exits_descendant_holds_pipes_argv(30.0),
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
        timeout_seconds=0.3,
    )

    try:
        assert result.timed_out is True, (
            "leader exiting early must not be mistaken for overall success "
            "while a descendant still holds the pipe open (P1-2 regression)"
        )
    finally:
        _reap_stray(result.pid)


# ---------------------------------------------------------------------------
# P1-3 / P2-2(4): an exception unwinding past a successful Popen() must
# still drive cleanup before propagating.
# ---------------------------------------------------------------------------


def test_exception_after_popen_still_cleans_up_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GIVEN `communicate()` raising an injected exception (standing in for
    e.g. `KeyboardInterrupt`) after a real child process has been launched
    WHEN `_run_child_with_supervision()` unwinds
    THEN the injected exception still propagates to the caller AND the
    process group has been driven through the bounded cleanup state machine
    (no leaked process group) before it does (Issue #2075 P1-3)."""

    class _InjectedFailure(Exception):
        pass

    call_count = {"n": 0}

    def _boom_communicate(self, *args, **kwargs):
        call_count["n"] += 1
        raise _InjectedFailure("simulated KeyboardInterrupt-like unwind")

    monkeypatch.setattr(real_exec.subprocess.Popen, "communicate", _boom_communicate)

    captured_pid: dict[str, int | None] = {"pid": None}
    real_popen = real_exec.subprocess.Popen

    def _spy_popen(argv, **kwargs):
        proc = real_popen(argv, **kwargs)
        captured_pid["pid"] = proc.pid
        return proc

    monkeypatch.setattr(real_exec.subprocess, "Popen", _spy_popen)

    with pytest.raises(_InjectedFailure):
        real_exec._run_child_with_supervision(
            _sleeper_argv(30.0),
            cwd=str(REPO_ROOT),
            env=dict(os.environ),
            timeout_seconds=10,
        )

    try:
        assert call_count["n"] == 1
        pid = captured_pid["pid"]
        assert pid is not None
        # The child must have actually been terminated and reaped, not left
        # running as an orphaned process group.
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        with pytest.raises(ProcessLookupError):
            os.killpg(pid, 0)
    finally:
        _reap_stray(captured_pid["pid"])


# ---------------------------------------------------------------------------
# P2-1: a failed pgid lookup must never be promoted to `confirmed_absent`,
# and `os.getpgid()` must never be used for the initial pgid capture.
# ---------------------------------------------------------------------------


def test_pgid_captured_from_proc_pid_without_getpgid_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GIVEN the outer-child supervisor launching a real child
    WHEN it captures the process-group id to supervise
    THEN it never calls `os.getpgid()` to do so -- `proc.pid` is used
    directly as the pgid, since `start_new_session=True` already guarantees
    the leader is its own group leader from the moment `Popen()` returns
    (Issue #2075 P2-1)."""

    def _forbidden_getpgid(*_args, **_kwargs):
        raise AssertionError(
            "os.getpgid() must not be used for pgid capture (Issue #2075 P2-1)"
        )

    monkeypatch.setattr(real_exec.os, "getpgid", _forbidden_getpgid)

    result = real_exec._run_child_with_supervision(
        _sleeper_argv(30.0),
        cwd=str(REPO_ROOT),
        env=dict(os.environ),
        timeout_seconds=0.2,
    )

    try:
        assert result.timed_out is True
        assert result.cleanup_status == real_exec.CLEANUP_STATUS_CONFIRMED_ABSENT
    finally:
        _reap_stray(result.pid)


def test_group_absence_check_permission_error_not_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GIVEN `killpg(pgid, 0)` raising a non-`ProcessLookupError` `OSError`
    (e.g. `PermissionError`) for the entire cleanup window
    WHEN the supervisor evaluates absence
    THEN `cleanup_status` is `unconfirmed`, never `confirmed_absent` --
    signal-dispatch failure of any kind other than `ProcessLookupError` must
    never be promoted to a confirmed success (Issue #2075 P2-1 / AC6/AC9
    fail-closed contract)."""
    real_killpg = real_exec.os.killpg

    def _flaky_killpg(pgid, sig):
        if sig == 0:
            raise PermissionError("simulated permission failure")
        return real_killpg(pgid, sig)

    monkeypatch.setattr(real_exec.os, "killpg", _flaky_killpg)

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
        if result.pid is not None:
            try:
                real_killpg(result.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
        _reap_stray(result.pid)


# ---------------------------------------------------------------------------
# P2-2(7): malformed child output must raise `UnicodeDecodeError`, matching
# the pre-#2075 `subprocess.run(text=True)` decode-error semantics.
# ---------------------------------------------------------------------------


def test_malformed_child_output_raises_unicode_decode_error() -> None:
    """GIVEN a child that writes an invalid UTF-8 byte sequence to stdout
    WHEN the supervisor captures its output via `communicate()`
    THEN a `UnicodeDecodeError` propagates to the caller -- exactly as the
    pre-#2075 `subprocess.run(text=True)` call would have raised -- instead
    of being silently swallowed, and the child is still fully reaped
    despite the exception (Issue #2075 P1-2)."""
    code = "import os, sys; os.write(sys.stdout.fileno(), b'\\xff\\xfe\\x00bad')"

    with pytest.raises(UnicodeDecodeError):
        real_exec._run_child_with_supervision(
            [PY, "-c", code],
            cwd=str(REPO_ROOT),
            env=dict(os.environ),
            timeout_seconds=10,
        )

    # `_run_child_with_supervision()`'s `except BaseException` handler
    # already drives the child through `_stage_cleanup()` before
    # re-raising, so no explicit reap is needed here -- this assertion is
    # itself the independent re-verification that nothing leaked.
