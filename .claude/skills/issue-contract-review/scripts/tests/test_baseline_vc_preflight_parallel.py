#!/usr/bin/env python3
"""
Unit tests for Issue #1338 AC4-AC8: bounded parallel execution (--max-workers)
in baseline_vc_preflight.py.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import baseline_vc_preflight as vcp  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[5]
_TARGET_A = ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py"
_TARGET_B = ".claude/skills/issue-contract-review/scripts/vc_contract_syntax.py"
_TARGET_C = ".claude/skills/issue-contract-review/scripts/run_contract_review_once.py"


def _run_main(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["baseline_vc_preflight.py", *argv])
    exit_code = vcp.main()
    captured = capsys.readouterr()
    return exit_code, json.loads(captured.out)


def _write_body(tmp_path, text):
    body_file = tmp_path / "issue_body.md"
    body_file.write_text(text, encoding="utf-8")
    return body_file


def _strip_volatile(data):
    """Drop timing/timestamp fields that legitimately vary between runs."""
    stripped = dict(data)
    stripped.pop("generated_at", None)
    stripped_results = []
    for r in data["results"]:
        rr = dict(r)
        rr.pop("duration_ms", None)
        stripped_results.append(rr)
    stripped["results"] = stripped_results
    return stripped


# ---------------------------------------------------------------------------
# AC4: --max-workers defaults to 1 and is output-identical to explicit 1
# (fully serial, backward-compatible)
# ---------------------------------------------------------------------------

_THREE_COMMAND_BODY = f"""## Verification Commands

```bash
# AC1
$ rg -q nonexistent_pattern_parallel_1338_a {_TARGET_A}
# AC2
$ rg -q nonexistent_pattern_parallel_1338_b {_TARGET_B}
# AC3
$ rg -q nonexistent_pattern_parallel_1338_c {_TARGET_C}
```
"""


def test_ac4_default_max_workers_matches_explicit_serial_output(tmp_path, monkeypatch, capsys):
    body_file = _write_body(tmp_path, _THREE_COMMAND_BODY)

    _, data_default = _run_main(
        monkeypatch, capsys,
        ["--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT)],
    )
    _, data_explicit_serial = _run_main(
        monkeypatch, capsys,
        [
            "--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT),
            "--max-workers", "1",
        ],
    )

    assert _strip_volatile(data_default) == _strip_volatile(data_explicit_serial)


# ---------------------------------------------------------------------------
# AC5: parallel-eligibility predicate
# ---------------------------------------------------------------------------

def test_ac5_parallel_eligible_predicate_allows_only_safe_read_only_shapes(tmp_path):
    # `rg` is eligible only with a validated, existing, repo-relative path
    # operand (P0-2: PR #1508 review). Use a real, existing regular file so
    # the validation gate actually passes.
    existing_file = str(_REPO_ROOT / _TARGET_A)
    assert vcp._is_parallel_eligible_command(
        f"rg -q foo {_TARGET_A}", str(_REPO_ROOT)
    ) is True
    assert vcp._is_parallel_eligible_command("test -f bar.py") is True
    assert vcp._is_parallel_eligible_command("test -d some_dir") is True
    assert vcp._is_parallel_eligible_command("test -s bar.py") is True
    assert existing_file  # sanity: path actually resolved


def test_ac5_parallel_eligible_predicate_excludes_stateful_or_ambiguous_shapes():
    # Not an exact 3-token test invocation.
    assert vcp._is_parallel_eligible_command("test -f bar.py extra") is False
    assert vcp._is_parallel_eligible_command("test -f") is False
    # Explicitly excluded even though allowed for serial preflight execution.
    assert vcp._is_parallel_eligible_command("pnpm typecheck") is False
    assert vcp._is_parallel_eligible_command("uv run pytest foo.py") is False
    assert vcp._is_parallel_eligible_command("uv run --locked pytest foo.py") is False
    assert vcp._is_parallel_eligible_command("pytest foo.py") is False
    assert vcp._is_parallel_eligible_command("gh issue view 1 --repo o/r") is False
    assert vcp._is_parallel_eligible_command("git status") is False
    assert vcp._is_parallel_eligible_command(
        "github_metadata_assert contains description x repos/o/r/milestones/1"
    ) is False


# ---------------------------------------------------------------------------
# P0-2 (PR #1508 review): grep/egrep/fgrep are no longer parallel-eligible,
# and `rg` requires a fully validated path operand.
# ---------------------------------------------------------------------------

def test_p0_2_grep_family_never_parallel_eligible_even_with_valid_path():
    """grep/egrep/fgrep are excluded from the parallel/dedup pool outright,
    regardless of path validity (basename-only classification previously
    allowed them with no path/stdin validation at all)."""
    assert vcp._is_parallel_eligible_command(
        f"grep -q foo {_TARGET_A}", str(_REPO_ROOT)
    ) is False
    assert vcp._is_parallel_eligible_command(
        f"egrep -q foo {_TARGET_A}", str(_REPO_ROOT)
    ) is False
    assert vcp._is_parallel_eligible_command(
        f"fgrep -q foo {_TARGET_A}", str(_REPO_ROOT)
    ) is False


def test_p0_2_rg_without_path_operand_is_not_eligible():
    """A bare `rg PATTERN` (no path operand) searches the whole repo (or
    reads stdin) and must never be treated as a pure/parallel-eligible
    observation."""
    assert vcp._is_parallel_eligible_command("rg pattern", str(_REPO_ROOT)) is False


def test_p0_2_rg_explicit_stdin_operand_is_not_eligible():
    """`rg pattern -` explicitly reads stdin; subprocess.run() defaults to
    inheriting the parent's stdin, which is a race hazard under concurrent
    execution and must be rejected."""
    assert vcp._is_parallel_eligible_command("rg pattern -", str(_REPO_ROOT)) is False


def test_p0_2_rg_broad_repo_root_path_is_not_eligible():
    """A path operand of '.' is unbounded recursion over the whole repo and
    is rejected regardless of Allowed Paths."""
    assert vcp._is_parallel_eligible_command("rg -R pattern .", str(_REPO_ROOT)) is False


def test_p0_2_rg_fifo_path_operand_is_not_eligible(tmp_path):
    """A path operand that resolves to a FIFO (named pipe) must be rejected
    even though it exists on disk."""
    fifo_path = tmp_path / "a_fifo"
    os.mkfifo(fifo_path)
    try:
        assert vcp._is_parallel_eligible_command(
            f"rg pattern {fifo_path}", str(tmp_path)
        ) is False
    finally:
        fifo_path.unlink()


def test_p0_2_rg_missing_path_operand_target_is_not_eligible(tmp_path):
    """A path operand that does not exist on disk cannot be certified pure
    (it may not even be a valid filesystem entry) and must be rejected."""
    assert vcp._is_parallel_eligible_command(
        "rg pattern does_not_exist_1338.py", str(tmp_path)
    ) is False


def test_p0_2_run_command_uses_stdin_devnull(monkeypatch):
    """run_command() must always pass stdin=subprocess.DEVNULL to
    subprocess.run() so concurrently-launched VCs never race on inherited
    stdin (P0-2)."""
    captured_kwargs = {}
    original_run = vcp.subprocess.run

    def recording_run(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(vcp.subprocess, "run", recording_run)
    vcp.run_command("true", 5, str(_REPO_ROOT))
    assert captured_kwargs.get("stdin") is vcp.subprocess.DEVNULL


def test_ac5_max_workers_two_dispatches_eligible_commands_through_thread_pool(tmp_path, monkeypatch, capsys):
    """AC5: with --max-workers 2, the (eligible) rg commands are executed via
    ThreadPoolExecutor; the executor is constructed with the requested width."""
    body_file = _write_body(tmp_path, _THREE_COMMAND_BODY)

    seen_max_workers = []
    original_executor = vcp.ThreadPoolExecutor

    class RecordingExecutor(original_executor):
        def __init__(self, max_workers=None, *args, **kwargs):
            seen_max_workers.append(max_workers)
            super().__init__(max_workers=max_workers, *args, **kwargs)

    monkeypatch.setattr(vcp, "ThreadPoolExecutor", RecordingExecutor)

    _, data = _run_main(
        monkeypatch, capsys,
        [
            "--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT),
            "--max-workers", "2",
        ],
    )

    assert seen_max_workers == [2]
    assert len(data["results"]) == 3
    assert all(r["runner"] == "exec" for r in data["results"])


# ---------------------------------------------------------------------------
# AC6: status aggregation priority unaffected by --max-workers
# ---------------------------------------------------------------------------

_MIXED_STATUS_BODY = f"""## Verification Commands

```bash
# AC1
$ rg -q nonexistent_pattern_parallel_1338_status {_TARGET_A}
# AC2
$ echo hello
```
"""


def test_ac6_status_priority_unaffected_by_max_workers(tmp_path, monkeypatch, capsys):
    body_file = _write_body(tmp_path, _MIXED_STATUS_BODY)

    _, data_serial = _run_main(
        monkeypatch, capsys,
        ["--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT), "--max-workers", "1"],
    )
    _, data_parallel = _run_main(
        monkeypatch, capsys,
        ["--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT), "--max-workers", "4"],
    )

    # `echo hello` exits 0 and is not a regression-gate command -> unexpected_pass/blocked.
    assert data_serial["status"] == "blocked"
    assert data_parallel["status"] == "blocked"


# ---------------------------------------------------------------------------
# AC7: results preserve Issue-body command order, not completion order
# ---------------------------------------------------------------------------

_FIVE_COMMAND_BODY = """## Verification Commands

```bash
# AC1
$ rg -q nonexistent_pattern_1338_order_1 .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py
# AC2
$ rg -q nonexistent_pattern_1338_order_2 .claude/skills/issue-contract-review/scripts/vc_contract_syntax.py
# AC3
$ rg -q nonexistent_pattern_1338_order_3 .claude/skills/issue-contract-review/scripts/run_contract_review_once.py
# AC4
$ test -f .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py
# AC5
$ test -d .claude/skills/issue-contract-review/scripts
```
"""


def test_ac7_result_order_matches_issue_body_command_order(tmp_path, monkeypatch, capsys):
    body_file = _write_body(tmp_path, _FIVE_COMMAND_BODY)

    _, data = _run_main(
        monkeypatch, capsys,
        [
            "--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT),
            "--max-workers", "5",
        ],
    )

    results = data["results"]
    assert [r["ac"] for r in results] == ["AC1", "AC2", "AC3", "AC4", "AC5"]


# ---------------------------------------------------------------------------
# AC8: a timeout on one command does not corrupt other commands' results
# ---------------------------------------------------------------------------

_TIMEOUT_BODY = f"""## Verification Commands

```bash
# AC1
$ rg -q TIMEOUT_TARGET_SENTINEL {_TARGET_A}
# AC2
$ rg -q nonexistent_pattern_1338_timeout_ok {_TARGET_B}
```
"""


def test_ac8_timeout_on_one_command_does_not_corrupt_others(tmp_path, monkeypatch, capsys):
    body_file = _write_body(tmp_path, _TIMEOUT_BODY)

    original_run_command = vcp.run_command

    def flaky_run_command(command, timeout_seconds, cwd):
        if "TIMEOUT_TARGET_SENTINEL" in command:
            return -1, "", "timeout", timeout_seconds * 1000, {}
        return original_run_command(command, timeout_seconds, cwd)

    monkeypatch.setattr(vcp, "run_command", flaky_run_command)

    _, data = _run_main(
        monkeypatch, capsys,
        [
            "--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT),
            "--max-workers", "2",
        ],
    )

    results = data["results"]
    assert len(results) == 2
    timed_out = next(r for r in results if r["ac"] == "AC1")
    other = next(r for r in results if r["ac"] == "AC2")

    assert timed_out["classification"] == "blocked"
    assert timed_out["category"] == "timeout"
    assert other["classification"] == "expected_fail"
    assert other["category"] == "expected_baseline_fail"


# ---------------------------------------------------------------------------
# P2-1 (PR #1508 review): --max-workers is bounded to [1, 8].
# ---------------------------------------------------------------------------


def test_p2_1_bounded_worker_count_accepts_valid_range():
    assert vcp.bounded_worker_count("1") == 1
    assert vcp.bounded_worker_count("8") == 8
    assert vcp.bounded_worker_count("4") == 4


@pytest.mark.parametrize("bad_value", ["0", "-1", "9", "100", "abc", ""])
def test_p2_1_bounded_worker_count_rejects_out_of_range_or_invalid(bad_value):
    with pytest.raises(argparse.ArgumentTypeError):
        vcp.bounded_worker_count(bad_value)


def test_p2_1_max_workers_cli_rejects_zero(tmp_path, monkeypatch, capsys):
    body_file = _write_body(tmp_path, _THREE_COMMAND_BODY)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "baseline_vc_preflight.py",
            "--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT),
            "--max-workers", "0",
        ],
    )
    with pytest.raises(SystemExit):
        vcp.main()


def test_p2_1_max_workers_cli_rejects_above_eight(tmp_path, monkeypatch, capsys):
    body_file = _write_body(tmp_path, _THREE_COMMAND_BODY)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "baseline_vc_preflight.py",
            "--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT),
            "--max-workers", "9",
        ],
    )
    with pytest.raises(SystemExit):
        vcp.main()


# ---------------------------------------------------------------------------
# P1-2 (PR #1508 review): tests that prove actual concurrent execution,
# completion-order reversal, and a REAL (not monkeypatched) subprocess
# timeout, rather than asserting only on mocked call counts.
# ---------------------------------------------------------------------------

_TWO_COMMAND_BODY = f"""## Verification Commands

```bash
# AC1
$ rg -q nonexistent_pattern_1338_concurrency_a {_TARGET_A}
# AC2
$ rg -q nonexistent_pattern_1338_concurrency_b {_TARGET_B}
```
"""


def test_p1_2_proves_actual_concurrent_execution_via_barrier(tmp_path, monkeypatch, capsys):
    """Use a threading.Barrier(2) inside the monkeypatched run_command: it
    only releases once BOTH VC subprocesses have actually started running
    at the same time. If execution were secretly serial (e.g. a thread pool
    that never really overlaps work), the barrier would time out and raise
    threading.BrokenBarrierError, failing this test."""
    body_file = _write_body(tmp_path, _TWO_COMMAND_BODY)
    barrier = threading.Barrier(2, timeout=5)

    def barrier_run_command(command, timeout_seconds, cwd):
        barrier.wait()  # blocks until both threads have reached this line
        return 1, "", "", 1, {}

    monkeypatch.setattr(vcp, "run_command", barrier_run_command)

    _, data = _run_main(
        monkeypatch, capsys,
        [
            "--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT),
            "--max-workers", "2",
        ],
    )
    assert len(data["results"]) == 2


def test_p1_2_reversed_completion_order_preserves_issue_body_result_order(tmp_path, monkeypatch, capsys):
    """Even when the FIRST Issue-body command's subprocess finishes LAST
    (sleep times deliberately reversed vs Issue-body order), results must
    still be emitted in Issue-body order (AC7), not completion order."""
    body_file = _write_body(tmp_path, _TWO_COMMAND_BODY)
    original_run_command = vcp.run_command

    def slow_first_run_command(command, timeout_seconds, cwd):
        if "concurrency_a" in command:
            time.sleep(0.2)
        return original_run_command(command, timeout_seconds, cwd)

    monkeypatch.setattr(vcp, "run_command", slow_first_run_command)

    _, data = _run_main(
        monkeypatch, capsys,
        [
            "--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT),
            "--max-workers", "2",
        ],
    )
    assert [r["ac"] for r in data["results"]] == ["AC1", "AC2"]


_REAL_TIMEOUT_BODY = f"""## Verification Commands

```bash
# AC1
$ rg -q nonexistent_pattern_1338_real_timeout_target {_TARGET_A}
# AC2
$ rg -q nonexistent_pattern_1338_real_timeout_ok {_TARGET_B}
```
"""


def test_p1_2_real_subprocess_timeout_does_not_corrupt_sibling_result(tmp_path, monkeypatch, capsys):
    """A REAL subprocess.TimeoutExpired (an actual `sleep` process killed by
    a real timeout, not a monkeypatched (-1, ..., "timeout", ...) tuple)
    must not corrupt the sibling result computed in the same parallel
    batch."""
    body_file = _write_body(tmp_path, _REAL_TIMEOUT_BODY)
    original_run_command = vcp.run_command

    def real_timeout_run_command(command, timeout_seconds, cwd):
        if "real_timeout_target" in command:
            # Real subprocess.run(timeout=...) -> real TimeoutExpired inside
            # run_command's own try/except, not a simulated return value.
            return original_run_command("sleep 5", 0.05, cwd)
        return original_run_command(command, timeout_seconds, cwd)

    monkeypatch.setattr(vcp, "run_command", real_timeout_run_command)

    _, data = _run_main(
        monkeypatch, capsys,
        [
            "--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT),
            "--max-workers", "2",
        ],
    )
    results = data["results"]
    assert len(results) == 2
    timed_out = next(r for r in results if r["ac"] == "AC1")
    other = next(r for r in results if r["ac"] == "AC2")

    assert timed_out["classification"] == "blocked"
    assert timed_out["category"] == "timeout"
    assert other["classification"] == "expected_fail"
    assert other["category"] == "expected_baseline_fail"


# ---------------------------------------------------------------------------
# P1-1 (PR #1508 review): --max-workers 1 (default) must be output-identical
# (mod additive/volatile fields) to the pre-#1338 baseline implementation,
# INCLUDING relative execution order against the immediate
# github_metadata_assert path (the previous defer-to-end-of-loop design
# could reorder regular VC subprocess launches to AFTER any
# github_metadata_assert call that appeared earlier in the Issue body).
# ---------------------------------------------------------------------------

# Immediate parent of the commit that introduced VC dedup/bounded-parallel
# execution (Issue #1338); this IS the pre-#1338 baseline implementation.
_PRE_1338_BASELINE_REV = "e375b3a1^"


def _load_legacy_baseline_module():
    """Load the pre-#1338 baseline_vc_preflight.py as a standalone module,
    with `__file__` pointed at the REAL script path so its own
    `Path(__file__).resolve().parents[4]` (repo-root resolution) and
    `sys.path` insertion for `vc_contract_syntax` still work correctly."""
    rel_path = ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py"
    result = subprocess.run(
        ["git", "show", f"{_PRE_1338_BASELINE_REV}:{rel_path}"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    module = types.ModuleType("baseline_vc_preflight_legacy_pre_1338")
    module.__file__ = str(_REPO_ROOT / rel_path)
    exec(compile(result.stdout, module.__file__, "exec"), module.__dict__)
    return module


def _semantic_results(results):
    """Additive/volatile-field-stripped projection for legacy-vs-new
    comparison: (ac, exit_code, classification, category, decision,
    scope_class). duration_ms/generated_at and #1338-only fields
    (execution_key_hash/runner/dedup/...) are intentionally excluded."""
    return [
        (r["ac"], r["exit_code"], r["classification"], r["category"], r["decision"], r["scope_class"])
        for r in results
    ]


def test_p1_1_max_workers_one_semantically_equivalent_to_legacy_baseline(tmp_path, monkeypatch, capsys):
    legacy = _load_legacy_baseline_module()

    body = f"""## Verification Commands

```bash
# AC1
$ rg -q nonexistent_pattern_1338_legacy_a {_TARGET_A}
# AC2
$ rg -q nonexistent_pattern_1338_legacy_b {_TARGET_B}
# AC3
$ echo hello
# AC4
# baseline-expect: pass
$ rg -q nonexistent_pattern_1338_legacy_a {_TARGET_A}
```
"""
    body_file = _write_body(tmp_path, body)

    monkeypatch.setattr(
        sys, "argv",
        ["baseline_vc_preflight.py", "--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT)],
    )
    legacy.main()
    legacy_data = json.loads(capsys.readouterr().out)

    _, new_data = _run_main(
        monkeypatch, capsys,
        [
            "--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT),
            "--max-workers", "1",
        ],
    )

    assert legacy_data["status"] == new_data["status"]
    assert _semantic_results(legacy_data["results"]) == _semantic_results(new_data["results"])


def test_p1_1_execution_order_matches_issue_body_order_across_immediate_and_deferred_paths(
    tmp_path, monkeypatch, capsys
):
    """The core P1-1 regression check: github_metadata_assert (executed
    IMMEDIATELY, inline) interleaved with regular rg VCs (previously
    deferred to a single batch AFTER the whole Issue body had been parsed)
    must launch subprocesses in EXACT Issue-body order -- never
    github_metadata_assert-then-all-deferred-VCs."""
    body = f"""## Verification Commands

```bash
# AC1
$ rg -q nonexistent_pattern_1338_order_before {_TARGET_A}
# AC2
$ github_metadata_assert not_contains description __never_present_1338__ repos/o/r/milestones/1
# AC3
$ rg -q nonexistent_pattern_1338_order_after {_TARGET_B}
```
"""
    body_file = _write_body(tmp_path, body)

    call_order = []
    original_run_command = vcp.run_command

    def recording_run_command(command, timeout_seconds, cwd):
        call_order.append(("rg", command))
        return original_run_command(command, timeout_seconds, cwd)

    def recording_assert(assertion_type, field, literal, endpoint, timeout_seconds=10):
        call_order.append(("github_metadata_assert", endpoint))
        return 0  # assertion holds; no live network call needed for this test

    monkeypatch.setattr(vcp, "run_command", recording_run_command)
    monkeypatch.setattr(vcp, "_check_github_metadata_assertion", recording_assert)

    _run_main(
        monkeypatch, capsys,
        ["--body-file", str(body_file), "--issue", "999", "--cwd", str(_REPO_ROOT)],
    )

    assert [kind for kind, _ in call_order] == ["rg", "github_metadata_assert", "rg"]
