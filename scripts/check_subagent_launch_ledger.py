#!/usr/bin/env python3
"""Validate SUBAGENT_LAUNCH_LEDGER_V1 fixtures and audit artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTATION_PATH = REPO_ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json"
FIXTURE_ONLY_FIELDS = {"fixture_expectation", "project_trust_state", "hook_state"}
OPTIONAL_FIELDS = {"codex_binary_status", "generated_at", "ledger_path", "tool_path_support"}
PROHIBITED_ROOT_ACTIONS = {
    "file_edit",
    "test_execution",
    "git_commit",
    "git_push",
    "review_judgment",
    "cleanup_git_mutation",
}
SUPPORTED_PRETOOL_NAMES = {"Bash", "apply_patch", "Edit", "Write"}


def load_expectations() -> dict:
    return json.loads(EXPECTATION_PATH.read_text(encoding="utf-8"))


def canonical_runtime_map(expectations: dict) -> dict[str, dict[str, str]]:
    return {
        agent_name: {
            "model": expected["model"],
            "reasoning_effort": expected["model_reasoning_effort"],
            "default_permissions": expected["default_permissions"],
        }
        for agent_name, expected in expectations["required_agents"].items()
    }


def validate_common_schema(payload: dict, *, fixture_mode: bool) -> list[str]:
    errors: list[str] = []
    required_keys = {
        "ledger_schema",
        "generated_by",
        "coverage_scope",
        "launches",
        "root_thread_actions",
    }
    if fixture_mode:
        required_keys |= {"fixture_expectation", "project_trust_state", "hook_state"}

    allowed_keys = required_keys | OPTIONAL_FIELDS | ({"fixture_expectation", "project_trust_state", "hook_state"} if fixture_mode else set())
    unknown = set(payload.keys()) - allowed_keys
    missing = required_keys - set(payload.keys())
    if unknown:
        errors.append(f"unknown top-level fields: {sorted(unknown)}")
    if missing:
        errors.append(f"missing top-level fields: {sorted(missing)}")
    if payload.get("ledger_schema") != "SUBAGENT_LAUNCH_LEDGER_V1":
        errors.append("ledger_schema must be SUBAGENT_LAUNCH_LEDGER_V1")
    if payload.get("generated_by") != "codex_hook_pipeline":
        errors.append("generated_by must be codex_hook_pipeline")
    if not isinstance(payload.get("launches"), list):
        errors.append("launches must be a list")
    if not isinstance(payload.get("root_thread_actions"), list):
        errors.append("root_thread_actions must be a list")
    if not isinstance(payload.get("coverage_scope"), dict):
        errors.append("coverage_scope must be an object")

    coverage = payload.get("coverage_scope")
    if isinstance(coverage, dict):
        required_coverage = {
            "subagent_start_event_recorded",
            "supported_pretooluse_paths",
            "unsupported_paths_fail_closed",
            "scope_note",
        }
        unknown_coverage = set(coverage.keys()) - required_coverage
        missing_coverage = required_coverage - set(coverage.keys())
        if unknown_coverage:
            errors.append(f"coverage_scope has unknown fields: {sorted(unknown_coverage)}")
        if missing_coverage:
            errors.append(f"coverage_scope missing fields: {sorted(missing_coverage)}")
        supported = coverage.get("supported_pretooluse_paths")
        if not isinstance(supported, list):
            errors.append("coverage_scope.supported_pretooluse_paths must be a list")
        elif sorted(set(supported)) != sorted(SUPPORTED_PRETOOL_NAMES):
            errors.append(
                "coverage_scope.supported_pretooluse_paths must equal ['Bash', 'Edit', 'Write', 'apply_patch']"
            )
        if coverage.get("subagent_start_event_recorded") is not True:
            errors.append("coverage_scope.subagent_start_event_recorded must be true")
        if coverage.get("unsupported_paths_fail_closed") is not True:
            errors.append("coverage_scope.unsupported_paths_fail_closed must be true")
        note = coverage.get("scope_note")
        if not isinstance(note, str) or "supported PreToolUse paths" not in note:
            errors.append("coverage_scope.scope_note must explain supported PreToolUse paths only")

    return errors


def validate_runtime_and_launches(payload: dict, expectations: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    codes: list[str] = []
    runtime_map = canonical_runtime_map(expectations)

    launches = payload.get("launches", [])
    if not launches:
        errors.append("at least one launch is required")
        codes.append("subagent_launch_evidence_missing")

    for launch in launches:
        required_launch_keys = {
            "agent_name",
            "event_type",
            "evidence_source",
            "event_fingerprint",
            "runtime",
        }
        if set(launch.keys()) != required_launch_keys:
            errors.append(f"launch has unknown/missing fields: {sorted(launch.keys())}")
            codes.append("launch_schema_violation")
            continue

        agent_name = launch["agent_name"]
        if agent_name not in runtime_map:
            errors.append(f"unexpected agent_name: {agent_name}")
            codes.append("launch_schema_violation")
            continue
        if launch["event_type"] != "SubagentStart":
            errors.append("launch event_type must be SubagentStart")
            codes.append("subagent_launch_evidence_missing")
        if launch["evidence_source"] != "event_derived":
            errors.append("launch evidence_source must be event_derived")
            codes.append("subagent_launch_evidence_missing")
        if not isinstance(launch["event_fingerprint"], str) or not launch["event_fingerprint"].strip():
            errors.append("launch event_fingerprint must be a non-empty string")
            codes.append("launch_schema_violation")

        runtime = launch["runtime"]
        if set(runtime.keys()) != {"model", "reasoning_effort", "default_permissions"}:
            errors.append(f"runtime has unknown/missing fields: {sorted(runtime.keys())}")
            codes.append("launch_schema_violation")
            continue
        expected_runtime = runtime_map[agent_name]
        if runtime != expected_runtime:
            errors.append(
                f"runtime mismatch for {agent_name}: expected {expected_runtime!r} got {runtime!r}"
            )
            codes.append("runtime_contract_mismatch")

    return errors, codes


def validate_root_thread_actions(payload: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    codes: list[str] = []
    for action in payload.get("root_thread_actions", []):
        required_action_keys = {
            "kind",
            "command",
            "tool_name",
            "coverage_source",
        }
        if set(action.keys()) != required_action_keys:
            errors.append(
                f"root_thread_action has unknown/missing fields: {sorted(action.keys())}"
            )
            codes.append("launch_schema_violation")
            continue
        if action["tool_name"] not in SUPPORTED_PRETOOL_NAMES:
            errors.append(f"unsupported tool_name observed: {action['tool_name']}")
            codes.append("unsupported_tool_path_observed")
        if action["coverage_source"] != "supported_pretooluse_path":
            errors.append("root_thread_action coverage_source must be supported_pretooluse_path")
            codes.append("launch_schema_violation")
        if action["kind"] in PROHIBITED_ROOT_ACTIONS:
            errors.append(
                "root thread must not execute data-plane actions: " + action["kind"]
            )
            codes.append("root_thread_data_plane_execution_observed")
    return errors, codes


def validate_fixture_only_signals(payload: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    codes: list[str] = []
    if payload.get("project_trust_state") != "trusted":
        errors.append("project_trust_state must be trusted")
        codes.append("hook_evidence_untrusted_or_missing")
    if payload.get("hook_state") != "enabled":
        errors.append("hook_state must be enabled")
        codes.append("hook_evidence_untrusted_or_missing")
    if payload.get("tool_path_support") is False:
        errors.append("tool_path_support must not be false")
        codes.append("unsupported_tool_path_observed")
    if payload.get("codex_binary_status") == "unavailable":
        errors.append("codex_binary_status must not be unavailable")
        codes.append("codex_binary_unavailable")
    return errors, codes


def run_fixture_mode(payload: dict, expectations: dict) -> dict:
    errors = validate_common_schema(payload, fixture_mode=True)
    launch_errors, launch_codes = validate_runtime_and_launches(payload, expectations)
    action_errors, action_codes = validate_root_thread_actions(payload)
    signal_errors, signal_codes = validate_fixture_only_signals(payload)
    errors.extend(launch_errors)
    errors.extend(action_errors)
    errors.extend(signal_errors)
    codes = sorted(set(launch_codes + action_codes + signal_codes))

    expectation = payload.get("fixture_expectation", {})
    expected_status = expectation.get("status", "pass")
    expected_codes = sorted(expectation.get("error_codes", []))
    actual_status = "pass" if not errors else "fail"

    if actual_status != expected_status:
        errors.append(
            f"fixture expectation mismatch: expected status {expected_status!r} got {actual_status!r}"
        )
    if expected_codes != codes:
        errors.append(
            f"fixture expectation mismatch: expected error_codes {expected_codes!r} got {codes!r}"
        )

    return {
        "status": actual_status,
        "error_codes": codes,
        "errors": errors,
        "exit_code": 0 if actual_status == expected_status and expected_codes == codes else 1,
    }


def run_audit_mode(ledger_path: Path, expectations: dict) -> dict:
    if not ledger_path.exists():
        return {
            "status": "fail",
            "error_codes": ["subagent_launch_evidence_missing"],
            "errors": [f"ledger file not found: {ledger_path}"],
            "exit_code": 1,
        }

    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    errors = validate_common_schema(payload, fixture_mode=False)
    unexpected_fixture_fields = sorted(set(payload.keys()) & FIXTURE_ONLY_FIELDS)
    if unexpected_fixture_fields:
        errors.append(
            f"audit-mode forbids fixture-only fields: {unexpected_fixture_fields}"
        )
    launch_errors, launch_codes = validate_runtime_and_launches(payload, expectations)
    action_errors, action_codes = validate_root_thread_actions(payload)
    errors.extend(launch_errors)
    errors.extend(action_errors)
    codes = sorted(set(launch_codes + action_codes))
    status = "pass" if not errors else "fail"
    return {
        "status": status,
        "error_codes": codes,
        "errors": errors,
        "exit_code": 0 if not errors else 1,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--fixture-mode", action="store_true")
    group.add_argument("--audit-mode", action="store_true")
    parser.add_argument("ledger", type=Path)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    expectations = load_expectations()

    if args.fixture_mode:
        payload = json.loads(args.ledger.read_text(encoding="utf-8"))
        result = run_fixture_mode(payload, expectations)
    else:
        result = run_audit_mode(args.ledger, expectations)

    output = {
        "ledger": str(args.ledger.resolve().relative_to(REPO_ROOT)) if args.ledger.exists() else str(args.ledger),
        "status": result["status"],
        "error_codes": result["error_codes"],
        "errors": result["errors"],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
