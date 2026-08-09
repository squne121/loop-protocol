"""test_visual_impact_waiver_authority.py (Issue #2019 AC14)

GIVEN a `waived` disposition, WHEN owner authority is evaluated, THEN it is
derived from a TRUSTED base-branch CODEOWNERS text (never PR-body
self-report, never a head-branch-introduced CODEOWNERS).
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "resolve_visual_impact.py"
_MODULE_NAME = "resolve_visual_impact_issue_2019_waiver_authority"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rvi = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = rvi
_spec.loader.exec_module(rvi)

TRUSTED_BASE_CODEOWNERS = """
# Issue #2019 CODEOWNERS fixture (represents TRUSTED base-branch content)
docs/dev/visual-surfaces.yml @squne121
tests/component/__screenshots__/ @squne121 @second-owner
"""


def test_authorized_owner_from_trusted_base_codeowners_passes():
    rules = rvi.parse_codeowners(TRUSTED_BASE_CODEOWNERS)
    owners = rvi.authorized_owners_for_paths(rules, ["docs/dev/visual-surfaces.yml"])
    assert owners == {"@squne121"}
    evaluation = rvi.evaluate_waiver(
        actor="squne121",
        waiver={"reason": "temporary layout freeze", "tracking_issue": "#9999", "expiry": "2099-01-01"},
        authorized_owners=owners,
        today=date(2026, 8, 8),
        tracking_issue_exists_and_open=True,
    )
    assert evaluation.ok is True


def test_last_matching_codeowners_rule_wins():
    text = """
docs/dev/ @first-owner
docs/dev/visual-surfaces.yml @second-owner
"""
    rules = rvi.parse_codeowners(text)
    owners = rvi.authorized_owners_for_paths(rules, ["docs/dev/visual-surfaces.yml"])
    assert owners == {"@second-owner"}


def test_pr_body_self_reported_owner_string_is_never_treated_as_authority():
    """AC14: a declaration cannot smuggle in its own "owner" claim -- the
    schema itself has no such field (see
    test_visual_impact_declaration_schema.py::test_unknown_waiver_field_is_rejected),
    and evaluate_waiver() only ever accepts owners already resolved from
    trusted CODEOWNERS via `authorized_owners`. This test proves that
    passing an actor NOT present in the trusted owner set fails even when
    the actor claims authority."""
    rules = rvi.parse_codeowners(TRUSTED_BASE_CODEOWNERS)
    owners = rvi.authorized_owners_for_paths(rules, ["docs/dev/visual-surfaces.yml"])
    evaluation = rvi.evaluate_waiver(
        actor="self-declared-owner-not-in-codeowners",
        waiver={"reason": "x", "tracking_issue": "#1", "expiry": "2099-01-01"},
        authorized_owners=owners,
        today=date(2026, 8, 8),
        tracking_issue_exists_and_open=True,
    )
    assert evaluation.ok is False
    assert evaluation.reason == "unauthorized_owner"


def test_head_branch_introduced_codeowners_is_never_the_trusted_source():
    """This Issue's own PR introduces .github/CODEOWNERS for the first
    time. AC14 requires the waiver authority to come from the TRUSTED BASE
    branch content (which -- before this PR merges -- has NO CODEOWNERS
    file at all), never from the new head-branch CODEOWNERS this same PR
    adds. Simulate: base branch has no CODEOWNERS (empty rules) -> nobody
    is authorized yet, even if the head branch's new CODEOWNERS would
    authorize the actor."""
    empty_base_rules = rvi.parse_codeowners("")  # base branch: no CODEOWNERS exists yet
    owners = rvi.authorized_owners_for_paths(empty_base_rules, ["docs/dev/visual-surfaces.yml"])
    assert owners == set()
    evaluation = rvi.evaluate_waiver(
        actor="squne121",
        waiver={"reason": "x", "tracking_issue": "#1", "expiry": "2099-01-01"},
        authorized_owners=owners,
        today=date(2026, 8, 8),
        tracking_issue_exists_and_open=True,
    )
    assert evaluation.ok is False
    assert evaluation.reason == "unauthorized_owner"
