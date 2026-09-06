"""Issue #2486 focused regression tests for
`scripts/ci/build_close_evidence_bundle_v1.py` (the close-evidence bundle
producer).

Every fixture under `scripts/ci/fixtures/close_evidence/` mirrors the REAL
`CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1` (#2423, `tests/ci/
test_ci_performance_gate.py`'s `build_close_grade_receipt()`) and
`CI_RELIABILITY_CLOSE_GRADE_RESULT_V1` (#2424, `scripts/ci/
build_ci_reliability_assessment_v1.py`'s `build_canonical_output()`) output
shapes -- field names, nesting, and run-ID representation (performance:
string, reliability: integer) were read directly from those owner modules,
never guessed (a prior review round on this Issue caught guessed-fixture
schema mismatches). This suite never imports or re-executes the real
#2423/#2424 producers themselves, and never mutates their logic -- only
`scripts/ci/build_close_evidence_bundle_v1.py` is under test here.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE_PATH = _REPO_ROOT / "scripts" / "ci" / "build_close_evidence_bundle_v1.py"
_FIXTURES_DIR = _REPO_ROOT / "scripts" / "ci" / "fixtures" / "close_evidence"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_close_evidence_bundle_v1_under_test", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def builder():
    return _load_module()


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture()
def fixture_paths(tmp_path):
    """Byte-identical copies of the 3 real-shape fixtures in a fresh
    tmp_path -- copied via `shutil.copyfile` (never re-serialized) so the
    performance receipt's own `manifest_sha256` claim keeps matching the
    manifest file's actual raw bytes."""
    manifest_path = tmp_path / "experiment-manifest.json"
    performance_path = tmp_path / "performance-close-grade-result.json"
    reliability_path = tmp_path / "ci_reliability_close_grade_result_v1.json"
    shutil.copyfile(_FIXTURES_DIR / "experiment_manifest.fixture.json", manifest_path)
    shutil.copyfile(_FIXTURES_DIR / "performance_close_grade_result.fixture.json", performance_path)
    shutil.copyfile(_FIXTURES_DIR / "reliability_close_grade_result.fixture.json", reliability_path)
    return {
        "manifest": manifest_path,
        "performance": performance_path,
        "reliability": reliability_path,
    }


def _build(builder, fixture_paths, output_dir, *, performance=None, reliability=None, manifest=None):
    return builder.build_close_evidence_bundle(
        performance_receipt_path=str(performance or fixture_paths["performance"]),
        reliability_receipt_path=str(reliability or fixture_paths["reliability"]),
        manifest_path=str(manifest or fixture_paths["manifest"]),
        output_dir=str(output_dir),
    )


# --------------------------------------------------------------------------- #
# AC1
# --------------------------------------------------------------------------- #
def test_bundle_generated_when_both_close_grade_eligible(builder, fixture_paths, tmp_path):
    output_dir = tmp_path / "close-evidence"
    close_evidence = _build(builder, fixture_paths, output_dir)

    assert (output_dir / "close_evidence.json").exists()
    assert (output_dir / "inputs" / "experiment-manifest.json").read_bytes() == fixture_paths["manifest"].read_bytes()
    assert (output_dir / "inputs" / "performance-close-grade-result.json").read_bytes() == fixture_paths[
        "performance"
    ].read_bytes()
    assert (output_dir / "inputs" / "ci_reliability_close_grade_result_v1.json").read_bytes() == fixture_paths[
        "reliability"
    ].read_bytes()

    assert close_evidence["schema"] == "CI_CLOSE_EVIDENCE_BUNDLE_V1"
    assert close_evidence["workflow_run_ids"] == {"monolith": [1001, 1002], "split": [2001, 2002]}
    assert close_evidence["tested_workflow_sha"] == json.loads(fixture_paths["manifest"].read_text())["workflow_sha"]
    assert set(close_evidence.keys()).isdisjoint(builder.GITHUB_UPLOAD_ONLY_KEYS)

    on_disk = json.loads((output_dir / "close_evidence.json").read_text(encoding="utf-8"))
    assert on_disk == close_evidence


# --------------------------------------------------------------------------- #
# AC2
# --------------------------------------------------------------------------- #
_RELIABILITY_INELIGIBLE_CASES = [
    ("aggregate_complete_false", ("aggregate", "complete"), False),
    ("aggregate_semantic_valid_false", ("aggregate", "semantic_valid"), False),
    ("aggregate_sample_satisfied_false", ("aggregate", "sample_satisfied"), False),
    ("aggregate_all_non_inferior_false", ("aggregate", "all_non_inferior"), False),
    ("aggregate_exit_code_nonzero", ("aggregate", "exit_code"), 1),
]


@pytest.mark.parametrize(
    "field_path,value",
    [(c[1], c[2]) for c in _RELIABILITY_INELIGIBLE_CASES],
    ids=[c[0] for c in _RELIABILITY_INELIGIBLE_CASES],
)
def test_reliability_ineligible_condition_fails_closed(builder, fixture_paths, tmp_path, field_path, value):
    reliability_data = json.loads(fixture_paths["reliability"].read_text(encoding="utf-8"))
    node = reliability_data
    for key in field_path[:-1]:
        node = node[key]
    node[field_path[-1]] = value
    tampered_path = tmp_path / "tampered-reliability.json"
    _write_json(tampered_path, reliability_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="reliability_close_grade_ineligible"):
        _build(builder, fixture_paths, output_dir, reliability=tampered_path)
    assert not output_dir.exists()


def test_reliability_ineligible_condition_fails_closed_missing_metric(builder, fixture_paths, tmp_path):
    reliability_data = json.loads(fixture_paths["reliability"].read_text(encoding="utf-8"))
    del reliability_data["validator_results"]["workflow_failure_rate"]
    tampered_path = tmp_path / "tampered-reliability.json"
    _write_json(tampered_path, reliability_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="reliability_close_grade_ineligible"):
        _build(builder, fixture_paths, output_dir, reliability=tampered_path)
    assert not output_dir.exists()


def test_reliability_ineligible_condition_fails_closed_extra_metric(builder, fixture_paths, tmp_path):
    reliability_data = json.loads(fixture_paths["reliability"].read_text(encoding="utf-8"))
    reliability_data["validator_results"]["extra_metric_not_in_contract"] = {
        "schema": "CI_TEST_RELIABILITY_ASSESSMENT_V1_VALIDATION_RESULT",
        "exit_code": 0,
        "structural_valid": True,
        "semantic_valid": True,
    }
    tampered_path = tmp_path / "tampered-reliability.json"
    _write_json(tampered_path, reliability_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="reliability_close_grade_ineligible"):
        _build(builder, fixture_paths, output_dir, reliability=tampered_path)
    assert not output_dir.exists()


# --------------------------------------------------------------------------- #
# AC3
# --------------------------------------------------------------------------- #
_PERFORMANCE_INELIGIBLE_CASES = [
    ("performance_assessment_complete_false", ("performance_assessment", "complete"), False),
    ("validation_semantic_valid_false", ("validation", "semantic_valid"), False),
    ("validation_approval_eligible_false", ("validation", "approval_eligible"), False),
    ("exit_code_nonzero", ("exit_code",), 1),
]


@pytest.mark.parametrize(
    "field_path,value",
    [(c[1], c[2]) for c in _PERFORMANCE_INELIGIBLE_CASES],
    ids=[c[0] for c in _PERFORMANCE_INELIGIBLE_CASES],
)
def test_performance_ineligible_condition_fails_closed(builder, fixture_paths, tmp_path, field_path, value):
    performance_data = json.loads(fixture_paths["performance"].read_text(encoding="utf-8"))
    node = performance_data
    for key in field_path[:-1]:
        node = node[key]
    node[field_path[-1]] = value
    tampered_path = tmp_path / "tampered-performance.json"
    _write_json(tampered_path, performance_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="performance_close_grade_ineligible"):
        _build(builder, fixture_paths, output_dir, performance=tampered_path)
    assert not output_dir.exists()


# --------------------------------------------------------------------------- #
# AC8
# --------------------------------------------------------------------------- #
def test_close_evidence_json_excludes_github_upload_only_keys(builder, fixture_paths, tmp_path):
    output_dir = tmp_path / "close-evidence"
    close_evidence = _build(builder, fixture_paths, output_dir)

    assert set(close_evidence.keys()).isdisjoint(builder.GITHUB_UPLOAD_ONLY_KEYS)

    publication_receipt = builder.build_publication_receipt(
        close_evidence,
        github_artifact_id="1234567890",
        github_artifact_digest="sha256:" + "a" * 64,
        artifact_url="https://github.com/squne121/loop-protocol/actions/runs/1/artifacts/1",
    )
    assert publication_receipt["schema"] == "CI_CLOSE_EVIDENCE_PUBLICATION_RECEIPT_V1"
    for key in builder.GITHUB_UPLOAD_ONLY_KEYS:
        assert key in publication_receipt
    assert publication_receipt["bundle_payload_digest"] == close_evidence["bundle_payload_digest"]


# --------------------------------------------------------------------------- #
# Bonus coverage (not a numbered AC, but directly exercises the digest
# semantics table in the Issue's In Scope section): a manifest whose raw
# bytes don't match the performance receipt's own `manifest_sha256` claim
# must fail closed, never silently bind the wrong manifest.
#
# PR #2528 review fix_delta: this now tampers the performance receipt's OWN
# `manifest_sha256` claim (instead of the manifest's `experiment_identity`)
# so this test isolates the raw-byte `manifest_sha256` check from the newer
# `verify_input_cross_binding()` identity/manifest-digest checks (which now
# run earlier and have their own dedicated tests below) -- previously,
# perturbing `experiment_identity` also incidentally broke the reliability
# receipt's `manifest_digest` binding, which is a DIFFERENT check.
# --------------------------------------------------------------------------- #
def test_manifest_sha256_mismatch_with_performance_receipt_fails_closed(builder, fixture_paths, tmp_path):
    performance_data = json.loads(fixture_paths["performance"].read_text(encoding="utf-8"))
    performance_data["manifest_sha256"] = "sha256:" + "0" * 64
    tampered_performance_path = tmp_path / "tampered-performance-manifest-sha.json"
    _write_json(tampered_performance_path, performance_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="experiment_manifest_file_sha256"):
        _build(builder, fixture_paths, output_dir, performance=tampered_performance_path)
    assert not output_dir.exists()


# --------------------------------------------------------------------------- #
# PR #2528 review fix_delta (P1-1): `verify_input_cross_binding()` /
# `input_cross_binding_invalid` -- individually close-grade-eligible
# receipts that describe DIFFERENT experiments/manifests must still fail
# closed. Each case updates the reliability receipt's OWN self-excluding
# `canonical_output_digest` correctly for whatever it touched, EXCEPT the
# dedicated "invalid canonical_output_digest" case, which deliberately
# leaves it stale to prove that specific self-consistency check fires on
# its own.
# --------------------------------------------------------------------------- #
def _recompute_reliability_canonical_output_digest(builder, reliability_data: dict) -> None:
    owner = builder._load_reliability_owner_module()
    without_digest = {k: v for k, v in reliability_data.items() if k != "canonical_output_digest"}
    reliability_data["canonical_output_digest"] = owner.sha256_of_canonical_json(without_digest)


def test_cross_binding_experiment_identity_mismatch_fails_closed(builder, fixture_paths, tmp_path):
    reliability_data = json.loads(fixture_paths["reliability"].read_text(encoding="utf-8"))
    reliability_data["experiment_identity"] = "issue-2486-a-different-experiment-run"
    _recompute_reliability_canonical_output_digest(builder, reliability_data)
    tampered_path = tmp_path / "tampered-reliability-identity.json"
    _write_json(tampered_path, reliability_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="experiment_identity_cross_binding_mismatch"):
        _build(builder, fixture_paths, output_dir, reliability=tampered_path)
    assert not output_dir.exists()


def test_cross_binding_reliability_manifest_digest_mismatch_fails_closed(builder, fixture_paths, tmp_path):
    reliability_data = json.loads(fixture_paths["reliability"].read_text(encoding="utf-8"))
    reliability_data["manifest_digest"] = "sha256:" + "1" * 64
    _recompute_reliability_canonical_output_digest(builder, reliability_data)
    tampered_path = tmp_path / "tampered-reliability-manifest-digest.json"
    _write_json(tampered_path, reliability_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="reliability_manifest_digest_mismatch"):
        _build(builder, fixture_paths, output_dir, reliability=tampered_path)
    assert not output_dir.exists()


def test_cross_binding_reliability_canonical_output_digest_invalid_fails_closed(builder, fixture_paths, tmp_path):
    reliability_data = json.loads(fixture_paths["reliability"].read_text(encoding="utf-8"))
    # Deliberately NOT recomputed -- the receipt's own content is untouched,
    # but its self-excluding digest claim itself is wrong.
    reliability_data["canonical_output_digest"] = "sha256:" + "2" * 64
    tampered_path = tmp_path / "tampered-reliability-self-digest.json"
    _write_json(tampered_path, reliability_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="reliability_canonical_output_digest_self_inconsistent"):
        _build(builder, fixture_paths, output_dir, reliability=tampered_path)
    assert not output_dir.exists()


def test_cross_binding_performance_reliability_run_set_digest_mismatch_fails_closed(builder, fixture_paths, tmp_path):
    reliability_data = json.loads(fixture_paths["reliability"].read_text(encoding="utf-8"))
    reliability_data["receipt_run_set_digest"] = "sha256:" + "3" * 64
    _recompute_reliability_canonical_output_digest(builder, reliability_data)
    tampered_path = tmp_path / "tampered-reliability-run-set-digest.json"
    _write_json(tampered_path, reliability_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="performance_reliability_run_set_digest_mismatch"):
        _build(builder, fixture_paths, output_dir, reliability=tampered_path)
    assert not output_dir.exists()


# --------------------------------------------------------------------------- #
# PR #2528 review fix_delta (P1-3): reliability receipt structural
# invariants beyond "validator_results has the 3 metric keys" -- extends
# the existing AC2 ineligibility coverage.
# --------------------------------------------------------------------------- #
def test_reliability_ineligible_assessment_content_digests_missing_metric(builder, fixture_paths, tmp_path):
    reliability_data = json.loads(fixture_paths["reliability"].read_text(encoding="utf-8"))
    del reliability_data["assessment_content_digests"]["workflow_failure_rate"]
    tampered_path = tmp_path / "tampered-reliability-digest-missing.json"
    _write_json(tampered_path, reliability_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="assessment_content_digests missing"):
        _build(builder, fixture_paths, output_dir, reliability=tampered_path)
    assert not output_dir.exists()


def test_reliability_ineligible_assessment_content_digests_extra_metric(builder, fixture_paths, tmp_path):
    reliability_data = json.loads(fixture_paths["reliability"].read_text(encoding="utf-8"))
    reliability_data["assessment_content_digests"]["extra_metric_not_in_contract"] = "sha256:" + "0" * 64
    tampered_path = tmp_path / "tampered-reliability-digest-extra.json"
    _write_json(tampered_path, reliability_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="assessment_content_digests has unexpected"):
        _build(builder, fixture_paths, output_dir, reliability=tampered_path)
    assert not output_dir.exists()


def test_reliability_ineligible_assessment_content_digests_malformed(builder, fixture_paths, tmp_path):
    reliability_data = json.loads(fixture_paths["reliability"].read_text(encoding="utf-8"))
    reliability_data["assessment_content_digests"]["workflow_failure_rate"] = "not-a-digest"
    tampered_path = tmp_path / "tampered-reliability-digest-malformed.json"
    _write_json(tampered_path, reliability_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="not a well-formed sha256 digest"):
        _build(builder, fixture_paths, output_dir, reliability=tampered_path)
    assert not output_dir.exists()


def test_reliability_ineligible_validator_result_null(builder, fixture_paths, tmp_path):
    reliability_data = json.loads(fixture_paths["reliability"].read_text(encoding="utf-8"))
    reliability_data["validator_results"]["workflow_failure_rate"] = None
    tampered_path = tmp_path / "tampered-reliability-validator-null.json"
    _write_json(tampered_path, reliability_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="is not an object"):
        _build(builder, fixture_paths, output_dir, reliability=tampered_path)
    assert not output_dir.exists()


_RELIABILITY_INDIVIDUAL_VALIDATOR_CONTRADICTION_CASES = [
    ("exit_code_nonzero", "exit_code", 3, "exit_code is not 0"),
    ("structural_valid_false", "structural_valid", False, "structural_valid is not true"),
    ("semantic_valid_false", "semantic_valid", False, "semantic_valid is not true"),
]


@pytest.mark.parametrize(
    "field,value,match",
    [(c[1], c[2], c[3]) for c in _RELIABILITY_INDIVIDUAL_VALIDATOR_CONTRADICTION_CASES],
    ids=[c[0] for c in _RELIABILITY_INDIVIDUAL_VALIDATOR_CONTRADICTION_CASES],
)
def test_reliability_ineligible_individual_validator_contradicts_aggregate_success(
    builder, fixture_paths, tmp_path, field, value, match
):
    """Aggregate stays declared success (`aggregate.*` all True/0) while one
    individual `validator_results` entry contradicts that success -- must
    still fail closed (PR #2528 review P1-3)."""
    reliability_data = json.loads(fixture_paths["reliability"].read_text(encoding="utf-8"))
    reliability_data["validator_results"]["workflow_failure_rate"][field] = value
    tampered_path = tmp_path / f"tampered-reliability-validator-{field}.json"
    _write_json(tampered_path, reliability_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match=match):
        _build(builder, fixture_paths, output_dir, reliability=tampered_path)
    assert not output_dir.exists()


# --------------------------------------------------------------------------- #
# PR #2528 review fix_delta (P2): a required arm/layout entirely absent
# (never `or {}`/`or []`-coerced into a trivially-matching empty set).
# --------------------------------------------------------------------------- #
def test_run_set_binding_missing_required_arm_fails_closed(builder, fixture_paths, tmp_path):
    performance_data = json.loads(fixture_paths["performance"].read_text(encoding="utf-8"))
    del performance_data["arms"]["split"]
    tampered_path = tmp_path / "tampered-performance-missing-arm.json"
    _write_json(tampered_path, performance_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="missing required arm workflow_run_ids"):
        _build(builder, fixture_paths, output_dir, performance=tampered_path)
    assert not output_dir.exists()


def test_run_set_binding_both_receipts_empty_run_sets_fails_closed(builder, fixture_paths, tmp_path):
    """Both receipts' run-set objects emptied -- must NOT be accepted as
    'matching empty sets' (the exact regression this Issue's review found)."""
    performance_data = json.loads(fixture_paths["performance"].read_text(encoding="utf-8"))
    performance_data["arms"] = {}
    reliability_data = json.loads(fixture_paths["reliability"].read_text(encoding="utf-8"))
    reliability_data["canonical_workflow_run_ids"] = {}
    _recompute_reliability_canonical_output_digest(builder, reliability_data)

    tampered_performance_path = tmp_path / "tampered-performance-empty-arms.json"
    _write_json(tampered_performance_path, performance_data)
    tampered_reliability_path = tmp_path / "tampered-reliability-empty-run-ids.json"
    _write_json(tampered_reliability_path, reliability_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="missing required"):
        _build(
            builder,
            fixture_paths,
            output_dir,
            performance=tampered_performance_path,
            reliability=tampered_reliability_path,
        )
    assert not output_dir.exists()


# --------------------------------------------------------------------------- #
# PR #2528 review fix_delta (operational P5): fail-before-write -- a
# non-integer-parseable run ID must be caught BEFORE any inputs/ file is
# written, never after (no partial bundle directory left behind).
# --------------------------------------------------------------------------- #
def test_non_integer_workflow_run_id_fails_before_any_write(builder, fixture_paths, tmp_path):
    performance_data = json.loads(fixture_paths["performance"].read_text(encoding="utf-8"))
    performance_data["arms"]["monolith"]["workflow_run_ids"] = ["1001notanumber", "1002"]
    reliability_data = json.loads(fixture_paths["reliability"].read_text(encoding="utf-8"))
    # Keep run-set binding itself consistent (both sides normalize to the
    # same string set) so this test isolates the int() conversion failure,
    # not an earlier run_set_binding_mismatch.
    reliability_data["canonical_workflow_run_ids"]["monolith"] = ["1001notanumber", 1002]
    _recompute_reliability_canonical_output_digest(builder, reliability_data)

    tampered_performance_path = tmp_path / "tampered-performance-non-integer-run-id.json"
    _write_json(tampered_performance_path, performance_data)
    tampered_reliability_path = tmp_path / "tampered-reliability-non-integer-run-id.json"
    _write_json(tampered_reliability_path, reliability_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="workflow_run_id_not_integer"):
        _build(
            builder,
            fixture_paths,
            output_dir,
            performance=tampered_performance_path,
            reliability=tampered_reliability_path,
        )
    # The exact "never a partial bundle directory" guarantee this case
    # extends: not even inputs/ was written.
    assert not output_dir.exists()
