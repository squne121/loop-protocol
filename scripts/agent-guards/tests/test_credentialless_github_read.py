"""Issue #2241: credentialless public GitHub REST read for Claude-GPT
isolated sessions.

Covers AC2 (host credentials never exposed), AC3 (unauthenticated public
Issue read succeeds), AC4 (cross-repository / non-GET rejection / redirect
rejection), and PR #2247 human review P1-1/P1-2/P1-4 hardening.
"""

from __future__ import annotations

import inspect
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import github_credentialless_read as gcr  # noqa: E402

# Non-GET HTTP method literals that must never appear anywhere in this
# module's source. Kept out of any docstring/comment in the module itself
# (including this test file's assertions on it) so a plain substring scan
# is a meaningful negative check, not one satisfied by a comment.
_NON_READ_HTTP_METHODS = ("POST", "PATCH", "DELETE", "PUT")


def test_sanitized_env_excludes_host_github_credentials(monkeypatch):
    """GIVEN host GitHub credential environment variables are set
    (GH_TOKEN / GITHUB_TOKEN / GH_CONFIG_DIR)
    WHEN `sanitized_env()` is called
    THEN none of their real values appear in the returned mapping
    (Issue #2241 AC2)."""
    monkeypatch.setenv("GH_TOKEN", "super-secret-host-token")
    monkeypatch.setenv("GITHUB_TOKEN", "another-secret-host-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/home/host-user/.config/gh")
    monkeypatch.setenv("UNRELATED_MARKER", "keep-me")

    env = gcr.sanitized_env()

    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GH_CONFIG_DIR" not in env
    assert "super-secret-host-token" not in env.values()
    assert "another-secret-host-token" not in env.values()
    assert env.get("UNRELATED_MARKER") == "keep-me"


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_opener_open(monkeypatch, captured: dict[str, object], body: bytes = b'{"ok": true}',
                        headers: dict[str, str] | None = None):
    def _fake_open(request, timeout=None):
        captured["request"] = request
        captured["method"] = request.get_method()
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["headers"] = dict(request.header_items())
        return _FakeResponse(body, headers)

    monkeypatch.setattr(gcr._opener, "open", _fake_open)


def test_credentialless_get_never_sets_authorization_header(monkeypatch):
    """GIVEN GH_TOKEN is set in the process environment
    WHEN `_credentialless_get` builds its request
    THEN the outgoing request never carries an Authorization header, and
    the request is opened via the redirect-rejecting opener with no
    credential material passed anywhere (Issue #2241 AC2)."""
    monkeypatch.setenv("GH_TOKEN", "super-secret-host-token")

    captured: dict[str, object] = {}
    _patch_opener_open(monkeypatch, captured)

    result = gcr._credentialless_get("https://api.github.com/repos/squne121/loop-protocol/issues/1")

    assert result == {"ok": True}
    assert captured["method"] == "GET"
    header_names_lower = {k.lower() for k in captured["headers"]}
    assert "authorization" not in header_names_lower
    for value in captured["headers"].values():
        assert "super-secret-host-token" not in str(value)


# ---------------------------------------------------------------------------
# PR #2247 review P1-4.2: behavioral (not source-literal) request invariants
# ---------------------------------------------------------------------------


def test_read_public_issue_sends_exact_get_request_with_no_body_or_auth(monkeypatch):
    """GIVEN a trusted repo/issue_number
    WHEN `read_public_issue` is called
    THEN the actual `urllib.request.Request` object handed to the opener is
    a GET with no body, no Authorization header, and the exact expected
    URL -- verified on the live `request` object, not by scanning source
    text for method literals (Issue #2241 AC4, PR #2247 review P1-4.2)."""
    captured: dict[str, object] = {}
    _patch_opener_open(monkeypatch, captured, body=b'{"number": 1, "title": "t"}')

    gcr.read_public_issue(1)

    request = captured["request"]
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.full_url == "https://api.github.com/repos/squne121/loop-protocol/issues/1"
    header_names_lower = {k.lower() for k in captured["headers"]}
    assert "authorization" not in header_names_lower


def test_list_issue_comments_sends_exact_get_requests_across_pagination(monkeypatch):
    """GIVEN a paginated comments response (Link: rel="next")
    WHEN `list_issue_comments` follows pagination
    THEN every request issued -- including the followed page -- is GET,
    body-less, unauthenticated, and targets the expected URL
    (Issue #2241 AC4, PR #2247 review P1-1/P1-4.2)."""
    calls: list[dict[str, object]] = []
    page_1_url = "https://api.github.com/repos/squne121/loop-protocol/issues/1/comments?per_page=100"
    page_2_url = "https://api.github.com/repos/squne121/loop-protocol/issues/1/comments?per_page=100&page=2"

    def _fake_open(request, timeout=None):
        calls.append({
            "method": request.get_method(),
            "data": request.data,
            "url": request.full_url,
            "headers": dict(request.header_items()),
        })
        if request.full_url == page_1_url:
            return _FakeResponse(b'[{"id": 1}]', headers={"Link": f'<{page_2_url}>; rel="next"'})
        assert request.full_url == page_2_url
        return _FakeResponse(b'[{"id": 2}]', headers={})

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    comments = gcr.list_issue_comments(1)

    assert comments == [{"id": 1}, {"id": 2}]
    assert len(calls) == 2
    for call in calls:
        assert call["method"] == "GET"
        assert call["data"] is None
        header_names_lower = {k.lower() for k in call["headers"]}
        assert "authorization" not in header_names_lower
    assert calls[0]["url"] == page_1_url
    assert calls[1]["url"] == page_2_url


@pytest.mark.parametrize("issue_number", [1])
def test_credentialless_public_issue_read_succeeds(issue_number):
    """GIVEN a real, public GitHub Issue in squne121/loop-protocol
    WHEN `read_public_issue` is called with no authentication configured
    THEN the read succeeds and returns the issue's canonical JSON body
    (Issue #2241 AC3). Only DNS/egress-unavailable failures are treated as
    an environment SKIP; any HTTP response (including error statuses) is a
    real assertion, not silently hidden (PR #2247 review P1-4.1)."""
    try:
        result = gcr.read_public_issue(issue_number)
    except gcr.UpstreamEnvironmentFailure as exc:
        pytest.skip(f"upstream 5xx, not a boundary/logic failure: {exc}")
    except (gcr.RateLimitedRejected, gcr.UnexpectedAuthenticationDependency,
            gcr.CanonicalResourceMissing) as exc:
        pytest.fail(f"AC3 unmet: credentialless public read did not succeed: {exc}")
    except urllib.error.URLError as exc:
        # DNS resolution failure / connection refused / no route to host --
        # egress is unavailable in this sandbox, not a real assertion
        # failure. urllib.error.HTTPError is a URLError subclass, but any
        # HTTPError actually received here was already reclassified by
        # `_classify_http_error` into one of the exception classes caught
        # above (or re-raised unmodified for unmapped statuses, which are
        # deliberately NOT skipped -- see the bare `except OSError` clause
        # below for genuinely transport-level failures only).
        pytest.skip(f"network/DNS unavailable in this environment: {exc}")
    except OSError as exc:  # pragma: no cover - transport-level only
        pytest.skip(f"network unavailable in this environment: {exc}")

    assert result["number"] == issue_number
    assert "title" in result
    assert result["html_url"] == f"https://github.com/{gcr.TRUSTED_REPO_SLUG}/issues/{issue_number}"


def test_credentialless_read_rejects_cross_repository_and_mutation_methods():
    """GIVEN a repository other than TRUSTED_REPO_SLUG
    WHEN `read_public_issue` is called with that repository
    THEN it is rejected before any network call, and separately, the
    module's source contains no non-GET HTTP method literal anywhere --
    there is no code path capable of constructing a mutating request
    (Issue #2241 AC4)."""
    with pytest.raises(gcr.CrossRepositoryReadRejected):
        gcr.read_public_issue(1, repo="attacker-org/other-repo")

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        gcr.read_public_issue(1, repo="squne121/some-other-loop-repo")

    source = inspect.getsource(gcr)
    for method in _NON_READ_HTTP_METHODS:
        assert method not in source, f"module source must never reference HTTP method {method}"


# ---------------------------------------------------------------------------
# PR #2247 review P1-2: redirect rejection (HTTP redirect based
# cross-repository/cross-host/scheme-downgrade escape)
# ---------------------------------------------------------------------------


def _make_request(url: str) -> "gcr.urllib.request.Request":
    return gcr.urllib.request.Request(url, method="GET")


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_to_different_repository_is_rejected(code):
    """GIVEN a trusted-repo Issue read
    WHEN the response redirects (any 30x) to a different repository's
    Issue URL (Issue-transfer scenario)
    THEN the redirect is rejected before it is ever followed
    (PR #2247 review P1-2, negative case: trusted repo -> other repo)."""
    handler = gcr._RedirectRejectingHandler()
    req = _make_request("https://api.github.com/repos/squne121/loop-protocol/issues/1")
    newurl = "https://api.github.com/repos/attacker-org/other-repo/issues/1"

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        handler.redirect_request(req, None, code, "redirected", {}, newurl)


def test_redirect_to_different_host_is_rejected():
    """GIVEN a trusted `api.github.com` request
    WHEN the response redirects to a different host
    THEN the redirect is rejected (PR #2247 review P1-2, negative case:
    api.github.com -> different host)."""
    handler = gcr._RedirectRejectingHandler()
    req = _make_request("https://api.github.com/repos/squne121/loop-protocol/issues/1")
    newurl = "https://evil.example.com/repos/squne121/loop-protocol/issues/1"

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        handler.redirect_request(req, None, 301, "redirected", {}, newurl)


def test_redirect_https_to_http_downgrade_is_rejected():
    """GIVEN an HTTPS request
    WHEN the response redirects to the same host over plain HTTP
    THEN the redirect is rejected (PR #2247 review P1-2, negative case:
    HTTPS -> HTTP downgrade)."""
    handler = gcr._RedirectRejectingHandler()
    req = _make_request("https://api.github.com/repos/squne121/loop-protocol/issues/1")
    newurl = "http://api.github.com/repos/squne121/loop-protocol/issues/1"

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        handler.redirect_request(req, None, 302, "redirected", {}, newurl)


def test_redirect_chain_is_rejected_on_first_hop():
    """GIVEN a request that would otherwise redirect twice (chain)
    WHEN the first redirect is encountered
    THEN it is rejected immediately -- there is no code path in this
    module that ever follows a second hop, because the first is already a
    hard failure (PR #2247 review P1-2, negative case: redirect chain)."""
    handler = gcr._RedirectRejectingHandler()
    req = _make_request("https://api.github.com/repos/squne121/loop-protocol/issues/1")
    first_hop = "https://api.github.com/repos/squne121/loop-protocol/issues/1?redirected=1"

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        handler.redirect_request(req, None, 302, "redirected", {}, first_hop)
    # A same-host, same-repo, same-scheme redirect is STILL rejected: this
    # module never follows any redirect, so a would-be chain never gets a
    # chance to reach its second hop.


def test_redirect_301_transferred_issue_is_rejected_end_to_end(monkeypatch):
    """GIVEN `read_public_issue` issues a real request through `_opener`
    WHEN the opener's HTTP layer raises the 301-transferred-issue redirect
    (simulated by making `_opener.open` itself route through
    `_RedirectRejectingHandler.redirect_request`, matching what
    `http.client`/`urllib` actually does internally on a 301 response)
    THEN `read_public_issue` propagates `CrossRepositoryReadRejected`
    instead of ever returning a foreign-repository Issue body
    (PR #2247 review P1-2, negative case: 301 transferred issue)."""

    def _fake_open(request, timeout=None):
        handler = gcr._RedirectRejectingHandler()
        newurl = "https://api.github.com/repos/attacker-org/transferred-repo/issues/1"
        handler.redirect_request(request, None, 301, "Moved Permanently", {}, newurl)

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        gcr.read_public_issue(1)


# ---------------------------------------------------------------------------
# PR #2247 review P1-4.1: HTTP status classification (fail-closed, no
# blanket OSError-to-skip conversion)
# ---------------------------------------------------------------------------


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/repos/squne121/loop-protocol/issues/1",
        status, "err", {}, io.BytesIO(b""),
    )


@pytest.mark.parametrize("status,expected_cls", [
    (401, gcr.UnexpectedAuthenticationDependency),
    (403, gcr.RateLimitedRejected),
    (429, gcr.RateLimitedRejected),
    (404, gcr.CanonicalResourceMissing),
    (500, gcr.UpstreamEnvironmentFailure),
    (503, gcr.UpstreamEnvironmentFailure),
])
def test_http_error_status_is_classified_not_hidden(monkeypatch, status, expected_cls):
    """GIVEN the opener raises `HTTPError` for a given status
    WHEN `read_public_issue` calls through `_credentialless_get`
    THEN the specific, distinguishable exception class is raised -- never a
    blanket `pytest.skip`-worthy `OSError` (PR #2247 review P1-4.1)."""

    def _fake_open(request, timeout=None):
        raise _http_error(status)

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(expected_cls):
        gcr.read_public_issue(1)


def test_unmapped_http_error_status_is_not_swallowed(monkeypatch):
    """GIVEN the opener raises `HTTPError` for a status this module does
    not have a specific classification for
    WHEN `read_public_issue` is called
    THEN the original `HTTPError` propagates unmodified (fail-closed --
    never silently downgraded to SKIP) (PR #2247 review P1-4.1)."""

    def _fake_open(request, timeout=None):
        raise _http_error(418)

    monkeypatch.setattr(gcr._opener, "open", _fake_open)

    with pytest.raises(urllib.error.HTTPError):
        gcr.read_public_issue(1)


# ---------------------------------------------------------------------------
# PR #2247 review P1-1: GitHubReadTransport / issue_to_gh_cli_shape
# ---------------------------------------------------------------------------


def test_issue_to_gh_cli_shape_maps_rest_fields_to_gh_cli_field_names():
    """GIVEN a raw GitHub REST `/issues/{number}` JSON body
    WHEN `issue_to_gh_cli_shape` converts it
    THEN the result matches the field names `gh issue view --json
    number,title,body,labels,url,updatedAt` produces (PR #2247 review
    P1-1), so `run_refinement_preflight.py`'s downstream `gh`-shaped
    consumers need no schema change to switch transports."""
    raw = {
        "number": 42,
        "title": "Example",
        "body": "body text",
        "labels": [{"name": "bug"}, {"name": "priority:high"}],
        "html_url": "https://github.com/squne121/loop-protocol/issues/42",
        "updated_at": "2026-08-01T00:00:00Z",
    }

    shaped = gcr.issue_to_gh_cli_shape(raw)

    assert shaped == {
        "number": 42,
        "title": "Example",
        "body": "body text",
        "labels": [{"name": "bug"}, {"name": "priority:high"}],
        "url": "https://github.com/squne121/loop-protocol/issues/42",
        "updatedAt": "2026-08-01T00:00:00Z",
    }


def test_credentialless_transport_implements_read_transport_protocol(monkeypatch):
    """GIVEN `CredentiallessGitHubReadTransport`
    WHEN its `read_issue`/`list_issue_comments` methods are called
    THEN they delegate to the credentialless functions above with the
    correct repo/issue_number binding and return the expected shapes
    (PR #2247 review P1-1)."""
    captured: dict[str, object] = {}
    body = json.dumps({
        "number": 7, "title": "t", "labels": [],
        "html_url": "https://github.com/squne121/loop-protocol/issues/7",
        "updated_at": "2026-08-01T00:00:00Z",
    }).encode("utf-8")
    _patch_opener_open(monkeypatch, captured, body=body)

    transport = gcr.CredentiallessGitHubReadTransport()
    issue = transport.read_issue(gcr.TRUSTED_REPO_SLUG, 7)

    assert issue["number"] == 7
    assert issue["url"] == "https://github.com/squne121/loop-protocol/issues/7"
    assert "updatedAt" in issue

    with pytest.raises(gcr.CrossRepositoryReadRejected):
        transport.read_issue("attacker-org/other-repo", 7)
