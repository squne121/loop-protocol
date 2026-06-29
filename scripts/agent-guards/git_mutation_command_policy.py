#!/usr/bin/env python3
"""Shared bounded policy for issue-worktree `rtk git` mutations.

This module is intentionally narrow: it recognizes only the exact command
shapes that Issue #1241 wants to recover (`rtk git add/commit/push`) and keeps
the rest fail-closed.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass


ALLOWED_RTK_GIT_SUBCOMMANDS = frozenset({"add", "commit", "push"})
DENIED_PUSH_FLAGS = frozenset({"--force", "-f", "--tags", "--all", "--mirror", "--delete"})
COMMAND_CLASS_RTK_GIT_ADD = "rtk_git_add"
COMMAND_CLASS_RTK_GIT_COMMIT = "rtk_git_commit"
COMMAND_CLASS_RTK_GIT_PUSH = "rtk_git_push"
COMMAND_CLASS_RTK_GIT_UNKNOWN = "rtk_git_unknown"


@dataclass(frozen=True)
class GitMutationPolicyResult:
    status: str
    command_class: str
    reason_code: str
    suggested_command: str | None = None
    verification_command: str | None = None


def _tokenize(command: str) -> list[str] | None:
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return None


def _current_branch(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    branch = result.stdout.strip()
    return branch or None


def _extract_git_argv(tokens: list[str]) -> list[str] | None:
    if len(tokens) >= 3 and tokens[0] == "rtk" and tokens[1] == "git":
        return tokens[1:]
    if tokens and os.path.basename(tokens[0]) in {"bash", "sh", "zsh"}:
        for idx, tok in enumerate(tokens[:-1]):
            if tok in {"-c", "-lc"}:
                inner = _tokenize(tokens[idx + 1])
                if inner:
                    return _extract_git_argv(inner)
    if tokens and tokens[0] == "env":
        rest = list(tokens[1:])
        while rest and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", rest[0]):
            rest = rest[1:]
        if rest:
            return _extract_git_argv(rest)
    if tokens and tokens[0] == "command":
        return _extract_git_argv(tokens[1:])
    return None


def _contains_broad_pathspec(pathspecs: list[str], cwd: str) -> bool:
    if not pathspecs:
        return True
    for pathspec in pathspecs:
        if pathspec in {".", "..", ":/"}:
            return True
        if any(ch in pathspec for ch in "*?[]"):
            return True
        if pathspec.startswith(":("):
            return True
        resolved = os.path.realpath(os.path.join(cwd, pathspec))
        if os.path.isdir(resolved):
            return True
    return False


def classify_rtk_git_mutation(
    command: str,
    *,
    cwd: str,
    require_active_branch_push: bool,
) -> GitMutationPolicyResult | None:
    """Return a bounded policy result for recognized `rtk git` commands."""
    tokens = _tokenize(command)
    if not tokens:
        return None
    git_argv = _extract_git_argv(tokens)
    if not git_argv or git_argv[0] != "git" or len(git_argv) < 2:
        return None

    subcommand = git_argv[1]
    args = git_argv[2:]
    if subcommand not in ALLOWED_RTK_GIT_SUBCOMMANDS:
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_UNKNOWN,
            reason_code="rtk_unknown_inner",
            suggested_command="rtk git add <allowed-path-file>",
            verification_command="git branch --show-current",
        )

    if subcommand == "add":
        if not args or any(arg in {"-A", "-u", "--all"} for arg in args):
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_ADD,
                reason_code="git_add_requires_explicit_pathspec",
                suggested_command="rtk git add <allowed-path-file>",
                verification_command="git diff --name-only",
            )
        filtered = [arg for arg in args if arg != "--"]
        if any(arg.startswith("--pathspec-from-file") for arg in filtered) or _contains_broad_pathspec(filtered, cwd):
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_ADD,
                reason_code="git_add_requires_explicit_pathspec",
                suggested_command="rtk git add <allowed-path-file>",
                verification_command="git diff --name-only",
            )
        return GitMutationPolicyResult(
            status="allow",
            command_class=COMMAND_CLASS_RTK_GIT_ADD,
            reason_code="rtk_git_add_allowed",
        )

    if subcommand == "commit":
        if len(args) != 2 or args[0] != "-m" or not args[1].strip():
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_COMMIT,
                reason_code="rtk_git_commit_requires_message",
                suggested_command='rtk git commit -m "issue-1241 update"',
                verification_command="git diff --cached --name-only",
            )
        return GitMutationPolicyResult(
            status="allow",
            command_class=COMMAND_CLASS_RTK_GIT_COMMIT,
            reason_code="rtk_git_commit_allowed",
        )

    if any(flag in args for flag in DENIED_PUSH_FLAGS):
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_PUSH,
            reason_code="push_refspec_requires_active_branch",
            suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
            verification_command="git branch --show-current",
        )
    if len(args) != 2 or args[0] != "origin":
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_PUSH,
            reason_code="push_refspec_requires_active_branch",
            suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
            verification_command="git branch --show-current",
        )
    refspec = args[1]
    if not refspec.startswith("HEAD:refs/heads/"):
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_PUSH,
            reason_code="push_refspec_requires_active_branch",
            suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
            verification_command="git branch --show-current",
        )
    target_branch = refspec.removeprefix("HEAD:refs/heads/")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", target_branch):
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_PUSH,
            reason_code="push_refspec_requires_active_branch",
            suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
            verification_command="git branch --show-current",
        )
    if require_active_branch_push:
        current = _current_branch(cwd)
        if not current or current != target_branch:
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_PUSH,
                reason_code="push_refspec_requires_active_branch",
                suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
                verification_command="git branch --show-current",
            )
    return GitMutationPolicyResult(
        status="allow",
        command_class=COMMAND_CLASS_RTK_GIT_PUSH,
        reason_code="rtk_git_push_allowed",
    )
