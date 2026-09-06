#!/usr/bin/env python3
"""finalize_no_diff_issue.py

Issue #1116: a small, re-runnable finalizer that consumes a root-supplied
no-diff / superseded termination decision and executes it against GitHub
(evidence comment + close, for the primary target and any explicit
supersedes targets), then reads the result back before declaring success.

This module is a *mechanical consumer*, not a semantic decision maker. It
never re-evaluates whether a target Issue should be closed / not-planned; it
only executes and confirms the decision it was handed (``--reason``,
per-AC verification results, evidence body, supersedes list). See the
Issue #1116 Outcome / In Scope / Out of Scope for the full rationale.

Design notes:
  - Evidence comments use a stable ownership marker
    (``<!-- issue_finalize_report:v1 repo=<repo> issue=<N> run_id=<id> -->``)
    separated from a content digest marker
    (``<!-- issue_finalize_report_digest:v1 sha256=<hash> -->``), mirroring
    the design of ``scripts/agent-logs/lib/github-comments.mjs`` (#937 /
    PR #977) ported to stdlib-only Python. The actual GitHub mutation is
    delegated to the existing ``issue_comment.publish`` controlled-executor
    command (``scripts/agent-guards/controlled_skill_mutation_exec.py``),
    whose marker-literal precheck already implements create / no-op, and
    whose Issue #1116 AC4 update branch implements update-on-digest-mismatch.
  - Issue close/reopen uses the already-allowed raw
    ``gh issue close <N> --repo <repo> --reason <reason>`` (no ``--comment``
    -- the evidence comment is always published separately via the
    controlled executor so it goes through the same idempotent marker path).
  - No automatic rollback: a partially-applied run is reported target-by
    -target (``applied | no_op | failed_no_mutation | failed_after_mutation |
    result_unknown``) and a re-run reads the live GitHub state again and
    only completes whatever is still missing.

Exit codes: 0 if overall status is "ok", 1 otherwise ("partial" / "failed").
Stdout: a single compact ``ISSUE_FINALIZE_RESULT_V1`` JSON object. No raw
child-process output, full Issue body, or full comment body is ever printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re as _re
import subprocess
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
_AGENT_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"
if str(_AGENT_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR))

from controlled_skill_mutation_policy import (  # noqa: E402
    COMMAND_ID_ISSUE_COMMENT_PUBLISH,
    INPUT_SCHEMA_BY_COMMAND,
    ISSUE_METADATA_NAMESPACE_SEGMENT,
    TRUSTED_REPO,
)

_CONTROLLED_EXEC_SCRIPT = _AGENT_GUARDS_DIR / "controlled_skill_mutation_exec.py"

# ---------------------------------------------------------------------------
# Schemas / vocabularies
# ---------------------------------------------------------------------------

FINALIZE_REQUEST_SCHEMA = "ISSUE_FINALIZE_REQUEST_V1"
FINALIZE_RESULT_SCHEMA = "ISSUE_FINALIZE_RESULT_V1"

VALID_REASONS = frozenset({"completed", "not_planned"})
VALID_AC_STATUSES = frozenset({"applicable", "not_applicable", "pre_existing_failure"})

# Issue #1116 AC6 status vocabulary, following the existing
# .claude/skills/edit-issue/scripts/edit_issue_txn.py vocabulary.
TARGET_STATUS_APPLIED = "applied"
TARGET_STATUS_NO_OP = "no_op"
TARGET_STATUS_FAILED_NO_MUTATION = "failed_no_mutation"
TARGET_STATUS_FAILED_AFTER_MUTATION = "failed_after_mutation"
TARGET_STATUS_RESULT_UNKNOWN = "result_unknown"

_OWNERSHIP_MARKER_RE = _re.compile(
    r"^<!-- issue_finalize_report:v1 repo=(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) "
    r"issue=(?P<issue>[0-9]+) run_id=(?P<run_id>[A-Za-z0-9_-]+) -->$"
)
_DIGEST_MARKER_RE = _re.compile(r"^<!-- issue_finalize_report_digest:v1 sha256=(?P<sha>[a-f0-9]{64}) -->$")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Request validation (AC2 / AC3): a closed-key, root-supplied termination
# decision. finalize_no_diff_issue.py never re-evaluates this decision --
# it only executes it and confirms via read-back.
# ---------------------------------------------------------------------------

_REQUEST_ALLOWED_KEYS = frozenset(
    {
        "schema",
        "issue_number",
        "repo",
        "reason",
        "ac_results",
        "evidence_body",
        "existing_evidence_comment_url",
        "supersedes",
        "run_id",
    }
)


def validate_request(data: object) -> str:
    """Validate a FINALIZE_REQUEST_V1-shaped dict. Returns "" on success,
    else a descriptive error string. Never inspects ac_results' semantic
    content beyond shape -- AC2: finalize does not re-evaluate the decision."""
    if not isinstance(data, dict):
        return "finalize_request_not_object"

    unknown_keys = set(data.keys()) - _REQUEST_ALLOWED_KEYS
    if unknown_keys:
        return f"finalize_request_unknown_fields: {sorted(unknown_keys)}"

    if data.get("schema") != FINALIZE_REQUEST_SCHEMA:
        return f"finalize_request_schema_mismatch: {data.get('schema')!r}"

    issue_number = data.get("issue_number")
    if type(issue_number) is not int or issue_number <= 0:
        return "finalize_request_issue_number_invalid"

    repo = data.get("repo")
    if repo != TRUSTED_REPO:
        return f"finalize_request_repo_mismatch: {repo!r} != {TRUSTED_REPO!r}"

    reason = data.get("reason")
    if reason not in VALID_REASONS:
        return f"finalize_request_reason_invalid: {reason!r}"

    ac_results = data.get("ac_results")
    if not isinstance(ac_results, list) or not ac_results:
        return "finalize_request_ac_results_invalid"
    for entry in ac_results:
        if not isinstance(entry, dict):
            return "finalize_request_ac_results_entry_not_object"
        if not isinstance(entry.get("ac"), str) or not entry["ac"]:
            return "finalize_request_ac_results_entry_ac_invalid"
        if entry.get("status") not in VALID_AC_STATUSES:
            return f"finalize_request_ac_results_entry_status_invalid: {entry.get('status')!r}"
        if not isinstance(entry.get("reason"), str) or not entry["reason"]:
            return "finalize_request_ac_results_entry_reason_invalid"

    evidence_body = data.get("evidence_body")
    existing_url = data.get("existing_evidence_comment_url")
    if bool(evidence_body) == bool(existing_url):
        return "finalize_request_evidence_source_must_be_exactly_one_of_body_or_existing_url"
    if evidence_body is not None and not isinstance(evidence_body, str):
        return "finalize_request_evidence_body_not_string"
    if existing_url is not None and not isinstance(existing_url, str):
        return "finalize_request_existing_evidence_comment_url_not_string"

    supersedes = data.get("supersedes", [])
    if not isinstance(supersedes, list):
        return "finalize_request_supersedes_not_list"
    for entry in supersedes:
        if type(entry) is not int or entry <= 0:
            return "finalize_request_supersedes_entry_invalid"
    if issue_number in supersedes:
        return "finalize_request_self_supersede_rejected"
    if len(set(supersedes)) != len(supersedes):
        return "finalize_request_supersedes_duplicate_entries"

    run_id = data.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not _re.match(r"^[A-Za-z0-9_-]+$", run_id)):
        return "finalize_request_run_id_invalid"

    return ""


def derive_run_id(request: dict) -> str:
    """Deterministic run_id derived from the semantic content of the
    request (Issue #1116 AC4/AC6): re-invoking the finalizer with the exact
    same termination decision produces the exact same ownership marker, so
    the executor's marker precheck can recognize a prior (possibly
    partially-applied) run instead of creating a fresh, unrelated comment
    each retry. An explicit request["run_id"] always takes priority."""
    if request.get("run_id"):
        return request["run_id"]
    canonical = _canonical_json(
        {
            "issue_number": request["issue_number"],
            "reason": request["reason"],
            "ac_results": request["ac_results"],
            "supersedes": sorted(request.get("supersedes", [])),
        }
    )
    return "auto-" + _sha256_hex(canonical)[:24]


def build_ownership_marker(repo: str, issue_number: int, run_id: str) -> str:
    return f"<!-- issue_finalize_report:v1 repo={repo} issue={issue_number} run_id={run_id} -->"


def build_digest_marker(sha256_hex: str) -> str:
    return f"<!-- issue_finalize_report_digest:v1 sha256={sha256_hex} -->"


def build_evidence_comment_body(repo: str, issue_number: int, run_id: str, payload_markdown: str) -> tuple[str, str]:
    """Returns (full_comment_body, ownership_marker). The digest is computed
    over payload_markdown alone (Issue #1116 AC4: marker identity and
    content digest are separated, mirroring github-comments.mjs)."""
    ownership_marker = build_ownership_marker(repo, issue_number, run_id)
    digest_marker = build_digest_marker(_sha256_hex(payload_markdown))
    body = f"{ownership_marker}\n{digest_marker}\n\n{payload_markdown}"
    return body, ownership_marker


def render_supersedes_payload(*, primary_issue_number: int, target_issue_number: int, reason: str) -> str:
    return (
        f"Superseded by #{primary_issue_number}: this Issue is closed as `{reason}` "
        f"because its scope is now covered by #{primary_issue_number}."
    )


# ---------------------------------------------------------------------------
# GitHub IO wrappers (each monkeypatchable independently in tests -- Issue
# #1116 uses fake-gh/fixture-style unit tests rather than a real gh binary).
# ---------------------------------------------------------------------------


def _fetch_issue_snapshot(issue_number: int, repo: str, gh_bin: str) -> tuple[dict | None, str]:
    """Live readback of an Issue's number/state/state_reason/is_pull_request
    (Issue #1116 AC3/AC5). Uses the REST single-issue endpoint, which always
    includes a `pull_request` key when the target number is actually a PR."""
    try:
        out = subprocess.run(
            [gh_bin, "api", f"repos/{repo}/issues/{issue_number}"],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
        if out.returncode != 0:
            return None, f"gh_api_issue_fetch_failed_rc_{out.returncode}"
        data = json.loads(out.stdout)
    except Exception as exc:  # noqa: BLE001 -- fail-closed on any transport surprise
        return None, f"gh_api_issue_fetch_exception:{exc}"

    if not isinstance(data, dict) or "state" not in data:
        return None, "gh_api_issue_fetch_schema_invalid"

    return (
        {
            "number": issue_number,
            "state": str(data.get("state", "")).upper(),
            "state_reason": data.get("state_reason"),
            "is_pull_request": "pull_request" in data,
        },
        "",
    )


def _close_issue_live(issue_number: int, repo: str, reason: str, gh_bin: str) -> str:
    """Raw `gh issue close` (Issue #1166 close/reopen is already allowed by
    local_main_branch_guard.py -- Issue #1116 does not add a new
    authorization layer). Never passes --comment: the evidence comment is
    always published separately via the controlled issue_comment.publish
    executor so it goes through the idempotent marker path."""
    gh_reason = "completed" if reason == "completed" else "not planned"
    try:
        out = subprocess.run(
            [gh_bin, "issue", "close", str(issue_number), "--repo", repo, "--reason", gh_reason],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
        if out.returncode != 0:
            return f"gh_issue_close_failed_rc_{out.returncode}:{(out.stderr or '')[:200]}"
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"gh_issue_close_exception:{exc}"


def _publish_marker_comment(
    *, issue_number: int, repo: str, marker: str, comment_body: str, gh_bin: str, project_root: Path
) -> tuple[str, str, str, str]:
    """Delegates to `controlled_skill_mutation_exec.py --command-id
    issue_comment.publish` (Issue #1116 AC4). Returns
    (status_detail, comment_id, comment_url, error)."""
    namespace_dir = (
        project_root
        / "artifacts"
        / str(issue_number)
        / ISSUE_METADATA_NAMESPACE_SEGMENT
        / COMMAND_ID_ISSUE_COMMENT_PUBLISH
    )
    namespace_dir.mkdir(parents=True, exist_ok=True)
    input_file = namespace_dir / "finalize_no_diff_issue_input.json"
    input_file.write_text(
        json.dumps(
            {
                "schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_ISSUE_COMMENT_PUBLISH],
                "issue_number": issue_number,
                "comment_body": comment_body,
                "marker": marker,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rel_input = str(input_file.relative_to(project_root))

    try:
        out = subprocess.run(
            [
                sys.executable,
                str(_CONTROLLED_EXEC_SCRIPT),
                "--command-id",
                COMMAND_ID_ISSUE_COMMENT_PUBLISH,
                "--issue-number",
                str(issue_number),
                "--input-file",
                rel_input,
                "--repo",
                repo,
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
            cwd=str(project_root),
        )
    except Exception as exc:  # noqa: BLE001
        return "", "", "", f"issue_comment_publish_exception:{exc}"

    try:
        result = json.loads(out.stdout) if out.stdout else {}
    except json.JSONDecodeError:
        result = {}

    if out.returncode != 0 or result.get("status") != "ok":
        reason = result.get("reason", f"issue_comment_publish_failed_rc_{out.returncode}")
        # Issue #2163 pattern: distinguish "mutation may have actually
        # happened remotely" (patch_attempted / applied_but_* statuses) from
        # a clean precondition reject, so callers never mistake a genuine
        # remote side effect for "nothing happened".
        if result.get("patch_attempted") or result.get("mutation_outcome") == "applied":
            reason = f"mutation_outcome_unknown:{reason}"
        return "", "", "", str(reason)

    return (
        str(result.get("status_detail", "")),
        str(result.get("comment_id", "")),
        str(result.get("comment_url", "")),
        "",
    )


def _verify_existing_comment_url(issue_number: int, repo: str, url: str, gh_bin: str) -> str:
    """AC2 alternate evidence source: verify a caller-referenced existing
    comment actually exists on the target Issue, instead of publishing a
    new one. Returns "" on success, else an error string."""
    try:
        out = subprocess.run(
            [gh_bin, "issue", "view", str(issue_number), "--repo", repo, "--json", "comments"],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
        if out.returncode != 0:
            return f"gh_failed_rc_{out.returncode}"
        data = json.loads(out.stdout)
    except Exception as exc:  # noqa: BLE001
        return f"existing_comment_verify_exception:{exc}"
    comments = data.get("comments", []) if isinstance(data, dict) else []
    if not any(c.get("url") == url for c in comments):
        return "existing_evidence_comment_url_not_found"
    return ""


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _target_result_skeleton(role: str) -> dict:
    return {
        "role": role,
        "observed_state": None,
        "completed_operations": [],
        "incomplete_operations": [],
        "result_unknown": False,
        "retryable": False,
        "target_status": TARGET_STATUS_RESULT_UNKNOWN,
        "evidence_comment": None,
        "errors": [],
    }


def _finalize_one_target(
    *,
    issue_number: int,
    role: str,
    reason: str,
    payload_markdown: str,
    repo: str,
    run_id: str,
    existing_evidence_comment_url: str | None,
    gh_bin: str,
    project_root: Path,
    initial_snapshot: dict,
) -> dict:
    result = _target_result_skeleton(role)

    # -- AC5/AC6: the pre-mutation snapshot was already read live by the
    # AC3 preflight pass (run_finalize), immediately before any mutation for
    # ANY target was attempted -- reused here rather than re-fetched, so a
    # stale local marker is never treated as authority for whether a close
    # is still needed (reopened-issue guard). The post-mutation read-back
    # below is always a fresh call.
    snapshot = initial_snapshot
    result["observed_state"] = snapshot

    # -- Evidence comment ------------------------------------------------
    if existing_evidence_comment_url:
        verify_err = _verify_existing_comment_url(issue_number, repo, existing_evidence_comment_url, gh_bin)
        if verify_err:
            result["incomplete_operations"].append("evidence_comment")
            result["errors"].append(verify_err)
        else:
            result["completed_operations"].append("evidence_comment")
            result["evidence_comment"] = {
                "status_detail": "existing_reference_verified",
                "comment_url": existing_evidence_comment_url,
            }
    else:
        comment_body, ownership_marker = build_evidence_comment_body(repo, issue_number, run_id, payload_markdown)
        status_detail, comment_id, comment_url, publish_err = _publish_marker_comment(
            issue_number=issue_number,
            repo=repo,
            marker=ownership_marker,
            comment_body=comment_body,
            gh_bin=gh_bin,
            project_root=project_root,
        )
        if publish_err:
            result["incomplete_operations"].append("evidence_comment")
            result["errors"].append(publish_err)
            if publish_err.startswith("mutation_outcome_unknown"):
                result["result_unknown"] = True
                result["retryable"] = True
        else:
            result["completed_operations"].append("evidence_comment")
            result["evidence_comment"] = {
                "status_detail": status_detail,
                "comment_id": comment_id,
                "comment_url": comment_url,
            }

    # -- Close -------------------------------------------------------------
    # Only close a target that is confirmed live-OPEN right now (never
    # inferred from a stale marker/comment -- Issue #1116 AC6). If already
    # CLOSED with the requested reason, this is a no-op close (no second
    # `gh issue close` call); if already CLOSED with a *different* reason,
    # this is a mismatch that is never silently corrected by reopening.
    close_attempted = False
    if snapshot["state"] == "OPEN":
        close_attempted = True
        close_err = _close_issue_live(issue_number, repo, reason, gh_bin)
        if close_err:
            result["incomplete_operations"].append("close")
            result["errors"].append(close_err)
        else:
            result["completed_operations"].append("close")
    elif snapshot["state"] == "CLOSED" and snapshot.get("state_reason") == reason:
        result["completed_operations"].append("close")
    else:
        result["incomplete_operations"].append("close")
        result["errors"].append(
            f"close_state_reason_mismatch: observed_state_reason={snapshot.get('state_reason')!r} "
            f"requested_reason={reason!r}"
        )

    # -- AC5: read-back after any mutation attempt. Read-back failure or
    # mismatch is never treated as success.
    after_snapshot, after_err = _fetch_issue_snapshot(issue_number, repo, gh_bin)
    if after_err:
        result["result_unknown"] = True
        result["retryable"] = True
        result["errors"].append(f"post_mutation_readback_failed:{after_err}")
    else:
        result["observed_state"] = after_snapshot
        if close_attempted and after_snapshot["state"] != "CLOSED":
            if "close" in result["completed_operations"]:
                result["completed_operations"].remove("close")
            if "close" not in result["incomplete_operations"]:
                result["incomplete_operations"].append("close")
            result["errors"].append("postcondition_close_state_mismatch")
        if after_snapshot["state"] == "CLOSED" and after_snapshot.get("state_reason") != reason:
            if "close" in result["completed_operations"]:
                result["completed_operations"].remove("close")
            if "close" not in result["incomplete_operations"]:
                result["incomplete_operations"].append("close")
            result["errors"].append("postcondition_state_reason_mismatch")

    # -- Classification (Issue #1116 AC6 vocabulary) ------------------------
    if result["result_unknown"]:
        result["target_status"] = TARGET_STATUS_RESULT_UNKNOWN
    elif not result["incomplete_operations"]:
        result["target_status"] = TARGET_STATUS_APPLIED if close_attempted else TARGET_STATUS_NO_OP
    elif result["completed_operations"]:
        result["target_status"] = TARGET_STATUS_FAILED_AFTER_MUTATION
        result["retryable"] = True
    else:
        result["target_status"] = TARGET_STATUS_FAILED_NO_MUTATION
        result["retryable"] = True

    return result


def run_finalize(request: dict, *, gh_bin: str = "gh", project_root: Path | None = None) -> dict:
    """Executes (or resumes) a no-diff/superseded termination decision.
    Never re-evaluates the decision's semantics (AC2) -- only orchestrates
    IO and read-back for the primary target and every explicit supersedes
    target, and classifies each target's final state (AC6)."""
    root = project_root or _REPO_ROOT

    req_err = validate_request(request)
    if req_err:
        return {
            "schema": FINALIZE_RESULT_SCHEMA,
            "status": "failed",
            "issue_number": request.get("issue_number") if isinstance(request, dict) else None,
            "repo": request.get("repo") if isinstance(request, dict) else None,
            "reason": request.get("reason") if isinstance(request, dict) else None,
            "targets": {},
            "errors": [req_err],
        }

    issue_number = request["issue_number"]
    repo = request["repo"]
    reason = request["reason"]
    supersedes = list(request.get("supersedes", []))
    run_id = derive_run_id(request)

    all_targets = [issue_number, *supersedes]

    # -- AC3: verify EVERY target (repo/issue-type/current-state) BEFORE any
    # mutation is attempted anywhere. If any target turns out to actually be
    # a pull request, the whole run stops before touching GitHub state.
    preflight: dict[int, dict] = {}
    for target in all_targets:
        snapshot, snap_err = _fetch_issue_snapshot(target, repo, gh_bin)
        preflight[target] = {"snapshot": snapshot, "error": snap_err}
        if snapshot is not None and snapshot["is_pull_request"]:
            targets_out = {}
            for t in all_targets:
                role = "primary" if t == issue_number else "supersedes"
                tr = _target_result_skeleton(role)
                if t == target:
                    tr["target_status"] = TARGET_STATUS_FAILED_NO_MUTATION
                    tr["errors"].append("target_is_pull_request")
                targets_out[str(t)] = tr
            return {
                "schema": FINALIZE_RESULT_SCHEMA,
                "status": "failed",
                "issue_number": issue_number,
                "repo": repo,
                "reason": reason,
                "targets": targets_out,
                "errors": [f"target_is_pull_request: #{target}"],
            }

    targets_out = {}

    def _finalize_or_unknown(*, target: int, role: str, target_reason: str, payload: str, existing_url: str | None) -> dict:
        pf = preflight[target]
        if pf["snapshot"] is None:
            tr = _target_result_skeleton(role)
            tr["result_unknown"] = True
            tr["retryable"] = True
            tr["target_status"] = TARGET_STATUS_RESULT_UNKNOWN
            tr["errors"].append(pf["error"])
            return tr
        return _finalize_one_target(
            issue_number=target,
            role=role,
            reason=target_reason,
            payload_markdown=payload,
            repo=repo,
            run_id=run_id,
            existing_evidence_comment_url=existing_url,
            gh_bin=gh_bin,
            project_root=root,
            initial_snapshot=pf["snapshot"],
        )

    primary_payload = request.get("evidence_body") or ""
    targets_out[str(issue_number)] = _finalize_or_unknown(
        target=issue_number,
        role="primary",
        target_reason=reason,
        payload=primary_payload,
        existing_url=request.get("existing_evidence_comment_url"),
    )

    for target in supersedes:
        payload = render_supersedes_payload(
            primary_issue_number=issue_number, target_issue_number=target, reason="not_planned"
        )
        targets_out[str(target)] = _finalize_or_unknown(
            target=target, role="supersedes", target_reason="not_planned", payload=payload, existing_url=None
        )

    statuses = {t["target_status"] for t in targets_out.values()}
    if statuses <= {TARGET_STATUS_APPLIED, TARGET_STATUS_NO_OP}:
        overall = "ok"
    elif statuses & {TARGET_STATUS_APPLIED, TARGET_STATUS_NO_OP}:
        overall = "partial"
    else:
        overall = "failed"

    return {
        "schema": FINALIZE_RESULT_SCHEMA,
        "status": overall,
        "issue_number": issue_number,
        "repo": repo,
        "reason": reason,
        "run_id": run_id,
        "targets": targets_out,
        "errors": [],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_request_from_args(args: argparse.Namespace) -> tuple[dict | None, str]:
    try:
        ac_results = json.loads(Path(args.ac_results_file).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"ac_results_file_unreadable:{exc}"

    evidence_body = None
    if args.evidence_body_file:
        try:
            evidence_body = Path(args.evidence_body_file).read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return None, f"evidence_body_file_unreadable:{exc}"

    request = {
        "schema": FINALIZE_REQUEST_SCHEMA,
        "issue_number": args.issue_number,
        "repo": args.repo,
        "reason": args.reason,
        "ac_results": ac_results,
        "supersedes": list(args.supersedes or []),
    }
    if evidence_body is not None:
        request["evidence_body"] = evidence_body
    if args.existing_evidence_comment_url:
        request["existing_evidence_comment_url"] = args.existing_evidence_comment_url
    if args.run_id:
        request["run_id"] = args.run_id
    return request, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize a no-diff / superseded Issue termination (Issue #1116)")
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--reason", required=True, choices=sorted(VALID_REASONS))
    parser.add_argument("--ac-results-file", required=True)
    parser.add_argument("--evidence-body-file")
    parser.add_argument("--existing-evidence-comment-url")
    parser.add_argument("--supersedes", type=int, action="append", default=[])
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)

    request, build_err = _build_request_from_args(args)
    if build_err:
        result = {
            "schema": FINALIZE_RESULT_SCHEMA,
            "status": "failed",
            "issue_number": args.issue_number,
            "repo": args.repo,
            "reason": args.reason,
            "targets": {},
            "errors": [build_err],
        }
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 1

    result = run_finalize(request)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
