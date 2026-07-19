"""Issue #1612 AC4: isProtectedPath() in scripts/check-codex-agents.mjs must be
a validated mirror of the Python PROTECTED_PATHS_POLICY_V1 loader
(scripts/agent-guards/protected_paths_policy.py) -- both consumers read the
same JSON SSOT (scripts/agent-guards/protected_paths_policy.v1.json), so this
test drives both implementations with the exact same candidate paths and
asserts their is-protected decisions never diverge.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _GUARDS_DIR.parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from protected_paths_policy import is_protected_path  # noqa: E402

_CHECK_SCRIPT = _REPO_ROOT / "scripts" / "check-codex-agents.mjs"

_NODE_BIN = shutil.which("node")

_CANDIDATE_PATHS = [
    # assets/ (root_directory rule)
    "assets/sprite.png",
    "assets/sub/dir/file.txt",
    # LICENSES/ (root_directory rule)
    "LICENSES/MIT.txt",
    "LICENSES/nested/APACHE-2.0.txt",
    # .env / .env.* (basename_glob rules)
    ".env",
    ".env.production",
    ".env.local",
    "config/.env",
    "config/.env.production",
    # secrets/ (root_directory rule)
    "secrets/api_key.txt",
    "secrets/nested/deep/token",
    # non-protected paths
    "docs/readme.md",
    "scripts/agent-guards/controlled_git_change_exec.py",
    "src/main.ts",
    "environment.py",
    "myenvvar.txt",
    "notasecrets/file.txt",
    "notassets/file.txt",
]


def _node_is_protected(candidate: str) -> bool:
    result = subprocess.run(  # noqa: S603
        [_NODE_BIN, str(_CHECK_SCRIPT), "--check-protected-path", candidate],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=20,
        check=True,
    )
    output = result.stdout.strip()
    assert output in ("true", "false"), f"unexpected node output: {output!r}"
    return output == "true"


@pytest.mark.skipif(_NODE_BIN is None, reason="node binary not found on PATH")
@pytest.mark.parametrize("candidate", _CANDIDATE_PATHS)
def test_node_python_protected_path_parity(candidate: str) -> None:
    python_decision = is_protected_path(candidate)
    node_decision = _node_is_protected(candidate)
    assert node_decision == python_decision, (
        f"protected-path decision mismatch for {candidate!r}: "
        f"python={python_decision} node={node_decision}"
    )
