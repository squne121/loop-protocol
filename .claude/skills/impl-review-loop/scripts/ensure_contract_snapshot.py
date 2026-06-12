#!/usr/bin/env python3
"""
ensure_contract_snapshot.py

impl-review-loop の missing_contract_go 分岐で呼ばれる orchestration script。
Issue に有効な CONTRACT_REVIEW_RESULT_V1 status: go コメントが存在するか確認し、
存在しない場合は issue-contract-review を自動実行して go コメントを取得・投稿する。

Exit codes:
  0   ok — contract snapshot が確認または materialize できた
  10  blocked_needs_refinement — contract blocked / readiness blocked
  20  human_judgment — 分類不能 / ambiguous / env error
  30  invalid_input — argument エラー
  40  runtime_error — subprocess / network エラー
  50  stale_or_conflicting_snapshot — atomicity 検証で body_sha256 mismatch

stdout: CONTRACT_SNAPSHOT_ENSURE_RESULT_V1 compact JSON のみ
stderr: diagnostic messages のみ

Modes:
  check-only  — 既存 go コメントを確認するのみ。mutation なし (default)
  auto        — go コメントがなければ run_contract_review_once.py を実行
  dry-run     — auto と同じ判定だが GitHub 投稿はしない

--post: GitHub mutation を有効化 (auto mode 以外では無視)

idempotency marker:
  <!-- loop-protocol:contract-snapshot issue=<N> body_sha256=sha256:<...> schema=CONTRACT_REVIEW_RESULT_V1 -->

body/comment snapshot atomicity:
  最初に一括取得し body_sha256 と comments_digest を保存。
  投稿直前に再取得して比較。不一致 → exit 50。

API error classification (403/429/422 blind retry 禁止):
  not_requested | dry_run | posted | deduped_existing |
  permission_denied | rate_limited | validation_failed_or_spam | ambiguous_no_retry
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
# parents: [0]=impl-review-loop, [1]=skills, [2]=.claude, [3]=<repo root>
_REPO_ROOT = _SCRIPTS_DIR.parents[3]

_ICR_SCRIPTS_DIR = (
    _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
)
_RUN_CONTRACT_REVIEW_ONCE_PY = _ICR_SCRIPTS_DIR / "run_contract_review_once.py"
_CONTRACT_REVIEW_RESULT_PARSER_PY = _ICR_SCRIPTS_DIR / "contract_review_result_parser.py"

_DEFAULT_REPO = "squne121/loop-protocol"
_DEFAULT_TIMEOUT = 30
_VC_TIMEOUT = 180

# Idempotency marker template
_IDEMPOTENCY_MARKER_TEMPLATE = (
    "<!-- loop-protocol:contract-snapshot issue={issue} "
    "body_sha256={body_sha256} schema=CONTRACT_REVIEW_RESULT_V1 -->"
)

# API post result codes
POST_RESULT_NOT_REQUESTED = "not_requested"
POST_RESULT_DRY_RUN = "dry_run"
POST_RESULT_POSTED = "posted"
POST_RESULT_DEDUPED = "deduped_existing"
POST_RESULT_PERMISSION_DENIED = "permission_denied"
POST_RESULT_RATE_LIMITED = "rate_limited"
POST_RESULT_VALIDATION_FAILED = "validation_failed_or_spam"
POST_RESULT_AMBIGUOUS = "ambiguous_no_retry"


# ---------------------------------------------------------------------------
# Import helper for contract_review_result_parser
# ---------------------------------------------------------------------------


def _import_parser_module():
    """Import contract_review_result_parser from ICR scripts dir."""
    spec = importlib.util.spec_from_file_location(
        "contract_review_result_parser",
        _CONTRACT_REVIEW_RESULT_PARSER_PY,
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load contract_review_result_parser")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Body snapshot helpers
# ---------------------------------------------------------------------------


def sha256_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_issue_body(issue_number: int, repo: str, timeout: int = _DEFAULT_TIMEOUT) -> tuple[Optional[str], Optional[str]]:
    """Returns (body_text, error_code_or_None)."""
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "body"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None, "gh_issue_view_error"
        data = json.loads(result.stdout)
        return data.get("body", ""), None
    except subprocess.TimeoutExpired:
        return None, "gh_timeout"
    except json.JSONDecodeError:
        return None, "gh_json_error"
    except Exception:
        return None, "gh_other_error"


def compute_comments_digest(comments: list[dict]) -> str:
    """
    Compute a stable digest of comment IDs and updated_at to detect changes.
    """
    digest_input = json.dumps(
        [{"id": c.get("id"), "updated_at": c.get("updated_at")} for c in comments],
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------


def find_idempotency_marker(
    comments: list[dict],
    issue_number: int,
    body_sha256: str,
) -> Optional[str]:
    """
    Check if the idempotency marker with matching body_sha256 already exists.
    Returns html_url if found, None otherwise.
    """
    marker = _IDEMPOTENCY_MARKER_TEMPLATE.format(
        issue=issue_number,
        body_sha256=body_sha256,
    )
    for comment in comments:
        body = comment.get("body", "") or ""
        if marker in body:
            return comment.get("html_url")
    return None


# ---------------------------------------------------------------------------
# HTTP error classification
# ---------------------------------------------------------------------------


def classify_post_http_error(status_code: int) -> str:
    """
    Classify HTTP error from GitHub comment post API.
    403/429/422 → specific codes, no blind retry.
    """
    if status_code == 403:
        return POST_RESULT_PERMISSION_DENIED
    elif status_code == 429:
        return POST_RESULT_RATE_LIMITED
    elif status_code == 422:
        return POST_RESULT_VALIDATION_FAILED
    else:
        return POST_RESULT_AMBIGUOUS


# ---------------------------------------------------------------------------
# GitHub comment posting
# ---------------------------------------------------------------------------


def post_comment(
    issue_number: int,
    repo: str,
    body: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[Optional[str], str, Optional[int]]:
    """
    Post a comment to the issue via gh CLI.
    Returns (html_url_or_None, result_code, http_status_or_None).

    result_code: posted | permission_denied | rate_limited |
                 validation_failed_or_spam | ambiguous_no_retry
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "comment",
                str(issue_number),
                "--repo",
                repo,
                "--body",
                body,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            # Try to extract URL from stdout
            url = result.stdout.strip() or None
            return url, POST_RESULT_POSTED, None

        # Extract HTTP status from stderr
        http_status = _extract_http_status(result.stderr)
        if http_status:
            code = classify_post_http_error(http_status)
            return None, code, http_status

        # Unknown error
        return None, POST_RESULT_AMBIGUOUS, None
    except subprocess.TimeoutExpired:
        return None, "gh_timeout", None
    except Exception:
        return None, POST_RESULT_AMBIGUOUS, None


def _extract_http_status(stderr: str) -> Optional[int]:
    """Extract HTTP status code from gh CLI stderr."""
    import re
    m = re.search(r"HTTP (\d{3})", stderr or "")
    if m:
        return int(m.group(1))
    # Check for error patterns
    if "403" in stderr:
        return 403
    if "429" in stderr:
        return 429
    if "422" in stderr:
        return 422
    return None


# ---------------------------------------------------------------------------
# run_contract_review_once wrapper
# ---------------------------------------------------------------------------


def run_contract_review_once(
    issue_number: int,
    repo: str,
    mode: str = "static",
    skip_idempotency_check: bool = True,
) -> tuple[Optional[dict], Optional[str]]:
    """
    Run run_contract_review_once.py as subprocess.
    Returns (result_dict, error_code_or_None).
    """
    cmd = [
        sys.executable,
        str(_RUN_CONTRACT_REVIEW_ONCE_PY),
        "--issue-number",
        str(issue_number),
        "--repo",
        repo,
        "--mode",
        mode,
    ]
    if skip_idempotency_check:
        cmd.append("--skip-idempotency-check")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_VC_TIMEOUT,
        )
        stdout = result.stdout.strip()
        if not stdout:
            return None, "no_stdout_from_run_contract_review_once"
        try:
            return json.loads(stdout), None
        except json.JSONDecodeError as exc:
            # subprocess JSON parse failure → runtime_error
            return None, f"json_parse_error:{exc}"
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as exc:
        return None, f"subprocess_error:{exc}"


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def ensure_contract_snapshot(
    issue_number: int,
    repo: str,
    mode: str = "check-only",
    do_post: bool = False,
    artifact_dir: Optional[str] = None,
) -> dict[str, Any]:
    """
    Main logic for ensure_contract_snapshot.

    Returns CONTRACT_SNAPSHOT_ENSURE_RESULT_V1 dict.
    """
    result: dict[str, Any] = {
        "schema": "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1",
        "issue_number": issue_number,
        "repo": repo,
        "mode": mode,
        "status": "runtime_error",
        "source": None,
        "contract_snapshot_url": None,
        "post_result": POST_RESULT_NOT_REQUESTED,
        "http_status": None,
        "body_sha256_at_check": None,
        "body_sha256_at_post": None,
        "comments_digest_at_check": None,
        "comments_digest_at_post": None,
        "idempotency_marker_found": False,
        "errors": [],
        "contract_review_once_result": None,
    }

    # Load parser module
    try:
        parser_mod = _import_parser_module()
    except Exception as exc:
        result["errors"].append(f"parser_import_error: {exc}")
        result["status"] = "runtime_error"
        return result

    # Step 1: Fetch body and comments atomically (initial snapshot)
    body, body_err = fetch_issue_body(issue_number, repo)
    if body_err:
        result["errors"].append(f"body_fetch_error: {body_err}")
        result["status"] = "runtime_error"
        return result

    body_sha256 = sha256_of(body or "")
    result["body_sha256_at_check"] = body_sha256

    comments, comments_err = parser_mod.fetch_issue_comments(issue_number, repo)
    if comments_err:
        result["errors"].append(f"comments_fetch_error: {comments_err}")
        result["status"] = "runtime_error"
        return result

    comments_digest = compute_comments_digest(comments)
    result["comments_digest_at_check"] = comments_digest

    # Build issue URL for validation
    owner_repo = repo.split("/")
    if len(owner_repo) == 2:
        issue_url = f"https://github.com/{repo}/issues/{issue_number}"
    else:
        issue_url = None

    # Step 2: Parse existing CONTRACT_REVIEW_RESULT_V1 comments
    results = parser_mod.parse_contract_review_results(comments, expected_issue_url=issue_url)
    latest = parser_mod.find_latest_result(results)
    go_result = parser_mod.find_latest_go(results)

    # If latest result is blocked, return blocked regardless of mode
    if latest and latest["status"] == "blocked":
        result["status"] = "blocked_needs_refinement"
        result["source"] = "latest_blocked"
        result["contract_snapshot_url"] = latest["html_url"]
        return result

    # If go result exists, return ok (idempotent)
    if go_result:
        result["status"] = "ok"
        result["source"] = "existing_go"
        result["contract_snapshot_url"] = go_result["html_url"]
        return result

    # No existing go result
    if mode == "check-only":
        result["status"] = "human_judgment"
        result["source"] = "readiness_blocked"
        result["errors"].append(
            "no_existing_go_comment: run issue-contract-review to generate contract snapshot"
        )
        return result

    # auto or dry-run mode: run contract review once
    review_result, review_err = run_contract_review_once(
        issue_number=issue_number,
        repo=repo,
        mode="static",
        skip_idempotency_check=True,
    )

    result["contract_review_once_result"] = review_result

    if review_err:
        result["errors"].append(f"run_contract_review_once_error: {review_err}")
        result["status"] = "runtime_error"
        return result

    if review_result is None:
        result["errors"].append("run_contract_review_once_returned_null")
        result["status"] = "runtime_error"
        return result

    review_status = review_result.get("status", "")

    # human_judgment from run_contract_review_once → human_judgment (NOT blocked)
    if review_status == "human_judgment":
        result["status"] = "human_judgment"
        result["source"] = "human_judgment"
        return result

    # runtime_error propagation
    if review_status == "runtime_error":
        result["status"] = "runtime_error"
        result["errors"].extend(review_result.get("errors", []))
        return result

    # blocked → blocked_needs_refinement
    if review_status == "blocked":
        result["status"] = "blocked_needs_refinement"
        result["source"] = "readiness_blocked"
        return result

    if review_status != "go":
        result["status"] = "runtime_error"
        result["errors"].append(f"unexpected_review_status: {review_status}")
        return result

    # review_status == go
    # dry-run: report would post but don't actually post
    if mode == "dry-run" or not do_post:
        result["status"] = "ok"
        result["source"] = "materialized_go"
        result["post_result"] = POST_RESULT_DRY_RUN
        return result

    # auto + --post: prepare comment body
    # Build idempotency marker
    idempotency_marker = _IDEMPOTENCY_MARKER_TEMPLATE.format(
        issue=issue_number,
        body_sha256=body_sha256,
    )

    # Check idempotency marker in existing comments
    existing_marker_url = find_idempotency_marker(comments, issue_number, body_sha256)
    if existing_marker_url:
        result["status"] = "ok"
        result["source"] = "existing_go"
        result["contract_snapshot_url"] = existing_marker_url
        result["post_result"] = POST_RESULT_DEDUPED
        result["idempotency_marker_found"] = True
        return result

    # Atomicity check: re-fetch body and comments before posting
    body_post, body_post_err = fetch_issue_body(issue_number, repo)
    if body_post_err:
        result["errors"].append(f"body_refetch_error: {body_post_err}")
        result["status"] = "runtime_error"
        return result

    body_sha256_post = sha256_of(body_post or "")
    result["body_sha256_at_post"] = body_sha256_post

    comments_post, comments_post_err = parser_mod.fetch_issue_comments(issue_number, repo)
    if comments_post_err:
        result["errors"].append(f"comments_refetch_error: {comments_post_err}")
        result["status"] = "runtime_error"
        return result

    comments_digest_post = compute_comments_digest(comments_post)
    result["comments_digest_at_post"] = comments_digest_post

    # Stale check: body changed between initial fetch and post
    if body_sha256_post != body_sha256:
        result["status"] = "stale_or_conflicting_snapshot"
        result["errors"].append(
            f"body_sha256_mismatch: initial={body_sha256} post={body_sha256_post}"
        )
        return result

    # Also check if a go comment appeared in the interim
    results_post = parser_mod.parse_contract_review_results(
        comments_post, expected_issue_url=issue_url
    )
    go_post = parser_mod.find_latest_go(results_post)
    if go_post:
        result["status"] = "ok"
        result["source"] = "existing_go"
        result["contract_snapshot_url"] = go_post["html_url"]
        result["post_result"] = POST_RESULT_DEDUPED
        return result

    # Build comment to post
    # The actual contract review result is embedded in the comment by run_contract_review_once
    # For now, post a structured comment indicating the review result
    comment_body = _build_contract_review_comment(
        issue_number=issue_number,
        repo=repo,
        review_result=review_result,
        idempotency_marker=idempotency_marker,
        body_sha256=body_sha256,
    )

    # Post comment (403/429/422 → no retry)
    url, post_code, http_status = post_comment(issue_number, repo, comment_body)
    result["post_result"] = post_code
    result["http_status"] = http_status

    if post_code == POST_RESULT_POSTED:
        result["status"] = "ok"
        result["source"] = "materialized_go"
        result["contract_snapshot_url"] = url
    elif post_code in (
        POST_RESULT_PERMISSION_DENIED,
        POST_RESULT_RATE_LIMITED,
        POST_RESULT_VALIDATION_FAILED,
        POST_RESULT_AMBIGUOUS,
    ):
        # 403/429/422: no blind retry — set status to human_judgment
        result["status"] = "human_judgment"
        result["errors"].append(
            f"post_api_error: {post_code} (http_status={http_status}) — no retry"
        )
    else:
        result["status"] = "runtime_error"
        result["errors"].append(f"post_error: {post_code}")

    return result


def _build_contract_review_comment(
    issue_number: int,
    repo: str,
    review_result: dict,
    idempotency_marker: str,
    body_sha256: str,
) -> str:
    """Build the GitHub comment body for contract review posting."""
    import datetime

    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    owner_repo = repo.split("/")
    issue_url = (
        f"https://github.com/{repo}/issues/{issue_number}"
        if len(owner_repo) == 2
        else ""
    )

    return f"""{idempotency_marker}

## Contract Review Result

```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: go
  generated_at: "{now}"
  generated_by: issue-contract-review
  issue_url: {issue_url}
  body_sha256: "{body_sha256}"
  source: ensure_contract_snapshot_auto
  readiness_status: {review_result.get("readiness_status", "go")}
```
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "ensure_contract_snapshot: check/materialize CONTRACT_REVIEW_RESULT_V1 "
            "for impl-review-loop missing_contract_go handling"
        )
    )
    parser.add_argument(
        "--issue-number",
        "--issue",
        dest="issue_number",
        type=int,
        required=True,
        help="GitHub Issue number",
    )
    parser.add_argument(
        "--repo",
        default=_DEFAULT_REPO,
        help="GitHub repo (owner/name)",
    )
    parser.add_argument(
        "--mode",
        choices=["check-only", "auto", "dry-run"],
        default="check-only",
        help=(
            "check-only: inspect only (default). "
            "auto: run contract review if missing. "
            "dry-run: same logic as auto but no GitHub mutation."
        ),
    )
    parser.add_argument(
        "--post",
        action="store_true",
        default=False,
        help="Enable GitHub mutations (comment posting). Only effective in --mode auto.",
    )
    parser.add_argument(
        "--artifact-dir",
        default=None,
        help="Directory to save artifact JSON",
    )

    args = parser.parse_args()

    if not args.issue_number:
        print(
            json.dumps(
                {
                    "schema": "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1",
                    "status": "runtime_error",
                    "errors": ["--issue-number is required"],
                }
            )
        )
        return 30  # invalid_input

    result = ensure_contract_snapshot(
        issue_number=args.issue_number,
        repo=args.repo,
        mode=args.mode,
        do_post=args.post,
        artifact_dir=args.artifact_dir,
    )

    # Save artifact if requested
    if args.artifact_dir:
        artifact_path = Path(args.artifact_dir) / f"contract-snapshot-{args.issue_number}.json"
        try:
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            result["artifact_path"] = str(artifact_path)
        except Exception as exc:
            result.setdefault("errors", []).append(f"artifact_write_error: {exc}")

    print(json.dumps(result))

    status = result.get("status", "runtime_error")
    if status == "ok":
        return 0
    elif status == "blocked_needs_refinement":
        return 10
    elif status == "human_judgment":
        return 20
    elif status == "stale_or_conflicting_snapshot":
        return 50
    else:  # runtime_error or unknown
        return 40


if __name__ == "__main__":
    sys.exit(main())
