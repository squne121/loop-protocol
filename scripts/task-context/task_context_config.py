"""Task Context v1 — repo_instance_key and state-root resolution.

See ``docs/dev/task-context.md`` (## Repository Instance Identity,
## LOOP_TASK_CONTEXT_STATE_ROOT Resolution) for the authoritative rationale.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess

STATE_ROOT_ENV_VAR = "LOOP_TASK_CONTEXT_STATE_ROOT"
XDG_STATE_HOME_ENV_VAR = "XDG_STATE_HOME"
DB_FILE_NAME = "task-context.sqlite3"


def repo_instance_key(cwd: str | pathlib.Path | None = None) -> str:
    """SHA-256 hex digest of the canonicalized (realpath) git common-dir.

    ``git rev-parse --path-format=absolute --git-common-dir`` resolves to the
    *same* physical directory for the main worktree and any linked worktree
    of the same repository (they share one ``.git`` common dir), and to a
    *different* directory for a separate ``git clone`` (AC13). We realpath
    the result before hashing so that symlinked repo checkouts / worktrees
    still resolve to one canonical key.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=True,
    )
    common_dir = result.stdout.strip()
    resolved = str(pathlib.Path(common_dir).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()


def _default_xdg_state_home() -> pathlib.Path:
    """Resolve ``$XDG_STATE_HOME`` per the freedesktop XDG Base Directory
    Specification.

    The spec requires all XDG_* path variables to be absolute; a relative
    (or empty) value must be treated as unset/invalid rather than silently
    resolved against the current working directory. When unset (or
    invalid), the spec-mandated default is ``$HOME/.local/state``.
    """
    raw = os.environ.get(XDG_STATE_HOME_ENV_VAR, "")
    if raw:
        candidate = pathlib.Path(raw)
        if candidate.is_absolute():
            return candidate
        # Relative $XDG_STATE_HOME is invalid per spec -- fall through to default.
    return pathlib.Path.home() / ".local" / "state"


def resolve_state_root(cwd: str | pathlib.Path | None = None) -> pathlib.Path:
    """Resolve the fully-resolved absolute Task Context registry instance
    directory.

    - If ``LOOP_TASK_CONTEXT_STATE_ROOT`` is explicitly set (non-empty), it
      MUST be an absolute path. A relative override is rejected (never
      silently resolved against ``cwd``) -- see AC/contract for
      ``LOOP_TASK_CONTEXT_STATE_ROOT``.
    - Otherwise: ``$XDG_STATE_HOME/loop-protocol/task-context/v1/<repo_instance_key>/``.
    """
    override = os.environ.get(STATE_ROOT_ENV_VAR, "")
    if override:
        candidate = pathlib.Path(override)
        if not candidate.is_absolute():
            raise ValueError(
                f"{STATE_ROOT_ENV_VAR} must be an absolute path; got relative path "
                f"{override!r}. Relative overrides are rejected instead of being "
                "silently resolved against the current working directory."
            )
        return candidate
    key = repo_instance_key(cwd=cwd)
    return _default_xdg_state_home() / "loop-protocol" / "task-context" / "v1" / key


def db_path(cwd: str | pathlib.Path | None = None) -> pathlib.Path:
    """Canonical DB file path: ``<resolved-root>/task-context.sqlite3``."""
    return resolve_state_root(cwd=cwd) / DB_FILE_NAME
