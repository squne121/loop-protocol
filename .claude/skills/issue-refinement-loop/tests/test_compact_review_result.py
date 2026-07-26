"""
test_compact_review_result.py - Tests for compact_review_result.py (AC3).

Verifies:
- raw review fixture → compact stdout and full artifact JSON generation
- verdict missing → exit 2
- ISSUE_REVIEW_RESULT_COMPACT_V1 schema constants are defined
- MUST_READ is always output even when empty (B7)
- unknown/invalid status → ValueError fail-close (B8)
- artifact containment is enforced via repo_root (B4)
- artifact content is checked for secrets before writing (B5)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

# Add the scripts directory to path
SKILLS_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILLS_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from compact_review_result import (
    COMPACT_SCHEMA_NAME,
    COMPACT_SCHEMA_VERSION,
    REQUIRED_COMPACT_FIELDS,
    _atomic_write,
    compact_review_result,
)
from reviewer_claim_replay import analyze

FIXTURES_DIR = SKILLS_ROOT / "fixtures"


# ---------------------------------------------------------------------------
# Schema constants tests
# ---------------------------------------------------------------------------


def test_schema_name_is_defined():
    """GIVEN compact_review_result module WHEN importing THEN COMPACT_SCHEMA_NAME is defined."""
    assert COMPACT_SCHEMA_NAME == "ISSUE_REVIEW_RESULT_COMPACT_V1"


def test_schema_version_is_defined():
    """GIVEN compact_review_result module WHEN importing THEN COMPACT_SCHEMA_VERSION is defined."""
    assert COMPACT_SCHEMA_VERSION == "1"


def test_required_compact_fields_contains_routing_fields():
    """GIVEN REQUIRED_COMPACT_FIELDS WHEN checked THEN contains routing-critical fields."""
    for field in ["STATUS", "VERDICT", "NEXT_ACTION", "ARTIFACT"]:
        assert field in REQUIRED_COMPACT_FIELDS, f"Missing required field: {field}"


# ---------------------------------------------------------------------------
# Happy path: approve fixture
# ---------------------------------------------------------------------------


def test_compact_review_result_approve(tmp_path):
    """GIVEN approve fixture WHEN compact_review_result called THEN stdout has VERDICT approve."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))

    compact_data, stdout_lines, *_ = compact_review_result(
        raw_result, artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop", issue_number=42
    )

    assert compact_data["STATUS"] == "ok"
    assert compact_data["VERDICT"] == "approve"
    assert compact_data["NEXT_ACTION"] == "proceed"
    assert compact_data["BLOCKERS"] == "0"

    # Check stdout lines
    lines_text = "\n".join(stdout_lines)
    assert "STATUS: ok" in lines_text
    assert "VERDICT: approve" in lines_text
    assert "NEXT_ACTION: proceed" in lines_text
    assert "ARTIFACT:" in lines_text


def test_compact_review_result_approve_artifact_written(tmp_path):
    """GIVEN approve fixture WHEN compact_review_result called THEN artifact JSON is written."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    compact_data, _stdout, artifact_path_val, artifact_content = compact_review_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42
    )
    _atomic_write(artifact_path_val, artifact_content)

    artifact_ref = compact_data["ARTIFACT"]
    assert artifact_ref.startswith("compact_review_result_v1=")
    artifact_path = Path(artifact_ref.split("=", 1)[1])
    assert artifact_path.exists()

    artifact_json = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact_json["schema"] == "ISSUE_REVIEW_RESULT_COMPACT_V1"
    assert artifact_json["verdict"] == "approve"
    assert artifact_json["producer_schema"] == "REVIEW_ISSUE_RESULT_V1"
    assert artifact_json["producer_body_sha256"].startswith("sha256:")
    assert artifact_json["findings"] == []


def test_compact_review_result_artifact_permissions(tmp_path):
    """GIVEN approve fixture WHEN artifact written THEN file has 0600 permissions."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    compact_data, _stdout, artifact_path_val, artifact_content = compact_review_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42
    )
    _atomic_write(artifact_path_val, artifact_content)
    artifact_path = Path(compact_data["ARTIFACT"].split("=", 1)[1])

    stat = artifact_path.stat()
    # Check that mode is 0600 (owner r/w only)
    assert oct(stat.st_mode & 0o777) == oct(0o600)


# ---------------------------------------------------------------------------
# Happy path: needs-fix fixture
# ---------------------------------------------------------------------------


def test_compact_review_result_needs_fix(tmp_path):
    """GIVEN needs-fix fixture WHEN compact_review_result called THEN VERDICT needs-fix."""
    fixture = FIXTURES_DIR / "review_result_needs_fix.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))

    compact_data, stdout_lines, *_ = compact_review_result(
        raw_result, artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop", issue_number=42
    )

    assert compact_data["VERDICT"] == "needs-fix"
    assert compact_data["NEXT_ACTION"] == "request_changes"
    assert compact_data["BLOCKERS"] == "2"


def test_compact_review_result_needs_fix_stdout_contains_all_fields(tmp_path):
    """GIVEN needs-fix fixture WHEN stdout generated THEN all required compact fields present including MUST_READ."""
    fixture = FIXTURES_DIR / "review_result_needs_fix.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))

    _compact, stdout_lines, *_ = compact_review_result(
        raw_result, artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop", issue_number=42
    )
    lines_text = "\n".join(stdout_lines)

    # B7: MUST_READ must always be present (even when empty)
    for field in ["STATUS:", "VERDICT:", "SUMMARY:", "BLOCKERS:", "NEXT_ACTION:", "MUST_READ:", "ARTIFACT:"]:
        assert field in lines_text, f"Missing field in stdout: {field}"


def test_compact_review_result_preserves_findings_losslessly(tmp_path):
    """GIVEN full review artifact WHEN compacted THEN findings/provenance remain in artifact JSON."""
    fixture = FIXTURES_DIR / "review_result_needs_fix.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    compact_data, _stdout, artifact_path_val, artifact_content = compact_review_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42
    )
    _atomic_write(artifact_path_val, artifact_content)
    artifact_path = Path(compact_data["ARTIFACT"].split("=", 1)[1])
    artifact_json = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert artifact_json["producer_schema_version"] == "review_issue_result/v1"
    assert artifact_json["producer_body_sha256"] == raw_result["body_sha256"]
    assert artifact_json["findings"] == raw_result["findings"]


def test_compact_review_result_preserves_structured_blockers_for_replay(tmp_path):
    """GIVEN blocking structured_blockers WHEN compacted THEN replay can still reconstruct deterministic fail."""
    raw_result = {
        "schema": "REVIEW_ISSUE_RESULT_V1",
        "schema_version": "review_issue_result/v1",
        "verdict": "needs-fix",
        "status": "ok",
        "body_sha256": "sha256:" + "4" * 64,
        "issue_kind": "implementation",
        "generated_at": "2026-06-21T00:00:00Z",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/42",
        "deterministic_checks": {"C4_vc_commands_present": "fail"},
        "blocking_issues": [{"code": "C4", "message": "missing $ prefix"}],
        "structured_blockers": [
            {
                "code": "C4",
                "message": "missing $ prefix",
                "finding_kind": "deterministic_domain_blocker",
                "deterministic_domain_key": "vc_command_format",
                "blocking": True,
                "checker_evidence": [
                    {
                        "source_check": "check_issue_contract",
                        "rule_id": "C4_vc_commands_present",
                        "category": "vc_command_format",
                        "artifact_path": ".claude/skills/review-issue/scripts/check_issue_contract.py",
                        "artifact_schema": "REVIEW_ISSUE_RESULT_V1",
                        "body_sha256": "sha256:" + "4" * 64,
                        "iteration_id": "iter-1",
                        "line_start": None,
                        "line_end": None,
                    }
                ],
            }
        ],
        "non_blocking_improvements": [],
        "findings": [
            {
                "finding_kind": "deterministic_domain_blocker",
                "deterministic_domain_key": "vc_command_format",
                "blocking": True,
                "checker_evidence": [
                    {
                        "source_check": "check_issue_contract",
                        "rule_id": "C4_vc_commands_present",
                        "category": "vc_command_format",
                        "artifact_path": ".claude/skills/review-issue/scripts/check_issue_contract.py",
                        "artifact_schema": "REVIEW_ISSUE_RESULT_V1",
                        "body_sha256": "sha256:" + "4" * 64,
                        "iteration_id": "iter-1",
                        "line_start": None,
                        "line_end": None,
                    }
                ],
                "message": "vc_command_format",
            }
        ],
        "diff_proposal": {},
        "parsed_vc_commands": [],
    }
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    compact_data, _stdout, artifact_path_val, artifact_content = compact_review_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42
    )
    _atomic_write(artifact_path_val, artifact_content)
    artifact_path = Path(compact_data["ARTIFACT"].split("=", 1)[1])
    artifact_json = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert artifact_json["structured_blockers"][0]["code"] == "C4"
    replay_result, _ = analyze(
        review_result=artifact_json,
        readiness_result={
            "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
            "body_sha256": "sha256:" + "4" * 64,
            "errors": []
        },
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert replay_result["verdict"] == "deterministic_fail_confirmed"
    assert replay_result["routing"] == "proceed_to_rewrite"


def test_compact_review_result_must_read_always_present_when_empty(tmp_path):
    """GIVEN approve fixture (no must_read) WHEN stdout generated THEN MUST_READ: line is present (B7)."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))

    _compact, stdout_lines, *_ = compact_review_result(
        raw_result, artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop", issue_number=42
    )
    lines_text = "\n".join(stdout_lines)
    assert "MUST_READ:" in lines_text, "MUST_READ: line must always be present even when empty"


# ---------------------------------------------------------------------------
# Error path: verdict missing → exit 2
# ---------------------------------------------------------------------------


def test_compact_review_result_missing_verdict_raises(tmp_path):
    """GIVEN fixture without verdict WHEN compact_review_result called THEN ValueError raised."""
    fixture = FIXTURES_DIR / "review_result_missing_verdict.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="verdict field missing"):
        compact_review_result(
            raw_result,
            artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop",
            issue_number=42,
        )


def test_compact_review_result_cli_missing_verdict_exits_2(tmp_path):
    """GIVEN CLI with missing-verdict fixture WHEN run THEN exit code is 2."""
    import subprocess

    fixture = FIXTURES_DIR / "review_result_missing_verdict.json"
    script = SCRIPTS_DIR / "compact_review_result.py"

    result = subprocess.run(
        [sys.executable, str(script), "--input-file", str(fixture),
         "--artifact-dir", str(tmp_path), "--issue-number", "42"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "STATUS: failed" in result.stdout or "ERROR:" in result.stderr


# ---------------------------------------------------------------------------
# Stdout compliance: no raw content
# ---------------------------------------------------------------------------


def test_compact_review_result_stdout_no_raw_diff(tmp_path):
    """GIVEN approve fixture WHEN stdout generated THEN no raw diff markers in stdout."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))

    _compact, stdout_lines, *_ = compact_review_result(
        raw_result, artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop", issue_number=42
    )
    lines_text = "\n".join(stdout_lines)

    assert "diff --git" not in lines_text
    assert "@@ -" not in lines_text


def test_compact_review_result_stdout_byte_limit(tmp_path):
    """GIVEN approve fixture WHEN stdout generated THEN UTF-8 bytes <= 2048."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))

    _compact, stdout_lines, *_ = compact_review_result(
        raw_result, artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop", issue_number=42
    )
    lines_text = "\n".join(stdout_lines)

    byte_count = len(lines_text.encode("utf-8"))
    assert byte_count <= 2048, f"stdout too large: {byte_count} bytes"


# ---------------------------------------------------------------------------
# B8: unknown/invalid status → ValueError fail-close
# ---------------------------------------------------------------------------


def test_compact_review_result_unknown_status_raises_valueerror(tmp_path):
    """GIVEN review result with unknown status WHEN compact_review_result THEN ValueError (B8)."""
    raw_result = {"verdict": "approve", "status": "mystery_status"}
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"
    with pytest.raises(ValueError, match="Unknown/invalid status"):
        compact_review_result(raw_result, artifact_dir=artifact_dir, issue_number=42)


def test_compact_review_result_unknown_status_cli_exits_2(tmp_path):
    """GIVEN CLI with unknown status WHEN run THEN exit code is 2 (B8)."""
    import subprocess

    bad_fixture = tmp_path / "bad_status.json"
    bad_fixture.write_text('{"verdict": "approve", "status": "mystery"}', encoding="utf-8")
    script = SCRIPTS_DIR / "compact_review_result.py"
    result = subprocess.run(
        [sys.executable, str(script), "--input-file", str(bad_fixture),
         "--artifact-dir", str(tmp_path), "--issue-number", "42"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# B4: artifact containment
# ---------------------------------------------------------------------------


def test_compact_review_result_containment_check_passes(tmp_path):
    """GIVEN valid repo_root WHEN compact_review_result THEN artifact is under base (B4)."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    repo_root = tmp_path
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    compact_data, *_ = compact_review_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42, repo_root=repo_root
    )
    assert "compact_review_result_v1=" in compact_data["ARTIFACT"]


def test_compact_review_result_containment_check_rejects_escape(tmp_path):
    """GIVEN artifact_dir outside repo_root WHEN compact_review_result THEN ValueError (B4)."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as other_root:
        repo_root = Path(other_root) / "repo"
        repo_root.mkdir()
        artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"
        with pytest.raises(ValueError, match="escapes base directory"):
            compact_review_result(
                raw_result, artifact_dir=artifact_dir, issue_number=42, repo_root=repo_root
            )


# ---------------------------------------------------------------------------
# B5: artifact content secret check
# ---------------------------------------------------------------------------


def test_compact_review_result_artifact_secret_check_fails(tmp_path):
    """GIVEN review result with secret-like content WHEN compact_review_result THEN ValueError (B5)."""
    raw_result = {
        "schema": "REVIEW_ISSUE_RESULT_V1",
        "schema_version": "review_issue_result/v1",
        "verdict": "approve",
        "status": "ok",
        "body_sha256": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
        "issue_kind": "implementation",
        "generated_at": "2026-06-11T00:00:00Z",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/42",
        "blocking_issues": [],
        "structured_blockers": [],
        "non_blocking_improvements": [],
        "findings": [],
        "diff_proposal": {"note": "token: ghp_" + "A" * 36},
        "deterministic_checks": {},
        "parsed_vc_commands": [],
    }
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"
    with pytest.raises(ValueError, match="secret-like strings detected in artifact content"):
        compact_review_result(raw_result, artifact_dir=artifact_dir, issue_number=42)


def test_compact_review_result_rejects_nan_on_write(tmp_path):
    """GIVEN review result with NaN WHEN artifact rendered THEN ValueError (strict JSON)."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    raw_result["diff_proposal"] = {"nan": float("nan")}

    with pytest.raises(ValueError):
        compact_review_result(
            raw_result,
            artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop",
            issue_number=42,
        )


def test_compact_review_result_cli_rejects_nan_input(tmp_path):
    """GIVEN CLI input containing NaN WHEN run THEN exit 2 (strict JSON parse)."""
    import subprocess

    bad_fixture = tmp_path / "bad_nan.json"
    bad_fixture.write_text(
        """{"schema":"REVIEW_ISSUE_RESULT_V1","schema_version":"review_issue_result/v1","verdict":"approve","status":"ok","body_sha256":"sha256:1111111111111111111111111111111111111111111111111111111111111111","issue_kind":"implementation","generated_at":"2026-06-21T00:00:00Z","deterministic_checks":{},"blocking_issues":[],"structured_blockers":[],"non_blocking_improvements":[],"findings":[],"diff_proposal":{"value":NaN},"parsed_vc_commands":[]}""",
        encoding="utf-8",
    )
    script = SCRIPTS_DIR / "compact_review_result.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--input-file",
            str(bad_fixture),
            "--artifact-dir",
            str(tmp_path),
            "--issue-number",
            "42"
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# Artifact path security
# ---------------------------------------------------------------------------


def test_compact_review_result_rejects_absolute_artifact_dir(tmp_path):
    """GIVEN absolute artifact_dir WHEN compact_review_result called THEN ValueError raised."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    _raw_result = json.loads(fixture.read_text(encoding="utf-8"))

    # We test the validator directly
    from compact_review_result import _validate_artifact_path
    with pytest.raises(ValueError, match="Absolute"):
        _validate_artifact_path("/absolute/path/to/artifacts")


def test_compact_review_result_rejects_path_traversal():
    """GIVEN path with .. WHEN _validate_artifact_path called THEN ValueError raised."""
    from compact_review_result import _validate_artifact_path
    with pytest.raises(ValueError, match="traversal"):
        _validate_artifact_path("../../etc/passwd")


# ---------------------------------------------------------------------------
# Issue #1791 review remediation (PR #1801 REQUEST_CHANGES fix_delta):
#
# - Critical #1: check_issue_contract.py --mode merge_readiness is now the
#   single deterministic producer that merges ISSUE_CONTRACT_READINESS_RESULT_V1
#   into REVIEW_ISSUE_RESULT_V1 (no LLM-driven merge step).
# - Critical #2: line_start/line_end == 0 (the real producer's value for
#   validator-timeout / internal-error / JSON-decode-error / VC-extraction
#   failure) must normalize to null, not pass through unchanged.
# - Critical #3: readiness_status == "human_judgment" errors must NOT become
#   deterministic_domain_blocker / blocking:true structured_blockers; the
#   distinction is carried via the top-level REVIEW_ISSUE_RESULT_V1.failure_class
#   field that compact_review_result.py already reads.
# - High #5: provenance (body_sha256 match, non-fabricated artifact_path,
#   source_payload passthrough) is validated via the existing
#   _is_valid_deterministic_evidence() gate; invalid evidence is dropped
#   fail-closed rather than emitted as a blocker.
# ---------------------------------------------------------------------------

REVIEW_ISSUE_SCRIPTS_DIR = (
    SKILLS_ROOT.parent / "review-issue" / "scripts"
)
if str(REVIEW_ISSUE_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(REVIEW_ISSUE_SCRIPTS_DIR))

from check_issue_contract import (  # noqa: E402
    _normalize_readiness_line,
    merge_readiness_into_review_result,
    readiness_error_to_structured_blocker,
    readiness_errors_to_structured_blockers,
    readiness_status_to_failure_class,
)

_SAMPLE_BODY_SHA256 = (
    "sha256:3333333333333333333333333333333333333333333333333333333333333333"
)

_SAMPLE_READINESS_ERRORS = [
    {
        "rule_id": "LP021",
        "severity": "error",
        "source_check": "validate_issue_body",
        "category": "body_lint",
        "section": "Allowed Paths",
        "line_start": 12,
        "line_end": 12,
        "minimal_context": ["## Allowed Paths"],
        "fix_hint": "Allowed Paths を明記してください",
        "autofixable": False,
    },
    {
        "rule_id": "BASELINE_VC_UNEXPECTED_PASS",
        "severity": "error",
        "source_check": "baseline_vc_preflight",
        "category": "vc_preflight",
        "section": "Verification Commands",
        "line_start": 40,
        "line_end": 42,
        "minimal_context": [],
        "fix_hint": "baseline で unexpected pass が検出されました",
        "autofixable": False,
        "source_payload": {
            "classification": "unexpected_pass",
            "category": "vc_preflight",
            "decision": "immediate",
            "exit_code": 0,
            "command_hash": "sha256:deadbeef",
        },
    },
]

# Real-producer-shaped errors: contract_readiness_check.py emits line_start=0 /
# line_end=0 (not omitted, not null) for JSON-decode-error and
# map_validate_errors_to_readiness_errors() default-value branches.
_REAL_PRODUCER_ZERO_LINE_ERRORS = [
    {
        "rule_id": "READINESS_JSON_DECODE_ERROR",
        "severity": "error",
        "source_check": "validate_issue_body",
        "category": "internal_error",
        "section": "(global)",
        "line_start": 0,
        "line_end": 0,
        "minimal_context": [],
        "fix_hint": "validator 実行環境を確認してください",
        "autofixable": False,
    },
    {
        "rule_id": "LP000",
        "severity": "error",
        "source_check": "validate_issue_body",
        "category": "body_lint",
        "section": "",
        "line_start": 0,
        "line_end": 0,
        "minimal_context": [],
        "fix_hint": "",
        "autofixable": False,
    },
]


# ---------------------------------------------------------------------------
# Critical #2: line_start/line_end normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, None),
        (-1, None),
        (True, None),  # bool is an int subclass; must not leak through as 1
        (False, None),
        (None, None),
        ("12", None),
        (1, 1),
        (12, 12),
    ],
)
def test_normalize_readiness_line(value, expected):
    """GIVEN various raw line values WHEN normalized THEN only ints >= 1
    survive; 0 (the real producer's failure-mode value), negative, bool, and
    non-int values normalize to None (AC2 regression, Critical #2)."""
    assert _normalize_readiness_line(value) == expected


def test_readiness_error_to_structured_blocker_normalizes_real_producer_zero_line():
    """GIVEN a real-producer-shaped readiness error with line_start=0 /
    line_end=0 WHEN converted THEN checker_evidence.line_start/line_end are
    null (schema requires null or >=1), not the raw 0 (Critical #2)."""
    blocker = readiness_error_to_structured_blocker(
        _REAL_PRODUCER_ZERO_LINE_ERRORS[0],
        body_sha256=_SAMPLE_BODY_SHA256,
        iteration_id="iter-1",
        artifact_path="artifacts/issue-refinement-loop/1791/readiness_result.json",
    )
    assert blocker is not None
    evidence = blocker["checker_evidence"][0]
    assert evidence["line_start"] is None
    assert evidence["line_end"] is None


def test_readiness_errors_to_structured_blockers_real_producer_zero_line_via_compact(
    tmp_path,
):
    """GIVEN real-producer-shaped errors (line_start/line_end=0, VALIDATOR
    JSON-decode-error shape) WHEN merged into a REVIEW_ISSUE_RESULT_V1 and
    passed through compact_review_result() THEN no schema_mismatch/ValueError
    is raised (AC2 regression against actual producer payloads, Critical #2)."""
    fixture = FIXTURES_DIR / "review_result_needs_fix.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    raw_result["structured_blockers"] = readiness_errors_to_structured_blockers(
        _REAL_PRODUCER_ZERO_LINE_ERRORS,
        body_sha256=raw_result["body_sha256"],
        iteration_id="iter-1",
        artifact_path="artifacts/issue-refinement-loop/1791/readiness_result.json",
    )

    compact_data, stdout_lines, *_ = compact_review_result(
        raw_result,
        artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop",
        issue_number=1791,
    )

    assert compact_data["STATUS"] == "ok"
    assert compact_data["VERDICT"] == "needs-fix"
    assert compact_data["NEXT_ACTION"] == "request_changes"
    lines_text = "\n".join(stdout_lines)
    assert "STATUS: ok" in lines_text


# ---------------------------------------------------------------------------
# AC1 / AC3: converted shape + checker_evidence completeness (incl. source_payload)
# ---------------------------------------------------------------------------


def test_readiness_error_to_structured_blocker_matches_schema_required_fields():
    """GIVEN a raw readiness errors[] element WHEN converted THEN the
    structured_blocker has code/message/finding_kind/deterministic_domain_key/
    blocking/checker_evidence (AC1)."""
    blocker = readiness_error_to_structured_blocker(
        _SAMPLE_READINESS_ERRORS[0],
        body_sha256=_SAMPLE_BODY_SHA256,
        iteration_id="iter-1",
        artifact_path="artifacts/issue-refinement-loop/1791/readiness_result.json",
    )

    assert blocker is not None
    for required_field in (
        "code",
        "message",
        "finding_kind",
        "deterministic_domain_key",
        "blocking",
        "checker_evidence",
    ):
        assert required_field in blocker, f"missing {required_field}"

    assert blocker["finding_kind"] == "deterministic_domain_blocker"
    assert blocker["blocking"] is True
    assert blocker["checker_evidence"], "checker_evidence must be non-empty"


def test_readiness_error_to_structured_blocker_checker_evidence_fields_complete():
    """GIVEN a converted structured_blocker WHEN inspecting checker_evidence
    THEN all required evidence fields are present and non-empty, and
    source_payload is preserved rather than dropped (AC3, High #5)."""
    blocker = readiness_error_to_structured_blocker(
        _SAMPLE_READINESS_ERRORS[1],
        body_sha256=_SAMPLE_BODY_SHA256,
        iteration_id="iter-1",
        artifact_path="artifacts/issue-refinement-loop/1791/readiness_result.json",
    )
    assert blocker is not None
    evidence = blocker["checker_evidence"][0]

    for required_field in (
        "source_check",
        "rule_id",
        "category",
        "artifact_path",
        "artifact_schema",
        "body_sha256",
        "iteration_id",
        "line_start",
        "line_end",
    ):
        assert required_field in evidence, f"missing checker_evidence.{required_field}"

    assert evidence["source_check"] == "baseline_vc_preflight"
    assert evidence["rule_id"] == "BASELINE_VC_UNEXPECTED_PASS"
    assert evidence["category"] == "vc_preflight"
    assert evidence["artifact_schema"] == "ISSUE_CONTRACT_READINESS_RESULT_V1"
    assert evidence["artifact_path"] == (
        "artifacts/issue-refinement-loop/1791/readiness_result.json"
    )
    assert evidence["body_sha256"] == _SAMPLE_BODY_SHA256
    assert evidence["iteration_id"] == "iter-1"
    assert evidence["source_payload"] == _SAMPLE_READINESS_ERRORS[1]["source_payload"]


def test_readiness_errors_to_structured_blockers_no_schema_mismatch_via_compact(
    tmp_path,
):
    """GIVEN a REVIEW_ISSUE_RESULT_V1 whose structured_blockers were built via
    readiness_errors_to_structured_blockers() WHEN passed through
    compact_review_result() THEN no schema_mismatch/ValueError is raised (AC2)."""
    fixture = FIXTURES_DIR / "review_result_needs_fix.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    raw_result["structured_blockers"] = readiness_errors_to_structured_blockers(
        _SAMPLE_READINESS_ERRORS,
        body_sha256=raw_result["body_sha256"],
        iteration_id="iter-1",
        artifact_path="artifacts/issue-refinement-loop/1791/readiness_result.json",
    )

    compact_data, stdout_lines, *_ = compact_review_result(
        raw_result,
        artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop",
        issue_number=1791,
    )

    assert compact_data["STATUS"] == "ok"
    assert compact_data["VERDICT"] == "needs-fix"
    lines_text = "\n".join(stdout_lines)
    assert "STATUS: ok" in lines_text


# ---------------------------------------------------------------------------
# Critical #3: human_judgment must not become a deterministic blocker
# ---------------------------------------------------------------------------


def test_readiness_errors_to_structured_blockers_human_judgment_returns_empty():
    """GIVEN readiness_status="human_judgment" WHEN converted THEN NO
    structured_blockers are produced (human_judgment is not a body-editable
    deterministic blocker; see readiness_status_to_failure_class() /
    merge_readiness_into_review_result() for the top-level routing, Critical
    #3)."""
    blockers = readiness_errors_to_structured_blockers(
        _SAMPLE_READINESS_ERRORS,
        body_sha256=_SAMPLE_BODY_SHA256,
        iteration_id="iter-1",
        readiness_status="human_judgment",
    )
    assert blockers == []


def test_readiness_status_to_failure_class():
    """GIVEN readiness statuses WHEN mapped THEN only human_judgment yields a
    failure_class value (Critical #3)."""
    assert (
        readiness_status_to_failure_class("human_judgment")
        == "contract_readiness_human_judgment"
    )
    assert readiness_status_to_failure_class("needs_fix") is None
    assert readiness_status_to_failure_class("go") is None
    assert readiness_status_to_failure_class(None) is None


# ---------------------------------------------------------------------------
# merge_readiness_into_review_result(): fail-closed body_sha256 + full pipeline
# ---------------------------------------------------------------------------


def _needs_fix_review_result() -> dict:
    fixture = FIXTURES_DIR / "review_result_needs_fix.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def test_merge_readiness_into_review_result_needs_fix_adds_blockers():
    """GIVEN a needs_fix readiness result WHEN merged THEN structured_blockers
    are appended and verdict stays needs-fix (Critical #1)."""
    review_result = _needs_fix_review_result()
    readiness_result = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": "needs_fix",
        "body_sha256": review_result["body_sha256"],
        "errors": _SAMPLE_READINESS_ERRORS,
    }

    merged = merge_readiness_into_review_result(
        review_result,
        readiness_result,
        readiness_artifact_path="artifacts/issue-refinement-loop/1791/readiness_result.json",
        iteration_id="iter-1",
    )

    assert merged["verdict"] == "needs-fix"
    assert merged.get("failure_class") is None
    assert len(merged["structured_blockers"]) == len(_SAMPLE_READINESS_ERRORS)
    for blocker in merged["structured_blockers"]:
        assert blocker["finding_kind"] == "deterministic_domain_blocker"
        assert blocker["blocking"] is True


def test_merge_readiness_into_review_result_human_judgment_sets_top_level_failure_class():
    """GIVEN a human_judgment readiness result WHEN merged THEN no
    structured_blockers are added, but the top-level failure_class is set so
    compact_review_result.py routes to human_judgment_required (Critical #3)."""
    review_result = _needs_fix_review_result()
    review_result["verdict"] = "approve"
    review_result["structured_blockers"] = []
    readiness_result = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": "human_judgment",
        "body_sha256": review_result["body_sha256"],
        "errors": _SAMPLE_READINESS_ERRORS,
    }

    merged = merge_readiness_into_review_result(
        review_result,
        readiness_result,
        readiness_artifact_path="artifacts/issue-refinement-loop/1791/readiness_result.json",
        iteration_id="iter-1",
    )

    assert merged["structured_blockers"] == []
    assert merged["failure_class"] == "contract_readiness_human_judgment"
    # NEXT_ACTION routing through compact_review_result() is exercised in
    # test_merge_readiness_into_review_result_human_judgment_next_action below.


def test_merge_readiness_into_review_result_human_judgment_next_action(tmp_path):
    """GIVEN the human_judgment merge output WHEN passed through
    compact_review_result() THEN NEXT_ACTION is human_judgment_required, not
    request_changes (Critical #3, full pipeline)."""
    review_result = _needs_fix_review_result()
    review_result["verdict"] = "needs-fix"
    review_result["structured_blockers"] = []
    readiness_result = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": "human_judgment",
        "body_sha256": review_result["body_sha256"],
        "errors": _SAMPLE_READINESS_ERRORS,
    }

    merged = merge_readiness_into_review_result(
        review_result,
        readiness_result,
        readiness_artifact_path="artifacts/issue-refinement-loop/1791/readiness_result.json",
        iteration_id="iter-1",
    )

    compact_data, *_ = compact_review_result(
        merged,
        artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop",
        issue_number=1791,
    )
    assert compact_data["NEXT_ACTION"] == "human_judgment_required"


def test_merge_readiness_into_review_result_body_sha256_mismatch_fail_closed():
    """GIVEN mismatched body_sha256 between REVIEW_ISSUE_RESULT_V1 and
    ISSUE_CONTRACT_READINESS_RESULT_V1 WHEN merged THEN ValueError is raised
    and no deterministic blockers are fabricated from stale evidence (High
    #5)."""
    review_result = _needs_fix_review_result()
    readiness_result = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": "needs_fix",
        "body_sha256": "sha256:" + "9" * 64,
        "errors": _SAMPLE_READINESS_ERRORS,
    }

    with pytest.raises(ValueError, match="body_sha256 mismatch"):
        merge_readiness_into_review_result(
            review_result,
            readiness_result,
            readiness_artifact_path="artifacts/issue-refinement-loop/1791/readiness_result.json",
            iteration_id="iter-1",
        )


def test_merge_readiness_into_review_result_no_errors_is_noop():
    """GIVEN an empty readiness errors[] WHEN merged THEN the review result is
    returned unchanged (no spurious mismatch failure on go/approve paths)."""
    review_result = _needs_fix_review_result()
    review_result["body_sha256"] = "sha256:" + "0" * 64  # deliberately mismatched
    readiness_result = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": "go",
        "body_sha256": "sha256:" + "1" * 64,
        "errors": [],
    }

    merged = merge_readiness_into_review_result(
        review_result,
        readiness_result,
        readiness_artifact_path="artifacts/issue-refinement-loop/1791/readiness_result.json",
        iteration_id="iter-1",
    )
    assert merged["structured_blockers"] == review_result["structured_blockers"]
    assert merged.get("failure_class") == review_result.get("failure_class")


# ---------------------------------------------------------------------------
# CLI wiring (Critical #1): real subprocess invocation of
# check_issue_contract.py --mode merge_readiness, not just an in-process
# function call, so production wiring regressions are caught (review point 7).
# ---------------------------------------------------------------------------


def test_check_issue_contract_cli_merge_readiness_mode(tmp_path):
    """GIVEN review-result and readiness-result JSON files on disk WHEN
    `check_issue_contract.py --mode merge_readiness` is invoked as a real
    subprocess THEN it writes a schema-valid merged REVIEW_ISSUE_RESULT_V1
    with the expected structured_blockers (Critical #1 production wiring)."""
    import subprocess

    review_result = _needs_fix_review_result()
    review_result_file = tmp_path / "review_result.json"
    review_result_file.write_text(json.dumps(review_result), encoding="utf-8")

    readiness_result_file = tmp_path / "readiness_result.json"
    readiness_result_file.write_text(
        json.dumps(
            {
                "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
                "status": "needs_fix",
                "body_sha256": review_result["body_sha256"],
                "errors": _SAMPLE_READINESS_ERRORS,
            }
        ),
        encoding="utf-8",
    )

    output_file = tmp_path / "merged_review_result.json"
    script = REVIEW_ISSUE_SCRIPTS_DIR / "check_issue_contract.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--mode",
            "merge_readiness",
            "--review-result-file",
            str(review_result_file),
            "--readiness-result-file",
            str(readiness_result_file),
            "--readiness-artifact-path",
            str(readiness_result_file),
            "--iteration-id",
            "iter-1",
            "--output-file",
            str(output_file),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stderr  # needs-fix → exit 1
    merged = json.loads(output_file.read_text(encoding="utf-8"))
    assert merged["verdict"] == "needs-fix"
    assert len(merged["structured_blockers"]) == len(_SAMPLE_READINESS_ERRORS)

    # Full pipeline: the merged CLI output must itself be schema-valid input
    # to compact_review_result.py (no schema_mismatch — Critical #1/#2).
    compact_data, *_ = compact_review_result(
        merged,
        artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop",
        issue_number=1791,
    )
    assert compact_data["STATUS"] == "ok"
    assert compact_data["VERDICT"] == "needs-fix"


def test_check_issue_contract_cli_merge_readiness_mode_missing_args_exits_2(tmp_path):
    """GIVEN --mode merge_readiness without the required file arguments WHEN
    invoked THEN it exits 2 rather than silently doing nothing."""
    import subprocess

    script = REVIEW_ISSUE_SCRIPTS_DIR / "check_issue_contract.py"
    result = subprocess.run(
        [sys.executable, str(script), "--mode", "merge_readiness"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2

