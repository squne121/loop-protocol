#!/usr/bin/env python3
"""Wait for required PR checks and emit PR_CHECKS_WAIT_RESULT_V1 JSON."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any

SCHEMA_NAME = "PR_CHECKS_WAIT_RESULT_V1"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_RUNTIME = 2

PENDING_BUCKETS = {"pending", None}
PENDING_STATES = {"queued", "in_progress", "waiting", "requested", "pending"}
FAIL_BUCKETS = {"fail", "cancel", "skipping"}
DEFAULT_NON_BLOCKING_RULES = frozenset({("deploy-pages", "deploy-pr")})


def run_gh(args: list[str]) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=60,
        )
    except FileNotFoundError:
        return 127, "", "gh not found"
    except subprocess.TimeoutExpired:
        return 124, "", "gh command timed out"
    except Exception as err:  # pragma: no cover
        return 1, "", str(err)
    return result.returncode, result.stdout.strip(), (result.stderr or "").strip()


def classify_gh_error(stderr: str) -> str:
    lowered = stderr.lower()
    if not lowered:
        return "gh_error"
    if any(token in lowered for token in ("auth", "token", "credential", "permission", "forbidden", "unauthorized")):
        return "auth_error"
    if "rate limit" in lowered or "429" in lowered:
        return "rate_limited"
    if "not found" in lowered or "404" in lowered:
        return "not_found"
    if "timed out" in lowered:
        return "gh_timeout"
    return "gh_error"


def emit_result(
    *,
    repo: str,
    pr_number: int,
    expected_head_sha: str,
    current_head_sha: str,
    decision: str,
    checks: list[dict[str, Any]],
    pending_count: int,
    failed_blocking_count: int,
    timed_out: bool,
    elapsed_seconds: int,
    interval_seconds: int,
    timeout_seconds: int,
    error_code: str | None,
    message: str | None,
    exit_code: int,
) -> int:
    payload = {
        "schema": SCHEMA_NAME,
        "repo": repo,
        "pr": pr_number,
        "expected_head_sha": expected_head_sha,
        "current_head_sha": current_head_sha,
        "decision": decision,
        "checks": checks,
        "pending_count": pending_count,
        "failed_blocking_count": failed_blocking_count,
        "timed_out": timed_out,
        "elapsed_seconds": elapsed_seconds,
        "interval_seconds": interval_seconds,
        "timeout_seconds": timeout_seconds,
        "error_code": error_code,
        "message": message,
    }
    print(json.dumps(payload, ensure_ascii=True))
    return exit_code


def get_pr_head(repo: str, pr_number: int) -> tuple[str | None, str | None, str | None]:
    rc, stdout, stderr = run_gh(
        ["pr", "view", str(pr_number), "--repo", repo, "--json", "headRefOid", "--jq", ".headRefOid"]
    )
    if rc != 0:
        return None, classify_gh_error(stderr or stdout), stderr or stdout or "gh pr view failed"
    head_sha = stdout.strip()
    if not head_sha:
        return None, "malformed_output", "headRefOid is empty"
    return head_sha, None, None


def get_required_checks(repo: str, pr_number: int) -> tuple[list[dict[str, Any]] | None, str | None, str | None]:
    rc, stdout, stderr = run_gh(
        [
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            repo,
            "--required",
            "--json",
            "name,bucket,state,workflow,link,startedAt,completedAt",
        ]
    )
    if rc != 0:
        return None, classify_gh_error(stderr or stdout), stderr or stdout or "gh pr checks failed"
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None, "malformed_output", stdout[:500]
    if not isinstance(data, list):
        return None, "malformed_output", f"expected list, got {type(data).__name__}"
    return data, None, None


def normalize_checks(
    checks: list[dict[str, Any]],
    non_blocking_rules: frozenset[tuple[str | None, str | None]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for check in checks:
        workflow = check.get("workflow")
        name = check.get("name")
        bucket = check.get("bucket")
        state = check.get("state")
        non_blocking = (workflow, name) in non_blocking_rules
        normalized.append(
            {
                "name": name,
                "workflow": workflow,
                "bucket": bucket,
                "state": state,
                "link": check.get("link"),
                "startedAt": check.get("startedAt"),
                "completedAt": check.get("completedAt"),
                "blocking": not non_blocking,
                "non_blocking_reason": "configured_exact_match" if non_blocking else None,
            }
        )
    return normalized


def summarize_checks(checks: list[dict[str, Any]]) -> tuple[int, int]:
    pending_count = 0
    failed_blocking_count = 0
    for check in checks:
        bucket = check.get("bucket")
        state = (check.get("state") or "").lower()
        blocking = bool(check.get("blocking", True))
        if bucket in PENDING_BUCKETS or state in PENDING_STATES:
            pending_count += 1
            continue
        if blocking and bucket in FAIL_BUCKETS:
            failed_blocking_count += 1
    return pending_count, failed_blocking_count


def decide_status(checks: list[dict[str, Any]]) -> tuple[str, str, int, int]:
    pending_count, failed_blocking_count = summarize_checks(checks)
    if not checks:
        return "no_required_evidence", "required checks are not available", pending_count, failed_blocking_count
    if pending_count > 0:
        return "pending", "required checks are still pending", pending_count, failed_blocking_count
    if failed_blocking_count > 0:
        return "failed_blocking", "blocking required checks failed", pending_count, failed_blocking_count
    return "pass", "all blocking required checks passed", pending_count, failed_blocking_count


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wait for required PR checks and emit PR_CHECKS_WAIT_RESULT_V1 JSON.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int, dest="pr_number")
    parser.add_argument("--expected-head-sha", required=True, dest="expected_head_sha")
    parser.add_argument("--interval-seconds", "--interval", type=int, default=15, dest="interval_seconds")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args(argv)
    if args.interval_seconds <= 0:
        raise ValueError("--interval-seconds must be a positive integer")
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be a positive integer")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv or sys.argv[1:])
    except (SystemExit, ValueError):
        return emit_result(
            repo="",
            pr_number=0,
            expected_head_sha="",
            current_head_sha="",
            decision="invalid_args",
            checks=[],
            pending_count=0,
            failed_blocking_count=0,
            timed_out=False,
            elapsed_seconds=0,
            interval_seconds=0,
            timeout_seconds=0,
            error_code="invalid_args",
            message="invalid arguments",
            exit_code=EXIT_RUNTIME,
        )

    start_time = time.monotonic()
    latest_checks: list[dict[str, Any]] = []

    current_head_sha, head_error, head_message = get_pr_head(args.repo, args.pr_number)
    if head_error:
        return emit_result(
            repo=args.repo,
            pr_number=args.pr_number,
            expected_head_sha=args.expected_head_sha,
            current_head_sha=current_head_sha or "",
            decision="gh_error",
            checks=[],
            pending_count=0,
            failed_blocking_count=0,
            timed_out=False,
            elapsed_seconds=0,
            interval_seconds=args.interval_seconds,
            timeout_seconds=args.timeout_seconds,
            error_code=head_error,
            message=head_message,
            exit_code=EXIT_RUNTIME,
        )
    if current_head_sha != args.expected_head_sha:
        return emit_result(
            repo=args.repo,
            pr_number=args.pr_number,
            expected_head_sha=args.expected_head_sha,
            current_head_sha=current_head_sha or "",
            decision="stale_head_sha",
            checks=[],
            pending_count=0,
            failed_blocking_count=0,
            timed_out=False,
            elapsed_seconds=0,
            interval_seconds=args.interval_seconds,
            timeout_seconds=args.timeout_seconds,
            error_code="stale_head_sha",
            message="head SHA changed before waiting",
            exit_code=EXIT_FAIL,
        )

    while True:
        elapsed_seconds = int(time.monotonic() - start_time)
        if elapsed_seconds >= args.timeout_seconds:
            pending_count, failed_blocking_count = summarize_checks(latest_checks)
            return emit_result(
                repo=args.repo,
                pr_number=args.pr_number,
                expected_head_sha=args.expected_head_sha,
                current_head_sha=current_head_sha or "",
                decision="human_judgment",
                checks=latest_checks,
                pending_count=pending_count,
                failed_blocking_count=failed_blocking_count,
                timed_out=True,
                elapsed_seconds=elapsed_seconds,
                interval_seconds=args.interval_seconds,
                timeout_seconds=args.timeout_seconds,
                error_code="pending_timeout",
                message="timeout waiting for required checks",
                exit_code=EXIT_FAIL,
            )

        current_head_sha, head_error, head_message = get_pr_head(args.repo, args.pr_number)
        if head_error:
            pending_count, failed_blocking_count = summarize_checks(latest_checks)
            return emit_result(
                repo=args.repo,
                pr_number=args.pr_number,
                expected_head_sha=args.expected_head_sha,
                current_head_sha="",
                decision="gh_error",
                checks=latest_checks,
                pending_count=pending_count,
                failed_blocking_count=failed_blocking_count,
                timed_out=False,
                elapsed_seconds=elapsed_seconds,
                interval_seconds=args.interval_seconds,
                timeout_seconds=args.timeout_seconds,
                error_code=head_error,
                message=head_message,
                exit_code=EXIT_RUNTIME,
            )
        if current_head_sha != args.expected_head_sha:
            pending_count, failed_blocking_count = summarize_checks(latest_checks)
            return emit_result(
                repo=args.repo,
                pr_number=args.pr_number,
                expected_head_sha=args.expected_head_sha,
                current_head_sha=current_head_sha or "",
                decision="stale_head_sha",
                checks=latest_checks,
                pending_count=pending_count,
                failed_blocking_count=failed_blocking_count,
                timed_out=False,
                elapsed_seconds=elapsed_seconds,
                interval_seconds=args.interval_seconds,
                timeout_seconds=args.timeout_seconds,
                error_code="stale_head_sha",
                message="head SHA changed while waiting for checks",
                exit_code=EXIT_FAIL,
            )

        checks, checks_error, checks_message = get_required_checks(args.repo, args.pr_number)
        if checks_error or checks is None:
            pending_count, failed_blocking_count = summarize_checks(latest_checks)
            return emit_result(
                repo=args.repo,
                pr_number=args.pr_number,
                expected_head_sha=args.expected_head_sha,
                current_head_sha=current_head_sha or "",
                decision="gh_error",
                checks=latest_checks,
                pending_count=pending_count,
                failed_blocking_count=failed_blocking_count,
                timed_out=False,
                elapsed_seconds=elapsed_seconds,
                interval_seconds=args.interval_seconds,
                timeout_seconds=args.timeout_seconds,
                error_code=checks_error,
                message=checks_message,
                exit_code=EXIT_RUNTIME,
            )

        latest_checks = normalize_checks(checks, DEFAULT_NON_BLOCKING_RULES)
        decision, message, pending_count, failed_blocking_count = decide_status(latest_checks)
        if decision == "pending":
            remaining = args.timeout_seconds - (time.monotonic() - start_time)
            sleep_seconds = min(args.interval_seconds, max(0.0, remaining))
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            continue

        exit_code = EXIT_PASS if decision == "pass" else EXIT_FAIL
        return emit_result(
            repo=args.repo,
            pr_number=args.pr_number,
            expected_head_sha=args.expected_head_sha,
            current_head_sha=current_head_sha,
            decision=decision,
            checks=latest_checks,
            pending_count=pending_count,
            failed_blocking_count=failed_blocking_count,
            timed_out=False,
            elapsed_seconds=elapsed_seconds,
            interval_seconds=args.interval_seconds,
            timeout_seconds=args.timeout_seconds,
            error_code=None if decision == "pass" else decision,
            message=message,
            exit_code=exit_code,
        )


if __name__ == "__main__":
    raise SystemExit(main())
