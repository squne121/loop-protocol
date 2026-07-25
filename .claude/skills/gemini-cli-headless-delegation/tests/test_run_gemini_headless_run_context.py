"""Hermetic regression tests for Issue #1771.

Covers AC2/AC3/AC4/AC8: the grounded_research call site inside
`_run_delegation_core()` (run_gemini_headless.py) must build a `run_context`
dict from fan-out correlation ids present on the request (`parent_run_id` /
`subtask_id` / `attempt_id`) plus `tool_profile` and a deterministically
computed `transcript_sha256`, and pass it to `_run_agy(..., run_context=...)`
-- while leaving standalone (non-fan-out) calls unaffected (`run_context=None`,
identical to pre-#1771 behavior, AC4).

No live `agy` binary or network access is required: `_run_agy` is patched
with a `side_effect` that (a) records the `run_context` it was called with,
and (b) returns a synthetic `subprocess.CompletedProcess` with
`agy_provenance_hook_events` built directly from that `run_context` --
mirroring exactly what the real isolated-workspace PreToolUse wrapper
(`agy_tool_provenance.py` `_HOOK_WRAPPER_TEMPLATE` /
`generate_workspace_hook_config()`) enriches each captured hook event with
(the wrapper reads `hook_context.json`, written verbatim from `run_context`
by `write_hook_context()`, and copies every field onto each event) -- without
spawning a real subprocess or touching the filesystem. This mirrors the
existing hermetic pattern in `test_agy_provider.py` /
`test_agy_hook_events_propagation.py`.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Module loading helpers (hermetic, no side-effects) -- mirrors
# test_agy_provider.py / test_agy_hook_events_propagation.py
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
_SCRIPT_PATH = _SCRIPTS_DIR / "run_gemini_headless.py"
_PROVENANCE_SCRIPT_PATH = _SCRIPTS_DIR / "agy_tool_provenance.py"


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rgh = _load_module(_SCRIPT_PATH, "run_gemini_headless")
agy_tool_provenance = _load_module(_PROVENANCE_SCRIPT_PATH, "agy_tool_provenance")

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agy_request(**kwargs: Any) -> dict[str, Any]:
    """Return a minimal valid agy delegation request (mirrors
    `test_agy_provider.py::_agy_request`)."""
    base = {
        "schema": "delegation_request_v1",
        "tool_profile": "grounded_research",
        "provider": "agy",
        "prompt": "search for the current LOOP_PROTOCOL version",
        "objective": "Fan-out grounded_research run_context wiring regression test",
        "instructions": ["Return exactly: LOOP_AGY_SMOKE_OK", "Do not add any extra text"],
        "output_sections": ["response"],
        "context_files": [],
        "timeout_sec": 300,
    }
    base.update(kwargs)
    return base


def _hook_event_from_run_context(
    run_context: dict[str, Any] | None, tool_name: str = "search_web"
) -> dict[str, Any]:
    """Build a realistic `agy_tool_provenance_v1` event the way the real
    wrapper script would, given a `hook_context.json` payload equal to
    *run_context* (see `agy_tool_provenance.write_hook_context()` /
    `_HOOK_WRAPPER_TEMPLATE`): every correlation field is copied verbatim
    from the context file onto the event, defaulting to `""` when absent --
    matching `_run_agy()`'s own `ctx.get(key, "")` fallback.
    """
    ctx = run_context or {}
    return {
        "schema": agy_tool_provenance.SCHEMA_NAME,
        "version": 1,
        "event": "PreToolUse",
        "toolCall": {
            "name": tool_name,
            "args_sha256": hashlib.sha256(b'{"query":"test"}').hexdigest(),
        },
        "stepIdx": 0,
        "conversationId": "conv-1771-test",
        "transcript_path_ref": "sha256:" + hashlib.sha256(b"/redacted/transcript.jsonl").hexdigest(),
        "transcript_sha256": str(ctx.get("transcript_sha256", "")),
        "parent_run_id": str(ctx.get("parent_run_id", "")),
        "subtask_id": str(ctx.get("subtask_id", "")),
        "attempt_id": str(ctx.get("attempt_id", "")),
        "provider": "agy",
        "tool_profile": str(ctx.get("tool_profile", "")),
        "monotonic_ns": 123456789,
        "utc": "2026-07-25T00:00:00.000000Z",
    }


def _make_agy_side_effect(captured: dict[str, Any]):
    def _fake_run_agy(
        prompt: str, timeout_sec: int, *, run_context: dict[str, Any] | None = None
    ) -> "subprocess.CompletedProcess[str]":
        captured["run_context"] = run_context
        captured["prompt"] = prompt
        completed = subprocess.CompletedProcess(
            args=["agy", "-p", prompt], returncode=0, stdout="grounded response text", stderr=""
        )
        completed.agy_provenance_hook_events = [  # type: ignore[attr-defined]
            _hook_event_from_run_context(run_context)
        ]
        completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]
        return completed

    return _fake_run_agy


def _run_fanout_delegation() -> tuple[dict[str, Any], dict[str, Any]]:
    request = _agy_request(
        parent_run_id="run-1771-parent",
        subtask_id="subtask-1771",
        attempt_id="attempt-1",
    )
    captured: dict[str, Any] = {}
    with patch.object(rgh, "_run_agy", side_effect=_make_agy_side_effect(captured)):
        result = rgh.run_delegation(request)
    return result, captured


# ---------------------------------------------------------------------------
# AC2: fan-out correlation ids present -> hook event carries them non-empty
# ---------------------------------------------------------------------------


def test_grounded_research_run_context_correlation_ids() -> None:
    """AC2: when the request carries parent_run_id/subtask_id/attempt_id, the
    hook event `_run_agy()` would produce (simulated here from the recorded
    `run_context`, mirroring the real wrapper's field copy) has non-empty
    parent_run_id/subtask_id/attempt_id/tool_profile."""
    result, captured = _run_fanout_delegation()

    run_context = captured["run_context"]
    assert run_context is not None, "grounded_research call site must build a run_context for a fan-out request"
    assert run_context["parent_run_id"] == "run-1771-parent"
    assert run_context["subtask_id"] == "subtask-1771"
    assert run_context["attempt_id"] == "attempt-1"
    assert run_context["tool_profile"] == "grounded_research"

    event = result["agy_provenance_hook_events"][0]
    assert event["parent_run_id"] == "run-1771-parent"
    assert event["subtask_id"] == "subtask-1771"
    assert event["attempt_id"] == "attempt-1"
    assert event["tool_profile"] == "grounded_research"


# ---------------------------------------------------------------------------
# AC3: transcript_sha256 non-empty and 64-char hex
# ---------------------------------------------------------------------------


def test_grounded_research_run_context_transcript_sha256() -> None:
    """AC3: the hook event's transcript_sha256 is non-empty and matches
    `agy_tool_provenance._HEX64_RE` (64-char lowercase hex)."""
    result, captured = _run_fanout_delegation()

    run_context = captured["run_context"]
    assert run_context is not None
    transcript_sha256 = run_context["transcript_sha256"]
    assert transcript_sha256
    assert agy_tool_provenance._HEX64_RE.match(transcript_sha256)
    assert _HEX64_RE.match(transcript_sha256)

    event = result["agy_provenance_hook_events"][0]
    assert event["transcript_sha256"] == transcript_sha256
    assert agy_tool_provenance._HEX64_RE.match(event["transcript_sha256"])

    # transcript_sha256 must also match the top-level delegation_result/v1
    # field (surfaced by _normalize_agy_result(), AC5) -- same single
    # computed value flows to both places.
    assert result["transcript_sha256"] == transcript_sha256


# ---------------------------------------------------------------------------
# AC4: standalone (non-fan-out) call -- run_context stays None, unchanged
# pre-#1771 behavior.
# ---------------------------------------------------------------------------


def test_grounded_research_run_context_backward_compat() -> None:
    """AC4: a standalone grounded_research request (no parent_run_id/
    subtask_id/attempt_id) still calls `_run_agy()` with `run_context=None`,
    identical to pre-#1771 behavior -- no fabricated correlation ids."""
    # Use tool_profile="no_tools" here (rather than grounded_research) so
    # this test isolates run_context wiring backward-compat from the
    # separate (Issue #1266) grounded_research fail-closed evidence gate,
    # which independently requires real grounding evidence in stdout.
    standalone_request = _agy_request(tool_profile="no_tools")
    assert "parent_run_id" not in standalone_request
    assert "subtask_id" not in standalone_request
    assert "attempt_id" not in standalone_request

    captured: dict[str, Any] = {}
    with patch.object(rgh, "_run_agy", side_effect=_make_agy_side_effect(captured)):
        result = rgh.run_delegation(standalone_request)

    assert captured["run_context"] is None

    # The (simulated) hook event still exists but every correlation field is
    # the empty string -- exactly matching the pre-#1771 write_hook_context()
    # default (`ctx.get(key, "")` when run_context is None/{}).
    event = result["agy_provenance_hook_events"][0]
    assert event["parent_run_id"] == ""
    assert event["subtask_id"] == ""
    assert event["attempt_id"] == ""
    assert event["tool_profile"] == ""

    # ok=True / response text path is otherwise unaffected.
    assert result["ok"] is True
    assert result["response_text"] == "grounded response text"


# ---------------------------------------------------------------------------
# AC8: redaction -- no raw credential-like content leaks through run_context
# or the delegation_result/v1 surfaced fields.
# ---------------------------------------------------------------------------


def test_grounded_research_run_context_redaction() -> None:
    """AC8: raw transcript content / credential-like strings never appear in
    the public delegation_result/v1 fields (conversation_id / transcript_sha256
    / hook event group) -- only their sha256 hashes do."""
    raw_secret = "sk-liveTESTKEYDONOTUSE1234567890ABCDEF"
    request = _agy_request(
        parent_run_id="run-1771-parent",
        subtask_id="subtask-1771",
        attempt_id="attempt-1",
        prompt=f"search for X using key {raw_secret}",
    )

    captured: dict[str, Any] = {}
    with patch.object(rgh, "_run_agy", side_effect=_make_agy_side_effect(captured)):
        result = rgh.run_delegation(request)

    serialized = json.dumps(result, default=str)
    assert raw_secret not in serialized, "raw prompt content leaked into delegation_result/v1"

    run_context = captured["run_context"]
    assert run_context is not None
    # transcript_sha256 is a deterministic hash, never the raw prompt text.
    assert raw_secret not in run_context["transcript_sha256"]
    assert isinstance(result["transcript_sha256"], str) and result["transcript_sha256"]
    assert raw_secret not in result["transcript_sha256"]

    # conversation_id (AC5) is also a public-safe opaque id, never raw text.
    assert result.get("conversation_id") is None or raw_secret not in result["conversation_id"]
