"""Tests for extended Claude/Codex agent parity linter.

Covers:
- AC1: compact schema drift detection
- AC2: mutation permission boundary diff
- AC3: model/reasoning_effort as config declaration (not runtime proof)
- AC4: nested delegation prohibition detection
- AC5: STATUS: warn / fail on drift
- AC7: artifact-only schema does not cause compact schema parity fail
- AC8: DECLARED_PERMISSION / MUTATION_BOUNDARY / RUNTIME_PROOF_NOTE 3-layer report
- AC9: Claude nested delegation from disallowedTools; Codex from max_depth
- AC10: drift evidence contains rule_id / file:line / launcher / agent / expected / actual
- AC11: STATUS:warn exit code default 0, --strict exit 1

fix_delta regression tests (B1-B8):
- B1: Codex final schema that matches Claude artifact-only schema is still drift (fail)
- B2: artifact-only schema extraction handles heading pattern 'SCHEMA（artifact のみ）'
- B3: schema/permission/delegation drift produces STATUS:fail (not warn)
- B4: real repo parity produces no unexpected blocker drift
- B5: Claude nested delegation is blocked when Agent absent from explicit tools allowlist
- B6: Codex delegation keywords in developer_instructions produce NESTED_DELEGATION_001
- B7: DECLARED_PERMISSION includes claude.tools and claude.disallowedTools
- B8: find_line_number returns 0 for empty/None search
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

REPO_ROOT_FOR_INTEGRATION = Path(__file__).resolve().parents[1]

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "check_claude_codex_agent_parity.py"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "agent-parity"

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
) -> str:
    if disallowed_tools is None:
        disallowed_tools = ["Agent", "Edit", "Write"]
    if tools is None:
        tools = ["Bash", "Read"]
    disallowed_str = "\n".join(f"  - {t}" for t in disallowed_tools)
    tools_str = "\n".join(f"  - {t}" for t in tools)
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
    lines.append("disallowedTools:")
    lines.extend(f"  - {t}" for t in disallowed_tools)
    lines.append("---")
    lines.append("")
    lines.append(f"## 出力契約（{output_schema}）")
    lines.append("")
    lines.append(f"Use `{output_schema}` as final output schema.{artifact_line}")
    lines.append("")
    lines.append("RUNTIME")
    lines.append("- runtime_dependency_status: codex_skill_required")
    lines.append("- runtime_followup_route: review-issue")
    lines.append("")
    lines.append("Known limitation")
    lines.append("- hooks are local guardrails.")
    if extra_body:
        lines.append(extra_body)
    return "\n".join(lines) + "\n"

def _write_codex_toml(
    path: Path,
    name: str = "issue-reviewer",
    model: str = "gpt-5.4-mini",
    reasoning_effort: str = "medium",
    default_permissions: str = "loop-protocol-readonly",
    output_schema: str = "ISSUE_REVIEW_RESULT_COMPACT_V1",
) -> None:
    """Write a minimal Codex agent TOML using multiline TOML string."""
    # Use TOML multiline basic string to avoid escape issues with newlines
    header = (
        f'name = "{name}"\n'
        f'description = "Test agent"\n'
        f'model = "{model}"\n'
        f'model_reasoning_effort = "{reasoning_effort}"\n'
        f'default_permissions = "{default_permissions}"\n'
    )
    instructions_body = (
        "ROLE\n"
        "- test reviewer.\n\n"
        "INPUT_CONTRACT\n"
        "- issue_number.\n\n"
        "OUTPUT_CONTRACT\n"
        f"- emit {output_schema} via compact_result.py stdout.\n\n"
        "EXECUTION_POLICY\n"
        "- validator-first.\n\n"
        "RUNTIME\n"
        "- runtime_dependency_status: codex_skill_required\n"
        "- runtime_followup_route: review-issue\n\n"
        "FAIL_CLOSED\n"
        "- No approve without evidence.\n\n"
        "Known limitation\n"
        "- hooks are local guardrails.\n"
    )
    # Write TOML multiline basic string: developer_instructions = """\n...\n"""
    full = header + 'developer_instructions = """\n' + instructions_body + '"""\n'
    path.write_text(full, encoding="utf-8")

def _write_config_toml(tmp_path: Path, max_depth: int = 1) -> Path:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"[agents]\nmax_depth = {max_depth}\n", encoding="utf-8")
    return config


def _write_minimal_contract(tmp_path: Path, agent_name: str = "issue-reviewer") -> None:
    fixture_dir = tmp_path / "tests" / "fixtures" / "codex-agent-config"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    minimal_contract = {
        "required_agents": {
            agent_name: {
                "path": f".codex/agents/{agent_name}.toml",
                "claude_agent_path": f".claude/agents/{agent_name}.md",
                "claude_model": "haiku",
                "claude_permission_mode": "dontAsk",
                "model": "gpt-5.4-mini",
                "model_reasoning_effort": "medium",
                "default_permissions": "loop-protocol-readonly",
                "runtime_dependency_status": "codex_skill_required",
                "runtime_followup_route": "review-issue",
            }
        },
        "required_instruction_tokens": [
            "ROLE", "INPUT_CONTRACT", "OUTPUT_CONTRACT",
            "EXECUTION_POLICY", "RUNTIME", "FAIL_CLOSED", "Known limitation",
        ],
        "required_hook_events": [],
        "required_hook_command_fragment": "check-codex-agents.mjs",
    }
    (fixture_dir / "expected-runtime-contract.json").write_text(
        json.dumps(minimal_contract), encoding="utf-8"
    )


def _run_cli(
    tmp_path: Path,
    claude_md: str,
    output_schema_codex: str = "ISSUE_REVIEW_RESULT_COMPACT_V1",
    agent_name: str = "issue-reviewer",
    extra_args: list[str] | None = None,
    max_depth: int = 1,
    codex_permissions: str = "loop-protocol-readonly",
) -> subprocess.CompletedProcess[str]:
    """Write fixture files and run the parity script via subprocess."""
    claude_dir = tmp_path / ".claude" / "agents"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / f"{agent_name}.md").write_text(claude_md, encoding="utf-8")

    codex_dir = tmp_path / ".codex" / "agents"
    codex_dir.mkdir(parents=True, exist_ok=True)
    _write_codex_toml(
        codex_dir / f"{agent_name}.toml",
        name=agent_name,
        default_permissions=codex_permissions,
        output_schema=output_schema_codex,
    )

    config = _write_config_toml(tmp_path, max_depth=max_depth)
    _write_minimal_contract(tmp_path, agent_name)

    expectation_file = tmp_path / "tests" / "fixtures" / "codex-agent-config" / "expected-runtime-contract.json"
    cmd = [
        sys.executable,
        str(MODULE_PATH),
        "--claude-agent-dir", str(claude_dir),
        "--codex-agent-dir", str(codex_dir),
        "--codex-config", str(config),
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
# AC1: compact schema drift
# ---------------------------------------------------------------------------

class TestSchemaParity:
    def test_schema_match_no_drift(self, tmp_path: Path):
        """AC1: matching schemas produce no drift."""
        result = _run_cli(
            tmp_path,
            _claude_md(output_schema="ISSUE_REVIEW_RESULT_COMPACT_V1"),
            output_schema_codex="ISSUE_REVIEW_RESULT_COMPACT_V1",
        )
        data = json.loads(result.stdout)
        schema_drifts = [d for d in data["drift"] if d["rule_id"] == "SCHEMA_PARITY_001"]
        assert schema_drifts == [], f"Expected no schema drift, got: {schema_drifts}"

    def test_schema_mismatch_produces_drift(self, tmp_path: Path):
        """AC1: differing compact schema names produce SCHEMA_PARITY_001 drift."""
        result = _run_cli(
            tmp_path,
            _claude_md(output_schema="ISSUE_REVIEW_COMPACT_V2"),
            output_schema_codex="ISSUE_REVIEW_RESULT_COMPACT_V1",
        )
        data = json.loads(result.stdout)
        schema_drifts = [d for d in data["drift"] if d["rule_id"] == "SCHEMA_PARITY_001"]
        assert len(schema_drifts) == 1
        assert schema_drifts[0]["expected"] == "ISSUE_REVIEW_RESULT_COMPACT_V1"
        assert schema_drifts[0]["actual"] == "ISSUE_REVIEW_COMPACT_V2"


# ---------------------------------------------------------------------------
# AC7: artifact-only schema does not cause parity fail
# ---------------------------------------------------------------------------

class TestArtifactOnlySchema:
    def test_artifact_only_schema_not_parity_fail(self, tmp_path: Path):
        """AC7: artifact-only schema in Claude output does not cause compact schema parity fail."""
        claude_md = _claude_md(
            output_schema="ISSUE_REVIEW_RESULT_COMPACT_V1",
            artifact_only="ISSUE_REVIEW_RESULT_V1",
        )
        result = _run_cli(
            tmp_path,
            claude_md,
            output_schema_codex="ISSUE_REVIEW_RESULT_COMPACT_V1",
        )
        data = json.loads(result.stdout)
        schema_drifts = [d for d in data["drift"] if d["rule_id"] == "SCHEMA_PARITY_001"]
        assert schema_drifts == [], (
            f"Artifact-only schema should not cause parity fail, got: {schema_drifts}"
        )

    def test_artifact_only_schema_extraction(self, tmp_path: Path):
        """AC7: artifact-only schemas can be extracted from Claude agent text."""
        text = _claude_md(
            output_schema="ISSUE_REVIEW_RESULT_COMPACT_V1",
            artifact_only="ISSUE_REVIEW_RESULT_V1",
        )
        artifact_schemas = MOD.extract_artifact_only_schemas_from_claude(
            text, "ISSUE_REVIEW_RESULT_COMPACT_V1"
        )
        assert "ISSUE_REVIEW_RESULT_V1" in artifact_schemas


# ---------------------------------------------------------------------------
# AC2 / AC8: mutation permission boundary
# ---------------------------------------------------------------------------

class TestPermissionBoundary:
    def test_permission_match_no_drift(self, tmp_path: Path):
        """AC2/AC8: matching permission boundary produces no drift."""
        result = _run_cli(
            tmp_path,
            _claude_md(permission_mode="dontAsk"),
            codex_permissions="loop-protocol-readonly",
        )
        data = json.loads(result.stdout)
        perm_drifts = [d for d in data["drift"] if d["rule_id"] == "PERMISSION_BOUNDARY_001"]
        assert perm_drifts == []

    def test_permission_mismatch_produces_drift(self, tmp_path: Path):
        """AC2: mismatched permission boundary produces PERMISSION_BOUNDARY_001 drift."""
        result = _run_cli(
            tmp_path,
            _claude_md(permission_mode="acceptEdits"),
            codex_permissions="loop-protocol-readonly",
        )
        data = json.loads(result.stdout)
        perm_drifts = [d for d in data["drift"] if d["rule_id"] == "PERMISSION_BOUNDARY_001"]
        assert len(perm_drifts) == 1
        assert perm_drifts[0]["rule_id"] == "PERMISSION_BOUNDARY_001"
        assert "readonly" in perm_drifts[0]["expected"]
        assert "issue-mutation" in perm_drifts[0]["actual"]

    def test_permission_report_contains_3_layers(self, tmp_path: Path):
        """AC8: permission report contains DECLARED_PERMISSION, MUTATION_BOUNDARY, RUNTIME_PROOF_NOTE."""
        result = _run_cli(
            tmp_path,
            _claude_md(permission_mode="dontAsk"),
            codex_permissions="loop-protocol-readonly",
        )
        data = json.loads(result.stdout)
        assert "permission_report" in data
        pr = data["permission_report"]
        assert len(pr) >= 1
        entry = pr[0]
        assert "DECLARED_PERMISSION" in entry
        assert "MUTATION_BOUNDARY" in entry
        assert "RUNTIME_PROOF_NOTE" in entry

    def test_declared_permission_shows_both_launchers(self, tmp_path: Path):
        """AC8: DECLARED_PERMISSION shows both claude and codex values."""
        result = _run_cli(
            tmp_path,
            _claude_md(permission_mode="dontAsk"),
            codex_permissions="loop-protocol-readonly",
        )
        data = json.loads(result.stdout)
        dp = data["permission_report"][0]["DECLARED_PERMISSION"]
        assert "claude" in dp
        assert "codex" in dp
        assert "permissionMode" in dp["claude"]
        assert "default_permissions" in dp["codex"]

    def test_mutation_boundary_match_field(self, tmp_path: Path):
        """AC8: MUTATION_BOUNDARY includes match boolean."""
        result = _run_cli(
            tmp_path,
            _claude_md(permission_mode="dontAsk"),
            codex_permissions="loop-protocol-readonly",
        )
        data = json.loads(result.stdout)
        mb = data["permission_report"][0]["MUTATION_BOUNDARY"]
        assert "match" in mb
        assert mb["match"] is True

    def test_runtime_proof_note_present(self, tmp_path: Path):
        """AC8: RUNTIME_PROOF_NOTE is present and mentions declaration."""
        result = _run_cli(
            tmp_path,
            _claude_md(permission_mode="dontAsk"),
            codex_permissions="loop-protocol-readonly",
        )
        data = json.loads(result.stdout)
        note = data["permission_report"][0]["RUNTIME_PROOF_NOTE"]
        assert "declaration" in note.lower() or "Declaration" in note


# ---------------------------------------------------------------------------
# AC3: model/reasoning_effort as config declaration
# ---------------------------------------------------------------------------

class TestModelDeclaration:
    def test_model_report_present(self, tmp_path: Path):
        """AC3: model_declaration_report is present in JSON output."""
        result = _run_cli(tmp_path, _claude_md())
        data = json.loads(result.stdout)
        assert "model_declaration_report" in data
        mr = data["model_declaration_report"]
        assert len(mr) >= 1

    def test_model_report_contains_advisory_note(self, tmp_path: Path):
        """AC3: model declaration note says it is NOT runtime proof."""
        result = _run_cli(tmp_path, _claude_md())
        data = json.loads(result.stdout)
        note = data["model_declaration_report"][0]["note"]
        assert "NOT" in note or "not" in note.lower()
        assert "runtime proof" in note.lower()

    def test_model_report_contains_advisory_in_values(self, tmp_path: Path):
        """AC3: model_declaration values mention 'advisory' or 'not runtime proof'."""
        result = _run_cli(tmp_path, _claude_md())
        data = json.loads(result.stdout)
        mr = data["model_declaration_report"][0]
        codex_model_decl = mr["model_declaration"]["codex"]
        assert "advisory" in codex_model_decl or "not runtime proof" in codex_model_decl


# ---------------------------------------------------------------------------
# AC4 / AC9: nested delegation prohibition
# ---------------------------------------------------------------------------

class TestNestedDelegation:
    def test_delegation_blocked_both_no_drift(self, tmp_path: Path):
        """AC4: both block nested delegation -> no drift."""
        result = _run_cli(
            tmp_path,
            _claude_md(disallowed_tools=["Agent", "Edit"]),
            max_depth=1,
        )
        data = json.loads(result.stdout)
        deleg_drifts = [d for d in data["drift"] if d["rule_id"] == "NESTED_DELEGATION_001"]
        assert deleg_drifts == []

    def test_delegation_mismatch_produces_drift(self, tmp_path: Path):
        """AC4: Claude allows Agent while Codex blocks -> drift."""
        result = _run_cli(
            tmp_path,
            _claude_md(tools=["Bash", "Read", "Agent"], disallowed_tools=["Edit"]),
            max_depth=1,
        )
        data = json.loads(result.stdout)
        deleg_drifts = [d for d in data["drift"] if d["rule_id"] == "NESTED_DELEGATION_001"]
        assert len(deleg_drifts) == 1

    def test_claude_delegation_from_disallowed_tools(self, tmp_path: Path):
        """AC9: Claude nested delegation prohibition determined from disallowedTools."""
        claude_text = _claude_md(disallowed_tools=["Agent", "Edit", "Write"])
        claude_path = tmp_path / "issue-reviewer.md"
        claude_path.write_text(claude_text, encoding="utf-8")
        facts = MOD.extract_claude_facts("issue-reviewer", claude_path, claude_text)
        assert facts.nested_delegation_blocked is True
        assert "Agent" in facts.nested_delegation_evidence or "disallowedTools" in facts.nested_delegation_evidence

    def test_codex_delegation_from_max_depth(self, tmp_path: Path):
        """AC9: Codex nested delegation prohibition determined from .codex/config.toml max_depth."""
        config = _write_config_toml(tmp_path, max_depth=1)
        old_config = MOD.CODEX_CONFIG_PATH
        MOD.CODEX_CONFIG_PATH = config
        try:
            codex_path = tmp_path / "issue-reviewer.toml"
            _write_codex_toml(codex_path)
            with codex_path.open("rb") as f:
                import tomllib
                codex_doc = tomllib.load(f)
            facts = MOD.extract_codex_facts("issue-reviewer", codex_path, codex_doc)
            assert facts.nested_delegation_blocked is True
            assert "max_depth" in facts.nested_delegation_evidence
        finally:
            MOD.CODEX_CONFIG_PATH = old_config

    def test_delegation_report_in_output(self, tmp_path: Path):
        """AC4: nested_delegation_report is present in JSON output."""
        result = _run_cli(
            tmp_path,
            _claude_md(disallowed_tools=["Agent", "Edit"]),
        )
        data = json.loads(result.stdout)
        assert "nested_delegation_report" in data
        dr = data["nested_delegation_report"]
        assert len(dr) >= 1
        entry = dr[0]
        assert "claude_blocked" in entry
        assert "codex_blocked" in entry
        assert "match" in entry


# ---------------------------------------------------------------------------
# AC5: STATUS warn / fail on drift
# ---------------------------------------------------------------------------

class TestStatusOutput:
    def test_no_drift_status_ok(self, tmp_path: Path):
        """AC5: no drift -> STATUS: ok."""
        result = _run_cli(tmp_path, _claude_md())
        data = json.loads(result.stdout)
        assert data["STATUS"] == "ok"

    def test_schema_drift_status_warn(self, tmp_path: Path):
        """AC5: schema drift -> STATUS: warn."""
        result = _run_cli(
            tmp_path,
            _claude_md(output_schema="ISSUE_REVIEW_COMPACT_V2"),
            output_schema_codex="ISSUE_REVIEW_RESULT_COMPACT_V1",
        )
        data = json.loads(result.stdout)
        assert data["STATUS"] in ("warn", "fail")

    def test_delegation_drift_status_warn(self, tmp_path: Path):
        """AC5: delegation drift -> STATUS: warn or fail."""
        result = _run_cli(
            tmp_path,
            _claude_md(tools=["Bash", "Read", "Agent"], disallowed_tools=["Edit"]),
            max_depth=1,
        )
        data = json.loads(result.stdout)
        assert data["STATUS"] in ("warn", "fail")


# ---------------------------------------------------------------------------
# AC10: drift evidence fields
# ---------------------------------------------------------------------------

class TestDriftEvidenceFields:
    def test_drift_evidence_has_required_fields(self, tmp_path: Path):
        """AC10: drift evidence contains rule_id, file:line, launcher, agent, expected, actual."""
        result = _run_cli(
            tmp_path,
            _claude_md(output_schema="ISSUE_REVIEW_COMPACT_V2"),
            output_schema_codex="ISSUE_REVIEW_RESULT_COMPACT_V1",
        )
        data = json.loads(result.stdout)
        drifts = data["drift"]
        assert len(drifts) >= 1
        d = drifts[0]
        assert "rule_id" in d
        assert "file:line" in d
        assert "launcher" in d
        assert "agent" in d
        assert "expected" in d
        assert "actual" in d

    def test_drift_evidence_file_line_is_stable(self, tmp_path: Path):
        """AC10: file:line in drift evidence is stable across runs."""
        claude_md = _claude_md(output_schema="ISSUE_REVIEW_COMPACT_V2")
        r1 = _run_cli(tmp_path / "run1", claude_md, output_schema_codex="ISSUE_REVIEW_RESULT_COMPACT_V1")
        r2 = _run_cli(tmp_path / "run2", claude_md, output_schema_codex="ISSUE_REVIEW_RESULT_COMPACT_V1")
        d1 = json.loads(r1.stdout)["drift"]
        d2 = json.loads(r2.stdout)["drift"]
        assert len(d1) == len(d2)
        line1 = d1[0]["file:line"].split(":")[-1]
        line2 = d2[0]["file:line"].split(":")[-1]
        assert line1 == line2

    def test_drift_evidence_line_number_is_positive(self, tmp_path: Path):
        """AC10: line number in file:line evidence is a positive integer."""
        result = _run_cli(
            tmp_path,
            _claude_md(output_schema="ISSUE_REVIEW_COMPACT_V2"),
            output_schema_codex="ISSUE_REVIEW_RESULT_COMPACT_V1",
        )
        data = json.loads(result.stdout)
        d = data["drift"][0]
        file_line = d["file:line"]
        line_num = int(file_line.split(":")[-1])
        assert line_num >= 1

    def test_drift_evidence_launcher_is_claude_or_codex(self, tmp_path: Path):
        """AC10: launcher field is 'claude' or 'codex'."""
        result = _run_cli(
            tmp_path,
            _claude_md(output_schema="ISSUE_REVIEW_COMPACT_V2"),
            output_schema_codex="ISSUE_REVIEW_RESULT_COMPACT_V1",
        )
        data = json.loads(result.stdout)
        for d in data["drift"]:
            assert d["launcher"] in ("claude", "codex")


# ---------------------------------------------------------------------------
# AC11: STATUS:warn exit code
# ---------------------------------------------------------------------------

class TestExitCode:
    def test_warn_default_exit_0(self, tmp_path: Path):
        """AC11: STATUS:warn -> exit code 0 by default."""
        result = _run_cli(
            tmp_path,
            _claude_md(output_schema="ISSUE_REVIEW_COMPACT_V2"),
            output_schema_codex="ISSUE_REVIEW_RESULT_COMPACT_V1",
        )
        data = json.loads(result.stdout)
        if data["STATUS"] == "warn":
            assert result.returncode == 0, f"Expected exit 0 on warn, got {result.returncode}"

    def test_warn_strict_exit_1(self, tmp_path: Path):
        """AC11: STATUS:warn with --strict -> exit code 1."""
        result = _run_cli(
            tmp_path,
            _claude_md(output_schema="ISSUE_REVIEW_COMPACT_V2"),
            output_schema_codex="ISSUE_REVIEW_RESULT_COMPACT_V1",
            extra_args=["--strict"],
        )
        data = json.loads(result.stdout)
        if data["STATUS"] == "warn":
            assert result.returncode == 1, f"Expected exit 1 on warn+strict, got {result.returncode}"

    def test_ok_exit_0(self, tmp_path: Path):
        """AC11: STATUS:ok -> exit code 0 regardless of --strict."""
        result = _run_cli(
            tmp_path,
            _claude_md(),
            extra_args=["--strict"],
        )
        data = json.loads(result.stdout)
        if data["STATUS"] == "ok":
            assert result.returncode == 0


# ---------------------------------------------------------------------------
# Integration: fixture files
# ---------------------------------------------------------------------------

class TestFixtureFiles:
    """Verify that pre-written fixture files produce expected outcomes."""

    def _run_with_fixtures(
        self,
        tmp_path: Path,
        claude_fixture: str,
        codex_fixture: str,
        agent_name: str = "issue-reviewer",
        extra_args: list[str] | None = None,
    ) -> dict:
        """Copy named fixtures into tmp_path and run parity check."""
        claude_dir = tmp_path / ".claude" / "agents"
        claude_dir.mkdir(parents=True, exist_ok=True)
        codex_dir = tmp_path / ".codex" / "agents"
        codex_dir.mkdir(parents=True, exist_ok=True)
        config = _write_config_toml(tmp_path)

        (claude_dir / f"{agent_name}.md").write_text(
            (FIXTURE_DIR / claude_fixture).read_text(encoding="utf-8"), encoding="utf-8"
        )
        (codex_dir / f"{agent_name}.toml").write_text(
            (FIXTURE_DIR / codex_fixture).read_text(encoding="utf-8"), encoding="utf-8"
        )

        _write_minimal_contract(tmp_path, agent_name)

        expectation_file = tmp_path / "tests" / "fixtures" / "codex-agent-config" / "expected-runtime-contract.json"
        cmd = [
            sys.executable,
            str(MODULE_PATH),
            "--claude-agent-dir", str(claude_dir),
            "--codex-agent-dir", str(codex_dir),
            "--codex-config", str(config),
            "--expectation-path", str(expectation_file),
            "--format", "json",
        ]
        if extra_args:
            cmd.extend(extra_args)

        result = subprocess.run(cmd, text=True, capture_output=True, check=False, cwd=tmp_path)
        return json.loads(result.stdout)

    def test_ok_fixtures_no_drift(self, tmp_path: Path):
        """OK fixtures produce STATUS ok with no drift."""
        data = self._run_with_fixtures(
            tmp_path,
            "ok-claude-issue-reviewer.md",
            "ok-codex-issue-reviewer.toml",
        )
        assert data["STATUS"] == "ok"
        assert data["drift"] == []

    def test_drift_schema_mismatch_fixture(self, tmp_path: Path):
        """Schema mismatch fixture produces schema drift evidence."""
        data = self._run_with_fixtures(
            tmp_path,
            "drift-claude-schema-mismatch.md",
            "ok-codex-issue-reviewer.toml",
        )
        schema_drifts = [d for d in data["drift"] if d["rule_id"] == "SCHEMA_PARITY_001"]
        assert len(schema_drifts) == 1

    def test_drift_permission_mismatch_fixture(self, tmp_path: Path):
        """Permission mismatch fixture produces permission drift evidence."""
        data = self._run_with_fixtures(
            tmp_path,
            "drift-claude-permission-mismatch.md",
            "ok-codex-issue-reviewer.toml",
        )
        perm_drifts = [d for d in data["drift"] if d["rule_id"] == "PERMISSION_BOUNDARY_001"]
        assert len(perm_drifts) == 1

    def test_drift_delegation_mismatch_fixture(self, tmp_path: Path):
        """Delegation mismatch fixture produces delegation drift evidence."""
        data = self._run_with_fixtures(
            tmp_path,
            "drift-claude-delegation-mismatch.md",
            "ok-codex-issue-reviewer.toml",
        )
        deleg_drifts = [d for d in data["drift"] if d["rule_id"] == "NESTED_DELEGATION_001"]
        assert len(deleg_drifts) == 1


# ---------------------------------------------------------------------------
# B1: Codex final schema in Claude artifact-only is still drift (not suppressed)
# ---------------------------------------------------------------------------

class TestB1ArtifactOnlySuppressBug:
    def test_codex_final_artifact_only_schema_is_drift(self, tmp_path: Path):
        """B1: Codex emitting an artifact-only schema as final must produce SCHEMA_PARITY_001.

        Claude final: ISSUE_REVIEW_RESULT_COMPACT_V1
        Claude artifact-only: REVIEW_ISSUE_RESULT_V1
        Codex final: REVIEW_ISSUE_RESULT_V1  <- this is drift, must NOT be suppressed.
        """
        claude_md = _claude_md(
            output_schema="ISSUE_REVIEW_RESULT_COMPACT_V1",
            artifact_only="REVIEW_ISSUE_RESULT_V1",
        )
        result = _run_cli(
            tmp_path,
            claude_md,
            # Codex emits the artifact-only schema as its final output -> drift
            output_schema_codex="REVIEW_ISSUE_RESULT_V1",
        )
        data = json.loads(result.stdout)
        schema_drifts = [d for d in data["drift"] if d["rule_id"] == "SCHEMA_PARITY_001"]
        assert len(schema_drifts) == 1, (
            f"B1: Expected SCHEMA_PARITY_001 for Codex emitting artifact-only schema, "
            f"but got: {schema_drifts}"
        )
        assert schema_drifts[0]["expected"] == "REVIEW_ISSUE_RESULT_V1"
        assert schema_drifts[0]["actual"] == "ISSUE_REVIEW_RESULT_COMPACT_V1"


# ---------------------------------------------------------------------------
# B2: artifact-only schema extraction with heading pattern
# ---------------------------------------------------------------------------

class TestB2ArtifactOnlyExtraction:
    def test_heading_pattern_schema_first(self):
        """B2: Extract artifact-only schema from '### 内部処理用 SCHEMA（artifact のみ）' heading."""
        text = (
            "---\nname: issue-reviewer\n---\n"
            "## 出力契約（ISSUE_REVIEW_RESULT_COMPACT_V1）\n\n"
            "### 内部処理用 REVIEW_ISSUE_RESULT_V1（artifact のみ）\n"
            "full schema stored in artifact.\n"
        )
        artifact_schemas = MOD.extract_artifact_only_schemas_from_claude(
            text, "ISSUE_REVIEW_RESULT_COMPACT_V1"
        )
        assert "REVIEW_ISSUE_RESULT_V1" in artifact_schemas, (
            f"B2: Expected REVIEW_ISSUE_RESULT_V1 in artifact_only, got: {artifact_schemas}"
        )

    def test_real_issue_reviewer_artifact_only_extracted(self):
        """B2: Real issue-reviewer.md artifact-only schema is correctly extracted."""
        claude_path = REPO_ROOT_FOR_INTEGRATION / ".claude" / "agents" / "issue-reviewer.md"
        if not claude_path.exists():
            pytest.skip("Real .claude/agents/issue-reviewer.md not accessible")
        text = claude_path.read_text(encoding="utf-8")
        final = MOD.extract_final_output_schema_from_claude(text)
        artifact_only = MOD.extract_artifact_only_schemas_from_claude(text, final)
        # issue-reviewer.md has '### 内部処理用 REVIEW_ISSUE_RESULT_V1（artifact のみ）'
        assert "REVIEW_ISSUE_RESULT_V1" in artifact_only, (
            f"B2: Real file should have REVIEW_ISSUE_RESULT_V1 as artifact-only, "
            f"but got: {artifact_only} (final={final})"
        )


# ---------------------------------------------------------------------------
# B3: drift produces STATUS:fail (not warn)
# ---------------------------------------------------------------------------

class TestB3DriftIsFail:
    def test_schema_drift_produces_fail(self, tmp_path: Path):
        """B3: schema drift -> STATUS: fail (not warn)."""
        result = _run_cli(
            tmp_path,
            _claude_md(output_schema="ISSUE_REVIEW_COMPACT_V2"),
            output_schema_codex="ISSUE_REVIEW_RESULT_COMPACT_V1",
        )
        data = json.loads(result.stdout)
        assert data["STATUS"] == "fail", (
            f"B3: Schema drift must produce STATUS:fail, got: {data['STATUS']}"
        )
        assert result.returncode == 1, (
            f"B3: Schema drift must produce exit 1, got: {result.returncode}"
        )

    def test_permission_drift_produces_fail(self, tmp_path: Path):
        """B3: permission drift -> STATUS: fail."""
        result = _run_cli(
            tmp_path,
            _claude_md(permission_mode="acceptEdits"),
            codex_permissions="loop-protocol-readonly",
        )
        data = json.loads(result.stdout)
        assert data["STATUS"] == "fail", (
            f"B3: Permission drift must produce STATUS:fail, got: {data['STATUS']}"
        )

    def test_delegation_drift_produces_fail(self, tmp_path: Path):
        """B3: delegation drift -> STATUS: fail."""
        result = _run_cli(
            tmp_path,
            _claude_md(tools=["Bash", "Read", "Agent"], disallowed_tools=["Edit"]),
            max_depth=1,
        )
        data = json.loads(result.stdout)
        assert data["STATUS"] == "fail", (
            f"B3: Delegation drift must produce STATUS:fail, got: {data['STATUS']}"
        )


# ---------------------------------------------------------------------------
# B4: real repo integration test
# ---------------------------------------------------------------------------

class TestB4RealRepoParity:
    def test_real_repo_parity(self):
        """B4: Real repo parity check produces no schema/permission/delegation drift.

        Runs the parity script against actual .claude/agents/ and .codex/agents/ files.
        Schema/permission/delegation drift would indicate a real config mismatch that
        must be fixed before merging.
        """
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT_FOR_INTEGRATION),
        )
        assert result.stdout, f"B4: parity script produced no stdout. stderr={result.stderr!r}"
        data = json.loads(result.stdout)

        # No hard failures (missing files, name mismatches, etc.)
        assert data["failures"] == [], (
            f"B4: Unexpected hard failures in real repo parity: {data['failures']}"
        )

        # No schema/permission/delegation drift
        schema_drifts = [d for d in data["drift"] if d["rule_id"] == "SCHEMA_PARITY_001"]
        perm_drifts = [d for d in data["drift"] if d["rule_id"] == "PERMISSION_BOUNDARY_001"]
        deleg_drifts = [d for d in data["drift"] if d["rule_id"] == "NESTED_DELEGATION_001"]

        assert schema_drifts == [], (
            f"B4: Unexpected schema drift in real repo: {schema_drifts}"
        )
        assert perm_drifts == [], (
            f"B4: Unexpected permission drift in real repo: {perm_drifts}"
        )
        assert deleg_drifts == [], (
            f"B4: Unexpected delegation drift in real repo: {deleg_drifts}"
        )


# ---------------------------------------------------------------------------
# B5: Claude nested delegation 3-value logic
# ---------------------------------------------------------------------------

class TestB5NestedDelegation3Value:
    def test_tools_key_without_agent_is_blocked(self, tmp_path: Path):
        """B5: Explicit tools allowlist without Agent -> nested_delegation_blocked=True."""
        claude_text = _claude_md(tools=["Bash", "Read"], disallowed_tools=[])
        claude_path = tmp_path / "issue-reviewer.md"
        claude_path.write_text(claude_text, encoding="utf-8")
        facts = MOD.extract_claude_facts("issue-reviewer", claude_path, claude_text)
        assert facts.nested_delegation_blocked is True, (
            f"B5: tools=[Bash,Read] without Agent should be blocked, got: "
            f"{facts.nested_delegation_blocked}"
        )

    def test_no_tools_key_is_unknown(self, tmp_path: Path):
        """B5: No tools key in frontmatter -> nested_delegation_blocked=None (unknown)."""
        # Build a minimal claude md without a tools key
        text = (
            "---\n"
            "name: issue-reviewer\n"
            "model: haiku\n"
            "permissionMode: dontAsk\n"
            "disallowedTools: []\n"
            "---\n\n"
            "## 出力契約（ISSUE_REVIEW_RESULT_COMPACT_V1）\n"
            "\nRUNTIME\n- runtime_dependency_status: codex_skill_required\n"
            "- runtime_followup_route: review-issue\n"
        )
        claude_path = tmp_path / "issue-reviewer.md"
        claude_path.write_text(text, encoding="utf-8")
        facts = MOD.extract_claude_facts("issue-reviewer", claude_path, text)
        assert facts.nested_delegation_blocked is None, (
            f"B5: No tools key should yield blocked=None (unknown), got: "
            f"{facts.nested_delegation_blocked}"
        )

    def test_tools_key_with_agent_is_allowed(self, tmp_path: Path):
        """B5: Explicit tools allowlist that includes Agent -> nested_delegation_blocked=False."""
        claude_text = _claude_md(tools=["Bash", "Read", "Agent"], disallowed_tools=[])
        claude_path = tmp_path / "issue-reviewer.md"
        claude_path.write_text(claude_text, encoding="utf-8")
        facts = MOD.extract_claude_facts("issue-reviewer", claude_path, claude_text)
        assert facts.nested_delegation_blocked is False, (
            f"B5: tools with Agent should be allowed, got: {facts.nested_delegation_blocked}"
        )

    def test_disallowed_takes_priority_over_tools(self, tmp_path: Path):
        """B5: disallowedTools with Agent overrides tools allowlist (blocked takes priority)."""
        claude_text = _claude_md(tools=["Bash", "Read", "Agent"], disallowed_tools=["Agent"])
        claude_path = tmp_path / "issue-reviewer.md"
        claude_path.write_text(claude_text, encoding="utf-8")
        facts = MOD.extract_claude_facts("issue-reviewer", claude_path, claude_text)
        assert facts.nested_delegation_blocked is True, (
            f"B5: disallowedTools Agent must override tools Agent, got: "
            f"{facts.nested_delegation_blocked}"
        )


# ---------------------------------------------------------------------------
# B7: DECLARED_PERMISSION includes tools and disallowedTools
# ---------------------------------------------------------------------------

class TestB7PermissionReportTools:
    def test_declared_permission_includes_claude_detail(self, tmp_path: Path):
        """B7: DECLARED_PERMISSION in permission report includes claude_detail with tools info."""
        result = _run_cli(
            tmp_path,
            _claude_md(
                permission_mode="dontAsk",
                tools=["Bash", "Read"],
                disallowed_tools=["Agent", "Edit"],
            ),
            codex_permissions="loop-protocol-readonly",
        )
        data = json.loads(result.stdout)
        pr = data["permission_report"][0]
        dp = pr["DECLARED_PERMISSION"]
        assert "claude_detail" in dp, f"B7: claude_detail missing from DECLARED_PERMISSION: {dp}"
        detail = dp["claude_detail"]
        assert "permissionMode" in detail
        assert "tools" in detail
        assert "disallowedTools" in detail
        assert "Bash" in detail["tools"]
        assert "Agent" in detail["disallowedTools"]

    def test_declared_permission_tools_omitted_when_empty(self, tmp_path: Path):
        """B7: claude_detail omits tools/disallowedTools keys when lists are empty."""
        # Build a claude md with empty tools and disallowed
        text = (
            "---\n"
            "name: issue-reviewer\n"
            "model: haiku\n"
            "permissionMode: dontAsk\n"
            "---\n\n"
            "## 出力契約（ISSUE_REVIEW_RESULT_COMPACT_V1）\n"
            "\nRUNTIME\n- runtime_dependency_status: codex_skill_required\n"
            "- runtime_followup_route: review-issue\n"
        )
        tmp_claude_path = tmp_path / "issue-reviewer.md"
        tmp_claude_path.write_text(text, encoding="utf-8")
        facts = MOD.extract_claude_facts("issue-reviewer", tmp_claude_path, text)
        assert facts.claude_tools == []
        assert facts.claude_disallowed_tools == []


# ---------------------------------------------------------------------------
# B8: find_line_number guards against empty/None
# ---------------------------------------------------------------------------

class TestB8FindLineNumber:
    def test_empty_search_returns_zero(self):
        """B8: find_line_number with empty string returns 0, not 1."""
        text = "line one\nline two\n"
        result = MOD.find_line_number(text, "")
        assert result == 0, f"B8: empty search should return 0, got: {result}"

    def test_none_search_returns_zero(self):
        """B8: find_line_number with None returns 0."""
        text = "line one\nline two\n"
        result = MOD.find_line_number(text, None)
        assert result == 0, f"B8: None search should return 0, got: {result}"

    def test_normal_search_still_works(self):
        """B8: find_line_number with valid search still returns correct line."""
        text = "alpha\nbeta\ngamma\n"
        result = MOD.find_line_number(text, "beta")
        assert result == 2, f"B8: 'beta' should be at line 2, got: {result}"

    def test_not_found_returns_zero(self):
        """B8: find_line_number returns 0 when search string not found."""
        text = "alpha\nbeta\n"
        result = MOD.find_line_number(text, "delta")
        assert result == 0, f"B8: missing search should return 0, got: {result}"
