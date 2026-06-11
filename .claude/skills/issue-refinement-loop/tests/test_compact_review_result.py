"""
test_compact_review_result.py - Tests for compact_review_result.py (AC3).

Verifies:
- raw review fixture → compact stdout and full artifact JSON generation
- verdict missing → exit 2
- ISSUE_REVIEW_RESULT_COMPACT_V1 schema constants are defined
"""

from __future__ import annotations

import json
import os
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
    """GIVEN needs-fix fixture WHEN stdout generated THEN all required compact fields present."""
    fixture = FIXTURES_DIR / "review_result_needs_fix.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))

    _, stdout_lines = compact_review_result(
        raw_result, artifact_dir=tmp_path / ".claude/artifacts/issue-refinement-loop", issue_number=42
    )
    lines_text = "\n".join(stdout_lines)

    for field in ["STATUS:", "VERDICT:", "SUMMARY:", "BLOCKERS:", "NEXT_ACTION:", "ARTIFACT:"]:
        assert field in lines_text, f"Missing field in stdout: {field}"


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
# Artifact path security
# ---------------------------------------------------------------------------


def test_compact_review_result_rejects_absolute_artifact_dir(tmp_path):
    """GIVEN absolute artifact_dir WHEN compact_review_result called THEN ValueError raised."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))

    # We test the validator directly
    from compact_review_result import _validate_artifact_path
    with pytest.raises(ValueError, match="Absolute"):
        _validate_artifact_path("/absolute/path/to/artifacts")


def test_compact_review_result_rejects_path_traversal():
    """GIVEN path with .. WHEN _validate_artifact_path called THEN ValueError raised."""
    from compact_review_result import _validate_artifact_path
    with pytest.raises(ValueError, match="traversal"):
        _validate_artifact_path("../../etc/passwd")
