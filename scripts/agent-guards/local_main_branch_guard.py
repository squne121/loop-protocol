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
from pathlib import Path
from typing import Any

_AGENT_GUARDS_DIR = Path(__file__).resolve().parent
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))

from skill_runtime_command_policy import (  # noqa: E402
    SKILL_RUNTIME_REASON_CODE,
    is_exact_skill_runtime_executor_command,
    looks_like_skill_runtime_executor_command,
)
from worktree_bootstrap_command_policy import (  # noqa: E402
    WORKTREE_BOOTSTRAP_REASON_CODE,
    is_exact_worktree_bootstrap_executor_command,
    looks_like_worktree_bootstrap_executor_command,
)


# ─── Reason codes ────────────────────────────────────────────────────────────

REASON_NOT_LOCAL_ROOT = "not_local_root_context"
REASON_READONLY = "readonly_command"
REASON_BRANCH_SAFE_MAINTENANCE = "branch_safe_maintenance_command"
REASON_RECOVERY = "recovery_to_default_branch"
REASON_DRIFT = "local_root_branch_drift"
REASON_ALREADY_DRIFTED = "already_drifted_root"
REASON_DETACHED_OR_UNKNOWN = "detached_or_unknown_root"
REASON_UNPARSEABLE = "unparseable_branch_mutation"
REASON_UNKNOWN_COMMAND = "unknown_command_requires_review"
REASON_GH_API = "github_api_command"
REASON_RTK_HELP_COMMAND = "rtk_help_command"
REASON_RTK_PROXY = "rtk_proxy_requires_review"
REASON_INLINE_OVERRIDE = "inline_env_override_not_allowed"
REASON_DETERMINISTIC_CHECKER = "deterministic_checker_command"
REASON_GITHUB_REMOTE_OPS = "github_remote_ops_command"
REASON_GH_MUTATION = "gh_mutation_denied"
REASON_SKILL_RUNTIME_EXECUTOR = SKILL_RUNTIME_REASON_CODE
REASON_WORKTREE_BOOTSTRAP_EXECUTOR = WORKTREE_BOOTSTRAP_REASON_CODE
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

# ─── GitHub remote ops classification vocabulary ──────────────────────────────
# 5-class vocabulary for gh command classification (Issue #1124).
# These constants are exported for test validation (AC15).
GITHUB_CMD_CLASS_DISPLAY_READONLY = "display_readonly_command"
GITHUB_CMD_CLASS_READONLY_EXPORT = "readonly_artifact_export_command"
GITHUB_CMD_CLASS_ISSUE_MUTATION = "github_issue_mutation_command"
GITHUB_CMD_CLASS_PR_METADATA = "github_pr_metadata_command"
GITHUB_CMD_CLASS_DESTRUCTIVE = "github_destructive_command"

# Trusted repository slug for gh issue create/edit mutation allowlist.
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
COMMAND_KIND_WORKTREE_BOOTSTRAP = "worktree_bootstrap_executor"


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
                return path_lines[0][len("worktree "):]
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
    "-C", "-c", "--git-dir", "--work-tree", "--config-env",
    "--namespace", "--super-prefix", "--exec-path",
}
_GIT_GLOBAL_FLAGS = {
    "--bare", "--no-pager", "--no-replace-objects", "--no-optional-locks",
    "--version", "--help", "--html-path", "--man-path", "--info-path",
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
DETERMINISTIC_CHECKER_ALLOWLIST: list[str] = []

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
    r"^git\s+checkout\s+--\s+\S",              # git checkout -- <path>
    r"^git\s+checkout\s+HEAD\s+--\s+\S",        # git checkout HEAD -- <path>
    r"^git\s+checkout\s+HEAD~?\d*\s+--\s+\S",  # git checkout HEAD~N -- <path>
    r"^git\s+checkout\s+--pathspec-from-file=", # --pathspec-from-file=
    r"^git\s+checkout\s+-p(\s|$)",              # git checkout -p [-- path]
    r"^git\s+checkout\s+-p\s+--\s+",            # git checkout -p -- <path>
    r"^git\s+restore(\s|$)",                    # git restore <path>
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
        "-f",
        "-F",
        "--field",
        "--raw-field",
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
    sensitive_flags = {"--body-file", "--body", "--input", "--field", "--raw-field"}
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

        if token in {"-f", "-F", "--field", "--input", "--raw-field"}:
            return False
        if token.startswith(("--field=", "--raw-field=")):
            return False

        if token.startswith("-"):
            i += 1
            continue

        endpoint = token
        i += 1
        break

    if method != "GET":
        return False
    if endpoint is None:
        return False

    return bool(_GH_API_ENDPOINT_RE.match(endpoint))


def _is_gh_api_mutation_command(cmd: str) -> bool:
    tokens = tokenize_command(cmd)
    return bool(tokens and len(tokens) >= 2 and tokens[0] == "gh" and tokens[1] == "api")


def _looks_like_direct_issue_refinement_runtime_command(cmd: str) -> bool:
    """
    Fail-closed detector for direct `uv run python3 .claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py ...`
    and similar future lexical variants. The exact allowlist is ONLY skill_runtime_exec.py;
    direct invocation must not silently pass through root.
    """
    return bool(
        re.search(
            r"(^|\s)(?:uv\s+run\s+)?python\d*\s+"
            r"(?:\.claude/skills/issue-refinement-loop/scripts/run_refinement_preflight\.py|"
            r"scripts/issue_refinement_loop/run_refinement_preflight\.py)(\s|$)",
            cmd,
        )
    )


def is_tmp_wrapper_or_python_c_command(cmd: str) -> bool:
    """
    Return True for unparseable tmp wrapper or python -c / inline script launch forms.
    AC14: fail-closed on wrappers that hide the actual mutating command.

    Examples blocked:
      python -c "..."
      python3 -c "..."
      /tmp/foo.py
      python /tmp/bar.py
      uv run python /tmp/bar.py
    """
    cmd = cmd.strip()
    if re.match(r"^(?:uv\s+run\s+)?python\d*\s+-c(\s|$)", cmd):
        return True
    if re.match(r"^(?:uv\s+run\s+)?python\d*\s+/tmp/\S+", cmd):
        return True
    if re.match(r"^/tmp/\S+", cmd):
        return True
    return False


def _extract_wrapper_and_inner(normalized_tokens: list[str], cwd: str) -> tuple[str | None, str]:
    """
    If command is a recognized wrapper, return (wrapper_name, normalized_inner_command).
    Otherwise return (None, normalized outer command).

    Supports:
      - rtk <subcommand...>
      - uv run <command...>
      - bash -lc '<command>'  (exact only; shell metachar already blocked before this)
    """
    if not normalized_tokens:
        return None, ""
    if normalized_tokens[0] == "rtk" and len(normalized_tokens) >= 2:
        return "rtk", " ".join(normalized_tokens[1:])
    if normalized_tokens[:2] == ["uv", "run"] and len(normalized_tokens) >= 3:
        return "uv_run", " ".join(normalized_tokens[2:])
    if normalized_tokens[:2] == ["bash", "-lc"] and len(normalized_tokens) == 3:
        inner = normalized_tokens[2]
        if _has_shell_metachar(inner):
            return "bash_lc", ""
        return "bash_lc", inner
    return None, " ".join(normalized_tokens)


def extract_target_branch_from_git_switch(cmd: str) -> str | None:
    """Extract target branch from `git switch ...` command."""
    tokens = tokenize_command(cmd)
    if not tokens or len(tokens) < 3:
        return None
    # tokens[0] = git, tokens[1] = switch
    return extract_target_branch_from_tokens(tokens, "switch")


def extract_target_branch_from_git_checkout(cmd: str) -> str | None:
    """Extract target branch from `git checkout ...` command."""
    tokens = tokenize_command(cmd)
    if not tokens or len(tokens) < 3:
        return None
    # tokens[0] = git, tokens[1] = checkout
    return extract_target_branch_from_tokens(tokens, "checkout")


def extract_target_branch_from_git_branch_rename(cmd: str) -> str | None:
    """Extract target branch from `git branch -m/-M ...` command."""
    tokens = tokenize_command(cmd)
    if not tokens or len(tokens) < 4:
        return None
    # git branch -m old new
    if len(tokens) >= 5:
        return tokens[4]
    # git branch -m new  (rename current branch)
    return tokens[3]


def extract_target_branch_from_gh_pr_checkout(cmd: str) -> str | None:
    """Extract target branch from gh/hub pr checkout command."""
    tokens = tokenize_command(cmd)
    if not tokens or len(tokens) < 4:
        return None
    # gh pr checkout 123  -> implicit target becomes pr/123
    pr_number = tokens[3]
    if pr_number.isdigit():
        return f"pr/{pr_number}"
    return pr_number


def extract_target_branch_from_tokens(tokens: list[str], subcommand: str) -> str | None:
    """
    Extract target branch from argv tokens for git switch/checkout.
    B6: quote-safe, argv-based extraction.

    Supported forms:
      git switch issue-123
      git switch -c issue-123
      git switch -C issue-123
      git switch --create issue-123
      git switch --force-create issue-123
      git switch -d issue-123            -> detach to branch name (mutation)
      git switch --detach issue-123
      git switch --orphan issue-123
      git switch --track origin/issue-123
      git switch --track=inherit origin/issue-123
      git switch --guess issue-123
      git switch --no-guess issue-123
      git switch -- conflict sentinel
      git checkout issue-123
      git checkout -b issue-123
      git checkout -B issue-123
      git checkout --track origin/issue-123
      git checkout --detach issue-123
      git checkout --orphan issue-123

    Path restore forms are excluded upstream by is_path_restore_command().
    """
    if len(tokens) < 3 or tokens[0] != "git" or tokens[1] != subcommand:
        return None

    # Cursor over args after subcommand
    args = tokens[2:]
    i = 0
    while i < len(args):
        tok = args[i]

        # Conflict sentinel: `git switch -- <path>` / `git checkout -- <path>`
        if tok == "--":
            return None

        # Options that take a target branch argument immediately after them
        if tok in {"-c", "-C", "-b", "-B", "--create", "--force-create", "--orphan"}:
            if i + 1 < len(args):
                return args[i + 1]
            return None

        # --detach can be bare or followed by branch-ish
        if tok in {"-d", "--detach"}:
            if i + 1 < len(args) and args[i + 1] != "--":
                return args[i + 1]
            return None

        # Track forms
        if tok == "--track":
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                return args[i + 1].split("/", 1)[-1]
            return None
        if tok.startswith("--track="):
            # --track=inherit still needs the next positional branch-ish
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                return args[i + 1].split("/", 1)[-1]
            return None

        # Flags that do not take a value
        if tok in {
            "--guess", "--no-guess", "--progress", "--quiet", "-q",
            "--discard-changes", "--merge", "--conflict=merge",
        }:
            i += 1
            continue

        # Unknown option: if it starts with '-' skip it conservatively
        if tok.startswith("-"):
            i += 1
            continue

        # First positional non-option = target branch / rev
        return tok

        i += 1

    return None


def is_readonly_command(cmd: str) -> bool:
    """Return True if cmd is an allowed read-only/display command."""
    for pattern in DISPLAY_READONLY_PATTERNS:
        if re.match(pattern, cmd):
            return True
    return False


def is_branch_safe_maintenance_command(cmd: str) -> bool:
    """
    Return True for exact branch-safe maintenance commands that may mutate local metadata
    but do NOT move the local root checkout away from the default branch.

    Issue #1075: `git fetch` and `git worktree prune` must classify to a distinct
    reason_code (`branch_safe_maintenance_command`) rather than generic readonly.
    """
    for pattern in BRANCH_SAFE_MAINTENANCE_PATTERNS:
        if re.match(pattern, cmd):
            return True
    return False


def is_readonly_artifact_export_command(cmd: str) -> bool:
    """
    Return True for exact readonly artifact-export commands.

    Only fixed allowlisted forms are permitted. These commands may write to local files
    but do NOT mutate GitHub state. Issue #1124 AC1/AC5/AC13.
    """
    artifact_patterns = [
        # gh pr diff / gh issue view piped to tee, with optional stdout redirection omitted.
        r"^gh\s+pr\s+diff\s+\d+\s+\|\s+tee\s+\S+$",
        r"^gh\s+issue\s+view\s+\d+\s+\|\s+tee\s+\S+$",
        # gh api GET exact issue-comment endpoint piped to tee
        r"^gh\s+api(?:\s+--method\s+GET|\s+--method=GET)?\s+repos/squne121/loop-protocol/issues/comments/\d+\s+\|\s+tee\s+\S+$",
    ]
    return any(re.match(p, cmd) for p in artifact_patterns)


def is_github_issue_mutation_command(cmd: str) -> bool:
    """
    Return True for strict shared allowlist of GitHub issue mutation commands.

    Allowed (exact-shape family only):
      gh issue create --repo squne121/loop-protocol --title ... --body-file tmp/...
      gh issue edit <n> --repo squne121/loop-protocol --body-file tmp/...

    Constraints:
      - must target TRUSTED_REPO_SLUG via explicit --repo
      - must use --body-file tmp/... (not --body / stdin / env)
      - interactive/editor/web flags are forbidden
      - only gh issue create/edit (no comment/close/reopen/etc.)
    """
    tokens = tokenize_command(cmd)
    if not tokens or len(tokens) < 3:
        return False
    if tokens[0] != "gh" or tokens[1] != "issue":
        return False
    if tokens[2] not in {"create", "edit"}:
        return False

    # Reject shell metachar / compound handled earlier, but be defensive here too.
    if _has_shell_metachar(cmd):
        return False

    # Reject interactive or alternate-output flags
    forbidden_flags = {
        "--body", "--editor", "--web", "--comment", "--recover", "--template",
        "--json", "--jq", "--label", "--assignee", "--milestone", "--project",
    }

    repo_value: str | None = None
    body_file: str | None = None
    i = 3
    while i < len(tokens):
        tok = tokens[i]
        if tok in forbidden_flags:
            return False
        if tok.startswith(("--body=", "--editor=", "--web=", "--json=", "--jq=")):
            return False
        if tok == "--repo":
            if i + 1 >= len(tokens):
                return False
            repo_value = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("--repo="):
            repo_value = tok.split("=", 1)[1]
            i += 1
            continue
        if tok == "--body-file":
            if i + 1 >= len(tokens):
                return False
            body_file = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("--body-file="):
            body_file = tok.split("=", 1)[1]
            i += 1
            continue
        # Allow issue number positional for edit, title value for create via --title, and other benign flags.
        i += 1

    if repo_value != TRUSTED_REPO_SLUG:
        return False
    if body_file is None:
        return False
    if not (body_file == "tmp" or body_file.startswith("tmp/")):
        return False
    return True


def _classify_gh_command(cmd: str) -> str | None:
    """
    Classify gh issue/pr command into 5 classes (Issue #1124 AC15).

    Returns one of:
      - display_readonly_command
      - readonly_artifact_export_command
      - github_issue_mutation_command
      - github_pr_metadata_command
      - github_destructive_command
      - None (not a gh issue/pr command)
    """
    if not GH_ISSUE_PR_COMMAND_PATTERN.match(cmd):
        return None

    if is_readonly_artifact_export_command(cmd):
        return GITHUB_CMD_CLASS_READONLY_EXPORT
    if is_github_issue_mutation_command(cmd):
        return GITHUB_CMD_CLASS_ISSUE_MUTATION
    if is_github_remote_ops_command(cmd):
        return GITHUB_CMD_CLASS_PR_METADATA

    # display_readonly_command is an exact allowlist (no bare issue/pr catch-all).
    readonly_patterns = [
        r"^gh\s+issue\s+(view|list)(\s|$)",
        r"^gh\s+pr\s+(view|list|status)(\s|$)",
    ]
    if any(re.match(p, cmd) for p in readonly_patterns):
        return GITHUB_CMD_CLASS_DISPLAY_READONLY

    return GITHUB_CMD_CLASS_DESTRUCTIVE


def is_github_remote_ops_command(cmd: str) -> bool:
    """
    Return True for exact post-merge-cleanup GitHub remote operations.

    Allowed minimum set (Issue #1124 / post-merge-cleanup):
      - gh pr create --draft --base <branch> --head <branch> --title <...> --body-file tmp/...
      - gh pr edit <num> --body-file tmp/...
      - gh issue comment <num> --body-file tmp/...
      - gh pr view <num> --json ...
      - gh pr checks <num>
      - gh pr merge <num> --squash --delete-branch
      - gh issue close <num>

    Commands outside this exact allowlist are NOT considered remote ops.
    """
    # PR metadata / managed workflow operations (minimal exact-shape allowlist)
    patterns = [
        r"^gh\s+pr\s+create\s+.*--draft(\s|$).*(--body-file\s+tmp/\S+|--body-file=tmp/\S+).*$",
        r"^gh\s+pr\s+edit\s+\d+\s+.*(--body-file\s+tmp/\S+|--body-file=tmp/\S+).*$",
        r"^gh\s+issue\s+comment\s+\d+\s+.*(--body-file\s+tmp/\S+|--body-file=tmp/\S+).*$",
        r"^gh\s+pr\s+view\s+\d+\s+.*--json(\s|=).*$",
        r"^gh\s+pr\s+checks\s+\d+(\s|$)",
        r"^gh\s+pr\s+merge\s+\d+\s+.*--squash(\s|$).*(--delete-branch)(\s|$).*$",
        r"^gh\s+issue\s+close\s+\d+(\s|$)",
    ]
    return any(re.match(p, cmd) for p in patterns)


def is_gh_mutation_command(cmd: str) -> bool:
    """
    Return True if cmd is a gh/hub mutation command that should be fail-closed.
    Issue #1124 AC11: any gh issue/pr command not in readonly or remote-ops allowlist
    is treated as destructive/mutating and blocked in local root.
    """
    cls = _classify_gh_command(cmd)
    return cls == GITHUB_CMD_CLASS_DESTRUCTIVE


def is_path_restore_command(cmd: str) -> bool:
    """Return True if cmd is a git checkout/restore path operation (not branch switch)."""
    for pattern in CHECKOUT_PATH_RESTORE_PATTERNS:
        if re.match(pattern, cmd):
            return True
    return False


def is_compound_or_wrapped(cmd: str) -> bool:
    """Return True if cmd is a compound shell command or uses wrappers.
    B6: uses shlex tokenization and shell metachar detection.
    """
    if _has_shell_metachar(cmd):
        return True
    tokens = tokenize_command(cmd)
    if not tokens or not tokens:
        return False
    # Wrapped commands are considered compound-like except exact allowlisted uv/rtk forms handled in evaluate()
    if tokens[0] in ("bash", "sh"):
        return True
    return False


def has_inline_env_override(cmd: str) -> bool:
    """
    Return True if cmd contains inline LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE=1 style override.
    B3: detect both leading and inline env assignments for fail-closed handling.
    """
    # Leading env assignments: LOOP_ALLOW_*=...
    if re.match(r"^LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE=", cmd):
        return True
    # Inline env after wrapper? e.g. env LOOP_ALLOW...=1 git switch issue
    if re.search(r"(^|\s)LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE=", cmd):
        return True
    if re.search(r"(^|\s)LOOP_LOCAL_ROOT_BRANCH_CHANGE_REASON=", cmd):
        return True
    return False


def is_manual_override_active() -> bool:
    """
    Return True if BOTH required env vars are set in the process environment:
      - LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE=1
      - LOOP_LOCAL_ROOT_BRANCH_CHANGE_REASON=<non-empty>

    This helper is intentionally NOT consulted by evaluate(); inline or ambient env
    must not silently bypass the guard. The override is only honored by human-reviewed
    wrapper flows outside this pure evaluator.
    """
    return (
        os.environ.get("LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE") == "1"
        and bool(os.environ.get("LOOP_LOCAL_ROOT_BRANCH_CHANGE_REASON", "").strip())
    )


# ─── Core evaluator ──────────────────────────────────────────────────────────

def evaluate(
    command: str,
    cwd: str,
    hook_flavor: str = "claude",
    event_kind: str = "PreToolUse",
) -> dict[str, Any]:
    """
    Evaluate a command and return LOCAL_MAIN_BRANCH_GUARD_RESULT_V1.

    B1/B2/B3/B4/B5/B6 fixes included.
    """
    current_branch = get_current_branch(cwd=cwd)
    default_branch = resolve_default_branch(cwd=cwd)
    event_kind = event_kind or "PreToolUse"

    if not is_local_root_context(cwd):
        return _result(
            status="allow",
            reason_code=REASON_NOT_LOCAL_ROOT,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
            parser_stage="context",
            command_kind=COMMAND_KIND_UNKNOWN,
            rule_id="context_not_local_root",
            event_kind=event_kind,
            decision_source="context",
        )

    # Step 1: inline/ambient override not honored here (pure evaluator fail-closed)
    if has_inline_env_override(command) or _has_leading_env_assignment(command):
        return _result(
            status="block",
            reason_code=REASON_INLINE_OVERRIDE,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
            parser_stage="inline_override",
            command_kind=COMMAND_KIND_UNKNOWN,
            rule_id="inline_override",
            event_kind=event_kind,
            decision_source="inline_override",
        )

    # Step 2: tokenize (B6)
    tokens = tokenize_command(command)
    if tokens is None:
        return _result(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
            parser_stage="tokenize",
            command_kind=COMMAND_KIND_UNKNOWN,
            rule_id="tokenize_error",
            event_kind=event_kind,
            decision_source="tokenize",
        )

    # Step 3: fd-dup readonly pipeline allow (AC10)
    # Allow exact forms like `git diff --stat 2>&1 | head -n 20`
    if _FD_DUP_STDERR_STDOUT_RE.search(command):
        # Must be a 2-segment pipeline: readonly cmd | head/tail/wc
        parts = [p.strip() for p in command.split("|")]
        if len(parts) == 2:
            left = re.sub(r"\s+2>&1\s*$", "", parts[0]).strip()
            right = parts[1].strip()
            if is_readonly_command(left) and any(re.match(p, right) for p in READONLY_PIPELINE_SEGMENT_PATTERNS):
                return _result(
                    status="allow",
                    reason_code=REASON_READONLY,
                    current_branch=current_branch,
                    target_branch=None,
                    target_branch_kind=None,
                    hook_flavor=hook_flavor,
                    parser_stage="fd_dup_readonly_pipeline",
                    command_kind=COMMAND_KIND_PIPELINE,
                    rule_id="fd_dup_readonly_pipeline",
                    event_kind=event_kind,
                    decision_source="readonly_pipeline",
                )
        return _result(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
            parser_stage="fd_dup_unparseable",
            command_kind=COMMAND_KIND_PIPELINE,
            rule_id="fd_dup_unparseable",
            event_kind=event_kind,
            decision_source="fd_dup",
        )

    # Step 4: shell metachar / wrappers — fail-closed except exact allowlisted readonly pipelines later
    if _has_shell_metachar(command):
        # Exact readonly pipeline allow: <readonly> | head/tail/wc
        parts = [p.strip() for p in command.split("|")]
        if len(parts) == 2 and is_readonly_command(parts[0]) and any(re.match(p, parts[1]) for p in READONLY_PIPELINE_SEGMENT_PATTERNS):
            return _result(
                status="allow",
                reason_code=REASON_READONLY,
                current_branch=current_branch,
                target_branch=None,
                target_branch_kind=None,
                hook_flavor=hook_flavor,
                parser_stage="readonly_pipeline",
                command_kind=COMMAND_KIND_PIPELINE,
                rule_id="readonly_pipeline",
                event_kind=event_kind,
                decision_source="readonly_pipeline",
            )
        if is_readonly_artifact_export_command(command):
            return _result(
                status="allow",
                reason_code=REASON_READONLY,
                current_branch=current_branch,
                target_branch=None,
                target_branch_kind=None,
                hook_flavor=hook_flavor,
                parser_stage="readonly_artifact_export",
                command_kind=COMMAND_KIND_GITHUB_ARTIFACT_EXPORT,
                rule_id="readonly_artifact_export",
                event_kind=event_kind,
                decision_source="readonly_artifact_export",
            )
        return _result(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
            parser_stage="compound_or_wrapped",
            command_kind=COMMAND_KIND_UNKNOWN,
            rule_id="compound_wrapper_blocked",
            event_kind=event_kind,
            decision_source="compound_or_wrapped",
        )

    # Step 5: normalize wrappers / git global opts
    wrapper, inner_command = _extract_wrapper_and_inner(tokens, cwd)
    normalized_cmd = inner_command
    normalized_tokens = tokenize_command(normalized_cmd)
    if normalized_tokens is None:
        return _result(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
            parser_stage="inner_tokenize",
            command_kind=COMMAND_KIND_UNKNOWN,
            rule_id="inner_tokenize_error",
            event_kind=event_kind,
            decision_source="inner_tokenize",
        )

    if normalized_tokens and normalized_tokens[0] == "git":
        normalized_tokens, fail_closed = _normalize_git_global_opts(normalized_tokens)
        normalized_cmd = " ".join(normalized_tokens)
        if fail_closed:
            return _result(
                status="block",
                reason_code=REASON_UNPARSEABLE,
                current_branch=current_branch,
                target_branch=None,
                target_branch_kind=None,
                hook_flavor=hook_flavor,
                parser_stage="git_global_opts_failclosed",
                command_kind=COMMAND_KIND_GIT_BRANCH_MUTATION,
                rule_id="git_global_opts_failclosed",
                event_kind=event_kind,
                decision_source="git_global_opts",
            )

    argv_redacted = _redact_argv(tokens)
    inner_argv_redacted = _redact_argv(normalized_tokens)

    def _emit(
        *,
        status: str,
        reason_code: str,
        target_branch: str | None,
        target_branch_kind: str | None,
        local_parser_stage: str,
        local_command_kind: str,
        local_rule_id: str,
        argv_tokens: list[str] | None = None,
        inner_tokens: list[str] | None = None,
    ) -> dict[str, Any]:
        return _result(
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
            decision_source=local_rule_id,
        )

    # Step 6: Exact readonly and maintenance allow
    if is_readonly_command(normalized_cmd):
        return _emit(
            status="allow",
            reason_code=REASON_READONLY,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="readonly",
            local_command_kind=COMMAND_KIND_READONLY,
            local_rule_id="readonly_command",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

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

    # Cleanup arbitration deferral (Issue #1137): do NOT decide cleanup tuples here.
    # Exact tuple safety / LOOP_V3 ownership is canonically enforced by worktree_scope_guard.
    if looks_like_cleanup_contract_command(normalized_cmd):
        return _emit(
            status="allow",
            reason_code=REASON_CLEANUP_DEFERRED,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="cleanup_contract_defer",
            local_command_kind=COMMAND_KIND_UNKNOWN,
            local_rule_id="cleanup_contract_defer",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )

    # Step 7: Exact helper / remote-ops / gh-api allowlists
    project_root = _resolve_project_root(cwd)
    if wrapper == "rtk" and normalized_cmd in {"--help", "help"}:
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
    if project_root and is_exact_worktree_bootstrap_executor_command(normalized_cmd, cwd, project_root):
        return _emit(
            status="allow",
            reason_code=REASON_WORKTREE_BOOTSTRAP_EXECUTOR,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="worktree_bootstrap_exec",
            local_command_kind=COMMAND_KIND_WORKTREE_BOOTSTRAP,
            local_rule_id="worktree_bootstrap_executor",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )
    if looks_like_worktree_bootstrap_executor_command(normalized_cmd):
        return _emit(
            status="block",
            reason_code=REASON_UNKNOWN_COMMAND,
            target_branch=None,
            target_branch_kind=None,
            local_parser_stage="worktree_bootstrap_guess_block",
            local_command_kind=COMMAND_KIND_WORKTREE_BOOTSTRAP,
            local_rule_id="worktree_bootstrap_guess",
            argv_tokens=argv_redacted,
            inner_tokens=inner_argv_redacted,
        )
    if _looks_like_direct_issue_refinement_runtime_command(normalized_cmd):
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

    # Step 9.6b: github_issue_mutation_command — gh issue create/edit with strict constraints.
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
            reason_code=REASON_DETERMINISTIC_CHECKER,
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
        reason_code=REASON_UNKNOWN_COMMAND,
        target_branch=None,
        target_branch_kind=None,
        local_parser_stage="final_classification",
        local_command_kind=COMMAND_KIND_UNKNOWN,
        local_rule_id="default_unknown_allow",
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
        )
        return 2

    return 0


def _emit_block_stderr(
    reason_code: str,
    current_branch_kind: str,
    current_is_default: bool,
    target_branch_kind: str | None,
    hook_flavor: str,
    event_kind: str | None = None,
    **_compat_ignored: object,
) -> None:
    """
    Emit bounded, non-leaking block message to stderr (max 10 lines).

    B5 fix: raw branch names are NOT emitted. Only abstracted kinds are safe to output.
    - current_branch_kind: default | issue_like | pr_like | other | detached | unknown
    - current_is_default: bool (true only when on the default branch)
    - target_branch_kind: from evaluate() result (already abstracted by classify_branch())
    Raw branch names belong in --json / preflight diagnostic mode only.

    event_kind and any extra compatibility kwargs are accepted for
    backward-compatible test/helper call sites but are intentionally not
    rendered into stderr.
    """
    lines = [
        "[local_main_branch_guard] blocked: local root checkout must stay on default branch",
        f"reason_code: {reason_code}",
        f"current_branch_kind: {current_branch_kind}",
        f"current_is_default: {str(current_is_default).lower()}",
    ]
    if target_branch_kind:
        lines.append(f"target_branch_kind: {target_branch_kind}")

    if reason_code == REASON_INLINE_OVERRIDE:
        lines.append("recovery: set LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE and LOOP_LOCAL_ROOT_BRANCH_CHANGE_REASON in CLI env before launch")
    elif reason_code in (REASON_ALREADY_DRIFTED, REASON_DETACHED_OR_UNKNOWN):
        lines.append("recovery: switch root back to default branch first")
    elif reason_code == REASON_UNPARSEABLE:
        lines.append("recovery: use simple (non-compound) git commands from local root")
    elif reason_code == REASON_DRIFT:
        lines.append("recovery: create/enter linked issue worktree, or switch only to default branch")
    elif reason_code == REASON_GH_MUTATION:
        lines.append("recovery: use the approved rtk/skill wrapper or run GitHub mutation from the designated issue workflow")

    lines.append(f"hook_flavor: {hook_flavor}")

    # Enforce max 10 lines
    for line in lines[:10]:
        print(line, file=sys.stderr)


# ─── CLI / self-test entry point ──────────────────────────────────────────────

def run_cli() -> int:
    """CLI mode: evaluate a command and print LOCAL_MAIN_BRANCH_GUARD_RESULT_V1."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Evaluate a command against the local_main_branch_guard"
    )
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
