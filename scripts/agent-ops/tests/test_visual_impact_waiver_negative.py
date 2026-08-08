"""test_visual_impact_waiver_negative.py (Issue #2019 AC15)

GIVEN a `waived` disposition, WHEN the owner is not in the authorized
owner set, or the tracking issue is missing/closed, or the waiver is
expired, THEN the waiver evaluation fails.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "resolve_visual_impact.py"
_MODULE_NAME = "resolve_visual_impact_issue_2019_waiver_negative"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rvi = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = rvi
_spec.loader.exec_module(rvi)

AUTHORIZED = {"@squne121"}


def test_unauthorized_owner_fails():
    evaluation = rvi.evaluate_waiver(
        actor="not-authorized",
        waiver={"reason": "x", "tracking_issue": "#1", "expiry": "2099-01-01"},
        authorized_owners=AUTHORIZED,
        today=date(2026, 8, 8),
        tracking_issue_exists_and_open=True,
    )
    assert evaluation.ok is False
    assert evaluation.reason == "unauthorized_owner"


def test_missing_tracking_issue_fails():
    evaluation = rvi.evaluate_waiver(
        actor="squne121",
        waiver={"reason": "x", "tracking_issue": "", "expiry": "2099-01-01"},
        authorized_owners=AUTHORIZED,
        today=date(2026, 8, 8),
        tracking_issue_exists_and_open=None,
    )
    assert evaluation.ok is False
    assert evaluation.reason == "missing_tracking_issue"


def test_closed_tracking_issue_fails():
    evaluation = rvi.evaluate_waiver(
        actor="squne121",
        waiver={"reason": "x", "tracking_issue": "#42", "expiry": "2099-01-01"},
        authorized_owners=AUTHORIZED,
        today=date(2026, 8, 8),
        tracking_issue_exists_and_open=False,
    )
    assert evaluation.ok is False
    assert evaluation.reason == "tracking_issue_missing_or_closed"


def test_unverifiable_tracking_issue_state_fails_closed():
    """If the caller could not confirm the tracking issue is open (e.g. a
    transient GitHub API failure), the waiver fails closed rather than
    optimistically passing."""
    evaluation = rvi.evaluate_waiver(
        actor="squne121",
        waiver={"reason": "x", "tracking_issue": "#42", "expiry": "2099-01-01"},
        authorized_owners=AUTHORIZED,
        today=date(2026, 8, 8),
        tracking_issue_exists_and_open=None,
    )
    assert evaluation.ok is False
    assert evaluation.reason == "tracking_issue_state_unverified"


def test_expired_waiver_fails():
    evaluation = rvi.evaluate_waiver(
        actor="squne121",
        waiver={"reason": "x", "tracking_issue": "#42", "expiry": "2020-01-01"},
        authorized_owners=AUTHORIZED,
        today=date(2026, 8, 8),
        tracking_issue_exists_and_open=True,
    )
    assert evaluation.ok is False
    assert evaluation.reason == "expired"


def test_waiver_expiring_today_still_passes_boundary():
    evaluation = rvi.evaluate_waiver(
        actor="squne121",
        waiver={"reason": "x", "tracking_issue": "#42", "expiry": "2026-08-08"},
        authorized_owners=AUTHORIZED,
        today=date(2026, 8, 8),
        tracking_issue_exists_and_open=True,
    )
    assert evaluation.ok is True


def test_missing_reason_fails():
    evaluation = rvi.evaluate_waiver(
        actor="squne121",
        waiver={"reason": "", "tracking_issue": "#42", "expiry": "2099-01-01"},
        authorized_owners=AUTHORIZED,
        today=date(2026, 8, 8),
        tracking_issue_exists_and_open=True,
    )
    assert evaluation.ok is False
    assert evaluation.reason == "missing_reason"
