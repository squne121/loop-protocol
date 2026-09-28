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
