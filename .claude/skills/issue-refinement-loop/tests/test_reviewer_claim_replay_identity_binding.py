"""Tests for reviewer_claim_replay.py AC5 (Issue #1515): same_lane identity
binding.

When the caller supplies repository_full_name / issue_number /
refinement_session_id, `analyze()`'s same_lane check requires all three to
match the previous state IN ADDITION TO the pre-existing
reviewer_blocker_code / normalized_kind / body_sha256 triple. Callers that
omit all three identity kwargs (legacy --state-file path) are unaffected.
"""

from __future__ import annotations

import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from reviewer_claim_replay import STATE_SCHEMA_V2, analyze  # noqa: E402

COMPACT_C4 = {
    "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
    "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
    "body_sha256": "sha256:body-a",
    "blocking_issues": [{"code": "C4", "message": "missing $ prefix"}],
    "structured_blockers": [],
}

READINESS_CLEAN = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [],
}

IDENTITY = {
    "repository_full_name": "squne121/loop-protocol",
    "issue_number": 1021,
    "refinement_session_id": "session-aaaa",
}


def _previous_state(**overrides: object) -> dict[str, object]:
    state = {
        "schema": STATE_SCHEMA_V2,
        "repository_full_name": IDENTITY["repository_full_name"],
        "issue_number": IDENTITY["issue_number"],
        "refinement_session_id": IDENTITY["refinement_session_id"],
        "body_sha256": "sha256:body-a",
        "reviewer_blocker_code": "C4",
        "normalized_kind": "vc_command_format",
        "consecutive_unbacked_count": 1,
        "last_review_artifact": "/tmp/prior.json",
        "updated_at_iteration_id": "2026-07-14T00:00:00Z",
    }
    state.update(overrides)
    return state


def test_matching_identity_continues_consecutive_count():
    result, next_state = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state=_previous_state(),
        **IDENTITY,
    )
    assert result["verdict"] == "reviewer_false_positive_suspected"
    assert result["routing"] == "human_escalation"
    assert next_state["consecutive_unbacked_count"] == 2
    assert next_state["schema"] == STATE_SCHEMA_V2
    assert next_state["repository_full_name"] == IDENTITY["repository_full_name"]
    assert next_state["issue_number"] == IDENTITY["issue_number"]
    assert next_state["refinement_session_id"] == IDENTITY["refinement_session_id"]


def test_different_issue_number_resets_despite_matching_legacy_fields():
    result, next_state = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state=_previous_state(issue_number=9999),
        **IDENTITY,
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert next_state["consecutive_unbacked_count"] == 1


def test_different_refinement_session_id_resets_despite_matching_body_and_issue():
    result, next_state = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state=_previous_state(refinement_session_id="a-different-session"),
        **IDENTITY,
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert next_state["consecutive_unbacked_count"] == 1


def test_different_repository_full_name_resets():
    result, next_state = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state=_previous_state(repository_full_name="other-org/other-repo"),
        **IDENTITY,
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert next_state["consecutive_unbacked_count"] == 1


def test_no_identity_kwargs_falls_back_to_legacy_three_field_same_lane():
    """When no identity kwargs are supplied at all (legacy --state-file
    caller), same_lane must behave exactly as before Issue #1515 -- identity
    fields on previous_state (if any) are ignored."""
    previous = {
        "schema": "REVIEWER_CLAIM_REPLAY_STATE_V1",
        "issue_url": COMPACT_C4["issue_url"],
        "body_sha256": "sha256:body-a",
        "reviewer_blocker_code": "C4",
        "normalized_kind": "vc_command_format",
        "consecutive_unbacked_count": 1,
        "last_review_artifact": "/tmp/prior.json",
    }
    result, next_state = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state=previous,
    )
    assert result["verdict"] == "reviewer_false_positive_suspected"
    assert next_state["consecutive_unbacked_count"] == 2
    # Legacy shape preserved (no identity fields leaked in).
    assert "repository_full_name" not in next_state
