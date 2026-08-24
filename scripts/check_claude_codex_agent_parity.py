#!/usr/bin/env python3
"""Validate machine-readable parity between Codex agent TOML and Claude agent docs.

Extended to detect:
- Output schema name drift (final output vs artifact-only schemas)
- Mutation permission drift (DECLARED_PERMISSION / MUTATION_BOUNDARY / RUNTIME_PROOF_NOTE)
- Model/reasoning_effort config declaration (advisory, not runtime proof)

Nested delegation (delegation_intent_hint) is advisory-only (PR #1879, af511e17,
Issue #1948): a Claude/Codex delegation_intent_hint mismatch alone is surfaced in
nested_delegation_report but never added to the drift list below, and never
changes STATUS or exit code.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTATION_PATH = REPO_ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json"
CODEX_CONFIG_PATH = REPO_ROOT / ".codex/config.toml"

# AC6: the former 4-agent PARITY_AGENTS allowlist has been removed in-place;
# the extended-check agent set is now resolved dynamically by
# resolve_shared_claude_runtime_agents() from the asset inventory
# classification (see below).
CODEX_ONLY_ALLOWED_AGENTS = {"spark-skim", "spark-worker", "spark-deep"}
CODEX_ONLY_PARITY_REASON = "manual_codex_spark_agent"
CODEX_ONLY_MODEL = "gpt-5.3-codex-spark"

# Permission profiles -> mutation boundary mapping
MUTATION_BOUNDARY_MAP = {
    "loop-protocol-readonly": "readonly",
    "loop-protocol-rtk": "issue-mutation",
    "loop-protocol-bootstrap": "repo-write",
    "loop-protocol-web-research": "readonly",
}

# Claude permissionMode -> a purely *informational* prompt-handling policy
# label. This is deliberately NOT used to derive `mutation_class` (Issue
# #2160 AC4, human PR-review 2026-08-25 P0 blocker, PR #2334 comment
# 5401806450): `permissionMode` describes whether Claude Code asks before
# running a tool, independent of the agent's actual tool allowlist and the
# mutation boundary of any skill the agent invokes. Treating `permissionMode`
# (alone, or combined with a heuristic tool-list scan) as the source of
# `mutation_class` was rejected by the human reviewer: this repository's
# `.claude/settings.json` Bash allowlist includes real mutation commands
# (`git push`, `git commit`, `gh issue`/`gh pr` mutate subcommands), so
# `dontAsk + Bash` alone proves nothing about mutation capability either way
# -- Bash presence is not evidence of "read_only", and Bash absence is not
# evidence of "repo-write". `mutation_class` is therefore an explicit,
# per-agent *declared ground truth* (see `required_agents.<agent>.
# mutation_class` in expected-runtime-contract.json), resolved by
# `resolve_mutation_class()` below, with `permissionMode` reported alongside
# it purely as an informational, independent axis.
CLAUDE_PERMISSION_LEVEL_MAP = {
    "dontAsk": "readonly",
    "acceptEdits": "issue-mutation",
    "default": "repo-write",
}

# Tools whose mere presence in an *allowed* tool set is direct evidence of
# repo-mutation capability, regardless of permissionMode.
MUTATION_CAPABLE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Tool alias normalization applied before any tool-set comparison (AC7).
# "Task" is a historical/alternate name for the SubAgent-delegation tool
# that this repository canonically calls "Agent".
TOOL_ALIAS_NORMALIZE = {"Task": "Agent"}

# Semantic model class: a coarse, repository-local classification of a
# Claude model_alias / Codex model, used for role-level comparison instead
# of exact provider-specific model ID equality (AC6).
SEMANTIC_MODEL_CLASS_MAP = {
    "haiku": "fast_cheap",
    "sonnet": "balanced",
    "opus": "deep_reasoning",
}

# Asset inventory classification enum (AC2, AC9).
ALLOWED_CLASSIFICATIONS = {
    "shared_claude_runtime",
    "claude_only",
    "legacy_codex_projection",
    "legacy_codex_only",
    "experimental",
}


class FrontmatterParseError(ValueError):
    """Raised when Claude agent frontmatter contains unsupported YAML syntax.

    AC7: unsupported syntax must fail loudly, never silently skip.
    """


def normalize_tool_alias(tool: str) -> str:
    """Normalize a tool token, mapping known aliases (e.g. Task -> Agent).

    Handles both bare tokens ("Task") and parameterized tokens
    ("Task(subagent_type:...)").
    """
    base, _, rest = tool.partition("(")
    normalized_base = TOOL_ALIAS_NORMALIZE.get(base, base)
    return normalized_base if not rest else f"{normalized_base}({rest}"


def normalize_tool_list(tools: list[str]) -> list[str]:
    return [normalize_tool_alias(t) for t in tools]


def resolve_mutation_class(
    declared_mutation_class: str | None,
    tools: list[str],
    disallowed_tools: list[str],
) -> tuple[str, str | None]:
    """Resolve `mutation_class` from its explicit declared ground truth.

    AC4 (human PR-review 2026-08-25 P0 blocker, PR #2334 comment 5401806450):
    `mutation_class` is NOT derived from `permissionMode` (alone or combined
    with a heuristic tool-list scan). It is an explicit, repository-local
    *behavioral contract* declared per agent in the checked-in fixture
    (`required_agents.<agent>.mutation_class` in
    expected-runtime-contract.json) -- the agent definition, its declared
    tool set, the skills it invokes, and its mutation boundary, judged as a
    whole by a human/reviewer, not synthesized from `permissionMode` at
    check time. This is a behavioral contract, not a security boundary
    (actual enforcement is PreToolUse hooks / branch protection / CI).

    `permissionMode` is reported as a fully separate, purely informational
    axis (see `declared_permission` on `AgentParityFacts`) and is never an
    input to this function. Bash presence in the tool allowlist is never,
    by itself, evidence for or against any mutation_class value: this
    repository's `.claude/settings.json` Bash allowlist includes real
    mutation commands (`git push`, `git commit`, `gh issue`/`gh pr` mutate
    subcommands), so the canonical counterexample `dontAsk + Bash !=
    read_only` holds even for a bare `tools: [Bash]` agent -- a `dontAsk`
    agent whose declared ground truth is `mutation_class: repo-write` with
    only `Bash` in its tool allowlist keeps that declared value; the
    checker never forces it back to "readonly" just because no Edit-family
    tool is present.

    The only automated cross-check performed here is an explicit
    contradiction between the declared contract and the tool grant: if the
    declared mutation_class is "readonly" but the tool allowlist grants a
    mutation-capable tool (Edit/Write/MultiEdit/NotebookEdit) that is not
    itself explicitly denied via `disallowedTools:`, that contradiction is
    returned as a non-None reason string so the caller can fail the check.

    Returns (mutation_class, contradiction_reason_or_none).
    """
    mutation_class = declared_mutation_class or "unknown"
    if mutation_class != "readonly":
        return mutation_class, None

    tool_bases = {normalize_tool_alias(t).split("(")[0] for t in tools}
    disallowed_bases = {normalize_tool_alias(t).split("(")[0] for t in disallowed_tools}
    mutation_tools_allowed = (tool_bases & MUTATION_CAPABLE_TOOLS) - disallowed_bases

    if mutation_tools_allowed:
        return mutation_class, (
            f"declared mutation_class='readonly' contradicted by mutation-capable "
            f"tool(s) {sorted(mutation_tools_allowed)!r} present in the tools "
            f"allowlist and not denied via disallowedTools"
        )

    return mutation_class, None

# Keywords in Codex developer_instructions that indicate nested delegation.
# These are advisory prose hints only; they are not runtime capability or
# strict-parity authority.
CODEX_DELEGATION_KEYWORDS = [
    "spawn_agents_on_csv",
    "recursive delegation",
    "child agent spawn",
    "spawn subagents",
]


def classify_delegation_intent_hint(instructions: str) -> str:
    """Return an advisory prose hint: allowed, blocked, or unknown."""
    normalized = instructions.casefold()
    blocked_phrases = (
        "do not spawn subagents",
        "must not spawn subagents",
        "do not use spawn_agent",
        "must not use spawn_agent",
    )
    if any(phrase in normalized for phrase in blocked_phrases):
        return "blocked"
    if any(keyword.casefold() in normalized for keyword in CODEX_DELEGATION_KEYWORDS):
        return "allowed"
    return "unknown"


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
        delegation_intent_hint: str = "unknown",
        delegation_intent_evidence: str = "",
        model_declaration: str | None = None,
        reasoning_effort_declaration: str | None = None,
        semantic_model_class: str = "unknown",
        effort_declared: str | None = None,
        evidence: list[DriftEvidence] | None = None,
        # For permission report (B7)
        claude_tools: list[str] | None = None,
        claude_disallowed_tools: list[str] | None = None,
        # AC4: contradiction reason between declared mutation_class ground
        # truth and the tool allowlist, or None when consistent.
        mutation_class_contradiction: str | None = None,
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
        self.delegation_intent_hint = delegation_intent_hint
        self.delegation_intent_evidence = delegation_intent_evidence
        # Model config (advisory; not runtime proof)
        self.model_declaration = model_declaration
        self.reasoning_effort_declaration = reasoning_effort_declaration
        self.semantic_model_class = semantic_model_class
        self.effort_declared = effort_declared
        # Raw evidence list
        self.evidence: list[DriftEvidence] = evidence if evidence is not None else []
        # B7: store raw tools lists for permission report
        self.claude_tools: list[str] = claude_tools if claude_tools is not None else []
        self.claude_disallowed_tools: list[str] = claude_disallowed_tools if claude_disallowed_tools is not None else []
        # AC4: mutation_class / permissionMode contradiction (see resolve_mutation_class)
        self.mutation_class_contradiction = mutation_class_contradiction

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


def excludes_permission_parity(agent_name: str, expected: dict) -> bool:
    """Return True when this agent's permission comparison is explicitly
    allowlisted out of strict PERMISSION_BOUNDARY_001 drift comparison.

    Generalized (Issue #2160 AC6) from a single scope-rollup-runner literal
    dict comparison to any agent whose expectation entry declares
    permission_parity: excluded with a well-formed permission_exclusion
    record naming itself as allowlisted_agent. This does not silently widen
    scope: every excluded agent must be individually declared in the
    checked-in expected-runtime-contract.json fixture with a reason and a
    follow_up_issue, which is reviewable in the PR diff.
    """
    exclusion = expected.get("permission_exclusion")
    if expected.get("permission_parity") != "excluded" or not isinstance(exclusion, dict):
        return False
    required_keys = {"allowlisted_agent", "reason", "follow_up_issue", "expires_on"}
    if not required_keys <= exclusion.keys():
        return False
    return exclusion.get("allowlisted_agent") == agent_name


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


def extract_frontmatter(text: str, source_path: str = "<unknown>") -> dict[str, object]:
    """Parse Claude agent markdown frontmatter using real YAML (yaml.safe_load).

    AC7 (human PR reviewer P1 blocker, 2026-08-25, PR #2334 comment
    5401806450): the previous hand-rolled `key: value` subset parser
    rejected nested mappings and non-empty inline objects, and only did a
    naive comma-split for inline lists -- but Claude Code's official
    subagent frontmatter is real YAML, supporting structured fields
    (mcpServers/hooks) and the shorthand `tools: Read, Grep, Glob, Bash`
    form. This repository already depends on `pyyaml>=6.0`.

    Fail-loud behavior is preserved: genuinely invalid YAML syntax (or a
    frontmatter block that does not parse to a mapping) raises
    FrontmatterParseError -- it is never silently skipped. `tools` /
    `disallowedTools` are normalized here from either a YAML list or the
    comma-separated shorthand string form into a plain `list[str]`; the
    Task->Agent tool alias is applied separately by callers before any
    tool-set comparison (see `normalize_tool_list()`).
    """
    if not text.startswith("---\n"):
        return {}
    _, _, remainder = text.partition("---\n")
    frontmatter, sep, _ = remainder.partition("\n---\n")
    if not sep:
        raise FrontmatterParseError(
            f"{source_path}: frontmatter block is not terminated by a closing '---' line"
        )
    try:
        result = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise FrontmatterParseError(
            f"{source_path}: invalid YAML frontmatter: {exc}"
        ) from exc
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise FrontmatterParseError(
            f"{source_path}: frontmatter must parse to a YAML mapping, got "
            f"{type(result).__name__}"
        )
    for key in ("tools", "disallowedTools"):
        if key in result and result[key] is not None:
            result[key] = _normalize_tool_field(result[key], source_path, key)
    return result


def _normalize_tool_field(value: object, source_path: str, key: str) -> list[str]:
    """Normalize a `tools:`/`disallowedTools:` YAML value into `list[str]`.

    Accepts both the real YAML list form and the comma-separated shorthand
    string form Claude Code's official subagent frontmatter also supports
    (e.g. `tools: Read, Grep, Glob, Bash`).
    """
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    raise FrontmatterParseError(
        f"{source_path}: {key!r} must be a YAML list or comma-separated "
        f"string, got {type(value).__name__}"
    )


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
    agent_name: str,
    claude_path: Path,
    claude_text: str,
    declared_mutation_class: str | None = None,
) -> AgentParityFacts:
    """Extract AgentParityFacts from a Claude agent markdown file.

    `declared_mutation_class` (AC4) is the per-agent ground-truth value from
    `required_agents.<agent>.mutation_class` in
    expected-runtime-contract.json. When omitted (e.g. isolated unit tests
    that do not construct a fixture entry), it defaults to None and
    `mutation_boundary` resolves to "unknown" rather than being guessed from
    permissionMode.
    """
    facts = AgentParityFacts(agent_name=agent_name)
    fm = extract_frontmatter(claude_text, source_path=str(claude_path))

    # Final output schema
    facts.final_output_schema = extract_final_output_schema_from_claude(claude_text)

    # Artifact-only schemas
    facts.artifact_only_schema_names = extract_artifact_only_schemas_from_claude(
        claude_text, facts.final_output_schema
    )

    # B7: store raw tools lists (AC7: Task/Agent alias normalized before any
    # tool-set comparison)
    disallowed = fm.get("disallowedTools", [])
    if not isinstance(disallowed, list):
        disallowed = []
    tools = fm.get("tools", [])
    if not isinstance(tools, list):
        tools = []
    disallowed = normalize_tool_list([str(t) for t in disallowed])
    tools = normalize_tool_list([str(t) for t in tools])
    facts.claude_tools = list(tools)
    facts.claude_disallowed_tools = list(disallowed)

    # Permission layers (AC4: permissionMode is a separate, purely
    # informational axis; mutation_class is the declared ground truth,
    # cross-checked only for an explicit contradiction against the tool
    # allowlist -- see resolve_mutation_class()).
    permission_mode = str(fm.get("permissionMode", ""))
    facts.declared_permission = f"claude.permissionMode={permission_mode}"
    if declared_mutation_class is not None:
        facts.mutation_boundary, facts.mutation_class_contradiction = resolve_mutation_class(
            declared_mutation_class, tools, disallowed
        )
    else:
        # Back-compat default for callers that do not supply an explicit
        # mutation_class ground truth (e.g. isolated unit-test fixtures
        # unrelated to AC4/PERMISSION_BOUNDARY_001). This fallback is
        # never exercised for the real checked-in
        # expected-runtime-contract.json, where every shared_claude_runtime
        # agent declares an explicit `mutation_class` (AC4).
        facts.mutation_boundary = CLAUDE_PERMISSION_LEVEL_MAP.get(permission_mode, "unknown")
        facts.mutation_class_contradiction = None

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
    facts.delegation_intent_hint = (
        "blocked" if facts.nested_delegation_blocked is True
        else "allowed" if facts.nested_delegation_blocked is False
        else "unknown"
    )
    facts.delegation_intent_evidence = facts.nested_delegation_evidence

    # Model declaration (advisory). model is treated as a Claude-side
    # model_alias (AC5/AC6); semantic_model_class buckets it into a coarse
    # role class instead of comparing concrete provider model IDs.
    model = str(fm.get("model", ""))
    facts.model_declaration = f"config: model_alias={model} (advisory, not runtime proof)"
    facts.semantic_model_class = SEMANTIC_MODEL_CLASS_MAP.get(model, "unknown")
    effort = fm.get("effort")
    if effort:
        facts.reasoning_effort_declaration = (
            f"config: effort={effort} (advisory, not runtime proof)"
        )
        facts.effort_declared = str(effort)
    else:
        facts.reasoning_effort_declaration = (
            "config: effort not declared in Claude frontmatter (advisory)"
        )
        facts.effort_declared = None

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
    if agent_name == "scope-rollup-runner":
        declared = extract_runtime_field(instructions, "MUTATION_BOUNDARY")
        facts.mutation_boundary = declared or "unknown"

    # Delegation prose is advisory only. It must not become strict parity or
    # runtime-capability authority.
    try:
        config = read_toml(CODEX_CONFIG_PATH)
        features = config.get("features", {})
        multi_agent_v2 = features.get("multi_agent_v2") if isinstance(features, dict) else None
        v2_enabled = (
            isinstance(multi_agent_v2, dict)
            and type(multi_agent_v2.get("enabled")) is bool
            and multi_agent_v2["enabled"] is True
        )
        facts.delegation_intent_hint = classify_delegation_intent_hint(instructions)
        if v2_enabled:
            facts.nested_delegation_blocked = None
            facts.nested_delegation_evidence = (
                "[features.multi_agent_v2].enabled=True; delegation capability is not "
                "proven by developer_instructions"
            )
        else:
            facts.nested_delegation_blocked = None
            facts.nested_delegation_evidence = (
                "[features.multi_agent_v2].enabled is not strict boolean true; "
                "Codex nested-delegation state is unknown"
            )
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        facts.nested_delegation_blocked = None
        facts.nested_delegation_evidence = ".codex/config.toml could not be read"

    facts.delegation_intent_evidence = (
        "developer_instructions prose heuristic; advisory only, not strict authority"
    )

    # Model declaration (advisory). Codex model remains a concrete
    # provider model ID (required for check-codex-agents.mjs TOML
    # validation, out of Allowed Paths); semantic_model_class is derived
    # from the paired Claude model_alias by the caller, not here, since a
    # Codex TOML alone does not declare a Claude-side alias.
    model = str(codex_doc.get("model", ""))
    reasoning_effort = str(codex_doc.get("model_reasoning_effort", ""))
    facts.model_declaration = f"config: model={model} (advisory, not runtime proof)"
    facts.semantic_model_class = "unknown"
    facts.effort_declared = reasoning_effort or None
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

    B3: schema and permission drift are fail-level (returned as evidence). The
    caller promotes all returned drift to 'fail' status.

    delegation_intent_hint mismatch alone never emits NESTED_DELEGATION_001 and
    is never appended to the returned drift list (advisory-only, PR #1879
    af511e17, Issue #1948); it is reported separately via
    nested_delegation_report. This advisory-only scoping applies specifically
    to the delegation_intent_hint prose heuristic and does not extend to any
    future normalized delegation policy contract (e.g. Issue #1943).
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

    # scope-rollup has a producer/consumer marker rather than a compact
    # response schema.  Compare its required structural discriminators, not
    # merely the schema-name token.
    if agent_name == "scope-rollup-runner":
        claude_text = claude_path.read_text(encoding="utf-8")
        codex_text = codex_path.read_text(encoding="utf-8")
        structural_requirements = {
            "marker_schema_version": ("marker_schema_version: 3", "marker_schema_version: 3"),
            "query_schema_version": ("query_schema_version", "query_schema_version: 4"),
            "issues_completeness": ("issues_completeness", "issues_completeness"),
            "pull_requests_completeness": ("pull_requests_completeness", "pull_requests_completeness"),
            "transaction_budget": ("transaction_budget", "transaction_budget"),
            "structured_payload": ("payload:", "payload: {schema_version: 2}"),
            "result_sha256": ("result_sha256", "result_sha256"),
            "verify_status": ("verify_status", "verify_status: verified"),
        }
        for name, (claude_token, codex_token) in structural_requirements.items():
            if claude_token not in claude_text or codex_token not in codex_text:
                drifts.append(DriftEvidence(
                    rule_id="SCHEMA_STRUCTURE_PARITY_001",
                    file=str(codex_path),
                    line=find_line_number(codex_text, codex_token),
                    launcher="codex",
                    agent=agent_name,
                    expected=f"structural field {name}",
                    actual="missing",
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

    permission_comparison: bool | str = claude_facts.mutation_boundary == codex_facts.mutation_boundary
    if agent_name == "scope-rollup-runner":
        permission_comparison = "not_compared"
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
            "match": permission_comparison,
        },
        "RUNTIME_PROOF_NOTE": claude_facts.runtime_proof_note,
    }


def build_model_report(
    agent_name: str,
    claude_facts: AgentParityFacts,
    codex_facts: AgentParityFacts,
) -> dict:
    """Build model/reasoning_effort config declaration report (AC3, AC6).

    semantic_model_class (AC6): a coarse role-level bucket derived from the
    Claude-side model_alias (SEMANTIC_MODEL_CLASS_MAP), used instead of
    provider-specific model ID equality.

    effort_requirement (AC6): compares the Claude `effort` frontmatter
    declaration (when present) against the Codex `model_reasoning_effort`
    declaration. Advisory-only (config declaration, not runtime proof); a
    missing Claude `effort` field is reported as "not_declared" rather than
    treated as a hard failure, since AC1 requires only issue-reviewer.md to
    carry the field at minimum.
    """
    claude_effort = claude_facts.effort_declared
    codex_effort = codex_facts.effort_declared
    if claude_effort is None:
        effort_match: bool | str = "not_declared"
    else:
        effort_match = claude_effort == codex_effort
    return {
        "agent": agent_name,
        "model_declaration": {
            "claude": claude_facts.model_declaration,
            "codex": codex_facts.model_declaration,
        },
        "semantic_model_class": {
            "claude": claude_facts.semantic_model_class,
        },
        "reasoning_effort_declaration": {
            "claude": claude_facts.reasoning_effort_declaration,
            "codex": codex_facts.reasoning_effort_declaration,
        },
        "effort_requirement": {
            "claude": claude_effort,
            "codex": codex_effort,
            "match": effort_match,
        },
        "note": (
            "Model, semantic_model_class and reasoning_effort/effort_requirement "
            "are config-level declarations only. They are NOT runtime proof of "
            "actual model used."
        ),
    }


def format_text_report(
    all_drifts: list[DriftEvidence],
    permission_reports: list[dict],
    model_reports: list[dict],
    delegation_reports: list[dict],
    status: str,
    warn_evidence: list[DriftEvidence] | None = None,
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

    if warn_evidence:
        # AC6: effort_requirement mismatch is a real, printed warn-level
        # drift (participates in --strict), never silent.
        lines.append("WARN_DRIFT:")
        for d in warn_evidence:
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
        lines.append("    authority: advisory")
        lines.append(f"    claude_intent_hint: {dr['claude_intent_hint']} evidence={dr['claude_evidence']}")
        lines.append(f"    codex_intent_hint: {dr['codex_intent_hint']} evidence={dr['codex_evidence']}")
    lines.append("")

    return "\n".join(lines)


def resolve_shared_claude_runtime_agents(expectations: dict) -> list[str]:
    """Resolve the agent set for extended parity checks (AC6).

    Prefers the checked-in asset_classification block (shared_claude_runtime
    entries under .claude/agents/); falls back to "any required_agents entry
    that declares a non-null claude_agent_path" for fixtures that predate
    the classification block (e.g. isolated unit-test fixtures built via
    _write_minimal_contract in tests/test_agent_parity.py).
    """
    classification = expectations.get("asset_classification")
    if classification:
        agents = {
            Path(path).stem
            for path, cls in classification.items()
            if cls == "shared_claude_runtime"
            and path.startswith(".claude/agents/")
            and path.endswith(".md")
        }
        if agents:
            return sorted(agents)
    return sorted(
        name
        for name, exp in expectations.get("required_agents", {}).items()
        if exp.get("claude_agent_path")
    )


def check_asset_classification_complete(
    classification: dict,
    claude_agent_dir: Path,
    codex_agent_dir: Path,
) -> list[str]:
    """AC2/AC9: every discovered .claude/agents/*.md and .codex/agents/*.toml
    asset must have a classification entry with a value in
    ALLOWED_CLASSIFICATIONS. Returns a list of failure strings (empty when
    the inventory is complete)."""
    failures: list[str] = []
    discovered: list[str] = []
    if claude_agent_dir.is_dir():
        discovered.extend(
            f".claude/agents/{p.name}" for p in sorted(claude_agent_dir.glob("*.md"))
        )
    if codex_agent_dir.is_dir():
        discovered.extend(
            f".codex/agents/{p.name}" for p in sorted(codex_agent_dir.glob("*.toml"))
        )
    for rel_path in discovered:
        cls = classification.get(rel_path)
        if cls is None:
            failures.append(f"asset_classification: {rel_path} is unclassified")
        elif cls not in ALLOWED_CLASSIFICATIONS:
            failures.append(
                f"asset_classification: {rel_path} has unknown classification {cls!r}"
                f" (expected one of {sorted(ALLOWED_CLASSIFICATIONS)!r})"
            )
    return failures


def check_asset_classification_pairing(
    classification: dict,
    required_agents: dict,
) -> list[str]:
    """AC2/AC9 (human PR reviewer P1 blocker, PR #2334 comment 5401806450):
    validate pairing invariants across the asset_classification inventory,
    not merely that a discovered file has *a* valid classification value.
    A mis-classified should-be-shared agent silently drops out of
    resolve_shared_claude_runtime_agents() extended-parity scope, defeating
    AC6's full-dimension verification. Checks:

    (a) every `.claude/agents/<stem>.md` classified `shared_claude_runtime`
        has a same-stem `.codex/agents/<stem>.toml` classified
        `legacy_codex_projection`, and vice versa;
    (b) every classification manifest entry points at a filesystem path
        that actually exists in this checkout;
    (c) classification entries correspond 1:1 with `required_agents`: every
        `.claude/agents/*.md` classified `shared_claude_runtime` or
        `claude_only` has a matching `required_agents` entry (by stem), and
        every `required_agents` entry whose `claude_agent_path` is set has
        a matching classification entry.
    """
    failures: list[str] = []

    shared_claude = {
        Path(p).stem: p for p, c in classification.items()
        if c == "shared_claude_runtime" and p.startswith(".claude/agents/") and p.endswith(".md")
    }
    legacy_projection = {
        Path(p).stem: p for p, c in classification.items()
        if c == "legacy_codex_projection" and p.startswith(".codex/agents/") and p.endswith(".toml")
    }
    for stem, path in shared_claude.items():
        if stem not in legacy_projection:
            failures.append(
                f"asset_classification pairing: {path} is classified "
                f"shared_claude_runtime but .codex/agents/{stem}.toml has no "
                f"matching legacy_codex_projection classification entry"
            )
    for stem, path in legacy_projection.items():
        if stem not in shared_claude:
            failures.append(
                f"asset_classification pairing: {path} is classified "
                f"legacy_codex_projection but .claude/agents/{stem}.md has no "
                f"matching shared_claude_runtime classification entry"
            )

    # (b) every classified path must exist on disk.
    for rel_path in classification:
        if not (REPO_ROOT / rel_path).exists():
            failures.append(
                f"asset_classification: {rel_path} is classified but does not "
                f"exist on disk (stale entry)"
            )

    # (c) 1:1 correspondence with required_agents. Only `shared_claude_runtime`
    # entries participate in Codex parity (`required_agents`); `claude_only`
    # agents have no Codex TOML counterpart by definition and are correctly
    # absent from required_agents.
    classified_shared_stems = set(shared_claude.keys())
    required_claude_stems = {
        name for name, exp in required_agents.items() if exp.get("claude_agent_path")
    }
    missing_from_required = classified_shared_stems - required_claude_stems
    missing_from_classification = required_claude_stems - classified_shared_stems
    for stem in sorted(missing_from_required):
        failures.append(
            f"asset_classification pairing: .claude/agents/{stem}.md is classified "
            f"shared_claude_runtime but has no matching required_agents entry"
        )
    for stem in sorted(missing_from_classification):
        failures.append(
            f"asset_classification pairing: required_agents.{stem} declares a "
            f"claude_agent_path but has no matching shared_claude_runtime "
            f"classification entry"
        )

    return failures


def check_duplicate_agent_names(claude_agent_dir: Path, codex_agent_dir: Path) -> list[str]:
    """AC7: duplicate agent `name:`/`name =` values within a launcher's
    agent directory are detected as errors."""
    failures: list[str] = []
    if claude_agent_dir.is_dir():
        seen: dict[str, Path] = {}
        for md_path in sorted(claude_agent_dir.glob("*.md")):
            fm = extract_frontmatter(md_path.read_text(encoding="utf-8"), source_path=str(md_path))
            name = fm.get("name")
            if not isinstance(name, str) or not name:
                continue
            if name in seen:
                failures.append(
                    f"duplicate Claude agent name {name!r}: {seen[name]} and {md_path}"
                )
            else:
                seen[name] = md_path
    if codex_agent_dir.is_dir():
        seen_codex: dict[str, Path] = {}
        for toml_path in sorted(codex_agent_dir.glob("*.toml")):
            try:
                doc = read_toml(toml_path)
            except (OSError, tomllib.TOMLDecodeError):
                continue
            name = doc.get("name")
            if not isinstance(name, str) or not name:
                continue
            if name in seen_codex:
                failures.append(
                    f"duplicate Codex agent name {name!r}: {seen_codex[name]} and {toml_path}"
                )
            else:
                seen_codex[name] = toml_path
    return failures


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
    # AC6: warn_evidence holds non-fail-level drift (currently just
    # EFFORT_REQUIREMENT_001 mismatch) that participates in --strict but
    # never causes an unconditional STATUS: fail.
    warn_evidence: list[DriftEvidence] = []
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
            claude_frontmatter = extract_frontmatter(claude_text, source_path=str(claude_path))
            if claude_frontmatter.get("name") != agent_name:
                failures.append(f"{expected['claude_agent_path']}: frontmatter name must be {agent_name}")
            if claude_frontmatter.get("model") != expected["model_alias"]:
                failures.append(
                    f"{expected['claude_agent_path']}: model_alias expected"
                    f" {expected['model_alias']!r} got {claude_frontmatter.get('model')!r}"
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
            and expected["runtime_followup_route"] != "none"
            and expected["runtime_followup_route"].split("|")[0] not in claude_text
        ):
            failures.append(
                f"{expected['claude_agent_path']}: expected route token"
                f" {expected['runtime_followup_route']!r} not found"
            )

        if not codex_only:
            if not excludes_permission_parity(agent_name, expected):
                permission_expected = (
                    "acceptEdits"
                    if expected["default_permissions"] == "loop-protocol-rtk"
                    else "dontAsk"
                )
                if agent_name == "post-merge-cleanup-worker":
                    permission_expected = "default"
                if claude_frontmatter.get("permissionMode") != permission_expected:
                    failures.append(
                        f"{expected['claude_agent_path']}: permissionMode must match"
                        f" Codex permission profile {expected['default_permissions']}"
                    )

    # --- Asset inventory checks (AC2, AC7, AC9): unclassified references
    # and duplicate agent names are hard failures, not silent skips. ---
    classification = expectations.get("asset_classification")
    if classification:
        failures.extend(
            check_asset_classification_complete(classification, claude_agent_dir, codex_agent_dir)
        )
        failures.extend(
            check_asset_classification_pairing(classification, expectations["required_agents"])
        )
    failures.extend(check_duplicate_agent_names(claude_agent_dir, codex_agent_dir))

    # --- Extended parity checks: role/permission/tool/output-schema/
    # semantic-model-class/effort-requirement (AC6). The former 4-agent
    # PARITY_AGENTS allowlist has been removed; the agent set is now
    # resolved from the asset inventory classification (shared_claude_runtime
    # entries) when available, falling back to "any required_agents entry
    # that declares a claude_agent_path" for isolated/unit-test fixtures
    # that do not carry an asset_classification block.
    for agent_name in resolve_shared_claude_runtime_agents(expectations):
        expected = expectations["required_agents"].get(agent_name)
        if expected is None:
            continue

        codex_path = codex_agent_dir / f"{agent_name}.toml"
        claude_path = claude_agent_dir / f"{agent_name}.md"

        if not codex_path.exists() or not claude_path.exists():
            continue  # already reported in legacy checks

        codex_doc = read_toml(codex_path)
        claude_text = claude_path.read_text(encoding="utf-8")

        claude_facts = extract_claude_facts(
            agent_name, claude_path, claude_text,
            declared_mutation_class=expected.get("mutation_class"),
        )
        codex_facts = extract_codex_facts(agent_name, codex_path, codex_doc)

        # AC4: an explicit declared-vs-tool-allowlist contradiction is a
        # hard failure (never inferred from permissionMode or from Bash
        # presence alone -- see resolve_mutation_class()).
        if claude_facts.mutation_class_contradiction:
            failures.append(
                f"{expected.get('claude_agent_path', claude_path)}: "
                f"{claude_facts.mutation_class_contradiction}"
            )

        drifts = compare_parity(
            agent_name,
            claude_path,
            codex_path,
            claude_facts,
            codex_facts,
            compare_permission=not excludes_permission_parity(agent_name, expected),
        )
        all_drifts.extend(drifts)

        permission_reports.append(build_permission_report(agent_name, claude_facts, codex_facts))
        model_report = build_model_report(agent_name, claude_facts, codex_facts)
        model_reports.append(model_report)
        # AC6 (human PR reviewer P1 blocker, PR #2334 comment 5401806450):
        # effort_requirement mismatch is a real warn-level drift that
        # participates in --strict, not silent report-only STATUS: ok.
        # Static declaration comparison only, not runtime proof (see
        # build_model_report docstring / effort_requirement note).
        if model_report["effort_requirement"]["match"] is False:
            warn_evidence.append(DriftEvidence(
                rule_id="EFFORT_REQUIREMENT_001",
                file=str(claude_path),
                line=find_line_number(claude_text, "effort"),
                launcher="claude",
                agent=agent_name,
                expected=str(model_report["effort_requirement"]["codex"]),
                actual=str(model_report["effort_requirement"]["claude"]),
            ))
        delegation_reports.append({
            "agent": agent_name,
            "claude_intent_hint": claude_facts.delegation_intent_hint,
            "claude_evidence": claude_facts.delegation_intent_evidence,
            "codex_intent_hint": codex_facts.delegation_intent_hint,
            "codex_evidence": codex_facts.delegation_intent_evidence,
        })

    # --- Determine overall status ---
    # B3: schema / permission drifts are fail-level. delegation_intent_hint
    # mismatch alone is advisory-only and is never added to all_drifts, so it
    # cannot affect status here.
    # AC6: effort_requirement mismatch (warn_evidence) is a real, non-silent
    # STATUS: warn drift that participates in `--strict` -- it never
    # escalates to an unconditional STATUS: fail on its own.
    if failures or all_drifts:
        status = "fail"
    elif warn_evidence:
        status = "warn"
    else:
        status = "ok"

    if args.format == "json":
        result = {
            "STATUS": status,
            "failures": failures,
            "drift": [d.to_dict() for d in all_drifts],
            "warn_drift": [d.to_dict() for d in warn_evidence],
            "permission_report": permission_reports,
            "model_declaration_report": model_reports,
            "nested_delegation_report": delegation_reports,
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        report = format_text_report(
            all_drifts, permission_reports, model_reports, delegation_reports, status,
            warn_evidence=warn_evidence,
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
