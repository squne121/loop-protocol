"""Regression tests for launch-ledger fixture and audit validation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_subagent_launch_ledger.py"
FIXTURES = ROOT / "tests" / "fixtures" / "subagent-ledger"


def run_validator(*args: str) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=ROOT, text=True, capture_output=True
    )
    return result.returncode, json.loads(result.stdout)


def test_given_valid_fixture_when_fixture_mode_then_passes():
    code, payload = run_validator("--fixture-mode", str(FIXTURES / "valid.json"))
    assert code == 0
    assert payload["status"] == "pass"


def test_given_wrong_runtime_fixture_when_fixture_mode_then_records_mismatch():
    code, payload = run_validator("--fixture-mode", str(FIXTURES / "wrong-runtime-config.json"))
    assert code == 0
    assert payload["status"] == "fail"
    assert "runtime_contract_mismatch" in payload["error_codes"]


def test_given_declared_only_ledger_when_audit_mode_then_fails_closed(tmp_path: Path):
    source = json.loads((FIXTURES / "valid.json").read_text(encoding="utf-8"))
    for key in ("fixture_expectation", "project_trust_state", "hook_state", "tool_path_support"):
        source.pop(key, None)
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps(source), encoding="utf-8")
    code, payload = run_validator("--audit-mode", str(ledger))
    assert code == 1
    assert "dispatch_evidence_missing" in payload["error_codes"]
