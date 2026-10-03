#!/usr/bin/env python3
"""Tests for validate_pr_body.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from validate_pr_body import REQUIRED_SECTIONS, validate_pr_body


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pr_body"
SCRIPT_PATH = Path(__file__).parent.parent / "validate_pr_body.py"
REPO_ROOT = Path(__file__).resolve().parents[5]
EVIDENCE_SECTIONS = ("受け入れ条件の達成状況", "検証コマンド結果", "Allowed Paths 遵守")


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def load_paths(name: str) -> list[str]:
    return [
        line.strip()
        for line in (FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_lp052_required_sections():
    result = validate_pr_body(load_fixture("missing_summary.md"), load_paths("non_safety_paths.txt"))
    errors = [error for error in result.errors if error.rule_id == "LP052"]
    assert result.status == "fail"
    assert any("Summary" in error.message for error in errors)


def test_lp053_schema_decision_invalid():
    result = validate_pr_body(load_fixture("invalid_schema_decision.md"), load_paths("non_safety_paths.txt"))
    errors = [error for error in result.errors if error.rule_id == "LP053"]
    assert result.status == "fail"
    assert len(errors) == 1


@pytest.mark.parametrize(
    "fixture_name",
    [
        "schema_change_missing_inventory.md",
        "schema_change_placeholder_inventory.md",
        "uncertain_missing_inventory.md",
    ],
)
def test_lp050_schema_inventory_required(fixture_name: str):
    result = validate_pr_body(load_fixture(fixture_name), load_paths("non_safety_paths.txt"))
    errors = [error for error in result.errors if error.rule_id == "LP050"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_not_schema_change_inventory_na():
    result = validate_pr_body(
        load_fixture("not_schema_change_with_na_inventory.md"),
        load_paths("non_safety_paths.txt")
    )
    lp050_errors = [error for error in result.errors if error.rule_id == "LP050"]
    assert result.status == "pass"
    assert lp050_errors == []


def test_lp051_safety_matrix_required():
    result = validate_pr_body(load_fixture("safety_sensitive_missing_matrix.md"), load_paths("safety_paths.txt"))
    errors = [error for error in result.errors if error.rule_id == "LP051"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_lp055_safety_matrix_columns_invalid():
    result = validate_pr_body(load_fixture("safety_matrix_missing_columns.md"), load_paths("safety_paths.txt"))
    errors = [error for error in result.errors if error.rule_id == "LP055"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_lp056_safety_followup_required():
    result = validate_pr_body(
        load_fixture("safety_matrix_not_controlled_without_followup.md"),
        load_paths("safety_paths.txt")
    )
    errors = [error for error in result.errors if error.rule_id == "LP056"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_lp057_related_issue_required():
    result = validate_pr_body(load_fixture("related_issue_missing.md"), load_paths("non_safety_paths.txt"))
    errors = [error for error in result.errors if error.rule_id == "LP057"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_lp058_changed_paths_unavailable():
    result = validate_pr_body(
        load_fixture("changed_paths_unavailable.md")
        if (FIXTURE_DIR / "changed_paths_unavailable.md").exists()
        else load_fixture("valid_not_schema_change.md"),
        None
    )
    errors = [error for error in result.errors if error.rule_id == "LP058"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_minimal_context_limits():
    long_line = "x" * 3000
    body = f"""## Summary

- context

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: maybe
- reason: invalid

## Schema Consumer Inventory

N/A
reason: invalid

## Safety Claim Matrix

N/A
reason: invalid

## Notes

- Related issue: #244

{long_line}
"""
    result = validate_pr_body(body, load_paths("non_safety_paths.txt"))
    assert result.errors
    for error in result.errors:
        context = "\n".join(error.minimal_context)
        assert len(error.minimal_context) <= 5
        assert len(context.encode("utf-8")) <= 2048


def test_cli_returns_loop_body_lint_v1_json():
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False) as body_file:
        body_file.write(load_fixture("valid_not_schema_change.md"))
        body_path = body_file.name
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--body-file",
                body_path,
                "--changed-paths-file",
                str(FIXTURE_DIR / "non_safety_paths.txt"),
                "--linked-issue",
                "330",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["schema"] == "loop_body_lint/v1"
        assert payload["target"] == "pr"
        assert payload["status"] == "pass"
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_b1_cli_body_file_not_found():
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--body-file",
            "/tmp/does-not-exist.md",
            "--linked-issue",
            "330",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "ERROR" in result.stderr
    assert "Cannot read body file" in result.stderr


def test_b1_cli_changed_paths_file_not_found():
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False) as body_file:
        body_file.write(load_fixture("valid_not_schema_change.md"))
        body_path = body_file.name
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--body-file",
                body_path,
                "--changed-paths-file",
                "/tmp/missing.txt",
                "--linked-issue",
                "330",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "ERROR" in result.stderr
        assert "Cannot read changed-paths file" in result.stderr
    finally:
        Path(body_path).unlink(missing_ok=True)


def test_b2_lp057_requires_matching_linked_issue():
    body = """## Summary

- test

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A

## Safety Claim Matrix

N/A

## Notes

- Related issue: #244
- Closes #244
"""
    result = validate_pr_body(body, load_paths("non_safety_paths.txt"), linked_issue=330)
    errors = [error for error in result.errors if error.rule_id == "LP057"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_b2_lp057_accepts_matching_linked_issue():
    body = """## Summary

- test

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A

## Safety Claim Matrix

N/A

## Notes

- Related issue: #330
- Closes #330
"""
    result = validate_pr_body(body, load_paths("non_safety_paths.txt"), linked_issue=330)
    errors = [error for error in result.errors if error.rule_id == "LP057"]
    assert len(errors) == 0


def test_n1_empty_changed_paths_treated_as_unavailable():
    result = validate_pr_body(load_fixture("valid_not_schema_change.md"), [])
    errors = [error for error in result.errors if error.rule_id == "LP058"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_safety_sensitive_na_reason_fails_lp051():
    body = """## Summary

- test

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A

## Safety Claim Matrix

N/A
reason: not needed

## Notes

- Related issue: #330
"""
    result = validate_pr_body(body, [".github/workflows/ci.yml"], linked_issue=330)
    errors = [error for error in result.errors if error.rule_id == "LP051"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_safety_sensitive_na_reason_fails_lp055():
    body = """## Summary

- test

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A

## Safety Claim Matrix

N/A
reason: not needed

## Notes

- Related issue: #330
"""
    result = validate_pr_body(body, [".claude/skills/open-pr/validate_pr_body.py"], linked_issue=330)
    errors = [error for error in result.errors if error.rule_id == "LP055"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_non_safety_sensitive_na_reason_passes():
    body = """## Summary

- docs-only change

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A

## Safety Claim Matrix

N/A
reason: docs-only change, no safety controls affected

## Notes

- Related issue: #330
"""
    result = validate_pr_body(body, ["docs/dev/foo.md"], linked_issue=330)
    safety_rule_ids = {error.rule_id for error in result.errors} & {"LP051", "LP055", "LP056"}
    assert not safety_rule_ids


def test_safety_claims_v1_yaml_contract():
    body = """## Summary

- test

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A
reason: none

## Safety Claim Matrix

```yaml
# SAFETY_CLAIMS_V1
safety_claims:
  - claim: Restrict claim scope
    implemented: "yes"
    evidence:
      - rg -n \"claim\" .
```

## Notes

- Related issue: #330

## 受け入れ条件の達成状況

- [x] AC1: 達成（fixture）

## 検証コマンド結果

```text
$ pnpm typecheck
pass
```

## Allowed Paths 遵守

- 変更ファイル: fixture のみ
- Allowed Paths 逸脱: なし
"""
    result = validate_pr_body(body, [".github/workflows/ci.yml"], linked_issue=330)
    assert result.status == "pass"


def test_unsafe_yaml_tag():
    body = """## Summary

- test

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A
reason: none

## Safety Claim Matrix

```yaml
# SAFETY_CLAIMS_V1
safety_claims: !!python/object/apply:os.system [\"echo nope\"]
```

## Notes

- Related issue: #330
"""
    result = validate_pr_body(body, [".github/workflows/ci.yml"], linked_issue=330)
    errors = [error for error in result.errors if error.rule_id == "E_SAFETY_CLAIMS_PARSE_ERROR"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_follow_up_missing_contract():
    body = """## Summary

- test

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A
reason: none

## Safety Claim Matrix

```yaml
# SAFETY_CLAIMS_V1
safety_claims:
  - claim: Narrow safety claim
    implemented: "partial"
    not_controlled:
      - Native tool registry
    evidence:
      - rg -n \"claim\" .
    follow_up:
      - TBD
```

## Notes

- Related issue: #330
"""
    result = validate_pr_body(body, [".github/workflows/ci.yml"], linked_issue=330)
    errors = [error for error in result.errors if error.rule_id == "E_FOLLOW_UP_MISSING_CONTRACT"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_markdown_table_backward_compat():
    result = validate_pr_body(
        load_fixture("valid_not_schema_change.md"),
        load_paths("non_safety_paths.txt"),
        linked_issue=330
    )
    assert result.status == "pass"


def test_fenced_code_heading_guard():
    fence = chr(96) * 3
    body = (
        "## Summary\n\n"
        "- test\n\n"
        "## Checks\n\n"
        + fence + "md\n## Schema Change Applicability\n" + fence + "\n\n"
        "## Schema Change Applicability\n\n"
        "- decision: not_schema_change\n\n"
        "## Schema Consumer Inventory\n\n"
        "N/A\nreason: none\n\n"
        "## Safety Claim Matrix\n\n"
        "N/A\nreason: docs only\n\n"
        "## Notes\n\n"
        "- Related issue: #330\n\n"
        "## 受け入れ条件の達成状況\n\n"
        "- [x] AC1: 達成（fixture）\n\n"
        "## 検証コマンド結果\n\n"
        + fence + "text\n$ pnpm typecheck\npass\n" + fence + "\n\n"
        "## Allowed Paths 遵守\n\n"
        "- 変更ファイル: fixture のみ\n"
        "- Allowed Paths 逸脱: なし\n"
    )
    result = validate_pr_body(body, load_paths("non_safety_paths.txt"), linked_issue=330)
    errors = [error for error in result.errors if error.rule_id == "LP052"]
    assert result.status == "pass"
    assert not errors


def test_duplicate_heading_guard():
    body = """## Summary

- test

## Checks

- check

## Schema Change Applicability

- decision: not_schema_change

## Schema Consumer Inventory

N/A
reason: none

## Safety Claim Matrix

N/A
reason: docs only

## Notes

- Related issue: #330

## Notes

- Related issue: #330
"""
    result = validate_pr_body(body, load_paths("non_safety_paths.txt"), linked_issue=330)
    errors = [error for error in result.errors if error.rule_id == "LP054"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_follow_up_missing_contract_when_omitted():
    fence = chr(96) * 3
    body = (
        "## Summary\n\n"
        "- test\n\n"
        "## Checks\n\n"
        "- check\n\n"
        "## Schema Change Applicability\n\n"
        "- decision: not_schema_change\n\n"
        "## Schema Consumer Inventory\n\n"
        "N/A\nreason: none\n\n"
        "## Safety Claim Matrix\n\n"
        + fence + (
            "yaml\n# SAFETY_CLAIMS_V1\nsafety_claims:\n  - claim: Narrow safety claim\n    implemented: \"partial\"\n "
            "   not_controlled:\n      - Native tool registry\n    evidence:\n      - rg -n \"claim\" .\n"
        ) + fence + "\n\n"
        "## Notes\n\n"
        "- Related issue: #330\n"
    )
    result = validate_pr_body(body, [".github/workflows/ci.yml"], linked_issue=330)
    errors = [error for error in result.errors if error.rule_id == "E_FOLLOW_UP_MISSING_CONTRACT"]
    assert result.status == "fail"
    assert len(errors) == 1


def _evidence_sections_block() -> str:
    return (
        "## 受け入れ条件の達成状況\n\n"
        "- [x] AC1: 達成（fixture）\n\n"
        "## 検証コマンド結果\n\n"
        "```text\n$ pnpm typecheck\npass\n```\n\n"
        "## Allowed Paths 遵守\n\n"
        "- 変更ファイル: fixture のみ\n"
        "- Allowed Paths 逸脱: なし\n"
    )


def test_ac1_required_sections_include_evidence_headings():
    for section in EVIDENCE_SECTIONS:
        assert section in REQUIRED_SECTIONS


def test_ac2_legacy_body_missing_evidence_sections_fails_lp052():
    # #2806-equivalent legacy body: satisfies the old (pre-#2808) LP052 six-section
    # inventory, but is missing the three reviewer-required evidence sections.
    body = """## Summary

- summarize_agent_transcript.py の GitHub token redaction を github_pat_ 対応にする

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change
- reason: parser のみ変更

## Schema Consumer Inventory

N/A
reason: schema を変更しない

## Safety Claim Matrix

N/A
reason: docs only

## Notes

- Related issue: #2806
"""
    result = validate_pr_body(body, load_paths("non_safety_paths.txt"), linked_issue=2806)
    lp052_messages = {error.message for error in result.errors if error.rule_id == "LP052"}
    assert result.status == "fail"
    for section in EVIDENCE_SECTIONS:
        assert any(section in message for message in lp052_messages), section


def test_ac3_body_with_evidence_sections_and_real_evidence_passes():
    body = (
        "## Summary\n\n"
        "- evidence section parity fixture\n\n"
        "## Checks\n\n"
        "- [x] `pnpm typecheck`\n\n"
        "## Schema Change Applicability\n\n"
        "- decision: not_schema_change\n"
        "- reason: parser のみ変更\n\n"
        "## Schema Consumer Inventory\n\n"
        "N/A\nreason: schema を変更しない\n\n"
        "## Safety Claim Matrix\n\n"
        "N/A\nreason: safety-sensitive path に該当しない\n\n"
        "## Notes\n\n"
        "- Related issue: #330\n\n"
    ) + _evidence_sections_block()
    result = validate_pr_body(body, load_paths("non_safety_paths.txt"), linked_issue=330)
    assert result.status == "pass"
    assert result.errors == []


def test_ac4_safety_floor_credential_redaction_text_signal_triggers_lp051():
    # PR #2806 regression: scripts/summarize_agent_transcript.py does not match any
    # SAFETY_SENSITIVE_PATH_PATTERNS, so only the text-based floor can catch this.
    body = (
        "## Summary\n\n"
        "- summarize_agent_transcript.py の GitHub token redaction を github_pat_ 対応にする\n\n"
        "## Checks\n\n"
        "- [x] `pnpm typecheck`\n\n"
        "## Schema Change Applicability\n\n"
        "- decision: not_schema_change\n"
        "- reason: parser のみ変更\n\n"
        "## Schema Consumer Inventory\n\n"
        "N/A\nreason: schema を変更しない\n\n"
        "## Safety Claim Matrix\n\n"
        "N/A\nreason: docs only\n\n"
        "## Notes\n\n"
        "- Related issue: #2806\n\n"
    ) + _evidence_sections_block()
    result = validate_pr_body(body, ["scripts/summarize_agent_transcript.py"], linked_issue=2806)
    errors = [error for error in result.errors if error.rule_id == "LP051"]
    assert result.status == "fail"
    assert len(errors) == 1


def test_ac4_safety_floor_applies_to_linked_issue_body_text_signal():
    # The changed path and PR body carry no strong signal on their own; only the linked
    # Issue body (available when the caller passes it) mentions the credential context.
    body = (
        "## Summary\n\n"
        "- fix transcript summarizer parser bug\n\n"
        "## Checks\n\n"
        "- [x] `pnpm typecheck`\n\n"
        "## Schema Change Applicability\n\n"
        "- decision: not_schema_change\n"
        "- reason: parser のみ変更\n\n"
        "## Schema Consumer Inventory\n\n"
        "N/A\nreason: schema を変更しない\n\n"
        "## Safety Claim Matrix\n\n"
        "N/A\nreason: docs only\n\n"
        "## Notes\n\n"
        "- Related issue: #2806\n\n"
    ) + _evidence_sections_block()
    linked_issue_body = "この Issue は personal access token の redaction 対応を扱う。"
    result = validate_pr_body(
        body,
        ["scripts/summarize_agent_transcript.py"],
        linked_issue=2806,
        linked_issue_body=linked_issue_body,
    )
    errors = [error for error in result.errors if error.rule_id == "LP051"]
    assert result.status == "fail"
    assert len(errors) == 1
    # Without the linked Issue body, this exact PR body/path pair is not text-sensitive.
    without_linked_body = validate_pr_body(
        body, ["scripts/summarize_agent_transcript.py"], linked_issue=2806
    )
    assert not [error for error in without_linked_body.errors if error.rule_id == "LP051"]


def test_ac5_near_miss_parser_and_design_token_wording_stays_non_safety_sensitive():
    body = (
        "## Summary\n\n"
        "- rename the parser token type and the design token palette constant\n\n"
        "## Checks\n\n"
        "- [x] `pnpm typecheck`\n\n"
        "## Schema Change Applicability\n\n"
        "- decision: not_schema_change\n"
        "- reason: rename only\n\n"
        "## Schema Consumer Inventory\n\n"
        "N/A\nreason: schema を変更しない\n\n"
        "## Safety Claim Matrix\n\n"
        "N/A\nreason: parser token / design token wording のみで credential を扱わない\n\n"
        "## Notes\n\n"
        "- Related issue: #330\n\n"
    ) + _evidence_sections_block()
    result = validate_pr_body(body, ["src/parser/token_lexer.ts"], linked_issue=330)
    safety_rule_ids = {error.rule_id for error in result.errors} & {"LP051", "LP055", "LP056"}
    assert not safety_rule_ids
    assert result.status == "pass"


def test_ac6_no_independent_safety_claims_v1_gate_script():
    # Issue #144 owns SAFETY_CLAIMS_V1 schema / check_safety_claims.py; #2808 must not add
    # a duplicate independent gate script.
    gate_script = REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "scripts" / "check_safety_claims.py"
    assert not gate_script.exists()


def test_ac9_pull_request_template_projects_all_required_sections():
    template_path = REPO_ROOT / ".github" / "pull_request_template.md"
    text = template_path.read_text(encoding="utf-8")
    for section in REQUIRED_SECTIONS:
        assert f"## {section}" in text, f"template missing projected section: {section}"


def test_ac9_ac_evidence_checks_projects_evidence_required_sections():
    reference_path = (
        REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "references" / "ac-evidence-checks.md"
    )
    text = reference_path.read_text(encoding="utf-8")
    for section in EVIDENCE_SECTIONS:
        assert f"## {section}" in text, f"ac-evidence-checks.md missing projected section: {section}"


def test_follow_up_missing_contract_when_empty_list():
    fence = chr(96) * 3
    body = (
        "## Summary\n\n"
        "- test\n\n"
        "## Checks\n\n"
        "- check\n\n"
        "## Schema Change Applicability\n\n"
        "- decision: not_schema_change\n\n"
        "## Schema Consumer Inventory\n\n"
        "N/A\nreason: none\n\n"
        "## Safety Claim Matrix\n\n"
        + fence + "yaml\n# SAFETY_CLAIMS_V1\nsafety_claims:\n"
        "  - claim: Narrow safety claim\n    implemented: \"partial\"\n"
        "    not_controlled:\n      - Native tool registry\n"
        "    evidence:\n      - rg -n \"claim\" .\n    follow_up: []\n"
        + fence + "\n\n"
        "## Notes\n\n"
        "- Related issue: #330\n"
    )
    result = validate_pr_body(body, [".github/workflows/ci.yml"], linked_issue=330)
    errors = [error for error in result.errors if error.rule_id == "E_FOLLOW_UP_MISSING_CONTRACT"]
    assert result.status == "fail"
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# Issue #2878: reference authority evaluator (single pure decision table)
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402

from validate_pr_body import evaluate_reference_policy  # noqa: E402

REF_REPO = "squne121/loop-protocol"
REF_ISSUE = 330
FENCE3 = chr(96) * 3
A1_REF_URL = f"https://github.com/{REF_REPO}/issues/{REF_ISSUE}#issuecomment-777"
A3_BODY = "## Runtime Verification Applicability\n\n- decision: immediate\n- reason: x\n"
A2_BODY = (
    "## Runtime Verification Applicability\n\n"
    "- decision: deferred\n"
    "- reason: merge 後の live evidence\n"
    "- deferred_destination:\n"
    "    - destination_type: phase\n"
    "    - destination_ref: post-merge-live-evidence\n"
    "- deferred_verification_condition: merge 後に取得する\n"
)
A2_NESTED_HEADING_BODY = (
    "## Runtime Verification Applicability\n\n"
    "- decision: deferred\n"
    "### 詳細\n"
    "- deferred_destination:\n"
    "    - destination_type: phase\n"
    "    - destination_ref: post-merge-live-evidence\n"
    "- deferred_verification_condition: merge 後に取得する\n"
)
DEFERRED_OTHER_BODY = A2_BODY.replace("post-merge-live-evidence", "phase-5")


def _comment(**overrides):
    comment = {
        "url": A1_REF_URL,
        "id": 777,
        "issue_url": f"https://api.github.com/repos/{REF_REPO}/issues/{REF_ISSUE}",
        "author_association": "OWNER",
        "body": f"REFERENCE_DECISION_V1: nonclosing issue=#{REF_ISSUE}\n",
    }
    comment.update(overrides)
    return comment


def _facts(state="OPEN", comment=None, pr_number=None, repo=REF_REPO):
    return {"repo": repo, "issue_state": state, "pr_number": pr_number, "decision_comment": comment}


def _evaluate(pr_body, *, state="OPEN", issue_body=A3_BODY, comment=None, pr_number=None, facts=None):
    return evaluate_reference_policy(
        pr_body.encode("utf-8"),
        REF_ISSUE,
        issue_body,
        facts if facts is not None else _facts(state, comment, pr_number),
    )


OPEN_A3 = dict(state="OPEN", issue_body=A3_BODY)
OPEN_A2 = dict(state="OPEN", issue_body=A2_BODY)
CLOSED = dict(state="CLOSED", issue_body=A3_BODY)
NOTES_330 = "\n## Notes\n\n- Related issue: #330\n"
NOTES_OTHER = "\n## Notes\n\n- Related issue: #331\n"

# (id, kwargs, pr_body, (decision, level, reason_code), (effective_kind, body_verdict, body_reason))
D_A3 = ("closing_required", "A3", "a3_close_ready")
D_A2 = ("nonclosing_required", "A2", "a2_contract_deferred")
D_A1 = ("nonclosing_required", "A1", "a1_explicit_decision")
D_CLOSED = ("nonclosing_required", "CLOSED", "issue_closed")
CLOSING_OK = ("closing", "valid", "ok")
REFS_OK = ("non-closing", "valid", "ok")
REPAIR = ("non-closing", "repair", "closing_missing")
FORBIDDEN = ("closing", "block", "closing_forbidden")
NONE_MISSING = ("none", "block", "reference_missing")
NOTES_MISSING = ("notes-only", "block", "reference_missing")
NOTES_VALID = ("notes-only", "valid", "ok")
OTHER_NONE = ("none", "block", "closing_for_other")
OTHER_REFS = ("non-closing", "block", "closing_for_other")
NOT_EVALUATED = ("none", "block", "not_evaluated")
UNRESOLVED = ("fail_closed", None, "runtime_applicability_unresolved")
A1_INVALID = ("fail_closed", None, "a1_decision_invalid")
A1_AMBIGUOUS = ("fail_closed", None, "a1_decision_ambiguous")
CLOSED_A3_BODY = dict(state="OPEN", issue_body="## Outcome\n\nnone\n")
NO_SECTION = "## Outcome\n\nnone\n"
RVA = "## Runtime Verification Applicability\n\n"
A1_LINE = f"Reference-Decision: {A1_REF_URL}"
ISSUE_BASE = f"https://github.com/{REF_REPO}/issues"


def _a1(**comment_overrides):
    return dict(OPEN_A3, comment=_comment(**comment_overrides))


def _open(issue_body, **extra):
    return dict(state="OPEN", issue_body=issue_body, **extra)


REFERENCE_POLICY_MATRIX = [
    # --- authority rows 0 / 3 / 4 with the body mapping ---------------------------------------------
    ("a3_closes", OPEN_A3, "Closes #330", D_A3, CLOSING_OK),
    ("a3_refs_only_repair", OPEN_A3, "Refs #330", D_A3, REPAIR),
    ("a3_none", OPEN_A3, "no reference", D_A3, NONE_MISSING),
    ("a3_notes_only", OPEN_A3, NOTES_330, D_A3, NOTES_MISSING),
    ("a3_notes_other_number", OPEN_A3, NOTES_OTHER, D_A3, NONE_MISSING),
    ("a3_closes_and_refs_coexist", OPEN_A3, "Refs #330\nCloses #330", D_A3, CLOSING_OK),
    ("a3_duplicate_closes", OPEN_A3, "Closes #330\nCloses #330", D_A3, CLOSING_OK),
    ("a3_not_applicable", _open(RVA + "decision: not_applicable\n"), "Closes #330", D_A3, CLOSING_OK),
    ("a3_deferred_other_destination", _open(DEFERRED_OTHER_BODY), "Closes #330", D_A3, CLOSING_OK),
    ("a2_refs", OPEN_A2, "Refs #330", D_A2, REFS_OK),
    ("a2_refs_duplicate", OPEN_A2, "Refs #330\nrefs #330", D_A2, REFS_OK),
    ("a2_closes_forbidden", OPEN_A2, "Closes #330", D_A2, FORBIDDEN),
    ("a2_closes_and_refs_forbidden", OPEN_A2, "Refs #330\nCloses #330", D_A2, FORBIDDEN),
    ("a2_none", OPEN_A2, "nothing", D_A2, NONE_MISSING),
    ("a2_notes_only", OPEN_A2, NOTES_330, D_A2, NOTES_MISSING),
    ("a2_nested_heading_ok", _open(A2_NESTED_HEADING_BODY), "Refs #330", D_A2, REFS_OK),
    # --- every GitHub closing keyword and every target grammar --------------------------------------
    *[
        (f"a3_keyword_{keyword.replace(chr(58), '_colon')}", OPEN_A3, f"{keyword} #330", D_A3, CLOSING_OK)
        for keyword in ("Close", "Closes", "Closed", "Fix", "Fixes", "Fixed", "Resolve", "Resolves", "Resolved",
                        "CLOSES", "fixes:")
    ],
    ("a3_cross_repo_form", OPEN_A3, f"Fixes {REF_REPO}#330", D_A3, CLOSING_OK),
    ("a3_issue_url_form", OPEN_A3, f"Resolves {ISSUE_BASE}/330", D_A3, CLOSING_OK),
    ("a2_fixes_forbidden", OPEN_A2, "Fixes #330\nRefs #330", D_A2, FORBIDDEN),
    # --- closing for another issue / repository / prefix collision ----------------------------------
    ("other_number", OPEN_A3, "Closes #331", D_A3, OTHER_NONE),
    ("other_number_with_target", OPEN_A3, "Closes #330\nFixes #331", D_A3, ("closing", "block", "closing_for_other")),
    ("other_repository", OPEN_A3, "Closes other/repo#330", D_A3, OTHER_NONE),
    ("prefix_collision_longer", OPEN_A3, "Closes #3301", D_A3, OTHER_NONE),
    ("prefix_collision_shorter", OPEN_A3, "Closes #33", D_A3, OTHER_NONE),
    ("refs_prefix_collision_is_not_a_reference", OPEN_A2, "Refs #3301", D_A2, NONE_MISSING),
    ("other_blocks_valid_refs", OPEN_A2, "Refs #330\nCloses #331", D_A2, OTHER_REFS),
    # --- ordinary prose that merely looks like a keyword is ignored, never fail_closed --------------
    ("prose_then_closes", OPEN_A3, "fixes the typo\nresolved by the earlier change\ncloses the loop\nCloses #330",
     D_A3, CLOSING_OK),
    ("prose_only_is_none", OPEN_A3, "fixes the typo\nresolved by the earlier change\ncloses the loop", D_A3,
     NONE_MISSING),
    ("prose_with_refs_under_a2", OPEN_A2, "closes the loop\nRefs #330", D_A2, REFS_OK),
    # --- code fence / quote / inline code handling (two separate decisions) -------------------------
    ("a2_closing_in_fence_blocked", OPEN_A2, f"Refs #330\n{FENCE3}\nCloses #330\n{FENCE3}\n", D_A2, FORBIDDEN),
    ("a2_closing_in_quote_blocked", OPEN_A2, "Refs #330\n> Fixes #330\n", D_A2, FORBIDDEN),
    ("other_in_fence_detected", OPEN_A2, f"Refs #330\n{FENCE3}\nFixes #331\n{FENCE3}\n", D_A2, OTHER_REFS),
    ("a3_closing_only_in_fence_repair", OPEN_A3, f"{FENCE3}\nCloses #330\n{FENCE3}\n", D_A3,
     ("closing", "repair", "closing_missing")),
    ("a3_closing_only_in_fence_with_outside_refs", OPEN_A3, f"Refs #330\n{FENCE3}\nCloses #330\n{FENCE3}\n", D_A3,
     ("closing", "repair", "closing_missing")),
    ("a3_closing_only_in_quote_repair", OPEN_A3, "> Closes #330\n", D_A3, ("closing", "repair", "closing_missing")),
    ("a3_fence_closing_plus_real_closing", OPEN_A3, f"{FENCE3}\nCloses #330\n{FENCE3}\nCloses #330", D_A3,
     CLOSING_OK),
    ("refs_in_fence_not_counted", OPEN_A2, f"{FENCE3}\nRefs #330\n{FENCE3}\n", D_A2, NONE_MISSING),
    ("refs_in_quote_not_counted", OPEN_A2, "> Refs #330\n", D_A2, NONE_MISSING),
    ("refs_in_inline_code_not_counted", OPEN_A2, "see `Refs #330`", D_A2, NONE_MISSING),
    # --- CLOSED Issue: dedicated mapping, no authority evaluation -----------------------------------
    ("closed_refs", CLOSED, "Refs #330", D_CLOSED, REFS_OK),
    ("closed_closes_forbidden", CLOSED, "Closes #330", D_CLOSED, FORBIDDEN),
    ("closed_closing_in_fence_forbidden", CLOSED, f"{FENCE3}\nFixes #330\n{FENCE3}\n", D_CLOSED, FORBIDDEN),
    ("closed_notes_only_valid", CLOSED, NOTES_330, D_CLOSED, NOTES_VALID),
    ("closed_none", CLOSED, "nothing", D_CLOSED, NONE_MISSING),
    ("closed_notes_other_number", CLOSED, NOTES_OTHER, D_CLOSED, NONE_MISSING),
    ("closed_closing_for_other", CLOSED, "Refs #330\nCloses #331", D_CLOSED, OTHER_REFS),
    ("closed_ignores_invalid_a1_line", CLOSED, "Refs #330\nReference-Decision: not-a-url", D_CLOSED, REFS_OK),
    # --- A1 explicit decision -----------------------------------------------------------------------
    ("a1_valid_over_a3", _a1(), f"Refs #330\n{A1_LINE}", D_A1, REFS_OK),
    ("a1_valid_without_applicability_section", _open(NO_SECTION, comment=_comment()), f"Refs #330\n{A1_LINE}",
     D_A1, REFS_OK),
    ("a1_valid_with_closing_forbidden", _a1(), f"Closes #330\n{A1_LINE}", D_A1, FORBIDDEN),
    ("a1_valid_member", _a1(author_association="MEMBER"), f"Refs #330\n{A1_LINE}", D_A1, REFS_OK),
    ("a1_valid_collaborator", _a1(author_association="COLLABORATOR"), f"Refs #330\n{A1_LINE}", D_A1, REFS_OK),
    ("a1_ignored_when_indented", _a1(), f"Closes #330\n  {A1_LINE}", D_A3, CLOSING_OK),
    # --- A2 / A3 unresolved -> fail_closed ----------------------------------------------------------
    ("applicability_missing", _open(NO_SECTION), "Refs #330", UNRESOLVED, NOT_EVALUATED),
    ("applicability_duplicated", _open(A3_BODY + "\n" + A3_BODY), "Refs #330", UNRESOLVED, NOT_EVALUATED),
    ("applicability_only_in_fence", _open(f"{FENCE3}\n{A2_BODY}{FENCE3}\n"), "Refs #330", UNRESOLVED,
     NOT_EVALUATED),
    ("applicability_decision_not_in_enum", _open(RVA + "- decision: maybe\n"), "Refs #330", UNRESOLVED,
     NOT_EVALUATED),
    ("applicability_decision_missing", _open(RVA + "- reason: x\n"), "Refs #330", UNRESOLVED, NOT_EVALUATED),
    ("applicability_decision_repeated", _open(RVA + "- decision: immediate\n- decision: deferred\n"), "Refs #330",
     UNRESOLVED, NOT_EVALUATED),
    ("applicability_deferred_without_destination",
     _open(RVA + "- decision: deferred\n- deferred_verification_condition: x\n"), "Refs #330", UNRESOLVED,
     NOT_EVALUATED),
    ("applicability_deferred_without_condition",
     _open(A2_BODY.replace("- deferred_verification_condition: merge 後に取得する\n", "")), "Refs #330",
     UNRESOLVED, NOT_EVALUATED),
    # --- A1 present-but-invalid never degrades to A2 / A3 -------------------------------------------
    ("a1_invalid_comment_missing", dict(OPEN_A2, comment=None), f"Refs #330\n{A1_LINE}", A1_INVALID, NOT_EVALUATED),
    ("a1_invalid_url_shape", dict(OPEN_A2, comment=_comment()), "Refs #330\nReference-Decision: https://example.com/x",
     A1_INVALID, NOT_EVALUATED),
    ("a1_invalid_other_issue_number", _a1(),
     f"Refs #330\nReference-Decision: {ISSUE_BASE}/331#issuecomment-777", A1_INVALID, NOT_EVALUATED),
    ("a1_invalid_other_repository", _a1(),
     "Refs #330\nReference-Decision: https://github.com/other/repo/issues/330#issuecomment-777", A1_INVALID,
     NOT_EVALUATED),
    ("a1_invalid_comment_url_mismatch", _a1(url=A1_REF_URL + "0"), f"Refs #330\n{A1_LINE}", A1_INVALID,
     NOT_EVALUATED),
    ("a1_invalid_comment_id_mismatch", _a1(id=778), f"Refs #330\n{A1_LINE}", A1_INVALID, NOT_EVALUATED),
    ("a1_invalid_comment_issue_url_mismatch", _a1(issue_url=f"https://api.github.com/repos/{REF_REPO}/issues/331"),
     f"Refs #330\n{A1_LINE}", A1_INVALID, NOT_EVALUATED),
    ("a1_invalid_insufficient_authority", _a1(author_association="CONTRIBUTOR"), f"Refs #330\n{A1_LINE}",
     A1_INVALID, NOT_EVALUATED),
    ("a1_invalid_marker_missing", _a1(body="no marker"), f"Refs #330\n{A1_LINE}", A1_INVALID, NOT_EVALUATED),
    ("a1_invalid_marker_for_other_issue", _a1(body="REFERENCE_DECISION_V1: nonclosing issue=#331"),
     f"Refs #330\n{A1_LINE}", A1_INVALID, NOT_EVALUATED),
    ("a1_invalid_marker_twice",
     _a1(body="REFERENCE_DECISION_V1: nonclosing issue=#330\nREFERENCE_DECISION_V1: nonclosing issue=#330"),
     f"Refs #330\n{A1_LINE}", A1_INVALID, NOT_EVALUATED),
    ("a1_invalid_marker_not_a_whole_line", _a1(body="note REFERENCE_DECISION_V1: nonclosing issue=#330"),
     f"Refs #330\n{A1_LINE}", A1_INVALID, NOT_EVALUATED),
    ("a1_ambiguous_two_different_lines", _a1(), f"Refs #330\n{A1_LINE}\n{A1_LINE}9", A1_AMBIGUOUS, NOT_EVALUATED),
    ("a1_ambiguous_same_url_twice", _a1(), f"Refs #330\n{A1_LINE}\n{A1_LINE}", A1_AMBIGUOUS, NOT_EVALUATED),
    ("a1_ambiguous_inside_a_fence_still_counts", dict(OPEN_A2, comment=_comment()),
     f"Refs #330\n{A1_LINE}\n{FENCE3}\n{A1_LINE}\n{FENCE3}\n", A1_AMBIGUOUS, NOT_EVALUATED),
]


def test_given_reference_policy_matrix_when_validated_then_accept_or_reject():
    seen_ids = set()
    for case_id, kwargs, pr_body, expected_decision, expected_body in REFERENCE_POLICY_MATRIX:
        assert case_id not in seen_ids, case_id
        seen_ids.add(case_id)
        kwargs = dict(kwargs)
        result = _evaluate(pr_body, **kwargs)
        assert (result["decision"], result["level"], result["reason_code"]) == expected_decision, (case_id, result)
        if expected_decision[0] == "fail_closed":
            # fail_closed is always a stop: block / not_evaluated, level null
            assert (result["body_verdict"], result["body_reason"]) == ("block", "not_evaluated"), case_id
            continue
        assert (result["effective_kind"], result["body_verdict"], result["body_reason"]) == expected_body, (
            case_id,
            result,
        )

    # the accept / reject decision a lint caller sees (LP057 with facts) follows the same evaluator
    for pr_body, kwargs, expect_error in (
        ("Closes #330", OPEN_A3, False),
        ("Refs #330", OPEN_A3, True),
        ("Refs #330", OPEN_A2, False),
        ("Closes #330", OPEN_A2, True),
        ("Refs #330", dict(state="OPEN", issue_body="## Outcome\n\nnone\n"), True),
    ):
        base = load_fixture("valid_not_schema_change.md").replace("- Related issue: #330", "- Related issue: N/A")
        body = base + "\n" + pr_body + "\n"
        policy = _evaluate(body, **kwargs)
        result = validate_pr_body(body, load_paths("non_safety_paths.txt"), REF_ISSUE, reference_policy=policy)
        lp057 = [error for error in result.errors if error.rule_id == "LP057"]
        assert bool(lp057) is expect_error, (pr_body, kwargs, lp057)


def test_given_reference_policy_facts_not_supplied_when_validated_then_legacy_lp057_is_unchanged():
    body = load_fixture("valid_not_schema_change.md")  # Notes `Related issue: #330` only
    legacy = validate_pr_body(body, load_paths("non_safety_paths.txt"), REF_ISSUE)
    assert [error.rule_id for error in legacy.errors if error.rule_id == "LP057"] == []
    for text, expect_error in (("Closes #330", False), ("Refs #330", False), ("Closes #331", True)):
        result = validate_pr_body(body + "\n" + text + "\n", load_paths("non_safety_paths.txt"), REF_ISSUE)
        assert bool([error for error in result.errors if error.rule_id == "LP057"]) is expect_error, text


REFERENCE_RESULT_KEYS = {
    "decision",
    "level",
    "reason_code",
    "repo",
    "issue_number",
    "pr_number",
    "pr_body_sha256",
    "effective_kind",
    "body_verdict",
    "body_reason",
}
REFERENCE_REASON_CODES = {
    "issue_closed",
    "a1_explicit_decision",
    "a1_decision_invalid",
    "a1_decision_ambiguous",
    "a2_contract_deferred",
    "a3_close_ready",
    "runtime_applicability_unresolved",
    "facts_invalid",
}


_DEFAULT_FACTS = object()


def _run_cli(tmp_path, *, pr_body_bytes, facts=_DEFAULT_FACTS, issue_body=A3_BODY, extra=(), omit=()):
    body_file = tmp_path / "body.md"
    body_file.write_bytes(pr_body_bytes)
    facts_file = tmp_path / "facts.json"
    facts_file.write_text(json.dumps(_facts() if facts is _DEFAULT_FACTS else facts), encoding="utf-8")
    issue_file = tmp_path / "issue.md"
    issue_file.write_text(issue_body, encoding="utf-8")
    argv = [sys.executable, str(SCRIPT_PATH), "--body-file", str(body_file), "--linked-issue", str(REF_ISSUE)]
    if "issue" not in omit:
        argv += ["--linked-issue-body-file", str(issue_file)]
    if "facts" not in omit:
        argv += ["--reference-facts-file", str(facts_file)]
    argv += list(extra)
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def test_given_reference_facts_wire_when_evaluated_then_exact_keys_and_body_bytes_hash(tmp_path):
    crlf_body = b"Closes #330\r\nsecond line\r\n"
    proc = _run_cli(tmp_path, pr_body_bytes=crlf_body, extra=["--evaluate-reference-policy"])
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert set(result) == REFERENCE_RESULT_KEYS
    assert result["decision"] == "closing_required" and result["level"] == "A3"
    assert result["reason_code"] in REFERENCE_REASON_CODES
    assert result["repo"] == REF_REPO and result["issue_number"] == REF_ISSUE and result["pr_number"] is None
    # sha256 of the exact bytes (CRLF kept), lowercase hex, not of a newline-normalized string
    assert result["pr_body_sha256"] == hashlib.sha256(crlf_body).hexdigest()
    assert result["pr_body_sha256"] != hashlib.sha256(crlf_body.replace(b"\r\n", b"\n")).hexdigest()
    assert result["pr_body_sha256"] == result["pr_body_sha256"].lower()
    assert (result["effective_kind"], result["body_verdict"], result["body_reason"]) == ("closing", "valid", "ok")

    # decision never changes the exit code: a stop is still exit 0 with a JSON `decision`
    stop = _run_cli(
        tmp_path, pr_body_bytes=b"Refs #330", issue_body="no section", extra=["--evaluate-reference-policy"]
    )
    assert stop.returncode == 0
    stopped = json.loads(stop.stdout)
    assert set(stopped) == REFERENCE_RESULT_KEYS
    assert (stopped["decision"], stopped["level"], stopped["reason_code"]) == (
        "fail_closed",
        None,
        "runtime_applicability_unresolved",
    )
    assert (stopped["body_verdict"], stopped["body_reason"]) == ("block", "not_evaluated")

    # evaluator mode needs both --reference-facts-file and --linked-issue-body-file
    for omitted in ("facts", "issue"):
        missing = _run_cli(tmp_path, pr_body_bytes=b"Refs #330", extra=["--evaluate-reference-policy"], omit=(omitted,))
        assert missing.returncode == 0
        missing_result = json.loads(missing.stdout)
        assert (missing_result["decision"], missing_result["reason_code"]) == ("fail_closed", "facts_invalid")
        assert set(missing_result) == REFERENCE_RESULT_KEYS

    # only an unreadable --body-file keeps the legacy stderr + exit 2
    unreadable = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--body-file", str(tmp_path / "nope.md"), "--linked-issue", "330",
         "--evaluate-reference-policy"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert unreadable.returncode == 2 and unreadable.stdout == ""


@pytest.mark.parametrize(
    "mutate",
    [
        lambda facts: {**facts, "extra": 1},  # unknown key
        lambda facts: {key: value for key, value in facts.items() if key != "repo"},  # missing key
        lambda facts: {**facts, "issue_state": "MERGED"},
        lambda facts: {**facts, "issue_state": None},
        # unhashable / non-string JSON values must be structured facts_invalid, never TypeError
        lambda facts: {**facts, "issue_state": []},
        lambda facts: {**facts, "issue_state": {}},
        lambda facts: {**facts, "issue_state": True},
        lambda facts: {**facts, "issue_state": 5},
        lambda facts: {**facts, "pr_number": 0},
        lambda facts: {**facts, "pr_number": True},
        lambda facts: {**facts, "pr_number": "7"},
        lambda facts: {**facts, "repo": "not-a-repo"},
        lambda facts: {**facts, "repo": 5},
        lambda facts: {**facts, "updated_at": "2026-10-03T00:00:00Z"},  # never a fact
        lambda facts: {**facts, "decision_comment": {**_comment(), "extra": 1}},
        lambda facts: {**facts, "decision_comment": {key: value for key, value in _comment().items() if key != "body"}},
        lambda facts: {**facts, "decision_comment": {**_comment(), "id": "777"}},
        lambda facts: {**facts, "decision_comment": {**_comment(), "id": True}},
        lambda facts: {**facts, "decision_comment": "comment"},
        lambda facts: ["not", "an", "object"],
        lambda facts: None,
    ],
)
def test_given_malformed_reference_facts_when_evaluated_then_facts_invalid(tmp_path, mutate):
    facts = mutate(_facts(comment=_comment()))
    proc = _run_cli(tmp_path, pr_body_bytes=b"Refs #330", facts=facts, extra=["--evaluate-reference-policy"])
    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert (result["decision"], result["level"], result["reason_code"]) == ("fail_closed", None, "facts_invalid")
    assert set(result) == REFERENCE_RESULT_KEYS


def test_given_non_json_facts_file_when_evaluated_then_facts_invalid(tmp_path):
    body_file = tmp_path / "body.md"
    body_file.write_text("Refs #330", encoding="utf-8")
    facts_file = tmp_path / "facts.json"
    facts_file.write_text("{not json", encoding="utf-8")
    issue_file = tmp_path / "issue.md"
    issue_file.write_text(A2_BODY, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--body-file", str(body_file), "--linked-issue", "330",
         "--linked-issue-body-file", str(issue_file), "--reference-facts-file", str(facts_file),
         "--evaluate-reference-policy"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["reason_code"] == "facts_invalid"


def test_given_facts_without_evaluate_flag_when_linted_then_lp057_follows_the_evaluator(tmp_path):
    body = load_fixture("valid_not_schema_change.md").replace("- Related issue: #330", "- Related issue: N/A")
    changed = tmp_path / "paths.txt"
    changed.write_text("src/example.ts\n", encoding="utf-8")

    def lint(pr_body, issue_body=A2_BODY, omit=()):
        proc = _run_cli(tmp_path, pr_body_bytes=(body + "\n" + pr_body + "\n").encode("utf-8"),
                        issue_body=issue_body, extra=["--changed-paths-file", str(changed)], omit=omit)
        payload = json.loads(proc.stdout)
        assert payload["schema"] == "loop_body_lint/v1"  # output stays the legacy lint shape
        return proc.returncode, [error for error in payload["errors"] if error["rule_id"] == "LP057"]

    assert lint("Refs #330") == (0, [])
    rc, lp057 = lint("Closes #330")
    assert rc == 1 and len(lp057) == 1 and "closing_forbidden" in lp057[0]["message"]
    rc, lp057 = lint("Refs #330", issue_body=A3_BODY)
    assert rc == 1 and "closing_missing" in lp057[0]["message"]
    # a missing linked-issue-body-file is facts_invalid, even when the body alone could decide
    rc, lp057 = lint("Refs #330", omit=("issue",))
    assert rc == 1 and "facts_invalid" in lp057[0]["message"]


# ---------------------------------------------------------------------------
# Issue #2878 (PR #2896 review): native auto-close risk check for `nonclosing_required`
# ---------------------------------------------------------------------------

from validate_pr_body import (  # noqa: E402
    NATIVE_AUTO_CLOSE_OUTPUT_KEYS,
    evaluate_native_auto_close_risk,
)

NATIVE_PR = 2896


def _native_facts(**overrides):
    """Squash-only repository (the live setting, not a hardcoded assumption) with a clean PR."""
    facts = {
        "repo": REF_REPO,
        "closing_relations": [],
        "closing_relations_complete": True,
        "merge_settings": {
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
            "squash_merge_commit_title": "PR_TITLE",
            "squash_merge_commit_message": "PR_BODY",
        },
        "merge_method": None,
        "pr_title": "feat: 参照と close の分離",
        "pr_body": "## Summary\n\nRefs #330\n",
        "commit_messages": ["feat: 実装\n\nbody"],
        "final_squash_message": None,
    }
    for key, value in overrides.items():
        if key == "merge_settings":
            value = {**facts["merge_settings"], **value}
        facts[key] = value
    return facts


def _policy(pr_body="Refs #330\n", *, issue_body=A2_BODY, state="OPEN", comment=None):
    return _evaluate(pr_body, state=state, issue_body=issue_body, comment=comment, pr_number=NATIVE_PR)


def _native(facts, **policy_kwargs):
    return evaluate_native_auto_close_risk(_policy(**policy_kwargs), facts)


def test_given_valid_refs_without_native_close_path_when_checked_then_clear_with_adopted_message_hash():
    for kwargs in ({}, {"issue_body": A3_BODY, "comment": _comment(), "pr_body": f"Refs #330\n{A1_LINE}\n"}):
        result = _native(_native_facts(), **kwargs)
        assert set(result) == set(NATIVE_AUTO_CLOSE_OUTPUT_KEYS)
        assert (result["status"], result["reason_code"]) == ("clear", "no_native_auto_close_path")
        assert result["findings"] == []
        assert result["decision"] == "nonclosing_required" and result["level"] in {"A1", "A2"}
        expected = hashlib.sha256(
            "feat: 参照と close の分離\n\n## Summary\n\nRefs #330\n".encode("utf-8")
        ).hexdigest()
        assert result["adopted_message_sha256"] == expected  # the verified message can be bound at merge time


@pytest.mark.parametrize("issue_body,comment_extra", [(A2_BODY, None), (A3_BODY, "a1")])
def test_given_native_relation_to_target_when_a1_or_a2_then_blocked_even_though_body_is_valid_refs(
    issue_body, comment_extra
):
    pr_body = "Refs #330\n" + (f"{A1_LINE}\n" if comment_extra else "")
    policy = _policy(pr_body, issue_body=issue_body, comment=_comment() if comment_extra else None)
    assert (policy["decision"], policy["body_verdict"]) == ("nonclosing_required", "valid")  # body alone says OPEN

    facts = _native_facts(closing_relations=[{"number": REF_ISSUE, "repository": REF_REPO.upper()}])
    result = evaluate_native_auto_close_risk(policy, facts)
    assert (result["status"], result["reason_code"]) == ("blocked", "native_relation_present")
    assert result["findings"][0]["kind"] == "native_relation"


def test_given_unrelated_native_relation_or_keyword_when_checked_then_target_is_not_blocked():
    unrelated = _native_facts(
        closing_relations=[
            {"number": REF_ISSUE + 1, "repository": REF_REPO},  # other Issue, same repository
            {"number": REF_ISSUE, "repository": "someone/else"},  # same number, other repository
        ],
        pr_body="Closes #331\nFixes someone/else#330\nRefs #330\n",
        commit_messages=["fix: x\n\nResolves #999"],
    )
    result = _native(unrelated)
    assert (result["status"], result["findings"]) == ("clear", [])


@pytest.mark.parametrize(
    "keyword", ["close", "Closes", "closed", "fix", "FIXES", "fixed", "resolve", "resolves", "Resolved"]
)
@pytest.mark.parametrize("target", ["#330", "squne121/loop-protocol#330", "https://github.com/squne121/loop-protocol/issues/330"])
def test_given_effective_squash_message_with_target_keyword_when_checked_then_blocked(keyword, target):
    # PR_BODY setting: the PR body is the adopted squash body
    facts = _native_facts(pr_body=f"## Summary\n\nRefs #330\n\n{keyword}: {target}\n")
    result = _native(facts)
    assert (result["status"], result["reason_code"]) == ("blocked", "effective_message_closing_keyword")
    assert result["findings"] == [{"kind": "effective_message", "method": "squash", "source": "squash_body"}]


def test_given_squash_settings_when_checked_then_only_the_adopted_text_is_inspected():
    kw_commit = ["fix: a\n\nFixes #330"]
    # PR_BODY / BLANK: the commit history is not adopted, so a keyword there is not a blocker
    for message_setting in ("PR_BODY", "BLANK"):
        settings = {"squash_merge_commit_message": message_setting}
        facts = _native_facts(merge_settings=settings, commit_messages=kw_commit)
        assert _native(facts)["status"] == "clear", message_setting
    # COMMIT_MESSAGES: the commits are adopted
    facts = _native_facts(merge_settings={"squash_merge_commit_message": "COMMIT_MESSAGES"}, commit_messages=kw_commit)
    assert _native(facts)["reason_code"] == "effective_message_closing_keyword"
    # BLANK ignores the PR body, so a PR body keyword is no longer adopted
    facts = _native_facts(merge_settings={"squash_merge_commit_message": "BLANK"}, pr_body="Closes #330\n")
    assert _native(facts)["status"] == "clear"
    # title settings: PR_TITLE vs COMMIT_OR_PR_TITLE (single commit headline wins; several commits -> PR title)
    titled = {"pr_title": "docs: x", "commit_messages": ["Closes #330"]}
    assert _native(_native_facts(**titled))["status"] == "clear"
    single = _native_facts(merge_settings={"squash_merge_commit_title": "COMMIT_OR_PR_TITLE"}, **titled)
    assert (_native(single)["reason_code"], _native(single)["findings"][0]["source"]) == (
        "effective_message_closing_keyword",
        "squash_title",
    )
    several = _native_facts(
        merge_settings={"squash_merge_commit_title": "COMMIT_OR_PR_TITLE"},
        pr_title="docs: x",
        commit_messages=["Closes #330", "second"],
    )
    assert _native(several)["status"] == "clear"
    # PR title with a keyword is adopted under PR_TITLE
    assert _native(_native_facts(pr_title="Fixes #330"))["findings"][0]["source"] == "squash_title"


def test_given_final_squash_message_when_checked_then_it_overrides_the_settings_derivation():
    # the human changed the final message in the merge UI: that exact text is what is checked
    changed = _native_facts(final_squash_message={"title": "feat: x", "body": "Resolves #330"})
    result = _native(changed)
    assert (result["status"], result["findings"][0]["source"]) == ("blocked", "squash_body")
    # a verified clean message is reported with a hash the merge step can compare
    clean = _native_facts(final_squash_message={"title": "feat: x", "body": "Refs #330"})
    result = _native(clean)
    assert result["status"] == "clear"
    assert result["adopted_message_sha256"] == hashlib.sha256(b"feat: x\n\nRefs #330").hexdigest()
    # a clean derivation from settings does not vouch for a different final message
    assert result["adopted_message_sha256"] != _native(_native_facts())["adopted_message_sha256"]


def test_given_live_merge_settings_when_checked_then_methods_are_not_hardcoded():
    commit_kw = ["fix: a\n\nFixes #330"]
    merge_enabled = {"allow_merge_commit": True}
    # merge commit enabled and no method chosen: every enabled method is checked (commits are adopted by merge)
    result = _native(_native_facts(merge_settings=merge_enabled, commit_messages=commit_kw))
    assert (result["status"], result["findings"]) == (
        "blocked",
        [{"kind": "effective_message", "method": "merge", "source": "commit_message"}],
    )
    # an explicit squash method does not inspect commit history that squash does not adopt
    assert _native(_native_facts(merge_settings=merge_enabled, merge_method="squash", commit_messages=commit_kw))[
        "status"
    ] == "clear"
    # rebase keeps the commit messages
    rebase = _native_facts(
        merge_settings={"allow_squash_merge": False, "allow_rebase_merge": True}, commit_messages=commit_kw
    )
    assert _native(rebase)["findings"] == [
        {"kind": "effective_message", "method": "rebase", "source": "commit_message"}
    ]
    # a method the repository does not allow is fail-closed, as is a repository with no merge method
    assert _native(_native_facts(merge_method="rebase"))["reason_code"] == "merge_method_not_allowed"
    none_allowed = _native_facts(merge_settings={"allow_squash_merge": False})
    assert _native(none_allowed)["reason_code"] == "merge_method_not_allowed"


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda f: None, "facts_invalid"),
        (lambda f: [], "facts_invalid"),
        (lambda f: {**f, "extra": 1}, "facts_invalid"),
        (lambda f: {k: v for k, v in f.items() if k != "merge_settings"}, "facts_invalid"),
        (lambda f: {**f, "repo": []}, "facts_invalid"),
        (lambda f: {**f, "repo": "other/repo"}, "facts_invalid"),
        (lambda f: {**f, "closing_relations": {}}, "facts_invalid"),
        (lambda f: {**f, "closing_relations": [{"number": "330", "repository": REF_REPO}]}, "facts_invalid"),
        (lambda f: {**f, "closing_relations": [{"number": True, "repository": REF_REPO}]}, "facts_invalid"),
        (lambda f: {**f, "closing_relations": [{"number": 330, "repository": []}]}, "facts_invalid"),
        (lambda f: {**f, "closing_relations": [{"number": 330}]}, "facts_invalid"),
        (lambda f: {**f, "closing_relations_complete": "yes"}, "facts_invalid"),
        (lambda f: {**f, "closing_relations_complete": False}, "relations_incomplete"),
        (lambda f: {**f, "merge_method": []}, "facts_invalid"),
        (lambda f: {**f, "merge_method": "ff"}, "facts_invalid"),
        (lambda f: {**f, "pr_title": None}, "facts_invalid"),
        (lambda f: {**f, "pr_body": 5}, "facts_invalid"),
        (lambda f: {**f, "commit_messages": "x"}, "facts_invalid"),
        (lambda f: {**f, "commit_messages": [1]}, "facts_invalid"),
        (lambda f: {**f, "final_squash_message": {"title": "x"}}, "facts_invalid"),
        (lambda f: {**f, "final_squash_message": "x"}, "facts_invalid"),
        (lambda f: {**f, "merge_settings": {**f["merge_settings"], "allow_squash_merge": "true"}}, "facts_invalid"),
        (lambda f: {**f, "merge_settings": {**f["merge_settings"], "squash_merge_commit_title": []}}, "facts_invalid"),
        (lambda f: {**f, "merge_settings": {**f["merge_settings"], "extra": 1}}, "facts_invalid"),
        (
            lambda f: {**f, "merge_settings": {**f["merge_settings"], "squash_merge_commit_title": "SOMETHING_NEW"}},
            "squash_settings_invalid",
        ),
        (
            lambda f: {**f, "merge_settings": {**f["merge_settings"], "squash_merge_commit_message": None}},
            "squash_settings_invalid",
        ),
    ],
)
def test_given_malformed_native_facts_when_checked_then_structured_fail_closed_not_exception(mutate, reason):
    result = evaluate_native_auto_close_risk(_policy(), mutate(_native_facts()))
    assert (result["status"], result["reason_code"]) == ("fail_closed", reason)
    assert set(result) == set(NATIVE_AUTO_CLOSE_OUTPUT_KEYS) and result["findings"] == []


def test_given_lane_without_open_promise_when_checked_then_not_applicable_or_fail_closed_policy_passes_through():
    facts = _native_facts(closing_relations=[{"number": REF_ISSUE, "repository": REF_REPO}])
    # A3: the PR is supposed to close the Issue, so a native relation is not a risk
    a3 = evaluate_native_auto_close_risk(_policy("Closes #330\n", issue_body=A3_BODY), facts)
    assert (a3["status"], a3["reason_code"]) == ("not_applicable", "closing_required_lane")
    # CLOSED: there is no OPEN state left to preserve
    closed = evaluate_native_auto_close_risk(_policy("Refs #330\n", state="CLOSED"), facts)
    assert (closed["status"], closed["reason_code"]) == ("not_applicable", "issue_closed")
    # fail_closed reference policy (A1 line without a usable comment) never becomes clear
    stopped = evaluate_native_auto_close_risk(_policy(f"Refs #330\n{A1_LINE}\n"), _native_facts())
    assert (stopped["status"], stopped["reason_code"]) == ("fail_closed", "reference_policy_fail_closed")
    # a malformed policy result is also structured
    for bad_policy in (None, [], {"decision": []}, {"decision": "nonclosing_required", "level": []}):
        result = evaluate_native_auto_close_risk(bad_policy, _native_facts())
        assert result["status"] in {"fail_closed", "not_applicable"}


def _run_native_cli(tmp_path, *, native_facts, pr_body=b"Refs #330\n", issue_body=A2_BODY, omit_native=False):
    native_file = tmp_path / "native.json"
    payload = native_facts if isinstance(native_facts, str) else json.dumps(native_facts)
    native_file.write_text(payload, encoding="utf-8")
    extra = ["--evaluate-native-auto-close-risk"]
    if not omit_native:
        extra += ["--native-close-facts-file", str(native_file)]
    return _run_cli(
        tmp_path,
        pr_body_bytes=pr_body,
        facts=_facts(pr_number=NATIVE_PR),
        issue_body=issue_body,
        extra=extra,
    )


def test_given_native_facts_file_when_cli_runs_then_json_with_exit_zero_for_every_status(tmp_path):
    clear = _run_native_cli(tmp_path, native_facts=_native_facts())
    assert clear.returncode == 0, clear.stderr
    assert json.loads(clear.stdout)["status"] == "clear"

    blocked = _run_native_cli(
        tmp_path, native_facts=_native_facts(closing_relations=[{"number": REF_ISSUE, "repository": REF_REPO}])
    )
    assert blocked.returncode == 0
    payload = json.loads(blocked.stdout)
    assert (payload["status"], payload["reason_code"], payload["pr_number"]) == (
        "blocked",
        "native_relation_present",
        NATIVE_PR,
    )

    # a unreadable / non-JSON / missing native facts file is facts_invalid, still exit 0
    for kwargs in ({"native_facts": "{not json"}, {"native_facts": _native_facts(), "omit_native": True}):
        proc = _run_native_cli(tmp_path, **kwargs)
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["reason_code"] == "facts_invalid"

    # the single evaluator is reused: its fail_closed (here an unresolved Issue contract) is passed through
    stopped = _run_native_cli(tmp_path, native_facts=_native_facts(), issue_body="no section")
    assert json.loads(stopped.stdout)["reason_code"] == "reference_policy_fail_closed"
