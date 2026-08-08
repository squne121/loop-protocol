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
    subprocess -- `subprocess.run` is called with argv containing the broker
    script path and the `execute` subcommand -- and never calls
    `broker.execute_operation()` in-process."""
    captured: dict[str, object] = {}

    class _FakeCompleted:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def _fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
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
        return _FakeCompleted(json.dumps(record, sort_keys=True))

    monkeypatch.setattr(e2e.subprocess, "run", _fake_run)

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
    # Real subprocess API (Popen/run), never a bare shell string.
    assert isinstance(argv[0], str) and argv[0] == sys.executable
    assert captured["kwargs"].get("shell", False) is False


def test_ac1_negative_probe_validation_never_spawns_a_subprocess(e2e, monkeypatch):
    """Pre-execution `validate_operation()` calls (negative probes, and the
    per-turn allow/deny check) must never themselves spawn a subprocess --
    only an *allowed* operation reaches `_execute_via_broker_subprocess()`."""

    def _boom(*_a, **_k):
        raise AssertionError("validate_operation() must never spawn a subprocess")

    monkeypatch.setattr(e2e.subprocess, "run", _boom)
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
    values only.)"""
    sentinel_token = "SENTINEL-FAKE-GH-TOKEN-0123456789abcdef"  # noqa: S105 - test fixture, not a real secret
    monkeypatch.setenv("GH_TOKEN", sentinel_token)
    fake_gh = _write_fake_gh(tmp_path)

    captured: dict[str, object] = {}
    real_run = subprocess.run

    def _spy_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env_kwarg"] = kwargs.get("env")
        captured["stdin"] = kwargs.get("stdin")
        completed = real_run(argv, **kwargs)
        captured["stdout"] = completed.stdout
        captured["stderr"] = completed.stderr
        return completed

    monkeypatch.setattr(e2e.subprocess, "run", _spy_run)

    result = e2e._execute_via_broker_subprocess("get_repo", {}, gh_bin=str(fake_gh), timeout_seconds=10)

    # The broker's own redacted result crossed back over stdout -- but never
    # contains the raw token.
    assert sentinel_token not in json.dumps(result)

    # The parent's own explicit subprocess-spawn surface never carries the
    # raw token: argv, explicit `env=` kwarg (there is none -- default
    # inheritance is used instead of the parent reading/forwarding it
    # itself), and stdin.
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert all(sentinel_token not in str(item) for item in argv)
    assert captured["env_kwarg"] is None
    assert captured["stdin"] is subprocess.DEVNULL
    assert sentinel_token not in str(captured.get("stdout", ""))
    assert sentinel_token not in str(captured.get("stderr", ""))


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
