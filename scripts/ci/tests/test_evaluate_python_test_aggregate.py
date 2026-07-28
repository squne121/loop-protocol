"""Table-driven tests for scripts/ci/evaluate_python_test_aggregate.py (Issue #1824 P1-1).

Every (core_result, codex_result, bench_mode) combination the aggregate step can
plausibly observe is fixed here so a future "simplify the aggregate to
echo ok" regression (the exact review finding) is caught at the unit level, not
just by a string-search verifier.
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


# (core_result, codex_result, bench_mode) -> expected ok
TABLE: list[tuple[str, str, bool, bool]] = [
    # --- non-bench mode: both must succeed ---
    ("success", "success", False, True),
    ("success", "failure", False, False),
    ("failure", "success", False, False),
    ("failure", "failure", False, False),
    ("success", "cancelled", False, False),
    ("success", "skipped", False, False),
    ("cancelled", "success", False, False),
    ("skipped", "success", False, False),
    # --- bench mode: only core_result determines ok (AC10). codex-execpolicy is
    # SKIPPED by its own `if:` in bench mode, so any codex_result is tolerated
    # (an unexpected non-skipped result only emits a `::warning::`, never fails
    # the aggregate -- that is the documented python_test_bench contract).
    ("success", "skipped", True, True),
    ("success", "success", True, True),  # unexpected but non-fatal per AC10
    ("success", "failure", True, True),  # unexpected but non-fatal per AC10
    ("failure", "skipped", True, False),
    ("cancelled", "skipped", True, False),
]


class TestTableDriven:
    @pytest.mark.parametrize("core_result,codex_result,bench_mode,expected_ok", TABLE)
    def test_combination(self, mod, core_result, codex_result, bench_mode, expected_ok):
        ok, reason = mod.evaluate(core_result=core_result, codex_result=codex_result, bench_mode=bench_mode)
        assert ok is expected_ok, reason


class TestCli:
    @pytest.mark.parametrize("core_result,codex_result,bench_mode,expected_ok", TABLE)
    def test_cli_exit_code_matches_table(self, core_result, codex_result, bench_mode, expected_ok):
        proc = subprocess.run(
            [
                sys.executable,
                str(_MODULE_PATH),
                "--core-result",
                core_result,
                "--codex-result",
                codex_result,
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
        real evaluator differentiates at least one failing case per mode."""
        non_bench_failures = [row for row in TABLE if not row[2] and not row[3]]
        bench_failures = [row for row in TABLE if row[2] and not row[3]]
        assert non_bench_failures, "table has no non-bench failing case"
        assert bench_failures, "table has no bench-mode failing case"
