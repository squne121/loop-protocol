"""Hermetic regression tests for Issue #1771.

Covers AC6: `validate_agy_fanout_e2e_evidence.py`'s predicate_07 / predicate_08
/ predicate_10 (`_predicate_hook_provenance()`) must return `status: pass`
against a `delegation_result/v1` + hook event bundle produced by *actually
running* `run_gemini_headless.run_delegation()` (this Issue's changed
production code path), not only against the hand-fabricated synthetic
fixture `tests/fixtures/agy_fanout_e2e/build_bundle.py` already uses (that
fixture predates this Issue and bypasses `_run_delegation_core()` /
`_run_agy()` / `_normalize_agy_result()` entirely -- it is why predicate_07/
08/10 could pass in the existing hermetic validator suite while the *live*
#1494 E2E run still hit the structural gap this Issue closes).

No live `agy` binary or network access is required: `_run_agy` is mocked with
a `side_effect` that returns a synthetic `subprocess.CompletedProcess` whose
`agy_provenance_hook_events` are built directly from the `run_context` kwarg
it receives -- mirroring exactly what the real isolated-workspace PreToolUse
wrapper (`agy_tool_provenance.py`) would produce, per
`test_run_gemini_headless_run_context.py`'s `_hook_event_from_run_context()`
helper (duplicated here to keep this file's regression scope self-contained
and independent of that other new test file).
"""
from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
_RGH_PATH = _SCRIPTS_DIR / "run_gemini_headless.py"
_VALIDATOR_PATH = _SCRIPTS_DIR / "validate_agy_fanout_e2e_evidence.py"
_PROVENANCE_PATH = _SCRIPTS_DIR / "agy_tool_provenance.py"


def _load_module(path: Path, register_name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(register_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[register_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


rgh = _load_module(_RGH_PATH, "run_gemini_headless_1771")
validator = _load_module(_VALIDATOR_PATH, "validate_agy_fanout_e2e_evidence_1771")
agy_tool_provenance = _load_module(_PROVENANCE_PATH, "agy_tool_provenance_1771")


def _hook_event_from_run_context(
    run_context: dict[str, Any] | None, tool_name: str = "search_web"
) -> dict[str, Any]:
    """Realistic `agy_tool_provenance_v1` event, every REQUIRED_TOP_FIELDS
    correlation field copied verbatim from *run_context* (matching what the
    real wrapper does with `hook_context.json`, written by
    `write_hook_context()`)."""
    ctx = run_context or {}
    return {
        "schema": agy_tool_provenance.SCHEMA_NAME,
        "version": agy_tool_provenance.SCHEMA_VERSION,
        "event": "PreToolUse",
        "toolCall": {
            "name": tool_name,
            "args_sha256": hashlib.sha256(b'{"query":"test"}').hexdigest(),
        },
        "stepIdx": 0,
        "conversationId": "conv-1771-e2e-test",
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


def _agy_grounded_research_request(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": "delegation_request_v1",
        "tool_profile": "grounded_research",
        "provider": "agy",
        "prompt": "search for the current LOOP_PROTOCOL version",
        "objective": "Issue #1771 predicate_07/08/10 hermetic regression",
        "instructions": ["Return exactly: LOOP_AGY_SMOKE_OK", "Do not add any extra text"],
        "output_sections": ["response"],
        "context_files": [],
        "timeout_sec": 300,
        "parent_run_id": "run-1771-e2e",
        "subtask_id": "grounded_research",
        "attempt_id": "attempt-1",
    }
    base.update(kwargs)
    return base


def _run_real_delegation_with_search_web(**request_kwargs: Any) -> dict[str, Any]:
    def _fake_run_agy(
        prompt: str, timeout_sec: int, *, run_context: dict[str, Any] | None = None
    ) -> "subprocess.CompletedProcess[str]":
        completed = subprocess.CompletedProcess(
            args=["agy", "-p", prompt], returncode=0, stdout="grounded response text", stderr=""
        )
        completed.agy_provenance_hook_events = [  # type: ignore[attr-defined]
            _hook_event_from_run_context(run_context)
        ]
        completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]
        return completed

    request = _agy_grounded_research_request(**request_kwargs)
    with patch.object(rgh, "_run_agy", side_effect=_fake_run_agy):
        result = rgh.run_delegation(request)
    return {"request": request, "result": result}


def _predicate_status(results: list[Any], predicate_id: str) -> str:
    for entry in results:
        if entry.predicate_id == predicate_id:
            return entry.status
    raise AssertionError(f"{predicate_id} not found")


# ---------------------------------------------------------------------------
# AC6: predicate_07 / predicate_08 / predicate_10 pass against a *real*
# production delegation_result/v1 + hook_events (search_web executed).
# ---------------------------------------------------------------------------


def test_predicate_07_08_10_pass_against_real_run_gemini_headless_output() -> None:
    child = _run_real_delegation_with_search_web()
    result = child["result"]

    # Sanity: this Issue's changes actually populated the top-level fields
    # (predicate_10 depends directly on these being truthy).
    assert result.get("conversation_id")
    assert result.get("transcript_sha256")
    assert result.get("agy_provenance_hook_events")

    bundle = {
        "children": {
            validator.PROFILE_GROUNDED_RESEARCH: {
                "request": child["request"],
                "result": result,
                "hook_events": result["agy_provenance_hook_events"],
            }
        }
    }

    predicate_results = validator._predicate_hook_provenance(bundle)

    assert _predicate_status(predicate_results, "predicate_07") == "pass"
    assert _predicate_status(predicate_results, "predicate_08") == "pass"
    assert _predicate_status(predicate_results, "predicate_10") == "pass"


def test_predicate_07_08_10_fail_when_run_context_not_wired() -> None:
    """Negative control: without this Issue's fix (simulated here by a
    standalone, non-fan-out request whose hook event therefore carries empty
    correlation/transcript fields, matching pre-#1771 behavior),
    predicate_07/08/10 correctly FAIL -- proving the positive test above is
    actually exercising the fix rather than a vacuously-passing predicate."""
    request = _agy_grounded_research_request()
    del request["parent_run_id"]
    del request["subtask_id"]
    del request["attempt_id"]

    def _fake_run_agy(
        prompt: str, timeout_sec: int, *, run_context: dict[str, Any] | None = None
    ) -> "subprocess.CompletedProcess[str]":
        completed = subprocess.CompletedProcess(
            args=["agy", "-p", prompt], returncode=0, stdout="grounded response text", stderr=""
        )
        completed.agy_provenance_hook_events = [  # type: ignore[attr-defined]
            _hook_event_from_run_context(run_context)
        ]
        completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]
        return completed

    with patch.object(rgh, "_run_agy", side_effect=_fake_run_agy):
        result = rgh.run_delegation(request)

    bundle = {
        "children": {
            validator.PROFILE_GROUNDED_RESEARCH: {
                "request": request,
                "result": result,
                "hook_events": result["agy_provenance_hook_events"],
            }
        }
    }

    predicate_results = validator._predicate_hook_provenance(bundle)

    # validate_provenance_event() rejects the event outright (empty
    # parent_run_id/subtask_id/attempt_id/tool_profile/transcript_sha256 are
    # all REQUIRED_TOP_FIELDS), so no event is ever "matched" -> P7/P8 fail,
    # and P10 (which also requires a non-empty conversation_id/
    # transcript_sha256 on the *result*) fails too since neither is set for
    # a standalone (non-fan-out) call.
    assert _predicate_status(predicate_results, "predicate_07") == "fail"
    assert _predicate_status(predicate_results, "predicate_08") == "fail"
    assert _predicate_status(predicate_results, "predicate_10") == "fail"
