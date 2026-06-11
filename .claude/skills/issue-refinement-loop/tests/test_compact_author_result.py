"""
test_compact_author_result.py - Tests for compact_author_result.py (AC5).

Verifies:
- subagent final response is compact stdout only (no raw body/diff/log in main context)
- body_hash required for ok status
- ISSUE_AUTHOR_RESULT_COMPACT_V1 schema constants are defined
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILLS_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from compact_author_result import (
    COMPACT_SCHEMA_NAME,
    COMPACT_SCHEMA_VERSION,
    REQUIRED_COMPACT_FIELDS,
    compact_author_result,
)

FIXTURES_DIR = SKILLS_ROOT / "fixtures"


# ---------------------------------------------------------------------------
# Schema constants tests
# ---------------------------------------------------------------------------


def test_author_schema_name_is_defined():
    """GIVEN compact_author_result module WHEN importing THEN COMPACT_SCHEMA_NAME is defined."""
    assert COMPACT_SCHEMA_NAME == "ISSUE_AUTHOR_RESULT_COMPACT_V1"


def test_author_schema_version_is_defined():
    """GIVEN compact_author_result module WHEN importing THEN COMPACT_SCHEMA_VERSION is defined."""
    assert COMPACT_SCHEMA_VERSION == "1"


def test_author_required_compact_fields_contains_routing_fields():
    """GIVEN REQUIRED_COMPACT_FIELDS WHEN checked THEN contains body_hash and artifact."""
    for field in ["STATUS", "BODY_HASH", "ARTIFACT", "NEXT_ACTION"]:
        assert field in REQUIRED_COMPACT_FIELDS, f"Missing required field: {field}"


# ---------------------------------------------------------------------------
# Happy path: ok fixture with checked_body_sha256
# ---------------------------------------------------------------------------


def test_compact_author_result_ok_from_fixture(tmp_path):
    """GIVEN ok fixture with checked_body_sha256 WHEN compact_author_result THEN body_hash set."""
    fixture = FIXTURES_DIR / "author_result_ok.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    compact_data, stdout_lines = compact_author_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42
    )

    assert compact_data["STATUS"] == "ok"
    assert compact_data["BODY_HASH"] != ""
    assert compact_data["NEXT_ACTION"] == "proceed"
    assert compact_data["ARTIFACT"].startswith("compact_author_result_v1=")


def test_compact_author_result_ok_artifact_written(tmp_path):
    """GIVEN ok fixture WHEN compact_author_result THEN artifact JSON is written."""
    fixture = FIXTURES_DIR / "author_result_ok.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    compact_data, _ = compact_author_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42
    )

    artifact_path = Path(compact_data["ARTIFACT"].split("=", 1)[1])
    assert artifact_path.exists()
    artifact_json = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact_json["schema"] == "ISSUE_AUTHOR_RESULT_COMPACT_V1"
    assert artifact_json["status"] == "ok"


def test_compact_author_result_ok_artifact_permissions(tmp_path):
    """GIVEN ok fixture WHEN artifact written THEN file has 0600 permissions."""
    fixture = FIXTURES_DIR / "author_result_ok.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    compact_data, _ = compact_author_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42
    )

    artifact_path = Path(compact_data["ARTIFACT"].split("=", 1)[1])
    stat = artifact_path.stat()
    assert oct(stat.st_mode & 0o777) == oct(0o600)


# ---------------------------------------------------------------------------
# body_hash via --updated-body argument
# ---------------------------------------------------------------------------


def test_compact_author_result_body_hash_from_updated_body(tmp_path):
    """GIVEN ok fixture without sha256 WHEN updated_body provided THEN body_hash computed."""
    fixture = FIXTURES_DIR / "author_result_no_body_hash.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    body = "## Updated Issue Body\n\nSome content here."
    compact_data, _ = compact_author_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42, updated_body=body
    )

    import hashlib
    expected = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert compact_data["BODY_HASH"] == expected


# ---------------------------------------------------------------------------
# Error path: body_hash missing for ok status
# ---------------------------------------------------------------------------


def test_compact_author_result_missing_body_hash_raises(tmp_path):
    """GIVEN ok fixture without any body hash WHEN compact_author_result THEN ValueError raised."""
    fixture = FIXTURES_DIR / "author_result_no_body_hash.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    with pytest.raises(ValueError, match="body_hash is required"):
        compact_author_result(raw_result, artifact_dir=artifact_dir, issue_number=42)


def test_compact_author_result_cli_missing_body_hash_exits_2(tmp_path):
    """GIVEN CLI with fixture missing body_hash WHEN run THEN exit code is 2."""
    import subprocess

    fixture = FIXTURES_DIR / "author_result_no_body_hash.json"
    script = SCRIPTS_DIR / "compact_author_result.py"

    result = subprocess.run(
        [sys.executable, str(script), "--input-file", str(fixture),
         "--artifact-dir", str(tmp_path), "--issue-number", "42"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# Stdout compliance: no raw content returned to main context (AC5)
# ---------------------------------------------------------------------------


def test_compact_author_result_stdout_no_raw_body(tmp_path):
    """GIVEN ok fixture WHEN stdout generated THEN no raw issue body in stdout."""
    fixture = FIXTURES_DIR / "author_result_ok.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    _, stdout_lines = compact_author_result(raw_result, artifact_dir=artifact_dir, issue_number=42)
    lines_text = "\n".join(stdout_lines)

    # No diff markers
    assert "diff --git" not in lines_text
    assert "@@ -" not in lines_text
    # No traceback
    assert "Traceback" not in lines_text
    # No raw issue body markers
    assert "## Machine-Readable Contract" not in lines_text


def test_compact_author_result_stdout_byte_limit(tmp_path):
    """GIVEN ok fixture WHEN stdout generated THEN UTF-8 bytes <= 2048."""
    fixture = FIXTURES_DIR / "author_result_ok.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    _, stdout_lines = compact_author_result(raw_result, artifact_dir=artifact_dir, issue_number=42)
    lines_text = "\n".join(stdout_lines)

    byte_count = len(lines_text.encode("utf-8"))
    assert byte_count <= 2048, f"stdout too large: {byte_count} bytes"


def test_compact_author_result_raw_log_fixture_would_fail_check_script():
    """GIVEN raw log content WHEN check_agent_friendly_stdout called THEN violation detected."""
    # This test validates the fixture would FAIL the stdout checker
    # (simulating raw output being returned to main context)
    import importlib.util
    import sys

    checker_path = Path(__file__).parent.parent.parent.parent.parent / "scripts" / "check_agent_friendly_stdout.py"
    if not checker_path.exists():
        pytest.skip(f"check_agent_friendly_stdout.py not found at {checker_path}")

    spec = importlib.util.spec_from_file_location("check_agent_friendly_stdout", checker_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    raw_log = "STATUS: failed\nTraceback (most recent call last):\n  File 'foo.py', line 1\nValueError: oops\n"
    violations = module.check_stdout(raw_log)
    assert any("RAW_LOG" in v for v in violations), f"Expected RAW_LOG violation, got: {violations}"


# ---------------------------------------------------------------------------
# Artifact path security
# ---------------------------------------------------------------------------


def test_compact_author_result_rejects_path_traversal():
    """GIVEN path with .. WHEN _validate_artifact_path called THEN ValueError raised."""
    from compact_author_result import _validate_artifact_path
    with pytest.raises(ValueError, match="traversal"):
        _validate_artifact_path("../../etc/passwd")


def test_compact_author_result_rejects_absolute_path():
    """GIVEN absolute path WHEN _validate_artifact_path called THEN ValueError raised."""
    from compact_author_result import _validate_artifact_path
    with pytest.raises(ValueError, match="Absolute"):
        _validate_artifact_path("/etc/passwd")
