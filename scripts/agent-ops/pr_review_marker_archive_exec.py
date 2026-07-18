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
     204=merged / 404=unmerged -- never a string compare on PR state);
  3. the marker's review_id is the *primary key* used to fetch the exact
     GitHub review and cross-validate id / html_url / pull_request_url /
     state / commit_id / trailing idempotency-derived body marker
     (Issue #1602 AC2).

Only after all of the above hold does the executor:
  - write an immutable, content-addressed archive envelope to a repo-external
    state root (XDG_STATE_HOME authority) using create-once / no-overwrite
    semantics (exclusive temp file + hardlink publish);
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
    for missing tooling), reason_code identifies the failed check.
  - A failure after ARCHIVE_DURABLE but before/during the source-removal
    unlink: if the source can still be positively confirmed present,
    status=source_retained. If presence cannot be confirmed either way
    (e.g. the confirming stat/readback itself failed), status=indeterminate
    -- the executor never claims "marker retained" without proof.
  - A failure after the source has been removed (SOURCE_REMOVED) but before
    SOURCE_REMOVAL_DURABLE / final readback completes: status=indeterminate
    (the source is gone; the executor cannot promise durability was
    observed, so it does not claim success either).
  - Full completion: status=archived. A retry against an already-archived,
    now-absent source that matches a pre-existing valid archive envelope
    returns status=already_archived (idempotent reconciliation).

Command line:
    uv run python3 scripts/agent-ops/pr_review_marker_archive_exec.py \
        --pr-number 1594 [--repo squne121/loop-protocol] [--dry-run] [--json]

Only post-merge-cleanup is expected to invoke this executor, and only with
an explicit merged PR number -- no path/glob user input is accepted; the
source path is entirely derived by the executor itself from --pr-number.

Issue #1602.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import stat as stat_module
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------

_THIS_FILE = Path(__file__).resolve()
# scripts/agent-ops/ -> scripts/ -> project_root
DEFAULT_PROJECT_ROOT = _THIS_FILE.parent.parent.parent

RESULT_SCHEMA = "PR_REVIEW_MARKER_ARCHIVE_RESULT_V1"
MARKER_SCHEMA = "PR_REVIEW_PUBLISH_MARKER_V1"
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
TRUSTED_GITHUB_HOST = "github.com"

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
    if not isinstance(review_id, (int, str)) or review_id in (None, ""):
        raise ArchiveRefused("marker_field_missing_review_id", "")


# --------------------------------------------------------------------------
# Untracked / primary-worktree / default-branch preconditions
# --------------------------------------------------------------------------


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=15,
    )


def ensure_source_untracked(project_root: Path, relpath: str) -> None:
    git_bin = shutil.which("git")
    if git_bin is None:
        raise EnvironmentBlocked("environment_blocked_missing_git", "git not found")
    try:
        proc = run_git(["ls-files", "--error-unmatch", relpath], cwd=project_root)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise EnvironmentBlocked("environment_blocked_git_invocation_failed", str(exc)) from exc
    if proc.returncode == 0:
        raise ArchiveRefused("git_tracked_file_conflict", relpath)


def ensure_primary_default_worktree(project_root: Path) -> None:
    git_bin = shutil.which("git")
    if git_bin is None:
        raise EnvironmentBlocked("environment_blocked_missing_git", "git not found")
    try:
        toplevel = run_git(["rev-parse", "--show-toplevel"], cwd=project_root)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise EnvironmentBlocked("environment_blocked_git_invocation_failed", str(exc)) from exc
    if toplevel.returncode != 0:
        raise ArchiveRefused("not_a_git_worktree", toplevel.stderr.strip())
    if os.path.realpath(toplevel.stdout.strip()) != os.path.realpath(str(project_root)):
        raise ArchiveRefused("source_repo_root_mismatch", toplevel.stdout.strip())


# --------------------------------------------------------------------------
# GitHub remote readback (AC2)
# --------------------------------------------------------------------------

GhCaller = Callable[[list[str]], tuple[int, str, str]]


def default_gh_caller(argv: list[str]) -> tuple[int, str, str]:
    gh_bin = shutil.which("gh")
    if gh_bin is None:
        raise EnvironmentBlocked("environment_blocked_missing_gh", "gh not found")
    try:
        proc = subprocess.run(
            [gh_bin, *argv],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise EnvironmentBlocked("environment_blocked_gh_invocation_failed", str(exc)) from exc
    return proc.returncode, proc.stdout, proc.stderr


def remote_check_merged(repo: str, pr_number: int, gh_caller: GhCaller) -> bool:
    """Uses the dedicated merged-check endpoint: 204=merged, 404=unmerged.
    Never a string compare on PR .state."""
    rc, out, err = gh_caller(
        [
            "api",
            "--hostname",
            TRUSTED_GITHUB_HOST,
            "-i",
            f"repos/{repo}/pulls/{pr_number}/merge",
        ]
    )
    status_line = (out.splitlines() or [""])[0]
    if "204" in status_line:
        return True
    if "404" in status_line:
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


def _pr_review_marker_str(idempotency_key: str) -> str:
    marker_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    return f"{PR_REVIEW_MARKER_PREFIX}{marker_hash}{PR_REVIEW_MARKER_SUFFIX}"


def validate_remote_binding(
    marker: dict[str, Any],
    review: dict[str, Any],
    repo: str,
    pr_number: int,
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
    if not isinstance(submitted_at, str) or not submitted_at:
        raise ArchiveRefused("remote_review_submitted_at_missing", "")
    body = review.get("body")
    if not isinstance(body, str):
        raise ArchiveRefused("remote_review_body_missing", "")
    expected_marker_str = _pr_review_marker_str(marker["idempotency_key"])
    if body.count(expected_marker_str) != 1:
        raise ArchiveRefused("remote_review_body_marker_mismatch", "")
    if not body.rstrip("\n").endswith(expected_marker_str):
        raise ArchiveRefused("remote_review_body_marker_not_trailing", "")


# --------------------------------------------------------------------------
# Archive root resolution (XDG_STATE_HOME authority, #1602 self-contained
# equivalent of the #1546 external-state-root contract)
# --------------------------------------------------------------------------


def resolve_archive_root(env: dict[str, str] | None = None) -> Path:
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
    return root


def _ensure_private_dir(path: Path) -> None:
    """Create `path` (and ancestors) with mode 0700, refusing symlinks and
    ownership/permission drift on the leaf directory."""
    is_abs = os.path.isabs(str(path))
    parts = path.parts
    current = Path(parts[0]) if is_abs else Path(".")
    remaining = parts[1:] if is_abs else parts
    for part in remaining:
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
        "schema": "PR_REVIEW_MARKER_ARCHIVE_ENVELOPE_V1",
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


def write_archive_no_overwrite(archive_root: Path, locator_rel: str, envelope: dict[str, Any]) -> tuple[bool, Path]:
    """Publish `envelope` at archive_root/locator_rel using create-once /
    no-overwrite semantics: write to an exclusive temp file, fsync it, then
    publish via os.link() (which fails with EEXIST if the destination
    already exists -- there is no window where an existing archive can be
    silently replaced), fsync the parent directory, then remove the temp
    name. Returns (already_existed, final_path)."""
    final_path = archive_root / locator_rel
    final_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    if final_path.exists():
        return True, final_path

    tmp_name = f".{os.path.basename(locator_rel)}.{os.getpid()}.{int(time.time() * 1000)}.tmp"
    tmp_path = final_path.parent / tmp_name
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", closefd=True) as fh:
            fh.write(json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(str(tmp_path), str(final_path))
        except FileExistsError:
            # Another invocation published concurrently; treat as
            # already-archived (idempotent reconciliation), not a failure.
            return True, final_path
        finally:
            try:
                os.unlink(str(tmp_path))
            except OSError:
                pass
    except Exception:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        raise

    dir_fd = os.open(str(final_path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

    return False, final_path


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
    try:
        data = json.loads(candidates[0].read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("schema") != "PR_REVIEW_MARKER_ARCHIVE_ENVELOPE_V1":
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


def source_still_present(validated: ValidatedMarker) -> bool | None:
    """Returns True/False if presence can be positively determined, None if
    the check itself failed (caller must treat as indeterminate)."""
    try:
        st = os.stat(
            validated.marker_name, dir_fd=validated.parent_dir_fd, follow_symlinks=False
        )
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return st.st_dev == validated.st_dev and st.st_ino == validated.st_ino


def _source_relpath(pr_number: int) -> str:
    return os.path.join(
        "artifacts", str(pr_number), ISSUE_METADATA_SEGMENT, COMMAND_ID, MARKER_FILE_NAME
    )


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def resolve_repo(explicit_repo: str | None, project_root: Path) -> str:
    if explicit_repo:
        return explicit_repo
    git_bin = shutil.which("git")
    if git_bin is None:
        raise EnvironmentBlocked("environment_blocked_missing_git", "git not found")
    proc = run_git(["remote", "get-url", "origin"], cwd=project_root)
    if proc.returncode != 0:
        raise ArchiveRefused("repo_unresolvable", proc.stderr.strip())
    url = proc.stdout.strip()
    url = url.removesuffix(".git")
    for prefix in ("git@github.com:", "https://github.com/", "http://github.com/"):
        if url.startswith(prefix):
            return url[len(prefix):]
    raise ArchiveRefused("repo_unresolvable_unexpected_remote_format", url)


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

    try:
        validated = validate_and_open_marker(project_root, pr_number)
    except ArchiveRefused as exc:
        if exc.reason_code in SOURCE_ABSENT_REASON_CODES:
            # -- Idempotent reconciliation: source already gone. Look for a
            # single pre-existing valid archive envelope for this PR.
            archive_root = archive_root_override or resolve_archive_root()
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

    ensure_source_untracked(project_root, source_relpath)
    validate_marker_schema(validated.data, pr_number, repo)

    merged = remote_check_merged(repo, pr_number, gh_caller)
    remote_summary["merged"] = merged
    if not merged:
        raise ArchiveRefused("remote_pr_not_merged", "")

    review = remote_fetch_review(repo, pr_number, validated.data["review_id"], gh_caller)
    validate_remote_binding(validated.data, review, repo, pr_number)
    remote_summary.update(
        {
            "review_id": review.get("id"),
            "state": review.get("state"),
            "commit_id": review.get("commit_id"),
        }
    )

    # -- SOURCE_VALIDATED reached with all remote checks green. From here
    # on, any failure is classified relative to ARCHIVE_DURABLE / the
    # source-removal boundary rather than failed_before_archive.
    archive_root = archive_root_override or resolve_archive_root()
    envelope = build_archive_envelope(repo, pr_number, validated, validated.data, review, merged)
    locator_rel = archive_locator_relpath(repo, pr_number, validated.sha256)

    already_existed, archive_path = write_archive_no_overwrite(archive_root, locator_rel, envelope)
    if already_existed:
        existing = json.loads(archive_path.read_text())
        existing_hash = str(existing.get("marker_sha256", "")).removeprefix("sha256:")
        if existing_hash != validated.sha256:
            raise ArchiveRefused("archive_collision_hash_mismatch", "")
    archive_durable = True  # ARCHIVE_DURABLE reached (fsync'd in the writer)

    # -- ARCHIVE_DURABLE reached. Attempt source removal; classify any
    # failure from here on as source_retained / indeterminate, never as a
    # plain refusal (the marker's fate is no longer simply "untouched").
    try:
        remove_source_with_recheck(validated)
    except Exception as exc:  # noqa: BLE001 - must classify, not propagate
        presence = source_still_present(validated)
        os.close(validated.marker_fd)
        os.close(validated.parent_dir_fd)
        if presence is True:
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
        os.close(validated.marker_fd)
        os.close(validated.parent_dir_fd)
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
    os.close(validated.marker_fd)
    os.close(validated.parent_dir_fd)
    if final_presence is not False:
        return ArchiveResult(
            status=STATUS_INDETERMINATE,
            reason_code="source_removal_readback_inconclusive",
            pr_number=pr_number,
            source_relpath=source_relpath,
            marker_sha256=f"sha256:{validated.sha256}",
            archive_locator=locator_rel,
            archive_durable=archive_durable,
            source_present_after="unknown" if final_presence is None else "true",
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
