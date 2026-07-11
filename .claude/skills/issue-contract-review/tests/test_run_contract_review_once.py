"""
tests/test_run_contract_review_once.py

Unit tests for run_contract_review_once.py

AC6: run_contract_review_once.py の unit test が PASS する

B1: run_once() から check_blockers.sh / check_product_spec_contract.py /
    baseline_vc_preflight.py が全て呼ばれる（不正時は blocked/human_judgment を返す）
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch


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


def _make_product_spec_json(decision: str, applicability: str = "applicable") -> dict:
    return {
        "schema": "product_spec_check/v1",
        "applicability": applicability,
        "decision": decision,
        "triggers": {},
        "conditions": {},
        "blocked_reasons": [],
        "body_sha256": "sha256:abc",
        "source_provenance": {
            "source_type": "github_issue_body",
            "body_file": None,
        },
    }


def _make_vc_preflight_json(status: str) -> dict:
    return {
        "schema": "BASELINE_VC_PREFLIGHT_RESULT_V1",
        "status": status,
        "results": [],
        "errors": [],
    }


def _make_subprocess_result(stdout: str, returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.stdout = stdout
    result.stderr = ""
    result.returncode = returncode
    return result


def _make_all_pass_side_effects():
    """
    Return side_effect iterables for _run_script and _run_shell_script
    that simulate all checks passing.

    _run_script call order:
      1. contract_readiness_check.py → go
      2. check_product_spec_contract.py → pass
      3. baseline_vc_preflight.py → pass

    _run_shell_script call order:
      1. check_blockers.sh → exit 0
    """
    readiness_json = _make_readiness_json("go")
    product_spec_json = _make_product_spec_json("pass", "applicable")
    vc_json = _make_vc_preflight_json("pass")

    run_script_results = [
        (readiness_json, 0, None),   # readiness
        (product_spec_json, 0, None),  # product_spec
        (vc_json, 0, None),           # vc_preflight
    ]
    shell_script_results = [
        (0, "OK: no blockers", ""),  # check_blockers.sh
    ]
    return run_script_results, shell_script_results


# ---------------------------------------------------------------------------
# B1: all four checks are called
# ---------------------------------------------------------------------------


class TestAllChecksCalledB1:
    """B1: run_once calls readiness, blockers, product_spec, and vc_preflight."""

    def test_all_four_checks_called_on_go(self, monkeypatch):
        """When all checks pass, all four are invoked and status is go."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert result["checks"]["readiness"] == "go"
        assert result["checks"]["blockers"] == "pass"
        assert result["checks"]["product_spec"] == "pass"
        assert result["checks"]["product_spec_check"] == _make_product_spec_json(
            "pass", "applicable"
        )
        assert result["checks"]["vc_preflight"] == "pass"

    def test_blockers_blocked_stops_pipeline(self, monkeypatch):
        """If check_blockers.sh returns exit 1 (open blockers), status: blocked."""
        readiness_json = _make_readiness_json("go")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 0, None)):
            with patch.object(
                _rcr_mod, "_run_shell_script",
                return_value=(1, "", "human_escalation: blocker open"),
            ):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "blocked"
        assert result["source"] == "check_blockers"
        assert result["checks"]["blockers"] == "blocked"

    def test_blockers_human_judgment(self, monkeypatch):
        """check_blockers.sh returns 'human_escalation: native API unavailable' → human_judgment."""
        readiness_json = _make_readiness_json("go")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 0, None)):
            with patch.object(
                _rcr_mod, "_run_shell_script",
                return_value=(1, "", "human_escalation: native dependency API unavailable"),
            ):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "human_judgment"
        assert result["source"] == "check_blockers"
        assert result["checks"]["blockers"] == "human_judgment"

    def test_product_spec_fail_blocked(self, monkeypatch):
        """check_product_spec_contract.py applicable+fail → blocked."""
        readiness_json = _make_readiness_json("go")
        product_spec_fail = _make_product_spec_json("fail", "applicable")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_fail, 1, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "blocked"
        assert result["source"] == "product_spec_check"
        assert result["checks"]["product_spec"] == "fail"

    def test_product_spec_human_judgment(self, monkeypatch):
        """check_product_spec_contract.py applicable+human_judgment → human_judgment."""
        readiness_json = _make_readiness_json("go")
        product_spec_hj = _make_product_spec_json("human_judgment", "applicable")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_hj, 1, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "human_judgment"
        assert result["source"] == "product_spec_check"
        assert result["checks"]["product_spec"] == "human_judgment"

    def test_product_spec_not_applicable_treated_as_pass(self, monkeypatch):
        """check_product_spec_contract.py not_applicable → treated as pass, pipeline continues."""
        readiness_json = _make_readiness_json("go")
        product_spec_na = _make_product_spec_json("pass", "not_applicable")
        vc_json = _make_vc_preflight_json("pass")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_na, 0, None),
            (vc_json, 0, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert result["checks"]["product_spec"] == "pass"

    def test_vc_preflight_blocked_stops(self, monkeypatch):
        """baseline_vc_preflight blocked → status: blocked."""
        readiness_json = _make_readiness_json("go")
        product_spec_json = _make_product_spec_json("pass")
        vc_blocked = _make_vc_preflight_json("blocked")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_json, 0, None),
            (vc_blocked, 1, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "blocked"
        assert result["source"] == "vc_preflight"
        assert result["checks"]["vc_preflight"] == "blocked"

    def test_vc_preflight_human_judgment(self, monkeypatch):
        """baseline_vc_preflight human_judgment → status: human_judgment."""
        readiness_json = _make_readiness_json("go")
        product_spec_json = _make_product_spec_json("pass")
        vc_hj = _make_vc_preflight_json("human_judgment")

        run_script_iter = iter([
            (readiness_json, 0, None),
            (product_spec_json, 0, None),
            (vc_hj, 2, None),
        ])

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_script_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", return_value=(0, "OK", "")):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "human_judgment"
        assert result["source"] == "vc_preflight"


# ---------------------------------------------------------------------------
# Status routing tests
# ---------------------------------------------------------------------------


class TestStatusRouting:
    """Test that run_once correctly routes based on readiness status."""

    def test_readiness_go_returns_go(self, monkeypatch):
        """Readiness check returns go (all others also pass) → status: go."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
                with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                    result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "go"
        assert result["source"] == "all_checks_pass"

    def test_readiness_needs_fix_returns_blocked(self, monkeypatch):
        """Readiness check returns needs_fix → status: blocked (pipeline stops)."""
        readiness_json = _make_readiness_json("needs_fix")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 1, None)):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "blocked"
        assert result["source"] == "readiness_check"

    def test_readiness_human_judgment_returns_human_judgment(self, monkeypatch):
        """Readiness check returns human_judgment → status: human_judgment."""
        readiness_json = _make_readiness_json("human_judgment")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 2, None)):
            with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, None)):
                result = run_once(_ISSUE_NUMBER, _REPO, skip_idempotency_check=True)

        assert result["status"] == "human_judgment"
        assert result["source"] == "readiness_check"

    def test_readiness_unknown_status_returns_runtime_error(self, monkeypatch):
        """Unknown readiness status → runtime_error (not human_judgment)."""
        readiness_json = _make_readiness_json("totally_unknown_status")

        with patch.object(_rcr_mod, "_run_script", return_value=(readiness_json, 5, None)):
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
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "check_existing_go_comment", return_value=(None, "gh_timeout")):
            with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
                with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
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
        """All required fields present in output including checks (B1)."""
        run_script_results, shell_results = _make_all_pass_side_effects()
        run_iter = iter(run_script_results)
        shell_iter = iter(shell_results)

        with patch.object(_rcr_mod, "_run_script", side_effect=lambda *a, **kw: next(run_iter)):
            with patch.object(_rcr_mod, "_run_shell_script", side_effect=lambda *a, **kw: next(shell_iter)):
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
            "checks",
            "idempotency_check",
            "errors",
        ]
        for field in required_fields:
            assert field in result, f"Missing required field: {field}"

        assert result["schema"] == "CONTRACT_REVIEW_ONCE_RESULT_V1"

        # B1: checks sub-fields
        assert "readiness" in result["checks"]
        assert "blockers" in result["checks"]
        assert "product_spec" in result["checks"]
        assert "vc_preflight" in result["checks"]
