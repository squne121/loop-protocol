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
