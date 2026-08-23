"""
issue-refinement-loop side consumer/router for
SOURCE_EVIDENCE_ACQUISITION_RESULT_V1 (#2195).

Owns: envelope schema validation, claim/baseline binding checks, and
run-scoped cross_lane_recovery_budget bookkeeping (residing here, in
memory, for the duration of a single refinement-run invocation -- no
persistent DB).

Does NOT reinterpret provider stderr / exit code / retry policy, and does
NOT re-run route selection -- both are entirely the producer's
(codebase-investigator / source-evidence producer) responsibility. This
module is a thin, deterministic router from `disposition` to a loop
routing action.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_GEMINI_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2] / "gemini-cli-headless-delegation" / "scripts"
)
if str(_GEMINI_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_GEMINI_SCRIPTS_DIR))

from source_evidence_acquisition import (  # noqa: E402
    DISPOSITIONS,
    FAILURE_DOMAINS,
    SCHEMA_ID,
    SEMANTIC_VERDICTS,
    RecoveryBudget,
)

__all__ = [
    "RecoveryBudget",
    "ROUTE_ACTIONS",
    "decide_routing_action",
    "reconcile_budget_consumption",
    "validate_envelope",
]

REQUIRED_ENVELOPE_FIELDS = (
    "schema",
    "claim",
    "baseline",
    "route_plan",
    "attempts",
    "evidence_refs",
    "semantic_verdict",
    "disposition",
)

ROUTE_ACTIONS = ("proceed", "recover", "human_review", "environment_degraded")


def validate_envelope(
    envelope: dict,
    *,
    expected_claim_id: Optional[str] = None,
    expected_evidence_kind: Optional[str] = None,
) -> dict:
    """Validate SOURCE_EVIDENCE_ACQUISITION_RESULT_V1 shape and claim /
    baseline binding. Returns {"ok": bool, "errors": [str]}."""
    errors: list[str] = []

    if envelope.get("schema") != SCHEMA_ID:
        errors.append(f"schema must be '{SCHEMA_ID}', got '{envelope.get('schema')}'")
        return {"ok": False, "errors": errors}

    for field_name in REQUIRED_ENVELOPE_FIELDS:
        if field_name not in envelope:
            errors.append(f"required field missing: {field_name}")
    if errors:
        return {"ok": False, "errors": errors}

    claim = envelope["claim"]
    if not isinstance(claim, dict) or "claim_id" not in claim or "evidence_kind" not in claim:
        errors.append("claim must include claim_id and evidence_kind")
        return {"ok": False, "errors": errors}

    if expected_claim_id is not None and claim.get("claim_id") != expected_claim_id:
        errors.append(
            f"claim_id binding mismatch: expected '{expected_claim_id}', got '{claim.get('claim_id')}'"
        )

    if expected_evidence_kind is not None and claim.get("evidence_kind") != expected_evidence_kind:
        errors.append(
            "evidence_kind binding mismatch: expected "
            f"'{expected_evidence_kind}', got '{claim.get('evidence_kind')}'"
        )

    if envelope["semantic_verdict"] not in SEMANTIC_VERDICTS:
        errors.append(f"invalid semantic_verdict: {envelope['semantic_verdict']}")

    if envelope["disposition"] not in DISPOSITIONS:
        errors.append(f"invalid disposition: {envelope['disposition']}")

    for attempt in envelope.get("attempts", []):
        failure_domain = attempt.get("failure_domain")
        if failure_domain is not None and failure_domain not in FAILURE_DOMAINS:
            errors.append(f"invalid failure_domain in attempts: {failure_domain}")

    # Fail-closed guard (AC2): an operational failure must never be
    # surfaced as a resolved semantic_verdict when no evidence was
    # actually acquired.
    if not envelope.get("evidence_refs") and envelope.get("semantic_verdict") != "not_evaluated":
        errors.append("semantic_verdict must be 'not_evaluated' when evidence_refs is empty")

    return {"ok": not errors, "errors": errors}


def reconcile_budget_consumption(envelope: dict, budget: RecoveryBudget) -> None:
    """Book-keep `budget` consumption based on cross-lane recovery
    attempts actually reported in `envelope`.

    Idempotent: if `budget` is the *same* instance already threaded into
    the producer call (the intended production wiring), this is a no-op
    because the producer already consumed the budget directly. It exists
    as a defensive reconciliation path for callers that maintain a
    separate ledger instance.
    """
    claim_id = envelope["claim"]["claim_id"]
    cross_lane_attempts = [a for a in envelope.get("attempts", []) if a.get("cross_lane_recovery")]
    already_consumed = budget._consumed_by_claim.get(claim_id, 0)  # noqa: SLF001
    missing = len(cross_lane_attempts) - already_consumed
    for _ in range(max(0, missing)):
        budget.consume(claim_id)


def decide_routing_action(envelope: dict) -> dict:
    """Map envelope.disposition to a deterministic issue-refinement-loop
    routing action. Pure function of `disposition` -- does not
    reinterpret provider internals."""
    disposition = envelope["disposition"]
    claim_id = envelope["claim"]["claim_id"]

    if disposition == "proceed":
        return {"action": "proceed", "claim_id": claim_id, "reason": "evidence_acquired_and_bound"}
    if disposition == "recover":
        return {
            "action": "recover",
            "claim_id": claim_id,
            "reason": "eligible_alternate_route_blocked_by_recovery_budget",
        }
    if disposition == "environment_degraded":
        return {
            "action": "environment_degraded",
            "claim_id": claim_id,
            "reason": "operational_failure_across_attempted_routes_not_semantic",
        }
    terminal_artifact = envelope.get("terminal_artifact") or {}
    return {
        "action": "human_review",
        "claim_id": claim_id,
        "reason": terminal_artifact.get("unresolved_reason", "unresolved"),
    }
