"""
tests/ci/test_ci_performance_gate_clock_alignment.py

Issue #2159 AC4 (P0-6): gate-ready latency before/after must be computed
from a single shared function fed by the SAME clock source (GitHub API
`workflow_run.run_started_at` -> corresponding check `completed_at`) for
both arms, eliminating the previous apples-to-oranges comparison (before:
manual `measurements.jsonl` elapsed-time sum; after: GitHub API).

Fixture-driven unit tests; no live GitHub Actions history required.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_MODULE_PATH = pathlib.Path(__file__).resolve().parent / "test_ci_performance_gate.py"


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("test_ci_performance_gate", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate_module()


def test_gate_ready_latency_uses_same_github_api_clock():
    """GIVEN a before-arm baseline and an after-arm baseline, each carrying
    run_started_at/check_completed_at GitHub API timestamps WHEN
    gate-ready latency is computed for both THEN the SAME function
    (`_gate_ready_latency_seconds_same_clock`) is used for both arms, and
    the result matches a hand-computed wall-clock delta -- not a
    `measurements.jsonl` elapsed-time sum."""
    before_baseline = {
        "schema": "ci_runtime_baseline_v1",
        "job": "e2e",
        "cohort_role": "before",
        "run_started_at": "2026-01-01T00:00:00Z",
        "check_completed_at": "2026-01-01T00:05:00Z",
        # A manual measurements.jsonl sum that intentionally disagrees with
        # the GitHub-API-clock delta above, to prove the same-clock
        # function is actually used (not silently falling back to this).
        "measurements": [{"phase_id": "test_e2e_ci", "elapsed_ms": 999_000, "status": 0}],
    }
    after_baseline = {
        "schema": "ci_runtime_baseline_v1",
        "job": "e2e",
        "cohort_role": "after",
        "run_started_at": "2026-01-02T00:00:00Z",
        "check_completed_at": "2026-01-02T00:03:00Z",
    }

    before_latency = gate._gate_ready_latency_seconds_from_baseline(before_baseline)
    after_latency = gate._gate_ready_latency_seconds_from_baseline(after_baseline)

    assert before_latency == 300.0  # 5 minutes, from the GitHub API clock, not 999s.
    assert after_latency == 180.0  # 3 minutes.

    # Both computed via the exact same function -- verifies architecturally
    # that before/after share one clock source, not two divergent ones.
    assert before_latency == gate._gate_ready_latency_seconds_same_clock(
        before_baseline["run_started_at"], before_baseline["check_completed_at"]
    )
    assert after_latency == gate._gate_ready_latency_seconds_same_clock(
        after_baseline["run_started_at"], after_baseline["check_completed_at"]
    )


def test_gate_ready_latency_missing_clock_fields_excluded_not_fallback():
    """GIVEN a baseline missing run_started_at/check_completed_at WHEN
    gate-ready latency is computed THEN it returns None (excluded from the
    cohort) rather than silently falling back to the measurements.jsonl
    sum (which would reintroduce the apples-to-oranges bug)."""
    baseline = {
        "schema": "ci_runtime_baseline_v1",
        "job": "e2e",
        "cohort_role": "before",
        "measurements": [{"phase_id": "test_e2e_ci", "elapsed_ms": 300_000, "status": 0}],
    }
    assert gate._gate_ready_latency_seconds_from_baseline(baseline) is None


def test_gate_ready_latency_rejects_negative_delta():
    """GIVEN check_completed_at BEFORE run_started_at (malformed/clock-skew
    data) WHEN computing gate-ready latency THEN it raises rather than
    silently returning a negative or absolute-valued latency."""
    with pytest.raises(ValueError):
        gate._gate_ready_latency_seconds_same_clock(
            "2026-01-01T00:05:00Z", "2026-01-01T00:00:00Z"
        )


def test_gate_ready_latency_handles_z_and_offset_timestamps_identically():
    """GIVEN two equivalent ISO 8601 timestamps, one with a `Z` suffix and
    one with an explicit `+00:00` offset, WHEN parsed THEN they produce the
    identical latency (the same-clock function is offset-format-agnostic,
    important since GitHub API returns `Z`-suffixed UTC timestamps)."""
    latency_z = gate._gate_ready_latency_seconds_same_clock(
        "2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z"
    )
    latency_offset = gate._gate_ready_latency_seconds_same_clock(
        "2026-01-01T00:00:00+00:00", "2026-01-01T00:10:00+00:00"
    )
    assert latency_z == latency_offset == 600.0
