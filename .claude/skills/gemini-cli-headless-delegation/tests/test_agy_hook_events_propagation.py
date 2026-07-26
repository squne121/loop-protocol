"""Hermetic regression tests for Issue #1752.

Covers AC1-AC4 and AC6: `_normalize_agy_result()` (run_gemini_headless.py) must
copy `completed.agy_provenance_hook_events` / `completed.agy_provenance_hook_load_error`
(dynamic attributes attached by `_run_agy()` *before* the isolated workspace / temp
cwd it read them from is deleted -- see `_run_agy()` docstring) through to every
`delegation_result/v1` return branch, so `run_delegation()` callers can rebuild a
hook-events bundle without re-reading the already-deleted workspace.

No live `agy` binary or network access is required: `_run_agy` is never called here,
only `_normalize_agy_result()` directly with hand-built `subprocess.CompletedProcess`
objects (mirroring the pattern in `test_agy_provider.py`).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import types
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Module loading helper (hermetic, no side-effects) -- mirrors test_agy_provider.py
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_gemini_headless", _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rgh = _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed(returncode: int, stdout: str = "", stderr: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=["agy", "-p", "test"], returncode=returncode, stdout=stdout, stderr=stderr)


def _sample_hook_event(tool_name: str = "search_web", args_sha256: str | None = None) -> dict[str, Any]:
    """Return a realistic `agy_tool_provenance_v1` event.

    Matches the field set the real hook wrapper (`agy_tool_provenance.py`
    `generate_workspace_hook_config()` / `_HOOK_WRAPPER_TEMPLATE`) emits: only a
    canonicalized-args *hash* is ever stored, never raw `toolCall.args` content.
    """
    return {
        "schema": "agy_tool_provenance_v1",
        "version": 1,
        "event": "PreToolUse",
        "toolCall": {
            "name": tool_name,
            "args_sha256": args_sha256 or hashlib.sha256(b'{"query":"test"}').hexdigest(),
        },
        "stepIdx": 0,
        "conversationId": "conv-1752-test",
        "transcript_path_ref": "sha256:" + hashlib.sha256(b"/redacted/transcript.jsonl").hexdigest(),
        "transcript_sha256": hashlib.sha256(b"transcript-body").hexdigest(),
        "parent_run_id": "run-1752",
        "subtask_id": "subtask-1752",
        "attempt_id": "attempt-1",
        "provider": "agy",
        "tool_profile": "grounded_research",
        "monotonic_ns": 123456789,
        "utc": "2026-07-25T00:00:00.000000Z",
    }


# ---------------------------------------------------------------------------
# AC1: exit_code=0, stdout non-empty (success branch)
# ---------------------------------------------------------------------------


def test_normalize_agy_result_success_propagates_hook_events() -> None:
    """AC1: success branch (exit_code=0, non-empty stdout) includes
    `agy_provenance_hook_events` verbatim from `completed.agy_provenance_hook_events`."""
    completed = _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")
    events = [_sample_hook_event()]
    completed.agy_provenance_hook_events = events  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]

    result = rgh._normalize_agy_result(
        completed,
        tool_profile="no_tools",
        requested_model=None,
    )

    assert result["ok"] is True
    assert result["schema"] == "delegation_result/v1"
    assert result["agy_provenance_hook_events"] == events
    assert result["agy_provenance_hook_load_error"] is None


# ---------------------------------------------------------------------------
# AC2: exit_code != 0 (failure branch) -- fail-closed diagnostics must not
# discard hook provenance.
# ---------------------------------------------------------------------------


def test_normalize_agy_result_nonzero_exit_propagates_hook_events() -> None:
    """AC2: exit_code != 0 branch also includes hook events / load error keys."""
    completed = _make_completed(1, stdout="", stderr="agy: some fatal error")
    events = [_sample_hook_event(tool_name="read_url_content")]
    completed.agy_provenance_hook_events = events  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]

    result = rgh._normalize_agy_result(
        completed,
        tool_profile="grounded_research",
        requested_model=None,
    )

    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert result["agy_provenance_hook_events"] == events
    assert result["agy_provenance_hook_load_error"] is None


def test_normalize_agy_result_nonzero_exit_propagates_hook_load_error() -> None:
    """AC2 (load-error variant): a fail-closed hook load error string is also
    propagated (not silently dropped) on the exit_code != 0 branch."""
    completed = _make_completed(2, stdout="", stderr="boom")
    completed.agy_provenance_hook_events = []  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = (  # type: ignore[attr-defined]
        "hook_event_log_parse_failed: invalid JSON on line 3"
    )

    result = rgh._normalize_agy_result(
        completed,
        tool_profile="no_tools",
        requested_model=None,
    )

    assert result["agy_provenance_hook_events"] == []
    assert result["agy_provenance_hook_load_error"] == "hook_event_log_parse_failed: invalid JSON on line 3"


# ---------------------------------------------------------------------------
# AC3: exit_code=0, stdout empty branch
# ---------------------------------------------------------------------------


def test_normalize_agy_result_empty_stdout_propagates_hook_events() -> None:
    """AC3: exit_code=0 but empty stdout branch also includes hook events keys."""
    completed = _make_completed(0, stdout="   ", stderr="")
    events = [_sample_hook_event()]
    completed.agy_provenance_hook_events = events  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]

    result = rgh._normalize_agy_result(
        completed,
        tool_profile="no_tools",
        requested_model=None,
    )

    assert result["ok"] is False
    assert result["agy_provenance_hook_events"] == events
    assert result["agy_provenance_hook_load_error"] is None


# ---------------------------------------------------------------------------
# AC4: `completed` lacks the dynamic attributes entirely (direct/mocked callers
# that never went through `_run_agy()`) -- must default to `[]` / `None`, never
# raise.
# ---------------------------------------------------------------------------


def test_normalize_agy_result_missing_attribute_defaults_to_empty_list() -> None:
    """AC4: no exception, and `agy_provenance_hook_events` defaults to `[]` when
    `completed` never had the attribute attached (back-compat with existing
    direct/mocked `CompletedProcess` callers, e.g. `test_agy_provider.py`)."""
    completed = _make_completed(0, stdout="plain response, no hook wiring at all")
    assert not hasattr(completed, "agy_provenance_hook_events")
    assert not hasattr(completed, "agy_provenance_hook_load_error")

    result = rgh._normalize_agy_result(
        completed,
        tool_profile="no_tools",
        requested_model=None,
    )

    assert result["agy_provenance_hook_events"] == []
    assert result["agy_provenance_hook_load_error"] is None

    # Also exercise the exit_code != 0 and empty-stdout branches with the same
    # missing-attribute precondition, since all three branches must be safe.
    completed_fail = _make_completed(1, stdout="", stderr="boom")
    assert not hasattr(completed_fail, "agy_provenance_hook_events")
    result_fail = rgh._normalize_agy_result(
        completed_fail,
        tool_profile="no_tools",
        requested_model=None,
    )
    assert result_fail["agy_provenance_hook_events"] == []
    assert result_fail["agy_provenance_hook_load_error"] is None

    completed_empty = _make_completed(0, stdout="   ")
    assert not hasattr(completed_empty, "agy_provenance_hook_events")
    result_empty = rgh._normalize_agy_result(
        completed_empty,
        tool_profile="no_tools",
        requested_model=None,
    )
    assert result_empty["agy_provenance_hook_events"] == []
    assert result_empty["agy_provenance_hook_load_error"] is None


# ---------------------------------------------------------------------------
# AC6: redaction regression -- hook events must never carry raw credential-like
# strings through `_normalize_agy_result()`.
# ---------------------------------------------------------------------------


def test_hook_events_redaction_no_credential_leak() -> None:
    """AC6: a credential-like value that is only ever present *hashed*
    (`toolCall.args_sha256`, matching the real hook wrapper's schema -- see
    `agy_tool_provenance.py` `_HOOK_WRAPPER_TEMPLATE`, which never emits raw
    `toolCall.args`) must not appear anywhere in the `delegation_result/v1`
    dict `_normalize_agy_result()` returns, for any of the three return
    branches. This guards against a future regression that accidentally
    threads raw tool-call args (instead of only the hash) into hook events
    passed through the new `agy_provenance_hook_events` field.
    """
    raw_secret_openai = "sk-liveTESTKEYDONOTUSE1234567890ABCDEF"
    raw_secret_google_oauth = "ya29.TESTFAKEDONOTUSE-abcdefghijklmnopqrstuvwxyz"
    args_sha256 = hashlib.sha256(
        json.dumps(
            {"query": raw_secret_openai, "token": raw_secret_google_oauth},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    event = _sample_hook_event(tool_name="search_web", args_sha256=args_sha256)
    # Sanity: the hashed schema field itself never equals (or contains) the
    # raw secret it was derived from.
    assert raw_secret_openai not in json.dumps(event)
    assert raw_secret_google_oauth not in json.dumps(event)

    for completed in (
        _make_completed(0, stdout="grounded response text"),
        _make_completed(1, stdout="", stderr="agy: fatal"),
        _make_completed(0, stdout="   "),
    ):
        completed.agy_provenance_hook_events = [event]  # type: ignore[attr-defined]
        completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]

        result = rgh._normalize_agy_result(
            completed,
            tool_profile="grounded_research",
            requested_model=None,
        )
        serialized = json.dumps(result, default=str)
        assert raw_secret_openai not in serialized, (
            "raw credential-like value leaked into delegation_result/v1 via "
            "agy_provenance_hook_events pass-through"
        )
        assert raw_secret_google_oauth not in serialized, (
            "raw credential-like value leaked into delegation_result/v1 via "
            "agy_provenance_hook_events pass-through"
        )
        # The events list itself must still be present verbatim (redaction
        # regression guard, not a silent field drop).
        assert result["agy_provenance_hook_events"] == [event]
