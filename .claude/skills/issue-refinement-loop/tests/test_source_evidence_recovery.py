"""
Tests for issue-refinement-loop's SOURCE_EVIDENCE_ACQUISITION_RESULT_V1
router (#2195): envelope validation, claim/baseline binding, run-scoped
cross_lane_recovery_budget bookkeeping, and disposition -> routing action
mapping.

AC1: failure_domain categories are all representable, REPO_EVIDENCE_REF_V1
     field set is unchanged.
AC2: semantic_verdict is bound to claim; unresolved operational failure is
     not_evaluated, never a resolved semantic value.
AC3: cross-lane recovery budget is enforced (run-wide max_total, per-claim
     max), and route plan only offers lanes producing the same
     evidence_kind.
AC5: dispatch_state outcome_unknown is treated as attempted; no re-dispatch
     of the same run_id/claim_id/route_id.
AC6: terminal artifact is bounded and secret/path free.
"""

import sys
from pathlib import Path

import pytest

scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))
gemini_scripts_dir = (
    Path(__file__).resolve().parents[2] / "gemini-cli-headless-delegation" / "scripts"
)
sys.path.insert(0, str(gemini_scripts_dir))

from route_source_evidence_result import (  # noqa: E402
    decide_routing_action,
    reconcile_budget_consumption,
    validate_envelope,
)
from source_evidence_acquisition import (  # noqa: E402
    FAILURE_DOMAINS,
    RecoveryBudget,
    build_route_plan,
    build_terminal_artifact,
    run_acquisition,
)


def _executor(outcome, *, failure_domain=None, provider_failure_code=None, evidence_ref=None):
    def _fn():
        return {
            "acquisition_outcome": outcome,
            "failure_domain": failure_domain,
            "provider_failure_code": provider_failure_code,
            "evidence_ref": evidence_ref,
        }

    return _fn


def _succeeding_ref(path="docs/adr/0001.md"):
    return {
        "type": "REPO_EVIDENCE_REF_V1",
        "commit_sha": "a" * 40,
        "object_format": "sha1",
        "path": path,
        "start_line": 1,
        "end_line": 1,
        "permalink": f"https://github.com/squne121/loop-protocol/blob/{'a' * 40}/{path}#L1-L1",
        "excerpt_sha256": "b" * 64,
        "anchor_text": None,
        "verification_status": "verified",
        "verification_method": "sha256_hash_match",
        "verified_at": "2026-05-23T15:30:45Z",
    }


class TestRoutePlanGeneration:
    """AC3: route plan generation is evidence_kind / capability based, and
    only lanes producing the same evidence_kind are recovery candidates."""

    def test_repo_blob_at_commit_offers_local_git_and_github_blob(self):
        plan = build_route_plan("repo_blob_at_commit")
        lanes = [r["lane"] for r in plan]
        assert lanes == ["local_git", "github_blob"]
        assert all(r["evidence_kind"] == "repo_blob_at_commit" for r in plan)

    def test_unknown_evidence_kind_has_no_routes(self):
        plan = build_route_plan("some_unregistered_evidence_kind")
        assert plan == []

    def test_capability_snapshot_missing_lane_is_not_eligible(self):
        plan = build_route_plan("repo_blob_at_commit", capability_snapshot={"local_git": True})
        by_lane = {r["lane"]: r for r in plan}
        assert by_lane["local_git"]["eligible"] is True
        assert by_lane["github_blob"]["eligible"] is False
        assert by_lane["github_blob"]["reason"] == "capability_unavailable"


class TestFailureDomainCoverage:
    """AC1: every failure_domain category (including null) is representable
    in the envelope without touching REPO_EVIDENCE_REF_V1's field set."""

    @pytest.mark.parametrize("failure_domain", FAILURE_DOMAINS)
    def test_each_failure_domain_produces_zero_evidence_refs_envelope(self, failure_domain):
        budget = RecoveryBudget(max_total=0, per_claim_max=0)
        dispatched: set = set()
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}

        envelope = run_acquisition(
            claim=claim,
            run_id="run-fd",
            executors={"local_git": _executor("failed", failure_domain=failure_domain)},
            budget=budget,
            dispatched_routes=dispatched,
        )

        assert envelope["evidence_refs"] == []
        assert envelope["attempts"][0]["failure_domain"] == failure_domain
        validation = validate_envelope(envelope)
        assert validation["ok"], validation["errors"]

    def test_null_failure_domain_on_unknown_outcome(self):
        budget = RecoveryBudget(max_total=0, per_claim_max=0)
        dispatched: set = set()
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}

        envelope = run_acquisition(
            claim=claim,
            run_id="run-null",
            executors={"local_git": _executor("unknown_outcome")},
            budget=budget,
            dispatched_routes=dispatched,
        )
        assert envelope["attempts"][0]["failure_domain"] is None
        assert envelope["attempts"][0]["dispatch_state"] == "outcome_unknown"


class TestSemanticVerdictBinding:
    """AC2: semantic_verdict is bound to the claim; operational failure is
    never surfaced as a resolved semantic verdict."""

    def test_no_evidence_yields_not_evaluated(self):
        budget = RecoveryBudget(max_total=0, per_claim_max=0)
        dispatched: set = set()
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}

        envelope = run_acquisition(
            claim=claim,
            run_id="run-sv",
            executors={"local_git": _executor("failed", failure_domain="transport")},
            budget=budget,
            dispatched_routes=dispatched,
        )
        assert envelope["semantic_verdict"] == "not_evaluated"
        validation = validate_envelope(envelope)
        assert validation["ok"], validation["errors"]

    def test_evidence_acquired_defaults_to_supported_without_evaluator(self):
        budget = RecoveryBudget(max_total=0, per_claim_max=0)
        dispatched: set = set()
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}

        envelope = run_acquisition(
            claim=claim,
            run_id="run-sv2",
            executors={"local_git": _executor("succeeded", evidence_ref=_succeeding_ref())},
            budget=budget,
            dispatched_routes=dispatched,
        )
        assert envelope["semantic_verdict"] == "supported"
        assert envelope["disposition"] == "proceed"

    def test_validator_rejects_resolved_verdict_with_no_evidence(self):
        envelope = {
            "schema": "source_evidence_acquisition_result/v1",
            "claim": {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"},
            "baseline": {},
            "route_plan": [],
            "attempts": [],
            "evidence_refs": [],
            "semantic_verdict": "supported",
            "disposition": "human_review",
        }
        validation = validate_envelope(envelope)
        assert validation["ok"] is False
        assert any("not_evaluated" in e for e in validation["errors"])


class TestCrossLaneRecoveryBudget:
    """AC3: run-wide max_total and per-claim max are both enforced; budget
    is threaded across claims within one run."""

    def test_recovery_succeeds_when_budget_available(self):
        budget = RecoveryBudget(max_total=1, per_claim_max=1)
        dispatched: set = set()
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}

        envelope = run_acquisition(
            claim=claim,
            run_id="run-rb",
            executors={
                "local_git": _executor("failed", failure_domain="provider"),
                "github_blob": _executor("succeeded", evidence_ref=_succeeding_ref()),
            },
            budget=budget,
            dispatched_routes=dispatched,
        )
        assert envelope["disposition"] == "proceed"
        assert budget.remaining_total() == 0
        assert len(envelope["attempts"]) == 2

    def test_run_wide_budget_exhausted_across_two_claims(self):
        budget = RecoveryBudget(max_total=1, per_claim_max=1)
        dispatched: set = set()

        claim_a = {"claim_id": "A", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}
        envelope_a = run_acquisition(
            claim=claim_a,
            run_id="run-shared",
            executors={
                "local_git": _executor("failed", failure_domain="provider"),
                "github_blob": _executor("failed", failure_domain="provider"),
            },
            budget=budget,
            dispatched_routes=dispatched,
        )
        assert len(envelope_a["attempts"]) == 2  # cross-lane recovery consumed the only run-wide slot

        claim_b = {"claim_id": "B", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}
        envelope_b = run_acquisition(
            claim=claim_b,
            run_id="run-shared",
            executors={
                "local_git": _executor("failed", failure_domain="provider"),
                "github_blob": _executor("succeeded", evidence_ref=_succeeding_ref()),
            },
            budget=budget,
            dispatched_routes=dispatched,
        )
        # Budget exhausted by claim A: claim B's alternate lane is never
        # dispatched even though it would have succeeded.
        assert len(envelope_b["attempts"]) == 1
        assert envelope_b["disposition"] == "recover"
        action = decide_routing_action(envelope_b)
        assert action["action"] == "recover"

    def test_reconcile_budget_consumption_is_idempotent_for_shared_instance(self):
        budget = RecoveryBudget(max_total=1, per_claim_max=1)
        dispatched: set = set()
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}

        envelope = run_acquisition(
            claim=claim,
            run_id="run-idem",
            executors={
                "local_git": _executor("failed", failure_domain="provider"),
                "github_blob": _executor("succeeded", evidence_ref=_succeeding_ref()),
            },
            budget=budget,
            dispatched_routes=dispatched,
        )
        before = budget.remaining_total()
        reconcile_budget_consumption(envelope, budget)
        assert budget.remaining_total() == before  # no double consumption


class TestNoRedispatchAndOutcomeUnknown:
    """AC5: outcome_unknown is treated as attempted; identical
    run_id/claim_id/route_id is never dispatched twice."""

    def test_duplicate_dispatch_within_same_run_raises(self):
        budget = RecoveryBudget(max_total=0, per_claim_max=0)
        dispatched: set = set()
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}
        run_acquisition(
            claim=claim,
            run_id="run-dup",
            executors={"local_git": _executor("failed", failure_domain="provider")},
            budget=budget,
            dispatched_routes=dispatched,
        )
        with pytest.raises(ValueError, match="duplicate_dispatch_forbidden"):
            run_acquisition(
                claim=claim,
                run_id="run-dup",
                executors={"local_git": _executor("failed", failure_domain="provider")},
                budget=RecoveryBudget(max_total=0, per_claim_max=0),
                dispatched_routes=dispatched,
            )

    def test_different_run_id_allows_dispatch(self):
        dispatched: set = set()
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}
        run_acquisition(
            claim=claim,
            run_id="run-1",
            executors={"local_git": _executor("failed", failure_domain="provider")},
            budget=RecoveryBudget(max_total=0, per_claim_max=0),
            dispatched_routes=dispatched,
        )
        # A different run_id is a distinct dedupe key.
        run_acquisition(
            claim=claim,
            run_id="run-2",
            executors={"local_git": _executor("failed", failure_domain="provider")},
            budget=RecoveryBudget(max_total=0, per_claim_max=0),
            dispatched_routes=dispatched,
        )


class TestTerminalArtifact:
    """AC6: bounded (<= 16 KiB), no raw stderr/transcript/credential/absolute
    path."""

    def test_terminal_artifact_present_on_human_review(self):
        budget = RecoveryBudget(max_total=1, per_claim_max=1)
        dispatched: set = set()
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}
        envelope = run_acquisition(
            claim=claim,
            run_id="run-ta",
            executors={
                "local_git": _executor("failed", failure_domain="reference_validation"),
                "github_blob": _executor("failed", failure_domain="reference_validation"),
            },
            budget=budget,
            dispatched_routes=dispatched,
        )
        assert envelope["disposition"] == "human_review"
        artifact = envelope["terminal_artifact"]
        assert artifact["schema"] == "source_evidence_terminal_artifact/v1"
        import json

        size = len(json.dumps(artifact, ensure_ascii=False).encode("utf-8"))
        assert size <= 16 * 1024

    def test_terminal_artifact_rejects_credential_content(self):
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}
        with pytest.raises(ValueError, match="forbidden_content"):
            build_terminal_artifact(
                run_id="run-secret",
                claim=claim,
                attempts=[
                    {
                        "route_id": "local_git:repo_blob_at_commit",
                        "lane": "local_git",
                        "dispatch_state": "dispatched",
                        "acquisition_outcome": "failed",
                        "failure_domain": "provider",
                        "provider_failure_code": "auth failed with token ghp_abcdefghijklmnopqrst1234",
                    }
                ],
                evidence_refs=[],
                disposition="human_review",
                unresolved_reason="all_eligible_routes_failed",
            )

    def test_terminal_artifact_rejects_absolute_path_content(self):
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}
        with pytest.raises(ValueError, match="forbidden_content"):
            build_terminal_artifact(
                run_id="run-path",
                claim=claim,
                attempts=[
                    {
                        "route_id": "local_git:repo_blob_at_commit",
                        "lane": "local_git",
                        "dispatch_state": "dispatched",
                        "acquisition_outcome": "failed",
                        "failure_domain": "provider",
                        "provider_failure_code": "read failed at /home/runner/work/secret.txt",
                    }
                ],
                evidence_refs=[],
                disposition="human_review",
                unresolved_reason="all_eligible_routes_failed",
            )

    def test_environment_degraded_disposition_for_pure_operational_failure(self):
        budget = RecoveryBudget(max_total=1, per_claim_max=1)
        dispatched: set = set()
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}
        envelope = run_acquisition(
            claim=claim,
            run_id="run-env",
            executors={
                "local_git": _executor("failed", failure_domain="transport"),
                "github_blob": _executor("failed", failure_domain="tool_execution"),
            },
            budget=budget,
            dispatched_routes=dispatched,
        )
        assert envelope["disposition"] == "environment_degraded"
        action = decide_routing_action(envelope)
        assert action["action"] == "environment_degraded"


class TestEnvelopeValidationBinding:
    def test_claim_id_binding_mismatch_is_rejected(self):
        envelope = {
            "schema": "source_evidence_acquisition_result/v1",
            "claim": {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"},
            "baseline": {},
            "route_plan": [],
            "attempts": [],
            "evidence_refs": [],
            "semantic_verdict": "not_evaluated",
            "disposition": "human_review",
        }
        validation = validate_envelope(envelope, expected_claim_id="C2")
        assert validation["ok"] is False
        assert any("claim_id binding mismatch" in e for e in validation["errors"])

    def test_wrong_schema_is_rejected(self):
        validation = validate_envelope({"schema": "something_else/v1"})
        assert validation["ok"] is False
