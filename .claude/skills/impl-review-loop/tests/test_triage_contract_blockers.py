from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
SCRIPT_PATH = HERE.parent / "scripts" / "triage_contract_blockers.py"
PREPARATION_MD = HERE.parent / "steps" / "preparation.md"

spec = importlib.util.spec_from_file_location("triage_contract_blockers", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]


def run_cli(payload: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def make_item(ac: str, category: str, stderr_head: list[str] | None = None, runner_env_delta: dict | None = None) -> dict:
    return {
        "ac": ac,
        "command_hash": f"sha256:{ac.lower()}",
        "category": category,
        "decision": "blocked",
        "stderr_head": stderr_head or [],
        "stdout_head": [],
        "runner_env_delta": runner_env_delta or {},
    }


def test_accepted_inputs_scalar_only_unsupported():
    payload = {"schema": "CONTRACT_REVIEW_RESULT_V1", "checks": {"vc_preflight": "blocked"}}
    result = mod.triage_contract_blockers(payload)
    assert result["source_integrity"]["evidence_complete"] is False
    assert result["source_integrity"]["unsupported_reason"] == "unsupported_input_schema"


def test_latest_blocked_incomplete():
    payload = {
        "schema": "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1",
        "source": "latest_blocked",
        "contract_snapshot_url": "https://example.invalid/comment",
    }
    result = mod.triage_contract_blockers(payload)
    assert result["source_integrity"]["evidence_complete"] is False
    assert result["source_integrity"]["unsupported_reason"] == "latest_blocked_requires_contract_review_once_result"


def test_pnpm_no_tty_retry_without_ci():
    payload = {"schema": "BASELINE_VC_PREFLIGHT_RESULT_V1", "results": [make_item("AC3", "package_manager_no_tty_prompt")]}
    result = mod.triage_contract_blockers(payload)
    assert result["aggregate_reason"] == "environment_artifact"
    assert result["environment_retry_recommended"] is True
    assert result["suggested_actions"][0]["command"] == "CI=true pnpm build"


def test_pnpm_no_tty_inspect_with_ci():
    payload = {
        "schema": "BASELINE_VC_PREFLIGHT_RESULT_V1",
        "results": [make_item("AC4", "package_manager_no_tty_prompt", runner_env_delta={"CI": "true"})],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["environment_retry_recommended"] is False
    assert result["suggested_actions"][0]["kind"] == "inspect_package_manager_state"


def test_schema_keys_and_governance():
    payload = {
        "schema": "CONTRACT_REVIEW_ONCE_RESULT_V1",
        "vc_preflight_classifications": [
            make_item("AC5", "package_manager_no_tty_prompt", runner_env_delta={"CI": "true"})
        ],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["schema"] == "CONTRACT_BLOCKER_TRIAGE_V1"
    for key in (
        "aggregate_reason",
        "step1_allowed",
        "termination_reason",
        "intake_gate_subreason",
        "issue_refinement_recommended",
        "environment_retry_recommended",
        "body_author_fixable",
        "suggested_actions",
        "per_ac",
        "source_integrity",
        "mutation_free",
    ):
        assert key in result


def test_pytest_exit_5_subreason():
    payload = {
        "schema": "BASELINE_VC_PREFLIGHT_RESULT_V1",
        "results": [make_item("AC6", "vc_no_tests_collected", stderr_head=["ERROR: file or directory not found: tests/missing.py"])],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["aggregate_reason"] == "vc_design_requires_refinement"
    assert result["per_ac"][0]["subreason"] == "test_path_missing_in_baseline"


def test_mixed_routing():
    payload = {
        "schema": "BASELINE_VC_PREFLIGHT_RESULT_V1",
        "results": [
            make_item("AC7A", "vc_no_tests_collected", stderr_head=["collected 0 tests"]),
            make_item("AC7B", "package_manager_no_tty_prompt", runner_env_delta={"CI": "true"}),
        ],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["aggregate_reason"] == "mixed"
    assert result["step1_allowed"] is False
    assert any(action["kind"] == "human_review" for action in result["suggested_actions"])


def test_mutation_free(monkeypatch):
    def forbidden_run(*args, **kwargs):
        raise AssertionError("subprocess must not be called")

    monkeypatch.setattr(subprocess, "run", forbidden_run)
    payload = {
        "schema": "BASELINE_VC_PREFLIGHT_RESULT_V1",
        "results": [make_item("AC8", "vc_no_tests_collected", stderr_head=["collected 0 tests"])],
    }
    result = mod.triage_contract_blockers(payload)
    assert result["mutation_free"] is True


def test_preparation_summary():
    body = PREPARATION_MD.read_text(encoding="utf-8")
    assert "contract_blocker_triage" in body
    assert "raw stdout / stderr を埋め込まない" in body
    assert "summary:" in body


def test_cli_round_trip():
    payload = {
        "schema": "BASELINE_VC_PREFLIGHT_RESULT_V1",
        "results": [
            {
                **make_item("AC9", "vc_no_tests_collected", stderr_head=["-k mismatch"]),
                "raw_command": "uv run pytest tests/foo.py -k mismatch -v",
            }
        ],
    }
    result = run_cli(payload)
    assert result["per_ac"][0]["subreason"] == "pytest_k_filter_matches_no_tests"
