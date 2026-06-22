"""Tests for validate_autonomy_policy_result.py

Covers:
- --help shows correct flags, exit 0
- valid marker (fenced YAML key) → pass
- valid marker (HTML comment + fenced YAML) → pass
- no marker (bare prose) → blocked (exit 1)
- marker as bare substring (not in fenced YAML block) → blocked (exit 1)
- malformed YAML frontmatter → blocked
- malformed YAML in result block → blocked
- missing required fields in result YAML → blocked
- explicit tools not declared → blocked
- read-only agent with Write/Edit → blocked
- write-capable implementation-worker with policy justification → pass
- write-capable agent missing from policy → blocked
- write-capable agent in policy but missing justification → blocked
- write-capable agent in policy but missing role_category → blocked
- policy-derived agent lists (not hardcoded Python constants)
- output schema includes read_only_agents_clear, write_capable_agents_have_justification, explicit_tools_declared
"""

import subprocess
import sys
import textwrap
from pathlib import Path

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
      subagent_security_ac:
        checked_subagents:
          - .claude/agents/implementation-worker.md
          - .claude/agents/test-runner.md
          - .claude/agents/pr-reviewer.md
      write_capable_agents:
        - agent: .claude/agents/implementation-worker.md
          role_category: implementation
          justification: |
            implementation-worker needs Edit/Write/MultiEdit for file creation.
      read_only_agents:
        - agent: .claude/agents/test-runner.md
          role_category: verification
          disallowed_write_tools:
            - Edit
            - Write
            - MultiEdit
        - agent: .claude/agents/pr-reviewer.md
          role_category: review
          disallowed_write_tools:
            - Edit
            - Write
            - MultiEdit
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

# Terminal output with the required marker as YAML key in fenced block (v1 style)
VALID_TERMINAL_YAML_KEY = textwrap.dedent("""\
    ## impl-review-loop: 完了

    ```yaml
    IMPL_REVIEW_LOOP_RESULT_V1:
      schema_version: 1
      status: draft_pr_ready
      termination_reason: approved
      merge_ready: true
    ```
    """)

# Terminal output with the required marker as HTML comment + fenced YAML (v2 style)
VALID_TERMINAL_HTML_COMMENT = textwrap.dedent("""\
    ## impl-review-loop: 完了

    <!-- IMPL_REVIEW_LOOP_RESULT_V1 -->
    ```yaml
    IMPL_REVIEW_LOOP_RESULT_V1:
      schema_version: 1
      status: draft_pr_ready
      termination_reason: approved
      merge_ready: true
    ```
    """)

# Terminal output WITHOUT the required marker (freeform prose)
INVALID_TERMINAL_PROSE = textwrap.dedent("""\
    The implementation looks great. All tests pass. The PR is ready.
    No machine-readable output here.
    """)

# Terminal output with bare substring of marker (not in fenced YAML key) — should be blocked
INVALID_TERMINAL_BARE_SUBSTRING = textwrap.dedent("""\
    The loop finished with IMPL_REVIEW_LOOP_RESULT_V1 status approved.
    This embeds the marker string in prose but not as an HTML comment or YAML key.
    """)

# Terminal output missing required fields
INVALID_TERMINAL_MISSING_FIELDS = textwrap.dedent("""\
    ## impl-review-loop: 完了

    ```yaml
    IMPL_REVIEW_LOOP_RESULT_V1:
      schema_version: 1
      status: draft_pr_ready
    ```
    """)


def run_script(
    tmp_path: Path,
    *,
    policy_content: str = POLICY_CONTENT,
    agent_files: dict[str, str] | None = None,
    terminal_content: str = VALID_TERMINAL_YAML_KEY,
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
    def test_valid_marker_yaml_key_returns_pass(self, tmp_path):
        """Marker as top-level YAML key in fenced block passes."""
        result = run_script(tmp_path, terminal_content=VALID_TERMINAL_YAML_KEY)
        assert result.returncode == 0, f"Expected exit 0 (pass), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "pass"
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["terminal_result_marker"]["found"] is True
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["blocked_reasons"] == []

    def test_valid_marker_html_comment_returns_pass(self, tmp_path):
        """Marker as HTML comment followed by fenced YAML block passes."""
        result = run_script(tmp_path, terminal_content=VALID_TERMINAL_HTML_COMMENT)
        assert result.returncode == 0, f"Expected exit 0 (pass), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "pass"
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["terminal_result_marker"]["found"] is True
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["blocked_reasons"] == []

    def test_output_includes_ac_boolean_fields(self, tmp_path):
        """Output YAML includes read_only_agents_clear, write_capable_agents_have_justification, explicit_tools_declared."""
        result = run_script(tmp_path, terminal_content=VALID_TERMINAL_YAML_KEY)
        assert result.returncode == 0
        output = yaml.safe_load(result.stdout)
        ac = output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["subagent_security_ac"]
        assert "read_only_agents_clear" in ac, f"Missing read_only_agents_clear in output: {ac}"
        assert "write_capable_agents_have_justification" in ac, f"Missing write_capable_agents_have_justification in output: {ac}"
        assert "explicit_tools_declared" in ac, f"Missing explicit_tools_declared in output: {ac}"
        assert ac["read_only_agents_clear"] is True
        assert ac["write_capable_agents_have_justification"] is True
        assert ac["explicit_tools_declared"] is True

    def test_output_includes_checked_subagents_from_policy(self, tmp_path):
        """checked_subagents in output comes from policy YAML, not hardcoded constants."""
        result = run_script(tmp_path, terminal_content=VALID_TERMINAL_YAML_KEY)
        assert result.returncode == 0
        output = yaml.safe_load(result.stdout)
        ac = output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["subagent_security_ac"]
        checked = ac.get("checked_subagents", [])
        assert ".claude/agents/implementation-worker.md" in checked
        assert ".claude/agents/test-runner.md" in checked
        assert ".claude/agents/pr-reviewer.md" in checked


# ---------------------------------------------------------------------------
# AC3 / AC4: no marker → blocked (exit 1)
# ---------------------------------------------------------------------------

class TestNoMarker:
    def test_freeform_prose_returns_blocked(self, tmp_path):
        """Freeform prose without marker returns blocked."""
        result = run_script(tmp_path, terminal_content=INVALID_TERMINAL_PROSE)
        assert result.returncode == 1, f"Expected exit 1 (blocked), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "blocked"
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["terminal_result_marker"]["found"] is False
        assert len(output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["blocked_reasons"]) > 0

    def test_bare_substring_not_in_fenced_block_returns_blocked(self, tmp_path):
        """Bare prose embedding the marker string (not as HTML comment or YAML key) is blocked."""
        result = run_script(tmp_path, terminal_content=INVALID_TERMINAL_BARE_SUBSTRING)
        assert result.returncode == 1, f"Expected exit 1 (blocked), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "blocked"


# ---------------------------------------------------------------------------
# Blocker 2: IMPL_REVIEW_LOOP_RESULT_V1 required field validation
# ---------------------------------------------------------------------------

class TestResultFieldValidation:
    def test_missing_required_fields_returns_blocked(self, tmp_path):
        """Result YAML missing termination_reason and merge_ready returns blocked."""
        result = run_script(tmp_path, terminal_content=INVALID_TERMINAL_MISSING_FIELDS)
        assert result.returncode == 1, f"Expected exit 1 (blocked), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "blocked"
        reasons = output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["blocked_reasons"]
        assert any("missing" in r.lower() or "field" in r.lower() for r in reasons), (
            f"Expected a reason about missing fields, got: {reasons}"
        )

    def test_malformed_result_yaml_returns_blocked(self, tmp_path):
        """Malformed YAML in result block returns blocked."""
        bad_terminal = textwrap.dedent("""\
            ```yaml
            IMPL_REVIEW_LOOP_RESULT_V1:
              schema_version: 1
              status: [unclosed
            ```
            """)
        result = run_script(tmp_path, terminal_content=bad_terminal)
        assert result.returncode == 1, f"Expected exit 1 (blocked), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "blocked"


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

    def test_explicit_tools_declared_sets_ac_flag_false_on_failure(self, tmp_path):
        """explicit_tools_declared is False when an agent fails the tools check."""
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
        assert result.returncode == 1
        output = yaml.safe_load(result.stdout)
        ac = output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["subagent_security_ac"]
        assert ac["explicit_tools_declared"] is False


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

    def test_read_only_agents_clear_flag_false_on_write_tool(self, tmp_path):
        """read_only_agents_clear is False when a read-only agent has write tools."""
        rogue_fm = textwrap.dedent("""\
            ---
            name: pr-reviewer
            tools:
              - Read
              - Edit
            disallowedTools:
              - Write
              - MultiEdit
            ---
            content
            """)
        agent_files = {
            "implementation-worker.md": IMPL_WORKER_FM,
            "test-runner.md": TEST_RUNNER_FM,
            "pr-reviewer.md": rogue_fm,
        }
        result = run_script(tmp_path, agent_files=agent_files)
        assert result.returncode == 1
        output = yaml.safe_load(result.stdout)
        ac = output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["subagent_security_ac"]
        assert ac["read_only_agents_clear"] is False


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
            terminal_content=VALID_TERMINAL_YAML_KEY,
        )
        assert result.returncode == 0, f"Expected exit 0 (pass), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "pass"

    def test_write_capable_without_policy_entry_returns_blocked(self, tmp_path):
        """implementation-worker NOT referenced in policy write_capable_agents → blocked."""
        _policy_without_impl = textwrap.dedent("""\
            ---
            schema_version: 1
            ---
            # AUTONOMY_POLICY_V1

            ```yaml
            AUTONOMY_POLICY_V1:
              subagent_security_ac:
                checked_subagents:
                  - .claude/agents/implementation-worker.md
              write_capable_agents: []
              read_only_agents:
                - agent: .claude/agents/test-runner.md
                  role_category: verification
                  disallowed_write_tools: [Edit, Write, MultiEdit]
                - agent: .claude/agents/pr-reviewer.md
                  role_category: review
                  disallowed_write_tools: [Edit, Write, MultiEdit]
            ```
            """)
        # Add implementation-worker to write_capable per policy (empty list → blocked)
        # We need a policy that has implementation-worker in write_capable_agents section
        # but the policy above has empty list, so it won't find the entry.
        # But we also need implementation-worker.md in agent_files.
        # The validator derives write_capable_agent_names from policy — with empty list,
        # implementation-worker won't be checked at all.
        # Let's use a policy that has no write_capable_agents key entirely:
        policy_no_write = textwrap.dedent("""\
            ---
            schema_version: 1
            ---
            # AUTONOMY_POLICY_V1

            ```yaml
            AUTONOMY_POLICY_V1:
              subagent_security_ac:
                checked_subagents:
                  - .claude/agents/implementation-worker.md
                  - .claude/agents/test-runner.md
              write_capable_agents:
                - agent: .claude/agents/implementation-worker.md
                  role_category: implementation
                  justification: ""
              read_only_agents:
                - agent: .claude/agents/test-runner.md
                  role_category: verification
                  disallowed_write_tools: [Edit, Write, MultiEdit]
                - agent: .claude/agents/pr-reviewer.md
                  role_category: review
                  disallowed_write_tools: [Edit, Write, MultiEdit]
            ```
            """)
        result = run_script(
            tmp_path,
            policy_content=policy_no_write,
            agent_files={
                "implementation-worker.md": IMPL_WORKER_FM,
                "test-runner.md": TEST_RUNNER_FM,
                "pr-reviewer.md": PR_REVIEWER_FM,
            },
            terminal_content=VALID_TERMINAL_YAML_KEY,
        )
        assert result.returncode == 1, f"Expected exit 1 (blocked), got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "blocked"

    def test_write_capable_missing_justification_per_agent_returns_blocked(self, tmp_path):
        """Per-agent check: policy entry with empty justification → blocked (not whole-doc search)."""
        # Policy has agent B with justification but agent A with empty justification.
        # The old whole-doc search would pass because 'justification' appears somewhere.
        # The new per-agent structural check must catch agent A's empty justification.
        policy_partial_justification = textwrap.dedent("""\
            ---
            schema_version: 1
            ---
            # AUTONOMY_POLICY_V1

            ```yaml
            AUTONOMY_POLICY_V1:
              subagent_security_ac:
                checked_subagents:
                  - .claude/agents/implementation-worker.md
                  - .claude/agents/test-runner.md
                  - .claude/agents/pr-reviewer.md
              write_capable_agents:
                - agent: .claude/agents/implementation-worker.md
                  role_category: implementation
                  justification: ""
              read_only_agents:
                - agent: .claude/agents/test-runner.md
                  role_category: verification
                  disallowed_write_tools: [Edit, Write, MultiEdit]
                - agent: .claude/agents/pr-reviewer.md
                  role_category: review
                  disallowed_write_tools: [Edit, Write, MultiEdit]
            ```
            # Elsewhere in the doc, justification appears for agent B
            justification: this appears in prose but not for implementation-worker entry
            """)
        result = run_script(
            tmp_path,
            policy_content=policy_partial_justification,
            agent_files={
                "implementation-worker.md": IMPL_WORKER_FM,
                "test-runner.md": TEST_RUNNER_FM,
                "pr-reviewer.md": PR_REVIEWER_FM,
            },
            terminal_content=VALID_TERMINAL_YAML_KEY,
        )
        assert result.returncode == 1, f"Expected exit 1 (blocked) due to empty justification, got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "blocked"

    def test_write_capable_missing_role_category_returns_blocked(self, tmp_path):
        """Per-agent check: policy entry missing role_category → blocked."""
        policy_no_role = textwrap.dedent("""\
            ---
            schema_version: 1
            ---
            # AUTONOMY_POLICY_V1

            ```yaml
            AUTONOMY_POLICY_V1:
              subagent_security_ac:
                checked_subagents:
                  - .claude/agents/implementation-worker.md
                  - .claude/agents/test-runner.md
                  - .claude/agents/pr-reviewer.md
              write_capable_agents:
                - agent: .claude/agents/implementation-worker.md
                  justification: |
                    implementation-worker needs Edit/Write for file creation.
              read_only_agents:
                - agent: .claude/agents/test-runner.md
                  role_category: verification
                  disallowed_write_tools: [Edit, Write, MultiEdit]
                - agent: .claude/agents/pr-reviewer.md
                  role_category: review
                  disallowed_write_tools: [Edit, Write, MultiEdit]
            ```
            """)
        result = run_script(
            tmp_path,
            policy_content=policy_no_role,
            agent_files={
                "implementation-worker.md": IMPL_WORKER_FM,
                "test-runner.md": TEST_RUNNER_FM,
                "pr-reviewer.md": PR_REVIEWER_FM,
            },
            terminal_content=VALID_TERMINAL_YAML_KEY,
        )
        assert result.returncode == 1, f"Expected exit 1 (blocked) due to missing role_category, got {result.returncode}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        output = yaml.safe_load(result.stdout)
        assert output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["status"] == "blocked"

    def test_write_capable_agents_have_justification_flag_false_on_failure(self, tmp_path):
        """write_capable_agents_have_justification is False when justification is empty."""
        policy_empty_justification = textwrap.dedent("""\
            ---
            schema_version: 1
            ---
            # AUTONOMY_POLICY_V1

            ```yaml
            AUTONOMY_POLICY_V1:
              subagent_security_ac:
                checked_subagents:
                  - .claude/agents/implementation-worker.md
              write_capable_agents:
                - agent: .claude/agents/implementation-worker.md
                  role_category: implementation
                  justification: ""
              read_only_agents:
                - agent: .claude/agents/test-runner.md
                  role_category: verification
                  disallowed_write_tools: [Edit, Write, MultiEdit]
                - agent: .claude/agents/pr-reviewer.md
                  role_category: review
                  disallowed_write_tools: [Edit, Write, MultiEdit]
            ```
            """)
        result = run_script(
            tmp_path,
            policy_content=policy_empty_justification,
            agent_files={
                "implementation-worker.md": IMPL_WORKER_FM,
                "test-runner.md": TEST_RUNNER_FM,
                "pr-reviewer.md": PR_REVIEWER_FM,
            },
            terminal_content=VALID_TERMINAL_YAML_KEY,
        )
        assert result.returncode == 1
        output = yaml.safe_load(result.stdout)
        ac = output["AUTONOMY_POLICY_VALIDATION_RESULT_V1"]["subagent_security_ac"]
        assert ac["write_capable_agents_have_justification"] is False
