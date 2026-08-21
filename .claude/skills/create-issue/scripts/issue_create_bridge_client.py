#!/usr/bin/env python3
"""Isolated-child bridge client for ``ISOLATION_ISSUE_CREATE_REQUEST_V1``.

Issue #2259: isolated Claude-GPT sessions never hold host GitHub credentials.
When ``create_issue_txn.py`` detects that it is running under an
isolation profile explicitly announced by the launcher (``CLAUDE_GPT_ISOLATION_PROFILE``),
it must not invoke raw ``gh issue create`` itself. Instead it builds a
closed-schema ``ISOLATION_ISSUE_CREATE_REQUEST_V1`` request and sends it over a
run-scoped Unix domain socket (never stdout, per Stop Conditions) to the
launcher-owned parent bridge server (``scripts/claude-gpt/issue_create_bridge_server.py``).

Design note (intentional duplication): the strict encode/decode logic below is
independently re-implemented on the server side
(``scripts/claude-gpt/issue_create_bridge_server.py``) rather than imported from
here. The parent process must never trust or execute child-controlled code paths
for its own validation -- only the schema *shape* is shared by convention, not by
import, across the trust boundary (AC1/AC4).
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass, field
from typing import Any

REQUEST_SCHEMA_NAME = "ISOLATION_ISSUE_CREATE_REQUEST_V1"
RESULT_SCHEMA_NAME = "ISOLATION_ISSUE_CREATE_RESULT_V1"

REQUEST_KEYS = frozenset(
    {
        "schema",
        "request_id",
        "run_nonce",
        "claimed_repo",
        "title",
        "body",
        "labels",
        "issue_kind",
        "label_profile",
        "parent_issue_number",
        "dependency_issue_numbers",
        "blocking_issue_numbers",
    }
)

RESULT_KEYS = frozenset(
    {
        "schema",
        "request_id",
        "status",
        "issue_number",
        "issue_url",
        "node_id",
        "body_sha256",
        "completed_steps",
        "failure_stage",
        "failure_message",
        "reconciled",
    }
)

RESULT_STATUSES = frozenset({"success", "partial_failure", "failure", "duplicate"})
LABEL_PROFILES = frozenset({"standard", "triage_only"})

_MAX_TITLE_LEN = 256
_MAX_BODY_LEN = 60_000
_MAX_LABELS = 20
_MAX_LABEL_LEN = 50
_MAX_ISSUE_KIND_LEN = 50
_MAX_CLAIMED_REPO_LEN = 200
_MAX_DEPENDENCY_ITEMS = 50
_MAX_RUN_NONCE_LEN = 128
_SOCKET_RECV_LIMIT_BYTES = 1_048_576  # 1 MiB -- bounded read to avoid memory DoS


class BridgeSchemaError(ValueError):
    """Raised when a request/result payload violates the closed schema."""


class BridgeClientError(RuntimeError):
    """Raised for transport-level bridge client failures (connect/timeout/protocol)."""


def is_isolated_profile() -> bool:
    """Return True when the launcher has explicitly announced an isolation profile.

    Authority for isolation detection lives entirely in this single env var,
    set only by ``launch.sh``. Network/auth failures or ``gh auth status``
    outcomes are never used to infer isolation (Stop Conditions,
    "絶対に避けるべき修正方向").
    """
    return os.environ.get("CLAUDE_GPT_ISOLATION_PROFILE", "") == "1"


def bridge_socket_path() -> str:
    value = os.environ.get("CLAUDE_GPT_ISSUE_CREATE_SOCKET", "")
    if not value:
        raise BridgeClientError("CLAUDE_GPT_ISSUE_CREATE_SOCKET is not set")
    return value


def bridge_run_nonce() -> str:
    value = os.environ.get("CLAUDE_GPT_ISSUE_CREATE_RUN_NONCE", "")
    if not value:
        raise BridgeClientError("CLAUDE_GPT_ISSUE_CREATE_RUN_NONCE is not set")
    return value


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise BridgeSchemaError(f"duplicate JSON key: {key!r}")
        seen.add(key)
        result[key] = value
    return result


def _decode_strict_object(raw: str) -> dict[str, Any]:
    obj = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    if not isinstance(obj, dict):
        raise BridgeSchemaError("top-level payload must be a JSON object")
    return obj


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_int_or_none(value: Any) -> bool:
    return value is None or _is_strict_int(value)


@dataclass(frozen=True)
class IssueCreateRequest:
    request_id: str
    run_nonce: str
    claimed_repo: str
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    issue_kind: str = ""
    label_profile: str = "standard"
    parent_issue_number: int | None = None
    dependency_issue_numbers: list[int] = field(default_factory=list)
    blocking_issue_numbers: list[int] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema": REQUEST_SCHEMA_NAME,
            "request_id": self.request_id,
            "run_nonce": self.run_nonce,
            "claimed_repo": self.claimed_repo,
            "title": self.title,
            "body": self.body,
            "labels": list(self.labels),
            "issue_kind": self.issue_kind,
            "label_profile": self.label_profile,
            "parent_issue_number": self.parent_issue_number,
            "dependency_issue_numbers": list(self.dependency_issue_numbers),
            "blocking_issue_numbers": list(self.blocking_issue_numbers),
        }


def validate_request_payload(obj: dict[str, Any]) -> None:
    """Raise BridgeSchemaError if ``obj`` does not exactly match REQUEST schema v1."""
    keys = set(obj.keys())
    unknown = keys - REQUEST_KEYS
    if unknown:
        raise BridgeSchemaError(f"unknown request keys: {sorted(unknown)}")
    missing = REQUEST_KEYS - keys
    if missing:
        raise BridgeSchemaError(f"missing request keys: {sorted(missing)}")
    if obj["schema"] != REQUEST_SCHEMA_NAME:
        raise BridgeSchemaError(f"schema mismatch: {obj['schema']!r}")
    if not isinstance(obj["request_id"], str) or not obj["request_id"]:
        raise BridgeSchemaError("request_id must be a non-empty string")
    if not isinstance(obj["run_nonce"], str) or not (1 <= len(obj["run_nonce"]) <= _MAX_RUN_NONCE_LEN):
        raise BridgeSchemaError("run_nonce out of bounds")
    if not isinstance(obj["claimed_repo"], str) or not (1 <= len(obj["claimed_repo"]) <= _MAX_CLAIMED_REPO_LEN):
        raise BridgeSchemaError("claimed_repo out of bounds")
    if not isinstance(obj["title"], str) or not (1 <= len(obj["title"]) <= _MAX_TITLE_LEN):
        raise BridgeSchemaError("title out of bounds")
    if not isinstance(obj["body"], str) or len(obj["body"]) > _MAX_BODY_LEN:
        raise BridgeSchemaError("body out of bounds")
    labels = obj["labels"]
    if not isinstance(labels, list) or len(labels) > _MAX_LABELS:
        raise BridgeSchemaError("labels out of bounds")
    for label in labels:
        if not isinstance(label, str) or not (1 <= len(label) <= _MAX_LABEL_LEN):
            raise BridgeSchemaError(f"invalid label: {label!r}")
    if not isinstance(obj["issue_kind"], str) or len(obj["issue_kind"]) > _MAX_ISSUE_KIND_LEN:
        raise BridgeSchemaError("issue_kind out of bounds")
    if obj["label_profile"] not in LABEL_PROFILES:
        raise BridgeSchemaError(f"invalid label_profile: {obj['label_profile']!r}")
    if not _is_strict_int_or_none(obj["parent_issue_number"]):
        raise BridgeSchemaError("parent_issue_number must be int or null")
    if isinstance(obj["parent_issue_number"], int) and obj["parent_issue_number"] < 1:
        raise BridgeSchemaError("parent_issue_number must be >= 1")
    for field_name in ("dependency_issue_numbers", "blocking_issue_numbers"):
        values = obj[field_name]
        if not isinstance(values, list) or len(values) > _MAX_DEPENDENCY_ITEMS:
            raise BridgeSchemaError(f"{field_name} out of bounds")
        for item in values:
            if not _is_strict_int(item) or item < 1:
                raise BridgeSchemaError(f"invalid {field_name} entry: {item!r}")


def validate_result_payload(obj: dict[str, Any]) -> None:
    """Raise BridgeSchemaError if ``obj`` does not exactly match RESULT schema v1."""
    keys = set(obj.keys())
    unknown = keys - RESULT_KEYS
    if unknown:
        raise BridgeSchemaError(f"unknown result keys: {sorted(unknown)}")
    missing = RESULT_KEYS - keys
    if missing:
        raise BridgeSchemaError(f"missing result keys: {sorted(missing)}")
    if obj["schema"] != RESULT_SCHEMA_NAME:
        raise BridgeSchemaError(f"schema mismatch: {obj['schema']!r}")
    if not isinstance(obj["request_id"], str) or not obj["request_id"]:
        raise BridgeSchemaError("request_id must be a non-empty string")
    if obj["status"] not in RESULT_STATUSES:
        raise BridgeSchemaError(f"invalid status: {obj['status']!r}")
    if not _is_strict_int_or_none(obj["issue_number"]):
        raise BridgeSchemaError("issue_number must be int or null")
    for field_name in ("issue_url", "node_id", "body_sha256", "failure_stage", "failure_message"):
        if obj[field_name] is not None and not isinstance(obj[field_name], str):
            raise BridgeSchemaError(f"{field_name} must be str or null")
    if not isinstance(obj["completed_steps"], list) or not all(isinstance(s, str) for s in obj["completed_steps"]):
        raise BridgeSchemaError("completed_steps must be a list of strings")
    if not isinstance(obj["reconciled"], bool):
        raise BridgeSchemaError("reconciled must be a bool")


def build_request(
    *,
    claimed_repo: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
    issue_kind: str = "",
    label_profile: str = "standard",
    parent_issue_number: int | None = None,
    dependency_issue_numbers: list[int] | None = None,
    blocking_issue_numbers: list[int] | None = None,
) -> IssueCreateRequest:
    request = IssueCreateRequest(
        request_id=uuid.uuid4().hex,
        run_nonce=bridge_run_nonce(),
        claimed_repo=claimed_repo,
        title=title,
        body=body,
        labels=list(labels or []),
        issue_kind=issue_kind,
        label_profile=label_profile,
        parent_issue_number=parent_issue_number,
        dependency_issue_numbers=list(dependency_issue_numbers or []),
        blocking_issue_numbers=list(blocking_issue_numbers or []),
    )
    validate_request_payload(request.to_json_dict())
    return request


def send_issue_create_request(
    request: IssueCreateRequest,
    *,
    connect_timeout_seconds: float = 10.0,
    total_timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    """Send a request over the bridge Unix domain socket and return the parsed result.

    Raises BridgeClientError on transport failure and BridgeSchemaError on a
    malformed/non-conformant result payload. Never writes the request/result to
    stdout (Stop Conditions: no magic JSON mixed into model output).
    """
    socket_path = bridge_socket_path()
    payload = json.dumps(request.to_json_dict(), ensure_ascii=True, sort_keys=True)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(connect_timeout_seconds)
        try:
            sock.connect(socket_path)
        except OSError as exc:
            raise BridgeClientError(f"failed to connect to bridge socket {socket_path!r}: {exc}") from exc
        sock.settimeout(total_timeout_seconds)
        try:
            sock.sendall((payload + "\n").encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)
        except OSError as exc:
            raise BridgeClientError(f"failed to send request to bridge socket: {exc}") from exc

        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError as exc:
                raise BridgeClientError(f"failed to read response from bridge socket: {exc}") from exc
            if not chunk:
                break
            total += len(chunk)
            if total > _SOCKET_RECV_LIMIT_BYTES:
                raise BridgeClientError("bridge response exceeded size limit")
            chunks.append(chunk)
    finally:
        sock.close()

    raw = b"".join(chunks).decode("utf-8").strip()
    if not raw:
        raise BridgeClientError("bridge server returned an empty response")
    try:
        result = _decode_strict_object(raw)
    except BridgeSchemaError:
        raise
    except json.JSONDecodeError as exc:
        raise BridgeClientError(f"bridge response is not valid JSON: {exc}") from exc

    validate_result_payload(result)
    if result["request_id"] != request.request_id:
        raise BridgeClientError(
            f"bridge response request_id mismatch: expected {request.request_id!r}, got {result['request_id']!r}"
        )
    return result
