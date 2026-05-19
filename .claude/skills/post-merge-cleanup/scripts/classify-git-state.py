#!/usr/bin/env python3
"""
classify-git-state.py

git status / git stash list / git branch -vv / git worktree list を実行し、
YAML 構造化出力を返す。

Usage:
    python3 classify-git-state.py [--format yaml|json]

Exit codes:
    0 — success
    1 — git command failed
"""

import argparse
import json
import re
import subprocess
import sys


def run_git(*args: str) -> str:
    """Run a git subcommand with fixed array args (subprocess, not shell)."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print(f"[WARN] git {' '.join(args)} exited {result.returncode}: {result.stderr.strip()}", file=sys.stderr)
    return result.stdout


def parse_status(raw: str) -> dict:
    staged = []
    unstaged = []
    untracked = []
    for line in raw.splitlines():
        if len(line) < 2:
            continue
        xy = line[:2]
        path = line[3:]
        if xy[0] != ' ' and xy[0] != '?':
            staged.append({"xy": xy, "path": path})
        if xy[1] != ' ' and xy[1] != '?':
            unstaged.append({"xy": xy, "path": path})
        if xy == '??':
            untracked.append(path)
    return {
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "clean": len(staged) == 0 and len(unstaged) == 0 and len(untracked) == 0,
    }


def parse_stash_list(raw: str) -> list:
    stashes = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            stashes.append(line)
    return stashes


def parse_branch_vv(raw: str) -> list:
    branches = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        current = line.startswith('*')
        if current:
            line = line[1:].strip()
        gone = ': gone]' in line
        # Extract branch name (first token)
        parts = line.split()
        name = parts[0] if parts else ""
        branches.append({
            "name": name,
            "current": current,
            "gone": gone,
            "raw": line,
        })
    return branches


def parse_worktree_list(raw: str) -> list:
    worktrees = []
    current_wt: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            if current_wt:
                worktrees.append(current_wt)
                current_wt = {}
            continue
        if line.startswith("worktree "):
            current_wt["path"] = line[len("worktree "):].strip()
        elif line.startswith("HEAD "):
            current_wt["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            current_wt["branch"] = line[len("branch "):].strip()
        elif line == "bare":
            current_wt["bare"] = True
        elif line == "detached":
            current_wt["detached"] = True
        elif line.startswith("locked"):
            current_wt["locked"] = True
    if current_wt:
        worktrees.append(current_wt)
    return worktrees


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Classify git state into structured output"
    )
    parser.add_argument(
        "--format",
        choices=["yaml", "json"],
        default="yaml",
        help="Output format (default: yaml)"
    )
    args = parser.parse_args()

    status_raw = run_git("status", "--short")
    stash_raw = run_git("stash", "list")
    branch_raw = run_git("branch", "-vv")
    worktree_raw = run_git("worktree", "list", "--porcelain")

    state = {
        "status": parse_status(status_raw),
        "stashes": parse_stash_list(stash_raw),
        "branches": parse_branch_vv(branch_raw),
        "worktrees": parse_worktree_list(worktree_raw),
    }

    if args.format == "json":
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        # Simple YAML-like output
        def emit_yaml(obj: object, indent: int = 0) -> None:
            pad = "  " * indent
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, (dict, list)):
                        print(f"{pad}{k}:")
                        emit_yaml(v, indent + 1)
                    elif isinstance(v, bool):
                        print(f"{pad}{k}: {str(v).lower()}")
                    elif v is None:
                        print(f"{pad}{k}: null")
                    else:
                        print(f"{pad}{k}: {v}")
            elif isinstance(obj, list):
                if not obj:
                    print(f"{pad}[]")
                    return
                for item in obj:
                    if isinstance(item, dict):
                        first = True
                        for k, v in item.items():
                            prefix = f"{pad}- " if first else f"{pad}  "
                            first = False
                            if isinstance(v, (dict, list)):
                                print(f"{prefix}{k}:")
                                emit_yaml(v, indent + 2)
                            elif isinstance(v, bool):
                                print(f"{prefix}{k}: {str(v).lower()}")
                            elif v is None:
                                print(f"{prefix}{k}: null")
                            else:
                                print(f"{prefix}{k}: {v}")
                    else:
                        print(f"{pad}- {item}")
            else:
                print(f"{pad}{obj}")

        emit_yaml(state)


if __name__ == "__main__":
    main()
