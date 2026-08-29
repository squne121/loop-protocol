"""Behavioral tests for the severity-arbitration deadlock override
(Issue #2396 AC2/AC3).

GIVEN `run_refinement_preflight.py`'s severity arbitration
(`run_preflight()`'s "Structural repair routing" block) WHEN a
`structural_repair_action` bundle is fully `auto_apply_safe` and exactly
covers the SAME `missing_required_section` blocker(s) that made the
pre-existing status (`blocked`, rank 3) more severe than the structural
target (`needs_fix`, rank 2) THEN the deadlock is resolved: the structural
verdict is adopted (`status: needs_fix` /
`next_action: apply_deterministic_structural_repair`) instead of being
deferred (Issue #2180's blocked-precedence incident).

Any of ambiguous insertion / incomplete coverage / an unrelated blocker
namespace must instead preserve the EXISTING deferred behavior (regression
coverage, AC3).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import jsonschema
import pytest

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import run_refinement_preflight as wrapper  # noqa: E402

_SCHEMAS_DIR = _SKILL_ROOT / "schemas"

# A local test-double Implementation Issue template declaring 4 fields in
# top-to-bottom order: machine-readable-contract, verification-commands,
# stop-conditions, required-skills. The latter three's `attributes.value`
# are real, committed, non-placeholder defaults, so a body that omits all
# three headings entirely classifies every one of them as
# `disposition: auto_apply_safe` / `derivation: template_value_exact`
# (`_TEMPLATE_VALUE_AUTO_SAFE_FIELD_IDS`, repair_issue_contract.py).
TEMPLATE_TEXT = """\
name: "Implementation Issue"
description: "test double"
body:
  - type: textarea
    id: machine-readable-contract
    attributes:
      label: "Machine-Readable Contract"
      value: |
        ```yaml
        contract_schema_version: v1
        issue_kind: implementation
        ```
    validations:
      required: true
  - type: textarea
    id: verification-commands
    attributes:
      label: "Verification Commands"
      value: |
        - `pnpm test`
    validations:
      required: true
  - type: textarea
    id: stop-conditions
    attributes:
      label: "Stop Conditions"
      value: |
        - none
    validations:
      required: true
  - type: textarea
    id: required-skills
    attributes:
      label: "Required Skills"
      value: |
        - python
    validations:
      required: true
"""

# All three whole-section fields (Verification Commands / Stop Conditions /
# Required Skills) omitted -- only Machine-Readable Contract (with every
# required contract key present) and Outcome remain. The planner is always
# mocked directly (never the real subprocess), so this body's own heading
# completeness relative to the PLANNER's independent required-heading check
# is irrelevant to these tests.
BODY_MISSING_THREE_SECTIONS = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#0"
goal_ref: "N/A"
change_kind: code
```

## Outcome

text
"""

REQUIRED_SECTIONS_FULL = ["Verification Commands", "Stop Conditions", "Required Skills"]


def _seed_template(tmp_path: Path) -> None:
    template_dir = tmp_path / ".github" / "ISSUE_TEMPLATE"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "implementation.yml").write_text(TEMPLATE_TEXT, encoding="utf-8")


def _write_fixture(tmp_path: Path, issue_number: int, body: str) -> Path:
    fixture = {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": issue_number,
        "repo": "testowner/testrepo",
        "now": "2026-01-01T00:00:00+00:00",
        "issue": {
            "number": issue_number,
            "title": "Test Issue",
            "body": body,
            "labels": [],
            "updatedAt": "2026-01-01T00:00:00Z",
        },
        "comments": [],
        "anchor_comment_urls": [],
    }
    fixture_path = tmp_path / f"fixture-{issue_number}.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    return fixture_path


def _mock_plan_blocked(*, required_sections: list, reason_codes: "list | None" = None) -> dict:
    """A planner `fail_closed` verdict shaped exactly like the real
    `plan_refinement_loop.py` output for a missing-required-heading finding
    (the OWNER's #2180 incident report: `PLANNER_FAIL_CLOSED` +
    `missing_required_section` together), built via the SAME
    `_build_safe_rewrite_constraints()` helper the production code path
    itself uses (never a hand-typed rewrite_constraints shape)."""
    rc = wrapper._build_safe_rewrite_constraints(required_sections, [])
    return {
        "schema_version": "refinement_loop_plan/v1",
        "fail_closed": {
            "required": True,
            "reason_codes": reason_codes or ["missing_required_section"],
            "rewrite_constraints": rc,
        },
        "decisions": {},
    }


def _run_preflight_with_mock_plan(tmp_path: Path, issue_number: int, body: str, plan: dict):
    fixture_path = _write_fixture(tmp_path, issue_number, body)
    _seed_template(tmp_path)
    with (
        mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
        mock.patch.object(wrapper, "_invoke_planner", return_value=(plan, 0, "", "")),
    ):
        return wrapper.run_preflight(
            issue_number=issue_number,
            repo="testowner/testrepo",
            anchor_comment_urls=[],
            fixture_path=fixture_path,
        )


def _validate_against_result_schema(result: dict) -> None:
    schema = json.loads((_SCHEMAS_DIR / "refinement_preflight_result_v1.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(instance=result, schema=schema)


class TestAC2FullCoverageNotDeferred:
    """AC2: a fully auto_apply_safe structural bundle that exactly covers
    the pre-existing `missing_required_section` blocker(s) (2+ items,
    line-adjacent anchors) resolves the deadlock instead of being
    deferred."""

    def test_full_coverage_auto_apply_safe_not_deferred(self, tmp_path: Path) -> None:
        plan = _mock_plan_blocked(required_sections=REQUIRED_SECTIONS_FULL)
        result, exit_code = _run_preflight_with_mock_plan(
            tmp_path, 239601, BODY_MISSING_THREE_SECTIONS, plan
        )

        assert result["status"] == "needs_fix", result
        assert result["next_action"] == "apply_deterministic_structural_repair", result
        assert exit_code == wrapper.EXIT_NEEDS_FIX
        assert not any(b.startswith("structural_repair_action_deferred:") for b in result["blockers"]), result
        assert result.get("structural_repair_action") is not None
        assert result["structural_repair_action"]["disposition_summary"] == "auto_apply_safe"
        # 2+ concurrent auto_apply_safe items sharing the same near-line
        # insertion anchor (AC2's "2件以上の同時item"), covering exactly
        # the 3 required sections.
        items = result["structural_repair_action"]["items"]
        assert len(items) == 3
        assert all(i["disposition"] == "auto_apply_safe" for i in items)
        assert {i["label"] for i in items} == set(REQUIRED_SECTIONS_FULL)

        _validate_against_result_schema(result)


class TestAC3RegressionStillDeferred:
    """AC3: ambiguous insertion / incomplete coverage / an unrelated
    blocker namespace must each preserve the existing deferred behavior
    (never silently override a pre-existing blocked status)."""

    def test_ambiguous_item_still_deferred(self, tmp_path: Path) -> None:
        """AC3(a): defense-in-depth -- even a bundle whose OWN
        `disposition_summary` claims `auto_apply_safe` with an item that
        is itself `auto_apply_safe` but carries `insertion.disposition ==
        "ambiguous"` (a producer bug or an adversarial/hand-crafted
        artifact; the real producer's own `_apply_insertion_decision()`
        never emits this combination) must not be adopted."""
        adversarial_bundle = {
            "schema_version": "structural_repair_action/v1",
            "policy_version": "template-derived-structural-repair/v1",
            "issue_kind": "implementation",
            "repo": "testowner/testrepo",
            "issue_number": 239602,
            "original_body_sha256": "sha256:" + "0" * 64,
            "original_updated_at": "2026-01-01T00:00:00Z",
            "items": [
                {
                    "field_id": "stop-conditions",
                    "label": "Stop Conditions",
                    "required": True,
                    "template_field_order": 2,
                    "template_path": ".github/ISSUE_TEMPLATE/implementation.yml",
                    "template_digest": "sha256:" + "1" * 64,
                    "expected_cardinality": 1,
                    "observed_cardinality": 0,
                    "disposition": "auto_apply_safe",
                    "derivation": "template_value_exact",
                    "reason_codes": ["template_default_value_exact"],
                    "candidate_value": "- none",
                    "candidate_digest": "sha256:" + "2" * 64,
                    "repo": "testowner/testrepo",
                    "issue_number": 239602,
                    "original_body_sha256": "sha256:" + "0" * 64,
                    "original_updated_at": "2026-01-01T00:00:00Z",
                    "insertion": {
                        "disposition": "ambiguous",
                        "relation": None,
                        "anchor_field_id": None,
                        "anchor_heading": None,
                        "anchor_start_line": None,
                        "anchor_digest": None,
                        "rendered_heading": "## Stop Conditions",
                        "candidate_section_digest": "sha256:" + "3" * 64,
                    },
                }
            ],
            "disposition_summary": "auto_apply_safe",
            "template_git_blob_sha": None,
            "template_source_ref": None,
        }
        plan = _mock_plan_blocked(required_sections=["Stop Conditions"])
        with mock.patch.object(wrapper, "build_structural_repair_bundle", return_value=adversarial_bundle):
            result, exit_code = _run_preflight_with_mock_plan(
                tmp_path, 239602, BODY_MISSING_THREE_SECTIONS, plan
            )

        assert result["status"] == "blocked", result
        assert exit_code == wrapper.EXIT_BLOCKED
        assert any(b.startswith("structural_repair_action_deferred:") for b in result["blockers"]), result
        assert result.get("structural_repair_action") is None, result
        _validate_against_result_schema(result)

    def test_incomplete_coverage_still_deferred(self, tmp_path: Path) -> None:
        """AC3(b): the planner's own required_sections lists a heading the
        structural bundle's template does not track at all (coverage is
        incomplete) -- the deadlock override must not fire."""
        plan = _mock_plan_blocked(
            required_sections=[*REQUIRED_SECTIONS_FULL, "Extra Uncovered Section"]
        )
        result, exit_code = _run_preflight_with_mock_plan(
            tmp_path, 239603, BODY_MISSING_THREE_SECTIONS, plan
        )

        assert result["status"] == "blocked", result
        assert exit_code == wrapper.EXIT_BLOCKED
        assert any(b.startswith("structural_repair_action_deferred:") for b in result["blockers"]), result
        assert result.get("structural_repair_action") is None, result
        _validate_against_result_schema(result)

    def test_unrelated_blocker_still_deferred(self, tmp_path: Path) -> None:
        """AC3(c): full auto_apply_safe coverage of the missing sections,
        but an UNRELATED blocker namespace (outside
        missing_required_section[:*] / structural_repair_action_deferred:*
        / PLANNER_FAIL_CLOSED) is also present -- the deadlock override
        must not fire even though coverage itself is complete."""
        plan = _mock_plan_blocked(
            required_sections=REQUIRED_SECTIONS_FULL,
            reason_codes=["missing_required_section", "some_unrelated_blocker_namespace"],
        )
        result, exit_code = _run_preflight_with_mock_plan(
            tmp_path, 239604, BODY_MISSING_THREE_SECTIONS, plan
        )

        assert result["status"] == "blocked", result
        assert exit_code == wrapper.EXIT_BLOCKED
        assert "some_unrelated_blocker_namespace" in result["blockers"], result
        assert any(b.startswith("structural_repair_action_deferred:") for b in result["blockers"]), result
        assert result.get("structural_repair_action") is None, result
        _validate_against_result_schema(result)


class TestDirectOverrideEligibilityUnit:
    """Direct unit coverage of `_structural_deadlock_override_eligible()`
    (hygiene, not a literal AC's VC -- strengthens confidence beyond the
    end-to-end run_preflight() tests above)."""

    def _bundle(self, *, items):
        return {"items": items}

    def _safe_item(self, label: str, field_id: str) -> dict:
        return {
            "field_id": field_id,
            "label": label,
            "disposition": "auto_apply_safe",
            "insertion": {"disposition": "exact"},
        }

    def test_empty_required_targets_never_eligible(self) -> None:
        bundle = self._bundle(items=[self._safe_item("Stop Conditions", "stop-conditions")])
        assert wrapper._structural_deadlock_override_eligible(
            bundle, ["missing_required_section"], [], []
        ) is False

    def test_non_dict_structural_repair_action_never_eligible(self) -> None:
        assert wrapper._structural_deadlock_override_eligible(
            None, ["missing_required_section"], ["Stop Conditions"], []
        ) is False

    def test_human_review_required_item_never_eligible(self) -> None:
        bundle = self._bundle(
            items=[
                {
                    "field_id": "stop-conditions",
                    "label": "Stop Conditions",
                    "disposition": "human_review_required",
                    "insertion": {"disposition": "exact"},
                }
            ]
        )
        assert wrapper._structural_deadlock_override_eligible(
            bundle, ["missing_required_section"], ["Stop Conditions"], []
        ) is False

    def test_mrc_contract_key_coverage_via_field_id_suffix(self) -> None:
        bundle = self._bundle(
            items=[self._safe_item("Machine-Readable Contract: change_kind", "machine-readable-contract.change_kind")]
        )
        assert wrapper._structural_deadlock_override_eligible(
            bundle, ["missing_required_section"], [], ["change_kind"]
        ) is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
