"""Regression tests for the Claude Code issue-reviewer SubagentStop guard."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK_PATH = REPO_ROOT / ".claude/hooks/validate_issue_reviewer_compact_output.py"
SCRIPTS_DIR = REPO_ROOT / ".claude/skills/issue-refinement-loop/scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import emit_parent_review_envelope_v2 as emit_mod  # noqa: E402


def _valid_approve() -> str:
    return "\n".join(
        [
            "STATUS: ok",
            "VERDICT: approve",
            "SUMMARY: contract ready",
            "BLOCKERS: 0",
            "NEXT_ACTION: proceed",
            "MUST_READ: ",
            "EVIDENCE: .claude/artifacts/issue-refinement-loop/1754/compact_review_result_20260717T000000Z.json",
            "ARTIFACT: compact_review_result_v1=.claude/artifacts/issue-refinement-loop/1754/compact_review_result_20260717T000000Z.json",
        ]
    ) + "\n"


def _valid_needs_fix(tmp_path: Path) -> str:
    fixture = REPO_ROOT / ".claude/skills/issue-refinement-loop/fixtures/review_result_needs_fix.json"
    source = json.loads(fixture.read_text(encoding="utf-8"))
    source["body_sha256"] = "sha256:" + "a" * 64
    source["blocking_issues"] = [{"code": "missing_section", "message": "missing section"}]
    input_file = tmp_path / "review-result.json"
    input_file.write_text(json.dumps(source), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "compact_review_result.py"),
            "--input-file",
            str(input_file),
            "--issue-number",
            "1754",
            "--repo-root",
            str(tmp_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _run_hook(payload: dict[str, object], receipt_dir: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"ISSUE_REVIEWER_RUNTIME_RECEIPT_DIR": str(receipt_dir)}
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        cwd=REPO_ROOT,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _payload(message: str, *, stop_hook_active: bool = False) -> dict[str, object]:
    return {
        "hook_event_name": "SubagentStop",
        "agent_type": "issue-reviewer",
        "last_assistant_message": message,
        "stop_hook_active": stop_hook_active,
    }


@pytest.mark.parametrize("message_factory", [_valid_approve, _valid_needs_fix])
def test_approve_or_needs_fix_are_allowed(tmp_path: Path, message_factory) -> None:
    message = message_factory(tmp_path) if message_factory is _valid_needs_fix else message_factory()
    assert emit_mod.validate_child_intermediate(message)["validation_status"] == "valid"

    result = _run_hook(_payload(message), tmp_path / "receipts")

    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "message",
    [
        "## Summary\nThe contract is ready.\n",
        lambda: "説明文\n" + _valid_approve(),
        lambda: _valid_approve() + "説明文\n",
        lambda: "```text\n" + _valid_approve() + "```\n",
        lambda: "\n".join(_valid_approve().splitlines()[:-1]) + "\n",
        lambda: "\n".join([_valid_approve().splitlines()[1], _valid_approve().splitlines()[0], *_valid_approve().splitlines()[2:]]) + "\n",
    ],
)
def test_rejects_invalid(tmp_path: Path, message) -> None:
    raw_message = message() if callable(message) else message
    result = _run_hook(_payload(raw_message), tmp_path / "receipts")

    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["decision"] == "block"
    assert output["reason"] == "canonical compact stdout をそのまま再生成してください。"
    assert raw_message not in result.stdout


def test_incident_fixture_blocks_markdown_summary_prose(tmp_path: Path) -> None:
    result = _run_hook(_payload("## Summary\nレビュー結果です。\n"), tmp_path / "receipts")

    assert result.returncode == 0
    assert json.loads(result.stdout)["decision"] == "block"


def test_settings_scopes_the_hook_to_issue_reviewer_only() -> None:
    settings = json.loads((REPO_ROOT / ".claude/settings.json").read_text(encoding="utf-8"))
    groups = settings["hooks"]["SubagentStop"]
    scoped = [group for group in groups if group.get("matcher") == "^issue-reviewer$"]

    assert len(scoped) == 1
    assert scoped[0]["hooks"] == [
        {
            "type": "command",
            "command": "python3",
            "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/validate_issue_reviewer_compact_output.py"],
            "timeout": 10,
        }
    ]


def test_other_subagent_is_not_decided_by_this_hook(tmp_path: Path) -> None:
    result = _run_hook(
        {
            "hook_event_name": "SubagentStop",
            "agent_type": "test-runner",
            "last_assistant_message": "free-form prose\n",
        },
        tmp_path / "receipts",
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_fail_close_missing_payload_and_retry_preserves_parent_boundary(tmp_path: Path) -> None:
    malformed_payload = {
        "hook_event_name": "SubagentStop",
        "agent_type": "issue-reviewer",
    }
    first = _run_hook(malformed_payload, tmp_path / "receipts")
    retry = _run_hook(_payload("free-form prose\n", stop_hook_active=True), tmp_path / "receipts")

    assert json.loads(first.stdout)["decision"] == "block"
    assert retry.returncode == 0
    assert json.loads(retry.stdout) == {"decision": "allow", "reason": "parent_fail_close_required"}


def test_does_not_mutate_last_assistant_message(tmp_path: Path) -> None:
    payload = _payload(_valid_approve())
    expected = copy.deepcopy(payload)

    result = _run_hook(payload, tmp_path / "receipts")

    assert result.returncode == 0
    assert payload == expected


def test_receipt_is_atomic_digest_only_and_records_initial_invalid(tmp_path: Path) -> None:
    raw_message = "## Summary\ninternal detail must not persist\n"
    receipts = tmp_path / "receipts"
    result = _run_hook(_payload(raw_message), receipts)

    assert json.loads(result.stdout)["decision"] == "block"
    receipt_files = list(receipts.glob("receipt-*.json"))
    assert len(receipt_files) == 1
    receipt_text = receipt_files[0].read_text(encoding="utf-8")
    receipt = json.loads(receipt_text)
    assert receipt["schema"] == "CLAUDE_SUBAGENT_RUNTIME_RECEIPT_V1"
    assert receipt["attempt"] == "initial"
    assert receipt["decision"] == "block"
    assert receipt["repo"] == "squne121/loop-protocol"
    assert receipt["issue"] == 1754
    assert receipt["pr"] == 1787
    assert len(receipt["head_sha"]) >= 40
    assert receipt["message_sha256"].startswith("sha256:")
    assert raw_message not in receipt_text
    assert "last_assistant_message" not in receipt
    assert not list(receipts.glob(".receipt-*"))


def test_retry_valid_and_retry_invalid_write_separate_receipts(tmp_path: Path) -> None:
    receipts = tmp_path / "receipts"
    valid = _run_hook(_payload(_valid_approve(), stop_hook_active=True), receipts)
    invalid = _run_hook(_payload("not canonical\n", stop_hook_active=True), receipts)

    assert valid.stdout == ""
    assert json.loads(invalid.stdout)["reason"] == "parent_fail_close_required"
    recorded = [json.loads(path.read_text(encoding="utf-8")) for path in receipts.glob("receipt-*.json")]
    assert len(recorded) == 2
    assert {item["attempt"] for item in recorded} == {"retry"}
    assert {item["decision"] for item in recorded} == {"allow"}
    assert {item["validation_status"] for item in recorded} == {"valid", "invalid"}
