#!/usr/bin/env python3

import json
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER = REPO_ROOT / "scripts" / "session-recording" / "codex-hook-adapter.mjs"
HOOKS_JSON = REPO_ROOT / ".codex" / "hooks.json"
HOOKS_VALIDATOR = REPO_ROOT / "scripts" / "session-recording" / "validate-codex-hooks.mjs"
MANIFEST_VALIDATOR = REPO_ROOT / "scripts" / "validate-agent-session-manifest.mjs"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "codex"
MANIFEST_ROOT = REPO_ROOT / "tmp" / "session-manifests" / "codex"


def run_adapter(event: str, payload, expect_exit: int = 0):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    result = subprocess.run(
        ["node", str(ADAPTER), "--event", event],
        input=data,
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == expect_exit, result.stderr
    return result


def setup_function():
    shutil.rmtree(MANIFEST_ROOT, ignore_errors=True)


def test_hook_config_positive_fixture():
    result = subprocess.run(
        ["node", str(HOOKS_VALIDATOR), str(HOOKS_JSON)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_positive_fixture_writes_private_manifest_and_returns_continue_true():
    payload = json.loads((FIXTURES / "positive_fixture.json").read_text())
    result = run_adapter("Stop", payload)
    assert json.loads(result.stdout) == {"continue": True}
    manifest_dir = MANIFEST_ROOT / "stop"
    files = sorted(manifest_dir.glob("*.json"))
    assert len(files) == 1

    validation = subprocess.run(
        ["node", str(MANIFEST_VALIDATOR), str(manifest_dir)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert validation.returncode == 0, validation.stderr


def test_kill_switch_public_checkpoint_stop():
    payload = json.loads((FIXTURES / "public_checkpoint_enabled.json").read_text())
    result = run_adapter("Stop", payload)
    response = json.loads(result.stdout)
    assert response["continue"] is False
    assert "public checkpoint" in response["stopReason"]


def test_kill_switch_unknown_visibility_subagent_stop():
    payload = json.loads((FIXTURES / "unknown_visibility_mapping.json").read_text())
    result = run_adapter("SubagentStop", payload)
    response = json.loads(result.stdout)
    assert response["continue"] is False
    assert "unknown visibility" in response["stopReason"]


def test_malformed_stop_payload_fail_closed():
    result = run_adapter("Stop", "{", expect_exit=0)
    response = json.loads(result.stdout)
    assert response["continue"] is False
    assert "Malformed Stop payload" in response["stopReason"]


def test_stdout_silent_for_stop_positive_fixture():
    payload = json.loads((FIXTURES / "positive_fixture.json").read_text())
    result = run_adapter("Stop", payload)
    assert "manifest_id" not in result.stdout
    assert "/home/" not in result.stdout
    assert result.stderr == ""


def test_pre_tool_use_guard_blocks_git_push():
    result = run_adapter("PreToolUse", {
        "tool_input": {"command": "git push origin main"}
    })
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pre_tool_use_guard_blocks_forbidden_paths():
    result = run_adapter("PreToolUse", {
        "tool_input": {"command": "cat .env"}
    })
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_permission_request_uses_event_specific_deny_shape():
    result = run_adapter("PermissionRequest", {
        "tool_input": {"command": "printenv"}
    })
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
    assert response["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_post_run_verifier_blocks_forbidden_paths():
    result = run_adapter("Stop", {
        "secrets_mode": "none",
        "touched_paths": ["assets/test.png"]
    })
    response = json.loads(result.stdout)
    assert response["continue"] is False
    assert "forbidden_path" in response["stopReason"]
