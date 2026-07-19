#!/usr/bin/env python3
"""
pr_review_marker_archive_exec.py

Limited-purpose executor that archives a single merged PR's
PR_REVIEW_PUBLISH_MARKER_V1 local marker (written by
controlled_skill_mutation_exec.py's pr_review.publish command) to a repo
-external archive root, then removes the repo-local marker -- but ONLY after
independently re-verifying, against remote GitHub state, that:

  1. the marker is the exact canonical file at
     artifacts/<pr>/issue-metadata/pr_review.publish/pr_review_publish.marker.json,
     a regular file with no hardlinks, reachable only through a directory-fd
     (openat-style) walk that rejects symlinks at every path component
     (TOCTOU-safe validation -- Issue #1602 AC1);
  2. the PR is remotely MERGED (via the dedicated merged-check endpoint,
     204=merged / 404=unmerged -- never a string compare on PR state, and the
     process return code for the 204 branch is also checked);
  3. the marker's review_id is the *primary key* used to fetch the exact
     GitHub review and cross-validate id / html_url / pull_request_url /
     state / commit_id / trailing idempotency-derived body marker AND the
     body's SHA-256 (recomputed from the marker's own idempotency_key, not
     merely a marker-string presence check) AND the review author identity
     against the authenticated `gh` identity (Issue #1602 AC2; PR #1628
     review P0-4).

Only after all of the above hold does the executor:
  - write an immutable, content-addressed archive envelope to a repo-external
    state root (XDG_STATE_HOME authority) using create-once / no-overwrite
    semantics (exclusive temp file + hardlink publish), with the destination
    entry (whether newly written or pre-existing) always re-opened through a
    directory-fd (O_NOFOLLOW) and strictly schema-validated before being
    trusted (PR #1628 review P0-3);
  - fsync the archive file and its parent directory (durability);
  - remove the repo-local marker via a directory-fd-relative unlink that
    re-validates st_dev/st_ino/nlink against the still-open source fd
    immediately before unlinking;
  - fsync the source's parent directory.

The whole sequence is modeled as an explicit state machine so that failures
are classified honestly instead of claiming an unverifiable postcondition
(Issue #1602 AC3):

    SOURCE_VALIDATED -> ARCHIVE_PREPARED -> ARCHIVE_DURABLE
        -> SOURCE_REMOVED -> SOURCE_REMOVAL_DURABLE -> COMMITTED

  - Any failure strictly before ARCHIVE_DURABLE is reached: the repo-local
    marker is guaranteed untouched. status=refused (or environment_blocked
    for missing tooling), reason_code identifies the failed check. Every
    filesystem error encountered while preparing/writing the archive (ENOSPC,
    EACCES, malformed pre-existing archive, symlinked destination, directory
    fsync failure, ...) is converted to a bounded reason_code here rather
    than propagating as a raw, uncaught exception (PR #1628 review P1-2);
    the source marker's file descriptors are always closed before this
    function returns, on every exit path.
  - A failure after ARCHIVE_DURABLE but before/during the source-removal
    unlink: if the source can still be positively confirmed present at the
    SAME inode it was validated at, status=source_retained. If a DIFFERENT
    inode now occupies the canonical path (e.g. a concurrent writer recreated
    it), that is never conflated with "absent" -- it is treated the same as
    "present" for postcondition purposes (source_present_after=true) but
    reported as indeterminate, since the executor cannot make any claim about
    that other file's provenance. If presence cannot be confirmed either way
    (e.g. the confirming stat/readback itself failed), status=indeterminate
    -- the executor never claims "marker retained" without proof.
  - A failure after the source has been removed (SOURCE_REMOVED) but before
    SOURCE_REMOVAL_DURABLE / final readback completes: status=indeterminate
    (the source is gone; the executor cannot promise durability was
    observed, so it does not claim success either).
  - Full completion: status=archived. A retry against an already-archived,
    now-absent source that matches a pre-existing valid archive envelope
    returns status=already_archived (idempotent reconciliation).

Trust boundary hardening (PR #1628 review P0-2): `gh` and `git` are resolved
ONLY from a fixed, trusted absolute PATH list -- never from the ambient
(possibly attacker-influenced) process PATH -- and every subprocess is given
a sanitized environment (GH_HOST/GH_REPO/GH_CONFIG_DIR/GH_DEBUG/DEBUG/
PYTHONPATH/PYTHONHOME stripped; GH_PROMPT_DISABLED/GH_NO_UPDATE_NOTIFIER set).
The repository this executor considers "the" repository is bound to the
canonical https/ssh github.com remote configured as `origin` in the worktree
it is invoked from; an explicit --repo that disagrees with that remote is
refused rather than silently trusted.

Command line:
    uv run python3 scripts/agent-ops/pr_review_marker_archive_exec.py \
        --pr-number 1594 [--repo squne121/loop-protocol] [--dry-run] [--json]

Only post-merge-cleanup is expected to invoke this executor, and only with
an explicit merged PR number -- no path/glob user input is accepted; the
source path is entirely derived by the executor itself from --pr-number.

Issue #1602. Hardening follow-ups from PR #1628 OWNER security review
(https://github.com/squne121/loop-protocol/pull/1628#issuecomment-5014930088).
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import stat as stat_module
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------

_THIS_FILE = Path(__file__).resolve()
# scripts/agent-ops/ -> scripts/ -> project_root
DEFAULT_PROJECT_ROOT = _THIS_FILE.parent.parent.parent

RESULT_SCHEMA = "PR_REVIEW_MARKER_ARCHIVE_RESULT_V1"
MARKER_SCHEMA = "PR_REVIEW_PUBLISH_MARKER_V1"
ARCHIVE_ENVELOPE_SCHEMA = "PR_REVIEW_MARKER_ARCHIVE_ENVELOPE_V1"
ISSUE_METADATA_SEGMENT = "issue-metadata"
COMMAND_ID = "pr_review.publish"
MARKER_FILE_NAME = "pr_review_publish.marker.json"
ARCHIVE_NAMESPACE = "pr-review-marker-archive"
ARCHIVE_APP_SEGMENT = "loop-protocol"

# Marker body decoration written by controlled_skill_mutation_exec.py's
# pr_review.publish command -- must match _pr_review_marker_str() there.
PR_REVIEW_MARKER_PREFIX = "<!-- PR_REVIEW_PUBLISH_MARKER:"
PR_REVIEW_MARKER_SUFFIX = " -->"

MAX_MARKER_BYTES = 1_000_000
MAX_ARCHIVE_ENVELOPE_BYTES = 1_000_000
TRUSTED_GITHUB_HOST = "github.com"
GITHUB_API_VERSION = "2022-11-28"

# -- Trust boundary: gh/git are resolved ONLY from this fixed, trusted PATH
# list -- never from the ambient process PATH (PR #1628 review P0-2). This
# mirrors scripts/agent-guards/controlled_skill_mutation_exec.py's
# _GH_TRUSTED_PATHS.
_TRUSTED_BIN_PATH_DIRS = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Env vars stripped from every gh/git subprocess so an inherited/attacker
# -influenced parent environment cannot redirect host/config/repo, shadow
# python modules, or open an interactive editor/browser (PR #1628 P0-2).
_ENV_STRIP_KEYS = frozenset(
    {
        "GH_HOST",
        "GH_REPO",
        "GH_CONFIG_DIR",
        "GH_DEBUG",
        "DEBUG",
        "PYTHONPATH",
        "PYTHONHOME",
        "GH_EDITOR",
        "EDITOR",
        "VISUAL",
        "BROWSER",
    }
)

_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# -- Status / reason_code closed sets (AC3 / AC4) ---------------------------

STATUS_ARCHIVED = "archived"
STATUS_ALREADY_ARCHIVED = "already_archived"
STATUS_SOURCE_RETAINED = "source_retained"
STATUS_INDETERMINATE = "indeterminate"
STATUS_REFUSED = "refused"
STATUS_ENVIRONMENT_BLOCKED = "environment_blocked"

ALL_STATUSES = frozenset(
    {
        STATUS_ARCHIVED,
        STATUS_ALREADY_ARCHIVED,
        STATUS_SOURCE_RETAINED,
        STATUS_INDETERMINATE,
        STATUS_REFUSED,
        STATUS_ENVIRONMENT_BLOCKED,
    }
)

# Reason codes on ArchiveRefused that mean "the source marker simply does
# not currently exist at its canonical location" (as opposed to "it exists
# but failed validation"). These route to the idempotent-absent-source
# reconciliation branch instead of a hard refusal.
SOURCE_ABSENT_REASON_CODES = frozenset(
    {
        "marker_absent_or_symlink",
        "source_component_absent",
    }
)

# -- source presence classification (PR #1628 review P0-5): "absent" must
# NEVER be conflated with "a different file now occupies the canonical
# path". Only FileNotFoundError proves absence.
SOURCE_PRESENCE_ABSENT = "absent"
SOURCE_PRESENCE_SAME_INODE = "same_inode_present"
SOURCE_PRESENCE_DIFFERENT_INODE = "different_inode_present"
SOURCE_PRESENCE_UNKNOWN = "unknown"


class ArchiveRefused(Exception):
    """Raised for any failure strictly before ARCHIVE_DURABLE. The source
    marker is guaranteed untouched when this is raised."""

    def __init__(self, reason_code: str, message: str = "") -> None:
        super().__init__(message or reason_code)
        self.reason_code = reason_code
        self.message = message or reason_code


class EnvironmentBlocked(Exception):
    """Raised when required tooling (git/gh) is unavailable."""

    def __init__(self, reason_code: str, message: str = "") -> None:
        super().__init__(message or reason_code)
        self.reason_code = reason_code
        self.message = message or reason_code


# --------------------------------------------------------------------------
# Result envelope
# --------------------------------------------------------------------------


@dataclass
class ArchiveResult:
    status: str
    reason_code: str | None
    pr_number: int
    source_relpath: str
    marker_sha256: str | None = None
    archive_locator: str | None = None
    archive_durable: bool = False
    source_present_after: str = "unknown"  # "true" | "false" | "unknown"
    source_directory_synced: bool = False
    remote: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RESULT_SCHEMA,
            "status": self.status,
            "reason_code": self.reason_code,
            "pr_number": self.pr_number,
            "source_relpath": self.source_relpath,
            "marker_sha256": self.marker_sha256,
            "archive_locator": self.archive_locator,
            "archive_durable": self.archive_durable,
            "source_present_after": self.source_present_after,
            "source_directory_synced": self.source_directory_synced,
            "remote": self.remote,
            "errors": self.errors,
        }


# --------------------------------------------------------------------------
# dir-fd relative (openat-style) TOCTOU-safe marker validation (AC1)
# --------------------------------------------------------------------------


@dataclass
class ValidatedMarker:
    parent_dir_fd: int
    marker_name: str
    marker_fd: int
    data: dict[str, Any]
    raw_bytes: bytes
    sha256: str
    st_dev: int
    st_ino: int


def _open_dir_fd(parent_fd: int, name: str) -> int:
    """openat-style: open `name` under `parent_fd` as a directory, refusing
    symlinks (O_NOFOLLOW) so an attacker cannot redirect a path component."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise ArchiveRefused("source_component_absent", f"{name}: {exc}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ArchiveRefused(
                "marker_path_component_symlink_rejected", f"{name}: {exc}"
            ) from exc
        raise ArchiveRefused(
            "marker_path_component_missing_or_not_directory",
            f"{name}: {exc}",
        ) from exc
    st = os.fstat(fd)
    if not stat_module.S_ISDIR(st.st_mode):
        os.close(fd)
        raise ArchiveRefused("marker_path_component_not_directory", name)
    return fd


def validate_and_open_marker(project_root: Path, pr_number: int) -> ValidatedMarker:
    """Walk artifacts/<pr>/issue-metadata/pr_review.publish/ as a chain of
    directory FDs (openat-style, O_NOFOLLOW at every component), then open
    the marker file itself with O_NOFOLLOW, read its bytes through the SAME
    fd, and fstat it (regular file, nlink==1, bounded size) before returning
    the still-open fds to the caller for later re-validation at unlink time.
    """
    components = [
        "artifacts",
        str(pr_number),
        ISSUE_METADATA_SEGMENT,
        COMMAND_ID,
    ]

    root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    try:
        root_fd = os.open(str(project_root), root_flags)
    except OSError as exc:
        raise ArchiveRefused("project_root_unreachable", str(exc)) from exc

    dir_fd = root_fd
    opened_fds: list[int] = [root_fd]
    try:
        for component in components:
            dir_fd = _open_dir_fd(dir_fd, component)
            opened_fds.append(dir_fd)

        marker_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            marker_fd = os.open(MARKER_FILE_NAME, marker_flags, dir_fd=dir_fd)
        except OSError as exc:
            raise ArchiveRefused("marker_absent_or_symlink", str(exc)) from exc

        st = os.fstat(marker_fd)
        if not stat_module.S_ISREG(st.st_mode):
            os.close(marker_fd)
            raise ArchiveRefused("marker_not_regular_file", MARKER_FILE_NAME)
        if st.st_nlink != 1:
            os.close(marker_fd)
            raise ArchiveRefused("marker_hardlinked_rejected", MARKER_FILE_NAME)
        if st.st_size > MAX_MARKER_BYTES:
            os.close(marker_fd)
            raise ArchiveRefused("marker_too_large", str(st.st_size))

        with os.fdopen(os.dup(marker_fd), "rb", closefd=True) as fh:
            raw = fh.read(MAX_MARKER_BYTES + 1)
        if len(raw) > MAX_MARKER_BYTES:
            os.close(marker_fd)
            raise ArchiveRefused("marker_too_large", "read_exceeded_bound")

        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            os.close(marker_fd)
            raise ArchiveRefused("marker_invalid_json", str(exc)) from exc
        if not isinstance(data, dict):
            os.close(marker_fd)
            raise ArchiveRefused("marker_schema_not_object", "")

        digest = hashlib.sha256(raw).hexdigest()

        # Keep marker_fd + the immediate parent dir_fd open (needed for the
        # unlink-time re-check); close only the intermediate ancestor fds.
        for fd in opened_fds[:-1]:
            os.close(fd)

        return ValidatedMarker(
            parent_dir_fd=dir_fd,
            marker_name=MARKER_FILE_NAME,
            marker_fd=marker_fd,
            data=data,
            raw_bytes=raw,
            sha256=digest,
            st_dev=st.st_dev,
            st_ino=st.st_ino,
        )
    except Exception:
        for fd in opened_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


REQUIRED_MARKER_STR_FIELDS = (
    "repo",
    "idempotency_key",
    "expected_head_sha",
    "review_url",
    "published_at",
)

_PR_HEAD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Strict idempotency_key grammar: "<repo>:<pr_number>:<head_sha>:<body_sha256>"
# -- must match the producer's construction in controlled_skill_mutation_exec.py
# (_render_pr_review_publish_request). PR #1628 review P0-4: this is parsed
# and cross-validated (not merely treated as an opaque string used only to
# derive the trailing marker hash) so a rewritten review body that keeps only
# the trailing marker literal can no longer be archived as "identity proven".
_IDEMPOTENCY_KEY_RE = re.compile(
    r"^(?P<repo>[^:]+):(?P<pr_number>\d+):(?P<head_sha>[0-9a-f]{40}):(?P<body_sha256>[0-9a-f]{64})$"
)


def _parse_idempotency_key(idempotency_key: str) -> dict[str, str]:
    m = _IDEMPOTENCY_KEY_RE.match(idempotency_key)
    if not m:
        raise ArchiveRefused("marker_idempotency_key_malformed", idempotency_key)
    return m.groupdict()


def _is_iso8601(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_marker_schema(data: dict[str, Any], pr_number: int, repo: str) -> None:
    if data.get("schema") != MARKER_SCHEMA:
        raise ArchiveRefused("marker_schema_mismatch", str(data.get("schema")))
    if not isinstance(data.get("pr_number"), int):
        raise ArchiveRefused("marker_pr_number_not_int", "")
    if data["pr_number"] != pr_number:
        raise ArchiveRefused("marker_pr_number_mismatch", str(data["pr_number"]))
    if data.get("repo") != repo:
        raise ArchiveRefused("marker_repo_mismatch", str(data.get("repo")))
    for name in REQUIRED_MARKER_STR_FIELDS:
        if not isinstance(data.get(name), str) or not data.get(name):
            raise ArchiveRefused(f"marker_field_missing_{name}", "")

    review_id = data.get("review_id")
    try:
        review_id_int = int(review_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ArchiveRefused("marker_review_id_not_integer", str(review_id)) from None
    if review_id_int <= 0:
        raise ArchiveRefused("marker_review_id_not_positive", str(review_id))

    if not _PR_HEAD_SHA_RE.match(data["expected_head_sha"]):
        raise ArchiveRefused("marker_expected_head_sha_invalid", data["expected_head_sha"])

    if not _is_iso8601(data.get("published_at")):
        raise ArchiveRefused("marker_published_at_invalid", str(data.get("published_at")))

    # PR #1628 review P0-4: idempotency_key is parsed and structurally bound
    # to repo/pr_number/expected_head_sha -- an idempotency_key that does not
    # match the marker's own declared fields is refused here, before any
    # remote call is made.
    parsed_key = _parse_idempotency_key(data["idempotency_key"])
    if parsed_key["repo"] != repo:
        raise ArchiveRefused("marker_idempotency_key_repo_mismatch", parsed_key["repo"])
    if parsed_key["pr_number"] != str(pr_number):
        raise ArchiveRefused(
            "marker_idempotency_key_pr_number_mismatch", parsed_key["pr_number"]
        )
    if parsed_key["head_sha"] != data["expected_head_sha"]:
        raise ArchiveRefused(
            "marker_idempotency_key_head_sha_mismatch", parsed_key["head_sha"]
        )


# --------------------------------------------------------------------------
# Trusted binary resolution + sanitized subprocess environment (P0-2)
# --------------------------------------------------------------------------


def _resolve_trusted_bin(name: str, trusted_path_dirs: str = _TRUSTED_BIN_PATH_DIRS) -> str | None:
    """Resolve `name` ONLY from `trusted_path_dirs`, never from the ambient
    process PATH. This defeats a PATH-prepended fake `gh`/`git` regardless of
    what the caller's environment looks like."""
    return shutil.which(name, path=trusted_path_dirs)


def _build_sanitized_subprocess_env() -> dict[str, str]:
    """Sanitized environment for every gh/git subprocess call. Built fresh
    (not memoized) so each call gets an independent copy."""
    env = os.environ.copy()
    for key in _ENV_STRIP_KEYS:
        env.pop(key, None)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    return env


def _normalize_owner_repo(path: str) -> str | None:
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not path or not _OWNER_REPO_RE.match(path):
        return None
    return path


def _parse_trusted_github_remote(url: str) -> str | None:
    """Return the normalized ``owner/repo`` iff url is a canonical HTTPS/SSH
    github.com remote. Returns None for any other host, scheme, port, or
    non-``git``/anonymous userinfo (evil host, file://, other-host SSH,
    etc.). Mirrors controlled_skill_mutation_exec.py's
    _parse_trusted_github_remote (Issue #1539 fix_delta Blocker 2)."""
    url = (url or "").strip()
    if not url or "\x00" in url:
        return None
    if "://" in url:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return None
        if parsed.scheme.lower() not in ("https", "ssh"):
            return None
        host = (parsed.hostname or "").lower()
        if host != TRUSTED_GITHUB_HOST:
            return None
        if parsed.port not in (None, 443, 22):
            return None
        if parsed.username not in (None, "git"):
            return None
        return _normalize_owner_repo(parsed.path)
    m = re.match(r"^(?:([A-Za-z0-9_.-]+)@)?([A-Za-z0-9_.-]+):(.+)$", url)
    if not m:
        return None
    user, host, path = m.group(1), m.group(2), m.group(3)
    if user not in (None, "git"):
        return None
    if host.lower() != TRUSTED_GITHUB_HOST:
        return None
    return _normalize_owner_repo(path)


# --------------------------------------------------------------------------
# Untracked / primary-worktree / default-branch / trusted-origin preconditions
# --------------------------------------------------------------------------


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    git_bin = _resolve_trusted_bin("git")
    if git_bin is None:
        raise EnvironmentBlocked(
            "environment_blocked_missing_git", "git not found in trusted path"
        )
    return subprocess.run(
        [git_bin, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=15,
        env=_build_sanitized_subprocess_env(),
    )


def ensure_source_untracked(project_root: Path, relpath: str) -> None:
    try:
        proc = run_git(["ls-files", "--error-unmatch", relpath], cwd=project_root)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise EnvironmentBlocked("environment_blocked_git_invocation_failed", str(exc)) from exc
    if proc.returncode == 0:
        raise ArchiveRefused("git_tracked_file_conflict", relpath)


def ensure_primary_default_worktree(project_root: Path) -> None:
    try:
        toplevel = run_git(["rev-parse", "--show-toplevel"], cwd=project_root)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise EnvironmentBlocked("environment_blocked_git_invocation_failed", str(exc)) from exc
    if toplevel.returncode != 0:
        raise ArchiveRefused("not_a_git_worktree", toplevel.stderr.strip())
    if os.path.realpath(toplevel.stdout.strip()) != os.path.realpath(str(project_root)):
        raise ArchiveRefused("source_repo_root_mismatch", toplevel.stdout.strip())


def ensure_trusted_repo_origin(project_root: Path, repo: str) -> None:
    """Bind `repo` (whether explicit --repo or origin-derived) to the
    worktree's actual `origin` remote: it must be a canonical https/ssh
    github.com remote for exactly `repo`. Defends against a confused-deputy
    caller that supplies a --repo disagreeing with the real remote (PR #1628
    review P0-2)."""
    try:
        proc = run_git(["remote", "get-url", "origin"], cwd=project_root)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise EnvironmentBlocked("environment_blocked_git_invocation_failed", str(exc)) from exc
    if proc.returncode != 0:
        raise ArchiveRefused("repo_origin_unresolvable", proc.stderr.strip())
    normalized = _parse_trusted_github_remote(proc.stdout.strip())
    if normalized is None:
        raise ArchiveRefused(
            "repo_origin_untrusted_host_or_scheme", proc.stdout.strip()
        )
    if normalized != repo:
        raise ArchiveRefused(
            "repo_origin_binding_mismatch", f"{normalized!r} != {repo!r}"
        )


# --------------------------------------------------------------------------
# GitHub remote readback (AC2)
# --------------------------------------------------------------------------

GhCaller = Callable[[list[str]], tuple[int, str, str]]

_STATUS_LINE_RE = re.compile(r"^HTTP/\d(?:\.\d)?\s+(\d{3})\b")


def _parse_http_status_line(status_line: str) -> int | None:
    m = _STATUS_LINE_RE.match(status_line.strip())
    if not m:
        return None
    return int(m.group(1))


def default_gh_caller(argv: list[str]) -> tuple[int, str, str]:
    gh_bin = _resolve_trusted_bin("gh")
    if gh_bin is None:
        raise EnvironmentBlocked(
            "environment_blocked_missing_gh", "gh not found in trusted path"
        )
    try:
        proc = subprocess.run(
            [gh_bin, *argv],
            capture_output=True,
            text=True,
            timeout=20,
            env=_build_sanitized_subprocess_env(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise EnvironmentBlocked("environment_blocked_gh_invocation_failed", str(exc)) from exc
    return proc.returncode, proc.stdout, proc.stderr


def _gh_api_headers() -> list[str]:
    return [
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
    ]


def remote_check_merged(repo: str, pr_number: int, gh_caller: GhCaller) -> bool:
    """Uses the dedicated merged-check endpoint: 204=merged, 404=unmerged.
    Never a string compare on PR .state. The 204 branch additionally
    requires rc==0 (a nonzero gh return code alongside a 204-shaped status
    line is refused, not silently trusted -- PR #1628 review P2)."""
    rc, out, err = gh_caller(
        [
            "api",
            "--hostname",
            TRUSTED_GITHUB_HOST,
            "-i",
            *_gh_api_headers(),
            f"repos/{repo}/pulls/{pr_number}/merge",
        ]
    )
    status_line = (out.splitlines() or [""])[0]
    status = _parse_http_status_line(status_line)
    if status == 204:
        if rc != 0:
            raise ArchiveRefused(
                "remote_merge_check_204_with_nonzero_rc", f"rc={rc}"
            )
        return True
    if status == 404:
        return False
    raise ArchiveRefused(
        "remote_merge_check_unexpected_response",
        f"rc={rc} status_line={status_line!r} err={err.strip()[:200]}",
    )


def remote_fetch_review(repo: str, pr_number: int, review_id: str | int, gh_caller: GhCaller) -> dict[str, Any]:
    rc, out, err = gh_caller(
        [
            "api",
            "--hostname",
            TRUSTED_GITHUB_HOST,
            *_gh_api_headers(),
            f"repos/{repo}/pulls/{pr_number}/reviews/{review_id}",
        ]
    )
    if rc != 0:
        raise ArchiveRefused("remote_review_fetch_failed", err.strip()[:200])
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ArchiveRefused("remote_review_response_invalid_json", str(exc)) from exc
    if not isinstance(parsed, dict):
        raise ArchiveRefused("remote_review_response_not_object", "")
    return parsed


def remote_fetch_authenticated_login(gh_caller: GhCaller) -> str:
    """The identity this process is actually authenticated as. Used as a
    postcondition identity binding (PR #1628 review P0-4): the review being
    archived must have been authored by THIS identity, not merely contain a
    marker string that matches -- otherwise a rewritten/reposted review body
    with the same trailing marker literal could be archived as if it were
    identity-proven."""
    rc, out, err = gh_caller(["api", "--hostname", TRUSTED_GITHUB_HOST, *_gh_api_headers(), "user"])
    if rc != 0:
        raise ArchiveRefused("remote_authenticated_user_fetch_failed", err.strip()[:200])
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ArchiveRefused("remote_authenticated_user_response_invalid_json", str(exc)) from exc
    login = parsed.get("login") if isinstance(parsed, dict) else None
    if not isinstance(login, str) or not login:
        raise ArchiveRefused("remote_authenticated_user_login_missing", "")
    return login


def _pr_review_marker_str(idempotency_key: str) -> str:
    marker_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    return f"{PR_REVIEW_MARKER_PREFIX}{marker_hash}{PR_REVIEW_MARKER_SUFFIX}"


def validate_remote_binding(
    marker: dict[str, Any],
    review: dict[str, Any],
    repo: str,
    pr_number: int,
    gh_caller: GhCaller,
) -> None:
    expected_review_id = marker["review_id"]
    actual_id = review.get("id")
    if str(actual_id) != str(expected_review_id):
        raise ArchiveRefused("remote_review_id_mismatch", str(actual_id))
    if review.get("html_url") != marker["review_url"]:
        raise ArchiveRefused("remote_review_url_mismatch", str(review.get("html_url")))
    expected_pr_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    if review.get("pull_request_url") != expected_pr_url:
        raise ArchiveRefused(
            "remote_review_pull_request_url_mismatch", str(review.get("pull_request_url"))
        )
    if review.get("state") != "COMMENTED":
        raise ArchiveRefused("remote_review_state_mismatch", str(review.get("state")))
    if review.get("commit_id") != marker["expected_head_sha"]:
        raise ArchiveRefused("remote_review_commit_mismatch", str(review.get("commit_id")))
    submitted_at = review.get("submitted_at")
    if not _is_iso8601(submitted_at):
        raise ArchiveRefused("remote_review_submitted_at_missing_or_invalid", str(submitted_at))
    body = review.get("body")
    if not isinstance(body, str):
        raise ArchiveRefused("remote_review_body_missing", "")
    expected_marker_str = _pr_review_marker_str(marker["idempotency_key"])
    if body.count(expected_marker_str) != 1:
        raise ArchiveRefused("remote_review_body_marker_mismatch", "")
    if not body.rstrip("\n").endswith(expected_marker_str):
        raise ArchiveRefused("remote_review_body_marker_not_trailing", "")

    # PR #1628 review P0-4: recompute the body SHA-256 from the SAME
    # separator rule the producer uses (raw_body + "\n\n" + marker_str) and
    # cross-check it against the idempotency_key's own body_sha256 component.
    # A body that has been rewritten while keeping only the trailing marker
    # literal now fails here instead of being archived as "identity proven".
    marker_idx = body.rfind(expected_marker_str)
    pre_marker = body[:marker_idx]
    stripped_body = pre_marker[: -2] if pre_marker.endswith("\n\n") else pre_marker
    recomputed_body_sha256 = hashlib.sha256(stripped_body.encode("utf-8")).hexdigest()
    parsed_key = _parse_idempotency_key(marker["idempotency_key"])
    if recomputed_body_sha256 != parsed_key["body_sha256"]:
        raise ArchiveRefused("remote_review_body_sha256_mismatch", "")

    # PR #1628 review P0-4: review author identity must match the identity
    # this process is actually authenticated as.
    authenticated_login = remote_fetch_authenticated_login(gh_caller)
    review_author_login = (review.get("user") or {}).get("login")
    if review_author_login != authenticated_login:
        raise ArchiveRefused(
            "remote_review_author_identity_mismatch", str(review_author_login)
        )


# --------------------------------------------------------------------------
# Archive root resolution (XDG_STATE_HOME authority, #1602 self-contained
# equivalent of the #1546 external-state-root contract)
# --------------------------------------------------------------------------


def _nearest_existing_ancestor(path: Path) -> Path:
    """Walk up from `path` until an existing (or symlink) entry is found.
    This is the trust boundary for ownership checks: everything ABOVE this
    ancestor is a pre-existing system directory (e.g. `/`, `/home`) that this
    executor did not create and has no reason to own; everything AT or BELOW
    it is created and owned by this executor (PR #1628 review P0-1)."""
    current = path
    while True:
        if os.path.lexists(current):
            return current
        parent = current.parent
        if parent == current:
            return current
        current = parent


def _ensure_private_dir(path: Path) -> None:
    """Create `path` (and any missing ancestors below the nearest existing
    ancestor) with mode 0700, refusing symlinks and ownership/permission
    drift from that boundary downward. Ownership of pre-existing ancestors
    ABOVE the boundary (e.g. `/`, `/home` on a typical multi-user Linux/WSL
    system, which are root-owned) is deliberately NOT required -- only the
    boundary directory itself (the deepest pre-existing directory, e.g.
    `$HOME` or `$HOME/.local/state`) and everything this executor creates
    below it must be owned by the current user (PR #1628 review P0-1)."""
    boundary = _nearest_existing_ancestor(path)
    if os.path.islink(boundary):
        raise ArchiveRefused("archive_root_ancestor_is_symlink", str(boundary))
    boundary_st = boundary.lstat()
    if not stat_module.S_ISDIR(boundary_st.st_mode):
        raise ArchiveRefused("archive_root_ancestor_not_directory", str(boundary))
    if boundary_st.st_uid != os.getuid():
        raise ArchiveRefused("archive_root_ancestor_owner_mismatch", str(boundary))
    boundary_mode = stat_module.S_IMODE(boundary_st.st_mode)
    if boundary == path:
        # The full target directory already exists AS the boundary itself
        # (nothing left to create) -- it must satisfy the same private-mode
        # rule as any leaf we would have created ourselves, not merely the
        # looser "not group/world writable" rule applied to pre-existing
        # ancestors above the boundary.
        if boundary_mode & 0o077:
            raise ArchiveRefused("archive_root_not_private_mode", str(boundary))
    elif boundary_mode & 0o022:
        # group/world-WRITABLE boundary directory -- refuse rather than trust
        # a shared-write directory as the private-state trust anchor.
        raise ArchiveRefused("archive_root_ancestor_writable_by_others", str(boundary))

    current = boundary
    if path == boundary:
        relative_parts: tuple[str, ...] = ()
    else:
        relative_parts = path.relative_to(boundary).parts
    for part in relative_parts:
        current = current / part
        if os.path.islink(current):
            raise ArchiveRefused("archive_root_ancestor_is_symlink", str(current))
        if not current.exists():
            try:
                os.mkdir(current, mode=0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ArchiveRefused("archive_root_mkdir_failed", str(exc)) from exc
        st = current.lstat()
        if not stat_module.S_ISDIR(st.st_mode):
            raise ArchiveRefused("archive_root_ancestor_not_directory", str(current))
        if st.st_uid != os.getuid():
            raise ArchiveRefused("archive_root_ancestor_owner_mismatch", str(current))
        if current == path:
            mode_bits = stat_module.S_IMODE(st.st_mode)
            if mode_bits & 0o077:
                # group/world-accessible leaf -- refuse rather than silently
                # tighten (avoid surprising a pre-existing shared directory).
                raise ArchiveRefused("archive_root_not_private_mode", str(current))


def resolve_archive_root(project_root: Path, env: dict[str, str] | None = None) -> Path:
    env = env if env is not None else os.environ
    state_home = env.get("XDG_STATE_HOME", "").strip()
    if not state_home:
        home = env.get("HOME", "").strip()
        if not home:
            raise EnvironmentBlocked("environment_blocked_missing_home", "HOME unresolved")
        state_home = os.path.join(home, ".local", "state")

    if not os.path.isabs(state_home):
        raise ArchiveRefused("archive_root_locator_not_absolute", state_home)
    if os.path.islink(state_home):
        raise ArchiveRefused("archive_root_locator_is_symlink", state_home)

    root = Path(state_home) / ARCHIVE_APP_SEGMENT / ARCHIVE_NAMESPACE
    _ensure_private_dir(root)

    # PR #1628 review P0-1: the resolved archive root must never end up
    # inside the repository itself -- otherwise "archive then remove the
    # repo-local marker" degenerates into "move the marker to a different
    # repo-local path", defeating the whole point of an external archive.
    resolved_root = Path(os.path.realpath(str(root)))
    resolved_repo = Path(os.path.realpath(str(project_root)))
    if resolved_root == resolved_repo or _is_relative_to(resolved_root, resolved_repo):
        raise ArchiveRefused("archive_root_inside_repository", str(resolved_root))

    return root


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
    except ValueError:
        return False
    return True


def archive_locator_relpath(repo: str, pr_number: int, marker_sha256: str) -> str:
    repo_segment = repo.replace("/", "_")
    return os.path.join(repo_segment, str(pr_number), f"{marker_sha256}.archive.json")


def build_archive_envelope(
    repo: str,
    pr_number: int,
    validated: ValidatedMarker,
    marker_data: dict[str, Any],
    review: dict[str, Any],
    merged: bool,
) -> dict[str, Any]:
    return {
        "schema": ARCHIVE_ENVELOPE_SCHEMA,
        "repo": repo,
        "pr_number": pr_number,
        "source_relpath": _source_relpath(pr_number),
        "marker_sha256": f"sha256:{validated.sha256}",
        "marker_bytes_base64": None,  # deliberately omitted -- hash identity is sufficient
        "source_stat": {
            "st_dev": validated.st_dev,
            "st_ino": validated.st_ino,
        },
        "review": {
            "id": review.get("id"),
            "url": review.get("html_url"),
            "state": review.get("state"),
            "submitted_at": review.get("submitted_at"),
            "commit_id": review.get("commit_id"),
        },
        "expected_head_sha": marker_data.get("expected_head_sha"),
        "idempotency_key": marker_data.get("idempotency_key"),
        "remote_merged_verification_method": "merge_check_endpoint_204_404",
        "merged": merged,
        "archived_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "executor_version": "1",
    }


_REQUIRED_ENVELOPE_STR_FIELDS = (
    "repo",
    "source_relpath",
    "marker_sha256",
    "expected_head_sha",
    "idempotency_key",
    "archived_at",
    "executor_version",
)


def validate_archive_envelope_schema(data: dict[str, Any]) -> None:
    """Strict validation of a (pre-existing or freshly-read-back) archive
    envelope (PR #1628 review P0-3): a forged/malformed pre-existing archive
    entry must never be trusted merely because a `marker_sha256` field is
    present at the expected content-addressed path."""
    if not isinstance(data, dict):
        raise ArchiveRefused("existing_archive_schema_not_object", "")
    if data.get("schema") != ARCHIVE_ENVELOPE_SCHEMA:
        raise ArchiveRefused("existing_archive_schema_mismatch", str(data.get("schema")))
    if not isinstance(data.get("pr_number"), int):
        raise ArchiveRefused("existing_archive_schema_pr_number_invalid", "")
    for name in _REQUIRED_ENVELOPE_STR_FIELDS:
        if not isinstance(data.get(name), str) or not data.get(name):
            raise ArchiveRefused(f"existing_archive_schema_missing_{name}", "")
    if not str(data["marker_sha256"]).startswith("sha256:"):
        raise ArchiveRefused("existing_archive_schema_marker_sha256_malformed", "")
    if not isinstance(data.get("merged"), bool):
        raise ArchiveRefused("existing_archive_schema_merged_not_bool", "")
    review = data.get("review")
    if not isinstance(review, dict):
        raise ArchiveRefused("existing_archive_schema_review_not_object", "")


def _classify_write_oserror(exc: OSError) -> str:
    if exc.errno == errno.ENOSPC:
        return "archive_write_no_space"
    if exc.errno in (errno.EACCES, errno.EPERM):
        return "archive_write_permission_denied"
    if exc.errno == errno.EROFS:
        return "archive_write_read_only_filesystem"
    if exc.errno == errno.ELOOP:
        return "archive_write_symlink_rejected"
    return f"archive_write_failed_errno_{exc.errno}"


def write_archive_no_overwrite(
    archive_root: Path, locator_rel: str, envelope: dict[str, Any]
) -> tuple[bool, Path, dict[str, Any] | None]:
    """Publish `envelope` at archive_root/locator_rel using create-once /
    no-overwrite semantics: write to an exclusive temp file (dir-fd
    relative), fsync it, then publish via linkat() (dir-fd relative, which
    fails with EEXIST if the destination already exists -- there is no
    window where an existing archive can be silently replaced), fsync the
    parent directory, then remove the temp name.

    A pre-existing destination (whether found up front or raced into via
    EEXIST from a concurrent writer) is ALWAYS re-opened through the parent
    directory fd with O_NOFOLLOW and strictly validated -- regular file,
    owned by this uid, mode 0600, nlink==1, and schema-valid JSON -- before
    being trusted (PR #1628 review P0-3). Returns
    (already_existed, final_path, existing_envelope_or_None)."""
    final_path = archive_root / locator_rel
    _ensure_private_dir(final_path.parent)

    parent_dir_fd = os.open(
        str(final_path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    )
    try:
        dest_name = os.path.basename(locator_rel)

        existing = _try_read_validated_existing_archive(parent_dir_fd, dest_name)
        if existing is not None:
            return True, final_path, existing

        tmp_name = f".{dest_name}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
        fd = os.open(
            tmp_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_dir_fd,
        )
        try:
            with os.fdopen(fd, "w", closefd=True) as fh:
                fh.write(json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True))
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(
                    tmp_name,
                    dest_name,
                    src_dir_fd=parent_dir_fd,
                    dst_dir_fd=parent_dir_fd,
                )
            except FileExistsError:
                # Another invocation published concurrently (durability
                # barrier -- PR #1628 review required test #5): fsync the
                # directory first so we observe the winner's durable bytes,
                # then re-open + strictly validate it exactly like any other
                # pre-existing destination.
                os.fsync(parent_dir_fd)
                concurrent_existing = _try_read_validated_existing_archive(
                    parent_dir_fd, dest_name
                )
                if concurrent_existing is None:
                    raise ArchiveRefused(
                        "archive_concurrent_publish_unreadable", dest_name
                    )
                return True, final_path, concurrent_existing
            finally:
                try:
                    os.unlink(tmp_name, dir_fd=parent_dir_fd)
                except OSError:
                    pass
        except Exception:
            try:
                os.unlink(tmp_name, dir_fd=parent_dir_fd)
            except OSError:
                pass
            raise

        os.fsync(parent_dir_fd)
        return False, final_path, None
    finally:
        os.close(parent_dir_fd)


def _try_read_validated_existing_archive(
    parent_dir_fd: int, name: str
) -> dict[str, Any] | None:
    """Return the strictly-validated envelope at `name` under
    `parent_dir_fd`, or None if no entry exists there. Raises ArchiveRefused
    for anything present but invalid (symlink, non-regular, wrong
    owner/mode/nlink, malformed/forged JSON) -- a bad pre-existing entry is
    never silently treated as absent (PR #1628 review P0-3)."""
    # O_NONBLOCK: a pre-existing destination could be a FIFO (named pipe);
    # opening a FIFO for O_RDONLY without O_NONBLOCK blocks the whole
    # process until a writer opens it, which would hang this executor
    # indefinitely on a maliciously-planted FIFO. O_NONBLOCK makes the open
    # itself non-blocking; the immediately-following fstat() still correctly
    # identifies (and rejects) the FIFO as non-regular.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        fd = os.open(name, flags, dir_fd=parent_dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ArchiveRefused("existing_archive_symlink_rejected", name) from exc
        raise ArchiveRefused("existing_archive_open_failed", f"{name}: {exc}") from exc

    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            raise ArchiveRefused("existing_archive_not_regular_file", name)
        if st.st_uid != os.getuid():
            raise ArchiveRefused("existing_archive_owner_mismatch", name)
        if stat_module.S_IMODE(st.st_mode) != 0o600:
            raise ArchiveRefused("existing_archive_mode_mismatch", name)
        if st.st_nlink != 1:
            raise ArchiveRefused("existing_archive_hardlinked_rejected", name)
        if st.st_size > MAX_ARCHIVE_ENVELOPE_BYTES:
            raise ArchiveRefused("existing_archive_too_large", name)
        with os.fdopen(os.dup(fd), "rb", closefd=True) as fh:
            raw = fh.read(MAX_ARCHIVE_ENVELOPE_BYTES + 1)
        if len(raw) > MAX_ARCHIVE_ENVELOPE_BYTES:
            raise ArchiveRefused("existing_archive_too_large", "read_exceeded_bound")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArchiveRefused("existing_archive_invalid_json", str(exc)) from exc
        validate_archive_envelope_schema(data)
        return data
    finally:
        os.close(fd)


def _existing_matching_archive(archive_root: Path, repo: str, pr_number: int) -> dict[str, Any] | None:
    """Best-effort idempotency lookup when the source marker is already
    absent: if exactly one valid archive envelope exists for this PR, treat
    a retry as already_archived; otherwise the executor cannot determine
    which prior transaction (if any) produced the missing source."""
    pr_dir = archive_root / repo.replace("/", "_") / str(pr_number)
    if not pr_dir.is_dir():
        return None
    candidates = sorted(p for p in pr_dir.glob("*.archive.json") if p.is_file())
    if len(candidates) != 1:
        return None
    dir_fd = os.open(str(pr_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        data = _try_read_validated_existing_archive(dir_fd, candidates[0].name)
    except ArchiveRefused:
        return None
    finally:
        os.close(dir_fd)
    if data is None:
        return None
    if data.get("repo") != repo or data.get("pr_number") != pr_number:
        return None
    return data


# --------------------------------------------------------------------------
# Source removal (unlinkat with immediate pre-unlink re-check)
# --------------------------------------------------------------------------


def remove_source_with_recheck(validated: ValidatedMarker) -> None:
    """Re-validate the directory entry against the already-open fd
    (st_dev/st_ino/nlink) immediately before unlinking, using unlinkat
    (dir_fd-relative) so no pathname race window exists between the check
    and the removal."""
    st = os.stat(
        validated.marker_name, dir_fd=validated.parent_dir_fd, follow_symlinks=False
    )
    if st.st_dev != validated.st_dev or st.st_ino != validated.st_ino:
        raise ArchiveRefused("source_inode_swapped_before_unlink", "")
    if st.st_nlink != 1:
        raise ArchiveRefused("source_hardlinked_before_unlink", "")
    os.unlink(validated.marker_name, dir_fd=validated.parent_dir_fd)


def fsync_parent_dir(validated: ValidatedMarker) -> None:
    os.fsync(validated.parent_dir_fd)


def source_still_present(validated: ValidatedMarker) -> str:
    """Returns one of SOURCE_PRESENCE_ABSENT / SOURCE_PRESENCE_SAME_INODE /
    SOURCE_PRESENCE_DIFFERENT_INODE / SOURCE_PRESENCE_UNKNOWN. ONLY a
    FileNotFoundError proves absence (PR #1628 review P0-5) -- a different
    inode now occupying the canonical path is never conflated with
    "path does not exist"."""
    try:
        st = os.stat(
            validated.marker_name, dir_fd=validated.parent_dir_fd, follow_symlinks=False
        )
    except FileNotFoundError:
        return SOURCE_PRESENCE_ABSENT
    except OSError:
        return SOURCE_PRESENCE_UNKNOWN
    if st.st_dev == validated.st_dev and st.st_ino == validated.st_ino:
        return SOURCE_PRESENCE_SAME_INODE
    return SOURCE_PRESENCE_DIFFERENT_INODE


def _source_relpath(pr_number: int) -> str:
    return os.path.join(
        "artifacts", str(pr_number), ISSUE_METADATA_SEGMENT, COMMAND_ID, MARKER_FILE_NAME
    )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def resolve_repo(explicit_repo: str | None, project_root: Path) -> str:
    try:
        proc = run_git(["remote", "get-url", "origin"], cwd=project_root)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise EnvironmentBlocked("environment_blocked_git_invocation_failed", str(exc)) from exc
    if proc.returncode != 0:
        raise ArchiveRefused("repo_unresolvable", proc.stderr.strip())
    normalized = _parse_trusted_github_remote(proc.stdout.strip())
    if normalized is None:
        raise ArchiveRefused(
            "repo_unresolvable_untrusted_host_or_scheme", proc.stdout.strip()
        )
    if explicit_repo and explicit_repo != normalized:
        raise ArchiveRefused(
            "repo_explicit_origin_mismatch", f"{explicit_repo!r} != {normalized!r}"
        )
    return normalized


def run_archive(
    pr_number: int,
    repo: str,
    project_root: Path,
    gh_caller: GhCaller,
    archive_root_override: Path | None = None,
) -> ArchiveResult:
    """Top-level entry point. Never raises ArchiveRefused/EnvironmentBlocked
    -- all such failures are converted into a terminal ArchiveResult."""
    source_relpath = _source_relpath(pr_number)
    try:
        return _run_archive_inner(pr_number, repo, project_root, gh_caller, archive_root_override)
    except EnvironmentBlocked as exc:
        return ArchiveResult(
            status=STATUS_ENVIRONMENT_BLOCKED,
            reason_code=exc.reason_code,
            pr_number=pr_number,
            source_relpath=source_relpath,
            errors=[exc.message],
        )
    except ArchiveRefused as exc:
        return ArchiveResult(
            status=STATUS_REFUSED,
            reason_code=exc.reason_code,
            pr_number=pr_number,
            source_relpath=source_relpath,
            errors=[exc.message],
        )


def _run_archive_inner(
    pr_number: int,
    repo: str,
    project_root: Path,
    gh_caller: GhCaller,
    archive_root_override: Path | None,
) -> ArchiveResult:
    source_relpath = _source_relpath(pr_number)
    remote_summary: dict[str, Any] = {}

    if pr_number <= 0:
        raise ArchiveRefused("invalid_pr_number", str(pr_number))

    ensure_primary_default_worktree(project_root)
    ensure_trusted_repo_origin(project_root, repo)

    try:
        validated = validate_and_open_marker(project_root, pr_number)
    except ArchiveRefused as exc:
        if exc.reason_code in SOURCE_ABSENT_REASON_CODES:
            # -- Idempotent reconciliation: source already gone. Look for a
            # single pre-existing valid archive envelope for this PR.
            archive_root = archive_root_override or resolve_archive_root(project_root)
            match = _existing_matching_archive(archive_root, repo, pr_number)
            if match is not None:
                marker_sha = str(match.get("marker_sha256", "")).removeprefix("sha256:")
                return ArchiveResult(
                    status=STATUS_ALREADY_ARCHIVED,
                    reason_code=None,
                    pr_number=pr_number,
                    source_relpath=source_relpath,
                    marker_sha256=match.get("marker_sha256"),
                    archive_locator=archive_locator_relpath(repo, pr_number, marker_sha),
                    archive_durable=True,
                    source_present_after="false",
                    source_directory_synced=True,
                    remote={},
                )
            return ArchiveResult(
                status=STATUS_REFUSED,
                reason_code="indeterminate_source_missing",
                pr_number=pr_number,
                source_relpath=source_relpath,
                source_present_after="false",
            )
        raise

    # -- From here on, `validated.marker_fd` / `validated.parent_dir_fd` are
    # open and MUST be closed on every exit path (PR #1628 review P1-2).
    with ExitStack() as fd_guard:
        fds_closed = {"value": False}

        def _close_validated_fds() -> None:
            if fds_closed["value"]:
                return
            fds_closed["value"] = True
            try:
                os.close(validated.marker_fd)
            except OSError:
                pass
            try:
                os.close(validated.parent_dir_fd)
            except OSError:
                pass

        fd_guard.callback(_close_validated_fds)

        try:
            ensure_source_untracked(project_root, source_relpath)
            validate_marker_schema(validated.data, pr_number, repo)

            merged = remote_check_merged(repo, pr_number, gh_caller)
            remote_summary["merged"] = merged
            if not merged:
                raise ArchiveRefused("remote_pr_not_merged", "")

            review = remote_fetch_review(repo, pr_number, validated.data["review_id"], gh_caller)
            validate_remote_binding(validated.data, review, repo, pr_number, gh_caller)
            remote_summary.update(
                {
                    "review_id": review.get("id"),
                    "state": review.get("state"),
                    "commit_id": review.get("commit_id"),
                }
            )
        except (ArchiveRefused, EnvironmentBlocked):
            # Strictly before ARCHIVE_DURABLE: source untouched, fds closed
            # by the ExitStack callback, exception propagates to run_archive.
            raise

        # -- SOURCE_VALIDATED reached with all remote checks green. From here
        # on, any failure is classified relative to ARCHIVE_DURABLE / the
        # source-removal boundary rather than failed_before_archive.
        try:
            archive_root = archive_root_override or resolve_archive_root(project_root)
            envelope = build_archive_envelope(repo, pr_number, validated, validated.data, review, merged)
            locator_rel = archive_locator_relpath(repo, pr_number, validated.sha256)

            already_existed, archive_path, existing_envelope = write_archive_no_overwrite(
                archive_root, locator_rel, envelope
            )
            if already_existed:
                existing = existing_envelope if existing_envelope is not None else {}
                existing_hash = str(existing.get("marker_sha256", "")).removeprefix("sha256:")
                if existing_hash != validated.sha256:
                    raise ArchiveRefused("archive_collision_hash_mismatch", "")
        except ArchiveRefused:
            # Strictly before ARCHIVE_DURABLE: source untouched.
            raise
        except OSError as exc:
            # PR #1628 review P1-2: any filesystem error while preparing the
            # archive (ENOSPC, EACCES, EROFS, ...) is a bounded refusal, not
            # a crash -- and, critically, is still strictly before
            # ARCHIVE_DURABLE, so the source marker is untouched.
            raise ArchiveRefused(_classify_write_oserror(exc), str(exc)) from exc

        archive_durable = True  # ARCHIVE_DURABLE reached (fsync'd in the writer)

        # -- ARCHIVE_DURABLE reached. Attempt source removal; classify any
        # failure from here on as source_retained / indeterminate, never as a
        # plain refusal (the marker's fate is no longer simply "untouched").
        try:
            remove_source_with_recheck(validated)
        except Exception as exc:  # noqa: BLE001 - must classify, not propagate
            presence = source_still_present(validated)
            _close_validated_fds()
            if presence == SOURCE_PRESENCE_SAME_INODE:
                return ArchiveResult(
                    status=STATUS_SOURCE_RETAINED,
                    reason_code="source_unlink_failed_source_confirmed_present",
                    pr_number=pr_number,
                    source_relpath=source_relpath,
                    marker_sha256=f"sha256:{validated.sha256}",
                    archive_locator=locator_rel,
                    archive_durable=archive_durable,
                    source_present_after="true",
                    source_directory_synced=False,
                    remote=remote_summary,
                    errors=[str(exc)],
                )
            if presence == SOURCE_PRESENCE_DIFFERENT_INODE:
                # A different file now occupies the canonical path. This is
                # NEVER "absent" -- report present-but-indeterminate rather
                # than silently claiming the original marker's fate.
                return ArchiveResult(
                    status=STATUS_INDETERMINATE,
                    reason_code="source_unlink_failed_different_inode_present",
                    pr_number=pr_number,
                    source_relpath=source_relpath,
                    marker_sha256=f"sha256:{validated.sha256}",
                    archive_locator=locator_rel,
                    archive_durable=archive_durable,
                    source_present_after="true",
                    source_directory_synced=False,
                    remote=remote_summary,
                    errors=[str(exc)],
                )
            return ArchiveResult(
                status=STATUS_INDETERMINATE,
                reason_code="source_unlink_failed_presence_unconfirmed",
                pr_number=pr_number,
                source_relpath=source_relpath,
                marker_sha256=f"sha256:{validated.sha256}",
                archive_locator=locator_rel,
                archive_durable=archive_durable,
                source_present_after="unknown",
                source_directory_synced=False,
                remote=remote_summary,
                errors=[str(exc)],
            )

        # -- SOURCE_REMOVED reached. Attempt SOURCE_REMOVAL_DURABLE (fsync the
        # parent directory) then a final readback. Any failure past this point
        # cannot claim "retained" (the file is gone) -- indeterminate only.
        try:
            fsync_parent_dir(validated)
            source_directory_synced = True
        except OSError as exc:
            _close_validated_fds()
            return ArchiveResult(
                status=STATUS_INDETERMINATE,
                reason_code="source_directory_fsync_failed",
                pr_number=pr_number,
                source_relpath=source_relpath,
                marker_sha256=f"sha256:{validated.sha256}",
                archive_locator=locator_rel,
                archive_durable=archive_durable,
                source_present_after="false",
                source_directory_synced=False,
                remote=remote_summary,
                errors=[str(exc)],
            )

        final_presence = source_still_present(validated)
        _close_validated_fds()
        if final_presence != SOURCE_PRESENCE_ABSENT:
            # PR #1628 review P0-5: success requires FileNotFoundError-proven
            # absence. Anything else (same inode still there, a DIFFERENT
            # inode recreated at the canonical path, or an unconfirmable
            # stat) is indeterminate, and a different-inode recreation is
            # reported as present (source_present_after=true), never as
            # "unknown"/"false".
            if final_presence == SOURCE_PRESENCE_UNKNOWN:
                present_after = "unknown"
                reason_code = "source_removal_readback_inconclusive"
            else:
                present_after = "true"
                reason_code = "source_removal_readback_different_inode_present"
            return ArchiveResult(
                status=STATUS_INDETERMINATE,
                reason_code=reason_code,
                pr_number=pr_number,
                source_relpath=source_relpath,
                marker_sha256=f"sha256:{validated.sha256}",
                archive_locator=locator_rel,
                archive_durable=archive_durable,
                source_present_after=present_after,
                source_directory_synced=source_directory_synced,
                remote=remote_summary,
            )

        return ArchiveResult(
            status=STATUS_ARCHIVED,
            reason_code=None,
            pr_number=pr_number,
            source_relpath=source_relpath,
            marker_sha256=f"sha256:{validated.sha256}",
            archive_locator=locator_rel,
            archive_durable=archive_durable,
            source_present_after="false",
            source_directory_synced=source_directory_synced,
            remote=remote_summary,
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--repo", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", dest="output_json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = DEFAULT_PROJECT_ROOT

    try:
        repo = resolve_repo(args.repo, project_root)
    except (ArchiveRefused, EnvironmentBlocked) as exc:
        status = STATUS_ENVIRONMENT_BLOCKED if isinstance(exc, EnvironmentBlocked) else STATUS_REFUSED
        result = ArchiveResult(
            status=status,
            reason_code=exc.reason_code,
            pr_number=args.pr_number,
            source_relpath=_source_relpath(args.pr_number),
            errors=[exc.message],
        )
        _emit(result, args.output_json)
        return 1

    if args.dry_run:
        result = ArchiveResult(
            status=STATUS_REFUSED,
            reason_code="dry_run_no_mutation",
            pr_number=args.pr_number,
            source_relpath=_source_relpath(args.pr_number),
        )
        _emit(result, args.output_json)
        return 0

    result = run_archive(args.pr_number, repo, project_root, default_gh_caller)
    _emit(result, args.output_json)
    return 0 if result.status in (STATUS_ARCHIVED, STATUS_ALREADY_ARCHIVED) else 1


def _emit(result: ArchiveResult, output_json: bool) -> None:
    if output_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
