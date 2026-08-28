"""Issue #2230: `visual-impact-decision-v1` / `component-vrt-evidence-manifest`
artifact-attempt (rerun freshness/consistency) binding.

PR #2229 review fix_delta P1-2 (scope narrowing note on `verify_trusted_artifact`)
tracked this as a separate, explicit follow-up: the GitHub REST artifacts-list
API has no `attempt_number` filter and artifact OBJECTS carry no attempt-
identity field, so the caller workflow's former `[0]` pick of a same-named
artifact could not be made attempt-exact without (a) attempt-specific artifact
NAMES at upload time (`ci.yml`), (b) a `name=` filtered, fully-paginated,
cardinality-exactly-one artifact ACQUISITION at fetch time
(`acquire_trusted_artifact()`), and (c) CONTENT-level `(workflow_run_id,
run_attempt, head_sha)` tuple binding cross-checked in `verify_trusted_artifact()`
against the already-authenticated `component-vrt-report` CheckRun provenance.

This module tests all three layers with an injectable HTTP transport (never a
real GitHub API/network dependency), mirroring the existing
`acquire_component_vrt_checkrun()` adversarial-test pattern in
test_resolve_visual_impact_checkrun_adversarial.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "resolve_visual_impact.py"
_MODULE_NAME = "resolve_visual_impact_issue_2230_artifact_attempt_binding"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rvi = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = rvi
_spec.loader.exec_module(rvi)

REPOSITORY = "squne121/loop-protocol"
HEAD_SHA = "a" * 40
RUN_ID = 555111
RUN_ATTEMPT = 2
CHECK_RUN_ID = 909090

DECISION_NAME = f"visual-impact-decision-v1-{RUN_ATTEMPT}"
EVIDENCE_NAME = f"component-vrt-evidence-manifest-{RUN_ATTEMPT}"

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "docs" / "dev" / "visual-impact.schema.json"


# ---------------------------------------------------------------------------
# Shared fixtures: authenticated provenance + decision/evidence-manifest
# builders (never hand-rolled dicts for the manifest -- always
# build_evidence_manifest_v2_record()).
# ---------------------------------------------------------------------------


def _job(*, run_id: int = RUN_ID, run_attempt: int = RUN_ATTEMPT) -> dict:
    return {
        "id": 700,
        "name": "component-vrt-report",
        "run_id": run_id,
        "run_attempt": run_attempt,
        "head_sha": HEAD_SHA,
        "conclusion": "success",
        "check_run_url": f"https://api.github.com/repos/{REPOSITORY}/check-runs/{CHECK_RUN_ID}",
    }


def _check_run() -> dict:
    return {
        "id": CHECK_RUN_ID,
        "name": "component-vrt-report",
        "head_sha": HEAD_SHA,
        "status": "completed",
        "conclusion": "success",
        "app": {"id": rvi.GITHUB_ACTIONS_APP_ID, "slug": rvi.GITHUB_ACTIONS_APP_SLUG},
    }


def _provenance(*, run_id: int = RUN_ID, run_attempt: int = RUN_ATTEMPT):
    return rvi.verify_component_vrt_checkrun_provenance(
        check_run=_check_run(),
        workflow_jobs=[_job(run_id=run_id, run_attempt=run_attempt)],
        jobs_complete=True,
        expected_workflow_run_id=RUN_ID,
        expected_run_attempt=RUN_ATTEMPT,
        expected_head_sha=HEAD_SHA,
        expected_repository=REPOSITORY,
    )


def _decision(*, workflow_run_id, run_attempt, check_run_id=CHECK_RUN_ID) -> dict:
    return rvi.build_decision(
        repository=REPOSITORY,
        pull_request_number=2230,
        base_sha="b" * 40,
        head_sha=HEAD_SHA,
        base_registry_blob_sha="c" * 40,
        head_registry_blob_sha="d" * 40,
        pr_body="",
        changed_path_entries=[],
        affected_surfaces=[],
        component_vrt_report_check_run_id=str(check_run_id) if check_run_id is not None else None,
        github_actions_app_identity=f"{rvi.GITHUB_ACTIONS_APP_SLUG}[bot]",
        artifact_id="artifact-1",
        artifact_digest="e" * 64,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
    )


def _manifest_record(*, workflow_run_id, run_attempt, head_sha=HEAD_SHA, surface_id="combat-hud-running") -> dict:
    # `run_attempt` is NOT part of `_MANIFEST_V2_RECORD_FIELDS` /
    # `manifest_sha256` (Issue #2230 fix_delta: the tamper-evidence digest
    # must stay identical to the base-branch-locked consumer's field set --
    # attempt-forgery is instead caught by the separate
    # `evidence_manifest_run_attempt_mismatch` cross-check). Set it on the
    # returned dict after construction, outside the digested field set.
    record = rvi.build_evidence_manifest_v2_record(
        surface_id=surface_id,
        contract_digest="f" * 64,
        head_sha=head_sha,
        workflow_run_id=workflow_run_id,
        check_run_id=None,
        check_suite_id=None,
        github_app_id=None,
        github_app_slug=None,
        check_conclusion=None,
        baseline_path="tests/component/__screenshots__/x.png",
        baseline_sha256="1" * 64,
        actual_sha256="1" * 64,
        mismatched_pixels=0,
        verify_command_id="vitest_component_vrt_verify",
        verify_succeeded=True,
        update_command_id="vitest_component_vrt_update",
        update_executed=False,
        update_succeeded=False,
        expected_artifact_id="e1",
        actual_artifact_id="a1",
        diff_artifact_id="d1",
    )
    record["run_attempt"] = run_attempt
    return record


def _verify(decision: dict, manifest: dict | None, *, provenance=None):
    trusted = rvi.TrustedRederivation(
        component_vrt_checkrun_provenance=provenance,
        require_component_vrt_checkrun_provenance=provenance is not None,
    )
    return rvi.verify_trusted_artifact(
        decision_raw=json.dumps(decision).encode("utf-8"),
        evidence_manifest_raw=(json.dumps(manifest).encode("utf-8") if manifest is not None else None),
        visual_impact_schema_path=SCHEMA_PATH,
        expected_head_sha=HEAD_SHA,
        expected_repository=REPOSITORY,
        expected_pr_number=2230,
        trusted_rederivation=trusted,
    )


# ---------------------------------------------------------------------------
# AC2: tuple_binding_both_artifacts
# ---------------------------------------------------------------------------


def test_tuple_binding_both_artifacts_matching_passes():
    """GIVEN a decision AND evidence-manifest record both carrying the exact
    authenticated (workflow_run_id, run_attempt, head_sha) tuple, WHEN
    verify_trusted_artifact() runs THEN the verdict is ok with no
    attempt-binding reason codes."""
    provenance = _provenance()
    assert provenance.ok is True
    decision = _decision(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT)
    decision["affected_surfaces"] = [
        {
            "surface_id": "combat-hud-running",
            "contract_id": "combat-hud-running:vitest-browser-mode",
            "disposition": "verified_unchanged",
            "evidence": {"evidence_manifest_digest": "placeholder"},
        }
    ]
    record = _manifest_record(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT)
    decision["affected_surfaces"][0]["evidence"]["evidence_manifest_digest"] = record["manifest_sha256"]
    manifest = {"schema": rvi.EVIDENCE_MANIFEST_V2_SCHEMA, "surfaces": [record]}
    verdict = _verify(decision, manifest, provenance=provenance)
    assert verdict.ok is True
    assert verdict.reason_codes == []


def test_tuple_binding_both_artifacts_decision_workflow_run_id_mismatch_fails_closed():
    provenance = _provenance()
    decision = _decision(workflow_run_id=RUN_ID + 1, run_attempt=RUN_ATTEMPT)
    verdict = _verify(decision, manifest=None, provenance=provenance)
    assert verdict.ok is False
    assert "decision_workflow_run_id_mismatch" in verdict.reason_codes


def test_tuple_binding_both_artifacts_decision_run_attempt_mismatch_fails_closed():
    provenance = _provenance()
    decision = _decision(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT + 1)
    verdict = _verify(decision, manifest=None, provenance=provenance)
    assert verdict.ok is False
    assert "decision_run_attempt_mismatch" in verdict.reason_codes


def test_tuple_binding_both_artifacts_evidence_manifest_run_attempt_mismatch_fails_closed():
    """Evidence manifest record from a DIFFERENT (stale) attempt must never
    be accepted even when its own tamper-evidence digest self-verifies."""
    provenance = _provenance()
    decision = _decision(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT)
    stale_record = _manifest_record(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT - 1)
    decision["affected_surfaces"] = [
        {
            "surface_id": "combat-hud-running",
            "contract_id": "combat-hud-running:vitest-browser-mode",
            "disposition": "verified_unchanged",
            "evidence": {"evidence_manifest_digest": stale_record["manifest_sha256"]},
        }
    ]
    manifest = {"schema": rvi.EVIDENCE_MANIFEST_V2_SCHEMA, "surfaces": [stale_record]}
    verdict = _verify(decision, manifest, provenance=provenance)
    assert verdict.ok is False
    assert any(code.startswith("evidence_manifest_run_attempt_mismatch") for code in verdict.reason_codes)


def test_tuple_binding_both_artifacts_evidence_manifest_workflow_run_id_mismatch_fails_closed():
    provenance = _provenance()
    decision = _decision(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT)
    stale_record = _manifest_record(workflow_run_id=RUN_ID + 999, run_attempt=RUN_ATTEMPT)
    decision["affected_surfaces"] = [
        {
            "surface_id": "combat-hud-running",
            "contract_id": "combat-hud-running:vitest-browser-mode",
            "disposition": "verified_unchanged",
            "evidence": {"evidence_manifest_digest": stale_record["manifest_sha256"]},
        }
    ]
    manifest = {"schema": rvi.EVIDENCE_MANIFEST_V2_SCHEMA, "surfaces": [stale_record]}
    verdict = _verify(decision, manifest, provenance=provenance)
    assert verdict.ok is False
    assert any(code.startswith("evidence_manifest_workflow_run_id_mismatch") for code in verdict.reason_codes)


def test_tuple_binding_both_artifacts_evidence_manifest_head_sha_mismatch_fails_closed():
    provenance = _provenance()
    decision = _decision(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT)
    stale_record = _manifest_record(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT, head_sha="f" * 40)
    decision["affected_surfaces"] = [
        {
            "surface_id": "combat-hud-running",
            "contract_id": "combat-hud-running:vitest-browser-mode",
            "disposition": "verified_unchanged",
            "evidence": {"evidence_manifest_digest": stale_record["manifest_sha256"]},
        }
    ]
    manifest = {"schema": rvi.EVIDENCE_MANIFEST_V2_SCHEMA, "surfaces": [stale_record]}
    verdict = _verify(decision, manifest, provenance=provenance)
    assert verdict.ok is False
    assert any(code.startswith("evidence_manifest_head_sha_mismatch") for code in verdict.reason_codes)


def test_v2_record_head_sha_mismatch_fails_closed_without_trusted_rederivation():
    """Issue #2379 OWNER fix_delta P2-1 (regression fix): this head_sha
    check must fire even when the caller passes NO `trusted_rederivation`
    at all (`None`, the plain V2 fallback verification path used by any
    caller that never supplies CheckRun provenance) -- previously the
    check only ran INSIDE the `trusted_rederivation is not None and
    trusted_rederivation.component_vrt_checkrun_provenance` guard, so a
    `trusted_rederivation=None` caller never detected a record whose
    `head_sha` does not match the expected candidate PR head at all,
    even though the decision itself and the record's own tamper-evidence
    digest are both otherwise valid."""
    decision = _decision(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT)
    mismatched_record = _manifest_record(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT, head_sha="f" * 40)
    decision["affected_surfaces"] = [
        {
            "surface_id": "combat-hud-running",
            "contract_id": "combat-hud-running:vitest-browser-mode",
            "disposition": "verified_unchanged",
            "evidence": {"evidence_manifest_digest": mismatched_record["manifest_sha256"]},
        }
    ]
    manifest = {"schema": rvi.EVIDENCE_MANIFEST_V2_SCHEMA, "surfaces": [mismatched_record]}
    verdict = rvi.verify_trusted_artifact(
        decision_raw=json.dumps(decision).encode("utf-8"),
        evidence_manifest_raw=json.dumps(manifest).encode("utf-8"),
        visual_impact_schema_path=SCHEMA_PATH,
        expected_head_sha=HEAD_SHA,
        expected_repository=REPOSITORY,
        expected_pr_number=2230,
        trusted_rederivation=None,
    )
    assert verdict.ok is False
    assert any(code.startswith("evidence_manifest_head_sha_mismatch") for code in verdict.reason_codes), (
        verdict.reason_codes
    )


def test_tuple_binding_both_artifacts_schema_requires_workflow_run_id_and_run_attempt():
    """AC2's `rg` baseline-check counterpart: the schema itself now REQUIRES
    workflow_run_id/run_attempt on VISUAL_IMPACT_DECISION_V1 (a decision
    missing either is schema-invalid, never silently accepted)."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    decision_schema = schema["$defs"]["VISUAL_IMPACT_DECISION_V1"]
    assert "workflow_run_id" in decision_schema["required"]
    assert "run_attempt" in decision_schema["required"]
    assert decision_schema["properties"]["workflow_run_id"]["type"] == "integer"
    assert decision_schema["properties"]["workflow_run_id"]["minimum"] == 1
    assert decision_schema["properties"]["run_attempt"]["type"] == "integer"
    assert decision_schema["properties"]["run_attempt"]["minimum"] == 1


# ---------------------------------------------------------------------------
# acquire_trusted_artifact(): injectable-transport fixture harness (mirrors
# acquire_component_vrt_checkrun()'s HttpTransportResponse pattern).
# ---------------------------------------------------------------------------


def _artifact(
    *, artifact_id: int, name: str, expired: bool = False, workflow_run: dict | None = None
) -> dict:
    record = {"id": artifact_id, "name": name, "expired": expired}
    if workflow_run is not None:
        record["workflow_run"] = workflow_run
    return record


def _paged_transport(
    pages: list[list[dict]],
    *,
    total_count: int | None = None,
    total_counts: list[int] | None = None,
    status_code: int = 200,
):
    """Builds a transport callable serving `pages` (one page per call, in
    order) as `{"total_count": ..., "artifacts": [...]}` responses.

    - `total_counts` (one value per page) lets a test simulate the
      `total_count` value CHANGING between pages (fail-closed case).
    - Otherwise `total_count` (a single fixed value for every page) is used,
      defaulting to the sum of every page's length (the "honest" case)."""
    computed_total = total_count if total_count is not None else sum(len(p) for p in pages)
    call_state = {"index": 0}

    def _transport(path: str) -> "rvi.HttpTransportResponse":
        idx = call_state["index"]
        call_state["index"] += 1
        if total_counts is not None:
            page_total = total_counts[idx] if idx < len(total_counts) else total_counts[-1]
        else:
            page_total = computed_total
        if idx >= len(pages):
            return rvi.HttpTransportResponse(
                status_code=status_code, json_body={"total_count": page_total, "artifacts": []}
            )
        return rvi.HttpTransportResponse(
            status_code=status_code, json_body={"total_count": page_total, "artifacts": pages[idx]}
        )

    return _transport


# ---------------------------------------------------------------------------
# AC3: pagination
# ---------------------------------------------------------------------------


def test_pagination_completes_across_multiple_pages_and_succeeds():
    page1 = [_artifact(artifact_id=1, name="other-artifact")]
    page2 = [_artifact(artifact_id=2, name=DECISION_NAME)]
    transport = _paged_transport([page1, page2])
    result = rvi.acquire_trusted_artifact(
        transport=transport,
        repository=REPOSITORY,
        run_id=RUN_ID,
        expected_artifact_name=DECISION_NAME,
        page_size=1,
    )
    assert result.ok is True
    assert result.artifact_id == 2


def test_pagination_total_count_mismatch_across_pages_fails_closed():
    """`total_count` CHANGING between pages of the SAME paginated request
    (e.g. a concurrent rerun deleting/adding an artifact mid-walk) must
    never be silently tolerated -- fail closed rather than trusting
    whichever page's count happened to be read first."""
    page1 = [_artifact(artifact_id=1, name=DECISION_NAME)]
    page2 = [_artifact(artifact_id=2, name="other")]
    transport = _paged_transport([page1, page2], total_counts=[2, 99])
    result = rvi.acquire_trusted_artifact(
        transport=transport,
        repository=REPOSITORY,
        run_id=RUN_ID,
        expected_artifact_name=DECISION_NAME,
        page_size=1,
    )
    assert result.ok is False
    assert "trusted_artifact_total_count_changed" in result.reason_codes


def test_pagination_incomplete_short_page_before_total_reached_fails_closed():
    """A page that returns FEWER items than `total_count` implies (an empty
    trailing page before the declared total is reached) must never be
    silently accepted as "done"."""
    page1 = [_artifact(artifact_id=1, name=DECISION_NAME)]
    transport = _paged_transport([page1], total_count=5)
    result = rvi.acquire_trusted_artifact(
        transport=transport,
        repository=REPOSITORY,
        run_id=RUN_ID,
        expected_artifact_name=DECISION_NAME,
        page_size=1,
    )
    assert result.ok is False
    assert "trusted_artifact_pagination_incomplete" in result.reason_codes


def test_pagination_http_status_non_200_fails_closed():
    transport = _paged_transport([[_artifact(artifact_id=1, name=DECISION_NAME)]], status_code=500)
    result = rvi.acquire_trusted_artifact(
        transport=transport, repository=REPOSITORY, run_id=RUN_ID, expected_artifact_name=DECISION_NAME
    )
    assert result.ok is False
    assert "trusted_artifact_http_status_invalid" in result.reason_codes


# ---------------------------------------------------------------------------
# AC4: attempt_specific_name_cardinality
# ---------------------------------------------------------------------------


def test_attempt_specific_name_cardinality_zero_matches_fails_closed():
    transport = _paged_transport([[_artifact(artifact_id=1, name="unrelated-name")]])
    result = rvi.acquire_trusted_artifact(
        transport=transport, repository=REPOSITORY, run_id=RUN_ID, expected_artifact_name=DECISION_NAME
    )
    assert result.ok is False
    assert f"trusted_artifact_cardinality_invalid:{DECISION_NAME}" in result.reason_codes


def test_attempt_specific_name_cardinality_multiple_matches_fails_closed():
    transport = _paged_transport(
        [[_artifact(artifact_id=1, name=DECISION_NAME), _artifact(artifact_id=2, name=DECISION_NAME)]]
    )
    result = rvi.acquire_trusted_artifact(
        transport=transport, repository=REPOSITORY, run_id=RUN_ID, expected_artifact_name=DECISION_NAME
    )
    assert result.ok is False
    assert f"trusted_artifact_cardinality_invalid:{DECISION_NAME}" in result.reason_codes


def test_attempt_specific_name_cardinality_same_name_expired_plus_live_fails_closed():
    """Issue #2230 fix_delta P1-4 (human reviewer): cardinality-exactly-one
    is evaluated over the NAME match ALONE, never pre-filtered by `expired`.
    A same-named expired artifact coexisting with a same-named live one
    (e.g. an old run-attempt's expired artifact plus the current attempt's
    live one) is 2 name matches -- a cardinality violation, regardless of
    expired status -- and must never be silently narrowed to "exactly one"
    by excluding the expired copy before the cardinality check runs."""
    transport = _paged_transport(
        [
            [
                _artifact(artifact_id=1, name=DECISION_NAME, expired=True),
                _artifact(artifact_id=2, name=DECISION_NAME, expired=False),
            ]
        ]
    )
    result = rvi.acquire_trusted_artifact(
        transport=transport, repository=REPOSITORY, run_id=RUN_ID, expected_artifact_name=DECISION_NAME
    )
    assert result.ok is False
    assert f"trusted_artifact_cardinality_invalid:{DECISION_NAME}" in result.reason_codes


def test_attempt_specific_name_cardinality_exactly_one_expired_only_fails_closed():
    """Exactly one same-named match, but that sole artifact is expired --
    cardinality is satisfied (exactly one), so this must fail on the
    SEPARATE expired check, not be conflated with a cardinality violation."""
    transport = _paged_transport([[_artifact(artifact_id=1, name=DECISION_NAME, expired=True)]])
    result = rvi.acquire_trusted_artifact(
        transport=transport, repository=REPOSITORY, run_id=RUN_ID, expected_artifact_name=DECISION_NAME
    )
    assert result.ok is False
    assert f"trusted_artifact_expired:{DECISION_NAME}" in result.reason_codes


# ---------------------------------------------------------------------------
# Issue #2230 fix_delta P2-1 (best-effort): nested workflow_run.id/head_sha
# cross-check on the SELECTED artifact, when `expected_head_sha` is supplied.
# ---------------------------------------------------------------------------

EXPECTED_HEAD_SHA_FOR_ACQUIRE = "f" * 40


def test_acquire_trusted_artifact_nested_workflow_run_head_sha_mismatch_rejected():
    """A same-named, non-expired, cardinality-exactly-one artifact whose
    nested `workflow_run.head_sha` does NOT match the caller-supplied
    `expected_head_sha` must be rejected, not silently accepted."""
    transport = _paged_transport(
        [
            [
                _artifact(
                    artifact_id=1,
                    name=DECISION_NAME,
                    workflow_run={"id": RUN_ID, "head_sha": "0" * 40},
                )
            ]
        ]
    )
    result = rvi.acquire_trusted_artifact(
        transport=transport,
        repository=REPOSITORY,
        run_id=RUN_ID,
        expected_artifact_name=DECISION_NAME,
        expected_head_sha=EXPECTED_HEAD_SHA_FOR_ACQUIRE,
    )
    assert result.ok is False
    assert f"trusted_artifact_workflow_run_head_sha_mismatch:{DECISION_NAME}" in result.reason_codes


def test_acquire_trusted_artifact_nested_workflow_run_id_mismatch_rejected():
    """Same, but the nested `workflow_run.id` (not `head_sha`) is the field
    that disagrees with the caller-supplied `run_id`."""
    transport = _paged_transport(
        [
            [
                _artifact(
                    artifact_id=1,
                    name=DECISION_NAME,
                    workflow_run={"id": RUN_ID + 1, "head_sha": EXPECTED_HEAD_SHA_FOR_ACQUIRE},
                )
            ]
        ]
    )
    result = rvi.acquire_trusted_artifact(
        transport=transport,
        repository=REPOSITORY,
        run_id=RUN_ID,
        expected_artifact_name=DECISION_NAME,
        expected_head_sha=EXPECTED_HEAD_SHA_FOR_ACQUIRE,
    )
    assert result.ok is False
    assert f"trusted_artifact_workflow_run_id_mismatch:{DECISION_NAME}" in result.reason_codes


def test_acquire_trusted_artifact_nested_workflow_run_missing_rejected():
    """When `expected_head_sha` is supplied but the artifact response omits
    the `workflow_run` object entirely, this must fail closed rather than
    silently skip the check."""
    transport = _paged_transport([[_artifact(artifact_id=1, name=DECISION_NAME)]])
    result = rvi.acquire_trusted_artifact(
        transport=transport,
        repository=REPOSITORY,
        run_id=RUN_ID,
        expected_artifact_name=DECISION_NAME,
        expected_head_sha=EXPECTED_HEAD_SHA_FOR_ACQUIRE,
    )
    assert result.ok is False
    assert f"trusted_artifact_workflow_run_missing:{DECISION_NAME}" in result.reason_codes


def test_acquire_trusted_artifact_nested_workflow_run_matching_tuple_accepted():
    """Companion positive case: a matching nested
    `(workflow_run.id, workflow_run.head_sha)` tuple must still succeed."""
    transport = _paged_transport(
        [
            [
                _artifact(
                    artifact_id=1,
                    name=DECISION_NAME,
                    workflow_run={"id": RUN_ID, "head_sha": EXPECTED_HEAD_SHA_FOR_ACQUIRE},
                )
            ]
        ]
    )
    result = rvi.acquire_trusted_artifact(
        transport=transport,
        repository=REPOSITORY,
        run_id=RUN_ID,
        expected_artifact_name=DECISION_NAME,
        expected_head_sha=EXPECTED_HEAD_SHA_FOR_ACQUIRE,
    )
    assert result.ok is True
    assert result.artifact_id == 1


def test_acquire_trusted_artifact_no_expected_head_sha_skips_nested_check():
    """When `expected_head_sha` is omitted (default `None`), the nested
    cross-check must be skipped entirely -- existing callers that never
    supply it keep their prior behavior unchanged."""
    transport = _paged_transport([[_artifact(artifact_id=1, name=DECISION_NAME)]])
    result = rvi.acquire_trusted_artifact(
        transport=transport, repository=REPOSITORY, run_id=RUN_ID, expected_artifact_name=DECISION_NAME
    )
    assert result.ok is True
    assert result.artifact_id == 1


# ---------------------------------------------------------------------------
# AC5: attempt_identity
# ---------------------------------------------------------------------------


def test_attempt_identity_decision_content_must_match_authenticated_run_attempt():
    """Even when `acquire_trusted_artifact()` correctly selects the single
    expected-name artifact, a decision whose CONTENT claims a different
    run_attempt than the trusted consumer's authenticated CheckRun
    provenance is rejected by verify_trusted_artifact()."""
    provenance = _provenance(run_attempt=RUN_ATTEMPT)
    forged_decision = _decision(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT - 1)
    verdict = _verify(forged_decision, manifest=None, provenance=provenance)
    assert verdict.ok is False
    assert "decision_run_attempt_mismatch" in verdict.reason_codes


def test_attempt_identity_selection_binds_run_id_via_path_never_cross_run():
    """`acquire_trusted_artifact()` scopes its query to a specific `run_id`
    path segment -- an artifact belonging to a DIFFERENT run_id can never be
    silently returned even if named identically (the fake transport here
    only ever serves this run_id's own artifacts, proving the function
    never needs/uses a global search across runs)."""
    transport = _paged_transport([[_artifact(artifact_id=42, name=DECISION_NAME)]])
    result = rvi.acquire_trusted_artifact(
        transport=transport, repository=REPOSITORY, run_id=RUN_ID, expected_artifact_name=DECISION_NAME
    )
    assert result.ok is True
    assert result.artifact_id == 42


# ---------------------------------------------------------------------------
# AC6 (negative control): old_current_coexistence_negative
# ---------------------------------------------------------------------------


def test_old_current_coexistence_negative_ambiguous_same_name_fails_closed():
    """GIVEN a same-run fixture where BOTH an attempt-1-produced artifact and
    an attempt-2-produced artifact share the SAME (non-attempt-specific)
    name -- e.g. a producer bug that failed to suffix the name -- WHEN
    attempt-2's trusted consumer queries for that name THEN acquisition
    fails closed on cardinality (never silently adopts either one, and
    never silently prefers "the first" the way the pre-#2230 `[0]` pick
    did)."""
    shared_name = "visual-impact-decision-v1"  # deliberately NOT attempt-specific
    old_attempt_artifact = _artifact(artifact_id=100, name=shared_name)
    current_attempt_artifact = _artifact(artifact_id=200, name=shared_name)
    transport = _paged_transport([[old_attempt_artifact, current_attempt_artifact]])
    result = rvi.acquire_trusted_artifact(
        transport=transport, repository=REPOSITORY, run_id=RUN_ID, expected_artifact_name=shared_name
    )
    assert result.ok is False
    assert f"trusted_artifact_cardinality_invalid:{shared_name}" in result.reason_codes


def test_old_current_coexistence_negative_stale_content_never_leaks_into_final_verdict():
    """Even in the pathological "old+current coexist" scenario, if an OLD
    attempt's evidence-manifest record is (mis-)selected and its content fed
    to verify_trusted_artifact() alongside a CURRENT-attempt decision, the
    stale attempt's content must never be silently adopted into the final
    verdict -- the tuple mismatch is caught regardless of how the artifact
    was acquired."""
    provenance = _provenance(run_id=RUN_ID, run_attempt=RUN_ATTEMPT)
    current_decision = _decision(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT)
    old_record = _manifest_record(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT - 1)
    current_decision["affected_surfaces"] = [
        {
            "surface_id": "combat-hud-running",
            "contract_id": "combat-hud-running:vitest-browser-mode",
            "disposition": "verified_unchanged",
            "evidence": {"evidence_manifest_digest": old_record["manifest_sha256"]},
        }
    ]
    old_manifest = {"schema": rvi.EVIDENCE_MANIFEST_V2_SCHEMA, "surfaces": [old_record]}
    verdict = _verify(current_decision, old_manifest, provenance=provenance)
    assert verdict.ok is False
    assert any(code.startswith("evidence_manifest_run_attempt_mismatch") for code in verdict.reason_codes)


# ---------------------------------------------------------------------------
# AC7 (positive control): old_current_coexistence_positive
# ---------------------------------------------------------------------------


def test_old_current_coexistence_positive_selects_current_attempt_artifacts_uniquely():
    """GIVEN a same-run fixture where attempt 1 and attempt 2 artifacts
    coexist under CORRECT attempt-specific names (the fixed producer
    behaviour this Issue introduces) WHEN attempt-2's trusted consumer
    queries for its OWN expected attempt-specific name THEN it selects
    exactly the 2 current-attempt artifacts (decision + evidence) and
    ignores attempt 1's, regardless of run-wide artifact count."""
    old_decision_name = "visual-impact-decision-v1-1"
    old_evidence_name = "component-vrt-evidence-manifest-1"
    all_run_artifacts = [
        _artifact(artifact_id=10, name=old_decision_name),
        _artifact(artifact_id=11, name=old_evidence_name),
        _artifact(artifact_id=20, name=DECISION_NAME),
        _artifact(artifact_id=21, name=EVIDENCE_NAME),
    ]
    decision_transport = _paged_transport([all_run_artifacts])
    decision_result = rvi.acquire_trusted_artifact(
        transport=decision_transport, repository=REPOSITORY, run_id=RUN_ID, expected_artifact_name=DECISION_NAME
    )
    assert decision_result.ok is True
    assert decision_result.artifact_id == 20

    evidence_transport = _paged_transport([all_run_artifacts])
    evidence_result = rvi.acquire_trusted_artifact(
        transport=evidence_transport, repository=REPOSITORY, run_id=RUN_ID, expected_artifact_name=EVIDENCE_NAME
    )
    assert evidence_result.ok is True
    assert evidence_result.artifact_id == 21


def test_old_current_coexistence_positive_end_to_end_verdict_ok_true():
    """The full pipeline: attempt-2 artifacts selected uniquely (AC7) AND
    their CONTENT tuple matches the authenticated provenance (AC2) AND the
    coexisting attempt-1 artifacts never influence the outcome -- final
    verdict is ok=True."""
    provenance = _provenance(run_id=RUN_ID, run_attempt=RUN_ATTEMPT)
    assert provenance.ok is True

    current_record = _manifest_record(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT)
    current_decision = _decision(workflow_run_id=RUN_ID, run_attempt=RUN_ATTEMPT)
    current_decision["affected_surfaces"] = [
        {
            "surface_id": "combat-hud-running",
            "contract_id": "combat-hud-running:vitest-browser-mode",
            "disposition": "verified_unchanged",
            "evidence": {"evidence_manifest_digest": current_record["manifest_sha256"]},
        }
    ]
    current_manifest = {"schema": rvi.EVIDENCE_MANIFEST_V2_SCHEMA, "surfaces": [current_record]}

    # Simulate artifact-selection first (AC7), confirming it would have
    # picked the CURRENT-attempt artifacts out of a coexisting run.
    all_run_artifacts = [
        _artifact(artifact_id=10, name="visual-impact-decision-v1-1"),
        _artifact(artifact_id=20, name=DECISION_NAME),
    ]
    selection = rvi.acquire_trusted_artifact(
        transport=_paged_transport([all_run_artifacts]),
        repository=REPOSITORY,
        run_id=RUN_ID,
        expected_artifact_name=DECISION_NAME,
    )
    assert selection.ok is True
    assert selection.artifact_id == 20

    verdict = _verify(current_decision, current_manifest, provenance=provenance)
    assert verdict.ok is True
    assert verdict.reason_codes == []
