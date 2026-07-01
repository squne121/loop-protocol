import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT_PATH = SCRIPT_DIR / "wait_pr_checks.py"
SPEC = importlib.util.spec_from_file_location("wait_pr_checks", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
main = MODULE.main

REPO = "owner/repo"
PR = 123
EXPECTED_SHA = "expected-head-sha"
DRIFT_SHA = "drifted-head-sha"


def check_payload(name: str, workflow: str, bucket: str, state: str) -> str:
    return json.dumps([{"name": name, "workflow": workflow, "bucket": bucket, "state": state}])


def run_main(mock_calls: list[tuple[int, str, str]], *, monotonic_values: list[float] | None = None):
    call_index = 0

    def fake_run_gh(_args: list[str]):
        nonlocal call_index
        result = mock_calls[call_index]
        call_index += 1
        return result

    monotonic_iter = iter(monotonic_values or [0.0, 0.0, 1.0, 1.0, 2.0])

    with patch.object(MODULE, "run_gh", side_effect=fake_run_gh):
        with patch.object(MODULE.time, "sleep", return_value=None):
            with patch.object(MODULE.time, "monotonic", side_effect=lambda: next(monotonic_iter)):
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


def test_imports_wait_pr_checks_from_current_checkout():
    assert SCRIPT_PATH.resolve() == Path(MODULE.__file__).resolve()


def test_pr_checks_wait_result_v1_contains_required_fields():
    exit_code, payload = run_main(
        [
            (0, EXPECTED_SHA, ""),
            (0, EXPECTED_SHA, ""),
            (0, json.dumps([{"name": "python-test", "workflow": "CI", "bucket": "pass", "state": "completed"}]), ""),
            (0, EXPECTED_SHA, ""),
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
            (0, check_payload("python-test", "CI", "pending", "in_progress"), ""),
            (0, EXPECTED_SHA, ""),
            (0, check_payload("python-test", "CI", "pass", "completed"), ""),
            (0, EXPECTED_SHA, ""),
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
            (0, check_payload("python-test", "CI", "pending", "in_progress"), ""),
            (0, EXPECTED_SHA, ""),
            (0, check_payload("python-test", "CI", "fail", "completed"), ""),
            (0, EXPECTED_SHA, ""),
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
            (0, check_payload("python-test", "CI", "pending", "in_progress"), ""),
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
            (0, check_payload("python-test", "CI", "pending", "in_progress"), ""),
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
            (0, EXPECTED_SHA, ""),
        ]
    )

    assert exit_code == 1
    assert payload["decision"] == "no_required_evidence"
    assert payload["checks"] == []


def test_required_non_blocking_rule_failure_still_blocks():
    exit_code, payload = run_main(
        [
            (0, EXPECTED_SHA, ""),
            (0, EXPECTED_SHA, ""),
            (0, check_payload("deploy-pr", "deploy-pages", "fail", "completed"), ""),
            (0, EXPECTED_SHA, ""),
        ]
    )

    assert exit_code == 1
    assert payload["decision"] == "failed_blocking"
    assert payload["failed_blocking_count"] == 1
    assert payload["checks"][0]["blocking"] is True
    assert payload["checks"][0]["non_blocking_reason"] is None


def test_required_non_blocking_rule_cancel_still_blocks():
    exit_code, payload = run_main(
        [
            (0, EXPECTED_SHA, ""),
            (0, EXPECTED_SHA, ""),
            (0, check_payload("deploy-pr", "deploy-pages", "cancel", "completed"), ""),
            (0, EXPECTED_SHA, ""),
        ]
    )

    assert exit_code == 1
    assert payload["decision"] == "failed_blocking"
    assert payload["failed_blocking_count"] == 1


def test_terminal_pass_then_head_drift_returns_stale_head_sha():
    exit_code, payload = run_main(
        [
            (0, EXPECTED_SHA, ""),
            (0, EXPECTED_SHA, ""),
            (0, check_payload("python-test", "CI", "pass", "completed"), ""),
            (0, DRIFT_SHA, ""),
        ]
    )

    assert exit_code == 1
    assert payload["decision"] == "stale_head_sha"
    assert payload["current_head_sha"] == DRIFT_SHA
    assert payload["checks"][0]["bucket"] == "pass"
