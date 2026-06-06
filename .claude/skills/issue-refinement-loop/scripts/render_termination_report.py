#!/usr/bin/env python3
"""
render_termination_report.py

Guard-compatible deterministic renderer for issue-refinement-loop termination reports.

Produces TERMINATION_REPORT_RENDER_RESULT_V1 JSON on stdout.
All diagnostics go to stderr only.

Usage:
    python3 render_termination_report.py < input.json

Input (stdin): TERMINATION_REPORT_INPUT_V1 JSON
Output (stdout): TERMINATION_REPORT_RENDER_RESULT_V1 JSON

Exit codes:
    0 - success (publishable may be true or false)
    2 - invalid input schema
    3 - internal error

Design constraints:
    - No LLM / ask / network / gh command calls
    - No stdout prose markdown (only machine JSON)
    - Maximum 2 render attempts (normal template -> fallback minimal template)
    - prose_boundary_policy public API only (classify_block, iter_markdown_blocks)
    - Dynamic fence to prevent GFM fence injection
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# prose_boundary_policy import (public API only, read-only reference)
# ---------------------------------------------------------------------------

_SKILLS_ROOT = Path(__file__).resolve().parent.parent.parent
_CREATE_ISSUE_SCRIPTS = _SKILLS_ROOT / "create-issue" / "scripts"
sys.path.insert(0, str(_CREATE_ISSUE_SCRIPTS))

from prose_boundary_policy import classify_block, iter_markdown_blocks  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
RESULT_SCHEMA = "TERMINATION_REPORT_RENDER_RESULT_V1"

VALID_TERMINATION_REASONS = frozenset({
    "approved",
    "human_escalation",
    "superseded_by_decision",
})

VALID_TERMINATION_CAUSES = frozenset({
    "needs_fix_at_iteration_limit",
    "max_iterations_exceeded",
    "human_judgment_required",
    None,
})

GUARD_FAIL_REASON_CODE = "guard_fail_limit_exceeded"


# ---------------------------------------------------------------------------
# Dynamic fence helper (AC8: GFM fence injection prevention)
# ---------------------------------------------------------------------------

def _make_dynamic_fence(content: str) -> str:
    """
    Compute the shortest backtick fence that does not appear in content.

    Scans content for sequences of consecutive backticks and returns
    a fence of backtick length = max_found + 1 (minimum 3).
    This prevents GFM fence injection when content contains backtick sequences.
    """
    max_backticks = 0
    for m in re.finditer(r"`+", content):
        if len(m.group()) > max_backticks:
            max_backticks = len(m.group())
    fence_len = max(3, max_backticks + 1)
    return "`" * fence_len


# ---------------------------------------------------------------------------
# Guard runner (dry-run, no side effects)
# ---------------------------------------------------------------------------

def _run_guard(body: str) -> tuple[bool, list[str]]:
    """
    Run prose_boundary_policy dry-run guard on a markdown body.

    Uses only the public API: iter_markdown_blocks, classify_block.
    Does not re-implement regex or block classifier internals.

    Guard rule: a termination report should not contain shell_command or
    vc_command blocks (these indicate GFM injection or template corruption).

    Returns (pass_ok, errors):
      - pass_ok: True if guard passes
      - errors: list of diagnostic strings (for stderr only, never stdout)
    """
    errors: list[str] = []

    try:
        blocks = list(iter_markdown_blocks(body))
    except Exception as exc:
        errors.append(f"iter_markdown_blocks raised: {exc}")
        return False, errors

    for i, (block_text, _raw_kind) in enumerate(blocks):
        kind = classify_block(block_text)
        if kind in ("shell_command", "vc_command"):
            errors.append(
                f"Guard fail: block[{i}] classified as '{kind}' "
                f"(shell/vc content unexpected in termination report). "
                f"Snippet: {block_text[:60]!r}"
            )

    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# Template renderers (deterministic, no LLM, no network, no gh)
# ---------------------------------------------------------------------------

def _render_normal_template(data: dict[str, Any]) -> str:
    """
    Render the normal (full) termination report template.

    Returns a GFM markdown string. Uses dynamic fence for any embedded content.
    No shell commands, no VC commands are included.
    """
    termination_reason: str = data["termination_reason"]
    termination_cause: str | None = data.get("termination_cause")
    issue_number: int | None = data.get("issue_number")
    iteration: int | None = data.get("iteration")
    blockers_summary: list[str] = data.get("blockers_summary") or []
    generated_at: str = data.get("generated_at", _now_iso())

    if termination_reason == "approved":
        headline = "Refinement Loop: Approved"
        status_line = (
            "The issue has been approved and is ready for implementation."
        )
    elif termination_reason == "human_escalation":
        headline = "Refinement Loop: Human Escalation Required"
        cause_label = _cause_label(termination_cause)
        status_line = (
            f"The loop has terminated and requires human review. "
            f"Cause: {cause_label}"
        )
    elif termination_reason == "superseded_by_decision":
        headline = "Refinement Loop: Superseded by Decision"
        status_line = "The issue has been superseded by an earlier decision."
    else:
        headline = f"Refinement Loop: {termination_reason}"
        status_line = "The loop has terminated."

    lines: list[str] = []
    lines.append(f"## {headline}")
    lines.append("")
    lines.append(status_line)
    lines.append("")

    # Metadata
    if issue_number is not None:
        lines.append(f"- Issue: #{issue_number}")
    if iteration is not None:
        lines.append(f"- Final iteration: {iteration}")
    lines.append(f"- Termination reason: `{termination_reason}`")
    if termination_cause is not None:
        lines.append(f"- Termination cause: `{termination_cause}`")
    lines.append(f"- Generated at: {generated_at}")
    lines.append("")

    # Blockers section (human_escalation only)
    # Use _make_dynamic_fence to prevent GFM fence injection (B1).
    # Each blocker is wrapped in a literal block with a fence that cannot
    # appear inside the blocker text, regardless of backtick sequences.
    if termination_reason == "human_escalation" and blockers_summary:
        lines.append("## Blockers")
        lines.append("")
        for b in blockers_summary:
            serialized = json.dumps(b, ensure_ascii=False)
            fence = _make_dynamic_fence(serialized)
            lines.append(f"{fence}text")
            lines.append(serialized)
            lines.append(fence)
            lines.append("")

    return "\n".join(lines)


def _render_fallback_minimal_template(data: dict[str, Any]) -> str:
    """
    Render the fallback minimal termination report template.

    Single-paragraph prose format with no embedded code/commands/fences.
    Designed to pass guard even when the normal template fails.
    """
    termination_reason: str = data["termination_reason"]
    termination_cause: str | None = data.get("termination_cause")
    issue_number: int | None = data.get("issue_number")
    generated_at: str = data.get("generated_at", _now_iso())

    cause_label = _cause_label(termination_cause) if termination_cause else ""

    parts: list[str] = [
        f"## Loop Termination: {termination_reason}",
        "",
    ]
    summary_parts = [f"Reason: {termination_reason}"]
    if cause_label:
        summary_parts.append(f"cause: {cause_label}")
    if issue_number is not None:
        summary_parts.append(f"issue: #{issue_number}")
    summary_parts.append(f"generated: {generated_at}")

    parts.append(". ".join(summary_parts) + ".")
    parts.append("")

    return "\n".join(parts)


def _cause_label(termination_cause: str | None) -> str:
    """Human-readable label for termination_cause."""
    if termination_cause is None:
        return "none"
    labels = {
        "needs_fix_at_iteration_limit": "needs-fix at iteration limit",
        "max_iterations_exceeded": "max iterations exceeded",
        "human_judgment_required": "human judgment required",
    }
    return labels.get(termination_cause, termination_cause)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Main render logic (AC4: max 2 attempts, no LLM/ask/network/gh)
# ---------------------------------------------------------------------------

class InputValidationError(ValueError):
    """Raised when input fails validation (used internally)."""


def render(data: dict[str, Any]) -> dict[str, Any]:
    """
    Attempt to render the termination report with guard validation.

    Validates input first (N1: validation runs even when called as library).
    Attempt 1: normal template
    Attempt 2 (if guard fails): fallback minimal template
    If both fail: publishable=false, body=null, reason_code=GUARD_FAIL_REASON_CODE

    Returns TERMINATION_REPORT_RENDER_RESULT_V1 dict.
    """
    # N1: validate input at render() entry so library callers also get validation
    _, err = _validate_input(data)
    if err:
        raise InputValidationError(err)

    generated_at = _now_iso()
    data = dict(data)
    data.setdefault("generated_at", generated_at)

    termination_reason: str = data["termination_reason"]
    termination_cause: str | None = data.get("termination_cause")

    attempts_log: list[dict] = []

    # Attempt 1: normal template
    body1 = _render_normal_template(data)
    ok1, errs1 = _run_guard(body1)
    attempts_log.append({
        "attempt": 1,
        "template": "normal",
        "guard_pass": ok1,
        "errors": errs1,
    })

    if ok1:
        return _result_ok(
            body=body1,
            termination_reason=termination_reason,
            termination_cause=termination_cause,
            attempts=1,
            attempts_log=attempts_log,
            generated_at=generated_at,
        )

    # Guard failed on attempt 1 - log to stderr only (never to stdout body)
    print(f"[render_termination_report] attempt 1 guard fail: {errs1}", file=sys.stderr)

    # Attempt 2: fallback minimal template
    body2 = _render_fallback_minimal_template(data)
    ok2, errs2 = _run_guard(body2)
    attempts_log.append({
        "attempt": 2,
        "template": "fallback_minimal",
        "guard_pass": ok2,
        "errors": errs2,
    })

    if ok2:
        return _result_ok(
            body=body2,
            termination_reason=termination_reason,
            termination_cause=termination_cause,
            attempts=2,
            attempts_log=attempts_log,
            generated_at=generated_at,
        )

    # Both attempts failed (AC5)
    print(
        f"[render_termination_report] attempt 2 guard fail: {errs2}. "
        "Returning publishable=false.",
        file=sys.stderr,
    )

    return {
        "schema": RESULT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "publishable": False,
        "body": None,
        "reason_code": GUARD_FAIL_REASON_CODE,
        "termination_reason": termination_reason,
        "termination_cause": termination_cause,
        "attempts": 2,
        "attempts_log": attempts_log,
        "generated_at": generated_at,
    }


def _result_ok(
    *,
    body: str,
    termination_reason: str,
    termination_cause: str | None,
    attempts: int,
    attempts_log: list[dict],
    generated_at: str,
) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "publishable": True,
        "body": body,
        "reason_code": None,
        "termination_reason": termination_reason,
        "termination_cause": termination_cause,
        "attempts": attempts,
        "attempts_log": attempts_log,
        "generated_at": generated_at,
    }


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def _validate_input(raw: Any) -> tuple[dict[str, Any] | None, str]:
    """
    Validate TERMINATION_REPORT_INPUT_V1.

    Returns (validated_data, error_message).
    error_message is empty string on success.
    """
    if not isinstance(raw, dict):
        return None, "Input must be a JSON object"

    termination_reason = raw.get("termination_reason")
    if termination_reason not in VALID_TERMINATION_REASONS:
        return None, (
            f"Invalid termination_reason: {termination_reason!r}. "
            f"Must be one of: {sorted(VALID_TERMINATION_REASONS)}"
        )

    termination_cause = raw.get("termination_cause")
    if termination_cause not in VALID_TERMINATION_CAUSES:
        return None, (
            f"Invalid termination_cause: {termination_cause!r}. "
            f"Must be one of: "
            f"{sorted(str(x) for x in VALID_TERMINATION_CAUSES if x is not None) + ['null']}"
        )

    issue_number = raw.get("issue_number")
    if issue_number is not None:
        # Use type() not isinstance() so bool (subclass of int) is rejected (B3)
        if type(issue_number) is not int:
            return None, (
                f"issue_number must be int or null, got {type(issue_number).__name__}"
            )

    iteration = raw.get("iteration")
    if iteration is not None:
        # Use type() not isinstance() so bool (subclass of int) is rejected (B3)
        if type(iteration) is not int:
            return None, (
                f"iteration must be int or null, got {type(iteration).__name__}"
            )

    blockers_summary = raw.get("blockers_summary")
    if blockers_summary is not None:
        if not isinstance(blockers_summary, list):
            return None, (
                f"blockers_summary must be list or null, got {type(blockers_summary).__name__}"
            )
        # Each element must be a string (B3)
        if not all(isinstance(x, str) for x in blockers_summary):
            return None, "blockers_summary must be a list of strings"

    return raw, ""


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        raw_input = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        result = {
            "schema": RESULT_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "publishable": False,
            "body": None,
            "reason_code": "invalid_input",
            "error": f"JSON decode error: {exc}",
        }
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 2

    data, err = _validate_input(raw_input)
    if err:
        result = {
            "schema": RESULT_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "publishable": False,
            "body": None,
            "reason_code": "invalid_input",
            "error": err,
        }
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 2

    try:
        result = render(data)
    except Exception as exc:
        # Internal error: English-only message, no stack trace on stdout
        print(f"[render_termination_report] internal error: {exc}", file=sys.stderr)
        result = {
            "schema": RESULT_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "publishable": False,
            "body": None,
            "reason_code": "internal_error",
            "error": "Internal error. See stderr for diagnostics.",
        }
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 3

    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
