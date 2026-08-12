#!/usr/bin/env python3
"""Route web-research availability without reinterpreting semantic evidence.

The web-researcher owns retries, provider provenance, grounding, and citation
materialization.  This consumer only joins its bounded result with the main
thread's repository-decision summary.  It deliberately emits no semantic
verdict: an unavailable external provider is an environment/evidence-acquisition
condition, not a reviewer disagreement.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SCHEMA = "WEB_RESEARCH_ROUTING_RESULT_V1"
ROLE_DISPOSITIVE = "dispositive"
ROLE_NON_DISPOSITIVE = "non_dispositive"
REPOSITORY_DECISION_DETERMINED = "determined"
REPOSITORY_DECISION_INCONCLUSIVE = "inconclusive"
NEXT_ACTION_PROCEED = "proceed"
NEXT_ACTION_PROCEED_WITH_NOTES = "proceed_with_notes"
NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED = "human_judgment_required"
TRANSPORT_STATUS_OK = "ok"
TRANSPORT_STATUS_ENVIRONMENT_FAILURE = "environment_failure"


def _transport_reason(web_research: Any) -> str | None:
    """Normalize an unavailable web result without naming it a semantic verdict."""
    if not isinstance(web_research, dict):
        return "web_research_result_missing_or_malformed"

    status = web_research.get("status")
    failure_class = web_research.get("failure_class")
    verification_route = web_research.get("verification_route")
    claims = web_research.get("claims")
    unresolved_risks = web_research.get("unresolved_risks")
    if verification_route not in {"grounded_research", "none"}:
        return "web_research_result_missing_or_malformed"
    if not isinstance(claims, list) or not isinstance(unresolved_risks, list):
        return "web_research_result_missing_or_malformed"
    if status == "ok":
        if failure_class is not None:
            return "web_research_result_missing_or_malformed"
        return None
    if status not in {"failed", "inconclusive", "insufficient_context"}:
        return "web_research_result_missing_or_malformed"

    if isinstance(failure_class, str) and failure_class:
        return f"external_evidence_unavailable:{failure_class}"
    if failure_class is None:
        return f"external_evidence_unavailable:{status}"
    return "web_research_result_missing_or_malformed"


def _is_incomplete_claimed_success(web_research: Any, reason: str | None) -> bool:
    """Reject a malformed `ok` result instead of downgrading its missing evidence."""
    return (
        isinstance(web_research, dict)
        and web_research.get("status") == "ok"
        and reason == "web_research_result_missing_or_malformed"
    )


def _validate_repository_decision(value: Any) -> tuple[str, str | None]:
    if not isinstance(value, dict):
        raise ValueError("repository_decision must be an object")
    status = value.get("status")
    disposition = value.get("disposition")
    if status == REPOSITORY_DECISION_DETERMINED:
        if not isinstance(disposition, str) or not disposition:
            raise ValueError("determined repository_decision requires disposition")
        return status, disposition
    if status == REPOSITORY_DECISION_INCONCLUSIVE and disposition is None:
        return status, None
    raise ValueError("repository_decision must be determined or inconclusive")


def _validate_claim_roles(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("critical_external_claims must be an array")
    if not value:
        raise ValueError("critical_external_claims must not be empty")
    roles: list[str] = []
    for claim in value:
        if not isinstance(claim, dict):
            raise ValueError("critical_external_claims entries must be objects")
        role = claim.get("role")
        if role not in {ROLE_DISPOSITIVE, ROLE_NON_DISPOSITIVE}:
            raise ValueError("critical_external_claim role must be dispositive or non_dispositive")
        roles.append(role)
    return roles


def _invalid_input_result(reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "transport_status": TRANSPORT_STATUS_ENVIRONMENT_FAILURE,
        "semantic_verdict": None,
        "next_action": NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED,
        "repository_disposition": None,
        "reason_codes": [reason],
        "unresolved_risks": [reason],
    }


def route_web_research_result(input_data: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded consumer route for a joined web/repository result."""
    if not isinstance(input_data, dict):
        return _invalid_input_result("web_research_routing_input_invalid")
    if input_data.get("schema") != "WEB_RESEARCH_ROUTING_INPUT_V1":
        return _invalid_input_result("web_research_routing_input_invalid")

    try:
        repository_status, repository_disposition = _validate_repository_decision(
            input_data.get("repository_decision")
        )
        roles = _validate_claim_roles(input_data.get("critical_external_claims"))
    except ValueError as exc:
        return _invalid_input_result(str(exc))

    web_research = input_data.get("web_research")
    unavailable_reason = _transport_reason(web_research)
    if unavailable_reason is None:
        return {
            "schema": SCHEMA,
            "transport_status": TRANSPORT_STATUS_OK,
            "semantic_verdict": None,
            "next_action": NEXT_ACTION_PROCEED,
            "repository_disposition": repository_disposition,
            "reason_codes": [],
            "unresolved_risks": [],
        }

    if _is_incomplete_claimed_success(web_research, unavailable_reason):
        return _invalid_input_result(unavailable_reason)

    repository_independent = repository_status == REPOSITORY_DECISION_DETERMINED
    all_non_dispositive = all(role == ROLE_NON_DISPOSITIVE for role in roles)
    if repository_independent and all_non_dispositive:
        return {
            "schema": SCHEMA,
            "transport_status": TRANSPORT_STATUS_ENVIRONMENT_FAILURE,
            "semantic_verdict": None,
            "next_action": NEXT_ACTION_PROCEED_WITH_NOTES,
            "repository_disposition": repository_disposition,
            "reason_codes": [unavailable_reason, "repository_decision_independent"],
            "unresolved_risks": [unavailable_reason],
        }

    reason_code = (
        "dispositive_external_evidence_unresolved"
        if ROLE_DISPOSITIVE in roles
        else "repository_decision_inconclusive"
    )
    return {
        "schema": SCHEMA,
        "transport_status": TRANSPORT_STATUS_ENVIRONMENT_FAILURE,
        "semantic_verdict": None,
        "next_action": NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED,
        "repository_disposition": repository_disposition,
        "reason_codes": [unavailable_reason, reason_code],
        "unresolved_risks": [unavailable_reason],
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-file", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        input_data = json.loads(args.input_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps(_invalid_input_result(f"input_file_unreadable:{exc.__class__.__name__}")))
        return 2
    print(json.dumps(route_web_research_result(input_data), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
