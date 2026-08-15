"""
tests/ci/test_ci_performance_gate_fingerprint_validation.py

Issue #2159 AC5 (P1-2): a comparability fingerprint field holding a
placeholder value ("" / null / "unknown" / "unknown/unknown" / "N/A") must
be treated as missing/invalid and excluded from the cohort -- never
silently accepted as a legitimate equality match (tuple equality between
two placeholder-valued runs would otherwise be a false positive "match").

Fixture-driven unit tests; no live GitHub Actions history required.
"""
from __future__ import annotations

import importlib.util
import pathlib

_MODULE_PATH = pathlib.Path(__file__).resolve().parent / "test_ci_performance_gate.py"


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("test_ci_performance_gate", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate_module()


def _good_baseline(job: str, workflow_run_id: int) -> dict:
    baseline = {
        "schema": "ci_runtime_baseline_v1",
        "job": job,
        "workflow_run_id": workflow_run_id,
        "measurements": [{"phase_id": "test_e2e_ci", "elapsed_ms": 100_000, "status": 0}],
    }
    for field in gate.WITHIN_COHORT_REQUIRED_EQUAL:
        baseline[field] = f"good-{field}-value"
    return baseline


def test_fingerprint_rejects_placeholder_and_missing_values():
    """GIVEN baselines whose fingerprint fields are placeholders
    ("" / null / "unknown" / "unknown/unknown" / "N/A") or entirely
    missing WHEN building a comparable cohort THEN those baselines are
    excluded -- they never end up grouped as if they legitimately matched
    each other."""
    good = [_good_baseline("e2e-core", i) for i in range(1, 21)]

    placeholder_variants = ["", None, "unknown", "unknown/unknown", "N/A"]
    bad = []
    for i, placeholder in enumerate(placeholder_variants, start=1000):
        baseline = _good_baseline("e2e-core", i)
        baseline["host_runner_image"] = placeholder
        bad.append(baseline)

    # A baseline missing a fingerprint field entirely (never set) should
    # also be excluded.
    missing_field_baseline = _good_baseline("e2e-core", 2000)
    del missing_field_baseline["workflow_digest"]
    bad.append(missing_field_baseline)

    cohort = gate._comparable_cohort(good + bad, ("e2e-core",))
    core_cohort = cohort["e2e-core"]

    assert len(core_cohort) == 20
    cohort_ids = {b["workflow_run_id"] for b in core_cohort}
    assert cohort_ids == set(range(1, 21))
    # None of the placeholder/missing-field baselines made it into the cohort.
    excluded_ids = {b["workflow_run_id"] for b in bad}
    assert cohort_ids.isdisjoint(excluded_ids)


def test_is_placeholder_matrix():
    """GIVEN each documented placeholder value WHEN checked THEN
    `_is_placeholder` returns True; a real value returns False."""
    for value in ("", None, "unknown", "unknown/unknown", "N/A"):
        assert gate._is_placeholder(value) is True
    assert gate._is_placeholder("mcr.microsoft.com/playwright@sha256:abc123") is False
    assert gate._is_placeholder("ubuntu-24.04") is False


def test_fingerprint_has_placeholder_detects_any_field():
    baseline = _good_baseline("e2e-core", 1)
    assert gate._fingerprint_has_placeholder(baseline) is False

    baseline["lockfile_hash"] = "unknown"
    assert gate._fingerprint_has_placeholder(baseline) is True


def test_two_placeholder_baselines_never_treated_as_matching_each_other():
    """Regression guard: two baselines that both have `host_runner_image:
    ""` must NOT be grouped together as a "matching" fingerprint pair --
    they must both be excluded from the cohort entirely (fail-closed, not
    fail-open on the placeholder itself matching)."""
    a = _good_baseline("e2e-core", 1)
    a["host_runner_image"] = ""
    b = _good_baseline("e2e-core", 2)
    b["host_runner_image"] = ""

    cohort = gate._comparable_cohort([a, b], ("e2e-core",))
    assert cohort["e2e-core"] == []


def test_v2_producer_shaped_baseline_from_ci_yml_is_admitted_to_cohort():
    """GIVEN 20 baselines shaped exactly like the POST-P0-1-fix
    `.github/workflows/ci.yml` `Collect ci_runtime_baseline_v1 artifact`
    step output (real values: `host_runner_image` from `runner.os`/
    `runner.arch` -- NEVER "unknown/unknown" inside a container, unlike the
    pre-fix `ImageOS`/`ImageVersion`-sourced `runner_image` --
    `playwright_container_image_digest` from the job's pinned container
    image, `cohort_role: "ci_default"`, `workflow_run_id` as an int) WHEN
    building a comparable cohort THEN all 20 are admitted (#2159 P0-1: this
    is the regression guard that the real producer/consumer field-contract
    gap the adversarial review found -- issuecomment-5295659213 P0-1 -- is
    now actually closed, not just fixture-simulated with hand-picked-good
    values)."""

    def v2_producer_shaped_baseline(workflow_run_id: int) -> dict:
        return {
            "schema": "ci_runtime_baseline_v1",
            "run_id": str(workflow_run_id),
            "run_attempt": "1",
            "head_sha": "a" * 40,
            "merge_sha": "a" * 40,
            "job": "e2e-core",
            "measurement_method": "date_plus3N_ms",
            "measurements": [{"phase_id": "test_e2e_ci", "elapsed_ms": 100_000, "status": 0}],
            "runner_image": "unknown/unknown",  # legacy field, still placeholder-valued
            "node_version": "v22.14.0",
            "pnpm_version": "11.7.0",
            "playwright_version": "1.60.0",
            "lockfile_hash": "sha256:" + "c" * 64,
            "workflow_digest": "sha256:" + "d" * 64,
            "workflow_run_id": workflow_run_id,
            "host_runner_image": "Linux/X64",
            "playwright_container_image_digest": (
                "sha256:9bd26ad900bb5e0f4dee75839e957a89ae89c2b7ab1e76050e559790e946b948"
            ),
            "cohort_role": "ci_default",
        }

    baselines = [v2_producer_shaped_baseline(i) for i in range(1, 21)]
    cohort = gate._comparable_cohort(baselines, ("e2e-core",))
    assert len(cohort["e2e-core"]) == 20
