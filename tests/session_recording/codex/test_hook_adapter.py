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


# ---------------------------------------------------------------------------
# AC3: secret_boundary_violation reason_code and command_kind fixtures
# ---------------------------------------------------------------------------

def test_pre_tool_use_secret_boundary_gh_secret():
    """GIVEN a gh secret command WHEN PreToolUse fires THEN reason_code=secret_boundary_violation command_kind=gh_secret"""
    result = run_adapter("PreToolUse", {"tool_input": {"command": "gh secret list"}})
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "secret_boundary_violation" in reason
    assert "command_kind=gh_secret" in reason


def test_pre_tool_use_secret_boundary_gh_api_secrets():
    """GIVEN a gh api .../secrets command WHEN PreToolUse fires THEN reason_code=secret_boundary_violation command_kind=gh_api_actions_secrets"""
    result = run_adapter("PreToolUse", {
        "tool_input": {"command": "gh api repos/squne121/loop-protocol/actions/secrets"}
    })
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "secret_boundary_violation" in reason
    assert "command_kind=gh_api_actions_secrets" in reason


def test_pre_tool_use_secret_boundary_printenv():
    """GIVEN printenv command WHEN PreToolUse fires THEN reason_code=secret_boundary_violation command_kind=printenv"""
    result = run_adapter("PreToolUse", {"tool_input": {"command": "printenv"}})
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "secret_boundary_violation" in reason
    assert "command_kind=printenv" in reason


# ---------------------------------------------------------------------------
# AC4: remote_write_requires_approval reason_code and command_kind fixtures
# ---------------------------------------------------------------------------

def test_pre_tool_use_remote_write_git_push():
    """GIVEN git push command WHEN PreToolUse fires THEN reason_code=remote_write_requires_approval command_kind=git_push"""
    result = run_adapter("PreToolUse", {"tool_input": {"command": "git push origin main"}})
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "remote_write_requires_approval" in reason
    assert "command_kind=git_push" in reason


def test_pre_tool_use_remote_write_git_push_dash_c():
    """GIVEN git -C <dir> push command WHEN PreToolUse fires THEN reason_code=remote_write_requires_approval"""
    result = run_adapter("PreToolUse", {"tool_input": {"command": "git -C /some/path push origin main"}})
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "remote_write_requires_approval" in reason
    assert "command_kind=git_push" in reason


def test_pre_tool_use_remote_write_reason_not_secret():
    """GIVEN git push WHEN PreToolUse fires THEN reason does NOT contain secret_boundary_violation"""
    result = run_adapter("PreToolUse", {"tool_input": {"command": "git push origin main"}})
    response = json.loads(result.stdout)
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "secret_boundary_violation" not in reason
    assert "remote_write_requires_approval" in reason


# ---------------------------------------------------------------------------
# AC6: supported output shape - PreToolUse must only emit permissionDecisionReason
# ---------------------------------------------------------------------------

def test_supported_output_shape():
    """GIVEN a denied PreToolUse event WHEN adapter emits deny JSON THEN only supported fields are present"""
    result = run_adapter("PreToolUse", {"tool_input": {"command": "git push origin main"}})
    response = json.loads(result.stdout)
    # Root-level must NOT contain Codex control fields that would interfere with PreToolUse
    root_unsupported = {"decision", "continue", "stopReason", "suppressOutput"}
    assert not root_unsupported.intersection(set(response.keys())), \
        f"Root-level response must not contain {root_unsupported}, got keys: {list(response.keys())}"
    hook_output = response["hookSpecificOutput"]
    # Required fields
    assert "hookEventName" in hook_output
    assert hook_output["hookEventName"] == "PreToolUse"
    assert "permissionDecision" in hook_output
    assert "permissionDecisionReason" in hook_output
    assert isinstance(hook_output["permissionDecisionReason"], str)
    # Must NOT contain unsupported top-level fields in hookSpecificOutput
    unsupported = {"decision", "behavior", "message", "stopReason"}
    assert not unsupported.intersection(set(hook_output.keys()))


def test_permission_request_supported_output_shape():
    """GIVEN a denied PermissionRequest event WHEN adapter emits deny JSON THEN event-specific shape is used"""
    result = run_adapter("PermissionRequest", {"tool_input": {"command": "printenv"}})
    response = json.loads(result.stdout)
    hook_output = response["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PermissionRequest"
    assert "decision" in hook_output
    assert hook_output["decision"]["behavior"] == "deny"
    # Must NOT emit PreToolUse-specific permissionDecision at top level
    assert "permissionDecision" not in hook_output


# ---------------------------------------------------------------------------
# AC7: mixed command priority / env_wrapper false-positive / blocked_command_preview redaction
# ---------------------------------------------------------------------------

def test_mixed_priority_secret_before_remote_write():
    """GIVEN a command that matches both secret AND remote write patterns WHEN guard fires THEN secret_boundary_violation takes priority"""
    # Hypothetical combined command: push that also dumps secrets (secret wins)
    result = run_adapter("PreToolUse", {
        "tool_input": {"command": "gh secret list && git push origin main"}
    })
    response = json.loads(result.stdout)
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    # gh secret triggers secret_boundary_violation first (higher priority)
    assert "secret_boundary_violation" in reason


def test_env_wrapper_not_secret():
    """GIVEN env FOO=bar <cmd> prefix WHEN PreToolUse fires THEN it is NOT treated as a secret dump"""
    result = run_adapter("PreToolUse", {"tool_input": {"command": "env PAGER=cat gh issue view 1"}})
    # env FOO=bar prefix should be stripped and gh issue view 1 is allowed (read-only)
    # If stdout is empty, the command passed through (no deny output = allowed behavior)
    if not result.stdout.strip():
        # Empty stdout = no deny emitted = command was allowed
        return
    response = json.loads(result.stdout)
    # If there is output, verify it's not a secret_boundary_violation deny
    if "hookSpecificOutput" in response and response["hookSpecificOutput"].get("permissionDecision") == "deny":
        reason = response["hookSpecificOutput"]["permissionDecisionReason"]
        assert "secret_boundary_violation" not in reason


def test_blocked_command_preview_redacted():
    """GIVEN a denied command with secret tokens WHEN PreToolUse deny reason is produced THEN secret tokens are redacted"""
    # Test 1: sk- token in a git push command (which triggers remote_write deny) — token is redacted
    # Note: we use a command that is actually blocked by the guard.
    # For redaction verification we rely on the redactCommandPreview unit behavior via gh secret.
    result = run_adapter("PreToolUse", {
        "tool_input": {"command": "gh secret list --token sk-abc123xyz456"}
    })
    response = json.loads(result.stdout)
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "blocked_command_preview" in reason
    assert "sk-abc123xyz456" not in reason

    # Test 2: ghp_ token in gh api secrets command
    result = run_adapter("PreToolUse", {
        "tool_input": {"command": "gh api /repos/owner/repo/actions/secrets --header Authorization:ghp_abc123xyz456"}
    })
    response = json.loads(result.stdout)
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "blocked_command_preview" in reason
    assert "ghp_abc123xyz456" not in reason

    # Test 3: MY_SECRET variable in printenv command (printenv → secret_boundary_violation)
    result = run_adapter("PreToolUse", {
        "tool_input": {"command": "MY_SECRET=hunter2 printenv"}
    })
    response = json.loads(result.stdout)
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "blocked_command_preview" in reason
    assert "hunter2" not in reason

    # Test 4: long command is truncated
    long_cmd = "git push origin main " + ("x" * 100)
    result = run_adapter("PreToolUse", {"tool_input": {"command": long_cmd}})
    response = json.loads(result.stdout)
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "blocked_command_preview" in reason
    assert "..." in reason


# ---------------------------------------------------------------------------
# Fix 2: env VAR=value prefix bypass adversarial regression tests
# ---------------------------------------------------------------------------

def test_env_prefix_does_not_bypass_secret():
    """GIVEN env VAR=val <secret-command> WHEN PreToolUse fires THEN secret classification still applies"""
    # env PAGER=cat gh secret list → must deny: secret_boundary_violation
    result = run_adapter("PreToolUse", {
        "tool_input": {"command": "env PAGER=cat gh secret list"}
    })
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "secret_boundary_violation" in reason

    # env FOO=bar printenv → must deny: secret_boundary_violation (printenv dumps env)
    result = run_adapter("PreToolUse", {
        "tool_input": {"command": "env FOO=bar printenv"}
    })
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "secret_boundary_violation" in reason


def test_env_prefix_remote_write_not_bypassed():
    """GIVEN env VAR=val git push WHEN PreToolUse fires THEN remote_write_requires_approval applies"""
    result = run_adapter("PreToolUse", {
        "tool_input": {"command": "env FOO=bar git push origin main"}
    })
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "remote_write_requires_approval" in reason


def test_bare_env_is_env_dump():
    """GIVEN bare 'env' command WHEN PreToolUse fires THEN env_dump deny is emitted"""
    # bare "env"
    result = run_adapter("PreToolUse", {"tool_input": {"command": "env"}})
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "secret_boundary_violation" in reason
    assert "env_dump" in reason

    # "env -0" (null-delimited dump)
    result = run_adapter("PreToolUse", {"tool_input": {"command": "env -0"}})
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "secret_boundary_violation" in reason
    assert "env_dump" in reason


def test_env_prefix_benign_command_allowed():
    """GIVEN env PAGER=cat gh issue view 1 WHEN PreToolUse fires THEN command is allowed (null = no deny)"""
    result = run_adapter("PreToolUse", {"tool_input": {"command": "env PAGER=cat gh issue view 1"}})
    # No deny should be emitted — stdout is empty or no deny in output
    if not result.stdout.strip():
        return  # empty stdout = allowed
    response = json.loads(result.stdout)
    if "hookSpecificOutput" in response:
        assert response["hookSpecificOutput"].get("permissionDecision") != "deny", \
            f"Expected allow but got deny: {response}"
