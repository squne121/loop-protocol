from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[4]


def test_quality_scope_is_limited_to_new_human_history():
    source = (_ROOT / ".claude/skills/issue-refinement-loop/scripts/publish_termination_report.py").read_text()
    assert "existing controlled publisher, not a new" in source
    assert "existing issue_comment.publish" in source
    assert "human-interface-finalizer" not in source
    assert "finalizer SubAgent" not in source
    step = (_ROOT / ".claude/skills/impl-review-loop/steps/context-protocol-and-guardrails.md").read_text()
    assert "human-history" in step
