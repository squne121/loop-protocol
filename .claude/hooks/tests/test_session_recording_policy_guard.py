#!/usr/bin/env python3
"""Smoke tests for session_recording_policy_guard.sh hook.

Tests verify:
1. No watched changes -> exit 0
2. Watched file changed + checker pass -> exit 0
3. Watched file changed + checker fail -> exit 2 + SESSION_RECORDING_POLICY_GUARD in stderr
4. Untracked watched file -> checker is called
5. Stop fixture JSON stdin -> deterministic result
6. SubagentStop fixture JSON stdin -> deterministic result
7. stop_hook_active: true -> exit 0 short-circuit
8. Invalid/non-repo cwd -> exit 2
9. .claude/settings.json has Stop/SubagentStop hooks with proper structure

Exit code 0 indicates test passed. Tests may be run individually with pytest.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

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

HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "session_recording_policy_guard.sh"
SETTINGS_JSON_PATH = REPO_ROOT / ".claude" / "settings.json"


def run_hook(
    repo_root: Path,
    hook_stdin: Dict[str, Any],
    cwd: Path | None = None,
) -> Tuple[int, str, str]:
    """Run the hook script with given stdin and cwd.

    Args:
        repo_root: The repository root directory for the hook to operate on.
        hook_stdin: Dict to be JSON-encoded and passed as stdin.
        cwd: Working directory for subprocess; if None, uses repo_root.

    Returns:
        (exit_code, stdout, stderr)
    """
    if cwd is None:
        cwd = repo_root

    result = subprocess.run(
        [str(HOOK_PATH)],
        input=json.dumps(hook_stdin),
        text=True,
        capture_output=True,
        cwd=str(cwd),
    )

    return result.returncode, result.stdout, result.stderr


def create_test_repo(tmp_path: Path) -> Path:
    """Create a minimal git repository with required structure.

    Returns the repo root path.
    """
    repo = tmp_path / "test_repo"
    repo.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    # Create required directories
    (repo / "docs" / "dev").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "schemas").mkdir(parents=True, exist_ok=True)
    (repo / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)

    # Create minimal policy file for checker to pass
    policy_content = """---
id: session-recording-policy
status: stable
---

# Test Policy

```yaml
schema: session_recording_policy/v1
source_of_truth:
  secret_policy: docs/dev/secret-policy.md
  manifest_schema: docs/schemas/agent-session-manifest.schema.json
derived_from_secret_policy:
  current_secrets_mode: none
  fail_closed_on_unknown_mapping: true
taxonomy_mapping:
  current:
    description: "Current state"
    secrets_mode_when_absent: none
    secrets_mode_when_present: unknown
    public_full_transcript_allowed: false
    session_recording_allowed: true
    checkpoint_push_allowed: false
    rationale: "Test"
  publish_secret:
    description: "Publish secret"
    secrets_mode: publish_secret
    public_full_transcript_allowed: false
    session_recording_allowed: false
    checkpoint_push_allowed: false
    rationale: "Test"
  app_runtime_secret:
    description: "App secret"
    secrets_mode: app_secret
    public_full_transcript_allowed: false
    session_recording_allowed: false
    checkpoint_push_allowed: false
    rationale: "Test"
  agent_local_secret:
    description: "Agent secret"
    secrets_mode: app_secret
    public_full_transcript_allowed: false
    session_recording_allowed: false
    checkpoint_push_allowed: false
    rationale: "Test"
  checkpoint_token:
    description: "Checkpoint token"
    secrets_mode: app_secret
    public_full_transcript_allowed: false
    session_recording_allowed: false
    checkpoint_push_allowed: false
    rationale: "Test"
public_surfaces:
  github_issue_comment:
    agent_session_manifest_allowed: true
    raw_transcript_allowed: false
    source_kind_prohibited:
      - transcript
      - local_file
    rationale: "Test"
github_public_checkpoint_branch_allowed: false
checkpoint_remote:
  allowed_visibility:
    - private_verified
  fail_closed_on_unknown_visibility: true
  visibility_check_unknown_action: fail_closed
  verification_method:
    github_remote: "test"
    required_result: "PRIVATE"
auto_push_sessions_allowed: false
manual_review_required_before_push: true
kill_switch:
  trigger_conditions:
    - secrets_mode != none
  required_end_state:
    session_recording_tool_enabled: false
    git_hooks_recording_enabled: false
    public_checkpoint_branch_present: false
    auto_push_sessions_allowed: false
    full_transcript_remote_visibility: none
    leaked_credentials_rotated_or_revoked: true
  verification_required: true
```
"""

    policy_file = repo / "docs" / "dev" / "session-recording-policy.md"
    policy_file.write_text(policy_content)

    # Create secret policy file
    secret_file = repo / "docs" / "dev" / "secret-policy.md"
    secret_file.write_text("# Secret Policy\n")

    # Create manifest schema file
    schema_file = repo / "docs" / "schemas" / "agent-session-manifest.schema.json"
    schema_file.write_text("{}\n")

    # Create checker script (minimal passable version)
    checker_script = repo / ".claude" / "scripts" / "check_session_recording_policy.py"
    checker_script.write_text(
        """#!/usr/bin/env python3
# Minimal passable checker for tests
import sys
import os
from pathlib import Path

# Create marker file to verify checker was called
marker_dir = Path("tmp")
marker_dir.mkdir(exist_ok=True)
(marker_dir / "checker-called.txt").write_text("called")

sys.exit(0)
"""
    )
    checker_script.chmod(0o755)

    # Create settings.json
    settings_file = repo / ".claude" / "settings.json"
    settings_file.write_text("{}\n")

    # Create an initial commit so we can use git diff
    subprocess.run(
        ["git", "add", "."],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    return repo


class TestSessionRecordingPolicyGuard:
    """Test suite for session_recording_policy_guard.sh."""

    def test_01_no_watched_changes_exits_zero(self, tmp_path: Path) -> None:
        """TC-1: No watched file changes -> exit 0.

        When a hook runs with no changes to watched files,
        it should exit with 0.
        """
        repo = create_test_repo(tmp_path)

        stdin = {"hook_event_name": "Stop", "cwd": str(repo), "stop_hook_active": False}
        exit_code, _, _ = run_hook(repo, stdin)

        assert exit_code == 0, "Expected exit 0 when no watched files changed"

    def test_02_watched_file_changed_checker_pass(self, tmp_path: Path) -> None:
        """TC-2: Watched file changed + checker pass -> exit 0.

        When a watched file changes and the checker passes,
        the hook should exit with 0.
        """
        repo = create_test_repo(tmp_path)

        # Modify a watched file
        policy_file = repo / "docs" / "dev" / "session-recording-policy.md"
        policy_file.write_text(policy_file.read_text() + "\nTest change\n")

        stdin = {"hook_event_name": "Stop", "cwd": str(repo), "stop_hook_active": False}
        exit_code, _, _ = run_hook(repo, stdin)

        assert exit_code == 0, "Expected exit 0 when changes detected but checker passes"

    def test_03_watched_file_changed_checker_fail(self, tmp_path: Path) -> None:
        """TC-3: Watched file changed + checker fail -> exit 2 + SESSION_RECORDING_POLICY_GUARD.

        When a watched file changes and the checker fails,
        the hook should exit with 2 and output SESSION_RECORDING_POLICY_GUARD in stderr.
        """
        repo = create_test_repo(tmp_path)

        # Make checker fail by breaking the policy file
        policy_file = repo / "docs" / "dev" / "session-recording-policy.md"
        policy_file.write_text("Invalid policy content")

        # Replace checker with one that always fails
        checker_script = repo / ".claude" / "scripts" / "check_session_recording_policy.py"
        checker_script.write_text(
            """#!/usr/bin/env python3
import sys
sys.exit(1)
"""
        )

        stdin = {"hook_event_name": "Stop", "cwd": str(repo), "stop_hook_active": False}
        exit_code, _, stderr = run_hook(repo, stdin)

        assert exit_code == 2, "Expected exit 2 when checker fails"
        assert "SESSION_RECORDING_POLICY_GUARD" in stderr, \
            "Expected SESSION_RECORDING_POLICY_GUARD in stderr"

    def test_04_untracked_watched_file_calls_checker(self, tmp_path: Path) -> None:
        """TC-4: Untracked watched file -> checker is called.

        When an untracked file in watched paths is detected,
        the checker should be invoked and a marker file should be created.
        """
        repo = create_test_repo(tmp_path)

        # IMPORTANT: In initial repo, watched file is tracked.
        # Remove the tracked version to make it truly untracked
        subprocess.run(
            ["git", "rm", "--cached", "docs/dev/session-recording-policy.md"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Remove tracked policy file"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )

        # Now create an untracked watched file with new content
        untracked_file = repo / "docs" / "dev" / "session-recording-policy.md"
        untracked_file.write_text("New untracked policy\n")

        # Verify file is untracked
        ls_files_result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "--", "docs/dev/session-recording-policy.md"],
            cwd=str(repo),
            check=True,
            text=True,
            capture_output=True,
        )
        assert "session-recording-policy.md" in ls_files_result.stdout, \
            "Expected file to be in untracked list"

        # Clear any existing marker from previous tests
        marker_file = repo / "tmp" / "checker-called.txt"
        if marker_file.exists():
            marker_file.unlink()

        stdin = {"hook_event_name": "Stop", "cwd": str(repo), "stop_hook_active": False}
        exit_code, _, _ = run_hook(repo, stdin)

        # Verify checker was called by checking marker file
        assert exit_code == 0, "Expected exit 0 when untracked file detected and checker passes"
        assert marker_file.exists(), "Expected marker file to exist after checker call"
        assert marker_file.read_text().strip() == "called", \
            "Expected marker file to contain 'called'"

    def test_05_stop_fixture_json_stdin(self, tmp_path: Path) -> None:
        """TC-5: Stop fixture JSON stdin -> deterministic result.

        When hook receives Stop event JSON,
        it should process correctly with cwd from stdin.
        """
        repo = create_test_repo(tmp_path)

        stdin = {
            "hook_event_name": "Stop",
            "cwd": str(repo),
            "stop_hook_active": False,
        }
        exit_code, _, _ = run_hook(repo, stdin)

        assert exit_code == 0, "Expected exit 0 for Stop event with no changes"

    def test_06_subagent_stop_fixture_json_stdin(self, tmp_path: Path) -> None:
        """TC-6: SubagentStop fixture JSON stdin -> deterministic result.

        When hook receives SubagentStop event JSON,
        it should process correctly with agent metadata.
        """
        repo = create_test_repo(tmp_path)

        stdin = {
            "hook_event_name": "SubagentStop",
            "cwd": str(repo),
            "agent_id": "test-agent-123",
            "agent_type": "implementation-worker",
            "agent_transcript_path": "/tmp/transcript.log",
            "stop_hook_active": False,
        }
        exit_code, _, _ = run_hook(repo, stdin)

        assert exit_code == 0, "Expected exit 0 for SubagentStop event with no changes"

    def test_07_stop_hook_active_true_short_circuits(self, tmp_path: Path) -> None:
        """TC-7: stop_hook_active: true -> exit 0 short-circuit.

        When stop_hook_active flag is true (8-time override),
        the hook should exit 0 immediately without running checker,
        even if watched files changed.
        """
        repo = create_test_repo(tmp_path)

        # Modify a watched file
        policy_file = repo / "docs" / "dev" / "session-recording-policy.md"
        policy_file.write_text(policy_file.read_text() + "\nChange that should be ignored\n")

        # Make checker fail
        checker_script = repo / ".claude" / "scripts" / "check_session_recording_policy.py"
        checker_script.write_text(
            """#!/usr/bin/env python3
import sys
sys.exit(1)
"""
        )

        stdin = {"hook_event_name": "Stop", "cwd": str(repo), "stop_hook_active": True}
        exit_code, _, _ = run_hook(repo, stdin)

        assert exit_code == 0, \
            "Expected exit 0 short-circuit when stop_hook_active is true (ignoring checker failure)"

    def test_08_invalid_non_repo_cwd_exits_two(self, tmp_path: Path) -> None:
        """TC-8: Invalid/non-repo cwd -> exit 2.

        When cwd is not a git repository,
        the hook should exit with 2 (fail-closed).
        """
        non_repo_dir = tmp_path / "not_a_repo"
        non_repo_dir.mkdir()

        stdin = {"hook_event_name": "Stop", "cwd": str(non_repo_dir), "stop_hook_active": False}
        exit_code, _, stderr = run_hook(non_repo_dir, stdin)

        assert exit_code == 2, "Expected exit 2 for non-git-repo directory"
        assert "SESSION_RECORDING_POLICY_GUARD" in stderr, \
            "Expected SESSION_RECORDING_POLICY_GUARD error message"

    def test_10_stop_payload_with_change_and_fail_returns_exit_2(self, tmp_path: Path) -> None:
        """TC-10: Stop fixture with watched change + checker fail -> exit 2.

        When Stop event has watched file changes and checker fails,
        the hook should exit 2 with SESSION_RECORDING_POLICY_GUARD in stderr.
        """
        repo = create_test_repo(tmp_path)

        # Modify a watched file
        policy_file = repo / "docs" / "dev" / "session-recording-policy.md"
        policy_file.write_text("Intentionally broken policy\n")

        # Make checker fail
        checker_script = repo / ".claude" / "scripts" / "check_session_recording_policy.py"
        checker_script.write_text(
            """#!/usr/bin/env python3
import sys
sys.exit(1)
"""
        )

        stdin = {
            "hook_event_name": "Stop",
            "cwd": str(repo),
            "stop_hook_active": False,
        }
        exit_code, _, stderr = run_hook(repo, stdin)

        assert exit_code == 2, "Expected exit 2 when Stop event has changes and checker fails"
        assert "SESSION_RECORDING_POLICY_GUARD" in stderr, \
            "Expected SESSION_RECORDING_POLICY_GUARD in stderr"

    def test_11_subagent_stop_payload_with_change_and_fail_returns_exit_2(self, tmp_path: Path) -> None:
        """TC-11: SubagentStop fixture with watched change + checker fail -> exit 2.

        When SubagentStop event has watched file changes and checker fails,
        the hook should exit 2 with SESSION_RECORDING_POLICY_GUARD in stderr.
        """
        repo = create_test_repo(tmp_path)

        # Modify a watched file
        policy_file = repo / "docs" / "dev" / "session-recording-policy.md"
        policy_file.write_text("Intentionally broken policy\n")

        # Make checker fail
        checker_script = repo / ".claude" / "scripts" / "check_session_recording_policy.py"
        checker_script.write_text(
            """#!/usr/bin/env python3
import sys
sys.exit(1)
"""
        )

        stdin = {
            "hook_event_name": "SubagentStop",
            "cwd": str(repo),
            "agent_id": "test-agent-123",
            "agent_type": "implementation-worker",
            "agent_transcript_path": "/tmp/transcript.log",
            "stop_hook_active": False,
        }
        exit_code, _, stderr = run_hook(repo, stdin)

        assert exit_code == 2, "Expected exit 2 when SubagentStop event has changes and checker fails"
        assert "SESSION_RECORDING_POLICY_GUARD" in stderr, \
            "Expected SESSION_RECORDING_POLICY_GUARD in stderr"

    def test_12_subagent_stop_with_stop_hook_active_short_circuits(self, tmp_path: Path) -> None:
        """TC-12: SubagentStop with stop_hook_active: true -> exit 0.

        When SubagentStop has stop_hook_active flag set to true,
        the hook should exit 0 immediately even with watched changes and checker failure.
        """
        repo = create_test_repo(tmp_path)

        # Modify a watched file
        policy_file = repo / "docs" / "dev" / "session-recording-policy.md"
        policy_file.write_text("Intentionally broken policy\n")

        # Make checker fail
        checker_script = repo / ".claude" / "scripts" / "check_session_recording_policy.py"
        checker_script.write_text(
            """#!/usr/bin/env python3
import sys
sys.exit(1)
"""
        )

        stdin = {
            "hook_event_name": "SubagentStop",
            "cwd": str(repo),
            "agent_id": "test-agent-123",
            "agent_type": "implementation-worker",
            "agent_transcript_path": "/tmp/transcript.log",
            "stop_hook_active": True,
        }
        exit_code, _, _ = run_hook(repo, stdin)

        assert exit_code == 0, \
            "Expected exit 0 short-circuit when SubagentStop has stop_hook_active: true"

    def test_09_settings_json_has_hooks_with_proper_structure(self) -> None:
        """TC-9: .claude/settings.json has Stop/SubagentStop hooks with proper structure.

        Verify that the main settings.json file contains hook handlers for Stop and SubagentStop
        with exact matching:
        - type: "command"
        - command == "${CLAUDE_PROJECT_DIR}/.claude/hooks/session_recording_policy_guard.sh"
        - args: [] (empty list)
        - timeout >= 30
        """
        assert SETTINGS_JSON_PATH.exists(), \
            f"settings.json not found at {SETTINGS_JSON_PATH}"

        with open(SETTINGS_JSON_PATH, encoding="utf-8") as f:
            settings = json.load(f)

        # Check hooks section exists
        assert "hooks" in settings, "hooks section not found in settings.json"
        hooks = settings["hooks"]

        # Check Stop handler
        assert "Stop" in hooks, "Stop handler not found in hooks"
        stop_handlers = hooks["Stop"]
        assert isinstance(stop_handlers, list), "Stop handlers should be a list"
        assert len(stop_handlers) > 0, "Stop handlers should not be empty"

        # Check SubagentStop handler
        assert "SubagentStop" in hooks, "SubagentStop handler not found in hooks"
        subagent_stop_handlers = hooks["SubagentStop"]
        assert isinstance(subagent_stop_handlers, list), "SubagentStop handlers should be a list"
        assert len(subagent_stop_handlers) > 0, "SubagentStop handlers should not be empty"

        expected_command = "${CLAUDE_PROJECT_DIR}/.claude/hooks/session_recording_policy_guard.sh"

        # Validate Stop handler structure (exact match)
        stop_found = False
        for handler_group in stop_handlers:
            if "hooks" in handler_group and isinstance(handler_group["hooks"], list):
                for hook in handler_group["hooks"]:
                    if (hook.get("type") == "command" and
                        hook.get("command") == expected_command and
                        hook.get("args") == [] and
                        hook.get("timeout", 0) >= 30):
                        stop_found = True
                        break

        assert stop_found, \
            f"Stop handler does not have proper structure: type=command, command={expected_command}, args=[], timeout>=30"

        # Validate SubagentStop handler structure (exact match)
        subagent_found = False
        for handler_group in subagent_stop_handlers:
            if "hooks" in handler_group and isinstance(handler_group["hooks"], list):
                for hook in handler_group["hooks"]:
                    if (hook.get("type") == "command" and
                        hook.get("command") == expected_command and
                        hook.get("args") == [] and
                        hook.get("timeout", 0) >= 30):
                        subagent_found = True
                        break

        assert subagent_found, \
            f"SubagentStop handler does not have proper structure: type=command, command={expected_command}, args=[], timeout>=30"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
