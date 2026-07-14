#!/usr/bin/env python3
"""
validate_review_compact_output.py - REVIEW_COMPACT_VALIDATION_RESULT_V1

Deterministically validates that the final text returned by the
`issue-reviewer` SubAgent (`ISSUE_REVIEW_RESULT_COMPACT_V1`) exactly matches
one of three mutually-exclusive canonical envelope grammars, so that the
`issue-refinement-loop` orchestrator can fail-closed to
`human_judgment_required` instead of accepting fabricated / malformed prose
(Issue #1507; triggered by the producer failure captured in
`.claude/artifacts/issue-refinement-loop/1501/producer_failure_schema_mismatch_20260713T215634Z.json`).

Envelopes (field ordering SSOT: `compact_review_result.py` / `reviewer_claim_replay.py`):

  - approve envelope (8 lines, exact):
        STATUS / VERDICT / SUMMARY / BLOCKERS / NEXT_ACTION / MUST_READ /
        EVIDENCE / ARTIFACT
    `REPLAY_*` fields MUST NOT be present.

  - needs-fix envelope (13 lines, exact): the 8 approve fields, followed by
        REPLAY_VERDICT / REPLAY_ROUTING / REPLAY_SHOULD_CONSUME /
        REPLAY_BODY_SHA256 / REPLAY_ARTIFACT_DIGEST

  - producer-failure envelope (5 lines, exact):
        STATUS / NEXT_ACTION / REASON_CODE / ARTIFACT / ARTIFACT_SHA256
    This envelope is syntactically valid but ALWAYS treated as
    `validation_status: invalid` / `next_action: human_judgment_required`
    (#1165 canonical failure envelope SSOT).

`REPLAY_VERDICT` is the 5-value enum synchronized with
`reviewer_claim_replay.py` (`_LEGACY_VERDICT_MAP_V1` / the top-level
`verdict` field returned by `analyze()`):

    deterministic_fail_confirmed
    checker_artifact_inconsistency
    reviewer_claim_unbacked_by_deterministic_checker
    reviewer_false_positive_suspected
    input_or_runtime_error

Any input that does not match one of the three grammars exactly (missing /
duplicate / unknown / out-of-order fields, leading/trailing prose, Markdown
code fences, blank lines, ANSI escapes, NUL / other control characters,
input exceeding 2048 UTF-8 bytes, whitespace around keys/values) is rejected
as `validation_status: invalid`. Injection attempts that concatenate a
producer-failure envelope with a forged approve envelope are rejected by the
exact ordered-field-sequence check (a concatenation never matches any of the
three canonical field sequences, even when the total line count happens to
coincide with the needs-fix envelope's 13 lines).

Issue #1507 P0-3 / P1-1 (AC15-AC20): active issue namespace binding and
producer-derived field invariants.

  - `--issue-number` (positive int, required on the CLI) binds the `ARTIFACT`
    issue segment to the active issue. A mismatched, `unknown`, `0`, or
    leading-zero segment is always rejected (AC15/AC16), independent of
    whether `--issue-number` was supplied to `validate_review_compact_output`
    directly (the pure function defaults `issue_number=None`, in which case
    only the `unknown`/`0`/leading-zero checks apply).
  - `MUST_READ` must always be empty (AC17); `EVIDENCE` must exactly equal
    the `ARTIFACT` path with its `compact_review_result_v1=` prefix stripped
    (AC18); the `ARTIFACT` filename (final path segment) for approve/
    needs-fix envelopes must match `compact_review_result_YYYYMMDDTHHMMSSZ.json`
    (AC19); `SUMMARY` must be exactly `contract ready` for approve, or match
    `N blocker(s)(; first=<code>)?` for needs-fix (AC20).

Usage:
    <subagent stdout text> | uv run python3 validate_review_compact_output.py --issue-number <N>
    uv run python3 validate_review_compact_output.py --input-file <path> --issue-number <N>

stdout: exactly one JSON object, schema `REVIEW_COMPACT_VALIDATION_RESULT_V1`.
Human-oriented diagnostics (if any) go to stderr only; stdout is
machine-only and MUST be parsed as JSON by callers.

Exit codes:
    0 - valid                                  (validation_status: valid)
    1 - contract-invalid                       (validation_status: invalid)
    2 - validator runtime/input/environment error
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from typing import Any

SCHEMA = "REVIEW_COMPACT_VALIDATION_RESULT_V1"
SCHEMA_VERSION = "1"

MAX_INPUT_BYTES = 2048

# ---------------------------------------------------------------------------
# Canonical field sequences (SSOT: compact_review_result.py / SKILL.md Step 2a)
# ---------------------------------------------------------------------------

APPROVE_FIELDS: list[str] = [
    "STATUS",
    "VERDICT",
    "SUMMARY",
    "BLOCKERS",
    "NEXT_ACTION",
    "MUST_READ",
    "EVIDENCE",
    "ARTIFACT",
]

NEEDS_FIX_FIELDS: list[str] = APPROVE_FIELDS + [
    "REPLAY_VERDICT",
    "REPLAY_ROUTING",
    "REPLAY_SHOULD_CONSUME",
    "REPLAY_BODY_SHA256",
    "REPLAY_ARTIFACT_DIGEST",
]

PRODUCER_FAILURE_FIELDS: list[str] = [
    "STATUS",
    "NEXT_ACTION",
    "REASON_CODE",
    "ARTIFACT",
    "ARTIFACT_SHA256",
]

_ENVELOPE_TEMPLATES: dict[str, list[str]] = {
    "approve": APPROVE_FIELDS,
    "needs_fix": NEEDS_FIX_FIELDS,
    "producer_failure": PRODUCER_FAILURE_FIELDS,
}

ALL_KNOWN_FIELDS: frozenset[str] = (
    frozenset(APPROVE_FIELDS) | frozenset(NEEDS_FIX_FIELDS) | frozenset(PRODUCER_FAILURE_FIELDS)
)

# ---------------------------------------------------------------------------
# Value enums (SSOT: compact_review_result.py VALID_* constants)
# ---------------------------------------------------------------------------

VALID_STATUSES: frozenset[str] = frozenset({"ok", "failed"})
VALID_VERDICTS: frozenset[str] = frozenset({"approve", "needs-fix"})
VALID_NEXT_ACTIONS: frozenset[str] = frozenset(
    {"proceed", "request_changes", "human_judgment_required"}
)

# REPLAY_VERDICT 5-value enum (SSOT: reviewer_claim_replay.py _LEGACY_VERDICT_MAP_V1)
VALID_REPLAY_VERDICTS: frozenset[str] = frozenset(
    {
        "deterministic_fail_confirmed",
        "checker_artifact_inconsistency",
        "reviewer_claim_unbacked_by_deterministic_checker",
        "reviewer_false_positive_suspected",
        "input_or_runtime_error",
    }
)

# REPLAY_VERDICT -> (REPLAY_ROUTING, REPLAY_SHOULD_CONSUME) canonical matrix.
# Synchronized with reviewer_claim_replay.py (verdict / routing /
# should_consume_iteration); see SKILL.md Step 2a.
REPLAY_MATRIX: dict[str, tuple[str, str]] = {
    "deterministic_fail_confirmed": ("proceed_to_rewrite", "true"),
    "checker_artifact_inconsistency": ("fix_checker_artifact", "false"),
    "reviewer_claim_unbacked_by_deterministic_checker": ("downgrade_to_non_blocking", "false"),
    "reviewer_false_positive_suspected": ("human_escalation", "false"),
    "input_or_runtime_error": ("human_judgment_required", "false"),
}

VALID_REPLAY_ROUTINGS: frozenset[str] = frozenset(
    routing for routing, _ in REPLAY_MATRIX.values()
)
VALID_REPLAY_SHOULD_CONSUME: frozenset[str] = frozenset({"true", "false"})

# ---------------------------------------------------------------------------
# Lexical patterns
# ---------------------------------------------------------------------------

_FIELD_LINE_RE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*): (?P<value>.*)$")
_BLOCKERS_RE = re.compile(r"^(0|[1-9][0-9]*)$")
_SHA256_PREFIXED_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256_PLAIN_RE = re.compile(r"^[0-9a-f]{64}$")

# ARTIFACT path lexical shape (active issue namespace, repo-relative).
# Absolute paths and `..` traversal are rejected before this pattern is
# even consulted (see _artifact_value_violations). This validator performs
# lexical validation ONLY -- it never opens, stats, or reads the referenced
# file (Issue #1472 isolation worktree boundary; #1507 P0-2).
_ARTIFACT_PATH_RE = re.compile(
    r"^\.claude/artifacts/issue-refinement-loop/(?P<segment>[0-9]+|unknown)/(?P<filename>[A-Za-z0-9_.-]+\.json)$"
)

# AC19: canonical compact_review_result artifact filename shape.
_COMPACT_FILENAME_RE = re.compile(r"^compact_review_result_[0-9]{8}T[0-9]{6}Z\.json$")

# AC20: needs-fix SUMMARY invariant shape.
_SUMMARY_NEEDS_FIX_RE = re.compile(r"^[0-9]+ blocker\(s\)(; first=.{1,60})?$")

_COMPACT_ARTIFACT_PREFIX = "compact_review_result_v1="


def _violation(code: str, **extra: Any) -> dict[str, Any]:
    v: dict[str, Any] = {"code": code}
    v.update(extra)
    return v


# ---------------------------------------------------------------------------
# Lexical / structural scanning
# ---------------------------------------------------------------------------


def _scan_control_chars(text: str) -> list[dict[str, Any]]:
    """Reject ANSI escapes, NUL, CR/CRLF, and other C0/DEL control chars.

    `\\n` is the canonical line separator and is always allowed.
    """
    violations: list[dict[str, Any]] = []
    if "\x1b" in text:
        violations.append(_violation("ansi_escape_detected"))
    if "\r" in text:
        violations.append(_violation("crlf_detected"))
    for ch in text:
        if ch in ("\n", "\x1b", "\r"):
            continue
        code_point = ord(ch)
        if code_point < 0x20 or code_point == 0x7F:
            violations.append(_violation("control_char_detected", char=f"\\x{code_point:02x}"))
    return violations


def _split_lines(text: str) -> list[str]:
    """Split on `\\n`, tolerating exactly one trailing newline.

    A second trailing newline (or any interior blank line) surfaces as a
    `blank_line_detected` violation once the split lines are inspected.
    """
    body = text[:-1] if text.endswith("\n") else text
    return body.split("\n")


def _parse_lines(
    lines: list[str],
) -> tuple[list[str], dict[str, str], list[dict[str, Any]]]:
    """Parse raw lines into (ordered_keys, field_values, violations).

    Lines that do not match the `KEY: value` grammar are recorded as
    `prose_prefix` / `prose_suffix` / `malformed_line` violations and
    contribute no key to `ordered_keys`.
    """
    ordered_keys: list[str] = []
    field_values: dict[str, str] = {}
    violations: list[dict[str, Any]] = []

    for index, line in enumerate(lines):
        if line == "":
            violations.append(_violation("blank_line_detected", line_index=index))
            continue
        if "```" in line:
            violations.append(_violation("code_fence_detected", line_index=index))
            continue
        match = _FIELD_LINE_RE.match(line)
        if match is None:
            if index == 0:
                code = "prose_prefix"
            elif index == len(lines) - 1:
                code = "prose_suffix"
            else:
                code = "malformed_line"
            violations.append(_violation(code, line_index=index, line=line))
            continue
        key = match.group("key")
        value = match.group("value")
        if key not in ALL_KNOWN_FIELDS:
            violations.append(_violation("unknown_field", field=key, line_index=index))
            continue
        if value != value.strip():
            violations.append(_violation("value_whitespace_violation", field=key, value=value))
        if key in field_values:
            violations.append(_violation("duplicate_field", field=key, line_index=index))
            # Keep the first occurrence's ordering position; do not overwrite value.
            continue
        ordered_keys.append(key)
        field_values[key] = value

    return ordered_keys, field_values, violations


def _classify_envelope(ordered_keys: list[str]) -> str | None:
    """Return the exact-matching envelope name, or None if no exact match."""
    for name, template in _ENVELOPE_TEMPLATES.items():
        if ordered_keys == template:
            return name
    return None


def _closest_template_name(ordered_keys: list[str]) -> str:
    """Best-effort guess of the intended envelope for missing/unknown/order
    diagnostics when no exact match was found. This is purely diagnostic
    (does not affect validation_status, which is always `invalid` in this
    branch)."""
    keys = set(ordered_keys)
    if "REASON_CODE" in keys or "ARTIFACT_SHA256" in keys:
        return "producer_failure"
    if any(k.startswith("REPLAY_") for k in keys):
        return "needs_fix"
    if "VERDICT" in keys:
        return "approve"
    # Fall back to the template with the largest field-set overlap.
    best_name = "approve"
    best_overlap = -1
    for name, template in _ENVELOPE_TEMPLATES.items():
        overlap = len(keys & set(template))
        if overlap > best_overlap:
            best_overlap = overlap
            best_name = name
    return best_name


def _diff_violations(ordered_keys: list[str]) -> list[dict[str, Any]]:
    """Compute missing/unknown-already-reported/out-of-order diagnostics
    against the closest template for a non-exact-match key sequence."""
    violations: list[dict[str, Any]] = []
    template_name = _closest_template_name(ordered_keys)
    template = _ENVELOPE_TEMPLATES[template_name]
    template_set = set(template)
    keys_set = set(ordered_keys)

    missing = [k for k in template if k not in keys_set]
    for field in missing:
        violations.append(_violation("missing_field", field=field, template=template_name))

    if keys_set == template_set and ordered_keys != template:
        violations.append(_violation("out_of_order_field", template=template_name))

    return violations


# ---------------------------------------------------------------------------
# Value / cross-field validation
# ---------------------------------------------------------------------------


def _artifact_value_violations(
    field: str,
    prefix: str,
    value: str,
    *,
    issue_number: int | None = None,
    check_filename_pattern: bool = False,
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    if not value.startswith(prefix):
        violations.append(
            _violation("artifact_prefix_invalid", field=field, expected_prefix=prefix, value=value)
        )
        return violations
    path = value[len(prefix) :]
    if path.startswith("/"):
        violations.append(_violation("artifact_absolute_path_rejected", field=field, value=value))
        return violations
    if ".." in path.split("/"):
        violations.append(_violation("artifact_parent_traversal_rejected", field=field, value=value))
        return violations
    match = _ARTIFACT_PATH_RE.match(path)
    if not match:
        violations.append(_violation("artifact_path_invalid", field=field, value=value))
        return violations

    # AC15/AC16: active issue namespace binding (independent of each other).
    segment = match.group("segment")
    if segment == "unknown":
        violations.append(
            _violation("artifact_issue_segment_unknown_rejected", field=field, value=value)
        )
    elif segment == "0" or (len(segment) > 1 and segment[0] == "0"):
        violations.append(
            _violation(
                "artifact_issue_segment_zero_or_leading_zero_rejected",
                field=field,
                value=value,
                segment=segment,
            )
        )
    elif issue_number is not None and int(segment) != int(issue_number):
        violations.append(
            _violation(
                "artifact_issue_number_mismatch",
                field=field,
                value=value,
                segment=segment,
                expected_issue_number=issue_number,
            )
        )

    # AC19: canonical compact_review_result filename shape (approve/needs-fix only).
    if check_filename_pattern:
        filename = match.group("filename")
        if not _COMPACT_FILENAME_RE.match(filename):
            violations.append(
                _violation("artifact_filename_pattern_invalid", field=field, value=value, filename=filename)
            )

    return violations


def _common_field_invariants(fields: dict[str, str], envelope_kind: str) -> list[dict[str, Any]]:
    """AC17/AC18/AC20 producer-derived value invariants shared by the
    approve and needs-fix envelopes (producer-failure envelope does not
    carry these fields)."""
    violations: list[dict[str, Any]] = []

    must_read = fields.get("MUST_READ", "")
    if must_read != "":
        violations.append(_violation("must_read_non_empty_rejected", value=must_read))

    artifact = fields.get("ARTIFACT", "")
    evidence = fields.get("EVIDENCE", "")
    if artifact.startswith(_COMPACT_ARTIFACT_PREFIX):
        expected_evidence = artifact[len(_COMPACT_ARTIFACT_PREFIX) :]
        if evidence != expected_evidence:
            violations.append(
                _violation(
                    "evidence_artifact_mismatch",
                    evidence=evidence,
                    expected=expected_evidence,
                )
            )

    summary = fields.get("SUMMARY", "")
    if envelope_kind == "approve":
        if summary != "contract ready":
            violations.append(
                _violation("summary_invariant_invalid", envelope="approve", value=summary)
            )
    elif envelope_kind == "needs_fix":
        if not _SUMMARY_NEEDS_FIX_RE.match(summary):
            violations.append(
                _violation("summary_invariant_invalid", envelope="needs_fix", value=summary)
            )

    return violations


def _validate_approve_values(
    fields: dict[str, str], *, issue_number: int | None = None
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    status = fields.get("STATUS", "")
    verdict = fields.get("VERDICT", "")
    next_action = fields.get("NEXT_ACTION", "")
    blockers = fields.get("BLOCKERS", "")
    artifact = fields.get("ARTIFACT", "")

    if status not in VALID_STATUSES:
        violations.append(_violation("status_value_invalid", value=status))
    if status != "ok":
        violations.append(_violation("approve_status_must_be_ok", value=status))
    if verdict != "approve":
        violations.append(_violation("verdict_value_invalid", expected="approve", value=verdict))
    if next_action != "proceed":
        violations.append(_violation("next_action_value_invalid", expected="proceed", value=next_action))
    if not _BLOCKERS_RE.match(blockers):
        violations.append(_violation("blockers_invalid_format", value=blockers))
    elif blockers != "0":
        violations.append(_violation("approve_blockers_must_be_zero", value=blockers))
    violations.extend(
        _artifact_value_violations(
            "ARTIFACT",
            _COMPACT_ARTIFACT_PREFIX,
            artifact,
            issue_number=issue_number,
            check_filename_pattern=True,
        )
    )
    violations.extend(_common_field_invariants(fields, "approve"))
    return violations


def _validate_needs_fix_values(
    fields: dict[str, str], *, issue_number: int | None = None
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    status = fields.get("STATUS", "")
    verdict = fields.get("VERDICT", "")
    next_action = fields.get("NEXT_ACTION", "")
    blockers = fields.get("BLOCKERS", "")
    artifact = fields.get("ARTIFACT", "")
    replay_verdict = fields.get("REPLAY_VERDICT", "")
    replay_routing = fields.get("REPLAY_ROUTING", "")
    replay_should_consume = fields.get("REPLAY_SHOULD_CONSUME", "")
    replay_body_sha256 = fields.get("REPLAY_BODY_SHA256", "")
    replay_artifact_digest = fields.get("REPLAY_ARTIFACT_DIGEST", "")

    if status not in VALID_STATUSES:
        violations.append(_violation("status_value_invalid", value=status))
    if verdict != "needs-fix":
        violations.append(_violation("verdict_value_invalid", expected="needs-fix", value=verdict))
    if next_action not in {"request_changes", "human_judgment_required"}:
        violations.append(
            _violation(
                "next_action_value_invalid",
                expected="request_changes|human_judgment_required",
                value=next_action,
            )
        )
    if not _BLOCKERS_RE.match(blockers):
        violations.append(_violation("blockers_invalid_format", value=blockers))
    elif blockers == "0":
        violations.append(_violation("needs_fix_blockers_must_be_nonzero", value=blockers))
    violations.extend(
        _artifact_value_violations(
            "ARTIFACT",
            _COMPACT_ARTIFACT_PREFIX,
            artifact,
            issue_number=issue_number,
            check_filename_pattern=True,
        )
    )
    violations.extend(_common_field_invariants(fields, "needs_fix"))

    if replay_verdict not in VALID_REPLAY_VERDICTS:
        violations.append(_violation("replay_verdict_invalid_enum", value=replay_verdict))
    else:
        expected_routing, expected_should_consume = REPLAY_MATRIX[replay_verdict]
        if replay_routing != expected_routing or replay_should_consume != expected_should_consume:
            violations.append(
                _violation(
                    "replay_verdict_routing_mismatch",
                    replay_verdict=replay_verdict,
                    expected_routing=expected_routing,
                    expected_should_consume=expected_should_consume,
                    actual_routing=replay_routing,
                    actual_should_consume=replay_should_consume,
                )
            )
    if replay_should_consume not in VALID_REPLAY_SHOULD_CONSUME:
        violations.append(_violation("replay_should_consume_invalid_literal", value=replay_should_consume))
    if not _SHA256_PREFIXED_RE.match(replay_body_sha256):
        violations.append(_violation("replay_body_sha256_invalid_format", value=replay_body_sha256))
    if not _SHA256_PREFIXED_RE.match(replay_artifact_digest):
        violations.append(_violation("replay_artifact_digest_invalid_format", value=replay_artifact_digest))

    return violations


def _validate_producer_failure_values(
    fields: dict[str, str], *, issue_number: int | None = None
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    status = fields.get("STATUS", "")
    next_action = fields.get("NEXT_ACTION", "")
    reason_code = fields.get("REASON_CODE", "")
    artifact = fields.get("ARTIFACT", "")
    artifact_sha256 = fields.get("ARTIFACT_SHA256", "")

    if status != "failed":
        violations.append(_violation("producer_failure_status_must_be_failed", value=status))
    if next_action != "human_judgment_required":
        violations.append(
            _violation(
                "producer_failure_next_action_must_be_human_judgment_required", value=next_action
            )
        )
    if not reason_code:
        violations.append(_violation("reason_code_empty"))
    violations.extend(
        _artifact_value_violations(
            "ARTIFACT",
            "producer_failure_v1=",
            artifact,
            issue_number=issue_number,
            check_filename_pattern=False,
        )
    )
    if not _SHA256_PLAIN_RE.match(artifact_sha256):
        violations.append(_violation("artifact_sha256_invalid_format", value=artifact_sha256))

    return violations


# ---------------------------------------------------------------------------
# Top-level validate()
# ---------------------------------------------------------------------------


def validate_review_compact_output(
    raw_text: str, *, issue_number: int | None = None
) -> dict[str, Any]:
    """Validate `raw_text` against the three canonical envelope grammars.

    `issue_number` (Issue #1507 AC15/AC16, optional for direct callers,
    required on the CLI): binds the `ARTIFACT` issue segment to the active
    issue. When omitted, the segment-shape invariants (not `unknown`, not
    `0`/leading-zero) still apply; only the exact-match binding to a
    specific issue number is skipped.

    Returns a dict with keys: validation_status, envelope_kind,
    normalized_payload, violations, next_action, artifact_path_policy.
    Does NOT include input_sha256 / input_byte_count (caller's
    responsibility, since those are computed over the exact original bytes
    before UTF-8 decoding).
    """
    byte_count = len(raw_text.encode("utf-8"))
    if byte_count > MAX_INPUT_BYTES:
        return {
            "validation_status": "invalid",
            "envelope_kind": "unknown",
            "normalized_payload": None,
            "violations": [_violation("byte_budget_exceeded", byte_count=byte_count, limit=MAX_INPUT_BYTES)],
            "next_action": "human_judgment_required",
            "artifact_path_policy": {"status": "not_applicable", "path": None},
        }

    if raw_text == "":
        return {
            "validation_status": "invalid",
            "envelope_kind": "unknown",
            "normalized_payload": None,
            "violations": [_violation("empty_input")],
            "next_action": "human_judgment_required",
            "artifact_path_policy": {"status": "not_applicable", "path": None},
        }

    control_violations = _scan_control_chars(raw_text)

    lines = _split_lines(raw_text)
    ordered_keys, fields, structural_violations = _parse_lines(lines)

    violations: list[dict[str, Any]] = list(control_violations) + list(structural_violations)

    has_malformed_line = any(
        v["code"] in {"prose_prefix", "prose_suffix", "malformed_line", "unknown_field", "duplicate_field"}
        for v in structural_violations
    )

    envelope_kind_exact = _classify_envelope(ordered_keys) if not has_malformed_line else None

    if envelope_kind_exact is None:
        violations.extend(_diff_violations(ordered_keys))
        return {
            "validation_status": "invalid",
            "envelope_kind": "unknown",
            "normalized_payload": None,
            "violations": violations,
            "next_action": "human_judgment_required",
            "artifact_path_policy": {"status": "not_applicable", "path": None},
        }

    if envelope_kind_exact == "approve":
        value_violations = _validate_approve_values(fields, issue_number=issue_number)
    elif envelope_kind_exact == "needs_fix":
        value_violations = _validate_needs_fix_values(fields, issue_number=issue_number)
    else:
        value_violations = _validate_producer_failure_values(fields, issue_number=issue_number)

    violations.extend(value_violations)

    artifact_field = "ARTIFACT" if "ARTIFACT" in fields else None
    artifact_value = fields.get("ARTIFACT", "") if artifact_field else None
    artifact_policy_status = "valid"
    if artifact_value is not None:
        artifact_policy_status = (
            "invalid"
            if any(v["field"] == "ARTIFACT" for v in value_violations if "field" in v)
            else "valid"
        )

    if envelope_kind_exact == "producer_failure":
        # Canonical producer-failure envelopes are syntactically parseable
        # but ALWAYS routed to human_judgment_required (#1165 SSOT); the
        # envelope is never `validation_status: valid` (AC3).
        return {
            "validation_status": "invalid",
            "envelope_kind": "producer_failure",
            "normalized_payload": dict(fields) if not value_violations else None,
            "violations": violations,
            "next_action": "human_judgment_required",
            "artifact_path_policy": {"status": artifact_policy_status, "path": artifact_value},
        }

    if violations:
        return {
            "validation_status": "invalid",
            "envelope_kind": envelope_kind_exact,
            "normalized_payload": None,
            "violations": violations,
            "next_action": "human_judgment_required",
            "artifact_path_policy": {"status": artifact_policy_status, "path": artifact_value},
        }

    next_action = fields["NEXT_ACTION"]
    return {
        "validation_status": "valid",
        "envelope_kind": envelope_kind_exact,
        "normalized_payload": dict(fields),
        "violations": [],
        "next_action": next_action,
        "artifact_path_policy": {"status": "valid", "path": artifact_value},
    }


def build_result(raw_bytes: bytes, *, issue_number: int | None = None) -> tuple[dict[str, Any], int]:
    """Build the full REVIEW_COMPACT_VALIDATION_RESULT_V1 payload + exit code."""
    input_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    input_byte_count = len(raw_bytes)

    try:
        raw_text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        payload = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "validation_status": "invalid",
            "envelope_kind": "runtime_error",
            "input_sha256": f"sha256:{input_sha256}",
            "input_byte_count": input_byte_count,
            "normalized_payload": None,
            "violations": [_violation("utf8_decode_error", detail=str(exc))],
            "next_action": "human_judgment_required",
            "artifact_path_policy": {"status": "not_applicable", "path": None},
        }
        return payload, 2

    inner = validate_review_compact_output(raw_text, issue_number=issue_number)
    payload = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "input_sha256": f"sha256:{input_sha256}",
        "input_byte_count": input_byte_count,
        **inner,
    }

    if payload["validation_status"] == "valid":
        exit_code = 0
    else:
        exit_code = 1
    return payload, exit_code


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    try:
        int_value = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--issue-number must be an integer, got {value!r}") from exc
    if int_value <= 0:
        raise argparse.ArgumentTypeError(f"--issue-number must be a positive integer, got {value!r}")
    return int_value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate issue-reviewer SubAgent compact output against "
        "ISSUE_REVIEW_RESULT_COMPACT_V1 canonical envelope grammars."
    )
    parser.add_argument(
        "--input-file",
        default=None,
        help="Path to the raw SubAgent stdout text (default: read from stdin).",
    )
    parser.add_argument(
        "--issue-number",
        type=_positive_int,
        required=True,
        help="Active issue number (positive integer). Binds ARTIFACT's issue "
        "segment to this value (Issue #1507 AC15/AC16).",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        if args.input_file:
            with open(args.input_file, "rb") as f:
                raw_bytes = f.read()
        else:
            raw_bytes = sys.stdin.buffer.read()
    except OSError as exc:
        payload = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "validation_status": "invalid",
            "envelope_kind": "runtime_error",
            "input_sha256": None,
            "input_byte_count": None,
            "normalized_payload": None,
            "violations": [_violation("input_read_error", detail=str(exc))],
            "next_action": "human_judgment_required",
            "artifact_path_policy": {"status": "not_applicable", "path": None},
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
        sys.stdout.write("\n")
        return 2

    payload, exit_code = build_result(raw_bytes, issue_number=args.issue_number)
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
