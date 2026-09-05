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

# #2184 AC4(iii) fix_delta (pr-reviewer REQUEST_CHANGES on PR #2493):
# sentinel distinguishing "caller didn't pass merge_sha" (auto-derive, see
# `_record` below) from an EXPLICIT `merge_sha=None` (construct a record
# genuinely lacking a derivable `workflow_run_head_sha`, for AC4(iii)
# hard-exclude coverage).
_MERGE_SHA_UNSET = object()


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
    merge_sha: str | None | object = _MERGE_SHA_UNSET,
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
    # #2184: `measured_head_sha` is OPT-IN (default `None`, meaning the
    # field is entirely ABSENT) -- pass it explicitly to construct a
    # new-style record exercising the #2184 measured_head_sha verification
    # paths (see the dedicated `#2184` test block below).
    #
    # #2184 AC4(iii) fix_delta: `merge_sha` (which `workflow_run_head_sha`
    # is derived from, #2184 AC2) defaults to `head_sha` -- NOT absent --
    # when the caller doesn't pass it explicitly. This mirrors real CI
    # history: `ci_runtime_baseline_v1`'s `merge_sha` field
    # (`GH_SHA: ${{ github.sha }}`) has been recorded unconditionally
    # since #2159, well before #2184's `measured_head_sha`, so a record
    # collected from any modern (#2159-era or later) `e2e-core`/
    # `e2e-responsive-matrix` run always HAS a `merge_sha` even when it
    # predates #2184's `measured_head_sha` field -- it is never "legacy
    # ambiguous" (AC4(iii)) on that basis alone. The genuinely ambiguous
    # case AC4(iii) targets (a record with NEITHER field, i.e. one that
    # predates even #2159's `merge_sha`) is constructed by passing
    # `merge_sha=None` explicitly (see the dedicated AC4(iii) test block).
    if merge_sha is _MERGE_SHA_UNSET:
        merge_sha = head_sha
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
# #2184 AC4(iii) (fix_delta after pr-reviewer REQUEST_CHANGES on PR #2493):
# a legacy record carrying NEITHER `measured_head_sha` NOR a derivable
# `workflow_run_head_sha` is hard-excluded from the trusted cohort with an
# explicit `legacy_ambiguous_head_sha` evidence_errors reason, scoped to
# `LEGACY_AMBIGUOUS_HEAD_SHA_JOBS` (`e2e-core`/`e2e-responsive-matrix`).
# --------------------------------------------------------------------------- #
def test_ac4e_legacy_record_lacking_both_head_sha_fields_is_hard_excluded():
    """GIVEN an `e2e-core` record carrying NEITHER `measured_head_sha` NOR
    a `merge_sha` (so `workflow_run_head_sha` cannot be derived either --
    the genuinely ambiguous pre-#2159 legacy shape) WHEN collected THEN it
    is excluded from the trusted cohort with the explicit
    `legacy_ambiguous_head_sha` reason -- never promoted by inferring/
    synthesizing either field, and never silently folded into the generic
    `run_record_verification_failed` bucket (#2184 AC4(iii))."""
    ambiguous_record = _record(650, job="e2e-core", head_sha=BEFORE_SHA, merge_sha=None)
    assert "measured_head_sha" not in ambiguous_record
    assert "merge_sha" not in ambiguous_record

    evidence_errors: list[dict] = []
    result = collector._collect_arm(
        arm_name="before",
        commit_sha=BEFORE_SHA,
        raw_records=[ambiguous_record],
        job_names=("e2e-core",),
        min_run_count=1,
        evidence_errors=evidence_errors,
    )
    assert result["jobs"]["e2e-core"]["run_count"] == 0
    assert result["jobs"]["e2e-core"]["runs"] == []

    matching_errors = [
        err
        for err in evidence_errors
        if err["arm"] == "before" and "workflow_run_id=650" in err["detail"]
    ]
    assert len(matching_errors) == 1
    assert matching_errors[0]["reason"] == "legacy_ambiguous_head_sha"


def test_ac4f_legacy_record_with_derivable_merge_sha_is_not_hard_excluded():
    """GIVEN an `e2e-core` record lacking `measured_head_sha` but carrying
    a `merge_sha` (a genuine #2159-era, pre-#2184 record -- `merge_sha`
    has been recorded unconditionally since #2159, well before #2184)
    WHEN collected THEN `workflow_run_head_sha` IS derivable from
    `merge_sha`, so the record is NOT `legacy_ambiguous_head_sha`-excluded
    -- it remains trusted via the existing (unchanged) `head_sha !=
    expected_head_sha` structural check (#2184 AC4(i) backward
    compatibility), and `workflow_run_head_sha` propagates to the
    manifest while `measured_head_sha` is correctly omitted."""
    legacy_record = _record(651, job="e2e-core", head_sha=BEFORE_SHA, merge_sha=BEFORE_SHA)
    assert "measured_head_sha" not in legacy_record
    assert legacy_record["merge_sha"] == BEFORE_SHA

    evidence_errors: list[dict] = []
    result = collector._collect_arm(
        arm_name="before",
        commit_sha=BEFORE_SHA,
        raw_records=[legacy_record],
        job_names=("e2e-core",),
        min_run_count=1,
        evidence_errors=evidence_errors,
    )
    assert evidence_errors == []
    run = result["jobs"]["e2e-core"]["runs"][0]
    assert "measured_head_sha" not in run
    assert run["workflow_run_head_sha"] == BEFORE_SHA


def test_ac4g_legacy_ambiguous_exclusion_scoped_to_e2e_core_responsive_matrix_jobs():
    """GIVEN a record for the permanently-retired pre-split `e2e` job
    (`DEFAULT_BEFORE_JOB_NAMES`, never touched by #2184's producer change)
    carrying NEITHER `measured_head_sha` NOR `merge_sha` WHEN collected
    THEN it is NOT `legacy_ambiguous_head_sha`-excluded -- the AC4(iii)
    hard-exclude is scoped to `LEGACY_AMBIGUOUS_HEAD_SHA_JOBS`
    (`e2e-core`/`e2e-responsive-matrix`) only, since the `e2e` job was
    never expected to carry either field in the first place (#2184 In
    Scope), unlike a genuine `e2e-core`/`e2e-responsive-matrix` legacy
    record."""
    record = _record(652, job="e2e", head_sha=BEFORE_SHA, merge_sha=None)
    assert "measured_head_sha" not in record
    assert "merge_sha" not in record

    evidence_errors: list[dict] = []
    result = collector._collect_arm(
        arm_name="before",
        commit_sha=BEFORE_SHA,
        raw_records=[record],
        job_names=("e2e",),
        min_run_count=1,
        evidence_errors=evidence_errors,
    )
    assert evidence_errors == []
    assert result["jobs"]["e2e"]["run_count"] == 1
    assert "workflow_run_head_sha" not in result["jobs"]["e2e"]["runs"][0]


# --------------------------------------------------------------------------- #
# #2184 AC2 (fix_delta after OWNER adversarial review of PR #2493,
# issuecomment-5540651128): `workflow_run_head_sha` MUST be derived SOLELY
# from the raw artifact's existing `merge_sha` field -- a caller-supplied
# `workflow_run_head_sha` field on the raw record is never consulted and
# can never override/launder `merge_sha`'s provenance.
# --------------------------------------------------------------------------- #
def test_ac2_explicit_workflow_run_head_sha_field_cannot_override_merge_sha_provenance():
    """GIVEN a raw record whose `merge_sha=C` (the genuine
    `ci_runtime_baseline_v1` provenance source, #2184 AC2) also carries a
    caller-supplied `workflow_run_head_sha=B` field (B != C -- e.g. a
    compromised/misbehaving producer step or a hand-crafted input JSON
    attempting to launder a `merge_sha` disagreement past verification)
    WHEN derived/collected/live-API-verified THEN `C` (`merge_sha`) is
    used throughout -- NEVER `B` -- so a live API response reporting
    `head_sha=B` for that artifact/job is correctly flagged as a
    self-consistency MISMATCH against `C`, rather than being silently
    accepted because it happens to equal the attacker-supplied `B`
    override (the exact laundering path the OWNER review flagged)."""
    merge_sha_c = "5" * 40
    explicit_override_b = "6" * 40
    assert merge_sha_c != explicit_override_b

    record = _record(653, job="e2e-core", head_sha=BEFORE_SHA, merge_sha=merge_sha_c)
    # Simulate a caller/producer that additionally injected an explicit
    # `workflow_run_head_sha` field disagreeing with `merge_sha` -- this
    # must be inert.
    record["workflow_run_head_sha"] = explicit_override_b

    # 1. `_derive_workflow_run_head_sha` returns `merge_sha` (C), never the
    #    caller-supplied override (B).
    assert collector._derive_workflow_run_head_sha(record) == merge_sha_c

    # 2. The derived value propagates into the manifest RunRecord as `C`,
    #    not `B`.
    evidence_errors: list[dict] = []
    result = collector._collect_arm(
        arm_name="before",
        commit_sha=BEFORE_SHA,
        raw_records=[record],
        job_names=("e2e-core",),
        min_run_count=1,
        evidence_errors=evidence_errors,
    )
    assert evidence_errors == []
    run = result["jobs"]["e2e-core"]["runs"][0]
    assert run["workflow_run_head_sha"] == merge_sha_c
    assert run["workflow_run_head_sha"] != explicit_override_b

    # 3. Live-API self-consistency: a live API response whose head_sha
    #    equals the attacker-supplied `B` override (NOT `C`) must be
    #    reported as a mismatch, never silently accepted.
    def fake_api_call(endpoint: str) -> dict:
        if endpoint == "repos/owner/repo/actions/runs/653/artifacts?per_page=100&page=1":
            return _fake_artifacts_response(
                [
                    _live_api_artifact(
                        653,
                        record["artifact_digest"],
                        653,
                        explicit_override_b,
                        name="ci-runtime-baseline-e2e-core-1",
                    )
                ]
            )
        if endpoint == "repos/owner/repo/actions/runs/653/attempts/1/jobs":
            return {
                "jobs": [
                    {
                        "run_attempt": 1,
                        "name": "e2e-core",
                        "head_sha": explicit_override_b,
                        "conclusion": "success",
                    }
                ]
            }
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    violations = collector.verify_run_record_against_live_api(
        record, BEFORE_SHA, "owner/repo", api_call=fake_api_call
    )
    assert any(
        f"artifact_head_sha_mismatch_vs_live_api: expected={merge_sha_c!r}" in v for v in violations
    )
    assert any(
        f"run_attempt_job_head_sha_mismatch_vs_live_api: expected={merge_sha_c!r}" in v for v in violations
    )


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


# =============================================================================
# Issue #2422: `benchmark_layout=monolith|split` A/B/A/B manifest v2 tests.
# =============================================================================

V2_SCHEMA_PATH = REPO_ROOT / "schemas" / "e2e_performance_benchmark_manifest_v2.schema.json"
FROZEN_SOURCE_SHA = "d" * 40
V2_WORKFLOW_SHA = "e" * 40
V2_WORKFLOW_DIGEST = "sha256:" + "1" * 64


def _image(name: str = "ubuntu-24.04", version: str = "20260901.1.0") -> dict:
    return {"name": name, "version": version}


def _provider_job(job: str, workflow_job_id: int, conclusion: str = "success", image: dict | None = None) -> dict:
    return {
        "job": job,
        "workflow_job_id": workflow_job_id,
        "conclusion": conclusion,
        "exact_runner_image": image if image is not None else _image(),
    }


def _default_provider_jobs_for_layout(layout: str, workflow_run_id: int) -> list[dict]:
    # Issue #2422 fix_delta Blocker 5 (OWNER REQUEST_CHANGES on PR #2501,
    # issuecomment-5549966497): the pre-fix_delta version of this fixture
    # gave BOTH layouts the same two-provider shape, hiding the REAL
    # asymmetric topology (monolith = 1 provider job covering both
    # workloads sequentially; split = 2 parallel provider jobs) from every
    # test that used this default. `monolith` now reports ONLY `e2e-core`.
    if layout == "monolith":
        return [_provider_job("e2e-core", workflow_run_id * 10 + 1)]
    return [
        _provider_job("e2e-core", workflow_run_id * 10 + 1),
        _provider_job("e2e-responsive-matrix", workflow_run_id * 10 + 2),
    ]


def _run(
    layout: str,
    workflow_run_id: int,
    run_attempt: int = 1,
    conclusion: str = "success",
    workflow_sha: str = V2_WORKFLOW_SHA,
    workflow_digest: str = V2_WORKFLOW_DIGEST,
    provider_jobs: list[dict] | None = None,
) -> dict:
    return {
        "benchmark_layout": layout,
        "workflow_run_id": workflow_run_id,
        "run_attempt": run_attempt,
        "conclusion": conclusion,
        "run_url": f"https://github.com/squne121/loop-protocol/actions/runs/{workflow_run_id}",
        "workflow_sha": workflow_sha,
        "workflow_digest": workflow_digest,
        "job_names_started": [j["job"] for j in (provider_jobs or [])],
        "provider_jobs": provider_jobs
        if provider_jobs is not None
        else _default_provider_jobs_for_layout(layout, workflow_run_id),
    }


def _block(block_id: str, monolith_run_id: int, split_run_id: int) -> dict:
    return {
        "block_id": block_id,
        "runs": [
            _run("monolith", monolith_run_id),
            _run("split", split_run_id),
        ],
    }


def _expected_playwright_invocation(
    invocation_id: str, lane: str, monolith_provider: str, split_provider: str, evidence_file: str
) -> dict:
    return {
        "invocation_id": invocation_id,
        "lane": lane,
        "provider_placement": {"monolith": monolith_provider, "split": split_provider},
        "evidence_file": evidence_file,
    }


def _frozen_non_treatment() -> dict:
    # Issue #2422 fix_delta Blocker 4: `expected_playwright_invocations` is a
    # LIST of identifiable invocations (id/lane/provider placement/evidence
    # file), never a bare test-count integer -- see the schema's
    # `ExpectedPlaywrightInvocation` def. `expected_test_count` is the
    # separate, optional plain aggregate count.
    return {
        "test_inventory_digest": "sha256:" + "2" * 64,
        "expected_playwright_invocations": [
            _expected_playwright_invocation(
                "core", "core", "e2e-core", "e2e-core", "test-results-e2e-core/core-evidence.json"
            ),
            _expected_playwright_invocation(
                "responsive",
                "responsive",
                "e2e-core",
                "e2e-responsive-matrix",
                "test-results-e2e-responsive-matrix/responsive-canvas-runtime-evidence.json",
            ),
        ],
        "expected_test_count": 42,
        "lockfile_hash": "sha256:" + "3" * 64,
        "toolchain_digest": "sha256:" + "4" * 64,
    }


def test_compute_experiment_run_set_digest_is_order_independent_and_outcome_independent():
    """GIVEN two equivalent run-identity lists differing only in input
    order and in `conclusion` (outcome) WHEN the digest is computed THEN
    the result is identical -- order independence and outcome
    independence are both required (Issue #2422 AC5)."""
    runs_a = [
        {"block_id": "block-0001", "benchmark_layout": "monolith", "workflow_run_id": 100, "run_attempt": 1},
        {"block_id": "block-0001", "benchmark_layout": "split", "workflow_run_id": 101, "run_attempt": 1},
    ]
    runs_b = list(reversed(runs_a))
    assert collector.compute_experiment_run_set_digest(runs_a) == collector.compute_experiment_run_set_digest(runs_b)

    # A DIFFERENT run_attempt genuinely changes identity -- the digest must
    # NOT be insensitive to real identity changes (only to ordering/outcome).
    runs_c = [dict(r) for r in runs_a]
    runs_c[0]["run_attempt"] = 2
    assert collector.compute_experiment_run_set_digest(runs_a) != collector.compute_experiment_run_set_digest(runs_c)

    assert collector.compute_experiment_run_set_digest(runs_a).startswith("sha256:")


def test_compute_workflow_digest_from_commit_bytes_uses_contents_api_not_local_checkout():
    """GIVEN a fake Contents API response for a specific workflow_sha WHEN
    `compute_workflow_digest_from_commit_bytes` is called THEN it hashes
    the base64-decoded API response bytes (never a local file read) --
    proven by injecting a fake `api_call` that returns DIFFERENT content
    per ref and asserting the resulting digests differ accordingly."""
    import base64
    import hashlib

    def fake_api_call(endpoint: str):
        assert "ref=" in endpoint
        ref = endpoint.split("ref=")[-1]
        content = f"workflow-bytes-for-{ref}".encode("utf-8")
        return {"encoding": "base64", "content": base64.b64encode(content).decode("ascii")}

    sha_one = "1" * 40
    sha_two = "2" * 40
    digest_one = collector.compute_workflow_digest_from_commit_bytes(
        sha_one, "squne121/loop-protocol", api_call=fake_api_call
    )
    digest_two = collector.compute_workflow_digest_from_commit_bytes(
        sha_two, "squne121/loop-protocol", api_call=fake_api_call
    )
    assert digest_one != digest_two
    expected_one = "sha256:" + hashlib.sha256(f"workflow-bytes-for-{sha_one}".encode("utf-8")).hexdigest()
    assert digest_one == expected_one


def test_verify_workflow_digest_matches_commit_bytes_rejects_false_green_same_wrong_digest_both_arms():
    """GIVEN two arms that each independently claim the SAME digest, but
    that digest was computed from the WRONG commit's bytes (not the
    claimed workflow_sha's real bytes) WHEN
    `verify_workflow_digest_matches_commit_bytes` is called for each arm
    THEN both are rejected -- proving cross-arm equality ALONE is
    insufficient (Issue #2422 AC3 false-green rejection: a naive check
    that only compares the two arms' claimed digests to each other would
    incorrectly PASS this case)."""
    import base64

    real_sha = "3" * 40
    real_content = b"the-real-workflow-bytes"

    def fake_api_call(endpoint: str):
        assert endpoint.endswith(f"ref={real_sha}")
        return {"encoding": "base64", "content": base64.b64encode(real_content).decode("ascii")}

    wrong_digest = "sha256:" + "9" * 64  # NOT derived from real_content
    # Both arms claim the SAME wrong digest -- cross-arm equality would pass.
    runs = [
        {"workflow_sha": real_sha, "workflow_digest": wrong_digest},
        {"workflow_sha": real_sha, "workflow_digest": wrong_digest},
    ]
    assert collector.verify_cross_arm_required_equal(runs) == []  # cross-arm check alone: false green

    violations = collector.verify_workflow_digest_matches_commit_bytes(
        real_sha, wrong_digest, "squne121/loop-protocol", api_call=fake_api_call
    )
    assert any("workflow_digest_mismatch_vs_commit_bytes" in v for v in violations)


def test_verify_cross_arm_required_equal_detects_workflow_sha_and_digest_mismatch():
    """GIVEN two runs disagreeing on workflow_sha or workflow_digest WHEN
    verified THEN a violation is reported for each mismatched field
    (Issue #2422 AC1/AC3)."""
    runs = [
        {"workflow_sha": "a" * 40, "workflow_digest": "sha256:" + "0" * 64},
        {"workflow_sha": "b" * 40, "workflow_digest": "sha256:" + "1" * 64},
    ]
    violations = collector.verify_cross_arm_required_equal(runs)
    assert any("workflow_sha" in v for v in violations)
    assert any("workflow_digest" in v for v in violations)


@pytest.mark.parametrize(
    "image",
    [
        None,
        {},
        {"name": "", "version": "1.0"},
        {"name": "ubuntu-24.04", "version": ""},
        {"name": "unknown", "version": "unknown"},
        {"name": "unknown/unknown", "version": "1.0"},
    ],
)
def test_verify_exact_runner_image_rejects_placeholder_and_malformed_values(image):
    """GIVEN a missing/placeholder/malformed exact_runner_image WHEN
    verified THEN it is rejected (Issue #2422 AC4)."""
    assert collector.verify_exact_runner_image(image) != []


def test_verify_exact_runner_image_accepts_well_formed_value():
    assert collector.verify_exact_runner_image(_image()) == []


def _real_set_up_job_log_excerpt(version: str = "20260901.1.0") -> str:
    """Issue #2422 AC8 fix_delta (live smoke dispatch verification against
    real `gh api repos/{repo}/actions/jobs/{id}/logs` output, PR #2501):
    reproduces the ACTUAL GitHub-hosted runner job log shape (confirmed
    against 6 real job logs from the `blocks=2` AC8 smoke dispatch, e.g.
    workflow_job_id=101248519729) -- a `##[group]Runner Image Provisioner`
    section with its OWN decoy `Version:` line (the Hosted Compute Agent's
    version, never the runner image version) precedes the real
    `##[group]Runner Image` section, whose bare `Version:` line (never
    `Image Version:`, which does not occur in a real log) is the genuine
    runner image version this module must extract."""
    return (
        "Current runner version: '2.330.0'\n"
        "##[group]Runner Image Provisioner\n"
        "Hosted Compute Agent\n"
        "Version: 20260828.587\n"
        "##[endgroup]\n"
        "##[group]Runner Image\n"
        "Image: ubuntu-24.04\n"
        f"Version: {version}\n"
        "Included Software: https://github.com/actions/runner-images/blob/ubuntu24/example/Ubuntu2404-Readme.md\n"
        "##[endgroup]\n"
    )


def test_extract_exact_runner_image_from_job_log_parses_set_up_job_section():
    """GIVEN a realistic `Set up job` log excerpt (including the decoy
    `Runner Image Provisioner` section's OWN `Version:` line) WHEN parsed
    THEN name and version are extracted from the REAL `Runner Image` group
    only -- never the Provisioner's decoy version (Issue #2422 AC4/AC8)."""
    assert collector.extract_exact_runner_image_from_job_log(_real_set_up_job_log_excerpt()) == _image()


def test_extract_exact_runner_image_from_job_log_returns_none_when_absent():
    """GIVEN a log with no Image/Version lines (e.g. a containerized job)
    WHEN parsed THEN None is returned -- never a synthesized fallback
    (Issue #2422 AC4)."""
    assert collector.extract_exact_runner_image_from_job_log("no relevant lines here\n") is None


def test_fetch_exact_runner_image_for_job_raises_when_log_lacks_set_up_job_section():
    """GIVEN a job log with no parseable Set up job section WHEN fetched
    THEN LiveAPIError is raised (fail-closed, never a placeholder fallback,
    Issue #2422 AC4)."""

    def fake_log_fetch(workflow_job_id: int, repo: str) -> str:
        return "nothing relevant"

    with pytest.raises(collector.LiveAPIError):
        collector.fetch_exact_runner_image_for_job(123, "squne121/loop-protocol", fake_log_fetch)


def test_fetch_exact_runner_image_for_job_returns_parsed_image_on_success():
    def fake_log_fetch(workflow_job_id: int, repo: str) -> str:
        return _real_set_up_job_log_excerpt()

    assert collector.fetch_exact_runner_image_for_job(123, "squne121/loop-protocol", fake_log_fetch) == _image()


def test_verify_exact_runner_image_required_equal_within_block_detects_mismatch():
    """GIVEN a block whose monolith and split runs disagree on the CORE
    workload's exact_runner_image WHEN verified THEN a violation is
    reported (Issue #2422 AC4: required-equal is scoped to the SAME
    block, compared per WORKLOAD -- fix_delta Blocker 5)."""
    block = _block("block-0001", 100, 101)
    block["runs"][1]["provider_jobs"][0]["exact_runner_image"] = _image(version="99999.9.9")
    violations = collector.verify_exact_runner_image_required_equal_within_block(block)
    assert any("workload='core'" in v for v in violations)


def test_verify_exact_runner_image_within_block_detects_responsive_mismatch_asymmetric_topology():
    """Issue #2422 fix_delta Blocker 5 (OWNER REQUEST_CHANGES on PR #2501,
    issuecomment-5549966497): the REAL dispatched topology is asymmetric --
    `monolith` runs core+responsive sequentially inside ONE `e2e-core` job;
    `split` runs them as TWO parallel jobs (`e2e-core` + `e2e-responsive-
    matrix`). A job-NAME-based comparison (grouping same-name records
    across arms, requiring >= 2 per group) never has a monolith-side
    `e2e-responsive-matrix` record to compare against at all, so a
    responsive-workload-only image mismatch was silently INVISIBLE to the
    pre-fix_delta comparison (it returned `[]`, no violation). The
    corrected WORKLOAD-based comparison must detect it."""
    monolith_run = _run("monolith", 100, provider_jobs=[_provider_job("e2e-core", 1001)])
    split_run = _run(
        "split",
        101,
        provider_jobs=[
            _provider_job("e2e-core", 1011),
            _provider_job("e2e-responsive-matrix", 1012, image=_image(version="99999.9.9")),
        ],
    )
    block = {"block_id": "block-0001", "runs": [monolith_run, split_run]}
    violations = collector.verify_exact_runner_image_required_equal_within_block(block)
    assert any("workload='responsive'" in v for v in violations)
    # The core workload (both sides' e2e-core job) still agrees -- only
    # responsive differs; the fix must not report a spurious core mismatch.
    assert not any("workload='core'" in v for v in violations)


def test_verify_exact_runner_image_required_equal_within_block_accepts_real_monolith_split_asymmetric_evidence():
    """Issue #2422 fix_delta Blocker 5: genuine monolith(1 provider)/
    split(2 providers) evidence with IDENTICAL images across both arms
    must be accepted with zero violations -- the asymmetric provider
    COUNT itself is never a violation, only a genuine image disagreement
    per workload is."""
    monolith_run = _run("monolith", 100, provider_jobs=[_provider_job("e2e-core", 1001)])
    split_run = _run(
        "split",
        101,
        provider_jobs=[
            _provider_job("e2e-core", 1011),
            _provider_job("e2e-responsive-matrix", 1012),
        ],
    )
    block = {"block_id": "block-0001", "runs": [monolith_run, split_run]}
    assert collector.verify_exact_runner_image_required_equal_within_block(block) == []


def test_verify_exact_runner_image_required_equal_within_block_never_compares_across_blocks():
    """GIVEN two DIFFERENT blocks with DIFFERENT (but internally-consistent)
    runner images WHEN each block is verified independently THEN neither
    reports a violation -- cross-block image drift (e.g. a rolling GitHub
    hosted-runner image update between blocks) is explicitly NOT an
    invariant (Issue #2422 AC4/Outcome)."""
    block_one = _block("block-0001", 100, 101)
    block_two = _block("block-0002", 200, 201)
    for run in block_two["runs"]:
        for job in run["provider_jobs"]:
            job["exact_runner_image"] = _image(version="99999.9.9")
    assert collector.verify_exact_runner_image_required_equal_within_block(block_one) == []
    assert collector.verify_exact_runner_image_required_equal_within_block(block_two) == []


def test_verify_ab_alternating_order_accepts_monolith_then_split():
    blocks = [_block("block-0001", 100, 101)]
    assert collector.verify_ab_alternating_order(blocks) == []


def test_verify_ab_alternating_order_rejects_split_then_monolith():
    block = _block("block-0001", 100, 101)
    block["runs"] = list(reversed(block["runs"]))
    assert collector.verify_ab_alternating_order([block]) != []


def test_verify_block_ids_unique_detects_duplicate():
    blocks = [_block("block-0001", 100, 101), _block("block-0001", 200, 201)]
    violations = collector.verify_block_ids_unique(blocks)
    assert any("block-0001" in v for v in violations)


def test_verify_block_ids_unique_accepts_distinct_ids():
    blocks = [_block("block-0001", 100, 101), _block("block-0002", 200, 201)]
    assert collector.verify_block_ids_unique(blocks) == []


@pytest.mark.parametrize("blocks", [1, 2, 3, 22])
def test_build_ab_block_plan_handles_arbitrary_positive_blocks(blocks):
    """GIVEN `blocks` any positive int (including 22, Issue #2422 AC9)
    WHEN the plan is built THEN it contains exactly `blocks` matched
    (monolith, split) block entries with unique block_ids, and the plan
    itself never performs a live dispatch."""
    plan = collector.build_ab_block_plan(blocks)
    assert len(plan) == blocks
    assert len({b["block_id"] for b in plan}) == blocks
    for block in plan:
        assert block["layouts"] == ["monolith", "split"]


@pytest.mark.parametrize("invalid", [0, -1, -22, True, "22", 2.5, None])
def test_build_ab_block_plan_rejects_non_positive_int_blocks(invalid):
    with pytest.raises(collector.OperationalErrorV2):
        collector.build_ab_block_plan(invalid)


def _fake_dispatch_call_factory(next_run_id: list[int], return_details: bool = True):
    def fake_dispatch_call(repo, workflow_file, ref, inputs, return_run_details):
        assert return_run_details is True  # Issue #2422 AC7: must always be requested
        if not return_details:
            return {}  # simulate 204 No Content shape (no workflow_run_id)
        run_id = next_run_id[0]
        next_run_id[0] += 1
        return {"workflow_run_id": run_id, "html_url": f"https://example.invalid/runs/{run_id}"}

    return fake_dispatch_call


def test_dispatch_workflow_run_requires_return_run_details_and_extracts_workflow_run_id():
    fake_dispatch_call = _fake_dispatch_call_factory([500])
    result = collector.dispatch_workflow_run(
        "monolith",
        "block-0001",
        FROZEN_SOURCE_SHA,
        "exp-1",
        "squne121/loop-protocol",
        "ci.yml",
        "main",
        fake_dispatch_call,
    )
    assert result["workflow_run_id"] == 500
    assert result["benchmark_layout"] == "monolith"
    assert result["block_id"] == "block-0001"


def test_dispatch_workflow_run_sends_target_sha_not_frozen_source_sha_key():
    """fix_delta (test-runner live AC8 dispatch, HTTP 422 "Unexpected
    inputs provided: [\"frozen_source_sha\"...]"):
    `.github/workflows/ci.yml`'s `workflow_dispatch.inputs` has no
    `frozen_source_sha` key -- only the pre-existing `target_sha` input,
    which is the SAME "measured application-code commit" checkout
    selector. The `inputs` payload `dispatch_workflow_run` sends to
    `dispatch_call` must carry the frozen source SHA under the
    `target_sha` key the workflow actually declares, and must NEVER send
    an undeclared `frozen_source_sha` key (that undeclared-input shape is
    exactly what produced the live 422)."""
    captured_inputs: dict = {}

    def capturing_dispatch_call(repo, workflow_file, ref, inputs, return_run_details):
        captured_inputs.update(inputs)
        return {"workflow_run_id": 501, "html_url": "https://example.invalid/runs/501"}

    collector.dispatch_workflow_run(
        "monolith",
        "block-0001",
        FROZEN_SOURCE_SHA,
        "exp-1",
        "squne121/loop-protocol",
        "ci.yml",
        "main",
        capturing_dispatch_call,
    )
    assert "frozen_source_sha" not in captured_inputs
    assert captured_inputs["target_sha"] == FROZEN_SOURCE_SHA
    assert captured_inputs["benchmark_layout"] == "monolith"
    assert captured_inputs["block_id"] == "block-0001"
    assert captured_inputs["experiment_id"] == "exp-1"


def test_dispatch_workflow_run_fails_closed_when_response_lacks_workflow_run_id():
    """GIVEN a dispatch response shaped like GitHub's 204 No Content (no
    workflow_run_id) WHEN dispatched THEN LiveAPIError is raised --
    `gh run list` post-hoc guessing is never substituted (Issue #2422
    Stop Conditions)."""

    def fake_dispatch_call(repo, workflow_file, ref, inputs, return_run_details):
        return {}

    with pytest.raises(collector.LiveAPIError):
        collector.dispatch_workflow_run(
            "monolith",
            "block-0001",
            FROZEN_SOURCE_SHA,
            "exp-1",
            "squne121/loop-protocol",
            "ci.yml",
            "main",
            fake_dispatch_call,
        )


def test_dispatch_workflow_run_rejects_invalid_benchmark_layout():
    fake_dispatch_call = _fake_dispatch_call_factory([1])
    with pytest.raises(collector.OperationalErrorV2):
        collector.dispatch_workflow_run(
            "neither",
            "block-0001",
            FROZEN_SOURCE_SHA,
            "exp-1",
            "squne121/loop-protocol",
            "ci.yml",
            "main",
            fake_dispatch_call,
        )


def test_run_bounded_experiment_blocks_2_produces_4_run_ab_ab_root_run_set():
    """GIVEN blocks=2 WHEN the bounded orchestrator runs THEN it produces
    EXACTLY 4 dispatches in monolith, split, monolith, split order across
    2 distinct block_ids -- the exact live smoke shape Issue #2422 AC8
    requires."""
    fake_dispatch_call = _fake_dispatch_call_factory([1000])
    root_run_set = collector.run_bounded_experiment(
        2, FROZEN_SOURCE_SHA, "exp-1", "squne121/loop-protocol", "ci.yml", "main", fake_dispatch_call
    )
    assert len(root_run_set) == 4
    assert [r["benchmark_layout"] for r in root_run_set] == ["monolith", "split", "monolith", "split"]
    assert len({r["block_id"] for r in root_run_set}) == 2
    assert len({r["workflow_run_id"] for r in root_run_set}) == 4


def test_run_bounded_experiment_blocks_22_produces_44_run_root_run_set_deterministically():
    """Issue #2422 AC9: `blocks=22` (monolith 22 + split 22 = 44 eligible
    unique workflow runs) is proven structurally correct WITHOUT any live
    dispatch -- this test never contacts a real GitHub API (Stop
    Conditions: blocks=22 live dispatch is explicitly out of this Issue's
    scope)."""
    fake_dispatch_call = _fake_dispatch_call_factory([1])
    root_run_set = collector.run_bounded_experiment(
        22, FROZEN_SOURCE_SHA, "exp-1", "squne121/loop-protocol", "ci.yml", "main", fake_dispatch_call
    )
    assert len(root_run_set) == 44
    monolith_count = sum(1 for r in root_run_set if r["benchmark_layout"] == "monolith")
    split_count = sum(1 for r in root_run_set if r["benchmark_layout"] == "split")
    assert monolith_count == 22
    assert split_count == 22
    assert len({r["block_id"] for r in root_run_set}) == 22
    # A/B/A/B: layouts strictly alternate starting with monolith.
    layouts = [r["benchmark_layout"] for r in root_run_set]
    assert layouts == ["monolith", "split"] * 22
    # Each block_id appears exactly twice: once monolith, once split.
    by_block: dict[str, set[str]] = {}
    for r in root_run_set:
        by_block.setdefault(r["block_id"], set()).add(r["benchmark_layout"])
    assert all(layouts_for_block == {"monolith", "split"} for layouts_for_block in by_block.values())


def _fake_get_run_status_factory(conclusion_by_run_id: dict[int, str], polls_until_terminal: int = 1):
    """Issue #2422 fix_delta Blocker 2: a fake `get_run_status` that reports
    `status: "queued"` for `polls_until_terminal - 1` calls per run id, then
    `status: "completed"` with the configured `conclusion` -- proves
    `wait_for_run_terminal` actually POLLS (never trusts the first response
    blindly) without any real sleeping (the injected `sleep` is a no-op in
    these tests)."""
    call_count: dict[int, int] = {}

    def fake_get_run_status(workflow_run_id: int, repo: str) -> dict:
        call_count[workflow_run_id] = call_count.get(workflow_run_id, 0) + 1
        if call_count[workflow_run_id] < polls_until_terminal:
            return {"status": "queued"}
        return {
            "status": "completed",
            "conclusion": conclusion_by_run_id.get(workflow_run_id, "success"),
            "run_attempt": 1,
        }

    return fake_get_run_status


def _fake_list_run_jobs_factory(jobs_by_run_id: dict[int, list[dict]]):
    def fake_list_run_jobs(workflow_run_id: int, repo: str) -> list[dict]:
        return jobs_by_run_id.get(workflow_run_id, [])

    return fake_list_run_jobs


def _fake_log_fetch(workflow_job_id: int, repo: str) -> str:
    return _real_set_up_job_log_excerpt()


def test_wait_for_run_terminal_polls_until_completed_and_returns_final_status():
    """Issue #2422 fix_delta Blocker 2: `wait_for_run_terminal` polls
    `get_run_status` repeatedly (never accepting a non-`completed` status
    as done) and returns the run's FINAL status dict once `status ==
    "completed"`."""
    fake_get_run_status = _fake_get_run_status_factory({100: "success"}, polls_until_terminal=3)
    sleeps: list[float] = []
    result = collector.wait_for_run_terminal(
        100,
        "squne121/loop-protocol",
        fake_get_run_status,
        poll_interval_seconds=0.01,
        max_polls=10,
        sleep=sleeps.append,
    )
    assert result["status"] == "completed"
    assert result["conclusion"] == "success"
    assert len(sleeps) == 2  # slept between poll 1->2 and 2->3, never after the terminal poll


def test_wait_for_run_terminal_times_out_fail_closed_never_polls_forever():
    """A run that NEVER reaches `completed` within `max_polls` raises
    `LiveAPIError` -- this must be a BOUNDED wait, never an infinite loop."""

    def fake_get_run_status(workflow_run_id: int, repo: str) -> dict:
        return {"status": "in_progress"}

    with pytest.raises(collector.LiveAPIError):
        collector.wait_for_run_terminal(
            100,
            "squne121/loop-protocol",
            fake_get_run_status,
            poll_interval_seconds=0.0,
            max_polls=3,
            sleep=lambda s: None,
        )


def test_collect_run_provider_jobs_excludes_skipped_provider_never_synthesizes_evidence():
    """Issue #2422 fix_delta Blocker 2/Blocker 7: a monolith run's live
    jobs API response has `e2e-responsive-matrix` reporting `conclusion:
    "skipped"` (expected -- monolith never starts that job) -- this must
    be EXCLUDED from `provider_jobs` (never given a fabricated
    `exact_runner_image`), while `job_names_started` still records its
    name as evidence that it was at least considered/reported."""
    jobs_by_run_id = {
        100: [
            {"id": 9001, "name": "e2e-core", "conclusion": "success"},
            {"id": 9002, "name": "e2e-responsive-matrix", "conclusion": "skipped"},
            {"id": 9003, "name": "e2e", "conclusion": "success"},
        ]
    }
    fake_list_run_jobs = _fake_list_run_jobs_factory(jobs_by_run_id)
    provider_jobs, job_names_started = collector.collect_run_provider_jobs(
        100, "squne121/loop-protocol", fake_list_run_jobs, _fake_log_fetch
    )
    assert [j["job"] for j in provider_jobs] == ["e2e-core"]
    assert provider_jobs[0]["exact_runner_image"] == _image()
    assert job_names_started == ["e2e", "e2e-core", "e2e-responsive-matrix"]


def test_collect_run_provider_jobs_split_layout_collects_both_providers():
    jobs_by_run_id = {
        101: [
            {"id": 9101, "name": "e2e-core", "conclusion": "success"},
            {"id": 9102, "name": "e2e-responsive-matrix", "conclusion": "success"},
            {"id": 9103, "name": "e2e", "conclusion": "success"},
        ]
    }
    fake_list_run_jobs = _fake_list_run_jobs_factory(jobs_by_run_id)
    provider_jobs, job_names_started = collector.collect_run_provider_jobs(
        101, "squne121/loop-protocol", fake_list_run_jobs, _fake_log_fetch
    )
    assert sorted(j["job"] for j in provider_jobs) == ["e2e-core", "e2e-responsive-matrix"]
    assert all(j["exact_runner_image"]["name"] and j["exact_runner_image"]["version"] for j in provider_jobs)


def test_write_json_atomic_never_leaves_a_partial_file_and_is_readable_back(tmp_path):
    target = tmp_path / "progress.json"
    collector._write_json_atomic(str(target), {"hello": "world"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"hello": "world"}
    # A second write REPLACES atomically -- no leftover .tmp-* file.
    collector._write_json_atomic(str(target), {"hello": "world2"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"hello": "world2"}
    assert list(tmp_path.glob("*.tmp-*")) == []


def test_execute_bounded_experiment_to_manifest_v2_connects_dispatch_wait_collect_and_build(tmp_path):
    """Issue #2422 fix_delta Blocker 2/Blocker 3: the FULL pipeline --
    dispatch -> incremental persistence -> wait-to-terminal -> per-job
    Runner Image collection -> manifest v2 build+validate -- connected in
    ONE call, using a `blocks=2` (4-run, AC8-smoke-shaped) fake
    experiment. `workflow_digest` must be computed via the injected
    commit-bytes function (fix_delta Blocker 3), never a bare literal."""
    fake_dispatch_call = _fake_dispatch_call_factory([1])
    jobs_by_run_id = {
        1: [{"id": 101, "name": "e2e-core", "conclusion": "success"}],  # monolith block-0001
        2: [
            {"id": 201, "name": "e2e-core", "conclusion": "success"},
            {"id": 202, "name": "e2e-responsive-matrix", "conclusion": "success"},
        ],  # split block-0001
        3: [{"id": 301, "name": "e2e-core", "conclusion": "success"}],  # monolith block-0002
        4: [
            {"id": 401, "name": "e2e-core", "conclusion": "success"},
            {"id": 402, "name": "e2e-responsive-matrix", "conclusion": "success"},
        ],  # split block-0002
    }
    fake_get_run_status = _fake_get_run_status_factory({1: "success", 2: "success", 3: "success", 4: "success"})
    fake_list_run_jobs = _fake_list_run_jobs_factory(jobs_by_run_id)

    import base64
    import hashlib

    contents_api_calls: list[str] = []
    workflow_bytes = b"name: ci\non: [push]\n"
    expected_digest = "sha256:" + hashlib.sha256(workflow_bytes).hexdigest()

    def fake_contents_api_call(endpoint: str):
        contents_api_calls.append(endpoint)
        assert f"ref={V2_WORKFLOW_SHA}" in endpoint
        return {"encoding": "base64", "content": base64.b64encode(workflow_bytes).decode("ascii")}

    output_path = tmp_path / "progress.json"
    manifest = collector.execute_bounded_experiment_to_manifest_v2(
        blocks=2,
        frozen_source_sha=FROZEN_SOURCE_SHA,
        experiment_id="exp-smoke-1",
        repo="squne121/loop-protocol",
        workflow_file="ci.yml",
        ref="main",
        workflow_sha=V2_WORKFLOW_SHA,
        frozen_non_treatment=_frozen_non_treatment(),
        root_run_set_output=str(output_path),
        dispatch_call=fake_dispatch_call,
        get_run_status=fake_get_run_status,
        list_run_jobs=fake_list_run_jobs,
        log_fetch=_fake_log_fetch,
        contents_api_call=fake_contents_api_call,
        poll_interval_seconds=0.0,
        max_polls=5,
        sleep=lambda s: None,
    )

    assert manifest["schema"] == "e2e_performance_benchmark_manifest_v2"
    assert manifest["workflow_digest"] == expected_digest
    # computed once (build) + recomputed once (verify) -- both via the SAME
    # injected commit-bytes transport, never a bare literal/local sha256sum.
    assert len(contents_api_calls) == 2
    assert len(manifest["blocks"]) == 2
    for block in manifest["blocks"]:
        assert [r["benchmark_layout"] for r in block["runs"]] == ["monolith", "split"]
        for run in block["runs"]:
            assert run["workflow_digest"] == expected_digest
            assert run["provider_jobs"]

    # AC6/Blocker 2 partial-write requirement: the progress file exists and
    # was written incrementally (not merely at the very end).
    progress = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(progress["root_run_set"]) == 4

    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(V2_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    assert list(validator.iter_errors(manifest)) == []
    assert manifest["evidence_errors"] == []


def test_execute_bounded_experiment_to_manifest_v2_never_re_dispatches_on_resume(tmp_path):
    """Issue #2422 fix_delta Blocker 2 resume semantics: a `(block_id,
    benchmark_layout)` pair already present in `resume_dispatched_run_set`
    is NEVER re-dispatched -- only the missing pairs get a fresh
    dispatch."""
    dispatch_calls: list[tuple[str, str]] = []

    def counting_dispatch_call(repo, workflow_file, ref, inputs, return_run_details):
        dispatch_calls.append((inputs["block_id"], inputs["benchmark_layout"]))
        run_id = 100 + len(dispatch_calls)
        return {"workflow_run_id": run_id, "html_url": f"https://example.invalid/runs/{run_id}"}

    resume_run_set = [
        {"workflow_run_id": 1, "run_url": "u1", "benchmark_layout": "monolith", "block_id": "block-0001"},
    ]
    jobs_by_run_id = {
        1: [{"id": 1001, "name": "e2e-core", "conclusion": "success"}],
        101: [
            {"id": 1101, "name": "e2e-core", "conclusion": "success"},
            {"id": 1102, "name": "e2e-responsive-matrix", "conclusion": "success"},
        ],
    }
    fake_get_run_status = _fake_get_run_status_factory({1: "success", 101: "success"})
    fake_list_run_jobs = _fake_list_run_jobs_factory(jobs_by_run_id)

    import base64

    def fake_contents_api_call(endpoint: str):
        return {"encoding": "base64", "content": base64.b64encode(b"workflow-bytes").decode("ascii")}

    manifest = collector.execute_bounded_experiment_to_manifest_v2(
        blocks=1,
        frozen_source_sha=FROZEN_SOURCE_SHA,
        experiment_id="exp-resume-1",
        repo="squne121/loop-protocol",
        workflow_file="ci.yml",
        ref="main",
        workflow_sha=V2_WORKFLOW_SHA,
        frozen_non_treatment=_frozen_non_treatment(),
        root_run_set_output=str(tmp_path / "progress.json"),
        dispatch_call=counting_dispatch_call,
        get_run_status=fake_get_run_status,
        list_run_jobs=fake_list_run_jobs,
        log_fetch=_fake_log_fetch,
        contents_api_call=fake_contents_api_call,
        poll_interval_seconds=0.0,
        max_polls=5,
        sleep=lambda s: None,
        resume_dispatched_run_set=resume_run_set,
    )
    # Only the SPLIT half of block-0001 was missing -- exactly one new dispatch.
    assert dispatch_calls == [("block-0001", "split")]
    assert len(manifest["blocks"]) == 1
    assert [r["workflow_run_id"] for r in manifest["blocks"][0]["runs"]] == [1, 101]


def test_build_manifest_v2_happy_path_conforms_to_schema():
    """GIVEN a well-formed 2-block (blocks=2 smoke-shaped) run set WHEN the
    v2 manifest is built THEN it conforms to
    schemas/e2e_performance_benchmark_manifest_v2.schema.json with zero
    evidence_errors (Issue #2422 AC5)."""
    jsonschema = pytest.importorskip("jsonschema")
    blocks = [
        _block("block-0001", 1001, 1002),
        _block("block-0002", 1003, 1004),
    ]
    manifest = collector.build_manifest_v2(
        experiment_identity="exp-smoke-1",
        frozen_source_sha=FROZEN_SOURCE_SHA,
        workflow_sha=V2_WORKFLOW_SHA,
        workflow_digest=V2_WORKFLOW_DIGEST,
        frozen_non_treatment=_frozen_non_treatment(),
        blocks=blocks,
        generated_at="2026-09-05T00:00:00Z",
    )
    assert manifest["schema"] == "e2e_performance_benchmark_manifest_v2"
    assert manifest["evidence_errors"] == []
    assert manifest["experiment_run_set_digest"].startswith("sha256:")

    schema = json.loads(V2_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(manifest))
    assert errors == [], [e.message for e in errors]

    assert collector.validate_manifest_v2_semantics(manifest) == []


def test_build_manifest_v2_reports_evidence_errors_never_raises_for_ab_order_violation():
    blocks = [_block("block-0001", 1001, 1002)]
    blocks[0]["runs"] = list(reversed(blocks[0]["runs"]))
    manifest = collector.build_manifest_v2(
        experiment_identity="exp-smoke-1",
        frozen_source_sha=FROZEN_SOURCE_SHA,
        workflow_sha=V2_WORKFLOW_SHA,
        workflow_digest=V2_WORKFLOW_DIGEST,
        frozen_non_treatment=_frozen_non_treatment(),
        blocks=blocks,
        generated_at="2026-09-05T00:00:00Z",
    )
    assert any(e["reason"] == "ab_order_violation" for e in manifest["evidence_errors"])


def test_build_manifest_v2_reports_evidence_errors_for_cross_arm_workflow_sha_mismatch():
    blocks = [_block("block-0001", 1001, 1002)]
    blocks[0]["runs"][1]["workflow_sha"] = "f" * 40
    manifest = collector.build_manifest_v2(
        experiment_identity="exp-smoke-1",
        frozen_source_sha=FROZEN_SOURCE_SHA,
        workflow_sha=V2_WORKFLOW_SHA,
        workflow_digest=V2_WORKFLOW_DIGEST,
        frozen_non_treatment=_frozen_non_treatment(),
        blocks=blocks,
        generated_at="2026-09-05T00:00:00Z",
    )
    assert any(e["reason"] == "cross_arm_fingerprint_mismatch" for e in manifest["evidence_errors"])


def test_build_manifest_v2_reports_evidence_errors_for_placeholder_exact_runner_image():
    blocks = [_block("block-0001", 1001, 1002)]
    blocks[0]["runs"][0]["provider_jobs"][0]["exact_runner_image"] = {"name": "unknown", "version": "unknown"}
    manifest = collector.build_manifest_v2(
        experiment_identity="exp-smoke-1",
        frozen_source_sha=FROZEN_SOURCE_SHA,
        workflow_sha=V2_WORKFLOW_SHA,
        workflow_digest=V2_WORKFLOW_DIGEST,
        frozen_non_treatment=_frozen_non_treatment(),
        blocks=blocks,
        generated_at="2026-09-05T00:00:00Z",
    )
    assert any(e["reason"] == "exact_runner_image_invalid" for e in manifest["evidence_errors"])


def test_validate_manifest_v2_semantics_detects_tampered_experiment_run_set_digest():
    """GIVEN a manifest whose `experiment_run_set_digest` was tampered
    with (no longer matches a recomputation from blocks[].runs[]) WHEN
    validated THEN a mismatch violation is reported (Issue #2422 AC5)."""
    blocks = [_block("block-0001", 1001, 1002)]
    manifest = collector.build_manifest_v2(
        experiment_identity="exp-smoke-1",
        frozen_source_sha=FROZEN_SOURCE_SHA,
        workflow_sha=V2_WORKFLOW_SHA,
        workflow_digest=V2_WORKFLOW_DIGEST,
        frozen_non_treatment=_frozen_non_treatment(),
        blocks=blocks,
        generated_at="2026-09-05T00:00:00Z",
    )
    manifest["experiment_run_set_digest"] = "sha256:" + "0" * 64
    violations = collector.validate_manifest_v2_semantics(manifest)
    assert any("experiment_run_set_digest_mismatch" in v for v in violations)


def test_verify_workflow_run_id_global_uniqueness_detects_run_id_reused_across_blocks():
    """Issue #2422 fix_delta Blocker 6: the SAME `workflow_run_id` (e.g.
    from copy-pasting 2 real runs into all 22 blocks of a `blocks=22`
    experiment) appearing under more than one `block_id` is a fail-closed
    identity violation."""
    blocks = [
        _block("block-0001", 1001, 1002),
        _block("block-0002", 1001, 1003),  # 1001 reused from block-0001's monolith run
    ]
    violations = collector.verify_workflow_run_id_global_uniqueness(blocks)
    assert any("workflow_run_id=1001" in v for v in violations)

    manifest = collector.build_manifest_v2(
        experiment_identity="exp-smoke-1",
        frozen_source_sha=FROZEN_SOURCE_SHA,
        workflow_sha=V2_WORKFLOW_SHA,
        workflow_digest=V2_WORKFLOW_DIGEST,
        frozen_non_treatment=_frozen_non_treatment(),
        blocks=blocks,
        generated_at="2026-09-05T00:00:00Z",
    )
    assert any(e["reason"] == "workflow_run_id_reused_across_blocks" for e in manifest["evidence_errors"])
    assert any(
        "workflow_run_id_reused_across_blocks" in v for v in collector.validate_manifest_v2_semantics(manifest)
    )


def test_verify_workflow_run_id_global_uniqueness_accepts_disjoint_ids_across_many_blocks():
    """GIVEN 22 blocks with fully disjoint workflow_run_ids (the genuine
    blocks=22 shape) WHEN checked THEN zero violations are reported."""
    blocks = [_block(f"block-{i:04d}", 1000 + 2 * i, 1001 + 2 * i) for i in range(1, 23)]
    assert collector.verify_workflow_run_id_global_uniqueness(blocks) == []


def test_verify_required_provider_jobs_present_for_layout_detects_missing_split_responsive_job():
    """Issue #2422 fix_delta Blocker 6: a `split` run whose `provider_jobs`
    is missing `e2e-responsive-matrix` entirely (not merely a non-success
    conclusion for it -- structurally absent) is rejected."""
    run = _run("split", 200, provider_jobs=[_provider_job("e2e-core", 2001)])
    violations = collector.verify_required_provider_jobs_present_for_layout(run)
    assert any("e2e-responsive-matrix" in v for v in violations)


def test_verify_required_provider_jobs_present_for_layout_accepts_genuine_monolith_single_provider():
    """A genuine monolith run (single e2e-core provider) satisfies its own
    layout's required-provider-jobs composition."""
    run = _run("monolith", 200, provider_jobs=[_provider_job("e2e-core", 2001)])
    assert collector.verify_required_provider_jobs_present_for_layout(run) == []


def test_verify_success_run_has_provider_job_evidence_rejects_success_with_empty_provider_jobs():
    """Issue #2422 fix_delta Blocker 6: `conclusion: success` with
    `provider_jobs: []` is a fail-closed contradiction -- rejected by the
    dedicated semantic check (independent of, and in addition to, the
    schema's own `minItems: 1` structural gate on `provider_jobs`)."""
    run = _run("monolith", 200, provider_jobs=[])
    run["conclusion"] = "success"
    violations = collector.verify_success_run_has_provider_job_evidence(run)
    assert any("success_conclusion_missing_provider_jobs_evidence" in v for v in violations)


def test_verify_success_run_has_provider_job_evidence_accepts_non_success_with_empty_provider_jobs():
    """A NON-success conclusion (e.g. a wholly-cancelled dispatch) with
    empty `provider_jobs` is not itself a contradiction this check flags
    -- only a claimed `success` with zero evidence is."""
    run = _run("monolith", 200, provider_jobs=[])
    run["conclusion"] = "cancelled"
    assert collector.verify_success_run_has_provider_job_evidence(run) == []


def test_manifest_v2_schema_rejects_bare_integer_expected_playwright_invocations():
    """Issue #2422 fix_delta Blocker 4 (OWNER REQUEST_CHANGES on PR #2501,
    issuecomment-5549966497): `expected_playwright_invocations` must be a
    LIST of identifiable invocations (id/lane/provider_placement/
    evidence_file) -- a bare integer test-count (the pre-fix_delta shape)
    cannot express the per-lane invocation identity #2424 needs and is now
    rejected by the schema."""
    jsonschema = pytest.importorskip("jsonschema")
    blocks = [_block("block-0001", 1001, 1002)]
    manifest = collector.build_manifest_v2(
        experiment_identity="exp-smoke-1",
        frozen_source_sha=FROZEN_SOURCE_SHA,
        workflow_sha=V2_WORKFLOW_SHA,
        workflow_digest=V2_WORKFLOW_DIGEST,
        frozen_non_treatment=_frozen_non_treatment(),
        blocks=blocks,
        generated_at="2026-09-05T00:00:00Z",
    )
    manifest["frozen_non_treatment"]["expected_playwright_invocations"] = 42
    schema = json.loads(V2_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(manifest))
    assert errors != []


def test_manifest_v2_schema_expected_playwright_invocations_carries_provider_placement_and_evidence_file():
    """Issue #2422 fix_delta Blocker 4: each invocation entry must bind a
    logical `invocation_id`/`lane` identity to its ARM-DEPENDENT physical
    `provider_placement` (monolith vs split run it under different
    provider jobs without changing invocation identity) and to the
    `evidence_file` its evidence is expected under."""
    jsonschema = pytest.importorskip("jsonschema")
    blocks = [_block("block-0001", 1001, 1002)]
    manifest = collector.build_manifest_v2(
        experiment_identity="exp-smoke-1",
        frozen_source_sha=FROZEN_SOURCE_SHA,
        workflow_sha=V2_WORKFLOW_SHA,
        workflow_digest=V2_WORKFLOW_DIGEST,
        frozen_non_treatment=_frozen_non_treatment(),
        blocks=blocks,
        generated_at="2026-09-05T00:00:00Z",
    )
    invocations = manifest["frozen_non_treatment"]["expected_playwright_invocations"]
    assert len(invocations) >= 1
    responsive = next(i for i in invocations if i["lane"] == "responsive")
    assert responsive["provider_placement"]["monolith"] == "e2e-core"
    assert responsive["provider_placement"]["split"] == "e2e-responsive-matrix"
    assert responsive["evidence_file"]

    schema = json.loads(V2_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    assert list(validator.iter_errors(manifest)) == []

    # A missing provider_placement arm (e.g. `split` omitted) must be rejected --
    # both arms need evidence-binding regardless of which one produced a run.
    invocations[0]["provider_placement"].pop("split")
    errors = list(validator.iter_errors(manifest))
    assert errors != []


def test_manifest_v2_generation_and_verification_path_rejects_false_green_wrong_commit_digest():
    """Issue #2422 fix_delta Blocker 3 (OWNER REQUEST_CHANGES on PR #2501,
    issuecomment-5549966497): a regression test at the REAL manifest
    generation+verification path -- `build_manifest_v2`'s output combined
    with `verify_workflow_digest_matches_commit_bytes` (exactly as
    `execute_bounded_experiment_to_manifest_v2` wires them together into
    the `run-experiment` execution flow), not merely the standalone helper
    tested in isolation. Both arms of a block report the SAME wrong
    digest (as would happen if a caller computed it from a local checkout
    at `head_sha` instead of `workflow_sha`'s own commit bytes) --
    cross-arm equality ALONE (`verify_cross_arm_required_equal`, already
    exercised inside `build_manifest_v2`) does NOT catch this; only
    independent commit-bytes recomputation does."""
    wrong_digest = "sha256:" + "9" * 64
    blocks = [_block("block-0001", 1001, 1002)]
    for block in blocks:
        for run in block["runs"]:
            run["workflow_digest"] = wrong_digest

    manifest = collector.build_manifest_v2(
        experiment_identity="exp-smoke-1",
        frozen_source_sha=FROZEN_SOURCE_SHA,
        workflow_sha=V2_WORKFLOW_SHA,
        workflow_digest=wrong_digest,
        frozen_non_treatment=_frozen_non_treatment(),
        blocks=blocks,
        generated_at="2026-09-05T00:00:00Z",
    )
    # Cross-arm-only check PASSES (both arms agree on the SAME wrong value) --
    # this is the exact false-green build_manifest_v2/validate_manifest_v2_semantics
    # alone cannot reject (AC3's explicitly-called-out defect).
    assert manifest["evidence_errors"] == []
    assert collector.validate_manifest_v2_semantics(manifest) == []

    # The REAL commit bytes at workflow_sha are DIFFERENT from wrong_digest --
    # independent recomputation (as execute_bounded_experiment_to_manifest_v2
    # wires it into the run-experiment execution path) rejects it.
    import base64

    def fake_contents_api_call(endpoint: str):
        assert f"ref={V2_WORKFLOW_SHA}" in endpoint
        return {"encoding": "base64", "content": base64.b64encode(b"the-real-workflow-file-bytes").decode("ascii")}

    violations = collector.verify_workflow_digest_matches_commit_bytes(
        manifest["workflow_sha"],
        manifest["workflow_digest"],
        "squne121/loop-protocol",
        api_call=fake_contents_api_call,
    )
    assert violations != []
    assert any("workflow_digest_mismatch_vs_commit_bytes" in v for v in violations)


def test_manifest_v2_schema_rejects_v1_hybrid_before_after_fields():
    """GIVEN a manifest that still carries v1's `before_sha`/`after_sha`
    hybrid fields WHEN validated against the v2 schema THEN it is
    rejected (`unevaluatedProperties: false`) -- Issue #2422 Outcome: v1
    hybrid semantics are removed from the benchmark route, never silently
    tolerated as extra keys on a v2 manifest."""
    jsonschema = pytest.importorskip("jsonschema")
    blocks = [_block("block-0001", 1001, 1002)]
    manifest = collector.build_manifest_v2(
        experiment_identity="exp-smoke-1",
        frozen_source_sha=FROZEN_SOURCE_SHA,
        workflow_sha=V2_WORKFLOW_SHA,
        workflow_digest=V2_WORKFLOW_DIGEST,
        frozen_non_treatment=_frozen_non_treatment(),
        blocks=blocks,
        generated_at="2026-09-05T00:00:00Z",
    )
    manifest["before_sha"] = BEFORE_SHA
    manifest["after_sha"] = AFTER_SHA
    schema = json.loads(V2_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(validator.iter_errors(manifest))
    assert errors != []
