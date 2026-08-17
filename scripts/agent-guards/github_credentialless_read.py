#!/usr/bin/env python3
"""Credentialless public GitHub REST read for Claude-GPT isolated sessions
(Issue #2241).

This module exists so an isolated Claude-GPT session -- which intentionally
never receives the host's `GH_TOKEN` / `GITHUB_TOKEN` / `GH_CONFIG_DIR`
(Issue #2232 comment 5316900237 root cause) -- can still read a *public*
GitHub Issue in this repository without any authentication. It is
deliberately narrow:

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

Out of scope (Issue #2241 "Out of Scope" / Stop Conditions): a GET-only
authenticated read broker for higher rate limits. This module intentionally
accepts the anonymous GitHub REST rate limit (60 requests/hour per source
IP) until that limit is demonstrated to be insufficient for normal
workload -- at which point a follow-up Issue should design an authenticated
broker, not this module.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

# Fixed trust boundary: this module only ever reads from this repository.
# Never derived from an environment variable or CLI argument default --
# callers must pass this value explicitly to widen scope, and every
# public read function still rejects anything else.
TRUSTED_REPO_SLUG = "squne121/loop-protocol"

GITHUB_API_BASE = "https://api.github.com"

# Host GitHub credential environment keys that must never be read by this
# module, and must never be present in any environment this module builds
# for a subprocess (this module does not spawn any subprocess today, but
# `sanitized_env()` exists so a future caller cannot accidentally forward
# these into one).
_CREDENTIAL_ENV_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR")

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class CredentiallessReadError(Exception):
    """Base class for credentialless-read rejections."""


class CrossRepositoryReadRejected(CredentiallessReadError):
    """Raised when a caller requests a repository other than
    `TRUSTED_REPO_SLUG` (Issue #2241 AC4)."""


class InvalidIssueNumberRejected(CredentiallessReadError):
    """Raised when the requested issue number is not a positive integer."""


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


def _credentialless_get(url: str, *, timeout: float = 15.0) -> dict:
    """Issue a single, unauthenticated GET request and return the parsed
    JSON body.

    This is the only function in this module that opens a network
    connection, and it is hardcoded to the GET method. No caller in this
    module -- and no public function exported by it -- can cause a
    non-read HTTP method to be issued.
    """
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "loop-protocol-credentialless-read (Issue-2241)")
    # Deliberately no Authorization header: no GH_TOKEN / GITHUB_TOKEN /
    # GH_CONFIG_DIR is ever read from the environment by this function.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        raw = response.read()
    return json.loads(raw)


def read_public_issue(issue_number: int, repo: str = TRUSTED_REPO_SLUG) -> dict:
    """Read a public GitHub Issue in `repo` with no authentication.

    Raises `CrossRepositoryReadRejected` if `repo` is not
    `TRUSTED_REPO_SLUG` (Issue #2241 AC4), and `InvalidIssueNumberRejected`
    if `issue_number` is not a positive integer -- both checks happen
    before any network call is attempted.
    """
    _validate_repo(repo)
    _validate_issue_number(issue_number)
    url = f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}"
    return _credentialless_get(url)


__all__ = [
    "TRUSTED_REPO_SLUG",
    "GITHUB_API_BASE",
    "CredentiallessReadError",
    "CrossRepositoryReadRejected",
    "InvalidIssueNumberRejected",
    "sanitized_env",
    "read_public_issue",
]
