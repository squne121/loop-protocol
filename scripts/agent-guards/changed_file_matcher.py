#!/usr/bin/env python3
"""Shared changed-file matcher / normalizer / ChangedFileRecord module.

Issue #1611: the Allowed Paths matcher grammar (`AllowedPathsMatcher`), the
repo-relative path normalizer, and the rename-aware `ChangedFileRecord`
structure previously lived only inside
`.claude/skills/pr-review-judge/scripts/allowed_paths_review_gate.py`. This
module is the single shared source of truth so that staging (`rtk git add`
equivalent, `controlled_git_change_exec.py`), commit, and PR review all use
the exact same grammar (AC11). Consumers MUST import from here rather than
re-implementing the matcher locally.

`scripts/` has a zero-external-dependency constraint (stdlib only).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

# Issue #1300: git diff --name-status letter -> canonical status name.
# NOTE: "D" maps to "removed" (not "deleted") to match the pre-existing
# `allowed_paths_review_gate.py` vocabulary this module was extracted from
# (Issue #1611 AC11 -- a single shared grammar, never two diverging ones).
_GIT_STATUS_LETTER_MAP: Dict[str, str] = {
    "A": "added",
    "M": "modified",
    "D": "removed",
    "T": "type_changed",
    "U": "unmerged",
    "X": "unknown",
    "B": "broken_pairing",
}


@dataclass
class ChangedFileRecord:
    """Structured changed-file record with rename/previous-path provenance.

    Represents either a `git diff --name-status -M -z` record (local
    deterministic fallback) or a GitHub PR files API record (preferred
    oracle, when supplied via --pr-files-json).
    """

    path: str
    status: str
    previous_path: Optional[str]
    source: str
    provenance_complete: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AllowedPathsMatcher:
    """Matches repo-relative POSIX paths against the restricted Allowed Paths grammar.

    matcher v2 grammar (shared by staging / commit / review):
      - literal segment  -> exact segment match
      - `*`              -> exactly one path segment
      - `**`             -> zero or more path segments (mid-path allowed)
      - trailing `/`      -> directory-prefix shorthand, normalized to `.../**`
      - partial-segment globs (`*.md`, `foo*`, `**suffix`, `***`) are invalid
        (fail-closed; never silently treated as literal or as a broader glob)
    """

    @staticmethod
    def normalize_path(path: str) -> Optional[str]:
        """Normalize repo-relative paths and reject invalid input."""
        if not path:
            return None
        if "\\" in path:
            return None
        if path.startswith("/"):
            return None

        normalized = path[2:] if path.startswith("./") else path
        if normalized in {"", "."}:
            return None
        segments = normalized.split("/")
        if ".." in segments:
            return None
        if "" in segments:
            return None
        return normalized

    @staticmethod
    def normalize_allowed_pattern(pattern: str) -> Optional[str]:
        # Trailing-slash patterns like "src/ui/" are treated as directory prefixes
        # and normalized to "src/ui/**". Wildcard + trailing-slash is invalid.
        if pattern.endswith("/"):
            bare = pattern[:-1]
            # Reject repeated trailing slash (e.g. "src//") or wildcard + trailing-slash
            if bare.endswith("/") or "*" in bare:
                return None
            normalized_bare = AllowedPathsMatcher.normalize_path(bare)
            if normalized_bare is None:
                return None
            return normalized_bare + "/**"
        normalized = AllowedPathsMatcher.normalize_path(pattern)
        if normalized is None:
            return None
        for segment in normalized.split("/"):
            if "*" in segment and segment not in ("*", "**"):
                return None
        return normalized

    @staticmethod
    def matches_pattern(file_path: str, pattern: str) -> bool:
        file_parts = file_path.split("/")
        pattern_parts = pattern.split("/")
        return AllowedPathsMatcher._segment_match(file_parts, pattern_parts)

    @staticmethod
    def _segment_match(file_parts: List[str], pattern_parts: List[str]) -> bool:
        n = len(file_parts)
        m = len(pattern_parts)
        # dp[i][j] is True iff file_parts[i:] matches pattern_parts[j:].
        dp = [[False] * (m + 1) for _ in range(n + 1)]
        dp[n][m] = True
        for j in range(m - 1, -1, -1):
            if pattern_parts[j] == "**":
                dp[n][j] = dp[n][j + 1]
        for i in range(n - 1, -1, -1):
            for j in range(m - 1, -1, -1):
                segment = pattern_parts[j]
                if segment == "**":
                    dp[i][j] = dp[i][j + 1] or dp[i + 1][j]
                elif segment == "*":
                    dp[i][j] = dp[i + 1][j + 1]
                else:
                    dp[i][j] = segment == file_parts[i] and dp[i + 1][j + 1]
        return dp[0][0]

    @staticmethod
    def is_file_allowed(file_path: str, allowed_paths: List[str]) -> bool:
        normalized_file = AllowedPathsMatcher.normalize_path(file_path)
        if normalized_file is None:
            return False

        for pattern in allowed_paths:
            normalized_pattern = AllowedPathsMatcher.normalize_allowed_pattern(pattern)
            if normalized_pattern is None:
                continue
            if AllowedPathsMatcher.matches_pattern(normalized_file, normalized_pattern):
                return True
        return False


def parse_git_diff_name_status_z(raw: str, source: str) -> List[ChangedFileRecord]:
    """Parse `git diff --name-status -M -z` (or -C) output into ChangedFileRecord list.

    NUL-separated tokens. Non-rename/copy records: "<status>\\0<path>\\0".
    Rename/copy records: "<status><score>\\0<old_path>\\0<new_path>\\0".
    Raises ValueError on malformed status, missing paths, or unknown status
    (fail-closed -- caller must treat this as indeterminate, never as a
    filename-only fallback).
    """
    tokens = [tok for tok in raw.split("\0") if tok != ""]
    records: List[ChangedFileRecord] = []
    i = 0
    while i < len(tokens):
        status_token = tokens[i]
        i += 1
        if not status_token:
            raise ValueError("malformed git diff --name-status record: empty status token")
        letter = status_token[0]
        if letter in ("R", "C"):
            if i + 1 >= len(tokens):
                raise ValueError(
                    f"malformed rename/copy record for status {status_token!r}: missing old/new path"
                )
            old_path = tokens[i]
            new_path = tokens[i + 1]
            i += 2
            if not old_path or not new_path:
                raise ValueError(f"malformed rename/copy record: empty path for status {status_token!r}")
            status_name = "renamed" if letter == "R" else "copied"
            records.append(
                ChangedFileRecord(
                    path=new_path,
                    status=status_name,
                    previous_path=old_path,
                    source=source,
                    provenance_complete=True,
                )
            )
        elif letter in _GIT_STATUS_LETTER_MAP:
            if i >= len(tokens):
                raise ValueError(f"malformed record for status {status_token!r}: missing path")
            path = tokens[i]
            i += 1
            if not path:
                raise ValueError(f"malformed record: empty path for status {status_token!r}")
            records.append(
                ChangedFileRecord(
                    path=path,
                    status=_GIT_STATUS_LETTER_MAP[letter],
                    previous_path=None,
                    source=source,
                    provenance_complete=True,
                )
            )
        else:
            raise ValueError(f"unknown git diff --name-status status: {status_token!r}")
    return records
