"""
test_rewrite_router.py

Tests for decide_rewrite_route function and LOOP_REWRITE_ROUTER_STATE_V1 schema.

Covers AC2 (max_rewrite_attempts boundary), AC3 (checker_exit_code gating),
AC4 (no-progress detection), and AC6 (regression test via direct function calls).
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from decide_rewrite_route import (
    decide_rewrite_route,
    LOOP_REWRITE_ROUTER_STATE_V1,
    ROUTE_HUMAN_JUDGMENT_REQUIRED,
    ROUTE_PROCEED_TO_REVIEW,
    ROUTE_CONTINUE_REWRITE,
    REASON_CODE_MAX_ATTEMPTS_EXCEEDED,
    REASON_CODE_BODY_HASH_UNCHANGED,
    REASON_CODE_MISSING_CONTRACT_NO_PROGRESS,
    REASON_CODE_CHECKER_PASSED,
    REASON_CODE_CHECKER_FAILED,
)


# ---------------------------------------------------------------------------
# AC2: max_rewrite_attempts boundary tests
# ---------------------------------------------------------------------------


class TestAC2MaxAttemptsBoundary:
    """AC2: max=2 boundary enforcement tests."""

    def _base_state(self, attempt: int, max_attempts: int = 2) -> LOOP_REWRITE_ROUTER_STATE_V1:
        return LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=attempt,
            max_rewrite_attempts=max_attempts,
            checker_exit_code=0,
            checked_body_sha256="abc123",
            missing_sections=[],
            missing_contract_keys=[],
        )

    def test_max2_attempt0_not_exceeded(self):
        """max=2, attempt=0 -> should NOT trigger human_judgment_required."""
        state = self._base_state(attempt=0, max_attempts=2)
        result = decide_rewrite_route(state)
        assert result.route != ROUTE_HUMAN_JUDGMENT_REQUIRED or result.reason_code != REASON_CODE_MAX_ATTEMPTS_EXCEEDED
        # Should be proceed_to_review (checker passes, no no-progress)
        assert result.route in (ROUTE_CONTINUE_REWRITE, ROUTE_PROCEED_TO_REVIEW)

    def test_max2_attempt1_not_exceeded(self):
        """max=2, attempt=1 -> should NOT trigger human_judgment_required due to max."""
        state = self._base_state(attempt=1, max_attempts=2)
        result = decide_rewrite_route(state)
        assert result.route != ROUTE_HUMAN_JUDGMENT_REQUIRED or result.reason_code != REASON_CODE_MAX_ATTEMPTS_EXCEEDED
        assert result.route in (ROUTE_CONTINUE_REWRITE, ROUTE_PROCEED_TO_REVIEW)

    def test_max2_attempt2_exceeded(self):
        """max=2, attempt=2 -> human_judgment_required with reason_code: max_attempts_exceeded."""
        state = self._base_state(attempt=2, max_attempts=2)
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_HUMAN_JUDGMENT_REQUIRED
        assert result.reason_code == REASON_CODE_MAX_ATTEMPTS_EXCEEDED

    def test_max2_attempt3_exceeded(self):
        """max=2, attempt=3 -> human_judgment_required with reason_code: max_attempts_exceeded."""
        state = self._base_state(attempt=3, max_attempts=2)
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_HUMAN_JUDGMENT_REQUIRED
        assert result.reason_code == REASON_CODE_MAX_ATTEMPTS_EXCEEDED

    def test_result_echoes_attempt_count(self):
        """RouteResult echoes rewrite_attempt_count from state."""
        state = self._base_state(attempt=2, max_attempts=2)
        result = decide_rewrite_route(state)
        assert result.rewrite_attempt_count == 2
        assert result.max_rewrite_attempts == 2


# ---------------------------------------------------------------------------
# AC3: checker_exit_code gating tests
# ---------------------------------------------------------------------------


class TestAC3CheckerExitCode:
    """AC3: checker_exit_code gating — only route to review when exit_code == 0."""

    def test_checker_exit0_max_not_exceeded_no_prev(self):
        """checker_exit_code=0, max not exceeded, no previous state -> proceed_to_review."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=0,
            max_rewrite_attempts=5,
            checker_exit_code=0,
            checked_body_sha256="abc123",
            missing_sections=[],
            missing_contract_keys=[],
        )
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_PROCEED_TO_REVIEW
        assert result.reason_code == REASON_CODE_CHECKER_PASSED

    def test_checker_exit1_max_not_exceeded(self):
        """checker_exit_code=1, max not exceeded -> continue_rewrite."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=0,
            max_rewrite_attempts=5,
            checker_exit_code=1,
            checked_body_sha256="abc123",
            missing_sections=["## AC"],
            missing_contract_keys=[],
        )
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_CONTINUE_REWRITE
        assert result.reason_code == REASON_CODE_CHECKER_FAILED

    def test_checker_exit_nonzero_does_not_proceed_to_review(self):
        """Any non-zero checker exit should never produce proceed_to_review."""
        for exit_code in [1, 2, 127, 255]:
            state = LOOP_REWRITE_ROUTER_STATE_V1(
                rewrite_attempt_count=0,
                max_rewrite_attempts=10,
                checker_exit_code=exit_code,
                checked_body_sha256="def456",
                missing_sections=[],
                missing_contract_keys=[],
            )
            result = decide_rewrite_route(state)
            assert result.route != ROUTE_PROCEED_TO_REVIEW, (
                f"exit_code={exit_code} should not produce proceed_to_review"
            )


# ---------------------------------------------------------------------------
# AC4: no-progress detection tests
# ---------------------------------------------------------------------------


class TestAC4NoProgressDetection:
    """AC4: no-progress detection — body hash unchanged or missing set not decreased."""

    def test_body_hash_unchanged_triggers_human_judgment(self):
        """body hash same as previous -> human_judgment_required(body_hash_unchanged)."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=5,
            checker_exit_code=1,
            checked_body_sha256="same_hash",
            missing_sections=["## AC"],
            missing_contract_keys=[],
            previous_checked_body_sha256="same_hash",
            previous_missing_sections=["## AC"],
            previous_missing_contract_keys=[],
        )
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_HUMAN_JUDGMENT_REQUIRED
        assert result.reason_code == REASON_CODE_BODY_HASH_UNCHANGED

    def test_body_hash_changed_missing_sections_decreased(self):
        """body hash changed, missing_sections decreased -> not no-progress."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=5,
            checker_exit_code=0,
            checked_body_sha256="new_hash",
            missing_sections=[],
            missing_contract_keys=[],
            previous_checked_body_sha256="old_hash",
            previous_missing_sections=["## AC"],
            previous_missing_contract_keys=[],
        )
        result = decide_rewrite_route(state)
        # Progress was made — should not be human_judgment for no-progress reasons
        assert result.route in (ROUTE_CONTINUE_REWRITE, ROUTE_PROCEED_TO_REVIEW)
        assert result.reason_code not in (
            REASON_CODE_BODY_HASH_UNCHANGED,
            REASON_CODE_MISSING_CONTRACT_NO_PROGRESS,
        )

    def test_body_hash_changed_both_missing_not_decreased(self):
        """body hash changed, both missing_sections and missing_contract_keys non-empty and not decreased.
        -> human_judgment_required(missing_contract_no_progress)."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=5,
            checker_exit_code=1,
            checked_body_sha256="new_hash",
            missing_sections=["## AC", "## Non-Goals"],
            missing_contract_keys=["allowed_paths", "acceptance_criteria"],
            previous_checked_body_sha256="old_hash",
            previous_missing_sections=["## AC", "## Non-Goals"],
            previous_missing_contract_keys=["allowed_paths", "acceptance_criteria"],
        )
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_HUMAN_JUDGMENT_REQUIRED
        assert result.reason_code == REASON_CODE_MISSING_CONTRACT_NO_PROGRESS

    def test_no_previous_state_no_progress_check(self):
        """Without previous state, no-progress detection is skipped."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=0,
            max_rewrite_attempts=5,
            checker_exit_code=1,
            checked_body_sha256="abc123",
            missing_sections=["## AC"],
            missing_contract_keys=["allowed_paths"],
        )
        result = decide_rewrite_route(state)
        # Only checker failed should trigger continue_rewrite
        assert result.route == ROUTE_CONTINUE_REWRITE
        assert result.reason_code == REASON_CODE_CHECKER_FAILED


# ---------------------------------------------------------------------------
# AC6: regression tests — direct function calls on LOOP_REWRITE_ROUTER_STATE_V1
# ---------------------------------------------------------------------------


class TestAC6RegressionDirectCalls:
    """AC6: regression tests using direct decide_rewrite_route calls.

    These verify that the implementation handles state contract correctly —
    not just string containment.
    """

    def test_state_contract_fields_echo_in_result(self):
        """RouteResult echoes all state contract fields correctly."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=3,
            checker_exit_code=0,
            checked_body_sha256="sha_abc",
            missing_sections=[],
            missing_contract_keys=[],
        )
        result = decide_rewrite_route(state)
        assert result.rewrite_attempt_count == 1
        assert result.max_rewrite_attempts == 3
        assert result.checker_exit_code == 0
        assert result.checked_body_sha256 == "sha_abc"

    def test_max_boundary_off_by_one_strict(self):
        """Strict off-by-one: attempt == max triggers stop, attempt == max-1 does not."""
        # attempt 4, max 5 -> allowed
        state_allowed = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=4,
            max_rewrite_attempts=5,
            checker_exit_code=0,
            checked_body_sha256="h1",
            missing_sections=[],
            missing_contract_keys=[],
        )
        result_allowed = decide_rewrite_route(state_allowed)
        assert result_allowed.reason_code != REASON_CODE_MAX_ATTEMPTS_EXCEEDED

        # attempt 5, max 5 -> exceeded
        state_exceeded = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=5,
            max_rewrite_attempts=5,
            checker_exit_code=0,
            checked_body_sha256="h1",
            missing_sections=[],
            missing_contract_keys=[],
        )
        result_exceeded = decide_rewrite_route(state_exceeded)
        assert result_exceeded.route == ROUTE_HUMAN_JUDGMENT_REQUIRED
        assert result_exceeded.reason_code == REASON_CODE_MAX_ATTEMPTS_EXCEEDED

    def test_route_result_to_dict_contains_required_fields(self):
        """RouteResult.to_dict() contains all AC5 terminal result fields."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=0,
            max_rewrite_attempts=3,
            checker_exit_code=0,
            checked_body_sha256="h1",
            missing_sections=[],
            missing_contract_keys=[],
        )
        result = decide_rewrite_route(state)
        d = result.to_dict()
        required_fields = [
            "schema_version",
            "route",
            "reason_code",
            "rewrite_attempt_count",
            "max_rewrite_attempts",
            "checked_body_sha256",
            "checker_exit_code",
            "missing_sections",
            "missing_contract_keys",
            "source_body_reset",
        ]
        for field in required_fields:
            assert field in d, f"Missing field in to_dict(): {field}"

    def test_checker_exit0_proceeds_to_review_not_continue(self):
        """checker_exit_code=0 with no issues -> proceed_to_review, NOT continue_rewrite."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=5,
            checker_exit_code=0,
            checked_body_sha256="hash_new",
            missing_sections=[],
            missing_contract_keys=[],
            previous_checked_body_sha256="hash_old",
            previous_missing_sections=[],
            previous_missing_contract_keys=[],
        )
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_PROCEED_TO_REVIEW
        assert result.route != ROUTE_CONTINUE_REWRITE

    def test_priority_max_exceeded_before_no_progress(self):
        """max_exceeded check has higher priority than no-progress detection."""
        # Even if body hash is unchanged, max_exceeded should fire first
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=3,
            max_rewrite_attempts=3,
            checker_exit_code=1,
            checked_body_sha256="same_hash",
            missing_sections=["## AC"],
            missing_contract_keys=[],
            previous_checked_body_sha256="same_hash",
            previous_missing_sections=["## AC"],
            previous_missing_contract_keys=[],
        )
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_HUMAN_JUDGMENT_REQUIRED
        assert result.reason_code == REASON_CODE_MAX_ATTEMPTS_EXCEEDED

    def test_missing_sections_only_no_progress(self):
        """Only missing_sections non-empty and not decreased -> missing_contract_no_progress."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=5,
            checker_exit_code=1,
            checked_body_sha256="new_hash",
            missing_sections=["## AC", "## Non-Goals"],
            missing_contract_keys=[],
            previous_checked_body_sha256="old_hash",
            previous_missing_sections=["## AC", "## Non-Goals"],
            previous_missing_contract_keys=[],
        )
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_HUMAN_JUDGMENT_REQUIRED
        assert result.reason_code == REASON_CODE_MISSING_CONTRACT_NO_PROGRESS

    def test_missing_keys_only_no_progress(self):
        """Only missing_contract_keys non-empty and not decreased -> missing_contract_no_progress."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=5,
            checker_exit_code=1,
            checked_body_sha256="new_hash",
            missing_sections=[],
            missing_contract_keys=["allowed_paths", "acceptance_criteria"],
            previous_checked_body_sha256="old_hash",
            previous_missing_sections=[],
            previous_missing_contract_keys=["allowed_paths", "acceptance_criteria"],
        )
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_HUMAN_JUDGMENT_REQUIRED
        assert result.reason_code == REASON_CODE_MISSING_CONTRACT_NO_PROGRESS

    def test_state_dataclass_instantiation(self):
        """LOOP_REWRITE_ROUTER_STATE_V1 can be instantiated with all fields."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=2,
            max_rewrite_attempts=5,
            checker_exit_code=0,
            checked_body_sha256="abc",
            missing_sections=["s1"],
            missing_contract_keys=["k1"],
            previous_checked_body_sha256="prev",
            previous_missing_sections=["s1", "s2"],
            previous_missing_contract_keys=["k1", "k2"],
            source_issue_body_sha256="source_sha",
            replay_safe=True,
        )
        assert state.rewrite_attempt_count == 2
        assert state.max_rewrite_attempts == 5
        assert state.checker_exit_code == 0
        assert state.source_issue_body_sha256 == "source_sha"
        assert state.replay_safe is True
