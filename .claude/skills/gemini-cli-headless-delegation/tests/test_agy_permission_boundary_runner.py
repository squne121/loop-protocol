"""Hermetic integration tests for the Issue #1814 dedicated runner."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import importlib.util
import json
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

RUNNER = Path(__file__).parents[1] / "scripts" / "run_agy_permission_boundary_e2e.py"
SPEC = importlib.util.spec_from_file_location("agy_permission_boundary_runner", RUNNER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _supported_capability_gate() -> dict[str, object]:
    """Issue #1979 AC2 test double: a bootstrap predicate reported `supported`.

    Used to exercise post-gate live-runner behavior in isolation from the
    real (currently `inconclusive`, deferred-to-live-run) gate result.
    """
    return {
        "bootstrap_predicate": "pre_invocation_ephemeral_message_injection",
        "predicate_kind": "bootstrap_prerequisite",
        "status": "supported",
        "reason_code": "test_override_supported",
        "evidence_source": "runtime_semantic_observation",
    }


def _invoke_result(returncode: int | None, stdout: str = "", stderr: str = "", *, timed_out: bool = False) -> dict[str, object]:
    """Issue #1979 AC7: build a fake `_invoke` dict return value for tests."""
    return {
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "process_group_isolated": True,
        "descendant_processes_absent": True,
    }


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
        if not require_keys(args, ("Url",)) or not isinstance(args["Url"], str):
            return False
        response = urllib.request.urlopen(args["Url"], timeout=2)
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
        if json.loads(line)["tool_name"] == "read_url_content"
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
    # Issue #1979: `Description` is required -- confirmed via a live investigative
    # probe that the real AGY `write_to_file` tool call always includes it, and that
    # a fixed literal value is reproduced byte-for-byte (see run_agy_permission_
    # boundary_e2e.py `attempt_args["write"]` comment for the empirical evidence).
    assert runtime["attempt_args"]["write"].keys() == {"TargetFile", "Overwrite", "CodeContent", "Description"}
    assert runtime["attempt_args"]["read"].keys() == {"AbsolutePath"}
    assert runtime["attempt_args"]["network"].keys() == {"Url"}
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
    # Issue #1979 (2026-08-04 revision): the gate now only short-circuits on
    # a genuine `unsupported` bootstrap predicate, not `inconclusive` -- so
    # this test (which targets the auth-failure-output detection path, not
    # the capability gate) supplies an explicit `supported` override to
    # isolate that behavior from the real predicate's `inconclusive` result.
    monkeypatch.setattr(MODULE, "_bootstrap_capability_gate", _supported_capability_gate)
    monkeypatch.setattr(
        MODULE,
        "_invoke",
        lambda *_args, **_kwargs: _invoke_result(1, stderr="authentication required"),
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
    monkeypatch.setattr(MODULE, "_bootstrap_capability_gate", _supported_capability_gate)
    monkeypatch.setattr(
        MODULE,
        "_invoke",
        lambda *_args, **_kwargs: _invoke_result(1, stderr="authentication required"),
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

    class _FakePopen:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            observed["command"] = command
            observed["env"] = kwargs.get("env")
            self.pid = 999999
            self.returncode = 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "", ""

    monkeypatch.setattr(MODULE.subprocess, "Popen", _FakePopen)
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


def test_live_hook_nonfire_is_prompt_noncompliant_not_auth_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #1979 AC8: when `_invoke` never produces any `PreToolUse`
    evidence across the bounded retry budget, every capability ends up
    prompt-noncompliant -- `EXIT_PROMPT_NONCOMPLIANT`(78), never the old
    `FAILURE_INCONCLUSIVE`/exit-1 (which would silently conflate "hook never
    fired" with "prompt not complied with")."""
    fake = tmp_path / "agy"
    _fake_agy(fake)
    runtime = MODULE._prepare_runtime(tmp_path / "runtime", "no_tools")
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: str(fake))
    monkeypatch.setattr(MODULE, "_version", lambda _path: ("agy 1.1.9", True))
    monkeypatch.setattr(MODULE, "_prepare_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(MODULE, "_bootstrap_capability_gate", _supported_capability_gate)
    monkeypatch.setattr(MODULE, "_invoke", lambda *_args, **_kwargs: _invoke_result(0))

    exit_code, artifact = MODULE._run(
        argparse.Namespace(mode="live", agy=None, allow_live=True, profile="no_tools", artifact_dir=tmp_path)
    )

    assert exit_code == MODULE.EXIT_PROMPT_NONCOMPLIANT
    assert artifact["failure_taxonomy"]["class"] == MODULE.FAILURE_PROMPT_NONCOMPLIANT
    assert artifact["runner"]["actual_agy_executed"] is True
    assert all(not attempt["predicates"]["pre_tool_use_present"] for attempt in artifact["attempts"])
    assert set(artifact["prompt_compliance"]) == set(MODULE.CAPABILITIES)
    for record in artifact["prompt_compliance"].values():
        assert record["compliant"] is False
        assert record["attempts"] == 3
    assert artifact["diagnostic_ledger"] == {
        "pre_invocation_hook_started": False,
        "pre_invocation_context_accepted": False,
        "injected_step_count": 0,
        "enforcement_event_count": 0,
        "pre_tool_use_event_count": 0,
        "post_tool_use_event_count": 0,
        "raw_payload_persisted": False,
    }


def test_missing_injected_attempt_is_prompt_noncompliant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #1979 AC8: an absent injected attempt (no `PreToolUse` ever
    observed for a capability across the bounded retry budget) is a distinct
    prompt-noncompliance non-completion path -- `EXIT_PROMPT_NONCOMPLIANT`(78),
    never silently scored as an allow/deny verdict."""
    fake = tmp_path / "agy"
    _fake_agy(fake)
    runtime = MODULE._prepare_runtime(tmp_path / "runtime", "no_tools")
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: str(fake))
    monkeypatch.setattr(MODULE, "_version", lambda _path: ("agy 1.1.9", True))
    monkeypatch.setattr(MODULE, "_prepare_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(MODULE, "_bootstrap_capability_gate", _supported_capability_gate)
    monkeypatch.setattr(MODULE, "_invoke", lambda *_args, **_kwargs: _invoke_result(0))

    exit_code, artifact = MODULE._run(
        argparse.Namespace(mode="live", agy=None, allow_live=True, profile="no_tools", artifact_dir=tmp_path)
    )

    assert exit_code == MODULE.EXIT_PROMPT_NONCOMPLIANT
    assert artifact["failure_taxonomy"] == {
        "class": MODULE.FAILURE_PROMPT_NONCOMPLIANT,
        "completion": False,
        "retry": "reattempt_prompt",
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
    assert set(artifact["prompt_compliance"]) == set(MODULE.CAPABILITIES)
    assert all(record["compliant"] is False for record in artifact["prompt_compliance"].values())
    assert MODULE.validate_artifact(artifact) == (True, "valid")


def test_live_runtime_launch_failure_is_exit_77_but_unknown_runner_error_is_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "agy"
    _fake_agy(fake)
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: str(fake))
    monkeypatch.setattr(MODULE, "_version", lambda _path: ("agy 1.1.9", True))
    monkeypatch.setattr(MODULE, "_bootstrap_capability_gate", _supported_capability_gate)
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
    assert artifact["cleanup"] == {
        "temporary_processes_removed": True,
        "loopback_servers_stopped": True,
        "process_group_isolated": True,
        "descendant_processes_absent": True,
    }
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


def _write_pre_tool_use_event(
    runtime: dict[str, object],
    *,
    profile: str,
    capability: str,
    conversation_id: str,
    step_index: int,
    decision: str,
) -> None:
    """Issue #1979: append a schema-shaped `agy_permission_boundary_hook/v1`
    enforcement-log entry directly, bypassing the real hook process, so the
    parent-side correlation/characterization logic in
    `_attempts_from_parent_observation` can be unit-tested without a live or
    fake AGY binary."""
    tool_name, _ = MODULE.ATTEMPT_SPECS[capability]
    args = runtime["attempt_args"][capability]  # type: ignore[index]
    event = {
        "schema": "agy_permission_boundary_hook/v1",
        "decision": decision,
        "run_id": runtime["run_id"],
        "conversation_id": conversation_id,
        "step_index": step_index,
        "tool_profile": profile,
        "canary_id": runtime["canary_id"],
        "tool_name": tool_name,
        "args_digest": MODULE._sha256(MODULE._canonical_json(args)),
    }
    with open(runtime["enforcement_log"], "a", encoding="utf-8") as output:  # type: ignore[arg-type]
        output.write(json.dumps(event, separators=(",", ":")) + "\n")


def _write_post_tool_use_event(
    runtime: dict[str, object],
    *,
    profile: str,
    conversation_id: str | None,
    step_index: int | None,
    status: str = "recorded",
    extra: dict[str, object] | None = None,
) -> None:
    """Issue #1979: append a schema-shaped PostToolUse occurrence event
    directly (mirrors what the real `posttooluse_logger.py` persists)."""
    event: dict[str, object] = {
        "kind": "post_tool_use",
        "status": status,
        "run_id": runtime["run_id"],
        "canary_id": runtime["canary_id"],
        "tool_profile": profile,
    }
    if conversation_id is not None:
        event["conversation_id"] = conversation_id
    if step_index is not None:
        event["step_index"] = step_index
    if extra:
        event.update(extra)
    with open(runtime["events_path"], "a", encoding="utf-8") as output:  # type: ignore[arg-type]
        output.write(json.dumps(event, separators=(",", ":")) + "\n")


def test_deny_correlated_posttooluse_is_characterized_not_failed(tmp_path: Path) -> None:
    """Issue #1979: real AGY may still dispatch a correlated, secret-safe
    PostToolUse event after an explicit PreToolUse deny. This must be
    characterized and recorded, not treated as a fixed mismatch -- all
    predicates for the attempt still pass."""
    runtime = MODULE._prepare_runtime(tmp_path / "runtime", "no_tools")
    _write_pre_tool_use_event(
        runtime, profile="no_tools", capability="command", conversation_id="conv-1", step_index=0, decision="deny"
    )
    _write_post_tool_use_event(runtime, profile="no_tools", conversation_id="conv-1", step_index=0)

    attempts = MODULE._attempts_from_parent_observation(runtime, "no_tools")
    command = next(item for item in attempts if item["correlation"]["capability"] == "command")

    assert command["expectation"] == "deny"
    assert command["predicates"] == {
        "deterministic_attempt_present": True,
        "pre_tool_use_present": True,
        "decision_matches_expectation": True,
        "post_tool_use_matches_expectation": True,
        "side_effect_matches_expectation": True,
        "same_attempt_correlation": True,
        "logger_failure_absent": True,
    }
    assert command["deny_post_tool_use_characterization"] == {
        "applicable": True,
        "observed": True,
        "correlated": True,
        "secret_scan_passed": True,
    }


def test_deny_uncorrelated_posttooluse_still_fails(tmp_path: Path) -> None:
    """Issue #1979: a PostToolUse occurrence that cannot be bound to the
    same attempt (different conversation/step) must never be silently
    accepted as expected -- this remains a genuine failure."""
    runtime = MODULE._prepare_runtime(tmp_path / "runtime", "no_tools")
    _write_pre_tool_use_event(
        runtime, profile="no_tools", capability="command", conversation_id="conv-1", step_index=0, decision="deny"
    )
    # A logger-failure-status event that cannot be correlated by conversation/step.
    _write_post_tool_use_event(
        runtime, profile="no_tools", conversation_id=None, step_index=None, status="parse_failure"
    )

    attempts = MODULE._attempts_from_parent_observation(runtime, "no_tools")
    command = next(item for item in attempts if item["correlation"]["capability"] == "command")

    assert command["predicates"]["post_tool_use_matches_expectation"] is False
    assert command["predicates"]["same_attempt_correlation"] is False
    assert command["predicates"]["logger_failure_absent"] is False
    assert command["deny_post_tool_use_characterization"]["correlated"] is False


def test_deny_correlated_posttooluse_with_secret_leak_fails(tmp_path: Path) -> None:
    """Issue #1979: a correlated PostToolUse occurrence that discloses a
    secret must fail the boundary check even though it correlates to the
    same attempt."""
    runtime = MODULE._prepare_runtime(tmp_path / "runtime", "no_tools")
    _write_pre_tool_use_event(
        runtime, profile="no_tools", capability="command", conversation_id="conv-1", step_index=0, decision="deny"
    )
    _write_post_tool_use_event(
        runtime,
        profile="no_tools",
        conversation_id="conv-1",
        step_index=0,
        extra={"leaked_field": MODULE.CANARY_SECRET},
    )

    attempts = MODULE._attempts_from_parent_observation(runtime, "no_tools")
    command = next(item for item in attempts if item["correlation"]["capability"] == "command")

    assert command["deny_post_tool_use_characterization"]["observed"] is True
    assert command["deny_post_tool_use_characterization"]["correlated"] is True
    assert command["deny_post_tool_use_characterization"]["secret_scan_passed"] is False
    assert command["predicates"]["post_tool_use_matches_expectation"] is False
    # Correlation itself is still intact -- only the secret-scan failed.
    assert command["predicates"]["same_attempt_correlation"] is True


def test_deny_no_posttooluse_remains_vacuously_characterized(tmp_path: Path) -> None:
    """Issue #1979: the pre-existing "no PostToolUse at all on deny"
    behavior must remain unaffected by the new characterization logic."""
    runtime = MODULE._prepare_runtime(tmp_path / "runtime", "no_tools")
    _write_pre_tool_use_event(
        runtime, profile="no_tools", capability="command", conversation_id="conv-1", step_index=0, decision="deny"
    )

    attempts = MODULE._attempts_from_parent_observation(runtime, "no_tools")
    command = next(item for item in attempts if item["correlation"]["capability"] == "command")

    assert all(command["predicates"].values())
    assert command["deny_post_tool_use_characterization"] == {
        "applicable": True,
        "observed": False,
        "correlated": True,
        "secret_scan_passed": True,
    }


def test_allow_attempt_characterization_is_not_applicable(tmp_path: Path) -> None:
    """Issue #1979: the deny-only characterization field is inert (but still
    schema-present) for allow-expectation attempts."""
    runtime = MODULE._prepare_runtime(tmp_path / "runtime", "grounded_research")
    _write_pre_tool_use_event(
        runtime,
        profile="grounded_research",
        capability="network",
        conversation_id="conv-1",
        step_index=3,
        decision="allow",
    )
    _write_post_tool_use_event(runtime, profile="grounded_research", conversation_id="conv-1", step_index=3)

    attempts = MODULE._attempts_from_parent_observation(runtime, "grounded_research")
    network = next(item for item in attempts if item["correlation"]["capability"] == "network")

    assert network["expectation"] == "allow"
    assert network["predicates"]["post_tool_use_matches_expectation"] is True
    assert network["deny_post_tool_use_characterization"] == {
        "applicable": False,
        "observed": False,
        "correlated": True,
        "secret_scan_passed": True,
    }


def test_deny_post_tool_use_characterization_schema_valid(tmp_path: Path) -> None:
    """Issue #1979 AC6: the new additive field must validate against the
    schema, and a live-shaped artifact carrying it must still be schema-valid."""
    fake = tmp_path / "fake-agy"
    _fake_agy(fake)
    assert _run(tmp_path, fake, "--profile", "no_tools").returncode == 1
    artifact = _artifact(tmp_path)
    for attempt in artifact["attempts"]:
        assert set(attempt["deny_post_tool_use_characterization"]) == {
            "applicable",
            "observed",
            "correlated",
            "secret_scan_passed",
        }
    assert MODULE.validate_artifact(artifact) == (True, "valid")


def test_loopback_canary_stops_cleanly_and_promptly_after_a_real_hit(tmp_path: Path) -> None:
    """Issue #1979 fix_delta regression test.

    Driving ``_LoopbackCanary`` through an actual GET request (as happens
    whenever ``read_url_content`` is genuinely invoked in the allow profile)
    must not leave ``stop()`` unbounded.  Before the fix,
    ``socketserver.ThreadingMixIn.server_close()``'s default
    ``block_on_close=True`` behaviour performed an *unbounded* join over the
    per-request handler thread spawned to serve that hit -- a race distinct
    from (and not bounded by) the ``self._thread.join(timeout=5)`` applied to
    the ``serve_forever`` accept-loop thread.  This test would have caught
    that: it exercises the real request-then-shutdown path and asserts both a
    ``True`` result and a bounded wall-clock cost.
    """
    counter_path = tmp_path / "network-canary-counter"
    counter_path.write_text("0\n", encoding="utf-8")
    canary = MODULE._LoopbackCanary(counter_path, run_id="test-run", canary_id="test-canary")
    try:
        response = urllib.request.urlopen(canary.url, timeout=5)
        assert response.status == 200
        assert counter_path.read_text(encoding="utf-8").strip() == "1"
    finally:
        started_at = time.time()
        stopped = canary.stop()
        elapsed = time.time() - started_at

    assert stopped is True
    assert elapsed < 5.0, f"loopback canary shutdown after a real hit took {elapsed:.3f}s (expected < 5.0s)"
    assert canary._thread.is_alive() is False


def test_cleanup_evidence_fields_are_independent_not_a_shared_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #1979 fix_delta regression test.

    ``loopback_servers_stopped`` and ``temporary_processes_removed`` used to
    be backed by a single shared ``cleanup_ok`` boolean, so an unrelated
    temp-directory removal failure would silently also report the loopback
    server as unstopped (and vice versa).  Forcing only the ``shutil.rmtree``
    step to fail must leave ``loopback_servers_stopped`` ``True`` while
    ``temporary_processes_removed`` is ``False``.
    """
    fake = tmp_path / "fake-agy"
    _fake_agy(fake)

    def _raise_rmtree(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated temp directory removal failure")

    monkeypatch.setattr(MODULE.shutil, "rmtree", _raise_rmtree)
    exit_code, artifact = MODULE._run(
        argparse.Namespace(mode="hermetic", agy=str(fake), allow_live=False, profile="grounded_research", artifact_dir=tmp_path)
    )
    assert exit_code == 1
    assert artifact["cleanup"]["temporary_processes_removed"] is False
    assert artifact["cleanup"]["loopback_servers_stopped"] is True
