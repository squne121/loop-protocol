"""
test_reviewer_claim_replay.py - Tests for reviewer_claim_replay.py

AC1: readiness result に C4 対応エラーがある場合 → deterministic_backed: true
AC2: readiness result に C4 対応エラーがない場合 → reviewer_claim_unbacked_by_deterministic_checker, should_consume_iteration: false
AC3: 同一 blocker_code を 2 回連続して replay したとき、2 回目は reviewer_false_positive_suspected を返す
AC4: deterministic_backed: true のとき should_consume_iteration: true
AC5: stdout が compact JSON のみで 2048 bytes 以内
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "reviewer_claim_replay.py"

sys.path.insert(0, str(SCRIPTS_DIR))

from reviewer_claim_replay import analyze, SCHEMA


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

READINESS_WITH_C4_ERROR = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "status": "needs_fix",
    "body_sha256": "sha256:abc123",
    "source_checks": [
        {"name": "validate_issue_body", "schema": "loop_body_lint/v1", "status": "fail", "exit_code": 1}
    ],
    "errors": [
        {
            "rule_id": "VCS001",
            "severity": "error",
            "source_check": "contract_readiness_check",
            "category": "compound_command_disallowed",
            "section": "Verification Commands",
            "line_start": 10,
            "line_end": 10,
            "minimal_context": ["$ rg foo | head -5"],
            "fix_hint": "Remove compound shell operators.",
            "autofixable": False,
        }
    ],
    "minimal_context": [],
    "fix_hint": None,
}

READINESS_WITHOUT_C4_ERROR = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "status": "go",
    "body_sha256": "sha256:def456",
    "source_checks": [
        {"name": "validate_issue_body", "schema": "loop_body_lint/v1", "status": "pass", "exit_code": 0}
    ],
    "errors": [],
    "minimal_context": [],
    "fix_hint": None,
}

READINESS_WITH_SECTION_ERROR = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "status": "needs_fix",
    "body_sha256": "sha256:sec789",
    "source_checks": [
        {"name": "validate_issue_body", "schema": "loop_body_lint/v1", "status": "fail", "exit_code": 1}
    ],
    "errors": [
        {
            "rule_id": "LP005",
            "severity": "error",
            "source_check": "validate_issue_body",
            "category": "missing_required_section",
            "section": "Acceptance Criteria",
            "line_start": 0,
            "line_end": 0,
            "minimal_context": [],
            "fix_hint": "Add required section.",
            "autofixable": False,
        }
    ],
    "minimal_context": [],
    "fix_hint": None,
}


# ---------------------------------------------------------------------------
# AC1: readiness result に C4 対応エラーがある場合 → deterministic_backed: true
# ---------------------------------------------------------------------------


def test_ac1_c4_backed_by_readiness_error():
    """AC1: VCS001/compound_command_disallowed in readiness → deterministic_backed: true."""
    result = analyze(
        blocker_code="C4",
        body_file=None,
        readiness_result=READINESS_WITH_C4_ERROR,
        vc_syntax_result=None,
        vc_preflight_result=None,
    )
    assert result["schema"] == SCHEMA
    assert result["deterministic_backed"] is True
    assert result["verdict"] == "deterministic_fail_confirmed"
    assert len(result["matched_source_checks"]) > 0


def test_ac1_vc_command_format_backed():
    """AC1: blocker_code 'vc_command_format' is vc_syntax class → backed by VCS001 error."""
    result = analyze(
        blocker_code="vc_command_format",
        body_file=None,
        readiness_result=READINESS_WITH_C4_ERROR,
        vc_syntax_result=None,
        vc_preflight_result=None,
    )
    assert result["deterministic_backed"] is True


def test_ac1_lp010_backed_by_vc_syntax_result():
    """AC1: LP010 blocker backed by vc_syntax_result errors."""
    vc_syntax = {
        "errors": [
            {"rule_id": "LP010", "section": "Verification Commands", "message": "missing $ prefix"}
        ]
    }
    result = analyze(
        blocker_code="LP010",
        body_file=None,
        readiness_result=READINESS_WITHOUT_C4_ERROR,
        vc_syntax_result=vc_syntax,
        vc_preflight_result=None,
    )
    assert result["deterministic_backed"] is True
    assert any("LP010" in s for s in result["matched_source_checks"])


# ---------------------------------------------------------------------------
# AC2: readiness result に C4 対応エラーがない場合 → unbacked, should_consume_iteration: false
# ---------------------------------------------------------------------------


def test_ac2_c4_unbacked_when_no_errors():
    """AC2: clean readiness result + C4 blocker → reviewer_claim_unbacked_by_deterministic_checker."""
    result = analyze(
        blocker_code="C4",
        body_file=None,
        readiness_result=READINESS_WITHOUT_C4_ERROR,
        vc_syntax_result=None,
        vc_preflight_result=None,
    )
    assert result["deterministic_backed"] is False
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert result["should_consume_iteration"] is False
    assert result["routing"] == "downgrade_to_non_blocking"


def test_ac2_missing_prefix_unbacked():
    """AC2: 'missing $ prefix' blocker with no backing errors → unbacked."""
    result = analyze(
        blocker_code="missing $ prefix",
        body_file=None,
        readiness_result=READINESS_WITHOUT_C4_ERROR,
        vc_syntax_result=None,
        vc_preflight_result=None,
    )
    assert result["should_consume_iteration"] is False
    assert result["deterministic_backed"] is False


def test_ac2_section_blocker_unbacked_when_no_section_errors():
    """AC2: missing_section blocker but no section errors in readiness → unbacked."""
    result = analyze(
        blocker_code="missing_section",
        body_file=None,
        readiness_result=READINESS_WITHOUT_C4_ERROR,
        vc_syntax_result=None,
        vc_preflight_result=None,
    )
    assert result["deterministic_backed"] is False
    assert result["should_consume_iteration"] is False


# ---------------------------------------------------------------------------
# AC3: consecutive_count >= 2 → reviewer_false_positive_suspected
# ---------------------------------------------------------------------------


def test_ac3_consecutive_count_2_returns_false_positive_suspected():
    """AC3: 2nd consecutive unbacked replay → reviewer_false_positive_suspected."""
    result = analyze(
        blocker_code="C4",
        body_file=None,
        readiness_result=READINESS_WITHOUT_C4_ERROR,
        vc_syntax_result=None,
        vc_preflight_result=None,
        consecutive_count=2,
    )
    assert result["deterministic_backed"] is False
    assert result["verdict"] == "reviewer_false_positive_suspected"
    assert result["should_consume_iteration"] is False


def test_ac3_consecutive_count_1_returns_unbacked_not_suspected():
    """AC3: 1st consecutive replay → reviewer_claim_unbacked (not false_positive_suspected)."""
    result = analyze(
        blocker_code="C4",
        body_file=None,
        readiness_result=READINESS_WITHOUT_C4_ERROR,
        vc_syntax_result=None,
        vc_preflight_result=None,
        consecutive_count=1,
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"


def test_ac3_consecutive_count_3_also_returns_false_positive_suspected():
    """AC3: consecutive_count >= 2 is the threshold."""
    result = analyze(
        blocker_code="missing_required_section",
        body_file=None,
        readiness_result=READINESS_WITHOUT_C4_ERROR,
        vc_syntax_result=None,
        vc_preflight_result=None,
        consecutive_count=3,
    )
    assert result["verdict"] == "reviewer_false_positive_suspected"


# ---------------------------------------------------------------------------
# AC4: deterministic_backed: true → should_consume_iteration: true
# ---------------------------------------------------------------------------


def test_ac4_backed_consumes_iteration():
    """AC4: deterministic_backed: true → should_consume_iteration: true."""
    result = analyze(
        blocker_code="C4",
        body_file=None,
        readiness_result=READINESS_WITH_C4_ERROR,
        vc_syntax_result=None,
        vc_preflight_result=None,
    )
    assert result["deterministic_backed"] is True
    assert result["should_consume_iteration"] is True


def test_ac4_section_backed_consumes_iteration():
    """AC4: missing_section backed by readiness error → should_consume_iteration: true."""
    result = analyze(
        blocker_code="missing_required_section",
        body_file=None,
        readiness_result=READINESS_WITH_SECTION_ERROR,
        vc_syntax_result=None,
        vc_preflight_result=None,
    )
    assert result["deterministic_backed"] is True
    assert result["should_consume_iteration"] is True


# ---------------------------------------------------------------------------
# AC5: stdout is compact JSON only, ≤ 2048 bytes
# ---------------------------------------------------------------------------


def test_ac5_stdout_is_compact_json_within_2048_bytes():
    """AC5: CLI stdout is compact JSON and ≤ 2048 bytes."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(READINESS_WITHOUT_C4_ERROR, f)
        readiness_path = f.name

    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--blocker-code", "C4",
                "--readiness-result-file", readiness_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, f"Script failed: {proc.stderr}"
        stdout = proc.stdout.strip()
        # Must be valid JSON
        parsed = json.loads(stdout)
        assert parsed["schema"] == SCHEMA
        # Must be compact (no extra whitespace beyond separators)
        assert "\n" not in stdout, "stdout must not contain newlines (compact JSON)"
        # Must be ≤ 2048 bytes
        byte_count = len(stdout.encode("utf-8"))
        assert byte_count <= 2048, f"stdout exceeds 2048 bytes: {byte_count}"
    finally:
        import os
        try:
            os.unlink(readiness_path)
        except OSError:
            pass


def test_ac5_stdout_backed_case_also_compact():
    """AC5: backed case CLI stdout is compact JSON ≤ 2048 bytes."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(READINESS_WITH_C4_ERROR, f)
        readiness_path = f.name

    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--blocker-code", "C4",
                "--readiness-result-file", readiness_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, f"Script failed: {proc.stderr}"
        stdout = proc.stdout.strip()
        parsed = json.loads(stdout)
        assert parsed["deterministic_backed"] is True
        byte_count = len(stdout.encode("utf-8"))
        assert byte_count <= 2048, f"stdout exceeds 2048 bytes: {byte_count}"
    finally:
        import os
        try:
            os.unlink(readiness_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


def test_unknown_blocker_code_returns_unbacked():
    """Unknown blocker codes → deterministic_backed: false, unknown_blocker_type."""
    result = analyze(
        blocker_code="PROSE_QUALITY_ISSUE",
        body_file=None,
        readiness_result=READINESS_WITH_C4_ERROR,
        vc_syntax_result=None,
        vc_preflight_result=None,
    )
    assert result["deterministic_backed"] is False
    assert result["blocker_kind"] == "unknown_blocker_type"
    assert result["should_consume_iteration"] is False


def test_vc_preflight_compound_command_backs_c4():
    """vc_preflight result with compound_command_disallowed backs C4 blocker."""
    preflight = {
        "schema": "baseline_vc_preflight/v1",
        "status": "blocked",
        "results": [
            {
                "ac": "AC1",
                "category": "compound_command_disallowed",
                "classification": "blocked",
                "decision": "blocked",
                "raw_command": "rg foo | head -5",
            }
        ],
        "errors": [],
    }
    result = analyze(
        blocker_code="C4",
        body_file=None,
        readiness_result=READINESS_WITHOUT_C4_ERROR,
        vc_syntax_result=None,
        vc_preflight_result=preflight,
    )
    assert result["deterministic_backed"] is True
    assert any("compound_command_disallowed" in s for s in result["matched_source_checks"])


def test_matched_source_checks_deduplicated():
    """matched_source_checks should not contain duplicates."""
    # Both readiness and vc_syntax have VCS001
    vc_syntax = {
        "errors": [
            {"rule_id": "VCS001", "section": "Verification Commands", "message": "compound op"}
        ]
    }
    result = analyze(
        blocker_code="C4",
        body_file=None,
        readiness_result=READINESS_WITH_C4_ERROR,
        vc_syntax_result=vc_syntax,
        vc_preflight_result=None,
    )
    # No duplicates
    assert len(result["matched_source_checks"]) == len(set(result["matched_source_checks"]))
