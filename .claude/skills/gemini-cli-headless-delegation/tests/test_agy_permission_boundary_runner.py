"""Hermetic integration tests for the Issue #1814 dedicated runner."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

RUNNER = Path(__file__).parents[1] / "scripts" / "run_agy_permission_boundary_e2e.py"
SPEC = importlib.util.spec_from_file_location("agy_permission_boundary_runner", RUNNER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _fake_agy(path: Path, *, emit_events: bool = True) -> None:
    script = r'''#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import sys

capture = os.environ.get("AGY_BOUNDARY_TEST_CAPTURE")
if capture:
    pathlib.Path(capture).write_text(json.dumps({
        "argv": sys.argv[1:], "cwd": os.getcwd(),
        "home": os.environ.get("HOME"),
        "context": os.environ.get("AGY_PERMISSION_BOUNDARY_CONTEXT_PATH"),
        "no_fallback": os.environ.get("AGY_PERMISSION_BOUNDARY_NO_FALLBACK"),
    }), encoding="utf-8")
if sys.argv[1:] == ["--version"]:
    print("fake-agy 1.1.9")
    raise SystemExit(0)
if sys.argv[1:] != ["--print", "permission-boundary-harness"]:
    raise SystemExit(12)
home = pathlib.Path(os.environ["HOME"])
assert (home / ".gemini" / "antigravity-cli" / "settings.json").is_file()
hooks = json.loads((home / ".gemini" / "config" / "hooks.json").read_text())
assert set(hooks) == {"permission-boundary-injector", "permission-boundary-enforcement"}
assert hooks["permission-boundary-injector"]["PreInvocation"][0]["type"] == "command"
assert hooks["permission-boundary-enforcement"]["PreToolUse"][0]["matcher"] == "run_command"
if os.environ.get("FAKE_AGY_EMIT_EVENTS") == "0":
    raise SystemExit(0)
context = json.loads(pathlib.Path(os.environ["AGY_PERMISSION_BOUNDARY_CONTEXT_PATH"]).read_text())
args = {
    "CommandLine": "sh -c 'printf 1 >> .agy-boundary-command-counter'",
    "Cwd": context["workspace"],
    "WaitMsBeforeAsync": 1000,
}
args_digest = "sha256:" + hashlib.sha256(
    json.dumps(args, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
common = {
    "run_id": context["run_id"], "conversation_id": context["conversation_id"],
    "step_index": 0, "tool_name": "run_command", "args_digest": args_digest,
    "profile": context["tool_profile"], "capability": "command",
    "canary_id": os.environ["AGY_PERMISSION_BOUNDARY_CANARY_ID"],
}
events = [
    {"kind": "pre_invocation", **common},
    {"kind": "pre_tool_use", "decision": "deny", **common},
    {"kind": "side_effects", "counters": {
        name: {"before": 0, "after": 0}
        for name in ("command", "write", "read", "network", "mcp")
    }},
]
with pathlib.Path(os.environ["AGY_PERMISSION_BOUNDARY_EVENT_PATH"]).open("w", encoding="utf-8") as output:
    for event in events:
        output.write(json.dumps(event, separators=(",", ":")) + "\n")
'''
    path.write_text(script, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def _run(tmp_path: Path, fake: Path | None, *extra: str) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(RUNNER), "--artifact-dir", str(tmp_path / "artifacts"), "--mode", "hermetic"]
    if fake is not None:
        command.extend(["--agy", str(fake)])
    command.extend(extra)
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _artifact(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "artifacts" / "agy_permission_boundary_e2e.json").read_text())


def test_official_settings_deny_precedence() -> None:
    policy = MODULE._load_policy_module()
    hostile = policy.build_official_agy_settings(policy.NO_TOOLS_PROFILE)
    hostile["permissions"]["allow"].append("command(*)")
    hostile["permissions"]["ask"].append("command(*)")
    assert policy.resolve_official_permission_action(hostile, "command", "anything") == "deny"


def test_missing_injected_attempt_is_inconclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "fake-agy"
    _fake_agy(fake)
    monkeypatch.setenv("FAKE_AGY_EMIT_EVENTS", "0")
    run = _run(tmp_path, fake)
    artifact = _artifact(tmp_path)
    assert run.returncode == 1
    assert artifact["failure_taxonomy"]["class"] == MODULE.FAILURE_INCONCLUSIVE
    assert artifact["attempts"][0]["predicates"]["deterministic_attempt_present"] is False


def test_hermetic_runner_requires_all_evidence_predicates(tmp_path: Path) -> None:
    fake = tmp_path / "fake-agy"
    _fake_agy(fake)
    run = _run(tmp_path, fake)
    artifact = _artifact(tmp_path)
    assert run.returncode == 0
    assert artifact["runner"]["actual_agy_executed"] is False
    assert all(artifact["attempts"][0]["predicates"].values())
    assert artifact["fallback"]["used"] is False
    assert MODULE.validate_artifact(artifact) == (True, "valid")


def test_evidence_artifact_conforms_to_declared_json_schema(tmp_path: Path) -> None:
    fake = tmp_path / "fake-agy"
    _fake_agy(fake)
    assert _run(tmp_path, fake).returncode == 0
    schema = json.loads(MODULE.SCHEMA_PATH.read_text())
    errors = list(Draft202012Validator(schema).iter_errors(_artifact(tmp_path)))
    assert errors == []


def test_fake_agy_exact_argv_env_and_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "fake-agy"
    capture = tmp_path / "capture.json"
    _fake_agy(fake)
    monkeypatch.setenv("AGY_BOUNDARY_TEST_CAPTURE", str(capture))
    assert _run(tmp_path, fake).returncode == 0
    observed = json.loads(capture.read_text())
    assert observed["argv"] == ["--print", "permission-boundary-harness"]
    assert Path(observed["cwd"]).name == "workspace"
    assert Path(observed["home"]).name == "home"
    assert Path(observed["context"]).name == "run-context.json"
    assert observed["no_fallback"] == "1"


def test_fake_agy_that_never_emits_lifecycle_events_is_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "fake-agy"
    _fake_agy(fake)
    monkeypatch.setenv("FAKE_AGY_EMIT_EVENTS", "0")
    assert _run(tmp_path, fake).returncode == 1


def test_live_without_preconfirmed_runtime_is_exit_77(tmp_path: Path) -> None:
    run = subprocess.run(
        [sys.executable, str(RUNNER), "--artifact-dir", str(tmp_path / "artifacts"), "--mode", "live"],
        text=True, capture_output=True, check=False,
    )
    artifact = _artifact(tmp_path)
    assert run.returncode == 77
    assert artifact["failure_taxonomy"]["class"] == MODULE.FAILURE_UNAVAILABLE
    assert artifact["runner"]["actual_agy_executed"] is False


def test_evidence_artifact_rejects_stdout_only_or_secret_data() -> None:
    attempt = {"correlation": {}, "predicates": {"deterministic_attempt_present": False}}
    artifact = MODULE._artifact(
        exit_code=1, actual_agy=False, executable=None, version="unavailable", attempt=attempt,
        failure_class=MODULE.FAILURE_INCONCLUSIVE, cleanup_ok=True,
    )
    artifact["stdout_claim"] = "denied"  # not a schema field and never evidence
    assert MODULE.validate_artifact(artifact)[0] is False
    del artifact["stdout_claim"]
    artifact["runner"]["executable_version"] = "agy-boundary-canary-secret"
    artifact["artifact"]["digest"] = MODULE._artifact_digest(artifact)
    artifact["runner"]["artifact_digest"] = artifact["artifact"]["digest"]
    assert MODULE.validate_artifact(artifact)[1] == "secret_or_absolute_path_detected"
