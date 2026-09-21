#!/usr/bin/env python3
"""Strict, read-only evidence for the impl-review-loop pre-Step-1 choke point.

`IMPLEMENTATION_SCOPE_COVERAGE_V1` is deliberately a PR-publication record,
not an intake-time reconstruction of historical Issue text.  This module owns
the normalizer used by both open-pr (producer) and intake (consumer).
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
MANIFEST_SCHEMA = "IMPLEMENTATION_SCOPE_MANIFEST_V1"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.I)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$", re.I)
_FENCE_RE = re.compile(r"```ya?ml\s*\n(.*?)```", re.S | re.I)
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _body_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_DIGEST_RE.fullmatch(value))


def _section(body: str, heading: str) -> str:
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$", body, re.M | re.I)
    if match is None:
        return ""
    following = _SECTION_RE.search(body, match.end())
    return body[match.end() : following.start() if following else len(body)]


def _list_items(text: str, *, acceptance: bool = False) -> list[str]:
    values: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*[-*]\s+(?:\[[ xX]\]\s*)?(.*\S)\s*$", line)
        if match is None:
            continue
        value = match.group(1).strip()
        if acceptance:
            value = re.sub(r"^AC\d+\s*:\s*", "", value, flags=re.I)
            value = re.sub(r"<!--.*?-->", "", value).strip()
        value = value.replace("`", "")
        if value:
            values.append(value)
    return sorted(set(values))


def _machine_contract(body: str) -> Mapping[str, Any]:
    for fence in _FENCE_RE.findall(body):
        try:
            import yaml  # PyYAML is already used by repository skill tooling.

            parsed = yaml.safe_load(fence)
        except Exception:
            continue
        if isinstance(parsed, Mapping) and "goal_ref" in parsed:
            return parsed
    return {}


def build_scope_manifest(issue_body: str) -> dict[str, Any]:
    """Return the exact semantic scope contract, excluding operational prose.

    Lists are sorted before serialization, so checkbox state and list ordering
    cannot turn a progress-only Issue edit into a scope expansion.
    """
    contract = _machine_contract(issue_body)
    return {
        "schema_version": MANIFEST_SCHEMA,
        "goal_ref": str(contract.get("goal_ref") or "").strip(),
        "change_kind": str(contract.get("change_kind") or "").strip(),
        "in_scope": _list_items(_section(issue_body, "In Scope")),
        "acceptance_criteria": _list_items(_section(issue_body, "Acceptance Criteria"), acceptance=True),
        "allowed_paths": _list_items(_section(issue_body, "Allowed Paths")),
    }


def canonicalize_scope_manifest(scope: Any) -> dict[str, Any]:
    """Compatibility entry point; bodies receive the fixed semantic manifest."""
    manifest = build_scope_manifest(scope) if isinstance(scope, str) else scope
    if not isinstance(manifest, Mapping):
        manifest = {"invalid_scope": True}

    def normalize(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): normalize(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
        if isinstance(value, (list, tuple, set, frozenset)):
            items = [normalize(item) for item in value]
            return sorted(
                items, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
        return value

    normalized = normalize(manifest)
    return {"manifest": normalized, "content_sha256": _digest(normalized)}


def normalize_scope_coverage(immutable_merged_snapshot: Any, live_current_scope: Any) -> dict[str, Any]:
    merged = canonicalize_scope_manifest(immutable_merged_snapshot)
    current = canonicalize_scope_manifest(live_current_scope)
    exact = merged["content_sha256"] == current["content_sha256"]
    return {
        "schema": COVERAGE_SCHEMA,
        "immutable_merged_snapshot": merged,
        "live_current_scope": current,
        "exact_coverage": exact,
        "later_scope_expansion": not exact,
        "status": "covered_exactly" if exact else "later_scope_expansion",
    }


def build_scope_coverage_marker(*, issue_number: int, issue_body: str, pr_head_sha: str) -> dict[str, Any]:
    manifest = build_scope_manifest(issue_body)
    return {
        COVERAGE_SCHEMA: {
            "schema_version": COVERAGE_SCHEMA,
            "issue_number": issue_number,
            "issue_body_sha256": _body_digest(issue_body),
            "normalized_scope_manifest_sha256": _digest(manifest),
            "pr_head_sha": pr_head_sha,
            "scope_manifest": manifest,
        }
    }


def render_scope_coverage_marker(marker: Mapping[str, Any]) -> str:
    """Render a YAML block without adding a serialization dependency."""
    payload = marker.get(COVERAGE_SCHEMA) if isinstance(marker, Mapping) else None
    if not isinstance(payload, Mapping):
        raise ValueError("invalid scope coverage marker")
    # JSON is a YAML subset; using it for nested objects keeps canonical values
    # byte-for-byte visible and is accepted by the existing PR-body validator.
    lines = [f"{COVERAGE_SCHEMA}:"]
    for key in (
        "schema_version",
        "issue_number",
        "issue_body_sha256",
        "normalized_scope_manifest_sha256",
        "pr_head_sha",
    ):
        lines.append(f"  {key}: {json.dumps(payload.get(key), ensure_ascii=False)}")
    lines.append("  scope_manifest: " + json.dumps(payload.get("scope_manifest"), ensure_ascii=False, sort_keys=True))
    return "```yaml\n" + "\n".join(lines) + "\n```"


def _parse_marker(pr_body: str, *, issue_number: int) -> tuple[dict[str, Any] | None, list[str]]:
    matches: list[Any] = []
    for fence in _FENCE_RE.findall(pr_body or ""):
        try:
            import yaml

            parsed = yaml.safe_load(fence)
        except Exception:
            continue
        if isinstance(parsed, Mapping) and COVERAGE_SCHEMA in parsed:
            matches.append(parsed[COVERAGE_SCHEMA])
    if not matches:
        return None, ["scope_coverage_marker_missing"]
    if len(matches) != 1 or not isinstance(matches[0], Mapping):
        return None, ["scope_coverage_marker_ambiguous_or_invalid"]
    marker = dict(matches[0])
    errors: list[str] = []
    if marker.get("schema_version") != COVERAGE_SCHEMA:
        errors.append("scope_coverage_schema_version_invalid")
    if marker.get("issue_number") != issue_number:
        errors.append("scope_coverage_issue_identity_mismatch")
    if not _valid_digest(marker.get("issue_body_sha256")):
        errors.append("scope_coverage_issue_body_digest_invalid")
    if not _valid_sha(marker.get("pr_head_sha")):
        errors.append("scope_coverage_pr_head_invalid")
    manifest = marker.get("scope_manifest")
    if not isinstance(manifest, Mapping) or _digest(dict(manifest)) != marker.get("normalized_scope_manifest_sha256"):
        errors.append("scope_coverage_manifest_digest_mismatch")
    return marker if not errors else None, errors


def coverage_from_pr_body(*, pr_body: str, issue_number: int, live_issue_body: str) -> dict[str, Any]:
    marker, errors = _parse_marker(pr_body, issue_number=issue_number)
    if marker is None:
        return {
            "status": "missing_marker" if errors == ["scope_coverage_marker_missing"] else "invalid",
            "errors": errors,
        }
    immutable = marker["scope_manifest"]
    coverage = normalize_scope_coverage(immutable, live_issue_body)
    coverage.update({"marker": marker, "errors": []})
    return coverage


def _allowed_paths_covered(allowed_paths: list[str], files: Any) -> bool:
    if not allowed_paths or not isinstance(files, list):
        return False
    paths = [str(f.get("path")) for f in files if isinstance(f, Mapping) and isinstance(f.get("path"), str)]
    return all(
        any(path == entry or path.startswith(entry.rstrip("/") + "/") for path in paths) for entry in allowed_paths
    )


def _candidate_errors(candidate: Any, repo: str, issue_number: int) -> list[str]:
    if not isinstance(candidate, Mapping):
        return ["candidate_not_mapping"]
    errors: list[str] = []
    target = candidate.get("target")
    if not isinstance(target, Mapping) or target.get("repo") != repo or target.get("issue_number") != issue_number:
        errors.append("candidate_target_identity_mismatch")
    provenance = candidate.get("provenance")
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("kind") not in {"closing_relation", "verified_cross_reference"}
        or provenance.get("verified") is not True
    ):
        errors.append("candidate_provenance_unverified")
    lifecycle = candidate.get("lifecycle")
    if lifecycle not in {"merged", "open", "draft", "closed_unmerged"}:
        errors.append("candidate_lifecycle_invalid")
    pr = candidate.get("pr")
    if not isinstance(pr, Mapping) or type(pr.get("number")) is not int or pr["number"] <= 0:
        errors.append("candidate_pr_identity_invalid")
    if lifecycle == "merged":
        ancestry = candidate.get("main_ancestry")
        if (
            not _valid_sha(candidate.get("merge_oid"))
            or not isinstance(ancestry, Mapping)
            or ancestry.get("verified") is not True
        ):
            errors.append("merged_candidate_main_ancestry_unverified")
    return errors


def parse_implementation_landed_evidence(raw: Any) -> tuple[dict[str, Any] | None, list[str]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None, ["evidence_invalid_json"]
    if not isinstance(raw, Mapping):
        return None, ["evidence_not_mapping"]
    result = dict(raw)
    if result.get("schema") != EVIDENCE_SCHEMA or result.get("schema_version") != 1:
        return None, ["evidence_schema_invalid"]
    return result, []


def validate_implementation_landed_evidence(
    raw: Any, *, repo: str, issue_number: int, now: dt.datetime | None = None
) -> dict[str, Any]:
    evidence, errors = parse_implementation_landed_evidence(raw)
    if evidence is None:
        return {"valid": False, "errors": errors, "evidence": None}
    target = evidence.get("target")
    if (
        not isinstance(target, Mapping)
        or target.get("repo") != repo
        or target.get("issue_number") != issue_number
        or not _valid_digest(target.get("body_sha256"))
    ):
        errors.append("target_identity_or_body_digest_invalid")
    freshness = evidence.get("freshness")
    if not isinstance(freshness, Mapping) or freshness.get("status") != "fresh":
        errors.append("evidence_stale_or_missing_freshness")
    if evidence.get("contradictory") is True:
        errors.append("evidence_contradictory")
    if not isinstance(evidence.get("candidates"), list):
        errors.append("candidates_not_list")
    else:
        for index, candidate in enumerate(evidence["candidates"]):
            errors.extend(f"candidate[{index}]:{err}" for err in _candidate_errors(candidate, repo, issue_number))
    rebind = evidence.get("decision_time_rebind")
    if rebind is not None and (not isinstance(rebind, Mapping) or rebind.get("status") != "fresh"):
        errors.append("freshness_rebind_failed")
    return {"valid": not errors, "errors": errors, "evidence": evidence}


def _coverage_for(candidate: Mapping[str, Any], evidence: Mapping[str, Any]) -> Mapping[str, Any] | None:
    local = candidate.get("scope_coverage")
    if isinstance(local, Mapping):
        return local
    global_coverage = evidence.get("scope_coverage")
    return global_coverage if isinstance(global_coverage, Mapping) else None


def derive_landing_disposition(
    raw: Any, *, repo: str, issue_number: int, now: dt.datetime | None = None
) -> dict[str, Any]:
    validated = validate_implementation_landed_evidence(raw, repo=repo, issue_number=issue_number, now=now)
    if not validated["valid"]:
        return {"disposition": "reconciliation_required", "reason_codes": validated["errors"], "candidate": None}
    evidence = validated["evidence"]
    candidates = evidence["candidates"]
    qualified = [c for c in candidates if c["lifecycle"] in {"merged", "open", "draft", "closed_unmerged"}]
    # Structured closing linkage has explicit GitHub landing semantics and is
    # therefore selected ahead of an otherwise valid timeline cross-reference.
    # Ambiguity is only among candidates at the chosen authority level.
    closing = [c for c in qualified if c.get("provenance", {}).get("kind") == "closing_relation"]
    if closing:
        qualified = closing
    if len(qualified) > 1:
        return {
            "disposition": "reconciliation_required",
            "reason_codes": ["qualified_candidate_conflict"],
            "candidate": None,
        }
    if not qualified:
        return {
            "disposition": "ordinary_dispatch_or_explicit_recovery",
            "reason_codes": ["no_qualified_candidate"],
            "candidate": None,
        }
    candidate = qualified[0]
    lifecycle = candidate["lifecycle"]
    if lifecycle == "closed_unmerged":
        return {
            "disposition": "ordinary_dispatch_or_explicit_recovery",
            "reason_codes": ["closed_unmerged_candidate"],
            "candidate": candidate,
        }
    if lifecycle == "merged":
        ancestry = candidate.get("main_ancestry")
        if not isinstance(ancestry, Mapping) or ancestry.get("reachable") is not True:
            return {
                "disposition": "ordinary_dispatch_or_explicit_recovery",
                "reason_codes": ["merged_candidate_not_on_current_main"],
                "candidate": candidate,
            }
        coverage = _coverage_for(candidate, evidence)
        if (
            isinstance(coverage, Mapping)
            and coverage.get("exact_coverage") is True
            and coverage.get("later_scope_expansion") is False
        ):
            return {"disposition": "implementation_already_landed", "reason_codes": [], "candidate": candidate}
        return {
            "disposition": "ordinary_dispatch_or_explicit_recovery",
            "reason_codes": ["legacy_or_later_scope_expansion"],
            "candidate": candidate,
        }
    coverage = _coverage_for(candidate, evidence)
    if isinstance(coverage, Mapping) and coverage.get("status") == "invalid":
        return {
            "disposition": "reconciliation_required",
            "reason_codes": list(coverage.get("errors") or ["invalid_scope_marker"]),
            "candidate": candidate,
        }
    if isinstance(coverage, Mapping) and coverage.get("exact_coverage") is True:
        return {"disposition": "existing_pr_resume", "reason_codes": [], "candidate": candidate}
    if (
        (coverage is None or (isinstance(coverage, Mapping) and coverage.get("status") == "missing_marker"))
        and candidate.get("current_scope_ownership") is True
        and candidate.get("head_fresh") is True
    ):
        return {
            "disposition": "existing_pr_resume",
            "reason_codes": ["markerless_allowed_paths_coverage"],
            "candidate": candidate,
        }
    return {
        "disposition": "reconciliation_required",
        "reason_codes": ["open_draft_scope_ownership_not_exact"],
        "candidate": candidate,
    }


def _run(argv: list[str]) -> tuple[int, str, str]:
    cp = subprocess.run(argv, check=False, capture_output=True, text=True)
    return cp.returncode, cp.stdout, cp.stderr


def _json(run: Callable[[list[str]], tuple[int, str, str]], argv: list[str]) -> tuple[Any, bool]:
    rc, stdout, _ = run(argv)
    if rc:
        return None, False
    try:
        return json.loads(stdout), True
    except json.JSONDecodeError:
        return None, False


def collect_candidate_inputs(
    *,
    repo: str,
    issue_number: int,
    current_scope: Any,
    run_command: Callable[[list[str]], tuple[int, str, str]] = _run,
    max_candidates: int = 20,
) -> dict[str, Any]:
    """Collect bounded closing and timeline cross-reference candidates.

    A body mention is never treated as a cross-reference.  Timeline candidates
    must carry `source.issue.pull_request`, and are later reconciled against a
    fresh PR record; closing linkage remains preferred provenance.
    """
    issue_body = current_scope if isinstance(current_scope, str) else json.dumps(current_scope, ensure_ascii=False)
    commands: list[dict[str, Any]] = []

    def run(argv: list[str]) -> tuple[int, str, str]:
        rc, out, err = run_command(argv)
        commands.append({"argv": argv, "exit_code": rc})
        return rc, out, err

    rows, list_ok = _json(
        run,
        [
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
            "number,closingIssuesReferences",
        ],
    )
    timeline, timeline_ok = _json(
        run,
        [
            "gh",
            "api",
            "--paginate",
            "-H",
            "Accept: application/vnd.github+json",
            f"repos/{repo}/issues/{issue_number}/timeline?per_page=100",
        ],
    )
    found: dict[int, str] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, Mapping) or type(row.get("number")) is not int:
                continue
            refs = row.get("closingIssuesReferences") or []
            if any(isinstance(ref, Mapping) and ref.get("number") == issue_number for ref in refs):
                found[row["number"]] = "closing_relation"
    if isinstance(timeline, list):
        for event in timeline:
            source = event.get("source") if isinstance(event, Mapping) else None
            source_issue = source.get("issue") if isinstance(source, Mapping) else None
            pr_info = source_issue.get("pull_request") if isinstance(source_issue, Mapping) else None
            number = source_issue.get("number") if isinstance(source_issue, Mapping) else None
            if isinstance(pr_info, Mapping) and type(number) is int and number > 0:
                found.setdefault(number, "verified_cross_reference")
    candidates: list[dict[str, Any]] = []
    for number, provenance_kind in list(found.items())[:max_candidates]:
        pr, ok = _json(
            run,
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,url,state,isDraft,mergedAt,mergeCommit,headRefOid,closingIssuesReferences,body,files",
            ],
        )
        if not ok or not isinstance(pr, Mapping):
            continue
        refs = pr.get("closingIssuesReferences") or []
        closing = any(isinstance(ref, Mapping) and ref.get("number") == issue_number for ref in refs)
        provenance_kind = "closing_relation" if closing else provenance_kind
        state = str(pr.get("state") or "").upper()
        lifecycle = (
            "merged"
            if pr.get("mergedAt")
            else "draft"
            if state == "OPEN" and pr.get("isDraft") is True
            else "open"
            if state == "OPEN"
            else "closed_unmerged"
        )
        candidate: dict[str, Any] = {
            "target": {"repo": repo, "issue_number": issue_number},
            "pr": {"number": number, "url": pr.get("url"), "head_sha": pr.get("headRefOid")},
            "provenance": {"kind": provenance_kind, "verified": True},
            "lifecycle": lifecycle,
            "head_fresh": lifecycle not in {"open", "draft"} or _valid_sha(pr.get("headRefOid")),
            "current_scope_ownership": False,
        }
        coverage = coverage_from_pr_body(
            pr_body=str(pr.get("body") or ""), issue_number=issue_number, live_issue_body=issue_body
        )
        candidate["scope_coverage"] = coverage
        if lifecycle in {"open", "draft"} and coverage.get("status") == "missing_marker":
            candidate["current_scope_ownership"] = _allowed_paths_covered(
                build_scope_manifest(issue_body)["allowed_paths"], pr.get("files")
            )
        if lifecycle == "merged":
            merge_oid = (pr.get("mergeCommit") or {}).get("oid") if isinstance(pr.get("mergeCommit"), Mapping) else None
            candidate["merge_oid"] = merge_oid
            candidate["main_ancestry"] = {"verified": False, "reachable": False}
            if _valid_sha(merge_oid):
                rc, out, _ = run(["gh", "api", f"repos/{repo}/compare/{merge_oid}...main", "--jq", ".status"])
                candidate["main_ancestry"] = {
                    "verified": rc == 0,
                    "reachable": rc == 0 and out.strip() in {"ahead", "identical"},
                }
        candidates.append(candidate)
    status = "fresh" if list_ok and timeline_ok else "stale"
    return {
        "schema": EVIDENCE_SCHEMA,
        "schema_version": 1,
        "target": {"repo": repo, "issue_number": issue_number, "body_sha256": _body_digest(issue_body)},
        "freshness": {"status": status},
        "contradictory": False,
        "candidates": candidates,
        "scope_coverage": None,
        "discovery": {"bounded_max_candidates": max_candidates, "commands": commands},
        "current_scope_manifest": canonicalize_scope_manifest(issue_body),
        "decision_time_rebind": {"status": "fresh" if status == "fresh" else "stale"},
    }
