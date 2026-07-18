#!/usr/bin/env python3
"""Controlled stage/commit executor (Issue #1611).

This module is the ONE trusted, in-process boundary that stages and
commits agent-driven changes with literal, explicit pathspecs. It never
executes a shell string like `rtk git add ...` / `git add ...` itself --
callers that need staging/commit MUST go through
`execute_controlled_change()` (or the CLI entrypoint below), never a raw
`git add` / `git commit` shell command (see `git_mutation_command_policy.py`
`classify_agent_lane_add_commit`, Issue #1611 AC9, which denies exactly
those shell shapes at the PreToolUse layer).

Within a single call, this executor:

  1. Verifies repository / worktree / branch / HEAD binding against an
     `IssueScopeSnapshot` (and, optionally, freshness against a live
     re-read of the Issue body / Allowed Paths hashes -- stale snapshot
     detection, AC8).
  2. Rejects any pathspec containing git pathspec magic (`:(...)`, glob
     characters) or that resolves to an existing directory (AC6) --
     every pathspec must be a literal, explicit file path.
  3. Stages the (already-validated) literal pathspecs with a single
     `git add -- <paths...>` call.
  4. Re-reads the index via `git diff --cached --name-status -M -z`
     (never the rename-unaware `--name-only`) through the SAME parser
     `allowed_paths_review_gate.py` uses (`changed_file_matcher.py`,
     Issue #1611 AC11), so rename old/new paths, deletions, and type
     changes are all explicitly classified (AC3/AC4), and detects
     submodule gitlink changes via a secondary `--raw` read.
  5. Checks every audited path (current + previous, for renames) against
     `protected_paths_policy.py` (always-deny, AC10) and the snapshot's
     Allowed Paths (AC2/AC3/AC4).
  6. Confirms the staged path set exactly equals the requested path set
     (AC7) -- any drift (e.g. leftover pre-existing staged content) is
     fail-closed denied.
  7. Commits, then re-audits the committed diff; if the post-commit audit
     ever disagrees with the pre-commit audit, the commit is rolled back
     (`git reset --soft HEAD^`) and denied.

Concurrency note (Issue #1611 In Scope): this executor narrows, but does
NOT eliminate, the stage-to-commit race window. It re-checks local HEAD
immediately before staging and immediately before commit (deny on any
drift), and requires the index to be empty of pre-existing staged changes
before it stages anything -- but it uses the repository's normal
`$GIT_DIR/index`, not a private `GIT_INDEX_FILE` + `git update-ref`
compare-and-swap. A fully isolated, index-file-swapping transaction was
judged excess for the current single-agent-per-worktree operating model;
if multiple controlled executor invocations can ever run concurrently
against the SAME worktree, a residual race remains between the
pre-commit HEAD re-check and the actual `git commit` call. This is a
known, documented limitation (see `docs/dev/agent-runtime-ops.md`), not
an oversight.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_AGENT_GUARDS_DIR = Path(__file__).resolve().parent
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))

from changed_file_matcher import (  # noqa: E402
    SOURCE_GIT_NAME_STATUS_Z,
    AllowedPathsMatcher,
    ChangedFileRecord,
    parse_git_diff_name_status_z,
)
import protected_paths_policy  # noqa: E402

SCHEMA_VERSION = "ISSUE_SCOPE_SNAPSHOT_V1"

# ─── Authority version state machine (Issue #1611 AC12) ─────────────────────

AUTHORITY_OLD_ONLY = "old_only"
AUTHORITY_MIGRATION_VALIDATION = "migration_validation"
AUTHORITY_NEW_ONLY = "new_only"
AUTHORITY_ROLLBACK_TO_OLD = "rollback_to_old"
AUTHORITY_VERSION_STATES = frozenset(
    {AUTHORITY_OLD_ONLY, AUTHORITY_MIGRATION_VALIDATION, AUTHORITY_NEW_ONLY, AUTHORITY_ROLLBACK_TO_OLD}
)

AUTHORITY_SOURCE_LEGACY_ENV = "legacy_codex_allowed_paths_env"
AUTHORITY_SOURCE_SNAPSHOT = "issue_scope_snapshot"
AUTHORITY_SOURCE_NONE = "none"


@dataclass(frozen=True)
class AuthorityResolution:
    authoritative_source: str
    authority_version: str
    reason_code: str


def resolve_authority(
    *,
    authority_version: str,
    legacy_allowed_paths_env: Optional[str] = None,
    snapshot: Optional["IssueScopeSnapshot"] = None,
) -> AuthorityResolution:
    """Pure decision core (Issue #1611 AC12): resolve which single source
    (`legacy_codex_allowed_paths_env` or `issue_scope_snapshot`) drives the
    actual allow/deny decision for a given `authority_version` state.

    `authoritative_source` is purely a function of `authority_version` --
    it never depends on which of `legacy_allowed_paths_env` / `snapshot`
    happen to be present, so the two inputs can never be blended or
    simultaneously authoritative, no matter what combination of
    (legacy_present, snapshot_present) a caller supplies. During
    `migration_validation`, the snapshot is expected to be computed and
    compared in parallel by the caller for observability, but this
    function never marks it authoritative for that state -- the legacy
    env stays the sole enforcement source until the state advances to
    `new_only`.
    """
    del legacy_allowed_paths_env, snapshot  # inputs are audit-only, never gate selection
    if authority_version not in AUTHORITY_VERSION_STATES:
        return AuthorityResolution(AUTHORITY_SOURCE_NONE, authority_version, "unknown_authority_version")
    if authority_version == AUTHORITY_OLD_ONLY:
        return AuthorityResolution(AUTHORITY_SOURCE_LEGACY_ENV, authority_version, "old_only_legacy_authoritative")
    if authority_version == AUTHORITY_ROLLBACK_TO_OLD:
        return AuthorityResolution(
            AUTHORITY_SOURCE_LEGACY_ENV, authority_version, "rollback_to_old_legacy_authoritative"
        )
    if authority_version == AUTHORITY_MIGRATION_VALIDATION:
        return AuthorityResolution(
            AUTHORITY_SOURCE_LEGACY_ENV,
            authority_version,
            "migration_validation_legacy_authoritative_snapshot_audit_only",
        )
    return AuthorityResolution(AUTHORITY_SOURCE_SNAPSHOT, authority_version, "new_only_snapshot_authoritative")


# ─── Issue scope snapshot (Issue #1611 AC1) ──────────────────────────────────


@dataclass(frozen=True)
class IssueScopeSnapshot:
    """ISSUE_SCOPE_SNAPSHOT_V1: a verified, local/private artifact binding a
    single controlled-executor invocation to the Issue contract it was
    authorized against."""

    schema_version: str
    issue_number: int
    body_sha256: str
    allowed_paths: Tuple[str, ...]
    allowed_paths_normalized_sha256: str
    base_branch: str
    base_sha: str
    target_branch: str
    worktree_realpath: str
    protected_paths_policy_version: str
    authority_version: str
    generated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "issue_number": self.issue_number,
            "body_sha256": self.body_sha256,
            "allowed_paths": list(self.allowed_paths),
            "allowed_paths_normalized_sha256": self.allowed_paths_normalized_sha256,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "target_branch": self.target_branch,
            "worktree_realpath": self.worktree_realpath,
            "protected_paths_policy_version": self.protected_paths_policy_version,
            "authority_version": self.authority_version,
            "generated_at": self.generated_at,
        }


def _canonicalize_allowed_paths(allowed_paths: List[str]) -> List[str]:
    canonicalized: List[str] = []
    for pattern in allowed_paths:
        normalized = AllowedPathsMatcher.normalize_allowed_pattern(pattern)
        if normalized is None:
            raise ValueError(f"invalid allowed path pattern: {pattern!r}")
        canonicalized.append(normalized)
    return sorted(set(canonicalized))


def compute_allowed_paths_sha256(allowed_paths: List[str]) -> str:
    canonical = _canonicalize_allowed_paths(allowed_paths)
    normalized_json = json.dumps(canonical, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(normalized_json.encode()).hexdigest()


def build_issue_scope_snapshot(
    *,
    issue_number: int,
    issue_body: str,
    allowed_paths: List[str],
    base_branch: str,
    base_sha: str,
    target_branch: str,
    worktree_path: str,
    authority_version: str = AUTHORITY_NEW_ONLY,
) -> IssueScopeSnapshot:
    """Build a verified ISSUE_SCOPE_SNAPSHOT_V1 (Issue #1611 AC1). Binds:
    issue body_sha256, Allowed Paths normalized sha256, base branch/sha,
    worktree realpath, and protected_paths_policy_version."""
    if authority_version not in AUTHORITY_VERSION_STATES:
        raise ValueError(f"unknown authority_version: {authority_version!r}")
    return IssueScopeSnapshot(
        schema_version=SCHEMA_VERSION,
        issue_number=issue_number,
        body_sha256=hashlib.sha256(issue_body.encode("utf-8")).hexdigest(),
        allowed_paths=tuple(allowed_paths),
        allowed_paths_normalized_sha256=compute_allowed_paths_sha256(allowed_paths),
        base_branch=base_branch,
        base_sha=base_sha,
        target_branch=target_branch,
        worktree_realpath=os.path.realpath(worktree_path),
        protected_paths_policy_version=protected_paths_policy.POLICY_VERSION,
        authority_version=authority_version,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


def detect_stale_snapshot(
    snapshot: IssueScopeSnapshot,
    *,
    current_body_sha256: Optional[str] = None,
    current_allowed_paths_sha256: Optional[str] = None,
) -> Optional[str]:
    """Return a reason_code if `snapshot` is stale relative to a freshly
    supplied readback, else None (Issue #1611 AC8, first half)."""
    if current_body_sha256 is not None and current_body_sha256 != snapshot.body_sha256:
        return "stale_snapshot_body_drift"
    if (
        current_allowed_paths_sha256 is not None
        and current_allowed_paths_sha256 != snapshot.allowed_paths_normalized_sha256
    ):
        return "stale_snapshot_allowed_paths_drift"
    return None


# ─── Pathspec literal validation (Issue #1611 AC5/AC6) ──────────────────────

_PATHSPEC_MAGIC_CHARS = frozenset("*?[]")
_PATHSPEC_BROAD_ROOTS = frozenset({".", "..", ":/", "/"})


def _validate_pathspec_literal(pathspec: str, cwd: str) -> Tuple[bool, Optional[str]]:
    """Reject pathspec magic (`:(top)`, `:(exclude)`, glob characters) and
    directory pathspecs (Issue #1611 AC6). Every pathspec must be a
    literal, explicit file path."""
    if not pathspec:
        return False, "pathspec_empty"
    if pathspec in _PATHSPEC_BROAD_ROOTS:
        return False, "pathspec_broad_root_rejected"
    if pathspec.startswith(":"):
        return False, "pathspec_magic_rejected"
    if any(ch in pathspec for ch in _PATHSPEC_MAGIC_CHARS):
        return False, "pathspec_magic_rejected"
    if pathspec.startswith("/"):
        return False, "pathspec_absolute_rejected"
    if pathspec.startswith("-"):
        return False, "pathspec_leading_dash_rejected"
    # NOTE: intentionally NOT `os.path.realpath` -- that dereferences
    # symlinks, which would misclassify a tracked symlink pathspec (e.g. a
    # type-changed file that is now a symlink) as pointing at whatever
    # directory the symlink target happens to resolve to. `os.path.islink`
    # is checked first so a symlink is never treated as a directory
    # pathspec purely because its target happens to be one.
    joined = os.path.join(cwd, pathspec)
    if not os.path.islink(joined) and os.path.isdir(joined):
        return False, "pathspec_directory_rejected"
    return True, None


def _pathspec_to_repo_relative(pathspec: str, cwd: str, repo_root: str) -> Optional[str]:
    # NOTE: intentionally NOT `Path.resolve()` -- that dereferences
    # symlinks along the path, which would silently rewrite a symlink
    # pathspec into whatever path its target string happens to name. Only
    # `..`/`.` lexical segments are normalized (via os.path.normpath),
    # never actual filesystem symlink targets.
    joined_cwd = os.path.normpath(os.path.abspath(cwd))
    candidate = os.path.normpath(os.path.join(joined_cwd, pathspec))
    repo_path = os.path.normpath(os.path.abspath(repo_root))
    if candidate == repo_path:
        return None
    prefix = repo_path + os.sep
    if not candidate.startswith(prefix):
        return None
    relative = candidate[len(prefix) :]
    return AllowedPathsMatcher.normalize_path(Path(relative).as_posix())


# ─── git subprocess helpers ──────────────────────────────────────────────────


def _run_git(args: List[str], cwd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def _current_branch(cwd: str) -> Optional[str]:
    result = _run_git(["branch", "--show-current"], cwd)
    branch = result.stdout.strip()
    return branch or None


def _current_head(cwd: str) -> Optional[str]:
    result = _run_git(["rev-parse", "HEAD"], cwd)
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head or None


def _git_toplevel(cwd: str) -> Optional[str]:
    result = _run_git(["rev-parse", "--show-toplevel"], cwd)
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return root or None


def _staged_name_status(cwd: str) -> Tuple[bool, str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-status", "-M", "-z"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0, result.stdout


def _staged_name_only_empty(cwd: str) -> Optional[bool]:
    result = _run_git(["diff", "--cached", "--name-only"], cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip() == ""


_GITLINK_MODE = "160000"


def _detect_gitlink_paths(cwd: str) -> set:
    """Detect staged submodule (gitlink) changes via a secondary `--raw`
    read (mode `160000`), so submodule changes are explicitly classified
    even though `--name-status` alone cannot distinguish them (Issue #1611
    AC4)."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--raw", "-M", "-z"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return set()
    tokens = [tok for tok in result.stdout.split("\0") if tok != ""]
    gitlinks: set = set()
    i = 0
    while i < len(tokens):
        meta = tokens[i]
        i += 1
        parts = meta.split(" ")
        if len(parts) < 5:
            continue
        old_mode = parts[0].lstrip(":")
        new_mode = parts[1]
        status_field = parts[4]
        status_letter = status_field[0] if status_field else ""
        is_gitlink = old_mode == _GITLINK_MODE or new_mode == _GITLINK_MODE
        if status_letter in ("R", "C"):
            if i + 1 >= len(tokens):
                break
            _old_path, new_path = tokens[i], tokens[i + 1]
            i += 2
            if is_gitlink:
                gitlinks.add(new_path)
        else:
            if i >= len(tokens):
                break
            path = tokens[i]
            i += 1
            if is_gitlink:
                gitlinks.add(path)
    return gitlinks


# ─── Controlled change result ────────────────────────────────────────────────


@dataclass(frozen=True)
class ControlledChangeResult:
    status: str  # "committed" | "denied"
    reason_code: str
    commit_sha: Optional[str] = None
    staged_paths: Tuple[str, ...] = ()
    requested_paths: Tuple[str, ...] = ()
    classified_records: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    denied_paths: Tuple[str, ...] = ()
    protected_paths_hit: Tuple[str, ...] = ()
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "commit_sha": self.commit_sha,
            "staged_paths": list(self.staged_paths),
            "requested_paths": list(self.requested_paths),
            "classified_records": list(self.classified_records),
            "denied_paths": list(self.denied_paths),
            "protected_paths_hit": list(self.protected_paths_hit),
            "detail": self.detail,
        }


def _denied(reason_code: str, *, detail: Optional[str] = None, **kwargs: Any) -> ControlledChangeResult:
    return ControlledChangeResult(status="denied", reason_code=reason_code, detail=detail, **kwargs)


def _staged_matches_requested(staged_comparison_set: set, requested_set: set) -> bool:
    """Pure comparison core for the staged/requested equivalence check
    (Issue #1611 AC7). Extracted so the mismatch decision itself is
    directly unit-testable, independent of any particular git scenario
    that would produce a mismatched `staged_comparison_set`."""
    return staged_comparison_set == requested_set


def _unstage(cwd: str, pathspecs: List[str]) -> None:
    if not pathspecs:
        return
    subprocess.run(
        ["git", "reset", "--quiet", "--", *pathspecs],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def execute_controlled_change(
    *,
    cwd: str,
    snapshot: IssueScopeSnapshot,
    requested_pathspecs: List[str],
    commit_message: str,
    expected_head: Optional[str] = None,
    current_body_sha256: Optional[str] = None,
    current_allowed_paths_sha256: Optional[str] = None,
) -> ControlledChangeResult:
    """Single-transaction stage -> classify -> audit -> commit -> re-audit
    boundary (Issue #1611 In Scope). See module docstring for the full
    step sequence."""
    # 1. Stale snapshot detection (AC8, first half) -- checked before any
    # repository state is touched.
    stale_reason = detect_stale_snapshot(
        snapshot,
        current_body_sha256=current_body_sha256,
        current_allowed_paths_sha256=current_allowed_paths_sha256,
    )
    if stale_reason is not None:
        return _denied(stale_reason)

    if not commit_message or not commit_message.strip():
        return _denied("commit_message_required")

    # 2. Repository / worktree / branch / HEAD binding (AC8, second half).
    repo_root = _git_toplevel(cwd)
    if repo_root is None:
        return _denied("repository_binding_unavailable")
    if os.path.realpath(repo_root) != snapshot.worktree_realpath:
        return _denied("worktree_binding_mismatch")

    current_branch = _current_branch(cwd)
    if not current_branch or current_branch != snapshot.target_branch:
        return _denied("branch_binding_mismatch")

    local_head = _current_head(cwd)
    if expected_head is not None and local_head != expected_head:
        return _denied("head_race_detected")

    # 3. Requested pathspecs must be literal (no magic, no directories).
    if not requested_pathspecs:
        return _denied("no_pathspecs_requested")

    normalized_requested: List[str] = []
    for pathspec in requested_pathspecs:
        is_valid, reason = _validate_pathspec_literal(pathspec, cwd)
        if not is_valid:
            return _denied(reason or "pathspec_rejected", denied_paths=(pathspec,))
        repo_relative = _pathspec_to_repo_relative(pathspec, cwd, repo_root)
        if repo_relative is None:
            return _denied("pathspec_outside_repository", denied_paths=(pathspec,))
        normalized_requested.append(repo_relative)

    requested_set = set(normalized_requested)

    # 4. Index must be empty of pre-existing staged changes before we
    # stage anything -- otherwise a "staged == requested" comparison could
    # silently absorb unrelated pre-staged content (AC7).
    index_clean = _staged_name_only_empty(cwd)
    if index_clean is None:
        return _denied("index_state_unavailable")
    if not index_clean:
        return _denied("index_not_clean_before_stage")

    # 5. Stage with a single literal `git add -- <paths...>` call.
    add_result = subprocess.run(
        ["git", "add", "--", *requested_pathspecs],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if add_result.returncode != 0:
        _unstage(cwd, requested_pathspecs)
        return _denied("git_add_failed", detail=add_result.stderr.strip())

    # 6. Re-read the index via the rename-aware, NUL-delimited grammar
    # shared with allowed_paths_review_gate.py (AC3/AC5/AC11).
    read_ok, raw_stdout = _staged_name_status(cwd)
    if not read_ok:
        _unstage(cwd, requested_pathspecs)
        return _denied("index_reread_failed")
    try:
        records: List[ChangedFileRecord] = parse_git_diff_name_status_z(raw_stdout, source=SOURCE_GIT_NAME_STATUS_Z)
    except ValueError as exc:
        _unstage(cwd, requested_pathspecs)
        return _denied("index_reread_parse_failed", detail=str(exc))

    gitlink_paths = _detect_gitlink_paths(cwd)

    classified_records: List[Dict[str, Any]] = []
    staged_current_paths: set = set()
    staged_previous_paths: set = set()
    audited_paths: List[str] = []
    for record in records:
        staged_current_paths.add(record.path)
        audited_paths.append(record.path)
        if record.previous_path:
            staged_previous_paths.add(record.previous_path)
            audited_paths.append(record.previous_path)
        classified_records.append(
            {
                "path": record.path,
                "previous_path": record.previous_path,
                "git_status": record.status,
                "is_submodule_gitlink_change": record.path in gitlink_paths
                or (record.previous_path in gitlink_paths if record.previous_path else False),
            }
        )

    # 7. Staged set must exactly equal the requested set (AC7).
    staged_comparison_set = staged_current_paths | staged_previous_paths
    if not _staged_matches_requested(staged_comparison_set, requested_set):
        _unstage(cwd, requested_pathspecs)
        return _denied(
            "staged_requested_mismatch",
            staged_paths=tuple(sorted(staged_comparison_set)),
            requested_paths=tuple(sorted(requested_set)),
        )

    # 8. Protected paths always deny, regardless of Allowed Paths (AC10).
    protected_hits = sorted({path for path in audited_paths if protected_paths_policy.is_protected_path(path)})
    if protected_hits:
        _unstage(cwd, requested_pathspecs)
        return _denied(
            "protected_path_denied",
            protected_paths_hit=tuple(protected_hits),
            staged_paths=tuple(sorted(staged_comparison_set)),
            requested_paths=tuple(sorted(requested_set)),
        )

    # 9. Every audited path (current + previous, for renames) must be
    # within the snapshot's Allowed Paths (AC2/AC3/AC4).
    out_of_scope = sorted(
        {
            path
            for path in audited_paths
            if not AllowedPathsMatcher.is_file_allowed(path, list(snapshot.allowed_paths))
        }
    )
    if out_of_scope:
        _unstage(cwd, requested_pathspecs)
        return _denied(
            "path_outside_allowed_paths",
            denied_paths=tuple(out_of_scope),
            staged_paths=tuple(sorted(staged_comparison_set)),
            requested_paths=tuple(sorted(requested_set)),
        )

    # 10. Re-confirm HEAD has not moved since step 2 (narrows, does not
    # eliminate, the stage-to-commit race -- see module docstring).
    if expected_head is not None and _current_head(cwd) != expected_head:
        _unstage(cwd, requested_pathspecs)
        return _denied("head_race_detected_before_commit")

    commit_result = subprocess.run(
        ["git", "commit", "-m", commit_message],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if commit_result.returncode != 0:
        return _denied("commit_failed", detail=commit_result.stderr.strip())

    commit_sha = _current_head(cwd)

    # 11. Post-commit re-audit (defense in depth): re-read the committed
    # diff and confirm no protected/out-of-scope path made it in.
    show_result = subprocess.run(
        ["git", "show", "--name-status", "-M", "-z", "--format=", commit_sha or "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    post_commit_violation = False
    if show_result.returncode == 0:
        try:
            post_records = parse_git_diff_name_status_z(show_result.stdout, source=SOURCE_GIT_NAME_STATUS_Z)
        except ValueError:
            post_commit_violation = True
        else:
            post_audited: List[str] = []
            for record in post_records:
                post_audited.append(record.path)
                if record.previous_path:
                    post_audited.append(record.previous_path)
            for path in post_audited:
                if protected_paths_policy.is_protected_path(path) or not AllowedPathsMatcher.is_file_allowed(
                    path, list(snapshot.allowed_paths)
                ):
                    post_commit_violation = True
                    break
    else:
        post_commit_violation = True

    if post_commit_violation:
        subprocess.run(
            ["git", "reset", "--soft", "HEAD^"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        _unstage(cwd, requested_pathspecs)
        return _denied("post_commit_audit_violation_rolled_back")

    return ControlledChangeResult(
        status="committed",
        reason_code="committed",
        commit_sha=commit_sha,
        staged_paths=tuple(sorted(staged_comparison_set)),
        requested_paths=tuple(sorted(requested_set)),
        classified_records=tuple(classified_records),
    )


# ─── CLI entrypoint ───────────────────────────────────────────────────────────


def _build_cli_parser():
    import argparse

    parser = argparse.ArgumentParser(
        description="Controlled stage/commit executor: the single authorized path for "
        "agent-driven git staging and commits (Issue #1611)."
    )
    parser.add_argument("--cwd", required=True, help="worktree directory to operate in")
    parser.add_argument("--snapshot-json", required=True, help="path to a JSON file with the IssueScopeSnapshot fields")
    parser.add_argument("--path", action="append", dest="paths", default=[], help="explicit pathspec to stage (repeatable)")
    parser.add_argument("--message", required=True, help="commit message")
    parser.add_argument("--expected-head", default=None, help="expected local HEAD SHA (race guard)")
    return parser


def _main(argv: Optional[List[str]] = None) -> int:
    args = _build_cli_parser().parse_args(argv if argv is not None else sys.argv[1:])
    with open(args.snapshot_json, encoding="utf-8") as fh:
        snapshot_data = json.load(fh)
    snapshot = IssueScopeSnapshot(
        schema_version=snapshot_data.get("schema_version", SCHEMA_VERSION),
        issue_number=snapshot_data["issue_number"],
        body_sha256=snapshot_data["body_sha256"],
        allowed_paths=tuple(snapshot_data["allowed_paths"]),
        allowed_paths_normalized_sha256=snapshot_data["allowed_paths_normalized_sha256"],
        base_branch=snapshot_data["base_branch"],
        base_sha=snapshot_data["base_sha"],
        target_branch=snapshot_data["target_branch"],
        worktree_realpath=snapshot_data["worktree_realpath"],
        protected_paths_policy_version=snapshot_data["protected_paths_policy_version"],
        authority_version=snapshot_data["authority_version"],
        generated_at=snapshot_data.get("generated_at", ""),
    )
    result = execute_controlled_change(
        cwd=args.cwd,
        snapshot=snapshot,
        requested_pathspecs=args.paths,
        commit_message=args.message,
        expected_head=args.expected_head,
    )
    print(json.dumps(result.to_dict()))
    return 0 if result.status == "committed" else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
