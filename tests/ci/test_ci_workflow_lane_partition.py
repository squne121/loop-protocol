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


# ---------------------------------------------------------------------------
# Issue #2969: component-vrt-report negative-control artifact must be bound to
# `github.run_attempt` (no stale-artifact false-green on re-run), its download
# round trip must be bounded, and the real hidden-file upload proof must stay.
# ---------------------------------------------------------------------------
_NC_STATIC_NAME = "component-vrt-negative-control-attachments"
_NC_NAME_RE = re.compile(
    rf"^{re.escape(_NC_STATIC_NAME)}-\$\{{\{{\s*github\.run_attempt\s*\}}\}}$"
)
_NC_UPLOAD_ID = "upload-negative-control-attachments"
_NC_VERIFY_ID = "verify-negative-control-attachments"
_NC_MAX_DOWNLOADS = 4
_NC_MAX_SLEEP_TOTAL_SECONDS = 30
_NC_LITERAL_SLEEP_RE = re.compile(r"^\s*sleep\s+(\d+)\s*$")


def _nc_job(workflow: dict) -> dict:
    return workflow["jobs"]["component-vrt-report"]


def _nc_uses(step: dict, action: str) -> bool:
    return str(step.get("uses", "")).split("@", 1)[0] == action


def _nc_normalize_if(value: object) -> str:
    text = str(value if value is not None else "").strip()
    match = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", text, flags=re.DOTALL)
    return match.group(1) if match else text


def _nc_locate(job: dict) -> dict:
    steps = [s for s in job.get("steps", []) if isinstance(s, dict)]
    upload_idx = [i for i, s in enumerate(steps) if s.get("id") == _NC_UPLOAD_ID]
    verify_idx = [i for i, s in enumerate(steps) if s.get("id") == _NC_VERIFY_ID]
    download_idx = [i for i, s in enumerate(steps) if _nc_uses(s, "actions/download-artifact")]
    return {
        "steps": steps,
        "upload": upload_idx,
        "verify": verify_idx,
        "downloads": download_idx,
    }


def _nc_path_parts(raw: str) -> list[str]:
    cleaned = raw.strip().strip("/")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    parts = [p for p in cleaned.split("/") if p and p != "."]
    while parts and re.fullmatch(r"\*+", parts[-1]):
        parts.pop()
    return parts


def _nc_paths_overlap(a: str, b: str) -> bool:
    pa, pb = _nc_path_parts(a), _nc_path_parts(b)
    if not pa or not pb:
        return True
    n = min(len(pa), len(pb))
    return pa[:n] == pb[:n]


def _nc_check_artifact_name_is_attempt_scoped(job: dict) -> list[str]:
    """Return violations for AC1 (empty list == compliant)."""
    loc = _nc_locate(job)
    steps = loc["steps"]
    violations: list[str] = []
    if len(loc["upload"]) != 1:
        return [f"expected exactly one upload step id={_NC_UPLOAD_ID}, found {len(loc['upload'])}"]
    upload = steps[loc["upload"][0]]
    if not _nc_uses(upload, "actions/upload-artifact"):
        violations.append("negative-control upload step is not actions/upload-artifact")
    upload_name = str((upload.get("with") or {}).get("name", ""))
    if not _NC_NAME_RE.fullmatch(upload_name):
        violations.append(f"upload name is not attempt-scoped: {upload_name!r}")
    if not loc["downloads"]:
        violations.append("no download-artifact step found")
    for idx in loc["downloads"]:
        name = str((steps[idx].get("with") or {}).get("name", ""))
        if not _NC_NAME_RE.fullmatch(name):
            violations.append(f"download step {idx} name is not attempt-scoped: {name!r}")
        elif name != upload_name and re.sub(r"\s+", "", name) != re.sub(r"\s+", "", upload_name):
            violations.append(f"download step {idx} name differs from upload name: {name!r}")
    return violations


def _nc_check_roundtrip_is_bounded(job: dict) -> list[str]:
    """Return violations for AC2 (empty list == compliant)."""
    loc = _nc_locate(job)
    steps = loc["steps"]
    if len(loc["upload"]) != 1 or len(loc["verify"]) != 1:
        return ["upload/verify steps must each be present exactly once"]
    upload_idx, verify_idx = loc["upload"][0], loc["verify"][0]
    downloads = loc["downloads"]
    violations: list[str] = []
    if not 1 <= len(downloads) <= _NC_MAX_DOWNLOADS:
        violations.append(f"download step count {len(downloads)} not in 1..{_NC_MAX_DOWNLOADS}")
    ids = [steps[i].get("id") for i in downloads]
    if any(not i for i in ids):
        violations.append("every download step needs an id")

    # Retry gating: every non-first download is gated on the previous
    # download's `outcome == 'failure'` (never `conclusion`, which is
    # 'success' under continue-on-error).
    for pos, idx in enumerate(downloads):
        step = steps[idx]
        cond = _nc_normalize_if(step.get("if"))
        if step.get("continue-on-error") is not True:
            violations.append(f"download step {idx} must set continue-on-error: true")
        if "conclusion" in cond:
            violations.append(f"download step {idx} gates on conclusion (use outcome)")
        if pos > 0:
            prev_id = steps[downloads[pos - 1]].get("id")
            if f"steps.{prev_id}.outcome == 'failure'" not in cond:
                violations.append(f"download step {idx} is not gated on previous failure outcome")
        elif "failure" in cond:
            violations.append("first download must not be failure-gated")

    # Everything between upload and assert: only upload/download/literal sleep.
    sleep_total = 0
    for idx in range(upload_idx + 1, verify_idx):
        step = steps[idx]
        run = step.get("run")
        if idx in downloads:
            if idx < upload_idx:
                violations.append("download before upload")
            continue
        if run is None:
            violations.append(f"unexpected non-run step {idx} between upload and assert")
            continue
        if re.search(r"\b(while|until|for)\b", str(run)):
            violations.append(f"loop construct in step {idx}")
        match = _NC_LITERAL_SLEEP_RE.fullmatch(str(run))
        if not match:
            violations.append(f"step {idx} is not a literal `sleep N` step: {str(run)!r}")
            continue
        sleep_total += int(match.group(1))
        cond = _nc_normalize_if(step.get("if"))
        if ".outcome == 'failure'" not in cond or "conclusion" in cond:
            violations.append(f"sleep step {idx} is not gated on a failure outcome")
    if sleep_total > _NC_MAX_SLEEP_TOTAL_SECONDS:
        violations.append(f"total sleep {sleep_total}s exceeds {_NC_MAX_SLEEP_TOTAL_SECONDS}s")
    if any(i < upload_idx or i > verify_idx for i in downloads):
        violations.append("download step outside upload..assert window")

    # The assert step must fail closed (and also scan loops/sleeps).
    verify = steps[verify_idx]
    script = str(verify.get("run", ""))
    if re.search(r"\b(while|until|sleep)\b", script):
        violations.append("assert step contains loop/sleep")
    if re.search(r"\|\|\s*true|\bset\s+\+e\b|\bexit\s+0\b|\|\|\s*:", script):
        violations.append("assert step contains a fail-open construct")
    if not re.search(r"\bexit\s+1\b", script):
        violations.append("assert step has no `exit 1` failure path")
    if '-eq 0' not in script:
        violations.append("assert step does not check for an empty file set")
    return violations


def _nc_check_hidden_file_proof(job: dict) -> list[str]:
    """Return violations for AC3 (empty list == compliant)."""
    loc = _nc_locate(job)
    steps = loc["steps"]
    if len(loc["upload"]) != 1 or len(loc["verify"]) != 1 or not loc["downloads"]:
        return ["upload/verify/download steps missing"]
    upload_idx, verify_idx = loc["upload"][0], loc["verify"][0]
    violations: list[str] = []
    upload_with = steps[upload_idx].get("with") or {}
    if str(upload_with.get("include-hidden-files")).lower() != "true":
        violations.append("upload lost include-hidden-files: true")
    if str(upload_with.get("if-no-files-found")) != "error":
        violations.append("upload lost if-no-files-found: error")
    upload_path = str(upload_with.get("path", ""))
    verify = steps[verify_idx]
    if _nc_normalize_if(verify.get("if")) != "always()":
        violations.append("assert step must have `if: ${{ always() }}`")
    if verify_idx < upload_idx or any(i > verify_idx for i in loc["downloads"]):
        violations.append("assert step must come after upload and every download")
    script = str(verify.get("run", ""))
    if ".vitest-attachments" in script:
        violations.append("assert step reads the local .vitest-attachments dir")
    download_paths = {str((steps[i].get("with") or {}).get("path", "")).strip() for i in loc["downloads"]}
    if len(download_paths) != 1 or "" in download_paths:
        violations.append(f"all downloads must share one non-empty path, got {sorted(download_paths)}")
        return violations
    (download_path,) = download_paths
    if ".vitest-attachments" in _nc_path_parts(download_path):
        violations.append("download path is under .vitest-attachments")
    for raw in upload_path.splitlines():
        if raw.strip() and _nc_paths_overlap(download_path, raw):
            violations.append(f"download path {download_path!r} overlaps upload path {raw.strip()!r}")
    base = download_path.rstrip("/")
    for kind in ("actual", "diff"):
        if f"{base}/**/*-{kind}-*.png" not in script:
            violations.append(f"assert step does not scan the download dir for *-{kind}-*.png")
    return violations


def test_component_vrt_negative_control_artifact_name_is_attempt_scoped():
    job = _nc_job(_load_workflow())
    assert _nc_check_artifact_name_is_attempt_scoped(job) == []

    def _step(j: dict, kind: str) -> list[dict]:
        if kind == "upload":
            return [s for s in j["steps"] if isinstance(s, dict) and s.get("id") == _NC_UPLOAD_ID]
        return [s for s in j["steps"] if isinstance(s, dict) and _nc_uses(s, f"actions/{kind}-artifact")]

    # mutated copy 1: static upload name
    m = copy.deepcopy(job)
    _step(m, "upload")[-1]["with"]["name"] = _NC_STATIC_NAME
    assert _nc_check_artifact_name_is_attempt_scoped(m)
    # mutated copy 2: static name on a single (retry) download
    m = copy.deepcopy(job)
    _step(m, "download")[-1]["with"]["name"] = _NC_STATIC_NAME
    assert _nc_check_artifact_name_is_attempt_scoped(m)
    # mutated copy 3: the first download uses a static name
    m = copy.deepcopy(job)
    _step(m, "download")[0]["with"]["name"] = _NC_STATIC_NAME
    assert _nc_check_artifact_name_is_attempt_scoped(m)
    # mutated copy 4: attempt id replaced by a non run_attempt expression
    m = copy.deepcopy(job)
    _step(m, "upload")[0]["with"]["name"] = f"{_NC_STATIC_NAME}-${{{{ github.run_id }}}}"
    assert _nc_check_artifact_name_is_attempt_scoped(m)
    # mutated copy 5: upload / download names diverge
    m = copy.deepcopy(job)
    _step(m, "download")[0]["with"]["name"] = f"{_NC_STATIC_NAME}-${{{{ github.run_attempt }}}}-x"
    assert _nc_check_artifact_name_is_attempt_scoped(m)


def test_component_vrt_negative_control_roundtrip_is_bounded():
    job = _nc_job(_load_workflow())
    assert _nc_check_roundtrip_is_bounded(job) == []

    steps_of = lambda j: j["steps"]  # noqa: E731
    sleeps = [s for s in steps_of(job) if isinstance(s, dict) and str(s.get("run", "")).startswith("sleep ")]
    downloads = [s for s in steps_of(job) if isinstance(s, dict) and _nc_uses(s, "actions/download-artifact")]
    verify = next(s for s in steps_of(job) if isinstance(s, dict) and s.get("id") == _NC_VERIFY_ID)

    def mutate(fn) -> list[str]:
        m = copy.deepcopy(job)
        fn(m)
        return _nc_check_roundtrip_is_bounded(m)

    def _dl(m: dict) -> list[dict]:
        return [s for s in m["steps"] if isinstance(s, dict) and _nc_uses(s, "actions/download-artifact")]

    def _sl(m: dict) -> list[dict]:
        return [s for s in m["steps"] if isinstance(s, dict) and str(s.get("run", "")).startswith("sleep ")]

    def _vf(m: dict) -> dict:
        return next(s for s in m["steps"] if isinstance(s, dict) and s.get("id") == _NC_VERIFY_ID)

    assert sleeps and downloads and verify

    # unbounded retry shapes
    assert mutate(lambda m: _sl(m)[0].__setitem__("run", "while true; do sleep 1; done"))
    assert mutate(lambda m: _sl(m)[0].__setitem__("run", "until [ -d negative-control-download ]; do sleep 1; done"))
    # non-literal / unparseable sleeps
    assert mutate(lambda m: _sl(m)[0].__setitem__("run", "sleep $((2 * 3))"))
    assert mutate(lambda m: _sl(m)[0].__setitem__("run", "sleep ${RETRY_SLEEP}"))
    assert mutate(lambda m: _sl(m)[0].__setitem__("run", "sleep 5 && true"))
    assert mutate(lambda m: _sl(m)[0].__setitem__("run", "sleep 5.5"))
    # total sleep above 30s
    assert mutate(lambda m: _sl(m)[0].__setitem__("run", "sleep 31"))
    # too many downloads (> 4)
    def add_download(m: dict) -> None:
        idx = max(
            i
            for i, s in enumerate(m["steps"])
            if isinstance(s, dict) and _nc_uses(s, "actions/download-artifact")
        )
        extra = copy.deepcopy(m["steps"][idx])
        extra["id"] = "download-negative-control-attachments-extra"
        extra["if"] = "${{ always() && steps.%s.outcome == 'failure' }}" % m["steps"][idx]["id"]
        for k in range(3):
            m["steps"].insert(idx + 1, copy.deepcopy(extra))
            m["steps"][idx + 1]["id"] = f"download-extra-{k}"
    assert mutate(add_download)
    # retry not gated on previous failure / gated on conclusion
    assert mutate(lambda m: _dl(m)[1].__setitem__("if", "${{ always() }}"))
    assert mutate(
        lambda m: _dl(m)[1].__setitem__(
            "if", "${{ always() && steps.%s.conclusion == 'failure' }}" % _dl(m)[0]["id"]
        )
    )
    # download without continue-on-error
    assert mutate(lambda m: _dl(m)[0].__delitem__("continue-on-error"))
    # assert step that tolerates an empty download directory
    assert mutate(lambda m: _vf(m).__setitem__("run", _vf(m)["run"].replace("exit 1", "exit 0")))
    assert mutate(lambda m: _vf(m).__setitem__("run", _vf(m)["run"] + "\ntrue || true\n"))
    assert mutate(lambda m: _vf(m).__setitem__("run", "set +e\n" + _vf(m)["run"]))
    assert mutate(lambda m: _vf(m).__setitem__("run", "exit 0\n" + _vf(m)["run"]))
    assert mutate(lambda m: _vf(m).__setitem__("run", "echo skipped"))
    # a loop in the assert step
    assert mutate(lambda m: _vf(m).__setitem__("run", "while true; do sleep 1; done\nexit 1"))


def test_component_vrt_negative_control_keeps_hidden_file_proof():
    job = _nc_job(_load_workflow())
    assert _nc_check_hidden_file_proof(job) == []

    def mutate(fn) -> list[str]:
        m = copy.deepcopy(job)
        fn(m)
        return _nc_check_hidden_file_proof(m)

    def _up(m: dict) -> dict:
        return next(s for s in m["steps"] if isinstance(s, dict) and s.get("id") == _NC_UPLOAD_ID)

    def _dl(m: dict) -> list[dict]:
        return [s for s in m["steps"] if isinstance(s, dict) and _nc_uses(s, "actions/download-artifact")]

    def _vf(m: dict) -> dict:
        return next(s for s in m["steps"] if isinstance(s, dict) and s.get("id") == _NC_VERIFY_ID)

    # hidden-file proof knobs
    assert mutate(lambda m: _up(m)["with"].__setitem__("include-hidden-files", False))
    assert mutate(lambda m: _up(m)["with"].__delitem__("include-hidden-files"))
    assert mutate(lambda m: _up(m)["with"].__setitem__("if-no-files-found", "warn"))
    assert mutate(lambda m: _vf(m).__setitem__("if", "${{ success() }}"))
    assert mutate(lambda m: _vf(m).__delitem__("if"))
    # local-dir-only assert (no download dir scan; reads .vitest-attachments)
    local_only = (
        "shopt -s globstar nullglob\n"
        "actual_files=(.vitest-attachments/**/*-actual-*.png)\n"
        "diff_files=(.vitest-attachments/**/*-diff-*.png)\n"
        '[ "${#actual_files[@]}" -eq 0 ] && exit 1\n'
        '[ "${#diff_files[@]}" -eq 0 ] && exit 1\n'
    )
    assert mutate(lambda m: _vf(m).__setitem__("run", local_only))
    # download path overlaps local attachments dir / upload path
    for bad in (
        ".vitest-attachments",
        ".vitest-attachments/download",
        ".vitest-attachments/tests/component/__negative_control__",
        ".vitest-attachments/tests/component/__negative_control__/sub",
        ".",
        "./",
    ):
        assert mutate(lambda m, bad=bad: [d["with"].__setitem__("path", bad) for d in _dl(m)]), bad
    # assert step before the downloads
    def move_assert_first(m: dict) -> None:
        verify = _vf(m)
        m["steps"].remove(verify)
        m["steps"].insert(next(i for i, s in enumerate(m["steps"]) if _nc_uses(s, "actions/download-artifact")), verify)
    assert mutate(move_assert_first)
    # a download path that differs from the scanned directory
    assert mutate(lambda m: [d["with"].__setitem__("path", "other-dir") for d in _dl(m)])


def _nc_run_assert_script(script: str, workdir: pathlib.Path) -> subprocess.CompletedProcess:
    # GitHub expressions are not evaluated locally; none are expected in this
    # script, but neutralise any so bash can run it. `shell: bash` in GitHub
    # Actions is `bash --noprofile --norc -e -o pipefail {0}`.
    script = re.sub(r"\$\{\{.*?\}\}", "EXPR", script)
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_component_vrt_negative_control_assert_script_behaviour(tmp_path):
    job = _nc_job(_load_workflow())
    verify = next(s for s in job["steps"] if isinstance(s, dict) and s.get("id") == _NC_VERIFY_ID)
    script = str(verify["run"])
    download = next(
        s for s in job["steps"] if isinstance(s, dict) and _nc_uses(s, "actions/download-artifact")
    )["with"]["path"]

    # missing download dir -> fail closed
    missing = tmp_path / "missing"
    missing.mkdir()
    assert _nc_run_assert_script(script, missing).returncode != 0

    # empty download dir -> fail closed
    empty = tmp_path / "empty"
    (empty / download).mkdir(parents=True)
    assert _nc_run_assert_script(script, empty).returncode != 0

    # only actual (no diff) -> fail closed
    partial = tmp_path / "partial"
    (partial / download / "sub").mkdir(parents=True)
    (partial / download / "sub" / "x-actual-1.png").write_bytes(b"png")
    assert _nc_run_assert_script(script, partial).returncode != 0

    # local .vitest-attachments populated but download dir empty -> still fail
    local_only = tmp_path / "local_only"
    (local_only / download).mkdir(parents=True)
    (local_only / ".vitest-attachments" / "t").mkdir(parents=True)
    (local_only / ".vitest-attachments" / "t" / "x-actual-1.png").write_bytes(b"png")
    (local_only / ".vitest-attachments" / "t" / "x-diff-1.png").write_bytes(b"png")
    assert _nc_run_assert_script(script, local_only).returncode != 0

    # valid download dir -> pass
    ok = tmp_path / "ok"
    (ok / download / "sub").mkdir(parents=True)
    (ok / download / "sub" / "x-actual-1.png").write_bytes(b"png")
    (ok / download / "sub" / "x-diff-1.png").write_bytes(b"png")
    result = _nc_run_assert_script(script, ok)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hidden_attachment_upload_proof=ok" in result.stdout
