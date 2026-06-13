#!/usr/bin/env python3
"""CI failed log retrieval helper for impl-review-loop.

Selects the GitHub Actions workflow run matching the requested head SHA,
retrieves failed job logs via gh CLI (primary) or REST API (fallback),
applies ANSI strip / token redaction / truncation, and emits
CI_FAILED_LOG_RESULT_V1 YAML at stdout end.
"""

import argparse
import json
import re
import subprocess
import sys
import textwrap
from typing import Optional

DEFAULT_MAX_BYTES = 60_000

PENDING_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}


def run_gh(*args: str, capture: bool = True) -> tuple[int, str, str]:
    result = subprocess.run(
        ["gh", *args],
        capture_output=capture,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[mGKHF]", "", text)


TOKEN_PATTERN = re.compile(
    r"(?i)(ghp_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{59,}"
    r"|[A-Za-z0-9+/]{40,}={0,2}"
    r"|eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*)"
)


def redact_tokens(text: str) -> bool:
    """Return (redacted_text, was_redacted)."""
    redacted, n = TOKEN_PATTERN.subn("[REDACTED]", text)
    return redacted, n > 0


def truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n...[TRUNCATED]", True


def select_run(runs: list[dict], head_sha: str, workflow_filter: Optional[str]) -> Optional[dict]:
    matching = [r for r in runs if r.get("headSha") == head_sha]
    if workflow_filter:
        matching = [r for r in matching if workflow_filter.lower() in r.get("workflowName", "").lower()]
    if not matching:
        return None
    matching.sort(key=lambda r: (r.get("attempt", 1)), reverse=True)
    return matching[0]


def fetch_logs_primary(repo: str, run_id: int, attempt: int) -> tuple[str, bool]:
    rc, stdout, stderr = run_gh(
        "run", "view", str(run_id),
        "--repo", repo,
        f"--attempt={attempt}",
        "--log-failed",
    )
    if rc == 0 and stdout.strip():
        return stdout, True
    return "", False


def fetch_logs_rest_fallback(repo: str, run_id: int, attempt: int) -> tuple[str, list[str], bool]:
    rc, out, _ = run_gh(
        "api", f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
        "--jq", "[.jobs[] | select(.conclusion==\"failure\") | {id: .id, name: .name}]",
    )
    if rc != 0 or not out.strip():
        return "", [], False

    try:
        jobs = json.loads(out)
    except json.JSONDecodeError:
        return "", [], False

    failed_names = [j["name"] for j in jobs]
    all_logs: list[str] = []
    for job in jobs:
        job_id = job["id"]
        rc2, log_out, _ = run_gh("api", f"/repos/{repo}/actions/jobs/{job_id}/logs")
        if rc2 == 0 and log_out.strip():
            all_logs.append(f"=== {job['name']} ===\n{log_out}")

    combined = "\n".join(all_logs)
    return combined, failed_names, bool(all_logs)


def emit_result(
    *,
    status: str,
    run_id: Optional[int],
    attempt: Optional[int],
    head_sha: str,
    workflow_name: Optional[str],
    failed_jobs: list[str],
    retrieval_method: Optional[str],
    redaction_applied: bool,
    truncated: bool,
    log_text: str,
    exit_code: int,
) -> None:
    if log_text:
        print(log_text)
    marker = textwrap.dedent(f"""\
        CI_FAILED_LOG_RESULT_V1:
          status: {status}
          run_id: {run_id}
          attempt: {attempt}
          head_sha: {head_sha}
          workflow_name: {workflow_name}
          failed_jobs: {json.dumps(failed_jobs)}
          retrieval_method: {retrieval_method}
          redaction_applied: {str(redaction_applied).lower()}
          truncated: {str(truncated).lower()}
    """)
    print(marker)
    sys.exit(exit_code)


def main() -> None:
    parser = argparse.ArgumentParser(description="Retrieve CI failed logs for a PR head SHA.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head-sha", required=True, dest="head_sha")
    parser.add_argument("--workflow", default=None)
    parser.add_argument("--job", default=None)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, dest="max_bytes")
    args = parser.parse_args()

    rc, out, err = run_gh(
        "run", "list",
        "--repo", args.repo,
        "--commit", args.head_sha,
        "--json", "databaseId,attempt,status,conclusion,headSha,workflowName,event,createdAt,updatedAt,url",
        "--limit", "50",
    )

    if rc != 0:
        emit_result(
            status="no_matching_run",
            run_id=None, attempt=None,
            head_sha=args.head_sha,
            workflow_name=None,
            failed_jobs=[],
            retrieval_method=None,
            redaction_applied=False,
            truncated=False,
            log_text=f"gh run list failed: {err.strip()}",
            exit_code=2,
        )

    try:
        runs = json.loads(out)
    except json.JSONDecodeError:
        emit_result(
            status="no_matching_run",
            run_id=None, attempt=None,
            head_sha=args.head_sha,
            workflow_name=None,
            failed_jobs=[],
            retrieval_method=None,
            redaction_applied=False,
            truncated=False,
            log_text=f"Failed to parse gh output: {out[:200]}",
            exit_code=2,
        )

    run = select_run(runs, args.head_sha, args.workflow)
    if run is None:
        emit_result(
            status="no_matching_run",
            run_id=None, attempt=None,
            head_sha=args.head_sha,
            workflow_name=None,
            failed_jobs=[],
            retrieval_method=None,
            redaction_applied=False,
            truncated=False,
            log_text="No run found matching the requested head SHA.",
            exit_code=1,
        )

    run_id: int = run["databaseId"]
    attempt: int = run.get("attempt", 1)
    workflow_name: str = run.get("workflowName", "")
    status_raw: str = run.get("status", "")
    conclusion: str = run.get("conclusion") or ""

    if status_raw in PENDING_STATUSES:
        emit_result(
            status="ci_pending",
            run_id=run_id, attempt=attempt,
            head_sha=args.head_sha,
            workflow_name=workflow_name,
            failed_jobs=[],
            retrieval_method=None,
            redaction_applied=False,
            truncated=False,
            log_text=f"Run {run_id} is {status_raw}; log not yet available.",
            exit_code=0,
        )

    if conclusion == "success":
        emit_result(
            status="ci_passed",
            run_id=run_id, attempt=attempt,
            head_sha=args.head_sha,
            workflow_name=workflow_name,
            failed_jobs=[],
            retrieval_method=None,
            redaction_applied=False,
            truncated=False,
            log_text="",
            exit_code=0,
        )

    log_text, primary_ok = fetch_logs_primary(args.repo, run_id, attempt)
    retrieval_method = "gh_log_failed" if primary_ok else None
    failed_jobs: list[str] = []

    if not primary_ok:
        log_text, failed_jobs, fallback_ok = fetch_logs_rest_fallback(args.repo, run_id, attempt)
        if fallback_ok:
            retrieval_method = "rest_job_logs"
        else:
            emit_result(
                status="log_unavailable",
                run_id=run_id, attempt=attempt,
                head_sha=args.head_sha,
                workflow_name=workflow_name,
                failed_jobs=[],
                retrieval_method="none",
                redaction_applied=False,
                truncated=False,
                log_text="Failed to retrieve logs via both primary and REST fallback.",
                exit_code=1,
            )

    log_text = strip_ansi(log_text)
    log_text, redaction_applied = redact_tokens(log_text)
    log_text, truncated = truncate(log_text, args.max_bytes)

    emit_result(
        status="ci_failed",
        run_id=run_id, attempt=attempt,
        head_sha=args.head_sha,
        workflow_name=workflow_name,
        failed_jobs=failed_jobs,
        retrieval_method=retrieval_method,
        redaction_applied=redaction_applied,
        truncated=truncated,
        log_text=log_text,
        exit_code=1,
    )


if __name__ == "__main__":
    main()
