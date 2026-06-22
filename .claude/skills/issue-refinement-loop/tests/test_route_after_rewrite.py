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
from pathlib import Path
from unittest import mock

import pytest

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"

# Add scripts to path for direct import
sys.path.insert(0, str(_SCRIPTS_DIR))


import run_refinement_preflight as wrapper  # noqa: E402 — for AC11 tests


# ---------------------------------------------------------------------------
# Helpers for AC11 tests
# ---------------------------------------------------------------------------

def run_planner(input_data):
    """Run plan_refinement_loop.py and return (output_dict, exit_code)."""
    import subprocess
    import json as _json
    import sys as _sys
    script = _SCRIPTS_DIR / "plan_refinement_loop.py"
    _result = _sys.modules[__name__]  # get module
    proc = subprocess.run(
        [_sys.executable, str(script)],
        input=_json.dumps(input_data, ensure_ascii=False),
        capture_output=True, text=True,
    )
    return _json.loads(proc.stdout), proc.returncode


def make_input(body, issue_number=1):
    """Build a minimal valid REFINEMENT_LOOP_PLANNER_INPUT_V1 from body text."""
    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": issue_number,
            "title": f"Test Issue #{issue_number}",
            "body": body,
            "labels": [],
        },
        "comments": None,
        "known_context": None,
        "now": "2026-01-01T00:00:00+00:00",
    }

from route_after_rewrite import (  # noqa: E402
    _sha256_of_body,
    _build_state_dict,
    _STATE_ALLOWLIST,
)
from decide_rewrite_route import (  # noqa: E402
    LOOP_REWRITE_ROUTER_STATE_V1,
    load_rewrite_router_state,
    validate_state_dict,
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
        state_dict, _ = _build_state_dict(
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
        state_dict, _ = _build_state_dict(
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
        state_dict, _ = _build_state_dict(
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
        state_dict, _ = _build_state_dict(
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
        state_dict, _ = _build_state_dict(
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
        state_dict, _ = _build_state_dict(
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

    def test_fix_category_derives_from_missing_sections(self, tmp_path):
        # B3: artifact は missing_sections の唯一のソース
        artifact = {
            "schema_version": "refinement_preflight_result/v1",
            "required_sections": ["Acceptance Criteria"],
            "required_contract_keys": [],
        }
        artifact_file = tmp_path / "preflight_artifact.json"
        artifact_file.write_text(json.dumps(artifact), encoding="utf-8")
        checker_json = {
            "verdict": "needs-fix",
            "blocking_issues": [
                "必須セクション '## Acceptance Criteria' が存在しない",
            ],
        }
        state_dict, err = _build_state_dict(
            rewrite_attempt_count=0,
            max_rewrite_attempts=3,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            checker_json=checker_json,
            previous_state=None,
            source_issue_body_sha256=VALID_SHA,
            source_body_reset=False,
            preflight_artifact_path=str(artifact_file),
        )
        assert err is None
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
        import tempfile as _tf326
        import os as _os326
        _td326 = _tf326.mkdtemp()
        _af326 = _os326.path.join(_td326, "art.json")
        with open(_af326, "w", encoding="utf-8") as _fp326:
            json.dump({"schema_version": "refinement_preflight_result/v1", "required_sections": ["Acceptance Criteria"], "required_contract_keys": []}, _fp326)
        state_dict, err = _build_state_dict(
            rewrite_attempt_count=2,
            max_rewrite_attempts=3,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            checker_json=checker_json,
            previous_state=previous,
            source_issue_body_sha256=VALID_SHA,
            source_body_reset=False,
            preflight_artifact_path=_af326,
        )
        assert err is None
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
        import tempfile as _tf360
        import os as _os360
        _td360 = _tf360.mkdtemp()
        _af360 = _os360.path.join(_td360, "art2.json")
        with open(_af360, "w", encoding="utf-8") as _fp360:
            json.dump({"schema_version": "refinement_preflight_result/v1", "required_sections": ["Acceptance Criteria"], "required_contract_keys": []}, _fp360)
        state_dict, err = _build_state_dict(
            rewrite_attempt_count=0,
            max_rewrite_attempts=3,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            checker_json=checker_json,
            previous_state=previous,
            source_issue_body_sha256=VALID_SHA,
            source_body_reset=True,
            preflight_artifact_path=_af360,
        )
        assert err is None
        assert state_dict["rewrite_history"] == ["missing_section"]
        assert state_dict["occurrence_count"] == 1
        assert state_dict["fix_category"] == "missing_section"


# ---------------------------------------------------------------------------
# Integration tests — CLI invocation via subprocess (AC4, AC4b, AC4d)
# ---------------------------------------------------------------------------


class TestRouteAfterRewrireCli:
    """Integration tests for route_after_rewrite.py via subprocess."""

    def _make_artifact(self, tmp_path, required_sections=None):
        """テスト用最小 artifact を作成."""
        import json as _j
        artifact = {
            "schema_version": "refinement_preflight_result/v1",
            "required_sections": required_sections or [],
            "required_contract_keys": [],
        }
        p = tmp_path / "artifact.json"
        p.write_text(_j.dumps(artifact), encoding="utf-8")
        return str(p)

    def _run_wrapper(
        self,
        body_file: str,
        state_path: str,
        max_rewrite_attempts: int = 3,
        artifact_path: str | None = None,
    ) -> tuple[int, dict]:
        """Run route_after_rewrite.py CLI and return (exit_code, route_result_dict)."""
        cmd = [
            sys.executable, str(_WRAPPER_SCRIPT),
            "--file", body_file,
            "--state-path", state_path,
            "--max-rewrite-attempts", str(max_rewrite_attempts),
        ]
        if artifact_path is not None:
            cmd += ["--artifact-path", artifact_path]
        proc = subprocess.run(
            cmd,
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
            artifact_path=self._make_artifact(tmp_path),
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
            artifact_path=self._make_artifact(tmp_path),
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

        self._run_wrapper(body_file=str(pass_fixture), state_path=state_path, artifact_path=self._make_artifact(tmp_path))

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

        _artifact = self._make_artifact(tmp_path)
        # First run
        self._run_wrapper(
            body_file=str(fail_fixture),
            state_path=state_path,
            max_rewrite_attempts=5,
            artifact_path=_artifact,
        )
        state_after_first = load_rewrite_router_state(state_path)
        assert state_after_first is not None
        attempt_after_first = state_after_first.rewrite_attempt_count

        # Second run
        self._run_wrapper(
            body_file=str(fail_fixture),
            state_path=state_path,
            max_rewrite_attempts=5,
            artifact_path=_artifact,
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
        artifact_path = self._make_artifact(tmp_path)
        proc = subprocess.run(
            [
                sys.executable, str(_WRAPPER_SCRIPT),
                "--file", str(fail_fixture),
                "--state-path", state_path,
                "--max-rewrite-attempts", "3",
                "--artifact-path", artifact_path,
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



# ---------------------------------------------------------------------------
# AC8: artifact source-of-truth for missing_sections (issue #1067)
# ---------------------------------------------------------------------------


class TestAC8ArtifactSourceOfTruth:
    """AC8: route_after_rewrite reads missing_sections from preflight artifact."""

    def _make_preflight_artifact(self, tmp_path, required_sections=None, required_contract_keys=None):
        """Create a minimal refinement_preflight_result_v1.json artifact."""
        artifact = {
            "schema_version": "refinement_preflight_result/v1",
            "status": "blocked",
            "issue_number": 100,
            "repo": "testowner/testrepo",
            "planner_exit_code": 0,
            "planner_fail_closed": True,
            "next_action": "human_judgment_required",
            "must_read": [],
            "do_not_read": [],
            "commands": [],
            "blockers": ["PLANNER_FAIL_CLOSED"],
            "planner_fail_closed_reason_codes": ["missing_required_section"],
            "required_sections": required_sections or ["Outcome"],
            "required_contract_keys": required_contract_keys or [],
            "artifacts": {},
            "hashes": {},
        }
        p = tmp_path / "refinement_preflight_result_v1.json"
        p.write_text(json.dumps(artifact), encoding="utf-8")
        return str(p)

    def test_extract_missing_from_artifact_success(self, tmp_path):
        """AC8: _extract_missing_from_artifact reads required_sections correctly."""
        from route_after_rewrite import _extract_missing_from_artifact
        artifact_path = self._make_preflight_artifact(
            tmp_path,
            required_sections=["Outcome", "Acceptance Criteria"],
            required_contract_keys=["contract_schema_version"],
        )
        sections, keys, err = _extract_missing_from_artifact(artifact_path)
        assert err is None, f"Expected no error, got {err}"
        assert sections == ["Outcome", "Acceptance Criteria"]
        assert keys == ["contract_schema_version"]

    def test_extract_missing_from_artifact_none_path(self):
        """AC8: None artifact_path returns error reason code."""
        from route_after_rewrite import _extract_missing_from_artifact
        sections, keys, err = _extract_missing_from_artifact(None)
        assert err is not None
        assert sections == []
        assert keys == []

    def test_extract_missing_from_artifact_missing_file(self, tmp_path):
        """AC8: non-existent artifact file returns error reason code."""
        from route_after_rewrite import _extract_missing_from_artifact
        sections, keys, err = _extract_missing_from_artifact(str(tmp_path / "nonexistent.json"))
        assert err is not None

    def test_extract_missing_from_artifact_invalid_schema(self, tmp_path):
        """AC8: artifact with wrong schema_version returns error reason code."""
        from route_after_rewrite import _load_preflight_artifact
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"schema_version": "wrong/v1", "data": "x"}), encoding="utf-8")
        result = _load_preflight_artifact(str(p))
        assert result is None


# ---------------------------------------------------------------------------
# AC11: false-positive fail_closed regression (issue #1067)
# ---------------------------------------------------------------------------


class TestAC11FalsePositiveFailClosedRegression:
    """AC11: valid canonical fixture does NOT trigger fail_closed."""

    def _make_canonical_valid_fixture(self) -> str:
        """Return a canonical valid implementation issue body."""
        return """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#100"
```

## Parent Issue

#100

## Parent Goal Ref

- Goal: test

## Current Validated Scope

- scripts/foo.py

## Remaining Parent Gaps

- [ ] None remaining

## Outcome

Add `scripts/foo.py`.

## In Scope

- scripts/foo.py

## Out of Scope

- Unrelated changes

## Acceptance Criteria

- [ ] AC1: Script exists.

## Verification Commands

```bash
uv run python3 scripts/foo.py
```

## Allowed Paths

- scripts/foo.py

## Stop Conditions

- Allowed Paths 外の変更が必要な場合

## Required Skills

なし
"""

    def test_valid_canonical_body_does_not_trigger_fail_closed(self):
        """AC11: canonical valid body must NOT produce fail_closed.required=True."""
        body = self._make_canonical_valid_fixture()
        output, exit_code = run_planner(make_input(body))
        # May produce warn (unknown confidence) but must NOT be fail_closed
        assert exit_code == 0, f"Expected exit 0 (may warn), got {exit_code}"
        assert output["fail_closed"]["required"] is False, (
            f"Valid canonical body must not trigger fail_closed, "
            f"got reason_codes={output['fail_closed'].get('reason_codes')}"
        )

    def test_valid_body_produces_pass_or_warn_not_blocked(self, tmp_path, capsys):
        """AC11: valid body through preflight → pass or warn, not blocked."""
        body = self._make_canonical_valid_fixture()
        fixture_data = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": 999,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": 999,
                "title": "Canonical Valid Issue",
                "body": body,
                "labels": [],
            },
            "comments": [],
            "anchor_comment_urls": [],
        }
        fixture_path = tmp_path / "canonical.json"
        fixture_path.write_text(json.dumps(fixture_data), encoding="utf-8")

        with mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path):
            result, exit_code = wrapper.run_preflight(
                issue_number=999,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        assert result["status"] in ("pass", "warn"), (
            f"Valid canonical body must produce pass/warn, got {result['status']} "
            f"with blockers={result.get('blockers')}"
        )
        assert exit_code in (0, 1), f"Expected exit 0 or 1, got {exit_code}"


# ---------------------------------------------------------------------------
# Major 3: CLI/subprocess 経由 AC9/AC10 回帰テスト
# ---------------------------------------------------------------------------


def _make_minimal_artifact(artifact_dir: Path, required_sections=None, required_contract_keys=None) -> Path:
    """テスト用の最小 preflight artifact を作成し、パスを返す。"""
    artifact = {
        "schema_version": "refinement_preflight_result/v1",
        "required_sections": required_sections or [],
        "required_contract_keys": required_contract_keys or [],
    }
    artifact_path = artifact_dir / "preflight_artifact.json"
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    return artifact_path


def _run_route_after_rewrite_cli(
    body_file: str,
    state_path: str,
    max_rewrite_attempts: int = 3,
    artifact_path: str | None = None,
    mutation_kind: str | None = None,
) -> tuple[int, dict]:
    """route_after_rewrite.py を subprocess 実行し (exit_code, route_result) を返す。"""
    cmd = [
        sys.executable, str(_WRAPPER_SCRIPT),
        "--file", body_file,
        "--state-path", state_path,
        "--max-rewrite-attempts", str(max_rewrite_attempts),
    ]
    if artifact_path is not None:
        cmd += ["--artifact-path", artifact_path]
    if mutation_kind is not None:
        cmd += ["--mutation-kind", mutation_kind]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    result = {}
    if proc.stdout.strip():
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            pass
    return proc.returncode, result


class TestAc9Ac10CliRegression:
    """Major 3: CLI subprocess 経由の AC9/AC10 回帰テスト。"""

    def _fail_body(self) -> str:
        return _minimal_fail_body() or "## Title\n\ncontent without required sections"

    def test_artifact_path_builds_nonnull_fingerprint(self, tmp_path):
        """AC9: --artifact-path を渡すと route_result.rewrite_request_fingerprint が非 None。"""
        body_file = tmp_path / "body.md"
        body_file.write_text(self._fail_body(), encoding="utf-8")
        state_path = str(tmp_path / "state.json")
        artifact_path = str(_make_minimal_artifact(tmp_path))

        exit_code, result = _run_route_after_rewrite_cli(
            str(body_file), state_path, artifact_path=artifact_path
        )
        assert exit_code == 0, f"Expected exit 0, got {exit_code}; result={result}"
        assert result.get("rewrite_request_fingerprint") is not None, (
            f"rewrite_request_fingerprint must not be None: {result}"
        )

    def test_missing_artifact_path_exits_3(self, tmp_path):
        """AC8/B3: --artifact-path を省略すると exit 3 (environment_failure)。"""
        body_file = tmp_path / "body.md"
        body_file.write_text(self._fail_body(), encoding="utf-8")
        state_path = str(tmp_path / "state.json")

        exit_code, _ = _run_route_after_rewrite_cli(str(body_file), state_path)
        assert exit_code == 3, (
            f"Expected exit 3 when --artifact-path omitted, got {exit_code}"
        )

    def test_format_only_repair_budget_debit_zero(self, tmp_path):
        """AC10: --mutation-kind format_only_repair のとき budget_debit=0。"""
        body_file = tmp_path / "body.md"
        body_file.write_text(self._fail_body(), encoding="utf-8")
        state_path = str(tmp_path / "state.json")
        artifact_path = str(_make_minimal_artifact(tmp_path))

        exit_code, result = _run_route_after_rewrite_cli(
            str(body_file), state_path,
            artifact_path=artifact_path,
            mutation_kind="format_only_repair",
        )
        assert exit_code == 0, f"Expected exit 0, got {exit_code}"
        assert result.get("budget_debit") == 0, (
            f"format_only_repair must produce budget_debit=0: {result}"
        )

    def test_duplicate_fingerprint_routes_human_judgment(self, tmp_path):
        """AC9: 同一 fingerprint が 2 回目で human_judgment_required へルーティングされる。

        fingerprint は checker_json.fail_closed の reason_codes + rewrite_constraints で決まる。
        artifact を変えることで fix_category を 1 回目と別にして AC3 (occurrence_count>=2) を
        回避しつつ、AC9 (fingerprint duplicate) が fire することを確認する。
        """
        body_file = tmp_path / "body.md"
        body_file.write_text(self._fail_body(), encoding="utf-8")
        state_path = str(tmp_path / "state.json")

        # 1 回目: required_sections で missing_section カテゴリ
        (tmp_path / "art1").mkdir(exist_ok=True)
        artifact1 = str(_make_minimal_artifact(tmp_path / "art1", required_sections=["Outcome"]))
        exit_code1, result1 = _run_route_after_rewrite_cli(
            str(body_file), state_path, artifact_path=artifact1
        )
        assert exit_code1 == 0, f"First call failed: {exit_code1}"
        fp1 = result1.get("rewrite_request_fingerprint")
        assert fp1 is not None, f"First call must produce fingerprint; result={result1}"

        # 2 回目: required_contract_keys で missing_contract_key カテゴリ（fix_category が変わる）。
        # fingerprint は checker_json.fail_closed に基づくので同じ body なら同一 → AC9 が fire する。
        (tmp_path / "art2").mkdir(exist_ok=True)
        artifact2 = str(_make_minimal_artifact(tmp_path / "art2", required_sections=[], required_contract_keys=["Outcome"]))
        exit_code2, result2 = _run_route_after_rewrite_cli(
            str(body_file), state_path, artifact_path=artifact2
        )
        assert exit_code2 == 0, f"Second call failed: {exit_code2}"
        fp2 = result2.get("rewrite_request_fingerprint")
        assert fp1 == fp2, (
            f"Fingerprints must match (based on checker_json.fail_closed, not artifact): {fp1!r} vs {fp2!r}"
        )
        assert result2.get("route") == "human_judgment_required", (
            f"Duplicate fingerprint must route to human_judgment_required: {result2.get('route')!r}\n"
            f"full result: {result2}"
        )

    def test_ac9_ac10_fields_persisted_in_state_file(self, tmp_path):
        """B4: AC9/AC10 フィールドが state JSON ファイルに保存される。"""
        body_file = tmp_path / "body.md"
        body_file.write_text(self._fail_body(), encoding="utf-8")
        state_path = tmp_path / "state.json"
        artifact_path = str(_make_minimal_artifact(tmp_path))

        exit_code, _ = _run_route_after_rewrite_cli(
            str(body_file), str(state_path), artifact_path=artifact_path
        )
        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        state_data = json.loads(state_path.read_text(encoding="utf-8"))
        for field in ("rewrite_request_fingerprint", "previous_rewrite_request_fingerprints",
                      "last_mutation_kind", "budget_debit"):
            assert field in state_data, (
                f"AC9/AC10 field '{field}' must be persisted in state: {list(state_data.keys())}"
            )
