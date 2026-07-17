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

    #1475 (fix_delta P1 item 2): the jq projection now also requests
    user.id / user.type so the trusted-publisher check can bind to the full
    GitHub identity tuple (id, login, type, association), not login/
    association alone.

    #1475 (fix_delta P2 item 4): fail-closed NDJSON handling. A single
    malformed line makes the whole fetch untrustworthy for authoritative
    go/blocked precedence -- a truncated/corrupted stream could silently
    drop the newest trusted comment and make a stale one look authoritative.
    Any json.JSONDecodeError on a non-empty line aborts the fetch with
    comments_fetch_incomplete instead of silently continuing with a partial
    comment list.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{repo}/issues/{issue_number}/comments?per_page=100",
                "--jq",
                '.[] | {id, html_url, created_at, updated_at, body, '
                'author: .user.login, author_id: .user.id, '
                'author_type: .user.type, author_association}',
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
            if not line:
                continue
            try:
                comments.append(json.loads(line))
            except json.JSONDecodeError:
                # Fail-closed (#1475 P2 item 4): a partially-decoded comment
                # stream must never be treated as a complete evidence set.
                return [], "comments_fetch_incomplete"
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
    Parse a YAML block using yaml.safe_load when available, with a
    minimal key-value fallback for environments where PyYAML is absent.

    Preference: yaml.safe_load (PyYAML) — handles nested structures, quoted
    strings, and all standard YAML scalar types correctly.
    Fallback: custom line-by-line parser (flat + one level of nesting only).
    """
    try:
        import yaml  # noqa: F401 — available in project venv (PyYAML)
        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except Exception:
        pass

    # --- Minimal fallback parser (used only when yaml is unavailable) ---
    result: dict[str, Any] = {}
    lines = block.splitlines()
    current_key: Optional[str] = None

    for line in lines:
        stripped = line.rstrip()
        if not stripped or stripped.lstrip().startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())

        if indent == 0:
            current_key = None
            m = re.match(r'^(\S[^:]*?):\s*(.*)', stripped)
            if m:
                key = m.group(1).strip()
                val = m.group(2).strip()
                if val:
                    if (val.startswith('"') and val.endswith('"')) or (
                        val.startswith("'") and val.endswith("'")
                    ):
                        val = val[1:-1]
                    result[key] = val
                else:
                    result[key] = None
                    current_key = key
        elif current_key is not None:
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
# Trust policy (GitHub provenance, #1475)
# ---------------------------------------------------------------------------

# Trusted GitHub author_association values. NONE/CONTRIBUTOR/FIRST_TIME_CONTRIBUTOR
# are excluded so that arbitrary outside commenters cannot post an authoritative
# snapshot. Retained for callers that only need the (weaker) association-only
# check as a display/audit signal; it is no longer sufficient on its own to
# authorize a snapshot -- see TRUSTED_CONTRACT_PUBLISHERS below (#1475).
TRUSTED_AUTHOR_ASSOCIATIONS: frozenset[str] = frozenset(
    {"OWNER", "MEMBER", "COLLABORATOR"}
)

# #1475 (fix_delta P1 item 2): static allowlist of controlled contract-snapshot
# publishers, keyed by the immutable GitHub user.id. author_association alone
# authorizes any current repo COLLABORATOR/MEMBER, which is too broad for an
# authoritative security-relevant snapshot. Authorization now requires the
# full (user.id, user.login, user.type, author_association) tuple to match a
# single allowlisted entry exactly. login is used for audit display and
# rename-drift detection only -- it is never sufficient by itself, and
# association is never sufficient by itself either.
TRUSTED_CONTRACT_PUBLISHERS: dict[int, dict[str, Any]] = {
    63350259: {
        "expected_login": "squne121",
        "expected_type": "User",
        "allowed_associations": frozenset({"OWNER"}),
    },
}


def is_trusted_snapshot_author(
    author: Optional[str],
    author_association: Optional[str],
    *,
    author_id: Optional[int] = None,
    author_type: Optional[str] = None,
) -> bool:
    """
    Decide whether a GitHub comment's full identity tuple is allowed to
    publish an authoritative CONTRACT_REVIEW_RESULT_V1 snapshot.

    Fail-closed (#1475 P1 item 2): identity authorization requires an exact
    match against TRUSTED_CONTRACT_PUBLISHERS, keyed by author_id (GitHub
    user.id, immutable). author_association alone is never sufficient --
    it no longer authorizes arbitrary repo COLLABORATOR/MEMBER accounts.

    Validation order:
      1. author_id must be present, a non-bool int, and > 0.
      2. author_id must be a key in TRUSTED_CONTRACT_PUBLISHERS.
      3. author (login) must equal the allowlisted expected_login.
      4. author_type must equal the allowlisted expected_type.
      5. author_association must be one of the allowlisted associations.
    All five conditions must hold; any missing/mismatched field is untrusted.
    """
    if author_id is None or isinstance(author_id, bool) or not isinstance(author_id, int):
        return False
    if author_id <= 0:
        return False

    entry = TRUSTED_CONTRACT_PUBLISHERS.get(author_id)
    if entry is None:
        return False

    if not author or author != entry["expected_login"]:
        return False
    if not author_type or author_type != entry["expected_type"]:
        return False
    if not author_association or author_association not in entry["allowed_associations"]:
        return False

    return True


# ---------------------------------------------------------------------------
# Source-bound contract fingerprint (Issue #1537)
# ---------------------------------------------------------------------------

# The 7-item fingerprint schema. Must stay identical to
# pr-review-judge/scripts/allowed_paths_review_gate.py ContractFingerprint --
# both producer (ensure_contract_snapshot.py) and consumer (this parser, the
# reviewer gate) must agree on the same key set for source-bound freshness
# comparison to be meaningful.
_FINGERPRINT_REQUIRED_KEYS = (
    "issue_number",
    "contract_source_kind",
    "contract_source_id",
    "contract_body_sha256",
    "allowed_paths_normalized_sha256",
    "base_ref",
    "base_sha_at_snapshot",
)

# contract_body_sha256 follows the repo-wide `sha256:<64 hex>` convention used
# by body_sha256 elsewhere in this schema (ensure_contract_snapshot.sha256_of).
_PREFIXED_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# allowed_paths_normalized_sha256 must byte-for-byte match
# AllowedPathsGateEvaluator.compute_allowed_paths_hash(), which returns a bare
# hex digest with NO "sha256:" prefix -- do not add one here.
_BARE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FULL_COMMIT_OID_RE = re.compile(r"^[0-9a-f]{40,64}$")

_ISSUE_NUMBER_FROM_URL_RE = re.compile(r"/issues/(\d+)\Z")


def _issue_number_from_url(issue_url: Optional[str]) -> Optional[int]:
    """Extract the trailing issue number from a GitHub issue URL."""
    if not issue_url:
        return None
    m = _ISSUE_NUMBER_FROM_URL_RE.search(issue_url)
    return int(m.group(1)) if m else None


def is_fingerprint_ready_go(
    inner: Any,
    comment_id: Optional[int] = None,
    issue_number: Optional[int] = None,
) -> bool:
    """
    Return whether a parsed CONTRACT_REVIEW_RESULT_V1 `inner` block carries a
    well-formed, source-bound `expected_contract_fingerprint` (Issue #1537).

    Fail-closed: any missing key, wrong type, malformed hash, or mismatch
    between the fingerprint's self-declared contract_source_id / issue_number
    and the actual comment / issue this block was read from makes the result
    NOT fingerprint-ready (False), regardless of `status`. `comment_id` /
    `issue_number` are independent authorities (the real GitHub comment id
    the block was parsed from, and the issue this parse run is scoped to) --
    when supplied they are cross-checked against the fingerprint's own
    self-reported values rather than trusted at face value.
    """
    if (
        isinstance(comment_id, bool)
        or not isinstance(comment_id, int)
        or comment_id <= 0
        or isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or issue_number <= 0
    ):
        return False
    if not isinstance(inner, dict):
        return False
    fingerprint = inner.get("expected_contract_fingerprint")
    if not isinstance(fingerprint, dict):
        return False
    if set(fingerprint) != set(_FINGERPRINT_REQUIRED_KEYS):
        return False

    fp_issue_number = fingerprint.get("issue_number")
    if isinstance(fp_issue_number, bool) or not isinstance(fp_issue_number, int):
        return False
    if fp_issue_number <= 0:
        return False
    if fp_issue_number != issue_number:
        return False

    if fingerprint.get("contract_source_kind") != "issue_comment":
        return False

    contract_source_id = fingerprint.get("contract_source_id")
    if not isinstance(contract_source_id, str) or not contract_source_id.isdigit():
        return False
    if str(comment_id) != contract_source_id:
        return False

    contract_body_sha256 = fingerprint.get("contract_body_sha256")
    if not isinstance(contract_body_sha256, str) or not _PREFIXED_SHA256_RE.fullmatch(
        contract_body_sha256
    ):
        return False
    if contract_body_sha256 != inner.get("body_sha256"):
        return False

    allowed_paths_hash = fingerprint.get("allowed_paths_normalized_sha256")
    if not isinstance(allowed_paths_hash, str) or not _BARE_SHA256_RE.fullmatch(
        allowed_paths_hash
    ):
        return False

    base_ref = fingerprint.get("base_ref")
    if not isinstance(base_ref, str) or not base_ref:
        return False

    base_sha_at_snapshot = fingerprint.get("base_sha_at_snapshot")
    if not isinstance(base_sha_at_snapshot, str) or not _FULL_COMMIT_OID_RE.fullmatch(
        base_sha_at_snapshot
    ):
        return False

    return True


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
                author = comment.get("author")
                author_association = comment.get("author_association")
                author_id = comment.get("author_id")
                author_type = comment.get("author_type")
                results.append(
                    {
                        "comment_id": comment.get("id"),
                        "html_url": comment.get("html_url", ""),
                        "created_at": comment.get("created_at", ""),
                        "block": parsed,
                        "inner": inner,
                        "status": inner.get("status", ""),
                        "author": author,
                        "author_association": author_association,
                        "author_id": author_id,
                        "author_type": author_type,
                        "is_trusted_author": is_trusted_snapshot_author(
                            author,
                            author_association,
                            author_id=author_id,
                            author_type=author_type,
                        ),
                        "is_fingerprint_ready": is_fingerprint_ready_go(
                            inner,
                            comment.get("id"),
                            _issue_number_from_url(expected_issue_url),
                        ),
                    }
                )
                # Only take first valid block per comment
                break

    return results


def filter_authoritative_results(results: list[dict]) -> list[dict]:
    """
    Return only the subset of parsed results whose author identity passes
    is_trusted_snapshot_author (#1475 P1 item 1).

    This is the single shared authoritative-candidate set: it MUST be applied
    to go/blocked precedence decisions BEFORE choosing the "latest" result,
    not only when selecting a go candidate. Applying trust filtering only to
    find_latest_go (and leaving find_latest_result unfiltered) allows an
    untrusted `status: blocked` comment posted after a trusted `status: go`
    to incorrectly take precedence and halt the workflow (the exact bug this
    function closes).
    """
    return [r for r in results if r.get("is_trusted_author") is True]


def find_latest_go(
    results: list[dict],
    *,
    trusted_only: bool = False,
    fingerprint_ready_only: bool = False,
) -> Optional[dict]:
    """
    Return the latest (by created_at desc, comment_id desc) valid
    CONTRACT_REVIEW_RESULT_V1 with status: go.
    Returns None if no go result found.

    trusted_only (#1475): when True, results whose is_trusted_author is not
    True are excluded from candidacy. A schema-valid but untrusted
    `status: go` is never returned as authoritative when trusted_only=True.

    fingerprint_ready_only (#1537): when True, results whose
    is_fingerprint_ready is not True are excluded from candidacy. A
    schema-valid, trusted `status: go` that lacks a well-formed source-bound
    expected_contract_fingerprint is never returned as loop-consumable when
    fingerprint_ready_only=True -- callers must re-materialize instead.
    """
    go_results = [r for r in results if r["status"] == "go"]
    if trusted_only:
        go_results = filter_authoritative_results(go_results)
    if fingerprint_ready_only:
        go_results = [r for r in go_results if r.get("is_fingerprint_ready") is True]
    if not go_results:
        return None
    # Sort by created_at desc, then comment_id desc
    go_results.sort(key=lambda r: (r.get("created_at", ""), r.get("comment_id", 0)), reverse=True)
    return go_results[0]


def find_latest_authoritative_go(results: list[dict]) -> Optional[dict]:
    """Return the single loop-consumable GO candidate.

    Trusted-but-provisional/orphan comments are intentionally excluded here.
    All authoritative consumers must use this predicate.
    """
    return find_latest_go(
        results, trusted_only=True, fingerprint_ready_only=True
    )


def find_latest_result(
    results: list[dict],
    *,
    trusted_only: bool = False,
) -> Optional[dict]:
    """
    Return the latest (by created_at desc, comment_id desc) valid
    CONTRACT_REVIEW_RESULT_V1 regardless of status.

    trusted_only (#1475 P1 item 1): when True, only authoritative
    (trusted-author) results are considered before selecting the latest
    entry. Every consumer that uses find_latest_result to decide go/blocked
    precedence MUST pass trusted_only=True, or an untrusted outsider comment
    can silently pre-empt a trusted go/blocked snapshot regardless of which
    status it carries.
    """
    candidates = filter_authoritative_results(results) if trusted_only else results
    if not candidates:
        return None
    sorted_results = sorted(
        candidates,
        key=lambda r: (r.get("created_at", ""), r.get("comment_id", 0)),
        reverse=True,
    )
    return sorted_results[0]
