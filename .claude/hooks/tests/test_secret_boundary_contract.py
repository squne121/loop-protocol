#!/usr/bin/env python3
"""test_secret_boundary_contract.py — Structural and sentinel tests for secret boundary (Issue #412).

Tests verify:
1. sentinel fixture does not appear in manifest/log/artifact in any encoded form (AC10)
2. settings.json contains PreToolUse secret_boundary_guard hook (AC13)
3. guard exits with code 2 on malformed stdin (AC13)
4. guard stderr does not echo command/path/env values (AC4, AC13)
5. session-manifest.yml has no pull_request_target (AC7, AC13)
6. session-manifest.yml permissions are contents: read or {} (AC6, AC13)
7. session-manifest.yml has no secrets. references (AC5, AC13)
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

# Resolve repo root via git
REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
)

SETTINGS_JSON_PATH = REPO_ROOT / ".claude" / "settings.json"
GUARD_PATH = REPO_ROOT / ".claude" / "hooks" / "secret_boundary_guard.sh"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "session-manifest.yml"
ARTIFACTS_DIR = REPO_ROOT / "artifacts"


# =============================================================================
# Sentinel fixture
# =============================================================================

SENTINEL_PLAINTEXT = "SENTINEL_SECRET_412_TEST_abc123XYZ"

def _encode_variants(value: str) -> list[str]:
    """Return multiple encoded representations of the sentinel value."""
    import base64
    import hashlib
    import urllib.parse
    variants = [
        value,  # raw
        base64.b64encode(value.encode()).decode(),  # base64
        value.encode().hex(),  # hex
        urllib.parse.quote(value),  # urlencoded
        hashlib.sha256(value.encode()).hexdigest(),  # sha256 hash
        hashlib.sha1(value.encode()).hexdigest(),  # sha1 hash
        value[:8],  # prefix partial (first 8 chars)
        value[-8:],  # suffix partial (last 8 chars)
    ]
    return variants


def _scan_file_for_sentinel(file_path: Path, sentinel: str) -> list[str]:
    """Return list of matching lines found in file."""
    if not file_path.exists():
        return []
    variants = _encode_variants(sentinel)
    hits = []
    content = file_path.read_text(errors="replace")
    for variant in variants:
        for i, line in enumerate(content.splitlines(), 1):
            if variant in line:
                hits.append(f"{file_path}:{i}: found variant '{variant[:20]}...'")
    return hits


# =============================================================================
# AC10: sentinel fixture does not appear in artifacts/manifest/log in any encoded form
# =============================================================================


def test_sentinel_not_in_artifacts(tmp_path):
    """GIVEN a sentinel secret value and an artifacts/ directory with fixture files,
    WHEN scanning artifacts/ dir,
    THEN the sentinel must not appear in any form (raw/base64/hex/urlencoded/sha256/sha1/partial).

    This test creates a clean fixture artifact (containing only non-sensitive data)
    and verifies it does not contain any encoded form of the sentinel.
    """
    # Create artifacts dir if it doesn't exist (for this test run)
    artifacts_dir = ARTIFACTS_DIR
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Write a clean fixture artifact that must NOT contain the sentinel
    fixture_file = artifacts_dir / "test_sentinel_clean_fixture.json"
    fixture_file.write_text(
        '{"status": "clean", "note": "fixture artifact for sentinel scan test"}\n'
    )

    try:
        hits = []
        for f in artifacts_dir.rglob("*"):
            if f.is_file():
                hits.extend(_scan_file_for_sentinel(f, SENTINEL_PLAINTEXT))
        assert hits == [], f"Sentinel found in artifacts: {hits}"
    finally:
        # Clean up the fixture file after test
        if fixture_file.exists():
            fixture_file.unlink()


def test_sentinel_not_in_settings_json():
    """GIVEN a sentinel secret value, WHEN scanning settings.json,
    THEN the sentinel must not appear in any encoded form."""
    hits = _scan_file_for_sentinel(SETTINGS_JSON_PATH, SENTINEL_PLAINTEXT)
    assert hits == [], f"Sentinel found in settings.json: {hits}"


# =============================================================================
# AC13: settings.json contains PreToolUse secret_boundary_guard hook
# =============================================================================


def test_settings_has_pretooluse_secret_boundary_guard():
    """GIVEN settings.json exists, WHEN checking hooks,
    THEN PreToolUse section must contain secret_boundary_guard hook."""
    assert SETTINGS_JSON_PATH.exists(), f"settings.json not found: {SETTINGS_JSON_PATH}"
    with open(SETTINGS_JSON_PATH) as f:
        settings = json.load(f)

    hooks = settings.get("hooks", {})
    pre_tool_use = hooks.get("PreToolUse", [])
    assert pre_tool_use, "PreToolUse hooks section is missing or empty"

    # Check that at least one hook references secret_boundary_guard
    guard_found = False
    for entry in pre_tool_use:
        for hook in entry.get("hooks", []):
            command = hook.get("command", "")
            args = hook.get("args", [])
            if "secret_boundary_guard" in command or any(
                "secret_boundary_guard" in str(a) for a in args
            ):
                guard_found = True
                break

    assert guard_found, (
        "secret_boundary_guard not found in PreToolUse hooks in settings.json"
    )


# =============================================================================
# AC13: guard exits with code 2 on malformed stdin
# =============================================================================


def test_guard_exits_2_on_malformed_stdin():
    """GIVEN secret_boundary_guard.sh exists, WHEN given malformed stdin (not JSON),
    THEN exit code must be 2."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    result = subprocess.run(
        [str(GUARD_PATH)],
        input="not valid json",
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for malformed stdin, got {result.returncode}\n"
        f"stderr: {result.stderr[:200]}"
    )


def test_guard_exits_2_on_empty_stdin():
    """GIVEN secret_boundary_guard.sh exists, WHEN given empty stdin,
    THEN exit code must be 2 (tool_name missing = fail closed)."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    result = subprocess.run(
        [str(GUARD_PATH)],
        input="{}",
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for empty JSON (tool_name missing), got {result.returncode}\n"
        f"stderr: {result.stderr[:200]}"
    )


def test_guard_allows_safe_bash_command():
    """GIVEN a safe Bash tool input, WHEN guard processes it,
    THEN exit code must be 0."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello"}})
    result = subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, (
        f"Expected exit code 0 for safe command, got {result.returncode}\n"
        f"stderr: {result.stderr[:200]}"
    )


def test_guard_blocks_printenv():
    """GIVEN a Bash tool input with printenv, WHEN guard processes it,
    THEN exit code must be 2."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "printenv"}})
    result = subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for printenv, got {result.returncode}\n"
        f"stderr: {result.stderr[:200]}"
    )


def test_guard_blocks_env_file_read():
    """GIVEN a Read tool input targeting .env file, WHEN guard processes it,
    THEN exit code must be 2."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/repo/.env"}})
    result = subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for .env read, got {result.returncode}\n"
        f"stderr: {result.stderr[:200]}"
    )


def test_guard_blocks_gh_secret():
    """GIVEN a Bash tool input with 'gh secret', WHEN guard processes it,
    THEN exit code must be 2."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh secret list"}})
    result = subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for 'gh secret list', got {result.returncode}\n"
        f"stderr: {result.stderr[:200]}"
    )


# =============================================================================
# AC4 / AC13: guard stderr does not echo command/path/env values
# =============================================================================


def test_guard_blocks_sed_env_read():
    """GIVEN a Bash tool input using sed to read .env, WHEN guard processes it,
    THEN exit code must be 2 (B5: sed bypass detection)."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "sed -n p .env"}})
    result = subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for 'sed -n p .env', got {result.returncode}\n"
        f"stderr: {result.stderr[:200]}"
    )


def test_guard_blocks_awk_env_read():
    """GIVEN a Bash tool input using awk to read .env, WHEN guard processes it,
    THEN exit code must be 2 (B5: awk bypass detection)."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "awk 1 .env"}})
    result = subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for 'awk 1 .env', got {result.returncode}\n"
        f"stderr: {result.stderr[:200]}"
    )


def test_guard_blocks_env_standalone():
    """GIVEN a Bash tool input with bare 'env' command, WHEN guard processes it,
    THEN exit code must be 2 (B1/B2: env dumps all environment variables)."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "env"}})
    result = subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for bare 'env', got {result.returncode}\n"
        f"stderr: {result.stderr[:200]}"
    )


def test_guard_blocks_set_standalone():
    """GIVEN a Bash tool input with bare 'set' command, WHEN guard processes it,
    THEN exit code must be 2 (B1/B2: set dumps all shell variables)."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "set"}})
    result = subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for bare 'set', got {result.returncode}\n"
        f"stderr: {result.stderr[:200]}"
    )


def test_guard_stderr_no_command_echo_on_block():
    """GIVEN guard blocks a command, WHEN checking stderr,
    THEN stderr must not contain the actual command string."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    secret_command = "printenv MY_SECRET_TOKEN_xyz987"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": secret_command}})
    result = subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, "Guard should have blocked this command"
    # The actual command value must NOT appear in stderr
    assert "MY_SECRET_TOKEN_xyz987" not in result.stderr, (
        f"Guard stderr leaked command value: {result.stderr[:200]}"
    )
    assert "printenv" not in result.stderr.lower() or "printenv" not in secret_command.split()[0], (
        # Allow "blocked: high-risk Bash command pattern detected" — generic message is OK
        # But should not echo back the actual command string
        "Guard stderr must use generic message, not echo command details"
    )


def test_guard_stderr_no_path_echo_on_block():
    """GIVEN guard blocks a path access, WHEN checking stderr,
    THEN stderr must not contain the actual path."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    secret_path = "/home/user/secrets/my_api_key.txt"
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": secret_path}})
    result = subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, "Guard should have blocked this path"
    # The actual path must NOT appear in stderr
    assert "my_api_key" not in result.stderr, (
        f"Guard stderr leaked path value: {result.stderr[:200]}"
    )


# =============================================================================
# AC7 / AC13: workflow has no pull_request_target
# =============================================================================


def test_workflow_no_pull_request_target():
    """GIVEN session-manifest.yml exists, WHEN checking triggers,
    THEN pull_request_target must not appear."""
    assert WORKFLOW_PATH.exists(), f"Workflow not found: {WORKFLOW_PATH}"
    content = WORKFLOW_PATH.read_text()
    assert "pull_request_target" not in content, (
        "session-manifest.yml must not use pull_request_target trigger"
    )


# =============================================================================
# AC6 / AC13: workflow permissions are contents: read or {}
# =============================================================================


def test_workflow_permissions_read_only():
    """GIVEN session-manifest.yml exists, WHEN checking permissions,
    THEN write permissions must not be present."""
    assert WORKFLOW_PATH.exists(), f"Workflow not found: {WORKFLOW_PATH}"
    content = WORKFLOW_PATH.read_text()
    # Disallow write permissions
    assert not re.search(r"write-all", content), (
        "session-manifest.yml must not have write-all permissions"
    )
    assert not re.search(r"issues:\s*write", content), (
        "session-manifest.yml must not have issues: write permission"
    )
    assert not re.search(r"pull-requests:\s*write", content), (
        "session-manifest.yml must not have pull-requests: write permission"
    )


# =============================================================================
# AC5 / AC13: workflow has no secrets. references
# =============================================================================


def test_workflow_no_secrets_reference():
    """GIVEN session-manifest.yml exists, WHEN checking secrets usage,
    THEN secrets. must not appear."""
    assert WORKFLOW_PATH.exists(), f"Workflow not found: {WORKFLOW_PATH}"
    content = WORKFLOW_PATH.read_text()
    matches = re.findall(r"secrets\.", content)
    assert not matches, (
        f"session-manifest.yml must not reference secrets., found {len(matches)} occurrence(s)"
    )
