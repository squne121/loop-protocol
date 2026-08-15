"""Issue #2165 AC1 (runtime-verification: true).

Real subprocess coverage: `run_root_review_pipeline.py produce` against a
live Issue whose body carries a legitimately long-running Verification
Command (a skill's full pytest suite) must not collapse to
`reviewer_transport_environment_failure`. This performs real network I/O
(`gh issue view`) and a real, potentially multi-minute subprocess execution
chain, matching the `## Runtime Verification Applicability` section of the
Issue body (`decision: immediate`, `skip_conditions`: no `gh auth` ->
pytest.skip, per the fail-closed fallback policy: a GitHub-reachable
environment that still fails to produce a result is a real FAIL, not a
fallback PASS).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = Path(__file__).parent.parent / "scripts" / "run_root_review_pipeline.py"
_ISSUE_NUMBER = 2156
_REPO = "squne121/loop-protocol"

# Issue #2165: PER_ATTEMPT_DEADLINE_SECONDS=300 / TOTAL_DEADLINE_SECONDS=340
# bound `run_reviewer_transport()`'s own retry loop; give the outer
# subprocess a further margin above that ceiling for `gh` I/O and process
# startup/teardown overhead around it.
_SUBPROCESS_TIMEOUT_SECONDS = 400


def _gh_auth_available() -> bool:
    gh = shutil.which("gh")
    if gh is None:
        return False
    try:
        completed = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return completed.returncode == 0


@pytest.mark.skipif(
    not _gh_auth_available(),
    reason="gh auth not available in this execution environment (SKIP, not PASS)",
)
def test_given_live_issue_with_long_running_vc_when_produce_runs_then_no_environment_failure():
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "produce",
            "--issue-number",
            str(_ISSUE_NUMBER),
            "--repo",
            _REPO,
        ],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        cwd=str(_REPO_ROOT),
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        pytest.fail(
            f"produce did not emit parseable JSON stdout: exit={completed.returncode} "
            f"stderr={completed.stderr[-2000:]}"
        )

    assert payload.get("error_code") != "reviewer_transport_environment_failure", (
        f"reviewer transport still collapses a legitimate long-running VC into "
        f"environment_failure: {json.dumps(payload)[:2000]}"
    )
    assert payload.get("status") in {"ok", "input_or_runtime_error"}
    if payload.get("status") == "ok":
        verdict = payload["compact_result"]["verdict"]
        assert verdict in {"approve", "needs-fix"}
    else:
        # A live-environment error unrelated to the deadline fix (e.g. the
        # live Issue body itself no longer exists, or a transient `gh`
        # failure) is acceptable here -- AC1 only asserts the specific
        # `reviewer_transport_environment_failure` regression is gone, not
        # that every other input_or_runtime_error path is impossible.
        assert payload.get("error_code") not in {None, "reviewer_transport_environment_failure"}
