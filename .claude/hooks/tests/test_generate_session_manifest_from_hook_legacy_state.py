#!/usr/bin/env python3
"""
Legacy-state diagnostic tests for generate_session_manifest_from_hook.mjs
(Issue #1430).

Verifies that startup detection of pre-#1426-cutover legacy runtime state
written directly under the repo-root artifacts/ directory (the old producer
layout: `.lock-<32hex>`, `.tmp-<uuid>[.failed]`,
`private-agent-session-manifest-*.json`) emits a fixed-schema
`SESSION_MANIFEST_LEGACY_STATE_V1=<JSON>` diagnostic on stderr without
fail-closing the hook, and that the matcher does not over-match unrelated
scratch files that merely resemble the legacy naming convention.

Isolation: each test points CLAUDE_PROJECT_DIR at a synthetic tmp "repo
root" so legacy-path detection (anchored to REPO_ROOT, independent of the
current-layout SESSION_MANIFEST_ARTIFACTS_DIR override) never touches this
checkout's own artifacts/ directory.

AC coverage:
  AC3  `.lock-<32hex>` / `.tmp-<uuid>` present -> legacy_kind=producer_lock_tmp.
  AC4  root-level `private-agent-session-manifest-*.json` present ->
       legacy_kind=producer_root_manifest.
  AC7  near-miss filenames that do not match the exact legacy grammar do not
       produce a diagnostic (matcher over-match guard).
"""

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
)

HOOK_WRAPPER_PATH = REPO_ROOT / ".claude" / "hooks" / "generate_session_manifest_from_hook.mjs"
PRODUCER_SCRIPT_PATH = REPO_ROOT / "scripts" / "generate-session-manifest.mjs"

LEGACY_DIAGNOSTIC_PREFIX = "SESSION_MANIFEST_LEGACY_STATE_V1="


def make_env(project_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    # Route current-layout artifact writes elsewhere so the producer's own
    # manifest/lock/tmp bookkeeping never collides with (or masks) the
    # synthetic legacy fixtures placed directly under project_root/artifacts.
    env["SESSION_MANIFEST_ARTIFACTS_DIR"] = str(project_root / "current-runtime")
    env["SESSION_MANIFEST_PRODUCER_SCRIPT"] = str(PRODUCER_SCRIPT_PATH)
    return env


def run_wrapper(project_root: Path) -> "subprocess.CompletedProcess[str]":
    payload = {
        "hook_event_name": "Stop",
        "cwd": str(project_root),
        "session_id": "legacy-state-test",
    }
    return subprocess.run(
        ["node", str(HOOK_WRAPPER_PATH)],
        cwd=REPO_ROOT,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=make_env(project_root),
        check=False,
        timeout=30,
    )


def extract_legacy_diagnostics(stderr: str) -> list[dict]:
    diagnostics = []
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith(LEGACY_DIAGNOSTIC_PREFIX):
            diagnostics.append(json.loads(stripped[len(LEGACY_DIAGNOSTIC_PREFIX) :]))
    return diagnostics


# ---------------------------------------------------------------------------
# AC3: legacy `.lock-<32hex>` / `.tmp-<uuid>` files
# ---------------------------------------------------------------------------


def test_legacy_lock_tmp_diagnostic(tmp_path: Path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True)

    stable_key = hashlib.sha256(b"legacy-lock-fixture").hexdigest()[:32]
    lock_name = f".lock-{stable_key}"
    (artifacts_dir / lock_name).write_text("", encoding="utf-8")

    tmp_uuid = str(uuid.uuid4())
    tmp_name = f".tmp-{tmp_uuid}"
    (artifacts_dir / tmp_name).write_text("{}", encoding="utf-8")

    result = run_wrapper(tmp_path)

    assert result.returncode == 0, f"hook fail-closed (non-fail-close violated): {result.stderr}"
    diagnostics = extract_legacy_diagnostics(result.stderr)
    matching = [d for d in diagnostics if d.get("legacy_kind") == "producer_lock_tmp"]
    assert matching, f"expected producer_lock_tmp diagnostic, got: {diagnostics}"
    diagnostic = matching[0]
    assert diagnostic["status"] == "legacy_state_detected"
    paths = diagnostic["paths"]
    assert any(lock_name in p for p in paths), f"lock file missing from paths: {paths}"
    assert any(tmp_name in p for p in paths), f"tmp file missing from paths: {paths}"


def test_legacy_tmp_failed_suffix_is_matched(tmp_path: Path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True)

    tmp_uuid = str(uuid.uuid4())
    failed_name = f".tmp-{tmp_uuid}.failed"
    (artifacts_dir / failed_name).write_text("{}", encoding="utf-8")

    result = run_wrapper(tmp_path)

    assert result.returncode == 0
    diagnostics = extract_legacy_diagnostics(result.stderr)
    matching = [d for d in diagnostics if d.get("legacy_kind") == "producer_lock_tmp"]
    assert matching, f"expected producer_lock_tmp diagnostic for .failed tmp file, got: {diagnostics}"
    assert any(failed_name in p for p in matching[0]["paths"])


# ---------------------------------------------------------------------------
# AC4: legacy root-level manifest
# ---------------------------------------------------------------------------


def test_legacy_root_manifest_diagnostic(tmp_path: Path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True)

    stable_key = hashlib.sha256(b"legacy-manifest-fixture").hexdigest()[:32]
    manifest_name = f"private-agent-session-manifest-stop-{int(time.time() * 1000)}-{stable_key}.json"
    (artifacts_dir / manifest_name).write_text("{}", encoding="utf-8")

    result = run_wrapper(tmp_path)

    assert result.returncode == 0, f"hook fail-closed (non-fail-close violated): {result.stderr}"
    diagnostics = extract_legacy_diagnostics(result.stderr)
    matching = [d for d in diagnostics if d.get("legacy_kind") == "producer_root_manifest"]
    assert matching, f"expected producer_root_manifest diagnostic, got: {diagnostics}"
    diagnostic = matching[0]
    assert diagnostic["status"] == "legacy_state_detected"
    assert any(manifest_name in p for p in diagnostic["paths"])


# ---------------------------------------------------------------------------
# AC7: matcher does not over-match unrelated scratch files
# ---------------------------------------------------------------------------


def test_legacy_matcher_does_not_overmatch_unrelated_scratch_files(tmp_path: Path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True)

    # Unrelated scratch prefix used by other skills -- must never match.
    (artifacts_dir / "some-other-prefix-12345.tmp").write_text("", encoding="utf-8")
    # Malformed stable key (not 32 hex chars).
    (artifacts_dir / ".lock-not-hex").write_text("", encoding="utf-8")
    # Malformed UUID suffix.
    (artifacts_dir / ".tmp-not-a-uuid").write_text("", encoding="utf-8")
    # Manifest-like name with non-digit timestamp and non-hex key segment.
    (artifacts_dir / "private-agent-session-manifest-stop-abc-notahex.json").write_text(
        "{}", encoding="utf-8"
    )
    # New-layout subtree directory (never a file match for either grammar).
    (artifacts_dir / "session-manifest-runtime" / "manifests").mkdir(parents=True)

    result = run_wrapper(tmp_path)

    assert result.returncode == 0
    diagnostics = extract_legacy_diagnostics(result.stderr)
    assert diagnostics == [], f"unexpected legacy diagnostic for near-miss files: {diagnostics}"


def test_no_legacy_state_no_diagnostic_when_artifacts_dir_absent(tmp_path: Path):
    result = run_wrapper(tmp_path)

    assert result.returncode == 0
    diagnostics = extract_legacy_diagnostics(result.stderr)
    assert diagnostics == [], f"unexpected legacy diagnostic when artifacts/ absent: {diagnostics}"
