#!/usr/bin/env python3
"""
parent_replay_binding.py - PARENT_REPLAY_BINDING_ARTIFACT_V1

Issue #1532: parent-owned replay binding V2. The issue-refinement-loop
orchestrator (parent) is the ONLY producer of this artifact. It combines:

  - parent-owned deterministic checker outputs already fetched by the
    orchestrator before invoking `issue-reviewer`: `review_result` (which
    itself embeds the untrusted-but-bounded `REVIEWER_BLOCKER_CLAIM_V1` the
    child SubAgent returned -- the reviewer's blocker findings), plus
    `readiness_result`, `vc_syntax_result`, `vc_preflight_result` (all
    produced by the orchestrator's own deterministic checker invocations,
    never read from a child isolation worktree).
  - a parent-owned `previous_state` (read via
    `reviewer_claim_replay_state_store.py --read`).
  - parent-owned identity: `repository_full_name`, `issue_number`,
    `refinement_session_id`, `iteration_id`.

It replays `reviewer_claim_replay.analyze()` IN-PROCESS over these
parent-owned inputs (never over a child isolation worktree's raw artifact
file -- Issue #1472 isolation boundary) to independently derive
`replay_next_state`, then binds everything into a canonical,
byte-for-byte-repeatable JSON artifact with a `binding_digest` --
surfaced as `REPLAY_PARENT_BINDING_DIGEST` -- distinct in meaning from the
existing child-stdout `REPLAY_ARTIFACT_DIGEST` (Issue #1507 / PR #1519).
Both digests are retained side by side in V2 so producer/validator/consumer
parity tests can fix their distinct semantics (AC1).

`iteration_id` is required precisely so wall-clock time never enters the
canonical payload: `generated_at` is intentionally NOT part of the
digest-covered payload, so the SAME parent-owned inputs always reproduce
the SAME `binding_digest` regardless of when the binding is computed
(AC3).

Usage:
    uv run python3 parent_replay_binding.py \
        --review-result-file <path> \
        --readiness-result-file <path> \
        [--vc-syntax-result-file <path>] \
        [--vc-preflight-result-file <path>] \
        [--previous-state-inline '<json>' | --previous-state-file <path>] \
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
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import reviewer_claim_replay as _replay  # noqa: E402

SCHEMA = "PARENT_REPLAY_BINDING_ARTIFACT_V1"
SCHEMA_VERSION = "1"


def _reject_nonfinite_json(token: str) -> None:
    raise ValueError(f"Non-finite JSON constant rejected: {token}")


def _strict_json_loads(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_nonfinite_json)


def canonical_json_bytes(payload: Any) -> bytes:
    """Canonical, wall-clock-free, byte-for-byte-repeatable serialization.

    Sorted keys + no insignificant whitespace so the SAME logical payload
    always produces the SAME bytes (AC3), independent of dict insertion
    order or platform.
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


def build_parent_replay_binding(
    *,
    review_result: dict[str, Any],
    readiness_result: dict[str, Any],
    vc_syntax_result: "dict[str, Any] | None",
    vc_preflight_result: "dict[str, Any] | None",
    previous_state: "dict[str, Any] | None",
    repository_full_name: str,
    issue_number: int,
    refinement_session_id: str,
    iteration_id: str,
) -> dict[str, Any]:
    """Replay `reviewer_claim_replay.analyze()` over parent-owned inputs and
    build the canonical PARENT_REPLAY_BINDING_ARTIFACT_V1 payload.

    Returns the full artifact dict (including `binding_digest`, computed
    over the canonical bytes of everything EXCEPT `binding_digest` itself
    -- see `recompute_binding_digest`). Never mutates the caller's input
    dicts. Raises whatever `reviewer_claim_replay.analyze()` raises for
    malformed inputs (fail-closed via the CLI's try/except).
    """
    result, next_state = _replay.analyze(
        review_result=review_result,
        readiness_result=readiness_result,
        vc_syntax_result=vc_syntax_result,
        vc_preflight_result=vc_preflight_result,
        previous_state=previous_state or {},
        repository_full_name=repository_full_name,
        issue_number=issue_number,
        refinement_session_id=refinement_session_id,
    )

    input_digests = {
        "review_result_sha256": _sha256_hex(canonical_json_bytes(review_result)),
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
        "input_digests": input_digests,
        "replay_result": result,
        "replay_next_state": next_state,
    }
    binding_digest = _sha256_hex(canonical_json_bytes(canonical_payload))

    artifact = dict(canonical_payload)
    artifact["binding_digest"] = f"sha256:{binding_digest}"
    return artifact


def recompute_binding_digest(artifact: dict[str, Any]) -> str:
    """Recompute `binding_digest` from an artifact dict (excludes the
    `binding_digest` key itself), for tamper detection by validators.
    Returns the same `sha256:<hex>` shape as `build_parent_replay_binding`.
    """
    canonical_payload = {k: v for k, v in artifact.items() if k != "binding_digest"}
    return f"sha256:{_sha256_hex(canonical_json_bytes(canonical_payload))}"


def canonical_replay_next_state_line(artifact: dict[str, Any]) -> str:
    """Canonical single-line JSON string for the `REPLAY_NEXT_STATE`
    envelope field, derived from a PARENT_REPLAY_BINDING_ARTIFACT_V1's
    `replay_next_state` value. Callers embed this EXACT string as the
    envelope field value (validators compare byte-for-byte)."""
    return canonical_json_bytes(artifact["replay_next_state"]).decode("utf-8")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Build PARENT_REPLAY_BINDING_ARTIFACT_V1")
    parser.add_argument("--review-result-file", required=True)
    parser.add_argument("--readiness-result-file", required=True)
    parser.add_argument("--vc-syntax-result-file", default=None)
    parser.add_argument("--vc-preflight-result-file", default=None)
    parser.add_argument("--previous-state-inline", default=None)
    parser.add_argument("--previous-state-file", default=None)
    parser.add_argument("--repository-full-name", required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--refinement-session-id", required=True)
    parser.add_argument("--iteration-id", required=True)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        review_result = _strict_json_loads(Path(args.review_result_file).read_text(encoding="utf-8"))
        readiness_result = _strict_json_loads(Path(args.readiness_result_file).read_text(encoding="utf-8"))
        vc_syntax_result = (
            _strict_json_loads(Path(args.vc_syntax_result_file).read_text(encoding="utf-8"))
            if args.vc_syntax_result_file
            else None
        )
        vc_preflight_result = (
            _strict_json_loads(Path(args.vc_preflight_result_file).read_text(encoding="utf-8"))
            if args.vc_preflight_result_file
            else None
        )
        if args.previous_state_inline:
            previous_state = _strict_json_loads(args.previous_state_inline)
        elif args.previous_state_file:
            previous_state = _strict_json_loads(Path(args.previous_state_file).read_text(encoding="utf-8"))
        else:
            previous_state = {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"schema": SCHEMA, "status": "error", "error": str(exc)}, ensure_ascii=True),
            flush=True,
        )
        return 2

    try:
        artifact = build_parent_replay_binding(
            review_result=review_result,
            readiness_result=readiness_result,
            vc_syntax_result=vc_syntax_result,
            vc_preflight_result=vc_preflight_result,
            previous_state=previous_state,
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
