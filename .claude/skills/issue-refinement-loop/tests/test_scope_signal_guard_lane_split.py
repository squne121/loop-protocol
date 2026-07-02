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
        # is represented via target_issue_number mismatch (the normalized
        # evidence contract never carries a raw external URL as "valid").
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
