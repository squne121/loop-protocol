"""
Fixture-based tests for check_issue_contract.py

Tests the C1-C11 deterministic checks using fixture Markdown files.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "check_issue_contract.py"
)
# contract_readiness_check.py is located in issue-contract-review skill
CONTRACT_READINESS_SCRIPT_PATH = (
    Path(__file__).parent.parent.parent / "issue-contract-review" / "scripts" / "contract_readiness_check.py"
)
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def run_checker(fixture_name: str) -> dict:
    """Run the checker script on a fixture file and return parsed JSON output."""
    fixture_path = FIXTURES_DIR / fixture_name
    assert fixture_path.exists(), f"Fixture file not found: {fixture_path}"

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--file", str(fixture_path), "--json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 1), (
        f"Script exited with unexpected code {result.returncode}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"Script output is not valid JSON: {e}\nstdout: {result.stdout}"
        ) from e


class TestPassCase:
    """GIVEN a well-formed implementation issue fixture WHEN checker runs THEN all checks pass."""

    def test_verdict_is_approve(self):
        """GIVEN pass_issue.md WHEN checker runs THEN verdict is approve."""
        output = run_checker("pass_issue.md")
        assert output["verdict"] == "approve", (
            f"Expected approve, got {output['verdict']}. "
            f"blocking_issues: {output.get('blocking_issues')}"
        )

    def test_all_deterministic_checks_pass(self):
        """GIVEN pass_issue.md WHEN checker runs THEN all C1-C11 are pass or n/a."""
        output = run_checker("pass_issue.md")
        checks = output["deterministic_checks"]
        allowed = {"pass", "n/a", "warn"}
        for check_id, result in checks.items():
            assert result in allowed, (
                f"Check {check_id} has unexpected result '{result}' for pass fixture"
            )

    def test_has_13_deterministic_checks(self):
        """GIVEN any fixture WHEN checker runs THEN deterministic_checks has exactly 13 keys (C1-C13)."""
        output = run_checker("pass_issue.md")
        checks = output["deterministic_checks"]
        assert len(checks) == 13, (
            f"Expected 13 deterministic checks (C1-C13), got {len(checks)}: {list(checks.keys())}"
        )

    def test_no_blocking_issues(self):
        """GIVEN pass_issue.md WHEN checker runs THEN blocking_issues is empty."""
        output = run_checker("pass_issue.md")
        assert output["blocking_issues"] == [], (
            f"Expected no blocking issues, got: {output['blocking_issues']}"
        )


class TestC1Fail:
    """GIVEN a fixture missing required sections WHEN checker runs THEN C1 fails."""

    def test_verdict_is_needs_fix(self):
        """GIVEN c1_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c1_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )

    def test_c1_fails(self):
        """GIVEN c1_fail_issue.md WHEN checker runs THEN C1_required_sections is fail."""
        output = run_checker("c1_fail_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C1_required_sections"] == "fail", (
            f"Expected C1 to fail, got {checks['C1_required_sections']}"
        )

    def test_c1_blocking_issue_message(self):
        """GIVEN c1_fail_issue.md WHEN checker runs THEN blocking_issues contains C1 message."""
        output = run_checker("c1_fail_issue.md")
        blocking = output["blocking_issues"]
        assert any("必須セクション" in msg or "Runtime Verification" in msg for msg in blocking), (
            f"Expected C1 blocking message in {blocking}"
        )


class TestC7Fail:
    """GIVEN a fixture with workflow skills in Required Skills WHEN checker runs THEN C7 fails."""

    def test_verdict_is_needs_fix(self):
        """GIVEN c7_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c7_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )

    def test_c7_fails(self):
        """GIVEN c7_fail_issue.md WHEN checker runs THEN C7_required_skills_semantics is fail."""
        output = run_checker("c7_fail_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C7_required_skills_semantics"] == "fail", (
            f"Expected C7 to fail, got {checks['C7_required_skills_semantics']}"
        )

    def test_c7_blocking_message_mentions_workflow_skill(self):
        """GIVEN c7_fail_issue.md WHEN checker runs THEN blocking_issues mentions workflow skill."""
        output = run_checker("c7_fail_issue.md")
        blocking = output["blocking_issues"]
        assert any("ワークフロースキル" in msg or "implement-issue" in msg for msg in blocking), (
            f"Expected workflow skill message in {blocking}"
        )


class TestC9Fail:
    """GIVEN a fixture missing Runtime Verification Applicability WHEN checker runs THEN C9 fails."""

    def test_verdict_is_needs_fix(self):
        """GIVEN c9_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c9_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )

    def test_c9_fails(self):
        """GIVEN c9_fail_issue.md WHEN checker runs THEN C9_runtime_applicability_present is fail or legacy_missing."""
        output = run_checker("c9_fail_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C9_runtime_applicability_present"] in ("fail", "legacy_missing_applicability"), (
            f"Expected C9 to fail or legacy_missing, got {checks['C9_runtime_applicability_present']}"
        )

    def test_c9_blocking_message(self):
        """GIVEN c9_fail_issue.md WHEN checker runs THEN blocking_issues contains C9 message."""
        output = run_checker("c9_fail_issue.md")
        blocking = output["blocking_issues"]
        assert any("Runtime Verification" in msg or "Applicability" in msg or "レガシー" in msg for msg in blocking), (
            f"Expected C9 blocking message in {blocking}"
        )


class TestC11Fail:
    """GIVEN a fixture with decision: immediate but no runtime-verification tags WHEN checker runs THEN C11 fails."""

    def test_verdict_is_needs_fix(self):
        """GIVEN c11_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c11_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )

    def test_c11_fails(self):
        """GIVEN c11_fail_issue.md WHEN checker runs THEN C11_decision_tag_consistency is fail."""
        output = run_checker("c11_fail_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C11_decision_tag_consistency"] == "fail", (
            f"Expected C11 to fail, got {checks['C11_decision_tag_consistency']}"
        )

    def test_c11_blocking_message(self):
        """GIVEN c11_fail_issue.md WHEN checker runs THEN blocking_issues contains C11 message."""
        output = run_checker("c11_fail_issue.md")
        blocking = output["blocking_issues"]
        assert any("immediate" in msg or "runtime-verification" in msg for msg in blocking), (
            f"Expected C11 blocking message in {blocking}"
        )


class TestJsonOutputStructure:
    """GIVEN any fixture WHEN checker runs with --json THEN output matches expected schema."""

    def test_json_has_required_keys(self):
        """GIVEN pass_issue.md WHEN checker runs THEN JSON has verdict, deterministic_checks, issue_kind, generated_at, etc."""
        output = run_checker("pass_issue.md")
        required_keys = {"verdict", "deterministic_checks", "blocking_issues", "non_blocking_improvements", "issue_kind", "generated_at"}
        missing = required_keys - set(output.keys())
        assert not missing, f"JSON output missing keys: {missing}"

    def test_all_c1_to_c11_keys_present(self):
        """GIVEN pass_issue.md WHEN checker runs THEN all C1-C11 keys are in deterministic_checks."""
        output = run_checker("pass_issue.md")
        checks = output["deterministic_checks"]
        expected_keys = {
            "C1_required_sections",
            "C2_stop_conditions_6",
            "C3_ac_checkbox_format",
            "C4_vc_commands_present",
            "C5_ac_vc_number_alignment",
            "C6_no_subjective_phrasing",
            "C7_required_skills_semantics",
            "C8_outcome_concreteness",
            "C9_runtime_applicability_present",
            "C10_deferred_destination_present",
            "C11_decision_tag_consistency",
        }
        missing = expected_keys - set(checks.keys())
        assert not missing, f"deterministic_checks missing keys: {missing}"


class TestC2Fail:
    """GIVEN a fixture with fewer than 6 stop conditions WHEN checker runs THEN C2 fails."""

    def test_c2_fail(self):
        """GIVEN c2_fail_issue.md WHEN checker runs THEN C2 is fail."""
        output = run_checker("c2_fail_issue.md")
        assert output["deterministic_checks"]["C2_stop_conditions_6"] == "fail", (
            f"Expected C2 to fail, got {output['deterministic_checks']['C2_stop_conditions_6']}"
        )

    def test_verdict_is_needs_fix(self):
        """GIVEN c2_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c2_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )


class TestC3Fail:
    """GIVEN a fixture with non-checkbox AC WHEN checker runs THEN C3 fails."""

    def test_c3_fail(self):
        """GIVEN c3_fail_issue.md WHEN checker runs THEN C3 is fail."""
        output = run_checker("c3_fail_issue.md")
        assert output["deterministic_checks"]["C3_ac_checkbox_format"] == "fail", (
            f"Expected C3 to fail, got {output['deterministic_checks']['C3_ac_checkbox_format']}"
        )

    def test_verdict_is_needs_fix(self):
        """GIVEN c3_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c3_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )


class TestC4Fail:
    """GIVEN a fixture with VC code block but no executable commands WHEN checker runs THEN C4 fails."""

    def test_c4_fail(self):
        """GIVEN c4_fail_issue.md WHEN checker runs THEN C4 is fail."""
        output = run_checker("c4_fail_issue.md")
        assert output["deterministic_checks"]["C4_vc_commands_present"] == "fail", (
            f"Expected C4 to fail, got {output['deterministic_checks']['C4_vc_commands_present']}"
        )

    def test_verdict_is_needs_fix(self):
        """GIVEN c4_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c4_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )


class TestC5Fail:
    """GIVEN a fixture where AC2 has no corresponding VC reference WHEN checker runs THEN C5 fails."""

    def test_c5_fail(self):
        """GIVEN c5_fail_issue.md WHEN checker runs THEN C5 is fail."""
        output = run_checker("c5_fail_issue.md")
        assert output["deterministic_checks"]["C5_ac_vc_number_alignment"] == "fail", (
            f"Expected C5 to fail, got {output['deterministic_checks']['C5_ac_vc_number_alignment']}"
        )

    def test_verdict_is_needs_fix(self):
        """GIVEN c5_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c5_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )


class TestC6Fail:
    """GIVEN a fixture with subjective phrasing in AC WHEN checker runs THEN C6 fails."""

    def test_c6_fail(self):
        """GIVEN c6_fail_issue.md WHEN checker runs THEN C6 is fail."""
        output = run_checker("c6_fail_issue.md")
        assert output["deterministic_checks"]["C6_no_subjective_phrasing"] == "fail", (
            f"Expected C6 to fail, got {output['deterministic_checks']['C6_no_subjective_phrasing']}"
        )

    def test_verdict_is_needs_fix(self):
        """GIVEN c6_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c6_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )


class TestC8Fail:
    """GIVEN a fixture with vague Outcome WHEN checker runs THEN C8 fails."""

    def test_c8_fail(self):
        """GIVEN c8_fail_issue.md WHEN checker runs THEN C8 is fail."""
        output = run_checker("c8_fail_issue.md")
        assert output["deterministic_checks"]["C8_outcome_concreteness"] == "fail", (
            f"Expected C8 to fail, got {output['deterministic_checks']['C8_outcome_concreteness']}"
        )

    def test_verdict_is_needs_fix(self):
        """GIVEN c8_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c8_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )


class TestC10Fail:
    """GIVEN a fixture with decision: deferred but no destination WHEN checker runs THEN C10 fails."""

    def test_c10_fail(self):
        """GIVEN c10_fail_issue.md WHEN checker runs THEN C10 is fail."""
        output = run_checker("c10_fail_issue.md")
        assert output["deterministic_checks"]["C10_deferred_destination_present"] == "fail", (
            f"Expected C10 to fail, got {output['deterministic_checks']['C10_deferred_destination_present']}"
        )

    def test_verdict_is_needs_fix(self):
        """GIVEN c10_fail_issue.md WHEN checker runs THEN verdict is needs-fix."""
        output = run_checker("c10_fail_issue.md")
        assert output["verdict"] == "needs-fix", (
            f"Expected needs-fix, got {output['verdict']}"
        )


class TestJsonOutputSchema:
    """GIVEN any fixture WHEN checker runs THEN JSON output has all required fields."""

    def test_json_output_schema(self):
        """GIVEN pass_issue.md WHEN checker runs THEN JSON has verdict, deterministic_checks, blocking_issues, non_blocking_improvements, issue_kind, generated_at."""
        output = run_checker("pass_issue.md")
        required_fields = {"verdict", "deterministic_checks", "blocking_issues", "non_blocking_improvements", "issue_kind", "generated_at"}
        missing = required_fields - set(output.keys())
        assert not missing, f"JSON output missing fields: {missing}"


class TestMachineReadableContractPriority:
    """GIVEN an issue with Machine-Readable Contract WHEN checker runs THEN issue_kind from contract takes priority."""

    def test_machine_readable_contract_priority(self):
        """GIVEN c2_fail_issue.md with issue_kind: implementation in contract WHEN checker runs THEN issue_kind is implementation."""
        # c2_fail_issue.md has issue_kind: implementation in the Machine-Readable Contract
        # and no labels/title prefix, so it should be detected from the contract
        output = run_checker("c2_fail_issue.md")
        assert output["issue_kind"] == "implementation", (
            f"Expected issue_kind=implementation from Machine-Readable Contract, got {output['issue_kind']}"
        )


# ---------------------------------------------------------------------------
# contract_readiness_execute integration tests (AC3, AC4, AC5, AC7, AC8, AC9)
# ---------------------------------------------------------------------------

# Inline fixture for human_judgment detection tests.
# This issue has a VC that calls a nonexistent tool, which produces exit 127
# (env_missing_dep → human_judgment status in contract_readiness_check.py).
# The key property: overall status == "human_judgment", NOT "needs_fix".
_HUMAN_JUDGMENT_ISSUE_CONTENT = """\
---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: human_judgment テスト用フィクスチャ（VC が env_missing_dep で human_judgment になる Issue）
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "human_judgment テスト用フィクスチャ"
change_kind: code
```

## Parent Issue

none

## Outcome

`human_judgment_issue.md` が baseline_vc_preflight で human_judgment 判定されることを確認する。

## Background

このフィクスチャは `--mode execute` の human_judgment 検出テスト用。VC の `$ loop-protocol-nonexistent-tool-xyz-abc` は常に exit 127 (command not found) を返すため、env_missing_dep → human_judgment となる。

## Parent Goal Ref

- Goal: human_judgment テスト用
- Desired Destination: human_judgment が検出されること

## Current Validated Scope

- テストフィクスチャのみ

## Remaining Parent Gaps

なし

## In Scope

- テストフィクスチャのみ

## Out of Scope

- その他のファイルの変更

## Required Skills

なし

## Acceptance Criteria

- [ ] AC1: `loop-protocol-nonexistent-tool-xyz-abc` が exit 127 を返すこと（human_judgment テスト用）

## Verification Commands

```bash
# AC1
$ loop-protocol-nonexistent-tool-xyz-abc
```

## Stop Conditions

- Allowed Paths 外の変更が必要な場合は停止
- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- スコープ外の refactoring が必要な場合は停止
- ビルドが壊れる場合は停止
- 依存関係の追加が必要な場合は停止

## Runtime Verification Applicability

decision: not_applicable
reason: テストフィクスチャ用。

## Allowed Paths

- `.claude/skills/review-issue/tests/test_check_issue_contract.py`
"""

# Inline fixture for unexpected_pass detection tests.
# This issue has a VC of `$ true` which always returns exit 0 at baseline,
# triggering unexpected_pass classification.
# Previously stored as fixtures/unexpected_pass_issue.md; inlined here so the
# fixtures/ directory does not need to be tracked for this AC.
_UNEXPECTED_PASS_ISSUE_CONTENT = """\
---
LABELS: phase/implementation,kind/implementation
TITLE: 実装: unexpected_pass テスト用フィクスチャ（VC が baseline で pass する Issue）
---
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "unexpected_pass テスト用フィクスチャ"
change_kind: code
```

## Parent Issue

none

## Outcome

`unexpected_pass_issue.md` が baseline_vc_preflight で unexpected_pass 判定されることを確認する。

## Background

このフィクスチャは `--mode execute` の unexpected_pass 検出テスト用。VC の `$ true` は実装前でも常に exit 0 を返すため unexpected_pass となる。

## Parent Goal Ref

- Goal: unexpected_pass テスト用
- Desired Destination: unexpected_pass が検出されること

## Current Validated Scope

- テストフィクスチャのみ

## Remaining Parent Gaps

なし

## In Scope

- テストフィクスチャのみ

## Out of Scope

- その他のファイルの変更

## Required Skills

なし

## Acceptance Criteria

- [ ] AC1: `true` コマンドが常に exit 0 を返すこと（unexpected_pass テスト用）

## Verification Commands

```bash
# AC1
$ true
```

## Stop Conditions

- Allowed Paths 外の変更が必要な場合は停止
- テストが修正できない場合は停止
- 既存の型定義と競合する場合は停止
- スコープ外の refactoring が必要な場合は停止
- ビルドが壊れる場合は停止
- 依存関係の追加が必要な場合は停止

## Runtime Verification Applicability

decision: not_applicable
reason: テストフィクスチャ用。

## Allowed Paths

- `.claude/skills/review-issue/tests/test_check_issue_contract.py`
"""


def run_contract_readiness(fixture_name: str, mode: str = "execute") -> tuple[dict, int]:
    """Run contract_readiness_check.py on a fixture file and return (parsed JSON, exit_code).

    Uses --body-file only (no --issue / gh / network) per AC7.
    shell=False is enforced by subprocess.run default per AC8.
    """
    fixture_path = FIXTURES_DIR / fixture_name
    assert fixture_path.exists(), f"Fixture file not found: {fixture_path}"

    result = subprocess.run(
        [sys.executable, str(CONTRACT_READINESS_SCRIPT_PATH), "--body-file", str(fixture_path), "--mode", mode],
        capture_output=True,
        text=True,
        shell=False,  # AC8: shell=True is NOT used
    )
    try:
        return json.loads(result.stdout), result.returncode
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"contract_readiness_check.py output is not valid JSON: {e}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        ) from e


def run_contract_readiness_from_content(content: str, mode: str = "execute") -> tuple[dict, int]:
    """Run contract_readiness_check.py with inline content via a temp file.

    Uses --body-file only (no --issue / gh / network) per AC7.
    shell=False is enforced by subprocess.run default per AC8.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    result = subprocess.run(
        [sys.executable, str(CONTRACT_READINESS_SCRIPT_PATH), "--body-file", tmp_path, "--mode", mode],
        capture_output=True,
        text=True,
        shell=False,  # AC8: shell=True is NOT used
    )
    Path(tmp_path).unlink(missing_ok=True)
    try:
        return json.loads(result.stdout), result.returncode
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"contract_readiness_check.py output is not valid JSON: {e}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        ) from e


class TestContractReadinessExecuteIntegration:
    """Integration tests for contract_readiness_check.py --mode execute via review-issue path.

    AC9: pytest contains integration fixture (not just mapper unit tests).
    AC3: unexpected_pass fixture → verdict: needs-fix + structured_blockers[].category == "unexpected_pass".
    """

    def test_execute_integration_mode_is_called(self):
        """GIVEN pass_issue.md fixture WHEN contract_readiness_check.py --mode execute runs
        THEN it returns ISSUE_CONTRACT_READINESS_RESULT_V1 schema (AC9: --mode execute actually called).

        Note: pass_issue.md has $ grep -r which may return exit 2 (file not found) → human_judgment.
        The key assertion is that the schema is ISSUE_CONTRACT_READINESS_RESULT_V1, confirming
        --mode execute was actually invoked (static mode would not call baseline_vc_preflight)."""
        result, exit_code = run_contract_readiness("pass_issue.md", mode="execute")
        assert result.get("schema") == "ISSUE_CONTRACT_READINESS_RESULT_V1", (
            f"Expected ISSUE_CONTRACT_READINESS_RESULT_V1 schema, got: {result.get('schema')}"
        )
        # --mode execute calls baseline_vc_preflight, which populates source_checks with
        # baseline_vc_preflight entry (absent in static mode).
        source_check_names = [s.get("name") for s in result.get("source_checks", [])]
        assert "baseline_vc_preflight" in source_check_names, (
            f"Expected 'baseline_vc_preflight' in source_checks (confirms --mode execute was called), "
            f"got: {source_check_names}"
        )

    def test_unexpected_pass_fixture_returns_needs_fix(self):
        """GIVEN inline unexpected_pass content (VC: $ true, always exit 0) WHEN --mode execute runs
        THEN status == needs_fix (AC3: verdict pathway produces needs-fix)."""
        result, exit_code = run_contract_readiness_from_content(_UNEXPECTED_PASS_ISSUE_CONTENT, mode="execute")
        assert result.get("status") == "needs_fix", (
            f"Expected needs_fix for unexpected_pass fixture, got: {result.get('status')}. "
            f"errors: {result.get('errors')}"
        )

    def test_unexpected_pass_category_in_errors(self):
        """GIVEN inline unexpected_pass content WHEN --mode execute runs
        THEN errors[] contains an entry with category == 'unexpected_pass' (AC3 full check)."""
        result, exit_code = run_contract_readiness_from_content(_UNEXPECTED_PASS_ISSUE_CONTENT, mode="execute")
        errors = result.get("errors", [])
        categories = [e.get("category") for e in errors]
        assert "unexpected_pass" in categories, (
            f"Expected 'unexpected_pass' category in errors, got: {categories}. "
            f"errors: {errors}"
        )

    def test_structured_blockers_lossless_passthrough(self):
        """GIVEN inline unexpected_pass content WHEN --mode execute runs
        THEN errors[] preserves source_check, source_payload fields (AC4: lossless pass-through)."""
        result, exit_code = run_contract_readiness_from_content(_UNEXPECTED_PASS_ISSUE_CONTENT, mode="execute")
        errors = result.get("errors", [])
        unexpected_pass_errors = [e for e in errors if e.get("category") == "unexpected_pass"]
        assert len(unexpected_pass_errors) > 0, (
            f"Expected at least one unexpected_pass error. errors: {errors}"
        )
        for err in unexpected_pass_errors:
            # AC4: source_check must be preserved
            assert "source_check" in err, f"error missing 'source_check': {err}"
            # AC4: source_payload with required sub-fields
            assert "source_payload" in err, f"error missing 'source_payload': {err}"
            payload = err["source_payload"]
            assert "decision" in payload, f"source_payload missing 'decision': {payload}"
            assert "classification" in payload, f"source_payload missing 'classification': {payload}"
            assert "exit_code" in payload, f"source_payload missing 'exit_code': {payload}"
            assert "command_hash" in payload, f"source_payload missing 'command_hash': {payload}"

    def test_human_judgment_not_collapsed_to_needs_fix(self):
        """GIVEN a fixture with a nonexistent-tool VC WHEN --mode execute runs
        THEN status == human_judgment (AC5: must NOT be collapsed to needs_fix).

        Uses _HUMAN_JUDGMENT_ISSUE_CONTENT which has VC: $ loop-protocol-nonexistent-tool-xyz-abc.
        That command exits with 127 (command not found) → env_missing_dep → human_judgment.
        The contract_readiness_check.py must NOT collapse this to needs_fix."""
        result, _exit_code = run_contract_readiness_from_content(_HUMAN_JUDGMENT_ISSUE_CONTENT, mode="execute")

        # The fixture must produce at least one error.
        # With the allowlist policy introduced in #514, unknown commands are classified as
        # command_not_allowed / blocked (exit_code=None, not executed) rather than
        # env_missing_dep / human_judgment (exit_code=127). Both must NOT be collapsed to needs_fix.
        errors = result.get("errors", [])
        non_needs_fix_errors = [
            e for e in errors
            if e.get("category") in ("env_missing_dep", "timeout", "unknown", "command_not_allowed")
            or e.get("source_payload", {}).get("decision") in ("human_judgment", "blocked")
        ]
        assert len(non_needs_fix_errors) > 0, (
            f"human_judgment/blocked fixture should produce at least one non-needs_fix error "
            f"(env_missing_dep/timeout/unknown/command_not_allowed). errors: {errors}"
        )

        # Overall status must be human_judgment or blocked (not needs_fix) (AC5)
        # command_not_allowed blocked VCs map to status=human_judgment at the contract level.
        assert result.get("status") in ("human_judgment", "blocked"), (
            f"non-needs_fix errors must NOT collapse overall status to needs_fix. "
            f"status={result.get('status')}, non_needs_fix_errors={non_needs_fix_errors}"
        )

        # Confirm: no error has been re-classified as needs_fix in source_payload
        for err in non_needs_fix_errors:
            payload = err.get("source_payload", {})
            assert payload.get("classification") != "needs_fix", (
                f"blocked/human_judgment error must not be reclassified as needs_fix in source_payload: {err}"
            )

    def test_execute_uses_body_file_only_no_network(self):
        """GIVEN a fixture file WHEN --mode execute runs with --body-file
        THEN it succeeds without requiring gh auth or network access (AC7).

        This is verified by running with --body-file only (no --issue / --repo).
        If gh were required, it would fail in offline / unauthed environments."""
        result, exit_code = run_contract_readiness("pass_issue.md", mode="execute")
        # Verify we got a valid ISSUE_CONTRACT_READINESS_RESULT_V1 response (not an auth error)
        assert result.get("schema") == "ISSUE_CONTRACT_READINESS_RESULT_V1", (
            f"Expected ISSUE_CONTRACT_READINESS_RESULT_V1, got: {result.get('schema')}"
        )
        # Input error (gh auth / network) would set status=human_judgment with INPUT001 rule_id
        errors = result.get("errors", [])
        input_errors = [e for e in errors if e.get("rule_id") == "INPUT001"]
        assert len(input_errors) == 0, (
            f"INPUT001 (network/auth) errors should not appear when using --body-file: {input_errors}"
        )

    def test_shell_false_enforced(self):
        """GIVEN contract_readiness_check.py subprocess call WHEN it runs
        THEN shell=False is used (AC8: shell=True must NOT be introduced).

        Verifies by inspecting the script source for shell=True patterns."""
        script_content = CONTRACT_READINESS_SCRIPT_PATH.read_text(encoding="utf-8")
        assert "shell=True" not in script_content, (
            "contract_readiness_check.py must not use shell=True. "
            "Existing shell=False convention must be preserved."
        )


class TestJsonStdoutPurity:
    """GIVEN --json flag WHEN checker runs THEN stdout contains only valid JSON (AC1/AC3: #574)."""

    def _run_raw(self, fixture_name: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        """Run checker with --json and return raw CompletedProcess (stdout/stderr unmodified)."""
        import os
        fixture_path = FIXTURES_DIR / fixture_name
        assert fixture_path.exists(), f"Fixture file not found: {fixture_path}"

        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)

        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--file", str(fixture_path), "--json"],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_stdout_is_valid_json_only(self):
        """GIVEN pass_issue.md with --json flag WHEN checker runs
        THEN stdout is exactly one valid JSON object with no extra bytes."""
        result = self._run_raw("pass_issue.md")
        assert result.returncode in (0, 1), (
            f"Unexpected exit code {result.returncode}. stderr: {result.stderr}"
        )
        # stdout must parse as JSON without any leading/trailing non-JSON text
        stripped = result.stdout.strip()
        assert stripped, "stdout must not be empty in --json mode"
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as e:
            raise AssertionError(
                f"stdout is not valid JSON: {e}\nstdout repr: {repr(result.stdout[:500])}"
            ) from e
        assert isinstance(parsed, dict), f"JSON output must be a dict, got {type(parsed)}"
        assert "verdict" in parsed, "JSON output must contain 'verdict' field"

    def test_stdout_contains_no_warning_text(self):
        """GIVEN pass_issue.md with --json flag WHEN checker runs
        THEN stdout contains no DeprecationWarning or warning text."""
        result = self._run_raw("pass_issue.md")
        assert "DeprecationWarning" not in result.stdout, (
            f"stdout must not contain DeprecationWarning. stdout: {repr(result.stdout[:500])}"
        )
        assert "Warning" not in result.stdout, (
            f"stdout must not contain Warning text. stdout: {repr(result.stdout[:500])}"
        )

    def test_no_deprecation_warning_with_pythonwarnings_error(self):
        """GIVEN PYTHONWARNINGS=error::DeprecationWarning WHEN --json checker runs
        THEN exit code is 0 or 1 (not 2/crash from DeprecationWarning treated as error)."""
        result = self._run_raw(
            "pass_issue.md",
            extra_env={"PYTHONWARNINGS": "error::DeprecationWarning"},
        )
        assert result.returncode in (0, 1), (
            f"DeprecationWarning was raised as an error (exit code {result.returncode}). "
            f"stderr: {result.stderr}\nstdout: {result.stdout}"
        )
        # stdout must still be valid JSON
        stripped = result.stdout.strip()
        assert stripped, "stdout must not be empty even with PYTHONWARNINGS=error"
        parsed = json.loads(stripped)
        assert "verdict" in parsed

    def test_utcnow_replaced_in_source(self):
        """GIVEN check_issue_contract.py source WHEN inspected
        THEN datetime.datetime.utcnow() is not present (AC2: #574)."""
        script_content = SCRIPT_PATH.read_text(encoding="utf-8")
        assert "datetime.utcnow()" not in script_content, (
            "datetime.datetime.utcnow() must be replaced with "
            "datetime.datetime.now(datetime.timezone.utc) — see Issue #574 AC2."
        )

    def test_generated_at_field_is_present_in_json_output(self):
        """GIVEN pass_issue.md with --json flag WHEN checker runs
        THEN output JSON contains 'generated_at' field with UTC timestamp."""
        result = self._run_raw(
            "pass_issue.md",
            extra_env={"PYTHONWARNINGS": "error::DeprecationWarning"},
        )
        assert result.returncode in (0, 1)
        parsed = json.loads(result.stdout.strip())
        assert "generated_at" in parsed, "JSON output must contain 'generated_at' field"
        # Verify it looks like a UTC timestamp (ends with Z)
        generated_at = parsed["generated_at"]
        assert generated_at.endswith("Z"), (
            f"generated_at must end with 'Z' (UTC), got: {generated_at}"
        )


# ---------------------------------------------------------------------------
# AC3 / AC3b: C5 grouped AC comment support (Issue #814)
# ---------------------------------------------------------------------------

# Import the check function directly for unit-level testing
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from check_issue_contract import check_c5_ac_vc_alignment, _extract_vc_ac_refs  # noqa: E402


class TestC5GroupedAcComment:
    """AC3: C5 parser recognises grouped AC comments (# AC2, AC3, AC4).
    AC3b: range notation (# AC2-AC4) is NOT supported.
    """

    def _body_with(self, ac_section: str, vc_section: str) -> str:
        return (
            "## Acceptance Criteria\n\n"
            + ac_section
            + "\n\n## Verification Commands\n\n"
            + "```bash\n"
            + vc_section
            + "\n```\n"
        )

    def test_single_ac_reference_still_works(self):
        """Single # AC1 reference is recognised (regression)."""
        body = self._body_with(
            "- [ ] AC1: foo\n",
            "# AC1\n$ uv run pytest tests/ -q\n",
        )
        result, issues = check_c5_ac_vc_alignment(body)
        assert result == "pass", f"Expected pass, got {result}. issues: {issues}"

    def test_grouped_ac_comment_all_recognised(self):
        """AC3: # AC2, AC3, AC4 grouped comment -> all three numbers extracted as referenced."""
        body = self._body_with(
            "- [ ] AC2: bar\n- [ ] AC3: baz\n- [ ] AC4: qux\n",
            "# AC2, AC3, AC4\n$ uv run pytest tests/ -q\n",
        )
        result, issues = check_c5_ac_vc_alignment(body)
        assert result == "pass", f"Expected pass, got {result}. issues: {issues}"

    def test_grouped_ac_comment_partial_coverage(self):
        """Grouped comment covering only some ACs -> fail for the uncovered ones."""
        body = self._body_with(
            "- [ ] AC1: first\n- [ ] AC2: second\n- [ ] AC3: third\n",
            "# AC1, AC2\n$ uv run pytest tests/ -q\n",
        )
        result, issues = check_c5_ac_vc_alignment(body)
        assert result == "fail", f"Expected fail (AC3 uncovered), got {result}"
        assert any("AC3" in msg for msg in issues), f"Expected AC3 in issues: {issues}"

    def test_extract_vc_ac_refs_single(self):
        """_extract_vc_ac_refs returns single number correctly."""
        refs = _extract_vc_ac_refs("# AC1\n$ foo")
        assert refs == {"1"}

    def test_extract_vc_ac_refs_grouped(self):
        """_extract_vc_ac_refs returns all numbers from grouped comment."""
        refs = _extract_vc_ac_refs("# AC2, AC3, AC4\n$ foo")
        assert refs == {"2", "3", "4"}

    def test_extract_vc_ac_refs_multiple_lines(self):
        """_extract_vc_ac_refs handles multiple comment lines."""
        vc = "# AC1\n$ cmd1\n# AC2, AC3\n$ cmd2\n"
        refs = _extract_vc_ac_refs(vc)
        assert refs == {"1", "2", "3"}

    def test_range_notation_not_supported(self):
        """AC3b: range notation # AC2-AC4 is NOT treated as grouped (not in scope).

        Range '# AC2-AC4' only extracts AC2 (the first number matched),
        because '-AC4' is not the comma pattern. The '-' is not a separator.
        This is the expected non-support behaviour per AC3b.
        """
        # '# AC2-AC4' should NOT yield {"2","3","4"}
        refs = _extract_vc_ac_refs("# AC2-AC4\n$ foo")
        # Only AC2 is captured; AC3 and AC4 are NOT (range not supported)
        assert "3" not in refs, (
            "Range notation # AC2-AC4 should NOT yield AC3 (range not supported per AC3b)"
        )
        assert "4" not in refs, (
            "Range notation # AC2-AC4 should NOT yield AC4 (range not supported per AC3b)"
        )

    def test_grouped_with_spaces_around_comma(self):
        """Grouped AC refs with varying whitespace are parsed correctly."""
        refs = _extract_vc_ac_refs("# AC1 , AC2 , AC3\n$ foo")
        assert "1" in refs
        assert "2" in refs
        assert "3" in refs
