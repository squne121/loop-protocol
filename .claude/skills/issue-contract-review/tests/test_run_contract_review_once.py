"""
tests/test_run_contract_review_once.py

Unit tests for run_contract_review_once.py

AC6: run_contract_review_once.py の unit test が PASS する
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_RCR_PATH = _SCRIPTS_DIR / "run_contract_review_once.py"

spec = importlib.util.spec_from_file_location("run_contract_review_once", _RCR_PATH)
assert spec is not None and spec.loader is not None
_rcr_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_rcr_mod)  # type: ignore[union-attr]

run_once = _rcr_mod.run_once
classify_http_error = _rcr_mod.classify_http_error
HTTP_ERROR_CLASSIFICATIONS = _rcr_mod.HTTP_ERROR_CLASSIFICATIONS

_ISSUE_NUMBER = 817
_REPO = "squne121/loop-protocol"
_ISSUE_URL = f"https://github.com/{_REPO}/issues/{_ISSUE_NUMBER}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_readiness_json(status: str) -> dict:
    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": status,
        "body_sha256": "sha256:abc",
        "source_checks": [],
        "errors": [],
        "minimal_context": [],
        "fix_hint": None,
    }


def _make_subprocess_result(stdout: str, returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.stderr = ""
    result.returncode = returncode
    return result


# ---------------------------------------------------------------------------
# Status routing tests
# ---------------------------------------------------------------------------


class TestStatusRouting:
    """Test that run_once correctly routes based on readiness status."""

    def test_readiness_go_returns_go(self, monkeypatch):
        """Readiness check returns go → status: go."""
        readiness_json = _make_readiness_json("go")

        def fake_run_script(cmd, timeout=30):
            return (readiness_json, 0, None)

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert result["source"] in ("readiness_check_static", "vc_preflight_pass")

    def test_readiness_needs_fix_returns_blocked(self, monkeypatch):
        """Readiness check returns needs_fix → status: blocked."""
        readiness_json = _make_readiness_json("needs_fix")

        def fake_run_script(cmd, timeout=30):
            return (readiness_json, 1, None)

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "blocked"
        assert result["source"] == "readiness_check"

    def test_readiness_human_judgment_returns_human_judgment(self, monkeypatch):
        """Readiness check returns human_judgment → status: human_judgment."""
        readiness_json = _make_readiness_json("human_judgment")

        def fake_run_script(cmd, timeout=30):
            return (readiness_json, 2, None)

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "human_judgment"
        assert result["source"] == "readiness_check"

    def test_readiness_unknown_status_returns_runtime_error(self, monkeypatch):
        """Unknown readiness status → runtime_error (not human_judgment)."""
        readiness_json = _make_readiness_json("totally_unknown_status")

        def fake_run_script(cmd, timeout=30):
            return (readiness_json, 5, None)

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"
        assert any("unknown_readiness_status" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# JSON parse failure → runtime_error (not human_judgment)
# ---------------------------------------------------------------------------


class TestJsonParseFailure:
    """AC design: subprocess JSON parse failure → runtime_error, NOT human_judgment."""

    def test_json_parse_failure_is_runtime_error(self, monkeypatch):
        """Corrupt JSON from readiness check → runtime_error."""

        def fake_run_script(cmd, timeout=30):
            return (None, 0, "json_parse_error: Expecting value: line 1 column 1")

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error", (
            "JSON parse failure must be runtime_error, not human_judgment"
        )
        assert any("readiness_check_error" in e for e in result["errors"])

    def test_json_parse_failure_not_human_judgment(self, monkeypatch):
        """JSON parse failure must NOT produce human_judgment status."""

        def fake_run_script(cmd, timeout=30):
            return (None, 1, "json_parse_error: unexpected end")

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] != "human_judgment", (
            "JSON parse failure must never produce human_judgment"
        )


# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------


class TestIdempotencyCheck:
    """Test that existing go comment is returned without running review."""

    def test_existing_go_deduped(self, monkeypatch):
        """If existing go comment found → return early with deduped."""
        existing_url = f"{_ISSUE_URL}#issuecomment-1001"

        with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(existing_url, None)):
            result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=False)

        assert result["status"] == "go"
        assert result["source"] == "existing_go_comment"
        assert result["go_comment_url"] == existing_url
        assert result["idempotency_check"]["deduped"] is True

    def test_idempotency_check_error_non_fatal(self, monkeypatch):
        """Idempotency check error → non-fatal, continue with review."""
        readiness_json = _make_readiness_json("go")

        def fake_run_script(cmd, timeout=30):
            return (readiness_json, 0, None)

        with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, "gh_timeout")):
            with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=False)

        # Error recorded but not fatal
        assert any("idempotency_check_error" in e for e in result["errors"])
        assert result["status"] == "go"  # Review still ran


# ---------------------------------------------------------------------------
# HTTP error classification
# ---------------------------------------------------------------------------


class TestHttpErrorClassification:
    """403/429/422 classification for contract review API calls."""

    def test_403_permission_denied(self):
        assert classify_http_error(403) == "permission_denied"

    def test_429_rate_limited(self):
        assert classify_http_error(429) == "rate_limited"

    def test_422_validation_failed(self):
        assert classify_http_error(422) == "validation_failed_or_spam"

    def test_unknown_ambiguous(self):
        assert classify_http_error(500) == "ambiguous_no_retry"
        assert classify_http_error(503) == "ambiguous_no_retry"

    def test_classification_table_complete(self):
        """Ensure all critical error codes are mapped."""
        assert 403 in HTTP_ERROR_CLASSIFICATIONS
        assert 429 in HTTP_ERROR_CLASSIFICATIONS
        assert 422 in HTTP_ERROR_CLASSIFICATIONS


# ---------------------------------------------------------------------------
# run_script helper
# ---------------------------------------------------------------------------


class TestRunScriptHelper:
    """Tests for _run_script error handling."""

    def test_timeout_returns_error(self, monkeypatch):
        """Timeout → error code, not human_judgment."""

        def fake_run_script(cmd, timeout=30):
            return (None, -1, "timeout")

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"
        assert any("timeout" in e for e in result["errors"])

    def test_no_output_returns_runtime_error(self, monkeypatch):
        """No output from readiness check → runtime_error."""

        def fake_run_script(cmd, timeout=30):
            return (None, 0, None)

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "runtime_error"


# ---------------------------------------------------------------------------
# Schema output validation
# ---------------------------------------------------------------------------


class TestSchemaOutput:
    """Ensure CONTRACT_REVIEW_ONCE_RESULT_V1 schema fields are present."""

    def test_schema_fields_present(self, monkeypatch):
        """All required fields present in output."""
        readiness_json = _make_readiness_json("go")

        def fake_run_script(cmd, timeout=30):
            return (readiness_json, 0, None)

        with patch.object(_rcr_mod, "_run_script", side_effect=fake_run_script):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        required_fields = [
            "schema",
            "issue_number",
            "repo",
            "mode",
            "status",
            "source",
            "go_comment_url",
            "readiness_status",
            "readiness_errors",
            "idempotency_check",
            "errors",
        ]
        for field in required_fields:
            assert field in result, f"Missing required field: {field}"

        assert result["schema"] == "CONTRACT_REVIEW_ONCE_RESULT_V1"
