#!/usr/bin/env python3
"""
Unit tests for Issue #1338 AC4-AC8: bounded parallel execution (--max-workers)
in baseline_vc_preflight.py.
"""

import json
import sys
from pathlib import Path

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

def test_ac5_parallel_eligible_predicate_allows_only_safe_read_only_shapes():
    assert vcp._is_parallel_eligible_command("rg -q foo bar.py") is True
    assert vcp._is_parallel_eligible_command("grep -q foo bar.py") is True
    assert vcp._is_parallel_eligible_command("egrep -q foo bar.py") is True
    assert vcp._is_parallel_eligible_command("fgrep -q foo bar.py") is True
    assert vcp._is_parallel_eligible_command("test -f bar.py") is True
    assert vcp._is_parallel_eligible_command("test -d some_dir") is True
    assert vcp._is_parallel_eligible_command("test -s bar.py") is True


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
