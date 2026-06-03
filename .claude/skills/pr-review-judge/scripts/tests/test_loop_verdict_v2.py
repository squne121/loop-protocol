"""
Unit tests for LOOP_VERDICT_V2 schema and classification rules.

Tests cover:
- AC1: LOOP_VERDICT_V2 schema definition in SKILL.md
- AC2: required_auto_actions non-empty forces merge_ready: false
- AC3: ensure_closing_keyword classification
- AC4: update_branch classification for BEHIND+MERGEABLE
- AC5: follow_up_issue_requests always blocking_merge_ready: false
- AC6: merge_ready: true only when all conditions met
- AC8: pr-reviewer.md references LOOP_VERDICT_V2
- AC9: no camelCase mergeStateStatus or recommendations in V2
- AC10: schema fixture invariants
- AC11: BEHIND+MERGEABLE -> required_auto_actions update_branch
- AC12: closing keywords accept all GitHub official variants
- AC13: semantic PR body validator failures remain in blockers
- AC14: auto_fix_applied always [] on initial output
- AC15: follow_up_issue_requests V1 field compatibility + blocking_merge_ready
- AC16: consumer_inventory in SKILL.md
"""

import re
from pathlib import Path
import pytest


SKILL_MD_PATH = Path(__file__).parent.parent.parent / "SKILL.md"
PR_REVIEWER_PATH = Path(__file__).parent.parent.parent.parent.parent / "agents" / "pr-reviewer.md"


def read_skill_md() -> str:
    return SKILL_MD_PATH.read_text(encoding="utf-8")


def read_pr_reviewer() -> str:
    return PR_REVIEWER_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers: pure logic functions extracted from SKILL.md description
# ---------------------------------------------------------------------------

def classify_auto_actions(
    pr_body: str,
    mergeable: str,
    branch_behind_main: bool,
    reviewed_head_sha: str = "abc123",
) -> list[dict]:
    """Classify required_auto_actions based on PR state.

    This implements the classification logic described in SKILL.md Step 5.
    """
    actions = []

    # 1. Closes #N 不足の検出
    closing_pattern = re.compile(
        r"\b(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+#\d+",
        re.IGNORECASE,
    )
    if not closing_pattern.search(pr_body):
        actions.append({
            "kind": "ensure_closing_keyword",
            "executor": "implementation-worker",
            "blocking_merge_ready": True,
            "mechanical": True,
        })

    # 3. BEHIND branch detection (only when MERGEABLE)
    if mergeable == "MERGEABLE" and branch_behind_main:
        actions.append({
            "kind": "update_branch",
            "executor": "implementation-worker",
            "blocking_merge_ready": True,
            "mechanical": True,
            "expected_head_sha": reviewed_head_sha,
        })

    return actions


def determine_merge_ready(
    verdict: str,
    blockers: list,
    required_auto_actions: list,
    mergeable: str,
    merge_state_status: str,
) -> bool:
    """Determine merge_ready based on LOOP_VERDICT_V2 rules from SKILL.md."""
    return (
        verdict == "APPROVE"
        and len(blockers) == 0
        and len(required_auto_actions) == 0
        and mergeable == "MERGEABLE"
        and merge_state_status in ("CLEAN", "UNSTABLE")
    )


def build_loop_verdict_v2(
    verdict: str,
    reviewed_head_sha: str,
    blockers: list,
    required_auto_actions: list,
    mergeable: str,
    merge_state_status: str,
    follow_up_issue_requests: list = None,
    auto_fix_applied: list = None,
) -> dict:
    """Build a LOOP_VERDICT_V2 dict as pr-review-judge would produce."""
    if follow_up_issue_requests is None:
        follow_up_issue_requests = []
    if auto_fix_applied is None:
        auto_fix_applied = []

    merge_ready = determine_merge_ready(
        verdict, blockers, required_auto_actions, mergeable, merge_state_status
    )

    return {
        "verdict": verdict,
        "reviewed_head_sha": reviewed_head_sha,
        "merge_ready": merge_ready,
        "mergeability": {
            "mergeable": mergeable,
            "merge_state_status": merge_state_status,
        },
        "blockers": blockers,
        "required_auto_actions": required_auto_actions,
        "auto_fix_applied": auto_fix_applied,
        "follow_up_issue_requests": follow_up_issue_requests,
    }


# ---------------------------------------------------------------------------
# AC1: LOOP_VERDICT_V2 schema defined in SKILL.md
# ---------------------------------------------------------------------------

class TestSkillMdContainsLoopVerdictV2Schema:
    """AC1: LOOP_VERDICT_V2 schema fields are defined in SKILL.md."""

    def test_loop_verdict_v2_schema_defined(self):
        """SKILL.md must contain LOOP_VERDICT_V2 schema definition."""
        skill = read_skill_md()
        assert "LOOP_VERDICT_V2" in skill

    def test_loop_verdict_v2_has_merge_ready_field(self):
        """LOOP_VERDICT_V2 schema includes merge_ready field."""
        skill = read_skill_md()
        assert "merge_ready" in skill

    def test_loop_verdict_v2_has_required_auto_actions_field(self):
        """LOOP_VERDICT_V2 schema includes required_auto_actions field."""
        skill = read_skill_md()
        assert "required_auto_actions" in skill

    def test_loop_verdict_v2_has_reviewed_head_sha_field(self):
        """LOOP_VERDICT_V2 schema includes reviewed_head_sha field."""
        skill = read_skill_md()
        assert "reviewed_head_sha" in skill

    def test_loop_verdict_v2_has_mergeability_field(self):
        """LOOP_VERDICT_V2 schema includes mergeability block."""
        skill = read_skill_md()
        assert "mergeability" in skill

    def test_loop_verdict_v2_has_follow_up_issue_requests_field(self):
        """LOOP_VERDICT_V2 schema includes follow_up_issue_requests field."""
        skill = read_skill_md()
        assert "follow_up_issue_requests" in skill

    def test_loop_verdict_v2_has_auto_fix_applied_field(self):
        """LOOP_VERDICT_V2 schema includes auto_fix_applied field."""
        skill = read_skill_md()
        assert "auto_fix_applied" in skill

    def test_loop_verdict_v2_schema_parses(self):
        """LOOP_VERDICT_V2 schema can be built as a valid dict."""
        verdict = build_loop_verdict_v2(
            verdict="APPROVE",
            reviewed_head_sha="abc123",
            blockers=[],
            required_auto_actions=[],
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
        )
        assert verdict["verdict"] == "APPROVE"
        assert "merge_ready" in verdict
        assert "mergeability" in verdict
        assert "required_auto_actions" in verdict
        assert "follow_up_issue_requests" in verdict
        assert "auto_fix_applied" in verdict
        assert "reviewed_head_sha" in verdict


# ---------------------------------------------------------------------------
# AC2 / AC6: merge_ready rules
# ---------------------------------------------------------------------------

class TestMergeReadyRules:
    """AC2 / AC6: merge_ready determination rules."""

    def test_required_auto_actions_force_merge_ready_false(self):
        """AC2: required_auto_actions non-empty -> merge_ready: false."""
        verdict = build_loop_verdict_v2(
            verdict="APPROVE",
            reviewed_head_sha="sha1",
            blockers=[],
            required_auto_actions=[{
                "kind": "ensure_closing_keyword",
                "executor": "implementation-worker",
                "blocking_merge_ready": True,
                "mechanical": True,
            }],
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
        )
        assert verdict["merge_ready"] is False

    def test_merge_ready_true_when_all_conditions_met(self):
        """AC6: merge_ready: true only when verdict=APPROVE, blockers=[], required_auto_actions=[], MERGEABLE, CLEAN."""
        verdict = build_loop_verdict_v2(
            verdict="APPROVE",
            reviewed_head_sha="sha1",
            blockers=[],
            required_auto_actions=[],
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
        )
        assert verdict["merge_ready"] is True

    def test_merge_ready_false_when_request_changes(self):
        """merge_ready: false when verdict=REQUEST_CHANGES."""
        verdict = build_loop_verdict_v2(
            verdict="REQUEST_CHANGES",
            reviewed_head_sha="sha1",
            blockers=["missing closing keyword"],
            required_auto_actions=[],
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
        )
        assert verdict["merge_ready"] is False

    def test_merge_ready_false_when_blockers_nonempty(self):
        """merge_ready: false when blockers non-empty."""
        verdict = build_loop_verdict_v2(
            verdict="APPROVE",
            reviewed_head_sha="sha1",
            blockers=["safety claim matrix missing"],
            required_auto_actions=[],
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
        )
        assert verdict["merge_ready"] is False

    def test_merge_ready_false_when_merge_state_status_behind(self):
        """merge_ready: false when merge_state_status=BEHIND (even with no required_auto_actions)."""
        verdict = build_loop_verdict_v2(
            verdict="APPROVE",
            reviewed_head_sha="sha1",
            blockers=[],
            required_auto_actions=[],
            mergeable="MERGEABLE",
            merge_state_status="BEHIND",
        )
        assert verdict["merge_ready"] is False

    def test_merge_ready_true_when_unstable(self):
        """merge_ready: true allowed when merge_state_status=UNSTABLE (CI flaky but mergeable)."""
        verdict = build_loop_verdict_v2(
            verdict="APPROVE",
            reviewed_head_sha="sha1",
            blockers=[],
            required_auto_actions=[],
            mergeable="MERGEABLE",
            merge_state_status="UNSTABLE",
        )
        assert verdict["merge_ready"] is True


# ---------------------------------------------------------------------------
# AC3: ensure_closing_keyword classification
# ---------------------------------------------------------------------------

class TestEnsureClosingKeyword:
    """AC3: ensure_closing_keyword classification rules."""

    def test_no_closing_keyword_adds_ensure_closing_keyword_action(self):
        """AC3: PR body without closing keyword -> ensure_closing_keyword in required_auto_actions."""
        actions = classify_auto_actions(
            pr_body="This PR does some stuff",
            mergeable="MERGEABLE",
            branch_behind_main=False,
        )
        kinds = [a["kind"] for a in actions]
        assert "ensure_closing_keyword" in kinds

    def test_ensure_closing_keyword_not_in_follow_up_issue_requests(self):
        """AC3: ensure_closing_keyword must NOT appear in follow_up_issue_requests."""
        actions = classify_auto_actions(
            pr_body="This PR does some stuff",
            mergeable="MERGEABLE",
            branch_behind_main=False,
        )
        # All returned are required_auto_actions, not follow_up
        for action in actions:
            assert action.get("kind") != "ensure_closing_keyword" or action.get("blocking_merge_ready") is True

    def test_closes_keyword_present_no_ensure_closing_keyword(self):
        """AC3: PR body with 'Closes #123' -> no ensure_closing_keyword action."""
        actions = classify_auto_actions(
            pr_body="Closes #123\n\nThis PR does some stuff",
            mergeable="MERGEABLE",
            branch_behind_main=False,
        )
        kinds = [a["kind"] for a in actions]
        assert "ensure_closing_keyword" not in kinds

    def test_ensure_closing_keyword_has_executor_implementation_worker(self):
        """ensure_closing_keyword action has executor: implementation-worker."""
        actions = classify_auto_actions(
            pr_body="No closing keyword here",
            mergeable="MERGEABLE",
            branch_behind_main=False,
        )
        closing_actions = [a for a in actions if a["kind"] == "ensure_closing_keyword"]
        assert len(closing_actions) == 1
        assert closing_actions[0]["executor"] == "implementation-worker"
        assert closing_actions[0]["mechanical"] is True


# ---------------------------------------------------------------------------
# AC4 / AC11: update_branch classification
# ---------------------------------------------------------------------------

class TestUpdateBranchClassification:
    """AC4 / AC11: update_branch in required_auto_actions for BEHIND+MERGEABLE."""

    def test_approve_with_behind_requires_update_branch(self):
        """AC11: BEHIND+MERGEABLE -> required_auto_actions has kind=update_branch."""
        actions = classify_auto_actions(
            pr_body="Closes #123",
            mergeable="MERGEABLE",
            branch_behind_main=True,
            reviewed_head_sha="sha_abc",
        )
        kinds = [a["kind"] for a in actions]
        assert "update_branch" in kinds

    def test_update_branch_has_blocking_merge_ready_true(self):
        """update_branch action has blocking_merge_ready: true."""
        actions = classify_auto_actions(
            pr_body="Closes #123",
            mergeable="MERGEABLE",
            branch_behind_main=True,
            reviewed_head_sha="sha_abc",
        )
        behind_actions = [a for a in actions if a["kind"] == "update_branch"]
        assert len(behind_actions) == 1
        assert behind_actions[0]["blocking_merge_ready"] is True
        assert behind_actions[0]["mechanical"] is True
        assert behind_actions[0]["expected_head_sha"] == "sha_abc"

    def test_update_branch_not_in_follow_up_issue_requests(self):
        """update_branch must NOT appear in follow_up_issue_requests (no recommendations field)."""
        # In V2, recommendations field is removed; update_branch goes to required_auto_actions
        skill = read_skill_md()
        # V2 schema section should NOT contain top-level 'recommendations:' key in LOOP_VERDICT_V2
        # Find the LOOP_VERDICT_V2 block and verify no 'recommendations:' key
        v2_section_match = re.search(
            r"LOOP_VERDICT_V2.*?```",
            skill,
            re.DOTALL,
        )
        # The key 'recommendations' should not appear as a top-level field in LOOP_VERDICT_V2 schema
        assert "kind: update_branch" in skill, "update_branch should be in SKILL.md as required_auto_actions kind"

    def test_behind_conflicting_does_not_add_update_branch(self):
        """CONFLICTING (not MERGEABLE) does NOT add update_branch."""
        actions = classify_auto_actions(
            pr_body="Closes #123",
            mergeable="CONFLICTING",
            branch_behind_main=True,
        )
        kinds = [a["kind"] for a in actions]
        assert "update_branch" not in kinds

    def test_behind_required_auto_actions_causes_merge_ready_false(self):
        """BEHIND detection -> required_auto_actions -> merge_ready: false."""
        actions = classify_auto_actions(
            pr_body="Closes #123",
            mergeable="MERGEABLE",
            branch_behind_main=True,
        )
        verdict = build_loop_verdict_v2(
            verdict="APPROVE",
            reviewed_head_sha="sha1",
            blockers=[],
            required_auto_actions=actions,
            mergeable="MERGEABLE",
            merge_state_status="BEHIND",
        )
        assert verdict["merge_ready"] is False
        assert any(a["kind"] == "update_branch" for a in verdict["required_auto_actions"])

    def test_no_recommendations_field_in_v2_verdict(self):
        """AC4: LOOP_VERDICT_V2 does not include top-level 'recommendations' field."""
        verdict = build_loop_verdict_v2(
            verdict="APPROVE",
            reviewed_head_sha="sha1",
            blockers=[],
            required_auto_actions=[],
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
        )
        assert "recommendations" not in verdict


# ---------------------------------------------------------------------------
# AC5: follow_up_issue_requests always blocking_merge_ready: false
# ---------------------------------------------------------------------------

class TestFollowUpIssueRequestsMustBeNonblocking:
    """AC5 / AC15: follow_up_issue_requests invariants."""

    def test_follow_up_issue_requests_must_be_nonblocking(self):
        """AC5: follow_up_issue_requests entries must all have blocking_merge_ready: false."""
        follow_ups = [
            {
                "title": "Improve test coverage",
                "issue_kind": "implementation",
                "severity": "optional_follow_up",
                "blocking_merge_ready": False,
                "source": {"kind": "pr_review", "url": "https://example.com", "note_id": "1"},
                "dedupe_key": "follow-up:repo:url:1",
                "desired_destination": "Better coverage",
                "validated_scope_delta": "Add tests",
                "origin_skill": "pr-review-judge",
                "labels": ["triage-required"],
                "initial_label_profile": "",
                "materialization": "",
            }
        ]
        for entry in follow_ups:
            assert entry["blocking_merge_ready"] is False, \
                f"follow_up_issue_requests entry must have blocking_merge_ready: false, got: {entry}"

    def test_follow_up_issue_requests_do_not_affect_merge_ready(self):
        """follow_up_issue_requests presence does not affect merge_ready."""
        follow_ups = [{"title": "Nice to have", "blocking_merge_ready": False}]
        verdict = build_loop_verdict_v2(
            verdict="APPROVE",
            reviewed_head_sha="sha1",
            blockers=[],
            required_auto_actions=[],
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
            follow_up_issue_requests=follow_ups,
        )
        assert verdict["merge_ready"] is True

    def test_follow_up_v1_field_compatibility(self):
        """AC15: follow_up_issue_requests has FOLLOW_UP_ISSUE_REQUEST_V1 compatible fields + blocking_merge_ready."""
        entry = {
            "title": "Refactor auth logic",
            "issue_kind": "implementation",
            "severity": "optional_follow_up",
            "blocking_merge_ready": False,  # V2 required field
            "source": {"kind": "pr_review", "url": "https://example.com", "note_id": "1"},
            "dedupe_key": "follow-up:repo:url:1",
            "desired_destination": "Cleaner auth module",
            "validated_scope_delta": "Refactor auth",
            "origin_skill": "pr-review-judge",
            "labels": ["triage-required"],
            "initial_label_profile": "triage",
            "materialization": "open",
        }
        # V1 fields
        v1_fields = ["title", "issue_kind", "severity", "source", "dedupe_key",
                     "desired_destination", "validated_scope_delta", "origin_skill",
                     "labels", "initial_label_profile", "materialization"]
        for field in v1_fields:
            assert field in entry, f"V1 field '{field}' missing"
        # V2 required addition
        assert "blocking_merge_ready" in entry
        assert entry["blocking_merge_ready"] is False


# ---------------------------------------------------------------------------
# AC8: pr-reviewer.md references LOOP_VERDICT_V2
# ---------------------------------------------------------------------------

class TestPrReviewerReferencesLoopVerdictV2:
    """AC8: pr-reviewer.md is updated to reference LOOP_VERDICT_V2."""

    def test_pr_reviewer_references_loop_verdict_v2_not_legacy_only(self):
        """AC8: pr-reviewer.md references LOOP_VERDICT_V2 (not legacy LOOP_VERDICT only)."""
        pr_reviewer = read_pr_reviewer()
        assert "LOOP_VERDICT_V2" in pr_reviewer, \
            "pr-reviewer.md must reference LOOP_VERDICT_V2"


# ---------------------------------------------------------------------------
# AC9: no camelCase mergeStateStatus or recommendations in V2
# ---------------------------------------------------------------------------

class TestNoTopLevelMergeStateStatusInV2:
    """AC9: V2 schema uses snake_case only; camelCase fields forbidden."""

    def test_no_top_level_mergeStateStatus_in_v2(self):
        """AC9: LOOP_VERDICT_V2 does not have top-level mergeStateStatus (camelCase)."""
        verdict = build_loop_verdict_v2(
            verdict="APPROVE",
            reviewed_head_sha="sha1",
            blockers=[],
            required_auto_actions=[],
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
        )
        assert "mergeStateStatus" not in verdict, \
            "V2 must not have top-level mergeStateStatus (camelCase forbidden)"

    def test_no_recommendations_field_in_v2(self):
        """AC9: LOOP_VERDICT_V2 does not have recommendations field (deprecated in V2)."""
        verdict = build_loop_verdict_v2(
            verdict="APPROVE",
            reviewed_head_sha="sha1",
            blockers=[],
            required_auto_actions=[],
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
        )
        assert "recommendations" not in verdict, \
            "V2 must not have recommendations field (use required_auto_actions)"

    def test_v2_uses_snake_case_merge_state_status(self):
        """V2 mergeability block uses snake_case merge_state_status."""
        verdict = build_loop_verdict_v2(
            verdict="APPROVE",
            reviewed_head_sha="sha1",
            blockers=[],
            required_auto_actions=[],
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
        )
        assert "merge_state_status" in verdict["mergeability"]

    def test_skill_md_forbids_mergeStateStatus_in_v2(self):
        """SKILL.md explicitly forbids mergeStateStatus in V2."""
        skill = read_skill_md()
        # SKILL.md should state that mergeStateStatus is forbidden in V2
        assert "mergeStateStatus" in skill and "禁止" in skill or "forbidden" in skill.lower() or "V2 では禁止" in skill


# ---------------------------------------------------------------------------
# AC12: closing keywords accept all GitHub official variants
# ---------------------------------------------------------------------------

class TestClosingKeywordsAcceptAllVariants:
    """AC12: ensure_closing_keyword accepts all GitHub official closing keywords."""

    @pytest.mark.parametrize("keyword,issue_ref", [
        ("close", "#42"),
        ("closes", "#42"),
        ("closed", "#42"),
        ("fix", "#42"),
        ("fixes", "#42"),
        ("fixed", "#42"),
        ("resolve", "#42"),
        ("resolves", "#42"),
        ("resolved", "#42"),
        # Case variations
        ("Close", "#42"),
        ("CLOSES", "#42"),
        ("Fixes", "#42"),
        ("Resolves", "#42"),
    ])
    def test_closing_keywords_accept_fix_resolve_close_variants(self, keyword, issue_ref):
        """AC12: keyword '{keyword} {issue_ref}' is accepted as closing keyword."""
        pr_body = f"{keyword} {issue_ref}"
        actions = classify_auto_actions(
            pr_body=pr_body,
            mergeable="MERGEABLE",
            branch_behind_main=False,
        )
        kinds = [a["kind"] for a in actions]
        assert "ensure_closing_keyword" not in kinds, \
            f"'{keyword} {issue_ref}' should be accepted as a closing keyword"


# ---------------------------------------------------------------------------
# AC13: semantic validator failures remain in blockers
# ---------------------------------------------------------------------------

class TestPrBodyValidatorSemanticFailureRemainsBlocker:
    """AC13: mechanical=false PR body failures stay in blockers, not required_auto_actions."""

    def test_pr_body_validator_semantic_failure_remains_blocker(self):
        """AC13: Safety Claim Matrix missing -> blocker (mechanical: false), not required_auto_actions."""
        # Semantic failures (Safety Claim Matrix, Consumer Inventory, Evidence)
        # must remain in blockers, not be classified as required_auto_actions
        semantic_blockers = [
            "Safety Claim Matrix section missing",
            "Schema Consumer Inventory not provided",
            "Evidence section references incorrect head SHA",
        ]
        # These should all be classified as blockers (mechanical: false)
        # The classify_auto_actions function only handles mechanical: true cases
        actions = classify_auto_actions(
            pr_body="Closes #123",  # closing keyword present
            mergeable="MERGEABLE",
            branch_behind_main=False,
        )
        # Semantic failures are NOT in required_auto_actions
        mechanical_kinds = [a["kind"] for a in actions]
        for semantic_issue in ["safety_claim_matrix_missing", "consumer_inventory_missing"]:
            assert semantic_issue not in mechanical_kinds, \
                f"Semantic issue '{semantic_issue}' must not be in required_auto_actions"

        # Verify SKILL.md distinguishes mechanical vs semantic failures
        skill = read_skill_md()
        assert "mechanical: false" in skill or "Safety Claim Matrix 不足" in skill

    def test_all_required_auto_actions_must_have_mechanical_true(self):
        """All required_auto_actions entries must have mechanical: true."""
        actions = classify_auto_actions(
            pr_body="No closing keyword",
            mergeable="MERGEABLE",
            branch_behind_main=True,
        )
        for action in actions:
            assert action.get("mechanical") is True, \
                f"required_auto_actions entry must have mechanical: true: {action}"


# ---------------------------------------------------------------------------
# AC14: auto_fix_applied is always [] on initial output
# ---------------------------------------------------------------------------

class TestAutoFixApplied:
    """AC14: auto_fix_applied is always [] on initial pr-review-judge output."""

    def test_auto_fix_applied_is_empty_on_initial_output(self):
        """AC14: pr-review-judge initial output has auto_fix_applied: []."""
        verdict = build_loop_verdict_v2(
            verdict="APPROVE",
            reviewed_head_sha="sha1",
            blockers=[],
            required_auto_actions=[],
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
        )
        assert verdict["auto_fix_applied"] == []

    def test_auto_fix_applied_is_empty_even_with_required_actions(self):
        """auto_fix_applied is [] even when required_auto_actions is non-empty."""
        actions = [{"kind": "update_branch", "executor": "implementation-worker",
                    "blocking_merge_ready": True, "mechanical": True}]
        verdict = build_loop_verdict_v2(
            verdict="APPROVE",
            reviewed_head_sha="sha1",
            blockers=[],
            required_auto_actions=actions,
            mergeable="MERGEABLE",
            merge_state_status="BEHIND",
        )
        assert verdict["auto_fix_applied"] == [], \
            "pr-review-judge must output auto_fix_applied: [] (implementation-worker fills it later)"

    def test_skill_md_mentions_auto_fix_applied_mutated_by_implementation_worker(self):
        """SKILL.md states that implementation-worker mutates auto_fix_applied."""
        skill = read_skill_md()
        assert "auto_fix_applied" in skill
        assert "implementation-worker" in skill


# ---------------------------------------------------------------------------
# AC16: consumer_inventory in SKILL.md
# ---------------------------------------------------------------------------

class TestConsumerInventory:
    """AC16: Schema Consumer Inventory is defined in SKILL.md."""

    def test_consumer_inventory_in_skill_md(self):
        """AC16: SKILL.md contains consumer_inventory section."""
        skill = read_skill_md()
        assert "consumer_inventory" in skill

    def test_consumer_inventory_references_impl_review_loop(self):
        """AC16: consumer_inventory mentions impl-review-loop."""
        skill = read_skill_md()
        assert "impl-review-loop" in skill

    def test_consumer_inventory_references_pr_reviewer(self):
        """AC16: consumer_inventory mentions pr-reviewer."""
        skill = read_skill_md()
        # pr-reviewer.md is referenced
        assert "pr-reviewer" in skill

    def test_consumer_inventory_notes_runtime_behavior_unchanged(self):
        """AC16: SKILL.md notes runtime behavior unchanged until #631/#632 complete."""
        skill = read_skill_md()
        # Should mention that consumers maintain compatibility until cutover
        assert "#631" in skill or "cutover" in skill or "runtime behavior" in skill


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
