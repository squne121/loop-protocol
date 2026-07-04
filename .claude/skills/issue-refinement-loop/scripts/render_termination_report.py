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

import jsonschema
import yaml

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

# #1311: LOOP_HANDOFF_RESULT_V1 marker constants.
# SSOT for the schema and the Routing Rules table is
# .claude/skills/issue-refinement-loop/references/termination-policy.md
LOOP_HANDOFF_MARKER = "<!-- LOOP_HANDOFF_RESULT_V1 -->"
LOOP_HANDOFF_SCHEMA_PATH = (
    _SKILLS_ROOT / "issue-refinement-loop" / "schemas" / "loop_handoff_result_v1.json"
)

# references/termination-policy.md "Routing Rules" table (fixed subset
# retained as documentation; the actual enforcement is now done field-by-field
# in _validate_loop_handoff_policy() below, since this simple top-level map
# cannot express the gate_result / blockers / auto_fixes.skipped consistency
# rules the Routing Rules table requires).
LOOP_HANDOFF_ROUTING_RULES: dict[str, str] = {
    "impl_ready": "run_impl_review_loop",
    "blocked": "blocked",
    "human_judgment_required": "ask_human",
}

# gate_result values that are never compatible with status=impl_ready and
# always require status=blocked / routing_action=blocked per the Routing
# Rules table (references/termination-policy.md).
_BAD_GATE_RESULTS = frozenset(
    {"missing_go", "stale_go", "invalidated_by_request_changes", "blocked"}
)

# auto_fixes.required kinds that count as metadata-readiness auto-fix
# evidence (references/termination-policy.md impl_ready definition, item 3/4).
_METADATA_READY_AUTO_FIX_KINDS = frozenset({"metadata_hygiene", "template_hygiene"})

_loop_handoff_schema_cache: dict[str, Any] | None = None


def _get_loop_handoff_schema() -> dict[str, Any]:
    """Load and cache schemas/loop_handoff_result_v1.json (read-only reference)."""
    global _loop_handoff_schema_cache
    if _loop_handoff_schema_cache is None:
        with open(LOOP_HANDOFF_SCHEMA_PATH, encoding="utf-8") as f:
            _loop_handoff_schema_cache = json.load(f)
    return _loop_handoff_schema_cache


def _normalize_loop_handoff(loop_handoff: Any) -> Any:
    """
    Accept both the bare LOOP_HANDOFF_RESULT_V1 inner object and the schema's
    canonical wrapper form ``{"LOOP_HANDOFF_RESULT_V1": {...}}``, returning the
    inner object in both cases.

    Does not synthesize or derive any field from a partial/minimal payload:
    if the wrapper's value is not itself a dict, or the shape is otherwise
    ambiguous (e.g. extra sibling keys alongside the wrapper key), the input
    is returned unchanged so that downstream schema validation rejects it
    (fail-closed) rather than silently guessing at intent.
    """
    if (
        isinstance(loop_handoff, dict)
        and set(loop_handoff.keys()) == {"LOOP_HANDOFF_RESULT_V1"}
        and isinstance(loop_handoff["LOOP_HANDOFF_RESULT_V1"], dict)
    ):
        return loop_handoff["LOOP_HANDOFF_RESULT_V1"]
    return loop_handoff


def _validate_loop_handoff_policy(loop_handoff: dict[str, Any]) -> str:
    """
    Policy-level validator for references/termination-policy.md Routing Rules
    and the impl_ready definition.

    This function is a pure *verifier*: it checks whether the caller-supplied
    status/routing_action is consistent with the other loop_handoff fields
    (contract_review.gate_result, blockers, auto_fixes.skipped, permissions,
    metadata + auto_fixes.required evidence). It never derives or synthesizes
    status/routing_action from those fields -- it only rejects combinations
    that the Routing Rules table declares impossible. Combinations that the
    table cannot decide purely from these fields (e.g. status=
    human_judgment_required due to a semantic scope/goal/AC change, which
    has no dedicated field here) are left unrejected by this function.

    Returns "" on success, an error message string otherwise.
    """
    status = loop_handoff.get("status")
    routing_action = loop_handoff.get("routing_action")

    contract_review = loop_handoff.get("contract_review")
    contract_review = contract_review if isinstance(contract_review, dict) else {}
    gate_result = contract_review.get("gate_result")

    blockers = loop_handoff.get("blockers")
    blockers = blockers if isinstance(blockers, list) else []

    auto_fixes = loop_handoff.get("auto_fixes")
    auto_fixes = auto_fixes if isinstance(auto_fixes, dict) else {}
    skipped = auto_fixes.get("skipped")
    skipped = skipped if isinstance(skipped, list) else []
    required = auto_fixes.get("required")
    required = required if isinstance(required, list) else []

    permissions = loop_handoff.get("permissions")
    permissions = permissions if isinstance(permissions, dict) else {}
    unavailable = permissions.get("unavailable")
    unavailable = unavailable if isinstance(unavailable, list) else []

    metadata = loop_handoff.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}

    if gate_result in _BAD_GATE_RESULTS:
        if status != "blocked" or routing_action != "blocked":
            return (
                f"loop_handoff policy violation: contract_review.gate_result={gate_result!r} "
                "requires status=blocked and routing_action=blocked per "
                "references/termination-policy.md Routing Rules "
                f"(got status={status!r} routing_action={routing_action!r})"
            )

    if blockers:
        if status != "blocked" or routing_action != "blocked":
            return (
                "loop_handoff policy violation: non-empty blockers requires "
                "status=blocked and routing_action=blocked per "
                "references/termination-policy.md Routing Rules "
                f"(got status={status!r} routing_action={routing_action!r})"
            )

    if skipped:
        if status != "human_judgment_required" or routing_action != "ask_human":
            return (
                "loop_handoff policy violation: non-empty auto_fixes.skipped requires "
                "status=human_judgment_required and routing_action=ask_human per "
                "references/termination-policy.md Routing Rules "
                f"(got status={status!r} routing_action={routing_action!r})"
            )

    if status == "impl_ready":
        # The schema's allOf/if-then clause already structurally enforces:
        # routing_action == run_impl_review_loop, contract_review.status ==
        # go, contract_review.gate_result == fresh_go, blockers == [],
        # permissions.unavailable == [], auto_fixes.skipped == [] (all of
        # these are top-level required properties, so the "then" clause's
        # sibling constraints always apply once status == impl_ready).
        #
        # The metadata readiness / auto-fix evidence fallback (impl_ready
        # definition items 3-4 in references/termination-policy.md) is not
        # expressible in that schema clause and is enforced here instead.
        for ready_key in ("title_prefix_ready", "phase_label_ready"):
            if metadata.get(ready_key):
                continue
            applied = any(
                isinstance(item, dict)
                and item.get("kind") in _METADATA_READY_AUTO_FIX_KINDS
                and item.get("result") == "applied"
                and isinstance(item.get("evidence"), dict)
                for item in required
            )
            if not applied:
                return (
                    f"loop_handoff policy violation: status=impl_ready requires "
                    f"metadata.{ready_key}=true, or an applied auto_fixes.required "
                    f"entry (kind in {sorted(_METADATA_READY_AUTO_FIX_KINDS)}) with "
                    f"evidence, but metadata.{ready_key}=false and no matching "
                    "applied auto-fix entry was found"
                )

        if unavailable:
            return (
                "loop_handoff policy violation: status=impl_ready requires "
                f"permissions.unavailable to be empty, got {unavailable!r}"
            )

    return ""


# ---------------------------------------------------------------------------
# date-time field validation (Medium: jsonschema's FormatChecker does not
# register a "date-time" checker unless an optional dependency such as
# rfc3339-validator / strict-rfc3339 is installed, so schema-level format
# validation silently no-ops for these fields without one. This supplements
# it with a dependency-free structural + calendar check.)
# ---------------------------------------------------------------------------

_DATE_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


def _is_valid_date_time(value: Any) -> bool:
    """Minimal dependency-free RFC3339 date-time validator."""
    if not isinstance(value, str) or not _DATE_TIME_RE.match(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _validate_loop_handoff_date_time_fields(loop_handoff: dict[str, Any]) -> str:
    """Validate generated_at date-time fields. Returns "" on success."""
    generated_at = loop_handoff.get("generated_at")
    if generated_at is not None and not _is_valid_date_time(generated_at):
        return f"loop_handoff.generated_at is not a valid date-time: {generated_at!r}"

    contract_review = loop_handoff.get("contract_review")
    if isinstance(contract_review, dict):
        cr_generated_at = contract_review.get("generated_at")
        if cr_generated_at is not None and not _is_valid_date_time(cr_generated_at):
            return (
                "loop_handoff.contract_review.generated_at is not a valid "
                f"date-time: {cr_generated_at!r}"
            )

    return ""


def _validate_loop_handoff(loop_handoff: Any) -> str:
    """
    Validate a loop_handoff payload against schemas/loop_handoff_result_v1.json
    (jsonschema Draft7Validator + FormatChecker), the date-time fields, and
    the Routing Rules / impl_ready policy.

    Returns "" on success, an error message string otherwise. This function is
    the sole responsibility boundary for loop_handoff acceptance: it does not
    derive status/routing_action, it only validates a fully-formed payload.
    """
    if not isinstance(loop_handoff, dict):
        return f"loop_handoff must be an object, got {type(loop_handoff).__name__}"

    schema = _get_loop_handoff_schema()
    validator = jsonschema.Draft7Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )
    wrapped = {"LOOP_HANDOFF_RESULT_V1": loop_handoff}
    errors = sorted(validator.iter_errors(wrapped), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        return (
            f"loop_handoff schema validation failed: {first.message} "
            f"(path: {list(first.path)})"
        )

    date_time_err = _validate_loop_handoff_date_time_fields(loop_handoff)
    if date_time_err:
        return date_time_err

    return _validate_loop_handoff_policy(loop_handoff)


def _render_loop_handoff_marker(loop_handoff: dict[str, Any]) -> str:
    """
    Render the LOOP_HANDOFF_RESULT_V1 marker block.

    Output: <!-- LOOP_HANDOFF_RESULT_V1 --> HTML comment followed by a fenced
    YAML block generated via yaml.safe_dump (no hand-written string
    concatenation). Uses a dynamic fence (_make_dynamic_fence) so that
    backtick/tilde sequences embedded in loop_handoff string fields cannot
    break out of the fence (fence injection defense, mirrors blockers_summary
    handling in _render_normal_template).
    """
    payload = {"LOOP_HANDOFF_RESULT_V1": loop_handoff}
    yaml_text = yaml.safe_dump(
        payload,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip("\n")
    fence = _make_dynamic_fence(yaml_text)

    lines = [
        LOOP_HANDOFF_MARKER,
        f"{fence}yaml",
        yaml_text,
        fence,
    ]
    return "\n".join(lines)

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

# #1090 AC6: scope_signal_guard_decision_v2 routes that must surface
# missing-approval diagnostics in the termination report blockers.
SCOPE_SIGNAL_GUARD_V2_BLOCKER_ROUTES = frozenset({
    "human_judgment_required",
    "security_risk_gate_required",
    "invalid_scope_delta_approval",
})


def _scope_signal_guard_v2_blocker_lines(decision: dict[str, Any]) -> list[str]:
    """#1090 AC6: project SCOPE_SIGNAL_GUARD_DECISION_V2 into blocker strings.

    Emits the route itself, the missing_approval_field flag, and (when
    present) the suggested_contract_patch so the termination report tells a
    human the concrete next action, not just scope_signal_guard_reason_code.
    """
    route = decision.get("route")
    if route not in SCOPE_SIGNAL_GUARD_V2_BLOCKER_ROUTES:
        return []
    approval = decision.get("scope_delta_approval") or {}
    if not isinstance(approval, dict):
        approval = {}
    lines = [
        f"scope_signal_guard_route:{route}",
        f"missing_approval_field:{'true' if approval.get('missing_approval_field') else 'false'}",
    ]
    patch = approval.get("suggested_contract_patch")
    if isinstance(patch, str) and patch:
        lines.append(f"suggested_contract_patch:{patch}")
    return lines


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


def normalize_input(raw: Any) -> dict[str, Any]:
    """
    Normalize TERMINATION_REPORT_INPUT_V1 into canonical form.

    - `blocker_summary` is treated as a legacy alias for `blockers_summary`
    - alias conflicts fail closed
    - `human_escalation` without an explicit cause falls back to
      `human_judgment_required`
    - `scope_signal_guard_decision_v2` (optional, #1090 AC6) is projected
      into `blockers_summary` entries (route / missing_approval_field /
      suggested_contract_patch) and then removed from the payload
    - `loop_handoff` (optional, #1311) accepts either the bare inner object
      or the schema's canonical wrapper form
      ``{"LOOP_HANDOFF_RESULT_V1": {...}}`` and is normalized to the inner
      object (see _normalize_loop_handoff)
    """
    if not isinstance(raw, dict):
        raise InputValidationError("Input must be a JSON object")

    data = dict(raw)

    if "loop_handoff" in data and data["loop_handoff"] is not None:
        data["loop_handoff"] = _normalize_loop_handoff(data["loop_handoff"])

    if "scope_signal_guard_decision_v2" in data:
        decision = data.pop("scope_signal_guard_decision_v2")
        if decision is not None:
            if not isinstance(decision, dict):
                raise InputValidationError(
                    "scope_signal_guard_decision_v2 must be an object or null"
                )
            extra = _scope_signal_guard_v2_blocker_lines(decision)
            if extra:
                existing = data.get("blockers_summary")
                if existing is None:
                    existing = []
                if not isinstance(existing, list):
                    raise InputValidationError(
                        "blockers_summary must be list or null"
                    )
                merged = list(existing)
                for line in extra:
                    if line not in merged:
                        merged.append(line)
                data["blockers_summary"] = merged

    if "blocker_summary" in data:
        blocker_summary = data["blocker_summary"]
        if not isinstance(blocker_summary, list) or not all(
            isinstance(item, str) for item in blocker_summary
        ):
            raise InputValidationError("blocker_summary must be a list of strings")

        canonical = data.get("blockers_summary")
        if "blockers_summary" in data and canonical != blocker_summary:
            raise InputValidationError(
                "blocker_summary and blockers_summary conflict"
            )
        data["blockers_summary"] = blocker_summary
        data.pop("blocker_summary", None)

    if (
        data.get("termination_reason") == "human_escalation"
        and data.get("termination_cause") is None
    ):
        data["termination_cause"] = "human_judgment_required"

    return data


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
    data, err = _validate_input(data)
    if err:
        raise InputValidationError(err)

    generated_at = _now_iso()
    data.setdefault("generated_at", generated_at)

    termination_reason: str = data["termination_reason"]
    termination_cause: str | None = data.get("termination_cause")

    # #1311: LOOP_HANDOFF_RESULT_V1 marker is only emitted on approved
    # termination and only when a validated loop_handoff payload was provided.
    # loop_handoff is validated up-front in _validate_input() regardless of
    # termination_reason (fail-closed even for non-approved payloads).
    loop_handoff = data.get("loop_handoff")
    attach_loop_handoff_marker = (
        termination_reason == "approved" and loop_handoff is not None
    )

    attempts_log: list[dict] = []

    # Attempt 1: normal template
    body1 = _render_normal_template(data)
    if attach_loop_handoff_marker:
        body1 = body1 + "\n" + _render_loop_handoff_marker(loop_handoff) + "\n"
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
    # #1311: the marker (when applicable) is attached to the fallback body too,
    # so that a fallback-template render still carries LOOP_HANDOFF_RESULT_V1.
    # If this attempt also fails the guard, the whole render fails closed
    # (publishable=false, body=None) rather than silently dropping the marker
    # to force a "success".
    body2 = _render_fallback_minimal_template(data)
    if attach_loop_handoff_marker:
        body2 = body2 + "\n" + _render_loop_handoff_marker(loop_handoff) + "\n"
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
    try:
        data = normalize_input(raw)
    except InputValidationError as exc:
        return None, str(exc)

    termination_reason = data.get("termination_reason")
    if termination_reason not in VALID_TERMINATION_REASONS:
        return None, (
            f"Invalid termination_reason: {termination_reason!r}. "
            f"Must be one of: {sorted(VALID_TERMINATION_REASONS)}"
        )

    termination_cause = data.get("termination_cause")
    if termination_cause not in VALID_TERMINATION_CAUSES:
        return None, (
            f"Invalid termination_cause: {termination_cause!r}. "
            f"Must be one of: "
            f"{sorted(str(x) for x in VALID_TERMINATION_CAUSES if x is not None) + ['null']}"
        )

    issue_number = data.get("issue_number")
    if issue_number is not None:
        # Use type() not isinstance() so bool (subclass of int) is rejected (B3)
        if type(issue_number) is not int:
            return None, (
                f"issue_number must be int or null, got {type(issue_number).__name__}"
            )

    iteration = data.get("iteration")
    if iteration is not None:
        # Use type() not isinstance() so bool (subclass of int) is rejected (B3)
        if type(iteration) is not int:
            return None, (
                f"iteration must be int or null, got {type(iteration).__name__}"
            )

    blockers_summary = data.get("blockers_summary")
    if blockers_summary is not None:
        if not isinstance(blockers_summary, list):
            return None, (
                f"blockers_summary must be list or null, got {type(blockers_summary).__name__}"
            )
        # Each element must be a string (B3)
        if not all(isinstance(x, str) for x in blockers_summary):
            return None, "blockers_summary must be a list of strings"

    # #1311: loop_handoff is validated regardless of termination_reason
    # (fail-closed even when termination_reason != "approved" — an invalid
    # loop_handoff payload must not be silently ignored).
    loop_handoff = data.get("loop_handoff")
    if loop_handoff is not None:
        loop_handoff_err = _validate_loop_handoff(loop_handoff)
        if loop_handoff_err:
            return None, loop_handoff_err

    return data, ""


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
