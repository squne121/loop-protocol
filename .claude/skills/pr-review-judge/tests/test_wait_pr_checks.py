import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(
    0,
    str(
        Path(
            "/home/squne/projects/LOOP_PROTOCOL/.claude/worktrees/issue-1055-ci-check-wait-normalizer-pending-polling"
        )
        / ".claude/skills/pr-review-judge/scripts"
    ),
)

from wait_pr_checks import main

REPO = "owner/repo"
PR = 123
EXPECTED_SHA = "expected-head-sha"
DRIFT_SHA = "drifted-head-sha"


def run_main(mock_calls: list[tuple[int, str, str]], *, monotonic_values: list[float] | None = None):
    call_index = 0

    def fake_run_gh(_args: list[str]):
        nonlocal call_index
        result = mock_calls[call_index]
        call_index += 1
        return result

    monotonic_iter = iter(monotonic_values or [0.0, 0.0, 1.0, 1.0, 2.0])

    with patch("wait_pr_checks.run_gh", side_effect=fake_run_gh):
        with patch("wait_pr_checks.time.sleep", return_value=None):
            with patch("wait_pr_checks.time.monotonic", side_effect=lambda: next(monotonic_iter)):
                with patch(
                    "sys.argv",
                    [
                        "wait_pr_checks.py",
                        "--repo",
                        REPO,
                        "--pr",
                        str(PR),
                        "--expected-head-sha",
                        EXPECTED_SHA,
                        "--interval-seconds",
                        "1",
                        "--timeout-seconds",
                        "5",
                    ],
                ):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = main()
    return exit_code, json.loads(output.getvalue())


def test_pr_checks_wait_result_v1_contains_required_fields():
    exit_code, payload = run_main(
        [
            (0, EXPECTED_SHA, ""),
            (0, EXPECTED_SHA, ""),
            (0, json.dumps([{"name": "python-test", "workflow": "CI", "bucket": "pass", "state": "completed"}]), ""),
        ]
    )

    assert exit_code == 0
    assert payload["schema"] == "PR_CHECKS_WAIT_RESULT_V1"
    assert payload["decision"] == "pass"
    assert payload["pending_count"] == 0
    assert payload["failed_blocking_count"] == 0
    assert payload["expected_head_sha"] == EXPECTED_SHA
    assert payload["current_head_sha"] == EXPECTED_SHA
    assert payload["timed_out"] is False


def test_pending_then_pass():
    exit_code, payload = run_main(
        [
            (0, EXPECTED_SHA, ""),
            (0, EXPECTED_SHA, ""),
            (0, json.dumps([{"name": "python-test", "workflow": "CI", "bucket": "pending", "state": "in_progress"}]), ""),
            (0, EXPECTED_SHA, ""),
            (0, json.dumps([{"name": "python-test", "workflow": "CI", "bucket": "pass", "state": "completed"}]), ""),
        ]
    )

    assert exit_code == 0
    assert payload["decision"] == "pass"
    assert payload["checks"][0]["bucket"] == "pass"


def test_pending_then_fail():
    exit_code, payload = run_main(
        [
            (0, EXPECTED_SHA, ""),
            (0, EXPECTED_SHA, ""),
            (0, json.dumps([{"name": "python-test", "workflow": "CI", "bucket": "pending", "state": "in_progress"}]), ""),
            (0, EXPECTED_SHA, ""),
            (0, json.dumps([{"name": "python-test", "workflow": "CI", "bucket": "fail", "state": "completed"}]), ""),
        ]
    )

    assert exit_code == 1
    assert payload["decision"] == "failed_blocking"
    assert payload["failed_blocking_count"] == 1


def test_timeout_returns_human_judgment_and_preserves_last_checks():
    exit_code, payload = run_main(
        [
            (0, EXPECTED_SHA, ""),
            (0, EXPECTED_SHA, ""),
            (0, json.dumps([{"name": "python-test", "workflow": "CI", "bucket": "pending", "state": "in_progress"}]), ""),
        ],
        monotonic_values=[0.0, 0.0, 1.0, 5.0],
    )

    assert exit_code == 1
    assert payload["decision"] == "human_judgment"
    assert payload["timed_out"] is True
    assert payload["checks"][0]["bucket"] == "pending"
    assert payload["pending_count"] == 1


def test_head_sha_changed():
    exit_code, payload = run_main(
        [
            (0, EXPECTED_SHA, ""),
            (0, EXPECTED_SHA, ""),
            (
                0,
                json.dumps(
                    [{"name": "python-test", "workflow": "CI", "bucket": "pending", "state": "in_progress"}]
                ),
                "",
            ),
            (0, DRIFT_SHA, ""),
        ]
    )

    assert exit_code == 1
    assert payload["decision"] == "stale_head_sha"
    assert payload["current_head_sha"] == DRIFT_SHA


def test_no_required_evidence():
    exit_code, payload = run_main(
        [
            (0, EXPECTED_SHA, ""),
            (0, EXPECTED_SHA, ""),
            (0, json.dumps([]), ""),
        ]
    )

    assert exit_code == 1
    assert payload["decision"] == "no_required_evidence"
    assert payload["checks"] == []


def test_non_blocking_exact_match():
    exit_code, payload = run_main(
        [
            (0, EXPECTED_SHA, ""),
            (0, EXPECTED_SHA, ""),
            (
                0,
                json.dumps(
                    [{"name": "deploy-pr", "workflow": "deploy-pages", "bucket": "pass", "state": "completed"}]
                ),
                "",
            ),
        ]
    )

    assert exit_code == 0
    assert payload["checks"][0]["blocking"] is False
    assert payload["checks"][0]["non_blocking_reason"] == "configured_exact_match"
