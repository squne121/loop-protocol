"""Tests for agy provider support in run_gemini_headless.py.

Covers AC1-AC14 for provider=agy path. Uses mock subprocess to avoid
requiring the agy CLI to be installed in the test environment.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Module loading helper (hermetic, no side-effects)
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


def _load_agy_advisory_fallback_router() -> types.ModuleType:
    route_path = _SCRIPT_PATH.parents[2] / "issue-refinement-loop" / "scripts" / "route_agy_advisory_fallback.py"
    spec = importlib.util.spec_from_file_location("route_agy_advisory_fallback", route_path)
    assert spec is not None
    router = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(router)  # type: ignore[union-attr]
    return router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["agy", "-p", "test"], returncode=returncode, stdout=stdout, stderr=stderr)


def _valid_hook_event(tool_name: str = "search_web") -> dict[str, Any]:
    """A validated `agy_tool_provenance_v1` PreToolUse hook event fixture
    (Issue #2038 fix_delta iteration 2): the legacy stdout/marker parser now
    requires this corroboration before resolving `grounding_status ==
    "grounded"` -- mirrors test_agy_provenance_grounding_wiring.py's
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
        "conversationId": "conv-2038-fix-delta-test",
        "monotonic_ns": 1,
        "utc": "2026-08-09T00:00:00.000000Z",
    }


def _write_valid_hook_event_for_subprocess_env(kwargs: dict[str, Any], tool_name: str = "search_web") -> None:
    """Append a validated `agy_tool_provenance_v1` PreToolUse hook event line
    to the isolated-workspace hook events log file that this real
    `_run_agy()` invocation's `env` points at (Issue #2038 fix_delta
    iteration 2). Used by `mock_run(cmd, **kwargs)` closures that patch
    `subprocess.run` (rather than `_run_agy` itself) so the real
    hook-loading code path (`agy_tool_provenance.load_hook_events()`) sees
    genuine corroboration -- mirroring what a real PreToolUse wrapper
    script would append."""
    import hashlib
    import json
    from pathlib import Path

    env = kwargs.get("env") or {}
    hook_log_path = env.get("AGY_PROVENANCE_HOOK_LOG_PATH")
    if not hook_log_path:
        return
    path = Path(hook_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "schema": "agy_tool_provenance_v1",
        "version": 1,
        "event": "PreToolUse",
        "toolCall": {
            "name": tool_name,
            "args_sha256": hashlib.sha256(b'{"query":"test"}').hexdigest(),
        },
        "conversationId": "conv-2038-fix-delta-test",
        "monotonic_ns": 1,
        "utc": "2026-08-09T00:00:00.000000Z",
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _agy_request(**kwargs: Any) -> dict[str, Any]:
    """Return a minimal valid agy delegation request."""
    base = {
        "schema": "delegation_request_v1",
        "tool_profile": "no_tools",
        "provider": "agy",
        "prompt": "Return exactly: LOOP_AGY_SMOKE_OK",
        "objective": "Smoke test for agy provider integration",
        "instructions": ["Return exactly: LOOP_AGY_SMOKE_OK", "Do not add any extra text"],
        "output_sections": ["response"],
        "context_files": [],
    }
    base.update(kwargs)
    return base


def _write_serena_manifest(root: Path, pinned_ref: str = "0123456789abcdef") -> None:
    manifest_path = root / rgh.SERENA_TOOL_MANIFEST_RELATIVE_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": "serena_tool_manifest_v1",
        "source": "https://github.com/oraios/serena",
        "pinned_ref": pinned_ref,
        "generated_at_utc": "2026-07-02T00:00:00Z",
        "mcp_command": [
            "uvx",
            "--from",
            f"git+https://github.com/oraios/serena@{pinned_ref}",
            "serena",
            "start-mcp-server",
            "--project-from-cwd",
        ],
        "read_only_allowlist": sorted(rgh.SERENA_READ_ONLY_TOOLS),
        "dangerous_denylist": sorted(rgh.SERENA_DANGEROUS_TOOLS),
        "known_tools": sorted(rgh.SERENA_READ_ONLY_TOOLS | rgh.SERENA_DANGEROUS_TOOLS),
        "notes": [],
    }
    manifest_path.write_text(__import__("json").dumps(manifest), encoding="utf-8")


# ---------------------------------------------------------------------------
# AC1: no_tools profile — agy returns response text + result.json via wrapper
# ---------------------------------------------------------------------------


def test_ac1_no_tools_returns_response_text() -> None:
    """AC1: provider=agy, no_tools -> response text returned, ok=True."""
    completed = _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")
    with patch.object(rgh, "_run_agy", return_value=completed):
        result = rgh.run_delegation(_agy_request(tool_profile="no_tools"))
    assert result["ok"] is True
    assert result["response_text"] == "LOOP_AGY_SMOKE_OK"
    assert result["provider"] == "agy"
    assert result["safety_mode"] == "degraded_wrapper_only"
    assert result["transport"] == "agy"


# ---------------------------------------------------------------------------
# AC2: proposal_only profile — agy returns proposal text + result.json
# ---------------------------------------------------------------------------


def test_ac2_proposal_only_returns_response_text() -> None:
    """AC2: provider=agy, proposal_only -> proposal text returned, ok=True."""
    completed = _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")
    with patch.object(rgh, "_run_agy", return_value=completed):
        result = rgh.run_delegation(_agy_request(tool_profile="proposal_only"))
    assert result["ok"] is True
    assert result["response_text"] == "LOOP_AGY_SMOKE_OK"
    assert result["provider"] == "agy"
    assert result["safety_mode"] == "degraded_wrapper_only"


# ---------------------------------------------------------------------------
# AC6: unknown_provider fails closed
# ---------------------------------------------------------------------------


def test_ac6_unknown_provider_fails_closed() -> None:
    """AC6: provider=unknown -> validation error with unknown_provider."""
    req = _agy_request(provider="unknown_provider_xyz")
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    assert result["failure_reason"] is not None
    assert result["failure_class"] == "unknown_provider"
    assert "unknown_provider" in result["failure_reason"]


def test_ac6_gemini_provider_accepted() -> None:
    """AC6: provider=gemini is valid (default path)."""
    errors = rgh.validate_request(
        {
            "schema": "delegation_request_v1",
            "tool_profile": "no_tools",
            "provider": "gemini",
            "objective": "Test gemini provider validation with enough detail",
            "instructions": ["Step one", "Step two"],
            "output_sections": ["response"],
            "context_files": [],
        }
    )
    # No unknown_provider error should be present
    assert not any("unknown_provider" in e for e in errors)


def test_ac6_missing_provider_defaults_to_gemini() -> None:
    """AC6: provider not specified -> gemini default, no unknown_provider error."""
    errors = rgh.validate_request(
        {
            "schema": "delegation_request_v1",
            "tool_profile": "no_tools",
            "objective": "Test default provider with enough detail here",
            "instructions": ["Step one", "Step two"],
            "output_sections": ["response"],
            "context_files": [],
        }
    )
    assert not any("unknown_provider" in e for e in errors)


# ---------------------------------------------------------------------------
# AC7: unsupported profile for agy fails closed (no fallback to gemini)
# ---------------------------------------------------------------------------


def test_ac7_agy_grounded_research_supported() -> None:
    """AC7: provider=agy + grounded_research is supported and returns websearch evidence.

    Success requires a machine-verifiable `tool_calls` trace with a recognized web tool
    name (e.g. `web_search`) in addition to a structured citation — a bare URL string is
    not sufficient (Issue #1266 Blocker 1). Issue #2038 fix_delta iteration 2: it also
    requires a validated `agy_tool_provenance_v1` hook event corroborating the tool call
    -- the stdout self-report JSON alone is never sufficient on its own (OWNER gate 4).
    """
    captured_timeout: dict[str, int | None] = {"value": None}
    grounded_output = (
        "Response from AGY.\n"
        '{"grounding":{"queries":["AGY WebSearch"],"sources":[{"url":"https://example.com","title":"example"}]},'
        '"tool_calls":[{"name":"web_search"}]}'
    )

    def _run_agy(prompt: str, timeout_sec: int = rgh.DEFAULT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
        captured_timeout["value"] = timeout_sec
        completed = _make_completed(0, stdout=grounded_output)
        completed.agy_provenance_hook_events = [_valid_hook_event()]  # type: ignore[attr-defined]
        completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]
        return completed

    with patch.object(rgh, "_run_agy", side_effect=_run_agy):
        result = rgh.run_delegation(_agy_request(tool_profile="grounded_research", timeout_sec=120))

    assert result["ok"] is True
    assert result["failure_class"] is None
    assert result["provider"] == "agy"
    assert captured_timeout["value"] == 300
    assert result["grounded_research_evidence"] is not None
    evidence = result["grounded_research_evidence"] or {}
    assert evidence["parsed_evidence"].get("source") == "json_line"
    assert isinstance(evidence["parsed_evidence"].get("data"), dict)
    expected_grounding = {
        "queries": ["AGY WebSearch"],
        "sources": [{"url": "https://example.com", "title": "example"}],
    }
    assert evidence["parsed_evidence"]["data"].get("grounding") == expected_grounding
    assert result["grounded_research_evidence"]["grounding_actor"] == "antigravity_cli"
    assert result["grounded_research_evidence"]["grounding_backend"] == "agy_native_websearch"
    assert result["grounded_research_evidence"]["grounding_status"] == "grounded"
    assert result["grounded_research_evidence"]["web_tool_call_count"] == 1
    assert result["grounded_research_evidence"]["url_citation_count"] == 1


def test_agy_grounded_research_forbids_gemini_google_search() -> None:
    """AC4: provider=agy + grounded_research never dispatches through the Gemini CLI/API path.

    Proves behaviorally (not by inspection) that the agy branch of
    run_delegation() returns before reaching _run_gemini() (the Gemini CLI
    subprocess call) or the ACP transport (run_gemini_acp.run_acp()). There is
    no Gemini API-level ``google_search`` / ``GenerationConfig`` grounding
    tool constructed anywhere in run_gemini_headless.py; the only grounding
    surface for provider=agy is agy's own native WebSearch via ``_run_agy``.
    """
    # Includes a machine-verifiable tool_calls trace (Issue #1266 Blocker 1) AND a
    # corroborating validated hook event (Issue #2038 fix_delta iteration 2) so this
    # AC4 test's ok=True assertion reflects a genuine grounded result, not a bare URL
    # scan or an unverified model self-report.
    grounded_output = (
        "Response from AGY.\n"
        '{"grounding":{"queries":["AGY WebSearch"],"sources":[{"url":"https://example.com","title":"example"}]},'
        '"tool_calls":[{"name":"web_search"}]}'
    )
    completed = _make_completed(0, stdout=grounded_output)
    completed.agy_provenance_hook_events = [_valid_hook_event()]  # type: ignore[attr-defined]
    completed.agy_provenance_hook_load_error = None  # type: ignore[attr-defined]
    with (
        patch.object(rgh, "_run_agy", return_value=completed) as mock_agy,
        patch.object(
            rgh,
            "_run_gemini",
            side_effect=AssertionError("_run_gemini (Gemini CLI/API path) must not be called for provider=agy"),
        ),
    ):
        result = rgh.run_delegation(_agy_request(tool_profile="grounded_research", timeout_sec=300))

    mock_agy.assert_called_once()
    assert result["ok"] is True
    assert result["provider"] == "agy"
    assert result["grounded_research_evidence"] is not None


def test_agy_grounded_research_no_citation_fail_closed() -> None:
    """provider=agy + grounded_research without a tool-call trace is fail-closed at both
    nested evidence and top-level result (Issue #1266 Blocker 1 / Blocker 2)."""
    result = rgh._normalize_agy_result(
        _make_completed(0, stdout="Grounded answer without a citation URL."),
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_status"] == "attempted_no_web_tool_call"
    assert evidence["grounding_failure_class"] == "agy_web_grounding_tool_call_missing"
    assert evidence["url_citation_count"] == 0
    assert result["ok"] is False
    assert result["failure_class"] == "agy_web_grounding_tool_call_missing"


def test_agy_grounded_research_no_web_tool_call_fail_closed() -> None:
    """provider=agy + grounded_research exposes missing web tool calls as fail-closed metadata
    at both nested evidence and top-level result (Issue #1266 Blocker 2)."""
    result = rgh._normalize_agy_result(
        _make_completed(0, stdout="No web tool call evidence."),
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["web_tool_call_count"] == 0
    assert evidence["grounding_failure_class"] == "agy_web_grounding_tool_call_missing"
    assert result["ok"] is False
    assert result["failure_class"] == "agy_web_grounding_tool_call_missing"


def test_agy_grounded_research_url_without_tool_trace_is_candidate_for_native_check() -> None:
    """A URL without provider telemetry remains a candidate, not a grounded success.

    The web-researcher must evaluate its source content or use native Web fallback;
    `web_tool_call_count == 0` does not erase the URL or become the verdict gate.
    """
    result = rgh._normalize_agy_result(
        _make_completed(0, stdout="Here is a helpful link: https://example.com/article"),
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_backend"] == "agy_final_result"
    assert evidence["grounding_status"] == "citation_candidates_unverified"
    assert evidence["grounding_failure_class"] == "agy_evidence_quality_unverified"
    assert evidence["web_tool_call_count"] == 0
    assert evidence["url_citation_count"] == 1
    assert result["ok"] is False
    assert result["failure_class"] == "agy_evidence_quality_unverified"


def test_agy_grounded_research_prompt_echo_url_not_counted_as_citation() -> None:
    """A URL that only appears because AGY echoed the prompt back must not be counted as a
    citation, and without a tool-call trace the result stays fail-closed (Issue #1266 Major 3
    test #2)."""
    prompt_echo_stdout = (
        "You asked: Search for: latest reliable news and return exactly one source URL.\n"
        "I cannot access the web right now."
    )
    result = rgh._normalize_agy_result(
        _make_completed(0, stdout=prompt_echo_stdout),
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["web_tool_call_count"] == 0
    assert evidence["url_citation_count"] == 0
    assert evidence["grounding_failure_class"] == "agy_web_grounding_tool_call_missing"
    assert result["ok"] is False


def test_agy_grounded_research_secret_like_token_redaction_fail_closed() -> None:
    """A secret-like token in AGY stdout is fail-closed via agy_web_grounding_redaction_failed
    and never emitted into the evidence excerpt (Issue #1266 Blocker 3 / Major 3 test #3)."""
    leaking_stdout = "Debug token: ghp_" + ("a" * 36) + " while browsing https://example.com"
    result = rgh._normalize_agy_result(
        _make_completed(0, stdout=leaking_stdout),
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_failure_class"] == "agy_web_grounding_redaction_failed"
    assert evidence["redaction_status"] == "redaction_failed"
    assert evidence["raw_credential_included"] is True
    for entry in evidence["grounding_transcript_evidence"]:
        assert "ghp_" + ("a" * 36) not in entry["excerpt"]
    assert result["ok"] is False
    assert result["failure_class"] == "agy_web_grounding_redaction_failed"


def test_agy_grounded_research_repo_absolute_path_redaction_fail_closed() -> None:
    """A repo absolute path in AGY stdout is fail-closed via agy_web_grounding_redaction_failed
    (Issue #1266 Blocker 3 / Major 3 test #3)."""
    repo_root = str(rgh._repo_root())
    leaking_stdout = f"Reading file at {repo_root}/secret_notes.md"
    result = rgh._normalize_agy_result(
        _make_completed(0, stdout=leaking_stdout),
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_failure_class"] == "agy_web_grounding_redaction_failed"
    assert evidence["repo_absolute_path_included"] is True
    for entry in evidence["grounding_transcript_evidence"]:
        assert repo_root not in entry["excerpt"]
    assert result["ok"] is False


def test_agy_grounded_research_quota_exhausted_stderr_signal_fail_closed() -> None:
    """RESOURCE_EXHAUSTED / HTTP 429 signals in stdout are classified as
    agy_web_grounding_quota_exhausted, not a generic no-citation failure (Issue #1266 Major 1 /
    Major 3 test #4)."""
    result = rgh._normalize_agy_result(
        _make_completed(0, stdout="RESOURCE_EXHAUSTED: Individual quota reached for WebSearch tool."),
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_failure_class"] == "agy_web_grounding_quota_exhausted"
    assert result["ok"] is False
    assert result["failure_class"] == "agy_web_grounding_quota_exhausted"


def test_agy_grounded_research_capability_missing_fail_closed() -> None:
    """provider=agy + grounded_research keeps capability-missing evidence non-grounded at both
    nested evidence and top-level result (Issue #1266 Blocker 2)."""
    result = rgh._normalize_agy_result(
        _make_completed(0, stdout="WebSearch capability unavailable."),
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_backend"] == "none"
    assert evidence["grounding_failure_class"] == "agy_web_grounding_tool_call_missing"
    assert result["ok"] is False
    assert result["failure_class"] == "agy_web_grounding_tool_call_missing"


def test_agy_grounded_research_quota_exhausted_fail_closed() -> None:
    """provider=agy + grounded_research classifies quota exhaustion text as
    agy_web_grounding_quota_exhausted (not a generic no-citation failure), fail-closed at both
    nested evidence and top-level result (Issue #1266 Major 1 / Blocker 2)."""
    result = rgh._normalize_agy_result(
        _make_completed(0, stdout="quota exhausted before WebSearch citation generation."),
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_failure_class"] == "agy_web_grounding_quota_exhausted"
    assert result["ok"] is False
    assert result["failure_class"] == "agy_web_grounding_quota_exhausted"


def test_agy_grounded_research_redacts_evidence_envelope() -> None:
    """agy_grounded_research_redaction_status: evidence envelope excludes raw transcript and
    credentials, using the contract's checked_no_secret_pattern literal (Issue #1266 Blocker 3)."""
    result = rgh._normalize_agy_result(
        _make_completed(0, stdout="Source https://example.com"),
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["raw_transcript_included"] is False
    assert evidence["raw_credential_included"] is False
    assert evidence["repo_absolute_path_included"] is False
    assert evidence["redaction_status"] == "checked_no_secret_pattern"
    failure_class = evidence["grounding_failure_class"]
    if failure_class:
        assert "agy_web_grounding_parse_error" not in failure_class


def test_ac7_agy_local_asset_research_rejected() -> None:
    """AC7: provider=agy with local_asset_research requires local_asset_research context files.

    Issue #1692 AC12: _validate_agy_local_asset_request() no longer delegates
    to the Gemini-only validate_request() (which produced the generic
    "context_files must contain at least 1 item(s)" message); the
    local_asset_research-specific check now produces its own dedicated
    message.
    """
    req = _agy_request(tool_profile="local_asset_research")
    with patch.object(rgh, "_validate_local_asset_research_settings", lambda: []):  # type: ignore[call-arg]
        result = rgh.run_delegation(req)
    assert result["ok"] is False
    assert result["failure_reason"].startswith("local_asset_research requires at least one context file")
    assert result["failure_class"].startswith("local_asset_research requires at least one context file")


def test_ac7_agy_local_asset_research_success_with_wrapper_validation(tmp_path, monkeypatch) -> None:
    """AC7: provider=agy + local_asset_research succeeds after wrapper-side validation."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    context_file = repo_root / "context.md"
    context_file.write_text("local asset content", encoding="utf-8")
    _write_serena_manifest(repo_root)

    monkeypatch.setattr(rgh, "_repo_root", lambda: repo_root)  # type: ignore[call-arg]
    monkeypatch.setattr(rgh, "_validate_local_asset_research_settings", lambda: [])  # type: ignore[call-arg]

    captured_prompt: dict[str, str] = {}

    def _fake_live_evidence(context_paths, root, manifest, *, deadline_monotonic=None):
        evidence_records = [
            {
                "tool_name": "find_file",
                "query": {"file_mask": "context.md"},
                "repo_relative_path": "context.md",
                "line_range": [1, 1],
                "content_snippet": "context.md",
                "byte_size": 10,
                "sha256": "0" * 64,
                "redaction_status": "checked_no_credential_pattern",
                "manifest_id": "serena_tool_manifest_v1:0123456789abcdef",
                "source_kind": "serena_mcp_read_only_evidence",
            },
            {
                "tool_name": "search_for_pattern",
                "query": {"substring_pattern": "local_asset_research"},
                "repo_relative_path": "context.md",
                "line_range": [1, 1],
                "content_snippet": "local asset content",
                "byte_size": 19,
                "sha256": "1" * 64,
                "redaction_status": "checked_no_credential_pattern",
                "manifest_id": "serena_tool_manifest_v1:0123456789abcdef",
                "source_kind": "serena_mcp_read_only_evidence",
            },
            {
                "tool_name": "get_symbols_overview",
                "query": {"relative_path": "context.md"},
                "repo_relative_path": "context.md",
                "line_range": [1, 1],
                "content_snippet": "[]",
                "byte_size": 2,
                "sha256": "2" * 64,
                "redaction_status": "checked_no_credential_pattern",
                "manifest_id": "serena_tool_manifest_v1:0123456789abcdef",
                "source_kind": "serena_mcp_read_only_evidence",
            },
        ]
        return {
            "status": "success",
            "context_text": "local asset context prepared",
            "evidence_document": __import__("json").dumps(
                {
                    "schema": "wrapper_serena_evidence_v1",
                    "evidence": evidence_records,
                },
                ensure_ascii=False,
            ),
            "evidence": {"source_kind": "serena_mcp_read_only_evidence"},
        }

    def _run_agy(prompt: str, timeout_sec: int = rgh.DEFAULT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
        captured_prompt["value"] = prompt
        return _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")

    req = _agy_request(
        tool_profile="local_asset_research",
        context_files=["context.md"],
        prompt="Summarize local repository evidence.",
    )
    with (
        patch.object(rgh, "_run_agy", side_effect=_run_agy),
        patch.object(
            rgh,
            "_collect_live_serena_read_only_evidence",
            side_effect=_fake_live_evidence,
        ),
    ):
        result = rgh.run_delegation(req, request_path=repo_root / "request.json")

    assert result["ok"] is True
    assert result["response_text"] == "LOOP_AGY_SMOKE_OK"
    assert result["provider"] == "agy"
    assert result["safety_mode"] == "degraded_wrapper_only"
    assert "AGY is executed in prompt-only wrapper-side evidence mode" in captured_prompt["value"]
    assert "BEGIN LOCAL ASSET EVIDENCE: context.md" in captured_prompt["value"]
    assert '"repo_relative_path": "context.md"' in captured_prompt["value"]
    assert '"source_kind": "serena_mcp_read_only_evidence"' in captured_prompt["value"]
    assert '"tool_name": "find_file"' in captured_prompt["value"]
    assert '"tool_name": "search_for_pattern"' in captured_prompt["value"]
    assert '"tool_name": "get_symbols_overview"' in captured_prompt["value"]
    assert "wrapper_serena_context_file" not in captured_prompt["value"]
    assert str(repo_root) not in captured_prompt["value"]
    assert "mcpServers" not in captured_prompt["value"]
    assert "Operator objective:" in captured_prompt["value"]


def test_ac7_context_file_test_double_does_not_claim_live_serena(tmp_path, monkeypatch) -> None:
    """AC7: direct context-file fallback evidence must not use live MCP source_kind."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    context_file = repo_root / "context.md"
    context_file.write_text("local asset content", encoding="utf-8")
    _write_serena_manifest(repo_root)
    manifest = rgh.load_serena_tool_manifest(repo_root)

    documents = rgh._collect_serena_read_only_evidence([context_file], repo_root, manifest)

    assert documents
    assert "serena_mcp_test_double_evidence" in documents[0]["content"]
    assert "serena_mcp_read_only_evidence" not in documents[0]["content"]


def test_ac7_agy_local_asset_research_rejects_context_outside_repo_before_read(tmp_path, monkeypatch) -> None:
    """AC7: outside-repo local_asset_research context is rejected before payload read."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_serena_manifest(repo_root)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    monkeypatch.setattr(rgh, "_repo_root", lambda: repo_root)  # type: ignore[call-arg]
    monkeypatch.setattr(rgh, "_validate_local_asset_research_settings", lambda: [])  # type: ignore[call-arg]

    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == outside:
            raise AssertionError("outside repo context must not be read")
        return original_read_text(path, *args, **kwargs)

    with patch.object(Path, "read_text", guarded_read_text):
        result = rgh.run_delegation(
            _agy_request(tool_profile="local_asset_research", context_files=[str(outside)]),
            request_path=repo_root / "request.json",
        )

    assert result["ok"] is False
    assert result["failure_reason"].startswith("local_asset_research context file must be inside repository")
    assert result["failure_class"] == "local_asset_research context file must be inside repository"


def test_issue_2434_global_local_asset_payload_limits_are_intentional(tmp_path) -> None:
    """Issue #2434 intentionally expands the global producer payload contract.

    These limits belong to ``run_gemini_headless.py``'s existing
    ``local_asset_research`` producer boundary, not only to the new
    issue-refinement controller. Keep the contract at 32 files, 1 MiB per
    file, and 4 MiB aggregate so consumers of the pre-existing route observe
    the same explicit behavior as the controller.
    """
    assert rgh.LOCAL_ASSET_MAX_CONTEXT_FILES == 32
    assert rgh.LOCAL_ASSET_MAX_CONTEXT_BYTES == 1024 * 1024
    assert rgh.LOCAL_ASSET_MAX_CONTEXT_TOTAL_BYTES == 4 * 1024 * 1024

    exact_limit_paths = []
    for index in range(4):
        path = tmp_path / f"context-{index}.txt"
        path.write_bytes(b"x" * rgh.LOCAL_ASSET_MAX_CONTEXT_BYTES)
        exact_limit_paths.append(path)
    assert rgh._validate_agy_local_asset_payload_bounds(exact_limit_paths) == []

    over_limit = tmp_path / "over-limit.txt"
    over_limit.write_bytes(b"x" * (rgh.LOCAL_ASSET_MAX_CONTEXT_BYTES + 1))
    errors = rgh._validate_agy_local_asset_payload_bounds([over_limit])
    assert any("context file is too large" in error for error in errors)


def test_ac7_agy_github_research_dispatches_to_e2e_route(monkeypatch, tmp_path) -> None:
    """AC7 (superseded by Issue #1920): provider=agy + github_research is now
    implemented, dispatched entirely to run_agy_github_research_e2e.py's
    bounded, broker-backed route -- it is no longer unsupported_provider_profile.
    Without a live agy CLI / GH_TOKEN in this hermetic test environment, the
    route SKIPs (exit_code 77) fail-closed rather than reporting a fabricated
    PASS or falling back to Gemini; see test_agy_github_research_contract.py
    and test_agy_github_research_e2e.py for the full contract.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    req = _agy_request(tool_profile="github_research")
    result = rgh.run_delegation(req)
    assert result["tool_profile"] == "github_research"
    assert result["failure_class"] != "unsupported_provider_profile"
    assert result["exit_code"] in (0, 77)
    if result["exit_code"] == 77:
        assert result["ok"] is False
        assert result["failure_class"] == "github_research_skip"


# ---------------------------------------------------------------------------
# AC8: _normalize_agy_result does NOT call _parse_envelope
# ---------------------------------------------------------------------------


def test_ac8_normalize_agy_skips_parse_envelope() -> None:
    """AC8: _normalize_agy_result exists and doesn't call _parse_envelope."""
    # Ensure _normalize_agy_result is a function in the module
    assert callable(getattr(rgh, "_normalize_agy_result", None))

    # Call directly with a mock completed process — _parse_envelope should not be called
    completed = _make_completed(0, stdout="plain text response")
    with patch.object(rgh, "_parse_envelope", side_effect=AssertionError("_parse_envelope must not be called for agy")):
        result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["ok"] is True
    assert result["response_text"] == "plain text response"


# ---------------------------------------------------------------------------
# AC9: agy exit 0 + empty stdout -> agy_output_missing / agy_empty_stdout
# ---------------------------------------------------------------------------


def test_ac9_exit0_empty_stdout_fails_closed() -> None:
    """AC9: provider=agy, exit 0, empty stdout -> fail with agy_empty_stdout."""
    with patch.dict(os.environ, {"CI": ""}, clear=False):
        completed = _make_completed(0, stdout="")
        result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["ok"] is False
    assert result["failure_reason"] == "agy_empty_stdout"
    assert result["failure_class"] == "agy_empty_stdout"


def test_ac9_exit0_whitespace_only_stdout_fails_closed() -> None:
    """AC9: provider=agy, exit 0, whitespace-only stdout -> fail with agy_empty_stdout."""
    with patch.dict(os.environ, {"CI": ""}, clear=False):
        completed = _make_completed(0, stdout="   \n  ")
        result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["ok"] is False
    assert result["failure_reason"] == "agy_empty_stdout"


def test_ac9_exit0_empty_stdout_in_ci_uses_output_missing() -> None:
    """AC9: provider=agy, CI 環境の empty stdout は agy_output_missing に揃える。"""
    with patch.dict(os.environ, {"CI": "1"}, clear=False):
        completed = _make_completed(0, stdout="")
        result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["ok"] is False
    assert result["failure_reason"] == "agy_output_missing"
    assert result["failure_class"] == "agy_output_missing"


# ---------------------------------------------------------------------------
# AC10: raw_command sanitization — no prompt text, absolute paths, or secrets
# ---------------------------------------------------------------------------


def test_ac10_raw_command_sanitized() -> None:
    """AC10: _build_agy_raw_command returns sanitized placeholder."""
    cmd = rgh._build_agy_raw_command("secret prompt with /absolute/path and token=ghp_abc123")
    assert cmd[0] in ("agy", "antigravity")  # basename only
    assert cmd[1] == "-p"
    assert cmd[2] == "<prompt>"  # placeholder, not actual prompt
    assert "secret" not in " ".join(cmd)
    assert "/absolute/path" not in " ".join(cmd)
    assert "ghp_abc123" not in " ".join(cmd)


def test_ac10_raw_command_uses_agy_bin_basename_only() -> None:
    """AC10: AGY_BIN with absolute path -> only basename in raw_command."""
    original = os.environ.get("AGY_BIN")
    try:
        os.environ["AGY_BIN"] = "/usr/local/bin/custom-agy"
        cmd = rgh._build_agy_raw_command("test")
        assert "/" not in cmd[0]
        assert cmd[0] == "custom-agy"
    finally:
        if original is None:
            os.environ.pop("AGY_BIN", None)
        else:
            os.environ["AGY_BIN"] = original


# ---------------------------------------------------------------------------
# AC11: post_to_issue_url forbidden for all agy profiles
# ---------------------------------------------------------------------------


def test_ac11_agy_no_tools_forbids_post_to_issue_url() -> None:
    """AC11: provider=agy, no_tools, post_to_issue_url -> provider_forbids_post_to_issue_url."""
    req = _agy_request(
        tool_profile="no_tools",
        post_to_issue_url="https://github.com/owner/repo/issues/1",
    )
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    assert result["failure_class"] == "provider_forbids_post_to_issue_url"


def test_ac11_agy_proposal_only_forbids_post_to_issue_url() -> None:
    """AC11: provider=agy, proposal_only, post_to_issue_url -> provider_forbids_post_to_issue_url."""
    req = _agy_request(
        tool_profile="proposal_only",
        post_to_issue_url="https://github.com/owner/repo/issues/1",
    )
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    assert result["failure_class"] == "provider_forbids_post_to_issue_url"


def test_agy_model_rejection_sets_failure_class() -> None:
    """provider=agy で explicit model は unsupported_provider_option を返す。"""
    result = rgh.run_delegation(_agy_request(model="gemini-3-pro"))
    assert result["ok"] is False
    assert result["failure_class"] == "unsupported_provider_option"


def test_agy_empty_prompt_sets_failure_class() -> None:
    """provider=agy で空 prompt は agy_empty_prompt を返す。"""
    result = rgh.run_delegation(_agy_request(prompt="   "))
    assert result["ok"] is False
    assert result["failure_class"] == "agy_empty_prompt"


# ---------------------------------------------------------------------------
# AC12: result contains provider="agy" and safety_mode="degraded_wrapper_only"
# ---------------------------------------------------------------------------


def test_ac12_result_contains_provider_and_safety_mode_on_success() -> None:
    """AC12: ok result includes provider=agy and safety_mode=degraded_wrapper_only."""
    completed = _make_completed(0, stdout="response text")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["provider"] == "agy"
    assert result["safety_mode"] == "degraded_wrapper_only"


def test_ac12_result_contains_provider_and_safety_mode_on_failure() -> None:
    """AC12: failure result also includes provider=agy and safety_mode=degraded_wrapper_only."""
    completed = _make_completed(1, stdout="", stderr="some error")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["provider"] == "agy"
    assert result["safety_mode"] == "degraded_wrapper_only"
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# AC13: shell=False, isolated cwd, minimal env, AGY_BIN override
# ---------------------------------------------------------------------------


def test_ac13_run_agy_uses_shell_false_and_minimal_env() -> None:
    """AC13: _run_agy uses shell=False with minimal env."""
    captured_kwargs: dict[str, Any] = {}

    _original_run = subprocess.run

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_kwargs.update(kwargs)
        return _make_completed(0, stdout="ok")

    with patch("subprocess.run", side_effect=mock_run):
        rgh._run_agy("test prompt", 30)

    # shell=False (default when not specified, but must not be True)
    assert (
        captured_kwargs.get("shell") is False or "shell" not in captured_kwargs or captured_kwargs.get("shell") is False
    )
    # env must be present and minimal
    env = captured_kwargs.get("env")
    assert env is not None, "env must be explicitly set (minimal env required)"
    # Must NOT contain sensitive env vars like GEMINI_API_KEY
    assert "GEMINI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    # cwd must be set to a temp directory
    cwd = captured_kwargs.get("cwd")
    assert cwd is not None


def test_ac13_agy_bin_override() -> None:
    """AC13: AGY_BIN env var overrides the agy binary path."""
    captured_cmd: list[Any] = []

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd.extend(cmd)
        return _make_completed(0, stdout="ok")

    original = os.environ.get("AGY_BIN")
    try:
        os.environ["AGY_BIN"] = "/custom/path/to/my-agy"
        with patch("subprocess.run", side_effect=mock_run):
            rgh._run_agy("test", 30)
    finally:
        if original is None:
            os.environ.pop("AGY_BIN", None)
        else:
            os.environ["AGY_BIN"] = original

    # The actual binary path (not basename) is used for execution
    assert captured_cmd[0] == "/custom/path/to/my-agy"


def test_ac13_minimal_agy_env_allowlist() -> None:
    """AC13: _minimal_agy_env only includes allowlisted keys."""
    env = rgh._minimal_agy_env()
    # Must be a dict
    assert isinstance(env, dict)
    allowed_keys = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"}
    for key in env:
        assert key in allowed_keys, f"unexpected env key: {key!r}"


# ---------------------------------------------------------------------------
# AC14: model specification rejected for agy provider
# ---------------------------------------------------------------------------


def test_ac14_agy_with_model_rejected() -> None:
    """AC14: provider=agy with explicit model -> unsupported_provider_option error."""
    req = _agy_request(model="gemini-3-flash-preview")
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    failure = result.get("failure_reason") or ""
    assert "unsupported_provider_option" in failure


def test_ac14_agy_without_model_accepted() -> None:
    """AC14: provider=agy without model -> no unsupported_provider_option error."""
    completed = _make_completed(0, stdout="test response")
    with patch.object(rgh, "_run_agy", return_value=completed):
        result = rgh.run_delegation(_agy_request())
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


def test_agy_exit_nonzero_returns_failure() -> None:
    """agy exit non-0 -> ok=False with agy_exit_nonzero failure class."""
    completed = _make_completed(1, stdout="", stderr="agy error message")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["ok"] is False
    assert result["failure_class"] == "agy_exit_nonzero"
    assert "agy_exit_nonzero" in result["failure_reason"]


def test_agy_result_surface_populated_on_success() -> None:
    """result_surface is properly populated for agy success."""
    completed = _make_completed(0, stdout="Hello from agy")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["ok"] is True
    rs = result.get("result_surface", {})
    assert rs.get("mode") == "artifact-first"
    assert rs.get("primary_artifact_type") == "inline_response_text"


def test_agy_no_tools_run_delegation_integration() -> None:
    """Full run_delegation path for provider=agy, no_tools profile."""
    completed = _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")
    with patch.object(rgh, "_run_agy", return_value=completed) as mock_run:
        result = rgh.run_delegation(_agy_request(tool_profile="no_tools"))
    mock_run.assert_called_once()
    assert result["ok"] is True
    assert result["response_text"] == "LOOP_AGY_SMOKE_OK"
    assert result["provider"] == "agy"
    assert result["safety_mode"] == "degraded_wrapper_only"
    assert result["transport"] == "agy"


def test_agy_proposal_only_run_delegation_integration() -> None:
    """Full run_delegation path for provider=agy, proposal_only profile."""
    completed = _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")
    with patch.object(rgh, "_run_agy", return_value=completed) as mock_run:
        result = rgh.run_delegation(_agy_request(tool_profile="proposal_only"))
    mock_run.assert_called_once()
    assert result["ok"] is True
    assert result["response_text"] == "LOOP_AGY_SMOKE_OK"


# ---------------------------------------------------------------------------
# Fix 4: additional edge case tests (empty prompt, invalid timeout, exception classes)
# ---------------------------------------------------------------------------


def test_agy_empty_prompt_fails_closed() -> None:
    """Fix4/AC: provider=agy with empty prompt -> agy_empty_prompt fail-closed."""
    req = _agy_request(prompt="")
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    failure = result.get("failure_reason") or ""
    assert "agy_empty_prompt" in failure


def test_agy_whitespace_only_prompt_fails_closed() -> None:
    """Fix4/AC: provider=agy with whitespace-only prompt -> agy_empty_prompt fail-closed."""
    req = _agy_request(prompt="   \n  ")
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    failure = result.get("failure_reason") or ""
    assert "agy_empty_prompt" in failure


def test_agy_none_prompt_fails_closed() -> None:
    """Fix4/AC: provider=agy with prompt=None -> agy_empty_prompt fail-closed."""
    req = _agy_request()
    req["prompt"] = None  # type: ignore[assignment]
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    failure = result.get("failure_reason") or ""
    assert "agy_empty_prompt" in failure


def test_agy_invalid_timeout_falls_back_to_default() -> None:
    """Fix4: timeout_sec='abc' -> falls back to DEFAULT_TIMEOUT_SEC, no uncaught ValueError."""
    completed = _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")
    with patch.object(rgh, "_run_agy", return_value=completed) as mock_run:
        result = rgh.run_delegation(_agy_request(timeout_sec="abc"))
    # Should not raise ValueError; result must be ok
    assert result["ok"] is True
    mock_run.assert_called_once()
    # timeout passed to _run_agy must be the default integer value
    call_args = mock_run.call_args
    actual_timeout = call_args[0][1] if call_args[0] else call_args[1].get("timeout_sec")
    assert isinstance(actual_timeout, int)
    assert actual_timeout == rgh.DEFAULT_TIMEOUT_SEC


def test_agy_timeout_expired_returns_failure_class() -> None:
    """Fix4: subprocess.TimeoutExpired -> failure_class='agy_timeout'."""
    with patch.object(rgh, "_run_agy", side_effect=subprocess.TimeoutExpired(cmd="agy", timeout=30)):
        result = rgh.run_delegation(_agy_request())
    assert result["ok"] is False
    assert result.get("failure_class") == "agy_timeout"
    assert "agy_timeout" in (result.get("failure_reason") or "")


def test_agy_file_not_found_returns_failure_class() -> None:
    """Fix4: FileNotFoundError -> failure_class='agy_not_found'."""
    with patch.object(rgh, "_run_agy", side_effect=FileNotFoundError("agy not found")):
        result = rgh.run_delegation(_agy_request())
    assert result["ok"] is False
    assert result.get("failure_class") == "agy_not_found"
    assert result.get("agy_failure_kind") == "operational"
    assert "agy_not_found" in (result.get("failure_reason") or "")


# ---------------------------------------------------------------------------
# Issue #2434: producer-owned AGY failure discriminator
# ---------------------------------------------------------------------------


def test_agy_success_has_null_producer_failure_kind() -> None:
    completed = _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")
    with patch.object(rgh, "_run_agy", return_value=completed):
        result = rgh.run_delegation(_agy_request())

    assert result["ok"] is True
    assert result["agy_failure_kind"] is None


def test_agy_operational_failure_has_producer_failure_kind() -> None:
    with patch.object(rgh, "_run_agy", side_effect=subprocess.TimeoutExpired(cmd="agy", timeout=30)):
        result = rgh.run_delegation(_agy_request())

    assert result["failure_class"] == "agy_timeout"
    assert result["agy_failure_kind"] == "operational"


def test_agy_policy_or_permission_failure_has_producer_failure_kind() -> None:
    completed = _make_completed(1, stderr="permission denied")
    with patch.object(rgh, "_run_agy", return_value=completed):
        result = rgh.run_delegation(_agy_request())

    assert result["failure_class"] == "agy_permission_denied"
    assert result["agy_failure_kind"] == "policy_or_permission"


def test_agy_contract_failure_has_producer_failure_kind() -> None:
    result = rgh.run_delegation(_agy_request(prompt=""))

    assert result["failure_class"] == "agy_empty_prompt"
    assert result["agy_failure_kind"] == "contract"


def test_unknown_agy_failure_class_is_conservatively_contract() -> None:
    assert rgh._agy_failure_kind("agy_future_unclassified") == "contract"


def test_canonical_agy_failure_kind_rejects_unknown_pair_class() -> None:
    assert rgh.canonical_agy_failure_kind("agy_timeout") == "operational"
    assert rgh.canonical_agy_failure_kind("agy_permission_denied") == "policy_or_permission"
    assert rgh.canonical_agy_failure_kind("agy_empty_prompt") == "contract"
    assert rgh.canonical_agy_failure_kind("agy_future_unclassified") is None
    assert rgh.canonical_agy_failure_kind("request_policy_denied") is None


def test_agy_invocation_attempted_is_false_before_subprocess() -> None:
    # An invalid request stops before the producer-owned direct subprocess
    # boundary, rather than merely reporting a false ContextVar afterwards.
    with patch("subprocess.run", side_effect=AssertionError("pre-AGY request must not spawn a subprocess")):
        result = rgh.run_delegation(_agy_request(prompt=""))

    assert result["ok"] is False
    assert result["agy_invocation_attempted"] is False


def test_agy_invocation_attempted_failure_is_true_after_direct_subprocess_boundary(
    monkeypatch, tmp_path: Path
) -> None:
    fake_agy_bin = "/hermetic/agy-ac10-timing"
    fixed_prompt = "AC10 timing failure invocation"
    fake_workspace = types.SimpleNamespace(
        env={}, workspace_dir=tmp_path, agy_oauth_token_bwrap_prefix=None
    )
    observed_argvs: list[list[Any]] = []
    monkeypatch.setenv("AGY_BIN", fake_agy_bin)
    attempt_token = rgh._AGY_INVOCATION_ATTEMPTED_CTX.set(False)

    def fake_run(cmd: Any, **_kwargs: Any) -> subprocess.CompletedProcess:
        assert isinstance(cmd, list)
        actual_argv = list(cmd)
        observed_argvs.append(actual_argv)
        assert actual_argv == [fake_agy_bin, "-p", fixed_prompt]
        assert rgh._AGY_INVOCATION_ATTEMPTED_CTX.get() is True
        return _make_completed(1, stderr="simulated process failure")

    try:
        assert rgh._AGY_INVOCATION_ATTEMPTED_CTX.get() is False
        with patch.object(
            rgh._agy_permission_policy, "materialize_isolated_agy_workspace", return_value=fake_workspace
        ):
            with patch("shutil.rmtree"), patch("subprocess.run", side_effect=fake_run):
                result = rgh.run_delegation(_agy_request(prompt=fixed_prompt))
    finally:
        rgh._AGY_INVOCATION_ATTEMPTED_CTX.reset(attempt_token)

    assert observed_argvs and len(observed_argvs) == 1
    assert result["ok"] is False
    assert result["agy_invocation_attempted"] is True
    assert result["agy_failure_kind"] == "operational"
    assert rgh.canonical_agy_failure_kind(result["failure_class"]) == "operational"


def test_agy_invocation_attempted_becomes_true_at_direct_subprocess_boundary(
    monkeypatch, tmp_path: Path
) -> None:
    fake_agy_bin = "/hermetic/agy-ac10-timing"
    fixed_prompt = "AC10 timing success invocation"
    fake_workspace = types.SimpleNamespace(
        env={}, workspace_dir=tmp_path, agy_oauth_token_bwrap_prefix=None
    )
    observed_argvs: list[list[Any]] = []
    monkeypatch.setenv("AGY_BIN", fake_agy_bin)
    attempt_token = rgh._AGY_INVOCATION_ATTEMPTED_CTX.set(False)

    def fake_run(cmd: Any, **_kwargs: Any) -> subprocess.CompletedProcess:
        assert isinstance(cmd, list)
        actual_argv = list(cmd)
        observed_argvs.append(actual_argv)
        assert actual_argv == [fake_agy_bin, "-p", fixed_prompt]
        assert rgh._AGY_INVOCATION_ATTEMPTED_CTX.get() is True
        return _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")

    try:
        assert rgh._AGY_INVOCATION_ATTEMPTED_CTX.get() is False
        with patch.object(
            rgh._agy_permission_policy, "materialize_isolated_agy_workspace", return_value=fake_workspace
        ):
            with patch("shutil.rmtree"), patch("subprocess.run", side_effect=fake_run):
                result = rgh.run_delegation(_agy_request(prompt=fixed_prompt))
    finally:
        rgh._AGY_INVOCATION_ATTEMPTED_CTX.reset(attempt_token)

    assert observed_argvs and len(observed_argvs) == 1
    assert result["ok"] is True
    assert result["agy_invocation_attempted"] is True
    assert result["agy_failure_kind"] is None


def test_agy_minimal_env_direct_subprocess_success_marks_attempted_with_exact_argv(monkeypatch) -> None:
    """The no-tool-profile branch marks before its exact direct AGY argv runs."""
    fake_agy_bin = "/hermetic/agy-direct-success"
    fixed_prompt = "direct minimal-env success"
    observed_argvs: list[list[Any]] = []
    monkeypatch.setenv("AGY_BIN", fake_agy_bin)
    attempt_token = rgh._AGY_INVOCATION_ATTEMPTED_CTX.set(False)
    profile_token = rgh._AGY_TOOL_PROFILE_CTX.set(None)

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        actual_argv = list(cmd)
        observed_argvs.append(actual_argv)
        assert actual_argv == [fake_agy_bin, "-p", fixed_prompt]
        assert kwargs["env"] == {"PATH": "/minimal-agy-path"}
        assert rgh._AGY_INVOCATION_ATTEMPTED_CTX.get() is True
        return _make_completed(0, stdout="direct AGY success")

    try:
        assert rgh._AGY_INVOCATION_ATTEMPTED_CTX.get() is False
        with (
            patch.object(rgh, "_minimal_agy_env", return_value={"PATH": "/minimal-agy-path"}) as minimal_env,
            patch.object(rgh, "_AGY_PROVENANCE_AVAILABLE", False),
            patch("subprocess.run", side_effect=fake_run),
        ):
            completed = rgh._run_agy(fixed_prompt, 30)
        result = rgh._attach_agy_failure_kind(
            {"provider": "agy"},
            rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None),
        )
    finally:
        rgh._AGY_TOOL_PROFILE_CTX.reset(profile_token)
        rgh._AGY_INVOCATION_ATTEMPTED_CTX.reset(attempt_token)

    minimal_env.assert_called_once_with()
    assert observed_argvs == [[fake_agy_bin, "-p", fixed_prompt]]
    assert completed.returncode == 0
    assert result["ok"] is True
    assert result["agy_invocation_attempted"] is True
    assert result["agy_failure_kind"] is None


def test_agy_minimal_env_direct_subprocess_nonzero_marks_attempted_with_exact_argv(monkeypatch) -> None:
    """The no-tool-profile direct branch preserves attempted/nonzero correlation."""
    fake_agy_bin = "/hermetic/agy-direct-failure"
    fixed_prompt = "direct minimal-env failure"
    observed_argvs: list[list[Any]] = []
    monkeypatch.setenv("AGY_BIN", fake_agy_bin)
    attempt_token = rgh._AGY_INVOCATION_ATTEMPTED_CTX.set(False)
    profile_token = rgh._AGY_TOOL_PROFILE_CTX.set(None)

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        actual_argv = list(cmd)
        observed_argvs.append(actual_argv)
        assert actual_argv == [fake_agy_bin, "-p", fixed_prompt]
        assert kwargs["env"] == {"PATH": "/minimal-agy-path"}
        assert rgh._AGY_INVOCATION_ATTEMPTED_CTX.get() is True
        return _make_completed(23, stderr="direct AGY failure")

    try:
        assert rgh._AGY_INVOCATION_ATTEMPTED_CTX.get() is False
        with (
            patch.object(rgh, "_minimal_agy_env", return_value={"PATH": "/minimal-agy-path"}) as minimal_env,
            patch.object(rgh, "_AGY_PROVENANCE_AVAILABLE", False),
            patch("subprocess.run", side_effect=fake_run),
        ):
            completed = rgh._run_agy(fixed_prompt, 30)
        result = rgh._attach_agy_failure_kind(
            {"provider": "agy"},
            rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None),
        )
    finally:
        rgh._AGY_TOOL_PROFILE_CTX.reset(profile_token)
        rgh._AGY_INVOCATION_ATTEMPTED_CTX.reset(attempt_token)

    minimal_env.assert_called_once_with()
    assert observed_argvs == [[fake_agy_bin, "-p", fixed_prompt]]
    assert completed.returncode == 23
    assert result["ok"] is False
    assert result["agy_invocation_attempted"] is True
    assert result["agy_failure_kind"] == "operational"


def test_bwrap_status_parser_requires_a_valid_positive_child_pid() -> None:
    """Only bounded, strict JSON status records with a positive child pid prove spawn."""
    assert rgh._bwrap_status_reports_child_started(b'{"child-pid":1234}\n') is True
    for invalid_status in (
        b"",
        b'{"exit-code":0}\n',
        b'{"child-pid":0}\n',
        b'{"child-pid":-1}\n',
        b'{"child-pid":true}\n',
        b'{"child-pid":"1234"}\n',
        b'{"child-pid":1234}\nnot-json\n',
        b"x" * (rgh._BWRAP_STATUS_MAX_BYTES + 1),
    ):
        assert rgh._bwrap_status_reports_child_started(invalid_status) is False


def test_agy_bwrap_preexec_nonzero_does_not_mark_actual_invocation(monkeypatch, tmp_path: Path) -> None:
    fake_agy_bin = "/hermetic/agy-ac10-bwrap"
    fixed_prompt = "AC10 bwrap pre-exec failure"
    bwrap_prefix = ["bwrap", "--deterministic-pre-exec-failure", "--"]
    fake_workspace = types.SimpleNamespace(
        env={}, workspace_dir=tmp_path, agy_oauth_token_bwrap_prefix=bwrap_prefix
    )
    observed_argvs: list[list[Any]] = []
    monkeypatch.setenv("AGY_BIN", fake_agy_bin)
    attempt_token = rgh._AGY_INVOCATION_ATTEMPTED_CTX.set(False)

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        actual_argv = list(cmd)
        observed_argvs.append(actual_argv)
        assert actual_argv[:2] == bwrap_prefix[:2]
        assert actual_argv[2] == "--json-status-fd"
        assert actual_argv[4:] == ["--", fake_agy_bin, "-p", fixed_prompt]
        assert kwargs["shell"] is False
        assert rgh._AGY_INVOCATION_ATTEMPTED_CTX.get() is False
        return _make_completed(125, stderr="simulated bwrap configuration failure")

    try:
        with patch.object(
            rgh._agy_permission_policy, "materialize_isolated_agy_workspace", return_value=fake_workspace
        ):
            with patch("shutil.rmtree"), patch("subprocess.run", side_effect=fake_run):
                result = rgh.run_delegation(_agy_request(prompt=fixed_prompt))
    finally:
        rgh._AGY_INVOCATION_ATTEMPTED_CTX.reset(attempt_token)

    assert len(observed_argvs) == 1
    assert result["ok"] is False
    assert result["failure_class"] == "agy_exit_nonzero"
    assert result["agy_failure_kind"] == "operational"
    assert result["agy_invocation_attempted"] is False
    assert not (result["agy_invocation_attempted"] and result["agy_failure_kind"] == "operational")


def test_agy_bwrap_child_pid_success_marks_attempted_and_preserves_success(monkeypatch, tmp_path: Path) -> None:
    """A valid bwrap child-pid is the sole proof that may authorize AGY success."""
    fake_agy_bin = "/hermetic/agy-ac10-bwrap"
    fixed_prompt = "AC10 bwrap child success"
    bwrap_prefix = ["bwrap", "--deterministic-child-success", "--"]
    fake_workspace = types.SimpleNamespace(
        env={}, workspace_dir=tmp_path, agy_oauth_token_bwrap_prefix=bwrap_prefix
    )
    monkeypatch.setenv("AGY_BIN", fake_agy_bin)
    attempt_token = rgh._AGY_INVOCATION_ATTEMPTED_CTX.set(False)

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        actual_argv = list(cmd)
        assert actual_argv[:2] == bwrap_prefix[:2]
        assert actual_argv[2] == "--json-status-fd"
        assert actual_argv[4:] == ["--", fake_agy_bin, "-p", fixed_prompt]
        assert rgh._AGY_INVOCATION_ATTEMPTED_CTX.get() is False
        os.write(kwargs["pass_fds"][0], b'{"child-pid":1234}\n{"exit-code":0}\n')
        return _make_completed(0, stdout="bwrap child AGY success")

    try:
        with patch.object(
            rgh._agy_permission_policy, "materialize_isolated_agy_workspace", return_value=fake_workspace
        ):
            with patch("shutil.rmtree"), patch("subprocess.run", side_effect=fake_run):
                result = rgh.run_delegation(_agy_request(prompt=fixed_prompt))
    finally:
        rgh._AGY_INVOCATION_ATTEMPTED_CTX.reset(attempt_token)

    assert result["ok"] is True
    assert result["response_text"] == "bwrap child AGY success"
    assert result["agy_invocation_attempted"] is True
    assert result["agy_failure_kind"] is None
    router = _load_agy_advisory_fallback_router()
    decision = router.route_agy_advisory_fallback(
        result,
        requirement="advisory",
        canonical_failure_kind=rgh.canonical_agy_failure_kind,
    )
    assert decision["status"] == "ok"
    assert decision["next_action"] == "continue_agy_result"


def test_agy_bwrap_normal_success_without_child_proof_fails_closed(monkeypatch, tmp_path: Path) -> None:
    """A zero bwrap return without valid child proof cannot expose a response or fallback."""
    fake_agy_bin = "/hermetic/agy-ac10-bwrap"
    fixed_prompt = "AC10 bwrap unproven normal success"
    bwrap_prefix = ["bwrap", "--deterministic-unproven-success", "--"]
    fake_workspace = types.SimpleNamespace(
        env={}, workspace_dir=tmp_path, agy_oauth_token_bwrap_prefix=bwrap_prefix
    )
    proof_failures: tuple[tuple[str, bytes | None], ...] = (
        ("absent", None),
        ("malformed", b"not-json\n"),
        ("oversized", b"x" * (rgh._BWRAP_STATUS_MAX_BYTES + 1)),
        ("no-positive-child-pid", b'{"child-pid":0}\n'),
    )
    monkeypatch.setenv("AGY_BIN", fake_agy_bin)

    for case_name, status in proof_failures:
        attempt_token = rgh._AGY_INVOCATION_ATTEMPTED_CTX.set(False)

        def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
            actual_argv = list(cmd)
            assert actual_argv[:2] == bwrap_prefix[:2]
            assert actual_argv[2] == "--json-status-fd"
            assert actual_argv[4:] == ["--", fake_agy_bin, "-p", fixed_prompt]
            assert rgh._AGY_INVOCATION_ATTEMPTED_CTX.get() is False
            if status is not None:
                assert os.write(kwargs["pass_fds"][0], status) == len(status)
            return _make_completed(0, stdout=f"unproven response: {case_name}")

        try:
            with patch.object(
                rgh._agy_permission_policy, "materialize_isolated_agy_workspace", return_value=fake_workspace
            ):
                with patch("shutil.rmtree"), patch("subprocess.run", side_effect=fake_run):
                    result = rgh.run_delegation(_agy_request(prompt=fixed_prompt))
        finally:
            rgh._AGY_INVOCATION_ATTEMPTED_CTX.reset(attempt_token)

        assert result["ok"] is False
        assert result["failure_class"] == "agy_exit_nonzero"
        assert result["response_text"] is None
        assert result["agy_invocation_attempted"] is False
        assert result["agy_failure_kind"] == "operational"
        router = _load_agy_advisory_fallback_router()
        decision = router.route_agy_advisory_fallback(
            result,
            requirement="advisory",
            canonical_failure_kind=rgh.canonical_agy_failure_kind,
        )
        assert decision["status"] == "failed"
        assert decision["next_action"] == "fail_closed"
        assert decision["reason_code"] == "non_agy_or_pre_agy"


def test_agy_bwrap_child_started_nonzero_marks_actual_invocation(monkeypatch, tmp_path: Path) -> None:
    fake_agy_bin = "/hermetic/agy-ac10-bwrap"
    fixed_prompt = "AC10 bwrap child failure"
    bwrap_prefix = ["bwrap", "--deterministic-child-failure", "--"]
    fake_workspace = types.SimpleNamespace(
        env={}, workspace_dir=tmp_path, agy_oauth_token_bwrap_prefix=bwrap_prefix
    )
    monkeypatch.setenv("AGY_BIN", fake_agy_bin)
    attempt_token = rgh._AGY_INVOCATION_ATTEMPTED_CTX.set(False)

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        actual_argv = list(cmd)
        assert actual_argv[:2] == bwrap_prefix[:2]
        assert actual_argv[2] == "--json-status-fd"
        assert actual_argv[4:] == ["--", fake_agy_bin, "-p", fixed_prompt]
        assert kwargs["shell"] is False
        assert rgh._AGY_INVOCATION_ATTEMPTED_CTX.get() is False
        os.write(kwargs["pass_fds"][0], b'{"child-pid":1234}\n{"exit-code":1}\n')
        return _make_completed(1, stderr="simulated AGY process failure")

    try:
        with patch.object(
            rgh._agy_permission_policy, "materialize_isolated_agy_workspace", return_value=fake_workspace
        ):
            with patch("shutil.rmtree"), patch("subprocess.run", side_effect=fake_run):
                result = rgh.run_delegation(_agy_request(prompt=fixed_prompt))
    finally:
        rgh._AGY_INVOCATION_ATTEMPTED_CTX.reset(attempt_token)

    assert result["ok"] is False
    assert result["failure_class"] == "agy_exit_nonzero"
    assert result["agy_failure_kind"] == "operational"
    assert result["agy_invocation_attempted"] is True
    assert rgh.canonical_agy_failure_kind(result["failure_class"]) == "operational"


def test_agy_bwrap_child_started_timeout_marks_actual_invocation_and_preserves_fallback_eligibility(
    monkeypatch, tmp_path: Path
) -> None:
    fake_agy_bin = "/hermetic/agy-ac10-bwrap"
    fixed_prompt = "AC10 bwrap child timeout"
    bwrap_prefix = ["bwrap", "--deterministic-child-timeout", "--"]
    fake_workspace = types.SimpleNamespace(
        env={}, workspace_dir=tmp_path, agy_oauth_token_bwrap_prefix=bwrap_prefix
    )
    observed_attempted: list[bool] = []
    monkeypatch.setenv("AGY_BIN", fake_agy_bin)

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        actual_argv = list(cmd)
        assert actual_argv[:2] == bwrap_prefix[:2]
        assert actual_argv[2] == "--json-status-fd"
        assert actual_argv[4:] == ["--", fake_agy_bin, "-p", fixed_prompt]
        assert kwargs["shell"] is False
        observed_attempted.append(rgh._AGY_INVOCATION_ATTEMPTED_CTX.get())
        os.write(kwargs["pass_fds"][0], b'{"child-pid":1234}\n')
        raise subprocess.TimeoutExpired(cmd=actual_argv, timeout=kwargs["timeout"])

    with patch.object(
        rgh._agy_permission_policy, "materialize_isolated_agy_workspace", return_value=fake_workspace
    ):
        with patch("shutil.rmtree"), patch("subprocess.run", side_effect=fake_run):
            result = rgh.run_delegation(_agy_request(prompt=fixed_prompt))

    assert observed_attempted == [False]
    assert result["ok"] is False
    assert result["failure_class"] == "agy_timeout"
    assert result["agy_invocation_attempted"] is True
    assert result["agy_failure_kind"] == "operational"

    router = _load_agy_advisory_fallback_router()
    decision = router.route_agy_advisory_fallback(
        result,
        requirement="advisory",
        canonical_failure_kind=rgh.canonical_agy_failure_kind,
    )
    assert decision["status"] == "degraded"
    assert decision["next_action"] == "native_non_mutating_fallback"
    assert decision["reason_code"] == "advisory_operational"


def test_agy_bwrap_preexec_timeout_does_not_mark_actual_invocation(monkeypatch, tmp_path: Path) -> None:
    fake_agy_bin = "/hermetic/agy-ac10-bwrap"
    fixed_prompt = "AC10 bwrap pre-exec timeout"
    bwrap_prefix = ["bwrap", "--deterministic-pre-exec-timeout", "--"]
    fake_workspace = types.SimpleNamespace(
        env={}, workspace_dir=tmp_path, agy_oauth_token_bwrap_prefix=bwrap_prefix
    )
    observed_attempted: list[bool] = []
    monkeypatch.setenv("AGY_BIN", fake_agy_bin)

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        observed_attempted.append(rgh._AGY_INVOCATION_ATTEMPTED_CTX.get())
        raise subprocess.TimeoutExpired(cmd=list(cmd), timeout=kwargs["timeout"])

    with patch.object(
        rgh._agy_permission_policy, "materialize_isolated_agy_workspace", return_value=fake_workspace
    ):
        with patch("shutil.rmtree"), patch("subprocess.run", side_effect=fake_run):
            result = rgh.run_delegation(_agy_request(prompt=fixed_prompt))

    assert observed_attempted == [False]
    assert result["failure_class"] == "agy_timeout"
    assert result["agy_invocation_attempted"] is False
    assert result["agy_failure_kind"] == "operational"


# ---------------------------------------------------------------------------
# Issue #1274 AC4/AC5: warnings[0] leading token must match failure_class in
# both non-CI and CI empty-stdout branches. This is a regression test for the
# fix already merged in #1331 (_normalize_agy_result empty-stdout warning
# construction). AC5 additionally requires this coverage located in
# test_agy_provider.py per the Issue #1274 specified location (duplicate of
# the equivalent test_quota_fallback.py coverage is intentional per Issue
# #1274 scope).
# ---------------------------------------------------------------------------


def test_agy_empty_stdout_warning_matches_failure_class():
    """AC4: non-CI empty stdout produces warnings[0] starting with agy_empty_stdout."""
    with patch.dict(os.environ, {"CI": ""}, clear=False):
        completed = _make_completed(0, stdout="")
        result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["failure_class"] == "agy_empty_stdout"
    assert result["warnings"][0].startswith("agy_empty_stdout")


def test_agy_empty_stdout_warning_matches_failure_class_in_ci():
    """AC5: CI empty stdout produces warnings[0] starting with agy_output_missing."""
    with patch.dict(os.environ, {"CI": "1"}, clear=False):
        completed = _make_completed(0, stdout="")
        result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)
    assert result["failure_class"] == "agy_output_missing"
    assert result["warnings"][0].startswith("agy_output_missing")


def test_agy_empty_stdout_warning_matches_failure_class_when_ci_unset(monkeypatch):
    """PR #1345 fix_delta Blocker 2: CI unset (not merely empty) must also take the
    non-CI (agy_empty_stdout) branch, distinct from the CI="" case already covered
    by test_agy_empty_stdout_warning_matches_failure_class above."""
    monkeypatch.delenv("CI", raising=False)
    completed = _make_completed(0, stdout="")
    result = rgh._normalize_agy_result(completed, tool_profile="no_tools", requested_model=None)

    assert result["failure_class"] == "agy_empty_stdout"
    assert result["warnings"][0].startswith("agy_empty_stdout")


# ---------------------------------------------------------------------------
# Issue #1749 (superseded by Issue #1777, further narrowed by Issue #2069):
# grounded_research forces --model claude-sonnet-4-6 so agy -p actually
# calls search_web/read_url_content instead of hallucinating a "searched"
# answer with the default model.
#
# Issue #1777 ran a controlled grounding matrix experiment and found the
# model-selection causal claim was NOT supported (prompt construction was
# the dominant factor, not model selection); the exact-model-hardcode
# AGY_GROUNDED_RESEARCH_MODEL constant was replaced by capability-driven
# routing (resolve_agy_grounded_research_model(), config/model_routing.yaml
# roles.grounded_research.model_chain).
#
# Issue #2069: the #1777 experiment's own finding (account_default
# outperformed the claude-sonnet-4-6 candidate) was still not reflected in
# the config -- roles.grounded_research.model_chain stayed hardcoded to
# ["claude-sonnet-4-6"], unilaterally consuming the Antigravity CLI shared
# "Claude and GPT Models" quota on every grounded_research call. The chain
# is now the empty default (`[]`, grounded_research_empty_chain_exception in
# load_model_routing()), so the default route omits --model entirely and
# defers model selection to AGY's own account_default. The test below is
# updated accordingly; see also test_grounded_research_default_route_no_model_flag.
# ---------------------------------------------------------------------------


def test_issue_1749_grounded_research_uses_capability_driven_model_candidate() -> None:
    """AC7 (replaces the old exact-string test; updated by Issue #2069):
    grounded_research's agy -p invocation resolves --model from
    config/model_routing.yaml roles.grounded_research.model_chain via
    capability-driven routing, not a hardcoded constant. With the current
    default (empty) chain, resolution correctly omits --model altogether
    (AGY account_default) rather than forcing a candidate."""
    captured_cmd: dict[str, Any] = {"value": None}

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd["value"] = list(cmd)
        return _make_completed(0, stdout="ok")

    token = rgh._AGY_TOOL_PROFILE_CTX.set("grounded_research")
    try:
        with patch("subprocess.run", side_effect=mock_run):
            rgh._run_agy("test prompt", 30)
    finally:
        rgh._AGY_TOOL_PROFILE_CTX.reset(token)

    cmd = captured_cmd["value"]
    assert cmd is not None
    expected_chain, _error = rgh.resolve_model_chain({"role": "grounded_research"})
    assert expected_chain == []
    assert "--model" not in cmd
    assert not hasattr(rgh, "AGY_GROUNDED_RESEARCH_MODEL")


def test_grounded_research_default_route_no_model_flag() -> None:
    """Issue #2069 AC8: on the default route (config/model_routing.yaml's
    roles.grounded_research.model_chain == []), resolve_agy_grounded_research_model()
    returns None, and the real agy invocation argv built by _run_agy() does
    not include a --model flag at all -- AGY account_default is used."""
    assert rgh.resolve_agy_grounded_research_model() is None

    captured_cmd: dict[str, Any] = {"value": None}

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd["value"] = list(cmd)
        return _make_completed(0, stdout="ok")

    token = rgh._AGY_TOOL_PROFILE_CTX.set("grounded_research")
    try:
        with patch("subprocess.run", side_effect=mock_run):
            rgh._run_agy("test prompt", 30)
    finally:
        rgh._AGY_TOOL_PROFILE_CTX.reset(token)

    cmd = captured_cmd["value"]
    assert cmd is not None
    assert "--model" not in cmd


def test_issue_1749_non_grounded_research_profile_omits_model_flag() -> None:
    """no_tools profile's agy -p invocation does NOT get the forced --model flag."""
    captured_cmd: dict[str, Any] = {"value": None}

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd["value"] = list(cmd)
        return _make_completed(0, stdout="ok")

    token = rgh._AGY_TOOL_PROFILE_CTX.set("no_tools")
    try:
        with patch("subprocess.run", side_effect=mock_run):
            rgh._run_agy("test prompt", 30)
    finally:
        rgh._AGY_TOOL_PROFILE_CTX.reset(token)

    cmd = captured_cmd["value"]
    assert cmd is not None
    assert "--model" not in cmd


def test_issue_1749_grounded_research_end_to_end_forces_model_via_run_delegation() -> None:
    """AC3/AC4 (pre-#1777; updated by Issue #2069): run_delegation(tool_profile=
    grounded_research) drives _run_agy() through the real subprocess.run() argv
    path with a grounded tool_calls trace correctly recognized. With the
    current default (empty) roles.grounded_research.model_chain, the real
    invocation correctly omits --model (AGY account_default) rather than
    forcing a candidate -- see test_grounded_research_default_route_no_model_flag
    for the focused unit-level assertion."""
    captured_cmd: dict[str, Any] = {"value": None}
    grounded_output = (
        "Response from AGY.\n"
        '{"grounding":{"queries":["AGY WebSearch"],"sources":[{"url":"https://example.com","title":"example"}]},'
        '"tool_calls":[{"name":"web_search"}]}'
    )

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd["value"] = list(cmd)
        # Issue #2038 fix_delta iteration 2: a validated hook event is now
        # required to reach grounding_status "grounded" via this real
        # _run_agy() -> subprocess.run() path.
        _write_valid_hook_event_for_subprocess_env(kwargs)
        return _make_completed(0, stdout=grounded_output)

    with patch("subprocess.run", side_effect=mock_run):
        result = rgh.run_delegation(_agy_request(tool_profile="grounded_research", timeout_sec=120))

    assert result["ok"] is True
    cmd = captured_cmd["value"]
    assert cmd is not None
    expected_chain, _error = rgh.resolve_model_chain({"role": "grounded_research"})
    assert expected_chain == []
    assert "--model" not in cmd


# ---------------------------------------------------------------------------
# Issue #1777 AC1-AC6: capability-driven routing / explicit-search prompt /
# optional model / bounded retry / fresh session / evidence gate regression.
# ---------------------------------------------------------------------------


def test_issue_1777_ac1_model_routing_yaml_defines_grounded_research_role() -> None:
    """AC1: grounded_research model candidates are loaded from
    model_routing.yaml's roles section (not a Python constant). Updated by
    Issue #2069: the default chain is now the empty list
    (grounded_research_empty_chain_exception), so resolve_agy_grounded_research_model()
    correctly resolves to None (AGY account_default) rather than chain[0]."""
    routing = rgh.load_model_routing()
    assert "grounded_research" in routing["roles"]
    chain = routing["roles"]["grounded_research"]["model_chain"]
    assert isinstance(chain, list)
    assert all(isinstance(entry, str) and entry.strip() for entry in chain)
    assert chain == []
    assert rgh.resolve_agy_grounded_research_model() is None


def test_issue_1777_ac2_grounded_research_prompt_contains_explicit_search_instruction() -> None:
    """AC2: the outgoing agy grounded_research prompt always carries the
    explicit-search-required instruction, even when the caller supplied no
    prompt text at all (bounded default) or already-had-content prompt text
    (appended, not silently dropped)."""
    captured_prompts: list[str] = []
    grounded_output = (
        "Response from AGY.\n"
        '{"grounding":{"queries":["AGY WebSearch"],"sources":[{"url":"https://example.com","title":"example"}]},'
        '"tool_calls":[{"name":"web_search"}]}'
    )

    def mock_run_agy(prompt: str, timeout_sec: int, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_prompts.append(prompt)
        return _make_completed(0, stdout=grounded_output)

    with patch.object(rgh, "_run_agy", side_effect=mock_run_agy):
        rgh.run_delegation(
            _agy_request(tool_profile="grounded_research", timeout_sec=120, prompt="Find the current release notes")
        )

    assert len(captured_prompts) == 1
    assert rgh.AGY_GROUNDED_RESEARCH_EXPLICIT_SEARCH_INSTRUCTION in captured_prompts[0]
    assert "Find the current release notes" in captured_prompts[0]


def test_issue_1777_ac3_grounded_research_model_optional_account_default(monkeypatch) -> None:
    """AC3: when every configured model candidate fails the availability
    preflight, _run_agy() issues agy -p with NO --model flag (account_default)
    and run_delegation(tool_profile=grounded_research) still returns ok: true."""
    monkeypatch.setenv(
        rgh.AGY_MODEL_AVAILABILITY_OVERRIDE_ENV,
        '{"claude-sonnet-4-6": false}',
    )
    captured_cmd: dict[str, Any] = {"value": None}
    grounded_output = (
        "Response from AGY.\n"
        '{"grounding":{"queries":["AGY WebSearch"],"sources":[{"url":"https://example.com","title":"example"}]},'
        '"tool_calls":[{"name":"web_search"}]}'
    )

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmd["value"] = list(cmd)
        # Issue #2038 fix_delta iteration 2: a validated hook event is now
        # required to reach grounding_status "grounded" via this real
        # _run_agy() -> subprocess.run() path.
        _write_valid_hook_event_for_subprocess_env(kwargs)
        return _make_completed(0, stdout=grounded_output)

    assert rgh.resolve_agy_grounded_research_model() is None

    with patch("subprocess.run", side_effect=mock_run):
        result = rgh.run_delegation(_agy_request(tool_profile="grounded_research", timeout_sec=120))

    assert result["ok"] is True
    cmd = captured_cmd["value"]
    assert cmd is not None
    assert "--model" not in cmd


def test_issue_1777_ac4_grounded_research_bounded_retry_does_not_exceed_limit() -> None:
    """AC4: when every attempt hallucinates (no verifiable tool call), the
    number of agy -p invocations is bounded by
    AGY_GROUNDED_RESEARCH_RETRY_LIMIT + 1 -- it does not retry forever."""
    call_count = {"value": 0}

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        call_count["value"] += 1
        return _make_completed(0, stdout="I searched and found nothing relevant.")

    with patch("subprocess.run", side_effect=mock_run):
        result = rgh.run_delegation(_agy_request(tool_profile="grounded_research", timeout_sec=120))

    assert result["ok"] is False
    assert result["failure_class"] == "agy_web_grounding_tool_call_missing"
    assert call_count["value"] == rgh.AGY_GROUNDED_RESEARCH_RETRY_LIMIT + 1
    assert result["agy_grounded_research_attempts"] == rgh.AGY_GROUNDED_RESEARCH_RETRY_LIMIT + 1


def test_issue_1777_ac5_grounded_research_retry_uses_fresh_session() -> None:
    """AC5: each bounded-retry attempt is a brand-new subprocess.run() call
    (fresh session) -- the failing first attempt's output is never fed back
    into a later attempt's prompt/argv, and a later attempt that succeeds
    stops the loop without exhausting the retry budget."""
    captured_cmds: list[list[str]] = []
    grounded_output = (
        "Response from AGY.\n"
        '{"grounding":{"queries":["AGY WebSearch"],"sources":[{"url":"https://example.com","title":"example"}]},'
        '"tool_calls":[{"name":"web_search"}]}'
    )

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        captured_cmds.append(list(cmd))
        call_number = len(captured_cmds)
        if call_number < 2:
            return _make_completed(0, stdout="I searched and found nothing relevant.")
        # Issue #2038 fix_delta iteration 2: a validated hook event is now
        # required to reach grounding_status "grounded" via this real
        # _run_agy() -> subprocess.run() path.
        _write_valid_hook_event_for_subprocess_env(kwargs)
        return _make_completed(0, stdout=grounded_output)

    with patch("subprocess.run", side_effect=mock_run):
        result = rgh.run_delegation(_agy_request(tool_profile="grounded_research", timeout_sec=120))

    assert result["ok"] is True
    assert result["agy_grounded_research_attempts"] == 2
    # Fresh session: each attempt is its own subprocess.run() call (argv
    # sent -- including the prompt text embedded via `-p` -- is identical
    # across attempts; the prior failing attempt's stdout is never fed back
    # into a later attempt's argv/prompt).
    assert len(captured_cmds) == 2
    prompt_index = captured_cmds[0].index("-p") + 1
    assert captured_cmds[0][prompt_index] == captured_cmds[1][prompt_index]


def test_issue_1777_ac6_grounded_research_evidence_gate_applies_regardless_of_model() -> None:
    """AC6: the #1708/#1710/#1771 fail-closed evidence gate
    (grounding_failure_class -> top-level ok=False) still applies exactly
    the same way whether or not a --model candidate was actually selected
    (account_default path included)."""
    hallucinated_output = "I searched and found the answer without citing anything."

    def mock_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return _make_completed(0, stdout=hallucinated_output)

    with patch("subprocess.run", side_effect=mock_run):
        with_model_result = rgh.run_delegation(_agy_request(tool_profile="grounded_research", timeout_sec=120))
    assert with_model_result["ok"] is False
    assert with_model_result["failure_class"] == "agy_web_grounding_tool_call_missing"

    with patch.dict(os.environ, {rgh.AGY_MODEL_AVAILABILITY_OVERRIDE_ENV: '{"claude-sonnet-4-6": false}'}):
        assert rgh.resolve_agy_grounded_research_model() is None
        with patch("subprocess.run", side_effect=mock_run):
            no_model_result = rgh.run_delegation(_agy_request(tool_profile="grounded_research", timeout_sec=120))
    assert no_model_result["ok"] is False
    assert no_model_result["failure_class"] == "agy_web_grounding_tool_call_missing"


# ---------------------------------------------------------------------------
# Issue #2015 AC2: hermetic transport-level test using a fake stdio MCP
# server that emits a large stderr burst before replying, verifying the
# collector completes without stalling and stdout JSON-RPC is never
# corrupted by merged stderr.
# ---------------------------------------------------------------------------

_FAKE_SERENA_STDERR_BACKPRESSURE_SERVER_SOURCE = """
import json
import sys


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


def _read():
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def main():
    tools = ["find_file", "search_for_pattern", "get_symbols_overview"]
    while True:
        msg = _read()
        if msg is None:
            return
        method = msg.get("method")
        mid = msg.get("id")
        if method == "notifications/initialized":
            continue
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": t} for t in tools]}})
        elif method == "tools/call":
            params = msg.get("params", {}) or {}
            name = params.get("name")
            # Simulate a repo-wide search_for_pattern dumping a large
            # amount of Serena-side logging to stderr before it can reply
            # on stdout -- this is the self-induced stall scenario from the
            # Issue #2015 background section.
            chunk = "serena-stderr-log-line " * 200
            for _ in range(500):
                sys.stderr.write(chunk + "\\n")
            sys.stderr.flush()
            _send({"jsonrpc": "2.0", "id": mid, "result": {"echo": name}})
        else:
            _send({"jsonrpc": "2.0", "id": mid, "result": {}})


if __name__ == "__main__":
    main()
"""


def test_ac2_serena_collector_survives_stderr_backpressure_fake_mcp_server(tmp_path) -> None:
    """AC2: a fake MCP server that writes a large stderr burst before every
    tools/call response must not cause the collector to time out. stderr is
    bounded/redacted and must never corrupt the stdout JSON-RPC stream."""
    server_path = tmp_path / "fake_serena_stderr_backpressure_server.py"
    server_path.write_text(_FAKE_SERENA_STDERR_BACKPRESSURE_SERVER_SOURCE, encoding="utf-8")

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    context_file = repo_root / "context.md"
    context_file.write_text("local_asset_research content", encoding="utf-8")

    def _fake_load_serena_from_mcp_config(root, mcp_config_path=None):
        return {"command": sys.executable, "args": [str(server_path)]}

    manifest = {
        "pinned_ref": "deadbeef00000000",
        "read_only_allowlist": ["find_file", "search_for_pattern", "get_symbols_overview"],
        "known_tools": ["find_file", "search_for_pattern", "get_symbols_overview"],
    }

    with (
        patch.object(rgh, "_load_serena_from_mcp_config", _fake_load_serena_from_mcp_config),
        patch.object(rgh, "SERENA_COLLECTOR_SESSION_DEADLINE_SEC", 20.0),
        patch.object(rgh, "SERENA_CLIENT_REQUEST_TIMEOUT_SEC", 10.0),
        patch.object(rgh, "SERENA_SERVER_TOOL_TIMEOUT_SEC", 8.0),
    ):
        documents, metadata = rgh._collect_live_serena_read_only_evidence([context_file], repo_root, manifest)

    assert len(documents) == 3
    assert metadata["manifest_drift_failed"] is False
    assert metadata["stderr_byte_count"] > 0
    # Bounded: even though the fake server wrote well over 1MB per call
    # across three tools/call round-trips, the retained tail never exceeds
    # the configured ring buffer cap.
    assert metadata["stderr_byte_count"] <= rgh.SERENA_STDERR_RING_BUFFER_MAX_BYTES
    for doc in documents:
        content = json.loads(doc["content"])
        # stdout JSON-RPC content must be intact -- no interleaved stderr
        # log lines leaking into the tool result snippet.
        assert "serena-stderr-log-line" not in content["content_snippet"]
