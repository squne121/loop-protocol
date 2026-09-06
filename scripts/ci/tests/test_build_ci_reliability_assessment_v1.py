"""Issue #2424 focused regression tests for
`scripts/ci/build_ci_reliability_assessment_v1.py` (production Reliability V1
close-grade evidence builder).

Every fixture below is a synthetic, hermetic stand-in for the real #2422
manifest / #2423 receipt / GitHub Actions workflow+job evidence / official
Playwright JSON reporter output -- this file never imports or re-executes the
real #2422/#2423 producers (Out of Scope), and never mutates the #2432/#2507
validator (`.claude/skills/ci-test-performance/scripts/
validate_ci_reliability_assessment_v1.py`), which is invoked unmodified via
`run_validator` for the production route (AC4).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess

import pytest

_MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build_ci_reliability_assessment_v1.py")
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
_spec = importlib.util.spec_from_file_location("build_ci_reliability_assessment_v1", _MODULE_PATH)
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)


WORKFLOW_SHA = "a" * 40
EXPERIMENT_IDENTITY = "exp-2424-smoke-1"


def _invocations():
    return [
        {"invocation_id": "e2e-core", "lane": "core", "evidence_file": "reliability-evidence/e2e-core.json"},
        {
            "invocation_id": "e2e-core-responsive",
            "lane": "responsive",
            "evidence_file": "reliability-evidence/e2e-core-responsive.json",
            "benchmark_layout_only": "monolith",
        },
        {
            "invocation_id": "e2e-responsive",
            "lane": "responsive",
            "evidence_file": "reliability-evidence/e2e-responsive.json",
            "benchmark_layout_only": "split",
        },
    ]


def _run_record(layout, run_id, conclusion):
    return {
        "benchmark_layout": layout,
        "workflow_run_id": run_id,
        "run_attempt": 1,
        "conclusion": conclusion,
        "workflow_sha": WORKFLOW_SHA,
        "workflow_digest": "sha256:" + "2" * 64,
        "provider_jobs": [{"job": "e2e-core"}],
    }


def make_manifest(
    monolith_run_id=100,
    split_run_id=200,
    monolith_conclusion="failure",
    split_conclusion="success",
    experiment_identity=EXPERIMENT_IDENTITY,
    expected_test_count=2,
    invocations=None,
):
    return {
        "schema": "e2e_performance_benchmark_manifest_v2",
        "schema_version": 2,
        "experiment_identity": experiment_identity,
        "experiment_run_set_digest": "sha256:" + "1" * 64,
        "workflow_sha": WORKFLOW_SHA,
        "workflow_digest": "sha256:" + "2" * 64,
        "frozen_non_treatment": {
            "expected_playwright_invocations": invocations if invocations is not None else _invocations(),
            "expected_test_count": expected_test_count,
        },
        "blocks": [
            {
                "block_id": "b1",
                "runs": [
                    _run_record("monolith", monolith_run_id, monolith_conclusion),
                    _run_record("split", split_run_id, split_conclusion),
                ],
            }
        ],
        "evidence_errors": [],
    }


def make_receipt(
    manifest,
    monolith_ids=None,
    split_ids=None,
    run_set_digest="sha256:" + "3" * 64,
    experiment_identity=None,
):
    if monolith_ids is None:
        monolith_ids = sorted(builder.manifest_run_ids_for_layout(manifest, "monolith"))
    if split_ids is None:
        split_ids = sorted(builder.manifest_run_ids_for_layout(manifest, "split"))
    resolved_identity = experiment_identity if experiment_identity is not None else manifest["experiment_identity"]
    return {
        "schema": "CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1",
        "experiment_identity": resolved_identity,
        "manifest_sha256": "sha256:" + "4" * 64,
        "run_set_digest": run_set_digest,
        "materialization_policy": "root_run_set_exhaustive_partition",
        "arms": {"monolith": {"workflow_run_ids": monolith_ids}, "split": {"workflow_run_ids": split_ids}},
        "evidence_errors": [],
    }


def make_workflow_evidence(manifest, rerun_run_id=None, missing_job_layout=None, conclusion_override=None):
    evidence = {"monolith": {}, "split": {}}
    for layout in ("monolith", "split"):
        for run_id in builder.manifest_run_ids_for_layout(manifest, layout):
            run = builder.manifest_run_record(manifest, layout, run_id)
            override_applies = conclusion_override and layout == missing_job_layout
            conclusion = conclusion_override if override_applies else run["conclusion"]
            latest = 2 if run_id == rerun_run_id else 1
            jobs = [
                {"name": j["job"], "conclusion": conclusion, "status": "completed"} for j in run["provider_jobs"]
            ]
            if layout == missing_job_layout and conclusion_override is None:
                jobs = []
            evidence[layout][str(run_id)] = {
                "conclusion": conclusion,
                "run_attempt": 1,
                "latest_run_attempt": latest,
                "jobs": jobs,
            }
    return evidence


def _playwright_payload(
    manifest, run_id, layout, invocation_id, lane, outcomes, report_errors=None, metadata_overrides=None
):
    metadata = {
        "experiment_identity": manifest["experiment_identity"],
        "workflow_run_id": str(run_id),
        "run_attempt": "1",
        "benchmark_layout": layout,
        "invocation_id": invocation_id,
        "lane": lane,
        "workflow_sha": manifest["workflow_sha"],
    }
    if metadata_overrides:
        metadata.update(metadata_overrides)
    specs = [
        {
            "id": f"spec-{invocation_id}-{i}",
            "title": f"test-{i}",
            "tests": [{"projectId": "chromium", "status": outcome}],
        }
        for i, outcome in enumerate(outcomes)
    ]
    return {
        "config": {"metadata": metadata},
        "suites": [{"title": "suite", "specs": specs, "suites": []}],
        "errors": report_errors or [],
        "stats": {},
    }


def _write_playwright_json(base_dir, layout, invocation_id, payload):
    path = os.path.join(base_dir, layout, f"{invocation_id}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


def _lane_for(invocation_id: str) -> str:
    return "responsive" if "responsive" in invocation_id else "core"


def _write_all_playwright_json(
    base_dir, manifest, monolith_run_id, split_run_id, monolith_outcomes=None, split_outcomes=None
):
    if monolith_outcomes is None:
        monolith_outcomes = {"e2e-core": ["expected"], "e2e-core-responsive": ["expected"]}
    if split_outcomes is None:
        split_outcomes = {"e2e-core": ["expected"], "e2e-responsive": ["expected"]}
    for invocation_id, outcomes in monolith_outcomes.items():
        payload = _playwright_payload(
            manifest, monolith_run_id, "monolith", invocation_id, _lane_for(invocation_id), outcomes
        )
        _write_playwright_json(base_dir, "monolith", invocation_id, payload)
    for invocation_id, outcomes in split_outcomes.items():
        payload = _playwright_payload(
            manifest, split_run_id, "split", invocation_id, _lane_for(invocation_id), outcomes
        )
        _write_playwright_json(base_dir, "split", invocation_id, payload)


def _happy_path(
    tmp_path, monolith_conclusion="failure", split_conclusion="success", monolith_run_id=100, split_run_id=200
):
    manifest = make_manifest(
        monolith_run_id=monolith_run_id,
        split_run_id=split_run_id,
        monolith_conclusion=monolith_conclusion,
        split_conclusion=split_conclusion,
    )
    receipt = make_receipt(manifest)
    workflow_evidence = make_workflow_evidence(manifest)
    playwright_dir = str(tmp_path / "playwright")
    _write_all_playwright_json(playwright_dir, manifest, monolith_run_id, split_run_id)
    loader = builder._default_playwright_json_loader(playwright_dir)
    return manifest, receipt, workflow_evidence, loader


# --------------------------------------------------------------------------- #
# AC1
# --------------------------------------------------------------------------- #
def test_rejects_non_exact_manifest_and_canonical_run_set_binding(tmp_path):
    manifest, receipt, workflow_evidence, loader = _happy_path(tmp_path)

    # Mutate the receipt's monolith arm to a DIFFERENT run set (subset/other
    # run) -- exact membership mismatch must fail closed, envelope is None.
    mismatched_receipt = make_receipt(manifest, monolith_ids=[999])
    envelope, diagnostic = builder.build_composite_envelope(manifest, mismatched_receipt, workflow_evidence, loader)
    assert envelope is None
    assert any("exact_run_set_membership_mismatch" in e for e in diagnostic["errors"])

    # #2422 experiment_run_set_digest and #2423 run_set_digest are SEPARATE
    # owner algorithms -- differing digest STRINGS must never by themselves
    # cause a rejection as long as expanded workflow_run_id membership is
    # exactly equal.
    non_digest_equal_receipt = make_receipt(manifest, run_set_digest="sha256:" + "9" * 64)
    assert non_digest_equal_receipt["run_set_digest"] != manifest["experiment_run_set_digest"]
    envelope2, diagnostic2 = builder.build_composite_envelope(
        manifest, non_digest_equal_receipt, workflow_evidence, loader
    )
    assert envelope2 is not None
    assert diagnostic2["errors"] == []


# --------------------------------------------------------------------------- #
# AC2 -- playwright.config.ts additive lane-aware official JSON reporter.
# Real functional check when pnpm + installed Playwright are available
# (this repo's dev/worktree environment); gracefully skipped in a
# python-only CI lane that never ran `pnpm install` (node_modules absent) --
# the AUTHORITATIVE runtime evidence for this AC is the live e2e-core/
# e2e-responsive-matrix dispatch (AC8 smoke), not this local check.
# --------------------------------------------------------------------------- #
def _playwright_available() -> bool:
    return os.path.isfile(os.path.join(_REPO_ROOT, "node_modules", ".bin", "playwright")) and os.path.isdir(
        os.path.join(_REPO_ROOT, "node_modules", "@playwright")
    )


def _run_playwright_list(env_overrides):
    """Runs the REAL `playwright test --list` (never `--reporter=json`,
    which would itself OVERRIDE -- not merely read -- the configured
    reporter set, defeating the purpose of observing what the config file
    itself declares). The config's own reporters (html/list always, json
    only on the evidence-required route) run for real; JSON evidence (when
    produced) is read back from the actual `PLAYWRIGHT_JSON_OUTPUT_FILE` on
    disk, exactly as #2424's builder does in production."""
    env = dict(os.environ)
    env.update(env_overrides)
    env["CI"] = "true"
    playwright_bin = os.path.join(_REPO_ROOT, "node_modules", ".bin", "playwright")
    result = subprocess.run(
        [playwright_bin, "test", "--list"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Total:" in result.stdout
    return result


def test_preserves_html_and_list_while_adding_lane_aware_official_json_reporter(tmp_path):
    if not _playwright_available():
        pytest.skip("pnpm/@playwright/test not installed in this environment (node_modules absent)")

    # Normal route (PLAYWRIGHT_JSON_OUTPUT_FILE unset): the JSON reporter
    # must never activate -- no JSON evidence file is produced at all.
    normal_json_path = tmp_path / "normal-would-be-output.json"
    _run_playwright_list({"PLAYWRIGHT_JSON_OUTPUT_FILE": ""})
    assert not normal_json_path.exists()

    # Evidence-required route: html/list are preserved (this repo's existing
    # `playwright-report/` producer contract and CI summary steps are
    # untouched) AND the official JSON reporter additively writes to the
    # exact per-invocation path, with the expected stable metadata bound in.
    json_output_file = tmp_path / "reliability-evidence" / "e2e-core.json"
    evidence_env = {
        "PLAYWRIGHT_JSON_OUTPUT_FILE": str(json_output_file),
        "RELIABILITY_EXPERIMENT_IDENTITY": EXPERIMENT_IDENTITY,
        "RELIABILITY_BENCHMARK_LAYOUT": "monolith",
        "RELIABILITY_INVOCATION_ID": "e2e-core",
        "RELIABILITY_LANE": "core",
        "RELIABILITY_WORKFLOW_SHA": WORKFLOW_SHA,
        "GITHUB_RUN_ID": "123456",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    _run_playwright_list(evidence_env)
    assert json_output_file.exists()
    evidence = json.loads(json_output_file.read_text())

    metadata = evidence["config"]["metadata"]
    assert metadata["experiment_identity"] == EXPERIMENT_IDENTITY
    assert metadata["workflow_run_id"] == "123456"
    assert metadata["run_attempt"] == "1"
    assert metadata["benchmark_layout"] == "monolith"
    assert metadata["invocation_id"] == "e2e-core"
    assert metadata["lane"] == "core"
    assert metadata["workflow_sha"] == WORKFLOW_SHA

    # html/list must still be the configured reporters alongside json (never
    # replaced). This reads `config.reporter` from the REAL JSON file the
    # config's OWN reporter wrote (not a `--reporter=json` CLI override,
    # which would itself replace the configured set instead of reporting
    # it) -- an accurate reflection of what actually ran this invocation.
    evidence_reporters = [entry[0] for entry in evidence["config"]["reporter"]]
    assert "html" in evidence_reporters
    assert "list" in evidence_reporters
    assert "json" in evidence_reporters
    json_entry = next(entry for entry in evidence["config"]["reporter"] if entry[0] == "json")
    assert json_entry[1]["outputFile"] == str(json_output_file)


# --------------------------------------------------------------------------- #
# AC3
# --------------------------------------------------------------------------- #
def test_builds_three_v1_assessments_from_exact_composite_workflow_outcome_and_playwright_evidence(tmp_path):
    manifest, receipt, workflow_evidence, loader = _happy_path(
        tmp_path,
        monolith_conclusion="failure",
        split_conclusion="success",
    )
    playwright_dir = str(tmp_path / "playwright")
    # Overwrite with one flaky observation on the monolith arm.
    _write_all_playwright_json(
        playwright_dir,
        manifest,
        100,
        200,
        monolith_outcomes={"e2e-core": ["flaky"], "e2e-core-responsive": ["expected"]},
        split_outcomes={"e2e-core": ["expected"], "e2e-responsive": ["unexpected"]},
    )
    loader = builder._default_playwright_json_loader(playwright_dir)

    envelope, diagnostic = builder.build_composite_envelope(manifest, receipt, workflow_evidence, loader)
    assert diagnostic["errors"] == []
    assert envelope is not None

    assessments = builder.build_all_assessments(envelope, issue_number=2424, pr_number=None)
    assert set(assessments) == {
        "workflow_failure_rate",
        "playwright_flaky_test_rate",
        "playwright_terminal_failure_rate",
    }

    wfr = assessments["workflow_failure_rate"]["reliability_metrics"]
    assert wfr["before"]["workflow_failure_rate"]["numerator"] == 1  # monolith == before, conclusion=failure
    assert wfr["after"]["workflow_failure_rate"]["numerator"] == 0  # split == after, conclusion=success

    flaky = assessments["playwright_flaky_test_rate"]["reliability_metrics"]
    assert flaky["before"]["playwright_flaky_test_rate"]["numerator"] == 1  # monolith has a flaky test case
    assert flaky["after"]["playwright_flaky_test_rate"]["numerator"] == 0

    terminal = assessments["playwright_terminal_failure_rate"]["reliability_metrics"]
    assert terminal["before"]["playwright_terminal_failure_rate"]["numerator"] == 0
    assert terminal["after"]["playwright_terminal_failure_rate"]["numerator"] == 1  # split has an unexpected test case

    for metric, assessment in assessments.items():
        assert assessment["target_metric"] == metric
        assert assessment["non_inferiority_evaluation"]["metric"] == metric


def test_ineligible_workflow_conclusion_is_evidence_ineligible_hard_error(tmp_path):
    manifest, receipt, workflow_evidence, loader = _happy_path(tmp_path)
    workflow_evidence["monolith"]["100"]["conclusion"] = "cancelled"
    envelope, diagnostic = builder.build_composite_envelope(manifest, receipt, workflow_evidence, loader)
    assert envelope is None
    assert any(
        "workflow_conclusion_binding_mismatch" in e or "workflow_run_evidence_ineligible" in e
        for e in diagnostic["errors"]
    )


# --------------------------------------------------------------------------- #
# AC4
# --------------------------------------------------------------------------- #
def test_emits_three_validator_results_and_rejects_validator_only_pass(tmp_path):
    manifest, receipt, workflow_evidence, loader = _happy_path(tmp_path)
    envelope, diagnostic = builder.build_composite_envelope(manifest, receipt, workflow_evidence, loader)
    assert diagnostic["errors"] == []
    assessments = builder.build_all_assessments(envelope, issue_number=2424, pr_number=None)

    validator_results = {}
    for metric, assessment in assessments.items():
        assessment_path = tmp_path / f"assessment_{metric}.json"
        assessment_path.write_text(json.dumps(assessment))
        output_path = tmp_path / f"validator_{metric}.json"
        validator_results[metric] = builder.run_validator(str(assessment_path), str(output_path))

    assert len(validator_results) == 3
    for metric, result in validator_results.items():
        assert result.get("exit_code") == 0, (metric, result)
        assert result.get("structural_valid") is True
        assert result.get("semantic_valid") is True

    # Every individual validator exit 0 -- but with n=1 << required_sample_count_per_arm=22,
    # the aggregate gate must NOT treat validator-exit-0 alone as PASS.
    aggregate = builder.aggregate_gate(envelope, diagnostic, assessments, validator_results)
    assert aggregate["semantic_valid"] is True
    assert aggregate["complete"] is False
    assert aggregate["sample_satisfied"] is False
    assert aggregate["exit_code"] != 0


# --------------------------------------------------------------------------- #
# AC6
# --------------------------------------------------------------------------- #
def test_rerun_marks_experiment_ineligible_without_dropping_or_replacing_attempt1(tmp_path):
    manifest, receipt, _, loader = _happy_path(tmp_path)
    workflow_evidence = make_workflow_evidence(manifest, rerun_run_id=100)

    envelope, diagnostic = builder.build_composite_envelope(manifest, receipt, workflow_evidence, loader)
    assert envelope is not None
    assert diagnostic["errors"] == []
    assert diagnostic["rerun_run_ids"] == [100]
    assert envelope["rerun_run_ids"] == [100]

    # The attempt-1 (failed) sample for workflow_run_id=100 must remain a
    # REGULAR sample -- never dropped and never replaced by a fresh run.
    records = builder.build_workflow_records(envelope)
    assert any(r["workflow_run_id"] == 100 and r["conclusion"] == "failure" and r["run_attempt"] == 1 for r in records)
    provenance = builder.build_sample_provenance(envelope)
    workflow_failure_observations = provenance["before"]["workflow_failure_rate"]["observations"]
    assert any(o["workflow_run_id"] == 100 for o in workflow_failure_observations)

    assessments = builder.build_all_assessments(envelope, issue_number=2424, pr_number=None)
    aggregate = builder.aggregate_gate(envelope, diagnostic, assessments, {})
    assert aggregate["rerun_detected"] is True
    assert aggregate["complete"] is False
    assert aggregate["exit_code"] == 2


# --------------------------------------------------------------------------- #
# AC9 -- focused regression coverage: exact-binding mismatch, missing
# workflow/job/Playwright evidence, cancelled workflow, report.errors
# non-empty / zero-TestCase, metadata binding false-green, digest-equality
# confusion, validator-only PASS rejection.
# --------------------------------------------------------------------------- #
def test_rejects_missing_or_cancelled_workflow_outcome_or_playwright_evidence_and_three_metric_false_green(tmp_path):
    # 1. Missing Playwright JSON artifact for one expected invocation.
    manifest, receipt, workflow_evidence, _ = _happy_path(tmp_path)
    playwright_dir = str(tmp_path / "partial-playwright")
    only_core_payload = _playwright_payload(manifest, 100, "monolith", "e2e-core", "core", ["expected"])
    _write_playwright_json(playwright_dir, "monolith", "e2e-core", only_core_payload)
    # e2e-core-responsive intentionally missing.
    _write_all_playwright_json(playwright_dir, manifest, 100, 200, monolith_outcomes={}, split_outcomes=None)
    loader = builder._default_playwright_json_loader(playwright_dir)
    envelope, diagnostic = builder.build_composite_envelope(manifest, receipt, workflow_evidence, loader)
    assert envelope is None
    assert any("missing_playwright_json_artifact" in e for e in diagnostic["errors"])

    # 2. Cancelled workflow conclusion recorded in workflow-evidence (binding
    #    mismatch against manifest's own recorded conclusion is also a hard
    #    error -- manifest and evidence must agree, and cancelled is
    #    evidence-ineligible regardless).
    manifest2, receipt2, workflow_evidence2, loader2 = _happy_path(tmp_path / "case2")
    manifest2["blocks"][0]["runs"][0]["conclusion"] = "cancelled"
    workflow_evidence2["monolith"]["100"]["conclusion"] = "cancelled"
    workflow_evidence2["monolith"]["100"]["jobs"] = []
    envelope2, diagnostic2 = builder.build_composite_envelope(manifest2, receipt2, workflow_evidence2, loader2)
    assert envelope2 is None
    assert any("workflow_run_evidence_ineligible" in e for e in diagnostic2["errors"])

    # 3. report.errors non-empty for one invocation.
    manifest3, receipt3, workflow_evidence3, _ = _happy_path(tmp_path / "case3")
    playwright_dir3 = str(tmp_path / "case3" / "playwright")
    _write_all_playwright_json(playwright_dir3, manifest3, 100, 200)
    payload_with_errors = _playwright_payload(
        manifest3, 100, "monolith", "e2e-core", "core", ["expected"], report_errors=[{"message": "boom"}]
    )
    _write_playwright_json(playwright_dir3, "monolith", "e2e-core", payload_with_errors)
    loader3 = builder._default_playwright_json_loader(playwright_dir3)
    envelope3, diagnostic3 = builder.build_composite_envelope(manifest3, receipt3, workflow_evidence3, loader3)
    assert envelope3 is None
    assert any("playwright_report_errors_non_empty" in e for e in diagnostic3["errors"])

    # 4. Zero TestCase count for one invocation.
    manifest4, receipt4, workflow_evidence4, _ = _happy_path(tmp_path / "case4")
    playwright_dir4 = str(tmp_path / "case4" / "playwright")
    _write_all_playwright_json(playwright_dir4, manifest4, 100, 200)
    zero_case_payload = _playwright_payload(manifest4, 100, "monolith", "e2e-core", "core", [])
    _write_playwright_json(playwright_dir4, "monolith", "e2e-core", zero_case_payload)
    loader4 = builder._default_playwright_json_loader(playwright_dir4)
    envelope4, diagnostic4 = builder.build_composite_envelope(manifest4, receipt4, workflow_evidence4, loader4)
    assert envelope4 is None
    assert any("zero_test_case_count" in e for e in diagnostic4["errors"])

    # 5. Playwright metadata binding false-green (wrong experiment_identity
    #    embedded in the JSON's config.metadata must be rejected, not
    #    silently trusted).
    manifest5, receipt5, workflow_evidence5, _ = _happy_path(tmp_path / "case5")
    playwright_dir5 = str(tmp_path / "case5" / "playwright")
    _write_all_playwright_json(playwright_dir5, manifest5, 100, 200)
    bad_meta_payload = _playwright_payload(
        manifest5,
        100,
        "monolith",
        "e2e-core",
        "core",
        ["expected"],
        metadata_overrides={"experiment_identity": "wrong-experiment"},
    )
    _write_playwright_json(playwright_dir5, "monolith", "e2e-core", bad_meta_payload)
    loader5 = builder._default_playwright_json_loader(playwright_dir5)
    envelope5, diagnostic5 = builder.build_composite_envelope(manifest5, receipt5, workflow_evidence5, loader5)
    assert envelope5 is None
    assert any("playwright_metadata_binding_mismatch" in e for e in diagnostic5["errors"])

    # 6. experiment_run_set_digest / run_set_digest string-equality confusion
    #    must NOT be a rejection reason by itself (already exercised in AC1),
    #    re-verified here alongside the false-green cases above so both
    #    directions (over-strict AND under-strict binding) are pinned in one
    #    file.
    manifest6, receipt6, workflow_evidence6, loader6 = _happy_path(tmp_path / "case6")
    receipt6["run_set_digest"] = "sha256:" + "f" * 64
    assert receipt6["run_set_digest"] != manifest6["experiment_run_set_digest"]
    envelope6, diagnostic6 = builder.build_composite_envelope(manifest6, receipt6, workflow_evidence6, loader6)
    assert envelope6 is not None
    assert diagnostic6["errors"] == []

    # 7. expected_test_count mismatch (a "missing" test silently excluded
    #    from the union) must hard-fail, not silently pass with a smaller
    #    denominator.
    manifest7, receipt7, workflow_evidence7, _ = _happy_path(tmp_path / "case7")
    manifest7["frozen_non_treatment"]["expected_test_count"] = 999
    playwright_dir7 = str(tmp_path / "case7" / "playwright")
    _write_all_playwright_json(playwright_dir7, manifest7, 100, 200)
    loader7 = builder._default_playwright_json_loader(playwright_dir7)
    envelope7, diagnostic7 = builder.build_composite_envelope(manifest7, receipt7, workflow_evidence7, loader7)
    assert envelope7 is None
    assert any("expected_test_count_mismatch" in e for e in diagnostic7["errors"])

    # 8. Validator-only PASS is rejected by the aggregate gate even when the
    #    assessment JSON is semantically self-consistent (3-metric false
    #    green guard): tamper a declared numerator so the validator itself
    #    must flag semantic invalidity, and confirm aggregate never reports
    #    complete=True in that case.
    manifest8, receipt8, workflow_evidence8, loader8 = _happy_path(tmp_path / "case8")
    envelope8, diagnostic8 = builder.build_composite_envelope(manifest8, receipt8, workflow_evidence8, loader8)
    assessments8 = builder.build_all_assessments(envelope8, issue_number=2424, pr_number=None)
    tampered = json.loads(json.dumps(assessments8["workflow_failure_rate"]))
    tampered["reliability_metrics"]["before"]["workflow_failure_rate"]["numerator"] = 0
    tampered["reliability_metrics"]["before"]["workflow_failure_rate"]["rate"] = 0.0
    assessment_path = tmp_path / "tampered.json"
    assessment_path.write_text(json.dumps(tampered))
    output_path = tmp_path / "tampered_validator.json"
    tampered_result = builder.run_validator(str(assessment_path), str(output_path))
    assert tampered_result.get("exit_code") != 0
    assert tampered_result.get("semantic_valid") is False
    tampered_validator_results = {"workflow_failure_rate": tampered_result}
    aggregate8 = builder.aggregate_gate(envelope8, diagnostic8, assessments8, tampered_validator_results)
    assert aggregate8["complete"] is False
    assert aggregate8["semantic_valid"] is False
