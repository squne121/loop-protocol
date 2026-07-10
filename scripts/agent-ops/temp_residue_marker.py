#!/usr/bin/env python3
"""temp_residue_marker.py — read-only parser/validator for temp_residue_owner/v1
ownership markers (Issue #1417).

A marker is placed by an agent inside an owned session subdirectory of an
approved temporary root (``tmp/`` or ``.claude/tmp/``) to reduce accidental
cross-agent deletion. This module implements the **accidental isolation
model** only (see schemas/temp_residue_owner_v1.schema.json): a marker is
NEVER deletion authorization by itself, and a self-claiming process on the
same OS user can forge one. Validation here is advisory input to
``temp_residue_classifier.py``'s read-only ``recommendation`` field, not a
trust boundary.

This module performs no filesystem mutation (no unlink/rmdir/rmtree/write).

Import-safe (no side effects at import time).
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone

MARKER_SCHEMA = "temp_residue_owner/v1"
MARKER_FILENAME = ".temp-residue-owner.json"
MAX_MARKER_BYTES_DEFAULT = 4096

STATE_ABSENT = "absent"
STATE_VALID = "valid"
STATE_MALFORMED = "malformed"
STATE_MISMATCH = "mismatch"
STATE_UNTRUSTED = "untrusted"
STATE_UNREADABLE = "unreadable"


class _DuplicateKeyError(ValueError):
    """Raised by the duplicate-key-rejecting object_pairs_hook."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKeyError(f"duplicate JSON key: {key!r}")
        seen[key] = value
    return seen


def _reject_constant(name: str) -> float:
    # Reached for NaN / Infinity / -Infinity tokens; refuse them explicitly.
    raise ValueError(f"disallowed JSON constant: {name}")


@dataclass(frozen=True)
class MarkerResult:
    state: str
    data: dict | None
    reason: str | None
    session_match: bool | None = None
    target_match: bool | None = None
    expired: bool | None = None


def _parse_iso8601_tz(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def validate_marker_schema(data: object) -> tuple[bool, str | None]:
    """Structural validation of a parsed marker dict. Does NOT check filesystem
    trust properties (symlink/owner/mode) — see ``read_marker_file`` for that."""
    if not isinstance(data, dict):
        return False, "not_object"
    if data.get("schema") != MARKER_SCHEMA:
        return False, "schema_mismatch"
    marker_id = data.get("marker_id")
    if not isinstance(marker_id, str) or not marker_id.startswith("tro-") or len(marker_id) < 12:
        return False, "marker_id_invalid"
    repository = data.get("repository")
    if not isinstance(repository, str) or "/" not in repository or repository.count("/") != 1:
        return False, "repository_invalid"
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return False, "session_id_invalid"
    target_relpath = data.get("target_relpath")
    if not isinstance(target_relpath, str) or not target_relpath.strip():
        return False, "target_relpath_invalid"
    if target_relpath.startswith("/") or ".." in target_relpath.split("/"):
        return False, "target_relpath_not_normalized"
    if _parse_iso8601_tz(data.get("created_at")) is None:
        return False, "created_at_invalid"
    expires_at = _parse_iso8601_tz(data.get("expires_at"))
    if expires_at is None:
        return False, "expires_at_invalid"
    nonce = data.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < 8:
        return False, "nonce_invalid"
    producer = data.get("producer")
    if not isinstance(producer, dict):
        return False, "producer_invalid"
    if producer.get("kind") not in ("self_claim", "trusted_materializer"):
        return False, "producer_kind_invalid"
    if not isinstance(producer.get("version"), str) or not producer["version"]:
        return False, "producer_version_invalid"
    allowed_keys = {
        "schema", "marker_id", "repository", "session_id", "target_relpath",
        "created_at", "expires_at", "nonce", "producer",
    }
    if set(data.keys()) - allowed_keys:
        return False, "unexpected_field"
    return True, None


def read_marker_file(
    marker_path: str,
    *,
    max_bytes: int = MAX_MARKER_BYTES_DEFAULT,
) -> MarkerResult:
    """Read+validate a marker file at ``marker_path`` without following symlinks
    and without ever mutating the filesystem.

    Rejects: missing file, symlink, non-regular file, group/other-writable
    mode, oversized file, duplicate JSON keys, NaN/Infinity, and schema
    violations. Does NOT check session/target match — call
    ``evaluate_marker`` for that.
    """
    try:
        st = os.lstat(marker_path)
    except FileNotFoundError:
        return MarkerResult(STATE_ABSENT, None, None)
    except OSError:
        return MarkerResult(STATE_UNREADABLE, None, "lstat_failed")

    if stat.S_ISLNK(st.st_mode):
        return MarkerResult(STATE_UNTRUSTED, None, "symlink_marker")
    if not stat.S_ISREG(st.st_mode):
        return MarkerResult(STATE_UNTRUSTED, None, "not_regular_file")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return MarkerResult(STATE_UNTRUSTED, None, "group_or_other_writable")
    if st.st_size > max_bytes:
        return MarkerResult(STATE_MALFORMED, None, "oversized_marker")

    try:
        fd = os.open(marker_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return MarkerResult(STATE_UNREADABLE, None, "open_failed")
    try:
        raw = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        return MarkerResult(STATE_MALFORMED, None, "oversized_marker")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return MarkerResult(STATE_MALFORMED, None, "not_utf8")

    try:
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, _DuplicateKeyError, ValueError):
        return MarkerResult(STATE_MALFORMED, None, "json_parse_error")

    ok, reason = validate_marker_schema(data)
    if not ok:
        return MarkerResult(STATE_MALFORMED, None, reason)

    return MarkerResult(STATE_VALID, data, None)


def evaluate_marker(
    marker_path: str,
    *,
    current_session_id: str | None,
    expected_target_relpath: str,
    expected_repository: str | None,
    now: datetime | None = None,
    max_bytes: int = MAX_MARKER_BYTES_DEFAULT,
) -> MarkerResult:
    """Read a marker and evaluate session/target/expiry/repository match.

    Returns a ``MarkerResult`` whose ``state`` reflects the strictest
    applicable classification:
      - absent: no marker file present
      - unreadable / untrusted / malformed: as per ``read_marker_file``
      - mismatch: schema-valid but session_id / target_relpath / repository
        does not match, or the marker has expired
      - valid: schema-valid AND session matches AND target matches AND
        repository matches AND not expired
    """
    result = read_marker_file(marker_path, max_bytes=max_bytes)
    if result.state != STATE_VALID:
        return result

    data = result.data or {}
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    expires_at = _parse_iso8601_tz(data.get("expires_at"))
    expired = expires_at is None or now >= expires_at

    session_match = (
        current_session_id is not None
        and isinstance(data.get("session_id"), str)
        and data["session_id"] == current_session_id
    )
    target_match = data.get("target_relpath") == expected_target_relpath
    repository_match = (
        expected_repository is None or data.get("repository") == expected_repository
    )

    if not (session_match and target_match and repository_match) or expired:
        return MarkerResult(
            STATE_MISMATCH,
            data,
            "session_or_target_or_repository_mismatch" if not expired else "marker_expired",
            session_match=session_match,
            target_match=target_match,
            expired=expired,
        )

    return MarkerResult(
        STATE_VALID,
        data,
        None,
        session_match=session_match,
        target_match=target_match,
        expired=expired,
    )
