#!/usr/bin/env python3
"""
plan_issue_scope_rollup.py

Read-only CLI that analyses a set of issues and PRs and produces an
ISSUE_SCOPE_ROLLUP_PLAN_V2 JSON payload describing merge / rollup candidates.

Usage:
    python3 plan_issue_scope_rollup.py --issues-json <path> --prs-json <path> [--current-issue <number>] [--repo <owner/repo>]

Mutation-free: this script never creates, edits, or closes any Issue or PR.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 2
SOURCE = "plan_issue_scope_rollup"

# Confidence levels
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

# Signals
SIGNAL_SHARED_DEDUPE_KEY = "shared_dedupe_key"
SIGNAL_EXACT_ALLOWED_PATH_OVERLAP = "exact_allowed_path_overlap"
SIGNAL_SAME_PARENT_ISSUE = "same_parent_issue"
SIGNAL_SAME_SKILL_FAMILY = "same_skill_family"
SIGNAL_SAME_FAILURE_MODE_MARKER = "same_failure_mode_marker"

# Suggested actions
ACTION_MERGE_INTO_CURRENT_PR = "merge_into_current_pr"
ACTION_AMEND_CURRENT_ISSUE = "amend_current_issue"
ACTION_CREATE_PARENT_ROLLUP_ISSUE = "create_parent_rollup_issue"
ACTION_KEEP_SEPARATE_WITH_REASON = "keep_separate_with_reason"
ACTION_HUMAN_REVIEW_REQUIRED = "human_review_required"

# Keywords that flag security-related content -> always human_review_required
SECURITY_KEYWORDS = frozenset(
    [
        "security",
        "auth",
        "authentication",
        "authorization",
        "permission",
        "sandbox",
        "privilege",
        "secret",
        "credential",
        "token",
        "oauth",
    ]
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_of_inputs(issues_raw: str, prs_raw: str) -> str:
    h = hashlib.sha256()
    h.update(issues_raw.encode("utf-8"))
    h.update(prs_raw.encode("utf-8"))
    return h.hexdigest()


def _load_json(path: str) -> tuple[list[dict[str, Any]], str]:
    """Return (parsed_list, raw_text).  Raises ValueError on parse error."""
    text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array in {path!r}, got {type(data).__name__}")
    return data, text


def _extract_field(item: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in item:
            return item[k]
    return default


def _body_text(item: dict[str, Any]) -> str:
    return str(_extract_field(item, "body", default=""))


def _extract_dedupe_key(item: dict[str, Any]) -> str | None:
    """Extract dedupe_key from body (## Source section) or item field."""
    if "dedupe_key" in item:
        return str(item["dedupe_key"])
    body = _body_text(item)
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("dedupe_key:"):
            val = line.split(":", 1)[1].strip().strip('"').strip("'")
            if val:
                return val
    return None


def _extract_allowed_paths(item: dict[str, Any]) -> frozenset[str]:
    """Extract Allowed Paths list from the issue/PR body."""
    body = _body_text(item)
    paths: list[str] = []
    in_section = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Allowed Paths"):
            in_section = True
            continue
        if in_section:
            if stripped.startswith("## ") and not stripped.startswith("## Allowed Paths"):
                break
            if stripped.startswith("- ") or stripped.startswith("* "):
                path = stripped[2:].strip().strip("`")
                if path:
                    paths.append(path)
    return frozenset(paths)


def _extract_parent_issue(item: dict[str, Any]) -> str | None:
    """Extract parent issue reference from body."""
    body = _body_text(item)
    for line in body.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("parent_issue:") or stripped.startswith("parent issue:"):
            val = line.split(":", 1)[1].strip().strip('"').strip("'")
            if val and val.lower() not in ("none", "null", ""):
                return val
    # check machine-readable contract block
    for line in body.splitlines():
        line_s = line.strip()
        if line_s.startswith("parent_issue:"):
            val = line_s.split(":", 1)[1].strip().strip('"').strip("'")
            if val and val.lower() not in ("none", "null", ""):
                return val
    return None


def _extract_skill_family(item: dict[str, Any]) -> str | None:
    """Derive a skill family from Allowed Paths or title keywords."""
    paths = _extract_allowed_paths(item)
    # Look for .claude/skills/<family>/ patterns
    families: set[str] = set()
    for p in paths:
        parts = p.replace("\\", "/").split("/")
        if ".claude" in parts and "skills" in parts:
            idx = parts.index("skills")
            if idx + 1 < len(parts):
                families.add(parts[idx + 1])
    if len(families) == 1:
        return next(iter(families))
    if len(families) > 1:
        # Multiple skill families in Allowed Paths -> return comma-joined (for signal purposes)
        return ",".join(sorted(families))
    # Fall back to title keywords
    title = str(_extract_field(item, "title", default=""))
    for keyword in ("issue-refinement-loop", "impl-review-loop", "review-issue", "create-issue", "edit-issue"):
        if keyword in title.lower():
            return keyword
    return None


def _extract_failure_mode_marker(item: dict[str, Any]) -> str | None:
    """Extract a failure mode marker from body (e.g. ## Failure Mode section)."""
    body = _body_text(item)
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("failure_mode_marker:") or stripped.startswith("failure_mode:"):
            val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            if val:
                return val
    return None


def _is_security_related(item: dict[str, Any]) -> bool:
    """Return True if the issue/PR touches security-sensitive areas."""
    body = _body_text(item).lower()
    title = str(_extract_field(item, "title", default="")).lower()
    combined = title + " " + body
    return any(kw in combined for kw in SECURITY_KEYWORDS)


def _determine_confidence(signals: list[str]) -> str:
    """
    Determine confidence level based on active signals.

    high conditions:
      - shared_dedupe_key
      - exact_allowed_path_overlap + same_parent_issue
      - exact_allowed_path_overlap + same_failure_mode_marker
      NOTE: same_skill_family ONLY is NOT sufficient for high.

    medium:
      - exact_allowed_path_overlap (alone)
      - same_parent_issue (alone or with same_skill_family)
      - same_skill_family + another signal (except the combinations already at high)

    low:
      - same_skill_family only
      - any single low-value signal
    """
    s = set(signals)

    if SIGNAL_SHARED_DEDUPE_KEY in s:
        return CONFIDENCE_HIGH

    if SIGNAL_EXACT_ALLOWED_PATH_OVERLAP in s and SIGNAL_SAME_PARENT_ISSUE in s:
        return CONFIDENCE_HIGH

    if SIGNAL_EXACT_ALLOWED_PATH_OVERLAP in s and SIGNAL_SAME_FAILURE_MODE_MARKER in s:
        return CONFIDENCE_HIGH

    # same_skill_family ONLY -> must NOT be high (AC6 requirement)
    if s == {SIGNAL_SAME_SKILL_FAMILY}:
        return CONFIDENCE_LOW

    if SIGNAL_EXACT_ALLOWED_PATH_OVERLAP in s:
        return CONFIDENCE_MEDIUM

    if SIGNAL_SAME_PARENT_ISSUE in s:
        return CONFIDENCE_MEDIUM

    if SIGNAL_SAME_SKILL_FAMILY in s and len(s) >= 2:
        return CONFIDENCE_MEDIUM

    if SIGNAL_SAME_FAILURE_MODE_MARKER in s:
        return CONFIDENCE_MEDIUM

    return CONFIDENCE_LOW


def _suggested_action(
    item: dict[str, Any],
    signals: list[str],
    confidence: str,
) -> str:
    """Choose the appropriate suggested_action for a candidate."""
    if _is_security_related(item):
        return ACTION_HUMAN_REVIEW_REQUIRED

    if confidence == CONFIDENCE_HIGH:
        if SIGNAL_SHARED_DEDUPE_KEY in signals:
            return ACTION_MERGE_INTO_CURRENT_PR
        if SIGNAL_EXACT_ALLOWED_PATH_OVERLAP in signals and SIGNAL_SAME_PARENT_ISSUE in signals:
            return ACTION_AMEND_CURRENT_ISSUE
        return ACTION_MERGE_INTO_CURRENT_PR

    if confidence == CONFIDENCE_MEDIUM:
        if SIGNAL_SAME_PARENT_ISSUE in signals:
            return ACTION_CREATE_PARENT_ROLLUP_ISSUE
        return ACTION_AMEND_CURRENT_ISSUE

    return ACTION_KEEP_SEPARATE_WITH_REASON


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------


def _build_candidates(
    current: dict[str, Any] | None,
    items: list[dict[str, Any]],
    kind: str,
) -> list[dict[str, Any]]:
    """Build candidate entries for a list of items (issues or PRs)."""
    if current is None:
        return []

    current_number = _extract_field(current, "number", default=None)
    current_dedupe_key = _extract_dedupe_key(current)
    current_allowed_paths = _extract_allowed_paths(current)
    current_parent = _extract_parent_issue(current)
    current_skill_family = _extract_skill_family(current)
    current_failure_mode = _extract_failure_mode_marker(current)

    candidates: list[dict[str, Any]] = []

    for item in items:
        item_number = _extract_field(item, "number", default=None)
        if item_number == current_number and kind == "issue":
            continue  # skip the current issue itself

        signals: list[str] = []

        # Signal: shared_dedupe_key
        item_dedupe_key = _extract_dedupe_key(item)
        if current_dedupe_key and item_dedupe_key and current_dedupe_key == item_dedupe_key:
            signals.append(SIGNAL_SHARED_DEDUPE_KEY)

        # Signal: exact_allowed_path_overlap
        item_allowed_paths = _extract_allowed_paths(item)
        if current_allowed_paths and item_allowed_paths:
            overlap = current_allowed_paths & item_allowed_paths
            if overlap:
                signals.append(SIGNAL_EXACT_ALLOWED_PATH_OVERLAP)

        # Signal: same_parent_issue
        item_parent = _extract_parent_issue(item)
        if (
            current_parent
            and item_parent
            and current_parent.strip() == item_parent.strip()
        ):
            signals.append(SIGNAL_SAME_PARENT_ISSUE)

        # Signal: same_skill_family
        item_skill_family = _extract_skill_family(item)
        if (
            current_skill_family
            and item_skill_family
            and current_skill_family == item_skill_family
        ):
            signals.append(SIGNAL_SAME_SKILL_FAMILY)

        # Signal: same_failure_mode_marker
        item_failure_mode = _extract_failure_mode_marker(item)
        if (
            current_failure_mode
            and item_failure_mode
            and current_failure_mode == item_failure_mode
        ):
            signals.append(SIGNAL_SAME_FAILURE_MODE_MARKER)

        if not signals:
            continue  # no relationship detected

        confidence = _determine_confidence(signals)
        action = _suggested_action(item, signals, confidence)

        # dedupe_key for the candidate itself
        candidate_dedupe_key = item_dedupe_key or f"{kind}-{item_number}"

        candidates.append(
            {
                "kind": kind,
                "number": item_number,
                "confidence": confidence,
                "dedupe_key": candidate_dedupe_key,
                "signals": signals,
                "suggested_action": action,
            }
        )

    return candidates


def run(
    issues_json_path: str,
    prs_json_path: str,
    current_issue_number: int | None = None,
    repo: str = "",
) -> dict[str, Any]:
    """Main analysis function. Returns ISSUE_SCOPE_ROLLUP_PLAN_V2 dict."""
    warnings: list[str] = []

    try:
        issues, issues_raw = _load_json(issues_json_path)
    except (json.JSONDecodeError, ValueError) as exc:
        issues = []
        issues_raw = ""
        warnings.append(f"issues_json parse error: {exc}")

    try:
        prs, prs_raw = _load_json(prs_json_path)
    except (json.JSONDecodeError, ValueError) as exc:
        prs = []
        prs_raw = ""
        warnings.append(f"prs_json parse error: {exc}")

    body_sha256 = _sha256_of_inputs(issues_raw, prs_raw)
    completeness = "partial" if warnings else "full"

    # Identify the "current" issue
    current_issue: dict[str, Any] | None = None
    if current_issue_number is not None:
        for iss in issues:
            if _extract_field(iss, "number", default=None) == current_issue_number:
                current_issue = iss
                break
        if current_issue is None and issues:
            warnings.append(
                f"current_issue #{current_issue_number} not found in issues_json; "
                "using first issue as reference"
            )
            current_issue = issues[0]
    elif issues:
        current_issue = issues[0]

    issue_candidates = _build_candidates(current_issue, issues, "issue")
    pr_candidates = _build_candidates(current_issue, prs, "pr")
    all_candidates = issue_candidates + pr_candidates

    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repo": repo,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": SOURCE,
        "body_sha256": body_sha256,
        "input": {
            "completeness": completeness,
            "warnings": warnings,
        },
        "candidates": all_candidates,
    }

    return plan


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Produce ISSUE_SCOPE_ROLLUP_PLAN_V2 JSON from issues and PRs lists."
    )
    parser.add_argument(
        "--issues-json",
        required=True,
        help="Path to JSON file containing a list of issues (gh issue list --json output).",
    )
    parser.add_argument(
        "--prs-json",
        required=True,
        help="Path to JSON file containing a list of PRs (gh pr list --json output).",
    )
    parser.add_argument(
        "--current-issue",
        type=int,
        default=None,
        help="Issue number of the current issue to compare against (optional).",
    )
    parser.add_argument(
        "--repo",
        default="",
        help="Repository in owner/repo format (informational only).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output file path. Defaults to stdout.",
    )

    args = parser.parse_args(argv)

    plan = run(
        issues_json_path=args.issues_json,
        prs_json_path=args.prs_json,
        current_issue_number=args.current_issue,
        repo=args.repo,
    )

    output_text = json.dumps(plan, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).write_text(output_text, encoding="utf-8")
    else:
        print(output_text)


if __name__ == "__main__":
    main()
