from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify_codex_agent_model_refresh_evidence.py"
CONTRACT = json.loads((ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json").read_text())


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def make_launch(agent: str, run_id: str, head: str, when: str) -> dict:
    expected = CONTRACT["required_agents"][agent]
    digest = hashlib.sha256((ROOT / expected["path"]).read_bytes()).hexdigest()
    return {
        "agent_name": agent,
        "event_type": "SubagentStart",
        "evidence_source": "event_derived",
        "event_fingerprint": f"session:turn:{agent}",
        "declared_runtime": {
            "model": expected["model"],
            "reasoning_effort": expected["model_reasoning_effort"],
            "default_permissions": expected["default_permissions"],
            "agent_definition_sha256": digest,
        },
        "observed_dispatch": {
            "model": expected["model"],
            "session_id": "session-1",
            "turn_id": f"turn-{agent}",
            "agent_id": f"agent-{agent}",
            "observed_at": when,
        },
        "correlation": {"evidence_run_id": run_id, "repo_head_sha": head, "worktree_dirty": True},
    }


def valid_files(tmp_path: Path) -> tuple[Path, Path]:
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=20)
    completed = now - timedelta(seconds=1)
    run_id = "run-test-1451"
    head = "a" * 40
    observed_at = iso(now - timedelta(seconds=10))
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"launches": [
        make_launch("pr-reviewer", run_id, head, observed_at),
        make_launch("test-runner", run_id, head, observed_at),
    ]}), encoding="utf-8")
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
        "ledger_path": str(ledger.relative_to(ROOT)) if ledger.is_relative_to(ROOT) else str(ledger),
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
    return config, ledger


def run(config: Path) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, str(SCRIPT), "--strict-config", str(config)], text=True, capture_output=True, cwd=ROOT)
    return result.returncode, json.loads(result.stdout)


def test_given_complete_fresh_evidence_when_verified_then_passes(tmp_path: Path):
    config, _ = valid_files(tmp_path)
    code, result = run(config)
    assert code == 0
    assert result["status"] == "PASS"


def test_missing_hook_trust_is_human_action_required(tmp_path: Path):
    config, _ = valid_files(tmp_path)
    value = json.loads(config.read_text())
    value["hook_trust"] = {"status": "unknown"}
    config.write_text(json.dumps(value))
    code, result = run(config)
    assert code == 2
    assert result["status"] == "HUMAN_ACTION_REQUIRED"


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        (lambda evidence, ledger: evidence.update(fallback_used=True), "model_fallback_detected"),
        (lambda evidence, ledger: evidence["direct_smokes"].pop(), "direct_smoke_missing"),
        (lambda evidence, ledger: ledger["launches"][0]["observed_dispatch"].update(model="wrong"), "runtime_contract_mismatch"),
        (lambda evidence, ledger: ledger["launches"].pop(), "terra_luna_coverage_missing"),
        (lambda evidence, ledger: ledger["launches"][0]["observed_dispatch"].update(session_id=None), "dispatch_evidence_missing"),
        (lambda evidence, ledger: evidence.update(repo_head_sha="c" * 40), "foreign_or_stale_commit"),
        (lambda evidence, ledger: evidence.update(allowed_paths_gate="fail"), "allowed_paths_violation"),
    ],
)
def test_invalid_runtime_evidence_fails_closed(tmp_path: Path, mutation, error_code: str):
    config, ledger_path = valid_files(tmp_path)
    evidence = json.loads(config.read_text())
    ledger = json.loads(ledger_path.read_text())
    mutation(evidence, ledger)
    config.write_text(json.dumps(evidence))
    ledger_path.write_text(json.dumps(ledger))
    code, result = run(config)
    assert code == 1
    assert result["status"] == "BLOCKED"
    assert result["error_code"] == error_code
