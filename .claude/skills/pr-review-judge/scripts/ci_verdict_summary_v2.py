#!/usr/bin/env python3
"""
ci_verdict_summary_v2.py

CI verdict artifact generator for schema version 2.
Generates ci_verdict_summary_v2.json from GitHub check run data.

Consumer canonical owner: pr-review-judge
impl-review-loop MUST NOT parse this artifact directly.

Design constraints:
- skipped / neutral are NOT pass for required evidence
- head_sha=null is NOT counted as required PR-head evidence
- check classification uses (workflow, check name) tuple
- Step Summary is generated from the same JSON (no separate calculation)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

SCHEMA = "ci_verdict_summary_v2"
SCHEMA_VERSION = 2

# Classification map: (workflow, check_name) -> classification
# "required" = must pass at expected head SHA for merge-ready
# "advisory" = informational, does not block merge-ready
# "evidence" = artifact-producing gate; classification == required + artifact
# "excluded" = allowlisted retrospective/conditional checks (head_sha=null+skipped OK)
# "unknown"  = not in any known list; treated conservatively

CLASSIFICATION_MAP: dict[tuple[str, str], str] = {
    # ci.yml required jobs
    ("ci", "typecheck"): "required",
    ("ci", "lint"): "required",
    ("ci", "test"): "required",
    ("ci", "build"): "required",
    ("ci", "e2e"): "evidence",
    ("ci", "python-test"): "evidence",
    ("ci", "actionlint"): "required",
    # ci-verdict-summary aggregator (evidence producer)
    ("ci", "ci-verdict-summary"): "evidence",
    # Check Japanese Content workflow
    ("Check Japanese Content", "PR Body Japanese Check"): "required",
    # Retrospective / conditional — allowlisted excluded
    ("Check Japanese Content", "PR Review Japanese Check (retrospective)"): "excluded",
    ("Check Japanese Content", "Issue Comment Japanese Check (retrospective)"): "excluded",
    ("Check Japanese Content", "Issue Body Japanese Check (retrospective)"): "excluded",
}

OVERALL_STATUS_ENUM = [
    "merge_ready",
    "blocked",
    "pending",
    "stale_head_sha",
    "gh_error",
    "no_required_evidence",
]

NEXT_ACTION_ENUM = [
    "none",
    "wait_for_ci",
    "inspect_failed_log_artifacts",
    "refresh_head_sha",
    "rerun_failed_check",
    "manual_review_gh_error",
    "manual_review_no_required_evidence",
]

FAILURE_REASON_ENUM = [
    "none",
    "failed",
    "pending",
    "cancelled_current_head",
    "stale_head_sha",
    "skipped_required",
    "neutral_required",
    "missing_required_artifact",
    "gh_error",
    "no_required_evidence",
]


def get_classification(workflow: str, check_name: str) -> str:
    return CLASSIFICATION_MAP.get((workflow, check_name), "unknown")


def is_pending_status(status: str | None) -> bool:
    return status in {"queued", "in_progress", "waiting", "requested", "pending"}


def determine_check_verdict(
    check: dict[str, Any],
    expected_head_sha: str,
) -> tuple[bool, str]:
    """
    Returns (blocking, failure_reason).
    blocking=True means this check blocks merge-ready.
    """
    classification = check.get("classification", "unknown")
    status = check.get("status")
    conclusion = check.get("conclusion")
    head_sha = check.get("head_sha")
    head_sha_match = check.get("head_sha_match", False)

    # excluded checks never block
    if classification == "excluded":
        return False, "none"

    # pending / in-progress status → wait
    if is_pending_status(status):
        return True, "pending"

    if status == "completed":
        # head_sha mismatch → stale
        if head_sha is not None and not head_sha_match:
            return True, "stale_head_sha"

        # head_sha=null: not counted as required evidence
        if head_sha is None:
            if classification in {"required", "evidence"}:
                # null head means we cannot confirm this is for expected head
                # treat as not-yet-conclusive pending unless we have a conclusion
                if conclusion == "skipped":
                    # allowlist check: only excluded classification is OK
                    # required/evidence with head_sha=null + skipped is not pass
                    return True, "skipped_required"
                elif conclusion == "success":
                    # success with head_sha=null is suspicious — block
                    return True, "stale_head_sha"
                elif conclusion in {"failure", "timed_out", "action_required"}:
                    return True, "failed"
                elif conclusion == "cancelled":
                    return True, "cancelled_current_head"
                elif conclusion == "neutral":
                    return True, "neutral_required"
                else:
                    return True, "gh_error"
            else:
                # advisory/unknown with head_sha=null: warn but don't block
                return False, "none"

        # head_sha matches expected
        if head_sha_match:
            if conclusion == "success":
                return False, "none"
            elif conclusion == "skipped":
                if classification in {"required", "evidence"}:
                    return True, "skipped_required"
                return False, "none"
            elif conclusion == "neutral":
                if classification in {"required", "evidence"}:
                    return True, "neutral_required"
                return False, "none"
            elif conclusion in {"failure", "timed_out", "action_required"}:
                return True, "failed"
            elif conclusion == "cancelled":
                return True, "cancelled_current_head"
            elif conclusion == "stale":
                return True, "stale_head_sha"
            else:
                return True, "gh_error"

    # unknown status
    return True, "gh_error"


def build_check_entry(
    raw: dict[str, Any],
    workflow: str,
    expected_head_sha: str,
) -> dict[str, Any]:
    name = raw.get("name", "")
    check_run_id = raw.get("databaseId") or raw.get("id")
    status = raw.get("status")
    conclusion = raw.get("conclusion")
    head_sha = raw.get("headSha") or raw.get("head_sha")

    classification = get_classification(workflow, name)
    head_sha_match = (
        head_sha is not None and head_sha == expected_head_sha
    )

    entry: dict[str, Any] = {
        "name": name,
        "workflow": workflow,
        "check_run_id": check_run_id,
        "status": status,
        "conclusion": conclusion,
        "classification": classification,
        "head_sha": head_sha,
        "expected_head_sha": expected_head_sha,
        "head_sha_match": head_sha_match,
        "blocking_merge_ready": False,  # filled below
        "failure_reason": "none",  # filled below
        "artifact_refs": [],
    }

    blocking, failure_reason = determine_check_verdict(entry, expected_head_sha)
    entry["blocking_merge_ready"] = blocking
    entry["failure_reason"] = failure_reason
    return entry


def compute_overall_status(
    checks: list[dict[str, Any]],
    expected_head_sha: str,
    head_sha: str | None,
) -> tuple[str, str]:
    """Returns (overall_status, next_action)."""

    if head_sha is not None and head_sha != expected_head_sha:
        return "stale_head_sha", "refresh_head_sha"

    blocking = [c for c in checks if c.get("blocking_merge_ready")]
    if not blocking:
        return "merge_ready", "none"

    # Determine most severe next_action
    reasons = {c.get("failure_reason") for c in blocking}

    if "pending" in reasons:
        return "pending", "wait_for_ci"

    if "stale_head_sha" in reasons:
        return "stale_head_sha", "refresh_head_sha"

    if "gh_error" in reasons:
        return "gh_error", "manual_review_gh_error"

    if any(r in reasons for r in {"failed", "cancelled_current_head", "skipped_required", "neutral_required"}):
        return "blocked", "inspect_failed_log_artifacts"

    return "blocked", "inspect_failed_log_artifacts"


def generate_verdict(
    expected_head_sha: str,
    pr_head_sha: str | None,
    repository: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
    event_name: str,
    raw_checks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the ci_verdict_summary_v2 artifact dict."""

    checks = []
    for raw in raw_checks:
        workflow = raw.get("workflow", raw.get("workflowName", "unknown"))
        entry = build_check_entry(raw, workflow, expected_head_sha)
        checks.append(entry)

    overall_status, next_action = compute_overall_status(
        checks, expected_head_sha, pr_head_sha
    )

    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository": repository,
        "workflow_run_id": workflow_run_id,
        "workflow_run_attempt": workflow_run_attempt,
        "event_name": event_name,
        "expected_head_sha": expected_head_sha,
        "head_sha": pr_head_sha,
        "overall_status": overall_status,
        "next_action": next_action,
        "checks": checks,
    }
    return artifact


def render_step_summary(artifact: dict[str, Any]) -> str:
    """
    Render GITHUB_STEP_SUMMARY markdown from the artifact.
    Step Summary is derived exclusively from the same JSON (no separate calculation).
    """
    lines = []
    lines.append("## CI Verdict Summary V2")
    lines.append("")
    lines.append(f"**schema:** `{artifact['schema']}` v{artifact['schema_version']}")
    lines.append(f"**overall_status:** `{artifact['overall_status']}`")
    lines.append(f"**next_action:** `{artifact['next_action']}`")
    lines.append(f"**expected_head_sha:** `{artifact['expected_head_sha']}`")
    head_sha = artifact.get("head_sha")
    lines.append(f"**head_sha:** `{head_sha}`")
    lines.append(f"**generated_at:** {artifact['generated_at']}")
    lines.append("")

    blockers = [c for c in artifact.get("checks", []) if c.get("blocking_merge_ready")]
    if blockers:
        lines.append("### Blockers")
        lines.append("")
        lines.append("| check | workflow | classification | status | conclusion | failure_reason |")
        lines.append("|---|---|---|---|---|---|")
        for c in blockers:
            lines.append(
                f"| {c['name']} | {c['workflow']} | {c['classification']} "
                f"| {c.get('status','')} | {c.get('conclusion','')} | {c.get('failure_reason','')} |"
            )
        lines.append("")
    else:
        lines.append("No blockers.")
        lines.append("")

    lines.append("### All Checks")
    lines.append("")
    lines.append("| check | workflow | classification | status | conclusion | head_sha_match | blocking |")
    lines.append("|---|---|---|---|---|---|---|")
    for c in artifact.get("checks", []):
        match_str = "yes" if c.get("head_sha_match") else "no"
        blocking_str = "yes" if c.get("blocking_merge_ready") else "no"
        lines.append(
            f"| {c['name']} | {c['workflow']} | {c['classification']} "
            f"| {c.get('status','')} | {c.get('conclusion','')} | {match_str} | {blocking_str} |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate ci_verdict_summary_v2 artifact")
    p.add_argument("--expected-head-sha", required=True)
    p.add_argument("--pr-head-sha", default=None)
    p.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--workflow-run-id", type=int, default=0)
    p.add_argument("--workflow-run-attempt", type=int, default=1)
    p.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", "unknown"))
    p.add_argument("--checks-json", default=None, help="Path to JSON file containing check run list")
    p.add_argument("--checks-stdin", action="store_true", help="Read check runs JSON from stdin")
    p.add_argument("--output", required=True, help="Output path for ci_verdict_summary_v2.json")
    p.add_argument("--summary-output", default=None, help="Optional path to write step summary markdown")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.checks_stdin:
        raw_text = sys.stdin.read()
    elif args.checks_json:
        with open(args.checks_json) as f:
            raw_text = f.read()
    else:
        raw_text = "[]"

    try:
        raw_checks = json.loads(raw_text)
    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to parse checks JSON: {e}", file=sys.stderr)
        return 1

    if not isinstance(raw_checks, list):
        raw_checks = [raw_checks]

    artifact = generate_verdict(
        expected_head_sha=args.expected_head_sha,
        pr_head_sha=args.pr_head_sha,
        repository=args.repository,
        workflow_run_id=args.workflow_run_id,
        workflow_run_attempt=args.workflow_run_attempt,
        event_name=args.event_name,
        raw_checks=raw_checks,
    )

    output_path = args.output
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(artifact, f, indent=2)
    print(f"Written: {output_path}", file=sys.stderr)

    summary = render_step_summary(artifact)
    if args.summary_output:
        with open(args.summary_output, "w") as f:
            f.write(summary)
        print(f"Written summary: {args.summary_output}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
