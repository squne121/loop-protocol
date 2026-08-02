"""Issue #1734: issue-creator / issue-editor split — frontmatter and body contract tests.

Verifies AC1-AC4, AC8-AC10 from the live Issue #1734 contract:
- AC1/AC2: `tools` frontmatter is the exact set {Read, Bash} (no `Skill`) for
  issue-creator.md / issue-editor.md
- AC3/AC4: `skills` frontmatter is the exact set {create-issue} / {edit-issue}
- AC8: both agent bodies document the controlled-executor procedural contract
  and do not claim a technical Bash-tool-level denial of raw `gh issue`
  mutation
- AC9: both agent bodies document that nested Skill invocation is
  structurally impossible (no `Skill` tool) and is a distinct concept from
  nested SubAgent invocation (e.g. issue-contract-fixer, Issue #998)
- AC10: issue-editor.md retains the #995 FAIL_CLOSED_REWRITE_CONSTRAINTS_V1
  contract and documents the #1909 context_bundle_path forward-compat input
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
ISSUE_CREATOR_PATH = AGENTS_DIR / "issue-creator.md"
ISSUE_EDITOR_PATH = AGENTS_DIR / "issue-editor.md"


def _read_frontmatter(path: Path) -> dict:
    """Strict YAML frontmatter parser: split on the first `---`/`---` pair."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: file must start with YAML frontmatter fence"
    _, _, remainder = text.partition("---\n")
    frontmatter, sep, _body = remainder.partition("\n---\n")
    assert sep, f"{path}: closing frontmatter fence not found"
    data = yaml.safe_load(frontmatter)
    assert isinstance(data, dict), f"{path}: frontmatter did not parse to a mapping"
    return data


def _read_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    _, _, remainder = text.partition("---\n")
    _frontmatter, sep, body = remainder.partition("\n---\n")
    assert sep, f"{path}: closing frontmatter fence not found"
    return body


def test_issue_creator_frontmatter_tools_exact_set():
    """AC1: issue-creator.md tools is the exact set {Read, Bash}, no Skill."""
    fm = _read_frontmatter(ISSUE_CREATOR_PATH)
    tools = fm.get("tools")
    assert isinstance(tools, list), "tools must be a list"
    assert set(tools) == {"Read", "Bash"}, f"tools must be exact set {{Read, Bash}}, got {tools}"
    assert "Skill" not in tools


def test_issue_editor_frontmatter_tools_exact_set():
    """AC2: issue-editor.md tools is the exact set {Read, Bash}, no Skill."""
    fm = _read_frontmatter(ISSUE_EDITOR_PATH)
    tools = fm.get("tools")
    assert isinstance(tools, list), "tools must be a list"
    assert set(tools) == {"Read", "Bash"}, f"tools must be exact set {{Read, Bash}}, got {tools}"
    assert "Skill" not in tools


def test_issue_creator_skills_exact_set():
    """AC3: issue-creator.md skills is the exact set [create-issue]."""
    fm = _read_frontmatter(ISSUE_CREATOR_PATH)
    skills = fm.get("skills")
    assert isinstance(skills, list), "skills must be a list"
    assert set(skills) == {"create-issue"}, f"skills must be exact set [create-issue], got {skills}"


def test_issue_editor_skills_exact_set():
    """AC4: issue-editor.md skills is the exact set [edit-issue]."""
    fm = _read_frontmatter(ISSUE_EDITOR_PATH)
    skills = fm.get("skills")
    assert isinstance(skills, list), "skills must be a list"
    assert set(skills) == {"edit-issue"}, f"skills must be exact set [edit-issue], got {skills}"


def test_both_agents_document_controlled_executor_procedural_contract():
    """AC8: both agents document controlled-executor mutation as procedural
    (not technically enforced), and do not claim Bash-tool-level denial of
    raw `gh issue create/edit/comment`.
    """
    creator_body = _read_body(ISSUE_CREATOR_PATH)
    editor_body = _read_body(ISSUE_EDITOR_PATH)

    assert "create_issue_txn.py" in creator_body
    assert "procedural contract" in creator_body
    assert "技術的な強制ではない" in creator_body or "技術的に拒否するとは主張しない" in creator_body

    assert "edit_issue_txn.py" in editor_body
    assert "procedural contract" in editor_body
    assert "技術的な強制ではない" in editor_body or "技術的に拒否するとは主張しない" in editor_body


def test_both_agents_document_nested_invocation_distinction():
    """AC9: both agents document that nested Skill invocation is structurally
    impossible (no Skill tool) and is distinct from nested SubAgent
    invocation (e.g. issue-contract-fixer, #998), which is not blocked.
    """
    creator_body = _read_body(ISSUE_CREATOR_PATH)
    editor_body = _read_body(ISSUE_EDITOR_PATH)

    for body in (creator_body, editor_body):
        assert "nested Skill invocation" in body
        assert "構造的に不可能" in body
        assert "nested SubAgent invocation" in body
        assert "issue-contract-fixer" in body
        assert "#998" in body


def test_issue_editor_retains_fail_closed_rewrite_and_context_bundle_contracts():
    """AC10: issue-editor.md retains the #995 FAIL_CLOSED_REWRITE_CONSTRAINTS_V1
    contract and documents the #1909 context_bundle_path input.
    """
    editor_body = _read_body(ISSUE_EDITOR_PATH)

    assert "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1" in editor_body
    assert "#995" in editor_body
    assert "context_bundle_path" in editor_body
    assert "#1909" in editor_body
