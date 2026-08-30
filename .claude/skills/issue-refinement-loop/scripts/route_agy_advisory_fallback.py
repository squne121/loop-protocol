#!/usr/bin/env python3
"""Pure routing core for controller-owned AGY advisory invocations."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

_DECISION_SCHEMA = "AGY_ADVISORY_FALLBACK_ROUTE_DECISION_V1"
_DECISION_KEYS = frozenset({"schema", "schema_version", "status", "next_action", "failure_class", "reason_code"})
_DECISION_STATUS_ACTIONS = {
    ("ok", "continue_agy_result"),
    ("degraded", "native_non_mutating_fallback"),
    ("failed", "fail_closed"),
}
_DECISION_REASON_CODES = frozenset(
    {
        "controller_input_invalid",
        "builder_failed",
        "wrapper_result_invalid",
        "deny_policy",
        "explicitly_required",
        "non_agy_or_pre_agy",
        "agy_success",
        "advisory_operational",
    }
)
_FAILURE_KINDS = frozenset({"operational", "policy_or_permission", "contract"})


class ProtocolError(ValueError):
    """Raised when a closed controller protocol value is malformed."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_object_bytes(data: bytes) -> dict[str, Any]:
    """Parse exactly one UTF-8 JSON object with at most one trailing LF."""
    if data.endswith(b"\n"):
        data = data[:-1]
    if not data or data[:1] != b"{" or data[-1:] != b"}":
        raise ProtocolError("JSON stream must contain exactly one object")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ProtocolError(f"invalid JSON constant: {value}")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON stream") from exc
    if not isinstance(value, dict):
        raise ProtocolError("JSON value must be an object")
    return value


def encode_closed_json(value: Mapping[str, Any]) -> bytes:
    """Encode one closed JSON object with the sole permitted trailing LF."""
    return json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def _decision(status: str, next_action: str, failure_class: str | None, reason_code: str) -> dict[str, Any]:
    return {
        "schema": _DECISION_SCHEMA,
        "schema_version": 1,
        "status": status,
        "next_action": next_action,
        "failure_class": failure_class,
        "reason_code": reason_code,
    }


def validate_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the controller's intentionally narrow delivery protocol."""
    if set(value) != _DECISION_KEYS:
        raise ProtocolError("decision keys are not exact")
    if (
        value.get("schema") != _DECISION_SCHEMA
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
    ):
        raise ProtocolError("decision schema mismatch")
    status = value.get("status")
    next_action = value.get("next_action")
    failure_class = value.get("failure_class")
    reason_code = value.get("reason_code")
    if (status, next_action) not in _DECISION_STATUS_ACTIONS:
        raise ProtocolError("invalid decision status/action")
    if not isinstance(reason_code, str) or reason_code not in _DECISION_REASON_CODES:
        raise ProtocolError("invalid decision reason")
    valid_reasons = {
        "ok": {"agy_success"},
        "degraded": {"advisory_operational"},
        "failed": {
            "controller_input_invalid",
            "builder_failed",
            "wrapper_result_invalid",
            "deny_policy",
            "explicitly_required",
            "non_agy_or_pre_agy",
        },
    }
    if reason_code not in valid_reasons[status]:
        raise ProtocolError("decision reason does not match status")
    if failure_class is not None and (not isinstance(failure_class, str) or not failure_class):
        raise ProtocolError("invalid decision failure_class")
    if status == "ok" and failure_class is not None:
        raise ProtocolError("ok decision requires null failure_class")
    if status == "degraded" and (not isinstance(failure_class, str) or not failure_class.startswith("agy_")):
        raise ProtocolError("degraded decision requires agy failure_class")
    return dict(value)


def route_agy_advisory_fallback(
    result: Mapping[str, Any],
    *,
    requirement: str,
    canonical_failure_kind: Callable[[str | None], str | None],
) -> dict[str, Any]:
    """Return the sole decision capable of authorizing native fallback.

    ``canonical_failure_kind`` is owned by the canonical producer. This core
    validates only the routing projection and never mirrors producer taxonomy.
    """
    if not isinstance(requirement, str) or requirement not in {"advisory", "explicitly_required"}:
        return _decision("failed", "fail_closed", None, "controller_input_invalid")
    if result.get("schema") != "delegation_result/v1":
        return _decision("failed", "fail_closed", None, "wrapper_result_invalid")
    if result.get("provider") != "agy":
        return _decision("failed", "fail_closed", None, "non_agy_or_pre_agy")
    if type(result.get("ok")) is not bool:
        return _decision("failed", "fail_closed", None, "wrapper_result_invalid")

    if result["ok"]:
        if (
            result.get("failure_class") is not None
            or result.get("agy_failure_kind") is not None
            or result.get("agy_invocation_attempted") is not True
        ):
            return _decision("failed", "fail_closed", None, "wrapper_result_invalid")
        return _decision("ok", "continue_agy_result", None, "agy_success")

    failure_class = result.get("failure_class")
    observed_kind = result.get("agy_failure_kind")
    if not isinstance(failure_class, str) or not failure_class or not failure_class.startswith("agy_"):
        return _decision("failed", "fail_closed", None, "wrapper_result_invalid")
    if result.get("agy_invocation_attempted") is not True or observed_kind not in _FAILURE_KINDS:
        return _decision("failed", "fail_closed", failure_class, "non_agy_or_pre_agy")
    try:
        canonical_kind = canonical_failure_kind(failure_class)
    except Exception:
        return _decision("failed", "fail_closed", failure_class, "wrapper_result_invalid")
    if canonical_kind is None or canonical_kind != observed_kind:
        return _decision("failed", "fail_closed", failure_class, "deny_policy")
    if requirement == "explicitly_required":
        return _decision("failed", "fail_closed", failure_class, "explicitly_required")
    if observed_kind != "operational":
        return _decision("failed", "fail_closed", failure_class, "deny_policy")
    return _decision("degraded", "native_non_mutating_fallback", failure_class, "advisory_operational")
