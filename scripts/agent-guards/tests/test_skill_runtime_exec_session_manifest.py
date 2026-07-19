"""
test_skill_runtime_exec_session_manifest.py

Issue #1546: Codex Stop/SubagentStop session manifest peer-write vs.
skill_runtime_exec.py runtime guard interaction. Covers AC3/AC4/AC5/AC10:

- AC3: an independent peer writer publishing the canonical *external*
  session manifest (outside the repository tree entirely) is never reported
  as an unauthorized_write_path false positive by the executor's generic
  repo-status diff -- no session-manifest-specific allowlist/typed policy is
  needed or added for this, because the write is structurally invisible to
  a repo-tree `git status` diff. AC3 exercises the real
  writeCodexSessionManifest() publish plumbing (open/write/fsync/link/
  fsync-dir/unlink) via a Node harness invoking it directly with a fixture
  manifest -- NOT the codex-hook-adapter.mjs producer subprocess. Issue
  #1546 OWNER Blocker 1: production code must never contain an environment
  variable that lets a caller skip producer validation while the adapter
  fabricates provenance/attestation fields for a manifest that was never
  actually produced. Exercising the writer directly (as the OWNER review
  itself suggested) proves the write-target/plumbing claim AC3 makes
  without needing that bypass, without needing ajv/ajv-formats on disk, and
  without weakening what AC3 asserts (repo-tree diff invisibility of the
  external write target).
- AC4 (regression gate): verified by the Issue's separate
  `git diff --exit-code -- scripts/agent-guards/skill_runtime_exec.py`
  Verification Command, not by this file.
- AC5: an independent peer's repo-local `tmp/session-manifests/**` write
  fails closed via the existing generic postcondition, across create,
  update, delete, ancestor-replacement, directory, symlink, and sibling
  variants -- the target object is never actually left in a *tracked*
  state (each test removes/restores what it created as part of proving the
  write was observed as unauthorized, matching the other runtime-exec
  negative tests in this directory).
- AC10: this whole module, run together, is the AC10 runtime evidence VC.
  test_ac10_runtime_evidence_artifact_is_captured additionally writes an
  AC10 evidence JSON artifact (OS/Node/Python versions, manifest SHA-256,
  locator, executor exit code, before/after unauthorized-change status).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import skill_runtime_exec as sre  # noqa: E402

WRITER = REPO_ROOT / "scripts" / "session-recording" / "write-codex-session-manifest.mjs"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "codex"
_ISSUE_NUMBER = "999999"

_FIXTURE_MANIFEST = {
    "schema": "agent_session_manifest/v1",
    "manifest_id": "asm-ac3-fixture-0000",
    "recorded_at": "2026-01-01T00:00:00.000Z",
    "repository": "squne121/loop-protocol",
    "actor": {"type": "ai_agent", "name": "codex-stop-hook"},
    "phase": {
        "main_loop": "impl",
        "phase_instance_id": "issue-1546:impl:001",
        "ledger_phase": "post_commit_verification",
    },
    "token_usage": {
        "availability": "unavailable",
        "source": "none",
        "prompt": None,
        "completion": None,
        "total": None,
    },
    "evidence": [
        {
            "source_kind": "artifact",
            "source_ref": "stop/ac3-fixture.json",
            "visibility": "private_artifact",
        }
    ],
    "producer": {
        "kind": "script_generated",
        "version": None,
        "command": "node scripts/generate-session-manifest.mjs",
        "source_ref": None,
    },
    "redaction": {
        "raw_transcript_included": False,
        "local_paths_included": False,
        "secret_scan_status": "clean",
    },
    "human_intervention": {"required": False, "type": "none", "summary": None},
    "secret_policy": {
        "value_exposed": False,
        "mode": "presence_only",
        "producer_contract": {
            "declared": True,
            "id": "presence_only_no_secret_values",
            "version": "v1",
            "claims": {"secret_values_not_serialized": True, "presence_only": True},
        },
        "runtime_boundary": {"attested": True, "evidence_ref": "stop/ac3-fixture.json"},
    },
}


def _run_writer_harness(state_home: Path) -> subprocess.CompletedProcess[str]:
    """AC3 Node harness: invoke writeCodexSessionManifest() directly with a
    fixture manifest, publishing to the canonical external per-user state
    root under an isolated XDG_STATE_HOME. Mirrors the harness pattern used
    by tests/session_recording/codex/test_external_manifest_publish.py's
    AC6 test (writer invoked directly via a generated .mjs script), so AC3
    needs neither the adapter's producer subprocess nor ajv on disk."""
    script = state_home.parent / "ac3_harness.mjs"
    script.write_text(
        "import { writeCodexSessionManifest } from " + json.dumps(str(WRITER)) + "\n"
        "const manifest = " + json.dumps(_FIXTURE_MANIFEST) + "\n"
        "const result = writeCodexSessionManifest({\n"
        "  manifest,\n"
        "  repoRoot: " + json.dumps(str(REPO_ROOT)) + ",\n"
        "  eventName: 'Stop',\n"
        "  env: { XDG_STATE_HOME: " + json.dumps(str(state_home)) + " },\n"
        "})\n"
        "process.stdout.write(JSON.stringify(result))\n",
        encoding="utf-8",
    )
    return subprocess.run(
        ["node", str(script)],
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
        check=False,
    )


def test_external_session_manifest_peer_write_does_not_stop_preflight(tmp_path: Path) -> None:
    """AC3: an independent peer writer publishing a fixture manifest to the
    canonical external per-user state root (isolated under a tmp
    XDG_STATE_HOME for this test) is invisible to the executor's repo-tree
    diff -- it never produces an unauthorized_write_path false positive."""
    before_snapshot = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
    before_status = sre._git_status_paths(str(REPO_ROOT))

    state_home = tmp_path / "state"
    result = _run_writer_harness(state_home)
    assert result.returncode == 0, result.stderr

    manifests = list(
        state_home.glob("loop-protocol/session-manifests/v1/*/codex/stop/*.json")
    )
    assert len(manifests) == 1, manifests
    written = json.loads(manifests[0].read_text())
    assert written == _FIXTURE_MANIFEST

    unauthorized = sre._find_unauthorized_repo_changes(
        str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot, before_status
    )
    assert unauthorized is None


def test_ac10_runtime_evidence_artifact_is_captured(tmp_path: Path) -> None:
    """AC10: capture OS/Node/Python version, the external manifest's
    SHA-256 and evidence locator, the harness executor's exit code, and the
    before/after unauthorized-repo-change status as a runtime evidence
    artifact."""
    before_snapshot = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
    before_status = sre._git_status_paths(str(REPO_ROOT))
    before_unauthorized = sre._find_unauthorized_repo_changes(
        str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot, before_status
    )

    state_home = tmp_path / "state"
    result = _run_writer_harness(state_home)
    assert result.returncode == 0, result.stderr

    manifests = list(
        state_home.glob("loop-protocol/session-manifests/v1/*/codex/stop/*.json")
    )
    assert len(manifests) == 1, manifests
    manifest_bytes = manifests[0].read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

    after_snapshot = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
    after_status = sre._git_status_paths(str(REPO_ROOT))
    after_unauthorized = sre._find_unauthorized_repo_changes(
        str(REPO_ROOT), _ISSUE_NUMBER, after_snapshot, after_status
    )

    node_version = subprocess.run(
        ["node", "--version"], text=True, capture_output=True, check=True
    ).stdout.strip()

    evidence = {
        "schema": "session_manifest_ac10_runtime_evidence/v1",
        "issue": 1546,
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "node_version": node_version,
        "python_version": platform.python_version(),
        "manifest_locator": str(manifests[0].relative_to(state_home)),
        "manifest_sha256": manifest_sha256,
        "executor_exit_code": result.returncode,
        "before_unauthorized": before_unauthorized,
        "after_unauthorized": after_unauthorized,
    }
    assert evidence["before_unauthorized"] is None
    assert evidence["after_unauthorized"] is None
    assert evidence["executor_exit_code"] == 0
    assert len(evidence["manifest_sha256"]) == 64

    artifact_dir = REPO_ROOT / "artifacts" / "issue-1546"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "ac10_runtime_evidence.json"
    artifact_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


def test_repo_local_session_manifest_write_is_rejected(tmp_path: Path) -> None:
    """AC5: an independent peer's plain repo-local
    tmp/session-manifests/codex/** create fails closed via the existing
    generic postcondition (no session-manifest-specific allowlist)."""
    target_dir = REPO_ROOT / "tmp" / "session-manifests" / "codex" / "stop"
    target_dir.mkdir(parents=True, exist_ok=True)
    dir_preexisted = True
    if not any(target_dir.parent.iterdir()) and not target_dir.exists():
        dir_preexisted = False

    before_snapshot = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
    before_status = sre._git_status_paths(str(REPO_ROOT))

    rogue_file = target_dir / "rogue-peer-write.json"
    try:
        rogue_file.write_text(json.dumps({"rogue": True}))

        unauthorized = sre._find_unauthorized_repo_changes(
            str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot, before_status
        )
        assert unauthorized is not None, "expected the repo-local peer write to fail closed"
    finally:
        rogue_file.unlink(missing_ok=True)
        if not dir_preexisted:
            shutil.rmtree(REPO_ROOT / "tmp" / "session-manifests", ignore_errors=True)


def test_repo_local_session_manifest_nonregular_and_sibling_writes_are_rejected(tmp_path: Path) -> None:
    """AC5: non-regular (symlink) and sibling repo-local
    tmp/session-manifests/codex/** writes also fail closed via the existing
    generic postcondition."""
    target_dir = REPO_ROOT / "tmp" / "session-manifests" / "codex" / "stop"
    dir_preexisted_before_test = (REPO_ROOT / "tmp" / "session-manifests").exists()
    target_dir.mkdir(parents=True, exist_ok=True)

    before_snapshot = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
    before_status = sre._git_status_paths(str(REPO_ROOT))

    symlink_path = target_dir / "rogue-symlink.json"
    sibling_path = target_dir.parent / "rogue-sibling.json"
    try:
        symlink_path.symlink_to(REPO_ROOT / "README.md")

        unauthorized_symlink = sre._find_unauthorized_repo_changes(
            str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot, before_status
        )
        assert unauthorized_symlink is not None, "expected symlink substitution to fail closed"
        symlink_path.unlink(missing_ok=True)

        before_snapshot_2 = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
        before_status_2 = sre._git_status_paths(str(REPO_ROOT))
        sibling_path.write_text(json.dumps({"sibling": True}))

        unauthorized_sibling = sre._find_unauthorized_repo_changes(
            str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot_2, before_status_2
        )
        assert unauthorized_sibling is not None, "expected sibling write to fail closed"
    finally:
        symlink_path.unlink(missing_ok=True)
        sibling_path.unlink(missing_ok=True)
        if not dir_preexisted_before_test:
            shutil.rmtree(REPO_ROOT / "tmp" / "session-manifests", ignore_errors=True)


def test_repo_local_session_manifest_update_and_delete_are_rejected(tmp_path: Path) -> None:
    """AC5: an independent peer's *update* of an existing repo-local
    tmp/session-manifests/codex/** file's content, and its outright
    *deletion*, both fail closed via the existing generic postcondition."""
    target_dir = REPO_ROOT / "tmp" / "session-manifests" / "codex" / "stop"
    dir_preexisted_before_test = (REPO_ROOT / "tmp" / "session-manifests").exists()
    target_dir.mkdir(parents=True, exist_ok=True)

    baseline_file = target_dir / "baseline-manifest.json"
    baseline_file.write_text(json.dumps({"v": 1}))
    try:
        before_snapshot = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
        before_status = sre._git_status_paths(str(REPO_ROOT))

        # Update: overwrite the tracked-baseline file's content in place.
        baseline_file.write_text(json.dumps({"v": 2}))
        unauthorized_update = sre._find_unauthorized_repo_changes(
            str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot, before_status
        )
        assert unauthorized_update is not None, "expected in-place content update to fail closed"

        # Delete: restore the baseline snapshot, then remove the file
        # outright and confirm the deletion itself fails closed.
        baseline_file.write_text(json.dumps({"v": 1}))
        before_snapshot_2 = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
        before_status_2 = sre._git_status_paths(str(REPO_ROOT))
        baseline_file.unlink()

        unauthorized_delete = sre._find_unauthorized_repo_changes(
            str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot_2, before_status_2
        )
        assert unauthorized_delete is not None, "expected deletion to fail closed"
    finally:
        baseline_file.unlink(missing_ok=True)
        if not dir_preexisted_before_test:
            shutil.rmtree(REPO_ROOT / "tmp" / "session-manifests", ignore_errors=True)


def test_repo_local_session_manifest_directory_fifo_socket_writes_are_rejected(tmp_path: Path) -> None:
    """AC5: an independent peer substituting a repo-local
    tmp/session-manifests/codex/** entry with a directory, a FIFO, or a Unix
    domain socket (instead of a plain file) also fails closed via the
    existing generic postcondition -- no non-regular kind is special-cased
    as authorized."""
    target_dir = REPO_ROOT / "tmp" / "session-manifests" / "codex" / "stop"
    dir_preexisted_before_test = (REPO_ROOT / "tmp" / "session-manifests").exists()
    target_dir.mkdir(parents=True, exist_ok=True)

    directory_path = target_dir / "rogue-directory.json"
    fifo_path = target_dir / "rogue-fifo.json"
    socket_path = target_dir / "rogue-socket.json"
    sock = None
    try:
        before_snapshot = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
        before_status = sre._git_status_paths(str(REPO_ROOT))
        directory_path.mkdir()
        unauthorized_dir = sre._find_unauthorized_repo_changes(
            str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot, before_status
        )
        assert unauthorized_dir is not None, "expected directory substitution to fail closed"
        directory_path.rmdir()

        before_snapshot_2 = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
        before_status_2 = sre._git_status_paths(str(REPO_ROOT))
        os.mkfifo(fifo_path)
        unauthorized_fifo = sre._find_unauthorized_repo_changes(
            str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot_2, before_status_2
        )
        assert unauthorized_fifo is not None, "expected FIFO substitution to fail closed"
        fifo_path.unlink()

        before_snapshot_3 = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
        before_status_3 = sre._git_status_paths(str(REPO_ROOT))
        # AF_UNIX bind() enforces a short OS-level path length limit (~108
        # bytes on Linux) that the deep repo path can exceed, so bind at a
        # short tmp_path location first, then move the resulting socket
        # special file into the repo target via os.replace (a directory
        # rename, not a bind-time path operation, so it is not subject to
        # the same length limit).
        short_socket_path = tmp_path / "s.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(short_socket_path))
        os.replace(short_socket_path, socket_path)
        unauthorized_socket = sre._find_unauthorized_repo_changes(
            str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot_3, before_status_3
        )
        assert unauthorized_socket is not None, "expected Unix domain socket substitution to fail closed"
    finally:
        if sock is not None:
            sock.close()
        if directory_path.is_dir():
            directory_path.rmdir()
        fifo_path.unlink(missing_ok=True)
        socket_path.unlink(missing_ok=True)
        if not dir_preexisted_before_test:
            shutil.rmtree(REPO_ROOT / "tmp" / "session-manifests", ignore_errors=True)


def test_repo_local_session_manifest_ancestor_replacement_is_rejected(tmp_path: Path) -> None:
    """AC5: replacing an *ancestor directory* of the repo-local
    tmp/session-manifests/** tree (not just a leaf file inside it) with a
    symlink also fails closed via the existing generic postcondition."""
    manifests_root = REPO_ROOT / "tmp" / "session-manifests"
    root_preexisted_before_test = manifests_root.exists()
    codex_dir = manifests_root / "codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "stop").mkdir(parents=True, exist_ok=True)

    before_snapshot = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
    before_status = sre._git_status_paths(str(REPO_ROOT))

    try:
        shutil.rmtree(codex_dir)
        os.symlink(REPO_ROOT, codex_dir)

        unauthorized_ancestor = sre._find_unauthorized_repo_changes(
            str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot, before_status
        )
        assert unauthorized_ancestor is not None, "expected ancestor-directory replacement to fail closed"
    finally:
        if codex_dir.is_symlink():
            codex_dir.unlink()
        if root_preexisted_before_test:
            (codex_dir).mkdir(parents=True, exist_ok=True)
            (codex_dir / "stop").mkdir(parents=True, exist_ok=True)
        else:
            shutil.rmtree(manifests_root, ignore_errors=True)
