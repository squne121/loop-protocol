"""Broker subprocess boundary contract (Issue #2012 AC1 / AC2).

`run_agy_github_research_e2e.py` (the E2E/orchestrator process) must invoke
`run_agy_github_research_broker.py` as a real, independent OS subprocess (its
`execute` CLI subcommand) rather than importing `run_agy_github_research_broker`
and calling `execute_operation()` in-process. The orchestrator process must
never read GH_TOKEN/GITHUB_TOKEN itself, never hold a raw token value, and
never forward one via argv, an explicit `env=` kwarg, or stdin when spawning
the broker subprocess.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str, filename: str) -> types.ModuleType:
    path = _SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture()
def e2e(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module = _load(f"run_agy_github_research_e2e_boundary_{id(tmp_path)}", "run_agy_github_research_e2e.py")
    monkeypatch.setattr(module, "_agy_version_and_permission_gate", lambda _bin: (True, None, {}))
    return module


def _write_fake_gh(tmp_path: Path) -> Path:
    """A trivial fake `gh` executable: succeeds on any subcommand, never
    needs (and never sees) a real token because the ambient GH_TOKEN env
    var is provisioned directly for these tests."""
    script = tmp_path / "fake-gh"
    script.write_text("#!/bin/sh\necho 'fake gh output'\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def test_ac1_broker_is_invoked_as_real_subprocess(e2e, monkeypatch):
    """AC1: `run_agy_github_research_e2e.py` invokes the broker as a real OS
    subprocess -- `subprocess.Popen` is called with argv containing the
    broker script path and the `execute` subcommand -- and never calls
    `broker.execute_operation()` in-process."""
    captured: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs) -> None:
            captured["argv"] = list(argv)
            captured["kwargs"] = kwargs
            self.pid = 999999

        def communicate(self, timeout=None):  # noqa: ARG002
            record = {
                "schema": e2e.broker.SCHEMA_COMMAND_RESULT,
                "operation": "get_repo",
                "argv": ["repo", "view", "github.com/squne121/loop-protocol"],
                "exit_code": 0,
                "timed_out": False,
                "duration_ms": 5,
                "truncated": False,
                "output_limit_exceeded": False,
                "redacted_stdout_sample": "fake gh output",
                "redacted_stderr_sample": "",
                "redacted_output_digest": "sha256:fake",
            }
            return json.dumps(record, sort_keys=True), ""

    monkeypatch.setattr(e2e.subprocess, "Popen", _FakePopen)

    def _boom(*_a, **_k):
        raise AssertionError("broker.execute_operation() must never be called in-process by the E2E orchestrator")

    monkeypatch.setattr(e2e.broker, "execute_operation", _boom)

    result = e2e._execute_via_broker_subprocess("get_repo", {}, gh_bin="/usr/bin/gh", timeout_seconds=10)
    assert result["exit_code"] == 0
    assert result["schema"] == e2e.broker.SCHEMA_COMMAND_RESULT

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert str(e2e._BROKER_SCRIPT_PATH) in argv
    assert "execute" in argv
    assert "get_repo" in argv
    # Real subprocess API (Popen), never a bare shell string.
    assert isinstance(argv[0], str) and argv[0] == sys.executable
    assert captured["kwargs"].get("shell", False) is False
    # Started in its own session/process group so a parent-side timeout can
    # terminate the whole broker session, not merely its direct pid (Issue
    # #2036 P0-3).
    assert captured["kwargs"].get("start_new_session") is True


def test_ac1_negative_probe_validation_never_spawns_a_subprocess(e2e, monkeypatch):
    """Pre-execution `validate_operation()` calls (negative probes, and the
    per-turn allow/deny check) must never themselves spawn a subprocess --
    only an *allowed* operation reaches `_execute_via_broker_subprocess()`."""

    def _boom(*_a, **_k):
        raise AssertionError("validate_operation() must never spawn a subprocess")

    monkeypatch.setattr(e2e.subprocess, "Popen", _boom)
    probes = e2e._run_negative_probes()
    assert probes
    assert all(probe["denied_pre_execution"] is True for probe in probes)


def test_ac2_parent_process_never_receives_or_forwards_raw_token(e2e, monkeypatch, tmp_path):
    """AC2: using a fake credential source (an ambient GH_TOKEN env var
    provisioned directly, simulating an already-authenticated shell) and a
    fake `gh` executable, the parent (E2E) process's own explicit
    subprocess-spawn surface -- argv, any explicit `env=` kwarg, and stdin --
    must never carry the raw token value. (Ordinary OS process-env
    inheritance, which the parent code never reads/touches, is a distinct,
    accepted pass-through -- this test asserts on the parent's *own* explicit
    values only.)

    Issue #2036 AC2 fix_delta: this is a genuine falsification of OS-level
    env-inheritance, not merely an assertion on a mocked call's `env=`
    kwarg. A sentinel token is planted in *this test process's own* ambient
    environment (simulating an operator's shell-level `GH_TOKEN`), a real
    `gh auth token` subprocess boundary is crossed for real, and the fake
    `gh` binary self-reports (never the sentinel value itself, only a
    bounded PRESENT/ABSENT diagnostic) whether it observed a `GH_TOKEN` key
    at all in its own runtime environment -- proving the broker subprocess
    (and, transitively, its own credential-bootstrap child) never inherited
    the parent's ambient secret."""
    sentinel_token = "SENTINEL-FAKE-GH-TOKEN-0123456789abcdef"  # noqa: S105 - test fixture, not a real secret
    monkeypatch.setenv("GH_TOKEN", sentinel_token)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    env_report_log = tmp_path / "env-report.log"
    fake_gh = tmp_path / "env-reporting-gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"log_path = {str(env_report_log)!r}\n"
        "presence = 'PRESENT' if 'GH_TOKEN' in os.environ else 'ABSENT'\n"
        "with open(log_path, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(' '.join(sys.argv[1:3]) + ':' + presence + chr(10))\n"
        "if len(sys.argv) >= 3 and sys.argv[1] == 'auth' and sys.argv[2] == 'token':\n"
        "    print('bootstrapped-non-sentinel-token-0123456789abcdef')\n"
        "    sys.exit(0)\n"
        "print('fake gh research output')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)

    captured: dict[str, object] = {}
    real_popen = subprocess.Popen

    class _SpyPopen(real_popen):
        def __init__(self, argv, **kwargs):
            captured["argv"] = list(argv)
            captured["env_kwarg"] = kwargs.get("env")
            captured["stdin"] = kwargs.get("stdin")
            super().__init__(argv, **kwargs)

    monkeypatch.setattr(e2e.subprocess, "Popen", _SpyPopen)

    result = e2e._execute_via_broker_subprocess("get_repo", {}, gh_bin=str(fake_gh), timeout_seconds=10)

    # The broker's own redacted result crossed back over stdout -- but never
    # contains the raw sentinel token (a *different*, non-sentinel
    # bootstrapped token is legitimately provisioned to the research `gh`
    # call itself -- that is expected and is not what this test asserts
    # against).
    assert sentinel_token not in json.dumps(result)

    # The parent's own explicit subprocess-spawn surface never carries the
    # raw sentinel token: argv, the explicit scrubbed `env=` kwarg it now
    # always passes, and stdin.
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert all(sentinel_token not in str(item) for item in argv)
    env_kwarg = captured["env_kwarg"]
    assert env_kwarg is not None
    assert "GH_TOKEN" not in env_kwarg
    assert "GITHUB_TOKEN" not in env_kwarg
    assert captured["stdin"] is subprocess.DEVNULL

    # The falsifying proof: the credential-bootstrap child (`gh auth token`)
    # never observed a `GH_TOKEN` key in its own runtime environment at all
    # -- not merely a different value -- confirming true non-inheritance
    # across the parent -> broker subprocess boundary.
    report_lines = env_report_log.read_text().splitlines()
    bootstrap_lines = [line for line in report_lines if line.startswith("auth token:")]
    assert bootstrap_lines, "expected the fake gh's credential-bootstrap branch to have run"
    assert all(line == "auth token:ABSENT" for line in bootstrap_lines)


def test_ac2_preflight_never_reads_gh_token_itself(e2e, monkeypatch):
    """`_preflight()` never fetches GH_TOKEN/GITHUB_TOKEN from its own
    process environment -- it only observes whether the broker subprocess
    (which owns credential resolution) can complete a probe operation."""
    monkeypatch.setenv("GH_TOKEN", "must-never-be-read-by-preflight")  # noqa: S105
    monkeypatch.setattr(e2e, "_resolve_agy_binary", lambda: "/usr/bin/agy")
    monkeypatch.setattr(e2e, "_resolve_gh_binary", lambda: "/usr/bin/gh")

    real_get = e2e.os.environ.get

    def _guarded_get(key, *args, **kwargs):
        if key in ("GH_TOKEN", "GITHUB_TOKEN"):
            raise AssertionError(f"_preflight() must never read {key} itself")
        return real_get(key, *args, **kwargs)

    monkeypatch.setattr(e2e.os.environ, "get", _guarded_get)
    monkeypatch.setattr(
        e2e,
        "_execute_via_broker_subprocess",
        lambda *_a, **_k: {"schema": e2e.broker.SCHEMA_COMMAND_RESULT, "exit_code": 0},
    )
    ok, reason = e2e._preflight(gh_token_env="GH_TOKEN")
    assert ok is True
    assert reason is None


# ---------------------------------------------------------------------------
# Issue #2036 P0-3: on a genuine parent-side timeout, the entire broker
# session -- including a downstream child spawned in its *own* separate
# session/process group (as the real broker's research `gh` invocation is,
# via `start_new_session=True`) -- must be reaped, never left orphaned.
# ---------------------------------------------------------------------------


def test_p0_3_parent_side_timeout_reaps_entire_broker_session_including_detached_child(e2e, monkeypatch, tmp_path):
    """Real-process test (no mocked `Popen`/`communicate`): a stand-in
    "broker" process is spawned exactly the way `_execute_via_broker_subprocess`
    spawns the real broker (`start_new_session=True`, no stdout ever
    written so the parent's `communicate(timeout=...)` genuinely expires).
    It implements the same downstream-cleanup *contract* the real broker's
    `install_termination_cleanup_handler()` / `_handle_broker_sigterm()`
    implement: it spawns its own child (representing the research `gh`
    process) in a *separate* session via `start_new_session=True`, records
    that child's pid, and installs a SIGTERM handler that kills that
    child's own process group before exiting. This isolates and proves the
    parent's own timeout-kill logic (`_terminate_broker_process_group`)
    correctly reaches the stand-in broker via `killpg`, and that -- given a
    broker that honors the cleanup contract -- zero descendants remain
    after cleanup, verified via `/proc` (not a self-report)."""
    child_pid_marker = tmp_path / "child-pid-marker"
    stand_in_broker = tmp_path / "stand_in_broker.py"
    stand_in_broker.write_text(
        "import os, signal, subprocess, sys, time\n"
        "child = subprocess.Popen(\n"
        "    ['sleep', '300'],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        "    start_new_session=True,\n"
        ")\n"
        f"with open({str(child_pid_marker)!r}, 'w', encoding='utf-8') as fh:\n"
        "    fh.write(str(child.pid))\n"
        "\n"
        "def _cascade_sigterm(signum, frame):\n"
        "    try:\n"
        "        pgid = os.getpgid(child.pid)\n"
        "        os.killpg(pgid, signal.SIGTERM)\n"
        "        time.sleep(0.2)\n"
        "        os.killpg(pgid, signal.SIGKILL)\n"
        "    except ProcessLookupError:\n"
        "        pass\n"
        "    raise SystemExit(143)\n"
        "\n"
        "signal.signal(signal.SIGTERM, _cascade_sigterm)\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(e2e, "_BROKER_SCRIPT_PATH", stand_in_broker)
    # Force the parent's own wait budget to be tiny so this test completes
    # quickly -- the stand-in broker never writes to stdout, so
    # `proc.communicate(timeout=...)` always genuinely expires.
    monkeypatch.setattr(e2e, "_PARENT_BROKER_WAIT_FIXED_OVERHEAD_SECONDS", 0.2)

    with pytest.raises(e2e.broker.BrokerTransportTimeout):
        e2e._execute_via_broker_subprocess("get_repo", {}, gh_bin="gh", timeout_seconds=0.3)

    assert child_pid_marker.exists(), "expected the stand-in broker to have recorded its child's pid"
    child_pid = int(child_pid_marker.read_text().strip())

    # Bounded poll: the child (spawned in its own separate session) must no
    # longer exist -- proving the parent's timeout-kill reached the entire
    # broker session (not merely the broker's own direct pid) and the
    # broker's own SIGTERM handler cascaded to its detached child.
    deadline = time.monotonic() + 5.0
    still_alive = True
    while time.monotonic() < deadline:
        if not Path(f"/proc/{child_pid}").exists():
            still_alive = False
            break
        time.sleep(0.1)
    assert not still_alive, f"child pid {child_pid} (in its own session) survived the parent-side timeout cleanup"
