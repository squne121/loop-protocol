"""Tests for AGY structured-output (`--output-format json`/`stream-json`)
argv allowlisting and provenance-bound grounded_research citation/count
fidelity (Issue #2038, follow-up of #2034).

Test style mirrors test_agy_invocation_argv_allowlist.py / test_preflight_agy.py:
importlib-based module load, no real `agy` binary required.
"""

from __future__ import annotations

import importlib.util
import subprocess
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

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


def _valid_hook_event(tool_name: str = "search_web") -> dict[str, Any]:
    """A validated `agy_tool_provenance_v1` PreToolUse hook event fixture
    (Issue #2038 fix_delta iteration 2): the legacy stdout/marker parser now
    requires this corroboration before resolving `grounding_status ==
    "grounded"` -- mirrors `test_agy_provenance_grounding_wiring.py`'s
    `_valid_hook_event()` fixture shape."""
    import hashlib

    return {
        "schema": "agy_tool_provenance_v1",
        "version": 1,
        "event": "PreToolUse",
        "toolCall": {
            "name": tool_name,
            "args_sha256": hashlib.sha256(b'{"query":"test"}').hexdigest(),
        },
        "stepIdx": 1,
        "conversationId": "conv-2038-fix-delta-test",
        "transcript_path_ref": "sha256:" + hashlib.sha256(b"/redacted/transcript.jsonl").hexdigest(),
        "transcript_sha256": hashlib.sha256(b"transcript-body").hexdigest(),
        "parent_run_id": "",
        "subtask_id": "",
        "attempt_id": "",
        "provider": "agy",
        "tool_profile": "grounded_research",
        "monotonic_ns": 1,
        "utc": "2026-08-09T00:00:00.000000Z",
    }


def _valid_hook_events(tool_name: str = "search_web") -> list[dict[str, Any]]:
    return [_valid_hook_event(tool_name=tool_name)]


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
    result = rgh._build_agy_grounded_research_metadata(stdout, hook_events=_valid_hook_events())
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
    result = rgh._build_agy_grounded_research_metadata(stdout, hook_events=_valid_hook_events())
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
    result = rgh._build_agy_grounded_research_metadata(stdout, hook_events=_valid_hook_events())
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
    result = rgh._build_agy_grounded_research_metadata(stdout, hook_events=_valid_hook_events())
    assert result["web_tool_call_count"] == 1
    assert result["search_query_count"] == 2


def test_tool_call_count_single_call_is_not_a_hardcoded_constant() -> None:
    """GIVEN a single recognized tool call (the previous behavior's only observed shape)
    WHEN grounding metadata is built
    THEN web_tool_call_count == 1 -- verifying the single-call case still behaves correctly
    now that the value is measured rather than hardcoded (regression guard for AC3).
    """
    stdout = 'AGY_GROUNDED_RESEARCH:{"tool_calls": [{"name": "web_search"}], "sources": [{"url": "https://example.com/a"}]}'
    result = rgh._build_agy_grounded_research_metadata(stdout, hook_events=_valid_hook_events())
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


# ---------------------------------------------------------------------------
# fix_delta P0-1/P0-2/P0-3 (OWNER REQUEST_CHANGES, PR #2043, 2026-08-08):
# route-level production wiring tests -- _run_agy() through
# _normalize_agy_result(), not just the direct builder/metadata unit tests
# above. See run_gemini_headless.py's _run_agy()/_normalize_agy_result()
# docstrings for the exact routing contract these tests pin down.
# ---------------------------------------------------------------------------


def _make_stream_json_stdout(url: str = "https://example.com/a") -> str:
    return "\n".join(
        [
            '{"type": "init"}',
            '{"type": "step_update", "step_type": "tool_call", "tool_info": '
            '{"name": "web_search", "output": {"sources": [{"url": "%s", "title": "A"}]}}}' % url,
            '{"type": "result", "status": "success"}',
        ]
    )


def _supported_capability_record() -> dict[str, Any]:
    return {
        "status": "supported",
        "reason_code": "semantic_probe_valid_init_step_result_stream_observed",
        "evidence_source": "runtime_semantic_observation",
        "detail": None,
    }


def _unsupported_capability_record() -> dict[str, Any]:
    return {
        "status": "unsupported",
        "reason_code": "flag_absent_from_help",
        "evidence_source": "help",
        "detail": None,
    }


# (a) grounded_research + supported -> real argv contains --output-format stream-json.
def test_run_agy_supported_capability_attaches_output_format_to_real_argv() -> None:
    """fix_delta P0-1(a): when the same-binary capability resolver reports
    "supported", _run_agy() attaches --output-format stream-json to the
    REAL subprocess.run() argv (not just the direct builder/validator unit
    tests above -- this is the previously-unreachable production wiring)."""
    captured: dict[str, Any] = {"argv": None}

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured["argv"] = list(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_make_stream_json_stdout(), stderr="")

    token = rgh._AGY_TOOL_PROFILE_CTX.set("grounded_research")
    try:
        with patch.object(
            rgh, "_resolve_run_agy_structured_output_capability_record", return_value=_supported_capability_record()
        ):
            with patch("subprocess.run", side_effect=mock_run):
                completed = rgh._run_agy("test prompt", 30)
    finally:
        rgh._AGY_TOOL_PROFILE_CTX.reset(token)

    assert captured["argv"] is not None
    assert "--output-format" in captured["argv"]
    fmt_index = captured["argv"].index("--output-format")
    assert captured["argv"][fmt_index + 1] == "stream-json"
    assert completed.agy_structured_output_used is True  # type: ignore[attr-defined]
    assert completed.agy_structured_output_capability_record["status"] == "supported"  # type: ignore[attr-defined]


def test_run_agy_supported_capability_end_to_end_uses_structured_parser() -> None:
    """fix_delta P0-1/P0-3: end-to-end through run_delegation(), a
    "supported" capability + a valid stream-json response is parsed by the
    strict NDJSON parser (grounding_backend distinguishes the structured
    route from the legacy text-marker route)."""

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_make_stream_json_stdout(), stderr="")

    with patch.object(
        rgh, "_resolve_run_agy_structured_output_capability_record", return_value=_supported_capability_record()
    ):
        with patch("subprocess.run", side_effect=mock_run):
            result = rgh.run_delegation(
                {
                    "schema": "delegation_request_v1",
                    "tool_profile": "grounded_research",
                    "provider": "agy",
                    "prompt": "Search for something",
                    "objective": "Test structured route end-to-end wiring",
                    "instructions": ["Search", "Cite sources"],
                    "output_sections": ["response"],
                    "context_files": [],
                }
            )

    assert result["ok"] is True
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_backend"] == "agy_native_websearch_structured"
    assert evidence["grounding_status"] == "grounded"
    assert evidence["url_citation_count"] == 1
    assert evidence["citation_evidence"][0]["url"] == "https://example.com/a"


# (b) grounded_research + non-supported -> real argv never contains
# --output-format, and grounded_research_evidence is built via the
# UNCHANGED legacy text/marker parser (never capability_unavailable) --
# preserving pre-existing protected regression tests
# (test_agy_provider.py::test_ac7_agy_grounded_research_supported,
# test_agy_invocation_argv_allowlist.py's real-_run_agy() tests) that rely
# on grounded_research remaining functional via the legacy route whenever
# structured output capability is not confirmed "supported" for the current
# environment (Issue #2038 AC4's capability_unavailable fail-closed path is
# reachable specifically via the structured route -- see
# test_structured_output_capability_unavailable_returns_fail_closed above
# and the malformed-stream regression test below -- not by making
# grounded_research universally non-functional by default). fix_delta
# iteration 2: "functional via the legacy route" now also requires a
# validated `agy_tool_provenance_v1` hook event to actually reach
# grounding_status "grounded" -- see
# test_run_agy_non_supported_capability_end_to_end_legacy_semantics_regression_proof
# and its companion _grounded_with_hook_corroboration test below.
def test_run_agy_non_supported_capability_argv_never_gets_output_format() -> None:
    """fix_delta P0-1(b) (adapted -- see module docstring note below for
    rationale): a "unsupported" capability status means the real argv is
    built exactly as before this fix_delta (no --output-format flag)."""
    captured: dict[str, Any] = {"argv": None}
    grounded_output = (
        "Response from AGY.\n"
        '{"grounding":{"queries":["AGY WebSearch"],"sources":[{"url":"https://example.com","title":"example"}]},'
        '"tool_calls":[{"name":"web_search"}]}'
    )

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured["argv"] = list(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=grounded_output, stderr="")

    token = rgh._AGY_TOOL_PROFILE_CTX.set("grounded_research")
    try:
        with patch.object(
            rgh,
            "_resolve_run_agy_structured_output_capability_record",
            return_value=_unsupported_capability_record(),
        ):
            with patch("subprocess.run", side_effect=mock_run):
                completed = rgh._run_agy("test prompt", 30)
    finally:
        rgh._AGY_TOOL_PROFILE_CTX.reset(token)

    assert captured["argv"] is not None
    assert "--output-format" not in captured["argv"]
    assert completed.agy_structured_output_used is False  # type: ignore[attr-defined]


def test_run_agy_non_supported_capability_end_to_end_legacy_semantics_regression_proof() -> None:
    """fix_delta P0 iteration 2 (ac4_unenforced_in_default_production_path):
    end-to-end through run_delegation(), grounded_research + non-supported
    capability (the realistic DEFAULT production configuration -- no
    AGY_PREFLIGHT_CONFIRM_RUNTIME_PROBE_COST opt-in, current AGY resolves to
    "inconclusive" not "supported") routes through the legacy text/marker
    parser exactly as before -- but a bare model-generated `tool_calls` /
    `sources` JSON blob in stdout, with NO corroborating validated
    `agy_tool_provenance_v1` hook event, must now resolve fail-closed
    instead of "grounded". This is the exact false-grounding anti-pattern
    the pr-reviewer flagged (this test previously asserted the UNSAFE
    "grounded" outcome for this same fixture -- see git history) and OWNER
    gate 4 ("custom marker や model-generated tool_calls / sources だけでは
    grounded にならない") targets. The route stays reachable and functional
    (`ok is False` here is a genuine fail-closed *evidence* verdict, not a
    hard error) -- see the companion
    test_run_agy_non_supported_capability_end_to_end_legacy_semantics_grounded_with_hook_corroboration
    below for proof that real, hook-corroborated grounding still works via
    this same legacy route."""
    grounded_output = (
        "Response from AGY.\n"
        '{"grounding":{"queries":["AGY WebSearch"],"sources":[{"url":"https://example.com","title":"example"}]},'
        '"tool_calls":[{"name":"web_search"}]}'
    )

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=grounded_output, stderr="")

    with patch.object(
        rgh,
        "_resolve_run_agy_structured_output_capability_record",
        return_value=_unsupported_capability_record(),
    ):
        with patch("subprocess.run", side_effect=mock_run):
            result = rgh.run_delegation(
                {
                    "schema": "delegation_request_v1",
                    "tool_profile": "grounded_research",
                    "provider": "agy",
                    "prompt": "Search for something",
                    "objective": "Test legacy route regression-proofing",
                    "instructions": ["Search", "Cite sources"],
                    "output_sections": ["response"],
                    "context_files": [],
                }
            )

    evidence = result["grounded_research_evidence"]
    # fix_delta iteration 2: model-generated stdout JSON alone (no hook
    # corroboration -- this end-to-end flow's mocked subprocess never wrote
    # a real hook_events.jsonl) must never resolve to "grounded".
    assert evidence["grounding_status"] != "grounded"
    assert evidence["grounding_failure_class"] == "agy_web_grounding_hook_corroboration_missing"
    assert evidence["url_citation_count"] == 0
    assert evidence["citation_evidence"] == []
    assert result["ok"] is False
    assert result["failure_class"] == "agy_web_grounding_hook_corroboration_missing"


def test_run_agy_non_supported_capability_end_to_end_legacy_semantics_grounded_with_hook_corroboration() -> None:
    """Companion to the regression-proof test above: the SAME legacy stdout
    self-report evidence, when corroborated by a validated
    `agy_tool_provenance_v1` hook event, still resolves to "grounded" via
    the unchanged legacy backend/route -- proving fix_delta iteration 2 did
    not regress grounded_research into an always-fail-closed no-op for the
    common non-supported-capability case; it only closes the
    hook-corroboration gap."""
    grounded_output = (
        "Response from AGY.\n"
        '{"grounding":{"queries":["AGY WebSearch"],"sources":[{"url":"https://example.com","title":"example"}]},'
        '"tool_calls":[{"name":"web_search"}]}'
    )
    completed = subprocess.CompletedProcess(
        args=["agy", "-p", "x"], returncode=0, stdout=grounded_output, stderr=""
    )
    completed.agy_structured_output_used = False  # type: ignore[attr-defined]
    completed.agy_structured_output_capability_record = _unsupported_capability_record()  # type: ignore[attr-defined]
    completed.agy_provenance_hook_events = _valid_hook_events()  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]

    result = rgh._normalize_agy_result(completed, tool_profile="grounded_research", requested_model=None)

    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_backend"] == "agy_native_websearch"
    assert evidence["grounding_status"] == "grounded"
    assert evidence["url_citation_count"] == 1
    assert result["ok"] is True


# (c) all other profiles -> unchanged legacy text argv/stdout semantics.
def test_run_agy_non_grounded_research_profile_never_resolves_capability() -> None:
    """fix_delta P0-1(c): a non-grounded_research profile (e.g. no_tools)
    never calls the structured-output capability resolver at all -- proving
    the new wiring is scoped exactly to grounded_research and cannot affect
    any other profile's argv/stdout semantics."""
    completed_stub = subprocess.CompletedProcess(args=["agy", "-p", "x"], returncode=0, stdout="ok", stderr="")

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return completed_stub

    token = rgh._AGY_TOOL_PROFILE_CTX.set("no_tools")
    try:
        with patch.object(
            rgh,
            "_resolve_run_agy_structured_output_capability_record",
            side_effect=AssertionError("capability resolver must not be called for non-grounded_research profiles"),
        ):
            with patch("subprocess.run", side_effect=mock_run):
                completed = rgh._run_agy("test prompt", 30)
    finally:
        rgh._AGY_TOOL_PROFILE_CTX.reset(token)

    assert completed.agy_structured_output_used is False  # type: ignore[attr-defined]
    assert completed.agy_structured_output_capability_record is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# P0-3: strict stream-json NDJSON state-machine parser (preflight_agy.py).
# ---------------------------------------------------------------------------


def test_parse_agy_stream_json_stream_accepts_valid_init_step_result() -> None:
    """A well-formed init -> step_update (with a recognized web tool's
    tool_info.output.sources) -> terminal result stream parses as valid and
    yields exactly one validated, URL-checked source record."""
    verdict = preflight_agy.parse_agy_stream_json_stream(_make_stream_json_stdout())
    assert verdict["status"] == "valid"
    assert verdict["init_count"] == 1
    assert verdict["result_count"] == 1
    assert len(verdict["source_records"]) == 1
    assert verdict["source_records"][0]["url"] == "https://example.com/a"


def test_parse_agy_stream_json_stream_rejects_missing_terminal_result() -> None:
    """A stream with no terminal result event (truncated mid-stream) is
    rejected, not partially accepted."""
    stream = '{"type": "init"}\n{"type": "step_update", "step_type": "text"}'
    verdict = preflight_agy.parse_agy_stream_json_stream(stream)
    assert verdict["status"] == "invalid"
    assert verdict["reason_code"] == "terminal_event_not_result_or_missing"


def test_parse_agy_stream_json_stream_rejects_duplicate_terminal_result() -> None:
    """Two `result` events in the same stream is rejected (duplicate
    terminal), even though the last event IS type "result"."""
    stream = '\n'.join(['{"type": "init"}', '{"type": "result"}', '{"type": "result"}'])
    verdict = preflight_agy.parse_agy_stream_json_stream(stream)
    assert verdict["status"] == "invalid"
    assert verdict["reason_code"] == "result_count_not_exactly_one"


def test_parse_agy_stream_json_stream_rejects_unknown_event_type() -> None:
    """An event whose top-level `type` is not in the closed
    {init, step_update, result} enum is rejected (fail-closed, never
    silently skipped)."""
    stream = '\n'.join(['{"type": "init"}', '{"type": "unknown_future_event"}', '{"type": "result"}'])
    verdict = preflight_agy.parse_agy_stream_json_stream(stream)
    assert verdict["status"] == "invalid"
    assert verdict["reason_code"] == "line_1_unknown_event_type"


def test_parse_agy_stream_json_stream_rejects_malformed_json_line() -> None:
    """A truncated/malformed NDJSON line is rejected, never best-effort
    recovered."""
    stream = '{"type": "init"}\n{"type": "step_update", truncated'
    verdict = preflight_agy.parse_agy_stream_json_stream(stream)
    assert verdict["status"] == "invalid"
    assert verdict["reason_code"] == "line_1_not_valid_json"


def test_parse_agy_stream_json_stream_never_accepts_legacy_marker_text() -> None:
    """The legacy `AGY_GROUNDED_RESEARCH:` custom-marker text (accepted by
    the old text/marker parser) is NOT valid stream-json NDJSON and is
    rejected outright by the strict parser (fix_delta P0-3: the structured
    route never falls back to the legacy marker format)."""
    legacy_marker_stdout = (
        "AGY_GROUNDED_RESEARCH:"
        '{"tool_calls": [{"name": "web_search"}], "sources": [{"url": "https://example.com/a"}]}'
    )
    verdict = preflight_agy.parse_agy_stream_json_stream(legacy_marker_stdout)
    assert verdict["status"] == "invalid"


def test_parse_agy_stream_json_stream_rejects_unrecognized_tool_source() -> None:
    """A step_update whose tool_info.name is NOT a recognized canonical web
    tool never contributes a source record, even if its tool_info.output
    contains a well-formed url (step/tool-call correlation requirement)."""
    stream = "\n".join(
        [
            '{"type": "init"}',
            '{"type": "step_update", "step_type": "tool_call", "tool_info": '
            '{"name": "run_shell_command", "output": {"sources": [{"url": "https://example.com/a"}]}}}',
            '{"type": "result"}',
        ]
    )
    verdict = preflight_agy.parse_agy_stream_json_stream(stream)
    assert verdict["status"] == "valid"
    assert verdict["source_records"] == []


def test_parse_agy_stream_json_stream_rejects_generic_userinfo_url() -> None:
    """A source URL containing a userinfo component (user:pass@host) is
    rejected by URL validation even when tool/step correlation is
    otherwise satisfied."""
    stream = _make_stream_json_stdout(url="https://user:pass@example.com/a")
    verdict = preflight_agy.parse_agy_stream_json_stream(stream)
    assert verdict["status"] == "valid"
    assert verdict["source_records"] == []


# fix_delta P0-1/P0-3: _normalize_agy_result() structured-route wiring
# (direct, hermetic -- no _run_agy() needed).
def test_normalize_agy_result_structured_route_valid_stream_grounded() -> None:
    """When agy_structured_output_used=True (this exact invocation attached
    --output-format), _normalize_agy_result() uses the strict NDJSON parser
    -- proving the AC4/P0-3 wiring is reachable through the full
    completed -> normalize pipeline, not just the direct builder call."""
    completed = subprocess.CompletedProcess(
        args=["agy", "-p", "x", "--output-format", "stream-json"],
        returncode=0,
        stdout=_make_stream_json_stdout(),
        stderr="",
    )
    completed.agy_structured_output_used = True  # type: ignore[attr-defined]
    completed.agy_structured_output_capability_record = _supported_capability_record()  # type: ignore[attr-defined]
    completed.agy_provenance_hook_events = []  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]

    result = rgh._normalize_agy_result(completed, tool_profile="grounded_research", requested_model=None)
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_backend"] == "agy_native_websearch_structured"
    assert evidence["grounding_status"] == "grounded"
    assert result["ok"] is True


def test_normalize_agy_result_structured_route_malformed_stream_fail_closed() -> None:
    """When agy_structured_output_used=True but the real agy stdout is not
    a valid stream-json NDJSON stream (e.g. plain prose despite the flag
    having been attached), the structured route fail-closes to
    agy_web_grounding_stream_json_malformed -- never silently falls back to
    the legacy text/marker parser on the SAME invocation."""
    completed = subprocess.CompletedProcess(
        args=["agy", "-p", "x", "--output-format", "stream-json"],
        returncode=0,
        stdout="I searched the web and found a great answer!",
        stderr="",
    )
    completed.agy_structured_output_used = True  # type: ignore[attr-defined]
    completed.agy_structured_output_capability_record = _supported_capability_record()  # type: ignore[attr-defined]
    completed.agy_provenance_hook_events = []  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]

    result = rgh._normalize_agy_result(completed, tool_profile="grounded_research", requested_model=None)
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_failure_class"] == "agy_web_grounding_stream_json_malformed"
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# P0-2: two-stage same-binary capability determination
# (preflight_agy.get_or_compute_structured_output_capability()).
# ---------------------------------------------------------------------------


def test_get_or_compute_structured_output_capability_stage1_only_stays_inconclusive_without_cost_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without AGY_PREFLIGHT_CONFIRM_RUNTIME_PROBE_COST=1, Stage 2 (a real,
    model-backed semantic probe) never runs even when Stage 1 (--help)
    evidence establishes candidate feature existence -- "supported" remains
    genuinely unreachable without an explicit runtime-probe cost opt-in
    (mirrors the existing leading_slash_is_literal probe's cost gate)."""
    monkeypatch.delenv("AGY_PREFLIGHT_CONFIRM_RUNTIME_PROBE_COST", raising=False)

    def fake_run_help(agy_bin: str) -> Any:
        class _Proc:
            returncode = 0
            stdout = "usage: agy [--output-format {json,stream-json}]"
            stderr = ""

        return _Proc()

    def fail_if_semantic_probe_called(agy_bin: str) -> dict[str, Any]:
        raise AssertionError("semantic probe must not run without cost confirmation")

    monkeypatch.setattr(preflight_agy, "_run_help", fake_run_help)
    monkeypatch.setattr(preflight_agy, "_run_structured_output_semantic_probe", fail_if_semantic_probe_called)
    preflight_agy._STRUCTURED_OUTPUT_CAPABILITY_MEMO_CACHE.clear()

    record = preflight_agy.get_or_compute_structured_output_capability("fake-agy-bin-stage1-only")
    assert record["status"] == "inconclusive"


def test_get_or_compute_structured_output_capability_promotes_to_supported_with_cost_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With cost confirmation set AND a valid Stage-2 semantic probe stream
    from the SAME binary, capability resolution promotes to "supported"."""
    monkeypatch.setenv("AGY_PREFLIGHT_CONFIRM_RUNTIME_PROBE_COST", "1")

    def fake_run_help(agy_bin: str) -> Any:
        class _Proc:
            returncode = 0
            stdout = "usage: agy [--output-format {json,stream-json}]"
            stderr = ""

        return _Proc()

    def fake_semantic_probe(agy_bin: str) -> dict[str, Any]:
        stdout = _make_stream_json_stdout()
        return {
            "argv": [agy_bin, "-p", "x", "--output-format", "stream-json"],
            "exit_code": 0,
            "stdout": stdout,
            "stderr": "",
            "timed_out": False,
            "binary_identity": preflight_agy.compute_binary_identity(agy_bin),
            "parser_verdict": preflight_agy.parse_agy_stream_json_stream(stdout),
        }

    monkeypatch.setattr(preflight_agy, "_run_help", fake_run_help)
    monkeypatch.setattr(preflight_agy, "_run_structured_output_semantic_probe", fake_semantic_probe)
    preflight_agy._STRUCTURED_OUTPUT_CAPABILITY_MEMO_CACHE.clear()

    record = preflight_agy.get_or_compute_structured_output_capability("fake-agy-bin-stage2-supported")
    assert record["status"] == "supported"
