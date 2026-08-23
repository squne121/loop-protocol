#!/usr/bin/env python3
"""Exact privileged executor for allowed skill runtime commands (Issue #1154)."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Iterable, NamedTuple

sys.dont_write_bytecode = True

from skill_runtime_command_policy import (
    GIT_SUBPROCESS_UNSET_ENV_KEYS,
    INSTEADOF_CONFIG_NAME_REGEXP,
    REGISTRY_REL,
    SKILL_RUNTIME_EXEC_REL,
    TRUSTED_REPO_SLUG,
    ExactSkillRuntimeCommand,
    GitSubprocessConfigProbeFailed,
    _is_safe_issue_artifact_path,
    command_allows_root_no_worktree,
    current_branch,
    is_exact_skill_runtime_anchor_executor_command,
    is_exact_skill_runtime_anchor_fixture_executor_command,
    is_exact_skill_runtime_authority_transport_consume_executor_command,
    is_exact_skill_runtime_authority_transport_produce_executor_command,
    is_exact_skill_runtime_contract_update_anchor_executor_command,
    is_exact_skill_runtime_decide_authority_executor_command,
    is_exact_skill_runtime_decide_executor_command,
    is_exact_skill_runtime_executor_command,
    is_exact_skill_runtime_fixture_executor_command,
    is_exact_skill_runtime_repair_action_apply_executor_command,
    load_registry_entry,
    parse_config_get_regexp_name_only_nul,
    reject_git_global_options,
    reject_insteadof_rewrite,
    reject_option_like_positional,
    resolve_active_issue,
    resolve_default_branch,
    resolve_project_root,
    resolve_repo_slug,
    validate_registry_entry,
)


# Roots that other concurrent local sessions/agents/hooks may legitimately
# write to while this executor's own child command is running. Changes under
# these roots must never be attributed to the child command's own subprocess
# (Issue #1343, Issue #1409): the executor only ever runs a single child
# process whose own allowed writes are scoped to the target issue's artifact
# root, so any other concurrent repo-wide drift under these roots is
# unattributable -- it may originate from a peer session/agent (Issue #1343)
# or from this same session's own asynchronous PostToolUse/SubagentStop hook
# machinery (Issue #1409: `.claude/hooks/session_manifest_debounce.mjs` /
# `.claude/hooks/generate_session_manifest_from_hook.mjs` writing under the
# hook-owned subtree `artifacts/session-manifest-runtime/`). Either way, the
# executor cannot distinguish "who" wrote it in stdlib-only race-tolerant
# mode, so this symbol is named for that shared property (unattributable),
# not for a single cause (peer-session).
#
# NOTE: `artifacts/session-manifest-runtime` is the *only* addition for
# Issue #1409 -- the repo-root `artifacts/` directory as a whole remains
# fully audited, because `artifacts/{issue}/issue-metadata/{command-id}/`
# is a controlled-mutation input/marker namespace whose provenance still
# needs to be tracked (OWNER REQUEST_CHANGES on the original repo-wide
# `artifacts/` exclusion proposal, see
# https://github.com/squne121/loop-protocol/issues/1409#issuecomment-4935283248).
_RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS = (
    ".claude/worktrees",
    ".claude/artifacts/issue-refinement-loop",
    "artifacts/session-manifest-runtime",
)
# Issue #1563: `.guard_shadow_log.jsonl` (repo-root peer-append log written by
# `.claude/hooks/shadow_log.py`, `.claude/hooks/guard-japanese-prose.sh`,
# `.claude/hooks/rtk_boundary_shadow_guard.sh`, and `scripts/check-codex-agents.mjs`)
# is deliberately NOT added to this tuple. This tuple is a *directory-root*
# exclusion class: `_snapshot_repo_paths()` prunes everything under these
# roots before even inspecting the transition kind, so adding an exact file
# here would make a symlink/directory/FIFO/socket/device replacement of that
# file invisible to this executor -- the exact opposite of the fail-close
# guarantee this executor exists to provide. `.guard_shadow_log.jsonl` instead
# gets its own narrow, exact-path, transition-typed policy (see
# `_SHADOW_LOG_EXACT_REL` below), mirroring the `_LEDGER_*` typed policy.


def _race_tolerant_unattributable_roots(project_root: str) -> list[Path]:
    root = Path(project_root)
    return [root / Path(rel) for rel in _RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS]


def _is_race_tolerant_unattributable_path(rel_path: str) -> bool:
    normalized = rel_path.replace(os.sep, "/")
    for prefix in _RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


# =============================================================================
# Typed SubAgent-launch-ledger transition policy (Issue #1502).
#
# `_RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS` above is a *directory root*
# exclusion class: it never inspects the transition kind of anything under
# those roots. The ledger final file cannot use that class (Out of Scope:
# directory-wide exclusion of `artifacts/`, `artifacts/codex/`, or `tmp/` is
# forbidden), so it gets its own narrow, exact-path, transition-typed policy:
#
# - stable exact peer file (`_LEDGER_STABLE_EXACT_REL`): the canonical ledger
#   final file. Only `absent -> regular` and `regular -> regular` transitions
#   are authorized; delete, symlink, directory, FIFO, socket, or device
#   substitution fail closed on that exact path (AC2).
# - transient protocol entries (`_LEDGER_TRANSIENT_EXACT_RELS`): the writer's
#   `.lock` / `.tmp` sibling files. These may exist only for the bounded
#   duration of a single native-writer invocation; the executor waits (bounded
#   quiescence) for them to vanish and fails closed on any residue that
#   outlives the timeout (AC3).
#
# Everything else under `artifacts/codex/` (siblings of the three exact
# paths) remains subject to the ordinary repo-wide snapshot/status diff with
# no special-casing, so unexpected sibling create/update/delete/rename still
# fails closed (AC4).
# =============================================================================

_LEDGER_ARTIFACT_DIR_REL = "artifacts/codex"
_LEDGER_STABLE_EXACT_REL = f"{_LEDGER_ARTIFACT_DIR_REL}/subagent-launch-ledger.json"
_LEDGER_TRANSIENT_EXACT_RELS = (
    f"{_LEDGER_STABLE_EXACT_REL}.lock",
    f"{_LEDGER_STABLE_EXACT_REL}.tmp",
)
_LEDGER_TYPED_EXACT_RELS = (_LEDGER_STABLE_EXACT_REL, *_LEDGER_TRANSIENT_EXACT_RELS)

# Ancestor directory *node* entries of the stable ledger path (e.g.
# "artifacts", "artifacts/codex"). When the stable ledger transitions
# `absent -> regular` for the first time in a fresh repo/worktree, its parent
# directories are newly created too, and each appears in the repo-wide
# snapshot as a brand-new directory-node entry (its own mtime/size), which
# would otherwise be reported as an unrelated unauthorized change even though
# the underlying ledger transition itself was already authorized above. This
# is narrower than a directory-wide exclusion: only the ancestor directory's
# own node entry is excluded, never any other path inside it, so an
# unexpected sibling file created alongside the ledger is still detected via
# its own (distinct) path entry (AC4).
_LEDGER_STABLE_ANCESTOR_DIR_RELS = tuple(
    str(parent).replace(os.sep, "/") for parent in Path(_LEDGER_STABLE_EXACT_REL).parents if str(parent) != "."
)


# `_LEDGER_STABLE_ANCESTOR_DIR_RELS` is ordered deepest-first (from
# `Path(...).parents`). Ancestor kind classification/exemption must instead
# walk shallowest-first: a substituted shallow parent (e.g. `artifacts`
# replaced by a symlink or file) can make a deeper rel (e.g.
# `artifacts/codex`) *look* like a fresh, legitimate `absent -> dir`
# transition purely because the substituted parent didn't previously resolve
# to anything -- shallow-first propagation prevents that laundering.
_LEDGER_STABLE_ANCESTOR_DIR_RELS_SHALLOW_TO_DEEP = tuple(reversed(_LEDGER_STABLE_ANCESTOR_DIR_RELS))

# Sentinel before-kind for an ancestor rel whose own parent was already
# confirmed non-traversable (a real file/fifo/socket/device node) before the
# child ran: the path is filesystem-unreachable (any real subpath under a
# non-directory, non-symlink leaf node raises `ENOTDIR`), so probing it
# directly would raise rather than classify. This sentinel never matches an
# authorized ancestor transition tuple, so it always fails closed.
_LEDGER_ANCESTOR_KIND_UNREACHABLE = "unreachable"

# Ancestor kinds that make every deeper path component unreachable via a
# direct `lstat` (a real subpath cannot exist under a plain file, FIFO,
# socket, or device node). `"absent"` and `"symlink"` are deliberately
# excluded: the OS still traverses through a missing or symlinked
# intermediate component without raising (a missing intermediate simply
# yields `FileNotFoundError` -> `"absent"` for the deeper path too; a
# symlinked intermediate is followed transparently unless it is the *final*
# path component).
_LEDGER_ANCESTOR_NON_TRAVERSABLE_KINDS = frozenset({"regular", "fifo", "socket", "device"})


def _ledger_ancestor_kinds(project_root: str) -> dict[str, str]:
    """Snapshot the on-disk kind of every stable-ledger ancestor directory
    node *before* the child command runs (Issue #1502 REQUEST_CHANGES
    Blocker 5). This is required so the ancestor exemption below can compare
    a genuine before-kind (which may be `"symlink"` or `"regular"` in a
    parent-substitution attack) instead of assuming `"absent"`.

    Walks shallowest-first and stops probing once a shallower ancestor is
    confirmed non-traversable, recording `_LEDGER_ANCESTOR_KIND_UNREACHABLE`
    for every deeper rel instead of calling `_path_kind` on it (which would
    otherwise raise `NotADirectoryError`/`ENOTDIR`, since Issue #1502
    REQUEST_CHANGES Blocker 2 no longer folds arbitrary `OSError` into
    `"absent"`)."""
    root = Path(project_root)
    kinds: dict[str, str] = {}
    blocked = False
    for rel in _LEDGER_STABLE_ANCESTOR_DIR_RELS_SHALLOW_TO_DEEP:
        if blocked:
            kinds[rel] = _LEDGER_ANCESTOR_KIND_UNREACHABLE
            continue
        kind = _path_kind(root / rel)
        kinds[rel] = kind
        if kind in _LEDGER_ANCESTOR_NON_TRAVERSABLE_KINDS:
            blocked = True
    return kinds


def _is_allowed_ancestor_transition(before_kind: str, after_kind: str) -> bool:
    """An ancestor directory-node side effect of an authorized stable-ledger
    transition is limited to `absent -> dir` (first-ever creation) and
    `dir -> dir` (already existed, unchanged kind). Any other before-kind
    (`symlink`, `regular`, `fifo`, `socket`, `device`, or the
    `"unreachable"` sentinel) transitioning into a real directory is parent
    substitution and must fail closed -- it is never silently excluded from
    the generic diff."""
    return (before_kind, after_kind) in {("absent", "dir"), ("dir", "dir")}


def _safe_ledger_ancestor_dir_rels(
    project_root: str, ancestor_before_kinds: dict[str, str] | None = None
) -> set[str]:
    """Return the subset of `_LEDGER_STABLE_ANCESTOR_DIR_RELS` whose
    before -> after kind transition is one of the two authorized ancestor
    transitions (Issue #1502 REQUEST_CHANGES Blocker 5). Postcondition-only
    inspection (checking only whether the *after* state is a real
    non-symlink directory) is insufficient: a parent that was a symlink or
    plain file *before* the child ran and got replaced by a real directory
    *during* the child's run must never be silently excluded here -- only a
    genuine directory-node side effect of the already-authorized ledger
    transition is.

    Walks shallowest-first and propagates unsafety downward: once any
    ancestor in the chain fails its own transition check, every deeper rel
    under it is excluded from the safe set too, even if that deeper rel's
    own isolated before/after kinds would otherwise look like a legitimate
    `absent -> dir` transition (which they can, spuriously, precisely
    because the substituted shallow parent didn't previously resolve to
    anything real)."""
    ancestor_before_kinds = ancestor_before_kinds or {}
    root = Path(project_root)
    safe: set[str] = set()
    chain_safe = True
    for rel in _LEDGER_STABLE_ANCESTOR_DIR_RELS_SHALLOW_TO_DEEP:
        if not chain_safe:
            # PR #1552 REQUEST_CHANGES follow-up: once a shallower ancestor
            # in the chain is already confirmed unsafe, do not probe any
            # deeper rel's kind at all -- a shallower ancestor substituted by
            # a plain file/fifo/socket/device makes every deeper path
            # unreachable via a direct `lstat` (raises `ENOTDIR`), so calling
            # `_path_kind` on it would only risk an uncaught crash for a
            # result this loop already discards (mirrors the `blocked`
            # skip-pattern in `_ledger_ancestor_kinds` above).
            continue
        before_kind = ancestor_before_kinds.get(rel, "absent")
        after_kind = _path_kind_or_ancestor_absent(root / rel)
        if _is_allowed_ancestor_transition(before_kind, after_kind):
            safe.add(rel)
        else:
            chain_safe = False
    return safe

# Bounded quiescence window: how long the executor waits, after the child
# process exits, for the writer's own `.lock` / `.tmp` protocol entries to be
# removed by the (already-exited-or-still-finishing) peer writer process
# before treating any residue as stale (fail-closed, never auto-deleted).
_LEDGER_TRANSIENT_QUIESCENCE_TIMEOUT_SECONDS = 2.0
_LEDGER_TRANSIENT_QUIESCENCE_POLL_INTERVAL_SECONDS = 0.05
# Issue #1502 REQUEST_CHANGES (Blocker 6): after an apparently-clean
# (fully-absent) poll, wait this long and re-poll before trusting the
# observation. A single empty poll is not sufficient evidence of quiescence
# -- a still-finishing peer writer could create/remove these entries again in
# the gap between that poll and the caller's subsequent "after" snapshot
# capture (TOCTOU). Bounded by the same overall deadline as the main loop.
_LEDGER_TRANSIENT_QUIESCENCE_CONFIRM_INTERVAL_SECONDS = 0.1


def _path_kind(path: Path) -> str:
    """Classify a filesystem path by its on-disk kind, never following the
    final symlink component (uses lstat so a symlink is reported as
    `"symlink"`, not as the kind of its target).

    Issue #1502 REQUEST_CHANGES (Blocker 2): only `FileNotFoundError` is
    treated as `"absent"`. Any other `OSError` (e.g. `EACCES`, `EIO`,
    `ENOTDIR` from a non-directory ancestor) must never be silently folded
    into `"absent"` -- doing so would fail-open a transition check that
    expects `"absent"` to mean "nothing is there", when the real condition is
    "the on-disk state could not be determined". Such errors propagate to the
    caller (and therefore to `main()`'s uncaught-exception fail-closed exit),
    never masquerading as a benign missing path."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return "absent"
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        return "device"
    return "other"


def _path_kind_or_ancestor_absent(path: Path) -> str:
    """Classify like `_path_kind`, but additionally treat
    `NotADirectoryError` (`ENOTDIR` -- some ancestor path component exists on
    disk as a non-directory node) as `"absent"` instead of propagating it
    uncaught.

    PR #1552 REQUEST_CHANGES follow-up (post-Blocker-2 regression): unlike
    genuinely ambiguous errors (`EACCES`, `EIO`, ...), which `_path_kind`
    correctly refuses to fold into `"absent"` because their real on-disk
    state is unknown, `ENOTDIR` is unambiguous proof that no real filesystem
    node can exist at the deeper path -- a directory-nested file literally
    cannot exist under a non-directory ancestor. Folding only this specific,
    provable case into `"absent"` here never masks a genuine
    parent-substitution attack: the independent, generic repo-wide diff in
    `_find_unauthorized_repo_changes` (driven by `git status`, not by
    `_path_kind`) still observes and fails closed on the substituted
    ancestor itself (e.g. `artifacts` created as a plain file) on its own,
    unconditionally. This helper exists only so that call sites which must
    keep evaluating past a broken ancestor (transient-lock quiescence
    polling, the stable-ledger transition check) route to that controlled
    fail-close path instead of crashing with an uncaught traceback -- which
    would silently *skip* the fail-close reporting entirely, the opposite of
    Blocker 2's intent."""
    try:
        return _path_kind(path)
    except NotADirectoryError:
        return "absent"


def _ledger_exact_kinds(project_root: str) -> dict[str, str]:
    root = Path(project_root)
    return {rel: _path_kind(root / rel) for rel in _LEDGER_TYPED_EXACT_RELS}


def _is_allowed_stable_ledger_transition(before_kind: str, after_kind: str) -> bool:
    """`absent -> regular` and `regular -> regular` are the only authorized
    stable-exact-ledger transitions (AC2). Delete (`regular -> absent`) and
    substitution into any non-regular kind (symlink / dir / fifo / socket /
    device), from any before-kind, are rejected.

    Issue #1502 REQUEST_CHANGES (Blocker 2): the previous implementation
    returned True whenever `after_kind == "regular"` regardless of
    `before_kind`, which silently authorized `symlink -> regular`,
    `directory -> regular`, `fifo -> regular`, `socket -> regular`, and
    `device -> regular` substitutions -- the exact opposite of the documented
    contract. This is an explicit allow-tuple match instead."""
    if before_kind == "absent" and after_kind == "absent":
        return True
    return (before_kind, after_kind) in {("absent", "regular"), ("regular", "regular")}


def _wait_for_ledger_transient_quiescence(project_root: str) -> list[str]:
    """Poll the writer's `.lock` / `.tmp` transient protocol entries until a
    clean (fully-absent) observation is *confirmed* after a short quiet
    interval, or the bounded quiescence window elapses.

    Issue #1502 REQUEST_CHANGES (Blocker 6): a bare single-poll "empty now ->
    return success immediately" check has a TOCTOU gap between that poll and
    the caller's subsequent "after" snapshot capture -- a still-finishing
    peer writer could re-create a `.lock` / `.tmp` entry in that gap and it
    would never be observed. This loop treats an empty poll as tentative: it
    re-polls after `_LEDGER_TRANSIENT_QUIESCENCE_CONFIRM_INTERVAL_SECONDS`
    and only returns success (`[]`) once the same clean generation is
    observed twice in a row. If the entries reappear during confirmation,
    polling resumes against the overall deadline as normal.

    Returns the (possibly empty) list of transient relative paths still
    present once the window elapses. Never deletes anything itself -- a
    non-empty return means stale residue that the caller must fail closed on
    (AC3)."""
    root = Path(project_root)
    deadline = time.monotonic() + _LEDGER_TRANSIENT_QUIESCENCE_TIMEOUT_SECONDS

    def _poll() -> list[str]:
        return [
            rel
            for rel in _LEDGER_TRANSIENT_EXACT_RELS
            if _path_kind_or_ancestor_absent(root / rel) != "absent"
        ]

    last = _poll()
    while True:
        now = time.monotonic()
        if not last:
            confirm_at = now + _LEDGER_TRANSIENT_QUIESCENCE_CONFIRM_INTERVAL_SECONDS
            if confirm_at > deadline:
                remaining_wait = max(0.0, deadline - now)
                if remaining_wait:
                    time.sleep(remaining_wait)
                return _poll()
            time.sleep(_LEDGER_TRANSIENT_QUIESCENCE_CONFIRM_INTERVAL_SECONDS)
            confirmed = _poll()
            if not confirmed:
                return []
            last = confirmed
            continue
        if now >= deadline:
            return last
        time.sleep(_LEDGER_TRANSIENT_QUIESCENCE_POLL_INTERVAL_SECONDS)
        last = _poll()


def _is_symlink_path(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part in ("", os.sep):
            continue
        current = current / part
        if current.exists() and current.is_symlink():
            return True
    return False


def _allowed_artifact_roots(project_root: str, issue_number: str, command_id: str = "") -> tuple[Path, ...]:
    roots = [Path(project_root) / ".claude" / "artifacts" / "issue-refinement-loop" / issue_number]
    if command_id in {"contract_update.run.with_anchor", "contract_update.run.with_human_context"}:
        # The existing edit-issue transaction writes its request metadata
        # under this exact Issue-scoped directory.  Do not grant the phase a
        # broader artifacts root or a second Issue's metadata directory.
        roots.append(Path(project_root) / "artifacts" / issue_number / "issue-metadata")
    return tuple(roots)


def _allowed_artifact_root(project_root: str, issue_number: str) -> Path:
    """Legacy single-root accessor for read-only preflight callers."""
    return _allowed_artifact_roots(project_root, issue_number)[0]


def _is_under_allowed_artifact_root(
    project_root: str, issue_number: str, rel_path: str, command_id: str = ""
) -> bool:
    root = Path(project_root)
    target = (root / rel_path).resolve()
    return any(
        target == allowed_root.resolve() or target.is_relative_to(allowed_root.resolve())
        for allowed_root in _allowed_artifact_roots(project_root, issue_number, command_id)
    )


def _git_status_paths(project_root: str) -> set[str]:
    git = shutil.which("git") or "git"
    out = subprocess.run(
        [
            git,
            "-C",
            project_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
            "-z",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError("git_status_failed")
    paths: set[str] = set()
    fields = [field for field in out.stdout.split("\0") if field]
    i = 0
    while i < len(fields):
        field = fields[i]
        if len(field) < 4:
            i += 1
            continue
        path = field[3:]
        if field[0] == "R" or field[1] == "R":
            paths.add(path)
            if i + 1 < len(fields):
                paths.add(fields[i + 1])
                i += 2
                continue
        paths.add(path)
        i += 1
    return paths


def _strict_ancestor_of_race_tolerant_root(rel_path: str) -> bool:
    """True when `rel_path` (a directory-status entry, e.g. `artifacts/`) is a
    strict ancestor of at least one race-tolerant-unattributable root, but is
    not itself one of those roots.

    Issue #1409 REQUEST_CHANGES (P1): Git's `--ignored=matching` collapses an
    entire ignored directory tree into a single status entry for the
    ignore-pattern-matched directory itself (e.g. `!! artifacts/`), not its
    descendants, whenever that ignored directory does not yet exist in the
    before-snapshot. Because the real repo's `.gitignore` ignores
    `artifacts/` as a whole, a cold-start creation of
    `artifacts/session-manifest-runtime/**` is folded and reported as the
    parent `artifacts/` entry rather than the excluded subtree -- this helper
    identifies that folding so the caller can expand it instead of
    fail-closing on the collapsed ancestor path.
    """
    normalized = rel_path.replace(os.sep, "/").rstrip("/")
    for root in _RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS:
        if normalized != root and root.startswith(normalized + "/"):
            return True
    return False


def _expand_folded_ignored_status_dir(project_root: str, rel_dir: str) -> set[str]:
    """Expand a single Git-status-folded ignored-directory entry (e.g.
    `artifacts/`) into its actual leaf file paths via a *targeted*
    (path-restricted, not repo-wide) `--ignored=traditional` scan. Restricting
    the scan to `rel_dir` keeps this bounded and avoids reintroducing a
    repo-wide `--ignored=traditional` walk (explicitly rejected as an
    unbounded alternative in Issue #1409 REQUEST_CHANGES)."""
    git = shutil.which("git") or "git"
    out = subprocess.run(
        [
            git,
            "-C",
            project_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=traditional",
            "-z",
            "--",
            rel_dir,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError("git_status_failed")
    paths: set[str] = set()
    for field in (f for f in out.stdout.split("\0") if f):
        if len(field) < 4:
            continue
        paths.add(field[3:])
    return paths


def _is_real_nonsymlink_dir(project_root: str, rel_dir: str) -> bool:
    path = Path(project_root) / rel_dir.rstrip("/")
    try:
        if not path.is_dir():
            return False
    except OSError:
        return False
    return not _is_symlink_path(path)


def _strict_ancestor_of_allowed_artifact_root(
    project_root: str, issue_number: str, rel_path: str, command_id: str
) -> bool:
    candidate = (Path(project_root) / rel_path.rstrip("/")).resolve()
    return any(
        candidate != allowed_root.resolve() and allowed_root.resolve().is_relative_to(candidate)
        for allowed_root in _allowed_artifact_roots(project_root, issue_number, command_id)
    )


def _expand_new_status_paths(
    project_root: str, new_raw_paths: set[str], issue_number: str = "", command_id: str = ""
) -> set[str]:
    """Expand any newly-appeared folded-ignored-ancestor entries (see
    `_strict_ancestor_of_race_tolerant_root`) into their real leaf paths so
    that race-tolerant-root exclusion can be applied precisely, instead of
    fail-closing on the collapsed ancestor directory itself.

    Safety (Issue #1409 REQUEST_CHANGES P1): expansion only happens when the
    collapsed entry is confirmed on disk to be a real, non-symlink directory.
    If the entry has instead been substituted by a file or a symlink (parent
    substitution), expansion is skipped and the raw entry is kept as-is so it
    fails closed via the normal unauthorized-path path.
    """
    expanded: set[str] = set()
    for path in new_raw_paths:
        if path.endswith("/") and (
            _strict_ancestor_of_race_tolerant_root(path)
            or _strict_ancestor_of_allowed_artifact_root(project_root, issue_number, path, command_id)
        ):
            if _is_real_nonsymlink_dir(project_root, path):
                expanded.update(_expand_folded_ignored_status_dir(project_root, path))
                continue
        expanded.add(path)
    return expanded


def _snapshot_repo_paths(
    project_root: str, issue_number: str, command_id: str = ""
) -> dict[str, tuple[str, int, int]]:
    root = Path(project_root)
    allowed_roots = _allowed_artifact_roots(project_root, issue_number, command_id)
    peer_roots = _race_tolerant_unattributable_roots(project_root)
    allowed_parent_dirs: set[Path] = set()
    for allowed_root in allowed_roots:
        for parent in allowed_root.parents:
            allowed_parent_dirs.add(parent)
            if parent == root:
                break
    if command_id in {"contract_update.run.with_anchor", "contract_update.run.with_human_context"}:
        # The existing edit-issue transaction uses the repository-approved
        # transaction-local ``tmp/`` workspace for its candidate and input
        # files, and deletes those files before returning.  Ignore only the
        # directory-node mtime churn here; any residual child path remains in
        # the snapshot and is still rejected below.
        allowed_parent_dirs.add(root / "tmp")
    # Issue #1409: also skip recording the directory-node entry (its own
    # mtime/size) for every ancestor of each race-tolerant-unattributable
    # root. Without this, a *new* top-level ancestor directory (e.g.
    # `artifacts/`, when it does not yet exist before the child command
    # runs and is first created by a peer/hook write under
    # `artifacts/session-manifest-runtime/**`) would itself appear as a
    # brand-new snapshot entry and be misreported as an unauthorized write,
    # even though the pruning above already fully excludes the peer root's
    # own contents. `.claude/worktrees` and
    # `.claude/artifacts/issue-refinement-loop` never hit this gap because
    # their ancestor (`.claude`) already coincides with an ancestor of this
    # issue's own `allowed_root`; `artifacts/session-manifest-runtime`'s
    # ancestor (`artifacts`) does not share that coincidence, so it needs
    # its own explicit ancestor-skip set.
    for peer_root in peer_roots:
        for parent in peer_root.parents:
            allowed_parent_dirs.add(parent)
            if parent == root:
                break

    snapshot: dict[str, tuple[str, int, int]] = {}
    for current_root, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current_root)
        if current_path == root / ".git":
            dirnames[:] = []
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if (current_path / name) != root / ".git"
        ]
        # Prune volatile peer-session roots entirely so that concurrent
        # local sessions/agents writing under them are never walked into
        # (and therefore never contribute snapshot drift for this command).
        dirnames[:] = [
            name
            for name in dirnames
            if (current_path / name) not in peer_roots
        ]
        for name in ["."] + dirnames + filenames:
            path = current_path if name == "." else current_path / name
            if path == root / ".git":
                continue
            if path in peer_roots:
                continue
            if any(path == allowed_root or path.is_relative_to(allowed_root) for allowed_root in allowed_roots):
                continue
            if path in allowed_parent_dirs:
                continue
            try:
                stat = path.lstat()
            except FileNotFoundError:
                continue
            rel = os.path.relpath(path, root)
            snapshot[rel] = (
                "dir" if path.is_dir() else "file",
                stat.st_mtime_ns,
                stat.st_size,
            )
    return snapshot


def _ensure_artifact_path_safe(project_root: str, issue_number: str, command_id: str = "") -> Path:
    artifact_roots = _allowed_artifact_roots(project_root, issue_number, command_id)
    for artifact_root in artifact_roots:
        parent = artifact_root.parent
        for candidate in (parent, *parent.parents):
            if candidate == Path(project_root).parent:
                break
            if candidate.exists() and _is_symlink_path(candidate):
                raise RuntimeError("artifact_parent_symlink_not_allowed")
        if artifact_root.exists() and (_is_symlink_path(artifact_root) or artifact_root.is_symlink()):
            raise RuntimeError("artifact_root_symlink_not_allowed")
    return artifact_roots[0]


# `pyproject.toml`'s `[tool.uv].required-version` is the canonical, read-only
# SSOT for the repository's pinned `uv` version (Issue #1598). This module
# never writes it and never introduces a second version literal -- it only
# consumes it as a defense-in-depth "version 照合" check on a `uv`
# resolution that did not come from the strongly-validated hostedtoolcache
# trust root (e.g. a system PATH directory such as `/usr/local/bin`); see
# `_validate_trusted_executable_version` below (Issue #2251 AC7).
_EXACT_VERSION_PIN_RE = re.compile(r"^==(?P<version>\d+\.\d+\.\d+)$")


def _required_uv_version(project_root: str) -> str | None:
    """Return the numeric `uv` version pinned by `pyproject.toml`'s
    `[tool.uv].required-version`, or None if that SSOT is missing,
    unreadable, or not an exact `==X.Y.Z` pin (Issue #2251 AC7)."""
    pyproject_path = Path(project_root) / "pyproject.toml"
    try:
        raw_toml = pyproject_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = tomllib.loads(raw_toml)
    except tomllib.TOMLDecodeError:
        return None
    uv_section = data.get("tool", {}).get("uv")
    if not isinstance(uv_section, dict):
        return None
    raw_pin = uv_section.get("required-version")
    if not isinstance(raw_pin, str):
        return None
    match = _EXACT_VERSION_PIN_RE.fullmatch(raw_pin.strip())
    return match.group("version") if match else None


def _validate_trusted_executable_version(name: str, resolved: str, project_root: str) -> bool:
    """Defense-in-depth "version 照合" for a trusted executable resolved from
    outside the strongly-validated hostedtoolcache trust root (Issue #2241 /
    PR #2247 review P1-3; Issue #2251 narrowed the trust root itself by
    excluding account-home `~/.local/bin`, but a `uv` resolved from a
    remaining non-hostedtoolcache entry -- e.g. `/usr/local/bin` -- still
    gets this check).

    This is not a trust boundary on its own (an executable that emits a
    matching `--version` banner is not thereby proven authentic), but it
    does reject an executable that was replaced with something that does
    not even claim to be the pinned `uv` version. For `uv`, the expected
    version is the exact `pyproject.toml` [tool.uv].required-version pin
    (Issue #2251 AC7) -- if that SSOT is missing/malformed there is nothing
    trustworthy to compare against, so this fails closed (returns False)
    rather than skipping the check."""
    if name != "uv":
        # No registered expectation for this executable name -- nothing to
        # compare against, so this check is inapplicable rather than a
        # rejection (the ownership/commonpath/regular-file checks above
        # remain the primary defense for non-`uv` names).
        return True
    required_version = _required_uv_version(project_root)
    if required_version is None:
        return False
    try:
        proc = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    match = re.match(r"^uv (\d+\.\d+\.\d+)", (proc.stdout or "").strip())
    return bool(match) and match.group(1) == required_version


def _os_account_home() -> str | None:
    """Return the real OS account home directory for the UID this process
    itself runs as, resolved via the passwd database (`pwd.getpwuid`) --
    never via the ambient `HOME` environment variable, which a caller or
    launcher can freely set to an arbitrary path (e.g. `HOME=/tmp/evil`)
    without that changing which Unix account this process actually runs
    as (Issue #2276 decision record). Returns None if the account has no
    passwd entry, so callers fail closed rather than raising."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return None


def _trusted_account_home_uv_dir() -> list[str]:
    """Return the real-account-home `.local/bin` directory as a `uv`
    PATH candidate (Issue #2276 / #2280), or an empty list if unavailable.

    This is the standard install location used by the official `uv`
    standalone installer on Linux, so no bespoke `trusted-bin` directory,
    sudo installation step, or repository-owned checksum authority is
    introduced. The directory is derived from `_os_account_home()`, not
    the ambient `HOME` env var, so an attacker who can only set `HOME`
    cannot relocate this trust candidate. It is not, on its own, a
    stronger guarantee than the pre-existing system PATH lane: anything
    resolved from here still must pass the exact-version check in
    `_validate_trusted_executable_version` (this directory is never part
    of the hostedtoolcache trust root), so a wrong-version or replaced
    `uv` here still fails closed exactly like `/usr/local/bin` does
    today."""
    home = _os_account_home()
    if not home:
        return []
    candidate = os.path.join(home, ".local", "bin")
    return [candidate] if os.path.isdir(candidate) else []


def _safe_path_entries() -> list[str]:
    """Return the ordered list of trusted, name-agnostic PATH directories
    consumed by `_resolve_trusted_executable`/`_sanitize_env`/
    `sanitized_git_subprocess_env` for every trusted executable name this
    module resolves (`uv`, `git`, `ssh`, ...).

    Real-account-home `.local/bin` (Issue #2276 / #2280 decision record) is
    deliberately NOT included here: see `_resolve_trusted_executable`,
    which adds it as a `uv`-only candidate on top of this shared list. This
    list is shared across every executable name, and a real account's
    `~/.local/bin` commonly contains a whole toolchain -- `git` included --
    installed by that same account, unlike hostedtoolcache (which
    structurally can only ever contain the pinned `uv`). Since
    `_validate_trusted_executable_version` is a no-op for names other than
    `uv`, widening this shared list to include account-home would let a
    same-UID attacker's lookalike `git` (or any other non-`uv` name
    resolved through this module) be trusted with zero version-pin defense
    (Issue #2280 Out of Scope: this module's `uv` trust boundary decision
    must not widen trust for any other executable name).

    This module does not claim to contain an attacker who already has
    arbitrary code execution as the same Unix account this process runs
    as -- such an attacker could modify any of these directories (or this
    module's own source) regardless of which ones are listed here; see
    `docs/dev/workflow.md` "Not Controlled". What this list *does* defend
    is "unintended executable selection": ambient `PATH` pollution and
    CWD/project-local substitution are excluded by construction. No new
    dedicated `trusted-bin` directory, sudo installation step, or
    repository-owned checksum authority is introduced by this lane.
    """
    return _dedupe_path_entries([*_trusted_toolchain_dirs("uv"), *_SYSTEM_STANDARD_PATH_DIRS])


# Fixed system standard directories, shared by `_safe_path_entries()` and
# `_resolve_trusted_executable`'s `uv`-only account-home extension below.
_SYSTEM_STANDARD_PATH_DIRS: tuple[str, ...] = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)


def _dedupe_path_entries(entries: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in entries:
        if entry and entry not in seen:
            seen.add(entry)
            ordered.append(entry)
    return ordered


# Fixed, hardcoded hosted-toolcache roots per trusted executable name. Not
# sourced from any environment variable (e.g. `UV_INSTALL_DIR`) -- a child
# process must never be able to widen its own trusted PATH by pointing an
# env var at an attacker-controlled directory (Issue #2241 rejected
# workaround list).
_TRUSTED_TOOLCHAIN_HOSTED_ROOTS: dict[str, Path] = {
    "uv": Path("/opt/hostedtoolcache/uv"),
}

# A trusted hostedtoolcache version-directory component must look like a
# version string (e.g. "0.4.30", "3.12.4"). This is a cheap defense-in-depth
# structural check ("version 照合") on top of the realpath/commonpath/
# ownership checks below -- it rejects directory names that were tampered
# with to smuggle something other than an actual toolchain version through
# the trust boundary.
_TOOLCHAIN_VERSION_DIR_RE = re.compile(r"^\d+(\.\d+){1,3}([+.\-][0-9A-Za-z.]+)?$")


def _trusted_toolchain_dirs(executable_name: str) -> list[str]:
    """Return trust-validated hostedtoolcache directories that may contain
    `executable_name`.

    Generalized from the former `uv`-only `_trusted_uv_toolcache_dirs`
    (Issue #2241) into a lookup keyed by `executable_name`, so future
    trusted toolchains only need an entry in
    `_TRUSTED_TOOLCHAIN_HOSTED_ROOTS` rather than a new bespoke resolver
    function.

    Each candidate is validated by:
      - regular-file type verification on the realpath-resolved target
        (rejects directories, FIFOs, sockets, devices, and symlinks that
        resolve to a non-regular file)
      - commonpath containment of the realpath-resolved target under the
        fixed trust root (rejects a symlink/copy pointing outside the
        hostedtoolcache root)
      - a version-shaped directory component ("version 照合")
      - ownership(uid) verification: the resolved regular file must be
        owned by root (uid 0 -- hostedtoolcache is root-installed in CI
        runner images) or by the account this process itself runs as
    """
    root = _TRUSTED_TOOLCHAIN_HOSTED_ROOTS.get(executable_name)
    if root is None or not root.is_dir():
        return []

    trusted_dirs: list[str] = []
    root_real = os.path.realpath(root)
    account_uid = os.getuid()
    for candidate in sorted(root.glob("*/x86_64")):
        version_component = candidate.parent.name
        if not _TOOLCHAIN_VERSION_DIR_RE.match(version_component):
            continue
        exe_path = candidate / executable_name
        if not exe_path.is_file() or not os.access(exe_path, os.X_OK):
            continue
        real = os.path.realpath(exe_path)
        try:
            real_st = os.stat(real)
        except OSError:
            continue
        if not stat.S_ISREG(real_st.st_mode):
            continue
        if real_st.st_uid not in (0, account_uid):
            continue
        if os.path.commonpath([root_real, real]) != root_real:
            continue
        trusted_dirs.append(str(candidate))
    return trusted_dirs


def _resolve_trusted_executable(name: str, project_root: str) -> str:
    """Resolve `name` to a trust-validated executable path.

    For "python3", the trust boundary is validated against the fully
    resolved (symlink-following) target, but the *returned* path preserves
    venv identity (`sys.executable` itself, unresolved) instead of the
    realpath. Returning the realpath here previously caused `uv run
    <realpath>` to lose association with the project venv whenever
    `sys.executable` was a symlink into a bare interpreter (e.g. a
    `uv python install`-managed toolchain with no project dependencies of
    its own), which made child subprocesses unable to import project
    dependencies (Issue #2073 root cause: jsonschema unimportable in CI).

    Check/use window (Issue #2073 human-review P1-2): returning the
    unresolved `sys.executable` path instead of its realpath means the
    string validated here (via `real`, below) and the string ultimately
    invoked by the caller are not byte-identical -- if something rewrote
    the `.venv/bin/python3` symlink between this call returning and the
    caller spawning the child process, the caller would exec whatever the
    symlink points to *at spawn time*, not the target validated here. This
    is a real TOCTOU window in the classic sense (CWE-367), but it cannot
    be closed by executing through an already-opened file descriptor
    (e.g. `/proc/self/fd/N` or `fexecve`) the way this module's other
    symlink-race guards do for *file reads*: CPython's own venv detection
    (`pyvenv.cfg` discovery, which determines `sys.prefix` and therefore
    which `site-packages` a spawned interpreter uses) walks *upward from
    the directory of the path it was invoked with*, so the invoked path
    string itself must remain `.../.venv/bin/python3` at exec time for the
    child to see the project's dependencies -- executing via a captured fd
    (which has no adjacent `pyvenv.cfg`) would silently reproduce the exact
    bug this function exists to fix. Given that, this function's only
    caller (`_resolve_child_argv`) re-resolves and re-validates on every
    single invocation immediately before the child is spawned (no caching,
    no reuse of a previously-resolved value across calls -- see
    `test_ac10_resolve_trusted_executable_is_not_cached_across_calls` in
    `tests/test_skill_runtime_preflight_bytecode_cache.py`), which bounds
    the window to the handful of Python statements between this function
    returning and `subprocess`/`Popen` being invoked, with no filesystem or
    network I/O in between. An attacker who can rewrite `.venv/bin/python3`
    inside that window already has write access to the repository working
    tree the executor itself operates in, which is a strictly stronger
    position than this check defends against elsewhere in this module.

    Pre-existing (not introduced by this fix) characteristic of the trust
    check itself, found while writing the regression test above: for
    `name == "python3"`, `runtime_dir` below is computed from `sys.executable`
    -- the exact same value being validated -- so `real_parent == runtime_dir`
    holds tautologically regardless of what the symlink resolves to, and the
    `{name}_outside_trusted_path` branch can never fire for `python3` (it is
    a real, load-bearing check only for `name == "uv"`, where `resolved` is
    independently looked up via `shutil.which`). The only check that
    actually constrains where `python3` may resolve is `{name}_inside_project_root`,
    below. This was true before this fix too (the pre-fix code computed
    `real`/`real_parent`/`runtime_dir` identically, all derived from
    `sys.executable`); this fix does not change which check is effective,
    only which string is returned once that check passes.
    """
    # The account-home `.local/bin` lane (Issue #2276 / #2280) is added on
    # top of the shared, name-agnostic `_safe_path_entries()` list ONLY for
    # `uv` -- never for `git`, `ssh`, or any other name resolved through
    # this function -- so that lane does not widen trust for executables
    # whose resolution has no version-pin defense (see `_safe_path_entries`
    # docstring). `_sanitize_env`/`sanitized_git_subprocess_env` also call
    # `_safe_path_entries()` directly (without this addition), so the
    # account-home lane never reaches the generic child `PATH` either.
    # Ordering: hostedtoolcache first (strongest validation), then the
    # account-home lane, then system standard directories -- so a
    # correctly pinned hostedtoolcache `uv` is preferred over the
    # local-dev convenience lane whenever both are present.
    if name == "uv":
        safe_entries = _dedupe_path_entries([
            *_trusted_toolchain_dirs("uv"),
            *_trusted_account_home_uv_dir(),
            *_SYSTEM_STANDARD_PATH_DIRS,
        ])
    else:
        safe_entries = _safe_path_entries()
    safe_path = os.pathsep.join(safe_entries)
    if name == "python3":
        resolved = sys.executable
    else:
        resolved = shutil.which(name, path=safe_path)
    if not resolved:
        if name == "uv":
            # Issue #2275: `uv_not_found` gets a structured diagnostic payload
            # instead of the bare `{name}_not_found` string used by every
            # other trusted executable name. `candidates_searched` is the
            # exact `safe_entries` list already built above for this `uv`
            # branch (never recomputed via `_safe_path_entries()`), so the
            # reported candidates are byte-identical to what was actually
            # passed to `shutil.which(name, path=safe_path)` -- no ambient
            # `PATH` entries and no account-home lane leaked in from the
            # shared, name-agnostic helper. `recommended_install_dir` is
            # derived from the real OS account home (`_os_account_home()`,
            # itself `pwd.getpwuid`-backed, never the ambient `HOME` env
            # var) and is reported even when that directory does not yet
            # exist -- unlike `candidates_searched`, which only ever
            # contains directories this process actually searched.
            account_home = _os_account_home()
            recommended_install_dir = (
                os.path.join(account_home, ".local", "bin") if account_home else None
            )
            diagnostic_payload = {
                "error": "uv_not_found",
                "candidates_searched": list(safe_entries),
                "expected_version": _required_uv_version(project_root),
                "recommended_install_dir": recommended_install_dir,
                "remediation_hint": (
                    "Install the pinned uv version with the official "
                    "standalone installer, specifying UV_INSTALL_DIR="
                    f"{recommended_install_dir or '<account-home>/.local/bin'} "
                    "so it lands in the no-sudo user-local lane, then "
                    "verify with `uv --version` for an exact match "
                    "against pyproject.toml's [tool.uv].required-version. "
                    "See docs/dev/workflow.md "
                    "'### Trusted uv のローカル開発復旧'."
                ),
            }
            raise RuntimeError("uv_not_found: " + json.dumps(diagnostic_payload))
        raise RuntimeError(f"{name}_not_found")
    real = os.path.realpath(resolved)
    project_root_real = os.path.realpath(project_root)
    if os.path.commonpath([project_root_real, real]) == project_root_real:
        raise RuntimeError(f"{name}_inside_project_root")
    allowed_dirs = {os.path.realpath(entry) for entry in safe_entries}
    real_parent = os.path.realpath(os.path.dirname(real))
    runtime_dir = os.path.realpath(str(Path(sys.executable).resolve().parent))
    if real_parent not in allowed_dirs and real_parent != runtime_dir:
        raise RuntimeError(f"{name}_outside_trusted_path")
    if name != "python3":
        # Issue #2241 / PR #2247 review P1-3(c) / Issue #2251 / Issue #2276:
        # a resolution that came from outside the strongly-validated
        # hostedtoolcache trust root (a system PATH directory such as
        # `/usr/local/bin`, or the real-account-home `.local/bin` lane
        # re-permitted by Issue #2276) gets an additional "version 照合"
        # defense-in-depth check -- see `_validate_trusted_executable_version`
        # docstring for why this is a confirmation, not a trust boundary, on
        # its own.
        hosted_dirs = {os.path.realpath(entry) for entry in _trusted_toolchain_dirs(name)}
        if real_parent not in hosted_dirs and not _validate_trusted_executable_version(name, real, project_root):
            raise RuntimeError(f"{name}_version_mismatch")
    return resolved if name == "python3" else real


def _sanitize_env(project_root: str, command_id: str = "") -> dict[str, str]:
    allowed_keys = {
        "GH_HOST",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    }
    # Only the closed offline fixture sibling may receive its isolated gh
    # configuration directory.  Production command environments retain their
    # existing allowlist and PATH/trusted-executable behavior.
    if command_id == "preflight.run.fixture.with_human_context":
        allowed_keys.add("GH_CONFIG_DIR")
    env = {
        key: value
        for key, value in os.environ.items()
        if value and (key in allowed_keys or key.startswith("SKILL_RUNTIME_TEST_"))
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CLAUDE_PROJECT_DIR"] = project_root
    env["PATH"] = os.pathsep.join(_safe_path_entries())
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def _validate_runtime_context(project_root: str, args: argparse.Namespace) -> Path:
    if os.path.realpath(os.getcwd()) != os.path.realpath(project_root):
        raise RuntimeError("cwd_not_canonical_main_root")
    branch = current_branch(project_root)
    default_branch = resolve_default_branch(project_root)
    if branch != default_branch:
        raise RuntimeError("root_not_default_branch")
    repo_slug = resolve_repo_slug(project_root)
    if repo_slug != TRUSTED_REPO_SLUG or args.repo != repo_slug:
        raise RuntimeError("repo_binding_mismatch")
    parsed = ExactSkillRuntimeCommand(
        command_id=args.command_id,
        issue_number=str(args.issue_number),
        repo=args.repo,
        argv=(),
    )
    if not command_allows_root_no_worktree(parsed):
        active_issue, entry = resolve_active_issue(project_root, project_root)
        if active_issue != str(args.issue_number):
            raise RuntimeError("active_issue_mismatch")
        if entry is None:
            raise RuntimeError("active_issue_worktree_missing")
    return _ensure_artifact_path_safe(project_root, str(args.issue_number), args.command_id)


def _resolve_child_argv(child_argv: Iterable[str]) -> list[str]:
    resolved = list(child_argv)
    if resolved[:3] == ["uv", "run", "python3"]:
        project_root = resolve_project_root()
        resolved[0] = _resolve_trusted_executable("uv", project_root)
        resolved[2] = _resolve_trusted_executable("python3", project_root)
    return resolved


_LEDGER_IMMUTABLE_TOP_LEVEL_FIELDS = ("ledger_schema", "generated_by", "coverage_scope")


def _read_bytes_or_none(path: Path) -> bytes | None:
    """Read a file's raw bytes, returning None on any OSError (including
    absent/unreadable) instead of raising -- callers treat None as "content
    could not be established" and fail closed accordingly."""
    try:
        return path.read_bytes()
    except OSError:
        return None


def _parse_ledger_bytes(data: bytes) -> dict | None:
    """Parse raw bytes as a JSON object. Returns None on any parse failure or
    if the top-level value is not an object."""
    try:
        parsed = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _load_ledger_json(path: Path) -> dict | None:
    """Read and parse a ledger JSON document. Returns None on any read/parse
    failure or if the top-level value is not an object -- callers must treat
    None as "not a valid ledger" and fail closed accordingly."""
    raw = _read_bytes_or_none(path)
    return _parse_ledger_bytes(raw) if raw is not None else None


def _is_valid_ledger_schema(data: dict) -> bool:
    """Minimal structural validation of a `SUBAGENT_LAUNCH_LEDGER_V1`
    document sufficient to compare two revisions of the same ledger safely.
    This is intentionally narrower than the full audit-mode validator in
    `check_subagent_launch_ledger.py` -- it only needs to reject documents
    where `launches` / `root_thread_actions` cannot be structurally compared
    as append-only lists."""
    return (
        data.get("ledger_schema") == "SUBAGENT_LAUNCH_LEDGER_V1"
        and isinstance(data.get("generated_by"), str)
        and isinstance(data.get("coverage_scope"), dict)
        and isinstance(data.get("launches"), list)
        and isinstance(data.get("root_thread_actions"), list)
    )


def _is_authorized_ledger_content_transition(before: dict, after: dict) -> bool:
    """Issue #1502 REQUEST_CHANGES (Blocker 3): a `regular -> regular`
    stable-ledger transition is authorized only when:

    - both the before and after content are valid `SUBAGENT_LAUNCH_LEDGER_V1`
      documents (a malformed replacement, e.g. `"not-json-at-all"`, fails
      closed);
    - the immutable top-level fields (`ledger_schema`, `generated_by`,
      `coverage_scope`) are byte-identical; and
    - `launches` and `root_thread_actions` are each a strict append: every
      existing before-entry is still present, unchanged, and in the same
      order in the after-list (deleting, reordering, or mutating an existing
      entry fails closed; only appending new valid entries is allowed).
    """
    if not _is_valid_ledger_schema(before) or not _is_valid_ledger_schema(after):
        return False
    for field in _LEDGER_IMMUTABLE_TOP_LEVEL_FIELDS:
        if before.get(field) != after.get(field):
            return False
    for key in ("launches", "root_thread_actions"):
        before_list = before[key]
        after_list = after[key]
        if len(after_list) < len(before_list):
            return False
        if after_list[: len(before_list)] != before_list:
            return False
    return True


# =============================================================================
# Typed shadow-log peer-append transition policy (Issue #1563).
#
# `.guard_shadow_log.jsonl` (repo root) is a peer file written by multiple
# independent hook producers (`.claude/hooks/shadow_log.py`,
# `.claude/hooks/guard-japanese-prose.sh`,
# `.claude/hooks/rtk_boundary_shadow_guard.sh`) and by
# `scripts/check-codex-agents.mjs`. It must NOT be added to
# `_RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS` above -- that symbol is a
# *directory-root* exclusion class that prunes the entire subtree before even
# inspecting the transition kind, so adding an exact file to it would make a
# symlink/directory/FIFO/socket/device replacement of that file invisible to
# this executor -- the exact opposite of Issue #1563 AC2. Instead this file
# gets its own narrow, exact-path, transition-typed policy, mirroring the
# `_LEDGER_*` typed policy above (Issue #1502 / PR #1552 pattern):
#
# - kind transition (`_is_allowed_shadow_log_kind_transition`): only
#   `absent -> absent`, `absent -> regular`, and `regular -> regular` are
#   authorized. Delete (`regular -> absent`) and any substitution into or out
#   of a non-regular kind (symlink / directory / FIFO / socket / device) fail
#   closed, regardless of before-kind (AC2).
# - content transition (`_is_authorized_shadow_log_content_transition`): a
#   `regular -> regular` byte change is authorized only when the new content
#   is a strict byte-level extension of the old content
#   (`after.startswith(before)`) AND both the before and after content parse
#   as well-formed JSONL (each complete line is a JSON object) AND every
#   parsed before-record is still present, unchanged, and in the same order
#   in the after-record list. Truncation, overwrite, malformed-JSONL
#   replacement, and record deletion or reordering all fail closed (AC3).
#
# Unlike the ledger's stable-exact-peer-file policy, no ancestor directory
# exemption is required here: `.guard_shadow_log.jsonl` lives directly at the
# project root, so its own transition never creates a new ancestor
# directory-node snapshot entry.
#
# stdlib snapshot mode provenance limitation (AC7): this policy runs on the
# stdlib-only race-tolerant snapshot model (a single fd-fstat-consistent
# before/after content read), so it cannot distinguish a regular
# guard_shadow_log.jsonl append performed by this executor's own child
# command's asynchronous peer hooks from an append made by a fully
# independent concurrent session/agent -- both are authorized identically as
# long as the transition is append-only; self-write and peer-write
# provenance are indistinguishable in this mode. The AC2 guarantee is
# strictly postcondition-based ("if a non-regular kind is observed at the
# end of the run, fail closed"), not a guarantee that the file was never
# replaced and replaced back before the final observation.
#
# PR #1572 REQUEST_CHANGES (Blocker 1: TOCTOU between the exact-path check
# and the generic repo-wide diff): the original implementation read the
# shadow-log "after" content and classified its "after" kind *before* the
# generic repo-wide snapshot/status ("after_snapshot" / "after_status") was
# captured, and unconditionally excluded `.guard_shadow_log.jsonl` from that
# later generic diff regardless of what happened to the path in the
# intervening gap. A path replaced (symlink, delete, truncate, overwrite)
# strictly between the exact-path content read and the generic snapshot
# capture would therefore have its later, unvalidated state silently
# excluded. `_find_unauthorized_repo_changes` now performs the exact-path
# shadow-log check *after* capturing `after_snapshot` / `after_status`, and
# `_shadow_log_stable_observation` below additionally guarantees that the
# kind/content it returns reflects a single, self-consistent filesystem
# generation (fd-fstat identity re-confirmed via a fresh `lstat()` after the
# read, with bounded retry on inconsistency) -- not two racing observations
# stitched together.
# =============================================================================

_SHADOW_LOG_EXACT_REL = ".guard_shadow_log.jsonl"

# Bounded retry budget for `_shadow_log_stable_observation` to absorb a
# legitimate in-flight peer write landing exactly inside the fd-fstat /
# final-lstat consistency window. Kept short: a real race here is on the
# order of a single syscall pair, not seconds.
#
# Issue #2243 AC7 (fixed-cutoff snapshot semantics, "B案"): each attempt
# below now reads only `[0, observed_end)` where `observed_end` is the size
# observed by `fstat()` at the top of that attempt -- it never waits for the
# stream to reach EOF, so a continuously-appending producer cannot indefinitely
# extend a single attempt's read. Retries here exist only to absorb kind
# replacement / truncation-below-`observed_end` races, not ongoing legitimate
# growth past the cutoff (growth past the cutoff is simply left for the next
# audit epoch to observe). Because a large file could still in principle be
# re-scanned once per retry attempt (e.g. under a truncate/replace/truncate
# adversarial loop), `_SHADOW_LOG_STABLE_OBSERVATION_DEADLINE_SECONDS` bounds
# the *wall-clock* cost of the whole retry loop as a second, independent
# guard -- so "25 attempts x one full cutoff-bound scan each" cannot become an
# unbounded-latency operation on an implausibly large file even in the
# adversarial-replace case.
_SHADOW_LOG_STABLE_OBSERVATION_ATTEMPTS = 25
_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS = 0.01
_SHADOW_LOG_STABLE_OBSERVATION_DEADLINE_SECONDS = 2.0

# Sentinel kind returned by `_shadow_log_stable_observation` when a
# self-consistent observation could not be made within the retry budget.
# This value is never a member of any authorized transition tuple in
# `_is_allowed_shadow_log_kind_transition`, so it always fails closed.
_SHADOW_LOG_KIND_UNSTABLE = "unstable"

# Issue #2243: the shadow log is an append-only file with many concurrent
# producers, so any *total-file-size* ceiling is guaranteed to eventually be
# reached in production and permanently fail-close the privileged executor
# (#2231). `_shadow_log_stable_observation` therefore no longer buffers file
# content into memory at all (no `chunks.append()` / `b"".join()` pattern):
# it streams the file in bounded chunks through a cryptographic digest
# (SHA-256, the repository's existing convention) and an incremental JSONL
# validity scanner, and never holds more than O(chunk size + per-record
# bound) bytes at any point, regardless of total file size.
#
# `_SHADOW_LOG_STREAM_CHUNK_BYTES` is the bounded read-chunk size used by the
# streaming scan.
_SHADOW_LOG_STREAM_CHUNK_BYTES = 1 << 20

# `_SHADOW_LOG_MAX_RECORD_BYTES` bounds the amount of buffered data held for
# a single not-yet-terminated JSONL line/record, so one pathologically large
# record cannot make working memory unbounded even though the overall file
# scan is streamed. This is a per-record memory bound, not a producer/consumer
# wire-format contract change: real shadow-log producer records observed in
# repository log evidence are ~400-610 bytes (max observed ~609 bytes as of
# Issue #2243), so a `regular -> regular` append composed entirely of records
# at or below this bound is always accepted regardless of where this constant
# is set within that range. 4MiB is chosen (raised from a prior 64KiB) purely
# as a generous compatibility safety margin above the ~609-byte observed
# maximum -- not because any existing or anticipated producer emits records
# anywhere near that size -- so this change strictly widens the accepted set
# (no previously-accepted record becomes rejected) and never narrows it.
_SHADOW_LOG_MAX_RECORD_BYTES = 4 * 1024 * 1024

# `_SHADOW_LOG_SAFETY_VALVE_MAX_BYTES` is a pure DoS safety valve, not the
# primary bounded-memory defense (that defense is the streaming scan itself,
# which never buffers content proportional to total file size). A file at or
# growing past this size still fails closed (sentinel
# `_SHADOW_LOG_KIND_UNSTABLE`, surfaced by the caller as the
# `unauthorized_write_path` reason code), but this is a deliberately new,
# much larger constant -- not a raised `_SHADOW_LOG_MAX_BYTES` -- because
# "just raise the cap" alone would not fix the underlying
# unbounded-heap-allocation defect (Issue #2243 Prohibited fixes). This valve
# only guards against an implausibly large file; it is not a retention,
# segmentation, or migration policy for the shadow log approaching this size
# in legitimate long-running operation -- that follow-up concern (rotation /
# segmentation / moving persistent runtime state outside the repository
# checkout once a file approaches this order of magnitude) is tracked
# separately in Issue #2252, not solved here.
_SHADOW_LOG_SAFETY_VALVE_MAX_BYTES = 512 * 1024 * 1024


class ShadowLogStreamCapture(NamedTuple):
    """Bounded-memory streaming observation of a shadow-log byte stream
    (Issue #2243). `sha256_hex` / `valid_jsonl` describe the *entire*
    observed stream. When the stream was observed with a `prefix_size`
    (i.e. as an "after" observation being compared against a known "before"
    length), `prefix_sha256_hex` is the digest of exactly the first
    `prefix_size` bytes (computed incrementally, without ever buffering
    that prefix as bytes) and `suffix_valid_jsonl` is the JSONL validity of
    exactly the bytes at or after that offset -- both are `None` when no
    `prefix_size` was requested, or when the stream turned out to be
    shorter than `prefix_size`."""

    total: int
    sha256_hex: str
    valid_jsonl: bool
    prefix_sha256_hex: str | None
    suffix_valid_jsonl: bool | None


class _JsonlLineScanner:
    """Incrementally validates a byte stream as well-formed JSON Lines,
    mirroring the exact well-formedness rules of `_parse_shadow_log_jsonl`
    (trailing newline required for non-empty content, no blank lines,
    UTF-8-decodable lines, `json.loads` with `_reject_shadow_log_json_constant`
    to reject the non-standard `NaN` / `Infinity` / `-Infinity` tokens, and
    every line must parse to a JSON object) -- but processes the stream
    line-by-line with a bounded per-line buffer (capped at
    `_SHADOW_LOG_MAX_RECORD_BYTES`) instead of holding the whole stream in
    memory. Splitting on the raw byte `b"\\n"` before UTF-8-decoding each
    completed line is safe: `0x0A` never appears inside a multi-byte UTF-8
    continuation sequence, so line boundaries found this way never split a
    multi-byte character."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._valid = True
        self._saw_any_byte = False
        self._pending_partial_line = False

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._saw_any_byte = True
        start = 0
        chunk_len = len(chunk)
        while True:
            idx = chunk.find(b"\n", start)
            if idx == -1:
                remainder = chunk[start:]
                if remainder:
                    if len(self._buf) + len(remainder) > _SHADOW_LOG_MAX_RECORD_BYTES:
                        # One record growing past the explicit per-record
                        # bound: fail closed, but never let the discarded
                        # remainder keep accumulating -- drop it so memory
                        # use stays bounded regardless of how large the
                        # offending record actually is on disk.
                        self._valid = False
                        self._buf.clear()
                    else:
                        self._buf.extend(remainder)
                self._pending_partial_line = True
                return
            line = bytes(self._buf) + chunk[start:idx]
            self._buf.clear()
            self._pending_partial_line = False
            if len(line) > _SHADOW_LOG_MAX_RECORD_BYTES:
                self._valid = False
            else:
                self._validate_line(line)
            start = idx + 1
            if start >= chunk_len:
                return

    def _validate_line(self, line: bytes) -> None:
        if not line:
            # A blank line is not a valid JSON value under the JSON Lines
            # specification.
            self._valid = False
            return
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            self._valid = False
            return
        try:
            obj = json.loads(text, parse_constant=_reject_shadow_log_json_constant)
        except (ValueError, RecursionError):
            # `RecursionError` (pathologically deeply nested JSON, e.g.
            # `[[[[...]]]]`) is caught alongside `ValueError` so a
            # stack-exhausting adversarial record fails closed as an
            # invalid record rather than propagating out of the scanner and
            # aborting the whole observation.
            self._valid = False
            return
        if not isinstance(obj, dict):
            self._valid = False

    def finalize(self) -> bool:
        if not self._valid:
            return False
        if not self._saw_any_byte:
            # Empty content is well-formed (zero records), matching
            # `_parse_shadow_log_jsonl(b"") == []`.
            return True
        # Non-empty content must end exactly on a line boundary (trailing
        # newline present, no dangling partial final line).
        return not self._pending_partial_line and len(self._buf) == 0


def _shadow_log_stream_capture(
    fd: int,
    prefix_size: int | None = None,
    read_upto: int | None = None,
) -> ShadowLogStreamCapture | None:
    """Stream-read `fd` in bounded `_SHADOW_LOG_STREAM_CHUNK_BYTES` chunks,
    computing a full-content SHA-256 digest and JSONL validity without ever
    buffering content proportional to total file size (Issue #2243). When
    `prefix_size` is given, also incrementally computes a digest over just
    the first `prefix_size` bytes and JSONL validity over just the bytes at
    or after that offset. Returns `None` if the stream exceeds
    `_SHADOW_LOG_SAFETY_VALVE_MAX_BYTES` (pure DoS safety valve, not the
    primary defense).

    Issue #2243 AC7 (fixed-cutoff snapshot semantics): when `read_upto` is
    given, this function reads *exactly* that many bytes (a fixed
    `fstat()`-observed cutoff) rather than reading until EOF -- it never
    blocks on / waits for a concurrently-appending writer to stop growing
    the file, so an append-only producer that keeps writing past the cutoff
    cannot prevent this read from completing. When `read_upto` is omitted
    (`None`), the stream is read to EOF (used by callers, and by direct unit
    tests, that want the whole current file content in a single call)."""
    full_hasher = hashlib.sha256()
    full_scanner = _JsonlLineScanner()
    prefix_hasher = hashlib.sha256() if prefix_size is not None else None
    suffix_scanner = _JsonlLineScanner() if prefix_size is not None else None
    total = 0
    while read_upto is None or total < read_upto:
        want = _SHADOW_LOG_STREAM_CHUNK_BYTES
        if read_upto is not None:
            want = min(want, read_upto - total)
        chunk = os.read(fd, want)
        if not chunk:
            # EOF (or, when `read_upto` is set, a short/absent read before
            # reaching the cutoff -- e.g. a concurrent truncation). Either
            # way there is nothing more to consume; the caller compares
            # `total` against the requested cutoff to decide whether this
            # was a clean, complete cutoff-bound read.
            break
        chunk_len = len(chunk)
        full_hasher.update(chunk)
        full_scanner.feed(chunk)
        chunk_start_offset = total
        total += chunk_len
        if total > _SHADOW_LOG_SAFETY_VALVE_MAX_BYTES:
            return None
        if prefix_size is not None:
            if chunk_start_offset < prefix_size:
                split = min(chunk_len, prefix_size - chunk_start_offset)
                prefix_hasher.update(chunk[:split])
                suffix_part = chunk[split:]
            else:
                suffix_part = chunk
            if suffix_part:
                suffix_scanner.feed(suffix_part)

    prefix_sha256_hex = None
    suffix_valid_jsonl = None
    if prefix_size is not None and total >= prefix_size:
        prefix_sha256_hex = prefix_hasher.hexdigest()  # type: ignore[union-attr]
        suffix_valid_jsonl = suffix_scanner.finalize()  # type: ignore[union-attr]

    return ShadowLogStreamCapture(
        total=total,
        sha256_hex=full_hasher.hexdigest(),
        valid_jsonl=full_scanner.finalize(),
        prefix_sha256_hex=prefix_sha256_hex,
        suffix_valid_jsonl=suffix_valid_jsonl,
    )


def _mode_kind(mode: int) -> str:
    """Classify a raw `st_mode` value into the same kind vocabulary as
    `_path_kind`, without re-`lstat()`-ing the path (used on an already
    captured `os.stat_result`, e.g. from `os.fstat()`)."""
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
        return "device"
    return "other"


def _shadow_log_stable_observation(
    path: Path,
    prefix_size: int | None = None,
) -> tuple[str, tuple[int, int, int, int] | None, ShadowLogStreamCapture | None]:
    """Return `(kind, identity, capture)` for the shadow-log path as a
    single, self-consistent filesystem-generation observation.

    - `kind`: `absent` / `symlink` / `dir` / `regular` / `fifo` / `socket` /
      `device` / `other` / `unstable` (see `_SHADOW_LOG_KIND_UNSTABLE`).
    - `identity`: `(st_dev, st_ino, st_size, st_mtime_ns)` for a non-absent
      path, else `None`.
    - `capture`: a `ShadowLogStreamCapture` bounded-memory streaming
      observation (digest + JSONL validity) of the file's content in
      `[0, observed_end)` when `kind == "regular"`, else `None`. Issue #2243:
      this REPLACES the file's raw buffered bytes that this function
      previously returned as its third element -- callers must not expect
      raw `bytes` here anymore.
    - `prefix_size`: when given and the observed content is at least this
      many bytes long, `capture.prefix_sha256_hex` / `capture.suffix_valid_jsonl`
      are additionally computed against this prefix offset (used by the
      "after" observation to validate a pure append against a known
      "before" length without re-buffering the "before" content).

    PR #1572 REQUEST_CHANGES Blocker 1: a plain `lstat()`-then-`open()`
    -then-`read()` sequence has a TOCTOU gap between classifying the kind
    and reading the content -- the path could be replaced (symlink, delete,
    truncate, overwrite) in that gap, and the caller would then authorize a
    transition based on content that no longer corresponds to the kind it
    classified (or vice versa). This helper closes that gap by opening with
    `O_NOFOLLOW | O_NONBLOCK` (never silently follows a symlink final
    component; never blocks indefinitely opening a FIFO with no writer),
    `fstat()`-ing the open descriptor to capture the identity of exactly the
    generation being read, streaming the content from that same descriptor
    (Issue #2243: bounded-memory, never buffered in full), and then
    re-`lstat()`-ing the path afterward to confirm its identity still
    matches what was `fstat()`'d.

    Issue #2243 AC7 (fixed-cutoff snapshot semantics, "B案", adopted via
    OWNER adversarial review of PR #2248): `fstat()` on the freshly opened
    descriptor fixes `observed_end` as this attempt's audit-epoch cutoff, and
    only `[0, observed_end)` is ever read here -- this function does NOT wait
    for the stream to reach EOF. A continuously-appending producer can extend
    the file arbitrarily far past `observed_end` while this attempt is
    running without ever causing this attempt to fail or retry: content
    appended at or after `observed_end` is simply left unobserved by this
    call and will be picked up as part of the *next* audit epoch (the next
    `_shadow_log_stable_observation` call, e.g. the caller's next "before"/
    "after" pair). This function guarantees only that `[0, observed_end)` was
    read from a single, self-consistent filesystem generation -- it does NOT
    guarantee "every byte present in the file at return time was validated".
    A bounded number of retries (both an attempt-count budget and, as a
    second independent bound, a wall-clock deadline -- see
    `_SHADOW_LOG_STABLE_OBSERVATION_DEADLINE_SECONDS`) absorbs a legitimate
    kind replacement / truncation-below-cutoff race landing exactly inside
    this narrow window; if the observation still cannot be made
    self-consistent within that budget, `_SHADOW_LOG_KIND_UNSTABLE` is
    returned, which always fails closed."""
    deadline_at = time.monotonic() + _SHADOW_LOG_STABLE_OBSERVATION_DEADLINE_SECONDS
    for _ in range(_SHADOW_LOG_STABLE_OBSERVATION_ATTEMPTS):
        if time.monotonic() >= deadline_at:
            # Issue #2243: an independent wall-clock bound on top of the
            # attempt-count budget, so a persistent adversarial
            # replace/truncate loop on an implausibly large file cannot cost
            # up to `_SHADOW_LOG_STABLE_OBSERVATION_ATTEMPTS` full
            # cutoff-bound scans' worth of latency -- it fails closed as soon
            # as either bound is exhausted, whichever comes first.
            break
        try:
            lst = path.lstat()
        except FileNotFoundError:
            return "absent", None, None

        if not stat.S_ISREG(lst.st_mode):
            try:
                confirm = path.lstat()
            except FileNotFoundError:
                time.sleep(_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS)
                continue
            if (confirm.st_dev, confirm.st_ino, confirm.st_mode) == (
                lst.st_dev,
                lst.st_ino,
                lst.st_mode,
            ):
                identity = (confirm.st_dev, confirm.st_ino, confirm.st_size, confirm.st_mtime_ns)
                return _mode_kind(confirm.st_mode), identity, None
            time.sleep(_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS)
            continue

        try:
            fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            time.sleep(_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS)
            continue
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENXIO):
                # ELOOP: a symlink was installed at the final path component
                # between the lstat() above and this open(). ENXIO: the path
                # was replaced by a socket (opening a UNIX-domain socket
                # special file with open(2) is not permitted on Linux) or by
                # a FIFO with O_NONBLOCK and no reader-compatible peer state.
                # Either way, re-observe from scratch.
                time.sleep(_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS)
                continue
            raise

        try:
            fstat_result = os.fstat(fd)
            if not stat.S_ISREG(fstat_result.st_mode):
                time.sleep(_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS)
                continue
            if fstat_result.st_size > _SHADOW_LOG_SAFETY_VALVE_MAX_BYTES:
                # Issue #2243: pure DoS safety valve, not the primary
                # bounded-memory defense (that is the streaming scan below,
                # which never buffers content proportional to file size).
                # An implausibly large file is still rejected outright
                # (fail closed, sentinel kind) without even attempting the
                # streaming scan. Follow-up: retention/segmentation/
                # repository-external migration once a file approaches this
                # order of magnitude is tracked separately in Issue #2252.
                return _SHADOW_LOG_KIND_UNSTABLE, None, None
            # Issue #2243 AC7: `observed_end` fixes this attempt's
            # audit-epoch cutoff at the size `fstat()` just observed. The
            # streaming capture below reads exactly `[0, observed_end)` and
            # never waits for EOF, so ongoing legitimate append past this
            # cutoff cannot block or fail this attempt.
            observed_end = fstat_result.st_size
            capture = _shadow_log_stream_capture(fd, prefix_size=prefix_size, read_upto=observed_end)
            if capture is None:
                # Exceeded the safety-valve bound while streaming (should not
                # happen given the `observed_end` cutoff and the check just
                # above, but fail closed defensively if it ever does).
                return _SHADOW_LOG_KIND_UNSTABLE, None, None
            if capture.total != observed_end:
                # A short read before reaching the cutoff (e.g. a concurrent
                # truncation racing the read) -- this attempt's content is
                # not a self-consistent `[0, observed_end)` snapshot. Do not
                # return it; re-observe from scratch (the `finally` below
                # still closes `fd` before this `continue` takes effect).
                time.sleep(_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS)
                continue
        finally:
            os.close(fd)

        try:
            final_lst = path.lstat()
        except FileNotFoundError:
            time.sleep(_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS)
            continue

        # Issue #2243 AC7: identity consistency no longer requires the
        # final `mtime` to be byte-identical to the `fstat()`-time `mtime`
        # (that would re-introduce the old "any concurrent write at all
        # fails this attempt" defect this redesign removes) -- it requires
        # only that (a) the path is still the same regular-file generation
        # (same `(st_dev, st_ino)`, ruling out a symlink/dir/FIFO/socket/
        # device substitution or an `os.replace()` onto a distinct inode),
        # and (b) the file was never observed to shrink below the cutoff
        # (`final_lst.st_size >= observed_end`, ruling out a truncation that
        # raced the read). Growth past `observed_end` between the `fstat()`
        # above and this `lstat()` is expected and authorized -- it is
        # simply outside this audit epoch's cutoff and will be covered by
        # the next one.
        identity_matches = (
            stat.S_ISREG(final_lst.st_mode)
            and (final_lst.st_dev, final_lst.st_ino) == (fstat_result.st_dev, fstat_result.st_ino)
            and final_lst.st_size >= observed_end
        )
        if identity_matches:
            identity = (fstat_result.st_dev, fstat_result.st_ino, observed_end, fstat_result.st_mtime_ns)
            return "regular", identity, capture
        time.sleep(_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS)

    return _SHADOW_LOG_KIND_UNSTABLE, None, None


def _is_allowed_shadow_log_kind_transition(before_kind: str, after_kind: str) -> bool:
    """`absent -> absent`, `absent -> regular`, and `regular -> regular` are
    the only authorized shadow-log kind transitions (AC2). Delete
    (`regular -> absent`) and substitution into/out of any non-regular kind
    (symlink / directory / FIFO / socket / device), from any before-kind, are
    rejected -- this is an explicit allow-tuple match, not a
    postcondition-only `after_kind == "regular"` check. `_SHADOW_LOG_KIND_UNSTABLE`
    (either side) is never a member of the allow-tuple set and therefore
    always fails closed."""
    if before_kind == "absent" and after_kind == "absent":
        return True
    return (before_kind, after_kind) in {("absent", "regular"), ("regular", "regular")}


def _reject_shadow_log_json_constant(constant: str) -> None:
    """`parse_constant` callback for `json.loads`: PR #1572 REQUEST_CHANGES
    Blocker 4. Python's `json` module accepts the non-standard tokens `NaN`,
    `Infinity`, and `-Infinity` by default (RFC 8259 / the JSON Lines
    specification do not permit them as JSON values). Raising here makes
    `json.loads` propagate a `ValueError` for any line containing one of
    these tokens instead of silently accepting it as a valid record."""
    raise ValueError(f"non_standard_json_constant:{constant}")


def _parse_shadow_log_jsonl(data: bytes) -> list[dict] | None:
    """Parse JSONL content into a list of record objects. Returns None if the
    content is not well-formed append-only JSONL: an incomplete final line
    (no trailing newline for non-empty content), a non-UTF-8 byte sequence, a
    blank line (JSON Lines requires every line to be a valid JSON value; an
    empty string is not one), a line that fails to parse as JSON, a line
    containing a non-standard `NaN` / `Infinity` / `-Infinity` constant
    (PR #1572 REQUEST_CHANGES Blocker 4), or a line whose parsed value is not
    a JSON object."""
    if not data:
        return []
    if not data.endswith(b"\n"):
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    records: list[dict] = []
    for line in text.split("\n")[:-1]:
        if not line:
            # PR #1572 REQUEST_CHANGES Blocker 4: a blank line is not a valid
            # JSON value under the JSON Lines specification, so it must be
            # rejected as malformed content, not silently skipped.
            return None
        try:
            obj = json.loads(line, parse_constant=_reject_shadow_log_json_constant)
        except (ValueError, RecursionError):
            # RecursionError (pathologically deeply nested JSON) fails
            # closed the same way a ValueError-raising malformed line does.
            return None
        if not isinstance(obj, dict):
            return None
        records.append(obj)
    return records


def _is_authorized_shadow_log_content_transition_capture(
    before: ShadowLogStreamCapture, after: ShadowLogStreamCapture
) -> bool:
    """A `regular -> regular` shadow-log content change is authorized only
    when it is a strict append (AC3), evaluated over bounded-memory
    streaming digests/validity (Issue #2243) instead of raw buffered bytes:
    `after`'s first `before.total` bytes must digest-match `before`'s full
    content (byte-for-byte prefix identity -- the same guarantee the prior
    `after.startswith(before)` byte-level check relied on, so this is not a
    weakening), and the appended tail must itself be complete, well-formed
    JSONL. Truncation, overwrite, malformed-JSONL replacement, and
    existing-record deletion/reordering are all rejected, whether or not
    the byte length changed. Inode identity (same-path-different-inode
    replacement, PR #1572 REQUEST_CHANGES Blocker 3) is validated
    separately by the caller before this function is reached."""
    if before.total == after.total and before.sha256_hex == after.sha256_hex:
        return True
    if after.total < before.total:
        return False
    if not before.valid_jsonl:
        return False
    if after.prefix_sha256_hex is None or after.prefix_sha256_hex != before.sha256_hex:
        return False
    return bool(after.suffix_valid_jsonl)


def _is_authorized_shadow_log_cold_start_content_capture(after: ShadowLogStreamCapture) -> bool:
    """PR #1572 REQUEST_CHANGES Blocker 2: an `absent -> regular` shadow-log
    creation must still be validated as well-formed JSONL content, not
    merely accepted because *some* regular file appeared. Returns True only
    when the full `after` content is well-formed JSONL (Issue #2243:
    `after.valid_jsonl` was computed by the bounded-memory streaming scan;
    see `_JsonlLineScanner` for the exact well-formedness contract,
    including the Blocker 4 blank-line / non-standard-constant
    rejections)."""
    return after.valid_jsonl


def _find_unauthorized_repo_changes(
    project_root: str,
    issue_number: str,
    before_snapshot: dict[str, tuple[str, int, int]],
    before_status: set[str],
    ledger_before_kinds: dict[str, str] | None = None,
    ledger_before_bytes: bytes | None = None,
    ledger_ancestor_before_kinds: dict[str, str] | None = None,
    shadow_log_before_kind: str | None = None,
    shadow_log_before_capture: ShadowLogStreamCapture | None = None,
    shadow_log_before_identity: tuple[int, int, int, int] | None = None,
    command_id: str = "",
) -> str | None:
    # Issue #1830: launch-ledger state is advisory telemetry. Keep its exact
    # paths out of the child-attribution diff, but never turn missing,
    # malformed, mixed, or concurrent ledger state into a routing failure.
    ledger_before_kinds = ledger_before_kinds or {}

    after_snapshot = _snapshot_repo_paths(project_root, issue_number, command_id)
    after_status = _git_status_paths(project_root)

    # Issue #1563 / PR #1572 REQUEST_CHANGES (Blocker 1): the shadow-log
    # exact-path typed transition is checked *after* the generic
    # `after_snapshot` / `after_status` capture above, not before it. The
    # earlier ordering read the shadow-log "after" kind/content, then
    # captured the generic repo-wide "after" state afterward while
    # unconditionally excluding the shadow-log path from that later diff --
    # a replacement (symlink, delete, truncate, overwrite) strictly between
    # the exact-path read and the generic capture would have its later,
    # unvalidated state silently excluded. Performing the exact-path
    # observation last means there is no unvalidated window left after it
    # for the excluded path to still change out from under this decision.
    resolved_shadow_log_before_kind = shadow_log_before_kind or "absent"
    shadow_log_path = Path(project_root) / _SHADOW_LOG_EXACT_REL
    # Issue #2243: when a "before" regular-file capture is available, pass
    # its length as the "after" observation's prefix_size so the streaming
    # scan can validate the append against it without ever re-buffering the
    # "before" content.
    shadow_log_prefix_size = (
        shadow_log_before_capture.total
        if resolved_shadow_log_before_kind == "regular" and shadow_log_before_capture is not None
        else None
    )
    shadow_log_after_kind, shadow_log_after_identity, shadow_log_after_capture = _shadow_log_stable_observation(
        shadow_log_path, prefix_size=shadow_log_prefix_size
    )
    if not _is_allowed_shadow_log_kind_transition(resolved_shadow_log_before_kind, shadow_log_after_kind):
        return _SHADOW_LOG_EXACT_REL
    if shadow_log_after_kind == "regular":
        if resolved_shadow_log_before_kind == "absent":
            # PR #1572 REQUEST_CHANGES Blocker 2: cold-start creation must
            # still be validated as well-formed JSONL content.
            if shadow_log_after_capture is None or not _is_authorized_shadow_log_cold_start_content_capture(
                shadow_log_after_capture
            ):
                return _SHADOW_LOG_EXACT_REL
        elif resolved_shadow_log_before_kind == "regular":
            # PR #1572 REQUEST_CHANGES Blocker 3: a `regular -> regular`
            # transition is only a genuine in-place append when the after
            # state is still the *same inode* as the before state -- an
            # `os.replace(tmp, shadow_log)` swap onto a distinct inode must
            # fail closed even if the replacement's bytes happen to be a
            # valid JSONL extension of the original content.
            if (
                shadow_log_before_identity is None
                or shadow_log_after_identity is None
                or shadow_log_before_identity[:2] != shadow_log_after_identity[:2]
            ):
                return _SHADOW_LOG_EXACT_REL
            if shadow_log_after_capture is None or shadow_log_before_capture is None:
                return _SHADOW_LOG_EXACT_REL
            if not _is_authorized_shadow_log_content_transition_capture(
                shadow_log_before_capture, shadow_log_after_capture
            ):
                return _SHADOW_LOG_EXACT_REL

    new_raw_status_paths = after_status - before_status
    # Issue #1409 REQUEST_CHANGES (P1): expand any collapsed ignored-ancestor
    # directory entries (e.g. `!! artifacts/`) into their real leaf paths
    # before applying race-tolerant-root exclusion, so cold-start creation of
    # a race-tolerant subtree under an ignored parent is not misreported as
    # an unauthorized write to the collapsed parent itself.
    expanded_new_status_paths = _expand_new_status_paths(
        project_root, new_raw_status_paths, issue_number, command_id
    )
    safe_ledger_ancestor_dir_rels = _safe_ledger_ancestor_dir_rels(project_root, ledger_ancestor_before_kinds)
    new_status_paths = {
        path
        for path in expanded_new_status_paths
        if not _is_under_allowed_artifact_root(project_root, issue_number, path, command_id)
        and not _is_race_tolerant_unattributable_path(path)
        and path not in _LEDGER_TYPED_EXACT_RELS
        and path != _SHADOW_LOG_EXACT_REL
        and path.rstrip("/") not in safe_ledger_ancestor_dir_rels
    }
    if new_status_paths:
        return sorted(
            new_status_paths,
            key=lambda item: (len(Path(item).parts), item),
        )[-1]
    if before_snapshot != after_snapshot:
        # Issue #1502 REQUEST_CHANGES (Blocker 4): the previous
        # implementation computed the symmetric-difference (create/delete) set
        # first and *skipped* the metadata-changed-for-existing-paths
        # computation whenever that symmetric difference was non-empty. That
        # meant a "ledger create" (or any other create/delete) happening in
        # the same invocation as an existing sibling's *content* update (same
        # path, different mtime/size) would silently drop the sibling update
        # from the diff. Always compute the union of both: paths that
        # appeared/disappeared, and paths that exist on both sides but whose
        # snapshot value differs.
        before_paths = set(before_snapshot)
        after_paths = set(after_snapshot)
        changed = sorted(
            (before_paths ^ after_paths)
            | {
                path
                for path in before_paths & after_paths
                if before_snapshot[path] != after_snapshot[path]
            }
        )
        # Issue #1502: the stable-exact ledger path is already authorized
        # above (regular -> regular content changes are expected peer
        # writes); the two transient `.lock` / `.tmp` paths are validated
        # separately via bounded quiescence before this function runs; and a
        # first-ever `absent -> regular` ledger transition also creates new
        # ancestor directory-node entries (`artifacts`, `artifacts/codex`)
        # that are a side effect of the already-authorized transition, not an
        # independent change. Drop all of these from the generic diff so an
        # authorized peer write is never reported as an unauthorized_write_path
        # false positive.
        filtered_changed = [
            item
            for item in changed
            if item not in _LEDGER_TYPED_EXACT_RELS
            and item != _SHADOW_LOG_EXACT_REL
            and item not in safe_ledger_ancestor_dir_rels
        ]
        if filtered_changed:
            return sorted(
                filtered_changed,
                key=lambda item: (len(Path(item).parts), item),
            )[-1]
        return None
    return None


def _repo_relative_path(project_root: str, path: str | Path) -> str:
    resolved = os.path.realpath(path)
    root_real = os.path.realpath(project_root)
    try:
        if os.path.commonpath([root_real, resolved]) == root_real:
            return os.path.relpath(resolved, root_real)
    except ValueError:
        pass
    return resolved


def _normalize_and_validate_runtime_env(project_root: str) -> list[tuple[str, str]]:
    worktrees_root = os.path.realpath(Path(project_root) / ".claude" / "worktrees")
    stale_entries: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for env_name in (
        "TMPDIR",
        "TEMP",
        "TMP",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
    ):
        env_value = os.environ.get(env_name)
        if not env_value:
            continue
        resolved = os.path.realpath(env_value)
        try:
            if os.path.commonpath([worktrees_root, resolved]) != worktrees_root:
                continue
        except ValueError:
            continue
        item = (env_name, _repo_relative_path(project_root, resolved))
        if item not in seen:
            seen.add(item)
            stale_entries.append(item)
    return stale_entries


def _parse_artifact_projection(stdout: str) -> list[str]:
    artifacts: list[str] = []
    collecting = False
    for line in stdout.splitlines():
        if line == "ARTIFACT:":
            collecting = True
            continue
        if not collecting:
            continue
        if not line.startswith("  "):
            break
        match = re.match(r"^\s{2}[^:]+:\s+(.+)$", line)
        if match:
            artifacts.append(match.group(1).strip())
    return artifacts


def _validate_stdout_artifact_projection(
    project_root: str, issue_number: str, stdout: str, command_id: str = ""
) -> list[str]:
    failures: list[str] = []
    root_real = os.path.realpath(project_root)
    for raw_path in _parse_artifact_projection(stdout):
        resolved = (
            os.path.realpath(raw_path)
            if os.path.isabs(raw_path)
            else os.path.realpath(Path(project_root) / raw_path)
        )
        rel_path = (
            os.path.relpath(resolved, root_real)
            if os.path.commonpath([root_real, resolved]) == root_real
            else resolved
        )
        if not _is_under_allowed_artifact_root(project_root, issue_number, rel_path, command_id):
            failures.append(_repo_relative_path(project_root, resolved))
    return failures


def _emit_stale_runtime_failure(issue_number: int, stale_entries: list[tuple[str, str]]) -> int:
    print(
        "SKILL_RUNTIME_FAIL: "
        f"reason_code=stale_worktree_runtime_state target_issue={issue_number} "
        f"stale_path={','.join(path for _, path in stale_entries)} "
        f"source_env={','.join(env for env, _ in stale_entries)} "
        "recovery=unset_or_correct_runtime_env_to_issue_artifacts_root",
        file=sys.stderr,
    )
    return 2


def _emit_artifact_projection_failure(issue_number: int, stale_paths: list[str]) -> int:
    print(
        "SKILL_RUNTIME_FAIL: "
        f"reason_code=stale_worktree_runtime_state target_issue={issue_number} "
        f"stale_path={','.join(stale_paths)} "
        "recovery=do_not_publish_artifact_projection_outside_issue_artifact_root",
        file=sys.stderr,
    )
    return 2


def _emit_unauthorized_write_failure(issue_number: int, unauthorized_path: str) -> int:
    print(
        "SKILL_RUNTIME_FAIL: "
        f"reason_code=unauthorized_write_path target_issue={issue_number} "
        f"unauthorized write path={unauthorized_path} "
        "recovery=do_not_write_outside_allowed_root",
        file=sys.stderr,
    )
    return 2


def _emit_ledger_transient_residue_failure(issue_number: int, stale_paths: list[str]) -> int:
    print(
        "SKILL_RUNTIME_FAIL: "
        f"reason_code=ledger_transient_residue_timeout target_issue={issue_number} "
        f"stale_path={','.join(sorted(stale_paths))} "
        "recovery=investigate_concurrent_ledger_writer_lock_or_temp_not_released",
        file=sys.stderr,
    )
    return 2


# ---------------------------------------------------------------------------
# Issue #2075: Popen-based outer-child supervisor.
#
# The previous implementation used `subprocess.run(child_argv, ...,
# timeout=timeout_seconds)` and reacted to `subprocess.TimeoutExpired` after
# the fact. That pattern cannot deliver a process-group cleanup guarantee:
# CPython's `subprocess.run()` already `kill()`s and `wait()`s its own direct
# child *before* re-raising `TimeoutExpired`, so by the time the `except`
# block runs there is no safe PID/PGID left for `os.killpg(...)` to act on
# (see the OWNER review on Issue #2075, and
# https://docs.python.org/3.12/library/subprocess.html).
#
# This module instead launches the child with `subprocess.Popen(...,
# start_new_session=True)` so the caller holds the child's PID/PGID from the
# moment it starts, and owns the full timeout + cleanup lifecycle:
#
#   execution timeout -> SIGTERM -> bounded grace -> SIGKILL
#     -> process-group absence verification -> direct-child (leader) reap
#
# Guarantee scope (Issue #2075 Outcome / OWNER review, narrowed from the
# original "reap the whole process tree" framing): only descendants that
# remain inside the executor-created process group (the common case for
# ordinary fork/exec descendants that never call `setsid()`/`setpgid()`
# themselves) are covered. A descendant that moves itself to a different
# session/process group is out of scope -- confirming its absence would
# require a subreaper/cgroup-level mechanism this module does not implement.
#
# `start_new_session` / `os.killpg` / `os.setsid` are POSIX-only. On a
# platform lacking them, this module falls back to direct-child-only
# termination and never reports `cleanup_status=confirmed_absent` (AC8):
# absence of a POSIX guarantee is reported as `unconfirmed`, not silently
# upgraded to a false "success".
# ---------------------------------------------------------------------------

_POSIX_PROCESS_GROUP_SUPPORTED = (
    os.name == "posix"
    and hasattr(os, "killpg")
    and hasattr(os, "setsid")
    and hasattr(os, "getpgid")
)

# Two independently bounded budgets govern outer-child supervision (Issue
# #2075 P1-4 OWNER review contract):
#
#   1. `timeout_seconds` (caller-supplied, per registry entry) bounds only
#      the *execution* wait -- i.e. `proc.communicate(timeout=...)` below.
#   2. `_CLEANUP_GRACE_SECONDS` is a separate, freshly-started budget for
#      the *entire* SIGTERM -> process-group-liveness poll -> SIGKILL ->
#      absence verification -> leader reap -> pipe close sequence, begun
#      only once the execution deadline has already been exceeded (or an
#      exception unwinds past a successful `Popen()`). It is never stacked
#      on top of the execution deadline, and no cleanup step below is ever
#      given a further, separate ad-hoc timeout on top of it -- every
#      cleanup step is bounded by `remaining(cleanup_deadline)`.
_CLEANUP_GRACE_SECONDS = 5.0
# Portion of `_CLEANUP_GRACE_SECONDS` allotted to waiting for SIGTERM to take
# effect before escalating to SIGKILL. Must stay smaller than
# `_CLEANUP_GRACE_SECONDS` so the escalation + absence-verification + reap
# steps that follow always retain some of the shared cleanup budget, instead
# of a SIGTERM-ignoring child silently consuming the entire cleanup deadline
# and leaving no time to ever send SIGKILL.
_TERM_GRACE_SECONDS = 2.0
_GROUP_POLL_INTERVAL_SECONDS = 0.02

CLEANUP_SCOPE_PROCESS_GROUP = "process_group"
CLEANUP_STATUS_CONFIRMED_ABSENT = "confirmed_absent"
CLEANUP_STATUS_UNCONFIRMED = "unconfirmed"
CLEANUP_STATUS_NOT_STARTED = "not_started"
TERMINATION_TERM = "term"
TERMINATION_TERM_THEN_KILL = "term_then_kill"
TERMINATION_NOT_NEEDED = "not_needed"


class _ChildSupervisionResult:
    """Outcome of `_run_child_with_supervision()` (Issue #2075)."""

    __slots__ = (
        "timed_out",
        "returncode",
        "stdout",
        "stderr",
        "cleanup_scope",
        "cleanup_status",
        "termination",
        "leader_reaped",
        "pid",
    )

    def __init__(
        self,
        *,
        timed_out: bool,
        returncode: int | None,
        stdout: str,
        stderr: str,
        cleanup_scope: str,
        cleanup_status: str,
        termination: str,
        leader_reaped: bool,
        pid: int | None = None,
    ) -> None:
        self.timed_out = timed_out
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.cleanup_scope = cleanup_scope
        self.cleanup_status = cleanup_status
        self.termination = termination
        self.leader_reaped = leader_reaped
        # `pid` is an introspection aid (not part of the closed telemetry
        # enum emitted by `_emit_timeout_failure()`) that lets a caller /
        # test independently confirm the direct child was actually reaped.
        self.pid = pid


def _remaining(deadline: float) -> float:
    """Return the non-negative seconds remaining until `deadline`."""
    return max(0.0, deadline - time.monotonic())


def _verify_process_group_absent(pgid: int) -> bool:
    """Return True only when `killpg(pgid, 0)` proves the group is gone
    (`ProcessLookupError`). Every other outcome -- the group is still alive,
    a `PermissionError`, or any other `OSError` -- must NOT be treated as
    confirmed absence (Issue #2075 AC6/AC9 fail-closed contract: cleanup
    success is never inferred from signal dispatch alone)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _poll_group_absent_until(proc: subprocess.Popen, pgid: int, deadline: float) -> bool:
    """Poll `_verify_process_group_absent(pgid)` until it is True or
    `deadline` elapses. Returns True iff absence was confirmed within the
    deadline.

    The escalation/absence decision itself is driven entirely by
    *process-group* liveness (`killpg(pgid, 0)`), never by leader
    (direct-child) liveness (Issue #2075 P1-1 OWNER review): a leader that
    exits early while a SIGTERM-ignoring descendant survives in the same
    process group must never suppress the SIGKILL escalation that follows.

    Each iteration also opportunistically calls `proc.poll()` (a
    non-blocking `waitpid()`) purely as a side effect so a leader that has
    already died does not linger as an unreaped zombie -- an unreaped
    zombie's PID slot still answers `kill(pid, 0)` successfully, which
    would otherwise make `killpg()` report the group as still alive even
    though every process in it has actually exited. This reap is
    incidental cleanup, not a liveness signal: it never gates the
    SIGTERM -> SIGKILL escalation decision above."""
    while True:
        proc.poll()
        if _verify_process_group_absent(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_GROUP_POLL_INTERVAL_SECONDS)


def _bounded_reap_leader(proc: subprocess.Popen, deadline: float) -> bool:
    """Best-effort `proc.wait()` bounded by `deadline`. Returns True iff the
    direct child leader was reaped (no zombie left behind)."""
    try:
        proc.wait(timeout=_remaining(deadline))
        return True
    except subprocess.TimeoutExpired:
        return False


def _bounded_close_pipes(proc: subprocess.Popen) -> None:
    """Best-effort close of the leader's stdout/stderr pipes so a
    descendant that still holds the write end open can never block this
    process from returning (Issue #2075 AC7: no partial output is ever
    surfaced on timeout regardless of how/when this closes)."""
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _stage_cleanup(
    proc: subprocess.Popen,
    pgid: int | None,
    *,
    posix_supervised: bool,
) -> tuple[str, str, str, bool]:
    """Run the full staged cleanup state machine -- SIGTERM, process-group
    liveness poll, SIGKILL escalation, absence verification, leader reap,
    pipe close -- under a single, freshly-started `cleanup_deadline`
    (`_CLEANUP_GRACE_SECONDS`) that is independent of whatever execution
    deadline just elapsed (Issue #2075 P1-4). Every step below is bounded
    by `remaining(cleanup_deadline)`; no step gets its own separate,
    unbounded-relative-to-the-caller ad-hoc timeout.

    Used from both the `TimeoutExpired` path and the `except BaseException`
    unwind path in `_run_child_with_supervision()` so that every exit path
    that follows a successful `Popen()` drives the process group through
    this exact same state machine (Issue #2075 P1-3).

    Returns `(cleanup_scope, cleanup_status, termination, leader_reaped)`.
    """
    cleanup_deadline = time.monotonic() + _CLEANUP_GRACE_SECONDS
    term_deadline = min(cleanup_deadline, time.monotonic() + _TERM_GRACE_SECONDS)

    group_supervised = posix_supervised and pgid is not None

    if group_supervised:
        termination = TERMINATION_NOT_NEEDED
        try:
            os.killpg(pgid, signal.SIGTERM)
            termination = TERMINATION_TERM
        except ProcessLookupError:
            termination = TERMINATION_NOT_NEEDED
        except OSError:
            termination = TERMINATION_TERM

        # P1-1: the SIGTERM -> SIGKILL escalation decision is driven by
        # *process-group* liveness, not leader liveness -- a leader that
        # exits (on its own, or from the SIGTERM) while a SIGTERM-ignoring
        # descendant survives in the same group must not suppress SIGKILL.
        group_absent = _poll_group_absent_until(proc, pgid, term_deadline)
        if not group_absent and time.monotonic() < cleanup_deadline:
            try:
                os.killpg(pgid, signal.SIGKILL)
                termination = TERMINATION_TERM_THEN_KILL
            except ProcessLookupError:
                pass
            except OSError:
                pass
            group_absent = _poll_group_absent_until(proc, pgid, cleanup_deadline)

        # AC6: absence is only ever reported `confirmed_absent` while
        # cleanup budget remains; deadline exhaustion is never silently
        # promoted to a confirmed success.
        if time.monotonic() >= cleanup_deadline:
            cleanup_status = CLEANUP_STATUS_UNCONFIRMED
        else:
            cleanup_status = (
                CLEANUP_STATUS_CONFIRMED_ABSENT if group_absent else CLEANUP_STATUS_UNCONFIRMED
            )
    else:
        # AC8: no `killpg`/`setsid` on this platform (or no pgid to
        # supervise) -- best-effort terminate the direct child only, and
        # never claim process-group absence was confirmed. There is no
        # POSIX guarantee here to confirm (P2-1: a failed pgid lookup must
        # never be promoted to `confirmed_absent` either).
        termination = TERMINATION_TERM
        try:
            proc.terminate()
        except OSError:
            pass
        if not _bounded_reap_leader(proc, term_deadline):
            termination = TERMINATION_TERM_THEN_KILL
            try:
                proc.kill()
            except OSError:
                pass
        cleanup_status = CLEANUP_STATUS_UNCONFIRMED

    leader_reaped = _bounded_reap_leader(proc, cleanup_deadline)
    _bounded_close_pipes(proc)

    return CLEANUP_SCOPE_PROCESS_GROUP, cleanup_status, termination, leader_reaped


def _run_child_with_supervision(
    child_argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout_seconds: float | int | None,
) -> _ChildSupervisionResult:
    """Launch `child_argv` under direct caller supervision.

    Normal-success semantics (stdout/stderr/returncode) are unchanged from
    the previous `subprocess.run(capture_output=True, text=True)` behavior
    (AC4): the execution wait itself is delegated to
    `proc.communicate(timeout=timeout_seconds)`, which preserves
    `subprocess.run()`'s own timeout/pipe-EOF/decode-error semantics
    exactly (Issue #2075 P1-2) -- including that a leader which exits while
    a descendant still holds the stdout/stderr pipe open keeps this call
    blocked (and, on timeout, still times out) rather than being mistaken
    for success, and that a `UnicodeDecodeError` from malformed child output
    propagates to the caller instead of being swallowed.

    On timeout (or any other exception unwinding past a successful
    `Popen()`, including `KeyboardInterrupt`), the process group is driven
    through `_stage_cleanup()`'s bounded state machine before returning /
    re-raising (Issue #2075 P1-3). On timeout, no partial stdout/stderr is
    ever surfaced (AC7) -- only cleanup telemetry is returned.
    """
    posix_supervised = _POSIX_PROCESS_GROUP_SUPPORTED

    popen_kwargs: dict[str, object] = dict(
        cwd=cwd,
        env=env,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if posix_supervised:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(child_argv, **popen_kwargs)  # type: ignore[arg-type]
    # P2-1: capture the process-group id at launch time, as `proc.pid`
    # itself -- not via a later `os.getpgid()` lookup. `start_new_session`
    # makes the leader its own session/group leader, so `proc.pid` *is* the
    # pgid from the instant `Popen()` returns; there is no window in which a
    # delayed `getpgid()` call could race, and a lookup failure can never be
    # (mis)promoted to `confirmed_absent` because this code path never
    # performs that lookup at all.
    pgid: int | None = proc.pid if posix_supervised else None

    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        cleanup_scope, cleanup_status, termination, leader_reaped = _stage_cleanup(
            proc, pgid, posix_supervised=posix_supervised
        )
        return _ChildSupervisionResult(
            timed_out=True,
            returncode=None,
            stdout="",
            stderr="",
            cleanup_scope=cleanup_scope,
            cleanup_status=cleanup_status,
            termination=termination,
            leader_reaped=leader_reaped,
            pid=proc.pid,
        )
    except BaseException:
        # P1-3: any other exception after a successful Popen() -- including
        # KeyboardInterrupt -- must still drive the process group through
        # the same bounded cleanup state machine before propagating, or a
        # detached process group (start_new_session=True) can outlive this
        # process entirely.
        _stage_cleanup(proc, pgid, posix_supervised=posix_supervised)
        raise
    else:
        return _ChildSupervisionResult(
            timed_out=False,
            returncode=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            cleanup_scope=CLEANUP_SCOPE_PROCESS_GROUP,
            cleanup_status=CLEANUP_STATUS_NOT_STARTED,
            termination=TERMINATION_NOT_NEEDED,
            leader_reaped=True,
            pid=proc.pid,
        )



def _emit_timeout_failure(
    issue_number: int,
    timeout_seconds: object,
    *,
    cleanup_scope: str = CLEANUP_SCOPE_PROCESS_GROUP,
    cleanup_status: str = CLEANUP_STATUS_NOT_STARTED,
    termination: str = TERMINATION_NOT_NEEDED,
    leader_reaped: bool = False,
) -> int:
    print(
        "SKILL_RUNTIME_FAIL: "
        f"reason_code=child_process_timeout target_issue={issue_number} "
        f"timeout_seconds={timeout_seconds} "
        f"cleanup_scope={cleanup_scope} "
        f"cleanup_status={cleanup_status} "
        f"termination={termination} "
        f"leader_reaped={'true' if leader_reaped else 'false'} "
        "recovery=investigate_child_process_hang_or_increase_registry_timeout",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="Privileged exact skill runtime executor", allow_abbrev=False
    )
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--fixture", required=False, default=None)
    parser.add_argument("--anchor-comment-url", required=False, default=None)
    parser.add_argument("--loop-state-file", required=False, default=None)
    parser.add_argument("--review-result-verdict", required=False, default=None)
    parser.add_argument("--max-iterations", required=False, default=None)
    # #2086 AC9/AC10: authority_transport.produce / authority_transport.consume.
    parser.add_argument("--invocation-id", required=False, default=None)
    parser.add_argument("--git-head-sha", required=False, default=None)
    parser.add_argument("--evidence-fixture-path", required=False, default=None)
    parser.add_argument("--router-receipt-path", required=False, default=None)
    parser.add_argument("--contract-patch-plan-file", required=False, default=None)
    parser.add_argument("--anchor-context-file", required=False, default=None)
    # Issue #2039 AC8/AC11: repair_action.apply.
    parser.add_argument("--apply-repair-action", required=False, default=None)
    # #2086 P0 fix_delta (Blocker 1/2): only ever meaningful for
    # preflight.run.with_human_context (the operator-selected human-context
    # lane) -- see skill_runtime_command_policy._parse_exact_skill_runtime_anchor_command.
    parser.add_argument("--investigation-evidence-transport-path", required=False, default=None)
    # #2086 P0 fix_delta (Blocker 3): decide.run "Mode B" -- the privileged
    # router additionally dispatching #2053's canonical authority-transport
    # verification (generate_router_receipt()) alongside its ordinary
    # loop-state decision, mirroring decide_next_loop_action.py's own
    # --authority-transport-path/--authority-expected CLI flags (added by PR
    # #2068, declared in command_registry.py's decide.run entry, but never
    # reachable through this executor before this fix_delta).
    parser.add_argument("--authority-transport-path", required=False, default=None)
    parser.add_argument("--authority-expected", action="store_true", default=False)
    # Argparse accepts duplicate options and keeps the final value.  Reject
    # the raw outer grammar first so a malformed invocation can never be
    # normalized into a valid child command.
    sibling_id = "preflight.run.fixture.with_human_context"
    if sibling_id in raw_argv or f"--command-id={sibling_id}" in raw_argv:
        raw_command = " ".join(
            ["uv", "run", "python3", SKILL_RUNTIME_EXEC_REL, *raw_argv]
        )
        if not is_exact_skill_runtime_anchor_fixture_executor_command(
            raw_command, resolve_project_root(), resolve_project_root()
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2

    args = parser.parse_args(raw_argv)

    project_root = resolve_project_root()
    stale_entries = _normalize_and_validate_runtime_env(project_root)
    if stale_entries:
        return _emit_stale_runtime_failure(args.issue_number, stale_entries)

    is_fixture_command = args.command_id == "preflight.run.fixture"
    is_fixture_human_context_command = (
        args.command_id == "preflight.run.fixture.with_human_context"
    )
    is_anchor_command = args.command_id in {
        "preflight.run.with_anchor",
        "preflight.run.with_human_context",
        "preflight.run.with_agent_report",
    }
    is_contract_update_command = args.command_id in {
        "contract_update.run.with_anchor",
        "contract_update.run.with_human_context",
    }
    is_decide_command = args.command_id == "decide.run"
    is_produce_command = args.command_id == "authority_transport.produce"
    is_consume_command = args.command_id == "authority_transport.consume"
    is_repair_action_apply_command = args.command_id == "repair_action.apply"
    # #2086 P0 fix_delta (Blocker 3): decide.run may ALSO carry
    # --invocation-id/--git-head-sha (bound into its Mode B authority-check
    # sub-fields), in addition to authority_transport.produce/consume.
    if not (is_produce_command or is_consume_command or is_decide_command) and (
        args.invocation_id
        or args.git_head_sha
        or args.evidence_fixture_path
        or args.router_receipt_path
        or args.contract_patch_plan_file
        or args.anchor_context_file
    ):
        print(
            "skill_runtime_exec: --invocation-id/--git-head-sha/--evidence-fixture-path/"
            "--router-receipt-path/--contract-patch-plan-file/--anchor-context-file are "
            "only allowed for authority_transport.produce/authority_transport.consume/decide.run",
            file=sys.stderr,
        )
        return 2
    if not is_repair_action_apply_command and args.apply_repair_action:
        print(
            "skill_runtime_exec: --apply-repair-action is only allowed for repair_action.apply",
            file=sys.stderr,
        )
        return 2
    if is_repair_action_apply_command and not args.apply_repair_action:
        print(
            "skill_runtime_exec: --apply-repair-action is required for repair_action.apply",
            file=sys.stderr,
        )
        return 2
    if not is_decide_command and (args.authority_transport_path or args.authority_expected):
        print(
            "skill_runtime_exec: --authority-transport-path/--authority-expected are "
            "only allowed for decide.run",
            file=sys.stderr,
        )
        return 2
    if is_fixture_human_context_command:
        if not args.fixture or not args.anchor_comment_url:
            print(
                "skill_runtime_exec: --fixture and --anchor-comment-url are required for "
                "preflight.run.fixture.with_human_context",
                file=sys.stderr,
            )
            return 2
        if args.loop_state_file or args.review_result_verdict or args.max_iterations:
            print("skill_runtime_exec: loop flags are not allowed for fixture human context", file=sys.stderr)
            return 2
        # Issue #2136 adversarial hardening (H3): validate the ORIGINAL,
        # un-serialized argparse values directly (argv-native), in addition
        # to (not instead of) the exact-parser gate below. The exact parser
        # only sees a re-tokenized `" ".join(...)` + `shlex.split()` round
        # trip of `command_tokens`, which is lossy for any value containing
        # shell-lexer-significant characters -- a value the round trip
        # mis-tokenizes could validate a different (truncated/shifted)
        # substring than the one actually forwarded to `render_command()`
        # and the real child subprocess below. Checking `args.fixture` /
        # `args.investigation_evidence_transport_path` here, before any
        # serialization, closes that validated-value-vs-used-value
        # divergence for this command class.
        if not _is_safe_issue_artifact_path(args.fixture, project_root, str(args.issue_number)):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
        if args.investigation_evidence_transport_path and not _is_safe_issue_artifact_path(
            args.investigation_evidence_transport_path, project_root, str(args.issue_number)
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
        command_tokens = [
            "uv", "run", "python3", SKILL_RUNTIME_EXEC_REL,
            "--command-id", args.command_id,
            "--issue-number", str(args.issue_number),
            "--repo", args.repo,
            "--fixture", args.fixture,
            "--anchor-comment-url", args.anchor_comment_url,
        ]
        if args.investigation_evidence_transport_path:
            command_tokens.extend([
                "--investigation-evidence-transport-path",
                args.investigation_evidence_transport_path,
            ])
        command_text = " ".join(command_tokens)
        if not is_exact_skill_runtime_anchor_fixture_executor_command(
            command_text, project_root, project_root
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_fixture_command:
        if not args.fixture:
            print("skill_runtime_exec: --fixture required for preflight.run.fixture", file=sys.stderr)
            return 2
        if args.anchor_comment_url:
            print(
                "skill_runtime_exec: --anchor-comment-url is not allowed for preflight.run.fixture",
                file=sys.stderr,
            )
            return 2
        if args.loop_state_file or args.review_result_verdict or args.max_iterations:
            print(
                "skill_runtime_exec: --loop-state-file/--review-result-verdict/"
                "--max-iterations are not allowed for preflight.run.fixture",
                file=sys.stderr,
            )
            return 2
        command_text = " ".join(
            [
                "uv",
                "run",
                "python3",
                SKILL_RUNTIME_EXEC_REL,
                "--command-id",
                args.command_id,
                "--issue-number",
                str(args.issue_number),
                "--repo",
                args.repo,
                "--fixture",
                args.fixture,
            ]
        )
        if not is_exact_skill_runtime_fixture_executor_command(command_text, project_root, project_root):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_anchor_command or is_contract_update_command:
        if args.fixture:
            print(
                "skill_runtime_exec: --fixture is not allowed for anchor runtime commands",
                file=sys.stderr,
            )
            return 2
        if not args.anchor_comment_url:
            print(
                "skill_runtime_exec: --anchor-comment-url required for anchor runtime commands",
                file=sys.stderr,
            )
            return 2
        if args.loop_state_file or args.review_result_verdict or args.max_iterations:
            print(
                "skill_runtime_exec: --loop-state-file/--review-result-verdict/"
                "--max-iterations are not allowed for anchor runtime commands",
                file=sys.stderr,
            )
            return 2
        if args.investigation_evidence_transport_path and args.command_id != "preflight.run.with_human_context":
            print(
                "skill_runtime_exec: --investigation-evidence-transport-path is only "
                "allowed for preflight.run.with_human_context",
                file=sys.stderr,
            )
            return 2
        command_text = " ".join(
            [
                "uv",
                "run",
                "python3",
                SKILL_RUNTIME_EXEC_REL,
                "--command-id",
                args.command_id,
                "--issue-number",
                str(args.issue_number),
                "--repo",
                args.repo,
                "--anchor-comment-url",
                args.anchor_comment_url,
                *(
                    ["--human-context-comment-url", args.anchor_comment_url]
                    if args.command_id in {"preflight.run.with_human_context", "contract_update.run.with_human_context"}
                    else ["--agent-report-comment-url", args.anchor_comment_url]
                    if args.command_id == "preflight.run.with_agent_report"
                    else []
                ),
                *(
                    ["--investigation-evidence-transport-path", args.investigation_evidence_transport_path]
                    if args.investigation_evidence_transport_path
                    else []
                ),
            ]
        )
        exact_anchor_command = (
            is_exact_skill_runtime_anchor_executor_command(command_text, project_root, project_root)
            if is_anchor_command
            else is_exact_skill_runtime_contract_update_anchor_executor_command(
                command_text, project_root, project_root
            )
        )
        if not exact_anchor_command:
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_decide_command:
        if args.fixture or args.anchor_comment_url:
            print(
                "skill_runtime_exec: --fixture/--anchor-comment-url are not allowed for decide.run",
                file=sys.stderr,
            )
            return 2
        if not args.loop_state_file or not args.review_result_verdict:
            print(
                "skill_runtime_exec: --loop-state-file and --review-result-verdict "
                "are required for decide.run",
                file=sys.stderr,
            )
            return 2
        # #2086 P0 fix_delta (Blocker 3): decide.run "Mode B" -- authority
        # sub-fields must be supplied all-or-none (fail-closed on any
        # partial/mixed set), never a generic passthrough of individual
        # optional registry fields. `--authority-expected` is included in
        # this all-or-none set (not treated as independently optional) so a
        # Mode B invocation is always unambiguous about whether a missing/
        # malformed manifest should fail closed.
        _decide_authority_fields = (
            args.invocation_id,
            args.git_head_sha,
            args.authority_transport_path,
        )
        _decide_authority_present = any(_decide_authority_fields) or args.authority_expected
        _decide_authority_complete = all(_decide_authority_fields) and args.authority_expected
        is_decide_authority_mode = False
        if _decide_authority_present:
            if not _decide_authority_complete:
                print(
                    "skill_runtime_exec: decide.run Mode B requires "
                    "--invocation-id, --git-head-sha, --authority-transport-path, "
                    "and --authority-expected all together (all-or-none)",
                    file=sys.stderr,
                )
                return 2
            is_decide_authority_mode = True
        max_iterations = args.max_iterations or "3"
        command_tokens = [
            "uv",
            "run",
            "python3",
            SKILL_RUNTIME_EXEC_REL,
            "--command-id",
            args.command_id,
            "--issue-number",
            str(args.issue_number),
            "--repo",
            args.repo,
            "--loop-state-file",
            args.loop_state_file,
            "--review-result-verdict",
            args.review_result_verdict,
            "--max-iterations",
            max_iterations,
        ]
        if is_decide_authority_mode:
            # These represent skill_runtime_exec.py's OWN new CLI flags on
            # the OUTER invocation (matched by the exact parser below) --
            # not a second --issue-number/--repo pair. The existing
            # --issue-number/--repo already parsed above are the ones
            # forwarded to decide_next_loop_action.py's own authority
            # sub-fields via render_params below.
            command_tokens += [
                "--authority-transport-path",
                args.authority_transport_path,
                "--authority-expected",
                "--invocation-id",
                args.invocation_id,
                "--git-head-sha",
                args.git_head_sha,
            ]
        command_text = " ".join(command_tokens)
        if is_decide_authority_mode:
            exact_decide_command = is_exact_skill_runtime_decide_authority_executor_command(
                command_text, project_root, project_root
            )
        else:
            exact_decide_command = is_exact_skill_runtime_decide_executor_command(
                command_text, project_root, project_root
            )
        if not exact_decide_command:
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_produce_command:
        if (
            args.fixture
            or args.anchor_comment_url
            or args.loop_state_file
            or args.review_result_verdict
            or args.max_iterations
        ):
            print(
                "skill_runtime_exec: only --invocation-id/--git-head-sha/"
                "--evidence-fixture-path are allowed for authority_transport.produce",
                file=sys.stderr,
            )
            return 2
        if not args.invocation_id or not args.git_head_sha or not args.evidence_fixture_path:
            print(
                "skill_runtime_exec: --invocation-id, --git-head-sha, and "
                "--evidence-fixture-path are required for authority_transport.produce",
                file=sys.stderr,
            )
            return 2
        command_text = " ".join(
            [
                "uv",
                "run",
                "python3",
                SKILL_RUNTIME_EXEC_REL,
                "--command-id",
                args.command_id,
                "--issue-number",
                str(args.issue_number),
                "--repo",
                args.repo,
                "--invocation-id",
                args.invocation_id,
                "--git-head-sha",
                args.git_head_sha,
                "--produce-authority-transport",
                args.evidence_fixture_path,
            ]
        )
        if not is_exact_skill_runtime_authority_transport_produce_executor_command(
            command_text, project_root, project_root
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_consume_command:
        if (
            args.fixture
            or args.anchor_comment_url
            or args.loop_state_file
            or args.review_result_verdict
            or args.max_iterations
        ):
            print(
                "skill_runtime_exec: only --invocation-id/--git-head-sha/"
                "--router-receipt-path/--contract-patch-plan-file/--anchor-context-file "
                "are allowed for authority_transport.consume",
                file=sys.stderr,
            )
            return 2
        if not args.invocation_id or not args.git_head_sha or not args.router_receipt_path:
            print(
                "skill_runtime_exec: --invocation-id, --git-head-sha, and "
                "--router-receipt-path are required for authority_transport.consume",
                file=sys.stderr,
            )
            return 2
        if bool(args.contract_patch_plan_file) != bool(args.anchor_context_file):
            print(
                "skill_runtime_exec: --contract-patch-plan-file and "
                "--anchor-context-file must be supplied together or not at all "
                "for authority_transport.consume",
                file=sys.stderr,
            )
            return 2
        consume_tail = [
            "uv",
            "run",
            "python3",
            SKILL_RUNTIME_EXEC_REL,
            "--command-id",
            args.command_id,
            "--issue-number",
            str(args.issue_number),
            "--repo",
            args.repo,
            "--invocation-id",
            args.invocation_id,
            "--git-head-sha",
            args.git_head_sha,
            "--consume-authority-transport",
            args.router_receipt_path,
        ]
        if args.contract_patch_plan_file and args.anchor_context_file:
            consume_tail += [
                "--contract-patch-plan-file",
                args.contract_patch_plan_file,
                "--anchor-context-file",
                args.anchor_context_file,
            ]
        command_text = " ".join(consume_tail)
        if not is_exact_skill_runtime_authority_transport_consume_executor_command(
            command_text, project_root, project_root
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_repair_action_apply_command:
        if (
            args.fixture
            or args.anchor_comment_url
            or args.loop_state_file
            or args.review_result_verdict
            or args.max_iterations
        ):
            print(
                "skill_runtime_exec: only --apply-repair-action is allowed for "
                "repair_action.apply",
                file=sys.stderr,
            )
            return 2
        command_text = " ".join(
            [
                "uv",
                "run",
                "python3",
                SKILL_RUNTIME_EXEC_REL,
                "--command-id",
                args.command_id,
                "--issue-number",
                str(args.issue_number),
                "--repo",
                args.repo,
                "--apply-repair-action",
                args.apply_repair_action,
            ]
        )
        if not is_exact_skill_runtime_repair_action_apply_executor_command(
            command_text, project_root, project_root
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    else:
        if args.fixture:
            print("skill_runtime_exec: --fixture is only allowed for preflight.run.fixture", file=sys.stderr)
            return 2
        if args.anchor_comment_url:
            print(
                "skill_runtime_exec: --anchor-comment-url is only allowed for "
                "an anchor-bound preflight profile",
                file=sys.stderr,
            )
            return 2
        if args.loop_state_file or args.review_result_verdict or args.max_iterations:
            print(
                "skill_runtime_exec: --loop-state-file/--review-result-verdict/"
                "--max-iterations are only allowed for decide.run",
                file=sys.stderr,
            )
            return 2
        command_text = " ".join(
            [
                "uv",
                "run",
                "python3",
                SKILL_RUNTIME_EXEC_REL,
                "--command-id",
                args.command_id,
                "--issue-number",
                str(args.issue_number),
                "--repo",
                args.repo,
            ]
        )
        if not is_exact_skill_runtime_executor_command(command_text, project_root, project_root):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2

    _validate_runtime_context(project_root, args)
    before_snapshot = _snapshot_repo_paths(project_root, str(args.issue_number), args.command_id)
    before_status = _git_status_paths(project_root)
    ledger_before_kinds = _ledger_exact_kinds(project_root)
    ledger_ancestor_before_kinds = _ledger_ancestor_kinds(project_root)
    ledger_before_bytes = (
        _read_bytes_or_none(Path(project_root) / _LEDGER_STABLE_EXACT_REL)
        if ledger_before_kinds.get(_LEDGER_STABLE_EXACT_REL) == "regular"
        else None
    )
    (
        shadow_log_before_kind,
        shadow_log_before_identity,
        shadow_log_before_capture,
    ) = _shadow_log_stable_observation(Path(project_root) / _SHADOW_LOG_EXACT_REL)

    entry = load_registry_entry(args.command_id, project_root)
    validate_registry_entry(args.command_id, entry, str(args.issue_number))

    registry_path = Path(project_root) / REGISTRY_REL
    if registry_path.is_symlink():
        raise RuntimeError("registry_symlink_not_allowed")
    if not registry_path.is_file():
        raise RuntimeError("registry_missing")

    # #2086 AC10: decide.run dispatches decide_next_loop_action.py, not
    # run_refinement_preflight.py -- the pre-existing integrity/symlink
    # check below must validate the script the command_id actually reaches,
    # otherwise decide.run could never pass this check even though it never
    # touches run_refinement_preflight.py.
    script_path = (
        Path(project_root) / ".claude" / "skills" / "issue-refinement-loop"
        / "scripts"
        / ("decide_next_loop_action.py" if is_decide_command else "run_refinement_preflight.py")
    )
    if script_path.is_symlink() or not script_path.is_file():
        raise RuntimeError("preflight_script_invalid")

    from importlib.util import spec_from_file_location, module_from_spec

    spec = spec_from_file_location("issue_refinement_command_registry_executor", str(registry_path))
    if spec is None or spec.loader is None:
        raise RuntimeError("registry_spec_invalid")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    render_command = getattr(module, "render_command", None)
    if not callable(render_command):
        raise RuntimeError("render_command_missing")
    # #2086 AC10 P0: `decide.run`'s registry entry declares only
    # loop_state_file/verdict/max_iterations placeholders (it has no
    # `{issue_number}`/`{repo}` tokens in its argv template -- unlike every
    # other command_id here). render_command() fail-closed-rejects any
    # extra params not in the entry's declared `placeholders`, so
    # unconditionally seeding `issue_number`/`repo` into render_params
    # made every real decide.run dispatch raise `ValueError` before ever
    # reaching a subprocess. This was masked by the previous test's stub
    # `render_command()`, which silently ignored undeclared params instead
    # of validating them (see test_decide_run_reaches_real_subprocess).
    if is_decide_command:
        render_params: dict[str, object] = {
            "loop_state_file": args.loop_state_file,
            "verdict": args.review_result_verdict,
            "max_iterations": args.max_iterations or "3",
        }
        # #2086 P0 fix_delta (Blocker 3): Mode B -- forward the authority
        # sub-fields to decide_next_loop_action.py's own
        # --issue-number/--repo/--authority-transport-path/
        # --authority-expected/--invocation-id/--git-head-sha flags
        # (command_registry.py decide.run entry, PR #2068). Only populated
        # when the pre-dispatch all-or-none check above accepted a
        # complete Mode B field set.
        if is_decide_authority_mode:
            render_params["issue_number"] = args.issue_number
            render_params["repo"] = args.repo
            render_params["authority_transport_manifest_path"] = args.authority_transport_path
            render_params["authority_expected"] = True
            render_params["invocation_id"] = args.invocation_id
            render_params["git_head_sha"] = args.git_head_sha
    elif is_produce_command:
        # #2086 AC9/AC10: producer role -- issue_number/repo are NOT seeded
        # here (unlike the generic `else` branch below) because
        # `authority_transport.produce`'s own render_params below already
        # supplies them alongside its own required placeholders.
        render_params = {
            "issue_number": args.issue_number,
            "repo": args.repo,
            "invocation_id": args.invocation_id,
            "git_head_sha": args.git_head_sha,
            "evidence_fixture_path": args.evidence_fixture_path,
        }
    elif is_consume_command:
        render_params = {
            "issue_number": args.issue_number,
            "repo": args.repo,
            "invocation_id": args.invocation_id,
            "git_head_sha": args.git_head_sha,
            "router_receipt_path": args.router_receipt_path,
        }
        if args.contract_patch_plan_file and args.anchor_context_file:
            render_params["contract_patch_plan_file"] = args.contract_patch_plan_file
            render_params["anchor_context_file"] = args.anchor_context_file
    elif is_repair_action_apply_command:
        render_params = {
            "issue_number": args.issue_number,
            "repo": args.repo,
            "preflight_result_path": args.apply_repair_action,
        }
    else:
        render_params = {"issue_number": args.issue_number, "repo": args.repo}
        if is_fixture_command or is_fixture_human_context_command:
            render_params["fixture"] = args.fixture
        if is_anchor_command or is_contract_update_command or is_fixture_human_context_command:
            render_params["anchor_comment_url"] = args.anchor_comment_url
            if args.investigation_evidence_transport_path:
                render_params["investigation_evidence_transport_path"] = (
                    args.investigation_evidence_transport_path
                )
    child_argv = render_command(args.command_id, render_params)
    child_argv = _resolve_child_argv(child_argv)

    timeout_seconds = entry.get("timeout_seconds")
    supervision = _run_child_with_supervision(
        child_argv,
        cwd=project_root,
        env=_sanitize_env(project_root, args.command_id),
        timeout_seconds=timeout_seconds,
    )
    if supervision.timed_out:
        return _emit_timeout_failure(
            args.issue_number,
            timeout_seconds,
            cleanup_scope=supervision.cleanup_scope,
            cleanup_status=supervision.cleanup_status,
            termination=supervision.termination,
            leader_reaped=supervision.leader_reaped,
        )
    result = supervision

    # Issue #1502 AC3: wait a bounded window for the writer's own `.lock` /
    # `.tmp` transient protocol entries to vanish before evaluating the
    # generic diff. This must run before `_find_unauthorized_repo_changes`
    # takes its "after" snapshot, so quiescent peer writes never appear as
    # residue in that snapshot.
    # Advisory ledger residue may be reported by diagnostics, but it must not
    # block the wrapped workflow command.

    unauthorized_path = _find_unauthorized_repo_changes(
        project_root,
        str(args.issue_number),
        before_snapshot,
        before_status,
        ledger_before_kinds,
        ledger_before_bytes,
        ledger_ancestor_before_kinds,
        shadow_log_before_kind,
        shadow_log_before_capture,
        shadow_log_before_identity,
        args.command_id,
    )
    if unauthorized_path is not None:
        return _emit_unauthorized_write_failure(args.issue_number, unauthorized_path)

    artifact_projection_failures = _validate_stdout_artifact_projection(
        project_root,
        str(args.issue_number),
        result.stdout,
        args.command_id,
    )
    if artifact_projection_failures:
        return _emit_artifact_projection_failure(args.issue_number, artifact_projection_failures)

    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode

# ---------------------------------------------------------------------------
# Issue #2196 (Child 1 of #2190): dedicated sanitized Git subprocess
# execution utility. Not yet wired into any production command dispatch
# path -- later children (#2197/#2198/#2199/#2200) are the ones that will
# call the closed `run_control_plane_git_*` builders below for their own
# dedicated Git subprocesses. This Issue only establishes and self-tests
# the utility. Per PR #2201's owner adversarial review (P1-3), there is no
# production-callsite API that accepts a raw `list[str]` argv: the only
# entry points are the closed, typed builders at the bottom of this
# section, each of which only accepts a fixed, validated subcommand shape.
# ---------------------------------------------------------------------------

_GIT_SUBPROCESS_EXECUTABLE_CACHE: str | None = None


def resolve_git_subprocess_executable(project_root: str) -> str:
    """Resolve the absolute path to the trusted `git` executable exactly
    once per process and reuse the cached value for every subsequent
    dedicated Git subprocess invocation (Issue #2196 AC2). Resolving only
    once (instead of re-resolving on every call, the way
    `_resolve_child_argv`/`_resolve_trusted_executable` deliberately do for
    `uv`/`python3` to defend against a TOCTOU venv-symlink rewrite) means a
    PATH or symlink change made mid-session cannot silently swap which
    `git` binary later dedicated-subprocess invocations run -- every
    invocation within this process keeps using the exact same absolute
    path string that was validated at first resolution.
    """
    global _GIT_SUBPROCESS_EXECUTABLE_CACHE
    if _GIT_SUBPROCESS_EXECUTABLE_CACHE is None:
        _GIT_SUBPROCESS_EXECUTABLE_CACHE = _resolve_trusted_executable("git", project_root)
    return _GIT_SUBPROCESS_EXECUTABLE_CACHE


def _reset_git_subprocess_executable_cache_for_tests() -> None:
    """Test-only reset hook. Production code never needs to call this --
    the whole point of AC2 is that the cache is never invalidated within a
    process lifetime."""
    global _GIT_SUBPROCESS_EXECUTABLE_CACHE
    _GIT_SUBPROCESS_EXECUTABLE_CACHE = None


def _resolve_trusted_ssh_command() -> str:
    """Resolve `GIT_SSH_COMMAND` to an absolute, trusted `ssh` executable
    resolved only from the same trusted `PATH` allowlist used for the rest
    of the dedicated Git subprocess environment (Issue #2196 AC7 / P1-2).
    Never falls back to an unqualified `"ssh"` token, which would resolve
    via whatever `PATH` the child process happened to inherit -- if no
    trusted `ssh` executable can be found, `GIT_SSH_COMMAND` is pointed at
    `false` (a command that always fails) instead, so an `ssh://` remote
    can never silently succeed through an unvetted `ssh`."""
    safe_path = os.pathsep.join(_safe_path_entries())
    ssh_path = shutil.which("ssh", path=safe_path)
    if ssh_path:
        return f"{ssh_path} -oBatchMode=yes -oStrictHostKeyChecking=yes"
    return "false"


def sanitized_git_subprocess_env(project_root: str) -> dict[str, str]:
    """Build the environment for a dedicated Git subprocess (Issue #2196
    AC1/AC5/AC7).

    Every key in `GIT_SUBPROCESS_UNSET_ENV_KEYS` is guaranteed absent from
    the returned mapping -- built by copying `os.environ` and excluding
    those keys, never by overwriting them with an empty string, so no
    caller-controlled value for any of them can survive into the child
    process. `PATH` is replaced entirely with the same trusted allowlist
    used to resolve the `git` executable itself (never the inherited
    `PATH`), so a poisoned `PATH` entry cannot substitute a fake Git
    helper (e.g. `git-remote-https`) or a fake `ssh` (Issue #2196 P1-2:
    `GIT_EXEC_PATH` alone unset is not sufficient, since Git also searches
    `PATH` for `git-<command>` helpers). Interactive prompting, an ambient
    credential helper, and interactive SSH are all disabled (terminal
    prompt disabled, the two askpass-style hooks cleared, SSH forced to
    batch mode against a trusted absolute `ssh` path) so the subprocess can
    never block waiting on stdin or silently invoke an ambient credential
    helper or an untrusted `ssh`.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in GIT_SUBPROCESS_UNSET_ENV_KEYS
    }
    env["CLAUDE_PROJECT_DIR"] = project_root
    env["PATH"] = os.pathsep.join(_safe_path_entries())
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GIT_SSH_COMMAND"] = _resolve_trusted_ssh_command()
    return env


def git_subprocess_trusted_hooks_dir(scratch_root: str) -> str:
    """Return the absolute realpath of a fresh, verified-empty,
    per-invocation directory to pin `core.hooksPath` at for a dedicated
    Git subprocess invocation (Issue #2196 AC3).

    `scratch_root` (and every existing ancestor of it) must not be a
    symlink -- rejected fail-closed via `_is_symlink_path`'s component-wise,
    no-follow walk -- and must be an absolute path, so a caller cannot
    redirect this trusted directory outside the location it appears to be
    in. A brand-new directory is created under `scratch_root` for every
    call (never a fixed, reused name), so no two invocations -- even
    concurrent ones -- ever share the same trusted-empty-hooks directory,
    and its resolved realpath is checked to equal itself (i.e. it is not
    itself reached through a symlink) before being handed back. Git only
    looks for hooks inside the configured `core.hooksPath`, and this
    directory can never contain any (verified empty immediately after
    creation), so no hook script (attacker- or accident-placed, e.g. a
    repo-local `.git/hooks/post-checkout`) can ever fire for a dedicated
    Git subprocess.
    """
    scratch_path = Path(scratch_root)
    if not scratch_path.is_absolute():
        raise RuntimeError("git_subprocess_scratch_root_not_absolute")
    if _is_symlink_path(scratch_path):
        raise RuntimeError("git_subprocess_scratch_root_symlinked")
    scratch_path.mkdir(parents=True, exist_ok=True)
    hooks_dir = Path(
        tempfile.mkdtemp(prefix=".skill-runtime-git-hooks-", dir=str(scratch_path))
    )
    real_hooks_dir = Path(os.path.realpath(hooks_dir))
    if real_hooks_dir != hooks_dir:
        raise RuntimeError("git_subprocess_trusted_hooks_dir_not_realpath")
    if any(hooks_dir.iterdir()):
        raise RuntimeError("git_subprocess_trusted_hooks_dir_not_empty")
    return str(hooks_dir)


def _probe_insteadof_rewrite_keys(
    git_executable: str,
    *,
    cwd: str,
    env: dict[str, str],
    hooks_dir: str,
    timeout: float,
) -> list[str]:
    """Run the structured, name-only, NUL-delimited, regexp-limited
    `insteadOf`/`pushInsteadOf` config probe (Issue #2196 AC4/AC6/AC9 /
    P1-1/P1-4/P1-5), using the exact same `git_executable`, `env`, and
    `-c core.hooksPath=`/`-c credential.helper=`/`--no-replace-objects`
    global options as the real command that follows it, so the probe and
    the real command are provably bound to identical config authority
    (P1-1: this is what makes a `GIT_CONFIG`-style split-execution attack
    structurally impossible here -- `GIT_CONFIG` itself is already unset
    for both, and both read config through the same argv/env/cwd shape).

    Returns the list of matched `url.<base>.(insteadOf|pushInsteadOf)`
    config key names (empty if none). Raises
    `GitSubprocessConfigProbeFailed` for any outcome other than "the probe
    positively established zero matches" -- a timeout, an OS-level
    transport error, a decode failure, or a nonzero exit accompanied by
    stderr output are all treated as "could not be evaluated", never as
    "no rewrite exists" (P1-5).
    """
    argv = [
        git_executable,
        "--no-replace-objects",
        "-c",
        "core.hooksPath=" + hooks_dir,
        "-c",
        "credential.helper=",
        "-C",
        cwd,
        "config",
        "--get-regexp",
        "--name-only",
        "-z",
        INSTEADOF_CONFIG_NAME_REGEXP,
    ]
    try:
        probe = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitSubprocessConfigProbeFailed("probe_timeout") from exc
    except OSError as exc:
        raise GitSubprocessConfigProbeFailed(f"probe_os_error:{exc}") from exc

    if probe.returncode == 0:
        return parse_config_get_regexp_name_only_nul(probe.stdout)
    if probe.returncode == 1:
        # `git config --get-regexp` exits 1 when no key matches the given
        # regexp -- but only when that is genuinely the reason: a nonzero
        # exit accompanied by stderr output means something else went
        # wrong (a config syntax error, a bad `include`, a permission
        # error, ...), which must not be silently treated as "no match".
        stderr_text = probe.stderr.decode("utf-8", errors="replace") if probe.stderr else ""
        if stderr_text.strip() == "":
            return []
        raise GitSubprocessConfigProbeFailed(
            f"probe_nonzero_exit_with_stderr:{stderr_text.strip()!r}"
        )
    raise GitSubprocessConfigProbeFailed(f"probe_nonzero_exit:{probe.returncode}")


def _run_sanitized_git_subprocess(
    argv: list[str],
    *,
    cwd: str,
    project_root: str,
    scratch_root: str | None = None,
    timeout: float,
) -> subprocess.CompletedProcess:
    """Internal engine for a dedicated, sanitized Git subprocess (Issue
    #2196). Private (leading underscore): the only production callers are
    the closed `run_control_plane_git_*` builders below, each of which
    constructs `argv` itself from validated, structured parameters -- this
    function is never exposed as a raw-argv production API (P1-3).

    `argv` is the Git subcommand and its arguments, WITHOUT the leading
    `git` executable name (e.g. `["status", "--short"]`) and WITHOUT any
    Git global option (rejected via `reject_git_global_options`, P1-3).
    This function:

    1. Resolves (once, cached -- AC2) the absolute trusted `git`
       executable.
    2. Builds the sanitized environment (AC1/AC5/AC7): GIT_* variables in
       `GIT_SUBPROCESS_UNSET_ENV_KEYS` are unset, `PATH` is a trusted
       allowlist, interactive prompting/credential helper/SSH prompting
       are disabled.
    3. Fixes `core.hooksPath` at a fresh, verified-empty, per-invocation
       trusted directory (AC3) via a config override applied identically
       to both the probe and the real command invocation, and
       additionally disables any ambient credential helper the same way.
    4. Fail-closed rejects the invocation before the real command runs if
       `cwd`'s effective Git configuration declares any
       `url.<base>.insteadOf`/`pushInsteadOf` rewrite (AC4/AC6), using a
       structured, name-only, NUL-delimited probe bound to identical
       config authority as the real command (P1-1/P1-4), and fails closed
       (rather than proceeding) if that probe itself could not be
       positively evaluated (AC9/P1-5).
    5. Applies `--no-replace-objects` as a fixed global option on every
       invocation (AC11), defaults `stdin=subprocess.DEVNULL` and requires
       a bounded `timeout` (AC10/P2-2).

    Raises `GitSubprocessRewriteRejected` for step 4's rewrite rejection,
    `GitSubprocessConfigProbeFailed` for step 4's probe-failure rejection.
    Any other precondition failure (e.g. an unexpected leading `git` token
    or embedded global option in `argv`, a non-bounded `timeout`, or a
    non-empty trusted hooks directory) raises `ValueError` / `RuntimeError`
    before any subprocess is spawned.
    """
    if not argv or argv[0] == "git":
        raise ValueError("argv_must_not_include_executable_name")
    if timeout is None or timeout <= 0:
        raise ValueError("timeout_required_bounded")
    reject_git_global_options(argv)

    git_executable = resolve_git_subprocess_executable(project_root)
    env = sanitized_git_subprocess_env(project_root)
    hooks_dir = git_subprocess_trusted_hooks_dir(scratch_root or project_root)

    matched_keys = _probe_insteadof_rewrite_keys(
        git_executable,
        cwd=cwd,
        env=env,
        hooks_dir=hooks_dir,
        timeout=timeout,
    )
    reject_insteadof_rewrite(matched_keys)

    full_argv = [
        git_executable,
        "--no-replace-objects",
        "-c",
        "core.hooksPath=" + hooks_dir,
        "-c",
        "credential.helper=",
        *argv,
    ]
    return subprocess.run(
        full_argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Closed command builder API (Issue #2196 AC8 / P1-3).
#
# Each builder below accepts only a fixed, validated subcommand shape --
# structured keyword/positional parameters describing *data* (a repo URL, a
# ref name, an object id, ...), never a caller-supplied argv list. None of
# them accept a Git global option in any form: `reject_git_global_options`
# defends the constructed argv itself (defense in depth), and every
# caller-supplied positional value is separately checked by
# `reject_option_like_positional` so it cannot be interpreted by Git as a
# flag. This is the only sanctioned way for production code (later
# children #2197-#2200) to run a dedicated sanitized Git subprocess; no
# raw-argv equivalent is exposed at production callsites.
# ---------------------------------------------------------------------------


def run_control_plane_git_ls_remote(
    repo: str,
    *,
    cwd: str,
    project_root: str,
    scratch_root: str | None = None,
    timeout: float,
) -> subprocess.CompletedProcess:
    """Closed builder for `git ls-remote -- <repo>`."""
    reject_option_like_positional(repo)
    return _run_sanitized_git_subprocess(
        ["ls-remote", "--", repo],
        cwd=cwd,
        project_root=project_root,
        scratch_root=scratch_root,
        timeout=timeout,
    )


def run_control_plane_git_fetch(
    repo: str,
    refspec: str,
    *,
    cwd: str,
    project_root: str,
    scratch_root: str | None = None,
    timeout: float,
) -> subprocess.CompletedProcess:
    """Closed builder for `git fetch -- <repo> <refspec>`."""
    reject_option_like_positional(repo)
    reject_option_like_positional(refspec)
    return _run_sanitized_git_subprocess(
        ["fetch", "--", repo, refspec],
        cwd=cwd,
        project_root=project_root,
        scratch_root=scratch_root,
        timeout=timeout,
    )


_CAT_FILE_ALLOWED_MODES = frozenset({"-p", "-t", "-s"})


def run_control_plane_git_cat_file(
    object_id: str,
    *,
    mode: str = "-p",
    cwd: str,
    project_root: str,
    scratch_root: str | None = None,
    timeout: float,
) -> subprocess.CompletedProcess:
    """Closed builder for `git cat-file <mode> <object_id>`. `mode` must be
    one of `-p`/`-t`/`-s` (the fixed, validated subcommand shape for this
    builder); any other value is rejected before a subprocess is spawned.
    """
    if mode not in _CAT_FILE_ALLOWED_MODES:
        raise ValueError(f"cat_file_mode_not_allowed:{mode}")
    reject_option_like_positional(object_id)
    return _run_sanitized_git_subprocess(
        ["cat-file", mode, object_id],
        cwd=cwd,
        project_root=project_root,
        scratch_root=scratch_root,
        timeout=timeout,
    )


def run_control_plane_git_update_ref(
    ref: str,
    new_oid: str,
    *,
    cwd: str,
    project_root: str,
    scratch_root: str | None = None,
    timeout: float,
) -> subprocess.CompletedProcess:
    """Closed builder for `git update-ref -- <ref> <new_oid>`."""
    reject_option_like_positional(ref)
    reject_option_like_positional(new_oid)
    return _run_sanitized_git_subprocess(
        ["update-ref", "--", ref, new_oid],
        cwd=cwd,
        project_root=project_root,
        scratch_root=scratch_root,
        timeout=timeout,
    )


_WORKTREE_ALLOWED_ACTIONS = frozenset({"list", "add", "remove", "prune"})


def run_control_plane_git_worktree(
    action: str,
    *args: str,
    cwd: str,
    project_root: str,
    scratch_root: str | None = None,
    timeout: float,
) -> subprocess.CompletedProcess:
    """Closed builder for `git worktree <action> [args...]`. `action` must
    be one of `list`/`add`/`remove`/`prune` (the fixed, validated
    subcommand shape for this builder); any other value is rejected before
    a subprocess is spawned. Every element of `args` is separately checked
    by `reject_option_like_positional`.
    """
    if action not in _WORKTREE_ALLOWED_ACTIONS:
        raise ValueError(f"worktree_action_not_allowed:{action}")
    for value in args:
        reject_option_like_positional(value)
    return _run_sanitized_git_subprocess(
        ["worktree", action, *args],
        cwd=cwd,
        project_root=project_root,
        scratch_root=scratch_root,
        timeout=timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
