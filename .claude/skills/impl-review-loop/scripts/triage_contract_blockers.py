#!/usr/bin/env python3
"""Normalize blocked contract evidence into CONTRACT_BLOCKER_TRIAGE_V1."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


SCHEMA_NAME = "CONTRACT_BLOCKER_TRIAGE_V1"
SCHEMA_GOVERNANCE_FOLLOW_UP = "docs/dev/schema-governance.md"
CI_TRUE_DELTA = {"CI": "true"}


def load_payload(input_file: str | None) -> dict[str, Any]:
    if input_file:
        return json.loads(Path(input_file).read_text(encoding="utf-8"))
    return json.loads(sys.stdin.read())


def _unsupported_result(
    *,
    input_schema: str | None,
    unsupported_reason: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_NAME,
        "status": "unsupported_input",
        "aggregate_reason": "unclassified",
        "step1_allowed": False,
        "termination_reason": "intake_gate_failed",
        "intake_gate_subreason": "missing_contract_go",
        "issue_refinement_recommended": False,
        "environment_retry_recommended": False,
        "body_author_fixable": False,
        "suggested_next_action": "human_review",
        "summary": reason,
        "suggested_actions": [
            {
                "kind": "human_review",
                "command": None,
                "preconditions": [],
                "reason": reason,
            }
        ],
        "per_ac": [],
        "source_integrity": {
            "input_schema": input_schema,
            "evidence_complete": False,
            "unsupported_reason": unsupported_reason,
        },
        "schema_change_applicability": {"decision": "schema_change"},
        "schema_governance_update": {
            "status": "out_of_scope_followup_required",
            "followup_issue": SCHEMA_GOVERNANCE_FOLLOW_UP,
        },
        "mutation_free": True,
        "errors": [],
    }


def extract_supported_items(payload: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]] | None, str | None]:
    schema_field = payload.get("schema")
    if schema_field == "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1":
        root = payload
        review_once = root.get("contract_review_once_result")
        if isinstance(review_once, dict):
            classifications = review_once.get("vc_preflight_classifications")
            if isinstance(classifications, list):
                return "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1", classifications, None
        if root.get("source") == "latest_blocked" and root.get("contract_snapshot_url"):
            return "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1", None, "latest_blocked_requires_contract_review_once_result"
        return "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1", None, "missing_classifications"

    if schema_field == "CONTRACT_REVIEW_ONCE_RESULT_V1":
        classifications = payload.get("vc_preflight_classifications")
        if isinstance(classifications, list):
            return "CONTRACT_REVIEW_ONCE_RESULT_V1", classifications, None
        return "CONTRACT_REVIEW_ONCE_RESULT_V1", None, "missing_classifications"

    if schema_field == "BASELINE_VC_PREFLIGHT_RESULT_V1":
        results = payload.get("results")
        if isinstance(results, list):
            return "BASELINE_VC_PREFLIGHT_RESULT_V1", results, None
        return "BASELINE_VC_PREFLIGHT_RESULT_V1", None, "missing_classifications"

    if schema_field == "CONTRACT_REVIEW_RESULT_V1":
        checks = payload.get("checks", {})
        if "vc_preflight" in checks:
            return "CONTRACT_REVIEW_RESULT_V1", None, "unsupported_input_schema"
        return "CONTRACT_REVIEW_RESULT_V1", None, "missing_classifications"

    if "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1" in payload:
        root = payload["CONTRACT_SNAPSHOT_ENSURE_RESULT_V1"]
        review_once = root.get("contract_review_once_result")
        if isinstance(review_once, dict):
            classifications = review_once.get("vc_preflight_classifications")
            if isinstance(classifications, list):
                return "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1", classifications, None
        if root.get("source") == "latest_blocked" and root.get("contract_snapshot_url"):
            return "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1", None, "latest_blocked_requires_contract_review_once_result"
        return "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1", None, "missing_classifications"

    if "CONTRACT_REVIEW_ONCE_RESULT_V1" in payload:
        root = payload["CONTRACT_REVIEW_ONCE_RESULT_V1"]
        classifications = root.get("vc_preflight_classifications")
        if isinstance(classifications, list):
            return "CONTRACT_REVIEW_ONCE_RESULT_V1", classifications, None
        return "CONTRACT_REVIEW_ONCE_RESULT_V1", None, "missing_classifications"

    if "BASELINE_VC_PREFLIGHT_RESULT_V1" in payload:
        root = payload["BASELINE_VC_PREFLIGHT_RESULT_V1"]
        results = root.get("results")
        if isinstance(results, list):
            return "BASELINE_VC_PREFLIGHT_RESULT_V1", results, None
        return "BASELINE_VC_PREFLIGHT_RESULT_V1", None, "missing_classifications"

    if "CONTRACT_REVIEW_RESULT_V1" in payload:
        checks = payload["CONTRACT_REVIEW_RESULT_V1"].get("checks", {})
        if "vc_preflight" in checks:
            return "CONTRACT_REVIEW_RESULT_V1", None, "unsupported_input_schema"
        return "CONTRACT_REVIEW_RESULT_V1", None, "missing_classifications"

    return None, None, "unknown_input_schema"


def infer_pytest_subreason(command: str, stdout_excerpt: str, stderr_excerpt: str) -> str:
    combined = f"{stdout_excerpt}\n{stderr_excerpt}".lower()
    if re.search(r"(^|\s)-k(\s|=)", command):
        return "pytest_k_filter_matches_no_tests"
    if "-k mismatch" in combined or "keyword expression" in combined:
        return "pytest_k_filter_matches_no_tests"
    if "file or directory not found" in combined:
        return "test_path_missing_in_baseline"
    if re.search(r"(^|\s)(tests?/|[^ ]+test[^ ]*\.py)", command):
        return "test_path_exists_but_collects_zero"
    return "ambiguous_no_collection_context"


def _normalize_runner_env_delta(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    command = item.get("command") or item.get("raw_command") or ""
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    runner_env_delta = _normalize_runner_env_delta(
        evidence.get("runner_env_delta", item.get("runner_env_delta", {}))
    )
    stdout_head = item.get("stdout_head", [])
    stderr_head = item.get("stderr_head", [])
    stdout_excerpt = str(evidence.get("stdout_excerpt", ""))
    stderr_excerpt = str(evidence.get("stderr_excerpt", ""))
    if not stdout_excerpt and isinstance(stdout_head, list):
        stdout_excerpt = "\n".join(str(line) for line in stdout_head)
    if not stderr_excerpt and isinstance(stderr_head, list):
        stderr_excerpt = "\n".join(str(line) for line in stderr_head)
    category = str(item.get("category", "unknown"))

    if category == "vc_no_tests_collected":
        triage_reason = "vc_design_requires_refinement"
        subreason = infer_pytest_subreason(command, stdout_excerpt, stderr_excerpt)
        suggested_actions = [
            {
                "kind": "refine_issue_contract",
                "command": None,
                "preconditions": ["vc_preflight classification category is vc_no_tests_collected"],
                "reason": "pytest exit 5 requires VC design refinement before Step 1",
            }
        ]
        body_author_fixable = True
        environment_retry_recommended = False
        evidence_pattern = "pytest_exit_5"
    elif category == "package_manager_no_tty_prompt":
        triage_reason = "environment_artifact"
        if runner_env_delta == CI_TRUE_DELTA:
            subreason = "ci_runner_delta_already_applied"
            suggested_actions = [
                {
                    "kind": "inspect_package_manager_state",
                    "command": None,
                    "preconditions": ["runner_env_delta == {'CI': 'true'}"],
                    "reason": "CI=true was already injected; inspect package manager or node_modules state",
                }
            ]
            environment_retry_recommended = False
        else:
            subreason = "ci_runner_delta_missing"
            suggested_actions = [
                {
                    "kind": "retry_with_ci_true",
                    "command": "CI=true pnpm build",
                    "preconditions": ["runner_env_delta != {'CI': 'true'}"],
                    "reason": "Retry with the canonical runner env delta before editing the Issue contract",
                }
            ]
            environment_retry_recommended = True
        body_author_fixable = False
        evidence_pattern = "pnpm_no_tty"
    else:
        triage_reason = "unclassified"
        subreason = "unsupported_blocker_category"
        suggested_actions = [
            {
                "kind": "human_review",
                "command": None,
                "preconditions": [],
                "reason": f"Unsupported blocked category requires human review: {category}",
            }
        ]
        body_author_fixable = False
        environment_retry_recommended = False
        evidence_pattern = "unknown"

    return {
        "ac": item.get("ac"),
        "command": command,
        "command_hash": "sha256:" + hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "category": category,
        "decision": item.get("decision", "blocked"),
        "triage_reason": triage_reason,
        "subreason": subreason,
        "evidence_pattern": evidence_pattern,
        "runner_env_delta_seen": runner_env_delta,
        "body_author_fixable": body_author_fixable,
        "environment_retry_recommended": environment_retry_recommended,
        "suggested_actions": suggested_actions,
    }


def summarize(per_ac: list[dict[str, Any]], aggregate_reason: str) -> str:
    if not per_ac:
        return "Blocked classifications were not available for deterministic triage."
    fragments = []
    for entry in per_ac[:3]:
        if entry["triage_reason"] == "vc_design_requires_refinement":
            fragments.append(f"{entry['ac']} pytest exit 5 requires VC refinement")
        elif entry["triage_reason"] == "environment_artifact":
            fragments.append(f"{entry['ac']} pnpm no-TTY is an environment artifact")
        else:
            fragments.append(f"{entry['ac']} needs human review")
    summary = "; ".join(fragments)
    if aggregate_reason == "mixed":
        summary += "; mixed routing keeps Step 1 blocked"
    return summary + "."


def build_triage_result(payload: dict[str, Any]) -> dict[str, Any]:
    input_schema, items, unsupported_reason = extract_supported_items(payload)
    if items is None:
        reason_map = {
            "latest_blocked_requires_contract_review_once_result": "Snapshot-only blocked evidence cannot be triaged deterministically.",
            "unsupported_input_schema": "Scalar vc_preflight status is unsupported; per-AC classifications are required.",
            "missing_classifications": "Blocked evidence is incomplete because vc_preflight classifications are missing.",
            "unknown_input_schema": "Input schema is unsupported for contract blocker triage.",
        }
        return _unsupported_result(
            input_schema=input_schema,
            unsupported_reason=unsupported_reason or "unknown_input_schema",
            reason=reason_map.get(unsupported_reason or "", "Unsupported input"),
        )

    blocked_items = [
        normalize_item(item)
        for item in items
        if isinstance(item, dict) and item.get("decision") == "blocked"
    ]
    triage_reasons = {entry["triage_reason"] for entry in blocked_items}

    if not blocked_items or triage_reasons == {"unclassified"}:
        aggregate_reason = "unclassified"
    elif triage_reasons == {"environment_artifact"}:
        aggregate_reason = "environment_artifact"
    elif triage_reasons == {"vc_design_requires_refinement"}:
        aggregate_reason = "vc_design_requires_refinement"
    else:
        aggregate_reason = "mixed"

    suggested_actions: list[dict[str, Any]] = []
    seen = set()
    for entry in blocked_items:
        for action in entry["suggested_actions"]:
            key = (action["kind"], action["command"], action["reason"])
            if key in seen:
                continue
            seen.add(key)
            suggested_actions.append(action)

    if aggregate_reason in {"mixed", "unclassified"}:
        action = {
            "kind": "human_review",
            "command": None,
            "preconditions": [],
            "reason": "Mixed or unclassified blocked evidence must be reviewed before Step 1",
        }
        key = (action["kind"], action["command"], action["reason"])
        if key not in seen:
            suggested_actions.append(action)

    if aggregate_reason == "vc_design_requires_refinement":
        suggested_next_action = "refine_issue_contract"
    elif aggregate_reason == "environment_artifact":
        suggested_next_action = (
            "retry_with_ci_true"
            if any(entry["environment_retry_recommended"] for entry in blocked_items)
            else "inspect_package_manager_state"
        )
    else:
        suggested_next_action = "human_review"

    return {
        "schema": SCHEMA_NAME,
        "status": "ok",
        "aggregate_reason": aggregate_reason,
        "step1_allowed": False,
        "termination_reason": "intake_gate_failed",
        "intake_gate_subreason": "missing_contract_go",
        "issue_refinement_recommended": any(
            entry["triage_reason"] == "vc_design_requires_refinement" for entry in blocked_items
        ),
        "environment_retry_recommended": any(
            entry["environment_retry_recommended"] for entry in blocked_items
        ),
        "body_author_fixable": bool(blocked_items) and all(
            entry["body_author_fixable"] for entry in blocked_items
        ),
        "suggested_next_action": suggested_next_action,
        "summary": summarize(blocked_items, aggregate_reason),
        "suggested_actions": suggested_actions,
        "per_ac": [
            {
                key: value
                for key, value in entry.items()
                if key != "suggested_actions"
            }
            for entry in blocked_items
        ],
        "source_integrity": {
            "input_schema": input_schema,
            "evidence_complete": True,
            "unsupported_reason": None,
        },
        "schema_change_applicability": {"decision": "schema_change"},
        "schema_governance_update": {
            "status": "out_of_scope_followup_required",
            "followup_issue": SCHEMA_GOVERNANCE_FOLLOW_UP,
        },
        "mutation_free": True,
        "errors": [],
    }


triage_contract_blockers = build_triage_result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize blocked contract evidence without mutating GitHub or rerunning preflight."
    )
    parser.add_argument(
        "--input-file",
        help="Read input JSON from file. Omit to read from stdin.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = load_payload(args.input_file)
    except json.JSONDecodeError as exc:
        print(json.dumps({"status": "error", "error": f"Invalid JSON: {exc}"}), file=sys.stderr)
        return 1
    except OSError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps(build_triage_result(payload), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
