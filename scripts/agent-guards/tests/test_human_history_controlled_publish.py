from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[3]
_GUARDS = _ROOT / "scripts/agent-guards"
sys.path.insert(0, str(_GUARDS))
import controlled_skill_mutation_exec as executor


def _marker(seed: str = "identity") -> str:
    return f"<!-- loop-protocol/human-history:v1:sha256:{hashlib.sha256(seed.encode()).hexdigest()} -->"


def _body(visible: str = "日本語の公開可能な履歴", marker: str | None = None, newline: str = "\n") -> str:
    return f"{visible}{newline}{marker or _marker()}{newline}"


def test_marker_ownership_uniqueness_create_noop_patch_and_readback_reconciliation_contract():
    marker = _marker()
    parsed, error = executor._parse_human_history_marker_source(_body(marker=marker, newline="\r\n"))
    assert error == ""
    assert parsed and parsed["marker"] == marker
    assert parsed["content_digest"] == hashlib.sha256("日本語の公開可能な履歴".encode()).hexdigest()

    # Every raw namespace occurrence is a candidate and malformed prose fails
    # before content normalization or ownership logic.
    malformed = [
        "text loop-protocol/human-history",
        _body(marker="<!-- loop-protocol/human-history:v2:sha256:" + "a" * 64 + " -->"),
        _body(marker="<!-- loop-protocol/human-history:v1:sha256:" + "A" * 64 + " -->"),
        _body(marker=" <!-- loop-protocol/human-history:v1:sha256:" + "a" * 64 + " -->"),
        _body(marker="<!-- loop-protocol/human-history:v1:sha256:" + "a" * 64 + " --> trailing"),
        _body(marker="<!-- loop-protocol/human-history:v1:sha256:" + "a" * 64 + " -->") + "later\n",
        _body(marker=marker) + marker,
    ]
    for value in malformed:
        assert executor._parse_human_history_marker_source(value)[0] is None

    # Terminal newline variants normalize identically; other Unicode bytes do
    # not.  BOM is input and is neither stripped nor normalized.
    canonical = _body(marker=marker)
    no_terminal = canonical[:-1]
    extra_terminal = canonical + "\n"
    for value in (canonical, no_terminal, extra_terminal, canonical.replace("\n", "\r\n")):
        parsed_value, parse_error = executor._parse_human_history_marker_source(value)
        assert parse_error == "" and parsed_value
        assert parsed_value["content_digest"] == hashlib.sha256("日本語の公開可能な履歴".encode()).hexdigest()
    bom, bom_error = executor._parse_human_history_marker_source(_body("﻿日本語の公開可能な履歴", marker))
    assert bom_error == "" and bom
    assert bom["content_digest"] != parsed["content_digest"]

    # The full handler routes same identity/digest to noop and changed visible
    # content to PATCH.  A foreign valid marker is never a mutation target.
    args = SimpleNamespace(issue_number=1908, repo="squne121/loop-protocol", command_id="issue_comment.publish", dry_run=False)
    calls: list[str] = []
    remote = {"body": canonical, "url": "https://github.com/x/y/issues/1#issuecomment-42", "id": "x", "author": {"login": "writer"}}
    data = {"comment_body": canonical, "marker": marker}
    with patch.object(executor, "_capture_pre_mutation_snapshot", return_value=(object(), None)), \
         patch.object(executor, "_fetch_authenticated_login", return_value=("writer", "")), \
         patch.object(executor, "_list_issue_comments", return_value=([remote], "")), \
         patch.object(executor, "_human_history_readback", return_value=({"comment_id": "x", "comment_url": "url", "identity_sha256": "a" * 64, "content_digest": parsed["content_digest"]}, "")), \
         patch.object(executor, "_check_no_tracked_changes", return_value=[]):
        assert executor._run_human_history_comment_publish(args, data, "/bin/gh", lambda *a, **k: 1, lambda x: calls.append(x) or 0) == 0
    assert calls[-1]["status_detail"] == "already_published"

    foreign = dict(remote, author={"login": "other"})
    with patch.object(executor, "_capture_pre_mutation_snapshot", return_value=(object(), None)), \
         patch.object(executor, "_fetch_authenticated_login", return_value=("writer", "")), \
         patch.object(executor, "_list_issue_comments", return_value=([foreign], "")), \
         patch.object(executor, "_post_gh_comment") as post:
        assert executor._run_human_history_comment_publish(args, data, "/bin/gh", lambda *a, **k: 1, lambda x: 0) == 1
        post.assert_not_called()
