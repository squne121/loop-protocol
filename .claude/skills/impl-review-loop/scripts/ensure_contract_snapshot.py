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
  50  stale_or_conflicting_snapshot — atomicity 検証で body_sha256 or updatedAt mismatch

stdout: CONTRACT_SNAPSHOT_ENSURE_RESULT_V1 compact JSON のみ
stderr: diagnostic messages のみ

Modes:
  check-only  — 既存 go コメントを確認するのみ。mutation なし (default)
  auto        — go コメントがなければ run_contract_review_once.py を実行
  dry-run     — auto と同じ判定だが GitHub 投稿はしない

--post: GitHub mutation を有効化 (auto mode 以外では無視)

idempotency marker:
  <!-- loop-protocol:contract-snapshot issue=<N> body_sha256=sha256:<...> schema=CONTRACT_REVIEW_RESULT_V1 -->

body/comment snapshot atomicity (B2):
  最初に一括取得し body_sha256, issue_updated_at, comments_digest を保存。
  投稿直前に再取得して比較。
  body_sha256 変化 OR updatedAt 変化 OR latest blocked コメント出現 → exit 50。

API error classification (403/429/422 blind retry 禁止):
  not_requested | dry_run_would_post | posted | deduped_existing |
  permission_denied | rate_limited | validation_failed_or_spam | ambiguous_no_retry

Schema key: post_status (not post_result — B4)

status: ok implies contract_snapshot_url is not None (B3).
dry-run / no-post → status: dry_run_would_post (not ok).

Comment posting: gh api REST (B5) for precise HTTP status classification.
V1 comment includes checks summary (B6).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
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

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from evaluate_product_spec_gate import evaluate_product_spec_payload  # noqa: E402

_DEFAULT_REPO = "squne121/loop-protocol"
_DEFAULT_TIMEOUT = 30
_VC_TIMEOUT = 180

# Idempotency marker template
_IDEMPOTENCY_MARKER_TEMPLATE = (
    "<!-- loop-protocol:contract-snapshot issue={issue} "
    "body_sha256={body_sha256} schema=CONTRACT_REVIEW_RESULT_V1 -->"
)

# API post status codes (B4: key is post_status throughout)
POST_STATUS_NOT_REQUESTED = "not_requested"
POST_STATUS_DRY_RUN = "dry_run_would_post"
POST_STATUS_POSTED = "posted"
POST_STATUS_DEDUPED = "deduped_existing"
POST_STATUS_PERMISSION_DENIED = "permission_denied"
POST_STATUS_RATE_LIMITED = "rate_limited"
POST_STATUS_VALIDATION_FAILED = "validation_failed_or_spam"
POST_STATUS_AMBIGUOUS = "ambiguous_no_retry"

# Legacy aliases for tests that import old names — mapped to new values
POST_RESULT_NOT_REQUESTED = POST_STATUS_NOT_REQUESTED
POST_RESULT_DRY_RUN = POST_STATUS_DRY_RUN
POST_RESULT_POSTED = POST_STATUS_POSTED
POST_RESULT_DEDUPED = POST_STATUS_DEDUPED
POST_RESULT_PERMISSION_DENIED = POST_STATUS_PERMISSION_DENIED
POST_RESULT_RATE_LIMITED = POST_STATUS_RATE_LIMITED
POST_RESULT_VALIDATION_FAILED = POST_STATUS_VALIDATION_FAILED
POST_RESULT_AMBIGUOUS = POST_STATUS_AMBIGUOUS


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
# Body + updatedAt snapshot helpers (B2)
# ---------------------------------------------------------------------------


def sha256_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


_CANONICAL_BODY_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def is_go_fresh(go_result: object, expected_body_sha256: str) -> bool:
    """Return whether a parsed go result is canonical and bound to this body."""
    if not isinstance(go_result, dict):
        return False
    inner = go_result.get("inner")
    if not isinstance(inner, dict):
        return False
    body_sha256 = inner.get("body_sha256")
    return (
        isinstance(body_sha256, str)
        and _CANONICAL_BODY_SHA256_RE.fullmatch(body_sha256) is not None
        and body_sha256 == expected_body_sha256
    )


def has_vc_preflight_classifications(go_result: object) -> bool:
    """Return whether a go snapshot carries baseline VC classifications."""
    if not isinstance(go_result, dict):
        return False
    inner = go_result.get("inner")
    if not isinstance(inner, dict):
        return False
    checks = inner.get("checks")
    if not isinstance(checks, dict):
        return False
    vc_preflight = checks.get("vc_preflight")
    return isinstance(vc_preflight, dict) and isinstance(
        vc_preflight.get("classifications"), list
    )


def is_go_current(go_result: object, expected_body_sha256: str) -> bool:
    """Return whether a go snapshot is fresh and complete for loop consumption."""
    if not is_go_fresh(go_result, expected_body_sha256):
        return False
    if not has_vc_preflight_classifications(go_result):
        return False
    inner = go_result.get("inner") if isinstance(go_result, dict) else None
    checks = inner.get("checks") if isinstance(inner, dict) else None
    product_spec_check = checks.get("product_spec_check") if isinstance(checks, dict) else None
    if not isinstance(product_spec_check, dict):
        return False
    if product_spec_check.get("body_sha256") != expected_body_sha256:
        return False
    return (
        evaluate_product_spec_payload(
            product_spec_check,
            body_sha256=expected_body_sha256,
        ).get("routing_action") == "continue"
    )


def fetch_issue_snapshot(
    issue_number: int,
    repo: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Fetch body + updatedAt in a single gh call.
    Returns (body_text, updated_at_str, error_code_or_None).

    B2: includes updatedAt for atomicity guard.
    """
    try:
        result = subprocess.run(
            [
                "gh", "issue", "view", str(issue_number),
                "--repo", repo,
                "--json", "body,updatedAt",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None, None, "gh_issue_view_error"
        data = json.loads(result.stdout)
        return data.get("body", ""), data.get("updatedAt", ""), None
    except subprocess.TimeoutExpired:
        return None, None, "gh_timeout"
    except json.JSONDecodeError:
        return None, None, "gh_json_error"
    except Exception:
        return None, None, "gh_other_error"


def fetch_issue_body(
    issue_number: int,
    repo: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[Optional[str], Optional[str]]:
    """
    Backward-compatible wrapper: returns (body_text, error_code_or_None).
    Internally calls fetch_issue_snapshot.
    """
    body, _updated_at, err = fetch_issue_snapshot(issue_number, repo, timeout)
    return body, err


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
        return POST_STATUS_PERMISSION_DENIED
    elif status_code == 429:
        return POST_STATUS_RATE_LIMITED
    elif status_code == 422:
        return POST_STATUS_VALIDATION_FAILED
    else:
        return POST_STATUS_AMBIGUOUS


# ---------------------------------------------------------------------------
# GitHub comment posting via REST API (B5)
# ---------------------------------------------------------------------------


def post_comment(
    issue_number: int,
    repo: str,
    body: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[Optional[str], str, Optional[int]]:
    """
    Post a comment to the issue via GitHub REST API.
    Uses 'gh api --method POST' for precise HTTP status classification (B5).

    Returns (html_url_or_None, post_status_code, http_status_or_None).

    post_status_code: posted | permission_denied | rate_limited |
                      validation_failed_or_spam | ambiguous_no_retry
    """
    import tempfile

    # Write body to temp file to avoid shell escaping issues
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(body)
            tmp_path = tmp.name

        try:
            result = subprocess.run(
                [
                    "gh", "api",
                    "--method", "POST",
                    f"repos/{repo}/issues/{issue_number}/comments",
                    "--field", f"body=@{tmp_path}",
                    "--jq", ".html_url",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        if result.returncode == 0:
            url = result.stdout.strip() or None
            return url, POST_STATUS_POSTED, None

        # Extract HTTP status from stderr
        http_status = _extract_http_status(result.stderr)
        if http_status:
            # Handle 404/410 as ambiguous_no_retry
            if http_status in (404, 410):
                return None, POST_STATUS_AMBIGUOUS, http_status
            code = classify_post_http_error(http_status)
            return None, code, http_status

        # Transport error: check idempotency via comment re-fetch
        return None, POST_STATUS_AMBIGUOUS, None

    except subprocess.TimeoutExpired:
        return None, "gh_timeout", None
    except Exception:
        return None, POST_STATUS_AMBIGUOUS, None


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
    evidence_mode: str = "baseline",
    cwd: Optional[str] = None,
    reviewed_head_sha: Optional[str] = None,
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
    if evidence_mode == "current-head":
        if not cwd or not reviewed_head_sha:
            return None, "current_head_requires_cwd_and_reviewed_head_sha"
        cmd.extend([
            "--evidence-mode", "current-head",
            "--cwd", cwd,
            "--reviewed-head-sha", reviewed_head_sha,
        ])

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
    evidence_mode: str = "baseline",
    cwd: Optional[str] = None,
    reviewed_head_sha: Optional[str] = None,
) -> dict[str, Any]:
    """
    Main logic for ensure_contract_snapshot.

    Returns CONTRACT_SNAPSHOT_ENSURE_RESULT_V1 dict.

    Schema invariant (B3): status: ok implies contract_snapshot_url is not None.
    dry-run / no-post → status: dry_run_would_post (not ok).
    Schema key: post_status (B4).
    """
    result: dict[str, Any] = {
        "schema": "CONTRACT_SNAPSHOT_ENSURE_RESULT_V1",
        "issue_number": issue_number,
        "repo": repo,
        "mode": mode,
        "status": "runtime_error",
        "source": None,
        "contract_snapshot_url": None,
        "post_status": POST_STATUS_NOT_REQUESTED,  # B4: key is post_status
        "http_status": None,
        "body_sha256_at_check": None,
        "body_sha256_at_post": None,
        "issue_updated_at_at_check": None,   # B2
        "issue_updated_at_at_post": None,    # B2
        "comments_digest_at_check": None,
        "comments_digest_at_post": None,
        "idempotency_marker_found": False,
        "errors": [],
        "contract_review_once_result": None,
        "vc_evidence": {"mode": evidence_mode},
        "current_vc_result": None,
    }

    # Load parser module
    try:
        parser_mod = _import_parser_module()
    except Exception as exc:
        result["errors"].append(f"parser_import_error: {exc}")
        result["status"] = "runtime_error"
        return result

    # Build issue URL for validation
    owner_repo = repo.split("/")
    if len(owner_repo) == 2:
        issue_url = f"https://github.com/{repo}/issues/{issue_number}"
    else:
        issue_url = None

    # Step 1/2: read a candidate snapshot.  A fresh existing go needs one
    # bounded recheck, otherwise a body edit between the two API calls could
    # make an old comment look current.
    for attempt in range(2):
        body, updated_at, snapshot_err = fetch_issue_snapshot(issue_number, repo)
        if snapshot_err:
            result["errors"].append(f"body_fetch_error: {snapshot_err}")
            result["status"] = "runtime_error"
            return result

        body_sha256 = sha256_of(body or "")
        result["body_sha256_at_check"] = body_sha256
        result["issue_updated_at_at_check"] = updated_at

        comments, comments_err = parser_mod.fetch_issue_comments(issue_number, repo)
        if comments_err:
            result["errors"].append(f"comments_fetch_error: {comments_err}")
            result["status"] = "runtime_error"
            return result
        comments_digest = compute_comments_digest(comments)
        result["comments_digest_at_check"] = comments_digest

        results = parser_mod.parse_contract_review_results(
            comments, expected_issue_url=issue_url
        )
        latest = parser_mod.find_latest_result(results)
        go_result = parser_mod.find_latest_go(results)

        # latest blocked retains precedence over existing-go adoption.
        if latest and latest["status"] == "blocked":
            result["status"] = "blocked_needs_refinement"
            result["source"] = "latest_blocked"
            result["contract_snapshot_url"] = latest["html_url"]
            return result

        if not is_go_current(go_result, body_sha256):
            break

        body_confirm, updated_confirm, confirm_err = fetch_issue_snapshot(issue_number, repo)
        if confirm_err:
            result["errors"].append(f"body_refetch_error: {confirm_err}")
            result["status"] = "runtime_error"
            return result
        if body_confirm == body and updated_confirm == updated_at:
            if evidence_mode != "current-head":
                result["status"] = "ok"
                result["source"] = "existing_go"
                result["contract_snapshot_url"] = go_result["html_url"]
                return result
            # Snapshot reuse and current-head evidence production are independent:
            # preserve the fresh snapshot, then continue to run the producer.
            result["source"] = "existing_go"
            result["contract_snapshot_url"] = go_result["html_url"]
            break
        if attempt == 1:
            result["status"] = "stale_or_conflicting_snapshot"
            result["errors"].append("issue_changed_during_existing_go_recheck")
            return result

    # No existing go result.  A current-head caller must still produce fresh
    # evidence when a snapshot was found above, even in check-only mode.
    if mode == "check-only" and result["contract_snapshot_url"] is None:
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
        evidence_mode=evidence_mode,
        cwd=cwd,
        reviewed_head_sha=reviewed_head_sha,
    )

    result["contract_review_once_result"] = review_result
    if review_result:
        result["vc_evidence"] = review_result.get("vc_evidence", result["vc_evidence"])
        result["current_vc_result"] = review_result.get(
            "current_vc_result", result["current_vc_result"]
        )

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

    if result["contract_snapshot_url"] is not None:
        result["status"] = "ok"
        result["source"] = "existing_go"
        return result

    # review_status == go
    # B3: dry-run / no-post → status: dry_run_would_post (NOT ok)
    # status: ok is reserved for cases where contract_snapshot_url is non-null
    if mode == "dry-run" or not do_post:
        result["status"] = "dry_run_would_post"
        result["source"] = "materialized_go"
        result["post_status"] = POST_STATUS_DRY_RUN
        return result

    # auto + --post: prepare comment body
    # Build idempotency marker
    idempotency_marker = _IDEMPOTENCY_MARKER_TEMPLATE.format(
        issue=issue_number,
        body_sha256=body_sha256,
    )

    # Atomicity check (B2): re-fetch body, updatedAt, and comments before posting
    body_post, updated_at_post, snapshot_post_err = fetch_issue_snapshot(issue_number, repo)
    if snapshot_post_err:
        result["errors"].append(f"body_refetch_error: {snapshot_post_err}")
        result["status"] = "runtime_error"
        return result

    body_sha256_post = sha256_of(body_post or "")
    result["body_sha256_at_post"] = body_sha256_post
    result["issue_updated_at_at_post"] = updated_at_post  # B2

    comments_post, comments_post_err = parser_mod.fetch_issue_comments(issue_number, repo)
    if comments_post_err:
        result["errors"].append(f"comments_refetch_error: {comments_post_err}")
        result["status"] = "runtime_error"
        return result

    comments_digest_post = compute_comments_digest(comments_post)
    result["comments_digest_at_post"] = comments_digest_post

    # B2: Stale check — body_sha256 OR updatedAt changed → exit 50
    if body_sha256_post != body_sha256:
        result["status"] = "stale_or_conflicting_snapshot"
        result["errors"].append(
            f"body_sha256_mismatch: initial={body_sha256} post={body_sha256_post}"
        )
        return result

    # Parse comments before treating an updatedAt change as conflicting: adding a
    # fresh go comment can legitimately advance Issue.updatedAt.
    results_post = parser_mod.parse_contract_review_results(
        comments_post, expected_issue_url=issue_url
    )
    latest_post = parser_mod.find_latest_result(results_post)
    if latest_post and latest_post["status"] == "blocked":
        result["status"] = "stale_or_conflicting_snapshot"
        result["errors"].append(
            "blocked_comment_appeared_during_atomicity_window"
        )
        return result

    # Also check if a go comment appeared in the interim
    go_post = parser_mod.find_latest_go(results_post)
    if is_go_current(go_post, body_sha256_post):
        body_dedupe, _updated_dedupe, dedupe_err = fetch_issue_snapshot(
            issue_number, repo
        )
        if dedupe_err:
            result["errors"].append(f"body_dedupe_refetch_error: {dedupe_err}")
            result["status"] = "runtime_error"
            return result
        if sha256_of(body_dedupe or "") != body_sha256_post:
            result["status"] = "stale_or_conflicting_snapshot"
            result["errors"].append("body_changed_during_fresh_go_dedupe")
            return result
        result["status"] = "ok"
        result["source"] = "existing_go"
        result["contract_snapshot_url"] = go_post["html_url"]
        result["post_status"] = POST_STATUS_DEDUPED
        return result

    if updated_at_post and updated_at and updated_at_post != updated_at:
        result["status"] = "stale_or_conflicting_snapshot"
        result["errors"].append(
            f"updated_at_mismatch: initial={updated_at} post={updated_at_post}"
        )
        return result

    # Build comment to post (B6: include checks summary)
    comment_body = _build_contract_review_comment(
        issue_number=issue_number,
        repo=repo,
        review_result=review_result,
        idempotency_marker=idempotency_marker,
        body_sha256=body_sha256,
    )

    # Post comment via REST API — B5: 403/429/422 → no retry
    url, post_code, http_status = post_comment(issue_number, repo, comment_body)
    result["post_status"] = post_code  # B4: key is post_status
    result["http_status"] = http_status

    if post_code == POST_STATUS_POSTED:
        # B3: status: ok only when contract_snapshot_url is non-null
        result["status"] = "ok"
        result["source"] = "materialized_go"
        result["contract_snapshot_url"] = url
    elif post_code in (
        POST_STATUS_PERMISSION_DENIED,
        POST_STATUS_RATE_LIMITED,
        POST_STATUS_VALIDATION_FAILED,
        POST_STATUS_AMBIGUOUS,
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
    """
    Build the GitHub comment body for contract review posting.
    Includes checks summary (B6).
    """
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    owner_repo = repo.split("/")
    issue_url = (
        f"https://github.com/{repo}/issues/{issue_number}"
        if len(owner_repo) == 2
        else ""
    )

    # B6: Build checks summary from review_result
    checks = review_result.get("checks", {}) or {}
    readiness_check = checks.get("readiness", "go") or "go"
    blockers_check = checks.get("blockers", "pass") or "pass"
    product_spec_summary = checks.get("product_spec", "pass") or "pass"
    product_spec_check = checks.get("product_spec_check")
    if not isinstance(product_spec_check, dict):
        product_spec_check = {}
    product_spec_check_json = json.dumps(
        product_spec_check, ensure_ascii=False, separators=(",", ":")
    )
    vc_preflight_check = checks.get("vc_preflight", "pass") or "pass"
    vc_preflight_classifications = review_result.get(
        "vc_preflight_classifications", []
    )
    if not isinstance(vc_preflight_classifications, list):
        vc_preflight_classifications = []
    classifications_json = json.dumps(
        vc_preflight_classifications, ensure_ascii=False, separators=(",", ":")
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
  checks:
    readiness: {readiness_check}
    blockers: {blockers_check}
    product_spec: {product_spec_summary}
    product_spec_check: {product_spec_check_json}
    vc_preflight:
      decision: {vc_preflight_check}
      classifications: {classifications_json}
  source: ensure_contract_snapshot_auto
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
    parser.add_argument("--evidence-mode", choices=["baseline", "current-head"], default="baseline")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--reviewed-head-sha", default=None)
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
        evidence_mode=args.evidence_mode,
        cwd=args.cwd,
        reviewed_head_sha=args.reviewed_head_sha,
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
    elif status in ("human_judgment", "dry_run_would_post"):
        return 20
    elif status == "stale_or_conflicting_snapshot":
        return 50
    else:  # runtime_error or unknown
        return 40


if __name__ == "__main__":
    sys.exit(main())
