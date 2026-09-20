"""
tests/ci/test_ci_workflow_lane_partition.py

Issue #2119 AC5/AC6/AC11/AC14: DAG topology, aggregate-job three-mode
failure differentiation, existing consumer authority, and the lane
selector's exclusive enum contract.
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


# Issue #2679 fix_delta (PR #2681 review, comment 5747896617) P2-1: the three
# logical required uploads Issue #2679 owns, keyed by (job_name, artifact_kind).
# Each target is identified by an EXACT match against either the raw,
# unresolved GHA expression template text (as it appears in a static parse of
# ci.yml) or a regex full-match against the job-resolved literal form (as used
# by this repo's own fixtures) -- never a substring match, so an unrelated
# diagnostic artifact that merely contains the same text is invisible to this
# check rather than being misidentified as the required upload.
_CI_RUNTIME_BASELINE_TEMPLATE = "ci-runtime-baseline-${{ github.job }}-${{ github.run_attempt }}"
_RESPONSIVE_CANVAS_EVIDENCE_TEMPLATE = "responsive-canvas-runtime-evidence-${{ github.run_attempt }}"
_CI_RUNTIME_BASELINE_PATH = "ci_runtime_baseline_artifacts/"
_RESPONSIVE_CANVAS_EVIDENCE_PATH = "responsive-canvas-runtime-evidence-artifact/"


def _required_upload_target(job_name: str, artifact_kind: str) -> tuple[str, "re.Pattern[str]", str]:
    """Return `(literal_template_name, resolved_name_regex, expected_path)`
    for one of Issue #2679's three logical required uploads, keyed by
    `job_name` and `artifact_kind` (`"ci-runtime-baseline"` or
    `"responsive-canvas-runtime-evidence"`)."""
    if artifact_kind == "ci-runtime-baseline":
        return (
            _CI_RUNTIME_BASELINE_TEMPLATE,
            re.compile(rf"^ci-runtime-baseline-{re.escape(job_name)}-\d+$"),
            _CI_RUNTIME_BASELINE_PATH,
        )
    if artifact_kind == "responsive-canvas-runtime-evidence":
        return (
            _RESPONSIVE_CANVAS_EVIDENCE_TEMPLATE,
            re.compile(r"^responsive-canvas-runtime-evidence-\d+$"),
            _RESPONSIVE_CANVAS_EVIDENCE_PATH,
        )
    raise ValueError(f"unknown artifact_kind: {artifact_kind!r}")


def _find_required_upload_step(
    job: dict, literal_template: str, resolved_regex: "re.Pattern[str]"
) -> dict | None:
    """Return the `with` block of the single `actions/upload-artifact` step
    in `job` whose `with.name` exactly equals `literal_template` (the raw,
    unresolved GHA expression form) or exactly regex-fullmatches
    `resolved_regex` (the job-resolved literal form). Names that merely
    CONTAIN the target as a substring are invisible to this check -- neither
    a match nor a conflicting duplicate -- so an added diagnostic artifact
    never affects the verdict. Returns `None` if no step matches."""
    for step in job.get("steps", []):
        if not isinstance(step, dict):
            continue
        uses = str(step.get("uses", ""))
        if uses.split("@", 1)[0] != "actions/upload-artifact":
            continue
        with_block = step.get("with") or {}
        name = str(with_block.get("name", ""))
        if name == literal_template or resolved_regex.fullmatch(name):
            return with_block
    return None


_REQUIRED_PROVIDER_EVIDENCE_TARGETS = (
    ("e2e-core", "ci-runtime-baseline"),
    ("e2e-responsive-matrix", "ci-runtime-baseline"),
    ("e2e-responsive-matrix", "responsive-canvas-runtime-evidence"),
)


def _assert_provider_evidence_upload_is_fail_closed(jobs: dict) -> None:
    """Issue #2679 AC1: structural (job/step/`with`-block) replacement for
    the removed free-text fallback (`"runtime evidence" in steps_text`).

    Each provider job's own required-evidence `actions/upload-artifact` step
    must be identified by exact contract-bound name AND `with.path` (fix_delta
    P2-1 -- substring matching both under- and over-matches), and must
    declare `if-no-files-found: error`. A job whose steps merely contain the
    literal string "runtime evidence" somewhere (e.g. in an unrelated
    comment-like field), without the artifact actually existing with the
    required fields set, must NOT satisfy this check.
    """
    for provider_job_name, artifact_kind in _REQUIRED_PROVIDER_EVIDENCE_TARGETS:
        provider_job = jobs[provider_job_name]
        literal_template, resolved_regex, expected_path = _required_upload_target(
            provider_job_name, artifact_kind
        )
        with_block = _find_required_upload_step(provider_job, literal_template, resolved_regex)
        assert with_block is not None, (
            f"jobs.{provider_job_name} must upload an artifact named exactly {literal_template!r} "
            f"(or matching {resolved_regex.pattern!r}) -- structural evidence-binding check for "
            f"{artifact_kind} -- got upload-artifact names: "
            f"{list(_collect_upload_artifact_if_no_files_found(provider_job))}"
        )
        actual_path = str(with_block.get("path", ""))
        assert actual_path == expected_path, (
            f"jobs.{provider_job_name} {artifact_kind} upload must set with.path == "
            f"{expected_path!r}, got: {actual_path!r}"
        )
        if_no_files_found = with_block.get("if-no-files-found")
        assert if_no_files_found == "error", (
            f"jobs.{provider_job_name} {artifact_kind} upload must set "
            f"if-no-files-found: error (fail-closed evidence binding), got: {if_no_files_found!r}"
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
                        "path": _CI_RUNTIME_BASELINE_PATH,
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
                        "path": _CI_RUNTIME_BASELINE_PATH,
                        "if-no-files-found": "error",
                    },
                },
                {
                    "name": "Upload responsive-canvas-runtime-evidence artifact",
                    "uses": "actions/upload-artifact@v7",
                    "with": {
                        "name": "responsive-canvas-runtime-evidence-${{ github.run_attempt }}",
                        "path": _RESPONSIVE_CANVAS_EVIDENCE_PATH,
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


# (job_name, step_index, artifact_kind) for each of the three required
# uploads, indexed against `_fixture_jobs_with_fail_closed_provider_evidence`.
_REQUIRED_PROVIDER_EVIDENCE_FIXTURE_TARGETS = (
    ("e2e-core", 0, "ci-runtime-baseline"),
    ("e2e-responsive-matrix", 0, "ci-runtime-baseline"),
    ("e2e-responsive-matrix", 1, "responsive-canvas-runtime-evidence"),
)


@pytest.mark.parametrize("job_name,step_index,artifact_kind", _REQUIRED_PROVIDER_EVIDENCE_FIXTURE_TARGETS)
@pytest.mark.parametrize("mutation", ("delete_field", "explicit_warn"))
def test_provider_evidence_structural_check_false_negative_on_if_no_files_found_missing_or_warn(
    job_name, step_index, artifact_kind, mutation
):
    """Issue #2679 AC3 (fix_delta P2-2, PR #2681 review comment 5747896617):
    both an explicit `if-no-files-found: warn` AND a MISSING field entirely
    must fail closed, for EACH of the three required uploads (not just
    `e2e-core` with an explicit `warn`, which was the only case previously
    covered). A regex `match=` on the raised message ties the failure to the
    specific mutated job/artifact, not just "any AssertionError"."""
    jobs = copy.deepcopy(_fixture_jobs_with_fail_closed_provider_evidence())
    with_block = jobs[job_name]["steps"][step_index]["with"]
    if mutation == "delete_field":
        del with_block["if-no-files-found"]
    else:
        with_block["if-no-files-found"] = "warn"
    with pytest.raises(
        AssertionError, match=rf"jobs\.{re.escape(job_name)}.*{re.escape(artifact_kind)}.*if-no-files-found"
    ):
        _assert_provider_evidence_upload_is_fail_closed(jobs)


def test_provider_evidence_structural_check_false_negative_free_text_does_not_rescue_missing_field():
    """Issue #2679 AC4 (fix_delta P2-2): free text mentioning "runtime
    evidence" in a step's own `name` field must not rescue a structurally
    broken fixture (real upload-artifact step present, but the
    `if-no-files-found` field deleted) -- proving free text never rescues a
    structurally-broken fixture, not just that a whole-step-missing case
    fails (already covered below)."""
    jobs = copy.deepcopy(_fixture_jobs_with_fail_closed_provider_evidence())
    step = jobs["e2e-responsive-matrix"]["steps"][1]
    step["name"] = "runtime evidence upload (free text must not rescue this)"
    del step["with"]["if-no-files-found"]
    with pytest.raises(
        AssertionError, match=r"jobs\.e2e-responsive-matrix.*responsive-canvas-runtime-evidence.*if-no-files-found"
    ):
        _assert_provider_evidence_upload_is_fail_closed(jobs)


def test_provider_evidence_structural_check_false_negative_on_renamed_required_artifact():
    """Issue #2679 fix_delta P2-1: renaming the actual required artifact so
    it no longer exactly matches the required literal/resolved name form
    must FAIL. Reproduced failure mode: substring matching previously
    WRONGLY ACCEPTED `debug-ci-runtime-baseline-e2e-core-1` as satisfying the
    `e2e-core` ci-runtime-baseline requirement."""
    jobs = copy.deepcopy(_fixture_jobs_with_fail_closed_provider_evidence())
    jobs["e2e-core"]["steps"][0]["with"]["name"] = "debug-ci-runtime-baseline-e2e-core-1"
    with pytest.raises(AssertionError, match=r"jobs\.e2e-core.*ci-runtime-baseline"):
        _assert_provider_evidence_upload_is_fail_closed(jobs)


def test_provider_evidence_structural_check_false_negative_on_wrong_path():
    """Issue #2679 fix_delta P2-1: swapping `with.path` on the real required
    artifact while keeping its name and `if-no-files-found: error` intact
    must FAIL. Reproduced failure mode: the prior check never inspected
    `with.path`, so it could not tell a real evidence upload from one
    pointing at the wrong file (e.g. `README.md`)."""
    jobs = copy.deepcopy(_fixture_jobs_with_fail_closed_provider_evidence())
    jobs["e2e-core"]["steps"][0]["with"]["path"] = "README.md"
    with pytest.raises(AssertionError, match=r"jobs\.e2e-core.*ci-runtime-baseline.*with\.path"):
        _assert_provider_evidence_upload_is_fail_closed(jobs)


def test_provider_evidence_structural_check_ignores_diagnostic_artifact_with_substring_name():
    """Issue #2679 fix_delta P2-1: an unrelated diagnostic upload-artifact
    step whose name merely CONTAINS `ci-runtime-baseline` as a substring
    (but does not exactly match the required literal/resolved name form)
    must be invisible to this check -- it must NOT be misidentified as the
    required upload, and must NOT cause the check to fail even though its
    own `if-no-files-found` is `warn` (Issue #2119's design allows
    diagnostic artifacts at `warn` alongside required evidence at `error`,
    this is not scope this check owns). Regression lock for the
    reproduced-and-fixed over-inclusive substring-matching failure mode."""
    jobs = copy.deepcopy(_fixture_jobs_with_fail_closed_provider_evidence())
    jobs["e2e-core"]["steps"].append(
        {
            "name": "Upload debug log",
            "uses": "actions/upload-artifact@v7",
            "with": {
                "name": "debug-ci-runtime-baseline-log",
                "if-no-files-found": "warn",
            },
        }
    )
    _assert_provider_evidence_upload_is_fail_closed(jobs)  # must not raise


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
