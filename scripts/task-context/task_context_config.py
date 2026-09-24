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
import warnings

STATE_ROOT_ENV_VAR = "LOOP_TASK_CONTEXT_STATE_ROOT"
XDG_STATE_HOME_ENV_VAR = "XDG_STATE_HOME"
DB_FILE_NAME = "task-context.sqlite3"

# --- Issue #2568 In Scope: runtime-smoke scope carrier ----------------------
#
# ``worktree-agent-runtime-smoke`` (and any other caller wanting to run
# ``task-contextctl smoke seed`` against a run-scoped isolated state root)
# sets this env var to ``RUNTIME_SMOKE_SCOPE_VALUE`` explicitly, alongside
# ``LOOP_TASK_CONTEXT_STATE_ROOT`` pointing at the isolated root. Unset (or
# any other value) means "not a runtime-smoke invocation" -- `task_contextctl
# _dispatch()` rejects `smoke_seed` in that case, strictly before opening/
# migrating the DB (AC6). This module never infers scope from anything else
# (state root path shape, cwd, ...).
SCOPE_ENV_VAR = "LOOP_TASK_CONTEXT_SCOPE"
RUNTIME_SMOKE_SCOPE_VALUE = "runtime_smoke"

# --- Issue #2569 PR #2731 review fix_delta P1-1: cold-restart startup
# orchestrator scope gate ------------------------------------------------
#
# Herdr's own plugin docs (https://herdr.dev, "Plugins" -> "Install and
# link") state plainly: "Installed and linked plugins, including their
# enabled state, are global to the current user and available in EVERY
# Herdr session" -- there is no Herdr-level mechanism to scope a linked
# plugin's `[[startup]]` hook to only the dedicated LOOP_PROTOCOL
# project-scoped named session. Left unguarded, the SAME startup-hook
# command would therefore also fire on every restart of the human/default
# Herdr session (and any other named session on the machine), which is
# exactly the interference the Issue's Real Herdr canary must rule out.
#
# `task_context_cold_restart_startup.py`'s `main()` requires this env var
# to be exactly ``COLD_RESTART_SCOPE_VALUE`` before it performs ANY herdr
# subprocess call or DB read -- otherwise it is an immediate, side-effect-
# free no-op. Only the dedicated named session's OWN launch wrapper sets
# this (alongside `HERDR_CONFIG_PATH` pointing at that session's
# `resume_agents_on_restore = false` config) -- ordinary OS process
# environment inheritance (Herdr's plugin startup-hook child process
# inherits the launching shell's/server's environment) carries it down to
# the plugin invocation without requiring any Herdr-specific plugin API
# support for per-session env injection.
COLD_RESTART_SCOPE_ENV_VAR = "LOOP_TASK_CONTEXT_COLD_RESTART_SCOPE"
COLD_RESTART_SCOPE_VALUE = "cold_restart_dedicated_session"


def is_cold_restart_dedicated_session() -> bool:
    """``True`` only when ``LOOP_TASK_CONTEXT_COLD_RESTART_SCOPE`` is
    exactly ``COLD_RESTART_SCOPE_VALUE`` -- the gate predicate
    ``task_context_cold_restart_startup.main()`` checks before doing
    anything else (fix_delta P1-1 "Herdr plugins are user-global" safety
    guard)."""
    return os.environ.get(COLD_RESTART_SCOPE_ENV_VAR, "") == COLD_RESTART_SCOPE_VALUE


def resolve_task_context_scope() -> str:
    """Read the raw ``LOOP_TASK_CONTEXT_SCOPE`` carrier value.

    Returns the empty string when unset -- never raises, never guesses from
    any other env var or from ``LOOP_TASK_CONTEXT_STATE_ROOT``."""
    return os.environ.get(SCOPE_ENV_VAR, "")


def is_runtime_smoke_scope() -> bool:
    """``True`` only when ``LOOP_TASK_CONTEXT_SCOPE`` is exactly
    ``RUNTIME_SMOKE_SCOPE_VALUE`` (AC6 gate predicate)."""
    return resolve_task_context_scope() == RUNTIME_SMOKE_SCOPE_VALUE

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

# --- Issue #2569 In Scope: Native profile migration contract ---------------
#
# ``native_claude_v1`` is the explicit effective-profile value for a
# Native-operator managed ExecutionRun (AC13). ``invalid_managed_profile`` is
# the sentinel returned by ``resolve_effective_runtime_profile()`` below for
# any managed ``(run_kind, runtime_profile, resume_profile)`` triple that
# does not match one of the fixed contract combinations (AC14) -- callers
# (the resume dispatcher) must treat it as ``RESTORE_BLOCKED``, never as a
# reason to fall back to plain Native.
NATIVE_CLAUDE_RUNTIME_PROFILE = "native_claude_v1"
INVALID_MANAGED_PROFILE = "invalid_managed_profile"


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
    - unset -> ``("native_operator", None, None)`` (intentional native
      default, unchanged).
    - any OTHER non-empty value (typo'd/future variant) -> also degrades to
      ``("native_operator", None, None)`` for now (the persisted ExecutionRun
      ``run_kind`` contract is intentionally left unchanged here -- see
      PR #2696 review fix_delta P2 note below), but this is no longer
      completely silent: it emits a ``RuntimeWarning`` (never raises, never
      changes the returned triple) so a misconfigured
      ``LOOP_TASK_CONTEXT_RUNTIME_VARIANT`` is observable instead of being
      indistinguishable from an intentionally-unset one.

      PR #2696 review fix_delta (P2, non-blocking, OWNER REQUEST_CHANGES):
      giving this case its OWN distinct ``run_kind`` (rather than degrading
      to ``native_operator``) would require changing
      ``task_context_schema.py``'s ``run_kind`` ``CHECK`` constraint and the
      ``VALID_RUN_KINDS``/``MANAGED_RUN_KINDS`` sets in
      ``task_context_service.py``, plus auditing every existing query that
      hardcodes ``run_kind IN ('native_operator', 'claude_gpt')`` -- a larger
      schema-migration-shaped change out of this fix_delta's narrow scope.
      Deferred as an optional follow-up; this warning is the narrow,
      non-blocking diagnostic improvement that fits within scope today."""
    raw = resolve_operator_runtime_variant()
    if raw == CLAUDE_GPT_RUNTIME_VARIANT:
        return CLAUDE_GPT_RUN_KIND, CLAUDE_GPT_RUNTIME_PROFILE, CLAUDE_GPT_RUNTIME_PROFILE
    if raw:
        warnings.warn(
            f"{RUNTIME_VARIANT_ENV_VAR}={raw!r} is not a recognized runtime "
            f"variant (expected unset or {CLAUDE_GPT_RUNTIME_VARIANT!r}) -- "
            f"degrading to {NATIVE_OPERATOR_RUN_KIND!r} run_kind/profiles "
            "rather than treating it as intentionally native.",
            RuntimeWarning,
            stacklevel=2,
        )
    return NATIVE_OPERATOR_RUN_KIND, None, None


def normalize_operator_profiles_for_new_run(
    run_kind: str, runtime_profile: str | None, resume_profile: str | None
) -> tuple[str, str | None, str | None]:
    """Issue #2569 AC13 ("本Issue以降に作成される新規Native ExecutionRunは
    ``runtime_profile``/``resume_profile``へ明示的に``native_claude_v1``を
    保存する -- NULL新規書き込みを終了する"): given the raw triple
    ``operator_run_kind_and_profiles()`` returns, upgrade the intentional
    ``(native_operator, None, None)`` legacy-shaped default to the explicit
    ``(native_operator, native_claude_v1, native_claude_v1)`` triple that
    every NEW managed ExecutionRun row must be started with from this Issue
    onward.

    Deliberately a separate function from ``operator_run_kind_and_profiles``
    (rather than changing that function's own return value) -- the raw
    triple's ``(native_operator, None, None)`` shape is still relied on
    elsewhere (its own "intentionally unset vs recognized variant" contract
    and existing tests), and this Issue's requirement is narrowly about what
    gets *persisted* on a newly-started ExecutionRun row, not about
    redefining what "unset ``LOOP_TASK_CONTEXT_RUNTIME_VARIANT``" means.
    Only the two callers that actually call ``start_execution_run`` for a
    NEW row (``task_context_hook_flows.on_session_start`` and
    ``task_context_service._attach_or_start_binding_run_tx``'s degrade path)
    apply this normalization. Claude-GPT triples (already explicit) and any
    other value pass through unchanged."""
    if run_kind == NATIVE_OPERATOR_RUN_KIND and runtime_profile is None and resume_profile is None:
        return NATIVE_OPERATOR_RUN_KIND, NATIVE_CLAUDE_RUNTIME_PROFILE, NATIVE_CLAUDE_RUNTIME_PROFILE
    return run_kind, runtime_profile, resume_profile


def resolve_effective_runtime_profile(
    run_kind: str, runtime_profile: str | None, resume_profile: str | None
) -> str:
    """Issue #2569 "Native profile migration contract" -- resolve the
    *effective* runtime profile for an already-persisted managed
    ExecutionRun's ``(run_kind, runtime_profile, resume_profile)`` triple
    (AC13/AC14). Read-time compatibility only -- never mutates the DB, never
    backfills.

    - ``(native_operator, NULL, NULL)`` -> ``native_claude_v1`` (legacy
      pre-#2569 row, current mainline's intentional
      ``operator_run_kind_and_profiles()`` default -- see that function's
      docstring).
    - ``(native_operator, native_claude_v1, native_claude_v1)`` ->
      ``native_claude_v1`` (new-style explicit row, #2569 onward).
    - ``(claude_gpt, claude_gpt_v1, claude_gpt_v1)`` -> ``claude_gpt_v1``.
    - any other combination of a managed run_kind (``native_operator`` /
      ``claude_gpt``) -> ``invalid_managed_profile`` (AC14 -- the resume
      dispatcher must RESTORE_BLOCKED, never fall back to plain Native)."""
    if run_kind == NATIVE_OPERATOR_RUN_KIND and runtime_profile is None and resume_profile is None:
        return NATIVE_CLAUDE_RUNTIME_PROFILE
    if (
        run_kind == NATIVE_OPERATOR_RUN_KIND
        and runtime_profile == NATIVE_CLAUDE_RUNTIME_PROFILE
        and resume_profile == NATIVE_CLAUDE_RUNTIME_PROFILE
    ):
        return NATIVE_CLAUDE_RUNTIME_PROFILE
    if (
        run_kind == CLAUDE_GPT_RUN_KIND
        and runtime_profile == CLAUDE_GPT_RUNTIME_PROFILE
        and resume_profile == CLAUDE_GPT_RUNTIME_PROFILE
    ):
        return CLAUDE_GPT_RUNTIME_PROFILE
    return INVALID_MANAGED_PROFILE


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

    Issue #2568 PR #2708 REQUEST_CHANGES fix_delta item 1 (atomic carrier
    integrity): when the caller has opted into ``LOOP_TASK_CONTEXT_SCOPE=
    runtime_smoke`` (``is_runtime_smoke_scope()``), the canonical XDG
    fallback above must NEVER be reached -- a runtime-smoke caller that
    omitted (or emptied) ``LOOP_TASK_CONTEXT_STATE_ROOT`` raises here,
    strictly before any canonical path is computed/returned, instead of
    silently resolving to (and later materializing/migrating) the
    canonical DB. When scope is NOT runtime_smoke this function's
    behavior is byte-identical to before this fix_delta.
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
    if is_runtime_smoke_scope():
        raise ValueError(
            f"{SCOPE_ENV_VAR}={RUNTIME_SMOKE_SCOPE_VALUE!r} requires an explicit, "
            f"non-empty, absolute {STATE_ROOT_ENV_VAR} -- refusing to fall back to "
            "the canonical state root/DB for a runtime-smoke-scoped caller."
        )
    key = repo_instance_key(cwd=cwd)
    return _default_xdg_state_home() / "loop-protocol" / "task-context" / "v1" / key


def db_path(cwd: str | pathlib.Path | None = None) -> pathlib.Path:
    """Canonical DB file path: ``<resolved-root>/task-context.sqlite3``."""
    return resolve_state_root(cwd=cwd) / DB_FILE_NAME
