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
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTATION_PATH = REPO_ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json"
CODEX_CONFIG_PATH = REPO_ROOT / ".codex/config.toml"

# Agents in scope for parity check (issue-reviewer and issue-author)
PARITY_AGENTS = {"issue-reviewer", "issue-author", "scope-rollup-runner"}
CODEX_ONLY_ALLOWED_AGENTS = {"spark-skim", "spark-worker", "spark-deep"}
CODEX_ONLY_PARITY_REASON = "manual_codex_spark_agent"
CODEX_ONLY_MODEL = "gpt-5.3-codex-spark"

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

# Keywords in Codex developer_instructions that indicate nested delegation
CODEX_DELEGATION_KEYWORDS = [
    "spawn_agents_on_csv",
    "recursive delegation",
    "child agent spawn",
    "spawn subagents",
]


class DriftEvidence:
    def __init__(
        self,
        rule_id: str,
        file: str,
        line: int,
        launcher: str,  # "claude" or "codex"
        agent: str,
        expected: str,
        actual: str,
    ) -> None:
        self.rule_id = rule_id
        self.file = file
        self.line = line
        self.launcher = launcher
        self.agent = agent
        self.expected = expected
        self.actual = actual

    def __repr__(self) -> str:
        return (
            f"DriftEvidence(rule_id={self.rule_id!r}, file={self.file!r}, "
            f"line={self.line!r}, launcher={self.launcher!r}, agent={self.agent!r}, "
            f"expected={self.expected!r}, actual={self.actual!r})"
        )

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "file:line": f"{self.file}:{self.line}",
            "launcher": self.launcher,
            "agent": self.agent,
            "expected": self.expected,
            "actual": self.actual,
        }


class AgentParityFacts:
    def __init__(
        self,
        agent_name: str,
        final_output_schema: str | None = None,
        artifact_only_schema_names: list[str] | None = None,
        declared_permission: str | None = None,
        mutation_boundary: str | None = None,
        runtime_proof_note: str = (
            "Declaration is config-level; runtime proof requires launch-ledger validation."
        ),
        # B5: nested_delegation_blocked is now bool | None
        # True = blocked, False = allowed, None = unknown (no tools key)
        nested_delegation_blocked: bool | None = False,
        nested_delegation_evidence: str = "",
        model_declaration: str | None = None,
        reasoning_effort_declaration: str | None = None,
        evidence: list[DriftEvidence] | None = None,
        # For permission report (B7)
        claude_tools: list[str] | None = None,
        claude_disallowed_tools: list[str] | None = None,
    ) -> None:
        self.agent_name = agent_name
        # Final output schema (compact schema returned to caller)
        self.final_output_schema = final_output_schema
        # Artifact-only schemas (never returned to caller, stored in artifacts only)
        self.artifact_only_schema_names: list[str] = (
            artifact_only_schema_names if artifact_only_schema_names is not None else []
        )
        # Permission layers
        self.declared_permission = declared_permission  # claude.permissionMode or codex.default_permissions
        self.mutation_boundary = mutation_boundary      # derived readonly/issue-mutation/repo-write
        self.runtime_proof_note = runtime_proof_note
        # Delegation (B5: bool | None)
        self.nested_delegation_blocked: bool | None = nested_delegation_blocked
        self.nested_delegation_evidence = nested_delegation_evidence
        # Model config (advisory; not runtime proof)
        self.model_declaration = model_declaration
        self.reasoning_effort_declaration = reasoning_effort_declaration
        # Raw evidence list
        self.evidence: list[DriftEvidence] = evidence if evidence is not None else []
        # B7: store raw tools lists for permission report
        self.claude_tools: list[str] = claude_tools if claude_tools is not None else []
        self.claude_disallowed_tools: list[str] = claude_disallowed_tools if claude_disallowed_tools is not None else []

    def __repr__(self) -> str:
        return (
            f"AgentParityFacts(agent_name={self.agent_name!r}, "
            f"final_output_schema={self.final_output_schema!r}, "
            f"mutation_boundary={self.mutation_boundary!r})"
        )


def load_expectations() -> dict:
    return json.loads(EXPECTATION_PATH.read_text(encoding="utf-8"))


def is_codex_only_parity(expected: dict) -> bool:
    return expected.get("parity_mode") == "codex_only"


def excludes_permission_parity(expected: dict) -> bool:
    return expected.get("permission_parity") == "excluded"


def validate_codex_only_expectation(agent_name: str, expected: dict) -> list[str]:
    failures: list[str] = []
    if agent_name not in CODEX_ONLY_ALLOWED_AGENTS:
        failures.append(
            f"{expected['path']}: codex_only parity is restricted to {sorted(CODEX_ONLY_ALLOWED_AGENTS)!r}"
        )
    if not expected["path"].startswith(".codex/agents/spark-"):
        failures.append(f"{expected['path']}: codex_only parity path must stay under .codex/agents/spark-*")
    if expected.get("claude_agent_path", "__missing__") is not None:
        failures.append(f"{expected['path']}: codex_only parity must use claude_agent_path: null")
    if expected.get("parity_exception_reason") != CODEX_ONLY_PARITY_REASON:
        failures.append(
            f"{expected['path']}: codex_only parity must use parity_exception_reason {CODEX_ONLY_PARITY_REASON!r}"
        )
    if expected.get("model") != CODEX_ONLY_MODEL:
        failures.append(f"{expected['path']}: codex_only parity must use model {CODEX_ONLY_MODEL!r}")
    if expected.get("runtime_followup_route") != "none":
        failures.append(f"{expected['path']}: codex_only parity must use runtime_followup_route 'none'")
    if expected.get("runtime_dependency_status") != "codex_native":
        failures.append(f"{expected['path']}: codex_only parity must use runtime_dependency_status 'codex_native'")
    if expected.get("repo_local_skill_surfaces", []) != []:
        failures.append(f"{expected['path']}: codex_only parity must not declare repo_local_skill_surfaces")
    return failures


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


def find_line_number(text: str, search: str | None) -> int:
    """Return 1-based line number of first occurrence of search in text, or 0 if not found.

    B8: Returns 0 if search is None or empty string to avoid false line 1 matches.
    """
    # B8: guard against empty/None search
    if not search:
        return 0
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
    m = re.search(
        r"最終応答の唯一の fenced YAML block.*?```yaml\s*\n([A-Z][A-Z0-9_]+_V\d+):",
        text,
        re.DOTALL,
    )
    if m:
        return m.group(1)
    return None


def extract_artifact_only_schemas_from_claude(text: str, final_schema: str | None) -> list[str]:
    """Extract artifact-only schemas from Claude agent markdown.

    These are schemas mentioned in 'artifact only:' or '（artifact のみ）' patterns,
    as well as heading patterns like '### 内部処理用 SCHEMA（artifact のみ）'.

    B2: Added support for:
    - '### 内部処理用 SCHEMA_NAME（artifact のみ）' (schema name first in heading)
    - Lines/headings containing '内部処理用', 'artifact のみ', 'artifact-only'
    """
    artifact_only: list[str] = []

    # Pattern: 'artifact only: `SCHEMA`' or 'artifact のみ: `SCHEMA`' (schema at end)
    for m in re.finditer(r"artifact\s+(?:only|のみ)[:\s]+[`']?([A-Z][A-Z0-9_]+_V\d+)[`']?", text, re.IGNORECASE):
        name = m.group(1)
        if name not in artifact_only:
            artifact_only.append(name)

    # B2: Pattern in heading: '### 内部処理用 SCHEMA_NAME（artifact のみ）' or similar
    # Matches: heading lines where schema name appears before the artifact marker
    for m in re.finditer(
        r"(?:^|\n)#{1,6}\s+(?:内部処理用|artifact[- ]only)[^\n]*?"
        r"([A-Z][A-Z0-9_]+_V\d+)[^\n]*?(?:artifact[- ]?(?:only|のみ)|内部処理用|のみ)",
        text,
        re.IGNORECASE,
    ):
        name = m.group(1)
        if name not in artifact_only:
            artifact_only.append(name)

    # B2: Also match heading: '### 内部処理用 SCHEMA_NAME（artifact のみ）'
    # where schema name comes after '内部処理用'
    for m in re.finditer(
        r"(?:^|\n)#{1,6}[^\n]*?内部処理用\s+([A-Z][A-Z0-9_]+_V\d+)",
        text,
    ):
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
    if agent_name == "scope-rollup-runner":
        readonly_contract = "GitHub への書き込み / repo への書き込みは一切行わない。read-only 実行のみ。"
        facts.mutation_boundary = "readonly" if readonly_contract in claude_text else "unknown"

    # B7: store raw tools lists
    disallowed = fm.get("disallowedTools", [])
    if not isinstance(disallowed, list):
        disallowed = []
    tools = fm.get("tools", [])
    if not isinstance(tools, list):
        tools = []
    facts.claude_tools = list(tools)
    facts.claude_disallowed_tools = list(disallowed)

    # B5: Nested delegation — 3-value logic
    # True = blocked, False = allowed, None = unknown (no tools key and no disallowed)
    has_tools_key = "tools" in fm
    agent_denied = any(
        t == "Agent" or t.startswith("Agent(") for t in disallowed
    )
    agent_in_tools = any(
        t == "Agent" or t.startswith("Agent(") for t in tools
    )

    if agent_denied:
        # disallowedTools takes priority
        facts.nested_delegation_blocked = True
        line = find_line_number(claude_text, "Agent")
        facts.nested_delegation_evidence = (
            f"Agent in disallowedTools at {claude_path.name}:{line}"
        )
    elif has_tools_key:
        # Explicit tools allowlist: blocked unless Agent is in it
        facts.nested_delegation_blocked = not agent_in_tools
        if agent_in_tools:
            facts.nested_delegation_evidence = f"Agent present in tools at {claude_path.name}"
        else:
            facts.nested_delegation_evidence = (
                f"Agent absent from explicit tools allowlist in {claude_path.name}"
            )
    else:
        # No tools key at all -> unknown
        facts.nested_delegation_blocked = None
        facts.nested_delegation_evidence = (
            f"No tools key in frontmatter of {claude_path.name} (unknown)"
        )

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

    # B6: Check developer_instructions for delegation-enabling keywords
    for keyword in CODEX_DELEGATION_KEYWORDS:
        if keyword in instructions:
            facts.nested_delegation_evidence += (
                f"; WARNING: delegation keyword '{keyword}' found in developer_instructions"
            )

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
    *,
    compare_permission: bool = True,
) -> list[DriftEvidence]:
    """Compare Claude and Codex facts and return list of drift evidence.

    B1: Schema parity is always final-to-final comparison.
    artifact_only_schema_names is supplementary info about Claude docs;
    it does NOT suppress drift when Codex final schema matches a Claude artifact-only schema.

    B3: schema / permission / delegation drifts are all fail-level (returned as evidence).
    The caller promotes all drift to 'fail' status.
    """
    drifts: list[DriftEvidence] = []

    # --- Schema parity (AC1, AC7) ---
    # B1: Always compare final-to-final. Never suppress based on artifact_only_schema_names.
    c_schema = claude_facts.final_output_schema
    x_schema = codex_facts.final_output_schema
    if c_schema != x_schema:
        # B8: find_line_number handles None/empty gracefully
        claude_text = claude_path.read_text(encoding="utf-8")
        line = find_line_number(claude_text, c_schema)
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
    if compare_permission and c_boundary != x_boundary:
        claude_text = claude_path.read_text(encoding="utf-8")
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
    # B5: Handle None (unknown) — None vs True/False is considered drift
    c_blocked = claude_facts.nested_delegation_blocked
    x_blocked = codex_facts.nested_delegation_blocked
    if c_blocked != x_blocked:
        claude_text = claude_path.read_text(encoding="utf-8")
        line = find_line_number(claude_text, "disallowedTools")
        drifts.append(DriftEvidence(
            rule_id="NESTED_DELEGATION_001",
            file=str(claude_path),
            line=line,
            launcher="claude",
            agent=agent_name,
            expected=f"nested_delegation_blocked={x_blocked}",
            actual=f"nested_delegation_blocked={c_blocked}",
        ))

    # B6: Check if Codex delegation keywords were found
    if codex_facts.nested_delegation_evidence and "delegation keyword" in codex_facts.nested_delegation_evidence:
        drifts.append(DriftEvidence(
            rule_id="NESTED_DELEGATION_001",
            file=str(codex_path),
            line=0,
            launcher="codex",
            agent=agent_name,
            expected="no delegation keywords in developer_instructions",
            actual="delegation keyword found in developer_instructions",
        ))

    return drifts


def build_permission_report(
    agent_name: str,
    claude_facts: AgentParityFacts,
    codex_facts: AgentParityFacts,
) -> dict:
    """Build 3-layer permission report (AC8).

    B7: DECLARED_PERMISSION now includes claude.tools and claude.disallowedTools.
    """
    # B7: Build rich claude declared_permission
    claude_permission_info: dict[str, object] = {
        "permissionMode": claude_facts.declared_permission,
    }
    if claude_facts.claude_tools:
        claude_permission_info["tools"] = claude_facts.claude_tools
    if claude_facts.claude_disallowed_tools:
        claude_permission_info["disallowedTools"] = claude_facts.claude_disallowed_tools

    return {
        "agent": agent_name,
        "DECLARED_PERMISSION": {
            "claude": claude_facts.declared_permission,
            "claude_detail": claude_permission_info,
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
        lines.append("    DECLARED_PERMISSION:")
        for k, v in pr["DECLARED_PERMISSION"].items():
            lines.append(f"      {k}: {v}")
        lines.append("    MUTATION_BOUNDARY:")
        mb = pr["MUTATION_BOUNDARY"]
        lines.append(f"      claude: {mb['claude']}")
        lines.append(f"      codex: {mb['codex']}")
        lines.append(f"      match: {mb['match']}")
        lines.append(f"    RUNTIME_PROOF_NOTE: {pr['RUNTIME_PROOF_NOTE']}")
    lines.append("")

    lines.append("MODEL_DECLARATION_REPORT:")
    for mr in model_reports:
        lines.append(f"  agent: {mr['agent']}")
        lines.append("    model_declaration:")
        for k, v in mr["model_declaration"].items():
            lines.append(f"      {k}: {v}")
        lines.append("    reasoning_effort_declaration:")
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
    # parse_known_args を使用: exec() 経由で呼ばれた際に sys.argv に残る
    # 親スクリプト (check_codex_agent_config.py 等) の引数を無視するため
    args, _ = parser.parse_known_args(argv)

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
        codex_only = is_codex_only_parity(expected)
        if args.codex_agent_dir:
            codex_path = codex_agent_dir / f"{agent_name}.toml"
        else:
            codex_path = REPO_ROOT / expected["path"]
        if codex_only:
            claude_path = None
        elif args.claude_agent_dir:
            claude_path = claude_agent_dir / f"{agent_name}.md"
        else:
            claude_path = REPO_ROOT / expected["claude_agent_path"]

        if not codex_path.exists():
            failures.append(f"missing codex agent file: {expected['path']}")
            continue
        if codex_only:
            failures.extend(validate_codex_only_expectation(agent_name, expected))
        else:
            if not claude_path or not claude_path.exists():
                failures.append(f"missing claude agent file: {expected['claude_agent_path']}")
                continue

        codex_doc = read_toml(codex_path)
        codex_instructions = str(codex_doc.get("developer_instructions", ""))

        if codex_doc.get("name") != agent_name:
            failures.append(f"{expected['path']}: name must be {agent_name}")
        if not codex_only:
            claude_text = claude_path.read_text(encoding="utf-8")
            claude_frontmatter = extract_frontmatter(claude_text)
            if claude_frontmatter.get("name") != agent_name:
                failures.append(f"{expected['claude_agent_path']}: frontmatter name must be {agent_name}")
            if claude_frontmatter.get("model") != expected["claude_model"]:
                failures.append(
                    f"{expected['claude_agent_path']}: model expected"
                    f" {expected['claude_model']!r} got {claude_frontmatter.get('model')!r}"
                )
            if claude_frontmatter.get("permissionMode") != expected["claude_permission_mode"]:
                failures.append(
                    f"{expected['claude_agent_path']}: permissionMode expected"
                    f" {expected['claude_permission_mode']!r}"
                    f" got {claude_frontmatter.get('permissionMode')!r}"
                )

            tools = claude_frontmatter.get("tools", [])
            if not isinstance(tools, list) or not tools:
                failures.append(f"{expected['claude_agent_path']}: tools frontmatter list is required")

        runtime_status = extract_runtime_field(codex_instructions, "runtime_dependency_status")
        runtime_route = extract_runtime_field(codex_instructions, "runtime_followup_route")
        if runtime_status != expected["runtime_dependency_status"]:
            failures.append(
                f"{expected['path']}: runtime_dependency_status expected"
                f" {expected['runtime_dependency_status']!r} got {runtime_status!r}"
            )
        if runtime_route != expected["runtime_followup_route"]:
            failures.append(
                f"{expected['path']}: runtime_followup_route expected"
                f" {expected['runtime_followup_route']!r} got {runtime_route!r}"
            )

        if (
            not codex_only
            and claude_path
            and
            expected["runtime_followup_route"] != "none"
            and expected["runtime_followup_route"].split("|")[0] not in claude_text
        ):
            failures.append(
                f"{expected['claude_agent_path']}: expected route token"
                f" {expected['runtime_followup_route']!r} not found"
            )

        if not codex_only:
            if not excludes_permission_parity(expected):
                permission_expected = "acceptEdits" if expected["default_permissions"] == "loop-protocol-rtk" else "dontAsk"
                if agent_name == "post-merge-cleanup-worker":
                    permission_expected = "default"
                if claude_frontmatter.get("permissionMode") != permission_expected:
                    failures.append(
                        f"{expected['claude_agent_path']}: permissionMode must match"
                        f" Codex permission profile {expected['default_permissions']}"
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

        drifts = compare_parity(
            agent_name,
            claude_path,
            codex_path,
            claude_facts,
            codex_facts,
            compare_permission=not excludes_permission_parity(expected),
        )
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
    # B3: schema / permission / delegation drifts are fail-level.
    # Model/reasoning_effort mismatch (advisory) remains warn-only.
    if failures or all_drifts:
        status = "fail"
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
