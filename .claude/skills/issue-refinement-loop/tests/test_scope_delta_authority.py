from __future__ import annotations

import importlib
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

sda = importlib.import_module("scope_signal_delta")


def _evidence(
    *,
    source_kind="issue_comment",
    author_association="OWNER",
    directive_markers=None,
    extracted_directives=None,
    boundary_flags=None,
    comment_url="https://github.com/squne121/loop-protocol/issues/1323#issuecomment-1",
    issue_url="https://github.com/squne121/loop-protocol/issues/1323",
    target_issue_number=1323,
    comment_id=1,
    confidence=None,
):
    return {
        "schema_version": "SCOPE_DELTA_AUTHORITY_EVIDENCE_V1",
        "source_kind": source_kind,
        "source_ref": comment_url,
        "source_issue_number": target_issue_number,
        "comment_id": comment_id,
        "comment_url": comment_url,
        "issue_url": issue_url,
        "target_issue_number": target_issue_number,
        "body_sha256": "sha256:deadbeef",
        "author_login": "reviewer",
        "author_type": "User",
        "author_association": author_association,
        "captured_at": "2026-07-04T00:00:00Z",
        "directive_markers": directive_markers or [],
        "extracted_directives": extracted_directives or [],
        "ambiguity_flags": [],
        "boundary_flags": boundary_flags or [],
        "confidence": confidence,
    }


# --- AC1: four-category classification -------------------------------------


def test_scope_delta_authority_classifies_into_four_categories():
    ai = sda.classify_scope_delta_authority(
        _evidence(source_kind="generated_by_agent", author_association=None),
        triggered=True,
    )
    assert ai["authority_category"] == "ai_inferred"

    human = sda.classify_scope_delta_authority(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: do X"],
        ),
        triggered=True,
    )
    assert human["authority_category"] == "human_review_directive"

    parent = sda.classify_scope_delta_authority(
        _evidence(source_kind="parent_issue", comment_url=None, issue_url=None),
        triggered=True,
    )
    assert parent["authority_category"] == "existing_parent_contract"

    related = sda.classify_scope_delta_authority(
        _evidence(source_kind="related_issue", comment_url=None, issue_url=None),
        triggered=True,
    )
    assert related["authority_category"] == "related_issue_dependency"


# --- AC2: explicit human review directive -> contract_update_required ------


def test_human_review_directive_yields_contract_update_required():
    result = sda.classify_scope_delta_authority(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add provider_auto_policy_v1"],
        ),
        triggered=True,
        target_issue_number=1323,
    )
    assert result["route"]["action"] == "contract_update_required"
    assert result["route"]["reason_code"] == "explicit_human_contract_directive"


# --- AC3: contract_patch_plan_v1 generated for human_review_directive ------


def test_human_review_directive_generates_contract_patch_plan():
    result = sda.classify_scope_delta_authority(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add provider_auto_policy_v1"],
        ),
        triggered=True,
        target_issue_number=1323,
        base_issue_body_sha256="sha256:base",
    )
    plan = result["contract_patch_plan"]
    assert plan["schema_version"] == "CONTRACT_PATCH_PLAN_V1"
    assert plan["target_issue_number"] == 1323
    assert plan["base_issue_body_sha256"] == "sha256:base"
    assert plan["operations"]
    assert plan["operations"][0]["section"] == "Acceptance Criteria"


# --- AC4: implementation_allowed:false even when contract_update_required --


def test_contract_update_required_sets_implementation_allowed_false():
    result = sda.classify_scope_delta_authority(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add provider_auto_policy_v1"],
        ),
        triggered=True,
    )
    assert result["route"]["action"] == "contract_update_required"
    assert result["route"]["implementation_allowed"] is False


# --- AC5: AI-inferred scope delta still escalates (regression) -------------


def test_ai_inferred_scope_delta_still_escalates():
    result = sda.classify_scope_delta_authority(None, triggered=True)
    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"


# --- AC6: ambiguous / conflicting / permission boundary still escalate -----


def test_ambiguous_conflicting_or_permission_boundary_still_escalates():
    ambiguous = sda.classify_scope_delta_authority(
        _evidence(directive_markers=["stop condition"], extracted_directives=[]),
        triggered=True,
    )
    assert ambiguous["route"]["action"] == "human_escalation"
    assert ambiguous["route"]["reason_code"] == "ambiguous_human_directive"

    conflicting = sda.classify_scope_delta_authority(
        [
            _evidence(
                comment_id=1,
                directive_markers=["revised acceptance criteria"],
                extracted_directives=["AC21: add X"],
            ),
            _evidence(
                comment_id=2,
                comment_url="https://github.com/squne121/loop-protocol/issues/1323#issuecomment-2",
                directive_markers=["revised acceptance criteria"],
                extracted_directives=["AC21: remove X instead"],
            ),
        ],
        triggered=True,
    )
    assert conflicting["route"]["action"] == "human_escalation"
    assert conflicting["route"]["reason_code"] == "conflicting_human_directives"

    permission = sda.classify_scope_delta_authority(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add X"],
            boundary_flags=["changes_permission_boundary"],
        ),
        triggered=True,
    )
    assert permission["route"]["action"] == "human_escalation"
    assert permission["route"]["reason_code"] == "changes_permission_boundary"


# --- AC7: specific reason_code, never the generic new_in_scope_area --------


def test_reason_code_is_specific_not_generic_new_in_scope_area():
    for evidence, kwargs in [
        (None, {}),
        (_evidence(directive_markers=["stop condition"]), {}),
        (
            _evidence(
                directive_markers=["revised acceptance criteria"],
                extracted_directives=["AC21: add X"],
                boundary_flags=["destructive_or_non_idempotent_operation"],
            ),
            {},
        ),
    ]:
        result = sda.classify_scope_delta_authority(evidence, triggered=True, **kwargs)
        reason_code = result["route"]["reason_code"]
        assert reason_code != "new_in_scope_area"
        assert reason_code is not None


# --- AC8: Issue #1270 fixture -> contract_update_required -------------------


def test_issue_1270_human_review_decision_fixture_yields_contract_update_required():
    fixture = _load_1270_fixture()
    result = sda.classify_scope_delta_authority(
        fixture, triggered=True, target_issue_number=1270
    )
    assert result["authority_category"] == "human_review_directive"
    assert result["route"]["action"] == "contract_update_required"


# --- AC9: next_step is rerun_refinement_after_contract_update --------------


def test_contract_update_required_next_step_is_rerun_refinement():
    result = sda.classify_scope_delta_authority(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add X"],
        ),
        triggered=True,
    )
    assert result["route"]["next_step"] == "rerun_refinement_after_contract_update"


# --- AC11: trusted anchor REQUEST_CHANGES fixture (#1008 AC1) ---------------


def test_trusted_anchor_request_changes_classified_as_human_review_directive():
    result = sda.classify_scope_delta_authority(
        _evidence(
            author_association="MEMBER",
            directive_markers=["allowed paths", "verification command"],
            extracted_directives=["Allowed Paths: add scripts/agy_provider.py"],
        ),
        triggered=True,
    )
    assert result["authority_category"] == "human_review_directive"


# --- AC12: AI self-proposed Allowed Paths expansion still escalates --------


def test_agent_inferred_allowed_path_expansion_still_escalates():
    result = sda.classify_scope_delta_authority(
        _evidence(source_kind="generated_by_agent", author_association=None),
        triggered=True,
    )
    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"


# --- AC13: untrusted / missing author association fails closed ------------


def test_untrusted_or_missing_author_association_fails_closed():
    for assoc in ("CONTRIBUTOR", "NONE", None):
        result = sda.classify_scope_delta_authority(
            _evidence(
                author_association=assoc,
                directive_markers=["revised acceptance criteria"],
                extracted_directives=["AC21: add X"],
            ),
            triggered=True,
        )
        assert result["authority_category"] == "ai_inferred"
        assert result["route"]["action"] == "human_escalation"
        assert result["route"]["reason_code"] == "untrusted_author_association"


# --- AC14: raw comment body never forwarded --------------------------------


def test_raw_comment_body_not_forwarded_to_issue_author():
    evidence = _evidence(
        directive_markers=["revised acceptance criteria"],
        extracted_directives=["AC21: add X"],
    )
    assert "body" not in evidence
    assert "raw_body" not in evidence
    result = sda.classify_scope_delta_authority(evidence, triggered=True)
    import json

    serialized = json.dumps(result, ensure_ascii=False)
    assert "raw_body" not in serialized


# --- AC15: provenance distinguishes trusted anchor from agent inferred -----


def test_provenance_distinguishes_trusted_anchor_from_agent_inferred():
    trusted = sda.classify_scope_delta_authority(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add X"],
        ),
        triggered=True,
    )
    assert trusted["provenance"]["source_kind"] == "issue_comment"
    assert trusted["provenance"]["author_association"] == "OWNER"

    agent = sda.classify_scope_delta_authority(None, triggered=True)
    assert agent["provenance"]["source_kind"] == "generated_by_agent"
    assert agent["provenance"]["author_association"] is None


# --- AC16: issue comment URL mismatch / PR review URL confusion -----------


def test_issue_comment_url_mismatch_or_pr_review_url_confusion_is_invalid():
    wrong_issue = sda.classify_scope_delta_authority(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add X"],
            comment_url="https://github.com/squne121/loop-protocol/issues/9999#issuecomment-1",
        ),
        triggered=True,
        target_issue_number=1323,
    )
    assert wrong_issue["authority_category"] == "ai_inferred"
    assert wrong_issue["route"]["action"] == "human_escalation"

    pr_review_confused = sda.classify_scope_delta_authority(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add X"],
            comment_url="https://github.com/squne121/loop-protocol/pull/42#issuecomment-1",
        ),
        triggered=True,
        target_issue_number=1323,
    )
    assert pr_review_confused["authority_category"] == "ai_inferred"
    assert pr_review_confused["route"]["action"] == "human_escalation"

    assert sda.parse_issue_comment_url(
        "https://github.com/squne121/loop-protocol/pull/42#issuecomment-1"
    ) is None
    parsed = sda.parse_issue_comment_url(
        "https://github.com/squne121/loop-protocol/issues/1323#issuecomment-1"
    )
    assert parsed == {
        "owner": "squne121",
        "repo": "loop-protocol",
        "issue_number": 1323,
        "comment_id": "1",
    }


# --- AC17: conflicting reviewer directives ---------------------------------


def test_conflicting_reviewer_directives_classified_as_conflicting_human_directives():
    result = sda.classify_scope_delta_authority(
        [
            _evidence(
                comment_id=10,
                comment_url="https://github.com/squne121/loop-protocol/issues/1323#issuecomment-10",
                directive_markers=["revised acceptance criteria"],
                extracted_directives=["AC21: add retry budget schema"],
            ),
            _evidence(
                comment_id=11,
                comment_url="https://github.com/squne121/loop-protocol/issues/1323#issuecomment-11",
                author_association="MEMBER",
                directive_markers=["revised acceptance criteria"],
                extracted_directives=["AC21: remove retry budget schema entirely"],
            ),
        ],
        triggered=True,
    )
    assert result["route"]["reason_code"] == "conflicting_human_directives"
    assert result["route"]["action"] == "human_escalation"


# --- AC18: boundary flags block contract_update_required even w/ approval --


def test_boundary_flags_block_contract_update_even_with_trusted_approval():
    for flag, expected_reason in (
        ("changes_permission_boundary", "changes_permission_boundary"),
        ("changes_external_service_boundary", "changes_external_service_boundary"),
        ("destructive_or_non_idempotent_operation", "destructive_or_non_idempotent_operation"),
    ):
        result = sda.classify_scope_delta_authority(
            _evidence(
                directive_markers=["revised acceptance criteria"],
                extracted_directives=["AC21: add X"],
                boundary_flags=[flag],
            ),
            triggered=True,
        )
        assert result["route"]["action"] == "human_escalation"
        assert result["route"]["reason_code"] == expected_reason
        assert result["route"]["implementation_allowed"] is False


# --- AC19: #1270 fixture generates contract_patch_plan_v1 w/ forbidden -----


def test_issue_1270_fixture_generates_contract_patch_plan_with_forbidden_operations():
    fixture = _load_1270_fixture()
    result = sda.classify_scope_delta_authority(
        fixture,
        triggered=True,
        target_issue_number=1270,
        base_issue_body_sha256="sha256:1270base",
    )
    plan = result["contract_patch_plan"]
    assert plan["forbidden"] == ["direct_github_write", "implementation_phase_transition"]
    assert plan["required_next_step"] == "rerun_refinement_after_contract_update"
    assert plan["operations"]


def _load_1270_fixture() -> dict:
    return _evidence(
        source_kind="issue_comment",
        author_association="OWNER",
        comment_url="https://github.com/squne121/loop-protocol/issues/1270#issuecomment-4881521790",
        issue_url="https://github.com/squne121/loop-protocol/issues/1270",
        target_issue_number=1270,
        comment_id=4881521790,
        directive_markers=["revised acceptance criteria", "precondition"],
        extracted_directives=[
            "AC21: provider_auto_policy_v1 must classify AGY failures",
            "AC22: retry budget schema must cap max_attempts",
        ],
    )


# --- not_triggered path -----------------------------------------------------


def test_not_triggered_returns_not_triggered_route():
    result = sda.classify_scope_delta_authority(
        _evidence(directive_markers=["revised acceptance criteria"]),
        triggered=False,
    )
    assert result["route"]["action"] == "not_triggered"
    assert result["route"]["reason_code"] is None
    assert result["route"]["implementation_allowed"] is True
