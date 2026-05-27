"""
Fixture-based tests for C12 product trace fields structure check.

Tests C12 deterministic check and non-blocking warnings (scope mismatch, VC anti-pattern).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "check_issue_contract.py"
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


class TestC12ProductTraceFields:
    """C12: Product trace fields structure check."""

    def test_c12_missing_trace_fields_fails(self):
        """GIVEN c12_missing_trace_fields_issue.md (product_spec_id/requirement_id/source_task_id not all present)
        WHEN checker runs THEN C12_product_trace_fields_structure is fail."""
        output = run_checker("c12_missing_trace_fields_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C12_product_trace_fields_structure"] == "fail", (
            f"Expected C12 to fail, got {checks['C12_product_trace_fields_structure']}"
        )
        assert output["verdict"] == "needs-fix"

    def test_c12_not_applicable_issue(self):
        """GIVEN c12_not_applicable_issue.md (no Product Spec Context)
        WHEN checker runs THEN C12_product_trace_fields_structure is n/a."""
        output = run_checker("c12_not_applicable_issue.md")
        checks = output["deterministic_checks"]
        assert checks["C12_product_trace_fields_structure"] == "n/a", (
            f"Expected C12 to be n/a, got {checks['C12_product_trace_fields_structure']}"
        )

    def test_c12_fixture_present(self):
        """GIVEN pass_issue.md WHEN checker runs THEN deterministic_checks has C12."""
        output = run_checker("pass_issue.md")
        checks = output["deterministic_checks"]
        assert "C12_product_trace_fields_structure" in checks, (
            "C12_product_trace_fields_structure not found in deterministic_checks"
        )


class TestVCUntrackFalseNegative:
    """vc_untracked_false_negative_pattern warning."""

    def test_vc_untracked_false_negative_detected(self):
        """GIVEN vc_untracked_false_negative_issue.md (git status | grep -v pattern)
        WHEN checker runs THEN non_blocking_improvements includes vc_untracked_false_negative_pattern."""
        output = run_checker("vc_untracked_false_negative_issue.md")
        warnings = output.get("non_blocking_improvements", [])
        codes = [w.get("code") for w in warnings]
        assert "vc_untracked_false_negative_pattern" in codes, (
            f"Expected vc_untracked_false_negative_pattern in warnings, got codes: {codes}"
        )


class TestVCNegativeGrepWithoutLiteral:
    """vc_negative_grep_without_literal_inventory warning."""

    def test_vc_negative_grep_without_literal_detected(self):
        """GIVEN vc_negative_grep_without_literal_issue.md (deletion + ! grep without literal list)
        WHEN checker runs THEN non_blocking_improvements includes vc_negative_grep_without_literal_inventory."""
        output = run_checker("vc_negative_grep_without_literal_issue.md")
        warnings = output.get("non_blocking_improvements", [])
        codes = [w.get("code") for w in warnings]
        assert "vc_negative_grep_without_literal_inventory" in codes, (
            f"Expected vc_negative_grep_without_literal_inventory in warnings, got codes: {codes}"
        )


class TestC1MissingSectionSentinel:
    """C1 missing section skeleton generation (sentinel marker)."""

    def test_c1_fail_has_blocking_issue(self):
        """GIVEN c1_missing_sections_issue.md (missing AC / VC / etc)
        WHEN checker runs THEN blocking_issues contains C1 failures."""
        output = run_checker("c1_missing_sections_issue.md")
        assert output["verdict"] == "needs-fix"
        blocking = output.get("blocking_issues", [])
        assert len(blocking) > 0, "Expected blocking_issues for missing sections"

    def test_c1_diff_proposal_contains_missing_section_skeleton(self):
        """PR #390 REQUEST_CHANGES blocker 2/3: AC6 enforcement.
        GIVEN c1_missing_sections_issue.md
        WHEN checker runs THEN diff_proposal.add contains entries with kind=missing_section_skeleton
             and placeholder_source is either 'template' or 'fallback_todo'."""
        output = run_checker("c1_missing_sections_issue.md")
        adds = output["diff_proposal"]["add"]
        skeleton_entries = [item for item in adds if item.get("kind") == "missing_section_skeleton"]
        assert len(skeleton_entries) > 0, (
            f"Expected at least one missing_section_skeleton entry in diff_proposal.add, got: {adds}"
        )
        for entry in skeleton_entries:
            assert "section" in entry, f"Entry missing 'section' key: {entry}"
            assert "skeleton" in entry, f"Entry missing 'skeleton' key: {entry}"
            assert entry.get("placeholder_source") in ("template", "fallback_todo"), (
                f"Entry placeholder_source must be 'template' or 'fallback_todo', got: {entry.get('placeholder_source')}"
            )


class TestC12ValidPass:
    """PR #390 REQUEST_CHANGES blocker 2: AC1 valid-case enforcement."""

    def test_c12_valid_trace_fields_passes(self):
        """GIVEN c12_valid_trace_fields_issue.md (3 fields present, valid format)
        WHEN checker runs THEN C12 == pass."""
        output = run_checker("c12_valid_trace_fields_issue.md")
        assert output["deterministic_checks"]["C12_product_trace_fields_structure"] == "pass", (
            f"Expected C12 pass, got {output['deterministic_checks']['C12_product_trace_fields_structure']}; "
            f"blocking={output.get('blocking_issues')}"
        )


class TestC12InvalidCases:
    """PR #390 REQUEST_CHANGES blocker 2: AC1 per-field invalid-case enforcement."""

    @pytest.mark.parametrize("fixture,expected_substr", [
        ("c12_missing_product_spec_id_issue.md", "product_spec_id"),
        ("c12_missing_requirement_id_issue.md", "requirement_id"),
        ("c12_missing_source_task_id_issue.md", "source_task_id"),
        ("c12_placeholder_trace_fields_issue.md", "placeholder"),
        ("c12_invalid_requirement_id_issue.md", "requirement_id"),
        ("c12_invalid_source_task_id_issue.md", "source_task_id"),
    ])
    def test_c12_invalid_cases_fail(self, fixture, expected_substr):
        """Each invalid C12 fixture must produce C12 == fail with a blocking_issue mentioning the
        offending field or 'placeholder'."""
        output = run_checker(fixture)
        assert output["deterministic_checks"]["C12_product_trace_fields_structure"] == "fail", (
            f"{fixture}: expected C12 fail, got "
            f"{output['deterministic_checks']['C12_product_trace_fields_structure']}"
        )
        blocking_text = "\n".join(output.get("blocking_issues", []))
        assert expected_substr in blocking_text, (
            f"{fixture}: expected '{expected_substr}' in blocking_issues, got: {blocking_text}"
        )


class TestScopeCVSInScopeMismatchWarning:
    """PR #390 REQUEST_CHANGES blocker 2: AC3 enforcement."""

    def test_scope_cvs_in_scope_mismatch_detected(self):
        """GIVEN scope_cvs_in_scope_mismatch_issue.md (CVS/In Scope tokens disjoint)
        WHEN checker runs THEN non_blocking_improvements includes scope_cvs_in_scope_mismatch."""
        output = run_checker("scope_cvs_in_scope_mismatch_issue.md")
        codes = [w.get("code") for w in output.get("non_blocking_improvements", [])]
        assert "scope_cvs_in_scope_mismatch" in codes, (
            f"Expected scope_cvs_in_scope_mismatch warning, got: {codes}"
        )


class TestScopeCVSInScopeMismatchExtended:
    """Issue #396: scope_cvs_in_scope_mismatch tokenization extension tests (AC1-AC7)."""

    def test_scope_cvs_bare_path_mismatch_warns(self):
        """GIVEN scope_cvs_in_scope_mismatch_bare_path_mismatch.md (bare path tokens on each side differ)
        WHEN checker runs THEN scope_cvs_in_scope_mismatch warning is present."""
        output = run_checker("scope_cvs_in_scope_mismatch_bare_path_mismatch.md")
        codes = [w.get("code") for w in output.get("non_blocking_improvements", [])]
        assert "scope_cvs_in_scope_mismatch" in codes, (
            f"Expected scope_cvs_in_scope_mismatch warning for bare path mismatch, got: {codes}"
        )

    def test_scope_cvs_bare_path_overlap_no_warn(self):
        """GIVEN scope_cvs_in_scope_mismatch_bare_path_overlap.md (same bare paths on both sides)
        WHEN checker runs THEN scope_cvs_in_scope_mismatch warning is NOT present."""
        output = run_checker("scope_cvs_in_scope_mismatch_bare_path_overlap.md")
        codes = [w.get("code") for w in output.get("non_blocking_improvements", [])]
        assert "scope_cvs_in_scope_mismatch" not in codes, (
            f"Expected no scope_cvs_in_scope_mismatch warning for bare path overlap, got: {codes}"
        )

    def test_scope_cvs_natural_en_mismatch_warns(self):
        """GIVEN scope_cvs_in_scope_mismatch_natural_en_mismatch.md (English natural text mismatch)
        WHEN checker runs THEN scope_cvs_in_scope_mismatch warning is present."""
        output = run_checker("scope_cvs_in_scope_mismatch_natural_en_mismatch.md")
        codes = [w.get("code") for w in output.get("non_blocking_improvements", [])]
        assert "scope_cvs_in_scope_mismatch" in codes, (
            f"Expected scope_cvs_in_scope_mismatch warning for natural English mismatch, got: {codes}"
        )

    def test_scope_cvs_natural_en_overlap_no_warn(self):
        """GIVEN scope_cvs_in_scope_mismatch_natural_en_overlap.md (English natural text with meaningful overlap)
        WHEN checker runs THEN scope_cvs_in_scope_mismatch warning is NOT present."""
        output = run_checker("scope_cvs_in_scope_mismatch_natural_en_overlap.md")
        codes = [w.get("code") for w in output.get("non_blocking_improvements", [])]
        assert "scope_cvs_in_scope_mismatch" not in codes, (
            f"Expected no scope_cvs_in_scope_mismatch warning for natural English overlap, got: {codes}"
        )

    def test_scope_cvs_boilerplate_only_overlap_warns(self):
        """GIVEN scope_cvs_in_scope_mismatch_boilerplate_only_overlap.md (stop-tokens only in common)
        WHEN checker runs THEN scope_cvs_in_scope_mismatch warning is still present (stop-tokens don't count)."""
        output = run_checker("scope_cvs_in_scope_mismatch_boilerplate_only_overlap.md")
        codes = [w.get("code") for w in output.get("non_blocking_improvements", [])]
        assert "scope_cvs_in_scope_mismatch" in codes, (
            f"Expected scope_cvs_in_scope_mismatch warning when only stop-tokens match, got: {codes}"
        )

    def test_scope_cvs_punct_normalization_no_warn(self):
        """GIVEN scope_cvs_in_scope_mismatch_punct_normalization.md (foo.py, and foo.py should match)
        WHEN checker runs THEN scope_cvs_in_scope_mismatch warning is NOT present."""
        output = run_checker("scope_cvs_in_scope_mismatch_punct_normalization.md")
        codes = [w.get("code") for w in output.get("non_blocking_improvements", [])]
        assert "scope_cvs_in_scope_mismatch" not in codes, (
            f"Expected no scope_cvs_in_scope_mismatch warning after punctuation normalization, got: {codes}"
        )

    def test_scope_cvs_extension_coverage_no_warn(self):
        """GIVEN scope_cvs_in_scope_mismatch_extension_coverage.md (.yaml/.tsx/.json/.sh/.yml paths)
        WHEN checker runs THEN scope_cvs_in_scope_mismatch warning is NOT present (all paths match)."""
        output = run_checker("scope_cvs_in_scope_mismatch_extension_coverage.md")
        codes = [w.get("code") for w in output.get("non_blocking_improvements", [])]
        assert "scope_cvs_in_scope_mismatch" not in codes, (
            f"Expected no scope_cvs_in_scope_mismatch warning for extension coverage overlap, got: {codes}"
        )

    def test_scope_cvs_one_side_empty_no_warn(self):
        """GIVEN scope_cvs_in_scope_mismatch_one_side_empty.md (one side yields 0 tokens)
        WHEN checker runs THEN scope_cvs_in_scope_mismatch warning is NOT present."""
        output = run_checker("scope_cvs_in_scope_mismatch_one_side_empty.md")
        codes = [w.get("code") for w in output.get("non_blocking_improvements", [])]
        assert "scope_cvs_in_scope_mismatch" not in codes, (
            f"Expected no scope_cvs_in_scope_mismatch warning when one side is empty, got: {codes}"
        )

    def test_scope_cvs_japanese_only_no_warn(self):
        """GIVEN scope_cvs_in_scope_mismatch_japanese_only.md (Japanese text only, no ASCII tokens)
        WHEN checker runs THEN scope_cvs_in_scope_mismatch warning is NOT present (no extractable tokens)."""
        output = run_checker("scope_cvs_in_scope_mismatch_japanese_only.md")
        codes = [w.get("code") for w in output.get("non_blocking_improvements", [])]
        assert "scope_cvs_in_scope_mismatch" not in codes, (
            f"Expected no scope_cvs_in_scope_mismatch warning for Japanese-only text, got: {codes}"
        )

    def test_scope_cvs_evidence_has_four_fields(self):
        """GIVEN scope_cvs_in_scope_mismatch_issue.md (warning should be present)
        WHEN checker runs THEN the warning evidence contains jaccard, overlap, missing_from_cvs, missing_from_in_scope."""
        output = run_checker("scope_cvs_in_scope_mismatch_issue.md")
        warnings = [w for w in output.get("non_blocking_improvements", [])
                    if w.get("code") == "scope_cvs_in_scope_mismatch"]
        assert len(warnings) == 1, f"Expected exactly 1 scope_cvs_in_scope_mismatch warning, got {len(warnings)}"

        evidence = warnings[0]["evidence"]
        # evidence is a list of dicts, each with one key
        evidence_keys: set[str] = set()
        for item in evidence:
            if isinstance(item, dict):
                evidence_keys.update(item.keys())

        assert "jaccard" in evidence_keys, f"Expected 'jaccard' in evidence keys, got: {evidence_keys}"
        assert "overlap" in evidence_keys, f"Expected 'overlap' in evidence keys, got: {evidence_keys}"
        assert "missing_from_cvs" in evidence_keys, f"Expected 'missing_from_cvs' in evidence keys, got: {evidence_keys}"
        assert "missing_from_in_scope" in evidence_keys, f"Expected 'missing_from_in_scope' in evidence keys, got: {evidence_keys}"

        # jaccard must be float-compatible
        jaccard_val = next(item["jaccard"] for item in evidence if isinstance(item, dict) and "jaccard" in item)
        assert isinstance(jaccard_val, (int, float)), f"jaccard must be numeric, got {type(jaccard_val)}"
        assert 0.0 <= jaccard_val <= 1.0, f"jaccard must be in [0, 1], got {jaccard_val}"


class TestNonBlockingWarningsStructure:
    """non_blocking_improvements structure validation."""

    def test_non_blocking_warning_has_required_fields(self):
        """GIVEN a fixture with warnings
        WHEN checker runs THEN each warning has code, severity, evidence, suggested_action fields."""
        output = run_checker("vc_untracked_false_negative_issue.md")
        warnings = output.get("non_blocking_improvements", [])

        for w in warnings:
            assert "code" in w, "Warning missing 'code' field"
            assert "severity" in w, "Warning missing 'severity' field"
            assert "evidence" in w, "Warning missing 'evidence' field"
            assert "suggested_action" in w, "Warning missing 'suggested_action' field"


class TestC12YamlSafeLoad:
    """PR #390 review-2 blocker 3: MRC YAML を yaml.safe_load で parse する。"""

    def test_c12_yaml_inline_comment_not_false_positive(self, tmp_path):
        """GIVEN MRC YAML with inline comment on requirement_id / source_task_id
        WHEN checker runs THEN C12 == pass (comment は YAML semantics で除去される)."""
        fixture = tmp_path / "c12_inline_comment.md"
        fixture.write_text(
            "---\n"
            "LABELS: phase/implementation,kind/implementation\n"
            "TITLE: 実装: yaml inline comment\n"
            "---\n"
            "## Machine-Readable Contract\n\n"
            "```yaml\n"
            "contract_schema_version: v1\n"
            "issue_kind: implementation\n"
            'parent_issue: "#300"\n'
            'goal_ref: "yaml inline comment test"\n'
            "change_kind: code\n"
            'product_spec_id: "features/game-core"  # spec id\n'
            "requirement_id: REQ-001  # generated from spec\n"
            "source_task_id: T001  # task id\n"
            "```\n\n"
            "## Outcome\n\nXを実装する。\n\n"
            "## Acceptance Criteria\n\n- [ ] AC1: X\n\n"
            "## Verification Commands\n\n```bash\n# AC1\n$ test -f X\n```\n\n"
            "## Stop Conditions\n\n- 1\n- 2\n- 3\n- 4\n- 5\n- 6\n\n"
            "## Runtime Verification Applicability\n\ndecision: not_applicable\n\n"
            "## Allowed Paths\n\n- `X`\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--file", str(fixture), "--json"],
            capture_output=True, text=True,
        )
        output = json.loads(result.stdout)
        assert output["deterministic_checks"]["C12_product_trace_fields_structure"] == "pass", (
            f"yaml inline comment should not trigger format fail. "
            f"Got {output['deterministic_checks']['C12_product_trace_fields_structure']}, "
            f"blocking={output.get('blocking_issues')}"
        )


class TestC12ApplicabilityRestrictedToStructuredSections:
    """PR #390 REQUEST_CHANGES blocker 1: applicability は構造化セクション限定。"""

    def test_narrative_mention_only_not_applicable(self, tmp_path):
        """本文 narrative に 'source_task_id' という語が登場するだけで構造化されていない場合、
        C12 == n/a になること (本文全体 word match での誤 block を防ぐ)。"""
        # 構造化セクション (MRC YAML / Product Spec Context) 無し
        # narrative の Out of Scope に "source_task_id" を含むが colon 構造ではない
        fixture = tmp_path / "narrative_mention_only.md"
        fixture.write_text(
            "---\n"
            "LABELS: phase/implementation,kind/implementation\n"
            "TITLE: 実装: narrative-only mention\n"
            "---\n"
            "## Machine-Readable Contract\n\n"
            "```yaml\n"
            'contract_schema_version: v1\n'
            'issue_kind: implementation\n'
            'parent_issue: "none"\n'
            'goal_ref: "narrative only"\n'
            'change_kind: code\n'
            "```\n\n"
            "## Outcome\n\nXを実装する。\n\n"
            "## Out of Scope\n\n- source_task_id は本 Issue では扱わない\n\n"
            "## Acceptance Criteria\n\n- [ ] AC1: X\n\n"
            "## Verification Commands\n\n```bash\n# AC1\n$ test -f X\n```\n\n"
            "## Stop Conditions\n\n- 1\n- 2\n- 3\n- 4\n- 5\n- 6\n\n"
            "## Runtime Verification Applicability\n\ndecision: not_applicable\n\n"
            "## Allowed Paths\n\n- `X`\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--file", str(fixture), "--json"],
            capture_output=True, text=True,
        )
        output = json.loads(result.stdout)
        assert output["deterministic_checks"]["C12_product_trace_fields_structure"] == "n/a", (
            f"narrative-only mention must yield n/a, got "
            f"{output['deterministic_checks']['C12_product_trace_fields_structure']}"
        )
