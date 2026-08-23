"""
Producer for SOURCE_EVIDENCE_ACQUISITION_RESULT_V1 (#2195).

Owns claim_kind classification, ordered route plan generation
(evidence_kind / dependency_group / capability based), primary route
dispatch, and — only when the primary route's *final* result (after
provider-owned retry) is a lane-specific failure — a single cross-lane
recovery dispatch bounded by a caller-supplied recovery budget.

This module does NOT reinterpret provider stderr, exit codes, or retry
policy (AC4). Executors passed in by the caller are expected to already
represent the terminal outcome of provider-internal retry; this module
only classifies that terminal outcome and decides whether/where to
recover.

REPO_EVIDENCE_REF_V1's existing required field set is never modified by
this module -- collectors below only *produce* REPO_EVIDENCE_REF_V1-shaped
dicts, they do not add new required fields to it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, TypedDict

SCHEMA_ID = "source_evidence_acquisition_result/v1"
TERMINAL_ARTIFACT_SCHEMA_ID = "source_evidence_terminal_artifact/v1"

FAILURE_DOMAINS = (
    "provider",
    "transport",
    "mcp_protocol",
    "tool_execution",
    "source_lookup",
    "reference_validation",
)

ACQUISITION_OUTCOMES = ("succeeded", "failed", "unknown_outcome")

SEMANTIC_VERDICTS = ("supported", "contradicted", "insufficient", "not_evaluated")

DISPOSITIONS = ("proceed", "recover", "human_review", "environment_degraded")

_OPERATIONAL_FAILURE_DOMAINS = {"transport", "mcp_protocol", "tool_execution"}

MAX_TERMINAL_ARTIFACT_BYTES = 16 * 1024

# Route registry: evidence_kind -> ordered lane ids that can independently
# produce that evidence_kind. local_git and github_blob are deliberately
# kept as the only pair eligible for cross-lane recovery under this Issue's
# scope: they depend on different failure domains (local git object store
# vs. GitHub REST/Contents API), so a failure in one lane is not evidence
# that the other lane will also fail.
_ROUTES_BY_EVIDENCE_KIND: dict[str, tuple[str, ...]] = {
    "repo_blob_at_commit": ("local_git", "github_blob"),
}

_ABSOLUTE_PATH_PATTERN = re.compile(r"(^|[\s\"'=:])/(home|Users|root)/[^\s\"']*")
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(bearer\s+[a-z0-9._\-]+|ghp_[a-z0-9]{20,}|api[_-]?key\s*[:=]\s*\S+)"
)


class RouteAttempt(TypedDict, total=False):
    route_id: str
    lane: str
    dispatch_state: str
    acquisition_outcome: str
    failure_domain: Optional[str]
    provider_failure_code: Optional[str]
    cross_lane_recovery: bool


class ExecutorResult(TypedDict, total=False):
    acquisition_outcome: str
    failure_domain: Optional[str]
    provider_failure_code: Optional[str]
    evidence_ref: Optional[dict]


class GithubBlobTransportError(Exception):
    """Raised by an injected blob_bytes_getter to signal a transport-layer
    failure (network, auth, gh CLI) rather than a content/lookup failure."""


@dataclass
class RecoveryBudget:
    """In-memory, single-refinement-run-invocation-scoped cross-lane
    recovery budget. Owned by issue-refinement-loop (the caller); the same
    instance must be threaded across all claim calls within one run so
    that `max_total` is enforced across the whole run, not per claim.

    `max_total` counts *dispatched* cross-lane recovery attempts
    (trial-based): a recovery attempt that itself fails still consumes
    budget.
    """

    max_total: int = 1
    per_claim_max: int = 1
    _consumed_total: int = field(default=0, repr=False)
    _consumed_by_claim: dict = field(default_factory=dict, repr=False)

    def remaining_total(self) -> int:
        return max(0, self.max_total - self._consumed_total)

    def remaining_for_claim(self, claim_id: str) -> int:
        used = self._consumed_by_claim.get(claim_id, 0)
        return max(0, self.per_claim_max - used)

    def can_recover(self, claim_id: str) -> bool:
        return self.remaining_total() > 0 and self.remaining_for_claim(claim_id) > 0

    def consume(self, claim_id: str) -> None:
        self._consumed_total += 1
        self._consumed_by_claim[claim_id] = self._consumed_by_claim.get(claim_id, 0) + 1


def build_route_plan(
    evidence_kind: str,
    *,
    capability_snapshot: Optional[dict] = None,
) -> list[dict]:
    """Build an ordered route plan for `evidence_kind`.

    `capability_snapshot` maps lane_id -> bool. When it is None, all known
    lanes for the evidence_kind are treated as eligible. When a lane is
    absent from a provided snapshot (capability could not be probed), the
    route is treated as `eligible: false` rather than erroring (per Issue
    #2195 Background: "route capability snapshot が取得できない場合は当該
    route を eligible: false として扱う").
    """
    lanes = _ROUTES_BY_EVIDENCE_KIND.get(evidence_kind, ())
    plan = []
    for lane in lanes:
        if capability_snapshot is None:
            eligible = True
            reason = None
        else:
            eligible = bool(capability_snapshot.get(lane, False))
            reason = None if eligible else "capability_unavailable"
        plan.append(
            {
                "route_id": f"{lane}:{evidence_kind}",
                "lane": lane,
                "evidence_kind": evidence_kind,
                "eligible": eligible,
                "reason": reason,
            }
        )
    return plan


def _classify_disposition(
    *,
    evidence_refs: list[dict],
    semantic_verdict: str,
    attempts: list[RouteAttempt],
    budget_blocked: bool,
) -> str:
    if evidence_refs and semantic_verdict != "not_evaluated":
        return "proceed"

    failure_domains = {a["failure_domain"] for a in attempts if a.get("failure_domain")}

    if budget_blocked:
        # An eligible alternate lane exists but the recovery budget was
        # exhausted before it could be dispatched. This is neither a
        # semantic dead end nor a pure environment failure -- surface it
        # distinctly so the caller does not conflate it with either.
        return "recover"

    if failure_domains and failure_domains <= _OPERATIONAL_FAILURE_DOMAINS:
        return "environment_degraded"

    return "human_review"


def _scrub_check(text: str) -> list[str]:
    """Return a list of forbidden-pattern violations found in `text`."""
    violations = []
    if _ABSOLUTE_PATH_PATTERN.search(text):
        violations.append("absolute_path_detected")
    if _CREDENTIAL_PATTERN.search(text):
        violations.append("credential_pattern_detected")
    return violations


def build_terminal_artifact(
    *,
    run_id: str,
    claim: dict,
    attempts: list[RouteAttempt],
    evidence_refs: list[dict],
    disposition: str,
    unresolved_reason: str,
) -> dict:
    """Build a bounded (<= 16 KiB serialized), secret/path-free terminal
    artifact (AC6). Raises ValueError if forbidden content is detected or
    if the artifact cannot be bounded under the size limit."""

    failure_taxonomy: dict[str, int] = {}
    for a in attempts:
        key = a.get("failure_domain") or "none"
        failure_taxonomy[key] = failure_taxonomy.get(key, 0) + 1

    def _attempts_view(detailed: bool) -> list[dict]:
        if detailed:
            return [
                {
                    "route_id": a["route_id"],
                    "lane": a["lane"],
                    "dispatch_state": a["dispatch_state"],
                    "acquisition_outcome": a["acquisition_outcome"],
                    "failure_domain": a.get("failure_domain"),
                    "provider_failure_code": a.get("provider_failure_code"),
                }
                for a in attempts
            ]
        return [
            {
                "route_id": a["route_id"],
                "lane": a["lane"],
                "dispatch_state": a["dispatch_state"],
                "acquisition_outcome": a["acquisition_outcome"],
            }
            for a in attempts
        ]

    baseline = claim.get("baseline") or {}
    bounded_baseline = {
        k: v
        for k, v in baseline.items()
        if k in ("claim_text_digest", "source_hint") and isinstance(v, (str, int, float, bool, type(None)))
    }

    artifact = {
        "schema": TERMINAL_ARTIFACT_SCHEMA_ID,
        "run_id": run_id,
        "claim_id": claim.get("claim_id"),
        "claim_kind": claim.get("claim_kind"),
        "evidence_kind": claim.get("evidence_kind"),
        "baseline": bounded_baseline,
        "attempts": _attempts_view(detailed=True),
        "failure_taxonomy": failure_taxonomy,
        "disposition": disposition,
        "unresolved_reason": unresolved_reason,
        "evidence_ref_count": len(evidence_refs),
    }

    encoded = json.dumps(artifact, ensure_ascii=False, sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_TERMINAL_ARTIFACT_BYTES:
        artifact["attempts"] = _attempts_view(detailed=False)
        encoded = json.dumps(artifact, ensure_ascii=False, sort_keys=True)

    if len(encoded.encode("utf-8")) > MAX_TERMINAL_ARTIFACT_BYTES:
        raise ValueError("terminal_artifact_exceeds_size_bound")

    violations = _scrub_check(encoded)
    if violations:
        raise ValueError(f"terminal_artifact_forbidden_content:{','.join(violations)}")

    return artifact


def _slice_and_hash(content: bytes, start_line: int, end_line: int) -> tuple[Optional[str], Optional[str]]:
    """Return (excerpt_sha256, error) for the 1-indexed inclusive line
    range [start_line, end_line] of `content`."""
    lines = content.split(b"\n")
    line_count = len(lines) - 1 if content.endswith(b"\n") else len(lines)
    if not (1 <= start_line <= end_line <= line_count):
        return None, "line_range_out_of_bounds"
    sliced = lines[start_line - 1 : end_line]
    reconstructed = b"\n".join(sliced)
    if end_line < len(lines):
        reconstructed += b"\n"
    return hashlib.sha256(reconstructed).hexdigest(), None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_repo_evidence_ref(
    *,
    commit_sha: str,
    path: str,
    start_line: int,
    end_line: int,
    excerpt_sha256: str,
    object_format: str,
    owner: str,
    repo: str,
    anchor_text: Optional[str],
) -> dict:
    permalink = f"https://github.com/{owner}/{repo}/blob/{commit_sha}/{path}#L{start_line}-L{end_line}"
    return {
        "type": "REPO_EVIDENCE_REF_V1",
        "commit_sha": commit_sha,
        "object_format": object_format,
        "path": path,
        "start_line": start_line,
        "end_line": end_line,
        "permalink": permalink,
        "excerpt_sha256": excerpt_sha256,
        "anchor_text": anchor_text,
        "verification_status": "verified",
        "verification_method": "sha256_hash_match",
        "verified_at": _now_iso(),
    }


def collect_local_git_evidence(
    *,
    commit_sha: str,
    path: str,
    start_line: int,
    end_line: int,
    repo_root: Optional[Path] = None,
    object_format: str = "sha1",
    owner: str = "squne121",
    repo: str = "loop-protocol",
    anchor_text: Optional[str] = None,
    blob_bytes_getter: Optional[Callable[[str, str], bytes]] = None,
) -> ExecutorResult:
    """local_git lane: `git show <commit>:<path>` based collector.

    `blob_bytes_getter(commit_sha, path) -> bytes` is injectable for unit
    tests; when absent, `git show` is invoked against `repo_root`.
    """
    try:
        if blob_bytes_getter is not None:
            content = blob_bytes_getter(commit_sha, path)
        else:
            result = subprocess.run(
                ["git", "show", f"{commit_sha}:{path}"],
                cwd=repo_root,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                return {
                    "acquisition_outcome": "failed",
                    "failure_domain": "source_lookup",
                    "provider_failure_code": "git_show_failed",
                    "evidence_ref": None,
                }
            content = result.stdout
    except FileNotFoundError:
        return {
            "acquisition_outcome": "failed",
            "failure_domain": "tool_execution",
            "provider_failure_code": "git_not_found",
            "evidence_ref": None,
        }
    except Exception:
        return {
            "acquisition_outcome": "unknown_outcome",
            "failure_domain": None,
            "provider_failure_code": "local_git_unexpected_error",
            "evidence_ref": None,
        }

    excerpt_sha256, error = _slice_and_hash(content, start_line, end_line)
    if error:
        return {
            "acquisition_outcome": "failed",
            "failure_domain": "reference_validation",
            "provider_failure_code": error,
            "evidence_ref": None,
        }

    evidence_ref = _build_repo_evidence_ref(
        commit_sha=commit_sha,
        path=path,
        start_line=start_line,
        end_line=end_line,
        excerpt_sha256=excerpt_sha256,
        object_format=object_format,
        owner=owner,
        repo=repo,
        anchor_text=anchor_text,
    )
    return {
        "acquisition_outcome": "succeeded",
        "failure_domain": None,
        "provider_failure_code": None,
        "evidence_ref": evidence_ref,
    }


def collect_github_blob_evidence(
    *,
    commit_sha: str,
    path: str,
    start_line: int,
    end_line: int,
    object_format: str = "sha1",
    owner: str = "squne121",
    repo: str = "loop-protocol",
    anchor_text: Optional[str] = None,
    blob_bytes_getter: Optional[Callable[[str, str], bytes]] = None,
) -> ExecutorResult:
    """github_blob lane: commit-pinned GitHub Contents/Blob API collector.

    `blob_bytes_getter(commit_sha, path) -> bytes` is injectable for unit
    tests and for real invocation (should wrap `gh api
    repos/{owner}/{repo}/contents/{path}?ref={commit_sha}` and base64
    decode the `content` field). Raise `GithubBlobTransportError` from the
    getter to signal a transport-layer failure distinct from a content
    lookup failure.
    """
    try:
        if blob_bytes_getter is not None:
            content = blob_bytes_getter(commit_sha, path)
        else:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{owner}/{repo}/contents/{path}",
                    "-f",
                    f"ref={commit_sha}",
                    "--jq",
                    ".content",
                ],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                return {
                    "acquisition_outcome": "failed",
                    "failure_domain": "transport",
                    "provider_failure_code": "gh_api_failed",
                    "evidence_ref": None,
                }
            content = base64.b64decode(result.stdout.strip() or b"")
    except GithubBlobTransportError:
        return {
            "acquisition_outcome": "failed",
            "failure_domain": "transport",
            "provider_failure_code": "github_blob_transport_error",
            "evidence_ref": None,
        }
    except FileNotFoundError:
        return {
            "acquisition_outcome": "failed",
            "failure_domain": "tool_execution",
            "provider_failure_code": "gh_not_found",
            "evidence_ref": None,
        }
    except Exception:
        return {
            "acquisition_outcome": "unknown_outcome",
            "failure_domain": None,
            "provider_failure_code": "github_blob_unexpected_error",
            "evidence_ref": None,
        }

    excerpt_sha256, error = _slice_and_hash(content, start_line, end_line)
    if error:
        return {
            "acquisition_outcome": "failed",
            "failure_domain": "reference_validation",
            "provider_failure_code": error,
            "evidence_ref": None,
        }

    evidence_ref = _build_repo_evidence_ref(
        commit_sha=commit_sha,
        path=path,
        start_line=start_line,
        end_line=end_line,
        excerpt_sha256=excerpt_sha256,
        object_format=object_format,
        owner=owner,
        repo=repo,
        anchor_text=anchor_text,
    )
    return {
        "acquisition_outcome": "succeeded",
        "failure_domain": None,
        "provider_failure_code": None,
        "evidence_ref": evidence_ref,
    }


def run_acquisition(
    *,
    claim: dict,
    run_id: str,
    executors: dict[str, Callable[[], ExecutorResult]],
    budget: RecoveryBudget,
    dispatched_routes: set,
    capability_snapshot: Optional[dict] = None,
    semantic_evaluator: Optional[Callable[[dict, list[dict]], str]] = None,
) -> dict:
    """Run the full producer flow for a single claim and return a
    SOURCE_EVIDENCE_ACQUISITION_RESULT_V1 envelope (as a dict).

    `executors` maps lane_id -> zero-arg callable returning ExecutorResult.
    Each callable is expected to represent the *final* result after any
    provider-internal retry has already completed (AC4) -- this function
    does not retry the same route itself.

    `dispatched_routes` is a caller-owned set of (run_id, claim_id,
    route_id) tuples used to enforce the AC5 no-redispatch invariant
    across calls within a run.
    """
    claim_id = claim["claim_id"]
    evidence_kind = claim["evidence_kind"]

    route_plan = build_route_plan(evidence_kind, capability_snapshot=capability_snapshot)
    eligible_routes = [r for r in route_plan if r["eligible"]]

    attempts: list[RouteAttempt] = []
    evidence_refs: list[dict] = []
    budget_blocked = False

    def _dispatch(route: dict, *, cross_lane: bool) -> RouteAttempt:
        key = (run_id, claim_id, route["route_id"])
        if key in dispatched_routes:
            raise ValueError(f"duplicate_dispatch_forbidden:{key}")
        dispatched_routes.add(key)

        exec_fn = executors.get(route["lane"])
        if exec_fn is None:
            result: ExecutorResult = {
                "acquisition_outcome": "unknown_outcome",
                "failure_domain": None,
                "provider_failure_code": "no_executor_bound",
                "evidence_ref": None,
            }
        else:
            result = exec_fn()

        outcome = result.get("acquisition_outcome", "unknown_outcome")
        dispatch_state = "outcome_unknown" if outcome == "unknown_outcome" else "dispatched"
        attempt: RouteAttempt = {
            "route_id": route["route_id"],
            "lane": route["lane"],
            "dispatch_state": dispatch_state,
            "acquisition_outcome": outcome,
            "failure_domain": result.get("failure_domain"),
            "provider_failure_code": result.get("provider_failure_code"),
            "cross_lane_recovery": cross_lane,
        }
        if outcome == "succeeded" and result.get("evidence_ref") is not None:
            evidence_refs.append(result["evidence_ref"])
        return attempt

    if eligible_routes:
        primary = eligible_routes[0]
        attempts.append(_dispatch(primary, cross_lane=False))

        primary_failed = attempts[-1]["acquisition_outcome"] != "succeeded"
        if primary_failed and len(eligible_routes) > 1:
            secondary = eligible_routes[1]
            if budget.can_recover(claim_id):
                budget.consume(claim_id)
                attempts.append(_dispatch(secondary, cross_lane=True))
            else:
                budget_blocked = True

    semantic_verdict = "not_evaluated"
    if evidence_refs:
        if semantic_evaluator is not None:
            semantic_verdict = semantic_evaluator(claim, evidence_refs)
        else:
            semantic_verdict = "supported"

    disposition = _classify_disposition(
        evidence_refs=evidence_refs,
        semantic_verdict=semantic_verdict,
        attempts=attempts,
        budget_blocked=budget_blocked,
    )

    envelope: dict = {
        "schema": SCHEMA_ID,
        "claim": {
            "claim_id": claim_id,
            "claim_kind": claim.get("claim_kind", "dispositive"),
            "evidence_kind": evidence_kind,
            "dependency_group": claim.get("dependency_group"),
        },
        "baseline": claim.get("baseline") or {},
        "route_plan": route_plan,
        "attempts": attempts,
        "evidence_refs": evidence_refs,
        "semantic_verdict": semantic_verdict,
        "disposition": disposition,
    }

    if disposition in ("human_review", "environment_degraded", "recover") and not evidence_refs:
        if not eligible_routes:
            unresolved_reason = "no_eligible_route"
        elif budget_blocked:
            unresolved_reason = "cross_lane_recovery_budget_exhausted"
        else:
            unresolved_reason = "all_eligible_routes_failed"
        envelope["terminal_artifact"] = build_terminal_artifact(
            run_id=run_id,
            claim=envelope["claim"],
            attempts=attempts,
            evidence_refs=evidence_refs,
            disposition=disposition,
            unresolved_reason=unresolved_reason,
        )

    return envelope
