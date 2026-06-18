#!/usr/bin/env python3
"""test_secret_boundary_multiedit.py — MultiEdit 対応テスト (Issue #970).

Tests verify:
1. MultiEdit で secret path を block する (AC3/AC9)
2. PreToolUse 配列で secret_boundary_guard が worktree_scope_guard より前に位置する (AC5/AC10)
3. MultiEdit で file_path 欠落/空文字は fail-closed (AC8)
4. 複数の sensitive path を parametrize でカバーする (AC9)
5. MultiEdit に match する PreToolUse hook 群で secret_boundary_guard が先頭に位置する (AC10)
"""

import json
import subprocess
from pathlib import Path

import pytest

# Resolve paths relative to this test file so that worktree isolation is maintained.
# Test file is at: <worktree>/.claude/hooks/tests/test_secret_boundary_multiedit.py
# Worktree root is: <worktree>/
_THIS_FILE = Path(__file__).resolve()
REPO_ROOT = _THIS_FILE.parent.parent.parent.parent  # worktree root

SETTINGS_JSON_PATH = REPO_ROOT / ".claude" / "settings.json"
GUARD_PATH = REPO_ROOT / ".claude" / "hooks" / "secret_boundary_guard.sh"


# =============================================================================
# AC3: MultiEdit で secret path を block する
# =============================================================================


def test_multiedit_secret_block():
    """GIVEN a MultiEdit tool input targeting a secret path (.env),
    WHEN guard processes it,
    THEN exit code must be 2 (block)."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    payload = json.dumps({
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": "/home/user/.env",
            "edits": [{"old_string": "x", "new_string": "y"}],
        },
    })
    result = subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for MultiEdit on .env, got {result.returncode}\n"
        f"stderr: {result.stderr[:200]}"
    )


# =============================================================================
# AC5/AC10: PreToolUse 配列で secret_boundary_guard が worktree_scope_guard より前
# =============================================================================


def test_guard_order_secret_before_worktree():
    """GIVEN settings.json with PreToolUse hooks,
    WHEN checking the order of hooks that match MultiEdit,
    THEN secret_boundary_guard must appear before worktree_scope_guard."""
    assert SETTINGS_JSON_PATH.exists(), f"settings.json not found: {SETTINGS_JSON_PATH}"
    with open(SETTINGS_JSON_PATH) as f:
        settings = json.load(f)

    pre_tool_use = settings.get("hooks", {}).get("PreToolUse", [])
    assert pre_tool_use, "PreToolUse hooks section is missing or empty"

    # Find indices of secret_boundary_guard and worktree_scope_guard
    # in the PreToolUse array (among entries that match MultiEdit)
    secret_guard_index = None
    worktree_guard_index = None

    for i, entry in enumerate(pre_tool_use):
        matcher = entry.get("matcher", "")
        # Check if this entry matches MultiEdit
        matcher_tools = [m.strip() for m in matcher.split("|")]
        if "MultiEdit" not in matcher_tools:
            continue

        for hook in entry.get("hooks", []):
            command = hook.get("command", "")
            if "secret_boundary_guard" in command:
                secret_guard_index = i
            if "worktree_scope_guard" in command:
                worktree_guard_index = i

    assert secret_guard_index is not None, (
        "secret_boundary_guard not found in PreToolUse hooks matching MultiEdit"
    )
    assert worktree_guard_index is not None, (
        "worktree_scope_guard not found in PreToolUse hooks matching MultiEdit"
    )
    assert secret_guard_index < worktree_guard_index, (
        f"secret_boundary_guard (index={secret_guard_index}) must appear before "
        f"worktree_scope_guard (index={worktree_guard_index}) in PreToolUse array"
    )


# =============================================================================
# AC8: MultiEdit で file_path 欠落/空文字は fail-closed (exit 2)
# stderr に raw payload/path/secret-like value を出さない
# =============================================================================


def test_multiedit_pathless_fail_closed_missing():
    """GIVEN a MultiEdit tool input with missing file_path,
    WHEN guard processes it,
    THEN exit code must be 2 (fail-closed) and stderr must not leak raw payload."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    # file_path キー自体が存在しない
    payload = json.dumps({
        "tool_name": "MultiEdit",
        "tool_input": {
            "edits": [{"old_string": "x", "new_string": "y"}],
        },
    })
    result = subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for MultiEdit with missing file_path, got {result.returncode}\n"
        f"stderr: {result.stderr[:200]}"
    )
    # stderr に raw payload/secret-like value が含まれていないことを確認
    _assert_no_raw_payload_in_stderr(result.stderr, payload)


def test_multiedit_pathless_fail_closed_empty():
    """GIVEN a MultiEdit tool input with empty file_path,
    WHEN guard processes it,
    THEN exit code must be 2 (fail-closed) and stderr must not leak raw payload."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    # file_path が空文字
    payload = json.dumps({
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": "",
            "edits": [{"old_string": "x", "new_string": "y"}],
        },
    })
    result = subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for MultiEdit with empty file_path, got {result.returncode}\n"
        f"stderr: {result.stderr[:200]}"
    )
    # stderr に raw payload/secret-like value が含まれていないことを確認
    _assert_no_raw_payload_in_stderr(result.stderr, payload)


def _assert_no_raw_payload_in_stderr(stderr: str, payload: str) -> None:
    """stderr に raw payload の断片や secret-like value が含まれていないことを確認する。"""
    # payload そのものが漏れていないこと
    # edits のような構造的な部分だけを確認（tool名などの一般的な単語は許容）
    secret_like_fragments = [
        "old_string",
        "new_string",
        '"edits"',
        "MY_SECRET",
        "api_key",
        "credentials",
    ]
    for fragment in secret_like_fragments:
        if fragment in payload:
            assert fragment not in stderr, (
                f"Raw payload fragment '{fragment}' found in stderr: {stderr[:200]}"
            )


# =============================================================================
# AC9: .env, .env.local, secrets/xxx 等を parametrize でカバー
# =============================================================================


@pytest.mark.parametrize("secret_path", [
    "/home/user/.env",
    "/home/user/.env.local",
    "/home/user/secrets/api_key.txt",
    "/home/user/settings.local.json",
    "/home/user/.netrc",
    "/home/user/.npmrc",
    "/home/user/.pypirc",
    "/home/user/.aws/credentials",
    "/home/user/.kube/config",
])
def test_multiedit_sensitive_paths_parametrize(secret_path):
    """GIVEN a MultiEdit tool input targeting various sensitive paths,
    WHEN guard processes it,
    THEN exit code must be 2 (block) for all sensitive paths."""
    assert GUARD_PATH.exists(), f"Guard script not found: {GUARD_PATH}"
    payload = json.dumps({
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": secret_path,
            "edits": [{"old_string": "x", "new_string": "y"}],
        },
    })
    result = subprocess.run(
        [str(GUARD_PATH)],
        input=payload,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2, (
        f"Expected exit code 2 for MultiEdit on {secret_path}, got {result.returncode}\n"
        f"stderr: {result.stderr[:200]}"
    )


# =============================================================================
# AC10: MultiEdit に match する PreToolUse hook 群で secret_boundary_guard が先頭
# =============================================================================


def test_multiedit_first_in_pretooluse_group():
    """GIVEN settings.json with PreToolUse hooks,
    WHEN checking the first hook entry that matches MultiEdit,
    THEN that entry must be secret_boundary_guard (not worktree_scope_guard or others)."""
    assert SETTINGS_JSON_PATH.exists(), f"settings.json not found: {SETTINGS_JSON_PATH}"
    with open(SETTINGS_JSON_PATH) as f:
        settings = json.load(f)

    pre_tool_use = settings.get("hooks", {}).get("PreToolUse", [])
    assert pre_tool_use, "PreToolUse hooks section is missing or empty"

    # Find the first hook entry that matches MultiEdit
    first_multiedit_entry = None
    for entry in pre_tool_use:
        matcher = entry.get("matcher", "")
        matcher_tools = [m.strip() for m in matcher.split("|")]
        if "MultiEdit" in matcher_tools:
            first_multiedit_entry = entry
            break

    assert first_multiedit_entry is not None, (
        "No PreToolUse hook entry matching MultiEdit found in settings.json"
    )

    # The first MultiEdit-matching entry must be secret_boundary_guard
    hooks = first_multiedit_entry.get("hooks", [])
    assert hooks, "First MultiEdit-matching entry has no hooks"

    first_hook_command = hooks[0].get("command", "")
    assert "secret_boundary_guard" in first_hook_command, (
        f"First MultiEdit-matching PreToolUse hook must be secret_boundary_guard, "
        f"but got: {first_hook_command!r}"
    )
