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

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))
gemini_scripts_dir = Path(__file__).resolve().parents[2] / "gemini-cli-headless-delegation" / "scripts"
sys.path.insert(0, str(gemini_scripts_dir))

from route_source_evidence_result import (  # noqa: E402
    C1_CLAIM,
    C1_PATH,
    C1_REPO,
    c1_baseline,
    decide_effective_step1_action,
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


class _C1Fixture:
    """Isolated real-Git, two separate production CLI subprocess invocations."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        target = self.repo / C1_PATH
        target.parent.mkdir(parents=True)
        target.write_text(
            "def classify(latest_main_net_diff, allowed_paths):\n"
            "    if any(path not in allowed_paths for path in latest_main_net_diff):\n"
            "        return 'allowed_paths_conflict'\n",
            encoding="utf-8",
        )
        (self.repo / "other.py").write_bytes(target.read_bytes())
        subprocess.run(["git", "-C", str(self.repo), "add", C1_PATH, "other.py"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "user.name=Fixture",
                "commit",
                "-qm",
                "C1 fixture",
            ],
            check=True,
        )
        self.main = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "refs/heads/main"],
            text=True,
        ).strip()
        self.body = "#2889: independent pinned Issue text for exact C1"
        self.issue = tmp_path / "issue-body.txt"
        self.issue.write_text(self.body, encoding="utf-8")
        self.context = tmp_path / "context.json"
        self.context_data = {
            "repo": C1_REPO,
            "issue_number": 2889,
            "issue_body_sha256": hashlib.sha256(self.body.encode()).hexdigest(),
            "claim_id": "C1",
            "claim_text": C1_CLAIM,
            "canonical_main_sha": self.main,
            "c1_target": {"repo": C1_REPO, "path": C1_PATH, "start_line": 1, "end_line": 3},
        }
        self.write(self.context, self.context_data)
        self.request_file = tmp_path / "request.json"
        self.request_data = {
            "run_id": "fixture-run-a",
            "claim": {
                "claim_id": "C1",
                "claim_kind": "dispositive",
                "evidence_kind": "repo_blob_at_commit",
                "dependency_group": None,
                "baseline": c1_baseline(issue_body=self.body, main_sha=self.main),
                "commit_sha": self.main,
                "path": C1_PATH,
                "start_line": 1,
                "end_line": 3,
            },
            "repo_root": str(self.repo),
            "capability_snapshot": {"local_git": True, "github_blob": False},
        }
        self.write(self.request_file, self.request_data)
        self.state = tmp_path / "state.json"
        self.initial = tmp_path / "initial.json"
        self.resolved = tmp_path / "resolved.json"
        self.snapshot = tmp_path / "operator-snapshot.json"
        self.readback = tmp_path / "operator-readback.json"
        self.initial_pin = None  # fixed by Step 1 once; never re-derived on resolution
        self.script = scripts_dir / "source_evidence_adapter_cli.py"
        self.argv = [
            sys.executable,
            str(self.script),
            "--request-file",
            str(self.request_file),
            "--state-file",
            str(self.state),
            "--step1-context-file",
            str(self.context),
            "--issue-body-file",
            str(self.issue),
        ]
        self.first_argv = [*self.argv, "--output-file", str(self.initial)]
        self.second_argv = [
            *self.argv,
            "--resolution-only",
            "--prior-result-file",
            str(self.initial),
            "--output-file",
            str(self.resolved),
            "--operator-snapshot-file",
            str(self.snapshot),
            "--operator-readback-file",
            str(self.readback),
        ]

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True, indent=2), encoding="utf-8")

    def invoke(self, argv):
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)

    def acquire(self):
        call = self.invoke(self.first_argv)
        assert call.returncode == 0, call.stderr
        initial = json.loads(self.initial.read_bytes())
        assert initial["validation"]["ok"]
        assert initial["envelope"]["semantic_verdict"] == "not_evaluated"
        assert initial["envelope"]["disposition"] == "human_review"
        self.initial_pin = hashlib.sha256(self.initial.read_bytes()).hexdigest()
        return initial

    def select_operator(self, initial, *, decision="supported", override=None):
        ref = initial["envelope"]["evidence_refs"][0]
        recorded = datetime.fromisoformat(initial["persisted_at"]) + timedelta(microseconds=1)
        record = {
            "decision": decision,
            "recorded_at": recorded.isoformat(),
            "run_id": initial["initial_binding"]["run_id"],
            "claim_id": "C1",
            "envelope_sha256": initial["initial_binding"]["envelope_sha256"],
            "canonical_main_sha": self.main,
            "evidence": {
                "repo": C1_REPO,
                "commit_sha": self.main,
                "path": C1_PATH,
                "start_line": 1,
                "end_line": 3,
                "excerpt_sha256": ref["excerpt_sha256"],
            },
        }
        if override:
            override(record)
        body = json.dumps(record, sort_keys=True)
        snapshot = {
            "lane": "with_human_context",
            "issue_number": 2889,
            "comment_id": 12345,
            "user_id": 123,
            "body": body,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "updated_at": (recorded + timedelta(microseconds=1)).isoformat(),
        }
        self.write(self.snapshot, snapshot)
        self.write(self.readback, {key: value for key, value in snapshot.items() if key != "lane"})
        return record

    def resolve(self, *, pin=None):
        pin = pin if pin is not None else self.initial_pin
        assert pin is not None, "Step 1 must pin the first result before resolution"
        return self.invoke([*self.second_argv, "--expected-result-sha256", pin])

    def pin_test_candidate(self, envelope):
        """Test-only adversarial candidate: make every byte/binding pin consistent.

        This is NOT the positive initial-acquisition path. Its separately
        recorded test pin isolates the C1 target guard from the digest guard.
        """
        initial = json.loads(self.initial.read_bytes())
        state = json.loads(self.state.read_bytes())
        initial["envelope"] = envelope
        binding = {
            "run_id": self.request_data["run_id"],
            "claim_id": "C1",
            "envelope_sha256": hashlib.sha256(
                json.dumps(envelope, sort_keys=True, indent=2, ensure_ascii=False).encode()
            ).hexdigest(),
        }
        state["resolution_bindings"] = [binding]
        self.write(self.state, state)
        initial["initial_binding"] = binding
        initial["state_sha256"] = hashlib.sha256(self.state.read_bytes()).hexdigest()
        initial["routing_action"] = decide_routing_action(envelope)
        self.initial.write_bytes(json.dumps(initial, sort_keys=True, indent=2, ensure_ascii=False).encode())
        self.initial_pin = hashlib.sha256(self.initial.read_bytes()).hexdigest()
        return initial

    def assert_stopped(self, *, pin=None):
        before_state = self.state.read_bytes() if self.state.exists() else None
        before_first = self.initial.read_bytes() if self.initial.exists() else None
        result = self.resolve(pin=pin)
        assert result.returncode != 0 or json.loads(result.stdout)["effective_step1_action"] != "proceed", result.stdout
        assert (self.state.read_bytes() if self.state.exists() else None) == before_state
        assert (self.initial.read_bytes() if self.initial.exists() else None) == before_first
        return result

    def evidence_artifact(self, *, scenario, initial, resolved, first_code, second_code, before_state):
        """Public-safe runtime evidence, emitted into the current worktree only."""
        root = Path(
            os.environ.get("SOURCE_EVIDENCE_RUNTIME_ARTIFACT_DIR", Path(__file__).resolve().parents[4] / "artifacts")
        )
        root.mkdir(parents=True, exist_ok=True)
        assert root.resolve().is_relative_to(Path(__file__).resolve().parents[4].resolve())
        raw_state = self.state.read_bytes()
        raw_result = self.initial.read_bytes()
        artifact = {
            "scenario": scenario,
            "verification_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[4], text=True
            ).strip(),
            "canonical_main_pin": self.main,
            "first_cli_argv": [
                "<python>",
                "source_evidence_adapter_cli.py",
                "--request-file",
                "<fixture>",
                "--state-file",
                "<fixture>",
                "--step1-context-file",
                "<fixture>",
                "--issue-body-file",
                "<fixture>",
                "--output-file",
                "<fixture>",
            ],
            "second_cli_argv": [
                "<python>",
                "source_evidence_adapter_cli.py",
                "--resolution-only",
                "--prior-result-file",
                "<fixture>",
                "--expected-result-sha256",
                "<pinned-digest>",
                "--operator-snapshot-file",
                "<fixture>",
                "--operator-readback-file",
                "<fixture>",
            ],
            "first_exit_code": first_code,
            "second_exit_code": second_code,
            "first_verifier_ok": initial["validation"]["ok"],
            "second_verifier_ok": resolved["validation"]["ok"],
            "first_dispatch": initial["runtime_counts"],
            "second_dispatch": resolved["runtime_counts"],
            "initial_state_byte_sha256": hashlib.sha256(before_state).hexdigest(),
            "second_state_byte_sha256": hashlib.sha256(raw_state).hexdigest(),
            "result_recorded_state_byte_sha256": initial["state_sha256"],
            "independently_pinned_initial_result_byte_sha256": self.initial_pin,
            "initial_result_byte_sha256_after_resolution": hashlib.sha256(raw_result).hexdigest(),
            "first_envelope_byte_sha256": initial["initial_binding"]["envelope_sha256"],
            "second_envelope_byte_sha256": resolved["initial_binding"]["envelope_sha256"],
            "run_id": initial["initial_binding"]["run_id"],
            "claim_id": initial["initial_binding"]["claim_id"],
            "ledger_unchanged": before_state == raw_state,
            "operator_receipt": resolved["operator_receipt"],
            "verified_evidence_tuple": {
                "repo": C1_REPO,
                "commit_sha": self.main,
                "path": C1_PATH,
                "start_line": 1,
                "end_line": 3,
                "excerpt_sha256": initial["envelope"]["evidence_refs"][0]["excerpt_sha256"],
                "verifier_status": initial["envelope"]["evidence_refs"][0]["verification_status"],
            },
            "base_action": resolved["routing_action"]["action"],
            "effective_step1_action": resolved["effective_step1_action"],
            "next_claim": "only after exact C1; other human_review remains stopped",
        }
        self.write(root / f"source-evidence-{scenario}.json", artifact)

    def rejection_artifact(self, scenario, outcomes):
        """Persist bounded public-safe negative subprocess outcomes, not raw input."""
        root = Path(__file__).resolve().parents[4] / "artifacts"
        root.mkdir(parents=True, exist_ok=True)
        assert root.resolve().is_relative_to(Path(__file__).resolve().parents[4].resolve())
        self.write(
            root / f"source-evidence-{scenario}.json",
            {
                "scenario": scenario,
                "verification_head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[4], text=True
                ).strip(),
                "canonical_main_pin": self.main,
                "cli": "source_evidence_adapter_cli.py --resolution-only --expected-result-sha256 <independent-pin>",
                "negative_outcomes": outcomes,
                "proceed_count": 0,
                "resolution_only_redispatch_count": 0,
                "state_byte_sha256": hashlib.sha256(self.state.read_bytes()).hexdigest()
                if self.state.exists()
                else None,
                "result_byte_sha256": hashlib.sha256(self.initial.read_bytes()).hexdigest()
                if self.initial.exists()
                else None,
            },
        )


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

    def test_other_human_review_stops_without_redispatch(self, tmp_path):
        fixture = _C1Fixture(tmp_path)
        first = fixture.acquire()
        original_state = fixture.state.read_bytes()
        fixture.select_operator(first)
        for decision in ("refuted", "inconclusive", "unknown"):
            fixture.select_operator(first, decision=decision)
            response = fixture.assert_stopped()
            assert response.returncode == 2, response.stderr
            result = json.loads(response.stdout)
            assert result["routing_action"]["action"] == "human_review"
            assert result["effective_step1_action"] == "human_review"
            assert result["runtime_counts"] == {"run_acquisition": 0, "collector_dispatch": 0}
        fixture.snapshot.unlink()
        fixture.assert_stopped()
        fixture.select_operator(first)
        request = json.loads(fixture.request_file.read_bytes())
        request["claim"]["claim_id"] = "C2"
        fixture.write(fixture.request_file, request)
        fixture.assert_stopped()
        assert fixture.state.read_bytes() == original_state

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

        adapter_script = Path(__file__).resolve().parent.parent / "scripts" / "source_evidence_adapter_cli.py"
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
            entry[0] == "adapter-smoke-run" and entry[1] == "ADAPTER-SMOKE-1" for entry in state["dispatched_routes"]
        )

    def test_operator_c1_two_phase_real_git_cli(self, tmp_path):
        fixture = _C1Fixture(tmp_path)
        initial = fixture.acquire()
        before = fixture.state.read_bytes()
        before_result = fixture.initial.read_bytes()
        first_envelope_bytes = json.dumps(initial["envelope"], indent=2, sort_keys=True, ensure_ascii=False).encode()
        assert hashlib.sha256(first_envelope_bytes).hexdigest() == initial["initial_binding"]["envelope_sha256"]
        assert hashlib.sha256(before).hexdigest() == initial["state_sha256"]
        assert initial["runtime_counts"] == {"run_acquisition": 1, "collector_dispatch": 1}
        assert initial["routing_action"]["action"] == "human_review"
        fixture.select_operator(initial)
        second = fixture.resolve()
        assert second.returncode == 0, second.stderr
        resolved = json.loads(second.stdout)
        assert resolved["validation"]["ok"]
        assert resolved["routing_action"]["action"] == "human_review"
        assert resolved["envelope"]["disposition"] == "human_review"
        assert resolved["envelope"]["semantic_verdict"] == "not_evaluated"
        assert resolved["effective_step1_action"] == "proceed"
        assert resolved["claim_resolution"] == "supported"
        assert resolved["runtime_counts"] == {"run_acquisition": 0, "collector_dispatch": 0}
        assert fixture.state.read_bytes() == before
        assert fixture.initial.read_bytes() == before_result
        assert fixture.initial.read_bytes() != fixture.resolved.read_bytes()
        assert resolved["envelope"] == initial["envelope"]
        fixture.evidence_artifact(
            scenario="c1-two-phase",
            initial=initial,
            resolved=resolved,
            first_code=0,
            second_code=second.returncode,
            before_state=before,
        )

    def test_operator_resolution_rejects_cross_run_or_tampered_first_result_cli(self, tmp_path):
        fixture = _C1Fixture(tmp_path)
        # No initial acquisition: fail closed without synthesizing a state.
        missing = fixture.assert_stopped(pin="0" * 64)
        assert not fixture.state.exists()
        first = fixture.acquire()
        fixture.select_operator(first)
        pin = hashlib.sha256(fixture.initial.read_bytes()).hexdigest()
        initial_bytes = fixture.initial.read_bytes()
        initial_state = fixture.state.read_bytes()
        tampered = json.loads(initial_bytes)
        tampered["initial_binding"]["envelope_sha256"] = "a" * 64
        fixture.write(fixture.initial, tampered)
        tamper_response = fixture.assert_stopped(pin=pin)
        assert "result byte SHA" in tamper_response.stderr
        fixture.initial.write_bytes(initial_bytes)
        request = dict(fixture.request_data)
        request["run_id"] = "other-run"
        fixture.write(fixture.request_file, request)
        cross_run = fixture.assert_stopped(pin=pin)
        assert fixture.state.read_bytes() == initial_state
        fixture.write(fixture.request_file, fixture.request_data)
        # A separately acquired, internally valid result/state for another run is
        # not the root-pinned original, even when the candidate is self-consistent.
        other_result = fixture.root / "other-result.json"
        other_state = fixture.root / "other-state.json"
        other_request = fixture.root / "other-request.json"
        request["run_id"] = "valid-other-run"
        fixture.write(other_request, request)
        other_argv = [
            sys.executable,
            str(fixture.script),
            "--request-file",
            str(other_request),
            "--state-file",
            str(other_state),
            "--output-file",
            str(other_result),
            "--step1-context-file",
            str(fixture.context),
            "--issue-body-file",
            str(fixture.issue),
        ]
        acquired = fixture.invoke(other_argv)
        assert acquired.returncode == 0, acquired.stderr
        fixture.initial.write_bytes(other_result.read_bytes())
        fixture.state.write_bytes(other_state.read_bytes())
        joint = fixture.assert_stopped(pin=pin)
        assert "result byte SHA" in joint.stderr
        assert initial_bytes != other_result.read_bytes()
        fixture.rejection_artifact(
            "cross-run",
            {
                "missing_initial": missing.returncode,
                "tampered_result": tamper_response.returncode,
                "wrong_run": cross_run.returncode,
                "joint_valid_other_run": joint.returncode,
                "independent_expected_result_sha256": pin,
                "joint_candidate_result_sha256": hashlib.sha256(other_result.read_bytes()).hexdigest(),
                "joint_candidate_self_consistent": json.loads(other_result.read_bytes())["state_sha256"]
                == hashlib.sha256(other_state.read_bytes()).hexdigest(),
                "independent_result_pin_rejected_joint_swap": True,
            },
        )

    def test_operator_resolution_rejects_ledger_tamper_and_joint_swap_cli(self, tmp_path):
        fixture = _C1Fixture(tmp_path)
        first = fixture.acquire()
        fixture.select_operator(first)
        original_state = fixture.state.read_bytes()
        pin = hashlib.sha256(fixture.initial.read_bytes()).hexdigest()
        state = json.loads(original_state)
        rejected = {}
        for name, mutation in (
            (
                "dispatched_routes",
                lambda s: s["dispatched_routes"].append(["run-x", "C9", "local_git:repo_blob_at_commit"]),
            ),
            ("consumed_budget", lambda s: s["budget"].update({"_consumed_total": 1})),
        ):
            state = json.loads(original_state)
            mutation(state)
            fixture.write(fixture.state, state)
            result = fixture.assert_stopped(pin=pin)
            assert "state full byte SHA" in result.stderr
            rejected[name] = result.returncode
            fixture.state.write_bytes(original_state)
        # Even when a tampered state is paired with a newly consistent result,
        # the independent first-result pin is not recomputed from the pair.
        state = json.loads(original_state)
        state["budget"]["_consumed_total"] = 1
        fixture.write(fixture.state, state)
        candidate = json.loads(fixture.initial.read_bytes())
        candidate["state_sha256"] = hashlib.sha256(fixture.state.read_bytes()).hexdigest()
        fixture.write(fixture.initial, candidate)
        joint = fixture.assert_stopped(pin=pin)
        assert "result byte SHA" in joint.stderr
        fixture.rejection_artifact(
            "ledger-tamper",
            {
                **rejected,
                "joint_state_result_tamper": joint.returncode,
                "independent_expected_result_sha256": pin,
                "candidate_result_sha256": hashlib.sha256(fixture.initial.read_bytes()).hexdigest(),
            },
        )

    def test_operator_resolution_rejects_old_commit_or_wrong_c1_target_cli(self, tmp_path):
        fixture = _C1Fixture(tmp_path)
        first = fixture.acquire()
        fixture.select_operator(first)
        # A correct excerpt hash from a previous commit is still stale versus main.
        source = fixture.repo / C1_PATH
        source.write_text(source.read_text() + "# main advanced\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(fixture.repo), "add", C1_PATH], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(fixture.repo),
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "user.name=Fixture",
                "commit",
                "-qm",
                "advance main",
            ],
            check=True,
        )
        old_commit = fixture.assert_stopped()
        assert "main ref drift" in old_commit.stderr
        # Restore the isolated fixture's main for independent target negatives.
        subprocess.run(
            ["git", "-C", str(fixture.repo), "reset", "--hard", fixture.main], check=True, capture_output=True
        )
        original = dict(fixture.context_data)
        negatives = {"old_commit_hash_valid": old_commit.returncode}
        for key, value in (("repo", "wrong/repo"), ("path", "another.py"), ("start_line", 2)):
            context = json.loads(json.dumps(original))
            if key == "repo":
                context["c1_target"]["repo"] = value
            else:
                context["c1_target"][key] = value
            fixture.write(fixture.context, context)
            negatives[f"wrong_{key}"] = fixture.assert_stopped().returncode
        fixture.write(fixture.context, original)
        # Defense-in-depth: even if an alternate first-result bundle is
        # independently pinned and its result/state/envelope digests are all
        # coherent, hash-valid evidence outside the root-pinned C1 target
        # must not pass the production second-subprocess consumer.
        from copy import deepcopy
        from validate_repo_evidence_ref import validate_repo_evidence_ref

        original_result = fixture.initial.read_bytes()
        original_state = fixture.state.read_bytes()
        original_pin = fixture.initial_pin
        ref_original = first["envelope"]["evidence_refs"][0]
        for kind in ("path", "repo", "line_range"):
            fixture.initial.write_bytes(original_result)
            fixture.state.write_bytes(original_state)
            fixture.initial_pin = original_pin
            ref = deepcopy(ref_original)
            decision_changes = {}
            if kind == "path":
                ref["path"] = "other.py"  # identical bytes -> identical valid excerpt SHA
                decision_changes["path"] = "other.py"
            elif kind == "repo":
                decision_changes["repo"] = "someone/else"
            else:
                ref["end_line"] = 2
                excerpt = b"\n".join((fixture.repo / C1_PATH).read_bytes().splitlines()[:2]) + b"\n"
                ref["excerpt_sha256"] = hashlib.sha256(excerpt).hexdigest()
                decision_changes.update({"end_line": 2, "excerpt_sha256": ref["excerpt_sha256"]})
            owner_repo = "someone/else" if kind == "repo" else C1_REPO
            ref["permalink"] = (
                f"https://github.com/{owner_repo}/blob/{fixture.main}/{ref['path']}"
                f"#L{ref['start_line']}-L{ref['end_line']}"
            )
            assert validate_repo_evidence_ref(ref, repo_root=fixture.repo)["status"] == "verified"
            envelope = deepcopy(first["envelope"])
            envelope["evidence_refs"] = [ref]
            candidate = fixture.pin_test_candidate(envelope)
            fixture.select_operator(candidate, override=lambda d: d["evidence"].update(decision_changes))
            rejected = fixture.assert_stopped()
            assert rejected.returncode == 2, rejected.stderr
            assert "evidence ref" in rejected.stdout
            negatives[f"hash_valid_wrong_ref_{kind}"] = rejected.returncode
        # A correct old-commit excerpt also fails when the independently pinned
        # main advances, even with fully coherent alternate result/state bytes.
        fixture.initial.write_bytes(original_result)
        fixture.state.write_bytes(original_state)
        fixture.initial_pin = original_pin
        (fixture.repo / "advance.txt").write_text("new main", encoding="utf-8")
        subprocess.run(["git", "-C", str(fixture.repo), "add", "advance.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(fixture.repo),
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "user.name=Fixture",
                "commit",
                "-qm",
                "advance for old commit guard",
            ],
            check=True,
        )
        new_main = subprocess.check_output(
            ["git", "-C", str(fixture.repo), "rev-parse", "refs/heads/main"],
            text=True,
        ).strip()
        fixture.main = new_main
        fixture.context_data["canonical_main_sha"] = new_main
        fixture.write(fixture.context, fixture.context_data)
        fixture.request_data["claim"]["baseline"] = c1_baseline(issue_body=fixture.body, main_sha=new_main)
        fixture.request_data["claim"]["commit_sha"] = new_main
        fixture.write(fixture.request_file, fixture.request_data)
        envelope = deepcopy(first["envelope"])
        envelope["baseline"] = fixture.request_data["claim"]["baseline"]
        candidate = fixture.pin_test_candidate(envelope)
        fixture.select_operator(
            candidate, override=lambda d: d["evidence"].update({"commit_sha": ref_original["commit_sha"]})
        )
        rejected = fixture.assert_stopped()
        assert rejected.returncode == 2, rejected.stderr
        assert "evidence ref does not match independent" in rejected.stdout
        negatives["hash_valid_old_ref_with_new_main_pin"] = rejected.returncode
        fixture.rejection_artifact("old-commit-wrong-target", negatives)

    def test_operator_resolution_rejects_authentic_old_tuple_receipt_cli(self, tmp_path):
        fixture = _C1Fixture(tmp_path)
        old = fixture.acquire()
        old_decision = fixture.select_operator(old)
        old_receipt = fixture.snapshot.read_bytes()
        old_readback = fixture.readback.read_bytes()
        # Advance the isolated canonical main; C1's excerpt is unchanged, so
        # the prior receipt is genuine for the old tuple, not for new main.
        (fixture.repo / "later.txt").write_text("later", encoding="utf-8")
        subprocess.run(["git", "-C", str(fixture.repo), "add", "later.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(fixture.repo),
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "user.name=Fixture",
                "commit",
                "-qm",
                "new canonical main",
            ],
            check=True,
        )
        new_main = subprocess.check_output(
            ["git", "-C", str(fixture.repo), "rev-parse", "refs/heads/main"],
            text=True,
        ).strip()
        fixture.main = new_main
        fixture.context_data["canonical_main_sha"] = new_main
        fixture.write(fixture.context, fixture.context_data)
        fixture.request_data["run_id"] = "fixture-run-b"
        fixture.request_data["claim"]["baseline"] = c1_baseline(issue_body=fixture.body, main_sha=new_main)
        fixture.request_data["claim"]["commit_sha"] = new_main
        fixture.write(fixture.request_file, fixture.request_data)
        fresh = fixture.acquire()
        assert (
            fresh["envelope"]["evidence_refs"][0]["excerpt_sha256"]
            == old["envelope"]["evidence_refs"][0]["excerpt_sha256"]
        )
        fixture.snapshot.write_bytes(old_receipt)
        fixture.readback.write_bytes(old_readback)
        reused = fixture.assert_stopped()
        assert reused.returncode != 0
        # A fresh declaration on the new run with a valid old tuple is still
        # not a confirmation of the independently verified new-main tuple.
        fixture.select_operator(fresh, override=lambda d: d.update({"evidence": old_decision["evidence"]}))
        wrong_tuple = fixture.assert_stopped()
        assert wrong_tuple.returncode == 2, wrong_tuple.stderr
        assert "operator decision does not identify" in wrong_tuple.stdout
        fixture.select_operator(fresh, override=lambda d: d.pop("evidence"))
        no_tuple = fixture.assert_stopped()
        assert no_tuple.returncode == 2
        fixture.rejection_artifact(
            "old-tuple-receipt",
            {
                "old_main": old["envelope"]["evidence_refs"][0]["commit_sha"],
                "new_main": new_main,
                "authentic_old_receipt_reused": reused.returncode,
                "fresh_declaration_old_tuple": wrong_tuple.returncode,
                "tuple_free_declaration": no_tuple.returncode,
            },
        )

    def test_step1_operator_handoff_reference_contract(self):
        content = (Path(__file__).resolve().parent.parent / "references" / "anchor-comment-handling.md").read_text()
        for marker in (
            "--resolution-only",
            "--expected-result-sha256",
            "state_sha256",
            "initial_binding",
            "--step1-context-file",
            "--issue-body-file",
            "canonical main",
            "C1",
            "with_human_context",
            "readback",
            "次 claim",
            "agent runtime integration PASS",
            "no-redispatch",
        ):
            assert marker in content, marker

    def test_adapter_cli_missing_request_file_is_usage_error(self, tmp_path):
        import subprocess

        adapter_script = Path(__file__).resolve().parent.parent / "scripts" / "source_evidence_adapter_cli.py"
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
    def test_operator_resolution_rejects_stale_cross_claim_ref_provenance_head(self, tmp_path):
        fixture = _C1Fixture(tmp_path)
        first = fixture.acquire()
        fixture.select_operator(first)
        assert fixture.resolve().returncode == 0
        context = json.loads(fixture.context.read_bytes())
        body = fixture.issue.read_text()
        fixture.issue.write_text("stale body", encoding="utf-8")
        fixture.assert_stopped()
        fixture.issue.write_text(body, encoding="utf-8")
        for key, wrong in (("claim_id", "C2"), ("claim_text", "different statement"), ("canonical_main_sha", "a" * 40)):
            candidate = dict(context)
            candidate[key] = wrong
            fixture.write(fixture.context, candidate)
            fixture.assert_stopped()
        fixture.write(fixture.context, context)
        snapshot = json.loads(fixture.snapshot.read_bytes())
        readback = json.loads(fixture.readback.read_bytes())
        for key, wrong in (("issue_number", 999), ("user_id", 0), ("updated_at", "2020-01-01T00:00:00Z")):
            candidate = dict(snapshot)
            candidate[key] = wrong
            fixture.write(fixture.snapshot, candidate)
            fixture.assert_stopped()
        fixture.write(fixture.snapshot, snapshot)
        fixture.write(fixture.readback, {**readback, "body_sha256": "f" * 64})
        fixture.assert_stopped()
        fixture.write(fixture.readback, readback)
        for lane in ("with_agent_report", "with_anchor", "operator", ""):
            fixture.write(
                fixture.snapshot, {**snapshot, "lane": lane, "author_association": "OWNER", "source": "operator"}
            )
            fixture.assert_stopped()
        fixture.write(fixture.snapshot, snapshot)
        # Worktree HEAD is irrelevant: the canonical main ref and context pin
        # must match even when request+operator collude to claim a new commit.
        root = fixture.repo
        (root / "elsewhere.txt").write_text("unrelated", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "elsewhere.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "user.name=Fixture",
                "commit",
                "-qm",
                "later",
            ],
            check=True,
        )
        fixture.assert_stopped()
        # For a hash-valid ref on the wrong path, the independent C1 target
        # guard rejects even if its permalink and excerpt SHA are internally valid.
        from copy import deepcopy

        ref = deepcopy(first["envelope"]["evidence_refs"][0])
        ref["path"] = "other.py"
        ref["permalink"] = f"https://github.com/{C1_REPO}/blob/{fixture.main}/other.py#L1-L3"
        bad_envelope = deepcopy(first["envelope"])
        bad_envelope["evidence_refs"] = [ref]
        from validate_repo_evidence_ref import validate_repo_evidence_ref

        assert validate_repo_evidence_ref(ref, repo_root=fixture.repo)["status"] == "verified"
        decision = json.loads(snapshot["body"])
        overlay = decide_effective_step1_action(
            bad_envelope,
            expected_baseline=first["envelope"]["baseline"],
            target=context["c1_target"],
            repo_root=fixture.repo,
            operator_decision=decision,
            run_id=first["initial_binding"]["run_id"],
            envelope_sha256=first["initial_binding"]["envelope_sha256"],
        )
        assert overlay["effective_step1_action"] == "human_review"
        bad_envelope["evidence_refs"] = []
        assert (
            decide_effective_step1_action(
                bad_envelope,
                expected_baseline=first["envelope"]["baseline"],
                target=context["c1_target"],
                repo_root=fixture.repo,
                operator_decision=decision,
                run_id=first["initial_binding"]["run_id"],
                envelope_sha256=first["initial_binding"]["envelope_sha256"],
            )["effective_step1_action"]
            == "human_review"
        )

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
