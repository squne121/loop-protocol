#!/usr/bin/env python3
"""
parent_replay_binding.py - PARENT_REPLAY_BINDING_ARTIFACT_V1

Issue #1532: parent-local replay integrity binding. The issue-refinement-loop
orchestrator (parent) is the ONLY producer of this artifact.

This module deliberately does NOT provide, and does not claim to provide,
producer identity / supply-chain provenance attestation for the child
SubAgent. It provides a *parent-local replay integrity binding*: the parent
independently replays deterministic arbitration over inputs it fetched,
saved, and read back itself, and cryptographically binds the result to a
canonical, byte-for-byte-repeatable artifact. It does NOT prove who or what
produced the child process, does not involve signatures or key management,
and does not authenticate the same-OS-UID child SubAgent (see Issue #1532
"Remaining Parent Gaps" / Out of Scope).

Trust boundary (Issue #1532 Blocker 1, closing the #1519 residual risk):

  - The ONLY untrusted input this module accepts from the child SubAgent is
    a `REVIEWER_BLOCKER_CLAIM_V1`-shaped bounded claim: `body_sha256` plus a
    list of `{reviewer_blocker_code, message, line_start, line_end}`
    entries. `validate_reviewer_blocker_claim()` enforces this with
    `additionalProperties: false` at every level -- a child claim can NOT
    smuggle `findings` / `checker_evidence` / `deterministic_checks` /
    readiness results into the replay. Those keys simply do not exist in
    the accepted schema; supplying them raises `ValueError` (fail-closed)
    rather than being silently dropped.
  - `readiness_result`, `vc_syntax_result`, `vc_preflight_result`, and
    `current_body_bytes` are 100% parent-owned: the orchestrator fetches,
    saves, and reads these back itself, and NEVER opens a child isolation
    worktree's raw artifact file (Issue #1472 isolation boundary).
  - `reviewer_claim_replay.analyze()` is replayed IN-PROCESS by the parent
    over a `review_result` object THIS module constructs itself from the
    above parent-owned inputs plus the untrusted claim's bounded
    `blockers` list -- `findings` and `deterministic_checks` are ALWAYS
    empty in that constructed object, so `deterministic_backed` can only
    ever be derived from the parent's OWN readiness/vc-preflight/vc-syntax
    evidence (`reviewer_claim_replay._matching_readiness_errors` /
    `_matching_vc_preflight` / `_matching_vc_syntax`), never from a
    child-supplied `deterministic_domain_blocker` finding.

`iteration_id` is a REQUIRED parent-owned value threaded into
`reviewer_claim_replay.analyze(..., iteration_id=...)` so
`next_state.updated_at_iteration_id` never contains a wall-clock value --
the SAME parent-owned inputs always reproduce the SAME `binding_digest`
regardless of when the binding is computed (AC3 / High-2).

Usage:
    uv run python3 parent_replay_binding.py \
        --reviewer-blocker-claim-file <path> \
        --readiness-result-file <path> \
        [--vc-syntax-result-file <path>] \
        [--vc-preflight-result-file <path>] \
        [--previous-state-inline '<json>' | --previous-state-file <path>] \
        --current-body-file <path> \
        --issue-url <url> \
        --repository-full-name <owner/repo> \
        --issue-number <N> \
        --refinement-session-id <id> \
        --iteration-id <id>

stdout: exactly one JSON object, schema PARENT_REPLAY_BINDING_ARTIFACT_V1.
Exit codes: 0 = ok, 2 = input/runtime error (fail-closed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import reviewer_claim_replay as _replay  # noqa: E402

SCHEMA = "PARENT_REPLAY_BINDING_ARTIFACT_V1"
SCHEMA_VERSION = "2"

REVIEWER_BLOCKER_CLAIM_SCHEMA = "REVIEWER_BLOCKER_CLAIM_V1"

MAX_SAFE_READ_BYTES = 1_000_000

# ---------------------------------------------------------------------------
# Untrusted child claim schema (Issue #1532 Blocker 1)
# ---------------------------------------------------------------------------

_BLOCKER_CLAIM_ITEM_REQUIRED = ("reviewer_blocker_code", "message", "line_start", "line_end")

# Issue #1541 PR #1557 OWNER REQUEST_CHANGES High-2: the 2048-byte envelope
# cap bounds the SERIALIZED claim line, but does not by itself bound the
# *shape* of the parsed claim object (e.g. a pathological input could try to
# pack many short blocker entries, or a single very long code/message, into
# that byte budget in aggregate before the outer envelope cap is measured).
# These schema-level bounds are a defense-in-depth structural cap,
# independent of and in addition to the byte budget.
MAX_BLOCKER_CLAIM_ITEMS = 100
MAX_REVIEWER_BLOCKER_CODE_LENGTH = 200
MAX_BLOCKER_CLAIM_MESSAGE_LENGTH = 2000


def _reject_nonfinite_json(token: str) -> None:
    raise ValueError(f"Non-finite JSON constant rejected: {token}")


def _no_duplicate_keys_object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Medium item (canonicalization): reject JSON objects with duplicate
    keys instead of silently letting the last occurrence win. A duplicate
    key is tamper-shaped input (or a non-canonical producer bug) and must
    fail closed rather than be canonicalized away."""
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON object key rejected: {key!r}")
        seen.add(key)
        result[key] = value
    return result


def _strict_json_loads(text: str) -> Any:
    return json.loads(
        text,
        parse_constant=_reject_nonfinite_json,
        object_pairs_hook=_no_duplicate_keys_object_pairs_hook,
    )


def canonical_json_bytes(payload: Any) -> bytes:
    """Canonical, wall-clock-free, byte-for-byte-repeatable serialization.

    Sorted keys + no insignificant whitespace so the SAME logical payload
    always produces the SAME bytes (AC3), independent of dict insertion
    order or platform. `ensure_ascii=True` so non-ASCII property values
    never change the byte-for-byte outcome across platforms with different
    default encodings.

    Issue #1541 PR #1557 OWNER REQUEST_CHANGES P2-2: this is a
    project-local Python canonical JSON encoding (`json.dumps(...,
    sort_keys=True, separators=(",", ":"), ensure_ascii=True)`) -- it is
    NOT an implementation of RFC 8785 JSON Canonicalization Scheme (JCS).
    In particular it does not perform JCS's Unicode NFC normalization,
    JCS's specific number serialization (`ECMAScript`-compatible number
    formatting), or interoperate with a non-Python JCS implementation.
    Only THIS function's own byte output is ever compared byte-for-byte
    elsewhere in this codebase (never against an external JCS producer),
    so that is the only interoperability property this docstring claims.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_reviewer_blocker_claim(claim: Any) -> dict[str, Any]:
    """Fail-closed shape validation for the untrusted child
    REVIEWER_BLOCKER_CLAIM_V1 payload.

    Raises `ValueError` if `claim` is not EXACTLY a
    `{schema, body_sha256, blockers: [...]}` object (no extra top-level
    keys -- in particular `findings` / `checker_evidence` /
    `deterministic_checks` / any readiness/VC result shape is rejected by
    construction, not merely ignored) or if any blocker item carries an
    unexpected key.
    """
    if not isinstance(claim, dict):
        raise ValueError("reviewer_blocker_claim must be a JSON object")

    allowed_top_keys = {"schema", "body_sha256", "blockers"}
    extra_top_keys = set(claim.keys()) - allowed_top_keys
    if extra_top_keys:
        raise ValueError(
            "reviewer_blocker_claim carries disallowed keys (untrusted claim "
            f"boundary violation): {sorted(extra_top_keys)}"
        )
    missing_top_keys = allowed_top_keys - set(claim.keys())
    if missing_top_keys:
        raise ValueError(f"reviewer_blocker_claim missing required keys: {sorted(missing_top_keys)}")

    if claim.get("schema") != REVIEWER_BLOCKER_CLAIM_SCHEMA:
        raise ValueError(
            f"reviewer_blocker_claim.schema must be {REVIEWER_BLOCKER_CLAIM_SCHEMA!r}, "
            f"got {claim.get('schema')!r}"
        )

    body_sha256 = claim.get("body_sha256")
    if not isinstance(body_sha256, str) or not body_sha256.startswith("sha256:") or len(body_sha256) != 71:
        raise ValueError(f"reviewer_blocker_claim.body_sha256 malformed: {body_sha256!r}")

    blockers = claim.get("blockers")
    if not isinstance(blockers, list):
        raise ValueError("reviewer_blocker_claim.blockers must be a list")
    # Issue #1541 High-2: structural cap on the number of blocker entries --
    # independent of, and in addition to, the outer 2048-byte envelope cap.
    if len(blockers) > MAX_BLOCKER_CLAIM_ITEMS:
        raise ValueError(
            f"reviewer_blocker_claim.blockers exceeds maxItems={MAX_BLOCKER_CLAIM_ITEMS}: "
            f"got {len(blockers)}"
        )

    normalized_blockers: list[dict[str, Any]] = []
    for index, item in enumerate(blockers):
        if not isinstance(item, dict):
            raise ValueError(f"reviewer_blocker_claim.blockers[{index}] must be an object")
        extra_item_keys = set(item.keys()) - set(_BLOCKER_CLAIM_ITEM_REQUIRED)
        if extra_item_keys:
            raise ValueError(
                f"reviewer_blocker_claim.blockers[{index}] carries disallowed keys "
                f"(untrusted claim boundary violation): {sorted(extra_item_keys)}"
            )
        missing_item_keys = set(_BLOCKER_CLAIM_ITEM_REQUIRED) - set(item.keys())
        if missing_item_keys:
            raise ValueError(
                f"reviewer_blocker_claim.blockers[{index}] missing keys: {sorted(missing_item_keys)}"
            )
        code = item.get("reviewer_blocker_code")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(f"reviewer_blocker_claim.blockers[{index}].reviewer_blocker_code invalid")
        if len(code) > MAX_REVIEWER_BLOCKER_CODE_LENGTH:
            raise ValueError(
                f"reviewer_blocker_claim.blockers[{index}].reviewer_blocker_code exceeds "
                f"maxLength={MAX_REVIEWER_BLOCKER_CODE_LENGTH}: got {len(code)}"
            )
        message = item.get("message")
        if message is not None and not isinstance(message, str):
            raise ValueError(f"reviewer_blocker_claim.blockers[{index}].message must be string or null")
        if isinstance(message, str) and len(message) > MAX_BLOCKER_CLAIM_MESSAGE_LENGTH:
            raise ValueError(
                f"reviewer_blocker_claim.blockers[{index}].message exceeds "
                f"maxLength={MAX_BLOCKER_CLAIM_MESSAGE_LENGTH}: got {len(message)}"
            )
        for field_name in ("line_start", "line_end"):
            value = item.get(field_name)
            if value is not None and not isinstance(value, int):
                raise ValueError(
                    f"reviewer_blocker_claim.blockers[{index}].{field_name} must be int or null"
                )
        normalized_blockers.append(
            {
                "reviewer_blocker_code": code,
                "message": message,
                "line_start": item.get("line_start"),
                "line_end": item.get("line_end"),
            }
        )

    return {
        "schema": REVIEWER_BLOCKER_CLAIM_SCHEMA,
        "body_sha256": body_sha256,
        "blockers": normalized_blockers,
    }


# ---------------------------------------------------------------------------
# Safe file reading (Medium item: symlink / non-regular / oversized rejection)
# ---------------------------------------------------------------------------


def read_file_safely(path: str, *, max_bytes: int = MAX_SAFE_READ_BYTES) -> bytes:
    """Open `path` rejecting symlinks (O_NOFOLLOW), non-regular files, and
    oversized content. Raises `ValueError` (fail-closed) on any violation.
    Never uses `Path.read_text()` / `open()` (those follow symlinks and do
    not bound the read size before touching the filesystem).

    Guarantee scope (Issue #1541 PR #1557 OWNER REQUEST_CHANGES P2-1):
    `O_NOFOLLOW` rejects a symlink ONLY at the final path component -- an
    ancestor directory component that is itself a symlink is still
    transparently followed by the OS before this open() call ever executes
    (this function does not walk/verify each intermediate path segment).
    This is a Linux-guaranteed `O_NOFOLLOW` semantic; `os.O_NOFOLLOW` is not
    guaranteed to exist on every platform Python runs on (hence the
    `hasattr(os, "O_NOFOLLOW")` guard below) -- on a platform where the flag
    is unavailable, the final-component symlink check is silently skipped
    rather than failing closed. Callers that need to reject symlinked
    ancestor directories, or that need a hard guarantee on
    non-`O_NOFOLLOW`-supporting platforms, must perform that check
    separately (e.g. `Path.resolve()` containment comparison against a
    trusted root) before calling this function."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"unable to open {path!r}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"refusing to read non-regular file: {path!r}")
        if st.st_size > max_bytes:
            raise ValueError(
                f"refusing to read oversized file ({st.st_size} bytes > {max_bytes} limit): {path!r}"
            )
        data = b""
        remaining = st.st_size
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            data += chunk
            remaining -= len(chunk)
        return data
    finally:
        os.close(fd)


def _read_json_file_safely(path: str) -> Any:
    return _strict_json_loads(read_file_safely(path).decode("utf-8"))


# ---------------------------------------------------------------------------
# PARENT_REPLAY_BINDING_ARTIFACT_V1 strict schema (High-1)
#
# Issue #1541 PR #1557 OWNER REQUEST_CHANGES P2-3: this is a STRICT
# TOP-LEVEL ENVELOPE schema. `additionalProperties: false` is enforced only
# at the top level and at `input_digests` (one level deep) -- `replay_result`
# and `replay_next_state` are each declared as bare `{"type": "object"}`,
# i.e. their OWN internal shape is not independently schema-validated here.
# Those two objects are instead cross-checked field-by-field, exact-match,
# against `reviewer_claim_replay.analyze()`'s own return value by
# `validate_review_compact_output_v2()` (see `_validate_parent_replay_fields()`
# / the `expected_*` comparisons in that function) -- this schema's
# responsibility ends at "the top-level envelope has exactly these keys,
# with exactly these top-level types", not "every nested field of
# `replay_result` / `replay_next_state` is itself schema-constrained".
# ---------------------------------------------------------------------------

PARENT_REPLAY_BINDING_ARTIFACT_V1_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "schema",
        "schema_version",
        "repository_full_name",
        "issue_number",
        "refinement_session_id",
        "iteration_id",
        "current_body_sha256",
        "input_digests",
        "replay_result",
        "replay_next_state",
        "binding_digest",
    ],
    "additionalProperties": False,
    "properties": {
        "schema": {"const": SCHEMA},
        "schema_version": {"const": SCHEMA_VERSION},
        "repository_full_name": {"type": "string", "minLength": 1},
        "issue_number": {"type": "integer"},
        "refinement_session_id": {"type": "string", "minLength": 1},
        "iteration_id": {"type": "string", "minLength": 1},
        "current_body_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "input_digests": {
            "type": "object",
            "required": [
                "reviewer_blocker_claim_sha256",
                "readiness_result_sha256",
                "vc_syntax_result_sha256",
                "vc_preflight_result_sha256",
                "previous_state_sha256",
            ],
            "additionalProperties": False,
            "properties": {
                "reviewer_blocker_claim_sha256": {"type": "string"},
                "readiness_result_sha256": {"type": "string"},
                "vc_syntax_result_sha256": {"type": ["string", "null"]},
                "vc_preflight_result_sha256": {"type": ["string", "null"]},
                "previous_state_sha256": {"type": "string"},
            },
        },
        "replay_result": {"type": "object"},
        "replay_next_state": {"type": "object"},
        "binding_digest": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    },
}


def validate_binding_artifact(artifact: Any) -> None:
    """Strict schema validation for a PARENT_REPLAY_BINDING_ARTIFACT_V1
    payload (High-1). Raises `ValueError` on any violation."""
    import jsonschema as _jsonschema

    try:
        _jsonschema.validate(instance=artifact, schema=PARENT_REPLAY_BINDING_ARTIFACT_V1_SCHEMA)
    except _jsonschema.ValidationError as exc:
        raise ValueError(f"binding artifact schema violation: {exc.message}") from exc


def build_parent_replay_binding(
    *,
    reviewer_blocker_claim: dict[str, Any],
    readiness_result: dict[str, Any],
    vc_syntax_result: "dict[str, Any] | None",
    vc_preflight_result: "dict[str, Any] | None",
    previous_state: "dict[str, Any] | None",
    current_body_bytes: bytes,
    issue_url: str,
    repository_full_name: str,
    issue_number: int,
    refinement_session_id: str,
    iteration_id: str,
) -> dict[str, Any]:
    """Replay `reviewer_claim_replay.analyze()` over parent-owned inputs plus
    the strictly-schema-validated untrusted child claim, and build the
    canonical PARENT_REPLAY_BINDING_ARTIFACT_V1 payload.

    `reviewer_blocker_claim` is the ONLY input sourced from the child
    SubAgent; it is validated by `validate_reviewer_blocker_claim()` before
    use (fail-closed). `findings` and `deterministic_checks` are ALWAYS
    empty in the `review_result` this function constructs for
    `analyze()` -- deterministic backing can therefore only ever come from
    the parent's OWN `readiness_result` / `vc_syntax_result` /
    `vc_preflight_result` evidence, never from a child-forged
    `deterministic_domain_blocker` finding (Blocker 1).

    Never mutates the caller's input dicts. Raises `ValueError` for
    malformed inputs (fail-closed via the CLI's try/except).
    """
    claim = validate_reviewer_blocker_claim(reviewer_blocker_claim)

    current_body_sha256 = f"sha256:{_sha256_hex(current_body_bytes)}"
    if claim["body_sha256"] != current_body_sha256:
        raise ValueError(
            "reviewer_blocker_claim.body_sha256 does not match the parent-owned "
            f"current body snapshot: claim={claim['body_sha256']!r} "
            f"current={current_body_sha256!r}"
        )

    review_result_for_replay: dict[str, Any] = {
        "issue_url": issue_url,
        "producer_body_sha256": current_body_sha256,
        "body_sha256": current_body_sha256,
        "structured_blockers": [
            {
                "reviewer_blocker_code": item["reviewer_blocker_code"],
                "message": item["message"],
                "line_start": item["line_start"],
                "line_end": item["line_end"],
            }
            for item in claim["blockers"]
        ],
        "blocking_issues": [],
        "findings": [],
        "deterministic_checks": {},
    }

    result, next_state = _replay.analyze(
        review_result=review_result_for_replay,
        readiness_result=readiness_result,
        vc_syntax_result=vc_syntax_result,
        vc_preflight_result=vc_preflight_result,
        previous_state=previous_state or {},
        repository_full_name=repository_full_name,
        issue_number=issue_number,
        refinement_session_id=refinement_session_id,
        iteration_id=iteration_id,
    )

    input_digests = {
        "reviewer_blocker_claim_sha256": _sha256_hex(canonical_json_bytes(claim)),
        "readiness_result_sha256": _sha256_hex(canonical_json_bytes(readiness_result)),
        "vc_syntax_result_sha256": (
            _sha256_hex(canonical_json_bytes(vc_syntax_result))
            if vc_syntax_result is not None
            else None
        ),
        "vc_preflight_result_sha256": (
            _sha256_hex(canonical_json_bytes(vc_preflight_result))
            if vc_preflight_result is not None
            else None
        ),
        "previous_state_sha256": _sha256_hex(canonical_json_bytes(previous_state or {})),
    }

    canonical_payload: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "repository_full_name": repository_full_name,
        "issue_number": issue_number,
        "refinement_session_id": refinement_session_id,
        "iteration_id": iteration_id,
        "current_body_sha256": current_body_sha256,
        "input_digests": input_digests,
        "replay_result": result,
        "replay_next_state": next_state,
    }
    binding_digest = _sha256_hex(canonical_json_bytes(canonical_payload))

    artifact = dict(canonical_payload)
    artifact["binding_digest"] = f"sha256:{binding_digest}"
    validate_binding_artifact(artifact)
    return artifact


def recompute_binding_digest(artifact: dict[str, Any]) -> str:
    """Recompute `binding_digest` from an artifact dict (excludes the
    `binding_digest` key itself), for tamper detection by validators.
    Returns the same `sha256:<hex>` shape as `build_parent_replay_binding`.
    """
    canonical_payload = {k: v for k, v in artifact.items() if k != "binding_digest"}
    return f"sha256:{_sha256_hex(canonical_json_bytes(canonical_payload))}"


def canonical_replay_next_state_line(artifact: dict[str, Any]) -> str:
    """Canonical single-line JSON string for the `PARENT_REPLAY_NEXT_STATE`
    envelope field, derived from a PARENT_REPLAY_BINDING_ARTIFACT_V1's
    `replay_next_state` value. Callers embed this EXACT string as the
    envelope field value (validators compare byte-for-byte)."""
    return canonical_json_bytes(artifact["replay_next_state"]).decode("utf-8")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Build PARENT_REPLAY_BINDING_ARTIFACT_V1")
    parser.add_argument("--reviewer-blocker-claim-file", required=True)
    parser.add_argument("--readiness-result-file", required=True)
    parser.add_argument("--vc-syntax-result-file", default=None)
    parser.add_argument("--vc-preflight-result-file", default=None)
    parser.add_argument("--previous-state-inline", default=None)
    parser.add_argument("--previous-state-file", default=None)
    parser.add_argument("--current-body-file", required=True)
    parser.add_argument("--issue-url", default="")
    parser.add_argument("--repository-full-name", required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--refinement-session-id", required=True)
    parser.add_argument("--iteration-id", required=True)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        reviewer_blocker_claim = _read_json_file_safely(args.reviewer_blocker_claim_file)
        readiness_result = _read_json_file_safely(args.readiness_result_file)
        vc_syntax_result = (
            _read_json_file_safely(args.vc_syntax_result_file) if args.vc_syntax_result_file else None
        )
        vc_preflight_result = (
            _read_json_file_safely(args.vc_preflight_result_file)
            if args.vc_preflight_result_file
            else None
        )
        if args.previous_state_inline:
            previous_state = _strict_json_loads(args.previous_state_inline)
        elif args.previous_state_file:
            previous_state = _read_json_file_safely(args.previous_state_file)
        else:
            previous_state = {}
        current_body_bytes = read_file_safely(args.current_body_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"schema": SCHEMA, "status": "error", "error": str(exc)}, ensure_ascii=True),
            flush=True,
        )
        return 2

    try:
        artifact = build_parent_replay_binding(
            reviewer_blocker_claim=reviewer_blocker_claim,
            readiness_result=readiness_result,
            vc_syntax_result=vc_syntax_result,
            vc_preflight_result=vc_preflight_result,
            previous_state=previous_state,
            current_body_bytes=current_body_bytes,
            issue_url=args.issue_url,
            repository_full_name=args.repository_full_name,
            issue_number=args.issue_number,
            refinement_session_id=args.refinement_session_id,
            iteration_id=args.iteration_id,
        )
    except Exception as exc:  # noqa: BLE001 -- fail-closed, never raise past the CLI boundary
        print(
            json.dumps({"schema": SCHEMA, "status": "error", "error": str(exc)}, ensure_ascii=True),
            flush=True,
        )
        return 2

    print(json.dumps(artifact, ensure_ascii=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
