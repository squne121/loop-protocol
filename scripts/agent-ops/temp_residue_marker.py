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
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone

MARKER_SCHEMA = "temp_residue_owner/v1"
MARKER_FILENAME = ".temp-residue-owner.json"
MAX_MARKER_BYTES_DEFAULT = 4096

# The following patterns/limits MUST stay byte-for-byte in sync with
# schemas/temp_residue_owner_v1.schema.json. validate_marker_schema() below
# is the canonical Python predicate for that JSON Schema; any divergence is a
# P0 defect (Issue #1417 PR #1427 review). A parity corpus test lives in
# tests/agent_ops/test_temp_residue_classifier.py.
_MARKER_ID_RE = re.compile(r"^tro-[0-9a-fA-F-]{8,}$")
_REPOSITORY_RE = re.compile(r"^[^/]+/[^/]+$")
_TARGET_RELPATH_RE = re.compile(r"^(tmp|\.claude/tmp)/[^/]+$")
_SESSION_ID_MAX_LENGTH = 256
_NONCE_MIN_LENGTH = 8

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
    trust properties (symlink/owner/mode) — see ``read_marker_file`` for that.

    This predicate MUST match schemas/temp_residue_owner_v1.schema.json
    exactly (same patterns / length bounds). See module-level comment.
    """
    if not isinstance(data, dict):
        return False, "not_object"
    if data.get("schema") != MARKER_SCHEMA:
        return False, "schema_mismatch"
    marker_id = data.get("marker_id")
    if not isinstance(marker_id, str) or not _MARKER_ID_RE.match(marker_id):
        return False, "marker_id_invalid"
    repository = data.get("repository")
    if not isinstance(repository, str) or not repository or not _REPOSITORY_RE.match(repository):
        return False, "repository_invalid"
    session_id = data.get("session_id")
    if (
        not isinstance(session_id, str)
        or not session_id
        or len(session_id) > _SESSION_ID_MAX_LENGTH
    ):
        return False, "session_id_invalid"
    target_relpath = data.get("target_relpath")
    if not isinstance(target_relpath, str) or not target_relpath:
        return False, "target_relpath_invalid"
    if target_relpath.startswith("/") or ".." in target_relpath.split("/"):
        return False, "target_relpath_not_normalized"
    if not _TARGET_RELPATH_RE.match(target_relpath):
        return False, "target_relpath_invalid"
    if _parse_iso8601_tz(data.get("created_at")) is None:
        return False, "created_at_invalid"
    expires_at = _parse_iso8601_tz(data.get("expires_at"))
    if expires_at is None:
        return False, "expires_at_invalid"
    nonce = data.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < _NONCE_MIN_LENGTH:
        return False, "nonce_invalid"
    producer = data.get("producer")
    if not isinstance(producer, dict):
        return False, "producer_invalid"
    if producer.get("kind") not in ("self_claim", "trusted_materializer"):
        return False, "producer_kind_invalid"
    if not isinstance(producer.get("version"), str) or not producer["version"]:
        return False, "producer_version_invalid"
    allowed_producer_keys = {"kind", "version"}
    if set(producer.keys()) - allowed_producer_keys:
        return False, "unexpected_field"
    allowed_keys = {
        "schema", "marker_id", "repository", "session_id", "target_relpath",
        "created_at", "expires_at", "nonce", "producer",
    }
    if set(data.keys()) - allowed_keys:
        return False, "unexpected_field"
    return True, None


def _open_marker_nofollow(marker_path: str | None, dir_fd: int | None, name: str | None) -> int:
    """Open the marker file with ``O_NOFOLLOW``, either by absolute
    ``marker_path`` or by ``name`` relative to an already-open ``dir_fd``.
    Raises ``OSError`` on failure (including ``ELOOP`` for a symlink)."""
    if dir_fd is not None:
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    return os.open(marker_path, os.O_RDONLY | os.O_NOFOLLOW)


def _read_marker_result(
    *,
    marker_path: str | None,
    dir_fd: int | None,
    name: str | None,
    max_bytes: int,
) -> MarkerResult:
    """Shared implementation for ``read_marker_file`` / ``read_marker_file_at``.

    TOCTOU note (Issue #1417 PR #1427 review): a preliminary ``os.lstat``
    (path form only) exists purely to distinguish "absent" from "present";
    ALL trust decisions (symlink / regular-file / mode / link-count / size)
    are made from ``os.fstat`` on the already-opened ``O_NOFOLLOW`` file
    descriptor, so an attacker cannot swap the target between a check and
    the read. The ``dir_fd`` form additionally avoids re-resolving any
    parent-directory pathname component.
    """
    if dir_fd is None:
        try:
            os.lstat(marker_path)
        except FileNotFoundError:
            return MarkerResult(STATE_ABSENT, None, None)
        except OSError:
            return MarkerResult(STATE_UNREADABLE, None, "lstat_failed")

    try:
        fd = _open_marker_nofollow(marker_path, dir_fd, name)
    except FileNotFoundError:
        if dir_fd is not None:
            return MarkerResult(STATE_ABSENT, None, None)
        return MarkerResult(STATE_UNREADABLE, None, "open_failed")
    except OSError as exc:
        if getattr(exc, "errno", None) == 40:  # ELOOP: path resolved to a symlink
            return MarkerResult(STATE_UNTRUSTED, None, "symlink_marker")
        return MarkerResult(STATE_UNREADABLE, None, "open_failed")

    try:
        st = os.fstat(fd)
        if stat.S_ISLNK(st.st_mode):
            return MarkerResult(STATE_UNTRUSTED, None, "symlink_marker")
        if not stat.S_ISREG(st.st_mode):
            return MarkerResult(STATE_UNTRUSTED, None, "not_regular_file")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return MarkerResult(STATE_UNTRUSTED, None, "group_or_other_writable")
        if st.st_nlink > 1:
            return MarkerResult(STATE_UNTRUSTED, None, "hard_link_count_gt_1")
        if st.st_size > max_bytes:
            return MarkerResult(STATE_MALFORMED, None, "oversized_marker")
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


def read_marker_file(
    marker_path: str,
    *,
    max_bytes: int = MAX_MARKER_BYTES_DEFAULT,
) -> MarkerResult:
    """Read+validate a marker file at ``marker_path`` without following symlinks
    and without ever mutating the filesystem.

    Rejects: missing file, symlink, non-regular file, group/other-writable
    mode, hard-linked file, oversized file, duplicate JSON keys,
    NaN/Infinity, and schema violations. Does NOT check session/target
    match — call ``evaluate_marker`` for that.
    """
    return _read_marker_result(marker_path=marker_path, dir_fd=None, name=None, max_bytes=max_bytes)


def read_marker_file_at(
    dir_fd: int,
    name: str = MARKER_FILENAME,
    *,
    max_bytes: int = MAX_MARKER_BYTES_DEFAULT,
) -> MarkerResult:
    """Same as ``read_marker_file`` but opens ``name`` relative to an
    already-open directory file descriptor ``dir_fd`` (Issue #1417 P0-1 /
    P0-5): no pathname is re-resolved from the filesystem root, so a
    symlink swapped into any ancestor directory between the caller's
    directory scan and this read cannot be followed."""
    return _read_marker_result(marker_path=None, dir_fd=dir_fd, name=name, max_bytes=max_bytes)


def _evaluate_marker_result(
    result: MarkerResult,
    *,
    current_session_id: str | None,
    expected_target_relpath: str,
    expected_repository: str | None,
    now: datetime | None,
) -> MarkerResult:
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
    # Issue #1417 PR #1427 review: an unresolved expected_repository (origin
    # unknown / not a GitHub remote) must NOT wildcard-match every marker —
    # that would let a marker claim any repository when we can't verify
    # which one we're actually operating on.
    repository_match = (
        expected_repository is not None and data.get("repository") == expected_repository
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


def evaluate_marker(
    marker_path: str,
    *,
    current_session_id: str | None,
    expected_target_relpath: str,
    expected_repository: str | None,
    now: datetime | None = None,
    max_bytes: int = MAX_MARKER_BYTES_DEFAULT,
) -> MarkerResult:
    """Read a marker at ``marker_path`` and evaluate session/target/expiry/
    repository match.

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
    return _evaluate_marker_result(
        result,
        current_session_id=current_session_id,
        expected_target_relpath=expected_target_relpath,
        expected_repository=expected_repository,
        now=now,
    )


def evaluate_marker_at(
    dir_fd: int,
    name: str = MARKER_FILENAME,
    *,
    current_session_id: str | None,
    expected_target_relpath: str,
    expected_repository: str | None,
    now: datetime | None = None,
    max_bytes: int = MAX_MARKER_BYTES_DEFAULT,
) -> MarkerResult:
    """Same as ``evaluate_marker`` but reads the marker via
    ``read_marker_file_at`` (dir-fd relative, no pathname re-resolution)."""
    result = read_marker_file_at(dir_fd, name, max_bytes=max_bytes)
    return _evaluate_marker_result(
        result,
        current_session_id=current_session_id,
        expected_target_relpath=expected_target_relpath,
        expected_repository=expected_repository,
        now=now,
    )
