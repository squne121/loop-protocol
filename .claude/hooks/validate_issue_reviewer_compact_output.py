#!/usr/bin/env python3
"""Fail-close Claude Code SubagentStop guard for issue-reviewer output."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from emit_parent_review_envelope_v2 import validate_child_intermediate  # noqa: E402


REPAIR_REASON = "canonical compact stdout をそのまま再生成してください。"
RUNTIME_ERROR_REASON = "compact output を検証できません。canonical compact stdout を再生成してください。"


def _block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


def _read_payload() -> dict[str, Any] | None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def main() -> int:
    payload = _read_payload()
    if payload is None:
        _block(RUNTIME_ERROR_REASON)
        return 0

    if payload.get("agent_type") != "issue-reviewer":
        return 0

    # Claude Code marks a continuation with stop_hook_active.  Do not create
    # another retry loop; the parent validator remains the fail-close owner.
    if payload.get("stop_hook_active") is True:
        return 0

    message = payload.get("last_assistant_message")
    if not isinstance(message, str):
        _block(RUNTIME_ERROR_REASON)
        return 0

    try:
        validation = validate_child_intermediate(message)
    except Exception:
        _block(RUNTIME_ERROR_REASON)
        return 0

    if validation.get("validation_status") != "valid":
        _block(REPAIR_REASON)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
