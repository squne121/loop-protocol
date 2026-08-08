"""Tests for AGY structured-output (`--output-format json`/`stream-json`)
argv allowlisting and provenance-bound grounded_research citation/count
fidelity (Issue #2038, follow-up of #2034).

Test style mirrors test_agy_invocation_argv_allowlist.py / test_preflight_agy.py:
importlib-based module load, no real `agy` binary required.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from typing import Any

import pytest


_RUN_HEADLESS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"
_PREFLIGHT_AGY_PATH = Path(__file__).resolve().parents[1] / "scripts" / "preflight_agy.py"


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rgh = _load_module(_RUN_HEADLESS_PATH, "run_gemini_headless")
preflight_agy = _load_module(_PREFLIGHT_AGY_PATH, "preflight_agy")


# ---------------------------------------------------------------------------
# AC1: --output-format allowlist (argv builder + positional structure
# allowlist validator).
# ---------------------------------------------------------------------------


def test_output_format_stream_json_allowlisted() -> None:
    """GIVEN a prompt and output_format="stream-json"
    WHEN _build_agy_inner_argv() builds the argv and _validate_agy_invocation_argv() validates it
    THEN the argv includes ["--output-format", "stream-json"] and validation does not raise.
    """
    argv = rgh._build_agy_inner_argv("agy", "hello world", output_format="stream-json")
    assert argv == ["agy", "-p", "hello world", "--output-format", "stream-json"]
    rgh._validate_agy_invocation_argv(argv)  # must not raise


def test_output_format_json_allowlisted() -> None:
    """GIVEN output_format="json"
    WHEN validated
    THEN no exception is raised (positive companion to the stream-json case above).
    """
    argv = rgh._build_agy_inner_argv("agy", "hello world", output_format="json")
    rgh._validate_agy_invocation_argv(argv)  # must not raise


def test_output_format_with_model_allowlisted_in_fixed_order() -> None:
    """GIVEN both a model and output_format
    WHEN the builder combines them
    THEN the trailing shape is [--model, <model>, --output-format, <fmt>] and validation passes.
    """
    argv = rgh._build_agy_inner_argv("agy", "hello world", "gemini-3-flash-preview", output_format="json")
    assert argv == ["agy", "-p", "hello world", "--model", "gemini-3-flash-preview", "--output-format", "json"]
    rgh._validate_agy_invocation_argv(argv, approved_models=frozenset({"gemini-3-flash-preview"}))


def test_output_format_unknown_value_rejected() -> None:
    """GIVEN an --output-format value outside {json, stream-json}
    WHEN validated
    THEN AgyInvocationPolicyError is raised (closed value enumeration, Issue #2038 Out of Scope guard).
    """
    argv = ["agy", "-p", "hello world", "--output-format", "yaml"]
    with pytest.raises(rgh.AgyInvocationPolicyError):
        rgh._validate_agy_invocation_argv(argv)


def test_output_format_permission_bypass_flag_still_rejected() -> None:
    """GIVEN a known permission-bypass flag appended after the approved prefix
    WHEN validated
    THEN it is still rejected (Issue #1807 defense-in-depth is not weakened by the #2038 extension).
    """
    argv = ["agy", "-p", "hello world", "--dangerously-skip-permissions"]
    with pytest.raises(rgh.AgyInvocationPolicyError):
        rgh._validate_agy_invocation_argv(argv)


def test_output_format_arbitrary_trailing_flag_after_output_format_rejected() -> None:
    """GIVEN --output-format followed by an unrelated extra flag
    WHEN validated
    THEN it is rejected -- the allowlist never degrades into a generic multi-flag parser.
    """
    argv = ["agy", "-p", "hello world", "--output-format", "json", "--dangerously-skip-permissions"]
    with pytest.raises(rgh.AgyInvocationPolicyError):
        rgh._validate_agy_invocation_argv(argv)


# ---------------------------------------------------------------------------
# AC2: citation_evidence cardinality is no longer truncated to 1.
# ---------------------------------------------------------------------------


def _hook_events_none() -> list[dict[str, Any]]:
    return []


def test_citation_evidence_cardinality_not_truncated_to_one() -> None:
    """GIVEN structured AGY stdout evidence with 3 distinct sources and a recognized tool-call trace
    WHEN _build_agy_grounded_research_metadata() builds grounding metadata
    THEN citation_evidence retains all 3 sources instead of being truncated to citation_evidence[:1].
    """
    stdout = (
        "AGY_GROUNDED_RESEARCH:"
        '{"tool_calls": [{"name": "web_search"}], '
        '"sources": ['
        '{"url": "https://example.com/a", "title": "A"}, '
        '{"url": "https://example.com/b", "title": "B"}, '
        '{"url": "https://example.com/c", "title": "C"}'
        "]}"
    )
    result = rgh._build_agy_grounded_research_metadata(stdout, hook_events=_hook_events_none())
    assert result["grounding_status"] == "grounded"
    assert result["url_citation_count"] == 3
    assert len(result["citation_evidence"]) == 3
    urls = {entry["url"] for entry in result["citation_evidence"]}
    assert urls == {"https://example.com/a", "https://example.com/b", "https://example.com/c"}


def test_citation_evidence_single_source_still_grounded() -> None:
    """GIVEN exactly one structured source (boundary case)
    WHEN grounding metadata is built
    THEN url_citation_count is 1 and grounding_status is "grounded" (no regression from the AC2 change).
    """
    stdout = 'AGY_GROUNDED_RESEARCH:{"tool_calls": [{"name": "web_search"}], "sources": [{"url": "https://example.com/only"}]}'
    result = rgh._build_agy_grounded_research_metadata(stdout, hook_events=_hook_events_none())
    assert result["url_citation_count"] == 1
    assert result["grounding_status"] == "grounded"


# ---------------------------------------------------------------------------
# AC3: web_tool_call_count / search_query_count reflect actual invocations.
# ---------------------------------------------------------------------------


def test_tool_call_count_reflects_actual_invocations() -> None:
    """GIVEN structured stdout evidence reporting 4 recognized tool calls
    WHEN grounding metadata is built
    THEN web_tool_call_count == 4 (not the previous hardcoded 1) and search_query_count matches it
    when no explicit query count is present in the structured evidence.
    """
    stdout = (
        "AGY_GROUNDED_RESEARCH:"
        '{"tool_calls": [{"name": "web_search"}, {"name": "web_search"}, '
        '{"name": "read_url"}, {"name": "web_search"}], '
        '"sources": [{"url": "https://example.com/a"}]}'
    )
    result = rgh._build_agy_grounded_research_metadata(stdout, hook_events=_hook_events_none())
    assert result["web_tool_call_count"] == 4
    assert result["search_query_count"] == 4


def test_search_query_count_uses_explicit_structured_queries_when_present() -> None:
    """GIVEN structured stdout evidence that separately reports 2 distinct queries
    alongside 1 tool call
    WHEN grounding metadata is built
    THEN search_query_count reflects the explicit query count (2), not the tool-call count (1).
    """
    stdout = (
        "AGY_GROUNDED_RESEARCH:"
        '{"tool_calls": [{"name": "web_search"}], '
        '"queries": ["first query", "second query"], '
        '"sources": [{"url": "https://example.com/a"}]}'
    )
    result = rgh._build_agy_grounded_research_metadata(stdout, hook_events=_hook_events_none())
    assert result["web_tool_call_count"] == 1
    assert result["search_query_count"] == 2


def test_tool_call_count_single_call_is_not_a_hardcoded_constant() -> None:
    """GIVEN a single recognized tool call (the previous behavior's only observed shape)
    WHEN grounding metadata is built
    THEN web_tool_call_count == 1 -- verifying the single-call case still behaves correctly
    now that the value is measured rather than hardcoded (regression guard for AC3).
    """
    stdout = 'AGY_GROUNDED_RESEARCH:{"tool_calls": [{"name": "web_search"}], "sources": [{"url": "https://example.com/a"}]}'
    result = rgh._build_agy_grounded_research_metadata(stdout, hook_events=_hook_events_none())
    assert result["web_tool_call_count"] == 1
    assert result["search_query_count"] == 1


# ---------------------------------------------------------------------------
# AC4: structured output capability_unavailable fail-closed, no silent
# fallback to stdout best-effort scraping.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "help_probe_result",
    [
        # unsupported: agy --help ran fine but never mentions --output-format
        {"exit_code": 0, "stdout": "usage: agy [-p PROMPT] [--model MODEL]", "stderr": ""},
        # unavailable: help probe itself failed
        {"exit_code": 1, "stdout": "", "stderr": "unknown error"},
        # evidence_invalid: flag mentioned but no recognized value token nearby
        {"exit_code": 0, "stdout": "usage: agy [--output-format FORMAT]", "stderr": ""},
    ],
)
def test_structured_output_capability_unavailable_returns_fail_closed(
    help_probe_result: dict[str, Any],
) -> None:
    """GIVEN a same-binary agy --help probe result classified by preflight_agy.py's
    capability matrix as unsupported / unavailable / evidence_invalid
    WHEN _build_agy_structured_output_metadata() is asked to build grounded_research metadata
    THEN it returns grounding_failure_class == "agy_web_grounding_capability_unavailable"
    with zeroed counts/empty citation_evidence, and never falls back to
    _build_agy_grounded_research_metadata()'s stdout best-effort text parsing
    (verified below via a stdout payload that WOULD otherwise be grounded).
    """
    # This stdout, if fed through the ordinary text-parsing path, would be
    # classified as "grounded" -- proving the capability_unavailable branch
    # really short-circuits before that parsing, rather than merely
    # happening to agree with it on this input.
    grounded_looking_stdout = (
        "AGY_GROUNDED_RESEARCH:"
        '{"tool_calls": [{"name": "web_search"}], "sources": [{"url": "https://example.com/a"}]}'
    )
    result = rgh._build_agy_structured_output_metadata(
        grounded_looking_stdout,
        help_probe_result=help_probe_result,
    )
    assert result["grounding_failure_class"] == "agy_web_grounding_capability_unavailable"
    assert result["grounding_status"] == "failed"
    assert result["grounding_backend"] == "none"
    assert result["web_tool_call_count"] == 0
    assert result["search_query_count"] == 0
    assert result["url_citation_count"] == 0
    assert result["citation_evidence"] == []
    assert result["parsed_evidence"] is None


def test_structured_output_capability_supported_uses_normal_metadata_path() -> None:
    """GIVEN a same-binary agy --help probe result that (per preflight_agy.py's
    evidence-priority policy) is only "inconclusive" help evidence, NOT a
    confirmed runtime_semantic_observation
    WHEN _build_agy_structured_output_metadata() is called
    THEN it still fail-closes to capability_unavailable (help alone never
    confirms "supported" -- Issue #1941 evidence-priority policy).
    """
    help_probe_result = {
        "exit_code": 0,
        "stdout": "usage: agy [--output-format {json,stream-json}]",
        "stderr": "",
    }
    result = rgh._build_agy_structured_output_metadata(
        "irrelevant stdout",
        help_probe_result=help_probe_result,
    )
    assert result["grounding_failure_class"] == "agy_web_grounding_capability_unavailable"
    assert result["structured_output_capability_status"] == "inconclusive"


def test_structured_output_capability_status_consumes_preflight_agy_ssot() -> None:
    """GIVEN preflight_agy.structured_output_capability_status() classifies a probe result
    WHEN run_gemini_headless._resolve_structured_output_capability_status() resolves it
    THEN the same status string is returned -- proving the runner consumes preflight_agy.py's
    matrix rather than independently parsing agy --help itself (Issue #2038 In Scope).
    """
    help_probe_result = {"exit_code": 0, "stdout": "usage: agy [-p PROMPT]", "stderr": ""}
    expected = preflight_agy.structured_output_capability_status(help_probe_result)["status"]
    actual = rgh._resolve_structured_output_capability_status(help_probe_result)
    assert actual == expected
    assert expected == "unsupported"


def test_structured_output_capability_unavailable_when_preflight_agy_not_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GIVEN preflight_agy is not importable in this environment (optional-dependency pattern)
    WHEN _resolve_structured_output_capability_status() is called
    THEN it returns "unavailable" fail-closed instead of raising or assuming "supported".
    """
    monkeypatch.setattr(rgh, "_PREFLIGHT_AGY_AVAILABLE", False)
    monkeypatch.setattr(rgh, "_preflight_agy", None)
    status = rgh._resolve_structured_output_capability_status({"exit_code": 0, "stdout": "x", "stderr": ""})
    assert status == "unavailable"
