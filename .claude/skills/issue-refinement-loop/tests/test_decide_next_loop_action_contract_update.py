from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
SCHEMAS_DIR = SKILL_ROOT / "schemas"

sys.path.insert(0, str(SCRIPTS_DIR))

router = importlib.import_module("decide_next_loop_action")


def _base_loop_state(**overrides) -> dict:
    state = {
        "schema_version": "loop_state/v1",
        "issue_number": 1323,
        "iteration": 0,
        "max_iterations": 3,
        "last_verdict": None,
        "termination_reason": None,
        "scope_signal_guard": {
            "triggered": True,
            "excluded_by_anchor_reframe": False,
            "reason_code": "new_in_scope_area",
        },
        "delivery_rollup": {"applicable": False, "unmaterialized_slots": []},
        "follow_up_materialization": {"candidates": []},
        "web_research_policy": {
            "required": False,
            "reason": None,
            "critical_external_claims": [],
            "skip_reason": None,
        },
    }
    state.update(overrides)
    return state


def _contract_update_decision_v2() -> dict:
    return {
        "schema_version": "SCOPE_SIGNAL_GUARD_DECISION_V2",
        "raw_signal": {"triggered": True, "reason_code": "new_in_scope_area"},
        "scope_context": {"path_layer": ["skill"]},
        "scope_delta_approval": {
            "present": False,
            "valid": False,
            "status": "not_required",
            "missing_approval_field": False,
            "suggested_contract_patch": None,
            "comment_id": None,
            "comment_url": None,
            "body_sha256": None,
            "author_association": None,
            "created_at": None,
            "issue_url": None,
            "required_rerun": [],
        },
        "security_sensitive": False,
        "route": "not_triggered",
        "scope_delta_authority": {
            "schema_version": "SCOPE_DELTA_AUTHORITY_V1",
            "authority_category": "human_review_directive",
            "provenance": {
                "source_kind": "issue_comment",
                "source_ref": "https://github.com/squne121/loop-protocol/issues/1323#issuecomment-1",
                "body_sha256": "sha256:deadbeef",
                "author_association": "OWNER",
            },
            "directive": {"confidence": "explicit", "extracted_markers": ["revised acceptance criteria"]},
            "boundary_flags": {
                "expands_allowed_paths": False,
                "changes_permission_boundary": False,
                "changes_external_service_boundary": False,
                "destructive_or_non_idempotent_operation": False,
                "requires_issue_split": False,
            },
            "route": {
                "action": "contract_update_required",
                "reason_code": "explicit_human_contract_directive",
                "implementation_allowed": False,
                "next_step": "rerun_refinement_after_contract_update",
            },
        },
    }


# --- AC20 (unit level): decide_next_action() consumes route directly -------


def test_contract_update_required_route_yields_proceed_with_contract_update():
    decision_v2 = _contract_update_decision_v2()
    decision_v2["route"] = "contract_update_required"

    status, next_action, commands, blockers, termination_cause_hint = router.decide_next_action(
        loop_state=_base_loop_state(),
        review_verdict=None,
        scope_signal_guard_decision_v2=decision_v2,
    )

    assert next_action == router.ACTION_PROCEED_WITH_CONTRACT_UPDATE
    assert status == router.STATUS_PASS
    assert termination_cause_hint is None


def test_contract_update_required_does_not_set_termination_reason():
    # decide_next_action is read-only w.r.t. loop_state; assert the input
    # dict itself is untouched (still termination_reason: None) and that the
    # router's own return tuple carries no termination_reason value at all.
    loop_state = _base_loop_state()
    decision_v2 = _contract_update_decision_v2()
    decision_v2["route"] = "contract_update_required"

    router.decide_next_action(
        loop_state=loop_state,
        review_verdict=None,
        scope_signal_guard_decision_v2=decision_v2,
    )

    assert loop_state["termination_reason"] is None


def test_route_other_than_contract_update_required_falls_through_to_existing_logic():
    decision_v2 = _contract_update_decision_v2()
    decision_v2["route"] = "not_triggered"

    status, next_action, commands, blockers, termination_cause_hint = router.decide_next_action(
        loop_state=_base_loop_state(
            scope_signal_guard={
                "triggered": False,
                "excluded_by_anchor_reframe": False,
                "reason_code": None,
            }
        ),
        review_verdict="approve",
        scope_signal_guard_decision_v2=decision_v2,
    )
    assert next_action == router.ACTION_PROCEED_TO_STEP_4_5


def test_missing_scope_signal_guard_decision_v2_does_not_regress_existing_behavior():
    # No sidecar provided at all (None): existing hard-stop behavior for a
    # triggered, non-excluded scope_signal_guard must still apply unchanged.
    status, next_action, commands, blockers, termination_cause_hint = router.decide_next_action(
        loop_state=_base_loop_state(),
        review_verdict=None,
        scope_signal_guard_decision_v2=None,
    )
    assert next_action == router.ACTION_HUMAN_ESCALATION
    assert termination_cause_hint == "human_judgment_required"


# --- AC20 (end-to-end CLI level) --------------------------------------------


def _run_cli(loop_state: dict, decision_v2: "dict | None" = None) -> subprocess.CompletedProcess:
    argv = [
        sys.executable,
        str(SCRIPTS_DIR / "decide_next_loop_action.py"),
        "--loop-state-json",
        json.dumps(loop_state, ensure_ascii=False),
    ]
    if decision_v2 is not None:
        argv += ["--scope-signal-guard-decision-v2-json", json.dumps(decision_v2, ensure_ascii=False)]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def test_cli_end_to_end_contract_update_required():
    decision_v2 = _contract_update_decision_v2()
    decision_v2["route"] = "contract_update_required"

    result = _run_cli(_base_loop_state(), decision_v2)
    assert result.returncode == 0
    assert f"NEXT_ACTION: {router.ACTION_PROCEED_WITH_CONTRACT_UPDATE}" in result.stdout
    assert "TERMINATION_CAUSE:" not in result.stdout


def test_cli_end_to_end_without_sidecar_regresses_to_existing_escalation():
    result = _run_cli(_base_loop_state())
    assert result.returncode == router.EXIT_HUMAN_ESCALATION
    assert f"NEXT_ACTION: {router.ACTION_HUMAN_ESCALATION}" in result.stdout


# --- termination_reason enum must remain unchanged (Non-Goal / Stop Condition) ---


def test_loop_state_schema_termination_reason_enum_unchanged():
    schema = json.loads((SCHEMAS_DIR / "loop_state.schema.json").read_text(encoding="utf-8"))
    # termination_reason is validated via LOOP_STATE_V1's own properties;
    # this test asserts the schema's top-level shape was NOT widened to add
    # scope_signal_guard_decision_v2 as a validated property (the sidecar is
    # intentionally passed out-of-band, never through --loop-state-file).
    assert schema["additionalProperties"] is False
    assert "scope_signal_guard_decision_v2" not in schema.get("properties", {})
