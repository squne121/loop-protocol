"""Issue #2241: credentialless public GitHub REST read for Claude-GPT
isolated sessions.

Covers AC2 (host credentials never exposed), AC3 (unauthenticated public
Issue read succeeds), and AC4 (cross-repository / non-GET rejection).
"""

from __future__ import annotations

import inspect
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


def test_credentialless_get_never_sets_authorization_header(monkeypatch):
    """GIVEN GH_TOKEN is set in the process environment
    WHEN `_credentialless_get` builds its request
    THEN the outgoing request never carries an Authorization header, and
    the request is opened via `urllib.request.urlopen` with no credential
    material passed anywhere (Issue #2241 AC2)."""
    monkeypatch.setenv("GH_TOKEN", "super-secret-host-token")

    captured: dict[str, object] = {}

    class _FakeResponse:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(request, timeout=None):
        captured["headers"] = dict(request.header_items())
        captured["method"] = request.get_method()
        captured["url"] = request.full_url
        return _FakeResponse()

    monkeypatch.setattr(gcr.urllib.request, "urlopen", _fake_urlopen)

    result = gcr._credentialless_get("https://api.github.com/repos/squne121/loop-protocol/issues/1")

    assert result == {"ok": True}
    assert captured["method"] == "GET"
    header_names_lower = {k.lower() for k in captured["headers"]}
    assert "authorization" not in header_names_lower
    for value in captured["headers"].values():
        assert "super-secret-host-token" not in str(value)


@pytest.mark.parametrize("issue_number", [1])
def test_credentialless_public_issue_read_succeeds(issue_number):
    """GIVEN a real, public GitHub Issue in squne121/loop-protocol
    WHEN `read_public_issue` is called with no authentication configured
    THEN the read succeeds and returns the issue's canonical JSON body
    (Issue #2241 AC3)."""
    try:
        result = gcr.read_public_issue(issue_number)
    except (OSError, urllib.error.URLError) as exc:  # pragma: no cover
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
