"""
issue-refinement-loop side consumer/router for
SOURCE_EVIDENCE_ACQUISITION_RESULT_V1 (#2195).

Owns: envelope schema validation (via the real
`source_evidence_acquisition_result_v1.schema.json` / `Draft202012Validator`,
not a hand-rolled subset), claim/baseline binding checks, and run-scoped
cross_lane_recovery_budget bookkeeping (residing here, in memory, for the
duration of a single refinement-run invocation -- no persistent DB).

Does NOT reinterpret provider stderr / exit code / retry policy, and does
NOT re-run route selection -- both are entirely the producer's
(codebase-investigator / source-evidence producer) responsibility. This
module is a thin, deterministic router from `disposition` to a loop
routing action.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Optional, Union

import jsonschema
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

_GEMINI_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "gemini-cli-headless-delegation" / "scripts"
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

_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"
_ACQUISITION_SCHEMA_PATH = _SCHEMAS_DIR / "source_evidence_acquisition_result_v1.schema.json"
_TERMINAL_ARTIFACT_SCHEMA_PATH = _SCHEMAS_DIR / "source_evidence_terminal_artifact_v1.schema.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


_ACQUISITION_SCHEMA = _load_json(_ACQUISITION_SCHEMA_PATH)
_TERMINAL_ARTIFACT_SCHEMA = _load_json(_TERMINAL_ARTIFACT_SCHEMA_PATH)

# source_evidence_acquisition_result_v1.schema.json's `terminal_artifact`
# property $refs this schema by its `$id`
# ("source_evidence_terminal_artifact_v1.schema.json"). Register it as a
# resource so jsonschema can resolve the $ref without a network fetch or a
# filesystem-path-based resolver (#2195 PR #2315 review fix).
_REGISTRY: Registry = Registry().with_resource(
    "source_evidence_terminal_artifact_v1.schema.json",
    Resource.from_contents(_TERMINAL_ARTIFACT_SCHEMA, default_specification=DRAFT202012),
)

_ENVELOPE_VALIDATOR = jsonschema.Draft202012Validator(_ACQUISITION_SCHEMA, registry=_REGISTRY)

# Kept for callers that only need the field-name check without paying for a
# full schema-validator error path (e.g. producing a fast rejection before
# constructing an envelope at all).
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


def _baseline_digest(baseline: dict) -> str:
    canonical = json.dumps(baseline or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_semantic_consistency(envelope: dict) -> list[str]:
    """Cross-array relationships that JSON Schema's per-item validation
    cannot express on its own: every attempt must reference a route_id
    that is actually present in route_plan, and a cross_lane_recovery
    attempt must never be the *only* attempt (there must be a preceding
    primary attempt for it to be "cross-lane" recovery *from*)."""
    errors: list[str] = []
    route_ids = {r.get("route_id") for r in envelope.get("route_plan", [])}
    attempts = envelope.get("attempts", [])
    for attempt in attempts:
        if attempt.get("route_id") not in route_ids:
            errors.append(f"attempt route_id '{attempt.get('route_id')}' is not present in route_plan")
    cross_lane_attempts = [a for a in attempts if a.get("cross_lane_recovery")]
    if cross_lane_attempts and len(attempts) < 2:
        errors.append("cross_lane_recovery attempt present without a preceding primary attempt")
    return errors


def validate_envelope(
    envelope: dict,
    *,
    expected_claim_id: Optional[str] = None,
    expected_evidence_kind: Optional[str] = None,
    expected_baseline: Optional[Union[dict, str]] = None,
) -> dict:
    """Validate SOURCE_EVIDENCE_ACQUISITION_RESULT_V1 shape (against the
    real JSON Schema, including the terminal_artifact $ref and the
    disposition/evidence_refs and disposition/failure_domain `if`/`then`
    conditions) and claim / baseline binding. Returns
    {"ok": bool, "errors": [str]}.

    `expected_baseline` may be either the exact baseline dict to compare
    against, or a precomputed digest string (sha256 of the canonical JSON
    form of the baseline) -- both are checked for an exact match.
    """
    errors: list[str] = []

    if envelope.get("schema") != SCHEMA_ID:
        errors.append(f"schema must be '{SCHEMA_ID}', got '{envelope.get('schema')}'")
        return {"ok": False, "errors": errors}

    for field_name in REQUIRED_ENVELOPE_FIELDS:
        if field_name not in envelope:
            errors.append(f"required field missing: {field_name}")
    if errors:
        return {"ok": False, "errors": errors}

    schema_errors = sorted(_ENVELOPE_VALIDATOR.iter_errors(envelope), key=lambda e: list(e.path))
    for err in schema_errors:
        loc = "/".join(str(p) for p in err.path) or "<root>"
        errors.append(f"schema validation failed at '{loc}': {err.message}")

    if errors:
        return {"ok": False, "errors": errors}

    claim = envelope["claim"]
    if not isinstance(claim, dict) or "claim_id" not in claim or "evidence_kind" not in claim:
        errors.append("claim must include claim_id and evidence_kind")
        return {"ok": False, "errors": errors}

    if expected_claim_id is not None and claim.get("claim_id") != expected_claim_id:
        errors.append(f"claim_id binding mismatch: expected '{expected_claim_id}', got '{claim.get('claim_id')}'")

    if expected_evidence_kind is not None and claim.get("evidence_kind") != expected_evidence_kind:
        errors.append(
            f"evidence_kind binding mismatch: expected '{expected_evidence_kind}', got '{claim.get('evidence_kind')}'"
        )

    if expected_baseline is not None:
        actual_baseline = envelope.get("baseline") or {}
        if isinstance(expected_baseline, str):
            if _baseline_digest(actual_baseline) != expected_baseline:
                errors.append("baseline binding mismatch: baseline digest does not match expected_baseline")
        elif actual_baseline != expected_baseline:
            errors.append("baseline binding mismatch: envelope baseline does not equal expected_baseline")

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

    errors.extend(_validate_semantic_consistency(envelope))

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


# This overlay belongs to Step 1, not the producer or the pure base router.
# The caller must independently pin the baseline, main ref and C1 target before
# invoking it; evidence and an operator receipt are never semantic authority by
# themselves. Keep the C1 restriction explicit instead of making a generic
# approval/evaluator framework out of the source-evidence router.
C1_REPO = "squne121/loop-protocol"
C1_PATH = ".claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py"
C1_CLAIM = (
    "latest_main_net_diff の path が Allowed Paths 外なら現行 production 条件は allowed_paths_conflict と分類する"
)


def c1_baseline(*, issue_body: str, main_sha: str) -> dict:
    return {
        "claim_text_sha256": hashlib.sha256(C1_CLAIM.encode("utf-8")).hexdigest(),
        "issue_body_sha256": hashlib.sha256(issue_body.encode("utf-8")).hexdigest(),
        "current_main_sha": main_sha,
    }


def decide_effective_step1_action(
    envelope: dict,
    *,
    expected_baseline: dict,
    target: dict,
    repo_root: Path,
    operator_decision: dict,
    run_id: str,
    envelope_sha256: str,
) -> dict:
    """Validate independently pinned C1 evidence and post-acquisition decision.

    Returns a separate effective action; never changes the envelope or base
    `decide_routing_action`. The operator snapshot/readback provenance and the
    initial result/state byte pins are verified by the CLI before this call.
    """
    from validate_repo_evidence_ref import validate_repo_evidence_ref  # noqa: PLC0415

    errors: list[str] = []
    validation = validate_envelope(
        envelope,
        expected_claim_id="C1",
        expected_evidence_kind="repo_blob_at_commit",
        expected_baseline=expected_baseline,
    )
    errors.extend(validation["errors"])
    if envelope.get("semantic_verdict") != "not_evaluated" or envelope.get("disposition") != "human_review":
        errors.append("initial producer verdict/disposition must remain not_evaluated/human_review")
    if decide_routing_action(envelope).get("action") != "human_review":
        errors.append("base router must remain human_review")
    refs = envelope.get("evidence_refs", [])
    if not isinstance(refs, list) or len(refs) != 1:
        errors.append("exactly one verified C1 evidence ref required")
    else:
        ref = refs[0]
        if not isinstance(ref, dict):
            errors.append("invalid evidence ref")
        else:
            expected = {
                "commit_sha": expected_baseline["current_main_sha"],
                "path": C1_PATH,
                "start_line": target["start_line"],
                "end_line": target["end_line"],
            }
            if any(ref.get(key) != value for key, value in expected.items()):
                errors.append("evidence ref does not match independent canonical C1 target")
            if ref.get("verification_status") != "verified":
                errors.append("evidence ref is not verified")
            if ref.get("permalink") != (
                f"https://github.com/{C1_REPO}/blob/{expected_baseline['current_main_sha']}/"
                f"{C1_PATH}#L{target['start_line']}-L{target['end_line']}"
            ):
                errors.append("evidence ref repository/permalink mismatch")
            check = validate_repo_evidence_ref(ref, repo_root=repo_root)
            if check["status"] != "verified" or not check["ok"]:
                errors.append("independent pinned Git evidence verification failed")
            expected_tuple = {
                "repo": C1_REPO,
                "commit_sha": expected_baseline["current_main_sha"],
                "path": C1_PATH,
                "start_line": target["start_line"],
                "end_line": target["end_line"],
                "excerpt_sha256": ref.get("excerpt_sha256"),
            }
            if operator_decision.get("evidence") != expected_tuple:
                errors.append("operator decision does not identify verified canonical C1 tuple")
    if operator_decision.get("run_id") != run_id or operator_decision.get("claim_id") != "C1":
        errors.append("operator decision run/claim mismatch")
    if operator_decision.get("envelope_sha256") != envelope_sha256:
        errors.append("operator decision initial envelope digest mismatch")
    if operator_decision.get("canonical_main_sha") != expected_baseline["current_main_sha"]:
        errors.append("operator decision canonical main mismatch")
    if operator_decision.get("decision") != "supported":
        errors.append("operator decision not supported or unknown")
    return {
        "effective_step1_action": "proceed" if not errors else "human_review",
        "claim_resolution": "supported" if not errors else "unresolved",
        "errors": errors,
    }
