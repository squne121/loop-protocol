#!/usr/bin/env python3
"""temp_residue_classifier.py — read-only classifier for root temporary
residue (Issue #1417).

Emits ``temp_residue_classification/v1`` (see
schemas/temp_residue_classification_v1.schema.json). This module and its CLI
**never** call ``os.unlink``, ``os.rmdir``, ``shutil.rmtree``, or any mutating
subprocess. ``recommendation: eligible_for_delete`` is advisory only — it
means "a separate deletion executor MAY re-verify and consider this
directory", never that deletion already happened or is authorized by this
output alone. See ``docs/dev/repository-folder-policy.md`` for the folder
class matrix this classifier implements.

Import-safe (no side effects at import time).

Security notes (Issue #1417 PR #1427 review):
- All filesystem traversal below the validated ``project_root`` anchor is
  performed via directory-file-descriptor chains
  (``os.open(name, O_NOFOLLOW, dir_fd=parent_fd)`` + ``os.scandir(fd)`` +
  ``os.stat(name, dir_fd=fd, follow_symlinks=False)``). Every path component
  is opened relative to its *already-open* parent directory fd, so no
  pathname is ever re-resolved from the filesystem root after the initial
  anchor open. A symlink swapped into any intermediate component (including
  ``.claude`` itself) after ``project_root`` resolution cannot cause traversal
  to escape the repository, because ``O_NOFOLLOW`` makes the kernel refuse to
  open a symlink dirent (``ELOOP``) rather than following it.
- ``project_root`` itself is validated against ``git rev-parse
  --show-toplevel`` before any scanning starts; if validation fails, every
  entry is forced to ``report_only`` and ``project_root.validated`` is
  ``false``.
- Git tracked/ignored state is resolved via exactly two ``git`` subprocess
  invocations for the whole scan (``git ls-files -z --cached`` and
  ``git status --porcelain=v1 -z --untracked-files=all
  --ignored=matching``), not one invocation per directory, so the scan
  cannot be turned into an unbounded subprocess-spawn amplification attack.
- A single monotonic ``ScanBudget`` (entries + wall-clock deadline) is
  threaded through repository-root resolution, alias-root discovery, the git
  state fetch, and the walk itself; once exhausted, scanning stops early and
  ``scan_status`` is forced to ``partial``.
"""

from __future__ import annotations

import argparse
import bisect
import errno
import fnmatch
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from temp_residue_marker import (  # noqa: E402
    MARKER_FILENAME,
    STATE_UNREADABLE,
    MarkerResult,
    evaluate_marker_at,
)

SCHEMA_ID = "temp_residue_classification/v1"

FOLDER_CLASS_APPROVED = "repo_approved_temporary_workspace"
FOLDER_CLASS_ALIAS = "root_temporary_alias"

APPROVED_ROOTS = ("tmp", ".claude/tmp")
DENIED_ALIAS_EXACT = (".tmp", ".temp")
DENIED_ALIAS_GLOB = ".tmp-*"

DEFAULT_MAX_ENTRIES = 10000
DEFAULT_MAX_DEPTH = 32
DEFAULT_MAX_MARKER_BYTES = 4096
DEFAULT_DEADLINE_SECONDS = 5.0
DEFAULT_GIT_TIMEOUT_SECONDS = 5.0

RECOMMEND_REPORT_ONLY = "report_only"
RECOMMEND_ELIGIBLE = "eligible_for_delete"

# Deterministic reason-code priority table (index 0 = highest priority).
# ``primary_reason_code`` is always the lowest-index member of a given
# entry's reason_codes set; report_only-forcing codes are ordered first so
# that any single unsafe observation always wins the primary slot.
REASON_PRIORITY = [
    "project_root_not_validated",
    "scan_incomplete",
    "git_state_unknown",
    "permission_denied",
    "invalid_filename_encoding",
    "nested_symlink_present",
    "special_file_present",
    "mount_boundary_crossed",
    "top_level_symlink",
    "not_a_session_directory",
    "tracked_content_present",
    "marker_unreadable",
    "marker_untrusted",
    "marker_malformed",
    "marker_expired",
    "marker_session_mismatch",
    "marker_target_mismatch",
    "marker_mismatch",
    "eligibility_precondition_failed",
    "marker_absent",
    "denied_alias_report_only_policy",
    "root_itself",
    "owned_session_eligible",
]
_PRIORITY_INDEX = {code: i for i, code in enumerate(REASON_PRIORITY)}


def _priority_key(code: str) -> int:
    return _PRIORITY_INDEX.get(code, len(REASON_PRIORITY))


@dataclass
class ScanLimits:
    max_entries: int = DEFAULT_MAX_ENTRIES
    max_depth: int = DEFAULT_MAX_DEPTH
    max_marker_bytes: int = DEFAULT_MAX_MARKER_BYTES
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS


@dataclass
class ScanBudget:
    limits: ScanLimits
    start_monotonic: float
    entries_seen: int = 0

    def deadline_exceeded(self) -> bool:
        return (time.monotonic() - self.start_monotonic) > self.limits.deadline_seconds

    def entries_exceeded(self) -> bool:
        return self.entries_seen >= self.limits.max_entries

    def exhausted(self) -> bool:
        return self.deadline_exceeded() or self.entries_exceeded()

    def remaining_seconds(self, cap: float) -> float:
        """Remaining wall-clock budget, clamped to ``cap`` and never negative."""
        remaining = self.limits.deadline_seconds - (time.monotonic() - self.start_monotonic)
        if remaining <= 0:
            return 0.0
        return min(remaining, cap)


# ============================================================================
# Batched Git state index (Issue #1417 P0-3): exactly two `git` subprocess
# invocations for the whole scan, instead of one pair per directory.
# ============================================================================


@dataclass
class GitStateIndex:
    ok: bool
    tracked_sorted: list[str] = field(default_factory=list)
    untracked_sorted: list[str] = field(default_factory=list)
    ignored_sorted: list[str] = field(default_factory=list)

    def _subtree_and_exact(self, sorted_list: list[str], rel_path: str) -> set[str]:
        matches: set[str] = set()
        # Exact match (rel_path itself is a tracked/untracked/ignored file).
        i = bisect.bisect_left(sorted_list, rel_path)
        if i < len(sorted_list) and sorted_list[i] == rel_path:
            matches.add(rel_path)
        # Subtree match (anything under rel_path/).
        prefix = rel_path + "/"
        lo = bisect.bisect_left(sorted_list, prefix)
        hi = bisect.bisect_left(sorted_list, prefix[:-1] + "0")  # '/' + 1 == '0'
        matches.update(sorted_list[lo:hi])
        return matches

    def tri_state(self, rel_path: str) -> tuple[str, str]:
        if not self.ok:
            return "unknown", "unknown"
        tracked = self._subtree_and_exact(self.tracked_sorted, rel_path)
        untracked = self._subtree_and_exact(self.untracked_sorted, rel_path)
        ignored = self._subtree_and_exact(self.ignored_sorted, rel_path)
        all_paths = tracked | untracked | ignored
        if not all_paths:
            return "none", "none"
        if not tracked:
            tracked_state = "none"
        elif tracked == all_paths:
            tracked_state = "all"
        else:
            tracked_state = "some"
        if not ignored:
            ignored_state = "none"
        elif ignored == all_paths:
            ignored_state = "all"
        else:
            ignored_state = "some"
        return tracked_state, ignored_state


def _run_git_z(args: list[str], cwd: str, timeout: float) -> tuple[bool, list[str]]:
    if timeout <= 0:
        return False, []
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False, []
    if result.returncode not in (0, 1):
        return False, []
    try:
        out = result.stdout.decode("utf-8", errors="surrogateescape")
    except Exception:
        return False, []
    return True, [p for p in out.split("\0") if p]


def build_git_state_index(project_root: str, budget: ScanBudget) -> GitStateIndex:
    """Build the whole-repository tracked/untracked/ignored index with exactly
    two ``git`` subprocess invocations, budgeted against ``budget``'s
    remaining monotonic deadline (Issue #1417 P0-3)."""
    timeout = budget.remaining_seconds(DEFAULT_GIT_TIMEOUT_SECONDS)
    ok1, tracked = _run_git_z(["ls-files", "-z", "--cached"], project_root, timeout)
    if not ok1:
        return GitStateIndex(ok=False)

    timeout2 = budget.remaining_seconds(DEFAULT_GIT_TIMEOUT_SECONDS)
    ok2, status_tokens = _run_git_z(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching"],
        project_root,
        timeout2,
    )
    if not ok2:
        return GitStateIndex(ok=False)

    untracked: list[str] = []
    ignored: list[str] = []
    for tok in status_tokens:
        if len(tok) < 4:
            continue
        xy = tok[:2]
        p = tok[3:]
        if xy == "!!":
            ignored.append(p)
        elif xy == "??":
            untracked.append(p)

    return GitStateIndex(
        ok=True,
        tracked_sorted=sorted(set(tracked)),
        untracked_sorted=sorted(set(untracked)),
        ignored_sorted=sorted(set(ignored)),
    )


# ============================================================================
# project_root validation (Issue #1417 P0-5)
# ============================================================================


def resolve_project_root(explicit: str | None) -> tuple[str, str]:
    """Returns (project_root, source) per project_root.source enum."""
    if explicit:
        return os.path.realpath(explicit), "script_location"
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        return os.path.realpath(env_root), "claude_project_dir"
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return os.path.realpath(out.stdout.strip()), "git_toplevel"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return os.path.realpath(os.getcwd()), "script_location"


def validate_project_root(project_root: str, timeout: float) -> tuple[bool, dict | None]:
    """Confirm ``project_root`` is exactly the toplevel of a Git worktree
    rooted at that path (Issue #1417 P0-5). Returns (validated, error|None).

    Fail-closed: any subprocess failure, non-zero exit, or path mismatch is
    ``validated: False``. This is intentionally re-checked even when
    ``project_root`` was itself derived from ``git rev-parse --show-toplevel``,
    so a caller-supplied ``--project-root`` that does not match the real
    toplevel (or points at a non-repository / unreadable directory) is
    rejected rather than trusted.
    """
    if timeout <= 0:
        return False, {"reason_code": "project_root_not_validated", "message": "deadline_exhausted", "path": None}
    if not os.path.isdir(project_root):
        return False, {"reason_code": "project_root_not_validated", "message": "not_a_directory", "path": None}
    try:
        out = subprocess.run(
            ["git", "-C", project_root, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, {"reason_code": "project_root_not_validated", "message": "git_rev_parse_failed", "path": None}
    if out.returncode != 0 or not out.stdout.strip():
        return False, {"reason_code": "project_root_not_validated", "message": "not_a_git_repository", "path": None}
    toplevel = os.path.realpath(out.stdout.strip())
    if toplevel != os.path.realpath(project_root):
        return False, {
            "reason_code": "project_root_not_validated",
            "message": "explicit_root_not_git_toplevel",
            "path": None,
        }
    return True, None


def resolve_repository_slug(project_root: str, timeout: float) -> str | None:
    if timeout <= 0:
        return None
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    url = out.stdout.strip()
    if not url:
        return None
    slug = url.rstrip("/")
    if slug.endswith(".git"):
        slug = slug[: -len(".git")]
    if "github.com" in slug:
        slug = slug.split("github.com", 1)[1].lstrip(":/")
    return slug or None


# ============================================================================
# dir-fd based, symlink-safe traversal (Issue #1417 P0-1)
# ============================================================================


class _DirFdOpenError(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _open_component_nofollow(parent_fd: int, name: str, *, want_dir: bool) -> int:
    """Open ``name`` relative to the already-open ``parent_fd``, refusing to
    follow a symlink dirent. Raises ``_DirFdOpenError`` on any failure.

    This is the core symlink-component-escape defense (Issue #1417 P0-1):
    the kernel resolves ``name`` only against ``parent_fd``'s directory
    entry table, never against a full pathname, so a symlink swapped into
    any ancestor component after ``project_root`` was validated cannot be
    followed here.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if want_dir and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise _DirFdOpenError("not_found") from exc
        if exc.errno == errno.ELOOP:
            raise _DirFdOpenError("symlink_component") from exc
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise _DirFdOpenError("permission_denied") from exc
        if want_dir and exc.errno == errno.ENOTDIR:
            # want_dir + O_NOFOLLOW + a non-directory terminal component is,
            # for our callers, always either a symlink (kernels vary on
            # ELOOP vs ENOTDIR when O_DIRECTORY is combined with O_NOFOLLOW)
            # or a plain file where a directory was expected; treat both as
            # the same "refuse, do not traverse" outcome as a symlink.
            raise _DirFdOpenError("symlink_component") from exc
        raise _DirFdOpenError("open_error") from exc


def _open_parent_chain(project_root: str, components: list[str]) -> tuple[int | None, str | None]:
    """Open ``project_root/<components...>`` (all but the caller's terminal
    component, if any) as a directory fd via a component-by-component
    O_NOFOLLOW chain. ``project_root`` itself is the single trusted,
    already-validated anchor (opened by absolute path once); every
    subsequent component is opened relative to its already-open parent, so
    a symlink swapped into any of THESE components cannot be followed.
    """
    try:
        root_fd = os.open(project_root, os.O_RDONLY | (os.O_DIRECTORY if hasattr(os, "O_DIRECTORY") else 0))
    except OSError:
        return None, "root_open_failed"
    cur_fd = root_fd
    try:
        for comp in components:
            new_fd = _open_component_nofollow(cur_fd, comp, want_dir=True)
            os.close(cur_fd)
            cur_fd = new_fd
        return cur_fd, None
    except _DirFdOpenError as exc:
        os.close(cur_fd)
        return None, exc.reason_code


_SURROGATE_LOW = 0xDC80
_SURROGATE_HIGH = 0xDCFF


def _contains_surrogate(name: str) -> bool:
    """True iff ``name`` contains a surrogateescape-decoded byte, i.e. the
    on-disk filename is not valid UTF-8 (Issue #1417 P0-4)."""
    return any(_SURROGATE_LOW <= ord(ch) <= _SURROGATE_HIGH for ch in name)


def classify_entry_type(st: os.stat_result | None, is_symlink: bool) -> str:
    if st is None:
        return "unknown"
    if is_symlink:
        return "symlink"
    if stat.S_ISDIR(st.st_mode):
        return "directory"
    if stat.S_ISREG(st.st_mode):
        return "regular_file"
    if stat.S_ISFIFO(st.st_mode) or stat.S_ISSOCK(st.st_mode) or stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
        return "special"
    return "unknown"


@dataclass
class _WalkResult:
    nested_symlink: bool = False
    special_file: bool = False
    mount_crossed: bool = False
    depth_truncated: bool = False
    permission_denied: bool = False


def _bounded_walk_flags(
    dir_fd: int, root_dev: int, budget: ScanBudget, max_depth: int
) -> _WalkResult:
    """Bounded, dir-fd-based, non-following walk rooted at the already-open
    ``dir_fd``. Stops early (recording ``depth_truncated`` or leaving
    ``budget`` exhausted) once max_entries / max_depth / deadline is hit;
    callers must check ``budget`` state after calling this. Takes ownership
    of ``dir_fd`` (closes it before returning).
    """
    result = _WalkResult()
    # Stack of (fd, depth); each fd is owned by this function and closed
    # after its children have been scanned.
    stack: list[tuple[int, int]] = [(dir_fd, 0)]
    while stack:
        if budget.exhausted():
            # Close remaining fds to avoid descriptor leaks on early exit.
            for fd, _d in stack:
                try:
                    os.close(fd)
                except OSError:
                    pass
            stack = []
            break
        current_fd, depth = stack.pop()
        try:
            with os.scandir(current_fd) as it:
                for de in it:
                    if budget.exhausted():
                        break
                    budget.entries_seen += 1
                    if _contains_surrogate(de.name):
                        result.special_file = True
                        continue
                    try:
                        is_link = de.is_symlink()
                    except PermissionError:
                        result.permission_denied = True
                        continue
                    except OSError:
                        result.special_file = True
                        continue
                    if is_link:
                        result.nested_symlink = True
                        continue
                    try:
                        child_st = de.stat(follow_symlinks=False)
                    except PermissionError:
                        result.permission_denied = True
                        continue
                    except OSError:
                        result.special_file = True
                        continue
                    if child_st.st_dev != root_dev:
                        result.mount_crossed = True
                        continue
                    if stat.S_ISDIR(child_st.st_mode):
                        if depth + 1 > max_depth:
                            result.depth_truncated = True
                            continue
                        try:
                            child_fd = _open_component_nofollow(current_fd, de.name, want_dir=True)
                        except _DirFdOpenError as exc:
                            if exc.reason_code == "permission_denied":
                                result.permission_denied = True
                            else:
                                # Raced out from under us (removed / replaced)
                                # or a symlink appeared between is_symlink()
                                # and open(); fail closed as unsafe.
                                result.nested_symlink = True
                            continue
                        stack.append((child_fd, depth + 1))
                    elif not stat.S_ISREG(child_st.st_mode):
                        result.special_file = True
        except PermissionError:
            result.permission_denied = True
        except OSError:
            result.special_file = True
        finally:
            try:
                os.close(current_fd)
            except OSError:
                pass
    return result


@dataclass
class ClassifyContext:
    project_root: str
    limits: ScanLimits
    current_session_id: str | None
    repository: str | None
    now: datetime
    git_index: GitStateIndex
    project_root_validated: bool
    errors: list[dict] = field(default_factory=list)
    scan_incomplete: bool = False


def _make_entry(
    rel_path: str,
    folder_class: str,
    entry_type: str,
    tracked_state: str,
    ignored_state: str,
    ownership_marker: dict,
    reason_codes: list[str],
    observation: dict,
    *,
    project_root_validated: bool,
) -> dict:
    codes = list(reason_codes)
    if not project_root_validated:
        codes.append("project_root_not_validated")
    reason_codes_sorted = sorted(set(codes), key=_priority_key)
    primary = reason_codes_sorted[0] if reason_codes_sorted else "unclassified"
    force_report_only = primary != "owned_session_eligible" or not project_root_validated
    recommendation = RECOMMEND_REPORT_ONLY if force_report_only else RECOMMEND_ELIGIBLE
    return {
        "path": rel_path,
        "folder_class": folder_class,
        "entry_type": entry_type,
        "tracked_state": tracked_state,
        "ignored_state": ignored_state,
        "ownership_marker": ownership_marker,
        "recommendation": recommendation,
        "primary_reason_code": primary,
        "reason_codes": reason_codes_sorted,
        "observation": observation,
    }


_ABSENT_MARKER = {
    "state": "absent",
    "schema": None,
    "session_match": None,
    "target_match": None,
    "expired": None,
}


def _observation_from_stat(st: os.stat_result | None) -> dict:
    if st is None:
        return {"device": None, "inode": None, "mtime_ns": None}
    return {"device": st.st_dev, "inode": st.st_ino, "mtime_ns": st.st_mtime_ns}


def classify_root(
    root_rel: str,
    folder_class: str,
    ctx: ClassifyContext,
    budget: ScanBudget,
) -> list[dict]:
    """Classify a single approved/denied root and its direct child entries."""
    entries: list[dict] = []
    components = root_rel.split("/")
    parent_components, leaf = components[:-1], components[-1]

    parent_fd, open_err = _open_parent_chain(ctx.project_root, parent_components)
    if open_err == "not_found":
        return entries
    if open_err is not None:
        ctx.errors.append({"reason_code": "scan_error", "message": open_err, "path": root_rel})
        ctx.scan_incomplete = True
        return entries

    # Stat the terminal component (fd-relative, non-following) BEFORE
    # opening it, so a symlinked root (e.g. `tmp` itself replaced with a
    # symlink) is reported as a `symlink` entry rather than silently
    # producing no entry at all — matching the schema's original shape
    # while still never following the symlink.
    try:
        root_st = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.close(parent_fd)
        return entries
    except OSError as exc:
        ctx.errors.append({"reason_code": "scan_error", "message": _errno_name(exc), "path": root_rel})
        ctx.scan_incomplete = True
        os.close(parent_fd)
        return entries

    root_is_symlink = stat.S_ISLNK(root_st.st_mode)
    root_entry_type = classify_entry_type(root_st, root_is_symlink)
    tracked_state, ignored_state = ctx.git_index.tri_state(root_rel)
    reason_codes = ["root_itself"]
    if root_is_symlink:
        reason_codes.append("top_level_symlink")
    if not ctx.git_index.ok:
        reason_codes.append("git_state_unknown")
        tracked_state, ignored_state = "unknown", "unknown"
    entries.append(
        _make_entry(
            root_rel, folder_class, root_entry_type, tracked_state, ignored_state,
            dict(_ABSENT_MARKER), reason_codes, _observation_from_stat(root_st),
            project_root_validated=ctx.project_root_validated,
        )
    )

    if root_is_symlink or root_entry_type != "directory":
        os.close(parent_fd)
        return entries

    root_dev = root_st.st_dev

    try:
        root_fd = _open_component_nofollow(parent_fd, leaf, want_dir=True)
    except _DirFdOpenError as exc:
        ctx.errors.append({"reason_code": "scan_error", "message": exc.reason_code, "path": root_rel})
        ctx.scan_incomplete = True
        return entries
    finally:
        os.close(parent_fd)

    try:
        children = sorted(os.scandir(root_fd), key=lambda d: d.name)
    except OSError as exc:
        ctx.errors.append({"reason_code": "scan_error", "message": _errno_name(exc), "path": root_rel})
        ctx.scan_incomplete = True
        os.close(root_fd)
        return entries

    for child in children:
        if budget.exhausted():
            ctx.scan_incomplete = True
            ctx.errors.append(
                {"reason_code": "scan_limit_exceeded", "message": "bounded scan stopped early", "path": root_rel}
            )
            break
        budget.entries_seen += 1
        if _contains_surrogate(child.name):
            ctx.scan_incomplete = True
            ctx.errors.append(
                {
                    "reason_code": "invalid_filename_encoding",
                    "message": "undecodable filename skipped",
                    "path": root_rel,
                }
            )
            entries.append(
                _make_entry(
                    f"{root_rel}/<invalid-encoding>", folder_class, "unknown", "unknown", "unknown",
                    dict(_ABSENT_MARKER), ["invalid_filename_encoding"], _observation_from_stat(None),
                    project_root_validated=ctx.project_root_validated,
                )
            )
            continue
        child_rel = f"{root_rel}/{child.name}"
        entries.append(classify_child(child, child_rel, folder_class, ctx, budget, root_dev, root_fd))

    os.close(root_fd)
    return entries


def _errno_name(exc: OSError) -> str:
    """Sanitized, secret-safe error message: errno name only, never the raw
    exception string (which may embed an absolute local path)."""
    if exc.errno is not None:
        try:
            return errno.errorcode.get(exc.errno, f"errno_{exc.errno}")
        except Exception:
            return f"errno_{exc.errno}"
    return "os_error"


def classify_child(
    child: os.DirEntry,
    child_rel: str,
    folder_class: str,
    ctx: ClassifyContext,
    budget: ScanBudget,
    root_dev: int,
    parent_fd: int,
) -> dict:
    try:
        is_symlink = child.is_symlink()
    except OSError:
        is_symlink = False
    try:
        child_st = child.stat(follow_symlinks=False)
    except OSError as exc:
        ctx.errors.append({"reason_code": "scan_error", "message": _errno_name(exc), "path": child_rel})
        return _make_entry(
            child_rel, folder_class, "unknown", "unknown", "unknown",
            dict(_ABSENT_MARKER), ["scan_incomplete"], {"device": None, "inode": None, "mtime_ns": None},
            project_root_validated=ctx.project_root_validated,
        )

    entry_type = classify_entry_type(child_st, is_symlink)
    tracked_state, ignored_state = ctx.git_index.tri_state(child_rel)
    observation = _observation_from_stat(child_st)

    reason_codes: list[str] = []
    if not ctx.git_index.ok:
        reason_codes.append("git_state_unknown")
        tracked_state, ignored_state = "unknown", "unknown"

    if folder_class == FOLDER_CLASS_ALIAS:
        reason_codes.append("denied_alias_report_only_policy")
        marker_dict = dict(_ABSENT_MARKER)
        if is_symlink:
            reason_codes.append("top_level_symlink")
        elif entry_type != "directory":
            reason_codes.append("not_a_session_directory")
        if tracked_state not in ("none", "unknown"):
            reason_codes.append("tracked_content_present")
        return _make_entry(
            child_rel, folder_class, entry_type, tracked_state, ignored_state,
            marker_dict, reason_codes, observation,
            project_root_validated=ctx.project_root_validated,
        )

    # FOLDER_CLASS_APPROVED path: session-directory eligibility evaluation.
    if is_symlink:
        reason_codes.append("top_level_symlink")
        return _make_entry(
            child_rel, folder_class, entry_type, tracked_state, ignored_state,
            dict(_ABSENT_MARKER), reason_codes, observation,
            project_root_validated=ctx.project_root_validated,
        )
    if entry_type != "directory":
        reason_codes.append("not_a_session_directory")
        return _make_entry(
            child_rel, folder_class, entry_type, tracked_state, ignored_state,
            dict(_ABSENT_MARKER), reason_codes, observation,
            project_root_validated=ctx.project_root_validated,
        )
    if child_st.st_dev != root_dev:
        reason_codes.append("mount_boundary_crossed")
        return _make_entry(
            child_rel, folder_class, entry_type, tracked_state, ignored_state,
            dict(_ABSENT_MARKER), reason_codes, observation,
            project_root_validated=ctx.project_root_validated,
        )

    # Open the session directory once via the dir-fd chain, and read its
    # ownership marker relative to that same fd (Issue #1417 P0-1 / P0-5):
    # no marker-path pathname is ever re-resolved from the filesystem root,
    # so a symlink swapped into an ancestor directory between our scan of
    # `parent_fd` and this read cannot be followed.
    try:
        child_fd = _open_component_nofollow(parent_fd, child.name, want_dir=True)
    except _DirFdOpenError:
        # Raced: replaced with a symlink between stat() and open(). Fail
        # closed as an unsafe observation rather than silently proceeding.
        reason_codes.append("nested_symlink_present")
        marker_result = None
    else:
        try:
            marker_dir_fd = os.dup(child_fd)
        except OSError:
            marker_result = None
        else:
            try:
                marker_result = evaluate_marker_at(
                    marker_dir_fd,
                    MARKER_FILENAME,
                    current_session_id=ctx.current_session_id,
                    expected_target_relpath=child_rel,
                    expected_repository=ctx.repository,
                    now=ctx.now,
                    max_bytes=ctx.limits.max_marker_bytes,
                )
            finally:
                os.close(marker_dir_fd)
        walk = _bounded_walk_flags(child_fd, root_dev, budget, ctx.limits.max_depth)
        if walk.depth_truncated:
            ctx.scan_incomplete = True
            reason_codes.append("scan_incomplete")
            ctx.errors.append(
                {"reason_code": "max_depth_exceeded", "message": "bounded scan stopped at max_depth", "path": child_rel}
            )
        if walk.nested_symlink:
            reason_codes.append("nested_symlink_present")
        if walk.special_file:
            reason_codes.append("special_file_present")
        if walk.mount_crossed:
            reason_codes.append("mount_boundary_crossed")
        if walk.permission_denied:
            ctx.scan_incomplete = True
            reason_codes.append("scan_incomplete")
            reason_codes.append("permission_denied")
            ctx.errors.append(
                {"reason_code": "permission_denied", "message": "nested entry not readable", "path": child_rel}
            )
    if budget.exhausted():
        ctx.scan_incomplete = True
        reason_codes.append("scan_incomplete")
    if tracked_state not in ("none", "unknown"):
        reason_codes.append("tracked_content_present")

    if marker_result is None:
        marker_result = MarkerResult(STATE_UNREADABLE, None, "session_dir_open_failed")

    marker_dict = {
        "state": marker_result.state,
        "schema": "temp_residue_owner/v1" if marker_result.data is not None else None,
        "session_match": marker_result.session_match,
        "target_match": marker_result.target_match,
        "expired": marker_result.expired,
    }

    if marker_result.state == "absent":
        reason_codes.append("marker_absent")
    elif marker_result.state == "unreadable":
        reason_codes.append("marker_unreadable")
    elif marker_result.state == "untrusted":
        reason_codes.append("marker_untrusted")
    elif marker_result.state == "malformed":
        reason_codes.append("marker_malformed")
    elif marker_result.state == "mismatch":
        if marker_result.expired:
            reason_codes.append("marker_expired")
        if marker_result.session_match is False:
            reason_codes.append("marker_session_mismatch")
        if marker_result.target_match is False:
            reason_codes.append("marker_target_mismatch")
        if not any(
            c in reason_codes
            for c in ("marker_expired", "marker_session_mismatch", "marker_target_mismatch")
        ):
            reason_codes.append("marker_mismatch")
    elif marker_result.state == "valid":
        if not any(
            c in reason_codes
            for c in (
                "scan_incomplete", "git_state_unknown", "nested_symlink_present",
                "special_file_present", "mount_boundary_crossed", "tracked_content_present",
            )
        ):
            reason_codes.append("owned_session_eligible")
        else:
            # Issue #1417 PR #1427 review: the marker itself is valid, but
            # another unsafe scan observation already forces report_only.
            # Use a distinct reason code so `ownership_marker.state: valid`
            # never coexists with a `marker_mismatch`-labeled diagnosis,
            # which would read as self-contradictory.
            reason_codes.append("eligibility_precondition_failed")

    return _make_entry(
        child_rel, folder_class, entry_type, tracked_state, ignored_state,
        marker_dict, reason_codes, observation,
        project_root_validated=ctx.project_root_validated,
    )


def _discover_denied_alias_roots(project_root: str, budget: ScanBudget) -> list[str]:
    roots = list(DENIED_ALIAS_EXACT)
    if budget.exhausted():
        return sorted(set(roots))
    try:
        with os.scandir(project_root) as it:
            for de in it:
                if budget.exhausted():
                    break
                budget.entries_seen += 1
                if de.name in DENIED_ALIAS_EXACT:
                    continue
                if fnmatch.fnmatchcase(de.name, DENIED_ALIAS_GLOB) and "/" not in de.name:
                    roots.append(de.name)
    except OSError:
        pass
    return sorted(set(roots))


def run_classification(
    project_root_arg: str | None,
    limits: ScanLimits,
    current_session_id: str | None,
) -> dict:
    now = datetime.now(timezone.utc)
    # Budget starts before project_root resolution (Issue #1417 P0-3): every
    # subsequent subprocess / scan call is charged against the same clock.
    budget = ScanBudget(limits=limits, start_monotonic=time.monotonic())

    project_root, source = resolve_project_root(project_root_arg)

    validate_timeout = budget.remaining_seconds(5.0)
    project_root_validated, validation_error = validate_project_root(project_root, validate_timeout)

    top_level_errors: list[dict] = []
    if validation_error is not None:
        top_level_errors.append(validation_error)

    repository = resolve_repository_slug(project_root, budget.remaining_seconds(5.0))

    git_index = build_git_state_index(project_root, budget)

    ctx = ClassifyContext(
        project_root=project_root,
        limits=limits,
        current_session_id=current_session_id,
        repository=repository,
        now=now,
        git_index=git_index,
        project_root_validated=project_root_validated,
        errors=top_level_errors,
    )

    entries: list[dict] = []
    if project_root_validated:
        for root_rel in APPROVED_ROOTS:
            if budget.exhausted():
                ctx.scan_incomplete = True
                break
            entries.extend(classify_root(root_rel, FOLDER_CLASS_APPROVED, ctx, budget))
        for root_rel in _discover_denied_alias_roots(project_root, budget):
            if budget.exhausted():
                ctx.scan_incomplete = True
                break
            entries.extend(classify_root(root_rel, FOLDER_CLASS_ALIAS, ctx, budget))
    else:
        ctx.scan_incomplete = True

    scan_status = "ok"
    if ctx.errors:
        scan_status = "partial" if ctx.scan_incomplete else "error"
    elif ctx.scan_incomplete:
        scan_status = "partial"

    return {
        "schema": SCHEMA_ID,
        "scan_status": scan_status,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "project_root": {"source": source, "validated": project_root_validated},
        "entries": entries,
        "errors": ctx.errors,
    }


def _emit_yaml(result: dict) -> str:
    return json.dumps(result, indent=2, sort_keys=False, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only root temporary residue classifier")
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--format", choices=["json", "yaml"], default="json")
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    parser.add_argument("--max-marker-bytes", type=int, default=DEFAULT_MAX_MARKER_BYTES)
    parser.add_argument("--deadline-seconds", type=float, default=DEFAULT_DEADLINE_SECONDS)
    parser.add_argument(
        "--current-session-id",
        default=os.environ.get("LOOP_PROTOCOL_SESSION_ID"),
        help="Opaque current session id for marker session_match evaluation (accidental isolation model only).",
    )
    args = parser.parse_args(argv)

    limits = ScanLimits(
        max_entries=args.max_entries,
        max_depth=args.max_depth,
        max_marker_bytes=args.max_marker_bytes,
        deadline_seconds=args.deadline_seconds,
    )
    result = run_classification(args.project_root, limits, args.current_session_id)

    if args.format == "json":
        payload = json.dumps(result, indent=2, ensure_ascii=False)
    else:
        payload = _emit_yaml(result)
    # Fail-closed guarantee (Issue #1417 P0-4): the emitted payload must be
    # strict UTF-8 (never surrogateescape-decoded bytes leaking through).
    sys.stdout.buffer.write(payload.encode("utf-8", errors="strict"))
    sys.stdout.buffer.write(b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
