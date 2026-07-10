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
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from temp_residue_marker import (  # noqa: E402
    MARKER_FILENAME,
    evaluate_marker,
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

RECOMMEND_REPORT_ONLY = "report_only"
RECOMMEND_ELIGIBLE = "eligible_for_delete"

# Deterministic reason-code priority table (index 0 = highest priority).
# ``primary_reason_code`` is always the lowest-index member of a given
# entry's reason_codes set; report_only-forcing codes are ordered first so
# that any single unsafe observation always wins the primary slot.
REASON_PRIORITY = [
    "scan_incomplete",
    "git_state_unknown",
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
        import time

        return (time.monotonic() - self.start_monotonic) > self.limits.deadline_seconds

    def entries_exceeded(self) -> bool:
        return self.entries_seen >= self.limits.max_entries


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


def _run_git(args: list[str], cwd: str, timeout: float) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False, ""
    if result.returncode not in (0, 1):
        # git status/ls-files use 0 for success; treat any other code as failure.
        return False, ""
    try:
        return True, result.stdout.decode("utf-8", errors="surrogateescape")
    except Exception:
        return False, ""


def git_tri_state(rel_path: str, project_root: str, timeout: float = 5.0) -> tuple[str, str, bool]:
    """Returns (tracked_state, ignored_state, ok) for the tree rooted at
    ``rel_path`` (repo-relative POSIX path, no leading ``./``).

    Uses NUL-delimited, argv-array git invocations only (Issue #1417 P0-5).
    On any git failure/timeout returns ``("unknown", "unknown", False)``.
    """
    pathspec = f":(literal){rel_path}"

    ok1, tracked_out = _run_git(
        ["ls-files", "-z", "--cached", "--", pathspec], project_root, timeout
    )
    if not ok1:
        return "unknown", "unknown", False
    tracked_paths = {p for p in tracked_out.split("\0") if p}

    ok2, status_out = _run_git(
        [
            "status", "--porcelain=v1", "-z",
            "--untracked-files=all", "--ignored=matching",
            "--", pathspec,
        ],
        project_root,
        timeout,
    )
    if not ok2:
        return "unknown", "unknown", False

    untracked_paths: set[str] = set()
    ignored_paths: set[str] = set()
    tokens = [t for t in status_out.split("\0") if t]
    for tok in tokens:
        if len(tok) < 4:
            continue
        xy = tok[:2]
        p = tok[3:]
        if xy == "!!":
            ignored_paths.add(p)
        elif xy == "??":
            untracked_paths.add(p)

    all_paths = tracked_paths | untracked_paths | ignored_paths
    if not all_paths:
        return "none", "none", True

    if len(tracked_paths) == 0:
        tracked_state = "none"
    elif tracked_paths == all_paths:
        tracked_state = "all"
    else:
        tracked_state = "some"

    if len(ignored_paths) == 0:
        ignored_state = "none"
    elif ignored_paths == all_paths:
        ignored_state = "all"
    else:
        ignored_state = "some"

    return tracked_state, ignored_state, True


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


def _bounded_walk_flags(
    dir_abs_path: str, root_dev: int, budget: ScanBudget
) -> tuple[bool, bool, bool]:
    """Bounded, non-following walk of ``dir_abs_path``.

    Returns (nested_symlink_present, special_file_present,
    mount_boundary_crossed). Stops early (marking scan incomplete via the
    budget) once max_entries / max_depth / deadline is hit; callers must
    check ``budget`` state after calling this.
    """
    nested_symlink = False
    special_file = False
    mount_crossed = False
    stack: list[tuple[str, int]] = [(dir_abs_path, 0)]
    while stack:
        if budget.deadline_exceeded() or budget.entries_exceeded():
            break
        current, depth = stack.pop()
        if depth > budget.limits.max_depth:
            continue
        try:
            with os.scandir(current) as it:
                for de in it:
                    if budget.deadline_exceeded() or budget.entries_exceeded():
                        break
                    budget.entries_seen += 1
                    try:
                        is_link = de.is_symlink()
                    except OSError:
                        special_file = True
                        continue
                    if is_link:
                        nested_symlink = True
                        continue
                    try:
                        child_st = de.stat(follow_symlinks=False)
                    except OSError:
                        special_file = True
                        continue
                    if child_st.st_dev != root_dev:
                        mount_crossed = True
                        continue
                    if stat.S_ISDIR(child_st.st_mode):
                        stack.append((de.path, depth + 1))
                    elif not stat.S_ISREG(child_st.st_mode):
                        special_file = True
        except OSError:
            special_file = True
    return nested_symlink, special_file, mount_crossed


@dataclass
class ClassifyContext:
    project_root: str
    limits: ScanLimits
    current_session_id: str | None
    repository: str | None
    now: datetime
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
) -> dict:
    reason_codes_sorted = sorted(set(reason_codes), key=_priority_key)
    primary = reason_codes_sorted[0] if reason_codes_sorted else "unclassified"
    force_report_only = primary != "owned_session_eligible"
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


def _observation_from_lstat(st: os.stat_result | None) -> dict:
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
    abs_root = os.path.join(ctx.project_root, root_rel)

    try:
        root_st = os.lstat(abs_root)
    except FileNotFoundError:
        return entries
    except OSError as exc:
        ctx.errors.append({"reason_code": "scan_error", "message": str(exc), "path": root_rel})
        return entries

    root_dev = root_st.st_dev
    root_is_symlink = stat.S_ISLNK(root_st.st_mode)
    root_entry_type = classify_entry_type(root_st, root_is_symlink)
    tracked_state, ignored_state, git_ok = git_tri_state(root_rel, ctx.project_root)
    reason_codes = ["root_itself"]
    if root_is_symlink:
        reason_codes.append("top_level_symlink")
    if not git_ok:
        reason_codes.append("git_state_unknown")
        tracked_state, ignored_state = "unknown", "unknown"
    entries.append(
        _make_entry(
            root_rel, folder_class, root_entry_type, tracked_state, ignored_state,
            dict(_ABSENT_MARKER), reason_codes, _observation_from_lstat(root_st),
        )
    )

    if root_is_symlink or root_entry_type != "directory":
        return entries

    try:
        children = sorted(os.scandir(abs_root), key=lambda d: d.name)
    except OSError as exc:
        ctx.errors.append({"reason_code": "scan_error", "message": str(exc), "path": root_rel})
        ctx.scan_incomplete = True
        return entries

    for child in children:
        if budget.deadline_exceeded() or budget.entries_exceeded():
            ctx.scan_incomplete = True
            ctx.errors.append(
                {"reason_code": "scan_limit_exceeded", "message": "bounded scan stopped early", "path": root_rel}
            )
            break
        budget.entries_seen += 1
        child_rel = f"{root_rel}/{child.name}"
        entries.append(classify_child(child, child_rel, folder_class, ctx, budget, root_dev))

    return entries


def classify_child(
    child: os.DirEntry,
    child_rel: str,
    folder_class: str,
    ctx: ClassifyContext,
    budget: ScanBudget,
    root_dev: int,
) -> dict:
    try:
        is_symlink = child.is_symlink()
    except OSError:
        is_symlink = False
    try:
        child_st = child.stat(follow_symlinks=False)
    except OSError as exc:
        ctx.errors.append({"reason_code": "scan_error", "message": str(exc), "path": child_rel})
        return _make_entry(
            child_rel, folder_class, "unknown", "unknown", "unknown",
            dict(_ABSENT_MARKER), ["scan_incomplete"], {"device": None, "inode": None, "mtime_ns": None},
        )

    entry_type = classify_entry_type(child_st, is_symlink)
    tracked_state, ignored_state, git_ok = git_tri_state(child_rel, ctx.project_root)
    observation = _observation_from_lstat(child_st)

    reason_codes: list[str] = []
    if not git_ok:
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
        )

    # FOLDER_CLASS_APPROVED path: session-directory eligibility evaluation.
    if is_symlink:
        reason_codes.append("top_level_symlink")
        return _make_entry(
            child_rel, folder_class, entry_type, tracked_state, ignored_state,
            dict(_ABSENT_MARKER), reason_codes, observation,
        )
    if entry_type != "directory":
        reason_codes.append("not_a_session_directory")
        return _make_entry(
            child_rel, folder_class, entry_type, tracked_state, ignored_state,
            dict(_ABSENT_MARKER), reason_codes, observation,
        )
    if child_st.st_dev != root_dev:
        reason_codes.append("mount_boundary_crossed")
        return _make_entry(
            child_rel, folder_class, entry_type, tracked_state, ignored_state,
            dict(_ABSENT_MARKER), reason_codes, observation,
        )

    nested_symlink, special_file, mount_crossed = _bounded_walk_flags(child.path, root_dev, budget)
    if budget.deadline_exceeded() or budget.entries_exceeded():
        ctx.scan_incomplete = True
        reason_codes.append("scan_incomplete")
    if nested_symlink:
        reason_codes.append("nested_symlink_present")
    if special_file:
        reason_codes.append("special_file_present")
    if mount_crossed:
        reason_codes.append("mount_boundary_crossed")
    if tracked_state not in ("none", "unknown"):
        reason_codes.append("tracked_content_present")

    marker_path = os.path.join(child.path, MARKER_FILENAME)
    marker_result = evaluate_marker(
        marker_path,
        current_session_id=ctx.current_session_id,
        expected_target_relpath=child_rel,
        expected_repository=ctx.repository,
        now=ctx.now,
        max_bytes=ctx.limits.max_marker_bytes,
    )

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
            reason_codes.append("marker_mismatch")

    return _make_entry(
        child_rel, folder_class, entry_type, tracked_state, ignored_state,
        marker_dict, reason_codes, observation,
    )


def _discover_denied_alias_roots(project_root: str) -> list[str]:
    roots = list(DENIED_ALIAS_EXACT)
    try:
        with os.scandir(project_root) as it:
            for de in it:
                if de.name in DENIED_ALIAS_EXACT:
                    continue
                if fnmatch.fnmatchcase(de.name, DENIED_ALIAS_GLOB) and "/" not in de.name:
                    roots.append(de.name)
    except OSError:
        pass
    return sorted(set(roots))


def resolve_repository_slug(project_root: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=5,
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


def run_classification(
    project_root_arg: str | None,
    limits: ScanLimits,
    current_session_id: str | None,
) -> dict:
    project_root, source = resolve_project_root(project_root_arg)
    now = datetime.now(timezone.utc)
    ctx = ClassifyContext(
        project_root=project_root,
        limits=limits,
        current_session_id=current_session_id,
        repository=resolve_repository_slug(project_root),
        now=now,
    )
    import time

    budget = ScanBudget(limits=limits, start_monotonic=time.monotonic())

    entries: list[dict] = []
    for root_rel in APPROVED_ROOTS:
        entries.extend(classify_root(root_rel, FOLDER_CLASS_APPROVED, ctx, budget))
    for root_rel in _discover_denied_alias_roots(project_root):
        entries.extend(classify_root(root_rel, FOLDER_CLASS_ALIAS, ctx, budget))

    scan_status = "ok"
    if ctx.errors:
        scan_status = "partial" if ctx.scan_incomplete else "error"
    elif ctx.scan_incomplete:
        scan_status = "partial"

    return {
        "schema": SCHEMA_ID,
        "scan_status": scan_status,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "project_root": {"source": source, "validated": True},
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
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(_emit_yaml(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
