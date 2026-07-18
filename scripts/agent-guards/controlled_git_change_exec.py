#!/usr/bin/env python3
"""Controlled stage/commit executor (Issue #1611, contract revision).

This module is the ONE trusted, in-process boundary that stages and
commits agent-driven changes with literal, explicit pathspecs. It never
executes a shell string like `rtk git add ...` / `git add ...` itself --
callers that need staging/commit MUST go through
`execute_controlled_change()` (or the CLI entrypoint below), never a raw
`git add` / `git commit` shell command (see `git_mutation_command_policy.py`
`classify_agent_lane_add_commit`, Issue #1611 AC9, which denies exactly
those shell shapes at the PreToolUse layer).

Transaction design ("案B": `git commit --only` + audit-then-rollback, the
ONLY transaction primitive this executor implements -- a private
`GIT_INDEX_FILE` + `commit-tree` + `update-ref` compare-and-swap ("案A") is
explicitly Out of Scope):

  1. Live/stale-snapshot checks (issue body / comments digest / Allowed
     Paths hash drift -- AC8) and repository / worktree / branch / HEAD
     binding, detached-HEAD / unborn-branch / in-progress-merge-rebase-
     cherry-pick / unmerged-index fail-closed guards.
  2. Every requested pathspec is validated literal (no pathspec magic, no
     directory pathspec -- AC6) and mapped to a repo-relative path.
  3. A BASELINE pre-existing-staged-content snapshot is taken via
     `git diff-index --cached --raw --full-index -z -M <EXPECTED_HEAD>`
     (bytes, NUL-delimited -- AC5) BEFORE anything is staged, so pre-
     existing unrelated staged content never counts as "ours".
  4. Stage with `git --literal-pathspecs add --pathspec-from-file=-
     --pathspec-file-nul` (stdin = NUL-joined literal pathspecs, bytes).
  5. Re-run the SAME `git diff-index --cached --raw --full-index -z -M
     <EXPECTED_HEAD>` oracle; the DELTA (paths present now that were not
     in the baseline) must exactly equal the requested path set (AC7) --
     rename old/new paths, deletions, type changes, and submodule gitlink
     changes (mode `160000`) are all explicitly classified from this same
     oracle (AC3/AC4), never the rename-unaware `--name-only` form.
  6. Every delta path (current + previous, for renames) is checked against
     `protected_paths_policy.py` (always-deny, AC10) and the snapshot's
     Allowed Paths.
  7. Commits with `git commit --only --pathspec-from-file=- --pathspec-
     file-nul -m <message>` -- `--only` restricts the commit to exactly the
     given pathspecs, so any pre-existing unrelated staged content (that
     survived the delta check because commit --only never touches it) is
     never swept into the commit.
  8. Post-commit re-audit: `git diff-tree --no-commit-id -r --raw --full-
     index -z -M <commit_sha>` (diffs the new commit's tree against its
     parent's tree directly -- NOT `diff-index` against the working tree,
     which would also see unrelated pre-existing staged/unstaged drift and
     produce false-positive rollbacks). If the audited commit content
     disagrees with the pre-commit delta, the commit is immediately rolled
     back via `git reset --soft HEAD~1` and denied (AC7).

Concurrency note (Issue #1611 In Scope): this executor narrows, but does
NOT eliminate, the stage-to-commit race window. It re-checks local HEAD
immediately before staging and immediately before commit (deny on any
drift), and diffs against a captured baseline rather than requiring a
clean index -- but it uses the repository's normal `$GIT_DIR/index`, not a
private `GIT_INDEX_FILE` + `git update-ref` compare-and-swap. A fully
isolated, index-file-swapping transaction was judged excess for the
current single-agent-per-worktree operating model; if multiple controlled
executor invocations can ever run concurrently against the SAME worktree,
a residual race remains between the pre-commit HEAD re-check and the
actual `git commit` call. This is a known, documented limitation (see
`docs/dev/agent-runtime-ops.md`), not an oversight.

Threat model (Issue #1611 contract revision, Notes for Reviewer): this is a
repository-local, cooperative-agent-workflow guardrail against accidental
Issue-scope overreach and unrelated-change mixing. It is NOT a security
boundary against a malicious co-located process, a candidate that can
rewrite this policy file itself, or an OS-level adversary.
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
from typing import Any, Dict, List, Optional, Sequence, Tuple

_AGENT_GUARDS_DIR = Path(__file__).resolve().parent
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))

from changed_file_matcher import (  # noqa: E402, F401 -- re-exported for AC11 cross-module identity checks
    GITLINK_MODE,
    SOURCE_GIT_DIFF_INDEX_RAW_Z,
    SOURCE_GIT_NAME_STATUS_Z,
    AllowedPathsMatcher,
    ChangedFileRecord,
    UnsupportedPathEncodingError,
    parse_git_diff_index_raw_z,
    parse_git_diff_name_status_z,
)
import protected_paths_policy  # noqa: E402

SCHEMA_VERSION = "ISSUE_SCOPE_SNAPSHOT_V1"
ALLOWED_PATHS_MATCHER_SCHEMA = "ALLOWED_PATHS_MATCHER_V1"

CONTRACT_SOURCE_ISSUE_BODY = "issue_body"
CONTRACT_SOURCE_ISSUE_COMMENT = "issue_comment"
CONTRACT_SOURCE_KINDS = frozenset({CONTRACT_SOURCE_ISSUE_BODY, CONTRACT_SOURCE_ISSUE_COMMENT})

# ─── Authority mode state machine (Issue #1611 AC12, contract revision) ─────

AUTHORITY_OLD_ONLY = "old_only"
AUTHORITY_MIGRATION_VALIDATION = "migration_validation"
AUTHORITY_NEW_ONLY = "new_only"
# Contract revision (P2): replaces the pre-revision `rollback_to_old`
# state/name. `new_disabled_fail_closed` STOPS add/commit outright -- it is
# an emergency-stop state, never an automatic fallback to the legacy env
# authority. Recovery from this state is an explicit, out-of-band release
# rollback procedure (documented in `docs/dev/agent-runtime-ops.md`), not
# something this module performs automatically.
AUTHORITY_NEW_DISABLED_FAIL_CLOSED = "new_disabled_fail_closed"
AUTHORITY_MODE_STATES = frozenset(
    {AUTHORITY_OLD_ONLY, AUTHORITY_MIGRATION_VALIDATION, AUTHORITY_NEW_ONLY, AUTHORITY_NEW_DISABLED_FAIL_CLOSED}
)

AUTHORITY_SOURCE_LEGACY_ENV = "legacy_codex_allowed_paths_env"
AUTHORITY_SOURCE_SNAPSHOT = "issue_scope_snapshot"
AUTHORITY_SOURCE_NONE = "none"


@dataclass(frozen=True)
class AuthorityResolution:
    authoritative_source: str
    authority_mode: str
    reason_code: str


def resolve_authority(
    *,
    authority_mode: str,
    legacy_allowed_paths_env: Optional[str] = None,
    snapshot: Optional["IssueScopeSnapshot"] = None,
) -> AuthorityResolution:
    """Pure decision core (Issue #1611 AC12): resolve which single source
    (`legacy_codex_allowed_paths_env` or `issue_scope_snapshot`) drives the
    actual allow/deny decision for a given `authority_mode` state, or
    `none` when add/commit is stopped outright (`new_disabled_fail_closed`).

    `authoritative_source` is purely a function of `authority_mode` -- it
    never depends on which of `legacy_allowed_paths_env` / `snapshot`
    happen to be present, so the two inputs can never be blended or
    simultaneously authoritative, no matter what combination of
    (legacy_present, snapshot_present) a caller supplies. During
    `migration_validation`, the snapshot is expected to be computed and
    compared in parallel by the caller for observability, but this
    function never marks it authoritative for that state -- the legacy
    env stays the sole enforcement source until the state advances to
    `new_only`. `new_disabled_fail_closed` NEVER resolves to the legacy env
    -- it is an emergency stop, not an automatic fallback."""
    del legacy_allowed_paths_env, snapshot  # inputs are audit-only, never gate selection
    if authority_mode not in AUTHORITY_MODE_STATES:
        return AuthorityResolution(AUTHORITY_SOURCE_NONE, authority_mode, "unknown_authority_mode")
    if authority_mode == AUTHORITY_OLD_ONLY:
        return AuthorityResolution(AUTHORITY_SOURCE_LEGACY_ENV, authority_mode, "old_only_legacy_authoritative")
    if authority_mode == AUTHORITY_NEW_DISABLED_FAIL_CLOSED:
        return AuthorityResolution(
            AUTHORITY_SOURCE_NONE,
            authority_mode,
            "new_disabled_fail_closed_add_commit_stopped_no_auto_fallback",
        )
    if authority_mode == AUTHORITY_MIGRATION_VALIDATION:
        return AuthorityResolution(
            AUTHORITY_SOURCE_LEGACY_ENV,
            authority_mode,
            "migration_validation_legacy_authoritative_snapshot_audit_only",
        )
    return AuthorityResolution(AUTHORITY_SOURCE_SNAPSHOT, authority_mode, "new_only_snapshot_authoritative")


# ─── Issue scope snapshot (Issue #1611 AC1, contract revision) ──────────────


@dataclass(frozen=True)
class IssueScopeSnapshot:
    """ISSUE_SCOPE_SNAPSHOT_V1: a verified, local/private artifact binding a
    single controlled-executor invocation to the Issue contract, repository,
    branch, and protected-paths policy it was authorized against."""

    schema_version: str
    repository_full_name: str
    issue_number: int
    contract_source_kind: str
    contract_source_id: str
    contract_source_body_sha256: str
    issue_body_sha256: str
    issue_updated_at: str
    comments_digest_sha256: str
    allowed_paths: Tuple[str, ...]
    allowed_paths_normalized_sha256: str
    allowed_paths_matcher_schema: str
    base_ref: str
    base_sha: str
    branch_ref: str
    worktree_realpath: str
    protected_paths_policy_schema: str
    protected_paths_policy_sha256: str
    authority_mode: str
    generated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repository_full_name": self.repository_full_name,
            "issue_number": self.issue_number,
            "contract_source_kind": self.contract_source_kind,
            "contract_source_id": self.contract_source_id,
            "contract_source_body_sha256": self.contract_source_body_sha256,
            "issue_body_sha256": self.issue_body_sha256,
            "issue_updated_at": self.issue_updated_at,
            "comments_digest_sha256": self.comments_digest_sha256,
            "allowed_paths": list(self.allowed_paths),
            "allowed_paths_normalized_sha256": self.allowed_paths_normalized_sha256,
            "allowed_paths_matcher_schema": self.allowed_paths_matcher_schema,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "branch_ref": self.branch_ref,
            "worktree_realpath": self.worktree_realpath,
            "protected_paths_policy_schema": self.protected_paths_policy_schema,
            "protected_paths_policy_sha256": self.protected_paths_policy_sha256,
            "authority_mode": self.authority_mode,
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


def compute_comments_digest_sha256(comment_bodies: Sequence[str]) -> str:
    """sha256 over the ordered list of comment bodies (Issue #1611 AC1/AC8).
    Order-sensitive (comment edit history / ordering IS part of the
    contract) and content-sensitive (any edit to any comment changes the
    digest -- comment drift, not just count drift, must be detectable)."""
    normalized_json = json.dumps(list(comment_bodies), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(normalized_json.encode()).hexdigest()


def build_issue_scope_snapshot(
    *,
    repository_full_name: str,
    issue_number: int,
    contract_source_kind: str,
    contract_source_id: str,
    contract_source_body: str,
    issue_body: str,
    issue_updated_at: str,
    comment_bodies: Sequence[str],
    allowed_paths: List[str],
    base_ref: str,
    base_sha: str,
    branch_ref: str,
    worktree_path: str,
    authority_mode: str = AUTHORITY_NEW_ONLY,
) -> IssueScopeSnapshot:
    """Build a verified ISSUE_SCOPE_SNAPSHOT_V1 (Issue #1611 AC1, contract
    revision). `issue_body` / `issue_updated_at` / `comment_bodies` MUST come
    from a live GitHub readback performed by the caller immediately before
    this call -- this function does not itself perform network I/O, but it
    fails closed (raises `ValueError`) if the readback evidence
    (`issue_body`, `issue_updated_at`) is missing, so a snapshot can never
    be silently built from stale/absent readback data."""
    if authority_mode not in AUTHORITY_MODE_STATES:
        raise ValueError(f"unknown authority_mode: {authority_mode!r}")
    if contract_source_kind not in CONTRACT_SOURCE_KINDS:
        raise ValueError(f"unknown contract_source_kind: {contract_source_kind!r}")
    if not issue_body:
        raise ValueError("github_live_readback_required: issue_body must be supplied from a live GitHub readback")
    if not issue_updated_at:
        raise ValueError(
            "github_live_readback_required: issue_updated_at must be supplied from a live GitHub readback"
        )
    if not contract_source_id:
        raise ValueError("contract_source_id is required")
    return IssueScopeSnapshot(
        schema_version=SCHEMA_VERSION,
        repository_full_name=repository_full_name,
        issue_number=issue_number,
        contract_source_kind=contract_source_kind,
        contract_source_id=contract_source_id,
        contract_source_body_sha256=hashlib.sha256(contract_source_body.encode("utf-8")).hexdigest(),
        issue_body_sha256=hashlib.sha256(issue_body.encode("utf-8")).hexdigest(),
        issue_updated_at=issue_updated_at,
        comments_digest_sha256=compute_comments_digest_sha256(comment_bodies),
        allowed_paths=tuple(allowed_paths),
        allowed_paths_normalized_sha256=compute_allowed_paths_sha256(allowed_paths),
        allowed_paths_matcher_schema=ALLOWED_PATHS_MATCHER_SCHEMA,
        base_ref=base_ref,
        base_sha=base_sha,
        branch_ref=branch_ref,
        worktree_realpath=os.path.realpath(worktree_path),
        protected_paths_policy_schema=protected_paths_policy.POLICY_SCHEMA,
        protected_paths_policy_sha256=protected_paths_policy.POLICY_SHA256,
        authority_mode=authority_mode,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )


def detect_stale_snapshot(
    snapshot: IssueScopeSnapshot,
    *,
    current_issue_body_sha256: Optional[str] = None,
    current_comments_digest_sha256: Optional[str] = None,
    current_allowed_paths_sha256: Optional[str] = None,
) -> Optional[str]:
    """Return a reason_code if `snapshot` is stale relative to a freshly
    supplied readback, else None (Issue #1611 AC8: Issue body OR comment
    drift, and Allowed Paths drift, are both covered)."""
    if current_issue_body_sha256 is not None and current_issue_body_sha256 != snapshot.issue_body_sha256:
        return "stale_snapshot_body_drift"
    if (
        current_comments_digest_sha256 is not None
        and current_comments_digest_sha256 != snapshot.comments_digest_sha256
    ):
        return "stale_snapshot_comment_drift"
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
    if "\x00" in pathspec:
        return False, "pathspec_embedded_nul_rejected"
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


# ─── env sanitization (Issue #1611 contract revision, P1-1) ────────────────

# Every one of these is a git-behavior-redirection variable: if any leaked
# into this executor's subprocess environment (from a parent shell, a CI
# runner, or a prior `git -C`/`--git-dir` invocation in the same process
# tree), git could silently operate against a different repository/index/
# object-store than the `cwd` this executor was told to operate in.
_SANITIZED_GIT_ENV_VARS: Tuple[str, ...] = (
    "GIT_DIR",
    "GIT_COMMON_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_COUNT",
    "GIT_EXEC_PATH",
    "GIT_CEILING_DIRECTORIES",
)


def _sanitized_git_env() -> Dict[str, str]:
    env = dict(os.environ)
    for var in _SANITIZED_GIT_ENV_VARS:
        env.pop(var, None)
    return env


# ─── git subprocess helpers ──────────────────────────────────────────────────


def _run_git(args: List[str], cwd: str, timeout: int = 30, *, binary: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=not binary,
        timeout=timeout,
        env=_sanitized_git_env(),
        check=False,
    )


def _run_git_stdin(
    args: List[str], cwd: str, stdin_bytes: bytes, timeout: int = 30
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=stdin_bytes,
        capture_output=True,
        timeout=timeout,
        env=_sanitized_git_env(),
        check=False,
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


def _git_common_dir(cwd: str) -> Optional[str]:
    result = _run_git(["rev-parse", "--git-common-dir"], cwd)
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    return raw if os.path.isabs(raw) else os.path.normpath(os.path.join(cwd, raw))


def _is_detached_head(cwd: str) -> bool:
    result = _run_git(["symbolic-ref", "-q", "HEAD"], cwd)
    return result.returncode != 0


def _is_unborn_branch(cwd: str) -> bool:
    result = _run_git(["rev-parse", "--verify", "-q", "HEAD"], cwd)
    return result.returncode != 0


def _repository_state_in_progress_reason(cwd: str) -> Optional[str]:
    """Return a reason_code if a merge/rebase/cherry-pick is in progress
    (Issue #1611 contract revision, controlled executor fail-closed list),
    else None."""
    git_dir = _git_common_dir(cwd)
    if git_dir is None:
        return "repository_binding_unavailable"
    if os.path.exists(os.path.join(git_dir, "MERGE_HEAD")):
        return "merge_in_progress"
    if os.path.exists(os.path.join(git_dir, "CHERRY_PICK_HEAD")):
        return "cherry_pick_in_progress"
    if os.path.isdir(os.path.join(git_dir, "rebase-merge")) or os.path.isdir(os.path.join(git_dir, "rebase-apply")):
        return "rebase_in_progress"
    return None


def _has_unmerged_index(cwd: str) -> bool:
    result = _run_git(["ls-files", "--unmerged"], cwd)
    if result.returncode != 0:
        return True  # fail-closed: cannot confirm a clean index
    return bool(result.stdout.strip())


_GITLINK_MODE = GITLINK_MODE  # backward-compat alias


def _diff_index_raw(cwd: str, tree_ish: str) -> Tuple[bool, bytes]:
    """AC3/AC4/AC5: run the canonical mode/OID-aware oracle
    `git diff-index --cached --raw --full-index -z -M <tree-ish>` and
    return `(ok, raw_bytes)`. Always invoked with `text=False` -- callers
    parse the NUL-delimited bytes directly via
    `parse_git_diff_index_raw_z`, never through a lossy text decode."""
    result = _run_git(["diff-index", "--cached", "--raw", "--full-index", "-z", "-M", tree_ish], cwd, binary=True)
    return result.returncode == 0, result.stdout


def _diff_tree_raw(cwd: str, commit_sha: str) -> Tuple[bool, bytes]:
    """Post-commit audit oracle (Issue #1611 contract revision AC7): diffs
    the committed tree directly against its parent tree via `git diff-tree`
    -- deliberately NOT `git diff-index` without `--cached` (which compares
    against the *working tree*, and would false-positive on unrelated
    pre-existing staged/unstaged drift that `git commit --only` correctly
    left uncommitted). `--no-commit-id -r` yields the same NUL-delimited
    raw record format `parse_git_diff_index_raw_z` already parses."""
    result = _run_git(
        ["diff-tree", "--no-commit-id", "-r", "--raw", "--full-index", "-z", "-M", commit_sha],
        cwd,
        binary=True,
    )
    return result.returncode == 0, result.stdout


def _record_full_paths(records: List[ChangedFileRecord]) -> set:
    paths: set = set()
    for record in records:
        paths.add(record.path)
        if record.previous_path:
            paths.add(record.previous_path)
    return paths


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
    """Pure comparison core for the staged-delta/requested equivalence check
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
        env=_sanitized_git_env(),
        check=False,
    )


def _pathspecs_to_nul_stdin(pathspecs: List[str]) -> bytes:
    return b"".join(p.encode("utf-8") + b"\0" for p in pathspecs)


def execute_controlled_change(
    *,
    cwd: str,
    snapshot: IssueScopeSnapshot,
    requested_pathspecs: List[str],
    commit_message: str,
    expected_head: str,
    current_issue_body_sha256: Optional[str] = None,
    current_comments_digest_sha256: Optional[str] = None,
    current_allowed_paths_sha256: Optional[str] = None,
) -> ControlledChangeResult:
    """Single-transaction stage -> classify -> audit -> commit -> re-audit
    boundary (Issue #1611 In Scope, contract revision). See module
    docstring for the full step sequence. `expected_head` is now REQUIRED
    (not optional) -- every invocation must supply the HEAD it believes it
    is operating against; this is the race guard, not an opt-in extra."""
    # 0. Authority gate (AC12): `new_disabled_fail_closed` stops add/commit
    # outright -- never an automatic fallback to the legacy env authority.
    if snapshot.authority_mode == AUTHORITY_NEW_DISABLED_FAIL_CLOSED:
        return _denied("authority_new_disabled_fail_closed_add_commit_stopped")
    if snapshot.authority_mode not in AUTHORITY_MODE_STATES:
        return _denied("unknown_authority_mode")

    # 1. Stale snapshot detection (AC8) -- checked before any repository
    # state is touched.
    stale_reason = detect_stale_snapshot(
        snapshot,
        current_issue_body_sha256=current_issue_body_sha256,
        current_comments_digest_sha256=current_comments_digest_sha256,
        current_allowed_paths_sha256=current_allowed_paths_sha256,
    )
    if stale_reason is not None:
        return _denied(stale_reason)

    if not commit_message or not commit_message.strip():
        return _denied("commit_message_required")
    if not expected_head:
        return _denied("expected_head_required")

    # 2. Repository / worktree / branch / HEAD binding.
    repo_root = _git_toplevel(cwd)
    if repo_root is None:
        return _denied("repository_binding_unavailable")
    if os.path.realpath(repo_root) != snapshot.worktree_realpath:
        return _denied("worktree_binding_mismatch")
    if os.path.realpath(cwd) != os.path.realpath(repo_root) and not os.path.realpath(cwd).startswith(
        os.path.realpath(repo_root) + os.sep
    ):
        return _denied("cwd_outside_worktree")

    if _is_detached_head(cwd):
        return _denied("detached_head_rejected")
    if _is_unborn_branch(cwd):
        return _denied("unborn_branch_rejected")

    in_progress_reason = _repository_state_in_progress_reason(cwd)
    if in_progress_reason is not None:
        return _denied(in_progress_reason)
    if _has_unmerged_index(cwd):
        return _denied("unmerged_index_conflict")

    current_branch = _current_branch(cwd)
    expected_branch = snapshot.branch_ref.split("/")[-1] if "/" in snapshot.branch_ref else snapshot.branch_ref
    if not current_branch or current_branch != expected_branch:
        return _denied("branch_binding_mismatch")

    local_head = _current_head(cwd)
    if local_head != expected_head:
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

    # 4. Capture a BASELINE of whatever is already staged (relative to
    # `expected_head`) BEFORE we stage anything -- `git commit --only`
    # tolerates pre-existing unrelated staged content (it is never swept
    # into our commit), so we no longer require a clean index; instead we
    # diff the DELTA our own staging introduces against this baseline.
    baseline_ok, baseline_raw = _diff_index_raw(cwd, expected_head)
    if not baseline_ok:
        return _denied("index_baseline_read_failed")
    try:
        baseline_records = parse_git_diff_index_raw_z(baseline_raw, source=SOURCE_GIT_DIFF_INDEX_RAW_Z)
    except UnsupportedPathEncodingError:
        return _denied("unsupported_path_encoding")
    except ValueError as exc:
        return _denied("index_baseline_parse_failed", detail=str(exc))
    baseline_paths = _record_full_paths(baseline_records)

    # 5. Stage with a single `git --literal-pathspecs add --pathspec-from-
    # file=- --pathspec-file-nul` call (bytes stdin, NUL-delimited -- AC5).
    stdin_bytes = _pathspecs_to_nul_stdin(requested_pathspecs)
    add_result = _run_git_stdin(
        ["--literal-pathspecs", "add", "--pathspec-from-file=-", "--pathspec-file-nul"], cwd, stdin_bytes
    )
    if add_result.returncode != 0:
        _unstage(cwd, requested_pathspecs)
        return _denied("git_add_failed", detail=add_result.stderr.decode("utf-8", errors="replace").strip())

    # 6. Re-read the index via the mode/OID-aware, NUL-delimited raw oracle
    # (AC3/AC4/AC5/AC11) and compute the DELTA against the baseline.
    post_stage_ok, post_stage_raw = _diff_index_raw(cwd, expected_head)
    if not post_stage_ok:
        _unstage(cwd, requested_pathspecs)
        return _denied("index_reread_failed")
    try:
        post_stage_records = parse_git_diff_index_raw_z(post_stage_raw, source=SOURCE_GIT_DIFF_INDEX_RAW_Z)
    except UnsupportedPathEncodingError:
        _unstage(cwd, requested_pathspecs)
        return _denied("unsupported_path_encoding")
    except ValueError as exc:
        _unstage(cwd, requested_pathspecs)
        return _denied("index_reread_parse_failed", detail=str(exc))

    delta_records = [
        record
        for record in post_stage_records
        if record.path not in baseline_paths or (record.previous_path and record.previous_path not in baseline_paths)
    ]
    delta_paths = _record_full_paths(delta_records)

    classified_records: List[Dict[str, Any]] = [
        {
            "path": record.path,
            "previous_path": record.previous_path,
            "git_status": record.status,
            "old_mode": record.old_mode,
            "new_mode": record.new_mode,
            "old_oid": record.old_oid,
            "new_oid": record.new_oid,
            "is_submodule_gitlink_change": record.is_submodule_gitlink_change,
        }
        for record in delta_records
    ]

    # 7. Delta set must exactly equal the requested set (AC7).
    if not _staged_matches_requested(delta_paths, requested_set):
        _unstage(cwd, requested_pathspecs)
        return _denied(
            "staged_requested_mismatch",
            staged_paths=tuple(sorted(delta_paths)),
            requested_paths=tuple(sorted(requested_set)),
        )

    # 8. Protected paths always deny, regardless of Allowed Paths (AC10).
    protected_hits = sorted({path for path in delta_paths if protected_paths_policy.is_protected_path(path)})
    if protected_hits:
        _unstage(cwd, requested_pathspecs)
        return _denied(
            "protected_path_denied",
            protected_paths_hit=tuple(protected_hits),
            staged_paths=tuple(sorted(delta_paths)),
            requested_paths=tuple(sorted(requested_set)),
        )

    # 9. Every delta path (current + previous, for renames) must be within
    # the snapshot's Allowed Paths (AC2/AC3/AC4).
    out_of_scope = sorted(
        {path for path in delta_paths if not AllowedPathsMatcher.is_file_allowed(path, list(snapshot.allowed_paths))}
    )
    if out_of_scope:
        _unstage(cwd, requested_pathspecs)
        return _denied(
            "path_outside_allowed_paths",
            denied_paths=tuple(out_of_scope),
            staged_paths=tuple(sorted(delta_paths)),
            requested_paths=tuple(sorted(requested_set)),
        )

    # 10. Re-confirm HEAD has not moved since step 2 (narrows, does not
    # eliminate, the stage-to-commit race -- see module docstring).
    if _current_head(cwd) != expected_head:
        _unstage(cwd, requested_pathspecs)
        return _denied("head_race_detected_before_commit")

    # 11. `git commit --only` restricts the commit to exactly the given
    # pathspecs -- pre-existing unrelated staged content is never swept in.
    commit_result = _run_git_stdin(
        ["commit", "--only", "--pathspec-from-file=-", "--pathspec-file-nul", "-m", commit_message],
        cwd,
        stdin_bytes,
    )
    if commit_result.returncode != 0:
        return _denied("commit_failed", detail=commit_result.stderr.decode("utf-8", errors="replace").strip())

    commit_sha = _current_head(cwd)
    if commit_sha is None:
        return _denied("commit_sha_unavailable")

    # 12. Post-commit re-audit (AC7): diff the committed tree directly
    # against its parent (never the working tree) and confirm the audited
    # set/classification still matches the pre-commit delta; roll back on
    # any disagreement.
    post_commit_ok, post_commit_raw = _diff_tree_raw(cwd, commit_sha)
    post_commit_violation = not post_commit_ok
    if post_commit_ok:
        try:
            commit_records = parse_git_diff_index_raw_z(post_commit_raw, source=SOURCE_GIT_DIFF_INDEX_RAW_Z)
        except (UnsupportedPathEncodingError, ValueError):
            post_commit_violation = True
        else:
            commit_paths = _record_full_paths(commit_records)
            if commit_paths != requested_set:
                post_commit_violation = True
            else:
                for path in commit_paths:
                    if protected_paths_policy.is_protected_path(path) or not AllowedPathsMatcher.is_file_allowed(
                        path, list(snapshot.allowed_paths)
                    ):
                        post_commit_violation = True
                        break

    if post_commit_violation:
        subprocess.run(
            ["git", "reset", "--soft", "HEAD~1"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            env=_sanitized_git_env(),
            check=False,
        )
        _unstage(cwd, requested_pathspecs)
        return _denied("post_commit_audit_violation_rolled_back")

    return ControlledChangeResult(
        status="committed",
        reason_code="committed",
        commit_sha=commit_sha,
        staged_paths=tuple(sorted(delta_paths)),
        requested_paths=tuple(sorted(requested_set)),
        classified_records=tuple(classified_records),
    )


# ─── CLI entrypoint ───────────────────────────────────────────────────────────

_MAX_INPUT_FILE_BYTES = 1_000_000


def _validate_input_file(path: Path) -> Optional[str]:
    """Fail-closed checks for `--snapshot-json` / `--message-file` (Issue
    #1611 contract revision, controlled executor fail-closed list): reject
    a missing file, a symlink, a hardlink (`st_nlink > 1`), or an
    over-sized file."""
    try:
        if not path.exists():
            return "input_file_missing"
        if path.is_symlink():
            return "input_file_symlink_rejected"
        st = path.stat()
        if st.st_nlink > 1:
            return "input_file_hardlink_rejected"
        if st.st_size > _MAX_INPUT_FILE_BYTES:
            return "input_file_oversized"
    except OSError:
        return "input_file_stat_failed"
    return None


def _build_cli_parser():
    import argparse

    parser = argparse.ArgumentParser(
        description="Controlled stage/commit executor: the single authorized path for "
        "agent-driven git staging and commits (Issue #1611)."
    )
    parser.add_argument("--cwd", required=True, help="worktree directory to operate in")
    parser.add_argument("--snapshot-json", required=True, help="path to a JSON file with the IssueScopeSnapshot fields")
    parser.add_argument("--path", action="append", dest="paths", default=[], help="explicit pathspec to stage (repeatable)")
    parser.add_argument("--message", default=None, help="commit message")
    parser.add_argument("--message-file", default=None, help="path to a file containing the commit message")
    parser.add_argument("--expected-head", required=True, help="expected local HEAD SHA (race guard, required)")
    return parser


def _main(argv: Optional[List[str]] = None) -> int:
    args = _build_cli_parser().parse_args(argv if argv is not None else sys.argv[1:])

    snapshot_path = Path(args.snapshot_json)
    snapshot_file_error = _validate_input_file(snapshot_path)
    if snapshot_file_error is not None:
        print(json.dumps({"status": "denied", "reason_code": snapshot_file_error}))
        return 1

    commit_message = args.message
    if args.message_file:
        message_path = Path(args.message_file)
        message_file_error = _validate_input_file(message_path)
        if message_file_error is not None:
            print(json.dumps({"status": "denied", "reason_code": message_file_error}))
            return 1
        commit_message = message_path.read_text(encoding="utf-8")
    if not commit_message:
        print(json.dumps({"status": "denied", "reason_code": "commit_message_required"}))
        return 1

    with open(snapshot_path, encoding="utf-8") as fh:
        snapshot_data = json.load(fh)
    snapshot = IssueScopeSnapshot(
        schema_version=snapshot_data.get("schema_version", SCHEMA_VERSION),
        repository_full_name=snapshot_data["repository_full_name"],
        issue_number=snapshot_data["issue_number"],
        contract_source_kind=snapshot_data["contract_source_kind"],
        contract_source_id=snapshot_data["contract_source_id"],
        contract_source_body_sha256=snapshot_data["contract_source_body_sha256"],
        issue_body_sha256=snapshot_data["issue_body_sha256"],
        issue_updated_at=snapshot_data["issue_updated_at"],
        comments_digest_sha256=snapshot_data["comments_digest_sha256"],
        allowed_paths=tuple(snapshot_data["allowed_paths"]),
        allowed_paths_normalized_sha256=snapshot_data["allowed_paths_normalized_sha256"],
        allowed_paths_matcher_schema=snapshot_data.get("allowed_paths_matcher_schema", ALLOWED_PATHS_MATCHER_SCHEMA),
        base_ref=snapshot_data["base_ref"],
        base_sha=snapshot_data["base_sha"],
        branch_ref=snapshot_data["branch_ref"],
        worktree_realpath=snapshot_data["worktree_realpath"],
        protected_paths_policy_schema=snapshot_data.get(
            "protected_paths_policy_schema", protected_paths_policy.POLICY_SCHEMA
        ),
        protected_paths_policy_sha256=snapshot_data["protected_paths_policy_sha256"],
        authority_mode=snapshot_data["authority_mode"],
        generated_at=snapshot_data.get("generated_at", ""),
    )
    result = execute_controlled_change(
        cwd=args.cwd,
        snapshot=snapshot,
        requested_pathspecs=args.paths,
        commit_message=commit_message,
        expected_head=args.expected_head,
    )
    print(json.dumps(result.to_dict()))
    return 0 if result.status == "committed" else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
