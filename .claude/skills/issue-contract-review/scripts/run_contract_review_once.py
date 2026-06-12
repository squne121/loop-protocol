#!/usr/bin/env python3
"""
run_contract_review_once.py

既存 preflight scripts への薄い orchestration wrapper。
issue-contract-review の一連のチェックを 1 回実行し、
CONTRACT_REVIEW_ONCE_RESULT_V1 を stdout に compact JSON で返す。

Wrapper status:
  go             — 全チェック pass / status: go
  blocked        — 1 つ以上の決定論的 block
  human_judgment — 分類不能・native dependency 不可・ambiguous fallback
  runtime_error  — subprocess JSON parse 失敗や環境エラー (human_judgment ではない)

Exit codes:
  0  status: go
  1  status: blocked
  2  status: human_judgment
  3  status: runtime_error
  4  input/argument error

stdout: CONTRACT_REVIEW_ONCE_RESULT_V1 compact JSON のみ
stderr: debug/diagnostic messages のみ（stdout には混入しない）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
# parents: [0]=issue-contract-review, [1]=skills, [2]=.claude, [3]=<repo root>
_REPO_ROOT = _SCRIPTS_DIR.parents[3]

_CONTRACT_READINESS_CHECK_PY = _SCRIPTS_DIR / "contract_readiness_check.py"
_BASELINE_VC_PREFLIGHT_PY = _SCRIPTS_DIR / "baseline_vc_preflight.py"
_CHECK_BLOCKERS_SH = _SCRIPTS_DIR / "check_blockers.sh"
_CHECK_PRODUCT_SPEC_PY = _SCRIPTS_DIR / "check_product_spec_contract.py"

# VC_PREFLIGHT_TIMEOUT_SECS: baseline_vc_preflight may take up to 120s per VC
_VC_PREFLIGHT_TIMEOUT = 180
_DEFAULT_TIMEOUT = 30

_IDEMPOTENCY_MARKER_PREFIX = "<!-- loop-protocol:contract-review-once"


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _run_script(
    cmd: list[str],
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[Optional[dict], int, Optional[str]]:
    """
    Run a script and parse stdout as JSON.
    Returns (parsed_json_or_None, exit_code, error_message_or_None).

    subprocess JSON parse failure → runtime_error (NOT human_judgment).
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = result.stdout.strip()
        if not stdout:
            return None, result.returncode, f"no stdout from {cmd[0]}"
        try:
            parsed = json.loads(stdout)
            return parsed, result.returncode, None
        except json.JSONDecodeError as exc:
            # JSON parse failure → runtime_error (AC design: NOT human_judgment)
            return None, result.returncode, f"json_parse_error: {exc}"
    except subprocess.TimeoutExpired:
        return None, -1, "timeout"
    except FileNotFoundError:
        return None, -1, f"script_not_found: {cmd[0]}"
    except Exception as exc:
        return None, -1, f"subprocess_error: {exc}"


# ---------------------------------------------------------------------------
# Idempotency: check for existing go comment
# ---------------------------------------------------------------------------


def check_existing_go_comment(
    issue_number: int,
    repo: str,
) -> tuple[Optional[str], Optional[str]]:
    """
    Check if a valid CONTRACT_REVIEW_RESULT_V1 status: go comment already exists.
    Returns (html_url_or_None, error_code_or_None).
    """
    try:
        from contract_review_result_parser import (
            fetch_issue_comments,
            find_latest_go,
            find_latest_result,
            parse_contract_review_results,
        )
    except ImportError:
        # Try absolute path import
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "contract_review_result_parser",
            _SCRIPTS_DIR / "contract_review_result_parser.py",
        )
        if spec is None or spec.loader is None:
            return None, "import_error"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        fetch_issue_comments = mod.fetch_issue_comments
        find_latest_go = mod.find_latest_go
        find_latest_result = mod.find_latest_result
        parse_contract_review_results = mod.parse_contract_review_results

    repo_parts = repo.split("/")
    if len(repo_parts) == 2:
        owner, repo_name = repo_parts
        issue_url = f"https://github.com/{owner}/{repo_name}/issues/{issue_number}"
    else:
        issue_url = None

    comments, err = fetch_issue_comments(issue_number, repo)
    if err:
        return None, err

    results = parse_contract_review_results(comments, expected_issue_url=issue_url)
    latest = find_latest_result(results)

    # If the latest result is blocked, do not return an existing go
    if latest and latest["status"] == "blocked":
        return None, None

    go = find_latest_go(results)
    if go:
        return go["html_url"], None
    return None, None


# ---------------------------------------------------------------------------
# HTTP error classification (for API post calls — 403/429/422 blind retry forbidden)
# ---------------------------------------------------------------------------

# This module does not post comments itself; ensure_contract_snapshot.py handles posting.
# The classification table is here for consistency and is exported.

HTTP_ERROR_CLASSIFICATIONS: dict[int, str] = {
    403: "permission_denied",
    429: "rate_limited",
    422: "validation_failed_or_spam",
}


def classify_http_error(status_code: int) -> str:
    """
    Classify HTTP error code for contract review API calls.
    403/429/422 → no blind retry (ambiguous_no_retry for unknown codes).
    """
    return HTTP_ERROR_CLASSIFICATIONS.get(status_code, "ambiguous_no_retry")


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def run_once(
    issue_number: int,
    repo: str,
    mode: str = "static",
    skip_idempotency_check: bool = False,
) -> dict[str, Any]:
    """
    Run issue-contract-review checks once for the given issue.

    Returns CONTRACT_REVIEW_ONCE_RESULT_V1 dict.
    """
    result: dict[str, Any] = {
        "schema": "CONTRACT_REVIEW_ONCE_RESULT_V1",
        "issue_number": issue_number,
        "repo": repo,
        "mode": mode,
        "status": "runtime_error",
        "source": None,
        "go_comment_url": None,
        "readiness_status": None,
        "readiness_errors": [],
        "vc_preflight_status": None,
        "vc_preflight_classifications": [],
        "idempotency_check": {
            "performed": not skip_idempotency_check,
            "existing_go_url": None,
            "deduped": False,
        },
        "errors": [],
    }

    # Step 1: idempotency check — if existing go exists, return early
    if not skip_idempotency_check:
        existing_url, id_err = check_existing_go_comment(issue_number, repo)
        result["idempotency_check"]["existing_go_url"] = existing_url
        if id_err:
            result["errors"].append(f"idempotency_check_error: {id_err}")
            # non-fatal: continue
        elif existing_url:
            # Already has a valid go comment — return deduped
            result["status"] = "go"
            result["source"] = "existing_go_comment"
            result["go_comment_url"] = existing_url
            result["idempotency_check"]["deduped"] = True
            return result

    # Step 2: contract_readiness_check.py (static check)
    readiness_cmd = [
        sys.executable,
        str(_CONTRACT_READINESS_CHECK_PY),
        "--issue",
        str(issue_number),
        "--issue-number",  # alias for compatibility
        str(issue_number),
        "--repo",
        repo,
        "--mode",
        mode if mode in ("static", "preflight-static", "execute") else "static",
    ]
    # Use --issue (primary arg) only — avoids duplicate
    readiness_cmd = [
        sys.executable,
        str(_CONTRACT_READINESS_CHECK_PY),
        "--issue",
        str(issue_number),
        "--repo",
        repo,
        "--mode",
        mode if mode in ("static", "preflight-static", "execute") else "static",
    ]

    readiness_json, readiness_rc, readiness_err = _run_script(readiness_cmd)

    if readiness_err:
        result["errors"].append(f"readiness_check_error: {readiness_err}")
        result["status"] = "runtime_error"
        return result

    if readiness_json is None:
        result["errors"].append("readiness_check_no_output")
        result["status"] = "runtime_error"
        return result

    readiness_status = readiness_json.get("status", "")
    result["readiness_status"] = readiness_status
    result["readiness_errors"] = readiness_json.get("errors", [])

    # Map readiness status
    if readiness_status == "human_judgment":
        result["status"] = "human_judgment"
        result["source"] = "readiness_check"
        return result
    elif readiness_status == "needs_fix":
        result["status"] = "blocked"
        result["source"] = "readiness_check"
        return result
    elif readiness_status != "go":
        # Unknown status from readiness check
        result["status"] = "runtime_error"
        result["errors"].append(f"unknown_readiness_status: {readiness_status}")
        return result

    # Readiness is go — run VC preflight if mode is execute
    if mode == "execute":
        vc_result_json, vc_rc, vc_err = _run_script(
            [
                sys.executable,
                str(_BASELINE_VC_PREFLIGHT_PY),
                "--issue",
                str(issue_number),
                "--repo",
                repo,
            ],
            timeout=_VC_PREFLIGHT_TIMEOUT,
        )

        if vc_err:
            result["errors"].append(f"vc_preflight_error: {vc_err}")
            result["status"] = "runtime_error"
            return result

        if vc_result_json is None:
            result["errors"].append("vc_preflight_no_output")
            result["status"] = "runtime_error"
            return result

        vc_status = vc_result_json.get("status", "")
        result["vc_preflight_status"] = vc_status
        result["vc_preflight_classifications"] = vc_result_json.get("results", [])

        if vc_status == "human_judgment":
            result["status"] = "human_judgment"
            result["source"] = "vc_preflight"
            return result
        elif vc_status == "blocked":
            result["status"] = "blocked"
            result["source"] = "vc_preflight"
            return result
        elif vc_status == "pass":
            result["status"] = "go"
            result["source"] = "vc_preflight_pass"
            return result
        else:
            # Unknown vc status
            result["status"] = "runtime_error"
            result["errors"].append(f"unknown_vc_preflight_status: {vc_status}")
            return result
    else:
        # static/preflight-static mode: readiness go is sufficient
        result["status"] = "go"
        result["source"] = "readiness_check_static"
        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "run_contract_review_once: run issue-contract-review checks once, "
            "return CONTRACT_REVIEW_ONCE_RESULT_V1 JSON"
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
        default="squne121/loop-protocol",
        help="GitHub repo (owner/name)",
    )
    parser.add_argument(
        "--mode",
        choices=["static", "preflight-static", "execute"],
        default="static",
        help="Check mode (default: static)",
    )
    parser.add_argument(
        "--skip-idempotency-check",
        action="store_true",
        default=False,
        help="Skip existing go comment check",
    )

    args = parser.parse_args()

    result = run_once(
        issue_number=args.issue_number,
        repo=args.repo,
        mode=args.mode,
        skip_idempotency_check=args.skip_idempotency_check,
    )

    print(json.dumps(result))

    status = result.get("status", "runtime_error")
    if status == "go":
        return 0
    elif status == "blocked":
        return 1
    elif status == "human_judgment":
        return 2
    else:  # runtime_error
        return 3


if __name__ == "__main__":
    sys.exit(main())
