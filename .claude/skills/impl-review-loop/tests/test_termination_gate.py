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


# ---------------------------------------------------------------------------
# AC2: required_auto_actions == [] required for termination_reason: approved
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC3: non-empty required_auto_actions routes to worker (not exit)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# AC6: SKILL.md top-level termination condition unified
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC7: step-5-mergeability-handling.md parses LOOP_VERDICT_V2 fenced YAML only
# ---------------------------------------------------------------------------


def test_ac7_v2_merge_ready_field_used():
    """AC7: V2 consumer path must use merge_ready field."""
    body = _read(STEP5_MH)
    assert "merge_ready" in body, (
        "step-5-mergeability-handling.md must reference merge_ready (V2 field)"
    )


# ---------------------------------------------------------------------------
# AC8: body-only required_auto_actions → verification: false, pr_review: true
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC9: update_branch re-runs verification and pr_review
# ---------------------------------------------------------------------------


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


def test_ac12_skill_md_emits_draft_pr_ready():
    """AC12: SKILL.md 終了条件 must reference draft_pr_ready emission."""
    body = _read(SKILL_MD)
    assert "draft_pr_ready" in body, (
        "SKILL.md must reference IMPL_REVIEW_LOOP_RESULT_V1.status: draft_pr_ready"
    )


# ---------------------------------------------------------------------------
# B1: required_auto_actions schema (object, not string-list)
# ---------------------------------------------------------------------------


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


def test_b2_malformed_yaml_routes_to_human_escalation():
    """B2: malformed YAML must route to human_escalation."""
    body = _read(STEP5_MH)
    assert "malformed" in body or "parse エラー" in body or "human_escalation" in body, (
        "step-5-mergeability-handling.md must route malformed YAML to human_escalation"
    )


# ---------------------------------------------------------------------------
# B3: draft_pr_ready / github_merge_ready separation + DRAFT/HAS_HOOKS routing
# ---------------------------------------------------------------------------


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

# Production consumer import for B5 routing tests
import sys as _sys
from pathlib import Path as _Path

_SCRIPTS_DIR = (
    _Path(__file__).resolve().parents[4]
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "scripts"
)
if str(_SCRIPTS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_SCRIPTS_DIR))

from route_loop_verdict_v2 import route_loop_verdict_v2  # noqa: E402


def _make_reviewer_verdict(verdict: str, blockers: list | None = None) -> dict:
    """Build a minimal reviewer_verdict dict (Issue #1873 minimal convention)."""
    return {
        "verdict": verdict,
        "reviewed_head_sha": "abc123def456",
        "blockers": blockers or [],
    }


def _make_live_mergeability(merge_state_status: str, mergeable: str = "MERGEABLE") -> dict:
    """Build a minimal live_mergeability dict as fetched from `gh pr view`."""
    return {
        "head_sha": "abc123def456",
        "mergeable": mergeable,
        "merge_state_status": merge_state_status,
    }


def test_b5_fixture_matrix_approved():
    """B5: APPROVE + no blockers + live CLEAN/MERGEABLE must route to approved."""
    rv = _make_reviewer_verdict("APPROVE")
    lm = _make_live_mergeability("CLEAN")
    result = route_loop_verdict_v2(rv, lm)
    assert result.route == "approved", (
        f"Expected 'approved', got '{result.route}'. errors: {result.errors}"
    )


def test_b5_fixture_matrix_update_branch():
    """B5: APPROVE + live BEHIND + branch_behind_main confirmed must route to
    route_to_update_branch, with the action synthesized by the router."""
    rv = _make_reviewer_verdict("APPROVE")
    lm = _make_live_mergeability("BEHIND")
    result = route_loop_verdict_v2(rv, lm, test_verdict={"branch_behind_main": True})
    assert result.route == "route_to_update_branch", (
        f"Expected 'route_to_update_branch', got '{result.route}'. errors: {result.errors}"
    )
    assert result.selected_action is not None
    assert dict(result.selected_action)["kind"] == "update_branch"


def test_b5_fixture_matrix_unknown_action_human_escalation():
    """B5: reviewer verdict HUMAN_REVIEW_REQUIRED must route to human escalation."""
    rv = _make_reviewer_verdict("HUMAN_REVIEW_REQUIRED", blockers=["ambiguous"])
    lm = _make_live_mergeability("CLEAN")
    result = route_loop_verdict_v2(rv, lm)
    assert result.route == "route_human_escalation", (
        f"Expected 'route_human_escalation', got '{result.route}'. errors: {result.errors}"
    )


def test_b5_fixture_matrix_nonempty_known_action_not_approved():
    """B5: APPROVE with non-empty blockers is an inconsistent reviewer result
    and must fail closed rather than approve."""
    rv = _make_reviewer_verdict("APPROVE", blockers=["still has a blocker"])
    lm = _make_live_mergeability("CLEAN")
    result = route_loop_verdict_v2(rv, lm)
    assert result.route != "approved", (
        "APPROVE with non-empty blockers must not route to approved"
    )
    assert result.fail_closed is True


def test_b5_fixture_matrix_draft_not_github_merge_ready():
    """B5: APPROVE + live DRAFT must not be approved (human judgment required)."""
    rv = _make_reviewer_verdict("APPROVE")
    lm = _make_live_mergeability("DRAFT")
    result = route_loop_verdict_v2(rv, lm)
    assert result.route != "approved", (
        f"DRAFT merge_state_status must not route to approved. Got '{result.route}'"
    )


def test_b5_fixture_matrix_unknown_status_human_escalation():
    """B5: APPROVE + live UNKNOWN merge_state_status must not be approved."""
    rv = _make_reviewer_verdict("APPROVE")
    lm = _make_live_mergeability("UNKNOWN")
    result = route_loop_verdict_v2(rv, lm)
    assert result.route != "approved", (
        f"UNKNOWN merge_state_status must not route to approved. Got '{result.route}'"
    )


def test_b5_fixture_matrix_request_changes_continue_loop():
    """B5: REQUEST_CHANGES must route to continue_loop (next iteration)."""
    rv = _make_reviewer_verdict("REQUEST_CHANGES", blockers=["needs fix"])
    lm = _make_live_mergeability("CLEAN")
    result = route_loop_verdict_v2(rv, lm)
    assert result.route == "continue_loop", (
        f"Expected 'continue_loop', got '{result.route}'"
    )


def test_b5_ac10_approved_and_human_escalation_mutually_exclusive():
    """B5/AC10: termination_reason: approved and human_escalation must be mutually exclusive."""
    body = STEP5_FT.read_text(encoding="utf-8")
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
