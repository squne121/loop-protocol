#!/usr/bin/env python3
"""
local_main_branch_guard.py

Shared core logic for the local root checkout branch drift guard.
Used by both Claude Code (.claude/hooks/local_main_branch_guard.sh)
and Codex CLI (.codex/hooks/local_main_branch_guard.sh) wrappers.

Guard judgment: When running in local root context (cwd is under primary worktree
and NOT inside a linked issue worktree), block any Bash command that would change
the local root checkout away from the default branch.

Exit codes (hook mode):
  0 — allow
  2 — block (fail-closed)

Output schema (LOCAL_MAIN_BRANCH_GUARD_RESULT_V1, self-test / CLI mode):
  status: allow | block
  reason_code: see REASON_CODES below
  current_branch: str | null
  target_branch: str | null
  target_branch_kind: default | issue | worktree_issue | pr | unknown | null
  hook_flavor: claude | codex | cli
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, Sequence

_AGENT_GUARDS_DIR = Path(__file__).resolve().parent
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))

from skill_runtime_command_policy import (  # noqa: E402
    SKILL_RUNTIME_REASON_CODE,
    is_exact_skill_runtime_anchor_executor_command,
    is_exact_skill_runtime_executor_command,
    looks_like_skill_runtime_executor_command,
)
from git_mutation_command_policy import (  # noqa: E402
    classify_rtk_git_mutation,
    COMMAND_CLASS_RTK_GIT_UNKNOWN,
)
from hook_repair_hints import build_hook_command_repair_hint  # noqa: E402

# Issue #1241 marker: HOOK_COMMAND_REPAIR_HINT_V1 is rendered via hook_repair_hints.py.


# ─── Reason codes ────────────────────────────────────────────────────────────

REASON_NOT_LOCAL_ROOT = "not_local_root_context"
REASON_LINKED_ISSUE_WORKTREE_CONTEXT = "linked_issue_worktree_context"
REASON_READONLY = "readonly_command"
REASON_BRANCH_SAFE_MAINTENANCE = "branch_safe_maintenance_command"
REASON_RECOVERY = "recovery_to_default_branch"
REASON_DRIFT = "local_root_branch_drift"
REASON_ALREADY_DRIFTED = "already_drifted_root"
REASON_DETACHED_OR_UNKNOWN = "detached_or_unknown_root"
REASON_UNPARSEABLE = "unparseable_branch_mutation"
REASON_UNKNOWN_COMMAND = "unknown_command_requires_review"
REASON_UNKNOWN_ALLOWED = "unknown_non_branch_command_allowed"
REASON_GH_API = "github_api_command"
REASON_RTK_HELP_COMMAND = "rtk_help_command"
REASON_RTK_PROXY = "rtk_proxy_requires_review"
REASON_INLINE_OVERRIDE = "inline_env_override_not_allowed"
REASON_DETERMINISTIC_CHECKER = "deterministic_checker_command"
REASON_CONTROLLED_SKILL_MUTATION_EXECUTOR = "controlled_skill_mutation_executor"
REASON_GITHUB_REMOTE_OPS = "github_remote_ops_command"
REASON_GH_MUTATION = "gh_mutation_denied"
REASON_WORKTREE_BOOTSTRAP_EXECUTOR = "worktree_bootstrap_executor_command"
REASON_SKILL_RUNTIME_EXECUTOR = SKILL_RUNTIME_REASON_CODE
# Issue #1137: exact cleanup commands (git worktree remove / git branch -d) are
# arbitrated by worktree_scope_guard against the V3 one-shot contract. The local
# root branch guard explicitly DEFERS authority to that guard so the two guards
# do not double-decide and the arbitration order is unambiguous.
REASON_CLEANUP_DEFERRED = "cleanup_deferred_to_worktree_scope_guard"

# ─── Shared controlled skill mutation policy (Issue #1166) ───────────────────
# Import from scripts/agent-guards (sibling module) so the SAME
# is_controlled_skill_mutation_exec_command function is consumed by both
# local_main_branch_guard and worktree_scope_guard (AC4/AC17 — no split-brain).
_AGENT_GUARDS_DIR = str(Path(__file__).resolve().parent)
if _AGENT_GUARDS_DIR not in sys.path:
    sys.path.insert(0, _AGENT_GUARDS_DIR)
try:
    from controlled_skill_mutation_policy import (
        is_controlled_skill_mutation_exec_command as _csm_exec_command,
    )

    _CSM_POLICY_AVAILABLE = True
except Exception:  # pragma: no cover - defensive fail-closed

    def _csm_exec_command(cmd: str, project_root: str) -> bool:  # type: ignore[misc]
        return False

    _CSM_POLICY_AVAILABLE = False

# ─── Shared PreToolUse fast-path classifier (Issue #1289) ────────────────────
# Shared library, NOT an independent PreToolUse hook. Used only to enrich the
# telemetry payload with a bounded fast-path classification; never changes the
# allow/block decision or reason_code of this guard (AC1/AC2/AC6).
#
# Import note: pretool_fastpath_classifier.py imports is_readonly_command /
# _parse_gh_api_command back from THIS module (they are defined further below
# in this file). If this module is imported first (e.g. `import
# local_main_branch_guard` from a caller, before anything imports the
# classifier), a top-level `import pretool_fastpath_classifier` here would
# create a circular import that reaches back into a not-yet-fully-initialized
# local_main_branch_guard module and fails to resolve those two names,
# permanently latching _FASTPATH_AVAILABLE = False for the whole process. To
# avoid this ordering hazard, the classifier is imported lazily on first use
# inside _compute_fastpath() instead of at module load time.
_fastpath: Any = None
_FASTPATH_AVAILABLE: bool | None = None  # None = not yet attempted


def _ensure_fastpath_imported() -> bool:
    """Lazily import pretool_fastpath_classifier on first use. Safe to call
    regardless of import order (Issue #1289 fix — see note above)."""
    global _fastpath, _FASTPATH_AVAILABLE
    if _FASTPATH_AVAILABLE is not None:
        return _FASTPATH_AVAILABLE
    try:
        import pretool_fastpath_classifier as _fastpath_module

        _fastpath = _fastpath_module
        _FASTPATH_AVAILABLE = True
    except Exception:  # pragma: no cover - defensive fail-closed
        _fastpath = None
        _FASTPATH_AVAILABLE = False
    return _FASTPATH_AVAILABLE

# ─── Worktree bootstrap executor policy (Issue #1209) ────────────────────────
try:
    from worktree_bootstrap_command_policy import (
        is_exact_worktree_bootstrap_executor_command as _wbe_exec_command,
        looks_like_worktree_bootstrap_executor_command as _wbe_looks_like,
    )
    _WBE_POLICY_AVAILABLE = True
except Exception:  # pragma: no cover - defensive fail-closed
    def _wbe_exec_command(  # type: ignore[misc]
        cmd: str, cwd: str, project_root: str, deadline: object | None = None
    ) -> bool:
        return False

    def _wbe_looks_like(cmd: str) -> bool:  # type: ignore[misc]
        return False
    _WBE_POLICY_AVAILABLE = False

# ─── GitHub remote ops classification vocabulary ──────────────────────────────
# 5-class vocabulary for gh command classification (Issue #1124).
# These constants are exported for test validation (AC15).
GITHUB_CMD_CLASS_DISPLAY_READONLY = "display_readonly_command"
GITHUB_CMD_CLASS_READONLY_EXPORT = "readonly_artifact_export_command"
GITHUB_CMD_CLASS_ISSUE_MUTATION = "github_issue_mutation_command"
GITHUB_CMD_CLASS_PR_METADATA = "github_pr_metadata_command"
GITHUB_CMD_CLASS_DESTRUCTIVE = "github_destructive_command"

# Trusted repository slug for gh issue create mutation allowlist.
TRUSTED_REPO_SLUG = "squne121/loop-protocol"

COMMAND_KIND_UNKNOWN = "unknown_command"
COMMAND_KIND_GITHUB_API = "github_api_command"
COMMAND_KIND_RTK_WRAPPER = "rtk_wrapper"
COMMAND_KIND_GIT_BRANCH_MUTATION = "git_branch_mutation"
COMMAND_KIND_PIPELINE = "readonly_pipeline"
COMMAND_KIND_GITHUB_DISPLAY = "github_display_readonly"
COMMAND_KIND_GITHUB_ARTIFACT_EXPORT = "readonly_artifact_export"
COMMAND_KIND_GITHUB_ISSUE_MUTATION = "github_issue_mutation"
COMMAND_KIND_GITHUB_PR_METADATA = "github_pr_metadata"
COMMAND_KIND_GITHUB_DESTRUCTIVE = "github_mutation"
COMMAND_KIND_GITHUB_MUTATION = COMMAND_KIND_GITHUB_DESTRUCTIVE
COMMAND_KIND_READONLY = "readonly_command"


# ─── Root state classification ────────────────────────────────────────────────


def classify_root_state(current_branch: str | None, default_branch: str) -> str:
    """
    Classify local root state into one of three kinds:
      default           — on default branch (normal state)
      drifted           — on a non-default named branch
      detached_or_unknown — detached HEAD (current_branch is None) or unknown state

    B1 fix: detached HEAD must NOT be treated as default/allow state.
    """
    if current_branch is None:
        return "detached_or_unknown"
    if current_branch == default_branch:
        return "default"
    return "drifted"


# ─── Branch name classification ───────────────────────────────────────────────


def classify_branch(branch: str, default_branch: str) -> str:
    """Classify a branch name into a kind."""
    if branch == default_branch:
        return "default"
    if re.match(r"^issue-\d+", branch):
        return "issue"
    if re.match(r"^worktree-issue-\d+", branch):
        return "worktree_issue"
    # PR checkout patterns: pr/N, pull/N, or branch with pr- prefix
    if re.match(r"^(pr/|pull/|pr-)\d+", branch):
        return "pr"
    return "unknown"


# ─── Git helpers ─────────────────────────────────────────────────────────────


def run_git(*args: str, cwd: str | None = None) -> tuple[int, str]:
    """Run a git command and return (exit_code, stdout)."""
    try:
        result = subprocess.run(
            ["git"] + list(args),
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
        )
        return result.returncode, result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 1, ""


def get_primary_worktree_path(cwd: str | None = None) -> str | None:
    """
    Get primary worktree path using `git worktree list --porcelain -z`.
    Does NOT rely on `git rev-parse --show-toplevel` alone.
    Returns the path of the primary (main) worktree.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain", "-z"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        # Parse NUL-separated blocks
        blocks = result.stdout.split("\x00")
        # Each block is lines separated by newlines; first block is primary worktree
        for block in blocks:
            lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
            if not lines:
                continue
            # Primary worktree has "bare" absent and no "worktree" prefix
            # Format: "worktree <path>\nHEAD <sha>\nbranch <ref>"
            # First block = primary worktree (no "linked" indicator in old git,
            # but in newer git the primary is always the first entry)
            is_bare = any(line == "bare" for line in lines)
            if is_bare:
                continue  # bare repo, skip
            path_lines = [line for line in lines if line.startswith("worktree ")]
            if path_lines:
                return path_lines[0][len("worktree ") :]
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def get_cwd_git_toplevel(cwd: str) -> str | None:
    """
    Get the git toplevel for the given cwd.
    Used to determine which worktree cwd belongs to.
    """
    rc, out = run_git("rev-parse", "--show-toplevel", cwd=cwd)
    if rc == 0 and out:
        return out
    return None


def get_current_branch(cwd: str | None = None) -> str | None:
    """Get current branch name. Returns None if detached HEAD."""
    rc, out = run_git("branch", "--show-current", cwd=cwd)
    if rc == 0 and out:
        return out
    return None


def resolve_default_branch(cwd: str | None = None) -> str:
    """
    Resolve default branch with priority:
    1. LOOP_DEFAULT_BRANCH env var
    2. git symbolic-ref --short refs/remotes/origin/HEAD
    3. main, master, trunk (first that exists)
    """
    env_override = os.environ.get("LOOP_DEFAULT_BRANCH", "").strip()
    if env_override:
        return env_override

    rc, out = run_git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=cwd)
    if rc == 0 and out:
        # Strip "origin/" prefix if present
        if "/" in out:
            return out.split("/", 1)[1]
        return out

    # Try common default branch names
    for candidate in ("main", "master", "trunk"):
        rc, _ = run_git("rev-parse", "--verify", candidate, cwd=cwd)
        if rc == 0:
            return candidate

    return "main"  # ultimate fallback


# ─── Context detection ────────────────────────────────────────────────────────


def is_local_root_context(cwd: str) -> bool:
    """
    Return True if cwd is under the primary worktree root (NOT inside a linked worktree).

    B1 fix: Compare cwd's git toplevel against the primary worktree catalog path.
    This ensures subdirectories of the primary worktree (scripts/, docs/, etc.)
    are also correctly detected as local root context.

    Uses git worktree list --porcelain -z catalog to identify primary root.
    Does NOT rely on git rev-parse --show-toplevel alone.
    """
    # Resolve project root with priority:
    # 1. CLAUDE_PROJECT_DIR env
    # 2. git worktree list primary path
    project_root = _resolve_project_root(cwd)
    if project_root is None:
        return False

    real_root = os.path.realpath(project_root)

    # If CLAUDE_PROJECT_DIR points at a linked issue worktree, classify as
    # non-local-root context so issue-bound worktree mutation is allowed under
    # existing worktree isolation policies.
    primary = get_primary_worktree_path(cwd=cwd)
    if primary is not None:
        real_primary = os.path.realpath(primary)
        if real_root != real_primary:
            return False

    # B1: Use git toplevel of cwd to handle subdirectories correctly.
    # If CLAUDE_PROJECT_DIR is set, also accept cwd being under that root.
    cwd_toplevel = get_cwd_git_toplevel(cwd)
    if cwd_toplevel is None:
        # Can't determine git toplevel; fall back to checking if cwd starts with root
        real_cwd = os.path.realpath(cwd)
        if real_cwd != real_root and not real_cwd.startswith(real_root + os.sep):
            return False
    else:
        real_cwd_toplevel = os.path.realpath(cwd_toplevel)
        if real_cwd_toplevel != real_root:
            # cwd's toplevel is a different worktree (linked worktree)
            return False

    # Check cwd is NOT under .claude/worktrees/
    # (linked worktrees inside the project root should not trigger this guard)
    real_cwd = os.path.realpath(cwd)
    worktrees_subdir = os.path.join(real_root, ".claude", "worktrees")
    if real_cwd.startswith(worktrees_subdir + os.sep) or real_cwd == worktrees_subdir:
        return False

    return True


def is_linked_issue_worktree_context(cwd: str) -> bool:
    """Return True iff cwd is inside a linked worktree rather than the primary root."""
    project_root = _resolve_project_root(cwd)
    if project_root is None:
        return False
    primary = get_primary_worktree_path(cwd=cwd)
    if primary is None:
        return False
    real_root = os.path.realpath(project_root)
    real_primary = os.path.realpath(primary)
    if real_root != real_primary:
        return True
    cwd_toplevel = get_cwd_git_toplevel(cwd)
    if cwd_toplevel is None:
        return False
    return os.path.realpath(cwd_toplevel) != real_root


def _resolve_project_root(cwd: str) -> str | None:
    """
    Resolve project root path with priority:
    1. CLAUDE_PROJECT_DIR env var
    2. Codex hook input cwd ancestry search (not applicable here, handled externally)
    3. git worktree list --porcelain -z primary worktree path
    4. Script path anchor (fallback)
    """
    env_root = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env_root:
        return env_root

    primary = get_primary_worktree_path(cwd=cwd)
    if primary:
        return primary

    # Script anchor fallback: go up from this file's location
    # scripts/agent-guards/ -> scripts/ -> project_root
    script_dir = Path(__file__).resolve().parent.parent.parent
    if (script_dir / ".git").exists() or (script_dir / ".git").is_file():
        return str(script_dir)

    return None


# ─── Command parsing ──────────────────────────────────────────────────────────

# Git global options that affect working directory or config.
# B2: These must be normalized away before subcommand detection.
# Presence of -C, --git-dir, --work-tree, --config-env → fail-closed in local root.
_GIT_GLOBAL_OPTS_WITH_ARG = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--config-env",
    "--namespace",
    "--super-prefix",
    "--exec-path",
}
_GIT_GLOBAL_FLAGS = {
    "--bare",
    "--no-pager",
    "--no-replace-objects",
    "--no-optional-locks",
    "--version",
    "--help",
    "--html-path",
    "--man-path",
    "--info-path",
    "--list-cmds",
}
# These git global options change execution context and must be fail-closed.
# B2 fix: -c is added because `git -c alias.sw='switch issue-N' sw` can bypass branch guard.
_GIT_GLOBAL_OPTS_FAILCLOSED = {"-C", "-c", "--git-dir", "--work-tree", "--config-env"}

# Shell metacharacters that indicate compound/unparseable commands
_SHELL_METACHAR_RE = re.compile(r"[;&<>|`]|\$\(")
_GH_API_ENDPOINT_RE = re.compile(r"^repos/squne121/loop-protocol/issues/comments/\d+$")

# Fd-duplication pattern: 2>&1 immediately before pipe
_FD_DUP_STDERR_STDOUT_RE = re.compile(r"\s+2>&1(\s*\|)")

# Leading env assignment pattern: NAME=value (possibly multiple)
_LEADING_ENV_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*=[^\s]*\s+)+")

# ─── Deterministic checker allowlist ─────────────────────────────────────────
# Deterministic checkers remain exact-path only; root execution of issue-refinement
# preflight moved to skill_runtime_exec.py in Issue #1154.
DETERMINISTIC_CHECKER_ALLOWLIST: list[str] = [
    "scripts/agent-ops/git_ref_probe.py",
    "scripts/agent-ops/git_worktree_probe.py",
]

# Probe script argv specs — must mirror _AGENT_OPS_ARG_SPECS in worktree_scope_guard.py.
# Both guards apply the same shape restrictions so a command allowed by one is also
# subject to the same argv contract in the other (AC6).
_PROBE_ARG_SPECS: dict[str, dict] = {
    "scripts/agent-ops/git_ref_probe.py": {
        "value_flags": frozenset({"--branch", "--remote"}),
        "bool_flags": frozenset({"--json"}),
        "required": frozenset({"--branch"}),
    },
    "scripts/agent-ops/git_worktree_probe.py": {
        "value_flags": frozenset(),
        "bool_flags": frozenset({"--json"}),
        "required": frozenset(),
    },
}


def _validate_probe_argv(rel_script: str, args: list[str]) -> bool:
    """True iff args is a valid, non-redundant argv for a probe script.

    Mirrors _validate_agent_ops_argv in worktree_scope_guard.py so that both
    guards enforce the same exact-argv contract (AC6).
    Rejects unknown flags, duplicates, --flag=value forms, positionals, and
    missing required flags.
    """
    spec = _PROBE_ARG_SPECS.get(rel_script)
    if spec is None:
        return False
    value_flags = spec["value_flags"]
    bool_flags = spec["bool_flags"]
    seen: set[str] = set()
    i = 0
    while i < len(args):
        tok = args[i]
        if not tok.startswith("--") or "=" in tok:
            return False
        if tok in seen:
            return False
        seen.add(tok)
        if tok in bool_flags:
            i += 1
            continue
        if tok in value_flags:
            if i + 1 >= len(args):
                return False
            if args[i + 1].startswith("--"):
                return False
            i += 2
            continue
        return False
    return spec["required"].issubset(seen)


# ─── Gh issue/pr command pattern (allowlist-closed, AC11) ─────────────────────
# ANY gh issue/pr command not present in DISPLAY_READONLY_PATTERNS or is_github_remote_ops_command is blocked.
GH_ISSUE_PR_COMMAND_PATTERN = re.compile(r"^gh\s+(issue|pr)\s+")

# Patterns for display-oriented read-only commands.
DISPLAY_READONLY_PATTERNS = [
    r"^rg(\s|$)",
    r"^grep(\s|$)",
    r"^cat(\s|$)",
    r"^head(\s|$)",
    r"^tail(\s|$)",
    r"^wc(\s|$)",
    r"^git\s+(status|log|show|diff|blame|annotate|shortlog|describe)(\s|$)",
    r"^git\s+branch\s+(--show-current|-a|-r|-v|--list|--verbose)(\s|$)",
    r"^git\s+rev-parse(\s|$)",
    r"^git\s+ls-files(\s|$)",
    r"^git\s+worktree\s+list(\s|$)",
    r"^gh\s+issue\s+(view|list)(\s|$)",
    r"^gh\s+pr\s+(view|list|status)(\s|$)",
]

# Operations that may touch local metadata but do not move the root checkout.
BRANCH_SAFE_MAINTENANCE_PATTERNS = [
    r"^git\s+fetch(\s|$)",
    r"^git\s+worktree\s+prune(\s|$)",
]

READONLY_PIPELINE_SEGMENT_PATTERNS = [
    r"^head(?:\s+-n\s+\d+)?$",
    r"^tail(?:\s+-n\s+\d+)?$",
    r"^wc\s+-l$",
]

# git checkout path-restore forms (NOT branch switch)
# These match `git checkout -- <path>`, `git checkout HEAD -- <path>`,
# `git checkout --pathspec-from-file=`, `git checkout -p`
CHECKOUT_PATH_RESTORE_PATTERNS = [
    r"^git\s+checkout\s+--\s+\S",  # git checkout -- <path>
    r"^git\s+checkout\s+HEAD\s+--\s+\S",  # git checkout HEAD -- <path>
    r"^git\s+checkout\s+HEAD~?\d*\s+--\s+\S",  # git checkout HEAD~N -- <path>
    r"^git\s+checkout\s+--pathspec-from-file=",  # --pathspec-from-file=
    r"^git\s+checkout\s+-p(\s|$)",  # git checkout -p [-- path]
    r"^git\s+checkout\s+-p\s+--\s+",  # git checkout -p -- <path>
    r"^git\s+restore(\s|$)",  # git restore <path>
]

# Minimal allowlist when root is already drifted or in detached_or_unknown state.
# B3/B4 fix: All commands NOT in this list are blocked.
# Note: git switch/checkout recovery to default branch is handled by Step 9 (branch mutation
# with target == default_branch => REASON_RECOVERY). They do NOT need to be listed here.
# B3 fix: git restore and git checkout -- <path> are intentionally NOT listed here;
# path restore is only allowed when root_state == "default" (handled in Step 8).
_DRIFTED_ALLOWLIST_PATTERNS = [
    # git read-only
    r"^git\s+status(\s|$)",
    r"^git\s+branch\s+(--show-current|-a|-r|-v|--list|--verbose)(\s|$)",
    r"^git\s+rev-parse(\s|$)",
    r"^git\s+worktree\s+(list|prune)(\s|$)",
    r"^git\s+fetch(\s|$)",
    r"^git\s+(log|show|diff|blame|annotate|shortlog|describe)(\s|$)",
    # gh read-only
    r"^gh\s+issue\s+(view|list)(\s|$)",
    r"^gh\s+pr\s+(view|list|status)(\s|$)",
]


def _has_leading_env_assignment(cmd: str) -> bool:
    """
    Return True if cmd starts with one or more NAME=value env assignments.
    E.g.: FOO=bar git switch issue-*
    B3: leading env assignments must be detected for fail-closed handling.
    """
    return bool(_LEADING_ENV_ASSIGN_RE.match(cmd))


def _strip_leading_env_assignments(cmd: str) -> str:
    """Strip leading NAME=value env assignments from cmd."""
    return _LEADING_ENV_ASSIGN_RE.sub("", cmd).strip()


def _has_shell_metachar(cmd: str) -> bool:
    """Return True if cmd contains shell metacharacters indicating compound commands."""
    return bool(_SHELL_METACHAR_RE.search(cmd))


def _normalize_git_global_opts(tokens: list[str]) -> tuple[list[str], bool]:
    """
    Normalize git global options from a token list.
    Returns (remaining_tokens_starting_from_subcommand, fail_closed).

    B2: If any fail-closed global option (-C, --git-dir, --work-tree, --config-env)
    is present, return fail_closed=True.

    Handles both:
      git -C . switch issue-*    (option + value as separate tokens)
      git --git-dir=/path switch  (option=value form)
    """
    if not tokens or tokens[0] != "git":
        return tokens, False

    fail_closed = False
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        # Check for option=value form: --git-dir=/path
        opt_key = tok.split("=", 1)[0] if "=" in tok else tok
        if opt_key in _GIT_GLOBAL_OPTS_WITH_ARG:
            if opt_key in _GIT_GLOBAL_OPTS_FAILCLOSED:
                fail_closed = True
            # Skip: the option and its value (if separate token)
            if "=" not in tok:
                i += 2  # skip option and value token
            else:
                i += 1  # value is embedded
        elif tok in _GIT_GLOBAL_FLAGS:
            i += 1
        else:
            # Reached the subcommand
            break
    # Remaining: ["git"] + tokens[i:]  (preserve "git" at front for downstream matching)
    return ["git"] + tokens[i:], fail_closed


def tokenize_command(cmd: str) -> list[str] | None:
    """
    Tokenize command using shlex.split().
    Returns None if parsing fails (treat as unparseable → fail-closed).
    B6: shlex.split() replaces cmd.split() for correct quoting handling.
    """
    try:
        return shlex.split(cmd)
    except ValueError:
        return None


def _redact_token(token: str) -> str:
    """Apply lightweight redaction for diagnostic argv output."""
    redaction_sensitive = {
        "--body",
        "--body-file",
        "--input",
        "--raw-field",
        "-f",
        "-F",
        "--field",
    }
    if token in redaction_sensitive:
        return token
    if token.startswith("--") and "=" in token:
        if token.startswith(("--body=", "--input=", "--field=", "--raw-field=")):
            key = token.split("=", 1)[0]
            return f"{key}=<redacted>"
        return token
    if "://github.com/" in token and "#issuecomment-" in token:
        return "<github-issue-comment-url>"
    if len(token) > 50:
        return "<redacted>"
    return token


def _redact_argv(tokens: list[str] | None) -> list[str]:
    """Return redacted argv representation for diagnostics."""
    if not tokens:
        return []
    redacted: list[str] = []
    skip_next = False
    sensitive_flags = {
        "--body-file",
        "--body",
        "--input",
        "--field",
        "--raw-field",
        "-f",
        "-F",
    }
    for idx, token in enumerate(tokens):
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        normalized = _redact_token(token)
        if token in sensitive_flags and idx + 1 < len(tokens):
            redacted.append(normalized)
            skip_next = True
            continue
        redacted.append(normalized)
    return redacted


def _parse_gh_api_command(cmd: str) -> bool:
    """
    Parse and classify gh api endpoint access.

    Allow:
      - `gh api repos/squne121/loop-protocol/issues/comments/<id>`
      - `gh api --method GET repos/squne121/loop-protocol/issues/comments/<id>`

    Block:
      - GraphQL call style (`graphql`)
      - `--method` values other than GET
      - mutation flags (`-f`, `-F`, `--field`, `--input`)
    """
    tokens = tokenize_command(cmd)
    if not tokens or len(tokens) < 3:
        return False
    if tokens[0] != "gh" or tokens[1] != "api":
        return False

    method = "GET"
    i = 2
    endpoint: str | None = None

    while i < len(tokens):
        token = tokens[i]

        if token == "graphql":
            return False

        if token == "--method":
            if i + 1 >= len(tokens):
                return False
            method = tokens[i + 1].upper()
            i += 2
            continue
        if token.startswith("--method="):
            method = token.split("=", 1)[1].upper()
            i += 1
            continue

        if token in {"-f", "-F", "--field", "--input", "--raw-field", "--hostname", "--paginate"}:
            return False
        if token.startswith(("--field=", "--input=", "--raw-field=", "--hostname=")):
            return False
        if token.startswith("-") and not endpoint:
            # Skip unknown flags; values remain allowed only if no endpoint consumed.
            i += 1
            continue

        if endpoint is None:
            endpoint = token
        # Any extra bare token after first endpoint is treated as non-matching extension.
        else:
            return False
        i += 1

    if not endpoint:
        return False
    if method != "GET":
        return False
    if "{owner}" in endpoint or "{repo}" in endpoint:
        return False
    if not _GH_API_ENDPOINT_RE.fullmatch(endpoint):
        return False
    return True


def _is_gh_api_mutation_command(cmd: str) -> bool:
    """
    Return True when gh api command is present but does not meet allowlist criteria.
    """
    tokens = tokenize_command(cmd)
    if not tokens or len(tokens) < 2:
        return False
    return tokens[0] == "gh" and tokens[1] == "api"


def _is_rtk_command(tokens: list[str]) -> bool:
    return bool(tokens) and tokens[0] == "rtk"


def _gh_has_web_flag(cmd: str) -> bool:
    """Return True if a gh command includes --web or -w flag (interactive browser open)."""
    tokens = tokenize_command(cmd)
    if tokens is None:
        return False
    return "--web" in tokens or "-w" in tokens


def _is_safe_tmp_body_file(path: str) -> bool:
    """
    Validate that body-file path is confined to project_root/tmp.
    Blocks absolute paths, path traversal (../), and non-tmp/ prefixes.
    """
    if not path or path == "-":
        return False
    if path.startswith("/") or "\\" in path or "\x00" in path:
        return False
    p = Path(path)
    if not p.parts or p.parts[0] != "tmp":
        return False
    if ".." in p.parts:
        return False
    project_root = Path(__file__).resolve().parent.parent.parent
    try:
        resolved = (project_root / p).resolve(strict=False)
        tmp_root = (project_root / "tmp").resolve(strict=False)
        return resolved.is_relative_to(tmp_root)
    except Exception:
        return False


def _is_safe_tmp_redirect_dest(dest: str) -> bool:
    """
    Validate that redirect destination is confined to project_root/tmp.
    Blocks absolute paths, path traversal (../), and non-tmp/ prefixes.
    """
    if not dest:
        return False
    if dest.startswith("/") or "\\" in dest or "\x00" in dest:
        return False
    p = Path(dest)
    if not p.parts or p.parts[0] != "tmp":
        return False
    if ".." in p.parts:
        return False
    project_root = Path(__file__).resolve().parent.parent.parent
    try:
        resolved = (project_root / p).resolve(strict=False)
        tmp_root = (project_root / "tmp").resolve(strict=False)
        return resolved.is_relative_to(tmp_root)
    except Exception:
        return False


def _find_flag_value(tokens: list[str], flag: str) -> "str | None":
    """
    Find the value of a flag in a token list.
    Handles both '--flag value' and '--flag=value' forms.
    Returns None if flag not found or has no value.
    """
    for i, t in enumerate(tokens):
        if t == flag:
            if i + 1 < len(tokens) and tokens[i + 1] and not tokens[i + 1].startswith("-"):
                return tokens[i + 1]
            return None
        if t.startswith(f"{flag}="):
            val = t[len(flag) + 1 :]
            return val if val else None
    return None


def _find_body_file_value(tokens: list[str]) -> "str | None":
    """Find the value of --body-file flag in a token list."""
    return _find_flag_value(tokens, "--body-file")


def _has_body_value(tokens: list[str]) -> bool:
    """
    Return True if --body/-b has an actual non-empty value (not another flag).
    Handles '--body <text>', '--body=<text>', '-b <text>'.
    """
    for i, t in enumerate(tokens):
        if t in ("--body", "-b"):
            if i + 1 < len(tokens) and tokens[i + 1] and not tokens[i + 1].startswith("-"):
                return True
            return False
        if t.startswith("--body="):
            val = t[len("--body=") :]
            return bool(val)
    return False


def is_readonly_command(cmd: str) -> bool:
    """Return True if the command is a display-oriented read-only command."""
    cmd = cmd.strip()
    # B3: gh issue/pr view --web/-w opens a browser (interactive); block it
    if re.match(r"^gh\s+(issue|pr)\s+(view|list|status)(\s|$)", cmd) and _gh_has_web_flag(cmd):
        return False
    for pattern in DISPLAY_READONLY_PATTERNS:
        if re.match(pattern, cmd):
            return True
    return False


def is_branch_safe_maintenance_command(cmd: str) -> bool:
    """Return True if the command mutates metadata but not the root checkout branch."""
    cmd = cmd.strip()
    for pattern in BRANCH_SAFE_MAINTENANCE_PATTERNS:
        if re.match(pattern, cmd):
            return True
    return False


def is_cleanup_class_command(cmd: str) -> bool:
    """Return True for the exact cleanup commands arbitrated by worktree_scope_guard.

    Issue #1137: only the exact 4-token forms ``git worktree remove <path>`` and
    ``git branch -d <branch>`` are recognized. Force variants (``-D`` / ``--force``
    / ``-f`` / multi-target) and extra args are NOT cleanup-class here; they fall
    through to generic branch-mutation / drift handling so they cannot be deferred.
    """
    tokens = tokenize_command(cmd)
    if not tokens or len(tokens) != 4 or tokens[0] != "git":
        return False
    if tokens[1] == "worktree" and tokens[2] == "remove":
        return True
    if tokens[1] == "branch" and tokens[2] == "-d":
        return True
    return False


def is_deterministic_checker_command(cmd: str, project_root: str | None = None) -> bool:
    """
    Return True if cmd is an exact-allowlisted deterministic checker script.
    Wildcard patterns are prohibited (AC5/AC12).
    """
    cmd = cmd.strip()
    tokens = tokenize_command(cmd)
    if not tokens or len(tokens) < 4:
        return False
    if tokens[:3] != ["uv", "run", "python3"]:
        return False
    script_path = tokens[3]
    for allowed in DETERMINISTIC_CHECKER_ALLOWLIST:
        if script_path == allowed or (project_root and script_path == os.path.join(project_root, allowed)):
            return _validate_probe_argv(allowed, tokens[4:])
    return False


def _is_skill_runtime_executor_command(cmd: str, cwd: str, project_root: str | None = None) -> bool:
    """Exact privileged skill runtime executor classifier (Issue #1154)."""
    if project_root is None:
        project_root = _resolve_project_root(cwd)
    if not project_root:
        return False
    return is_exact_skill_runtime_executor_command(cmd, cwd, project_root)


_ISSUE_REFINEMENT_LOOP_SCRIPTS_REL = os.path.join(".claude", "skills", "issue-refinement-loop", "scripts")


class LauncherParseKind(Enum):
    """Issue #1543 (OWNER REQUEST_CHANGES): tri-state launcher-grammar parse
    result.

    TARGET      -- a script operand was identified; the caller MUST
                   realpath-check it against the guarded directory.
    NOT_TARGET  -- this classifier confidently determined the command is
                   NOT one of the launcher grammars it claims (e.g.
                   ``python -c`` / ``-m``, combined short options embedding
                   ``c``/``m``) and defers to existing downstream
                   fail-closed classifiers (e.g.
                   ``is_tmp_wrapper_or_python_c_command``).
    UNSUPPORTED -- the launcher grammar could not be safely parsed (unknown
                   option, missing option value, unmodeled wrapper program,
                   or dynamic shell-expansion syntax in the resolved
                   operand). The caller MUST treat this as blocked
                   (fail-closed) -- it must never fall through to a
                   default-allow path.
    """

    TARGET = auto()
    NOT_TARGET = auto()
    UNSUPPORTED = auto()


@dataclass(frozen=True)
class LauncherParse:
    """Result of parsing a launcher command's execution-target grammar."""

    kind: LauncherParseKind
    script_operand: str | None = None
    effective_cwd: str | None = None
    reason: str | None = None


# ─── uv / python launcher arity tables (Issue #1543 Blockers 1-2) ─────────────
# Options NOT present in these tables are treated as UNSUPPORTED (fail-closed)
# rather than guessed as boolean flags -- this is the core arity fix.

_UV_GLOBAL_VALUE_FLAGS = frozenset({
    "--directory",
    "--cache-dir",
    "--config-file",
    "--python-preference",
    "--color",
})
_UV_GLOBAL_BOOL_FLAGS = frozenset({
    "--no-cache",
    "--offline",
    "--refresh",
    "--isolated",
    "--no-config",
    "--native-tls",
    "--no-native-tls",
    "--no-progress",
    "-q",
    "--quiet",
    "-v",
    "--verbose",
})
_UV_RUN_VALUE_FLAGS = frozenset({
    "--directory",
    "--python",
    "-p",
    "--project",
    "--package",
    "--extra",
    "--group",
    "--no-group",
    "--with",
    "--with-requirements",
    "--with-editable",
    "--index",
    "--index-url",
    "--extra-index-url",
    "-f",
    "--find-links",
    "--env-file",
    "--config-file",
    "--python-preference",
    "--cache-dir",
    "--color",
    "--index-strategy",
    "--resolution",
    "--prerelease",
    "--exclude-newer",
    "--link-mode",
})
_UV_RUN_BOOL_FLAGS = frozenset({
    "--isolated",
    "--no-project",
    "--no-sync",
    "--frozen",
    "--locked",
    "--all-extras",
    "--no-dev",
    "--dev",
    "--exact",
    "--no-config",
    "--offline",
    "--refresh",
    "--no-cache",
    "-v",
    "--verbose",
    "-q",
    "--quiet",
    "--native-tls",
    "--no-native-tls",
    "--no-editable",
    "--all-packages",
    "--compile-bytecode",
})

_PY_VALUE_FLAGS = frozenset({"-W", "-X", "--check-hash-based-pycs"})
_PY_BOOL_FLAGS_EXACT = frozenset({
    "-B", "-E", "-I", "-O", "-OO", "-S", "-s", "-t", "-tt",
    "-u", "-v", "-x", "-b", "-bb", "-d", "-q", "-3",
})
# -c / -m launch an inline/module target this classifier does not model;
# defer to the existing (extended) is_tmp_wrapper_or_python_c_command check.
_PY_UNSUPPORTED_WRAPPER_FORMS = frozenset({"-c", "-m"})

# Issue #1543 High 4: a script operand containing shell-expansion syntax
# cannot be safely resolved via literal realpath comparison because this
# guard does not evaluate shell expansion (variables/globs). Such operands
# are always UNSUPPORTED (fail-closed) rather than compared.
_SHELL_EXPANSION_CHARS_RE = re.compile(r"[$*?\[]")

# Issue #1543 Blocker 3: wrapper programs this classifier does not model.
# `exec`, `time`, `timeout`, and any `env` invocation (bare or
# path-qualified, e.g. /usr/bin/env) shift the actually-executed command
# into a following token this classifier does not walk. A path-qualified
# `uv` (e.g. /usr/local/bin/uv) is likewise not recognized -- only the bare
# `uv` token is modeled below.
_UNMODELED_WRAPPER_HEADS = frozenset({"exec", "time", "timeout"})


def _has_shell_expansion_syntax(token: str) -> bool:
    """True if token contains shell-expansion syntax ($ / * / ? / [) that
    this guard cannot safely evaluate (Issue #1543 High 4)."""
    return bool(_SHELL_EXPANSION_CHARS_RE.search(token))


def _parse_uv_run_options(
    toks: Sequence[str], start_idx: int, cwd: str, effective_cwd: str
) -> tuple[int, str, "LauncherParseKind"]:
    """Parse ``uv run`` sub-options starting at ``start_idx`` (just after
    ``run``). Returns ``(idx, effective_cwd, kind)`` where ``kind`` is
    TARGET (positional found at ``idx``) or UNSUPPORTED (unknown or
    missing-value option encountered, or no positional found)."""
    idx = start_idx
    while idx < len(toks):
        tok = toks[idx]
        if tok == "--":
            idx += 1
            if idx < len(toks):
                return idx, effective_cwd, LauncherParseKind.TARGET
            return idx, effective_cwd, LauncherParseKind.UNSUPPORTED
        if not tok.startswith("-"):
            return idx, effective_cwd, LauncherParseKind.TARGET
        opt_key = tok.split("=", 1)[0]
        has_inline_value = "=" in tok
        if opt_key == "--directory":
            if has_inline_value:
                value = tok.split("=", 1)[1]
                idx += 1
            else:
                if idx + 1 >= len(toks):
                    return idx, effective_cwd, LauncherParseKind.UNSUPPORTED
                value = toks[idx + 1]
                idx += 2
            effective_cwd = value if os.path.isabs(value) else os.path.join(cwd, value)
            continue
        if opt_key in _UV_RUN_VALUE_FLAGS:
            if has_inline_value:
                idx += 1
            else:
                if idx + 1 >= len(toks) or toks[idx + 1].startswith("--"):
                    return idx, effective_cwd, LauncherParseKind.UNSUPPORTED
                idx += 2
            continue
        if opt_key in _UV_RUN_BOOL_FLAGS:
            idx += 1
            continue
        return idx, effective_cwd, LauncherParseKind.UNSUPPORTED
    return idx, effective_cwd, LauncherParseKind.UNSUPPORTED


def _parse_python_options(toks: Sequence[str], start_idx: int) -> tuple[int, "LauncherParseKind"]:
    """Parse python-interpreter sub-options starting at ``start_idx`` (just
    after the interpreter token). Returns ``(idx, kind)``."""
    idx = start_idx
    while idx < len(toks):
        tok = toks[idx]
        if tok == "--":
            idx += 1
            if idx < len(toks):
                return idx, LauncherParseKind.TARGET
            return idx, LauncherParseKind.UNSUPPORTED
        if not tok.startswith("-"):
            return idx, LauncherParseKind.TARGET
        if tok in _PY_UNSUPPORTED_WRAPPER_FORMS:
            return idx, LauncherParseKind.NOT_TARGET
        opt_key = tok.split("=", 1)[0]
        has_inline_value = "=" in tok
        if opt_key in _PY_VALUE_FLAGS:
            if has_inline_value:
                idx += 1
            else:
                if idx + 1 >= len(toks):
                    return idx, LauncherParseKind.UNSUPPORTED
                idx += 2
            continue
        if opt_key in _PY_BOOL_FLAGS_EXACT:
            idx += 1
            continue
        if not tok.startswith("--") and len(tok) > 2:
            # Combined short-option form (e.g. -Ic). Exact doubled flags
            # like -tt/-bb/-OO are matched above; anything else combined may
            # embed -c/-m and is not modeled here -- defer (Issue #1543
            # rule 5) rather than guess.
            return idx, LauncherParseKind.NOT_TARGET
        return idx, LauncherParseKind.UNSUPPORTED
    return idx, LauncherParseKind.UNSUPPORTED


def _extract_launcher_script_operand(tokens: Sequence[str], cwd: str) -> LauncherParse:
    """Parse a launcher command's actually-executed script operand using an
    argv-aware, arity-correct grammar (Issue #1543 OWNER REQUEST_CHANGES).

    Supported grammars:
      - ``<target-script>``                             (direct exec)
      - ``python3 [opts] <script>``                      (any ``pythonX[.Y]`` name; arity-correct option table)
      - ``uv run [opts] [--] <script>``                   (arity-correct uv-run option table)
      - ``uv run [opts] [--] python3 [opts] <script>``
      - ``uv [global-opts] run [opts] [--] <script>``     (uv global options preceding ``run``)

    Any other launcher shape -- ``python -c`` / ``-m``, combined short
    options such as ``-Ic``, unknown/missing-value options, dynamic
    shell-expansion syntax (``$``, ``${``, ``*``, ``?``, ``[``) in the
    resolved operand, or unmodeled wrapper programs such as ``exec`` /
    ``env`` / ``time`` / ``timeout`` / path-qualified ``uv`` -- returns
    ``NOT_TARGET`` (defer to existing downstream fail-closed classifiers) or
    ``UNSUPPORTED`` (the caller MUST treat this as blocked; never allow an
    unparseable launcher shape).
    """
    toks = list(tokens)
    if not toks:
        return LauncherParse(LauncherParseKind.NOT_TARGET, reason="empty_command")

    effective_cwd = cwd
    head = toks[0]
    head_base = os.path.basename(head)

    if head in _UNMODELED_WRAPPER_HEADS or head_base == "env":
        return LauncherParse(LauncherParseKind.UNSUPPORTED, reason=f"unmodeled_wrapper:{head}")
    if head != "uv" and head_base == "uv":
        return LauncherParse(LauncherParseKind.UNSUPPORTED, reason="path_qualified_uv_unsupported")

    idx = 0
    if head == "uv":
        if len(toks) < 2:
            return LauncherParse(LauncherParseKind.UNSUPPORTED, reason="uv_missing_subcommand")
        gidx = 1
        while gidx < len(toks):
            tok = toks[gidx]
            if tok == "run":
                break
            opt_key = tok.split("=", 1)[0]
            has_inline_value = "=" in tok
            if opt_key == "--directory":
                if has_inline_value:
                    value = tok.split("=", 1)[1]
                    gidx += 1
                else:
                    if gidx + 1 >= len(toks):
                        return LauncherParse(LauncherParseKind.UNSUPPORTED, reason="uv_directory_missing_value")
                    value = toks[gidx + 1]
                    gidx += 2
                effective_cwd = value if os.path.isabs(value) else os.path.join(cwd, value)
                continue
            if opt_key in _UV_GLOBAL_VALUE_FLAGS:
                if has_inline_value:
                    gidx += 1
                else:
                    if gidx + 1 >= len(toks) or toks[gidx + 1].startswith("--"):
                        return LauncherParse(LauncherParseKind.UNSUPPORTED, reason="uv_global_option_missing_value")
                    gidx += 2
                continue
            if opt_key in _UV_GLOBAL_BOOL_FLAGS:
                gidx += 1
                continue
            if tok.startswith("-"):
                return LauncherParse(LauncherParseKind.UNSUPPORTED, reason=f"uv_unknown_global_option:{tok}")
            # Non-flag, non-`run` token: a different uv subcommand (sync,
            # pip, tool, add, ...) this classifier does not model as a
            # script launcher. Defer to existing classification.
            return LauncherParse(LauncherParseKind.NOT_TARGET, reason=f"uv_non_run_subcommand:{tok}")
        if gidx >= len(toks) or toks[gidx] != "run":
            return LauncherParse(LauncherParseKind.UNSUPPORTED, reason="uv_run_not_found")
        run_idx, effective_cwd, kind = _parse_uv_run_options(toks, gidx + 1, cwd, effective_cwd)
        if kind is not LauncherParseKind.TARGET:
            return LauncherParse(kind, reason="uv_run_option_parse_failed")
        idx = run_idx

    if idx < len(toks) and re.fullmatch(r"python[0-9.]*", os.path.basename(toks[idx])):
        py_idx, kind = _parse_python_options(toks, idx + 1)
        if kind is not LauncherParseKind.TARGET:
            return LauncherParse(kind, reason="python_option_parse_failed")
        idx = py_idx

    if idx >= len(toks):
        return LauncherParse(LauncherParseKind.UNSUPPORTED, reason="missing_script_operand")

    script_operand = toks[idx]
    if _has_shell_expansion_syntax(script_operand):
        return LauncherParse(LauncherParseKind.UNSUPPORTED, reason="shell_expansion_syntax_in_operand")

    return LauncherParse(LauncherParseKind.TARGET, script_operand=script_operand, effective_cwd=effective_cwd)


def _looks_like_direct_issue_refinement_runtime_command(
    tokens: Sequence[str] | None, cwd: str, project_root: str | None = None
) -> bool:
    """Argv-aware classifier (Issue #1543): True iff `tokens` actually launches
    a script under `.claude/skills/issue-refinement-loop/scripts/` as its
    execution target -- not merely mentions the path substring somewhere in
    an unrelated argument value (e.g. an `--allowed-paths` JSON array) -- OR
    the launcher grammar could not be safely parsed (UNSUPPORTED), in which
    case this classifier fails closed and blocks rather than falling through
    to a default-allow path (OWNER REQUEST_CHANGES, Issue #1543 Blockers
    1-4 / High 4).

    Path identity is compared via realpath normalization so `./` relative
    forms, absolute forms, and symlink-mediated indirection all resolve to
    the same canonical script identity.
    """
    if not tokens:
        return False
    if project_root is None:
        project_root = _resolve_project_root(cwd)
    if not project_root:
        return False

    parsed = _extract_launcher_script_operand(tokens, cwd)

    if parsed.kind is LauncherParseKind.UNSUPPORTED:
        # Fail-closed: an unparseable launcher shape must never be treated
        # as safe-to-allow. This rule blocks it directly rather than letting
        # it fall through to a downstream default-allow path.
        return True
    if parsed.kind is LauncherParseKind.NOT_TARGET:
        # Confidently not one of the grammars this classifier claims (e.g.
        # python -c/-m, combined short options). Defer to downstream
        # fail-closed classifiers (e.g. is_tmp_wrapper_or_python_c_command).
        return False

    script_operand = parsed.script_operand
    effective_cwd = parsed.effective_cwd or cwd
    if not script_operand:
        return False
    # skill_runtime_exec.py invocations are governed by the exact allow/deny
    # classifiers above this call site in evaluate(); this classifier never
    # re-decides them (defensive redundancy -- unreachable in practice given
    # call order, but kept for explicitness).
    if os.path.basename(script_operand) == "skill_runtime_exec.py":
        return False

    candidate = script_operand if os.path.isabs(script_operand) else os.path.join(effective_cwd, script_operand)
    resolved = os.path.realpath(candidate)
    target_dir = os.path.realpath(os.path.join(project_root, _ISSUE_REFINEMENT_LOOP_SCRIPTS_REL))
    try:
        common = os.path.commonpath([target_dir, resolved])
    except ValueError:
        return False
    return common == target_dir


def is_github_remote_ops_command(cmd: str) -> bool:
    """
    post-merge-cleanup で必要な GitHub remote ops の最小集合のみを allow。
    token ベース classifier でフラグ・引数まで検証する。

    Return True: post-merge-cleanup 最小集合に合致 → allow（ブロックしない）
    Return False: 合致しない → caller が is_gh_mutation_command のブロック判定に従う

    許可対象（post-merge-cleanup 最小集合）:
      - gh issue close <N>              N は数値 issue 番号必須
      - gh issue reopen <N>             N は数値 issue 番号必須
      - gh pr comment <N> --body <V> / --body-file tmp/...
                        N は数値、body 値必須、canonical tmp/ パス
      - gh pr edit <N> ...              N は数値 PR 番号必須、--base は block

    明示ブロック対象フラグ（全コマンド共通）:
      - --delete-last  destructive
      - --edit-last    destructive
      - --editor / -e  interactive
      - --web / -w     interactive browser
      - --create-if-none  interactive
      - --yes          silent-destructive
    """
    cmd = cmd.strip()
    tokens = tokenize_command(cmd)
    if tokens is None or len(tokens) < 3:
        return False
    if tokens[0] != "gh":
        return False
    resource = tokens[1]  # "issue" or "pr"
    subcommand = tokens[2]  # "close", "comment", "reopen", "edit"

    # Destructive / interactive flags → block always (applied to all subcommands)
    BLOCKED_FLAGS = {
        "--delete-last",
        "--edit-last",
        "--editor",
        "-e",
        "--web",
        "-w",
        "--create-if-none",
        "--yes",
    }
    if any(t in BLOCKED_FLAGS for t in tokens[3:]):
        return False

    if resource == "issue":
        # gh issue close <N>
        if subcommand == "close":
            return len(tokens) >= 4 and tokens[3].isdigit()

        # gh issue reopen <N>
        if subcommand == "reopen":
            return len(tokens) >= 4 and tokens[3].isdigit()

    elif resource == "pr":
        # B4: gh pr comment <N> --body <V> OR --body-file tmp/...
        if subcommand == "comment":
            if len(tokens) < 4 or not tokens[3].isdigit():
                return False
            has_body_val = _has_body_value(tokens[4:])
            body_file = _find_body_file_value(tokens)
            if body_file is not None:
                if not _is_safe_tmp_body_file(body_file):
                    return False
                return True
            return has_body_val

        # B4: gh pr edit <N> ... (--base is blocked, --body-file canonical path)
        if subcommand == "edit":
            if not (len(tokens) >= 4 and tokens[3].isdigit()):
                return False
            # --base changes base branch → block
            if "--base" in tokens[4:]:
                return False
            # --body-file, if present, must be canonical tmp/ path
            body_file = _find_body_file_value(tokens)
            if body_file is not None and not _is_safe_tmp_body_file(body_file):
                return False
            return True

    return False


def is_github_issue_mutation_command(cmd: str) -> bool:
    """
    Classify gh issue create as github_issue_mutation_command → allow.

    Allow conditions (ALL must be satisfied):
      1. Command is `gh issue create`
      2. --repo squne121/loop-protocol present (exact match)
      3. --body-file <path> present where path is canonical tmp/ path and is NOT "-"
      4. No interactive flags: --editor / -e / --web / -w
      5. B1: `gh issue create` requires --title with actual value

    Block conditions (any one triggers block → return False):
      - --body-file - (stdin)
      - --editor / -e / --web / -w
      - Missing --repo or wrong repo
      - Missing --body-file
      - Any `gh issue edit ...`
      - bare `gh issue create`
      - B1: `gh issue create` without --title value
      - B2: --body-file with path traversal (tmp/../...) or absolute path

    Note: gh issue close/reopen remain handled by is_github_remote_ops_command.
    Raw gh issue comment intentionally falls through to gh_mutation_denied.
    """
    cmd = cmd.strip()
    tokens = tokenize_command(cmd)
    if tokens is None or len(tokens) < 3:
        return False
    if tokens[0] != "gh" or tokens[1] != "issue":
        return False

    subcommand = tokens[2]
    if subcommand != "create":
        return False

    args = tokens[3:]

    # Interactive flags → block
    INTERACTIVE_FLAGS = {"--editor", "-e", "--web", "-w"}
    if any(t in INTERACTIVE_FLAGS for t in args):
        return False

    # B1: gh issue create requires --title with a real value
    title_val = _find_flag_value(list(args), "--title")
    if not title_val:
        return False

    allowed_flags = {"--repo", "--body-file"}
    allowed_flags.add("--title")

    # Check --repo matches trusted slug and reject mutation-expanding flags.
    has_trusted_repo = False
    i = 0
    while i < len(args):
        tok = args[i]
        flag_name = tok.split("=", 1)[0]
        if tok.startswith("--") and flag_name not in allowed_flags:
            return False
        if tok == "--repo":
            if i + 1 < len(args) and args[i + 1] == TRUSTED_REPO_SLUG:
                has_trusted_repo = True
            i += 2
        elif tok.startswith("--repo="):
            if tok[len("--repo=") :] == TRUSTED_REPO_SLUG:
                has_trusted_repo = True
            i += 1
        else:
            i += 1

    if not has_trusted_repo:
        return False

    # B2: Check --body-file is present and path is canonical tmp/ (no path traversal)
    has_body_file = False
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--body-file":
            if i + 1 < len(args):
                path = args[i + 1]
                if path != "-" and _is_safe_tmp_body_file(path):
                    has_body_file = True
            i += 2
        elif tok.startswith("--body-file="):
            path = tok[len("--body-file=") :]
            if path != "-" and _is_safe_tmp_body_file(path):
                has_body_file = True
            i += 1
        else:
            i += 1

    return has_body_file


# Regex for readonly artifact export: gh issue view <N> [args] > tmp/<filename>
# Only `>` (not `>>`) to tmp/ path is allowed. No other destinations.
_READONLY_EXPORT_RE = re.compile(
    r"^gh\s+issue\s+view\s+\d+"  # gh issue view <N>
    r"(?:\s+[^>]*)?"  # optional args (no > inside)
    r"\s+>\s+"  # single redirect `>`
    r"(tmp/\S+)$"  # destination starts with tmp/
)
# Blocked destination patterns for readonly artifact export
_BLOCKED_EXPORT_DEST_RE = re.compile(r"^(src/|docs/|\.env|\.git)")


def is_readonly_artifact_export_command(cmd: str) -> bool:
    """
    Classify `gh issue view <N> ... > tmp/<filename>` as readonly_artifact_export_command → allow.

    Allow conditions (ALL must be satisfied):
      1. Command is `gh issue view <N>` (view only, not edit/create)
      2. Redirect is single `>` (not `>>`)
      3. Destination path starts with tmp/ and is canonical (no path traversal)
      4. No other shell metacharacters (&&, ||, ;, |, backtick, $()

    Block conditions (any one triggers block → return False):
      - `>>` append redirect
      - Destination is src/*, docs/*, .env, .git*
      - Any shell metachar other than the single `>` redirect
      - Pipe `|` present
      - Path traversal in destination (tmp/../...)
    """
    cmd = cmd.strip()

    # Must be a gh issue view command
    if not re.match(r"^gh\s+issue\s+view\s+", cmd):
        return False

    # Block append redirect
    if ">>" in cmd:
        return False

    # Block other dangerous metacharacters (pipe, semicolon, &&, ||, backtick, $()
    # We allow exactly one `>` for the redirect
    # Split on `>` — must have exactly 2 parts
    parts = cmd.split(">")
    if len(parts) != 2:
        return False

    lhs = parts[0]  # the command part before `>`
    rhs = parts[1].strip()  # the destination

    # lhs must not contain any other metacharacters
    if _SHELL_METACHAR_RE.search(lhs):
        return False

    # rhs (destination) must be canonical tmp/ path (no path traversal)
    if not _is_safe_tmp_redirect_dest(rhs):
        return False
    if _SHELL_METACHAR_RE.search(rhs):
        return False
    if _BLOCKED_EXPORT_DEST_RE.match(rhs):
        return False

    # Validate the lhs is a pure gh issue view command
    lhs_stripped = lhs.strip()
    if not re.match(r"^gh\s+issue\s+view\s+\d+", lhs_stripped):
        return False

    # Block --web/-w flag: browser open is not a readonly artifact export
    if _gh_has_web_flag(lhs_stripped):
        return False

    return True


def is_gh_mutation_command(cmd: str) -> bool:
    """gh issue/pr コマンドで readonly allowlist、is_github_remote_ops_command、
    is_github_issue_mutation_command 以外のものは fail-closed ブロック (allowlist-closed, AC11)。
    DISPLAY_READONLY_PATTERNS に含まれる gh issue view/list, gh pr view/list/status のみ通過。
    is_github_remote_ops_command に合致する post-merge-cleanup 最小集合は通過（GitHub ops として許可）。
    is_github_issue_mutation_command に合致する gh issue create/edit（--repo + --body-file 必須）は通過。
    gh issue develop/transfer/pin/unpin, gh pr merge/checkout/revert/lock/unlock 等はすべてブロック。
    """
    cmd = cmd.strip()
    # Only applies to gh issue/pr subcommands
    if not GH_ISSUE_PR_COMMAND_PATTERN.match(cmd):
        return False
    # If in readonly allowlist (and not overridden by web flag etc.), it is NOT a mutation.
    # Use is_readonly_command() so that --web/-w blocking (B3) flows through correctly.
    if is_readonly_command(cmd):
        return False
    # gh issue/pr not in any allowlist → treat as mutation → block
    return True


_PY_C_OR_M_TOKEN_RE = re.compile(r"^-[A-Za-z]*[cm][A-Za-z]*$")


def is_tmp_wrapper_or_python_c_command(cmd: str) -> bool:
    """Return True if command is /tmp wrapper or python -c/-m (fail-closed,
    AC14; extended by Issue #1543 rule 5 to also catch -m and combined short
    options embedding c/m such as -Ic, which the launcher-grammar classifier
    defers here via LauncherParseKind.NOT_TARGET)."""
    cmd = cmd.strip()
    tokens = tokenize_command(cmd)
    if not tokens:
        return False
    # Block: python[X.Y] -c / -m / combined short option embedding c or m
    # (inline code / module execution), as a LEADING option only.
    head_base = os.path.basename(tokens[0])
    if re.fullmatch(r"python[0-9.]*", head_base):
        for tok in tokens[1:]:
            if tok == "--" or not tok.startswith("-"):
                break
            if tok in ("-c", "-m"):
                return True
            if not tok.startswith("--") and len(tok) > 1 and _PY_C_OR_M_TOKEN_RE.match(tok):
                return True
    # Block: uv run python3 /tmp/*.py
    if len(tokens) >= 4 and tokens[:3] == ["uv", "run", "python3"] and tokens[3].startswith("/tmp/"):
        return True
    # Block: direct /tmp/*.py execution
    if tokens[0].startswith("/tmp/") and tokens[0].endswith(".py"):
        return True
    return False


def _tokenize_readonly_pipeline(cmd: str) -> list[str] | None:
    """Tokenize a potential read-only pipeline with shell punctuation preserved."""
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars="|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _is_readonly_pipeline_segment(cmd: str) -> bool:
    """Return True if a segment is an allowed post-processing reader."""
    normalized = cmd.strip()
    for pattern in READONLY_PIPELINE_SEGMENT_PATTERNS:
        if re.match(pattern, normalized):
            return True
    return False


def classify_readonly_pipeline(cmd: str) -> bool:
    """
    Allow only narrow readonly pipeline forms.

    Examples:
      rg -n "TODO" README.md | head -n 20
      git status --short | head -n 20
      git diff --stat | head -n 20
      git diff --stat 2>&1 | head -n 20  (fd-duplication, AC10)
    """
    # Normalize fd-duplication: "2>&1 |" -> " |" (fd-dup before pipe only, AC10)
    cmd_to_parse = _FD_DUP_STDERR_STDOUT_RE.sub(r" \1", cmd).strip()

    if "|" not in cmd_to_parse:
        return False
    if any(token in cmd_to_parse for token in ("&&", "||", ";", "`", "$(")):
        return False

    tokens = _tokenize_readonly_pipeline(cmd_to_parse)
    if not tokens:
        return False

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token == "|":
            if not current:
                return False
            segments.append(current)
            current = []
            continue
        if token in {"<", "<<", "<<<", ">", ">>"}:
            return False
        current.append(token)

    if not current or not segments:
        return False
    segments.append(current)

    first_segment = " ".join(segments[0]).strip()
    if not is_readonly_command(first_segment):
        return False

    for segment_tokens in segments[1:]:
        if not _is_readonly_pipeline_segment(" ".join(segment_tokens)):
            return False

    return True


def is_path_restore_command(cmd: str) -> bool:
    """Return True if command is a git checkout path-restore (not branch switch)."""
    cmd = cmd.strip()
    for pattern in CHECKOUT_PATH_RESTORE_PATTERNS:
        if re.match(pattern, cmd):
            return True
    return False


def is_compound_or_wrapped(cmd: str) -> bool:
    """
    Return True if command appears to be a compound/wrapped shell command.
    B6: Uses shlex parsing and metachar detection.
    """
    # Check for shell metacharacters (;, &&, ||, |, `, $()
    if _has_shell_metachar(cmd):
        return True
    # Check for shell wrappers at the start
    if re.match(r"^(bash|sh|zsh)\s+", cmd):
        return True
    # env prefix
    if re.match(r"^env\s+", cmd):
        return True
    # command prefix
    if re.match(r"^command\s+", cmd):
        return True
    return False


def _rebuild_normalized_cmd(tokens: list[str]) -> str:
    """Rebuild a normalized command string from tokens (for regex matching)."""
    return " ".join(tokens)


def extract_target_branch_from_tokens(tokens: list[str], subcommand: str) -> str | None:
    """
    Extract target branch from tokenized git switch/checkout/branch command.
    B6: argv-based extraction replaces regex-on-string.
    """
    if subcommand == "switch":
        return _extract_switch_branch_from_tokens(tokens)
    elif subcommand == "checkout":
        return _extract_checkout_branch_from_tokens(tokens)
    elif subcommand == "branch":
        return _extract_branch_rename_from_tokens(tokens)
    return None


def _extract_switch_branch_from_tokens(tokens: list[str]) -> str | None:
    """Extract target branch from git switch token list (tokens[0]='git', tokens[1]='switch')."""
    # tokens: ["git", "switch", ...]
    args = tokens[2:]
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--detach",):
            # Next arg is start-point (optional)
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                return args[i + 1]
            return "<detached>"
        if a in ("--orphan", "--guess", "-c", "-C"):
            if i + 1 < len(args):
                return args[i + 1]
            return None
        if a.startswith("--orphan="):
            return a.split("=", 1)[1]
        if a.startswith("-c=") or a.startswith("-C="):
            return a.split("=", 1)[1]
        if a.startswith("-") and len(a) == 2:
            # Short flag without value (e.g. -d), skip
            i += 1
            continue
        if a.startswith("--"):
            # Unknown long flag, skip
            i += 1
            continue
        # Non-flag arg = branch name
        return a
        i += 1
    return None


def _extract_checkout_branch_from_tokens(tokens: list[str]) -> str | None:
    """Extract target branch from git checkout token list."""
    args = tokens[2:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--detach":
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                return args[i + 1]
            return "<detached>"
        if a in ("--orphan", "--ignore-other-worktrees", "-b", "-B"):
            if i + 1 < len(args):
                return args[i + 1]
            return None
        if a == "--":
            # Everything after -- is a path, not a branch
            return None
        if a.startswith("-") and not a.startswith("--"):
            i += 1
            continue
        if a.startswith("--"):
            i += 1
            continue
        # Non-flag = branch name
        return a
        i += 1
    return None


def _extract_branch_rename_from_tokens(tokens: list[str]) -> str | None:
    """Extract new branch name from git branch -m/-M tokens."""
    # tokens: ["git", "branch", "-m"/"M", old?, new]
    args = tokens[2:]
    # Find -m or -M flag
    non_flags = [a for a in args if not a.startswith("-")]
    # If two non-flags: first=old, second=new; if one: it's the new name
    if len(non_flags) >= 2:
        return non_flags[1]
    elif len(non_flags) == 1:
        return non_flags[0]
    return None


def extract_target_branch_from_git_switch(cmd: str) -> str | None:
    """Extract branch name from git switch command (regex fallback)."""
    cmd = cmd.strip()
    m = re.match(r"^git\s+switch\s+(?:.*\s+)?--detach(?:\s+(\S+))?", cmd)
    if m:
        return m.group(1) or "<detached>"
    m = re.match(r"^git\s+switch\s+(?:.*\s+)?--orphan\s+(\S+)", cmd)
    if m:
        return m.group(1)
    m = re.match(r"^git\s+switch\s+(?:.*\s+)?-[cC]\s+(\S+)", cmd)
    if m:
        return m.group(1)
    m = re.match(r"^git\s+switch\s+(?:.*\s+)?--guess\s+(\S+)", cmd)
    if m:
        return m.group(1)
    parts = cmd.split()
    branches = [p for p in parts[2:] if not p.startswith("-")]
    if branches:
        return branches[-1]
    return None


def extract_target_branch_from_git_checkout(cmd: str) -> str | None:
    """Extract branch name from git checkout command (regex fallback)."""
    cmd = cmd.strip()
    m = re.match(r"^git\s+checkout\s+(?:.*\s+)?--detach(?:\s+(\S+))?", cmd)
    if m:
        return m.group(1) or "<detached>"
    m = re.match(r"^git\s+checkout\s+(?:.*\s+)?--orphan\s+(\S+)", cmd)
    if m:
        return m.group(1)
    m = re.match(r"^git\s+checkout\s+(?:.*\s+)?--ignore-other-worktrees\s+(\S+)", cmd)
    if m:
        return m.group(1)
    m = re.match(r"^git\s+checkout\s+(?:.*\s+)?-[bB]\s+(\S+)", cmd)
    if m:
        return m.group(1)
    parts = cmd.split()
    branches = [p for p in parts[2:] if not p.startswith("-")]
    if branches:
        return branches[-1]
    return None


def extract_target_branch_from_git_branch_rename(cmd: str) -> str | None:
    """Extract new branch name from git branch -m/-M."""
    m = re.match(r"^git\s+branch\s+-[mM]\s+(\S+)(?:\s+(\S+))?", cmd)
    if m:
        return m.group(2) if m.group(2) else m.group(1)
    return None


def extract_target_branch_from_gh_pr_checkout(cmd: str) -> str | None:
    """Extract PR ref from gh pr checkout."""
    m = re.match(r"^(?:gh|hub)\s+pr\s+checkout\s+(\S+)", cmd)
    if m:
        return f"pr/{m.group(1)}"
    return None


def has_inline_env_override(cmd: str) -> bool:
    """
    Return True if the Bash command string (tool_input.command) contains
    an inline env assignment for LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE.
    Inline env overrides are NOT allowed; only hook process env is.
    """
    pattern = r"(?:^|\s)LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE\s*=\s*\S+"
    return bool(re.search(pattern, cmd))


def is_manual_override_active() -> bool:
    """
    Return True only if both env vars are set in the hook process environment
    (NOT in the Bash command string).
    """
    allow = os.environ.get("LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE", "").strip()
    reason = os.environ.get("LOOP_LOCAL_ROOT_BRANCH_CHANGE_REASON", "").strip()
    return bool(allow) and bool(reason)


# ─── Main guard logic ─────────────────────────────────────────────────────────


def evaluate(
    command: str,
    cwd: str,
    hook_flavor: str = "cli",
    event_kind: str = "PreToolUse",
) -> dict[str, Any]:
    """
    Evaluate whether the command should be allowed or blocked.

    Returns a dict matching LOCAL_MAIN_BRANCH_GUARD_RESULT_V1.
    """
    cmd = command.strip()
    event_kind = event_kind or "PreToolUse"
    wrapper = None
    inner_argv_redacted: list[str] | None = None
    current_branch: str | None = None
    _fastpath_cache: dict[str, Any] | None | bool = False  # False = not computed yet

    def _compute_fastpath() -> dict[str, Any] | None:
        # Issue #1289: fastpath is a bounded telemetry enrichment only — it
        # NEVER influences status/reason_code and is best-effort (never raises).
        nonlocal _fastpath_cache
        if _fastpath_cache is not False:
            return _fastpath_cache  # type: ignore[return-value]
        if not _ensure_fastpath_imported():
            _fastpath_cache = None
            return None
        try:
            root = _resolve_project_root(cwd) or cwd
            result = _fastpath.classify(cmd, cwd, root)
            _fastpath_cache = result.to_telemetry_dict()
        except Exception:  # pragma: no cover - defensive fail-open telemetry
            _fastpath_cache = None
        return _fastpath_cache

    # Helper to emit canonicalized result dictionaries.
    def _emit(
        status: str,
        reason_code: str,
        target_branch: str | None,
        target_branch_kind: str | None,
        local_parser_stage: str,
        local_command_kind: str = COMMAND_KIND_UNKNOWN,
        local_rule_id: str | None = None,
        argv_tokens: list[str] | None = None,
        inner_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        res = _result(
            status=status,
            reason_code=reason_code,
            current_branch=current_branch,
            target_branch=target_branch,
            target_branch_kind=target_branch_kind,
            hook_flavor=hook_flavor,
            parser_stage=local_parser_stage,
            command_kind=local_command_kind,
            rule_id=local_rule_id,
            argv_redacted=argv_tokens,
            wrapper=wrapper,
            inner_argv_redacted=inner_tokens,
            event_kind=event_kind,
            decision_source=local_rule_id or reason_code,
        )
        res["fastpath"] = _compute_fastpath()
        return res

    # Step 1: Check if we are in local root context
    if not is_local_root_context(cwd):
        reason_code = (
            REASON_LINKED_ISSUE_WORKTREE_CONTEXT
            if is_linked_issue_worktree_context(cwd)
            else REASON_NOT_LOCAL_ROOT
        )
        return _emit(
            status="allow",
            reason_code=reason_code,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="context_check",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id=reason_code,
        )

    # Step 2: Resolve current branch
    current_branch = get_current_branch(cwd=cwd)
    default_branch = resolve_default_branch(cwd=cwd)

    # Step 3: Check for inline env override (block it explicitly)
    if has_inline_env_override(cmd):
        return _emit(
            status="block",
            reason_code=REASON_INLINE_OVERRIDE,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="inline_override",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="inline_env_override",
        )

    # Step 4: Check manual override (hook process env only)
    if is_manual_override_active():
        return _emit(
            status="allow",
            reason_code="manual_override_accepted",
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="manual_override",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="manual_override_accepted",
        )

    # Step 5: readonly pipeline classifier.
    # Must run before generic compound detection so `rg ... | head ...` can pass,
    # while mixed command / wrapper / redirection forms still fail closed.
    if classify_readonly_pipeline(cmd):
        return _emit(
            status="allow",
            reason_code=REASON_READONLY,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="readonly_pipeline",
            local_command_kind=COMMAND_KIND_PIPELINE,
            local_rule_id="readonly_pipeline",
        )

    # Step 5.5: readonly_artifact_export_command — gh issue view ... > tmp/<filename>
    # Must run before compound/metachar detection (Step 6) because `>` is a metachar.
    # Only the exact form `gh issue view <N> ... > tmp/<file>` is allowed.
    if is_readonly_artifact_export_command(cmd):
        return _emit(
            status="allow",
            reason_code=REASON_READONLY,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="readonly_artifact_export",
            local_command_kind=COMMAND_KIND_GITHUB_ARTIFACT_EXPORT,
            local_rule_id="gh_issue_view_to_tmp",
        )

    # Step 6: Compound/wrapped commands in local root context: fail-closed.
    # (Must check BEFORE readonly: "git status || git switch issue-*" starts with "git status")
    if is_compound_or_wrapped(cmd):
        return _emit(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="compound_or_wrapped",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="compound_wrapper_blocked",
        )

    # B3: Leading env assignments in local root context — fail-closed.
    # LOOP_DEFAULT_BRANCH=... or FOO=bar git switch ... are blocked.
    if _has_leading_env_assignment(cmd):
        return _emit(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="leading_env_assignment",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="leading_env_assignment",
        )

    # Step 7: shlex parse failure → fail-closed
    tokens = tokenize_command(cmd)
    if tokens is None:
        return _emit(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="tokenize",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="tokenize_failure",
        )

    # Step 8: Normalize git global options.
    # If fail-closed global options (-C, --git-dir, --work-tree, --config-env) present → block.
    normalized_tokens = tokens
    argv_redacted = _redact_argv(normalized_tokens)
    normalized_cmd = _rebuild_normalized_cmd(normalized_tokens)
    wrapper = None
    inner_argv_redacted = None

    # Step 8.5: rtk wrapper unwrapping (Issue #1198)
    if _is_rtk_command(normalized_tokens):
        wrapper = "rtk"
        inner_tokens = normalized_tokens[1:]
        inner_argv_redacted = _redact_argv(inner_tokens)
        if not inner_tokens:
            return _emit(
                status="block",
                reason_code=REASON_UNKNOWN_COMMAND,
                target_branch=None,
                target_branch_kind=None,
                local_parser_stage="rtk_empty",
                local_command_kind=COMMAND_KIND_RTK_WRAPPER,
                local_rule_id="rtk_empty",
                argv_tokens=argv_redacted,
                inner_tokens=inner_argv_redacted,
            )

        inner_command_name = inner_tokens[0]
        if inner_command_name in {"--help", "-h", "help"}:
            return _emit(
                status="allow",
                reason_code=REASON_RTK_HELP_COMMAND,
                target_branch=None,
                target_branch_kind=None,
                local_parser_stage="rtk_help",
                local_command_kind=COMMAND_KIND_RTK_WRAPPER,
                local_rule_id="rtk_help",
                argv_tokens=argv_redacted,
                inner_tokens=inner_argv_redacted,
            )

        if inner_command_name in {"gain", "session"}:
            return _emit(
                status="allow",
                reason_code=REASON_READONLY,
                target_branch=None,
                target_branch_kind=None,
                local_parser_stage="rtk_default_allow",
                local_command_kind=COMMAND_KIND_RTK_WRAPPER,
                local_rule_id=f"rtk_{inner_command_name}",
                argv_tokens=argv_redacted,
                inner_tokens=inner_argv_redacted,
            )

        if inner_command_name == "proxy":
            # rtk proxy は中間に複雑な委譲コマンドを内包し得るため、
            # allow/deny を安全側に寄せるため proxy 直下は review-required とする。
            return _emit(
                status="block",
                reason_code=REASON_RTK_PROXY,
                target_branch=None,
                target_branch_kind=None,
                local_parser_stage="rtk_proxy_requires_review",
                local_command_kind=COMMAND_KIND_RTK_WRAPPER,
                local_rule_id="rtk_proxy_requires_review",
                argv_tokens=argv_redacted,
                inner_tokens=_redact_argv(inner_tokens[1:]),
            )

        normalized_tokens = inner_tokens
        normalized_cmd = _rebuild_normalized_cmd(normalized_tokens)
        argv_redacted = _redact_argv(normalized_tokens)
        inner_argv_redacted = _redact_argv(normalized_tokens)
        bounded_rtk_git = classify_rtk_git_mutation(
            command=command,
            cwd=cwd,
            require_active_branch_push=False,
        )
        if bounded_rtk_git is not None:
            if bounded_rtk_git.status == "allow":
                return _emit(
                    status="block",
                    reason_code=REASON_UNKNOWN_COMMAND,
                    target_branch=None,
                    target_branch_kind=None,
                    local_parser_stage="rtk_git_root_denied",
                    local_command_kind=COMMAND_KIND_RTK_WRAPPER,
                    local_rule_id="rtk_git_root_denied",
                    argv_tokens=argv_redacted,
                    inner_tokens=inner_argv_redacted,
                )
            return _emit(
                status="block",
                reason_code=bounded_rtk_git.reason_code,
                target_branch=None,
                target_branch_kind=None,
                local_parser_stage="rtk_git_policy",
                local_command_kind=COMMAND_KIND_RTK_WRAPPER,
                local_rule_id=bounded_rtk_git.reason_code,
                argv_tokens=argv_redacted,
                inner_tokens=inner_argv_redacted,
            )
    git_global_fail_closed = False
    if normalized_tokens and normalized_tokens[0] == "git":
        normalized_tokens, git_global_fail_closed = _normalize_git_global_opts(normalized_tokens)
        if git_global_fail_closed:
            return _emit(
                status="block",
                reason_code=REASON_UNPARSEABLE,
                target_branch=None,
                target_branch_kind=None,
                local_parser_stage="git_global_opts",
                local_command_kind=COMMAND_KIND_UNKNOWN,
                local_rule_id="git_global_opts_failclosed",
            )

    # Rebuild normalized cmd string for pattern matching
    normalized_cmd = _rebuild_normalized_cmd(normalized_tokens)
    argv_redacted = _redact_argv(normalized_tokens)
    if wrapper == "rtk":
        inner_argv_redacted = _redact_argv(normalized_tokens)

    # Step 9: Read-only commands are always allowed (safe even in drifted/detached states)
    if is_readonly_command(normalized_cmd):
        return _emit(
            status="allow",
            reason_code=REASON_READONLY,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="readonly_classify",
            local_command_kind=COMMAND_KIND_GITHUB_DISPLAY
            if normalized_cmd.startswith("gh ")
            else COMMAND_KIND_READONLY,
            local_rule_id="readonly_command",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Step 9.5: Branch-safe maintenance commands remain allowed, but must not
    # reuse readonly telemetry or readonly pipeline classification.
    if is_branch_safe_maintenance_command(normalized_cmd):
        return _emit(
            status="allow",
            reason_code=REASON_BRANCH_SAFE_MAINTENANCE,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="branch_safe_maintenance",
            local_command_kind=COMMAND_KIND_READONLY,
            local_rule_id="branch_safe_maintenance",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Step 9.55: Cleanup-class commands (Issue #1137 arbitration). Exact
    # `git worktree remove <path>` / `git branch -d <branch>` are deferred to
    # worktree_scope_guard, which arbitrates them against the V3 one-shot contract.
    # Recognizing them before generic branch-mutation handling fixes the
    # arbitration order so the local root guard does not double-decide.
    if is_cleanup_class_command(normalized_cmd):
        return _emit(
            status="allow",
            reason_code=REASON_CLEANUP_DEFERRED,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="cleanup_deferred",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="cleanup_deferred",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Step 9.57: exact privileged skill runtime command class (Issue #1154).
    project_root = _resolve_project_root(cwd)
    if project_root and is_exact_skill_runtime_executor_command(normalized_cmd, cwd, project_root):
        return _emit(
            status="allow",
            reason_code=REASON_SKILL_RUNTIME_EXECUTOR,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="skill_runtime_exec",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="skill_runtime_executor",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )
    # Issue #1498: exact privileged skill runtime anchor command class
    # (`preflight.run.with_anchor`). Must be checked before the
    # `looks_like_skill_runtime_executor_command` block below, since that
    # block matches ANY command mentioning `skill_runtime_exec.py`.
    if project_root and is_exact_skill_runtime_anchor_executor_command(normalized_cmd, cwd, project_root):
        return _emit(
            status="allow",
            reason_code=REASON_SKILL_RUNTIME_EXECUTOR,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="skill_runtime_exec_anchor",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="skill_runtime_executor_anchor",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )
    if looks_like_skill_runtime_executor_command(normalized_cmd):
        return _emit(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="skill_runtime_like_block",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="skill_runtime_guess",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )
    if project_root and _wbe_exec_command(normalized_cmd, cwd, project_root):
        return _emit(
            status="allow",
            reason_code=REASON_WORKTREE_BOOTSTRAP_EXECUTOR,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="worktree_bootstrap_exec",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="worktree_bootstrap_executor",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )
    if _wbe_looks_like(normalized_cmd):
        return _emit(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="worktree_bootstrap_like_block",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="worktree_bootstrap_guess",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )
    if _looks_like_direct_issue_refinement_runtime_command(normalized_tokens, cwd, project_root):
        return _emit(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="issue_refinement_runtime_block",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="issue_refinement_direct",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Step 9.6: Tmp wrapper / python -c commands — fail-closed (AC14).
    if is_tmp_wrapper_or_python_c_command(normalized_cmd):
        return _emit(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="tmp_wrapper",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="tmp_wrapper_python",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Step 9.6b: github_issue_mutation_command — gh issue create with strict constraints.
    # --repo squne121/loop-protocol and --body-file tmp/... required; interactive flags blocked.
    if is_github_issue_mutation_command(normalized_cmd):
        return _emit(
            status="allow",
            reason_code=REASON_GITHUB_REMOTE_OPS,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="github_issue_mutation",
            local_command_kind=COMMAND_KIND_GITHUB_ISSUE_MUTATION,
            local_rule_id="github_issue_mutation_allow",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Step 9.65: gh api exact allowlist: only GET issue-comment endpoint by allowed slug.
    if _parse_gh_api_command(normalized_cmd):
        return _emit(
            status="allow",
            reason_code=REASON_GH_API,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="github_api_allow",
            local_command_kind=COMMAND_KIND_GITHUB_API,
            local_rule_id="gh_api_issue_comment_get",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Step 9.65b: gh api command without allowlist match is treated as review-required
    # (not as readonly or unparseable) to avoid silent blind bypass.
    if _is_gh_api_mutation_command(normalized_cmd):
        return _emit(
            status="block",
            reason_code=REASON_GH_API,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="github_api_block",
            local_command_kind=COMMAND_KIND_GITHUB_API,
            local_rule_id="gh_api_not_allowed",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Step 9.7a: GitHub remote ops (post-merge-cleanup minimum set) — allow with distinct reason_code.
    if is_github_remote_ops_command(normalized_cmd):
        return _emit(
            status="allow",
            reason_code=REASON_GITHUB_REMOTE_OPS,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="github_remote_ops",
            local_command_kind=COMMAND_KIND_GITHUB_PR_METADATA,
            local_rule_id="github_remote_ops_allow",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Step 9.7b: Gh mutation commands — fail-closed (AC11).
    if is_gh_mutation_command(normalized_cmd):
        return _emit(
            status="block",
            reason_code=REASON_GH_MUTATION,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="gh_mutation",
            local_command_kind=COMMAND_KIND_GITHUB_MUTATION,
            local_rule_id="github_mutation_denied",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Step 10: Classify root state.
    # B1 fix: detached HEAD (current_branch is None) is treated as detached_or_unknown,
    # NOT as default/allow. This must be checked BEFORE path restore allow (B3 fix).
    root_state = classify_root_state(current_branch, default_branch)
    already_drifted = root_state in ("drifted", "detached_or_unknown")

    # Step 11: Path restore commands — allowed only when on default branch.
    # B3 fix: drifted/detached_or_unknown roots must NOT allow path restore.
    # In drifted/detached state, only the explicit allowlist (_DRIFTED_ALLOWLIST_PATTERNS) applies.
    if is_path_restore_command(normalized_cmd) and root_state == "default":
        return _emit(
            status="allow",
            reason_code=REASON_READONLY,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="path_restore",
            local_command_kind=COMMAND_KIND_READONLY,
            local_rule_id="git_path_restore",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Step 12: Check if this is a branch mutation command
    is_branch_mutation = _is_branch_mutation_command(normalized_cmd, normalized_tokens)

    if is_branch_mutation:
        # Extract target branch using argv-based extraction (B6)
        target_branch = _extract_target_branch(normalized_cmd, normalized_tokens)
        target_kind = classify_branch(target_branch, default_branch) if target_branch else "unknown"

        # Allow recovery to default branch (always permitted regardless of root state)
        if target_branch == default_branch:
            return _emit(
                status="allow",
                reason_code=REASON_RECOVERY,
                target_branch=target_branch,
                target_branch_kind="default",
                local_parser_stage="branch_mutation",
                local_command_kind=COMMAND_KIND_GIT_BRANCH_MUTATION,
                local_rule_id="git_recovery_to_default",
                argv_tokens=argv_redacted,
                inner_tokens=inner_argv_redacted,
            )

        # Block drift to non-default branch
        if root_state == "detached_or_unknown":
            block_reason = REASON_DETACHED_OR_UNKNOWN
        elif already_drifted:
            block_reason = REASON_ALREADY_DRIFTED
        else:
            block_reason = REASON_DRIFT
        return _emit(
            status="block",
            reason_code=block_reason,
            target_branch=target_branch,
            target_branch_kind=target_kind,
            local_parser_stage="branch_mutation_block",
            local_command_kind=COMMAND_KIND_GIT_BRANCH_MUTATION,
            local_rule_id="git_branch_mutation_block",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Step 13: Drifted or detached root: use explicit allowlist (B1/B4)
    # Both "drifted" and "detached_or_unknown" share the same strict allowlist.
    if already_drifted:
        if not _is_allowed_when_drifted(normalized_cmd):
            block_reason = REASON_DETACHED_OR_UNKNOWN if root_state == "detached_or_unknown" else REASON_ALREADY_DRIFTED
            return _emit(
                status="block",
                reason_code=block_reason,
                target_branch=None,
                target_branch_kind=None,
                local_parser_stage="drift_allowlist_block",
                local_command_kind=COMMAND_KIND_UNKNOWN,
                local_rule_id="drift_allowlist_block",
                argv_tokens=argv_redacted,
                inner_tokens=inner_argv_redacted,
            )

    # Step 13.5: Deterministic checker commands on default branch — allowed with distinct reason_code (AC5/AC12).
    # NOTE: drifted/detached root is blocked by step 13 before reaching here.
    # deterministic_checker is intentionally NOT in the drifted allowlist; checker scripts should run from worktrees.
    if is_deterministic_checker_command(normalized_cmd, project_root):
        return _emit(
            status="allow",
            reason_code=REASON_DETERMINISTIC_CHECKER,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="deterministic_checker",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="deterministic_checker",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Step 13.6: Controlled skill mutation executor (Issue #1166 AC4/AC17).
    # Shared policy function — same is_controlled_skill_mutation_exec_command as
    # consumed by worktree_scope_guard. No split-brain allowlist.
    # Only allowed from default branch root (drifted root blocked by step 13 above).
    if _CSM_POLICY_AVAILABLE and _csm_exec_command(normalized_cmd, project_root or ""):
        return _emit(
            status="allow",
            reason_code=REASON_CONTROLLED_SKILL_MUTATION_EXECUTOR,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="controlled_skill_mutation",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="controlled_skill_mutation",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # rtk unknown inner command requires review rather than being treated as readonly.
    if wrapper == "rtk":
        return _emit(
            status="block",
            reason_code=REASON_UNKNOWN_COMMAND,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="rtk_unknown_inner",
            local_command_kind=COMMAND_KIND_RTK_WRAPPER,
            local_rule_id="rtk_unknown_inner",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Default: allow
    return _emit(
        status="allow",
        reason_code=REASON_UNKNOWN_ALLOWED,
        target_branch=None,
        target_branch_kind=None,
        local_parser_stage="final_classification",
        local_command_kind=COMMAND_KIND_UNKNOWN,
        local_rule_id="unknown_non_branch_command_allowed",
        argv_tokens=argv_redacted,
        inner_tokens=inner_argv_redacted,
    )


def _is_branch_mutation_command(cmd: str, tokens: list[str] | None = None) -> bool:
    """Return True if cmd is a branch mutation command.
    B2/B6: Works on normalized cmd (after git global option stripping).
    """
    if re.match(r"^git\s+switch\s+", cmd):
        return True
    if re.match(r"^git\s+checkout\s+", cmd):
        # Exclude path restore forms
        if is_path_restore_command(cmd):
            return False
        return True
    if re.match(r"^git\s+branch\s+-[mM]\s+", cmd):
        return True
    if re.match(r"^(?:gh|hub)\s+pr\s+checkout\s+", cmd):
        return True
    return False


def _extract_target_branch(cmd: str, tokens: list[str] | None = None) -> str | None:
    """Extract target branch from a branch mutation command.
    B6: Uses argv-based extraction when tokens are available.
    """
    if tokens and len(tokens) >= 2 and tokens[0] == "git":
        subcommand = tokens[1] if len(tokens) > 1 else ""
        if subcommand in ("switch", "checkout", "branch"):
            result = extract_target_branch_from_tokens(tokens, subcommand)
            if result is not None:
                return result
    # Fallback to regex
    if re.match(r"^git\s+switch\s+", cmd):
        return extract_target_branch_from_git_switch(cmd)
    if re.match(r"^git\s+checkout\s+", cmd):
        return extract_target_branch_from_git_checkout(cmd)
    if re.match(r"^git\s+branch\s+-[mM]\s+", cmd):
        return extract_target_branch_from_git_branch_rename(cmd)
    if re.match(r"^(?:gh|hub)\s+pr\s+checkout\s+", cmd):
        return extract_target_branch_from_gh_pr_checkout(cmd)
    return None


def _is_allowed_when_drifted(cmd: str) -> bool:
    """
    B4: Return True if command is in the explicit allowlist when root is already drifted.
    All other commands are blocked (allowlist-closed, not blocklist-closed).
    """
    for pattern in _DRIFTED_ALLOWLIST_PATTERNS:
        if re.match(pattern, cmd):
            return True
    return False


def _is_blocked_when_drifted(cmd: str) -> bool:
    """
    Legacy blocklist check — kept for backward compatibility with existing tests.
    In evaluate(), _is_allowed_when_drifted is used instead (B4 fix).
    """
    # Block mutating gh commands
    if re.match(r"^gh\s+issue\s+(edit|comment|close|reopen|delete|lock|unlock)(\s|$)", cmd):
        return True
    if re.match(r"^gh\s+pr\s+(checkout|edit|comment|merge|close|reopen|ready|review|update-branch)(\s|$)", cmd):
        return True
    if re.match(r"^hub\s+pr\s+checkout(\s|$)", cmd):
        return True
    # Block build/test runners
    if re.match(r"^(?:uv\s+run|pnpm|npm|yarn)\s+", cmd):
        return True
    return False


def _result(
    status: str,
    reason_code: str,
    current_branch: str | None,
    target_branch: str | None,
    target_branch_kind: str | None,
    hook_flavor: str,
    parser_stage: str | None = None,
    command_kind: str = COMMAND_KIND_UNKNOWN,
    rule_id: str | None = None,
    argv_redacted: list[str] | None = None,
    wrapper: str | None = None,
    inner_argv_redacted: list[str] | None = None,
    event_kind: str | None = None,
    decision_source: str | None = None,
) -> dict[str, Any]:
    decision = "allow" if status == "allow" else "block"
    if rule_id is None:
        rule_id = reason_code
    if decision_source is None:
        decision_source = rule_id
    if event_kind is None:
        event_kind = "PreToolUse"
    return {
        "decision": decision,
        "decision_source": decision_source,
        "status": status,
        "reason_code": reason_code,
        "current_branch": current_branch,
        "target_branch": target_branch,
        "target_branch_kind": target_branch_kind,
        "hook_name": "local_main_branch_guard",
        "event_kind": event_kind,
        "parser_stage": parser_stage or "classification",
        "command_kind": command_kind,
        "rule_id": rule_id,
        "argv_redacted": argv_redacted,
        "inner_argv_redacted": inner_argv_redacted,
        "wrapper": wrapper,
        "hook_flavor": hook_flavor,
    }


# ─── Hook entry point ─────────────────────────────────────────────────────────


def _classify_branch_kind(branch: str | None, default_branch: str) -> str:
    """
    Classify a branch name into a kind for safe stderr output.
    B5 fix: raw branch names MUST NOT appear in hook stderr (may contain sensitive identifiers).
    Returns one of: default | issue_like | pr_like | other | detached
    """
    if branch is None:
        return "detached"
    if branch == default_branch:
        return "default"
    if re.match(r"^(issue-|worktree-issue-)\d+", branch):
        return "issue_like"
    if re.match(r"^(pr/|pull/|pr-)\d+", branch):
        return "pr_like"
    return "other"


def run_hook(hook_flavor: str = "claude") -> int:
    """
    Entry point for hook mode.
    Reads hook JSON from stdin, evaluates, returns exit code.
    """
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Cannot parse stdin: fail-closed
        _emit_block_stderr(
            reason_code=REASON_UNPARSEABLE,
            current_branch_kind="unknown",
            current_is_default=False,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
            decision="block",
            event_kind="PreToolUse",
            command_kind=COMMAND_KIND_UNKNOWN,
            parser_stage="input_parse",
            rule_id="input_parse",
            argv_redacted=[],
        )
        return 2

    # Extract command from tool input
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}

    command = tool_input.get("command", "")
    if not isinstance(command, str):
        command = ""

    # Extract cwd from hook context or fall back to os.getcwd()
    cwd = data.get("cwd", "") or os.getcwd()
    hook_event = data.get("event")
    if not isinstance(hook_event, str):
        hook_event = data.get("hook_event_name")
        if not isinstance(hook_event, str):
            hook_event = "PreToolUse"

    if not command:
        # No command: allow (not a Bash tool call we care about)
        return 0

    result = evaluate(
        command=command,
        cwd=cwd,
        hook_flavor=hook_flavor,
        event_kind=hook_event,
    )

    if result["status"] == "block":
        # B5 fix: resolve default_branch for kind classification
        default_branch = resolve_default_branch(cwd=cwd)
        current_branch = result.get("current_branch")
        _emit_block_stderr(
            reason_code=result["reason_code"],
            current_branch_kind=_classify_branch_kind(current_branch, default_branch),
            current_is_default=(current_branch == default_branch),
            target_branch_kind=result.get("target_branch_kind"),
            hook_flavor=hook_flavor,
            decision=result["decision"],
            event_kind=result.get("event_kind", hook_event),
            command_kind=result.get("command_kind", COMMAND_KIND_UNKNOWN),
            parser_stage=result.get("parser_stage", "classification"),
            rule_id=result.get("rule_id"),
            argv_redacted=result.get("argv_redacted", []),
        )
        return 2

    return 0


def _emit_block_stderr(
    reason_code: str,
    current_branch_kind: str,
    current_is_default: bool,
    target_branch_kind: str | None,
    hook_flavor: str,
    *,
    decision: str = "block",
    event_kind: str = "PreToolUse",
    command_kind: str = COMMAND_KIND_UNKNOWN,
    parser_stage: str = "classification",
    rule_id: str | None = None,
    argv_redacted: list[str] | None = None,
) -> None:
    """
    Emit bounded, non-leaking block message to stderr (max 10 lines).

    B5 fix: raw branch names are NOT emitted. Only abstracted kinds are safe to output.
    - current_branch_kind: default | issue_like | pr_like | other | detached | unknown
    - current_is_default: bool (true only when on the default branch)
    - target_branch_kind: from evaluate() result (already abstracted by classify_branch())
    Raw branch names belong in --json / preflight diagnostic mode only.
    """
    hint_suggested = None
    hint_verify = None
    if reason_code == REASON_GH_MUTATION:
        hint_suggested = (
            "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py "
            "--command-id issue_body.update --issue-number <issue> "
            "--input-file artifacts/<issue>/issue-metadata/issue_body.update/input.json "
            "--repo squne121/loop-protocol --dry-run"
        )
    elif reason_code == REASON_UNPARSEABLE and rule_id == "issue_refinement_direct":
        hint_suggested = (
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run --issue-number <issue> "
            "--repo squne121/loop-protocol"
        )
    elif reason_code == REASON_UNPARSEABLE:
        hint_suggested = "git branch --show-current"
    elif reason_code == REASON_UNKNOWN_COMMAND:
        hint_suggested = "rtk git add <allowed-path-file>"
    elif reason_code == REASON_INLINE_OVERRIDE:
        hint_suggested = "git branch --show-current"
    repair_hint = build_hook_command_repair_hint(
        blocked_command_class=command_kind if command_kind != COMMAND_KIND_UNKNOWN else COMMAND_CLASS_RTK_GIT_UNKNOWN,
        reason_code=reason_code,
        suggested_command=hint_suggested,
        verification_command=hint_verify,
    )
    recovery = repair_hint["safe_action"]
    suggested_command = repair_hint["suggested_command"] or ""
    if suggested_command:
        recovery = f'{recovery}; approved path: "{suggested_command}"'
    lines = [
        "[local_main_branch_guard] blocked: local root checkout must stay on default branch",
        f"hook_name: local_main_branch_guard event_kind: {event_kind} decision: {decision}",
        f"reason_code: {reason_code} rule_id: {rule_id or '-'}",
        f"command_kind: {command_kind} parser_stage: {parser_stage}",
        (
            f"current_branch_kind: {current_branch_kind} "
            f"current_is_default: {str(current_is_default).lower()} "
            f"target_branch_kind: {target_branch_kind}"
        ),
        f"argv_redacted: {argv_redacted or []}",
        f"recovery: {recovery}",
        "HOOK_COMMAND_REPAIR_HINT_V1:",
        (
            f'  blocked_command_class: "{repair_hint["blocked_command_class"]}" '
            f'reason_code: "{repair_hint["reason_code"]}" '
            f'safe_action: "{repair_hint["safe_action"]}" '
            f'suggested_command: "{repair_hint["suggested_command"] or ""}"'
        ),
        (
            f'  forbidden_alternatives: {repair_hint["forbidden_alternatives"]} '
            f'verification_command: "{repair_hint["verification_command"] or ""}" '
            f'stop_condition: "{repair_hint["stop_condition"]}"'
        ),
    ]
    for line in lines[:10]:
        print(line, file=sys.stderr)


# ─── CLI / self-test entry point ──────────────────────────────────────────────


def run_cli() -> int:
    """CLI mode: evaluate a command and print LOCAL_MAIN_BRANCH_GUARD_RESULT_V1."""
    import argparse

    parser = argparse.ArgumentParser(description="Evaluate a command against the local_main_branch_guard")
    parser.add_argument("--command", required=True, help="Bash command to evaluate")
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory")
    parser.add_argument(
        "--flavor",
        default="cli",
        choices=["claude", "codex", "cli"],
        help="Hook flavor",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    result = evaluate(command=args.command, cwd=args.cwd, hook_flavor=args.flavor)

    if args.json:
        print(json.dumps({"LOCAL_MAIN_BRANCH_GUARD_RESULT_V1": result}, indent=2))
    else:
        import yaml  # type: ignore[import]

        print(yaml.dump({"LOCAL_MAIN_BRANCH_GUARD_RESULT_V1": result}, default_flow_style=False))

    return 0 if result["status"] == "allow" else 2


if __name__ == "__main__":
    # Detect hook mode vs CLI mode
    # Hook mode: no sys.argv args (invoked by shell hook wrapper)
    if len(sys.argv) == 1:
        flavor = os.environ.get("LOCAL_MAIN_BRANCH_GUARD_FLAVOR", "claude")
        sys.exit(run_hook(hook_flavor=flavor))
    else:
        sys.exit(run_cli())
