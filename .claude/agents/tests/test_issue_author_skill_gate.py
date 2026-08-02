"""Static contract tests for the issue-author Agent Skill gate (Issue #1734).

These tests parse `.claude/agents/issue-author.md` with a strict YAML
frontmatter parser and assert on the frontmatter contract (AC1/AC2) and on
the body content that must be present/absent after the thin-ification
(AC5/AC8/AC10). Runtime AC (AC3/AC7/AC9) are covered separately by
`scripts/agent-ops/run_worktree_agent_runtime_smoke.py` fresh-session smoke
runs -- this file intentionally does not fabricate runtime evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
AGENT_PATH = REPO_ROOT / ".claude" / "agents" / "issue-author.md"


def _read_agent_text() -> str:
    return AGENT_PATH.read_text(encoding="utf-8")


def _parse_frontmatter(text: str) -> dict:
    """Strict frontmatter parser: the document must start with a `---`
    delimited YAML block, parsed with `yaml.safe_load` (no custom regex
    heuristics). Raises if the delimiters are missing or the YAML block is
    not a mapping."""
    assert text.startswith("---\n"), "issue-author.md must start with a YAML frontmatter block"
    end = text.index("\n---\n", 4)
    raw_frontmatter = text[4:end]
    body = text[end + len("\n---\n") :]
    data = yaml.safe_load(raw_frontmatter)
    assert isinstance(data, dict), "frontmatter must parse to a YAML mapping"
    return data, body


@pytest.fixture(scope="module")
def agent_frontmatter_and_body():
    text = _read_agent_text()
    return _parse_frontmatter(text)


def test_frontmatter_tools_exact_set(agent_frontmatter_and_body):
    """AC1: tools is the exact set {Bash, Read, Skill}."""
    frontmatter, _ = agent_frontmatter_and_body
    tools = frontmatter.get("tools")
    assert isinstance(tools, list), "tools must be a YAML list"
    assert set(tools) == {"Bash", "Read", "Skill"}
    assert len(tools) == 3, "tools must not contain duplicates"


def test_dispatcher_allows_only_create_and_edit_skill(agent_frontmatter_and_body):
    """AC2: the agent-local deterministic dispatcher (the `skills:`
    frontmatter allowlist consumed natively by the Claude Code Skill tool
    gate) allows exactly {create-issue, edit-issue}."""
    frontmatter, _ = agent_frontmatter_and_body
    skills = frontmatter.get("skills")
    assert isinstance(skills, list), "skills must be a YAML list"
    assert set(skills) == {"create-issue", "edit-issue"}
    assert len(skills) == 2, "skills must not contain duplicates"


def test_agent_body_removes_duplicated_skill_procedures(agent_frontmatter_and_body):
    """AC5: the body no longer duplicates create-issue/edit-issue's own
    schema, validation, publish procedure, retry procedure, or terminal
    routing detail. The removed markers below are the exact strings that
    were present in the pre-#1734 body (direct transaction-helper script
    invocation duplicating edit-issue's own procedure)."""
    _, body = agent_frontmatter_and_body
    removed_markers = [
        # Direct script invocation procedure -- superseded by Skill tool use.
        "uv run --locked python3 .claude/skills/edit-issue/scripts/edit_issue_txn.py --input-file",
        "## 既存 Issue 更新フロー (Existing Issue Flow)",
        # Full readiness_forwarding_payload schema enumeration -- now a
        # one-line pointer to READINESS_FORWARDING_PAYLOAD_V1 instead.
        "## readiness_forwarding_payload 契約",
        "## fail-closed terminal result の確認項目",
    ]
    for marker in removed_markers:
        assert marker not in body, f"duplicated procedure marker still present: {marker!r}"

    # Positive: authoring role / routing / output contract must remain.
    retained_markers = [
        "## 結果ルーティング (Result Routing)",
        "## 出力契約（ISSUE_AUTHOR_RESULT_COMPACT_V1）",
        "## Create／Edit 選択条件",
    ]
    for marker in retained_markers:
        assert marker in body, f"required retained section missing: {marker!r}"


def test_agent_body_documents_create_edit_routing_condition(agent_frontmatter_and_body):
    """AC6: routing (Create vs Edit selection) is documented under the
    canonical heading."""
    _, body = agent_frontmatter_and_body
    assert "## Create／Edit 選択条件" in body


def test_unknown_skill_nested_skill_raw_cli_negative_expectations_documented(agent_frontmatter_and_body):
    """AC8: the body documents the negative-test expectations for unknown
    Skill, nested Skill invocation (distinct from nested SubAgent
    invocation), and raw CLI mutation. Runtime enforcement of the unknown
    Skill / nested Skill case is proven separately by AC3 runtime smoke;
    raw CLI mutation denial is proven by the existing shared classifier test
    referenced in AC4."""
    frontmatter, body = agent_frontmatter_and_body

    # unknown_skill / nested_skill_invocation: only create-issue/edit-issue
    # are declared, so any other Skill name (including a Skill invoking
    # another Skill) is outside the allowed set.
    skills = set(frontmatter.get("skills") or [])
    assert skills == {"create-issue", "edit-issue"}
    assert "未知 Skill" in body or "unknown" in body.lower()

    # raw_cli_mutation: the body must explicitly say raw gh issue
    # mutation subcommands (create/edit/comment etc.) are not the
    # production authority. The exact literal substrings "gh issue edit" /
    # "gh issue comment" / "gh api --method PATCH" / "gh api --method POST"
    # are deliberately NOT asserted here, because a separate cross-file
    # contract test (test_skill_and_issue_author_no_raw_existing_issue_
    # mutation_contract in .claude/skills/edit-issue/tests/
    # test_edit_issue_txn.py) asserts those exact substrings must NOT
    # appear anywhere in this agent body.
    assert "gh issue" in body
    assert "create" in body and "edit" in body


def test_body_retains_fail_closed_rewrite_and_context_bundle_contracts(agent_frontmatter_and_body):
    """AC10: thin-ification retains the #995 FAIL_CLOSED_REWRITE_CONSTRAINTS_V1
    forwarding contract and the #1909 context_bundle_path forward-compat
    input contract note."""
    _, body = agent_frontmatter_and_body
    assert "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1" in body
    assert "ISSUE_AUTHOR_RESULT_V1" in body
    assert "context_bundle_path" in body
    assert "#1909" in body


def test_nested_skill_vs_nested_subagent_invocation_distinguished(agent_frontmatter_and_body):
    """AC11: the distinction between "nested Skill invocation" (forbidden)
    and "nested SubAgent invocation" (a different concept, not forbidden by
    this agent, e.g. #998's issue-contract-fixer call) is explicit in the
    agent body/description."""
    text = _read_agent_text()
    assert "nested SubAgent invocation" in text
    assert "nested Skill invocation" in text


def test_disallowed_tools_still_blocks_agent_edit_multiedit_write(agent_frontmatter_and_body):
    """Regression: the pre-existing disallowedTools guard must survive the
    thin-ification (Agent/Edit/MultiEdit/Write must remain disallowed)."""
    frontmatter, _ = agent_frontmatter_and_body
    disallowed = frontmatter.get("disallowedTools")
    assert isinstance(disallowed, list)
    assert set(disallowed) == {"Agent", "Edit", "MultiEdit", "Write"}
