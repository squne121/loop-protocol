"""
test_scope_delta_authority_transport.py

#2053 AC1/AC7/AC8/AC10: producer -> router transport of
SCOPE_DELTA_AUTHORITY_TRANSPORT_V1, immutable per-invocation artifacts, and
fail-closed environment_failure when authority_expected=true and the
sidecar is missing / digest-mismatched.

These tests invoke real subprocesses via command_registry.render_command()
(registry-rendered argv), matching the Issue's "canonical registry-rendered
subprocess chain (producer->router->consumer)" runtime verification shape
(AC7/AC8/AC11).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import command_registry as reg  # noqa: E402

REPO_ROOT = SKILL_ROOT.parent.parent.parent
REPO = "squne121/loop-protocol"


def _evidence_fixture(tmp_path: Path, *, comment_id: int = 1) -> Path:
    evidence = {
        "schema_version": "SCOPE_DELTA_AUTHORITY_EVIDENCE_V1",
        "source_kind": "issue_comment",
        "source_ref": f"https://github.com/{REPO}/issues/2053#issuecomment-{comment_id}",
        "source_issue_number": 2053,
        "comment_id": comment_id,
        "comment_url": f"https://github.com/{REPO}/issues/2053#issuecomment-{comment_id}",
        "issue_url": f"https://github.com/{REPO}/issues/2053",
        "body_sha256": "sha256:deadbeef",
        "author_login": "owner",
        "author_type": "User",
        "author_association": "OWNER",
        "captured_at": "2026-08-09T00:00:00Z",
        "directive_markers": ["revised acceptance criteria"],
        "extracted_directives": ["AC1: do X"],
        "ambiguity_flags": [],
        "boundary_flags": [],
        "confidence": "explicit",
    }
    fixture_path = tmp_path / "evidence.json"
    fixture_path.write_text(json.dumps(evidence), encoding="utf-8")
    return fixture_path


def _run_registry_command(command_id: str, params: dict) -> subprocess.CompletedProcess:
    argv = reg.render_command(command_id, params)
    return subprocess.run(argv, capture_output=True, text=True, cwd=str(REPO_ROOT))


def _issue_artifact_dir(issue_number: int, invocation_id: str) -> Path:
    return (
        REPO_ROOT
        / ".claude"
        / "artifacts"
        / "issue-refinement-loop"
        / str(issue_number)
        / "authority-transport"
        / invocation_id
    )


def _cleanup(issue_number: int) -> None:
    import shutil

    target = REPO_ROOT / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


@pytest.fixture(autouse=True)
def _cleanup_artifacts():
    yield
    _cleanup(2053)


def test_producer_generates_immutable_authority_transport_with_digest(tmp_path):
    """GIVEN a SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 fixture
    WHEN the producer (registry-rendered authority_transport.produce) runs
    THEN it writes an immutable SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest
    under an invocation-scoped directory, with a payload_sha256 that matches
    the canonical digest of the payload.
    """
    fixture_path = _evidence_fixture(tmp_path)
    invocation_id = "test-producer-1"
    proc = _run_registry_command(
        "authority_transport.produce",
        {
            "issue_number": 2053,
            "repo": REPO,
            "invocation_id": invocation_id,
            "git_head_sha": "cafebabe0123456789",
            "evidence_fixture_path": str(fixture_path),
        },
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "ok"
    manifest = result["manifest"]
    assert manifest["schema_version"] == "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1"
    assert manifest["invocation_id"] == invocation_id
    assert manifest["canonicalization_id"] == "loop-protocol-json-c14n-v1"
    assert len(manifest["payload_sha256"]) == 64

    manifest_path = Path(result["manifest_path"])
    assert manifest_path.exists()
    assert manifest_path.parent == _issue_artifact_dir(2053, invocation_id)
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk["payload_sha256"] == manifest["payload_sha256"]


def test_registry_producer_router_consumer_bind_same_digest(tmp_path):
    """GIVEN a fresh invocation
    WHEN the producer, router, and consumer all run via registry-rendered
    argv (real subprocesses)
    THEN all three stages report the exact same canonical payload digest,
    and the consumer reports status: ok with mutation/readback/fresh-rerun
    all true.
    """
    fixture_path = _evidence_fixture(tmp_path, comment_id=2)
    invocation_id = "test-e2e-chain-1"
    git_head_sha = "0123456789abcdef0123456789abcdef01234567"

    produce = _run_registry_command(
        "authority_transport.produce",
        {
            "issue_number": 2053,
            "repo": REPO,
            "invocation_id": invocation_id,
            "git_head_sha": git_head_sha,
            "evidence_fixture_path": str(fixture_path),
        },
    )
    assert produce.returncode == 0, produce.stderr
    produce_result = json.loads(produce.stdout)
    manifest_path = produce_result["manifest_path"]
    producer_digest = produce_result["manifest"]["payload_sha256"]

    loop_state_path = _issue_artifact_dir(2053, invocation_id) / "loop_state.json"
    loop_state_path.parent.mkdir(parents=True, exist_ok=True)
    loop_state_path.write_text(json.dumps({"iteration": 0, "max_iterations": 3}), encoding="utf-8")
    loop_state_relpath = loop_state_path.relative_to(REPO_ROOT)

    route = _run_registry_command(
        "decide.run.with_authority_transport",
        {
            "loop_state_file": str(loop_state_relpath),
            "verdict": "needs-fix",
            "max_iterations": 3,
            "issue_number": 2053,
            "authority_transport_manifest_path": manifest_path,
            "invocation_id": invocation_id,
            "git_head_sha": git_head_sha,
        },
    )
    assert route.returncode == 0, route.stderr
    assert "STATUS: pass" in route.stdout

    router_receipt_path = _issue_artifact_dir(2053, invocation_id) / "scope_delta_router_receipt_v1.json"
    assert router_receipt_path.exists()
    router_receipt = json.loads(router_receipt_path.read_text(encoding="utf-8"))
    assert router_receipt["status"] == "ok"
    assert router_receipt["transport_payload_sha256"] == producer_digest
    assert router_receipt["recomputed_payload_sha256"] == producer_digest

    consume = _run_registry_command(
        "authority_transport.consume",
        {
            "issue_number": 2053,
            "repo": REPO,
            "invocation_id": invocation_id,
            "git_head_sha": git_head_sha,
            "router_receipt_path": str(router_receipt_path),
        },
    )
    assert consume.returncode == 0, consume.stderr
    consumption_receipt = json.loads(consume.stdout)
    assert consumption_receipt["status"] == "ok"
    assert consumption_receipt["transport_payload_sha256"] == producer_digest
    assert consumption_receipt["consumed_payload_sha256"] == producer_digest
    assert consumption_receipt["mutation_applied"] is True
    assert consumption_receipt["readback_verified"] is True
    assert consumption_receipt["fresh_rerun_performed"] is True


def test_expected_sidecar_missing_is_environment_failure():
    """GIVEN authority_expected=true and no manifest at the given path
    WHEN the router (decide_next_loop_action.py) runs
    THEN it fails closed to environment_failure instead of downgrading to
    the legacy route (AC8).
    """
    import decide_next_loop_action as router

    receipt = router.generate_router_receipt(
        transport_manifest_path=str(Path("/nonexistent/does-not-exist.json")),
        issue_number=2053,
        invocation_id="missing-sidecar-1",
        git_head_sha="deadbeef",
        authority_expected=True,
        repo_root=None,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "missing_file"


def test_digest_or_invocation_mismatch_blocks_before_router(tmp_path):
    """GIVEN a manifest whose payload_sha256 has been tampered with
    WHEN the router verifies it
    THEN it fails closed with digest_mismatch (never accepted as ok).
    """
    import decide_next_loop_action as router

    manifest = {
        "schema_version": "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1",
        "invocation_id": "tampered-1",
        "issue_number": 2053,
        "repo": REPO,
        "git_head_sha": "deadbeef",
        "generated_at": "2026-08-09T00:00:00Z",
        "canonicalization_id": "loop-protocol-json-c14n-v1",
        "source_comment_id": 1,
        "source_comment_url": None,
        "source_issue_body_sha256": None,
        "source_kind": "issue_comment",
        "payload": {"a": 1},
        "payload_sha256": "0" * 64,  # deliberately wrong digest
    }
    manifest_path = tmp_path / "tampered_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    receipt = router.generate_router_receipt(
        transport_manifest_path=str(manifest_path),
        issue_number=2053,
        invocation_id="tampered-1",
        git_head_sha="deadbeef",
        authority_expected=True,
        repo_root=None,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "digest_mismatch"


def test_stale_previous_invocation_sidecar_is_never_reused(tmp_path):
    """GIVEN a consumption receipt already exists for an invocation_id
    WHEN the consumer is invoked again for the same invocation_id
    THEN it refuses to mutate a second time (stale_previous_invocation),
    never silently re-consuming a possibly-stale artifact (AC10).
    """
    import run_refinement_preflight as preflight

    issue_number = 2053
    invocation_id = "stale-guard-1"
    repo_root = REPO_ROOT
    invocation_dir = _issue_artifact_dir(issue_number, invocation_id)
    invocation_dir.mkdir(parents=True, exist_ok=True)
    (invocation_dir / "scope_delta_consumption_receipt_v1.json").write_text(
        json.dumps({"schema_version": "SCOPE_DELTA_CONSUMPTION_RECEIPT_V1", "status": "ok"}),
        encoding="utf-8",
    )

    receipt = preflight.consume_authority_transport(
        router_receipt_path=str(invocation_dir / "scope_delta_router_receipt_v1.json"),
        issue_number=issue_number,
        repo=REPO,
        invocation_id=invocation_id,
        git_head_sha="deadbeef",
        repo_root=repo_root,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "stale_previous_invocation"
    assert receipt["mutation_applied"] is False
