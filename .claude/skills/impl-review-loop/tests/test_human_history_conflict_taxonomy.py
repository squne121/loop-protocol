from __future__ import annotations

import importlib.util
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location(
    "human_history_conflict",
    _ROOT / ".claude/skills/issue-refinement-loop/scripts/publish_termination_report.py",
)
publisher = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(publisher)


def test_only_repeated_same_iteration_conflict_becomes_human_escalation_history():
    base = {
        "loop_kind": "impl-review-loop",
        "phase": "conflict-resolution",
        "source_issue_number": 1908,
        "target_kind": "issue",
        "target_number": 1908,
        "reviewed_ref": "a" * 64,
    }
    standalone = dict(base, route_or_termination_reason="head_drift")
    repeated = dict(base, route_or_termination_reason="human_escalation")
    assert publisher._validate_human_history_identity(standalone)[0] is None
    assert publisher._validate_human_history_identity(repeated)[0] is not None
    policy = (_ROOT / ".claude/skills/issue-refinement-loop/references/termination-policy.md").read_text()
    step = (_ROOT / ".claude/skills/impl-review-loop/steps/step-5-feedback-and-termination.md").read_text()
    assert "conflict_hard_stop" in policy or "conflict_hard_stop" in step
