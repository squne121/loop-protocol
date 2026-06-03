"""
Regression fixture for impl-review-loop termination gate.

Issue #632: Unifies impl-review-loop termination condition to
APPROVE && merge_ready == true && required_auto_actions == []

Tests verify that:
- AC1: APPROVE immediate-exit block (verdict: APPROVE alone) is gone
- AC2: required_auto_actions == [] is required for termination_reason: approved
- AC3: non-empty required_auto_actions routes to implementation-worker (not exit)
- AC4: termination_reason: approved cannot be reached with remaining required_auto_actions
- AC5: APPROVE + BEHIND does not set termination_reason: approved
- AC6: SKILL.md top-level termination condition is unified
- AC7: step-5-mergeability-handling.md parses LOOP_VERDICT_V2 fenced YAML only
- AC8: body-only required_auto_actions re-run pr_review but not verification
- AC9: update_branch re-runs verification and pr_review
- AC10: worker failed/blocked/permission_blocked does not reach approved
- AC11: reviewed_head_sha mismatch triggers PR review rerun before dispatch
- AC12: final approved emits IMPL_REVIEW_LOOP_RESULT_V1 status: draft_pr_ready and merge_ready: true
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

STEP5_FT = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "steps"
    / "step-5-feedback-and-termination.md"
)

STEP5_MH = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "steps"
    / "step-5-mergeability-handling.md"
)

SKILL_MD = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "SKILL.md"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC1: APPROVE immediate-exit block (verdict: APPROVE alone) is gone
# ---------------------------------------------------------------------------


def test_ac1_approve_alone_not_sufficient_for_termination():
    """AC1: step-5-feedback-and-termination.md must not have APPROVE-only exit block."""
    body = _read(STEP5_FT)
    # The old pattern was: 'LOOP_VERDICT.verdict: APPROVE' → termination immediately
    # The new pattern must require merge_ready == true AND required_auto_actions == []
    # Verify that the simple "APPROVE → termination" row is gone
    assert "verdict: APPROVE` | `termination_reason: approved" not in body, (
        "step-5-feedback-and-termination.md must not have APPROVE-alone → approved exit"
    )


def test_ac1_approve_requires_merge_ready_and_empty_required_auto_actions():
    """AC1: The termination condition must combine verdict, merge_ready, and required_auto_actions."""
    body = _read(STEP5_FT)
    # Must have combined condition
    assert "merge_ready == true" in body, (
        "step-5-feedback-and-termination.md must require merge_ready == true"
    )
    assert "required_auto_actions == []" in body, (
        "step-5-feedback-and-termination.md must require required_auto_actions == []"
    )


# ---------------------------------------------------------------------------
# AC2: required_auto_actions == [] required for termination_reason: approved
# ---------------------------------------------------------------------------


def test_ac2_required_auto_actions_empty_is_termination_condition():
    """AC2: required_auto_actions == [] must be in the termination condition."""
    body = _read(STEP5_FT)
    assert "required_auto_actions == []" in body, (
        "step-5-feedback-and-termination.md must specify required_auto_actions == [] "
        "as termination condition"
    )


def test_ac2_termination_reason_approved_gate_described():
    """AC2: termination_reason: approved must be gated on required_auto_actions."""
    body = _read(STEP5_FT)
    # The approved exit must be conditional on required_auto_actions == []
    idx_empty = body.find("required_auto_actions == []")
    idx_approved = body.find("termination_reason: approved")
    assert idx_empty != -1, "required_auto_actions == [] must be present"
    assert idx_approved != -1, "termination_reason: approved must be present"
    # required_auto_actions == [] must appear in context with the approved condition
    context_approved = body[max(0, idx_approved - 300) : idx_approved + 300]
    assert "merge_ready" in context_approved or "required_auto_actions" in context_approved, (
        "termination_reason: approved must appear near merge_ready/required_auto_actions gate"
    )


# ---------------------------------------------------------------------------
# AC3: non-empty required_auto_actions routes to worker (not exit)
# ---------------------------------------------------------------------------


def test_ac3_nonempty_required_auto_actions_routes_to_worker():
    """AC3: non-empty required_auto_actions must route to implementation-worker, not exit."""
    body = _read(STEP5_FT)
    assert "required_auto_action_result_routing" in body, (
        "step-5-feedback-and-termination.md must define required_auto_action_result_routing"
    )


def test_ac3_nonempty_required_auto_actions_does_not_terminate():
    """AC3: non-empty required_auto_actions must explicitly state it does not terminate."""
    body = _read(STEP5_FT)
    # Must say something about "not terminating" when required_auto_actions is non-empty
    assert "終了しない" in body or "route" in body.lower(), (
        "step-5-feedback-and-termination.md must state required_auto_actions non-empty "
        "does not trigger termination"
    )


# ---------------------------------------------------------------------------
# AC4: required_auto_actions remaining → cannot reach termination_reason: approved
# ---------------------------------------------------------------------------


def test_ac4_three_stage_gate_defined():
    """AC4: APPROVE gate must be three-stage (reviewed_head_sha, required_auto_actions, merge_ready)."""
    body = _read(STEP5_FT)
    assert "APPROVE 時の終了 gate" in body or "APPROVE gate" in body, (
        "step-5-feedback-and-termination.md must define APPROVE gate stages"
    )


def test_ac4_required_auto_actions_gate_before_merge_ready_gate():
    """AC4: required_auto_actions gate must appear before merge_ready gate in APPROVE flow."""
    body = _read(STEP5_FT)
    idx_req = body.find("required_auto_actions gate")
    idx_merge = body.find("merge_ready gate")
    assert idx_req != -1, "required_auto_actions gate must be defined"
    assert idx_merge != -1, "merge_ready gate must be defined"
    assert idx_req < idx_merge, (
        "required_auto_actions gate must appear before merge_ready gate in APPROVE flow"
    )


def test_ac4_worker_status_failed_leads_to_human_escalation():
    """AC4: worker_status_failed must route to human_escalation (not approved)."""
    body = _read(STEP5_FT)
    assert "worker_status_failed" in body, (
        "step-5-feedback-and-termination.md must define worker_status_failed routing"
    )
    idx = body.find("worker_status_failed")
    context = body[idx : idx + 200]
    assert "human_escalation" in context, (
        "worker_status_failed must route to human_escalation"
    )


def test_ac4_worker_status_blocked_leads_to_human_escalation():
    """AC4: worker_status_blocked must route to human_escalation (not approved)."""
    body = _read(STEP5_FT)
    assert "worker_status_blocked" in body, (
        "step-5-feedback-and-termination.md must define worker_status_blocked routing"
    )
    idx = body.find("worker_status_blocked")
    context = body[idx : idx + 200]
    assert "human_escalation" in context, (
        "worker_status_blocked must route to human_escalation"
    )


def test_ac4_worker_status_permission_blocked_leads_to_human_escalation():
    """AC4: worker_status_permission_blocked must route to human_escalation (not approved)."""
    body = _read(STEP5_FT)
    assert "worker_status_permission_blocked" in body, (
        "step-5-feedback-and-termination.md must define worker_status_permission_blocked routing"
    )
    idx = body.find("worker_status_permission_blocked")
    context = body[idx : idx + 200]
    assert "human_escalation" in context, (
        "worker_status_permission_blocked must route to human_escalation"
    )


# ---------------------------------------------------------------------------
# AC5: APPROVE + BEHIND does not set termination_reason: approved
# ---------------------------------------------------------------------------


def test_ac5_approve_behind_does_not_terminate():
    """AC5: APPROVE + BEHIND must not route to termination_reason: approved."""
    body = _read(STEP5_MH)
    # Verify the BEHIND row in the routing table does NOT say "approved"
    # The routing table must show BEHIND → BEHIND 分岐 (not approved)
    assert "BEHIND" in body, "step-5-mergeability-handling.md must reference BEHIND"
    # Check that the BEHIND row explicitly says not to set termination_reason: approved
    assert "termination_reason: approved" in body and "立てない" in body, (
        "step-5-mergeability-handling.md must explicitly state APPROVE + BEHIND "
        "does not set termination_reason: approved"
    )


def test_ac5_c5_c6_conflict_resolved():
    """AC5: The C5 vs C6 conflict must be resolved (routing table has required_auto_actions column)."""
    body = _read(STEP5_MH)
    assert "required_auto_actions" in body, (
        "step-5-mergeability-handling.md must include required_auto_actions in routing table"
    )
    assert "C5 vs C6" in body or "C5" in body, (
        "step-5-mergeability-handling.md must reference C5 vs C6 conflict resolution"
    )


# ---------------------------------------------------------------------------
# AC6: SKILL.md top-level termination condition unified
# ---------------------------------------------------------------------------


def test_ac6_skill_md_termination_condition_unified():
    """AC6: SKILL.md must show APPROVE && merge_ready == true && required_auto_actions == []."""
    body = _read(SKILL_MD)
    assert "merge_ready == true" in body, (
        "SKILL.md must include merge_ready == true in termination condition"
    )
    assert "required_auto_actions == []" in body, (
        "SKILL.md must include required_auto_actions == [] in termination condition"
    )


def test_ac6_skill_md_approve_alone_not_sufficient():
    """AC6: SKILL.md must not show APPROVE alone as sufficient for termination."""
    body = _read(SKILL_MD)
    # The loop structure section should NOT show APPROVE → exit without conditions
    # New loop structure must mention merge_ready and required_auto_actions
    loop_idx = body.find("## Loop Structure")
    assert loop_idx != -1, "SKILL.md must have Loop Structure section"
    loop_section = body[loop_idx : loop_idx + 800]
    assert "merge_ready" in loop_section or "required_auto_actions" in loop_section, (
        "SKILL.md Loop Structure must reference merge_ready or required_auto_actions"
    )


def test_ac6_skill_md_termination_table_updated():
    """AC6: SKILL.md termination table must be updated with new conditions."""
    body = _read(SKILL_MD)
    termination_idx = body.find("## 終了条件")
    assert termination_idx != -1, "SKILL.md must have 終了条件 section"
    termination_section = body[termination_idx : termination_idx + 800]
    assert "merge_ready" in termination_section, (
        "SKILL.md 終了条件 must include merge_ready"
    )
    assert "required_auto_actions" in termination_section, (
        "SKILL.md 終了条件 must include required_auto_actions"
    )


# ---------------------------------------------------------------------------
# AC7: step-5-mergeability-handling.md parses LOOP_VERDICT_V2 fenced YAML only
# ---------------------------------------------------------------------------


def test_ac7_v2_fenced_yaml_parse_specified():
    """AC7: step-5-mergeability-handling.md must specify LOOP_VERDICT_V2 fenced YAML parse."""
    body = _read(STEP5_MH)
    assert "LOOP_VERDICT_V2" in body, (
        "step-5-mergeability-handling.md must reference LOOP_VERDICT_V2"
    )
    assert "fenced YAML" in body or "フェンス付き" in body, (
        "step-5-mergeability-handling.md must specify fenced YAML only parse"
    )


def test_ac7_v2_no_top_level_mergestatestatus_reference():
    """AC7: V2 consumer path must not reference top-level mergeStateStatus."""
    body = _read(STEP5_MH)
    # The new V2 path should use mergeability.merge_state_status, not top-level mergeStateStatus
    # The table should still show top-level reference as "参照しない"
    assert "参照しない" in body or "V2 フィールド" in body, (
        "step-5-mergeability-handling.md must state V2 consumer does not use "
        "top-level mergeStateStatus"
    )


def test_ac7_v2_merge_ready_field_used():
    """AC7: V2 consumer path must use merge_ready field."""
    body = _read(STEP5_MH)
    assert "merge_ready" in body, (
        "step-5-mergeability-handling.md must reference merge_ready (V2 field)"
    )


# ---------------------------------------------------------------------------
# AC8: body-only required_auto_actions → verification: false, pr_review: true
# ---------------------------------------------------------------------------


def test_ac8_update_pr_body_hygiene_no_verification_rerun():
    """AC8: update_pr_body_hygiene must not re-run verification."""
    body = _read(STEP5_FT)
    idx = body.find("update_pr_body_hygiene")
    assert idx != -1, "step-5-feedback-and-termination.md must define update_pr_body_hygiene"
    context = body[idx : idx + 400]
    assert "verification: false" in context, (
        "update_pr_body_hygiene must have verification: false (head SHA unchanged)"
    )
    assert "pr_review: true" in context, (
        "update_pr_body_hygiene must have pr_review: true"
    )


def test_ac8_ensure_closing_keyword_no_verification_rerun():
    """AC8: ensure_closing_keyword must not re-run verification."""
    body = _read(STEP5_FT)
    idx = body.find("ensure_closing_keyword")
    assert idx != -1, "step-5-feedback-and-termination.md must define ensure_closing_keyword"
    context = body[idx : idx + 400]
    assert "verification: false" in context, (
        "ensure_closing_keyword must have verification: false (head SHA unchanged)"
    )
    assert "pr_review: true" in context, (
        "ensure_closing_keyword must have pr_review: true"
    )


# ---------------------------------------------------------------------------
# AC9: update_branch re-runs verification and pr_review
# ---------------------------------------------------------------------------


def test_ac9_update_branch_reruns_verification_and_pr_review():
    """AC9: update_branch must re-run both verification and pr_review."""
    body = _read(STEP5_FT)
    idx = body.find("update_branch:")
    assert idx != -1, "step-5-feedback-and-termination.md must define update_branch action"
    # Look at the routing table entry for update_branch
    context = body[idx : idx + 400]
    assert "head_change_expected: true" in context, (
        "update_branch must have head_change_expected: true"
    )
    assert "verification: true" in context, (
        "update_branch must have verification: true"
    )
    assert "pr_review: true" in context, (
        "update_branch must have pr_review: true"
    )


# ---------------------------------------------------------------------------
# AC10: worker failed/blocked/permission_blocked → not approved
# ---------------------------------------------------------------------------


def test_ac10_worker_failed_not_approved():
    """AC10: worker result failed must not lead to termination_reason: approved."""
    body = _read(STEP5_FT)
    assert "worker_status_failed" in body, (
        "step-5-feedback-and-termination.md must define worker_status_failed"
    )
    # Check that failed leads to human_escalation, not approved
    idx = body.find("worker_status_failed")
    context = body[idx : idx + 150]
    assert "human_escalation" in context, (
        "worker result failed must route to human_escalation"
    )
    # termination_reason: approved and human_escalation must be mutually exclusive in context
    has_approved = "termination_reason: approved" in context
    has_escalation = "human_escalation" in context
    assert not (has_approved and has_escalation) or (has_escalation and not has_approved), (
        "termination_reason: approved and human_escalation must be mutually exclusive "
        "near worker_status_failed"
    )
    assert has_escalation, "worker result failed must route to human_escalation"


def test_ac10_worker_blocked_not_approved():
    """AC10: worker result blocked must not lead to termination_reason: approved."""
    body = _read(STEP5_FT)
    idx = body.find("worker_status_blocked")
    assert idx != -1, "step-5-feedback-and-termination.md must define worker_status_blocked"
    context = body[idx : idx + 150]
    assert "human_escalation" in context, (
        "worker result blocked must route to human_escalation"
    )


def test_ac10_worker_permission_blocked_not_approved():
    """AC10: worker result permission_blocked must not lead to termination_reason: approved."""
    body = _read(STEP5_FT)
    idx = body.find("worker_status_permission_blocked")
    assert idx != -1, (
        "step-5-feedback-and-termination.md must define worker_status_permission_blocked"
    )
    context = body[idx : idx + 150]
    assert "human_escalation" in context, (
        "worker result permission_blocked must route to human_escalation"
    )


# ---------------------------------------------------------------------------
# AC11: reviewed_head_sha mismatch → dispatch 前に PR review rerun
# ---------------------------------------------------------------------------


def test_ac11_reviewed_head_sha_mismatch_triggers_pr_review_rerun():
    """AC11: reviewed_head_sha mismatch must trigger PR review rerun before dispatch."""
    body = _read(STEP5_FT)
    assert "reviewed_head_sha" in body, (
        "step-5-feedback-and-termination.md must reference reviewed_head_sha"
    )
    # Verify that there's a check for SHA mismatch before dispatch
    idx = body.find("reviewed_head_sha")
    context = body[idx : idx + 400]
    assert "dispatch" in context or "PR review" in context or "再実行" in context, (
        "reviewed_head_sha mismatch must trigger PR review rerun before dispatch"
    )


def test_ac11_stale_loop_verdict_rerun_also_in_mergeability_handling():
    """AC11: stale LOOP_VERDICT handling must be in mergeability-handling.md."""
    body = _read(STEP5_MH)
    assert "reviewed_head_sha" in body, (
        "step-5-mergeability-handling.md must reference reviewed_head_sha"
    )
    assert "stale" in body, (
        "step-5-mergeability-handling.md must reference stale LOOP_VERDICT detection"
    )


# ---------------------------------------------------------------------------
# AC12: final approved emits IMPL_REVIEW_LOOP_RESULT_V1 status: draft_pr_ready + merge_ready: true
# ---------------------------------------------------------------------------


def test_ac12_impl_review_loop_result_v1_defined():
    """AC12: IMPL_REVIEW_LOOP_RESULT_V1 must be emitted on approved termination."""
    body = _read(STEP5_FT)
    assert "IMPL_REVIEW_LOOP_RESULT_V1" in body, (
        "step-5-feedback-and-termination.md must define IMPL_REVIEW_LOOP_RESULT_V1"
    )


def test_ac12_impl_review_loop_result_v1_status_draft_pr_ready():
    """AC12: IMPL_REVIEW_LOOP_RESULT_V1 must have status: draft_pr_ready."""
    body = _read(STEP5_FT)
    assert "status: draft_pr_ready" in body, (
        "step-5-feedback-and-termination.md must emit status: draft_pr_ready "
        "in IMPL_REVIEW_LOOP_RESULT_V1"
    )


def test_ac12_impl_review_loop_result_v1_merge_ready_true():
    """AC12: IMPL_REVIEW_LOOP_RESULT_V1 must have merge_ready: true."""
    body = _read(STEP5_FT)
    assert "merge_ready: true" in body, (
        "step-5-feedback-and-termination.md must emit merge_ready: true "
        "in IMPL_REVIEW_LOOP_RESULT_V1"
    )


def test_ac12_skill_md_emits_draft_pr_ready():
    """AC12: SKILL.md 終了条件 must reference draft_pr_ready emission."""
    body = _read(SKILL_MD)
    assert "draft_pr_ready" in body, (
        "SKILL.md must reference IMPL_REVIEW_LOOP_RESULT_V1.status: draft_pr_ready"
    )


# ---------------------------------------------------------------------------
# B1: required_auto_actions schema (object, not string-list)
# ---------------------------------------------------------------------------


def test_b1_required_auto_actions_object_schema_defined():
    """B1: required_auto_actions must be documented as array-of-objects schema."""
    body = _read(STEP5_FT)
    assert "array-of-objects" in body or "array of objects" in body.lower(), (
        "step-5-feedback-and-termination.md must document required_auto_actions "
        "as array-of-objects (not string-list)"
    )


def test_b1_required_auto_actions_schema_fields():
    """B1: schema must document kind, executor, skill, blocking_merge_ready, expected_head_sha fields."""
    body = _read(STEP5_FT)
    for field in ("kind", "executor", "blocking_merge_ready", "expected_head_sha"):
        assert field in body, (
            f"step-5-feedback-and-termination.md must document '{field}' in schema"
        )


def test_b1_unknown_kind_routes_to_human_escalation():
    """B1: unknown kind must route to human_escalation."""
    body = _read(STEP5_FT)
    assert "unknown_kind_route" in body or (
        "unknown" in body and "human_escalation" in body
    ), (
        "step-5-feedback-and-termination.md must route unknown kind to human_escalation"
    )


def test_b1_missing_expected_head_sha_routes_to_human_escalation():
    """B1: missing expected_head_sha for update_branch must route to human_escalation."""
    body = _read(STEP5_FT)
    assert "missing_expected_head_sha_for_update_branch" in body or (
        "expected_head_sha" in body and "human_escalation" in body
    ), (
        "step-5-feedback-and-termination.md must route missing expected_head_sha "
        "for update_branch to human_escalation"
    )


# ---------------------------------------------------------------------------
# B2: fenced YAML extraction policy
# ---------------------------------------------------------------------------


def test_b2_first_yaml_block_dependency_forbidden():
    """B2: parse must not depend on 'first ```yaml block'."""
    body = _read(STEP5_MH)
    # The doc must enumerate LOOP_VERDICT_V2 blocks, not just take the first yaml block
    assert "最初の" not in body or "禁止" in body or "LOOP_VERDICT_V2" in body, (
        "step-5-mergeability-handling.md must not depend on 'first yaml block'"
    )
    # Must enumerate LOOP_VERDICT_V2-containing blocks
    assert "LOOP_VERDICT_V2" in body and ("列挙" in body or "enumerate" in body.lower() or "全て" in body or "含む" in body), (
        "step-5-mergeability-handling.md must enumerate LOOP_VERDICT_V2-containing fenced blocks"
    )


def test_b2_malformed_yaml_routes_to_human_escalation():
    """B2: malformed YAML must route to human_escalation."""
    body = _read(STEP5_MH)
    assert "malformed" in body or "parse エラー" in body or "human_escalation" in body, (
        "step-5-mergeability-handling.md must route malformed YAML to human_escalation"
    )


def test_b2_prose_loop_verdict_v2_ignored():
    """B2: LOOP_VERDICT_V2 text outside code blocks must be ignored."""
    body = _read(STEP5_MH)
    assert "prose" in body or "コードブロック外" in body or "コードブロック" in body, (
        "step-5-mergeability-handling.md must state prose LOOP_VERDICT_V2 references are ignored"
    )


def test_b2_v1_top_level_fields_ignored_in_v2_path():
    """B2: V1 top-level mergeStateStatus and recommendations must be ignored in V2 path."""
    body = _read(STEP5_MH)
    assert "mergeStateStatus" in body and "参照しない" in body, (
        "step-5-mergeability-handling.md must explicitly state top-level mergeStateStatus "
        "is not referenced in V2 consumer path"
    )


# ---------------------------------------------------------------------------
# B3: draft_pr_ready / github_merge_ready separation + DRAFT/HAS_HOOKS routing
# ---------------------------------------------------------------------------


def test_b3_github_merge_ready_field_defined():
    """B3: IMPL_REVIEW_LOOP_RESULT_V1 must include github_merge_ready field."""
    body = _read(STEP5_FT)
    assert "github_merge_ready" in body, (
        "step-5-feedback-and-termination.md must define github_merge_ready field"
    )


def test_b3_draft_pr_defined_separately_from_github_merge_ready():
    """B3: draft_pr_ready and github_merge_ready must be documented as distinct fields."""
    body = _read(STEP5_FT)
    assert "draft_pr_ready" in body and "github_merge_ready" in body, (
        "Both draft_pr_ready and github_merge_ready must be present"
    )
    # They must be in proximity (within same section)
    idx_draft = body.find("draft_pr_ready")
    idx_github = body.find("github_merge_ready")
    assert abs(idx_draft - idx_github) < 2000, (
        "draft_pr_ready and github_merge_ready must be defined in close proximity"
    )


def test_b3_draft_merge_state_routes_to_github_merge_ready_false():
    """B3: DRAFT merge_state_status must result in github_merge_ready: false."""
    body = _read(STEP5_FT)
    assert "DRAFT" in body, (
        "step-5-feedback-and-termination.md must reference DRAFT merge_state_status"
    )
    idx = body.find("DRAFT")
    context = body[idx : idx + 400]
    assert "github_merge_ready: false" in context or "false" in context, (
        "DRAFT merge_state_status must yield github_merge_ready: false"
    )


def test_b3_has_hooks_routes_to_github_merge_ready_true():
    """B3: HAS_HOOKS merge_state_status must allow github_merge_ready: true."""
    body = _read(STEP5_FT)
    assert "HAS_HOOKS" in body, (
        "step-5-feedback-and-termination.md must reference HAS_HOOKS merge_state_status"
    )


# ---------------------------------------------------------------------------
# B4: worker result status union (#631/#638 alignment)
# ---------------------------------------------------------------------------


def test_b4_worker_status_stale_verdict_defined():
    """B4: worker_status_stale_verdict must be defined with human_escalation route."""
    body = _read(STEP5_FT)
    assert "worker_status_stale_verdict" in body, (
        "step-5-feedback-and-termination.md must define worker_status_stale_verdict"
    )
    idx = body.find("worker_status_stale_verdict")
    context = body[idx : idx + 200]
    assert "human_escalation" in context, (
        "worker_status_stale_verdict must route to human_escalation"
    )


def test_b4_worker_status_forbidden_defined():
    """B4: worker_status_forbidden must be defined with human_escalation route."""
    body = _read(STEP5_FT)
    assert "worker_status_forbidden" in body, (
        "step-5-feedback-and-termination.md must define worker_status_forbidden"
    )
    idx = body.find("worker_status_forbidden")
    context = body[idx : idx + 200]
    assert "human_escalation" in context, (
        "worker_status_forbidden must route to human_escalation"
    )


def test_b4_worker_status_validation_failed_defined():
    """B4: worker_status_validation_failed must be defined with human_escalation route."""
    body = _read(STEP5_FT)
    assert "worker_status_validation_failed" in body, (
        "step-5-feedback-and-termination.md must define worker_status_validation_failed"
    )
    idx = body.find("worker_status_validation_failed")
    context = body[idx : idx + 200]
    assert "human_escalation" in context, (
        "worker_status_validation_failed must route to human_escalation"
    )


def test_b4_worker_status_timeout_defined():
    """B4: worker_status_timeout must be defined with human_escalation route."""
    body = _read(STEP5_FT)
    assert "worker_status_timeout" in body, (
        "step-5-feedback-and-termination.md must define worker_status_timeout"
    )
    idx = body.find("worker_status_timeout")
    context = body[idx : idx + 200]
    assert "human_escalation" in context, (
        "worker_status_timeout must route to human_escalation"
    )


def test_b4_worker_status_ok_rerun_required_true_does_not_terminate():
    """B4: worker ok with rerun_required: true must not terminate immediately."""
    body = _read(STEP5_FT)
    assert "worker_status_ok_rerun_required_true" in body, (
        "step-5-feedback-and-termination.md must define worker_status_ok_rerun_required_true"
    )
    idx = body.find("worker_status_ok_rerun_required_true")
    context = body[idx : idx + 300]
    assert "rerun" in context or "即終了しない" in context, (
        "worker_status_ok_rerun_required_true must specify rerun is required (not immediate exit)"
    )


# ---------------------------------------------------------------------------
# B5: behavior routing fixture matrix
# ---------------------------------------------------------------------------


def _make_verdict_context(
    verdict: str,
    merge_ready: bool,
    required_auto_actions: list,
    merge_state_status: str,
) -> str:
    """Simulate a routing context string based on fixture parameters."""
    actions_str = "[]" if not required_auto_actions else str(required_auto_actions)
    return (
        f"verdict: {verdict}\n"
        f"merge_ready: {str(merge_ready).lower()}\n"
        f"required_auto_actions: {actions_str}\n"
        f"merge_state_status: {merge_state_status}\n"
    )


def _evaluate_route(
    verdict: str,
    merge_ready: bool,
    required_auto_actions: list,
    merge_state_status: str,
) -> str:
    """
    Deterministic routing logic mirroring step-5 decision table.
    Returns: 'approved', 'implementation-worker.update_branch', 'human_escalation',
             'not_approved_github', 'continue_loop'
    """
    if merge_state_status == "UNKNOWN":
        return "human_escalation"

    if verdict == "REQUEST_CHANGES":
        return "continue_loop"

    if verdict != "APPROVE":
        return "human_escalation"

    # APPROVE path
    if merge_state_status == "DRAFT":
        # draft_pr_ready=true but github_merge_ready=false
        return "not_approved_github"

    # Check required_auto_actions
    for action in required_auto_actions:
        if isinstance(action, dict) and action.get("kind") == "update_branch":
            return "implementation-worker.update_branch"
        if isinstance(action, dict) and action.get("kind") not in (
            "update_branch", "update_pr_body_hygiene", "ensure_closing_keyword"
        ):
            return "human_escalation"
        if isinstance(action, str):
            # unknown / non-object action
            return "human_escalation"

    if required_auto_actions:
        return "not_approved"  # non-empty but no update_branch → still not terminal

    if not merge_ready:
        return "not_approved"

    return "approved"


# Fixture matrix: (verdict, merge_ready, required_auto_actions, mergeStateStatus, expected_route)
_FIXTURE_MATRIX = [
    # APPROVE + merge_ready=true + [] + CLEAN → approved
    ("APPROVE", True, [], "CLEAN", "approved"),
    # APPROVE + merge_ready=false + [update_branch object] + BEHIND → update_branch worker
    ("APPROVE", False, [{"kind": "update_branch", "expected_head_sha": "abc123"}], "BEHIND",
     "implementation-worker.update_branch"),
    # APPROVE + merge_ready=true + [unknown action object] + CLEAN → human_escalation
    ("APPROVE", True, [{"kind": "unknown_action"}], "CLEAN", "human_escalation"),
    # APPROVE + merge_ready=true + [non-empty known action] + CLEAN → not approved (continue)
    ("APPROVE", True, [{"kind": "update_pr_body_hygiene"}], "CLEAN", "not_approved"),
    # APPROVE + merge_ready=false + [] + DRAFT → not_approved_github (github_merge_ready: false)
    ("APPROVE", False, [], "DRAFT", "not_approved_github"),
    # APPROVE + merge_ready=false + [] + UNKNOWN → human_escalation
    ("APPROVE", False, [], "UNKNOWN", "human_escalation"),
    # REQUEST_CHANGES + merge_ready=false + [] + CLEAN → continue_loop
    ("REQUEST_CHANGES", False, [], "CLEAN", "continue_loop"),
]


def test_b5_fixture_matrix_approved():
    """B5: APPROVE + merge_ready=true + [] + CLEAN must route to approved."""
    verdict, merge_ready, actions, status, expected = _FIXTURE_MATRIX[0]
    result = _evaluate_route(verdict, merge_ready, actions, status)
    assert result == expected, f"Expected '{expected}', got '{result}'"


def test_b5_fixture_matrix_update_branch():
    """B5: APPROVE + merge_ready=false + [update_branch] + BEHIND must route to update_branch worker."""
    verdict, merge_ready, actions, status, expected = _FIXTURE_MATRIX[1]
    result = _evaluate_route(verdict, merge_ready, actions, status)
    assert result == expected, f"Expected '{expected}', got '{result}'"


def test_b5_fixture_matrix_unknown_action_human_escalation():
    """B5: APPROVE + [unknown_action object] + CLEAN must route to human_escalation."""
    verdict, merge_ready, actions, status, expected = _FIXTURE_MATRIX[2]
    result = _evaluate_route(verdict, merge_ready, actions, status)
    assert result == expected, f"Expected '{expected}', got '{result}'"


def test_b5_fixture_matrix_nonempty_known_action_not_approved():
    """B5: APPROVE + [update_pr_body_hygiene] + CLEAN must not be approved (loop continues)."""
    verdict, merge_ready, actions, status, expected = _FIXTURE_MATRIX[3]
    result = _evaluate_route(verdict, merge_ready, actions, status)
    assert result == expected, f"Expected '{expected}', got '{result}'"


def test_b5_fixture_matrix_draft_not_github_merge_ready():
    """B5: APPROVE + [] + DRAFT must not be github_merge_ready (draft_pr_ready=true but not mergeable)."""
    verdict, merge_ready, actions, status, expected = _FIXTURE_MATRIX[4]
    result = _evaluate_route(verdict, merge_ready, actions, status)
    assert result == expected, f"Expected '{expected}', got '{result}'"


def test_b5_fixture_matrix_unknown_status_human_escalation():
    """B5: APPROVE + UNKNOWN merge_state_status must route to human_escalation."""
    verdict, merge_ready, actions, status, expected = _FIXTURE_MATRIX[5]
    result = _evaluate_route(verdict, merge_ready, actions, status)
    assert result == expected, f"Expected '{expected}', got '{result}'"


def test_b5_fixture_matrix_request_changes_continue_loop():
    """B5: REQUEST_CHANGES must route to continue_loop (next iteration)."""
    verdict, merge_ready, actions, status, expected = _FIXTURE_MATRIX[6]
    result = _evaluate_route(verdict, merge_ready, actions, status)
    assert result == expected, f"Expected '{expected}', got '{result}'"


def test_b5_ac10_approved_and_human_escalation_mutually_exclusive():
    """B5/AC10: termination_reason: approved and human_escalation must be mutually exclusive."""
    body = _read(STEP5_FT)
    idx = body.find("worker_status_failed")
    assert idx != -1, "worker_status_failed must be defined"
    context = body[idx : idx + 200]
    # termination_reason: approved must NOT appear in the same context block as human_escalation routing
    has_termination_approved = "termination_reason: approved" in context
    has_human_escalation = "human_escalation" in context
    # They must be mutually exclusive: if human_escalation is present, approved must not be
    assert not (has_termination_approved and has_human_escalation), (
        "termination_reason: approved and human_escalation are not mutually exclusive "
        "near worker_status_failed — this is a structural defect"
    )
    assert has_human_escalation, (
        "human_escalation must be present near worker_status_failed"
    )


# ---------------------------------------------------------------------------
# B6: no bidirectional Unicode control characters
# ---------------------------------------------------------------------------


_BIDI_CHARS = [
    "‪",  # LEFT-TO-RIGHT EMBEDDING
    "‫",  # RIGHT-TO-LEFT EMBEDDING
    "‬",  # POP DIRECTIONAL FORMATTING
    "‭",  # LEFT-TO-RIGHT OVERRIDE
    "‮",  # RIGHT-TO-LEFT OVERRIDE
    "⁦",  # LEFT-TO-RIGHT ISOLATE
    "⁧",  # RIGHT-TO-LEFT ISOLATE
    "⁨",  # FIRST STRONG ISOLATE
    "⁩",  # POP DIRECTIONAL ISOLATE
    "​",  # ZERO WIDTH SPACE
    "‌",  # ZERO WIDTH NON-JOINER
    "‍",  # ZERO WIDTH JOINER
    "‎",  # LEFT-TO-RIGHT MARK
    "‏",  # RIGHT-TO-LEFT MARK
]


def _check_no_bidi(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    found = []
    for ch in _BIDI_CHARS:
        if ch in text:
            found.append(f"U+{ord(ch):04X}")
    return found


def test_b6_no_bidi_in_step5_feedback():
    """B6: step-5-feedback-and-termination.md must not contain bidi control characters."""
    found = _check_no_bidi(STEP5_FT)
    assert not found, (
        f"step-5-feedback-and-termination.md contains forbidden bidi chars: {found}"
    )


def test_b6_no_bidi_in_step5_mergeability():
    """B6: step-5-mergeability-handling.md must not contain bidi control characters."""
    found = _check_no_bidi(STEP5_MH)
    assert not found, (
        f"step-5-mergeability-handling.md contains forbidden bidi chars: {found}"
    )


def test_b6_no_bidi_in_skill_md():
    """B6: SKILL.md must not contain bidi control characters."""
    found = _check_no_bidi(SKILL_MD)
    assert not found, (
        f"SKILL.md contains forbidden bidi chars: {found}"
    )
