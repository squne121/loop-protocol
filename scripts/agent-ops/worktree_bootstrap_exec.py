#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARDS_DIR = REPO_ROOT / "scripts" / "agent-guards"
if str(GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(GUARDS_DIR))

from worktree_bootstrap_command_policy import (  # noqa: E402
    expected_branch_name,
    expected_worktree_path,
    normalize_default_branch_ref,
    parse_exact_worktree_bootstrap_command,
)
from skill_runtime_command_policy import (  # noqa: E402
    current_branch,
    resolve_default_branch,
)


def _emit(status: str, reason_code: str | None, *, issue_number: int, slug: str,
          worktree_path: str, branch: str, base_ref: str, head_oid: str | None = None,
          errors: list[str] | None = None) -> int:
    payload = {
        "schema": "WORKTREE_BOOTSTRAP_RESULT_V1",
        "status": status,
        "reason_code": reason_code,
        "issue_number": issue_number,
        "slug": slug,
        "worktree_path": worktree_path,
        "branch": branch,
        "base_ref": base_ref,
        "head_oid": head_oid,
        "errors": errors or [],
    }
    print(json.dumps(payload))
    return 0 if status.startswith("ok_") else 1


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        capture_output=True,
        check=False,
    )


def _tracked_worktree_path(repo: Path, branch: str) -> str | None:
    result = _git(repo, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return None
    current_path: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line.split(" ", 1)[1]
            continue
        if line.startswith("branch ") and current_path:
            ref = line.split(" ", 1)[1]
            if ref == f"refs/heads/{branch}":
                try:
                    return os.path.relpath(current_path, repo)
                except ValueError:
                    return current_path
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--branch-name", required=True)
    parser.add_argument("--worktree-path", required=True)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    repo = Path.cwd()
    default_branch = resolve_default_branch(str(repo))
    root_branch = current_branch(str(repo))
    if root_branch != default_branch:
        return _emit(
            "blocked",
            "root_not_default_branch",
            issue_number=args.issue_number,
            slug=args.slug,
            worktree_path=args.worktree_path,
            branch=args.branch_name,
            base_ref=args.base_ref,
            errors=[f"current branch {root_branch!r} is not default branch {default_branch!r}"],
        )

    expected_branch = expected_branch_name(args.issue_number, args.slug)
    expected_path = expected_worktree_path(args.issue_number, args.slug)
    if args.branch_name != expected_branch:
        return _emit(
            "blocked",
            "invalid_branch",
            issue_number=args.issue_number,
            slug=args.slug,
            worktree_path=args.worktree_path,
            branch=args.branch_name,
            base_ref=args.base_ref,
            errors=[f"expected branch {expected_branch!r}"],
        )
    if args.worktree_path != expected_path:
        return _emit(
            "blocked",
            "invalid_path",
            issue_number=args.issue_number,
            slug=args.slug,
            worktree_path=args.worktree_path,
            branch=args.branch_name,
            base_ref=args.base_ref,
            errors=[f"expected path {expected_path!r}"],
        )
    if normalize_default_branch_ref(args.base_ref, default_branch) is None:
        return _emit(
            "blocked",
            "invalid_base_ref",
            issue_number=args.issue_number,
            slug=args.slug,
            worktree_path=args.worktree_path,
            branch=args.branch_name,
            base_ref=args.base_ref,
            errors=[f"base ref {args.base_ref!r} is not default-branch aligned"],
        )

    exact_cmd = (
        f"uv run python3 scripts/agent-ops/worktree_bootstrap_exec.py "
        f"--issue-number {args.issue_number} --slug {args.slug} "
        f"--branch-name {args.branch_name} --worktree-path {args.worktree_path} "
        f"--base-ref {args.base_ref} --json"
    )
    if parse_exact_worktree_bootstrap_command(exact_cmd, str(repo)) is None:
        return _emit(
            "blocked",
            "invalid_command_shape",
            issue_number=args.issue_number,
            slug=args.slug,
            worktree_path=args.worktree_path,
            branch=args.branch_name,
            base_ref=args.base_ref,
        )

    existing_path = _tracked_worktree_path(repo, args.branch_name)
    if existing_path is not None:
        if existing_path == args.worktree_path:
            head_oid = _git(repo, "rev-parse", args.branch_name).stdout.strip() or None
            return _emit(
                "ok_existing",
                None,
                issue_number=args.issue_number,
                slug=args.slug,
                worktree_path=args.worktree_path,
                branch=args.branch_name,
                base_ref=args.base_ref,
                head_oid=head_oid,
            )
        return _emit(
            "blocked",
            "existing_conflict",
            issue_number=args.issue_number,
            slug=args.slug,
            worktree_path=args.worktree_path,
            branch=args.branch_name,
            base_ref=args.base_ref,
            errors=[f"branch already checked out at {existing_path}"],
        )

    worktree_abs = repo / args.worktree_path
    worktree_abs.parent.mkdir(parents=True, exist_ok=True)
    result = _git(
        repo,
        "worktree",
        "add",
        "-b",
        args.branch_name,
        str(worktree_abs),
        default_branch,
    )
    if result.returncode != 0:
        return _emit(
            "blocked",
            "git_worktree_add_failed",
            issue_number=args.issue_number,
            slug=args.slug,
            worktree_path=args.worktree_path,
            branch=args.branch_name,
            base_ref=args.base_ref,
            errors=[result.stderr.strip() or result.stdout.strip()],
        )
    head_oid = _git(repo, "rev-parse", args.branch_name).stdout.strip() or None
    return _emit(
        "ok_created",
        None,
        issue_number=args.issue_number,
        slug=args.slug,
        worktree_path=args.worktree_path,
        branch=args.branch_name,
        base_ref=args.base_ref,
        head_oid=head_oid,
    )


if __name__ == "__main__":
    raise SystemExit(main())
