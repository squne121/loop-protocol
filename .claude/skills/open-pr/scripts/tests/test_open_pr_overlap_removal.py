#!/usr/bin/env python3
"""#1679 AC4/AC5: negative behavior tests for the removed overlap preflight
hard gate.

`open_pr.py`'s public CLI no longer exposes `--overlap-preflight-*` flags,
and a linked Issue carrying the `phase/implementation` label no longer
triggers any overlap gate (the label-forcing mechanism itself has been
removed from production, per #1679 In Scope items 1-3).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import open_pr
import validate_pr_body


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pr_body"


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def write_temp_body(body: str) -> str:
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False)
    handle.write(body)
    handle.flush()
    handle.close()
    return handle.name


def test_public_cli_has_no_overlap_preflight_flags():
    """AC4: `--overlap-preflight-*` flags are gone from the public CLI."""
    args = open_pr.parse_args(
        [
            "--pr-title", "feat: test",
            "--linked-issue", "1",
            "--publish", "yes",
            "--pr-body-file", "/tmp/does-not-matter.md",
        ]
    )
    for removed_attr in (
        "overlap_preflight_required",
        "overlap_preflight_evidence_file",
        "overlap_preflight_expected_evidence_sha256",
        "overlap_preflight_expected_decision_inputs_sha256",
    ):
        assert not hasattr(args, removed_attr), f"removed CLI attribute still present: {removed_attr}"

    with pytest.raises(SystemExit):
        open_pr.parse_args(
            [
                "--pr-title", "feat: test",
                "--linked-issue", "1",
                "--publish", "yes",
                "--pr-body-file", "/tmp/does-not-matter.md",
                "--overlap-preflight-required",
            ]
        )


def test_phase_implementation_label_does_not_trigger_overlap_gate(monkeypatch: pytest.MonkeyPatch):
    """AC5: a `phase/implementation`-labeled linked Issue no longer forces
    any overlap gate -- `gh pr create` proceeds normally with no overlap
    evidence input, and no label re-fetch for gate-forcing purposes occurs.
    """
    body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
    create_called = {"value": False}
    label_fetch_called = {"value": False}

    def fake_create_pr(repo, title, body_file, branch, draft):
        create_called["value"] = True
        return "https://github.com/squne121/loop-protocol/pull/9999"

    try:
        monkeypatch.setattr(open_pr, "resolve_repo", lambda: "squne121/loop-protocol")
        monkeypatch.setattr(open_pr, "resolve_branch", lambda: "worktree-issue-1679-test")
        monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: "OPEN")
        monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda provided: ["src/example.ts"])
        monkeypatch.setattr(
            open_pr,
            "_run_pr_body_validator",
            lambda body, changed_paths, linked_issue: {"status": "pass", "errors": []},
        )
        monkeypatch.setattr(
            open_pr,
            "_run_japanese_content_validator",
            lambda body_text, threshold=0.1: {
                "status": "pass",
                "failed_blocks": 0,
                "aggregate_ratio": 0.5,
                "threshold": 0.1,
                "body_sha256": "",
                "stderr": "",
            },
        )
        monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)
        monkeypatch.setattr(open_pr, "resolve_canonical_repository", lambda repo: repo)
        monkeypatch.setattr(open_pr, "create_pr", fake_create_pr)

        # open_pr module no longer defines a label-forcing gate at all; there
        # is no `fetch_current_linked_issue_labels` attribute to monkeypatch
        # or call, regardless of the linked Issue's labels (e.g.
        # `phase/implementation`).
        assert not hasattr(open_pr, "fetch_current_linked_issue_labels")

        rc = open_pr.main(
            [
                "--pr-title", "feat: test",
                "--linked-issue", "1679",
                "--publish", "yes",
                "--pr-body-file", body_path,
            ]
        )
        assert rc == 0
        assert create_called["value"] is True
        assert label_fetch_called["value"] is False
    finally:
        Path(body_path).unlink(missing_ok=True)


HISTORICAL_OVERLAP_BYPASS_VOCABULARY = (
    "\n\n## Overlap Preflight 自動判定結果の補足説明\n\n"
    "overlap preflight が兄弟 Issue との `C3 parent_child_collision` を検出し "
    "`route: human_review_required` を返したが、本 PR は overlap gate を経由せず "
    "`gh pr create` を直接実行して起票する（バイパス判断）。\n"
)


def test_historical_overlap_c2a_c3_bypass_vocabulary_does_not_trigger_publication_block():
    """Issue #1679 In Scope 9: a PR body that still contains historical
    overlap/C2a/C3/bypass vocabulary (as used by the now-removed
    OVERLAP_GATE_BYPASS_V1 hard gate, Issue #1776) no longer produces any
    overlap-specific publication block. `validate_pr_body()` no longer has
    an `_validate_overlap_gate_bypass` check at all, so this vocabulary is
    inert prose with no gate consequence.
    """
    base_body = load_fixture("valid_not_schema_change.md")
    body_with_legacy_vocabulary = base_body + HISTORICAL_OVERLAP_BYPASS_VOCABULARY

    result = validate_pr_body.validate_pr_body(
        body_with_legacy_vocabulary,
        changed_paths=["src/example.ts"],
        linked_issue=330,
    )

    assert result.status == "pass", (
        f"expected legacy overlap/C2a/C3/bypass vocabulary to be inert, got errors: {result.errors}"
    )
    overlap_rule_ids = {
        error.rule_id
        for error in result.errors
        if "OVERLAP" in error.rule_id.upper() or error.rule_id == "LP059"
    }
    assert not overlap_rule_ids, f"unexpected overlap-specific rule triggered: {overlap_rule_ids}"
    assert not hasattr(validate_pr_body, "_validate_overlap_gate_bypass")
