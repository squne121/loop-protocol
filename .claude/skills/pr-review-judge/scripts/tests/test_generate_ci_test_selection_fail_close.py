"""Fail-close + plan-parity tests for generate_ci_test_selection_artifact.py (Issue #1064).

The generator must:
- derive its pytest scope argv from the python-test-plan SSOT (AC2/AC7),
- include schemas/tests/ exactly once (AC7),
- fail-close (non-zero exit) and record collection_status when pytest --collect-only
  exits non-zero, times out, or collects zero nodeids (AC6).
"""

import argparse
import importlib.util
import json
import subprocess
import types
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[5]
_GEN_PATH = (
    _REPO_ROOT
    / ".claude"
    / "skills"
    / "pr-review-judge"
    / "scripts"
    / "generate_ci_test_selection_artifact.py"
)


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_under_test", _GEN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


gen = _load_gen()


def _fake_completed(returncode, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# --- AC2 / AC7: plan-derived scope argv ---


def test_resolve_pytest_args_derives_from_plan():
    args = argparse.Namespace(pytest_args=None, plan=None)
    argv = gen.resolve_pytest_args(args)
    assert argv.count("schemas/tests/") == 1
    assert any(a.startswith("--ignore=") for a in argv)


def test_resolve_pytest_args_override_wins():
    args = argparse.Namespace(pytest_args=["only/this/"], plan=None)
    assert gen.resolve_pytest_args(args) == ["only/this/"]


# --- AC6: fail-close on collection failure ---


def test_collection_status_non_zero_is_not_ok(monkeypatch):
    monkeypatch.setattr(
        gen.subprocess, "run", lambda *a, **k: _fake_completed(2, stdout="", stderr="boom")
    )
    files, nodeids, status = gen.get_pytest_collected_tests(["x/"])
    assert status["ok"] is False
    assert status["returncode"] == 2
    assert status["nodeid_count"] == 0
    assert "boom" in status["stderr_tail"]


def test_collection_status_timeout_is_not_ok(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(gen.subprocess, "run", _raise)
    files, nodeids, status = gen.get_pytest_collected_tests(["x/"])
    assert status["ok"] is False
    assert status["timed_out"] is True


def test_collection_status_zero_nodeids_is_not_ok(monkeypatch):
    monkeypatch.setattr(
        gen.subprocess,
        "run",
        lambda *a, **k: _fake_completed(0, stdout="\n\n", stderr=""),
    )
    files, nodeids, status = gen.get_pytest_collected_tests(["x/"])
    assert status["ok"] is False
    assert status["nodeid_count"] == 0


def test_collection_status_ok_when_nodeids_present(monkeypatch):
    out = "pkg/test_a.py::test_one\npkg/test_a.py::test_two\n2 tests collected in 0.1s\n"
    monkeypatch.setattr(
        gen.subprocess, "run", lambda *a, **k: _fake_completed(0, stdout=out, stderr="")
    )
    files, nodeids, status = gen.get_pytest_collected_tests(["pkg/"])
    assert status["ok"] is True
    assert status["nodeid_count"] == 2
    assert "pkg/test_a.py::test_one" in nodeids
    # The pytest summary line must not be counted as a nodeid.
    assert all("collected" not in n for n in nodeids)


def test_generate_artifact_fail_closes_and_records_status(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gen, "get_pytest_collected_tests", lambda argv: ([], [], {
            "returncode": 1, "timed_out": False, "error": None,
            "nodeid_count": 0, "stderr_tail": "collect error", "ok": False,
        })
    )
    monkeypatch.setattr(gen, "get_changed_test_files", lambda *a, **k: ([], []))
    monkeypatch.setattr(gen, "get_current_head_sha", lambda: "deadbeef")
    out = tmp_path / "artifact.json"
    args = argparse.Namespace(
        output=str(out), pytest_args=["x/"], plan=None, pr_head_sha="h",
        checked_out_sha=None, merge_sha="m", workflow="ci", job="python-test",
        ci_run_url=None,
    )
    rc = gen.generate_artifact(args)
    assert rc == 2  # fail-closed
    data = json.loads(out.read_text())
    assert data["collection_status"]["ok"] is False
    assert data["pytest_argv"] == ["x/"]


def test_generate_artifact_succeeds_when_collection_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gen, "get_pytest_collected_tests", lambda argv: (
            ["pkg/test_a.py"], ["pkg/test_a.py::test_one"], {
                "returncode": 0, "timed_out": False, "error": None,
                "nodeid_count": 1, "stderr_tail": "", "ok": True,
            }
        )
    )
    monkeypatch.setattr(gen, "get_changed_test_files", lambda *a, **k: ([], []))
    monkeypatch.setattr(gen, "get_current_head_sha", lambda: "deadbeef")
    out = tmp_path / "artifact.json"
    args = argparse.Namespace(
        output=str(out), pytest_args=None, plan=None, pr_head_sha="h",
        checked_out_sha=None, merge_sha="m", workflow="ci", job="python-test",
        ci_run_url=None,
    )
    rc = gen.generate_artifact(args)
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["collection_status"]["ok"] is True
    # AC7: artifact pytest_argv derives from the plan SSOT and includes schemas once.
    assert data["pytest_argv"].count("schemas/tests/") == 1
