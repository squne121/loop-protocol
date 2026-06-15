"""
Fixture-driven unit tests for route_loop_verdict_v2 production consumer.

Issue #777: Exercises the positive/negative fixture files in this directory
against the production consumer module.

Each fixture file defines:
  - loop_verdict: LOOP_VERDICT_V2 dict
  - test_verdict: optional TEST_VERDICT_MACHINE/v1 dict or null
  - expected.route: expected RouteDecision.route value
  - expected.fail_closed: expected RouteDecision.fail_closed value
  - expected.reason_code_prefix: optional prefix match for RouteDecision.reason_code
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Path setup: import production consumer from scripts/
# ---------------------------------------------------------------------------

# parents[3] = .claude/skills/impl-review-loop (from fixtures/step5_routing_consumer/test_*.py)
IMPL_REVIEW_LOOP_DIR = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = IMPL_REVIEW_LOOP_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from route_loop_verdict_v2 import RouteDecision, route_loop_verdict_v2  # noqa: E402

FIXTURE_DIR = Path(__file__).parent


def _load_fixture(name: str) -> dict:
    return yaml.safe_load((FIXTURE_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------


def test_positive_approved():
    """positive_approved.yml: APPROVE + CLEAN + empty actions + merge_ready=true → approved."""
    fx = _load_fixture("positive_approved.yml")
    result = route_loop_verdict_v2(
        fx["loop_verdict"],
        test_verdict=fx.get("test_verdict"),
    )
    assert result.route == fx["expected"]["route"], (
        f"Expected route '{fx['expected']['route']}', got '{result.route}'. errors: {result.errors}"
    )
    assert result.fail_closed is fx["expected"]["fail_closed"]


def test_positive_update_branch():
    """positive_update_branch.yml: full update_branch matrix → route_to_update_branch."""
    fx = _load_fixture("positive_update_branch.yml")
    result = route_loop_verdict_v2(
        fx["loop_verdict"],
        test_verdict=fx.get("test_verdict"),
    )
    assert result.route == fx["expected"]["route"], (
        f"Expected route '{fx['expected']['route']}', got '{result.route}'. errors: {result.errors}"
    )
    assert result.fail_closed is fx["expected"]["fail_closed"]
    assert result.selected_action is not None
    assert result.rerun_required == {"verification": True, "pr_review": True}


def test_positive_body_only_ensure_closing_keyword():
    """positive_body_only_ensure_closing_keyword.yml: ensure_closing_keyword → route_to_body_only_action."""
    fx = _load_fixture("positive_body_only_ensure_closing_keyword.yml")
    result = route_loop_verdict_v2(
        fx["loop_verdict"],
        test_verdict=fx.get("test_verdict"),
    )
    assert result.route == fx["expected"]["route"], (
        f"Expected route '{fx['expected']['route']}', got '{result.route}'. errors: {result.errors}"
    )
    assert result.fail_closed is fx["expected"]["fail_closed"]
    assert result.rerun_required == {"verification": False, "pr_review": True}


def test_positive_body_only_update_pr_body_hygiene():
    """positive_body_only_update_pr_body_hygiene.yml: update_pr_body_hygiene → route_to_body_only_action."""
    fx = _load_fixture("positive_body_only_update_pr_body_hygiene.yml")
    result = route_loop_verdict_v2(
        fx["loop_verdict"],
        test_verdict=fx.get("test_verdict"),
    )
    assert result.route == fx["expected"]["route"], (
        f"Expected route '{fx['expected']['route']}', got '{result.route}'. errors: {result.errors}"
    )
    assert result.fail_closed is fx["expected"]["fail_closed"]


def test_positive_continue_loop():
    """positive_continue_loop.yml: REQUEST_CHANGES → continue_loop."""
    fx = _load_fixture("positive_continue_loop.yml")
    result = route_loop_verdict_v2(
        fx["loop_verdict"],
        test_verdict=fx.get("test_verdict"),
    )
    assert result.route == fx["expected"]["route"], (
        f"Expected route '{fx['expected']['route']}', got '{result.route}'"
    )
    assert result.fail_closed is fx["expected"]["fail_closed"]


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------


def _assert_fail_closed(result: RouteDecision, expected: dict, fixture_name: str) -> None:
    """Common assertions for fail-closed cases."""
    assert result.route == "fail_closed", (
        f"[{fixture_name}] Expected route 'fail_closed', got '{result.route}'. "
        f"reason_code: {result.reason_code!r}. errors: {result.errors}"
    )
    assert result.fail_closed is True, (
        f"[{fixture_name}] Expected fail_closed=True, got False"
    )
    if "reason_code_prefix" in expected:
        prefix = expected["reason_code_prefix"]
        assert result.reason_code is not None and result.reason_code.startswith(prefix), (
            f"[{fixture_name}] Expected reason_code to start with '{prefix}', "
            f"got '{result.reason_code}'"
        )
    if "reason_code" in expected and expected["reason_code"] is not None:
        assert result.reason_code == expected["reason_code"], (
            f"[{fixture_name}] Expected reason_code '{expected['reason_code']}', "
            f"got '{result.reason_code}'"
        )


def test_negative_wrong_executor():
    """negative_wrong_executor.yml: wrong executor → fail-closed."""
    fx = _load_fixture("negative_wrong_executor.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_wrong_executor")


def test_negative_wrong_skill():
    """negative_wrong_skill.yml: wrong skill value → fail-closed."""
    fx = _load_fixture("negative_wrong_skill.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_wrong_skill")


def test_negative_skill_no_subcommand():
    """negative_skill_no_subcommand.yml: skill=implement-issue (no subcommand) → fail-closed (AC4)."""
    fx = _load_fixture("negative_skill_no_subcommand.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_skill_no_subcommand")


def test_negative_missing_skill():
    """negative_missing_skill.yml: missing skill field → fail-closed."""
    fx = _load_fixture("negative_missing_skill.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_missing_skill")


def test_negative_unknown_kind():
    """negative_unknown_kind.yml: unknown kind → fail-closed."""
    fx = _load_fixture("negative_unknown_kind.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_unknown_kind")


def test_negative_apply_pr_review_fix_delta():
    """negative_apply_pr_review_fix_delta.yml: rejected kind → fail-closed."""
    fx = _load_fixture("negative_apply_pr_review_fix_delta.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_apply_pr_review_fix_delta")


def test_negative_body_only_action_behind():
    """negative_body_only_action_behind.yml: body-only action while BEHIND → fail-closed."""
    fx = _load_fixture("negative_body_only_action_behind.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_body_only_action_behind")


def test_negative_multiple_actions():
    """negative_multiple_actions.yml: multiple required_auto_actions → fail-closed."""
    fx = _load_fixture("negative_multiple_actions.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_multiple_actions")


def test_negative_branch_behind_true_clean():
    """negative_branch_behind_true_clean.yml: branch_behind_main=true + CLEAN → AC6 invariant fail-closed."""
    fx = _load_fixture("negative_branch_behind_true_clean.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_branch_behind_true_clean")


def test_negative_branch_behind_false_behind():
    """negative_branch_behind_false_behind.yml: branch_behind_main=false + BEHIND → AC6 invariant fail-closed."""
    fx = _load_fixture("negative_branch_behind_false_behind.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_branch_behind_false_behind")


def test_negative_string_list_actions():
    """negative_string_list_actions.yml: required_auto_actions as string-list → schema_invalid (AC7)."""
    fx = _load_fixture("negative_string_list_actions.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_string_list_actions")


# ---------------------------------------------------------------------------
# Blocker 1: expected_head_sha mismatch must fail-closed
# ---------------------------------------------------------------------------


def test_negative_expected_head_sha_mismatch():
    """negative_expected_head_sha_mismatch.yml: expected_head_sha != reviewed_head_sha → fail-closed."""
    fx = _load_fixture("negative_expected_head_sha_mismatch.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_expected_head_sha_mismatch")


# ---------------------------------------------------------------------------
# Blocker 2: mechanical is required (must be True)
# ---------------------------------------------------------------------------


def test_negative_missing_mechanical():
    """negative_missing_mechanical.yml: mechanical field absent → fail-closed."""
    fx = _load_fixture("negative_missing_mechanical.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_missing_mechanical")


def test_negative_mechanical_false():
    """negative_mechanical_false.yml: mechanical=false → fail-closed."""
    fx = _load_fixture("negative_mechanical_false.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_mechanical_false")


# ---------------------------------------------------------------------------
# Blocker 4: merge_state_status=BEHIND requires test_verdict with branch_behind_main
# ---------------------------------------------------------------------------


def test_negative_behind_missing_branch_behind_main():
    """negative_behind_missing_branch_behind_main.yml: BEHIND + test_verdict=None → fail-closed."""
    fx = _load_fixture("negative_behind_missing_branch_behind_main.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_behind_missing_branch_behind_main")


def test_negative_behind_branch_behind_main_key_absent():
    """negative_behind_branch_behind_main_key_absent.yml: BEHIND + branch_behind_main key missing → fail-closed."""
    fx = _load_fixture("negative_behind_branch_behind_main_key_absent.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    _assert_fail_closed(result, fx["expected"], "negative_behind_branch_behind_main_key_absent")


# ---------------------------------------------------------------------------
# Extra 5: REQUEST_CHANGES + malformed required_auto_actions design decision
# ---------------------------------------------------------------------------


def test_negative_request_changes_with_malformed_update_branch_action():
    """Extra 5: REQUEST_CHANGES verdict skips action validation → continue_loop.

    Design decision: REQUEST_CHANGES routes to continue_loop without inspecting
    required_auto_actions. Action field validation only runs under APPROVE.
    """
    fx = _load_fixture("negative_request_changes_with_malformed_update_branch_action.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    assert result.route == fx["expected"]["route"], (
        f"Expected route '{fx['expected']['route']}', got '{result.route}'. "
        f"reason_code: {result.reason_code}"
    )
    assert result.fail_closed is fx["expected"]["fail_closed"]


# ---------------------------------------------------------------------------
# Extra 6: RouteDecision.selected_action and rerun_required are immutable
# ---------------------------------------------------------------------------


def test_route_decision_selected_action_is_immutable():
    """Extra 6: selected_action must be MappingProxyType (immutable)."""
    from types import MappingProxyType
    fx = _load_fixture("positive_update_branch.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    assert result.selected_action is not None
    assert isinstance(result.selected_action, MappingProxyType), (
        f"Expected MappingProxyType, got {type(result.selected_action)}"
    )
    with pytest.raises(TypeError):
        result.selected_action["kind"] = "hacked"  # type: ignore[index]


def test_route_decision_rerun_required_is_immutable():
    """Extra 6: rerun_required must be MappingProxyType (immutable)."""
    from types import MappingProxyType
    fx = _load_fixture("positive_update_branch.yml")
    result = route_loop_verdict_v2(fx["loop_verdict"], test_verdict=fx.get("test_verdict"))
    assert isinstance(result.rerun_required, MappingProxyType), (
        f"Expected MappingProxyType, got {type(result.rerun_required)}"
    )
    with pytest.raises(TypeError):
        result.rerun_required["verification"] = False  # type: ignore[index]


# ---------------------------------------------------------------------------
# Pure unit tests for AC1 (no subprocess / import side effects)
# ---------------------------------------------------------------------------


def test_module_no_forbidden_imports():
    """Extra 7 (AST-based): route_loop_verdict_v2.py must not import forbidden modules."""
    src = SCRIPTS_DIR / "route_loop_verdict_v2.py"
    tree = ast.parse(src.read_text())
    forbidden = {"subprocess", "socket", "urllib", "requests", "httpx", "os"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                names = [node.module or ""]
            for name in names:
                root = name.split(".")[0]
                assert root not in forbidden, (
                    f"Forbidden import in route_loop_verdict_v2.py: {name!r}"
                )


def test_route_decision_is_frozen():
    """AC2: RouteDecision must be frozen (immutable)."""
    rd = RouteDecision(
        route="approved",
        fail_closed=False,
        reason_code=None,
        selected_action=None,
        rerun_required={"verification": False, "pr_review": False},
        errors=(),
    )
    with pytest.raises((AttributeError, TypeError)):
        rd.route = "continue_loop"  # type: ignore[misc]


def test_route_decision_fields():
    """AC2: RouteDecision must have all required fields."""
    rd = RouteDecision(
        route="approved",
        fail_closed=False,
        reason_code=None,
        selected_action=None,
        rerun_required={"verification": False, "pr_review": False},
        errors=(),
    )
    assert hasattr(rd, "route")
    assert hasattr(rd, "fail_closed")
    assert hasattr(rd, "reason_code")
    assert hasattr(rd, "rerun_required")
    assert hasattr(rd, "selected_action")
    assert hasattr(rd, "errors")
