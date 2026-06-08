#!/usr/bin/env python3
"""Strict validator for SUBAGENT_LAUNCH_LEDGER_V1 fixtures."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_AGENTS = {
    "implementation-worker",
    "test-runner",
    "pr-reviewer",
    "post-merge-cleanup-worker",
}
PROHIBITED_ROOT_ACTIONS = {
    "file_edit",
    "test_execution",
    "git_commit",
    "git_push",
    "review_judgment",
    "cleanup_git_mutation",
}
ALLOWED_TRUST_STATES = {"trusted"}
ALLOWED_BINARY_STATUSES = {"available"}
ALLOWED_HOOK_STATES = {"enabled"}
ALLOWED_EVIDENCE_SOURCES = {"event_derived"}


def validate_schema(payload: dict) -> list[str]:
    errors: list[str] = []
    required_keys = {
        "ledger_schema",
        "generated_by",
        "codex_binary_status",
        "hook_state",
        "tool_path_support",
        "project_trust_state",
        "launches",
        "root_thread_actions",
        "fixture_expectation",
    }
    unknown = set(payload.keys()) - required_keys
    missing = required_keys - set(payload.keys())
    if unknown:
        errors.append(f"unknown top-level fields: {sorted(unknown)}")
    if missing:
        errors.append(f"missing top-level fields: {sorted(missing)}")
    if payload.get("ledger_schema") != "SUBAGENT_LAUNCH_LEDGER_V1":
        errors.append("ledger_schema must be SUBAGENT_LAUNCH_LEDGER_V1")
    if not isinstance(payload.get("launches"), list):
        errors.append("launches must be a list")
    if not isinstance(payload.get("root_thread_actions"), list):
        errors.append("root_thread_actions must be a list")
    return errors


def validate_fail_closed(payload: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    codes: list[str] = []

    if payload.get("generated_by") != "codex_hook_pipeline":
        errors.append("generated_by must be codex_hook_pipeline")
        codes.append("subagent_launch_evidence_missing")
    if payload.get("project_trust_state") not in ALLOWED_TRUST_STATES:
        errors.append("project_trust_state must be trusted")
        codes.append("hook_evidence_untrusted_or_missing")
    if payload.get("hook_state") not in ALLOWED_HOOK_STATES:
        errors.append("hook_state must be enabled")
        codes.append("hook_evidence_untrusted_or_missing")
    if payload.get("tool_path_support") is not True:
        errors.append("tool_path_support must be true")
        codes.append("unsupported_tool_path_observed")
    if payload.get("codex_binary_status") not in ALLOWED_BINARY_STATUSES:
        errors.append("codex_binary_status must be available")
        codes.append("codex_binary_unavailable")

    launches = payload.get("launches", [])
    if not launches:
        errors.append("at least one launch is required")
        codes.append("subagent_launch_evidence_missing")
    for launch in launches:
        if set(launch.keys()) != {
            "agent_name",
            "event_type",
            "evidence_source",
            "event_fingerprint",
            "runtime",
        }:
            errors.append(f"launch has unknown/missing fields: {sorted(launch.keys())}")
            codes.append("launch_schema_violation")
            continue
        if launch["agent_name"] not in ALLOWED_AGENTS:
            errors.append(f"unexpected agent_name: {launch['agent_name']}")
            codes.append("launch_schema_violation")
        if launch["event_type"] != "SubagentStart":
            errors.append("launch event_type must be SubagentStart")
            codes.append("subagent_launch_evidence_missing")
        if launch["evidence_source"] not in ALLOWED_EVIDENCE_SOURCES:
            errors.append("launch evidence_source must be event_derived")
            codes.append("subagent_launch_evidence_missing")
        runtime = launch["runtime"]
        if set(runtime.keys()) != {"model", "reasoning_effort", "default_permissions"}:
            errors.append(f"runtime has unknown/missing fields: {sorted(runtime.keys())}")
            codes.append("launch_schema_violation")

    prohibited_seen = []
    for action in payload.get("root_thread_actions", []):
        if set(action.keys()) != {"kind", "command"}:
            errors.append(f"root_thread_action has unknown/missing fields: {sorted(action.keys())}")
            codes.append("launch_schema_violation")
            continue
        if action["kind"] in PROHIBITED_ROOT_ACTIONS:
            prohibited_seen.append(action["kind"])
    if prohibited_seen:
        errors.append(
            "root thread must not execute data-plane actions: "
            + ", ".join(sorted(set(prohibited_seen)))
        )
        codes.append("root_thread_data_plane_execution_observed")

    return errors, codes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    args = parser.parse_args()

    payload = json.loads(args.fixture.read_text(encoding="utf-8"))
    errors = validate_schema(payload)
    fail_closed_errors, codes = validate_fail_closed(payload)
    errors.extend(fail_closed_errors)
    codes = sorted(set(codes))

    expectation = payload.get("fixture_expectation", {})
    expected_status = expectation.get("status", "pass")
    expected_codes = sorted(expectation.get("error_codes", []))
    actual_status = "pass" if not errors else "fail"
    expectation_mismatch = False
    if actual_status != expected_status:
        expectation_mismatch = True
        errors.append(
            f"fixture expectation mismatch: expected status {expected_status!r} got {actual_status!r}"
        )
    if expected_codes != codes:
        expectation_mismatch = True
        errors.append(
            f"fixture expectation mismatch: expected error_codes {expected_codes!r} got {codes!r}"
        )

    result = {
        "fixture": str(args.fixture.resolve().relative_to(REPO_ROOT)),
        "status": actual_status,
        "error_codes": codes,
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not expectation_mismatch else 1


if __name__ == "__main__":
    sys.exit(main())
