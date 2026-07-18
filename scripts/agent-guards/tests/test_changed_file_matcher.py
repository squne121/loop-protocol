from __future__ import annotations

import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import pytest

from changed_file_matcher import (
    AllowedPathsMatcher,
    ChangedFileRecord,
    parse_git_diff_name_status_z,
)


def test_normalize_path_rejects_traversal_and_absolute():
    assert AllowedPathsMatcher.normalize_path("../etc/passwd") is None
    assert AllowedPathsMatcher.normalize_path("/etc/passwd") is None
    assert AllowedPathsMatcher.normalize_path("a\\b") is None
    assert AllowedPathsMatcher.normalize_path("./src/x.py") == "src/x.py"


def test_normalize_allowed_pattern_directory_shorthand():
    assert AllowedPathsMatcher.normalize_allowed_pattern("src/ui/") == "src/ui/**"
    assert AllowedPathsMatcher.normalize_allowed_pattern("src//") is None
    assert AllowedPathsMatcher.normalize_allowed_pattern("src/*.md") is None


def test_matches_pattern_double_star_mid_path():
    assert AllowedPathsMatcher.matches_pattern("a/b/c/d.py", "a/**/d.py")
    assert not AllowedPathsMatcher.matches_pattern("a/b/c/d.py", "a/**/e.py")


def test_is_file_allowed_uses_normalized_patterns():
    allowed = ["scripts/agent-guards/", "docs/dev/hook-boundaries.md"]
    assert AllowedPathsMatcher.is_file_allowed("scripts/agent-guards/foo.py", allowed)
    assert AllowedPathsMatcher.is_file_allowed("docs/dev/hook-boundaries.md", allowed)
    assert not AllowedPathsMatcher.is_file_allowed("docs/dev/other.md", allowed)


def test_parse_git_diff_name_status_z_rename_record():
    raw = "R100\0old/path.py\0new/path.py\0"
    records = parse_git_diff_name_status_z(raw, source="test")
    assert records == [
        ChangedFileRecord(
            path="new/path.py",
            status="renamed",
            previous_path="old/path.py",
            source="test",
            provenance_complete=True,
        )
    ]


def test_parse_git_diff_name_status_z_unknown_status_raises():
    with pytest.raises(ValueError):
        parse_git_diff_name_status_z("Z\0path.py\0", source="test")
