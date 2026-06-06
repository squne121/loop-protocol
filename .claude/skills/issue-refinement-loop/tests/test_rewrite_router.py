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


# ---------------------------------------------------------------------------
# Blocker 2 (review #718): set-theoretic no-progress correctness
# ---------------------------------------------------------------------------

from decide_rewrite_route import (  # noqa: E402
    load_rewrite_router_state,
    save_rewrite_router_state,
    validate_state_dict,
    RewriteRouterStateError,
)


class TestNoProgressSetSemantics:
    """No-progress must use strict-subset semantics, not length comparison."""

    def test_replacement_missing_item_is_not_progress(self):
        """prev {A, B} -> current {C}: a replacement is NOT progress.

        Length shrank (2 -> 1) but {C} is not a subset of {A, B}, so the missing
        universe did not strictly decrease. Must route human_judgment_required.
        """
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=2,
            checker_exit_code=1,
            checked_body_sha256="new_hash",
            missing_sections=["C"],
            missing_contract_keys=[],
            previous_checked_body_sha256="old_hash",
            previous_missing_sections=["A", "B"],
            previous_missing_contract_keys=[],
        )
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_HUMAN_JUDGMENT_REQUIRED
        assert result.reason_code == REASON_CODE_MISSING_CONTRACT_NO_PROGRESS

    def test_one_category_decreases_but_other_grows_is_not_progress(self):
        """sections shrink but contract keys grow -> NOT progress.

        prev sections {S1, S2} / keys {} ; current sections {S1} / keys {K1}.
        Per-category OR logic would (wrongly) pass this; the combined universe
        {(section,S1),(contract_key,K1)} is not a subset of {(section,S1),(section,S2)}.
        """
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=2,
            checker_exit_code=1,
            checked_body_sha256="new_hash",
            missing_sections=["S1"],
            missing_contract_keys=["K1"],
            previous_checked_body_sha256="old_hash",
            previous_missing_sections=["S1", "S2"],
            previous_missing_contract_keys=[],
        )
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_HUMAN_JUDGMENT_REQUIRED
        assert result.reason_code == REASON_CODE_MISSING_CONTRACT_NO_PROGRESS

    def test_strict_subset_is_progress(self):
        """prev {A, B} -> current {A}: strict subset IS progress; continue rewrite."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=2,
            checker_exit_code=1,
            checked_body_sha256="new_hash",
            missing_sections=["A"],
            missing_contract_keys=[],
            previous_checked_body_sha256="old_hash",
            previous_missing_sections=["A", "B"],
            previous_missing_contract_keys=[],
        )
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_CONTINUE_REWRITE
        assert result.reason_code == REASON_CODE_CHECKER_FAILED

    def test_cross_category_strict_subset_is_progress(self):
        """A contract key resolved (sections unchanged subset) IS progress."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=2,
            checker_exit_code=0,
            checked_body_sha256="new_hash",
            missing_sections=["S1"],
            missing_contract_keys=[],
            previous_checked_body_sha256="old_hash",
            previous_missing_sections=["S1"],
            previous_missing_contract_keys=["K1"],
        )
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_PROCEED_TO_REVIEW

    def test_same_set_reordered_is_not_progress(self):
        """Same missing set (different order) is not progress (sort+unique normalization)."""
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=2,
            checker_exit_code=1,
            checked_body_sha256="new_hash",
            missing_sections=["B", "A"],
            missing_contract_keys=[],
            previous_checked_body_sha256="old_hash",
            previous_missing_sections=["A", "B"],
            previous_missing_contract_keys=[],
        )
        result = decide_rewrite_route(state)
        assert result.route == ROUTE_HUMAN_JUDGMENT_REQUIRED
        assert result.reason_code == REASON_CODE_MISSING_CONTRACT_NO_PROGRESS


# ---------------------------------------------------------------------------
# Blocker 5 (review #718): source_body_reset is observable in the route result
# ---------------------------------------------------------------------------

VALID_SHA = "a" * 64
OTHER_SHA = "b" * 64


class TestSourceBodyReset:
    """Reset due to source-body change must be visible in the terminal result."""

    def test_loader_reset_sets_flag_and_zeroes_attempts(self, tmp_path):
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=2,
            max_rewrite_attempts=2,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            missing_sections=["S1"],
            missing_contract_keys=[],
            source_issue_body_sha256=VALID_SHA,
        )
        path = str(tmp_path / "state.json")
        save_rewrite_router_state(state, path)

        # Human changed the source body -> different sha
        loaded = load_rewrite_router_state(path, current_source_body_sha256=OTHER_SHA)
        assert loaded is not None
        assert loaded.source_body_reset is True
        assert loaded.rewrite_attempt_count == 0
        assert loaded.source_issue_body_sha256 == OTHER_SHA

    def test_reset_fact_propagates_to_route_result(self):
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=0,
            max_rewrite_attempts=2,
            checker_exit_code=0,
            checked_body_sha256=VALID_SHA,
            missing_sections=[],
            missing_contract_keys=[],
            source_issue_body_sha256=OTHER_SHA,
            source_body_reset=True,
        )
        result = decide_rewrite_route(state)
        assert result.source_body_reset is True
        assert result.to_dict()["source_body_reset"] is True

    def test_no_reset_when_source_unchanged(self, tmp_path):
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=2,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            missing_sections=["S1"],
            missing_contract_keys=[],
            source_issue_body_sha256=VALID_SHA,
        )
        path = str(tmp_path / "state.json")
        save_rewrite_router_state(state, path)
        loaded = load_rewrite_router_state(path, current_source_body_sha256=VALID_SHA)
        assert loaded is not None
        assert loaded.source_body_reset is False
        assert loaded.rewrite_attempt_count == 1


# ---------------------------------------------------------------------------
# Blocker 3 (review #718): schema validation is actually enforced
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    """validate_state_dict enforces the full schema, not just key presence."""

    def _valid_dict(self) -> dict:
        return {
            "schema_version": "loop_rewrite_router_state/v1",
            "rewrite_attempt_count": 0,
            "max_rewrite_attempts": 2,
            "checker_exit_code": 0,
            "checked_body_sha256": VALID_SHA,
            "missing_sections": [],
            "missing_contract_keys": [],
        }

    def test_valid_dict_passes(self):
        ok, _ = validate_state_dict(self._valid_dict())
        assert ok is True

    def test_string_attempt_count_rejected(self):
        d = self._valid_dict()
        d["rewrite_attempt_count"] = "2"
        ok, msg = validate_state_dict(d)
        assert ok is False
        assert msg

    def test_negative_attempt_count_rejected(self):
        d = self._valid_dict()
        d["rewrite_attempt_count"] = -1
        ok, _ = validate_state_dict(d)
        assert ok is False

    def test_max_attempts_zero_rejected(self):
        d = self._valid_dict()
        d["max_rewrite_attempts"] = 0
        ok, _ = validate_state_dict(d)
        assert ok is False

    def test_additional_property_rejected(self):
        d = self._valid_dict()
        d["unexpected_field"] = True
        ok, _ = validate_state_dict(d)
        assert ok is False

    def test_bad_sha256_rejected(self):
        d = self._valid_dict()
        d["checked_body_sha256"] = "not-a-sha"
        ok, _ = validate_state_dict(d)
        assert ok is False

    def test_wrong_schema_version_rejected(self):
        d = self._valid_dict()
        d["schema_version"] = "loop_rewrite_router_state/v2"
        ok, _ = validate_state_dict(d)
        assert ok is False


# ---------------------------------------------------------------------------
# Blocker 4 (review #718): persistence is crash-safe / fail-closed
# ---------------------------------------------------------------------------


class TestPersistenceFailClosed:
    """Corrupt state must NOT be silently reset; save must be atomic."""

    def test_missing_file_returns_none(self, tmp_path):
        loaded = load_rewrite_router_state(str(tmp_path / "nope.json"))
        assert loaded is None

    def test_corrupt_json_raises_not_silent_reset(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{ this is not valid json", encoding="utf-8")
        import pytest

        with pytest.raises(RewriteRouterStateError):
            load_rewrite_router_state(str(path))

    def test_truncated_partial_json_raises(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text('{"rewrite_attempt_count": 2,', encoding="utf-8")
        import pytest

        with pytest.raises(RewriteRouterStateError):
            load_rewrite_router_state(str(path))

    def test_schema_violating_file_raises(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(
            '{"rewrite_attempt_count": "two", "max_rewrite_attempts": 2,'
            ' "checker_exit_code": 0, "checked_body_sha256": "' + VALID_SHA + '"}',
            encoding="utf-8",
        )
        import pytest

        with pytest.raises(RewriteRouterStateError):
            load_rewrite_router_state(str(path))

    def test_save_load_roundtrip_preserves_attempts(self, tmp_path):
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=1,
            max_rewrite_attempts=2,
            checker_exit_code=1,
            checked_body_sha256=VALID_SHA,
            missing_sections=["S1"],
            missing_contract_keys=["K1"],
            previous_checked_body_sha256=OTHER_SHA,
            previous_missing_sections=["S1", "S2"],
            previous_missing_contract_keys=["K1"],
            source_issue_body_sha256=VALID_SHA,
        )
        path = str(tmp_path / "state.json")
        save_rewrite_router_state(state, path)
        loaded = load_rewrite_router_state(path, current_source_body_sha256=VALID_SHA)
        assert loaded is not None
        assert loaded.rewrite_attempt_count == 1
        assert loaded.previous_missing_sections == ["S1", "S2"]

    def test_save_leaves_no_tmp_file(self, tmp_path):
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=0,
            max_rewrite_attempts=2,
            checker_exit_code=0,
            checked_body_sha256=VALID_SHA,
            missing_sections=[],
            missing_contract_keys=[],
        )
        path = str(tmp_path / "state.json")
        save_rewrite_router_state(state, path)
        assert not os.path.exists(path + ".tmp")
        assert os.path.exists(path)


# ---------------------------------------------------------------------------
# Blocker 1 (review #718) / AC9: router is wired into the actual orchestration path
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _SKILL_ROOT / "scripts" / "decide_rewrite_route.py"
_TERMINATION_POLICY = _SKILL_ROOT / "references" / "termination-policy.md"
_SKILL_MD = _SKILL_ROOT / "SKILL.md"


class TestOrchestratorWiring:
    """AC9: decide_rewrite_route must be reachable from the documented runtime path.

    The issue-refinement-loop is a markdown-orchestrated skill, so 'wiring' means
    the normative SSOT (termination-policy.md) documents the router invocation and
    the orchestrator entrypoint (SKILL.md Step 4) references it. These tests fail
    if the router becomes a dead library again (the #718 review Blocker 1).
    """

    def test_termination_policy_documents_router_invocation(self):
        text = _TERMINATION_POLICY.read_text(encoding="utf-8")
        assert "decide_rewrite_route.py" in text
        assert "Rewrite Loop Runtime Router" in text
        # routes the orchestrator must branch on
        assert "continue_rewrite" in text
        assert "proceed_to_review" in text
        assert "human_judgment_required" in text

    def test_skill_md_step4_references_router(self):
        text = _SKILL_MD.read_text(encoding="utf-8")
        assert "decide_rewrite_route.py" in text

    def test_cli_end_to_end_rewrite_loop_sequence(self):
        """Exercise the exact CLI contract the orchestrator invokes, across a loop.

        attempt 0 (checker failing, progress made) -> continue_rewrite
        attempt 1 (checker passing)                -> proceed_to_review
        attempt 2 (max reached)                    -> human_judgment_required
        """

        def run(state: dict) -> dict:
            proc = subprocess.run(
                ["python3", str(_SCRIPT)],
                input=_json.dumps(state),
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, proc.stderr
            return _json.loads(proc.stdout)

        # attempt 0: checker still failing but missing set shrank -> continue
        r0 = run({
            "schema_version": "loop_rewrite_router_state/v1",
            "rewrite_attempt_count": 0,
            "max_rewrite_attempts": 2,
            "checker_exit_code": 1,
            "checked_body_sha256": "c" * 64,
            "missing_sections": ["S1"],
            "missing_contract_keys": [],
            "previous_checked_body_sha256": "d" * 64,
            "previous_missing_sections": ["S1", "S2"],
            "previous_missing_contract_keys": [],
        })
        assert r0["route"] == "continue_rewrite"

        # attempt 1: checker passes -> proceed_to_review
        r1 = run({
            "schema_version": "loop_rewrite_router_state/v1",
            "rewrite_attempt_count": 1,
            "max_rewrite_attempts": 2,
            "checker_exit_code": 0,
            "checked_body_sha256": "e" * 64,
            "missing_sections": [],
            "missing_contract_keys": [],
        })
        assert r1["route"] == "proceed_to_review"

        # attempt 2: max reached -> human_judgment_required
        r2 = run({
            "schema_version": "loop_rewrite_router_state/v1",
            "rewrite_attempt_count": 2,
            "max_rewrite_attempts": 2,
            "checker_exit_code": 1,
            "checked_body_sha256": "f" * 64,
            "missing_sections": ["S1"],
            "missing_contract_keys": [],
        })
        assert r2["route"] == "human_judgment_required"
        assert r2["reason_code"] == "max_attempts_exceeded"

    def test_cli_rejects_schema_violating_input(self):
        """The documented CLI path fail-closes (exit 2) on invalid state."""
        proc = subprocess.run(
            ["python3", str(_SCRIPT)],
            input=_json.dumps({
                "rewrite_attempt_count": "not-an-int",
                "max_rewrite_attempts": 2,
                "checker_exit_code": 0,
                "checked_body_sha256": "g" * 64,
            }),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 2
