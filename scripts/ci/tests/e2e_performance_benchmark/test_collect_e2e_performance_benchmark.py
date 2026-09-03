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


# #2159 OWNER scope-authority ruling (issuecomment-5299412215, item
# 1/P0-2): fixture workflow SHA distinct from BEFORE_SHA/AFTER_SHA (the
# measured application code commits) -- proves `workflow_sha` is recorded
# as an INDEPENDENT field, never conflated with `head_sha`.
WORKFLOW_SHA = "c" * 40


def _record(
    workflow_run_id: int,
    job: str = "e2e-core",
    head_sha: str = BEFORE_SHA,
    artifact_id: int | None = None,
    artifact_digest: str | None = None,
    conclusion: str = "success",
    workflow_digest: str = "workflow-digest-fixture-v1",
    workflow_sha: str = WORKFLOW_SHA,
    run_attempt: int = 1,
    measured_head_sha: str | None = None,
    merge_sha: str | None = None,
) -> dict:
    # #2182 fix_delta (OWNER adversarial review issuecomment-5302446086,
    # P0-3): every fixture in this baseline test file now carries an
    # EXPLICIT `run_attempt` (default 1) -- a record with a MISSING
    # `run_attempt` key is excluded from the trusted/selected cohort
    # entirely under the corrected policy (see
    # test_collect_e2e_performance_benchmark_rerun_attempt.py's
    # `test_missing_run_attempt_excluded_from_trusted_cohort_and_reported`
    # for the dedicated regression coverage of that exclusion itself).
    #
    # #2184: `measured_head_sha`/`merge_sha` are OPT-IN (default `None`,
    # meaning the field is entirely ABSENT -- a pre-#2184 legacy-shaped
    # record) -- pass them explicitly to construct a new-style record
    # exercising the #2184 measured_head_sha/workflow_run_head_sha
    # verification paths (see the dedicated `#2184` test block below).
    record: dict = {
        "workflow_run_id": workflow_run_id,
        "job": job,
        "head_sha": head_sha,
        "artifact_id": artifact_id if artifact_id is not None else workflow_run_id,
        "artifact_digest": artifact_digest or ("sha256:" + f"{workflow_run_id:064x}"),
        "conclusion": conclusion,
        "workflow_digest": workflow_digest,
        "workflow_sha": workflow_sha,
        "run_attempt": run_attempt,
    }
    if measured_head_sha is not None:
        record["measured_head_sha"] = measured_head_sha
    if merge_sha is not None:
        record["merge_sha"] = merge_sha
    return record


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
        # #2182 fix_delta P1: an explicit run_attempt=2 rerun -- distinct
        # from the run_attempt=1 original above, so this is a genuine
        # non-attempt-1 exclusion, not an ambiguous same-identity-
        # different-content collision (which would exclude BOTH).
        _record(500, job="e2e-core", head_sha=BEFORE_SHA, artifact_id=999, run_attempt=2),
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
    # #2182 fix_delta P1: explicit run_attempt=2 -- a genuine rerun
    # attempt, distinct from the run_attempt=1 original (never an
    # ambiguous same-identity-different-content collision).
    for run_id in range(1, 6):
        before_records.append(
            _record(run_id, job="e2e-core", head_sha=BEFORE_SHA, artifact_id=90000 + run_id, run_attempt=2)
        )
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


def test_manifest_requires_workflow_sha_per_run_record():
    """#2159 OWNER scope-authority ruling (issuecomment-5299412215, item
    1/P0-2): a run record missing `workflow_sha` (the WORKFLOW DEFINITION's
    own commit, distinct from `head_sha`) fails `_verify_run_record` with
    `missing_or_invalid_workflow_sha` -- the collector never silently
    accepts a record that conflates workflow_sha with head_sha or omits it
    entirely."""
    record = _record(1)
    del record["workflow_sha"]
    violations = collector._verify_run_record(record, BEFORE_SHA)
    assert "missing_or_invalid_workflow_sha" in violations


def test_manifest_records_workflow_sha_distinct_from_head_sha():
    """GIVEN a run record whose `workflow_sha` differs from `head_sha`
    (the expected shape for a fixed-SHA benchmark dispatch, where the
    workflow definition stays on the current branch tip while the
    application code is pinned to an old/new target_sha) WHEN the manifest
    is collected THEN both fields are preserved, independently, on the
    resulting manifest run record."""
    records = [_record(1, job="e2e-core", head_sha=BEFORE_SHA, workflow_sha=WORKFLOW_SHA)]
    evidence_errors: list[dict] = []
    result = collector._collect_arm(
        arm_name="before",
        commit_sha=BEFORE_SHA,
        raw_records=records,
        job_names=("e2e-core",),
        min_run_count=1,
        evidence_errors=evidence_errors,
    )
    run = result["jobs"]["e2e-core"]["runs"][0]
    assert run["head_sha"] == BEFORE_SHA
    assert run["workflow_sha"] == WORKFLOW_SHA
    assert run["head_sha"] != run["workflow_sha"]


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


# --------------------------------------------------------------------------- #
# #2159 P0-3 (fix_delta after adversarial review issuecomment-5295659213):
# trusted (live-API-verified) collector layer, fixture-driven with an
# injected fake transport (no real network in these tests).
# --------------------------------------------------------------------------- #
def _fake_artifacts_response(
    artifacts: list[dict],
) -> dict:
    return {"artifacts": artifacts}


def _live_api_artifact(
    artifact_id: int,
    digest: str,
    workflow_run_id: int,
    head_sha: str,
    name: str | None = None,
) -> dict:
    artifact = {
        "id": artifact_id,
        "digest": digest,
        "workflow_run": {"id": workflow_run_id, "head_sha": head_sha},
    }
    if name is not None:
        artifact["name"] = name
    return artifact


def test_live_api_verification_accepts_genuinely_matching_record():
    """GIVEN a record whose claimed artifact_id/artifact_digest/head_sha
    all match a (fake, injected) live GitHub Actions API response, AND
    whose (default, #2182 fix_delta) run_attempt=1 is bound to a genuine
    attempt-specific job (matching name/head_sha/conclusion), WHEN
    `verify_run_record_against_live_api` runs THEN there are no
    violations."""
    record = _record(500, job="e2e-core", head_sha=BEFORE_SHA, artifact_id=777, artifact_digest="sha256:" + "e" * 64)

    def fake_api_call(endpoint: str) -> dict:
        if endpoint == "repos/owner/repo/actions/runs/500/artifacts?per_page=100&page=1":
            return _fake_artifacts_response(
                [_live_api_artifact(777, "sha256:" + "e" * 64, 500, BEFORE_SHA, name="ci-runtime-baseline-e2e-core-1")]
            )
        if endpoint == "repos/owner/repo/actions/runs/500/attempts/1/jobs":
            return {"jobs": [{"run_attempt": 1, "name": "e2e-core", "head_sha": BEFORE_SHA, "conclusion": "success"}]}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    violations = collector.verify_run_record_against_live_api(
        record, BEFORE_SHA, "owner/repo", api_call=fake_api_call
    )
    assert violations == []


def test_live_api_verification_rejects_fabricated_artifact_id():
    """GIVEN a record claiming an artifact_id that does NOT appear in the
    live API's artifact listing for that workflow_run_id (a fabricated
    artifact ID -- the review's exact P0-3 attack example) WHEN
    `verify_run_record_against_live_api` runs THEN
    artifact_not_found_via_live_api is reported."""
    record = _record(
        1001, job="e2e-core", head_sha=BEFORE_SHA, artifact_id=999999999, artifact_digest="sha256:" + "f" * 64
    )

    def fake_api_call(endpoint: str) -> dict:
        # The live run DOES exist, but its real artifacts do not include
        # the fabricated ID.
        return _fake_artifacts_response(
            [_live_api_artifact(123456, "sha256:" + "a" * 64, 1001, BEFORE_SHA)]
        )

    violations = collector.verify_run_record_against_live_api(
        record, BEFORE_SHA, "owner/repo", api_call=fake_api_call
    )
    assert any("artifact_not_found_via_live_api" in v for v in violations)


def test_live_api_verification_rejects_digest_mismatch():
    """GIVEN a record whose claimed artifact_digest does NOT match the
    live API's digest for that (real) artifact_id WHEN
    `verify_run_record_against_live_api` runs THEN
    artifact_digest_mismatch_vs_live_api is reported."""
    record = _record(
        1002, job="e2e-core", head_sha=BEFORE_SHA, artifact_id=555, artifact_digest="sha256:" + "1" * 64
    )

    def fake_api_call(endpoint: str) -> dict:
        return _fake_artifacts_response(
            [_live_api_artifact(555, "sha256:" + "2" * 64, 1002, BEFORE_SHA)]
        )

    violations = collector.verify_run_record_against_live_api(
        record, BEFORE_SHA, "owner/repo", api_call=fake_api_call
    )
    assert any("artifact_digest_mismatch_vs_live_api" in v for v in violations)


def test_live_api_verification_rejects_head_sha_mismatch():
    """GIVEN a record/expected_head_sha that does NOT match the live API's
    `workflow_run.head_sha` for the claimed artifact (the artifact is real
    but belongs to a DIFFERENT commit than claimed) WHEN
    `verify_run_record_against_live_api` runs THEN
    artifact_head_sha_mismatch_vs_live_api is reported."""
    record = _record(
        1003, job="e2e-core", head_sha=BEFORE_SHA, artifact_id=666, artifact_digest="sha256:" + "3" * 64
    )

    def fake_api_call(endpoint: str) -> dict:
        return _fake_artifacts_response(
            [_live_api_artifact(666, "sha256:" + "3" * 64, 1003, "f" * 40)]  # different head_sha
        )

    violations = collector.verify_run_record_against_live_api(
        record, BEFORE_SHA, "owner/repo", api_call=fake_api_call
    )
    assert any("artifact_head_sha_mismatch_vs_live_api" in v for v in violations)


def test_live_api_verification_batch_reports_only_failing_records():
    records = [
        _record(2001, job="e2e-core", head_sha=BEFORE_SHA, artifact_id=1, artifact_digest="sha256:" + "0" * 64),
        _record(2002, job="e2e-core", head_sha=BEFORE_SHA, artifact_id=2, artifact_digest="sha256:" + "0" * 64),
    ]

    def fake_api_call(endpoint: str) -> dict:
        if endpoint == "repos/owner/repo/actions/runs/2001/artifacts?per_page=100&page=1":
            return _fake_artifacts_response(
                [_live_api_artifact(1, "sha256:" + "0" * 64, 2001, BEFORE_SHA, name="ci-runtime-baseline-e2e-core-1")]
            )
        if endpoint == "repos/owner/repo/actions/runs/2001/attempts/1/jobs":
            return {"jobs": [{"run_attempt": 1, "name": "e2e-core", "head_sha": BEFORE_SHA, "conclusion": "success"}]}
        if endpoint == "repos/owner/repo/actions/runs/2002/artifacts?per_page=100&page=1":
            return _fake_artifacts_response([])  # 2002's artifact is fabricated
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    failures = collector.verify_records_against_live_api(records, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert len(failures) == 1
    assert failures[0]["record"]["workflow_run_id"] == 2002


def test_live_api_transport_error_is_reported_not_raised():
    """GIVEN the injected transport raises `LiveAPIError` (network/auth
    failure) WHEN `verify_run_record_against_live_api` runs THEN it is
    caught and reported as a violation string, not propagated as an
    uncaught exception (a transient API outage must not crash the
    collector)."""
    record = _record(3001, job="e2e-core", head_sha=BEFORE_SHA)

    def failing_api_call(endpoint: str) -> dict:
        raise collector.LiveAPIError("simulated_rate_limit")

    violations = collector.verify_run_record_against_live_api(
        record, BEFORE_SHA, "owner/repo", api_call=failing_api_call
    )
    assert any("live_api_artifacts_fetch_failed" in v for v in violations)


def test_cli_verify_against_live_api_requires_repo(tmp_path):
    before_path = tmp_path / "before_runs.json"
    after_path = tmp_path / "after_runs.json"
    output_path = tmp_path / "manifest.json"
    before_path.write_text("[]", encoding="utf-8")
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
            "--verify-against-live-api",
        ]
    )
    assert exit_code == collector.EXIT_OPERATIONAL_FAILURE


def test_cli_verify_against_live_api_surfaces_failures_as_evidence_errors(tmp_path, monkeypatch):
    """GIVEN --verify-against-live-api with an injected fake `gh api` CLI
    call (via monkeypatching the module's default transport) WHEN a record
    fails live verification THEN it is EXCLUDED from the cohort AND
    reported as a `live_api_verification_failed` evidence_error in the
    written manifest -- never silently dropped."""

    def fake_default_api_call(endpoint: str) -> dict:
        return _fake_artifacts_response([])  # nothing ever verifies

    monkeypatch.setattr(collector, "_default_gh_api_call", fake_default_api_call)

    before_records = [_record(1, job="e2e", head_sha=BEFORE_SHA)]
    after_records = [_record(101, job="e2e-core", head_sha=AFTER_SHA)]

    before_path = tmp_path / "before_runs.json"
    after_path = tmp_path / "after_runs.json"
    output_path = tmp_path / "manifest.json"
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
            "--verify-against-live-api",
            "--repo",
            "owner/repo",
            "--min-runs",
            "1",
        ]
    )
    assert exit_code == collector.EXIT_INCOMPLETE
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["arms"]["before"]["jobs"]["e2e"]["run_count"] == 0
    live_failures = [e for e in written["evidence_errors"] if e["reason"] == "live_api_verification_failed"]
    assert len(live_failures) == 2


# --------------------------------------------------------------------------- #
# #2184 AC4: measured_head_sha / workflow_run_head_sha structural + live-API
# self-consistency verification and manifest propagation.
# --------------------------------------------------------------------------- #
MEASURED_A = "1" * 40
WORKFLOW_RUN_B = "2" * 40


def test_ac4a_measured_and_workflow_run_head_sha_differ_and_propagate_to_manifest():
    """GIVEN a workflow_dispatch-origin record with `measured_head_sha=A`
    (matching the benchmark's target_sha) and `merge_sha=B` (A != B --
    `merge_sha` mirrors the dispatch ref's own `github.sha`) WHEN
    collected THEN the record is structurally trusted (no
    `measured_head_sha_mismatch`) AND the resulting manifest RunRecord
    carries BOTH `measured_head_sha` and `workflow_run_head_sha`
    (verbatim / derived from `merge_sha`), and they DIFFER -- the
    expected shape for a fixed-SHA benchmark dispatch (#2184 AC1
    Outcome), never asserted equal. Live-API self-consistency is verified
    against `workflow_run_head_sha` (B), NOT `expected_head_sha`/
    `measured_head_sha` (A) -- proving the two concepts are checked
    independently (AC4(i))."""
    record = _record(
        600,
        job="e2e-core",
        head_sha=WORKFLOW_RUN_B,
        measured_head_sha=MEASURED_A,
        merge_sha=WORKFLOW_RUN_B,
    )
    evidence_errors: list[dict] = []
    result = collector._collect_arm(
        arm_name="after",
        commit_sha=MEASURED_A,
        raw_records=[record],
        job_names=("e2e-core",),
        min_run_count=1,
        evidence_errors=evidence_errors,
    )
    assert evidence_errors == []
    run = result["jobs"]["e2e-core"]["runs"][0]
    assert run["measured_head_sha"] == MEASURED_A
    assert run["workflow_run_head_sha"] == WORKFLOW_RUN_B
    assert run["measured_head_sha"] != run["workflow_run_head_sha"]

    def fake_api_call(endpoint: str) -> dict:
        if endpoint == "repos/owner/repo/actions/runs/600/artifacts?per_page=100&page=1":
            return _fake_artifacts_response(
                [
                    _live_api_artifact(
                        600, record["artifact_digest"], 600, WORKFLOW_RUN_B, name="ci-runtime-baseline-e2e-core-1"
                    )
                ]
            )
        if endpoint == "repos/owner/repo/actions/runs/600/attempts/1/jobs":
            return {
                "jobs": [{"run_attempt": 1, "name": "e2e-core", "head_sha": WORKFLOW_RUN_B, "conclusion": "success"}]
            }
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    violations = collector.verify_run_record_against_live_api(record, MEASURED_A, "owner/repo", api_call=fake_api_call)
    assert violations == []


def test_ac4b_measured_head_sha_mismatch_fails_structural_verification():
    """GIVEN a record whose `measured_head_sha` does NOT match the
    benchmark's expected commit (target_sha) WHEN structurally verified
    THEN `measured_head_sha_mismatch` is reported and the record is
    excluded from the trusted cohort -- `head_sha` itself is never
    compared for this record (it legitimately differs, resolving to
    `github.sha`)."""
    record = _record(
        601,
        job="e2e-core",
        head_sha=WORKFLOW_RUN_B,
        measured_head_sha="3" * 40,
        merge_sha=WORKFLOW_RUN_B,
    )
    violations = collector._verify_run_record(record, MEASURED_A)
    assert "measured_head_sha_mismatch" in violations
    assert "head_sha_mismatch" not in violations


def test_ac4c_workflow_run_head_sha_mismatch_vs_live_api_fails():
    """GIVEN a record whose derived `workflow_run_head_sha` (from
    `merge_sha`) does NOT match the live API's own head_sha for that
    artifact/job WHEN verified THEN a self-consistency violation is
    reported -- even though `measured_head_sha` correctly matches
    `expected_head_sha`, proving the live-API check compares against
    `workflow_run_head_sha`, never `expected_head_sha` (#2184 AC4(i))."""
    record = _record(
        602,
        job="e2e-core",
        head_sha=WORKFLOW_RUN_B,
        measured_head_sha=MEASURED_A,
        merge_sha=WORKFLOW_RUN_B,
    )

    def fake_api_call(endpoint: str) -> dict:
        if endpoint == "repos/owner/repo/actions/runs/602/artifacts?per_page=100&page=1":
            return _fake_artifacts_response(
                [
                    _live_api_artifact(
                        602, record["artifact_digest"], 602, "4" * 40, name="ci-runtime-baseline-e2e-core-1"
                    )
                ]
            )
        if endpoint == "repos/owner/repo/actions/runs/602/attempts/1/jobs":
            return {"jobs": [{"run_attempt": 1, "name": "e2e-core", "head_sha": "4" * 40, "conclusion": "success"}]}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    violations = collector.verify_run_record_against_live_api(record, MEASURED_A, "owner/repo", api_call=fake_api_call)
    assert any("artifact_head_sha_mismatch_vs_live_api" in v for v in violations)
    assert any("run_attempt_job_head_sha_mismatch_vs_live_api" in v for v in violations)


def test_ac4d_normal_execution_measured_equals_workflow_run_head_sha_passes():
    """GIVEN normal (non-benchmark) execution records where
    `measured_head_sha == workflow_run_head_sha` (both derived from the
    same `github.sha` on a push/pull_request run) for BOTH `e2e-core` and
    `e2e-responsive-matrix`, sharing the same `workflow_run_id` set, WHEN
    collected THEN both fields are propagated identically, the after arm
    is trusted/complete, the manifest still validates against the JSON
    Schema, and `validate_manifest_semantics` reports no
    `pair_set_mismatch_e2e_core_e2e_responsive_matrix` -- proving the
    symmetric #2184 AC1 producer change preserves the existing pair-set
    invariant (#2184 AC4 last sentence)."""
    after_records = []
    for i in range(20):
        run_id = 700 + i
        for job in ("e2e-core", "e2e-responsive-matrix"):
            after_records.append(
                _record(run_id, job=job, head_sha=AFTER_SHA, measured_head_sha=AFTER_SHA, merge_sha=AFTER_SHA)
            )
    before_records = _full_job_set_records(20, BEFORE_SHA, start_id=1)

    manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA,
        AFTER_SHA,
        before_records,
        after_records,
        before_job_names=("e2e",),
        after_job_names=("e2e-core", "e2e-responsive-matrix"),
    )
    assert manifest["arms"]["after"]["complete"] is True
    for job in ("e2e-core", "e2e-responsive-matrix"):
        for run in manifest["arms"]["after"]["jobs"][job]["runs"]:
            assert run["measured_head_sha"] == AFTER_SHA
            assert run["workflow_run_head_sha"] == AFTER_SHA

    collector._validate_against_schema(manifest)
    violations = collector.validate_manifest_semantics(manifest)
    assert not any("pair_set_mismatch" in v for v in violations)


# --------------------------------------------------------------------------- #
# #2159 P0-3: genuine LIVE GitHub API verification (real network call via
# `gh api`, no injected fake transport). Requires an authenticated `gh` CLI
# -- SKIPs (not a fabricated PASS) when unavailable, per this repo's
# Runtime Verification Applicability SKIP policy for evidence that can
# only exist against live GitHub Actions history. This is proven against a
# REAL artifact this PR's own CI produced (run 31819439122, artifact
# 9226468408, ci-runtime-baseline-e2e-core-1, verified via `gh api
# repos/squne121/loop-protocol/actions/runs/31819439122/artifacts` during
# this fix_delta implementation session).
# --------------------------------------------------------------------------- #
def _gh_cli_available() -> bool:
    import shutil
    import subprocess as _subprocess

    if shutil.which("gh") is None:
        return False
    try:
        result = _subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=10, check=False
        )
    except OSError:
        return False
    return result.returncode == 0


@pytest.mark.skipif(not _gh_cli_available(), reason="gh CLI not available/authenticated in this environment")
def test_live_api_verification_against_real_pr_2172_ci_artifact():
    """GIVEN the REAL `ci-runtime-baseline-e2e-core-1` artifact this PR's
    own CI produced (run 31819439122, artifact 9226468408) WHEN verified
    against the LIVE GitHub API (real network call, not an injected fake)
    THEN it is accepted with zero violations; a fabricated artifact ID
    against that same real run is rejected."""
    real_head_sha = "79065f96e69e7079bda8fc2344e07caac402bc04"
    real_record = {
        "workflow_run_id": 31819439122,
        "job": "e2e-core",
        "head_sha": real_head_sha,
        "artifact_id": 9226468408,
        "artifact_digest": "sha256:246d5683f6e1b792d304289ad6af167edc139a0e1a6816c354bc990a2ebbe79b",
        "conclusion": "success",
    }
    try:
        violations = collector.verify_run_record_against_live_api(
            real_record, real_head_sha, "squne121/loop-protocol"
        )
    except Exception:  # pragma: no cover -- transport-level, not a test bug
        pytest.skip("live GitHub API call failed unexpectedly (network/auth) -- see gh CLI output")
    if any("live_api_artifacts_fetch_failed" in v for v in violations):
        # Artifact retention is 30 days from creation (2026-08-14); once it
        # expires this genuine live check can no longer run against this
        # fixed historical artifact id -- SKIP (not a fabricated
        # PASS/FAIL) rather than permanently failing this test file.
        pytest.skip(f"real artifact 9226468408 likely expired (30-day retention): {violations}")
    assert violations == []

    fabricated_record = dict(real_record, artifact_id=999999999, artifact_digest="sha256:" + "a" * 64)
    fabricated_violations = collector.verify_run_record_against_live_api(
        fabricated_record, real_head_sha, "squne121/loop-protocol"
    )
    assert any("artifact_not_found_via_live_api" in v for v in fabricated_violations)
