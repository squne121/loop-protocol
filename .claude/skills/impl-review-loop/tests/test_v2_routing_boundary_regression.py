"""
Regression fixtures for impl-review-loop V2 routing boundary.

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

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
BOUNDARY_DOC = REPO_ROOT / "docs" / "dev" / "agent-skill-boundaries.md"
FIXTURES_DIR = REPO_ROOT / ".claude" / "skills" / "impl-review-loop" / "tests" / "fixtures"

ALLOWED_ACTION_KINDS = {
    "ensure_closing_keyword",
    "update_pr_body_hygiene",
    "update_branch",
    "apply_pr_review_fix_delta",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_yaml_fixture(name: str) -> dict:
    return yaml.safe_load((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _route_required_auto_action(verdict: dict) -> str:
    merge_state_status = verdict.get("mergeability", {}).get("merge_state_status")
    required_auto_actions = verdict.get("required_auto_actions", [])
    reviewed_head_sha = verdict.get("reviewed_head_sha")

    if merge_state_status == "BEHIND" and not required_auto_actions:
        return "fail_closed_missing_required_auto_actions"

    for action in required_auto_actions:
        kind = action.get("kind")
        if kind not in ALLOWED_ACTION_KINDS:
            return "fail_closed_unknown_required_auto_actions_kind"

        if kind == "update_branch":
            expected_head_sha = action.get("expected_head_sha")
            if not expected_head_sha or expected_head_sha != reviewed_head_sha:
                return "fail_closed_expected_head_sha_guard"
            return "route_to_update_branch"

    return "route_to_required_auto_action"


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
    fixture = _load_yaml_fixture("v2_update_branch_behind.yml")
    verdict = fixture["verdict"]
    action = verdict["required_auto_actions"][0]

    assert action["kind"] == "update_branch"
    assert action["executor"] == "implementation-worker"
    assert action["skill"] == "implement-issue"
    assert action["expected_head_sha"] == verdict["reviewed_head_sha"]
    assert _route_required_auto_action(verdict) == fixture["expected"]["route"]


def test_given_unknown_kind_fixture_when_routed_then_it_fails_closed() -> None:
    fixture = _load_yaml_fixture("v2_unknown_required_auto_actions_kind.yml")
    verdict = fixture["verdict"]

    assert verdict["required_auto_actions"][0]["kind"] == "rotate_branch_magic"
    assert _route_required_auto_action(verdict) == fixture["expected"]["route"]
    assert fixture["expected"]["fail_closed"] is True


def test_given_expected_head_sha_guard_fixture_when_routed_then_missing_or_mismatch_fails_closed() -> None:
    fixture = _load_yaml_fixture("v2_update_branch_expected_head_sha_guard.yml")

    for case in fixture["cases"]:
        route = _route_required_auto_action(case["verdict"])
        assert route == case["expected"]["route"], case["name"]
        assert case["expected"]["fail_closed"] is True
