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
- needs.result input uses provenance: needs_result_synthetic (no real head_sha)
- unknown classification is treated conservatively (blocking, gh_error)
- empty input → no_required_evidence (not merge_ready)
"""
from __future__ import annotations

import argparse
import json
import os
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
# "unknown"  = not in any known list; treated conservatively (blocking)

CLASSIFICATION_MAP: dict[tuple[str, str], str] = {
    # ci.yml required jobs
    ("ci", "typecheck"): "required",
    ("ci", "lint"): "required",
    ("ci", "test"): "required",
    ("ci", "build"): "required",
    ("ci", "e2e"): "evidence",
    ("ci", "python-test"): "evidence",
    ("ci", "node-backed-hook-tests"): "evidence",
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

# REQUIRED_CHECKS: (workflow, name) tuples that MUST appear with conclusion=success
# for overall_status to be merge_ready. Absence → no_required_evidence.
REQUIRED_CHECKS: set[tuple[str, str]] = {
    ("ci", "typecheck"),
    ("ci", "lint"),
    ("ci", "test"),
    ("ci", "build"),
    ("ci", "e2e"),
    ("ci", "python-test"),
    ("ci", "node-backed-hook-tests"),
    ("ci", "actionlint"),
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

    B5: unknown classification is treated conservatively — always blocking with gh_error.
    """
    classification = check.get("classification", "unknown")
    status = check.get("status")
    conclusion = check.get("conclusion")
    head_sha = check.get("head_sha")
    head_sha_match = check.get("head_sha_match", False)
    check_run_id = check.get("check_run_id")

    # excluded checks never block
    if classification == "excluded":
        return False, "none"

    # B5: unknown classification is conservatively blocking
    if classification == "unknown":
        return True, "gh_error"

    # Required/evidence rows are only merge-ready evidence when their exact
    # GitHub CheckRun is addressable.  A status/conclusion without an id
    # cannot be rebound to the current PR head during artifact readback.
    if classification in {"required", "evidence"} and (
        not isinstance(check_run_id, int) or isinstance(check_run_id, bool) or check_run_id <= 0
    ):
        return True, "gh_error"

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
                # advisory with head_sha=null: warn but don't block
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
    # REST CheckRun API uses ``id``. The API adapter intentionally preserves
    # it as ``check_run_id`` so the artifact retains current-head provenance.
    check_run_id = raw.get("databaseId") or raw.get("id") or raw.get("check_run_id")
    status = raw.get("status")
    conclusion = raw.get("conclusion")

    # B2: needs_result_synthetic provenance — real head_sha is not available
    # from needs.result, so head_sha is set to None to avoid spoofing.
    provenance = raw.get("provenance", None)
    if provenance == "needs_result_synthetic":
        head_sha = None
        head_sha_match = False
    else:
        head_sha = raw.get("headSha") or raw.get("head_sha")
        head_sha_match = head_sha is not None and head_sha == expected_head_sha

    classification = get_classification(workflow, name)

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
        "artifact_refs": raw.get("artifact_refs", []),
    }

    if provenance is not None:
        entry["provenance"] = provenance

    blocking, failure_reason = determine_check_verdict(entry, expected_head_sha)
    entry["blocking_merge_ready"] = blocking
    entry["failure_reason"] = failure_reason
    return entry


def compute_overall_status(
    checks: list[dict[str, Any]],
    expected_head_sha: str,
    head_sha: str | None,
) -> tuple[str, str]:
    """Returns (overall_status, next_action).

    B3: REQUIRED_CHECKS must all appear with conclusion=success and
    blocking_merge_ready=False. If any required check is missing or
    not successful, return no_required_evidence rather than merge_ready.
    Empty input always returns no_required_evidence.
    """

    if head_sha is not None and head_sha != expected_head_sha:
        return "stale_head_sha", "refresh_head_sha"

    blocking = [c for c in checks if c.get("blocking_merge_ready")]
    if blocking:
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

    # No blocking checks — but verify all REQUIRED_CHECKS are present with success.
    # B3: empty input or missing required check → no_required_evidence.
    passing_keys: set[tuple[str, str]] = set()
    for c in checks:
        if (
            not c.get("blocking_merge_ready")
            and c.get("classification") in {"required", "evidence"}
            and c.get("conclusion") == "success"
        ):
            passing_keys.add((c["workflow"], c["name"]))

    missing = REQUIRED_CHECKS - passing_keys
    if missing:
        return "no_required_evidence", "manual_review_no_required_evidence"

    return "merge_ready", "none"


def generate_verdict(
    expected_head_sha: str,
    pr_head_sha: str | None,
    repository: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
    event_name: str,
    raw_checks: list[dict[str, Any]],
    artifact_id: str | None = None,
    artifact_url: str | None = None,
    artifact_digest: str | None = None,
    artifact_name: str | None = None,
    artifact_workflow_run_id: int | None = None,
    artifact_workflow_run_attempt: int | None = None,
) -> dict[str, Any]:
    """Build the ci_verdict_summary_v2 artifact dict.

    B4: artifact_id/url/digest are stored in artifact_refs when provided.
    """

    checks = []
    for raw in raw_checks:
        workflow = raw.get("workflow", raw.get("workflowName", "unknown"))
        entry = build_check_entry(raw, workflow, expected_head_sha)
        checks.append(entry)

    overall_status, next_action = compute_overall_status(
        checks, expected_head_sha, pr_head_sha
    )

    # B4: populate artifact_refs from upload-artifact outputs
    artifact_refs: list[dict[str, Any]] = []
    if artifact_id or artifact_url or artifact_name:
        ref: dict[str, Any] = {
            "artifact_id": artifact_id,
            "artifact_url": artifact_url,
            "artifact_name": artifact_name,
            "workflow_run_id": artifact_workflow_run_id,
            "workflow_run_attempt": artifact_workflow_run_attempt,
        }
        if artifact_digest:
            ref["artifact_digest"] = artifact_digest
        artifact_refs.append(ref)

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
        "artifact_refs": artifact_refs,
        "checks": checks,
    }
    return artifact


def render_step_summary(artifact: dict[str, Any]) -> str:
    """
    Render GITHUB_STEP_SUMMARY markdown from the artifact.
    Step Summary is derived exclusively from the same JSON (no separate calculation).
    B4: artifact-id/url/digest are shown from artifact_refs.
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
    # B4: show artifact upload metadata from artifact_refs
    for ref in artifact.get("artifact_refs", []):
        lines.append(f"**artifact-id:** {ref.get('artifact_id', 'N/A')}")
        lines.append(f"**artifact-url:** {ref.get('artifact_url', 'N/A')}")
        if "artifact_digest" in ref:
            lines.append(f"**artifact-digest:** {ref['artifact_digest']}")
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
    p.add_argument("--expected-head-sha", default=None)
    p.add_argument("--pr-head-sha", default=None)
    p.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--workflow-run-id", type=int, default=0)
    p.add_argument("--workflow-run-attempt", type=int, default=1)
    p.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", "unknown"))
    # B1: --needs-json accepts JSON dict of {job_name: result} from workflow needs context
    p.add_argument("--needs-json", default=None, help="JSON string or @file with needs.*.result map")
    p.add_argument("--checks-json", default=None, help="Path to JSON file containing check run list")
    p.add_argument(
        "--check-runs-api-json",
        default=None,
        help="GitHub REST commit check-runs response bound to --workflow-run-id",
    )
    p.add_argument("--checks-stdin", action="store_true", help="Read check runs JSON from stdin")
    p.add_argument("--output", default=None, help="Output path for ci_verdict_summary_v2.json")
    p.add_argument("--summary-input", default=None, help="Existing artifact JSON to render without regenerating it")
    p.add_argument("--summary-output", default=None, help="Optional path to write step summary markdown")
    # B4: artifact upload metadata (from actions/upload-artifact outputs)
    p.add_argument("--artifact-id", default=None, help="artifact-id from upload-artifact output")
    p.add_argument("--artifact-url", default=None, help="artifact-url from upload-artifact output")
    p.add_argument("--artifact-digest", default=None, help="artifact-digest from upload-artifact output")
    p.add_argument("--artifact-name", default=None, help="Name of the already-uploaded binding artifact")
    p.add_argument("--artifact-workflow-run-id", type=int, default=None)
    p.add_argument("--artifact-workflow-run-attempt", type=int, default=None)
    return p.parse_args(argv)


def needs_json_to_raw_checks(needs_map: dict[str, str]) -> list[dict[str, Any]]:
    """
    B1/B2: Convert needs.*.result map to raw check list with provenance=needs_result_synthetic.
    head_sha is not set (None) because needs.result does not carry real head SHA.
    head_sha_match is False for all entries (provenance synthetic).
    """
    NEEDS_JOB_WORKFLOW = "ci"

    def result_to_conclusion(result: str) -> str | None:
        mapping = {
            "success": "success",
            "failure": "failure",
            "cancelled": "cancelled",
            "skipped": "skipped",
        }
        return mapping.get(result)

    def result_to_status(result: str) -> str:
        if result in {"success", "failure", "cancelled", "skipped"}:
            return "completed"
        return "unknown"

    raw_checks = []
    for job_name, result in needs_map.items():
        raw_checks.append({
            "name": job_name,
            "workflow": NEEDS_JOB_WORKFLOW,
            "status": result_to_status(result) if result else None,
            "conclusion": result_to_conclusion(result),
            # B2: no real head_sha from needs.result — provenance marks this synthetic
            "headSha": None,
            "provenance": "needs_result_synthetic",
        })
    return raw_checks


def check_runs_api_to_raw_checks(
    payload: Any,
    *,
    workflow_run_id: int,
    workflow: str = "ci",
) -> list[dict[str, Any]]:
    """Normalize only real GitHub Check Runs for this workflow run.

    ``needs.<job>.result`` has no CheckRun id or head SHA and must never be
    used to produce merge-ready evidence. The REST endpoint is commit-scoped;
    each retained row is additionally bound to this Actions run via its URL.
    """
    check_runs = payload.get("check_runs") if isinstance(payload, dict) else payload
    if not isinstance(check_runs, list):
        raise ValueError("check_runs_api_payload_invalid")

    expected_run_fragment = f"/actions/runs/{workflow_run_id}/"
    raw_checks: list[dict[str, Any]] = []
    for row in check_runs:
        if not isinstance(row, dict):
            raise ValueError("check_runs_api_payload_invalid")
        details_url = row.get("details_url") or row.get("detailsUrl")
        if not isinstance(details_url, str) or expected_run_fragment not in details_url:
            continue
        name = row.get("name")
        head_sha = row.get("head_sha") or row.get("headSha")
        check_run_id = row.get("id") or row.get("databaseId")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(head_sha, str)
            or not head_sha
            or check_run_id is None
        ):
            raise ValueError("check_runs_api_row_invalid")
        # This job creates its own CheckRun while generating the artifact;
        # it is not an upstream input and would necessarily be in progress.
        if name == "ci-verdict-summary":
            continue
        raw_checks.append(
            {
                "name": name,
                "workflow": workflow,
                "status": row.get("status"),
                "conclusion": row.get("conclusion"),
                "head_sha": head_sha,
                "check_run_id": check_run_id,
                "details_url": details_url,
                "provenance": "github_check_run_api",
            }
        )
    if not raw_checks:
        raise ValueError("check_runs_api_no_current_workflow_evidence")
    return raw_checks


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.summary_input:
        with open(args.summary_input) as f:
            artifact = json.load(f)
        summary = render_step_summary(artifact)
        if args.summary_output:
            with open(args.summary_output, "w") as f:
                f.write(summary)
            print(f"Written summary: {args.summary_output}", file=sys.stderr)
        elif os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write(summary)
            print("Written to GITHUB_STEP_SUMMARY", file=sys.stderr)
        return 0

    if not args.output:
        print("ERROR: --output is required unless --summary-input is supplied", file=sys.stderr)
        return 1
    if not args.expected_head_sha:
        print("ERROR: --expected-head-sha is required when generating an artifact", file=sys.stderr)
        return 1

    raw_checks: list[dict[str, Any]]

    # B1: --needs-json takes priority — workflow should use this instead of inline Python
    if args.needs_json:
        needs_str = args.needs_json
        if needs_str.startswith("@"):
            with open(needs_str[1:]) as f:
                needs_str = f.read()
        try:
            needs_map = json.loads(needs_str)
        except json.JSONDecodeError as e:
            print(f"ERROR: Failed to parse --needs-json: {e}", file=sys.stderr)
            return 1
        if not isinstance(needs_map, dict):
            print("ERROR: --needs-json must be a JSON object {job_name: result}", file=sys.stderr)
            return 1
        raw_checks = needs_json_to_raw_checks(needs_map)
    elif args.check_runs_api_json:
        try:
            with open(args.check_runs_api_json) as f:
                raw_payload = json.load(f)
            raw_checks = check_runs_api_to_raw_checks(
                raw_payload, workflow_run_id=args.workflow_run_id
            )
        except (OSError, json.JSONDecodeError, ValueError) as e:
            print(f"ERROR: Failed to load real CheckRun API evidence: {e}", file=sys.stderr)
            return 1
    elif args.checks_stdin:
        raw_text = sys.stdin.read()
        try:
            raw_checks = json.loads(raw_text)
        except json.JSONDecodeError as e:
            print(f"ERROR: Failed to parse checks JSON: {e}", file=sys.stderr)
            return 1
        if not isinstance(raw_checks, list):
            raw_checks = [raw_checks]
    elif args.checks_json:
        with open(args.checks_json) as f:
            raw_text = f.read()
        try:
            raw_checks = json.loads(raw_text)
        except json.JSONDecodeError as e:
            print(f"ERROR: Failed to parse checks JSON: {e}", file=sys.stderr)
            return 1
        if not isinstance(raw_checks, list):
            raw_checks = [raw_checks]
    else:
        raw_checks = []

    artifact = generate_verdict(
        expected_head_sha=args.expected_head_sha,
        pr_head_sha=args.pr_head_sha,
        repository=args.repository,
        workflow_run_id=args.workflow_run_id,
        workflow_run_attempt=args.workflow_run_attempt,
        event_name=args.event_name,
        raw_checks=raw_checks,
        artifact_id=args.artifact_id,
        artifact_url=args.artifact_url,
        artifact_digest=args.artifact_digest,
        artifact_name=args.artifact_name,
        artifact_workflow_run_id=args.artifact_workflow_run_id,
        artifact_workflow_run_attempt=args.artifact_workflow_run_attempt,
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
    elif os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(summary)
        print("Written to GITHUB_STEP_SUMMARY", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
