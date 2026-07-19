#!/usr/bin/env python3
"""PROTECTED_PATHS_POLICY_V1 loader (Issue #1611 AC10, contract revision P1-2).

`scripts/agent-guards/protected_paths_policy.v1.json` is the single,
language-independent source of truth for repository paths that must always
be denied for AI-driven staging/commit, regardless of what an Issue's
declared Allowed Paths say. This module is a thin Python loader over that
JSON -- it does NOT hardcode the rule list. Other consumers (Node
`scripts/check-codex-agents.mjs`, `.codex/config.toml`) are expected to
either read the same JSON file directly, or keep a validated mirror in sync
with it (see `docs/dev/agent-runtime-ops.md` for the mirror verification
procedure).

Protected paths are denied even when an Issue's Allowed Paths explicitly
lists them -- Allowed Paths can never widen access to a protected path.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_AGENT_GUARDS_DIR = Path(__file__).resolve().parent
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))

from changed_file_matcher import AllowedPathsMatcher  # noqa: E402

POLICY_FILE = _AGENT_GUARDS_DIR / "protected_paths_policy.v1.json"

# Kept for backward compatibility with pre-JSON-SSOT consumers that bind to
# this exact schema string (Issue #1611 original impl). Never bump this
# independent of the JSON file's `schema` key -- they must always match.
POLICY_VERSION = "PROTECTED_PATHS_POLICY_V1"

_RULE_KIND_ROOT_DIRECTORY = "root_directory"
_RULE_KIND_BASENAME_GLOB = "basename_glob"
_SUPPORTED_RULE_KINDS = frozenset({_RULE_KIND_ROOT_DIRECTORY, _RULE_KIND_BASENAME_GLOB})


def _load_policy_raw_bytes(policy_file: Path = POLICY_FILE) -> bytes:
    return policy_file.read_bytes()


def compute_policy_sha256(policy_file: Path = POLICY_FILE) -> str:
    """sha256 of the JSON file's raw *content* (Issue #1611 contract
    revision AC1: `protected_paths_policy_sha256` binds to policy content,
    not a version string -- so a silent edit to the rule list is detected
    even if `POLICY_VERSION` / `schema` are left unchanged)."""
    return hashlib.sha256(_load_policy_raw_bytes(policy_file)).hexdigest()


def load_policy(policy_file: Path = POLICY_FILE) -> Dict[str, Any]:
    raw = _load_policy_raw_bytes(policy_file)
    data = json.loads(raw.decode("utf-8"))
    if data.get("schema") != POLICY_VERSION:
        raise ValueError(f"unexpected protected paths policy schema: {data.get('schema')!r}")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("protected paths policy JSON must declare a non-empty 'rules' list")
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("kind") not in _SUPPORTED_RULE_KINDS:
            raise ValueError(f"unsupported protected paths policy rule: {rule!r}")
    return data


_POLICY_DATA = load_policy()
POLICY_SCHEMA = _POLICY_DATA["schema"]
POLICY_RULES: tuple = tuple(_POLICY_DATA["rules"])
POLICY_SHA256 = compute_policy_sha256()

# Human-readable pattern list, derived from POLICY_RULES (kept for
# documentation / validated-mirror comparison in other languages -- never
# independently hand-edited).
PROTECTED_PATH_PATTERNS: tuple = tuple(
    sorted(
        {
            (f"{rule['path']}/**" if rule["kind"] == _RULE_KIND_ROOT_DIRECTORY else rule["pattern"])
            for rule in POLICY_RULES
        }
        | {f"**/{rule['pattern']}" for rule in POLICY_RULES if rule["kind"] == _RULE_KIND_BASENAME_GLOB}
    )
)

_ROOT_DIRECTORY_PREFIXES: tuple = tuple(
    f"{rule['path']}/" for rule in POLICY_RULES if rule["kind"] == _RULE_KIND_ROOT_DIRECTORY
)
_BASENAME_GLOBS: tuple = tuple(rule["pattern"] for rule in POLICY_RULES if rule["kind"] == _RULE_KIND_BASENAME_GLOB)


def _normalize(file_path: str) -> Optional[str]:
    return AllowedPathsMatcher.normalize_path(file_path)


def _basename_matches_glob(basename: str, pattern: str) -> bool:
    """Minimal, fail-closed basename glob matcher: only a trailing `*` is
    supported (e.g. `.env.*`); any other glob character is a policy-file
    authoring error and is treated as literal (no match unless equal)."""
    if pattern.endswith("*") and "*" not in pattern[:-1]:
        return basename.startswith(pattern[:-1])
    return basename == pattern


def is_protected_path(file_path: str) -> bool:
    """Return True iff `file_path` (repo-relative) matches a protected path
    rule from `protected_paths_policy.v1.json` (Issue #1611 AC10): a
    `root_directory` rule (any depth under that top-level directory), or a
    `basename_glob` rule (matched against the final path segment, at any
    depth). An unparseable/invalid path is treated fail-closed as NOT
    protected here -- callers that stage an invalid path are already denied
    upstream by the pathspec/Allowed Paths checks; this function only
    answers "is this specific normalized path protected"."""
    normalized = _normalize(file_path)
    if normalized is None:
        return False
    if any(normalized.startswith(prefix) for prefix in _ROOT_DIRECTORY_PREFIXES):
        return True
    basename = normalized.rsplit("/", 1)[-1]
    return any(_basename_matches_glob(basename, pattern) for pattern in _BASENAME_GLOBS)


def filter_protected_paths(file_paths: List[str]) -> List[str]:
    """Return the subset of `file_paths` that are protected."""
    return [path for path in file_paths if is_protected_path(path)]
