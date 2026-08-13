"""Hermetic regression tests for Issue #1768: wiring validated `agy_provenance_hook_events`
into `_build_agy_grounded_research_metadata()` / `_normalize_agy_result()`'s
`grounding_status` decision.

Live investigation (`references/grounded-research-isolated-workspace-investigation.md`)
confirmed that isolated-workspace `search_web` / `read_url_content` calls can genuinely
succeed with a validated `agy_tool_provenance_v1` hook event captured, while AGY's own
stdout response contains no structured `tool_calls` self-report JSON at all (a plain-
prose answer). Before this fix, such cases were always misclassified as
`attempted_no_web_tool_call` (Issue #1708's fail-closed evidence gate false-positive).

No live `agy` binary or network access is required here: `_normalize_agy_result()` /
`_build_agy_grounded_research_metadata()` are called directly with hand-built
`subprocess.CompletedProcess` objects and `agy_provenance_hook_events` payloads
(mirroring the pattern in `test_agy_hook_events_propagation.py` / `test_agy_provider.py`).
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import types
from pathlib import Path
from typing import Any

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_gemini_headless", _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rgh = _load_module()


def _make_completed(returncode: int, stdout: str = "", stderr: str = "") -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=["agy", "-p", "test"], returncode=returncode, stdout=stdout, stderr=stderr)


def _valid_hook_event(tool_name: str = "search_web") -> dict[str, Any]:
    return {
        "schema": "agy_tool_provenance_v1",
        "version": 1,
        "event": "PreToolUse",
        "toolCall": {
            "name": tool_name,
            "args_sha256": hashlib.sha256(b'{"query":"test"}').hexdigest(),
        },
        "stepIdx": 3,
        "conversationId": "conv-1768-test",
        "transcript_path_ref": "sha256:" + hashlib.sha256(b"/redacted/transcript.jsonl").hexdigest(),
        "transcript_sha256": hashlib.sha256(b"transcript-body").hexdigest(),
        "parent_run_id": "run-1768",
        "subtask_id": "subtask-1768",
        "attempt_id": "attempt-1",
        "provider": "agy",
        "tool_profile": "grounded_research",
        "monotonic_ns": 123456789,
        "utc": "2026-07-25T00:00:00.000000Z",
    }


# ---------------------------------------------------------------------------
# (a) validated hook event + no stdout self-report -> must NOT be
#     attempted_no_web_tool_call (the reported bug).
# ---------------------------------------------------------------------------


def test_validated_hook_event_without_stdout_self_report_not_attempted_no_web_tool_call() -> None:
    """AC4: a validated hook event alone (plain-prose stdout, no structured tool_calls
    JSON, no citation URL) must not be misclassified as attempted_no_web_tool_call."""
    completed = _make_completed(0, stdout="Cloudy, high of 34C, thunderstorms expected this evening.")
    completed.agy_provenance_hook_events = [_valid_hook_event()]  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]

    result = rgh._normalize_agy_result(
        completed,
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]

    assert evidence["grounding_status"] != "attempted_no_web_tool_call"
    assert evidence["grounding_failure_class"] != "agy_web_grounding_tool_call_missing"
    assert evidence["web_tool_call_count"] == 1
    # No citation URL anywhere in stdout -> attempted_no_citations, not silently "grounded".
    assert evidence["grounding_status"] == "attempted_no_citations"
    assert evidence["url_citation_count"] == 0


def test_validated_read_url_content_hook_event_without_stdout_self_report() -> None:
    """Same as above but for the other canonical web tool name, read_url_content."""
    completed = _make_completed(0, stdout="Here is a plain-prose summary of the page.")
    completed.agy_provenance_hook_events = [_valid_hook_event(tool_name="read_url_content")]  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]

    result = rgh._normalize_agy_result(
        completed,
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_status"] != "attempted_no_web_tool_call"
    assert evidence["web_tool_call_count"] == 1


# ---------------------------------------------------------------------------
# (b) validated hook event + real grounding citation URL in stdout -> grounded.
# ---------------------------------------------------------------------------


def test_validated_hook_event_with_real_grounding_citation_url_is_grounded() -> None:
    """AC6(b): validated hook event + a real vertexaisearch grounding-api-redirect
    citation URL in stdout (but no structured tool_calls/citations JSON) -> grounded,
    with the URL captured as citation evidence."""
    citation_url = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AbCdEf123"
    completed = _make_completed(0, stdout=f"Sunny, 27C. Source: {citation_url}")
    completed.agy_provenance_hook_events = [_valid_hook_event()]  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]

    result = rgh._normalize_agy_result(
        completed,
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]

    assert evidence["grounding_status"] == "grounded"
    assert evidence["grounding_backend"] == "agy_native_websearch"
    assert evidence["url_citation_count"] == 1
    assert evidence["citation_evidence"][0]["url"] == citation_url
    assert result["ok"] is True


def test_generic_url_without_vertex_grounding_pattern_is_candidate_not_grounded() -> None:
    """A generic URL remains available for source-content verification, but neither
    provider hook nor tool telemetry may promote it to grounded success."""
    completed = _make_completed(0, stdout="Sunny. See https://example.com/weather for details.")
    completed.agy_provenance_hook_events = [_valid_hook_event()]  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]

    result = rgh._normalize_agy_result(
        completed,
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_status"] == "citation_candidates_unverified"
    assert evidence["grounding_backend"] == "agy_final_result"
    assert evidence["grounding_failure_class"] == "agy_evidence_quality_unverified"
    assert evidence["url_citation_count"] == 1


# ---------------------------------------------------------------------------
# (c) no hook event, stdout-only hallucination claim -> still fail-closed.
# ---------------------------------------------------------------------------


def test_no_hook_events_stdout_only_hallucination_still_fail_closed() -> None:
    """AC5/AC6(c): with an empty hook_events list, stdout-only claims of success remain
    fail-closed exactly as before this fix (no regression on the fail-closed property)."""
    completed = _make_completed(0, stdout="I searched the web and found a great answer!")
    completed.agy_provenance_hook_events = []  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]

    result = rgh._normalize_agy_result(
        completed,
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_status"] == "attempted_no_web_tool_call"
    assert evidence["grounding_failure_class"] == "agy_web_grounding_tool_call_missing"
    assert result["ok"] is False
    assert result["failure_class"] == "agy_web_grounding_tool_call_missing"


def test_missing_hook_events_attribute_stdout_only_hallucination_still_fail_closed() -> None:
    """Same as above, but the `agy_provenance_hook_events` attribute is entirely absent
    (direct/mocked CompletedProcess callers that never went through `_run_agy()`)."""
    completed = _make_completed(0, stdout="I searched the web and found a great answer!")
    # Intentionally do not set agy_provenance_hook_events / agy_provenance_hook_load_error.

    result = rgh._normalize_agy_result(
        completed,
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_status"] == "attempted_no_web_tool_call"
    assert result["ok"] is False


def test_malformed_hook_event_stdout_only_hallucination_still_fail_closed() -> None:
    """A schema-invalid hook event (missing required fields) must not be treated as
    confirmation of a tool call -- only validated events count."""
    completed = _make_completed(0, stdout="I searched the web and found a great answer!")
    completed.agy_provenance_hook_events = [{"schema": "agy_tool_provenance_v1", "toolCall": {"name": "search_web"}}]  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]

    result = rgh._normalize_agy_result(
        completed,
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_status"] == "attempted_no_web_tool_call"
    assert result["ok"] is False


def test_unrecognized_tool_name_hook_event_stdout_only_hallucination_still_fail_closed() -> None:
    """A validated event for a non-canonical tool name (e.g. a legacy alias) must not
    confirm a web tool call for grounded_research evidence purposes."""
    completed = _make_completed(0, stdout="I searched the web and found a great answer!")
    completed.agy_provenance_hook_events = [_valid_hook_event(tool_name="web_search")]  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]

    result = rgh._normalize_agy_result(
        completed,
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_status"] == "attempted_no_web_tool_call"
    assert result["ok"] is False


def test_hook_load_error_present_stdout_only_hallucination_still_fail_closed() -> None:
    """A hook load/generation error (fail-closed signal from `_run_agy()`) combined with
    a stdout-only success claim must still fail closed, not silently "pass" due to the
    error swallowing hook_events into an empty list."""
    completed = _make_completed(0, stdout="I searched the web and found a great answer!")
    completed.agy_provenance_hook_events = []  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = "workspace_hook_generation_failed: disk full"  # type: ignore[attr-defined]

    result = rgh._normalize_agy_result(
        completed,
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_status"] == "attempted_no_web_tool_call"
    assert result["ok"] is False
    assert result["agy_provenance_hook_load_error"] == "workspace_hook_generation_failed: disk full"
