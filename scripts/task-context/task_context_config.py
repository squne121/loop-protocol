"""Task Context v1 — repo_instance_key and state-root resolution.

See ``docs/dev/task-context.md`` (## Repository Instance Identity,
## LOOP_TASK_CONTEXT_STATE_ROOT Resolution) for the authoritative rationale.

Issue #2567 (Claude-GPT canonical Task Context DB / operator runtime
profile integration) added the ``LOOP_TASK_CONTEXT_RUNTIME_VARIANT`` carrier
and the ``operator_run_kind_and_profiles()`` helper below. This module stays
the single SSOT for both "where is the canonical state root" (pre-existing)
and "which ExecutionRun run_kind/runtime_profile/resume_profile does the
current operator process use" (new) -- callers (``task_context_hook_flows``,
``task_context_service``, and the Claude-GPT launcher's own
``scripts/claude-gpt/lib.sh``) read this module rather than re-deriving
either decision themselves.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess

STATE_ROOT_ENV_VAR = "LOOP_TASK_CONTEXT_STATE_ROOT"
XDG_STATE_HOME_ENV_VAR = "XDG_STATE_HOME"
DB_FILE_NAME = "task-context.sqlite3"

# --- Issue #2567 In Scope: runtime variant carrier -------------------------
#
# The Claude-GPT launcher (``scripts/claude-gpt/launch.sh``) exports this
# env var (fixed value ``"claude_gpt"``) on every normal-mode launch, before
# it swaps HOME/XDG_CONFIG_HOME/XDG_CACHE_HOME to the isolated Claude-GPT
# profile. Native Claude never sets it, so its absence (or any value other
# than ``"claude_gpt"``) means "native_operator" -- this module never infers
# the variant from anything else (HOME, CLAUDE_CONFIG_DIR, proxy env, ...).
RUNTIME_VARIANT_ENV_VAR = "LOOP_TASK_CONTEXT_RUNTIME_VARIANT"
CLAUDE_GPT_RUNTIME_VARIANT = "claude_gpt"
NATIVE_OPERATOR_RUN_KIND = "native_operator"
CLAUDE_GPT_RUN_KIND = "claude_gpt"
CLAUDE_GPT_RUNTIME_PROFILE = "claude_gpt_v1"


def resolve_operator_runtime_variant() -> str:
    """Read the raw ``LOOP_TASK_CONTEXT_RUNTIME_VARIANT`` carrier value.

    Returns the empty string when unset (the native_operator default) --
    never raises, never guesses from any other env var."""
    return os.environ.get(RUNTIME_VARIANT_ENV_VAR, "")


def operator_run_kind_and_profiles() -> tuple[str, str | None, str | None]:
    """Resolve the ``(run_kind, runtime_profile, resume_profile)`` triple a
    managed operator ExecutionRun (SessionStart new-binding / restored-
    binding, and the ``_attach_or_start_binding_run_tx`` degrade path) should
    be started/recorded with, based on the current process's
    ``LOOP_TASK_CONTEXT_RUNTIME_VARIANT`` (AC4).

    - ``"claude_gpt"`` -> ``("claude_gpt", "claude_gpt_v1", "claude_gpt_v1")``
    - anything else (unset, or any other value) -> ``("native_operator",
      None, None)`` -- the pre-existing native behavior, unchanged."""
    if resolve_operator_runtime_variant() == CLAUDE_GPT_RUNTIME_VARIANT:
        return CLAUDE_GPT_RUN_KIND, CLAUDE_GPT_RUNTIME_PROFILE, CLAUDE_GPT_RUNTIME_PROFILE
    return NATIVE_OPERATOR_RUN_KIND, None, None


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
