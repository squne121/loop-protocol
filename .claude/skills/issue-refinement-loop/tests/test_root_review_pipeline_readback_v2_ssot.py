"""Issue #2242 — regression coverage for the writer/reader V2 SSOT drift.

`readback_persisted_artifact()` (`run_root_review_pipeline.py`) used to parse
persisted JSON itself and read `payload.get("body_sha256")` /
`payload.get("verdict")` as TOP-LEVEL keys.  The canonical V2 writer
(`reviewer_transport.write_semantic_artifact()`) has never persisted those
keys: body binding lives at the top-level `reviewed_body_sha256` field and
`verdict` lives INSIDE the nested `semantic_result` dict.  Every fresh V2
artifact therefore always read back as `None` for both fields, and
`gate_final_review()` was permanently `body_sha256_mismatch` /
`verdict_mismatch` (#2231 issuecomment-5315427655 / issuecomment-5316915493).

This module proves the FULL producer -> readback -> gate roundtrip against
the REAL canonical writer (never a hand-written payload the test reshapes to
match), both in-process and via the actual CLI subcommands, and pins the
#2054/PR #2142 SSOT contract that `reviewer_transport.py` remains the sole
owner of this schema.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
SCRIPTS_DIR = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
PIPELINE_SCRIPT = SCRIPTS_DIR / "run_root_review_pipeline.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import reviewer_transport as transport  # noqa: E402


def _load_pipeline_module():
    spec = importlib.util.spec_from_file_location("run_root_review_pipeline_v2_ssot", PIPELINE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("run_root_review_pipeline_v2_ssot", module)
    spec.loader.exec_module(module)
    return module


_PIPELINE = _load_pipeline_module()

_REPO = "squne121/loop-protocol"
_ISSUE = 2242
_BODY_SHA256 = "sha256:" + "c" * 64


# ---------------------------------------------------------------------------
# AC1 / Required regression test: canonical producer -> persisted V2
# artifact -> readback_persisted_artifact() -> gate_final_review() reaches
# final_review_allowed: true for a fresh "approve" artifact, with NO
# test-side payload reshaping (the exact bytes write_semantic_artifact()
# wrote are the exact bytes readback_persisted_artifact() consumes).
# ---------------------------------------------------------------------------


def test_given_canonical_approve_artifact_when_full_roundtrip_then_final_review_allowed(tmp_path: Path):
    relative, _artifact_sha256 = transport.write_semantic_artifact(
        artifact_root=tmp_path,
        issue_number=_ISSUE,
        repo=_REPO,
        invocation_id="roundtrip-approve",
        attempt=1,
        reviewed_body_sha256=_BODY_SHA256,
        semantic_result={"verdict": "approve", "blocking_issues": []},
    )
    artifact_path = tmp_path / relative

    readback = _PIPELINE.readback_persisted_artifact(
        artifact_path, expected_body_sha256=_BODY_SHA256, expected_verdict="approve"
    )
    assert readback["violations"] == []
    assert readback["verdict_identity"] is True

    gate = _PIPELINE.gate_final_review(remote_update_ok=True, readback=readback)
    assert gate == {"final_review_allowed": True, "reasons": []}


# ---------------------------------------------------------------------------
# AC2: "needs-fix" artifact also correctly verifies expected verdict identity.
# ---------------------------------------------------------------------------


def test_given_canonical_needs_fix_artifact_when_full_roundtrip_then_final_review_allowed(tmp_path: Path):
    relative, _artifact_sha256 = transport.write_semantic_artifact(
        artifact_root=tmp_path,
        issue_number=_ISSUE,
        repo=_REPO,
        invocation_id="roundtrip-needs-fix",
        attempt=1,
        reviewed_body_sha256=_BODY_SHA256,
        semantic_result={"verdict": "needs-fix", "blocking_issues": [{"code": "C1"}]},
    )
    artifact_path = tmp_path / relative

    readback = _PIPELINE.readback_persisted_artifact(
        artifact_path, expected_body_sha256=_BODY_SHA256, expected_verdict="needs-fix"
    )
    assert readback["violations"] == []
    assert readback["verdict_identity"] is True

    gate = _PIPELINE.gate_final_review(remote_update_ok=True, readback=readback)
    assert gate == {"final_review_allowed": True, "reasons": []}


# ---------------------------------------------------------------------------
# AC3: wrong reviewed_body_sha256 -> fail-closed.
# ---------------------------------------------------------------------------


def test_given_wrong_expected_body_sha256_when_readback_then_fail_closed(tmp_path: Path):
    relative, _artifact_sha256 = transport.write_semantic_artifact(
        artifact_root=tmp_path,
        issue_number=_ISSUE,
        repo=_REPO,
        invocation_id="wrong-body-sha",
        attempt=1,
        reviewed_body_sha256=_BODY_SHA256,
        semantic_result={"verdict": "approve", "blocking_issues": []},
    )
    artifact_path = tmp_path / relative

    readback = _PIPELINE.readback_persisted_artifact(
        artifact_path, expected_body_sha256="sha256:" + "0" * 64, expected_verdict="approve"
    )
    assert readback["verdict_identity"] is False
    assert "body_sha256_mismatch" in readback["violations"]

    gate = _PIPELINE.gate_final_review(remote_update_ok=True, readback=readback)
    assert gate["final_review_allowed"] is False
    assert "body_sha256_mismatch" in gate["reasons"]


# ---------------------------------------------------------------------------
# AC4: wrong repo / issue / invocation / attempt / artifact SHA (checked at
# the reviewer_transport.verify_artifact() layer readback_persisted_artifact()
# now delegates its binding comparison to) -> fail-closed.
# ---------------------------------------------------------------------------


def test_given_wrong_binding_fields_when_verify_artifact_then_fail_closed(tmp_path: Path):
    relative, digest = transport.write_semantic_artifact(
        artifact_root=tmp_path,
        issue_number=_ISSUE,
        repo=_REPO,
        invocation_id="wrong-binding",
        attempt=1,
        reviewed_body_sha256=_BODY_SHA256,
        semantic_result={"verdict": "approve", "blocking_issues": []},
    )
    base_kwargs = dict(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="wrong-binding",
        expected_attempt=1,
        expected_sha256=digest,
    )
    assert transport.verify_artifact(**base_kwargs)["status"] == "valid"

    assert transport.verify_artifact(**{**base_kwargs, "expected_repo": "other/repo"})["status"] == "integrity_failure"
    assert transport.verify_artifact(**{**base_kwargs, "expected_issue": 1})["status"] == "integrity_failure"
    assert (
        transport.verify_artifact(**{**base_kwargs, "expected_invocation_id": "other-invocation"})["status"]
        == "integrity_failure"
    )
    assert transport.verify_artifact(**{**base_kwargs, "expected_attempt": 2})["status"] == "integrity_failure"
    assert (
        transport.verify_artifact(**{**base_kwargs, "expected_sha256": "sha256:" + "0" * 64})["status"]
        == "integrity_failure"
    )


# ---------------------------------------------------------------------------
# AC5: artifact semantic_result vs compact wire tampering mismatch ->
# fail-closed via verify_wire_matches_artifact().
# ---------------------------------------------------------------------------


def test_given_tampered_compact_wire_when_cross_checked_against_artifact_then_fail_closed(tmp_path: Path):
    relative, digest = transport.write_semantic_artifact(
        artifact_root=tmp_path,
        issue_number=_ISSUE,
        repo=_REPO,
        invocation_id="wire-tamper",
        attempt=1,
        reviewed_body_sha256=_BODY_SHA256,
        semantic_result={"verdict": "needs-fix", "blocking_issues": [{"code": "C1"}]},
    )
    verified = transport.verify_artifact(
        artifact_root=tmp_path,
        artifact_relative=relative,
        expected_repo=_REPO,
        expected_issue=_ISSUE,
        expected_body_sha256=_BODY_SHA256,
        expected_invocation_id="wire-tamper",
        expected_attempt=1,
        expected_sha256=digest,
    )
    assert verified["status"] == "valid"

    tampered_wire = transport.build_compact_v2(
        verdict="approve",
        summary="contract ready",
        blockers=0,
        reviewed_body_sha256=_BODY_SHA256,
        attempt_id="wire-tamper",
        artifact_relative=relative,
        artifact_sha256=digest,
    )
    cross = transport.verify_wire_matches_artifact(
        wire=tampered_wire, verified_artifact=verified, artifact_relative=relative, artifact_sha256=digest
    )
    assert cross["status"] == "integrity_failure"
    assert cross["reason_code"] == "wire_artifact_semantic_mismatch"

    genuine_wire = transport.project_compact_v2_from_artifact(
        verified["payload"], attempt_id="wire-tamper", artifact_relative=relative, artifact_sha256=digest
    )
    genuine_cross = transport.verify_wire_matches_artifact(
        wire=genuine_wire, verified_artifact=verified, artifact_relative=relative, artifact_sha256=digest
    )
    assert genuine_cross["status"] == "valid"


# ---------------------------------------------------------------------------
# AC6: existing malformed JSON / duplicate key / non-finite JSON / symlink /
# non-regular-file security regressions still fail-closed (this module's
# OWN symlink / non-regular-file coverage; the malformed-JSON/duplicate-key
# fixtures themselves live in test_issue_reviewer_contract_static.py, updated
# to production V2 shape by this same Issue).
# ---------------------------------------------------------------------------


def test_given_symlinked_artifact_path_when_readback_then_rejected_as_not_regular_file(tmp_path: Path):
    relative, _artifact_sha256 = transport.write_semantic_artifact(
        artifact_root=tmp_path,
        issue_number=_ISSUE,
        repo=_REPO,
        invocation_id="symlink-target",
        attempt=1,
        reviewed_body_sha256=_BODY_SHA256,
        semantic_result={"verdict": "approve", "blocking_issues": []},
    )
    real_path = tmp_path / relative
    symlink_path = tmp_path / "artifact_symlink.json"
    symlink_path.symlink_to(real_path)

    readback = _PIPELINE.readback_persisted_artifact(
        symlink_path, expected_body_sha256=_BODY_SHA256, expected_verdict="approve"
    )
    assert readback["verdict_identity"] is False
    assert "artifact_not_regular_file" in readback["violations"]


def test_given_duplicate_json_key_when_readback_then_rejected_as_not_strict_json(tmp_path: Path):
    artifact = tmp_path / "artifact.json"
    artifact.write_text(
        '{"schema": "REVIEWER_COMPACT_ARTIFACT_V2", "repository": "squne121/loop-protocol", '
        '"issue_number": 2242, "reviewed_body_sha256": "sha256:aa", "reviewed_body_sha256": "sha256:bb", '
        '"invocation_id": "dup-key", "attempt": 1, '
        '"semantic_result": {"verdict": "approve", "blocking_issues": []}}',
        encoding="utf-8",
    )
    readback = _PIPELINE.readback_persisted_artifact(
        artifact, expected_body_sha256="sha256:aa", expected_verdict="approve"
    )
    assert readback["verdict_identity"] is False
    assert "artifact_not_strict_json" in readback["violations"]


# ---------------------------------------------------------------------------
# AC7: run_root_review_pipeline.py must not maintain a second independent
# field-map of the canonical V2 persisted artifact layout.
# ---------------------------------------------------------------------------


def test_pipeline_source_delegates_v2_field_access_to_reviewer_transport():
    """GIVEN run_root_review_pipeline.py's source
    WHEN scanned for direct top-level flat-V1-key access on a readback
    payload
    THEN it no longer reads `payload.get("body_sha256")` /
    `payload.get("verdict")` directly -- the SAME regression this Issue
    fixes -- and instead calls the shared reviewer_transport accessors."""
    source = PIPELINE_SCRIPT.read_text(encoding="utf-8")
    assert 'payload.get("body_sha256")' not in source
    assert 'payload.get("verdict")' not in source
    assert "_reviewer_transport.extract_binding_context(" in source
    assert "_reviewer_transport.check_artifact_binding(" in source
    assert "_reviewer_transport.semantic_verdict_and_count(" in source


# ---------------------------------------------------------------------------
# AC8: #2054 AC8 / PR #2142 SSOT contract (reviewer_transport.py is the sole
# owner of artifact schema) pinned by an explicit regression test.
# ---------------------------------------------------------------------------


def test_reviewer_transport_owns_artifact_schema_constant_and_accessors():
    """GIVEN reviewer_transport.py
    WHEN checked for the canonical artifact schema tag and its accessors
    THEN ARTIFACT_SCHEMA and the binding/verdict accessors this Issue's
    readback fix depends on all live in reviewer_transport.py (not
    reimplemented anywhere else), pinning the #2054/PR #2142 SSOT contract."""
    assert transport.ARTIFACT_SCHEMA == "REVIEWER_COMPACT_ARTIFACT_V2"
    assert callable(transport.extract_binding_context)
    assert callable(transport.check_artifact_binding)
    assert callable(transport.semantic_verdict_and_count)
    assert callable(transport.verify_artifact)

    pipeline_source = PIPELINE_SCRIPT.read_text(encoding="utf-8")
    assert 'ARTIFACT_SCHEMA = "REVIEWER_COMPACT_ARTIFACT_V2"' not in pipeline_source


# ---------------------------------------------------------------------------
# AC9: gate-final-review CLI E2E test reaches final_review_allowed: true for
# an artifact generated by the canonical producer.
# ---------------------------------------------------------------------------


def test_given_canonical_artifact_when_gate_final_review_cli_invoked_then_final_review_allowed(tmp_path: Path):
    relative, _artifact_sha256 = transport.write_semantic_artifact(
        artifact_root=tmp_path,
        issue_number=_ISSUE,
        repo=_REPO,
        invocation_id="cli-e2e",
        attempt=1,
        reviewed_body_sha256=_BODY_SHA256,
        semantic_result={"verdict": "approve", "blocking_issues": []},
    )
    artifact_path = tmp_path / relative

    readback_proc = subprocess.run(
        [
            sys.executable,
            str(PIPELINE_SCRIPT),
            "readback",
            "--artifact-path",
            str(artifact_path),
            "--expected-body-sha256",
            _BODY_SHA256,
            "--expected-verdict",
            "approve",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert readback_proc.returncode == 0, readback_proc.stderr
    readback_payload = json.loads(readback_proc.stdout)
    assert readback_payload["verdict_identity"] is True

    gate_proc = subprocess.run(
        [
            sys.executable,
            str(PIPELINE_SCRIPT),
            "gate-final-review",
            "--artifact-path",
            str(artifact_path),
            "--expected-body-sha256",
            _BODY_SHA256,
            "--expected-verdict",
            "approve",
            "--remote-update-ok",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert gate_proc.returncode == 0, gate_proc.stderr
    gate_payload = json.loads(gate_proc.stdout)
    assert gate_payload == {"final_review_allowed": True, "reasons": []}
