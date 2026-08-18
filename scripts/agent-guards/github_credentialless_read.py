#!/usr/bin/env python3
"""Credentialless public GitHub REST read for Claude-GPT isolated sessions
(Issue #2241, hardened per PR #2247 human review).

This module exists so an isolated Claude-GPT session -- which intentionally
never receives the host's `GH_TOKEN` / `GITHUB_TOKEN` / `GH_CONFIG_DIR`
(Issue #2232 comment 5316900237 root cause) -- can still read a *public*
GitHub Issue (and its comments) in this repository without any
authentication. It is deliberately narrow:

  - GET only. There is no function anywhere in this module that constructs
    a request with any HTTP method other than GET. This is a hard,
    read-only boundary enforced at the source level, not a runtime flag.
  - Repository-bound. `TRUSTED_REPO_SLUG` is a fixed constant
    ("squne121/loop-protocol"); every read helper rejects any other
    repository slug before making a network call.
  - Never sends an `Authorization` header. Host credentials
    (`GH_TOKEN` / `GITHUB_TOKEN` / `GH_CONFIG_DIR`) are neither read from
    nor forwarded into the request. `sanitized_env()` below documents and
    enforces this even for any future subprocess-based caller.
  - Never follows an HTTP redirect. `_RedirectRejectingHandler` makes every
    redirect (301/302/303/307/308, regardless of target host/scheme) a
    hard `CrossRepositoryReadRejected` instead of an implicit follow --
    GitHub Issue transfer between repositories is served as a redirect, and
    a follow would silently defeat the repository-bound check above
    (PR #2247 review P1-2).

Out of scope (Issue #2241 "Out of Scope" / Stop Conditions): a GET-only
authenticated read broker for higher rate limits. This module intentionally
accepts the anonymous GitHub REST rate limit (60 requests/hour per source
IP) until that limit is demonstrated to be insufficient for normal
workload -- at which point a follow-up Issue should design an authenticated
broker, not this module.

PR #2247 review P1-1 note: this module provides the credentialless
transport primitives (`read_public_issue`, `list_issue_comments`,
`CredentiallessGitHubReadTransport`). Wiring `run_refinement_preflight.py`'s
`_fetch_issue()` / `_fetch_issue_comments()` to select this transport under
an isolated-session profile requires editing
`.claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py`,
which is outside this Issue's Allowed Paths -- see the PR body "Not
controlled" section and the accompanying human-review reply comment.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Protocol

# Fixed trust boundary: this module only ever reads from this repository.
# Never derived from an environment variable or CLI argument default --
# callers must pass this value explicitly to widen scope, and every
# public read function still rejects anything else.
TRUSTED_REPO_SLUG = "squne121/loop-protocol"

GITHUB_API_BASE = "https://api.github.com"
_TRUSTED_API_HOST = "api.github.com"

# Host GitHub credential environment keys that must never be read by this
# module, and must never be present in any environment this module builds
# for a subprocess (this module does not spawn any subprocess today, but
# `sanitized_env()` exists so a future caller cannot accidentally forward
# these into one).
_CREDENTIAL_ENV_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR")

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# GitHub's paginated `Link` response header, e.g.:
#   <https://api.github.com/...&page=2>; rel="next", <...>; rel="last"
_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


class CredentiallessReadError(Exception):
    """Base class for credentialless-read rejections."""


class CrossRepositoryReadRejected(CredentiallessReadError):
    """Raised when a caller requests a repository other than
    `TRUSTED_REPO_SLUG` (Issue #2241 AC4), or when the underlying HTTP
    transport attempts to redirect to a different repository/host/scheme
    (PR #2247 review P1-2)."""


class InvalidIssueNumberRejected(CredentiallessReadError):
    """Raised when the requested issue number is not a positive integer."""


class RateLimitedRejected(CredentiallessReadError):
    """Raised on HTTP 403/429 (anonymous rate limit exhausted or abuse
    detection). Distinguished from `UnexpectedAuthenticationDependency` and
    `CanonicalResourceMissing` so callers can tell "try again later" apart
    from "this read structurally cannot succeed" (PR #2247 review P1-4.1)."""


class UnexpectedAuthenticationDependency(CredentiallessReadError):
    """Raised on HTTP 401: a public, unauthenticated GET should never
    require authentication. Signals a real problem (the resource is not
    actually public, or GitHub's anonymous-read contract changed), not a
    transient condition (PR #2247 review P1-4.1)."""


class CanonicalResourceMissing(CredentiallessReadError):
    """Raised on HTTP 404: the requested Issue/comment does not exist at
    the trusted, canonical URL (PR #2247 review P1-4.1)."""


class UpstreamEnvironmentFailure(CredentiallessReadError):
    """Raised on HTTP 5xx: an upstream GitHub failure, not a boundary
    violation and not a client error. Callers must not treat this as PASS
    (it is a real failure to obtain the resource) but should distinguish it
    from the fail-closed classes above (PR #2247 review P1-4.1)."""


def sanitized_env() -> dict[str, str]:
    """Return a copy of the current process environment with every known
    host GitHub credential key removed (Issue #2241 AC2).

    This module performs its network I/O in-process via `urllib.request`
    and never spawns a subprocess, so nothing here actually consumes this
    mapping today. It is provided as an explicit, independently-testable
    guarantee: even if a future change in this module (or a caller) adds a
    subprocess invocation, using `sanitized_env()` for that subprocess's
    environment keeps host credentials from leaking into it.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _CREDENTIAL_ENV_KEYS
    }


def _validate_repo(repo: str) -> None:
    if repo != TRUSTED_REPO_SLUG or not _REPO_SLUG_RE.match(repo):
        raise CrossRepositoryReadRejected(f"cross_repository_read_rejected:{repo!r}")


def _validate_issue_number(issue_number: int) -> None:
    if isinstance(issue_number, bool) or not isinstance(issue_number, int) or issue_number <= 0:
        raise InvalidIssueNumberRejected(f"invalid_issue_number:{issue_number!r}")


class _RedirectRejectingHandler(urllib.request.HTTPRedirectHandler):
    """Rejects every HTTP redirect outright (Issue #2241 / PR #2247 review
    P1-2).

    GitHub serves an Issue-transfer-to-another-repository as a redirect
    (e.g. HTTP 301) to the new canonical URL. Silently following it would
    let this module read (and implicitly trust) an Issue outside
    `TRUSTED_REPO_SLUG` even though the caller-supplied `repo` argument was
    validated up front -- the redirect target's repository identity is
    never re-validated by a plain `urlopen()` follow. This handler makes
    ALL redirects (any status code, any target host, any target scheme --
    including an HTTPS-to-HTTP downgrade) a hard failure instead of an
    implicit follow. There is no "same repository, so allow it" carve-out:
    a trusted read must reach its answer via the URL this module
    constructed, in one hop, or not at all.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise CrossRepositoryReadRejected(
            f"redirect_rejected:status={code}:from={req.full_url!r}:to={newurl!r}"
        )

    # `HTTPRedirectHandler` also defines `http_error_301`/`http_error_302`/
    # etc. as thin wrappers around `redirect_request` for the legacy
    # `http_error_*` dispatch path; overriding `redirect_request` alone is
    # sufficient because those wrappers all delegate to it.


# Built once at import time. `build_opener` replaces the default
# `HTTPRedirectHandler`-derived handler with this subclass (it detects the
# subclass relationship), so every request issued through `_opener` -- and
# only requests issued through `_opener`, never the module-level
# `urllib.request.urlopen` -- is redirect-rejecting.
_opener = urllib.request.build_opener(_RedirectRejectingHandler)


def _classify_http_error(exc: urllib.error.HTTPError) -> Exception:
    """Map an `HTTPError` status code to the specific exception class
    callers should see (PR #2247 review P1-4.1).

    401 -> UnexpectedAuthenticationDependency (a public GET should never
           need auth)
    403, 429 -> RateLimitedRejected (anonymous rate limit / abuse
           detection -- also covers GitHub's documented behavior of
           returning 403 rather than 429 for the unauthenticated REST rate
           limit)
    404 -> CanonicalResourceMissing
    5xx -> UpstreamEnvironmentFailure
    anything else -> the original HTTPError, unmodified
    """
    status = exc.code
    if status == 401:
        return UnexpectedAuthenticationDependency(f"unexpected_authentication_dependency:{status}")
    if status in (403, 429):
        return RateLimitedRejected(f"rate_limited:{status}")
    if status == 404:
        return CanonicalResourceMissing(f"canonical_resource_missing:{status}")
    if 500 <= status < 600:
        return UpstreamEnvironmentFailure(f"upstream_environment_failure:{status}")
    return exc


def _build_request(url: str) -> urllib.request.Request:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "loop-protocol-credentialless-read (Issue-2241)")
    # Deliberately no Authorization header: no GH_TOKEN / GITHUB_TOKEN /
    # GH_CONFIG_DIR is ever read from the environment by this function.
    return request


def _credentialless_get(url: str, *, timeout: float = 15.0) -> dict:
    """Issue a single, unauthenticated GET request and return the parsed
    JSON body.

    This is the only function in this module that opens a network
    connection without also returning pagination metadata, and it is
    hardcoded to the GET method. No caller in this module -- and no public
    function exported by it -- can cause a non-read HTTP method to be
    issued, and no response is ever followed through a redirect (see
    `_RedirectRejectingHandler`).
    """
    data, _next_url = _credentialless_get_page(url, timeout=timeout)
    return data


def _credentialless_get_page(url: str, *, timeout: float = 15.0) -> tuple[object, "str | None"]:
    """Issue a single, unauthenticated GET request and return
    `(parsed_json_body, next_page_url_or_None)`.

    `next_page_url_or_None` is populated from the response's `Link:
    rel="next"` header (RFC 5988), used by `list_issue_comments` to follow
    pagination without ever falling back to an unbounded/unauthenticated
    `gh api --paginate` shell invocation.
    """
    request = _build_request(url)
    try:
        with _opener.open(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
            link_header = response.headers.get("Link", "")
    except urllib.error.HTTPError as exc:
        raise _classify_http_error(exc) from exc
    data = json.loads(raw)
    next_match = _LINK_NEXT_RE.search(link_header or "")
    next_url = next_match.group(1) if next_match else None
    if next_url is not None:
        _validate_pagination_target(next_url)
    return data, next_url


def _validate_pagination_target(url: str) -> None:
    """Re-validate a `Link: rel="next"` URL before it is followed
    (PR #2247 review P1-2 defense-in-depth): pagination is a second,
    server-supplied URL this module did not construct itself, so it gets
    the same host/scheme trust check as the redirect handler applies to
    redirects, rather than being followed unconditionally."""
    parsed_scheme, _, rest = url.partition("://")
    if parsed_scheme != "https":
        raise CrossRepositoryReadRejected(f"pagination_scheme_rejected:{url!r}")
    host = rest.split("/", 1)[0].split("@")[-1].split(":")[0]
    if host != _TRUSTED_API_HOST:
        raise CrossRepositoryReadRejected(f"pagination_host_rejected:{url!r}")


def read_public_issue(issue_number: int, repo: str = TRUSTED_REPO_SLUG) -> dict:
    """Read a public GitHub Issue in `repo` with no authentication.

    Raises `CrossRepositoryReadRejected` if `repo` is not
    `TRUSTED_REPO_SLUG` (Issue #2241 AC4), `InvalidIssueNumberRejected` if
    `issue_number` is not a positive integer (both checks happen before any
    network call is attempted), and one of `UnexpectedAuthenticationDependency`
    / `RateLimitedRejected` / `CanonicalResourceMissing` /
    `UpstreamEnvironmentFailure` for the corresponding HTTP status
    (PR #2247 review P1-4.1).
    """
    _validate_repo(repo)
    _validate_issue_number(issue_number)
    url = f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}"
    return _credentialless_get(url)


def list_issue_comments(issue_number: int, repo: str = TRUSTED_REPO_SLUG, *, per_page: int = 100) -> list[dict]:
    """Read every comment on a public GitHub Issue in `repo` with no
    authentication, following `Link: rel="next"` pagination until
    exhausted (PR #2247 review P1-1).

    Returns comments in the same REST JSON shape `gh api
    repos/{repo}/issues/{issue_number}/comments` would return (this
    endpoint IS that REST endpoint), so a caller that already consumes
    `gh api` comment JSON needs no field-name translation.
    """
    _validate_repo(repo)
    _validate_issue_number(issue_number)
    if not isinstance(per_page, int) or not (1 <= per_page <= 100):
        raise CredentiallessReadError(f"invalid_per_page:{per_page!r}")
    comments: list[dict] = []
    url = f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}/comments?per_page={per_page}"
    seen_urls: set[str] = set()
    while url:
        if url in seen_urls:
            raise CredentiallessReadError(f"pagination_cycle_detected:{url!r}")
        seen_urls.add(url)
        page, url = _credentialless_get_page(url)
        if not isinstance(page, list):
            raise CredentiallessReadError(f"unexpected_comments_page_type:{type(page).__name__}")
        comments.extend(page)
    return comments


def issue_to_gh_cli_shape(raw_issue: dict) -> dict:
    """Convert a raw GitHub REST `/issues/{number}` JSON body (as returned
    by `read_public_issue`) into the field-name shape
    `gh issue view --json number,title,body,labels,url,updatedAt` produces
    (PR #2247 review P1-1) -- so a caller that already consumes the `gh`
    CLI shape (e.g. `run_refinement_preflight.py`'s `_fetch_issue`) can
    switch transports without a schema change downstream.
    """
    labels_raw = raw_issue.get("labels") or []
    labels = [
        {"name": label.get("name")}
        for label in labels_raw
        if isinstance(label, dict) and label.get("name") is not None
    ]
    return {
        "number": raw_issue.get("number"),
        "title": raw_issue.get("title"),
        "body": raw_issue.get("body") or "",
        "labels": labels,
        "url": raw_issue.get("html_url"),
        "updatedAt": raw_issue.get("updated_at"),
    }


class GitHubReadTransport(Protocol):
    """Transport-selector interface (PR #2247 review P1-1): a caller that
    needs to fetch an Issue and its comments without caring whether the
    underlying transport is `gh` (authenticated) or this credentialless
    module can depend on this Protocol instead of importing either
    transport module directly."""

    def read_issue(self, repo: str, issue_number: int) -> dict:
        ...

    def list_issue_comments(self, repo: str, issue_number: int) -> list[dict]:
        ...


class CredentiallessGitHubReadTransport:
    """`GitHubReadTransport` implementation backed entirely by this
    module's unauthenticated REST GET functions. Returns the `gh` CLI
    field-name shape for `read_issue` (via `issue_to_gh_cli_shape`) and the
    raw REST shape for `list_issue_comments` (already `gh api`-shaped) so a
    caller written against `gh`-shaped data needs no other changes."""

    def read_issue(self, repo: str, issue_number: int) -> dict:
        return issue_to_gh_cli_shape(read_public_issue(issue_number, repo=repo))

    def list_issue_comments(self, repo: str, issue_number: int) -> list[dict]:
        return list_issue_comments(issue_number, repo=repo)


__all__ = [
    "TRUSTED_REPO_SLUG",
    "GITHUB_API_BASE",
    "CredentiallessReadError",
    "CrossRepositoryReadRejected",
    "InvalidIssueNumberRejected",
    "RateLimitedRejected",
    "UnexpectedAuthenticationDependency",
    "CanonicalResourceMissing",
    "UpstreamEnvironmentFailure",
    "sanitized_env",
    "read_public_issue",
    "list_issue_comments",
    "issue_to_gh_cli_shape",
    "GitHubReadTransport",
    "CredentiallessGitHubReadTransport",
]
