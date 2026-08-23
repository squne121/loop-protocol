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

import json
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

    def test_evidence_acquired_without_evaluator_is_not_evaluated_not_proceed(self):
        """Acquiring evidence bytes is not the same thing as the claim
        being semantically supported: without an injected evaluator, the
        verdict must stay 'not_evaluated' and must never auto-promote to
        'supported' / 'proceed' (#2195 PR #2315 review fix)."""
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
        assert envelope["semantic_verdict"] == "not_evaluated"
        assert envelope["disposition"] != "proceed"
        assert envelope["disposition"] == "human_review"

    def test_evidence_acquired_with_evaluator_supported_routes_to_proceed(self):
        budget = RecoveryBudget(max_total=0, per_claim_max=0)
        dispatched: set = set()
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}

        envelope = run_acquisition(
            claim=claim,
            run_id="run-sv3",
            executors={"local_git": _executor("succeeded", evidence_ref=_succeeding_ref())},
            budget=budget,
            dispatched_routes=dispatched,
            semantic_evaluator=lambda _claim, _refs: "supported",
        )
        assert envelope["semantic_verdict"] == "supported"
        assert envelope["disposition"] == "proceed"

    def test_dispositive_insufficient_verdict_routes_to_human_review(self):
        budget = RecoveryBudget(max_total=0, per_claim_max=0)
        dispatched: set = set()
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}

        envelope = run_acquisition(
            claim=claim,
            run_id="run-sv4",
            executors={"local_git": _executor("succeeded", evidence_ref=_succeeding_ref())},
            budget=budget,
            dispatched_routes=dispatched,
            semantic_evaluator=lambda _claim, _refs: "insufficient",
        )
        assert envelope["semantic_verdict"] == "insufficient"
        assert envelope["disposition"] == "human_review"

    def test_supporting_insufficient_verdict_may_proceed(self):
        budget = RecoveryBudget(max_total=0, per_claim_max=0)
        dispatched: set = set()
        claim = {"claim_id": "C1", "claim_kind": "supporting", "evidence_kind": "repo_blob_at_commit"}

        envelope = run_acquisition(
            claim=claim,
            run_id="run-sv5",
            executors={"local_git": _executor("succeeded", evidence_ref=_succeeding_ref())},
            budget=budget,
            dispatched_routes=dispatched,
            semantic_evaluator=lambda _claim, _refs: "insufficient",
        )
        assert envelope["semantic_verdict"] == "insufficient"
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
            semantic_evaluator=lambda _claim, _refs: "supported",
        )
        assert envelope["disposition"] == "proceed"
        assert budget.remaining_total() == 0
        assert len(envelope["attempts"]) == 2

    def test_recovery_success_without_evaluator_is_human_review_not_proceed(self):
        """Same shape as above but with no evaluator injected: recovery
        still consumes the budget and still acquires evidence, but the
        disposition must reflect that the acquired evidence was never
        semantically evaluated (#2195 PR #2315 review fix)."""
        budget = RecoveryBudget(max_total=1, per_claim_max=1)
        dispatched: set = set()
        claim = {"claim_id": "C1", "claim_kind": "dispositive", "evidence_kind": "repo_blob_at_commit"}

        envelope = run_acquisition(
            claim=claim,
            run_id="run-rb2",
            executors={
                "local_git": _executor("failed", failure_domain="provider"),
                "github_blob": _executor("succeeded", evidence_ref=_succeeding_ref()),
            },
            budget=budget,
            dispatched_routes=dispatched,
        )
        assert envelope["semantic_verdict"] == "not_evaluated"
        assert envelope["disposition"] == "human_review"
        assert len(envelope["evidence_refs"]) == 1
        assert budget.remaining_total() == 0

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


class TestSourceEvidenceAdapterCliSmoke:
    """AC7 / mandate 4 (#2195 PR #2315 review fix): the producer and
    consumer are actually reachable from a single subprocess invocation
    (`source_evidence_adapter_cli.py`), not just importable Python
    functions wired together only inside unit tests. Uses a fixture
    request that points `local_git`'s collector at this very repository
    (a stable, always-present file/commit) so the test needs no network
    access and no live GitHub / Serena / Gemini call."""

    def _repo_root(self) -> Path:
        return Path(__file__).resolve().parents[4]

    def _head_commit_sha(self) -> str:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self._repo_root(),
            capture_output=True,
            check=True,
            text=True,
        )
        return result.stdout.strip()

    def test_adapter_cli_end_to_end_local_git_success(self, tmp_path):
        import subprocess

        commit_sha = self._head_commit_sha()
        request = {
            "run_id": "adapter-smoke-run",
            "claim": {
                "claim_id": "ADAPTER-SMOKE-1",
                "claim_kind": "dispositive",
                "evidence_kind": "repo_blob_at_commit",
                "dependency_group": None,
                "baseline": {"claim_text_digest": "smoke"},
                "commit_sha": commit_sha,
                "path": "CLAUDE.md",
                "start_line": 1,
                "end_line": 1,
            },
            "repo_root": str(self._repo_root()),
            "capability_snapshot": {"local_git": True, "github_blob": False},
        }
        request_file = tmp_path / "request.json"
        state_file = tmp_path / "state.json"
        output_file = tmp_path / "output.json"
        request_file.write_text(json.dumps(request), encoding="utf-8")

        adapter_script = (
            Path(__file__).resolve().parent.parent / "scripts" / "source_evidence_adapter_cli.py"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(adapter_script),
                "--request-file",
                str(request_file),
                "--state-file",
                str(state_file),
                "--output-file",
                str(output_file),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

        output = json.loads(output_file.read_text(encoding="utf-8"))
        assert output["validation"]["ok"], output["validation"]["errors"]
        envelope = output["envelope"]
        assert envelope["schema"] == "source_evidence_acquisition_result/v1"
        assert envelope["attempts"][0]["lane"] == "local_git"
        assert envelope["attempts"][0]["acquisition_outcome"] == "succeeded"
        # No semantic_evaluator was wired in this smoke test, so the
        # verdict must stay not_evaluated (never auto-promoted).
        assert envelope["semantic_verdict"] == "not_evaluated"
        assert output["routing_action"]["action"] == "human_review"

        # State file must have been persisted for a follow-up invocation
        # within the same run to observe the no-redispatch ledger.
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert any(
            entry[0] == "adapter-smoke-run" and entry[1] == "ADAPTER-SMOKE-1"
            for entry in state["dispatched_routes"]
        )

    def test_adapter_cli_missing_request_file_is_usage_error(self, tmp_path):
        import subprocess

        adapter_script = (
            Path(__file__).resolve().parent.parent / "scripts" / "source_evidence_adapter_cli.py"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(adapter_script),
                "--request-file",
                str(tmp_path / "does-not-exist.json"),
                "--state-file",
                str(tmp_path / "state.json"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1


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
