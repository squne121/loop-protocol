"""
tests/ci/test_verify_e2e_lane_partition.py

Issue #2119 AC1/AC2/AC7/AC8: e2e-core / e2e-responsive-matrix lane
partition invariants.

AC1/AC2 delegate the actual test-inventory collection to
`scripts/ci/verify-e2e-lane-partition.mjs` (real Playwright `--list
--reporter=json` collection under both `LOOP_E2E_LANE` values) rather than
re-deriving it via a separate Python implementation — a second,
independently-buggy implementation of the same collection logic would not
add real assurance. If `node_modules`/the Playwright binary is not
installed in the current environment, these tests SKIP with a clear reason
(a local/CI environment preflight gap, not a partition contract failure) —
they never silently report PASS for that condition.

AC7/AC8 verify workflow-level artifact-naming and preview-namespace
exactly-once invariants by structurally parsing `.github/workflows/ci.yml`.

Issue #2679 AC2/AC3/AC4 add a `ci-runtime-baseline-*` `if-no-files-found:
error` structural regression test (mirroring the AC7 pattern), plus
fixture-based false-negative/false-positive regression-lock tests that call
the extracted parse/assert logic directly (both this file's own logic and,
via `importlib`, the sibling structural check in
`test_ci_workflow_lane_partition.py` that Issue #2679 AC1 fixed) rather than
re-deriving a second, independently-buggy implementation.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import re
import subprocess
import types

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "ci" / "verify-e2e-lane-partition.mjs"
PLAYWRIGHT_BIN = REPO_ROOT / "node_modules" / ".bin" / "playwright"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CI_WORKFLOW_LANE_PARTITION_TEST_MODULE = REPO_ROOT / "tests" / "ci" / "test_ci_workflow_lane_partition.py"


def _run_partition_check() -> dict:
    if not PLAYWRIGHT_BIN.is_file():
        pytest.skip(
            "playwright binary not installed under node_modules/.bin — run `pnpm install` "
            "first; this is a local environment preflight gap, not a partition failure "
            "(SKIP, not a fabricated PASS, per docs/dev/runtime-verification-policy.md)"
        )
    proc = subprocess.run(
        ["node", str(VERIFY_SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode in (0, 1), (
        f"verify-e2e-lane-partition.mjs errored (exit {proc.returncode}, expected 0 or 1): "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    return json.loads(proc.stdout)


def test_inventory_union_matches_standard_baseline():
    """AC1: union(e2e-core, e2e-responsive-matrix) canonical ids == frozen baseline."""
    result = _run_partition_check()
    assert result["missing_from_union"] == [], result["failures"]
    assert result["extra_in_union"] == [], result["failures"]


def test_provider_inventory_intersection_is_empty():
    """AC2: e2e-core and e2e-responsive-matrix inventories never overlap."""
    result = _run_partition_check()
    assert result["intersection"] == [], result["failures"]


def _load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _collect_upload_artifact_names(job: dict) -> list[str]:
    names = []
    for step in job.get("steps", []):
        if not isinstance(step, dict):
            continue
        uses = str(step.get("uses", ""))
        if uses.split("@", 1)[0] == "actions/upload-artifact":
            with_block = step.get("with") or {}
            names.append(str(with_block.get("name", "")))
    return names


def _collect_upload_artifact_metadata(job: dict) -> list[dict]:
    """Like `_collect_upload_artifact_names`, but also captures each
    `actions/upload-artifact` step's `with.if-no-files-found` value
    (structural field, not free text) so regression tests can assert on it
    directly (Issue #2679 AC2)."""
    metadata = []
    for step in job.get("steps", []):
        if not isinstance(step, dict):
            continue
        uses = str(step.get("uses", ""))
        if uses.split("@", 1)[0] == "actions/upload-artifact":
            with_block = step.get("with") or {}
            metadata.append(
                {
                    "name": str(with_block.get("name", "")),
                    "if_no_files_found": with_block.get("if-no-files-found"),
                }
            )
    return metadata


def _assert_ci_runtime_baseline_if_no_files_found_is_error(jobs: dict) -> None:
    """Issue #2679 AC2 (fix_delta P2-1/P2-3, PR #2681 review comment
    5747896617): `ci-runtime-baseline-*` `actions/upload-artifact` steps in
    both `e2e-core` and `e2e-responsive-matrix` must declare
    `if-no-files-found: error` AND the contract-bound `with.path`, identified
    by EXACT name matching (never substring matching -- see the sibling
    module's docstring for the reproduced under-/over-inclusive failure
    modes this replaces). This delegates identification to the sibling
    `test_ci_workflow_lane_partition.py` module's shared
    `_required_upload_target` / `_find_required_upload_step` helpers (via the
    existing cross-file sibling-module-load pattern) rather than
    reimplementing the same contract-bound matching a second time."""
    sibling = _load_ci_workflow_lane_partition_module()
    for job_name in ("e2e-core", "e2e-responsive-matrix"):
        job = jobs[job_name]
        literal_template, resolved_regex, expected_path = sibling._required_upload_target(
            job_name, "ci-runtime-baseline"
        )
        with_block = sibling._find_required_upload_step(job, literal_template, resolved_regex)
        assert with_block is not None, (
            f"jobs.{job_name} must upload a ci-runtime-baseline-* artifact named exactly "
            f"{literal_template!r} (or matching {resolved_regex.pattern!r}), got upload-artifact "
            f"steps: {_collect_upload_artifact_metadata(job)}"
        )
        actual_path = str(with_block.get("path", ""))
        assert actual_path == expected_path, (
            f"jobs.{job_name} ci-runtime-baseline upload must set with.path == {expected_path!r}, "
            f"got: {actual_path!r}"
        )
        if_no_files_found = with_block.get("if-no-files-found")
        assert if_no_files_found == "error", (
            f"jobs.{job_name} ci-runtime-baseline upload step must set "
            f"if-no-files-found: error, got: {if_no_files_found!r}"
        )


def _load_ci_workflow_lane_partition_module() -> types.ModuleType:
    """Load the sibling `test_ci_workflow_lane_partition.py` module under a
    distinct module name (not the one pytest's own `--import-mode=importlib`
    collection assigns it) so this file can directly re-invoke the Issue
    #2679 AC1 structural check function without reimplementing it, avoiding
    a `sys.modules` name collision with pytest's own collected copy of that
    file."""
    spec = importlib.util.spec_from_file_location(
        "ci2679_test_ci_workflow_lane_partition_reexport",
        CI_WORKFLOW_LANE_PARTITION_TEST_MODULE,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ci_runtime_baseline_artifact_upload_is_fail_closed():
    """Issue #2679 AC2: ci-runtime-baseline-* artifact upload (e2e-core and
    e2e-responsive-matrix) must be if-no-files-found: error, verified by
    structurally parsing .github/workflows/ci.yml."""
    doc = _load_workflow()
    _assert_ci_runtime_baseline_if_no_files_found_is_error(doc["jobs"])


_CI_RUNTIME_BASELINE_PATH = "ci_runtime_baseline_artifacts/"


def _fixture_jobs_with_fail_closed_ci_runtime_baseline() -> dict:
    return {
        "e2e-core": {
            "steps": [
                {
                    "uses": "actions/upload-artifact@v7",
                    "with": {
                        "name": "ci-runtime-baseline-e2e-core-1",
                        "path": _CI_RUNTIME_BASELINE_PATH,
                        "if-no-files-found": "error",
                    },
                }
            ]
        },
        "e2e-responsive-matrix": {
            "steps": [
                {
                    "uses": "actions/upload-artifact@v7",
                    "with": {
                        "name": "ci-runtime-baseline-e2e-responsive-matrix-1",
                        "path": _CI_RUNTIME_BASELINE_PATH,
                        "if-no-files-found": "error",
                    },
                }
            ]
        },
    }


_REQUIRED_CI_RUNTIME_BASELINE_FIXTURE_JOBS = ("e2e-core", "e2e-responsive-matrix")


@pytest.mark.parametrize("job_name", _REQUIRED_CI_RUNTIME_BASELINE_FIXTURE_JOBS)
@pytest.mark.parametrize("mutation", ("delete_field", "explicit_warn"))
def test_ci_runtime_baseline_structural_check_false_negative_on_if_no_files_found_missing_or_warn(
    job_name, mutation
):
    """Issue #2679 AC3 (fix_delta P2-2, PR #2681 review comment 5747896617):
    both an explicit `if-no-files-found: warn` AND a MISSING field entirely
    must make the AC2 check added in this file FAIL, for BOTH e2e-core and
    e2e-responsive-matrix (not just `e2e-core` with an explicit `warn`,
    which was the only case previously covered). A regex `match=` ties the
    failure to the specific mutated job, not just "any AssertionError"."""
    jobs = copy.deepcopy(_fixture_jobs_with_fail_closed_ci_runtime_baseline())
    with_block = jobs[job_name]["steps"][0]["with"]
    if mutation == "delete_field":
        del with_block["if-no-files-found"]
    else:
        with_block["if-no-files-found"] = "warn"
    with pytest.raises(AssertionError, match=rf"jobs\.{re.escape(job_name)}.*if-no-files-found"):
        _assert_ci_runtime_baseline_if_no_files_found_is_error(jobs)


def test_ci_runtime_baseline_structural_check_false_negative_sibling_ac1_check_also_fails_closed():
    """Issue #2679 AC3 (cross-file regression lock): the sibling
    `test_ci_workflow_lane_partition.py` module's AC1-fixed structural check
    must ALSO fail closed on a missing-field mutation, invoked directly (not
    reimplemented) per Issue #2679 Notes. Per-target `warn`/missing-field
    coverage for the sibling's own three targets lives in that file's own
    parametrized tests (`test_provider_evidence_structural_check_false_negative_on_if_no_files_found_missing_or_warn`)
    -- this is a light cross-file lock, not a duplicate of that coverage."""
    sibling = _load_ci_workflow_lane_partition_module()
    ac1_jobs = sibling._fixture_jobs_with_fail_closed_provider_evidence()
    del ac1_jobs["e2e-core"]["steps"][0]["with"]["if-no-files-found"]
    with pytest.raises(AssertionError):
        sibling._assert_provider_evidence_upload_is_fail_closed(ac1_jobs)


def test_ci_runtime_baseline_structural_check_ignores_diagnostic_artifact_with_substring_name():
    """Issue #2679 fix_delta P2-1: an unrelated diagnostic upload-artifact
    step whose name merely CONTAINS `ci-runtime-baseline` as a substring
    (but does not exactly match the required literal/resolved name form)
    must be invisible to the AC2 check added in this file -- it must NOT be
    misidentified as the required upload, and must NOT cause the check to
    fail even though its own `if-no-files-found` is `warn`."""
    jobs = copy.deepcopy(_fixture_jobs_with_fail_closed_ci_runtime_baseline())
    jobs["e2e-core"]["steps"].append(
        {
            "uses": "actions/upload-artifact@v7",
            "with": {
                "name": "debug-ci-runtime-baseline-log",
                "if-no-files-found": "warn",
            },
        }
    )
    _assert_ci_runtime_baseline_if_no_files_found_is_error(jobs)  # must not raise


def test_ci_runtime_baseline_structural_check_false_positive_on_comment_only_runtime_evidence_string():
    """Issue #2679 AC4: a fixture whose steps merely contain the literal
    string "runtime evidence" (comment-like, no real upload-artifact step
    with if-no-files-found: error) must NOT be accepted as PASS by EITHER
    regression test's structural check — the AC2 check added in this file,
    and the AC1-fixed check in the sibling
    `test_ci_workflow_lane_partition.py` file (invoked directly here, not
    reimplemented, per Issue #2679 Notes)."""
    comment_only_jobs = {
        "e2e-core": {
            "steps": [
                {"name": "runtime evidence note for ci-runtime-baseline (comment-only, no real field)"},
            ]
        },
        "e2e-responsive-matrix": {
            "steps": [
                {"name": "runtime evidence note for ci-runtime-baseline (comment-only, no real field)"},
            ]
        },
    }
    with pytest.raises(AssertionError):
        _assert_ci_runtime_baseline_if_no_files_found_is_error(comment_only_jobs)

    sibling = _load_ci_workflow_lane_partition_module()
    with pytest.raises(AssertionError):
        sibling._assert_provider_evidence_upload_is_fail_closed(comment_only_jobs)


def test_provider_artifacts_do_not_collide_and_bind_to_head_sha_and_run_attempt():
    """AC7: provider artifact names never collide across e2e-core / e2e-responsive-matrix
    / preview-namespace, and every artifact newly introduced by this Issue is bound to
    github.run_attempt in its name (head SHA provenance is additionally recorded inside
    the ci_runtime_baseline_v1.json / responsive-canvas-runtime-evidence.json artifact
    bodies themselves, both of which this test cross-checks the producing steps for)."""
    doc = _load_workflow()
    jobs = doc["jobs"]
    core_names = _collect_upload_artifact_names(jobs["e2e-core"])
    responsive_names = _collect_upload_artifact_names(jobs["e2e-responsive-matrix"])

    # `${{ github.job }}` in a name template resolves to a DIFFERENT literal
    # string per job at runtime (e.g. `ci-runtime-baseline-e2e-core-1` vs
    # `ci-runtime-baseline-e2e-responsive-matrix-1`), so identical raw
    # template text containing that expression is not itself a collision --
    # exclude those before the literal-text dedup check below.
    def _is_job_scoped_template(name: str) -> bool:
        return "${{ github.job }}" in name

    literal_names = [n for n in (core_names + responsive_names) if not _is_job_scoped_template(n)]
    assert len(literal_names) == len(set(literal_names)), (
        f"duplicate upload-artifact names across e2e-core/e2e-responsive-matrix: {literal_names}"
    )

    # Artifacts newly introduced by Issue #2119 for the responsive-matrix
    # provider must bind to github.run_attempt (the pre-existing
    # ci-runtime-baseline-* pattern already does this; this test locks that
    # in for the new responsive-canvas-runtime-evidence artifact too).
    responsive_evidence_name = next(
        (n for n in responsive_names if "responsive-canvas-runtime-evidence" in n), None
    )
    assert responsive_evidence_name is not None, (
        f"e2e-responsive-matrix must upload a responsive-canvas-runtime-evidence artifact, got: {responsive_names}"
    )
    assert "run_attempt" in responsive_evidence_name, (
        "responsive-canvas-runtime-evidence artifact name must bind to github.run_attempt: "
        f"{responsive_evidence_name!r}"
    )

    # If-no-files-found: error is the fail-closed contract for this required
    # evidence artifact (Issue #2119 Runtime Verification Applicability
    # fallback_policy.fallback_success_is_pass: false).
    for step in jobs["e2e-responsive-matrix"].get("steps", []):
        if not isinstance(step, dict):
            continue
        with_block = step.get("with") or {}
        if with_block.get("name") == responsive_evidence_name:
            assert with_block.get("if-no-files-found") == "error", (
                "responsive-canvas-runtime-evidence upload must be fail-closed "
                "(if-no-files-found: error)"
            )

    # Head SHA provenance: both provider jobs set EXPECTED_PR_HEAD_SHA and
    # the ci_runtime_baseline_v1.json body written by each provider records
    # head_sha explicitly (structural check on the inline python source).
    for job_name in ("e2e-core", "e2e-responsive-matrix"):
        job = jobs[job_name]
        assert "EXPECTED_PR_HEAD_SHA" in job.get("env", {}), (
            f"jobs.{job_name}.env must declare EXPECTED_PR_HEAD_SHA for head-SHA-bound evidence"
        )
        steps_text = json.dumps(job.get("steps", []))
        assert '"head_sha"' in steps_text or "head_sha" in steps_text, (
            f"jobs.{job_name} must record head_sha in its ci_runtime_baseline_v1 artifact body"
        )


def test_preview_namespace_lane_runs_exactly_once_with_no_isolation_regression():
    """AC8: the preview-namespace lane must still run exactly once (inside e2e-core,
    which this Issue makes the explicit owner of standard E2E / preview-namespace
    exactly-once / existing visual artifacts), and must NOT be duplicated into
    e2e-responsive-matrix."""
    doc = _load_workflow()
    jobs = doc["jobs"]

    def _mentions_preview_namespace(job: dict) -> int:
        text = json.dumps(job.get("steps", []))
        return text.count("preview-namespace") + text.count("PREVIEW_NAMESPACE_LANE")

    core_hits = _mentions_preview_namespace(jobs["e2e-core"])
    responsive_hits = _mentions_preview_namespace(jobs["e2e-responsive-matrix"])
    assert core_hits > 0, "e2e-core must own the preview-namespace lane (Issue #2119 AC8)"
    assert responsive_hits == 0, (
        "e2e-responsive-matrix must not run/duplicate the preview-namespace lane"
    )

    # "Exactly once" (PR #2137 human review issuecomment-5273090534 P2 fix):
    # the prior `<= 1` assertion did not fail if the invocation step were
    # removed entirely (zero invocations). AC8 requires EXACTLY one real
    # invocation step in e2e-core, and NONE in e2e-responsive-matrix -- both
    # identified by their `run:` field (an execution marker), not by raw
    # substring occurrence across the job's full steps text (which would
    # also match unrelated comments/metadata referencing the same string).
    def _invocation_steps(job: dict) -> list[dict]:
        return [
            s
            for s in job.get("steps", [])
            if isinstance(s, dict) and "test:e2e:preview-namespace" in str(s.get("run", ""))
        ]

    core_invocation_steps = _invocation_steps(jobs["e2e-core"])
    responsive_invocation_steps = _invocation_steps(jobs["e2e-responsive-matrix"])

    assert len(core_invocation_steps) == 1, (
        f"preview-namespace lane must run exactly once in e2e-core, found "
        f"{len(core_invocation_steps)} invocation steps"
    )
    assert len(responsive_invocation_steps) == 0, (
        f"preview-namespace lane must not run in e2e-responsive-matrix, found "
        f"{len(responsive_invocation_steps)} invocation steps"
    )

    # Confirm the single e2e-core invocation step will actually EXECUTE (not
    # just be present in the workflow text): it must have a non-empty `run:`
    # command containing the real pnpm invocation, a distinct step `id`
    # (the execution/timing marker `timed-e2e-preview-namespace` this step
    # writes to `measurements.jsonl` under, cross-checked at runtime by the
    # ci_runtime_baseline_v1 pipeline), and must not be unconditionally
    # skipped via an `if:` guard that always evaluates false.
    invocation_step = core_invocation_steps[0]
    run_command = str(invocation_step.get("run", ""))
    assert "pnpm run test:e2e:preview-namespace" in run_command, (
        f"preview-namespace invocation step must actually invoke the pnpm script, got run={run_command!r}"
    )
    assert invocation_step.get("id"), (
        "preview-namespace invocation step must declare a stable `id` (execution marker)"
    )

    def _is_unconditional_skip(condition: object) -> bool:
        if condition is None:
            return False
        normalized = str(condition).strip().lower()
        return normalized in ("false", "${{ false }}")

    assert not _is_unconditional_skip(invocation_step.get("if")), (
        f"preview-namespace invocation step must not be unconditionally skipped: "
        f"if={invocation_step.get('if')!r}"
    )
