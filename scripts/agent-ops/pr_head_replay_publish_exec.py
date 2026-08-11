#!/usr/bin/env python3
"""Fail-closed replay/publish executor for an approved PR source range.

The executor deliberately has no merge, rebase, reset, or force-push path.
It reproduces one reviewed source range in an executor-owned detached worktree,
then performs one guarded SHA refspec push after two independent head checks.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence


Runner = Callable[..., subprocess.CompletedProcess[str]]
SHA_LENGTH = 40
PROTECTED_PREFIXES = ("assets/", "LICENSES/")


def _run(argv: Sequence[str], *, cwd: Path, input: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=str(cwd),
        input=input,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _error_code(proc: subprocess.CompletedProcess[str]) -> str:
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    return detail[0][:240] if detail else f"exit_{proc.returncode}"


def _is_sha(value: str) -> bool:
    return len(value) == SHA_LENGTH and all(char in "0123456789abcdef" for char in value.lower())


def _result(status: str, *, errors: list[str], **fields: Any) -> dict[str, Any]:
    return {
        "PR_HEAD_REPLAY_PUBLISH_RESULT_V1": {
            "status": status,
            "pushed": False,
            "new_commit_sha": None,
            "errors": errors,
            **fields,
        }
    }


def _read_pr(runner: Runner, root: Path, repo: str, pr_number: int) -> tuple[dict[str, Any] | None, str | None]:
    proc = runner(
        ["rtk", "gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "headRefName,headRefOid"],
        cwd=root,
    )
    if proc.returncode:
        return None, f"pr_read_failed:{_error_code(proc)}"
    try:
        document = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None, "pr_read_invalid_json"
    if not isinstance(document, dict):
        return None, "pr_read_invalid_document"
    return document, None


def _allowed_paths(runner: Runner, root: Path, repo: str, issue_number: int) -> tuple[list[str] | None, str | None]:
    proc = runner(
        ["rtk", "gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "body"], cwd=root
    )
    if proc.returncode:
        return None, f"issue_read_failed:{_error_code(proc)}"
    try:
        body = json.loads(proc.stdout).get("body")
    except (AttributeError, json.JSONDecodeError):
        return None, "issue_read_invalid_json"
    if not isinstance(body, str):
        return None, "issue_body_missing"
    marker = "## Allowed Paths"
    if marker not in body:
        return None, "allowed_paths_section_missing"
    section = body.split(marker, 1)[1].split("\n## ", 1)[0]
    paths = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        candidate = stripped[1:].strip().strip("`")
        if candidate:
            paths.append(candidate)
    if not paths:
        return None, "allowed_paths_empty"
    return paths, None


def _parse_name_status(raw: str) -> set[str]:
    items = raw.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(items):
        status = items[index]
        if not status:
            index += 1
            continue
        code = status[:1]
        if code in {"R", "C"}:
            if index + 2 >= len(items):
                raise ValueError("truncated_rename_status")
            paths.update((items[index + 1], items[index + 2]))
            index += 3
        elif code in {"A", "M", "D", "T", "U", "X", "B"}:
            if index + 1 >= len(items):
                raise ValueError("truncated_path_status")
            paths.add(items[index + 1])
            index += 2
        else:
            raise ValueError(f"unknown_name_status:{status}")
    return paths


def _path_allowed(path: str, allowed_paths: list[str]) -> bool:
    if not path or path.startswith("/") or path.startswith("../") or "/../" in path:
        return False
    if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in PROTECTED_PREFIXES):
        return False
    return any(path == allowed or (allowed.endswith("/") and path.startswith(allowed)) for allowed in allowed_paths)


def _pr_matches(pr: dict[str, Any], *, expected_head: str, target_branch: str) -> bool:
    return pr.get("headRefOid") == expected_head and pr.get("headRefName") == target_branch


def execute(
    *,
    repo: str,
    issue_number: int,
    pr_number: int,
    target_branch: str,
    expected_remote_pr_head: str,
    source_base: str,
    source_head: str,
    project_root: Path,
    runner: Runner = _run,
) -> dict[str, Any]:
    """Replay one source range and publish it only under exact-head guards."""
    input_fields = {
        "repo": repo,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "target_branch": target_branch,
        "expected_remote_pr_head": expected_remote_pr_head,
        "source_base": source_base,
        "source_head": source_head,
    }
    if not repo or issue_number <= 0 or pr_number <= 0 or not target_branch or not all(
        _is_sha(value) for value in (expected_remote_pr_head, source_base, source_head)
    ):
        return _result("blocked", errors=["invalid_arguments"], **input_fields)

    root_proc = runner(["git", "rev-parse", "--show-toplevel"], cwd=project_root)
    if root_proc.returncode:
        return _result("blocked", errors=[f"project_root_invalid:{_error_code(root_proc)}"], **input_fields)
    root = Path(root_proc.stdout.strip()).resolve()

    pr, error = _read_pr(runner, root, repo, pr_number)
    if error or pr is None:
        return _result("blocked", errors=[error or "pr_read_failed"], **input_fields)
    if not _pr_matches(pr, expected_head=expected_remote_pr_head, target_branch=target_branch):
        return _result("blocked", errors=["pr_head_or_branch_mismatch"], **input_fields)
    fetched = runner(["git", "fetch", "origin", f"refs/heads/{target_branch}"], cwd=root)
    if fetched.returncode:
        return _result("blocked", errors=[f"target_fetch_failed:{_error_code(fetched)}"], **input_fields)

    ancestor = runner(["git", "merge-base", "--is-ancestor", source_base, source_head], cwd=root)
    if ancestor.returncode:
        return _result("blocked", errors=["source_base_not_ancestor"], **input_fields)

    allowed_paths, error = _allowed_paths(runner, root, repo, issue_number)
    if error or allowed_paths is None:
        return _result("blocked", errors=[error or "allowed_paths_unavailable"], **input_fields)
    source_status = runner(["git", "diff", "--name-status", "-z", "-M", source_base, source_head], cwd=root)
    if source_status.returncode:
        return _result("blocked", errors=[f"source_diff_failed:{_error_code(source_status)}"], **input_fields)
    try:
        source_paths = _parse_name_status(source_status.stdout)
    except ValueError as exc:
        return _result("blocked", errors=[str(exc)], **input_fields)
    if not source_paths:
        return _result("blocked", errors=["source_range_empty"], **input_fields)
    disallowed = sorted(path for path in source_paths if not _path_allowed(path, allowed_paths))
    if disallowed:
        return _result(
            "blocked",
            errors=["source_range_contains_disallowed_path"],
            disallowed_paths=disallowed,
            **input_fields,
        )

    worktree = root / ".claude" / "worktrees" / f"pr-head-replay-{issue_number}-{pr_number}-{uuid.uuid4().hex}"
    cleanup_error: str | None = None
    new_commit: str | None = None
    pushed = False
    try:
        created = runner(["git", "worktree", "add", "--detach", str(worktree), expected_remote_pr_head], cwd=root)
        if created.returncode:
            return _result(
                "failed", errors=[f"temporary_worktree_create_failed:{_error_code(created)}"], **input_fields
            )
        binary_diff = runner(["git", "diff", "--binary", source_base, source_head], cwd=root)
        if binary_diff.returncode:
            return _result("failed", errors=[f"binary_diff_failed:{_error_code(binary_diff)}"], **input_fields)
        applied = runner(
            ["git", "apply", "--index", "--whitespace=nowarn", "-"],
            cwd=worktree,
            input=binary_diff.stdout,
        )
        if applied.returncode:
            return _result("failed", errors=[f"binary_apply_failed:{_error_code(applied)}"], **input_fields)
        staged = runner(["git", "diff", "--cached", "--name-status", "-z", "-M"], cwd=worktree)
        if staged.returncode:
            return _result("failed", errors=[f"staged_audit_failed:{_error_code(staged)}"], **input_fields)
        try:
            staged_paths = _parse_name_status(staged.stdout)
        except ValueError as exc:
            return _result("failed", errors=[str(exc)], **input_fields)
        if staged_paths != source_paths or any(not _path_allowed(path, allowed_paths) for path in staged_paths):
            return _result("failed", errors=["staged_paths_do_not_match_approved_source_range"], **input_fields)
        checked = runner(["git", "diff", "--cached", "--check"], cwd=worktree)
        if checked.returncode:
            return _result("failed", errors=[f"staged_whitespace_check_failed:{_error_code(checked)}"], **input_fields)
        committed = runner(["git", "commit", "-m", f"fix: replay approved #{issue_number} source range"], cwd=worktree)
        if committed.returncode:
            return _result("failed", errors=[f"replay_commit_failed:{_error_code(committed)}"], **input_fields)
        commit_proc = runner(["git", "rev-parse", "HEAD"], cwd=worktree)
        if commit_proc.returncode or not _is_sha(commit_proc.stdout.strip()):
            return _result("failed", errors=["replay_commit_sha_unavailable"], **input_fields)
        new_commit = commit_proc.stdout.strip()

        pre_push_pr, error = _read_pr(runner, root, repo, pr_number)
        if error or pre_push_pr is None or not _pr_matches(
            pre_push_pr, expected_head=expected_remote_pr_head, target_branch=target_branch
        ):
            return _result(
                "blocked",
                errors=[error or "pr_changed_before_publish"],
                new_commit_sha=new_commit,
                **input_fields,
            )
        remote = runner(["git", "ls-remote", "origin", f"refs/heads/{target_branch}"], cwd=root)
        remote_head = remote.stdout.split()[0] if remote.returncode == 0 and remote.stdout.split() else None
        if remote_head != expected_remote_pr_head:
            return _result(
                "blocked",
                errors=["remote_target_changed_before_publish"],
                new_commit_sha=new_commit,
                **input_fields,
            )
        pushed_proc = runner(["git", "push", "origin", f"{new_commit}:refs/heads/{target_branch}"], cwd=worktree)
        if pushed_proc.returncode:
            return _result(
                "failed",
                errors=[f"publish_push_failed:{_error_code(pushed_proc)}"],
                new_commit_sha=new_commit,
                **input_fields,
            )
        pushed = True
        post_push_pr, error = _read_pr(runner, root, repo, pr_number)
        if (
            error
            or post_push_pr is None
            or post_push_pr.get("headRefName") != target_branch
            or post_push_pr.get("headRefOid") != new_commit
        ):
            return _result(
                "failed",
                errors=[error or "post_publish_pr_readback_mismatch"],
                new_commit_sha=new_commit,
                **input_fields,
            )
        return _result("ok", errors=[], pushed=True, new_commit_sha=new_commit, **input_fields)
    finally:
        if worktree.exists():
            removed = runner(["git", "worktree", "remove", "--force", str(worktree)], cwd=root)
            if removed.returncode:
                cleanup_error = f"temporary_worktree_cleanup_failed:{_error_code(removed)}"
        # A return inside the try cannot be modified here.  Cleanup is still
        # best-effort, while the executor owns no other worktree or ref.
        _ = cleanup_error, pushed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--target-branch", required=True)
    parser.add_argument("--expected-remote-pr-head", required=True)
    parser.add_argument("--source-base", required=True)
    parser.add_argument("--source-head", required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = execute(
        repo=args.repo,
        issue_number=args.issue_number,
        pr_number=args.pr_number,
        target_branch=args.target_branch,
        expected_remote_pr_head=args.expected_remote_pr_head,
        source_base=args.source_base,
        source_head=args.source_head,
        project_root=args.project_root,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["PR_HEAD_REPLAY_PUBLISH_RESULT_V1"]["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
