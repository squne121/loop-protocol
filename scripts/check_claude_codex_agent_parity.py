#!/usr/bin/env python3
"""Validate structural self-consistency of the Claude agent (`.claude/agents/*.md`)
runtime contract.

Provider-neutral checker (Issue #2161 AC13): native Codex CLI has been
retired from this repository, so this script no longer compares
`.claude/agents/*.md` against the retired native Codex CLI `agents/*.toml`
counterpart. It now
validates `.claude/agents/*.md` frontmatter/instructions against the
declared ground truth in `expected-runtime-contract.json` on its own:

- frontmatter name / model_alias / permissionMode / non-empty tools list
- `mutation_class` declared ground truth vs. tool-allowlist contradiction
  (see `resolve_mutation_class()`)
- `runtime_followup_route` token presence in the agent's instructions
- asset_classification completeness (every `.claude/agents/*.md` is
  classified) and its 1:1 correspondence with `required_agents`
- duplicate agent `name:` values across `.claude/agents/*.md`

This checker predates Issue #2161; historically it also compared against
Codex agent TOML files (removed native Codex CLI executable consumer, see
Issue #2161 and parent #2154).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTATION_PATH = REPO_ROOT / "tests/fixtures/agent-config/expected-runtime-contract.json"

# Permission profiles -> mutation boundary mapping (fallback only; the
# declared ground truth is `required_agents.<agent>.mutation_class`, see
# resolve_mutation_class()).
# Claude permissionMode -> a purely *informational* prompt-handling policy
# label. This is deliberately NOT used to derive `mutation_class` (Issue
# #2160 AC4, human PR-review 2026-08-25 P0 blocker, PR #2334 comment
# 5401806450): `permissionMode` describes whether Claude Code asks before
# running a tool, independent of the agent's actual tool allowlist and the
# mutation boundary of any skill the agent invokes. `mutation_class` is
# therefore an explicit, per-agent *declared ground truth* (see
# `required_agents.<agent>.mutation_class` in expected-runtime-contract.json),
# resolved by `resolve_mutation_class()` below, with `permissionMode`
# reported alongside it purely as an informational, independent axis.
CLAUDE_PERMISSION_LEVEL_MAP = {
    "dontAsk": "readonly",
    "acceptEdits": "issue-mutation",
    "default": "repo-write",
}

# Tools whose mere presence in an *allowed* tool set is direct evidence of
# repo-mutation capability, regardless of permissionMode.
MUTATION_CAPABLE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Tool alias normalization applied before any tool-set comparison.
# "Task" is a historical/alternate name for the SubAgent-delegation tool
# that this repository canonically calls "Agent".
TOOL_ALIAS_NORMALIZE = {"Task": "Agent"}

# Semantic model class: a coarse, repository-local classification of a
# Claude model_alias, used for role-level reporting.
SEMANTIC_MODEL_CLASS_MAP = {
    "haiku": "fast_cheap",
    "sonnet": "balanced",
    "opus": "deep_reasoning",
}

# Asset inventory classification enum.
ALLOWED_CLASSIFICATIONS = {
    "shared_claude_runtime",
    "claude_only",
    "experimental",
}


class FrontmatterParseError(ValueError):
    """Raised when Claude agent frontmatter contains unsupported YAML syntax.

    Unsupported syntax must fail loudly, never silently skip.
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

    (human PR-review 2026-08-25 P0 blocker, PR #2334 comment 5401806450):
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


class DriftEvidence:
    def __init__(
        self,
        rule_id: str,
        file: str,
        line: int,
        agent: str,
        expected: str,
        actual: str,
    ) -> None:
        self.rule_id = rule_id
        self.file = file
        self.line = line
        self.agent = agent
        self.expected = expected
        self.actual = actual

    def __repr__(self) -> str:
        return (
            f"DriftEvidence(rule_id={self.rule_id!r}, file={self.file!r}, "
            f"line={self.line!r}, agent={self.agent!r}, "
            f"expected={self.expected!r}, actual={self.actual!r})"
        )

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "file:line": f"{self.file}:{self.line}",
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
        model_declaration: str | None = None,
        reasoning_effort_declaration: str | None = None,
        semantic_model_class: str = "unknown",
        effort_declared: str | None = None,
        claude_tools: list[str] | None = None,
        claude_disallowed_tools: list[str] | None = None,
        mutation_class_contradiction: str | None = None,
        # nested_delegation_blocked: True = blocked, False = allowed,
        # None = unknown (no tools key)
        nested_delegation_blocked: bool | None = None,
        nested_delegation_evidence: str = "",
    ) -> None:
        self.agent_name = agent_name
        self.final_output_schema = final_output_schema
        self.artifact_only_schema_names: list[str] = (
            artifact_only_schema_names if artifact_only_schema_names is not None else []
        )
        self.declared_permission = declared_permission
        self.mutation_boundary = mutation_boundary
        self.nested_delegation_blocked: bool | None = nested_delegation_blocked
        self.nested_delegation_evidence = nested_delegation_evidence
        self.model_declaration = model_declaration
        self.reasoning_effort_declaration = reasoning_effort_declaration
        self.semantic_model_class = semantic_model_class
        self.effort_declared = effort_declared
        self.claude_tools: list[str] = claude_tools if claude_tools is not None else []
        self.claude_disallowed_tools: list[str] = claude_disallowed_tools if claude_disallowed_tools is not None else []
        self.mutation_class_contradiction = mutation_class_contradiction

    def __repr__(self) -> str:
        return (
            f"AgentParityFacts(agent_name={self.agent_name!r}, "
            f"final_output_schema={self.final_output_schema!r}, "
            f"mutation_boundary={self.mutation_boundary!r})"
        )


def load_expectations() -> dict:
    return json.loads(EXPECTATION_PATH.read_text(encoding="utf-8"))


def extract_frontmatter(text: str, source_path: str = "<unknown>") -> dict[str, object]:
    """Parse Claude agent markdown frontmatter using real YAML (yaml.safe_load).

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


def find_line_number(text: str, search: str | None) -> int:
    """Return 1-based line number of first occurrence of search in text, or 0 if not found.

    Returns 0 if search is None or empty string to avoid false line 1 matches.
    """
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
    m = re.search(r"出力契約[（(]([A-Z][A-Z0-9_]+_V\d+)[）)]", text)
    if m:
        return m.group(1)
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
    """
    artifact_only: list[str] = []

    for m in re.finditer(r"artifact\s+(?:only|のみ)[:\s]+[`']?([A-Z][A-Z0-9_]+_V\d+)[`']?", text, re.IGNORECASE):
        name = m.group(1)
        if name not in artifact_only:
            artifact_only.append(name)

    for m in re.finditer(
        r"(?:^|\n)#{1,6}\s+(?:内部処理用|artifact[- ]only)[^\n]*?"
        r"([A-Z][A-Z0-9_]+_V\d+)[^\n]*?(?:artifact[- ]?(?:only|のみ)|内部処理用|のみ)",
        text,
        re.IGNORECASE,
    ):
        name = m.group(1)
        if name not in artifact_only:
            artifact_only.append(name)

    for m in re.finditer(
        r"(?:^|\n)#{1,6}[^\n]*?内部処理用\s+([A-Z][A-Z0-9_]+_V\d+)",
        text,
    ):
        name = m.group(1)
        if name not in artifact_only:
            artifact_only.append(name)

    m = re.search(r"出力契約[（(][^）)]*artifact[_\s]only[:\s]+([A-Z][A-Z0-9_]+_V\d+)", text)
    if m:
        name = m.group(1)
        if name not in artifact_only:
            artifact_only.append(name)

    return artifact_only


def extract_claude_facts(
    agent_name: str,
    claude_path: Path,
    claude_text: str,
    declared_mutation_class: str | None = None,
) -> AgentParityFacts:
    """Extract AgentParityFacts from a Claude agent markdown file.

    `declared_mutation_class` is the per-agent ground-truth value from
    `required_agents.<agent>.mutation_class` in
    expected-runtime-contract.json. When omitted (e.g. isolated unit tests
    that do not construct a fixture entry), it defaults to None and
    `mutation_boundary` resolves to "unknown" rather than being guessed from
    permissionMode.
    """
    facts = AgentParityFacts(agent_name=agent_name)
    fm = extract_frontmatter(claude_text, source_path=str(claude_path))

    facts.final_output_schema = extract_final_output_schema_from_claude(claude_text)
    facts.artifact_only_schema_names = extract_artifact_only_schemas_from_claude(
        claude_text, facts.final_output_schema
    )

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

    # Nested delegation — 3-value logic.
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

    permission_mode = str(fm.get("permissionMode", ""))
    facts.declared_permission = f"claude.permissionMode={permission_mode}"
    if declared_mutation_class is not None:
        facts.mutation_boundary, facts.mutation_class_contradiction = resolve_mutation_class(
            declared_mutation_class, tools, disallowed
        )
    else:
        facts.mutation_boundary = CLAUDE_PERMISSION_LEVEL_MAP.get(permission_mode, "unknown")
        facts.mutation_class_contradiction = None

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


def format_text_report(
    permission_reports: list[dict],
    model_reports: list[dict],
    status: str,
) -> str:
    lines: list[str] = []
    lines.append(f"STATUS: {status}")
    lines.append("")

    lines.append("PERMISSION_REPORT:")
    for pr in permission_reports:
        lines.append(f"  agent: {pr['agent']}")
        lines.append(f"    DECLARED_PERMISSION: {pr['declared_permission']}")
        lines.append(f"    MUTATION_BOUNDARY: {pr['mutation_boundary']}")
    lines.append("")

    lines.append("MODEL_DECLARATION_REPORT:")
    for mr in model_reports:
        lines.append(f"  agent: {mr['agent']}")
        lines.append(f"    model_declaration: {mr['model_declaration']}")
        lines.append(f"    semantic_model_class: {mr['semantic_model_class']}")
        lines.append(f"    reasoning_effort_declaration: {mr['reasoning_effort_declaration']}")
    lines.append("")

    return "\n".join(lines)


def resolve_shared_claude_runtime_agents(expectations: dict) -> list[str]:
    """Resolve the agent set for the extended structural checks.

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
) -> list[str]:
    """Every discovered .claude/agents/*.md asset must have a classification
    entry with a value in ALLOWED_CLASSIFICATIONS. Returns a list of
    failure strings (empty when the inventory is complete)."""
    failures: list[str] = []
    discovered: list[str] = []
    if claude_agent_dir.is_dir():
        discovered.extend(
            f".claude/agents/{p.name}" for p in sorted(claude_agent_dir.glob("*.md"))
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
    """Validate pairing invariants across the asset_classification inventory:

    (a) every classification manifest entry points at a filesystem path
        that actually exists in this checkout;
    (b) classification entries correspond 1:1 with `required_agents`: every
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

    for rel_path in classification:
        if not (REPO_ROOT / rel_path).exists():
            failures.append(
                f"asset_classification: {rel_path} is classified but does not "
                f"exist on disk (stale entry)"
            )

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


def check_duplicate_agent_names(claude_agent_dir: Path) -> list[str]:
    """Duplicate agent `name:` values within `.claude/agents/` are errors."""
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
        help="No-op compatibility flag (retained for existing callers).",
    )
    # Allow overriding paths for testing
    parser.add_argument("--claude-agent-dir", type=Path, default=None)
    parser.add_argument("--expectation-path", type=Path, default=None,
                        help="Override path to expected-runtime-contract.json (for testing)")
    # parse_known_args: tolerate unrelated args from exec()-style callers.
    args, _ = parser.parse_known_args(argv)

    claude_agent_dir = args.claude_agent_dir or (REPO_ROOT / ".claude/agents")
    expectation_path = args.expectation_path or EXPECTATION_PATH

    expectations = json.loads(expectation_path.read_text(encoding="utf-8"))
    failures: list[str] = []
    permission_reports: list[dict] = []
    model_reports: list[dict] = []

    # --- Per-agent structural checks against the declared ground truth ---
    for agent_name, expected in expectations.get("required_agents", {}).items():
        if args.claude_agent_dir:
            claude_path = claude_agent_dir / f"{agent_name}.md"
        else:
            claude_agent_path = expected.get("claude_agent_path")
            if not claude_agent_path:
                continue
            claude_path = REPO_ROOT / claude_agent_path

        if not claude_path.exists():
            failures.append(f"missing claude agent file: {claude_path}")
            continue

        claude_text = claude_path.read_text(encoding="utf-8")
        claude_frontmatter = extract_frontmatter(claude_text, source_path=str(claude_path))
        if claude_frontmatter.get("name") != agent_name:
            failures.append(f"{claude_path}: frontmatter name must be {agent_name}")
        if "model_alias" in expected and claude_frontmatter.get("model") != expected["model_alias"]:
            failures.append(
                f"{claude_path}: model_alias expected"
                f" {expected['model_alias']!r} got {claude_frontmatter.get('model')!r}"
            )
        if "claude_permission_mode" in expected and claude_frontmatter.get("permissionMode") != expected["claude_permission_mode"]:
            failures.append(
                f"{claude_path}: permissionMode expected"
                f" {expected['claude_permission_mode']!r}"
                f" got {claude_frontmatter.get('permissionMode')!r}"
            )

        tools = claude_frontmatter.get("tools", [])
        if not isinstance(tools, list) or not tools:
            failures.append(f"{claude_path}: tools frontmatter list is required")

        route = expected.get("runtime_followup_route")
        if route and route != "none" and route.split("|")[0] not in claude_text:
            failures.append(
                f"{claude_path}: expected route token {route!r} not found"
            )

    # --- Asset inventory checks: unclassified references and duplicate
    # agent names are hard failures, not silent skips. ---
    classification = expectations.get("asset_classification")
    if classification:
        failures.extend(
            check_asset_classification_complete(classification, claude_agent_dir)
        )
        failures.extend(
            check_asset_classification_pairing(classification, expectations.get("required_agents", {}))
        )
    failures.extend(check_duplicate_agent_names(claude_agent_dir))

    # --- Extended structural checks: mutation_class contradiction /
    # model+effort declaration report, for the shared_claude_runtime agent
    # set resolved from the asset inventory classification. ---
    for agent_name in resolve_shared_claude_runtime_agents(expectations):
        expected = expectations.get("required_agents", {}).get(agent_name)
        if expected is None:
            continue

        claude_path = claude_agent_dir / f"{agent_name}.md"
        if not claude_path.exists():
            continue  # already reported above

        claude_text = claude_path.read_text(encoding="utf-8")
        claude_facts = extract_claude_facts(
            agent_name, claude_path, claude_text,
            declared_mutation_class=expected.get("mutation_class"),
        )

        if claude_facts.mutation_class_contradiction:
            failures.append(
                f"{expected.get('claude_agent_path', claude_path)}: "
                f"{claude_facts.mutation_class_contradiction}"
            )

        permission_reports.append({
            "agent": agent_name,
            "declared_permission": claude_facts.declared_permission,
            "mutation_boundary": claude_facts.mutation_boundary,
        })
        model_reports.append({
            "agent": agent_name,
            "model_declaration": claude_facts.model_declaration,
            "semantic_model_class": claude_facts.semantic_model_class,
            "reasoning_effort_declaration": claude_facts.reasoning_effort_declaration,
        })

    status = "fail" if failures else "ok"

    if args.format == "json":
        result = {
            "STATUS": status,
            "failures": failures,
            "permission_report": permission_reports,
            "model_declaration_report": model_reports,
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        report = format_text_report(permission_reports, model_reports, status)
        print(report)
        if failures:
            for f in failures:
                print(f"[FAIL] {f}")

    return 1 if status == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
