"""
scripts/ci/tests/e2e_performance_benchmark/test_collect_e2e_performance_benchmark.py

Issue #2159 AC1/AC2/AC7/AC12: unit/integration tests for
scripts/ci/collect_e2e_performance_benchmark.py.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "collect_e2e_performance_benchmark.py"


def _load_collector():
    spec = importlib.util.spec_from_file_location("collect_e2e_performance_benchmark", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


collector = _load_collector()

BEFORE_SHA = "a" * 40
AFTER_SHA = "b" * 40


def _record(
    workflow_run_id: int,
    job: str = "e2e-core",
    head_sha: str = BEFORE_SHA,
    artifact_id: int | None = None,
    artifact_digest: str | None = None,
    conclusion: str = "success",
) -> dict:
    return {
        "workflow_run_id": workflow_run_id,
        "job": job,
        "head_sha": head_sha,
        "artifact_id": artifact_id if artifact_id is not None else workflow_run_id,
        "artifact_digest": artifact_digest or ("sha256:" + f"{workflow_run_id:064x}"),
        "conclusion": conclusion,
    }


def _full_job_set_records(count: int, head_sha: str, start_id: int = 1) -> list[dict]:
    records = []
    for i in range(count):
        run_id = start_id + i
        for job in ("e2e-core", "e2e-responsive-matrix", "e2e"):
            records.append(_record(run_id, job=job, head_sha=head_sha))
    return records


def test_fixed_sha_recorded():
    """GIVEN a before/after fixed 40-hex commit SHA WHEN a manifest is
    collected THEN both SHAs are recorded verbatim in the manifest
    (AC1)."""
    before_records = _full_job_set_records(1, BEFORE_SHA, start_id=1)
    after_records = _full_job_set_records(1, AFTER_SHA, start_id=101)

    manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA, AFTER_SHA, before_records, after_records, min_run_count=1
    )
    assert manifest["before_sha"] == BEFORE_SHA
    assert manifest["after_sha"] == AFTER_SHA
    assert manifest["schema"] == "e2e_performance_benchmark_manifest_v1"
    assert manifest["schema_version"] == 1


def test_fixed_sha_must_be_40_hex():
    """GIVEN a malformed before_sha WHEN collecting THEN an
    OperationalError is raised (fail-closed, not silently truncated/coerced)."""
    with pytest.raises(collector.OperationalError):
        collector.collect_benchmark_manifest("not-a-sha", AFTER_SHA, [], [])


def test_sample_identity_workflow_run_id():
    """GIVEN two records sharing the SAME workflow_run_id (simulating a
    rerun attempt) WHEN deduped THEN only ONE sample is counted -- rerun
    attempts never add an independent sample (AC2/P1-1)."""
    records = [
        _record(500, job="e2e-core", head_sha=BEFORE_SHA),
        _record(500, job="e2e-core", head_sha=BEFORE_SHA, artifact_id=999),  # rerun of the same run
        _record(501, job="e2e-core", head_sha=BEFORE_SHA),
    ]
    deduped = collector._dedupe_by_workflow_run_id(records)
    assert len(deduped) == 2
    assert {r["workflow_run_id"] for r in deduped} == {500, 501}


def test_sample_identity_20_run_cohort_deduped_correctly():
    """GIVEN 20 unique workflow_run_id samples PLUS 5 duplicate-run_id
    'rerun' records WHEN a manifest is collected with min_run_count=20
    THEN the arm is complete (run_count == 20, not 25)."""
    before_records = _full_job_set_records(20, BEFORE_SHA, start_id=1)
    # 5 rerun duplicates of the first 5 workflow_run_ids for e2e-core only.
    for run_id in range(1, 6):
        before_records.append(_record(run_id, job="e2e-core", head_sha=BEFORE_SHA, artifact_id=90000 + run_id))
    after_records = _full_job_set_records(20, AFTER_SHA, start_id=101)

    manifest = collector.collect_benchmark_manifest(BEFORE_SHA, AFTER_SHA, before_records, after_records)
    before_core = manifest["arms"]["before"]["jobs"]["e2e-core"]
    assert before_core["run_count"] == 20
    assert len(before_core["sample_workflow_run_ids"]) == 20
    assert manifest["arms"]["before"]["complete"] is True
    assert manifest["arms"]["after"]["complete"] is True


def test_artifact_id_digest_head_job_verified():
    """GIVEN a record with a missing artifact_digest, a mismatched
    head_sha, and a non-40-hex head_sha WHEN collected THEN each is
    reported as a distinct evidence error and NOT silently included in the
    cohort (AC7)."""
    good = _full_job_set_records(1, BEFORE_SHA, start_id=1)
    bad_digest = _record(50, job="e2e-core", head_sha=BEFORE_SHA, artifact_digest="not-a-digest")
    bad_head = _record(51, job="e2e-core", head_sha="c" * 40)  # does not match BEFORE_SHA
    malformed_head = _record(52, job="e2e-core", head_sha="short")

    manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA,
        AFTER_SHA,
        good + [bad_digest, bad_head, malformed_head],
        _full_job_set_records(1, AFTER_SHA, start_id=101),
        min_run_count=1,
    )
    core_ids = {r["workflow_run_id"] for r in manifest["arms"]["before"]["jobs"]["e2e-core"]["runs"]}
    assert 50 not in core_ids
    assert 51 not in core_ids
    assert 52 not in core_ids

    errors_by_run = {}
    for err in manifest["evidence_errors"]:
        if err["arm"] != "before":
            continue
        errors_by_run[err["detail"]] = err["reason"]
    assert any("missing_or_invalid_artifact_digest" in detail for detail in errors_by_run)
    assert any("head_sha_mismatch" in detail for detail in errors_by_run)
    assert any("missing_or_invalid_head_sha" in detail for detail in errors_by_run)


def test_manifest_conforms_to_schema():
    """GIVEN a complete 20-run-per-job manifest WHEN validated against
    schemas/e2e_performance_benchmark_manifest_v1.schema.json THEN it
    passes structural validation (AC7)."""
    before_records = _full_job_set_records(20, BEFORE_SHA, start_id=1)
    after_records = _full_job_set_records(20, AFTER_SHA, start_id=101)
    manifest = collector.collect_benchmark_manifest(BEFORE_SHA, AFTER_SHA, before_records, after_records)
    # Must not raise.
    collector._validate_against_schema(manifest)


def test_manifest_incomplete_when_below_min_run_count():
    """GIVEN fewer than min_run_count samples for one job WHEN collected
    THEN that arm's `complete` is False (and the overall manifest is still
    written -- CLI exit code 2, not silently marked complete)."""
    before_records = _full_job_set_records(5, BEFORE_SHA, start_id=1)
    after_records = _full_job_set_records(20, AFTER_SHA, start_id=101)
    manifest = collector.collect_benchmark_manifest(BEFORE_SHA, AFTER_SHA, before_records, after_records)
    assert manifest["arms"]["before"]["complete"] is False
    assert manifest["arms"]["after"]["complete"] is True


def test_cli_exit_code_reflects_completeness(tmp_path):
    """GIVEN a complete cohort passed via CLI WHEN main() runs THEN exit 0
    and a schema-conformant manifest file is written; GIVEN an incomplete
    cohort THEN exit 2."""
    before_path = tmp_path / "before_runs.json"
    after_path = tmp_path / "after_runs.json"
    output_path = tmp_path / "manifest.json"

    before_path.write_text(json.dumps(_full_job_set_records(20, BEFORE_SHA, start_id=1)), encoding="utf-8")
    after_path.write_text(json.dumps(_full_job_set_records(20, AFTER_SHA, start_id=101)), encoding="utf-8")

    exit_code = collector.main(
        [
            "--before-sha",
            BEFORE_SHA,
            "--after-sha",
            AFTER_SHA,
            "--before-runs-json",
            str(before_path),
            "--after-runs-json",
            str(after_path),
            "--output",
            str(output_path),
        ]
    )
    assert exit_code == 0
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["arms"]["before"]["complete"] is True

    # Now an incomplete cohort.
    before_path.write_text(json.dumps(_full_job_set_records(3, BEFORE_SHA, start_id=1)), encoding="utf-8")
    exit_code_incomplete = collector.main(
        [
            "--before-sha",
            BEFORE_SHA,
            "--after-sha",
            AFTER_SHA,
            "--before-runs-json",
            str(before_path),
            "--after-runs-json",
            str(after_path),
            "--output",
            str(output_path),
        ]
    )
    assert exit_code_incomplete == 2


def test_cli_operational_failure_on_unparseable_input(tmp_path):
    """GIVEN a malformed JSON input file WHEN main() runs THEN exit 3
    (operational failure), not a crash or a silently-empty manifest."""
    before_path = tmp_path / "before_runs.json"
    after_path = tmp_path / "after_runs.json"
    output_path = tmp_path / "manifest.json"

    before_path.write_text("{not valid json", encoding="utf-8")
    after_path.write_text("[]", encoding="utf-8")

    exit_code = collector.main(
        [
            "--before-sha",
            BEFORE_SHA,
            "--after-sha",
            AFTER_SHA,
            "--before-runs-json",
            str(before_path),
            "--after-runs-json",
            str(after_path),
            "--output",
            str(output_path),
        ]
    )
    assert exit_code == 3
    assert not output_path.exists()
