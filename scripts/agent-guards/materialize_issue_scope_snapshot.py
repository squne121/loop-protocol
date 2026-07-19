#!/usr/bin/env python3
"""Materialize a live-GitHub-bound ``ISSUE_SCOPE_SNAPSHOT_V1`` artifact.

The controlled stage/commit executor intentionally keeps
``ISSUE_SCOPE_SNAPSHOT_V1`` stable.  This producer therefore writes the
unchanged snapshot beside a provenance sidecar in the command-id-scoped
artifact directory.  Consumers reject snapshots that do not have that
sidecar and fixed location.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from controlled_git_change_exec import (  # noqa: E402
    CONTRACT_SOURCE_ISSUE_COMMENT,
    IssueScopeSnapshot,
    build_issue_scope_snapshot,
)

TRUSTED_REPO = "squne121/loop-protocol"
COMMAND_ID = "issue_scope_snapshot.materialize"
OUTPUT_NAME = "issue_scope_snapshot.json"
PROVENANCE_NAME = "issue_scope_snapshot.provenance.json"
PROVENANCE_SCHEMA = "ISSUE_SCOPE_SNAPSHOT_MATERIALIZER_PROVENANCE_V1"
REQUEST_SCHEMA = "ISSUE_SCOPE_SNAPSHOT_MATERIALIZE_INPUT_V1"
MAX_ARTIFACT_BYTES = 1_000_000


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _project_root() -> Path:
    return _HERE.parent.parent


def artifact_dir(project_root: Path, issue_number: int) -> Path:
    return project_root / "artifacts" / str(issue_number) / "issue-metadata" / COMMAND_ID


def expected_output_path(project_root: Path, issue_number: int) -> Path:
    return artifact_dir(project_root, issue_number) / OUTPUT_NAME


def expected_provenance_path(project_root: Path, issue_number: int) -> Path:
    return artifact_dir(project_root, issue_number) / PROVENANCE_NAME


def _is_regular_nonlinked(path: Path) -> bool:
    try:
        entry = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1 and not path.is_symlink()


def _ensure_no_symlink_components(project_root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("artifact_path_outside_project") from exc
    cursor = project_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            if cursor.is_symlink():
                raise ValueError("artifact_path_symlink_rejected")


def _validate_output_path(project_root: Path, issue_number: int, output: str) -> Path:
    raw = PurePosixPath(output)
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError("unsafe_artifact_path")
    expected = expected_output_path(project_root, issue_number)
    candidate = (project_root / raw).resolve(strict=False)
    if candidate != expected.resolve(strict=False):
        raise ValueError("artifact_path_binding_mismatch")
    _ensure_no_symlink_components(project_root, expected.parent)
    for path in (expected, expected_provenance_path(project_root, issue_number)):
        if path.exists() and not _is_regular_nonlinked(path):
            raise ValueError("artifact_path_existing_file_unsafe")
    return expected


def _run(args: list[str], cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=20, check=False)
    if result.returncode != 0:
        raise ValueError("binding_readback_failed")
    return result.stdout.strip()


def _default_branch_sha(project_root: Path, base_ref: str) -> str:
    default_branch = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], project_root)
    if default_branch.startswith("origin/"):
        default_branch = default_branch.removeprefix("origin/")
    if base_ref != default_branch:
        raise ValueError("base_ref_not_default_branch")
    sha = _run(["git", "rev-parse", base_ref], project_root)
    if len(sha) != 40:
        raise ValueError("base_sha_invalid")
    return sha


def _validate_worktree_binding(project_root: Path, worktree_path: str, branch_name: str) -> Path:
    declared = Path(worktree_path)
    if not declared.is_absolute() or declared.is_symlink():
        raise ValueError("worktree_path_unsafe")
    resolved = declared.resolve()
    if resolved != project_root.resolve():
        raise ValueError("worktree_binding_mismatch")
    if _run(["git", "rev-parse", "--show-toplevel"], project_root) != str(resolved):
        raise ValueError("worktree_git_binding_mismatch")
    if _run(["git", "branch", "--show-current"], project_root) != branch_name:
        raise ValueError("branch_binding_mismatch")
    return resolved


def _load_live_issue(issue_number: int, repo: str, project_root: Path) -> dict[str, Any]:
    raw = _run(
        ["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "body,updatedAt,comments"],
        project_root,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("github_readback_invalid_json") from exc
    if not isinstance(payload.get("body"), str) or not isinstance(payload.get("updatedAt"), str):
        raise ValueError("github_readback_schema_invalid")
    if not isinstance(payload.get("comments"), list):
        raise ValueError("github_comments_schema_invalid")
    return payload


def _source_comment(live_issue: dict[str, Any], source_url: str, issue_body: str) -> tuple[str, str]:
    comments = [comment for comment in live_issue["comments"] if comment.get("url") == source_url]
    if len(comments) != 1 or not isinstance(comments[0].get("body"), str):
        raise ValueError("contract_source_not_found")
    body = comments[0]["body"]
    if "CONTRACT_REVIEW_RESULT_V1:" not in body or "status: go" not in body:
        raise ValueError("contract_source_not_go")
    marker = "body_sha256: \""
    start = body.find(marker)
    if start < 0:
        raise ValueError("contract_source_body_sha_missing")
    start += len(marker)
    end = body.find('"', start)
    if end < 0 or body[start:end] != _sha256_bytes(issue_body.encode("utf-8")):
        raise ValueError("contract_source_drift")
    source_id = source_url.rsplit("-", 1)[-1]
    if not source_id.isdigit():
        raise ValueError("contract_source_id_invalid")
    return source_id, body


def _allowed_paths(issue_body: str) -> list[str]:
    marker = "## Allowed Paths"
    start = issue_body.find(marker)
    if start < 0:
        raise ValueError("allowed_paths_missing")
    section = issue_body[start + len(marker):]
    next_heading = section.find("\n## ")
    if next_heading >= 0:
        section = section[:next_heading]
    paths = [line[2:].strip() for line in section.splitlines() if line.startswith("- ")]
    if not paths:
        raise ValueError("allowed_paths_empty")
    return paths


def materialize(
    *,
    issue_number: int,
    repo: str,
    contract_snapshot_url: str,
    base_ref: str,
    branch_name: str,
    worktree_path: str,
    output: str,
    project_root: Path | None = None,
) -> dict[str, Any]:
    if repo != TRUSTED_REPO or issue_number <= 0:
        raise ValueError("repo_or_issue_binding_mismatch")
    root = (project_root or _project_root()).resolve()
    source_prefix = f"https://github.com/{repo}/issues/{issue_number}#issuecomment-"
    if not contract_snapshot_url.startswith(source_prefix):
        raise ValueError("contract_source_url_binding_mismatch")
    target = _validate_output_path(root, issue_number, output)
    worktree = _validate_worktree_binding(root, worktree_path, branch_name)
    base_sha = _default_branch_sha(root, base_ref)
    live_issue = _load_live_issue(issue_number, repo, root)
    source_id, source_body = _source_comment(live_issue, contract_snapshot_url, live_issue["body"])
    snapshot = build_issue_scope_snapshot(
        repository_full_name=repo,
        issue_number=issue_number,
        contract_source_kind=CONTRACT_SOURCE_ISSUE_COMMENT,
        contract_source_id=source_id,
        contract_source_body=source_body,
        issue_body=live_issue["body"],
        issue_updated_at=live_issue["updatedAt"],
        comment_bodies=[str(comment.get("body", "")) for comment in live_issue["comments"]],
        allowed_paths=_allowed_paths(live_issue["body"]),
        base_ref=base_ref,
        base_sha=base_sha,
        branch_ref=f"refs/heads/{branch_name}",
        worktree_path=str(worktree),
    )
    snapshot_bytes = (json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(snapshot_bytes) > MAX_ARTIFACT_BYTES:
        raise ValueError("snapshot_artifact_oversized")
    provenance = {
        "schema": PROVENANCE_SCHEMA,
        "producer": "scripts/agent-guards/materialize_issue_scope_snapshot.py",
        "command_id": COMMAND_ID,
        "repository_full_name": repo,
        "issue_number": issue_number,
        "contract_snapshot_url": contract_snapshot_url,
        "artifact_path": output,
        "artifact_sha256": _sha256_bytes(snapshot_bytes),
        "worktree_realpath": str(worktree),
        "branch_ref": f"refs/heads/{branch_name}",
        "base_ref": base_ref,
        "base_sha": base_sha,
    }
    provenance_bytes = (json.dumps(provenance, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    provenance_path = expected_provenance_path(root, issue_number)
    try:
        target.write_bytes(snapshot_bytes)
        provenance_path.write_bytes(provenance_bytes)
    except Exception:
        for path in (target, provenance_path):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return {
        "schema": "ISSUE_SCOPE_SNAPSHOT_MATERIALIZE_RESULT_V1",
        "status": "ok",
        "issue_number": issue_number,
        "snapshot_path": output,
        "provenance_path": str(provenance_path.relative_to(root)),
        "snapshot_sha256": provenance["artifact_sha256"],
        "base_sha": base_sha,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Materialize a live GitHub-bound issue scope snapshot")
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--contract-snapshot-url", required=True)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--branch-name", required=True)
    parser.add_argument("--worktree-path", required=True)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = materialize(
            issue_number=args.issue_number,
            repo=args.repo,
            contract_snapshot_url=args.contract_snapshot_url,
            base_ref=args.base_ref,
            branch_name=args.branch_name,
            worktree_path=args.worktree_path,
            output=args.output,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"schema": "ISSUE_SCOPE_SNAPSHOT_MATERIALIZE_RESULT_V1", "status": "denied", "reason_code": str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
