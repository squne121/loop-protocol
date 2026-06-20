"""Tests for agent-ops-auditor agent definition and configuration."""

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
CLAUDE_AGENT_MD = REPO_ROOT / ".claude" / "agents" / "agent-ops-auditor.md"
CODEX_AGENT_TOML = REPO_ROOT / ".codex" / "agents" / "agent-ops-auditor.toml"
CHECK_CODEX_AGENTS_MJS = REPO_ROOT / "scripts" / "check-codex-agents.mjs"


def test_ac1_claude_agent_exists_and_disallows_write_tools():
    """AC1: .claude/agents/agent-ops-auditor.md exists and disallows Write/Edit/MultiEdit/Agent."""
    assert CLAUDE_AGENT_MD.exists(), f"{CLAUDE_AGENT_MD} does not exist"
    content = CLAUDE_AGENT_MD.read_text(encoding="utf-8")
    # disallowedTools section must exist
    assert "disallowedTools:" in content, "disallowedTools section not found"
    # Each prohibited tool must be listed
    for tool in ("Write", "Edit", "MultiEdit", "Agent"):
        assert f"- {tool}" in content, f"Tool '{tool}' not found in disallowedTools"


def test_ac2_toml_uses_readonly_permissions():
    """AC2: .codex/agents/agent-ops-auditor.toml has default_permissions = 'loop-protocol-readonly'."""
    assert CODEX_AGENT_TOML.exists(), f"{CODEX_AGENT_TOML} does not exist"
    content = CODEX_AGENT_TOML.read_text(encoding="utf-8")
    assert 'default_permissions = "loop-protocol-readonly"' in content, (
        "default_permissions must be 'loop-protocol-readonly'"
    )


def test_ac3_claude_agent_contains_audit_result_schema():
    """AC3: .claude/agents/agent-ops-auditor.md contains AGENT_OPS_AUDIT_RESULT_V1."""
    assert CLAUDE_AGENT_MD.exists(), f"{CLAUDE_AGENT_MD} does not exist"
    content = CLAUDE_AGENT_MD.read_text(encoding="utf-8")
    assert "AGENT_OPS_AUDIT_RESULT_V1" in content, (
        "AGENT_OPS_AUDIT_RESULT_V1 schema not found in agent-ops-auditor.md"
    )


def test_ac4_claude_agent_references_artifact_path():
    """AC4: .claude/agents/agent-ops-auditor.md mentions artifact_path for raw logs reference."""
    assert CLAUDE_AGENT_MD.exists(), f"{CLAUDE_AGENT_MD} does not exist"
    content = CLAUDE_AGENT_MD.read_text(encoding="utf-8")
    assert "artifact_path" in content, (
        "artifact_path reference not found in agent-ops-auditor.md"
    )
    # raw logs should be referenced via artifact path, not dumped inline
    assert "artifact" in content.lower(), (
        "artifact reference not found — raw logs must be directed to artifact path"
    )


def test_ac5_check_codex_agents_mjs_includes_agent_ops_auditor():
    """AC5: scripts/check-codex-agents.mjs requiredAgentNames includes 'agent-ops-auditor'."""
    assert CHECK_CODEX_AGENTS_MJS.exists(), f"{CHECK_CODEX_AGENTS_MJS} does not exist"
    content = CHECK_CODEX_AGENTS_MJS.read_text(encoding="utf-8")
    assert "'agent-ops-auditor'" in content, (
        "'agent-ops-auditor' not found in check-codex-agents.mjs"
    )


def test_ac6_claude_agent_is_thin_wrapper():
    """AC6: .claude/agents/agent-ops-auditor.md is a thin wrapper (no duplicated procedure body)."""
    assert CLAUDE_AGENT_MD.exists(), f"{CLAUDE_AGENT_MD} does not exist"
    content = CLAUDE_AGENT_MD.read_text(encoding="utf-8")
    lines = content.splitlines()
    # thin wrapper: total line count should be reasonable (not a massive duplicated skill body)
    assert len(lines) < 200, (
        f"agent-ops-auditor.md has {len(lines)} lines — expected a thin wrapper (<200 lines)"
    )
    # Should not re-implement check-codex-agents logic inline
    assert "requiredAgentNames" not in content, (
        "requiredAgentNames found in .md — agent must not duplicate check-codex-agents.mjs logic"
    )
