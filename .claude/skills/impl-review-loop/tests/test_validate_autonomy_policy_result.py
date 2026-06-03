"""Tests for validate_autonomy_policy_result.py

Covers:
- --help shows correct flags, exit 0
- valid marker → pass
- no marker → blocked
- malformed YAML frontmatter → blocked
- explicit tools not declared → blocked
- read-only agent with Write/Edit → blocked
- write-capable implementation-worker with policy justification → pass
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml


# Path to the script under test (relative to repo root, resolved at runtime)
SCRIPT = Path(__file__).parent.parent / "scripts" / "validate_autonomy_policy_result.py"

# Minimal real-ish policy content (just enough to satisfy checks)
POLICY_CONTENT = textwrap.dedent("""\
    ---
    schema_version: 1
    policy_id: AUTONOMY_POLICY_V1
    ---
    # AUTONOMY_POLICY_V1

    ```yaml
    AUTONOMY_POLICY_V1:
      write_capable_agents:
        - agent: .claude/agents/implementation-worker.md
          role_category: implementation
          justification: |
            implementation-worker needs Edit/Write/MultiEdit for file creation.
    ```
    """)

# Minimal valid implementation-worker frontmatter (write-capable)
IMPL_WORKER_FM = textwrap.dedent("""\
    ---
    name: implementation-worker
    tools:
      - Read
      - Grep
      - Glob
      - Bash
      - Edit
      - Write
      - MultiEdit
    permissionMode: acceptEdits
    ---
    content
    """)

# Minimal valid test-runner frontmatter (read-only)
TEST_RUNNER_FM = textwrap.dedent("""\
    ---
    name: test-runner
    tools:
      - Read
      - Grep
      - Glob
      - Bash
    disallowedTools:
      - Edit
      - Write
      - MultiEdit
    permissionMode: dontAsk
    ---
    content
    """)

# Minimal valid pr-reviewer frontmatter (read-only)
PR_REVIEWER_FM = textwrap.dedent("""\
    ---
    name: pr-reviewer
    tools:
      - Bash
      - Read
      - Grep
      - Glob
    disallowedTools:
      - Edit
      - Write
      - MultiEdit
    permissionMode: dontAsk
    ---
    content
    """)

# Terminal output with the required marker
VALID_TERMINAL = textwrap.dedent("""\
    ## impl-review-loop: 完了

    ```yaml
    IMPL_REVIEW_LOOP_RESULT_V1:
      schema_version: 1
      status: draft_pr_ready
      termination_reason: approved
    ```
    """)

# Terminal output WITHOUT the required marker (freeform prose)
INVALID_TERMINAL = textwrap.dedent("""\
    The implementation looks great. All tests pass. The PR is ready.
    No machine-readable output here.
    """)


def run_script(
    tmp_path: Path,
    *,
    policy_content: str = POLICY_CONTENT,
    agent_files: dict[str, str] | None = None,
    terminal_content: str = VALID_TERMINAL,
) -> subprocess.CompletedProcess:
    """Helper: write temp files and run the validator script."""
    # Default agent files
    if agent_files is None:
        agent_files = {
            "implementation-worker.md": IMPL_WORKER_FM,
            "test-runner.md": TEST_RUNNER_FM,
            "pr-reviewer.md": PR_REVIEWER_FM,
        }

    policy_file = tmp_path / "autonomy-policy.md"
    policy_file.write_text(policy_content, encoding="utf-8")

    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    for name, content in agent_files.items():
        (agent_dir / name).write_text(content, encoding="utf-8")

    terminal_file = tmp_path / "terminal-output.txt"
    terminal_file.write_text(terminal_content, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--policy", str(policy_file),
            "--agent-dir", str(agent_dir),
            "--terminal-output-file", str(terminal_file),
        ],
        capture_output=True,
        text=True,
    )
    return result


# ---------------------------------------------------------------------------
# AC8: --help shows correct flags, exits 0
# ---------------------------------------------------------------------------

class TestHelp:
    def test_help_shows_all_flags_and_exits_0(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"--help returned non-zero: {result.returncode}"
        assert "--policy" in result.stdout, "--policy not in --help output"
        assert "--agent-dir" in result.stdout, "--agent-dir not in --help output"
        assert "--terminal-output-file" in result.stdout, "--terminal-output-file not in --help output"


# ---------------------------------------------------------------------------
# AC4 / AC6: valid marker → pass
# ---------------------------------------------------------------------------

class TestValidMarker:
    def test_valid_marker_returns_pass(self, tmp_path):
        result = run_script(tmp_path, terminal_content=VALID_TERMINAL)
        assert result.returncode == 0, f"Expected exit 0 (pass), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "pass"
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["terminal_result_marker"]["found"] is True
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["blocked_reasons"] == []


# ---------------------------------------------------------------------------
# AC3 / AC4: no marker → blocked (exit 1)
# ---------------------------------------------------------------------------

class TestNoMarker:
    def test_no_marker_returns_blocked(self, tmp_path):
        result = run_script(tmp_path, terminal_content=INVALID_TERMINAL)
        assert result.returncode == 1, f"Expected exit 1 (blocked), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "blocked"
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["terminal_result_marker"]["found"] is False
        assert len(output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["blocked_reasons"]) > 0


# ---------------------------------------------------------------------------
# AC9: malformed YAML frontmatter → blocked
# ---------------------------------------------------------------------------

class TestMalformed:
    def test_malformed_yaml_frontmatter_returns_blocked(self, tmp_path):
        malformed_agent_fm = textwrap.dedent("""\
            ---
            name: test-runner
            tools: [Read, Grep
            # malformed: missing closing bracket
            ---
            content
            """)
        agent_files = {
            "implementation-worker.md": IMPL_WORKER_FM,
            "test-runner.md": malformed_agent_fm,
            "pr-reviewer.md": PR_REVIEWER_FM,
        }
        result = run_script(tmp_path, agent_files=agent_files)
        assert result.returncode == 1, f"Expected exit 1 (blocked), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "blocked"
        assert len(output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["blocked_reasons"]) > 0


# ---------------------------------------------------------------------------
# AC10: explicit tools not declared → blocked
# ---------------------------------------------------------------------------

class TestExplicitTools:
    def test_explicit_tools_not_declared_returns_blocked(self, tmp_path):
        """Agent without 'tools' declaration fails explicit_tools check."""
        no_tools_fm = textwrap.dedent("""\
            ---
            name: test-runner
            permissionMode: dontAsk
            disallowedTools:
              - Edit
              - Write
              - MultiEdit
            ---
            content
            """)
        agent_files = {
            "implementation-worker.md": IMPL_WORKER_FM,
            "test-runner.md": no_tools_fm,
            "pr-reviewer.md": PR_REVIEWER_FM,
        }
        result = run_script(tmp_path, agent_files=agent_files)
        assert result.returncode == 1, f"Expected exit 1 (blocked), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "blocked"
        reasons = output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["blocked_reasons"]
        assert any("test-runner.md" in r and "tools" in r for r in reasons), (
            f"Expected a reason mentioning test-runner.md and tools, got: {reasons}"
        )


# ---------------------------------------------------------------------------
# AC11: read-only agent with Write/Edit → blocked
# ---------------------------------------------------------------------------

class TestReadOnly:
    def test_read_only_agent_with_write_tool_returns_blocked(self, tmp_path):
        """A 'read-only' agent that has Write in its tools list fails the check."""
        rogue_test_runner_fm = textwrap.dedent("""\
            ---
            name: test-runner
            tools:
              - Read
              - Grep
              - Glob
              - Bash
              - Write
            disallowedTools:
              - Edit
              - MultiEdit
            permissionMode: dontAsk
            ---
            content
            """)
        agent_files = {
            "implementation-worker.md": IMPL_WORKER_FM,
            "test-runner.md": rogue_test_runner_fm,
            "pr-reviewer.md": PR_REVIEWER_FM,
        }
        result = run_script(tmp_path, agent_files=agent_files)
        assert result.returncode == 1, f"Expected exit 1 (blocked), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "blocked"
        reasons = output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["blocked_reasons"]
        assert any("test-runner.md" in r for r in reasons), (
            f"Expected a reason mentioning test-runner.md, got: {reasons}"
        )


# ---------------------------------------------------------------------------
# AC12: write-capable implementation-worker with policy justification → pass
# ---------------------------------------------------------------------------

class TestWriteCapable:
    def test_write_capable_with_policy_justification_returns_pass(self, tmp_path):
        """implementation-worker with Edit/Write in tools AND policy justification → pass."""
        result = run_script(
            tmp_path,
            agent_files={
                "implementation-worker.md": IMPL_WORKER_FM,
                "test-runner.md": TEST_RUNNER_FM,
                "pr-reviewer.md": PR_REVIEWER_FM,
            },
            terminal_content=VALID_TERMINAL,
        )
        assert result.returncode == 0, f"Expected exit 0 (pass), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "pass"

    def test_write_capable_without_policy_justification_returns_blocked(self, tmp_path):
        """implementation-worker NOT referenced in policy → blocked."""
        policy_without_impl = textwrap.dedent("""\
            ---
            schema_version: 1
            ---
            # AUTONOMY_POLICY_V1 (no write_capable_agents section)
            """)
        result = run_script(
            tmp_path,
            policy_content=policy_without_impl,
            agent_files={
                "implementation-worker.md": IMPL_WORKER_FM,
                "test-runner.md": TEST_RUNNER_FM,
                "pr-reviewer.md": PR_REVIEWER_FM,
            },
            terminal_content=VALID_TERMINAL,
        )
        assert result.returncode == 1, f"Expected exit 1 (blocked), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "blocked"
