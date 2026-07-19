#!/usr/bin/env python3
"""Materialize a live-GitHub-bound ``ISSUE_SCOPE_SNAPSHOT_V1`` artifact.

Issue #1629 fix_delta (PR review REQUEST_CHANGES) contract revision: the
snapshot artifact + provenance sidecar written to disk are audit trail ONLY.
They are never read back as an authorization source -- a caller could always
hand-write a self-consistent snapshot/sidecar pair, since both files are
produced by the same trust domain. The one authoritative output of this
module is the in-memory ``snapshot`` dict returned by :func:`materialize`,
which a consumer (``controlled_git_change_exec.build_snapshot_via_live_
materializer``) must obtain by calling this module directly, in the same
transaction as the stage/commit it authorizes, never by re-reading a
previously-written artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from controlled_git_change_exec import (  # noqa: E402
    CONTRACT_SOURCE_ISSUE_COMMENT,
    build_issue_scope_snapshot,
    compute_allowed_paths_sha256,
)

TRUSTED_REPO = "squne121/loop-protocol"
COMMAND_ID = "issue_scope_snapshot.materialize"
OUTPUT_NAME = "issue_scope_snapshot.json"
PROVENANCE_NAME = "issue_scope_snapshot.provenance.json"
PROVENANCE_SCHEMA = "ISSUE_SCOPE_SNAPSHOT_MATERIALIZER_PROVENANCE_V1"
REQUEST_SCHEMA = "ISSUE_SCOPE_SNAPSHOT_MATERIALIZE_INPUT_V1"
MAX_ARTIFACT_BYTES = 1_000_000

_CONTRACT_PARSER_REL = ".claude/skills/issue-contract-review/scripts/contract_review_result_parser.py"

# Issue #1629 fix_delta P1 (untrusted_gh_git_env): every `gh` / `git`
# subprocess this module runs MUST use a sanitized environment -- ambient
# GH_HOST / GH_REPO / GH_CONFIG_DIR can silently redirect `gh` to a different
# host/repo/config, and GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE /
# GIT_OBJECT_DIRECTORY / GIT_ALTERNATE_OBJECT_DIRECTORIES can silently
# redirect `git` to a different repository/index/object-store than `cwd`.
_GH_ENV_STRIP_KEYS: Tuple[str, ...] = ("GH_HOST", "GH_REPO", "GH_CONFIG_DIR", "GH_DEBUG", "DEBUG")
_GIT_ENV_STRIP_KEYS: Tuple[str, ...] = (
    "GIT_DIR",
    "GIT_COMMON_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_COUNT",
    "GIT_EXEC_PATH",
    "GIT_CEILING_DIRECTORIES",
)

_ISSUECOMMENT_ID_RE = re.compile(r"#issuecomment-(\d+)\Z")


def _sanitized_subprocess_env(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(base_env if base_env is not None else os.environ)
    for key in (*_GH_ENV_STRIP_KEYS, *_GIT_ENV_STRIP_KEYS):
        env.pop(key, None)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    return env


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


def _run(args: list[str], cwd: Path, env: Dict[str, str]) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=20, env=env, check=False)
    if result.returncode != 0:
        raise ValueError("binding_readback_failed")
    return result.stdout.strip()


def _atomic_write(path: Path, data: bytes) -> None:
    """Issue #1629 fix_delta P0 (stale_artifact_reuse): write to a temp file
    in the same directory and `os.replace()` it into place, so a failed
    materialize never leaves a torn/partial artifact and a concurrent reader
    never observes a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _live_default_branch(gh_bin: str, repo: str, project_root: Path, env: Dict[str, str]) -> Tuple[str, str]:
    """Issue #1629 fix_delta P1 (default_base_sha_local_not_live): resolve
    the default branch name AND its tip SHA from the GitHub REST API --
    never from the local `refs/heads/<base_ref>`, which can be stale or
    diverged from what GitHub actually considers current."""
    raw = _run([gh_bin, "api", f"repos/{repo}"], project_root, env)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("github_default_branch_readback_invalid_json") from exc
    branch = payload.get("default_branch") if isinstance(payload, dict) else None
    if not isinstance(branch, str) or not branch:
        raise ValueError("github_default_branch_missing")
    ref_raw = _run([gh_bin, "api", f"repos/{repo}/git/ref/heads/{branch}"], project_root, env)
    try:
        ref_payload = json.loads(ref_raw)
    except json.JSONDecodeError as exc:
        raise ValueError("github_default_branch_ref_invalid_json") from exc
    obj = ref_payload.get("object") if isinstance(ref_payload, dict) else None
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or len(sha) != 40:
        raise ValueError("github_default_branch_sha_invalid")
    return branch, sha


def _validate_worktree_binding(project_root: Path, worktree_path: str, branch_name: str, env: Dict[str, str]) -> Path:
    declared = Path(worktree_path)
    if not declared.is_absolute() or declared.is_symlink():
        raise ValueError("worktree_path_unsafe")
    resolved = declared.resolve()
    if resolved != project_root.resolve():
        raise ValueError("worktree_binding_mismatch")
    if _run(["git", "rev-parse", "--show-toplevel"], project_root, env) != str(resolved):
        raise ValueError("worktree_git_binding_mismatch")
    if _run(["git", "branch", "--show-current"], project_root, env) != branch_name:
        raise ValueError("branch_binding_mismatch")
    return resolved


def _load_live_issue(gh_bin: str, issue_number: int, repo: str, project_root: Path, env: Dict[str, str]) -> dict[str, Any]:
    raw = _run(
        [gh_bin, "issue", "view", str(issue_number), "--repo", repo, "--json", "body,updatedAt"],
        project_root,
        env,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("github_readback_invalid_json") from exc
    if not isinstance(payload.get("body"), str) or not isinstance(payload.get("updatedAt"), str):
        raise ValueError("github_readback_schema_invalid")
    return payload


def _fetch_comments_with_identity(
    gh_bin: str, issue_number: int, repo: str, project_root: Path, env: Dict[str, str]
) -> list[dict[str, Any]]:
    """Fetch every comment on the issue with the full GitHub identity tuple
    (id/login/type/association) needed by `contract_review_result_parser`'s
    trusted-publisher check. Fail-closed NDJSON handling mirrors
    `contract_review_result_parser.fetch_issue_comments` (#1475 P2 item 4)."""
    result = subprocess.run(
        [
            gh_bin,
            "api",
            "--paginate",
            f"repos/{repo}/issues/{issue_number}/comments?per_page=100",
            "--jq",
            ".[] | {id, html_url, created_at, updated_at, body, "
            "author: .user.login, author_id: .user.id, "
            "author_type: .user.type, author_association}",
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("github_comments_readback_failed")
    comments: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            comments.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError("github_comments_readback_incomplete") from exc
    return comments


def _load_contract_parser(project_root: Path):
    """Load the canonical, trusted-publisher-aware CONTRACT_REVIEW_RESULT_V1
    parser (`contract_review_result_parser.py`) as the single source of
    truth for trust/fingerprint validation (Issue #1629 fix_delta P1:
    contract_source_substring_check). Mirrors the realpath-checked
    importlib loading `controlled_skill_mutation_exec._readback_contract_
    snapshot` already uses for the same module."""
    parser_path = (project_root / _CONTRACT_PARSER_REL).resolve()
    if not parser_path.exists() or not parser_path.is_relative_to(project_root.resolve()):
        raise ValueError("contract_review_result_parser_module_shadowing")
    spec = importlib.util.spec_from_file_location("contract_review_result_parser", parser_path)
    if spec is None or spec.loader is None:
        raise ValueError("contract_review_result_parser_import_error")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_contract_source(
    *,
    gh_bin: str,
    repo: str,
    issue_number: int,
    contract_snapshot_url: str,
    issue_body: str,
    base_ref: str,
    base_sha: str,
    allowed_paths: list[str],
    project_root: Path,
    env: Dict[str, str],
) -> Tuple[str, str, list[dict[str, Any]]]:
    """Validate `contract_snapshot_url` against a live re-fetch of every
    issue comment, using the canonical parser's trusted-publisher +
    source-bound-fingerprint checks (Issue #1629 fix_delta P1:
    contract_source_substring_check). Cross-checks the fingerprint's
    `base_ref` / `base_sha_at_snapshot` /
    `allowed_paths_normalized_sha256` against the live values
    this materialize() call independently computed (P1:
    default_base_sha_local_not_live). Returns
    (contract_source_id, contract_source_body, comment_bodies_in_order)."""
    match = _ISSUECOMMENT_ID_RE.search(contract_snapshot_url)
    if not match:
        raise ValueError("contract_source_id_invalid")
    comment_id = int(match.group(1))

    parser_mod = _load_contract_parser(project_root)
    comments = _fetch_comments_with_identity(gh_bin, issue_number, repo, project_root, env)
    issue_url = f"https://github.com/{repo}/issues/{issue_number}"
    results = parser_mod.parse_contract_review_results(comments, issue_url)
    entry = next((r for r in results if r.get("comment_id") == comment_id), None)
    if entry is None:
        raise ValueError("contract_source_not_found")
    if entry.get("html_url") != contract_snapshot_url:
        raise ValueError("contract_source_url_mismatch")
    if entry.get("status") != "go":
        raise ValueError("contract_source_not_go")
    if entry.get("is_trusted_author") is not True:
        raise ValueError("contract_source_untrusted_author")
    if entry.get("is_fingerprint_ready") is not True:
        raise ValueError("contract_source_fingerprint_not_ready")

    inner = entry.get("inner") or {}
    if inner.get("body_sha256") != _sha256_bytes(issue_body.encode("utf-8")):
        raise ValueError("contract_source_drift")

    fingerprint = inner.get("expected_contract_fingerprint") or {}
    if fingerprint.get("base_ref") != base_ref:
        raise ValueError("base_ref_fingerprint_mismatch")
    if fingerprint.get("base_sha_at_snapshot") != base_sha:
        raise ValueError("base_sha_fingerprint_mismatch")
    if fingerprint.get("allowed_paths_normalized_sha256") != compute_allowed_paths_sha256(allowed_paths):
        raise ValueError("allowed_paths_fingerprint_mismatch")

    comment_obj = next((c for c in comments if c.get("id") == comment_id), None)
    if comment_obj is None or not isinstance(comment_obj.get("body"), str):
        raise ValueError("contract_source_body_unavailable")

    ordered_bodies = [str(comment.get("body", "")) for comment in comments]
    return str(comment_id), comment_obj["body"], ordered_bodies


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
    gh_bin: str,
    env: Optional[Dict[str, str]] = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    if repo != TRUSTED_REPO or issue_number <= 0:
        raise ValueError("repo_or_issue_binding_mismatch")
    if not gh_bin:
        raise ValueError("gh_bin_required")
    root = (project_root or _project_root()).resolve()
    sanitized_env = _sanitized_subprocess_env(env)
    source_prefix = f"https://github.com/{repo}/issues/{issue_number}#issuecomment-"
    if not contract_snapshot_url.startswith(source_prefix):
        raise ValueError("contract_source_url_binding_mismatch")
    target = _validate_output_path(root, issue_number, output)
    worktree = _validate_worktree_binding(root, worktree_path, branch_name, sanitized_env)
    live_default_branch, base_sha = _live_default_branch(gh_bin, repo, root, sanitized_env)
    if live_default_branch != base_ref:
        raise ValueError("base_ref_not_default_branch")
    live_issue = _load_live_issue(gh_bin, issue_number, repo, root, sanitized_env)
    allowed_paths = _allowed_paths(live_issue["body"])
    source_id, source_body, comment_bodies = _validate_contract_source(
        gh_bin=gh_bin,
        repo=repo,
        issue_number=issue_number,
        contract_snapshot_url=contract_snapshot_url,
        issue_body=live_issue["body"],
        base_ref=base_ref,
        base_sha=base_sha,
        allowed_paths=allowed_paths,
        project_root=root,
        env=sanitized_env,
    )
    snapshot = build_issue_scope_snapshot(
        repository_full_name=repo,
        issue_number=issue_number,
        contract_source_kind=CONTRACT_SOURCE_ISSUE_COMMENT,
        contract_source_id=source_id,
        contract_source_body=source_body,
        issue_body=live_issue["body"],
        issue_updated_at=live_issue["updatedAt"],
        comment_bodies=comment_bodies,
        allowed_paths=allowed_paths,
        base_ref=base_ref,
        base_sha=base_sha,
        branch_ref=f"refs/heads/{branch_name}",
        worktree_path=str(worktree),
    )
    snapshot_dict = snapshot.to_dict()
    snapshot_bytes = (json.dumps(snapshot_dict, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
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
        "audit_trail_only": True,
    }
    provenance_bytes = (json.dumps(provenance, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    provenance_path = expected_provenance_path(root, issue_number)
    # Issue #1629 fix_delta P0 (stale_artifact_reuse): atomic, same-filesystem
    # os.replace() -- never a partially-written artifact on failure, and any
    # pre-existing artifact from a prior successful materialize is only
    # swapped once BOTH the snapshot and its sidecar have been fully staged
    # to temp files and are ready to replace.
    _atomic_write(target, snapshot_bytes)
    _atomic_write(provenance_path, provenance_bytes)
    return {
        "schema": "ISSUE_SCOPE_SNAPSHOT_MATERIALIZE_RESULT_V1",
        "status": "ok",
        "issue_number": issue_number,
        "snapshot_path": output,
        "provenance_path": str(provenance_path.relative_to(root)),
        "snapshot_sha256": provenance["artifact_sha256"],
        "base_sha": base_sha,
        # Issue #1629 fix_delta P0 (provenance_self_attestation): the ONLY
        # authoritative output. Consumers must use this in-memory dict, never
        # re-read `snapshot_path` / `provenance_path` for authorization.
        "snapshot": snapshot_dict,
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
    parser.add_argument("--gh-bin", required=True)
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
            gh_bin=args.gh_bin,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(
            json.dumps(
                {
                    "schema": "ISSUE_SCOPE_SNAPSHOT_MATERIALIZE_RESULT_V1",
                    "status": "denied",
                    "reason_code": str(exc),
                }
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
