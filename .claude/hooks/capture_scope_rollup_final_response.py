#!/usr/bin/env python3
"""Capture scope-rollup-runner final responses from SubagentStop hook payloads."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Issue #1527 Scope Delta (2): eligibility/readiness are validated exclusively
# against the fixed private location authority in
# .claude/scripts/check_session_recording_runtime_safety.py — never from
# hook-payload-supplied inline objects, arbitrary paths, or artifacts[]
# fuzzy-match values. Import it as a sibling module under .claude/scripts.
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import check_session_recording_runtime_safety as _srrs  # noqa: E402

MARKER_NAME = "ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1"
TARGET_AGENT_TYPE = "scope-rollup-runner"
DEFAULT_CAPTURE_DIR = Path("/tmp")
FENCED_YAML_RE = re.compile(r"```ya?ml[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
INVOCATION_ID_RE = re.compile(r"^\s*invocation_id:\s*['\"]?([A-Za-z0-9._:-]+)['\"]?\s*$", re.MULTILINE)
STRICT_STATUS = {"ok", "failed", "runner_unavailable"}

# Codex-only transport gate: only when the caller (the Codex adapter's
# `uv run`/fixed-interpreter invocation) sets this env var does the producer
# enforce fixed-location source-bound eligibility/readiness. The Claude
# `session_manifest_coordinator.sh` path never sets it and therefore keeps
# capturing raw payloads exactly as before #1527 (Scope Delta (2) AC16
# regression guard). This flag can only be set by our own trusted process
# spawn — never by hook-payload content.
REQUIRE_SOURCE_BOUND_ENV = "SCOPE_ROLLUP_REQUIRE_SOURCE_BOUND_ELIGIBILITY"

READINESS_SCHEMA = "SESSION_RECORDING_SCOPE_ROLLUP_READINESS_V1"
READINESS_KEYS = frozenset(
    {
        "schema",
        "artifact_version",
        "repo_root_realpath",
        "uv_lock_digest",
        "python_version_digest",
        "interpreter_realpath",
        "interpreter_version",
        "producer_digest",
        "prepared",
        "generated_at",
    }
)
READINESS_MAX_BYTES = 8192


def _readiness_artifact_path(repo_root: Path) -> Path:
    override = os.environ.get("SCOPE_ROLLUP_READINESS_ARTIFACT_PATH")
    if override:
        return Path(override)
    return repo_root / ".claude" / "tmp" / "session-recording" / "scope-rollup-readiness.json"


def _load_and_verify_readiness_artifact(
    repo_root: Path, *, hook_received_at: datetime,
) -> tuple[dict[str, Any] | None, str, str | None]:
    path = _readiness_artifact_path(repo_root)
    try:
        st = os.lstat(path)
    except OSError:
        return None, "readiness_missing", None
    if stat.S_ISLNK(st.st_mode):
        return None, "readiness_invalid_symlink", None
    if not stat.S_ISREG(st.st_mode):
        return None, "readiness_invalid_not_regular_file", None
    if stat.S_IMODE(st.st_mode) != 0o600:
        return None, "readiness_invalid_mode", None
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return None, "readiness_invalid_owner", None
    if st.st_size > READINESS_MAX_BYTES:
        return None, "readiness_invalid_size", None

    try:
        raw = path.read_bytes()
    except OSError:
        return None, "readiness_unreadable", None
    digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"

    try:
        artifact = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "readiness_invalid_json", digest
    if not isinstance(artifact, dict):
        return None, "readiness_invalid_json", digest

    extra_keys = set(artifact.keys()) - READINESS_KEYS
    if extra_keys:
        return None, "readiness_invalid_additional_properties", digest
    missing_keys = READINESS_KEYS - set(artifact.keys())
    if missing_keys:
        return None, "readiness_invalid_missing_fields", digest

    if artifact.get("schema") != READINESS_SCHEMA:
        return None, "readiness_invalid_schema", digest
    if artifact.get("repo_root_realpath") != str(repo_root.resolve()):
        return None, "readiness_binding_repo_mismatch", digest
    if artifact.get("prepared") is not True:
        return None, "readiness_unprepared", digest

    producer_digest = f"sha256:{hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}"
    if artifact.get("producer_digest") != producer_digest:
        return None, "readiness_binding_producer_digest_mismatch", digest

    generated_at = _parse_timestamp(artifact.get("generated_at"))
    if generated_at is None:
        return None, "readiness_invalid_timestamp", digest
    if generated_at > hook_received_at:
        return None, "readiness_stale_future_generated_at", digest

    return artifact, "ok", digest


class DuplicateKeyError(ValueError):
    """Raised when a YAML mapping contains duplicate keys."""


class _StrictLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: Any, node: yaml.nodes.MappingNode) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in mapping:
            raise DuplicateKeyError(f"duplicate key: {key}")
        mapping[key] = loader.construct_object(value_node)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,
)


@dataclass
class CaptureDecision:
    capture_mode: str
    capture_status: str
    parser_status: str
    capture_routing_action: str
    agent_type: str | None
    invocation_id: str | None
    requested_at: str | None
    generated_at: str | None
    capture_path: str | None
    capture_sha256: str | None
    capture_source: str
    provenance: dict[str, Any]
    notes: list[str]


def _safe_invocation_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)


def _read_payload() -> dict[str, Any] | None:
    raw = sys.stdin.read()
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _parse_iso8601(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc)


def _normalize_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        return value
    return None


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _payload_digest(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha256_bytes(serialized.encode("utf-8"))


def _load_yaml_no_duplicate_keys(text: str) -> Any:
    return yaml.load(text, Loader=_StrictLoader)


def _extract_marker_payload(last_assistant_message: str) -> tuple[str, dict[str, Any] | None]:
    blocks: list[dict[str, Any]] = []
    parse_failed = False
    for match in FENCED_YAML_RE.finditer(last_assistant_message):
        block_text = match.group(1).strip()
        if MARKER_NAME not in block_text:
            continue
        try:
            parsed = _load_yaml_no_duplicate_keys(block_text)
        except Exception:
            parse_failed = True
            continue
        if not isinstance(parsed, dict):
            continue
        candidate = parsed.get(MARKER_NAME)
        if isinstance(candidate, dict):
            blocks.append(candidate)
        elif candidate is not None:
            parse_failed = True

    if not blocks:
        return ("marker_malformed" if parse_failed else "marker_missing"), None
    if len(blocks) > 1:
        return "marker_ambiguous", None
    return "ok", blocks[0]


def _extract_invocation_id(last_assistant_message: str, marker_payload: dict[str, Any] | None) -> str | None:
    if marker_payload is not None:
        value = marker_payload.get("invocation_id")
        if isinstance(value, str) and value:
            return value
    matches = INVOCATION_ID_RE.findall(last_assistant_message)
    unique = sorted(set(matches))
    if len(unique) == 1:
        return unique[0]
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _parse_iso8601(value)
    except Exception:
        return None


def _evaluate_source_bound_artifacts(
    repo_root: Path,
    marker_payload: dict[str, Any] | None,
    hook_received_at: datetime,
) -> tuple[bool, list[str], str, dict[str, Any]]:
    """Issue #1527 Scope Delta (2) AC12/AC13/AC14: the sole eligibility/
    readiness source is the fixed private location authority in
    check_session_recording_runtime_safety.py — never a hook-payload value.
    Returns (ok, notes, reason_code, provenance).
    """
    # PyYAML resolves an unquoted ISO8601-looking scalar to a native
    # datetime object (not a str) — normalize back to text before parsing,
    # same as every other marker timestamp field in this module.
    marker_generated_at = (
        _parse_timestamp(_normalize_text(marker_payload.get("generated_at"))) if marker_payload else None
    )

    eligibility_artifact, eligibility_reason, eligibility_digest = (
        _srrs.load_and_verify_scope_rollup_eligibility_artifact(
            repo_root,
            hook_received_at=hook_received_at,
            marker_generated_at=marker_generated_at,
        )
    )
    provenance: dict[str, Any] = {
        "eligibility_artifact_digest": eligibility_digest,
        "eligibility_verification_reason_code": eligibility_reason,
        "readiness_artifact_digest": None,
        "readiness_verification_reason_code": None,
    }
    if eligibility_artifact is None:
        note = "source-bound eligibility artifact is missing" if eligibility_reason == "eligibility_missing" \
            else f"source-bound eligibility artifact rejected: {eligibility_reason}"
        # Pass the underlying reason code through verbatim (rather than
        # collapsing distinct failure modes into a small bucket) so callers
        # can distinguish e.g. binding mismatches from staleness (AC13/AC14).
        return False, [note], eligibility_reason, provenance

    readiness_artifact, readiness_reason, readiness_digest = _load_and_verify_readiness_artifact(
        repo_root, hook_received_at=hook_received_at,
    )
    provenance["readiness_artifact_digest"] = readiness_digest
    provenance["readiness_verification_reason_code"] = readiness_reason
    if readiness_artifact is None:
        note = "source-bound readiness artifact is missing" if readiness_reason == "readiness_missing" \
            else ("source-bound readiness artifact is not prepared" if readiness_reason == "readiness_unprepared"
                  else f"source-bound readiness artifact rejected: {readiness_reason}")
        return False, [note], readiness_reason, provenance

    return True, [], "ok", provenance


def _resolve_capture_dir() -> Path:
    candidate = Path(os.environ.get("SCOPE_ROLLUP_CAPTURE_DIR", str(DEFAULT_CAPTURE_DIR)))
    return candidate.resolve()


def _validate_capture_path(path: Path, capture_dir: Path) -> bool:
    if not path.is_absolute():
        return False
    if any(part == ".." for part in path.parts):
        return False
    try:
        resolved_parent = path.parent.resolve()
    except OSError:
        return False
    if resolved_parent != capture_dir:
        return False
    try:
        if path.exists() and path.is_symlink():
            return False
        if path.parent.is_symlink():
            return False
    except OSError:
        return False
    return True


def _atomic_write(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)


def _write_record(path: Path, record: dict[str, Any]) -> None:
    rendered = yaml.safe_dump(
        {"SCOPE_ROLLUP_CAPTURE_RESULT_V1": record},
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")
    _atomic_write(path, rendered)


def _build_record(decision: CaptureDecision) -> dict[str, Any]:
    return {
        "capture_mode": decision.capture_mode,
        "capture_status": decision.capture_status,
        "parser_status": decision.parser_status,
        "capture_routing_action": decision.capture_routing_action,
        "routing_action": decision.capture_routing_action,
        "agent_type": decision.agent_type,
        "invocation_id": decision.invocation_id,
        "requested_at": decision.requested_at,
        "generated_at": decision.generated_at,
        "capture_path": decision.capture_path,
        "capture_sha256": decision.capture_sha256,
        "capture_source": decision.capture_source,
        "provenance": decision.provenance,
        "notes": decision.notes,
    }


def _canonical_stem(decision: CaptureDecision, payload: dict[str, Any]) -> str:
    if decision.invocation_id:
        return f"scope_rollup_{_safe_invocation_id(decision.invocation_id)}"

    digest = _payload_digest(payload)[:12]
    mode = re.sub(r"[^a-z0-9_-]+", "-", decision.capture_mode.lower()).strip("-") or "unknown"
    status = re.sub(r"[^a-z0-9_-]+", "-", decision.capture_status.lower()).strip("-") or "unknown"
    return f"scope_rollup_{mode}_{status}_{digest}"


def _record_stem(decision: CaptureDecision, payload: dict[str, Any]) -> str:
    base = _canonical_stem(decision, payload)
    if decision.capture_status == "captured":
        return base

    digest = _payload_digest(payload)[:12]
    status = re.sub(r"[^a-z0-9_-]+", "-", decision.capture_status.lower()).strip("-") or "unknown"
    return f"{base}.{status}.{digest}"


def _decision_from_payload(payload: dict[str, Any]) -> CaptureDecision:
    hook_event_name = payload.get("hook_event_name")
    agent_type = payload.get("agent_type")
    transcript_path = payload.get("agent_transcript_path")
    last_assistant_message = payload.get("last_assistant_message")
    provenance = {
        "hook_event_name": hook_event_name,
        "agent_transcript_path": transcript_path,
        "eligibility_artifact_digest": None,
        "eligibility_verification_reason_code": None,
        "readiness_artifact_digest": None,
        "readiness_verification_reason_code": None,
    }

    if hook_event_name != "SubagentStop":
        return CaptureDecision(
            capture_mode="unsupported",
            capture_status="hook_unavailable",
            parser_status="not_applicable",
            capture_routing_action="stop_human",
            agent_type=agent_type if isinstance(agent_type, str) else None,
            invocation_id=None,
            requested_at=None,
            generated_at=None,
            capture_path=None,
            capture_sha256=None,
            capture_source="last_assistant_message",
            provenance=provenance,
            notes=["hook_event_name is not SubagentStop"],
        )

    if agent_type != TARGET_AGENT_TYPE:
        return CaptureDecision(
            capture_mode="subagent_stop_hook",
            capture_status="agent_type_mismatch",
            parser_status="not_applicable",
            capture_routing_action="stop_human",
            agent_type=agent_type if isinstance(agent_type, str) else None,
            invocation_id=None,
            requested_at=None,
            generated_at=None,
            capture_path=None,
            capture_sha256=None,
            capture_source="last_assistant_message",
            provenance=provenance,
            notes=["agent_type does not match scope-rollup-runner"],
        )

    if not isinstance(last_assistant_message, str) or not last_assistant_message.strip():
        return CaptureDecision(
            capture_mode="subagent_stop_hook",
            capture_status="missing_final_response",
            parser_status="marker_missing",
            capture_routing_action="stop_human",
            agent_type=TARGET_AGENT_TYPE,
            invocation_id=None,
            requested_at=None,
            generated_at=None,
            capture_path=None,
            capture_sha256=None,
            capture_source="last_assistant_message",
            provenance=provenance,
            notes=["last_assistant_message is empty"],
        )

    parser_status, marker_payload = _extract_marker_payload(last_assistant_message)
    invocation_id = _extract_invocation_id(last_assistant_message, marker_payload)
    requested_at = None
    generated_at = None
    if marker_payload is not None:
        requested_at = _normalize_text(marker_payload.get("requested_at"))
        generated_at = _normalize_text(marker_payload.get("generated_at"))

    if parser_status != "ok":
        return CaptureDecision(
            capture_mode="subagent_stop_hook",
            capture_status="parser_rejected",
            parser_status=parser_status,
            capture_routing_action="stop_human",
            agent_type=TARGET_AGENT_TYPE,
            invocation_id=invocation_id,
            requested_at=requested_at,
            generated_at=generated_at,
            capture_path=None,
            capture_sha256=None,
            capture_source="last_assistant_message",
            provenance=provenance,
            notes=["final response marker is not uniquely parseable"],
        )

    marker_status = marker_payload.get("status")
    if marker_status not in STRICT_STATUS:
        return CaptureDecision(
            capture_mode="subagent_stop_hook",
            capture_status="parser_rejected",
            parser_status="marker_malformed",
            capture_routing_action="stop_human",
            agent_type=TARGET_AGENT_TYPE,
            invocation_id=invocation_id,
            requested_at=requested_at,
            generated_at=generated_at,
            capture_path=None,
            capture_sha256=None,
            capture_source="last_assistant_message",
            provenance=provenance,
            notes=["status field is not allowed"],
        )

    if invocation_id is None:
        return CaptureDecision(
            capture_mode="subagent_stop_hook",
            capture_status="parser_rejected",
            parser_status="marker_malformed",
            capture_routing_action="stop_human",
            agent_type=TARGET_AGENT_TYPE,
            invocation_id=None,
            requested_at=requested_at,
            generated_at=generated_at,
            capture_path=None,
            capture_sha256=None,
            capture_source="last_assistant_message",
            provenance=provenance,
            notes=["invocation_id is missing or ambiguous"],
        )

    try:
        requested_dt = _parse_iso8601(requested_at or "")
        generated_dt = _parse_iso8601(generated_at or "")
    except Exception:
        return CaptureDecision(
            capture_mode="subagent_stop_hook",
            capture_status="parser_rejected",
            parser_status="marker_malformed",
            capture_routing_action="stop_human",
            agent_type=TARGET_AGENT_TYPE,
            invocation_id=invocation_id,
            requested_at=requested_at,
            generated_at=generated_at,
            capture_path=None,
            capture_sha256=None,
            capture_source="last_assistant_message",
            provenance=provenance,
            notes=["requested_at/generated_at could not be parsed"],
        )

    # Issue #1527 Scope Delta (2) AC16: the source-bound eligibility gate is
    # enforced ONLY for callers that explicitly opt in (the Codex adapter,
    # via its own trusted process spawn env — never a hook-payload value).
    # The Claude session_manifest_coordinator.sh raw-payload path never sets
    # this and therefore keeps capturing exactly as it did before #1527.
    if os.environ.get(REQUIRE_SOURCE_BOUND_ENV) == "1":
        repo_root = Path(os.environ.get("SCOPE_ROLLUP_REPO_ROOT", str(Path(__file__).resolve().parents[2])))
        source_bound_ok, source_bound_notes, source_bound_status, source_bound_provenance = (
            _evaluate_source_bound_artifacts(repo_root, marker_payload, datetime.now(timezone.utc))
        )
        provenance.update(source_bound_provenance)
        if not source_bound_ok:
            return CaptureDecision(
                capture_mode="subagent_stop_hook",
                capture_status="parser_rejected",
                parser_status=source_bound_status,
                capture_routing_action="stop_human",
                agent_type=TARGET_AGENT_TYPE,
                invocation_id=invocation_id,
                requested_at=requested_at,
                generated_at=generated_at,
                capture_path=None,
                capture_sha256=None,
                capture_source="last_assistant_message",
                provenance=provenance,
                notes=source_bound_notes,
            )

    if generated_dt <= requested_dt:
        return CaptureDecision(
            capture_mode="subagent_stop_hook",
            capture_status="stale_capture",
            parser_status="rejected",
            capture_routing_action="stop_human",
            agent_type=TARGET_AGENT_TYPE,
            invocation_id=invocation_id,
            requested_at=requested_at,
            generated_at=generated_at,
            capture_path=None,
            capture_sha256=None,
            capture_source="last_assistant_message",
            provenance=provenance,
            notes=["generated_at must be later than requested_at"],
        )

    capture_dir = _resolve_capture_dir()
    safe_invocation_id = _safe_invocation_id(invocation_id)
    capture_path = capture_dir / f"scope_rollup_{safe_invocation_id}.txt"
    if not _validate_capture_path(capture_path, capture_dir):
        return CaptureDecision(
            capture_mode="subagent_stop_hook",
            capture_status="write_failed",
            parser_status="rejected",
            capture_routing_action="stop_human",
            agent_type=TARGET_AGENT_TYPE,
            invocation_id=invocation_id,
            requested_at=requested_at,
            generated_at=generated_at,
            capture_path=str(capture_path),
            capture_sha256=None,
            capture_source="last_assistant_message",
            provenance=provenance,
            notes=["capture path is outside the allowed temp directory"],
        )

    content = last_assistant_message.encode("utf-8")
    capture_sha256 = _sha256_bytes(content)
    return CaptureDecision(
        capture_mode="subagent_stop_hook",
        capture_status="captured",
        parser_status=str(marker_status),
        capture_routing_action="continue" if marker_status == "ok" else "stop_human",
        agent_type=TARGET_AGENT_TYPE,
        invocation_id=invocation_id,
        requested_at=requested_at,
        generated_at=generated_at,
        capture_path=str(capture_path),
        capture_sha256=capture_sha256,
        capture_source="last_assistant_message",
        provenance=provenance,
        notes=[],
    )


def _capture(decision: CaptureDecision, last_assistant_message: str | None) -> CaptureDecision:
    if decision.capture_status != "captured" or decision.capture_path is None:
        return decision

    capture_path = Path(decision.capture_path)
    if capture_path.exists():
        decision.capture_status = "duplicate_invocation"
        decision.capture_routing_action = "stop_human"
        decision.notes.append("capture file already exists")
        return decision

    try:
        _atomic_write(capture_path, (last_assistant_message or "").encode("utf-8"))
    except FileExistsError:
        decision.capture_status = "duplicate_invocation"
        decision.capture_routing_action = "stop_human"
        decision.notes.append("capture file already exists")
        return decision
    except OSError as exc:
        decision.capture_status = "write_failed"
        decision.capture_routing_action = "stop_human"
        decision.notes.append(f"capture write failed: {exc.__class__.__name__}")
        return decision

    mode = stat.S_IMODE(capture_path.stat().st_mode)
    if mode != 0o600:
        decision.capture_status = "write_failed"
        decision.capture_routing_action = "stop_human"
        decision.notes.append(f"capture mode mismatch: {oct(mode)}")
        return decision

    return decision


def main() -> int:
    payload = _read_payload()
    if payload is None:
        return 0

    last_assistant_message = payload.get("last_assistant_message")
    if not isinstance(last_assistant_message, str):
        last_assistant_message = None

    decision = _decision_from_payload(payload)
    decision = _capture(decision, last_assistant_message)

    capture_dir = _resolve_capture_dir()
    record_path = capture_dir / f"{_record_stem(decision, payload)}.capture.yaml"
    if not _validate_capture_path(record_path, capture_dir):
        return 0
    if decision.capture_status == "captured" and record_path.exists():
        return 0

    try:
        _write_record(record_path, _build_record(decision))
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
