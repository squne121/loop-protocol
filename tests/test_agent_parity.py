"""Tests for the provider-neutral Claude agent runtime-contract checker
(scripts/check_claude_codex_agent_parity.py).

Issue #2161 (Phase 3, retire Codex CLI assets and documentation): native
Codex CLI has been retired from this repository, so the checker no longer
compares `.claude/agents/*.md` against the retired native Codex CLI
`agents/*.toml` counterpart.
This test suite was rewritten to exercise the shrunk, `.claude/agents/*.md`
-only checker; the historical Claude/Codex parity-comparison test suite
(schema drift, permission-boundary drift, nested-delegation drift,
effort_requirement warn-drift, V1/V2 native Codex CLI `config.toml` fixtures) has been
retired along with the Codex-side extraction functions it exercised.

Covers:
- extract_claude_facts(): nested-delegation 3-value logic, tools /
  disallowedTools capture, mutation_class resolution via
  resolve_mutation_class()
- resolve_mutation_class(): declared-vs-tool-allowlist contradiction
- extract_final_output_schema_from_claude() / extract_artifact_only_schemas_from_claude()
- find_line_number(): 0 for empty/None search, correct line for a hit
- asset_classification completeness / pairing / duplicate-name checks
- CLI integration: STATUS ok/fail, --strict as a no-op compatibility flag,
  and no unexpected failures against the real repository
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT_FOR_INTEGRATION = Path(__file__).resolve().parents[1]

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "check_claude_codex_agent_parity.py"


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

def _load_module():
    spec = importlib.util.spec_from_file_location("check_claude_codex_agent_parity", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["check_claude_codex_agent_parity"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _claude_md(
    name: str = "issue-reviewer",
    model: str = "haiku",
    permission_mode: str = "dontAsk",
    disallowed_tools: list[str] | None = None,
    tools: list[str] | None = None,
    output_schema: str = "ISSUE_REVIEW_RESULT_COMPACT_V1",
    artifact_only: str | None = None,
    extra_body: str = "",
    effort: str | None = None,
    route: str = "review-issue",
) -> str:
    if disallowed_tools is None:
        disallowed_tools = ["Agent", "Edit", "Write"]
    if tools is None:
        tools = ["Bash", "Read"]
    artifact_line = f"\nartifact only: `{artifact_only}`" if artifact_only else ""
    lines = [
        "---",
        f"name: {name}",
        "description: Test agent",
        f"model: {model}",
        "tools:",
    ]
    lines.extend(f"  - {t}" for t in tools)
    lines.append(f"permissionMode: {permission_mode}")
    if effort is not None:
        lines.append(f"effort: {effort}")
    lines.append("disallowedTools:")
    lines.extend(f"  - {t}" for t in disallowed_tools)
    lines.append("---")
    lines.append("")
    lines.append(f"## 出力契約（{output_schema}）")
    lines.append("")
    lines.append(f"Use `{output_schema}` as final output schema.{artifact_line}")
    lines.append("")
    lines.append("RUNTIME")
    lines.append(f"- runtime_followup_route: {route}")
    lines.append("")
    lines.append("Known limitation")
    lines.append("- hooks are local guardrails.")
    if extra_body:
        lines.append(extra_body)
    return "\n".join(lines) + "\n"


def _write_minimal_contract(
    tmp_path: Path,
    agent_name: str = "issue-reviewer",
    *,
    model_alias: str = "haiku",
    claude_permission_mode: str = "dontAsk",
    mutation_class: str = "readonly",
    runtime_followup_route: str = "review-issue",
) -> Path:
    fixture_dir = tmp_path / "tests" / "fixtures" / "agent-config"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    minimal_contract = {
        "required_agents": {
            agent_name: {
                "claude_agent_path": f".claude/agents/{agent_name}.md",
                "model_alias": model_alias,
                "claude_permission_mode": claude_permission_mode,
                "mutation_class": mutation_class,
                "runtime_followup_route": runtime_followup_route,
            }
        },
    }
    path = fixture_dir / "expected-runtime-contract.json"
    path.write_text(json.dumps(minimal_contract), encoding="utf-8")
    return path


def _run_cli(
    tmp_path: Path,
    claude_md: str,
    agent_name: str = "issue-reviewer",
    extra_args: list[str] | None = None,
    **contract_kwargs,
) -> subprocess.CompletedProcess[str]:
    """Write fixture files and run the checker via subprocess."""
    claude_dir = tmp_path / ".claude" / "agents"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / f"{agent_name}.md").write_text(claude_md, encoding="utf-8")

    expectation_file = _write_minimal_contract(tmp_path, agent_name, **contract_kwargs)

    cmd = [
        sys.executable,
        str(MODULE_PATH),
        "--claude-agent-dir", str(claude_dir),
        "--expectation-path", str(expectation_file),
        "--format", "json",
    ]
    if extra_args:
        cmd.extend(extra_args)

    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
        cwd=tmp_path,
    )


# ---------------------------------------------------------------------------
# extract_final_output_schema_from_claude / extract_artifact_only_schemas_from_claude
# ---------------------------------------------------------------------------

class TestSchemaExtraction:
    def test_final_schema_extracted_from_heading(self):
        text = _claude_md(output_schema="ISSUE_REVIEW_RESULT_COMPACT_V1")
        assert MOD.extract_final_output_schema_from_claude(text) == "ISSUE_REVIEW_RESULT_COMPACT_V1"

    def test_artifact_only_schema_extraction(self):
        text = _claude_md(
            output_schema="ISSUE_REVIEW_RESULT_COMPACT_V1",
            artifact_only="ISSUE_REVIEW_RESULT_V1",
        )
        artifact_schemas = MOD.extract_artifact_only_schemas_from_claude(
            text, "ISSUE_REVIEW_RESULT_COMPACT_V1"
        )
        assert "ISSUE_REVIEW_RESULT_V1" in artifact_schemas

    def test_heading_pattern_schema_first(self):
        """Extract artifact-only schema from '### 内部処理用 SCHEMA（artifact のみ）' heading."""
        text = (
            "---\nname: issue-reviewer\n---\n"
            "## 出力契約（ISSUE_REVIEW_RESULT_COMPACT_V1）\n\n"
            "### 内部処理用 REVIEW_ISSUE_RESULT_V1（artifact のみ）\n"
            "full schema stored in artifact.\n"
        )
        artifact_schemas = MOD.extract_artifact_only_schemas_from_claude(
            text, "ISSUE_REVIEW_RESULT_COMPACT_V1"
        )
        assert "REVIEW_ISSUE_RESULT_V1" in artifact_schemas

    def test_real_issue_reviewer_artifact_only_extracted(self):
        """Real issue-reviewer.md artifact-only schema is correctly extracted."""
        claude_path = REPO_ROOT_FOR_INTEGRATION / ".claude" / "agents" / "issue-reviewer.md"
        if not claude_path.exists():
            pytest.skip("Real .claude/agents/issue-reviewer.md not accessible")
        text = claude_path.read_text(encoding="utf-8")
        final = MOD.extract_final_output_schema_from_claude(text)
        artifact_only = MOD.extract_artifact_only_schemas_from_claude(text, final)
        assert "REVIEW_ISSUE_RESULT_V1" in artifact_only, (
            f"Real file should have REVIEW_ISSUE_RESULT_V1 as artifact-only, "
            f"but got: {artifact_only} (final={final})"
        )


# ---------------------------------------------------------------------------
# find_line_number
# ---------------------------------------------------------------------------

class TestFindLineNumber:
    def test_empty_search_returns_zero(self):
        text = "line one\nline two\n"
        assert MOD.find_line_number(text, "") == 0

    def test_none_search_returns_zero(self):
        text = "line one\nline two\n"
        assert MOD.find_line_number(text, None) == 0

    def test_normal_search_still_works(self):
        text = "alpha\nbeta\ngamma\n"
        assert MOD.find_line_number(text, "beta") == 2

    def test_not_found_returns_zero(self):
        text = "alpha\nbeta\n"
        assert MOD.find_line_number(text, "delta") == 0


# ---------------------------------------------------------------------------
# resolve_mutation_class(): declared ground truth vs tool-allowlist contradiction
# ---------------------------------------------------------------------------

class TestResolveMutationClass:
    def test_readonly_declared_with_no_mutation_tools_is_consistent(self):
        mutation_class, contradiction = MOD.resolve_mutation_class("readonly", ["Bash", "Read"], [])
        assert mutation_class == "readonly"
        assert contradiction is None

    def test_readonly_declared_with_edit_tool_is_contradiction(self):
        mutation_class, contradiction = MOD.resolve_mutation_class("readonly", ["Bash", "Edit"], [])
        assert mutation_class == "readonly"
        assert contradiction is not None
        assert "Edit" in contradiction

    def test_readonly_declared_with_edit_tool_denied_is_consistent(self):
        mutation_class, contradiction = MOD.resolve_mutation_class(
            "readonly", ["Bash", "Edit"], ["Edit"]
        )
        assert mutation_class == "readonly"
        assert contradiction is None

    def test_non_readonly_declared_class_short_circuits(self):
        mutation_class, contradiction = MOD.resolve_mutation_class(
            "repo-write", ["Bash", "Edit", "Write"], []
        )
        assert mutation_class == "repo-write"
        assert contradiction is None

    def test_declared_none_defaults_to_unknown(self):
        mutation_class, contradiction = MOD.resolve_mutation_class(None, ["Bash"], [])
        assert mutation_class == "unknown"
        assert contradiction is None


# ---------------------------------------------------------------------------
# extract_claude_facts(): nested delegation 3-value logic
# ---------------------------------------------------------------------------

class TestNestedDelegation3Value:
    def test_tools_key_without_agent_is_blocked(self, tmp_path: Path):
        claude_text = _claude_md(tools=["Bash", "Read"], disallowed_tools=[])
        claude_path = tmp_path / "issue-reviewer.md"
        claude_path.write_text(claude_text, encoding="utf-8")
        facts = MOD.extract_claude_facts("issue-reviewer", claude_path, claude_text)
        assert facts.nested_delegation_blocked is True
        assert "Agent" in facts.nested_delegation_evidence or "disallowedTools" in facts.nested_delegation_evidence

    def test_no_tools_key_is_unknown(self, tmp_path: Path):
        text = (
            "---\n"
            "name: issue-reviewer\n"
            "model: haiku\n"
            "permissionMode: dontAsk\n"
            "disallowedTools: []\n"
            "---\n\n"
            "## 出力契約（ISSUE_REVIEW_RESULT_COMPACT_V1）\n"
            "\nRUNTIME\n- runtime_followup_route: review-issue\n"
        )
        claude_path = tmp_path / "issue-reviewer.md"
        claude_path.write_text(text, encoding="utf-8")
        facts = MOD.extract_claude_facts("issue-reviewer", claude_path, text)
        assert facts.nested_delegation_blocked is None

    def test_tools_key_with_agent_is_allowed(self, tmp_path: Path):
        claude_text = _claude_md(tools=["Bash", "Read", "Agent"], disallowed_tools=[])
        claude_path = tmp_path / "issue-reviewer.md"
        claude_path.write_text(claude_text, encoding="utf-8")
        facts = MOD.extract_claude_facts("issue-reviewer", claude_path, claude_text)
        assert facts.nested_delegation_blocked is False

    def test_disallowed_takes_priority_over_tools(self, tmp_path: Path):
        claude_text = _claude_md(tools=["Bash", "Read", "Agent"], disallowed_tools=["Agent"])
        claude_path = tmp_path / "issue-reviewer.md"
        claude_path.write_text(claude_text, encoding="utf-8")
        facts = MOD.extract_claude_facts("issue-reviewer", claude_path, claude_text)
        assert facts.nested_delegation_blocked is True


# ---------------------------------------------------------------------------
# extract_claude_facts(): tools / disallowedTools capture
# ---------------------------------------------------------------------------

class TestClaudeFactsToolCapture:
    def test_tools_and_disallowed_captured(self, tmp_path: Path):
        claude_text = _claude_md(
            permission_mode="dontAsk",
            tools=["Bash", "Read"],
            disallowed_tools=["Agent", "Edit"],
        )
        claude_path = tmp_path / "issue-reviewer.md"
        claude_path.write_text(claude_text, encoding="utf-8")
        facts = MOD.extract_claude_facts("issue-reviewer", claude_path, claude_text)
        assert "Bash" in facts.claude_tools
        assert "Agent" in facts.claude_disallowed_tools

    def test_tools_omitted_when_empty(self, tmp_path: Path):
        text = (
            "---\n"
            "name: issue-reviewer\n"
            "model: haiku\n"
            "permissionMode: dontAsk\n"
            "---\n\n"
            "## 出力契約（ISSUE_REVIEW_RESULT_COMPACT_V1）\n"
            "\nRUNTIME\n- runtime_followup_route: review-issue\n"
        )
        tmp_claude_path = tmp_path / "issue-reviewer.md"
        tmp_claude_path.write_text(text, encoding="utf-8")
        facts = MOD.extract_claude_facts("issue-reviewer", tmp_claude_path, text)
        assert facts.claude_tools == []
        assert facts.claude_disallowed_tools == []


# ---------------------------------------------------------------------------
# CLI integration: STATUS ok/fail
# ---------------------------------------------------------------------------

class TestStatusOutput:
    def test_matching_agent_status_ok(self, tmp_path: Path):
        result = _run_cli(tmp_path, _claude_md())
        data = json.loads(result.stdout)
        assert data["STATUS"] == "ok", data
        assert data["failures"] == []
        assert result.returncode == 0

    def test_model_alias_mismatch_produces_fail(self, tmp_path: Path):
        result = _run_cli(tmp_path, _claude_md(model="sonnet"))
        data = json.loads(result.stdout)
        assert data["STATUS"] == "fail"
        assert any("model_alias" in f for f in data["failures"])
        assert result.returncode == 1

    def test_permission_mode_mismatch_produces_fail(self, tmp_path: Path):
        result = _run_cli(tmp_path, _claude_md(permission_mode="acceptEdits"))
        data = json.loads(result.stdout)
        assert data["STATUS"] == "fail"
        assert any("permissionMode" in f for f in data["failures"])

    def test_missing_tools_produces_fail(self, tmp_path: Path):
        result = _run_cli(tmp_path, _claude_md(tools=[]))
        data = json.loads(result.stdout)
        assert data["STATUS"] == "fail"
        assert any("tools" in f for f in data["failures"])

    def test_route_token_missing_produces_fail(self, tmp_path: Path):
        text = _claude_md(route="some-other-route")
        result = _run_cli(tmp_path, text, runtime_followup_route="review-issue")
        data = json.loads(result.stdout)
        assert data["STATUS"] == "fail"
        assert any("route" in f for f in data["failures"])

    def test_mutation_class_contradiction_produces_fail(self, tmp_path: Path):
        result = _run_cli(
            tmp_path,
            _claude_md(tools=["Bash", "Edit"], disallowed_tools=[]),
            mutation_class="readonly",
        )
        data = json.loads(result.stdout)
        assert data["STATUS"] == "fail"
        assert any("contradicted" in f for f in data["failures"])

    def test_missing_claude_agent_file_produces_fail(self, tmp_path: Path):
        # Write the contract but never write the .claude/agents/*.md file.
        claude_dir = tmp_path / ".claude" / "agents"
        claude_dir.mkdir(parents=True, exist_ok=True)
        expectation_file = _write_minimal_contract(tmp_path)
        cmd = [
            sys.executable,
            str(MODULE_PATH),
            "--claude-agent-dir", str(claude_dir),
            "--expectation-path", str(expectation_file),
            "--format", "json",
        ]
        result = subprocess.run(cmd, text=True, capture_output=True, check=False, cwd=tmp_path)
        data = json.loads(result.stdout)
        assert data["STATUS"] == "fail"
        assert any("missing claude agent file" in f for f in data["failures"])


# ---------------------------------------------------------------------------
# --strict is a no-op compatibility flag (existing callers, e.g.
# scripts/check_post_merge_cleanup_boundary.py, still pass it)
# ---------------------------------------------------------------------------

class TestStrictFlagCompat:
    def test_strict_flag_accepted_and_does_not_change_ok_status(self, tmp_path: Path):
        result = _run_cli(tmp_path, _claude_md(), extra_args=["--strict"])
        data = json.loads(result.stdout)
        assert data["STATUS"] == "ok"
        assert result.returncode == 0

    def test_strict_flag_does_not_change_fail_exit_code(self, tmp_path: Path):
        result = _run_cli(tmp_path, _claude_md(model="sonnet"), extra_args=["--strict"])
        assert result.returncode == 1


# ---------------------------------------------------------------------------
# asset_classification: completeness / pairing / duplicate names
# ---------------------------------------------------------------------------

class TestAssetClassification:
    def test_unclassified_agent_produces_failure(self):
        classification = {".claude/agents/issue-reviewer.md": "shared_claude_runtime"}
        failures = MOD.check_asset_classification_complete(
            classification, REPO_ROOT / ".claude/agents"
        )
        # issue-reviewer.md is classified; every other real .claude/agents/*.md
        # is not present in this partial classification dict, so at least one
        # unclassified failure is expected.
        assert any("unclassified" in f for f in failures)

    def test_unknown_classification_value_produces_failure(self, tmp_path: Path):
        agent_dir = tmp_path / ".claude" / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "x.md").write_text("---\nname: x\n---\nbody\n", encoding="utf-8")
        classification = {".claude/agents/x.md": "legacy_codex_projection"}
        failures = MOD.check_asset_classification_complete(classification, agent_dir)
        assert any("unknown classification" in f for f in failures)

    def test_pairing_requires_matching_required_agents_entry(self):
        classification = {".claude/agents/does-not-exist-in-required-agents.md": "shared_claude_runtime"}
        failures = MOD.check_asset_classification_pairing(classification, {})
        assert any("no matching required_agents entry" in f for f in failures)

    def test_duplicate_agent_names_detected(self, tmp_path: Path):
        agent_dir = tmp_path / ".claude" / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "a.md").write_text("---\nname: dup\n---\nbody\n", encoding="utf-8")
        (agent_dir / "b.md").write_text("---\nname: dup\n---\nbody\n", encoding="utf-8")
        failures = MOD.check_duplicate_agent_names(agent_dir)
        assert any("duplicate Claude agent name" in f for f in failures)

    def test_nested_subdirectory_agent_is_discovered(self, tmp_path: Path):
        # Claude Code recursively scans `.claude/agents/**` (subdirectories
        # included, see https://code.claude.com/docs/en/sub-agents), so a
        # nested `.claude/agents/<subdir>/foo.md` must be discovered by both
        # asset-classification completeness and duplicate-name detection,
        # not silently skipped by a non-recursive `glob("*.md")`.
        agent_dir = tmp_path / ".claude" / "agents"
        subdir = agent_dir / "subdir"
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / "foo.md").write_text("---\nname: nested-foo\n---\nbody\n", encoding="utf-8")

        failures = MOD.check_asset_classification_complete({}, agent_dir)
        assert any(
            f == "asset_classification: .claude/agents/subdir/foo.md is unclassified"
            for f in failures
        )

        # A same-named duplicate in a different subdirectory must still be
        # caught (duplicate-name uniqueness is repo-subtree-wide, not
        # per-directory).
        other_subdir = agent_dir / "other"
        other_subdir.mkdir(parents=True, exist_ok=True)
        (other_subdir / "foo.md").write_text("---\nname: nested-foo\n---\nbody\n", encoding="utf-8")
        dup_failures = MOD.check_duplicate_agent_names(agent_dir)
        assert any("duplicate Claude agent name 'nested-foo'" in f for f in dup_failures)


# ---------------------------------------------------------------------------
# Real repo integration (parity with the historical B4 test)
# ---------------------------------------------------------------------------

class TestRealRepoIntegration:
    def test_real_repo_produces_no_unexpected_failures(self):
        """The checker run against the real repository produces STATUS: ok
        with no hard failures (missing files, name mismatches, etc.)."""
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT_FOR_INTEGRATION),
        )
        assert result.stdout, f"parity script produced no stdout. stderr={result.stderr!r}"
        data = json.loads(result.stdout)
        assert data["failures"] == [], (
            f"Unexpected hard failures in real repo parity check: {data['failures']}"
        )
        assert data["STATUS"] == "ok", data
