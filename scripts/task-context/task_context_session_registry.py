"""Task Context v1 — read-only Claude Code native session registry
consumer (Issue #2566 fix_delta P1-B, PR #2691 OWNER review 2026-09-21).

Issue #2567 AC5 (OWNER anchor comment P1 blocker, 2026-09-21): Claude-GPT's
launcher-owned isolated ``CLAUDE_CONFIG_DIR`` relocates the on-disk session
registry this module reads (Claude Code moves ``~/.claude/sessions`` under
``CLAUDE_CONFIG_DIR`` when that env var is set). ``resolve_session_registry_dir()``
therefore resolves, in order: (1) ``LOOP_TASK_CONTEXT_SESSION_REGISTRY_DIR``
override (tests only), (2) ``CLAUDE_CONFIG_DIR/sessions`` when
``CLAUDE_CONFIG_DIR`` is set (covers both Claude-GPT's isolated config root
and any Native invocation that also sets ``CLAUDE_CONFIG_DIR``), (3) the
pre-existing ``~/.claude/sessions`` default.

Claude Code itself maintains its own on-disk session registry under
``~/.claude/sessions/<pid>.json`` -- one JSON record per Claude Code
session, independently confirmed (real on-machine sample, plus
https://code.claude.com/docs/en/cross-session-messaging) to carry at least
``pid, sessionId, cwd, startedAt, procStart, version, peerProtocol,
peerFeatures, kind, entrypoint, pidDomain, messagingSocketPath, name,
nameSource, nameSince, status, updatedAt, statusUpdatedAt``. Per that same
independent investigation, a real ``SendMessage``/``notify_when_idle``
``to`` value addresses a session by that ``name`` field (a unique name
resolves on its own; a colliding name is disambiguated upstream by a short
identifier suffix whose exact derivation this fix_delta's investigation did
not confirm -- see the "Known limitation" note below).

This module performs a **read-only** filesystem scan of that *existing*,
Claude-Code-owned registry to resolve a ``name`` to a ``sessionId``. It:

- never writes to the registry (read-only ``glob``/``read_text`` only),
- never invents a new peer registry/router/message queue of its own --
  Claude Code already owns and maintains this one; this module only
  *reads* it,
- performs no DB access and no subprocess/Herdr I/O (Task Context's own DB
  access rules stay in ``task_context_service.py`` /
  ``task_context_hook_flows.py``, unchanged by this module),
- never reads or forwards message body / terminal output / any field
  beyond ``name``/``sessionId`` into EventJournal or anywhere else (AC7).

Known limitation (Issue #2566 fix_delta P1-B, intentionally out of scope):
short-identifier collision disambiguation is **not** implemented. When more
than one on-disk session record's ``name`` field matches ``to``, this
module reports the name as unresolved (``None``) rather than guessing which
record ``to`` actually means -- Claude Code's own exact short-identifier
derivation algorithm for that collision case is not confirmed by this
fix_delta's investigation, and a wrong guess would silently misroute a
cross-Task guard decision to the wrong peer, which is worse than falling
through to the existing ``unknown_independent_session`` -> ASK fail-safe
that callers already apply when this function returns ``None``.
"""

from __future__ import annotations

import json
import os
import pathlib

SESSION_REGISTRY_DIR_ENV_VAR = "LOOP_TASK_CONTEXT_SESSION_REGISTRY_DIR"
CLAUDE_CONFIG_DIR_ENV_VAR = "CLAUDE_CONFIG_DIR"


def _default_session_registry_dir() -> pathlib.Path:
    """Issue #2567 AC5: prefer ``CLAUDE_CONFIG_DIR/sessions`` when
    ``CLAUDE_CONFIG_DIR`` is set (non-empty) -- this is the directory Claude
    Code's own session registry actually lives under for any invocation that
    sets a custom config dir (Claude-GPT's isolated profile being the
    motivating case). Falls back to ``~/.claude/sessions`` only when
    ``CLAUDE_CONFIG_DIR`` is unset/empty, unchanged from prior behavior."""
    claude_config_dir = os.environ.get(CLAUDE_CONFIG_DIR_ENV_VAR, "")
    if claude_config_dir:
        return pathlib.Path(claude_config_dir) / "sessions"
    return pathlib.Path.home() / ".claude" / "sessions"


def resolve_session_registry_dir() -> pathlib.Path:
    """Resolve the on-disk session registry directory to scan.

    Precedence (Issue #2567 AC5):
    1. ``LOOP_TASK_CONTEXT_SESSION_REGISTRY_DIR`` override (tests only --
       mirrors the sibling ``task_context_config.resolve_state_root``
       override contract: used as-is, never silently resolved against
       ``cwd``).
    2. ``CLAUDE_CONFIG_DIR/sessions`` when ``CLAUDE_CONFIG_DIR`` is set.
    3. ``~/.claude/sessions`` (Claude Code's real default registry
       location)."""
    override = os.environ.get(SESSION_REGISTRY_DIR_ENV_VAR, "")
    if override:
        return pathlib.Path(override)
    return _default_session_registry_dir()


def _iter_session_records(registry_dir: pathlib.Path):
    """Yield every parseable JSON object found under ``registry_dir``.
    Fail-closed: a missing directory, an unreadable file, or invalid JSON
    is silently skipped -- never raised -- so a corrupt/absent registry
    degrades to "no records" rather than propagating an exception into the
    PreToolUse guard hot path."""
    try:
        entries = sorted(registry_dir.glob("*.json"))
    except OSError:
        return
    for entry in entries:
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if isinstance(record, dict):
            yield record


def resolve_session_name_to_claude_session_id(
    to: str | None, *, registry_dir: pathlib.Path | None = None
) -> str | None:
    """Resolve a ``SendMessage``/``notify_when_idle`` ``to`` value against
    Claude Code's own on-disk session registry ``name`` field.

    Returns the matching record's ``sessionId`` when exactly one on-disk
    record's ``name`` equals ``to``. Returns ``None`` (unresolved,
    fail-safe) when: ``to`` is empty; the registry directory does not
    exist or cannot be read; no record's ``name`` matches; or more than one
    record's ``name`` matches (an intentionally unresolved short-identifier
    collision -- see module docstring).

    Read-only, filesystem-only (no DB access, no subprocess/Herdr I/O),
    fail-closed on any I/O/parse error -- never raises."""
    if not to:
        return None
    directory = registry_dir if registry_dir is not None else resolve_session_registry_dir()
    matches: list[str] = []
    for record in _iter_session_records(directory):
        name = record.get("name")
        session_id = record.get("sessionId")
        if name == to and isinstance(session_id, str) and session_id:
            matches.append(session_id)
    if len(matches) != 1:
        return None
    return matches[0]
