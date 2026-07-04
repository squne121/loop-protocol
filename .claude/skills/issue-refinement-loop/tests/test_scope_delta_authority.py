from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import jsonschema

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
SCHEMAS_DIR = SKILL_ROOT / "schemas"

sys.path.insert(0, str(SCRIPTS_DIR))

sda = importlib.import_module("scope_signal_delta")


def _load_evidence_schema() -> dict:
    return json.loads(
        (SCHEMAS_DIR / "scope_delta_authority_evidence_v1.schema.json").read_text(encoding="utf-8")
    )


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


def _classify(
    evidence,
    *,
    expected_repo="squne121/loop-protocol",
    base_issue_body_sha256="sha256:test-base",
    **kwargs,
):
    """Wrapper defaulting expected_repo and base_issue_body_sha256 (PR #1332
    review fix, P0/P1): classify_scope_delta_authority() now requires
    expected_repo to accept issue_comment evidence (AC16 hardening --
    fail-closed without it), and build_contract_patch_plan_v1() (invoked for
    explicit-confidence human_review_directive evidence) now fail-closes to
    human_escalation without a non-null base_issue_body_sha256 (P1). Tests
    that need to exercise the fail-closed/missing paths pass
    expected_repo=None or base_issue_body_sha256=None explicitly.
    """
    return sda.classify_scope_delta_authority(
        evidence,
        expected_repo=expected_repo,
        base_issue_body_sha256=base_issue_body_sha256,
        **kwargs,
    )


# --- AC1: four-category classification -------------------------------------


def test_scope_delta_authority_classifies_into_four_categories():
    ai = _classify(
        _evidence(source_kind="generated_by_agent", author_association=None),
        triggered=True,
    )
    assert ai["authority_category"] == "ai_inferred"

    human = _classify(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: do X"],
        ),
        triggered=True,
    )
    assert human["authority_category"] == "human_review_directive"

    parent = _classify(
        _evidence(source_kind="parent_issue", comment_url=None, issue_url=None),
        triggered=True,
    )
    assert parent["authority_category"] == "existing_parent_contract"

    related = _classify(
        _evidence(source_kind="related_issue", comment_url=None, issue_url=None),
        triggered=True,
    )
    assert related["authority_category"] == "related_issue_dependency"


# --- AC2: explicit human review directive -> contract_update_required ------


def test_human_review_directive_yields_contract_update_required():
    result = _classify(
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
    result = _classify(
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


# --- P1 (PR #1332 review): base_issue_body_sha256 required/non-null --------


def test_missing_base_issue_body_sha256_fails_closed_to_human_escalation():
    # PR #1332 review, P1: an explicit human_review_directive that would
    # otherwise route to contract_update_required must NOT generate a
    # CONTRACT_PATCH_PLAN_V1 (and must not route to contract_update_required
    # at all) when base_issue_body_sha256 cannot be resolved.
    result = _classify(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add provider_auto_policy_v1"],
        ),
        triggered=True,
        target_issue_number=1323,
        base_issue_body_sha256=None,
    )
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "missing_base_issue_body_sha256"
    assert "contract_patch_plan" not in result


def test_build_contract_patch_plan_v1_raises_without_base_issue_body_sha256():
    with __import__("pytest").raises(sda.ContractPatchPlanBaseShaMissingError):
        sda.build_contract_patch_plan_v1(
            target_issue_number=1323,
            base_issue_body_sha256=None,
            source_evidence=[],
            operations=[],
        )


def test_contract_patch_plan_source_evidence_carries_comment_id_and_text_hash():
    # PR #1332 review, P1: source_evidence entries must carry
    # source_comment_id / extracted_text_sha256 / captured_at so
    # issue-author/edit-issue can detect a stale or mismatched source
    # comment before applying an operation derived from it.
    result = _classify(
        _evidence(
            comment_id=42424242,
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add provider_auto_policy_v1"],
        ),
        triggered=True,
        target_issue_number=1323,
        base_issue_body_sha256="sha256:base",
    )
    entry = result["contract_patch_plan"]["source_evidence"][0]
    assert entry["source_comment_id"] == 42424242
    assert entry["captured_at"] == "2026-07-04T00:00:00Z"
    assert entry["extracted_text_sha256"] is not None
    assert entry["extracted_text_sha256"] != "sha256:base"


# --- AC4: implementation_allowed:false even when contract_update_required --


def test_contract_update_required_sets_implementation_allowed_false():
    result = _classify(
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
    result = _classify(None, triggered=True)
    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"


# --- AC6: ambiguous / conflicting / permission boundary still escalate -----


def test_ambiguous_conflicting_or_permission_boundary_still_escalates():
    ambiguous = _classify(
        _evidence(directive_markers=["stop condition"], extracted_directives=[]),
        triggered=True,
    )
    assert ambiguous["route"]["action"] == "human_escalation"
    assert ambiguous["route"]["reason_code"] == "ambiguous_human_directive"

    conflicting = _classify(
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

    permission = _classify(
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
        result = _classify(evidence, triggered=True, **kwargs)
        reason_code = result["route"]["reason_code"]
        assert reason_code != "new_in_scope_area"
        assert reason_code is not None


# --- AC8: Issue #1270 fixture (real OWNER comment issuecomment-4881420705)
# -> contract_update_required -------------------------------------------------


def test_issue_1270_actual_comment_4881420705_yields_contract_update_required():
    fixture = _load_issue_1270_actual_comment_4881420705_fixture()
    result = _classify(
        fixture, triggered=True, target_issue_number=1270
    )
    assert result["authority_category"] == "human_review_directive"
    assert result["route"]["action"] == "contract_update_required"


# --- AC9: next_step is rerun_refinement_after_contract_update --------------


def test_contract_update_required_next_step_is_rerun_refinement():
    result = _classify(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add X"],
        ),
        triggered=True,
    )
    assert result["route"]["next_step"] == "rerun_refinement_after_contract_update"


# --- AC11: trusted anchor REQUEST_CHANGES fixture (#1008 AC1) ---------------


def test_trusted_anchor_request_changes_classified_as_human_review_directive():
    result = _classify(
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
    result = _classify(
        _evidence(source_kind="generated_by_agent", author_association=None),
        triggered=True,
    )
    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"


# --- AC13: untrusted / missing author association fails closed ------------


def test_untrusted_or_missing_author_association_fails_closed():
    for assoc in ("CONTRIBUTOR", "NONE", None):
        result = _classify(
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
    result = _classify(evidence, triggered=True)
    import json

    serialized = json.dumps(result, ensure_ascii=False)
    assert "raw_body" not in serialized


# --- AC15: provenance distinguishes trusted anchor from agent inferred -----


def test_provenance_distinguishes_trusted_anchor_from_agent_inferred():
    trusted = _classify(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add X"],
        ),
        triggered=True,
    )
    assert trusted["provenance"]["source_kind"] == "issue_comment"
    assert trusted["provenance"]["author_association"] == "OWNER"

    agent = _classify(None, triggered=True)
    assert agent["provenance"]["source_kind"] == "generated_by_agent"
    assert agent["provenance"]["author_association"] is None


# --- AC16: issue comment URL mismatch / PR review URL confusion -----------


def test_issue_comment_url_mismatch_or_pr_review_url_confusion_is_invalid():
    wrong_issue = _classify(
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

    pr_review_confused = _classify(
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


# --- AC16 hardening (PR #1332 review, P0/P1): cross-repo spoof rejection ----


def test_cross_repo_spoof_with_matching_issue_number_is_fail_closed():
    # Same issue number (1323) but a *different* repo -- must be rejected
    # even though issue_number alone would match. Regression guard for the
    # classifier-layer gap flagged in PR #1332 review (validate function was
    # not receiving expected_repo from all call sites).
    spoofed = _classify(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add X"],
            comment_url="https://github.com/other/repo/issues/1323#issuecomment-1",
            issue_url="https://github.com/other/repo/issues/1323",
        ),
        triggered=True,
        target_issue_number=1323,
        expected_repo="squne121/loop-protocol",
    )
    assert spoofed["authority_category"] == "ai_inferred"
    assert spoofed["route"]["action"] == "human_escalation"
    assert spoofed["route"]["reason_code"] == "ai_inferred_scope_delta"


def test_missing_expected_repo_fails_closed_for_issue_comment_evidence():
    # classify_scope_delta_authority's expected_repo is mandatory for
    # issue_comment evidence acceptance (PR #1332 review, P0/P1): a caller
    # that forgets to supply it must not silently accept the evidence.
    result = _classify(
        _evidence(
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add X"],
        ),
        triggered=True,
        target_issue_number=1323,
        expected_repo=None,
    )
    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "ai_inferred_scope_delta"


def test_pull_request_review_source_kind_is_fail_closed_rejected():
    # PR #1332 review, P0/P1: pull_request_review evidence cannot yet be
    # structurally verified against pull_request_url / _links.pull_request /
    # repo / PR number (SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 does not carry
    # those fields), so it must be rejected unconditionally rather than
    # accepted by default.
    result = _classify(
        _evidence(
            source_kind="pull_request_review",
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add X"],
        ),
        triggered=True,
        target_issue_number=1323,
    )
    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "ai_inferred_scope_delta"


def test_pull_request_review_mislabeled_issue_comment_url_is_still_rejected():
    # A genuine issue-comment URL mislabeled with source_kind
    # pull_request_review must not be accepted either (net effect unchanged
    # by the fail-closed hardening: previously detected explicitly, now
    # covered by the unconditional pull_request_review rejection).
    result = _classify(
        _evidence(
            source_kind="pull_request_review",
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC21: add X"],
            comment_url="https://github.com/squne121/loop-protocol/issues/1323#issuecomment-1",
        ),
        triggered=True,
        target_issue_number=1323,
    )
    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"


def test_validate_scope_delta_authority_evidence_url_rejects_pull_request_review_directly():
    assert sda.validate_scope_delta_authority_evidence_url(
        {"source_kind": "pull_request_review", "comment_url": None},
        target_issue_number=1323,
        expected_repo="squne121/loop-protocol",
    ) is False


def test_validate_scope_delta_authority_evidence_url_rejects_issue_comment_without_expected_repo():
    assert sda.validate_scope_delta_authority_evidence_url(
        {
            "source_kind": "issue_comment",
            "comment_url": "https://github.com/squne121/loop-protocol/issues/1323#issuecomment-1",
        },
        target_issue_number=1323,
        expected_repo=None,
    ) is False


# --- AC17: conflicting reviewer directives ---------------------------------


def test_conflicting_reviewer_directives_classified_as_conflicting_human_directives():
    result = _classify(
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
        result = _classify(
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


def test_issue_1270_actual_comment_4881420705_generates_contract_patch_plan_with_forbidden_operations():
    fixture = _load_issue_1270_actual_comment_4881420705_fixture()
    result = _classify(
        fixture,
        triggered=True,
        target_issue_number=1270,
        base_issue_body_sha256="sha256:1270base",
    )
    plan = result["contract_patch_plan"]
    assert plan["forbidden"] == ["direct_github_write", "implementation_phase_transition"]
    assert plan["required_next_step"] == "rerun_refinement_after_contract_update"
    assert plan["operations"]


# Real OWNER review comment on Issue #1270 (fetched via
# `gh api repos/squne121/loop-protocol/issues/comments/4881420705`,
# created_at 2026-07-04T09:11:40Z, author_association: OWNER). This is the
# comment Issue #1323's Background actually refers to as the "Revised
# Acceptance Criteria を明示した OWNER レビューコメント" -- NOT
# issuecomment-4881521790 (that ID is a different comment: a ChatGPT
# dialogue log about how #1323 itself came to be proposed). body_sha256,
# directive_markers and extracted_directives below are the deterministic
# output of scope_signal_delta.extract_directive_markers() /
# extract_directive_items() run against the real comment body (never the
# raw body itself -- AC14).
ISSUE_1270_ACTUAL_COMMENT_4881420705_BODY_SHA256 = (
    "sha256:35f0c1fa52e29f0f6d6cc2ffb7b83f7781bae29992e512166d442035d1bf6cb6"
)


def _load_issue_1270_actual_comment_4881420705_fixture() -> dict:
    evidence = _evidence(
        source_kind="issue_comment",
        author_association="OWNER",
        comment_url="https://github.com/squne121/loop-protocol/issues/1270#issuecomment-4881420705",
        issue_url="https://github.com/squne121/loop-protocol/issues/1270",
        target_issue_number=1270,
        comment_id=4881420705,
        directive_markers=["revised ac", "revised acceptance criteria", "stop condition", "前提条件"],
        # Real "## Revised Acceptance Criteria" bullet lines from the actual
        # comment body, as scope_signal_delta.extract_directive_items()
        # would extract them (bullet-list stripping keeps the leading
        # "[ ] " checkbox marker).
        extracted_directives=[
            "[ ] AC0: `provider_auto_policy_v1` が docs/config に明文化され、"
            "runtime order、eligible profiles、retryable failure classes、"
            "stop conditions、idempotency guard が定義されている。",
            "[ ] AC1: `failure-class-taxonomy.md` に Gemini / AGY / canonical "
            "class の対応表が追加され、`agy_rate_limited`、"
            "`agy_capacity_exhausted`、`agy_web_grounding_quota_exhausted`、"
            "`provider_fallback_exhausted` が定義されている。",
        ],
    )
    evidence["body_sha256"] = ISSUE_1270_ACTUAL_COMMENT_4881420705_BODY_SHA256
    evidence["captured_at"] = "2026-07-04T09:11:40Z"
    return evidence


# --- not_triggered path -----------------------------------------------------


def test_not_triggered_returns_not_triggered_route():
    result = _classify(
        _evidence(directive_markers=["revised acceptance criteria"]),
        triggered=False,
    )
    assert result["route"]["action"] == "not_triggered"
    assert result["route"]["reason_code"] is None
    assert result["route"]["implementation_allowed"] is True


# --- P1 (PR #1332 review): scope_delta_authority_evidence_v1.schema.json
# must be additionalProperties:false and reject raw payload fields ----------


def _minimal_valid_evidence() -> dict:
    return {
        "schema_version": "SCOPE_DELTA_AUTHORITY_EVIDENCE_V1",
        "source_kind": "issue_comment",
        "source_ref": "https://github.com/squne121/loop-protocol/issues/1323#issuecomment-1",
        "source_issue_number": 1323,
        "comment_id": 1,
        "comment_url": "https://github.com/squne121/loop-protocol/issues/1323#issuecomment-1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1323",
        "body_sha256": "sha256:deadbeef",
        "author_login": "reviewer",
        "author_type": "User",
        "author_association": "OWNER",
        "captured_at": "2026-07-04T00:00:00Z",
        "directive_markers": [],
        "extracted_directives": [],
        "ambiguity_flags": [],
        "boundary_flags": [],
    }


def test_evidence_schema_is_additional_properties_false():
    schema = _load_evidence_schema()
    assert schema["additionalProperties"] is False


def test_evidence_schema_accepts_minimal_valid_evidence():
    schema = _load_evidence_schema()
    jsonschema.validate(instance=_minimal_valid_evidence(), schema=schema)


def test_evidence_schema_rejects_raw_body_fields():
    schema = _load_evidence_schema()
    for raw_field in ("body", "body_text", "body_html", "raw_body", "snapshot"):
        instance = _minimal_valid_evidence()
        instance[raw_field] = "raw comment payload should never appear here"
        try:
            jsonschema.validate(instance=instance, schema=schema)
        except jsonschema.ValidationError:
            continue
        raise AssertionError(f"schema unexpectedly accepted raw field: {raw_field!r}")


def test_real_producer_evidence_shape_validates_against_schema():
    # AC14 regression guard: the actual evidence dict SHAPE produced by
    # run_refinement_preflight._build_scope_delta_authority_evidence() (its
    # exact key set, not the test-only _evidence() helper which additionally
    # carries a target_issue_number convenience field never emitted by the
    # real producer) must validate cleanly now that additionalProperties is
    # false.
    schema = _load_evidence_schema()
    producer_shaped = _evidence(confidence="explicit")
    del producer_shaped["target_issue_number"]
    jsonschema.validate(instance=producer_shaped, schema=schema)
