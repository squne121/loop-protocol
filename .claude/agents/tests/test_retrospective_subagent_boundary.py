#!/usr/bin/env python3
"""Boundary tests for the agent-retrospective leaf SubAgent definitions
(Issue #2237): `.claude/agents/retrospective-runtime-observer.md` and
`.claude/agents/retrospective-evaluator.md`.

Pure frontmatter-shape assertions (Runtime Verification Applicability:
deferred) -- no Agent tool is ever invoked; this parses each SubAgent
Markdown file's YAML frontmatter block and asserts static properties.

Covers:
  AC3  runtime_observer_no_skill_tool
  AC4  evaluator_no_skill_tool
  AC5  no_agent_tool
  AC12 leaf_frontmatter_contract
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_AGENTS_DIR = Path(__file__).resolve().parents[1]

_RUNTIME_OBSERVER_PATH = _AGENTS_DIR / "retrospective-runtime-observer.md"
_EVALUATOR_PATH = _AGENTS_DIR / "retrospective-evaluator.md"


def _parse_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} does not start with a YAML frontmatter block"
    _, _, rest = text.partition("---\n")
    frontmatter_text, _, _body = rest.partition("\n---\n")
    parsed = yaml.safe_load(frontmatter_text)
    assert isinstance(parsed, dict)
    return parsed


@pytest.fixture(scope="module")
def runtime_observer_frontmatter() -> dict[str, Any]:
    return _parse_frontmatter(_RUNTIME_OBSERVER_PATH)


@pytest.fixture(scope="module")
def evaluator_frontmatter() -> dict[str, Any]:
    return _parse_frontmatter(_EVALUATOR_PATH)


def _tools_and_disallowed(frontmatter: dict[str, Any]) -> tuple[list[str], list[str]]:
    tools = frontmatter.get("tools") or []
    disallowed = frontmatter.get("disallowedTools") or []
    return tools, disallowed


# ---------------------------------------------------------------------------
# AC3: retrospective-runtime-observer.md exists, tools excludes Skill
# ---------------------------------------------------------------------------


def test_runtime_observer_no_skill_tool_file_exists() -> None:
    assert _RUNTIME_OBSERVER_PATH.is_file()


def test_runtime_observer_no_skill_tool(runtime_observer_frontmatter: dict[str, Any]) -> None:
    tools, _disallowed = _tools_and_disallowed(runtime_observer_frontmatter)
    assert "Skill" not in tools


# ---------------------------------------------------------------------------
# AC4: retrospective-evaluator.md exists, tools excludes Skill
# ---------------------------------------------------------------------------


def test_evaluator_no_skill_tool_file_exists() -> None:
    assert _EVALUATOR_PATH.is_file()


def test_evaluator_no_skill_tool(evaluator_frontmatter: dict[str, Any]) -> None:
    tools, _disallowed = _tools_and_disallowed(evaluator_frontmatter)
    assert "Skill" not in tools


# ---------------------------------------------------------------------------
# AC5: both SubAgents exclude Agent from `tools` (not via `Agent(name)`
# allowlist syntax -- full exclusion or disallowedTools)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [_RUNTIME_OBSERVER_PATH, _EVALUATOR_PATH])
def test_no_agent_tool(path: Path) -> None:
    frontmatter = _parse_frontmatter(path)
    tools, disallowed = _tools_and_disallowed(frontmatter)
    # Full exclusion from `tools` (not merely present with an allowlist
    # qualifier like "Agent(name1, name2)").
    assert not any(t == "Agent" or t.startswith("Agent(") for t in tools)
    # Belt-and-suspenders: also explicitly denied.
    assert "Agent" in disallowed


# ---------------------------------------------------------------------------
# AC12: leaf frontmatter contract -- no Write/Edit, explicit integer
# maxTurns, mcpServers/hooks/memory explicitly empty or unused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [_RUNTIME_OBSERVER_PATH, _EVALUATOR_PATH])
def test_leaf_frontmatter_contract_required_keys(path: Path) -> None:
    frontmatter = _parse_frontmatter(path)
    assert isinstance(frontmatter.get("name"), str) and frontmatter["name"]
    assert isinstance(frontmatter.get("description"), str) and frontmatter["description"]
    assert "tools" in frontmatter  # explicit allowlist, never omitted


@pytest.mark.parametrize("path", [_RUNTIME_OBSERVER_PATH, _EVALUATOR_PATH])
def test_leaf_frontmatter_contract_no_write_edit(path: Path) -> None:
    frontmatter = _parse_frontmatter(path)
    tools, disallowed = _tools_and_disallowed(frontmatter)
    for write_tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        assert write_tool not in tools
        assert write_tool in disallowed


@pytest.mark.parametrize("path", [_RUNTIME_OBSERVER_PATH, _EVALUATOR_PATH])
def test_leaf_frontmatter_contract_max_turns_is_concrete_int(path: Path) -> None:
    frontmatter = _parse_frontmatter(path)
    max_turns = frontmatter.get("maxTurns")
    assert isinstance(max_turns, int) and not isinstance(max_turns, bool)
    assert max_turns > 0


@pytest.mark.parametrize("path", [_RUNTIME_OBSERVER_PATH, _EVALUATOR_PATH])
def test_leaf_frontmatter_contract_mcp_hooks_memory_unused(path: Path) -> None:
    frontmatter = _parse_frontmatter(path)
    for key in ("mcpServers", "hooks", "memory"):
        # "明示的に空または不使用" -- either the key is absent (not used at
        # all) or, if present, it is explicitly empty (falsy).
        if key in frontmatter:
            assert not frontmatter[key], f"{path.name}: {key} must be empty if present"


@pytest.mark.parametrize("path", [_RUNTIME_OBSERVER_PATH, _EVALUATOR_PATH])
def test_leaf_frontmatter_contract_model_declared(path: Path) -> None:
    frontmatter = _parse_frontmatter(path)
    assert isinstance(frontmatter.get("model"), str) and frontmatter["model"]
