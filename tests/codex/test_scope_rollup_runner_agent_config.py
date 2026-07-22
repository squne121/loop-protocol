from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_PATH = REPO_ROOT / ".codex" / "agents" / "scope-rollup-runner.toml"
EXPECTATION_PATH = REPO_ROOT / "tests" / "fixtures" / "codex-agent-config" / "expected-runtime-contract.json"
CAPTURE_PATH = REPO_ROOT / ".claude" / "hooks" / "capture_scope_rollup_final_response.py"


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


def test_runtime_named_spawn_is_skipped_without_a_pinned_session_recording_runtime():
    pytest.skip(
        "live named-spawn requires a pinned Codex/session-recording runtime; "
        "the static adapter_path_verified fixture is the required automated evidence"
    )
