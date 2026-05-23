#!/usr/bin/env python3
"""plan_child_materialization.py — Read-only CHILD_MATERIALIZATION_PLAN_V1 generator.

Reads a delivery-rollup parent issue body (via GitHub CLI or a local fixture file)
and produces a CHILD_MATERIALIZATION_PLAN_V1 YAML plan on stdout.

This script is read-only: it never mutates GitHub Issues.
Mutation is delegated to create_issue_txn.py and the edit-issue skill.

Usage:
    # From a live GitHub issue:
    uv run python3 plan_child_materialization.py --repo owner/repo --issue 254

    # From a local body fixture (for tests / dry-run):
    uv run python3 plan_child_materialization.py --body-file fixture.md --issue 254

Exit codes:
    0  — plan generated successfully (may contain warnings)
    1  — fatal error (bad args, GitHub API failure, unrecoverable parse error)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ChildStatus = Literal["missing", "existing", "stale_body_only", "ambiguous"]
ChildAction = Literal["create_issue", "reuse_and_update_parent", "no_op", "human_escalation"]


@dataclass
class ChildEntry:
    child_id: str
    title: str
    status: ChildStatus
    existing_issue_number: int | None
    action: ChildAction
    dedupe_key: str


@dataclass
class ParentBodyUpdate:
    replace: str
    with_: str


@dataclass
class Plan:
    parent_issue: int
    parent_mode: str
    children: list[ChildEntry] = field(default_factory=list)
    parent_body_updates: list[ParentBodyUpdate] = field(default_factory=list)
    required_issue_creations: list[str] = field(default_factory=list)
    required_issue_edits: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Matches a child list line in either of:
#   - C254-3 ... （未起票）
#   - C254-3 ... #281
#   - - C254-3 ...（未起票）  (bullet form)
_CHILD_LINE_RE = re.compile(
    r"^[-*]?\s*"
    r"(?P<child_id>C\d+-\d+)"
    r"\s+"
    r"(?P<rest>.+)$"
)

# Matches an existing GitHub issue reference at the end of a rest string, e.g. "#281"
_ISSUE_REF_RE = re.compile(r"#(?P<number>\d+)")

# Placeholder text patterns (Japanese and common English variants)
_PLACEHOLDER_PATTERNS = [
    r"（未起票）",
    r"\(未起票\)",
    r"\(not yet created\)",
    r"\(TBD\)",
]
_PLACEHOLDER_RE = re.compile("|".join(_PLACEHOLDER_PATTERNS))

# Matches the parent_mode in the Machine-Readable Contract block
_PARENT_MODE_RE = re.compile(
    r"(?im)^[ \t]*parent_mode\s*:\s*[\"']?(?P<mode>[\w-]+)[\"']?\s*$"
)


def _extract_parent_mode(body: str) -> str:
    """Extract parent_mode from the Machine-Readable Contract block."""
    m = _PARENT_MODE_RE.search(body)
    if m:
        return m.group("mode")
    return "delivery-rollup"  # default assumption


def _parse_child_lines(body: str) -> list[dict]:
    """Parse all Cxxx-N child lines from the issue body.

    Returns a list of raw dicts with keys:
        child_id, title_fragment, rest, is_placeholder, raw_issue_refs
    """
    results = []
    for line in body.splitlines():
        m = _CHILD_LINE_RE.match(line.strip())
        if not m:
            continue
        child_id = m.group("child_id")
        rest = m.group("rest").strip()
        is_placeholder = bool(_PLACEHOLDER_RE.search(rest))
        issue_refs = [int(r) for r in _ISSUE_REF_RE.findall(rest)]
        results.append(
            {
                "child_id": child_id,
                "rest": rest,
                "is_placeholder": is_placeholder,
                "raw_issue_refs": issue_refs,
            }
        )
    return results


# ---------------------------------------------------------------------------
# GitHub API helpers (read-only)
# ---------------------------------------------------------------------------

def _fetch_issue_body(repo: str, issue_number: int, gh_bin: str = "gh") -> str:
    """Fetch the issue body text from GitHub. Raises RuntimeError on failure."""
    args = [
        gh_bin,
        "issue",
        "view",
        str(issue_number),
        "--repo",
        repo,
        "--json",
        "body",
        "--jq",
        ".body",
    ]
    cp = subprocess.run(args, capture_output=True, text=True)
    if cp.returncode != 0:
        raise RuntimeError(
            f"gh issue view failed (exit {cp.returncode}): {cp.stderr.strip()}"
        )
    return cp.stdout.strip()


def _fetch_open_issues(repo: str, gh_bin: str = "gh") -> list[dict]:
    """Fetch all open issues as a list of {number, title} dicts. Best-effort."""
    args = [
        gh_bin,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        "number,title",
    ]
    cp = subprocess.run(args, capture_output=True, text=True)
    if cp.returncode != 0:
        return []
    try:
        return json.loads(cp.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------------------
# Classification logic
# ---------------------------------------------------------------------------

def _classify_child(
    parsed: dict,
    open_issues: list[dict],
    *,
    parent_issue: int,
) -> ChildEntry:
    """Classify a single parsed child line into a ChildEntry."""
    child_id: str = parsed["child_id"]
    rest: str = parsed["rest"]
    is_placeholder: bool = parsed["is_placeholder"]
    raw_issue_refs: list[int] = parsed["raw_issue_refs"]

    dedupe_key = f"delivery-rollup:{parent_issue}:{child_id}"

    # Strip trailing issue references and placeholder text to get a clean title
    title = _PLACEHOLDER_RE.sub("", rest)
    # Remove trailing issue refs like "#281"
    title = re.sub(r"\s*#\d+\s*$", "", title).strip()
    # Remove trailing colon/dash artefacts
    title = title.rstrip("-:").strip()

    if is_placeholder and not raw_issue_refs:
        # Body says "(未起票)" with no issue ref → missing
        return ChildEntry(
            child_id=child_id,
            title=title,
            status="missing",
            existing_issue_number=None,
            action="create_issue",
            dedupe_key=dedupe_key,
        )

    if raw_issue_refs:
        issue_number = raw_issue_refs[0]
        # Check if the referenced issue exists in open_issues
        open_numbers = {i["number"] for i in open_issues}
        if issue_number in open_numbers:
            if is_placeholder:
                # Body says both "(未起票)" and a real issue ref → stale body
                return ChildEntry(
                    child_id=child_id,
                    title=title,
                    status="stale_body_only",
                    existing_issue_number=issue_number,
                    action="reuse_and_update_parent",
                    dedupe_key=dedupe_key,
                )
            else:
                # Normal case: body references a known open issue
                return ChildEntry(
                    child_id=child_id,
                    title=title,
                    status="existing",
                    existing_issue_number=issue_number,
                    action="no_op",
                    dedupe_key=dedupe_key,
                )
        else:
            # Issue ref present but not found in open issues
            # Could be closed or non-existent — ambiguous
            return ChildEntry(
                child_id=child_id,
                title=title,
                status="ambiguous",
                existing_issue_number=issue_number,
                action="human_escalation",
                dedupe_key=dedupe_key,
            )

    # No placeholder, no issue ref → treat as missing (bare child description)
    return ChildEntry(
        child_id=child_id,
        title=title,
        status="missing",
        existing_issue_number=None,
        action="create_issue",
        dedupe_key=dedupe_key,
    )


# ---------------------------------------------------------------------------
# Plan assembly
# ---------------------------------------------------------------------------

def build_plan(
    body: str,
    parent_issue: int,
    open_issues: list[dict],
) -> Plan:
    """Build a CHILD_MATERIALIZATION_PLAN_V1 from a parent issue body."""
    parent_mode = _extract_parent_mode(body)
    plan = Plan(parent_issue=parent_issue, parent_mode=parent_mode)

    parsed_children = _parse_child_lines(body)

    if not parsed_children:
        plan.warnings.append(
            "No child lines (Cxxx-N pattern) found in the parent issue body. "
            "Verify the body contains delivery-rollup child references."
        )

    for parsed in parsed_children:
        entry = _classify_child(parsed, open_issues, parent_issue=parent_issue)
        plan.children.append(entry)

        # Collect required_issue_creations
        if entry.action == "create_issue":
            plan.required_issue_creations.append(entry.child_id)

        # Collect required_issue_edits (parent body update needed)
        if entry.action == "reuse_and_update_parent":
            plan.required_issue_edits.append(
                f"#{parent_issue}: replace stale placeholder for {entry.child_id}"
            )

        # Build parent_body_updates for stale entries
        if entry.status == "stale_body_only" and entry.existing_issue_number:
            # Find the original line to replace
            original_rest = parsed["rest"]
            updated_rest = _PLACEHOLDER_RE.sub("", original_rest).strip()
            if f"#{entry.existing_issue_number}" not in updated_rest:
                updated_rest = f"{updated_rest} #{entry.existing_issue_number}".strip()
            plan.parent_body_updates.append(
                ParentBodyUpdate(
                    replace=f"{entry.child_id} {original_rest}",
                    with_=f"{entry.child_id} {updated_rest}",
                )
            )

    return plan


# ---------------------------------------------------------------------------
# YAML serialization (stdlib only — no PyYAML dependency required)
# ---------------------------------------------------------------------------

def _quote_yaml_str(s: str) -> str:
    """Return a safely double-quoted YAML string."""
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def plan_to_yaml(plan: Plan) -> str:
    """Serialize Plan to YAML string (CHILD_MATERIALIZATION_PLAN_V1 format)."""
    lines: list[str] = ["CHILD_MATERIALIZATION_PLAN_V1:"]
    lines.append(f"  parent_issue: {plan.parent_issue}")
    lines.append(f"  parent_mode: {plan.parent_mode}")
    lines.append("  children:")

    if not plan.children:
        lines.append("    []")
    else:
        for child in plan.children:
            lines.append(f"    - child_id: {child.child_id}")
            lines.append(f"      title: {_quote_yaml_str(child.title)}")
            lines.append(f"      status: {child.status}")
            if child.existing_issue_number is None:
                lines.append("      existing_issue_number: null")
            else:
                lines.append(f"      existing_issue_number: {child.existing_issue_number}")
            lines.append(f"      action: {child.action}")
            lines.append(f"      dedupe_key: {_quote_yaml_str(child.dedupe_key)}")

    lines.append("  parent_body_updates:")
    if not plan.parent_body_updates:
        lines.append("    []")
    else:
        for upd in plan.parent_body_updates:
            lines.append(f"    - replace: {_quote_yaml_str(upd.replace)}")
            lines.append(f"      with: {_quote_yaml_str(upd.with_)}")

    lines.append("  required_issue_creations:")
    if not plan.required_issue_creations:
        lines.append("    []")
    else:
        for item in plan.required_issue_creations:
            lines.append(f"    - {item}")

    lines.append("  required_issue_edits:")
    if not plan.required_issue_edits:
        lines.append("    []")
    else:
        for item in plan.required_issue_edits:
            lines.append(f"    - {_quote_yaml_str(item)}")

    lines.append("  warnings:")
    if not plan.warnings:
        lines.append("    []")
    else:
        for w in plan.warnings:
            lines.append(f"    - {_quote_yaml_str(w)}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate CHILD_MATERIALIZATION_PLAN_V1 from a delivery-rollup parent issue."
    )
    parser.add_argument(
        "--issue",
        type=int,
        required=True,
        help="Parent issue number (e.g. 254)",
    )
    parser.add_argument(
        "--repo",
        default="",
        help="owner/repo (required when fetching from GitHub)",
    )
    parser.add_argument(
        "--body-file",
        default="",
        help="Path to a local body fixture file (skips GitHub API call)",
    )
    parser.add_argument(
        "--gh",
        default="gh",
        help="Path to gh CLI binary (default: gh)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 1. Acquire body text
    if args.body_file:
        p = Path(args.body_file)
        if not p.is_file():
            sys.stderr.write(f"[ERROR] body-file not found: {args.body_file}\n")
            return 1
        body = p.read_text(encoding="utf-8")
        open_issues: list[dict] = []
    else:
        if not args.repo:
            sys.stderr.write("[ERROR] --repo is required when --body-file is not provided\n")
            return 1
        try:
            body = _fetch_issue_body(args.repo, args.issue, args.gh)
        except RuntimeError as exc:
            sys.stderr.write(f"[ERROR] {exc}\n")
            return 1
        open_issues = _fetch_open_issues(args.repo, args.gh)

    # 2. Build plan
    plan = build_plan(body, parent_issue=args.issue, open_issues=open_issues)

    # 3. Output YAML
    sys.stdout.write(plan_to_yaml(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
