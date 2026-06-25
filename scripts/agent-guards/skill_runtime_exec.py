#!/usr/bin/env python3
"""Exact privileged executor for allowed skill runtime commands (Issue #1154)."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from skill_runtime_command_policy import (
    REGISTRY_REL,
    SKILL_RUNTIME_EXEC_REL,
    TRUSTED_REPO_SLUG,
    current_branch,
    is_exact_skill_runtime_executor_command,
    load_registry_entry,
    resolve_active_issue,
    resolve_default_branch,
    resolve_project_root,
    resolve_repo_slug,
    validate_registry_entry,
)


def _is_symlink_path(path: Path) -> bool:
    current = Path(path.anchor) if path.is_absolute() else Path()
    for part in path.parts:
        if part in ("", os.sep):
            continue
        current = current / part
        if current.exists() and current.is_symlink():
            return True
    return False


def _allowed_artifact_root(project_root: str, issue_number: str) -> Path:
    return Path(project_root) / ".claude" / "artifacts" / "issue-refinement-loop" / issue_number


def _tracked_and_untracked_paths(project_root: str) -> set[str]:
    git = shutil.which("git") or "git"
    out = subprocess.run(
        [git, "-C", project_root, "status", "--porcelain=v1", "--untracked-files=all", "-z"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError("git_status_failed")
    paths: set[str] = set()
    fields = [field for field in out.stdout.split("\0") if field]
    i = 0
    while i < len(fields):
        field = fields[i]
        if len(field) < 4:
            i += 1
            continue
        path = field[3:]
        if field[0] == "R" or field[1] == "R":
            paths.add(path)
            if i + 1 < len(fields):
                paths.add(fields[i + 1])
                i += 2
                continue
        paths.add(path)
        i += 1
    return paths


def _ensure_artifact_path_safe(project_root: str, issue_number: str) -> Path:
    artifact_root = _allowed_artifact_root(project_root, issue_number)
    parent = artifact_root.parent
    for candidate in (Path(project_root) / ".claude", Path(project_root) / ".claude" / "artifacts", parent):
        if candidate.exists() and _is_symlink_path(candidate):
            raise RuntimeError("artifact_parent_symlink_not_allowed")
    if artifact_root.exists() and (_is_symlink_path(artifact_root) or artifact_root.is_symlink()):
        raise RuntimeError("artifact_root_symlink_not_allowed")
    return artifact_root


def _sanitize_env(project_root: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["CLAUDE_PROJECT_DIR"] = project_root
    return env


def _validate_runtime_context(project_root: str, args: argparse.Namespace) -> Path:
    if os.path.realpath(os.getcwd()) != os.path.realpath(project_root):
        raise RuntimeError("cwd_not_canonical_main_root")
    branch = current_branch(project_root)
    default_branch = resolve_default_branch(project_root)
    if branch != default_branch:
        raise RuntimeError("root_not_default_branch")
    repo_slug = resolve_repo_slug(project_root)
    if repo_slug != TRUSTED_REPO_SLUG or args.repo != repo_slug:
        raise RuntimeError("repo_binding_mismatch")
    active_issue, entry = resolve_active_issue(project_root, project_root)
    if active_issue != str(args.issue_number):
        raise RuntimeError("active_issue_mismatch")
    if entry is None:
        raise RuntimeError("active_issue_worktree_missing")
    return _ensure_artifact_path_safe(project_root, str(args.issue_number))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Privileged exact skill runtime executor")
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)

    project_root = resolve_project_root()
    command_text = " ".join(
        [
            "python3",
            SKILL_RUNTIME_EXEC_REL,
            "--command-id",
            args.command_id,
            "--issue-number",
            str(args.issue_number),
            "--repo",
            args.repo,
        ]
    )
    if not is_exact_skill_runtime_executor_command(command_text, project_root, project_root):
        print("skill_runtime_exec: exact command class rejected", file=sys.stderr)
        return 2

    artifact_root = _validate_runtime_context(project_root, args)
    before_paths = _tracked_and_untracked_paths(project_root)

    entry = load_registry_entry(args.command_id, project_root)
    validate_registry_entry(args.command_id, entry, str(args.issue_number))

    registry_path = Path(project_root) / REGISTRY_REL
    if registry_path.is_symlink():
        raise RuntimeError("registry_symlink_not_allowed")
    if not registry_path.is_file():
        raise RuntimeError("registry_missing")

    script_path = Path(project_root) / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py"
    if script_path.is_symlink() or not script_path.is_file():
        raise RuntimeError("preflight_script_invalid")

    from importlib.util import spec_from_file_location, module_from_spec

    spec = spec_from_file_location("issue_refinement_command_registry_executor", str(registry_path))
    if spec is None or spec.loader is None:
        raise RuntimeError("registry_spec_invalid")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    render_command = getattr(module, "render_command", None)
    if not callable(render_command):
        raise RuntimeError("render_command_missing")
    child_argv = render_command(
        args.command_id,
        {"issue_number": args.issue_number, "repo": args.repo},
    )

    result = subprocess.run(
        child_argv,
        cwd=project_root,
        env=_sanitize_env(project_root),
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )

    after_paths = _tracked_and_untracked_paths(project_root)
    new_paths = after_paths - before_paths
    allowed_root_real = os.path.realpath(str(artifact_root))
    for rel_path in new_paths:
        abs_path = os.path.realpath(os.path.join(project_root, rel_path))
        if abs_path != allowed_root_real and not abs_path.startswith(allowed_root_real + os.sep):
            print(f"skill_runtime_exec: unauthorized write path {rel_path}", file=sys.stderr)
            return 2

    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
