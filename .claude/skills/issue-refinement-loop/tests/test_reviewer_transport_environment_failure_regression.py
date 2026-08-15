"""Issue #2165 AC3: transport-failure detection must survive the deadline fix.

Regression coverage that (a) genuine transport-layer faults (spawn failure,
signal, real hang) still collapse to `transport_status: environment_failure`
after the deadline/retry-policy changes, and (b) the new structured
`inner_timeout` classification (a child-detected `{"error_code": "timeout"}`
that previously collapsed into the generic `nonzero_exit` reason code) is
both correctly distinguished from an unrelated non-zero exit and correctly
excluded from the deterministic backend's retryable set (so it does not
re-amplify load by retrying an already-over-budget VC).
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import reviewer_transport as transport  # noqa: E402

SHA = "sha256:" + "e" * 64


def _run(
    tmp_path: Path,
    program: str,
    *,
    backend: str = "fixture",
    per_attempt_deadline: int = 1,
    total_deadline: int = 5,
) -> dict:
    return transport.run_reviewer_transport(
        base_argv=[sys.executable, "-c", program],
        command_id="issue-reviewer.run",
        argv_template_id="issue-reviewer.run/v2",
        backend=backend,
        issue_number=2165,
        repo="squne121/loop-protocol",
        reviewed_body_sha256=SHA,
        artifact_root=tmp_path,
        invocation_id="regression",
        session_id="same-session",
        per_attempt_deadline=per_attempt_deadline,
        total_deadline=total_deadline,
    )


def test_given_spawn_failure_when_deadlines_are_production_defaults_then_still_environment_failure(tmp_path: Path):
    result = transport.run_reviewer_transport(
        base_argv=[str(tmp_path / "missing-reviewer")],
        command_id="issue-reviewer.run",
        argv_template_id="issue-reviewer.run/v2",
        backend="fixture",
        issue_number=2165,
        repo="squne121/loop-protocol",
        reviewed_body_sha256=SHA,
        artifact_root=tmp_path,
        invocation_id="spawn-failure-regression",
        session_id="same-session",
        # Deliberately use the module's production defaults here (no
        # per_attempt_deadline/total_deadline override) so this test fails
        # if a future edit widens them enough to change spawn-failure
        # behavior; spawn failure is immediate regardless of deadline size.
    )
    assert result["transport_status"] == "environment_failure"
    assert result["semantic_verdict"] is None
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"spawn_failure"}


def test_given_real_hang_when_per_attempt_deadline_small_then_timeout_still_detected(tmp_path: Path):
    result = _run(tmp_path, "import time; time.sleep(5)", backend="fixture", per_attempt_deadline=1, total_deadline=2)
    assert result["transport_status"] == "environment_failure"
    assert result["semantic_verdict"] is None
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"timeout"}
    assert all(attempt["descendants_reaped"] is True for attempt in result["attempts"])


def test_given_signal_when_child_killed_then_still_environment_failure(tmp_path: Path):
    result = _run(tmp_path, "import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
    assert result["transport_status"] == "environment_failure"
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"signal"}


def test_given_child_detected_timeout_json_on_stderr_then_classified_inner_timeout_not_nonzero_exit(tmp_path: Path):
    # Mirrors `run_root_review_pipeline.py`'s `_cmd_run_checker_attempt()`:
    # on an inner checker's `subprocess.TimeoutExpired`, it prints
    # `{"error_code": "timeout"}` to stderr and exits 2.
    program = (
        "import json, sys\n"
        "print(json.dumps({'error_code': 'timeout'}), file=sys.stderr)\n"
        "sys.exit(2)\n"
    )
    result = _run(tmp_path, program, backend="deterministic")
    assert result["transport_status"] == "environment_failure"
    assert result["semantic_verdict"] is None
    # deterministic backend must NOT retry inner_timeout -- exactly one
    # attempt, not the closed three-attempt matrix.
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["reason_code"] == "inner_timeout"


def test_given_unrelated_nonzero_exit_then_classification_is_unchanged(tmp_path: Path):
    result = _run(tmp_path, "import sys; sys.exit(9)", backend="deterministic")
    assert result["transport_status"] == "environment_failure"
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"nonzero_exit"}
    # nonzero_exit remains retryable for the deterministic backend (it is a
    # transport-layer fault, not a VC-execution-budget fault), so all
    # MAX_ATTEMPTS attempts are exhausted, matching pre-#2165 behavior.
    assert len(result["attempts"]) == transport.MAX_ATTEMPTS


def test_given_nonjson_stderr_on_nonzero_exit_then_classification_falls_back_to_nonzero_exit(tmp_path: Path):
    program = "import sys; sys.stderr.write('not json at all'); sys.exit(2)"
    result = _run(tmp_path, program, backend="deterministic")
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"nonzero_exit"}


def test_given_stderr_json_without_timeout_error_code_then_classification_falls_back_to_nonzero_exit(tmp_path: Path):
    program = (
        "import json, sys\n"
        "print(json.dumps({'error_code': 'gh_auth_failed'}), file=sys.stderr)\n"
        "sys.exit(2)\n"
    )
    result = _run(tmp_path, program, backend="deterministic")
    assert {attempt["reason_code"] for attempt in result["attempts"]} == {"nonzero_exit"}
