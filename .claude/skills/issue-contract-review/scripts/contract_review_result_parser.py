#!/usr/bin/env python3
"""
contract_review_result_parser.py

Issue コメントから valid CONTRACT_REVIEW_RESULT_V1 を解析する共有 parser。
#66 との統合を見越した canonical entry point。

Integration note (#66):
  Issue #66 で将来的に contract result の canonical storage が変更された場合、
  このモジュールを更新することで downstream consumers (run_contract_review_once.py,
  ensure_contract_snapshot.py 等) への影響を最小化する。
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Optional


# ---------------------------------------------------------------------------
# GitHub comment fetching
# ---------------------------------------------------------------------------


def fetch_issue_comments(
    issue_number: int, repo: str, timeout: int = 20
) -> tuple[list[dict], Optional[str]]:
    """
    Fetch all issue comments via gh CLI with pagination.
    Returns (comments_list, error_code_or_None).
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{repo}/issues/{issue_number}/comments?per_page=100",
                "--jq",
                '.[] | {id, html_url, created_at, updated_at, body}',
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr.lower()
            if "not authenticated" in stderr or "authentication failed" in stderr:
                return [], "gh_auth_failed"
            if "not found" in stderr or "could not resolve" in stderr:
                return [], "gh_not_found"
            return [], "gh_other_error"
        # --jq with .[] produces one JSON object per line (NDJSON)
        comments: list[dict] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    comments.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return comments, None
    except subprocess.TimeoutExpired:
        return [], "gh_timeout"
    except Exception:
        return [], "gh_other_error"


# ---------------------------------------------------------------------------
# YAML block extraction
# ---------------------------------------------------------------------------

_FENCED_YAML_RE = re.compile(
    r"```ya?ml[ \t]*\n(.*?)```",
    re.DOTALL,
)

_CONTRACT_REVIEW_MARKER = "CONTRACT_REVIEW_RESULT_V1"


def _extract_yaml_blocks(body: str) -> list[str]:
    """Extract all fenced yaml/yml block contents from a comment body."""
    return [m.group(1) for m in _FENCED_YAML_RE.finditer(body)]


def _parse_simple_yaml_block(block: str) -> dict[str, Any]:
    """
    Minimal YAML key-value parser for flat + nested blocks.
    Only handles string and bool scalar values at depth 1-2.
    Does NOT import yaml to avoid external deps.
    """
    result: dict[str, Any] = {}
    lines = block.splitlines()
    current_key: Optional[str] = None
    current_indent: Optional[int] = None

    for line in lines:
        stripped = line.rstrip()
        if not stripped or stripped.lstrip().startswith("#"):
            continue

        # Detect indent level
        indent = len(line) - len(line.lstrip())

        # Top-level key: value
        if indent == 0:
            current_key = None
            current_indent = None
            m = re.match(r'^(\S[^:]*?):\s*(.*)', stripped)
            if m:
                key = m.group(1).strip()
                val = m.group(2).strip()
                if val:
                    # Remove surrounding quotes
                    if (val.startswith('"') and val.endswith('"')) or (
                        val.startswith("'") and val.endswith("'")
                    ):
                        val = val[1:-1]
                    result[key] = val
                else:
                    result[key] = None
                    current_key = key
                    current_indent = 2  # expect children at indent >= 2
        elif current_key is not None:
            # Nested key under current_key
            if isinstance(result.get(current_key), dict):
                m = re.match(r'^\s+(\S[^:]*?):\s*(.*)', stripped)
                if m:
                    sub_key = m.group(1).strip()
                    sub_val = m.group(2).strip()
                    if (sub_val.startswith('"') and sub_val.endswith('"')) or (
                        sub_val.startswith("'") and sub_val.endswith("'")
                    ):
                        sub_val = sub_val[1:-1]
                    result[current_key][sub_key] = sub_val or None
            else:
                # First nested line — convert parent to dict
                m = re.match(r'^\s+(\S[^:]*?):\s*(.*)', stripped)
                if m:
                    sub_key = m.group(1).strip()
                    sub_val = m.group(2).strip()
                    if (sub_val.startswith('"') and sub_val.endswith('"')) or (
                        sub_val.startswith("'") and sub_val.endswith("'")
                    ):
                        sub_val = sub_val[1:-1]
                    result[current_key] = {sub_key: sub_val or None}

    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _is_valid_contract_review_result(
    block: dict,
    expected_issue_url: Optional[str] = None,
) -> bool:
    """
    Validate that a parsed YAML block is a valid CONTRACT_REVIEW_RESULT_V1.

    Required fields:
      - status: go | blocked
      - generated_by: issue-contract-review
      - issue_url: must match expected_issue_url if provided
      - generated_at: non-empty ISO8601-like string
    """
    # Must have CONTRACT_REVIEW_RESULT_V1 as root key
    if _CONTRACT_REVIEW_MARKER not in block:
        return False

    inner = block.get(_CONTRACT_REVIEW_MARKER)
    if not isinstance(inner, dict):
        return False

    # status must be go or blocked
    status = inner.get("status", "")
    if status not in ("go", "blocked"):
        return False

    # generated_by must be issue-contract-review
    if inner.get("generated_by") != "issue-contract-review":
        return False

    # generated_at must be non-empty
    if not inner.get("generated_at"):
        return False

    # issue_url must match if expected
    issue_url = inner.get("issue_url", "")
    if expected_issue_url and issue_url != expected_issue_url:
        return False

    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_contract_review_results(
    comments: list[dict],
    expected_issue_url: Optional[str] = None,
) -> list[dict]:
    """
    Parse all comments and return list of valid CONTRACT_REVIEW_RESULT_V1 dicts.

    Each returned item has:
      {
        "comment_id": int,
        "html_url": str,
        "created_at": str,
        "block": {  CONTRACT_REVIEW_RESULT_V1: {...} },
        "inner": { status, generated_by, issue_url, generated_at, ... },
        "status": "go" | "blocked",
      }

    Results are ordered by created_at ascending (comment_id ascending).
    """
    results: list[dict] = []

    for comment in comments:
        body = comment.get("body", "") or ""
        if _CONTRACT_REVIEW_MARKER not in body:
            continue

        blocks = _extract_yaml_blocks(body)
        for raw_block in blocks:
            # Only consider blocks that contain the marker
            if _CONTRACT_REVIEW_MARKER not in raw_block:
                continue
            parsed = _parse_simple_yaml_block(raw_block)
            if _is_valid_contract_review_result(parsed, expected_issue_url):
                inner = parsed[_CONTRACT_REVIEW_MARKER]
                if not isinstance(inner, dict):
                    continue
                results.append(
                    {
                        "comment_id": comment.get("id"),
                        "html_url": comment.get("html_url", ""),
                        "created_at": comment.get("created_at", ""),
                        "block": parsed,
                        "inner": inner,
                        "status": inner.get("status", ""),
                    }
                )
                # Only take first valid block per comment
                break

    return results


def find_latest_go(
    results: list[dict],
) -> Optional[dict]:
    """
    Return the latest (by created_at desc, comment_id desc) valid
    CONTRACT_REVIEW_RESULT_V1 with status: go.
    Returns None if no go result found.
    """
    go_results = [r for r in results if r["status"] == "go"]
    if not go_results:
        return None
    # Sort by created_at desc, then comment_id desc
    go_results.sort(key=lambda r: (r.get("created_at", ""), r.get("comment_id", 0)), reverse=True)
    return go_results[0]


def find_latest_result(
    results: list[dict],
) -> Optional[dict]:
    """
    Return the latest (by created_at desc, comment_id desc) valid
    CONTRACT_REVIEW_RESULT_V1 regardless of status.
    """
    if not results:
        return None
    sorted_results = sorted(
        results,
        key=lambda r: (r.get("created_at", ""), r.get("comment_id", 0)),
        reverse=True,
    )
    return sorted_results[0]
