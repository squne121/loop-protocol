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
    workflow_digest: str = "workflow-digest-fixture-v1",
) -> dict:
    return {
        "workflow_run_id": workflow_run_id,
        "job": job,
        "head_sha": head_sha,
        "artifact_id": artifact_id if artifact_id is not None else workflow_run_id,
        "artifact_digest": artifact_digest or ("sha256:" + f"{workflow_run_id:064x}"),
        "conclusion": conclusion,
        "workflow_digest": workflow_digest,
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
        BEFORE_SHA,
        AFTER_SHA,
        before_records,
        after_records,
        job_names=("e2e-core", "e2e-responsive-matrix", "e2e"),
        min_run_count=1,
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

    manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA,
        AFTER_SHA,
        before_records,
        after_records,
        job_names=("e2e-core", "e2e-responsive-matrix", "e2e"),
    )
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
        job_names=("e2e-core", "e2e-responsive-matrix", "e2e"),
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
    manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA,
        AFTER_SHA,
        before_records,
        after_records,
        job_names=("e2e-core", "e2e-responsive-matrix", "e2e"),
    )
    # Must not raise.
    collector._validate_against_schema(manifest)


def test_manifest_incomplete_when_below_min_run_count():
    """GIVEN fewer than min_run_count samples for one job WHEN collected
    THEN that arm's `complete` is False (and the overall manifest is still
    written -- CLI exit code 2, not silently marked complete)."""
    before_records = _full_job_set_records(5, BEFORE_SHA, start_id=1)
    after_records = _full_job_set_records(20, AFTER_SHA, start_id=101)
    manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA,
        AFTER_SHA,
        before_records,
        after_records,
        job_names=("e2e-core", "e2e-responsive-matrix", "e2e"),
    )
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


# --------------------------------------------------------------------------- #
# #2159 P0-4 (fix_delta after adversarial review issuecomment-5295659213):
# arm-specific job topology + non-successful conclusion exclusion.
# --------------------------------------------------------------------------- #
def test_default_topology_is_arm_specific_not_symmetric():
    """GIVEN the default (no explicit job_names) topology WHEN a manifest is
    collected THEN the before arm only tracks the pre-split `e2e` job and
    the after arm only tracks the post-split `e2e-core`/`e2e-responsive-matrix`
    jobs -- NOT the same 3-job set applied symmetrically to both arms."""
    before_records = [_record(i, job="e2e", head_sha=BEFORE_SHA) for i in range(1, 21)]
    after_records = []
    for i in range(20):
        after_records.append(_record(100 + i, job="e2e-core", head_sha=AFTER_SHA))
        after_records.append(_record(100 + i, job="e2e-responsive-matrix", head_sha=AFTER_SHA))

    manifest = collector.collect_benchmark_manifest(BEFORE_SHA, AFTER_SHA, before_records, after_records)

    assert set(manifest["arms"]["before"]["jobs"].keys()) == {"e2e"}
    assert set(manifest["arms"]["after"]["jobs"].keys()) == {"e2e-core", "e2e-responsive-matrix"}
    assert manifest["arms"]["before"]["topology"] == "pre_split"
    assert manifest["arms"]["after"]["topology"] == "post_split"
    assert manifest["arms"]["before"]["complete"] is True
    assert manifest["arms"]["after"]["complete"] is True


def test_before_arm_split_job_contamination_does_not_count_toward_pre_split_completeness():
    """GIVEN a before-arm input record set that ALSO contains post-split
    `e2e-core`/`e2e-responsive-matrix` records (a topology-contamination
    attack) WHEN collected under the default arm-specific topology THEN
    those contaminating records are silently out-of-scope for the before
    arm (not tracked, not counted), and completeness is judged purely on
    real `e2e` samples."""
    before_records = [_record(i, job="e2e", head_sha=BEFORE_SHA) for i in range(1, 6)]
    # Contamination: split-job records injected into the before arm's input.
    before_records += [_record(1000 + i, job="e2e-core", head_sha=BEFORE_SHA) for i in range(20)]
    after_records = []
    for i in range(20):
        after_records.append(_record(2000 + i, job="e2e-core", head_sha=AFTER_SHA))
        after_records.append(_record(2000 + i, job="e2e-responsive-matrix", head_sha=AFTER_SHA))

    manifest = collector.collect_benchmark_manifest(BEFORE_SHA, AFTER_SHA, before_records, after_records)
    assert set(manifest["arms"]["before"]["jobs"].keys()) == {"e2e"}
    assert manifest["arms"]["before"]["jobs"]["e2e"]["run_count"] == 5
    assert manifest["arms"]["before"]["complete"] is False


def test_after_core_responsive_pair_set_mismatch_detected_by_semantic_validator():
    """GIVEN an after arm where `e2e-core` and `e2e-responsive-matrix` do
    NOT share the same set of `workflow_run_id`s (an incomplete pairing)
    WHEN `validate_manifest_semantics` runs THEN
    `pair_set_mismatch_e2e_core_e2e_responsive_matrix` is reported for the
    after arm (#2159 P0-9)."""
    before_records = [_record(i, job="e2e", head_sha=BEFORE_SHA) for i in range(20)]
    after_records = [_record(100 + i, job="e2e-core", head_sha=AFTER_SHA) for i in range(20)]
    # Responsive side uses a DIFFERENT, non-overlapping id range.
    after_records += [_record(9000 + i, job="e2e-responsive-matrix", head_sha=AFTER_SHA) for i in range(20)]

    manifest = collector.collect_benchmark_manifest(BEFORE_SHA, AFTER_SHA, before_records, after_records)
    violations = collector.validate_manifest_semantics(manifest)
    assert any("pair_set_mismatch_e2e_core_e2e_responsive_matrix: after" in v for v in violations)


def test_failure_conclusion_runs_excluded_from_sample_and_reported_as_evidence_error():
    """GIVEN 20 structurally-valid `e2e` records all with `conclusion:
    failure` WHEN collected THEN none of them count toward run_count/
    completeness (a failed run's timing is not a valid performance sample),
    and each is reported as an explicit
    `non_successful_conclusion_excluded_from_sample` evidence error rather
    than silently vanishing."""
    before_records = [_record(i, job="e2e", head_sha=BEFORE_SHA, conclusion="failure") for i in range(1, 21)]
    after_records = []
    for i in range(20):
        after_records.append(_record(100 + i, job="e2e-core", head_sha=AFTER_SHA))
        after_records.append(_record(100 + i, job="e2e-responsive-matrix", head_sha=AFTER_SHA))

    manifest = collector.collect_benchmark_manifest(BEFORE_SHA, AFTER_SHA, before_records, after_records)
    assert manifest["arms"]["before"]["jobs"]["e2e"]["run_count"] == 0
    assert manifest["arms"]["before"]["complete"] is False
    non_success_errors = [
        e
        for e in manifest["evidence_errors"]
        if e["arm"] == "before" and e["reason"] == "non_successful_conclusion_excluded_from_sample"
    ]
    assert len(non_success_errors) == 20


def test_mixed_success_and_non_success_only_success_counts():
    """GIVEN 20 `success` records and 5 additional `cancelled`/`skipped`/
    `timed_out` records for the same job WHEN collected THEN run_count
    reflects only the 20 successful samples, not 25."""
    before_records = [_record(i, job="e2e", head_sha=BEFORE_SHA, conclusion="success") for i in range(1, 21)]
    before_records += [
        _record(500, job="e2e", head_sha=BEFORE_SHA, conclusion="cancelled"),
        _record(501, job="e2e", head_sha=BEFORE_SHA, conclusion="skipped"),
        _record(502, job="e2e", head_sha=BEFORE_SHA, conclusion="timed_out"),
    ]
    after_records = []
    for i in range(20):
        after_records.append(_record(100 + i, job="e2e-core", head_sha=AFTER_SHA))
        after_records.append(_record(100 + i, job="e2e-responsive-matrix", head_sha=AFTER_SHA))

    manifest = collector.collect_benchmark_manifest(BEFORE_SHA, AFTER_SHA, before_records, after_records)
    assert manifest["arms"]["before"]["jobs"]["e2e"]["run_count"] == 20


# --------------------------------------------------------------------------- #
# #2159 P0-9: semantic manifest validator (cross-field invariants JSON
# Schema alone cannot express).
# --------------------------------------------------------------------------- #
def _minimal_valid_manifest() -> dict:
    before_records = [_record(i, job="e2e", head_sha=BEFORE_SHA) for i in range(20)]
    after_records = []
    for i in range(20):
        after_records.append(_record(100 + i, job="e2e-core", head_sha=AFTER_SHA))
        after_records.append(_record(100 + i, job="e2e-responsive-matrix", head_sha=AFTER_SHA))
    return collector.collect_benchmark_manifest(BEFORE_SHA, AFTER_SHA, before_records, after_records)


def test_semantic_validator_passes_on_self_produced_manifest():
    """GIVEN a manifest produced by `collect_benchmark_manifest` itself
    WHEN `validate_manifest_semantics` runs THEN there are no violations
    (the producer's own output is internally consistent by construction)."""
    manifest = _minimal_valid_manifest()
    assert collector.validate_manifest_semantics(manifest) == []


def test_semantic_validator_detects_root_sha_vs_arm_commit_sha_mismatch():
    manifest = _minimal_valid_manifest()
    manifest["arms"]["before"]["commit_sha"] = "c" * 40
    violations = collector.validate_manifest_semantics(manifest)
    assert any("commit_sha_mismatches_root_before_sha" in v for v in violations)


def test_semantic_validator_detects_run_count_mismatches_len_runs():
    manifest = _minimal_valid_manifest()
    manifest["arms"]["before"]["jobs"]["e2e"]["run_count"] = 999
    violations = collector.validate_manifest_semantics(manifest)
    assert any("run_count_mismatches_len_runs: before/e2e" in v for v in violations)


def test_semantic_validator_detects_sample_ids_mismatches_runs():
    manifest = _minimal_valid_manifest()
    manifest["arms"]["before"]["jobs"]["e2e"]["sample_workflow_run_ids"] = [999999]
    violations = collector.validate_manifest_semantics(manifest)
    assert any("sample_workflow_run_ids_mismatches_runs: before/e2e" in v for v in violations)


def test_semantic_validator_detects_complete_true_with_zero_runs():
    manifest = _minimal_valid_manifest()
    manifest["arms"]["before"]["jobs"]["e2e"]["run_count"] = 0
    manifest["arms"]["before"]["jobs"]["e2e"]["runs"] = []
    manifest["arms"]["before"]["jobs"]["e2e"]["sample_workflow_run_ids"] = []
    manifest["arms"]["before"]["complete"] = True
    violations = collector.validate_manifest_semantics(manifest)
    assert any("complete_true_with_zero_runs: before/e2e" in v for v in violations)


def test_semantic_validator_detects_complete_true_with_evidence_errors():
    manifest = _minimal_valid_manifest()
    manifest["arms"]["before"]["complete"] = True
    manifest["evidence_errors"].append(
        {"arm": "before", "reason": "run_record_verification_failed", "detail": "workflow_run_id=999 violations=[]"}
    )
    violations = collector.validate_manifest_semantics(manifest)
    assert any("complete_true_with_evidence_errors: before" in v for v in violations)


def test_semantic_validator_detects_job_topology_mismatch():
    manifest = _minimal_valid_manifest()
    # Inject an out-of-topology job key into the pre_split before arm.
    manifest["arms"]["before"]["jobs"]["e2e-core"] = dict(manifest["arms"]["after"]["jobs"]["e2e-core"])
    violations = collector.validate_manifest_semantics(manifest)
    assert any("job_topology_mismatch: before" in v for v in violations)


def test_manifest_requires_workflow_digest_per_run_record():
    """GIVEN a run record missing `workflow_digest` (the AC1 field the
    review confirmed was entirely absent from this schema) WHEN
    `_verify_run_record` runs THEN `missing_or_invalid_workflow_digest` is
    reported and the record is excluded from the cohort (#2159 P0-9)."""
    record = _record(1, job="e2e", head_sha=BEFORE_SHA)
    del record["workflow_digest"]
    violations = collector._verify_run_record(record, BEFORE_SHA)
    assert "missing_or_invalid_workflow_digest" in violations


def test_cli_semantic_violation_causes_operational_failure(tmp_path):
    """GIVEN a manifest that would pass JSON Schema structural validation
    but fails a semantic cross-field invariant WHEN `main()` runs THEN it
    exits EXIT_OPERATIONAL_FAILURE (3), not silently written as if valid.
    This is exercised by monkeypatching `validate_manifest_semantics` via a
    contaminated before-arm topology (job map key does not match the
    arm-specific default), which the CLI wiring rejects."""
    before_path = tmp_path / "before_runs.json"
    after_path = tmp_path / "after_runs.json"
    output_path = tmp_path / "manifest.json"

    # Force a job_topology_mismatch: request `e2e-core` as the before-arm
    # job name (via --before-job-names), which does not match the
    # pre_split topology label the collector always assigns to the before
    # arm -- the semantic validator rejects this even though every
    # individual record and the JSON Schema shape are both otherwise valid.
    before_records = [_record(i, job="e2e-core", head_sha=BEFORE_SHA) for i in range(20)]
    after_records = []
    for i in range(20):
        after_records.append(_record(100 + i, job="e2e-core", head_sha=AFTER_SHA))
        after_records.append(_record(100 + i, job="e2e-responsive-matrix", head_sha=AFTER_SHA))

    before_path.write_text(json.dumps(before_records), encoding="utf-8")
    after_path.write_text(json.dumps(after_records), encoding="utf-8")

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
            "--before-job-names",
            "e2e-core",
        ]
    )
    assert exit_code == collector.EXIT_OPERATIONAL_FAILURE
    assert not output_path.exists()
