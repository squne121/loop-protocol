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
  AC9  the `paths` array is bound to a fixed number of entries with
       `total_count` / `truncated` fields when the legacy residue exceeds the
       bound.
  AC11 the UUID matcher enforces the RFC4122 v4 grammar (version nibble `4`,
       variant nibble `8|9|a|b`) and the root-manifest matcher restricts the
       hook-name segment to the known hook-event vocabulary.
  AC12 directory/symlink entries masquerading under a legacy filename are not
       misclassified as legacy residue, and an fs scan error is reported as a
       distinct `scan_failed` diagnostic rather than silently treated as
       "absent".
"""

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

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
LEGACY_SCAN_FAILURE_PREFIX = "SESSION_MANIFEST_LEGACY_SCAN_V1="


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
            diagnostics.append(json.loads(stripped[len(LEGACY_DIAGNOSTIC_PREFIX):]))
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


# ---------------------------------------------------------------------------
# AC9: paths field is bounded, with total_count / truncated
# ---------------------------------------------------------------------------


def test_paths_field_is_bounded_with_total_count_and_truncated(tmp_path: Path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True)

    # 25 legacy lock files -- exceeds the fixed bound of 20 (Issue #1430 AC9).
    lock_names = []
    for i in range(25):
        stable_key = hashlib.sha256(f"legacy-lock-bound-{i}".encode("utf-8")).hexdigest()[:32]
        lock_name = f".lock-{stable_key}"
        (artifacts_dir / lock_name).write_text("", encoding="utf-8")
        lock_names.append(lock_name)

    result = run_wrapper(tmp_path)

    assert result.returncode == 0, f"hook fail-closed (non-fail-close violated): {result.stderr}"
    diagnostics = extract_legacy_diagnostics(result.stderr)
    matching = [d for d in diagnostics if d.get("legacy_kind") == "producer_lock_tmp"]
    assert matching, f"expected producer_lock_tmp diagnostic, got: {diagnostics}"
    diagnostic = matching[0]

    assert diagnostic["total_count"] == 25, f"expected total_count=25, got: {diagnostic}"
    assert diagnostic["truncated"] is True, f"expected truncated=true, got: {diagnostic}"
    assert len(diagnostic["paths"]) <= 20, (
        f"paths must be bounded to <= 20 entries, got {len(diagnostic['paths'])}: {diagnostic}"
    )
    assert len(diagnostic["paths"]) == 20, f"expected exactly 20 bounded paths, got: {diagnostic}"


def test_paths_field_is_not_truncated_when_under_bound(tmp_path: Path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True)

    stable_key = hashlib.sha256(b"legacy-lock-under-bound").hexdigest()[:32]
    (artifacts_dir / f".lock-{stable_key}").write_text("", encoding="utf-8")

    result = run_wrapper(tmp_path)

    assert result.returncode == 0
    diagnostics = extract_legacy_diagnostics(result.stderr)
    matching = [d for d in diagnostics if d.get("legacy_kind") == "producer_lock_tmp"]
    assert matching
    diagnostic = matching[0]
    assert diagnostic["total_count"] == 1
    assert diagnostic["truncated"] is False


# ---------------------------------------------------------------------------
# AC11: strict RFC4122 v4 UUID grammar + known hook-name manifest grammar
# ---------------------------------------------------------------------------


def test_uuid_matcher_rejects_invalid_version_variant_near_miss(tmp_path: Path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True)

    # Version nibble '1' (must be '4') -- invalid, must NOT match.
    bad_version_uuid = "12345678-1234-1234-8234-123456789abc"
    (artifacts_dir / f".tmp-{bad_version_uuid}").write_text("{}", encoding="utf-8")

    # Variant nibble '0' (must be one of 8/9/a/b) -- invalid, must NOT match.
    bad_variant_uuid = "12345678-1234-4234-0234-123456789abc"
    (artifacts_dir / f".tmp-{bad_variant_uuid}").write_text("{}", encoding="utf-8")

    # Control: version=4, variant=8 -- valid RFC4122 v4 shape, must match.
    valid_uuid = "12345678-1234-4234-8234-123456789abc"
    (artifacts_dir / f".tmp-{valid_uuid}").write_text("{}", encoding="utf-8")

    # Root manifest with an unknown hook-event name segment -- must NOT match
    # (AC11: hook-name segment restricted to the known hook-event vocabulary).
    stable_key = hashlib.sha256(b"unknown-hook-manifest").hexdigest()[:32]
    unknown_hook_manifest = f"private-agent-session-manifest-unknownhook-{int(time.time() * 1000)}-{stable_key}.json"
    (artifacts_dir / unknown_hook_manifest).write_text("{}", encoding="utf-8")

    # Control: known hook-event name -- must match.
    known_stable_key = hashlib.sha256(b"known-hook-manifest").hexdigest()[:32]
    known_hook_manifest = f"private-agent-session-manifest-subagentstop-{int(time.time() * 1000)}-{known_stable_key}.json"
    (artifacts_dir / known_hook_manifest).write_text("{}", encoding="utf-8")

    result = run_wrapper(tmp_path)

    assert result.returncode == 0, f"hook fail-closed (non-fail-close violated): {result.stderr}"
    diagnostics = extract_legacy_diagnostics(result.stderr)

    lock_tmp = [d for d in diagnostics if d.get("legacy_kind") == "producer_lock_tmp"]
    assert lock_tmp, f"expected the valid uuid tmp file to match: {diagnostics}"
    matched_paths = lock_tmp[0]["paths"]
    assert any(valid_uuid in p for p in matched_paths), (
        f"valid RFC4122 v4 uuid should match: {matched_paths}"
    )
    assert not any(bad_version_uuid in p for p in matched_paths), (
        f"invalid version-nibble uuid must not match (AC11): {matched_paths}"
    )
    assert not any(bad_variant_uuid in p for p in matched_paths), (
        f"invalid variant-nibble uuid must not match (AC11): {matched_paths}"
    )

    manifests = [d for d in diagnostics if d.get("legacy_kind") == "producer_root_manifest"]
    assert manifests, f"expected the known-hook-name manifest to match: {diagnostics}"
    manifest_paths = manifests[0]["paths"]
    assert any(known_hook_manifest in p for p in manifest_paths), (
        f"known hook-name manifest should match: {manifest_paths}"
    )
    assert not any(unknown_hook_manifest in p for p in manifest_paths), (
        f"unknown hook-name manifest must not match root manifest matcher (AC11): {manifest_paths}"
    )


# ---------------------------------------------------------------------------
# AC12: file type distinction + fs scan error handling
# ---------------------------------------------------------------------------


def test_scan_distinguishes_file_type_and_fs_error_from_absence(tmp_path: Path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True)

    # A directory named like a legacy lock file -- must NOT be flagged.
    stable_key = hashlib.sha256(b"dir-masquerade-fixture").hexdigest()[:32]
    (artifacts_dir / f".lock-{stable_key}").mkdir()

    # A directory named like a legacy tmp file -- must NOT be flagged.
    tmp_uuid = str(uuid.uuid4())
    (artifacts_dir / f".tmp-{tmp_uuid}").mkdir()

    result = run_wrapper(tmp_path)

    assert result.returncode == 0, f"hook fail-closed (non-fail-close violated): {result.stderr}"
    diagnostics = extract_legacy_diagnostics(result.stderr)
    matching = [d for d in diagnostics if d.get("legacy_kind") == "producer_lock_tmp"]
    assert matching == [], (
        f"directories masquerading as legacy lock/tmp files must not be flagged (AC12): {matching}"
    )

    if os.geteuid() == 0:
        pytest.skip(
            "chmod-based permission-denial simulation is ineffective when running as root; "
            "file-type-distinction assertions above already cover AC12's primary claim."
        )

    # fs access error: artifacts/ itself becomes unreadable -- must be
    # reported as a distinct scan_failed diagnostic, not silently treated the
    # same as "legacy state absent".
    (artifacts_dir / "placeholder.txt").write_text("x", encoding="utf-8")
    artifacts_dir.chmod(0o000)
    try:
        error_result = run_wrapper(tmp_path)
    finally:
        artifacts_dir.chmod(0o755)

    assert error_result.returncode == 0, (
        f"fs scan error must not fail-close the hook (AC12): {error_result.stderr}"
    )
    assert LEGACY_SCAN_FAILURE_PREFIX in error_result.stderr, (
        f"expected a distinct scan_failed diagnostic on fs error, got: {error_result.stderr!r}"
    )
    error_diagnostics = extract_legacy_diagnostics(error_result.stderr)
    assert error_diagnostics == [], (
        f"fs scan error must not be misclassified as legacy_state_detected (AC12): {error_diagnostics}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
