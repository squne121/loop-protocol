"""Transaction ordering regression coverage for Issue #1660."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import edit_issue_txn as txn  # noqa: E402


def test_comment_failure_after_content_update_is_failed_after_mutation(monkeypatch):
    body = "new body"
    input_data = {
        "schema": "ISSUE_EDIT_TXN_INPUT_V1",
        "issue_number": 1660,
        "repo": "squne121/loop-protocol",
        "new_body_file": "tmp/ignored.md",
        "readiness_forwarding_payload": {"readiness_result": {
            "status": "go", "body_sha256": "sha256:x", "source_checks": [],
            "errors": [], "readiness_result_ref": "artifacts/ready.json",
        }},
        "comment_mode": {"mode": "publish", "comment_body_file": "tmp/comment.md", "marker": "<!-- marker -->"},
        "expected_previous_body_sha256": txn._sha256_text("old body"),
        "expected_previous_updated_at": "2026-07-22T00:00:00Z",
        "title_update": {"required": True, "proposed_title": "new title", "reason": "scope reframe"},
    }
    readbacks = iter([
        {"title": "old title", "body": "old body", "updatedAt": "2026-07-22T00:00:00Z"},
        {"title": "new title", "body": body, "updatedAt": "2026-07-22T00:00:01Z"},
    ])
    monkeypatch.setattr(txn, "_fetch_issue", lambda *_: (next(readbacks), ""))
    monkeypatch.setattr(txn, "_read_text_file", lambda _: body)
    monkeypatch.setattr(txn, "_run_command", lambda *_: subprocess.CompletedProcess([], 0, "", ""))
    metadata_input_path = (
        "artifacts/1660/issue-metadata/issue_content.update/input.json"
    )
    monkeypatch.setattr(
        txn, "_write_issue_metadata_input", lambda *_: metadata_input_path
    )
    monkeypatch.setattr(txn, "_invoke_controlled_exec", lambda command, *_: (
        subprocess.CompletedProcess([], 0, "{}", ""), {"new_body_sha256": txn._sha256_text(body)}
    ) if command == "issue_content.update" else (subprocess.CompletedProcess([], 1, "", "comment failed"), None))

    result = txn.run_transaction(input_data)

    assert result["status"] == "failed_after_mutation"
    assert result["mutation_started"] is True
    assert result["body_update"]["status"] == "ok"
    assert result["comment_publish"]["status"] == "failed"
