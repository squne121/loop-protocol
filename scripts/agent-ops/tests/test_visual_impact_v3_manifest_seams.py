"""test_visual_impact_v3_manifest_seams.py (Issue #2284 Phase A)

Covers the `VISUAL_BASELINE_REVIEW_EVIDENCE_V3` schema Issue #2284 adds on
top of the existing V2 manifest (`test_visual_impact_v2_manifest_seams.py`,
unchanged/still passing -- AC4):

- AC1: V3 envelope (`schema`/`workflow_run_id`/`run_attempt`/`head_sha`/
  `surfaces`) required-field + strict type/range validation.
- AC2: `run_attempt` participates in the per-record tamper-evidence digest
  (unlike V2, where it is compatibility-excluded).
- AC3: closed record shape -- missing/extra key, invalid `run_attempt`
  type/range, duplicate `surface_id`, unknown schema all rejected.
- AC5: real producer (`--mode build-evidence-manifest --evidence-manifest-
  schema v3`) -> policy (`evaluate_pr_policy()`) -> trusted-verifier
  (`verify_trusted_artifact()`) seam, exercised via subprocess (never a
  synthetic in-process fixture standing in for the real CLI).
- AC6: no-impact (`surfaces: []`) identity -- PASS only when the envelope
  tuple matches, FAIL on mismatch/omission.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "resolve_visual_impact.py"
_MODULE_NAME = "resolve_visual_impact_issue_2284_v3_manifest_seams"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
rvi = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = rvi
_spec.loader.exec_module(rvi)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "agent-ops" / "resolve_visual_impact.py"
REGISTRY_PATH = REPO_ROOT / "docs" / "dev" / "visual-surfaces.yml"
SCHEMA_PATH = REPO_ROOT / "docs" / "dev" / "visual-surfaces.schema.json"
VISUAL_IMPACT_SCHEMA_PATH = REPO_ROOT / "docs" / "dev" / "visual-impact.schema.json"


def _v3_record_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "surface_id": "combat-hud-running",
        "contract_digest": "c" * 64,
        "head_sha": "a" * 40,
        "workflow_run_id": 100,
        "run_attempt": 1,
        "check_run_id": None,
        "check_suite_id": None,
        "github_app_id": None,
        "github_app_slug": None,
        "check_conclusion": None,
        "baseline_path": "docs/dev/visual-baselines/combat-hud-running.png",
        "baseline_sha256": "b" * 64,
        "actual_sha256": "b" * 64,
        "mismatched_pixels": 0,
        "verify_command_id": "vc1",
        "verify_succeeded": True,
        "update_command_id": "uc1",
        "update_executed": False,
        "update_succeeded": False,
        "expected_artifact_id": "1",
        "actual_artifact_id": "2",
        "diff_artifact_id": "3",
    }
    fields.update(overrides)
    return fields


def _v3_manifest(*, workflow_run_id: int = 100, run_attempt: int = 1, head_sha: str = "a" * 40, surfaces=None) -> dict:
    if surfaces is None:
        surfaces = [rvi.build_evidence_manifest_v3_record(**_v3_record_fields())]
    return {
        "schema": rvi.EVIDENCE_MANIFEST_V3_SCHEMA,
        "workflow_run_id": workflow_run_id,
        "run_attempt": run_attempt,
        "head_sha": head_sha,
        "surfaces": surfaces,
    }


# ---------------------------------------------------------------------------
# AC1: envelope required-field + strict type/range validation.
# ---------------------------------------------------------------------------


def test_v3_envelope_accepts_well_formed_manifest() -> None:
    """GIVEN a fully well-formed V3 manifest (envelope + one closed-shape
    record whose identity tuple matches the envelope) WHEN validated THEN
    no errors are reported."""
    manifest = _v3_manifest()
    assert rvi.validate_evidence_manifest_v3_envelope(manifest) == []


def test_v3_envelope_rejects_missing_required_fields() -> None:
    """AC1: `schema`/`workflow_run_id`/`run_attempt`/`head_sha`/`surfaces`
    are ALL required at the envelope level."""
    for missing_field in ("schema", "workflow_run_id", "run_attempt", "head_sha", "surfaces"):
        manifest = _v3_manifest()
        del manifest[missing_field]
        errors = rvi.validate_evidence_manifest_v3_envelope(manifest)
        assert any(f"missing_field:{missing_field}" in e for e in errors), (missing_field, errors)


def test_v3_envelope_rejects_non_int_and_out_of_range_workflow_run_id_and_run_attempt() -> None:
    """AC1: `workflow_run_id`/`run_attempt` must be real JSON integers
    `>= 1` -- string/bool/float/zero/negative all rejected, same strict
    validator used for the per-record identity fields elsewhere."""
    for bad_value in ("100", True, 1.0, 0, -1, None):
        manifest = _v3_manifest()
        manifest["workflow_run_id"] = bad_value
        errors = rvi.validate_evidence_manifest_v3_envelope(manifest)
        assert any("invalid_workflow_run_id" in e for e in errors), (bad_value, errors)

        manifest2 = _v3_manifest()
        manifest2["run_attempt"] = bad_value
        errors2 = rvi.validate_evidence_manifest_v3_envelope(manifest2)
        assert any("invalid_run_attempt" in e for e in errors2), (bad_value, errors2)


def test_v3_envelope_rejects_malformed_head_sha() -> None:
    """AC1: `head_sha` must be a full 40-character LOWERCASE hex string --
    short, uppercase, and non-hex values are all rejected."""
    for bad_head_sha in ("a" * 39, "A" * 40, "z" * 40, "", 12345):
        manifest = _v3_manifest()
        manifest["head_sha"] = bad_head_sha
        errors = rvi.validate_evidence_manifest_v3_envelope(manifest)
        assert any("invalid_head_sha" in e for e in errors), (bad_head_sha, errors)


def test_v3_envelope_rejects_record_identity_mismatch_with_envelope() -> None:
    """Issue #2284 In Scope: each surface record's OWN `head_sha`/
    `workflow_run_id`/`run_attempt` must match the envelope's -- a record
    silently carrying a DIFFERENT identity than the envelope it is bundled
    with is rejected even though the record's own digest self-verifies."""
    record = rvi.build_evidence_manifest_v3_record(**_v3_record_fields(head_sha="f" * 40))
    manifest = _v3_manifest(surfaces=[record])
    errors = rvi.validate_evidence_manifest_v3_envelope(manifest)
    assert any("record_head_sha_mismatch" in e for e in errors), errors


# ---------------------------------------------------------------------------
# AC2: run_attempt participates in the per-record digest.
# ---------------------------------------------------------------------------


def test_v3_record_digest_differs_when_only_run_attempt_differs() -> None:
    """AC2: two records identical in every OTHER field but `run_attempt`
    must have DIFFERENT `manifest_sha256` values (V2's `run_attempt` is
    compatibility-excluded from its digest; V3 includes it)."""
    record_attempt_1 = rvi.build_evidence_manifest_v3_record(**_v3_record_fields(run_attempt=1))
    record_attempt_2 = rvi.build_evidence_manifest_v3_record(**_v3_record_fields(run_attempt=2))
    assert record_attempt_1["manifest_sha256"] != record_attempt_2["manifest_sha256"]


def test_v3_record_digest_rejects_run_attempt_tampered_after_digest_computed() -> None:
    """AC2: a record whose `run_attempt` is changed AFTER
    `manifest_sha256` was computed must fail
    `verify_evidence_manifest_v3_record_digest()` -- unlike V2's
    compatibility behavior (see the V2 golden-digest test's companion
    assertion), where the identical mutation is explicitly tolerated."""
    record = rvi.build_evidence_manifest_v3_record(**_v3_record_fields(run_attempt=1))
    assert rvi.verify_evidence_manifest_v3_record_digest(record) is True
    tampered = dict(record)
    tampered["run_attempt"] = 2
    assert rvi.verify_evidence_manifest_v3_record_digest(tampered) is False


def test_build_evidence_manifest_v3_record_matches_golden_digest_fixture() -> None:
    """GIVEN a fully-populated, fixed-value V3 record built via the real
    production `build_evidence_manifest_v3_record()` WHEN its
    `manifest_sha256` is compared against an INDEPENDENTLY computed
    (hand-rolled canonical JSON + hashlib, never calling the production
    digest helper) expected SHA-256 THEN they match -- pinning the exact
    V3 digest algorithm (canonical JSON key order / separators / excluded
    `manifest_sha256` field / `run_attempt` INCLUSION in V3's digest
    input, the exact opposite of V2) against silent regression."""
    record = rvi.build_evidence_manifest_v3_record(**_v3_record_fields())
    digest_input = {name: record.get(name) for name in rvi._MANIFEST_V3_RECORD_FIELDS}
    canonical = json.dumps(digest_input, sort_keys=True, separators=(",", ":"))
    expected_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert record["manifest_sha256"] == expected_sha256
    assert len(record["manifest_sha256"]) == 64
    assert rvi.verify_evidence_manifest_v3_record_digest(record) is True
    # `run_attempt` IS in the digest input (opposite of V2's golden-digest
    # companion assertion): the expected digest, per this test, is
    # computed OVER `run_attempt` -- proven above by including it in
    # `digest_input` and getting a match.
    assert "run_attempt" in rvi._MANIFEST_V3_RECORD_FIELDS


# ---------------------------------------------------------------------------
# AC3: closed record shape.
# ---------------------------------------------------------------------------


def test_v3_closed_shape_rejects_missing_key() -> None:
    fields = _v3_record_fields()
    del fields["baseline_sha256"]
    try:
        rvi.build_evidence_manifest_v3_record(**fields)
        raised = False
    except ValueError:
        raised = True
    assert raised, "build_evidence_manifest_v3_record must reject a missing field"


def test_v3_closed_shape_rejects_extra_key() -> None:
    fields = _v3_record_fields()
    fields["unexpected_extra_field"] = "not part of the schema"
    try:
        rvi.build_evidence_manifest_v3_record(**fields)
        raised = False
    except ValueError:
        raised = True
    assert raised, "build_evidence_manifest_v3_record must reject an unknown field"

    # Also exercise the READ-side closed-shape check (an untrusted manifest
    # a producer could hand-craft, never going through the builder at all).
    record = rvi.build_evidence_manifest_v3_record(**_v3_record_fields())
    record["unexpected_extra_field"] = "smuggled in"
    assert rvi.verify_evidence_manifest_v3_record_digest(record) is False


def test_v3_closed_shape_rejects_invalid_run_attempt_values() -> None:
    """AC3: string/bool/float/zero/negative/null `run_attempt` are all
    rejected by the read-side closed-shape+digest verifier."""
    for bad_run_attempt in ("1", True, 1.0, 0, -1, None):
        record = rvi.build_evidence_manifest_v3_record(**_v3_record_fields())
        record["run_attempt"] = bad_run_attempt
        assert rvi.verify_evidence_manifest_v3_record_digest(record) is False, bad_run_attempt


def test_v3_closed_shape_rejects_invalid_workflow_run_id_values() -> None:
    """Issue #2379 OWNER fix_delta P3: `workflow_run_id` was previously
    only compared with `!=` against the envelope (which would silently
    accept e.g. `100.0 == 100`) -- it is now strict-type-validated inside
    `_validate_v3_record_shape()` exactly like `run_attempt`, so
    string/bool/float/zero/negative/null are all rejected by the read-side
    closed-shape+digest verifier."""
    for bad_workflow_run_id in ("100", True, 100.0, 0, -1, None):
        record = rvi.build_evidence_manifest_v3_record(**_v3_record_fields())
        record["workflow_run_id"] = bad_workflow_run_id
        assert rvi.verify_evidence_manifest_v3_record_digest(record) is False, bad_workflow_run_id
        errors = rvi._validate_v3_record_shape(record)
        assert any("invalid_workflow_run_id" in e for e in errors), errors


def test_v3_closed_shape_rejects_duplicate_surface_id() -> None:
    """AC3: V3 does NOT inherit V2's "first match wins" lookup semantics --
    a manifest with more than one record sharing the same `surface_id` is
    rejected: `find_evidence_manifest_v3_record()` returns `None` for that
    surface_id (never silently picking either duplicate), AND
    `validate_evidence_manifest_v3_envelope()` flags the manifest as
    invalid outright."""
    record_a = rvi.build_evidence_manifest_v3_record(**_v3_record_fields(mismatched_pixels=0))
    record_b = rvi.build_evidence_manifest_v3_record(**_v3_record_fields(mismatched_pixels=5))
    manifest = _v3_manifest(surfaces=[record_a, record_b])

    assert rvi.find_evidence_manifest_v3_record(manifest, "combat-hud-running") is None

    errors = rvi.validate_evidence_manifest_v3_envelope(manifest)
    assert any("duplicate_surface_id" in e for e in errors), errors


def test_v3_closed_shape_rejects_unknown_schema_in_find_and_build_evidence() -> None:
    """AC3: a manifest whose `schema` is neither V2 nor V3 (or is simply
    absent) is never treated as a V3 manifest by the V3 lookup/consumption
    helpers."""
    manifest = _v3_manifest()
    manifest["schema"] = "VISUAL_BASELINE_REVIEW_EVIDENCE_V99"
    assert rvi.find_evidence_manifest_v3_record(manifest, "combat-hud-running") is None

    evidence = rvi.build_evidence_from_manifest_v3(
        manifest,
        surface_id="combat-hud-running",
        head_sha="a" * 40,
        expected_contract_digest="c" * 64,
        trusted_check_run_id="1",
        trusted_check_suite_id="2",
        trusted_github_app_id="3",
        trusted_github_app_slug="github-actions",
        trusted_check_conclusion="success",
    )
    assert evidence.evidence_manifest_surface_matches is False
    assert evidence.canonical_verify_success is False


# ---------------------------------------------------------------------------
# AC5: real producer (CLI subprocess) -> policy -> trusted-verifier seam.
# ---------------------------------------------------------------------------


def _run_build_evidence_manifest_v3_cli(
    surface_inputs_path: Path,
    manifest_output: Path,
    *,
    head_sha: str,
    run_id: int,
    run_attempt: int,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "uv",
            "run",
            "--locked",
            "python3",
            str(SCRIPT_PATH),
            "--mode",
            "build-evidence-manifest",
            "--evidence-manifest-schema",
            "v3",
            "--registry",
            str(REGISTRY_PATH),
            "--schema",
            str(SCHEMA_PATH),
            "--repo-root",
            str(REPO_ROOT),
            "--surface-inputs-file",
            str(surface_inputs_path),
            "--manifest-output",
            str(manifest_output),
            "--head-sha",
            head_sha,
            "--run-id",
            str(run_id),
            "--run-attempt",
            str(run_attempt),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _surface_inputs_json(*, head_sha: str, run_id: int, run_attempt: int) -> str:
    return json.dumps(
        [
            {
                "surface_id": "combat-hud-running",
                "head_sha": head_sha,
                "workflow_run_id": run_id,
                "run_attempt": run_attempt,
                "actual_sha256": "d" * 64,
                "mismatched_pixels": 0,
                "verify_succeeded": True,
                "update_executed": False,
                "update_succeeded": False,
                "expected_artifact_id": "10",
                "actual_artifact_id": "11",
                "diff_artifact_id": "12",
            }
        ]
    )


def test_build_evidence_manifest_cli_producer_seam_v3_produces_valid_manifest(tmp_path: Path) -> None:
    """AC5: GIVEN the real CLI argv `ci.yml`'s Phase B producer step uses
    (`--mode build-evidence-manifest --evidence-manifest-schema v3`) WHEN
    invoked as a subprocess THEN it produces a well-formed
    VISUAL_BASELINE_REVIEW_EVIDENCE_V3 manifest whose envelope validates
    and whose record digest self-verifies (covering `run_attempt`)."""
    head_sha = "c" * 40
    surface_inputs = tmp_path / "surface_inputs_v3.json"
    surface_inputs.write_text(_surface_inputs_json(head_sha=head_sha, run_id=42, run_attempt=1), encoding="utf-8")
    manifest_output = tmp_path / "manifest_v3.json"

    proc = _run_build_evidence_manifest_v3_cli(
        surface_inputs, manifest_output, head_sha=head_sha, run_id=42, run_attempt=1
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    manifest = json.loads(manifest_output.read_text(encoding="utf-8"))
    assert manifest["schema"] == "VISUAL_BASELINE_REVIEW_EVIDENCE_V3"
    assert manifest["workflow_run_id"] == 42
    assert manifest["run_attempt"] == 1
    assert manifest["head_sha"] == head_sha
    assert rvi.validate_evidence_manifest_v3_envelope(manifest) == []

    record = manifest["surfaces"][0]
    assert rvi.verify_evidence_manifest_v3_record_digest(record) is True
    tampered = dict(record)
    tampered["run_attempt"] = 999
    assert rvi.verify_evidence_manifest_v3_record_digest(tampered) is False


def test_v3_producer_seam_consumed_end_to_end_by_evaluate_pr_policy(tmp_path: Path) -> None:
    """AC5: GIVEN a real V3 manifest produced by the CLI producer path WHEN
    fed into `evaluate_pr_policy()` (the real policy-check consumer, dual-
    read dispatching on the manifest's OWN `schema` field) with trusted
    CheckRun binding params THEN the surface PASSES verified_unchanged."""
    head_sha = "1" * 40
    surface_inputs = tmp_path / "surface_inputs_v3_policy.json"
    surface_inputs.write_text(_surface_inputs_json(head_sha=head_sha, run_id=77, run_attempt=1), encoding="utf-8")
    manifest_output = tmp_path / "manifest_v3_policy.json"

    proc = _run_build_evidence_manifest_v3_cli(
        surface_inputs, manifest_output, head_sha=head_sha, run_id=77, run_attempt=1
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    manifest = json.loads(manifest_output.read_text(encoding="utf-8"))

    registry_doc = rvi.load_and_validate_registry(REGISTRY_PATH, SCHEMA_PATH, None, REPO_ROOT)
    resolve_result = rvi.ResolveResult(
        changed_paths=["src/ui/combatHud.ts"],
        affected_surfaces=[{"surface_id": "combat-hud-running", "reason": "direct_producer"}],
    )
    declaration_doc = {"surfaces": [{"surface_id": "combat-hud-running", "disposition": "verified_unchanged"}]}

    from datetime import date

    policy_result = rvi.evaluate_pr_policy(
        resolve_result=resolve_result,
        declaration_doc=declaration_doc,
        registry_doc=registry_doc,
        evidence_manifest=manifest,
        head_sha=head_sha,
        changed_paths=["src/ui/combatHud.ts"],
        actor="squne121",
        authorized_owners=set(),
        today=date(2026, 8, 29),
        trusted_check_run_id="1",
        trusted_check_conclusion="success",
    )
    assert policy_result["ok"] is True, policy_result["failures"]


def test_v3_producer_seam_consumed_end_to_end_by_verify_trusted_artifact(tmp_path: Path) -> None:
    """AC5 (full chain): real CLI producer -> `build_decision()` (the
    policy-check job's own decision-artifact builder) -> real
    `verify_trusted_artifact()` (the trusted-consumer's independent
    re-verification), all wired through a genuine V3 manifest -- never a
    synthetic decision/manifest pair standing in for the producer step."""
    head_sha = "2" * 40
    surface_inputs = tmp_path / "surface_inputs_v3_verify.json"
    surface_inputs.write_text(_surface_inputs_json(head_sha=head_sha, run_id=88, run_attempt=3), encoding="utf-8")
    manifest_output = tmp_path / "manifest_v3_verify.json"

    proc = _run_build_evidence_manifest_v3_cli(
        surface_inputs, manifest_output, head_sha=head_sha, run_id=88, run_attempt=3
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    manifest = json.loads(manifest_output.read_text(encoding="utf-8"))
    record = manifest["surfaces"][0]

    decision = rvi.build_decision(
        repository="squne121/loop-protocol",
        pull_request_number=2284,
        base_sha="0" * 40,
        head_sha=head_sha,
        base_registry_blob_sha="3" * 40,
        head_registry_blob_sha="4" * 40,
        pr_body="",
        changed_path_entries=[],
        affected_surfaces=[
            {
                "surface_id": "combat-hud-running",
                "contract_id": "combat-hud-running:vitest-browser-mode",
                "disposition": "verified_unchanged",
                "evidence": {
                    "baseline_unchanged": True,
                    "canonical_verify_success": True,
                    "evidence_manifest_digest": record["manifest_sha256"],
                },
            }
        ],
        component_vrt_report_check_run_id="555",
        github_actions_app_identity="github-actions[bot]",
        artifact_id="1",
        artifact_digest="sha256:" + "e" * 64,
        workflow_run_id=88,
        run_attempt=3,
    )

    provenance = rvi.ComponentVrtCheckrunProvenanceResult(
        ok=True,
        reason_codes=[],
        check_run_id=555,
        workflow_run_id=88,
        run_attempt=3,
        head_sha=head_sha,
        app_id=rvi.GITHUB_ACTIONS_APP_ID,
        app_slug=rvi.GITHUB_ACTIONS_APP_SLUG,
    )
    trusted_rederivation = rvi.TrustedRederivation(
        component_vrt_checkrun_provenance=provenance,
        require_component_vrt_checkrun_provenance=True,
        changed_paths_complete=True,
    )

    verdict = rvi.verify_trusted_artifact(
        decision_raw=json.dumps(decision).encode("utf-8"),
        evidence_manifest_raw=json.dumps(manifest).encode("utf-8"),
        visual_impact_schema_path=VISUAL_IMPACT_SCHEMA_PATH,
        expected_head_sha=head_sha,
        expected_repository="squne121/loop-protocol",
        expected_pr_number=2284,
        trusted_rederivation=trusted_rederivation,
    )
    assert verdict.ok is True, verdict.reason_codes


# ---------------------------------------------------------------------------
# AC6: no-impact (`surfaces: []`) identity.
# ---------------------------------------------------------------------------


def _no_impact_decision(*, head_sha: str, repository: str, pr_number: int, run_id: int, run_attempt: int) -> dict:
    return rvi.build_decision(
        repository=repository,
        pull_request_number=pr_number,
        base_sha="0" * 40,
        head_sha=head_sha,
        base_registry_blob_sha="1" * 40,
        head_registry_blob_sha="2" * 40,
        pr_body="",
        changed_path_entries=[],
        affected_surfaces=[],
        component_vrt_report_check_run_id=None,
        github_actions_app_identity="github-actions[bot]",
        artifact_id=None,
        artifact_digest=None,
        workflow_run_id=run_id,
        run_attempt=run_attempt,
    )


def test_v3_no_impact_manifest_passes_when_envelope_tuple_matches() -> None:
    """AC6: `surfaces: []` still PASSES when the V3 envelope's
    `(workflow_run_id, run_attempt, head_sha)` tuple matches what the
    trusted consumer independently expects/authenticated."""
    head_sha = "a" * 40
    decision = _no_impact_decision(
        head_sha=head_sha, repository="squne121/loop-protocol", pr_number=1, run_id=500, run_attempt=1
    )
    manifest = _v3_manifest(workflow_run_id=500, run_attempt=1, head_sha=head_sha, surfaces=[])

    verdict = rvi.verify_trusted_artifact(
        decision_raw=json.dumps(decision).encode("utf-8"),
        evidence_manifest_raw=json.dumps(manifest).encode("utf-8"),
        visual_impact_schema_path=VISUAL_IMPACT_SCHEMA_PATH,
        expected_head_sha=head_sha,
        expected_repository="squne121/loop-protocol",
        expected_pr_number=1,
    )
    assert verdict.ok is True, verdict.reason_codes


def test_v3_no_impact_manifest_fails_when_envelope_head_sha_mismatches() -> None:
    """AC6: `surfaces: []` FAILS when the envelope's `head_sha` does not
    match the trusted consumer's independently re-fetched candidate PR
    head -- never silently accepted just because there is nothing else to
    check."""
    head_sha = "a" * 40
    decision = _no_impact_decision(
        head_sha=head_sha, repository="squne121/loop-protocol", pr_number=1, run_id=500, run_attempt=1
    )
    manifest = _v3_manifest(workflow_run_id=500, run_attempt=1, head_sha="b" * 40, surfaces=[])

    verdict = rvi.verify_trusted_artifact(
        decision_raw=json.dumps(decision).encode("utf-8"),
        evidence_manifest_raw=json.dumps(manifest).encode("utf-8"),
        visual_impact_schema_path=VISUAL_IMPACT_SCHEMA_PATH,
        expected_head_sha=head_sha,
        expected_repository="squne121/loop-protocol",
        expected_pr_number=1,
    )
    assert verdict.ok is False
    assert any("evidence_manifest_v3_envelope_head_sha_mismatch" in r for r in verdict.reason_codes), (
        verdict.reason_codes
    )


def test_v3_no_impact_manifest_fails_when_envelope_run_attempt_mismatches_authenticated_provenance() -> None:
    """AC6: even when the envelope's OWN internal shape is well-formed and
    its `head_sha` matches, a `run_attempt` that does not match the
    trusted consumer's independently-authenticated CheckRun provenance
    (an old-attempt evidence manifest re-used for a new attempt) is
    rejected."""
    head_sha = "a" * 40
    decision = _no_impact_decision(
        head_sha=head_sha, repository="squne121/loop-protocol", pr_number=1, run_id=500, run_attempt=2
    )
    # Manifest claims run_attempt=1 (stale) while the trusted consumer's
    # authenticated CheckRun provenance says the real attempt is 2.
    manifest = _v3_manifest(workflow_run_id=500, run_attempt=1, head_sha=head_sha, surfaces=[])

    provenance = rvi.ComponentVrtCheckrunProvenanceResult(
        ok=True,
        reason_codes=[],
        workflow_run_id=500,
        run_attempt=2,
        head_sha=head_sha,
    )
    trusted_rederivation = rvi.TrustedRederivation(component_vrt_checkrun_provenance=provenance)

    verdict = rvi.verify_trusted_artifact(
        decision_raw=json.dumps(decision).encode("utf-8"),
        evidence_manifest_raw=json.dumps(manifest).encode("utf-8"),
        visual_impact_schema_path=VISUAL_IMPACT_SCHEMA_PATH,
        expected_head_sha=head_sha,
        expected_repository="squne121/loop-protocol",
        expected_pr_number=1,
        trusted_rederivation=trusted_rederivation,
    )
    assert verdict.ok is False
    assert any("evidence_manifest_v3_envelope_run_attempt_mismatch" in r for r in verdict.reason_codes), (
        verdict.reason_codes
    )


def test_v3_no_impact_manifest_fails_when_manifest_missing_entirely() -> None:
    """AC6: a completely missing evidence-manifest artifact still fails
    closed for a no-impact decision (pre-existing unconditional check,
    Issue #2230 fix_delta P1-5 -- exercised here specifically against a
    decision produced with V3-shaped `workflow_run_id`/`run_attempt`
    identity to confirm the V3 rollout does not weaken it)."""
    head_sha = "a" * 40
    decision = _no_impact_decision(
        head_sha=head_sha, repository="squne121/loop-protocol", pr_number=1, run_id=500, run_attempt=1
    )
    verdict = rvi.verify_trusted_artifact(
        decision_raw=json.dumps(decision).encode("utf-8"),
        evidence_manifest_raw=None,
        visual_impact_schema_path=VISUAL_IMPACT_SCHEMA_PATH,
        expected_head_sha=head_sha,
        expected_repository="squne121/loop-protocol",
        expected_pr_number=1,
    )
    assert verdict.ok is False
    assert any("evidence_manifest_missing" in r for r in verdict.reason_codes), verdict.reason_codes



def test_v3_no_impact_manifest_fails_when_decision_and_envelope_workflow_run_id_mismatch_no_provenance() -> None:
    """Issue #2379 OWNER fix_delta P2-2 (AC6): a no-impact decision's
    `workflow_run_id` and the V3 envelope's own `workflow_run_id` must be
    directly cross-checked EVEN WITHOUT any authenticated CheckRun
    provenance (`trusted_rederivation=None`) -- previously the only
    workflow_run_id comparison happened inside the `trusted_rederivation
    is not None and trusted_rederivation.component_vrt_checkrun_provenance`
    guard, so a no-provenance caller passing a decision/envelope pair with
    mismatched run identities was never caught by this direct comparison."""
    head_sha = "a" * 40
    decision = _no_impact_decision(
        head_sha=head_sha, repository="squne121/loop-protocol", pr_number=1, run_id=500, run_attempt=1
    )
    # Envelope claims workflow_run_id=999, disagreeing with the decision's
    # own claimed workflow_run_id=500 -- no provenance is supplied, so this
    # can ONLY be caught by the unconditional decision<->envelope check.
    manifest = _v3_manifest(workflow_run_id=999, run_attempt=1, head_sha=head_sha, surfaces=[])

    verdict = rvi.verify_trusted_artifact(
        decision_raw=json.dumps(decision).encode("utf-8"),
        evidence_manifest_raw=json.dumps(manifest).encode("utf-8"),
        visual_impact_schema_path=VISUAL_IMPACT_SCHEMA_PATH,
        expected_head_sha=head_sha,
        expected_repository="squne121/loop-protocol",
        expected_pr_number=1,
    )
    assert verdict.ok is False
    assert any(
        "evidence_manifest_v3_envelope_decision_workflow_run_id_mismatch" in r for r in verdict.reason_codes
    ), verdict.reason_codes


def test_v3_no_impact_manifest_fails_when_decision_and_envelope_run_attempt_mismatch_no_provenance() -> None:
    """Issue #2379 OWNER fix_delta P2-2 (AC6): same as the workflow_run_id
    variant above, but for `run_attempt` -- a no-impact decision's
    `run_attempt` and the V3 envelope's own `run_attempt` must be directly
    cross-checked even without any authenticated CheckRun provenance."""
    head_sha = "a" * 40
    decision = _no_impact_decision(
        head_sha=head_sha, repository="squne121/loop-protocol", pr_number=1, run_id=500, run_attempt=1
    )
    # Envelope claims run_attempt=7, disagreeing with the decision's own
    # claimed run_attempt=1 -- no provenance is supplied.
    manifest = _v3_manifest(workflow_run_id=500, run_attempt=7, head_sha=head_sha, surfaces=[])

    verdict = rvi.verify_trusted_artifact(
        decision_raw=json.dumps(decision).encode("utf-8"),
        evidence_manifest_raw=json.dumps(manifest).encode("utf-8"),
        visual_impact_schema_path=VISUAL_IMPACT_SCHEMA_PATH,
        expected_head_sha=head_sha,
        expected_repository="squne121/loop-protocol",
        expected_pr_number=1,
    )
    assert verdict.ok is False
    assert any(
        "evidence_manifest_v3_envelope_decision_run_attempt_mismatch" in r for r in verdict.reason_codes
    ), verdict.reason_codes
