#!/usr/bin/env python3
"""
reviewer_claim_replay_state_store.py - REVIEWER_CLAIM_REPLAY_STATE_STORE_RESULT_V1

Orchestrator-owned atomic read/write for REVIEWER_CLAIM_REPLAY_STATE_V2
(Issue #1515). This is the *sole* persistence layer for Step 2a
consecutive-unbacked state: `reviewer_claim_replay.py` itself performs no
file I/O for state when invoked with `--previous-state-inline` (see that
module for the analyze()/CLI side of this contract).

Contract summary (state_contract, Issue #1504 / #1515):
- owner: orchestrator (this script), scope: refinement_session
- identity_key: repository_full_name, issue_number, refinement_session_id,
  body_sha256, normalized_kind, reviewer_blocker_code
- concurrency_policy: single_writer, detected via an O_CREAT|O_EXCL lock file
- write_policy: atomic_replace (same-directory temp file + fsync + os.replace)
- symlink_policy: reject (both the state path and the temp file path)
- corrupt_state_policy: fail_closed (`status: corrupt`, never silently reset)
- retention_policy: delete_on_loop_termination (caller's responsibility --
  this script has no retention logic of its own)

CLI:
  --read  --state-dir <dir> --repository-full-name <str> \
          --issue-number <int> --refinement-session-id <str>
  --write --state-dir <dir> --next-state-inline <json>

Exit codes: 0 on `status: ok` (including a reset with `reset_reason` set),
1 on `status: corrupt` / `concurrent_write_detected` / `error`.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

import jsonschema as _jsonschema

STORE_RESULT_SCHEMA = "REVIEWER_CLAIM_REPLAY_STATE_STORE_RESULT_V1"
STATE_SCHEMA_V2 = "REVIEWER_CLAIM_REPLAY_STATE_V2"
STATE_FILE_NAME = "reviewer_claim_replay_state.json"

REQUIRED_STATE_FIELDS: tuple[str, ...] = (
    "schema",
    "repository_full_name",
    "issue_number",
    "refinement_session_id",
    "body_sha256",
    "reviewer_blocker_code",
    "normalized_kind",
    "consecutive_unbacked_count",
    "last_review_artifact",
    "updated_at_iteration_id",
)

STATE_V2_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": list(REQUIRED_STATE_FIELDS),
    "additionalProperties": False,
    "properties": {
        "schema": {"const": STATE_SCHEMA_V2},
        "repository_full_name": {"type": "string", "minLength": 1},
        "issue_number": {"type": "integer"},
        "refinement_session_id": {"type": "string", "minLength": 1},
        "body_sha256": {"type": "string", "minLength": 1},
        "reviewer_blocker_code": {"type": ["string", "null"]},
        "normalized_kind": {"type": ["string", "null"]},
        "consecutive_unbacked_count": {"type": "integer", "minimum": 0},
        "last_review_artifact": {"type": ["string", "null"]},
        "updated_at_iteration_id": {"type": ["string", "null"]},
    },
}


def _reject_nonfinite_json(token: str) -> None:
    raise ValueError(f"Non-finite JSON constant rejected: {token}")


def _strict_json_loads(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_nonfinite_json)


def _strict_json_dumps(payload: Any) -> str:
    return json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
    )


def _state_path(state_dir: Path) -> Path:
    return state_dir / STATE_FILE_NAME


def _lock_path(state_path: Path) -> Path:
    return state_path.parent / (state_path.name + ".lock")


def _is_symlink_or_non_regular(path: Path) -> bool:
    """True if `path` exists and is a symlink, or exists as a non-regular
    file (fifo/device/etc). False if it does not exist (not yet created)."""
    try:
        st = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    if not stat.S_ISREG(st.st_mode):
        return True
    return False


def _result(
    operation: str,
    status: str,
    *,
    state: dict[str, Any] | None = None,
    reset_reason: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": STORE_RESULT_SCHEMA,
        "operation": operation,
        "status": status,
        "state": state if state is not None else {},
        "reset_reason": reset_reason,
        "error": error,
    }


def read_state(
    *,
    state_dir: Path,
    repository_full_name: str,
    issue_number: int,
    refinement_session_id: str,
) -> dict[str, Any]:
    """--read: return the existing REVIEWER_CLAIM_REPLAY_STATE_V2 if it
    matches the supplied identity, an empty (first-time) state if the file
    is absent or the identity does not match (`reset_reason` set, not an
    error), or `status: corrupt` if the file is unreadable, a symlink, a
    non-regular file, malformed JSON, wrong schema, or missing required
    identity fields (fail-closed -- never silently treated as fresh)."""
    state_path = _state_path(state_dir)

    if _is_symlink_or_non_regular(state_path):
        return _result(
            "read",
            "corrupt",
            error=f"state path is a symlink or non-regular file: {state_path}",
        )

    if not state_path.exists():
        return _result("read", "ok", state={}, reset_reason="not_found")

    try:
        raw_text = state_path.read_text(encoding="utf-8")
        data = _strict_json_loads(raw_text)
    except (OSError, ValueError) as exc:
        return _result("read", "corrupt", error=f"state file read/decode error: {exc}")

    if not isinstance(data, dict):
        return _result("read", "corrupt", error="state file is not a JSON object")

    try:
        _jsonschema.validate(instance=data, schema=STATE_V2_SCHEMA)
    except _jsonschema.ValidationError as exc:
        return _result("read", "corrupt", error=f"state schema violation: {exc.message}")

    identity_matches = (
        data.get("repository_full_name") == repository_full_name
        and data.get("issue_number") == issue_number
        and data.get("refinement_session_id") == refinement_session_id
    )
    if not identity_matches:
        return _result("read", "ok", state={}, reset_reason="identity_mismatch")

    return _result("read", "ok", state=data)


def write_state(*, state_dir: Path, next_state: dict[str, Any]) -> dict[str, Any]:
    """--write: validate `next_state` against REVIEWER_CLAIM_REPLAY_STATE_V2,
    then atomically replace the state file (same-directory temp file, fsync,
    os.replace). Rejects a symlinked/non-regular state path. Uses an
    O_CREAT|O_EXCL lock file to detect a concurrent writer -- does not wait
    or retry; returns `status: concurrent_write_detected` immediately."""
    try:
        _jsonschema.validate(instance=next_state, schema=STATE_V2_SCHEMA)
    except _jsonschema.ValidationError as exc:
        return _result("write", "error", error=f"next-state schema violation: {exc.message}")

    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = _state_path(state_dir)
    lock_path = _lock_path(state_path)

    if _is_symlink_or_non_regular(state_path):
        return _result(
            "write",
            "corrupt",
            error=f"state path is a symlink or non-regular file: {state_path}",
        )

    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _result(
            "write",
            "concurrent_write_detected",
            error=f"lock file already exists (concurrent writer): {lock_path}",
        )

    try:
        os.close(lock_fd)
        fd, tmp_path_str = tempfile.mkstemp(dir=str(state_dir))
        tmp_path = Path(tmp_path_str)
        try:
            if _is_symlink_or_non_regular(tmp_path):
                raise ValueError(f"temp file is a symlink or non-regular file: {tmp_path}")
            os.chmod(str(tmp_path), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(_strict_json_dumps(next_state))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(tmp_path), str(state_path))
        except Exception as exc:  # noqa: BLE001 -- convert to a result, never raise
            try:
                os.unlink(str(tmp_path))
            except OSError:
                pass
            return _result("write", "error", error=f"atomic write failed: {exc}")
    finally:
        try:
            os.unlink(str(lock_path))
        except OSError:
            pass

    return _result("write", "ok", state=next_state)


def write_state_v2_from_validated_payload(
    *, state_dir: Path, validation_result_v2: dict[str, Any]
) -> dict[str, Any]:
    """Issue #1532 AC5: the ONLY V2 write path.

    `validation_result_v2` MUST be a REVIEW_COMPACT_VALIDATION_RESULT_V2-
    shaped payload (as produced by
    `validate_review_compact_output.validate_review_compact_output_v2`).
    `REPLAY_NEXT_STATE` is persisted IF AND ONLY IF
    `validation_result_v2["validation_status"] == "valid"` -- raw child
    stdout, an invalid validation result, or a binding-digest mismatch
    NEVER reach `write_state()`. This is the sole enforcement point that
    prevents an unvalidated (or tampered) `REPLAY_NEXT_STATE` claim from
    ever being persisted (Issue #1532 Remaining Parent Gap: previously
    `REPLAY_NEXT_STATE` was outside the validated grammar entirely and
    was written directly from raw child stdout)."""
    if validation_result_v2.get("validation_status") != "valid":
        return _result(
            "write_v2",
            "rejected",
            error=(
                "REPLAY_NEXT_STATE not persisted: validation_status is not "
                f"'valid' ({validation_result_v2.get('validation_status')!r})"
            ),
        )

    normalized_payload = validation_result_v2.get("normalized_payload")
    if not isinstance(normalized_payload, dict) or "REPLAY_NEXT_STATE" not in normalized_payload:
        return _result(
            "write_v2",
            "rejected",
            error="normalized_payload missing REPLAY_NEXT_STATE",
        )

    try:
        next_state = _strict_json_loads(normalized_payload["REPLAY_NEXT_STATE"])
    except (ValueError, json.JSONDecodeError) as exc:
        return _result(
            "write_v2",
            "rejected",
            error=f"REPLAY_NEXT_STATE is not valid JSON: {exc}",
        )
    if not isinstance(next_state, dict):
        return _result("write_v2", "rejected", error="REPLAY_NEXT_STATE must be a JSON object")

    result = write_state(state_dir=state_dir, next_state=next_state)
    result["operation"] = "write_v2"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Orchestrator-owned atomic read/write for REVIEWER_CLAIM_REPLAY_STATE_V2"
    )
    parser.add_argument("--read", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument(
        "--write-v2",
        action="store_true",
        help=(
            "Issue #1532: write REPLAY_NEXT_STATE only if the supplied "
            "REVIEW_COMPACT_VALIDATION_RESULT_V2 (--validation-result-v2-inline) "
            "has validation_status: valid."
        ),
    )
    parser.add_argument("--state-dir")
    parser.add_argument("--repository-full-name")
    parser.add_argument("--issue-number", type=int)
    parser.add_argument("--refinement-session-id")
    parser.add_argument("--next-state-inline")
    parser.add_argument("--validation-result-v2-inline")
    args = parser.parse_args()

    mode_count = sum([args.read, args.write, args.write_v2])
    if mode_count != 1:
        print(
            _strict_json_dumps(
                _result(
                    "unknown",
                    "error",
                    error="exactly one of --read, --write, or --write-v2 is required",
                )
            ),
            flush=True,
        )
        return 1

    if args.write_v2:
        if not args.state_dir:
            print(
                _strict_json_dumps(_result("write_v2", "error", error="--state-dir is required")),
                flush=True,
            )
            return 1
        if not args.validation_result_v2_inline:
            print(
                _strict_json_dumps(
                    _result(
                        "write_v2",
                        "error",
                        error="--validation-result-v2-inline is required",
                    )
                ),
                flush=True,
            )
            return 1
        try:
            validation_result_v2 = _strict_json_loads(args.validation_result_v2_inline)
        except (ValueError, json.JSONDecodeError) as exc:
            print(
                _strict_json_dumps(
                    _result(
                        "write_v2",
                        "error",
                        error=f"invalid --validation-result-v2-inline JSON: {exc}",
                    )
                ),
                flush=True,
            )
            return 1
        if not isinstance(validation_result_v2, dict):
            print(
                _strict_json_dumps(
                    _result(
                        "write_v2",
                        "error",
                        error="--validation-result-v2-inline must be a JSON object",
                    )
                ),
                flush=True,
            )
            return 1
        result = write_state_v2_from_validated_payload(
            state_dir=Path(args.state_dir), validation_result_v2=validation_result_v2
        )
        print(_strict_json_dumps(result), flush=True)
        return 0 if result["status"] == "ok" else 1

    if not args.state_dir:
        print(
            _strict_json_dumps(
                _result("read" if args.read else "write", "error", error="--state-dir is required")
            ),
            flush=True,
        )
        return 1
    state_dir = Path(args.state_dir)

    if args.read:
        missing = [
            name
            for name, value in (
                ("--repository-full-name", args.repository_full_name),
                ("--issue-number", args.issue_number),
                ("--refinement-session-id", args.refinement_session_id),
            )
            if value is None
        ]
        if missing:
            result = _result("read", "error", error=f"missing required args: {missing}")
        else:
            result = read_state(
                state_dir=state_dir,
                repository_full_name=args.repository_full_name,
                issue_number=args.issue_number,
                refinement_session_id=args.refinement_session_id,
            )
    else:
        if not args.next_state_inline:
            result = _result("write", "error", error="--next-state-inline is required")
        else:
            try:
                next_state = _strict_json_loads(args.next_state_inline)
            except (ValueError, json.JSONDecodeError) as exc:
                next_state = None
                result = _result("write", "error", error=f"invalid --next-state-inline JSON: {exc}")
            if next_state is not None:
                if not isinstance(next_state, dict):
                    result = _result("write", "error", error="--next-state-inline must be a JSON object")
                else:
                    result = write_state(state_dir=state_dir, next_state=next_state)

    print(_strict_json_dumps(result), flush=True)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
