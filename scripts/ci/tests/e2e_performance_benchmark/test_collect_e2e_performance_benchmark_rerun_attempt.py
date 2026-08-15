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


def _genuine_attempt_jobs_response(name: str = "e2e-core", head_sha: str = BEFORE_SHA) -> dict:
    return {"jobs": [{"run_attempt": 1, "name": name, "head_sha": head_sha, "conclusion": "success"}]}


def test_run_attempt_trusted_binding_accepts_genuine_attempt():
    """GIVEN a record explicitly claiming run_attempt=1, whose claimed
    artifact matches the live artifacts API (including its `name`,
    #2182 P0-2) AND whose attempt-specific jobs API confirms a real job
    for attempt 1 (matching name/head_sha/conclusion, #2182 P0-2), WHEN
    verified THEN there are no violations (AC5)."""
    record = _record(800, artifact_id=555, artifact_digest="sha256:" + "5" * 64, run_attempt=1)

    def fake_api_call(endpoint: str) -> dict:
        if endpoint == "repos/owner/repo/actions/runs/800/artifacts?per_page=100&page=1":
            return _fake_artifacts_response(
                [
                    {
                        "id": 555,
                        "digest": "sha256:" + "5" * 64,
                        "name": "ci-runtime-baseline-e2e-core-1",
                        "workflow_run": {"id": 800, "head_sha": BEFORE_SHA},
                    }
                ]
            )
        if endpoint == "repos/owner/repo/actions/runs/800/attempts/1/jobs":
            return _genuine_attempt_jobs_response()
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
        if endpoint == "repos/owner/repo/actions/runs/801/artifacts?per_page=100&page=1":
            return _fake_artifacts_response(
                [
                    {
                        "id": 556,
                        "digest": "sha256:" + "6" * 64,
                        "name": "ci-runtime-baseline-e2e-core-1",
                        "workflow_run": {"id": 801, "head_sha": BEFORE_SHA},
                    }
                ]
            )
        if endpoint == "repos/owner/repo/actions/runs/801/attempts/1/jobs":
            return {"jobs": []}  # attempt 1 never actually ran
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert any("run_attempt_not_found_via_live_api" in v for v in violations)


def test_run_attempt_absent_skips_trusted_binding_call_backward_compat():
    """GIVEN a record that omits `run_attempt` entirely (pre-#2179
    fixture, #2182 P0-3 "missing" classification) WHEN verified THEN NO
    attempt-specific jobs API call is made and NO artifact-name check is
    performed -- only the pre-existing artifacts-listing call -- this
    function's OWN behavior stays backward compatible for such records
    even though they are separately excluded from the trusted/selected
    cohort entirely (see `_select_initial_attempt_records`, #2182 P0-3)."""
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
    assert calls == ["repos/owner/repo/actions/runs/802/artifacts?per_page=100&page=1"]


# --------------------------------------------------------------------------- #
# #2182 P0-2 (fix_delta after OWNER adversarial review of PR #2182,
# issuecomment-5302446086): the trusted live-binding identity tuple is
# `(workflow_run_id, run_attempt, job_name, workflow_run_head_sha,
# job_conclusion, artifact_id, artifact_name, artifact_digest)` -- every
# element must correspond to the SAME specific job+attempt, never merely
# "some job exists in that attempt's job list".
# --------------------------------------------------------------------------- #
def _base_p02_fake_api_call(
    workflow_run_id: int,
    artifact_id: int,
    digest: str,
    artifact_name: str,
    jobs: list[dict],
):
    def fake_api_call(endpoint: str) -> dict:
        if endpoint == f"repos/owner/repo/actions/runs/{workflow_run_id}/artifacts?per_page=100&page=1":
            return _fake_artifacts_response(
                [
                    {
                        "id": artifact_id,
                        "digest": digest,
                        "name": artifact_name,
                        "workflow_run": {"id": workflow_run_id, "head_sha": BEFORE_SHA},
                    }
                ]
            )
        if endpoint == f"repos/owner/repo/actions/runs/{workflow_run_id}/attempts/1/jobs":
            return {"jobs": jobs}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    return fake_api_call


def test_run_attempt_binding_rejects_attempt2_relabeled_as_attempt1():
    """GIVEN an attempt-2 artifact (its OWN artifact `name` carries `-2`)
    relabeled by the record as run_attempt=1, WHEN verified THEN
    `artifact_name_mismatch_vs_job_run_attempt` is reported -- the pre-P0-2
    check (artifact_id/digest present in SOME attempt-1 job list) would
    have missed this relabeling attack entirely."""
    record = _record(810, job="e2e-core", artifact_id=560, artifact_digest="sha256:" + "a" * 64, run_attempt=1)
    fake_api_call = _base_p02_fake_api_call(
        810, 560, "sha256:" + "a" * 64, "ci-runtime-baseline-e2e-core-2", [
            {"run_attempt": 1, "name": "e2e-core", "head_sha": BEFORE_SHA, "conclusion": "success"}
        ]
    )
    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert any("artifact_name_mismatch_vs_job_run_attempt" in v for v in violations)


def test_run_attempt_binding_rejects_job_name_mismatch():
    """GIVEN an attempt-1 jobs list containing ONLY jobs for a DIFFERENT
    job name than the record claims WHEN verified THEN
    `run_attempt_not_found_via_live_api` is reported -- an unrelated job
    existing in attempt 1's job list is never sufficient (#2182 P0-2 item
    2: "attempt-1 job list contains only unrelated jobs")."""
    record = _record(811, job="e2e-core", artifact_id=561, artifact_digest="sha256:" + "b" * 64, run_attempt=1)
    fake_api_call = _base_p02_fake_api_call(
        811, 561, "sha256:" + "b" * 64, "ci-runtime-baseline-e2e-core-1", [
            {"run_attempt": 1, "name": "e2e-responsive-matrix", "head_sha": BEFORE_SHA, "conclusion": "success"}
        ]
    )
    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert any("run_attempt_not_found_via_live_api" in v for v in violations)


def test_run_attempt_binding_rejects_job_head_sha_mismatch():
    """GIVEN the matching attempt-1 job's OWN `head_sha` differs from the
    expected commit WHEN verified THEN
    `run_attempt_job_head_sha_mismatch_vs_live_api` is reported."""
    record = _record(812, job="e2e-core", artifact_id=562, artifact_digest="sha256:" + "c" * 64, run_attempt=1)
    fake_api_call = _base_p02_fake_api_call(
        812, 562, "sha256:" + "c" * 64, "ci-runtime-baseline-e2e-core-1", [
            {"run_attempt": 1, "name": "e2e-core", "head_sha": "f" * 40, "conclusion": "success"}
        ]
    )
    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert any("run_attempt_job_head_sha_mismatch_vs_live_api" in v for v in violations)


def test_run_attempt_binding_rejects_conclusion_not_success():
    """GIVEN the matching attempt-1 job's `conclusion` is NOT `success`
    WHEN verified THEN `run_attempt_job_conclusion_not_success_via_live_api`
    is reported."""
    record = _record(813, job="e2e-core", artifact_id=563, artifact_digest="sha256:" + "d" * 64, run_attempt=1)
    fake_api_call = _base_p02_fake_api_call(
        813, 563, "sha256:" + "d" * 64, "ci-runtime-baseline-e2e-core-1", [
            {"run_attempt": 1, "name": "e2e-core", "head_sha": BEFORE_SHA, "conclusion": "failure"}
        ]
    )
    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert any("run_attempt_job_conclusion_not_success_via_live_api" in v for v in violations)


def test_run_attempt_jobs_api_pagination_over_30_jobs():
    """GIVEN a workflow run whose attempt-specific jobs API response
    contains > 30 jobs (a realistic large job-matrix run) WHEN the
    genuine matching job is present among them THEN it is still found
    (matching_jobs filters the full returned list, not merely the first
    page) -- this endpoint's `jobs` array is consumed as-given by this
    module (no client-side pagination loop needed here, since `gh api`
    with `--paginate` is expected to have already assembled it upstream);
    this test proves the FILTER itself does not implicitly cap/truncate
    at some smaller count."""
    unrelated_jobs = [
        {"run_attempt": 1, "name": f"unrelated-job-{i}", "head_sha": BEFORE_SHA, "conclusion": "success"}
        for i in range(35)
    ]
    genuine_job = {"run_attempt": 1, "name": "e2e-core", "head_sha": BEFORE_SHA, "conclusion": "success"}
    record = _record(814, job="e2e-core", artifact_id=564, artifact_digest="sha256:" + "e" * 64, run_attempt=1)
    fake_api_call = _base_p02_fake_api_call(
        814, 564, "sha256:" + "e" * 64, "ci-runtime-baseline-e2e-core-1", unrelated_jobs + [genuine_job]
    )
    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert violations == []


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
def _paged_artifacts_response(artifacts: list[dict], total_count: int) -> dict:
    return {"artifacts": artifacts, "total_count": total_count}


def test_artifact_listing_paginates_all_pages():
    """GIVEN a workflow_run_id whose artifacts span TWO pages (a
    heavily-rerun run with more artifacts than fit on page 1) WHEN
    `verify_run_record_against_live_api` verifies a record whose artifact
    only appears on PAGE 2, THEN it is found (not misreported as
    `artifact_not_found_via_live_api`) -- proves full pagination, not just
    page 1 (#2179 AC8). #2182 P0-1: page 1 now ALSO carries an explicit
    `per_page=100` -- the same page size used on every subsequent page."""
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
        if endpoint == "repos/owner/repo/actions/runs/900/artifacts?per_page=100&page=1":
            return _paged_artifacts_response(page_1_artifacts, 3)
        if endpoint == "repos/owner/repo/actions/runs/900/artifacts?per_page=100&page=2":
            return _paged_artifacts_response(page_2_artifacts, 3)
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert violations == []
    assert len(calls) == 2


def test_artifact_listing_single_page_does_not_query_page_2():
    """GIVEN a `total_count` that matches page 1's returned artifact count
    WHEN verifying THEN no page-2 call is made (only ONE call total, at
    `per_page=100&page=1`) -- proves the consistent page size does not
    itself introduce an unnecessary extra request when everything fits on
    page 1 (#2182 P0-1)."""
    record = _record(901, artifact_id=1, artifact_digest="sha256:" + f"{1:064x}")

    calls = []

    def fake_api_call(endpoint: str) -> dict:
        calls.append(endpoint)
        artifact = {
            "id": 1,
            "digest": "sha256:" + f"{1:064x}",
            "workflow_run": {"id": 901, "head_sha": BEFORE_SHA},
        }
        return _paged_artifacts_response([artifact], 1)

    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert violations == []
    assert calls == ["repos/owner/repo/actions/runs/901/artifacts?per_page=100&page=1"]


# --------------------------------------------------------------------------- #
# #2182 P0-1 (fix_delta after OWNER adversarial review of PR #2182,
# issuecomment-5302446086): the pre-fix_delta version issued page 1
# WITHOUT an explicit `per_page` (GitHub defaults to 30), then switched to
# `per_page=100` for page 2+ -- since GitHub's page numbering is relative
# to page SIZE, this silently SKIPPED artifacts 31-100 in any
# `total_count > 30` run. Parametrized regression across a fixed
# `total_count=130` cohort, placing the target artifact at every
# consequential boundary position.
# --------------------------------------------------------------------------- #
def _build_130_artifact_pages(target_position: int) -> dict:
    """Builds a 2-page (`per_page=100`) fake artifact universe of size 130,
    with the TARGET artifact (id=999999) placed at `target_position`
    (1-indexed) and every other slot filled with a distinct filler
    artifact id. Returns `{page_number: [artifacts...]}`."""
    all_artifacts = []
    for position in range(1, 131):
        if position == target_position:
            all_artifacts.append(
                {"id": 999999, "digest": "sha256:" + "9" * 64, "workflow_run": {"id": 950, "head_sha": BEFORE_SHA}}
            )
        else:
            all_artifacts.append(
                {
                    "id": 100000 + position,
                    "digest": "sha256:" + f"{position:064x}",
                    "workflow_run": {"id": 950, "head_sha": BEFORE_SHA},
                }
            )
    return {1: all_artifacts[:100], 2: all_artifacts[100:]}


@pytest.mark.parametrize("target_position", [30, 31, 50, 100, 101, 130], ids=lambda p: f"position_{p}")
def test_artifact_pagination_consistent_page_size_total_count_130(target_position):
    """GIVEN a `total_count=130` artifact universe (2 pages at the SAME
    `per_page=100`) WHEN the target artifact is placed at position 30, 31,
    50, 100, 101, or 130 THEN it is always found by
    `verify_run_record_against_live_api` -- proves the consistent page
    size (#2182 P0-1) never mis-slices the boundary between page 1 and
    page 2 regardless of which side of the 30/100-item boundary the
    target falls on (position 30/31 straddle the OLD implicit-per_page=30
    boundary that the pre-fix_delta bug would have silently skipped for
    any `target_position > 30`; 100/101 straddle the NEW per_page=100
    page boundary; 130 is the last item)."""
    pages = _build_130_artifact_pages(target_position)
    record = _record(950, artifact_id=999999, artifact_digest="sha256:" + "9" * 64)

    def fake_api_call(endpoint: str) -> dict:
        if endpoint == "repos/owner/repo/actions/runs/950/artifacts?per_page=100&page=1":
            return _paged_artifacts_response(pages[1], 130)
        if endpoint == "repos/owner/repo/actions/runs/950/artifacts?per_page=100&page=2":
            return _paged_artifacts_response(pages[2], 130)
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert violations == []


def test_artifact_pagination_fails_closed_on_empty_page_before_total_count_reached():
    """GIVEN a page 2 response that comes back EMPTY while
    `total_count=130` claims more artifacts remain WHEN fetched THEN
    `LiveAPIError` is raised (surfaced as
    `live_api_artifacts_fetch_failed` -> `artifact_pagination_empty_page_
    before_total_count_reached`) -- never silently treated as "that's all
    of them" (#2182 P0-1 fail-closed condition a)."""
    record = _record(951, artifact_id=1, artifact_digest="sha256:" + "1" * 64)

    def fake_api_call(endpoint: str) -> dict:
        if endpoint == "repos/owner/repo/actions/runs/951/artifacts?per_page=100&page=1":
            page_1 = [
                {"id": i, "digest": "sha256:" + f"{i:064x}", "workflow_run": {"id": 951, "head_sha": BEFORE_SHA}}
                for i in range(1, 101)
            ]
            return _paged_artifacts_response(page_1, 130)
        if endpoint == "repos/owner/repo/actions/runs/951/artifacts?per_page=100&page=2":
            return _paged_artifacts_response([], 130)  # inconsistent: claims 130 but page 2 is empty
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert any("live_api_artifacts_fetch_failed" in v for v in violations)
    assert any("artifact_pagination_empty_page_before_total_count_reached" in v for v in violations)


def test_artifact_pagination_fails_closed_on_duplicate_artifact_id():
    """GIVEN the SAME artifact `id` appearing on BOTH page 1 and page 2
    WHEN fetched THEN `LiveAPIError` is raised
    (`artifact_pagination_duplicate_artifact_id`) -- a paging consistency
    violation is never silently double-counted (#2182 P0-1 fail-closed
    condition b)."""
    record = _record(952, artifact_id=1, artifact_digest="sha256:" + "1" * 64)

    def fake_api_call(endpoint: str) -> dict:
        if endpoint == "repos/owner/repo/actions/runs/952/artifacts?per_page=100&page=1":
            page_1 = [
                {"id": i, "digest": "sha256:" + f"{i:064x}", "workflow_run": {"id": 952, "head_sha": BEFORE_SHA}}
                for i in range(1, 101)
            ]
            return _paged_artifacts_response(page_1, 101)
        if endpoint == "repos/owner/repo/actions/runs/952/artifacts?per_page=100&page=2":
            duplicate = {"id": 1, "digest": "sha256:" + "1" * 64, "workflow_run": {"id": 952, "head_sha": BEFORE_SHA}}
            return _paged_artifacts_response([duplicate], 101)
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert any("live_api_artifacts_fetch_failed" in v for v in violations)
    assert any("artifact_pagination_duplicate_artifact_id" in v for v in violations)


def test_artifact_pagination_fails_closed_on_total_count_changing_mid_fetch():
    """GIVEN `total_count` reported by page 2 DIFFERS from page 1's
    `total_count` for the SAME fetch WHEN fetched THEN `LiveAPIError` is
    raised (`artifact_pagination_total_count_changed_mid_fetch`) -- the
    already-collected pages are no longer a consistent snapshot (#2182
    P0-1 fail-closed condition c)."""
    record = _record(953, artifact_id=1, artifact_digest="sha256:" + "1" * 64)

    def fake_api_call(endpoint: str) -> dict:
        if endpoint == "repos/owner/repo/actions/runs/953/artifacts?per_page=100&page=1":
            page_1 = [
                {"id": i, "digest": "sha256:" + f"{i:064x}", "workflow_run": {"id": 953, "head_sha": BEFORE_SHA}}
                for i in range(1, 101)
            ]
            return _paged_artifacts_response(page_1, 130)
        if endpoint == "repos/owner/repo/actions/runs/953/artifacts?per_page=100&page=2":
            page_2 = [
                {"id": i, "digest": "sha256:" + f"{i:064x}", "workflow_run": {"id": 953, "head_sha": BEFORE_SHA}}
                for i in range(101, 121)
            ]
            return _paged_artifacts_response(page_2, 120)  # total_count changed from 130 to 120
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    violations = collector.verify_run_record_against_live_api(record, BEFORE_SHA, "owner/repo", api_call=fake_api_call)
    assert any("live_api_artifacts_fetch_failed" in v for v in violations)
    assert any("artifact_pagination_total_count_changed_mid_fetch" in v for v in violations)


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


def test_normalize_run_attempt_missing_key_is_not_promoted_to_attempt_1():
    """#2182 P0-3 (fix_delta after OWNER adversarial review of PR #2182,
    issuecomment-5302446086, supersedes the pre-fix_delta
    "defaults_to_1_backward_compat" test this replaces): GIVEN a record
    whose `run_attempt` key is entirely ABSENT (a pre-#2179 fixture shape)
    WHEN normalized THEN `_normalize_run_attempt` returns `None` (NOT 1)
    -- the pre-fix_delta behavior silently promoted a legacy,
    NEVER-live-API-verified record to be indistinguishable from a
    genuinely verified attempt-1 selection (provenance laundering). The
    underlying classification is still explicitly "missing" (readable,
    schema-parseable), never "invalid" (malformed) -- see
    `_classify_run_attempt`."""
    record = _record(960, job="e2e")
    assert "run_attempt" not in record
    assert collector._normalize_run_attempt(record) is None
    assert collector._classify_run_attempt(record) == (None, "missing")


def test_missing_run_attempt_excluded_from_trusted_cohort_and_reported():
    """#2182 P0-3: GIVEN ONLY a record with a MISSING `run_attempt` key
    for a `workflow_run_id` (no competing explicit attempt-1 record) WHEN
    a manifest is collected THEN that `workflow_run_id` is EXCLUDED from
    the trusted/selected cohort (`run_count == 0`, never silently
    defaulted to a verified `run_attempt: 1` entry), and the exclusion is
    reported with the DISTINCT `legacy_unverified_run_attempt` reason
    (never conflated with the generic malformed-value
    `missing_or_invalid_initial_attempt_excluded_from_sample` reason)."""
    legacy_record = _record(970, job="e2e")
    assert "run_attempt" not in legacy_record

    manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA, AFTER_SHA, [legacy_record], [], job_names=("e2e",), min_run_count=1
    )
    before_e2e = manifest["arms"]["before"]["jobs"]["e2e"]
    assert before_e2e["run_count"] == 0
    assert before_e2e["sample_workflow_run_ids"] == []
    assert before_e2e["runs"] == []

    matching_errors = [
        err
        for err in manifest["evidence_errors"]
        if err["arm"] == "before" and "workflow_run_id=970" in err["detail"]
    ]
    assert len(matching_errors) == 1
    assert matching_errors[0]["reason"] == "legacy_unverified_run_attempt"


def test_reprocessing_legacy_input_never_promotes_to_verified_attempt_1():
    """#2182 P0-3: re-running the SAME legacy (no-`run_attempt`-key) input
    through `collect_benchmark_manifest` twice never causes it to
    "accumulate" trust or get synthesized a `run_attempt: 1` /
    `rerun_attempt_selection_policy` entry on a SECOND pass -- the
    exclusion is a pure function of the input, not stateful."""
    legacy_record = _record(971, job="e2e")

    first_manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA, AFTER_SHA, [legacy_record], [], job_names=("e2e",), min_run_count=1
    )
    second_manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA, AFTER_SHA, [legacy_record], [], job_names=("e2e",), min_run_count=1
    )
    for manifest in (first_manifest, second_manifest):
        before_e2e = manifest["arms"]["before"]["jobs"]["e2e"]
        assert before_e2e["run_count"] == 0
        assert before_e2e["runs"] == []
        assert not any(
            "run_attempt" in run or "rerun_attempt_selection_policy" in run for run in before_e2e["runs"]
        )


def test_schema_validation_succeeds_for_excluded_legacy_record_manifest():
    """#2182 P0-3: a manifest whose ONLY input record is a legacy
    (missing-`run_attempt`) one that fails TRUSTED-cohort selection still
    passes JSON Schema STRUCTURAL validation (an empty `runs`/`jobs`
    cohort with `complete: false` and a recorded `evidence_errors` entry
    is a structurally valid manifest shape) -- schema conformance and
    trusted-cohort eligibility are independent checks, never conflated."""
    legacy_record = _record(972, job="e2e")
    manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA, AFTER_SHA, [legacy_record], [], job_names=("e2e",), min_run_count=1
    )
    # Must not raise -- structural schema validation succeeds even though
    # the arm is incomplete (min_run_count not met) and carries an
    # evidence_errors entry for the excluded legacy record.
    collector._validate_against_schema(manifest)
    assert manifest["arms"]["before"]["complete"] is False


# --------------------------------------------------------------------------- #
# #2182 P1 (fix_delta after OWNER adversarial review of PR #2182,
# issuecomment-5302446086): identity collisions must be detected on the
# ENTIRE normalized record (byte-for-byte), never merely
# (artifact_id, artifact_digest) -- and a genuine content DISAGREEMENT
# among same-identity candidates must exclude the WHOLE sample, never be
# silently resolved via canonical-JSON `min()`.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "field,value_b",
    [
        ("head_sha", "f" * 40),
        ("conclusion", "failure"),
        ("workflow_digest", "workflow-digest-fixture-DIFFERENT"),
        ("workflow_sha", "d" * 40),
    ],
    ids=["head_sha", "conclusion", "workflow_digest", "workflow_sha"],
)
def test_identity_collision_on_any_disagreeing_field_excludes_whole_sample(field, value_b):
    """GIVEN two records sharing `(workflow_run_id, job, run_attempt)` but
    disagreeing on a field OTHER than artifact_id/artifact_digest (the
    pre-fix_delta collision detector's ONLY comparison, which would have
    silently `min()`-resolved these) WHEN their identity is examined
    directly via `_detect_run_attempt_identity_collisions` THEN the
    `workflow_run_id` is flagged as a collision -- never silently
    tie-broken (#2182 P1). Exercised directly against the collision
    detector (rather than the full `collect_benchmark_manifest` pipeline)
    because `_verify_run_record` independently gates `head_sha` against a
    single `expected_head_sha` per arm -- a `head_sha`-disagreeing pair
    would never BOTH reach `_collect_arm`'s per-job verified pool in the
    first place, which would defeat this specific field's coverage."""
    record_a = _record(
        980,
        job="e2e",
        artifact_id=5000,
        artifact_digest="sha256:" + "5" * 64,
        conclusion="success",
        workflow_digest="workflow-digest-fixture-v1",
        workflow_sha=WORKFLOW_SHA,
        run_attempt=1,
    )
    record_b = dict(record_a, **{field: value_b})
    assert record_a[field] != record_b[field]

    collisions = collector._detect_run_attempt_identity_collisions([record_a, record_b])
    assert 980 in collisions
    assert len(collisions[980]) == 2

    # Also exercise the full pipeline for the fields NOT independently
    # gated by `_verify_run_record` before collision detection runs
    # (conclusion/workflow_digest/workflow_sha do not affect whether a
    # record reaches `_collect_arm`'s per-job verified pool the way
    # head_sha does).
    if field != "head_sha":
        manifest = collector.collect_benchmark_manifest(
            BEFORE_SHA, AFTER_SHA, [record_a, record_b], [], job_names=("e2e",), min_run_count=1
        )
        before_e2e = manifest["arms"]["before"]["jobs"]["e2e"]
        assert before_e2e["run_count"] == 0
        assert 980 not in before_e2e["sample_workflow_run_ids"]
        collision_errors = [
            err
            for err in manifest["evidence_errors"]
            if err["arm"] == "before"
            and err["reason"] == "run_attempt_identity_collision"
            and "workflow_run_id=980" in err["detail"]
        ]
        assert len(collision_errors) == 1


def test_byte_identical_duplicate_is_unchanged_not_a_collision():
    """GIVEN two BYTE-FOR-BYTE identical records sharing `(workflow_run_id,
    job, run_attempt)` (a harmless duplicate -- e.g. the SAME record
    fetched twice) WHEN collected THEN this is NOT a collision -- exactly
    ONE sample is selected, with all of its ORIGINAL field values intact
    (#2182 P1: only content DISAGREEMENT is a collision)."""
    record_a = _record(981, job="e2e", artifact_id=5001, artifact_digest="sha256:" + "6" * 64, run_attempt=1)
    record_b = dict(record_a)  # byte-for-byte identical

    manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA, AFTER_SHA, [record_a, record_b], [], job_names=("e2e",), min_run_count=1
    )
    before_e2e = manifest["arms"]["before"]["jobs"]["e2e"]
    assert before_e2e["run_count"] == 1
    assert before_e2e["sample_workflow_run_ids"] == [981]
    assert before_e2e["runs"][0]["artifact_id"] == 5001


def test_missing_attempt_record_colliding_with_explicit_attempt_1_is_a_collision():
    """#2182 P1: GIVEN one record with a MISSING `run_attempt` key and
    ANOTHER record with an EXPLICIT `run_attempt: 1`, both sharing the
    same `workflow_run_id`/`job` but disagreeing on content (different
    `artifact_id`), WHEN collected THEN this is a genuine collision (the
    missing-key record is grouped into the attempt-1 identity slot for
    COLLISION DETECTION purposes -- see `_effective_attempt_for_collision_
    grouping`) -- the WHOLE `workflow_run_id` sample is excluded, not just
    the (separately, always-excluded) missing-run_attempt one."""
    explicit_attempt_1 = _record(982, job="e2e", artifact_id=6000, artifact_digest="sha256:" + "7" * 64, run_attempt=1)
    missing_attempt = _record(982, job="e2e", artifact_id=6001, artifact_digest="sha256:" + "8" * 64)
    assert "run_attempt" not in missing_attempt

    manifest = collector.collect_benchmark_manifest(
        BEFORE_SHA, AFTER_SHA, [explicit_attempt_1, missing_attempt], [], job_names=("e2e",), min_run_count=1
    )
    before_e2e = manifest["arms"]["before"]["jobs"]["e2e"]
    assert before_e2e["run_count"] == 0
    assert 982 not in before_e2e["sample_workflow_run_ids"]
    collision_errors = [
        err
        for err in manifest["evidence_errors"]
        if err["arm"] == "before"
        and err["reason"] == "run_attempt_identity_collision"
        and "workflow_run_id=982" in err["detail"]
    ]
    assert len(collision_errors) == 1
