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
import tempfile

import pytest
import yaml

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
BUILDER_PATH = os.path.join(REPO_ROOT, "scripts", "ci", "build_ci_reliability_assessment_v1.py")
SCHEMA_PATH = os.path.join(REPO_ROOT, "schemas", "e2e_performance_benchmark_manifest_v2.schema.json")

_spec = importlib.util.spec_from_file_location("build_ci_reliability_assessment_v1_wiring", BUILDER_PATH)
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)


def _load_workflow() -> dict:
    with open(WORKFLOW_PATH, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


WORKFLOW_SHA = "b" * 40
EXPERIMENT_IDENTITY = "exp-2424-wiring-1"


def _invocations():
    """Issue #2424 Finding 1 fix_delta (OWNER REQUEST_CHANGES issuecomment-
    5556542041): common logical invocation identity across BOTH
    `benchmark_layout` arms -- only `provider_placement[layout]` (the
    PHYSICAL provider job) varies. Schema-conformant (no `benchmark_layout_
    only`, which `ExpectedPlaywrightInvocation`'s `unevaluatedProperties:
    false` rejects)."""
    return [
        {
            "invocation_id": "e2e-core",
            "lane": "core",
            "provider_placement": {"monolith": "e2e-core", "split": "e2e-core"},
            "evidence_file": "reliability-evidence/e2e-core.json",
        },
        {
            "invocation_id": "e2e-responsive",
            "lane": "responsive",
            "provider_placement": {"monolith": "e2e-core", "split": "e2e-responsive-matrix"},
            "evidence_file": "reliability-evidence/e2e-responsive.json",
        },
    ]


def _assert_manifest_matches_real_schema(manifest: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        schema = json.load(handle)
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(manifest), key=lambda e: list(e.path))
    assert not errors, [e.message for e in errors]


def _provider_jobs_for_layout(layout):
    job_names = ["e2e-core"] if layout == "monolith" else ["e2e-core", "e2e-responsive-matrix"]
    return [
        {
            "job": name,
            "workflow_job_id": 900000 + i,
            "conclusion": "success",
            "exact_runner_image": {"name": "ubuntu-24.04", "version": "20260901.1.0"},
        }
        for i, name in enumerate(job_names)
    ]


def _run_record(layout, run_id, conclusion):
    return {
        "benchmark_layout": layout,
        "workflow_run_id": run_id,
        "run_attempt": 1,
        "conclusion": conclusion,
        "workflow_sha": WORKFLOW_SHA,
        "workflow_digest": "sha256:" + "2" * 64,
        "provider_jobs": _provider_jobs_for_layout(layout),
    }


def _real_experiment_run_set_digest(blocks):
    module = builder._load_collect_e2e_performance_benchmark_module()
    return module.compute_experiment_run_set_digest(module._run_identity_tuples_from_blocks(blocks))


def _real_run_set_digest(monolith_ids, split_ids):
    """Issue #2424 Finding 3 residual-gap closure: computed via the REAL
    #2423 owner function `compute_run_set_digest` (never re-implemented) so
    `_make_receipt()`'s `run_set_digest` passes independent owner-algorithm
    re-verification (`recompute_receipt_run_set_digest`)."""
    module = builder._load_performance_gate_test_module()
    return module.compute_run_set_digest(monolith_ids, split_ids)


def _make_manifest(monolith_ids, split_ids, expected_test_count=2):
    """Issue #2424 Finding 1 fix_delta: one BLOCK per matched
    `(monolith_id, split_id)` pair, each with EXACTLY 2 runs (`[monolith,
    split]`, in that order) -- the real #2422 schema's `Block.runs` is
    `minItems: 2, maxItems: 2`. The pre-fix_delta version of this helper put
    ALL `2*N` runs into a SINGLE block, which is not just schema-
    nonconformant but is also what forced `_build_envelope_for_full_cohort`
    (Finding 2) to split into per-pair sub-manifests and merge envelopes by
    hand instead of calling the production builder once."""
    assert len(monolith_ids) == len(split_ids)
    blocks = [
        {
            "block_id": f"b{i}",
            "runs": [_run_record("monolith", m_id, "success"), _run_record("split", s_id, "success")],
        }
        for i, (m_id, s_id) in enumerate(zip(monolith_ids, split_ids))
    ]
    manifest = {
        "schema": "e2e_performance_benchmark_manifest_v2",
        "schema_version": 2,
        "generated_at": "2026-09-01T00:00:00Z",
        "experiment_identity": EXPERIMENT_IDENTITY,
        "experiment_run_set_digest": _real_experiment_run_set_digest(blocks),
        "frozen_source_sha": "d" * 40,
        "workflow_sha": WORKFLOW_SHA,
        "workflow_digest": "sha256:" + "2" * 64,
        "frozen_non_treatment": {
            "test_inventory_digest": "sha256:" + "5" * 64,
            "expected_playwright_invocations": _invocations(),
            "expected_test_count": expected_test_count,
            "lockfile_hash": "sha256:" + "6" * 64,
            "toolchain_digest": "sha256:" + "7" * 64,
        },
        "blocks": blocks,
        "evidence_errors": [],
    }
    _assert_manifest_matches_real_schema(manifest)
    return manifest


def _make_receipt(manifest):
    monolith_ids = sorted(builder.manifest_run_ids_for_layout(manifest, "monolith"))
    split_ids = sorted(builder.manifest_run_ids_for_layout(manifest, "split"))
    return {
        "schema": "CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1",
        "experiment_identity": manifest["experiment_identity"],
        "manifest_sha256": "sha256:" + "4" * 64,
        # Issue #2424 Finding 3 residual-gap closure: real #2423
        # owner-algorithm value (not a placeholder) so this fixture passes
        # independent owner-algorithm re-verification.
        "run_set_digest": _real_run_set_digest(monolith_ids, split_ids),
        "materialization_policy": "root_run_set_exhaustive_partition",
        "arms": {
            "monolith": {"workflow_run_ids": monolith_ids},
            "split": {"workflow_run_ids": split_ids},
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
                "jobs": [
                    {"name": j["job"], "conclusion": run["conclusion"], "status": "completed"}
                    for j in run["provider_jobs"]
                ],
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
    """Issue #2424 Finding 2 fix_delta: writes REAL per-(layout, run_id,
    invocation_id) files and returns a loader matching the PRODUCTION
    `(layout, workflow_run_id, invocation_id)` signature directly -- no
    `benchmark_layout_only` filtering (removed per Finding 1: every
    invocation is expected under every arm)."""
    written = {}
    for layout, ids in (("monolith", monolith_ids), ("split", split_ids)):
        for run_id in ids:
            for inv in _invocations():
                payload = _playwright_payload(manifest, run_id, layout, inv["invocation_id"], inv["lane"])
                path = os.path.join(base_dir, layout, str(run_id), f"{inv['invocation_id']}.json")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                written[(layout, run_id, inv["invocation_id"])] = path

    def loader(layout, workflow_run_id, invocation_id):
        path = written.get((layout, workflow_run_id, invocation_id))
        if path is None or not os.path.isfile(path):
            return None
        return builder.load_json_file(path)

    return loader


def _build_envelope_for_full_cohort(tmp_path, n_per_arm=22):
    """Issue #2424 Finding 2 fix_delta (OWNER REQUEST_CHANGES issuecomment-
    5556542041): calls the REAL production `build_composite_envelope` ONCE
    against a single full N-run/arm manifest/receipt/workflow-evidence,
    using the production `(layout, workflow_run_id, invocation_id)` loader
    signature directly -- no per-pair manifest splitting, no test-side
    `runs_by_arm` merge. This is the exact regression coverage the OWNER
    required: 2 run/arm and 22 run/arm both go through the SAME CLI path,
    never combined inside the test."""
    monolith_ids = list(range(3000, 3000 + n_per_arm))
    split_ids = list(range(4000, 4000 + n_per_arm))
    manifest = _make_manifest(monolith_ids, split_ids)
    receipt = _make_receipt(manifest)
    workflow_evidence = _make_workflow_evidence(manifest)
    loader = _multi_run_playwright_loader(manifest, monolith_ids, split_ids, str(tmp_path / "playwright"))

    envelope, diagnostic = builder.build_composite_envelope(manifest, receipt, workflow_evidence, loader)
    assert diagnostic["errors"] == [], diagnostic["errors"]
    assert envelope is not None
    assert set(envelope["runs_by_arm"]["before"]) == set(monolith_ids)
    assert set(envelope["runs_by_arm"]["after"]) == set(split_ids)
    return envelope, manifest, receipt


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
# real completed monolith/split run id pair, PLUS the operator-materialized
# #2422 manifest / #2423 receipt for that pair) -- a normal `pytest -q` run
# (including this repo's own required CI) SKIPs this test, matching the
# Issue's own skip_conditions ("workflow_dispatch権限、GitHub Actions
# 利用可能性、又はrunner capacityがない場合はexit 77 SKIP", mapped here to a
# pytest skip since this AC's Verification Command is a pytest nodeid, not a
# standalone script).
#
# Issue #2424 Finding AC8 fix_delta (OWNER REQUEST_CHANGES issuecomment-
# 5556542041): the pre-fix_delta version of this test ALWAYS reached an
# unconditional `pytest.skip()` at the end, even when every precondition
# (env vars, gh CLI, run completion) was satisfied -- it never actually
# performed the readback its own name promises. This version performs the
# REAL artifact readback + canonical-output assertions (Finding AC8's
# Option A: "既存の completed run IDs / assessment artifact を入力し、
# artifact readback と canonical assertions を実際に実行する lightweight
# test") once the operator ALSO supplies the already-materialized manifest/
# receipt via `RELIABILITY_LIVE_MANIFEST_PATH`/`RELIABILITY_LIVE_RECEIPT_
# PATH` -- this test itself never dispatches or waits on the monolith/split
# measured runs (that remains the operator/workflow-owner's own
# responsibility, matching the assessment job's own read-only contract; no
# second dispatch orchestrator is implemented here).
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

    manifest_path = os.environ.get("RELIABILITY_LIVE_MANIFEST_PATH")
    receipt_path = os.environ.get("RELIABILITY_LIVE_RECEIPT_PATH")
    if not manifest_path or not receipt_path:
        pytest.skip(
            "RELIABILITY_LIVE_MANIFEST_PATH / RELIABILITY_LIVE_RECEIPT_PATH not provided -- "
            "dispatching the monolith/split measured runs is an operator/workflow-owner "
            "responsibility (docs/dev/ci-test-reliability-assessment.md); once the operator "
            "has ALSO materialized the #2422 manifest and #2423 receipt for this already-"
            "completed run pair (offline, via their own producers), this test performs the "
            "REAL artifact readback + canonical-output assertions below."
        )

    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"unable to determine repo via gh: {result.stderr.strip()}")
    repo = result.stdout.strip()

    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    with open(receipt_path, encoding="utf-8") as handle:
        receipt = json.load(handle)

    def gh_api(endpoint):
        api_result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, check=False)
        if api_result.returncode != 0:
            pytest.skip(f"gh api {endpoint} failed: {api_result.stderr.strip()}")
        return json.loads(api_result.stdout)

    def record_for(run_id):
        latest = gh_api(f"repos/{repo}/actions/runs/{run_id}")
        attempt1 = gh_api(f"repos/{repo}/actions/runs/{run_id}/attempts/1")
        jobs = gh_api(f"repos/{repo}/actions/runs/{run_id}/attempts/1/jobs")
        return {
            "conclusion": attempt1.get("conclusion"),
            "run_attempt": attempt1.get("run_attempt", 1),
            "latest_run_attempt": latest.get("run_attempt", 1),
            "jobs": [
                {"name": job.get("name"), "conclusion": job.get("conclusion"), "status": job.get("status")}
                for job in jobs.get("jobs", [])
            ],
        }

    arms = receipt.get("arms", {})
    workflow_evidence = {
        "monolith": {str(rid): record_for(rid) for rid in arms.get("monolith", {}).get("workflow_run_ids", [])},
        "split": {str(rid): record_for(rid) for rid in arms.get("split", {}).get("workflow_run_ids", [])},
    }

    with tempfile.TemporaryDirectory(prefix="reliability-live-smoke-") as tmp_dir:
        input_dir = os.path.join(tmp_dir, "input")
        os.makedirs(input_dir, exist_ok=True)
        workflow_evidence_path = os.path.join(input_dir, "workflow_evidence.json")
        with open(workflow_evidence_path, "w", encoding="utf-8") as handle:
            json.dump(workflow_evidence, handle)

        invocation_ids = [
            inv["invocation_id"]
            for inv in manifest.get("frozen_non_treatment", {}).get("expected_playwright_invocations", [])
        ]
        playwright_dir = os.path.join(input_dir, "playwright")
        for layout in ("monolith", "split"):
            for run_id in arms.get(layout, {}).get("workflow_run_ids", []):
                for invocation_id in invocation_ids:
                    name = f"ci-reliability-{run_id}-a1-{invocation_id}"
                    dest = os.path.join(playwright_dir, layout, str(run_id))
                    os.makedirs(dest, exist_ok=True)
                    download_result = subprocess.run(
                        ["gh", "run", "download", str(run_id), "-n", name, "--dir", dest],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if download_result.returncode != 0:
                        pytest.skip(
                            f"artifact {name!r} not downloadable for run {run_id}: "
                            f"{download_result.stderr.strip()}"
                        )

        manifest_path_copy = os.path.join(input_dir, "manifest.json")
        receipt_path_copy = os.path.join(input_dir, "receipt.json")
        shutil.copy(manifest_path, manifest_path_copy)
        shutil.copy(receipt_path, receipt_path_copy)

        output_dir = os.path.join(tmp_dir, "output")
        exit_code = builder.main(
            [
                "--manifest",
                manifest_path_copy,
                "--receipt",
                receipt_path_copy,
                "--workflow-evidence",
                workflow_evidence_path,
                "--playwright-json-dir",
                playwright_dir,
                "--issue-number",
                "2424",
                "--output-dir",
                output_dir,
            ]
        )

        canonical_path = os.path.join(output_dir, "ci_reliability_close_grade_result_v1.json")
        assert os.path.isfile(canonical_path), "canonical output must be written even on fail-closed/shortage exit"
        with open(canonical_path, encoding="utf-8") as handle:
            canonical = json.load(handle)
        assert canonical["schema"] == "CI_RELIABILITY_CLOSE_GRADE_RESULT_V1"

        aggregate_path = os.path.join(output_dir, "aggregate_result.json")
        if os.path.isfile(aggregate_path):
            with open(aggregate_path, encoding="utf-8") as handle:
                aggregate = json.load(handle)
            # Issue #2424 Runtime Verification Applicability fallback_policy:
            # full sample is NOT required for this smoke -- a 1(or few)-run/
            # arm cohort producing `sample_satisfied: False` (n <
            # REQUIRED_SAMPLE_COUNT_PER_ARM=22) is the EXPECTED, non-
            # fabricated result; this assertion never coerces a shortage
            # into a fabricated PASS.
            if aggregate.get("sample_satisfied") is False:
                assert exit_code != 0
            else:
                assert exit_code == 0
        else:
            # No `aggregate_result.json` means the prerequisite gate itself
            # rejected the (manifest, receipt) pair before any binding
            # work started -- still a valid, non-fabricated outcome.
            assert exit_code != 0
            assert any("prerequisite" in e for e in canonical.get("diagnostic_errors", []))


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
    loader = _multi_run_playwright_loader(manifest, monolith_ids, split_ids, str(tmp_path / "playwright"))

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

    # Issue #2424 Finding 5 fix_delta: `invocation_artifacts` provenance,
    # populated from an artifact index (never fabricated).
    artifact_index = {
        "ci-reliability-500-a1-e2e-core": {"id": 1, "digest": "sha256:" + "a" * 64},
        # run 500 is `monolith` -- its `e2e-responsive` invocation uploads
        # under the `e2e-core-responsive` artifact-name suffix (collision-
        # avoidance with split's `e2e-responsive-matrix` job).
        "ci-reliability-500-a1-e2e-core-responsive": {"id": 2, "digest": "sha256:" + "b" * 64},
        "ci-reliability-600-a1-e2e-core": {"id": 3, "digest": "sha256:" + "c" * 64},
        "ci-reliability-600-a1-e2e-responsive": {"id": 4, "digest": "sha256:" + "d" * 64},
    }
    invocation_artifacts = builder.build_invocation_artifacts(envelope, manifest, artifact_index)
    assert len(invocation_artifacts) == 4

    canonical_a = builder.build_canonical_output(
        manifest, receipt, envelope, diagnostic, assessments, validator_results, aggregate, invocation_artifacts
    )
    canonical_b = builder.build_canonical_output(
        manifest, receipt, envelope, diagnostic, assessments, validator_results, aggregate, invocation_artifacts
    )
    assert canonical_a["invocation_artifacts"] == invocation_artifacts

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
