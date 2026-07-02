#!/usr/bin/env python3
"""
Tests for SCOPE_SIGNAL_GUARD_DECISION_V2 (#1090).

Covers the escalation lane split: scope_context.path_layer classification
(AC1), anchor-comment based Scope Delta Approval routing (AC2/AC3/AC4),
#985 / #1060 regression fixtures (AC5/AC11/AC12), missing-approval
diagnostics (AC6), Scope Delta Approval validity boundaries (AC8/AC9),
the SCOPE_SIGNAL_GUARD_DECISION_V2 artifact shape (AC10), and the
security-sensitive fail-closed gate (AC13).

This suite calls plan_refinement_loop() directly (in-process) so that the
new SCOPE_SIGNAL_GUARD_DECISION_V2 fields -- which are intentionally kept
out of schemas/refinement_loop_plan_v1.json (outside this issue's Allowed
Paths) -- can be asserted without touching schema validation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def planner_module():
    # scope_signal_delta must be importable as a top-level module name
    # because plan_refinement_loop.py does `from scope_signal_delta import ...`.
    if "scope_signal_delta" not in sys.modules:
        _load_module("scope_signal_delta", "scope_signal_delta.py")
    return _load_module("plan_refinement_loop_1090", "plan_refinement_loop.py")


ISSUE_BODY_TEMPLATE = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test"
change_kind: code
```

## Parent Issue

none

## Parent Goal Ref

- Goal: test
- Desired Destination: test

## Current Validated Scope

- docs/foo.md

## Remaining Parent Gaps

none

## Outcome

Test outcome.

## In Scope

- test scope

## Out of Scope

- N/A

## Allowed Paths

- `docs/foo.md`

## Acceptance Criteria

- [ ] AC1: something concrete happens

## Verification Commands

```bash
$ true
```

## Stop Conditions

- N/A

## Required Skills

- none
"""


def _base_input(
    issue_number: int,
    scope_signal_delta_input: dict[str, Any],
    scope_delta_approval_evidence: "dict | None" = None,
) -> dict[str, Any]:
    known_context: dict[str, Any] = {"scope_signal_delta_input": scope_signal_delta_input}
    if scope_delta_approval_evidence is not None:
        known_context["scope_delta_approval_evidence"] = scope_delta_approval_evidence
    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": issue_number,
            "title": "Test",
            "body": ISSUE_BODY_TEMPLATE,
            "labels": [],
            # AC8: the planner derives the expected owner/repo for structural
            # comment_url validation from issue.html_url (fail-closed when
            # underivable).
            "html_url": f"https://github.com/squne121/loop-protocol/issues/{issue_number}",
        },
        "comments": None,
        "known_context": known_context,
        "now": "2025-05-25T12:00:00+00:00",
    }


def _delta_input(before_allowed: str, after_allowed: str) -> dict[str, Any]:
    before_body = ISSUE_BODY_TEMPLATE.replace("- `docs/foo.md`", before_allowed)
    after_body = ISSUE_BODY_TEMPLATE.replace("- `docs/foo.md`", after_allowed)
    return {
        "before_body": before_body,
        "current_body": after_body,
        "after_body": after_body,
        "source_refs": {"before": None, "current": None, "after": None},
    }


def _trusted_evidence(target_issue_number: int, **overrides: Any) -> dict[str, Any]:
    evidence = {
        "marker_present": True,
        "target_issue_number": target_issue_number,
        "author_association": "OWNER",
        "comment_id": 123456,
        "comment_url": f"https://github.com/squne121/loop-protocol/issues/{target_issue_number}#issuecomment-123456",
        "body_sha256": "a" * 64,
        "created_at": "2026-07-01T00:00:00Z",
        "issue_url": f"https://github.com/squne121/loop-protocol/issues/{target_issue_number}",
        "rationale": "docs-only scope expansion",
    }
    evidence.update(overrides)
    return evidence


class TestPathLayerClassification:
    """AC1: scope_context.path_layer classification."""

    def test_runtime_layer(self, planner_module):
        assert planner_module._classify_path_layer("src/systems/EnemySpawnSystem.ts") == "runtime"

    def test_docs_layer(self, planner_module):
        assert planner_module._classify_path_layer("docs/dev/foo.md") == "docs"

    def test_skill_layer(self, planner_module):
        assert planner_module._classify_path_layer(".claude/skills/ci-test-performance/SKILL.md") == "skill"

    def test_hook_layer(self, planner_module):
        assert planner_module._classify_path_layer(".claude/hooks/local_main_branch_guard.sh") == "hook"

    def test_agent_layer(self, planner_module):
        assert planner_module._classify_path_layer(".claude/agents/implementation-worker.md") == "agent"

    def test_test_fixture_layer(self, planner_module):
        assert planner_module._classify_path_layer("tests/e2e/spawn.spec.ts") == "test_fixture"

    def test_unknown_layer(self, planner_module):
        assert planner_module._classify_path_layer("assets/sprites/foo.png") == "unknown"

    def test_path_layer_surfaced_in_decision_v2(self, planner_module):
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["scope_context"]["path_layer"] == ["runtime"]


class TestAnchorApprovalRouting:
    """AC2/AC3/AC4: proceed_with_notes vs human_judgment_required routing."""

    def test_ac3_no_reframe_runtime_expansion_is_human_judgment_required(self, planner_module):
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["raw_signal"]["triggered"] is True
        assert decision["route"] == "human_judgment_required"
        assert decision["scope_delta_approval"]["present"] is False
        assert decision["scope_delta_approval"]["missing_approval_field"] is True

    def test_ac2_trusted_anchor_with_matching_issue_is_proceed_with_notes(self, planner_module):
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(985),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "proceed_with_notes"
        assert decision["scope_delta_approval"]["valid"] is True

    def test_ac4_docs_only_skill_addition_with_approval_no_escalation(self, planner_module):
        input_data = _base_input(
            1060,
            _delta_input(
                "- `docs/foo.md`",
                "- `docs/foo.md`\n- `.claude/skills/ci-test-performance/scripts/check.py`",
            ),
            scope_delta_approval_evidence=_trusted_evidence(1060),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "proceed_with_notes"
        assert set(decision["scope_context"]["path_layer"]) <= {"docs", "skill"}

    def test_not_triggered_route_when_no_scope_signal(self, planner_module):
        input_data = _base_input(1, _delta_input("- `docs/foo.md`", "- `docs/foo.md`"))
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["raw_signal"]["triggered"] is False
        assert decision["route"] == "not_triggered"

    def test_v2_not_triggered_uses_approval_status_not_required(self, planner_module):
        # PR #1294 review: no scope signal must not leave misleading
        # "approval missing" diagnostics in the artifact.
        input_data = _base_input(1, _delta_input("- `docs/foo.md`", "- `docs/foo.md`"))
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        approval = plan["scope_signal_guard_decision_v2"]["scope_delta_approval"]
        assert approval["status"] == "not_required"
        assert approval["missing_approval_field"] is False
        assert approval["suggested_contract_patch"] is None


class TestScopeDeltaDecisionAdapter:
    """PR #1294 review Blocker 1: production preflight path (scope_delta_decision)
    must project into v2 scope_delta_approval and reach proceed_with_notes."""

    @staticmethod
    def _preflight_known_context(issue_number: int, delta_input: dict) -> dict:
        # Exactly what run_refinement_preflight.py propagates for a trusted
        # anchor (#920/#1027 contract): scope_delta_decision + anchor context,
        # WITHOUT any scope_delta_approval_evidence.
        anchor_url = (
            f"https://github.com/squne121/loop-protocol/issues/{issue_number}"
            "#issuecomment-777001"
        )
        return {
            "scope_signal_delta_input": delta_input,
            "anchor_reframe": True,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": "b" * 64,
            "scope_delta_decision": {
                "status": "approved_by_trusted_anchor",
                "implementation_go": False,
                "anchor_author_association": "OWNER",
                "anchor_comment_url": anchor_url,
                "anchor_comment_hash": "b" * 64,
                "allowed_path_deltas": ["src/systems/EnemySpawnSystem.ts"],
                "required_rerun": ["contract_review", "refinement_preflight"],
            },
        }

    def test_preflight_scope_delta_decision_projects_to_v2_proceed_with_notes(self, planner_module):
        issue_number = 985
        delta_input = _delta_input(
            "- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"
        )
        input_data = _base_input(issue_number, delta_input)
        input_data["known_context"] = self._preflight_known_context(issue_number, delta_input)
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        # Raw signal stays pre-exclusion even though the legacy guard is
        # suppressed by anchor_reframe_exclusion.
        assert decision["raw_signal"]["triggered"] is True
        assert decision["route"] == "proceed_with_notes"
        approval = decision["scope_delta_approval"]
        assert approval["valid"] is True
        assert approval["status"] == "approved"
        assert approval["comment_url"].endswith("#issuecomment-777001")
        assert approval["body_sha256"] == "b" * 64
        assert approval["author_association"] == "OWNER"
        assert approval["required_rerun"] == ["contract_review", "refinement_preflight"]
        # Legacy guard keeps the post-exclusion view (unchanged contract).
        legacy = plan["decisions"]["scope_signal_guard"]
        assert legacy["triggered"] is False
        assert legacy["reason_code"] == "anchor_reframe_exclusion"

    def test_fail_closed_scope_delta_decision_projects_to_invalid(self, planner_module):
        issue_number = 985
        delta_input = _delta_input(
            "- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"
        )
        input_data = _base_input(issue_number, delta_input)
        input_data["known_context"] = {
            "scope_signal_delta_input": delta_input,
            "scope_delta_decision": {
                "status": "fail_closed",
                "reason": "untrusted_author_association: 'CONTRIBUTOR'",
                "implementation_go": False,
                "anchor_author_association": "CONTRIBUTOR",
                "anchor_comment_url": "https://github.com/squne121/loop-protocol/issues/985#issuecomment-1",
                "anchor_comment_hash": "c" * 64,
                "allowed_path_deltas": [],
                "required_rerun": [],
            },
        }
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        assert plan["scope_signal_guard_decision_v2"]["route"] == "invalid_scope_delta_approval"

    def test_no_payload_scope_delta_decision_is_missing_marker_lane(self, planner_module):
        issue_number = 985
        delta_input = _delta_input(
            "- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"
        )
        input_data = _base_input(issue_number, delta_input)
        input_data["known_context"] = {
            "scope_signal_delta_input": delta_input,
            "scope_delta_decision": {
                "status": "fail_closed",
                "reason": "no_anchor_scope_reframe_v1_payload",
                "implementation_go": False,
                "anchor_author_association": "OWNER",
                "anchor_comment_url": "https://github.com/squne121/loop-protocol/issues/985#issuecomment-2",
                "anchor_comment_hash": "d" * 64,
                "allowed_path_deltas": [],
                "required_rerun": [],
            },
        }
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "human_judgment_required"
        assert decision["scope_delta_approval"]["status"] == "missing_marker"


class TestRegressionFixtures:
    """AC5/AC11/AC12: #985 / #1060 regression fixtures, non-overlapping with #1086."""

    def test_ac11_issue_985_missing_approval(self, planner_module):
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "human_judgment_required"

    def test_ac11_issue_985_approval_present_still_requires_rerun(self, planner_module):
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(985),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        # proceed_with_notes is NOT implementation go; scope_delta_decision
        # (legacy field, untouched by #1090) still governs implementation_go.
        assert decision["route"] == "proceed_with_notes"

    def test_ac12_issue_1060_docs_skill_layer_deterministic_approval(self, planner_module):
        input_data = _base_input(
            1060,
            _delta_input(
                "- `docs/foo.md`",
                "- `docs/foo.md`\n- `.claude/skills/ci-test-performance/references/policy.md`",
            ),
            scope_delta_approval_evidence=_trusted_evidence(1060),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "proceed_with_notes"
        assert decision["security_sensitive"] is False


class TestMissingApprovalDiagnostics:
    """AC6: missing approval field + suggested contract patch."""

    def test_missing_approval_has_suggested_contract_patch(self, planner_module):
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        approval = plan["scope_signal_guard_decision_v2"]["scope_delta_approval"]
        assert approval["missing_approval_field"] is True
        assert approval["suggested_contract_patch"]
        assert "ANCHOR_SCOPE_REFRAME" in approval["suggested_contract_patch"]

    def test_approved_has_no_suggested_contract_patch(self, planner_module):
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(985),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        approval = plan["scope_signal_guard_decision_v2"]["scope_delta_approval"]
        assert approval["missing_approval_field"] is False
        assert approval["suggested_contract_patch"] is None


class TestApprovalValidityBoundaries:
    """AC8/AC9: approval only valid for the target issue's own comment, trusted author only."""

    def test_ac8_wrong_issue_number_is_invalid(self, planner_module):
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(999),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "invalid_scope_delta_approval"
        assert decision["scope_delta_approval"]["valid"] is False
        assert decision["scope_delta_approval"]["status"] == "invalid_scope_delta_approval"

    def test_ac8_external_url_evidence_uses_target_issue_number_mismatch(self, planner_module):
        # Approval evidence pointing at a different repo/issue's comment URL
        # fails both the target_issue_number check and the structural URL check.
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(
                42,
                comment_url="https://github.com/some-org/other-repo/issues/42#issuecomment-1",
            ),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "invalid_scope_delta_approval"

    def test_v2_rejects_external_comment_url_even_if_target_issue_number_matches(self, planner_module):
        # PR #1294 review Blocker 2 attack example: target_issue_number matches
        # but comment_url is an external host and issue_url is another repo.
        input_data = _base_input(
            1090,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(
                1090,
                comment_url="https://evil.example/some/comment",
                issue_url="https://github.com/other/repo/issues/1090",
            ),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "invalid_scope_delta_approval"
        assert decision["scope_delta_approval"]["valid"] is False

    def test_v2_rejects_other_repo_url_even_if_issue_number_matches(self, planner_module):
        # Consistent other-repo comment_url + issue_url pair must still be
        # rejected against the runtime repo derived from issue.html_url.
        input_data = _base_input(
            1090,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(
                1090,
                comment_url="https://github.com/other/repo/issues/1090#issuecomment-123456",
                issue_url="https://github.com/other/repo/issues/1090",
            ),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        assert plan["scope_signal_guard_decision_v2"]["route"] == "invalid_scope_delta_approval"

    def test_v2_rejects_pr_review_comment_url(self, planner_module):
        input_data = _base_input(
            1090,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(
                1090,
                comment_url="https://github.com/squne121/loop-protocol/pull/1090#discussion_r123456",
            ),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        assert plan["scope_signal_guard_decision_v2"]["route"] == "invalid_scope_delta_approval"

    def test_v2_rejects_comment_id_fragment_mismatch(self, planner_module):
        input_data = _base_input(
            1090,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(1090, comment_id=999999),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        assert plan["scope_signal_guard_decision_v2"]["route"] == "invalid_scope_delta_approval"

    def test_v2_fails_closed_when_runtime_repo_underivable(self, planner_module):
        # Without issue.html_url / known_context.repo the expected repo cannot
        # be established: evidence-based approval must fail closed (AC8).
        input_data = _base_input(
            1090,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(1090),
        )
        del input_data["issue"]["html_url"]
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        assert plan["scope_signal_guard_decision_v2"]["route"] == "invalid_scope_delta_approval"

    @pytest.mark.parametrize("author_association", ["CONTRIBUTOR", "NONE", None, "", "owner"])
    def test_ac9_untrusted_author_association_is_invalid(self, planner_module, author_association):
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(985, author_association=author_association),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "invalid_scope_delta_approval"

    @pytest.mark.parametrize("author_association", ["OWNER", "MEMBER", "COLLABORATOR"])
    def test_ac9_trusted_author_associations_are_valid(self, planner_module, author_association):
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(985, author_association=author_association),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "proceed_with_notes"

    def test_missing_marker_is_treated_as_missing_not_invalid(self, planner_module):
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(985, marker_present=False),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "human_judgment_required"
        assert decision["scope_delta_approval"]["status"] == "missing_marker"


class TestScopeSignalGuardDecisionV2Artifact:
    """AC10: SCOPE_SIGNAL_GUARD_DECISION_V2 artifact shape."""

    def test_artifact_has_required_top_level_fields(self, planner_module):
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(985),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["schema_version"] == "SCOPE_SIGNAL_GUARD_DECISION_V2"
        for key in ("raw_signal", "scope_context", "scope_delta_approval", "security_sensitive", "route"):
            assert key in decision

    def test_artifact_carries_comment_provenance_fields(self, planner_module):
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
            scope_delta_approval_evidence=_trusted_evidence(985),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        approval = plan["scope_signal_guard_decision_v2"]["scope_delta_approval"]
        assert approval["comment_id"] == 123456
        assert approval["comment_url"].endswith("#issuecomment-123456")
        assert approval["body_sha256"] == "a" * 64
        assert approval["author_association"] == "OWNER"
        assert approval["created_at"] == "2026-07-01T00:00:00Z"
        assert approval["issue_url"] == "https://github.com/squne121/loop-protocol/issues/985"

    def test_absent_when_scope_signal_delta_input_not_provided(self, planner_module):
        """Opt-in guard: legacy callers without scope_signal_delta_input never
        see the new field, keeping pre-existing golden fixtures byte-identical."""
        input_data = {
            "schema_version": "refinement_loop_planner_input/v1",
            "issue": {
                "number": 1,
                "title": "Test",
                "body": ISSUE_BODY_TEMPLATE,
                "labels": [],
            },
            "comments": None,
            "known_context": None,
            "now": "2025-05-25T12:00:00+00:00",
        }
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        assert "scope_signal_guard_decision_v2" not in plan


class TestSecuritySensitiveGate:
    """AC13: security-sensitive path/term is fail-closed even with approval."""

    def test_hooks_path_is_security_risk_gate_required_despite_approval(self, planner_module):
        input_data = _base_input(
            42,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `.claude/hooks/new_guard.sh`"),
            scope_delta_approval_evidence=_trusted_evidence(42),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "security_risk_gate_required"

    def test_workflows_path_is_security_risk_gate_required(self, planner_module):
        input_data = _base_input(
            42,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `.github/workflows/new_ci.yml`"),
            scope_delta_approval_evidence=_trusted_evidence(42),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "security_risk_gate_required"

    def test_security_term_in_rationale_is_security_risk_gate_required(self, planner_module):
        input_data = _base_input(
            42,
            _delta_input("- `src/existing.ts`", "- `src/existing.ts`\n- `docs/dev/new-notes.md`"),
            scope_delta_approval_evidence=_trusted_evidence(42, rationale="rotate the api token for CI"),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "security_risk_gate_required"

    def test_non_security_docs_path_is_not_gated(self, planner_module):
        input_data = _base_input(
            42,
            _delta_input("- `src/existing.ts`", "- `src/existing.ts`\n- `docs/dev/new-notes.md`"),
            scope_delta_approval_evidence=_trusted_evidence(42),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "proceed_with_notes"
        assert decision["security_sensitive"] is False

    @pytest.mark.parametrize(
        "sensitive_path",
        [
            ".github/actions/setup-node/action.yml",
            ".github/dependabot.yml",
            ".github/CODEOWNERS",
            "docs/dev/secret-policy.md",
            ".codex/agents/reviewer.md",
            ".claude/agents/implementation-worker.md",
        ],
    )
    def test_558_aligned_sensitive_paths_not_overridable_by_approval(
        self, planner_module, sensitive_path
    ):
        # PR #1294 review Blocker 4: #558-aligned security-sensitive paths
        # must route to security_risk_gate_required even with trusted approval.
        input_data = _base_input(
            42,
            _delta_input("- `docs/foo.md`", f"- `docs/foo.md`\n- `{sensitive_path}`"),
            scope_delta_approval_evidence=_trusted_evidence(42),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        assert plan["scope_signal_guard_decision_v2"]["route"] == "security_risk_gate_required"

    @pytest.mark.parametrize(
        "rationale",
        [
            "update auth flow for the deploy job",
            "tighten access-control on the runner",
            "change access_control defaults",
            "switch CI to OIDC federation",
            "rotate the deploy-key",
            "embed a private-key for signing",
        ],
    )
    def test_558_aligned_sensitive_terms_not_overridable_by_approval(
        self, planner_module, rationale
    ):
        input_data = _base_input(
            42,
            _delta_input("- `src/existing.ts`", "- `src/existing.ts`\n- `docs/dev/new-notes.md`"),
            scope_delta_approval_evidence=_trusted_evidence(42, rationale=rationale),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        assert plan["scope_signal_guard_decision_v2"]["route"] == "security_risk_gate_required"

    def test_word_boundary_author_is_not_auth_false_positive(self, planner_module):
        # "author" must not be treated as the security term "auth".
        input_data = _base_input(
            42,
            _delta_input("- `src/existing.ts`", "- `src/existing.ts`\n- `docs/dev/new-notes.md`"),
            scope_delta_approval_evidence=_trusted_evidence(
                42, rationale="add an author attribution note to docs"
            ),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["security_sensitive"] is False
        assert decision["route"] == "proceed_with_notes"


class TestFailClosedSafetyPreserved:
    """AC7: existing fail-closed safety (legacy scope_signal_guard) is unchanged."""

    def test_legacy_scope_signal_guard_fields_unchanged_shape(self, planner_module):
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        legacy = plan["decisions"]["scope_signal_guard"]
        assert set(legacy.keys()) == {
            "triggered",
            "reason_code",
            "excluded_by_anchor_reframe",
            "evidence_spans",
        }
        assert legacy["triggered"] is True
        assert legacy["reason_code"] == "new_allowed_path_layer"

    def test_malformed_scope_signal_delta_input_still_fail_closed(self, planner_module):
        input_data = _base_input(985, {"not": "a valid delta input"})
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        assert plan["fail_closed"]["required"] is True
        assert "scope_signal_guard_decision_v2" not in plan

    def test_v2_delta_failure_is_fail_closed_not_silent_absent(self, planner_module):
        # PR #1294 review: a delta failure must produce a fail-closed plan,
        # never a "normal" plan that silently omits the v2 artifact.
        input_data = _base_input(985, {"before_body": 123, "current_body": None})
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        assert plan["fail_closed"]["required"] is True
        assert "scope_signal_guard_decision_v2" not in plan
        # The plan must NOT contain a normal decisions payload pretending
        # the guard evaluation succeeded.
        assert plan["decisions"].get("scope_signal_guard") is None or plan["fail_closed"]["required"]


class TestTerminationReportIntegration:
    """#1090 AC6 (PR #1294 review Blocker 3): the rendered termination report
    surfaces missing_approval_field and suggested_contract_patch."""

    @pytest.fixture(scope="class")
    def renderer_module(self):
        return _load_module("render_termination_report_1090", "render_termination_report.py")

    def test_termination_report_includes_missing_approval_field_and_suggested_patch(
        self, planner_module, renderer_module
    ):
        # End-to-end: planner produces the v2 decision, renderer consumes it.
        input_data = _base_input(
            985,
            _delta_input("- `docs/foo.md`", "- `docs/foo.md`\n- `src/systems/EnemySpawnSystem.ts`"),
        )
        plan, exit_code = planner_module.plan_refinement_loop(input_data)
        assert exit_code == 0
        decision = plan["scope_signal_guard_decision_v2"]
        assert decision["route"] == "human_judgment_required"

        result = renderer_module.render(
            {
                "termination_reason": "human_escalation",
                "termination_cause": "human_judgment_required",
                "issue_number": 985,
                "iteration": 1,
                "blockers_summary": [
                    "scope_signal_guard_triggered",
                    "scope_signal_guard_reason_code:new_allowed_path_layer",
                ],
                "scope_signal_guard_decision_v2": decision,
            }
        )
        assert result["publishable"] is True
        body = result["body"]
        assert "scope_signal_guard_route:human_judgment_required" in body
        assert "missing_approval_field:true" in body
        assert "ANCHOR_SCOPE_REFRAME" in body  # suggested_contract_patch text

    def test_termination_report_not_polluted_for_proceed_with_notes(self, renderer_module):
        result = renderer_module.render(
            {
                "termination_reason": "human_escalation",
                "termination_cause": "human_judgment_required",
                "issue_number": 985,
                "blockers_summary": ["some_other_blocker"],
                "scope_signal_guard_decision_v2": {
                    "route": "proceed_with_notes",
                    "scope_delta_approval": {
                        "missing_approval_field": False,
                        "suggested_contract_patch": None,
                    },
                },
            }
        )
        assert result["publishable"] is True
        assert "scope_signal_guard_route" not in result["body"]

    def test_termination_report_rejects_non_object_decision(self, renderer_module):
        with pytest.raises(Exception):
            renderer_module.render(
                {
                    "termination_reason": "human_escalation",
                    "scope_signal_guard_decision_v2": "not-an-object",
                }
            )
