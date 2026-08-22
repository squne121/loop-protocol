"""scripts/agent-guards/tests/test_trusted_runtime_capability_preflight.py

Issue #2273 AC12: `trusted_runtime_capabilities.check_trusted_uv` must not
mutate git state or the filesystem. This test drives the REAL module
against the REAL repository root (no fakes/mocks for the resolver itself)
and asserts that `git status --porcelain` and the repository's tracked
file set are byte-identical before and after the call.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_GUARDS_DIR = _TESTS_DIR.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import trusted_runtime_capabilities as capabilities_mod  # noqa: E402


def _project_root() -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return Path(proc.stdout.strip())


def _git_status_porcelain(project_root: Path) -> str:
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return proc.stdout


def test_trusted_runtime_capability_preflight_is_non_mutating():
    project_root = _project_root()
    before = _git_status_porcelain(project_root)

    result = capabilities_mod.check_trusted_uv(str(project_root))

    after = _git_status_porcelain(project_root)

    assert before == after, (
        "check_trusted_uv must not change git working tree state "
        f"(before={before!r} after={after!r})"
    )
    assert result["status"] in (
        capabilities_mod.STATUS_OK,
        capabilities_mod.STATUS_MISSING,
        capabilities_mod.STATUS_VERSION_MISMATCH,
    )
    assert "reason" in result
    assert "resolved_path" in result


def test_trusted_runtime_capability_preflight_result_has_no_credential_like_keys():
    project_root = _project_root()
    result = capabilities_mod.check_trusted_uv(str(project_root))
    assert set(result.keys()) == {"status", "reason", "resolved_path"}
    for forbidden in ("token", "secret", "credential", "GH_TOKEN", "GITHUB_TOKEN"):
        assert forbidden not in str(result.get("reason", ""))
