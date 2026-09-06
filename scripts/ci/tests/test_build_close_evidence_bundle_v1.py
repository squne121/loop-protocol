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
# --------------------------------------------------------------------------- #
def test_manifest_sha256_mismatch_with_performance_receipt_fails_closed(builder, fixture_paths, tmp_path):
    manifest_data = json.loads(fixture_paths["manifest"].read_text(encoding="utf-8"))
    manifest_data["experiment_identity"] = "issue-2486-drifted-manifest-fixture"
    drifted_manifest_path = tmp_path / "drifted-manifest.json"
    _write_json(drifted_manifest_path, manifest_data)

    output_dir = tmp_path / "close-evidence"
    with pytest.raises(builder.CloseEvidenceBundleError, match="experiment_manifest_file_sha256"):
        _build(builder, fixture_paths, output_dir, manifest=drifted_manifest_path)
    assert not output_dir.exists()
