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


def _write_fixture(
    tmp_path: Path,
    issue_number: int,
    body: str,
    *,
    anchor_comment_urls: "list[str] | None" = None,
    anchor_comments: "list[dict] | None" = None,
) -> Path:
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
        "anchor_comment_urls": anchor_comment_urls or [],
        "anchor_comments": anchor_comments or [],
    }
    fixture_path = tmp_path / f"fixture-{issue_number}.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    return fixture_path


# ---------------------------------------------------------------------------
# Issue #2598: parent-shaped (`issue_kind: parent`, `parent_mode:
# delivery-rollup`) fixtures for the `missing_required_parent_section`
# deadlock-override extension. `_PARENT_TEMPLATE_TEXT` is the REAL, checked-in
# `.github/ISSUE_TEMPLATE/parent.yml` (not a fixture-invented template) so
# `_PARENT_TARGET_SECTIONS` below are the template's own actual required
# section labels -- a renamed/fictional reason-code string could not make
# these assertions pass by coincidence.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PARENT_TEMPLATE_PATH = _REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "parent.yml"
_PARENT_TEMPLATE_TEXT = _PARENT_TEMPLATE_PATH.read_text(encoding="utf-8")

_PARENT_TARGET_SECTIONS = ("Quality Decision Record", "Child Issues", "Remaining Parent Gaps")

_PARENT_REQUIRED_SECTIONS_IN_ORDER = [
    (
        "Machine-Readable Contract",
        "```yaml\ncontract_schema_version: v1\nissue_kind: parent\ngoal_ref: g\n"
        "change_kind: workflow\nparent_mode: delivery-rollup\nclosure_mode: child-complete\n```",
    ),
    ("Summary", "summary"),
    ("Goal", "goal"),
    ("Desired Destination", "destination"),
    ("Current Validated Scope", "- scope"),
    ("Decisions Fixed", "- 2026-01-01: decision"),
    ("Quality Decision Record", "- `Status`: N/A"),
    ("Parent Closure Rule", "- delivery-rollup: child rollup complete"),
    ("Child Issues", "- [ ] #1 — child"),
    ("Remaining Parent Gaps", "- none"),
    ("Phase Handoff Contract", "- handoff"),
    ("Acceptance Criteria", "- [ ] AC1"),
]


def _seed_parent_template(tmp_path: Path) -> None:
    template_dir = tmp_path / ".github" / "ISSUE_TEMPLATE"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "parent.yml").write_text(_PARENT_TEMPLATE_TEXT, encoding="utf-8")


def _build_parent_body(*, omit_sections: "frozenset[str] | set[str]" = frozenset()) -> str:
    """Build a valid, production-shaped parent delivery-rollup contract
    (real `.github/ISSUE_TEMPLATE/parent.yml` required-section labels),
    omitting only `omit_sections`."""
    sections = [
        (label, content)
        for label, content in _PARENT_REQUIRED_SECTIONS_IN_ORDER
        if label not in omit_sections
    ]
    return "\n\n".join(f"## {heading}\n\n{content}" for heading, content in sections) + "\n"


def _owner_anchor_comment(*, issue_number: int, comment_id: int, body: str) -> "tuple[str, dict]":
    url = f"https://github.com/testowner/testrepo/issues/{issue_number}#issuecomment-{comment_id}"
    return url, {
        "id": comment_id,
        "body": body,
        "issue_url": f"https://api.github.com/repos/testowner/testrepo/issues/{issue_number}",
        "html_url": url,
        "url": f"https://api.github.com/repos/testowner/testrepo/issues/comments/{comment_id}",
        "user": {"login": "owner-user", "type": "User"},
        "author_association": "OWNER",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


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


def _run_preflight_with_mock_plan(
    tmp_path: Path,
    issue_number: int,
    body: str,
    plan: dict,
    *,
    anchor_comment_urls: "list[str] | None" = None,
    anchor_comments: "list[dict] | None" = None,
    known_context: "dict | None" = None,
    seed_parent_template: bool = False,
):
    fixture_path = _write_fixture(
        tmp_path, issue_number, body,
        anchor_comment_urls=anchor_comment_urls, anchor_comments=anchor_comments,
    )
    _seed_template(tmp_path)
    if seed_parent_template:
        _seed_parent_template(tmp_path)
    with (
        mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
        mock.patch.object(wrapper, "_invoke_planner", return_value=(plan, 0, "", "")),
    ):
        return wrapper.run_preflight(
            issue_number=issue_number,
            repo="testowner/testrepo",
            anchor_comment_urls=anchor_comment_urls or [],
            fixture_path=fixture_path,
            known_context=known_context,
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


class TestAC1ParentMissingSectionOverrideNotDeferred:
    """Issue #2598 AC1: a bare `missing_required_parent_section` planner
    blocker, for a production-shaped `issue_kind: parent` /
    `parent_mode: delivery-rollup` body whose structural repair bundle
    itself resolves ALL of the actually-missing parent target sections
    (`Quality Decision Record` / `Child Issues` / `Remaining Parent Gaps`,
    the REAL `.github/ISSUE_TEMPLATE/parent.yml` required-section labels)
    with `exact` `auto_apply_safe` insertions and full coverage, resolves
    the SAME #2180-shaped severity-arbitration deadlock #2396 already fixed
    for the generic `missing_required_section` code."""

    def test_parent_delivery_rollup_full_coverage_not_deferred(self, tmp_path: Path) -> None:
        issue_number = 259801
        anchor_body = (
            "\n\n".join(
                f"## {label}\n\n{content}"
                for label, content in (
                    ("Quality Decision Record", "- `Status`: N/A"),
                    ("Child Issues", "- [ ] #1 — child"),
                    ("Remaining Parent Gaps", "- none"),
                )
            )
            + "\n"
        )
        url, anchor_comment = _owner_anchor_comment(
            issue_number=issue_number, comment_id=5599001001, body=anchor_body
        )
        body = _build_parent_body(omit_sections=frozenset(_PARENT_TARGET_SECTIONS))
        plan = _mock_plan_blocked(
            required_sections=list(_PARENT_TARGET_SECTIONS),
            reason_codes=["missing_required_parent_section"],
        )
        result, exit_code = _run_preflight_with_mock_plan(
            tmp_path,
            issue_number,
            body,
            plan,
            anchor_comment_urls=[url],
            anchor_comments=[anchor_comment],
            known_context={"human_context_comment_urls": [url]},
            seed_parent_template=True,
        )

        assert result["status"] == "needs_fix", result
        assert result["next_action"] == "apply_deterministic_structural_repair", result
        assert exit_code == wrapper.EXIT_NEEDS_FIX
        sra = result.get("structural_repair_action")
        assert sra is not None, result
        assert sra["issue_kind"] == "parent"
        assert sra["disposition_summary"] == "auto_apply_safe"
        items = sra["items"]
        assert {i["label"] for i in items} == set(_PARENT_TARGET_SECTIONS)
        assert all(i["disposition"] == "auto_apply_safe" for i in items)
        assert all(i["insertion"]["disposition"] == "exact" for i in items)
        assert "missing_required_parent_section" not in result["blockers"], result
        assert not any(
            b.startswith("structural_repair_action_deferred:") for b in result["blockers"]
        ), result
        assert wrapper.BLOCKER_FAIL_CLOSED not in result["blockers"], result
        _validate_against_result_schema(result)


class TestParentDeadlockNegativeRegressions:
    """Issue #2598 negative regressions: the SAME parent-shaped
    `missing_required_parent_section` code must NOT trigger the override
    when any single AC3-precedent condition is violated. Uses a
    hand-constructed `structural_repair_action` (`issue_kind: parent`)
    mocked directly at `build_structural_repair_bundle()`, mirroring
    `TestAC3RegressionStillDeferred`'s adversarial-bundle style above --
    this file's owner-anchor/source-span provenance matrix itself is
    exercised elsewhere (PR #2583 / test_structural_repair_known_scalars_wiring.py)
    and is intentionally NOT duplicated here."""

    def _bundle(self, *, items: list, issue_kind: str = "parent") -> dict:
        return {
            "schema_version": "structural_repair_action/v1",
            "policy_version": "template-derived-structural-repair/v1",
            "issue_kind": issue_kind,
            "repo": "testowner/testrepo",
            "issue_number": 0,
            "original_body_sha256": "sha256:" + "0" * 64,
            "original_updated_at": "2026-01-01T00:00:00Z",
            "items": items,
            "disposition_summary": "auto_apply_safe",
            "template_git_blob_sha": None,
            "template_source_ref": None,
        }

    def _safe_item(
        self, label: str, field_id: str, *, disposition: str = "auto_apply_safe", insertion_disposition: str = "exact"
    ) -> dict:
        return {
            "field_id": field_id,
            "label": label,
            "required": True,
            "template_field_order": 1,
            "template_path": ".github/ISSUE_TEMPLATE/parent.yml",
            "template_digest": "sha256:" + "1" * 64,
            "expected_cardinality": 1,
            "observed_cardinality": 0,
            "disposition": disposition,
            "derivation": "source_span_exact",
            "reason_codes": [],
            "candidate_value": "- x",
            "candidate_digest": "sha256:" + "2" * 64,
            "repo": "testowner/testrepo",
            "issue_number": 0,
            "original_body_sha256": "sha256:" + "0" * 64,
            "original_updated_at": "2026-01-01T00:00:00Z",
            "insertion": {
                "disposition": insertion_disposition,
                "relation": "replace_section_content",
                "anchor_field_id": field_id,
                "anchor_heading": label,
                "anchor_start_line": 1,
                "anchor_digest": "sha256:" + "3" * 64,
                "rendered_heading": f"## {label}",
                "candidate_section_digest": "sha256:" + "4" * 64,
            },
        }

    def _run(
        self,
        tmp_path: Path,
        issue_number: int,
        bundle: dict,
        required_sections: list,
        reason_codes: "list | None" = None,
    ):
        body = _build_parent_body(omit_sections=frozenset(_PARENT_TARGET_SECTIONS))
        plan = _mock_plan_blocked(
            required_sections=required_sections,
            reason_codes=reason_codes or ["missing_required_parent_section"],
        )
        with mock.patch.object(wrapper, "build_structural_repair_bundle", return_value=bundle):
            return _run_preflight_with_mock_plan(
                tmp_path, issue_number, body, plan, seed_parent_template=True
            )

    def _assert_deferred(self, result: dict, exit_code: int) -> None:
        assert result["status"] == "blocked", result
        assert exit_code == wrapper.EXIT_BLOCKED
        assert any(b.startswith("structural_repair_action_deferred:") for b in result["blockers"]), result
        assert result.get("structural_repair_action") is None, result
        _validate_against_result_schema(result)

    def test_parent_ambiguous_insertion_still_deferred(self, tmp_path: Path) -> None:
        """A non-exact (`ambiguous`) insertion on an otherwise auto_apply_safe
        item must keep the deadlock deferred."""
        items = [
            self._safe_item("Quality Decision Record", "quality-decision-record", insertion_disposition="ambiguous"),
            self._safe_item("Child Issues", "child-issues"),
            self._safe_item("Remaining Parent Gaps", "remaining-parent-gaps"),
        ]
        result, exit_code = self._run(tmp_path, 259802, self._bundle(items=items), list(_PARENT_TARGET_SECTIONS))
        self._assert_deferred(result, exit_code)

    def test_parent_incomplete_coverage_fewer_still_deferred(self, tmp_path: Path) -> None:
        """The bundle covers FEWER targets than the planner's own
        required_sections -- coverage is incomplete."""
        items = [
            self._safe_item("Quality Decision Record", "quality-decision-record"),
            self._safe_item("Child Issues", "child-issues"),
        ]
        result, exit_code = self._run(tmp_path, 259804, self._bundle(items=items), list(_PARENT_TARGET_SECTIONS))
        self._assert_deferred(result, exit_code)

    def test_parent_incomplete_coverage_extra_still_deferred(self, tmp_path: Path) -> None:
        """The bundle covers MORE targets than the planner's own
        required_sections -- coverage is not EXACT."""
        items = [
            self._safe_item("Quality Decision Record", "quality-decision-record"),
            self._safe_item("Child Issues", "child-issues"),
            self._safe_item("Remaining Parent Gaps", "remaining-parent-gaps"),
            self._safe_item("Acceptance Criteria", "acceptance-criteria"),
        ]
        result, exit_code = self._run(tmp_path, 259805, self._bundle(items=items), list(_PARENT_TARGET_SECTIONS))
        self._assert_deferred(result, exit_code)

    def test_parent_unrelated_blocker_still_deferred(self, tmp_path: Path) -> None:
        """Full auto_apply_safe coverage, but an UNRELATED blocker
        namespace is also present -- the override must not fire."""
        items = [
            self._safe_item("Quality Decision Record", "quality-decision-record"),
            self._safe_item("Child Issues", "child-issues"),
            self._safe_item("Remaining Parent Gaps", "remaining-parent-gaps"),
        ]
        result, exit_code = self._run(
            tmp_path,
            259806,
            self._bundle(items=items),
            list(_PARENT_TARGET_SECTIONS),
            reason_codes=["missing_required_parent_section", "some_unrelated_blocker_namespace"],
        )
        assert "some_unrelated_blocker_namespace" in result["blockers"], result
        self._assert_deferred(result, exit_code)


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

    # -- Issue #2598: bare `missing_required_parent_section` code --------

    def _parent_bundle(self, *, issue_kind: str = "parent") -> dict:
        return {
            "issue_kind": issue_kind,
            "items": [
                self._safe_item(label, field_id)
                for label, field_id in (
                    ("Quality Decision Record", "quality-decision-record"),
                    ("Child Issues", "child-issues"),
                    ("Remaining Parent Gaps", "remaining-parent-gaps"),
                )
            ],
        }

    def test_parent_reason_code_eligible_when_bundle_issue_kind_parent(self) -> None:
        bundle = self._parent_bundle(issue_kind="parent")
        assert wrapper._structural_deadlock_override_eligible(
            bundle, ["missing_required_parent_section"], list(_PARENT_TARGET_SECTIONS), []
        ) is True

    def test_parent_reason_code_not_eligible_when_bundle_issue_kind_mismatched(self) -> None:
        """Defense-in-depth: the blocker STRING alone is never trusted to
        imply the bundle's own shape -- a bundle independently resolved as a
        different issue_kind must not be adopted even if `blockers` claims
        `missing_required_parent_section`."""
        bundle = self._parent_bundle(issue_kind="implementation")
        assert wrapper._structural_deadlock_override_eligible(
            bundle, ["missing_required_parent_section"], list(_PARENT_TARGET_SECTIONS), []
        ) is False

    def test_parent_non_auto_apply_safe_item_never_eligible(self) -> None:
        """An item that is not itself `auto_apply_safe` must keep the
        deadlock deferred (direct unit coverage, bypassing the separate
        disposition_summary/items consistency schema check that an
        end-to-end adversarial fixture would otherwise trip)."""
        bundle = {
            "issue_kind": "parent",
            "items": [
                {
                    "field_id": "quality-decision-record",
                    "label": "Quality Decision Record",
                    "disposition": "human_review_required",
                    "insertion": {"disposition": "exact"},
                },
                self._safe_item("Child Issues", "child-issues"),
                self._safe_item("Remaining Parent Gaps", "remaining-parent-gaps"),
            ],
        }
        assert wrapper._structural_deadlock_override_eligible(
            bundle, ["missing_required_parent_section"], list(_PARENT_TARGET_SECTIONS), []
        ) is False

    def test_suffixed_parent_reason_code_never_eligible(self) -> None:
        """Only the EXACT bare `missing_required_parent_section` code
        counts -- a suffixed form is an unrelated blocker namespace,
        matching the existing bare-code-only precedent for
        `PLANNER_FAIL_CLOSED`."""
        bundle = self._parent_bundle(issue_kind="parent")
        assert wrapper._structural_deadlock_override_eligible(
            bundle,
            ["missing_required_parent_section:some_detail"],
            list(_PARENT_TARGET_SECTIONS),
            [],
        ) is False

    def test_unknown_future_insertion_disposition_never_eligible(self) -> None:
        """Issue #2603 P2 fix: the `insertion.disposition` check must be a
        POSITIVE allowlist (`== "exact"`), not merely `!= "ambiguous"`.
        Direct-call coverage here bypasses schema validation (which today
        only accepts `exact`/`ambiguous`) so that a hypothetical future
        non-`"ambiguous"` disposition value is proven rejected by the
        predicate itself, independent of whatever the current schema's enum
        happens to allow."""
        bundle = self._bundle(
            items=[
                {
                    "field_id": "quality-decision-record",
                    "label": "Quality Decision Record",
                    "disposition": "auto_apply_safe",
                    "insertion": {"disposition": "partial"},
                },
                self._safe_item("Child Issues", "child-issues"),
                self._safe_item("Remaining Parent Gaps", "remaining-parent-gaps"),
            ]
        )
        assert wrapper._structural_deadlock_override_eligible(
            bundle, ["missing_required_section"], list(_PARENT_TARGET_SECTIONS), []
        ) is False

        bundle_alt = self._bundle(
            items=[
                {
                    "field_id": "quality-decision-record",
                    "label": "Quality Decision Record",
                    "disposition": "auto_apply_safe",
                    "insertion": {"disposition": "unknown_future_value"},
                },
                self._safe_item("Child Issues", "child-issues"),
                self._safe_item("Remaining Parent Gaps", "remaining-parent-gaps"),
            ]
        )
        assert wrapper._structural_deadlock_override_eligible(
            bundle_alt, ["missing_required_section"], list(_PARENT_TARGET_SECTIONS), []
        ) is False


class TestStructuralDeadlockEligibleBlockerUnit:
    """Direct unit coverage of the shared `_structural_deadlock_eligible_blocker()`
    predicate (Issue #2598) extracted for use by BOTH the eligibility check
    and the post-override blocker cleanup."""

    def test_generic_codes_always_eligible_regardless_of_bundle(self) -> None:
        for blocker in (
            "missing_required_section",
            "missing_required_section:Outcome",
            "structural_repair_action_deferred:some_reason",
            wrapper.BLOCKER_FAIL_CLOSED,
        ):
            assert wrapper._structural_deadlock_eligible_blocker(blocker, None) is True

    def test_bare_parent_code_eligible_only_when_bundle_issue_kind_parent(self) -> None:
        assert wrapper._structural_deadlock_eligible_blocker(
            "missing_required_parent_section", {"issue_kind": "parent"}
        ) is True
        assert wrapper._structural_deadlock_eligible_blocker(
            "missing_required_parent_section", {"issue_kind": "implementation"}
        ) is False
        assert wrapper._structural_deadlock_eligible_blocker(
            "missing_required_parent_section", None
        ) is False

    def test_suffixed_parent_code_never_eligible(self) -> None:
        assert wrapper._structural_deadlock_eligible_blocker(
            "missing_required_parent_section:detail", {"issue_kind": "parent"}
        ) is False

    def test_unrelated_blocker_never_eligible(self) -> None:
        assert wrapper._structural_deadlock_eligible_blocker(
            "some_unrelated_blocker_namespace", {"issue_kind": "parent"}
        ) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
