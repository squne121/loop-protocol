from __future__ import annotations

import importlib.util
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[4]
_PUBLISH = _ROOT / ".claude/skills/issue-refinement-loop/scripts/publish_termination_report.py"
_spec = importlib.util.spec_from_file_location("human_history_publish_phase", _PUBLISH)
publisher = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(publisher)


def _identity(loop: str, phase: str, target_kind: str, reason: str) -> dict:
    target = 47 if target_kind == "pull_request" else 1908
    reviewed = ("refs/pull/47/head@" + "b" * 40) if target_kind == "pull_request" else "a" * 64
    return {
        "loop_kind": loop,
        "phase": phase,
        "source_issue_number": 1908,
        "target_kind": target_kind,
        "target_number": target,
        "route_or_termination_reason": reason,
        "reviewed_ref": reviewed,
    }


def _render(identity: dict):
    return publisher.render_human_history_comment(
        identity=identity,
        result="判定を記録しました",
        evidence_refs=["https://github.com/squne121/loop-protocol/issues/1908"],
        recommended_action="次の判断を実施してください",
        recommended_reason="現在の証跡に基づくためです",
        impact_if_unaddressed="判断根拠が不足します",
    )


def test_new_human_history_destination_matrix_preserves_machine_comments():
    rows = [
        ("issue-refinement-loop", "review-complete", "issue", "completed"),
        ("impl-review-loop", "pre-PR-binding", "issue", "needs_fix"),
        ("impl-review-loop", "binding-validation", "issue", "binding_missing"),
        ("impl-review-loop", "post-PR-binding", "pull_request", "completed"),
        ("impl-review-loop", "post-PR-head-drift", "pull_request", "head_drift"),
        ("impl-review-loop", "conflict-resolution", "pull_request", "human_escalation"),
    ]
    for row in rows:
        rendered, error = _render(_identity(*row))
        assert error == "" and rendered
        assert rendered["identity"]["target_kind"] == row[2]
        assert rendered["body"].count("loop-protocol/human-history") == 1
    executor_source = (_ROOT / "scripts/agent-guards/controlled_skill_mutation_exec.py").read_text()
    assert "existing machine-comment" in executor_source
    assert "_run_human_history_comment_publish" in executor_source


def test_emission_matrix_reviewed_ref_ssot_and_template_inputs():
    issue = _identity("impl-review-loop", "binding-validation", "issue", "binding_wrong_repo")
    pr = _identity("impl-review-loop", "post-PR-binding", "pull_request", "needs_fix")
    for identity in (issue, pr):
        rendered, error = _render(identity)
        assert error == "" and rendered
        for required in ("実施内容", "推奨アクション", "推奨する理由", "対応しない場合の影響", "evidence refs", identity["reviewed_ref"]):
            assert required in rendered["body"]
    assert issue["target_number"] == issue["source_issue_number"]
    assert pr["target_number"] == 47


def test_post_pr_head_drift_rechecks_prevent_old_result_patch_and_rereview():
    primary = _identity("impl-review-loop", "post-PR-binding", "pull_request", "completed")
    stale = _identity("impl-review-loop", "post-PR-head-drift", "pull_request", "head_drift")
    stale["reviewed_ref"] = primary["reviewed_ref"]
    for identity in (primary, stale):
        assert publisher._validate_human_history_identity(identity)[0] is not None
    # Different snapshots necessarily yield different immutable identities:
    # an old primary cannot be PATCHed into a result for a new head.
    newer = dict(primary, reviewed_ref="refs/pull/47/head@" + "c" * 40)
    assert publisher._human_history_jcs(primary) != publisher._human_history_jcs(newer)
    step = (_ROOT / ".claude/skills/impl-review-loop/steps/step-5-feedback-and-termination.md").read_text()
    assert "direct PR-head read" in step
    assert "head_drift" in step
