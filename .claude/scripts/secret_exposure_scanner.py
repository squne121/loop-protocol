#!/usr/bin/env python3
"""
secret_exposure_scanner.py

Secret exposure scanner for session recording artifacts.
Scans files for secret-like patterns (provider-aware rules).
Outputs findings in SECRET_EXPOSURE_SCAN_RESULT_V1 format.
raw_value / matched_text / context_line are NEVER included in output.

Usage:
    python3 .claude/scripts/secret_exposure_scanner.py --local <path> [--fail-on-finding]

Exit codes:
    0  - no findings
    1  - findings detected (with --fail-on-finding)
    2  - scan error
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Secret patterns (provider-aware)
# ---------------------------------------------------------------------------

RULES: list[dict[str, Any]] = [
    {
        "rule_id": "entircli_checkpoint_v1",
        "source_kind": "entircli_checkpoint",
        "pattern": re.compile(r"entire/checkpoints/v1"),
        "description": "EntireCLI checkpoint v1 marker",
    },
    {
        "rule_id": "entircli_checkpoint_header",
        "source_kind": "entircli_checkpoint",
        "pattern": re.compile(r"Entire-Checkpoint"),
        "description": "EntireCLI checkpoint header",
    },
    {
        "rule_id": "entircli_attribution_header",
        "source_kind": "entircli_checkpoint",
        "pattern": re.compile(r"Entire-Attribution"),
        "description": "EntireCLI attribution header",
    },
    {
        "rule_id": "transcript_source_kind",
        "source_kind": "transcript",
        "pattern": re.compile(r"source_kind:\s*transcript"),
        "description": "Raw transcript source_kind marker",
    },
    {
        "rule_id": "local_file_source_kind",
        "source_kind": "transcript",
        "pattern": re.compile(r"source_kind:\s*local_file"),
        "description": "Local file source_kind marker",
    },
    {
        "rule_id": "raw_transcript_marker",
        "source_kind": "transcript",
        "pattern": re.compile(r"\braw_transcript\b"),
        "description": "Raw transcript field marker",
    },
    {
        "rule_id": "assistant_response_marker",
        "source_kind": "transcript",
        "pattern": re.compile(r"\bassistant_response\b"),
        "description": "Assistant response field marker",
    },
    {
        "rule_id": "tool_result_marker",
        "source_kind": "transcript",
        "pattern": re.compile(r"\btool_result\b"),
        "description": "Tool result field marker",
    },
    {
        "rule_id": "absolute_path_posix_home",
        "source_kind": "path",
        "pattern": re.compile(r"/home/[^\s/]+"),
        "description": "Absolute POSIX home path",
    },
    {
        "rule_id": "absolute_path_posix_users",
        "source_kind": "path",
        "pattern": re.compile(r"/Users/[^\s/]+"),
        "description": "Absolute POSIX users path",
    },
    {
        "rule_id": "absolute_path_tmp",
        "source_kind": "path",
        "pattern": re.compile(r"/tmp/[^\s]*"),
        "description": "Absolute /tmp path",
    },
    {
        "rule_id": "absolute_path_windows",
        "source_kind": "path",
        "pattern": re.compile(r"[A-Z]:\\[^\s]+"),
        "description": "Absolute Windows path",
    },
    {
        "rule_id": "dotenv_content",
        "source_kind": "env_file",
        "pattern": re.compile(r"^[A-Z_][A-Z0-9_]*=\S+", re.MULTILINE),
        "description": ".env file content pattern",
    },
    {
        "rule_id": "private_key_begin",
        "source_kind": "crypto_key",
        "pattern": re.compile(r"BEGIN\s+\w*\s*PRIVATE\s+KEY"),
        "description": "PEM private key header",
    },
    {
        "rule_id": "anthropic_api_key",
        "source_kind": "api_key",
        "pattern": re.compile(r"sk-[A-Za-z0-9_\-]{30,}"),
        "description": "Anthropic API key (sk-...)",
    },
    {
        "rule_id": "anthropic_project_key",
        "source_kind": "api_key",
        "pattern": re.compile(r"sk-proj-[A-Za-z0-9_\-]{30,}"),
        "description": "Anthropic project API key (sk-proj-...)",
    },
    {
        "rule_id": "github_token_classic",
        "source_kind": "github_token",
        "pattern": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,255}"),
        "description": "GitHub classic PAT or OAuth token (length range updated for 2026 formats)",
    },
    {
        "rule_id": "github_fine_grained_pat",
        "source_kind": "github_token",
        "pattern": re.compile(r"github_pat_[A-Za-z0-9_]{82}"),
        "description": "GitHub fine-grained PAT (github_pat_ prefix, 82 char body)",
    },
]


# ---------------------------------------------------------------------------
# Safe redaction (no raw value in output)
# ---------------------------------------------------------------------------

def _sha256_prefix(raw_match: str, length: int = 8) -> str:
    """Return hex SHA-256 prefix of the raw match value (no raw value exposed)."""
    digest = hashlib.sha256(raw_match.encode("utf-8")).hexdigest()
    return digest[:length]


def _redacted_preview(raw_match: str, max_chars: int = 8) -> str:
    """
    Return a redacted preview: first min(4, len) chars + '****'.
    Never exposes the full value.
    """
    show = min(4, len(raw_match))
    return raw_match[:show] + "****"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# B7: default max file size (1MB)
_MAX_FILE_SIZE: int = 1024 * 1024


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def scan_text(content: str, location: str) -> list[dict[str, Any]]:
    """
    Scan text content for secret patterns.
    Returns findings with NO raw_value / matched_text / context_line.
    """
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()  # (rule_id, sha256_prefix) dedup

    for rule in RULES:
        for match in rule["pattern"].finditer(content):
            raw = match.group(0)
            sha_prefix = _sha256_prefix(raw)
            dedup_key = (rule["rule_id"], sha_prefix)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            finding: dict[str, Any] = {
                "rule_id": rule["rule_id"],
                "source_kind": rule["source_kind"],
                "redacted_preview": _redacted_preview(raw),
                "sha256_prefix": sha_prefix,
                "location": location,
            }
            # Explicit enforcement: raw_value / matched_text / context_line MUST NOT appear
            assert "raw_value" not in finding
            assert "matched_text" not in finding
            assert "context_line" not in finding
            findings.append(finding)

    return findings


def scan_file(path: Path, base: Path) -> list[dict[str, Any]]:
    """
    Scan a single file. Returns findings list.
    B5 fix: location uses relative path from base to avoid absolute paths in output.
    """
    # B7: skip symlinks and binary-like files
    if path.is_symlink():
        return []
    # B7: skip files above max size (default 1MB)
    try:
        if path.stat().st_size > _MAX_FILE_SIZE:
            return []
    except OSError:
        return []

    # B7: skip binary files (check for null bytes in first 8KB)
    try:
        sample = path.read_bytes()[:8192]
        if b"\x00" in sample:
            return []
    except OSError:
        pass

    try:
        location = str(path.relative_to(base))
    except ValueError:
        location = str(path)

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [{
            "rule_id": "scan_error",
            "source_kind": "error",
            "redacted_preview": "****",
            "sha256_prefix": "00000000",
            "location": location,
        }]
    return scan_text(content, location=location)


def scan_local(root: Path, exclude_patterns: list[str] | None = None) -> list[dict[str, Any]]:
    """
    Recursively scan a local directory or file.
    B7: Supports exclude patterns and skips symlinks/binaries.
    """
    import fnmatch
    findings: list[dict[str, Any]] = []
    if root.is_file():
        findings.extend(scan_file(root, root.parent))
    elif root.is_dir():
        for child in sorted(root.rglob("*")):
            if not child.is_file():
                continue
            if child.is_symlink():
                continue
            # B7: apply exclude patterns
            if exclude_patterns:
                rel = str(child.relative_to(root))
                if any(fnmatch.fnmatch(rel, pat) for pat in exclude_patterns):
                    continue
            findings.extend(scan_file(child, root))
    return findings


def _run_gh_api(url: str, fail_on_api_error: bool) -> Any | None:
    """
    Run `gh api <url>` and return parsed JSON, or None on error.
    B5: GitHub surface scan helper.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["gh", "api", url],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(
                f"ERROR: gh api {url!r} failed (exit {result.returncode}): {result.stderr.strip()}",
                file=sys.stderr,
            )
            if fail_on_api_error:
                sys.exit(2)
            return None
        return json.loads(result.stdout)
    except FileNotFoundError:
        print("ERROR: `gh` CLI not found. Install gh and authenticate to use GitHub surface scan.", file=sys.stderr)
        if fail_on_api_error:
            sys.exit(2)
        return None
    except Exception as exc:
        print(f"ERROR: gh api request failed: {exc}", file=sys.stderr)
        if fail_on_api_error:
            sys.exit(2)
        return None


def _parse_owner_repo_number(spec: str) -> tuple[str, str, str]:
    """
    Parse 'OWNER/REPO#N' into (owner, repo, number).
    B5: GitHub surface scan helper.
    """
    import re as _re
    m = _re.match(r"^([^/]+)/([^#]+)#(\d+)$", spec)
    if not m:
        raise ValueError(f"Invalid GitHub spec {spec!r}. Expected OWNER/REPO#N")
    return m.group(1), m.group(2), m.group(3)


def scan_github_issue(spec: str, fail_on_api_error: bool) -> list[dict[str, Any]]:
    """
    B5: Scan GitHub issue comments for secrets.
    spec: 'OWNER/REPO#N'
    """
    owner, repo, number = _parse_owner_repo_number(spec)
    url = f"repos/{owner}/{repo}/issues/{number}/comments"
    comments = _run_gh_api(url, fail_on_api_error)
    if comments is None:
        return []

    findings: list[dict[str, Any]] = []
    for comment in comments:
        comment_id = comment.get("id", "unknown")
        body = comment.get("body", "") or ""
        location = f"issue:{number}#comment:{comment_id}"
        findings.extend(scan_text(body, location=location))
    return findings


def scan_github_pr(spec: str, fail_on_api_error: bool) -> list[dict[str, Any]]:
    """
    B5: Scan GitHub PR comments and review comments for secrets.
    spec: 'OWNER/REPO#N'
    """
    owner, repo, number = _parse_owner_repo_number(spec)
    findings: list[dict[str, Any]] = []

    # PR issue comments (general comments on the PR)
    issue_url = f"repos/{owner}/{repo}/issues/{number}/comments"
    issue_comments = _run_gh_api(issue_url, fail_on_api_error) or []
    for comment in issue_comments:
        comment_id = comment.get("id", "unknown")
        body = comment.get("body", "") or ""
        location = f"pr:{number}#comment:{comment_id}"
        findings.extend(scan_text(body, location=location))

    # PR review comments (inline review comments)
    review_url = f"repos/{owner}/{repo}/pulls/{number}/comments"
    review_comments = _run_gh_api(review_url, fail_on_api_error) or []
    for comment in review_comments:
        comment_id = comment.get("id", "unknown")
        body = comment.get("body", "") or ""
        location = f"pr:{number}#review_comment:{comment_id}"
        findings.extend(scan_text(body, location=location))

    return findings


# ---------------------------------------------------------------------------
# Output schema (SECRET_EXPOSURE_SCAN_RESULT_V1)
# ---------------------------------------------------------------------------

def build_result(findings: list[dict[str, Any]], scanned_path: str) -> dict[str, Any]:
    """Build the SECRET_EXPOSURE_SCAN_RESULT_V1 output schema."""
    return {
        "schema": "SECRET_EXPOSURE_SCAN_RESULT_V1",
        "raw_value_included": False,  # REQUIRED false
        "scanned_path": scanned_path,
        "finding_count": len(findings),
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Secret exposure scanner for session recording artifacts."
    )
    parser.add_argument(
        "--local",
        metavar="PATH",
        help="Local path (file or directory) to scan",
    )
    parser.add_argument(
        "--github-issue",
        metavar="OWNER/REPO#N",
        help="Scan GitHub issue comments for secrets (B5: GitHub surface scan)",
    )
    parser.add_argument(
        "--github-pr",
        metavar="OWNER/REPO#N",
        help="Scan GitHub PR comments and review comments for secrets (B5: GitHub surface scan)",
    )
    parser.add_argument(
        "--fail-on-finding",
        action="store_true",
        default=False,
        help="Exit non-zero if any findings are detected",
    )
    parser.add_argument(
        "--fail-on-api-error",
        action="store_true",
        default=False,
        help="Exit non-zero if GitHub API call fails (used with --github-issue / --github-pr)",
    )
    parser.add_argument(
        "--exclude",
        metavar="PATTERN",
        action="append",
        default=[],
        help="Glob pattern to exclude (relative to scan root). May be specified multiple times.",
    )
    parser.add_argument(
        "--max-file-size",
        metavar="BYTES",
        type=int,
        default=1024 * 1024,
        help="Skip files larger than this size in bytes (default: 1048576 = 1MB)",
    )
    args = parser.parse_args()

    # Determine scan mode
    scan_modes = [m for m in [args.local, args.github_issue, args.github_pr] if m]
    if not scan_modes:
        print("ERROR: one of --local, --github-issue, or --github-pr is required", file=sys.stderr)
        return 2
    if len(scan_modes) > 1:
        print("ERROR: only one scan mode may be specified at a time", file=sys.stderr)
        return 2

    findings: list[dict[str, Any]]

    if args.local:
        scan_root = Path(args.local).resolve()
        if not scan_root.exists():
            print(f"ERROR: path not found: {scan_root}", file=sys.stderr)
            return 2

        # B7: apply --max-file-size globally
        global _MAX_FILE_SIZE
        _MAX_FILE_SIZE = args.max_file_size

        # Use relative path from cwd if possible, else use the argument as-is
        try:
            display_path = str(scan_root.relative_to(Path.cwd()))
        except ValueError:
            display_path = args.local

        findings = scan_local(scan_root, exclude_patterns=args.exclude if args.exclude else None)

    elif args.github_issue:
        display_path = f"github:issue:{args.github_issue}"
        try:
            findings = scan_github_issue(args.github_issue, fail_on_api_error=args.fail_on_api_error)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    elif args.github_pr:
        display_path = f"github:pr:{args.github_pr}"
        try:
            findings = scan_github_pr(args.github_pr, fail_on_api_error=args.fail_on_api_error)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    else:
        # Unreachable, but satisfies type checker
        return 2

    result = build_result(findings, scanned_path=display_path)

    # Enforce: raw_value MUST be false in output
    assert result["raw_value_included"] is False, "raw_value_included must be false"

    # Print JSON result (no raw secrets in output)
    output = json.dumps(result, indent=2, ensure_ascii=False)
    print(output, flush=True)

    if findings:
        print(f"\nScan complete: {len(findings)} finding(s) detected.", file=sys.stderr, flush=True)
        if args.fail_on_finding:
            return 1
    else:
        print("Scan complete: no findings.", file=sys.stderr, flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
