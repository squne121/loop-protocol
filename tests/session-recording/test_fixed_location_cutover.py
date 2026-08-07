#!/usr/bin/env python3
"""Tests for the source-bound eligibility/readiness fixed private location
cutover from ``.claude/tmp/session-recording/`` to ``tmp/session-recording/``
(Issue #2004, follow-up to Issue #1995).

The three files under test move in lockstep and never dual-read the old
location:

- ``scripts/session-recording/bootstrap-source-bound-readiness.mjs``
  (readiness producer)
- ``.claude/scripts/check_session_recording_runtime_safety.py``
  (eligibility producer/loader)
- ``.claude/hooks/capture_scope_rollup_final_response.py``
  (readiness consumer; eligibility is loaded via the module above)
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
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

_SCRIPTS_DIR = REPO_ROOT / ".claude" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import check_session_recording_runtime_safety as srrs  # noqa: E402

BOOTSTRAP_SCRIPT = REPO_ROOT / "scripts" / "session-recording" / "bootstrap-source-bound-readiness.mjs"
PRODUCER_PATH = REPO_ROOT / ".claude" / "hooks" / "capture_scope_rollup_final_response.py"
_POLICY_PATH = REPO_ROOT / "docs" / "dev" / "session-recording-policy.md"
_SECRET_POLICY_PATH = REPO_ROOT / "docs" / "dev" / "secret-policy.md"
POLICY_DIGEST = f"sha256:{hashlib.sha256(_POLICY_PATH.read_bytes()).hexdigest()}"
SECRET_POLICY_DIGEST = f"sha256:{hashlib.sha256(_SECRET_POLICY_PATH.read_bytes()).hexdigest()}"
PRODUCER_DIGEST = f"sha256:{hashlib.sha256(PRODUCER_PATH.read_bytes()).hexdigest()}"

ELIGIBILITY_GENERATED_AT = "2026-06-15T11:00:00Z"
ELIGIBILITY_EXPIRES_AT = "2030-01-01T00:00:00Z"

_PLACEHOLDER_DIGEST_A = "sha256:" + ("0" * 64)
_PLACEHOLDER_DIGEST_B = "sha256:" + ("1" * 64)


def _render_marker(
    *,
    invocation_id: str = "inv-2004",
    requested_at: str | None = None,
    generated_at: str | None = None,
) -> str:
    # Default timestamps are relative to real wall-clock "now" (rather than
    # a fixed past date) so that markers built against a freshly regenerated
    # eligibility artifact (whose generated_at is also real wall-clock
    # "now") are never rejected as eligibility_stale_generated_after_marker.
    now = datetime.now(timezone.utc)
    if requested_at is None:
        requested_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if generated_at is None:
        generated_at = (now + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return """```yaml
ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1:
  status: ok
  schema_version: 1
  repo: squne121/loop-protocol
  current_issue: 2004
  invocation_id: {invocation_id}
  requested_at: {requested_at}
  generated_at: {generated_at}
  git_head_sha: 0000000000000000000000000000000000000000
  script_path: .claude/skills/issue-refinement-loop/scripts/plan_issue_scope_rollup.py
  script_blob_sha256: deadbeef
  result:
    plan_schema: ISSUE_SCOPE_ROLLUP_PLAN_V2
    raw_plan_location: /tmp/scope_rollup_{invocation_id}.json
    result_sha256: deadbeef
    verify_status: verified
    suggested_actions_summary: No action
    candidate_count: 0
    high_confidence_count: 0
```""".format(
        invocation_id=invocation_id,
        requested_at=requested_at,
        generated_at=generated_at,
    )


def _write_json_mode_0600(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _default_eligibility(**overrides: object) -> dict[str, object]:
    artifact = {
        "schema": "SESSION_RECORDING_SCOPE_ROLLUP_ELIGIBILITY_V1",
        "artifact_version": 1,
        "repo_root_realpath": str(REPO_ROOT.resolve()),
        "head_sha": None,
        "policy_digest": POLICY_DIGEST,
        "secret_policy_digest": SECRET_POLICY_DIGEST,
        "public_checkpoint_present": False,
        "visibility": "public",
        "secrets_mode": "none",
        "generated_at": ELIGIBILITY_GENERATED_AT,
        "expires_at": ELIGIBILITY_EXPIRES_AT,
        "safety_verdict": "allow",
    }
    artifact.update(overrides)
    return artifact


def _default_readiness(**overrides: object) -> dict[str, object]:
    artifact = {
        "schema": "SESSION_RECORDING_SCOPE_ROLLUP_READINESS_V1",
        "artifact_version": 1,
        "repo_root_realpath": str(REPO_ROOT.resolve()),
        "uv_lock_digest": None,
        "python_version_digest": None,
        "interpreter_realpath": sys.executable,
        "interpreter_version": "Python 3.x",
        "producer_digest": PRODUCER_DIGEST,
        "prepared": True,
        "generated_at": ELIGIBILITY_GENERATED_AT,
    }
    artifact.update(overrides)
    return artifact


def _read_capture_record(capture_dir: Path) -> dict[str, object]:
    import yaml

    records = list(capture_dir.glob("scope_rollup_*.capture.yaml"))
    assert len(records) == 1, f"expected exactly 1 capture record, found {len(records)}"
    parsed = yaml.safe_load(records[0].read_text(encoding="utf-8"))
    result = parsed["SCOPE_ROLLUP_CAPTURE_RESULT_V1"]
    assert isinstance(result, dict)
    return result


def _run_bootstrap_readiness(readiness_path: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["SCOPE_ROLLUP_READINESS_ARTIFACT_PATH"] = str(readiness_path)
    return subprocess.run(
        ["node", str(BOOTSTRAP_SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
    )


def _run_producer(
    payload: dict[str, object],
    *,
    capture_dir: Path,
    eligibility_path: Path,
    readiness_path: Path,
    repo_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "SCOPE_ROLLUP_CAPTURE_DIR": str(capture_dir),
        "SCOPE_ROLLUP_REQUIRE_SOURCE_BOUND_ELIGIBILITY": "1",
        "SCOPE_ROLLUP_REPO_ROOT": str((repo_root or REPO_ROOT).resolve()),
        "SCOPE_ROLLUP_ELIGIBILITY_ARTIFACT_PATH": str(eligibility_path),
        "SCOPE_ROLLUP_READINESS_ARTIFACT_PATH": str(readiness_path),
    }
    return subprocess.run(
        [sys.executable, str(PRODUCER_PATH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
    )


# ---------------------------------------------------------------------------
# AC1: both producers default to tmp/session-recording/
# ---------------------------------------------------------------------------


def test_default_fixed_location_producer_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCOPE_ROLLUP_ELIGIBILITY_ARTIFACT_PATH", raising=False)
    monkeypatch.delenv("SCOPE_ROLLUP_READINESS_ARTIFACT_PATH", raising=False)

    eligibility_default = srrs.scope_rollup_eligibility_artifact_path(REPO_ROOT)
    assert eligibility_default == REPO_ROOT / "tmp" / "session-recording" / "scope-rollup-eligibility.json"

    # Readiness default lives in the Node bootstrap script. Run it for real
    # (Runtime Verification Applicability: immediate) with the override
    # unset so it exercises its own default-path computation. Back up /
    # restore any real artifact a concurrent developer session may have
    # produced at the same default location.
    readiness_default = REPO_ROOT / "tmp" / "session-recording" / "scope-rollup-readiness.json"
    backup_bytes = readiness_default.read_bytes() if readiness_default.exists() else None
    existed_before = readiness_default.exists()
    try:
        if readiness_default.exists():
            readiness_default.unlink()
        env = dict(os.environ)
        env.pop("SCOPE_ROLLUP_READINESS_ARTIFACT_PATH", None)
        result = subprocess.run(
            ["node", str(BOOTSTRAP_SCRIPT)],
            text=True,
            capture_output=True,
            check=False,
            cwd=str(REPO_ROOT),
            env=env,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert readiness_default.exists()
        parsed = json.loads(readiness_default.read_text(encoding="utf-8"))
        assert parsed["prepared"] is True
    finally:
        if backup_bytes is not None:
            readiness_default.write_bytes(backup_bytes)
            readiness_default.chmod(0o600)
        elif not existed_before and readiness_default.exists():
            readiness_default.unlink()


# ---------------------------------------------------------------------------
# AC3: parent directory 0700, artifact 0600, non-symlink, owner match
# ---------------------------------------------------------------------------


def test_new_private_directory_and_artifact_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Eligibility (Python producer): emit into a nested, not-yet-existing
    # directory to prove the producer itself creates it with mode 0700.
    eligibility_path = tmp_path / "eligibility-root" / "session-recording" / "scope-rollup-eligibility.json"
    monkeypatch.setenv("SCOPE_ROLLUP_ELIGIBILITY_ARTIFACT_PATH", str(eligibility_path))
    monkeypatch.setenv("SRRS_SECRETS_MODE", "none")
    monkeypatch.setenv("SRRS_GIT_LS_REMOTE_EXIT", "2")  # 2 == branch absent (safe)
    monkeypatch.setenv("SRRS_GH_VISIBILITY", "public")

    exit_code, artifact = srrs.emit_scope_rollup_eligibility_artifact(REPO_ROOT)
    assert exit_code == srrs.EXIT_PASS
    assert artifact["safety_verdict"] == "allow"

    parent_stat = os.lstat(eligibility_path.parent)
    assert not stat.S_ISLNK(parent_stat.st_mode)
    assert stat.S_ISDIR(parent_stat.st_mode)
    assert stat.S_IMODE(parent_stat.st_mode) == 0o700
    assert parent_stat.st_uid == os.getuid()

    file_stat = os.lstat(eligibility_path)
    assert not stat.S_ISLNK(file_stat.st_mode)
    assert stat.S_ISREG(file_stat.st_mode)
    assert stat.S_IMODE(file_stat.st_mode) == 0o600

    # Readiness (Node producer): same directory-mode contract via
    # SCOPE_ROLLUP_READINESS_ARTIFACT_PATH override into an isolated,
    # not-yet-existing directory.
    readiness_path = tmp_path / "readiness-root" / "session-recording" / "scope-rollup-readiness.json"
    result = _run_bootstrap_readiness(readiness_path)
    assert result.returncode == 0, result.stderr

    r_parent_stat = os.lstat(readiness_path.parent)
    assert not stat.S_ISLNK(r_parent_stat.st_mode)
    assert stat.S_ISDIR(r_parent_stat.st_mode)
    assert stat.S_IMODE(r_parent_stat.st_mode) == 0o700
    assert r_parent_stat.st_uid == os.getuid()

    r_file_stat = os.lstat(readiness_path)
    assert not stat.S_ISLNK(r_file_stat.st_mode)
    assert stat.S_ISREG(r_file_stat.st_mode)
    assert stat.S_IMODE(r_file_stat.st_mode) == 0o600


# ---------------------------------------------------------------------------
# AC4: env override is absolute-only or repo-root normalized
# ---------------------------------------------------------------------------


def test_env_override_repo_root_normalized(tmp_path: Path) -> None:
    # Absolute override: used as-is regardless of cwd.
    absolute_target = tmp_path / "abs" / "eligibility.json"
    resolved = srrs.resolve_session_recording_artifact_override(str(absolute_target), REPO_ROOT)
    assert resolved == absolute_target

    # Relative override: resolved against repo_root, never the process cwd —
    # verify by calling with a repo_root argument that differs from the real
    # cwd and confirming the relative path is anchored there, not to cwd.
    relative_override = "tmp/issue-2004-relative-check/eligibility.json"
    fake_repo_root = tmp_path / "fake-repo"
    fake_repo_root.mkdir()
    resolved_relative = srrs.resolve_session_recording_artifact_override(relative_override, fake_repo_root)
    assert resolved_relative == (fake_repo_root / relative_override).resolve()
    assert resolved_relative != (Path.cwd() / relative_override).resolve()

    # End-to-end: the readiness (Node) producer resolves a relative override
    # against repoRoot even when invoked from an unrelated cwd.
    other_cwd = tmp_path / "unrelated-cwd"
    other_cwd.mkdir()
    relative_readiness = "tmp/issue-2004-readiness-relative-check/scope-rollup-readiness.json"
    env = dict(os.environ)
    env["SCOPE_ROLLUP_READINESS_ARTIFACT_PATH"] = relative_readiness
    result = subprocess.run(
        ["node", str(BOOTSTRAP_SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
        cwd=str(other_cwd),
        env=env,
        timeout=120,
    )
    expected_path = REPO_ROOT / relative_readiness
    try:
        assert result.returncode == 0, result.stderr
        assert expected_path.exists()
        assert not (other_cwd / relative_readiness).exists()
    finally:
        if expected_path.exists():
            expected_path.unlink()
        parent = expected_path.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()


# ---------------------------------------------------------------------------
# AC5: producer-consumer round trip via the new fixed location — both
# eligibility and readiness reason codes are "ok"
# ---------------------------------------------------------------------------


def test_source_bound_default_path_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eligibility_path = tmp_path / "session-recording" / "scope-rollup-eligibility.json"
    readiness_path = tmp_path / "session-recording" / "scope-rollup-readiness.json"

    monkeypatch.setenv("SCOPE_ROLLUP_ELIGIBILITY_ARTIFACT_PATH", str(eligibility_path))
    monkeypatch.setenv("SRRS_SECRETS_MODE", "none")
    monkeypatch.setenv("SRRS_GIT_LS_REMOTE_EXIT", "2")
    monkeypatch.setenv("SRRS_GH_VISIBILITY", "public")
    exit_code, _artifact = srrs.emit_scope_rollup_eligibility_artifact(REPO_ROOT)
    assert exit_code == srrs.EXIT_PASS

    bootstrap_result = _run_bootstrap_readiness(readiness_path)
    assert bootstrap_result.returncode == 0, bootstrap_result.stderr

    message = _render_marker()
    payload = {
        "hook_event_name": "SubagentStop",
        "agent_type": "scope-rollup-runner",
        "last_assistant_message": message,
        "stop_hook_active": False,
    }
    capture_dir = tmp_path / "capture"
    capture_dir.mkdir()
    result = _run_producer(
        payload,
        capture_dir=capture_dir,
        eligibility_path=eligibility_path,
        readiness_path=readiness_path,
    )
    assert result.returncode == 0, result.stderr

    record = _read_capture_record(capture_dir)
    provenance = record["provenance"]
    assert provenance["eligibility_verification_reason_code"] == "ok"
    assert provenance["readiness_verification_reason_code"] == "ok"
    assert record["parser_status"] == "ok"


# ---------------------------------------------------------------------------
# AC6: old-path-only artifacts are never consumed (no dual-read fallback)
# ---------------------------------------------------------------------------


def test_old_default_location_not_consumed(tmp_path: Path) -> None:
    fake_repo_root = tmp_path / "fake-repo"
    fake_repo_root.mkdir()
    # The eligibility binding check recomputes the policy/secret-policy
    # digest from repo_root/docs/dev/*.md — mirror those two files into the
    # isolated fixture repo root so _default_eligibility()'s digests (taken
    # from the real repo) remain self-consistent within this fixture.
    (fake_repo_root / "docs" / "dev").mkdir(parents=True)
    (fake_repo_root / "docs" / "dev" / "session-recording-policy.md").write_bytes(_POLICY_PATH.read_bytes())
    (fake_repo_root / "docs" / "dev" / "secret-policy.md").write_bytes(_SECRET_POLICY_PATH.read_bytes())

    old_eligibility_path = fake_repo_root / ".claude" / "tmp" / "session-recording" / "scope-rollup-eligibility.json"
    old_readiness_path = fake_repo_root / ".claude" / "tmp" / "session-recording" / "scope-rollup-readiness.json"
    _write_json_mode_0600(
        old_eligibility_path, _default_eligibility(repo_root_realpath=str(fake_repo_root.resolve()))
    )
    _write_json_mode_0600(
        old_readiness_path, _default_readiness(repo_root_realpath=str(fake_repo_root.resolve()))
    )

    # No SCOPE_ROLLUP_*_ARTIFACT_PATH override => default (new) path is used.
    # The fake, empty repo root means the new-path default has nothing there.
    message = _render_marker(invocation_id="inv-2004-oldpath")
    payload = {
        "hook_event_name": "SubagentStop",
        "agent_type": "scope-rollup-runner",
        "last_assistant_message": message,
        "stop_hook_active": False,
    }
    capture_dir = fake_repo_root / "capture"
    capture_dir.mkdir()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "SCOPE_ROLLUP_CAPTURE_DIR": str(capture_dir),
        "SCOPE_ROLLUP_REQUIRE_SOURCE_BOUND_ELIGIBILITY": "1",
        "SCOPE_ROLLUP_REPO_ROOT": str(fake_repo_root.resolve()),
    }
    result = subprocess.run(
        [sys.executable, str(PRODUCER_PATH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert result.returncode == 0, result.stderr
    record = _read_capture_record(capture_dir)
    assert record["parser_status"] == "eligibility_missing"
    assert not any(capture_dir.glob("scope_rollup_*.txt"))

    # Second case: eligibility exists at the NEW path (so it passes), but
    # readiness exists only at the OLD path => readiness_missing.
    new_eligibility_path = fake_repo_root / "tmp" / "session-recording" / "scope-rollup-eligibility.json"
    _write_json_mode_0600(
        new_eligibility_path, _default_eligibility(repo_root_realpath=str(fake_repo_root.resolve()))
    )

    capture_dir_2 = fake_repo_root / "capture2"
    capture_dir_2.mkdir()
    message_2 = _render_marker(invocation_id="inv-2004-oldpath-readiness")
    payload_2 = dict(payload, last_assistant_message=message_2)
    env_2 = dict(env, SCOPE_ROLLUP_CAPTURE_DIR=str(capture_dir_2))
    result_2 = subprocess.run(
        [sys.executable, str(PRODUCER_PATH)],
        input=json.dumps(payload_2),
        text=True,
        capture_output=True,
        check=False,
        cwd=str(REPO_ROOT),
        env=env_2,
    )
    assert result_2.returncode == 0, result_2.stderr
    record_2 = _read_capture_record(capture_dir_2)
    assert record_2["parser_status"] == "readiness_missing"
    assert not any(capture_dir_2.glob("scope_rollup_*.txt"))


# ---------------------------------------------------------------------------
# AC7: digest-mismatch -> fail-closed -> regeneration in the documented
# order (eligibility, then readiness) -> ok
# ---------------------------------------------------------------------------


def test_digest_update_regeneration_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eligibility_path = tmp_path / "session-recording" / "scope-rollup-eligibility.json"
    readiness_path = tmp_path / "session-recording" / "scope-rollup-readiness.json"

    # Step 0: stale eligibility (wrong policy_digest, simulating a
    # docs/dev/session-recording-policy.md edit that has not yet been
    # followed by eligibility regeneration) + a valid readiness artifact.
    _write_json_mode_0600(eligibility_path, _default_eligibility(policy_digest=_PLACEHOLDER_DIGEST_A))

    def _run(readiness: dict[str, object], invocation_id: str, capture_dir: Path) -> dict[str, object]:
        capture_dir.mkdir(parents=True, exist_ok=True)
        _write_json_mode_0600(readiness_path, readiness)
        message = _render_marker(invocation_id=invocation_id)
        payload = {
            "hook_event_name": "SubagentStop",
            "agent_type": "scope-rollup-runner",
            "last_assistant_message": message,
            "stop_hook_active": False,
        }
        result = _run_producer(
            payload,
            capture_dir=capture_dir,
            eligibility_path=eligibility_path,
            readiness_path=readiness_path,
        )
        assert result.returncode == 0, result.stderr
        return _read_capture_record(capture_dir)

    record = _run(_default_readiness(), "inv-2004-ac7-stale-eligibility", tmp_path / "c1")
    assert record["parser_status"] == "eligibility_binding_policy_digest_mismatch"

    # Step 1 (documented order): regenerate eligibility first.
    monkeypatch.setenv("SCOPE_ROLLUP_ELIGIBILITY_ARTIFACT_PATH", str(eligibility_path))
    monkeypatch.setenv("SRRS_SECRETS_MODE", "none")
    monkeypatch.setenv("SRRS_GIT_LS_REMOTE_EXIT", "2")
    monkeypatch.setenv("SRRS_GH_VISIBILITY", "public")
    exit_code, _artifact = srrs.emit_scope_rollup_eligibility_artifact(REPO_ROOT)
    assert exit_code == srrs.EXIT_PASS

    # readiness is still stale (wrong producer_digest simulating a
    # capture_scope_rollup_final_response.py edit not yet followed by a
    # readiness rebuild).
    record = _run(
        _default_readiness(producer_digest=_PLACEHOLDER_DIGEST_B),
        "inv-2004-ac7-stale-readiness",
        tmp_path / "c2",
    )
    assert record["parser_status"] == "readiness_binding_producer_digest_mismatch"

    # Step 2 (documented order): regenerate readiness via the real bootstrap
    # producer, which recomputes the current producer_digest.
    bootstrap_result = _run_bootstrap_readiness(readiness_path)
    assert bootstrap_result.returncode == 0, bootstrap_result.stderr

    capture_dir_3 = tmp_path / "c3"
    capture_dir_3.mkdir()
    message = _render_marker(invocation_id="inv-2004-ac7-ok")
    payload = {
        "hook_event_name": "SubagentStop",
        "agent_type": "scope-rollup-runner",
        "last_assistant_message": message,
        "stop_hook_active": False,
    }
    result = _run_producer(
        payload,
        capture_dir=capture_dir_3,
        eligibility_path=eligibility_path,
        readiness_path=readiness_path,
    )
    assert result.returncode == 0, result.stderr
    final_record = _read_capture_record(capture_dir_3)
    assert final_record["parser_status"] == "ok"
    assert final_record["provenance"]["eligibility_verification_reason_code"] == "ok"
    assert final_record["provenance"]["readiness_verification_reason_code"] == "ok"
