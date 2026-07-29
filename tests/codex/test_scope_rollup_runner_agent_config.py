from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_PATH = REPO_ROOT / ".codex" / "agents" / "scope-rollup-runner.toml"
EXPECTATION_PATH = REPO_ROOT / "tests" / "fixtures" / "codex-agent-config" / "expected-runtime-contract.json"
CAPTURE_PATH = REPO_ROOT / ".claude" / "hooks" / "capture_scope_rollup_final_response.py"
ADAPTER_PATH = REPO_ROOT / "scripts" / "session-recording" / "codex-hook-adapter.mjs"
RUNTIME_PROBE = REPO_ROOT / "scripts" / "agent-guards" / "check_scope_rollup_runtime.py"
RAW_HOOK_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "session-recording" / "codex" / "subagent-stop-0.145.0.json.fixture"


def _load_capture_module():
    spec = importlib.util.spec_from_file_location("scope_rollup_capture", CAPTURE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_native_scope_rollup_runner_contract():
    with AGENT_PATH.open("rb") as handle:
        agent = tomllib.load(handle)

    assert agent["name"] == "scope-rollup-runner"
    assert agent["default_permissions"] == "loop-protocol-readonly"
    instructions = agent["developer_instructions"]
    for token in (
        "ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1",
        "nested delegation",
        "exact executor",
        "quoted ISO timestamp",
        "verbatim executor payload",
        "caller invocation echo",
        "completeness fields",
        "marker_schema_version: 3",
        "query_schema_version: 4",
        "required_effective_permission_profile: loop-protocol-scope-rollup",
        "uv sync",
        "session feature set",
    ):
        assert token in instructions
    assert instructions.count("```yaml") == 1


def test_scope_rollup_runner_is_required_by_codex_dispatch_validators():
    expectations = json.loads(EXPECTATION_PATH.read_text(encoding="utf-8"))
    expected = expectations["required_agents"]["scope-rollup-runner"]
    assert expected["path"] == ".codex/agents/scope-rollup-runner.toml"
    assert expected["claude_agent_path"] == ".claude/agents/scope-rollup-runner.md"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/check_codex_agent_config.py",
            "--assert-required-fields",
            "--assert-runtime-contract",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_adapter_path_verified_fixture_allows_named_agent_and_rejects_generic_agent():
    capture = _load_capture_module()
    payload = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "hooks" / "codex-scope-rollup-runner-stop.json").read_text(
            encoding="utf-8"
        )
    )

    named = capture._decision_from_payload(payload)
    generic = capture._decision_from_payload({**payload, "agent_type": "worker"})

    assert named.agent_type == "scope-rollup-runner"
    assert named.capture_source == "last_assistant_message"
    assert named.parser_status == "ok"
    assert generic.capture_status == "agent_type_mismatch"
    assert generic.capture_routing_action == "stop_human"


def test_release_pinned_raw_hook_fixture_is_quarantined_to_passive_metadata(tmp_path: Path):
    """A release-pinned raw payload reaches the passive recorder without
    restoring the quarantined scope-rollup capture path."""
    payload = json.loads(RAW_HOOK_FIXTURE.read_text(encoding="utf-8"))
    assert set(payload) == {
        "hook_event_name", "session_id", "transcript_path", "cwd", "model", "permission_mode",
        "turn_id", "agent_id", "agent_type", "agent_transcript_path", "stop_hook_active", "last_assistant_message",
    }
    recording_dir = tmp_path / "passive-recording"
    env = {**os.environ, "CODEX_PASSIVE_RECORDING_DIR": str(recording_dir)}
    named = subprocess.run(
        ["node", str(ADAPTER_PATH), "--event", "SubagentStop"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    assert named.returncode == 0, named.stderr
    assert named.stdout.strip() == '{"continue":true}'
    records = (recording_dir / "passive-events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(records) == 1
    record = json.loads(records[0])
    assert record["schema"] == "codex_passive_session_record/v1"
    assert record["event"] == "SubagentStop"
    assert set(record) <= {"schema", "event", "recorded_at", "session_id", "thread_id", "agent_id"}
    serialized = json.dumps(record)
    assert payload["last_assistant_message"] not in serialized
    assert payload["agent_type"] not in serialized

    rejected = subprocess.run(
        ["node", str(ADAPTER_PATH), "--event", "PreToolUse"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    assert rejected.returncode == 0
    assert rejected.stdout == ""
    assert len((recording_dir / "passive-events.jsonl").read_text().splitlines()) == 1


def test_runtime_probe_is_availability_gated_and_never_promotes_skip_to_pass():
    result = subprocess.run([sys.executable, str(RUNTIME_PROBE)], text=True, capture_output=True, cwd=REPO_ROOT, check=False)
    artifact = json.loads(result.stdout)["SCOPE_ROLLUP_RUNTIME_EVIDENCE_V1"]
    assert artifact["status"] in {"PASS", "SKIP"}
    assert artifact["uv_sync_used"] is False
    if artifact["status"] == "SKIP":
        assert result.returncode == 77
        assert artifact["reason"]


def test_permission_exclusion_is_reasoned_and_consumer_inventory_is_complete():
    expectations = json.loads(EXPECTATION_PATH.read_text(encoding="utf-8"))
    exclusion = expectations["required_agents"]["scope-rollup-runner"]["permission_exclusion"]
    assert exclusion == {
        "allowlisted_agent": "scope-rollup-runner",
        "reason": "claude_auto_permission_is_not_comparable_to_codex_ephemeral_write_profile",
        "follow_up_issue": "#1686",
        "expires_on": "2026-12-31",
    }
    result = subprocess.run([sys.executable, "scripts/check_claude_codex_agent_parity.py", "--strict"], cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "claude.permissionMode=auto" in result.stdout
    assert "match: not_compared" in result.stdout
    policy = (REPO_ROOT / "docs" / "dev" / "session-recording-policy.md").read_text(encoding="utf-8")
    for consumer in (
        "run_scope_rollup_preflight.py", "codex-hook-adapter.mjs", "capture_scope_rollup_final_response.py",
        "parse_scope_rollup_run_result.py", "verify_scope_rollup_result.py", "steps/preparation.md",
    ):
        assert consumer in policy


def test_runtime_named_spawn_is_skipped_without_a_pinned_session_recording_runtime():
    pytest.skip(
        "live named-spawn requires a pinned Codex/session-recording runtime; "
        "the static adapter_path_verified fixture is the required automated evidence"
    )
