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

Plus (Issue #1917 PR #2529 REQUEST_CHANGES fix_delta, three defects):
- P1 run isolation: every invocation gets its own unique run directory
  (`_init_run_dir`, `mktemp -d`); a step's fixed expected body SHA-256 is
  captured immediately after body generation / static validation and BEFORE
  `_invoke_txn` performs the remote mutation, and is never recomputed by
  re-reading a (potentially since-mutated) file afterward; the offline test
  harness never shares a run directory with a real (non-test) canary
  execution.
- P1-2 strict schema/type checking in `_evaluate_step`: JSON `null` /
  array / string / number / bool standing in for the transaction or
  readback result object is a FAIL (not silently skipped), and
  `attempted` / `patch_attempted` must be the Python bool `True` (a
  string `"false"` or `"true"` must not be treated as truthy/falsy by
  accident).
- P2 fixture-validation-before-creation: every creation + per-step body
  fixture is statically validated before the first `gh issue create`; a
  fixture defect is a FAIL (never exit 77) and zero disposable Issues are
  created.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "live_canary_full_relationship_cycle.sh"
_REPO_ROOT = Path(__file__).resolve().parents[4]

# Issue #1917 fix_delta P1 item 4: the offline regression test's run
# directories must never share a path with a real (non-test) canary
# execution's run directory. `LIVE_CANARY_ARTIFACT_ROOT` (read by
# `_init_run_dir` in the script) is pointed here for every scenario instead
# of the production default (`artifacts/1917/issue-metadata/
# live_canary_full_relationship_cycle`), and this directory is wiped before
# and after every test so no test-created run directory outlives its test
# or leaks into a later one.
_TEST_ARTIFACT_ROOT = (
    _REPO_ROOT / "artifacts" / "1917" / "issue-metadata" / "live_canary_full_relationship_cycle_pytest"
)


@pytest.fixture(autouse=True)
def _isolated_test_artifact_root() -> None:
    shutil.rmtree(_TEST_ARTIFACT_ROOT, ignore_errors=True)
    yield
    shutil.rmtree(_TEST_ARTIFACT_ROOT, ignore_errors=True)


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

    ``LIVE_CANARY_ARTIFACT_ROOT`` is always pointed at ``_TEST_ARTIFACT_ROOT``
    (never the production live-run artifact directory) so this offline
    harness can never collide with, read, or clobber a real canary run's
    on-disk artifacts (Issue #1917 fix_delta P1 item 4).
    """
    _require_bash()
    script = "\n".join(
        [
            "set -u",
            "export LIVE_CANARY_TEST_MODE=1",
            f'export LIVE_CANARY_ARTIFACT_ROOT="{_TEST_ARTIFACT_ROOT}"',
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
        "_init_run_dir",
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
        "_init_run_dir",
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
        "_init_run_dir",
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
        # Not under test here (covered separately below) -- stubbed so this
        # scenario stays focused on cleanup-failure control flow and does
        # not pay for real per-fixture readiness-check subprocesses.
        "_validate_fixtures_before_creation() { return 0; }",
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
        # Not under test here (covered separately below) -- stubbed so this
        # scenario stays focused on partial-creation cleanup control flow.
        "_validate_fixtures_before_creation() { return 0; }",
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


# ---------------------------------------------------------------------------
# Issue #1917 PR #2529 REQUEST_CHANGES fix_delta P1: run-local isolation +
# expected-SHA fixed before mutation (never recomputed from a possibly
# since-mutated file).
# ---------------------------------------------------------------------------


def test_init_run_dir_produces_a_real_unique_directory_inside_the_repo() -> None:
    lines = [
        "_init_run_dir",
        'echo "RC=$?"',
        'echo "RUN_DIR=${RUN_DIR}"',
        'echo "RELATIVE_RUN_DIR=${RELATIVE_RUN_DIR}"',
        '[ -d "${RUN_DIR}" ] && echo "IS_DIR=yes" || echo "IS_DIR=no"',
    ]
    rc, out = _run_scenario(lines)
    assert rc == 0, out
    assert "RC=0" in out, out
    assert "IS_DIR=yes" in out, out
    run_dir_line = next(line for line in out.splitlines() if line.startswith("RUN_DIR="))
    relative_line = next(line for line in out.splitlines() if line.startswith("RELATIVE_RUN_DIR="))
    run_dir = run_dir_line.split("=", 1)[1]
    relative_run_dir = relative_line.split("=", 1)[1]
    assert run_dir, out
    assert relative_run_dir, out
    assert not relative_run_dir.startswith("/"), (
        f"RELATIVE_RUN_DIR must be repo-relative (no leading slash) for the "
        f"production new_body_file/input-file contract: {relative_run_dir!r}"
    )
    assert run_dir.endswith(relative_run_dir), out
    # Must never be the shared, non-unique, pre-fix path -- run-local
    # isolation is the whole point of this fixture.
    assert not relative_run_dir.endswith("live_canary_full_relationship_cycle"), out


def test_separate_invocations_get_distinct_non_colliding_run_directories() -> None:
    """Two independent invocations (separate sourced subprocesses, as two
    real canary runs -- or a live run and a test run -- would be) must never
    be assigned the same run directory (Issue #1917 fix_delta P1 item 1/4)."""
    rc1, out1 = _run_scenario(["_init_run_dir", 'echo "RUN_DIR=${RUN_DIR}"'])
    rc2, out2 = _run_scenario(["_init_run_dir", 'echo "RUN_DIR=${RUN_DIR}"'])
    assert rc1 == 0, out1
    assert rc2 == 0, out2
    run_dir_1 = next(line for line in out1.splitlines() if line.startswith("RUN_DIR=")).split("=", 1)[1]
    run_dir_2 = next(line for line in out2.splitlines() if line.startswith("RUN_DIR=")).split("=", 1)[1]
    assert run_dir_1 and run_dir_2, (out1, out2)
    assert run_dir_1 != run_dir_2, "two separate invocations must not collide on the same run directory"


def test_same_role_run_id_marker_in_different_run_dirs_does_not_collide() -> None:
    """Direct regression for the reported P1 defect: even with the SAME
    role/run_id/marker (the previous, non-run-scoped path was keyed only on
    these), writing the body fixture from a second, independently
    initialized run directory must not touch the first run's file."""
    lines = [
        "_init_run_dir",
        'run_a_dir="${RUN_DIR}"',
        'body_rel_a="$(_write_body_file "S" "samerunid" "step-1")"',
        'sha_a_before="$(_sha256_of_file "${REPO_ROOT}/${body_rel_a}")"',
        # Force a second, independent _init_run_dir call (simulating a
        # second run/process) with the exact same role/run_id/marker.
        'RUN_DIR=""',
        'RELATIVE_RUN_DIR=""',
        "_init_run_dir",
        'run_b_dir="${RUN_DIR}"',
        'body_rel_b="$(_write_body_file "S" "samerunid" "step-1")"',
        'sha_a_after="$(_sha256_of_file "${REPO_ROOT}/${body_rel_a}")"',
        'echo "RUN_A_DIR=${run_a_dir}"',
        'echo "RUN_B_DIR=${run_b_dir}"',
        'echo "BODY_REL_A=${body_rel_a}"',
        'echo "BODY_REL_B=${body_rel_b}"',
        'echo "SHA_A_BEFORE=${sha_a_before}"',
        'echo "SHA_A_AFTER=${sha_a_after}"',
    ]
    rc, out = _run_scenario(lines)
    assert rc == 0, out
    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    assert values["RUN_A_DIR"] != values["RUN_B_DIR"], out
    assert values["BODY_REL_A"] != values["BODY_REL_B"], (
        f"same role/run_id/marker in two different run directories must still "
        f"produce two distinct on-disk paths: {out}"
    )
    assert values["SHA_A_BEFORE"] == values["SHA_A_AFTER"], (
        f"run A's body file must be untouched by run B's write to a "
        f"same-named fixture in a different run directory: {out}"
    )


def test_expected_body_sha_is_fixed_before_invoke_txn_and_survives_later_file_mutation() -> None:
    """Direct regression for the reported P1 ordering defect: `_run_step`
    must fix `body_expected_sha` immediately after body generation / static
    validation and BEFORE `_invoke_txn` runs, so that even if something
    mutates the run-local body file during/after the (simulated) remote
    mutation, the value already handed to `_evaluate_step` is unaffected."""
    lines = [
        "_init_run_dir",
        'pre_body_rel="$(_write_body_file "S" "shafix" "step-1")"',
        'pre_sha="$(_sha256_of_file "${REPO_ROOT}/${pre_body_rel}")"',
        'echo "PRE_SHA=${pre_sha}"',
        'CAPTURED_SHA_FILE="$(mktemp)"',
        '_invoke_txn() {',
        # Simulate a corruption/overwrite of this run's OWN body file
        # happening while the "remote mutation" is in flight -- this must
        # NOT be able to retroactively change the already-fixed expected
        # SHA used at evaluation time.
        '  printf "CORRUPTED-BY-CONCURRENT-WRITE" > "${REPO_ROOT}/${pre_body_rel}"',
        '  echo \'{"schema":"ISSUE_EDIT_TXN_RESULT_V1","status":"ok","issue_number":9001,'
        '"repo":"squne121/loop-protocol","body_update":{"attempted":true},'
        '"content_update":{"patch_attempted":true}}\'',
        "  return 0",
        "}",
        '_independent_readback() { echo \'{"title":"whatever"}\'; return 0; }',
        '_evaluate_step() { echo "$6" > "${CAPTURED_SHA_FILE}"; echo "ok"; return 0; }',
        'CUR_BODY_SHA="sha256:previous"',
        'CUR_UPDATED_AT="2026-01-01T00:00:00Z"',
        '_run_step 1 9001 9002 9003 "shafix"',
        'echo "STEP_RC=$?"',
        'echo "CAPTURED_SHA=$(cat "${CAPTURED_SHA_FILE}")"',
        'echo "POST_FILE_CONTENT=$(cat "${REPO_ROOT}/${pre_body_rel}")"',
    ]
    rc, out = _run_scenario(lines)
    assert rc == 0, out
    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    assert values["STEP_RC"] == "0", out
    assert values["POST_FILE_CONTENT"] == "CORRUPTED-BY-CONCURRENT-WRITE", (
        "sanity check: the on-disk file really was mutated during _invoke_txn"
    )
    assert values["CAPTURED_SHA"] == values["PRE_SHA"], (
        f"expected body sha handed to _evaluate_step must be the PRE-mutation "
        f"value fixed before _invoke_txn ran, not recomputed from the "
        f"since-corrupted file: {out}"
    )


# ---------------------------------------------------------------------------
# Issue #1917 PR #2529 REQUEST_CHANGES fix_delta P1-2: strict schema/type
# checking in _evaluate_step. Called directly (it is a pure function of its
# 9 positional args -- no gh, no RUN_DIR needed) for precise fault
# injection independent of _run_step's surrounding machinery.
# ---------------------------------------------------------------------------


def _evaluate_step_scenario(
    txn_exit: int,
    txn_stdout: str,
    readback_exit: int,
    readback_json: str,
) -> tuple[int, str]:
    lines = [
        "_evaluate_step 9001 squne121/loop-protocol "
        f"{txn_exit} '{txn_stdout}' 'expected title' 'sha256:expected' "
        "'{\"parent\":null,\"blocked_by\":[],\"blocking\":[]}' "
        f"{readback_exit} '{readback_json}'",
        'echo "EVAL_RC=$?"',
    ]
    return _run_scenario(lines)


_OK_TXN = (
    '{"schema":"ISSUE_EDIT_TXN_RESULT_V1","status":"ok","issue_number":9001,'
    '"repo":"squne121/loop-protocol","body_update":{"attempted":true},'
    '"content_update":{"patch_attempted":true}}'
)
_OK_READBACK = (
    '{"title":"expected title","body_sha256":"sha256:expected","parent":null,'
    '"blocked_by":[],"blocking":[],"blocked_by_total_count":0,"blocking_total_count":0,'
    '"blocked_by_has_next_page":false,"blocking_has_next_page":false}'
)


@pytest.mark.parametrize("txn_stdout", ["null", "[]", '"a string"', "42", "true", "false"])
def test_non_object_txn_stdout_is_fail_not_silently_skipped(txn_stdout: str) -> None:
    rc, out = _evaluate_step_scenario(0, txn_stdout, 0, _OK_READBACK)
    assert rc == 0, out
    assert "EVAL_RC=1" in out, out
    assert "txn_stdout_not_object" in out, (
        f"non-object (including JSON null) txn stdout must be a FAIL with an "
        f"explicit reason, never silently pass through: {out}"
    )


@pytest.mark.parametrize("readback_stdout", ["null", "[]", '"a string"', "42", "true", "false"])
def test_non_object_readback_stdout_is_fail_not_silently_skipped(readback_stdout: str) -> None:
    rc, out = _evaluate_step_scenario(0, _OK_TXN, 0, readback_stdout)
    assert rc == 0, out
    assert "EVAL_RC=1" in out, out
    assert "readback_stdout_not_object" in out, (
        f"non-object (including JSON null) readback stdout must be a FAIL "
        f"with an explicit reason, never silently pass through: {out}"
    )


def test_string_false_attempted_is_fail_not_truthy() -> None:
    txn_stdout = (
        '{"schema":"ISSUE_EDIT_TXN_RESULT_V1","status":"ok","issue_number":9001,'
        '"repo":"squne121/loop-protocol","body_update":{"attempted":"false"},'
        '"content_update":{"patch_attempted":true}}'
    )
    rc, out = _evaluate_step_scenario(0, txn_stdout, 0, _OK_READBACK)
    assert rc == 0, out
    assert "EVAL_RC=1" in out, out
    assert "body_update_not_attempted" in out, (
        f"a string \"false\" must never be accepted as truthy for "
        f"body_update.attempted: {out}"
    )


def test_string_false_patch_attempted_is_fail_not_truthy() -> None:
    txn_stdout = (
        '{"schema":"ISSUE_EDIT_TXN_RESULT_V1","status":"ok","issue_number":9001,'
        '"repo":"squne121/loop-protocol","body_update":{"attempted":true},'
        '"content_update":{"patch_attempted":"false"}}'
    )
    rc, out = _evaluate_step_scenario(0, txn_stdout, 0, _OK_READBACK)
    assert rc == 0, out
    assert "EVAL_RC=1" in out, out
    assert "content_update_not_patch_attempted" in out, (
        f"a string \"false\" must never be accepted as truthy for "
        f"content_update.patch_attempted: {out}"
    )


def test_fully_valid_txn_and_readback_still_pass() -> None:
    """Non-regression: the strict type checks above must not turn a
    genuinely well-formed success result into a false FAIL."""
    rc, out = _evaluate_step_scenario(0, _OK_TXN, 0, _OK_READBACK)
    assert rc == 0, out
    assert "EVAL_RC=0" in out, out


# ---------------------------------------------------------------------------
# Issue #1917 PR #2529 REQUEST_CHANGES fix_delta P2: fixture validation
# (creation fixtures + all 7 step body fixtures) must run, and must be able
# to FAIL closed, strictly before the first `gh issue create`.
# ---------------------------------------------------------------------------


def test_fixture_defect_blocks_all_disposable_issue_creation_and_fails_not_skips() -> None:
    lines = [
        "_check_environment_preflight() { return 0; }",
        'CREATE_COUNTER="$(mktemp)"',
        "echo 0 > \"${CREATE_COUNTER}\"",
        "_create_disposable_issue() {",
        '  local n; n=$(( $(cat "${CREATE_COUNTER}") + 1 ))',
        '  echo "$n" > "${CREATE_COUNTER}"',
        '  echo "https://github.com/squne121/loop-protocol/issues/900${n}"',
        "}",
        # Simulate a fixture defect (e.g. a body-hygiene/readiness violation)
        # detected by the SAME static check _run_step normally relies on.
        "_run_readiness_check() { echo '{\"status\":\"not_go\",\"errors\":[\"disposable fixture defect\"]}'; return 1; }",
        "( _main ); echo \"MAIN_RC=$?\"",
        'echo "CREATE_CALL_COUNT=$(cat "${CREATE_COUNTER}")"',
    ]
    rc, out = _run_scenario(lines)
    assert rc == 0, out
    assert "MAIN_RC=1" in out, out
    assert "MAIN_RC=77" not in out, (
        f"a fixture defect must FAIL (exit 1), never be rounded to SKIP "
        f"(exit 77 -- reserved for environment preconditions): {out}"
    )
    assert "CREATE_CALL_COUNT=0" in out, (
        f"zero disposable Issues (S/P1/P2) may be created once fixture "
        f"validation fails: {out}"
    )
    assert "SKIP" not in out, out


def test_valid_fixtures_allow_disposable_issue_creation_to_proceed() -> None:
    """Non-regression: real, well-formed fixtures must not be blocked by the
    new pre-creation validation phase."""
    lines = [
        "_check_environment_preflight() { return 0; }",
        'CREATE_COUNTER="$(mktemp)"',
        "echo 0 > \"${CREATE_COUNTER}\"",
        "_create_disposable_issue() {",
        '  local n; n=$(( $(cat "${CREATE_COUNTER}") + 1 ))',
        '  echo "$n" > "${CREATE_COUNTER}"',
        '  echo "https://github.com/squne121/loop-protocol/issues/900${n}"',
        "}",
        "_fetch_subject_state() { echo '{\"title\":\"t\",\"body\":\"b\",\"updatedAt\":\"2026-01-01T00:00:00Z\"}'; }",
        "_run_all_steps() { return 0; }",
        "_close_issue() { return 0; }",
        "( _main ); echo \"MAIN_RC=$?\"",
        'echo "CREATE_CALL_COUNT=$(cat "${CREATE_COUNTER}")"',
    ]
    rc, out = _run_scenario(lines)
    assert rc == 0, out
    assert "MAIN_RC=0" in out, out
    assert "CREATE_CALL_COUNT=3" in out, (
        f"S/P1/P2 must all be created once real fixtures pass static validation: {out}"
    )
    assert "PASS:" in out, out
