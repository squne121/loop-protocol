from __future__ import annotations

import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from protected_paths_policy import (
    PROTECTED_PATHS_POLICY_VERSION,
    any_protected,
    is_protected_path,
)


def test_protected_paths_deny_regardless_of_allowed_paths():
    """AC10: protected paths (assets/, LICENSES/, dotenv files, secrets/) are
    always denied, even if an Issue's Allowed Paths explicitly lists them."""
    protected_candidates = [
        "assets/sprites/hero.png",
        "LICENSES/MIT.txt",
        ".env",
        ".env.production",
        "secrets/api_key.pem",
        "nested/secrets/inner.json",
    ]
    for candidate in protected_candidates:
        assert is_protected_path(candidate), candidate

    # Even when the Allowed Paths matcher (a *separate* mechanism) would
    # otherwise allow the directory-shaped protected candidates, protected
    # paths is a deny-always layer that Allowed Paths cannot widen.
    from changed_file_matcher import AllowedPathsMatcher

    allowed_paths = ["assets/", "LICENSES/", ".env", "secrets/"]
    directory_shaped = [
        "assets/sprites/hero.png",
        "LICENSES/MIT.txt",
        ".env",
        "secrets/api_key.pem",
    ]
    for candidate in directory_shaped:
        assert AllowedPathsMatcher.is_file_allowed(candidate, allowed_paths)
        assert is_protected_path(candidate)


def test_protected_paths_does_not_flag_ordinary_scripts_paths():
    assert not is_protected_path("scripts/agent-guards/controlled_git_change_exec.py")
    assert not is_protected_path("docs/dev/agent-runtime-ops.md")


def test_protected_paths_version_is_stable_identifier():
    assert PROTECTED_PATHS_POLICY_VERSION == "PROTECTED_PATHS_POLICY_V1"


def test_any_protected_returns_only_matching_subset():
    candidates = ["scripts/agent-guards/x.py", "assets/img.png", "docs/dev/hook-boundaries.md"]
    assert any_protected(candidates) == ["assets/img.png"]
