#!/usr/bin/env python3
"""Wait for required CI checks for an impl-review-loop PR head SHA."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any

GH_ENV = {"GH_PROMPT_DISABLED": "1"}

EXIT_PASS = 0
EXIT_NEGATIVE = 1
EXIT_RUNTIME = 2


def run_gh(args: list[str]) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.update(GH_ENV)
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        return 127, "", "gh not found"
    return result.returncode, result.stdout, result.stderr


def classify_gh_error(stderr: str, rc: int) -> str:
    lowered = stderr.lower()
    if rc == 127:
        return "gh_error"
    if any(token in lowered for token in ("authenticat", "bad credentials", "not logged in")):
        return "auth_error"
    if any(token in lowered for token in ("forbidden", "resource not accessible", "not authorized", "permission")):
        return "auth_error"
    return "gh_error"


def emit_result(
    *,
    status: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    current_head_sha: str,
    checks: list[dict[str, Any]],
    elapsed_seconds: int,
    interval_seconds: int,
    timeout_seconds: int,
    error_code: str | None,
    message: str | None,
    exit_code: int,
) -> int:
    payload = {
        "schema": "CI_WAIT_RESULT_V1",
        "status": status,
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "current_head_sha": current_head_sha,
        "required_only": True,
        "checks": checks,
        "elapsed_seconds": elapsed_seconds,
        "interval_seconds": interval_seconds,
        "timeout_seconds": timeout_seconds,
        "error_code": error_code,
        "message": message,
    }
    print(f"CI_WAIT_RESULT_V1_JSON={json.dumps(payload, ensure_ascii=True)}")
    return exit_code


def get_current_head_sha(repo: str, pr_number: int) -> tuple[str | None, str | None, str | None]:
    rc, stdout, stderr = run_gh(
        ["pr", "view", str(pr_number), "--repo", repo, "--json", "headRefOid", "--jq", ".headRefOid"]
    )
    if rc != 0:
        return None, classify_gh_error(stderr, rc), stderr.strip() or stdout.strip()
    head_sha = stdout.strip()
    if not head_sha:
        return None, "malformed_gh_response", "empty headRefOid"
    return head_sha, None, None


def fetch_checks(repo: str, pr_number: int) -> tuple[list[dict[str, Any]] | None, str | None, str | None]:
    fields = "name,bucket,state,workflow,link,startedAt,completedAt"
    rc, stdout, stderr = run_gh(
        ["pr", "checks", str(pr_number), "--repo", repo, "--required", "--json", fields]
    )
    if rc != 0:
        return None, classify_gh_error(stderr, rc), stderr.strip() or stdout.strip()
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None, "malformed_gh_response", stdout[:400]
    if not isinstance(data, list):
        return None, "malformed_gh_response", "gh pr checks did not return a list"
    return data, None, None


def decide_status(checks: list[dict[str, Any]]) -> tuple[str, str]:
    buckets = [check.get("bucket") for check in checks]
    if any(bucket == "pending" or bucket is None for bucket in buckets):
        return "pending", "required checks still pending"
    if any(bucket == "fail" for bucket in buckets):
        return "failed", "required checks failed"
    if any(bucket == "cancel" for bucket in buckets):
        return "cancelled", "required checks were cancelled"
    if any(bucket == "skipping" for bucket in buckets):
        if all(bucket == "skipping" for bucket in buckets):
            return "skipped_only", "all required checks are skipped"
        return "failed", "required checks include skipped entries"
    return "passed", "all required checks passed"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wait for required PR checks to complete.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int, dest="pr_number")
    parser.add_argument("--head-sha", required=True, dest="head_sha")
    parser.add_argument("--required", action="store_true")
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--timeout-seconds", type=int, default=1800, dest="timeout_seconds")
    args = parser.parse_args(argv)
    if not args.required:
        parser.error("--required is mandatory for wait_ci_checks.sh")
    if args.interval <= 0 or args.timeout_seconds <= 0:
        parser.error("--interval and --timeout-seconds must be positive integers")
    return args


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_RUNTIME
        return emit_result(
            status="gh_error",
            repo="",
            pr_number=0,
            head_sha="",
            current_head_sha="",
            checks=[],
            elapsed_seconds=0,
            interval_seconds=0,
            timeout_seconds=0,
            error_code="invalid_args",
            message="invalid arguments",
            exit_code=EXIT_RUNTIME if code != 0 else EXIT_PASS,
        )

    if shutil_which("gh") is None:
        return emit_result(
            status="gh_error",
            repo=args.repo,
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            current_head_sha=args.head_sha,
            checks=[],
            elapsed_seconds=0,
            interval_seconds=args.interval,
            timeout_seconds=args.timeout_seconds,
            error_code="gh_error",
            message="gh CLI not found",
            exit_code=EXIT_RUNTIME,
        )

    start_ts = time.time()

    current_head_sha, head_error, head_message = get_current_head_sha(args.repo, args.pr_number)
    if head_error:
        return emit_result(
            status=head_error,
            repo=args.repo,
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            current_head_sha="",
            checks=[],
            elapsed_seconds=0,
            interval_seconds=args.interval,
            timeout_seconds=args.timeout_seconds,
            error_code=head_error,
            message=head_message,
            exit_code=EXIT_RUNTIME,
        )
    if current_head_sha != args.head_sha:
        return emit_result(
            status="head_sha_changed",
            repo=args.repo,
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            current_head_sha=current_head_sha or "",
            checks=[],
            elapsed_seconds=0,
            interval_seconds=args.interval,
            timeout_seconds=args.timeout_seconds,
            error_code="head_sha_changed",
            message="head SHA changed before wait",
            exit_code=EXIT_NEGATIVE,
        )

    while True:
        elapsed = int(time.time() - start_ts)
        if elapsed >= args.timeout_seconds:
            current_head_sha, head_error, head_message = get_current_head_sha(args.repo, args.pr_number)
            if head_error:
                return emit_result(
                    status=head_error,
                    repo=args.repo,
                    pr_number=args.pr_number,
                    head_sha=args.head_sha,
                    current_head_sha="",
                    checks=[],
                    elapsed_seconds=elapsed,
                    interval_seconds=args.interval,
                    timeout_seconds=args.timeout_seconds,
                    error_code=head_error,
                    message=head_message,
                    exit_code=EXIT_RUNTIME,
                )
            return emit_result(
                status="pending_timeout",
                repo=args.repo,
                pr_number=args.pr_number,
                head_sha=args.head_sha,
                current_head_sha=current_head_sha or "",
                checks=[],
                elapsed_seconds=elapsed,
                interval_seconds=args.interval,
                timeout_seconds=args.timeout_seconds,
                error_code="pending_timeout",
                message="timeout waiting for required checks",
                exit_code=EXIT_NEGATIVE,
            )

        checks, checks_error, checks_message = fetch_checks(args.repo, args.pr_number)
        if checks_error:
            current_head_sha, head_error, head_message = get_current_head_sha(args.repo, args.pr_number)
            if head_error:
                return emit_result(
                    status=head_error,
                    repo=args.repo,
                    pr_number=args.pr_number,
                    head_sha=args.head_sha,
                    current_head_sha="",
                    checks=[],
                    elapsed_seconds=elapsed,
                    interval_seconds=args.interval,
                    timeout_seconds=args.timeout_seconds,
                    error_code=head_error,
                    message=head_message,
                    exit_code=EXIT_RUNTIME,
                )
            return emit_result(
                status=checks_error,
                repo=args.repo,
                pr_number=args.pr_number,
                head_sha=args.head_sha,
                current_head_sha=current_head_sha or "",
                checks=[],
                elapsed_seconds=elapsed,
                interval_seconds=args.interval,
                timeout_seconds=args.timeout_seconds,
                error_code=checks_error,
                message=checks_message,
                exit_code=EXIT_RUNTIME,
            )

        if not checks:
            current_head_sha, head_error, head_message = get_current_head_sha(args.repo, args.pr_number)
            if head_error:
                return emit_result(
                    status=head_error,
                    repo=args.repo,
                    pr_number=args.pr_number,
                    head_sha=args.head_sha,
                    current_head_sha="",
                    checks=[],
                    elapsed_seconds=elapsed,
                    interval_seconds=args.interval,
                    timeout_seconds=args.timeout_seconds,
                    error_code=head_error,
                    message=head_message,
                    exit_code=EXIT_RUNTIME,
                )
            return emit_result(
                status="no_checks",
                repo=args.repo,
                pr_number=args.pr_number,
                head_sha=args.head_sha,
                current_head_sha=current_head_sha or "",
                checks=[],
                elapsed_seconds=elapsed,
                interval_seconds=args.interval,
                timeout_seconds=args.timeout_seconds,
                error_code="no_checks",
                message="required checks are not available",
                exit_code=EXIT_NEGATIVE,
            )

        decision, message = decide_status(checks)
        if decision == "pending":
            time.sleep(args.interval)
            continue

        current_head_sha, head_error, head_message = get_current_head_sha(args.repo, args.pr_number)
        if head_error:
            return emit_result(
                status=head_error,
                repo=args.repo,
                pr_number=args.pr_number,
                head_sha=args.head_sha,
                current_head_sha="",
                checks=checks,
                elapsed_seconds=elapsed,
                interval_seconds=args.interval,
                timeout_seconds=args.timeout_seconds,
                error_code=head_error,
                message=head_message,
                exit_code=EXIT_RUNTIME,
            )
        if current_head_sha != args.head_sha:
            return emit_result(
                status="head_sha_changed",
                repo=args.repo,
                pr_number=args.pr_number,
                head_sha=args.head_sha,
                current_head_sha=current_head_sha or "",
                checks=checks,
                elapsed_seconds=elapsed,
                interval_seconds=args.interval,
                timeout_seconds=args.timeout_seconds,
                error_code="head_sha_changed",
                message="head SHA changed while waiting for checks",
                exit_code=EXIT_NEGATIVE,
            )

        exit_code = EXIT_PASS if decision == "passed" else EXIT_NEGATIVE
        error_code = None if decision == "passed" else decision
        return emit_result(
            status=decision,
            repo=args.repo,
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            current_head_sha=current_head_sha or "",
            checks=checks,
            elapsed_seconds=elapsed,
            interval_seconds=args.interval,
            timeout_seconds=args.timeout_seconds,
            error_code=error_code,
            message=message,
            exit_code=exit_code,
        )


def shutil_which(name: str) -> str | None:
    from shutil import which
    return which(name)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
