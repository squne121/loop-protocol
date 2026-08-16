"""Issue #2196 (Child 1 of #2190): sanitized Git subprocess policy tests.

Covers `GIT_SUBPROCESS_UNSET_ENV_KEYS` and `reject_insteadof_rewrite` in
`skill_runtime_command_policy.py` (AC1 / AC4).
"""

from __future__ import annotations

import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import pytest  # noqa: E402

from skill_runtime_command_policy import (  # noqa: E402
    GIT_SUBPROCESS_UNSET_ENV_KEYS,
    GitSubprocessRewriteRejected,
    reject_insteadof_rewrite,
)


def test_git_subprocess_unset_env_keys_matches_ac1_named_set():
    """GIVEN the Issue #2196 AC1 contract list
    WHEN reading GIT_SUBPROCESS_UNSET_ENV_KEYS
    THEN it contains exactly the nine named GIT_* variables, no more, no
    fewer."""
    expected = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_COUNT",
    }
    assert GIT_SUBPROCESS_UNSET_ENV_KEYS == frozenset(expected)


def test_reject_insteadof_rewrite_raises_on_exact_key():
    """GIVEN config output containing `url.<base>.insteadof=<rewrite>`
    WHEN reject_insteadof_rewrite is called
    THEN GitSubprocessRewriteRejected is raised naming the offending key."""
    lines = ["user.name=tester", "url.https://example.com/.insteadof=git@example.com:"]
    with pytest.raises(GitSubprocessRewriteRejected) as excinfo:
        reject_insteadof_rewrite(lines)
    assert "url.https://example.com/.insteadof" in str(excinfo.value)


def test_reject_insteadof_rewrite_case_insensitive_key_match():
    """GIVEN a mixed-case insteadOf key (as `git config --list` may emit
    lowercase, but hand-authored fixtures may vary case)
    WHEN reject_insteadof_rewrite is called
    THEN it still raises (fail-closed on case, not case-sensitive bypass)."""
    lines = ["url.https://example.com/.InsteadOf=git@example.com:"]
    with pytest.raises(GitSubprocessRewriteRejected):
        reject_insteadof_rewrite(lines)


def test_reject_insteadof_rewrite_no_raise_for_unrelated_config():
    """GIVEN config output with no insteadOf entries
    WHEN reject_insteadof_rewrite is called
    THEN no exception is raised."""
    lines = [
        "user.name=tester",
        "user.email=tester@example.com",
        "core.bare=false",
        "remote.origin.url=https://example.com/repo.git",
    ]
    reject_insteadof_rewrite(lines)  # must not raise


def test_reject_insteadof_rewrite_ignores_malformed_lines():
    """GIVEN lines without an '=' separator
    WHEN reject_insteadof_rewrite is called
    THEN malformed lines are skipped rather than raising an unrelated
    exception, and a genuine insteadOf entry later in the list still
    raises."""
    lines = ["not-a-config-line", "url.https://example.com/.insteadof=x"]
    with pytest.raises(GitSubprocessRewriteRejected):
        reject_insteadof_rewrite(lines)


def test_reject_insteadof_rewrite_does_not_match_similarly_named_keys():
    """GIVEN a key that merely contains the substring "insteadof" but is
    not the exact `url.<base>.insteadof` shape
    WHEN reject_insteadof_rewrite is called
    THEN it does not raise (no over-matching)."""
    lines = ["custom.insteadoflike=value", "urlinsteadof.foo=value"]
    reject_insteadof_rewrite(lines)  # must not raise
