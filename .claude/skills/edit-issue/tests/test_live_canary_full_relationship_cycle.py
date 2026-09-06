"""Offline fault-injection regression tests for
``live_canary_full_relationship_cycle.sh`` (Issue #1917).

These tests never touch real GitHub. They source the canary script with
``LIVE_CANARY_TEST_MODE=1`` (a guard the script itself defines -- see the
comment above the ``if [ -z "${LIVE_CANARY_TEST_MODE:-}" ]; then`` block at
the bottom of the script), which makes every helper function available for
direct invocation without running the live preflight/steps. Individual
network-facing wrapper functions (``_invoke_txn``, ``_independent_readback``,
``_create_disposable_issue``, ``_close_issue``, ``_check_environment_preflight``,
``_run_step``, ``_run_all_steps``) are then redefined per-scenario, mirroring
the existing ``live_canary_blocking_direction.sh`` / ``_cleanup`` test
pattern in
``.claude/skills/create-issue/tests/test_create_issue_txn_blocking.py``
(#1946 Owner required tests 6/7) -- no new fake-PATH / fake-``gh``-binary
harness is introduced.

Covers (Issue #1917 Verification Commands / In Scope bullet list):
- readback mismatch -> canary nonzero
- readback process failure (nonzero exit / unparsable output) -> canary nonzero
- transaction child exits nonzero despite "successful-looking" JSON -> nonzero
- first step failure stops all subsequent mutation steps
- cleanup failure -> nonzero AND no PASS printed
- partial disposable-Issue creation -> only created resources are cleaned up
- `bash -n` syntax check
- each step's title/body payload differs from every other step's
- environment precheck runs (and SKIPs with exit 77) before any side effect
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "live_canary_full_relationship_cycle.sh"
_REPO_ROOT = Path(__file__).resolve().parents[4]


def _require_bash() -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")


def _run_scenario(body_lines: list[str], timeout: int = 60) -> tuple[int, str]:
    """Source the canary in LIVE_CANARY_TEST_MODE and run ``body_lines``.

    Returns (bash -c's own returncode, combined stdout+stderr). Scenarios
    that need to observe the canary's *internal* function return codes
    (rather than the outer ``bash -c`` exit code) should ``echo`` those
    explicitly, since most scenarios call helpers that never themselves call
    ``exit``.
    """
    _require_bash()
    script = "\n".join(
        [
            "set -u",
            "export LIVE_CANARY_TEST_MODE=1",
            f'cd "{_REPO_ROOT}"',
            f'source "{_SCRIPT}"',
            *body_lines,
        ]
    )
    cp = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=timeout)
    return cp.returncode, cp.stdout + cp.stderr


# ---------------------------------------------------------------------------
# AC1: bash syntax + distinct per-step payloads
# ---------------------------------------------------------------------------


def test_script_has_valid_bash_syntax() -> None:
    _require_bash()
    cp = subprocess.run(["bash", "-n", str(_SCRIPT)], capture_output=True, text=True, timeout=10)
    assert cp.returncode == 0, cp.stderr


def test_step_payloads_have_distinct_title_and_body_per_step() -> None:
    lines = [
        'RUN_ID="testrun_distinct"',
        "titles=()",
        "bodies=()",
        "for step in 1 2 3 4 5 6 7; do",
        '  titles+=("$(_render_step_title "S" "${RUN_ID}" "step-${step}")")',
        '  bodies+=("$(_render_step_body "S" "${RUN_ID}" "step-${step}")")',
        "done",
        'printf "%s\\n" "${titles[@]}"',
        'echo "===BODIES==="',
        'for b in "${bodies[@]}"; do',
        '  printf "%s" "${b}" | sha256sum | cut -d" " -f1',
        "done",
    ]
    rc, out = _run_scenario(lines)
    assert rc == 0, out
    before, after = out.split("===BODIES===")
    titles = [line for line in before.splitlines() if line.strip()]
    body_hashes = [line for line in after.splitlines() if line.strip()]
    assert len(titles) == 7, out
    assert len(set(titles)) == 7, f"expected 7 distinct step titles, got: {titles}"
    assert len(body_hashes) == 7, out
    assert len(set(body_hashes)) == 7, "expected 7 distinct step bodies (by content hash)"


# ---------------------------------------------------------------------------
# AC3: per-step evaluation fault injection (via _run_step, with only the
# network-facing _invoke_txn / _independent_readback wrappers faked -- the
# real, local-only readiness check / body rendering / txn-input assembly all
# still run for real).
# ---------------------------------------------------------------------------


def test_readback_mismatch_causes_nonzero() -> None:
    lines = [
        '_invoke_txn() {',
        '  echo \'{"schema":"ISSUE_EDIT_TXN_RESULT_V1","status":"ok","issue_number":9001,'
        '"repo":"squne121/loop-protocol","body_update":{"attempted":true},'
        '"content_update":{"patch_attempted":true}}\'',
        "  return 0",
        "}",
        '_independent_readback() {',
        '  echo \'{"title":"WRONG_TITLE","body_sha256":"sha256:deadbeef","parent":999,'
        '"blocked_by":[],"blocking":[],"blocked_by_total_count":0,"blocking_total_count":0,'
        '"blocked_by_has_next_page":false,"blocking_has_next_page":false}\'',
        "  return 0",
        "}",
        'CUR_BODY_SHA="sha256:previous"',
        'CUR_UPDATED_AT="2026-01-01T00:00:00Z"',
        '_run_step 1 9001 9002 9003 "runmismatch"',
        'echo "STEP_RC=$?"',
    ]
    rc, out = _run_scenario(lines)
    assert rc == 0, out
    assert "STEP_RC=1" in out, out
    assert "readback_" in out, out


def test_readback_process_failure_causes_nonzero() -> None:
    lines = [
        '_invoke_txn() {',
        '  echo \'{"schema":"ISSUE_EDIT_TXN_RESULT_V1","status":"ok","issue_number":9001,'
        '"repo":"squne121/loop-protocol","body_update":{"attempted":true},'
        '"content_update":{"patch_attempted":true}}\'',
        "  return 0",
        "}",
        '_independent_readback() { echo "not-json-at-all"; return 1; }',
        'CUR_BODY_SHA="sha256:previous"',
        'CUR_UPDATED_AT="2026-01-01T00:00:00Z"',
        '_run_step 1 9001 9002 9003 "runreadbackfail"',
        'echo "STEP_RC=$?"',
    ]
    rc, out = _run_scenario(lines)
    assert rc == 0, out
    assert "STEP_RC=1" in out, out
    assert "readback_process_failed_exit" in out, out


def test_transaction_nonzero_exit_despite_successful_json_causes_nonzero() -> None:
    lines = [
        '_invoke_txn() {',
        '  echo \'{"schema":"ISSUE_EDIT_TXN_RESULT_V1","status":"ok","issue_number":9001,'
        '"repo":"squne121/loop-protocol","body_update":{"attempted":true},'
        '"content_update":{"patch_attempted":true}}\'',
        "  return 1",
        "}",
        '_independent_readback() { echo \'{"title":"whatever"}\'; return 0; }',
        'CUR_BODY_SHA="sha256:previous"',
        'CUR_UPDATED_AT="2026-01-01T00:00:00Z"',
        '_run_step 1 9001 9002 9003 "runnonzeroexit"',
        'echo "STEP_RC=$?"',
    ]
    rc, out = _run_scenario(lines)
    assert rc == 0, out
    assert "STEP_RC=1" in out, out
    assert "txn_child_exit_nonzero:1" in out, out


# ---------------------------------------------------------------------------
# AC4: fail-fast / cleanup control flow
# ---------------------------------------------------------------------------


def test_first_step_failure_stops_subsequent_mutations() -> None:
    lines = [
        'CALLS_LOG="$(mktemp)"',
        '_run_step() {',
        '  echo "$1" >> "${CALLS_LOG}"',
        '  if [ "$1" = "1" ]; then return 1; fi',
        "  return 0",
        "}",
        '_run_all_steps 9001 9002 9003 "runfailfast"',
        'echo "ALL_RC=$?"',
        'echo "CALLS=$(tr "\\n" "," < "${CALLS_LOG}")"',
    ]
    rc, out = _run_scenario(lines)
    assert rc == 0, out
    assert "ALL_RC=1" in out, out
    assert "CALLS=1," in out, out
    assert "CALLS=1,2" not in out, out


def test_cleanup_failure_causes_nonzero_and_no_pass() -> None:
    lines = [
        "_check_environment_preflight() { return 0; }",
        'CREATE_COUNTER="$(mktemp)"',
        "echo 9000 > \"${CREATE_COUNTER}\"",
        "_create_disposable_issue() {",
        '  local n; n=$(( $(cat "${CREATE_COUNTER}") + 1 ))',
        '  echo "$n" > "${CREATE_COUNTER}"',
        '  echo "https://github.com/squne121/loop-protocol/issues/${n}"',
        "}",
        "_fetch_subject_state() { echo '{\"title\":\"t\",\"body\":\"b\",\"updatedAt\":\"2026-01-01T00:00:00Z\"}'; }",
        "_run_all_steps() { return 0; }",
        "_close_issue() { return 1; }",
        "( _main ); echo \"MAIN_RC=$?\"",
    ]
    rc, out = _run_scenario(lines)
    assert rc == 0, out
    assert "MAIN_RC=1" in out, out
    assert "cleanup_failed" in out, out
    assert "PASS:" not in out, out


def test_partial_disposable_creation_cleans_created_resources() -> None:
    lines = [
        "_check_environment_preflight() { return 0; }",
        'CLOSE_LOG="$(mktemp)"',
        'CREATE_CALLS="$(mktemp)"',
        "_create_disposable_issue() {",
        '  echo "$1" >> "${CREATE_CALLS}"',
        '  if [ "$1" = "S" ]; then',
        '    echo "https://github.com/squne121/loop-protocol/issues/9101"',
        "    return 0",
        "  fi",
        "  return 1",
        "}",
        '_close_issue() { echo "$1" >> "${CLOSE_LOG}"; return 0; }',
        "( _main ); echo \"MAIN_RC=$?\"",
        'echo "CLOSED=$(tr "\\n" "," < "${CLOSE_LOG}")"',
        'echo "CREATE_ATTEMPTS=$(tr "\\n" "," < "${CREATE_CALLS}")"',
    ]
    rc, out = _run_scenario(lines)
    assert rc == 0, out
    assert "MAIN_RC=1" in out, out
    assert "CLOSED=9101," in out, out
    assert "CLOSED=9101,9102" not in out, out
    create_attempts_line = next(line for line in out.splitlines() if line.startswith("CREATE_ATTEMPTS="))
    assert create_attempts_line == "CREATE_ATTEMPTS=S,P1,", (
        f"P2 creation must never be attempted once P1 creation failed (fail-fast): {create_attempts_line!r}"
    )


# ---------------------------------------------------------------------------
# AC5: environment precheck runs before any side effect
# ---------------------------------------------------------------------------


def test_environment_precheck_before_side_effects_skips_with_exit_77(tmp_path: Path) -> None:
    _require_bash()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "gh_calls.log"
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{call_log}"\n'
        'if [ "$1" = "auth" ]; then exit 1; fi\n'
        "exit 0\n"
    )
    fake_gh.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    cp = subprocess.run(["bash", str(_SCRIPT)], capture_output=True, text=True, timeout=30, env=env)
    combined = cp.stdout + cp.stderr

    assert cp.returncode == 77, combined
    assert "SKIP" in combined, combined
    if call_log.exists():
        calls = call_log.read_text()
        assert "create" not in calls, (
            f"disposable Issue creation must not be attempted before the environment "
            f"precheck passes: {calls}"
        )
