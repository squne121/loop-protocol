from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_PATH = __import__("pathlib").Path(__file__).resolve().parent.parent / "scripts" / "open_pr.py"


def load_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("open_pr", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _sample_pr_body(linked_issue: int = 1861, change_kind: str = "mixed", linked_action: str = "Closes") -> str:
    return f"""## Linked Issue
{linked_action} #{linked_issue}
change_kind: {change_kind}

## Parent Goal Ref
- none

## Summary
- full body for test

## Current Validated Scope
- none

## Remaining Parent Gaps
- none

## Normalized Findings
- normalized findings: なし
- finding_id: なし
- source:

## Acceptance Criteria -> Evidence
- AC1:
  - Evidence:
- AC2:
  - Evidence:
- AC3:
  - Evidence:
- AC4:
  - Evidence:
- AC5:
  - Evidence:

## Commands Run
- none

## Changed Paths
- .agents/skills/open-pr/SKILL.md

## Risks
none

## Rollback
n/a

## Follow-ups Intentionally Deferred
none

## 類似 Issue 統合方針
none

## Knowledge Harvesting
none

## Process / Skill / Agent Improvements
- none

## Renumbering / Identifier Migration
none

## Long-form Evidence
- n/a
"""


def test_open_pr_success_pr_create(monkeypatch, capsys):
    module = load_module()
    _result_cmd: dict[str, list[str]] = {}

    def fake_run(args, check=False, text=True, capture_output=True):
        command = list(args)
        if command == ["git", "branch", "--show-current"]:
            return _result(0, "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper\n")
        if command[:3] == ["gh", "issue", "view"]:
            return _result(0, json.dumps({"state": "OPEN"}))
        if command[:3] == ["gh", "pr", "list"] and command[3] == "--head":
            return _result(0, "[]")
        if command[:3] == ["gh", "pr", "list"] and "--search" in command:
            return _result(0, "[]")
        if command[:2] == ["gh", "api"]:
            return _result(0, json.dumps({"name": "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper"}))
        if command[:3] == ["gh", "pr", "create"]:
            _result_cmd["argv"] = command
            assert command == [
                "gh",
                "pr",
                "create",
                "--draft",
                "--title",
                "feat: test",
                "--body-file",
                command[7],
            ]
            body = Path(command[7]).read_text(encoding="utf-8")
            assert "## Acceptance Criteria -> Evidence" in body
            assert "## Normalized Findings" in body
            assert "change_kind: mixed" in body
            assert "Closes #1861" in body
            assert "--json" not in command
            return _result(0, "https://github.com/example/repo/pull/123")
        return _result()

    monkeypatch.setattr(module, "_run_command", fake_run)
    code = module.main([
        "--publish",
        "yes",
        "--pr_title",
        "feat: test",
        "--linked_issue",
        "1861",
        "--pr_body",
        _sample_pr_body(),
    ])
    out = capsys.readouterr()

    assert _result_cmd["argv"] == [
        "gh",
        "pr",
        "create",
        "--draft",
        "--title",
        "feat: test",
        "--body-file",
        _result_cmd["argv"][7],
    ]
    assert code == 0
    assert "PR_URL=https://github.com/example/repo/pull/123" in out.out
    assert "CANONICAL_PR_URL=https://github.com/example/repo/pull/123" in out.out
    assert "CANONICAL_PR_SOURCE=new-pr" in out.out
    assert "LINKED_ISSUE_ACTION=Closes" in out.out


def test_open_pr_existing_pr_idempotent(monkeypatch, capsys):
    module = load_module()

    def fake_run(args, check=False, text=True, capture_output=True):
        command = list(args)
        if command == ["git", "branch", "--show-current"]:
            return _result(0, "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper\n")
        if command[:3] == ["gh", "issue", "view"]:
            return _result(0, json.dumps({"state": "OPEN"}))
        if command[:3] == ["gh", "pr", "list"] and command[3] == "--head":
            return _result(0, json.dumps([
                {
                    "url": "https://github.com/example/repo/pull/333",
                    "state": "OPEN",
                    "headRefName": "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper",
                }
            ]))
        if command[:3] == ["gh", "pr", "list"] and "--search" in command:
            return _result(0, "[]")
        if command[:3] == ["gh", "pr", "create"]:
            pytest.fail("should not create when existing PR exists")
        return _result()

    monkeypatch.setattr(module, "_run_command", fake_run)
    code = module.main([
        "--publish",
        "yes",
        "--pr_title",
        "feat: test",
        "--linked_issue",
        "1861",
        "--pr_body",
        _sample_pr_body(),
        "--superseded_prs",
        "[]",
    ])
    out = capsys.readouterr()

    assert code == 0
    assert "PR_URL=https://github.com/example/repo/pull/333 (existing)" in out.out
    assert "EXISTING_PR_BODY_UPDATED=false" in out.out
    assert "SUPERSEDED_PRS=none" in out.out


def test_open_pr_downgrade_closed_issue(monkeypatch, capsys):
    module = load_module()

    def fake_run(args, check=False, text=True, capture_output=True):
        command = list(args)
        if command == ["git", "branch", "--show-current"]:
            return _result(0, "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper\n")
        if command[:3] == ["gh", "issue", "view"]:
            return _result(0, json.dumps({"state": "CLOSED"}))
        if command[:3] == ["gh", "pr", "list"] and command[3] == "--head":
            return _result(0, "[]")
        if command[:3] == ["gh", "pr", "list"] and "--search" in command:
            return _result(0, "[]")
        if command[:2] == ["gh", "api"]:
            return _result(0, json.dumps({"name": "feat/issue-1860-follow-up"}))
        if command[:3] == ["gh", "pr", "create"]:
            body = Path(command[7]).read_text(encoding="utf-8")
            assert "Refs #1860" in body
            return _result(0, "https://github.com/example/repo/pull/444")
        return _result()

    monkeypatch.setattr(module, "_run_command", fake_run)
    code = module.main([
        "--publish",
        "yes",
        "--pr_title",
        "feat: test",
        "--linked_issue",
        "1860",
        "--pr_body",
        _sample_pr_body(1860),
    ])
    out = capsys.readouterr()

    assert code == 0
    assert "WARN_DOWNGRADE=Closes->Refs" in out.out
    assert "LINKED_ISSUE_ACTION=Refs" in out.out
    assert "PR_URL=https://github.com/example/repo/pull/444" in out.out


def test_open_pr_template_guard_failure(monkeypatch, capsys):
    module = load_module()

    body = """## Linked Issue
Closes #1861

## Summary
- incomplete
"""

    def fake_run(args, check=False, text=True, capture_output=True):
        if args == ["git", "branch", "--show-current"]:
            return _result(0, "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper\n")
        if args[:3] == ["gh", "issue", "view"]:
            return _result(0, json.dumps({"state": "OPEN"}))
        return _result()

    monkeypatch.setattr(module, "_run_command", fake_run)
    code = module.main(["--publish", "yes", "--pr_title", "feat: test", "--linked_issue", "1861", "--pr_body", body])
    out = capsys.readouterr()

    assert code == 1
    assert "ERROR=E_PR_TEMPLATE_GUARD" in out.out
    assert "MISSING_SECTIONS=" in out.out
    assert "PREFLIGHT_CHECK=template-required-sections" in out.out
    assert "DIAGNOSTIC_KIND=template-preflight-failure" in out.out


def test_open_pr_template_drift_check_failure(monkeypatch, capsys):
    module = load_module()
    body = _sample_pr_body()

    def fake_run(args, check=False, text=True, capture_output=True):
        if (
            args
            and args[0] == sys.executable
            and str(args[1]).endswith("scripts/sync-pr-evidence-template.py")
            and args[2:] == ["--check"]
        ):
            return _result(
                1,
                "",
                "ERROR: PR evidence mirror drift detected.",
            )
        return _result()

    monkeypatch.setattr(module, "_run_command", fake_run)
    code = module.main([
        "--publish",
        "yes",
        "--pr_title",
        "feat: test",
        "--linked_issue",
        "1861",
        "--pr_body",
        body,
    ])
    out = capsys.readouterr()

    assert code == 1
    assert "ERROR=E_PR_TEMPLATE_GUARD" in out.out
    assert "PREFLIGHT_CHECK=template-evidence-drift-check" in out.out
    assert "ERROR_DETAIL=template-drift-check-failed" in out.out
    assert "DIAGNOSTIC_KIND=template-preflight-failure" in out.out


def test_open_pr_canonical_ambiguous_without_strong_match(monkeypatch, capsys):
    module = load_module()

    issue_prs = [
        {
            "url": "https://github.com/example/repo/pull/1000",
            "title": "Fix unrelated issue",
            "body": "## Summary\n- touched #1861 in prose only",
            "state": "OPEN",
        },
    ]

    def fake_run(args, check=False, text=True, capture_output=True):
        command = list(args)
        if command == ["git", "branch", "--show-current"]:
            return _result(0, "feat/issue-ambiguous\n")
        if command[:3] == ["gh", "issue", "view"]:
            return _result(0, json.dumps({"state": "OPEN"}))
        if command[:3] == ["gh", "pr", "list"] and command[3] == "--head":
            return _result(0, "[]")
        if command[:3] == ["gh", "pr", "list"] and "--search" in command:
            return _result(0, json.dumps(issue_prs))
        return _result()

    monkeypatch.setattr(module, "_run_command", fake_run)
    code = module.main(["--publish", "yes", "--pr_title", "feat: test", "--linked_issue", "1861", "--pr_body", _sample_pr_body()])
    out = capsys.readouterr()

    assert code == 1
    assert "ERROR=E_CANONICAL_PR_AMBIGUOUS" in out.out


def test_open_pr_canonical_match_positive(monkeypatch, capsys):
    module = load_module()

    issue_prs = [
        {
            "url": "https://github.com/example/repo/pull/1100",
            "title": "feat: fix linked issue flow",
            "body": "## Linked Issue\nCloses #1861\nchange_kind: mixed\n\n## Summary\n- already linked by body",
            "state": "OPEN",
            "headRefName": "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper",
        },
    ]

    def fake_run(args, check=False, text=True, capture_output=True):
        command = list(args)
        if command == ["git", "branch", "--show-current"]:
            return _result(0, "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper\n")
        if command[:3] == ["gh", "issue", "view"]:
            return _result(0, json.dumps({"state": "OPEN"}))
        if command[:3] == ["gh", "pr", "list"] and command[3] == "--head":
            return _result(0, "[]")
        if command[:3] == ["gh", "pr", "list"] and "--search" in command:
            return _result(0, json.dumps(issue_prs))
        if command[:3] == ["gh", "pr", "create"]:
            pytest.fail("should not create when same-issue canonical PR exists")
        return _result()

    monkeypatch.setattr(module, "_run_command", fake_run)
    code = module.main([
        "--publish",
        "yes",
        "--pr_title",
        "feat: test",
        "--linked_issue",
        "1861",
        "--pr_body",
        _sample_pr_body(),
    ])
    out = capsys.readouterr()

    assert code == 0
    assert "CANONICAL_PR_SOURCE=same-issue-open-pr" in out.out
    assert "PR_URL=https://github.com/example/repo/pull/1100 (existing)" in out.out
    assert "CANONICAL_PR_URL=https://github.com/example/repo/pull/1100" in out.out


def test_pr_matches_linked_issue_regex_strong_matches_title_and_head():
    module = load_module()
    assert module._pr_matches_linked_issue({
        "title": "fix issue #1861 regression",
        "headRefName": "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper",
        "body": "## Summary\n- no linked issue",
    }, "1861")
    assert not module._pr_matches_linked_issue({
        "title": "fix issue #18610 regression",
        "headRefName": "feat/issue-18610-follow-up-skills-open-pr-cli-wrapper",
        "body": "## Summary\n- no linked issue",
    }, "1861")
    assert module._pr_matches_linked_issue({
        "title": "fix issue flow",
        "headRefName": "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper",
        "body": "## Summary\n- no linked issue",
    }, "1861")
    assert not module._pr_matches_linked_issue({
        "title": "fix issue flow",
        "headRefName": "feat/issue-18610-follow-up-skills-open-pr-cli-wrapper",
        "body": "## Summary\n- no linked issue",
    }, "1861")


def test_open_pr_repair_context_create_replacement(monkeypatch, capsys):
    module = load_module()
    previous_url = "https://github.com/example/repo/pull/2000"

    issue_prs = [
        {"url": previous_url, "title": "wip", "body": "## Linked Issue\nCloses #1861", "state": "OPEN"},
    ]

    def fake_run(args, check=False, text=True, capture_output=True):
        command = list(args)
        if command == ["git", "branch", "--show-current"]:
            return _result(0, "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper\n")
        if command[:3] == ["gh", "issue", "view"]:
            return _result(0, json.dumps({"state": "OPEN"}))
        if command[:3] == ["gh", "pr", "list"] and command[3] == "--head":
            return _result(0, "[]")
        if command[:3] == ["gh", "pr", "list"] and "--search" in command:
            return _result(0, json.dumps(issue_prs))
        if command[:2] == ["gh", "api"]:
            return _result(0, json.dumps({"name": "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper"}))
        if command[:3] == ["gh", "pr", "create"]:
            body = Path(command[7]).read_text(encoding="utf-8")
            assert "Closes #1861" in body
            return _result(0, "https://github.com/example/repo/pull/2001")
        return _result()

    monkeypatch.setattr(module, "_run_command", fake_run)
    repair_context = json.dumps({
        "previous_pr_url": previous_url,
        "mode": "create-replacement",
        "reason": "retry",
    })
    code = module.main([
        "--publish",
        "yes",
        "--pr_title",
        "feat: test",
        "--linked_issue",
        "1861",
        "--pr_body",
        _sample_pr_body(),
        "--repair_context",
        repair_context,
    ])
    out = capsys.readouterr()

    assert code == 0
    assert "CANONICAL_PR_SOURCE=repair-replacement" in out.out
    assert "SUPERSEDED_PR_URL=https://github.com/example/repo/pull/2000" in out.out


def test_open_pr_repair_context_create_replacement_ambiguous(monkeypatch, capsys):
    module = load_module()
    previous_url = "https://github.com/example/repo/pull/2000"

    issue_prs = [
        {"url": previous_url, "title": "wip", "body": "## Linked Issue\nCloses #1861", "state": "OPEN"},
        {
            "url": "https://github.com/example/repo/pull/2002",
            "title": "feat: issue #1861 canonical",
            "body": "## Summary\n- canonical",
            "state": "OPEN",
            "headRefName": "feat/issue-1861-canonical",
        },
    ]

    def fake_run(args, check=False, text=True, capture_output=True):
        command = list(args)
        if command == ["git", "branch", "--show-current"]:
            return _result(0, "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper\n")
        if command[:3] == ["gh", "issue", "view"]:
            return _result(0, json.dumps({"state": "OPEN"}))
        if command[:3] == ["gh", "pr", "list"] and command[3] == "--head":
            return _result(0, "[]")
        if command[:3] == ["gh", "pr", "list"] and "--search" in command:
            return _result(0, json.dumps(issue_prs))
        if command[:3] == ["gh", "pr", "create"]:
            pytest.fail("should not create when replacement is ambiguous")
        return _result()

    monkeypatch.setattr(module, "_run_command", fake_run)
    repair_context = json.dumps({
        "previous_pr_url": previous_url,
        "mode": "create-replacement",
        "reason": "retry",
    })
    code = module.main([
        "--publish",
        "yes",
        "--pr_title",
        "feat: test",
        "--linked_issue",
        "1861",
        "--pr_body",
        _sample_pr_body(),
        "--repair_context",
        repair_context,
    ])
    out = capsys.readouterr()

    assert code == 1
    assert "ERROR=E_CANONICAL_PR_AMBIGUOUS" in out.out


def test_open_pr_branch_not_found(monkeypatch, capsys):
    module = load_module()

    def fake_run(args, check=False, text=True, capture_output=True):
        command = list(args)
        if command == ["git", "branch", "--show-current"]:
            return _result(0, "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper\n")
        if command[:3] == ["gh", "issue", "view"]:
            return _result(0, json.dumps({"state": "OPEN"}))
        if command[:3] == ["gh", "pr", "list"] and command[3] == "--head":
            return _result(0, "[]")
        if command[:3] == ["gh", "pr", "list"] and "--search" in command:
            return _result(0, "[]")
        if command[:2] == ["gh", "api"]:
            return _result(1, "", "branch not found")
        if command[:3] == ["gh", "pr", "create"]:
            pytest.fail("should not create when branch is not published")
        return _result()

    monkeypatch.setattr(module, "_run_command", fake_run)
    code = module.main([
        "--publish",
        "yes",
        "--pr_title",
        "feat: test",
        "--linked_issue",
        "1861",
        "--pr_body",
        _sample_pr_body(),
    ])
    out = capsys.readouterr()

    assert code == 1
    assert "ERROR=E_BRANCH_NOT_FOUND" in out.out


def test_open_pr_json_command_failure_diagnostics(monkeypatch, capsys):
    module = load_module()

    def fake_run(args, check=False, text=True, capture_output=True):
        command = list(args)
        if command[:3] == ["gh", "issue", "view"]:
            return _result(1, "", "HTTP 500")
        return _result(0, "")

    monkeypatch.setattr(module, "_run_command", fake_run)
    code = module.main([
        "--publish",
        "yes",
        "--pr_title",
        "feat: test",
        "--linked_issue",
        "1861",
        "--pr_body",
        _sample_pr_body(),
    ])
    out = capsys.readouterr()

    assert code == 1
    assert "ERROR=E_GH_COMMAND_FAILED" in out.out
    assert "DIAGNOSTIC_STAGE=linked-issue-state" in out.out
    assert "DIAGNOSTIC_KIND=json-command-failure" in out.out
    assert "ERROR_DETAIL=non-zero-exit" in out.out
    assert "FAILED_COMMAND=gh issue view 1861 ..." in out.out
    assert "COMMAND_STDERR=HTTP 500" in out.out
    assert "open-pr json-command-failure stage=linked-issue-state" in out.err


def test_open_pr_json_parse_failure_diagnostics(monkeypatch, capsys):
    module = load_module()

    def fake_run(args, check=False, text=True, capture_output=True):
        command = list(args)
        if command[:3] == ["gh", "issue", "view"]:
            return _result(0, "{not-json}", "")
        return _result(0, "")

    monkeypatch.setattr(module, "_run_command", fake_run)
    code = module.main([
        "--publish",
        "yes",
        "--pr_title",
        "feat: test",
        "--linked_issue",
        "1861",
        "--pr_body",
        _sample_pr_body(),
    ])
    out = capsys.readouterr()

    assert code == 1
    assert "ERROR=E_GH_COMMAND_FAILED" in out.out
    assert "DIAGNOSTIC_STAGE=linked-issue-state" in out.out
    assert "DIAGNOSTIC_KIND=json-parse-failure" in out.out
    assert "ERROR_DETAIL=invalid-json-output" in out.out
    assert "FAILED_COMMAND=gh issue view 1861 ..." in out.out
    assert "open-pr json-parse-failure stage=linked-issue-state" in out.err


def test_open_pr_non_json_failure_diagnostics(monkeypatch, capsys):
    module = load_module()

    def fake_run(args, check=False, text=True, capture_output=True):
        command = list(args)
        if command == ["git", "branch", "--show-current"]:
            return _result(0, "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper\n")
        if command[:3] == ["gh", "issue", "view"]:
            return _result(0, json.dumps({"state": "OPEN"}))
        if command[:3] == ["gh", "pr", "list"] and command[3] == "--head":
            return _result(0, "[]")
        if command[:3] == ["gh", "pr", "list"] and "--search" in command:
            return _result(0, "[]")
        if command[:2] == ["gh", "api"]:
            return _result(0, json.dumps({"name": "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper"}))
        if command[:3] == ["gh", "pr", "create"]:
            return _result(1, "", "GraphQL create failed")
        return _result()

    monkeypatch.setattr(module, "_run_command", fake_run)
    code = module.main([
        "--publish",
        "yes",
        "--pr_title",
        "feat: test",
        "--linked_issue",
        "1861",
        "--pr_body",
        _sample_pr_body(),
    ])
    out = capsys.readouterr()

    assert code == 1
    assert "ERROR=E_GH_PR_CREATE_FAILED" in out.out
    assert "DIAGNOSTIC_STAGE=pr-create" in out.out
    assert "DIAGNOSTIC_KIND=non-json-command-failure" in out.out
    assert "ERROR_DETAIL=gh-pr-create-non-zero-exit" in out.out
    assert "COMMAND_STDERR=GraphQL create failed" in out.out
    assert "open-pr non-json-command-failure stage=pr-create" in out.err


def test_open_pr_non_json_failure_diagnostics_escape_multiline_stderr(monkeypatch, capsys):
    module = load_module()

    def fake_run(args, check=False, text=True, capture_output=True):
        command = list(args)
        if command == ["git", "branch", "--show-current"]:
            return _result(0, "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper\n")
        if command[:3] == ["gh", "issue", "view"]:
            return _result(0, json.dumps({"state": "OPEN"}))
        if command[:3] == ["gh", "pr", "list"] and command[3] == "--head":
            return _result(0, "[]")
        if command[:3] == ["gh", "pr", "list"] and "--search" in command:
            return _result(0, "[]")
        if command[:2] == ["gh", "api"]:
            return _result(0, json.dumps({"name": "feat/issue-1861-follow-up-skills-open-pr-cli-wrapper"}))
        if command[:3] == ["gh", "pr", "create"]:
            return _result(1, "", "line1\nERROR=spoof\r\nline=2")
        return _result()

    monkeypatch.setattr(module, "_run_command", fake_run)
    code = module.main([
        "--publish",
        "yes",
        "--pr_title",
        "feat: test",
        "--linked_issue",
        "1861",
        "--pr_body",
        _sample_pr_body(),
    ])
    out = capsys.readouterr()

    assert code == 1
    assert "COMMAND_STDERR=line1\\nERROR\\x3dspoof\\r\\nline\\x3d2" in out.out


def test_open_pr_argument_parsing_failure_emits_error_contract(capsys):
    module = load_module()

    code = module.main([
        "--publish",
        "yes",
        "--pr_title",
        "feat: test",
        "--linked_issue",
        "1861",
        "--pr_body",
        _sample_pr_body(),
        "--dry_run",
        "not-a-bool",
    ])
    out = capsys.readouterr()

    assert code == 1
    assert "ERROR=E_OPEN_PR_ARGUMENT_INVALID" in out.out
    assert "DIAGNOSTIC_STAGE=argument-parsing" in out.out
    assert "DIAGNOSTIC_KIND=input-validation-failure" in out.out
    assert "ERROR_DETAIL=argument-parse-error" in out.out


def test_open_pr_missing_pr_body_uses_template_preflight_contract(capsys):
    module = load_module()

    code = module.main([
        "--publish",
        "yes",
        "--pr_title",
        "feat: test",
        "--linked_issue",
        "1861",
    ])
    out = capsys.readouterr()

    assert code == 1
    assert "ERROR=E_PR_TEMPLATE_GUARD" in out.out
    assert "PREFLIGHT_CHECK=template-required-sections" in out.out
    assert "ERROR_DETAIL=missing-pr-body" in out.out
