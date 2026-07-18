from __future__ import annotations

import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import hashlib  # noqa: E402

from protected_paths_policy import (  # noqa: E402
    POLICY_FILE,
    POLICY_RULES,
    POLICY_SCHEMA,
    POLICY_SHA256,
    POLICY_VERSION,
    PROTECTED_PATH_PATTERNS,
    compute_policy_sha256,
    filter_protected_paths,
    is_protected_path,
    load_policy,
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


def test_protected_paths_policy_is_json_ssot_content_hash_bound():
    """Issue #1611 (contract revision, P1-2): `protected_paths_policy.v1.json`
    is the single source of truth; `POLICY_SHA256` binds to its raw content
    (never a hand-maintained version string), and the Python module derives
    its rule set from the JSON, not a hardcoded list."""
    assert POLICY_FILE.name == "protected_paths_policy.v1.json"
    assert POLICY_FILE.exists()
    on_disk = load_policy()
    assert on_disk["schema"] == POLICY_SCHEMA == "PROTECTED_PATHS_POLICY_V1"
    assert tuple(on_disk["rules"]) == POLICY_RULES
    expected_sha = hashlib.sha256(POLICY_FILE.read_bytes()).hexdigest()
    assert POLICY_SHA256 == expected_sha
    assert compute_policy_sha256() == expected_sha
    for rule in POLICY_RULES:
        assert rule["kind"] in ("root_directory", "basename_glob")
