"""Issue #2165: per-attempt/total deadline and retry-policy regression coverage.

The old `PER_ATTEMPT_DEADLINE_SECONDS = 90` could not fit even a single
legitimately long-running Verification Command (the `issue-refinement-loop`
skill's own full pytest suite, measured at 111.07s), and the closed
three-attempt retry matrix multiplied that fixed timeout without changing
the outcome (`3 * 90s = 270s` already exceeded the old
`TOTAL_DEADLINE_SECONDS = 240`). These tests pin the new values' internal
consistency and the backend-aware retry-policy fix so a future edit cannot
silently reintroduce either arithmetic break.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import reviewer_transport as transport  # noqa: E402

# Measured in Issue #2165's Background section: `1316 passed in 111.07s`.
MEASURED_FULL_SUITE_SECONDS = 111.07


def test_per_attempt_deadline_covers_measured_worst_case_vc_with_margin():
    # >= 35% margin above the measured full-suite VC duration.
    assert transport.PER_ATTEMPT_DEADLINE_SECONDS >= MEASURED_FULL_SUITE_SECONDS * 1.35


def test_total_deadline_does_not_starve_a_single_full_length_attempt():
    # `run_reviewer_transport()` bounds each attempt's wait() to
    # `min(per_attempt_deadline, total_deadline - elapsed)`; if the total
    # deadline were smaller than the per-attempt deadline, the very first
    # (and, for the deterministic backend, only) attempt would never get
    # its full budget.
    assert transport.TOTAL_DEADLINE_SECONDS >= transport.PER_ATTEMPT_DEADLINE_SECONDS


def test_deterministic_backend_does_not_retry_timeout_or_inner_timeout():
    for reason_code in ("timeout", "inner_timeout"):
        assert (
            transport.retry_matrix(
                backend="deterministic", initial_session_id=None, attempt=1, reason_code=reason_code
            )
            is None
        )


def test_deterministic_backend_still_retries_transport_layer_failures():
    for reason_code in ("spawn_failure", "signal", "empty_output", "nonzero_exit", "capture_failure"):
        assert (
            transport.retry_matrix(
                backend="deterministic", initial_session_id=None, attempt=1, reason_code=reason_code
            )
            is not None
        )


def test_other_backends_still_retry_timeout_unchanged():
    for backend in ("claude", "codex", "fixture"):
        assert (
            transport.retry_matrix(backend=backend, initial_session_id=None, attempt=1, reason_code="timeout")
            is not None
        )


def test_deterministic_non_retryable_timeout_codes_are_exactly_timeout_and_inner_timeout():
    assert transport._DETERMINISTIC_NON_RETRYABLE_TIMEOUT_CODES == frozenset({"timeout", "inner_timeout"})


def test_retry_matrix_respects_max_attempts_regardless_of_backend():
    assert (
        transport.retry_matrix(
            backend="deterministic",
            initial_session_id=None,
            attempt=transport.MAX_ATTEMPTS,
            reason_code="nonzero_exit",
        )
        is None
    )
