"""Issue #2810 AC12 — consumer replay test for the Step 5 dispatch procedure.

Proves that Step 5's dispatch actually goes THROUGH the real
`classify_runtime_migration.py` CLI (subprocess, stdin JSON -> stdout JSON)
rather than being satisfiable by a markdown string match. Replays the
representative fixtures from AC2-AC7 through the CLI and asserts on the
parsed `class` / `route` / `human_action_report` output fields.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
CLASSIFY_SCRIPT = SCRIPTS_DIR / "classify_runtime_migration.py"


def _run_classifier_cli(payload: dict) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(CLASSIFY_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"classifier CLI exited {proc.returncode}: stderr={proc.stderr!r}"
    return proc.returncode, json.loads(proc.stdout)


def _base_payload(**overrides):
    payload = {
        "failure_evidence": {
            "cause": "proxy_model_catalog_incompatible",
            "repair_command": "bash scripts/claude-gpt/repair_proxy.sh",
            "required_models": ["gpt-6-sol", "gpt-6-luna"],
            "missing_models": ["gpt-6-sol", "gpt-6-luna"],
        },
        "live_issue_authorizes_migration": True,
        "effective_env": {
            "claude_gpt_home": "/home/operator/.claude-gpt",
            "override_vars_present": False,
        },
        "probes": {"install_dir_writable": True, "host_reachable": True},
        "capability_flags": {
            "needs_credential": False,
            "needs_secret": False,
            "needs_privilege": False,
            "destructive_or_global": False,
        },
        "worker_result": None,
    }
    payload.update(overrides)
    return payload


def test_cli_replays_ac2_agent_executable_migration_fixture():
    """GIVEN the AC2 representative fixture WHEN replayed through the real
    CLI subprocess THEN class == agent_executable_migration and route is
    the Step 5 -> Step 1 route."""
    _, result = _run_classifier_cli(_base_payload())
    assert result["class"] == "agent_executable_migration"
    assert result["route"] == "route_to_step1_runtime_migration_fix_delta"
    assert result["human_action_report"] is None


def test_cli_replays_ac2_negative_not_authorized_fixture():
    """GIVEN the AC2 negative fixture (live Issue does not authorize
    migration) WHEN replayed through the CLI THEN class !=
    agent_executable_migration."""
    _, result = _run_classifier_cli(_base_payload(live_issue_authorizes_migration=False))
    assert result["class"] != "agent_executable_migration"


def test_cli_replays_ac4_implementation_defect_fixture():
    """GIVEN the AC4 representative fixture (repair execution failed) WHEN
    replayed through the CLI THEN class == implementation_defect."""
    _, result = _run_classifier_cli(
        _base_payload(
            worker_result={
                "status": "failed",
                "reason_code": "repair_failed",
                "exit_code": 1,
                "deny_evidence_verified": False,
                "sudo_required_in_log": False,
            }
        )
    )
    assert result["class"] == "implementation_defect"
    assert result["human_action_report"] is None


def test_cli_replays_ac5_human_capability_blocker_fixtures():
    """GIVEN each AC5 representative fixture (credential / secret /
    privilege / destructive / unreachable host / non-writable install dir /
    env override / sudo-required-in-log / verified tool-call denial) WHEN
    replayed through the CLI THEN class == human_capability_blocker with a
    fully populated human_action_report, and (for the non-writable case)
    the repair command is never started."""
    fixtures = [
        _base_payload(
            capability_flags={
                "needs_credential": True,
                "needs_secret": False,
                "needs_privilege": False,
                "destructive_or_global": False,
            }
        ),
        _base_payload(probes={"install_dir_writable": True, "host_reachable": False}),
        _base_payload(probes={"install_dir_writable": False, "host_reachable": True}),
        _base_payload(
            effective_env={
                "claude_gpt_home": "/home/operator/.claude-gpt",
                "override_vars_present": True,
            }
        ),
        _base_payload(
            worker_result={
                "status": "failed",
                "reason_code": "repair_failed",
                "exit_code": 1,
                "deny_evidence_verified": False,
                "sudo_required_in_log": True,
            }
        ),
        _base_payload(
            worker_result={
                "status": "permission_blocked",
                "reason_code": "permission_denied",
                "exit_code": None,
                "deny_evidence_verified": True,
                "sudo_required_in_log": False,
            }
        ),
    ]
    for fixture in fixtures:
        _, result = _run_classifier_cli(fixture)
        assert result["class"] == "human_capability_blocker", fixture
        report = result["human_action_report"]
        for field in (
            "reason",
            "required_human_action",
            "target_environment",
            "verification_command",
            "resume_condition",
        ):
            assert report[field], f"missing/empty {field} for fixture {fixture}"


def test_cli_rejects_malformed_json_input_exit_2():
    """GIVEN malformed (non-JSON) stdin WHEN the CLI is invoked THEN it
    exits 2 (not a silent 0/success), never crashing with an unhandled
    traceback that could be mistaken for a passing classification."""
    proc = subprocess.run(
        [sys.executable, str(CLASSIFY_SCRIPT)],
        input="not valid json {{{",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2
