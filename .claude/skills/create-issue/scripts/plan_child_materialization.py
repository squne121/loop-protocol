#!/usr/bin/env python3
"""plan_child_materialization.py — Read-only CHILD_MATERIALIZATION_PLAN_V2 generator.

Reads a delivery-rollup parent issue body (via GitHub CLI or a local fixture file)
and produces a CHILD_MATERIALIZATION_PLAN_V2 YAML plan on stdout.

This script is read-only: it never mutates GitHub Issues.
Mutation is delegated to create_issue_txn.py and the edit-issue skill.

Usage:
    # From a live GitHub issue:
    uv run python3 plan_child_materialization.py --repo owner/repo --issue 254

    # From a local body fixture (for tests / dry-run):
    uv run python3 plan_child_materialization.py --body-file fixture.md --issue 254

    NOTE (dry-run / --body-file limitation):
        When --body-file is used, issue refs found in the body are classified as
        'existing_unverified' instead of 'existing_open' or 'existing_closed'.
        The script does NOT call the GitHub API in dry-run mode.
        Do not rely on status=existing_open/existing_closed for --body-file runs.

Exit codes:
    0  — plan generated successfully (may contain warnings)
    1  — fatal error (bad args, GitHub API failure, unrecoverable parse error)
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ChildStatus = Literal[
    "missing",
    "existing_open",
    "existing_closed",
    "existing_unverified",
    "stale_body_only",
    "ambiguous",
]
ChildAction = Literal["create_issue", "reuse_and_update_parent", "no_op", "human_escalation"]


@dataclass
class ExistingIssueInfo:
    number: int
    state: str  # "OPEN" | "CLOSED"
    state_reason: Optional[str]  # "COMPLETED" | "NOT_PLANNED" | None
    url: str


@dataclass
class ChildEntry:
    child_id: str
    title: str
    status: ChildStatus
    existing_issue: Optional[ExistingIssueInfo]
    action: ChildAction
    dedupe_key: str
    existing_issue_candidates: list[dict] = field(default_factory=list)


@dataclass
class ParentBodyUpdate:
    section: str
    line_number: int  # 1-based line number in the parent body
    old_line: str
    new_line: str
    expected_match_count: int = 1


@dataclass
class IssueLookup:
    strategy: str
    complete: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class Plan:
    schema_version: int
    repo: str
    generated_at: str
    source_issue_number: int
    body_sha256: str
    parent_issue: int
    parent_mode: str
    closure_mode: str
    issue_lookup: IssueLookup
    children: list[ChildEntry] = field(default_factory=list)
    parent_body_updates: list[ParentBodyUpdate] = field(default_factory=list)
    required_issue_creations: list[str] = field(default_factory=list)
    required_issue_edits: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Matches a child list line in either of:
#   - [ ] #N — C254-M: ...
#   - C254-M ... （未起票）  (bullet form)
#   - C254-M ... #N
_CHILD_LINE_RE = re.compile(
    r"^[-*]?\s*"
    r"(?:\[[ xX]?\]\s*)?"   # optional checkbox like "- [ ]" or "- [x]"
    r"(?:#\d+\s*[—–—–-]\s*)?"  # optional leading "#N —" issue reference form
    r"(?P<child_id>C\d+-\d+)"
    r"[:\s]+"               # separator: space(s) or colon+space(s) after child_id
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

# Matches the closure_mode in the Machine-Readable Contract block
_CLOSURE_MODE_RE = re.compile(
    r"(?im)^[ \t]*closure_mode\s*:\s*[\"']?(?P<mode>[\w-]+)[\"']?\s*$"
)


def _extract_parent_mode(body: str) -> str:
    """Extract parent_mode from the Machine-Readable Contract block.

    Returns 'unknown' when not found (rather than assuming delivery-rollup,
    which would auto-process issues with broken Machine-Readable Contracts).
    """
    m = _PARENT_MODE_RE.search(body)
    if m:
        return m.group("mode")
    return "unknown"


def _extract_closure_mode(body: str) -> str:
    """Extract closure_mode from the Machine-Readable Contract block.

    Returns 'unknown' when not found.
    """
    m = _CLOSURE_MODE_RE.search(body)
    if m:
        return m.group("mode")
    return "unknown"


def _extract_child_issues_section(body: str) -> list[tuple[int, str]]:
    """Extract lines from the '## Child Issues' section only.

    Returns a list of (1-based-line-number, line) tuples for lines
    within the section, stopping at the next heading.
    """
    in_section = False
    results: list[tuple[int, str]] = []
    for i, line in enumerate(body.splitlines(), start=1):
        stripped = line.strip()
        if stripped == "## Child Issues":
            in_section = True
            continue
        if in_section:
            # Stop at any other heading (## or deeper)
            if re.match(r"^#{1,6}\s", stripped):
                break
            results.append((i, line))
    return results


def _parse_child_lines(body: str) -> list[dict]:
    """Parse Cxxx-N child lines from the '## Child Issues' section only.

    Returns a list of raw dicts with keys:
        child_id, title_fragment, rest, is_placeholder, raw_issue_refs,
        line_number (1-based), raw_line
    """
    section_lines = _extract_child_issues_section(body)
    results = []
    for line_number, line in section_lines:
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
                "line_number": line_number,
                "raw_line": line,
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


def _view_issue(repo: str, issue_number: int, gh_bin: str = "gh") -> Optional[ExistingIssueInfo]:
    """Fetch a single issue's number, title, state, stateReason, url via gh issue view.

    Returns None and records warning if the call fails (issue is truly ambiguous in that case).
    """
    args = [
        gh_bin,
        "issue",
        "view",
        str(issue_number),
        "--repo",
        repo,
        "--json",
        "number,title,state,stateReason,url",
    ]
    cp = subprocess.run(args, capture_output=True, text=True)
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout.strip())
        return ExistingIssueInfo(
            number=data["number"],
            state=data.get("state", "UNKNOWN"),
            state_reason=data.get("stateReason"),
            url=data.get("url", ""),
        )
    except (json.JSONDecodeError, KeyError):
        return None


def _search_dedupe_candidates(
    repo: str, dedupe_key: str, gh_bin: str = "gh"
) -> list[dict]:
    """Search for existing issues matching a dedupe_key in all states.

    Returns a list of candidate dicts (may be empty).
    """
    args = [
        gh_bin,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--search",
        dedupe_key,
        "--json",
        "number,title,state,url",
        "--limit",
        "10",
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
    *,
    parent_issue: int,
    parent_mode: str,
    repo: str,
    gh_bin: str = "gh",
    dry_run: bool = False,
    issue_lookup_warnings: list[str],
) -> ChildEntry:
    """Classify a single parsed child line into a ChildEntry.

    Parameters
    ----------
    parsed:
        Raw dict from _parse_child_lines.
    parent_issue:
        Parent issue number (used for dedupe_key).
    parent_mode:
        Parent mode string ('delivery-rollup', 'unknown', etc.).
    repo:
        owner/repo string. Empty string in dry-run mode.
    gh_bin:
        Path to gh CLI binary.
    dry_run:
        When True, no GitHub API calls are made.
    issue_lookup_warnings:
        Mutable list to append lookup warnings to.
    """
    child_id: str = parsed["child_id"]
    rest: str = parsed["rest"]
    is_placeholder: bool = parsed["is_placeholder"]
    raw_issue_refs: list[int] = parsed["raw_issue_refs"]

    # parent_mode unknown → human_escalation for this child
    if parent_mode == "unknown":
        dedupe_key = f"unknown:{parent_issue}:{child_id}"
        title = _build_title(rest)
        return ChildEntry(
            child_id=child_id,
            title=title,
            status="ambiguous",
            existing_issue=None,
            action="human_escalation",
            dedupe_key=dedupe_key,
            existing_issue_candidates=[],
        )

    dedupe_key = f"{parent_mode}:{parent_issue}:{child_id}"

    # Strip trailing issue references and placeholder text to get a clean title
    title = _build_title(rest)

    if is_placeholder and not raw_issue_refs:
        # Body says "(未起票)" with no issue ref → missing
        return ChildEntry(
            child_id=child_id,
            title=title,
            status="missing",
            existing_issue=None,
            action="create_issue",
            dedupe_key=dedupe_key,
            existing_issue_candidates=[],
        )

    if raw_issue_refs:
        issue_number = raw_issue_refs[0]

        if dry_run:
            # In dry-run mode, we cannot verify issue state
            status: ChildStatus = "existing_unverified"
            if is_placeholder:
                status = "stale_body_only"
            return ChildEntry(
                child_id=child_id,
                title=title,
                status=status,
                existing_issue=ExistingIssueInfo(
                    number=issue_number,
                    state="UNKNOWN",
                    state_reason=None,
                    url="",
                ),
                action="no_op" if not is_placeholder else "reuse_and_update_parent",
                dedupe_key=dedupe_key,
                existing_issue_candidates=[],
            )

        # Verify issue state via individual gh issue view
        info = _view_issue(repo, issue_number, gh_bin)
        if info is None:
            # API failure for this individual issue → ambiguous (not a plan-level failure)
            issue_lookup_warnings.append(
                f"gh issue view #{issue_number} failed — cannot determine state. "
                f"Classified as ambiguous."
            )
            return ChildEntry(
                child_id=child_id,
                title=title,
                status="ambiguous",
                existing_issue=None,
                action="human_escalation",
                dedupe_key=dedupe_key,
                existing_issue_candidates=[],
            )

        is_open = info.state.upper() == "OPEN"

        if is_placeholder:
            if is_open:
                # Body says "(未起票)" + issue ref + issue is open → stale body
                return ChildEntry(
                    child_id=child_id,
                    title=title,
                    status="stale_body_only",
                    existing_issue=info,
                    action="reuse_and_update_parent",
                    dedupe_key=dedupe_key,
                    existing_issue_candidates=[],
                )
            else:
                # placeholder + closed ref → stale + closed, treat as human_escalation
                return ChildEntry(
                    child_id=child_id,
                    title=title,
                    status="stale_body_only",
                    existing_issue=info,
                    action="human_escalation",
                    dedupe_key=dedupe_key,
                    existing_issue_candidates=[],
                )
        else:
            if is_open:
                # Normal case: body references a known open issue
                candidates = _search_dedupe_candidates(repo, dedupe_key, gh_bin)
                return ChildEntry(
                    child_id=child_id,
                    title=title,
                    status="existing_open",
                    existing_issue=info,
                    action="no_op",
                    dedupe_key=dedupe_key,
                    existing_issue_candidates=candidates,
                )
            else:
                # closed child — in child-complete mode this is a NORMAL success state
                candidates = _search_dedupe_candidates(repo, dedupe_key, gh_bin)
                return ChildEntry(
                    child_id=child_id,
                    title=title,
                    status="existing_closed",
                    existing_issue=info,
                    action="no_op",
                    dedupe_key=dedupe_key,
                    existing_issue_candidates=candidates,
                )

    # No placeholder, no issue ref → treat as missing (bare child description)
    return ChildEntry(
        child_id=child_id,
        title=title,
        status="missing",
        existing_issue=None,
        action="create_issue",
        dedupe_key=dedupe_key,
        existing_issue_candidates=[],
    )


def _build_title(rest: str) -> str:
    """Build a clean title string from a raw 'rest' field."""
    title = _PLACEHOLDER_RE.sub("", rest)
    # Remove trailing issue refs like "#281"
    title = re.sub(r"\s*#\d+\s*$", "", title).strip()
    # Remove leading "—" or "–" (em/en dash) when child_id was already stripped
    title = re.sub(r"^[—–-]\s*", "", title).strip()
    # Remove trailing colon/dash artefacts
    title = title.rstrip("-:").strip()
    return title


# ---------------------------------------------------------------------------
# Plan assembly
# ---------------------------------------------------------------------------

def build_plan(
    body: str,
    parent_issue: int,
    repo: str = "",
    gh_bin: str = "gh",
    dry_run: bool = False,
) -> Plan:
    """Build a CHILD_MATERIALIZATION_PLAN_V2 from a parent issue body.

    Parameters
    ----------
    body:
        Parent issue body text.
    parent_issue:
        Parent issue number.
    repo:
        owner/repo string. Required for live mode; empty string for dry-run.
    gh_bin:
        Path to gh CLI binary.
    dry_run:
        When True, no GitHub API calls are made (--body-file mode).
    """
    parent_mode = _extract_parent_mode(body)
    closure_mode = _extract_closure_mode(body)
    body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    issue_lookup_warnings: list[str] = []
    issue_lookup = IssueLookup(
        strategy="referenced_issue_view_and_dedupe_search_all_states",
        complete=True,
        warnings=issue_lookup_warnings,
    )

    if dry_run:
        issue_lookup.strategy = "body_file_dry_run_no_api_calls"

    plan = Plan(
        schema_version=2,
        repo=repo,
        generated_at=generated_at,
        source_issue_number=parent_issue,
        body_sha256=body_sha256,
        parent_issue=parent_issue,
        parent_mode=parent_mode,
        closure_mode=closure_mode,
        issue_lookup=issue_lookup,
    )

    parsed_children = _parse_child_lines(body)

    if not parsed_children:
        plan.warnings.append(
            "No child lines (Cxxx-N pattern) found in the '## Child Issues' section. "
            "Verify the body contains delivery-rollup child references under that heading."
        )

    for parsed in parsed_children:
        entry = _classify_child(
            parsed,
            parent_issue=parent_issue,
            parent_mode=parent_mode,
            repo=repo,
            gh_bin=gh_bin,
            dry_run=dry_run,
            issue_lookup_warnings=issue_lookup_warnings,
        )
        plan.children.append(entry)

        # Collect required_issue_creations
        if entry.action == "create_issue":
            plan.required_issue_creations.append(entry.child_id)

        # Collect required_issue_edits (parent body update needed)
        if entry.action == "reuse_and_update_parent":
            plan.required_issue_edits.append(
                f"#{parent_issue}: replace stale placeholder for {entry.child_id}"
            )

        # Build parent_body_updates for stale entries (open only)
        if (
            entry.status == "stale_body_only"
            and entry.existing_issue is not None
            and entry.existing_issue.state.upper() == "OPEN"
        ):
            raw_line: str = parsed["raw_line"]
            line_number: int = parsed["line_number"]
            issue_num = entry.existing_issue.number
            # Build new_line by removing placeholder and ensuring issue ref
            new_rest = _PLACEHOLDER_RE.sub("", parsed["rest"]).strip()
            if f"#{issue_num}" not in new_rest:
                new_rest = f"{new_rest} #{issue_num}".strip()
            # Preserve the original line prefix (bullet, checkbox, etc.)
            prefix_m = re.match(r"^([-*]?\s*(?:\[[ xX]?\]\s*)?)", raw_line)
            prefix = prefix_m.group(1) if prefix_m else ""
            new_line = f"{prefix}{entry.child_id} {new_rest}"

            # Count occurrences to detect duplicate matches
            expected = body.count(raw_line.rstrip())
            plan.parent_body_updates.append(
                ParentBodyUpdate(
                    section="Child Issues",
                    line_number=line_number,
                    old_line=raw_line.rstrip(),
                    new_line=new_line,
                    expected_match_count=expected,
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
    """Serialize Plan to YAML string (CHILD_MATERIALIZATION_PLAN_V2 format)."""
    lines: list[str] = ["CHILD_MATERIALIZATION_PLAN_V2:"]
    lines.append(f"  schema_version: {plan.schema_version}")
    lines.append(f"  repo: {_quote_yaml_str(plan.repo)}")
    lines.append(f"  generated_at: {_quote_yaml_str(plan.generated_at)}")
    lines.append("  source:")
    lines.append("    kind: parent_issue_body")
    lines.append(f"    issue_number: {plan.source_issue_number}")
    lines.append(f"    body_sha256: {_quote_yaml_str(plan.body_sha256)}")
    lines.append("  parent:")
    lines.append(f"    issue_number: {plan.parent_issue}")
    lines.append(f"    parent_mode: {plan.parent_mode}")
    lines.append(f"    closure_mode: {plan.closure_mode}")
    lines.append("  issue_lookup:")
    lines.append(f"    strategy: {_quote_yaml_str(plan.issue_lookup.strategy)}")
    lines.append(f"    complete: {'true' if plan.issue_lookup.complete else 'false'}")
    lines.append("    warnings:")
    if not plan.issue_lookup.warnings:
        lines.append("      []")
    else:
        for w in plan.issue_lookup.warnings:
            lines.append(f"      - {_quote_yaml_str(w)}")
    lines.append("  children:")

    if not plan.children:
        lines.append("    []")
    else:
        for child in plan.children:
            lines.append(f"    - child_id: {child.child_id}")
            lines.append(f"      title: {_quote_yaml_str(child.title)}")
            lines.append(f"      status: {child.status}")
            if child.existing_issue is None:
                lines.append("      existing_issue: null")
            else:
                lines.append("      existing_issue:")
                lines.append(f"        number: {child.existing_issue.number}")
                lines.append(f"        state: {child.existing_issue.state}")
                sr = child.existing_issue.state_reason
                lines.append(f"        state_reason: {'null' if sr is None else sr}")
                lines.append(f"        url: {_quote_yaml_str(child.existing_issue.url)}")
            lines.append(f"      action: {child.action}")
            lines.append(f"      dedupe_key: {_quote_yaml_str(child.dedupe_key)}")
            lines.append("      existing_issue_candidates:")
            if not child.existing_issue_candidates:
                lines.append("        []")
            else:
                for c in child.existing_issue_candidates:
                    lines.append(f"        - number: {c.get('number', '')}")
                    lines.append(f"          title: {_quote_yaml_str(str(c.get('title', '')))}")
                    lines.append(f"          state: {c.get('state', '')}")
                    lines.append(f"          url: {_quote_yaml_str(str(c.get('url', '')))}")

    lines.append("  parent_body_updates:")
    if not plan.parent_body_updates:
        lines.append("    []")
    else:
        for upd in plan.parent_body_updates:
            lines.append(f"    - section: {_quote_yaml_str(upd.section)}")
            lines.append(f"      line_number: {upd.line_number}")
            lines.append(f"      old_line: {_quote_yaml_str(upd.old_line)}")
            lines.append(f"      new_line: {_quote_yaml_str(upd.new_line)}")
            lines.append(f"      expected_match_count: {upd.expected_match_count}")

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
        description="Generate CHILD_MATERIALIZATION_PLAN_V2 from a delivery-rollup parent issue."
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
        help=(
            "Path to a local body fixture file (skips GitHub API call). "
            "Issue refs will be classified as 'existing_unverified'."
        ),
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
    dry_run = False
    if args.body_file:
        p = Path(args.body_file)
        if not p.is_file():
            sys.stderr.write(f"[ERROR] body-file not found: {args.body_file}\n")
            return 1
        body = p.read_text(encoding="utf-8")
        dry_run = True
        repo = args.repo  # may be empty in dry-run mode
    else:
        if not args.repo:
            sys.stderr.write("[ERROR] --repo is required when --body-file is not provided\n")
            return 1
        try:
            body = _fetch_issue_body(args.repo, args.issue, args.gh)
        except RuntimeError as exc:
            sys.stderr.write(f"[ERROR] {exc}\n")
            return 1
        repo = args.repo

    # 2. Build plan
    plan = build_plan(
        body,
        parent_issue=args.issue,
        repo=repo,
        gh_bin=args.gh,
        dry_run=dry_run,
    )

    # 3. Check if lookup was complete (non-dry-run only)
    if not dry_run and not plan.issue_lookup.complete:
        sys.stderr.write(
            "[WARN] issue_lookup.complete=false — plan is non-actionable. "
            "Consumer skills must NOT mutate GitHub Issues based on this plan.\n"
        )

    # 4. Output YAML
    sys.stdout.write(plan_to_yaml(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
