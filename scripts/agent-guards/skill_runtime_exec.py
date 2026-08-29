#!/usr/bin/env python3
"""Exact privileged executor for allowed skill runtime commands (Issue #1154)."""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import math
import os
import pwd
import re
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from dataclasses import dataclass
from typing import Iterable

sys.dont_write_bytecode = True

from skill_runtime_command_policy import (
    GIT_SUBPROCESS_UNSET_ENV_KEYS,
    INSTEADOF_CONFIG_NAME_REGEXP,
    REGISTRY_REL,
    SKILL_RUNTIME_EXEC_REL,
    TRUSTED_REPO_SLUG,
    ExactSkillRuntimeCommand,
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
    is_exact_skill_runtime_structural_repair_action_apply_executor_command,
    load_registry_entry,
    resolve_active_issue,
    resolve_default_branch,
    resolve_project_root,
    resolve_repo_slug,
    validate_registry_entry,
    AllowedRemoteRef,
    ControlPlanePrivateRef,
    DetachedWorktreePath,
    LiteralRemoteUrl,
    RepositoryObjectFormat,
    RepositoryObjectId,
    make_control_plane_private_ref,
    validate_allowed_remote_ref,
    validate_control_plane_private_ref,
    validate_detached_worktree_path,
    validate_existing_detached_worktree_path,
    validate_literal_remote_url,
    validate_repository_object_format,
    validate_repository_object_id,
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
    # Issue #1526: `.pytest_cache` (repo-root pytest cacheprovider directory,
    # ignored via `.gitignore:69`). pytest's own `NFPlugin`/`LFPlugin`
    # (registered together in `_pytest/cacheprovider.py`'s `pytest_configure`
    # hookimpl) both write into it from `pytest_sessionfinish`: `NFPlugin`
    # saves `cache/nodeids` on every ordinary session finish, and `LFPlugin`
    # saves `cache/lastfailed` whenever the failed-node-id set changed since
    # the last run (PR #2364 review P2: not unconditionally -- pytest 9.0.3's
    # `LFPlugin.pytest_sessionfinish` compares against the previously saved
    # value first). Third-party plugins can additionally write arbitrary keys
    # under `.pytest_cache/v/cache/` via the public `config.cache` API. None
    # of this is canonical evidence, it is concurrent peer pytest's ordinary
    # generated/disposable cache state that this stdlib-only snapshot diff
    # cannot attribute to the executor child vs. a peer process.
    # `.pytest_cache/CACHEDIR.TAG` and its own auto-generated `.gitignore: *`
    # already self-declare the directory as regenerable. Unlike
    # `_LEDGER_*` below, there is no single
    # stable-identity file here whose symlink/directory/FIFO/socket/device
    # substitution would need typed exact-file protection -- the whole tree
    # is disposable, so the directory-root exclusion class used for the peer
    # roots above is the right shape, not the typed exact-file policy.
    ".pytest_cache",
)

# Issue #1526 PR #2364 review P1-1: pytest's cacheprovider does not create
# `.pytest_cache` directly on a cold start. `Cache._ensure_cache_dir_and_
# supporting_files()` first materializes a `tempfile.TemporaryDirectory(
# prefix="pytest-cache-files-", dir=self._cachedir.parent)` -- i.e. a
# repo-root sibling of `.pytest_cache`, not a path under it -- populates it
# with README.md/.gitignore/CACHEDIR.TAG, and only then renames it onto
# `.pytest_cache`. A genuinely concurrent peer pytest process racing this
# executor's before/after snapshot can therefore leave a real, transient
# `pytest-cache-files-<random>/` directory sitting directly at repo root,
# which the `.pytest_cache` root-exclusion above does not cover. This is the
# same disposable/generated-state class as `.pytest_cache` itself (pytest's
# own atomic-rename implementation detail, never canonical evidence), so it
# gets a narrow, repo-root-only, prefix-matched exemption alongside it rather
# than widening `_RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS` (which only does
# exact-string/exact-subtree matching, not prefix globs).
_PYTEST_CACHE_COLD_START_TEMP_DIR_PREFIX = "pytest-cache-files-"


def _is_pytest_cache_cold_start_temp_path(rel_path: str) -> bool:
    """True for a repo-root-only `pytest-cache-files-<random>` transient
    directory (or any path inside it) that pytest's cacheprovider creates
    while atomically materializing `.pytest_cache` on a cold start (PR #2364
    review P1-1). Deliberately top-level-only: a nested
    `sub/pytest-cache-files-x` is never pytest's own cache_dir sibling (its
    `dir=self._cachedir.parent` is always the pytest rootdir, which this
    executor treats as repo root) and must remain fully audited.
    """
    normalized = rel_path.replace(os.sep, "/")
    first_segment = normalized.split("/", 1)[0]
    return first_segment.startswith(_PYTEST_CACHE_COLD_START_TEMP_DIR_PREFIX)


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


def _safe_ledger_ancestor_dir_rels(project_root: str, ancestor_before_kinds: dict[str, str] | None = None) -> set[str]:
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
        return [rel for rel in _LEDGER_TRANSIENT_EXACT_RELS if _path_kind_or_ancestor_absent(root / rel) != "absent"]

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


def _is_under_allowed_artifact_root(project_root: str, issue_number: str, rel_path: str, command_id: str = "") -> bool:
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


def _snapshot_repo_paths(project_root: str, issue_number: str, command_id: str = "") -> dict[str, tuple[str, int, int]]:
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
        dirnames[:] = [name for name in dirnames if (current_path / name) != root / ".git"]
        # Prune volatile peer-session roots entirely so that concurrent
        # local sessions/agents writing under them are never walked into
        # (and therefore never contribute snapshot drift for this command).
        dirnames[:] = [name for name in dirnames if (current_path / name) not in peer_roots]
        # PR #2364 review P1-1: also prune a genuine repo-root-only pytest
        # cold-start temp dir (`pytest-cache-files-<random>`, see
        # `_is_pytest_cache_cold_start_temp_path` above) from this full
        # filesystem walk. `peer_roots` above only prunes exact, statically
        # known paths (like `.pytest_cache` itself); this dynamic-suffix
        # sibling needs its own prefix check, scoped to `current_path ==
        # root` so a nested lookalike is never pruned.
        if current_path == root:
            dirnames[:] = [name for name in dirnames if not _is_pytest_cache_cold_start_temp_path(name)]
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
        safe_entries = _dedupe_path_entries(
            [
                *_trusted_toolchain_dirs("uv"),
                *_trusted_account_home_uv_dir(),
                *_SYSTEM_STANDARD_PATH_DIRS,
            ]
        )
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
            recommended_install_dir = os.path.join(account_home, ".local", "bin") if account_home else None
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
    # Issue #2311 fix_delta (PR #2320 review P0-1): only the bare
    # `preflight.run` command's first hop (`workflow_start_entry.py`) reads
    # an invocation-scoped capability request off these three env vars as
    # its CLI-flag fallback (the canonical bare `preflight.run` registry
    # argv itself only ever carries `--issue-number`/`--repo` -- there is no
    # `--spark-mode`/`--spark-fallback`/`--planned-operations-json` flag on
    # that argv for a caller to use instead). Without this allowlist
    # addition, a caller-declared capability request set via these env vars
    # before invoking the canonical executor was silently dropped by this
    # function, so `workflow_start_entry.py` always observed `None`/`None`/
    # `[]` regardless of what the caller actually intended -- masking
    # `unsupported_operation`/`required+forbidden` Spark blocks that should
    # have stopped the inner preflight from ever starting. No other command
    # id receives these three keys: sibling profiles
    # (`preflight.run.with_anchor` / `.with_human_context` /
    # `.with_agent_report` / `.fixture` / `.fixture.with_human_context`)
    # first-hop into `run_refinement_preflight.py` directly and do not
    # consume this env-based capability request at all.
    if command_id == "preflight.run":
        allowed_keys |= {
            "LOOP_SPARK_MODE",
            "LOOP_SPARK_FALLBACK",
            "LOOP_PLANNED_OPERATIONS_JSON",
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


def _find_unauthorized_repo_changes(
    project_root: str,
    issue_number: str,
    before_snapshot: dict[str, tuple[str, int, int]],
    before_status: set[str],
    ledger_before_kinds: dict[str, str] | None = None,
    ledger_before_bytes: bytes | None = None,
    ledger_ancestor_before_kinds: dict[str, str] | None = None,
    command_id: str = "",
) -> str | None:
    # Issue #1830: launch-ledger state is advisory telemetry. Keep its exact
    # paths out of the child-attribution diff, but never turn missing,
    # malformed, mixed, or concurrent ledger state into a routing failure.
    ledger_before_kinds = ledger_before_kinds or {}

    after_snapshot = _snapshot_repo_paths(project_root, issue_number, command_id)
    after_status = _git_status_paths(project_root)

    new_raw_status_paths = after_status - before_status
    # Issue #1409 REQUEST_CHANGES (P1): expand any collapsed ignored-ancestor
    # directory entries (e.g. `!! artifacts/`) into their real leaf paths
    # before applying race-tolerant-root exclusion, so cold-start creation of
    # a race-tolerant subtree under an ignored parent is not misreported as
    # an unauthorized write to the collapsed parent itself.
    expanded_new_status_paths = _expand_new_status_paths(project_root, new_raw_status_paths, issue_number, command_id)
    safe_ledger_ancestor_dir_rels = _safe_ledger_ancestor_dir_rels(project_root, ledger_ancestor_before_kinds)
    new_status_paths = {
        path
        for path in expanded_new_status_paths
        if not _is_under_allowed_artifact_root(project_root, issue_number, path, command_id)
        and not _is_race_tolerant_unattributable_path(path)
        and not _is_pytest_cache_cold_start_temp_path(path)
        and path not in _LEDGER_TYPED_EXACT_RELS
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
            | {path for path in before_paths & after_paths if before_snapshot[path] != after_snapshot[path]}
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
            os.path.realpath(raw_path) if os.path.isabs(raw_path) else os.path.realpath(Path(project_root) / raw_path)
        )
        rel_path = (
            os.path.relpath(resolved, root_real) if os.path.commonpath([root_real, resolved]) == root_real else resolved
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
    os.name == "posix" and hasattr(os, "killpg") and hasattr(os, "setsid") and hasattr(os, "getpgid")
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
            cleanup_status = CLEANUP_STATUS_CONFIRMED_ABSENT if group_absent else CLEANUP_STATUS_UNCONFIRMED
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
    parser = argparse.ArgumentParser(description="Privileged exact skill runtime executor", allow_abbrev=False)
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
    # Generic and structural mutation consumers have distinct command IDs and
    # exact outer flags. Argparse rejects a mixed invocation before dispatch;
    # each command branch below also enforces its exact pairing.
    apply_action_flags = parser.add_mutually_exclusive_group()
    apply_action_flags.add_argument("--apply-repair-action", required=False, default=None)
    apply_action_flags.add_argument("--apply-structural-repair-action", required=False, default=None)
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
        raw_command = " ".join(["uv", "run", "python3", SKILL_RUNTIME_EXEC_REL, *raw_argv])
        if not is_exact_skill_runtime_anchor_fixture_executor_command(
            raw_command, resolve_project_root(), resolve_project_root()
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    structural_id = "structural_repair_action.apply"
    if structural_id in raw_argv or f"--command-id={structural_id}" in raw_argv:
        # Validate raw argv before argparse can normalize a duplicate option.
        # This is an outer transport check only; the child owns payload parsing.
        raw_command = " ".join(["uv", "run", "python3", SKILL_RUNTIME_EXEC_REL, *raw_argv])
        if not is_exact_skill_runtime_structural_repair_action_apply_executor_command(
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
    is_fixture_human_context_command = args.command_id == "preflight.run.fixture.with_human_context"
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
    is_structural_repair_action_apply_command = args.command_id == "structural_repair_action.apply"
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
    if not is_structural_repair_action_apply_command and args.apply_structural_repair_action:
        print(
            "skill_runtime_exec: --apply-structural-repair-action is only allowed for structural_repair_action.apply",
            file=sys.stderr,
        )
        return 2
    if is_structural_repair_action_apply_command and not args.apply_structural_repair_action:
        print(
            "skill_runtime_exec: --apply-structural-repair-action is required for structural_repair_action.apply",
            file=sys.stderr,
        )
        return 2
    if not is_decide_command and (args.authority_transport_path or args.authority_expected):
        print(
            "skill_runtime_exec: --authority-transport-path/--authority-expected are only allowed for decide.run",
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
            "--anchor-comment-url",
            args.anchor_comment_url,
        ]
        if args.investigation_evidence_transport_path:
            command_tokens.extend(
                [
                    "--investigation-evidence-transport-path",
                    args.investigation_evidence_transport_path,
                ]
            )
        command_text = " ".join(command_tokens)
        if not is_exact_skill_runtime_anchor_fixture_executor_command(command_text, project_root, project_root):
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
                "skill_runtime_exec: --loop-state-file and --review-result-verdict are required for decide.run",
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
                "skill_runtime_exec: only --apply-repair-action is allowed for repair_action.apply",
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
        if not is_exact_skill_runtime_repair_action_apply_executor_command(command_text, project_root, project_root):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
            return 2
    elif is_structural_repair_action_apply_command:
        # Preserve strict outer-transport isolation. The safe path predicate is
        # applied to the original argparse value, then the exact parser checks
        # the fixed command shape. No structural payload is opened at root.
        if not _is_safe_issue_artifact_path(
            args.apply_structural_repair_action, project_root, str(args.issue_number)
        ):
            print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
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
                "--apply-structural-repair-action",
                args.apply_structural_repair_action,
            ]
        )
        if not is_exact_skill_runtime_structural_repair_action_apply_executor_command(
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
                "skill_runtime_exec: --anchor-comment-url is only allowed for an anchor-bound preflight profile",
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
    #
    # Issue #2311 AC1: canonical bare `preflight.run` now first-hops into
    # `workflow_start_entry.py` (not `run_refinement_preflight.py` directly).
    # Sibling anchor-comment-driven profiles (`preflight.run.with_anchor` /
    # `.with_human_context` / `.with_agent_report` / `.fixture` /
    # `.fixture.with_human_context`) are unaffected by this Issue and
    # continue to first-hop into `run_refinement_preflight.py`. This is a
    # three-way branch (not a generalized/loosened check): each branch is an
    # exact, explicit first-hop target for its command_id class.
    if is_decide_command:
        script_name = "decide_next_loop_action.py"
    elif args.command_id == "preflight.run":
        script_name = "workflow_start_entry.py"
    else:
        script_name = "run_refinement_preflight.py"
    script_path = Path(project_root) / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / script_name
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
    elif is_structural_repair_action_apply_command:
        render_params = {
            "issue_number": args.issue_number,
            "repo": args.repo,
            "preflight_result_path": args.apply_structural_repair_action,
        }
    else:
        render_params = {"issue_number": args.issue_number, "repo": args.repo}
        if is_fixture_command or is_fixture_human_context_command:
            render_params["fixture"] = args.fixture
        if is_anchor_command or is_contract_update_command or is_fixture_human_context_command:
            render_params["anchor_comment_url"] = args.anchor_comment_url
            if args.investigation_evidence_transport_path:
                render_params["investigation_evidence_transport_path"] = args.investigation_evidence_transport_path
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
# Issue #2378: closed remote-default-ref Git protocol builders. A consumer can
# express only protocol data flow; it cannot provide argv, flags, a refspec,
# a worktree action, or a lock reason.
# ---------------------------------------------------------------------------

_GIT_SUBPROCESS_EXECUTABLE_CACHE: str | None = None
_GIT_NO_LAZY_FETCH_CAPABILITY_CACHE: dict[str, bool] = {}
_CONTROL_PLANE_WORKTREE_LOCK_REASON = "loop-protocol-control-plane-default-ref"

# The short-lived per-invocation supervisor, never the long-lived executor
# host, opts into PR_SET_CHILD_SUBREAPER. Consequently an orphaned Git
# descendant is reparented only to the supervisor that spawned its Git leader;
# the host's unrelated children cannot enter this attribution domain.
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37


def _enable_linux_child_subreaper() -> bool:
    """Enable containment in an invocation supervisor before it spawns Git.

    This function is called exclusively after `_run_closed_git_process` has
    forked its short-lived supervisor. It must never run in the long-lived
    executor host: PR_SET_CHILD_SUBREAPER changes orphan reparenting semantics
    for the entire calling process and would otherwise make unrelated host
    children eligible for classification, signalling, or reaping.

    The supervisor remains alive until its one Git invocation reaches a
    confirmed terminal state, so an escaped descendant is attributable to the
    known Git leader rather than to the host. This containment implementation
    is deliberately Linux/WSL-only and fail-closed on other platforms; it runs
    only in the short-lived single-threaded supervisor after `fork()`.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        enabled = ctypes.c_int()
        if prctl(_PR_GET_CHILD_SUBREAPER, ctypes.addressof(enabled), 0, 0, 0) != 0:
            return False
        if enabled.value:
            return True
        if prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            return False
        confirmed = ctypes.c_int()
        return prctl(_PR_GET_CHILD_SUBREAPER, ctypes.addressof(confirmed), 0, 0, 0) == 0 and confirmed.value == 1
    except (AttributeError, OSError):
        return False


class GitProtocolDeadlineExhausted(RuntimeError):
    """Raised before a terminal command could consume cleanup reserve."""


class GitProtocolTimeout(RuntimeError):
    """Raised only after a containment backend confirms terminal cleanup."""


class GitProtocolProcessGroupCleanupFailed(RuntimeError):
    """Fail closed when dedicated process-group absence is not confirmed."""


@dataclass(frozen=True)
class GitProtocolDeadline:
    """One monotonic deadline shared by every step in one remote protocol."""

    deadline_at: float
    cleanup_reserve_seconds: float

    @classmethod
    def start(cls, timeout_seconds: float, cleanup_reserve_seconds: float = 1.0) -> "GitProtocolDeadline":
        timeout = _validate_deadline_value(timeout_seconds, "timeout")
        cleanup_reserve = _validate_deadline_value(cleanup_reserve_seconds, "cleanup_reserve")
        if timeout <= cleanup_reserve:
            raise ValueError("git_protocol_deadline_invalid")
        return cls(time.monotonic() + timeout, cleanup_reserve)

    def execution_seconds(self) -> float:
        deadline_at = _validate_deadline_value(self.deadline_at, "deadline_at")
        cleanup_reserve = _validate_deadline_value(self.cleanup_reserve_seconds, "cleanup_reserve")
        remaining = deadline_at - time.monotonic()
        if remaining <= cleanup_reserve:
            raise GitProtocolDeadlineExhausted("git_protocol_cleanup_reserve_required")
        return remaining - cleanup_reserve


def resolve_git_subprocess_executable(project_root: str) -> str:
    global _GIT_SUBPROCESS_EXECUTABLE_CACHE
    if _GIT_SUBPROCESS_EXECUTABLE_CACHE is None:
        _GIT_SUBPROCESS_EXECUTABLE_CACHE = _resolve_trusted_executable("git", project_root)
    return _GIT_SUBPROCESS_EXECUTABLE_CACHE


def _reset_git_subprocess_executable_cache_for_tests() -> None:
    global _GIT_SUBPROCESS_EXECUTABLE_CACHE
    _GIT_SUBPROCESS_EXECUTABLE_CACHE = None
    _GIT_NO_LAZY_FETCH_CAPABILITY_CACHE.clear()


def _resolve_trusted_ssh_command() -> str:
    ssh_path = shutil.which("ssh", path=os.pathsep.join(_safe_path_entries()))
    return f"{ssh_path} -oBatchMode=yes -oStrictHostKeyChecking=yes" if ssh_path else "false"


def sanitized_git_subprocess_env(project_root: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in GIT_SUBPROCESS_UNSET_ENV_KEYS}
    env["CLAUDE_PROJECT_DIR"] = project_root
    env["PATH"] = os.pathsep.join(_safe_path_entries())
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GIT_SSH_COMMAND"] = _resolve_trusted_ssh_command()
    return env


def git_subprocess_trusted_hooks_dir(scratch_root: str) -> str:
    scratch_path = Path(scratch_root)
    if not scratch_path.is_absolute() or _is_symlink_path(scratch_path):
        raise RuntimeError("git_subprocess_scratch_root_invalid")
    scratch_path.mkdir(parents=True, exist_ok=True)
    hooks_dir = Path(tempfile.mkdtemp(prefix=".skill-runtime-git-hooks-", dir=str(scratch_path)))
    if Path(os.path.realpath(hooks_dir)) != hooks_dir or any(hooks_dir.iterdir()):
        raise RuntimeError("git_subprocess_trusted_hooks_dir_invalid")
    return str(hooks_dir)


@dataclass(frozen=True)
class _GitOperation:
    """Private supported operation plus typed payload, never caller argv."""

    kind: str
    remote_url: LiteralRemoteUrl | None = None
    remote_ref: AllowedRemoteRef | None = None
    private_ref: ControlPlanePrivateRef | None = None
    object_id: RepositoryObjectId | None = None
    worktree_path: DetachedWorktreePath | None = None


_SUPPORTED_GIT_OPERATION_KINDS = frozenset(
    {
        "probe_rewrite",
        "probe_no_lazy_fetch_support",
        "probe_promisor_remote",
        "effective_remote_url",
        "observe_default_ref",
        "repository_object_format",
        "fetch_default_ref",
        "fetch_default_ref_no_lazy",
        "read_private_ref_oid",
        "require_commit_object",
        "read_worktree_head",
        "add_detached_locked_worktree",
        "remove_detached_locked_worktree",
        "list_worktrees_porcelain",
        "delete_private_ref_cas",
    }
)


def _validate_deadline_value(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"git_protocol_deadline_{field}_invalid")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"git_protocol_deadline_{field}_invalid")
    return normalized


def _revalidate_literal_remote_url(value: LiteralRemoteUrl) -> LiteralRemoteUrl:
    if not isinstance(value, LiteralRemoteUrl):
        raise TypeError("literal_remote_url_required")
    return validate_literal_remote_url(value.value)


def _revalidate_allowed_remote_ref(value: AllowedRemoteRef) -> AllowedRemoteRef:
    if not isinstance(value, AllowedRemoteRef):
        raise TypeError("allowed_remote_ref_required")
    return validate_allowed_remote_ref(value.value)


def _revalidate_private_ref(value: ControlPlanePrivateRef) -> ControlPlanePrivateRef:
    if not isinstance(value, ControlPlanePrivateRef):
        raise TypeError("control_plane_private_ref_required")
    return validate_control_plane_private_ref(value.value)


def _revalidate_object_format(value: RepositoryObjectFormat) -> RepositoryObjectFormat:
    if not isinstance(value, RepositoryObjectFormat):
        raise TypeError("repository_object_format_required")
    return validate_repository_object_format(value.value)


def _revalidate_object_id(
    value: RepositoryObjectId, object_format: RepositoryObjectFormat | None = None
) -> RepositoryObjectId:
    if not isinstance(value, RepositoryObjectId):
        raise TypeError("repository_object_id_required")
    format_value = object_format
    if format_value is None:
        format_value = validate_repository_object_format("sha1" if len(value.value) == 40 else "sha256")
    return validate_repository_object_id(value.value, _revalidate_object_format(format_value))


def _revalidate_worktree_path(
    value: DetachedWorktreePath, project_root: str, *, require_fresh: bool
) -> DetachedWorktreePath:
    if not isinstance(value, DetachedWorktreePath):
        raise TypeError("detached_worktree_path_required")
    validator = validate_detached_worktree_path if require_fresh else validate_existing_detached_worktree_path
    return validator(value.value, project_root)


def _revalidate_semantic_operation(operation: _GitOperation, project_root: str) -> _GitOperation:
    """Revalidate path-bearing internal operations before even the rewrite probe.

    `_GitOperation` is private, but it remains a trust boundary: a forged
    `DetachedWorktreePath` must not turn into argv merely because it carries
    the right runtime type.
    """
    if not isinstance(operation, _GitOperation):
        raise TypeError("git_operation_required")
    if operation.kind not in _SUPPORTED_GIT_OPERATION_KINDS:
        raise ValueError("git_operation_not_supported")
    if operation.kind == "add_detached_locked_worktree":
        return _GitOperation(
            operation.kind,
            object_id=operation.object_id,
            worktree_path=_revalidate_worktree_path(operation.worktree_path, project_root, require_fresh=True),
        )
    if operation.kind == "remove_detached_locked_worktree":
        return _GitOperation(
            operation.kind,
            worktree_path=_revalidate_worktree_path(operation.worktree_path, project_root, require_fresh=False),
        )
    # `fetch_default_ref_no_lazy` is internally synthesized only after the
    # trusted executable passed the fixed capability probe. Rebuild every
    # payload-bearing operation so a forged dataclass cannot smuggle state.
    if operation.kind in {"fetch_default_ref", "fetch_default_ref_no_lazy"}:
        return _GitOperation(
            "fetch_default_ref",
            remote_url=operation.remote_url,
            remote_ref=operation.remote_ref,
            private_ref=operation.private_ref,
        )
    return operation


@dataclass(frozen=True)
class _TrackedGitDescendant:
    """A Linux process identity observed beneath a dedicated Git leader."""

    pid: int
    start_time: str


def _linux_process_identity(pid: int) -> tuple[int, int, str] | None:
    """Read `(pid, parent_pid, start_time)` without trusting a reused PID."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    _prefix, separator, suffix = stat.rpartition(")")
    fields = suffix.split()
    if not separator or len(fields) < 20:
        return None
    try:
        return (pid, int(fields[1]), fields[19])
    except ValueError:
        return None


def _observe_git_descendants(leader_pid: int) -> set[_TrackedGitDescendant] | None:
    """Observe the live descendant tree before a child can escape its group.

    A descendant may call `setsid()` and leave the group created by
    `start_new_session`. Linux `/proc` parent links are an advisory snapshot
    only: an escaped child can fork a delayed descendant and exit between two
    observations. Callers must therefore never use a snapshot as proof that
    all descendants are absent; terminal cleanup remains fail-closed.
    """
    proc_root = Path("/proc")
    if not sys.platform.startswith("linux") or not proc_root.is_dir():
        return None
    records: dict[int, tuple[int, str]] = {}
    try:
        proc_entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in proc_entries:
        if not entry.name.isdecimal():
            continue
        identity = _linux_process_identity(int(entry.name))
        if identity is not None:
            pid, parent_pid, start_time = identity
            records[pid] = (parent_pid, start_time)
    descendants: set[_TrackedGitDescendant] = set()
    frontier = {leader_pid}
    while frontier:
        parent_pid = frontier.pop()
        children = {
            pid: start_time
            for pid, (record_parent_pid, start_time) in records.items()
            if record_parent_pid == parent_pid
        }
        frontier.update(children)
        descendants.update(_TrackedGitDescendant(pid, start_time) for pid, start_time in children.items())
    return descendants


def _tracked_descendants_absent(descendants: set[_TrackedGitDescendant]) -> bool:
    return all(
        (identity := _linux_process_identity(descendant.pid)) is None or identity[2] != descendant.start_time
        for descendant in descendants
    )


def _observe_invocation_supervisor_children() -> set[_TrackedGitDescendant] | None:
    """Return live children adopted by this one-invocation supervisor.

    This is deliberately distinct from `_observe_git_descendants`: a readable
    tree snapshot rooted at the Git leader is only advisory. Once that leader
    is reaped, the supervisor's PR_SET_CHILD_SUBREAPER gives an empty
    direct-child set a kernel-parentage meaning: no live orphaned descendant
    of that Git leader remains. The supervisor spawned no other child, and an
    unreadable `/proc` result is never interpreted as that proof.
    """
    proc_root = Path("/proc")
    if not sys.platform.startswith("linux") or not proc_root.is_dir():
        return None
    parent_pid = os.getpid()
    children: set[_TrackedGitDescendant] = set()
    try:
        proc_entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in proc_entries:
        if not entry.name.isdecimal():
            continue
        identity = _linux_process_identity(int(entry.name))
        if identity is not None and identity[1] == parent_pid:
            children.add(_TrackedGitDescendant(identity[0], identity[2]))
    return children


def _observe_descendants_of(parent_pids: set[int]) -> set[_TrackedGitDescendant] | None:
    """Return the complete current tree below known Linux parent PIDs."""
    proc_root = Path("/proc")
    if not sys.platform.startswith("linux") or not proc_root.is_dir():
        return None
    records: dict[int, tuple[int, str]] = {}
    try:
        proc_entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in proc_entries:
        if not entry.name.isdecimal():
            continue
        identity = _linux_process_identity(int(entry.name))
        if identity is not None:
            records[identity[0]] = (identity[1], identity[2])
    descendants: set[_TrackedGitDescendant] = set()
    frontier = set(parent_pids)
    while frontier:
        parent_pid = frontier.pop()
        children = {
            pid: start_time
            for pid, (record_parent_pid, start_time) in records.items()
            if record_parent_pid == parent_pid
        }
        frontier.update(children)
        descendants.update(_TrackedGitDescendant(pid, start_time) for pid, start_time in children.items())
    return descendants


def _signal_invocation_child_trees(children: set[_TrackedGitDescendant], signal_number: int) -> bool:
    descendants = _observe_descendants_of({child.pid for child in children})
    if descendants is None:
        return False
    return _signal_tracked_descendants(children | descendants, signal_number)


def _reap_tracked_children(children: set[_TrackedGitDescendant]) -> bool:
    """Reap only verified direct subreaper children; never a reused PID."""
    for child in children:
        identity = _linux_process_identity(child.pid)
        if identity is None or identity[2] != child.start_time:
            continue
        try:
            os.waitpid(child.pid, os.WNOHANG)
        except ChildProcessError:
            # It was reaped by a concurrent wait; it is no longer a leak.
            continue
        except OSError:
            return False
    return True


def _terminate_invocation_supervisor_children(deadline: GitProtocolDeadline) -> bool:
    """Drain Git descendants adopted by this invocation supervisor only.

    A direct child of the supervisor may itself fork after SIGTERM. Repeating
    discovery is not used as a timing heuristic: its parentage is still within
    the known Git invocation. No host child can be seen by this process.
    """
    term_until = min(deadline.deadline_at, time.monotonic() + max(0.01, deadline.cleanup_reserve_seconds / 2))
    for signal_number, phase_deadline in ((signal.SIGTERM, term_until), (signal.SIGKILL, deadline.deadline_at)):
        while time.monotonic() < phase_deadline:
            children = _observe_invocation_supervisor_children()
            if children is None:
                return False
            if not children:
                return True
            if not _signal_invocation_child_trees(children, signal_number):
                return False
            if not _reap_tracked_children(children):
                return False
            time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
    children = _observe_invocation_supervisor_children()
    if children is None:
        return False
    if children and not _signal_invocation_child_trees(children, signal.SIGKILL):
        return False
    if children and not _reap_tracked_children(children):
        return False
    final_children = _observe_invocation_supervisor_children()
    return final_children is not None and not final_children


def _signal_tracked_descendants(descendants: set[_TrackedGitDescendant], signal_number: int) -> bool:
    """Signal only descendants whose PID identity still matches observation."""
    for descendant in descendants:
        identity = _linux_process_identity(descendant.pid)
        if identity is None or identity[2] != descendant.start_time:
            continue
        try:
            os.kill(descendant.pid, signal_number)
        except ProcessLookupError:
            continue
        except OSError:
            return False
    return True


def _terminate_git_process_group(
    proc: subprocess.Popen,
    pgid: int | None,
    deadline: GitProtocolDeadline,
    descendants: set[_TrackedGitDescendant] | None,
) -> bool:
    """Bounded cleanup with supervisor-scoped containment confirmation.

    The caller is the invocation supervisor. Its subreaper domain contains
    only the Git leader it spawned and descendants subsequently orphaned from
    that leader, never a child of the long-lived executor host.
    """
    if not _POSIX_PROCESS_GROUP_SUPPORTED or pgid is None:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=max(0.0, deadline.deadline_at - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=max(0.0, deadline.deadline_at - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired):
                pass
        return False

    if descendants is None:
        # Observation itself failed after Popen. We cannot confirm containment,
        # but the known dedicated group must still receive bounded TERM/KILL.
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        term_until = min(deadline.deadline_at, time.monotonic() + max(0.01, deadline.cleanup_reserve_seconds / 2))
        while time.monotonic() < term_until:
            if _verify_process_group_absent(pgid):
                try:
                    proc.wait(timeout=0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                _terminate_invocation_supervisor_children(deadline)
                return False
            time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        while time.monotonic() < deadline.deadline_at:
            if _verify_process_group_absent(pgid):
                try:
                    proc.wait(timeout=0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                _terminate_invocation_supervisor_children(deadline)
                return False
            time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
        _terminate_invocation_supervisor_children(deadline)
        return False

    def confirmed_absence() -> bool:
        children = _observe_invocation_supervisor_children()
        if children is None:
            return False
        if children:
            # An escaped descendant was observed in the isolated supervisor.
            # Reap it, but do not promote an escape into successful cleanup.
            _terminate_invocation_supervisor_children(deadline)
            return False
        return True

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        return False
    if not _signal_tracked_descendants(descendants, signal.SIGTERM):
        return False
    term_until = min(deadline.deadline_at, time.monotonic() + max(0.01, deadline.cleanup_reserve_seconds / 2))
    while time.monotonic() < term_until:
        proc.poll()
        if _verify_process_group_absent(pgid) and _tracked_descendants_absent(descendants):
            try:
                proc.wait(timeout=0)
            except (OSError, subprocess.TimeoutExpired):
                return False
            return confirmed_absence()
        time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        return False
    if not _signal_tracked_descendants(descendants, signal.SIGKILL):
        return False
    while time.monotonic() < deadline.deadline_at:
        proc.poll()
        if _verify_process_group_absent(pgid) and _tracked_descendants_absent(descendants):
            try:
                proc.wait(timeout=0)
            except (OSError, subprocess.TimeoutExpired):
                return False
            return confirmed_absence()
        time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
    # Ensure only invocation-attributable adopted children are not left as
    # zombies, even though final confirmation has failed.
    _terminate_invocation_supervisor_children(deadline)
    return False


def _operation_arguments(operation: _GitOperation, *, cwd: str) -> list[str]:
    """Construct exact argv only from a supported operation and typed payload."""
    if not isinstance(operation, _GitOperation) or operation.kind not in _SUPPORTED_GIT_OPERATION_KINDS:
        raise ValueError("git_operation_not_supported")
    if operation.kind == "probe_rewrite":
        return ["-C", cwd, "config", "--get-regexp", "--name-only", "-z", INSTEADOF_CONFIG_NAME_REGEXP]
    if operation.kind == "probe_no_lazy_fetch_support":
        return ["--version"]
    if operation.kind == "probe_promisor_remote":
        return ["config", "--local", "--get-regexp", r"^remote\..*\.promisor$"]
    if operation.kind == "effective_remote_url":
        remote_url = _revalidate_literal_remote_url(operation.remote_url)
        return ["ls-remote", "--get-url", remote_url.value]
    if operation.kind == "observe_default_ref":
        remote_url = _revalidate_literal_remote_url(operation.remote_url)
        return ["ls-remote", "--exit-code", "--symref", remote_url.value, "HEAD"]
    if operation.kind == "repository_object_format":
        return ["rev-parse", "--show-object-format"]
    if operation.kind in {"fetch_default_ref", "fetch_default_ref_no_lazy"}:
        remote_url = _revalidate_literal_remote_url(operation.remote_url)
        remote_ref = _revalidate_allowed_remote_ref(operation.remote_ref)
        private_ref = _revalidate_private_ref(operation.private_ref)
        return [
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "--no-write-fetch-head",
            remote_url.value,
            f"{remote_ref.value}:{private_ref.value}",
        ]
    if operation.kind == "read_private_ref_oid":
        private_ref = _revalidate_private_ref(operation.private_ref)
        return ["rev-parse", "--verify", "--quiet", private_ref.value]
    if operation.kind == "require_commit_object":
        object_id = _revalidate_object_id(operation.object_id)
        return ["cat-file", "-e", object_id.value + "^{commit}"]
    if operation.kind == "read_worktree_head":
        return ["rev-parse", "--verify", "HEAD"]
    if operation.kind == "add_detached_locked_worktree":
        path = operation.worktree_path
        object_id = _revalidate_object_id(operation.object_id)
        if not isinstance(path, DetachedWorktreePath):
            raise TypeError("detached_worktree_path_required")
        return [
            "worktree",
            "add",
            "--detach",
            "--lock",
            "--reason",
            _CONTROL_PLANE_WORKTREE_LOCK_REASON,
            path.value,
            object_id.value,
        ]
    if operation.kind == "remove_detached_locked_worktree":
        path = operation.worktree_path
        if not isinstance(path, DetachedWorktreePath):
            raise TypeError("detached_worktree_path_required")
        return ["worktree", "remove", "--force", "--force", path.value]
    if operation.kind == "list_worktrees_porcelain":
        return ["worktree", "list", "--porcelain"]
    if operation.kind == "delete_private_ref_cas":
        private_ref = _revalidate_private_ref(operation.private_ref)
        object_id = _revalidate_object_id(operation.object_id)
        return ["update-ref", "-d", private_ref.value, object_id.value]
    raise AssertionError("unreachable_supported_operation")


def _exact_git_argv(operation: _GitOperation, *, git_executable: str, cwd: str, hooks_dir: str) -> list[str]:
    no_lazy_fetch = ("--no-lazy-fetch",) if operation.kind in {
        "probe_no_lazy_fetch_support",
        "fetch_default_ref_no_lazy",
    } else ()
    return [
        git_executable,
        "--no-replace-objects",
        "-c",
        "core.hooksPath=" + hooks_dir,
        "-c",
        "credential.helper=",
        *no_lazy_fetch,
        *_operation_arguments(operation, cwd=cwd),
    ]


_INVOCATION_SUPERVISOR_MAX_RESULT_BYTES = 32 * 1024 * 1024
_INVOCATION_SUPERVISOR_READY = b"R"
_INVOCATION_SUPERVISOR_CLEANUP_ABSENT = b"A"
_INVOCATION_SUPERVISOR_CLEANUP_UNCONFIRMED = b"U"


class _InvocationSupervisorCleanupRequested(BaseException):
    """Ask the isolated supervisor to clean its Git-only child tree."""


def _request_invocation_supervisor_cleanup(_signum: int, _frame: object) -> None:
    raise _InvocationSupervisorCleanupRequested()


def _record_git_descendant_observation(
    proc: subprocess.Popen, descendants: set[_TrackedGitDescendant] | None
) -> set[_TrackedGitDescendant] | None:
    """Merge an advisory observation without letting its failure skip cleanup."""
    try:
        observed = _observe_git_descendants(proc.pid)
    except BaseException:
        return None
    if descendants is None or observed is None:
        return None
    descendants.update(observed)
    return descendants


def _drain_invocation_children_after_leader_exit(pgid: int, deadline: GitProtocolDeadline) -> str:
    """Classify only supervisor-adopted Git children after leader exit.

    A helper in the original dedicated Git session is an ordinary Git
    descendant completing shutdown, even when a non-interactive shell gives it
    another process group. An adopted child in another session escaped that
    containment and remains a fail-closed leak. Both cases are observed solely
    inside the short-lived invocation supervisor.
    """
    # start_new_session=True makes the leader PID both session and process
    # group ID; retain that numeric session identity after the leader is reaped.
    leader_session_id = pgid
    drain_until = min(deadline.deadline_at, time.monotonic() + max(0.01, deadline.cleanup_reserve_seconds / 2))
    while time.monotonic() < drain_until:
        children = _observe_invocation_supervisor_children()
        if children is None:
            return "unconfirmed"
        if not children:
            return "absent"
        for child in children:
            identity = _linux_process_identity(child.pid)
            if identity is None or identity[2] != child.start_time:
                continue
            try:
                if os.getsid(child.pid) != leader_session_id:
                    if not _terminate_invocation_supervisor_children(deadline):
                        return "unconfirmed"
                    # It is still a Git-derived child in this isolated
                    # supervisor. Confirmed cleanup is sufficient; do not
                    # reclassify it as a host child or leave it running.
                    return "absent"
            except OSError:
                return "unconfirmed"
        if not _reap_tracked_children(children):
            return "unconfirmed"
        time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
    children = _observe_invocation_supervisor_children()
    if children is None:
        return "unconfirmed"
    if not children:
        return "absent"
    if not _terminate_invocation_supervisor_children(deadline):
        return "unconfirmed"
    return "absent"


def _run_closed_git_process_in_invocation_supervisor(
    operation: _GitOperation,
    *,
    git_executable: str,
    cwd: str,
    env: dict[str, str],
    hooks_dir: str,
    deadline: GitProtocolDeadline,
) -> subprocess.CompletedProcess:
    """Run one Git command inside its short-lived subreaper supervisor."""
    if not _enable_linux_child_subreaper():
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_containment_unavailable")
    argv = _exact_git_argv(operation, git_executable=git_executable, cwd=cwd, hooks_dir=hooks_dir)
    kwargs: dict[str, object] = {
        "cwd": cwd,
        "env": env,
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": operation.kind != "probe_rewrite",
    }
    posix_supervised = _POSIX_PROCESS_GROUP_SUPPORTED
    if posix_supervised:
        kwargs["start_new_session"] = True
    proc: subprocess.Popen | None = None
    pgid: int | None = None
    descendants: set[_TrackedGitDescendant] | None = set() if posix_supervised else None
    try:
        proc = subprocess.Popen(argv, **kwargs)  # type: ignore[arg-type]
        pgid = proc.pid if posix_supervised else None
        # This initial observation is deliberately inside the guarded region:
        # observer failures occur after Popen and therefore require bounded
        # cleanup just like communicate(), poll(), and timeout failures.
        if posix_supervised:
            descendants = _record_git_descendant_observation(proc, descendants)
        while True:
            if posix_supervised:
                descendants = _record_git_descendant_observation(proc, descendants)
            try:
                stdout, stderr = proc.communicate(
                    timeout=min(_GROUP_POLL_INTERVAL_SECONDS, deadline.execution_seconds())
                )
                break
            except subprocess.TimeoutExpired:
                continue
    except GitProtocolDeadlineExhausted as exc:
        if proc is None or not _terminate_git_process_group(proc, pgid, deadline, descendants):
            raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed") from exc
        raise GitProtocolTimeout("git_process_timeout") from exc
    except BaseException as exc:
        if proc is None or not _terminate_git_process_group(proc, pgid, deadline, descendants):
            raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed") from exc
        raise

    # A non-success result is terminal, except the documented no-match result
    # of the rewrite probe. Cleanup remains mandatory before its result leaves
    # this supervisor.
    if proc.returncode != 0 and not (operation.kind == "probe_rewrite" and proc.returncode == 1):
        if not _terminate_git_process_group(proc, pgid, deadline, descendants):
            raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
    if posix_supervised and descendants is None:
        _terminate_git_process_group(proc, pgid, deadline, descendants)
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")

    # communicate() has reaped the direct Git leader. The invocation
    # supervisor may still have adopted a normal helper in that leader's
    # process group; drain it without widening attribution to any host child.
    if not posix_supervised or pgid is None:
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
    drain_result = _drain_invocation_children_after_leader_exit(pgid, deadline)
    if drain_result == "unconfirmed":
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
    if drain_result != "absent":
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_descendant_leak")
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _encode_invocation_supervisor_stream(value: str | bytes | None) -> dict[str, object]:
    if isinstance(value, str):
        raw = value.encode("utf-8")
        text = True
    elif isinstance(value, bytes):
        raw = value
        text = False
    else:
        raw = b""
        text = True
    return {"text": text, "data": base64.b64encode(raw).decode("ascii")}


def _decode_invocation_supervisor_stream(value: object) -> str | bytes | None:
    if not isinstance(value, dict) or not isinstance(value.get("text"), bool) or not isinstance(value.get("data"), str):
        raise ValueError("invalid_invocation_supervisor_stream")
    raw = base64.b64decode(value["data"], validate=True)
    return raw.decode("utf-8") if value["text"] else raw


def _write_invocation_supervisor_result(write_fd: int, outcome: dict[str, object]) -> None:
    """Send one bounded JSON frame over a private parent/supervisor pipe."""
    try:
        payload = json.dumps(outcome, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > _INVOCATION_SUPERVISOR_MAX_RESULT_BYTES:
            return
        frame = struct.pack("!Q", len(payload)) + payload
        sent = 0
        while sent < len(frame):
            sent += os.write(write_fd, frame[sent:])
    except (OSError, TypeError, ValueError):
        return


def _supervisor_cleanup_deadline(deadline: GitProtocolDeadline) -> GitProtocolDeadline:
    """Reserve a bounded post-interruption window for the Git-only handshake."""
    return GitProtocolDeadline(
        deadline_at=max(
            deadline.deadline_at,
            time.monotonic() + max(0.1, deadline.cleanup_reserve_seconds),
        ),
        cleanup_reserve_seconds=deadline.cleanup_reserve_seconds,
    )


def _write_invocation_supervisor_cleanup_status(write_fd: int, status: bytes) -> None:
    try:
        os.write(write_fd, status)
    except OSError:
        pass


def _invocation_supervisor_main(
    write_fd: int,
    ready_write_fd: int,
    cleanup_write_fd: int,
    operation: _GitOperation,
    *,
    git_executable: str,
    cwd: str,
    env: dict[str, str],
    hooks_dir: str,
    deadline: GitProtocolDeadline,
) -> None:
    """Execute one Git operation and certify Git-only cleanup before exit."""
    handler_installed = False
    try:
        # The parent waits for this byte before it can interrupt result reading.
        # Therefore a parent-requested SIGTERM cannot arrive before this handler
        # converts it into the normal, bounded Git cleanup path.
        signal.signal(signal.SIGTERM, _request_invocation_supervisor_cleanup)
        handler_installed = True
        _write_invocation_supervisor_cleanup_status(ready_write_fd, _INVOCATION_SUPERVISOR_READY)
        try:
            completed = _run_closed_git_process_in_invocation_supervisor(
                operation,
                git_executable=git_executable,
                cwd=cwd,
                env=env,
                hooks_dir=hooks_dir,
                deadline=deadline,
            )
            outcome: dict[str, object] = {
                "kind": "completed",
                "argv": completed.args,
                "returncode": completed.returncode,
                "stdout": _encode_invocation_supervisor_stream(completed.stdout),
                "stderr": _encode_invocation_supervisor_stream(completed.stderr),
            }
        except BaseException as exc:
            outcome = {"kind": "raised", "type": type(exc).__name__, "message": str(exc)}
        _write_invocation_supervisor_result(write_fd, outcome)
    except BaseException as exc:
        # A signal during result serialization still reaches the final cleanup
        # handshake. Its result is deliberately not trusted by the parent.
        _write_invocation_supervisor_result(
            write_fd, {"kind": "raised", "type": type(exc).__name__, "message": str(exc)}
        )
    finally:
        if handler_installed:
            try:
                # Once cancellation has been accepted, do not permit a second
                # SIGTERM to interrupt the cleanup proof.
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            except (OSError, ValueError):
                pass
        cleanup_confirmed = _terminate_invocation_supervisor_children(_supervisor_cleanup_deadline(deadline))
        _write_invocation_supervisor_cleanup_status(
            cleanup_write_fd,
            _INVOCATION_SUPERVISOR_CLEANUP_ABSENT
            if cleanup_confirmed
            else _INVOCATION_SUPERVISOR_CLEANUP_UNCONFIRMED,
        )
        for fd in (write_fd, ready_write_fd, cleanup_write_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        os._exit(0 if cleanup_confirmed else 1)


def _read_invocation_supervisor_cleanup_status(
    read_fd: int, deadline_at: float, *, expected: bytes
) -> bool:
    """Read one bounded supervisor-only handshake byte and close its pipe."""
    try:
        while time.monotonic() < deadline_at:
            readable, _writable, _exceptional = select.select(
                [read_fd], [], [], min(_GROUP_POLL_INTERVAL_SECONDS, deadline_at - time.monotonic())
            )
            if not readable:
                continue
            return os.read(read_fd, 1) == expected
        return False
    except (OSError, ValueError):
        return False
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass


def _wait_for_invocation_supervisor_reap(supervisor_pid: int, deadline_at: float) -> bool:
    """Reap only the known direct supervisor, never a host process."""
    while time.monotonic() < deadline_at:
        try:
            waited_pid, _status = os.waitpid(supervisor_pid, os.WNOHANG)
        except ChildProcessError:
            # SIGCHLD may be configured for automatic reaping. The cleanup
            # pipe is still bound to the original supervisor, so it remains a
            # valid absence certificate without any PID-directed action.
            return True
        except OSError:
            return False
        if waited_pid == supervisor_pid:
            return True
        time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
    return False


def _stop_invocation_supervisor(supervisor_pid: int) -> None:
    """Last-resort SIGKILL of a still-known direct supervisor only."""
    try:
        waited_pid, _status = os.waitpid(supervisor_pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return
    if waited_pid == supervisor_pid:
        return
    try:
        os.kill(supervisor_pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        return
    try:
        os.waitpid(supervisor_pid, 0)
    except ChildProcessError:
        pass


def _cleanup_interrupted_invocation_supervisor(
    supervisor_pid: int, cleanup_read_fd: int, deadline: GitProtocolDeadline
) -> bool:
    """Request and verify bounded Git-only cleanup before propagating failure.

    The parent can signal and reap exactly its known one-invocation supervisor.
    It never scans, signals, or reaps a host child. The supervisor's subreaper
    acknowledgement is accepted only after its direct-child set proves the Git
    leader and every escaped descendant absent.
    """
    # The supervisor may first spend its ordinary reserved cleanup phase
    # terminating the leader and then run the final subreaper absence proof.
    # Keep the parent handshake bounded while allowing both Git-only phases.
    cleanup_deadline_at = max(
        deadline.deadline_at,
        time.monotonic() + max(0.1, deadline.cleanup_reserve_seconds) * 3,
    )
    try:
        waited_pid, _status = os.waitpid(supervisor_pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        try:
            os.close(cleanup_read_fd)
        except OSError:
            pass
        return False
    supervisor_reaped = waited_pid == supervisor_pid
    if not supervisor_reaped:
        try:
            os.kill(supervisor_pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            try:
                os.close(cleanup_read_fd)
            except OSError:
                pass
            return False
    cleanup_absent = _read_invocation_supervisor_cleanup_status(
        cleanup_read_fd, cleanup_deadline_at, expected=_INVOCATION_SUPERVISOR_CLEANUP_ABSENT
    )
    if cleanup_absent and (
        supervisor_reaped or _wait_for_invocation_supervisor_reap(supervisor_pid, cleanup_deadline_at)
    ):
        return True
    _stop_invocation_supervisor(supervisor_pid)
    return False


def _read_invocation_supervisor_result(
    read_fd: int, supervisor_pid: int, deadline: GitProtocolDeadline
) -> dict[str, object] | None:
    """Read a bounded result while retaining a deadline for supervisor cleanup."""
    data = bytearray()
    result_deadline_at = deadline.deadline_at + max(0.1, _GROUP_POLL_INTERVAL_SECONDS * 2)
    try:
        while time.monotonic() < result_deadline_at:
            readable, _writable, _exceptional = select.select(
                [read_fd], [], [], min(_GROUP_POLL_INTERVAL_SECONDS, result_deadline_at - time.monotonic())
            )
            if not readable:
                continue
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > _INVOCATION_SUPERVISOR_MAX_RESULT_BYTES + 8:
                return None
    except (OSError, ValueError):
        return None
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
    if len(data) < 8:
        return None
    size = struct.unpack("!Q", data[:8])[0]
    if size > _INVOCATION_SUPERVISOR_MAX_RESULT_BYTES or len(data) != size + 8:
        return None
    try:
        outcome = json.loads(data[8:].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(outcome, dict) or outcome.get("kind") not in {"completed", "raised"}:
        return None
    while time.monotonic() < result_deadline_at:
        try:
            waited_pid, _status = os.waitpid(supervisor_pid, os.WNOHANG)
        except OSError:
            return None
        if waited_pid == supervisor_pid:
            return outcome
        time.sleep(_GROUP_POLL_INTERVAL_SECONDS)
    return None


def _raise_invocation_supervisor_error(outcome: dict[str, object]) -> None:
    error_type = outcome.get("type")
    message = outcome.get("message")
    if not isinstance(error_type, str) or not isinstance(message, str):
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
    error_classes: dict[str, type[BaseException]] = {
        "GitProtocolDeadlineExhausted": GitProtocolDeadlineExhausted,
        "GitProtocolTimeout": GitProtocolTimeout,
        "GitProtocolProcessGroupCleanupFailed": GitProtocolProcessGroupCleanupFailed,
        "RuntimeError": RuntimeError,
        "ValueError": ValueError,
        "TypeError": TypeError,
    }
    error_class = error_classes.get(error_type)
    if error_class is None:
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
    raise error_class(message)


def _run_closed_git_process(
    operation: _GitOperation,
    *,
    git_executable: str,
    cwd: str,
    env: dict[str, str],
    hooks_dir: str,
    deadline: GitProtocolDeadline,
) -> subprocess.CompletedProcess:
    """Delegate one Git process to a one-invocation subreaper supervisor.

    The host creates and waits for only the known supervisor PID. It never
    enables subreaping, scans `/proc` for host children, signals them, or reaps
    them. A failed or interrupted result read first performs a bounded,
    supervisor-certified Git-only cleanup handshake before it propagates.
    """
    deadline.execution_seconds()
    try:
        result_read_fd, result_write_fd = os.pipe()
        ready_read_fd, ready_write_fd = os.pipe()
        cleanup_read_fd, cleanup_write_fd = os.pipe()
        supervisor_pid = os.fork()
    except (AttributeError, OSError) as exc:
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_containment_unavailable") from exc
    if supervisor_pid == 0:
        try:
            for fd in (result_read_fd, ready_read_fd, cleanup_read_fd):
                os.close(fd)
            _invocation_supervisor_main(
                result_write_fd,
                ready_write_fd,
                cleanup_write_fd,
                operation,
                git_executable=git_executable,
                cwd=cwd,
                env=env,
                hooks_dir=hooks_dir,
                deadline=deadline,
            )
        finally:
            os._exit(1)
    for fd in (result_write_fd, ready_write_fd, cleanup_write_fd):
        os.close(fd)

    startup_deadline_at = min(
        deadline.deadline_at,
        time.monotonic() + max(0.1, deadline.cleanup_reserve_seconds),
    )
    try:
        supervisor_ready = _read_invocation_supervisor_cleanup_status(
            ready_read_fd, startup_deadline_at, expected=_INVOCATION_SUPERVISOR_READY
        )
    except BaseException as exc:
        try:
            os.close(result_read_fd)
        except OSError:
            pass
        if not _cleanup_interrupted_invocation_supervisor(supervisor_pid, cleanup_read_fd, deadline):
            raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed") from exc
        raise
    if not supervisor_ready:
        try:
            os.close(result_read_fd)
        except OSError:
            pass
        if not _cleanup_interrupted_invocation_supervisor(supervisor_pid, cleanup_read_fd, deadline):
            raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")

    try:
        outcome = _read_invocation_supervisor_result(result_read_fd, supervisor_pid, deadline)
    except BaseException as exc:
        try:
            # The normal reader closes this descriptor in its finally block;
            # repeat it defensively so an interrupted/custom reader cannot
            # block the supervisor while it reports cancellation.
            os.close(result_read_fd)
        except OSError:
            pass
        if not _cleanup_interrupted_invocation_supervisor(supervisor_pid, cleanup_read_fd, deadline):
            raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed") from exc
        raise
    if outcome is None:
        if not _cleanup_interrupted_invocation_supervisor(supervisor_pid, cleanup_read_fd, deadline):
            raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")

    # Result success is not a cleanup proof. The isolated supervisor writes
    # this acknowledgement only after its subreaper observes no Git leader or
    # escaped descendants and exits; `_read_invocation_supervisor_result` has
    # already reaped that exact known supervisor PID.
    normal_cleanup_deadline_at = _supervisor_cleanup_deadline(deadline).deadline_at
    if not _read_invocation_supervisor_cleanup_status(
        cleanup_read_fd, normal_cleanup_deadline_at, expected=_INVOCATION_SUPERVISOR_CLEANUP_ABSENT
    ):
        raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed")
    if outcome["kind"] == "completed":
        try:
            argv = outcome["argv"]
            returncode = outcome["returncode"]
            if (
                not isinstance(argv, list)
                or not all(isinstance(arg, str) for arg in argv)
                or not isinstance(returncode, int)
            ):
                raise ValueError("invalid_invocation_supervisor_result")
            return subprocess.CompletedProcess(
                argv,
                returncode,
                _decode_invocation_supervisor_stream(outcome.get("stdout")),
                _decode_invocation_supervisor_stream(outcome.get("stderr")),
            )
        except (TypeError, ValueError, KeyError):
            raise GitProtocolProcessGroupCleanupFailed("git_process_group_cleanup_unconfirmed") from None
    _raise_invocation_supervisor_error(outcome)
    raise AssertionError("unreachable_invocation_supervisor_error")


def _git_supports_no_lazy_fetch(
    git_executable: str, *, cwd: str, env: dict[str, str], hooks_dir: str, deadline: GitProtocolDeadline
) -> bool:
    """Probe the trusted executable once; never infer capability from an env var."""
    cached = _GIT_NO_LAZY_FETCH_CAPABILITY_CACHE.get(git_executable)
    if cached is not None:
        return cached
    probe = _run_closed_git_process(
        _GitOperation("probe_no_lazy_fetch_support"),
        git_executable=git_executable,
        cwd=cwd,
        env=env,
        hooks_dir=hooks_dir,
        deadline=deadline,
    )
    supported = probe.returncode == 0
    _GIT_NO_LAZY_FETCH_CAPABILITY_CACHE[git_executable] = supported
    return supported


def _repository_has_promisor_remote(
    git_executable: str, *, cwd: str, env: dict[str, str], hooks_dir: str, deadline: GitProtocolDeadline
) -> bool:
    """Detect a local partial-clone promisor without consulting ambient config."""
    probe = _run_closed_git_process(
        _GitOperation("probe_promisor_remote"),
        git_executable=git_executable,
        cwd=cwd,
        env=env,
        hooks_dir=hooks_dir,
        deadline=deadline,
    )
    if probe.returncode == 1:
        return False
    if probe.returncode != 0:
        raise RuntimeError(f"promisor_remote_probe_failed:{probe.returncode}")
    return any(
        line.rsplit(maxsplit=1)[-1].lower() in {"true", "yes", "on", "1"}
        for line in (probe.stdout or "").splitlines()
        if line.split()
    )


def _require_literal_effective_remote_url(
    remote_url: LiteralRemoteUrl,
    *,
    git_executable: str,
    cwd: str,
    env: dict[str, str],
    hooks_dir: str,
    deadline: GitProtocolDeadline,
) -> None:
    """Fail closed only when Git rewrites the transport actually in use.

    An unrelated `insteadOf` or any `pushInsteadOf` key is not authority for a
    read/fetch protocol. `ls-remote --get-url` is local/no-network and returns
    the exact URL Git would use for this literal remote.
    """
    effective = _require_success(
        _run_closed_git_process(
            _GitOperation("effective_remote_url", remote_url=remote_url),
            git_executable=git_executable,
            cwd=cwd,
            env=env,
            hooks_dir=hooks_dir,
            deadline=deadline,
        ),
        "effective_remote_url",
    )
    if (effective.stdout or "").rstrip("\n") != remote_url.value:
        raise RuntimeError("effective_remote_url_mismatch")


def _execute_semantic_git(
    operation: _GitOperation,
    *,
    cwd: str,
    project_root: str,
    scratch_root: str | None,
    deadline: GitProtocolDeadline,
) -> subprocess.CompletedProcess:
    operation = _revalidate_semantic_operation(operation, project_root)
    deadline.execution_seconds()
    git = resolve_git_subprocess_executable(project_root)
    env = sanitized_git_subprocess_env(project_root)
    hooks_dir = git_subprocess_trusted_hooks_dir(scratch_root or project_root)
    try:
        if operation.kind in {"observe_default_ref", "fetch_default_ref"}:
            remote_url = _revalidate_literal_remote_url(operation.remote_url)
            _require_literal_effective_remote_url(
                remote_url,
                git_executable=git,
                cwd=cwd,
                env=env,
                hooks_dir=hooks_dir,
                deadline=deadline,
            )
        if operation.kind == "fetch_default_ref":
            if _git_supports_no_lazy_fetch(
                git, cwd=cwd, env=env, hooks_dir=hooks_dir, deadline=deadline
            ):
                operation = _GitOperation(
                    "fetch_default_ref_no_lazy",
                    remote_url=operation.remote_url,
                    remote_ref=operation.remote_ref,
                    private_ref=operation.private_ref,
                )
            elif _repository_has_promisor_remote(
                git, cwd=cwd, env=env, hooks_dir=hooks_dir, deadline=deadline
            ):
                raise RuntimeError("git_no_lazy_fetch_not_supported_for_promisor_repository")
        return _run_closed_git_process(
            operation,
            git_executable=git,
            cwd=cwd,
            env=env,
            hooks_dir=hooks_dir,
            deadline=deadline,
        )
    finally:
        try:
            shutil.rmtree(hooks_dir)
        except OSError as exc:
            raise GitProtocolProcessGroupCleanupFailed("git_subprocess_hooks_cleanup_failed") from exc


def _require_success(result: subprocess.CompletedProcess, operation: str) -> subprocess.CompletedProcess:
    if result.returncode != 0:
        raise RuntimeError(f"{operation}_failed:{result.returncode}:{(result.stderr or '').strip()}")
    return result


def run_control_plane_git_effective_remote_url(
    expected_remote_url: str,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> LiteralRemoteUrl:
    literal = validate_literal_remote_url(expected_remote_url)
    result = _require_success(
        _execute_semantic_git(
            _GitOperation("effective_remote_url", remote_url=literal),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "effective_remote_url",
    )
    if (result.stdout or "").rstrip("\n") != literal.value:
        raise RuntimeError("effective_remote_url_mismatch")
    return literal


def run_control_plane_git_observe_default_ref(
    remote_url: LiteralRemoteUrl,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> subprocess.CompletedProcess:
    remote_url = _revalidate_literal_remote_url(remote_url)
    return _require_success(
        _execute_semantic_git(
            _GitOperation("observe_default_ref", remote_url=remote_url),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "observe_default_ref",
    )


def run_control_plane_git_repository_object_format(
    *, cwd: str, project_root: str, deadline: GitProtocolDeadline, scratch_root: str | None = None
) -> RepositoryObjectFormat:
    result = _require_success(
        _execute_semantic_git(
            _GitOperation("repository_object_format"),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "repository_object_format",
    )
    return validate_repository_object_format((result.stdout or "").strip())


def run_control_plane_git_fetch_default_ref(
    remote_url: LiteralRemoteUrl,
    remote_ref: AllowedRemoteRef,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> ControlPlanePrivateRef:
    remote_url = _revalidate_literal_remote_url(remote_url)
    remote_ref = _revalidate_allowed_remote_ref(remote_ref)
    private_ref = make_control_plane_private_ref()
    _require_success(
        _execute_semantic_git(
            _GitOperation("fetch_default_ref", remote_url=remote_url, remote_ref=remote_ref, private_ref=private_ref),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "fetch_default_ref",
    )
    return private_ref


def run_control_plane_git_read_private_ref_oid(
    private_ref: ControlPlanePrivateRef,
    object_format: RepositoryObjectFormat,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> RepositoryObjectId:
    private_ref = _revalidate_private_ref(private_ref)
    object_format = _revalidate_object_format(object_format)
    result = _require_success(
        _execute_semantic_git(
            _GitOperation("read_private_ref_oid", private_ref=private_ref),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "read_private_ref_oid",
    )
    return validate_repository_object_id((result.stdout or "").strip(), object_format)


def run_control_plane_git_require_commit_object(
    object_id: RepositoryObjectId,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> None:
    object_id = _revalidate_object_id(object_id)
    _require_success(
        _execute_semantic_git(
            _GitOperation("require_commit_object", object_id=object_id),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "commit_object",
    )


def run_control_plane_git_read_worktree_head(
    path: DetachedWorktreePath,
    object_format: RepositoryObjectFormat,
    *,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> RepositoryObjectId:
    path = _revalidate_worktree_path(path, project_root, require_fresh=False)
    object_format = _revalidate_object_format(object_format)
    result = _require_success(
        _execute_semantic_git(
            _GitOperation("read_worktree_head", worktree_path=path),
            cwd=path.value,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "read_worktree_head",
    )
    return validate_repository_object_id((result.stdout or "").strip(), object_format)


def _rollback_deadline(deadline: GitProtocolDeadline) -> GitProtocolDeadline:
    """Bound compensation after a terminal add has consumed protocol time.

    This is not a new normal-protocol deadline. It is a bounded recovery window
    reserved solely to remove a worktree that this builder just created and to
    prove both its path and its catalog entry are absent.
    """
    reserve = _validate_deadline_value(deadline.cleanup_reserve_seconds, "cleanup_reserve")
    return GitProtocolDeadline(
        deadline_at=max(deadline.deadline_at, time.monotonic() + reserve * 6),
        cleanup_reserve_seconds=reserve,
    )


def _worktree_catalog_contains_path(porcelain: str, path: DetachedWorktreePath) -> bool:
    target = os.path.realpath(path.value)
    return any(
        line.startswith("worktree ") and os.path.realpath(line.removeprefix("worktree ")) == target
        for line in porcelain.splitlines()
    )


def _rollback_detached_locked_worktree(
    path: DetachedWorktreePath,
    *,
    cwd: str,
    project_root: str,
    scratch_root: str | None,
    deadline: GitProtocolDeadline,
) -> None:
    recovery_deadline = _rollback_deadline(deadline)
    if Path(path.value).exists():
        _require_success(
            _execute_semantic_git(
                _GitOperation("remove_detached_locked_worktree", worktree_path=path),
                cwd=cwd,
                project_root=project_root,
                scratch_root=scratch_root,
                deadline=recovery_deadline,
            ),
            "remove_detached_locked_worktree",
        )
    if Path(path.value).exists():
        raise RuntimeError("detached_worktree_rollback_path_present")
    catalog = _require_success(
        _execute_semantic_git(
            _GitOperation("list_worktrees_porcelain"),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=recovery_deadline,
        ),
        "list_worktrees_porcelain",
    )
    if _worktree_catalog_contains_path(catalog.stdout or "", path):
        raise RuntimeError("detached_worktree_rollback_catalog_present")


def run_control_plane_git_add_detached_locked_worktree(
    path: DetachedWorktreePath,
    commit: RepositoryObjectId,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> None:
    path = _revalidate_worktree_path(path, project_root, require_fresh=True)
    commit = _revalidate_object_id(commit)
    run_control_plane_git_require_commit_object(
        commit, cwd=cwd, project_root=project_root, deadline=deadline, scratch_root=scratch_root
    )
    add_attempted = False
    try:
        add_attempted = True
        _require_success(
            _execute_semantic_git(
                _GitOperation("add_detached_locked_worktree", object_id=commit, worktree_path=path),
                cwd=cwd,
                project_root=project_root,
                scratch_root=scratch_root,
                deadline=deadline,
            ),
            "add_detached_locked_worktree",
        )
        object_format = validate_repository_object_format("sha1" if len(commit.value) == 40 else "sha256")
        if (
            run_control_plane_git_read_worktree_head(
                path,
                object_format,
                project_root=project_root,
                deadline=deadline,
                scratch_root=scratch_root,
            )
            != commit
        ):
            raise RuntimeError("detached_worktree_head_mismatch")
    except BaseException:
        # A result-read interruption may happen after `git worktree add`
        # succeeded, so the fresh path is also checked, not only a local flag.
        if add_attempted and Path(path.value).exists():
            _rollback_detached_locked_worktree(
                path,
                cwd=cwd,
                project_root=project_root,
                scratch_root=scratch_root,
                deadline=deadline,
            )
        raise


def run_control_plane_git_delete_private_ref_cas(
    private_ref: ControlPlanePrivateRef,
    expected_oid: RepositoryObjectId,
    *,
    cwd: str,
    project_root: str,
    deadline: GitProtocolDeadline,
    scratch_root: str | None = None,
) -> None:
    private_ref = _revalidate_private_ref(private_ref)
    expected_oid = _revalidate_object_id(expected_oid)
    _require_success(
        _execute_semantic_git(
            _GitOperation("delete_private_ref_cas", private_ref=private_ref, object_id=expected_oid),
            cwd=cwd,
            project_root=project_root,
            scratch_root=scratch_root,
            deadline=deadline,
        ),
        "delete_private_ref_cas",
    )


if __name__ == "__main__":
    raise SystemExit(main())
