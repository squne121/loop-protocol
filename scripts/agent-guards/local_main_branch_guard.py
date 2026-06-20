#!/usr/bin/env python3
"""
local_main_branch_guard.py

Shared core logic for the local root checkout branch drift guard.
Used by both Claude Code (.claude/hooks/local_main_branch_guard.sh)
and Codex CLI (.codex/hooks/local_main_branch_guard.sh) wrappers.

Guard judgment: When running in local root context (cwd == primary worktree root
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
import subprocess
import sys
from pathlib import Path
from typing import Any


# ─── Reason codes ────────────────────────────────────────────────────────────

REASON_NOT_LOCAL_ROOT = "not_local_root_context"
REASON_READONLY = "readonly_command"
REASON_RECOVERY = "recovery_to_default_branch"
REASON_DRIFT = "local_root_branch_drift"
REASON_ALREADY_DRIFTED = "already_drifted_root"
REASON_UNPARSEABLE = "unparseable_branch_mutation"
REASON_INLINE_OVERRIDE = "inline_env_override_not_allowed"


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
    Return True if cwd is the primary worktree root (NOT inside a linked worktree).

    Uses git worktree list --porcelain -z catalog to identify primary root.
    Does NOT rely on git rev-parse --show-toplevel alone.
    """
    # Resolve project root with priority:
    # 1. CLAUDE_PROJECT_DIR env
    # 2. git worktree list primary path
    project_root = _resolve_project_root(cwd)
    if project_root is None:
        return False

    real_cwd = os.path.realpath(cwd)
    real_root = os.path.realpath(project_root)

    # cwd must equal primary worktree root
    if real_cwd != real_root:
        return False

    # Also check cwd is NOT under .claude/worktrees/
    # (defensive: if worktrees are inside the project root)
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

# Patterns for allowed read-only / non-branch-mutating commands
READONLY_PATTERNS = [
    r"^git\s+(status|fetch|log|show|diff|blame|annotate|shortlog|describe)(\s|$)",
    r"^git\s+branch\s+(--show-current|-a|-r|-v|--list|--verbose)(\s|$)",
    r"^git\s+rev-parse(\s|$)",
    r"^git\s+worktree\s+(list|prune)(\s|$)",
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

# git switch/checkout to default branch (recovery)
# Will be matched after resolving default_branch
BRANCH_MUTATION_PATTERNS = [
    # git switch variants that mutate branch
    r"^git\s+switch\s+",
    # git checkout <branch> forms (NOT path restore)
    r"^git\s+checkout\s+(?!--)(?!HEAD\s+--)(?!HEAD~\d+\s+--)(?!--pathspec)(?!-p)(?!-q\s+--)(?!\S+\s+--\s+)",
    # git branch -m/-M (rename)
    r"^git\s+branch\s+-[mM]\s+",
    # gh pr checkout
    r"^gh\s+pr\s+checkout\s+",
    # hub pr checkout
    r"^hub\s+pr\s+checkout\s+",
]

# Compound / wrapped shell patterns that are fail-closed
COMPOUND_SHELL_PATTERNS = [
    r"^bash\s+",
    r"^sh\s+",
    r"^zsh\s+",
    r"^env\s+",
    r"^command\s+",
    r"&&",
    r"\|\|",
    r";",
    r"`",
    r"\$\(",
]

# Allow-list when already drifted
ALLOWED_WHEN_DRIFTED_PATTERNS = [
    # git read-only + recovery
    r"^git\s+(status|branch\s+--show-current|rev-parse(\s|$)|worktree\s+(list|prune)|fetch)(\s|$)",
    r"^git\s+switch\s+",   # will be further checked against default branch
    r"^git\s+checkout\s+", # will be further checked against default branch
    # gh read-only
    r"^gh\s+issue\s+(view|list)(\s|$)",
    r"^gh\s+pr\s+(view|list|status)(\s|$)",
]


def is_readonly_command(cmd: str) -> bool:
    """Return True if the command is clearly read-only and safe."""
    cmd = cmd.strip()
    for pattern in READONLY_PATTERNS:
        if re.match(pattern, cmd):
            return True
    return False


def is_path_restore_command(cmd: str) -> bool:
    """Return True if command is a git checkout path-restore (not branch switch)."""
    cmd = cmd.strip()
    for pattern in CHECKOUT_PATH_RESTORE_PATTERNS:
        if re.match(pattern, cmd):
            return True
    return False


def is_compound_or_wrapped(cmd: str) -> bool:
    """Return True if command appears to be a compound/wrapped shell command."""
    # Check for shell wrappers at the start
    if re.match(r"^(bash|sh|zsh)\s+(-[a-z]+\s+)*[\"']", cmd):
        return True
    if re.match(r"^(bash|sh|zsh)\s+(-[a-z]+\s+)*\"?\$\(", cmd):
        return True
    if re.match(r"^(bash|sh|zsh)\s+(-[a-z]+\s+)*-c\s+", cmd):
        return True
    # Check for shell-level compound operators
    for pattern in ["&&", "||", ";", "`"]:
        if pattern in cmd:
            return True
    # env prefix
    if re.match(r"^env\s+", cmd):
        return True
    # command prefix
    if re.match(r"^command\s+", cmd):
        return True
    return False


def extract_target_branch_from_git_switch(cmd: str) -> str | None:
    """Extract branch name from git switch command."""
    cmd = cmd.strip()
    # git switch [options] <branch>
    # Options that create/change branches:
    # -c/-C <new-branch>, --orphan <branch>, --detach [<start>], --guess <remote>
    # We want the target branch for all these

    # git switch --detach [<commit>]
    m = re.match(r"^git\s+switch\s+(?:.*\s+)?--detach(?:\s+(\S+))?", cmd)
    if m:
        return m.group(1) or "<detached>"

    # git switch --orphan <branch>
    m = re.match(r"^git\s+switch\s+(?:.*\s+)?--orphan\s+(\S+)", cmd)
    if m:
        return m.group(1)

    # git switch -c/-C <new-branch> [<start-point>]
    m = re.match(r"^git\s+switch\s+(?:.*\s+)?-[cC]\s+(\S+)", cmd)
    if m:
        return m.group(1)

    # git switch --guess <remote-tracking-branch>
    m = re.match(r"^git\s+switch\s+(?:.*\s+)?--guess\s+(\S+)", cmd)
    if m:
        return m.group(1)

    # git switch <branch> (simple form, last non-flag arg)
    # Strip options starting with - and get last arg
    parts = cmd.split()
    branches = [p for p in parts[2:] if not p.startswith("-")]
    if branches:
        return branches[-1]
    return None


def extract_target_branch_from_git_checkout(cmd: str) -> str | None:
    """Extract branch name from git checkout command (branch-switching forms only)."""
    cmd = cmd.strip()
    # git checkout --detach [<commit>]
    m = re.match(r"^git\s+checkout\s+(?:.*\s+)?--detach(?:\s+(\S+))?", cmd)
    if m:
        return m.group(1) or "<detached>"

    # git checkout --orphan <branch>
    m = re.match(r"^git\s+checkout\s+(?:.*\s+)?--orphan\s+(\S+)", cmd)
    if m:
        return m.group(1)

    # git checkout --ignore-other-worktrees <branch>
    m = re.match(r"^git\s+checkout\s+(?:.*\s+)?--ignore-other-worktrees\s+(\S+)", cmd)
    if m:
        return m.group(1)

    # git checkout -b/-B <branch> [<start>]
    m = re.match(r"^git\s+checkout\s+(?:.*\s+)?-[bB]\s+(\S+)", cmd)
    if m:
        return m.group(1)

    # git checkout <branch> (simple form)
    # Last arg that doesn't start with -
    parts = cmd.split()
    branches = [p for p in parts[2:] if not p.startswith("-")]
    if branches:
        return branches[-1]
    return None


def extract_target_branch_from_git_branch_rename(cmd: str) -> str | None:
    """Extract new branch name from git branch -m/-M <old> <new> or -m/-M <new>."""
    m = re.match(r"^git\s+branch\s+-[mM]\s+(\S+)(?:\s+(\S+))?", cmd)
    if m:
        # If two args: first is old, second is new; if one arg: it's the new name
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
    # Match leading env assignments like: VAR=value command ...
    # or inline: ... LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE=1 git switch ...
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

    # Step 5: Compound/wrapped commands in local root context: fail-closed
    # (Must check BEFORE readonly: "git status || git switch issue-*" starts with "git status")
    if is_compound_or_wrapped(cmd):
        # Could contain hidden branch mutation
        return _result(
            status="block",
            reason_code=REASON_UNPARSEABLE,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # Step 6: Read-only commands are always allowed
    if is_readonly_command(cmd):
        return _result(
            status="allow",
            reason_code=REASON_READONLY,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # Step 7: Path restore commands are not branch mutations
    if is_path_restore_command(cmd):
        return _result(
            status="allow",
            reason_code=REASON_READONLY,
            current_branch=current_branch,
            target_branch=None,
            target_branch_kind=None,
            hook_flavor=hook_flavor,
        )

    # Step 8: Already-drifted root has stricter rules
    already_drifted = (current_branch is not None and current_branch != default_branch)

    # Step 9: Check if this is a branch mutation command
    is_branch_mutation = _is_branch_mutation_command(cmd)

    if is_branch_mutation:
        # Extract target branch
        target_branch = _extract_target_branch(cmd)
        target_kind = classify_branch(target_branch, default_branch) if target_branch else "unknown"

        # Allow recovery to default branch
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
        return _result(
            status="block",
            reason_code=REASON_DRIFT if not already_drifted else REASON_ALREADY_DRIFTED,
            current_branch=current_branch,
            target_branch=target_branch,
            target_branch_kind=target_kind,
            hook_flavor=hook_flavor,
        )

    # Step 10: Already-drifted root: block mutating gh commands and uv/pnpm
    if already_drifted:
        if _is_blocked_when_drifted(cmd):
            return _result(
                status="block",
                reason_code=REASON_ALREADY_DRIFTED,
                current_branch=current_branch,
                target_branch=None,
                target_branch_kind=None,
                hook_flavor=hook_flavor,
            )

    # Default: allow
    return _result(
        status="allow",
        reason_code=REASON_READONLY if not is_branch_mutation else REASON_RECOVERY,
        current_branch=current_branch,
        target_branch=None,
        target_branch_kind=None,
        hook_flavor=hook_flavor,
    )


def _is_branch_mutation_command(cmd: str) -> bool:
    """Return True if cmd is a branch mutation command."""
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


def _extract_target_branch(cmd: str) -> str | None:
    """Extract target branch from a branch mutation command."""
    if re.match(r"^git\s+switch\s+", cmd):
        return extract_target_branch_from_git_switch(cmd)
    if re.match(r"^git\s+checkout\s+", cmd):
        return extract_target_branch_from_git_checkout(cmd)
    if re.match(r"^git\s+branch\s+-[mM]\s+", cmd):
        return extract_target_branch_from_git_branch_rename(cmd)
    if re.match(r"^(?:gh|hub)\s+pr\s+checkout\s+", cmd):
        return extract_target_branch_from_gh_pr_checkout(cmd)
    return None


def _is_blocked_when_drifted(cmd: str) -> bool:
    """Return True if command is blocked when root is already drifted."""
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
            current_branch=None,
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
        _emit_block_stderr(
            reason_code=result["reason_code"],
            current_branch=result.get("current_branch"),
            target_branch_kind=result.get("target_branch_kind"),
            hook_flavor=hook_flavor,
        )
        return 2

    return 0


def _emit_block_stderr(
    reason_code: str,
    current_branch: str | None,
    target_branch_kind: str | None,
    hook_flavor: str,
) -> None:
    """Emit bounded, non-leaking block message to stderr (max 10 lines)."""
    lines = [
        "[local_main_branch_guard] blocked: local root checkout must stay on default branch",
        f"reason_code: {reason_code}",
    ]
    if current_branch:
        lines.append(f"current_branch: {current_branch}")
    if target_branch_kind:
        lines.append(f"target_branch_kind: {target_branch_kind}")

    if reason_code == REASON_INLINE_OVERRIDE:
        lines.append("recovery: set LOOP_ALLOW_LOCAL_ROOT_BRANCH_CHANGE and LOOP_LOCAL_ROOT_BRANCH_CHANGE_REASON in CLI env before launch")
    elif reason_code == REASON_ALREADY_DRIFTED:
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
