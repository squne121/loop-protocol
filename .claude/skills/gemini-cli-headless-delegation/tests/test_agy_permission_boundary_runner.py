"""Hermetic integration tests for the Issue #1814 dedicated runner."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
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


def _fake_agy(path: Path, *, exit_code: int = 0) -> None:
    """Create a fake runtime which dispatches configured hooks, never events."""
    script = rf"""#!/usr/bin/env python3
import json
import os
import pathlib
import shlex
import subprocess
import sys

if sys.argv[1:] == ["--version"]:
    print("fake-agy 1.1.9")
    raise SystemExit(0)
if sys.argv[1:] != ["--print", "permission-boundary-harness"]:
    raise SystemExit(12)
home = pathlib.Path(os.environ["HOME"])
hooks = json.loads((home / ".gemini" / "config" / "hooks.json").read_text())
workspace = pathlib.Path(os.getcwd())
base = {{"conversationId": "conversation-hook", "invocationNum": 7, "workspacePaths": [str(workspace)]}}

def invoke(command, payload):
    if isinstance(command, str): command = shlex.split(command)
    result = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True, env=os.environ.copy(), check=False)  # noqa: E501
    if result.returncode:
        raise SystemExit(result.returncode)
    return json.loads(result.stdout)

injection = hooks["permission-boundary-injector"]["PreInvocation"][0]["command"]
steps = invoke([injection], base)["injectSteps"]
for index, step in enumerate(steps):
    tool_call = step["toolCall"]
    payload = {{"toolCall": tool_call, "stepIdx": index, "conversationId": base["conversationId"], "workspacePaths": base["workspacePaths"]}}  # noqa: E501
    dispatched = False
    decision = {{"decision": "deny"}}
    for item in hooks["permission-boundary-enforcement"]["PreToolUse"]:
        if item["matcher"] == tool_call["name"]:
            dispatched = True
            decision = invoke(item["hooks"][0]["command"], payload)
            break
    if not dispatched or decision.get("decision") != "allow":
        continue
    canary = tool_call["args"]["canaryPath"]
    pathlib.Path(canary).write_text("1\\n", encoding="utf-8")
    for item in hooks["permission-boundary-postlogger"]["PostToolUse"]:
        if item["matcher"] == tool_call["name"]:
            invoke(item["hooks"][0]["command"], payload)
raise SystemExit({exit_code})
"""
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


def test_hermetic_fake_dispatches_actual_hooks_and_parent_observes_all_denied_cells(tmp_path: Path) -> None:
    fake = tmp_path / "fake-agy"
    _fake_agy(fake)
    run = _run(tmp_path, fake)
    artifact = _artifact(tmp_path)
    assert run.returncode == 1  # a fake can validate lifecycle, never complete live evidence
    assert artifact["runner"]["actual_agy_executed"] is False
    assert artifact["failure_taxonomy"]["class"] == MODULE.FAILURE_INCONCLUSIVE
    assert artifact["matrix"]["capabilities"] == list(MODULE.CAPABILITIES)
    assert {attempt["correlation"]["capability"] for attempt in artifact["attempts"]} == set(MODULE.CAPABILITIES)
    for attempt in artifact["attempts"]:
        assert attempt["predicates"] == {
            "deterministic_attempt_present": True,
            "pre_tool_use_present": True,
            "explicit_deny": True,
            "post_tool_use_absent": True,
            "side_effect_invariant": True,
        }
    assert MODULE.validate_artifact(artifact) == (True, "valid")


def test_nonzero_child_return_code_cannot_pass_even_when_hooks_deny(tmp_path: Path) -> None:
    fake = tmp_path / "fake-agy"
    _fake_agy(fake, exit_code=9)
    run = _run(tmp_path, fake)
    artifact = _artifact(tmp_path)
    assert run.returncode == 1
    assert artifact["failure_taxonomy"]["class"] == MODULE.FAILURE_INCONCLUSIVE
    assert artifact["runner"]["child_returncode"] == 9


def test_fake_dispatches_registered_posttooluse_only_after_an_allowed_attempt(tmp_path: Path) -> None:
    fake = tmp_path / "fake-agy"
    _fake_agy(fake)
    assert _run(tmp_path, fake, "--profile", "grounded_research").returncode == 1
    network = next(item for item in _artifact(tmp_path)["attempts"] if item["correlation"]["capability"] == "network")
    assert network["predicates"]["pre_tool_use_present"] is True
    assert network["predicates"]["explicit_deny"] is False
    assert network["predicates"]["post_tool_use_absent"] is False
    assert network["predicates"]["side_effect_invariant"] is False


def test_live_rejects_agy_override_before_execution(tmp_path: Path) -> None:
    fake = tmp_path / "not-agy"
    _fake_agy(fake)
    run = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--artifact-dir",
            str(tmp_path / "artifacts"),
            "--mode",
            "live",
            "--allow-live",
            "--agy",
            str(fake),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert run.returncode == 1
    assert _artifact(tmp_path)["failure_taxonomy"]["class"] == "agy_permission_boundary_invalid_live_identity"


def test_live_without_preconfirmed_runtime_is_exit_77(tmp_path: Path) -> None:
    run = subprocess.run(
        [sys.executable, str(RUNNER), "--artifact-dir", str(tmp_path / "artifacts"), "--mode", "live"],
        text=True,
        capture_output=True,
        check=False,
    )
    artifact = _artifact(tmp_path)
    assert run.returncode == 77
    assert artifact["failure_taxonomy"]["class"] == MODULE.FAILURE_UNAVAILABLE
    assert artifact["runner"]["actual_agy_executed"] is False


def test_live_authentication_unavailable_is_exit_77_without_capability_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "agy"
    _fake_agy(fake)
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: str(fake))
    monkeypatch.setattr(MODULE, "_version", lambda _path: ("agy 1.1.9", True))
    monkeypatch.setattr(
        MODULE,
        "_invoke",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["agy"], 1, "", "authentication required"),
    )
    exit_code, artifact = MODULE._run(
        argparse.Namespace(mode="live", agy=None, allow_live=True, profile="no_tools", artifact_dir=tmp_path)
    )
    assert exit_code == 77
    assert artifact["failure_taxonomy"] == {
        "class": MODULE.FAILURE_UNAVAILABLE,
        "completion": False,
        "retry": "restore_runtime",
    }
    assert artifact["runner"]["actual_agy_executed"] is False
    assert MODULE.validate_artifact(artifact) == (True, "valid")


def test_main_preserves_schema_valid_live_auth_unavailable_exit_77(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "agy"
    _fake_agy(fake)
    artifact_dir = tmp_path / "artifacts"
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: str(fake))
    monkeypatch.setattr(MODULE, "_version", lambda _path: ("agy 1.1.9", True))
    monkeypatch.setattr(
        MODULE,
        "_invoke",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["agy"], 1, "", "authentication required"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [str(RUNNER), "--artifact-dir", str(artifact_dir), "--mode", "live", "--allow-live"],
    )
    assert MODULE.main() == 77
    artifact = json.loads((artifact_dir / "agy_permission_boundary_e2e.json").read_text())
    assert artifact["runner"]["exit_code"] == 77
    assert artifact["runner"]["actual_agy_executed"] is False
    assert artifact["failure_taxonomy"]["class"] == MODULE.FAILURE_UNAVAILABLE
    assert MODULE.validate_artifact(artifact) == (True, "valid")


def test_live_uses_supported_isolated_auth_bootstrap_and_bwrap_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_home = tmp_path / "real-home"
    token_dir = real_home / ".gemini" / "antigravity-cli"
    token_dir.mkdir(parents=True)
    (token_dir / "antigravity-oauth-token").write_text("fixture-token", encoding="utf-8")
    monkeypatch.setenv("HOME", str(real_home))
    policy = MODULE._load_policy_module()
    monkeypatch.setattr(MODULE, "_load_policy_module", lambda: policy)
    monkeypatch.setattr(policy, "_bwrap_available", lambda: True)

    runtime = MODULE._prepare_runtime(tmp_path / "runtime", "no_tools", auth_bootstrap=True)

    assert runtime["env"]["HOME"] == str(runtime["home"])
    assert Path(runtime["env"]["HOME"]) != real_home
    assert (Path(runtime["env"]["HOME"]) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").is_symlink()
    assert runtime["agy_command_prefix"][0] == "bwrap"

    observed: dict[str, object] = {}

    def capture(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(MODULE.subprocess, "run", capture)
    MODULE._invoke(tmp_path / "agy", runtime, live=True)

    assert observed["command"] == runtime["agy_command_prefix"] + [
        str(tmp_path / "agy"),
        "--print",
        "permission-boundary-harness",
    ]
    assert observed["env"] is not None
    assert observed["env"]["HOME"] == runtime["env"]["HOME"]  # type: ignore[index]
    assert observed["env"]["HOME"] != str(real_home)  # type: ignore[index]


def test_live_auth_bootstrap_unavailable_is_exit_77_before_hook_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "agy"
    _fake_agy(fake)
    real_home = tmp_path / "real-home"
    token_dir = real_home / ".gemini" / "antigravity-cli"
    token_dir.mkdir(parents=True)
    (token_dir / "antigravity-oauth-token").write_text("fixture-token", encoding="utf-8")
    monkeypatch.setenv("HOME", str(real_home))
    policy = MODULE._load_policy_module()
    monkeypatch.setattr(MODULE, "_load_policy_module", lambda: policy)
    monkeypatch.setattr(policy, "_bwrap_available", lambda: False)
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: str(fake))
    monkeypatch.setattr(MODULE, "_version", lambda _path: ("agy 1.1.9", True))

    exit_code, artifact = MODULE._run(
        argparse.Namespace(mode="live", agy=None, allow_live=True, profile="no_tools", artifact_dir=tmp_path)
    )

    assert exit_code == 77
    assert artifact["failure_taxonomy"]["class"] == MODULE.FAILURE_UNAVAILABLE
    assert artifact["runner"]["actual_agy_executed"] is False


def test_live_hook_nonfire_is_inconclusive_not_auth_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "agy"
    _fake_agy(fake)
    runtime = MODULE._prepare_runtime(tmp_path / "runtime", "no_tools")
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: str(fake))
    monkeypatch.setattr(MODULE, "_version", lambda _path: ("agy 1.1.9", True))
    monkeypatch.setattr(MODULE, "_prepare_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(
        MODULE,
        "_invoke",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(["agy"], 0, "", ""),
    )

    exit_code, artifact = MODULE._run(
        argparse.Namespace(mode="live", agy=None, allow_live=True, profile="no_tools", artifact_dir=tmp_path)
    )

    assert exit_code == 1
    assert artifact["failure_taxonomy"]["class"] == MODULE.FAILURE_INCONCLUSIVE
    assert artifact["runner"]["actual_agy_executed"] is True
    assert all(not attempt["predicates"]["pre_tool_use_present"] for attempt in artifact["attempts"])


def test_live_runtime_launch_failure_is_exit_77_but_unknown_runner_error_is_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "agy"
    _fake_agy(fake)
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: str(fake))
    monkeypatch.setattr(MODULE, "_version", lambda _path: ("agy 1.1.9", True))
    monkeypatch.setattr(MODULE, "_invoke", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unavailable")))
    exit_code, artifact = MODULE._run(
        argparse.Namespace(mode="live", agy=None, allow_live=True, profile="no_tools", artifact_dir=tmp_path)
    )
    assert exit_code == 77
    assert artifact["runner"]["actual_agy_executed"] is False
    assert MODULE.validate_artifact(artifact) == (True, "valid")
    monkeypatch.setattr(MODULE, "_invoke", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("unknown")))
    exit_code, artifact = MODULE._run(
        argparse.Namespace(mode="live", agy=None, allow_live=True, profile="no_tools", artifact_dir=tmp_path)
    )
    assert exit_code == 1
    assert artifact["failure_taxonomy"]["class"] == MODULE.FAILURE_INCONCLUSIVE
    assert MODULE.validate_artifact(artifact) == (True, "valid")


def test_main_writes_structured_exit_1_artifact_and_cleans_runtime_after_runner_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "fake-agy"
    _fake_agy(fake)

    def fail_after_residue(root: Path, _profile: str) -> dict[str, object]:
        (root / "control").mkdir()
        (root / "control" / "residue").write_text("x", encoding="utf-8")
        raise RuntimeError("injected_runtime_failure")

    artifact_dir = tmp_path / "artifacts"
    monkeypatch.setattr(MODULE, "_prepare_runtime", fail_after_residue)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(RUNNER), "--artifact-dir", str(artifact_dir), "--mode", "hermetic", "--agy", str(fake)],
    )
    assert MODULE.main() == 1
    artifact = json.loads((artifact_dir / "agy_permission_boundary_e2e.json").read_text())
    assert artifact["runner"]["exit_code"] == 1
    assert artifact["cleanup"] == {"temporary_processes_removed": True, "loopback_servers_stopped": True}
    assert not list(artifact_dir.glob("agy-boundary-*"))


def test_main_converts_validator_exception_to_structured_exit_1_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_dir = tmp_path / "artifacts"
    monkeypatch.setattr(MODULE, "validate_artifact", lambda _artifact: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(sys, "argv", [str(RUNNER), "--artifact-dir", str(artifact_dir), "--mode", "live"])
    assert MODULE.main() == 1
    artifact = json.loads((artifact_dir / "agy_permission_boundary_e2e.json").read_text())
    assert artifact["runner"]["exit_code"] == 1
    assert artifact["failure_taxonomy"]["class"] == "agy_permission_boundary_validator_exception"


def test_evidence_artifact_conforms_to_declared_json_schema_and_runtime_validator(tmp_path: Path) -> None:
    fake = tmp_path / "fake-agy"
    _fake_agy(fake)
    assert _run(tmp_path, fake).returncode == 1
    artifact = _artifact(tmp_path)
    schema = json.loads(MODULE.SCHEMA_PATH.read_text())
    assert list(Draft202012Validator(schema).iter_errors(artifact)) == []
    artifact["attempts"] = []
    assert MODULE.validate_artifact(artifact)[0] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cleanup", {"temporary_processes_removed": False, "loopback_servers_stopped": True}),
        ("failure_taxonomy", {"class": "none", "completion": True, "retry": "none"}),
    ],
)
def test_validator_rejects_exit_and_cleanup_invariant_violations(tmp_path: Path, field: str, value: object) -> None:
    fake = tmp_path / "fake-agy"
    _fake_agy(fake)
    assert _run(tmp_path, fake).returncode == 1
    artifact = _artifact(tmp_path)
    artifact[field] = value
    if field == "cleanup":
        artifact["runner"]["exit_code"] = 77
        artifact["failure_taxonomy"] = {
            "class": MODULE.FAILURE_UNAVAILABLE,
            "completion": False,
            "retry": "restore_runtime",
        }
    artifact["artifact"]["digest"] = MODULE._artifact_digest(artifact)
    artifact["runner"]["artifact_digest"] = artifact["artifact"]["digest"]
    assert MODULE.validate_artifact(artifact)[0] is False


def test_evidence_artifact_rejects_stdout_only_or_secret_data() -> None:
    artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE)
    artifact["stdout_claim"] = "denied"
    assert MODULE.validate_artifact(artifact)[0] is False
    del artifact["stdout_claim"]
    artifact["runner"]["executable_version"] = "agy-boundary-canary-secret"
    artifact["artifact"]["digest"] = MODULE._artifact_digest(artifact)
    artifact["runner"]["artifact_digest"] = artifact["artifact"]["digest"]
    assert MODULE.validate_artifact(artifact)[1] == "secret_or_absolute_path_detected"
