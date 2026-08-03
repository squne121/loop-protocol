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


def _fake_agy(path: Path, *, exit_code: int = 0, post_mode: str = "normal") -> None:
    """Create a fake runtime which dispatches configured hooks, never events."""
    script = """#!/usr/bin/env python3
import json
import os
import pathlib
import shlex
import subprocess
import urllib.request
import sys


if sys.argv[1:] == ["--version"]:
    print("fake-agy 1.1.9")
    raise SystemExit(0)
if len(sys.argv) < 4 or sys.argv[1:3] != ["--print", "permission-boundary-harness"] or sys.argv[3] != "--add-dir":
    raise SystemExit(12)


home = pathlib.Path(os.environ["HOME"])
hooks = json.loads((home / ".gemini" / "config" / "hooks.json").read_text())
workspace = pathlib.Path(os.getcwd())
base = {"conversationId": "conversation-hook", "invocationNum": 7, "workspacePaths": [str(workspace)]}
context = json.loads(pathlib.Path(os.environ["AGY_PERMISSION_BOUNDARY_CONTEXT_PATH"]).read_text())

def invoke(command, payload):
    if isinstance(command, str):
        command = shlex.split(command)
    input_data = payload if isinstance(payload, str) else json.dumps(payload)
    result = subprocess.run(command, input=input_data, text=True, capture_output=True, env=os.environ.copy(), check=False)
    return result

def require_keys(args, keys):
    return isinstance(args, dict) and set(args) == set(keys)

def execute(tool_call):
    name, args = tool_call["name"], tool_call["args"]
    capability = context["native_capabilities"].get(name)
    if capability == "command":
        if not require_keys(args, ("CommandLine", "Cwd", "WaitMsBeforeAsync")):
            return False
        if args["Cwd"] != str(workspace) or not isinstance(args["WaitMsBeforeAsync"], int):
            return False
        return subprocess.run(args["CommandLine"], cwd=args["Cwd"], shell=True, check=False).returncode == 0
    if capability == "write":
        if not require_keys(args, ("TargetFile", "Overwrite", "CodeContent")):
            return False
        target = pathlib.Path(args["TargetFile"])
        if target != pathlib.Path(context["canary_paths"]["write"]) or args["Overwrite"] is not True:
            return False
        target.write_text(args["CodeContent"], encoding="utf-8")
        return True
    if capability == "read":
        if not require_keys(args, ("AbsolutePath",)):
            return False
        target = pathlib.Path(args["AbsolutePath"])
        if target != pathlib.Path(context["canary_paths"]["read"]):
            return False
        target.read_text(encoding="utf-8")
        return True
    if capability == "network":
        if not require_keys(args, ("query",)) or not isinstance(args["query"], str):
            return False
        response = urllib.request.urlopen(args["query"], timeout=2)
        return response.status == 200
    return False

if "__POST_MODE__" == "start_failure":
    for item in hooks["permission-boundary-postlogger"]["PostToolUse"]:
        item["hooks"][0]["command"] = str(workspace / "nonexistent_post_logger_does_not_exist.py")

if "__POST_MODE__" == "nonzero":
    failing_logger = workspace / "forced_post_logger.py"
    failing_logger.write_text(
        "import json,os,sys\\n"
        "context=json.load(open(os.environ['AGY_PERMISSION_BOUNDARY_CONTEXT_PATH']))\\n"
        "payload=json.load(sys.stdin)\\n"
        "event={'kind':'post_tool_use','status':'logger_nonzero','run_id':context['run_id'],'canary_id':context['canary_id'],'tool_profile':context['tool_profile'],'conversation_id':payload.get('conversationId'),'step_index':payload.get('stepIdx')}\\n"
        "open(context['events_path'],'a').write(json.dumps(event,separators=(',',':'))+'\\\\n')\\n"
        "raise SystemExit(9)\\n",
        encoding="utf-8",
    )
    failing_logger.chmod(0o700)
    for item in hooks["permission-boundary-postlogger"]["PostToolUse"]:
        item["hooks"][0]["command"] = f"{sys.executable} {failing_logger}"
    (home / ".gemini" / "config" / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    hooks = json.loads((home / ".gemini" / "config" / "hooks.json").read_text())

injection = hooks["permission-boundary-injector"]["PreInvocation"][0]["command"]
injected = invoke([injection], base)
if injected.returncode:
    sys.stderr.write(injected.stderr)
    raise SystemExit(injected.returncode)
steps = json.loads(injected.stdout)["injectSteps"]
for index, step in enumerate(steps):
    tool_call = step["toolCall"]
    payload = {"toolCall": tool_call, "stepIdx": index, "conversationId": base["conversationId"], "workspacePaths": base["workspacePaths"]}
    matching = [item for item in hooks["permission-boundary-enforcement"]["PreToolUse"] if item["matcher"] == tool_call["name"]]
    if len(matching) != 1:
        raise SystemExit(14)
    pre = invoke(matching[0]["hooks"][0]["command"], payload)
    if pre.returncode:
        raise SystemExit(pre.returncode)
    decision = json.loads(pre.stdout)
    if decision.get("decision") != "allow":
        continue
    if not execute(tool_call):
        raise SystemExit(15)
    matching_post = [item for item in hooks["permission-boundary-postlogger"]["PostToolUse"] if item["matcher"] == tool_call["name"]]
    if len(matching_post) != 1:
        raise SystemExit(16)
    post_payload = {"conversationId": base["conversationId"], "stepIdx": index, "error": None}
    post_command = matching_post[0]["hooks"][0]["command"]
    try:
        post = invoke(post_command, "{malformed" if "__POST_MODE__" == "parse_failure" else post_payload)
    except OSError:
        # The dispatch source (this fake runtime), not the unstartable logger
        # itself, is the only party able to observe a spawn failure.
        dispatch_failure_event = {
            "kind": "post_tool_use",
            "status": "dispatch_start_failure",
            "run_id": context["run_id"],
            "canary_id": context["canary_id"],
            "tool_profile": context["tool_profile"],
        }
        with open(context["events_path"], "a") as events_output:
            events_output.write(json.dumps(dispatch_failure_event, separators=(",", ":")) + "\\n")
        post = None
    if post is not None and post.returncode and "__POST_MODE__" not in ("parse_failure", "nonzero"):
        raise SystemExit(post.returncode)
raise SystemExit(__EXIT_CODE__)
"""
    script = script.replace("__EXIT_CODE__", str(exit_code)).replace("__POST_MODE__", post_mode)
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
        assert attempt["expectation"] == "deny"
        assert all(attempt["predicates"].values())
    assert artifact["diagnostic_ledger"] == {
        "pre_invocation_hook_started": True,
        "pre_invocation_context_accepted": True,
        "injected_step_count": 4,
        "enforcement_event_count": 4,
        "pre_tool_use_event_count": 4,
        "post_tool_use_event_count": 0,
        "raw_payload_persisted": False,
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
    assert network["expectation"] == "allow"
    assert all(network["predicates"].values())


def test_allow_control_binds_exact_injected_loopback_effect_and_full_lifecycle_tuple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parent counter accepts only the query the injected tool received."""
    fake = tmp_path / "fake-agy"
    _fake_agy(fake)
    monkeypatch.setattr(MODULE.shutil, "rmtree", lambda *_args, **_kwargs: None)
    exit_code, artifact = MODULE._run(
        argparse.Namespace(mode="hermetic", agy=str(fake), allow_live=False, profile="grounded_research", artifact_dir=tmp_path)
    )
    assert exit_code == 1  # Hermetic evidence never claims a live completion.
    runtime = next(tmp_path.glob("agy-boundary-*"))
    network_counter = runtime / "workspace" / ".agy-boundary-network-sentinel"
    assert network_counter.read_text(encoding="utf-8") == "1\n"
    events = [json.loads(line) for line in (runtime / "control" / "events.jsonl").read_text().splitlines()]
    pre_events = [
        json.loads(line)
        for line in (runtime / "control" / "enforcement.jsonl").read_text().splitlines()
        if json.loads(line)["tool_name"] == "search_web"
    ]
    post_events = [event for event in events if event["kind"] == "post_tool_use"]
    assert len(pre_events) == len(post_events) == 1
    pre, post = pre_events[0], post_events[0]
    assert (pre["run_id"], pre["conversation_id"], pre["step_index"], pre["canary_id"]) == (
        post["run_id"],
        post["conversation_id"],
        post["step_index"],
        post["canary_id"],
    )
    assert pre["args_digest"] == artifact["attempts"][-1]["correlation"]["args_digest"]
    assert "toolCall" not in post


def test_denied_attempts_preserve_actual_sentinels_and_emit_no_posttooluse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "fake-agy"
    _fake_agy(fake)
    monkeypatch.setattr(MODULE.shutil, "rmtree", lambda *_args, **_kwargs: None)
    exit_code, _artifact_value = MODULE._run(
        argparse.Namespace(mode="hermetic", agy=str(fake), allow_live=False, profile="no_tools", artifact_dir=tmp_path)
    )
    assert exit_code == 1
    runtime = next(tmp_path.glob("agy-boundary-*"))
    assert {
        path.name: path.read_text(encoding="utf-8")
        for path in (runtime / "workspace").glob(".agy-boundary-*-sentinel")
    } == {
        ".agy-boundary-command-sentinel": "0\n",
        ".agy-boundary-write-sentinel": "0\n",
        ".agy-boundary-read-sentinel": "0\n",
        ".agy-boundary-network-sentinel": "0\n",
    }
    events_path = runtime / "control" / "events.jsonl"
    assert all(json.loads(line)["kind"] != "post_tool_use" for line in events_path.read_text().splitlines())


@pytest.mark.parametrize("post_mode", ["parse_failure", "nonzero", "start_failure"])
def test_posttooluse_logger_failure_is_inconclusive_not_expected_absence(tmp_path: Path, post_mode: str) -> None:
    fake = tmp_path / "fake-agy"
    _fake_agy(fake, post_mode=post_mode)
    run = _run(tmp_path, fake, "--profile", "grounded_research")
    artifact = _artifact(tmp_path)
    network = next(item for item in artifact["attempts"] if item["correlation"]["capability"] == "network")
    assert run.returncode == 1
    assert network["expectation"] == "allow"
    assert network["predicates"]["pre_tool_use_present"] is True
    assert network["predicates"]["logger_failure_absent"] is False
    assert network["predicates"]["post_tool_use_matches_expectation"] is False
    assert network["predicates"]["same_attempt_correlation"] is False
    assert artifact["failure_taxonomy"]["completion"] is False


def test_hermetic_attempts_use_documented_args_and_exclude_undiscovered_mcp(tmp_path: Path) -> None:
    runtime = MODULE._prepare_runtime(tmp_path / "runtime", "no_tools")
    assert set(runtime["attempt_args"]) == {"command", "write", "read", "network"}
    assert runtime["attempt_args"]["command"].keys() == {"CommandLine", "Cwd", "WaitMsBeforeAsync"}
    assert runtime["attempt_args"]["write"].keys() == {"TargetFile", "Overwrite", "CodeContent"}
    assert runtime["attempt_args"]["read"].keys() == {"AbsolutePath"}
    assert runtime["attempt_args"]["network"].keys() == {"query"}
    assert "mcp_call" not in MODULE.ATTEMPT_SPECS


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


def test_literal_ac5_command_is_cost_guarded_preflight_exit_77(tmp_path: Path) -> None:
    """The Issue command lacks --allow-live and must not invoke real AGY."""
    run = subprocess.run(
        [sys.executable, str(RUNNER), "--profile", "no_tools", "--artifact-dir", str(tmp_path / "artifacts")],
        text=True,
        capture_output=True,
        check=False,
    )
    artifact = _artifact(tmp_path)
    assert run.returncode == 77
    assert artifact["failure_taxonomy"]["class"] == MODULE.FAILURE_UNAVAILABLE
    assert artifact["runner"]["actual_agy_executed"] is False
    assert MODULE.validate_artifact(artifact) == (True, "valid")


def test_missing_allow_live_never_invokes_discovered_agy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "agy"
    _fake_agy(fake)
    artifact_dir = tmp_path / "artifacts"
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: str(fake))
    monkeypatch.setattr(
        MODULE,
        "_invoke",
        lambda *_args, **_kwargs: pytest.fail("cost guard must stop before AGY invocation"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [str(RUNNER), "--profile", "no_tools", "--artifact-dir", str(artifact_dir)],
    )

    assert MODULE.main() == 77
    artifact = json.loads((artifact_dir / "agy_permission_boundary_e2e.json").read_text())
    assert artifact["runner"]["actual_agy_executed"] is False
    assert artifact["failure_taxonomy"]["class"] == MODULE.FAILURE_UNAVAILABLE
    assert MODULE.validate_artifact(artifact) == (True, "valid")


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
        "--add-dir",
        str(runtime["workspace"]),
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
    assert artifact["diagnostic_ledger"] == {
        "pre_invocation_hook_started": False,
        "pre_invocation_context_accepted": False,
        "injected_step_count": 0,
        "enforcement_event_count": 0,
        "pre_tool_use_event_count": 0,
        "post_tool_use_event_count": 0,
        "raw_payload_persisted": False,
    }


def test_missing_injected_attempt_is_inconclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent injected attempt is boundary failure, never completion evidence."""
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
    assert artifact["failure_taxonomy"] == {
        "class": MODULE.FAILURE_INCONCLUSIVE,
        "completion": False,
        "retry": "fix_or_reprobe",
    }
    assert artifact["runner"]["actual_agy_executed"] is True
    for attempt in artifact["attempts"]:
        assert attempt["predicates"] == {
            "deterministic_attempt_present": False,
            "pre_tool_use_present": False,
            "decision_matches_expectation": False,
            "post_tool_use_matches_expectation": False,
            "side_effect_matches_expectation": True,
            "same_attempt_correlation": False,
            "logger_failure_absent": True,
        }
    assert MODULE.validate_artifact(artifact) == (True, "valid")


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


def test_evidence_artifact_rejects_raw_diagnostic_payload() -> None:
    artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE)
    artifact["diagnostic_ledger"]["raw_payload"] = {"toolCall": {"args": "forbidden"}}
    artifact["artifact"]["digest"] = MODULE._artifact_digest(artifact)
    artifact["runner"]["artifact_digest"] = artifact["artifact"]["digest"]
    assert MODULE.validate_artifact(artifact) == (False, "draft202012_invalid")
