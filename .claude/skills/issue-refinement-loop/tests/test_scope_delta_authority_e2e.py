"""
test_scope_delta_authority_e2e.py

#2053 AC9: the controlled consumer verifies the same digest the producer
generated, mutates exactly once, reads back, and performs a fresh rerun --
consuming exactly the generated authority (never a substituted / stale /
foreign payload).
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


def _evidence(comment_id=42):
    return {
        "schema_version": "SCOPE_DELTA_AUTHORITY_EVIDENCE_V1",
        "source_kind": "issue_comment",
        "source_ref": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#issuecomment-{comment_id}",
        "source_issue_number": ISSUE_NUMBER,
        "comment_id": comment_id,
        "comment_url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#issuecomment-{comment_id}",
        "issue_url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}",
        "body_sha256": "sha256:e2e",
        "author_login": "owner",
        "author_type": "User",
        "author_association": "OWNER",
        "captured_at": "2026-08-09T00:00:00Z",
        "directive_markers": ["revised acceptance criteria"],
        "extracted_directives": ["AC1: e2e directive"],
        "ambiguity_flags": [],
        "boundary_flags": [],
        "confidence": "explicit",
    }


def test_controlled_rewrite_consumes_exactly_generated_authority():
    """GIVEN the producer generates a transport manifest for a specific
    directive payload
    WHEN the router accepts it and the controlled consumer runs
    THEN the consumer's consumed_payload_sha256 is bit-for-bit the same
    digest the producer generated -- never a different/edited/substituted
    payload -- and the payload content itself round-trips unchanged.
    """
    invocation_id = "e2e-exact-consumption-1"
    git_head_sha = "abc123def456abc123def456abc123def456abc"
    evidence = _evidence()

    produced, error = preflight.generate_authority_transport_manifest(
        evidence=evidence,
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        invocation_id=invocation_id,
        git_head_sha=git_head_sha,
        repo_root=REPO_ROOT,
    )
    assert error is None, error
    manifest = produced["manifest"]

    router_receipt = router.generate_router_receipt(
        transport_manifest_path=produced["manifest_path"],
        issue_number=ISSUE_NUMBER,
        invocation_id=invocation_id,
        git_head_sha=git_head_sha,
        authority_expected=True,
        repo_root=REPO_ROOT,
    )
    assert router_receipt["status"] == "ok"

    router_receipt_path = _artifact_dir(invocation_id) / "scope_delta_router_receipt_v1.json"
    assert router_receipt_path.exists()

    consumption_receipt = preflight.consume_authority_transport(
        router_receipt_path=str(router_receipt_path),
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        invocation_id=invocation_id,
        git_head_sha=git_head_sha,
        repo_root=REPO_ROOT,
    )

    assert consumption_receipt["status"] == "ok"
    assert consumption_receipt["consumed_payload_sha256"] == manifest["payload_sha256"]
    assert consumption_receipt["mutation_applied"] is True
    assert consumption_receipt["readback_verified"] is True
    assert consumption_receipt["fresh_rerun_performed"] is True

    # AND the on-disk consumed payload round-trips the exact evidence.
    consumed_path = _artifact_dir(invocation_id) / "consumed_authority_payload_v1.json"
    consumed = json.loads(consumed_path.read_text(encoding="utf-8"))
    assert consumed["payload"] == evidence
    assert consumed["payload_sha256"] == manifest["payload_sha256"]


def test_consumer_never_accepts_a_router_receipt_pointing_at_a_different_manifest_payload(tmp_path):
    """GIVEN a router receipt whose transport_payload_sha256 does not match
    the manifest it points to (simulated tamper between router and
    consumer)
    WHEN the consumer runs
    THEN it fails closed with digest_mismatch and performs no mutation.
    """
    invocation_id = "e2e-tamper-1"
    git_head_sha = "abc123def456abc123def456abc123def456abc"
    evidence = _evidence(comment_id=7)

    produced, error = preflight.generate_authority_transport_manifest(
        evidence=evidence,
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        invocation_id=invocation_id,
        git_head_sha=git_head_sha,
        repo_root=REPO_ROOT,
    )
    assert error is None

    tampered_receipt = {
        "schema_version": "SCOPE_DELTA_ROUTER_RECEIPT_V1",
        "invocation_id": invocation_id,
        "issue_number": ISSUE_NUMBER,
        "git_head_sha": git_head_sha,
        "generated_at": "2026-08-09T00:00:00Z",
        "transport_manifest_path": produced["manifest_path"],
        "transport_payload_sha256": "f" * 64,  # deliberately wrong
        "recomputed_payload_sha256": "f" * 64,
        "status": "ok",
        "reason_code": None,
    }
    # #2053 P1 fix-delta: the tampered receipt must live under the confined
    # artifact root (consume_authority_transport() now rejects any path
    # outside .claude/artifacts/ before opening it).
    receipt_path = _artifact_dir(invocation_id) / "tampered_router_receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(tampered_receipt), encoding="utf-8")

    consumption_receipt = preflight.consume_authority_transport(
        router_receipt_path=str(receipt_path),
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        invocation_id=invocation_id,
        git_head_sha=git_head_sha,
        repo_root=REPO_ROOT,
    )
    assert consumption_receipt["status"] == "environment_failure"
    assert consumption_receipt["reason_code"] == "digest_mismatch"
    assert consumption_receipt["mutation_applied"] is False

    consumed_path = _artifact_dir(invocation_id) / "consumed_authority_payload_v1.json"
    assert not consumed_path.exists()
