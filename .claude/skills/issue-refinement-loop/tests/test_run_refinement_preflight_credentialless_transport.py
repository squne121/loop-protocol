"""Read-transport selection regression coverage for
`run_refinement_preflight.py` (Issue #2241 AC8 / #2257, updated by #2872).

History: Issue #2241 routed an isolated Claude-GPT session (HOME != the OS
account home) to the credentialless REST transport
(`scripts/agent-guards/github_credentialless_read.py`) because the launcher
of that era never forwarded GitHub auth. Issue #2299 changed the launcher
contract: GitHub auth (an ambient `GH_CONFIG_DIR`, `GH_TOKEN`/`GITHUB_TOKEN`/
`GH_HOST`/`GH_REPO`) is shared natively, so an isolated HOME says nothing
about GitHub auth. Issue #2872 therefore makes the production selector a
constant: native `gh`, always, for every read of one invocation.

This file keeps HOME isolation, GitHub auth availability and transport
selection as INDEPENDENT axes:

* the selector tests (isolated + token env / isolated + stored
  `GH_CONFIG_DIR` / isolated + authless / normal profile) pin that the
  transport never depends on either axis;
* the call-path tests (single transport threaded through every read,
  mid-read failure, multi-page comments + anchor) drive the real
  `run_preflight()` / `_fetch_*` functions with a recording transport;
* the remaining credentialless tests exercise the GET-only transport
  adapter through an EXPLICIT `transport=` argument -- it is no longer
  selected implicitly by any profile.

No test here reads, copies or prints a credential: token values are
sentinels and are asserted absent from every result.

Module loading follows the `test_operator_selected_scope_reframe.py`
convention (`importlib.util.spec_from_file_location` with a unique module
name, so this test never collides with another test file's own load of the
same source file in the same pytest session).
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

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


def test_explicit_credentialless_transport_never_invokes_gh_marker_executable(tmp_path, monkeypatch):
    """GIVEN the credentialless GET-only transport passed EXPLICITLY (it is no
    longer selected implicitly by any profile since Issue #2872) and a marker
    `gh` executable installed at the front of PATH
    WHEN `_fetch_issue()` and `_fetch_issue_comments()` are called
    THEN both succeed via that transport and the marker `gh` executable is
    never invoked (Issue #2241 AC8 contract surface, retained)."""
    invocation_marker_file = _install_gh_marker_executable(tmp_path, monkeypatch)
    transport = preflight._CredentiallessPreflightTransport()

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

    issue, issue_err = preflight._fetch_issue(REPO, ISSUE_NUMBER, transport=transport)
    comments, comments_err = preflight._fetch_issue_comments(REPO, ISSUE_NUMBER, transport=transport)

    assert issue_err == ""
    assert comments_err == ""
    assert issue is not None
    assert comments is not None
    assert not invocation_marker_file.exists(), (
        "gh marker executable was invoked -- the explicit credentialless transport fell back to the gh CLI"
    )


def test_credentialless_transport_fetch_issue_converts_credentialless_data_to_gh_cli_shape(tmp_path, monkeypatch):
    """GIVEN an explicit credentialless transport and a raw GitHub REST issue body
    WHEN `_fetch_issue()` routes through the credentialless transport
    THEN the returned dict matches the `gh issue view --json
    number,title,body,labels,url,updatedAt` field-name shape existing
    consumers of `_fetch_issue()` already depend on (Issue #2241 AC8)."""
    _install_gh_marker_executable(tmp_path, monkeypatch)
    transport = preflight._CredentiallessPreflightTransport()

    issue_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}"
    issue_body = (
        b'{"number": 2241, "title": "credentialless transport wiring", '
        b'"body": "issue body text", "labels": [{"name": "bug"}, {"name": "P1"}], '
        b'"html_url": "https://github.com/squne121/loop-protocol/issues/2241", '
        b'"updated_at": "2026-08-17T00:00:00Z"}'
    )
    _patch_credentialless_opener(monkeypatch, {issue_url: (issue_body, {})})

    issue, err = preflight._fetch_issue(REPO, ISSUE_NUMBER, transport=transport)

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
    transport = preflight._CredentiallessPreflightTransport()

    page_1_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments?per_page=100"
    page_2_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments?per_page=100&page=2"
    _patch_credentialless_opener(
        monkeypatch,
        {
            page_1_url: (b'[{"id": 1}]', {"Link": f'<{page_2_url}>; rel="next"'}),
            page_2_url: (b'[{"id": 2}]', {}),
        },
    )

    comments, err = preflight._fetch_issue_comments(REPO, ISSUE_NUMBER, transport=transport)

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


# ---------------------------------------------------------------------------
# Issue #2257 AC1/AC2/AC3/AC6: `_fetch_single_comment` isolated-profile
# regression coverage. Issue #2197's anchor comment 5315264311 was
# misclassified as missing because `_fetch_single_comment` (unlike
# `_fetch_issue`/`_fetch_issue_comments` above) had no isolated-profile
# branch and always hit an unauthenticated `gh api` call.
# ---------------------------------------------------------------------------


def test_fetch_single_comment_credentialless_transport_never_invokes_gh_marker_executable(tmp_path, monkeypatch):
    """GIVEN an isolated Claude-GPT session profile and a marker `gh`
    executable installed at the front of PATH
    WHEN `_fetch_single_comment()` is called
    THEN it succeeds via the credentialless transport and the marker `gh`
    executable is never invoked (Issue #2257 AC1/AC6 -- this is the exact
    function Issue #2197's incident traced to)."""
    invocation_marker_file = _install_gh_marker_executable(tmp_path, monkeypatch)
    transport = preflight._CredentiallessPreflightTransport()

    comment_id = 5315264311
    comment_url = f"https://api.github.com/repos/{REPO}/issues/comments/{comment_id}"
    comment_body = (
        b'{"id": 5315264311, "body": "anchor comment body", '
        b'"issue_url": "https://api.github.com/repos/squne121/loop-protocol/issues/2197"}'
    )
    _patch_credentialless_opener(monkeypatch, {comment_url: (comment_body, {})})

    data, err = preflight._fetch_single_comment(REPO, comment_id, transport=transport)

    assert err == ""
    assert data is not None
    assert data["id"] == 5315264311
    assert not invocation_marker_file.exists(), (
        "gh marker executable was invoked -- isolated profile fell back to the gh CLI "
        "(this is exactly the Issue #2197 regression: a genuinely-existing anchor "
        "comment must never be resolved through an unauthenticated gh api call)"
    )


def test_fetch_single_comment_credentialless_transport_true_404_is_semantic_missing(tmp_path, monkeypatch):
    """GIVEN an isolated Claude-GPT session profile and a genuinely
    nonexistent comment id (a true HTTP 404)
    WHEN `_fetch_single_comment()` is called
    THEN the error is classified as `semantic_missing`, not
    `transport_failure` (Issue #2257 AC2)."""
    _install_gh_marker_executable(tmp_path, monkeypatch)
    transport = preflight._CredentiallessPreflightTransport()

    comment_id = 99999999
    comment_url = f"https://api.github.com/repos/{REPO}/issues/comments/{comment_id}"

    def _fake_open(request, timeout=None):
        raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            comment_url, 404, "Not Found", {}, __import__("io").BytesIO(b"")
        )

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    data, err = preflight._fetch_single_comment(REPO, comment_id, transport=transport)

    assert data is None
    assert err.startswith("semantic_missing:")
    assert not preflight._is_transport_failure(err)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_fetch_single_comment_credentialless_transport_transport_failures_are_never_semantic_missing(
    tmp_path, monkeypatch, status
):
    """GIVEN an isolated Claude-GPT session profile and any non-404 HTTP
    failure resolving the anchor comment (401/403/429/5xx)
    WHEN `_fetch_single_comment()` is called
    THEN the error is classified as `transport_failure`, never
    `semantic_missing` -- a genuinely-existing anchor comment must never
    be reported as not found because of an auth/rate-limit/upstream
    failure (Issue #2257 AC3, exact regression class of the #2197
    incident: gh exit 4 in the isolated profile was being conflated with
    ANCHOR_COMMENT_NOT_FOUND)."""
    _install_gh_marker_executable(tmp_path, monkeypatch)
    transport = preflight._CredentiallessPreflightTransport()

    comment_id = 5315264311
    comment_url = f"https://api.github.com/repos/{REPO}/issues/comments/{comment_id}"

    def _fake_open(request, timeout=None):
        raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            comment_url, status, "err", {}, __import__("io").BytesIO(b"")
        )

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    data, err = preflight._fetch_single_comment(REPO, comment_id, transport=transport)

    assert data is None
    assert err.startswith("transport_failure:")
    assert preflight._is_transport_failure(err)
    assert not err.startswith("semantic_missing:")


def test_fetch_single_comment_credentialless_transport_dns_failure_is_transport_failure(tmp_path, monkeypatch):
    """GIVEN an isolated Claude-GPT session profile and a DNS resolution
    failure resolving the anchor comment
    WHEN `_fetch_single_comment()` is called
    THEN the error is classified as `transport_failure` (Issue #2257 AC3/
    AC7: DNS fault-injection case)."""
    import socket
    import urllib.error as _urllib_error

    _install_gh_marker_executable(tmp_path, monkeypatch)
    transport = preflight._CredentiallessPreflightTransport()

    def _fake_open(request, timeout=None):
        raise _urllib_error.URLError(socket.gaierror("Name or service not known"))

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    data, err = preflight._fetch_single_comment(REPO, 5315264311, transport=transport)

    assert data is None
    assert err.startswith("transport_failure:")


def test_fetch_single_comment_non_isolated_profile_uses_gh_cli(monkeypatch):
    """GIVEN a normal (non-isolated) profile
    WHEN `_fetch_single_comment()` is called
    THEN it still routes through `_run_gh` (unchanged `gh api` behavior --
    Issue #2257 AC8: the normal-profile gh path must not regress)."""
    real_home = preflight.pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("HOME", real_home)
    assert preflight._is_isolated_claude_gpt_profile() is False

    calls: list[list[str]] = []

    def _fake_run_gh(argv, timeout=preflight.GH_API_TIMEOUT):
        calls.append(argv)
        return {"id": 1, "issue_url": "x"}, ""

    monkeypatch.setattr(preflight, "_run_gh", _fake_run_gh)

    data, err = preflight._fetch_single_comment(REPO, 1)

    assert err == ""
    assert data == {"id": 1, "issue_url": "x"}
    assert len(calls) == 1
    assert calls[0] == ["gh", "api", f"repos/{REPO}/issues/comments/1"]


def test_fetch_single_comment_non_isolated_profile_gh_exit_4_is_transport_failure(monkeypatch):
    """GIVEN a normal (non-isolated) profile where `gh api` fails with
    exit code 4 (gh CLI's documented "authentication required" exit code)
    WHEN `_fetch_single_comment()` is called
    THEN the error is classified as `transport_failure`, not
    `semantic_missing` (Issue #2257 AC3: the gh-CLI-path mirror of the
    isolated-profile 401 case above)."""
    real_home = preflight.pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("HOME", real_home)

    def _fake_run_gh(argv, timeout=preflight.GH_API_TIMEOUT):
        return None, "gh_exit_4: authentication required"

    monkeypatch.setattr(preflight, "_run_gh", _fake_run_gh)

    data, err = preflight._fetch_single_comment(REPO, 1)

    assert data is None
    assert err.startswith("transport_failure:")


def test_fetch_single_comment_non_isolated_profile_gh_404_is_semantic_missing(monkeypatch):
    """GIVEN a normal (non-isolated) profile where `gh api` fails with a
    genuine HTTP 404
    WHEN `_fetch_single_comment()` is called
    THEN the error is classified as `semantic_missing` (Issue #2257 AC2)."""
    real_home = preflight.pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("HOME", real_home)

    monkeypatch.setattr(
        preflight,
        "_run_gh",
        lambda argv, timeout=preflight.GH_API_TIMEOUT: (None, "gh_exit_1: HTTP 404: Not Found (https://api.github.com/...)"),
    )

    data, err = preflight._fetch_single_comment(REPO, 1)

    assert data is None
    assert err.startswith("semantic_missing:")


# ---------------------------------------------------------------------------
# Issue #2257 AC5/AC6 (retained as an explicit-transport, opt-in live test by
# Issue #2872): exact incident replay against the real GitHub REST API (no
# opener mocking) -- Issue #2197 anchor comment 5315264311, under a fresh
# isolated HOME, empty GH_CONFIG_DIR, token unset. Only DNS/egress-
# unavailable/upstream-5xx/rate-limit failures are an environment SKIP
# (never silently upgraded to PASS); any auth-dependency result is a hard
# `pytest.fail` -- that IS the #2197 regression reproducing.
# ---------------------------------------------------------------------------


@pytest.mark.github_live
def test_ac5_exact_incident_replay_anchor_comment_5315264311_resolves_live(tmp_path, monkeypatch):
    """Pre-#2299 contract replay of the GET-only transport (Issue #2872:
    explicit transport, `github_live` opt-in -- an anonymous call must not be
    a default/required gate, #2361).

    GIVEN a fresh isolated HOME, empty GH_CONFIG_DIR, GH_TOKEN/GITHUB_TOKEN
    unset, and a fail-on-use `gh` marker executable at the front of PATH
    WHEN `_fetch_single_comment()` resolves Issue #2197's real anchor
    comment 5315264311 with NO transport mocking (a real, unauthenticated
    GitHub REST call)
    THEN the comment resolves successfully via the credentialless
    transport, the `gh` marker executable is never invoked, and its
    `issue_url` points at Issue #2197 -- reproducing the exact incident
    input and proving it no longer misclassifies as missing (Issue #2257
    AC5/AC6)."""
    invocation_marker_file = _install_gh_marker_executable(tmp_path, monkeypatch)
    transport = preflight._CredentiallessPreflightTransport()
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    empty_gh_config_dir = tmp_path / "empty-gh-config-dir"
    empty_gh_config_dir.mkdir()
    monkeypatch.setenv("GH_CONFIG_DIR", str(empty_gh_config_dir))

    try:
        data, err = preflight._fetch_single_comment("squne121/loop-protocol", 5315264311, transport=transport)
    finally:
        assert not invocation_marker_file.exists(), (
            "gh marker executable was invoked during the AC5 exact incident "
            "replay -- the isolated profile fell back to the gh CLI, "
            "reproducing the Issue #2197 split-brain transport regression"
        )

    if err.startswith("transport_failure:") and (
        "rate_limited" in err or "upstream_environment_failure" in err or "transport_connectivity_failure" in err
    ):
        pytest.skip(f"network/rate-limit/upstream unavailable in this environment: {err}")
    if err.startswith("transport_failure:") and "authentication" in err:
        pytest.fail(
            f"AC5 unmet: exact incident replay reproduced the Issue #2197 auth-dependency "
            f"misclassification for a genuinely-existing anchor comment: {err}"
        )

    assert err == "", f"AC5 unmet: anchor comment 5315264311 did not resolve: {err}"
    assert data is not None
    assert str(data.get("id")) == "5315264311"
    assert data.get("issue_url") == "https://api.github.com/repos/squne121/loop-protocol/issues/2197"



# ---------------------------------------------------------------------------
# Issue #2872: transport selection is independent of HOME isolation and of
# GitHub auth availability. Native `gh` is selected for every read of one
# production invocation; no auth-state probing, no mid-read authority split.
# ---------------------------------------------------------------------------

TOKEN_SENTINEL = "SENTINEL_2872_token_value_must_never_appear_anywhere"
HOSTS_YML_SENTINEL = "SENTINEL_2872_stored_config_content_must_never_be_read"
ANCHOR_COMMENT_ID = 5942301496
ANCHOR_URL = f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#issuecomment-{ANCHOR_COMMENT_ID}"
_ISSUE_PAYLOAD = {
    "number": ISSUE_NUMBER,
    "title": "t",
    "body": "## Outcome\n\nx\n",
    "labels": [],
    "url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}",
    "updatedAt": "2026-10-02T00:00:00Z",
}
_ANCHOR_PAYLOAD = {
    "id": ANCHOR_COMMENT_ID,
    "body": "anchor body",
    "html_url": ANCHOR_URL,
    "issue_url": f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}",
    "author_association": "OWNER",
    "updated_at": "2026-10-02T00:00:00Z",
}


def _forbid_credentialless_authority(monkeypatch) -> list[str]:
    """Fails the test if anything routes to the anonymous authority: neither
    the adapter class is constructed nor the raw opener is invoked. Returns a
    list that records any (unexpected) use."""
    used: list[str] = []

    class _Forbidden:
        def __init__(self, *args, **kwargs):
            used.append("_CredentiallessPreflightTransport constructed")
            raise AssertionError("credentialless transport must not be selected")

    def _forbidden_open(request, timeout=None):
        used.append(f"credentialless GET {getattr(request, 'full_url', request)!r}")
        raise AssertionError("anonymous credentialless GET must not be issued")

    monkeypatch.setattr(preflight, "_CredentiallessPreflightTransport", _Forbidden)
    if gcr is not None:
        monkeypatch.setattr(gcr._opener, "open", _forbidden_open)
    return used


class _RecordingTransport:
    """`GitHubReadTransport`-shaped recorder standing in for the single read
    authority. Each method records its call so a test can prove every read of
    one invocation used THIS instance."""

    SOURCE_LABEL = "recording_native_gh"

    def __init__(self, *, comments=None, comments_error="", anchor=None, anchor_error="", issue_error=""):
        self.calls: list[tuple[str, tuple]] = []
        self._comments = [] if comments is None else comments
        self._comments_error = comments_error
        self._anchor = anchor
        self._anchor_error = anchor_error
        self._issue_error = issue_error

    def read_issue(self, repo, issue_number):
        self.calls.append(("read_issue", (repo, issue_number)))
        if self._issue_error:
            return None, self._issue_error
        return dict(_ISSUE_PAYLOAD), ""

    def list_issue_comments(self, repo, issue_number):
        self.calls.append(("list_issue_comments", (repo, issue_number)))
        if self._comments_error:
            return None, self._comments_error
        return list(self._comments), ""

    def read_issue_comment(self, repo, comment_id):
        self.calls.append(("read_issue_comment", (repo, comment_id)))
        if self._anchor_error:
            return None, self._anchor_error
        return (dict(self._anchor) if self._anchor is not None else None), ""


def _run_live_preflight(monkeypatch, tmp_path, transport, anchor_urls=None):
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(preflight, "_select_read_transport", lambda: transport)
    return preflight.run_preflight(
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        anchor_comment_urls=list(anchor_urls or []),
        fixture_path=None,
    )


def _assert_no_secret_in(*values) -> None:
    rendered = "".join(repr(v) for v in values)
    assert TOKEN_SENTINEL not in rendered
    assert HOSTS_YML_SENTINEL not in rendered


def test_isolated_with_token_env_selects_native_gh_transport(tmp_path, monkeypatch):
    """GIVEN an isolated HOME (launcher-equivalent) and GitHub auth shared via
    token env (`GH_TOKEN`/`GITHUB_TOKEN`)
    WHEN the production read transport is selected and an issue is read
    THEN it is the native `gh` transport (HOME difference alone never selects
    the anonymous credentialless transport), the read is a `gh issue view`,
    no anonymous GET is issued and the token value is never surfaced (Issue
    #2872 AC1/AC5(a)/AC6)."""
    _force_isolated_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("GH_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN_SENTINEL)
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    used = _forbid_credentialless_authority(monkeypatch)
    argvs: list[list[str]] = []

    def _fake_run_gh(argv, timeout=preflight.GH_API_TIMEOUT):
        argvs.append(list(argv))
        return dict(_ISSUE_PAYLOAD), ""

    monkeypatch.setattr(preflight, "_run_gh", _fake_run_gh)

    transport = preflight._select_read_transport()
    issue, err = preflight._fetch_issue(REPO, ISSUE_NUMBER)

    assert preflight._is_isolated_claude_gpt_profile() is True
    assert isinstance(transport, preflight._GhCliPreflightTransport)
    assert transport.SOURCE_LABEL == "gh_cli"
    assert err == ""
    assert issue == _ISSUE_PAYLOAD
    assert argvs == [
        ["gh", "issue", "view", str(ISSUE_NUMBER), "--repo", REPO, "--json", "number,title,body,labels,url,updatedAt"]
    ]
    assert used == []
    _assert_no_secret_in(issue, err, argvs)


def test_isolated_with_stored_gh_config_dir_selects_native_gh_transport(tmp_path, monkeypatch):
    """GIVEN an isolated HOME and a stored `gh auth login` configuration
    reached only via `GH_CONFIG_DIR` (NO token env)
    WHEN the production read transport is selected and an issue is read
    THEN it is the native `gh` transport -- a separate positive case from the
    token-env one -- and the selector/transport never open or read anything
    inside that config directory (auth resolution is left to `gh`; Issue #2872
    AC1/AC5(b)/AC8)."""
    _force_isolated_profile(tmp_path, monkeypatch)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config_dir = tmp_path / "stored-gh-config"
    config_dir.mkdir()
    (config_dir / "hosts.yml").write_text(f"github.com:\n  {HOSTS_YML_SENTINEL}\n", encoding="utf-8")
    monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))
    used = _forbid_credentialless_authority(monkeypatch)
    opened: list[str] = []
    real_open = open

    def _spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    argvs: list[list[str]] = []

    def _fake_run_gh(argv, timeout=preflight.GH_API_TIMEOUT):
        argvs.append(list(argv))
        return dict(_ISSUE_PAYLOAD), ""

    monkeypatch.setattr(preflight, "_run_gh", _fake_run_gh)
    monkeypatch.setattr("builtins.open", _spy_open)
    try:
        transport = preflight._select_read_transport()
        issue, err = preflight._fetch_issue(REPO, ISSUE_NUMBER)
    finally:
        monkeypatch.setattr("builtins.open", real_open)

    assert isinstance(transport, preflight._GhCliPreflightTransport)
    assert err == ""
    assert issue == _ISSUE_PAYLOAD
    assert [a[:3] for a in argvs] == [["gh", "issue", "view"]]
    assert used == []
    assert not any(str(config_dir) in path for path in opened), opened
    _assert_no_secret_in(issue, err, argvs)


def test_normal_profile_selects_native_gh_transport(monkeypatch):
    """GIVEN a normal profile (HOME == the real OS account home)
    WHEN the production read transport is selected
    THEN it is the same native `gh` transport used for isolated profiles --
    the selection is one constant, not a per-profile decision (Issue #2872
    AC5(d))."""
    real_home = preflight.pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("HOME", real_home)
    assert preflight._is_isolated_claude_gpt_profile() is False

    normal = preflight._select_read_transport()
    monkeypatch.setenv("HOME", "/nonexistent-isolated-home-2872")
    assert preflight._is_isolated_claude_gpt_profile() is True
    isolated = preflight._select_read_transport()

    assert isinstance(normal, preflight._GhCliPreflightTransport)
    assert normal.SOURCE_LABEL == "gh_cli"
    assert normal is isolated


def test_single_transport_threaded_through_all_read_call_sites(tmp_path, monkeypatch):
    """GIVEN one production `run_preflight()` invocation whose selector hands
    out ONE recording transport
    WHEN Issue and comments (anchor present) are read through the real
    `_fetch_*` call paths and -- in a second scenario -- the anchor
    single-comment fallback is read through the real anchor validation
    THEN every read lands on that very instance (call recording) and nothing
    is routed to the anonymous credentialless transport (Issue #2872 AC3,
    #2257 split-brain non-regression)."""
    used = _forbid_credentialless_authority(monkeypatch)

    # Scenario 1: the anchor is part of the complete comments traversal.
    transport_1 = _RecordingTransport(comments=[dict(_ANCHOR_PAYLOAD)])
    _run_live_preflight(monkeypatch, tmp_path, transport_1, anchor_urls=[ANCHOR_URL])
    methods_1 = [name for name, _ in transport_1.calls]
    assert methods_1[:2] == ["read_issue", "list_issue_comments"], methods_1
    assert "read_issue_comment" not in methods_1, methods_1

    # Scenario 2: the single-comment read (fresh-readback fallback of the
    # anchor validation, reached when no pre-fetched traversal is supplied)
    # must use the SAME explicitly-passed instance, never re-select.
    transport_2 = _RecordingTransport(anchor=dict(_ANCHOR_PAYLOAD))
    monkeypatch.setattr(preflight, "_select_read_transport", lambda: pytest.fail("re-selected"))
    valid, blockers = preflight._validate_anchor_comment_url(
        ANCHOR_URL, REPO, ISSUE_NUMBER, fixture_comments=None, transport=transport_2
    )
    assert (valid, blockers) == (True, [])
    assert [name for name, _ in transport_2.calls] == ["read_issue_comment"]
    monkeypatch.undo()

    # Scenario 3: NO selector patching, isolated HOME -- the production
    # selector itself must route every read of the invocation (Issue,
    # comments, anchor) through native `gh`, never the anonymous transport.
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: tmp_path)
    _force_isolated_profile(tmp_path, monkeypatch)
    used = _forbid_credentialless_authority(monkeypatch)
    argvs: list[list[str]] = []

    def _fake_run_gh(argv, timeout=preflight.GH_API_TIMEOUT):
        argvs.append(list(argv))
        if argv[:3] == ["gh", "issue", "view"]:
            return dict(_ISSUE_PAYLOAD), ""
        return [[dict(_ANCHOR_PAYLOAD)]], ""

    monkeypatch.setattr(preflight, "_run_gh", _fake_run_gh)
    preflight.run_preflight(issue_number=ISSUE_NUMBER, repo=REPO, anchor_comment_urls=[ANCHOR_URL], fixture_path=None)
    assert [a[:2] for a in argvs] == [["gh", "issue"], ["gh", "api"]], argvs
    assert used == []


def test_isolated_authless_keeps_contract_without_secret_probing(tmp_path, monkeypatch):
    """GIVEN a genuinely authless isolated environment (isolated HOME, no
    token env, no `GH_CONFIG_DIR`) and a decoy stored `hosts.yml` under the
    isolated HOME, with a marker `gh` that answers exit 4 ("authentication
    required") and records every invocation
    WHEN `run_preflight()` reads the Issue
    THEN the result is the structured `environment_failure` /
    `gh_auth_required` (current contract), exactly ONE `gh` read was made, no
    `gh auth ...` probe ran, `hosts.yml` was never opened, and no anonymous
    GET was issued (Issue #2872 AC4/AC8)."""
    _force_isolated_profile(tmp_path, monkeypatch)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GH_ENTERPRISE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    decoy_dir = Path(os.environ["HOME"]) / ".config" / "gh"
    decoy_dir.mkdir(parents=True)
    (decoy_dir / "hosts.yml").write_text(f"github.com:\n  {HOSTS_YML_SENTINEL}\n", encoding="utf-8")

    bin_dir = tmp_path / "authless-bin"
    bin_dir.mkdir()
    log_file = tmp_path / "gh-argv.log"
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/bin/sh\necho "$@" >> "$GH_ARGV_LOG"\n'
        'echo "To get started with GitHub CLI, please run:  gh auth login" >&2\nexit 4\n',
        encoding="utf-8",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("GH_ARGV_LOG", str(log_file))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    used = _forbid_credentialless_authority(monkeypatch)
    opened: list[str] = []
    real_open = open

    def _spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(preflight, "_find_repo_root", lambda: tmp_path)
    monkeypatch.setattr("builtins.open", _spy_open)
    try:
        result, exit_code = preflight.run_preflight(
            issue_number=ISSUE_NUMBER, repo=REPO, anchor_comment_urls=[ANCHOR_URL], fixture_path=None
        )
    finally:
        monkeypatch.setattr("builtins.open", real_open)

    assert exit_code == preflight.EXIT_ENVIRONMENT_FAILURE
    assert result["status"] == "environment_failure"
    assert result["reason_code"] == "gh_auth_required"
    assert result["source"] == "gh_cli"
    assert result["operation"] == "read_issue"
    gh_calls = log_file.read_text(encoding="utf-8").splitlines()
    assert len(gh_calls) == 1 and gh_calls[0].startswith("issue view"), gh_calls
    assert not any(call.split()[:1] == ["auth"] for call in gh_calls)
    assert not any("hosts.yml" in path for path in opened), opened
    assert used == []
    _assert_no_secret_in(result)


def test_mid_read_failure_does_not_switch_transport(tmp_path, monkeypatch):
    """GIVEN a read transport whose comments listing fails mid-invocation with
    an auth/rate-limit style error (after the Issue read succeeded)
    WHEN `run_preflight()` runs
    THEN the invocation reports a structured `environment_failure` sourced
    from THAT transport, performs no retry on another authority, and never
    constructs the credentialless transport or issues an anonymous GET (Issue
    #2872 AC3/AC5(e); #2257 split-brain non-regression)."""
    used = _forbid_credentialless_authority(monkeypatch)
    transport = _RecordingTransport(comments_error="gh_exit_1: HTTP 403: API rate limit exceeded")

    result, exit_code = _run_live_preflight(monkeypatch, tmp_path, transport, anchor_urls=[ANCHOR_URL])

    assert exit_code == preflight.EXIT_ENVIRONMENT_FAILURE
    assert result["status"] == "environment_failure"
    assert result["source"] == "recording_native_gh"
    assert result["operation"] == "list_issue_comments"
    assert [name for name, _ in transport.calls] == ["read_issue", "list_issue_comments"]
    assert used == []
    monkeypatch.undo()

    # Same failure through the PRODUCTION selector under an isolated HOME: the
    # failing authority is native `gh`, reported as such, with no anonymous
    # retry (no credentialless construction, no raw GET).
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: tmp_path)
    _force_isolated_profile(tmp_path, monkeypatch)
    used = _forbid_credentialless_authority(monkeypatch)
    argvs: list[list[str]] = []

    def _fake_run_gh(argv, timeout=preflight.GH_API_TIMEOUT):
        argvs.append(list(argv))
        if argv[:3] == ["gh", "issue", "view"]:
            return dict(_ISSUE_PAYLOAD), ""
        return None, "gh_exit_1: HTTP 403: API rate limit exceeded"

    monkeypatch.setattr(preflight, "_run_gh", _fake_run_gh)
    result, exit_code = preflight.run_preflight(
        issue_number=ISSUE_NUMBER, repo=REPO, anchor_comment_urls=[ANCHOR_URL], fixture_path=None
    )
    assert exit_code == preflight.EXIT_ENVIRONMENT_FAILURE
    assert result["source"] == "gh_cli"
    assert result["operation"] == "list_issue_comments"
    assert [a[:2] for a in argvs] == [["gh", "issue"], ["gh", "api"]], argvs
    assert used == []


def test_multipage_comments_anchor_resolution_single_transport(tmp_path, monkeypatch):
    """GIVEN the real native `gh` transport and a two-page paginated comments
    response (`gh api --paginate --slurp` shape) whose second page holds the
    anchor comment
    WHEN `run_preflight()` resolves the anchor
    THEN exactly one Issue read and one comments traversal run through the
    same `gh` transport, the anchor is resolved from the complete traversal
    (NO extra single-comment GET) and nothing goes anonymous (Issue #2872
    AC5(f))."""
    _force_isolated_profile(tmp_path, monkeypatch)
    used = _forbid_credentialless_authority(monkeypatch)
    argvs: list[list[str]] = []
    page_1 = [{"id": 111, "body": "first page comment"}]
    page_2 = [dict(_ANCHOR_PAYLOAD)]

    def _fake_run_gh(argv, timeout=preflight.GH_API_TIMEOUT):
        argvs.append(list(argv))
        if argv[:3] == ["gh", "issue", "view"]:
            return dict(_ISSUE_PAYLOAD), ""
        if "--slurp" in argv and any("/comments" in a for a in argv):
            return [page_1, page_2], ""
        raise AssertionError(f"unexpected extra gh read: {argv}")

    monkeypatch.setattr(preflight, "_run_gh", _fake_run_gh)
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: tmp_path)

    preflight.run_preflight(
        issue_number=ISSUE_NUMBER, repo=REPO, anchor_comment_urls=[ANCHOR_URL], fixture_path=None
    )

    assert len(argvs) == 2, argvs
    assert argvs[0][:3] == ["gh", "issue", "view"]
    assert any(f"repos/{REPO}/issues/{ISSUE_NUMBER}/comments" in a for a in argvs[1]), argvs[1]
    assert not any(f"/issues/comments/{ANCHOR_COMMENT_ID}" in " ".join(a) for a in argvs)
    assert used == []


def test_selector_is_constant_and_ignores_every_auth_signal(tmp_path, monkeypatch):
    """The selector returns one process-wide native `gh` instance regardless
    of HOME, token env, `GH_CONFIG_DIR` and `GH_HOST` (no auth-state
    inspection exists to depend on; Issue #2872 AC4/AC8)."""
    first = preflight._select_read_transport()
    _force_isolated_profile(tmp_path, monkeypatch)
    monkeypatch.setenv("GH_TOKEN", TOKEN_SENTINEL)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("GH_HOST", "example.invalid")
    assert preflight._select_read_transport() is first
    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.delenv("GH_CONFIG_DIR")
    assert preflight._select_read_transport() is first
    assert first is preflight._NATIVE_GH_READ_TRANSPORT


def test_default_and_explicit_fetch_call_paths_land_on_one_transport_instance(tmp_path, monkeypatch):
    """Call-recording proof that the process-wide constant selector cannot
    split authority (Issue #2872 AC3): the production constant is swapped for
    ONE recording instance, then the `transport=None` default fetchers
    (`_fetch_issue` / `_fetch_issue_comments` / `_fetch_single_comment`), the
    explicit-`transport=` default-fetcher call sites added for the
    repair-apply / trusted-anchor lane, and a real `run_preflight()` all run.
    Every recorded read lands on that single instance and nothing reaches the
    anonymous credentialless authority."""
    used = _forbid_credentialless_authority(monkeypatch)
    recorder = _RecordingTransport(comments=[dict(_ANCHOR_PAYLOAD)], anchor=dict(_ANCHOR_PAYLOAD))
    monkeypatch.setattr(preflight, "_NATIVE_GH_READ_TRANSPORT", recorder)
    assert preflight._select_read_transport() is recorder

    # `transport=None` default fetchers resolve the one constant instance.
    assert preflight._fetch_issue(REPO, ISSUE_NUMBER) == (dict(_ISSUE_PAYLOAD), "")
    assert preflight._fetch_issue_comments(REPO, ISSUE_NUMBER) == ([dict(_ANCHOR_PAYLOAD)], "")
    assert preflight._fetch_single_comment(REPO, ANCHOR_COMMENT_ID) == (dict(_ANCHOR_PAYLOAD), "")
    # An explicitly passed instance is honoured as-is (explicit threading).
    explicit = _RecordingTransport()
    preflight._fetch_issue(REPO, ISSUE_NUMBER, transport=explicit)
    assert [name for name, _ in explicit.calls] == ["read_issue"]
    assert [name for name, _ in recorder.calls] == ["read_issue", "list_issue_comments", "read_issue_comment"]

    # A real production invocation: Issue, comments (anchor resolved out of
    # the complete traversal) all land on the same instance.
    recorder.calls.clear()
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: tmp_path)
    preflight.run_preflight(issue_number=ISSUE_NUMBER, repo=REPO, anchor_comment_urls=[ANCHOR_URL], fixture_path=None)
    assert [name for name, _ in recorder.calls][:2] == ["read_issue", "list_issue_comments"], recorder.calls
    assert used == []


def test_unthreaded_read_call_sites_are_a_closed_set_resolving_the_constant_selector():
    """Structural guard: every call site of the three `_fetch_*` readers that
    does NOT pass `transport=` is a known, closed set of functions. Those
    sites resolve the `transport=None` default, which is the one
    process-wide constant instance (see
    `test_default_and_explicit_fetch_call_paths_land_on_one_transport_instance`).
    They are intentionally not threaded explicitly: pre-existing tests
    outside this module's Allowed Paths (e.g.
    `test_structural_repair_action_apply_consumer.py`,
    `test_evidence_index_preflight_integration.py`) patch the readers with
    fixed-arity doubles. A NEW un-threaded call site must be threaded or
    consciously added here -- it cannot silently pick its own authority."""
    import ast

    tree = ast.parse(Path(preflight.__file__).read_text(encoding="utf-8"))
    readers = {"_fetch_issue", "_fetch_issue_comments", "_fetch_single_comment"}
    unthreaded: list[tuple[str, str]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[str] = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name in readers and "transport" not in {kw.arg for kw in node.keywords}:
                unthreaded.append((name, self.stack[-1] if self.stack else "<module>"))
            self.generic_visit(node)

    _Visitor().visit(tree)
    assert {fn for _, fn in unthreaded} <= {
        "run_preflight",
        "_default_fetch_current",
        "_revalidate_owner_anchor_sources_before_dispatch",
        "fetch_current",
    }, unthreaded


def test_gh_exit_4_is_projected_as_gh_auth_required():
    """gh's documented authentication-required exit code maps to the closed
    `gh_auth_required` reason, never to a generic `gh_exit_error`."""
    assert preflight._project_environment_failure_reason("gh_exit_4: authentication required") == "gh_auth_required"
    assert preflight._project_environment_failure_reason("gh_exit_1: boom") == "gh_exit_error"


# ---------------------------------------------------------------------------
# Issue #2872 AC9 runtime verification (opt-in `github_live`; deselected from
# the default python-test run and NOT a required CI gate -- #2361).
# ---------------------------------------------------------------------------

_AC9_ISSUE_NUMBER = 2845
_AC9_ANCHOR_URL = f"https://github.com/{REPO}/issues/{_AC9_ISSUE_NUMBER}#issuecomment-5942301496"
_AC9_UNAVAILABLE_REASON_CODES = frozenset(
    {"rate_limited", "upstream_environment_failure", "transport_connectivity_failure"}
)


@pytest.mark.github_live
def test_ac9_isolated_preflight_reaches_step1_with_native_gh_auth_runtime():
    """GIVEN a fresh interactive Claude-GPT session (launcher-provided
    isolated HOME) at the canonical main root with `gh` authenticated (token
    env OR stored `GH_CONFIG_DIR`; this test never reads either)
    WHEN the real `skill_runtime_exec.py --command-id
    preflight.run.with_human_context` is run against #2845's anchor
    `5942301496` (read-only)
    THEN Issue/comments/anchor read via native `gh` and preflight proceeds
    past the read phase: no `rate_limited` / `credentialless_transport` /
    `read_issue` environment_failure. GitHub/runtime/gh unavailability is
    reported as SKIP (exit-77 semantics), never promoted to PASS; a
    downstream contract blocker is a Step-1 reach, not an AC9 failure
    (Issue #2872 AC9)."""
    import shutil
    import subprocess
    from datetime import datetime, timezone

    if shutil.which("gh") is None or shutil.which("uv") is None:
        pytest.skip("UNAVAILABLE: gh/uv not installed (exit 77 semantics)")

    def _git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, cwd=str(REPO_ROOT), check=False
        ).stdout.strip()

    if "/.claude/worktrees/" in _git("rev-parse", "--show-toplevel") or _git("branch", "--show-current") != "main":
        pytest.skip("UNAVAILABLE: canonical main root on the default branch required (exit 77 semantics)")

    proc = subprocess.run(
        [
            "uv", "run", "python3", "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id", "preflight.run.with_human_context",
            "--issue-number", str(_AC9_ISSUE_NUMBER),
            "--repo", REPO,
            "--anchor-comment-url", _AC9_ANCHOR_URL,
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    fields = {
        key: [ln[len(key) + 1 :].strip() for ln in proc.stdout.splitlines() if ln.startswith(key + ":")]
        for key in ("STATUS", "REASON_CODE", "SOURCE", "OPERATION")
    }
    if (
        fields["STATUS"] == ["environment_failure"]
        and fields["REASON_CODE"]
        and fields["REASON_CODE"][0] in _AC9_UNAVAILABLE_REASON_CODES
        and fields["SOURCE"] != ["credentialless_transport"]
    ):
        pytest.skip(f"UNAVAILABLE: external GitHub unavailable ({fields['REASON_CODE']}); exit 77 semantics")

    # Sanitized evidence only (never credentials, transcripts or HOME paths).
    gh_version = subprocess.run(["gh", "--version"], capture_output=True, text=True, check=False).stdout.splitlines()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifacts = REPO_ROOT / "artifacts"
    try:
        artifacts.mkdir(exist_ok=True)
        (artifacts / f"runtime-verification-AC9-{stamp}.log").write_text(
            "\n".join(
                [
                    f"commit_sha: {_git('rev-parse', 'HEAD')}",
                    f"gh_version: {gh_version[0] if gh_version else 'unknown'}",
                    "auth_form: native gh (value and credential files never recorded)",
                    f"exit_code: {proc.returncode}",
                    f"status: {fields['STATUS']}",
                    f"reason_code: {fields['REASON_CODE']}",
                    f"source: {fields['SOURCE']}",
                    f"operation: {fields['OPERATION']}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass

    assert fields["SOURCE"] != ["credentialless_transport"], fields
    assert not (
        fields["STATUS"] == ["environment_failure"] and fields["OPERATION"] in (["read_issue"], ["list_issue_comments"])
    ), f"read phase failed: {fields}"
    assert fields["STATUS"], "preflight produced no STATUS (did not reach Step 1)"
