"""Regression coverage for Issue #2361 AC3's exact-nodeid marker
collection split for `test_credentialless_public_issue_read_succeeds`
(the credentialless GitHub read live positive control, Issue #2241 AC3).

Unlike the existing count-based `test_github_live_marker_collection.py`
(Issue #1562 AC4), this file asserts against the single, exact nodeid
`test_credentialless_public_issue_read_succeeds[1]` -- a count-only
assertion (e.g. "N deselected") would still pass if the marker were
attached to the wrong test, or if collection totals shifted for an
unrelated reason (OWNER review comment on Issue #2361, "AC3 の件数ベースの
regression の弱さ"). The existing `test_github_live_marker_collection.py`
is left unmodified, per the Issue #2361 contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_LIVE_TEST_FILE = (
    _REPOSITORY_ROOT
    / "scripts"
    / "agent-guards"
    / "tests"
    / "test_credentialless_github_read.py"
)
_TARGET_NODEID = (
    "scripts/agent-guards/tests/test_credentialless_github_read.py"
    "::test_credentialless_public_issue_read_succeeds[1]"
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


def test_default_collection_deselects_exact_nodeid() -> None:
    """GIVEN the credentialless-read live test file, which also contains
    many non-`github_live` tests
    WHEN pytest collects it with the repository's default addopts
    (`-m 'not github_live and not claude_live'`)
    THEN the exact nodeid `test_credentialless_public_issue_read_succeeds[1]`
    is not present in the collection output, while collection itself still
    succeeds (Issue #2361 AC1/AC3)."""
    result = _collect()
    output = result.stdout + result.stderr
    assert _TARGET_NODEID not in output, output
    assert result.returncode == 0, output


def test_github_live_marker_collects_exact_nodeid() -> None:
    """GIVEN the credentialless-read live test file
    WHEN pytest collects it with `-m github_live`
    THEN the exact nodeid `test_credentialless_public_issue_read_succeeds[1]`
    is present in the collection output (Issue #2361 AC1/AC3)."""
    result = _collect("-m", "github_live")
    output = result.stdout + result.stderr
    assert _TARGET_NODEID in output, output
    assert result.returncode == 0, output
