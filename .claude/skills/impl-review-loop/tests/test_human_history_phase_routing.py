from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[4]
_GUARDS = _ROOT / "scripts/agent-guards"
sys.path.insert(0, str(_GUARDS))
_EXECUTOR = _GUARDS / "controlled_skill_mutation_exec.py"
_executor_spec = importlib.util.spec_from_file_location("human_history_controlled_executor", _EXECUTOR)
executor = importlib.util.module_from_spec(_executor_spec)
assert _executor_spec and _executor_spec.loader
_executor_spec.loader.exec_module(executor)

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
        stale_evidence=(
            "latest head: " + "c" * 40 if identity["phase"] == "post-PR-head-drift" else None
        ),
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
        required_fields = (
            "実施内容",
            "推奨アクション",
            "推奨する理由",
            "対応しない場合の影響",
            "evidence refs",
            identity["reviewed_ref"],
        )
        for required in required_fields:
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


def _post_pr_publish_input(identity: dict, *, result: str = "判定を記録しました", stale_evidence: str | None = None):
    rendered, error = publisher.render_human_history_comment(
        identity=identity,
        result=result,
        evidence_refs=["https://github.com/squne121/loop-protocol/pull/47"],
        recommended_action="次の判断を実施してください",
        recommended_reason="現在の証跡に基づくためです",
        impact_if_unaddressed="判断根拠が不足します",
        stale_evidence=stale_evidence,
    )
    assert error == "" and rendered
    parsed, parse_error = executor._parse_human_history_marker_source(rendered["body"])
    assert parse_error == "" and parsed
    return {"comment_body": rendered["body"], "marker": rendered["marker"]}, parsed


def test_post_pr_head_checks_directly_guard_create_patch_noop_and_post_readback():
    primary = _identity("impl-review-loop", "post-PR-binding", "pull_request", "completed")
    data, parsed = _post_pr_publish_input(primary)
    args = SimpleNamespace(
        issue_number=47, repo="squne121/loop-protocol", command_id="issue_comment.publish", dry_run=False
    )

    def run_case(kind: str) -> None:
        remote_body = data["comment_body"]
        if kind == "patch":
            remote_body = remote_body.replace("判定を記録しました", "以前の判定です")
        remote = {
            "body": remote_body,
            "url": "https://github.com/squne121/loop-protocol/issues/47#issuecomment-42",
            "id": "x",
            "author": {"login": "writer"},
        }
        matching = [] if kind == "create" else [remote]
        ok: list[dict] = []
        with patch.object(executor, "_capture_pre_mutation_snapshot", return_value=(object(), None)), \
             patch.object(executor, "_fetch_authenticated_login", return_value=("writer", "")), \
             patch.object(executor, "_list_issue_comments", return_value=(matching, "")), \
             patch.object(executor, "_human_history_readback", return_value=({
                 "comment_id": "x", "comment_url": "url", "identity_sha256": "a" * 64,
                 "content_digest": parsed["content_digest"],
             }, "")), \
             patch.object(executor, "_check_no_tracked_changes", return_value=[]), \
             patch.object(executor, "_fetch_pr_head_sha", return_value=("b" * 40, "")) as head_read, \
             patch.object(executor, "_post_gh_comment", return_value=("url", "x", "")) as post, \
             patch.object(executor, "_patch_gh_comment", return_value="") as patch_comment:
            assert executor._run_human_history_comment_publish(
                args, data, "/bin/gh", lambda *a, **k: 1, lambda value: ok.append(value) or 0
            ) == 0
        assert head_read.call_count == 2  # decision-time read and post-readback read
        assert post.called is (kind == "create")
        assert patch_comment.called is (kind == "patch")
        expected_status = {
            "create": "created",
            "patch": "updated",
            "noop": "already_published",
        }[kind]
        assert ok[-1]["status_detail"] == expected_status

    for decision in ("create", "patch", "noop"):
        run_case(decision)


def test_post_pr_head_drift_routes_reconciliation_before_or_after_noop():
    primary = _identity("impl-review-loop", "post-PR-binding", "pull_request", "completed")
    data, parsed = _post_pr_publish_input(primary)
    args = SimpleNamespace(
        issue_number=47, repo="squne121/loop-protocol", command_id="issue_comment.publish", dry_run=False
    )
    remote = {
        "body": data["comment_body"],
        "url": "https://github.com/squne121/loop-protocol/issues/47#issuecomment-42",
        "id": "x",
        "author": {"login": "writer"},
    }
    failures: list[tuple[tuple, dict]] = []
    with patch.object(executor, "_capture_pre_mutation_snapshot", return_value=(object(), None)), \
         patch.object(executor, "_fetch_authenticated_login", return_value=("writer", "")), \
         patch.object(executor, "_list_issue_comments", return_value=([remote], "")), \
         patch.object(executor, "_human_history_readback", return_value=({
             "comment_id": "x", "comment_url": "url", "identity_sha256": "a" * 64,
             "content_digest": parsed["content_digest"],
         }, "")), \
         patch.object(executor, "_check_no_tracked_changes", return_value=[]), \
         patch.object(executor, "_fetch_pr_head_sha", side_effect=[("b" * 40, ""), ("c" * 40, "")]):
        assert executor._run_human_history_comment_publish(
            args, data, "/bin/gh", lambda *a, **k: failures.append((a, k)) or 1, lambda _: 0
        ) == 1
    assert failures[-1][0][0] == "human_history_primary_head_drift_reconciliation_required"
    assert failures[-1][1]["status"] == "stale_head"
    assert failures[-1][1]["extra"]["head_drift"]["route"] == "reconcile_head_drift_then_rereview"

    diagnostic = _identity("impl-review-loop", "post-PR-head-drift", "pull_request", "head_drift")
    diagnostic["reviewed_ref"] = primary["reviewed_ref"]
    diagnostic_data, _ = _post_pr_publish_input(diagnostic, stale_evidence="latest head: " + "c" * 40)
    failures.clear()
    with patch.object(executor, "_capture_pre_mutation_snapshot", return_value=(object(), None)), \
         patch.object(executor, "_fetch_authenticated_login", return_value=("writer", "")), \
         patch.object(executor, "_list_issue_comments", return_value=([], "")), \
         patch.object(executor, "_fetch_pr_head_sha", return_value=("d" * 40, "")), \
         patch.object(executor, "_post_gh_comment") as post:
        assert executor._run_human_history_comment_publish(
            args, diagnostic_data, "/bin/gh", lambda *a, **k: failures.append((a, k)) or 1, lambda _: 0
        ) == 1
    post.assert_not_called()
    assert failures[-1][0][0] == "human_history_diagnostic_head_drift_reconciliation_required"
    assert failures[-1][1]["extra"]["head_drift"]["route"] == "reconcile_same_head_drift_identity_then_rereview"


def test_head_drift_rejects_primary_patch_and_diagnostic_noop_before_mutation():
    primary = _identity("impl-review-loop", "post-PR-binding", "pull_request", "completed")
    primary_data, _ = _post_pr_publish_input(primary)
    args = SimpleNamespace(
        issue_number=47, repo="squne121/loop-protocol", command_id="issue_comment.publish", dry_run=False
    )
    changed_primary = primary_data["comment_body"].replace("判定を記録しました", "以前の判定です")
    primary_remote = {
        "body": changed_primary,
        "url": "https://github.com/squne121/loop-protocol/issues/47#issuecomment-42",
        "id": "x",
        "author": {"login": "writer"},
    }
    failures: list[tuple[tuple, dict]] = []
    with patch.object(executor, "_capture_pre_mutation_snapshot", return_value=(object(), None)), \
         patch.object(executor, "_fetch_authenticated_login", return_value=("writer", "")), \
         patch.object(executor, "_list_issue_comments", return_value=([primary_remote], "")), \
         patch.object(executor, "_fetch_pr_head_sha", return_value=("c" * 40, "")), \
         patch.object(executor, "_patch_gh_comment") as patch_comment:
        assert executor._run_human_history_comment_publish(
            args, primary_data, "/bin/gh", lambda *a, **k: failures.append((a, k)) or 1, lambda _: 0
        ) == 1
    patch_comment.assert_not_called()
    assert failures[-1][0][0] == "human_history_primary_head_drift_reconciliation_required"

    diagnostic = _identity("impl-review-loop", "post-PR-head-drift", "pull_request", "head_drift")
    diagnostic["reviewed_ref"] = primary["reviewed_ref"]
    data, _ = _post_pr_publish_input(diagnostic, stale_evidence="latest head: " + "c" * 40)
    diagnostic_remote = dict(primary_remote, body=data["comment_body"])
    failures.clear()
    with patch.object(executor, "_capture_pre_mutation_snapshot", return_value=(object(), None)), \
         patch.object(executor, "_fetch_authenticated_login", return_value=("writer", "")), \
         patch.object(executor, "_list_issue_comments", return_value=([diagnostic_remote], "")), \
         patch.object(executor, "_fetch_pr_head_sha", return_value=("d" * 40, "")), \
         patch.object(executor, "_patch_gh_comment") as patch_comment, \
         patch.object(executor, "_post_gh_comment") as post:
        assert executor._run_human_history_comment_publish(
            args, data, "/bin/gh", lambda *a, **k: failures.append((a, k)) or 1, lambda _: 0
        ) == 1
    patch_comment.assert_not_called()
    post.assert_not_called()
    assert failures[-1][0][0] == "human_history_diagnostic_head_drift_reconciliation_required"


def test_diagnostic_noop_rechecks_head_after_readback_before_rereview():
    primary = _identity("impl-review-loop", "post-PR-binding", "pull_request", "completed")
    diagnostic = _identity("impl-review-loop", "post-PR-head-drift", "pull_request", "head_drift")
    diagnostic["reviewed_ref"] = primary["reviewed_ref"]
    data, parsed = _post_pr_publish_input(diagnostic, stale_evidence="latest head: " + "c" * 40)
    args = SimpleNamespace(
        issue_number=47, repo="squne121/loop-protocol", command_id="issue_comment.publish", dry_run=False
    )
    remote = {
        "body": data["comment_body"],
        "url": "https://github.com/squne121/loop-protocol/issues/47#issuecomment-42",
        "id": "x",
        "author": {"login": "writer"},
    }
    failures: list[tuple[tuple, dict]] = []
    with patch.object(executor, "_capture_pre_mutation_snapshot", return_value=(object(), None)), \
         patch.object(executor, "_fetch_authenticated_login", return_value=("writer", "")), \
         patch.object(executor, "_list_issue_comments", return_value=([remote], "")), \
         patch.object(executor, "_human_history_readback", return_value=({
             "comment_id": "x", "comment_url": "url", "identity_sha256": "a" * 64,
             "content_digest": parsed["content_digest"],
         }, "")), \
         patch.object(executor, "_fetch_pr_head_sha", side_effect=[("c" * 40, ""), ("d" * 40, "")]):
        assert executor._run_human_history_comment_publish(
            args, data, "/bin/gh", lambda *a, **k: failures.append((a, k)) or 1, lambda _: 0
        ) == 1
    assert failures[-1][0][0] == "human_history_diagnostic_head_drift_reconciliation_required"
    assert failures[-1][1]["status"] == "stale_head"
    assert failures[-1][1]["extra"]["rerun_required"] == {"pr_review": True}
