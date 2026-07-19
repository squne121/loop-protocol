"""Tests for reviewer_claim_replay_state_store.py (Issue #1515).

Covers:
- AC1: --write uses same-directory temp file + os.replace atomic write
- AC2: --write / --read reject symlinked state paths
- AC3: --write / --read reject non-regular files (fifo)
- AC4: --read fails closed (status: corrupt) on malformed/unknown-schema/
  missing-identity-field state, never silently treats it as fresh
- AC11: a different issue_number resets (identity_mismatch, not an error)
- AC12: a different refinement_session_id (same issue/body) resets
- AC13: a concurrent writer (pre-existing lock file) is detected and the
  write fails without waiting/retrying
- AC14: a crash between temp-file write and os.replace leaves the prior
  valid state file untouched
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest import mock

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import reviewer_claim_replay_state_store as store  # noqa: E402

IDENTITY = {
    "repository_full_name": "squne121/loop-protocol",
    "issue_number": 1021,
    "refinement_session_id": "session-aaaa",
}


def _valid_state(**overrides: object) -> dict[str, object]:
    state = {
        "schema": store.STATE_SCHEMA_V2,
        "repository_full_name": IDENTITY["repository_full_name"],
        "issue_number": IDENTITY["issue_number"],
        "refinement_session_id": IDENTITY["refinement_session_id"],
        "body_sha256": "sha256:body-a",
        "reviewer_blocker_code": "C4",
        "normalized_kind": "vc_command_format",
        "consecutive_unbacked_count": 1,
        "last_review_artifact": "/tmp/review.json",
        "updated_at_iteration_id": "2026-07-14T00:00:00Z",
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# AC1: atomic replace
# ---------------------------------------------------------------------------


def test_write_uses_atomic_replace(tmp_path: Path):
    """GIVEN a first write WHEN write_state runs THEN os.replace is called
    with a same-directory temp source and the target state path."""
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def _spy_replace(src: str, dst: str) -> None:
        calls.append((src, dst))
        real_replace(src, dst)

    with mock.patch.object(os, "replace", side_effect=_spy_replace):
        result = store.write_state(state_dir=tmp_path, next_state=_valid_state())

    assert result["status"] == "ok"
    assert len(calls) == 1
    src, dst = calls[0]
    assert Path(src).parent == tmp_path
    assert dst == str(tmp_path / store.STATE_FILE_NAME)
    assert json.loads((tmp_path / store.STATE_FILE_NAME).read_text(encoding="utf-8")) == _valid_state()


# ---------------------------------------------------------------------------
# AC2: symlink rejection
# ---------------------------------------------------------------------------


def test_write_rejects_symlinked_state_path(tmp_path: Path):
    real_target = tmp_path / "elsewhere.json"
    real_target.write_text("{}", encoding="utf-8")
    state_path = tmp_path / store.STATE_FILE_NAME
    state_path.symlink_to(real_target)

    result = store.write_state(state_dir=tmp_path, next_state=_valid_state())
    assert result["status"] == "corrupt"
    assert "symlink" in (result["error"] or "")
    # The symlink target must not have been overwritten.
    assert real_target.read_text(encoding="utf-8") == "{}"


def test_read_rejects_symlinked_state_path(tmp_path: Path):
    real_target = tmp_path / "elsewhere.json"
    real_target.write_text(json.dumps(_valid_state()), encoding="utf-8")
    state_path = tmp_path / store.STATE_FILE_NAME
    state_path.symlink_to(real_target)

    result = store.read_state(state_dir=tmp_path, **IDENTITY)
    assert result["status"] == "corrupt"
    assert "symlink" in (result["error"] or "")


# ---------------------------------------------------------------------------
# AC3: non-regular file rejection
# ---------------------------------------------------------------------------


def test_write_rejects_non_regular_file(tmp_path: Path):
    state_path = tmp_path / store.STATE_FILE_NAME
    os.mkfifo(str(state_path))
    try:
        result = store.write_state(state_dir=tmp_path, next_state=_valid_state())
        assert result["status"] == "corrupt"
        assert "non-regular" in (result["error"] or "")
        assert stat.S_ISFIFO(state_path.stat().st_mode)
    finally:
        state_path.unlink()


def test_read_rejects_non_regular_file(tmp_path: Path):
    state_path = tmp_path / store.STATE_FILE_NAME
    os.mkfifo(str(state_path))
    try:
        result = store.read_state(state_dir=tmp_path, **IDENTITY)
        assert result["status"] == "corrupt"
        assert "non-regular" in (result["error"] or "")
    finally:
        state_path.unlink()


# ---------------------------------------------------------------------------
# AC4: fail-closed on corrupt / malformed state
# ---------------------------------------------------------------------------


def test_corrupt_state_fail_closed_on_bad_json(tmp_path: Path):
    (tmp_path / store.STATE_FILE_NAME).write_text("{not valid json", encoding="utf-8")
    result = store.read_state(state_dir=tmp_path, **IDENTITY)
    assert result["status"] == "corrupt"
    assert result["state"] == {}


def test_corrupt_state_fail_closed_on_unknown_schema(tmp_path: Path):
    bad = _valid_state(schema="SOME_OTHER_SCHEMA_V9")
    (tmp_path / store.STATE_FILE_NAME).write_text(json.dumps(bad), encoding="utf-8")
    result = store.read_state(state_dir=tmp_path, **IDENTITY)
    assert result["status"] == "corrupt"


def test_corrupt_state_fail_closed_on_missing_identity_field(tmp_path: Path):
    bad = _valid_state()
    del bad["refinement_session_id"]
    (tmp_path / store.STATE_FILE_NAME).write_text(json.dumps(bad), encoding="utf-8")
    result = store.read_state(state_dir=tmp_path, **IDENTITY)
    assert result["status"] == "corrupt"


def test_corrupt_state_fail_closed_on_nan(tmp_path: Path):
    (tmp_path / store.STATE_FILE_NAME).write_text(
        '{"schema":"REVIEWER_CLAIM_REPLAY_STATE_V2","bad":NaN}', encoding="utf-8"
    )
    result = store.read_state(state_dir=tmp_path, **IDENTITY)
    assert result["status"] == "corrupt"


def test_write_rejects_next_state_with_unknown_field(tmp_path: Path):
    bad = _valid_state()
    bad["unexpected_extra_field"] = "nope"
    result = store.write_state(state_dir=tmp_path, next_state=bad)
    assert result["status"] == "error"
    assert not (tmp_path / store.STATE_FILE_NAME).exists()


# ---------------------------------------------------------------------------
# not_found is a reset, not an error (first-time case)
# ---------------------------------------------------------------------------


def test_read_missing_file_is_first_time_ok(tmp_path: Path):
    result = store.read_state(state_dir=tmp_path, **IDENTITY)
    assert result["status"] == "ok"
    assert result["state"] == {}
    assert result["reset_reason"] == "not_found"


# ---------------------------------------------------------------------------
# AC11 / AC12: identity mismatch resets
# ---------------------------------------------------------------------------


def test_wrong_issue_number_resets(tmp_path: Path):
    store.write_state(state_dir=tmp_path, next_state=_valid_state())
    result = store.read_state(
        state_dir=tmp_path,
        repository_full_name=IDENTITY["repository_full_name"],
        issue_number=9999,
        refinement_session_id=IDENTITY["refinement_session_id"],
    )
    assert result["status"] == "ok"
    assert result["state"] == {}
    assert result["reset_reason"] == "identity_mismatch"


def test_wrong_refinement_session_id_resets(tmp_path: Path):
    store.write_state(state_dir=tmp_path, next_state=_valid_state())
    result = store.read_state(
        state_dir=tmp_path,
        repository_full_name=IDENTITY["repository_full_name"],
        issue_number=IDENTITY["issue_number"],
        refinement_session_id="a-different-session",
    )
    assert result["status"] == "ok"
    assert result["state"] == {}
    assert result["reset_reason"] == "identity_mismatch"


def test_same_body_hash_different_issue_number_resets(tmp_path: Path):
    """Same body_sha256, different issue_number -- must still reset (the
    body hash alone is not a sufficient identity match)."""
    store.write_state(state_dir=tmp_path, next_state=_valid_state(body_sha256="sha256:shared"))
    result = store.read_state(
        state_dir=tmp_path,
        repository_full_name=IDENTITY["repository_full_name"],
        issue_number=IDENTITY["issue_number"] + 1,
        refinement_session_id=IDENTITY["refinement_session_id"],
    )
    assert result["reset_reason"] == "identity_mismatch"


def test_matching_identity_preserves_state(tmp_path: Path):
    store.write_state(state_dir=tmp_path, next_state=_valid_state(consecutive_unbacked_count=1))
    result = store.read_state(state_dir=tmp_path, **IDENTITY)
    assert result["status"] == "ok"
    assert result["reset_reason"] is None
    assert result["state"]["consecutive_unbacked_count"] == 1


# ---------------------------------------------------------------------------
# AC13: concurrent writer detection
# ---------------------------------------------------------------------------


def test_concurrent_write_detected(tmp_path: Path):
    lock_path = tmp_path / (store.STATE_FILE_NAME + ".lock")
    tmp_path.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    try:
        result = store.write_state(state_dir=tmp_path, next_state=_valid_state())
        assert result["status"] == "concurrent_write_detected"
        assert not (tmp_path / store.STATE_FILE_NAME).exists()
    finally:
        lock_path.unlink(missing_ok=True)


def test_write_removes_lock_file_after_success(tmp_path: Path):
    result = store.write_state(state_dir=tmp_path, next_state=_valid_state())
    assert result["status"] == "ok"
    assert not (tmp_path / (store.STATE_FILE_NAME + ".lock")).exists()


def test_write_removes_lock_file_after_failure(tmp_path: Path):
    with mock.patch.object(os, "replace", side_effect=OSError("boom")):
        result = store.write_state(state_dir=tmp_path, next_state=_valid_state())
    assert result["status"] == "error"
    assert not (tmp_path / (store.STATE_FILE_NAME + ".lock")).exists()


# ---------------------------------------------------------------------------
# AC14: crash before os.replace preserves prior state
# ---------------------------------------------------------------------------


def test_crash_before_replace_preserves_prior_state(tmp_path: Path):
    first = _valid_state(consecutive_unbacked_count=1)
    ok_first = store.write_state(state_dir=tmp_path, next_state=first)
    assert ok_first["status"] == "ok"
    before_bytes = (tmp_path / store.STATE_FILE_NAME).read_bytes()

    second = _valid_state(consecutive_unbacked_count=2)
    with mock.patch.object(os, "replace", side_effect=OSError("simulated crash before replace")):
        result = store.write_state(state_dir=tmp_path, next_state=second)

    assert result["status"] == "error"
    after_bytes = (tmp_path / store.STATE_FILE_NAME).read_bytes()
    assert after_bytes == before_bytes
    # No leftover temp files in the state dir.
    leftovers = [
        p
        for p in tmp_path.iterdir()
        if p.name not in (store.STATE_FILE_NAME,)
    ]
    assert leftovers == []


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def _run_cli(args: list[str]) -> tuple[int, dict]:
    import subprocess

    script = SCRIPTS_DIR / "reviewer_claim_replay_state_store.py"
    proc = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return proc.returncode, json.loads(proc.stdout)


def test_cli_read_and_write_round_trip(tmp_path: Path):
    state_dir = str(tmp_path)
    rc, payload = _run_cli(
        [
            "--read",
            "--state-dir",
            state_dir,
            "--repository-full-name",
            IDENTITY["repository_full_name"],
            "--issue-number",
            str(IDENTITY["issue_number"]),
            "--refinement-session-id",
            IDENTITY["refinement_session_id"],
        ]
    )
    assert rc == 0
    assert payload["reset_reason"] == "not_found"

    rc, payload = _run_cli(
        ["--write", "--state-dir", state_dir, "--next-state-inline", json.dumps(_valid_state())]
    )
    assert rc == 0
    assert payload["status"] == "ok"

    rc, payload = _run_cli(
        [
            "--read",
            "--state-dir",
            state_dir,
            "--repository-full-name",
            IDENTITY["repository_full_name"],
            "--issue-number",
            str(IDENTITY["issue_number"]),
            "--refinement-session-id",
            IDENTITY["refinement_session_id"],
        ]
    )
    assert rc == 0
    assert payload["state"]["consecutive_unbacked_count"] == 1


def test_cli_requires_exactly_one_of_read_or_write(tmp_path: Path):
    rc, payload = _run_cli(["--state-dir", str(tmp_path)])
    assert rc == 1
    assert payload["status"] == "error"

    rc, payload = _run_cli(
        [
            "--read",
            "--write",
            "--state-dir",
            str(tmp_path),
        ]
    )
    assert rc == 1
    assert payload["status"] == "error"


# ---------------------------------------------------------------------------
# Issue #1532 AC5/High-3: --write-v2 only persists PARENT_REPLAY_NEXT_STATE
# from a STRICTLY validated REVIEW_COMPACT_VALIDATION_RESULT_V2 payload --
# not merely a payload whose validation_status field says "valid" (High-3:
# a caller-fabricated {"validation_status": "valid", ...} object is
# rejected unless schema/schema_version/envelope_kind/violations/identity
# also check out).
# ---------------------------------------------------------------------------

_VALID_BINDING_DIGEST = "sha256:" + ("a" * 64)


def _valid_validation_result_v2(**overrides: object) -> dict[str, object]:
    payload = {
        "schema": "REVIEW_COMPACT_VALIDATION_RESULT_V2",
        "schema_version": "2",
        "validation_status": "valid",
        "envelope_kind": "needs_fix_v2",
        "violations": [],
        "normalized_payload": {
            "PARENT_REPLAY_NEXT_STATE": json.dumps(
                _valid_state(), separators=(",", ":"), sort_keys=True
            ),
            "PARENT_REPLAY_BINDING_DIGEST": _VALID_BINDING_DIGEST,
        },
    }
    payload.update(overrides)
    return payload


class TestWriteV2ValidatedOnly:
    """Issue #1532 AC5/High-3: state store persists PARENT_REPLAY_NEXT_STATE
    if and only if the caller-supplied REVIEW_COMPACT_VALIDATION_RESULT_V2
    is STRICTLY well-formed (schema / schema_version / envelope_kind /
    empty violations / identity) -- not merely `validation_status: valid`.
    Raw stdout / invalid validation / binding mismatch / caller-fabricated
    partial payloads never reach the state file."""

    def test_write_v2_persists_when_validation_status_is_valid(self, tmp_path: Path):
        result = store.write_state_v2_from_validated_payload(
            state_dir=tmp_path,
            validation_result_v2=_valid_validation_result_v2(),
            expected_repository_full_name=IDENTITY["repository_full_name"],
            expected_issue_number=IDENTITY["issue_number"],
            expected_refinement_session_id=IDENTITY["refinement_session_id"],
            expected_parent_binding_digest=_VALID_BINDING_DIGEST,
        )
        assert result["status"] == "ok"
        assert result["operation"] == "write_v2"
        assert result["state"]["consecutive_unbacked_count"] == 1

        read_result = store.read_state(state_dir=tmp_path, **IDENTITY)
        assert read_result["status"] == "ok"
        assert read_result["state"]["consecutive_unbacked_count"] == 1

    def test_write_v2_rejects_invalid_validation_status_without_touching_state_file(
        self, tmp_path: Path
    ):
        # First establish a known-good baseline state.
        store.write_state_v2_from_validated_payload(
            state_dir=tmp_path, validation_result_v2=_valid_validation_result_v2()
        )
        state_path = store._state_path(tmp_path)
        before_bytes = state_path.read_bytes()

        # A human_judgment_required / invalid validation result (e.g. the
        # child's PARENT_REPLAY_BINDING_DIGEST failed to match the
        # orchestrator's independently-computed expected digest) must
        # never overwrite the prior state.
        tampered = _valid_validation_result_v2(
            validation_status="invalid",
            normalized_payload=None,
        )
        result = store.write_state_v2_from_validated_payload(
            state_dir=tmp_path, validation_result_v2=tampered
        )
        assert result["status"] == "rejected"
        assert state_path.read_bytes() == before_bytes

    def test_write_v2_rejects_missing_replay_next_state_field(self, tmp_path: Path):
        result = store.write_state_v2_from_validated_payload(
            state_dir=tmp_path,
            validation_result_v2=_valid_validation_result_v2(normalized_payload={}),
        )
        assert result["status"] == "rejected"
        assert not store._state_path(tmp_path).exists()

    def test_write_v2_rejects_malformed_replay_next_state_json(self, tmp_path: Path):
        result = store.write_state_v2_from_validated_payload(
            state_dir=tmp_path,
            validation_result_v2=_valid_validation_result_v2(
                normalized_payload={
                    "PARENT_REPLAY_NEXT_STATE": "not-json{{",
                    "PARENT_REPLAY_BINDING_DIGEST": _VALID_BINDING_DIGEST,
                }
            ),
        )
        assert result["status"] == "rejected"
        assert not store._state_path(tmp_path).exists()

    def test_write_v2_rejects_caller_fabricated_valid_status_missing_schema(
        self, tmp_path: Path
    ):
        """High-3 regression: a bare {"validation_status": "valid", ...}
        object assembled by a caller/attacker (not produced by
        build_result_v2) must be rejected -- the schema/schema_version/
        envelope_kind/violations checks are NOT optional."""
        fabricated = {
            "validation_status": "valid",
            "normalized_payload": {
                "PARENT_REPLAY_NEXT_STATE": json.dumps(_valid_state()),
                "PARENT_REPLAY_BINDING_DIGEST": _VALID_BINDING_DIGEST,
            },
        }
        result = store.write_state_v2_from_validated_payload(
            state_dir=tmp_path, validation_result_v2=fabricated
        )
        assert result["status"] == "rejected"
        assert not store._state_path(tmp_path).exists()

    def test_write_v2_rejects_nonempty_violations_even_when_status_is_valid(
        self, tmp_path: Path
    ):
        tampered = _valid_validation_result_v2(violations=[{"code": "some_violation"}])
        result = store.write_state_v2_from_validated_payload(
            state_dir=tmp_path, validation_result_v2=tampered
        )
        assert result["status"] == "rejected"
        assert not store._state_path(tmp_path).exists()

    def test_write_v2_rejects_expected_binding_digest_mismatch(self, tmp_path: Path):
        result = store.write_state_v2_from_validated_payload(
            state_dir=tmp_path,
            validation_result_v2=_valid_validation_result_v2(),
            expected_parent_binding_digest="sha256:" + ("f" * 64),
        )
        assert result["status"] == "rejected"
        assert not store._state_path(tmp_path).exists()

    def test_cli_write_v2_round_trip(self, tmp_path: Path):
        validation_result_v2 = _valid_validation_result_v2()
        rc, payload = _run_cli(
            [
                "--write-v2",
                "--state-dir",
                str(tmp_path),
                "--validation-result-v2-inline",
                json.dumps(validation_result_v2),
                "--expected-parent-binding-digest",
                _VALID_BINDING_DIGEST,
            ]
        )
        assert rc == 0
        assert payload["status"] == "ok"
        assert payload["operation"] == "write_v2"

    def test_cli_write_v2_rejects_invalid_validation_result(self, tmp_path: Path):
        validation_result_v2 = _valid_validation_result_v2(validation_status="invalid")
        rc, payload = _run_cli(
            [
                "--write-v2",
                "--state-dir",
                str(tmp_path),
                "--validation-result-v2-inline",
                json.dumps(validation_result_v2),
            ]
        )
        assert rc == 1
        assert payload["status"] == "rejected"

    def test_cli_requires_exactly_one_of_read_write_or_write_v2(self, tmp_path: Path):
        rc, payload = _run_cli(
            [
                "--write",
                "--write-v2",
                "--state-dir",
                str(tmp_path),
            ]
        )
        assert rc == 1
        assert payload["status"] == "error"
