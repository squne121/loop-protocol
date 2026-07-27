"""Tests for scripts/ci/verify_ci_check_conclusions.py (Issue #1760 AC9).

Runtime evidence check: real check-run conclusions for the SAME head SHA
(actionlint / python-test-core / codex-execpolicy / python-test / node-backed-hook-tests)
plus the AC6 sentinel artifact content -- never a plain string search.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE_PATH = _REPO_ROOT / "scripts" / "ci" / "verify_ci_check_conclusions.py"

EXPECTED_SHA = "a" * 40
OTHER_SHA = "b" * 40


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_ci_check_conclusions", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _make_check_runs(
    names: list[str],
    *,
    head_sha: str = EXPECTED_SHA,
    status: str = "completed",
    conclusion: str = "success",
) -> dict:
    return {
        "check_runs": [
            {"name": name, "head_sha": head_sha, "status": status, "conclusion": conclusion, "id": idx}
            for idx, name in enumerate(names)
        ]
    }


ALL_REQUIRED = ["actionlint", "python-test-core", "codex-execpolicy", "python-test", "node-backed-hook-tests"]
TERMINAL_SENTINEL = {"schema": "codex_execpolicy_matrix_status_v1", "status": "completed", "exit_code": 0}


class TestPositive:
    def test_all_green_same_head_is_ok(self, mod):
        report = mod.verify(
            check_runs_payload=_make_check_runs(ALL_REQUIRED),
            sentinel_payload=TERMINAL_SENTINEL,
            expected_head_sha=EXPECTED_SHA,
            bench_mode=False,
        )
        assert report["ok"] is True, report["violations"]

    def test_bench_mode_skips_codex_and_sentinel(self, mod):
        names_minus_codex = [n for n in ALL_REQUIRED if n != "codex-execpolicy"]
        payload = _make_check_runs(names_minus_codex)
        payload["check_runs"].append(
            {
                "name": "codex-execpolicy",
                "head_sha": EXPECTED_SHA,
                "status": "completed",
                "conclusion": "skipped",
                "id": 99,
            }
        )
        report = mod.verify(
            check_runs_payload=payload,
            sentinel_payload=None,
            expected_head_sha=EXPECTED_SHA,
            bench_mode=True,
        )
        assert report["ok"] is True, report["violations"]


class TestMissingCheck:
    def test_missing_required_check_is_rejected(self, mod):
        names = [n for n in ALL_REQUIRED if n != "codex-execpolicy"]
        report = mod.verify(
            check_runs_payload=_make_check_runs(names),
            sentinel_payload=TERMINAL_SENTINEL,
            expected_head_sha=EXPECTED_SHA,
            bench_mode=False,
        )
        assert report["ok"] is False
        assert any("no check run named 'codex-execpolicy'" in v for v in report["violations"])


class TestStaleHeadSha:
    def test_check_at_different_head_sha_is_not_accepted(self, mod):
        report = mod.verify(
            check_runs_payload=_make_check_runs(ALL_REQUIRED, head_sha=OTHER_SHA),
            sentinel_payload=TERMINAL_SENTINEL,
            expected_head_sha=EXPECTED_SHA,
            bench_mode=False,
        )
        assert report["ok"] is False
        assert all(report["checks"][name]["found"] is False for name in ALL_REQUIRED)


class TestFailedConclusion:
    def test_failed_conclusion_is_rejected(self, mod):
        payload = _make_check_runs(ALL_REQUIRED)
        for run in payload["check_runs"]:
            if run["name"] == "python-test-core":
                run["conclusion"] = "failure"
        report = mod.verify(
            check_runs_payload=payload,
            sentinel_payload=TERMINAL_SENTINEL,
            expected_head_sha=EXPECTED_SHA,
            bench_mode=False,
        )
        assert report["ok"] is False
        assert any("python-test-core" in v and "failure" in v for v in report["violations"])

    def test_non_bench_skipped_conclusion_is_rejected(self, mod):
        payload = _make_check_runs(ALL_REQUIRED)
        for run in payload["check_runs"]:
            if run["name"] == "codex-execpolicy":
                run["conclusion"] = "skipped"
        report = mod.verify(
            check_runs_payload=payload,
            sentinel_payload=TERMINAL_SENTINEL,
            expected_head_sha=EXPECTED_SHA,
            bench_mode=False,
        )
        assert report["ok"] is False
        assert any("codex-execpolicy" in v and "skipped" in v for v in report["violations"])

    def test_pending_status_is_rejected(self, mod):
        payload = _make_check_runs(ALL_REQUIRED, status="in_progress", conclusion=None)
        report = mod.verify(
            check_runs_payload=payload,
            sentinel_payload=TERMINAL_SENTINEL,
            expected_head_sha=EXPECTED_SHA,
            bench_mode=False,
        )
        assert report["ok"] is False


class TestSentinelContent:
    def test_sentinel_started_only_is_rejected(self, mod):
        report = mod.verify(
            check_runs_payload=_make_check_runs(ALL_REQUIRED),
            sentinel_payload={"schema": "codex_execpolicy_matrix_status_v1", "status": "started"},
            expected_head_sha=EXPECTED_SHA,
            bench_mode=False,
        )
        assert report["ok"] is False
        assert any("codex_execpolicy_matrix_status_v1.json status" in v for v in report["violations"])

    def test_sentinel_bootstrap_failed_is_rejected(self, mod):
        report = mod.verify(
            check_runs_payload=_make_check_runs(ALL_REQUIRED),
            sentinel_payload={"schema": "codex_execpolicy_matrix_status_v1", "status": "bootstrap_failed"},
            expected_head_sha=EXPECTED_SHA,
            bench_mode=False,
        )
        assert report["ok"] is False

    def test_sentinel_missing_is_rejected(self, mod):
        report = mod.verify(
            check_runs_payload=_make_check_runs(ALL_REQUIRED),
            sentinel_payload=None,
            expected_head_sha=EXPECTED_SHA,
            bench_mode=False,
        )
        assert report["ok"] is False


class TestCli:
    def test_cli_operational_error_on_missing_file(self, tmp_path):
        import subprocess

        proc = subprocess.run(
            [
                "python3",
                str(_MODULE_PATH),
                "--check-runs-api-json",
                str(tmp_path / "missing.json"),
                "--expected-head-sha",
                EXPECTED_SHA,
                "--bench-mode",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 3
        payload = json.loads(proc.stdout)
        assert payload["ok"] is False

    def test_cli_exit_0_on_success(self, tmp_path):
        import subprocess

        runs_path = tmp_path / "runs.json"
        sentinel_path = tmp_path / "sentinel.json"
        runs_path.write_text(json.dumps(_make_check_runs(ALL_REQUIRED)))
        sentinel_path.write_text(json.dumps(TERMINAL_SENTINEL))
        proc = subprocess.run(
            [
                "python3",
                str(_MODULE_PATH),
                "--check-runs-api-json",
                str(runs_path),
                "--codex-sentinel-json",
                str(sentinel_path),
                "--expected-head-sha",
                EXPECTED_SHA,
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_cli_exit_2_on_violation(self, tmp_path):
        import subprocess

        runs_path = tmp_path / "runs.json"
        runs_path.write_text(json.dumps(_make_check_runs([])))
        proc = subprocess.run(
            [
                "python3",
                str(_MODULE_PATH),
                "--check-runs-api-json",
                str(runs_path),
                "--expected-head-sha",
                EXPECTED_SHA,
                "--bench-mode",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 2
