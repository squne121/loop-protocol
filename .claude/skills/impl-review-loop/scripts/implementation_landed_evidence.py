#!/usr/bin/env python3
"""Evidence-only landing disposition for impl-review-loop pre-Step-1 intake.

This module is deliberately conservative: candidate discovery is not landing
authority.  Only a validated, fresh evidence record can suppress worker,
worktree, or new-PR dispatch.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from typing import Any

EVIDENCE_SCHEMA = "IMPLEMENTATION_LANDED_EVIDENCE_V1"
COVERAGE_SCHEMA = "IMPLEMENTATION_SCOPE_COVERAGE_V1"
VALID_DISPOSITIONS = frozenset(
    {
        "implementation_already_landed",
        "existing_pr_resume",
        "ordinary_dispatch_or_explicit_recovery",
        "reconciliation_required",
    }
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$", re.IGNORECASE)


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set, frozenset)):
        normalized = [_canonical(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    if isinstance(value, str):
        return "\n".join(line.rstrip() for line in value.strip().splitlines())
    return value


def _digest(value: Any) -> str:
    payload = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _as_utc(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_DIGEST_RE.fullmatch(value))


def canonicalize_scope_manifest(scope: Any) -> dict[str, Any]:
    """Normalize a scope snapshot into a stable content-addressed manifest."""
    canonical_scope = _canonical(scope)
    return {
        "canonical_scope": canonical_scope,
        "content_sha256": _digest(canonical_scope),
    }


def normalize_scope_coverage(
    immutable_merged_snapshot: Any,
    live_current_scope: Any,
) -> dict[str, Any]:
    """Compare immutable merged scope with live scope without heuristics."""
    merged = canonicalize_scope_manifest(immutable_merged_snapshot)
    current = canonicalize_scope_manifest(live_current_scope)
    exact = merged["content_sha256"] == current["content_sha256"]
    return {
        "schema": COVERAGE_SCHEMA,
        "immutable_merged_snapshot": merged,
        "live_current_scope": current,
        "exact_coverage": exact,
        "later_scope_expansion": not exact,
        "status": "exact" if exact else "later_scope_expansion",
    }


def _candidate_errors(candidate: Any, repo: str, issue_number: int) -> list[str]:
    if not isinstance(candidate, Mapping):
        return ["candidate_not_mapping"]
    errors: list[str] = []
    provenance = candidate.get("provenance")
    if not isinstance(provenance, Mapping):
        errors.append("candidate_provenance_missing")
    else:
        if provenance.get("kind") not in {"closing_relation", "verified_cross_reference"}:
            errors.append("candidate_provenance_kind_invalid")
        if provenance.get("verified") is not True:
            errors.append("candidate_provenance_not_verified")
    lifecycle = candidate.get("lifecycle")
    if lifecycle not in {"merged", "open", "draft", "closed_unmerged"}:
        errors.append("candidate_lifecycle_invalid")
    pr = candidate.get("pr")
    if not isinstance(pr, Mapping) or not isinstance(pr.get("number"), int) or pr["number"] <= 0:
        errors.append("candidate_pr_identity_invalid")
    target = candidate.get("target")
    if target is not None:
        if not isinstance(target, Mapping) or target.get("repo") != repo or target.get("issue_number") != issue_number:
            errors.append("candidate_target_identity_mismatch")
    if lifecycle == "merged":
        merge_oid = candidate.get("merge_oid")
        ancestry = candidate.get("main_ancestry")
        if not _valid_sha(merge_oid):
            errors.append("merged_candidate_merge_oid_invalid")
        if (
            not isinstance(ancestry, Mapping)
            or ancestry.get("verified") is not True
            or ancestry.get("reachable") is not True
        ):
            errors.append("merged_candidate_main_ancestry_unverified")
    if lifecycle in {"open", "draft"}:
        if candidate.get("head_fresh") is not True:
            errors.append("resumable_candidate_head_not_fresh")
        if candidate.get("current_scope_ownership") is not True:
            errors.append("resumable_candidate_scope_ownership_unverified")
    return errors


def parse_implementation_landed_evidence(raw: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Strictly parse a JSON object or mapping; never infer absent evidence."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None, ["evidence_invalid_json"]
    if not isinstance(raw, Mapping):
        return None, ["evidence_not_mapping"]
    result = dict(raw)
    if result.get("schema") != EVIDENCE_SCHEMA:
        return None, ["evidence_schema_invalid"]
    if result.get("schema_version") != 1:
        return None, ["evidence_schema_version_invalid"]
    return result, []


def validate_implementation_landed_evidence(
    raw: Any,
    *,
    repo: str,
    issue_number: int,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Validate identity, freshness, candidate provenance, and contradictions."""
    evidence, errors = parse_implementation_landed_evidence(raw)
    if evidence is None:
        return {"valid": False, "errors": errors, "evidence": None}
    target = evidence.get("target")
    if not isinstance(target, Mapping) or target.get("repo") != repo or target.get("issue_number") != issue_number:
        errors.append("target_identity_mismatch")
    elif not _valid_digest(target.get("body_sha256")):
        errors.append("target_body_digest_invalid")
    freshness = evidence.get("freshness")
    if not isinstance(freshness, Mapping) or freshness.get("status") != "fresh":
        errors.append("evidence_stale_or_missing_freshness")
    elif now is not None:
        observed_at = _as_utc(freshness.get("observed_at"))
        max_age_seconds = freshness.get("max_age_seconds")
        if (
            observed_at is None
            or not isinstance(max_age_seconds, int)
            or isinstance(max_age_seconds, bool)
            or max_age_seconds < 0
        ):
            errors.append("evidence_freshness_shape_invalid")
        elif observed_at + dt.timedelta(seconds=max_age_seconds) < now.astimezone(dt.timezone.utc):
            errors.append("evidence_stale")
    if evidence.get("contradictory") is True:
        errors.append("evidence_contradictory")
    candidates = evidence.get("candidates")
    if not isinstance(candidates, list):
        errors.append("candidates_not_list")
    else:
        for index, candidate in enumerate(candidates):
            errors.extend(f"candidate[{index}]:{error}" for error in _candidate_errors(candidate, repo, issue_number))
    coverage = evidence.get("scope_coverage")
    if coverage is not None:
        if not isinstance(coverage, Mapping):
            errors.append("scope_coverage_not_mapping")
        elif coverage.get("schema") != COVERAGE_SCHEMA:
            errors.append("scope_coverage_schema_invalid")
        elif (
            not isinstance(coverage.get("exact_coverage"), bool)
            or not isinstance(coverage.get("later_scope_expansion"), bool)
            or coverage["exact_coverage"] == coverage["later_scope_expansion"]
        ):
            errors.append("scope_coverage_flags_invalid")
        else:
            merged_manifest = coverage.get("immutable_merged_snapshot")
            live_manifest = coverage.get("live_current_scope")
            if not isinstance(merged_manifest, Mapping) or not isinstance(live_manifest, Mapping):
                errors.append("scope_coverage_manifest_missing")
            elif not _valid_digest(merged_manifest.get("content_sha256")) or not _valid_digest(
                live_manifest.get("content_sha256")
            ):
                errors.append("scope_coverage_manifest_digest_invalid")
    return {"valid": not errors, "errors": errors, "evidence": evidence}


def derive_landing_disposition(
    raw: Any,
    *,
    repo: str,
    issue_number: int,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Return the only pre-Step-1 disposition from validated evidence.

    Invalid, stale, contradictory, or identity-mismatched evidence has the
    highest priority and always reconciles rather than silently dispatching.
    """
    validated = validate_implementation_landed_evidence(raw, repo=repo, issue_number=issue_number, now=now)
    if not validated["valid"]:
        return {"disposition": "reconciliation_required", "reason_codes": validated["errors"], "candidate": None}
    evidence = validated["evidence"]
    candidates = evidence["candidates"]
    merged = [candidate for candidate in candidates if candidate["lifecycle"] == "merged"]
    coverage = evidence.get("scope_coverage")
    if merged:
        if (
            isinstance(coverage, Mapping)
            and coverage.get("exact_coverage") is True
            and coverage.get("later_scope_expansion") is False
        ):
            return {"disposition": "implementation_already_landed", "reason_codes": [], "candidate": merged[0]}
        return {
            "disposition": "ordinary_dispatch_or_explicit_recovery",
            "reason_codes": ["later_scope_expansion_or_missing_exact_coverage"],
            "candidate": merged[0],
        }
    resumable = [candidate for candidate in candidates if candidate["lifecycle"] in {"open", "draft"}]
    if resumable:
        return {"disposition": "existing_pr_resume", "reason_codes": [], "candidate": resumable[0]}
    return {
        "disposition": "ordinary_dispatch_or_explicit_recovery",
        "reason_codes": ["no_qualified_landed_or_resumable_candidate"],
        "candidate": None,
    }


def _run(argv: list[str]) -> tuple[int, str, str]:
    completed = subprocess.run(argv, check=False, capture_output=True, text=True)
    return completed.returncode, completed.stdout, completed.stderr


def collect_candidate_inputs(
    *,
    repo: str,
    issue_number: int,
    current_scope: Any,
    run_command: Callable[[list[str]], tuple[int, str, str]] = _run,
    max_candidates: int = 20,
) -> dict[str, Any]:
    """Bounded read-only discovery; emits evidence, never a landing verdict.

    Closing relations and explicit cross-references merely annotate candidate
    provenance. Scope snapshots are intentionally absent unless supplied by a
    trusted immutable source, so this collector cannot manufacture landed
    authority from PR metadata alone.
    """
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    commands: list[dict[str, Any]] = []
    argv = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--limit",
        str(max_candidates),
        "--json",
        "number,url,state,isDraft,mergedAt,mergeCommit,headRefOid,closingIssuesReferences,body,updatedAt",
    ]
    rc, stdout, stderr = run_command(argv)
    commands.append({"argv": argv, "exit_code": rc})
    candidates: list[dict[str, Any]] = []
    if rc == 0:
        try:
            rows = json.loads(stdout)
        except json.JSONDecodeError:
            rows = []
        if isinstance(rows, list):
            for row in rows[:max_candidates]:
                if not isinstance(row, Mapping) or not isinstance(row.get("number"), int):
                    continue
                closing = any(
                    isinstance(ref, Mapping)
                    and ref.get("number") == issue_number
                    and ref.get("url") == f"https://github.com/{repo}/issues/{issue_number}"
                    for ref in (row.get("closingIssuesReferences") or [])
                )
                body = str(row.get("body") or "")
                generic_refs = bool(
                    re.search(
                        rf"(?im)^\s*refs\s*:?[ \t]+(?:#|https://github\\.com/[^/]+/[^/]+/issues/){issue_number}(?:\b|/)",
                        body,
                    )
                )
                explicit_cross_reference = bool(re.search(rf"(?<!\w)#{issue_number}(?!\w)", body)) and not generic_refs
                if not closing and not explicit_cross_reference:
                    continue
                state = str(row.get("state") or "").upper()
                lifecycle = (
                    "merged"
                    if row.get("mergedAt")
                    else (
                        "draft"
                        if state == "OPEN" and row.get("isDraft") is True
                        else "open"
                        if state == "OPEN"
                        else "closed_unmerged"
                    )
                )
                merge_oid = (
                    (row.get("mergeCommit") or {}).get("oid") if isinstance(row.get("mergeCommit"), Mapping) else None
                )
                candidate: dict[str, Any] = {
                    "target": {"repo": repo, "issue_number": issue_number},
                    "pr": {"number": row["number"], "url": row.get("url"), "head_sha": row.get("headRefOid")},
                    "provenance": {
                        "kind": "closing_relation" if closing else "verified_cross_reference",
                        "verified": True,
                    },
                    "lifecycle": lifecycle,
                    # Discovery alone never attests these resume-only facts.
                    "head_fresh": False,
                    "current_scope_ownership": False,
                }
                if lifecycle == "merged":
                    candidate["merge_oid"] = merge_oid
                    candidate["main_ancestry"] = {"verified": False, "reachable": False}
                    if _valid_sha(merge_oid):
                        compare_argv = ["gh", "api", f"repos/{repo}/compare/{merge_oid}...main", "--jq", ".status"]
                        compare_rc, compare_out, _compare_err = run_command(compare_argv)
                        commands.append({"argv": compare_argv, "exit_code": compare_rc})
                        candidate["main_ancestry"] = {
                            "verified": compare_rc == 0,
                            "reachable": compare_rc == 0 and compare_out.strip() in {"ahead", "identical"},
                        }
                candidates.append(candidate)
    freshness_status = "fresh" if rc == 0 else "stale"
    return {
        "schema": EVIDENCE_SCHEMA,
        "schema_version": 1,
        "target": {
            "repo": repo,
            "issue_number": issue_number,
            "body_sha256": _digest(current_scope),
        },
        "freshness": {"status": freshness_status, "observed_at": now, "max_age_seconds": 300},
        "contradictory": False,
        "candidates": candidates,
        "scope_coverage": None,
        "discovery": {
            "bounded_max_candidates": max_candidates,
            "commands": commands,
            "stderr": stderr.strip() if rc else "",
        },
        "current_scope_manifest": canonicalize_scope_manifest(current_scope),
    }
