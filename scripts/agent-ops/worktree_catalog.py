#!/usr/bin/env python3
"""worktree_catalog.py — shared git worktree catalog + monotonic deadline (Issue #1137).

Single source of truth for parsing ``git worktree list --porcelain -z`` so that
``worktree_scope_guard``, ``guard_preflight``, and ``cleanup_exec`` all resolve
worktrees identically (OWNER review Blocker 3 / Medium "porcelain -z 統一"). The
``-z`` form is used everywhere to avoid newline/quoting ambiguity in paths.

Also provides ``Deadline``, a shared monotonic budget so every guard subprocess
runs under one wall-clock ceiling smaller than the outer hook timeout (OWNER
review High "timeout"). The module is import-safe and has no side effects.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

SCHEMA_ENTRY = "WORKTREE_CATALOG_ENTRY_V1"

# Reason code returned when the shared deadline is exhausted.
GUARD_DEADLINE_EXCEEDED = "guard_deadline_exceeded"


class GuardDeadlineExceeded(Exception):
    """Raised when a subprocess cannot run within the remaining shared budget."""


class Deadline:
    """A monotonic wall-clock budget shared across guard subprocesses.

    ``budget_seconds`` is the total time all checks may consume. ``remaining()``
    returns the seconds left (never negative). ``subprocess_timeout(maximum)``
    returns a per-call timeout clamped to the remaining budget so the sum of
    inner timeouts can never exceed the outer hook timeout.
    """

    def __init__(self, budget_seconds: float) -> None:
        self._budget = float(budget_seconds)
        self._start = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def remaining(self) -> float:
        return max(0.0, self._budget - self.elapsed())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def subprocess_timeout(self, maximum: float) -> float:
        """Per-subprocess timeout: min(maximum, remaining). Raises if no budget."""
        rem = self.remaining()
        if rem <= 0.0:
            raise GuardDeadlineExceeded(GUARD_DEADLINE_EXCEEDED)
        return min(float(maximum), rem)


def parse_worktree_porcelain_z(data: str) -> list[dict]:
    """Parse ``git worktree list --porcelain -z`` (NUL-separated attribute lines).

    Returns a list of ``WORKTREE_CATALOG_ENTRY_V1`` dicts with keys
    ``worktree_realpath`` / ``branch_ref`` / ``git_common_dir`` / ``detached``.
    A record starts at a ``worktree <path>`` field and runs until the next one.
    """
    entries: list[dict] = []
    current: dict | None = None

    def _flush() -> None:
        if current is not None:
            entries.append(current)

    for field in data.split("\0"):
        if field == "":
            continue
        if field.startswith("worktree "):
            _flush()
            raw = field[len("worktree "):]
            current = {
                "schema": SCHEMA_ENTRY,
                "worktree_realpath": os.path.realpath(raw),
                "branch_ref": None,
                "git_common_dir": None,
                "detached": False,
            }
        elif current is None:
            continue
        elif field.startswith("branch "):
            ref = field[len("branch "):]
            current["branch_ref"] = ref
        elif field == "detached":
            current["detached"] = True
        elif field.startswith("HEAD "):
            current["head"] = field[len("HEAD "):]
    _flush()
    return entries


def list_worktrees(project_root: str, deadline: Deadline | None = None) -> list[dict] | None:
    """Return the worktree catalog for ``project_root``, or None on git failure.

    Uses ``git worktree list --porcelain -z``. When a ``Deadline`` is supplied the
    subprocess timeout is clamped to the remaining budget.
    """
    git = shutil.which("git")
    if not git:
        return None
    timeout = deadline.subprocess_timeout(10.0) if deadline is not None else 10.0
    try:
        out = subprocess.run(
            [git, "-C", project_root, "worktree", "list", "--porcelain", "-z"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    # git_common_dir is shared across linked worktrees; resolve once.
    common_dir = _git_common_dir(project_root, git, deadline)
    entries = parse_worktree_porcelain_z(out.stdout)
    for e in entries:
        e["git_common_dir"] = common_dir
    return entries


def _git_common_dir(project_root: str, git: str, deadline: Deadline | None) -> str | None:
    timeout = deadline.subprocess_timeout(5.0) if deadline is not None else 5.0
    try:
        out = subprocess.run(
            [git, "-C", project_root, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    val = out.stdout.strip()
    if not val:
        return None
    return os.path.realpath(os.path.join(project_root, val))


def find_by_realpath(catalog: list[dict], target_realpath: str) -> dict | None:
    """Return the catalog entry whose worktree_realpath equals target, else None."""
    target = os.path.realpath(target_realpath)
    for e in catalog:
        if e.get("worktree_realpath") == target:
            return e
    return None


def branch_short_name(branch_ref: str | None) -> str | None:
    """Strip refs/heads/ from a branch ref. Returns None for detached/None."""
    if not branch_ref:
        return None
    if branch_ref.startswith("refs/heads/"):
        return branch_ref[len("refs/heads/"):]
    return branch_ref
