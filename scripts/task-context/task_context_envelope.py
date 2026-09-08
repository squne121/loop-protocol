"""Task Context v1 — stable request/result envelope (AC8).

The envelope shape itself is frozen by the Issue #2563 contract:

    request: {schema_version, operation, request_id, payload}
    result:  {schema_version, status, code, data}

``payload`` (request) and ``data`` (result) are intentionally *open* --
this module does not enumerate or validate operation-specific fields inside
them. Future consumer children (#2564/#2565/#2568/...) add fields inside
``payload``/``data`` additively, without needing to change this envelope.
"""

from __future__ import annotations

import uuid
from typing import Any

REQUEST_SCHEMA_VERSION = "task-context-request/v1"
RESULT_SCHEMA_VERSION = "task-context-result/v1"

REQUEST_ENVELOPE_KEYS = frozenset({"schema_version", "operation", "request_id", "payload"})
RESULT_ENVELOPE_KEYS = frozenset({"schema_version", "status", "code", "data"})

STATUS_OK = "ok"
STATUS_ERROR = "error"


def build_request(
    operation: str, payload: dict[str, Any] | None = None, request_id: str | None = None
) -> dict[str, Any]:
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "operation": operation,
        "request_id": request_id or str(uuid.uuid4()),
        "payload": payload if payload is not None else {},
    }


def build_result(status: str, code: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "status": status,
        "code": code,
        "data": data if data is not None else {},
    }


def build_ok_result(data: dict[str, Any] | None = None, code: str = "OK") -> dict[str, Any]:
    return build_result(STATUS_OK, code, data)


def build_error_result(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"message": message}
    if details:
        data["details"] = details
    return build_result(STATUS_ERROR, code, data)


def is_valid_request_envelope(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and REQUEST_ENVELOPE_KEYS.issubset(obj.keys())
        and obj.get("schema_version") == REQUEST_SCHEMA_VERSION
        and isinstance(obj.get("operation"), str)
        and isinstance(obj.get("request_id"), str)
        and isinstance(obj.get("payload"), dict)
    )


def is_valid_result_envelope(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and RESULT_ENVELOPE_KEYS.issubset(obj.keys())
        and obj.get("schema_version") == RESULT_SCHEMA_VERSION
        and obj.get("status") in (STATUS_OK, STATUS_ERROR)
        and isinstance(obj.get("code"), str)
        and isinstance(obj.get("data"), dict)
    )
