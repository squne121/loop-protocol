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

import jsonschema as _jsonschema

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
REVIEW_RESULT_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent / "review-issue" / "schemas" / "review_issue_result_v1.json"
)

# Issue #1541 PR #1557 OWNER REQUEST_CHANGES High-1: the "wire bytes" contract
# is the EXACT byte sequence that ends up on stdout -- one trailing LF after
# the last of `stdout_lines`, not `"\n".join(stdout_lines)` alone. Every
# producer/validator in the chain (this module, `emit_parent_review_envelope_v2.py`
# `validate_child_intermediate()`, `validate_review_compact_output.py`) MUST
# measure/compare this SAME byte sequence, or a producer can emit output it
# believes is within budget while a downstream consumer measures one byte
# more (the final LF) and rejects it as `byte_budget_exceeded` -- exactly the
# boundary bug this constant/helper closes.


def wire_bytes(stdout_lines: list[str]) -> bytes:
    """The exact UTF-8 byte sequence written to stdout for `stdout_lines`:
    each line followed by a single LF (matching `print(line, flush=True)`
    called once per line), i.e. `"\\n".join(stdout_lines) + "\\n"`. This is
    the SSOT byte-count contract shared by every producer/validator that
    measures the 2048-byte budget (High-1)."""
    if not stdout_lines:
        return b""
    return ("\n".join(stdout_lines) + "\n").encode("utf-8")


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


def _validate_artifact_containment(artifact_path: Path, repo_root: Path) -> None:
    """
    Validate that the resolved artifact path is contained within the expected base directory.

    Uses Path.resolve() to follow symlinks and eliminate '..' before checking containment.
    Raises ValueError if the resolved path escapes the base directory.
    """
    base = (repo_root / ".claude/artifacts/issue-refinement-loop").resolve()
    resolved = artifact_path.resolve()
    if not resolved.is_relative_to(base):
        raise ValueError(
            f"Artifact path escapes base directory: resolved={resolved}, base={base}"
        )


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


def _load_review_result_schema() -> dict[str, Any]:
    return _strict_json_loads(REVIEW_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))


def _reject_nonfinite_json(token: str) -> None:
    raise ValueError(f"Non-finite JSON constant rejected: {token}")


def _strict_json_loads(text: str) -> dict[str, Any]:
    return json.loads(text, parse_constant=_reject_nonfinite_json)


def _strict_json_dumps(payload: Any, *, indent: int | None = None) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=indent, allow_nan=False)


def _strict_json_dumps_compact(payload: Any) -> str:
    """Canonical single-line JSON (sorted keys, no whitespace, ASCII-only)
    -- MUST match `parent_replay_binding.canonical_json_bytes()` byte for
    byte, since the parent recomputes this digest independently over the
    normalized claim object (Issue #1532)."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _validate_review_result_schema(raw_result: dict[str, Any]) -> None:
    _jsonschema.validate(instance=raw_result, schema=_load_review_result_schema())


# ---------------------------------------------------------------------------
# Canonical failure envelope (AC1 / #1165)
# ---------------------------------------------------------------------------


def _write_failure_artifact(
    artifact_dir: Path,
    issue_number: int | None,
    reason_code: str,
    detail: str,
    repo_root: Path | None = None,
    extra: "dict[str, Any] | None" = None,
) -> "tuple[Path, str]":
    """
    Write a PRODUCER_FAILURE_V1 artifact and return (path, sha256).

    AC7: when repo_root is provided, the canonical path
    <repo_root>/.claude/artifacts/issue-refinement-loop/<issue>/ is used.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slot = str(issue_number) if issue_number else "unknown"
    _validate_issue_slot(slot)
    # AC7: use canonical base when repo_root is provided
    if repo_root is not None:
        base = repo_root / ".claude" / "artifacts" / "issue-refinement-loop"
    else:
        base = artifact_dir
    artifact_subdir = base / slot
    artifact_path = artifact_subdir / f"producer_failure_{reason_code}_{ts}.json"
    payload: "dict[str, Any]" = {
        "schema": "PRODUCER_FAILURE_V1",
        "generated_at": ts,
        "reason_code": reason_code,
        "detail": str(detail),
    }
    if extra:
        payload.update(extra)
    content = _strict_json_dumps(payload, indent=2).encode("utf-8")
    sha256 = hashlib.sha256(content).hexdigest()
    _atomic_write(artifact_path, content)
    return artifact_path, sha256


def _emit_failure_envelope(
    reason_code: str,
    next_action: str,
    detail: str,
    artifact_dir: Path,
    issue_number: "int | None",
    repo_root: "Path | None" = None,
    extra: "dict[str, Any] | None" = None,
) -> None:
    """
    Emit canonical failure envelope to stdout and write failure artifact.

    stdout format (always <= 2048 UTF-8 wire bytes, see `wire_bytes()`):
      STATUS: failed
      NEXT_ACTION: <next_action>
      REASON_CODE: <reason_code>
      ARTIFACT: producer_failure_v1=<path>
      ARTIFACT_SHA256: <sha256>
    """
    artifact_ref = "producer_failure_v1=<write_failed>"
    artifact_sha256 = "write_failed"
    try:
        artifact_path, sha256 = _write_failure_artifact(
            artifact_dir, issue_number, reason_code, detail, repo_root, extra
        )
        artifact_ref = f"producer_failure_v1={artifact_path}"
        artifact_sha256 = sha256
    except Exception:
        pass  # Artifact write failed; emit envelope with write_failed sentinel

    lines = [
        "STATUS: failed",
        f"NEXT_ACTION: {next_action}",
        f"REASON_CODE: {reason_code}",
        f"ARTIFACT: {artifact_ref}",
        f"ARTIFACT_SHA256: {artifact_sha256}",
    ]

    # Issue #1541 High-1: enforce the 2048-byte cap on the EXACT wire bytes
    # (lines joined by LF, plus the trailing LF the final `print()` call
    # emits) -- not on the pre-trailing-LF join alone. Truncate artifact_ref
    # if needed.
    if len(wire_bytes(lines)) > 2048:
        short_ref = artifact_ref[:80] + "..." if len(artifact_ref) > 80 else artifact_ref
        lines[3] = f"ARTIFACT: {short_ref}"

    for line in lines:
        print(line, flush=True)


def compact_review_result(
    raw_result: dict[str, Any],
    artifact_dir: Path,
    issue_number: int | None = None,
    repo_root: Path | None = None,
) -> tuple[dict[str, Any], list[str], Path, bytes]:
    """
    Convert raw REVIEW_ISSUE_RESULT_V1 to ISSUE_REVIEW_RESULT_COMPACT_V1.

    Returns (compact_data, stdout_lines, artifact_path, artifact_content).
    Does NOT write the artifact — caller must write it AFTER budget check (B3).
    Raises ValueError if:
    - verdict is missing or invalid
    - status is unknown/invalid (fail-close; B8)
    - artifact path escapes containment base (B4)
    """
    # Validate required fields
    verdict = raw_result.get("verdict")
    if not verdict:
        raise ValueError("verdict field missing in REVIEW_ISSUE_RESULT_V1")
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"Invalid verdict: {verdict!r}. Expected one of {VALID_VERDICTS}")

    # B8: fail-close on unknown/invalid status (do not round to ok)
    status = raw_result.get("status", "ok")
    if status not in VALID_STATUSES:
        raise ValueError(
            f"Unknown/invalid status: {status!r}. Expected one of {VALID_STATUSES}"
        )
    _validate_review_result_schema(raw_result)

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

    # B4: containment check — resolve symlinks and verify artifact stays under base
    if repo_root is not None:
        _validate_artifact_containment(artifact_path, repo_root)

    # Build full artifact JSON (contains full structured data, never returned raw to main context)
    full_artifact: dict[str, Any] = {
        "schema": COMPACT_SCHEMA_NAME,
        "schema_version": COMPACT_SCHEMA_VERSION,
        "generated_at": ts,
        "status": status,
        "verdict": verdict,
        "producer_schema": raw_result.get("schema"),
        "producer_schema_version": raw_result.get("schema_version"),
        "producer_body_sha256": raw_result.get("body_sha256"),
        "summary": summary,
        "next_action": next_action,
        "blockers_count": blockers_count,
        "blocking_issues": blocking_issues,
        "structured_blockers": raw_result.get("structured_blockers", []),
        "findings": raw_result.get("findings", []),
        "non_blocking_improvements": raw_result.get("non_blocking_improvements", []),
        "diff_proposal": raw_result.get("diff_proposal", {}),
        "deterministic_checks": raw_result.get("deterministic_checks", {}),
        "needs_second_pass": raw_result.get("needs_second_pass", False),
        "issue_url": issue_url,
        "evidence_refs": evidence_refs,
        "failure_class": raw_result.get("failure_class"),
    }

    # B5: secret check on artifact content before writing
    artifact_content_str = _strict_json_dumps(full_artifact, indent=2)
    artifact_violations = _no_secret_check(artifact_content_str)
    if artifact_violations:
        raise ValueError(
            f"secret-like strings detected in artifact content: {artifact_violations}"
        )

    # B3: do NOT write artifact here; caller writes AFTER stdout budget check
    artifact_content = artifact_content_str.encode("utf-8")

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
    # B7: MUST_READ is always output (even when empty)
    stdout_lines = [
        f"STATUS: {compact_data['STATUS']}",
        f"VERDICT: {compact_data['VERDICT']}",
        f"SUMMARY: {compact_data['SUMMARY']}",
        f"BLOCKERS: {compact_data['BLOCKERS']}",
        f"NEXT_ACTION: {compact_data['NEXT_ACTION']}",
        f"MUST_READ: {compact_data['MUST_READ']}",
    ]
    if compact_data["EVIDENCE"]:
        stdout_lines.append(f"EVIDENCE: {compact_data['EVIDENCE']}")
    stdout_lines.append(f"ARTIFACT: {compact_data['ARTIFACT']}")

    # Issue #1532 Blocker 1/3.1: needs-fix appends exactly ONE additional
    # field -- the bounded, untrusted REVIEWER_BLOCKER_CLAIM_V1 claim. This
    # SubAgent no longer co-locate-runs reviewer_claim_replay.py and no
    # longer emits REPLAY_VERDICT/ROUTING/SHOULD_CONSUME/etc: those
    # semantic (routing) fields are now EXCLUSIVELY parent-computed
    # (PARENT_REPLAY_* -- see parent_replay_binding.py /
    # validate_review_compact_output.py). The claim is built here (a
    # deterministic script), not authored freeform by the SubAgent prompt,
    # and carries only reviewer_blocker_code/message/line_start/line_end --
    # no findings/checker_evidence/deterministic_checks can be smuggled in.
    if verdict == "needs-fix":
        body_sha256_raw = raw_result.get("body_sha256")
        body_sha256 = (
            body_sha256_raw if isinstance(body_sha256_raw, str) and body_sha256_raw else "sha256:" + ("0" * 64)
        )
        claim_blockers: list[dict[str, Any]] = []
        for item in blocking_issues:
            if isinstance(item, dict):
                code = str(item.get("code") or item.get("reviewer_blocker_code") or "").strip()
                if not code:
                    continue
                claim_blockers.append(
                    {
                        "reviewer_blocker_code": code,
                        "message": item.get("message"),
                        "line_start": item.get("line_start"),
                        "line_end": item.get("line_end"),
                    }
                )
            elif isinstance(item, str) and item.strip():
                claim_blockers.append(
                    {
                        "reviewer_blocker_code": item.strip(),
                        "message": None,
                        "line_start": None,
                        "line_end": None,
                    }
                )
        reviewer_blocker_claim = {
            "schema": "REVIEWER_BLOCKER_CLAIM_V1",
            "body_sha256": body_sha256,
            "blockers": claim_blockers,
        }
        claim_line = _strict_json_dumps_compact(reviewer_blocker_claim)
        compact_data["REVIEWER_BLOCKER_CLAIM"] = claim_line
        stdout_lines.append(f"REVIEWER_BLOCKER_CLAIM: {claim_line}")

    return compact_data, stdout_lines, artifact_path, artifact_content


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
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root for artifact containment check (B4)",
    )
    args = parser.parse_args()

    # Read input
    try:
        if args.input_file:
            raw_text = args.input_file.read_text(encoding="utf-8")
        else:
            raw_text = sys.stdin.read()
        raw_result = _strict_json_loads(raw_text)
    except (json.JSONDecodeError, ValueError) as e:
        _emit_failure_envelope(
            reason_code="schema_mismatch",
            next_action="human_judgment_required",
            detail=f"JSON parse error: {e}",
            artifact_dir=args.artifact_dir,
            issue_number=args.issue_number,
            repo_root=args.repo_root,
        )
        return 2
    except Exception as e:
        _emit_failure_envelope(
            reason_code="schema_mismatch",
            next_action="human_judgment_required",
            detail=f"Input read error: {e}",
            artifact_dir=args.artifact_dir,
            issue_number=args.issue_number,
            repo_root=args.repo_root,
        )
        return 2

    # Convert (B3: compact_review_result does NOT write artifact)
    try:
        _compact, stdout_lines, artifact_path, artifact_content = compact_review_result(
            raw_result,
            artifact_dir=args.artifact_dir,
            issue_number=args.issue_number,
            repo_root=args.repo_root,
        )
    except (ValueError, _jsonschema.ValidationError, OSError) as e:
        _emit_failure_envelope(
            reason_code="schema_mismatch",
            next_action="human_judgment_required",
            detail=str(e),
            artifact_dir=args.artifact_dir,
            issue_number=args.issue_number,
            repo_root=args.repo_root,
        )
        return 2

    # Secret check on stdout output
    output_text = "\n".join(stdout_lines)
    violations = _no_secret_check(output_text)
    if violations:
        _emit_failure_envelope(
            reason_code="schema_mismatch",
            next_action="human_judgment_required",
            detail=f"secret-like strings detected in stdout: {violations}",
            artifact_dir=args.artifact_dir,
            issue_number=args.issue_number,
            repo_root=args.repo_root,
        )
        return 2

    # Issue #1541 High-1: enforce the 2048 UTF-8 byte limit on the EXACT wire
    # bytes (`wire_bytes()` -- lines joined by LF, plus the trailing LF the
    # final `print()` call below emits), BEFORE writing the success artifact.
    # The previous implementation measured `"\n".join(stdout_lines)` alone
    # (omitting that trailing LF), so a producer could self-measure exactly
    # 2048 bytes while the actual stdout it printed was 2049 bytes -- a
    # downstream consumer independently measuring the real wire bytes (e.g.
    # `emit_parent_review_envelope_v2.validate_child_intermediate()`) would
    # then reject output this producer believed was within budget.
    wire = wire_bytes(stdout_lines)
    byte_count = len(wire)
    if byte_count > 2048:
        # AC3: no success artifact written; save details to failure artifact only
        output_sha256 = hashlib.sha256(wire).hexdigest()
        _emit_failure_envelope(
            reason_code="output_budget_violation",
            next_action="human_judgment_required",
            detail=f"stdout exceeds 2048 UTF-8 bytes limit: {byte_count} bytes",
            artifact_dir=args.artifact_dir,
            issue_number=args.issue_number,
            repo_root=args.repo_root,
            extra={
                "byte_count": byte_count,
                "output_sha256": output_sha256,
                "bounded_preview": output_text[:256],
            },
        )
        return 2

    # Budget OK: now write success artifact atomically
    _atomic_write(artifact_path, artifact_content)

    # Output compact lines
    for line in stdout_lines:
        print(line, flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
