"""Tests for the agent asset portability / inventory contract (Issue #2160).

Covers:
- AC1 (sub-requirement): permission-affecting frontmatter fields
  (tools:/disallowedTools:) must not silently drift from the checked-in
  baseline captured in tests/fixtures/codex-agent-config/
  agent_permission_baseline.json.
- AC2: every .claude/agents/*.md and .codex/agents/*.toml asset is
  uniquely classified (shared_claude_runtime / claude_only /
  legacy_codex_projection / legacy_codex_only / experimental), zero
  unclassified.
- AC4: mutation_class is not derived from permissionMode alone -- at
  least 3 negative (contradiction) cases are exercised.
- AC7: the frontmatter parser fails loudly (raises, does not silently
  skip) on unsupported YAML syntax; Task/Agent tool alias is normalized
  before tool-set comparison; duplicate agent name / unknown
  classification are detected as errors.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "check_claude_codex_agent_parity.py"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "codex-agent-config"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_claude_codex_agent_parity_portability", MODULE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["check_claude_codex_agent_parity_portability"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _load_module()


# ---------------------------------------------------------------------------
# AC1 (sub-requirement): permission-affecting frontmatter fields baseline
# ---------------------------------------------------------------------------

class TestPermissionBaselineUnchanged:
    def test_all_agents_tools_and_disallowed_tools_match_baseline(self):
        """AC1: normalized tools:/disallowedTools: for every .claude/agents/*.md
        must equal the checked-in baseline. This is the machine-verifiable
        guard that the `effort` frontmatter migration (and any future
        change) does not silently alter permission-affecting fields."""
        baseline = json.loads(
            (FIXTURE_DIR / "agent_permission_baseline.json").read_text(encoding="utf-8")
        )
        claude_dir = REPO_ROOT / ".claude" / "agents"
        discovered = sorted(p.name for p in claude_dir.glob("*.md"))
        assert discovered == sorted(baseline.keys()), (
            "agent_permission_baseline.json must be kept in sync with the "
            f"set of .claude/agents/*.md files. discovered={discovered} "
            f"baseline={sorted(baseline.keys())}"
        )
        mismatches = []
        for name, expected in baseline.items():
            text = (claude_dir / name).read_text(encoding="utf-8")
            fm = MOD.extract_frontmatter(text, source_path=str(claude_dir / name))
            tools = fm.get("tools", [])
            disallowed = fm.get("disallowedTools", [])
            tools = sorted(tools) if isinstance(tools, list) else []
            disallowed = sorted(disallowed) if isinstance(disallowed, list) else []
            if tools != expected["tools"] or disallowed != expected["disallowedTools"]:
                mismatches.append(
                    {
                        "agent": name,
                        "expected_tools": expected["tools"],
                        "actual_tools": tools,
                        "expected_disallowedTools": expected["disallowedTools"],
                        "actual_disallowedTools": disallowed,
                    }
                )
        assert mismatches == [], (
            f"permission-affecting frontmatter fields changed unexpectedly: {mismatches}"
        )

    def test_issue_reviewer_has_effort_frontmatter(self):
        """AC1: at least issue-reviewer.md carries an `effort` frontmatter field."""
        text = (REPO_ROOT / ".claude" / "agents" / "issue-reviewer.md").read_text(
            encoding="utf-8"
        )
        fm = MOD.extract_frontmatter(text, source_path="issue-reviewer.md")
        assert fm.get("effort"), "issue-reviewer.md must declare an effort frontmatter field"


# ---------------------------------------------------------------------------
# AC2: asset inventory classification completeness
# ---------------------------------------------------------------------------

class TestAssetInventoryClassification:
    def test_all_agent_assets_classified_no_unclassified(self):
        """AC2: every .claude/agents/*.md and .codex/agents/*.toml file has a
        unique classification in ALLOWED_CLASSIFICATIONS; zero unclassified."""
        expectations = json.loads(
            (FIXTURE_DIR / "expected-runtime-contract.json").read_text(encoding="utf-8")
        )
        classification = expectations["asset_classification"]
        claude_dir = REPO_ROOT / ".claude" / "agents"
        codex_dir = REPO_ROOT / ".codex" / "agents"
        failures = MOD.check_asset_classification_complete(classification, claude_dir, codex_dir)
        assert failures == [], f"unclassified or invalid assets found: {failures}"

        # Positive assertion: the enum values actually used are within the
        # allowed set (defends against a future typo in the fixture itself).
        used_values = set(classification.values())
        assert used_values <= MOD.ALLOWED_CLASSIFICATIONS, (
            f"asset_classification uses values outside ALLOWED_CLASSIFICATIONS: "
            f"{used_values - MOD.ALLOWED_CLASSIFICATIONS}"
        )

    def test_asset_classification_covers_every_discovered_file(self):
        """AC2: no discovered .md/.toml agent file is missing from the
        classification map (defends against the fixture going stale when a
        new agent file is added without updating the inventory)."""
        expectations = json.loads(
            (FIXTURE_DIR / "expected-runtime-contract.json").read_text(encoding="utf-8")
        )
        classification = expectations["asset_classification"]
        claude_dir = REPO_ROOT / ".claude" / "agents"
        codex_dir = REPO_ROOT / ".codex" / "agents"
        discovered = {f".claude/agents/{p.name}" for p in claude_dir.glob("*.md")}
        discovered |= {f".codex/agents/{p.name}" for p in codex_dir.glob("*.toml")}
        missing = discovered - classification.keys()
        assert missing == set(), f"discovered assets missing from asset_classification: {missing}"


# ---------------------------------------------------------------------------
# AC4: mutation_class is not derived from permissionMode alone
# ---------------------------------------------------------------------------

class TestMutationClassNotPermissionModeAlone:
    def test_mutation_class_not_derived_from_permission_mode_alone(self):
        """AC4: two agents sharing the same permissionMode ("dontAsk") but
        different declared tool sets must be able to produce different
        mutation_class values -- proving mutation_class is a function of
        (permission_mode, tools), not permission_mode alone."""
        readonly_case = MOD.derive_mutation_class("dontAsk", ["Bash", "Read"], ["Edit", "Write", "MultiEdit"])
        contradicted_case = MOD.derive_mutation_class("dontAsk", ["Bash", "Edit"], [])
        assert readonly_case == "readonly"
        assert contradicted_case != "readonly"
        assert readonly_case != contradicted_case, (
            "mutation_class must differ when the tool set differs, even "
            "though permission_mode is identical in both cases"
        )

    def test_dont_ask_with_bash_and_edit_granted_is_not_readonly(self):
        """AC4 negative case 1 (canonical example wording: `dontAsk + Bash !=
        read_only`): a dontAsk agent whose tools allowlist grants Bash
        *and* Edit is not read_only -- the Edit grant contradicts the
        permissionMode-only readonly assumption."""
        result = MOD.derive_mutation_class("dontAsk", ["Bash", "Edit"], [])
        assert result != "readonly", (
            f"dontAsk + Bash + Edit (mutation-capable tool granted, not "
            f"denied) must not collapse to readonly, got: {result}"
        )
        assert result == "repo-write"

    def test_dont_ask_with_write_granted_and_not_denied_is_not_readonly(self):
        """AC4 negative case 2: dontAsk + Write granted (and not explicitly
        denied) must not be readonly."""
        result = MOD.derive_mutation_class("dontAsk", ["Bash", "Write"], [])
        assert result != "readonly"
        assert result == "repo-write"

    def test_dont_ask_with_multiedit_granted_and_not_denied_is_not_readonly(self):
        """AC4 negative case 3: dontAsk + MultiEdit granted (and not
        explicitly denied) must not be readonly."""
        result = MOD.derive_mutation_class("dontAsk", ["Bash", "Read", "MultiEdit"], [])
        assert result != "readonly"
        assert result == "repo-write"

    def test_dont_ask_with_edit_but_explicitly_denied_stays_readonly(self):
        """Contrast case: dontAsk + Edit granted BUT Edit is also in
        disallowedTools (contradictory declaration; disallowedTools wins)
        stays readonly, matching every real .claude/agents/*.md file in
        this repository (Edit/Write/MultiEdit are explicitly denied even
        though not present in `tools:`)."""
        result = MOD.derive_mutation_class("dontAsk", ["Bash", "Edit"], ["Edit"])
        assert result == "readonly"


# ---------------------------------------------------------------------------
# AC7: frontmatter parser fails loudly / tool alias normalization / dup names
# ---------------------------------------------------------------------------

class TestFrontmatterParserFailsLoudly:
    def test_frontmatter_parser_rejects_unsupported_syntax(self):
        """AC7: an unsupported nested-mapping frontmatter value raises
        FrontmatterParseError instead of being silently skipped."""
        text = (
            "---\n"
            "name: broken-agent\n"
            "model: haiku\n"
            "nested:\n"
            "  inner: value\n"
            "permissionMode: dontAsk\n"
            "---\n\n"
            "body\n"
        )
        with pytest.raises(MOD.FrontmatterParseError):
            MOD.extract_frontmatter(text, source_path="broken-agent.md")

    def test_frontmatter_parser_rejects_inline_object(self):
        """AC7: a non-empty inline object value (`key: {a: b}`) is not
        supported and must raise, not silently produce a wrong string."""
        text = (
            "---\n"
            "name: broken-agent\n"
            "model: haiku\n"
            "config: {a: b}\n"
            "permissionMode: dontAsk\n"
            "---\n\n"
            "body\n"
        )
        with pytest.raises(MOD.FrontmatterParseError):
            MOD.extract_frontmatter(text, source_path="broken-agent.md")

    def test_frontmatter_parser_accepts_known_real_repo_forms(self):
        """Regression guard: the strict parser must still accept every real
        supported form in this repository (empty inline list `[]`, empty
        inline mapping `{}`, and folded block scalar `>-` descriptions),
        not just the naive subset."""
        text = (
            "---\n"
            "name: ok-agent\n"
            "description: >-\n"
            "  line one\n"
            "  line two\n"
            "tools: []\n"
            "mcpServers: []\n"
            "hooks: {}\n"
            "model: haiku\n"
            "permissionMode: dontAsk\n"
            "---\n\n"
            "body\n"
        )
        fm = MOD.extract_frontmatter(text, source_path="ok-agent.md")
        assert fm["name"] == "ok-agent"
        assert fm["description"] == "line one line two"
        assert fm["tools"] == []
        assert fm["hooks"] == {}

    def test_all_real_agent_files_parse_without_raising(self):
        """Regression guard: every real .claude/agents/*.md file in this
        repository must parse without raising FrontmatterParseError."""
        claude_dir = REPO_ROOT / ".claude" / "agents"
        for md_path in sorted(claude_dir.glob("*.md")):
            text = md_path.read_text(encoding="utf-8")
            MOD.extract_frontmatter(text, source_path=str(md_path))  # must not raise


class TestToolAliasNormalization:
    def test_task_alias_normalized_to_agent(self):
        """AC7: `Task` is normalized to `Agent` before any tool-set
        comparison."""
        assert MOD.normalize_tool_alias("Task") == "Agent"
        assert MOD.normalize_tool_alias("Task(subagent_type:foo)") == "Agent(subagent_type:foo)"
        assert MOD.normalize_tool_list(["Bash", "Task"]) == ["Bash", "Agent"]

    def test_disallowed_task_blocks_delegation_like_disallowed_agent(self):
        """AC7: an agent that disallows `Task` (the alias) is treated the
        same as disallowing `Agent` for nested-delegation detection."""
        claude_text = (
            "---\n"
            "name: alias-agent\n"
            "model: haiku\n"
            "tools:\n"
            "  - Bash\n"
            "  - Read\n"
            "permissionMode: dontAsk\n"
            "disallowedTools:\n"
            "  - Task\n"
            "---\n\n"
            "## \u51fa\u529b\u5951\u7d04\uff08ISSUE_REVIEW_RESULT_COMPACT_V1\uff09\n"
        )
        claude_path = REPO_ROOT / "tests" / "fixtures" / "agent-parity" / "__alias_probe__.md"
        facts = MOD.extract_claude_facts("alias-agent", claude_path, claude_text)
        assert facts.nested_delegation_blocked is True
        assert "Agent" in facts.claude_disallowed_tools


class TestDuplicateAgentNameDetection:
    def test_check_duplicate_agent_names_detects_duplicate_claude_names(self, tmp_path):
        claude_dir = tmp_path / ".claude" / "agents"
        claude_dir.mkdir(parents=True)
        codex_dir = tmp_path / ".codex" / "agents"
        codex_dir.mkdir(parents=True)
        (claude_dir / "a.md").write_text(
            "---\nname: dup-agent\nmodel: haiku\npermissionMode: dontAsk\n---\n\nbody\n",
            encoding="utf-8",
        )
        (claude_dir / "b.md").write_text(
            "---\nname: dup-agent\nmodel: haiku\npermissionMode: dontAsk\n---\n\nbody\n",
            encoding="utf-8",
        )
        failures = MOD.check_duplicate_agent_names(claude_dir, codex_dir)
        assert any("dup-agent" in f for f in failures)

    def test_check_duplicate_agent_names_no_false_positive_on_real_repo(self):
        """Regression guard: the real repository has zero duplicate agent
        names today."""
        claude_dir = REPO_ROOT / ".claude" / "agents"
        codex_dir = REPO_ROOT / ".codex" / "agents"
        failures = MOD.check_duplicate_agent_names(claude_dir, codex_dir)
        assert failures == []
