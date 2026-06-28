#!/usr/bin/env python3
"""Controlled executor for implementation worktree bootstrap (Issue #1209)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_AGENT_GUARDS_DIR = _ROOT / "scripts" / "agent-guards"
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))

from skill_runtime_command_policy import TRUSTED_REPO_SLUG, resolve_default_branch, resolve_repo_slug  # noqa: E402
from worktree_bootstrap_command_policy import (  # noqa: E402
    _is_valid_slug,
    expected_branch_name,
    expected_worktree_path,
    normalize_default_branch_ref,
)
from worktree_catalog import branch_short_name, find_by_realpath, list_worktrees  # noqa: E402

SCHEMA = "WORKTREE_BOOTSTRAP_RESULT_V1"


def _result(
    *,
    status: str,
    reason_code: str | None,
    issue_number: int,
    slug: str,
    worktree_path: str,
    branch: str,
    base_ref: str | None,
    head_oid: str | None,
    errors: list[str],
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "status": status,
        "reason_code": reason_code,
        "issue_number": issue_number,
        "slug": slug,
        "worktree_path": worktree_path,
        "branch": branch,
        "base_ref": base_ref,
        "head_oid": head_oid,
        "errors": errors,
    }


def _emit(payload: dict[str, object], exit_code: int) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.write("\n")
    return exit_code


def _run_git(project_root: str, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git") or "git"
    return subprocess.run(
        [git, "-C", project_root, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )


def _git_stdout(project_root: str, *args: str, timeout: float = 10.0) -> str | None:
    try:
        result = _run_git(project_root, *args, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _current_branch(project_root: str) -> str | None:
    return _git_stdout(project_root, "branch", "--show-current")


def _is_under(base: str, target: str) -> bool:
    """Return True iff realpath(target) is under realpath(base)."""
    try:
        real_base = os.path.realpath(base)
        real_target = os.path.realpath(target)
        return os.path.commonpath([real_base, real_target]) == real_base
    except ValueError:
        return False


def _validate_repo_root(project_root: str) -> tuple[bool, str | None]:
    toplevel = _git_stdout(project_root, "rev-parse", "--show-toplevel")
    if toplevel is None or os.path.realpath(toplevel) != os.path.realpath(project_root):
        return False, "invalid_repo_root"
    catalog = list_worktrees(project_root)
    if not catalog:
        return False, "worktree_catalog_unavailable"
    primary = catalog[0].get("worktree_realpath")
    if not primary or os.path.realpath(primary) != os.path.realpath(project_root):
        return False, "not_primary_worktree"
    repo_slug = resolve_repo_slug(project_root)
    if repo_slug != TRUSTED_REPO_SLUG:
        return False, "invalid_repo_slug"
    return True, None


def _branch_exists(project_root: str, branch_name: str) -> bool:
    result = _git_stdout(project_root, "rev-parse", "--verify", f"refs/heads/{branch_name}")
    return result is not None


def _validate_existing_state(
    project_root: str,
    issue_number: int,
    slug: str,
    worktree_realpath: str,
    branch_name: str,
) -> tuple[str, str | None]:
    catalog = list_worktrees(project_root)
    if catalog is None:
        return "blocked", "worktree_catalog_unavailable"
    entry = find_by_realpath(catalog, worktree_realpath)
    if entry is not None:
        if branch_short_name(entry.get("branch_ref")) != branch_name:
            return "blocked", "existing_conflict"
        if entry.get("detached"):
            return "blocked", "existing_conflict"
        if not os.path.isdir(worktree_realpath):
            return "blocked", "existing_conflict"
        if os.path.basename(worktree_realpath) != f"issue-{issue_number}-{slug}":
            return "blocked", "existing_conflict"
        return "ok_existing", None

    if os.path.lexists(worktree_realpath):
        return "blocked", "existing_conflict"

    if _branch_exists(project_root, branch_name):
        for candidate in catalog:
            if branch_short_name(candidate.get("branch_ref")) == branch_name:
                return "blocked", "existing_conflict"
        return "blocked", "existing_conflict"
    return "create", None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--branch-name", required=True)
    parser.add_argument("--worktree-path", required=True)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    # B6: --json is mandatory; reject if missing
    if not args.json:
        payload = _result(
            status="blocked",
            reason_code="invalid_args",
            issue_number=0,
            slug=str(args.slug),
            worktree_path=str(args.worktree_path),
            branch=str(args.branch_name),
            base_ref=str(args.base_ref),
            head_oid=None,
            errors=["--json flag is required"],
        )
        return _emit(payload, 1)

    project_root = os.path.realpath(os.getcwd())
    issue_text = str(args.issue_number)
    slug = str(args.slug)
    branch_name = str(args.branch_name)
    worktree_path = str(args.worktree_path)
    base_ref = str(args.base_ref)

    if not issue_text.isdigit() or int(issue_text) <= 0:
        payload = _result(
            status="blocked",
            reason_code="invalid_args",
            issue_number=0,
            slug=slug,
            worktree_path=worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["issue_number must be a positive integer"],
        )
        return _emit(payload, 1)
    issue_number = int(issue_text)

    expected_path = expected_worktree_path(issue_number, slug)
    expected_branch = expected_branch_name(issue_number, slug)
    normalized_worktree_path = os.path.normpath(worktree_path)
    worktree_realpath = os.path.realpath(os.path.join(project_root, normalized_worktree_path))

    # B3: Symlink escape guard
    worktrees_dir = os.path.join(project_root, ".claude", "worktrees")
    if os.path.islink(worktrees_dir):
        payload = _result(
            status="blocked",
            reason_code="invalid_path",
            issue_number=issue_number,
            slug=slug,
            worktree_path=worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["'.claude/worktrees' is a symlink — symlink escape rejected"],
        )
        return _emit(payload, 1)
    if not _is_under(project_root, worktree_realpath):
        payload = _result(
            status="blocked",
            reason_code="invalid_path",
            issue_number=issue_number,
            slug=slug,
            worktree_path=worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["worktree_path realpath escapes project root"],
        )
        return _emit(payload, 1)

    if not _is_valid_slug(slug):
        payload = _result(
            status="blocked",
            reason_code="invalid_args",
            issue_number=issue_number,
            slug=slug,
            worktree_path=worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["slug must match [a-z0-9][a-z0-9-]{0,63}"],
        )
        return _emit(payload, 1)
    if branch_name != expected_branch:
        payload = _result(
            status="blocked",
            reason_code="invalid_branch",
            issue_number=issue_number,
            slug=slug,
            worktree_path=worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["branch_name must match worktree-issue-<issue>-<slug>"],
        )
        return _emit(payload, 1)
    if normalized_worktree_path != expected_path or worktree_path.startswith("/") or "\\" in worktree_path:
        payload = _result(
            status="blocked",
            reason_code="invalid_path",
            issue_number=issue_number,
            slug=slug,
            worktree_path=worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["worktree_path must be .claude/worktrees/issue-<issue>-<slug>"],
        )
        return _emit(payload, 1)
    try:
        check_ref = _run_git(project_root, "check-ref-format", "--branch", branch_name)
    except (OSError, subprocess.TimeoutExpired):
        check_ref = None
    if check_ref is None or check_ref.returncode != 0:
        payload = _result(
            status="blocked",
            reason_code="invalid_branch",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["branch_name failed git check-ref-format --branch"],
        )
        return _emit(payload, 1)

    repo_ok, repo_reason = _validate_repo_root(project_root)
    if not repo_ok:
        payload = _result(
            status="blocked",
            reason_code="invalid_repo",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=[repo_reason or "invalid repo state"],
        )
        return _emit(payload, 1)

    default_branch = resolve_default_branch(project_root)
    if _current_branch(project_root) != default_branch:
        payload = _result(
            status="blocked",
            reason_code="root_not_default_branch",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["current branch must equal the repository default branch"],
        )
        return _emit(payload, 1)

    normalized_base_ref = normalize_default_branch_ref(base_ref, default_branch)
    if normalized_base_ref is None:
        payload = _result(
            status="blocked",
            reason_code="invalid_base_ref",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=base_ref,
            head_oid=None,
            errors=["base_ref must normalize to the repository default branch"],
        )
        return _emit(payload, 1)

    state, state_reason = _validate_existing_state(
        project_root,
        issue_number,
        slug,
        worktree_realpath,
        branch_name,
    )
    if state == "ok_existing":
        head_oid = _git_stdout(worktree_realpath, "rev-parse", "HEAD")
        payload = _result(
            status="ok_existing",
            reason_code=None,
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=normalized_base_ref,
            head_oid=head_oid,
            errors=[],
        )
        return _emit(payload, 0)
    if state == "blocked":
        payload = _result(
            status="blocked",
            reason_code=state_reason,
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=normalized_base_ref,
            head_oid=None,
            errors=[state_reason or "existing worktree conflict"],
        )
        return _emit(payload, 1)

    try:
        result = _run_git(
            project_root,
            "worktree",
            "add",
            "--no-guess-remote",
            "-b",
            branch_name,
            normalized_worktree_path,
            normalized_base_ref,
            timeout=20.0,
        )
    except subprocess.TimeoutExpired:
        payload = _result(
            status="failed",
            reason_code="timeout",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=normalized_base_ref,
            head_oid=None,
            errors=["git worktree add timed out"],
        )
        return _emit(payload, 1)
    except OSError:
        payload = _result(
            status="failed",
            reason_code="git_failed",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=normalized_base_ref,
            head_oid=None,
            errors=["git executable failed to start"],
        )
        return _emit(payload, 1)

    if result.returncode != 0:
        payload = _result(
            status="failed",
            reason_code="git_failed",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=normalized_base_ref,
            head_oid=None,
            errors=["git worktree add returned non-zero"],
        )
        return _emit(payload, 1)

    catalog = list_worktrees(project_root)
    entry = find_by_realpath(catalog or [], worktree_realpath)
    # B4: Full post-creation readback — equivalent to _validate_existing_state checks
    creation_invalid = (
        entry is None
        or branch_short_name(entry.get("branch_ref")) != branch_name
        or entry.get("detached")
        or not os.path.isdir(worktree_realpath)
        or os.path.basename(worktree_realpath) != f"issue-{issue_number}-{slug}"
    )
    if creation_invalid:
        payload = _result(
            status="failed",
            reason_code="git_failed",
            issue_number=issue_number,
            slug=slug,
            worktree_path=normalized_worktree_path,
            branch=branch_name,
            base_ref=normalized_base_ref,
            head_oid=None,
            errors=["created worktree failed post-creation readback validation"],
        )
        return _emit(payload, 1)

    head_oid = _git_stdout(worktree_realpath, "rev-parse", "HEAD")
    payload = _result(
        status="ok_created",
        reason_code=None,
        issue_number=issue_number,
        slug=slug,
        worktree_path=normalized_worktree_path,
        branch=branch_name,
        base_ref=normalized_base_ref,
        head_oid=head_oid,
        errors=[],
    )
    return _emit(payload, 0)


if __name__ == "__main__":
    raise SystemExit(main())
