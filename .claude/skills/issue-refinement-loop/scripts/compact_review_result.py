#!/usr/bin/env python3
"""
compact_review_result.py - Convert raw review result to ISSUE_REVIEW_RESULT_COMPACT_V1.

Reads REVIEW_ISSUE_RESULT_V1 JSON from stdin (or --input-file),
writes compact stdout and full artifact JSON.

stdout format (machine-readable compact lines):
  STATUS: ok | failed
  VERDICT: approve | needs-fix
  SUMMARY: <one-line prose>
  BLOCKERS: <count>
  NEXT_ACTION: proceed | request_changes | human_judgment_required
  MUST_READ: <paths or empty>
  EVIDENCE: <artifact path>
  ARTIFACT: compact_review_result_v1=<path>

exit codes: 0=ok, 1=warn, 2=verdict_missing / validation_error
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Schema constants (SSOT for ISSUE_REVIEW_RESULT_COMPACT_V1)
# ---------------------------------------------------------------------------

COMPACT_SCHEMA_NAME = "ISSUE_REVIEW_RESULT_COMPACT_V1"
COMPACT_SCHEMA_VERSION = "1"

REQUIRED_COMPACT_FIELDS = [
    "STATUS",
    "VERDICT",
    "SUMMARY",
    "BLOCKERS",
    "NEXT_ACTION",
    "MUST_READ",
    "EVIDENCE",
    "ARTIFACT",
]

VALID_VERDICTS = {"approve", "needs-fix"}
VALID_STATUSES = {"ok", "failed"}
VALID_NEXT_ACTIONS = {"proceed", "request_changes", "human_judgment_required"}


def _default_artifact_dir() -> Path:
    """Return default artifact directory."""
    return Path(".claude/artifacts/issue-refinement-loop")


def _validate_artifact_path(path: str | Path) -> Path:
    """
    Validate artifact path component: reject .. and absolute paths.

    This validates user-supplied path components (issue_number, filenames),
    not trusted artifact_dir base paths.
    """
    p = Path(path)
    if p.is_absolute():
        raise ValueError(f"Absolute artifact path rejected: {path}")
    parts = p.parts
    if ".." in parts:
        raise ValueError(f"Path traversal rejected: {path}")
    return p


def _validate_issue_slot(slot: str) -> None:
    """Validate that the issue slot component does not contain path traversal."""
    if ".." in slot or "/" in slot or "\\" in slot:
        raise ValueError(f"Invalid issue slot: {slot!r}")


def _atomic_write(path: Path, content: bytes) -> None:
    """Write content atomically with 0600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent)
    try:
        os.chmod(tmp_path, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _no_secret_check(text: str) -> list[str]:
    """
    Detect secret-like strings in output.
    Returns list of violation descriptions (empty = clean).
    """
    import re

    patterns = [
        (r"(?i)(Bearer\s+)[A-Za-z0-9\-._~+/]{20,}", "Bearer token"),
        (r"(?i)(Authorization:\s*)[A-Za-z0-9+/=]{20,}", "Authorization header"),
        (r"(?i)(api[_-]?key\s*[:=]\s*)[A-Za-z0-9\-._]{20,}", "API key"),
        (r"(?i)(secret\s*[:=]\s*)[A-Za-z0-9\-._]{20,}", "secret value"),
        (r"(?i)(token\s*[:=]\s*)[A-Za-z0-9\-._]{20,}", "token value"),
        (r"(?i)(cookie\s*[:=]\s*)[A-Za-z0-9\-._]{20,}", "cookie value"),
        (r"ghp_[A-Za-z0-9]{36}", "GitHub personal access token"),
        (r"ghs_[A-Za-z0-9]{36}", "GitHub server token"),
    ]
    violations = []
    for pattern, label in patterns:
        if re.search(pattern, text):
            violations.append(label)
    return violations


def compact_review_result(
    raw_result: dict[str, Any],
    artifact_dir: Path,
    issue_number: int | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """
    Convert raw REVIEW_ISSUE_RESULT_V1 to ISSUE_REVIEW_RESULT_COMPACT_V1.

    Returns (compact_data, stdout_lines).
    Raises ValueError if verdict is missing or invalid.
    """
    # Validate required fields
    verdict = raw_result.get("verdict")
    if not verdict:
        raise ValueError("verdict field missing in REVIEW_ISSUE_RESULT_V1")
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"Invalid verdict: {verdict!r}. Expected one of {VALID_VERDICTS}")

    status = raw_result.get("status", "ok")
    if status not in VALID_STATUSES:
        status = "ok"

    # Derive NEXT_ACTION from verdict
    if verdict == "approve":
        next_action = "proceed"
    else:
        failure_class = raw_result.get("failure_class")
        if failure_class and "human_judgment" in str(failure_class):
            next_action = "human_judgment_required"
        else:
            next_action = "request_changes"

    # Extract blockers summary
    blocking_issues = raw_result.get("blocking_issues", [])
    blockers_count = len(blocking_issues)

    # Extract evidence refs (non-raw: only URLs and file paths)
    evidence_refs: list[str] = []
    issue_url = raw_result.get("issue_url", "")
    if issue_url:
        evidence_refs.append(issue_url)

    # Build compact summary (single line, no raw content)
    summary_parts = []
    if verdict == "approve":
        summary_parts.append("contract ready")
    else:
        summary_parts.append(f"{blockers_count} blocker(s)")
        if blocking_issues:
            first_code = ""
            first = blocking_issues[0]
            if isinstance(first, dict):
                first_code = first.get("code", "")
            elif isinstance(first, str):
                first_code = first[:60]
            if first_code:
                summary_parts.append(f"first={first_code}")
    summary = "; ".join(summary_parts)

    # Determine artifact path
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slot = str(issue_number) if issue_number else "unknown"
    _validate_issue_slot(slot)
    artifact_subdir = artifact_dir / slot
    artifact_filename = f"compact_review_result_{ts}.json"
    artifact_path = artifact_subdir / artifact_filename

    # Build full artifact JSON (contains full structured data, never returned raw to main context)
    full_artifact: dict[str, Any] = {
        "schema": COMPACT_SCHEMA_NAME,
        "schema_version": COMPACT_SCHEMA_VERSION,
        "generated_at": ts,
        "status": status,
        "verdict": verdict,
        "summary": summary,
        "next_action": next_action,
        "blockers_count": blockers_count,
        "blocking_issues": blocking_issues,
        "structured_blockers": raw_result.get("structured_blockers", []),
        "non_blocking_improvements": raw_result.get("non_blocking_improvements", []),
        "diff_proposal": raw_result.get("diff_proposal", {}),
        "deterministic_checks": raw_result.get("deterministic_checks", {}),
        "needs_second_pass": raw_result.get("needs_second_pass", False),
        "issue_url": issue_url,
        "evidence_refs": evidence_refs,
        "failure_class": raw_result.get("failure_class"),
    }

    # Write artifact atomically
    artifact_content = json.dumps(full_artifact, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_write(artifact_path, artifact_content)

    # Build compact dict (stdout representation)
    compact_data = {
        "STATUS": status,
        "VERDICT": verdict,
        "SUMMARY": summary,
        "BLOCKERS": str(blockers_count),
        "NEXT_ACTION": next_action,
        "MUST_READ": "",
        "EVIDENCE": str(artifact_path),
        "ARTIFACT": f"compact_review_result_v1={artifact_path}",
    }

    # Build stdout lines
    stdout_lines = [
        f"STATUS: {compact_data['STATUS']}",
        f"VERDICT: {compact_data['VERDICT']}",
        f"SUMMARY: {compact_data['SUMMARY']}",
        f"BLOCKERS: {compact_data['BLOCKERS']}",
        f"NEXT_ACTION: {compact_data['NEXT_ACTION']}",
    ]
    if compact_data["MUST_READ"]:
        stdout_lines.append(f"MUST_READ: {compact_data['MUST_READ']}")
    if compact_data["EVIDENCE"]:
        stdout_lines.append(f"EVIDENCE: {compact_data['EVIDENCE']}")
    stdout_lines.append(f"ARTIFACT: {compact_data['ARTIFACT']}")

    return compact_data, stdout_lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert REVIEW_ISSUE_RESULT_V1 to ISSUE_REVIEW_RESULT_COMPACT_V1"
    )
    parser.add_argument(
        "--input-file",
        type=Path,
        default=None,
        help="Path to REVIEW_ISSUE_RESULT_V1 JSON (default: stdin)",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=_default_artifact_dir(),
        help="Base artifact directory (default: .claude/artifacts/issue-refinement-loop)",
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        default=None,
        help="Issue number for artifact sub-directory",
    )
    args = parser.parse_args()

    # Read input
    try:
        if args.input_file:
            raw_text = args.input_file.read_text(encoding="utf-8")
        else:
            raw_text = sys.stdin.read()
        raw_result = json.loads(raw_text)
    except json.JSONDecodeError as e:
        print(f"STATUS: failed", flush=True)
        print(f"ERROR: JSON parse error: {e}", flush=True, file=sys.stderr)
        return 2
    except Exception as e:
        print(f"STATUS: failed", flush=True)
        print(f"ERROR: {e}", flush=True, file=sys.stderr)
        return 2

    # Convert
    try:
        _compact, stdout_lines = compact_review_result(
            raw_result,
            artifact_dir=args.artifact_dir,
            issue_number=args.issue_number,
        )
    except ValueError as e:
        print(f"STATUS: failed", flush=True)
        print(f"ERROR: {e}", flush=True, file=sys.stderr)
        return 2

    # Secret check on stdout output
    output_text = "\n".join(stdout_lines)
    violations = _no_secret_check(output_text)
    if violations:
        print("STATUS: failed", flush=True)
        print(
            f"ERROR: secret-like strings detected in stdout: {violations}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    # Output compact lines
    for line in stdout_lines:
        print(line, flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
