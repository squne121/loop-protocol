"""
tests/ci/test_ci_workflow_lane_partition.py

Issue #2119 AC5/AC6/AC11/AC14: DAG topology, aggregate-job three-mode
failure differentiation, existing consumer authority, and the lane
selector's exclusive enum contract.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import types

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CI_VERDICT_SCRIPT = REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "scripts" / "ci_verdict_summary_v2.py"
PLAYWRIGHT_BIN = REPO_ROOT / "node_modules" / ".bin" / "playwright"
PW_CONFIG = REPO_ROOT / "playwright.config.ts"


def _load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _collect_upload_artifact_if_no_files_found(job: dict) -> dict[str, str]:
    """Map each `actions/upload-artifact` step's `with.name` to its
    `with.if-no-files-found` value for a single job, by structurally
    inspecting the job/step/`with` block fields (never free text)."""
    result: dict[str, str] = {}
    for step in job.get("steps", []):
        if not isinstance(step, dict):
            continue
        uses = str(step.get("uses", ""))
        if uses.split("@", 1)[0] == "actions/upload-artifact":
            with_block = step.get("with") or {}
            name = str(with_block.get("name", ""))
            result[name] = str(with_block.get("if-no-files-found", ""))
    return result


def _assert_provider_evidence_upload_is_fail_closed(jobs: dict) -> None:
    """Issue #2679 AC1: structural (job/step/`with`-block) replacement for
    the removed free-text fallback (`"runtime evidence" in steps_text`).

    Each provider job's own required-evidence `actions/upload-artifact` step
    must declare `if-no-files-found: error`. A job whose steps merely
    contain the literal string "runtime evidence" somewhere (e.g. in an
    unrelated comment-like field), without the artifact actually existing
    with the required field set, must NOT satisfy this check.
    """
    for provider_job_name, required_fragment in (
        ("e2e-core", "ci-runtime-baseline"),
        ("e2e-responsive-matrix", "ci-runtime-baseline"),
        ("e2e-responsive-matrix", "responsive-canvas-runtime-evidence"),
    ):
        provider_job = jobs[provider_job_name]
        upload_map = _collect_upload_artifact_if_no_files_found(provider_job)
        matching = {name: value for name, value in upload_map.items() if required_fragment in name}
        assert matching, (
            f"jobs.{provider_job_name} must upload an artifact whose name contains "
            f"{required_fragment!r} (structural evidence-binding check), got upload-artifact "
            f"names: {list(upload_map)}"
        )
        assert all(value == "error" for value in matching.values()), (
            f"jobs.{provider_job_name} artifact(s) matching {required_fragment!r} must set "
            f"if-no-files-found: error (fail-closed evidence binding), got: {matching}"
        )


def test_e2e_core_and_e2e_responsive_matrix_have_no_dag_cross_dependency():
    doc = _load_workflow()
    jobs = doc["jobs"]
    assert "e2e-core" in jobs, "static topology failure: jobs.e2e-core missing"
    assert "e2e-responsive-matrix" in jobs, "static topology failure: jobs.e2e-responsive-matrix missing"

    core_needs = jobs["e2e-core"].get("needs")
    responsive_needs = jobs["e2e-responsive-matrix"].get("needs")

    def _as_set(needs) -> set[str]:
        if needs is None:
            return set()
        if isinstance(needs, str):
            return {needs}
        return set(needs)

    assert "e2e-responsive-matrix" not in _as_set(core_needs)
    assert "e2e-core" not in _as_set(responsive_needs)


def test_aggregate_e2e_uses_needs_and_if_always_and_distinguishes_three_failure_modes():
    doc = _load_workflow()
    jobs = doc["jobs"]
    assert "e2e" in jobs, "static topology failure: jobs.e2e (aggregate) missing"
    aggregate = jobs["e2e"]

    needs = aggregate.get("needs")
    assert isinstance(needs, list), "jobs.e2e.needs must be a list"
    assert set(needs) == {"e2e-core", "e2e-responsive-matrix"}
    assert aggregate.get("if") == "always()"

    steps_text = json.dumps(aggregate.get("steps", []))
    # (b) runtime result failure: failure/cancelled/skipped are each
    # distinguished (not collapsed into one generic branch).
    for token in ("failure", "cancelled", "skipped"):
        assert f'"{token}"' in steps_text or f"'{token}'" in steps_text or token in steps_text, (
            f"aggregate e2e must distinguish runtime result '{token}'"
        )
    assert "needs.e2e-core.result" in steps_text
    assert "needs.e2e-responsive-matrix.result" in steps_text

    # (c) runtime evidence failure: the aggregate must not degrade to
    # success purely on `result == success` without any evidence binding —
    # each provider's own if-no-files-found: error step is the enforcement
    # mechanism. This is verified structurally (job/step/`with`-block direct
    # inspection of each dependency named in `jobs.e2e.needs` above), not by
    # matching a free-text fallback string against this job's own steps
    # (Issue #2679 AC1 — the prior `or "runtime evidence" in steps_text`
    # fallback allowed a comment-only PASS with no real field present).
    _assert_provider_evidence_upload_is_fail_closed(jobs)


def _fixture_jobs_with_fail_closed_provider_evidence() -> dict:
    """Minimal synthetic job dicts that satisfy
    `_assert_provider_evidence_upload_is_fail_closed` as-is (used as the
    baseline for the false-negative/false-positive regression-lock tests
    below, per Issue #2679 AC3/AC4)."""
    return {
        "e2e-core": {
            "steps": [
                {
                    "name": "Upload ci-runtime-baseline artifact",
                    "uses": "actions/upload-artifact@v7",
                    "with": {
                        "name": "ci-runtime-baseline-e2e-core-1",
                        "if-no-files-found": "error",
                    },
                }
            ]
        },
        "e2e-responsive-matrix": {
            "steps": [
                {
                    "name": "Upload ci-runtime-baseline artifact",
                    "uses": "actions/upload-artifact@v7",
                    "with": {
                        "name": "ci-runtime-baseline-e2e-responsive-matrix-1",
                        "if-no-files-found": "error",
                    },
                },
                {
                    "name": "Upload responsive-canvas-runtime-evidence artifact",
                    "uses": "actions/upload-artifact@v7",
                    "with": {
                        "name": "responsive-canvas-runtime-evidence-${{ github.run_attempt }}",
                        "if-no-files-found": "error",
                    },
                },
            ]
        },
    }


def test_provider_evidence_structural_check_fixture_baseline_passes():
    """Sanity check: the fixture used by the false-negative/false-positive
    tests below is itself accepted by the structural check as-is."""
    _assert_provider_evidence_upload_is_fail_closed(_fixture_jobs_with_fail_closed_provider_evidence())


def test_provider_evidence_structural_check_false_negative_on_if_no_files_found_warn():
    """Issue #2679 AC3: mutating a fixture's `if-no-files-found` to `warn`
    must make the structural check FAIL — it must not silently pass."""
    jobs = _fixture_jobs_with_fail_closed_provider_evidence()
    jobs["e2e-core"]["steps"][0]["with"]["if-no-files-found"] = "warn"
    with pytest.raises(AssertionError):
        _assert_provider_evidence_upload_is_fail_closed(jobs)


def test_provider_evidence_structural_check_false_positive_on_comment_only_runtime_evidence_string():
    """Issue #2679 AC4: a fixture job whose steps merely contain the literal
    string "runtime evidence" (comment-like, no real `uses`/`with` fields)
    must NOT be accepted as PASS by the structural check."""
    jobs = {
        "e2e-core": {
            "steps": [
                {"name": "runtime evidence note (comment-only, no real upload-artifact step)"},
            ]
        },
        "e2e-responsive-matrix": {
            "steps": [
                {"name": "runtime evidence note (comment-only, no real upload-artifact step)"},
            ]
        },
    }
    with pytest.raises(AssertionError):
        _assert_provider_evidence_upload_is_fail_closed(jobs)


def _load_ci_verdict_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("ci_verdict_summary_v2", CI_VERDICT_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_existing_visual_and_ci_verdict_consumers_treat_aggregate_e2e_as_authoritative():
    doc = _load_workflow()
    jobs = doc["jobs"]
    needs = jobs["ci-verdict-summary"]["needs"]
    assert "e2e" in needs, "ci-verdict-summary must still depend on the stable aggregate check name 'e2e'"
    assert "e2e-core" not in needs and "e2e-responsive-matrix" not in needs, (
        "ci-verdict-summary must reference the stable aggregate 'e2e', not the provider jobs directly"
    )

    v2 = _load_ci_verdict_module()
    assert v2.get_classification("ci", "e2e") in {"required", "evidence"}


def test_lane_selector_enum_rejects_invalid_multi_lane_combination():
    if not PLAYWRIGHT_BIN.is_file():
        pytest.skip("playwright binary not installed under node_modules/.bin — run `pnpm install` first")

    def _list(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
        import os

        env = dict(os.environ)
        env.update(env_overrides)
        env["CI"] = "true"
        return subprocess.run(
            [str(PLAYWRIGHT_BIN), "test", "--list", "--reporter=json", f"--config={PW_CONFIG}"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    ok = _list({"LOOP_E2E_LANE": "core"})
    assert ok.returncode == 0, f"LOOP_E2E_LANE=core must succeed: {ok.stderr}"

    multi = _list({"LOOP_E2E_LANE": "core,responsive"})
    assert multi.returncode != 0, "LOOP_E2E_LANE=core,responsive (multi-lane) must be rejected fail-closed"
    assert "LOOP_E2E_LANE" in multi.stderr

    unknown = _list({"LOOP_E2E_LANE": "bogus-lane"})
    assert unknown.returncode != 0, "LOOP_E2E_LANE=bogus-lane (unknown) must be rejected fail-closed"
    assert "LOOP_E2E_LANE" in unknown.stderr

    # AC8/AC14 fix_delta (PR #2137 review, iteration 1): `e2e-core` owns
    # preview-namespace-exactly-once, so `LOOP_E2E_LANE=core` combined with
    # `LOOP_E2E_PREVIEW_NAMESPACE_LANE=true` is a LEGITIMATE combination
    # (the e2e-core CI job runs both the standard core suite and, in a
    # later step, the dedicated preview-namespace spec under the same
    # job-level LOOP_E2E_LANE=core env var) and must NOT be rejected.
    # The preview-namespace spec itself requires LOOP_EXPECTED_STORAGE_KEY to
    # be set at module scope (unrelated to the LOOP_E2E_LANE selector under
    # test here) -- provide a valid non-production value so a --list
    # collection failure there can never be misread as a lane-selector
    # rejection.
    core_with_preview_namespace_flag = _list(
        {
            "LOOP_E2E_LANE": "core",
            "LOOP_E2E_PREVIEW_NAMESPACE_LANE": "true",
            "LOOP_EXPECTED_STORAGE_KEY": "loop-protocol.preview.pr-0.mvp.save",
        }
    )
    assert core_with_preview_namespace_flag.returncode == 0, (
        "LOOP_E2E_LANE=core + LOOP_E2E_PREVIEW_NAMESPACE_LANE=true must be "
        f"accepted (e2e-core owns preview-namespace-exactly-once): "
        f"{core_with_preview_namespace_flag.stderr}"
    )

    # `responsive` has its own dedicated, mutually exclusive spec selection,
    # so combining it with the preview-namespace flag remains a genuine,
    # fail-closed-rejected inconsistency.
    responsive_with_preview_namespace_flag = _list(
        {"LOOP_E2E_LANE": "responsive", "LOOP_E2E_PREVIEW_NAMESPACE_LANE": "true"}
    )
    assert responsive_with_preview_namespace_flag.returncode != 0, (
        "LOOP_E2E_LANE=responsive + LOOP_E2E_PREVIEW_NAMESPACE_LANE=true "
        "must be rejected fail-closed"
    )
    assert "LOOP_E2E_LANE" in responsive_with_preview_namespace_flag.stderr
