#!/usr/bin/env python3
"""Independent, fail-closed AGY ``PreToolUse`` enforcement hook.

The hook accepts only the official camelCase event payload on stdin.  Profile,
policy and run bindings are read from a private, immutable run-context file;
stdin is never an authority source.  The provenance hook remains observe-only
and is intentionally not imported here.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Mapping

DECISION_ALLOW = "allow"
DECISION_DENY = "deny"
CONTEXT_ENV = "AGY_PERMISSION_BOUNDARY_CONTEXT_PATH"

RUN_CONTEXT_SCHEMA = "agy_permission_boundary_run_context/v1"
POLICY_SCHEMA = "agy_permission_boundary_policy/v1"
CANONICAL_PERMISSION_RESOURCES: frozenset[str] = frozenset(
    {
        "command",
        "read_file",
        "write_file",
        "read_url",
        "execute_url",
        "unsandboxed",
        "mcp",
    }
)
ALLOWED_PROFILES: frozenset[str] = frozenset(
    {"no_tools", "local_asset_research", "grounded_research", "proposal_only"}
)

# Native hook tool names are not official permission resources.  Unknown
# dispatchers deny; the map stays deliberately small until live evidence can
# establish another native name.
NATIVE_TO_RESOURCE: dict[str, str] = {
    "run_command": "command",
    "view_file": "read_file",
    "write_to_file": "write_file",
    "replace_file_content": "write_file",
    "multi_replace_file_content": "write_file",
    "search_web": "read_url",
    "read_url_content": "read_url",
}


def _reason(value: str) -> str:
    """Return a bounded reason token suitable for the hook stdout contract."""
    return value[:64]


def _decision(reason: str) -> dict[str, str]:
    return {"decision": DECISION_DENY, "reason": _reason(reason)}


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_private_immutable_file(path: Path) -> bool:
    """Require a regular same-user file that is not writable by group/other."""
    try:
        file_stat = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
        return False
    if file_stat.st_uid != os.getuid() or file_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    return True


def _read_private_json(path_value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(path_value, str) or not path_value:
        return None, "context_load_failure"
    path = Path(path_value)
    if not _is_private_immutable_file(path):
        return None, "context_load_failure"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "context_load_failure"
    if not isinstance(value, dict):
        return None, "context_load_failure"
    return value, None


def _load_context() -> tuple[dict[str, Any] | None, str | None]:
    context, error = _read_private_json(os.environ.get(CONTEXT_ENV))
    if error or context is None:
        return None, "context_load_failure"
    if context.get("schema") != RUN_CONTEXT_SCHEMA:
        return None, "context_load_failure"
    required_strings = (
        "run_id",
        "conversation_id",
        "workspace",
        "tool_profile",
        "policy_path",
        "policy_sha256",
        "enforcement_log_path",
    )
    if any(not isinstance(context.get(key), str) or not context[key] for key in required_strings):
        return None, "context_load_failure"
    if context["tool_profile"] not in ALLOWED_PROFILES:
        return None, "context_load_failure"
    invocation = context.get("invocation_number")
    if not isinstance(invocation, int) or isinstance(invocation, bool) or invocation < 0:
        return None, "context_load_failure"
    return context, None


def _load_policy(context: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    path_value = context.get("policy_path")
    if not isinstance(path_value, str) or not path_value:
        return None, "policy_load_failure"
    path = Path(path_value)
    if not _is_private_immutable_file(path):
        return None, "policy_load_failure"
    try:
        raw = path.read_bytes()
        policy = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None, "policy_load_failure"
    expected_digest = context.get("policy_sha256")
    actual_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if not isinstance(expected_digest, str) or expected_digest != actual_digest:
        return None, "policy_digest_mismatch"
    if not isinstance(policy, dict) or policy.get("schema") != POLICY_SCHEMA:
        return None, "policy_load_failure"
    if policy.get("profile") != context.get("tool_profile"):
        return None, "context_policy_mismatch"
    for key in ("allowed_resources", "denied_resources"):
        resources = policy.get(key)
        if not isinstance(resources, list) or not all(
            isinstance(resource, str) and resource in CANONICAL_PERMISSION_RESOURCES
            for resource in resources
        ):
            return None, "policy_load_failure"
    return policy, None


def _parse_payload(payload: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(payload, dict):
        return None, "malformed_payload"
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict) or set(tool_call) != {"name", "args"}:
        return None, "malformed_payload"
    name = tool_call.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, "malformed_payload"
    conversation = payload.get("conversationId")
    step_index = payload.get("stepIdx")
    workspace_paths = payload.get("workspacePaths")
    if (
        not isinstance(conversation, str)
        or not conversation
        or not isinstance(step_index, int)
        or isinstance(step_index, bool)
        or step_index < 0
        or not isinstance(workspace_paths, list)
        or not workspace_paths
        or not all(isinstance(path, str) and path for path in workspace_paths)
    ):
        return None, "malformed_payload"
    return {
        "tool_name": name.strip().lower(),
        "args_digest": _canonical_digest(tool_call["args"]),
        "conversation_id": conversation,
        "step_index": step_index,
        "workspace_paths": workspace_paths,
    }, None


def _event(
    *, context: Mapping[str, Any], payload: Mapping[str, Any], resource: str, decision: str, reason: str
) -> dict[str, Any]:
    """Build a secret-safe record: no raw args, paths, prompt or payload."""
    event = {
        "schema": "agy_permission_boundary_hook/v1",
        "decision": decision,
        "reason": _reason(reason),
        "run_id": context["run_id"],
        "conversation_id": payload["conversation_id"],
        "invocation_number": context["invocation_number"],
        "step_index": payload["step_index"],
        "tool_name": payload["tool_name"],
        "resource": resource,
        "tool_profile": context["tool_profile"],
        "args_digest": payload["args_digest"],
    }
    canary_id = context.get("canary_id")
    if isinstance(canary_id, str) and canary_id:
        event["canary_id"] = canary_id
    return event


def _write_event(context: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    path_value = context.get("enforcement_log_path")
    if not isinstance(path_value, str) or not path_value:
        return False
    try:
        log_path = Path(path_value)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    except OSError:
        return False
    return True


def _evaluate(payload: Any) -> tuple[dict[str, str], dict[str, Any] | None, dict[str, Any] | None]:
    context, context_error = _load_context()
    if context_error or context is None:
        return _decision(context_error or "context_load_failure"), None, None
    parsed, payload_error = _parse_payload(payload)
    if payload_error or parsed is None:
        return _decision(payload_error or "malformed_payload"), context, None
    if parsed["conversation_id"] != context["conversation_id"]:
        return _decision("conversation_mismatch"), context, _event(
            context=context, payload=parsed, resource="", decision=DECISION_DENY, reason="conversation_mismatch"
        )
    if context["workspace"] not in parsed["workspace_paths"]:
        return _decision("workspace_binding_mismatch"), context, _event(
            context=context, payload=parsed, resource="", decision=DECISION_DENY, reason="workspace_binding_mismatch"
        )
    policy, policy_error = _load_policy(context)
    if policy_error or policy is None:
        return _decision(policy_error or "policy_load_failure"), context, _event(
            context=context,
            payload=parsed,
            resource="",
            decision=DECISION_DENY,
            reason=policy_error or "policy_load_failure",
        )
    resource = NATIVE_TO_RESOURCE.get(parsed["tool_name"])
    if resource is None:
        return _decision("unknown_native_tool"), context, _event(
            context=context, payload=parsed, resource="", decision=DECISION_DENY, reason="unknown_native_tool"
        )
    denied = set(policy["denied_resources"])
    allowed = set(policy["allowed_resources"])
    if resource in denied:
        decision, reason = DECISION_DENY, "policy_deny"
    elif resource in allowed:
        decision, reason = DECISION_ALLOW, "policy_allow"
    else:
        decision, reason = DECISION_DENY, "policy_default_deny"
    return {"decision": decision, "reason": _reason(reason)}, context, _event(
        context=context, payload=parsed, resource=resource, decision=decision, reason=reason
    )


def decide(payload: Any) -> dict[str, str]:
    """Return the official decision object and fail closed when logging fails."""
    decision, context, event = _evaluate(payload)
    if event is not None and context is not None and not _write_event(context, event):
        return _decision("log_write_failed")
    return decision


def main() -> int:
    try:
        payload: Any = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError):
        payload = None
    decision = decide(payload)
    # Hook stdout is deliberately limited to the documented decision contract.
    print(json.dumps(decision, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
