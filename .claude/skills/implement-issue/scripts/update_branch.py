#!/usr/bin/env python3
"""Deterministic update-branch REST wrapper for implementation-worker."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable


UPDATE_METHOD = "merge_only"
REASON_EXPECTED_HEAD_SHA_MISSING = "expected_head_sha_missing"
REASON_EXPECTED_HEAD_SHA_MISMATCH = "expected_head_sha_mismatch"
REASON_PERMISSION_DENIED = "permission_denied"
REASON_SECONDARY_RATE_LIMIT = "secondary_rate_limit"
REASON_VALIDATION_FAILED = "validation_failed"
REASON_HEAD_UNCHANGED = "head_unchanged_after_accepted"
REASON_TRANSPORT_ERROR = "transport_error"
REASON_UNKNOWN_HTTP_STATUS = "unknown_http_status"
RERUN_REASON = "pr_head_changed_by_update_branch"
RATE_LIMIT_MARKERS = (
    "secondary rate limit",
    "rate limit",
    "retry later",
    "abuse detection",
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


@dataclass(frozen=True)
class UpdateBranchRequest:
    pr_number: int
    repo: str
    expected_head_sha: str
    update_method: str = UPDATE_METHOD
    caller: str = "manual"


GhRunner = Callable[[list[str]], CommandResult]
SleepFn = Callable[[float], None]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--caller", default="manual")
    parser.add_argument("--update-method", default=UPDATE_METHOD)
    parser.add_argument("--poll-max", type=int, default=12)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    return parser.parse_args(argv)


def run_gh(args: list[str]) -> CommandResult:
    completed = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _base_result(request: UpdateBranchRequest) -> dict[str, object]:
    return {
        "status": "failed",
        "reason_code": None,
        "update_method": request.update_method,
        "http_status": None,
        "before_head_sha": None,
        "after_head_sha": None,
        "new_head_sha": None,
        "poll_attempts": 0,
        "rerun_required": {
            "verification": False,
            "pr_review": False,
            "reason": None,
        },
        "permission_diagnostics": None,
        "error_body": None,
        "errors": [],
    }


def _json_loads(raw: str) -> object | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _extract_http_status(raw: str) -> tuple[int | None, str]:
    match = re.search(r"^HTTP/\S+\s+(\d{3})", raw, flags=re.MULTILINE)
    status = int(match.group(1)) if match else None
    split = re.split(r"\r?\n\r?\n", raw, maxsplit=1)
    body = split[1] if len(split) == 2 else ""
    return status, body.strip()


def _get_current_head_sha(
    request: UpdateBranchRequest,
    gh_runner: GhRunner,
) -> tuple[str | None, str | None]:
    result = gh_runner(
        [
            "pr",
            "view",
            str(request.pr_number),
            "--repo",
            request.repo,
            "--json",
            "headRefOid",
            "--jq",
            ".headRefOid",
        ]
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "gh pr view failed"
        return None, detail
    head = result.stdout.strip()
    if not head:
        return None, "headRefOid was empty"
    return head, None


def _get_permission_diagnostics(
    request: UpdateBranchRequest,
    gh_runner: GhRunner,
) -> dict[str, object]:
    auth_actor = ""
    auth = gh_runner(["api", "user", "--jq", ".login"])
    if auth.returncode == 0:
        auth_actor = auth.stdout.strip()

    pr_view = gh_runner(
        [
            "pr",
            "view",
            str(request.pr_number),
            "--repo",
            request.repo,
            "--json",
            "headRepository,baseRepository,maintainerCanModify,isCrossRepository",
        ]
    )
    payload = _json_loads(pr_view.stdout) if pr_view.returncode == 0 else None
    head_repo = request.repo
    base_repo = request.repo
    maintainer_can_modify = False
    fork_pr = False
    if isinstance(payload, dict):
        head_repo = (
            payload.get("headRepository", {}) or {}
        ).get("nameWithOwner", request.repo)
        base_repo = (
            payload.get("baseRepository", {}) or {}
        ).get("nameWithOwner", request.repo)
        maintainer_can_modify = bool(payload.get("maintainerCanModify", False))
        fork_pr = bool(payload.get("isCrossRepository", False))

    return {
        "auth_actor": auth_actor,
        "head_repo": head_repo,
        "base_repo": base_repo,
        "fork_pr": fork_pr,
        "maintainer_can_modify": maintainer_can_modify,
        "required_permissions": "pull_requests:write",
    }


def _is_secondary_rate_limit(http_status: int | None, body: str) -> bool:
    if http_status not in {403, 422, 429}:
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def execute_update_branch(
    request: UpdateBranchRequest,
    *,
    gh_runner: GhRunner = run_gh,
    sleep_fn: SleepFn = time.sleep,
    poll_max: int = 12,
    poll_interval: float = 5.0,
) -> dict[str, object]:
    result = _base_result(request)

    if request.update_method != UPDATE_METHOD:
        result["reason_code"] = REASON_VALIDATION_FAILED
        result["error_body"] = (
            f"Unsupported update_method={request.update_method!r}; merge_only only."
        )
        result["errors"].append("update_method must be merge_only")
        return result

    expected_head_sha = request.expected_head_sha.strip()
    if not expected_head_sha:
        result["status"] = "blocked"
        result["reason_code"] = REASON_EXPECTED_HEAD_SHA_MISSING
        result["errors"].append("expected_head_sha is required")
        return result

    current_head_sha, preflight_error = _get_current_head_sha(request, gh_runner)
    if preflight_error:
        result["reason_code"] = REASON_TRANSPORT_ERROR
        result["error_body"] = preflight_error
        result["errors"].append(preflight_error)
        return result

    result["before_head_sha"] = current_head_sha
    if current_head_sha != expected_head_sha:
        result["status"] = "blocked"
        result["reason_code"] = REASON_EXPECTED_HEAD_SHA_MISMATCH
        result["after_head_sha"] = current_head_sha
        result["error_body"] = (
            "current PR head did not match expected_head_sha; API call skipped"
        )
        result["errors"].append("current PR head mismatch")
        return result

    update_response = gh_runner(
        [
            "api",
            "-i",
            "-X",
            "PUT",
            f"repos/{request.repo}/pulls/{request.pr_number}/update-branch",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            "-f",
            f"expected_head_sha={expected_head_sha}",
        ]
    )

    raw_response = update_response.stdout if update_response.stdout else update_response.stderr
    http_status, response_body = _extract_http_status(raw_response)
    result["http_status"] = http_status
    result["error_body"] = response_body or None

    if update_response.returncode != 0 and http_status is None:
        result["reason_code"] = REASON_TRANSPORT_ERROR
        result["errors"].append(update_response.stderr.strip() or "gh api failed")
        return result

    if http_status == 202:
        for attempt in range(1, poll_max + 1):
            result["poll_attempts"] = attempt
            head_sha, poll_error = _get_current_head_sha(request, gh_runner)
            if poll_error:
                result["reason_code"] = REASON_TRANSPORT_ERROR
                result["errors"].append(poll_error)
                return result
            if head_sha and head_sha != expected_head_sha:
                result["status"] = "ok"
                result["after_head_sha"] = head_sha
                result["new_head_sha"] = head_sha
                result["rerun_required"] = {
                    "verification": True,
                    "pr_review": True,
                    "reason": RERUN_REASON,
                }
                return result
            if attempt < poll_max:
                sleep_fn(poll_interval)

        result["reason_code"] = REASON_HEAD_UNCHANGED
        result["after_head_sha"] = expected_head_sha
        result["error_body"] = "head did not change after accepted update-branch request"
        result["errors"].append("head unchanged after accepted")
        return result

    if http_status == 403 and _is_secondary_rate_limit(http_status, response_body):
        result["reason_code"] = REASON_SECONDARY_RATE_LIMIT
        result["errors"].append("secondary rate limit")
        return result

    if http_status == 403:
        result["status"] = "permission_blocked"
        result["reason_code"] = REASON_PERMISSION_DENIED
        result["permission_diagnostics"] = _get_permission_diagnostics(request, gh_runner)
        result["errors"].append("permission denied")
        return result

    if _is_secondary_rate_limit(http_status, response_body):
        result["reason_code"] = REASON_SECONDARY_RATE_LIMIT
        result["errors"].append("secondary rate limit")
        return result

    if http_status == 422:
        lowered = response_body.lower()
        if "expected_head_sha" in lowered or "head sha" in lowered:
            result["status"] = "blocked"
            result["reason_code"] = REASON_EXPECTED_HEAD_SHA_MISMATCH
            result["after_head_sha"] = expected_head_sha
            result["errors"].append("expected_head_sha mismatch")
            return result
        result["reason_code"] = REASON_VALIDATION_FAILED
        result["errors"].append("validation failed")
        return result

    if http_status == 429:
        result["reason_code"] = REASON_SECONDARY_RATE_LIMIT
        result["errors"].append("secondary rate limit")
        return result

    result["reason_code"] = REASON_UNKNOWN_HTTP_STATUS
    result["errors"].append(f"unexpected HTTP status: {http_status}")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    request = UpdateBranchRequest(
        pr_number=args.pr_number,
        repo=args.repo,
        expected_head_sha=args.expected_head_sha,
        update_method=args.update_method,
        caller=args.caller,
    )
    result = execute_update_branch(
        request,
        poll_max=args.poll_max,
        poll_interval=args.poll_interval,
    )
    json.dump(result, sys.stdout, ensure_ascii=True, indent=2)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
