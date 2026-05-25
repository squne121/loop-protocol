"""
Validator for REPO_EVIDENCE_REF_V1 schema.

Validates file evidence references used in codebase-investigator and issue-refinement-loop.
Performs strict verification of:
  - type == "REPO_EVIDENCE_REF_V1"
  - commit_sha format (40-char or 64-char hex, checked against object_format)
  - object_format enum (sha1 or sha256)
  - excerpt_sha256 hash matching (via git show + blob bytes)
  - permalink parser (strict commit-pinned URLs, rejects tree/ and branch refs)
  - ISO8601 timestamp parsing (verified_at field)
  - Required field presence and types

Usage:
    from validate_repo_evidence_ref import validate_repo_evidence_ref, ValidationResult

    result = validate_repo_evidence_ref(evidence, repo_root=Path("/path/to/repo"))
    if result["ok"]:
        print("Evidence is verified")
    else:
        print(f"Verification failed: {result['status']}")
        for error in result["errors"]:
            print(f"  - {error}")
"""

import re
import subprocess
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Callable, Optional, TypedDict


class ValidationResult(TypedDict, total=False):
    """Result of evidence validation."""
    ok: bool
    status: str  # "verified" | "inconclusive" | "rejected"
    errors: list[str]
    reasons: list[str]


def validate_repo_evidence_ref(
    evidence: dict,
    *,
    repo_root: Optional[Path] = None,
    blob_bytes_getter: Optional[Callable[[str, str], bytes]] = None
) -> ValidationResult:
    """
    Validate a REPO_EVIDENCE_REF_V1 instance.

    Args:
        evidence: The evidence dict to validate
        repo_root: Repository root path (required for real git show, optional for unit tests)
        blob_bytes_getter: Optional callable(commit_sha, path) -> bytes for unit testing.
                          If provided, used instead of git show. If None, uses git CLI.

    Returns:
        ValidationResult with ok, status, errors, reasons fields.
        status: "verified" | "inconclusive" | "rejected"
    """
    errors = []
    reasons = []

    # Check 1: type field is constant REPO_EVIDENCE_REF_V1
    if evidence.get("type") != "REPO_EVIDENCE_REF_V1":
        errors.append(f"type field must be 'REPO_EVIDENCE_REF_V1', got '{evidence.get('type')}'")
        return {
            "ok": False,
            "status": "rejected",
            "errors": errors,
            "reasons": reasons,
        }

    # Check 2: Required fields presence
    required_fields = [
        "commit_sha", "object_format", "path", "start_line", "end_line",
        "permalink", "excerpt_sha256", "verification_status",
        "verification_method", "verified_at"
    ]
    for field in required_fields:
        if field not in evidence:
            errors.append(f"Required field missing: {field}")

    if errors:
        return {
            "ok": False,
            "status": "rejected",
            "errors": errors,
            "reasons": reasons,
        }

    # Check 3: object_format enum
    object_format = evidence.get("object_format")
    if object_format not in ("sha1", "sha256"):
        errors.append(f"object_format must be 'sha1' or 'sha256', got '{object_format}'")

    # Check 4: commit_sha format and length based on object_format
    commit_sha = evidence.get("commit_sha", "")
    if not isinstance(commit_sha, str):
        errors.append("commit_sha must be a string")
    else:
        expected_length = 40 if object_format == "sha1" else 64
        if len(commit_sha) != expected_length:
            errors.append(
                f"commit_sha must be {expected_length} characters for "
                f"object_format '{object_format}', got {len(commit_sha)}"
            )
        if not re.match(r"^[a-f0-9]+$", commit_sha):
            errors.append("commit_sha must be lowercase hexadecimal")

    # Check 5: excerpt_sha256 format
    excerpt_sha256 = evidence.get("excerpt_sha256", "")
    if not isinstance(excerpt_sha256, str) or len(excerpt_sha256) != 64:
        errors.append("excerpt_sha256 must be exactly 64 lowercase hex characters")
    elif not re.match(r"^[a-f0-9]{64}$", excerpt_sha256):
        errors.append("excerpt_sha256 must be valid 64-character SHA-256 hash")

    # Check 6: start_line and end_line are positive integers
    start_line = evidence.get("start_line")
    end_line = evidence.get("end_line")

    if not isinstance(start_line, int) or start_line < 1:
        errors.append("start_line must be a positive integer (>= 1)")

    if not isinstance(end_line, int) or end_line < 1:
        errors.append("end_line must be a positive integer (>= 1)")

    if isinstance(start_line, int) and isinstance(end_line, int) and end_line < start_line:
        errors.append("end_line must be >= start_line")

    # Check 7: path is a non-empty string
    path = evidence.get("path", "")
    if not isinstance(path, str) or not path:
        errors.append("path must be a non-empty string")

    # Check 8: verification_status enum
    verification_status = evidence.get("verification_status")
    if verification_status not in ("verified", "inconclusive"):
        errors.append(
            f"verification_status must be 'verified' or 'inconclusive', got '{verification_status}'"
        )

    # Check 9: verification_method enum
    verification_method = evidence.get("verification_method")
    allowed_methods = ("sha256_hash_match", "sha256_hash_mismatch", "line_range_unverified", "fetch_error")
    if verification_method not in allowed_methods:
        errors.append(f"verification_method must be one of {allowed_methods}, got '{verification_method}'")

    # Check 10: Consistent status + method
    if verification_status == "verified" and verification_method != "sha256_hash_match":
        errors.append(
            "verification_status 'verified' requires verification_method 'sha256_hash_match'"
        )

    # Check 11: verified_at ISO8601 parsing
    verified_at = evidence.get("verified_at")
    if isinstance(verified_at, str):
        try:
            datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            errors.append(f"verified_at must be ISO8601 datetime, got '{verified_at}'")
    else:
        errors.append("verified_at must be a string (ISO8601 datetime)")

    # Check 12: anchor_text is None or string
    anchor_text = evidence.get("anchor_text")
    if anchor_text is not None and not isinstance(anchor_text, str):
        errors.append("anchor_text must be null or a string")

    # If there are structural errors, return rejected before permalink parsing
    if errors:
        return {
            "ok": False,
            "status": "rejected",
            "errors": errors,
            "reasons": reasons,
        }

    # Check 13: Strict permalink parser (requires commit SHA, rejects tree/ and branch refs)
    permalink = evidence.get("permalink", "")

    # Pattern: https://github.com/{owner}/{repo}/blob/{commit_sha}/{path}#L{start}-L{end}
    # Does NOT match tree/ or blob/{branch-name}
    permalink_pattern = (
        r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
        r"/blob/(?P<sha>[a-f0-9]{40,64})/(?P<path>[^#]+)"
        r"#L(?P<start>\d+)-L(?P<end>\d+)$"
    )

    match = re.match(permalink_pattern, permalink)
    if not match:
        errors.append(
            "permalink must be a GitHub blob URL with commit SHA (not branch name, not tree/): "
            "https://github.com/{owner}/{repo}/blob/{commit_sha}/{path}#L{start}-L{end}"
        )
    else:
        # Validate parsed permalink components match evidence fields
        parsed_sha = match.group("sha")
        parsed_path = match.group("path")
        parsed_start = int(match.group("start"))
        parsed_end = int(match.group("end"))

        if parsed_sha != commit_sha:
            errors.append(
                f"permalink SHA '{parsed_sha}' does not match commit_sha '{commit_sha}'"
            )

        if parsed_path != path:
            errors.append(
                f"permalink path '{parsed_path}' does not match evidence path '{path}'"
            )

        if parsed_start != start_line:
            errors.append(
                f"permalink start line {parsed_start} does not match evidence start_line {start_line}"
            )

        if parsed_end != end_line:
            errors.append(
                f"permalink end line {parsed_end} does not match evidence end_line {end_line}"
            )

    # If permalink validation failed, reject early
    if errors:
        return {
            "ok": False,
            "status": "rejected",
            "errors": errors,
            "reasons": reasons,
        }

    # Check 14: excerpt_sha256 verification (optional, only if blob_bytes_getter or repo_root provided)
    if blob_bytes_getter is not None or repo_root is not None:
        try:
            if blob_bytes_getter is not None:
                # Unit test mode: use provided getter
                excerpt_bytes = blob_bytes_getter(commit_sha, path)
            else:
                # Real mode: use git show
                result = subprocess.run(
                    ["git", "show", f"{commit_sha}:{path}"],
                    cwd=repo_root,
                    capture_output=True,
                    check=False,
                )
                if result.returncode != 0:
                    reasons.append(f"git show failed: {result.stderr.decode('utf-8', errors='ignore')}")
                    return {
                        "ok": False,
                        "status": "inconclusive",
                        "errors": errors,
                        "reasons": reasons,
                    }
                excerpt_bytes = result.stdout

            # Slice lines: convert to list, slice [start_line-1 : end_line] (0-indexed, inclusive)
            lines = excerpt_bytes.split(b"\n")

            # Handle inclusive range: start_line=1, end_line=10 -> lines[0:10]
            # (note: split produces N+1 elements if there's a trailing LF, so this is careful)
            sliced_lines = lines[start_line - 1 : end_line]

            # Reconstruct: each line should have its trailing \n, except possibly the last
            reconstructed = b"\n".join(sliced_lines)

            # If the last line in the original file has a LF, append one
            # (lines[end_line-1] is the last line we want; check if it had trailing LF)
            if end_line <= len(lines) - 1:  # end_line is within the file
                # The line exists and may have had trailing LF
                # We use split() which removes the LF, so add it back
                if end_line < len(lines):
                    # There's a next line, so the current line definitely had LF
                    reconstructed += b"\n"
                # else: end_line is the last line (potentially without LF)

            computed_hash = hashlib.sha256(reconstructed).hexdigest()

            if computed_hash != excerpt_sha256:
                reasons.append(
                    f"excerpt_sha256 mismatch: claimed '{excerpt_sha256}', "
                    f"computed '{computed_hash}'. Line range may be stale."
                )
                return {
                    "ok": False,
                    "status": "inconclusive",
                    "errors": errors,
                    "reasons": reasons,
                }

            reasons.append("excerpt_sha256 matches computed hash from git show")

        except subprocess.CalledProcessError as e:
            reasons.append(f"Failed to retrieve file content: {e}")
            return {
                "ok": False,
                "status": "inconclusive",
                "errors": errors,
                "reasons": reasons,
            }
        except Exception as e:
            reasons.append(f"Error during hash verification: {e}")
            return {
                "ok": False,
                "status": "inconclusive",
                "errors": errors,
                "reasons": reasons,
            }

    # All checks passed
    return {
        "ok": True,
        "status": "verified",
        "errors": errors,
        "reasons": reasons,
    }
