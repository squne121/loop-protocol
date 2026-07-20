"""Regression coverage for Issue #1562 AC4's github_live collection split."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_LIVE_TEST_FILE = (
    _REPOSITORY_ROOT
    / ".claude/skills/impl-review-loop/tests/test_ensure_contract_snapshot_fingerprint_patch.py"
)


def _collect(*extra_args: str) -> subprocess.CompletedProcess[str]:
    """Collect the live-test module in a child pytest process without running it."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(_LIVE_TEST_FILE),
            "--collect-only",
            "-q",
            *extra_args,
        ],
        cwd=_REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_github_live_marker_default_collect_deselects() -> None:
    """Default collection must deselect live tests; explicit marker collects both."""
    default = _collect()
    default_output = default.stdout + default.stderr
    assert default.returncode == 5, default_output
    assert "2 deselected" in default_output, default_output

    github_live = _collect("-m", "github_live")
    github_live_output = github_live.stdout + github_live.stderr
    assert github_live.returncode == 0, github_live_output
    collected = re.search(r"(\d+) tests? collected", github_live_output)
    assert collected is not None, github_live_output
    assert int(collected.group(1)) >= 2, github_live_output
