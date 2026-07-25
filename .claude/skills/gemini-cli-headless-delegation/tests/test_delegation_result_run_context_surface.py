"""Hermetic regression tests for Issue #1771.

Covers AC5/AC7: `delegation_result/v1` top-level `conversation_id` /
`transcript_sha256` surfacing, and that these two new keys are purely
optional / additive (no existing consumer of `_normalize_agy_result()` --
e.g. `test_agy_provider.py`, `test_delegation_result_fanout_correlation_ids.py`
-- breaks when it does not pass or read them).

Consumer inventory (Issue #1771 AC7, `rg -n "delegation_result/v1"`):
- `_normalize_agy_result()` (run_gemini_headless.py): sole producer of the
  `delegation_result/v1` dict for `provider=agy`. Now additionally populates
  `conversation_id` (derived from the first captured
  `agy_provenance_hook_events` entry that has one, else `None`) and
  `transcript_sha256` (the caller-supplied deterministic prompt-text hash,
  else `None` for callers that never pass the new keyword-only arg).
- `_normalize_acp_result()` (run_gemini_headless.py, ACP/gemini transport):
  a distinct producer for `provider=gemini` ACP results; not touched by this
  Issue -- it never sets/reads `conversation_id` / `transcript_sha256`, and
  omitting them is backward compatible (`dict.get(...)` callers see `None`
  and always did, since the keys were entirely absent pre-#1771 too).
- `run_delegation()` / `_run_delegation_core()`: pass-through callers; no
  change needed since they return whatever `_normalize_agy_result()` /
  `_normalize_acp_result()` produced verbatim.
- `build_fanout_evidence_bundle.py`: reads specific known keys
  (`parent_run_id` / `subtask_id` / `attempt_id` / etc.) off
  `delegation_result/v1` via `.get(...)`; an unknown-to-it additive key is a
  no-op (dict `.get()` on keys it never asks for).
- `validate_agy_fanout_e2e_evidence.py` (`_predicate_hook_provenance()`):
  the new, Issue-#1771-motivated consumer of `result.get("conversation_id")`
  / `result.get("transcript_sha256")` (predicate_07/08/10). Covered by
  `test_agy_fanout_e2e_validator_run_context.py` in this same Issue.

Compatibility decision: both new keys are additive-only dict entries with a
`None` default (never a required/renamed/removed key), so every existing
consumer that does not know about them is unaffected (`dict.get("conversation_id")`
on a pre-#1771 result already returned `None` for a missing key; `dict.get(...)`
on a post-#1771 result with the caller default path also returns `None`).
No existing closed-schema test enumerates an exhaustive key allowlist for
`delegation_result/v1` (verified via `rg -n "delegation_result/v1"` across
`tests/`), so no existing test needed updating for this additive change.

No live `agy` binary or network access is required.
"""
from __future__ import annotations

import re
import subprocess
import importlib.util
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_gemini_headless", _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rgh = _load_module()

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _make_completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=["agy", "-p", "test"], returncode=returncode, stdout=stdout, stderr=stderr)


def _agy_request(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": "delegation_request_v1",
        "tool_profile": "no_tools",
        "provider": "agy",
        "prompt": "Return exactly: LOOP_AGY_SMOKE_OK",
        "objective": "Smoke test for delegation_result/v1 run_context surface fields",
        "instructions": ["Return exactly: LOOP_AGY_SMOKE_OK", "Do not add any extra text"],
        "output_sections": ["response"],
        "context_files": [],
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# AC5: conversation_id / transcript_sha256 present + non-empty on success
# ---------------------------------------------------------------------------


def test_conversation_id_transcript_sha256_top_level() -> None:
    """AC5: delegation_result/v1 top level carries non-empty conversation_id
    / transcript_sha256 for a normal (successful, hook-event-producing) run."""

    def _fake_run_agy(
        prompt: str, timeout_sec: int, *, run_context: dict[str, Any] | None = None
    ) -> "subprocess.CompletedProcess[str]":
        completed = _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")
        completed.agy_provenance_hook_events = [  # type: ignore[attr-defined]
            {
                "schema": "agy_tool_provenance_v1",
                "version": 1,
                "event": "PreToolUse",
                "toolCall": {"name": "search_web", "args_sha256": "a" * 64},
                "stepIdx": 0,
                "conversationId": "conv-ac5-test",
                "transcript_path_ref": "sha256:" + "b" * 64,
                "transcript_sha256": (run_context or {}).get("transcript_sha256", ""),
                "parent_run_id": "",
                "subtask_id": "",
                "attempt_id": "",
                "provider": "agy",
                "tool_profile": "no_tools",
                "monotonic_ns": 1,
                "utc": "2026-07-25T00:00:00.000000Z",
            }
        ]
        completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]
        return completed

    with patch.object(rgh, "_run_agy", side_effect=_fake_run_agy):
        result = rgh.run_delegation(_agy_request())

    assert result["schema"] == "delegation_result/v1"
    assert result["ok"] is True
    assert isinstance(result["conversation_id"], str) and result["conversation_id"] == "conv-ac5-test"
    assert isinstance(result["transcript_sha256"], str) and result["transcript_sha256"]
    assert _HEX64_RE.match(result["transcript_sha256"])


def test_conversation_id_falls_back_to_none_without_hook_events() -> None:
    """conversation_id is never fabricated: when no hook event was captured
    (e.g. AGY made no tool call), it stays None while transcript_sha256 (a
    deterministic pre-call hash, not dependent on hook capture) is still
    populated."""
    with patch.object(rgh, "_run_agy", return_value=_make_completed(0, stdout="LOOP_AGY_SMOKE_OK")):
        result = rgh.run_delegation(_agy_request())

    assert result["conversation_id"] is None
    assert isinstance(result["transcript_sha256"], str) and result["transcript_sha256"]
    assert _HEX64_RE.match(result["transcript_sha256"])


# ---------------------------------------------------------------------------
# AC7: additive / optional -- existing (pre-#1771) call patterns unaffected
# ---------------------------------------------------------------------------


def test_delegation_result_v1_backward_compatible_additive_fields() -> None:
    """AC7: pre-#1771 direct callers of _normalize_agy_result() that never
    pass transcript_sha256= (mirrors test_agy_provider.py /
    test_delegation_result_fanout_correlation_ids.py call sites, which are
    Allowed-Paths-adjacent existing tests this Issue must not need to
    modify) still get a well-formed delegation_result/v1: the two new keys
    are present with a safe None default, and every pre-existing key/value
    this Issue did not touch is completely unchanged."""
    completed = _make_completed(returncode=0, stdout="LOOP_AGY_SMOKE_OK")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)

    # New additive keys, safe default.
    assert result["conversation_id"] is None
    assert result["transcript_sha256"] is None

    # Untouched pre-#1771 key/value set (regression guard).
    assert result["schema"] == "delegation_result/v1"
    assert result["ok"] is True
    assert result["provider"] == "agy"
    assert result["safety_mode"] == "degraded_wrapper_only"
    assert result["response_text"] == "LOOP_AGY_SMOKE_OK"
    assert result["parent_run_id"] is None
    assert result["subtask_id"] is None
    assert result["attempt_id"] is None
    assert result["agy_provenance_hook_events"] == []
    assert result["agy_provenance_hook_load_error"] is None


def test_delegation_result_v1_additive_fields_present_on_all_three_branches() -> None:
    """AC7: conversation_id / transcript_sha256 are present (not KeyError-ing
    consumers) on every _normalize_agy_result() return branch -- nonzero
    exit, empty stdout, and success -- not just the success path."""
    nonzero = rgh._normalize_agy_result(
        _make_completed(returncode=1, stdout="", stderr="boom"),
        tool_profile="no_tools",
        requested_model=None,
        transcript_sha256="c" * 64,
    )
    assert nonzero["conversation_id"] is None
    assert nonzero["transcript_sha256"] == "c" * 64

    empty_stdout = rgh._normalize_agy_result(
        _make_completed(returncode=0, stdout="   "),
        tool_profile="no_tools",
        requested_model=None,
        transcript_sha256="d" * 64,
    )
    assert empty_stdout["conversation_id"] is None
    assert empty_stdout["transcript_sha256"] == "d" * 64

    success = rgh._normalize_agy_result(
        _make_completed(returncode=0, stdout="LOOP_AGY_SMOKE_OK"),
        tool_profile="no_tools",
        requested_model=None,
        transcript_sha256="e" * 64,
    )
    assert success["conversation_id"] is None
    assert success["transcript_sha256"] == "e" * 64
