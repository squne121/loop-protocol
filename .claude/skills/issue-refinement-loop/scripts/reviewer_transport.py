#!/usr/bin/env python3
"""Parent-owned, fail-closed transport for issue-reviewer results.

The V2 compact wire is deliberately small, but it is *not* the authority for
the review result.  The parent creates an immutable attempt directory before
starting a child, records transport facts there, and accepts a semantic result
only after the same raw artifact bytes have passed all binding checks.

This module is the V2 contract owner.  Callers must not reimplement its
grammar, artifact layout, or retry policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_V2 = "ISSUE_REVIEW_RESULT_COMPACT_V2"
ATTEMPT_SCHEMA = "REVIEWER_ATTEMPT_RESULT_V1"
MANIFEST_SCHEMA = "REVIEWER_ATTEMPT_MANIFEST_V1"
ARTIFACT_SCHEMA = "REVIEWER_COMPACT_ARTIFACT_V2"
MAX_ATTEMPTS = 3
PER_ATTEMPT_DEADLINE_SECONDS = 90
TOTAL_DEADLINE_SECONDS = 240
STDOUT_CAP = 65_536
STDERR_CAP = 262_144
ARTIFACT_MAX_BYTES = 1_048_576
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
_ATTEMPT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

V2_FIELDS = (
    "SCHEMA",
    "STATUS",
    "VERDICT",
    "SUMMARY",
    "BLOCKERS",
    "NEXT_ACTION",
    "MUST_READ",
    "REVIEWED_BODY_SHA256",
    "ATTEMPT_ID",
    "ARTIFACT",
    "ARTIFACT_SHA256",
)


def canonical_json_bytes(value: Any) -> bytes:
    """#2068 parity: deterministic UTF-8 JSON digest preimage."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_prefixed(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def generate_invocation_id() -> str:
    return str(uuid.uuid4())


def strict_json_loads(raw: bytes | str) -> Any:
    """Parse JSON while rejecting duplicate members and non-finite constants."""
    text = raw.decode("utf-8", "strict") if isinstance(raw, bytes) else raw

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    return json.loads(
        text, object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError("non_finite_json"))
    )


def _relative_parts(path: str) -> tuple[str, ...]:
    candidate = Path(path)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError("artifact_path_not_relative")
    return candidate.parts


def artifact_relative_path(issue_number: int, invocation_id: str, attempt: int) -> str:
    if issue_number <= 0 or attempt <= 0 or not _ATTEMPT.match(invocation_id):
        raise ValueError("invalid_artifact_identity")
    return f"{issue_number}/{invocation_id}/attempt-{attempt:03d}/compact_review_result_v2.json"


def attempt_relative_dir(issue_number: int, invocation_id: str, attempt: int) -> str:
    return str(Path(artifact_relative_path(issue_number, invocation_id, attempt)).parent)


def _atomic_create(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def _artifact_file(root: Path, relative: str) -> Path:
    # Lexical validation plus parent-created components.  Consumption uses
    # dir-fd traversal below and never trusts this Path for security.
    return root.joinpath(*_relative_parts(relative))


def _bounded_reader(stream: Any, cap: int, bucket: dict[str, Any]) -> None:
    digest = hashlib.sha256()
    prefix = bytearray()
    suffix = bytearray()
    length = 0
    while True:
        block = stream.read(8192)
        if not block:
            break
        length += len(block)
        digest.update(block)
        if len(prefix) < cap:
            prefix.extend(block[: cap - len(prefix)])
        suffix.extend(block)
        if len(suffix) > cap:
            del suffix[:-cap]
    bucket.update(length=length, sha256="sha256:" + digest.hexdigest(), prefix=bytes(prefix), suffix=bytes(suffix))


def _redact_bytes(raw: bytes) -> str:
    # The values are diagnostic-only and capped.  Keep no env/prompt/secret
    # material in receipt telemetry.
    return "<redacted:%d-bytes>" % len(raw)


def _make_manifest(
    *,
    issue_number: int,
    repo: str,
    invocation_id: str,
    attempt: int,
    backend: str,
    session_id: str | None,
    retry_intent: str,
) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "issue_number": issue_number,
        "repository": repo,
        "invocation_id": invocation_id,
        "attempt": attempt,
        "backend": backend,
        "session_id": session_id,
        "retry_intent": retry_intent,
        "transport_status": "started",
    }


def _write_json_once(root: Path, relative: str, value: dict[str, Any]) -> Path:
    path = _artifact_file(root, relative)
    _atomic_create(path, canonical_json_bytes(value))
    return path


@dataclass(frozen=True)
class RetryIntent:
    kind: str
    session_id: str | None


def retry_matrix(*, backend: str, initial_session_id: str | None, attempt: int, reason_code: str) -> RetryIntent | None:
    """Closed three-attempt matrix; no implicit fourth or fallback path."""
    if attempt >= MAX_ATTEMPTS or reason_code not in {
        "spawn_failure",
        "timeout",
        "signal",
        "empty_output",
        "nonzero_exit",
        "malformed_output",
        "capture_failure",
        "artifact_validation_failure",
    }:
        return None
    if attempt == 1 and initial_session_id:
        return RetryIntent("same_session_resume", initial_session_id)
    return RetryIntent("fresh_session_replacement", None)


def build_compact_v2(
    *,
    verdict: str,
    summary: str,
    blockers: int,
    reviewed_body_sha256: str,
    attempt_id: str,
    artifact_relative: str,
    artifact_sha256: str,
    must_read: str = "",
) -> bytes:
    if (
        verdict not in {"approve", "needs-fix"}
        or blockers < 0
        or not _SHA.match(reviewed_body_sha256)
        or not _ATTEMPT.match(attempt_id)
        or not _SHA.match(artifact_sha256)
    ):
        raise ValueError("invalid_compact_v2_values")
    action = "proceed" if verdict == "approve" else "request_changes"
    if (verdict == "approve" and blockers != 0) or (verdict == "needs-fix" and blockers == 0):
        raise ValueError("compact_v2_cross_field_invalid")
    values = {
        "SCHEMA": SCHEMA_V2,
        "STATUS": "ok",
        "VERDICT": verdict,
        "SUMMARY": summary,
        "BLOCKERS": str(blockers),
        "NEXT_ACTION": action,
        "MUST_READ": must_read,
        "REVIEWED_BODY_SHA256": reviewed_body_sha256,
        "ATTEMPT_ID": attempt_id,
        "ARTIFACT": "compact_review_result_v2=" + artifact_relative,
        "ARTIFACT_SHA256": artifact_sha256,
    }
    return ("\n".join(f"{key}: {values[key]}" for key in V2_FIELDS) + "\n").encode("utf-8")


def validate_compact_v2(
    raw: bytes | str, *, issue_number: int | None = None, invocation_id: str | None = None, attempt: int | None = None
) -> dict[str, Any]:
    wire = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    result: dict[str, Any] = {
        "schema": "REVIEW_COMPACT_VALIDATION_RESULT_V2",
        "validation_status": "invalid",
        "normalized_payload": None,
        "violations": [],
        "input_sha256": sha256_prefixed(wire),
        "input_byte_count": len(wire),
    }
    try:
        text = wire.decode("utf-8", "strict")
    except UnicodeDecodeError:
        result["violations"].append({"code": "utf8_decode_error"})
        return result
    if not text.endswith("\n") or "\r" in text or len(wire) > 2048:
        result["violations"].append({"code": "wire_format_invalid"})
        return result
    lines = text[:-1].split("\n")
    if len(lines) != len(V2_FIELDS):
        result["violations"].append({"code": "line_count_invalid"})
        return result
    values: dict[str, str] = {}
    for expected, line in zip(V2_FIELDS, lines):
        if not line.startswith(expected + ": "):
            result["violations"].append({"code": "field_order_or_name_invalid", "field": expected})
            return result
        value = line[len(expected) + 2 :]
        if "\x00" in value or "\n" in value:
            result["violations"].append({"code": "control_character"})
            return result
        values[expected] = value
    artifact_prefix = "compact_review_result_v2="
    if (
        values["SCHEMA"] != SCHEMA_V2
        or values["STATUS"] != "ok"
        or values["VERDICT"] not in {"approve", "needs-fix"}
        or values["NEXT_ACTION"] != ("proceed" if values["VERDICT"] == "approve" else "request_changes")
    ):
        result["violations"].append({"code": "value_invalid"})
        return result
    if (
        not values["BLOCKERS"].isdigit()
        or (values["VERDICT"] == "approve") != (values["BLOCKERS"] == "0")
        or not _SHA.match(values["REVIEWED_BODY_SHA256"])
        or not _SHA.match(values["ARTIFACT_SHA256"])
        or not _ATTEMPT.match(values["ATTEMPT_ID"])
        or not values["ARTIFACT"].startswith(artifact_prefix)
    ):
        result["violations"].append({"code": "cross_field_invalid"})
        return result
    relative = values["ARTIFACT"][len(artifact_prefix) :]
    try:
        parts = _relative_parts(relative)
        if issue_number is not None and (not parts or parts[0] != str(issue_number)):
            raise ValueError("issue_mismatch")
        if invocation_id is not None and (len(parts) < 2 or parts[1] != invocation_id):
            raise ValueError("invocation_mismatch")
        if attempt is not None and (len(parts) < 3 or parts[2] != f"attempt-{attempt:03d}"):
            raise ValueError("attempt_mismatch")
    except ValueError as exc:
        result["violations"].append({"code": str(exc)})
        return result
    result.update(validation_status="valid", normalized_payload=values)
    return result


def _open_no_follow(root: Path, relative: str) -> tuple[int, int]:
    """Open root and each path component by FD; returns (root_fd, leaf_fd)."""
    required = ("O_NOFOLLOW", "O_DIRECTORY")
    if not hasattr(os, "open") or any(not hasattr(os, name) for name in required) or not os.supports_dir_fd:
        raise OSError("unsupported_secure_open_capability")
    parts = _relative_parts(relative)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current = root_fd
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            if current != root_fd:
                os.close(current)
            current = next_fd
        leaf_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current)
        if current != root_fd:
            os.close(current)
        return root_fd, leaf_fd
    except BaseException:
        if current != root_fd:
            os.close(current)
        os.close(root_fd)
        raise


def secure_read_json(
    *, artifact_root: Path, artifact_relative: str, max_bytes: int = ARTIFACT_MAX_BYTES
) -> dict[str, Any]:
    """Open+read exact artifact bytes exactly once via dir-fd-anchored,
    component-wise ``O_DIRECTORY|O_NOFOLLOW`` traversal, then parse the SAME
    raw bytes with both SHA-256 and strict JSON.  No ``Path.resolve()`` ->
    ``stat()`` -> separate ``open()`` fallback is used anywhere in this path
    (AC6).  Callers layer schema/binding-specific checks on top of this
    generic, reusable primitive; this function performs no such checks
    itself.
    """
    try:
        root_fd, leaf_fd = _open_no_follow(artifact_root, artifact_relative)
        try:
            file_stat = os.fstat(leaf_fd)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > max_bytes:
                raise ValueError("non_regular_or_oversize_artifact")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(leaf_fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(leaf_fd)
            os.close(root_fd)
        if len(raw) > max_bytes:
            raise ValueError("raw_byte_oversize")
        payload = strict_json_loads(raw)
        return {"status": "valid", "raw_bytes": raw, "sha256": sha256_prefixed(raw), "payload": payload}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "integrity_failure", "reason_code": str(exc)}


def verify_artifact(
    *,
    artifact_root: Path,
    artifact_relative: str,
    expected_repo: str,
    expected_issue: int,
    expected_body_sha256: str,
    expected_invocation_id: str,
    expected_attempt: int,
    expected_sha256: str,
) -> dict[str, Any]:
    """Verify exact artifact bytes once.  Any inability is integrity failure."""
    read = secure_read_json(artifact_root=artifact_root, artifact_relative=artifact_relative)
    if read["status"] != "valid":
        return read
    raw = read["raw_bytes"]
    payload = read["payload"]
    if read["sha256"] != expected_sha256:
        return {"status": "integrity_failure", "reason_code": "raw_byte_hash_mismatch"}
    if not isinstance(payload, dict) or payload.get("schema") != ARTIFACT_SCHEMA:
        return {"status": "integrity_failure", "reason_code": "schema_mismatch"}
    expected = {
        "repository": expected_repo,
        "issue_number": expected_issue,
        "reviewed_body_sha256": expected_body_sha256,
        "invocation_id": expected_invocation_id,
        "attempt": expected_attempt,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        return {"status": "integrity_failure", "reason_code": "artifact_binding_mismatch"}
    return {"status": "valid", "raw_bytes": raw, "payload": payload}


def write_semantic_artifact(
    *,
    artifact_root: Path,
    issue_number: int,
    repo: str,
    invocation_id: str,
    attempt: int,
    reviewed_body_sha256: str,
    semantic_result: dict[str, Any],
) -> tuple[str, str]:
    relative = artifact_relative_path(issue_number, invocation_id, attempt)
    payload = {
        "schema": ARTIFACT_SCHEMA,
        "repository": repo,
        "issue_number": issue_number,
        "reviewed_body_sha256": reviewed_body_sha256,
        "invocation_id": invocation_id,
        "attempt": attempt,
        "semantic_result": semantic_result,
    }
    raw = canonical_json_bytes(payload)
    _atomic_create(_artifact_file(artifact_root, relative), raw)
    return relative, sha256_prefixed(raw)


def _attempt_result(**kwargs: Any) -> dict[str, Any]:
    return {"schema": ATTEMPT_SCHEMA, "transport_status": "environment_failure", "semantic_verdict": None, **kwargs}


def run_reviewer_transport(
    *,
    command: list[str],
    command_id: str,
    argv_template_id: str,
    backend: str,
    issue_number: int,
    repo: str,
    reviewed_body_sha256: str,
    artifact_root: Path,
    invocation_id: str | None = None,
    session_id: str | None = None,
    per_attempt_deadline: int = PER_ATTEMPT_DEADLINE_SECONDS,
    total_deadline: int = TOTAL_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """Run a reviewer child with process-group reaping and closed retries."""
    if not command or issue_number <= 0 or not _SHA.match(reviewed_body_sha256):
        raise ValueError("invalid_transport_request")
    invocation_id = invocation_id or generate_invocation_id()
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    active_session = session_id
    retry_intent_kind = "initial"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if time.monotonic() - started >= total_deadline:
            break
        intent = retry_intent_kind
        manifest_relative = attempt_relative_dir(issue_number, invocation_id, attempt) + "/attempt_manifest.json"
        _write_json_once(
            artifact_root,
            manifest_relative,
            _make_manifest(
                issue_number=issue_number,
                repo=repo,
                invocation_id=invocation_id,
                attempt=attempt,
                backend=backend,
                session_id=active_session,
                retry_intent=intent,
            ),
        )
        common = {
            "invocation_id": invocation_id,
            "attempt": attempt,
            "backend": backend,
            "session_id": active_session,
            "command_id": command_id,
            "argv_template_id": argv_template_id,
            "rendered_argv_sha256": sha256_prefixed("\0".join(command).encode()),
            "pid": None,
            "process_group": None,
            "exit_code": None,
            "signal": None,
            "timeout": False,
            "descendants_reaped": False,
            "stdout_length": 0,
            "stdout_sha256": None,
            "stderr_length": 0,
            "stderr_sha256": None,
            "stdout_prefix": "<redacted:0-bytes>",
            "stdout_suffix": "<redacted:0-bytes>",
            "stderr_prefix": "<redacted:0-bytes>",
            "stderr_suffix": "<redacted:0-bytes>",
        }
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            result = _attempt_result(
                **common, failure_phase="spawn", reason_code="spawn_failure", spawn_error=type(exc).__name__
            )
            result_relative = attempt_relative_dir(issue_number, invocation_id, attempt) + "/attempt_result.json"
            _write_json_once(artifact_root, result_relative, result)
            results.append(result)
            retry = retry_matrix(
                backend=backend, initial_session_id=session_id, attempt=attempt, reason_code=result["reason_code"]
            )
            if retry is None:
                break
            active_session = retry.session_id if retry.session_id is not None else generate_invocation_id()
            retry_intent_kind = retry.kind
            continue
        common.update(pid=process.pid, process_group=process.pid)
        stdout: dict[str, Any] = {}
        stderr: dict[str, Any] = {}
        threads = [
            threading.Thread(target=_bounded_reader, args=(process.stdout, STDOUT_CAP, stdout)),
            threading.Thread(target=_bounded_reader, args=(process.stderr, STDERR_CAP, stderr)),
        ]
        [thread.start() for thread in threads]
        timed_out = False
        try:
            process.wait(timeout=min(per_attempt_deadline, max(1, total_deadline - (time.monotonic() - started))))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        [thread.join() for thread in threads]
        common.update(
            timeout=timed_out,
            descendants_reaped=timed_out,
            exit_code=process.returncode,
            signal=(-process.returncode if process.returncode and process.returncode < 0 else None),
            stdout_length=stdout.get("length", 0),
            stdout_sha256=stdout.get("sha256"),
            stderr_length=stderr.get("length", 0),
            stderr_sha256=stderr.get("sha256"),
            stdout_prefix=_redact_bytes(stdout.get("prefix", b"")),
            stdout_suffix=_redact_bytes(stdout.get("suffix", b"")),
            stderr_prefix=_redact_bytes(stderr.get("prefix", b"")),
            stderr_suffix=_redact_bytes(stderr.get("suffix", b"")),
        )
        if timed_out:
            result = _attempt_result(**common, failure_phase="execution", reason_code="timeout")
        elif process.returncode != 0:
            result = _attempt_result(
                **common, failure_phase="execution", reason_code=("signal" if common["signal"] else "nonzero_exit")
            )
        elif not stdout.get("length"):
            result = _attempt_result(**common, failure_phase="capture", reason_code="empty_output")
        elif stdout["length"] > STDOUT_CAP:
            result = _attempt_result(**common, failure_phase="capture", reason_code="capture_failure")
        else:
            # The child returns structured review JSON.  It never authors the
            # compact wire or artifact: this parent is the V2 producer.
            try:
                semantic = strict_json_loads(stdout.get("prefix", b""))
                verdict = semantic.get("verdict") if isinstance(semantic, dict) else None
                blockers = semantic.get("blocking_issues", []) if isinstance(semantic, dict) else []
                if verdict not in {"approve", "needs-fix"} or not isinstance(blockers, list):
                    raise ValueError("semantic_result_invalid")
                count = len(blockers)
                if (verdict == "approve" and count != 0) or (verdict == "needs-fix" and count == 0):
                    raise ValueError("semantic_result_cross_field_invalid")
                relative, digest = write_semantic_artifact(
                    artifact_root=artifact_root,
                    issue_number=issue_number,
                    repo=repo,
                    invocation_id=invocation_id,
                    attempt=attempt,
                    reviewed_body_sha256=reviewed_body_sha256,
                    semantic_result=semantic,
                )
                wire = build_compact_v2(
                    verdict=verdict,
                    summary=("contract ready" if verdict == "approve" else f"{count} blocker(s)"),
                    blockers=count,
                    reviewed_body_sha256=reviewed_body_sha256,
                    attempt_id=invocation_id,
                    artifact_relative=relative,
                    artifact_sha256=digest,
                )
                compact = validate_compact_v2(
                    wire, issue_number=issue_number, invocation_id=invocation_id, attempt=attempt
                )
                if compact["validation_status"] != "valid":
                    raise ValueError("parent_compact_validation_failure")
                fields = compact["normalized_payload"]
                verified = verify_artifact(
                    artifact_root=artifact_root,
                    artifact_relative=relative,
                    expected_repo=repo,
                    expected_issue=issue_number,
                    expected_body_sha256=reviewed_body_sha256,
                    expected_invocation_id=invocation_id,
                    expected_attempt=attempt,
                    expected_sha256=digest,
                )
                if verified["status"] != "valid":
                    result = _attempt_result(
                        **common,
                        failure_phase="artifact_validation",
                        reason_code="integrity_failure",
                        artifact_validation=verified,
                    )
                else:
                    result = {
                        "schema": ATTEMPT_SCHEMA,
                        "transport_status": "ok",
                        "semantic_verdict": verdict,
                        **common,
                        "failure_phase": None,
                        "reason_code": None,
                        "compact": fields,
                    }
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                result = _attempt_result(
                    **common,
                    failure_phase="compact_validation",
                    reason_code="malformed_output",
                    compact_error=type(exc).__name__,
                )
        result_relative = attempt_relative_dir(issue_number, invocation_id, attempt) + "/attempt_result.json"
        _write_json_once(artifact_root, result_relative, result)
        results.append(result)
        if result["transport_status"] == "ok":
            return {
                "schema": "REVIEWER_TRANSPORT_RESULT_V1",
                "transport_status": "ok",
                "semantic_verdict": result["semantic_verdict"],
                "invocation_id": invocation_id,
                "attempts": results,
            }
        retry = retry_matrix(
            backend=backend, initial_session_id=session_id, attempt=attempt, reason_code=result["reason_code"]
        )
        if retry is None:
            break
        active_session = retry.session_id if retry.session_id is not None else generate_invocation_id()
        retry_intent_kind = retry.kind
    return {
        "schema": "REVIEWER_TRANSPORT_RESULT_V1",
        "transport_status": "environment_failure",
        "semantic_verdict": None,
        "invocation_id": invocation_id,
        "attempts": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run/validate reviewer transport V2")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--invocation-id")
    parser.add_argument("--attempt", type=int)
    args = parser.parse_args(argv)
    raw = sys.stdin.buffer.read()
    if not args.validate:
        parser.error("only --validate is a CLI surface; execution is parent API only")
    value = validate_compact_v2(
        raw, issue_number=args.issue_number, invocation_id=args.invocation_id, attempt=args.attempt
    )
    sys.stdout.buffer.write(canonical_json_bytes(value) + b"\n")
    return 0 if value["validation_status"] == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
