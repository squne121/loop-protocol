"""Issue #2811 AC4: validate_pr_body.py validate-if-present IMPLEMENTATION_SCOPE_COVERAGE_V1 check."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from validate_pr_body import validate_pr_body

ROOT = Path(__file__).resolve().parents[5]
EVIDENCE = ROOT / ".claude/skills/impl-review-loop/scripts/implementation_landed_evidence.py"
ISSUE = 2811
CHANGED_PATHS = ["docs/dev/foo.md"]

BASE_BODY = """## Summary

- PR body validator の marker 検証（実装計画）

## Checks

- [ ] `pnpm typecheck`

## Schema Change Applicability

- decision: not_schema_change
- reason: Python validator と test のみを変更するため

## Schema Consumer Inventory

N/A
reason: schema を変更しないため inventory は不要

## Safety Claim Matrix

N/A
reason: safety-sensitive path に該当しない

## Notes

- Related issue: #2811
- 上記は関連する Issue 番号です
- Closes #2811（対象 Issue）

## 受け入れ条件の達成状況

- [x] AC4: 達成（テスト）

## 検証コマンド結果

```text
$ pnpm typecheck
pass
```

## Allowed Paths 遵守

- 変更ファイル: テストのみ
- Allowed Paths 逸脱: なし
"""


def _load_evidence():
    spec = importlib.util.spec_from_file_location("evidence_for_validate_pr_body_marker_test", EVIDENCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _issue_body() -> str:
    return """## Machine-Readable Contract
```yaml
goal_ref: marker goal
change_kind: code
```
## In Scope
- validate marker
## Acceptance Criteria
- [ ] AC4: marker validates
## Allowed Paths
- `.claude/a.py`
"""


def _malformed_block() -> str:
    zeros = "0" * 64
    return (
        "```yaml\n"
        "IMPLEMENTATION_SCOPE_COVERAGE_V1:\n"
        '  schema_version: "WRONG_SCHEMA"\n'
        f"  issue_number: {ISSUE}\n"
        f'  issue_body_sha256: "sha256:{zeros}"\n'
        f'  normalized_scope_manifest_sha256: "sha256:{zeros}"\n'
        '  pr_head_sha: "not-a-valid-sha"\n'
        "  scope_manifest: {}\n"
        "```\n"
    )


def _lp059(result) -> list:
    return [error for error in result.errors if error.rule_id == "LP059"]


def test_validate_if_present_marker_check():
    evidence = _load_evidence()

    # marker token/block absent: allowed as before.
    absent = validate_pr_body(BASE_BODY, CHANGED_PATHS, linked_issue=ISSUE)
    assert absent.status == "pass", absent.errors
    assert _lp059(absent) == []

    # marker present + canonical parser valid: allowed.
    marker = evidence.build_scope_coverage_marker(issue_number=ISSUE, issue_body=_issue_body(), pr_head_sha="a" * 40)
    valid_body = BASE_BODY + "\n" + evidence.render_scope_coverage_marker(marker) + "\n"
    valid = validate_pr_body(valid_body, CHANGED_PATHS, linked_issue=ISSUE)
    assert valid.status == "pass", valid.errors
    assert _lp059(valid) == []

    # marker present + canonical parser invalid: fail-closed.
    invalid_body = BASE_BODY + "\n" + _malformed_block()
    invalid = validate_pr_body(invalid_body, CHANGED_PATHS, linked_issue=ISSUE)
    assert invalid.status == "fail"
    errors = _lp059(invalid)
    assert len(errors) == 1
    assert "scope_coverage_schema_version_invalid" in errors[0].message
    assert "scope_coverage_pr_head_invalid" in errors[0].message

    # marker for a different linked Issue is an identity mismatch: fail-closed.
    other_issue = validate_pr_body(valid_body, CHANGED_PATHS, linked_issue=ISSUE + 1)
    assert other_issue.status == "fail"
    assert "scope_coverage_issue_identity_mismatch" in _lp059(other_issue)[0].message


def test_token_without_parseable_marker_fence_is_treated_as_absent():
    """A prose mention of the token (no parseable fence) is `marker_missing`, i.e. absent, not malformed."""
    body = BASE_BODY + "\nIMPLEMENTATION_SCOPE_COVERAGE_V1: は生成済みです。\n"
    result = validate_pr_body(body, CHANGED_PATHS, linked_issue=ISSUE)
    assert result.status == "pass", result.errors
    assert _lp059(result) == []
