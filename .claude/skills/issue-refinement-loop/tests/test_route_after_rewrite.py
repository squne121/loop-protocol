"""
test_route_after_rewrite.py

Tests for route_after_rewrite.py wrapper script (Issue #814, AC4, AC4b, AC4c, AC4d).

Covers:
  AC4:  wrapper parses checker stdout JSON only; stderr not merged
  AC4b: checker exit 1 is NOT an infrastructure failure; routing proceeds normally
  AC4c: schema allowlist-outside keys are NOT injected into router state
  AC4d: wrapper uses --state-path / --max-rewrite-attempts; load/save helpers used
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"

# Add scripts to path for direct import
sys.path.insert(0, str(_SCRIPTS_DIR))

from route_after_rewrite import (  # noqa: E402
    _sha256_of_body,
    _run_checker,
    _build_state_dict,
    _STATE_ALLOWLIST,
)
from decide_rewrite_route import (  # noqa: E402
    LOOP_REWRITE_ROUTER_STATE_V1,
    load_rewrite_router_state,
    save_rewrite_router_state,
    validate_state_dict,
    ROUTE_PROCEED_TO_REVIEW,
    ROUTE_CONTINUE_REWRITE,
    ROUTE_HUMAN_JUDGMENT_REQUIRED,
    REASON_CODE_CHECKER_PASSED,
    REASON_CODE_MAX_ATTEMPTS_EXCEEDED,
)

_WRAPPER_SCRIPT = _SCRIPTS_DIR / "route_after_rewrite.py"
_REVIEW_ISSUE_FIXTURES = (
    _SKILL_ROOT.parent / "review-issue" / "fixtures"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_SHA = "a" * 64


def _minimal_pass_body() -> str:
    """Return a minimal issue body that makes check_issue_contract.py return approve (exit 0)."""
    fixture_path = _REVIEW_ISSUE_FIXTURES / "pass_issue.md"
    if fixture_path.exists():
        return fixture_path.read_text(encoding="utf-8")
    # Fallback: inline minimal body (should not be reached in normal test runs)
    return ""


def _minimal_fail_body() -> str:
    """Return an issue body that makes check_issue_contract.py return needs-fix (exit 1)."""
    fixture_path = _REVIEW_ISSUE_FIXTURES / "c1_fail_issue.md"
    if fixture_path.exists():
        return fixture_path.read_text(encoding="utf-8")
    return ""


# ---------------------------------------------------------------------------
# Unit tests — _sha256_of_body
# ---------------------------------------------------------------------------


class TestSha256OfBody:
    def test_deterministic(self):
        """Same body yields same hash."""
        body = "hello world"
        assert _sha256_of_body(body) == _sha256_of_body(body)

    def test_different_bodies_yield_different_hashes(self):
        """Different bodies yield different hashes."""
        assert _sha256_of_body("foo") != _sha256_of_body("bar")

    def test_returns_64_hex_chars(self):
        """SHA-256 hex digest is 64 characters."""
        h = _sha256_of_body("anything")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# Unit tests — _build_state_dict allowlist enforcement (AC4c)
# ---------------------------------------------------------------------------


class TestBuildStateDictAllowlist:
    """AC4c: _build_state_dict must not inject schema-outside keys."""

    def _checker_json_with_extra_keys(self) -> dict:
        """Simulate a checker_json that has keys not in the router state schema."""
        return {
            "verdict": "needs-fix",
            "blocking_issues": [],
            "non_blocking_improvements": [],
            "issue_kind": "implementation",
            "deterministic_checks": {
                "C1_required_sections": "pass",
            },
            "extra_field_not_in_schema": "should not appear in state",
            "another_forbidden_key": 42,
        }

    def test_no_extra_keys_in_state_dict(self):
        """All keys in built state dict are in _STATE_ALLOWLIST."""
        checker_json = self._checker_json_with_extra_keys()
        state_dict = _build_state_dict(
            rewrite_attempt_count=0,
            max_rewrite_attempts=3,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            checker_json=checker_json,
            previous_state=None,
            source_issue_body_sha256=VALID_SHA,
            source_body_reset=False,
        )
        extra = set(state_dict.keys()) - _STATE_ALLOWLIST
        assert not extra, f"Unexpected keys in state dict: {extra}"

    def test_verdict_not_in_state_dict(self):
        """'verdict' from checker_json is never injected into router state."""
        checker_json = {"verdict": "needs-fix", "blocking_issues": []}
        state_dict = _build_state_dict(
            rewrite_attempt_count=0,
            max_rewrite_attempts=3,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            checker_json=checker_json,
            previous_state=None,
            source_issue_body_sha256=VALID_SHA,
            source_body_reset=False,
        )
        assert "verdict" not in state_dict

    def test_deterministic_checks_not_in_state_dict(self):
        """'deterministic_checks' from checker_json is never in router state."""
        checker_json = {
            "verdict": "approve",
            "blocking_issues": [],
            "deterministic_checks": {"C1": "pass"},
        }
        state_dict = _build_state_dict(
            rewrite_attempt_count=0,
            max_rewrite_attempts=3,
            checker_exit_code=0,
            checked_body_sha256=VALID_SHA,
            checker_json=checker_json,
            previous_state=None,
            source_issue_body_sha256=VALID_SHA,
            source_body_reset=False,
        )
        assert "deterministic_checks" not in state_dict

    def test_state_dict_is_schema_valid(self):
        """Built state dict passes validate_state_dict."""
        checker_json = {"verdict": "approve", "blocking_issues": []}
        state_dict = _build_state_dict(
            rewrite_attempt_count=0,
            max_rewrite_attempts=3,
            checker_exit_code=0,
            checked_body_sha256=VALID_SHA,
            checker_json=checker_json,
            previous_state=None,
            source_issue_body_sha256=VALID_SHA,
            source_body_reset=False,
        )
        valid, err = validate_state_dict(state_dict)
        assert valid, f"State dict failed schema validation: {err}"


# ---------------------------------------------------------------------------
# Unit tests — previous_state fields propagated correctly (AC4d)
# ---------------------------------------------------------------------------


class TestBuildStateDictPreviousState:
    """AC4d: previous_* fields from loaded state are correctly wired into new state."""

    def test_no_previous_state_sets_none_and_empty(self):
        """With no previous state, previous_* fields are None / empty."""
        checker_json = {"verdict": "needs-fix", "blocking_issues": []}
        state_dict = _build_state_dict(
            rewrite_attempt_count=0,
            max_rewrite_attempts=3,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            checker_json=checker_json,
            previous_state=None,
            source_issue_body_sha256=VALID_SHA,
            source_body_reset=False,
        )
        assert state_dict["previous_checked_body_sha256"] is None
        assert state_dict["previous_missing_sections"] == []
        assert state_dict["previous_missing_contract_keys"] == []
        assert state_dict["replay_safe"] is False

    def test_previous_state_fields_propagated(self):
        """With previous state, previous_* fields are taken from it."""
        prev = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=3,
            checker_exit_code=1,
            checked_body_sha256="b" * 64,
            missing_sections=["S1"],
            missing_contract_keys=["K1"],
        )
        checker_json = {"verdict": "needs-fix", "blocking_issues": []}
        state_dict = _build_state_dict(
            rewrite_attempt_count=2,
            max_rewrite_attempts=3,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            checker_json=checker_json,
            previous_state=prev,
            source_issue_body_sha256=VALID_SHA,
            source_body_reset=False,
        )
        assert state_dict["previous_checked_body_sha256"] == "b" * 64
        assert state_dict["previous_missing_sections"] == ["S1"]
        assert state_dict["previous_missing_contract_keys"] == ["K1"]
        assert state_dict["replay_safe"] is True


# ---------------------------------------------------------------------------
# Unit tests — category metadata in state dict (fix_category / history / count)
# ---------------------------------------------------------------------------


class TestBuildStateDictCategoryMetadata:
    """fix_category and rewrite_history are built deterministically."""

    def test_fix_category_derives_from_missing_sections(self):
        checker_json = {
            "verdict": "needs-fix",
            "blocking_issues": [
                "必須セクション '## Acceptance Criteria' が存在しない",
            ],
        }
        state_dict = _build_state_dict(
            rewrite_attempt_count=0,
            max_rewrite_attempts=3,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            checker_json=checker_json,
            previous_state=None,
            source_issue_body_sha256=VALID_SHA,
            source_body_reset=False,
        )
        assert state_dict["fix_category"] == "missing_section"
        assert state_dict["rewrite_history"] == ["missing_section"]
        assert state_dict["occurrence_count"] == 1

    def test_rewrite_history_tracks_category_recurrence(self):
        previous = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=3,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            fix_category="missing_section",
            rewrite_history=["missing_section", "missing_contract_key"],
            missing_sections=["S1"],
            missing_contract_keys=["K1"],
        )
        checker_json = {
            "verdict": "needs-fix",
            "blocking_issues": [
                "必須セクション '## Acceptance Criteria' が存在しない",
            ],
        }
        state_dict = _build_state_dict(
            rewrite_attempt_count=2,
            max_rewrite_attempts=3,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            checker_json=checker_json,
            previous_state=previous,
            source_issue_body_sha256=VALID_SHA,
            source_body_reset=False,
        )
        assert state_dict["rewrite_history"] == [
            "missing_section",
            "missing_contract_key",
            "missing_section",
        ]
        assert state_dict["occurrence_count"] == 2

    def test_source_body_reset_clears_stale_rewrite_history(self):
        previous = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=2,
            max_rewrite_attempts=3,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            fix_category="missing_section",
            rewrite_history=["missing_section", "missing_contract_key"],
            missing_sections=["S1"],
            missing_contract_keys=["K1"],
        )
        checker_json = {
            "verdict": "needs-fix",
            "blocking_issues": [
                "必須セクション '## Acceptance Criteria' が存在しない",
            ],
        }
        state_dict = _build_state_dict(
            rewrite_attempt_count=0,
            max_rewrite_attempts=3,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            checker_json=checker_json,
            previous_state=previous,
            source_issue_body_sha256=VALID_SHA,
            source_body_reset=True,
        )
        assert state_dict["rewrite_history"] == ["missing_section"]
        assert state_dict["occurrence_count"] == 1
        assert state_dict["fix_category"] == "missing_section"


# ---------------------------------------------------------------------------
# Integration tests — CLI invocation via subprocess (AC4, AC4b, AC4d)
# ---------------------------------------------------------------------------


class TestRouteAfterRewrireCli:
    """Integration tests for route_after_rewrite.py via subprocess."""

    def _run_wrapper(
        self,
        body_file: str,
        state_path: str,
        max_rewrite_attempts: int = 3,
    ) -> tuple[int, dict]:
        """Run route_after_rewrite.py CLI and return (exit_code, route_result_dict)."""
        proc = subprocess.run(
            [
                sys.executable, str(_WRAPPER_SCRIPT),
                "--file", body_file,
                "--state-path", state_path,
                "--max-rewrite-attempts", str(max_rewrite_attempts),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode not in (0, 2, 3):
            pytest.fail(
                f"Unexpected exit code {proc.returncode}\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        if proc.returncode == 0:
            try:
                return proc.returncode, json.loads(proc.stdout)
            except json.JSONDecodeError:
                pytest.fail(f"stdout is not valid JSON: {proc.stdout[:500]}")
        return proc.returncode, {}

    def test_checker_approve_yields_proceed_to_review(self, tmp_path):
        """AC4: pass fixture -> checker exit 0 -> route: proceed_to_review."""
        pass_fixture = _REVIEW_ISSUE_FIXTURES / "pass_issue.md"
        if not pass_fixture.exists():
            pytest.skip("pass_issue.md fixture not found")

        state_path = str(tmp_path / "state.json")
        exit_code, route_result = self._run_wrapper(
            body_file=str(pass_fixture),
            state_path=state_path,
        )
        assert exit_code == 0
        assert route_result.get("route") == "proceed_to_review"
        assert route_result.get("reason_code") == "checker_passed"

    def test_checker_fail_yields_continue_or_human_judgment(self, tmp_path):
        """AC4b: fail fixture -> checker exit 1 is NOT infrastructure failure; routing proceeds."""
        fail_fixture = _REVIEW_ISSUE_FIXTURES / "c1_fail_issue.md"
        if not fail_fixture.exists():
            pytest.skip("c1_fail_issue.md fixture not found")

        state_path = str(tmp_path / "state.json")
        exit_code, route_result = self._run_wrapper(
            body_file=str(fail_fixture),
            state_path=state_path,
            max_rewrite_attempts=3,
        )
        assert exit_code == 0, "Wrapper must exit 0 even when checker returns needs-fix"
        route = route_result.get("route")
        assert route in ("continue_rewrite", "human_judgment_required"), (
            f"Expected continue_rewrite or human_judgment_required, got {route}"
        )

    def test_state_persisted_after_invocation(self, tmp_path):
        """AC4d: state.json is written after each invocation (attempt counter persists)."""
        pass_fixture = _REVIEW_ISSUE_FIXTURES / "pass_issue.md"
        if not pass_fixture.exists():
            pytest.skip("pass_issue.md fixture not found")

        state_path = str(tmp_path / "state.json")
        assert not os.path.exists(state_path), "State file should not exist before first run"

        self._run_wrapper(body_file=str(pass_fixture), state_path=state_path)

        assert os.path.exists(state_path), "State file must be created after wrapper run"

        loaded = load_rewrite_router_state(state_path)
        assert loaded is not None
        assert loaded.rewrite_attempt_count >= 0

    def test_second_invocation_increments_attempt_counter(self, tmp_path):
        """AC4d: second run increments attempt counter (replay-safe persistence)."""
        fail_fixture = _REVIEW_ISSUE_FIXTURES / "c1_fail_issue.md"
        if not fail_fixture.exists():
            pytest.skip("c1_fail_issue.md fixture not found")

        state_path = str(tmp_path / "state.json")

        # First run
        self._run_wrapper(
            body_file=str(fail_fixture),
            state_path=state_path,
            max_rewrite_attempts=5,
        )
        state_after_first = load_rewrite_router_state(state_path)
        assert state_after_first is not None
        attempt_after_first = state_after_first.rewrite_attempt_count

        # Second run
        self._run_wrapper(
            body_file=str(fail_fixture),
            state_path=state_path,
            max_rewrite_attempts=5,
        )
        state_after_second = load_rewrite_router_state(state_path)
        assert state_after_second is not None
        attempt_after_second = state_after_second.rewrite_attempt_count

        assert attempt_after_second > attempt_after_first, (
            f"Attempt counter must increase: {attempt_after_first} -> {attempt_after_second}"
        )

    def test_missing_required_args_exits_nonzero(self, tmp_path):
        """AC4d: --state-path and --max-rewrite-attempts are required."""
        pass_fixture = _REVIEW_ISSUE_FIXTURES / "pass_issue.md"
        if not pass_fixture.exists():
            pytest.skip("pass_issue.md fixture not found")

        # Missing --state-path
        proc = subprocess.run(
            [
                sys.executable, str(_WRAPPER_SCRIPT),
                "--file", str(pass_fixture),
                "--max-rewrite-attempts", "3",
                # no --state-path
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0, "Should fail without --state-path"

        # Missing --max-rewrite-attempts
        proc2 = subprocess.run(
            [
                sys.executable, str(_WRAPPER_SCRIPT),
                "--file", str(pass_fixture),
                "--state-path", str(tmp_path / "state.json"),
                # no --max-rewrite-attempts
            ],
            capture_output=True,
            text=True,
        )
        assert proc2.returncode != 0, "Should fail without --max-rewrite-attempts"

    def test_checker_exit1_not_treated_as_infrastructure_failure(self, tmp_path):
        """AC4b: checker exit 1 must NOT cause wrapper to exit with code 3.

        Exit code 3 is reserved for infrastructure failures (gh error, JSON parse
        failure, subprocess error). Exit 1 from the checker (needs-fix) is a normal
        routing outcome and must be forwarded to the router, not raised as an error.
        """
        fail_fixture = _REVIEW_ISSUE_FIXTURES / "c1_fail_issue.md"
        if not fail_fixture.exists():
            pytest.skip("c1_fail_issue.md fixture not found")

        state_path = str(tmp_path / "state.json")
        proc = subprocess.run(
            [
                sys.executable, str(_WRAPPER_SCRIPT),
                "--file", str(fail_fixture),
                "--state-path", state_path,
                "--max-rewrite-attempts", "3",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 3, (
            "Wrapper must NOT exit 3 (infrastructure failure) when checker returns needs-fix (exit 1). "
            f"stderr: {proc.stderr}"
        )
        assert proc.returncode == 0, (
            f"Wrapper must exit 0 when routing succeeds. "
            f"returncode={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
