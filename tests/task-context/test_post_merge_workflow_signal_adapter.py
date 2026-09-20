"""AC8 regression: partial GraphQL snapshots cannot start cleanup."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_POST_MERGE_SCRIPTS = (
    Path(__file__).resolve().parents[2] / ".claude" / "skills" / "post-merge-cleanup" / "scripts"
)
sys.path.insert(0, str(_POST_MERGE_SCRIPTS))

import task_context_workflow_signal as post_merge_signal  # noqa: E402


def test_given_graphql_data_plus_top_level_errors_when_merge_signal_runs_then_it_never_applies_or_begins_cleanup(
    tmp_path, monkeypatch, capsys
):
    snapshot = {
        "data": {
            "repository": {
                "nameWithOwner": "squne121/loop-protocol",
                "pullRequest": {
                    "number": 21,
                    "merged": True,
                    "mergeCommit": {"oid": "b" * 40},
                    "closingIssuesReferences": {"nodes": [{"number": 20, "repository": {"nameWithOwner": "squne121/loop-protocol"}}]},
                },
            }
        },
        "errors": [{"message": "partial resolver failure"}],
    }
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")

    def fail_task_context_mutation(*args, **kwargs):
        raise AssertionError("partial GraphQL data must not apply a signal or begin cleanup")

    monkeypatch.setattr(post_merge_signal, "_run", fail_task_context_mutation)

    assert (
        post_merge_signal.main(
            [
                "--snapshot-file",
                str(snapshot_file),
                "--issue-number",
                "20",
                "--pr-number",
                "21",
                "--phase",
                "merged",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "disposition": "deferred",
        "reason_code": "RELATION_UNAVAILABLE",
    }


def _merged_snapshot() -> dict:
    return {
        "data": {
            "repository": {
                "nameWithOwner": "squne121/loop-protocol",
                "pullRequest": {
                    "number": 21,
                    "merged": True,
                    "mergeCommit": {"oid": "b" * 40},
                    "closingIssuesReferences": {"nodes": [{"number": 20, "repository": {"nameWithOwner": "squne121/loop-protocol"}}]},
                },
            }
        }
    }


def _cleanup_report(*, status: str = "ok", human_review_required: bool = False) -> dict:
    return {
        "status": status,
        "generated_at": "2026-01-01T00:00:00Z",
        "generated_by": "post-merge-cleanup-worker",
        "human_review_required": human_review_required,
        "cleaned_branches": [],
        "cleaned_worktrees": [],
        "unresolved_cleanup_items": [],
        "parent_issue_status": {
            "parent_issue_number": 1,
            "all_children_closed": False,
            "recommended_action": "keep_open",
        },
        "superseded_prs": [],
        "follow_up_issue_requests": [],
        "stash_restored": "n/a",
        "stash_entry_ref": None,
        "warnings": [],
        "errors": [],
    }


def test_given_cross_repository_same_number_closing_relation_when_merged_evidence_is_derived_then_it_is_rejected():
    snapshot = _merged_snapshot()
    snapshot["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"][0]["repository"] = {
        "nameWithOwner": "owner/other"
    }

    evidence, reason = post_merge_signal._merged_evidence(snapshot, 20, 21)

    assert (evidence, reason) == (None, "RELATION_ISSUE_MISMATCH")


def _must_not_apply(*_args, **_kwargs):
    raise AssertionError("non-final cleanup evidence must not apply a signal")


def test_given_no_final_success_receipt_when_cleanup_completion_runs_then_it_never_applies_signal(
    tmp_path, monkeypatch, capsys
):
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(json.dumps(_merged_snapshot()), encoding="utf-8")
    monkeypatch.setattr(post_merge_signal, "_run", _must_not_apply)

    assert post_merge_signal.main([
        "--snapshot-file", str(snapshot_file), "--issue-number", "20", "--pr-number", "21", "--phase", "completed"
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "disposition": "deferred",
        "reason_code": "CLEANUP_FINAL_SUCCESS_RECEIPT_REQUIRED",
    }


def test_given_partial_failed_or_human_review_receipt_when_cleanup_completion_runs_then_it_never_applies_signal(
    tmp_path, monkeypatch, capsys
):
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(json.dumps(_merged_snapshot()), encoding="utf-8")
    monkeypatch.setattr(post_merge_signal, "_run", _must_not_apply)

    for index, report in enumerate((
        _cleanup_report(status="partial"),
        _cleanup_report(status="failed"),
        _cleanup_report(human_review_required=True),
    )):
        receipt_file = tmp_path / f"receipt-{index}.json"
        receipt_file.write_text(json.dumps(report), encoding="utf-8")
        assert post_merge_signal.main([
            "--snapshot-file", str(snapshot_file), "--issue-number", "20", "--pr-number", "21", "--phase", "completed",
            "--cleanup-receipt-file", str(receipt_file),
        ]) == 0
        assert json.loads(capsys.readouterr().out) == {
            "disposition": "deferred",
            "reason_code": "CLEANUP_NOT_FINAL_SUCCESS",
        }
