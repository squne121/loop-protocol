#!/usr/bin/env python3
"""Tests for generate_session_manifest_from_hook.mjs and settings.json structural verification.

Tests verify:
1. generate_session_manifest_from_hook.mjs file exists (AC2)
2. wrapper does not emit manifest JSON on stdout (AC2)
3. PostToolUse hook uses matcher to limit target tools (AC4)
4. Stop/SubagentStop: session_recording_policy_guard.sh appears before producer hook (AC5)
5. settings.json does not reference SessionStart (AC3)
6. settings.json references generate_session_manifest_from_hook.mjs (AC1)
7. settings.json hook commands use exec-form (command="node", args=[wrapper]) (Blocker 1)
8. duplicate skip: same stable key skipped on second invocation (Blocker 4)
9. PostToolUse payload does not emit absolute paths in stderr (Blocker 5)
10. PostToolUse matcher excludes Read (HIGH fix)
11. artifact JSON file is actually created in artifacts/ dir (Blocker 5 - artifact creation)
12. duplicate skip after artifact exists: second run skips and artifact count unchanged (Blocker 5 - robustness)
13. stderr never contains absolute paths matching /home/, /Users/, C:\\ patterns (Blocker 5 - strict redaction)
14. settings.json exec-form: command == "node" and args[0] ends with wrapper .mjs (Blocker 1 - enhanced)
"""

import json
import subprocess
import tempfile
import time
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
# HIGH: PostToolUse matcher must NOT include Read
# ============================================================================


def test_settings_json_post_tool_use_matcher_excludes_read():
    """GIVEN PostToolUse hook is configured (HIGH fix),
    WHEN checking matcher value,
    THEN 'Read' must NOT appear in any PostToolUse matcher."""
    data = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
    hooks_section = data.get("hooks", {})
    post_tool_use_entries = hooks_section.get("PostToolUse", [])

    for i, entry in enumerate(post_tool_use_entries):
        matcher = entry.get("matcher", "")
        # Check 'Read' as a standalone matcher component (not partial match)
        matcher_tools = [t.strip() for t in matcher.split("|")]
        assert "Read" not in matcher_tools, (
            f"PostToolUse entry[{i}] matcher includes 'Read' — "
            "Read must be excluded to avoid firing on every file read"
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

    # In exec-form, command is "node" and args[0] is the script path.
    # Search both command and args for identifying strings.
    hooks_section = data.get("hooks", {})
    event_entries = hooks_section.get("Stop", [])
    hook_entries = []
    for entry in event_entries:
        for hook in entry.get("hooks", []):
            cmd = hook.get("command", "")
            args = hook.get("args", [])
            combined = cmd + " " + " ".join(args)
            hook_entries.append(combined)

    guard_indices = [i for i, s in enumerate(hook_entries) if "session_recording_policy_guard" in s]
    producer_indices = [i for i, s in enumerate(hook_entries) if "generate_session_manifest_from_hook" in s]

    assert guard_indices, "session_recording_policy_guard.sh not found in Stop hooks"
    assert producer_indices, "generate_session_manifest_from_hook not found in Stop hooks"

    assert min(guard_indices) < min(producer_indices), (
        f"session_recording_policy_guard.sh (index {min(guard_indices)}) must appear "
        f"before generate_session_manifest_from_hook (index {min(producer_indices)}) in Stop hooks"
    )


def test_hook_config_policy_guard_before_producer_in_subagent_stop():
    """GIVEN SubagentStop hooks are configured (AC5),
    WHEN checking hook ordering,
    THEN session_recording_policy_guard.sh must appear before generate_session_manifest_from_hook."""
    data = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))

    hooks_section = data.get("hooks", {})
    event_entries = hooks_section.get("SubagentStop", [])
    hook_entries = []
    for entry in event_entries:
        for hook in entry.get("hooks", []):
            cmd = hook.get("command", "")
            args = hook.get("args", [])
            combined = cmd + " " + " ".join(args)
            hook_entries.append(combined)

    guard_indices = [i for i, s in enumerate(hook_entries) if "session_recording_policy_guard" in s]
    producer_indices = [i for i, s in enumerate(hook_entries) if "generate_session_manifest_from_hook" in s]

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


# ============================================================================
# Blocker 1: exec-form structural test
# ============================================================================


def test_settings_json_manifest_producer_hooks_use_exec_form():
    """GIVEN Blocker 1 fix (exec-form compliance),
    WHEN checking settings.json hook entries for generate_session_manifest_from_hook,
    THEN command must be 'node' and args[0] must be the wrapper path."""
    data = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
    hooks_section = data.get("hooks", {})

    producer_hooks_found = 0
    for event_name, event_entries in hooks_section.items():
        for entry in event_entries:
            for hook in entry.get("hooks", []):
                args = hook.get("args", [])
                if args and "generate_session_manifest_from_hook" in args[0]:
                    producer_hooks_found += 1
                    assert hook.get("command") == "node", (
                        f"Hook for {event_name} must have command='node' (exec-form), "
                        f"got: {hook.get('command')!r}"
                    )
                    assert args[0].endswith("generate_session_manifest_from_hook.mjs"), (
                        f"Hook for {event_name} args[0] must point to wrapper .mjs, "
                        f"got: {args[0]!r}"
                    )
                # Also check for old shell-form (command includes 'node' + script path)
                cmd = hook.get("command", "")
                if "generate_session_manifest_from_hook" in cmd and "node" in cmd:
                    pytest.fail(
                        f"Hook for {event_name} uses shell-form command: {cmd!r} — "
                        "must use exec-form: command='node', args=[wrapper_path]"
                    )

    assert producer_hooks_found >= 1, (
        "No manifest producer hooks found using exec-form in settings.json"
    )


# ============================================================================
# Blocker 4: duplicate skip integration test
# ============================================================================


def test_duplicate_skip_on_second_invocation():
    """GIVEN the hook wrapper with stable-key duplicate detection,
    WHEN invoked twice with the same hook_event_name and tool_name,
    THEN the second invocation must emit 'duplicate skip' to stderr and exit 0."""
    if not HOOK_WRAPPER_PATH.exists():
        pytest.skip("Hook wrapper not found")

    # Use a temp artifacts dir to isolate this test from real artifacts
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Patch the wrapper to use a temp artifacts dir by setting env var is not
        # feasible without modifying the script. Instead, run with a temp cwd that
        # mirrors the repo structure so ARTIFACTS_DIR resolves to tmp.
        # Strategy: create a minimal mirror with symlinks and override REPO_ROOT env
        # via node -e approach is complex. Use the real artifacts dir but clean up after.

        # Simpler approach: run once (creates artifact), then run again (should skip).
        # We identify skip by checking stderr for "duplicate skip".
        # Artifact cleanup: we track files created and remove them after test.

        artifacts_dir = REPO_ROOT / "artifacts"
        artifacts_dir.mkdir(exist_ok=True)

        before_files = set(artifacts_dir.glob("private-agent-session-manifest-posttooluse-*.json"))

        # Use a unique PostToolUse payload with a stable tool_name
        hook_stdin = json.dumps({
            "hook_event_name": "PostToolUse",
            "tool_name": "TestDuplicateSkipTool_unique_marker_12345",
            "tool_use_id": "test-tool-use-id-001",
        })

        run_kwargs = dict(
            input=hook_stdin,
            text=True,
            capture_output=True,
            cwd=str(REPO_ROOT),
            timeout=30,
        )

        # First invocation
        result1 = subprocess.run(
            ["node", str(HOOK_WRAPPER_PATH)],
            **run_kwargs,
        )
        assert result1.returncode == 0, f"First invocation failed: {result1.stderr}"

        # Small delay to ensure different timestamps in filename (not required for
        # stable-key logic but avoids filesystem race)
        time.sleep(0.05)

        # Second invocation with same payload
        result2 = subprocess.run(
            ["node", str(HOOK_WRAPPER_PATH)],
            **run_kwargs,
        )
        assert result2.returncode == 0, f"Second invocation failed: {result2.stderr}"

        # Second invocation must emit duplicate skip
        assert "duplicate skip" in result2.stderr, (
            f"Expected 'duplicate skip' in second invocation stderr, got: {result2.stderr!r}"
        )

        # Cleanup: remove artifacts created by this test
        after_files = set(artifacts_dir.glob("private-agent-session-manifest-posttooluse-*.json"))
        new_files = after_files - before_files
        for f in new_files:
            if "testduplicateskiptool" in f.name.lower() or "unique_marker" in f.name.lower():
                try:
                    f.unlink()
                except OSError:
                    pass


# ============================================================================
# Blocker 5: PostToolUse payload — no absolute paths in stderr
# ============================================================================


def test_post_tool_use_payload_no_absolute_path_in_stderr():
    """GIVEN hook wrapper is invoked with a PostToolUse payload containing sensitive paths,
    WHEN it runs,
    THEN stderr must not emit absolute path strings."""
    if not HOOK_WRAPPER_PATH.exists():
        pytest.skip("Hook wrapper not found")

    hook_stdin = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_use_id": "test-tool-use-id-002",
        "transcript_path": "/home/user/secrets/transcript.jsonl",
        "cwd": "/home/user/private/workspace",
    })

    result = subprocess.run(
        ["node", str(HOOK_WRAPPER_PATH)],
        input=hook_stdin,
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )

    # stderr must not contain the sensitive absolute paths from stdin
    assert "/home/user/secrets" not in result.stderr, (
        f"transcript_path leaked to stderr: {result.stderr[:300]!r}"
    )
    assert "/home/user/private/workspace" not in result.stderr, (
        f"cwd absolute path leaked to stderr: {result.stderr[:300]!r}"
    )
    # stdout must be empty regardless
    assert result.stdout == "", (
        f"Expected empty stdout for PostToolUse payload, got: {result.stdout[:200]!r}"
    )


# ============================================================================
# Blocker 5 (new): artifact JSON file creation in artifacts/ dir
# ============================================================================


def test_artifact_json_created_in_artifacts_dir():
    """GIVEN hook wrapper is invoked with a Stop event,
    WHEN it runs successfully,
    THEN an artifact JSON file must be created in artifacts/ (Blocker 5 - artifact creation)."""
    if not HOOK_WRAPPER_PATH.exists():
        pytest.skip("Hook wrapper not found")

    artifacts_dir = REPO_ROOT / "artifacts"
    artifacts_dir.mkdir(exist_ok=True)

    # Use a unique tool name so we don't collide with existing artifacts
    unique_marker = "ArtifactCreationTest_unique_67890"
    hook_stdin = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": unique_marker,
        "tool_use_id": "test-artifact-creation-001",
    })

    before_files = set(artifacts_dir.glob("private-agent-session-manifest-posttooluse-*.json"))

    result = subprocess.run(
        ["node", str(HOOK_WRAPPER_PATH)],
        input=hook_stdin,
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )

    assert result.returncode == 0, (
        f"Hook wrapper exited with {result.returncode}: {result.stderr[:300]!r}"
    )

    after_files = set(artifacts_dir.glob("private-agent-session-manifest-posttooluse-*.json"))
    new_files = after_files - before_files

    # Cleanup before asserting (so failures don't leave artifacts)
    for f in list(new_files):
        try:
            f.unlink()
        except OSError:
            pass

    # Exactly one new artifact file must have been created (or skip was emitted)
    if "duplicate skip" in result.stderr:
        # Already existed from a prior test run — acceptable (robustness)
        pytest.skip("Stable key already exists; duplicate skip triggered (prior test run)")

    assert len(new_files) >= 1, (
        f"Expected at least 1 new artifact JSON in artifacts/, got 0. "
        f"stderr: {result.stderr[:300]!r}"
    )


# ============================================================================
# Blocker 5 (new): duplicate skip with artifact verified to exist first
# ============================================================================


def test_duplicate_skip_robustness_artifact_exists_first():
    """GIVEN an artifact with a known stable key already exists,
    WHEN hook wrapper is invoked with the same payload,
    THEN it must emit 'duplicate skip' and NOT create a second artifact (Blocker 5 - robustness)."""
    if not HOOK_WRAPPER_PATH.exists():
        pytest.skip("Hook wrapper not found")

    artifacts_dir = REPO_ROOT / "artifacts"
    artifacts_dir.mkdir(exist_ok=True)

    unique_marker = "DuplicateSkipRobustnessTest_unique_11111"
    hook_stdin = json.dumps({
        "hook_event_name": "PostToolUse",
        "tool_name": unique_marker,
        "tool_use_id": "test-duplicate-robustness-001",
    })

    run_kwargs = dict(
        input=hook_stdin,
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )

    before_first = set(artifacts_dir.glob("private-agent-session-manifest-posttooluse-*.json"))

    # First invocation — should create artifact
    result1 = subprocess.run(["node", str(HOOK_WRAPPER_PATH)], **run_kwargs)
    assert result1.returncode == 0, f"First invocation failed: {result1.stderr}"

    after_first = set(artifacts_dir.glob("private-agent-session-manifest-posttooluse-*.json"))
    created_by_first = after_first - before_first

    if "duplicate skip" in result1.stderr:
        # Already existed — test cannot verify fresh creation; skip gracefully
        pytest.skip("Stable key already exists before first run; cannot test robustness from scratch")

    assert len(created_by_first) >= 1, (
        f"First invocation did not create artifact. stderr: {result1.stderr[:300]!r}"
    )

    # Small delay for filesystem consistency
    time.sleep(0.05)

    # Second invocation — must skip
    result2 = subprocess.run(["node", str(HOOK_WRAPPER_PATH)], **run_kwargs)
    assert result2.returncode == 0, f"Second invocation failed: {result2.stderr}"

    after_second = set(artifacts_dir.glob("private-agent-session-manifest-posttooluse-*.json"))
    created_by_second = after_second - after_first

    # Cleanup all artifacts created by this test
    for f in created_by_first | created_by_second:
        try:
            f.unlink()
        except OSError:
            pass

    assert "duplicate skip" in result2.stderr, (
        f"Expected 'duplicate skip' in second invocation stderr. "
        f"stderr: {result2.stderr[:300]!r}"
    )
    assert len(created_by_second) == 0, (
        f"Second invocation created {len(created_by_second)} new artifact(s) — must be 0 on duplicate skip"
    )


# ============================================================================
# Blocker 5 (new): strict stderr absolute path redaction test
# ============================================================================


def test_stderr_never_contains_absolute_paths():
    """GIVEN hook wrapper is invoked with payloads containing absolute paths,
    WHEN it runs (for Stop, PostToolUse, and SubagentStop event types),
    THEN stderr must never emit strings matching /home/, /Users/, or C:\\ patterns."""
    if not HOOK_WRAPPER_PATH.exists():
        pytest.skip("Hook wrapper not found")

    sensitive_path_patterns = [
        "/home/sensitive-user/workspace",
        "/Users/Developer/secret-project",
        "C:\\Users\\Developer\\workspace",
    ]

    payloads = [
        {
            "hook_event_name": "Stop",
            "transcript_path": f"{sensitive_path_patterns[0]}/transcript.jsonl",
            "cwd": sensitive_path_patterns[0],
        },
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_use_id": "test-strict-redaction-001",
            "transcript_path": f"{sensitive_path_patterns[1]}/session.jsonl",
            "cwd": sensitive_path_patterns[1],
        },
        {
            "hook_event_name": "SubagentStop",
            "agent_id": "test-agent-12345678",
            "transcript_path": f"{sensitive_path_patterns[2]}\\transcript.jsonl",
            "cwd": sensitive_path_patterns[2],
        },
    ]

    for payload in payloads:
        result = subprocess.run(
            ["node", str(HOOK_WRAPPER_PATH)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        # Check stderr does not contain sensitive absolute paths from stdin
        for sensitive in sensitive_path_patterns:
            # Normalize to check both forward-slash and backslash variants
            sensitive_fwd = sensitive.replace("\\", "/")
            assert sensitive_fwd not in result.stderr and sensitive not in result.stderr, (
                f"Event={payload['hook_event_name']}: sensitive path leaked to stderr: "
                f"{sensitive!r} found in {result.stderr[:300]!r}"
            )


# ============================================================================
# Blocker 1 (enhanced): exec-form — verify command == "node" and args[0] is wrapper
# ============================================================================


def test_settings_json_exec_form_command_is_node_and_args0_is_wrapper():
    """GIVEN Blocker 1 fix (exec-form compliance, enhanced),
    WHEN checking all hook entries that reference generate_session_manifest_from_hook,
    THEN command must equal 'node' exactly and args[0] must end with
    'generate_session_manifest_from_hook.mjs'."""
    data = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
    hooks_section = data.get("hooks", {})

    producer_hooks_found = 0
    for event_name, event_entries in hooks_section.items():
        for entry in event_entries:
            for hook in entry.get("hooks", []):
                args = hook.get("args", [])
                if not args:
                    continue
                if "generate_session_manifest_from_hook" not in args[0]:
                    continue

                producer_hooks_found += 1
                cmd = hook.get("command", "")

                assert cmd == "node", (
                    f"[{event_name}] manifest producer hook must have command='node' "
                    f"(exec-form), got: {cmd!r}"
                )
                assert args[0].endswith("generate_session_manifest_from_hook.mjs"), (
                    f"[{event_name}] manifest producer hook args[0] must end with "
                    f"'generate_session_manifest_from_hook.mjs', got: {args[0]!r}"
                )
                # args[0] must NOT be just the bare filename without a path prefix
                # (must include ${CLAUDE_PROJECT_DIR} or equivalent absolute-like reference)
                assert "/" in args[0] or "\\" in args[0], (
                    f"[{event_name}] manifest producer hook args[0] must include a path "
                    f"(not bare filename), got: {args[0]!r}"
                )

    assert producer_hooks_found >= 1, (
        "No manifest producer hooks (exec-form) found in settings.json — "
        "expected at least 1 entry with args[0] containing generate_session_manifest_from_hook"
    )
