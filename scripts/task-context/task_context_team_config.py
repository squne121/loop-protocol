"""Task Context v1 -- read-only Claude Code Agent Teams config consumer
(Issue #2822).

Claude Code (not Task Context) owns the Agent Teams config
``<config-root>/teams/session-<first 8 chars of session id>/config.json``.
Its ``members[]`` lists the teammates of the lead session (one team per
session, removed when the session ends); an idle teammate stays addressable
by ``name`` in ``SendMessage`` regardless of any individual run's lifetime.
This module is the only place that reads that file. It

- is strictly **read-only** (never creates/edits a team config -- the docs
  forbid hand-editing it), performs no DB access and no subprocess I/O;
- resolves the config root through the existing config-root abstraction
  (``task_context_session_registry.resolve_teams_dir``: ``CLAUDE_CONFIG_DIR``
  aware, ``LOOP_TASK_CONTEXT_TEAMS_DIR`` test override), never a hardcoded
  ``~/.claude``;
- is **fail-closed**: every uncertainty (experimental flag off, missing dir,
  unreadable / unparsable file, unexpected schema, config that names another
  session, duplicate member names, name absent from ``members[]``) yields
  "not addressable", which the caller turns into ASK;
- reads only ``members[].name`` and (for diagnostics) nothing else -- no
  message body, transcript, or member prompt ever leaves this module (AC7).
"""

from __future__ import annotations

import json
import os
import pathlib
import re

import task_context_session_registry as session_registry

AGENT_TEAMS_FLAG_ENV_VAR = "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_TEAM_DIR_PREFIX = "session-"
_TEAM_DIR_SESSION_CHARS = 8
_SAFE_SESSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]*$")
# Bounded read: a real team config is tiny; refuse anything huge rather than
# parsing an arbitrary file inside the PreToolUse hot path.
_MAX_CONFIG_BYTES = 1_048_576

# Reason codes (diagnostic only; the caller only tests membership).
FOUND = "teammate_found"
FLAG_DISABLED = "agent_teams_flag_disabled"
NO_SESSION = "no_caller_session"
CONFIG_MISSING = "team_config_missing"
CONFIG_UNREADABLE = "team_config_unreadable"
SCHEMA_MISMATCH = "team_config_schema_mismatch"
SESSION_MISMATCH = "team_config_session_mismatch"
NAME_COLLISION = "teammate_name_collision"
NAME_NOT_MEMBER = "teammate_not_member"


def agent_teams_enabled() -> bool:
    return os.environ.get(AGENT_TEAMS_FLAG_ENV_VAR, "").strip().lower() in _TRUTHY


def team_config_path(caller_session_id: str, *, teams_dir: pathlib.Path | None = None) -> pathlib.Path | None:
    """``<teams-dir>/session-<first 8 of session id>/config.json`` or ``None``
    when the session id cannot safely form a directory name."""
    if not caller_session_id or not _SAFE_SESSION_RE.match(caller_session_id):
        return None
    base = teams_dir if teams_dir is not None else session_registry.resolve_teams_dir()
    return base / f"{_TEAM_DIR_PREFIX}{caller_session_id[:_TEAM_DIR_SESSION_CHARS]}" / "config.json"


def _owning_session_ids(config: dict) -> list[str]:
    """Any explicit session ids the config declares for its lead. The schema
    is Claude-owned and not fully documented, so this only *tightens*: if a
    known lead-session key is present it must agree with the caller."""
    found: list[str] = []
    for key in ("leadSessionId", "lead_session_id"):
        value = config.get(key)
        if value is None:
            continue
        found.append(value if isinstance(value, str) else "")
    return found


def resolve_teammate_by_name(
    to: str | None,
    caller_session_id: str | None,
    *,
    teams_dir: pathlib.Path | None = None,
    require_flag: bool = True,
) -> tuple[bool, str]:
    """Return ``(addressable, reason_code)`` for teammate ``to`` as seen from
    ``caller_session_id``. ``addressable`` is True only when the experimental
    flag is on, the caller's own team config is readable with the expected
    schema, does not name another session, and ``members[]`` contains exactly
    one entry whose ``name == to``."""
    if require_flag and not agent_teams_enabled():
        return False, FLAG_DISABLED
    if not to or not caller_session_id:
        return False, NO_SESSION
    path = team_config_path(caller_session_id, teams_dir=teams_dir)
    if path is None:
        return False, NO_SESSION
    try:
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            return False, CONFIG_UNREADABLE
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, CONFIG_MISSING
    except (OSError, UnicodeDecodeError):
        return False, CONFIG_UNREADABLE
    try:
        config = json.loads(raw)
    except ValueError:
        return False, CONFIG_UNREADABLE
    if not isinstance(config, dict):
        return False, SCHEMA_MISMATCH
    members = config.get("members")
    if not isinstance(members, list):
        return False, SCHEMA_MISMATCH
    names: list[str] = []
    for member in members:
        if not isinstance(member, dict):
            return False, SCHEMA_MISMATCH
        name = member.get("name")
        if not isinstance(name, str) or not name:
            return False, SCHEMA_MISMATCH
        names.append(name)
    for owner in _owning_session_ids(config):
        if owner != caller_session_id:
            return False, SESSION_MISMATCH
    matches = names.count(to)
    if matches == 0:
        return False, NAME_NOT_MEMBER
    if matches > 1:
        return False, NAME_COLLISION
    return True, FOUND
