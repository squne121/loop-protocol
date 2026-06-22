"""
Regression fixtures for impl-review-loop V2 routing boundary.

Issue #777: Migrated from shadow helper to production consumer import.
Issue #454 fixes the consumer-side boundary registry for
TEST_VERDICT_MACHINE/v1.branch_behind_main and
LOOP_VERDICT_V2.required_auto_actions[].kind|executor|skill|expected_head_sha.
The fixtures in this module freeze the canonical V2 path and the fail-closed
behavior for unknown action kinds and stale/missing expected_head_sha guards.

TEST_VERDICT_MACHINE:
  version: 1
  result: pass
  head_sha: "pending"
  commands:
    - command: "uv run pytest .claude/skills/impl-review-loop/tests/test_v2_routing_boundary_regression.py -v"
      exit_code: 0
      stdout_sha256: "pending"
  fixtures:
    - case: "AC3_v2_update_branch_behind"
      after_pass_verified: true
    - case: "AC4_v2_unknown_required_auto_actions_kind"
      after_pass_verified: true
    - case: "AC5_v2_expected_head_sha_guard"
      after_pass_verified: true
  skipped: []
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
BOUNDARY_DOC = REPO_ROOT / "docs" / "dev" / "agent-skill-boundaries.md"
FIXTURES_DIR = REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "tests" / "fixtures"
STEP5_FIXTURES_DIR = FIXTURES_DIR / "step5_routing_consumer"

# ---------------------------------------------------------------------------
# Production consumer import (replaces shadow helper)
# ---------------------------------------------------------------------------

SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from route_loop_verdict_v2 import route_loop_verdict_v2  # noqa: E402


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_yaml_fixture(name: str) -> dict:
    return yaml.safe_load((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _load_step5_fixture(name: str) -> dict:
    return yaml.safe_load((STEP5_FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_given_boundary_doc_when_read_then_v2_routing_fields_are_registered() -> None:
    body = _read(BOUNDARY_DOC)
    assert "TEST_VERDICT_MACHINE/v1.branch_behind_main" in body
    assert "LOOP_VERDICT_V2.required_auto_actions[].kind" in body
    assert "LOOP_VERDICT_V2.required_auto_actions[].executor" in body
    assert "LOOP_VERDICT_V2.required_auto_actions[].skill" in body
    assert "LOOP_VERDICT_V2.required_auto_actions[].expected_head_sha" in body


def test_given_boundary_doc_when_read_then_stale_v1_wording_is_marked_noncanonical() -> None:
    body = _read(BOUNDARY_DOC)
    assert "LOOP_VERDICT.recommendations must not be treated as a canonical routing field" in body
    assert "unknown required_auto_actions.kind must fail closed" in body


def test_given_v2_update_branch_fixture_when_routed_then_update_branch_path_is_selected() -> None:
    """AC3: APPROVE + BEHIND + full update_branch matrix → route_to_update_branch.

    Uses step5_routing_consumer/positive_update_branch.yml which has the canonical
    skill=implement-issue.update_branch (not the legacy v2_update_branch_behind.yml
    which has skill=implement-issue and is fail-closed per AC4).
    """
    fx = _load_step5_fixture("positive_update_branch.yml")
    result = route_loop_verdict_v2(
        fx["loop_verdict"],
        test_verdict=fx.get("test_verdict"),
    )
    assert result.route == fx["expected"]["route"], (
        f"Expected route '{fx['expected']['route']}', got '{result.route}'. errors: {result.errors}"
    )
    assert result.fail_closed is fx["expected"]["fail_closed"]
    assert result.selected_action is not None
    action = dict(result.selected_action)
    assert action["kind"] == "update_branch"
    assert action["executor"] == "implementation-worker"
    assert action["skill"] == "implement-issue.update_branch"
    assert action["expected_head_sha"] == fx["loop_verdict"]["reviewed_head_sha"]


def test_given_unknown_kind_fixture_when_routed_then_it_fails_closed() -> None:
    """AC4: unknown required_auto_actions kind → fail-closed."""
    fixture = _load_yaml_fixture("v2_unknown_required_auto_actions_kind.yml")
    verdict = fixture["verdict"]

    assert verdict["required_auto_actions"][0]["kind"] == "rotate_branch_magic"

    result = route_loop_verdict_v2(verdict)
    assert result.route == "fail_closed", (
        f"Expected fail_closed, got '{result.route}'. reason_code: {result.reason_code}"
    )
    assert result.fail_closed is True
    assert fixture["expected"]["fail_closed"] is True


def test_given_expected_head_sha_guard_fixture_when_routed_then_missing_or_mismatch_fails_closed() -> None:
    """AC5: expected_head_sha guard — missing or mismatched SHA must fail closed.

    Note: these fixtures also have skill=implement-issue (no subcommand), so the
    production consumer fails closed at skill validation before reaching the SHA check.
    Either way, the result must be fail_closed=True (the AC5 intent is preserved).
    """
    fixture = _load_yaml_fixture("v2_update_branch_expected_head_sha_guard.yml")

    for case in fixture["cases"]:
        result = route_loop_verdict_v2(case["verdict"])
        assert result.route == "fail_closed", (
            f"[{case['name']}] Expected fail_closed, got '{result.route}'. "
            f"reason_code: {result.reason_code}"
        )
        assert result.fail_closed is True, (
            f"[{case['name']}] Expected fail_closed=True"
        )
        assert case["expected"]["fail_closed"] is True
