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


# ─── Reason codes ────────────────────────────────────────────────────────────

REASON_NOT_LOCAL_ROOT = "not_local_root_context"
REASON_READONLY = "readonly_command"
REASON_BRANCH_SAFE_MAINTENANCE = "branch_safe_maintenance_command"
REASON_RECOVERY = "recovery_to_default_branch"
REASON_DRIFT = "local_root_branch_drift"
REASON_ALREADY_DRIFTED = "already_drifted_root"
REASON_DETACHED_OR_UNKNOWN = "detached_or_unknown_root"
REASON_UNPARSEABLE = "unparseable_branch_mutation"
REASON_INLINE_OVERRIDE = "inline_env_override_not_allowed"
REASON_DETERMINISTIC_CHECKER = "deterministic_checker_command"


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

# Fd-duplication pattern: 2>&1 immediately before pipe
_FD_DUP_STDERR_STDOUT_RE = re.compile(r"\s+2>&1(\s*\|)")

# Leading env assignment pattern: NAME=value (possibly multiple)
_LEADING_ENV_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*=[^\s]*\s+)+")

# ─── Deterministic checker allowlist ─────────────────────────────────────────
# Exact-path allowlist for deterministic checker scripts.
# Wildcard patterns are prohibited. Add entries only for:
# - non-repo-state-mutating scripts
# - trusted entrypoints (not /tmp/, not python -c, not bash -lc)
DETERMINISTIC_CHECKER_ALLOWLIST = [
    ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
]

# ─── Gh issue/pr command pattern (allowlist-closed, AC11) ─────────────────────
# ANY gh issue/pr command not present in DISPLAY_READONLY_PATTERNS or GH_OPS_ALLOW_PATTERNS is blocked.
GH_ISSUE_PR_COMMAND_PATTERN = re.compile(r"^gh\s+(issue|pr)\s+")

# GitHub ops operations that are allowed in local root context.
# These are issue/PR metadata mutations that do NOT affect the local checkout state.
# gh pr merge / gh pr checkout are intentionally NOT here (they affect local branch).
GH_OPS_ALLOW_PATTERNS = [
    r"^gh\s+issue\s+close(\s|$)",
    r"^gh\s+issue\s+create(\s|$)",
    r"^gh\s+issue\s+edit(\s|$)",
    r"^gh\s+issue\s+comment(\s|$)",
    r"^gh\s+issue\s+reopen(\s|$)",
    r"^gh\s+pr\s+create(\s|$)",
    r"^gh\s+pr\s+comment(\s|$)",
    r"^gh\s+pr\s+edit(\s|$)",
]

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


def is_readonly_command(cmd: str) -> bool:
    """Return True if the command is a display-oriented read-only command."""
    cmd = cmd.strip()
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
        if script_path == allowed:
            return True
        if project_root:
            abs_allowed = os.path.join(project_root, allowed)
            if script_path == abs_allowed:
                return True
    return False


def is_gh_mutation_command(cmd: str) -> bool:
    """gh issue/pr コマンドで readonly allowlist および GH_OPS_ALLOW_PATTERNS 以外のものは fail-closed ブロック (allowlist-closed, AC11)。
    DISPLAY_READONLY_PATTERNS に含まれる gh issue view/list, gh pr view/list/status のみ通過。
    GH_OPS_ALLOW_PATTERNS に含まれる gh issue close/create/edit/comment/reopen,
    gh pr create/comment/edit は通過（GitHub ops として許可）。
    gh issue develop/transfer/pin/unpin, gh pr merge/checkout/revert/lock/unlock 等はすべてブロック。
    """
    cmd = cmd.strip()
    # Only applies to gh issue/pr subcommands
    if not GH_ISSUE_PR_COMMAND_PATTERN.match(cmd):
        return False
    # If in readonly allowlist, it is NOT a mutation
    for pattern in DISPLAY_READONLY_PATTERNS:
        if re.match(pattern, cmd):
            return False
    # If in GitHub ops allowlist, it is allowed (post-merge-cleanup etc.)
    for pattern in GH_OPS_ALLOW_PATTERNS:
        if re.match(pattern, cmd):
            return False
    # gh issue/pr not in any allowlist → treat as mutation → block
    return True


def is_tmp_wrapper_or_python_c_command(cmd: str) -> bool:
    """Return True if command is /tmp wrapper or python -c (fail-closed, AC14)."""
    cmd = cmd.strip()
    tokens = tokenize_command(cmd)
    if not tokens:
        return False
    # Block: python -c / python3 -c (inline code)
    if tokens[0] in ("python", "python3") and "-c" in tokens[1:]:
        return True
    # Block: uv run python3 /tmp/*.py
    if (len(tokens) >= 4 and tokens[:3] == ["uv", "run", "python3"]
            and tokens[3].startswith("/tmp/")):
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
) -> dict[str, Any]:
    """
    Evaluate whether the command should be allowed or blocked.

    Returns a dict matching LOCAL_MAIN_BRANCH_GUARD_RESULT_V1.
    """
    cmd = command.strip()

    # Step 1: Check if we are in local root context
    if not is_local_root_context(cwd):
        return _result(
            status="allow",
            reason_code=REASON_NOT_LOCAL_ROOT,
            current_branch=None,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # Step 2: Resolve current branch
    current_branch = get_current_branch(cwd=cwd)
    default_branch = resolve_default_branch(cwd=cwd)

    # Step 3: Check for inline env override (block it explicitly)
    if has_inline_env_override(cmd):
        return _result(
            status="block",
            reason_code=REASON_INLINE_OVERRIDE,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # Step 4: Check manual override (hook process env only)
    if is_manual_override_active():
        return _result(
            status="allow",
            reason_code="manual_override_accepted",
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # Step 5: readonly pipeline classifier.
    # Must run before generic compound detection so `rg ... | head ...` can pass,
    # while mixed command / wrapper / redirection forms still fail closed.
    if classify_readonly_pipeline(cmd):
        return _result(
            status="allow",
            reason_code=REASON_READONLY,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # Step 6: Compound/wrapped commands in local root context: fail-closed.
    # (Must check BEFORE readonly: "git status || git switch issue-*" starts with "git status")
    if is_compound_or_wrapped(cmd):
        return _result(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # B3: Leading env assignments in local root context — fail-closed.
    # LOOP_DEFAULT_BRANCH=... or FOO=bar git switch ... are blocked.
    if _has_leading_env_assignment(cmd):
        return _result(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # Step 7: shlex parse failure → fail-closed
    tokens = tokenize_command(cmd)
    if tokens is None:
        return _result(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # Step 8: Normalize git global options.
    # If fail-closed global options (-C, --git-dir, --work-tree, --config-env) present → block.
    normalized_tokens = tokens
    git_global_fail_closed = False
    if tokens and tokens[0] == "git":
        normalized_tokens, git_global_fail_closed = _normalize_git_global_opts(tokens)
        if git_global_fail_closed:
            return _result(
                status="block",
                reason_code=REASON_UNPARSEABLE,
                current_branch=current_branch,
                target_branch=None,
                target_branch_kind=None,
                hook_flavor=hook_flavor,
            )

    # Rebuild normalized cmd string for pattern matching
    normalized_cmd = _rebuild_normalized_cmd(normalized_tokens)

    # Step 9: Read-only commands are always allowed (safe even in drifted/detached states)
    if is_readonly_command(normalized_cmd):
        return _result(
            status="allow",
            reason_code=REASON_READONLY,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # Step 9.5: Branch-safe maintenance commands remain allowed, but must not
    # reuse readonly telemetry or readonly pipeline classification.
    if is_branch_safe_maintenance_command(normalized_cmd):
        return _result(
            status="allow",
            reason_code=REASON_BRANCH_SAFE_MAINTENANCE,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # Step 9.6: Tmp wrapper / python -c commands — fail-closed (AC14).
    if is_tmp_wrapper_or_python_c_command(normalized_cmd):
        return _result(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # Step 9.7: Gh mutation commands — fail-closed (AC11).
    if is_gh_mutation_command(normalized_cmd):
        return _result(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
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
        return _result(
            status="allow",
            reason_code=REASON_READONLY,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # Step 12: Check if this is a branch mutation command
    is_branch_mutation = _is_branch_mutation_command(normalized_cmd, normalized_tokens)

    if is_branch_mutation:
        # Extract target branch using argv-based extraction (B6)
        target_branch = _extract_target_branch(normalized_cmd, normalized_tokens)
        target_kind = classify_branch(target_branch, default_branch) if target_branch else "unknown"

        # Allow recovery to default branch (always permitted regardless of root state)
        if target_branch == default_branch:
            return _result(
                status="allow",
                reason_code=REASON_RECOVERY,
                current_branch=current_branch,
                target_branch=target_branch,
                target_branch_kind="default",
                hook_flavor=hook_flavor,
            )

        # Block drift to non-default branch
        if root_state == "detached_or_unknown":
            block_reason = REASON_DETACHED_OR_UNKNOWN
        elif already_drifted:
            block_reason = REASON_ALREADY_DRIFTED
        else:
            block_reason = REASON_DRIFT
        return _result(
            status="block",
            reason_code=block_reason,
            current_branch=current_branch,
            target_branch=target_branch,
            target_branch_kind=target_kind,
            hook_flavor=hook_flavor,
        )

    # Step 13: Drifted or detached root: use explicit allowlist (B1/B4)
    # Both "drifted" and "detached_or_unknown" share the same strict allowlist.
    if already_drifted:
        if not _is_allowed_when_drifted(normalized_cmd):
            block_reason = REASON_DETACHED_OR_UNKNOWN if root_state == "detached_or_unknown" else REASON_ALREADY_DRIFTED
            return _result(
                status="block",
                reason_code=block_reason,
                current_branch=current_branch,
                target_branch=None,
                target_branch_kind=None,
                hook_flavor=hook_flavor,
            )

    # Step 13.5: Deterministic checker commands on default branch — allowed with distinct reason_code (AC5/AC12).
    # NOTE: drifted/detached root is blocked by step 13 before reaching here.
    # deterministic_checker is intentionally NOT in the drifted allowlist; checker scripts should run from worktrees.
    project_root = _resolve_project_root(cwd)
    if is_deterministic_checker_command(normalized_cmd, project_root):
        return _result(
            status="allow",
            reason_code=REASON_DETERMINISTIC_CHECKER,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # Default: allow
    return _result(
        status="allow",
        reason_code=REASON_READONLY,
        current_branch=current_branch,
        target_branch=None,
        target_branch_kind=None,
        hook_flavor=hook_flavor,
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
) -> dict[str, Any]:
    return {
        "status": status,
        "reason_code": reason_code,
        "current_branch": current_branch,
        "target_branch": target_branch,
        "target_branch_kind": target_branch_kind,
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
    Reads PreToolUse JSON from stdin, evaluates, returns exit code.
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

    if not command:
        # No command: allow (not a Bash tool call we care about)
        return 0

    result = evaluate(command=command, cwd=cwd, hook_flavor=hook_flavor)

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
) -> None:
    """
    Emit bounded, non-leaking block message to stderr (max 10 lines).

    B5 fix: raw branch names are NOT emitted. Only abstracted kinds are safe to output.
    - current_branch_kind: default | issue_like | pr_like | other | detached | unknown
    - current_is_default: bool (true only when on the default branch)
    - target_branch_kind: from evaluate() result (already abstracted by classify_branch())
    Raw branch names belong in --json / preflight diagnostic mode only.
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
