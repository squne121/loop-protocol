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

    # Inner (per-command) timeout: 1s (integer-seconds CLI contract). Fixture
    # sleeps far longer (5s) than the inner cap, so the inner cap must fire
    # first (classified `timeout`) well before the fixture's own natural
    # completion -- and well before any outer aggregate deadline would
    # matter (this test does not need to reach one).
    inner_timeout_seconds = 1
    fixture_sleep_seconds = 5.0

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
