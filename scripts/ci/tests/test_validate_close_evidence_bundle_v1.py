"""Issue #2486 focused regression tests for
`scripts/ci/validate_close_evidence_bundle_v1.py` (the standalone
close-evidence bundle validator).

Every test builds a genuine bundle first via `build_close_evidence_bundle_v1.
build_close_evidence_bundle()` (real producer, real fixtures under
`scripts/ci/fixtures/close_evidence/`), then tampers with either the
`inputs/` copies or `close_evidence.json` itself post-generation to prove
the validator independently recomputes/re-derives every binding instead of
trusting `close_evidence.json`'s own claims (Issue #2486 In Scope /
AC4-AC7)."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_VALIDATOR_MODULE_PATH = _REPO_ROOT / "scripts" / "ci" / "validate_close_evidence_bundle_v1.py"
_PRODUCER_MODULE_PATH = _REPO_ROOT / "scripts" / "ci" / "build_close_evidence_bundle_v1.py"
_FIXTURES_DIR = _REPO_ROOT / "scripts" / "ci" / "fixtures" / "close_evidence"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def validator():
    return _load_module("validate_close_evidence_bundle_v1_under_test", _VALIDATOR_MODULE_PATH)


@pytest.fixture(scope="module")
def producer():
    return _load_module("build_close_evidence_bundle_v1_for_validator_tests", _PRODUCER_MODULE_PATH)


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _recompute_bundle_payload_digest(producer, close_evidence: dict) -> str:
    without_digest = copy.deepcopy(close_evidence)
    without_digest.pop("bundle_payload_digest", None)
    owner = producer._load_reliability_owner_module()
    return owner.sha256_of_canonical_json(without_digest)


def _rewrite_close_evidence(producer, bundle_dir: Path, mutate) -> dict:
    """Loads close_evidence.json, applies `mutate(dict) -> None` in place,
    correctly recomputes the self-excluding `bundle_payload_digest` for the
    mutated content (Issue #2486 AC7's exact adversarial model: the outer
    digest is recomputed correctly, but nothing else is fixed up), and
    writes it back."""
    path = bundle_dir / "close_evidence.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    data["bundle_payload_digest"] = _recompute_bundle_payload_digest(producer, data)
    _write_json(path, data)
    return data


@pytest.fixture()
def valid_bundle(producer, tmp_path):
    output_dir = tmp_path / "close-evidence"
    producer.build_close_evidence_bundle(
        performance_receipt_path=str(_FIXTURES_DIR / "performance_close_grade_result.fixture.json"),
        reliability_receipt_path=str(_FIXTURES_DIR / "reliability_close_grade_result.fixture.json"),
        manifest_path=str(_FIXTURES_DIR / "experiment_manifest.fixture.json"),
        output_dir=str(output_dir),
    )
    return output_dir


# --------------------------------------------------------------------------- #
# Sanity: the untouched, freshly-generated bundle validates clean.
# --------------------------------------------------------------------------- #
def test_freshly_generated_bundle_validates_clean(validator, valid_bundle):
    errors = validator.validate_bundle(str(valid_bundle))
    assert errors == []


# --------------------------------------------------------------------------- #
# AC4
# --------------------------------------------------------------------------- #
def test_run_set_binding_arm_swap(validator, valid_bundle):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    monolith = reliability_data["canonical_workflow_run_ids"]["monolith"]
    split = reliability_data["canonical_workflow_run_ids"]["split"]
    reliability_data["canonical_workflow_run_ids"]["monolith"] = split
    reliability_data["canonical_workflow_run_ids"]["split"] = monolith
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("run_set_binding_mismatch" in e for e in errors)
    # The raw-byte copy digest for the reliability receipt is now stale too
    # (the file content changed after the bundle was generated).
    assert any("reliability_close_grade_result_file_sha256_mismatch" in e for e in errors)


def test_run_set_binding_duplicate(validator, valid_bundle):
    performance_path = valid_bundle / "inputs" / "performance-close-grade-result.json"
    performance_data = json.loads(performance_path.read_text(encoding="utf-8"))
    performance_data["arms"]["monolith"]["workflow_run_ids"] = ["1001", "1001", "1002"]
    _write_json(performance_path, performance_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("duplicate_workflow_run_id" in e for e in errors)


def test_run_set_binding_missing(validator, valid_bundle):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    reliability_data["canonical_workflow_run_ids"]["monolith"] = [1001]  # 1002 missing
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("run_set_binding_mismatch" in e and "layout=monolith" in e for e in errors)


def test_run_set_binding_extra(validator, valid_bundle):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    reliability_data["canonical_workflow_run_ids"]["split"] = [2001, 2002, 9999]  # 9999 extra
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("run_set_binding_mismatch" in e and "layout=split" in e for e in errors)


def test_run_id_string_int_normalization_matches(validator, valid_bundle):
    """Performance stores run IDs as strings (`"1001"`); reliability stores
    them as integers (`1001`). Fixture already exercises this cross-owner
    representation split -- normalization must accept it, never a false
    positive."""
    performance_data = json.loads((valid_bundle / "inputs" / "performance-close-grade-result.json").read_text())
    reliability_data = json.loads((valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json").read_text())
    assert all(isinstance(x, str) for x in performance_data["arms"]["monolith"]["workflow_run_ids"])
    assert all(isinstance(x, int) for x in reliability_data["canonical_workflow_run_ids"]["monolith"])

    errors = validator.validate_bundle(str(valid_bundle))
    assert errors == []


def test_eligible_projection_not_used_as_root_set(validator, producer, tmp_path):
    """Mirrors the real upstream `missing_pair_e2e-responsive-matrix`
    regression fixed_delta shape (Remaining Parent Gaps): a layout whose
    `performance_eligible_workflow_run_ids` is a STRICT SUBSET of (or even
    empty relative to) `workflow_run_ids` must still bind successfully on
    the full root set -- `performance_eligible_workflow_run_ids` is a
    metric-specific projection, never a substitute root set."""
    performance_data = json.loads(
        (_FIXTURES_DIR / "performance_close_grade_result.fixture.json").read_text(encoding="utf-8")
    )
    # Root set unchanged; eligible projection deliberately emptied for one
    # arm (as the real monolith missing_pair_e2e-responsive-matrix case
    # does) -- this must NOT affect run-set binding at all.
    performance_data["arms"]["monolith"]["performance_eligible_workflow_run_ids"] = []

    performance_path = tmp_path / "performance-eligible-empty.json"
    _write_json(performance_path, performance_data)

    output_dir = tmp_path / "close-evidence"
    close_evidence = producer.build_close_evidence_bundle(
        performance_receipt_path=str(performance_path),
        reliability_receipt_path=str(_FIXTURES_DIR / "reliability_close_grade_result.fixture.json"),
        manifest_path=str(_FIXTURES_DIR / "experiment_manifest.fixture.json"),
        output_dir=str(output_dir),
    )
    assert close_evidence["workflow_run_ids"]["monolith"] == [1001, 1002]

    errors = validator.validate_bundle(str(output_dir))
    assert errors == []


# --------------------------------------------------------------------------- #
# AC5
# --------------------------------------------------------------------------- #
def test_tested_workflow_sha_binding_mismatch_fails_closed(validator, producer, valid_bundle):
    _rewrite_close_evidence(producer, valid_bundle, lambda d: d.__setitem__("tested_workflow_sha", "0" * 40))

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("tested_workflow_sha_binding_mismatch" in e for e in errors)


# --------------------------------------------------------------------------- #
# AC6
# --------------------------------------------------------------------------- #
def test_standalone_validation_after_bundle_directory_moved(validator, valid_bundle, tmp_path):
    moved_dir = tmp_path / "moved-elsewhere" / "close-evidence"
    moved_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(valid_bundle), str(moved_dir))

    errors = validator.validate_bundle(str(moved_dir))
    assert errors == []


# --------------------------------------------------------------------------- #
# AC7 -- 4 independent tamper cases proving outer bundle_payload_digest
# recompute ALONE never passes validation: each test tampers exactly one of
# (a) run/arm binding, (b) manifest content, (c) performance result content,
# (d) reliability result content, then correctly recomputes
# `bundle_payload_digest` for whatever it touched in close_evidence.json --
# and the validator must still catch it via an independent semantic binding
# check.
# --------------------------------------------------------------------------- #
def test_tampered_run_binding_survives_outer_digest_recompute_fails_closed(validator, producer, valid_bundle):
    def _swap_run_ids(d):
        d["workflow_run_ids"]["monolith"], d["workflow_run_ids"]["split"] = (
            d["workflow_run_ids"]["split"],
            d["workflow_run_ids"]["monolith"],
        )

    tampered = _rewrite_close_evidence(producer, valid_bundle, _swap_run_ids)
    # Sanity: outer digest recompute really does match (an attacker who
    # only recomputes the outer digest is NOT caught by that check alone).
    assert tampered["bundle_payload_digest"] == _recompute_bundle_payload_digest(producer, tampered)

    errors = validator.validate_bundle(str(valid_bundle))
    assert not any("bundle_payload_digest_mismatch" in e for e in errors)
    assert any("close_evidence_workflow_run_ids_mismatch" in e for e in errors)


def test_tampered_manifest_survives_outer_digest_recompute_fails_closed(validator, producer, valid_bundle):
    manifest_path = valid_bundle / "inputs" / "experiment-manifest.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_data["workflow_sha"] = "f" * 40
    _write_json(manifest_path, manifest_data)

    # Only the outer digest is "fixed" -- experiment_manifest_file_sha256
    # and tested_workflow_sha in close_evidence.json are deliberately left
    # stale, mirroring an attacker who forgets to also refresh those.
    tampered = _rewrite_close_evidence(producer, valid_bundle, lambda d: None)
    assert tampered["bundle_payload_digest"] == _recompute_bundle_payload_digest(producer, tampered)

    errors = validator.validate_bundle(str(valid_bundle))
    assert not any("bundle_payload_digest_mismatch" in e for e in errors)
    assert any("experiment_manifest_file_sha256_mismatch" in e for e in errors)
    assert any("tested_workflow_sha_binding_mismatch" in e for e in errors)


def test_tampered_performance_result_survives_outer_digest_recompute_fails_closed(validator, producer, valid_bundle):
    performance_path = valid_bundle / "inputs" / "performance-close-grade-result.json"
    performance_data = json.loads(performance_path.read_text(encoding="utf-8"))
    performance_data["exit_code"] = 1
    _write_json(performance_path, performance_data)

    tampered = _rewrite_close_evidence(producer, valid_bundle, lambda d: None)
    assert tampered["bundle_payload_digest"] == _recompute_bundle_payload_digest(producer, tampered)

    errors = validator.validate_bundle(str(valid_bundle))
    assert not any("bundle_payload_digest_mismatch" in e for e in errors)
    assert any("performance_close_grade_result_file_sha256_mismatch" in e for e in errors)
    assert any("performance_close_grade_ineligible" in e for e in errors)


def test_tampered_reliability_result_survives_outer_digest_recompute_fails_closed(validator, producer, valid_bundle):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    reliability_data["aggregate"]["all_non_inferior"] = False
    _write_json(reliability_path, reliability_data)

    tampered = _rewrite_close_evidence(producer, valid_bundle, lambda d: None)
    assert tampered["bundle_payload_digest"] == _recompute_bundle_payload_digest(producer, tampered)

    errors = validator.validate_bundle(str(valid_bundle))
    assert not any("bundle_payload_digest_mismatch" in e for e in errors)
    assert any("reliability_close_grade_result_file_sha256_mismatch" in e for e in errors)
    assert any("reliability_close_grade_ineligible" in e for e in errors)


# --------------------------------------------------------------------------- #
# PR #2528 review fix_delta (P1-1): cross-binding between the three
# inputs/ copies -- individually eligible receipts alone do not prove they
# describe the SAME experiment/manifest. Each case tampers ONLY the
# reliability receipt's inputs/ copy (never close_evidence.json) and, where
# needed, correctly recomputes reliability's OWN self-excluding
# `canonical_output_digest` so the specific cross-binding check under test
# is isolated from the receipt's own self-consistency check.
# --------------------------------------------------------------------------- #
def _recompute_reliability_self_digest(producer, reliability_data: dict) -> None:
    owner = producer._load_reliability_owner_module()
    without_digest = {k: v for k, v in reliability_data.items() if k != "canonical_output_digest"}
    reliability_data["canonical_output_digest"] = owner.sha256_of_canonical_json(without_digest)


def test_cross_binding_experiment_identity_mismatch_fails_closed(validator, producer, valid_bundle):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    reliability_data["experiment_identity"] = "issue-2486-a-different-experiment-run"
    _recompute_reliability_self_digest(producer, reliability_data)
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("experiment_identity_cross_binding_mismatch" in e for e in errors)


def test_cross_binding_reliability_manifest_digest_mismatch_fails_closed(validator, producer, valid_bundle):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    reliability_data["manifest_digest"] = "sha256:" + "1" * 64
    _recompute_reliability_self_digest(producer, reliability_data)
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("reliability_manifest_digest_mismatch" in e for e in errors)


def test_cross_binding_reliability_canonical_output_digest_invalid_fails_closed(validator, valid_bundle):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    # Deliberately NOT recomputed -- the receipt's own content is untouched,
    # but its self-excluding digest claim itself is wrong.
    reliability_data["canonical_output_digest"] = "sha256:" + "2" * 64
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("reliability_canonical_output_digest_self_inconsistent" in e for e in errors)


def test_cross_binding_performance_reliability_run_set_digest_mismatch_fails_closed(validator, producer, valid_bundle):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    reliability_data["receipt_run_set_digest"] = "sha256:" + "3" * 64
    _recompute_reliability_self_digest(producer, reliability_data)
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("performance_reliability_run_set_digest_mismatch" in e for e in errors)


# --------------------------------------------------------------------------- #
# PR #2528 review fix_delta (P1-2): close_evidence.json's OWN declared
# values -- independently re-derived from inputs/ and compared. Each case
# tampers exactly ONE declared field, leaves inputs/ untouched, and
# correctly recomputes the outer `bundle_payload_digest` (Issue #2486 AC7's
# exact adversarial model: passing the outer self-consistency check alone
# must never be sufficient).
# --------------------------------------------------------------------------- #
_DECLARED_VALUE_TAMPER_CASES = [
    ("experiment_identity", lambda d: d.__setitem__("experiment_identity", "issue-2486-tampered-identity")),
    (
        "experiment_manifest_canonical_digest",
        lambda d: d.__setitem__("experiment_manifest_canonical_digest", "sha256:" + "4" * 64),
    ),
    (
        "reliability_canonical_output_digest",
        lambda d: d.__setitem__("reliability_canonical_output_digest", "sha256:" + "5" * 64),
    ),
    ("performance_run_set_digest", lambda d: d.__setitem__("performance_run_set_digest", "sha256:" + "6" * 64)),
    (
        "reliability_receipt_run_set_digest",
        lambda d: d.__setitem__("reliability_receipt_run_set_digest", "sha256:" + "7" * 64),
    ),
    ("schema", lambda d: d.__setitem__("schema", "CI_CLOSE_EVIDENCE_BUNDLE_V2_UNKNOWN")),
    ("schema_version", lambda d: d.__setitem__("schema_version", 2)),
]


@pytest.mark.parametrize(
    "field,mutate",
    [(c[0], c[1]) for c in _DECLARED_VALUE_TAMPER_CASES],
    ids=[c[0] for c in _DECLARED_VALUE_TAMPER_CASES],
)
def test_declared_value_tamper_survives_outer_digest_recompute_fails_closed(
    validator, producer, valid_bundle, field, mutate
):
    tampered = _rewrite_close_evidence(producer, valid_bundle, mutate)
    assert tampered["bundle_payload_digest"] == _recompute_bundle_payload_digest(producer, tampered)

    errors = validator.validate_bundle(str(valid_bundle))
    assert not any("bundle_payload_digest_mismatch" in e for e in errors)
    assert any(f"declared_value_mismatch: field={field}" in e for e in errors)


def test_declared_experiment_identity_deleted_fails_closed(validator, producer, valid_bundle):
    def _delete_identity(d):
        del d["experiment_identity"]

    tampered = _rewrite_close_evidence(producer, valid_bundle, _delete_identity)
    assert tampered["bundle_payload_digest"] == _recompute_bundle_payload_digest(producer, tampered)

    errors = validator.validate_bundle(str(valid_bundle))
    assert not any("bundle_payload_digest_mismatch" in e for e in errors)
    assert any("declared_value_mismatch: field=experiment_identity" in e for e in errors)


def test_declared_bundle_side_duplicate_run_id_fails_closed(validator, producer, valid_bundle):
    """Exact reproduction from the PR #2528 review comment: bundle's OWN
    declared `workflow_run_ids` gains a duplicate that naive
    set-normalization-before-dedup-check would silently absorb."""

    def _duplicate_monolith_run_id(d):
        d["workflow_run_ids"]["monolith"] = d["workflow_run_ids"]["monolith"] + [
            d["workflow_run_ids"]["monolith"][0]
        ]

    tampered = _rewrite_close_evidence(producer, valid_bundle, _duplicate_monolith_run_id)
    assert tampered["bundle_payload_digest"] == _recompute_bundle_payload_digest(producer, tampered)

    errors = validator.validate_bundle(str(valid_bundle))
    assert not any("bundle_payload_digest_mismatch" in e for e in errors)
    assert any("duplicate_workflow_run_id" in e and "source=close_evidence" in e for e in errors)


def test_declared_workflow_run_ids_missing_required_layout_fails_closed(validator, producer, valid_bundle):
    def _delete_split_layout(d):
        del d["workflow_run_ids"]["split"]

    tampered = _rewrite_close_evidence(producer, valid_bundle, _delete_split_layout)
    assert tampered["bundle_payload_digest"] == _recompute_bundle_payload_digest(producer, tampered)

    errors = validator.validate_bundle(str(valid_bundle))
    assert not any("bundle_payload_digest_mismatch" in e for e in errors)
    assert any("close_evidence.workflow_run_ids missing required layout: split" in e for e in errors)


def test_declared_workflow_run_ids_field_missing_entirely_fails_closed(validator, producer, valid_bundle):
    def _delete_workflow_run_ids(d):
        del d["workflow_run_ids"]

    tampered = _rewrite_close_evidence(producer, valid_bundle, _delete_workflow_run_ids)
    assert tampered["bundle_payload_digest"] == _recompute_bundle_payload_digest(producer, tampered)

    errors = validator.validate_bundle(str(valid_bundle))
    assert not any("bundle_payload_digest_mismatch" in e for e in errors)
    assert any("close_evidence.workflow_run_ids is missing or not an object" in e for e in errors)


# --------------------------------------------------------------------------- #
# PR #2528 review fix_delta (P1-3): reliability receipt structural
# invariants, re-verified by the validator directly from inputs/ (never
# just "validator_results has the 3 metric keys").
# --------------------------------------------------------------------------- #
def test_reliability_assessment_content_digests_missing_metric_fails_closed(validator, valid_bundle):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    del reliability_data["assessment_content_digests"]["playwright_flaky_test_rate"]
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("assessment_content_digests missing" in e for e in errors)


def test_reliability_assessment_content_digests_extra_metric_fails_closed(validator, valid_bundle):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    reliability_data["assessment_content_digests"]["extra_metric_not_in_contract"] = "sha256:" + "0" * 64
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("assessment_content_digests has unexpected" in e for e in errors)


def test_reliability_assessment_content_digest_malformed_fails_closed(validator, valid_bundle):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    reliability_data["assessment_content_digests"]["workflow_failure_rate"] = "not-a-digest"
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("not a well-formed sha256 digest" in e for e in errors)


def test_reliability_validator_results_missing_metric_fails_closed(validator, valid_bundle):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    del reliability_data["validator_results"]["playwright_terminal_failure_rate"]
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("validator_results missing" in e for e in errors)


def test_reliability_validator_result_null_fails_closed(validator, valid_bundle):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    reliability_data["validator_results"]["workflow_failure_rate"] = None
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("is not an object" in e for e in errors)


def test_reliability_individual_validator_exit_code_failure_with_aggregate_success_fails_closed(validator, valid_bundle):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    reliability_data["validator_results"]["workflow_failure_rate"]["exit_code"] = 3
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("exit_code is not 0" in e for e in errors)


def test_reliability_individual_validator_structural_valid_false_with_aggregate_success_fails_closed(
    validator, valid_bundle
):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    reliability_data["validator_results"]["playwright_flaky_test_rate"]["structural_valid"] = False
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("structural_valid is not true" in e for e in errors)


def test_reliability_individual_validator_semantic_valid_false_with_aggregate_success_fails_closed(
    validator, valid_bundle
):
    reliability_path = valid_bundle / "inputs" / "ci_reliability_close_grade_result_v1.json"
    reliability_data = json.loads(reliability_path.read_text(encoding="utf-8"))
    reliability_data["validator_results"]["playwright_terminal_failure_rate"]["semantic_valid"] = False
    _write_json(reliability_path, reliability_data)

    errors = validator.validate_bundle(str(valid_bundle))
    assert any("semantic_valid is not true" in e for e in errors)


# --------------------------------------------------------------------------- #
# Offline integration/compatibility test (PR #2528 review): feeds a
# reliability receipt produced by #2424's REAL, public
# `build_canonical_output()` (never a hand-rolled fixture) through this
# Issue's producer -> validator pipeline, to catch drift between
# hand-written fixtures and the real upstream output shape. This is a
# compatibility check, distinct from production close judgment -- offline
# only, no real GitHub Actions dispatch or 44-run experiment.
# --------------------------------------------------------------------------- #
def test_offline_integration_with_real_reliability_owner_canonical_output_builder(validator, producer, tmp_path):
    reliability_owner = producer._load_reliability_owner_module()

    manifest_path = tmp_path / "experiment-manifest.json"
    performance_path = tmp_path / "performance-close-grade-result.json"
    reliability_path = tmp_path / "ci_reliability_close_grade_result_v1.json"
    shutil.copyfile(_FIXTURES_DIR / "experiment_manifest.fixture.json", manifest_path)
    shutil.copyfile(_FIXTURES_DIR / "performance_close_grade_result.fixture.json", performance_path)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    performance_receipt = json.loads(performance_path.read_text(encoding="utf-8"))

    assessments = {
        metric: {"schema": "CI_TEST_RELIABILITY_ASSESSMENT_V1", "metric": metric, "value": 0.0}
        for metric in reliability_owner.METRICS
    }
    validator_results = {
        metric: {
            "schema": "CI_TEST_RELIABILITY_ASSESSMENT_V1_VALIDATION_RESULT",
            "exit_code": 0,
            "structural_valid": True,
            "semantic_valid": True,
        }
        for metric in reliability_owner.METRICS
    }
    aggregate = {
        "complete": True,
        "semantic_valid": True,
        "sample_satisfied": True,
        "all_non_inferior": True,
        "exit_code": 0,
    }
    # build_canonical_output() only reads `run_set_digest` off its `receipt`
    # parameter (see #2424 module docstring) -- a minimal stub is correct,
    # never a second hand-rolled full receipt.
    receipt_stub = {"run_set_digest": performance_receipt["run_set_digest"]}

    reliability_receipt = reliability_owner.build_canonical_output(
        manifest,
        receipt_stub,
        None,
        {},
        assessments,
        validator_results,
        aggregate,
    )
    _write_json(reliability_path, reliability_receipt)

    output_dir = tmp_path / "close-evidence"
    close_evidence = producer.build_close_evidence_bundle(
        performance_receipt_path=str(performance_path),
        reliability_receipt_path=str(reliability_path),
        manifest_path=str(manifest_path),
        output_dir=str(output_dir),
    )
    assert close_evidence["reliability_canonical_output_digest"] == reliability_receipt["canonical_output_digest"]

    errors = validator.validate_bundle(str(output_dir))
    assert errors == []
