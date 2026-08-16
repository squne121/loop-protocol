"""
test_repair_apply_result_schema.py

Issue #2039 AC2: repair_apply_result/v1 の JSON Schema validation テスト。

repair_action.apply consumer が生成する repair_apply_result_v1.schema.json の
required / additionalProperties:false / closed enum / cross-field invariant
（multiple_mutation_intents, second_body_drift, applied, complete, fresh
validation success の各不変条件）を固定する。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = SKILL_ROOT / "schemas"
SCHEMA_FILE = SCHEMAS_DIR / "repair_apply_result_v1.schema.json"


def load_schema() -> dict:
    assert SCHEMA_FILE.exists(), f"Schema file not found: {SCHEMA_FILE}"
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def validate(data: dict) -> list[str]:
    try:
        import jsonschema
    except ImportError:
        pytest.skip("jsonschema not available")

    schema = load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    return [e.message for e in errors]


def test_schema_is_valid_draft202012():
    import jsonschema

    schema = load_schema()
    jsonschema.Draft202012Validator.check_schema(schema)


VALID_NOT_ATTEMPTED = {
    "schema_version": "repair_apply_result/v1",
    "phase": "not_started",
    "mutation_outcome": "not_attempted",
    "failure_code": "multiple_mutation_intents",
    "repo": "squne121/loop-protocol",
    "issue_number": 2039,
    "provenance": {
        "repo": "squne121/loop-protocol",
        "issue_number": 2039,
        "original_body_sha256": "a" * 64,
        "original_updated_at": "2026-08-16T00:00:00Z",
        "preflight_run_identity": "b" * 64,
        "producer_schema_version": "repair_action/v1",
        "producer_policy_version": "deterministic-issue-repair/v1",
        "repair_action_core_sha256": "c" * 64,
        "candidate_digest": "d" * 64,
        "source_lane": "anchor",
        "source_refs_digest": "e" * 64,
    },
    "rebase": {
        "attempted": False,
        "producer_reruns": 0,
        "drift_detected": False,
        "second_drift": False,
    },
    "retry": {
        "post_dispatch_retry_budget": 0,
        "retries_used": 0,
    },
    "receipt": {
        "patch_attempted": False,
        "executor_status": None,
        "mutation_outcome": "not_attempted",
        # PR #2202 review fix-delta (P1-3): must equal the top-level
        # failure_code ("multiple_mutation_intents" above) -- this fixture
        # previously left receipt.failure_code as None while the top-level
        # field carried a real failure code, which is exactly the
        # top-level-vs-receipt failure_code mismatch the review flagged as
        # unvalidated. run_repair_action_apply() always keeps these equal
        # outside phase==fresh_validation (see `_repair_apply_not_attempted_result`).
        "failure_code": "multiple_mutation_intents",
        "final_readback": {
            "status": "not_applicable",
            "digest": None,
            "digest_class": "not_applicable",
        },
    },
    "fresh_validation": {
        "status": "not_run",
        "source_lane_preserved": False,
        "actionable_repair_remaining": None,
        "final_body_digest_match": None,
    },
    "historical_artifacts": {
        "physically_deleted": False,
        "latest_action_reference_invalidated": False,
    },
}


def _applied_success_payload() -> dict:
    data = copy.deepcopy(VALID_NOT_ATTEMPTED)
    data["phase"] = "complete"
    data["mutation_outcome"] = "applied"
    data["failure_code"] = None
    data["receipt"]["patch_attempted"] = True
    data["receipt"]["executor_status"] = "ok"
    data["receipt"]["mutation_outcome"] = "applied"
    # PR #2202 review fix-delta (P1-3): receipt.failure_code must equal the
    # top-level failure_code (None above) -- VALID_NOT_ATTEMPTED's receipt
    # carries "multiple_mutation_intents", which must not leak into an
    # applied/success payload.
    data["receipt"]["failure_code"] = None
    data["receipt"]["final_readback"] = {
        "status": "verified",
        "digest": "d" * 64,
        "digest_class": "candidate",
    }
    data["fresh_validation"] = {
        "status": "success",
        "source_lane_preserved": True,
        "actionable_repair_remaining": False,
        "final_body_digest_match": True,
    }
    data["historical_artifacts"]["latest_action_reference_invalidated"] = True
    return data


class TestRequiredFields:
    def test_valid_not_attempted_payload_passes(self):
        errors = validate(VALID_NOT_ATTEMPTED)
        assert errors == [], f"Expected valid payload to pass, got: {errors}"

    def test_valid_applied_success_payload_passes(self):
        errors = validate(_applied_success_payload())
        assert errors == [], f"Expected valid applied payload to pass, got: {errors}"

    @pytest.mark.parametrize(
        "field",
        [
            "schema_version",
            "phase",
            "mutation_outcome",
            "repo",
            "issue_number",
            "provenance",
            "rebase",
            "retry",
            "receipt",
            "fresh_validation",
            "historical_artifacts",
        ],
    )
    def test_missing_required_top_level_field(self, field):
        data = {k: v for k, v in VALID_NOT_ATTEMPTED.items() if k != field}
        errors = validate(data)
        assert errors, f"Expected error for missing {field}"

    @pytest.mark.parametrize(
        "field",
        [
            "repo",
            "issue_number",
            "original_body_sha256",
            "original_updated_at",
            "preflight_run_identity",
            "producer_schema_version",
            "producer_policy_version",
            "repair_action_core_sha256",
            "candidate_digest",
            "source_lane",
            "source_refs_digest",
        ],
    )
    def test_missing_required_provenance_field(self, field):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        del data["provenance"][field]
        errors = validate(data)
        assert errors, f"Expected error for missing provenance.{field}"


class TestAdditionalProperties:
    def test_extra_top_level_property_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["extra_field"] = "unexpected"
        errors = validate(data)
        assert errors, "Expected error for extra top-level property"

    def test_extra_provenance_property_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["provenance"]["extra_field"] = "unexpected"
        errors = validate(data)
        assert errors, "Expected error for extra provenance property"

    def test_extra_receipt_property_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["receipt"]["extra_field"] = "unexpected"
        errors = validate(data)
        assert errors, "Expected error for extra receipt property"


class TestClosedEnums:
    def test_wrong_schema_version_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["schema_version"] = "repair_apply_result/v2"
        errors = validate(data)
        assert errors, "Expected error for wrong schema_version"

    def test_wrong_phase_enum_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["phase"] = "unknown_phase"
        errors = validate(data)
        assert errors, "Expected error for unknown phase"

    def test_wrong_mutation_outcome_enum_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["mutation_outcome"] = "failed"
        errors = validate(data)
        assert errors, "Expected error for invalid mutation_outcome (failed not in enum)"

    def test_contract_patch_plan_missing_rejected_as_failure_code(self):
        """Repair lane must never emit contract_patch_plan_missing (that code
        belongs to the contract_update lane only)."""
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["failure_code"] = "contract_patch_plan_missing"
        errors = validate(data)
        assert errors, "Expected error for contract_patch_plan_missing outside closed enum"

    def test_wrong_source_lane_enum_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["provenance"]["source_lane"] = "with_anchor"
        errors = validate(data)
        assert errors, "Expected error for invalid source_lane value"

    def test_wrong_digest_class_enum_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["receipt"]["final_readback"]["digest_class"] = "new_digest"
        errors = validate(data)
        assert errors, "Expected error for invalid digest_class value"


class TestRetryBudgetIsZero:
    def test_nonzero_post_dispatch_retry_budget_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["retry"]["post_dispatch_retry_budget"] = 1
        errors = validate(data)
        assert errors, "Expected error: post_dispatch_retry_budget must be const 0"

    def test_nonzero_retries_used_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["retry"]["retries_used"] = 1
        errors = validate(data)
        assert errors, "Expected error: retries_used must be const 0"


class TestCrossFieldInvariants:
    def test_multiple_mutation_intents_requires_not_attempted(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["mutation_outcome"] = "applied"
        errors = validate(data)
        assert errors, (
            "Expected error: failure_code=multiple_mutation_intents must force "
            "mutation_outcome=not_attempted"
        )

    def test_second_drift_true_requires_not_attempted_and_failure_code(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["failure_code"] = None
        data["rebase"]["second_drift"] = True
        data["mutation_outcome"] = "applied"
        errors = validate(data)
        assert errors, "Expected error: second_drift=true must force not_attempted/second_body_drift"

    def test_second_drift_true_valid_when_fields_consistent(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["failure_code"] = "second_body_drift"
        # PR #2202 review fix-delta (P1-3): receipt.failure_code must track
        # the top-level failure_code (equality invariant added this
        # session) -- previously left at VALID_NOT_ATTEMPTED's inherited
        # "multiple_mutation_intents", which is inconsistent with this
        # payload's own top-level second_body_drift.
        data["receipt"]["failure_code"] = "second_body_drift"
        data["rebase"]["second_drift"] = True
        errors = validate(data)
        assert errors == [], f"Expected consistent second_drift payload to pass, got: {errors}"

    def test_applied_requires_patch_attempted_true(self):
        data = _applied_success_payload()
        data["receipt"]["patch_attempted"] = False
        errors = validate(data)
        assert errors, "Expected error: mutation_outcome=applied requires receipt.patch_attempted=true"

    def test_applied_requires_failure_code_null(self):
        data = _applied_success_payload()
        data["failure_code"] = "transaction_execute_error"
        errors = validate(data)
        assert errors, "Expected error: mutation_outcome=applied requires failure_code null"

    def test_complete_phase_rejects_unknown_outcome(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["phase"] = "complete"
        data["mutation_outcome"] = "unknown"
        errors = validate(data)
        assert errors, "Expected error: phase=complete must not carry mutation_outcome=unknown"

    def test_fresh_validation_success_requires_no_actionable_repair(self):
        data = _applied_success_payload()
        data["fresh_validation"]["actionable_repair_remaining"] = True
        errors = validate(data)
        assert errors, "Expected error: fresh_validation success requires actionable_repair_remaining=false"

    def test_fresh_validation_success_requires_digest_match(self):
        data = _applied_success_payload()
        data["fresh_validation"]["final_body_digest_match"] = False
        errors = validate(data)
        assert errors, "Expected error: fresh_validation success requires final_body_digest_match=true"

    def test_fresh_validation_success_requires_source_lane_preserved(self):
        data = _applied_success_payload()
        data["fresh_validation"]["source_lane_preserved"] = False
        errors = validate(data)
        assert errors, "Expected error: fresh_validation success requires source_lane_preserved=true"


class TestHistoricalArtifactsNeverPhysicallyDeleted:
    def test_physically_deleted_true_fails(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["historical_artifacts"]["physically_deleted"] = True
        errors = validate(data)
        assert errors, "Expected error: physically_deleted must be const false"


class TestP1_3ContradictoryStateInvariants:
    """PR #2202 human adversarial review (P1-3): the schema must reject
    self-contradictory states. Each test below constructs exactly one of
    the invalid combinations the review enumerated and asserts
    `jsonschema.validate()` rejects it (via `validate()`'s
    `iter_errors()`-backed helper, which is the file's existing raise-
    equivalent convention -- a non-empty error list means
    `jsonschema.Draft202012Validator.validate()` itself would raise
    `ValidationError` on the same instance/schema pair).

    NOTE on the review's exact shorthand `phase=complete かつ
    mutation_outcome=not_attempted`: this combination is NOT actually
    invalid in the current design -- `run_repair_action_apply()`
    legitimately reaches `phase=complete` with `mutation_outcome=not_attempted`
    in TWO distinct real cases, confirmed via real (non-hand-built)
    `run_repair_action_apply()` calls this session:
    (a) `edit_issue_txn.py` returns `status=human_judgment` (dispatch
        genuinely happened, `failure_code=null`) -- see
        `test_receipt_projection_is_lossless_across_statuses[human_judgment-not_attempted]`
        in test_repair_action_apply_consumer.py; and
    (b) `edit_issue_txn.py` returns `status=failed_no_mutation` WITH a
        non-empty `errors` list (dispatch genuinely happened but a control-
        plane error occurred, `failure_code=transaction_execute_error`) --
        see `test_repair_action_apply_real_subprocess_never_calls_gh_edit`
        in scripts/agent-guards/tests/test_skill_runtime_exec_stdout.py,
        which exercises this via a REAL (non-mocked) edit_issue_txn.py
        subprocess.
    So `phase=complete` does NOT universally require `failure_code=null`
    -- what it DOES universally require (enforced by the top-level==receipt
    failure_code equality invariant above, scoped to phase!=fresh_validation,
    which phase=complete always satisfies) is that failure_code losslessly
    matches whatever `edit_issue_txn.py`/the pre-dispatch gate actually
    reported. `test_complete_not_attempted_with_receipt_failure_code_mismatch_is_invalid`
    below is the genuinely-invalid variant of this combination."""

    def test_complete_not_attempted_with_receipt_failure_code_mismatch_is_invalid(self):
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["phase"] = "complete"
        data["mutation_outcome"] = "not_attempted"
        data["failure_code"] = "transaction_execute_error"
        data["receipt"]["mutation_outcome"] = "not_attempted"
        # Deliberately diverges from the top-level failure_code above --
        # this is the genuinely-invalid shape (case (b) always keeps these
        # two fields equal in production).
        data["receipt"]["failure_code"] = "secure_open_rejected"
        errors = validate(data)
        assert errors, (
            "Expected error: phase=complete with mutation_outcome=not_attempted "
            "still requires failure_code == receipt.failure_code"
        )

    def test_top_level_applied_with_receipt_unknown_is_invalid(self):
        """Review: `mutation_outcome=applied` at top level but
        `receipt.mutation_outcome=unknown` must be rejected -- the two
        fields must always agree."""
        data = _applied_success_payload()
        data["receipt"]["mutation_outcome"] = "unknown"
        errors = validate(data)
        assert errors, (
            "Expected error: top-level mutation_outcome=applied must equal "
            "receipt.mutation_outcome"
        )

    def test_applied_with_unresolved_final_readback_is_invalid(self):
        """Review: `applied` with an unresolved final readback must be
        rejected -- an applied outcome requires independently-verified
        evidence of the resulting body state."""
        data = _applied_success_payload()
        data["receipt"]["final_readback"] = {
            "status": "unresolved",
            "digest": None,
            "digest_class": "not_applicable",
        }
        errors = validate(data)
        assert errors, "Expected error: applied requires receipt.final_readback.status=verified"

    def test_fresh_validation_failed_but_phase_complete_failure_code_null_is_invalid(self):
        """Review: a fresh_validation failure must never be silently
        absorbed into phase=complete/failure_code=null. This specific
        combination is ALSO prevented at the run_repair_action_apply()
        code level by the P0-5 fix (which moves phase to fresh_validation
        whenever fresh_validation fails after an otherwise-complete phase);
        this test locks the same invariant in at the schema level as an
        independent, defense-in-depth guard."""
        data = _applied_success_payload()
        data["fresh_validation"] = {
            "status": "failed",
            "source_lane_preserved": True,
            "actionable_repair_remaining": None,
            "final_body_digest_match": False,
        }
        # phase and failure_code deliberately left as the (wrong) complete/null
        # shape _applied_success_payload() already set.
        errors = validate(data)
        assert errors, (
            "Expected error: fresh_validation.status=failed must force "
            "phase=fresh_validation, never phase=complete/failure_code=null"
        )

    def test_auto_apply_dispatch_with_null_original_updated_at_is_invalid(self):
        """Review: an auto-apply mutation (receipt.patch_attempted=true)
        must never carry a null original_updated_at -- that silently
        disables the P0-3 ABA (A->B->A) protection. This is ALSO prevented
        at the run_repair_action_apply() code level by this session's P1-3
        fix (a pre-dispatch fail-closed gate); this test locks the same
        invariant in at the schema level independently."""
        data = _applied_success_payload()
        data["provenance"]["original_updated_at"] = None
        errors = validate(data)
        assert errors, (
            "Expected error: receipt.patch_attempted=true requires a non-null "
            "provenance.original_updated_at"
        )

    def test_auto_apply_dispatch_with_null_run_identity_is_invalid(self):
        data = _applied_success_payload()
        data["provenance"]["preflight_run_identity"] = None
        errors = validate(data)
        assert errors, (
            "Expected error: receipt.patch_attempted=true requires a non-null "
            "provenance.preflight_run_identity"
        )

    def test_top_level_failure_code_vs_receipt_failure_code_mismatch_is_invalid(self):
        """Review: a top-level failure_code that disagrees with
        receipt.failure_code (outside the deliberate phase=fresh_validation
        re-derivation carve-out) must be rejected."""
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["failure_code"] = "digest_mismatch"
        data["receipt"]["failure_code"] = "secure_open_rejected"
        errors = validate(data)
        assert errors, (
            "Expected error: top-level failure_code must equal "
            "receipt.failure_code outside phase=fresh_validation"
        )

    def test_unknown_outcome_requires_final_readback_phase_and_failure_code(self):
        """Review-adjacent invariant this fix-delta also adds: `unknown`
        must always carry phase=final_readback and
        failure_code=final_readback_unresolvable (never e.g.
        phase=complete or a different failure_code)."""
        data = copy.deepcopy(VALID_NOT_ATTEMPTED)
        data["phase"] = "final_readback"
        data["mutation_outcome"] = "unknown"
        data["failure_code"] = "transaction_execute_error"  # wrong -- must be final_readback_unresolvable
        data["receipt"]["mutation_outcome"] = "unknown"
        data["receipt"]["failure_code"] = "transaction_execute_error"
        errors = validate(data)
        assert errors, (
            "Expected error: mutation_outcome=unknown requires "
            "failure_code=final_readback_unresolvable"
        )


class TestP1_3RealProducerOutputStillValidates:
    """PR #2202 review (P1-3): 'assert the current valid-output shape (from
    a real run_repair_action_apply() call, not hand-built) still validates
    successfully'. This exercises the actual production function end to
    end (real candidate artifact on disk, real intent arbiter, real
    provenance binding, real receipt adapter) rather than a hand-built
    schema instance, closing exactly the fixture-authenticity gap the
    review's 'テストがgreenでも検出できなかった理由' section flagged."""

    def test_real_run_repair_action_apply_no_drift_output_validates(self, tmp_path):
        import hashlib
        import sys

        skill_root = Path(__file__).resolve().parent.parent
        scripts_dir = skill_root / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import run_refinement_preflight as rrp

        issue_number = 2039
        original_body = "original body\n"
        repaired_body = "repaired body\n"

        def _hex(text: str) -> str:
            return hashlib.sha256(text.encode("utf-8")).hexdigest()

        artifact_dir = tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        candidate_path = artifact_dir / "candidate_body.md"
        candidate_path.write_text(repaired_body)
        repair_action = {
            "schema_version": "repair_action/v1",
            "policy_version": "deterministic-issue-repair/v1",
            "disposition": "auto_apply_safe",
            "original_body_sha256": _hex(original_body),
            "repaired_body_sha256": _hex(repaired_body),
            "diagnostics_artifact": None,
            "candidate_body_artifact": str(candidate_path),
            "repair_kinds": ["trailing_whitespace"],
            "reason_codes": ["trailing_whitespace_stripped"],
            "source_lane": "unanchored",
            "preflight_run_identity": "sha256:testrun",
            "original_updated_at": "2024-01-01T00:00:00Z",
            "source_refs_digest": None,
        }
        preflight_result = {
            "schema": "issue_refinement_preflight_result/v1",
            "repair_action": repair_action,
            "result_core_sha256": "sha256:testrun",
        }
        result_path = artifact_dir / "preflight_result.json"
        result_path.write_text(json.dumps(preflight_result))

        def _fake_apply_transaction(current_issue, candidate_body):
            return {
                "status": "ok",
                "mutation_started": True,
                "body_update": {
                    "attempted": True,
                    "status": "ok",
                    "remote_current_body_sha256": f"sha256:{_hex(repaired_body)}",
                },
                "content_update": {"patch_attempted": True, "mutation_outcome": "applied"},
                "errors": [],
            }

        fetch_bodies = iter([original_body, repaired_body])

        def _fetch():
            return {"body": next(fetch_bodies), "updatedAt": "2024-01-01T00:00:00Z"}

        result = rrp.run_repair_action_apply(
            repo="squne121/loop-protocol",
            issue_number=issue_number,
            preflight_result_path=str(result_path.relative_to(tmp_path)),
            repo_root=tmp_path,
            fetch_current=_fetch,
            apply_transaction=_fake_apply_transaction,
        )

        import jsonschema

        jsonschema.validate(result, load_schema())
        assert result["mutation_outcome"] == "applied"
        assert result["phase"] == "complete"
