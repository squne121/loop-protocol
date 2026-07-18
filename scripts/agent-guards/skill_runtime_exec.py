#!/usr/bin/env python3
"""Exact privileged executor for allowed skill runtime commands (Issue #1154)."""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

sys.dont_write_bytecode = True

from skill_runtime_command_policy import (
    REGISTRY_REL,
    SKILL_RUNTIME_EXEC_REL,
    TRUSTED_REPO_SLUG,
    ExactSkillRuntimeCommand,
    command_allows_root_no_worktree,
    current_branch,
    is_exact_skill_runtime_anchor_executor_command,
    is_exact_skill_runtime_executor_command,
    is_exact_skill_runtime_fixture_executor_command,
    load_registry_entry,
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


def _allowed_artifact_root(project_root: str, issue_number: str) -> Path:
    return Path(project_root) / ".claude" / "artifacts" / "issue-refinement-loop" / issue_number


def _is_under_allowed_artifact_root(project_root: str, issue_number: str, rel_path: str) -> bool:
    root = Path(project_root)
    target = (root / rel_path).resolve()
    allowed_root = _allowed_artifact_root(project_root, issue_number).resolve()
    return target == allowed_root or target.is_relative_to(allowed_root)


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


def _expand_new_status_paths(project_root: str, new_raw_paths: set[str]) -> set[str]:
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
        if path.endswith("/") and _strict_ancestor_of_race_tolerant_root(path):
            if _is_real_nonsymlink_dir(project_root, path):
                expanded.update(_expand_folded_ignored_status_dir(project_root, path))
                continue
        expanded.add(path)
    return expanded


def _snapshot_repo_paths(project_root: str, issue_number: str) -> dict[str, tuple[str, int, int]]:
    root = Path(project_root)
    allowed_root = _allowed_artifact_root(project_root, issue_number)
    peer_roots = _race_tolerant_unattributable_roots(project_root)
    allowed_parent_dirs: set[Path] = set()
    for parent in allowed_root.parents:
        allowed_parent_dirs.add(parent)
        if parent == root:
            break
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
            if path == allowed_root or path.is_relative_to(allowed_root):
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


def _ensure_artifact_path_safe(project_root: str, issue_number: str) -> Path:
    artifact_root = _allowed_artifact_root(project_root, issue_number)
    parent = artifact_root.parent
    for candidate in (Path(project_root) / ".claude", Path(project_root) / ".claude" / "artifacts", parent):
        if candidate.exists() and _is_symlink_path(candidate):
            raise RuntimeError("artifact_parent_symlink_not_allowed")
    if artifact_root.exists() and (_is_symlink_path(artifact_root) or artifact_root.is_symlink()):
        raise RuntimeError("artifact_root_symlink_not_allowed")
    return artifact_root


def _safe_path_entries() -> list[str]:
    entries = [
        str(Path.home() / ".local" / "bin"),
        *_trusted_uv_toolcache_dirs(),
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in entries:
        if entry and entry not in seen:
            seen.add(entry)
            ordered.append(entry)
    return ordered


def _trusted_uv_toolcache_dirs() -> list[str]:
    root = Path("/opt/hostedtoolcache/uv")
    if not root.is_dir():
        return []

    trusted_dirs: list[str] = []
    root_real = os.path.realpath(root)
    for candidate in sorted(root.glob("*/x86_64")):
        uv_path = candidate / "uv"
        if not uv_path.is_file() or not os.access(uv_path, os.X_OK):
            continue
        real = os.path.realpath(uv_path)
        if os.path.commonpath([root_real, real]) != root_real:
            continue
        trusted_dirs.append(str(candidate))
    return trusted_dirs


def _resolve_trusted_executable(name: str, project_root: str) -> str:
    safe_entries = _safe_path_entries()
    safe_path = os.pathsep.join(safe_entries)
    if name == "python3":
        resolved = os.path.realpath(sys.executable)
    else:
        resolved = shutil.which(name, path=safe_path)
    if not resolved:
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
    return real


def _sanitize_env(project_root: str) -> dict[str, str]:
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
    return _ensure_artifact_path_safe(project_root, str(args.issue_number))


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
_SHADOW_LOG_STABLE_OBSERVATION_ATTEMPTS = 25
_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS = 0.01

# Sentinel kind returned by `_shadow_log_stable_observation` when a
# self-consistent observation could not be made within the retry budget.
# This value is never a member of any authorized transition tuple in
# `_is_allowed_shadow_log_kind_transition`, so it always fails closed.
_SHADOW_LOG_KIND_UNSTABLE = "unstable"

# PR #1572 REQUEST_CHANGES (Medium): explicit size contract for the shadow
# log. `_shadow_log_stable_observation` never buffers content past this
# bound into memory -- a file at or growing past this size fails closed
# (sentinel `_SHADOW_LOG_KIND_UNSTABLE`) instead of being read in full.
_SHADOW_LOG_MAX_BYTES = 8 * 1024 * 1024


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
) -> tuple[str, tuple[int, int, int, int] | None, bytes | None]:
    """Return `(kind, identity, content)` for the shadow-log path as a
    single, self-consistent filesystem-generation observation.

    - `kind`: `absent` / `symlink` / `dir` / `regular` / `fifo` / `socket` /
      `device` / `other` / `unstable` (see `_SHADOW_LOG_KIND_UNSTABLE`).
    - `identity`: `(st_dev, st_ino, st_size, st_mtime_ns)` for a non-absent
      path, else `None`.
    - `content`: the file's full bytes when `kind == "regular"`, else `None`.

    PR #1572 REQUEST_CHANGES Blocker 1: a plain `lstat()`-then-`open()`
    -then-`read()` sequence has a TOCTOU gap between classifying the kind
    and reading the content -- the path could be replaced (symlink, delete,
    truncate, overwrite) in that gap, and the caller would then authorize a
    transition based on content that no longer corresponds to the kind it
    classified (or vice versa). This helper closes that gap by opening with
    `O_NOFOLLOW | O_NONBLOCK` (never silently follows a symlink final
    component; never blocks indefinitely opening a FIFO with no writer),
    `fstat()`-ing the open descriptor to capture the identity of exactly the
    generation being read, reading the full content from that same
    descriptor, and then re-`lstat()`-ing the path afterward to confirm its
    identity still matches what was `fstat()`'d. A bounded number of retries
    absorbs a legitimate in-flight write landing exactly inside this narrow
    window; if the observation still cannot be made self-consistent after
    the retry budget, `_SHADOW_LOG_KIND_UNSTABLE` is returned, which always
    fails closed."""
    for _ in range(_SHADOW_LOG_STABLE_OBSERVATION_ATTEMPTS):
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
            if fstat_result.st_size > _SHADOW_LOG_MAX_BYTES:
                # PR #1572 REQUEST_CHANGES (Medium): never buffer an
                # unbounded amount of shadow-log content into memory. A file
                # over the documented size contract is rejected outright
                # (fail closed, sentinel kind) instead of being read in full.
                return _SHADOW_LOG_KIND_UNSTABLE, None, None
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > _SHADOW_LOG_MAX_BYTES:
                    # Grew past the cap during this very read (concurrent
                    # append mid-read) -- also fail closed rather than
                    # continuing to buffer unbounded content.
                    return _SHADOW_LOG_KIND_UNSTABLE, None, None
                chunks.append(chunk)
            content = b"".join(chunks)
        finally:
            os.close(fd)

        try:
            final_lst = path.lstat()
        except FileNotFoundError:
            time.sleep(_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS)
            continue

        identity_matches = (
            stat.S_ISREG(final_lst.st_mode)
            and (final_lst.st_dev, final_lst.st_ino) == (fstat_result.st_dev, fstat_result.st_ino)
            and final_lst.st_size == len(content)
            and final_lst.st_mtime_ns == fstat_result.st_mtime_ns
        )
        if identity_matches:
            identity = (fstat_result.st_dev, fstat_result.st_ino, len(content), fstat_result.st_mtime_ns)
            return "regular", identity, content
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
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        records.append(obj)
    return records


def _is_authorized_shadow_log_content_transition(before: bytes, after: bytes) -> bool:
    """A `regular -> regular` shadow-log content change is authorized only
    when it is a strict append: `after` starts with the exact `before`
    bytes, and every JSONL record present in `before` is still present,
    unchanged, and in the same order in `after` (AC3). Truncation,
    overwrite, malformed-JSONL replacement, and existing-record
    deletion/reordering are all rejected, whether or not the byte length
    changed. Inode identity (same-path-different-inode replacement,
    PR #1572 REQUEST_CHANGES Blocker 3) is validated separately by the
    caller before this function is reached."""
    if before == after:
        return True
    if not after.startswith(before):
        return False
    before_records = _parse_shadow_log_jsonl(before)
    after_records = _parse_shadow_log_jsonl(after)
    if before_records is None or after_records is None:
        return False
    if len(after_records) < len(before_records):
        return False
    return after_records[: len(before_records)] == before_records


def _is_authorized_shadow_log_cold_start_content(after: bytes) -> bool:
    """PR #1572 REQUEST_CHANGES Blocker 2: an `absent -> regular` shadow-log
    creation must still be validated as well-formed JSONL content, not
    merely accepted because *some* regular file appeared. Returns True only
    when the full `after` content parses as well-formed JSONL (each
    complete line is a JSON object; see `_parse_shadow_log_jsonl` for the
    exact well-formedness contract, including the Blocker 4 blank-line /
    non-standard-constant rejections)."""
    return _parse_shadow_log_jsonl(after) is not None


def _find_unauthorized_repo_changes(
    project_root: str,
    issue_number: str,
    before_snapshot: dict[str, tuple[str, int, int]],
    before_status: set[str],
    ledger_before_kinds: dict[str, str] | None = None,
    ledger_before_bytes: bytes | None = None,
    ledger_ancestor_before_kinds: dict[str, str] | None = None,
    shadow_log_before_kind: str | None = None,
    shadow_log_before_bytes: bytes | None = None,
    shadow_log_before_identity: tuple[int, int, int, int] | None = None,
) -> str | None:
    # Issue #1502: the stable-exact ledger transition is checked first and
    # independently of the generic snapshot/status diff below. If the
    # transition is not one of the two authorized kinds, fail closed on that
    # exact path immediately (AC2) rather than folding it into the generic
    # diff (which would only report the deepest-path heuristic winner).
    ledger_before_kinds = ledger_before_kinds or {}
    stable_before_kind = ledger_before_kinds.get(_LEDGER_STABLE_EXACT_REL, "absent")
    stable_ledger_path = Path(project_root) / _LEDGER_STABLE_EXACT_REL
    stable_after_kind = _path_kind_or_ancestor_absent(stable_ledger_path)
    if not _is_allowed_stable_ledger_transition(stable_before_kind, stable_after_kind):
        return _LEDGER_STABLE_EXACT_REL

    # Issue #1502 REQUEST_CHANGES (Blocker 3): `regular -> regular` is a
    # *type*-authorized transition, but the type check alone says nothing
    # about content -- a peer could replace a valid ledger with malformed
    # content (e.g. `"not-json-at-all"`), or with a replacement that silently
    # drops/mutates existing `launches` / `root_thread_actions` entries.
    # Validate the content transition here, independently of the generic
    # snapshot/status diff below (which only compares mtime/size, not
    # content). Byte-identical before/after content (nothing actually
    # changed) is always safe regardless of schema validity, so a
    # pre-existing malformed-but-untouched ledger never blocks detection of
    # an unrelated sibling change.
    if stable_before_kind == "regular" and stable_after_kind == "regular":
        after_bytes = _read_bytes_or_none(stable_ledger_path)
        if after_bytes is None or ledger_before_bytes is None:
            return _LEDGER_STABLE_EXACT_REL
        if after_bytes != ledger_before_bytes:
            before_content = _parse_ledger_bytes(ledger_before_bytes)
            after_content = _parse_ledger_bytes(after_bytes)
            if (
                before_content is None
                or after_content is None
                or not _is_authorized_ledger_content_transition(before_content, after_content)
            ):
                return _LEDGER_STABLE_EXACT_REL

    after_snapshot = _snapshot_repo_paths(project_root, issue_number)
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
    shadow_log_after_kind, shadow_log_after_identity, shadow_log_after_bytes = _shadow_log_stable_observation(
        shadow_log_path
    )
    if not _is_allowed_shadow_log_kind_transition(resolved_shadow_log_before_kind, shadow_log_after_kind):
        return _SHADOW_LOG_EXACT_REL
    if shadow_log_after_kind == "regular":
        if resolved_shadow_log_before_kind == "absent":
            # PR #1572 REQUEST_CHANGES Blocker 2: cold-start creation must
            # still be validated as well-formed JSONL content.
            if shadow_log_after_bytes is None or not _is_authorized_shadow_log_cold_start_content(
                shadow_log_after_bytes
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
            if shadow_log_after_bytes is None or shadow_log_before_bytes is None:
                return _SHADOW_LOG_EXACT_REL
            if shadow_log_after_bytes != shadow_log_before_bytes and not _is_authorized_shadow_log_content_transition(
                shadow_log_before_bytes, shadow_log_after_bytes
            ):
                return _SHADOW_LOG_EXACT_REL

    new_raw_status_paths = after_status - before_status
    # Issue #1409 REQUEST_CHANGES (P1): expand any collapsed ignored-ancestor
    # directory entries (e.g. `!! artifacts/`) into their real leaf paths
    # before applying race-tolerant-root exclusion, so cold-start creation of
    # a race-tolerant subtree under an ignored parent is not misreported as
    # an unauthorized write to the collapsed parent itself.
    expanded_new_status_paths = _expand_new_status_paths(project_root, new_raw_status_paths)
    safe_ledger_ancestor_dir_rels = _safe_ledger_ancestor_dir_rels(project_root, ledger_ancestor_before_kinds)
    new_status_paths = {
        path
        for path in expanded_new_status_paths
        if not _is_under_allowed_artifact_root(project_root, issue_number, path)
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


def _validate_stdout_artifact_projection(project_root: str, issue_number: str, stdout: str) -> list[str]:
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
        if not _is_under_allowed_artifact_root(project_root, issue_number, rel_path):
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


def _emit_timeout_failure(issue_number: int, timeout_seconds: object) -> int:
    print(
        "SKILL_RUNTIME_FAIL: "
        f"reason_code=child_process_timeout target_issue={issue_number} "
        f"timeout_seconds={timeout_seconds} "
        "recovery=investigate_child_process_hang_or_increase_registry_timeout",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Privileged exact skill runtime executor", allow_abbrev=False
    )
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--fixture", required=False, default=None)
    parser.add_argument("--anchor-comment-url", required=False, default=None)
    args = parser.parse_args(argv)

    project_root = resolve_project_root()
    stale_entries = _normalize_and_validate_runtime_env(project_root)
    if stale_entries:
        return _emit_stale_runtime_failure(args.issue_number, stale_entries)

    is_fixture_command = args.command_id == "preflight.run.fixture"
    is_anchor_command = args.command_id == "preflight.run.with_anchor"
    if is_fixture_command:
        if not args.fixture:
            print("skill_runtime_exec: --fixture required for preflight.run.fixture", file=sys.stderr)
            return 2
        if args.anchor_comment_url:
            print(
                "skill_runtime_exec: --anchor-comment-url is not allowed for preflight.run.fixture",
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
    elif is_anchor_command:
        if args.fixture:
            print(
                "skill_runtime_exec: --fixture is not allowed for preflight.run.with_anchor",
                file=sys.stderr,
            )
            return 2
        if not args.anchor_comment_url:
            print(
                "skill_runtime_exec: --anchor-comment-url required for preflight.run.with_anchor",
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
            ]
        )
        if not is_exact_skill_runtime_anchor_executor_command(command_text, project_root, project_root):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    else:
        if args.fixture:
            print("skill_runtime_exec: --fixture is only allowed for preflight.run.fixture", file=sys.stderr)
            return 2
        if args.anchor_comment_url:
            print(
                "skill_runtime_exec: --anchor-comment-url is only allowed for "
                "preflight.run.with_anchor",
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
    before_snapshot = _snapshot_repo_paths(project_root, str(args.issue_number))
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
        shadow_log_before_bytes,
    ) = _shadow_log_stable_observation(Path(project_root) / _SHADOW_LOG_EXACT_REL)

    entry = load_registry_entry(args.command_id, project_root)
    validate_registry_entry(args.command_id, entry, str(args.issue_number))

    registry_path = Path(project_root) / REGISTRY_REL
    if registry_path.is_symlink():
        raise RuntimeError("registry_symlink_not_allowed")
    if not registry_path.is_file():
        raise RuntimeError("registry_missing")

    script_path = (
        Path(project_root) / ".claude" / "skills" / "issue-refinement-loop"
        / "scripts" / "run_refinement_preflight.py"
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
    render_params: dict[str, object] = {"issue_number": args.issue_number, "repo": args.repo}
    if is_fixture_command:
        render_params["fixture"] = args.fixture
    if is_anchor_command:
        render_params["anchor_comment_url"] = args.anchor_comment_url
    child_argv = render_command(args.command_id, render_params)
    child_argv = _resolve_child_argv(child_argv)

    timeout_seconds = entry.get("timeout_seconds")
    try:
        result = subprocess.run(
            child_argv,
            cwd=project_root,
            env=_sanitize_env(project_root),
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return _emit_timeout_failure(args.issue_number, timeout_seconds)

    # Issue #1502 AC3: wait a bounded window for the writer's own `.lock` /
    # `.tmp` transient protocol entries to vanish before evaluating the
    # generic diff. This must run before `_find_unauthorized_repo_changes`
    # takes its "after" snapshot, so quiescent peer writes never appear as
    # residue in that snapshot.
    stale_transient = _wait_for_ledger_transient_quiescence(project_root)
    if stale_transient:
        return _emit_ledger_transient_residue_failure(args.issue_number, stale_transient)

    unauthorized_path = _find_unauthorized_repo_changes(
        project_root,
        str(args.issue_number),
        before_snapshot,
        before_status,
        ledger_before_kinds,
        ledger_before_bytes,
        ledger_ancestor_before_kinds,
        shadow_log_before_kind,
        shadow_log_before_bytes,
        shadow_log_before_identity,
    )
    if unauthorized_path is not None:
        return _emit_unauthorized_write_failure(args.issue_number, unauthorized_path)

    artifact_projection_failures = _validate_stdout_artifact_projection(
        project_root,
        str(args.issue_number),
        result.stdout,
    )
    if artifact_projection_failures:
        return _emit_artifact_projection_failure(args.issue_number, artifact_projection_failures)

    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
