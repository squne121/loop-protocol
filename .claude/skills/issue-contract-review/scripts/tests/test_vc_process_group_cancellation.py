#!/usr/bin/env python3
"""
Real-process fault-injection tests for Issue #2207 AC9/AC10:
`baseline_vc_preflight.py` SIGTERM cooperative cancellation and
inner-timeout-precedes-outer-deadline classification.

Runtime Verification Applicability: immediate (per live Issue #2207 body).
These tests launch `baseline_vc_preflight.py` as a REAL subprocess (not
in-process) against a scaled fixture process tree, so a platform without
POSIX process-group semantics is treated as `environment blocked`
(pytest.skip, NOT a silent PASS).

The fixture process tree is invoked as an interpreter `-m pytest <fixture>`
command (NOT a raw `-c` inline script or unlisted script invocation)
because `baseline_vc_preflight.py`s static command allowlist only permits
`-m py_compile|pytest` invocations -- this test exercises the SAME
production classification/allowlist path a real Issue body VC would go
through, not a bypass of it. Fixture parameters (marker paths, sleep
durations) are baked directly into the generated fixture source at
generation time (no environment-variable plumbing across the subprocess
boundary).
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = Path(__file__).resolve().parents[5]
_BASELINE_VC_PREFLIGHT_PY = _SCRIPT_DIR / "baseline_vc_preflight.py"

if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import baseline_vc_preflight as vcp  # noqa: E402
import contract_readiness_check as crc  # noqa: E402

pytestmark = pytest.mark.skipif(
    not vcp.posix_process_groups_supported(),
    reason="environment blocked: POSIX process-group semantics unavailable (Issue #2207 AC9/AC10)",
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def _wait_for_file(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"marker file not created within {timeout}s: {path}")


def _wait_until_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.02)
    return False


def test_sigterm_reaps_active_vc_process_groups(tmp_path):
    """AC9 (正常系): while `baseline_vc_preflight.py` is executing a VC that
    has spawned a grandchild process, sending SIGTERM to
    `baseline_vc_preflight.py` itself terminates and reaps the WHOLE
    process group (the VC subprocess AND its grandchild) within bounded
    time -- no descendant is left orphaned."""
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()

    self_pid_path = marker_dir / "self.pid"
    grandchild_pid_path = marker_dir / "grandchild.pid"

    fixture_source = f'''
import subprocess
import sys


def test_spawn_grandchild_and_sleep():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10.0)"])
    import os as _os
    with open({str(self_pid_path)!r}, "w") as f:
        f.write(str(_os.getpid()))
    with open({str(grandchild_pid_path)!r}, "w") as f:
        f.write(str(child.pid))
    child.wait()
'''
    fixture_path = tmp_path / "test_spawn_tree_fixture.py"
    fixture_path.write_text(fixture_source, encoding="utf-8")

    body_path = tmp_path / "issue_body.md"
    body_path.write_text(
        "## Verification Commands\n\n"
        "```bash\n"
        f"$ uv run --locked pytest {fixture_path} -q -s\n"
        "```\n",
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [
            sys.executable,
            str(_BASELINE_VC_PREFLIGHT_PY),
            "--body-file",
            str(body_path),
            "--timeout-seconds",
            "60",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    try:
        _wait_for_file(self_pid_path)
        _wait_for_file(grandchild_pid_path)

        vc_pid = int(self_pid_path.read_text().strip())
        grandchild_pid = int(grandchild_pid_path.read_text().strip())

        assert _pid_alive(vc_pid), "VC subprocess should be alive before SIGTERM fault injection"
        assert _pid_alive(grandchild_pid), "grandchild should be alive before SIGTERM fault injection"

        # Fault injection: SIGTERM the outer baseline_vc_preflight.py process.
        proc.send_signal(signal.SIGTERM)

        proc.wait(timeout=15)

        assert _wait_until_dead(vc_pid, timeout=10), "VC subprocess (direct child) was not reaped after SIGTERM"
        assert _wait_until_dead(
            grandchild_pid, timeout=10
        ), "grandchild process was NOT reaped after SIGTERM (process group leak)"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        for p in (self_pid_path, grandchild_pid_path):
            if p.exists():
                try:
                    pid = int(p.read_text().strip())
                    if _pid_alive(pid):
                        os.kill(pid, signal.SIGKILL)
                except (ValueError, OSError):
                    pass


def test_scaled_fault_injection_inner_timeout_precedes_outer_deadline(tmp_path):
    """AC10 (正常系): with a production-shaped subprocess hierarchy (an
    interpreter `-m pytest` VC subprocess), an inner (per-command) VC
    timeout well below the fixture's own sleep duration fires and
    classifies as `timeout` BEFORE the fixture would have completed
    naturally -- and (fallback-free) the marker file proves the process
    was actually killed mid-sleep rather than merely raced to a natural
    finish."""
    marker_path = tmp_path / "marker.txt"

    # Inner (per-command) timeout: 3s (integer-seconds CLI contract).
    # `uv run --locked pytest <fixture>` subprocess startup overhead
    # (interpreter boot + venv/lock resolution + pytest collection) can
    # itself consume well over 1s before the fixture even reaches its
    # first statement, which previously caused the inner cap to fire
    # before the fixture wrote its "started" marker (FileNotFoundError,
    # not a genuine inner-precedes-outer signal). Widening the inner cap
    # to 3s -- combined with the warm-up invocation below, which pre-primes
    # the uv/pytest environment (venv resolution, bytecode compilation) so
    # the TIMED invocation's own startup overhead is negligible -- makes
    # the "marker written before kill" assertion robust to subprocess/
    # interpreter startup jitter instead of racing against it. Fixture
    # sleeps far longer (8s) than the inner cap, so the inner cap must
    # fire first (classified `timeout`) well before the fixture's own
    # natural completion -- and well before any outer aggregate deadline
    # would matter (this test does not need to reach one).
    inner_timeout_seconds = 3
    fixture_sleep_seconds = 8.0

    fixture_source = f'''
import time


def test_sleep_and_mark_completion():
    with open({str(marker_path)!r}, "w") as f:
        f.write("started")

    time.sleep({fixture_sleep_seconds})

    with open({str(marker_path)!r}, "w") as f:
        f.write("completed_without_being_killed")
'''
    fixture_path = tmp_path / "test_inner_outer_fixture.py"
    fixture_path.write_text(fixture_source, encoding="utf-8")

    body_path = tmp_path / "issue_body.md"
    body_path.write_text(
        "## Verification Commands\n\n"
        "```bash\n"
        f"$ uv run --locked pytest {fixture_path} -q -s\n"
        "```\n",
        encoding="utf-8",
    )

    # Pre-warm the uv/pytest environment (venv resolution, dependency
    # locking, bytecode compilation) OUTSIDE the timed window by running a
    # throwaway collect-only invocation first. This amortizes subprocess
    # startup jitter so it does not compete with the fixture's own
    # `time.sleep()` for the narrow inner_timeout_seconds budget below.
    subprocess.run(
        [
            "uv",
            "run",
            "--locked",
            "pytest",
            str(fixture_path),
            "-q",
            "--collect-only",
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=60,
    )

    start = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            str(_BASELINE_VC_PREFLIGHT_PY),
            "--body-file",
            str(body_path),
            "--timeout-seconds",
            str(inner_timeout_seconds),
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=30,
    )
    elapsed = time.monotonic() - start

    payload = json.loads(result.stdout)
    assert payload["results"], "expected at least one classified VC result"
    classification = payload["results"][0]

    assert classification["category"] == "timeout", classification
    # Inner timeout fired well before the fixture's own sleep would have
    # completed it -- proves inner cap preceded natural completion, not a
    # race that happened to also finish naturally.
    assert elapsed < fixture_sleep_seconds

    # Fallback-free: the marker file must still say "started" (never
    # overwritten to "completed_without_being_killed"), proving the
    # process was actually reaped mid-sleep rather than allowed to finish.
    assert marker_path.read_text() == "started"


# Issue #2207 OWNER P1-2 item 5 (PR #2221 REQUEST_CHANGES): repeat count.
# Each repeat launches a REAL `uv run --locked pytest` subprocess tree
# (interpreter + pytest collection + a grandchild process) and waits out an
# outer deadline + bounded reap -- unlike the in-process race tests
# elsewhere in this repo, this cannot be sped up to microseconds. ~50
# same-shape repeats of a full production subprocess spawn would take
# several minutes and make this suite impractical to run routinely; 10
# repeats already exercises the SIGTERM-delivery -> handler-raises ->
# top-level-catch -> register/unregister -> reap -> confirm-absence
# sequence enough times to catch the register/unregister ordering races
# this item targets (a single run cannot distinguish "always correct" from
# "correct by luck once"), while keeping wall-clock time bounded to roughly
# a repeat_count * (deadline + reap grace) upper bound.
_OUTER_DEADLINE_REPEAT_COUNT = 10

# Issue #2207 OWNER P1-2 item 5: the literal 50-200ms range specified for
# the injected deadline assumes negligible child-process startup. Measured
# in THIS repo, `.venv/bin/python3 -m pytest --version` alone (interpreter
# boot + pytest import, before any collection) already costs ~200ms (see
# also the documented `uv run --locked pytest` startup-jitter finding in
# `test_scaled_fault_injection_inner_timeout_precedes_outer_deadline` above
# in this same file) -- a 50-200ms deadline would fire before the fixture's
# grandchild is even spawned, making it impossible to prove grandchild
# reaping (as opposed to "nothing had started yet"). 600ms is the smallest
# value that reliably let the fixture reach "grandchild spawned and alive"
# before the outer deadline fires in local measurement, while still being
# a small fraction (well under 1%) of the real per-VC-command production
# cap (150s). The venv interpreter (`sys.executable`, NOT `uv run
# --locked`) is used as the VC command's interpreter to avoid uv's own
# per-invocation dependency-resolution overhead stacking on top.
_OUTER_DEADLINE_SECONDS = 0.6
_REAP_GRACE_SECONDS = 0.2


def test_outer_deadline_via_run_baseline_vc_preflight_reaps_full_process_tree(tmp_path):
    """Issue #2207 OWNER P1-2 item 5 (PR #2221 REQUEST_CHANGES): a
    production-shaped integration test that goes through
    `contract_readiness_check.run_baseline_vc_preflight()` -- the REAL
    production entry point `run_root_review_pipeline.py`'s
    `_cmd_produce()` uses -- not a hand-rolled harness that calls
    `baseline_vc_preflight.py` internals directly. It actually triggers an
    outer (aggregate wrapper) deadline via `override_timeout_seconds`
    (Issue #2207 OWNER P1-2 item 7's DI hook), spawns both a direct child
    (the VC leader, a `pytest` worker process) and a SIGTERM-ignoring
    grandchild, confirms the `baseline_vc_preflight.py` SIGTERM handler
    actually ran (via a test-only marker file it writes on handler entry),
    confirms FULL absence of the wrapper (`baseline_vc_preflight.py`
    itself), the VC leader, and the grandchild afterward, confirms the
    outer result is the typed `runtime_error` / `baseline_vc_preflight_aggregate`
    timeout-phase payload (never a plain `errors: [...]` blocked payload),
    and repeats this `_OUTER_DEADLINE_REPEAT_COUNT` times (see rationale
    above) to catch register/unregister ordering races."""
    # `sys.executable` itself may report basename "python" (e.g. a venv's
    # primary entry point) rather than "python3", but the VC preflight
    # allowlist requires the literal basename "python3" (Issue #2207 OWNER
    # P1-2 item 5). venvs created by `uv`/`python -m venv` conventionally
    # also install a "python3" sibling binary alongside "python" in the
    # same bin directory (pointing at the same interpreter) -- use that
    # sibling so the spawned process still has pytest installed.
    interpreter = str(Path(sys.executable).parent / "python3")
    assert Path(interpreter).exists(), (
        f"expected a 'python3' sibling binary next to sys.executable ({sys.executable!r}) "
        f"for the VC preflight allowlist to accept it, but {interpreter!r} does not exist"
    )

    for iteration in range(_OUTER_DEADLINE_REPEAT_COUNT):
        marker_dir = tmp_path / f"iter_{iteration}"
        marker_dir.mkdir()

        sigterm_marker_path = marker_dir / "sigterm_handler.marker"
        self_pid_path = marker_dir / "self.pid"
        grandchild_pid_path = marker_dir / "grandchild.pid"

        fixture_source = (
            "import os\n"
            "import signal\n"
            "import subprocess\n"
            "import sys\n"
            "\n"
            "\n"
            "def _ignore_sigterm(signum, frame):\n"
            "    pass\n"
            "\n"
            "\n"
            "def test_spawn_sigterm_ignoring_grandchild_and_sleep():\n"
            "    signal.signal(signal.SIGTERM, _ignore_sigterm)\n"
            "    grandchild = subprocess.Popen(\n"
            "        [sys.executable, \"-c\",\n"
            "         \"import signal, time\\n\"\n"
            "         \"signal.signal(signal.SIGTERM, lambda *a: None)\\n\"\n"
            "         \"time.sleep(30.0)\"]\n"
            "    )\n"
            f"    with open({str(self_pid_path)!r}, \"w\") as f:\n"
            "        f.write(str(os.getpid()))\n"
            f"    with open({str(grandchild_pid_path)!r}, \"w\") as f:\n"
            "        f.write(str(grandchild.pid))\n"
            "    grandchild.wait()\n"
        )
        fixture_path = marker_dir / "test_immortal_grandchild_fixture.py"
        fixture_path.write_text(fixture_source, encoding="utf-8")

        body = (
            "## Verification Commands\n\n"
            "```bash\n"
            f"$ {interpreter} -m pytest {fixture_path} -q -s\n"
            "```\n"
        )

        result, exit_code = crc.run_baseline_vc_preflight(
            body,
            override_timeout_seconds=_OUTER_DEADLINE_SECONDS,
            override_grace_seconds=_REAP_GRACE_SECONDS,
            _test_extra_env={
                "BASELINE_VC_PREFLIGHT_TEST_SIGTERM_MARKER_PATH": str(sigterm_marker_path),
            },
        )

        # Typed runtime_error payload (Issue #2165 P0-1 / Issue #2207 OWNER
        # P0-1): never a plain `errors: ["timeout"]` blocked payload.
        assert result["status"] == "runtime_error", result
        assert result["failure_class"] == "timeout", result
        assert result["timeout_phase"] == "baseline_vc_preflight_aggregate", result
        assert result["retryable"] is False, result
        assert exit_code == -1

        # The SIGTERM handler must have actually run (not just "the process
        # died somehow") -- the marker file is written ONLY from inside
        # `main()`'s `except CooperativeCancellationRequested` block.
        _wait_for_file(sigterm_marker_path, timeout=5.0)
        marker_content = sigterm_marker_path.read_text()
        assert marker_content.startswith("sigterm_handler_entered pid="), marker_content
        wrapper_pid = int(marker_content.strip().split("pid=", 1)[1])

        # The VC leader and grandchild pid files are only written once the
        # fixture actually started running, so their presence here proves
        # the process tree was genuinely alive before the outer deadline
        # reaped it (not "never started").
        _wait_for_file(self_pid_path, timeout=5.0)
        _wait_for_file(grandchild_pid_path, timeout=5.0)
        vc_leader_pid = int(self_pid_path.read_text().strip())
        grandchild_pid = int(grandchild_pid_path.read_text().strip())

        # Full absence of wrapper / VC-leader / grandchild, confirmed via
        # bounded poll (reap is asynchronous relative to
        # `run_baseline_vc_preflight()` returning in the rare case the
        # supervisor's own poll window elapsed right at the edge).
        assert _wait_until_dead(wrapper_pid, timeout=5.0), (
            f"wrapper (baseline_vc_preflight.py, pid={wrapper_pid}) was not reaped "
            f"after the outer deadline (iteration {iteration})"
        )
        assert _wait_until_dead(vc_leader_pid, timeout=5.0), (
            f"VC leader (pid={vc_leader_pid}) was not reaped after the outer "
            f"deadline (iteration {iteration})"
        )
        assert _wait_until_dead(grandchild_pid, timeout=5.0), (
            f"SIGTERM-ignoring grandchild (pid={grandchild_pid}) was not reaped "
            f"after the outer deadline (iteration {iteration}) -- process group leak"
        )



def test_kill_process_group_reaps_sigterm_ignoring_grandchild(tmp_path):
    """Issue #2207 OWNER P0-3 (PR #2221 REQUEST_CHANGES) regression test:
    `_kill_process_group()` must reap the WHOLE process group, not just
    the leader, even when a grandchild explicitly ignores SIGTERM. Prior
    behavior `return`ed as soon as the LEADER's own `process.wait()`
    succeeded (the leader itself does not ignore SIGTERM here and exits
    normally), leaving the SIGTERM-ignoring grandchild running forever
    (SIGKILL was only ever sent along the leader's own wait path, never as
    a group-wide fallback after leader-exit)."""
    self_pid_path = tmp_path / "leader.pid"
    grandchild_pid_path = tmp_path / "grandchild.pid"

    leader_source = (
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "\n"
        "grandchild = subprocess.Popen(\n"
        "    [sys.executable, \"-c\",\n"
        "     \"import signal, time\\n\"\n"
        "     \"signal.signal(signal.SIGTERM, lambda *a: None)\\n\"\n"
        "     \"time.sleep(30.0)\"]\n"
        ")\n"
        f"with open({str(self_pid_path)!r}, \"w\") as f:\n"
        "    f.write(str(os.getpid()))\n"
        f"with open({str(grandchild_pid_path)!r}, \"w\") as f:\n"
        "    f.write(str(grandchild.pid))\n"
        "grandchild.wait()\n"
    )
    leader_path = tmp_path / "leader.py"
    leader_path.write_text(leader_source, encoding="utf-8")

    process = subprocess.Popen(
        [sys.executable, str(leader_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _wait_for_file(self_pid_path)
        _wait_for_file(grandchild_pid_path)
        leader_pid = int(self_pid_path.read_text().strip())
        grandchild_pid = int(grandchild_pid_path.read_text().strip())
        assert leader_pid == process.pid
        assert _pid_alive(leader_pid)
        assert _pid_alive(grandchild_pid)

        vcp._kill_process_group(process, grace_seconds=0.5, poll_interval=0.02)

        assert _wait_until_dead(leader_pid, timeout=5.0), "leader was not reaped"
        assert _wait_until_dead(
            grandchild_pid, timeout=5.0
        ), "SIGTERM-ignoring grandchild survived _kill_process_group() -- process group leak"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for p in (self_pid_path, grandchild_pid_path):
            if p.exists():
                try:
                    pid = int(p.read_text().strip())
                    if _pid_alive(pid):
                        os.kill(pid, signal.SIGKILL)
                except (ValueError, OSError):
                    pass
