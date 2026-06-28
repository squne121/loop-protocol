"""
test_artifact_security.py - Tests for artifact write security (AC7).

Verifies:
- artifact write is under .claude/artifacts/issue-refinement-loop/<N>/ only
- .. and absolute paths are rejected
- atomic write with 0600 permissions
- stdout has no secret-like strings
- artifact containment via repo_root (B4)
- artifact content secret check (B5)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SKILLS_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILLS_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from compact_review_result import (
    _atomic_write,
    _no_secret_check,
    _validate_artifact_path,
    _validate_artifact_containment,
    compact_review_result,
)

FIXTURES_DIR = SKILLS_ROOT / "fixtures"


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def test_validate_artifact_path_rejects_absolute():
    """GIVEN absolute path WHEN _validate_artifact_path THEN ValueError raised."""
    with pytest.raises(ValueError, match="Absolute"):
        _validate_artifact_path("/tmp/artifact.json")


def test_validate_artifact_path_rejects_dot_dot():
    """GIVEN path with .. WHEN _validate_artifact_path THEN ValueError raised."""
    with pytest.raises(ValueError, match="traversal"):
        _validate_artifact_path("../../../etc/passwd")


def test_validate_artifact_path_rejects_embedded_dot_dot():
    """GIVEN path with embedded .. WHEN _validate_artifact_path THEN ValueError raised."""
    with pytest.raises(ValueError, match="traversal"):
        _validate_artifact_path(".claude/artifacts/../../../etc/passwd")


def test_validate_artifact_path_accepts_relative():
    """GIVEN relative path without .. WHEN _validate_artifact_path THEN returns Path."""
    result = _validate_artifact_path(".claude/artifacts/issue-refinement-loop/42/result.json")
    assert str(result) == ".claude/artifacts/issue-refinement-loop/42/result.json"


def test_validate_artifact_path_accepts_simple_relative():
    """GIVEN simple relative path WHEN _validate_artifact_path THEN returns Path."""
    result = _validate_artifact_path("artifacts/42/result.json")
    assert result == Path("artifacts/42/result.json")


# ---------------------------------------------------------------------------
# Atomic write with 0600 permissions
# ---------------------------------------------------------------------------


def test_atomic_write_creates_file(tmp_path):
    """GIVEN content and path WHEN _atomic_write THEN file is created."""
    target = tmp_path / "subdir" / "artifact.json"
    _atomic_write(target, b'{"test": true}')
    assert target.exists()
    assert target.read_bytes() == b'{"test": true}'


def test_atomic_write_permissions(tmp_path):
    """GIVEN content WHEN _atomic_write THEN file has 0600 permissions."""
    target = tmp_path / "artifact.json"
    _atomic_write(target, b'{"test": true}')
    stat = target.stat()
    assert oct(stat.st_mode & 0o777) == oct(0o600)


def test_atomic_write_overwrites_existing(tmp_path):
    """GIVEN existing file WHEN _atomic_write THEN file is overwritten atomically."""
    target = tmp_path / "artifact.json"
    _atomic_write(target, b'{"original": true}')
    _atomic_write(target, b'{"updated": true}')
    assert target.read_bytes() == b'{"updated": true}'


def test_atomic_write_creates_parent_dirs(tmp_path):
    """GIVEN path with missing parent dirs WHEN _atomic_write THEN dirs created."""
    target = tmp_path / "a" / "b" / "c" / "artifact.json"
    _atomic_write(target, b'{}')
    assert target.exists()


# ---------------------------------------------------------------------------
# Artifact is under expected directory
# ---------------------------------------------------------------------------


def test_compact_review_result_artifact_path_under_expected_dir(tmp_path):
    """GIVEN approve fixture WHEN compact called THEN artifact is under expected artifact subdir."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude" / "artifacts" / "issue-refinement-loop"

    compact_data, *_ = compact_review_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42
    )

    artifact_path = Path(compact_data["ARTIFACT"].split("=", 1)[1])
    # Artifact must be under artifact_dir/42/
    assert str(artifact_path).startswith(str(artifact_dir / "42"))


# ---------------------------------------------------------------------------
# Secret-like string detection in stdout
# ---------------------------------------------------------------------------


def test_no_secret_check_passes_clean_output():
    """GIVEN clean compact stdout WHEN _no_secret_check THEN no violations."""
    clean = "STATUS: ok\nVERDICT: approve\nNEXT_ACTION: proceed\n"
    violations = _no_secret_check(clean)
    assert violations == []


def test_no_secret_check_detects_bearer_token():
    """GIVEN text with Bearer token WHEN _no_secret_check THEN violation detected."""
    text = "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    violations = _no_secret_check(text)
    assert len(violations) > 0


def test_no_secret_check_detects_github_pat():
    """GIVEN text with GitHub PAT WHEN _no_secret_check THEN violation detected."""
    text = "token: ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    violations = _no_secret_check(text)
    assert len(violations) > 0


def test_no_secret_check_passes_artifact_path():
    """GIVEN text with artifact file path WHEN _no_secret_check THEN no violation."""
    text = (
        "ARTIFACT: compact_review_result_v1="
        ".claude/artifacts/issue-refinement-loop/42/compact_review_result_20260611T000000Z.json"
    )
    violations = _no_secret_check(text)
    assert violations == []


def test_compact_review_result_stdout_no_secrets(tmp_path):
    """GIVEN approve fixture WHEN compact_review_result THEN stdout has no secret violations."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    _compact, stdout_lines, *_ = compact_review_result(raw_result, artifact_dir=artifact_dir, issue_number=42)
    stdout_text = "\n".join(stdout_lines)

    violations = _no_secret_check(stdout_text)
    assert violations == [], f"Secret-like strings detected: {violations}"


# ---------------------------------------------------------------------------
# Artifact content: no secrets
# ---------------------------------------------------------------------------


def test_artifact_json_schema_field_present(tmp_path):
    """GIVEN approve fixture WHEN artifact written THEN schema field is ISSUE_REVIEW_RESULT_COMPACT_V1."""
    fixture = FIXTURES_DIR / "review_result_approve.json"
    raw_result = json.loads(fixture.read_text(encoding="utf-8"))
    artifact_dir = tmp_path / ".claude/artifacts/issue-refinement-loop"

    compact_data, _stdout, artifact_path_val, artifact_content = compact_review_result(
        raw_result, artifact_dir=artifact_dir, issue_number=42
    )
    _atomic_write(artifact_path_val, artifact_content)
    artifact_path = Path(compact_data["ARTIFACT"].split("=", 1)[1])
    artifact_json = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert artifact_json["schema"] == "ISSUE_REVIEW_RESULT_COMPACT_V1"
    assert artifact_json["schema_version"] == "1"


# ---------------------------------------------------------------------------
# B4: _validate_artifact_containment
# ---------------------------------------------------------------------------


def test_validate_artifact_containment_passes(tmp_path):
    """GIVEN artifact path under repo_root WHEN _validate_artifact_containment THEN no error (B4)."""
    repo_root = tmp_path
    artifact_path = repo_root / ".claude/artifacts/issue-refinement-loop/42/result.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.touch()
    _validate_artifact_containment(artifact_path, repo_root)  # should not raise


def test_validate_artifact_containment_rejects_escape(tmp_path):
    """GIVEN artifact path outside repo_root WHEN _validate_artifact_containment THEN ValueError (B4)."""
    import tempfile
    with tempfile.TemporaryDirectory() as other_dir:
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        # artifact_path is in other_dir which is outside repo_root
        artifact_path = Path(other_dir) / ".claude/artifacts/issue-refinement-loop/42/result.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.touch()
        with pytest.raises(ValueError, match="escapes base directory"):
            _validate_artifact_containment(artifact_path, repo_root)


# ---------------------------------------------------------------------------
# B5: artifact content secret check
# ---------------------------------------------------------------------------


def test_no_secret_check_detects_token_in_artifact_content():
    """GIVEN artifact JSON content with token WHEN _no_secret_check THEN violation detected (B5)."""
    content = json.dumps({"note": "token: ghp_" + "A" * 36})
    violations = _no_secret_check(content)
    assert len(violations) > 0


def test_no_secret_check_passes_clean_artifact():
    """GIVEN clean artifact JSON WHEN _no_secret_check THEN no violations (B5)."""
    content = json.dumps({"schema": "ISSUE_REVIEW_RESULT_COMPACT_V1", "verdict": "approve"})
    violations = _no_secret_check(content)
    assert violations == []
