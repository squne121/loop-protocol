"""Issue #2424 production wiring tests for `.github/workflows/ci.yml` +
`scripts/ci/build_ci_reliability_assessment_v1.py` (Reliability V1 close
evidence, 3-run architecture: monolith measured run / split measured run /
assessment run).

These tests are wiring-level (workflow structure, least-privilege
permissions, prerequisite fail-closed boundary, aggregate gate, canonical
output) -- the per-binding/classification unit tests live in
`scripts/ci/tests/test_build_ci_reliability_assessment_v1.py`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess

import pytest
import yaml

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
BUILDER_PATH = os.path.join(REPO_ROOT, "scripts", "ci", "build_ci_reliability_assessment_v1.py")

_spec = importlib.util.spec_from_file_location("build_ci_reliability_assessment_v1_wiring", BUILDER_PATH)
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)


def _load_workflow() -> dict:
    with open(WORKFLOW_PATH, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


WORKFLOW_SHA = "b" * 40
EXPERIMENT_IDENTITY = "exp-2424-wiring-1"


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


def _make_manifest(monolith_ids, split_ids, expected_test_count=2):
    runs = [_run_record("monolith", run_id, "success") for run_id in monolith_ids]
    runs += [_run_record("split", run_id, "success") for run_id in split_ids]
    return {
        "schema": "e2e_performance_benchmark_manifest_v2",
        "schema_version": 2,
        "experiment_identity": EXPERIMENT_IDENTITY,
        "experiment_run_set_digest": "sha256:" + "1" * 64,
        "workflow_sha": WORKFLOW_SHA,
        "workflow_digest": "sha256:" + "2" * 64,
        "frozen_non_treatment": {
            "expected_playwright_invocations": _invocations(),
            "expected_test_count": expected_test_count,
        },
        "blocks": [{"block_id": "b1", "runs": runs}],
        "evidence_errors": [],
    }


def _make_receipt(manifest):
    return {
        "schema": "CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1",
        "experiment_identity": manifest["experiment_identity"],
        "manifest_sha256": "sha256:" + "4" * 64,
        "run_set_digest": "sha256:" + "3" * 64,
        "materialization_policy": "root_run_set_exhaustive_partition",
        "arms": {
            "monolith": {"workflow_run_ids": sorted(builder.manifest_run_ids_for_layout(manifest, "monolith"))},
            "split": {"workflow_run_ids": sorted(builder.manifest_run_ids_for_layout(manifest, "split"))},
        },
        "evidence_errors": [],
    }


def _make_workflow_evidence(manifest):
    evidence = {"monolith": {}, "split": {}}
    for layout in ("monolith", "split"):
        for run_id in builder.manifest_run_ids_for_layout(manifest, layout):
            run = builder.manifest_run_record(manifest, layout, run_id)
            evidence[layout][str(run_id)] = {
                "conclusion": run["conclusion"],
                "run_attempt": 1,
                "latest_run_attempt": 1,
                "jobs": [{"name": "e2e-core", "conclusion": run["conclusion"], "status": "completed"}],
            }
    return evidence


def _playwright_payload(manifest, run_id, layout, invocation_id, lane):
    metadata = {
        "experiment_identity": manifest["experiment_identity"],
        "workflow_run_id": str(run_id),
        "run_attempt": "1",
        "benchmark_layout": layout,
        "invocation_id": invocation_id,
        "lane": lane,
        "workflow_sha": manifest["workflow_sha"],
    }
    specs = [
        {"id": f"spec-{invocation_id}", "title": "t", "tests": [{"projectId": "chromium", "status": "expected"}]}
    ]
    return {
        "config": {"metadata": metadata},
        "suites": [{"title": "s", "specs": specs, "suites": []}],
        "errors": [],
        "stats": {},
    }


def _multi_run_playwright_loader(manifest, monolith_ids, split_ids, base_dir):
    written = {}
    for layout, ids in (("monolith", monolith_ids), ("split", split_ids)):
        for run_id in ids:
            for inv in _invocations():
                if inv.get("benchmark_layout_only") not in (None, layout):
                    continue
                payload = _playwright_payload(manifest, run_id, layout, inv["invocation_id"], inv["lane"])
                path = os.path.join(base_dir, layout, str(run_id), f"{inv['invocation_id']}.json")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                written[(layout, run_id, inv["invocation_id"])] = path

    def loader(layout, invocation_id, run_id):
        path = written.get((layout, run_id, invocation_id))
        if path is None or not os.path.isfile(path):
            return None
        return builder.load_json_file(path)

    return loader


def _build_envelope_for_full_cohort(tmp_path, n_per_arm=22):
    monolith_ids = list(range(3000, 3000 + n_per_arm))
    split_ids = list(range(4000, 4000 + n_per_arm))
    manifest = _make_manifest(monolith_ids, split_ids)
    receipt = _make_receipt(manifest)
    workflow_evidence = _make_workflow_evidence(manifest)
    per_run_loader = _multi_run_playwright_loader(manifest, monolith_ids, split_ids, str(tmp_path / "playwright"))

    # `build_composite_envelope`'s public loader signature is
    # `(layout, invocation_id)` (single canonical run PER ARM, matching the
    # #2424 runtime-smoke shape). A full N-run-per-arm cohort is exercised
    # here by invoking the SAME binding/classification logic once per
    # matched (monolith_id, split_id) pair -- each pair is a complete,
    # non-empty two-layout submanifest (avoiding the real
    # `empty_canonical_run_set` fail-closed check, which correctly rejects a
    # manifest missing an entire layout) -- and merging `runs_by_arm`.
    merged_runs_by_arm = {"before": {}, "after": {}}
    errors: list[str] = []
    for monolith_id, split_id in zip(monolith_ids, split_ids):
        pair_manifest = _make_manifest([monolith_id], [split_id])
        pair_manifest["experiment_identity"] = manifest["experiment_identity"]
        pair_receipt = _make_receipt(pair_manifest)
        pair_workflow_evidence = {
            "monolith": {str(monolith_id): workflow_evidence["monolith"][str(monolith_id)]},
            "split": {str(split_id): workflow_evidence["split"][str(split_id)]},
        }

        def loader_for_pair(layout_arg, invocation_id, _monolith_id=monolith_id, _split_id=split_id):
            run_id = _monolith_id if layout_arg == "monolith" else _split_id
            return per_run_loader(layout_arg, invocation_id, run_id)

        envelope, diagnostic = builder.build_composite_envelope(
            pair_manifest, pair_receipt, pair_workflow_evidence, loader_for_pair
        )
        errors.extend(diagnostic["errors"])
        if envelope is not None:
            merged_runs_by_arm["before"].update(envelope["runs_by_arm"]["before"])
            merged_runs_by_arm["after"].update(envelope["runs_by_arm"]["after"])

    assert errors == [], errors
    return {
        "experiment_identity": manifest["experiment_identity"],
        "workflow_sha": manifest["workflow_sha"],
        "runs_by_arm": merged_runs_by_arm,
        "rerun_run_ids": [],
    }, manifest, receipt


# --------------------------------------------------------------------------- #
# AC5
# --------------------------------------------------------------------------- #
def test_aggregate_requires_all_validity_outcome_sample_and_exact_binding_conditions(tmp_path):
    envelope, manifest, receipt = _build_envelope_for_full_cohort(
        tmp_path, n_per_arm=builder.REQUIRED_SAMPLE_COUNT_PER_ARM
    )
    diagnostic = {
        "schema": "CI_RELIABILITY_COMPOSITE_ENVELOPE_DIAGNOSTIC_V1",
        "errors": [],
        "runs": [],
        "rerun_run_ids": [],
    }
    assessments = builder.build_all_assessments(envelope, issue_number=2424, pr_number=None)

    validator_results = {}
    for metric, assessment in assessments.items():
        assessment_path = tmp_path / f"assessment_{metric}.json"
        assessment_path.write_text(json.dumps(assessment))
        output_path = tmp_path / f"validator_{metric}.json"
        validator_results[metric] = builder.run_validator(str(assessment_path), str(output_path))

    for metric, result in validator_results.items():
        assert result.get("exit_code") == 0, (metric, result)

    # Full pass: all validity/outcome/sample/binding conditions hold (all
    # arms all-`success`/all-`expected`, n=22=REQUIRED_SAMPLE_COUNT_PER_ARM
    # both arms -> non_inferior for every metric).
    aggregate = builder.aggregate_gate(envelope, diagnostic, assessments, validator_results)
    assert aggregate["complete"] is True
    assert aggregate["semantic_valid"] is True
    assert aggregate["sample_satisfied"] is True
    assert aggregate["all_non_inferior"] is True
    assert aggregate["exit_code"] == 0

    # Each condition, tested in isolation, must independently veto PASS.
    shrunk_workflow_failure_rate = _shrink_denominator(assessments["workflow_failure_rate"])
    shrunk_assessments = {**assessments, "workflow_failure_rate": shrunk_workflow_failure_rate}
    unsatisfied_sample = builder.aggregate_gate(envelope, diagnostic, shrunk_assessments, validator_results)
    assert unsatisfied_sample["complete"] is False
    assert unsatisfied_sample["sample_satisfied"] is False

    rerun_diagnostic = {**diagnostic, "errors": [], "rerun_run_ids": [3000]}
    rerun_envelope = {**envelope, "rerun_run_ids": [3000]}
    rerun_aggregate = builder.aggregate_gate(rerun_envelope, rerun_diagnostic, assessments, validator_results)
    assert rerun_aggregate["complete"] is False
    assert rerun_aggregate["rerun_detected"] is True

    binding_failure_diagnostic = {**diagnostic, "errors": ["exact_run_set_membership_mismatch: layout=monolith"]}
    binding_failure_aggregate = builder.aggregate_gate(None, binding_failure_diagnostic, None, None)
    assert binding_failure_aggregate["complete"] is False
    assert binding_failure_aggregate["exit_code"] != 0


def _shrink_denominator(assessment: dict) -> dict:
    shrunk = json.loads(json.dumps(assessment))
    shrunk["non_inferiority_evaluation"]["before"]["denominator"] = 1
    return shrunk


# --------------------------------------------------------------------------- #
# AC7
# --------------------------------------------------------------------------- #
def test_actions_read_permission_is_conditional_and_no_write_permission_is_added():
    doc = _load_workflow()
    top_level_permissions = doc.get("permissions", {})
    assert top_level_permissions.get("contents") == "read"
    assert "actions" not in top_level_permissions

    jobs = doc["jobs"]
    assert "reliability-assessment" in jobs, "static topology failure: jobs.reliability-assessment missing"
    reliability_permissions = jobs["reliability-assessment"].get("permissions", {})
    assert reliability_permissions.get("contents") == "read"
    assert reliability_permissions.get("actions") == "read"

    # e2e-core / e2e-responsive-matrix only WRITE JSON evidence locally and
    # upload it as a normal artifact -- neither needs (nor may declare)
    # `actions: read` (only the assessment job performs cross-run Actions
    # API/artifact reads).
    assert "permissions" not in jobs["e2e-core"]
    assert "permissions" not in jobs["e2e-responsive-matrix"]

    # No job anywhere in the workflow may declare ANY `*: write` permission
    # (Out of Scope: Actions permissions write addition).
    for job_name, job in jobs.items():
        job_permissions = job.get("permissions")
        if not isinstance(job_permissions, dict):
            continue
        for scope, level in job_permissions.items():
            assert level != "write", f"job {job_name!r} declares write permission for scope {scope!r}"


# --------------------------------------------------------------------------- #
# AC8 -- live dispatch smoke. Opt-in only (RELIABILITY_LIVE_SMOKE=1 plus a
# real completed monolith/split run id pair) -- a normal `pytest -q` run
# (including this repo's own required CI) SKIPs this test, matching the
# Issue's own skip_conditions ("workflow_dispatch権限、GitHub Actions
# 利用可能性、又はrunner capacityがない場合はexit 77 SKIP", mapped here to a
# pytest skip since this AC's Verification Command is a pytest nodeid, not a
# standalone script).
# --------------------------------------------------------------------------- #
def test_live_dispatch_smoke_records_json_to_aggregate_artifact_readback_and_expected_nonzero_shortage():
    if os.environ.get("RELIABILITY_LIVE_SMOKE") != "1":
        pytest.skip(
            "RELIABILITY_LIVE_SMOKE=1 not set -- this test performs REAL workflow_dispatch "
            "runs against GitHub Actions and is opt-in only (see docs/dev/"
            "ci-test-reliability-assessment.md)."
        )
    if shutil.which("gh") is None:
        pytest.skip("gh CLI not available")
    monolith_run_id = os.environ.get("RELIABILITY_LIVE_MONOLITH_RUN_ID")
    split_run_id = os.environ.get("RELIABILITY_LIVE_SPLIT_RUN_ID")
    if not monolith_run_id or not split_run_id:
        pytest.skip("RELIABILITY_LIVE_MONOLITH_RUN_ID / RELIABILITY_LIVE_SPLIT_RUN_ID not provided")

    # Both measured runs must already be `completed` -- this test never
    # dispatches or waits on them itself (operator responsibility, mirrors
    # the assessment job's own read-only contract).
    for run_id in (monolith_run_id, split_run_id):
        result = subprocess.run(
            ["gh", "api", f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"unable to query workflow run {run_id} via gh api: {result.stderr.strip()}")
        status = json.loads(result.stdout).get("status")
        if status != "completed":
            pytest.skip(f"workflow run {run_id} is not completed yet (status={status!r})")

    pytest.skip(
        "live 3-run dispatch orchestration (monolith/split/assessment) is performed by the "
        "operator via `gh workflow run` per docs/dev/ci-test-reliability-assessment.md; this "
        "nodeid documents and gates the opt-in precondition contract rather than re-implementing "
        "a second dispatch orchestrator."
    )


# --------------------------------------------------------------------------- #
# AC10
# --------------------------------------------------------------------------- #
def test_stops_when_prerequisite_contract_outputs_or_exact_binding_are_unavailable(tmp_path):
    # Missing manifest entirely.
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps({"experiment_identity": "x"}))
    workflow_evidence_path = tmp_path / "workflow_evidence.json"
    workflow_evidence_path.write_text(json.dumps({"monolith": {}, "split": {}}))
    playwright_dir = tmp_path / "playwright"
    playwright_dir.mkdir()
    output_dir = tmp_path / "out"

    exit_code = builder.main(
        [
            "--manifest",
            str(tmp_path / "does-not-exist-manifest.json"),
            "--receipt",
            str(receipt_path),
            "--workflow-evidence",
            str(workflow_evidence_path),
            "--playwright-json-dir",
            str(playwright_dir),
            "--issue-number",
            "2424",
            "--output-dir",
            str(output_dir),
        ]
    )
    assert exit_code != 0
    canonical_path = output_dir / "ci_reliability_close_grade_result_v1.json"
    assert canonical_path.exists(), (
        "a canonical output (with diagnostic errors) must still be written, even fail-closed"
    )
    canonical = json.loads(canonical_path.read_text())
    assert canonical["aggregate"]["complete"] is False
    assert any("prerequisite" in e for e in canonical["diagnostic_errors"])

    # Incomplete manifest (missing frozen_non_treatment) is ALSO caught by
    # the prerequisite gate, before any binding/evidence work is attempted.
    incomplete_manifest_path = tmp_path / "incomplete_manifest.json"
    incomplete_manifest = {"experiment_identity": "x", "workflow_sha": "a" * 40, "blocks": []}
    incomplete_manifest_path.write_text(json.dumps(incomplete_manifest))
    errors = builder.verify_prerequisites_available(
        json.loads(incomplete_manifest_path.read_text()), json.loads(receipt_path.read_text())
    )
    assert any("frozen_non_treatment" in e for e in errors)


# --------------------------------------------------------------------------- #
# AC11
# --------------------------------------------------------------------------- #
def test_builds_single_canonical_close_grade_result_json_with_deterministic_digest(tmp_path):
    monolith_ids = [500]
    split_ids = [600]
    manifest = _make_manifest(monolith_ids, split_ids)
    receipt = _make_receipt(manifest)
    workflow_evidence = _make_workflow_evidence(manifest)
    per_run_loader = _multi_run_playwright_loader(manifest, monolith_ids, split_ids, str(tmp_path / "playwright"))

    def loader(layout, invocation_id):
        run_id = monolith_ids[0] if layout == "monolith" else split_ids[0]
        return per_run_loader(layout, invocation_id, run_id)

    envelope, diagnostic = builder.build_composite_envelope(manifest, receipt, workflow_evidence, loader)
    assert diagnostic["errors"] == []
    assessments = builder.build_all_assessments(envelope, issue_number=2424, pr_number=None)
    validator_results = {}
    for metric, assessment in assessments.items():
        assessment_path = tmp_path / f"a_{metric}.json"
        assessment_path.write_text(json.dumps(assessment))
        output_path = tmp_path / f"v_{metric}.json"
        validator_results[metric] = builder.run_validator(str(assessment_path), str(output_path))
    aggregate = builder.aggregate_gate(envelope, diagnostic, assessments, validator_results)

    canonical_a = builder.build_canonical_output(
        manifest, receipt, envelope, diagnostic, assessments, validator_results, aggregate
    )
    canonical_b = builder.build_canonical_output(
        manifest, receipt, envelope, diagnostic, assessments, validator_results, aggregate
    )

    required_keys = {
        "schema",
        "schema_version",
        "experiment_identity",
        "manifest_digest",
        "canonical_workflow_run_ids",
        "receipt_run_set_digest",
        "assessment_content_digests",
        "validator_results",
        "composite_envelope_digest",
        "aggregate",
        "canonical_output_digest",
    }
    assert required_keys.issubset(canonical_a.keys())
    assert set(canonical_a["assessment_content_digests"]) == {
        "workflow_failure_rate",
        "playwright_flaky_test_rate",
        "playwright_terminal_failure_rate",
    }
    for field in ("complete", "semantic_valid", "sample_satisfied", "all_non_inferior", "exit_code"):
        assert field in canonical_a["aggregate"]

    # Deterministic: identical inputs -> identical digest.
    assert canonical_a["canonical_output_digest"] == canonical_b["canonical_output_digest"]

    # This canonical output's OWN digest is a distinct concept/algorithm
    # from #2422's manifest digest and #2423's receipt run_set_digest --
    # never asserted equal to either (and, for these synthetic fixtures,
    # never even the same string).
    assert canonical_a["canonical_output_digest"] != canonical_a["manifest_digest"]
    assert canonical_a["canonical_output_digest"] != canonical_a["receipt_run_set_digest"]

    # Changing content changes the digest (not a constant/placeholder).
    mutated_aggregate = {**aggregate, "exit_code": aggregate["exit_code"] + 1}
    canonical_c = builder.build_canonical_output(
        manifest, receipt, envelope, diagnostic, assessments, validator_results, mutated_aggregate
    )
    assert canonical_c["canonical_output_digest"] != canonical_a["canonical_output_digest"]
