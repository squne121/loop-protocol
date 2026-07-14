"""
Unit tests for ci_verdict_summary.py

Tests AC1-AC12: 4-state verdict (pass/fail/pending/stale),
bucket mapping, conclusion mapping, stdout schema, log_excerpt,
sanitize_check_name, check-name filter, gh_error handling.

Fixtures are JSON files under tests/fixtures/ci_verdict/.
"""

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest

# Ensure scripts/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from ci_verdict_summary import (
    EXIT_ALL_PASS,
    EXIT_FAILED,
    EXIT_GH_ERROR,
    EXIT_NO_REQUIRED_EVIDENCE,
    EXIT_PENDING,
    EXIT_STALE,
    HEAD_SHA_NULL_SKIPPED_EXCLUDE_RULES,
    classify_check,
    classify_gh_error,
    compute_overall_status,
    determine_check_verdict,
    extract_run_id_from_link,
    fetch_checks,
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

# GitHub GraphQL statusCheckRollup の実 API 応答形を縮小保存した golden fixture。
STATUS_CHECK_ROLLUP_GOLDEN = {
    "data": {"repository": {"pullRequest": {"commits": {"nodes": [{"commit": {
        "oid": HEAD_SHA,
        "statusCheckRollup": {"contexts": {"pageInfo": {"hasNextPage": False}, "nodes": [
            {
                "__typename": "CheckRun", "databaseId": 86953783527,
                "name": "PR Body Japanese Check", "status": "COMPLETED",
                "conclusion": "SUCCESS", "startedAt": "2026-07-13T22:44:01Z",
                "completedAt": "2026-07-13T22:44:08Z",
                "detailsUrl": "https://github.com/squne121/loop-protocol/actions/runs/29290822289/job/86953783527",
                "checkSuite": {"commit": {"oid": HEAD_SHA}, "workflowRun": {
                    "event": "pull_request", "workflow": {"name": "Check Japanese Content"},
                }},
            },
            {
                "__typename": "CheckRun", "databaseId": 86955636256,
                "name": "PR Body Japanese Check", "status": "COMPLETED",
                "conclusion": "SKIPPED", "startedAt": "2026-07-13T22:55:08Z",
                "completedAt": "2026-07-13T22:55:08Z",
                "detailsUrl": "https://github.com/squne121/loop-protocol/actions/runs/29291421456/job/86955636256",
                "checkSuite": {"commit": {"oid": HEAD_SHA}, "workflowRun": {
                    "event": "pull_request_review", "workflow": {"name": "Check Japanese Content"},
                }},
            },
        ]}},
    }}]}}}}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_fixture(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text())


def status_check_rollup_response(
    checks: list[dict],
    *,
    head_sha: str,
    runs: Optional[dict[int, dict]] = None,
    default_run: Optional[dict] = None,
) -> dict:
    """GraphQL statusCheckRollup の実 API 形を返す test fixture adapter。"""
    nodes = []
    for index, check in enumerate(checks, start=1):
        direct_run_id = check.get("runId") or extract_run_id_from_link(check.get("link") or "")
        run_id = int(direct_run_id or index)
        run = (runs or {}).get(run_id, default_run or {}) if direct_run_id else {}
        conclusion = run.get("conclusion") or (check.get("state") or "").lower()
        status = run.get("status") or ("completed" if check.get("bucket") != "pending" else "in_progress")
        run_head = run.get("headSha", head_sha)
        nodes.append({
            "__typename": "CheckRun",
            "databaseId": run_id,
            "name": check.get("name"),
            "status": status.upper(),
            "conclusion": conclusion.upper(),
            "startedAt": check.get("startedAt"),
            "completedAt": check.get("completedAt"),
            "detailsUrl": check.get("link"),
            "checkSuite": {
                "commit": {"oid": run_head},
                "workflowRun": {
                    "event": check.get("event"),
                    "workflow": {"name": check.get("workflow")},
                },
            },
        })
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "commits": {
                        "nodes": [{
                            "commit": {
                                "oid": head_sha,
                                "statusCheckRollup": {
                                    "contexts": {"pageInfo": {"hasNextPage": False}, "nodes": nodes},
                                },
                            },
                        }],
                    },
                },
            },
        },
    }


def make_mock_run_gh(
    head_sha: str = HEAD_SHA,
    checks: Optional[list] = None,
    checks_ok: bool = True,
    checks_err_msg: str = "",
    run_data: Optional[dict] = None,
):
    """
    run_gh をモックする。
    呼び出し順: pr view → GraphQL statusCheckRollup
    """
    call_count = 0
    checks_list = checks if checks is not None else []

    def _run_gh(args: list[str]):
        nonlocal call_count
        call_count += 1

        if "pr" in args and "view" in args and "headRefOid" in args:
            return True, {"headRefOid": head_sha}, json.dumps({"headRefOid": head_sha})
        if "api" in args and "graphql" in args:
            if not checks_ok:
                return False, None, checks_err_msg
            payload = status_check_rollup_response(
                checks_list, head_sha=head_sha, default_run=run_data,
            )
            return True, payload, json.dumps(payload)
        if "run" in args and "view" in args and "--log" not in args:
            if run_data is not None:
                return True, run_data, json.dumps(run_data)
            return True, {}, "{}"
        if "run" in args and "view" in args and "--log" in args:
            return True, None, "fake log line 1\nfake log line 2"
        return False, None, "unexpected gh call"

    return _run_gh


def run_summary_with_details(checks: list[dict], runs: dict[int, dict], check_name: Optional[str] = None):
    """event/state provenance を含む integration fixture を main() 経由で集約する。"""
    def _run_gh(args: list[str]):
        if "pr" in args and "view" in args and "headRefOid" in args:
            return True, {"headRefOid": HEAD_SHA}, "{}"
        if "api" in args and "graphql" in args:
            payload = status_check_rollup_response(checks, head_sha=HEAD_SHA, runs=runs)
            return True, payload, json.dumps(payload)
        return False, None, "unexpected gh call"

    argv = [
        "ci_verdict_summary.py", "--pr", "1456", "--repo", "owner/repo",
        "--expected-head-sha", HEAD_SHA,
    ]
    if check_name:
        argv.extend(["--check-name", check_name])
    import io
    from contextlib import redirect_stdout
    with patch("ci_verdict_summary.run_gh", side_effect=_run_gh), patch("sys.argv", argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = main()
    return exit_code, json.loads(buf.getvalue())


def provenance_check(
    run_id: int,
    *,
    bucket: str,
    state: str,
    event: Optional[str],
    completed_at: str,
) -> dict:
    return {
        "name": "PR Body Japanese Check",
        "bucket": bucket,
        "state": state,
        "workflow": "Check Japanese Content",
        "link": f"https://github.com/owner/repo/actions/runs/{run_id}",
        "event": event,
        "startedAt": completed_at,
        "completedAt": completed_at,
    }


def completed_run(conclusion: str, head_sha: str = HEAD_SHA) -> dict:
    return {
        "headSha": head_sha,
        "conclusion": conclusion,
        "status": "completed",
        "workflowName": "Check Japanese Content",
        "jobs": [],
        "databaseId": 1,
    }


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
                "startedAt": "2026-07-14T00:00:00Z",
                "completedAt": "2026-07-14T00:00:01Z",
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
        assert "pr-99" in artifact_entry["path"]
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
                "startedAt": "2026-07-14T00:00:00Z",
                "completedAt": "2026-07-14T00:00:01Z",
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
            if "api" in args and "graphql" in args:
                payload = status_check_rollup_response(
                    checks_data, head_sha=HEAD_SHA, default_run=run_data,
                )
                return True, payload, json.dumps(payload)
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
            if "api" in args and "graphql" in args:
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
            if "api" in args and "graphql" in args:
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
                "startedAt": "2026-07-14T00:00:00Z",
                "completedAt": "2026-07-14T00:00:01Z",
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
                "startedAt": "2026-07-14T00:00:00Z",
                "completedAt": "2026-07-14T00:00:01Z",
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
            if "api" in args and "graphql" in args:
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


# ---------------------------------------------------------------------------
# AC1/AC3/AC4/AC5: head_sha=null skipped allowlist (Issue #863)
# ---------------------------------------------------------------------------

class TestHeadShaNullSkippedExclude:
    """AC1/AC3/AC4/AC5: head_sha=None かつ conclusion=skipped の allowlist 除外テスト"""

    def test_allowlist_contains_expected_entries(self):
        """AC4: HEAD_SHA_NULL_SKIPPED_EXCLUDE_RULES に必須エントリが含まれる"""
        assert ("deploy-pages", "deploy-main") in HEAD_SHA_NULL_SKIPPED_EXCLUDE_RULES
        assert ("deploy-pages", "cleanup-pr") in HEAD_SHA_NULL_SKIPPED_EXCLUDE_RULES
        assert (
            "Check Japanese Content",
            "Issue Body Japanese Check (retrospective)",
        ) in HEAD_SHA_NULL_SKIPPED_EXCLUDE_RULES
        assert (
            "Check Japanese Content",
            "Issue Comment Japanese Check (retrospective)",
        ) in HEAD_SHA_NULL_SKIPPED_EXCLUDE_RULES
        assert (
            "Check Japanese Content",
            "PR Review Japanese Check (retrospective)",
        ) in HEAD_SHA_NULL_SKIPPED_EXCLUDE_RULES
        assert ("agent-retro-index", "build-index") in HEAD_SHA_NULL_SKIPPED_EXCLUDE_RULES
        assert (
            "agent-retro-index",
            "upsert-parent-comment",
        ) in HEAD_SHA_NULL_SKIPPED_EXCLUDE_RULES

    def test_head_sha_null_skipped_deploy_main_is_excluded(self):
        """AC1: deploy-main の head_sha=None かつ conclusion=skipped は excluded"""
        entry = {
            "head_sha": None,
            "conclusion": "skipped",
            "name": "deploy-main",
            "workflow": "deploy-pages",
            "bucket": "skipping",
            "status": "completed",
        }
        assert determine_check_verdict(entry, HEAD_SHA) == "excluded"

    def test_head_sha_null_skipped_cleanup_pr_is_excluded(self):
        """AC1/AC3: cleanup-pr の head_sha=None かつ conclusion=skipped は excluded"""
        entry = {
            "head_sha": None,
            "conclusion": "skipped",
            "name": "cleanup-pr",
            "workflow": "deploy-pages",
            "bucket": "skipping",
            "status": "completed",
        }
        assert determine_check_verdict(entry, HEAD_SHA) == "excluded"

    def test_head_sha_null_skipped_agent_retro_index_build_index_is_excluded(self):
        """AC11: agent-retro-index の build-index の head_sha=None かつ conclusion=skipped は excluded"""
        entry = {
            "head_sha": None,
            "conclusion": "skipped",
            "name": "build-index",
            "workflow": "agent-retro-index",
            "bucket": "skipping",
            "status": "completed",
        }
        assert determine_check_verdict(entry, HEAD_SHA) == "excluded"

    def test_head_sha_null_skipped_agent_retro_index_upsert_parent_comment_is_excluded(self):
        """AC11: agent-retro-index の upsert-parent-comment の head_sha=None かつ conclusion=skipped は excluded"""
        entry = {
            "head_sha": None,
            "conclusion": "skipped",
            "name": "upsert-parent-comment",
            "workflow": "agent-retro-index",
            "bucket": "skipping",
            "status": "completed",
        }
        assert determine_check_verdict(entry, HEAD_SHA) == "excluded"

    def test_head_sha_null_skipped_issue_body_japanese_is_excluded(self):
        """AC1/AC3: Issue Body Japanese Check (retrospective) は excluded"""
        entry = {
            "head_sha": None,
            "conclusion": "skipped",
            "name": "Issue Body Japanese Check (retrospective)",
            "workflow": "Check Japanese Content",
            "bucket": "skipping",
            "status": "completed",
        }
        assert determine_check_verdict(entry, HEAD_SHA) == "excluded"

    def test_head_sha_null_skipped_issue_comment_japanese_is_excluded(self):
        """AC1/AC3: Issue Comment Japanese Check (retrospective) は excluded"""
        entry = {
            "head_sha": None,
            "conclusion": "skipped",
            "name": "Issue Comment Japanese Check (retrospective)",
            "workflow": "Check Japanese Content",
            "bucket": "skipping",
            "status": "completed",
        }
        assert determine_check_verdict(entry, HEAD_SHA) == "excluded"

    def test_head_sha_null_skipped_pr_review_japanese_is_excluded(self):
        """AC1/AC3: PR Review Japanese Check (retrospective) は excluded"""
        entry = {
            "head_sha": None,
            "conclusion": "skipped",
            "name": "PR Review Japanese Check (retrospective)",
            "workflow": "Check Japanese Content",
            "bucket": "skipping",
            "status": "completed",
        }
        assert determine_check_verdict(entry, HEAD_SHA) == "excluded"

    @pytest.mark.parametrize("required_job_name", [
        "typecheck",
        "lint",
        "test",
        "build",
        "e2e",
        "python-test",
        "actionlint",
        "PR Body Japanese Check",
    ])
    def test_required_job_null_skipped_stays_failed(self, required_job_name: str):
        """AC5: required job の head_sha=None かつ conclusion=skipped は failed のまま"""
        entry = {
            "head_sha": None,
            "conclusion": "skipped",
            "name": required_job_name,
            "bucket": "skipping",
            "status": "completed",
        }
        assert determine_check_verdict(entry, HEAD_SHA) == "failed"

    def test_excluded_does_not_affect_overall_status(self):
        """AC3: excluded verdict は overall status を failed にしない（all_pass を維持）"""
        verdicts = ["all_pass", "excluded", "excluded"]
        assert compute_overall_status(verdicts) == "all_pass"

    def test_excluded_with_all_pass_checks_yields_all_pass(self):
        """AC3: all_pass checks + excluded checks → overall all_pass"""
        verdicts = ["all_pass", "all_pass", "excluded"]
        assert compute_overall_status(verdicts) == "all_pass"

    def test_excluded_only_yields_no_required_evidence(self):
        """excluded のみの場合は no_required_evidence（all_pass にしない）"""
        verdicts = ["excluded", "excluded"]
        assert compute_overall_status(verdicts) == "no_required_evidence"

    def test_excluded_only_single_yields_no_required_evidence(self):
        """excluded が 1 件のみの場合も no_required_evidence"""
        verdicts = ["excluded"]
        assert compute_overall_status(verdicts) == "no_required_evidence"

    def test_excluded_only_exit_code_is_10(self):
        """excluded のみ → no_required_evidence → exit 10（failed 扱い）"""
        # compute_overall_status で no_required_evidence が返ることを確認
        result = compute_overall_status(["excluded"])
        assert result == "no_required_evidence"
        # EXIT_NO_REQUIRED_EVIDENCE = 10 (same as EXIT_FAILED)
        assert EXIT_NO_REQUIRED_EVIDENCE == EXIT_FAILED

    def test_excluded_does_not_mask_failed(self):
        """excluded があっても failed は failed のまま"""
        verdicts = ["failed", "excluded"]
        assert compute_overall_status(verdicts) == "failed"

    def test_non_null_head_sha_skipped_is_not_excluded(self):
        """head_sha が non-None の skipped は allowlist に関わらず通常判定"""
        entry = {
            "head_sha": HEAD_SHA,  # non-None
            "conclusion": "skipped",
            "name": "deploy-main",  # allowlist に含まれるが head_sha が non-None
            "workflow": "deploy-pages",
            "bucket": "skipping",
            "status": "completed",
        }
        # head_sha が PR head SHA と一致するので stale ではない → conclusion=skipped → failed
        assert determine_check_verdict(entry, HEAD_SHA) == "failed"

    def test_head_sha_null_non_skipped_conclusion_not_excluded(self):
        """head_sha=None でも conclusion != skipped ならば excluded にならない"""
        entry = {
            "head_sha": None,
            "conclusion": "failure",
            "name": "deploy-main",
            "workflow": "deploy-pages",
            "bucket": "fail",
            "status": "completed",
        }
        assert determine_check_verdict(entry, HEAD_SHA) == "failed"

    def test_deploy_main_wrong_workflow_not_excluded(self):
        """AC Blocker2: name=deploy-main だが workflow=ci → excluded にならず failed"""
        entry = {
            "head_sha": None,
            "conclusion": "skipped",
            "name": "deploy-main",
            "workflow": "ci",  # workflow mismatch — deploy-pages ではない
            "bucket": "skipping",
            "status": "completed",
        }
        assert determine_check_verdict(entry, HEAD_SHA) == "failed"

    def test_summary_has_excluded_checks_fields(self):
        """Blocker3: summary に excluded_checks と excluded_count が含まれる"""
        checks_data = [
            {
                "name": "deploy-main",
                "bucket": "skipping",
                "state": "SKIPPED",
                "workflow": "deploy-pages",
                "link": None,
                "event": "push",
                "startedAt": None,
                "completedAt": None,
            },
            {
                "name": "build",
                "bucket": "pass",
                "state": "SUCCESS",
                "workflow": "CI",
                "link": "https://github.com/owner/repo/actions/runs/1001",
                "event": "push",
                "startedAt": "2026-07-14T00:00:00Z",
                "completedAt": "2026-07-14T00:00:01Z",
            },
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
                                    "--pr", "99",
                                    "--repo", "owner/repo",
                                    "--expected-head-sha", HEAD_SHA]):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    _exit_code = main()
        out = json.loads(buf.getvalue())
        assert "excluded_checks" in out
        assert "excluded_count" in out
        assert "deploy-main" in out["excluded_checks"]
        assert out["excluded_count"] == 1


# ---------------------------------------------------------------------------
# Issue #1456: event-aware rerun canonicalization and review skipped shadows
# ---------------------------------------------------------------------------

class TestReviewShadowAndRerunCanonicalization:
    """GIVEN provenance-complete checks WHEN summarized THEN only proven shadows exclude."""

    def test_status_check_rollup_golden_fixture_has_direct_run_identity(self):
        """GIVEN actual GraphQL form WHEN fetched THEN it retains ID/event/head/timestamps."""
        with patch("ci_verdict_summary.run_gh", return_value=(True, STATUS_CHECK_ROLLUP_GOLDEN, "{}")):
            checks, error = fetch_checks(1505, "squne121/loop-protocol")
        assert error is None
        assert checks is not None
        assert checks[0]["runId"] == 86953783527
        assert checks[0]["event"] == "pull_request"
        assert checks[0]["headSha"] == HEAD_SHA
        assert checks[0]["completedAt"] == "2026-07-13T22:44:08Z"

    def _peer_and_shadow(self, event: str = "pull_request_review") -> tuple[list[dict], dict[int, dict]]:
        checks = [
            provenance_check(
                101, bucket="pass", state="SUCCESS", event="pull_request",
                completed_at="2026-07-14T00:00:01Z",
            ),
            provenance_check(102, bucket="skipping", state="SKIPPED", event=event, completed_at="2026-07-14T00:00:02Z"),
        ]
        return checks, {101: completed_run("success"), 102: completed_run("skipped")}

    @pytest.mark.parametrize("event", ["pull_request_review", "pull_request_review_comment"])
    def test_exact_review_skipped_shadow_is_excluded_only_with_current_head_success_peer(self, event: str):
        checks, runs = self._peer_and_shadow(event)
        exit_code, out = run_summary_with_details(checks, runs, "PR Body Japanese Check")
        assert exit_code == EXIT_ALL_PASS
        assert out["status"] == "all_pass"
        assert out["excluded_checks"] == ["PR Body Japanese Check"]
        assert all("event" not in entry for entry in out["checks"])

    @pytest.mark.parametrize("peer_head", [None, STALE_SHA])
    def test_missing_or_stale_peer_keeps_shadow_failed(self, peer_head: Optional[str]):
        checks, runs = self._peer_and_shadow()
        if peer_head is None:
            checks = checks[1:]
            runs = {102: runs[102]}
        else:
            runs[101] = completed_run("success", peer_head)
        _, out = run_summary_with_details(checks, runs)
        assert out["status"] in {"failed", "stale_head_sha"}
        assert out["excluded_checks"] == []

    @pytest.mark.parametrize("state,event", [("NEUTRAL", "pull_request_review"), ("SKIPPED", None)])
    def test_neutral_or_missing_event_is_not_excluded(self, state: str, event: Optional[str]):
        checks, runs = self._peer_and_shadow()
        checks[1]["state"] = state
        checks[1]["event"] = event
        runs[102]["conclusion"] = state.lower()
        _, out = run_summary_with_details(checks, runs)
        assert out["status"] == "failed"
        assert out["excluded_checks"] == []

    def test_reversed_input_has_same_verdict_and_buckets(self):
        checks, runs = self._peer_and_shadow()
        _, normal = run_summary_with_details(checks, runs)
        _, reversed_out = run_summary_with_details(list(reversed(checks)), runs)
        assert (normal["status"], normal["failed_checks"], normal["excluded_checks"]) == (
            reversed_out["status"], reversed_out["failed_checks"], reversed_out["excluded_checks"]
        )

    def test_true_rerun_uses_latest_same_event_without_removing_cross_event_shadow(self):
        checks, runs = self._peer_and_shadow()
        checks.insert(
            0,
            provenance_check(
                100, bucket="fail", state="FAILURE", event="pull_request",
                completed_at="2026-07-14T00:00:00Z",
            ),
        )
        runs[100] = completed_run("failure")
        exit_code, out = run_summary_with_details(checks, runs, "PR Body Japanese Check")
        assert exit_code == EXIT_ALL_PASS
        assert out["status"] == "all_pass"
        assert len(out["checks"]) == 2
        assert out["excluded_count"] == 1

    def test_current_success_and_newer_stale_failure_are_not_canonicalized_together(self):
        checks = [
            provenance_check(
                100, bucket="pass", state="SUCCESS", event="pull_request", completed_at="2026-07-14T00:00:01Z"
            ),
            provenance_check(
                101, bucket="fail", state="FAILURE", event="pull_request", completed_at="2026-07-14T00:00:02Z"
            ),
        ]
        _, out = run_summary_with_details(
            checks,
            {100: completed_run("success"), 101: completed_run("failure", STALE_SHA)},
        )
        assert out["status"] == "stale_head_sha"
        assert len(out["checks"]) == 2

    def test_current_failure_and_newer_stale_success_preserve_current_failure(self):
        checks = [
            provenance_check(
                100, bucket="fail", state="FAILURE", event="pull_request", completed_at="2026-07-14T00:00:01Z"
            ),
            provenance_check(
                101, bucket="pass", state="SUCCESS", event="pull_request", completed_at="2026-07-14T00:00:02Z"
            ),
        ]
        _, out = run_summary_with_details(
            checks,
            {100: completed_run("failure"), 101: completed_run("success", STALE_SHA)},
        )
        assert out["status"] == "stale_head_sha"
        assert out["failed_checks"] == ["PR Body Japanese Check"]

    def test_missing_head_sha_is_not_deduplicated_or_accepted_as_success(self):
        checks = [
            provenance_check(
                100, bucket="fail", state="FAILURE", event="pull_request", completed_at="2026-07-14T00:00:01Z"
            ),
            provenance_check(
                101, bucket="pass", state="SUCCESS", event="pull_request", completed_at="2026-07-14T00:00:02Z"
            ),
        ]
        _, out = run_summary_with_details(
            checks,
            {100: completed_run("failure"), 101: completed_run("success", None)},
        )
        assert out["status"] == "pending_or_queued"
        assert len(out["checks"]) == 2

    def test_same_head_old_failure_new_success_is_canonicalized(self):
        checks = [
            provenance_check(
                100, bucket="fail", state="FAILURE", event="pull_request", completed_at="2026-07-14T00:00:01Z"
            ),
            provenance_check(
                101, bucket="pass", state="SUCCESS", event="pull_request", completed_at="2026-07-14T00:00:02Z"
            ),
        ]
        exit_code, out = run_summary_with_details(
            checks,
            {100: completed_run("failure"), 101: completed_run("success")},
        )
        assert exit_code == EXIT_ALL_PASS
        assert out["status"] == "all_pass"
        assert len(out["checks"]) == 1

    @pytest.mark.parametrize("field,value", [("workflow", "wrong workflow"), ("name", "wrong name")])
    def test_wrong_review_shadow_tuple_is_not_excluded(self, field: str, value: str):
        checks, runs = self._peer_and_shadow()
        checks[1][field] = value
        _, out = run_summary_with_details(checks, runs)
        assert out["status"] == "failed"
        assert out["excluded_checks"] == []

    @pytest.mark.parametrize("incomplete_run", [101, 102])
    def test_shadow_or_peer_incomplete_direct_detail_is_fail_closed(self, incomplete_run: int):
        checks, runs = self._peer_and_shadow()
        runs[incomplete_run]["headSha"] = None
        _, out = run_summary_with_details(checks, runs)
        assert out["status"] == "pending_or_queued"
        assert out["excluded_checks"] == []

    @pytest.mark.parametrize(
        "order",
        [(100, 101, 102), (102, 100, 101), (101, 102, 100), (102, 101, 100)],
    )
    def test_failure_success_shadow_three_element_order_is_stable(self, order: tuple[int, int, int]):
        checks = {
            100: provenance_check(
                100, bucket="fail", state="FAILURE", event="pull_request", completed_at="2026-07-14T00:00:01Z"
            ),
            101: provenance_check(
                101, bucket="pass", state="SUCCESS", event="pull_request", completed_at="2026-07-14T00:00:02Z"
            ),
            102: provenance_check(
                102,
                bucket="skipping",
                state="SKIPPED",
                event="pull_request_review",
                completed_at="2026-07-14T00:00:03Z",
            ),
        }
        _, out = run_summary_with_details(
            [checks[run_id] for run_id in order],
            {100: completed_run("failure"), 101: completed_run("success"), 102: completed_run("skipped")},
        )
        assert out["status"] == "all_pass"
        assert out["excluded_checks"] == ["PR Body Japanese Check"]

    def test_same_timestamp_different_run_ids_is_not_deduplicated(self):
        checks = [
            provenance_check(
                100, bucket="fail", state="FAILURE", event="pull_request", completed_at="2026-07-14T00:00:01Z"
            ),
            provenance_check(
                101, bucket="pass", state="SUCCESS", event="pull_request", completed_at="2026-07-14T00:00:01Z"
            ),
        ]
        _, out = run_summary_with_details(
            checks,
            {100: completed_run("failure"), 101: completed_run("success")},
        )
        assert out["status"] == "failed"
        assert len(out["checks"]) == 2

    def test_ambiguous_rerun_timestamp_is_not_deduped_to_success(self):
        checks = [
            provenance_check(100, bucket="fail", state="FAILURE", event="pull_request", completed_at="not-a-timestamp"),
            provenance_check(101, bucket="pass", state="SUCCESS", event="pull_request", completed_at="not-a-timestamp"),
        ]
        _, out = run_summary_with_details(checks, {100: completed_run("failure"), 101: completed_run("success")})
        assert out["status"] == "failed"
        assert out["failed_checks"] == ["PR Body Japanese Check"]

    def test_run_detail_head_does_not_erase_retrospective_skip_provenance(self):
        """GIVEN an allowlisted retrospective skip WHEN detail is completed THEN it stays excluded."""
        checks = [
            {
                "name": "PR Body Japanese Check",
                "bucket": "pass",
                "state": "SUCCESS",
                "workflow": "Check Japanese Content",
                "link": "https://github.com/owner/repo/actions/runs/201",
                "event": "pull_request",
                "startedAt": "2026-07-14T00:00:01Z",
                "completedAt": "2026-07-14T00:00:01Z",
            },
            {
                "name": "PR Review Japanese Check (retrospective)",
                "bucket": "skipping",
                "state": "SKIPPED",
                "workflow": "Check Japanese Content",
                "link": "https://github.com/owner/repo/actions/runs/202",
                "event": "pull_request_review",
                "startedAt": "2026-07-14T00:00:02Z",
                "completedAt": "2026-07-14T00:00:02Z",
            },
        ]
        runs = {201: completed_run("success"), 202: completed_run("skipped")}
        exit_code, out = run_summary_with_details(checks, runs)
        assert exit_code == EXIT_ALL_PASS
        assert out["excluded_checks"] == ["PR Review Japanese Check (retrospective)"]
        retrospective = next(
            entry for entry in out["checks"]
            if entry["name"] == "PR Review Japanese Check (retrospective)"
        )
        assert retrospective["head_sha"] is None
