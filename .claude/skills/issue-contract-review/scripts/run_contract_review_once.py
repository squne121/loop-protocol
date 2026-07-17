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

Check execution order (all modes):
  1. contract_readiness_check.py  — readiness needs_fix → blocked
  2. check_blockers.sh            — exit 1 / fallback ambiguous → blocked/human_judgment
  3. check_product_spec_contract.py — applicable+fail → blocked; applicable+human_judgment → human_judgment
  4. baseline_vc_preflight.py     — blocked → blocked; human_judgment → human_judgment
     (vc_preflight is run in all modes, not only execute)

All four checks pass → status: go with checks summary.
"""

from __future__ import annotations

import argparse
import json
import re
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
_EVALUATE_PRODUCT_SPEC_GATE_PY = (
    _SCRIPTS_DIR.parent.parent / "impl-review-loop" / "scripts"
)
if str(_EVALUATE_PRODUCT_SPEC_GATE_PY) not in sys.path:
    sys.path.insert(0, str(_EVALUATE_PRODUCT_SPEC_GATE_PY))

from evaluate_product_spec_gate import evaluate_product_spec_payload  # noqa: E402

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Issue #1333 AC2/AC3: baseline_vc_preflight.py の DEFAULT_TIMEOUT_SECONDS を
# import し、per-command timeout の単一の正本として参照する（drift防止）。
from baseline_vc_preflight import DEFAULT_TIMEOUT_SECONDS as _VC_PREFLIGHT_PER_COMMAND_TIMEOUT  # noqa: E402

# Issue #1333 AC2: _VC_PREFLIGHT_TIMEOUT は per-command timeout の named
# constant から関係式として導出する（単純な独立リテラル引き上げは禁止）。
# _VC_PREFLIGHT_MAX_COMMAND_BUDGET: Issue #1333 の暫定的な wrapper timeout
# budget（直列実行の想定上限コマンド数）。Issue #1328 のような、同一の重い
# VC コマンドが多数の AC から重複参照される構造への根本対策（dedup/replay・
# bounded parallel execution による総実行時間削減）は Issue #1338 で扱う。
# 本定数は #1328 型のケースを timeout 側だけで完全に吸収することを意図した
# ものではない。
# _VC_PREFLIGHT_OVERHEAD_SECONDS: subprocess 起動・JSON parse 等の固定オーバーヘッド。
_VC_PREFLIGHT_MAX_COMMAND_BUDGET = 6
_VC_PREFLIGHT_OVERHEAD_SECONDS = 60
_VC_PREFLIGHT_TIMEOUT = (
    _VC_PREFLIGHT_PER_COMMAND_TIMEOUT * _VC_PREFLIGHT_MAX_COMMAND_BUDGET
    + _VC_PREFLIGHT_OVERHEAD_SECONDS
)
_DEFAULT_TIMEOUT = 30

# Issue #1338 AC9: named constant for the --max-workers value explicitly
# passed to baseline_vc_preflight.py. Bounded parallel execution there is
# restricted to a dedicated safe read-only predicate (`rg` with a fully
# validated bounded path operand, or exact test -f|-d|-s PATH); pnpm/uv run
# pytest/pytest/gh/git/github_metadata_assert always stay serial regardless
# of this value. grep/egrep/fgrep are intentionally EXCLUDED from the
# parallel-eligible predicate (PR #1508 review P0-2): the prior basename-only
# classification allowed them into the pool with no path/stdin validation.
_VC_PREFLIGHT_MAX_WORKERS = 2

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


def _run_shell_script(
    cmd: list[str],
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[int, str, str]:
    """
    Run a shell script (non-JSON output).
    Returns (exit_code, stdout, stderr).
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", f"script_not_found: {cmd[0]}"
    except Exception as exc:
        return -1, "", f"subprocess_error: {exc}"


# ---------------------------------------------------------------------------
# Idempotency: check for existing go comment
# ---------------------------------------------------------------------------


def _is_current_go_snapshot(go_result: object, expected_body_sha256: str) -> bool:
    """Use the loop consumer's currentness predicate for producer dedupe."""
    import importlib.util

    ensure_path = (
        _SCRIPTS_DIR.parent.parent / "impl-review-loop" / "scripts"
        / "ensure_contract_snapshot.py"
    )
    spec = importlib.util.spec_from_file_location("ensure_contract_snapshot", ensure_path)
    if spec is None or spec.loader is None:
        return False
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return bool(module.is_go_current(go_result, expected_body_sha256))


def check_existing_go_comment(
    issue_number: int,
    repo: str,
) -> tuple[Optional[dict], Optional[str]]:
    """
    Return a current, complete go snapshot or None.

    Dedupe is only safe when the existing comment satisfies the same
    currentness predicate consumed by impl-review-loop.
    """
    try:
        from contract_review_result_parser import (
            fetch_issue_comments,
            find_latest_authoritative_go,
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
        find_latest_authoritative_go = getattr(
            mod, "find_latest_authoritative_go",
            lambda results: mod.find_latest_go(results, trusted_only=True, fingerprint_ready_only=True),
        )
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
    # #1475 (fix_delta P1 item 1): trust filtering must be applied BEFORE
    # go/blocked precedence is decided. An untrusted comment posted after a
    # trusted go must never pre-empt that go, regardless of its status.
    latest = find_latest_result(results, trusted_only=True)

    # If the latest (trusted) result is blocked, do not return an existing go
    if latest and latest["status"] == "blocked":
        return None, None

    # #1475: only a trusted-author go snapshot is authoritative for dedupe.
    go = find_latest_authoritative_go(results)
    if go is None:
        return None, None

    try:
        issue = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "body"],
            capture_output=True,
            text=True,
            timeout=_DEFAULT_TIMEOUT,
        )
        if issue.returncode != 0:
            return None, "issue_body_fetch_error"
        current_body = json.loads(issue.stdout).get("body", "")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None, "issue_body_fetch_error"

    import hashlib

    current_body_sha256 = "sha256:" + hashlib.sha256(
        current_body.encode("utf-8")
    ).hexdigest()
    if _is_current_go_snapshot(go, current_body_sha256):
        return go, None
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


_FULL_COMMIT_OID_RE = re.compile(r"^[0-9a-f]{40,64}$")


def validate_current_head_envelope(payload: Any, returncode: int) -> list[str]:
    """Return fail-closed validation errors for a producer current-head envelope."""
    if returncode != 0:
        return [f"producer_nonzero_exit:{returncode}"]
    if not isinstance(payload, dict):
        return ["producer_payload_not_object"]

    errors: list[str] = []
    required_scalars = {
        "schema": "baseline_vc_preflight/v1",
        "evidence_mode": "current-head",
        "status": "pass",
    }
    for key, expected in required_scalars.items():
        if payload.get(key) != expected:
            errors.append(f"invalid_{key}")
    if not isinstance(payload.get("generated_at"), str) or not payload["generated_at"]:
        errors.append("missing_generated_at")
    if payload.get("errors") != []:
        errors.append("errors_not_empty")
    if not isinstance(payload.get("results"), list):
        errors.append("missing_results")
    source = payload.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("body_sha256"), str) or not source["body_sha256"]:
        errors.append("missing_source_body_sha256")
    for key in ("fallback_detected", "human_review_required", "stop_condition_triggered"):
        if payload.get(key) is not False:
            errors.append(f"unsafe_{key}")
    for key in ("clean_before", "clean_after"):
        if payload.get(key) is not True:
            errors.append(f"unclean_{key}")
    head_values = [payload.get(key) for key in ("head_sha", "reviewed_head_sha", "head_after_sha")]
    if (
        not all(isinstance(value, str) and _FULL_COMMIT_OID_RE.fullmatch(value) for value in head_values)
        or len(set(head_values)) != 1
    ):
        errors.append("head_sha_mismatch_or_invalid")
    return errors


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
    evidence_mode: str = "baseline",
    cwd: str | None = None,
    reviewed_head_sha: str | None = None,
) -> dict[str, Any]:
    """
    Run issue-contract-review checks once for the given issue.

    Execution order:
      1. contract_readiness_check.py
      2. check_blockers.sh
      3. check_product_spec_contract.py
      4. baseline_vc_preflight.py

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
        "vc_evidence": {"mode": evidence_mode},
        "current_vc_result": None,
        "checks": {
            "readiness": None,
            "blockers": None,
            "product_spec": None,
            "product_spec_check": None,
            "vc_preflight": None,
        },
        "idempotency_check": {
            "performed": not skip_idempotency_check,
            "existing_go_url": None,
            "deduped": False,
        },
        "errors": [],
    }

    # Step 1: idempotency check — if existing go exists, return early
    if not skip_idempotency_check:
        existing_go, id_err = check_existing_go_comment(issue_number, repo)
        existing_url = existing_go.get("html_url") if existing_go else None
        result["idempotency_check"]["existing_go_url"] = existing_url
        if id_err:
            result["errors"].append(f"idempotency_check_error: {id_err}")
            # non-fatal: continue
        elif existing_go:
            # Already has a valid go comment — return deduped
            result["status"] = "go"
            result["source"] = "existing_go_comment"
            result["go_comment_url"] = existing_url
            result["idempotency_check"]["deduped"] = True
            checks = existing_go.get("inner", {}).get("checks", {})
            result["checks"]["product_spec_check"] = checks.get("product_spec_check")
            return result

    # Step 2: contract_readiness_check.py (static check)
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
        result["checks"]["readiness"] = "human_judgment"
        result["status"] = "human_judgment"
        result["source"] = "readiness_check"
        return result
    elif readiness_status == "needs_fix":
        result["checks"]["readiness"] = "needs_fix"
        result["status"] = "blocked"
        result["source"] = "readiness_check"
        return result
    elif readiness_status != "go":
        # Unknown status from readiness check
        result["status"] = "runtime_error"
        result["errors"].append(f"unknown_readiness_status: {readiness_status}")
        return result
    else:
        result["checks"]["readiness"] = "go"

    # Step 3: check_blockers.sh
    blockers_rc, blockers_stdout, blockers_stderr = _run_shell_script(
        ["bash", str(_CHECK_BLOCKERS_SH), str(issue_number), repo],
        timeout=_DEFAULT_TIMEOUT,
    )

    if blockers_rc == -1:
        # Script not found or timeout
        result["errors"].append(f"check_blockers_error: {blockers_stderr}")
        result["status"] = "runtime_error"
        return result
    elif blockers_rc == 0:
        result["checks"]["blockers"] = "pass"
    else:
        # exit 1 from check_blockers.sh:
        #   "blocker が open" → deterministic blocked
        #   "native dependency API unavailable" / "不一致" (mismatch) → human_judgment
        stderr_lower = blockers_stderr.lower()
        # Detect truly-ambiguous cases: API unavailable with no fallback, or mismatch
        is_ambiguous = (
            "unavailable" in stderr_lower
            or "mismatch" in stderr_lower
            or "不一致" in blockers_stderr
            or "ambiguous" in stderr_lower
        )
        if is_ambiguous:
            result["checks"]["blockers"] = "human_judgment"
            result["status"] = "human_judgment"
            result["source"] = "check_blockers"
            result["errors"].append(f"check_blockers_human_judgment: {blockers_stderr.strip()}")
            return result
        else:
            # blocker open OR fallback-based determination
            result["checks"]["blockers"] = "blocked"
            result["status"] = "blocked"
            result["source"] = "check_blockers"
            result["errors"].append(f"check_blockers_blocked: {blockers_stderr.strip()}")
            return result

    # Step 4: check_product_spec_contract.py
    product_spec_json, product_spec_rc, product_spec_err = _run_script(
        [
            sys.executable,
            str(_CHECK_PRODUCT_SPEC_PY),
            "--issue-number",
            str(issue_number),
            "--repo",
            repo,
        ],
        timeout=_DEFAULT_TIMEOUT,
    )

    if product_spec_err:
        result["errors"].append(f"product_spec_check_error: {product_spec_err}")
        result["status"] = "runtime_error"
        return result

    if product_spec_json is None:
        result["errors"].append("product_spec_check_no_output")
        result["status"] = "runtime_error"
        return result

    if product_spec_rc not in (0, 1):
        result["errors"].append(
            f"product_spec_check_nonzero_exit: rc={product_spec_rc}"
        )
        result["status"] = "runtime_error"
        return result

    gate = evaluate_product_spec_payload(
        product_spec_json,
        issue_url=f"https://github.com/{repo}/issues/{issue_number}",
        body_sha256=product_spec_json.get("body_sha256") if isinstance(product_spec_json, dict) else None,
        exit_code=product_spec_rc,
    )
    ps_applicability = gate.get("applicability")
    ps_decision = gate.get("decision")

    if gate.get("routing_action") == "refresh_contract_snapshot":
        result["errors"].append(
            f"product_spec_check_invalid_output: {gate.get('reason', 'unknown')}"
        )
        result["status"] = "runtime_error"
        return result

    # Preserve the validated evaluator payload for consumers that need to
    # distinguish a legacy scalar summary from a schema-valid Product Spec
    # decision bound to this review run.
    result["checks"]["product_spec_check"] = product_spec_json

    if ps_applicability == "applicable":
        if ps_decision == "fail":
            result["checks"]["product_spec"] = "fail"
            result["status"] = "blocked"
            result["source"] = "product_spec_check"
            result["errors"].append(
                f"product_spec_check_fail: {json.dumps(product_spec_json.get('blocked_reasons', []))}"
            )
            return result
        elif ps_decision == "human_judgment":
            result["checks"]["product_spec"] = "human_judgment"
            result["status"] = "human_judgment"
            result["source"] = "product_spec_check"
            return result
        else:
            result["checks"]["product_spec"] = "pass"
    else:
        # not_applicable → treat as pass
        result["checks"]["product_spec"] = "pass"

    # Step 5: baseline_vc_preflight.py (run in all modes)
    vc_command = [
            sys.executable,
            str(_BASELINE_VC_PREFLIGHT_PY),
            "--issue",
            str(issue_number),
            "--repo",
            repo,
            "--timeout-seconds",
            str(_VC_PREFLIGHT_PER_COMMAND_TIMEOUT),
            "--max-workers",
            str(_VC_PREFLIGHT_MAX_WORKERS),
    ]
    if evidence_mode == "current-head":
        if not cwd or not reviewed_head_sha:
            result["status"] = "blocked"
            result["source"] = "vc_preflight"
            result["checks"]["vc_preflight"] = "blocked"
            result["errors"].append("current_head_requires_cwd_and_reviewed_head_sha")
            return result
        vc_command.extend([
            "--cwd", cwd,
            "--evidence-mode", "current-head",
            "--reviewed-head-sha", reviewed_head_sha,
            "--format", "json",
        ])
    vc_result_json, vc_rc, vc_err = _run_script(
        vc_command,
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
    result["current_vc_result"] = vc_result_json
    result["vc_evidence"] = vc_result_json

    if evidence_mode == "current-head":
        envelope_errors = validate_current_head_envelope(vc_result_json, vc_rc)
        if envelope_errors:
            result["checks"]["vc_preflight"] = "blocked"
            result["status"] = "blocked"
            result["source"] = "vc_preflight"
            result["errors"].extend(
                f"uncertified_current_head_vc_evidence:{error}"
                for error in envelope_errors
            )
            return result

    if vc_status == "human_judgment":
        result["checks"]["vc_preflight"] = "human_judgment"
        result["status"] = "human_judgment"
        result["source"] = "vc_preflight"
        return result
    elif vc_status == "blocked":
        result["checks"]["vc_preflight"] = "blocked"
        result["status"] = "blocked"
        result["source"] = "vc_preflight"
        return result
    elif vc_status == "pass":
        result["checks"]["vc_preflight"] = "pass"
        result["status"] = "go"
        result["source"] = "all_checks_pass"
        return result
    else:
        # Unknown vc status
        result["status"] = "runtime_error"
        result["errors"].append(f"unknown_vc_preflight_status: {vc_status}")
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
    parser.add_argument("--evidence-mode", choices=["baseline", "current-head"], default="baseline")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--reviewed-head-sha", default=None)
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
        evidence_mode=args.evidence_mode,
        cwd=args.cwd,
        reviewed_head_sha=args.reviewed_head_sha,
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
