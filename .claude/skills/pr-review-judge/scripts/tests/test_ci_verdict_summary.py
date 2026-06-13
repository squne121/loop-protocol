"""
Unit tests for ci_verdict_summary.py

Tests AC1-AC12: 4-state verdict (pass/fail/pending/stale),
bucket mapping, conclusion mapping, stdout schema, log_excerpt,
sanitize_check_name, check-name filter, gh_error handling.

Fixtures are JSON files under tests/fixtures/ci_verdict/.
"""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from ci_verdict_summary import (
    EXIT_ALL_PASS,
    EXIT_FAILED,
    EXIT_GH_ERROR,
    EXIT_PENDING,
    EXIT_STALE,
    classify_check,
    classify_gh_error,
    compute_overall_status,
    determine_check_verdict,
    extract_run_id_from_link,
    find_failed_job_id,
    main,
    next_action_for,
    sanitize_check_name,
    save_log_artifact,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ci_verdict"

HEAD_SHA = "abc1234567890abcdef1234567890abcdef123456"
STALE_SHA = "deadbeef1234567890abcdef1234567890abcdef"
EXPECTED_SHA = HEAD_SHA


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_fixture(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text())


def make_mock_run_gh(
    head_sha: str = HEAD_SHA,
    checks: Optional[list] = None,
    checks_ok: bool = True,
    checks_err_msg: str = "",
    run_data: Optional[dict] = None,
):
    """
    run_gh をモックする。
    呼び出し順: pr view → pr checks → (run view)*
    """
    call_count = 0
    checks_list = checks if checks is not None else []

    def _run_gh(args: list[str]):
        nonlocal call_count
        call_count += 1

        if "pr" in args and "view" in args and "headRefOid" in args:
            return True, {"headRefOid": head_sha}, json.dumps({"headRefOid": head_sha})
        if "pr" in args and "checks" in args:
            if not checks_ok:
                return False, None, checks_err_msg
            return True, checks_list, json.dumps(checks_list)
        if "run" in args and "view" in args and "--log" not in args:
            if run_data is not None:
                return True, run_data, json.dumps(run_data)
            return True, {}, "{}"
        if "run" in args and "view" in args and "--log" in args:
            return True, None, "fake log line 1\nfake log line 2"
        return False, None, "unexpected gh call"

    return _run_gh


# ---------------------------------------------------------------------------
# AC: stale (head SHA mismatch)
# ---------------------------------------------------------------------------

class TestStale:
    """AC5: stale_head_sha の4状態のうち stale 状態判定テスト"""

    def test_stale_overall_status_when_pr_head_differs_from_expected(self):
        """GIVEN PR head SHA != expected SHA
        WHEN summary is computed
        THEN overall status is stale_head_sha and exit is EXIT_STALE
        """
        checks_data = load_fixture("checks_all_pass.json")
        mock_fn = make_mock_run_gh(head_sha=STALE_SHA, checks=checks_data)
        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "42",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", EXPECTED_SHA]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    exit_code = main()
        assert exit_code == EXIT_STALE
        out = json.loads(buf.getvalue())
        assert out["status"] == "stale_head_sha"

    def test_stale_check_entry_head_sha_mismatch(self):
        """GIVEN check entry has different head_sha than pr head
        WHEN determine_check_verdict is called
        THEN verdict is stale_head_sha
        """
        entry = {
            "head_sha": "different_sha",
            "bucket": "pass",
            "status": "completed",
            "conclusion": "success",
        }
        assert determine_check_verdict(entry, pr_head_sha=HEAD_SHA) == "stale_head_sha"

    def test_stale_priority_over_failed(self):
        """GIVEN both stale and failed verdicts
        WHEN compute_overall_status
        THEN stale_head_sha wins
        """
        verdicts = ["stale_head_sha", "failed", "all_pass"]
        assert compute_overall_status(verdicts) == "stale_head_sha"


# ---------------------------------------------------------------------------
# AC: bucket mapping
# ---------------------------------------------------------------------------

class TestBucket:
    """AC6: bucket フィールドの正規化テスト"""

    def test_pass_bucket_yields_all_pass(self):
        raw = {
            "name": "build", "bucket": "pass", "state": "SUCCESS",
            "workflow": "CI", "link": None, "event": "push",
            "startedAt": None, "completedAt": None,
        }
        entry = classify_check(raw, HEAD_SHA)
        assert entry["bucket"] == "pass"
        assert entry["conclusion"] == "success"
        assert determine_check_verdict(entry, HEAD_SHA) == "all_pass"

    def test_fail_bucket_yields_failed(self):
        raw = {
            "name": "test", "bucket": "fail", "state": "FAILURE",
            "workflow": "CI", "link": None, "event": "push",
            "startedAt": None, "completedAt": None,
        }
        entry = classify_check(raw, HEAD_SHA)
        assert entry["bucket"] == "fail"
        assert determine_check_verdict(entry, HEAD_SHA) == "failed"

    def test_pending_bucket_yields_pending_or_queued(self):
        raw = {
            "name": "deploy", "bucket": "pending", "state": "IN_PROGRESS",
            "workflow": "CD", "link": None, "event": "push",
            "startedAt": None, "completedAt": None,
        }
        entry = classify_check(raw, HEAD_SHA)
        assert determine_check_verdict(entry, HEAD_SHA) == "pending_or_queued"

    def test_skipping_bucket_yields_failed(self):
        """skipping → conclusion=skipped → failed (approve evidence として不可)"""
        raw = {
            "name": "lint", "bucket": "skipping", "state": "SKIPPED",
            "workflow": "CI", "link": None, "event": "push",
            "startedAt": None, "completedAt": None,
        }
        entry = classify_check(raw, HEAD_SHA)
        assert entry["conclusion"] == "skipped"
        assert determine_check_verdict(entry, HEAD_SHA) == "failed"

    def test_cancel_bucket_yields_failed(self):
        raw = {
            "name": "build", "bucket": "cancel", "state": "CANCELLED",
            "workflow": "CI", "link": None, "event": "push",
            "startedAt": None, "completedAt": None,
        }
        entry = classify_check(raw, HEAD_SHA)
        assert determine_check_verdict(entry, HEAD_SHA) == "failed"

    def test_null_bucket_treated_as_pending(self):
        """null bucket → unknown → pending_or_queued"""
        raw = {
            "name": "check", "bucket": None, "state": None,
            "workflow": None, "link": None, "event": "push",
            "startedAt": None, "completedAt": None,
        }
        entry = classify_check(raw, HEAD_SHA)
        assert determine_check_verdict(entry, HEAD_SHA) == "pending_or_queued"

    def test_all_checks_pass_status_is_all_pass(self):
        """GIVEN all checks have pass bucket WHEN run THEN status all_pass"""
        checks_data = load_fixture("checks_all_pass.json")
        # run_data には HEAD_SHA を一致させる
        run_data = {
            "headSha": HEAD_SHA,
            "conclusion": "success",
            "status": "completed",
            "workflowName": "CI",
            "jobs": [{"databaseId": 1001, "name": "build", "conclusion": "success"}],
            "databaseId": 1001,
        }
        mock_fn = make_mock_run_gh(head_sha=HEAD_SHA, checks=checks_data, run_data=run_data)
        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "1",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    exit_code = main()
        assert exit_code == EXIT_ALL_PASS
        out = json.loads(buf.getvalue())
        assert out["status"] == "all_pass"
        assert out["failed_checks"] == []


# ---------------------------------------------------------------------------
# AC: conclusion mapping
# ---------------------------------------------------------------------------

class TestConclusion:
    """AC7: GitHub status/conclusion → verdict mapping テスト"""

    @pytest.mark.parametrize("conclusion,expected_verdict", [
        ("success", "all_pass"),
        ("failure", "failed"),
        ("timed_out", "failed"),
        ("action_required", "failed"),
        ("neutral", "failed"),    # approve evidence として不可
        ("skipped", "failed"),    # approve evidence として不可
        ("stale", "failed"),      # approve evidence として不可
        ("cancelled", "failed"),
    ])
    def test_conclusion_to_verdict(self, conclusion: str, expected_verdict: str):
        entry = {
            "head_sha": None,
            "bucket": None,
            "status": "completed",
            "conclusion": conclusion,
        }
        assert determine_check_verdict(entry, HEAD_SHA) == expected_verdict

    @pytest.mark.parametrize("status_val", [
        "queued", "in_progress", "waiting", "requested", "pending"
    ])
    def test_pending_statuses_yield_pending_or_queued(self, status_val: str):
        entry = {
            "head_sha": None,
            "bucket": None,
            "status": status_val,
            "conclusion": None,
        }
        assert determine_check_verdict(entry, HEAD_SHA) == "pending_or_queued"

    def test_priority_stale_over_gh_error_over_pending_over_failed(self):
        verdicts = ["all_pass", "failed", "pending_or_queued", "gh_error", "stale_head_sha"]
        assert compute_overall_status(verdicts) == "stale_head_sha"
        verdicts2 = ["all_pass", "failed", "pending_or_queued", "gh_error"]
        assert compute_overall_status(verdicts2) == "gh_error"
        verdicts3 = ["all_pass", "failed", "pending_or_queued"]
        assert compute_overall_status(verdicts3) == "pending_or_queued"
        verdicts4 = ["all_pass", "failed"]
        assert compute_overall_status(verdicts4) == "failed"
        verdicts5 = ["all_pass"]
        assert compute_overall_status(verdicts5) == "all_pass"


# ---------------------------------------------------------------------------
# AC: stdout schema
# ---------------------------------------------------------------------------

class TestStdout:
    """AC8: stdout が CI_VERDICT_SUMMARY_V1 schema を持つことを確認"""

    REQUIRED_KEYS = [
        "schema", "generated_at", "repo", "pr", "expected_head_sha",
        "head_sha", "status", "checks", "failed_checks", "pending_checks",
        "stale_checks", "log_artifacts", "errors", "next_action",
    ]

    def test_stdout_schema_fields_present_on_all_pass(self):
        """GIVEN all pass checks WHEN run THEN stdout JSON has all required schema fields"""
        checks_data = load_fixture("checks_all_pass.json")
        run_data = {
            "headSha": HEAD_SHA,
            "conclusion": "success",
            "status": "completed",
            "workflowName": "CI",
            "jobs": [{"databaseId": 1001, "name": "build", "conclusion": "success"}],
            "databaseId": 1001,
        }
        mock_fn = make_mock_run_gh(head_sha=HEAD_SHA, checks=checks_data, run_data=run_data)
        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "10",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    main()
        out = json.loads(buf.getvalue())
        for key in self.REQUIRED_KEYS:
            assert key in out, f"Missing key: {key}"

    def test_stdout_schema_field_schema_is_CI_VERDICT_SUMMARY_V1(self):
        checks_data = load_fixture("checks_all_pass.json")
        run_data = {
            "headSha": HEAD_SHA,
            "conclusion": "success",
            "status": "completed",
            "workflowName": "CI",
            "jobs": [{"databaseId": 1001, "name": "build", "conclusion": "success"}],
            "databaseId": 1001,
        }
        mock_fn = make_mock_run_gh(head_sha=HEAD_SHA, checks=checks_data, run_data=run_data)
        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "10",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    main()
        out = json.loads(buf.getvalue())
        assert out["schema"] == "CI_VERDICT_SUMMARY_V1"

    def test_stdout_next_action_mapping(self):
        assert next_action_for("all_pass") == "none"
        assert next_action_for("failed") == "inspect_failed_log_artifacts"
        assert next_action_for("pending_or_queued") == "wait_for_ci"
        assert next_action_for("stale_head_sha") == "refresh_head_sha"
        assert next_action_for("gh_error") == "manual_review_gh_error"

    def test_failed_checks_populated_on_failure(self):
        checks_data = load_fixture("checks_failed.json")
        run_data = {
            "headSha": HEAD_SHA,
            "conclusion": "failure",
            "status": "completed",
            "workflowName": "CI",
            "jobs": [{"databaseId": 1002, "name": "test", "conclusion": "failure"}],
            "databaseId": 1002,
        }
        mock_fn = make_mock_run_gh(head_sha=HEAD_SHA, checks=checks_data, run_data=run_data)
        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "11",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    exit_code = main()
        out = json.loads(buf.getvalue())
        assert exit_code == EXIT_FAILED
        assert "test" in out["failed_checks"]

    def test_pending_checks_populated_on_pending(self):
        checks_data = load_fixture("checks_pending.json")
        mock_fn = make_mock_run_gh(head_sha=HEAD_SHA, checks=checks_data)
        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "12",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    exit_code = main()
        out = json.loads(buf.getvalue())
        assert exit_code == EXIT_PENDING
        assert "test" in out["pending_checks"]


# ---------------------------------------------------------------------------
# AC: log_excerpt (artifact materialization)
# ---------------------------------------------------------------------------

class TestLogExcerpt:
    """AC9: --include-log-excerpt が stdout に raw log を出力せず artifact を保存する"""

    def test_log_excerpt_does_not_write_raw_log_to_stdout(self):
        """GIVEN --include-log-excerpt
        WHEN failed check has job_id
        THEN stdout does NOT contain raw log lines
        """
        checks_data = [
            {
                "name": "test",
                "bucket": "fail",
                "state": "FAILURE",
                "workflow": "CI",
                "link": "https://github.com/owner/repo/actions/runs/9999",
                "event": "push",
                "startedAt": None,
                "completedAt": None,
            }
        ]
        run_data = {
            "headSha": HEAD_SHA,
            "conclusion": "failure",
            "status": "completed",
            "workflowName": "CI",
            "jobs": [{"databaseId": 12345, "name": "test", "conclusion": "failure"}],
            "databaseId": 9999,
        }
        mock_fn = make_mock_run_gh(head_sha=HEAD_SHA, checks=checks_data, run_data=run_data)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
                with patch("ci_verdict_summary.get_repo_root", return_value=Path(tmpdir)):
                    with patch("sys.argv", ["ci_verdict_summary.py",
                                            "--pr", "5",
                                            "--repo", "owner/repo",
                                            "--expected-head-sha", HEAD_SHA,
                                            "--include-log-excerpt"]):
                        import io
                        from contextlib import redirect_stdout
                        buf = io.StringIO()
                        with redirect_stdout(buf):
                            main()

        out_text = buf.getvalue()
        # raw log lines must not appear in stdout
        assert "fake log line 1" not in out_text
        assert "fake log line 2" not in out_text

    def test_log_artifact_entry_has_required_fields(self):
        """save_log_artifact returns dict with required fields"""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_entry = save_log_artifact(
                log_text="line1\nline2",
                pr_number=42,
                head_sha=HEAD_SHA,
                check_name="my-check",
                job_id=999,
                artifacts_base=Path(tmpdir),
            )
        required = {"check_name", "job_id", "path", "sha256", "bytes", "truncated"}
        assert required.issubset(set(artifact_entry.keys()))
        assert artifact_entry["check_name"] == "my-check"
        assert artifact_entry["job_id"] == 999
        assert artifact_entry["sha256"].startswith("sha256:")
        assert not artifact_entry["truncated"]

    def test_log_artifact_truncated_for_large_logs(self):
        """Logs exceeding 64KB are truncated"""
        large_log = "x" * (64 * 1024 + 100)
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_entry = save_log_artifact(
                log_text=large_log,
                pr_number=1,
                head_sha=HEAD_SHA,
                check_name="big-check",
                job_id=None,
                artifacts_base=Path(tmpdir),
            )
        assert artifact_entry["truncated"] is True
        assert artifact_entry["bytes"] == 64 * 1024

    def test_log_artifact_path_contains_pr_and_sha(self):
        """Artifact path includes pr-<n>/head-<sha>/<check>.log"""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_entry = save_log_artifact(
                log_text="log content",
                pr_number=99,
                head_sha=HEAD_SHA,
                check_name="build",
                job_id=1,
                artifacts_base=Path(tmpdir),
            )
        assert f"pr-99" in artifact_entry["path"]
        assert f"head-{HEAD_SHA}" in artifact_entry["path"]
        assert artifact_entry["path"].endswith(".log")

    def test_log_fetch_error_sets_gh_error_verdict(self):
        """B4: ログ取得失敗時は gh_error verdict が追加され next_action が manual_review_gh_error になる"""
        checks_data = [
            {
                "name": "test",
                "bucket": "fail",
                "state": "FAILURE",
                "workflow": "CI",
                "link": "https://github.com/owner/repo/actions/runs/9999",
                "event": "push",
                "startedAt": None,
                "completedAt": None,
            }
        ]
        run_data = {
            "headSha": HEAD_SHA,
            "conclusion": "failure",
            "status": "completed",
            "workflowName": "CI",
            "jobs": [{"databaseId": 12345, "name": "test", "conclusion": "failure"}],
            "databaseId": 9999,
        }

        def mock_fn_log_fail(args: list[str]):
            if "pr" in args and "view" in args and "headRefOid" in args:
                return True, {"headRefOid": HEAD_SHA}, json.dumps({"headRefOid": HEAD_SHA})
            if "pr" in args and "checks" in args:
                return True, checks_data, json.dumps(checks_data)
            if "run" in args and "view" in args and "--log" not in args:
                return True, run_data, json.dumps(run_data)
            if "run" in args and "view" in args and "--log" in args:
                return False, None, "403 permission denied"
            return False, None, "unexpected"

        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn_log_fail):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "5",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA,
                                    "--include-log-excerpt"]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    exit_code = main()
        out = json.loads(buf.getvalue())
        assert exit_code == EXIT_GH_ERROR
        assert out["status"] == "gh_error"
        assert out["next_action"] == "manual_review_gh_error"
        log_fetch_errors = [e for e in out["errors"] if e.get("kind") == "log_fetch_error"]
        assert len(log_fetch_errors) > 0


# ---------------------------------------------------------------------------
# AC: sanitize_check_name
# ---------------------------------------------------------------------------

class TestSanitize:
    """AC10: check name の path traversal sanitize テスト"""

    def test_sanitize_removes_slash(self):
        assert "/" not in sanitize_check_name("a/b/c")

    def test_sanitize_removes_backslash(self):
        assert "\\" not in sanitize_check_name("a\\b")

    def test_sanitize_removes_dotdot(self):
        result = sanitize_check_name("../evil")
        assert ".." not in result
        assert "/" not in result

    def test_sanitize_removes_leading_dot(self):
        result = sanitize_check_name(".hidden")
        assert not result.startswith(".")

    def test_sanitize_path_traversal_complex(self):
        result = sanitize_check_name("../../etc/passwd")
        assert "/" not in result
        assert ".." not in result

    def test_sanitize_normal_name_preserved(self):
        result = sanitize_check_name("build-and-test")
        assert "build" in result
        assert "test" in result

    def test_sanitize_truncates_to_128(self):
        long_name = "a" * 200
        result = sanitize_check_name(long_name)
        assert len(result) <= 128

    def test_sanitize_empty_name_returns_unnamed(self):
        result = sanitize_check_name("")
        assert result == "unnamed"

    def test_sanitize_only_dangerous_chars_returns_unnamed(self):
        result = sanitize_check_name("///")
        assert result == "unnamed"


# ---------------------------------------------------------------------------
# AC: check_name filter (B1: 0件→gh_error, 複数件→gh_error)
# ---------------------------------------------------------------------------

class TestCheckName:
    """AC11: --check-name exact match フィルタのテスト（B1修正後）"""

    def test_check_name_filter_includes_only_matching(self):
        """GIVEN --check-name build WHEN checks include build and test
        THEN only build appears in output checks
        """
        checks_data = load_fixture("checks_all_pass.json")
        run_data = {
            "headSha": HEAD_SHA,
            "conclusion": "success",
            "status": "completed",
            "workflowName": "CI",
            "jobs": [{"databaseId": 1001, "name": "build", "conclusion": "success"}],
            "databaseId": 1001,
        }
        mock_fn = make_mock_run_gh(head_sha=HEAD_SHA, checks=checks_data, run_data=run_data)
        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "20",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA,
                                    "--check-name", "build"]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    main()
        out = json.loads(buf.getvalue())
        check_names = [c["name"] for c in out["checks"]]
        assert "build" in check_names
        assert "test" not in check_names

    def test_check_name_filter_no_match_returns_gh_error(self):
        """B1: GIVEN --check-name nonexistent WHEN checks don't match
        THEN status is gh_error with kind=check_not_found (fail-closed)
        """
        checks_data = load_fixture("checks_all_pass.json")
        mock_fn = make_mock_run_gh(head_sha=HEAD_SHA, checks=checks_data)
        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "21",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA,
                                    "--check-name", "nonexistent"]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    exit_code = main()
        out = json.loads(buf.getvalue())
        assert exit_code == EXIT_GH_ERROR
        assert out["status"] == "gh_error"
        # check_not_found error should be present
        kinds = [e.get("kind") for e in out["errors"]]
        assert "check_not_found" in kinds

    def test_check_name_filter_multiple_match_returns_gh_error(self):
        """B1: GIVEN --check-name that matches multiple checks
        THEN status is gh_error with kind=ambiguous_check_name (fail-closed)
        """
        checks_data = load_fixture("checks_ambiguous.json")
        mock_fn = make_mock_run_gh(head_sha=HEAD_SHA, checks=checks_data)
        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "22",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA,
                                    "--check-name", "build"]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    exit_code = main()
        out = json.loads(buf.getvalue())
        assert exit_code == EXIT_GH_ERROR
        assert out["status"] == "gh_error"
        kinds = [e.get("kind") for e in out["errors"]]
        assert "ambiguous_check_name" in kinds


# ---------------------------------------------------------------------------
# AC: gh_error handling
# ---------------------------------------------------------------------------

class TestGhError:
    """AC12: gh コマンド失敗時の gh_error 処理テスト"""

    def test_gh_checks_auth_failure_returns_gh_error(self):
        """GIVEN gh pr checks returns auth error WHEN run THEN status gh_error"""
        def mock_fn(args):
            if "pr" in args and "view" in args and "headRefOid" in args:
                return True, {"headRefOid": HEAD_SHA}, json.dumps({"headRefOid": HEAD_SHA})
            if "pr" in args and "checks" in args:
                return False, None, "Error: authentication required (401)"
            return False, None, "unexpected"

        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "30",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    exit_code = main()
        assert exit_code == EXIT_GH_ERROR
        out = json.loads(buf.getvalue())
        assert out["status"] == "gh_error"
        assert len(out["errors"]) > 0

    def test_gh_pr_view_failure_returns_gh_error(self):
        """GIVEN gh pr view fails WHEN run THEN status gh_error"""
        def mock_fn(args):
            if "pr" in args and "view" in args:
                return False, None, "Error: not found (404)"
            return False, None, "unexpected"

        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "31",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    exit_code = main()
        assert exit_code == EXIT_GH_ERROR
        out = json.loads(buf.getvalue())
        assert out["status"] == "gh_error"

    def test_gh_error_has_error_kind_and_detail(self):
        """Error entries must have kind and detail fields"""
        def mock_fn(args):
            if "pr" in args and "view" in args and "headRefOid" in args:
                return False, None, "rate limit exceeded"
            return False, None, "unexpected"

        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "32",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    main()
        # We just check that main() doesn't raise and produces valid JSON
        # (error detail format is validated in the error entries check below)

    def test_errors_list_contains_kind_and_detail_keys(self):
        """GIVEN gh error WHEN output THEN errors[] items have kind and detail"""
        def mock_fn(args):
            if "pr" in args and "view" in args and "headRefOid" in args:
                return True, {"headRefOid": HEAD_SHA}, json.dumps({"headRefOid": HEAD_SHA})
            if "pr" in args and "checks" in args:
                return False, None, "permission denied (403)"
            return False, None, "unexpected"

        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "33",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    main()
        out = json.loads(buf.getvalue())
        assert len(out["errors"]) > 0
        for err in out["errors"]:
            assert "kind" in err
            assert "detail" in err


# ---------------------------------------------------------------------------
# B2: pass check の head_sha 補完テスト
# ---------------------------------------------------------------------------

class TestPassCheckHeadShaVerification:
    """B2: all_pass に寄与する check は run details で head SHA 確認"""

    def test_pass_check_with_matching_run_head_sha_is_all_pass(self):
        """GIVEN pass check and run head_sha matches expected
        WHEN run
        THEN status is all_pass
        """
        checks_data = [
            {
                "name": "build",
                "bucket": "pass",
                "state": "SUCCESS",
                "workflow": "CI",
                "link": "https://github.com/owner/repo/actions/runs/1001",
                "event": "push",
                "startedAt": None,
                "completedAt": None,
            }
        ]
        run_data = {
            "headSha": HEAD_SHA,
            "conclusion": "success",
            "status": "completed",
            "workflowName": "CI",
            "jobs": [{"databaseId": 1001, "name": "build", "conclusion": "success"}],
            "databaseId": 1001,
        }
        mock_fn = make_mock_run_gh(head_sha=HEAD_SHA, checks=checks_data, run_data=run_data)
        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "50",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    exit_code = main()
        assert exit_code == EXIT_ALL_PASS
        out = json.loads(buf.getvalue())
        assert out["status"] == "all_pass"

    def test_pass_check_with_stale_run_head_sha_is_stale(self):
        """GIVEN pass check but run head_sha is different from expected
        WHEN run
        THEN status is stale_head_sha (not all_pass)
        """
        checks_data = [
            {
                "name": "build",
                "bucket": "pass",
                "state": "SUCCESS",
                "workflow": "CI",
                "link": "https://github.com/owner/repo/actions/runs/1001",
                "event": "push",
                "startedAt": None,
                "completedAt": None,
            }
        ]
        run_data = {
            "headSha": STALE_SHA,  # Different from EXPECTED_SHA
            "conclusion": "success",
            "status": "completed",
            "workflowName": "CI",
            "jobs": [{"databaseId": 1001, "name": "build", "conclusion": "success"}],
            "databaseId": 1001,
        }
        mock_fn = make_mock_run_gh(head_sha=HEAD_SHA, checks=checks_data, run_data=run_data)
        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            with patch("sys.argv", ["ci_verdict_summary.py",
                                    "--pr", "51",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    exit_code = main()
        out = json.loads(buf.getvalue())
        # head_sha in run is STALE_SHA != HEAD_SHA → stale_head_sha
        assert out["status"] == "stale_head_sha"
        assert exit_code == EXIT_STALE


# ---------------------------------------------------------------------------
# B3: find_failed_job_id テスト
# ---------------------------------------------------------------------------

class TestFindFailedJobId:
    """B3: failed job を適切に特定する"""

    def test_find_failed_job_id_prefers_check_name_match(self):
        jobs = [
            {"databaseId": 1, "name": "lint", "conclusion": "success"},
            {"databaseId": 2, "name": "test", "conclusion": "failure"},
            {"databaseId": 3, "name": "build", "conclusion": "success"},
        ]
        result = find_failed_job_id(jobs, "test")
        assert result == 2

    def test_find_failed_job_id_falls_back_to_failed_conclusion(self):
        jobs = [
            {"databaseId": 1, "name": "lint", "conclusion": "success"},
            {"databaseId": 2, "name": "other-job", "conclusion": "failure"},
        ]
        result = find_failed_job_id(jobs, "nonexistent")
        assert result == 2

    def test_find_failed_job_id_falls_back_to_first_if_all_success(self):
        jobs = [
            {"databaseId": 1, "name": "lint", "conclusion": "success"},
            {"databaseId": 2, "name": "build", "conclusion": "success"},
        ]
        result = find_failed_job_id(jobs, None)
        assert result == 1

    def test_find_failed_job_id_returns_none_for_empty_jobs(self):
        result = find_failed_job_id([], "test")
        assert result is None


# ---------------------------------------------------------------------------
# B5: artifact path が repo-root 相対である確認
# ---------------------------------------------------------------------------

class TestArtifactPath:
    """B5: artifact path は repo-root 相対の文字列で返す"""

    def test_artifact_path_is_repo_root_relative(self):
        """save_log_artifact の path は artifacts/ で始まる相対パスを返す"""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            artifacts_base = repo_root / "artifacts"
            with patch("ci_verdict_summary.get_repo_root", return_value=repo_root):
                artifact_entry = save_log_artifact(
                    log_text="log content",
                    pr_number=42,
                    head_sha=HEAD_SHA,
                    check_name="build",
                    job_id=1,
                    artifacts_base=artifacts_base,
                )
        # path should be relative to repo_root (artifacts/ci-verdict/...)
        assert artifact_entry["path"].startswith("artifacts/")
        assert not artifact_entry["path"].startswith("/")


# ---------------------------------------------------------------------------
# B6: classify_gh_error 共通分類テスト
# ---------------------------------------------------------------------------

class TestClassifyGhError:
    """B6: 共通 gh エラー分類関数のテスト"""

    @pytest.mark.parametrize("stderr,expected_kind", [
        ("unauthorized: authentication credentials required", "auth_failed"),
        ("authentication failed", "auth_failed"),
        ("credentials not found", "auth_failed"),
        ("403 Forbidden: permission denied", "permission_denied"),
        ("error: permission denied", "permission_denied"),
        ("rate limit exceeded", "rate_limited"),
        ("429 too many requests", "rate_limited"),
        ("404 not found", "not_found"),
        ("resource not found", "not_found"),
        ("json decode error", "json_parse_error"),
        ("parse error in response", "json_parse_error"),
        ("some unknown error xyz", "gh_other_error"),
    ])
    def test_classify_gh_error_classification(self, stderr: str, expected_kind: str):
        assert classify_gh_error(stderr) == expected_kind

    def test_fetch_head_sha_uses_classify_gh_error(self):
        """B6: fetch_head_sha が共通分類を使うことを確認"""
        def mock_fn(args):
            if "pr" in args and "view" in args:
                return False, None, "rate limit exceeded"
            return False, None, "unexpected"

        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            from ci_verdict_summary import fetch_head_sha
            _, err = fetch_head_sha(42, "owner/repo")
        assert err is not None
        assert err["kind"] == "rate_limited"

    def test_fetch_checks_uses_classify_gh_error(self):
        """B6: fetch_checks が共通分類を使うことを確認"""
        def mock_fn(args):
            if "pr" in args and "checks" in args:
                return False, None, "403 permission denied"
            return False, None, "unexpected"

        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            from ci_verdict_summary import fetch_checks
            _, err = fetch_checks(42, "owner/repo")
        assert err is not None
        assert err["kind"] == "permission_denied"

    def test_fetch_run_details_uses_classify_gh_error(self):
        """B6: fetch_run_details が共通分類を使うことを確認"""
        def mock_fn(args):
            if "run" in args and "view" in args:
                return False, None, "404 not found"
            return False, None, "unexpected"

        with patch("ci_verdict_summary.run_gh", side_effect=mock_fn):
            from ci_verdict_summary import fetch_run_details
            _, err = fetch_run_details(9999, "owner/repo")
        assert err is not None
        assert err["kind"] == "not_found"


# ---------------------------------------------------------------------------
# Utility: extract_run_id_from_link
# ---------------------------------------------------------------------------

class TestExtractRunId:
    def test_extracts_run_id_from_actions_url(self):
        url = "https://github.com/owner/repo/actions/runs/12345"
        assert extract_run_id_from_link(url) == 12345

    def test_returns_none_for_non_actions_url(self):
        assert extract_run_id_from_link("https://example.com/other") is None

    def test_returns_none_for_empty_string(self):
        assert extract_run_id_from_link("") is None

    def test_returns_none_for_none(self):
        assert extract_run_id_from_link(None) is None
