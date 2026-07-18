#!/usr/bin/env python3
"""PROTECTED_PATHS_POLICY_V1 -- single, language-independent protected-paths
policy (Issue #1611).

Protected paths are denied for agent-lane staging/commit regardless of what
an Issue's `## Allowed Paths` section says. Issue Allowed Paths can never
widen this policy; only a human editing this file (and its validated
mirrors) can.

Validated mirrors (kept in sync manually, checked by
`test_protected_paths_mirrors_in_sync` style tests in consumer test suites):
  - `.codex/config.toml` (Codex CLI sandbox writable-roots exclusions)
  - `.claude/settings.json` (Claude Code Edit/Write deny patterns)
  - Node/Python consumers that need the same policy at runtime import this
    module (Python) or must keep an equivalent frozen literal list in sync
    (Node), and are covered by a regression test.
"""

from __future__ import annotations

import posixpath
from typing import Iterable, List

from changed_file_matcher import AllowedPathsMatcher

PROTECTED_PATHS_POLICY_VERSION = "PROTECTED_PATHS_POLICY_V1"

# Directory-prefix patterns (matcher v2 grammar). Kept in the shared matcher
# grammar so these are validated by the same fail-closed normalizer as
# Allowed Paths. "**/secrets/**" matches `secrets/` anywhere in the tree
# (mid-path "**"), not just at the repo root.
PROTECTED_DIRECTORY_PATTERNS: tuple[str, ...] = (
    "assets/",
    "LICENSES/",
    "secrets/",
    "**/secrets/**",
)

# dotenv family: matcher v2 grammar forbids partial-segment globs (`.env.*`
# is not representable as a literal/`*`/`**` segment), so dotenv detection
# is a dedicated basename-prefix check rather than a matcher pattern.
_DOTENV_BASENAMES: tuple[str, ...] = (".env",)
_DOTENV_PREFIX = ".env."


def _is_dotenv_basename(basename: str) -> bool:
    return basename in _DOTENV_BASENAMES or basename.startswith(_DOTENV_PREFIX)


def is_protected_path(file_path: str) -> bool:
    """Return True if `file_path` (repo-relative) matches the protected paths
    policy. Deny-always: Issue Allowed Paths cannot override this."""
    normalized = AllowedPathsMatcher.normalize_path(file_path)
    if normalized is None:
        # Fail-closed: an unparseable path is treated as protected so it is
        # never silently allowed through.
        return True
    if _is_dotenv_basename(posixpath.basename(normalized)):
        return True
    return AllowedPathsMatcher.is_file_allowed(normalized, list(PROTECTED_DIRECTORY_PATTERNS))


def any_protected(file_paths: Iterable[str]) -> List[str]:
    """Return the subset of `file_paths` that are protected (order preserved)."""
    return [path for path in file_paths if is_protected_path(path)]
