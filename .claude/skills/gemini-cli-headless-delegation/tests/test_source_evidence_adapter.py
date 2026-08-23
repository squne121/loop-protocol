"""
End-to-end fixture-based tests (#2195 AC7): producer envelope ->
issue-refinement routing -> proceed / recover / terminal (human_review /
environment_degraded).

Exercises the full chain: source_evidence_acquisition.run_acquisition()
(producer, gemini-cli-headless-delegation) feeding into
route_source_evidence_result.decide_routing_action() (consumer,
issue-refinement-loop) via validate_envelope() as the schema gate between
them.
"""

import sys
from pathlib import Path

scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))
refinement_scripts_dir = (
    Path(__file__).resolve().parents[2] / "issue-refinement-loop" / "scripts"
)
sys.path.insert(0, str(refinement_scripts_dir))

from source_evidence_acquisition import RecoveryBudget, run_acquisition  # noqa: E402
from route_source_evidence_result import decide_routing_action, validate_envelope  # noqa: E402


def _ref(path="docs/adr/0001.md"):
    return {
        "type": "REPO_EVIDENCE_REF_V1",
        "commit_sha": "c" * 40,
        "object_format": "sha1",
        "path": path,
        "start_line": 1,
        "end_line": 1,
        "permalink": f"https://github.com/squne121/loop-protocol/blob/{'c' * 40}/{path}#L1-L1",
        "excerpt_sha256": "d" * 64,
        "anchor_text": None,
        "verification_status": "verified",
        "verification_method": "sha256_hash_match",
        "verified_at": "2026-05-23T15:30:45Z",
    }


def _run_end_to_end(*, run_id, claim, executors, budget, semantic_evaluator=None):
    dispatched: set = set()
    envelope = run_acquisition(
        claim=claim,
        run_id=run_id,
        executors=executors,
        budget=budget,
        dispatched_routes=dispatched,
        semantic_evaluator=semantic_evaluator or (lambda _claim, _refs: "supported"),
    )
    validation = validate_envelope(
        envelope,
        expected_claim_id=claim["claim_id"],
        expected_evidence_kind=claim["evidence_kind"],
    )
    assert validation["ok"], validation["errors"]
    action = decide_routing_action(envelope)
    return envelope, action


class TestEndToEndProceed:
    """Positive case: primary route succeeds -> proceed."""

    def test_primary_route_success_routes_to_proceed(self):
        claim = {"claim_id": "AC7-1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}
        envelope, action = _run_end_to_end(
            run_id="e2e-proceed",
            claim=claim,
            executors={
                "local_git": lambda: {
                    "acquisition_outcome": "succeeded",
                    "failure_domain": None,
                    "provider_failure_code": None,
                    "evidence_ref": _ref(),
                },
            },
            budget=RecoveryBudget(max_total=1, per_claim_max=1),
        )
        assert envelope["disposition"] == "proceed"
        assert action == {"action": "proceed", "claim_id": "AC7-1", "reason": "evidence_acquired_and_bound"}

    def test_cross_lane_recovery_success_routes_to_proceed(self):
        claim = {"claim_id": "AC7-2", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}
        envelope, action = _run_end_to_end(
            run_id="e2e-recover-success",
            claim=claim,
            executors={
                "local_git": lambda: {
                    "acquisition_outcome": "failed",
                    "failure_domain": "provider",
                    "provider_failure_code": "agy_lane_failure",
                    "evidence_ref": None,
                },
                "github_blob": lambda: {
                    "acquisition_outcome": "succeeded",
                    "failure_domain": None,
                    "provider_failure_code": None,
                    "evidence_ref": _ref(),
                },
            },
            budget=RecoveryBudget(max_total=1, per_claim_max=1),
        )
        assert envelope["disposition"] == "proceed"
        assert action["action"] == "proceed"
        assert len(envelope["attempts"]) == 2
        assert envelope["attempts"][1]["cross_lane_recovery"] is True


class TestEndToEndRecoverTerminal:
    """Negative cases: budget exhaustion -> recover; both lanes exhausted
    with content-level failure -> human_review; both lanes exhausted with
    purely operational failure -> environment_degraded."""

    def test_budget_exhausted_routes_to_recover(self):
        # Exhaust the run-wide budget on an unrelated first claim.
        budget = RecoveryBudget(max_total=1, per_claim_max=1)
        dispatched: set = set()
        run_acquisition(
            claim={"claim_id": "spender", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"},
            run_id="e2e-recover",
            executors={
                "local_git": lambda: {
                    "acquisition_outcome": "failed",
                    "failure_domain": "provider",
                    "provider_failure_code": "x",
                    "evidence_ref": None,
                },
                "github_blob": lambda: {
                    "acquisition_outcome": "failed",
                    "failure_domain": "provider",
                    "provider_failure_code": "y",
                    "evidence_ref": None,
                },
            },
            budget=budget,
            dispatched_routes=dispatched,
        )

        claim = {"claim_id": "AC7-3", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}
        envelope = run_acquisition(
            claim=claim,
            run_id="e2e-recover",
            executors={
                "local_git": lambda: {
                    "acquisition_outcome": "failed",
                    "failure_domain": "provider",
                    "provider_failure_code": "z",
                    "evidence_ref": None,
                },
                "github_blob": lambda: {
                    "acquisition_outcome": "succeeded",
                    "failure_domain": None,
                    "provider_failure_code": None,
                    "evidence_ref": _ref(),
                },
            },
            budget=budget,
            dispatched_routes=dispatched,
        )
        validation = validate_envelope(envelope, expected_claim_id="AC7-3")
        assert validation["ok"], validation["errors"]
        action = decide_routing_action(envelope)
        assert action["action"] == "recover"
        assert envelope["evidence_refs"] == []  # alternate lane never actually dispatched

    def test_all_routes_content_failure_routes_to_human_review(self):
        claim = {"claim_id": "AC7-4", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}
        envelope, action = _run_end_to_end(
            run_id="e2e-human-review",
            claim=claim,
            executors={
                "local_git": lambda: {
                    "acquisition_outcome": "failed",
                    "failure_domain": "reference_validation",
                    "provider_failure_code": "line_range_out_of_bounds",
                    "evidence_ref": None,
                },
                "github_blob": lambda: {
                    "acquisition_outcome": "failed",
                    "failure_domain": "source_lookup",
                    "provider_failure_code": "not_found",
                    "evidence_ref": None,
                },
            },
            budget=RecoveryBudget(max_total=1, per_claim_max=1),
        )
        assert envelope["disposition"] == "human_review"
        assert action["action"] == "human_review"
        assert envelope["terminal_artifact"]["unresolved_reason"] == "all_eligible_routes_failed"

    def test_all_routes_operational_failure_routes_to_environment_degraded(self):
        claim = {"claim_id": "AC7-5", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}
        envelope, action = _run_end_to_end(
            run_id="e2e-env-degraded",
            claim=claim,
            executors={
                "local_git": lambda: {
                    "acquisition_outcome": "failed",
                    "failure_domain": "transport",
                    "provider_failure_code": "network_error",
                    "evidence_ref": None,
                },
                "github_blob": lambda: {
                    "acquisition_outcome": "unknown_outcome",
                    "failure_domain": None,
                    "provider_failure_code": "gh_timeout",
                    "evidence_ref": None,
                },
            },
            budget=RecoveryBudget(max_total=1, per_claim_max=1),
        )
        assert envelope["disposition"] == "environment_degraded"
        assert action["action"] == "environment_degraded"
        # not_evaluated semantic_verdict, never mistaken for a claim-level
        # semantic resolution (transport failure != claim unresolved).
        assert envelope["semantic_verdict"] == "not_evaluated"

    def test_no_eligible_route_routes_to_human_review(self):
        claim = {"claim_id": "AC7-6", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}
        dispatched: set = set()
        envelope = run_acquisition(
            claim=claim,
            run_id="e2e-no-route",
            executors={},
            budget=RecoveryBudget(max_total=1, per_claim_max=1),
            dispatched_routes=dispatched,
            capability_snapshot={"local_git": False, "github_blob": False},
        )
        validation = validate_envelope(envelope, expected_claim_id="AC7-6")
        assert validation["ok"], validation["errors"]
        action = decide_routing_action(envelope)
        assert envelope["disposition"] == "human_review"
        assert action["action"] == "human_review"
        assert envelope["terminal_artifact"]["unresolved_reason"] == "no_eligible_route"
