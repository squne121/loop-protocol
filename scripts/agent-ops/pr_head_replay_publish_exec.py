#!/usr/bin/env python3
"""Fail-closed replay/publish executor for an approved PR source range.

The executor deliberately has no merge, rebase, reset, or force-push path.
It reproduces one reviewed source range in an executor-owned detached worktree,
then performs one expected-old SHA lease guarded refspec push after two
independent head checks.
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


def _live_ref_sha(runner: Runner, root: Path, ref: str) -> str | None:
    proc = runner(["git", "ls-remote", "origin", f"refs/heads/{ref}"], cwd=root)
    parts = proc.stdout.split() if proc.returncode == 0 else []
    return parts[0] if parts else None


def _merge_tree_conflicts(runner: Runner, root: Path, base_sha: str, candidate_sha: str) -> bool:
    """Deterministic real-conflict oracle (Issue #2102 P1-C).

    Uses ``git merge-tree --write-tree`` (worktree/index-free) to decide
    whether ``candidate_sha`` conflicts against ``base_sha``. This replaces
    caller-supplied boolean ``semantic_ambiguity`` flags with an actual Git
    merge computation. A nonzero exit from the two-ref form of
    ``merge-tree --write-tree`` means the merge produced conflicts.
    """
    proc = runner(["git", "merge-tree", "--write-tree", base_sha, candidate_sha], cwd=root)
    return proc.returncode != 0


def compute_semantic_ambiguity(
    base_sha: str,
    candidate_sha: str,
    *,
    cwd: Path,
    runner: Runner = _run,
) -> bool:
    """Public export of the deterministic real-conflict oracle (Issue #2102
    fix_delta iteration 5, Blocker B).

    ``route_loop_verdict_v2.py`` (a pure, side-effect-free module by design;
    see its module docstring) has no subprocess authority of its own, so it
    cannot compute ``semantic_ambiguity`` itself. This is the exact-file
    Allowed Paths counterpart that CAN: it wraps ``_merge_tree_conflicts()``
    (unchanged) as a stable public entry point a caller-side wrapper in
    ``route_loop_verdict_v2.py`` imports, rather than duplicating the
    ``git merge-tree --write-tree`` probe.
    """
    return _merge_tree_conflicts(runner, cwd, base_sha, candidate_sha)


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
    current_base_branch: str | None = None,
    expected_current_base_sha: str | None = None,
) -> dict[str, Any]:
    """Replay one source range and publish it only under exact-head guards.

    When ``current_base_branch`` / ``expected_current_base_sha`` are both
    supplied (Issue #2102 main-drift reconciliation path), the executor
    additionally: (1) re-reads the live current-base SHA before building the
    candidate and fails closed on drift, (2) runs a deterministic
    ``git merge-tree`` conflict check between the current base and the
    candidate final head instead of trusting a caller-supplied boolean, and
    (3) recomputes the ``current_base_sha..candidate_head`` final net diff
    and re-validates Allowed Paths against it (``candidate_final_net_diff``),
    which is a distinct field from the source range diff used by the
    unconditional path above. When these two arguments are omitted, behavior
    is unchanged (backward compatible).
    """
    input_fields = {
        "repo": repo,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "target_branch": target_branch,
        "expected_remote_pr_head": expected_remote_pr_head,
        "source_base": source_base,
        "source_head": source_head,
    }
    main_drift_reconciliation = bool(current_base_branch and expected_current_base_sha)
    if main_drift_reconciliation and not _is_sha(expected_current_base_sha or ""):
        return _result("blocked", errors=["invalid_arguments"], **input_fields)
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

    if main_drift_reconciliation:
        base_fetch = runner(["git", "fetch", "origin", f"refs/heads/{current_base_branch}"], cwd=root)
        if base_fetch.returncode:
            return _result(
                "blocked", errors=[f"current_base_fetch_failed:{_error_code(base_fetch)}"], **input_fields
            )
        live_base_sha = _live_ref_sha(runner, root, current_base_branch or "")
        if live_base_sha != expected_current_base_sha:
            return _result(
                "blocked",
                errors=["current_base_drift_before_publish"],
                live_current_base_sha=live_base_sha,
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

        candidate_final_net_diff: list[str] | None = None
        if main_drift_reconciliation:
            # Deterministic real-conflict oracle (P1-C) instead of a
            # caller-supplied boolean semantic_ambiguity flag.
            if _merge_tree_conflicts(runner, root, expected_current_base_sha or "", new_commit):
                return _result(
                    "blocked",
                    errors=["current_base_merge_conflict"],
                    new_commit_sha=new_commit,
                    **input_fields,
                )
            # Final net diff (current_base_sha..candidate_head), distinct from
            # the source-range diff above (P1-B). Allowed Paths must hold
            # against this final diff, not only the applied source range.
            final_diff_proc = runner(
                ["git", "diff", "--name-status", "-z", "-M", expected_current_base_sha or "", new_commit],
                cwd=root,
            )
            if final_diff_proc.returncode:
                return _result(
                    "failed",
                    errors=[f"candidate_final_net_diff_failed:{_error_code(final_diff_proc)}"],
                    new_commit_sha=new_commit,
                    **input_fields,
                )
            try:
                final_diff_paths = _parse_name_status(final_diff_proc.stdout)
            except ValueError as exc:
                return _result("failed", errors=[str(exc)], new_commit_sha=new_commit, **input_fields)
            candidate_final_net_diff = sorted(final_diff_paths)
            final_disallowed = sorted(
                path for path in final_diff_paths if not _path_allowed(path, allowed_paths)
            )
            if final_disallowed:
                return _result(
                    "blocked",
                    errors=["candidate_final_net_diff_contains_disallowed_path"],
                    disallowed_paths=final_disallowed,
                    candidate_final_net_diff=candidate_final_net_diff,
                    new_commit_sha=new_commit,
                    **input_fields,
                )

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
        # Bind the preflight observation to the ref update itself. The
        # ordinary fast-forward check is not a substitute for this CAS guard:
        # another fast-forward update can occur after ``ls-remote`` and before
        # this push. This replay is a child of expected_remote_pr_head, so the
        # lease never broadens the executor into a raw force-push path.
        expected_old_lease = f"refs/heads/{target_branch}:{expected_remote_pr_head}"
        pushed_proc = runner(
            [
                "git",
                "push",
                f"--force-with-lease={expected_old_lease}",
                "origin",
                f"{new_commit}:refs/heads/{target_branch}",
            ],
            cwd=worktree,
        )
        if pushed_proc.returncode:
            changed_remote = runner(["git", "ls-remote", "origin", f"refs/heads/{target_branch}"], cwd=root)
            changed_remote_head = (
                changed_remote.stdout.split()[0]
                if changed_remote.returncode == 0 and changed_remote.stdout.split()
                else None
            )
            if changed_remote_head != expected_remote_pr_head:
                return _result(
                    "blocked",
                    errors=["remote_target_changed_during_publish"],
                    new_commit_sha=new_commit,
                    **input_fields,
                )
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
        return _result(
            "ok",
            errors=[],
            pushed=True,
            new_commit_sha=new_commit,
            candidate_final_net_diff=candidate_final_net_diff,
            **input_fields,
        )
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
    parser.add_argument(
        "--current-base-branch",
        default=None,
        help="Enables main-drift reconciliation checks (Issue #2102) when combined with --expected-current-base-sha.",
    )
    parser.add_argument("--expected-current-base-sha", default=None)
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
        current_base_branch=args.current_base_branch,
        expected_current_base_sha=args.expected_current_base_sha,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["PR_HEAD_REPLAY_PUBLISH_RESULT_V1"]["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
