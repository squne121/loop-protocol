#!/usr/bin/env python3
"""
compact_author_result.py - Convert raw author result to ISSUE_AUTHOR_RESULT_COMPACT_V1.

Reads ISSUE_AUTHOR_RESULT_V1 JSON from stdin (or --input-file),
writes compact stdout and full artifact JSON.

stdout format (machine-readable compact lines):
  STATUS: ok | partial_failure | failed | no_change
  BODY_HASH: <sha256 of updated body or empty>
  COMMENT_URL: <url or empty>
  ARTIFACT: compact_author_result_v1=<path>
  NEXT_ACTION: proceed | human_judgment_required

exit codes: 0=ok, 1=no_change or partial_failure, 2=failed / body_hash_missing
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
# Schema constants (SSOT for ISSUE_AUTHOR_RESULT_COMPACT_V1)
# ---------------------------------------------------------------------------

COMPACT_SCHEMA_NAME = "ISSUE_AUTHOR_RESULT_COMPACT_V1"
COMPACT_SCHEMA_VERSION = "1"

REQUIRED_COMPACT_FIELDS = [
    "STATUS",
    "BODY_HASH",
    "COMMENT_URL",
    "ARTIFACT",
    "NEXT_ACTION",
]

VALID_STATUSES = {"ok", "partial_failure", "failed", "no_change"}


def _default_artifact_dir() -> Path:
    return Path(".claude/artifacts/issue-refinement-loop")


def _validate_artifact_path(path: str | Path) -> Path:
    """
    Validate artifact path component: reject .. and absolute paths.

    This validates user-supplied path components, not trusted artifact_dir base paths.
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
    """Detect secret-like strings in output."""
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


def compact_author_result(
    raw_result: dict[str, Any],
    artifact_dir: Path,
    issue_number: int | None = None,
    updated_body: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """
    Convert raw ISSUE_AUTHOR_RESULT_V1 to ISSUE_AUTHOR_RESULT_COMPACT_V1.

    Returns (compact_data, stdout_lines).
    Raises ValueError if body_hash is missing for ok/partial_failure status.
    """
    status = raw_result.get("status", "ok")
    if status not in VALID_STATUSES:
        status = "ok"

    # Derive body hash: from --updated-body or from raw_result.checked_body_sha256
    body_hash = ""
    if updated_body is not None:
        body_hash = hashlib.sha256(updated_body.encode("utf-8")).hexdigest()
    elif "checked_body_sha256" in raw_result:
        body_hash = raw_result["checked_body_sha256"]

    # body_hash is required for ok and partial_failure statuses
    if status in ("ok", "partial_failure") and not body_hash:
        raise ValueError(
            f"body_hash is required for status={status!r} but was not provided. "
            "Pass --updated-body or include checked_body_sha256 in the input."
        )

    # Extract comment URL (if any)
    comment_url = raw_result.get("comment_url", "") or ""

    # Determine NEXT_ACTION
    if status in ("ok", "no_change"):
        next_action = "proceed"
    elif status == "partial_failure":
        next_action = "proceed"
    else:
        next_action = "human_judgment_required"

    # Determine artifact path
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slot = str(issue_number) if issue_number else "unknown"
    _validate_issue_slot(slot)
    artifact_subdir = artifact_dir / slot
    artifact_filename = f"compact_author_result_{ts}.json"
    artifact_path = artifact_subdir / artifact_filename

    # Build full artifact JSON
    full_artifact: dict[str, Any] = {
        "schema": COMPACT_SCHEMA_NAME,
        "schema_version": COMPACT_SCHEMA_VERSION,
        "generated_at": ts,
        "status": status,
        "body_hash": body_hash,
        "comment_url": comment_url,
        "next_action": next_action,
        "updated_fields": raw_result.get("updated_fields", []),
        "mutation_result": raw_result.get("mutation_result", {}),
        "unchanged_reason": raw_result.get("unchanged_reason"),
        "validation_blockers": raw_result.get("validation_blockers", []),
        "reflection_notes": raw_result.get("reflection_notes", []),
        "parser_gap_repaired": raw_result.get("parser_gap_repaired", False),
        "contract_hygiene_repair_applied": raw_result.get(
            "contract_hygiene_repair_applied", False
        ),
    }

    # Write artifact atomically
    artifact_content = json.dumps(full_artifact, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_write(artifact_path, artifact_content)

    # Build compact dict
    compact_data = {
        "STATUS": status,
        "BODY_HASH": body_hash,
        "COMMENT_URL": comment_url,
        "ARTIFACT": f"compact_author_result_v1={artifact_path}",
        "NEXT_ACTION": next_action,
    }

    # Build stdout lines
    stdout_lines = [
        f"STATUS: {compact_data['STATUS']}",
        f"BODY_HASH: {compact_data['BODY_HASH']}",
    ]
    if compact_data["COMMENT_URL"]:
        stdout_lines.append(f"COMMENT_URL: {compact_data['COMMENT_URL']}")
    stdout_lines.append(f"ARTIFACT: {compact_data['ARTIFACT']}")
    stdout_lines.append(f"NEXT_ACTION: {compact_data['NEXT_ACTION']}")

    return compact_data, stdout_lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert ISSUE_AUTHOR_RESULT_V1 to ISSUE_AUTHOR_RESULT_COMPACT_V1"
    )
    parser.add_argument(
        "--input-file",
        type=Path,
        default=None,
        help="Path to ISSUE_AUTHOR_RESULT_V1 JSON (default: stdin)",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=_default_artifact_dir(),
        help="Base artifact directory",
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        default=None,
        help="Issue number for artifact sub-directory",
    )
    parser.add_argument(
        "--updated-body",
        type=str,
        default=None,
        help="Updated issue body text (for body_hash computation)",
    )
    parser.add_argument(
        "--updated-body-file",
        type=Path,
        default=None,
        help="Path to updated issue body file (for body_hash computation)",
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
        print("STATUS: failed", flush=True)
        print(f"ERROR: JSON parse error: {e}", file=sys.stderr, flush=True)
        return 2
    except Exception as e:
        print("STATUS: failed", flush=True)
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        return 2

    # Resolve updated_body
    updated_body: str | None = args.updated_body
    if args.updated_body_file:
        try:
            updated_body = args.updated_body_file.read_text(encoding="utf-8")
        except Exception as e:
            print("STATUS: failed", flush=True)
            print(f"ERROR: failed to read --updated-body-file: {e}", file=sys.stderr, flush=True)
            return 2

    # Convert
    try:
        _compact, stdout_lines = compact_author_result(
            raw_result,
            artifact_dir=args.artifact_dir,
            issue_number=args.issue_number,
            updated_body=updated_body,
        )
    except ValueError as e:
        print("STATUS: failed", flush=True)
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
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

    # Output
    for line in stdout_lines:
        print(line, flush=True)

    status = raw_result.get("status", "ok")
    if status == "no_change":
        return 1
    if status == "partial_failure":
        return 1
    if status == "failed":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
