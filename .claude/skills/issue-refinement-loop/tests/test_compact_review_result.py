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

    compact_data, stdout_lines = compact_review_result(
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

    compact_data, _ = compact_review_result(raw_result, artifact_dir=artifact_dir, issue_number=42)

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

    compact_data, _ = compact_review_result(raw_result, artifact_dir=artifact_dir, issue_number=42)
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

    compact_data, stdout_lines = compact_review_result(
        raw_result, artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop", issue_number=42
    )

    assert compact_data["VERDICT"] == "needs-fix"
    assert compact_data["NEXT_ACTION"] == "request_changes"
    assert compact_data["BLOCKERS"] == "2"


def test_compact_review_result_needs_fix_stdout_contains_all_fields(tmp_path):
    """GIVEN needs-fix fixture WHEN stdout generated THEN all required compact fields present including MUST_READ."""
    fixture = FIXTURES_DIR / "review_result_needs_fix.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))

    _, stdout_lines = compact_review_result(
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

    compact_data, _ = compact_review_result(raw_result, artifact_dir=artifact_dir, issue_number=42)
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

    compact_data, _ = compact_review_result(raw_result, artifact_dir=artifact_dir, issue_number=42)
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

    _, stdout_lines = compact_review_result(
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

    _, stdout_lines = compact_review_result(
        raw_result, artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop", issue_number=42
    )
    lines_text = "\n".join(stdout_lines)

    assert "diff --git" not in lines_text
    assert "@@ -" not in lines_text


def test_compact_review_result_stdout_byte_limit(tmp_path):
    """GIVEN approve fixture WHEN stdout generated THEN UTF-8 bytes <= 2048."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))

    _, stdout_lines = compact_review_result(
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

    compact_data, _ = compact_review_result(
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
