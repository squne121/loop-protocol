from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify_codex_agent_model_refresh_evidence.py"


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def valid_config(tmp_path: Path) -> Path:
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=20)
    completed = now - timedelta(seconds=1)
    run_id = "run-test-1451"
    head = "a" * 40
    observed_at = iso(now - timedelta(seconds=10))
    evidence = {
        "schema": "CODEX_AGENT_MODEL_REFRESH_EVIDENCE_V1",
        "evidence_run_id": run_id,
        "started_at": iso(started),
        "completed_at": iso(completed),
        "max_age_seconds": 3600,
        "repo_head_sha": head,
        "expected_head_sha": head,
        "worktree_dirty": True,
        "codex_version": "codex-cli 1.0",
        "auth_mode": "chatgpt",
        "hook_trust": {"status": "trusted", "digest": "sha256:" + "b" * 64},
        "fallback_used": False,
        "direct_smokes": [
            {"model": model, "reasoning_effort": effort, "status": "pass", "fallback_used": False, "observed_at": observed_at}
            for model, effort in sorted({
                ("gpt-5.6-terra", "low"), ("gpt-5.6-terra", "medium"),
                ("gpt-5.6-terra", "high"), ("gpt-5.6-luna", "low"),
                ("gpt-5.6-luna", "medium"),
            })
        ],
        "changed_files": ["scripts/check-codex-agents.mjs"],
        "allowed_paths_gate": "pass",
    }
    config = tmp_path / "evidence.json"
    config.write_text(json.dumps(evidence), encoding="utf-8")
    return config


def run(config: Path) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, str(SCRIPT), "--strict-config", str(config)], text=True, capture_output=True, cwd=ROOT)
    return result.returncode, json.loads(result.stdout)


def test_given_complete_fresh_evidence_when_verified_then_passes(tmp_path: Path):
    config = valid_config(tmp_path)
    code, result = run(config)
    assert code == 0
    assert result["status"] == "PASS"


def test_missing_hook_trust_is_human_action_required(tmp_path: Path):
    config = valid_config(tmp_path)
    value = json.loads(config.read_text())
    value["hook_trust"] = {"status": "unknown"}
    config.write_text(json.dumps(value))
    code, result = run(config)
    assert code == 2
    assert result["status"] == "HUMAN_ACTION_REQUIRED"


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        (lambda evidence: evidence.update(fallback_used=True), "model_fallback_detected"),
        (lambda evidence: evidence["direct_smokes"].pop(), "direct_smoke_missing"),
        (lambda evidence: evidence["direct_smokes"][0].update(status="blocked"), "model_unavailable_or_unsupported_effort"),
        (lambda evidence: evidence.update(repo_head_sha="c" * 40), "foreign_or_stale_commit"),
        (lambda evidence: evidence.update(auth_mode="restricted"), "auth_restriction"),
        (lambda evidence: evidence.update(allowed_paths_gate="fail"), "allowed_paths_violation"),
    ],
)
def test_invalid_runtime_evidence_fails_closed(tmp_path: Path, mutation, error_code: str):
    config = valid_config(tmp_path)
    evidence = json.loads(config.read_text())
    mutation(evidence)
    config.write_text(json.dumps(evidence))
    code, result = run(config)
    assert code == 1
    assert result["status"] == "BLOCKED"
    assert result["error_code"] == error_code


def test_dispatch_ledger_is_not_required_runtime_evidence(tmp_path: Path):
    config = valid_config(tmp_path)
    evidence = json.loads(config.read_text())
    evidence["ledger_path"] = "tmp/nonexistent-dispatch-ledger.json"
    config.write_text(json.dumps(evidence))
    code, result = run(config)
    assert code == 0
    assert result["status"] == "PASS"
