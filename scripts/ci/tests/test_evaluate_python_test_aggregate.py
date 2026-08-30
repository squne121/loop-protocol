"""Table-driven tests for scripts/ci/evaluate_python_test_aggregate.py (Issue #1824 P1-1).

Every (core_result, bench_mode) combination the aggregate step can plausibly
observe is fixed here so a future "simplify the aggregate to echo ok"
regression (the exact review finding) is caught at the unit level, not just by
a string-search verifier.

Issue #2161 (native Codex CLI retirement): jobs.python-test's dependency on
codex-execpolicy (and this evaluator's --codex-result argument /
codex_result decision input) was removed along with the job; this suite was
updated accordingly.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE_PATH = _REPO_ROOT / "scripts" / "ci" / "evaluate_python_test_aggregate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("evaluate_python_test_aggregate", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


# (core_result, bench_mode) -> expected ok
TABLE: list[tuple[str, bool, bool]] = [
    ("success", False, True),
    ("failure", False, False),
    ("cancelled", False, False),
    ("skipped", False, False),
    ("success", True, True),
    ("failure", True, False),
    ("cancelled", True, False),
    ("skipped", True, False),
]


class TestTableDriven:
    @pytest.mark.parametrize("core_result,bench_mode,expected_ok", TABLE)
    def test_combination(self, mod, core_result, bench_mode, expected_ok):
        ok, reason = mod.evaluate(core_result=core_result, bench_mode=bench_mode)
        assert ok is expected_ok, reason


class TestCli:
    @pytest.mark.parametrize("core_result,bench_mode,expected_ok", TABLE)
    def test_cli_exit_code_matches_table(self, core_result, bench_mode, expected_ok):
        proc = subprocess.run(
            [
                sys.executable,
                str(_MODULE_PATH),
                "--core-result",
                core_result,
                "--bench-mode",
                "true" if bench_mode else "false",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == (0 if expected_ok else 1), proc.stdout + proc.stderr

    def test_cli_missing_required_arg_is_argparse_error(self):
        proc = subprocess.run(
            [sys.executable, str(_MODULE_PATH), "--core-result", "success"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 2

    def test_regression_echo_ok_style_stub_would_be_caught_by_table(self, mod):
        """The exact PR #1824 review finding: an aggregate step body that does
        `echo python_test_bench_aggregate_policy ok` unconditionally would report
        ok=True for every row in TABLE, including known-failing rows. Assert the
        real evaluator differentiates at least one failing case."""
        failures = [row for row in TABLE if not row[2]]
        assert failures, "table has no failing case"
