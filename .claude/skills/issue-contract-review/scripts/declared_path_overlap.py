#!/usr/bin/env python3
"""
declared_path_overlap.py

OPEN PR 一覧の完全性契約付き inventory 取得と、対象 Issue の Allowed Paths
との changed-file 名の単純な重なり（``declared_path_overlap``）の算出。

Issue #1680 の設計:

- これは実 Git merge 競合（3-way merge / hunk 競合 / rename・delete 競合）の
  証明ではない。changed-file 名の単純な重複を **advisory のみ** で記録する。
- 実 Git merge 競合の観測・blocking 判定は独立 producer（Issue #1792、
  ``PAIRWISE_MERGE_OBSERVATION_V1``）とその呼び出し元配線（Issue #1793）に
  分離済みであり、本モジュールの責務ではない。
- 単独では blocking にしない。呼び出し側（``run_contract_review_once.py``）
  はこの check の結果によって status を ``blocked`` / ``human_judgment`` に
  変えてはならない。

OPEN PR inventory の完全性契約（Issue #1680 In Scope）:

- ``state``: 常に ``open`` 固定（GraphQL ``states: OPEN`` 相当）
- ``base_ref``: 明示的にフィルタする場合のみ指定。フィルタしない場合も、
  各 PR エントリは自身の ``base_ref_name`` を明示的に保持する
- ``draft_policy``: ``include_drafts`` — draft PR も inventory に含める
  （各 PR エントリの ``is_draft`` で識別可能）
- fork PR は ``head_repository_owner`` / ``head_repository_name`` /
  ``head_ref_oid`` / ``is_cross_repository`` で識別する
- pagination は GraphQL cursor（``hasNextPage`` が ``false`` になるまで）で
  行い、``totalCount`` と ``fetched_count`` を cross-check する
- ``complete``: ``fetched_count == totalCount`` かつ ``has_next_page is False``
  かつエラーなしの場合のみ ``True``
- ``limit`` は safety cap（既定 200）。cap に到達しても ``has_next_page`` が
  ``True`` のままの場合は ``saturated: True`` とし、``complete`` を ``False``
  に倒す（fail-closed、全件性を証明できない場合は complete を騙らない）
- ``inventory_digest``: 収集した PR number / head_ref_oid の sha256 digest
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
# parents: [0]=issue-contract-review, [1]=skills, [2]=.claude, [3]=<repo root>
_REPO_ROOT = _SCRIPTS_DIR.parents[3]
_CHECK_ISSUE_OVERLAP_PY = (
    _REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts" / "check_issue_overlap.py"
)

_DEFAULT_TIMEOUT = 30
_DEFAULT_LIMIT = 200
_PAGE_SIZE = 50

OPEN_PR_INVENTORY_SCHEMA = "OPEN_PR_INVENTORY_V1"
DECLARED_PATH_OVERLAP_SCHEMA = "declared_path_overlap/v1"

_OPEN_PR_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: %d, after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        baseRefName
        headRefName
        headRefOid
        isDraft
        isCrossRepository
        headRepositoryOwner { login }
        headRepository { name }
        url
      }
    }
  }
}
""" % _PAGE_SIZE


def _load_path_normalizer() -> Optional[Any]:
    """check_issue_overlap.py の pure path normalizer を正本として再利用する。"""
    import importlib.util

    if not _CHECK_ISSUE_OVERLAP_PY.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "check_issue_overlap_for_declared_path_overlap", _CHECK_ISSUE_OVERLAP_PY
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Python 3.12 dataclass 実装は cls.__module__ を sys.modules から解決する
    # ため、exec_module 前に sys.modules へ登録しておかないと frozen
    # dataclass 定義時に AttributeError になる (dataclasses._is_type)。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _run_gh_graphql(
    query: str, variables: dict[str, Any], timeout: int = _DEFAULT_TIMEOUT
) -> tuple[Optional[dict], Optional[str]]:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        cmd.extend(["-F", f"{key}={value}"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except FileNotFoundError:
        return None, "gh_not_found"
    except OSError as exc:
        return None, f"subprocess_error: {exc}"
    if result.returncode != 0:
        return None, f"gh_error: {result.stderr.strip()[:200]}"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"json_parse_error: {exc}"


def fetch_open_pr_inventory(
    repo: str,
    base_ref: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """OPEN PR 一覧を完全性契約付きで取得する（``OPEN_PR_INVENTORY_V1``）。"""
    parts = repo.split("/")
    if len(parts) != 2:
        return {
            "schema": OPEN_PR_INVENTORY_SCHEMA,
            "repo": repo,
            "state": "open",
            "base_ref": base_ref,
            "draft_policy": "include_drafts",
            "prs": [],
            "fetched_count": 0,
            "totalCount": None,
            "has_next_page": None,
            "complete": False,
            "saturated": False,
            "limit": limit,
            "page_count": 0,
            "inventory_digest": None,
            "errors": ["invalid_repo_format"],
        }
    owner, name = parts

    prs: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    total_count: Optional[int] = None
    has_next_page = True
    errors: list[str] = []
    page_count = 0

    while has_next_page and len(prs) < limit:
        variables: dict[str, Any] = {"owner": owner, "name": name}
        if cursor:
            variables["cursor"] = cursor
        payload, err = _run_gh_graphql(_OPEN_PR_QUERY, variables)
        page_count += 1
        if err:
            errors.append(err)
            has_next_page = False
            break
        try:
            pr_connection = payload["data"]["repository"]["pullRequests"]  # type: ignore[index]
        except (KeyError, TypeError):
            errors.append("malformed_graphql_response")
            has_next_page = False
            break
        total_count = pr_connection.get("totalCount")
        nodes = pr_connection.get("nodes") or []
        for node in nodes:
            head_owner = (node.get("headRepositoryOwner") or {}).get("login")
            head_repo_name = (node.get("headRepository") or {}).get("name")
            prs.append(
                {
                    "number": node.get("number"),
                    "title": node.get("title"),
                    "base_ref_name": node.get("baseRefName"),
                    "head_ref_name": node.get("headRefName"),
                    "head_ref_oid": node.get("headRefOid"),
                    "is_draft": node.get("isDraft"),
                    "is_cross_repository": node.get("isCrossRepository"),
                    "head_repository_owner": head_owner,
                    "head_repository_name": head_repo_name,
                    "url": node.get("url"),
                }
            )
        page_info = pr_connection.get("pageInfo") or {}
        has_next_page = bool(page_info.get("hasNextPage"))
        cursor = page_info.get("endCursor")

    if base_ref:
        prs = [pr for pr in prs if pr.get("base_ref_name") == base_ref]

    saturated = has_next_page and len(prs) >= limit
    fetched_count = len(prs)
    # base_ref フィルタ適用時は fetched_count は totalCount と一致しないのが
    # 正常なので cross-check の対象外にする（フィルタ前の全件取得が
    # has_next_page=False で完了していれば complete とみなす）。
    complete = (
        not errors
        and not saturated
        and not has_next_page
        and total_count is not None
        and (base_ref is not None or fetched_count == total_count)
    )

    digest_source = json.dumps(
        sorted((pr["number"], pr.get("head_ref_oid")) for pr in prs if pr.get("number") is not None),
        sort_keys=True,
    )
    inventory_digest = "sha256:" + hashlib.sha256(digest_source.encode("utf-8")).hexdigest()

    return {
        "schema": OPEN_PR_INVENTORY_SCHEMA,
        "repo": repo,
        "state": "open",
        "base_ref": base_ref,
        "draft_policy": "include_drafts",
        "prs": prs,
        "fetched_count": fetched_count,
        "totalCount": total_count,
        "has_next_page": has_next_page,
        "complete": complete,
        "saturated": saturated,
        "limit": limit,
        "page_count": page_count,
        "inventory_digest": inventory_digest,
        "errors": errors,
    }


def fetch_pr_changed_files(
    pr_number: int, repo: str, timeout: int = _DEFAULT_TIMEOUT
) -> tuple[list[str], Optional[str]]:
    """``gh pr diff --name-only`` 相当。ファイル名のみを返す（overlap の証明ではない）。"""
    try:
        result = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--repo", repo, "--name-only"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [], "timeout"
    except FileNotFoundError:
        return [], "gh_not_found"
    except OSError as exc:
        return [], f"subprocess_error: {exc}"
    if result.returncode != 0:
        return [], f"gh_error: {result.stderr.strip()[:200]}"
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return files, None


def _unavailable_result(reason: str) -> dict[str, Any]:
    return {
        "schema": DECLARED_PATH_OVERLAP_SCHEMA,
        "advisory": True,
        "blocking": False,
        "decision": "unavailable",
        "disjoint": None,
        "overlapping_prs": [],
        "inventory": None,
        "errors": [reason],
        "note": (
            "changed-file 名の単純な重なりのみを証明する advisory check。"
            "実 Git merge 競合の証明ではなく、単独では blocking にしない。"
        ),
    }


def compute_declared_path_overlap(
    allowed_paths: list[str],
    repo: str,
    base_ref: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """``declared_path_overlap``（advisory のみ）を算出する。

    対象 Issue の Allowed Paths と OPEN PR の changed-file 名が
    ``disjoint``（重なりなし）であれば ``overlapping_prs`` は空になる。
    これは実 Git merge 競合の証明ではない。3-way merge・hunk 競合・
    rename/delete 競合は評価しない。単独では blocking にしない
    （advisory のみ、記録専用 — Issue #1792 / #1793 に分離済み）。
    """
    normalizer = _load_path_normalizer()
    if normalizer is None:
        return _unavailable_result("path_normalizer_unavailable")

    if not allowed_paths:
        return _unavailable_result("no_allowed_paths")

    inventory = fetch_open_pr_inventory(repo, base_ref=base_ref, limit=limit)
    normalized_allowed = normalizer.normalize_paths(allowed_paths)

    overlapping_prs: list[dict[str, Any]] = []
    errors = list(inventory.get("errors", []))

    for pr in inventory.get("prs", []):
        pr_number = pr.get("number")
        if pr_number is None:
            continue
        changed_files, err = fetch_pr_changed_files(pr_number, repo)
        if err:
            errors.append(f"pr_{pr_number}_changed_files_error: {err}")
            continue
        matched_files = [
            path
            for path in changed_files
            if any(normalizer.paths_conflict(path, allowed) for allowed in normalized_allowed)
        ]
        if matched_files:
            overlapping_prs.append(
                {
                    "pr_number": pr_number,
                    "url": pr.get("url"),
                    "head_ref_oid": pr.get("head_ref_oid"),
                    "is_draft": pr.get("is_draft"),
                    "is_cross_repository": pr.get("is_cross_repository"),
                    "matched_files": matched_files,
                }
            )

    disjoint = len(overlapping_prs) == 0

    return {
        "schema": DECLARED_PATH_OVERLAP_SCHEMA,
        "advisory": True,
        "blocking": False,
        "decision": "advisory_only",
        "disjoint": disjoint,
        "overlapping_prs": overlapping_prs,
        "inventory": inventory,
        "errors": errors,
        "note": (
            "changed-file 名の単純な重なりのみを証明する advisory check。"
            "3-way merge・hunk 競合・rename/delete 競合は評価しない"
            "（実 Git merge 競合の判定は Issue #1792 の "
            "PAIRWISE_MERGE_OBSERVATION_V1 producer の責務）。"
            "単独では blocking にしない。"
        ),
    }


def compute_declared_path_overlap_for_issue(
    issue_number: int,
    repo: str,
    base_ref: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Issue 本文から Allowed Paths を readback し ``declared_path_overlap`` を返す。"""
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "body"],
            capture_output=True,
            text=True,
            timeout=_DEFAULT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return _unavailable_result("issue_body_fetch_timeout")
    except FileNotFoundError:
        return _unavailable_result("gh_not_found")
    except OSError as exc:
        return _unavailable_result(f"issue_body_fetch_error: {exc}")
    if result.returncode != 0:
        return _unavailable_result(f"issue_body_fetch_gh_error: {result.stderr.strip()[:200]}")
    try:
        body = json.loads(result.stdout).get("body", "")
    except json.JSONDecodeError as exc:
        return _unavailable_result(f"issue_body_json_parse_error: {exc}")

    normalizer = _load_path_normalizer()
    if normalizer is None:
        return _unavailable_result("path_normalizer_unavailable")

    allowed_paths = normalizer.extract_allowed_paths(body)
    return compute_declared_path_overlap(
        allowed_paths=allowed_paths,
        repo=repo,
        base_ref=base_ref,
        limit=limit,
    )
