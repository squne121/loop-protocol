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
from pathlib import Path


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


def _git_toplevel(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    root = result.stdout.strip()
    return root or None


def _extract_git_argv(tokens: list[str]) -> list[str] | None:
    if len(tokens) >= 3 and tokens[0] == "rtk" and tokens[1] == "git":
        return tokens[1:]
    return None


def _load_allowed_paths() -> list[str] | None:
    raw = os.environ.get("CODEX_ALLOWED_PATHS", "").strip()
    if not raw:
        return None
    entries = [line.strip() for line in raw.splitlines() if line.strip()]
    return entries or None


def _normalize_path(path: str) -> str | None:
    if not path or "\\" in path or path.startswith("/"):
        return None
    normalized = path[2:] if path.startswith("./") else path
    if normalized in {"", "."}:
        return None
    segments = normalized.split("/")
    if ".." in segments or "" in segments:
        return None
    return normalized


def _normalize_allowed_pattern(pattern: str) -> str | None:
    if pattern.endswith("/"):
        bare = pattern[:-1]
        if bare.endswith("/") or "*" in bare:
            return None
        normalized_bare = _normalize_path(bare)
        if normalized_bare is None:
            return None
        return normalized_bare + "/**"
    normalized = _normalize_path(pattern)
    if normalized is None:
        return None
    for segment in normalized.split("/"):
        if "*" in segment and segment not in ("*", "**"):
            return None
    return normalized


def _segment_match(file_parts: list[str], pattern_parts: list[str]) -> bool:
    n = len(file_parts)
    m = len(pattern_parts)
    dp = [[False] * (m + 1) for _ in range(n + 1)]
    dp[n][m] = True
    for j in range(m - 1, -1, -1):
        if pattern_parts[j] == "**":
            dp[n][j] = dp[n][j + 1]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            segment = pattern_parts[j]
            if segment == "**":
                dp[i][j] = dp[i][j + 1] or dp[i + 1][j]
            elif segment == "*":
                dp[i][j] = dp[i + 1][j + 1]
            else:
                dp[i][j] = segment == file_parts[i] and dp[i + 1][j + 1]
    return dp[0][0]


def _is_allowed_path(file_path: str, allowed_paths: list[str]) -> bool:
    normalized_file = _normalize_path(file_path)
    if normalized_file is None:
        return False
    for pattern in allowed_paths:
        normalized_pattern = _normalize_allowed_pattern(pattern)
        if normalized_pattern is None:
            continue
        if _segment_match(normalized_file.split("/"), normalized_pattern.split("/")):
            return True
    return False


def _pathspec_to_repo_relative(pathspec: str, cwd: str, repo_root: str) -> str | None:
    candidate = Path(cwd, pathspec).resolve()
    repo_path = Path(repo_root).resolve()
    try:
        relative = candidate.relative_to(repo_path)
    except ValueError:
        return None
    return _normalize_path(relative.as_posix())


def _staged_repo_paths(cwd: str) -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "-z"],
            cwd=cwd,
            capture_output=True,
            text=False,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw_entries = [entry.decode("utf-8", errors="replace") for entry in result.stdout.split(b"\x00") if entry]
    normalized = []
    for entry in raw_entries:
        candidate = _normalize_path(entry)
        if candidate is None:
            return None
        normalized.append(candidate)
    return normalized


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
        return None

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
        allowed_paths = _load_allowed_paths()
        repo_root = _git_toplevel(cwd)
        if not allowed_paths or not repo_root:
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_ADD,
                reason_code="allowed_paths_missing_for_git_mutation",
                suggested_command="git diff --name-only",
                verification_command="git diff --name-only",
            )
        for pathspec in filtered:
            repo_relative = _pathspec_to_repo_relative(pathspec, cwd, repo_root)
            if repo_relative is None or not _is_allowed_path(repo_relative, allowed_paths):
                return GitMutationPolicyResult(
                    status="deny",
                    command_class=COMMAND_CLASS_RTK_GIT_ADD,
                    reason_code="git_add_outside_allowed_paths",
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
        allowed_paths = _load_allowed_paths()
        staged_paths = _staged_repo_paths(cwd)
        if not allowed_paths or staged_paths is None:
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_COMMIT,
                reason_code="allowed_paths_missing_for_git_mutation",
                suggested_command="git diff --cached --name-only",
                verification_command="git diff --cached --name-only",
            )
        if any(not _is_allowed_path(path, allowed_paths) for path in staged_paths):
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_COMMIT,
                reason_code="commit_staged_changes_outside_allowed_paths",
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
