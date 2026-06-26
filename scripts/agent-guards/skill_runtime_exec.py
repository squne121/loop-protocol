#!/usr/bin/env python3
"""Exact privileged executor for allowed skill runtime commands (Issue #1154)."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

sys.dont_write_bytecode = True

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


def _is_under_allowed_artifact_root(project_root: str, issue_number: str, rel_path: str) -> bool:
    root = Path(project_root)
    target = (root / rel_path).resolve()
    allowed_root = _allowed_artifact_root(project_root, issue_number).resolve()
    return target == allowed_root or target.is_relative_to(allowed_root)


def _git_status_paths(project_root: str) -> set[str]:
    git = shutil.which("git") or "git"
    out = subprocess.run(
        [
            git,
            "-C",
            project_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
            "-z",
        ],
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


def _snapshot_repo_paths(project_root: str, issue_number: str) -> dict[str, tuple[str, int, int]]:
    root = Path(project_root)
    allowed_root = _allowed_artifact_root(project_root, issue_number)
    allowed_parent_dirs: set[Path] = set()
    for parent in allowed_root.parents:
        allowed_parent_dirs.add(parent)
        if parent == root:
            break

    snapshot: dict[str, tuple[str, int, int]] = {}
    for current_root, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current_root)
        if current_path == root / ".git":
            dirnames[:] = []
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if (current_path / name) != root / ".git"
        ]
        for name in ["."] + dirnames + filenames:
            path = current_path if name == "." else current_path / name
            if path == root / ".git":
                continue
            if path == allowed_root or path.is_relative_to(allowed_root):
                continue
            if path in allowed_parent_dirs:
                continue
            try:
                stat = path.lstat()
            except FileNotFoundError:
                continue
            rel = os.path.relpath(path, root)
            snapshot[rel] = (
                "dir" if path.is_dir() else "file",
                stat.st_mtime_ns,
                stat.st_size,
            )
    return snapshot


def _ensure_artifact_path_safe(project_root: str, issue_number: str) -> Path:
    artifact_root = _allowed_artifact_root(project_root, issue_number)
    parent = artifact_root.parent
    for candidate in (Path(project_root) / ".claude", Path(project_root) / ".claude" / "artifacts", parent):
        if candidate.exists() and _is_symlink_path(candidate):
            raise RuntimeError("artifact_parent_symlink_not_allowed")
    if artifact_root.exists() and (_is_symlink_path(artifact_root) or artifact_root.is_symlink()):
        raise RuntimeError("artifact_root_symlink_not_allowed")
    return artifact_root


def _safe_path_entries() -> list[str]:
    entries = [
        str(Path.home() / ".local" / "bin"),
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in entries:
        if entry and entry not in seen:
            seen.add(entry)
            ordered.append(entry)
    return ordered


def _resolve_trusted_executable(name: str) -> str:
    safe_path = os.pathsep.join(_safe_path_entries())
    resolved = shutil.which(name, path=safe_path)
    if not resolved:
        raise RuntimeError(f"{name}_not_found")
    real = os.path.realpath(resolved)
    allowed_dirs = {os.path.realpath(entry) for entry in _safe_path_entries()}
    real_parent = os.path.realpath(os.path.dirname(real))
    if real_parent not in allowed_dirs:
        raise RuntimeError(f"{name}_outside_trusted_path")
    return real


def _sanitize_env(project_root: str) -> dict[str, str]:
    allowed_keys = {
        "GH_HOST",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if value and (key in allowed_keys or key.startswith("SKILL_RUNTIME_TEST_"))
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CLAUDE_PROJECT_DIR"] = project_root
    env["PATH"] = os.pathsep.join(_safe_path_entries())
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GH_PROMPT_DISABLED"] = "1"
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


def _resolve_child_argv(child_argv: Iterable[str]) -> list[str]:
    resolved = list(child_argv)
    if resolved[:3] == ["uv", "run", "python3"]:
        resolved[0] = _resolve_trusted_executable("uv")
        resolved[2] = _resolve_trusted_executable("python3")
    return resolved


def _find_unauthorized_repo_changes(
    project_root: str,
    issue_number: str,
    before_snapshot: dict[str, tuple[str, int, int]],
    before_status: set[str],
) -> str | None:
    after_snapshot = _snapshot_repo_paths(project_root, issue_number)
    after_status = _git_status_paths(project_root)
    if before_snapshot != after_snapshot:
        changed = sorted(set(before_snapshot) ^ set(after_snapshot))
        if not changed:
            changed = sorted(
                path
                for path in before_snapshot.keys() & after_snapshot.keys()
                if before_snapshot[path] != after_snapshot[path]
            )
        return changed[0] if changed else "snapshot_drift"
    new_status_paths = {
        path
        for path in (after_status - before_status)
        if not _is_under_allowed_artifact_root(project_root, issue_number, path)
    }
    if new_status_paths:
        return sorted(new_status_paths)[0]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Privileged exact skill runtime executor")
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)

    project_root = resolve_project_root()
    command_text = " ".join(
        [
            "uv",
            "run",
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
    before_snapshot = _snapshot_repo_paths(project_root, str(args.issue_number))
    before_status = _git_status_paths(project_root)

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
    child_argv = _resolve_child_argv(child_argv)

    result = subprocess.run(
        child_argv,
        cwd=project_root,
        env=_sanitize_env(project_root),
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )

    unauthorized_path = _find_unauthorized_repo_changes(
        project_root,
        str(args.issue_number),
        before_snapshot,
        before_status,
    )
    if unauthorized_path is not None:
        print(f"skill_runtime_exec: unauthorized write path {unauthorized_path}", file=sys.stderr)
        return 2

    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
