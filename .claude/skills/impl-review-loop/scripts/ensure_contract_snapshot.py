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
  60  controlled_publisher_binding_failed — #1475: 投稿直後の comment ID readback binding 不一致・欠落

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

Security scope (#1475 fix_delta P1 item 3 -- explicit, deliberately narrowed
claim rather than an unimplemented receipt schema):
  "Authoritative" means: authored by a GitHub account present in
  contract_review_result_parser.TRUSTED_CONTRACT_PUBLISHERS (an exact
  user.id + user.login + user.type + author_association match), which today
  is the repo OWNER account only. It does NOT mean "identical to the exact
  bytes this specific ensure_contract_snapshot.py process instance posted
  moments ago" for every code path:
    - The publish-time path (POST_STATUS_POSTED) DOES verify the freshly
      posted comment id, issue binding, publisher identity, and comment
      body hash via an independent direct-GET readback
      (verify_controlled_publisher_comment_id_binding).
    - The existing-snapshot-reuse path (source: existing_go, status: ok
      without a POST in this run) does NOT re-run that binding check. It is
      protected instead by the strict identity-tuple allowlist applied to
      every comment considered a candidate (only the allowlisted account
      can ever produce an authoritative go/blocked entry) plus the
      body_sha256 / vc_preflight / product_spec_check freshness checks in
      is_go_current(). It does not protect against the allowlisted account's
      own comment being edited after posting to a body that still hashes to
      a value is_go_current() would accept as current -- that residual risk
      is intentionally out of scope for this Issue and tracked as a
      follow-up (CONTRACT_SNAPSHOT_PUBLISH_RECEIPT_V1, option 1 in the
      Issue #1475 fix_delta) rather than claimed as solved here.
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

# #1537: single source of truth for Allowed Paths normalization / hashing,
# shared with the reviewer's Allowed Paths gate so the fingerprint's
# allowed_paths_normalized_sha256 is byte-for-byte reproducible by both sides.
_PRJ_SCRIPTS_DIR = (
    _REPO_ROOT / ".claude" / "skills" / "pr-review-judge" / "scripts"
)
_ALLOWED_PATHS_REVIEW_GATE_PY = _PRJ_SCRIPTS_DIR / "allowed_paths_review_gate.py"
_BASELINE_VC_PREFLIGHT_PY = (
    _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
    / "baseline_vc_preflight.py"
)

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


# ---------------------------------------------------------------------------
# Source-bound contract fingerprint (Issue #1537)
# ---------------------------------------------------------------------------


def _import_allowed_paths_gate_module():
    """Import allowed_paths_review_gate from pr-review-judge scripts dir.

    Single source of truth for Allowed Paths normalization/hash rules
    (AllowedPathsMatcher, AllowedPathsGateEvaluator.compute_allowed_paths_hash)
    so the fingerprint's allowed_paths_normalized_sha256 is computed
    identically by producer (this module) and the reviewer's gate.
    """
    spec = importlib.util.spec_from_file_location(
        "allowed_paths_review_gate_for_ensure_contract_snapshot",
        _ALLOWED_PATHS_REVIEW_GATE_PY,
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load allowed_paths_review_gate")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def extract_allowed_paths_from_body(body: str) -> list[str]:
    """Use the contract-review canonical Allowed Paths grammar."""
    spec = importlib.util.spec_from_file_location(
        "baseline_vc_preflight_allowed_paths", _BASELINE_VC_PREFLIGHT_PY
    )
    if spec is None or spec.loader is None:
        raise ValueError("canonical_allowed_paths_extractor_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    paths = module.extract_allowed_paths(body or "")
    return paths if isinstance(paths, list) else []


def _canonicalize_allowed_paths_strict(allowed_paths: list[str]) -> list[str]:
    """Match the review gate's normalization or fail before any POST."""
    gate_mod = _import_allowed_paths_gate_module()
    canonicalized: list[str] = []
    for pattern in allowed_paths:
        normalized = gate_mod.AllowedPathsMatcher.normalize_allowed_pattern(pattern)
        if normalized is None:
            raise ValueError(f"invalid_allowed_path_pattern:{pattern}")
        canonicalized.append(normalized)
    if not canonicalized:
        raise ValueError("allowed_paths_missing_or_empty")
    return sorted(set(canonicalized))


def compute_expected_contract_fingerprint(
    *,
    issue_number: int,
    contract_source_id: str,
    contract_body_sha256: str,
    allowed_paths: list[str],
    base_ref: str,
    base_sha_at_snapshot: str,
) -> dict[str, Any]:
    """
    Compute the 7-item expected_contract_fingerprint dict.

    allowed_paths_normalized_sha256 reuses
    AllowedPathsGateEvaluator.compute_allowed_paths_hash() verbatim (via the
    shared AllowedPathsMatcher normalization) so it is byte-for-byte
    reproducible by the reviewer's independent recomputation (AC4).
    """
    canonicalized = _canonicalize_allowed_paths_strict(allowed_paths)
    normalized_json = json.dumps(
        canonicalized, separators=(",", ":"), ensure_ascii=True
    )
    allowed_paths_hash = hashlib.sha256(normalized_json.encode()).hexdigest()
    return {
        "issue_number": issue_number,
        "contract_source_kind": "issue_comment",
        "contract_source_id": contract_source_id,
        "contract_body_sha256": contract_body_sha256,
        "allowed_paths_normalized_sha256": allowed_paths_hash,
        "base_ref": base_ref,
        "base_sha_at_snapshot": base_sha_at_snapshot,
    }


def capture_base_ref_and_sha(
    repo: str, timeout: int = _DEFAULT_TIMEOUT
) -> tuple[Optional[str], Optional[str]]:
    """
    Capture the repository default branch name and its current tip SHA via
    the GitHub API, for binding a materialized snapshot's fingerprint to a
    concrete (base_ref, base_sha_at_snapshot) pair (AC1).

    Returns (base_ref, base_sha_at_snapshot); either may be None on failure
    -- callers must treat (None, ...) or (..., None) as a capture failure and
    must not materialize a go without both values.
    """
    try:
        repo_result = subprocess.run(
            ["gh", "api", f"repos/{repo}", "--jq", ".default_branch"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, Exception):
        return None, None
    if repo_result.returncode != 0:
        return None, None
    base_ref = repo_result.stdout.strip() or None
    if not base_ref:
        return None, None

    try:
        sha_result = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{base_ref}", "--jq", ".sha"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, Exception):
        return base_ref, None
    if sha_result.returncode != 0:
        return base_ref, None
    base_sha = sha_result.stdout.strip() or None
    if not base_sha:
        return base_ref, None

    return base_ref, base_sha


def patch_comment(
    issue_number: int,
    repo: str,
    comment_id: int,
    body: str,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[bool, Optional[str]]:
    """
    PATCH an already-posted comment's body via the GitHub REST API (AC1
    step 2 of the two-phase materialize flow: POST provisional body, then
    PATCH the same comment id with the final body once the real comment id
    is known and can be embedded in expected_contract_fingerprint's
    contract_source_id).

    Returns (success, error_code_or_None).  The exact canonical UTF-8 JSON
    payload is sent once, then the PATCH response and an independent GET are
    both bound to that same body.  A transport timeout is reconciled by GET;
    ambiguity remains fail-closed.
    """
    try:
        payload = json.dumps({"body": body}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        result = subprocess.run(
            [
                "gh", "api", "--method", "PATCH",
                f"repos/{repo}/issues/comments/{comment_id}", "--input", "-",
            ],
            input=payload,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            try:
                response = json.loads(result.stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False, "patch_response_invalid_json"
            if not isinstance(response, dict) or response.get("id") != comment_id:
                return False, "patch_response_id_mismatch"
            if response.get("body") != body:
                return False, "patch_response_body_mismatch"
            ok, err = verify_controlled_publisher_comment_id_binding(
                issue_number, repo, comment_id, expected_body_sha256=sha256_of(body), timeout=timeout
            )
            return (True, None) if ok else (False, f"patch_get_reconciliation_failed:{err}")

        # The remote may have applied the update despite a transport error.
        ok, _err = verify_controlled_publisher_comment_id_binding(
            issue_number, repo, comment_id, expected_body_sha256=sha256_of(body), timeout=timeout
        )
        return (True, None) if ok else (False, "patch_transport_unreconciled")
    except subprocess.TimeoutExpired:
        ok, _err = verify_controlled_publisher_comment_id_binding(
            issue_number, repo, comment_id, expected_body_sha256=sha256_of(body), timeout=timeout
        )
        return (True, None) if ok else (False, "patch_timeout_unreconciled")
    except Exception:
        return False, "patch_error"


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
# Controlled publisher comment ID binding (#1475, AC3)
# ---------------------------------------------------------------------------

_COMMENT_ID_FROM_URL_RE = re.compile(r"#issuecomment-(\d+)$")


def extract_comment_id_from_url(url: Optional[str]) -> Optional[int]:
    """Extract the numeric comment id from a GitHub issue comment html_url."""
    if not url:
        return None
    m = _COMMENT_ID_FROM_URL_RE.search(url)
    if not m:
        return None
    return int(m.group(1))


def verify_controlled_publisher_comment_id_binding(
    issue_number: int,
    repo: str,
    expected_comment_id: Optional[int],
    expected_body_sha256: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[bool, Optional[str]]:
    """
    Confirm that the just-posted comment id is retrievable via the single
    comment GitHub REST endpoint (repos/{repo}/issues/comments/{id}) and that
    it is bound to this issue, this trusted publisher identity, and (when
    provided) the exact comment body that was intended to be published.

    Fail-closed (#1475 fix_delta P1 item 3): a missing/invalid id, fetch
    error, invalid payload, id mismatch, issue mismatch, untrusted publisher
    identity, or body hash mismatch all return (False, <reason_code>).

    This function only re-validates the state of the comment id at the
    moment it is called (publish-time direct GET). It does NOT by itself
    protect a later `existing_go` snapshot-reuse decision made in a
    different process run days/weeks later -- that decision is instead
    protected by requiring a strict identity-tuple match
    (contract_review_result_parser.is_trusted_snapshot_author) on every
    comment considered as an authoritative candidate, not by re-running this
    binding check at reuse-time. See the ensure_contract_snapshot docstring
    and the PR's Safety Claim Matrix for the explicit scope of this
    guarantee ("authored by an allowlisted GitHub account", not "byte-for-
    byte identical to what this specific process instance posted").
    """
    if (
        expected_comment_id is None
        or isinstance(expected_comment_id, bool)
        or not isinstance(expected_comment_id, int)
        or expected_comment_id <= 0
    ):
        return False, "missing_comment_id"
    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/{repo}/issues/comments/{expected_comment_id}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "binding_readback_timeout"
    except Exception:
        return False, "binding_readback_error"

    if result.returncode != 0:
        return False, "binding_readback_error"

    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return False, "binding_readback_invalid_json"
    if not isinstance(payload, dict):
        return False, "binding_readback_invalid_json"

    if payload.get("id") != expected_comment_id:
        return False, "binding_id_mismatch"

    issue_url = str(payload.get("issue_url") or "")
    expected_suffix = f"/repos/{repo}/issues/{issue_number}"
    if not issue_url.endswith(expected_suffix):
        return False, "binding_issue_mismatch"

    html_url = str(payload.get("html_url") or "")
    if not html_url or extract_comment_id_from_url(html_url) != expected_comment_id:
        return False, "binding_html_url_mismatch"

    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    author_login = user.get("login")
    author_id = user.get("id")
    author_type = user.get("type")
    author_association = payload.get("author_association")

    try:
        parser_mod = _import_parser_module()
    except Exception:
        return False, "binding_parser_import_error"

    if not parser_mod.is_trusted_snapshot_author(
        author_login,
        author_association,
        author_id=author_id,
        author_type=author_type,
    ):
        return False, "binding_publisher_untrusted"

    if expected_body_sha256:
        body = str(payload.get("body") or "")
        actual_body_sha256 = sha256_of(body)
        if actual_body_sha256 != expected_body_sha256:
            return False, "binding_body_hash_mismatch"

    return True, None


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
        # #1475 (fix_delta P1 item 1): trust filtering must be applied BEFORE
        # go/blocked precedence is decided, not only when selecting a go
        # candidate. Otherwise an untrusted comment posted after a trusted
        # go can still pre-empt it via the "latest blocked wins" branch below.
        latest = parser_mod.find_latest_result(results, trusted_only=True)
        try:
            go_result = parser_mod.find_latest_go(
                results, trusted_only=True, fingerprint_ready_only=True
            )
        except TypeError:
            # A legacy parser/test double cannot prove fingerprint readiness.
            # Do not fall back to its trusted-only result: absence of the
            # predicate is non-authoritative by contract.
            go_result = None

        # latest (trusted) blocked retains precedence over existing-go adoption.
        if latest and latest["status"] == "blocked":
            result["status"] = "blocked_needs_refinement"
            result["source"] = "latest_blocked"
            result["contract_snapshot_url"] = latest["html_url"]
            return result

        # #1475: parser_mod.find_latest_go(..., trusted_only=True) above already
        # excludes untrusted-author results; is_go_current only re-checks freshness.
        # #1537: an existing go lacking a well-formed source-bound
        # expected_contract_fingerprint must never be reused as current --
        # fall through to (re-)materialization instead.
        fingerprint_ready = bool(
            go_result
            and parser_mod.is_fingerprint_ready_go(
                go_result.get("inner", {}), go_result.get("comment_id"), issue_number
            )
        )
        if not is_go_current(go_result, body_sha256) or not fingerprint_ready:
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
    # #1475 (fix_delta P1 item 1): trust filtering before precedence, same as
    # the pre-post check above.
    latest_post = parser_mod.find_latest_result(results_post, trusted_only=True)
    if latest_post and latest_post["status"] == "blocked":
        result["status"] = "stale_or_conflicting_snapshot"
        result["errors"].append(
            "blocked_comment_appeared_during_atomicity_window"
        )
        return result

    # Also check if a go comment appeared in the interim
    try:
        go_post = parser_mod.find_latest_go(
            results_post, trusted_only=True, fingerprint_ready_only=True
        )
    except TypeError:
        # See the initial-read compatibility branch above: a parser without
        # fingerprint-ready support remains fail-closed.
        go_post = None
    go_post_fingerprint_ready = bool(
        go_post
        and parser_mod.is_fingerprint_ready_go(
            go_post.get("inner", {}), go_post.get("comment_id"), issue_number
        )
    )
    if is_go_current(go_post, body_sha256_post) and go_post_fingerprint_ready:
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

    # #1537 AC1: capture the fingerprint's (base_ref, base_sha_at_snapshot)
    # binding before materializing. If either cannot be captured, do not
    # post a go at all -- fail closed rather than emit a fingerprint bound to
    # an unknown/incomplete base.
    base_ref, base_sha_at_snapshot = capture_base_ref_and_sha(repo)
    if not base_ref or not base_sha_at_snapshot:
        result["status"] = "runtime_error"
        result["errors"].append(
            "base_ref_or_base_sha_capture_failed: cannot materialize a "
            "source-bound fingerprint without both values"
        )
        return result
    try:
        allowed_paths_at_post = extract_allowed_paths_from_body(body_post or "")
        # Validate before POST; a malformed contract must not create even a
        # provisional remote comment.
        _canonicalize_allowed_paths_strict(allowed_paths_at_post)
    except ValueError as exc:
        result["status"] = "runtime_error"
        result["errors"].append(f"allowed_paths_pre_post_validation_failed:{exc}")
        return result

    # Build comment to post (B6: include checks summary).
    # #1537 AC1 (two-phase materialize, step 1): the provisional body omits
    # expected_contract_fingerprint -- contract_source_id (the real GitHub
    # comment id) cannot be known before POST. A comment lacking the
    # fingerprint is never treated as fingerprint-ready by parsers/gates
    # (fail-closed), so this window is safe even if read concurrently.
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
        # #1475 fix_delta P1 item 3: verify the controlled publisher's
        # comment id is bound to this issue, this trusted publisher
        # identity, AND the exact comment body just posted, via an
        # independent direct-GET readback -- before treating the freshly
        # materialized snapshot as authoritative. Fail-closed on mismatch.
        expected_comment_id = extract_comment_id_from_url(url)
        bound_ok, binding_err = verify_controlled_publisher_comment_id_binding(
            issue_number,
            repo,
            expected_comment_id,
            expected_body_sha256=sha256_of(comment_body),
        )
        if not bound_ok:
            result["status"] = "controlled_publisher_binding_failed"
            result["contract_snapshot_url"] = None
            result["errors"].append(
                f"controlled_publisher_binding_failed: {binding_err}"
            )
            return result

        # #1537 AC1 (two-phase materialize, step 2): the real comment id is
        # now confirmed via independent read-back. Compute the source-bound
        # fingerprint using that confirmed id as contract_source_id, then
        # PATCH the SAME comment with a final body embedding it, and
        # re-verify via a second independent read-back before treating the
        # snapshot as authoritative.
        expected_contract_fingerprint = compute_expected_contract_fingerprint(
            issue_number=issue_number,
            contract_source_id=str(expected_comment_id),
            contract_body_sha256=body_sha256,
            allowed_paths=allowed_paths_at_post,
            base_ref=base_ref,
            base_sha_at_snapshot=base_sha_at_snapshot,
        )
        final_comment_body = _build_contract_review_comment(
            issue_number=issue_number,
            repo=repo,
            review_result=review_result,
            idempotency_marker=idempotency_marker,
            body_sha256=body_sha256,
            expected_contract_fingerprint=expected_contract_fingerprint,
        )
        patch_ok, patch_err = patch_comment(
            issue_number, repo, expected_comment_id, final_comment_body
        )
        if not patch_ok:
            result["status"] = "controlled_publisher_binding_failed"
            result["contract_snapshot_url"] = None
            result["errors"].append(f"fingerprint_patch_failed: {patch_err}")
            return result

        final_bound_ok, final_binding_err = verify_controlled_publisher_comment_id_binding(
            issue_number,
            repo,
            expected_comment_id,
            expected_body_sha256=sha256_of(final_comment_body),
        )
        if not final_bound_ok:
            result["status"] = "controlled_publisher_binding_failed"
            result["contract_snapshot_url"] = None
            result["errors"].append(
                f"fingerprint_patch_binding_failed: {final_binding_err}"
            )
            return result

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
    expected_contract_fingerprint: Optional[dict[str, Any]] = None,
) -> str:
    """
    Build the GitHub comment body for contract review posting.
    Includes checks summary (B6).

    expected_contract_fingerprint (#1537): when provided, embeds the
    source-bound 7-item fingerprint as a sibling key of CONTRACT_REVIEW_RESULT_V1.
    Left as None for the step-1 provisional POST (the real comment id is not
    known yet).  That POST is deliberately a pending-only schema, never a
    syntactically valid CONTRACT_REVIEW_RESULT_V1.
    """
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    owner_repo = repo.split("/")
    issue_url = (
        f"https://github.com/{repo}/issues/{issue_number}"
        if len(owner_repo) == 2
        else ""
    )

    if expected_contract_fingerprint is None:
        return f"""{idempotency_marker}

```yaml
CONTRACT_SNAPSHOT_MATERIALIZATION_PENDING_V1:
  issue_number: {issue_number}
  body_sha256: \"{body_sha256}\"
  phase: awaiting_comment_id_binding
```
"""

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

    fingerprint_json = json.dumps(
        expected_contract_fingerprint, ensure_ascii=False, separators=(",", ":")
    )
    fingerprint_yaml = f"\n  expected_contract_fingerprint: {fingerprint_json}"

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
  source: ensure_contract_snapshot_auto{fingerprint_yaml}
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
    elif status == "controlled_publisher_binding_failed":
        return 60
    else:  # runtime_error or unknown
        return 40


if __name__ == "__main__":
    sys.exit(main())
