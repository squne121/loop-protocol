#!/usr/bin/env python3
"""
milestone_rollup.py — M1 descendant rollup checker
Outputs MILESTONE_DESCENDANT_ROLLUP_V1 schema to stdout.

Usage:
    uv run python3 scripts/milestone_rollup.py <milestone_number> [--format json|markdown] [--strict]

SSOT: docs/dev/milestone-ops.md
Schema: MILESTONE_DESCENDANT_ROLLUP_V1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import deque
from typing import Any

# Compiled regex for Link header "next" relation
NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def md_cell(value: Any) -> str:
    """Escape a value for safe embedding in a Markdown table cell."""
    s = str(value).replace("|", "\\|").replace("\n", "<br>").replace("`", "\\`")
    return s


def _build_headers(token: str | None) -> dict[str, str]:
    """Build GitHub API request headers. token is optional (rate-limit purposes)."""
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _api_get(url: str, token: str | None) -> Any:
    """Perform a single GET request to the GitHub API. Returns parsed JSON."""
    headers = _build_headers(token)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url}: {exc.read().decode()}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error for {url}: {exc.reason}") from exc


def _parse_next_link(link_header: str) -> str | None:
    """Extract the 'next' URL from a GitHub Link header using regex."""
    if not link_header:
        return None
    m = NEXT_LINK_RE.search(link_header)
    return m.group(1) if m else None


def _api_get_paginated(base_url: str, token: str | None) -> list[Any]:
    """Collect all pages of a paginated GitHub API endpoint.

    Uses /repos/{owner}/{repo}/issues?milestone=...&state=all&per_page=100
    (official docs endpoint).
    """
    results: list[Any] = []
    url: str | None = base_url
    while url:
        headers = _build_headers(token)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                page = json.loads(resp.read().decode("utf-8"))
                if isinstance(page, list):
                    results.extend(page)
                else:
                    results.append(page)
                link_header = resp.headers.get("Link", "")
                url = _parse_next_link(link_header)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"HTTP {exc.code} for {url}: {exc.read().decode()}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Network error for {url}: {exc.reason}") from exc
    return results


def _get_sub_issues_paginated(
    owner: str, repo: str, issue_number: int, token: str | None
) -> tuple[list[Any], list[dict[str, Any]], bool]:
    """Get sub-issues for an issue with pagination.

    Returns (children_list, warnings_list, partial).
    - 200 empty -> children = [], partial=False (normal, no children)
    - 404/410 -> warning sub_issues_unavailable, children = [], partial=False
    - 422 -> warning sub_issues_error, children = [], partial=True (degraded mode)
    - other HTTP errors -> raise RuntimeError
    """
    results: list[Any] = []
    warnings: list[dict[str, Any]] = []
    url: str | None = (
        f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/sub_issues?per_page=100"
    )
    while url:
        headers = _build_headers(token)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                page = json.loads(resp.read().decode("utf-8"))
                if isinstance(page, list):
                    results.extend(page)
                link_header = resp.headers.get("Link", "")
                url = _parse_next_link(link_header)
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code in (404, 410):
                warnings.append(
                    {
                        "type": "sub_issues_unavailable",
                        "issue_number": issue_number,
                        "http_code": code,
                        "message": "sub_issues endpoint not available for this issue",
                    }
                )
                return [], warnings, False
            if code == 422:
                warnings.append(
                    {
                        "type": "sub_issues_error",
                        "issue_number": issue_number,
                        "http_code": code,
                        "message": "sub_issues endpoint returned 422 (unprocessable entity)",
                    }
                )
                return [], warnings, True
            raise RuntimeError(
                f"HTTP {code} fetching sub_issues for issue #{issue_number}: {exc.read().decode()}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Network error for sub_issues #{issue_number}: {exc.reason}"
            ) from exc
    return results, warnings, False


def _get_native_dependencies(
    owner: str, repo: str, issue_number: int, token: str | None
) -> tuple[list[int] | None, str]:
    """Fetch dependencies via the native GitHub dependency API.

    Returns (dep_numbers_or_None, source_string).
    - If native API returns 200: returns (list_of_numbers, "native")
    - If native API returns 404/410/501 (endpoint unavailable): returns (None, "fallback_trigger")
    - 200 empty array is "no deps", returns ([], "native") — no fallback
    - 403/429/network/parse errors -> raise RuntimeError (do not silently fallback)
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/dependencies/blocked_by?per_page=100"
    try:
        pages = _api_get_paginated(url, token)
        numbers: list[int] = []
        for dep in pages:
            n = dep.get("number")
            if isinstance(n, int):
                numbers.append(n)
        return numbers, "native"
    except RuntimeError as exc:
        msg = str(exc)
        # Only fallback for 404/410/501 (endpoint not available)
        if any(f"HTTP {code}" in msg for code in ("404", "410", "501")):
            return None, "fallback_trigger"
        # 403/429/network/parse errors: propagate as error (do not silently fallback)
        raise


def _parse_depends_on(body: str | None) -> list[int]:
    """Extract issue numbers from '## Depends On' section in issue body."""
    if not body:
        return []
    in_section = False
    numbers: list[int] = []
    for line in body.splitlines():
        if re.match(r"^##\s+Depends On\s*$", line):
            in_section = True
            continue
        if in_section:
            if re.match(r"^##\s+", line):
                break
            for match in re.finditer(r"#(\d+)", line):
                numbers.append(int(match.group(1)))
    return numbers


def _get_dependencies_with_source(
    owner: str, repo: str, issue_number: int, token: str | None, body: str
) -> tuple[list[int], str]:
    """Get dependency issue numbers, preferring native API with fallback to body parsing.

    Returns (dep_numbers, source) where source is "native" or "depends_on_section".
    """
    dep_numbers, source = _get_native_dependencies(owner, repo, issue_number, token)
    if dep_numbers is not None:
        # Native API returned successfully (including empty list = no deps)
        return dep_numbers, "native"
    # Fallback: parse ## Depends On section
    return _parse_depends_on(body), "depends_on_section"


def collect_descendants(
    owner: str,
    repo: str,
    milestone_number: int,
    token: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """BFS traversal of milestone direct issues + sub-issues.

    Returns (all_issues, warnings_list, partial).
    Each issue dict has keys: number, title, state, milestone, labels, body,
                              is_pr, depth, parent_number.
    visited set: (owner, repo, number) to prevent cycles and cross-repo duplicates.
    partial=True if any 422 was encountered during sub_issues traversal.

    Uses official milestone issues endpoint:
      GET /repos/{owner}/{repo}/issues?milestone={n}&state=all&per_page=100
    PRs are identified by presence of the `pull_request` field and placed in pr_mixed.
    """
    # Blocker 2: use official milestone issues endpoint (not /milestones/{n}/issues)
    milestone_url = (
        f"https://api.github.com/repos/{owner}/{repo}/issues"
        f"?milestone={milestone_number}&state=all&per_page=100"
    )
    direct_items = _api_get_paginated(milestone_url, token)

    visited: set[tuple[str, str, int]] = set()
    queue: deque[tuple[Any, int, int | None]] = deque()
    all_issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    any_partial: bool = False

    for item in direct_items:
        key = (owner, repo, item["number"])
        if key not in visited:
            visited.add(key)
            queue.append((item, 0, None))

    while queue:
        item, depth, parent_number = queue.popleft()
        number = item["number"]
        is_pr = "pull_request" in item and item["pull_request"] is not None
        m = item.get("milestone")
        labels = [lbl["name"] for lbl in item.get("labels", [])]
        body = item.get("body") or ""

        all_issues.append(
            {
                "number": number,
                "title": item.get("title", ""),
                "state": item.get("state", ""),
                "milestone": m,
                "milestone_number": m["number"] if m else None,
                "labels": labels,
                "body": body,
                "is_pr": is_pr,
                "depth": depth,
                "parent_number": parent_number,
            }
        )

        if not is_pr:
            children, sub_warnings, sub_partial = _get_sub_issues_paginated(owner, repo, number, token)
            warnings.extend(sub_warnings)
            if sub_partial:
                any_partial = True
            for child in children:
                # Cross-repo guard: exact match (not startswith) to prevent owner/repo-evil bypass
                child_repo_url = child.get("repository_url", "")
                expected = f"https://api.github.com/repos/{owner}/{repo}"
                if child_repo_url and child_repo_url.rstrip("/") != expected:
                    warnings.append(
                        {
                            "type": "cross_repo_sub_issue",
                            "parent_number": number,
                            "child_number": child["number"],
                            "child_repo_url": child_repo_url,
                        }
                    )
                    continue

                child_key = (owner, repo, child["number"])
                if child_key in visited:
                    warnings.append(
                        {
                            "type": "cycle_or_duplicate",
                            "parent_number": number,
                            "child_number": child["number"],
                        }
                    )
                    continue

                visited.add(child_key)
                queue.append((child, depth + 1, number))

    return all_issues, warnings, any_partial


def analyze(
    all_issues: list[dict[str, Any]],
    milestone_number: int,
    owner: str,
    repo: str,
    token: str | None,
) -> dict[str, Any]:
    """Produce MILESTONE_DESCENDANT_ROLLUP_V1 findings from collected issues."""
    pr_mixed: list[dict[str, Any]] = []
    milestone_mismatches: list[dict[str, Any]] = []
    stale_state_labels: list[dict[str, Any]] = []
    open_blockers: list[dict[str, Any]] = []

    for issue in all_issues:
        number = issue["number"]
        is_pr = issue["is_pr"]
        state = issue["state"]
        m_num = issue["milestone_number"]
        labels = issue["labels"]

        # AC6: PR direct milestone attachment
        if is_pr:
            pr_mixed.append(
                {
                    "number": number,
                    "title": issue["title"],
                    "state": state,
                    "depth": issue["depth"],
                }
            )
            continue  # PRs don't need further checks

        # AC3/AC5: milestone null or mismatch
        # AC5: depth >= 1 (descendant) with milestone=null is NORMAL (indirect member).
        # Only depth 0 (direct items) require the target milestone.
        # depth >= 1 with an explicit different milestone is still a mismatch (scope conflict).
        depth = issue["depth"]
        if depth == 0:
            # Direct item: must have the target milestone
            if m_num is None or m_num != milestone_number:
                milestone_mismatches.append(
                    {
                        "number": number,
                        "title": issue["title"],
                        "state": state,
                        "milestone_number": m_num,
                        "depth": depth,
                        "parent_number": issue["parent_number"],
                    }
                )
        else:
            # Descendant (depth >= 1):
            # milestone=null → indirect member, normal per AC5 (do not flag as mismatch)
            # milestone=other → scope conflict (still flagged as mismatch for human review)
            if m_num is not None and m_num != milestone_number:
                milestone_mismatches.append(
                    {
                        "number": number,
                        "title": issue["title"],
                        "state": state,
                        "milestone_number": m_num,
                        "depth": depth,
                        "parent_number": issue["parent_number"],
                    }
                )

        # AC4: closed issue with stale state labels
        if state == "closed":
            stale = [lbl for lbl in labels if lbl in ("state/queued", "state/in-progress")]
            if stale:
                stale_state_labels.append(
                    {
                        "number": number,
                        "title": issue["title"],
                        "stale_labels": stale,
                        "depth": issue["depth"],
                    }
                )

        # AC5: open issue with open blockers (native API preferred, fallback to body)
        if state == "open":
            body = issue.get("body", "") or ""
            dep_numbers, dep_source = _get_dependencies_with_source(
                owner, repo, number, token, body
            )

            open_dep_numbers: list[int] = []
            for dep_n in dep_numbers:
                try:
                    dep_issue = _api_get(
                        f"https://api.github.com/repos/{owner}/{repo}/issues/{dep_n}",
                        token,
                    )
                    if dep_issue.get("state") == "open":
                        open_dep_numbers.append(dep_n)
                except RuntimeError:
                    # If we can't fetch, conservatively note it as a potential blocker
                    open_dep_numbers.append(dep_n)

            if open_dep_numbers:
                open_blockers.append(
                    {
                        "number": number,
                        "title": issue["title"],
                        "open_blocker_numbers": open_dep_numbers,
                        "depth": issue["depth"],
                        "source": dep_source,
                    }
                )

    return {
        "pr_mixed": pr_mixed,
        "milestone_mismatches": milestone_mismatches,
        "stale_state_labels": stale_state_labels,
        "open_blockers": open_blockers,
    }


def build_report(
    milestone_number: int,
    all_issues: list[dict[str, Any]],
    findings: dict[str, Any],
    warnings: list[dict[str, Any]],
    generated_at: str,
    repo: str,
    partial: bool = False,
) -> dict[str, Any]:
    """Build the MILESTONE_DESCENDANT_ROLLUP_V1 report structure."""
    issues_only = [i for i in all_issues if not i["is_pr"]]
    open_count = sum(1 for i in issues_only if i["state"] == "open")
    closed_count = sum(1 for i in issues_only if i["state"] == "closed")

    has_invariant_violation = len(findings["pr_mixed"]) > 0

    return {
        "schema": "MILESTONE_DESCENDANT_ROLLUP_V1",
        "generated_at": generated_at,
        "repo": repo,
        "milestone_number": milestone_number,
        "partial": partial,
        "summary": {
            "total_descendants": len(all_issues),
            "open_issues": open_count,
            "closed_issues": closed_count,
            "pr_mixed_count": len(findings["pr_mixed"]),
            "milestone_mismatch_count": len(findings["milestone_mismatches"]),
            "stale_state_label_count": len(findings["stale_state_labels"]),
            "open_blocker_count": len(findings["open_blockers"]),
            "has_invariant_violation": has_invariant_violation,
            "partial": partial,
        },
        "pr_mixed": findings["pr_mixed"],
        "milestone_mismatches": findings["milestone_mismatches"],
        "stale_state_labels": findings["stale_state_labels"],
        "open_blockers": findings["open_blockers"],
        "warnings": warnings,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render MILESTONE_DESCENDANT_ROLLUP_V1 as Markdown."""
    lines: list[str] = []
    s = report["summary"]
    lines.append(f"## Milestone Descendant Rollup: #{report['milestone_number']}")
    lines.append(f"\ngenerated_at: {report['generated_at']}")
    lines.append(f"repo: {report['repo']}")
    lines.append("\n### Summary\n")
    lines.append("| field | value |")
    lines.append("|---|---|")
    for k, v in s.items():
        lines.append(f"| {md_cell(k)} | {md_cell(v)} |")

    def _table_section(title: str, items: list[dict[str, Any]], cols: list[str]) -> None:
        lines.append(f"\n### {title}\n")
        if not items:
            lines.append("(none)")
            return
        lines.append("| " + " | ".join(md_cell(c) for c in cols) + " |")
        lines.append("|" + "|".join(["---"] * len(cols)) + "|")
        for item in items:
            row = [md_cell(item.get(c, "")) for c in cols]
            lines.append("| " + " | ".join(row) + " |")

    _table_section(
        "pr_mixed (invariant violation)",
        report["pr_mixed"],
        ["number", "title", "state", "depth"],
    )
    _table_section(
        "milestone_mismatches",
        report["milestone_mismatches"],
        ["number", "title", "state", "milestone_number", "depth", "parent_number"],
    )
    _table_section(
        "stale_state_labels",
        report["stale_state_labels"],
        ["number", "title", "stale_labels", "depth"],
    )
    _table_section(
        "open_blockers",
        report["open_blockers"],
        ["number", "title", "open_blocker_numbers", "depth", "source"],
    )

    if report["warnings"]:
        lines.append("\n### warnings\n")
        for w in report["warnings"]:
            lines.append(f"- {json.dumps(w)}")

    return "\n".join(lines) + "\n"


def main() -> int:
    import datetime

    parser = argparse.ArgumentParser(
        description="MILESTONE_DESCENDANT_ROLLUP_V1 — M1 descendant rollup checker"
    )
    parser.add_argument(
        "milestone_number",
        nargs="?",
        type=int,
        default=1,
        help="Milestone number (default: 1)",
    )
    parser.add_argument(
        "--milestone",
        type=int,
        dest="milestone_number_opt",
        help="Milestone number (alternative flag)",
    )
    parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="json",
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if invariant violations exist",
    )
    parser.add_argument(
        "--repo",
        default="squne121/loop-protocol",
        help="GitHub repository in owner/repo format",
    )
    args = parser.parse_args()

    milestone_number = args.milestone_number_opt if args.milestone_number_opt else args.milestone_number

    if not isinstance(milestone_number, int) or milestone_number < 1:
        print("ERROR: milestone_number must be a positive integer", file=sys.stderr)
        return 2

    # Blocker 3: token is optional — unauthenticated GET works for public repos
    token: str | None = os.environ.get("GITHUB_TOKEN") or None
    if not token:
        import subprocess
        try:
            result = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
            )
            t = result.stdout.strip()
            token = t if t else None
        except Exception:
            pass

    if not token:
        print(
            "[warn] No GITHUB_TOKEN or gh CLI token found; proceeding unauthenticated "
            "(rate-limited to 60 req/h for public repos)",
            file=sys.stderr,
        )

    repo = args.repo
    parts = repo.split("/")
    if len(parts) != 2:
        print(f"ERROR: --repo must be in owner/repo format, got: {repo}", file=sys.stderr)
        return 2
    owner, repo_name = parts

    generated_at = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        print(
            f"[info] Collecting milestone #{milestone_number} descendants from {owner}/{repo_name}...",
            file=sys.stderr,
        )
        all_issues, warnings, partial = collect_descendants(
            owner, repo_name, milestone_number, token
        )
        print(
            f"[info] Collected {len(all_issues)} items ({len(warnings)} warnings, partial={partial})",
            file=sys.stderr,
        )

        findings = analyze(all_issues, milestone_number, owner, repo_name, token)
        report = build_report(
            milestone_number, all_issues, findings, warnings, generated_at, repo, partial=partial
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_markdown(report))

    if args.strict and report["summary"]["has_invariant_violation"]:
        print(
            f"[strict] invariant violation detected: pr_mixed_count={report['summary']['pr_mixed_count']}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
