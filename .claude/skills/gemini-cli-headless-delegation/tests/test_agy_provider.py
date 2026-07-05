"""Tests for agy provider support in run_gemini_headless.py.

Covers AC1-AC14 for provider=agy path. Uses mock subprocess to avoid
requiring the agy CLI to be installed in the test environment.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["agy", "-p", "test"], returncode=returncode, stdout=stdout, stderr=stderr)


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
    errors = rgh.validate_request({
        "schema": "delegation_request_v1",
        "tool_profile": "no_tools",
        "provider": "gemini",
        "objective": "Test gemini provider validation with enough detail",
        "instructions": ["Step one", "Step two"],
        "output_sections": ["response"],
        "context_files": [],
    })
    # No unknown_provider error should be present
    assert not any("unknown_provider" in e for e in errors)


def test_ac6_missing_provider_defaults_to_gemini() -> None:
    """AC6: provider not specified -> gemini default, no unknown_provider error."""
    errors = rgh.validate_request({
        "schema": "delegation_request_v1",
        "tool_profile": "no_tools",
        "objective": "Test default provider with enough detail here",
        "instructions": ["Step one", "Step two"],
        "output_sections": ["response"],
        "context_files": [],
    })
    assert not any("unknown_provider" in e for e in errors)


# ---------------------------------------------------------------------------
# AC7: unsupported profile for agy fails closed (no fallback to gemini)
# ---------------------------------------------------------------------------


def test_ac7_agy_grounded_research_supported() -> None:
    """AC7: provider=agy + grounded_research is supported and returns websearch evidence.

    Success requires a machine-verifiable `tool_calls` trace with a recognized web tool
    name (e.g. `web_search`) in addition to a structured citation — a bare URL string is
    not sufficient (Issue #1266 Blocker 1).
    """
    captured_timeout: dict[str, int | None] = {"value": None}
    grounded_output = (
        "Response from AGY.\n"
        '{"grounding":{"queries":["AGY WebSearch"],"sources":[{"url":"https://example.com","title":"example"}]},'
        '"tool_calls":[{"name":"web_search"}]}'
    )

    def _run_agy(prompt: str, timeout_sec: int = rgh.DEFAULT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
        captured_timeout["value"] = timeout_sec
        return _make_completed(0, stdout=grounded_output)

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
    # Includes a machine-verifiable tool_calls trace (Issue #1266 Blocker 1) so this AC4 test's
    # ok=True assertion reflects a genuine grounded result, not a bare URL scan.
    grounded_output = (
        "Response from AGY.\n"
        '{"grounding":{"queries":["AGY WebSearch"],"sources":[{"url":"https://example.com","title":"example"}]},'
        '"tool_calls":[{"name":"web_search"}]}'
    )
    completed = _make_completed(0, stdout=grounded_output)
    with patch.object(rgh, "_run_agy", return_value=completed) as mock_agy, patch.object(
        rgh,
        "_run_gemini",
        side_effect=AssertionError(
            "_run_gemini (Gemini CLI/API path) must not be called for provider=agy"
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


def test_agy_grounded_research_url_without_tool_trace_fail_closed() -> None:
    """A bare URL string in stdout without a machine-verifiable tool-call trace must NOT be
    treated as a WebSearch execution proof (Issue #1266 Blocker 1)."""
    result = rgh._normalize_agy_result(
        _make_completed(0, stdout="Here is a helpful link: https://example.com/article"),
        tool_profile="grounded_research",
        requested_model=None,
    )
    evidence = result["grounded_research_evidence"]
    assert evidence["grounding_backend"] == "none"
    assert evidence["grounding_status"] == "attempted_no_web_tool_call"
    assert evidence["grounding_failure_class"] == "agy_web_grounding_tool_call_missing"
    assert evidence["web_tool_call_count"] == 0
    assert evidence["url_citation_count"] == 0
    assert result["ok"] is False
    assert result["failure_class"] == "agy_web_grounding_tool_call_missing"


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
    """AC7: provider=agy with local_asset_research requires local_asset_research context files."""
    req = _agy_request(tool_profile="local_asset_research")
    with patch.object(rgh, "_validate_local_asset_research_settings", lambda: []):  # type: ignore[call-arg]
        result = rgh.run_delegation(req)
    assert result["ok"] is False
    assert result["failure_reason"].startswith("context_files must contain at least 1 item(s)")
    assert result["failure_class"].startswith("context_files must contain at least 1 item(s)")


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

    def _fake_live_evidence(context_paths, root, manifest):
        return [
            {
                "path": "context.md",
                "content": __import__("json").dumps(
                    {
                        "schema": "wrapper_serena_evidence_v1",
                        "evidence": [
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
                        ],
                    },
                    ensure_ascii=False,
                ),
                "evidence": {"source_kind": "serena_mcp_read_only_evidence"},
            }
        ]

    def _run_agy(prompt: str, timeout_sec: int = rgh.DEFAULT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
        captured_prompt["value"] = prompt
        return _make_completed(0, stdout="LOOP_AGY_SMOKE_OK")

    req = _agy_request(
        tool_profile="local_asset_research",
        context_files=["context.md"],
        prompt="Summarize local repository evidence.",
    )
    with patch.object(rgh, "_run_agy", side_effect=_run_agy), patch.object(
        rgh,
        "_collect_live_serena_read_only_evidence",
        side_effect=_fake_live_evidence,
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


def test_ac7_agy_github_research_rejected() -> None:
    """AC7: provider=agy with github_research -> unsupported_provider_profile."""
    req = _agy_request(tool_profile="github_research")
    result = rgh.run_delegation(req)
    assert result["ok"] is False
    assert result["failure_class"] == "unsupported_provider_profile"


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
        captured_kwargs.get("shell") is False
        or "shell" not in captured_kwargs
        or captured_kwargs.get("shell") is False
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
    assert "agy_not_found" in (result.get("failure_reason") or "")


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
