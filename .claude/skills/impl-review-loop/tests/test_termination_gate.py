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
    # Must NOT say approved near failed
    assert "approved" not in context.lower() or "human_escalation" in context, (
        "worker result failed must not lead to approved"
    )


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
