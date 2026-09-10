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

import os
import sys
import uuid
from typing import Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import task_context_errors as errors  # noqa: E402

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


def validate_and_unwrap_request(obj: Any, *, expected_operation: str) -> dict[str, Any]:
    """Strictly validate a top-level request envelope object against the
    frozen shape and unwrap its ``payload`` (fix_delta finding 1 --
    ``task-contextctl`` must actually validate/unwrap the request envelope
    it claims to freeze, instead of treating raw stdin JSON as the operation
    payload directly).

    Unlike ``is_valid_request_envelope`` (a loose boolean shape check used
    by envelope-builder self-tests), this raises a typed
    ``errors.ValidationError`` that identifies exactly what is wrong, and
    additionally rejects:

    - any top-level field not in ``REQUEST_ENVELOPE_KEYS`` (schema
      ``additionalProperties: false``),
    - a missing required top-level field,
    - a ``schema_version`` other than ``REQUEST_SCHEMA_VERSION``,
    - a non-string/empty ``operation`` or ``request_id``,
    - a non-object ``payload``,
    - an ``operation`` that does not match ``expected_operation`` (the
      operation the CLI already deterministically derived from argv/the
      subcommand invoked).

    Returns the envelope's ``payload`` dict on success -- this becomes the
    operation-specific input passed to ``task_contextctl._dispatch``.
    """
    if not isinstance(obj, dict):
        raise errors.ValidationError("request envelope must be a JSON object (mapping)")

    extra = set(obj.keys()) - REQUEST_ENVELOPE_KEYS
    if extra:
        raise errors.ValidationError(
            f"request envelope has unexpected top-level field(s): {sorted(extra)}", extra_fields=sorted(extra)
        )

    missing = REQUEST_ENVELOPE_KEYS - set(obj.keys())
    if missing:
        raise errors.ValidationError(
            f"request envelope is missing required top-level field(s): {sorted(missing)}",
            missing_fields=sorted(missing),
        )

    schema_version = obj.get("schema_version")
    if schema_version != REQUEST_SCHEMA_VERSION:
        raise errors.ValidationError(
            f"request envelope schema_version must be {REQUEST_SCHEMA_VERSION!r}, got {schema_version!r}"
        )

    operation = obj.get("operation")
    if not isinstance(operation, str) or not operation:
        raise errors.ValidationError("request envelope 'operation' must be a non-empty string")

    request_id = obj.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise errors.ValidationError("request envelope 'request_id' must be a non-empty string")

    payload = obj.get("payload")
    if not isinstance(payload, dict):
        raise errors.ValidationError("request envelope 'payload' must be a JSON object (mapping)")

    if operation != expected_operation:
        raise errors.ValidationError(
            f"request envelope operation {operation!r} does not match the operation "
            f"{expected_operation!r} determined from the CLI command invoked",
            envelope_operation=operation,
            expected_operation=expected_operation,
        )

    return payload


def is_valid_result_envelope(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and RESULT_ENVELOPE_KEYS.issubset(obj.keys())
        and obj.get("schema_version") == RESULT_SCHEMA_VERSION
        and obj.get("status") in (STATUS_OK, STATUS_ERROR)
        and isinstance(obj.get("code"), str)
        and isinstance(obj.get("data"), dict)
    )
