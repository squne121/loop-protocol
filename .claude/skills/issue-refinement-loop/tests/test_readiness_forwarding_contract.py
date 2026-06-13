#!/usr/bin/env python3

from pathlib import Path


ROOT = Path(__file__).parent.parent.parent.parent
SKILL_MD = ROOT / "skills" / "issue-refinement-loop" / "SKILL.md"
ISSUE_AUTHOR_MD = ROOT / "agents" / "issue-author.md"
EDIT_ISSUE_MD = ROOT / "skills" / "edit-issue" / "SKILL.md"


def read(path: Path) -> str:
    assert path.exists(), f"missing file: {path}"
    return path.read_text(encoding="utf-8")


def test_step4_runs_pre_author_preflight_static_check():
    text = read(SKILL_MD)
    assert "contract_readiness_check.py" in text
    assert "--mode preflight-static" in text
    assert "--body-file <current_body_file>" in text


def test_step4_declares_readiness_forwarding_payload_fields():
    text = read(SKILL_MD)
    for fragment in [
        "READINESS_FORWARDING_PAYLOAD_V1",
        "status: go | needs_fix | human_judgment | input_or_runtime_error",
        "body_sha256:",
        "source_checks:",
        "errors: []",
        "readiness_result_ref:",
        "unexpected_pass",
    ]:
        assert fragment in text, f"missing fragment in Step 4 contract: {fragment}"


def test_step4_routes_all_readiness_statuses():
    text = read(SKILL_MD)
    for fragment in [
        "exit_code_0:",
        "action: invoke_issue_author",
        "readiness_errors: []",
        "exit_code_1:",
        "action: invoke_issue_author_with_readiness_result",
        "exit_code_2:",
        "action: skip_issue_author_and_go_step5",
        "exit_code_3:",
        "action: human_escalation",
    ]:
        assert fragment in text, f"missing routing fragment: {fragment}"


def test_issue_author_consumes_readiness_forwarding_payload():
    text = read(ISSUE_AUTHOR_MD)
    for fragment in [
        "readiness_forwarding_payload",
        "READINESS_FORWARDING_PAYLOAD_V1",
        "status: go | needs_fix | human_judgment | input_or_runtime_error",
        "`status: go`",
        "`status: needs_fix`",
        "`status: human_judgment` または `status: input_or_runtime_error`",
    ]:
        assert fragment in text, f"missing issue-author consumer fragment: {fragment}"


def test_edit_issue_consumes_readiness_forwarding_payload():
    text = read(EDIT_ISSUE_MD)
    for fragment in [
        "readiness_forwarding_payload",
        "READINESS_FORWARDING_PAYLOAD_V1",
        "status: go | needs_fix | human_judgment | input_or_runtime_error",
        "status: go` の場合は pre-author static readiness blocker なし",
        "status: needs_fix` の場合は `errors[]` と `readiness_result_ref`",
        "status: human_judgment | input_or_runtime_error` の場合は fail-closed",
    ]:
        assert fragment in text, f"missing edit-issue consumer fragment: {fragment}"
