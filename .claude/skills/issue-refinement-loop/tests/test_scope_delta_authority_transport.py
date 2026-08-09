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

    # Issue #2053 P0 fix-delta (iteration 2): authority transport routing
    # is wired directly into the canonical "decide.run" command (not a
    # sibling ID) -- decide_next_loop_action.py's actual production
    # invocation path carries the manifest.
    route = _run_registry_command(
        "decide.run",
        {
            "loop_state_file": str(loop_state_relpath),
            "verdict": "needs-fix",
            "max_iterations": 3,
            "issue_number": 2053,
            "authority_transport_manifest_path": manifest_path,
            "authority_expected": True,
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


def test_fresh_rerun_drift_fails_closed_not_silently_ok(tmp_path):
    """#2053 P0 fix-delta (iteration 2, OWNER PR review): the consumer's
    "fresh rerun" (re-running classify_scope_delta_authority() against the
    consumed payload) must be a real gate, not best-effort telemetry that
    is discarded. GIVEN a manifest whose payload no longer reclassifies to
    contract_update_required (here: missing source_issue_body_sha256, so
    the fresh classification fails closed to human_escalation) THEN the
    consumption receipt is environment_failure/fresh_rerun_route_drift with
    fresh_rerun_performed=False -- never status: ok.
    """
    import run_refinement_preflight as preflight

    issue_number = 2053
    invocation_id = "fresh-rerun-drift-1"
    repo_root = REPO_ROOT
    git_head_sha = "deadbeef"
    invocation_dir = _issue_artifact_dir(issue_number, invocation_id)
    invocation_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1",
        "invocation_id": invocation_id,
        "issue_number": issue_number,
        "repo": REPO,
        "git_head_sha": git_head_sha,
        "generated_at": "2026-08-09T00:00:00Z",
        "canonicalization_id": "loop-protocol-json-c14n-v1",
        "source_comment_id": 1,
        "source_comment_url": f"https://github.com/{REPO}/issues/2053#issuecomment-1",
        # No source_issue_body_sha256 -- the fresh classification of an
        # "explicit" human directive fails closed to human_escalation
        # without a base issue body digest to bind a patch plan to.
        "source_issue_body_sha256": None,
        "source_kind": "issue_comment",
        "payload": {
            "schema_version": "SCOPE_DELTA_AUTHORITY_EVIDENCE_V1",
            "source_kind": "issue_comment",
            "source_ref": f"https://github.com/{REPO}/issues/2053#issuecomment-1",
            "source_issue_number": issue_number,
            "comment_id": 1,
            "comment_url": f"https://github.com/{REPO}/issues/2053#issuecomment-1",
            "issue_url": f"https://github.com/{REPO}/issues/2053",
            "body_sha256": None,
            "author_login": "owner",
            "author_type": "User",
            "author_association": "OWNER",
            "captured_at": "2026-08-09T00:00:00Z",
            "directive_markers": ["revised acceptance criteria"],
            "extracted_directives": ["AC1: do X"],
            "ambiguity_flags": [],
            "boundary_flags": [],
            "confidence": "explicit",
        },
    }
    manifest["payload_sha256"] = preflight._sha256(preflight._canonical_json(manifest["payload"]))
    manifest_path = invocation_dir / "scope_delta_authority_transport_v1.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    router_receipt = {
        "schema_version": "SCOPE_DELTA_ROUTER_RECEIPT_V1",
        "invocation_id": invocation_id,
        "issue_number": issue_number,
        "git_head_sha": git_head_sha,
        "generated_at": "2026-08-09T00:00:01Z",
        "transport_manifest_path": str(manifest_path),
        "transport_payload_sha256": manifest["payload_sha256"],
        "recomputed_payload_sha256": manifest["payload_sha256"],
        "status": "ok",
        "reason_code": None,
    }
    router_receipt_path = invocation_dir / "scope_delta_router_receipt_v1.json"
    router_receipt_path.write_text(json.dumps(router_receipt), encoding="utf-8")

    receipt = preflight.consume_authority_transport(
        router_receipt_path=str(router_receipt_path),
        issue_number=issue_number,
        repo=REPO,
        invocation_id=invocation_id,
        git_head_sha=git_head_sha,
        repo_root=repo_root,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "fresh_rerun_route_drift"
    assert receipt["fresh_rerun_performed"] is False
    # The bounded local mutation (consumed payload write) still happened --
    # the gate is on the *receipt status*, not on suppressing the mutation
    # after the fact.
    assert receipt["mutation_applied"] is True


def test_symlinked_router_receipt_path_is_rejected(tmp_path):
    """#2053 P1 fix-delta (iteration 2, OWNER PR review): a router_receipt_path
    that is a symlink (even one that resolves to a legitimate on-disk
    router receipt) must be rejected before it is ever opened -- artifact
    paths are confined to the .claude/artifacts/ root and must be regular
    files, not symlinks.
    """
    import run_refinement_preflight as preflight

    issue_number = 2053
    invocation_id = "symlink-defense-1"
    repo_root = REPO_ROOT
    invocation_dir = _issue_artifact_dir(issue_number, invocation_id)
    invocation_dir.mkdir(parents=True, exist_ok=True)

    real_receipt_path = invocation_dir / "real_receipt.json"
    real_receipt_path.write_text(
        json.dumps({"schema_version": "SCOPE_DELTA_ROUTER_RECEIPT_V1", "status": "ok"}),
        encoding="utf-8",
    )
    symlink_path = invocation_dir / "scope_delta_router_receipt_v1.json"
    symlink_path.symlink_to(real_receipt_path)

    receipt = preflight.consume_authority_transport(
        router_receipt_path=str(symlink_path),
        issue_number=issue_number,
        repo=REPO,
        invocation_id=invocation_id,
        git_head_sha="deadbeef",
        repo_root=repo_root,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "path_confinement_symlink_rejected"
    assert receipt["mutation_applied"] is False


def test_outside_artifact_root_router_receipt_path_is_rejected(tmp_path):
    """#2053 P1 fix-delta: a router_receipt_path outside .claude/artifacts/
    (e.g. an attacker-influenceable string pointing elsewhere on disk) is
    rejected by path confinement rather than being opened.
    """
    import run_refinement_preflight as preflight

    outside_path = tmp_path / "not_confined_receipt.json"
    outside_path.write_text(
        json.dumps({"schema_version": "SCOPE_DELTA_ROUTER_RECEIPT_V1", "status": "ok"}),
        encoding="utf-8",
    )

    receipt = preflight.consume_authority_transport(
        router_receipt_path=str(outside_path),
        issue_number=2053,
        repo=REPO,
        invocation_id="outside-root-1",
        git_head_sha="deadbeef",
        repo_root=REPO_ROOT,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "path_confinement_outside_artifact_root"
    assert receipt["mutation_applied"] is False


def test_router_and_consumer_reject_wrong_repo_manifest():
    """#2053 P1 fix-delta (iteration 2, OWNER PR review): a manifest whose
    own `repo` field does not match the caller-expected repo must be
    rejected by BOTH the router (generate_router_receipt) and the
    controlled consumer (consume_authority_transport) -- same-issue-number,
    cross-repo spoofing must never pass through unnoticed (this is the same
    boundary PR #1332 previously added for evidence classification).
    """
    import decide_next_loop_action as router
    import run_refinement_preflight as preflight

    invocation_id = "wrong-repo-1"
    manifest = {
        "schema_version": "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1",
        "invocation_id": invocation_id,
        "issue_number": 2053,
        "repo": "attacker/loop-protocol-fork",
        "git_head_sha": "deadbeef",
        "generated_at": "2026-08-09T00:00:00Z",
        "canonicalization_id": "loop-protocol-json-c14n-v1",
        "source_comment_id": 1,
        "source_comment_url": None,
        "source_issue_body_sha256": None,
        "source_kind": "issue_comment",
        "payload": {"a": 1},
    }
    manifest_path = Path("/tmp") / f"wrong_repo_manifest_{invocation_id}.json"
    manifest["payload_sha256"] = preflight._sha256(preflight._canonical_json(manifest["payload"]))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    router_receipt = router.generate_router_receipt(
        transport_manifest_path=str(manifest_path),
        issue_number=2053,
        invocation_id=invocation_id,
        git_head_sha="deadbeef",
        authority_expected=True,
        repo=REPO,
        repo_root=None,
    )
    assert router_receipt["status"] == "environment_failure"
    assert router_receipt["reason_code"] == "wrong_repo"

    manifest_path.unlink()


def test_consumer_rejects_wrong_repo_manifest(tmp_path):
    """#2053 P1 fix-delta: the controlled consumer independently checks
    manifest["repo"] == repo, not just issue_number/git_head/invocation_id.
    """
    import run_refinement_preflight as preflight

    issue_number = 2053
    invocation_id = "wrong-repo-consumer-1"
    git_head_sha = "deadbeef"
    invocation_dir = _issue_artifact_dir(issue_number, invocation_id)
    invocation_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1",
        "invocation_id": invocation_id,
        "issue_number": issue_number,
        "repo": "attacker/loop-protocol-fork",
        "git_head_sha": git_head_sha,
        "generated_at": "2026-08-09T00:00:00Z",
        "canonicalization_id": "loop-protocol-json-c14n-v1",
        "source_comment_id": 1,
        "source_comment_url": None,
        "source_issue_body_sha256": None,
        "source_kind": "issue_comment",
        "payload": {"a": 1},
    }
    manifest["payload_sha256"] = preflight._sha256(preflight._canonical_json(manifest["payload"]))
    manifest_path = invocation_dir / "scope_delta_authority_transport_v1.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    router_receipt = {
        "schema_version": "SCOPE_DELTA_ROUTER_RECEIPT_V1",
        "invocation_id": invocation_id,
        "issue_number": issue_number,
        "git_head_sha": git_head_sha,
        "generated_at": "2026-08-09T00:00:01Z",
        "transport_manifest_path": str(manifest_path),
        "transport_payload_sha256": manifest["payload_sha256"],
        "recomputed_payload_sha256": manifest["payload_sha256"],
        "status": "ok",
        "reason_code": None,
    }
    router_receipt_path = invocation_dir / "scope_delta_router_receipt_v1.json"
    router_receipt_path.write_text(json.dumps(router_receipt), encoding="utf-8")

    receipt = preflight.consume_authority_transport(
        router_receipt_path=str(router_receipt_path),
        issue_number=issue_number,
        repo=REPO,
        invocation_id=invocation_id,
        git_head_sha=git_head_sha,
        repo_root=REPO_ROOT,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "wrong_repo"
    assert receipt["mutation_applied"] is False


def test_producer_refuses_to_overwrite_existing_manifest_same_invocation_id(tmp_path):
    """#2053 P1 fix-delta (iteration 2, OWNER PR review): true immutability
    -- re-running the producer with the SAME invocation_id must refuse to
    overwrite the manifest it already wrote (per-invocation-directory
    naming alone is not immutability; os.replace() would happily replace an
    existing destination).
    """
    import run_refinement_preflight as preflight

    fixture_path = _evidence_fixture(tmp_path, comment_id=101)
    evidence = json.loads(fixture_path.read_text(encoding="utf-8"))
    invocation_id = "producer-immutability-1"

    first, error = preflight.generate_authority_transport_manifest(
        evidence=evidence,
        issue_number=2053,
        repo=REPO,
        invocation_id=invocation_id,
        git_head_sha="cafebabe",
        repo_root=REPO_ROOT,
    )
    assert error is None
    assert first is not None

    second, error2 = preflight.generate_authority_transport_manifest(
        evidence=evidence,
        issue_number=2053,
        repo=REPO,
        invocation_id=invocation_id,
        git_head_sha="cafebabe",
        repo_root=REPO_ROOT,
    )
    assert second is None
    assert error2 == "manifest_already_exists"


def test_consumer_refuses_to_re_mutate_leftover_consumed_payload_without_receipt(tmp_path):
    """#2053 P1 fix-delta (iteration 2, OWNER PR review): idempotency state
    must be bound BEFORE mutation, not just via post-mutation receipt
    presence. GIVEN a consumed_authority_payload_v1.json already exists for
    this invocation_id (simulating a crash between the mutation write and
    the receipt publish) but NO consumption receipt exists yet, THEN the
    consumer refuses to re-mutate rather than silently re-applying.
    """
    import run_refinement_preflight as preflight

    issue_number = 2053
    invocation_id = "consumer-bind-before-mutation-1"
    git_head_sha = "deadbeef"
    invocation_dir = _issue_artifact_dir(issue_number, invocation_id)
    invocation_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1",
        "invocation_id": invocation_id,
        "issue_number": issue_number,
        "repo": REPO,
        "git_head_sha": git_head_sha,
        "generated_at": "2026-08-09T00:00:00Z",
        "canonicalization_id": "loop-protocol-json-c14n-v1",
        "source_comment_id": 1,
        "source_comment_url": None,
        "source_issue_body_sha256": "sha256:deadbeef",
        "source_kind": "issue_comment",
        "payload": {"a": 1},
    }
    manifest["payload_sha256"] = preflight._sha256(preflight._canonical_json(manifest["payload"]))
    manifest_path = invocation_dir / "scope_delta_authority_transport_v1.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    router_receipt = {
        "schema_version": "SCOPE_DELTA_ROUTER_RECEIPT_V1",
        "invocation_id": invocation_id,
        "issue_number": issue_number,
        "git_head_sha": git_head_sha,
        "generated_at": "2026-08-09T00:00:01Z",
        "transport_manifest_path": str(manifest_path),
        "transport_payload_sha256": manifest["payload_sha256"],
        "recomputed_payload_sha256": manifest["payload_sha256"],
        "status": "ok",
        "reason_code": None,
    }
    router_receipt_path = invocation_dir / "scope_delta_router_receipt_v1.json"
    router_receipt_path.write_text(json.dumps(router_receipt), encoding="utf-8")

    # Simulate a crash: the mutation write happened, but the receipt was
    # never published.
    (invocation_dir / "consumed_authority_payload_v1.json").write_text(
        json.dumps({"schema_version": "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1_CONSUMED"}),
        encoding="utf-8",
    )

    receipt = preflight.consume_authority_transport(
        router_receipt_path=str(router_receipt_path),
        issue_number=issue_number,
        repo=REPO,
        invocation_id=invocation_id,
        git_head_sha=git_head_sha,
        repo_root=REPO_ROOT,
    )
    assert receipt["status"] == "environment_failure"
    assert receipt["reason_code"] == "stale_previous_invocation"
    assert receipt["mutation_applied"] is False


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
