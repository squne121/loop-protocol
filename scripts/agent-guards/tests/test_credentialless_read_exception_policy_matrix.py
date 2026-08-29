"""Issue #2361 AC2: fault-injection matrix for the exception-handling
policy inside `test_credentialless_public_issue_read_succeeds`
(`test_credentialless_github_read.py`, the credentialless GitHub read
live positive control, Issue #2241 AC3).

The live positive-control test's actual design (PR #2363 OWNER review,
comment 5445567396) does not enumerate the production transport module's
(`scripts/agent-guards/github_credentialless_read.py`) exception taxonomy
at all: it catches only `TransportConnectivityFailure` (DNS/egress
unavailable -- no HTTP response was ever received) and treats that as an
environment SKIP. Every other exception a `read_public_issue` call can
raise -- every other `CredentiallessReadError` subclass
(`RateLimitedRejected`, `ForbiddenRejected`,
`UnexpectedAuthenticationDependency`, `CanonicalResourceMissing`,
`UpstreamEnvironmentFailure`, `MalformedResponseBody`, or any future
addition to that module) and any raw/unmapped `urllib.error.HTTPError` --
is left uncaught by the live test and propagates, which pytest reports as
a FAIL. Because the live test never lists these classes by name, this
matrix does not need to be updated every time the production taxonomy
gains a new exception class for the fail-closed contract (Issue #2241
AC3, Issue #2313 investigation) to keep holding.

This module injects representative exceptions via `monkeypatch` directly
against the live positive-control test function (loaded from its source
file, not executed through a nested pytest collection) and asserts each
one reaches the correct pytest outcome: `TransportConnectivityFailure`
resolves to `pytest.skip.Exception` (SKIP); every other exception
propagates unmodified out of the test function (which pytest reports as a
FAIL), verified here by asserting the exact original exception type is
what escapes -- so a future regression that widens the SKIP net (e.g. an
added `except` clause that intercepts and skips a non-transport failure)
is caught deterministically, without depending on real network access.
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.error
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
_LIVE_TEST_FILE = _GUARDS_DIR / "tests" / "test_credentialless_github_read.py"

if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import github_credentialless_read as gcr  # noqa: E402


def _load_live_test_module():
    """Load `test_credentialless_github_read.py` under a dedicated,
    unique module name so this in-process load never collides with
    pytest's own (separate, `--import-mode=importlib`-based) collection
    of that same file elsewhere in a full-suite run. Registering the
    module in `sys.modules` under that unique name before `exec_module`
    follows the repository's established manual-module-load isolation
    convention (avoids re-entrant import surprises even though this
    module has no circular import back to itself)."""
    module_name = "loop_protocol_issue2361_credentialless_read_policy_matrix_target"
    spec = importlib.util.spec_from_file_location(module_name, _LIVE_TEST_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_live_test_module = _load_live_test_module()
# `pytest.mark.github_live` / `pytest.mark.parametrize` only attach marker
# metadata used by pytest's collector -- the underlying function object is
# still a plain callable that takes `issue_number` positionally, so it can
# be invoked directly here without going through pytest collection.
_TARGET = _live_test_module.test_credentialless_public_issue_read_succeeds


def test_transport_connectivity_failure_is_skip(monkeypatch):
    """GIVEN `read_public_issue` raises `TransportConnectivityFailure`
    (DNS/egress unavailable -- no HTTP response was ever received)
    WHEN the live positive-control test body runs
    THEN it resolves to a pytest SKIP, via the public `pytest.skip.Exception`
    alias (Issue #2361 AC2)."""

    def _raise(issue_number):
        raise gcr.TransportConnectivityFailure("transport_connectivity_failure:test_injected")

    monkeypatch.setattr(gcr, "read_public_issue", _raise)

    with pytest.raises(pytest.skip.Exception):
        _TARGET(1)


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: gcr.RateLimitedRejected("rate_limited:test_injected"),
        lambda: gcr.ForbiddenRejected("http_403_forbidden:test_injected"),
        lambda: gcr.UnexpectedAuthenticationDependency(
            "unexpected_authentication_dependency:test_injected"
        ),
        lambda: gcr.CanonicalResourceMissing("canonical_resource_missing:test_injected"),
        lambda: gcr.UpstreamEnvironmentFailure("upstream_environment_failure:test_injected"),
        lambda: gcr.MalformedResponseBody("malformed_response_body:test_injected"),
    ],
    ids=[
        "RateLimitedRejected",
        "ForbiddenRejected",
        "UnexpectedAuthenticationDependency",
        "CanonicalResourceMissing",
        "UpstreamEnvironmentFailure",
        "MalformedResponseBody",
    ],
)
def test_classified_non_transport_failures_propagate_uncaught(monkeypatch, exc_factory):
    """GIVEN `read_public_issue` raises one of the structured
    `CredentiallessReadError` subclasses other than
    `TransportConnectivityFailure` (including `MalformedResponseBody`,
    which is not derived from an HTTP status via `_classify_http_error`)
    WHEN the live positive-control test body runs
    THEN the exact same exception instance propagates uncaught out of the
    test body -- the live test's only `except` clause matches
    `TransportConnectivityFailure`, so nothing here is silently downgraded
    to SKIP -- meaning pytest reports a FAIL when this actually runs under
    pytest collection (Issue #2361 AC2 -- matches Issue #2241 AC3's
    fail-closed contract: a real HTTP response that structurally cannot
    succeed, or a response body that cannot even be parsed, must never be
    hidden as an environment SKIP). Because the live test does not name
    this exception's class in an `except` clause, this same assertion
    holds for any future `CredentiallessReadError` subclass without
    requiring a change to the live test (PR #2363 OWNER review, comment
    5445567396)."""

    exc = exc_factory()

    def _raise(issue_number):
        raise exc

    monkeypatch.setattr(gcr, "read_public_issue", _raise)

    with pytest.raises(type(exc)) as excinfo:
        _TARGET(1)
    assert excinfo.value is exc


def test_unmapped_http_error_propagates_uncaught(monkeypatch):
    """GIVEN `read_public_issue` raises a raw `urllib.error.HTTPError` for
    an HTTP status `_classify_http_error` does not map to any named
    taxonomy class (e.g. HTTP 418)
    WHEN the live positive-control test body runs
    THEN the `HTTPError` propagates uncaught -- it is not a
    `TransportConnectivityFailure`, so it is never intercepted -- meaning
    pytest reports a FAIL when this actually runs under pytest collection,
    not a SKIP.

    This is the exact historical regression Issue #2361 AC2 corrects:
    `urllib.error.HTTPError` is a `urllib.error.URLError` subclass, so the
    previous broad `except urllib.error.URLError` clause silently
    swallowed an unmapped/unclassified HTTP error response as an
    environment SKIP instead of failing it."""

    def _raise(issue_number):
        raise urllib.error.HTTPError(
            "https://api.github.com/repos/squne121/loop-protocol/issues/1",
            418,
            "I'm a teapot",
            None,
            None,
        )

    monkeypatch.setattr(gcr, "read_public_issue", _raise)

    with pytest.raises(urllib.error.HTTPError):
        _TARGET(1)


def test_successful_read_returns_normally_without_skip_or_fail(monkeypatch):
    """GIVEN `read_public_issue` succeeds and returns a canonical issue body
    WHEN the live positive-control test body runs
    THEN it returns normally (no SKIP, no FAIL) -- the exception-handling
    policy must not accidentally intercept the success path."""
    fake_result = {
        "number": 1,
        "title": "fixture title",
        "html_url": f"https://github.com/{gcr.TRUSTED_REPO_SLUG}/issues/1",
    }

    def _fake(issue_number):
        return fake_result

    monkeypatch.setattr(gcr, "read_public_issue", _fake)

    _TARGET(1)  # must not raise Skipped or Failed
