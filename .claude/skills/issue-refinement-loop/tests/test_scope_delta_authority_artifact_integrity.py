"""
test_scope_delta_authority_artifact_integrity.py

#2053 AC11: fault-injection regression matrix for the producer -> router ->
consumer chain: write_failure, missing_file, malformed_json, digest_mismatch,
wrong_issue, wrong_git_head, wrong_invocation_id, stale_previous_invocation.

Each fault must be independently detected and must never be silently
accepted (`status: ok`) at either the router or the consumer stage.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import run_refinement_preflight as preflight  # noqa: E402
import decide_next_loop_action as router  # noqa: E402

REPO_ROOT = SKILL_ROOT.parent.parent.parent
REPO = "squne121/loop-protocol"
ISSUE_NUMBER = 2053
GIT_HEAD_SHA = "1111111111222222222233333333334444444444"


def _artifact_dir(invocation_id: str) -> Path:
    return (
        REPO_ROOT
        / ".claude"
        / "artifacts"
        / "issue-refinement-loop"
        / str(ISSUE_NUMBER)
        / "authority-transport"
        / invocation_id
    )


@pytest.fixture(autouse=True)
def _cleanup_artifacts():
    yield
    target = REPO_ROOT / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


def _evidence(comment_id=1):
    return {
        "schema_version": "SCOPE_DELTA_AUTHORITY_EVIDENCE_V1",
        "source_kind": "issue_comment",
        "source_ref": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#issuecomment-{comment_id}",
        "source_issue_number": ISSUE_NUMBER,
        "comment_id": comment_id,
        "comment_url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#issuecomment-{comment_id}",
        "issue_url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}",
        "body_sha256": "sha256:fault",
        "author_login": "owner",
        "author_type": "User",
        "author_association": "OWNER",
        "captured_at": "2026-08-09T00:00:00Z",
        "directive_markers": ["revised acceptance criteria"],
        "extracted_directives": ["AC1: fault injection"],
        "ambiguity_flags": [],
        "boundary_flags": [],
        "confidence": "explicit",
    }


def _produce(invocation_id: str) -> dict:
    produced, error = preflight.generate_authority_transport_manifest(
        evidence=_evidence(),
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        invocation_id=invocation_id,
        git_head_sha=GIT_HEAD_SHA,
        repo_root=REPO_ROOT,
    )
    assert error is None, error
    return produced


def _accept_router_receipt(produced: dict, invocation_id: str) -> dict:
    receipt = router.generate_router_receipt(
        transport_manifest_path=produced["manifest_path"],
        issue_number=ISSUE_NUMBER,
        invocation_id=invocation_id,
        git_head_sha=GIT_HEAD_SHA,
        authority_expected=True,
        repo_root=REPO_ROOT,
    )
    assert receipt["status"] == "ok"
    return _artifact_dir(invocation_id) / "scope_delta_router_receipt_v1.json"


# --- write_failure -----------------------------------------------------


def test_write_failure_producer_reports_error(tmp_path, monkeypatch):
    """GIVEN the atomic writer cannot rename the temp file into place
    WHEN the producer runs
    THEN it returns (None, error) rather than a manifest -- never
    "succeeding" with a partially-written artifact.
    """

    def _boom(path, data):
        return False, None, "write_failure:OSError:simulated"

    monkeypatch.setattr(preflight, "_atomic_write_json_with_readback", _boom)
    result, error = preflight.generate_authority_transport_manifest(
        evidence=_evidence(),
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        invocation_id="fault-write-failure",
        git_head_sha=GIT_HEAD_SHA,
        repo_root=REPO_ROOT,
    )
    assert result is None
    assert error.startswith("write_failure")


# --- missing_file --------------------------------------------------------


def test_missing_file_router_environment_failure():
    receipt = router.generate_router_receipt(
        transport_manifest_path=str(_artifact_dir("fault-missing") / "does_not_exist.json"),
        issue_number=ISSUE_NUMBER,
        invocation_id="fault-missing",
        git_head_sha=GIT_HEAD_SHA,
        authority_expected=True,
        repo_root=REPO_ROOT,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "missing_file"


def test_missing_file_consumer_environment_failure():
    receipt = preflight.consume_authority_transport(
        router_receipt_path=str(_artifact_dir("fault-missing-consumer") / "does_not_exist.json"),
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        invocation_id="fault-missing-consumer",
        git_head_sha=GIT_HEAD_SHA,
        repo_root=REPO_ROOT,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "missing_file"


# --- malformed_json --------------------------------------------------------


def test_malformed_json_router_environment_failure(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    receipt = router.generate_router_receipt(
        transport_manifest_path=str(bad),
        issue_number=ISSUE_NUMBER,
        invocation_id="fault-malformed",
        git_head_sha=GIT_HEAD_SHA,
        authority_expected=True,
        repo_root=REPO_ROOT,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "malformed_json"


def test_malformed_json_consumer_environment_failure(tmp_path):
    # #2053 P1 fix-delta: the bad receipt must live under the confined
    # artifact root -- consume_authority_transport() now rejects any path
    # outside .claude/artifacts/ before it is ever opened, so a tmp_path
    # fixture here would (correctly) fail with path_confinement_* instead
    # of exercising the malformed_json fault this test targets.
    bad = _artifact_dir("fault-malformed-consumer") / "bad_receipt.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not json at all", encoding="utf-8")
    receipt = preflight.consume_authority_transport(
        router_receipt_path=str(bad),
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        invocation_id="fault-malformed-consumer",
        git_head_sha=GIT_HEAD_SHA,
        repo_root=REPO_ROOT,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "malformed_json"


# --- digest_mismatch --------------------------------------------------------


def test_digest_mismatch_router_environment_failure(tmp_path):
    produced = _produce("fault-digest-router")
    manifest = json.loads(Path(produced["manifest_path"]).read_text(encoding="utf-8"))
    manifest["payload"]["extracted_directives"] = ["AC1: TAMPERED"]
    tampered_path = tmp_path / "tampered_manifest.json"
    tampered_path.write_text(json.dumps(manifest), encoding="utf-8")

    receipt = router.generate_router_receipt(
        transport_manifest_path=str(tampered_path),
        issue_number=ISSUE_NUMBER,
        invocation_id="fault-digest-router",
        git_head_sha=GIT_HEAD_SHA,
        authority_expected=True,
        repo_root=REPO_ROOT,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "digest_mismatch"


def test_digest_mismatch_consumer_environment_failure(tmp_path):
    produced = _produce("fault-digest-consumer")
    router_receipt_path = _accept_router_receipt(produced, "fault-digest-consumer")

    # Tamper with the manifest on disk AFTER the router accepted it.
    manifest_path = Path(produced["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["payload"]["extracted_directives"] = ["AC1: TAMPERED AFTER ROUTER"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    receipt = preflight.consume_authority_transport(
        router_receipt_path=str(router_receipt_path),
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        invocation_id="fault-digest-consumer",
        git_head_sha=GIT_HEAD_SHA,
        repo_root=REPO_ROOT,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "digest_mismatch"


# --- wrong_issue / wrong_git_head / wrong_invocation_id --------------------


@pytest.mark.parametrize(
    ("field", "bad_value", "reason_code"),
    [
        ("issue_number", 999999, "wrong_issue"),
        ("git_head_sha", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "wrong_git_head"),
        ("invocation_id", "some-other-invocation", "wrong_invocation_id"),
    ],
)
def test_source_mismatch_router_environment_failure(tmp_path, field, bad_value, reason_code):
    produced = _produce(f"fault-mismatch-{reason_code}")
    manifest = json.loads(Path(produced["manifest_path"]).read_text(encoding="utf-8"))
    manifest[field] = bad_value
    mismatched_path = tmp_path / f"mismatch_{reason_code}.json"
    mismatched_path.write_text(json.dumps(manifest), encoding="utf-8")

    receipt = router.generate_router_receipt(
        transport_manifest_path=str(mismatched_path),
        issue_number=ISSUE_NUMBER,
        invocation_id=f"fault-mismatch-{reason_code}",
        git_head_sha=GIT_HEAD_SHA,
        authority_expected=True,
        repo_root=REPO_ROOT,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == reason_code


# --- stale_previous_invocation ----------------------------------------------


def test_stale_previous_invocation_consumer_environment_failure():
    invocation_id = "fault-stale-1"
    invocation_dir = _artifact_dir(invocation_id)
    invocation_dir.mkdir(parents=True, exist_ok=True)
    (invocation_dir / "scope_delta_consumption_receipt_v1.json").write_text(
        json.dumps({"schema_version": "SCOPE_DELTA_CONSUMPTION_RECEIPT_V1", "status": "ok"}),
        encoding="utf-8",
    )

    receipt = preflight.consume_authority_transport(
        router_receipt_path=str(invocation_dir / "scope_delta_router_receipt_v1.json"),
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        invocation_id=invocation_id,
        git_head_sha=GIT_HEAD_SHA,
        repo_root=REPO_ROOT,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "stale_previous_invocation"
    assert receipt["mutation_applied"] is False
