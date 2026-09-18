"""AC8 regression: partial GraphQL snapshots cannot start cleanup."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_POST_MERGE_SCRIPTS = (
    Path(__file__).resolve().parents[2] / ".claude" / "skills" / "post-merge-cleanup" / "scripts"
)
sys.path.insert(0, str(_POST_MERGE_SCRIPTS))

import task_context_workflow_signal as post_merge_signal


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
                    "closingIssuesReferences": {"nodes": [{"number": 20}]},
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
