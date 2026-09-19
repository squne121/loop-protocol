"""AC5/AC14: open-pr producer adapter only emits a validated fact."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".claude" / "skills" / "open-pr" / "scripts"))

import open_pr


def test_given_graphql_errors_when_open_pr_observes_an_existing_pr_then_it_never_calls_signal_apply(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    graphql_response = {
        "errors": [{"message": "resolver failed"}],
        "data": {
            "repository": {
                "nameWithOwner": "squne121/loop-protocol",
                "pullRequest": {"number": 21, "closingIssuesReferences": {"nodes": [{"number": 20}]}},
            }
        },
    }

    def fake_run_gh(*args, **kwargs):
        assert args[:2] == ("api", "graphql")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(graphql_response), stderr="")

    def fail_signal_apply(*args, **kwargs):
        raise AssertionError("unavailable GraphQL relation must not emit a Task Context signal")

    monkeypatch.setattr(open_pr, "run_gh", fake_run_gh)
    monkeypatch.setattr(open_pr.subprocess, "run", fail_signal_apply)

    assert open_pr.emit_implementation_pr_observed(repo="squne121/loop-protocol", pr_number=21, linked_issue=20) == (
        "deferred",
        "RELATION_UNAVAILABLE",
    )


def test_given_unbound_origin_when_open_pr_observes_a_pr_then_it_does_not_fetch_or_emit(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        open_pr, "run_gh", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not fetch"))
    )
    assert open_pr.emit_implementation_pr_observed(repo="squne121/loop-protocol", pr_number=21, linked_issue=20) == (
        "deferred",
        "unbound",
    )


def test_given_dry_run_and_existing_pr_when_open_pr_runs_then_it_never_emits_a_signal(tmp_path, monkeypatch):
    body_file = tmp_path / "body.md"
    body_file.write_text("## Summary\n\npreview", encoding="utf-8")
    monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda *_args: "OPEN")
    monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda *_args: [])
    monkeypatch.setattr(open_pr, "_run_pr_body_validator", lambda *_args: {"status": "pass"})
    monkeypatch.setattr(open_pr, "_run_japanese_content_validator", lambda *_args: {"status": "pass"})
    monkeypatch.setattr(
        open_pr,
        "find_existing_pr",
        lambda *_args: (_ for _ in ()).throw(AssertionError("dry-run must not inspect an existing PR")),
    )
    monkeypatch.setattr(
        open_pr,
        "emit_implementation_pr_observed",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("dry-run must not emit a Task Context signal")),
    )

    assert (
        open_pr.main(
            [
                "--pr-title",
                "preview",
                "--linked-issue",
                "20",
                "--publish",
                "yes",
                "--pr-body-file",
                str(body_file),
                "--repo",
                "owner/repo",
                "--branch",
                "preview-branch",
                "--dry-run",
            ]
        )
        == 0
    )
