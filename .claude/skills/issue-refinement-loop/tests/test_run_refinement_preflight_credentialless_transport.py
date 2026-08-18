"""Issue #2241 AC8 / PR #2247 review P1-1 regression coverage.

`run_refinement_preflight.py`'s `_fetch_issue()` / `_fetch_issue_comments()`
must route to the credentialless REST transport
(`scripts/agent-guards/github_credentialless_read.py`) under an isolated
Claude-GPT session profile, and must never fall back to the `gh` CLI in
that profile -- the whole point of Issue #2241 P1-1 is that an isolated
session's `gh` cannot authenticate (Issue #2232 comment 5316900237 root
cause), so any fallback to it would silently reintroduce the incident.

This file never re-implements the transport or the profile-detection logic
under test -- it only exercises the production
`run_refinement_preflight._is_isolated_claude_gpt_profile` /
`run_refinement_preflight._fetch_issue` /
`run_refinement_preflight._fetch_issue_comments` functions, following the
existing `test_operator_selected_scope_reframe.py` module-loading
convention (`importlib.util.spec_from_file_location` with a unique module
name, so this test's import of `run_refinement_preflight.py` never
collides with another test file's own load of the same source file in the
same pytest session).
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[4]

_AGENT_GUARDS_DIR = REPO_ROOT / "scripts" / "agent-guards"
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))


def _load_preflight_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / "run_refinement_preflight.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = _load_preflight_module("run_refinement_preflight_2241_ac8_credentialless_transport")
gcr = preflight._credentialless_read

REPO = "squne121/loop-protocol"
ISSUE_NUMBER = 2241

_GH_MARKER_SCRIPT = """#!/bin/sh
# Test-only marker executable (Issue #2241 AC8): if this is ever invoked,
# it proves the isolated-profile fetch path fell back to the `gh` CLI,
# which the AC8 fix must never do. It always exits non-zero so a caller
# that DID invoke it (a regression) sees a hard failure, not a silently
# swallowed one.
echo -n "gh_marker_invoked" >> "$GH_MARKER_INVOCATION_FILE"
exit 7
"""


class _FakeCredentiallessResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_gh_marker_executable(tmp_path: Path, monkeypatch) -> Path:
    """Puts a marker `gh` executable at the front of PATH and returns the
    path to the invocation marker file (absent unless `gh` actually runs)."""
    marker_bin_dir = tmp_path / "marker-bin"
    marker_bin_dir.mkdir()
    gh_marker_path = marker_bin_dir / "gh"
    gh_marker_path.write_text(_GH_MARKER_SCRIPT, encoding="utf-8")
    gh_marker_path.chmod(gh_marker_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    invocation_marker_file = tmp_path / "gh_marker_invoked.marker"
    monkeypatch.setenv("GH_MARKER_INVOCATION_FILE", str(invocation_marker_file))
    monkeypatch.setenv("PATH", f"{marker_bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return invocation_marker_file


def _force_isolated_profile(tmp_path: Path, monkeypatch) -> None:
    """Forces `_is_isolated_claude_gpt_profile()` to True the same way
    `scripts/claude-gpt/launch.sh` does in production: point `HOME` at a
    fresh sandbox directory distinct from the real OS account home
    (`pwd.getpwuid(os.getuid()).pw_dir`), never at the real OS account home
    itself."""
    real_home = preflight.pwd.getpwuid(os.getuid()).pw_dir
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    assert str(isolated_home) != real_home
    monkeypatch.setenv("HOME", str(isolated_home))
    assert preflight._is_isolated_claude_gpt_profile() is True


def _patch_credentialless_opener(monkeypatch, responses_by_url: dict[str, tuple[bytes, dict[str, str]]]):
    def _fake_open(request, timeout=None):
        url = request.full_url
        assert url in responses_by_url, f"unexpected credentialless GET: {url!r}"
        body, headers = responses_by_url[url]
        return _FakeCredentiallessResponse(body, headers)

    monkeypatch.setattr(gcr._opener, "open", _fake_open)


def test_isolated_profile_never_invokes_gh_marker_executable(tmp_path, monkeypatch):
    """GIVEN an isolated Claude-GPT session profile (HOME diverges from the
    real OS account home, exactly as `scripts/claude-gpt/launch.sh` sets it)
    and a marker `gh` executable installed at the front of PATH
    WHEN `_fetch_issue()` and `_fetch_issue_comments()` are called
    THEN both succeed via the credentialless transport and the marker `gh`
    executable is never invoked (Issue #2241 AC8 / PR #2247 review P1-1)."""
    invocation_marker_file = _install_gh_marker_executable(tmp_path, monkeypatch)
    _force_isolated_profile(tmp_path, monkeypatch)

    issue_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}"
    comments_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments?per_page=100"
    issue_body = (
        b'{"number": 2241, "title": "credentialless transport wiring", '
        b'"body": "issue body text", "labels": [{"name": "bug"}], '
        b'"html_url": "https://github.com/squne121/loop-protocol/issues/2241", '
        b'"updated_at": "2026-08-17T00:00:00Z"}'
    )
    comments_body = b'[{"id": 1, "body": "comment one"}]'
    _patch_credentialless_opener(
        monkeypatch,
        {
            issue_url: (issue_body, {}),
            comments_url: (comments_body, {}),
        },
    )

    issue, issue_err = preflight._fetch_issue(REPO, ISSUE_NUMBER)
    comments, comments_err = preflight._fetch_issue_comments(REPO, ISSUE_NUMBER)

    assert issue_err == ""
    assert comments_err == ""
    assert issue is not None
    assert comments is not None
    assert not invocation_marker_file.exists(), (
        "gh marker executable was invoked -- isolated profile fell back to the gh CLI"
    )


def test_isolated_profile_fetch_issue_converts_credentialless_data_to_gh_cli_shape(tmp_path, monkeypatch):
    """GIVEN the isolated profile and a raw GitHub REST issue body
    WHEN `_fetch_issue()` routes through the credentialless transport
    THEN the returned dict matches the `gh issue view --json
    number,title,body,labels,url,updatedAt` field-name shape existing
    consumers of `_fetch_issue()` already depend on (Issue #2241 AC8)."""
    _install_gh_marker_executable(tmp_path, monkeypatch)
    _force_isolated_profile(tmp_path, monkeypatch)

    issue_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}"
    issue_body = (
        b'{"number": 2241, "title": "credentialless transport wiring", '
        b'"body": "issue body text", "labels": [{"name": "bug"}, {"name": "P1"}], '
        b'"html_url": "https://github.com/squne121/loop-protocol/issues/2241", '
        b'"updated_at": "2026-08-17T00:00:00Z"}'
    )
    _patch_credentialless_opener(monkeypatch, {issue_url: (issue_body, {})})

    issue, err = preflight._fetch_issue(REPO, ISSUE_NUMBER)

    assert err == ""
    assert issue == {
        "number": 2241,
        "title": "credentialless transport wiring",
        "body": "issue body text",
        "labels": [{"name": "bug"}, {"name": "P1"}],
        "url": "https://github.com/squne121/loop-protocol/issues/2241",
        "updatedAt": "2026-08-17T00:00:00Z",
    }


def test_isolated_profile_fetch_issue_comments_follows_pagination_and_returns_flat_list(tmp_path, monkeypatch):
    """GIVEN the isolated profile and a two-page paginated comments response
    WHEN `_fetch_issue_comments()` routes through the credentialless
    transport
    THEN pagination is followed to exhaustion and the result is a single
    flat list -- the same shape `_fetch_issue_comments()` has always
    returned to its callers (Issue #2241 AC8)."""
    _install_gh_marker_executable(tmp_path, monkeypatch)
    _force_isolated_profile(tmp_path, monkeypatch)

    page_1_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments?per_page=100"
    page_2_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments?per_page=100&page=2"
    _patch_credentialless_opener(
        monkeypatch,
        {
            page_1_url: (b'[{"id": 1}]', {"Link": f'<{page_2_url}>; rel="next"'}),
            page_2_url: (b'[{"id": 2}]', {}),
        },
    )

    comments, err = preflight._fetch_issue_comments(REPO, ISSUE_NUMBER)

    assert err == ""
    assert comments == [{"id": 1}, {"id": 2}]


def test_non_isolated_profile_is_not_detected_as_isolated(monkeypatch):
    """GIVEN a normal human/dev/CI shell (HOME == the real OS account home)
    WHEN `_is_isolated_claude_gpt_profile()` is evaluated
    THEN it returns False, so this fix cannot regress the existing `gh` CLI
    path for every non-isolated caller (Issue #2241 AC8, `run_refinement_
    preflight.py`'s other consumers/other command_ids must be unaffected)."""
    real_home = preflight.pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("HOME", real_home)

    assert preflight._is_isolated_claude_gpt_profile() is False
