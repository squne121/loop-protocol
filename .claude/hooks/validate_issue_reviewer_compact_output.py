#!/usr/bin/env python3
"""Fail-close Claude Code SubagentStop guard for issue-reviewer output.

The hook intentionally keeps only digest-bearing receipts.  It never persists
the hook payload, final response, or a transcript path's contents.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from emit_parent_review_envelope_v2 import validate_child_intermediate  # noqa: E402


TARGET_AGENT_TYPE = "issue-reviewer"
TARGET_MATCHER = "^issue-reviewer$"
RECEIPT_SCHEMA = "CLAUDE_SUBAGENT_RUNTIME_RECEIPT_V1"
REPAIR_REASON = "canonical compact stdout をそのまま再生成してください。"
RUNTIME_ERROR_REASON = "compact output を検証できません。canonical compact stdout を再生成してください。"
PARENT_FAIL_CLOSE_REASON = "parent_fail_close_required"
SAFE_IDENTIFIER = re.compile(r"[^A-Za-z0-9._:-]")


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _safe_identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return SAFE_IDENTIFIER.sub("_", value)[:160]


def _safe_int_env(name: str) -> int | None:
    value = os.environ.get(name, "")
    return int(value) if value.isdecimal() else None


def _repo_identifier() -> str:
    value = os.environ.get("LOOP_RUNTIME_REPO", "squne121/loop-protocol")
    return value if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) else "squne121/loop-protocol"


def _current_head() -> str | None:
    supplied = _safe_identifier(os.environ.get("LOOP_RUNTIME_HEAD_SHA"))
    if supplied:
        return supplied
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if re.fullmatch(r"[0-9a-f]{40,64}", value) else None


def _digest_file(path: Path) -> str | None:
    try:
        return _sha256(path.read_bytes())
    except OSError:
        return None


def _receipt_dir() -> Path:
    configured = os.environ.get("ISSUE_REVIEWER_RUNTIME_RECEIPT_DIR")
    if configured:
        candidate = Path(configured)
        if candidate.is_absolute():
            return candidate
    return REPO_ROOT / ".claude" / "artifacts" / "1754" / "runtime-receipts"


def _atomic_write_receipt(receipt: dict[str, object]) -> bool:
    directory = _receipt_dir()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = (json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        filename = f"receipt-{time.time_ns()}-{uuid.uuid4().hex}.json"
        target = directory / filename
        fd, temporary = tempfile.mkstemp(prefix=".receipt-", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError:
        return False
    return True


def _read_payload_once() -> tuple[dict[str, Any] | None, str | None]:
    """Consume stdin exactly once; no caller gets a second payload read."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, TypeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return payload, _sha256(canonical)


def _receipt(
    payload: dict[str, Any],
    payload_sha256: str,
    *,
    decision: str,
    reason: str | None,
    validation_status: str,
) -> dict[str, object]:
    message = payload.get("last_assistant_message")
    transcript_path = payload.get("agent_transcript_path")
    return {
        "schema": RECEIPT_SCHEMA,
        "repo": _repo_identifier(),
        "issue": _safe_int_env("LOOP_RUNTIME_ISSUE") or 1754,
        "pr": _safe_int_env("LOOP_RUNTIME_PR") or 1787,
        "head_sha": _current_head(),
        "agent_type": _safe_identifier(payload.get("agent_type")),
        "matcher": TARGET_MATCHER,
        "session_id": _safe_identifier(payload.get("session_id")),
        "agent_id": _safe_identifier(payload.get("agent_id") or payload.get("subagent_id")),
        "payload_sha256": payload_sha256,
        "message_sha256": _sha256(message.encode("utf-8")) if isinstance(message, str) else None,
        "transcript_path_sha256": _sha256(transcript_path.encode("utf-8")) if isinstance(transcript_path, str) else None,
        "hook_source_sha256": _digest_file(Path(__file__)),
        "settings_sha256": _digest_file(REPO_ROOT / ".claude" / "settings.json"),
        "attempt": "retry" if payload.get("stop_hook_active") is True else "initial",
        "stop_hook_active": payload.get("stop_hook_active") is True,
        "decision": decision,
        "reason": reason,
        "validation_status": validation_status,
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def _emit(decision: str, reason: str) -> None:
    print(json.dumps({"decision": decision, "reason": reason}, ensure_ascii=False))


def main() -> int:
    payload, payload_sha256 = _read_payload_once()
    if payload is None or payload_sha256 is None:
        _emit("block", RUNTIME_ERROR_REASON)
        return 0
    if payload.get("agent_type") != TARGET_AGENT_TYPE:
        return 0

    message = payload.get("last_assistant_message")
    validation_status = "runtime_error"
    if isinstance(message, str):
        try:
            validation = validate_child_intermediate(message)
            validation_status = str(validation.get("validation_status", "runtime_error"))
        except Exception:
            validation_status = "runtime_error"

    retry = payload.get("stop_hook_active") is True
    if validation_status == "valid":
        decision, reason = "allow", None
    elif retry:
        # Claude must not enter a second hook loop.  The unmodified invalid
        # response reaches the existing parent validator, which owns routing.
        decision, reason = "allow", PARENT_FAIL_CLOSE_REASON
    else:
        decision = "block"
        reason = REPAIR_REASON if validation_status == "invalid" else RUNTIME_ERROR_REASON

    receipt = _receipt(
        payload,
        payload_sha256,
        decision=decision,
        reason=reason,
        validation_status=validation_status,
    )
    if not _atomic_write_receipt(receipt):
        # A missing receipt is never a successful hook result.  A retry still
        # avoids a second block, while making parent fail-close explicit.
        if retry:
            _emit("allow", PARENT_FAIL_CLOSE_REASON)
        else:
            _emit("block", RUNTIME_ERROR_REASON)
        return 0

    if decision == "block":
        _emit("block", reason or RUNTIME_ERROR_REASON)
    elif reason is not None:
        _emit("allow", reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
