#!/usr/bin/env python3
"""PROTECTED_PATHS_POLICY_V1 (Issue #1611 AC10).

Single, language-independent source of truth for repository paths that
must always be denied for AI-driven staging/commit, regardless of what an
Issue's declared Allowed Paths say. This module is the ONE place the
protected-path pattern list is defined -- `controlled_git_change_exec.py`
consumes it for staging/commit denial, and other consumers (Node/Python,
`.codex/config.toml`, `.claude/settings.json`) are expected to keep a
validated mirror of `PROTECTED_PATH_PATTERNS` / `POLICY_VERSION` in sync
with this file (see `docs/dev/agent-runtime-ops.md` for the mirror
verification procedure).

Protected paths are denied even when an Issue's Allowed Paths explicitly
lists them -- Allowed Paths can never widen access to a protected path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

_AGENT_GUARDS_DIR = Path(__file__).resolve().parent
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))

from changed_file_matcher import AllowedPathsMatcher  # noqa: E402

# Bump this string whenever PROTECTED_PATH_PATTERNS / the matching rules
# below change -- consumers (controlled_git_change_exec.py, validated
# mirrors) bind to this exact version string so a stale mirror can be
# detected.
POLICY_VERSION = "PROTECTED_PATHS_POLICY_V1"

# Human-readable pattern list (kept for documentation / validated-mirror
# comparison in other languages, e.g. `.codex/config.toml`,
# `.claude/settings.json`, Node consumers). `.env.*` / `**/.env.*` use a
# partial-segment prefix glob that the strict Allowed Paths matcher v2
# grammar (`AllowedPathsMatcher`, `scripts/agent-guards/changed_file_matcher.py`)
# intentionally rejects (fail-closed) as a segment pattern -- so protected
# paths are evaluated by the dedicated `is_protected_path()` below, not by
# `AllowedPathsMatcher.is_file_allowed()`.
PROTECTED_PATH_PATTERNS: tuple[str, ...] = (
    "assets/**",
    "LICENSES/**",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "secrets/**",
)

_PROTECTED_DIRECTORY_PREFIXES: tuple[str, ...] = ("assets/", "LICENSES/", "secrets/")
_DOTENV_BASENAME = ".env"
_DOTENV_PREFIX = ".env."


def _normalize(file_path: str) -> Optional[str]:
    return AllowedPathsMatcher.normalize_path(file_path)


def is_protected_path(file_path: str) -> bool:
    """Return True iff `file_path` (repo-relative) matches a protected path
    pattern (Issue #1611 AC10): `assets/**`, `LICENSES/**`, `secrets/**`
    (any depth under those top-level directories), or a dotenv file
    (`.env` or `.env.<suffix>`) at any depth. An unparseable/invalid path
    is treated fail-closed as NOT protected here -- callers that stage an
    invalid path are already denied upstream by the pathspec/Allowed Paths
    checks; this function only answers "is this specific normalized path
    protected"."""
    normalized = _normalize(file_path)
    if normalized is None:
        return False
    if any(normalized.startswith(prefix) for prefix in _PROTECTED_DIRECTORY_PREFIXES):
        return True
    basename = normalized.rsplit("/", 1)[-1]
    if basename == _DOTENV_BASENAME:
        return True
    if basename.startswith(_DOTENV_PREFIX):
        return True
    return False


def filter_protected_paths(file_paths: List[str]) -> List[str]:
    """Return the subset of `file_paths` that are protected."""
    return [path for path in file_paths if is_protected_path(path)]
