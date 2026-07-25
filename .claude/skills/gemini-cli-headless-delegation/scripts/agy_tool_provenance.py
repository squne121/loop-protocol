#!/usr/bin/env python3
"""AGY PreToolUse hook provenance: schema, validator, and workspace-scoped hook wiring.

Issue #1708 (parent research: #1494, Blocker 1): the AGY fan-out WebSearch/read_url_content
"success" judgment must NOT rely on AGY's own stdout self-report (structured `tool_calls`
JSON, or `AGY_WEBSEARCH:` / `AGY_GROUNDED_RESEARCH:` marker lines). A model can fabricate
that JSON without ever invoking a tool. This module makes the AGY `PreToolUse` lifecycle
hook (see `hooks.md` bundled with the installed Antigravity CLI, and
`references/runtime-portability.md`) the source of truth instead:

- Each AGY subprocess run gets an *isolated temporary workspace* (see
  ``run_gemini_headless._run_agy``). This module generates a workspace-scoped
  ``.agents/hooks.json`` inside that temp dir only -- the user's global Antigravity
  settings/hooks file is never read or written.
- The generated hook's `command` is a wrapper script that receives the AGY-native
  `PreToolUse` stdin payload (`toolCall.name`, `toolCall.args`, `stepIdx`,
  `conversationId`, `transcriptPath`, ...) and appends one `agy_tool_provenance_v1`
  JSON line to a run-scoped event log, enriched with orchestrator-known run-binding
  fields (`parent_run_id`, `subtask_id`, `attempt_id`, `provider`, `tool_profile`)
  and integrity fields (`args_sha256`, `transcript_sha256`, `monotonic_ns`, `utc`).
  It always answers ``{"decision": "allow"}`` on stdout so the underlying tool call
  is not blocked -- this hook is provenance-only, not a permission gate (permission
  gating is out of scope; see Issue #1705).
- ``evaluate_websearch_provenance()`` is the authoritative success judge: it ignores
  stdout self-report content entirely for the `grounding_backend` /
  `web_tool_call_count` decision and requires a *validated*, *run-matched*
  ``agy_tool_provenance_v1`` hook event whose ``toolCall.name`` is one of the
  canonical web tool names (``search_web``, ``read_url_content``).

Canonical web tool names were confirmed by reading the installed Antigravity CLI's
bundled hook documentation (``builtin/skills/agy-customizations/docs/hooks.md``,
Antigravity CLI 1.1.5 as reported by ``agy --version``) together with live
``PreToolUse`` transcript samples recorded under
``~/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl``, which
show ``toolCall.name == "search_web"`` for AGY-native web search tool calls. See
AC1 / ``references/usage-contract.md`` for the full readback record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_NAME = "agy_tool_provenance_v1"
SCHEMA_VERSION = 1

# Canonical AGY web tool names (Issue #1708 AC1/AC2). Any other name -- including the
# names a prior implementation (#1266) mistakenly treated as recognized web tools
# (`web_search`, `websearch`, `browser_navigate`, `browser`, `url_read`, `read_url`,
# `fetch_url`, `fetch`) -- is NOT a canonical AGY tool name and must fail closed.
CANONICAL_WEB_TOOL_NAMES: frozenset[str] = frozenset({"search_web", "read_url_content"})

# Historical/incorrect names a prior implementation recognized. Kept only so the
# fail-closed validator can produce a specific, actionable failure_class instead of a
# generic "unknown tool" message when one of these legacy aliases is seen.
_LEGACY_UNRECOGNIZED_ALIASES: frozenset[str] = frozenset(
    {
        "web_search",
        "websearch",
        "browser_navigate",
        "browser",
        "url_read",
        "read_url",
        "fetch_url",
        "fetch",
    }
)

REQUIRED_TOP_FIELDS: tuple[str, ...] = (
    "schema",
    "version",
    "event",
    "toolCall",
    "stepIdx",
    "conversationId",
    "transcript_path_ref",
    "transcript_sha256",
    "parent_run_id",
    "subtask_id",
    "attempt_id",
    "provider",
    "tool_profile",
    "monotonic_ns",
    "utc",
)
REQUIRED_TOOL_CALL_FIELDS: tuple[str, ...] = ("name", "args_sha256")

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)$")

_CREDENTIAL_REGEX = re.compile(
    r"(AIza[0-9A-Za-z_\-]{35})"
    r"|(sk-[A-Za-z0-9]{16,})"
    r"|(ghp_[A-Za-z0-9]{20,})"
    r"|(xox[baprs]-[A-Za-z0-9\-]{10,})"
    r"|(-----BEGIN [A-Z ]*PRIVATE KEY-----)",
)

_REDACTION_PLACEHOLDER = "<redacted>"


class ProvenanceWorkspaceHookError(RuntimeError):
    """Raised when workspace-scoped hook config generation fails (fail-closed)."""


class ProvenanceParseError(RuntimeError):
    """Raised when an `agy_tool_provenance_v1` event log cannot be parsed (fail-closed)."""


# ---------------------------------------------------------------------------
# Canonicalization / hashing
# ---------------------------------------------------------------------------


def canonicalize_args_sha256(args: Any) -> str:
    """Return sha256 hex digest of *args* canonicalized as sorted, compact JSON.

    Used for ``toolCall.args_sha256`` -- we never emit raw tool-call args (which may
    contain URLs with query-string secrets, file contents, etc.) into the provenance
    event; only their hash.
    """
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def monotonic_ns() -> int:
    return time.monotonic_ns()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# Redaction (Issue #1708 AC6)
# ---------------------------------------------------------------------------


def scan_event_for_leaks(event: dict[str, Any], *, home: str | None = None, repo_root: str | None = None) -> list[str]:
    """Scan a (nested) provenance event for raw credential / HOME / repo path leaks.

    Returns a list of violation codes; empty list means clean. This is an actual
    runtime scan of stringified event content -- it does not rely on a self-reported
    "already redacted" flag.
    """
    violations: list[str] = []
    blob = json.dumps(event, sort_keys=True, default=str)
    if _CREDENTIAL_REGEX.search(blob):
        violations.append("raw_credential_detected")
    home = home if home is not None else os.environ.get("HOME")
    if home and home in blob:
        violations.append("home_absolute_path_detected")
    if repo_root and repo_root in blob:
        violations.append("repo_absolute_path_detected")
    # Raw transcript content is never allowed inline -- only a path reference/hash.
    if "transcript_path" in event or "transcriptPath" in event:
        violations.append("raw_transcript_path_field_present")
    return violations


def redact_text(text: str, *, home: str | None = None, repo_root: str | None = None) -> str:
    redacted = _CREDENTIAL_REGEX.sub(_REDACTION_PLACEHOLDER, text or "")
    home = home if home is not None else os.environ.get("HOME")
    if home:
        redacted = redacted.replace(home, "$HOME")
    if repo_root:
        redacted = redacted.replace(repo_root, "<repo_root>")
    return redacted


def transcript_path_ref(transcript_path: str, *, home: str | None = None, repo_root: str | None = None) -> str:
    """Return a public-safe identifier for a transcript path (never the raw absolute path)."""
    return "sha256:" + sha256_text(redact_text(transcript_path, home=home, repo_root=repo_root))


# ---------------------------------------------------------------------------
# Event construction
# ---------------------------------------------------------------------------


def build_provenance_event(
    *,
    event: str,
    tool_name: str,
    tool_args: Any,
    step_idx: int,
    conversation_id: str,
    transcript_path: str,
    transcript_sha256: str,
    parent_run_id: str,
    subtask_id: str,
    attempt_id: str,
    provider: str = "agy",
    tool_profile: str,
    home: str | None = None,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Build a well-formed ``agy_tool_provenance_v1`` event dict (fail-closed inputs).

    Raw ``tool_args`` and the raw ``transcript_path`` are never stored -- only their
    hash / a public-safe reference (Issue #1708 AC6).
    """
    return {
        "schema": SCHEMA_NAME,
        "version": SCHEMA_VERSION,
        "event": event,
        "toolCall": {
            "name": tool_name,
            "args_sha256": canonicalize_args_sha256(tool_args),
        },
        "stepIdx": step_idx,
        "conversationId": conversation_id,
        "transcript_path_ref": transcript_path_ref(transcript_path, home=home, repo_root=repo_root),
        "transcript_sha256": transcript_sha256,
        "parent_run_id": parent_run_id,
        "subtask_id": subtask_id,
        "attempt_id": attempt_id,
        "provider": provider,
        "tool_profile": tool_profile,
        "monotonic_ns": monotonic_ns(),
        "utc": utc_now_iso(),
    }


# ---------------------------------------------------------------------------
# Validation (Issue #1708 AC2 / AC4)
# ---------------------------------------------------------------------------


def validate_provenance_event(event: Any) -> tuple[bool, list[str]]:
    """Fail-closed structural + semantic validation of an `agy_tool_provenance_v1` event.

    Returns ``(ok, violations)``. ``ok`` is True only when the event has every
    required field with a plausible type/shape AND ``toolCall.name`` is a canonical
    web tool name.
    """
    violations: list[str] = []
    if not isinstance(event, dict):
        return False, ["event_not_object"]

    for field in REQUIRED_TOP_FIELDS:
        if field not in event or event[field] in (None, ""):
            violations.append(f"missing_required_field:{field}")

    if event.get("schema") != SCHEMA_NAME:
        violations.append("wrong_schema")
    if event.get("version") != SCHEMA_VERSION:
        violations.append("wrong_version")

    tool_call = event.get("toolCall")
    if not isinstance(tool_call, dict):
        violations.append("missing_required_field:toolCall")
    else:
        for field in REQUIRED_TOOL_CALL_FIELDS:
            if field not in tool_call or tool_call[field] in (None, ""):
                violations.append(f"missing_required_field:toolCall.{field}")
        name = tool_call.get("name")
        if isinstance(name, str):
            normalized = name.strip().lower()
            if normalized not in CANONICAL_WEB_TOOL_NAMES:
                if normalized in _LEGACY_UNRECOGNIZED_ALIASES:
                    violations.append("unknown_tool_provenance:legacy_alias")
                else:
                    violations.append("unknown_tool_provenance")
        args_sha256 = tool_call.get("args_sha256")
        if isinstance(args_sha256, str) and not _HEX64_RE.match(args_sha256):
            violations.append("malformed_args_sha256")

    step_idx = event.get("stepIdx")
    if step_idx is not None and not isinstance(step_idx, int):
        violations.append("malformed_stepIdx")

    transcript_sha256 = event.get("transcript_sha256")
    if isinstance(transcript_sha256, str) and not _HEX64_RE.match(transcript_sha256):
        violations.append("malformed_transcript_sha256")

    monotonic_value = event.get("monotonic_ns")
    if monotonic_value is not None and not isinstance(monotonic_value, int):
        violations.append("malformed_monotonic_ns")

    utc_value = event.get("utc")
    if isinstance(utc_value, str) and not _ISO_UTC_RE.match(utc_value):
        violations.append("malformed_utc")

    return (len(violations) == 0), violations


# ---------------------------------------------------------------------------
# Run-context matching (Issue #1708 AC7)
# ---------------------------------------------------------------------------


def match_run_context(
    event: dict[str, Any],
    *,
    conversation_id: str,
    parent_run_id: str,
    attempt_id: str,
    transcript_sha256: str,
) -> tuple[bool, list[str]]:
    """Compare a validated event's run-binding fields to the expected fan-out run context."""
    mismatches: list[str] = []
    if event.get("conversationId") != conversation_id:
        mismatches.append("conversation_id_mismatch")
    if event.get("parent_run_id") != parent_run_id:
        mismatches.append("parent_run_id_mismatch")
    if event.get("attempt_id") != attempt_id:
        mismatches.append("attempt_id_mismatch")
    if event.get("transcript_sha256") != transcript_sha256:
        mismatches.append("transcript_sha256_mismatch")
    return (len(mismatches) == 0), mismatches


# ---------------------------------------------------------------------------
# Workspace-scoped hook generation (Issue #1708 AC5 / AC9)
# ---------------------------------------------------------------------------

_HOOK_LOG_ENV = "AGY_PROVENANCE_HOOK_LOG_PATH"
_HOOK_CONTEXT_ENV = "AGY_PROVENANCE_HOOK_CONTEXT_PATH"

_HOOK_WRAPPER_TEMPLATE = """#!/usr/bin/env python3
# Auto-generated by agy_tool_provenance.generate_workspace_hook_config(). Do not edit by
# hand -- regenerated per AGY run in an isolated temp workspace. Reads AGY's native
# PreToolUse stdin payload, enriches it with run-binding context read from
# {context_env}, and appends one agy_tool_provenance_v1 JSON line to the log at
# {log_env}. Always answers {{"decision": "allow"}} -- provenance capture only, not a
# permission gate.
import hashlib, json, os, sys, time
from datetime import datetime, timezone

def _sha256(s):
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({{"decision": "allow"}}))
        return 0
    log_path = os.environ.get("{log_env}")
    context_path = os.environ.get("{context_env}")
    if log_path and context_path:
        try:
            with open(context_path, "r", encoding="utf-8") as fh:
                ctx = json.load(fh)
        except Exception:
            ctx = {{}}
        tool_call = payload.get("toolCall") or {{}}
        args = tool_call.get("args")
        canonical_args = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
        transcript_path = payload.get("transcriptPath") or ""
        home = os.environ.get("HOME") or ""
        repo_root = ctx.get("repo_root") or ""
        redacted_transcript_path = transcript_path
        if home:
            redacted_transcript_path = redacted_transcript_path.replace(home, "$HOME")
        if repo_root:
            redacted_transcript_path = redacted_transcript_path.replace(repo_root, "<repo_root>")
        event = {{
            "schema": "agy_tool_provenance_v1",
            "version": 1,
            "event": "PreToolUse",
            "toolCall": {{
                "name": (tool_call.get("name") or "").strip().lower(),
                "args_sha256": _sha256(canonical_args),
            }},
            "stepIdx": payload.get("stepIdx"),
            "conversationId": payload.get("conversationId"),
            "transcript_path_ref": "sha256:" + _sha256(redacted_transcript_path),
            "transcript_sha256": ctx.get("transcript_sha256", ""),
            "parent_run_id": ctx.get("parent_run_id", ""),
            "subtask_id": ctx.get("subtask_id", ""),
            "attempt_id": ctx.get("attempt_id", ""),
            "provider": "agy",
            "tool_profile": ctx.get("tool_profile", ""),
            "monotonic_ns": time.monotonic_ns(),
            "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }}
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, sort_keys=True) + "\\n")
        except Exception:
            pass
    print(json.dumps({{"decision": "allow"}}))
    return 0

if __name__ == "__main__":
    sys.exit(main())
"""


def generate_workspace_hook_config(
    workspace_dir: str | Path,
    *,
    hook_log_path: str | Path,
    hook_context_path: str | Path,
    matcher: str = "search_web|read_url_content",
) -> Path:
    """Write a workspace-scoped ``.agents/hooks.json`` + wrapper script into *workspace_dir*.

    Writes ONLY inside *workspace_dir* -- never touches a user's global Antigravity
    settings/hooks file (Issue #1708 AC5). Raises :class:`ProvenanceWorkspaceHookError`
    on any write failure instead of silently degrading (fail-closed, Issue #1708 AC9).
    """
    workspace_dir = Path(workspace_dir)
    agents_dir = workspace_dir / ".agents"
    try:
        agents_dir.mkdir(parents=True, exist_ok=True)
        wrapper_path = agents_dir / "agy_provenance_hook.py"
        wrapper_source = _HOOK_WRAPPER_TEMPLATE.format(
            log_env=_HOOK_LOG_ENV,
            context_env=_HOOK_CONTEXT_ENV,
        )
        wrapper_path.write_text(wrapper_source, encoding="utf-8")
        wrapper_path.chmod(0o755)

        hooks_config = {
            "agy-tool-provenance": {
                "enabled": True,
                "PreToolUse": [
                    {
                        "matcher": matcher,
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"python3 {wrapper_path}",
                                "timeout": 10,
                            }
                        ],
                    }
                ],
            }
        }
        hooks_json_path = agents_dir / "hooks.json"
        hooks_json_path.write_text(
            json.dumps(hooks_config, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError as exc:
        raise ProvenanceWorkspaceHookError(f"failed to write workspace-scoped hook config: {exc}") from exc

    # hook_log_path / hook_context_path are handed back to the caller so it can set
    # the env vars the wrapper script reads; validate they are writable-looking paths.
    hook_log_path = Path(hook_log_path)
    hook_context_path = Path(hook_context_path)
    try:
        hook_log_path.parent.mkdir(parents=True, exist_ok=True)
        hook_context_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProvenanceWorkspaceHookError(f"failed to prepare hook log/context dirs: {exc}") from exc

    return hooks_json_path


def write_hook_context(
    hook_context_path: str | Path,
    *,
    parent_run_id: str,
    subtask_id: str,
    attempt_id: str,
    tool_profile: str,
    transcript_sha256: str,
    repo_root: str | None = None,
) -> None:
    """Persist run-binding context the wrapper hook script enriches events with.

    Raises :class:`ProvenanceWorkspaceHookError` on write failure (fail-closed).
    """
    context = {
        "parent_run_id": parent_run_id,
        "subtask_id": subtask_id,
        "attempt_id": attempt_id,
        "tool_profile": tool_profile,
        "transcript_sha256": transcript_sha256,
        "repo_root": repo_root or "",
    }
    try:
        Path(hook_context_path).write_text(json.dumps(context), encoding="utf-8")
    except OSError as exc:
        raise ProvenanceWorkspaceHookError(f"failed to write hook context: {exc}") from exc


def hook_env(hook_log_path: str | Path, hook_context_path: str | Path) -> dict[str, str]:
    """Environment variables the wrapper script needs; merge into the AGY subprocess env."""
    return {
        _HOOK_LOG_ENV: str(hook_log_path),
        _HOOK_CONTEXT_ENV: str(hook_context_path),
    }


# ---------------------------------------------------------------------------
# Event log loading (Issue #1708 AC9)
# ---------------------------------------------------------------------------


def load_hook_events(hook_log_path: str | Path) -> list[dict[str, Any]]:
    """Load newline-delimited JSON `agy_tool_provenance_v1` events from *hook_log_path*.

    Missing log file => empty list (no hook fired yet is not a parse error). A file
    that exists but contains malformed JSON on any non-blank line raises
    :class:`ProvenanceParseError` -- callers MUST fail closed (never silently fall back
    to stdout marker parsing) rather than swallow the error (Issue #1708 AC9).
    """
    path = Path(hook_log_path)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProvenanceParseError(f"failed to read hook event log: {exc}") from exc
    for line_no, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            raise ProvenanceParseError(
                f"malformed agy_tool_provenance_v1 event at line {line_no}: {exc}"
            ) from exc
    return events


# ---------------------------------------------------------------------------
# Authoritative WebSearch success judgment (Issue #1708 AC3 / AC7 / AC8)
# ---------------------------------------------------------------------------


def evaluate_websearch_provenance(
    *,
    hook_events: list[dict[str, Any]] | None,
    expected_run_context: dict[str, Any],
    stdout_self_report: dict[str, Any] | None = None,
    hook_load_error: str | None = None,
) -> dict[str, Any]:
    """Authoritative provenance-based WebSearch success judgment.

    ``stdout_self_report`` (AGY's stdout `tool_calls`/marker JSON, if any) is carried
    through purely as a non-authoritative, informational field -- it is NEVER used to
    set ``grounding_backend`` or ``web_tool_call_count`` on its own (Issue #1708 AC3 /
    AC8). Only validated, run-matched, canonical-tool-name hook events count.

    ``hook_load_error`` being non-None (workspace hook write failure or event-log parse
    failure) forces a fail-closed result regardless of any other input (Issue #1708 AC9).
    """
    result: dict[str, Any] = {
        "schema": "agy_websearch_provenance_evaluation/v1",
        "grounding_backend": "none",
        "web_tool_call_count": 0,
        "provenance_status": "no_hook_event",
        "failure_class": "agy_provenance_hook_event_missing",
        "validated_tool_calls": [],
        "violations": [],
        "stdout_self_report": stdout_self_report,
    }

    if hook_load_error:
        result["provenance_status"] = "hook_load_failed"
        result["failure_class"] = "agy_provenance_hook_load_failed"
        result["violations"].append(hook_load_error)
        return result

    events = hook_events or []
    if not events:
        return result

    validated: list[dict[str, Any]] = []
    all_violations: list[str] = []
    for raw_event in events:
        ok, violations = validate_provenance_event(raw_event)
        if not ok:
            all_violations.extend(violations)
            continue
        match_ok, mismatches = match_run_context(
            raw_event,
            conversation_id=expected_run_context.get("conversation_id", ""),
            parent_run_id=expected_run_context.get("parent_run_id", ""),
            attempt_id=expected_run_context.get("attempt_id", ""),
            transcript_sha256=expected_run_context.get("transcript_sha256", ""),
        )
        if not match_ok:
            all_violations.extend(mismatches)
            continue
        validated.append(raw_event)

    result["violations"] = all_violations

    if not validated:
        result["provenance_status"] = "no_valid_matched_hook_event"
        if any(v.startswith("unknown_tool_provenance") for v in all_violations):
            result["failure_class"] = "unknown_tool_provenance"
        elif any(v.endswith("_mismatch") for v in all_violations):
            result["failure_class"] = "run_context_mismatch"
        else:
            result["failure_class"] = "agy_provenance_hook_event_missing"
        return result

    result["validated_tool_calls"] = [event["toolCall"]["name"] for event in validated]
    result["grounding_backend"] = "agy_native_websearch"
    result["web_tool_call_count"] = len(validated)
    result["provenance_status"] = "grounded_by_hook_provenance"
    result["failure_class"] = None
    return result
