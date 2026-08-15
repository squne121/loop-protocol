"""
scripts/ci/tests/e2e_performance_benchmark/test_collect_e2e_performance_benchmark_rerun_attempt.py

Issue #2179 (P1-1 rerun-attempt-selection follow-up to #2159/PR #2172,
OWNER adversarial review issuecomment-5295659213): regression tests for
`scripts/ci/collect_e2e_performance_benchmark.py`'s `initial_attempt_only_v1`
rerun-attempt selection policy -- order-independent dedupe/selection
(AC1/AC3), manifest `run_attempt`/`rerun_attempt_selection_policy`
recording (AC4), trusted live-API binding for an explicit `run_attempt`
(AC5), parametrized order-independence including canonical `runs` sort
(AC7), full artifact-listing pagination (AC8), and `run_attempt` type
normalization / identity-collision fail-closed handling (AC9).
"""
from __future__ import annotations

import copy
import importlib.util
import pathlib
import random

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
    run_attempt: int | None | str = None,
) -> dict:
    record = {
        "workflow_run_id": workflow_run_id,
        "job": job,
        "head_sha": head_sha,
        "artifact_id": artifact_id if artifact_id is not None else workflow_run_id,
        "artifact_digest": artifact_digest or ("sha256:" + f"{workflow_run_id:064x}"),
        "conclusion": conclusion,
        "workflow_digest": workflow_digest,
        "workflow_sha": workflow_sha,
    }
    if run_attempt is not None:
        record["run_attempt"] = run_attempt
    return record


def _full_job_set_records(count: int, head_sha: str, start_id: int = 1) -> list[dict]:
    records = []
    for i in range(count):
        run_id = start_id + i
        for job in ("e2e-core", "e2e-responsive-matrix", "e2e"):
            records.append(_record(run_id, job=job, head_sha=head_sha))
    return records


# --------------------------------------------------------------------------- #
# AC1: order-independent dedupe.
# --------------------------------------------------------------------------- #
def test_dedupe_by_workflow_run_id_order_independent():
    """GIVEN attempt-1 (success) and attempt-2 (success) records for the
    SAME workflow_run_id, presented in both orders, WHEN deduped THEN the
    attempt-1 record is always selected regardless of input order
    (initial_attempt_only_v1, AC1) -- never a "first/last seen wins"
    artifact of list order."""
    attempt_1 = _record(700, artifact_id=1, artifact_digest="sha256:" + "1" * 64, run_attempt=1)
    attempt_2 = _record(700, artifact_id=2, artifact_digest="sha256:" + "2" * 64, run_attempt=2)

    forward = collector._dedupe_by_workflow_run_id([attempt_1, attempt_2])
    reverse = collector._dedupe_by_workflow_run_id([attempt_2, attempt_1])

    assert len(forward) == 1
    assert len(reverse) == 1
    assert forward[0]["artifact_id"] == 1
    assert reverse[0]["artifact_id"] == 1
    assert forward == reverse


def test_dedupe_excludes_sample_when_attempt_1_failed_never_substitutes_later_success():
    """GIVEN attempt 1 FAILED and attempt 2 SUCCEEDED for the same
    workflow_run_id WHEN deduped THEN the workflow_run_id is excluded
    entirely -- attempt 2's success is NEVER substituted in for a failed
    attempt 1 (#2179 P0-4)."""
    attempt_1_failed = _record(701, artifact_id=10, conclusion="failure", run_attempt=1)
    attempt_2_success = _record(701, artifact_id=11, conclusion="success", run_attempt=2)

    deduped = collector._dedupe_by_workflow_run_id([attempt_1_failed, attempt_2_success])
    assert len(deduped) == 1
    assert deduped[0]["artifact_id"] == 10
    assert deduped[0]["conclusion"] == "failure"


# --------------------------------------------------------------------------- #
# AC3: policy is a single explicit rule, not first/last-seen-wins.
# --------------------------------------------------------------------------- #
def test_selection_policy_is_initial_attempt_only_v1():
    """GIVEN the module-level policy constant WHEN inspected THEN it is
    the single explicit string `initial_attempt_only_v1` (AC3)."""
    assert collector.RERUN_ATTEMPT_SELECTION_POLICY == "initial_attempt_only_v1"


def test_selection_never_relies_on_setdefault_first_seen_semantics():
    """GIVEN records for TWO workflow_run_ids each with an attempt-1 AND
    attempt-2 record, in an order where the attempt-2 record for EACH id
    comes first WHEN deduped THEN both selected records are the attempt-1
    ones (not the first-seen attempt-2 records) -- proves selection is
    attempt-based, not insertion-order-based (AC3)."""
    records = [
        _record(710, artifact_id=2, run_attempt=2),
        _record(710, artifact_id=1, run_attempt=1),
        _record(711, artifact_id=4, run_attempt=2),
        _record(711, artifact_id=3, run_attempt=1),
    ]
    deduped = collector._dedupe_by_workflow_run_id(records)
    assert len(deduped) == 2
    by_id = {r["workflow_run_id"]: r for r in deduped}
    assert by_id[710]["artifact_id"] == 1
    assert by_id[711]["artifact_id"] == 3


# --------------------------------------------------------------------------- #
# AC4: run_attempt / rerun_attempt_selection_policy recorded in the manifest,
# schema-conformant.
# --------------------------------------------------------------------------- #
def test_run_attempt_recorded_schema_conformant():
    """GIVEN a manifest collected from explicit-attempt records WHEN built
    THEN every run entry carries `run_attempt: 1` and
    `rerun_attempt_selection_policy: "initial_attempt_only_v1"`, and the
    manifest passes schema validation (AC4)."""
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
    collector._validate_against_schema(manifest)

    for arm_name in ("before", "after"):
        for job_cohort in manifest["arms"][arm_name]["jobs"].values():
            for run in job_cohort["runs"]:
                assert run["run_attempt"] == 1
                assert run["rerun_attempt_selection_policy"] == "initial_attempt_only_v1"


# --------------------------------------------------------------------------- #
# AC5: trusted binding against a live attempt-specific jobs API.
# --------------------------------------------------------------------------- #
def _fake_artifacts_response(artifacts: list[dict]) -> dict:
    return {"artifacts": artifacts}


def test_run_attempt_trusted_binding_accepts_genuine_attempt():
    """GIVEN a record explicitly claiming run_attempt=1, whose claimed
    artifact matches the live artifacts API AND whose attempt-specific
    jobs API confirms a real job for attempt 1, WHEN verified THEN there
    are no violations (AC5)."""
    record = _record(800, artifact_id=555, artifact_digest="sha256:" + "5" * 64, run_attempt=1)

    def fake_api_call(endpoint: str) -> dict:
        if endpoint == "repos/owner/repo/actions/runs/800/artifacts":
            return _fake_artifacts_response(
                [{"id": 555, "digest": "sha256:" + "5" * 64, "workflow_run": {"id": 800, "head_sha": BEFORE_SHA}}]
            )
        if endpoint == "repos/owner/repo/actions/runs/800/attempts/1/jobs":
            return {"jobs": [{"run_attempt": 1, "name": "e2e-core"}]}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert violations == []


def test_run_attempt_trusted_binding_conflict_fails_closed():
    """GIVEN a record claiming run_attempt=1 whose artifact is genuine but
    whose attempt-specific jobs API reports NO job for attempt 1 (the
    claimed attempt never actually ran) WHEN verified THEN
    `run_attempt_not_found_via_live_api` is reported -- a fabricated
    `run_attempt` value is never accepted merely because the artifact
    itself is real (AC5, OWNER P0-3 item 3: min()/max() on an unbound
    run_attempt would let a fake value manipulate canonical selection)."""
    record = _record(801, artifact_id=556, artifact_digest="sha256:" + "6" * 64, run_attempt=1)

    def fake_api_call(endpoint: str) -> dict:
        if endpoint == "repos/owner/repo/actions/runs/801/artifacts":
            return _fake_artifacts_response(
                [{"id": 556, "digest": "sha256:" + "6" * 64, "workflow_run": {"id": 801, "head_sha": BEFORE_SHA}}]
            )
        if endpoint == "repos/owner/repo/actions/runs/801/attempts/1/jobs":
            return {"jobs": []}  # attempt 1 never actually ran
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert any("run_attempt_not_found_via_live_api" in v for v in violations)


def test_run_attempt_absent_skips_trusted_binding_call_backward_compat():
    """GIVEN a record that omits `run_attempt` entirely (pre-#2179
    fixture) WHEN verified THEN NO attempt-specific jobs API call is made
    -- only the pre-existing artifacts-listing call -- preserving
    backward compatibility with callers that never set this field."""
    record = _record(802, artifact_id=557, artifact_digest="sha256:" + "7" * 64)
    assert "run_attempt" not in record

    calls = []

    def fake_api_call(endpoint: str) -> dict:
        calls.append(endpoint)
        return _fake_artifacts_response(
            [{"id": 557, "digest": "sha256:" + "7" * 64, "workflow_run": {"id": 802, "head_sha": BEFORE_SHA}}]
        )

    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert violations == []
    assert calls == ["repos/owner/repo/actions/runs/802/artifacts"]


# --------------------------------------------------------------------------- #
# AC7: parametrized order-independence + canonical (workflow_run_id
# ascending) runs/selection output order.
# --------------------------------------------------------------------------- #
def _order_independence_fixture() -> list[dict]:
    records = []
    for i, run_id in enumerate((910, 905, 920, 915, 900)):
        records.append(_record(run_id, job="e2e", artifact_id=1000 + i, run_attempt=1))
    return records


@pytest.mark.parametrize(
    "shuffle_fn",
    [
        lambda records: records,
        lambda records: list(reversed(records)),
        lambda records: random.Random(42).sample(records, k=len(records)),
    ],
    ids=["original", "reversed", "shuffled"],
)
def test_order_independent_parametrized_matches_canonical_sort(shuffle_fn):
    """GIVEN the SAME underlying record set presented in original, reversed,
    and shuffled order WHEN a manifest is collected THEN the selected
    `runs` array is IDENTICAL across all three orderings, and is sorted by
    `workflow_run_id` ascending (canonical order) regardless of input
    order (#2179 AC7)."""
    base_records = _order_independence_fixture()
    ordered_records = shuffle_fn(copy.deepcopy(base_records))

    manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA,
        AFTER_SHA,
        ordered_records,
        [],
        job_names=("e2e",),
        min_run_count=1,
    )
    runs = manifest["arms"]["before"]["jobs"]["e2e"]["runs"]
    run_ids = [r["workflow_run_id"] for r in runs]
    assert run_ids == sorted(run_ids)
    assert run_ids == [900, 905, 910, 915, 920]

    canonical_manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA, AFTER_SHA, base_records, [], job_names=("e2e",), min_run_count=1
    )
    assert runs == canonical_manifest["arms"]["before"]["jobs"]["e2e"]["runs"]


# --------------------------------------------------------------------------- #
# AC8: artifact-listing pagination.
# --------------------------------------------------------------------------- #
def test_artifact_listing_paginates_all_pages():
    """GIVEN a workflow_run_id whose artifacts span TWO pages (a
    heavily-rerun run with more artifacts than fit on page 1) WHEN
    `verify_run_record_against_live_api` verifies a record whose artifact
    only appears on PAGE 2, THEN it is found (not misreported as
    `artifact_not_found_via_live_api`) -- proves full pagination, not just
    page 1 (#2179 AC8)."""
    record = _record(900, artifact_id=42, artifact_digest="sha256:" + "9" * 64)

    page_1_artifacts = [
        {"id": i, "digest": "sha256:" + f"{i:064x}", "workflow_run": {"id": 900, "head_sha": BEFORE_SHA}}
        for i in range(1, 3)
    ]
    page_2_artifacts = [
        {"id": 42, "digest": "sha256:" + "9" * 64, "workflow_run": {"id": 900, "head_sha": BEFORE_SHA}},
    ]

    calls = []

    def fake_api_call(endpoint: str) -> dict:
        calls.append(endpoint)
        if endpoint == "repos/owner/repo/actions/runs/900/artifacts":
            return {"artifacts": page_1_artifacts, "total_count": 3}
        if endpoint == "repos/owner/repo/actions/runs/900/artifacts?per_page=100&page=2":
            return {"artifacts": page_2_artifacts, "total_count": 3}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert violations == []
    assert len(calls) == 2


def test_artifact_listing_single_page_does_not_query_page_2():
    """GIVEN a `total_count` that matches page 1's returned artifact count
    WHEN verifying THEN no page-2 call is made (preserves the pre-#2179
    single-call shape when there IS only one page)."""
    record = _record(901, artifact_id=1, artifact_digest="sha256:" + f"{1:064x}")

    calls = []

    def fake_api_call(endpoint: str) -> dict:
        calls.append(endpoint)
        artifact = {
            "id": 1,
            "digest": "sha256:" + f"{1:064x}",
            "workflow_run": {"id": 901, "head_sha": BEFORE_SHA},
        }
        return {"artifacts": [artifact], "total_count": 1}

    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert violations == []
    assert calls == ["repos/owner/repo/actions/runs/901/artifacts"]


# --------------------------------------------------------------------------- #
# AC9: run_attempt type normalization + identity-collision fail-closed.
# --------------------------------------------------------------------------- #
def test_run_attempt_type_normalization_and_conflict_fail_closed():
    """GIVEN records with a NULL, string-typed, zero, and negative
    `run_attempt`, and a genuine (workflow_run_id, job, run_attempt)
    identity collision (same key, differing artifact identity), WHEN
    collected THEN every one of these is excluded from the sample AND
    reported as an explicit evidence error -- never silently coerced or
    merged (#2179 AC9)."""
    null_attempt = _record(950, job="e2e", artifact_id=1, run_attempt=None)
    null_attempt["run_attempt"] = None  # explicit null, distinct from an absent key
    string_attempt = _record(951, job="e2e", artifact_id=2, run_attempt="2")
    zero_attempt = _record(952, job="e2e", artifact_id=3, run_attempt=0)
    negative_attempt = _record(953, job="e2e", artifact_id=4, run_attempt=-1)
    collision_a = _record(954, job="e2e", artifact_id=5, artifact_digest="sha256:" + "5" * 64, run_attempt=1)
    collision_b = _record(954, job="e2e", artifact_id=6, artifact_digest="sha256:" + "6" * 64, run_attempt=1)

    manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA,
        AFTER_SHA,
        [null_attempt, string_attempt, zero_attempt, negative_attempt, collision_a, collision_b],
        [],
        job_names=("e2e",),
        min_run_count=1,
    )
    before_e2e = manifest["arms"]["before"]["jobs"]["e2e"]
    assert before_e2e["run_count"] == 0
    assert before_e2e["sample_workflow_run_ids"] == []

    reasons_by_id: dict[int, set[str]] = {}
    for err in manifest["evidence_errors"]:
        if err["arm"] != "before":
            continue
        for workflow_run_id in (950, 951, 952, 953, 954):
            if f"workflow_run_id={workflow_run_id}" in err["detail"]:
                reasons_by_id.setdefault(workflow_run_id, set()).add(err["reason"])

    assert reasons_by_id.get(950) == {"missing_or_invalid_initial_attempt_excluded_from_sample"}
    assert reasons_by_id.get(951) == {"missing_or_invalid_initial_attempt_excluded_from_sample"}
    assert reasons_by_id.get(952) == {"missing_or_invalid_initial_attempt_excluded_from_sample"}
    assert reasons_by_id.get(953) == {"missing_or_invalid_initial_attempt_excluded_from_sample"}
    assert reasons_by_id.get(954) == {"run_attempt_identity_collision"}


def test_normalize_run_attempt_missing_key_defaults_to_1_backward_compat():
    """GIVEN a record whose `run_attempt` key is entirely ABSENT (a
    pre-#2179 fixture shape) WHEN normalized THEN it defaults to 1 --
    backward compatible with every existing fixture in
    `test_collect_e2e_performance_benchmark.py` that never sets this
    field (#2179 AC9 contrast case)."""
    record = _record(960, job="e2e")
    assert "run_attempt" not in record
    assert collector._normalize_run_attempt(record) == 1
