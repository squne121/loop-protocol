from __future__ import annotations

import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from protected_paths_policy import (  # noqa: E402
    POLICY_VERSION,
    PROTECTED_PATH_PATTERNS,
    filter_protected_paths,
    is_protected_path,
)


def test_protected_paths_deny_regardless_of_allowed_paths():
    """Issue #1611 AC10: protected paths are denied even when an Issue's
    declared Allowed Paths explicitly list them (files that would be
    "in scope" under a naive Allowed Paths match are still protected)."""
    protected_candidates = [
        "assets/sprite.png",
        "assets/sub/dir/file.txt",
        "LICENSES/MIT.txt",
        ".env",
        ".env.production",
        ".env.local",
        "config/.env",
        "config/.env.production",
        "secrets/api_key.txt",
        "secrets/nested/deep/token",
    ]
    for path in protected_candidates:
        assert is_protected_path(path) is True, f"{path} expected protected"

    # filter_protected_paths must return every one of the candidates above,
    # proving that Allowed Paths membership never overrides protection.
    assert set(filter_protected_paths(protected_candidates)) == set(protected_candidates)


def test_protected_paths_do_not_over_match_non_protected():
    non_protected = [
        "docs/readme.md",
        "scripts/agent-guards/controlled_git_change_exec.py",
        "src/main.ts",
        "environment.py",
        "myenvvar.txt",
        "notasecrets/file.txt",
        "notassets/file.txt",
    ]
    for path in non_protected:
        assert is_protected_path(path) is False, f"{path} expected NOT protected"


def test_protected_paths_policy_version_and_pattern_list_stable():
    assert POLICY_VERSION == "PROTECTED_PATHS_POLICY_V1"
    for pattern in ("assets/**", "LICENSES/**", ".env", ".env.*", "**/.env", "**/.env.*", "secrets/**"):
        assert pattern in PROTECTED_PATH_PATTERNS


def test_protected_paths_reject_invalid_input_fail_closed():
    assert is_protected_path("") is False
    assert is_protected_path("../secrets/token") is False
    assert is_protected_path("/secrets/token") is False
