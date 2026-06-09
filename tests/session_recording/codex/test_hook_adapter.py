#!/usr/bin/env python3

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER = REPO_ROOT / "scripts" / "session-recording" / "codex-hook-adapter.mjs"
HOOKS_JSON = REPO_ROOT / ".codex" / "hooks.json"
HOOKS_VALIDATOR = REPO_ROOT / "scripts" / "session-recording" / "validate-codex-hooks.mjs"
MANIFEST_VALIDATOR = REPO_ROOT / "scripts" / "validate-agent-session-manifest.mjs"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "codex"
VALID_MANIFEST = REPO_ROOT / "tests" / "fixtures" / "agent-session-manifest" / "valid-basic.json"
MANIFEST_ROOT = REPO_ROOT / "tmp" / "session-manifests" / "codex"


def run_adapter(event: str, payload, expect_exit: int = 0, env=None):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    result = subprocess.run(
        ["node", str(ADAPTER), "--event", event],
        input=data,
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        check=False,
        env=env,
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


def test_manifest_validator_accepts_root_directory_with_nested_event_manifests(tmp_path: Path):
    manifest_root = tmp_path / "codex"
    nested_manifest = manifest_root / "stop" / "valid.json"
    nested_manifest.parent.mkdir(parents=True)
    nested_manifest.write_text(VALID_MANIFEST.read_text())

    validation = subprocess.run(
        ["node", str(MANIFEST_VALIDATOR), str(manifest_root)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert validation.returncode == 0, validation.stderr


def test_manifest_validator_rejects_root_directory_without_any_json(tmp_path: Path):
    empty_root = tmp_path / "codex"
    (empty_root / "stop").mkdir(parents=True)

    validation = subprocess.run(
        ["node", str(MANIFEST_VALIDATOR), str(empty_root)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert validation.returncode == 1


def test_zz_positive_fixture_leaves_manifest_directory_for_followup_validation():
    payload = json.loads((FIXTURES / "positive_fixture.json").read_text())
    result = run_adapter("Stop", payload)
    assert json.loads(result.stdout) == {"continue": True}

    manifest_dir = MANIFEST_ROOT / "stop"
    files = sorted(manifest_dir.glob("*.json"))
    assert len(files) == 1


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


@pytest.mark.parametrize(
    "command",
    [
        "git -C . push origin main",
        "gh api repos/squne121/loop-protocol/actions/secrets",
        "cat ./.env",
        "cat foo/.env",
    ],
)
def test_pre_tool_use_guard_blocks_bypass_variants(command: str):
    result = run_adapter("PreToolUse", {"tool_input": {"command": command}})
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"


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


def test_hook_validator_rejects_duplicate_stop_composite_handler(tmp_path: Path):
    payload = json.loads(HOOKS_JSON.read_text())
    duplicate_hook = payload["hooks"]["Stop"][0]["hooks"][0].copy()
    payload["hooks"]["Stop"][0]["hooks"].append(duplicate_hook)
    fixture_path = tmp_path / "hooks.json"
    fixture_path.write_text(json.dumps(payload))

    result = subprocess.run(
        ["node", str(HOOKS_VALIDATOR), str(fixture_path)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1


def test_stop_event_scrubs_producer_failure_stderr(tmp_path: Path):
    producer = tmp_path / "producer.mjs"
    producer.write_text(
        'process.stderr.write("cwd=/home/leak/project secret=sk-test-123\\n"); process.exit(1);\n'
    )
    payload = json.loads((FIXTURES / "positive_fixture.json").read_text())
    env = os.environ.copy()
    env["CODEX_SESSION_RECORDING_PRODUCER"] = str(producer)

    result = subprocess.run(
        ["node", str(ADAPTER), "--event", "Stop"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        check=False,
        env=env,
    )
    assert "/home/leak/project" not in result.stderr
    assert "sk-test-123" not in result.stderr
    assert str(producer) not in result.stderr


def test_post_run_verifier_blocks_forbidden_paths():
    result = run_adapter("Stop", {
        "secrets_mode": "none",
        "touched_paths": ["assets/test.png"]
    })
    response = json.loads(result.stdout)
    assert response["continue"] is False
    assert "forbidden_path" in response["stopReason"]
