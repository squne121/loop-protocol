from __future__ import annotations

import importlib.util
import hashlib
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


def test_release_pinned_raw_hook_fixture_traverses_adapter_capture_and_canonical_parser(tmp_path: Path):
    """GIVEN a pinned raw wire fixture WHEN adapter capture runs THEN only named passes.

    The test runs the real Node adapter and canonical Python producer.  It
    does not claim that a live Codex session or hook trust was active.
    """
    payload = json.loads(RAW_HOOK_FIXTURE.read_text(encoding="utf-8"))
    assert set(payload) == {
        "hook_event_name", "session_id", "transcript_path", "cwd", "model", "permission_mode",
        "turn_id", "agent_id", "agent_type", "agent_transcript_path", "stop_hook_active", "last_assistant_message",
    }
    planner = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "plan_issue_scope_rollup.py"
    planner_sha = hashlib.sha256(planner.read_bytes()).hexdigest()
    invocation_id = f"scope-rollup-e2e-{tmp_path.name}"
    plan_payload = {
        "schema_version": 2, "repo": "squne121/loop-protocol", "generated_at": "2026-07-15T12:00:01Z",
        "source": "plan_issue_scope_rollup", "body_sha256": "0" * 64,
        "input": {"completeness": "full", "warnings": []}, "candidates": [],
    }
    result_sha = hashlib.sha256(json.dumps(plan_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    marker = f'''```yaml
ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1:
  status: ok
  schema_version: 1
  marker_schema_version: 3
  repo: squne121/loop-protocol
  current_issue: 1671
  invocation_id: {invocation_id}
  requested_at: "2026-07-15T12:00:00Z"
  generated_at: "2026-07-15T12:00:01Z"
  script_blob_sha256: "{planner_sha}"
  inputs:
    current_issue_sha256: "{'0' * 64}"
    issues_all_sha256: "{'1' * 64}"
    prs_all_sha256: "{'2' * 64}"
    issue_count: 0
    pr_count: 0
    query_schema_version: 4
    issues_completeness: {{page_count: 1, item_count: 0, total_count: 0, pagination_complete: true, sha256: "{'1' * 64}"}}
    pull_requests_completeness: {{page_count: 1, item_count: 0, total_count: 0, pagination_complete: true, sha256: "{'2' * 64}"}}
    transaction_budget: {{page_count: 2, response_bytes: 1, inventory_items: 0, max_transaction_pages: 10, max_response_bytes: 10, max_inventory_items: 10, deadline_seconds: 1}}
  result:
    plan_schema: ISSUE_SCOPE_ROLLUP_PLAN_V2
    plan_schema_name: ISSUE_SCOPE_ROLLUP_PLAN_V2
    plan_schema_version: 2
    raw_plan_location: null
    result_sha256: "{result_sha}"
    verify_status: verified
    payload: {json.dumps(plan_payload, ensure_ascii=False)}
```\n'''
    payload["cwd"] = str(REPO_ROOT)
    payload["last_assistant_message"] = marker
    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    eligibility = tmp_path / "eligibility.json"
    readiness = tmp_path / "readiness.json"
    policy = REPO_ROOT / "docs" / "dev" / "session-recording-policy.md"
    secret = REPO_ROOT / "docs" / "dev" / "secret-policy.md"
    producer_digest = "sha256:" + hashlib.sha256(CAPTURE_PATH.read_bytes()).hexdigest()
    digest = lambda path: "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    eligibility.write_text(json.dumps({"schema": "SESSION_RECORDING_SCOPE_ROLLUP_ELIGIBILITY_V1", "artifact_version": 1, "repo_root_realpath": str(REPO_ROOT.resolve()), "head_sha": None, "policy_digest": digest(policy), "secret_policy_digest": digest(secret), "public_checkpoint_present": False, "visibility": "public", "secrets_mode": "none", "generated_at": "2026-07-15T11:00:00Z", "expires_at": "2030-01-01T00:00:00Z", "safety_verdict": "allow"}))
    readiness.write_text(json.dumps({"schema": "SESSION_RECORDING_SCOPE_ROLLUP_READINESS_V1", "artifact_version": 1, "repo_root_realpath": str(REPO_ROOT.resolve()), "uv_lock_digest": None, "python_version_digest": None, "interpreter_realpath": str(Path(sys.executable).resolve()), "interpreter_version": sys.version.split()[0], "producer_digest": producer_digest, "prepared": True, "generated_at": "2026-07-15T11:00:00Z"}))
    os.chmod(eligibility, 0o600)
    os.chmod(readiness, 0o600)
    env = {**os.environ, "SCOPE_ROLLUP_CAPTURE_DIR": str(capture_dir), "SCOPE_ROLLUP_ELIGIBILITY_ARTIFACT_PATH": str(eligibility), "SCOPE_ROLLUP_READINESS_ARTIFACT_PATH": str(readiness), "CODEX_SESSION_RECORDING_PRODUCER": str(REPO_ROOT / "tests" / "hooks" / "_stub-producer.mjs"), "CODEX_HOOK_MANIFEST_ROOT": str(tmp_path / "manifest")}
    named = subprocess.run(["node", str(ADAPTER_PATH), "--event", "SubagentStop"], input=json.dumps(payload), text=True, capture_output=True, cwd=REPO_ROOT, env=env, check=False)
    assert named.returncode == 0 and named.stdout.strip() == '{"continue":true}'
    captured = next(capture_dir.glob("*.txt"))
    sidecar = next(capture_dir.glob("*.capture.yaml"))
    parser = _load_capture_module().__file__.replace("capture_scope_rollup_final_response.py", "../skills/impl-review-loop/scripts/parse_scope_rollup_run_result.py")
    spec = importlib.util.spec_from_file_location("canonical_parser", Path(parser).resolve())
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    parsed = module.parse_scope_rollup_output(assistant_output=captured.read_text(), assistant_output_file=captured, capture_sidecar_file=sidecar, repo="squne121/loop-protocol", issue_number=1671, invocation_id=invocation_id, expected_script_sha=planner_sha, requested_at="2026-07-15T12:00:00Z")
    assert parsed["SCOPE_ROLLUP_MARKER_PARSE_RESULT_V1"]["status"] == "ok"
    payload["agent_type"] = "worker"
    rejected = subprocess.run(["node", str(ADAPTER_PATH), "--event", "SubagentStop"], input=json.dumps(payload), text=True, capture_output=True, cwd=REPO_ROOT, env={**env, "SCOPE_ROLLUP_CAPTURE_DIR": str(tmp_path / "rejected")}, check=False)
    assert rejected.returncode == 0


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
