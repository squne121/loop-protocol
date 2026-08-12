#!/usr/bin/env python3
"""
test_issue_reviewer_contract_static.py — Issue #2049 AC9.

Static contract test: a read-only agent's `developer_instructions` MUST NOT
carry a workspace write requirement (a self-contradiction with
`default_permissions = "loop-protocol-readonly"`). Exercises
`run_root_review_pipeline.check_agent_is_read_only_advisory()` against:

  1. the real `.codex/agents/issue-reviewer.toml` (must have zero violations
     after Issue #2049's fix), and
  2. a synthetic read-only fixture that DOES carry a workspace write
     requirement (must be rejected -- proves the checker is not vacuously
     permissive).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
ISSUE_REVIEWER_TOML = ROOT / ".codex" / "agents" / "issue-reviewer.toml"
PIPELINE_SCRIPT = (
    ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_root_review_pipeline.py"
)


def _load_pipeline_module():
    spec = importlib.util.spec_from_file_location("run_root_review_pipeline", PIPELINE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("run_root_review_pipeline", module)
    spec.loader.exec_module(module)
    return module


_PIPELINE = _load_pipeline_module()


def test_real_issue_reviewer_toml_has_no_workspace_write_requirement():
    """GIVEN the real issue-reviewer.toml (read-only, post Issue #2049 fix)
    WHEN checked for a workspace write requirement
    THEN there are zero violations."""
    text = ISSUE_REVIEWER_TOML.read_text(encoding="utf-8")
    violations = _PIPELINE.check_agent_is_read_only_advisory(text)
    assert violations == [], f"unexpected workspace-write markers: {violations}"


def test_real_issue_reviewer_toml_declares_readonly_permissions():
    """GIVEN the real issue-reviewer.toml
    WHEN inspected for its permission profile
    THEN it still declares `loop-protocol-readonly` (Issue #2049 AC8)."""
    text = ISSUE_REVIEWER_TOML.read_text(encoding="utf-8")
    assert 'default_permissions = "loop-protocol-readonly"' in text


def test_contradictory_read_only_fixture_is_rejected():
    """GIVEN a synthetic read-only agent config whose instructions still
    require it to persist a full artifact / temp file (a self-contradiction)
    WHEN checked for a workspace write requirement
    THEN the checker returns at least one violation (does not silently
    accept the contradiction)."""
    fixture = '''
name = "fixture-reviewer"
default_permissions = "loop-protocol-readonly"
developer_instructions = """
ROLE
- read-only reviewer.

OUTPUT_CONTRACT
- full structured data は
  .claude/artifacts/issue-refinement-loop/<N>/ 配下に
  artifact として保存し、findings[] を保持する。
"""
'''
    violations = _PIPELINE.check_agent_is_read_only_advisory(fixture)
    assert violations, "expected the checker to reject a read-only agent with a workspace write requirement"


def test_non_read_only_fixture_is_not_flagged_for_write_requirements():
    """GIVEN a fixture agent config that is NOT declared read-only
    WHEN checked for a workspace write requirement
    THEN it is not flagged (the check only applies to agents that declare
    themselves read-only; a writer agent legitimately writing artifacts is
    not a contradiction)."""
    fixture = '''
name = "fixture-writer"
default_permissions = "loop-protocol-standard"
developer_instructions = """
OUTPUT_CONTRACT
- full structured data は .claude/artifacts/issue-refinement-loop/<N>/ 配下に artifact として保存する。
"""
'''
    violations = _PIPELINE.check_agent_is_read_only_advisory(fixture)
    assert violations == []
