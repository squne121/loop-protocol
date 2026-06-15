#!/usr/bin/env python3
"""Validate machine-readable parity between Codex agent TOML and Claude agent docs.

Extended to detect:
- Output schema name drift (final output vs artifact-only schemas)
- Mutation permission drift (DECLARED_PERMISSION / MUTATION_BOUNDARY / RUNTIME_PROOF_NOTE)
- Nested delegation prohibition drift
- Model/reasoning_effort config declaration (advisory, not runtime proof)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTATION_PATH = REPO_ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json"
CODEX_CONFIG_PATH = REPO_ROOT / ".codex/config.toml"

# Agents in scope for parity check (issue-reviewer and issue-author)
PARITY_AGENTS = {"issue-reviewer", "issue-author"}

# Permission profiles -> mutation boundary mapping
MUTATION_BOUNDARY_MAP = {
    "loop-protocol-readonly": "readonly",
    "loop-protocol-rtk": "issue-mutation",
    "loop-protocol-bootstrap": "repo-write",
}

# Claude permissionMode -> declared permission level
CLAUDE_PERMISSION_LEVEL_MAP = {
    "dontAsk": "readonly",
    "acceptEdits": "issue-mutation",
    "default": "repo-write",
}


@dataclass
class DriftEvidence:
    rule_id: str
    file: str
    line: int
    launcher: str  # "claude" or "codex"
    agent: str
    expected: str
    actual: str

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "file:line": f"{self.file}:{self.line}",
            "launcher": self.launcher,
            "agent": self.agent,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class AgentParityFacts:
    agent_name: str
    # Final output schema (compact schema returned to caller)
    final_output_schema: str | None = None
    # Artifact-only schemas (never returned to caller, stored in artifacts only)
    artifact_only_schema_names: list[str] = field(default_factory=list)
    # Permission layers
    declared_permission: str | None = None   # claude.permissionMode or codex.default_permissions
    mutation_boundary: str | None = None     # derived readonly/issue-mutation/repo-write
    runtime_proof_note: str = (
        "Declaration is config-level; runtime proof requires launch-ledger validation."
    )
    # Delegation
    nested_delegation_blocked: bool = False
    nested_delegation_evidence: str = ""
    # Model config (advisory; not runtime proof)
    model_declaration: str | None = None
    reasoning_effort_declaration: str | None = None
    # Raw evidence list
    evidence: list[DriftEvidence] = field(default_factory=list)


def load_expectations() -> dict:
    return json.loads(EXPECTATION_PATH.read_text(encoding="utf-8"))


def read_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def extract_frontmatter(text: str) -> dict[str, object]:
    if not text.startswith("---\n"):
        return {}
    _, _, remainder = text.partition("---\n")
    frontmatter, _, _ = remainder.partition("\n---\n")
    result: dict[str, object] = {}
    current_key: str | None = None
    for raw_line in frontmatter.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line.startswith("  - ") and current_key:
            result.setdefault(current_key, [])
            cast_list = result[current_key]
            if isinstance(cast_list, list):
                cast_list.append(raw_line[4:].strip())
            continue
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        current_key = key.strip()
        parsed = value.strip()
        if not parsed:
            result[current_key] = []
        else:
            result[current_key] = parsed
    return result


def extract_runtime_field(instructions: str, field: str) -> str | None:
    match = re.search(rf"{re.escape(field)}:\s*([a-zA-Z0-9._|-]+)", instructions)
    return match.group(1) if match else None


def find_line_number(text: str, search: str) -> int:
    """Return 1-based line number of first occurrence of search in text, or 0 if not found."""
    for i, line in enumerate(text.splitlines(), start=1):
        if search in line:
            return i
    return 0


def extract_final_output_schema_from_claude(text: str) -> str | None:
    """Extract the primary compact schema name from Claude agent markdown.

    Looks for patterns like:
    - '## 出力契約（SCHEMA_NAME）'
    - 'emit `SCHEMA_NAME` via'
    """
    # Pattern 1: 出力契約 heading with schema in parens
    m = re.search(r"出力契約[（(]([A-Z][A-Z0-9_]+_V\d+)[）)]", text)
    if m:
        return m.group(1)
    # Pattern 2: emit `SCHEMA_NAME` via
    m = re.search(r"emit\s+[`']?([A-Z][A-Z0-9_]+_V\d+)[`']?\s+via", text)
    if m:
        return m.group(1)
    return None


def extract_artifact_only_schemas_from_claude(text: str, final_schema: str | None) -> list[str]:
    """Extract artifact-only schemas from Claude agent markdown.

    These are schemas mentioned in 'artifact only:' or '（artifact のみ）' patterns,
    as well as any ISSUE_*_V* schemas that are not the final output schema.
    """
    artifact_only: list[str] = []

    # Pattern: 'artifact only: `SCHEMA`' or 'artifact のみ: `SCHEMA`'
    for m in re.finditer(r"artifact\s+(?:only|のみ)[:\s]+[`']?([A-Z][A-Z0-9_]+_V\d+)[`']?", text, re.IGNORECASE):
        name = m.group(1)
        if name not in artifact_only:
            artifact_only.append(name)

    # Pattern in parens: '出力契約（SCHEMA / artifact_only: SCHEMA2）'
    m = re.search(r"出力契約[（(][^）)]*artifact[_\s]only[:\s]+([A-Z][A-Z0-9_]+_V\d+)", text)
    if m:
        name = m.group(1)
        if name not in artifact_only:
            artifact_only.append(name)

    return artifact_only


def extract_final_output_schema_from_codex(instructions: str) -> str | None:
    """Extract the primary compact schema name from Codex developer_instructions.

    Looks for 'emit SCHEMA_NAME via' in OUTPUT_CONTRACT section.
    """
    m = re.search(r"emit\s+([A-Z][A-Z0-9_]+_V\d+)\s+via", instructions)
    if m:
        return m.group(1)
    # Fallback: first ISSUE_*_COMPACT_V* mention in OUTPUT_CONTRACT section
    oc_match = re.search(r"OUTPUT_CONTRACT.*?(?=\n[A-Z_]+\n|\Z)", instructions, re.DOTALL)
    if oc_match:
        m2 = re.search(r"([A-Z][A-Z0-9_]+_COMPACT_V\d+)", oc_match.group(0))
        if m2:
            return m2.group(1)
    return None


def extract_claude_facts(
    agent_name: str, claude_path: Path, claude_text: str
) -> AgentParityFacts:
    """Extract AgentParityFacts from a Claude agent markdown file."""
    facts = AgentParityFacts(agent_name=agent_name)
    fm = extract_frontmatter(claude_text)

    # Final output schema
    facts.final_output_schema = extract_final_output_schema_from_claude(claude_text)

    # Artifact-only schemas
    facts.artifact_only_schema_names = extract_artifact_only_schemas_from_claude(
        claude_text, facts.final_output_schema
    )

    # Permission layers
    permission_mode = str(fm.get("permissionMode", ""))
    facts.declared_permission = f"claude.permissionMode={permission_mode}"
    facts.mutation_boundary = CLAUDE_PERMISSION_LEVEL_MAP.get(permission_mode, "unknown")

    # Nested delegation: check disallowedTools for 'Agent'
    disallowed = fm.get("disallowedTools", [])
    if not isinstance(disallowed, list):
        disallowed = []
    tools = fm.get("tools", [])
    if not isinstance(tools, list):
        tools = []

    agent_in_disallowed = "Agent" in disallowed
    agent_in_tools = "Agent" in tools
    facts.nested_delegation_blocked = agent_in_disallowed and not agent_in_tools
    if agent_in_disallowed:
        line = find_line_number(claude_text, "- Agent")
        facts.nested_delegation_evidence = (
            f"Agent in disallowedTools at {claude_path.name}:{line}"
        )
    elif not agent_in_tools:
        facts.nested_delegation_evidence = (
            f"Agent absent from tools list in {claude_path.name}"
        )
    else:
        facts.nested_delegation_evidence = f"Agent present in tools at {claude_path.name}"

    # Model declaration (advisory)
    model = str(fm.get("model", ""))
    facts.model_declaration = f"config: model={model} (advisory, not runtime proof)"
    facts.reasoning_effort_declaration = (
        "config: reasoning_effort not declared in Claude frontmatter (advisory)"
    )

    return facts


def extract_codex_facts(
    agent_name: str, codex_path: Path, codex_doc: dict
) -> AgentParityFacts:
    """Extract AgentParityFacts from a Codex agent TOML."""
    facts = AgentParityFacts(agent_name=agent_name)
    instructions = str(codex_doc.get("developer_instructions", ""))

    # Final output schema
    facts.final_output_schema = extract_final_output_schema_from_codex(instructions)

    # No artifact-only schemas in Codex (OUTPUT_CONTRACT is minimal)
    facts.artifact_only_schema_names = []

    # Permission layers
    default_perms = str(codex_doc.get("default_permissions", ""))
    facts.declared_permission = f"codex.default_permissions={default_perms}"
    facts.mutation_boundary = MUTATION_BOUNDARY_MAP.get(default_perms, "unknown")

    # Nested delegation: Codex uses [agents].max_depth == 1 in config.toml
    # We read it as read-only dependency (no re-implementation)
    try:
        config = read_toml(CODEX_CONFIG_PATH)
        max_depth = config.get("agents", {}).get("max_depth")
        facts.nested_delegation_blocked = max_depth == 1
        facts.nested_delegation_evidence = (
            f"[agents].max_depth={max_depth} in .codex/config.toml"
        )
    except (FileNotFoundError, KeyError):
        facts.nested_delegation_blocked = False
        facts.nested_delegation_evidence = ".codex/config.toml not found or missing [agents].max_depth"

    # Model declaration (advisory)
    model = str(codex_doc.get("model", ""))
    reasoning_effort = str(codex_doc.get("model_reasoning_effort", ""))
    facts.model_declaration = f"config: model={model} (advisory, not runtime proof)"
    facts.reasoning_effort_declaration = (
        f"config: reasoning_effort={reasoning_effort} (advisory, not runtime proof)"
    )

    return facts


def compare_parity(
    agent_name: str,
    claude_path: Path,
    codex_path: Path,
    claude_facts: AgentParityFacts,
    codex_facts: AgentParityFacts,
) -> list[DriftEvidence]:
    """Compare Claude and Codex facts and return list of drift evidence."""
    drifts: list[DriftEvidence] = []

    # --- Schema parity (AC1, AC7) ---
    c_schema = claude_facts.final_output_schema
    x_schema = codex_facts.final_output_schema
    if c_schema != x_schema:
        # Check if codex schema is in claude's artifact-only list (AC7: not a fail)
        if x_schema and x_schema in claude_facts.artifact_only_schema_names:
            pass  # artifact-only schema: not a parity fail
        else:
            # Find line number of schema in claude file
            claude_text = claude_path.read_text(encoding="utf-8")
            line = find_line_number(claude_text, c_schema or "")
            drifts.append(DriftEvidence(
                rule_id="SCHEMA_PARITY_001",
                file=str(claude_path),
                line=line,
                launcher="claude",
                agent=agent_name,
                expected=x_schema or "(none)",
                actual=c_schema or "(none)",
            ))

    # --- Permission parity (AC2, AC8) ---
    c_boundary = claude_facts.mutation_boundary
    x_boundary = codex_facts.mutation_boundary
    if c_boundary != x_boundary:
        claude_text = claude_path.read_text(encoding="utf-8")
        fm = extract_frontmatter(claude_text)
        pm = str(fm.get("permissionMode", ""))
        line = find_line_number(claude_text, "permissionMode")
        drifts.append(DriftEvidence(
            rule_id="PERMISSION_BOUNDARY_001",
            file=str(claude_path),
            line=line,
            launcher="claude",
            agent=agent_name,
            expected=x_boundary or "unknown",
            actual=c_boundary or "unknown",
        ))

    # --- Nested delegation parity (AC4, AC9) ---
    if claude_facts.nested_delegation_blocked != codex_facts.nested_delegation_blocked:
        claude_text = claude_path.read_text(encoding="utf-8")
        line = find_line_number(claude_text, "disallowedTools")
        drifts.append(DriftEvidence(
            rule_id="NESTED_DELEGATION_001",
            file=str(claude_path),
            line=line,
            launcher="claude",
            agent=agent_name,
            expected=f"nested_delegation_blocked={codex_facts.nested_delegation_blocked}",
            actual=f"nested_delegation_blocked={claude_facts.nested_delegation_blocked}",
        ))

    return drifts


def build_permission_report(
    agent_name: str,
    claude_facts: AgentParityFacts,
    codex_facts: AgentParityFacts,
) -> dict:
    """Build 3-layer permission report (AC8)."""
    return {
        "agent": agent_name,
        "DECLARED_PERMISSION": {
            "claude": claude_facts.declared_permission,
            "codex": codex_facts.declared_permission,
        },
        "MUTATION_BOUNDARY": {
            "claude": claude_facts.mutation_boundary,
            "codex": codex_facts.mutation_boundary,
            "match": claude_facts.mutation_boundary == codex_facts.mutation_boundary,
        },
        "RUNTIME_PROOF_NOTE": claude_facts.runtime_proof_note,
    }


def build_model_report(
    agent_name: str,
    claude_facts: AgentParityFacts,
    codex_facts: AgentParityFacts,
) -> dict:
    """Build model/reasoning_effort config declaration report (AC3)."""
    return {
        "agent": agent_name,
        "model_declaration": {
            "claude": claude_facts.model_declaration,
            "codex": codex_facts.model_declaration,
        },
        "reasoning_effort_declaration": {
            "claude": claude_facts.reasoning_effort_declaration,
            "codex": codex_facts.reasoning_effort_declaration,
        },
        "note": (
            "Model and reasoning_effort are config-level declarations only. "
            "They are NOT runtime proof of actual model used."
        ),
    }


def format_text_report(
    all_drifts: list[DriftEvidence],
    permission_reports: list[dict],
    model_reports: list[dict],
    delegation_reports: list[dict],
    status: str,
) -> str:
    lines: list[str] = []
    lines.append(f"STATUS: {status}")
    lines.append("")

    if all_drifts:
        lines.append("DRIFT:")
        for d in all_drifts:
            lines.append(
                f"  [{d.rule_id}] {d.file}:{d.line} "
                f"launcher={d.launcher} agent={d.agent} "
                f"expected={d.expected!r} actual={d.actual!r}"
            )
        lines.append("")

    lines.append("PERMISSION_REPORT:")
    for pr in permission_reports:
        lines.append(f"  agent: {pr['agent']}")
        lines.append(f"    DECLARED_PERMISSION:")
        for k, v in pr["DECLARED_PERMISSION"].items():
            lines.append(f"      {k}: {v}")
        lines.append(f"    MUTATION_BOUNDARY:")
        mb = pr["MUTATION_BOUNDARY"]
        lines.append(f"      claude: {mb['claude']}")
        lines.append(f"      codex: {mb['codex']}")
        lines.append(f"      match: {mb['match']}")
        lines.append(f"    RUNTIME_PROOF_NOTE: {pr['RUNTIME_PROOF_NOTE']}")
    lines.append("")

    lines.append("MODEL_DECLARATION_REPORT:")
    for mr in model_reports:
        lines.append(f"  agent: {mr['agent']}")
        lines.append(f"    model_declaration:")
        for k, v in mr["model_declaration"].items():
            lines.append(f"      {k}: {v}")
        lines.append(f"    reasoning_effort_declaration:")
        for k, v in mr["reasoning_effort_declaration"].items():
            lines.append(f"      {k}: {v}")
        lines.append(f"    note: {mr['note']}")
    lines.append("")

    lines.append("NESTED_DELEGATION_REPORT:")
    for dr in delegation_reports:
        lines.append(f"  agent: {dr['agent']}")
        lines.append(f"    claude: blocked={dr['claude_blocked']} evidence={dr['claude_evidence']}")
        lines.append(f"    codex: blocked={dr['codex_blocked']} evidence={dr['codex_evidence']}")
        lines.append(f"    match: {dr['match']}")
    lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 on STATUS:warn (default: exit 0 on warn, exit 1 on fail)",
    )
    # Allow overriding paths for testing
    parser.add_argument("--claude-agent-dir", type=Path, default=None)
    parser.add_argument("--codex-agent-dir", type=Path, default=None)
    parser.add_argument("--codex-config", type=Path, default=None)
    parser.add_argument("--expectation-path", type=Path, default=None,
                        help="Override path to expected-runtime-contract.json (for testing)")
    args = parser.parse_args(argv)

    claude_agent_dir = args.claude_agent_dir or (REPO_ROOT / ".claude/agents")
    codex_agent_dir = args.codex_agent_dir or (REPO_ROOT / ".codex/agents")
    global CODEX_CONFIG_PATH
    if args.codex_config:
        CODEX_CONFIG_PATH = args.codex_config

    expectation_path = args.expectation_path or EXPECTATION_PATH

    def load_expectations_override() -> dict:
        return json.loads(expectation_path.read_text(encoding="utf-8"))

    expectations = load_expectations_override()
    failures: list[str] = []
    all_drifts: list[DriftEvidence] = []
    permission_reports: list[dict] = []
    model_reports: list[dict] = []
    delegation_reports: list[dict] = []

    # --- Legacy checks (preserved for backward compatibility) ---
    # When agent dirs are overridden (testing), derive paths from agent dirs
    for agent_name, expected in expectations["required_agents"].items():
        if args.codex_agent_dir:
            codex_path = codex_agent_dir / f"{agent_name}.toml"
        else:
            codex_path = REPO_ROOT / expected["path"]
        if args.claude_agent_dir:
            claude_path = claude_agent_dir / f"{agent_name}.md"
        else:
            claude_path = REPO_ROOT / expected["claude_agent_path"]

        if not codex_path.exists():
            failures.append(f"missing codex agent file: {expected['path']}")
            continue
        if not claude_path.exists():
            failures.append(f"missing claude agent file: {expected['claude_agent_path']}")
            continue

        codex_doc = read_toml(codex_path)
        claude_text = claude_path.read_text(encoding="utf-8")
        claude_frontmatter = extract_frontmatter(claude_text)
        codex_instructions = str(codex_doc.get("developer_instructions", ""))

        if codex_doc.get("name") != agent_name:
            failures.append(f"{expected['path']}: name must be {agent_name}")
        if claude_frontmatter.get("name") != agent_name:
            failures.append(f"{expected['claude_agent_path']}: frontmatter name must be {agent_name}")
        if claude_frontmatter.get("model") != expected["claude_model"]:
            failures.append(
                f"{expected['claude_agent_path']}: model expected {expected['claude_model']!r} got {claude_frontmatter.get('model')!r}"
            )
        if claude_frontmatter.get("permissionMode") != expected["claude_permission_mode"]:
            failures.append(
                f"{expected['claude_agent_path']}: permissionMode expected {expected['claude_permission_mode']!r} got {claude_frontmatter.get('permissionMode')!r}"
            )

        tools = claude_frontmatter.get("tools", [])
        if not isinstance(tools, list) or not tools:
            failures.append(f"{expected['claude_agent_path']}: tools frontmatter list is required")

        runtime_status = extract_runtime_field(codex_instructions, "runtime_dependency_status")
        runtime_route = extract_runtime_field(codex_instructions, "runtime_followup_route")
        if runtime_status != expected["runtime_dependency_status"]:
            failures.append(
                f"{expected['path']}: runtime_dependency_status expected {expected['runtime_dependency_status']!r} got {runtime_status!r}"
            )
        if runtime_route != expected["runtime_followup_route"]:
            failures.append(
                f"{expected['path']}: runtime_followup_route expected {expected['runtime_followup_route']!r} got {runtime_route!r}"
            )

        if expected["runtime_followup_route"] != "none" and expected["runtime_followup_route"].split("|")[0] not in claude_text:
            failures.append(
                f"{expected['claude_agent_path']}: expected route token {expected['runtime_followup_route']!r} not found"
            )

        permission_expected = "acceptEdits" if expected["default_permissions"] == "loop-protocol-rtk" else "dontAsk"
        if agent_name == "post-merge-cleanup-worker":
            permission_expected = "default"
        if claude_frontmatter.get("permissionMode") != permission_expected:
            failures.append(
                f"{expected['claude_agent_path']}: permissionMode must match Codex permission profile {expected['default_permissions']}"
            )

    # --- Extended parity checks for PARITY_AGENTS ---
    for agent_name in PARITY_AGENTS:
        expected = expectations["required_agents"].get(agent_name)
        if expected is None:
            continue

        codex_path = codex_agent_dir / f"{agent_name}.toml"
        claude_path = claude_agent_dir / f"{agent_name}.md"

        if not codex_path.exists() or not claude_path.exists():
            continue  # already reported in legacy checks

        codex_doc = read_toml(codex_path)
        claude_text = claude_path.read_text(encoding="utf-8")

        claude_facts = extract_claude_facts(agent_name, claude_path, claude_text)
        codex_facts = extract_codex_facts(agent_name, codex_path, codex_doc)

        drifts = compare_parity(agent_name, claude_path, codex_path, claude_facts, codex_facts)
        all_drifts.extend(drifts)

        permission_reports.append(build_permission_report(agent_name, claude_facts, codex_facts))
        model_reports.append(build_model_report(agent_name, claude_facts, codex_facts))
        delegation_reports.append({
            "agent": agent_name,
            "claude_blocked": claude_facts.nested_delegation_blocked,
            "claude_evidence": claude_facts.nested_delegation_evidence,
            "codex_blocked": codex_facts.nested_delegation_blocked,
            "codex_evidence": codex_facts.nested_delegation_evidence,
            "match": claude_facts.nested_delegation_blocked == codex_facts.nested_delegation_blocked,
        })

    # --- Determine overall status ---
    if failures:
        status = "fail"
    elif all_drifts:
        status = "warn"
    else:
        status = "ok"

    if args.format == "json":
        result = {
            "STATUS": status,
            "failures": failures,
            "drift": [d.to_dict() for d in all_drifts],
            "permission_report": permission_reports,
            "model_declaration_report": model_reports,
            "nested_delegation_report": delegation_reports,
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        report = format_text_report(
            all_drifts, permission_reports, model_reports, delegation_reports, status
        )
        print(report)
        if failures:
            for f in failures:
                print(f"[FAIL] {f}")

    if status == "fail":
        return 1
    if status == "warn" and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
