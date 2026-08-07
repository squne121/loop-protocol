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
# Issue #2004 P3: computed relative to real wall-clock "now" (rather than a
# fixed calendar date) so this fixture never expires as real time passes.
# _load_and_verify_readiness_artifact()/eligibility lifecycle checks compare
# generated_at/expires_at against real wall-clock hook_received_at, so a
# hardcoded far-future date would eventually become a real past date.
ELIGIBILITY_EXPIRES_AT = (datetime.now(timezone.utc) + timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%SZ")

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


def test_default_fixed_location_producer_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCOPE_ROLLUP_ELIGIBILITY_ARTIFACT_PATH", raising=False)
    monkeypatch.delenv("SCOPE_ROLLUP_READINESS_ARTIFACT_PATH", raising=False)

    eligibility_default = srrs.scope_rollup_eligibility_artifact_path(REPO_ROOT)
    assert eligibility_default == REPO_ROOT / "tmp" / "session-recording" / "scope-rollup-eligibility.json"

    # Readiness default lives in the Node bootstrap script. Issue #2004 P2:
    # exercise the REAL bootstrap script's default-path computation against
    # an ISOLATED fake repo via its `--repo-root` test seam, rather than
    # mutating the live repo's canonical runtime artifact (the previous
    # implementation here unlinked/backed-up/restored the real
    # tmp/session-recording/scope-rollup-readiness.json, which raced against
    # any concurrent developer session using the same default location).
    # test_hermetic_default_path_producer_consumer_roundtrip below covers
    # the full producer-consumer round trip in the same isolated fashion.
    fake_repo_root = tmp_path / "fake-repo-default-path"
    fake_repo_root.mkdir()
    env = dict(os.environ)
    env.pop("SCOPE_ROLLUP_READINESS_ARTIFACT_PATH", None)
    result = subprocess.run(
        ["node", str(BOOTSTRAP_SCRIPT), "--repo-root", str(fake_repo_root)],
        text=True,
        capture_output=True,
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    readiness_default = fake_repo_root / "tmp" / "session-recording" / "scope-rollup-readiness.json"
    assert readiness_default.exists()
    parsed = json.loads(readiness_default.read_text(encoding="utf-8"))
    assert parsed["prepared"] is True
    assert parsed["repo_root_realpath"] == str(fake_repo_root.resolve())


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


# ---------------------------------------------------------------------------
# P1-1 (OWNER REQUEST_CHANGES on PR #2008): parent-directory non-symlink /
# owner-match hardening for the eligibility producer/loader (pure Python --
# no Node subprocess, stays in the python-test-core lane).
# ---------------------------------------------------------------------------


def test_eligibility_parent_dir_toctou_hardening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """reject_symlink_parent / reject_wrong_owner_parent /
    reject_or_repair_loose_parent_by_explicit_policy /
    reject_parent_replaced_after_validation -- all four via
    prepare_private_parent_dir() (the producer side), which used to do an
    unconditional mkdir(exist_ok=True) + chmod(parent, 0o700) with no
    verification at all.
    """
    monkeypatch.setenv("SRRS_SECRETS_MODE", "none")
    monkeypatch.setenv("SRRS_GIT_LS_REMOTE_EXIT", "2")
    monkeypatch.setenv("SRRS_GH_VISIBILITY", "public")

    # -- reject_symlink_parent: a pre-existing symlink parent must be
    # rejected outright, and the symlink's REAL target must never be
    # touched (no chmod through the link).
    real_target = tmp_path / "real-target-dir"
    real_target.mkdir()
    os.chmod(real_target, 0o755)
    symlinked_parent_artifact = tmp_path / "symlinked-parent" / "eligibility.json"
    os.symlink(real_target, symlinked_parent_artifact.parent)
    with pytest.raises(srrs.PrivateParentDirError) as excinfo:
        srrs.prepare_private_parent_dir(symlinked_parent_artifact)
    assert excinfo.value.reason_code == "parent_is_symlink"
    assert stat.S_IMODE(os.stat(real_target).st_mode) == 0o755  # untouched

    # -- reject_wrong_owner_parent: simulated via an injected expected_uid
    # (CI has no second real uid available) -- a real, non-symlink,
    # currently-existing directory whose owner does not match the caller's
    # expected uid must be rejected, never chmod'd.
    owner_mismatch_artifact = tmp_path / "owner-mismatch" / "eligibility.json"
    owner_mismatch_artifact.parent.mkdir(parents=True)
    os.chmod(owner_mismatch_artifact.parent, 0o755)
    with pytest.raises(srrs.PrivateParentDirError) as excinfo:
        srrs.prepare_private_parent_dir(owner_mismatch_artifact, expected_uid=os.getuid() + 999_999)
    assert excinfo.value.reason_code == "parent_owner_mismatch"
    assert stat.S_IMODE(os.stat(owner_mismatch_artifact.parent).st_mode) == 0o755  # untouched

    # -- reject_or_repair_loose_parent_by_explicit_policy: a pre-existing
    # parent that IS safe (non-symlink, owned by us) but has a looser mode
    # (left by an older version of this script) is explicitly REPAIRED to
    # 0700 -- never silently trusted as-is, and never rejected outright
    # either (that would regress AC3 for every pre-#2004 checkout).
    loose_mode_artifact = tmp_path / "loose-mode" / "eligibility.json"
    loose_mode_artifact.parent.mkdir(parents=True)
    os.chmod(loose_mode_artifact.parent, 0o755)
    srrs.prepare_private_parent_dir(loose_mode_artifact)
    assert stat.S_IMODE(os.stat(loose_mode_artifact.parent).st_mode) == 0o700

    # -- reject_parent_replaced_after_validation: a parent that was safe at
    # the time of an EARLIER call is replaced with a symlink before a LATER
    # call -- each call independently re-validates via its own single
    # O_NOFOLLOW open (never trusting state left over from a prior call).
    replaced_artifact = tmp_path / "replaced" / "eligibility.json"
    replaced_artifact.parent.mkdir(parents=True)
    srrs.prepare_private_parent_dir(replaced_artifact)  # first call: safe, succeeds
    assert stat.S_IMODE(os.stat(replaced_artifact.parent).st_mode) == 0o700
    replaced_artifact.parent.rmdir()
    other_real_dir = tmp_path / "replaced-real-elsewhere"
    other_real_dir.mkdir()
    os.chmod(other_real_dir, 0o755)
    os.symlink(other_real_dir, replaced_artifact.parent)
    with pytest.raises(srrs.PrivateParentDirError) as excinfo:
        srrs.prepare_private_parent_dir(replaced_artifact)  # second call: now a symlink
    assert excinfo.value.reason_code == "parent_is_symlink"
    assert stat.S_IMODE(os.stat(other_real_dir).st_mode) == 0o755  # untouched


def test_eligibility_artifact_swapped_between_stat_and_read_is_toctou_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reject_artifact_swapped_between_stat_and_read: open_private_artifact_or_reason()
    opens with O_NOFOLLOW and fstats/reads the SAME fd -- so even if the
    pathname is deleted and replaced with a symlink to a completely
    different file immediately after the open+fstat step, the bytes
    ultimately read must still be the ORIGINAL file's bytes (the fd is
    pinned to the original inode; the swapped pathname is never consulted
    again).
    """
    monkeypatch.setenv("SRRS_SECRETS_MODE", "none")
    monkeypatch.setenv("SRRS_GIT_LS_REMOTE_EXIT", "2")
    monkeypatch.setenv("SRRS_GH_VISIBILITY", "public")

    eligibility_path = tmp_path / "session-recording" / "scope-rollup-eligibility.json"
    monkeypatch.setenv("SCOPE_ROLLUP_ELIGIBILITY_ARTIFACT_PATH", str(eligibility_path))
    exit_code, artifact = srrs.emit_scope_rollup_eligibility_artifact(REPO_ROOT)
    assert exit_code == srrs.EXIT_PASS
    original_bytes = eligibility_path.read_bytes()

    fd, st, reason = srrs.open_private_artifact_or_reason(
        eligibility_path, symlink_reason="test_symlink", missing_reason="test_missing",
    )
    assert fd is not None, reason
    try:
        # Swap the pathname for a symlink to a decoy file AFTER the fd was
        # already opened (simulating an attacker racing the stat-then-read
        # window this fix closes).
        decoy_path = tmp_path / "decoy.json"
        decoy_path.write_bytes(b'{"decoy": true}')
        eligibility_path.unlink()
        os.symlink(decoy_path, eligibility_path)

        raw = os.read(fd, st.st_size)
    finally:
        os.close(fd)

    assert raw == original_bytes  # NOT the decoy content
    assert stat.S_ISREG(st.st_mode)  # the fstat result also describes the original regular file


# ---------------------------------------------------------------------------
# P1-1: the readiness consumer performs the SAME read-only parent-directory
# validation before trusting anything under it (pure Python -- the consumer
# itself is a Python script; only the readiness PRODUCER is Node).
# ---------------------------------------------------------------------------


def test_readiness_consumer_rejects_symlink_parent(tmp_path: Path) -> None:
    eligibility_path = tmp_path / "eligibility-dir" / "scope-rollup-eligibility.json"
    eligibility_path.parent.mkdir(parents=True, mode=0o700)
    _write_json_mode_0600(eligibility_path, _default_eligibility())

    real_readiness_dir = tmp_path / "readiness-real-target"
    real_readiness_dir.mkdir()
    os.chmod(real_readiness_dir, 0o700)
    readiness_path = tmp_path / "readiness-symlinked-parent" / "scope-rollup-readiness.json"
    os.symlink(real_readiness_dir, readiness_path.parent)
    _write_json_mode_0600(real_readiness_dir / "scope-rollup-readiness.json", _default_readiness())

    message = _render_marker(invocation_id="inv-2004-readiness-symlink-parent")
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
    assert record["parser_status"] == "readiness_invalid_parent_symlink"
    assert stat.S_IMODE(os.stat(real_readiness_dir).st_mode) == 0o700  # untouched (read-only check)


# ---------------------------------------------------------------------------
# P1-1 (readiness PRODUCER, Node side) + P1-3 (cross-language override
# parity) + P2 (hermetic default-path E2E). These invoke the real Node
# bootstrap script, so they run in the node-backed-hook-tests CI lane (see
# .github/ci/python-test-plan.json deselect + .github/workflows/ci.yml).
# ---------------------------------------------------------------------------


def _print_resolved_override(override: str, repo_root: Path) -> tuple[int, str]:
    result = subprocess.run(
        ["node", str(BOOTSTRAP_SCRIPT), "--print-resolved-override", override, str(repo_root)],
        text=True,
        capture_output=True,
        check=False,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    return result.returncode, result.stdout.strip()


def test_cross_language_override_resolution_parity(tmp_path: Path) -> None:
    """P1-3: Python's resolve_session_recording_artifact_override() and
    Node's resolveReadinessOverride() must agree byte-for-byte, including
    for inputs involving a symlinked ancestor directory (never resolved by
    either side any more -- purely lexical), `..` traversal (rejected by
    both), and a leaf that does not exist on disk.
    """
    repo_root = tmp_path / "parity-repo"
    repo_root.mkdir()
    # A symlinked ancestor directory under repo_root: since resolution is
    # now purely lexical, this must NOT affect the resolved path in either
    # language (neither calls realpath/fs.realpathSync on it).
    real_dir = tmp_path / "parity-real-target"
    real_dir.mkdir()
    os.symlink(real_dir, repo_root / "linked-ancestor")

    cases: list[str] = [
        "tmp/session-recording/scope-rollup-eligibility.json",
        "./tmp/session-recording/scope-rollup-eligibility.json",
        "linked-ancestor/nested/does-not-exist-yet.json",
        "/abs/tmp/session-recording/scope-rollup-eligibility.json",
        "/abs/./tmp/session-recording/scope-rollup-eligibility.json",
    ]
    for override in cases:
        py_resolved = str(srrs.resolve_session_recording_artifact_override(override, repo_root))
        node_exit, node_resolved = _print_resolved_override(override, repo_root)
        assert node_exit == 0, f"node side failed for override={override!r}: {node_resolved!r}"
        assert py_resolved == node_resolved, f"parity mismatch for override={override!r}"

    # `..` traversal: both languages reject outright (never silently
    # collapsed), regardless of absolute/relative.
    for dotdot_override in ("../escape/eligibility.json", "/abs/../escape/eligibility.json"):
        with pytest.raises(srrs.ArtifactOverridePathError) as excinfo:
            srrs.resolve_session_recording_artifact_override(dotdot_override, repo_root)
        assert excinfo.value.reason_code == "override_path_contains_dotdot"

        node_exit, node_output = _print_resolved_override(dotdot_override, repo_root)
        assert node_exit == 3
        assert node_output == "ERROR:override_path_contains_dotdot"


def test_readiness_producer_parent_dir_toctou_hardening(tmp_path: Path) -> None:
    """P1-1 (Node side): same symlink-rejection / loose-mode-repair
    contract as the Python eligibility producer, exercised against the real
    bootstrap-source-bound-readiness.mjs preparePrivateParentDir().
    """
    # reject_symlink_parent: real target must never be chmod'd through the
    # symlink, and the bootstrap run must fail closed (non-zero exit, no
    # artifact written).
    real_target = tmp_path / "node-real-target"
    real_target.mkdir()
    os.chmod(real_target, 0o755)
    symlinked_parent = tmp_path / "node-symlinked-parent"
    os.symlink(real_target, symlinked_parent)
    readiness_path = symlinked_parent / "scope-rollup-readiness.json"
    result = _run_bootstrap_readiness(readiness_path)
    assert result.returncode != 0
    assert "parent_is_symlink" in result.stderr
    assert not readiness_path.exists()
    assert stat.S_IMODE(os.stat(real_target).st_mode) == 0o755  # untouched

    # reject_or_repair_loose_parent_by_explicit_policy: a real, safe,
    # pre-existing parent with a looser mode is repaired to exactly 0700.
    loose_parent = tmp_path / "node-loose-mode"
    loose_parent.mkdir()
    os.chmod(loose_parent, 0o755)
    loose_readiness_path = loose_parent / "scope-rollup-readiness.json"
    result = _run_bootstrap_readiness(loose_readiness_path)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(os.stat(loose_parent).st_mode) == 0o700


def test_hermetic_default_path_producer_consumer_roundtrip(tmp_path: Path) -> None:
    """P2: a fully isolated (tmp_path fake-repo) default-path E2E -- the
    real eligibility producer, the real Node readiness producer (via the
    `--repo-root` test seam), and the real consumer, all invoked WITHOUT any
    SCOPE_ROLLUP_*_ARTIFACT_PATH override, so the fixture repo's OWN default
    ``tmp/session-recording/`` path is exercised end to end. The live real
    repo's canonical runtime artifact is never touched by this test.

    Same fixture also exercises the AC6-style negative controls: old-path-
    only, single-artifact-missing, and a symlinked default parent -- all
    without ever needing the real repo's default location.
    """
    fake_repo_root = tmp_path / "fake-repo"
    (fake_repo_root / "docs" / "dev").mkdir(parents=True)
    (fake_repo_root / "docs" / "dev" / "session-recording-policy.md").write_bytes(_POLICY_PATH.read_bytes())
    (fake_repo_root / "docs" / "dev" / "secret-policy.md").write_bytes(_SECRET_POLICY_PATH.read_bytes())

    def _emit_eligibility() -> None:
        old_env = {
            k: os.environ.get(k)
            for k in ("SRRS_SECRETS_MODE", "SRRS_GIT_LS_REMOTE_EXIT", "SRRS_GH_VISIBILITY")
        }
        os.environ["SRRS_SECRETS_MODE"] = "none"
        os.environ["SRRS_GIT_LS_REMOTE_EXIT"] = "2"
        os.environ["SRRS_GH_VISIBILITY"] = "public"
        try:
            exit_code, _artifact = srrs.emit_scope_rollup_eligibility_artifact(fake_repo_root)
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        assert exit_code == srrs.EXIT_PASS

    # 1. Real eligibility producer, no override.
    _emit_eligibility()
    eligibility_default_path = fake_repo_root / "tmp" / "session-recording" / "scope-rollup-eligibility.json"
    assert eligibility_default_path.exists()

    # 2. Real Node readiness producer, no override, via --repo-root.
    result = subprocess.run(
        ["node", str(BOOTSTRAP_SCRIPT), "--repo-root", str(fake_repo_root)],
        text=True,
        capture_output=True,
        check=False,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    readiness_default_path = fake_repo_root / "tmp" / "session-recording" / "scope-rollup-readiness.json"
    assert readiness_default_path.exists()

    # 3. Real consumer, no override -> both from the fixture's own default paths.
    def _run_default_path_consumer(invocation_id: str, capture_dir: Path) -> dict[str, object]:
        capture_dir.mkdir(parents=True, exist_ok=True)
        message = _render_marker(invocation_id=invocation_id)
        payload = {
            "hook_event_name": "SubagentStop",
            "agent_type": "scope-rollup-runner",
            "last_assistant_message": message,
            "stop_hook_active": False,
        }
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
        return _read_capture_record(capture_dir)

    record = _run_default_path_consumer("inv-2004-hermetic-ok", tmp_path / "capture-ok")
    assert record["provenance"]["eligibility_verification_reason_code"] == "ok"
    assert record["provenance"]["readiness_verification_reason_code"] == "ok"
    assert record["parser_status"] == "ok"

    # Negative control: readiness missing (renamed away) -> readiness_missing,
    # eligibility unaffected -> proves neither artifact silently substitutes
    # for the other.
    renamed_readiness = readiness_default_path.with_name("scope-rollup-readiness.json.moved")
    readiness_default_path.rename(renamed_readiness)
    try:
        record_missing = _run_default_path_consumer("inv-2004-hermetic-missing-readiness", tmp_path / "capture-missing")
        assert record_missing["parser_status"] == "readiness_missing"
    finally:
        renamed_readiness.rename(readiness_default_path)

    # Negative control: default parent directory (shared by BOTH eligibility
    # and readiness -- both live directly under tmp/session-recording/)
    # replaced with a symlink -> fail-closed at the eligibility stage (the
    # first check performed), real target untouched.
    real_elsewhere = tmp_path / "hermetic-symlink-target"
    real_elsewhere.mkdir()
    os.chmod(real_elsewhere, 0o755)
    shared_parent = eligibility_default_path.parent
    assert shared_parent == readiness_default_path.parent
    eligibility_default_path.unlink()
    readiness_default_path.unlink()
    shared_parent.rmdir()
    os.symlink(real_elsewhere, shared_parent)
    record_symlink = _run_default_path_consumer("inv-2004-hermetic-symlink-parent", tmp_path / "capture-symlink")
    assert record_symlink["parser_status"] == "eligibility_invalid_parent_symlink"
    assert stat.S_IMODE(os.stat(real_elsewhere).st_mode) == 0o755  # untouched
