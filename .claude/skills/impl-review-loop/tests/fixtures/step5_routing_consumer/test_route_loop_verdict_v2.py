"""
Fixture-driven unit tests for route_loop_verdict_v2 production consumer.

Issue #1873: route_loop_verdict_v2 no longer accepts reviewer self-reported
merge_ready / required_auto_actions / allowed_paths_gate / mergeability.
Instead it takes reviewer_verdict (verdict, reviewed_head_sha, blockers,
warnings) and live_mergeability (head_sha, mergeable, merge_state_status)
as separate inputs. Each fixture file defines:
  - reviewer_verdict: minimal result convention dict
  - live_mergeability: live GitHub PR state dict
  - test_verdict: optional TEST_VERDICT_MACHINE/v1 dict or null
  - expected.route: expected RouteDecision.route value
  - expected.fail_closed: expected RouteDecision.fail_closed value
  - expected.reason_code: optional exact match for RouteDecision.reason_code
  - expected.reason_code_prefix: optional prefix match
  - expected.selected_action: optional exact dict match for RouteDecision.selected_action
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

IMPL_REVIEW_LOOP_DIR = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = IMPL_REVIEW_LOOP_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from route_loop_verdict_v2 import route_loop_verdict_v2  # noqa: E402

FIXTURE_DIR = Path(__file__).parent


def _fixture_files() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.yml"))


def _load_fixture(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture_path", _fixture_files(), ids=lambda p: p.stem)
def test_fixture(fixture_path: Path):
    fx = _load_fixture(fixture_path)
    result = route_loop_verdict_v2(
        fx["reviewer_verdict"],
        fx["live_mergeability"],
        test_verdict=fx.get("test_verdict"),
    )

    expected = fx["expected"]

    assert result.route == expected["route"], (
        f"{fixture_path.name}: expected route '{expected['route']}', "
        f"got '{result.route}'. errors: {result.errors}"
    )
    assert result.fail_closed is expected["fail_closed"], (
        f"{fixture_path.name}: expected fail_closed={expected['fail_closed']}, "
        f"got {result.fail_closed}"
    )

    if "reason_code" in expected:
        assert result.reason_code == expected["reason_code"], (
            f"{fixture_path.name}: expected reason_code={expected['reason_code']!r}, "
            f"got {result.reason_code!r}"
        )

    if "reason_code_prefix" in expected:
        prefix = expected["reason_code_prefix"]
        assert result.reason_code is not None and result.reason_code.startswith(prefix), (
            f"{fixture_path.name}: expected reason_code to start with {prefix!r}, "
            f"got {result.reason_code!r}"
        )

    if "selected_action" in expected:
        assert result.selected_action is not None
        assert dict(result.selected_action) == expected["selected_action"], (
            f"{fixture_path.name}: selected_action mismatch: "
            f"{dict(result.selected_action)} != {expected['selected_action']}"
        )


def test_at_least_one_positive_and_one_negative_fixture_present():
    names = [p.stem for p in _fixture_files()]
    assert any(n.startswith("positive_") for n in names)
    assert any(n.startswith("negative_") for n in names)


def test_no_legacy_v1_fields_accepted_at_all():
    """A reviewer_verdict carrying any V1/V2-legacy field must fail closed."""
    for legacy_key in ("merge_ready", "required_auto_actions", "allowed_paths_gate",
                       "mergeability", "mergeStateStatus", "recommendations"):
        reviewer_verdict = {
            "verdict": "APPROVE",
            "reviewed_head_sha": "abc123",
            "blockers": [],
            legacy_key: "anything",
        }
        live_mergeability = {
            "head_sha": "abc123",
            "mergeable": "MERGEABLE",
            "merge_state_status": "CLEAN",
        }
        result = route_loop_verdict_v2(reviewer_verdict, live_mergeability)
        assert result.fail_closed is True, f"legacy field {legacy_key} was not rejected"
        assert result.reason_code == f"schema_invalid_legacy_field_present:{legacy_key}"
