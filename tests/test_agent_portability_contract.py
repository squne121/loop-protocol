"""Tests for the agent asset portability / inventory contract (Issue #2160).

Covers:
- AC1 (sub-requirement): permission-affecting frontmatter fields
  (tools:/disallowedTools:) must not silently drift from the checked-in
  baseline captured in tests/fixtures/agent-config/
  agent_permission_baseline.json.
- AC2: every .claude/agents/*.md asset is uniquely classified
  (shared_claude_runtime / claude_only / experimental), zero unclassified
  (Issue #2161: native Codex CLI retired, the retired native Codex CLI
  `agents/*.toml` classification removed along with it).
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
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "agent-config"


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
    def test_all_agents_tools_and_disallowed_tools_match_checked_in_baseline(self):
        """AC1: normalized tools:/disallowedTools: for every .claude/agents/*.md
        must equal the checked-in baseline. CI-safe fallback guard (does
        not require git history): the checked-in baseline was itself
        generated from the pre-migration commit recorded in its
        `_provenance` block. (The #2334-era one-off
        "pre_migration_git_history" git-history diff-proof was removed per
        OWNER PR #2365 review 2026-08-28: it permanently forbade any future
        intentional tools:/disallowedTools: change, which is harness scope
        leakage beyond Issue #2160's one-time migration proof. This
        checked-in baseline is now the sole AC1 guard; an intentional
        permission change updates this baseline in the same PR, and the
        diff itself is the human-reviewable record of the change.)"""
        raw = json.loads(
            (FIXTURE_DIR / "agent_permission_baseline.json").read_text(encoding="utf-8")
        )
        assert "_provenance" in raw and raw["_provenance"].get("merge_base_commit"), (
            "agent_permission_baseline.json must record the pre-migration "
            "merge_base_commit it was generated from"
        )
        baseline = {k: v for k, v in raw.items() if not k.startswith("_")}
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
        """AC2: every .claude/agents/*.md file has a unique classification
        in ALLOWED_CLASSIFICATIONS; zero unclassified."""
        expectations = json.loads(
            (FIXTURE_DIR / "expected-runtime-contract.json").read_text(encoding="utf-8")
        )
        classification = expectations["asset_classification"]
        claude_dir = REPO_ROOT / ".claude" / "agents"
        failures = MOD.check_asset_classification_complete(classification, claude_dir)
        assert failures == [], f"unclassified or invalid assets found: {failures}"

        # Positive assertion: the enum values actually used are within the
        # allowed set (defends against a future typo in the fixture itself).
        used_values = set(classification.values())
        assert used_values <= MOD.ALLOWED_CLASSIFICATIONS, (
            f"asset_classification uses values outside ALLOWED_CLASSIFICATIONS: "
            f"{used_values - MOD.ALLOWED_CLASSIFICATIONS}"
        )

    def test_asset_classification_covers_every_discovered_file(self):
        """AC2: no discovered .md agent file is missing from the
        classification map (defends against the fixture going stale when a
        new agent file is added without updating the inventory)."""
        expectations = json.loads(
            (FIXTURE_DIR / "expected-runtime-contract.json").read_text(encoding="utf-8")
        )
        classification = expectations["asset_classification"]
        claude_dir = REPO_ROOT / ".claude" / "agents"
        discovered = {f".claude/agents/{p.name}" for p in claude_dir.glob("*.md")}
        missing = discovered - classification.keys()
        assert missing == set(), f"discovered assets missing from asset_classification: {missing}"

    def test_real_repo_classification_pairing_is_consistent(self):
        """AC2/AC9 (human PR reviewer P1 blocker): the real repository's
        asset_classification inventory must satisfy every pairing
        invariant with zero failures."""
        expectations = json.loads(
            (FIXTURE_DIR / "expected-runtime-contract.json").read_text(encoding="utf-8")
        )
        failures = MOD.check_asset_classification_pairing(
            expectations["asset_classification"], expectations["required_agents"]
        )
        assert failures == [], f"asset_classification pairing failures: {failures}"

    def test_pairing_detects_shared_claude_runtime_without_required_agents_entry(self):
        """AC2/AC9 negative case (Issue #2161: native Codex CLI retired, the
        retired native Codex CLI `agents/*.toml` pairing check was removed along with it): a
        shared_claude_runtime entry with no matching required_agents entry
        is detected."""
        classification = {".claude/agents/orphan.md": "shared_claude_runtime"}
        failures = MOD.check_asset_classification_pairing(classification, {})
        assert any("orphan" in f and "no matching required_agents entry" in f for f in failures)

    def test_pairing_detects_classified_path_that_does_not_exist(self):
        """AC2/AC9 negative case: a classification entry pointing at a
        nonexistent filesystem path is a stale-entry failure."""
        classification = {
            ".claude/agents/__definitely_does_not_exist__.md": "claude_only",
        }
        failures = MOD.check_asset_classification_pairing(classification, {})
        assert any("does not exist on disk" in f for f in failures)

    def test_pairing_detects_required_agents_entry_without_classification(self):
        """AC2/AC9 negative case: a required_agents entry with a
        claude_agent_path but no matching shared_claude_runtime
        classification entry is detected."""
        required_agents = {
            "unclassified-agent": {"claude_agent_path": ".claude/agents/unclassified-agent.md"}
        }
        failures = MOD.check_asset_classification_pairing({}, required_agents)
        assert any("unclassified-agent" in f for f in failures)


# ---------------------------------------------------------------------------
# AC4: mutation_class is an explicit declared ground truth, never derived
# from permissionMode (alone or in combination with a tool-list heuristic).
# Re-scoped 2026-08-25 per human PR reviewer P0 blocker (PR #2334 comment
# 5401806450): `resolve_mutation_class()` takes the declared ground-truth
# value directly and only ever flags an explicit contradiction against the
# tool allowlist when the declared value is "readonly". permissionMode is
# never an input.
# ---------------------------------------------------------------------------

class TestMutationClassIsDeclaredGroundTruth:
    def test_mutation_class_is_declared_ground_truth_not_derived_from_permission_mode(self):
        """AC4: two agents sharing the same permissionMode ("dontAsk") and
        the same tool set (["Bash"]) can still resolve to different
        mutation_class values purely because their declared ground truth
        differs -- proving mutation_class is not a function of
        (permission_mode, tools) at all, but of the explicit declaration."""
        readonly_case, readonly_reason = MOD.resolve_mutation_class("readonly", ["Bash"], [])
        repo_write_case, repo_write_reason = MOD.resolve_mutation_class("repo-write", ["Bash"], [])
        assert readonly_case == "readonly"
        assert readonly_reason is None
        assert repo_write_case == "repo-write"
        assert repo_write_reason is None
        assert readonly_case != repo_write_case, (
            "mutation_class must be able to differ for identical "
            "(permission_mode, tools) inputs, driven only by the declared "
            "ground truth"
        )

    def test_dont_ask_bare_bash_declared_repo_write_is_respected_canonical_counterexample(self):
        """AC4 canonical counterexample (Issue #2160 wording, literal):
        `dontAsk + Bash != read_only`. A `dontAsk` agent whose tools
        allowlist grants *only* `Bash` (no Edit/Write/MultiEdit) can still
        be declared `mutation_class: repo-write` in the ground-truth
        fixture (e.g. because Bash is used to run `git push` / `gh pr
        merge`, as `.claude/settings.json`'s Bash allowlist permits for
        several agents in this repository). The checker must respect that
        declared value and must never force it back to "readonly" just
        because permissionMode is dontAsk and no Edit-family tool is
        present."""
        claude_text = (
            "---\n"
            "name: bash-mutator\n"
            "model: haiku\n"
            "tools:\n"
            "  - Bash\n"
            "permissionMode: dontAsk\n"
            "---\n\n"
            "body\n"
        )
        claude_path = REPO_ROOT / "tests" / "fixtures" / "agent-parity" / "__bash_mutator_probe__.md"
        facts = MOD.extract_claude_facts(
            "bash-mutator", claude_path, claude_text, declared_mutation_class="repo-write"
        )
        assert facts.mutation_boundary == "repo-write"
        assert facts.mutation_class_contradiction is None
        assert facts.declared_permission == "claude.permissionMode=dontAsk"

    def test_declared_readonly_with_edit_granted_and_not_denied_is_contradiction(self):
        """AC4 negative case 1: a declared `mutation_class: readonly` agent
        whose tools allowlist grants Edit (and does not deny it) is an
        explicit contradiction between the declared contract and the tool
        grant -- this is the only automated cross-check performed."""
        value, reason = MOD.resolve_mutation_class("readonly", ["Bash", "Edit"], [])
        assert value == "readonly"
        assert reason is not None
        assert "Edit" in reason

    def test_declared_readonly_with_write_granted_and_not_denied_is_contradiction(self):
        """AC4 negative case 2: declared readonly + Write granted (and not
        explicitly denied) is a contradiction."""
        value, reason = MOD.resolve_mutation_class("readonly", ["Bash", "Write"], [])
        assert value == "readonly"
        assert reason is not None
        assert "Write" in reason

    def test_declared_readonly_with_multiedit_granted_and_not_denied_is_contradiction(self):
        """AC4 negative case 3: declared readonly + MultiEdit granted (and
        not explicitly denied) is a contradiction."""
        value, reason = MOD.resolve_mutation_class("readonly", ["Bash", "Read", "MultiEdit"], [])
        assert value == "readonly"
        assert reason is not None
        assert "MultiEdit" in reason

    def test_declared_readonly_with_edit_but_explicitly_denied_has_no_contradiction(self):
        """Contrast case: declared readonly + Edit granted BUT Edit is also
        in disallowedTools (disallowedTools wins) is not a contradiction,
        matching every real .claude/agents/*.md file in this repository
        (Edit/Write/MultiEdit are explicitly denied even when not present
        in `tools:`)."""
        value, reason = MOD.resolve_mutation_class("readonly", ["Bash", "Edit"], ["Edit"])
        assert value == "readonly"
        assert reason is None

    def test_bash_alone_never_evidences_readonly_or_repo_write(self):
        """AC4: Bash presence alone must never be treated as evidence of
        either "readonly" or "repo-write" -- only the declared ground truth
        determines the value, for any tool set containing only Bash."""
        readonly_value, readonly_reason = MOD.resolve_mutation_class("readonly", ["Bash"], [])
        repo_write_value, repo_write_reason = MOD.resolve_mutation_class("repo-write", ["Bash"], [])
        issue_mutation_value, issue_mutation_reason = MOD.resolve_mutation_class(
            "issue-mutation", ["Bash"], []
        )
        assert (readonly_value, readonly_reason) == ("readonly", None)
        assert (repo_write_value, repo_write_reason) == ("repo-write", None)
        assert (issue_mutation_value, issue_mutation_reason) == ("issue-mutation", None)


# ---------------------------------------------------------------------------
# AC7: frontmatter parser fails loudly / tool alias normalization / dup names
# ---------------------------------------------------------------------------

class TestFrontmatterParserFailsLoudly:
    def test_frontmatter_parser_accepts_nested_mapping_and_inline_object(self):
        """AC7 (human PR reviewer P1 blocker, PR #2334 comment 5401806450):
        real YAML (yaml.safe_load) legitimately supports nested mappings
        and non-empty inline objects -- Claude Code's official subagent
        frontmatter uses structured fields like this (mcpServers/hooks).
        These must now parse successfully, not raise, unlike the old
        hand-rolled subset parser."""
        text = (
            "---\n"
            "name: ok-agent\n"
            "model: haiku\n"
            "nested:\n"
            "  inner: value\n"
            "config: {a: b}\n"
            "permissionMode: dontAsk\n"
            "---\n\n"
            "body\n"
        )
        fm = MOD.extract_frontmatter(text, source_path="ok-agent.md")
        assert fm["nested"] == {"inner": "value"}
        assert fm["config"] == {"a": "b"}

    def test_frontmatter_parser_rejects_genuinely_invalid_yaml(self):
        """AC7: genuinely malformed YAML (unterminated flow sequence) still
        raises FrontmatterParseError, not silently skipped/misparsed."""
        text = (
            "---\n"
            "name: broken-agent\n"
            "model: haiku\n"
            "tools: [Bash, Read\n"
            "permissionMode: dontAsk\n"
            "---\n\n"
            "body\n"
        )
        with pytest.raises(MOD.FrontmatterParseError):
            MOD.extract_frontmatter(text, source_path="broken-agent.md")

    def test_frontmatter_parser_rejects_non_mapping_frontmatter(self):
        """AC7: a frontmatter block that parses to a YAML list (or other
        non-mapping) rather than a mapping is a fail-loud contract
        violation, not a silent empty-dict result."""
        text = (
            "---\n"
            "- just\n"
            "- a\n"
            "- list\n"
            "---\n\n"
            "body\n"
        )
        with pytest.raises(MOD.FrontmatterParseError):
            MOD.extract_frontmatter(text, source_path="broken-agent.md")

    def test_frontmatter_parser_rejects_non_list_string_tools_value(self):
        """AC7: a `tools:`/`disallowedTools:` value that is neither a YAML
        list nor a comma-separated string (e.g. a bare mapping) is a
        fail-loud contract violation."""
        text = (
            "---\n"
            "name: broken-agent\n"
            "model: haiku\n"
            "tools: {a: b}\n"
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

    def test_frontmatter_parser_accepts_comma_separated_shorthand_tools(self):
        """AC7: Claude Code's official subagent frontmatter shorthand form
        `tools: Read, Grep, Glob, Bash` (comma-separated string, not a YAML
        list) is normalized into a list[str], not rejected."""
        text = (
            "---\n"
            "name: shorthand-agent\n"
            "model: haiku\n"
            "tools: Read, Grep, Glob, Bash\n"
            "permissionMode: dontAsk\n"
            "---\n\n"
            "body\n"
        )
        fm = MOD.extract_frontmatter(text, source_path="shorthand-agent.md")
        assert fm["tools"] == ["Read", "Grep", "Glob", "Bash"]

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
        (claude_dir / "a.md").write_text(
            "---\nname: dup-agent\nmodel: haiku\npermissionMode: dontAsk\n---\n\nbody\n",
            encoding="utf-8",
        )
        (claude_dir / "b.md").write_text(
            "---\nname: dup-agent\nmodel: haiku\npermissionMode: dontAsk\n---\n\nbody\n",
            encoding="utf-8",
        )
        failures = MOD.check_duplicate_agent_names(claude_dir)
        assert any("dup-agent" in f for f in failures)

    def test_check_duplicate_agent_names_no_false_positive_on_real_repo(self):
        """Regression guard: the real repository has zero duplicate agent
        names today."""
        claude_dir = REPO_ROOT / ".claude" / "agents"
        failures = MOD.check_duplicate_agent_names(claude_dir)
        assert failures == []
