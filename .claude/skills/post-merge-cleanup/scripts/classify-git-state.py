#!/usr/bin/env python3
"""
classify-git-state.py

git status / git stash list / git branch -vv / git worktree list を実行し、
YAML 構造化出力を返す。`--format yaml`（デフォルト）/ `--format json` の
どちらでも read-only な `temp_residue_classification/v1`（Issue #1417,
scripts/agent-ops/temp_residue_classifier.py）を `temp_residue_classification`
field として含める（Issue #1417 PR #1427 review: 以前は `--format json` の
場合にのみ含まれており、SKILL.md の Procedure が案内する既定の
`--format yaml` 実行経路では分類結果が計算コストだけ発生して破棄されていた）。
分類器の実行に失敗した場合は `temp_residue_classification: null` を返し、
classifier failure を empty result（scan_status: ok かつ entries: []）と
明確に区別する。呼び出し側は `null` を成功として扱ってはならない。

Usage:
    python3 classify-git-state.py [--format yaml|json]

Exit codes:
    0 — success
    1 — git command failed
"""

import argparse
import json
import os
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


def classify_temp_residue() -> dict | None:
    """Best-effort, read-only invocation of temp_residue_classifier.py.

    Returns the ``temp_residue_classification/v1`` payload dict, or ``None``
    if the classifier itself could not run (import/exec failure). ``None``
    must be treated by consumers as "classifier failure", distinct from a
    successful run that found zero entries (``entries: []`` with
    ``scan_status: ok``).
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    agent_ops_dir = os.path.normpath(
        os.path.join(script_dir, "..", "..", "..", "..", "scripts", "agent-ops")
    )
    if agent_ops_dir not in sys.path:
        sys.path.insert(0, agent_ops_dir)
    try:
        import temp_residue_classifier as trc  # noqa: PLC0415
    except ImportError:
        return None
    try:
        limits = trc.ScanLimits()
        return trc.run_classification(None, limits, os.environ.get("LOOP_PROTOCOL_SESSION_ID"))
    except Exception:
        return None


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
        "temp_residue_classification": classify_temp_residue(),
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
