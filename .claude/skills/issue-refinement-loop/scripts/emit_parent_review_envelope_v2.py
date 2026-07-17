#!/usr/bin/env python3
"""
emit_parent_review_envelope_v2.py - deterministic ISSUE_REVIEW_RESULT_COMPACT_V2 producer.

Issue #1541: closes the parent-side manual-assembly gap left by Issue #1532 /
PR #1535. Before this module existed, the orchestrator had to hand-append six
`PARENT_REPLAY_*` lines (computed from a `PARENT_REPLAY_BINDING_ARTIFACT_V1`)
to the child `issue-reviewer` SubAgent's validated stdout -- a manual
f-string assembly step that only existed as a test-only helper
(`_assemble_v2_envelope()` in `test_parent_replay_isolation_runtime.py`).
This module is the production, deterministic replacement.

Two-stage trust boundary (unchanged from Issue #1532 -- this module does NOT
reintroduce child-side routing semantics):

  1. `validate_child_intermediate()` strictly validates the EXACT text the
     `issue-reviewer` child SubAgent returns:
       - approve: exact 8 lines (STATUS..ARTIFACT, no claim/replay fields)
       - needs-fix intermediate: exact 9 lines (the 8 approve fields plus a
         single bounded, untrusted `REVIEWER_BLOCKER_CLAIM` field)
     This is a DISTINCT grammar from both the V1 final grammar (13-line
     `REPLAY_*` child self-report, retired) and the V2 final grammar (15-line
     `PARENT_REPLAY_*`, parent-only) in `validate_review_compact_output.py`.
     Unknown/duplicate/out-of-order fields, prose, code fences, blank lines,
     control characters, and oversize input are all rejected exactly as they
     are for the V1/V2 final grammars.

  2. `render_parent_review_envelope_v2()` is a PURE function (no subprocess,
     no I/O) that takes ONLY the validated child payload (dict) and an
     ALREADY-validated `PARENT_REPLAY_BINDING_ARTIFACT_V1` dict, and produces
     byte-for-byte deterministic UTF-8 output. It does not call
     `reviewer_claim_replay.py` itself (the binding artifact already carries
     the parent's replay result -- see `parent_replay_binding.py`).

`emit_parent_review_envelope_v2()` is the orchestration function the CLI
calls: it loads/validates the binding artifact, cross-checks it against the
caller-supplied identity (repository/issue/session/iteration/current body)
and against the child claim's own canonical digest (Issue #1532 Blocker 1 --
the ONLY untrusted child input), and only then renders the final envelope.
Any cross-check failure is fail-closed `contract_invalid`; the six
`PARENT_REPLAY_*` fields are NEVER derived from anything other than the
independently-validated binding artifact (Issue #1541 AC3).

Failure/output contract (AC8):
    success:           exit 0, stdout = final envelope exact bytes, stderr empty
    contract-invalid:  exit 1, stdout empty, stderr = machine-readable diagnostic
    runtime/env error: exit 2, stdout empty, stderr = machine-readable diagnostic
No partial envelope is ever written to stdout: the full byte string is built
in memory and written exactly once, only after every check has passed.

Usage:
    <issue-reviewer stdout text> | uv run python3 emit_parent_review_envelope_v2.py \\
        --issue-number <N> \\
        --binding-artifact-file <path> \\
        --repository-full-name <owner/repo> \\
        --refinement-session-id <id> \\
        --iteration-id <id> \\
        --current-body-file <path>

For an approve child envelope, only `--issue-number` is required; the
binding/identity flags are never consulted (approve never invokes parent
replay, binding, or state write -- AC6).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import parent_replay_binding as _pb  # noqa: E402
import validate_review_compact_output as _v1  # noqa: E402

SCHEMA = "ISSUE_REVIEW_RESULT_COMPACT_V2"
SCHEMA_VERSION = "2"

MAX_INPUT_BYTES = 2048

# ---------------------------------------------------------------------------
# Child intermediate grammar (DISTINCT from V1/V2 final grammars)
# ---------------------------------------------------------------------------

CHILD_APPROVE_FIELDS: list[str] = list(_v1.APPROVE_FIELDS)
CHILD_NEEDS_FIX_FIELDS: list[str] = list(_v1.APPROVE_FIELDS) + [_v1.REVIEWER_BLOCKER_CLAIM_FIELD]

_CHILD_TEMPLATES: dict[str, list[str]] = {
    "approve": CHILD_APPROVE_FIELDS,
    "needs_fix_intermediate": CHILD_NEEDS_FIX_FIELDS,
}

ALL_CHILD_KNOWN_FIELDS: frozenset[str] = frozenset(CHILD_NEEDS_FIX_FIELDS)

# Final V2 envelope field order (SSOT for byte layout).
FINAL_V2_FIELDS: list[str] = (
    list(_v1.APPROVE_FIELDS) + [_v1.REVIEWER_BLOCKER_CLAIM_FIELD] + list(_v1.PARENT_REPLAY_FIELDS)
)


def _violation(code: str, **extra: Any) -> dict[str, Any]:
    v: dict[str, Any] = {"code": code}
    v.update(extra)
    return v


def _parse_lines_child(lines: list[str]) -> tuple[list[str], dict[str, str], list[dict[str, Any]]]:
    """Same lexical grammar as `validate_review_compact_output._parse_lines`,
    restricted to the child-intermediate known field set (8 approve fields +
    `REVIEWER_BLOCKER_CLAIM`). `PARENT_REPLAY_*` / `REPLAY_*` fields are NOT
    known here -- a child that emits them is rejected as `unknown_field`
    (Issue #1532 Blocker 2: those are parent-only)."""
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
        match = _v1._FIELD_LINE_RE.match(line)
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
        if key not in ALL_CHILD_KNOWN_FIELDS:
            violations.append(_violation("unknown_field", field=key, line_index=index))
            continue
        if value != value.strip():
            violations.append(_violation("value_whitespace_violation", field=key, value=value))
        if key in field_values:
            violations.append(_violation("duplicate_field", field=key, line_index=index))
            continue
        ordered_keys.append(key)
        field_values[key] = value

    return ordered_keys, field_values, violations


def _classify_child_envelope(ordered_keys: list[str]) -> "str | None":
    for name, template in _CHILD_TEMPLATES.items():
        if ordered_keys == template:
            return name
    return None


def _invalid_child_result(violations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "validation_status": "invalid",
        "envelope_kind": "unknown",
        "normalized_payload": None,
        "violations": violations,
    }


def validate_child_intermediate(raw_text: str, *, issue_number: "int | None" = None) -> dict[str, Any]:
    """Strictly validate the EXACT text returned by the `issue-reviewer`
    child SubAgent against the two-shape child-intermediate grammar (8-line
    approve / 9-line needs-fix). Returns a dict with keys:
    validation_status, envelope_kind, normalized_payload, violations.

    This is intentionally a SEPARATE grammar from
    `validate_review_compact_output.py`'s V1/V2 FINAL grammars -- the child
    never emits `REPLAY_*` or `PARENT_REPLAY_*` fields (Issue #1541 P0-5)."""
    byte_count = len(raw_text.encode("utf-8"))
    if byte_count > MAX_INPUT_BYTES:
        return _invalid_child_result(
            [_violation("byte_budget_exceeded", byte_count=byte_count, limit=MAX_INPUT_BYTES)]
        )
    if raw_text == "":
        return _invalid_child_result([_violation("empty_input")])

    control_violations = _v1._scan_control_chars(raw_text)
    lines = _v1._split_lines(raw_text)
    ordered_keys, fields, structural_violations = _parse_lines_child(lines)

    violations: list[dict[str, Any]] = list(control_violations) + list(structural_violations)

    has_malformed_line = any(
        v["code"] in {"prose_prefix", "prose_suffix", "malformed_line", "unknown_field", "duplicate_field"}
        for v in structural_violations
    )

    envelope_kind = _classify_child_envelope(ordered_keys) if not has_malformed_line else None
    if envelope_kind is None:
        keys_set = set(ordered_keys)
        best_name = "approve"
        best_overlap = -1
        for name, template in _CHILD_TEMPLATES.items():
            overlap = len(keys_set & set(template))
            if overlap > best_overlap:
                best_overlap = overlap
                best_name = name
        template = _CHILD_TEMPLATES[best_name]
        missing = [k for k in template if k not in keys_set]
        for field in missing:
            violations.append(_violation("missing_field", field=field, template=best_name))
        if keys_set == set(template) and ordered_keys != template:
            violations.append(_violation("out_of_order_field", template=best_name))
        return _invalid_child_result(violations)

    if envelope_kind == "approve":
        value_violations = _v1._validate_approve_values(fields, issue_number=issue_number)
    else:
        value_violations = _v1._validate_needs_fix_base_values(fields, issue_number=issue_number)
        value_violations.extend(
            _v1._validate_reviewer_blocker_claim_field(fields.get(_v1.REVIEWER_BLOCKER_CLAIM_FIELD, ""))
        )

    violations.extend(value_violations)
    if violations:
        return {
            "validation_status": "invalid",
            "envelope_kind": envelope_kind,
            "normalized_payload": None,
            "violations": violations,
        }

    return {
        "validation_status": "valid",
        "envelope_kind": envelope_kind,
        "normalized_payload": dict(fields),
        "violations": [],
    }


# ---------------------------------------------------------------------------
# Pure rendering (Issue #1541 AC2 -- byte-deterministic)
# ---------------------------------------------------------------------------


def render_parent_review_envelope_v2(
    validated_child_payload: dict[str, str], binding_artifact: "dict[str, Any] | None"
) -> bytes:
    """Pure function: no subprocess, no file I/O, no wall-clock input.

    `validated_child_payload` MUST be the `normalized_payload` returned by
    `validate_child_intermediate()` (exactly the 8 approve keys, or the 8
    approve keys plus `REVIEWER_BLOCKER_CLAIM`). For the approve shape,
    `binding_artifact` is ignored (may be None). For the needs-fix shape,
    `binding_artifact` MUST be an already-validated
    `PARENT_REPLAY_BINDING_ARTIFACT_V1` dict (schema, digest self-
    consistency, and identity checks are the CALLER's responsibility --
    see `emit_parent_review_envelope_v2()`); this function only reads
    `replay_result` / `binding_digest` / `replay_next_state` off of it.

    Output: UTF-8 bytes, LF line separator, exactly one trailing LF, no BOM.
    The SAME inputs always produce the SAME bytes.
    """
    keys = set(validated_child_payload.keys())

    if keys == set(CHILD_APPROVE_FIELDS):
        lines = [f"{k}: {validated_child_payload[k]}" for k in CHILD_APPROVE_FIELDS]
        return ("\n".join(lines) + "\n").encode("utf-8")

    if keys == set(CHILD_NEEDS_FIX_FIELDS):
        if binding_artifact is None:
            raise ValueError("binding_artifact is required to render a needs-fix V2 envelope")
        replay_result = binding_artifact["replay_result"]
        should_consume = "true" if replay_result["should_consume_iteration"] else "false"
        next_state_line = _pb.canonical_replay_next_state_line(binding_artifact)

        lines = [f"{k}: {validated_child_payload[k]}" for k in _v1.APPROVE_FIELDS]
        lines.append(f"{_v1.REVIEWER_BLOCKER_CLAIM_FIELD}: {validated_child_payload[_v1.REVIEWER_BLOCKER_CLAIM_FIELD]}")
        lines.append(f"PARENT_REPLAY_VERDICT: {replay_result['verdict']}")
        lines.append(f"PARENT_REPLAY_ROUTING: {replay_result['routing']}")
        lines.append(f"PARENT_REPLAY_SHOULD_CONSUME: {should_consume}")
        lines.append(f"PARENT_REPLAY_BODY_SHA256: {replay_result['body_sha256']}")
        lines.append(f"PARENT_REPLAY_NEXT_STATE: {next_state_line}")
        lines.append(f"PARENT_REPLAY_BINDING_DIGEST: {binding_artifact['binding_digest']}")
        return ("\n".join(lines) + "\n").encode("utf-8")

    raise ValueError(f"validated_child_payload does not match a known child envelope shape: {sorted(keys)}")


# ---------------------------------------------------------------------------
# Orchestration (loads/validates binding artifact, cross-checks, renders)
# ---------------------------------------------------------------------------


class EmitContractError(ValueError):
    """Raised for fail-closed contract-invalid conditions (exit 1)."""

    def __init__(self, violations: list[dict[str, Any]]):
        super().__init__(str(violations))
        self.violations = violations


class EmitRuntimeError(RuntimeError):
    """Raised for runtime/environment errors (exit 2)."""


def emit_parent_review_envelope_v2(
    child_raw_text: str,
    *,
    issue_number: int,
    binding_artifact: "dict[str, Any] | None" = None,
    repository_full_name: "str | None" = None,
    refinement_session_id: "str | None" = None,
    iteration_id: "str | None" = None,
    current_body_bytes: "bytes | None" = None,
) -> dict[str, Any]:
    """Orchestration entry point (pure w.r.t. its arguments -- no file I/O,
    no subprocess). Returns a dict:
        {"status": "ok", "envelope_bytes": bytes, "envelope_kind": str}
      or raises `EmitContractError` (contract-invalid, exit 1) /
      `EmitRuntimeError` (runtime error, exit 2).
    """
    child_result = validate_child_intermediate(child_raw_text, issue_number=issue_number)
    if child_result["validation_status"] != "valid":
        raise EmitContractError(child_result["violations"])

    envelope_kind = child_result["envelope_kind"]
    normalized_payload = child_result["normalized_payload"]

    if envelope_kind == "approve":
        # AC6: approve never touches binding/claim/replay/state-write, even
        # if binding_artifact/identity args were (incorrectly) supplied.
        envelope_bytes = render_parent_review_envelope_v2(normalized_payload, None)
        return {"status": "ok", "envelope_bytes": envelope_bytes, "envelope_kind": "approve"}

    # needs_fix_intermediate: binding artifact + full identity context REQUIRED.
    missing = [
        name
        for name, value in (
            ("binding_artifact", binding_artifact),
            ("repository_full_name", repository_full_name),
            ("refinement_session_id", refinement_session_id),
            ("iteration_id", iteration_id),
            ("current_body_bytes", current_body_bytes),
        )
        if value is None
    ]
    if missing:
        raise EmitRuntimeError(f"missing required inputs for needs-fix emission: {missing}")

    violations: list[dict[str, Any]] = []

    try:
        _pb.validate_binding_artifact(binding_artifact)
    except ValueError as exc:
        violations.append(_violation("binding_artifact_invalid", detail=str(exc)))
        raise EmitContractError(violations) from exc

    recomputed_digest = _pb.recompute_binding_digest(binding_artifact)
    if recomputed_digest != binding_artifact.get("binding_digest"):
        violations.append(
            _violation(
                "binding_artifact_digest_self_inconsistent",
                recomputed=recomputed_digest,
                stored=binding_artifact.get("binding_digest"),
            )
        )

    current_body_sha256 = f"sha256:{hashlib.sha256(current_body_bytes).hexdigest()}"

    if binding_artifact.get("repository_full_name") != repository_full_name:
        violations.append(_violation("binding_artifact_repository_mismatch"))
    if binding_artifact.get("issue_number") != issue_number:
        violations.append(_violation("binding_artifact_issue_number_mismatch"))
    if binding_artifact.get("refinement_session_id") != refinement_session_id:
        violations.append(_violation("binding_artifact_session_mismatch"))
    if binding_artifact.get("iteration_id") != iteration_id:
        violations.append(_violation("binding_artifact_iteration_mismatch"))
    if binding_artifact.get("current_body_sha256") != current_body_sha256:
        violations.append(_violation("binding_artifact_body_mismatch"))

    # Issue #1532 Blocker 1 / Issue #1541 AC3: the claim is the ONLY input
    # sourced from the (untrusted) child. Re-derive its canonical digest and
    # require an EXACT match against the digest the parent bound when it
    # built this artifact -- a tampered claim (or a claim swapped in from a
    # different binding run) fails closed here, never silently substituted.
    claim_raw = normalized_payload.get(_v1.REVIEWER_BLOCKER_CLAIM_FIELD, "")
    expected_claim_sha256 = binding_artifact.get("input_digests", {}).get("reviewer_blocker_claim_sha256")
    try:
        parsed_claim = _pb._strict_json_loads(claim_raw)
        normalized_claim = _pb.validate_reviewer_blocker_claim(parsed_claim)
        canonical_claim_digest = hashlib.sha256(_pb.canonical_json_bytes(normalized_claim)).hexdigest()
        if expected_claim_sha256 is not None and canonical_claim_digest != expected_claim_sha256:
            violations.append(
                _violation("reviewer_blocker_claim_digest_mismatch", expected=expected_claim_sha256)
            )
    except (ValueError, json.JSONDecodeError) as exc:
        violations.append(_violation("reviewer_blocker_claim_invalid", detail=str(exc)))

    if violations:
        raise EmitContractError(violations)

    envelope_bytes = render_parent_review_envelope_v2(normalized_payload, binding_artifact)

    # Defense-in-depth self-check (Issue #1541 P1-2): the bytes this module
    # is about to return MUST themselves validate against the SAME
    # independently-supplied binding artifact via the V2 validator -- a
    # mismatch here indicates an emitter defect, not a caller-supplied
    # contract violation, so it is a runtime error (exit 2), not
    # contract-invalid (exit 1).
    self_check = _v1.validate_review_compact_output_v2(
        envelope_bytes.decode("utf-8"),
        issue_number=issue_number,
        binding_artifact=binding_artifact,
        repository_full_name=repository_full_name,
        refinement_session_id=refinement_session_id,
        iteration_id=iteration_id,
        current_body_sha256=current_body_sha256,
    )
    if self_check["validation_status"] != "valid":
        raise EmitRuntimeError(f"internal self-check failed: {self_check['violations']}")

    return {"status": "ok", "envelope_bytes": envelope_bytes, "envelope_kind": "needs_fix_v2"}


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


def _write_diagnostic(reason_code: str, detail: str, *, violations: "list[dict[str, Any]] | None" = None) -> None:
    payload = {
        "schema": "EMIT_PARENT_REVIEW_ENVELOPE_V2_FAILURE",
        "reason_code": reason_code,
        "detail": detail,
        "violations": violations or [],
    }
    sys.stderr.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    sys.stderr.write("\n")
    sys.stderr.flush()


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministically emit ISSUE_REVIEW_RESULT_COMPACT_V2 from a "
        "validated child intermediate and a PARENT_REPLAY_BINDING_ARTIFACT_V1."
    )
    parser.add_argument("--input-file", default=None, help="Path to child stdout text (default: stdin)")
    parser.add_argument("--issue-number", type=_positive_int, required=True)
    parser.add_argument("--binding-artifact-file", default=None)
    parser.add_argument("--repository-full-name", default=None)
    parser.add_argument("--refinement-session-id", default=None)
    parser.add_argument("--iteration-id", default=None)
    parser.add_argument("--current-body-file", default=None)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        if args.input_file:
            with open(args.input_file, "rb") as f:
                raw_bytes = f.read()
        else:
            raw_bytes = sys.stdin.buffer.read()
    except OSError as exc:
        _write_diagnostic("input_read_error", str(exc))
        return 2

    try:
        child_raw_text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        _write_diagnostic("utf8_decode_error", str(exc))
        return 2

    binding_artifact: "dict[str, Any] | None" = None
    current_body_bytes: "bytes | None" = None

    if args.binding_artifact_file is not None:
        try:
            binding_artifact = _pb._read_json_file_safely(args.binding_artifact_file)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _write_diagnostic("binding_artifact_read_error", str(exc))
            return 2

    if args.current_body_file is not None:
        try:
            current_body_bytes = _pb.read_file_safely(args.current_body_file)
        except (OSError, ValueError) as exc:
            _write_diagnostic("current_body_read_error", str(exc))
            return 2

    try:
        result = emit_parent_review_envelope_v2(
            child_raw_text,
            issue_number=args.issue_number,
            binding_artifact=binding_artifact,
            repository_full_name=args.repository_full_name,
            refinement_session_id=args.refinement_session_id,
            iteration_id=args.iteration_id,
            current_body_bytes=current_body_bytes,
        )
    except EmitContractError as exc:
        _write_diagnostic("contract_invalid", "child intermediate or binding artifact failed validation", violations=exc.violations)
        return 1
    except EmitRuntimeError as exc:
        _write_diagnostic("runtime_error", str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001 -- fail-closed, never leak a traceback to stdout
        _write_diagnostic("unexpected_error", str(exc))
        return 2

    sys.stdout.buffer.write(result["envelope_bytes"])
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
