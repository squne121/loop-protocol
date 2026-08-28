"""Issue #2196 (Child 1 of #2190): sanitized Git subprocess policy tests.

Covers `GIT_SUBPROCESS_UNSET_ENV_KEYS`, `reject_insteadof_rewrite`,
`parse_config_get_regexp_name_only_nul`, `reject_git_global_options`, and
`reject_option_like_positional` in `skill_runtime_command_policy.py`
(AC1 / AC4 / AC6 / AC8 / AC9). Reflects the PR #2201 owner adversarial
review fix delta (P1-1 / P1-3 / P1-4 / P1-5): the insteadOf/pushInsteadOf
detection API now takes structured, name-only key lists (never a hand
split of `git config --list` line output).
"""

from __future__ import annotations

import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import pytest  # noqa: E402
import skill_runtime_command_policy as policy  # noqa: E402

from skill_runtime_command_policy import (  # noqa: E402
    GIT_GLOBAL_OPTION_TOKENS,
    GIT_SUBPROCESS_UNSET_ENV_KEYS,
    INSTEADOF_CONFIG_NAME_REGEXP,
    GitSubprocessConfigProbeFailed,
    GitSubprocessRewriteRejected,
    parse_config_get_regexp_name_only_nul,
    reject_git_global_options,
    reject_insteadof_rewrite,
    reject_option_like_positional,
)


def test_git_subprocess_unset_env_keys_matches_ac1_named_set():
    """GIVEN the Issue #2196 (post owner-review) AC1 contract list
    WHEN reading GIT_SUBPROCESS_UNSET_ENV_KEYS
    THEN it contains exactly the fourteen named GIT_* variables, no more,
    no fewer -- including GIT_CONFIG/GIT_CONFIG_PARAMETERS (P1-1) and
    GIT_EXEC_PATH/GIT_CEILING_DIRECTORIES (P1-2), added by the owner
    adversarial review fix delta."""
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
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_EXEC_PATH",
        "GIT_CEILING_DIRECTORIES",
        "GIT_SSL_NO_VERIFY",
    }
    assert GIT_SUBPROCESS_UNSET_ENV_KEYS == frozenset(expected)
    assert len(GIT_SUBPROCESS_UNSET_ENV_KEYS) == 14


# ---------------------------------------------------------------------------
# AC4 / AC6 / AC9: reject_insteadof_rewrite (structured key-name input)
# ---------------------------------------------------------------------------


def test_reject_insteadof_rewrite_raises_on_matched_keys():
    """GIVEN a non-empty list of matched config key names (as produced by a
    structured `--get-regexp --name-only` query)
    WHEN reject_insteadof_rewrite is called
    THEN GitSubprocessRewriteRejected is raised naming the first key."""
    with pytest.raises(GitSubprocessRewriteRejected) as excinfo:
        reject_insteadof_rewrite(["url.https://example.com/.insteadof"])
    assert "url.https://example.com/.insteadof" in str(excinfo.value)


def test_reject_insteadof_rewrite_no_raise_for_empty_match_list():
    """GIVEN an empty matched-key list
    WHEN reject_insteadof_rewrite is called
    THEN no exception is raised."""
    reject_insteadof_rewrite([])  # must not raise


def test_insteadof_config_name_regexp_matches_pushinsteadof_too():
    """GIVEN the INSTEADOF_CONFIG_NAME_REGEXP pattern
    WHEN matched against a name-only key list containing both `insteadof`
    and `pushinsteadof` variants
    THEN both match (the pre-owner-review code only checked `insteadOf`,
    never `pushInsteadOf`, despite its docstring implying both -- P1-4)."""
    import re

    pattern = re.compile(INSTEADOF_CONFIG_NAME_REGEXP)
    assert pattern.match("url.https://example.com/.insteadof")
    assert pattern.match("url.https://example.com/.pushinsteadof")
    assert not pattern.match("url.https://example.com/.insteadoflike")
    assert not pattern.match("urlinsteadof.foo")


def test_insteadof_config_name_regexp_matches_subsection_containing_equals():
    """GIVEN a config key name whose subsection legally contains '='
    (e.g. `url.https://evil.example/?x=y.insteadof`)
    WHEN matched against INSTEADOF_CONFIG_NAME_REGEXP
    THEN it still matches -- this is only possible because the detection
    path now operates on structured, name-only query output, never a
    hand split of `key=value` line text (P1-4: the pre-owner-review
    line-splitting parser would have misidentified this exact shape)."""
    import re

    pattern = re.compile(INSTEADOF_CONFIG_NAME_REGEXP)
    tricky_name = "url.https://evil.example/?x=y.insteadof"
    assert pattern.match(tricky_name)
    reject = []
    try:
        reject_insteadof_rewrite([tricky_name])
    except GitSubprocessRewriteRejected as exc:
        reject.append(str(exc))
    assert reject and tricky_name in reject[0]


# ---------------------------------------------------------------------------
# AC9 / P1-5: parse_config_get_regexp_name_only_nul + probe-failure disposition
# ---------------------------------------------------------------------------


def test_parse_config_get_regexp_name_only_nul_splits_on_nul():
    raw = b"url.a.insteadof\x00url.b.pushinsteadof\x00"
    assert parse_config_get_regexp_name_only_nul(raw) == [
        "url.a.insteadof",
        "url.b.pushinsteadof",
    ]


def test_parse_config_get_regexp_name_only_nul_empty_stdout_is_empty_list():
    assert parse_config_get_regexp_name_only_nul(b"") == []


def test_parse_config_get_regexp_name_only_nul_fails_closed_on_decode_error():
    """GIVEN stdout bytes that are not valid UTF-8
    WHEN parse_config_get_regexp_name_only_nul is called
    THEN GitSubprocessConfigProbeFailed is raised -- an undecodable probe
    result must never be silently treated as "no matches" (AC9)."""
    with pytest.raises(GitSubprocessConfigProbeFailed):
        parse_config_get_regexp_name_only_nul(b"\xff\xfe not valid utf-8")


# ---------------------------------------------------------------------------
# AC8 / P1-3: reject_git_global_options / reject_option_like_positional
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["-c", "core.hooksPath=/tmp/evil", "status"],
        ["status", "-c", "credential.helper=evil"],
        ["--config-env=core.hooksPath=EVIL", "status"],
        ["-C", "/etc", "status"],
        ["--git-dir=/etc/passwd", "status"],
        ["--work-tree=/", "status"],
        ["--exec-path=/tmp/evil-helpers", "status"],
        ["--namespace=evil", "status"],
    ],
)
def test_reject_git_global_options_rejects_every_named_global_option(argv):
    """GIVEN argv containing any Git global option named in the Issue
    #2196 contract (`-c` / `--config-env` / `-C` / `--git-dir` /
    `--work-tree` / `--exec-path` / `--namespace`), at any position
    WHEN reject_git_global_options is called
    THEN ValueError is raised (AC8)."""
    with pytest.raises(ValueError):
        reject_git_global_options(argv)


def test_reject_git_global_options_allows_ordinary_subcommand_argv():
    reject_git_global_options(["status", "--short"])  # must not raise
    reject_git_global_options(["ls-remote", "--", "https://example.com/repo.git"])


def test_git_global_option_tokens_matches_contract_set():
    assert GIT_GLOBAL_OPTION_TOKENS == frozenset(
        {
            "-c",
            "--config-env",
            "-C",
            "--git-dir",
            "--work-tree",
            "--exec-path",
            "--namespace",
        }
    )


def test_reject_option_like_positional_rejects_leading_dash():
    with pytest.raises(ValueError):
        reject_option_like_positional("-c")
    with pytest.raises(ValueError):
        reject_option_like_positional("--upload-pack=evil")


def test_reject_option_like_positional_allows_ordinary_value():
    reject_option_like_positional("https://example.com/repo.git")  # must not raise
    reject_option_like_positional("refs/heads/main")  # must not raise


# ---------------------------------------------------------------------------
# Issue #2378: remote protocol structural policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["-c", "origin", "git@host:repo", "https://host/repo?redirect=evil", "https://user@host/repo"])
def test_validate_literal_remote_url_rejects_nonliteral_or_indirect_values(value):
    with pytest.raises(ValueError):
        policy.validate_literal_remote_url(value)


def test_remote_ref_and_repository_object_id_are_closed_typed_values(tmp_path):
    assert policy.validate_allowed_remote_ref("refs/heads/main").value == "refs/heads/main"
    for bad_ref in ("HEAD", "refs/tags/v1", "refs/heads/../main", "-c"):
        with pytest.raises(ValueError):
            policy.validate_allowed_remote_ref(bad_ref)
    sha1 = policy.validate_repository_object_format("sha1")
    assert policy.validate_repository_object_id("a" * 40, sha1).value == "a" * 40
    for bad_oid in ("A" * 40, "a" * 39, "g" * 40):
        with pytest.raises(ValueError):
            policy.validate_repository_object_id(bad_oid, sha1)
    with pytest.raises(ValueError):
        policy.validate_repository_object_format("sha512")


def test_detached_worktree_path_is_fresh_and_project_confined(tmp_path):
    project = tmp_path / "project"
    target = project / ".claude" / "worktrees" / "dedicated"
    assert policy.validate_detached_worktree_path(str(target), str(project)).value == str(target)
    with pytest.raises(ValueError):
        policy.validate_detached_worktree_path(str(tmp_path / "outside"), str(project))
    target.parent.mkdir(parents=True)
    target.mkdir()
    with pytest.raises(ValueError):
        policy.validate_detached_worktree_path(str(target), str(project))
