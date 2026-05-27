#!/usr/bin/env python3
"""Tests for generate_session_manifest_from_hook.mjs and settings.json structural verification.

Tests verify:
1. generate_session_manifest_from_hook.mjs file exists (AC2)
2. wrapper does not emit manifest JSON on stdout (AC2)
3. PostToolUse hook uses matcher to limit target tools (AC4)
4. Stop/SubagentStop: session_recording_policy_guard.sh appears before producer hook (AC5)
5. settings.json does not reference SessionStart (AC3)
6. settings.json references generate_session_manifest_from_hook.mjs (AC1)
"""

import json
import subprocess
from pathlib import Path

import pytest

# Dynamically resolve repo root using git rev-parse
REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
)

HOOK_WRAPPER_PATH = REPO_ROOT / ".claude" / "hooks" / "generate_session_manifest_from_hook.mjs"
SETTINGS_JSON_PATH = REPO_ROOT / ".claude" / "settings.json"
POLICY_GUARD_PATH = REPO_ROOT / ".claude" / "hooks" / "session_recording_policy_guard.sh"


# ============================================================================
# AC2: wrapper file exists
# ============================================================================


def test_generate_session_manifest_from_hook_file_exists():
    """GIVEN the hook wrapper is implemented, WHEN checking file existence,
    THEN generate_session_manifest_from_hook.mjs must exist."""
    assert HOOK_WRAPPER_PATH.exists(), (
        f"Hook wrapper not found: {HOOK_WRAPPER_PATH.relative_to(REPO_ROOT)}"
    )


# ============================================================================
# AC2: stdout must be silent (no manifest JSON on stdout)
# ============================================================================


def test_generate_session_manifest_from_hook_stdout_is_silent():
    """GIVEN the hook wrapper is invoked with a Stop event context,
    WHEN it runs successfully,
    THEN stdout must be empty (no manifest JSON emitted)."""
    if not HOOK_WRAPPER_PATH.exists():
        pytest.skip("Hook wrapper not found")

    hook_stdin = json.dumps({
        "hook_event_name": "Stop",
        "transcript_path": "/tmp/test-transcript.jsonl",
        "cwd": str(REPO_ROOT),
    })

    result = subprocess.run(
        ["node", str(HOOK_WRAPPER_PATH)],
        input=hook_stdin,
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )

    # stdout must be empty (AC2: wrapper does not emit manifest to stdout)
    assert result.stdout == "", (
        f"Expected empty stdout, got: {result.stdout[:200]!r}"
    )


# ============================================================================
# AC1: settings.json references generate_session_manifest_from_hook
# ============================================================================


def test_settings_json_references_hook_wrapper():
    """GIVEN settings.json is updated for AC1,
    WHEN checking for hook wrapper reference,
    THEN generate_session_manifest_from_hook must appear in settings.json."""
    content = SETTINGS_JSON_PATH.read_text(encoding="utf-8")
    assert "generate_session_manifest_from_hook" in content, (
        "settings.json does not reference generate_session_manifest_from_hook"
    )


# ============================================================================
# AC3: SessionStart must NOT appear in settings.json hooks
# ============================================================================


def test_settings_json_no_session_start_hook():
    """GIVEN the AC3 constraint (SessionStart excluded),
    WHEN checking settings.json hooks,
    THEN SessionStart must not appear in the hooks section."""
    data = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
    hooks_section = data.get("hooks", {})
    assert "SessionStart" not in hooks_section, (
        "SessionStart found in settings.json hooks — it must not be invocation target"
    )


# ============================================================================
# AC4: PostToolUse must have a matcher (not unconditional)
# ============================================================================


def test_settings_json_post_tool_use_has_matcher():
    """GIVEN PostToolUse hook is configured,
    WHEN checking settings.json structure,
    THEN each PostToolUse entry must have a 'matcher' field to limit target tools."""
    data = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
    hooks_section = data.get("hooks", {})
    post_tool_use_entries = hooks_section.get("PostToolUse", [])

    if not post_tool_use_entries:
        # PostToolUse not configured — AC4 satisfied trivially
        return

    for i, entry in enumerate(post_tool_use_entries):
        assert "matcher" in entry, (
            f"PostToolUse entry[{i}] is missing 'matcher' field — "
            "must not fire unconditionally on all tool calls"
        )
        assert entry["matcher"], (
            f"PostToolUse entry[{i}] has empty 'matcher' — must specify target tools"
        )


# ============================================================================
# AC5: policy_guard must appear before producer hook in Stop/SubagentStop
# ============================================================================


def _get_hook_commands(data: dict, event: str) -> list[str]:
    """Extract ordered list of hook commands for a given event."""
    hooks_section = data.get("hooks", {})
    event_entries = hooks_section.get(event, [])
    commands = []
    for entry in event_entries:
        for hook in entry.get("hooks", []):
            cmd = hook.get("command", "")
            commands.append(cmd)
    return commands


def test_hook_config_policy_guard_before_producer_in_stop():
    """GIVEN Stop hooks are configured (AC5),
    WHEN checking hook ordering,
    THEN session_recording_policy_guard.sh must appear before generate_session_manifest_from_hook."""
    data = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
    commands = _get_hook_commands(data, "Stop")

    guard_indices = [i for i, cmd in enumerate(commands) if "session_recording_policy_guard" in cmd]
    producer_indices = [i for i, cmd in enumerate(commands) if "generate_session_manifest_from_hook" in cmd]

    assert guard_indices, "session_recording_policy_guard.sh not found in Stop hooks"
    assert producer_indices, "generate_session_manifest_from_hook not found in Stop hooks"

    # Guard must appear before producer
    assert min(guard_indices) < min(producer_indices), (
        f"session_recording_policy_guard.sh (index {min(guard_indices)}) must appear "
        f"before generate_session_manifest_from_hook (index {min(producer_indices)}) in Stop hooks"
    )


def test_hook_config_policy_guard_before_producer_in_subagent_stop():
    """GIVEN SubagentStop hooks are configured (AC5),
    WHEN checking hook ordering,
    THEN session_recording_policy_guard.sh must appear before generate_session_manifest_from_hook."""
    data = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
    commands = _get_hook_commands(data, "SubagentStop")

    guard_indices = [i for i, cmd in enumerate(commands) if "session_recording_policy_guard" in cmd]
    producer_indices = [i for i, cmd in enumerate(commands) if "generate_session_manifest_from_hook" in cmd]

    assert guard_indices, "session_recording_policy_guard.sh not found in SubagentStop hooks"
    assert producer_indices, "generate_session_manifest_from_hook not found in SubagentStop hooks"

    assert min(guard_indices) < min(producer_indices), (
        f"session_recording_policy_guard.sh (index {min(guard_indices)}) must appear "
        f"before generate_session_manifest_from_hook (index {min(producer_indices)}) in SubagentStop hooks"
    )


# ============================================================================
# AC6: no transcript_path / cwd absolute paths in stdout
# ============================================================================


def test_generate_session_manifest_from_hook_no_absolute_path_in_stdout():
    """GIVEN hook wrapper is invoked with transcript_path and cwd in stdin,
    WHEN it runs,
    THEN stdout must not contain absolute path strings (AC6)."""
    if not HOOK_WRAPPER_PATH.exists():
        pytest.skip("Hook wrapper not found")

    hook_stdin = json.dumps({
        "hook_event_name": "Stop",
        "transcript_path": "/home/user/sensitive/transcript.jsonl",
        "cwd": "/home/user/projects/secret-project",
    })

    result = subprocess.run(
        ["node", str(HOOK_WRAPPER_PATH)],
        input=hook_stdin,
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )

    # stdout must not contain the sensitive paths
    assert "/home/user/sensitive" not in result.stdout, (
        "transcript_path leaked to stdout"
    )
    assert "/home/user/projects/secret-project" not in result.stdout, (
        "cwd absolute path leaked to stdout"
    )
