"""Tests for ACP transport (run_gemini_acp.py).

All tests run without launching a real gemini CLI process — deterministic.
Uses a mock JSON-RPC server piped via asyncio subprocess.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import textwrap
import time
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the scripts directory is importable
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import run_gemini_acp as acp


# ---------------------------------------------------------------------------
# HeartbeatWatchdog tests
# ---------------------------------------------------------------------------


class TestHeartbeatWatchdog:
    """GIVEN a HeartbeatWatchdog, WHEN timeouts are simulated, THEN the correct stage fires."""

    def test_no_timeout_within_limits(self) -> None:
        """GIVEN watchdog just created, WHEN check() is called immediately, THEN no timeout."""
        watchdog = acp.HeartbeatWatchdog()
        assert watchdog.check() is None

    def test_connect_timeout_before_first_result(self) -> None:
        """GIVEN no first result received AND idle beyond CONNECT_TIMEOUT_SEC but not INITIAL_IDLE, THEN connect timeout fires."""
        watchdog = acp.HeartbeatWatchdog()
        watchdog._first_result_received = False
        # Set last heartbeat to just beyond CONNECT_TIMEOUT but below INITIAL_IDLE
        # and start_time close enough that total is not exceeded
        now = watchdog._loop.time()
        idle = acp.CONNECT_TIMEOUT_SEC + 1
        watchdog._start_time = now - idle
        watchdog._last_heartbeat = now - idle
        result = watchdog.check()
        assert result is not None
        # Either connect timeout or initial_idle fires (both are correct for this range)
        assert "connect timeout" in result or "initial_idle" in result or "total timeout" in result

    def test_initial_idle_timeout(self) -> None:
        """GIVEN no first result AND idle beyond INITIAL_IDLE_TIMEOUT_SEC, THEN initial_idle fires."""
        watchdog = acp.HeartbeatWatchdog()
        watchdog._first_result_received = False
        # Force last heartbeat to be old enough
        watchdog._last_heartbeat = watchdog._loop.time() - acp.INITIAL_IDLE_TIMEOUT_SEC - 1
        result = watchdog.check()
        assert result is not None
        assert "initial_idle" in result

    def test_subsequent_idle_timeout(self) -> None:
        """GIVEN first result received AND idle beyond SUBSEQUENT_IDLE_TIMEOUT_SEC, THEN subsequent_idle fires."""
        watchdog = acp.HeartbeatWatchdog()
        watchdog._first_result_received = True
        watchdog._last_heartbeat = watchdog._loop.time() - acp.SUBSEQUENT_IDLE_TIMEOUT_SEC - 1
        result = watchdog.check()
        assert result is not None
        assert "subsequent_idle" in result

    def test_total_timeout(self) -> None:
        """GIVEN total elapsed exceeds TOTAL_TIMEOUT_SEC, THEN total timeout fires."""
        watchdog = acp.HeartbeatWatchdog()
        watchdog._start_time = watchdog._loop.time() - acp.TOTAL_TIMEOUT_SEC - 1
        result = watchdog.check()
        assert result is not None
        assert "total timeout" in result

    def test_heartbeat_resets_idle(self) -> None:
        """GIVEN subsequent_idle almost expired, WHEN heartbeat() is called, THEN no timeout."""
        watchdog = acp.HeartbeatWatchdog()
        watchdog._first_result_received = True
        watchdog._last_heartbeat = watchdog._loop.time() - acp.SUBSEQUENT_IDLE_TIMEOUT_SEC + 5
        # Should still be ok
        assert watchdog.check() is None
        # Reset it far in the past
        watchdog._last_heartbeat = watchdog._loop.time() - acp.SUBSEQUENT_IDLE_TIMEOUT_SEC - 1
        watchdog.heartbeat()
        assert watchdog.check() is None

    def test_four_timeout_constants_are_distinct(self) -> None:
        """GIVEN the 4 timeout constants, THEN they are all distinct and positive."""
        constants = [
            acp.CONNECT_TIMEOUT_SEC,
            acp.INITIAL_IDLE_TIMEOUT_SEC,
            acp.SUBSEQUENT_IDLE_TIMEOUT_SEC,
            acp.TOTAL_TIMEOUT_SEC,
        ]
        assert all(c > 0 for c in constants)
        assert len(set(constants)) == 4, "All 4 timeout constants must be distinct"

    def test_watchdog_class_attributes_match_constants(self) -> None:
        """GIVEN HeartbeatWatchdog class, THEN class attributes match module-level constants."""
        assert acp.HeartbeatWatchdog.CONNECT == acp.CONNECT_TIMEOUT_SEC
        assert acp.HeartbeatWatchdog.INITIAL_IDLE == acp.INITIAL_IDLE_TIMEOUT_SEC
        assert acp.HeartbeatWatchdog.SUBSEQUENT_IDLE == acp.SUBSEQUENT_IDLE_TIMEOUT_SEC
        assert acp.HeartbeatWatchdog.TOTAL == acp.TOTAL_TIMEOUT_SEC


# ---------------------------------------------------------------------------
# Permission proxy tests
# ---------------------------------------------------------------------------


class TestPermissionProxy:
    """GIVEN handle_request_permission, WHEN write ops are requested without approve_edits, THEN they are denied."""

    def test_write_file_denied_without_approve_edits(self) -> None:
        """GIVEN approve_edits=False, WHEN write_file permission is requested, THEN denied."""
        result = acp.handle_request_permission({"type": "write_file"}, approve_edits=False)
        assert result["granted"] is False
        assert "write operation" in result["reason"]
        assert "write_file" in result["reason"]

    def test_edit_file_denied_without_approve_edits(self) -> None:
        """GIVEN approve_edits=False, WHEN edit_file permission is requested, THEN denied."""
        result = acp.handle_request_permission({"type": "edit_file"}, approve_edits=False)
        assert result["granted"] is False

    def test_run_shell_command_denied_without_approve_edits(self) -> None:
        """GIVEN approve_edits=False, WHEN run_shell_command permission is requested, THEN denied."""
        result = acp.handle_request_permission({"type": "run_shell_command"}, approve_edits=False)
        assert result["granted"] is False

    def test_write_file_allowed_with_approve_edits(self) -> None:
        """GIVEN approve_edits=True, WHEN write_file permission is requested, THEN granted."""
        result = acp.handle_request_permission({"type": "write_file"}, approve_edits=True)
        assert result["granted"] is True

    def test_unknown_permission_type_allowed_without_approve_edits(self) -> None:
        """GIVEN approve_edits=False, WHEN an unknown permission type is requested, THEN granted."""
        result = acp.handle_request_permission({"type": "read_file"}, approve_edits=False)
        assert result["granted"] is True

    def test_all_write_types_in_permission_set(self) -> None:
        """GIVEN WRITE_PERMISSION_TYPES, THEN all known write operations are covered."""
        expected = {"write_file", "edit_file", "create_file", "delete_file", "run_shell_command", "execute_code"}
        assert expected.issubset(acp.WRITE_PERMISSION_TYPES)

    def test_empty_type_allowed(self) -> None:
        """GIVEN empty type string, THEN granted (not in write set)."""
        result = acp.handle_request_permission({}, approve_edits=False)
        assert result["granted"] is True


# ---------------------------------------------------------------------------
# Known bug detection tests
# ---------------------------------------------------------------------------


class TestKnownBugDetection:
    """GIVEN detect_known_bug_from_stderr, WHEN bug signals appear in stderr, THEN detected."""

    def test_auth_hang_detected(self) -> None:
        result = acp.detect_known_bug_from_stderr("error: refreshing credentials timed out")
        assert result == acp.AUTH_HANG_BUG

    def test_settings_hang_detected(self) -> None:
        result = acp.detect_known_bug_from_stderr("settings.json parsing hang detected")
        assert result == acp.SETTINGS_HANG_BUG

    def test_no_bug_in_normal_output(self) -> None:
        result = acp.detect_known_bug_from_stderr("Gemini is ready to help")
        assert result is None

    def test_empty_stderr(self) -> None:
        result = acp.detect_known_bug_from_stderr("")
        assert result is None


# ---------------------------------------------------------------------------
# Structured events extraction tests
# ---------------------------------------------------------------------------


class TestStructuredEventsExtraction:
    """GIVEN ACP session output, WHEN structured events are collected, THEN they appear in result."""

    def _make_notification(self, event_type: str, text: str) -> str:
        msg = {
            "jsonrpc": "2.0",
            "method": event_type,
            "params": {"type": event_type, "text": text},
        }
        return json.dumps(msg)

    def test_structured_event_types_set(self) -> None:
        """GIVEN STRUCTURED_EVENT_TYPES, THEN it contains the 3 required event types."""
        assert "AgentMessageChunk" in acp.STRUCTURED_EVENT_TYPES
        assert "AgentThoughtChunk" in acp.STRUCTURED_EVENT_TYPES
        assert "ToolCallStart" in acp.STRUCTURED_EVENT_TYPES


# ---------------------------------------------------------------------------
# Fallback logic tests
# ---------------------------------------------------------------------------


class TestFallbackToHeadlessJson:
    """GIVEN ACP failure, WHEN fallback_to_headless_json is called, THEN it delegates correctly."""

    def test_fallback_adds_warning(self) -> None:
        """GIVEN fallback invocation, WHEN headless_json succeeds, THEN warning is prepended."""
        fake_headless_result: dict[str, Any] = {
            "ok": True,
            "response_text": "fallback response",
            "warnings": [],
        }

        mock_module = types.ModuleType("run_gemini_headless")
        mock_module.run_delegation = MagicMock(return_value=fake_headless_result)  # type: ignore[attr-defined]

        with patch.dict(sys.modules, {"run_gemini_headless": mock_module}):
            result = acp._fallback_to_headless_json(
                request={"schema": "delegation_request_v1"},
                request_path=None,
                failure_reason="connect timeout (60s) — possible bug #22782",
            )

        assert result["ok"] is True
        assert result.get("_acp_fallback") is True
        assert any("fell back to headless_json" in w for w in result["warnings"])

    def test_fallback_invoked_on_early_failure(self) -> None:
        """GIVEN run_acp with initialize failure (failure_class), WHEN fallback is available, THEN fallback result returned."""
        fake_headless_result: dict[str, Any] = {
            "ok": True,
            "response_text": "headless response",
            "warnings": [],
            "schema": "delegation_result/v1",
            "transport": "headless_json",
        }

        mock_module = types.ModuleType("run_gemini_headless")
        mock_module.run_delegation = MagicMock(return_value=fake_headless_result)  # type: ignore[attr-defined]

        # Simulate ACP session that fails at initialize
        async def _failing_session(**kwargs: Any) -> dict[str, Any]:
            return {
                "ok": False,
                "structured_events": [],
                "response_text": None,
                "stderr": None,
                "warnings": ["connect timeout"],
                "failure_reason": "connect timeout (60s) waiting for initialize response",
                "failure_class": "initialize_failed",
            }

        with patch.object(acp, "_run_acp_session", side_effect=_failing_session):
            with patch.dict(sys.modules, {"run_gemini_headless": mock_module}):
                result = acp.run_acp(
                    request={"schema": "delegation_request_v1", "objective": "test"},
                    prepared_prompt="built prompt",
                )

        assert result.get("_acp_fallback") is True
        assert result["ok"] is True

    def test_fallback_not_invoked_on_late_failure(self) -> None:
        """GIVEN run_acp with a late session failure (prompt_error), THEN fallback is NOT invoked."""
        # B4: fallback is driven by failure_class, not substring matching.
        # prompt_error is a late failure → no fallback.

        async def _late_failing_session(**kwargs: Any) -> dict[str, Any]:
            return {
                "ok": False,
                "structured_events": [],
                "response_text": None,
                "stderr": None,
                "warnings": ["session/prompt error"],
                "failure_reason": "session/prompt error: model refused",
                "failure_class": "prompt_error",
            }

        with patch.object(acp, "_run_acp_session", side_effect=_late_failing_session):
            result = acp.run_acp(
                request={"schema": "delegation_request_v1", "objective": "test"},
                prepared_prompt="built prompt",
            )

        assert result.get("_acp_fallback") is None or result.get("_acp_fallback") is False
        assert result["ok"] is False
        assert result.get("failure_class") == "prompt_error"

    def test_fallback_driven_by_failure_class_not_keyword(self) -> None:
        """GIVEN a failure_reason mentioning 'initialize' but failure_class=prompt_error, THEN no fallback (B4)."""
        # Regression: old code matched the substring "initialize" anywhere in
        # failure_reason and wrongly fell back. With failure_class routing this
        # late failure must NOT fall back even though the reason text mentions
        # the word "initialize".
        async def _misleading_session(**kwargs: Any) -> dict[str, Any]:
            return {
                "ok": False,
                "structured_events": [],
                "response_text": None,
                "stderr": None,
                "warnings": [],
                "failure_reason": "session/prompt error: model failed to initialize its plan",
                "failure_class": "prompt_error",
            }

        with patch.object(acp, "_run_acp_session", side_effect=_misleading_session):
            result = acp.run_acp(
                request={"schema": "delegation_request_v1", "objective": "test"},
                prepared_prompt="built prompt",
            )

        assert result.get("_acp_fallback") is None or result.get("_acp_fallback") is False
        assert result["ok"] is False

    def test_fallback_classes_for_each_early_failure(self) -> None:
        """GIVEN each early failure_class, THEN fallback is invoked (B4)."""
        for fclass in (
            "gemini_not_found",
            "launch_failed",
            "initialize_failed",
            "session_new_failed",
        ):
            fake_headless_result: dict[str, Any] = {
                "ok": True,
                "response_text": "headless",
                "warnings": [],
            }
            mock_module = types.ModuleType("run_gemini_headless")
            mock_module.run_delegation = MagicMock(return_value=fake_headless_result)  # type: ignore[attr-defined]

            async def _failing(**kwargs: Any) -> dict[str, Any]:
                return {
                    "ok": False,
                    "structured_events": [],
                    "response_text": None,
                    "stderr": None,
                    "warnings": [],
                    "failure_reason": f"failed at {fclass}",
                    "failure_class": fclass,
                }

            with patch.object(acp, "_run_acp_session", side_effect=_failing):
                with patch.dict(sys.modules, {"run_gemini_headless": mock_module}):
                    result = acp.run_acp(
                        request={"schema": "delegation_request_v1", "objective": "x"},
                        prepared_prompt="built prompt",
                    )
            assert result.get("_acp_fallback") is True, f"{fclass} should fall back"


# ---------------------------------------------------------------------------
# run_acp integration-ish tests (no real gemini CLI)
# ---------------------------------------------------------------------------


class TestRunAcpResult:
    """GIVEN run_acp, WHEN session completes successfully, THEN result has expected shape."""

    def test_successful_session_result_shape(self) -> None:
        """GIVEN a successful ACP session mock, WHEN run_acp is called, THEN result has required keys."""
        async def _mock_session(**kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "structured_events": [
                    {"type": "AgentMessageChunk", "params": {"text": "PONG"}}
                ],
                "response_text": "PONG",
                "stderr": None,
                "warnings": [],
                "failure_reason": None,
            }

        with patch.object(acp, "_run_acp_session", side_effect=_mock_session):
            result = acp.run_acp({"objective": "reply with PONG"}, prepared_prompt="built prompt")

        assert result["ok"] is True
        assert result["schema"] == "acp_result_v1"
        assert result["transport"] == "acp"
        assert isinstance(result["structured_events"], list)
        assert len(result["structured_events"]) == 1
        assert result["structured_events"][0]["type"] == "AgentMessageChunk"
        assert result["response_text"] == "PONG"

    def test_gemini_not_found_returns_failure(self) -> None:
        """GIVEN gemini CLI not found, WHEN run_acp is called, THEN ok=False with failure_reason."""
        async def _not_found_session(**kwargs: Any) -> dict[str, Any]:
            return {
                "ok": False,
                "structured_events": [],
                "response_text": None,
                "stderr": None,
                "warnings": ["gemini CLI not found in PATH"],
                "failure_reason": "gemini CLI not found in PATH",
                "failure_class": "gemini_not_found",
            }

        # Patch fallback to also fail so we get the raw acp failure
        def _failing_fallback(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise ImportError("run_gemini_headless not available in test")

        with patch.object(acp, "_run_acp_session", side_effect=_not_found_session):
            with patch.object(acp, "_fallback_to_headless_json", side_effect=_failing_fallback):
                result = acp.run_acp({"objective": "test"}, prepared_prompt="built prompt")

        assert result["ok"] is False
        assert "gemini CLI not found" in (result.get("failure_reason") or "")

    def test_result_always_has_schema_and_transport(self) -> None:
        """GIVEN any run_acp call, THEN result always contains schema and transport fields."""
        async def _mock_session(**kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "structured_events": [],
                "response_text": "ok",
                "stderr": None,
                "warnings": [],
                "failure_reason": None,
            }

        with patch.object(acp, "_run_acp_session", side_effect=_mock_session):
            result = acp.run_acp({}, prepared_prompt="built prompt")

        assert "schema" in result
        assert "transport" in result
        assert result["transport"] == "acp"


# ---------------------------------------------------------------------------
# B1: prepared_prompt / model_override — delegation contract routing
# ---------------------------------------------------------------------------


class TestPreparedPromptAndModelOverride:
    """GIVEN run_acp with prepared_prompt / model_override, THEN they are honoured."""

    def test_prepared_prompt_is_used_verbatim(self) -> None:
        """GIVEN prepared_prompt, WHEN run_acp is called, THEN the session receives it unchanged."""
        captured: dict[str, Any] = {}

        async def _capturing_session(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "ok": True,
                "structured_events": [],
                "response_text": "ok",
                "stderr": None,
                "warnings": [],
                "failure_reason": None,
            }

        prepared = "FULLY BUILT PROMPT FROM build_prompt()"
        with patch.object(acp, "_run_acp_session", side_effect=_capturing_session):
            acp.run_acp(
                {"objective": "ignored", "instructions": ["ignored too"]},
                prepared_prompt=prepared,
            )

        assert captured["prompt"] == prepared

    def test_model_override_takes_precedence(self) -> None:
        """GIVEN model_override, WHEN run_acp is called, THEN session model uses the override."""
        captured: dict[str, Any] = {}

        async def _capturing_session(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "ok": True,
                "structured_events": [],
                "response_text": "ok",
                "stderr": None,
                "warnings": [],
                "failure_reason": None,
            }

        with patch.object(acp, "_run_acp_session", side_effect=_capturing_session):
            acp.run_acp(
                {"objective": "x", "model": "request-model"},
                prepared_prompt="built prompt",
                model_override="resolved-chain-model",
            )

        assert captured["model"] == "resolved-chain-model"

    def test_no_prepared_prompt_is_contract_bypass(self) -> None:
        """GIVEN no prepared_prompt, THEN run_acp fails closed (NB1: contract bypass)."""
        # NB1: building the prompt from objective/instructions inside run_acp
        # would bypass validate_request()/build_prompt(). run_acp must refuse.
        session_called = False

        async def _should_not_run(**kwargs: Any) -> dict[str, Any]:
            nonlocal session_called
            session_called = True
            return {"ok": True}

        with patch.object(acp, "_run_acp_session", side_effect=_should_not_run):
            result = acp.run_acp({"objective": "do thing", "instructions": ["step a"]})

        assert session_called is False
        assert result["ok"] is False
        assert result["failure_class"] == "contract_bypass"
        assert result["schema"] == "acp_result_v1"
        assert result["transport"] == "acp"


# ---------------------------------------------------------------------------
# B1: ACP path goes through validate_request — invalid requests fail before ACP
# ---------------------------------------------------------------------------


class TestAcpRoutesThroughDelegationContract:
    """GIVEN transport=acp via run_delegation, THEN validate_request runs before any ACP session."""

    def test_invalid_acp_request_fails_validation_before_acp(self) -> None:
        """GIVEN an invalid acp request, WHEN run_delegation is called, THEN it fails at validation and never reaches run_acp."""
        import run_gemini_headless as headless

        # Missing tool_profile / output_sections / context_files — invalid.
        invalid_request = {
            "schema": "delegation_request_v1",
            "transport": "acp",
            "objective": "do something specific and clear here",
        }

        with patch("run_gemini_acp.run_acp") as mock_run_acp:
            result = headless.run_delegation(invalid_request)

        # run_acp must NOT have been reached — validation failed first.
        mock_run_acp.assert_not_called()
        assert result["ok"] is False
        assert result["schema"] == "delegation_result/v1"
        assert result.get("failure_reason")

    def test_valid_acp_request_reaches_run_acp_after_build_prompt(self, tmp_path: Path) -> None:
        """GIVEN a valid acp request, WHEN run_delegation is called, THEN run_acp receives a prepared_prompt."""
        import run_gemini_headless as headless

        ctx = tmp_path / "ctx.txt"
        ctx.write_text("context content", encoding="utf-8")

        valid_request = {
            "schema": "delegation_request_v1",
            "transport": "acp",
            "objective": "Summarize the provided context file in two sentences",
            "instructions": ["Be concise.", "Do not invent facts."],
            "tool_profile": "no_tools",
            "output_sections": ["Summary"],
            "context_files": [str(ctx)],
            "model": "gemini-2.5-flash",
            "timeout_sec": 120,
        }

        captured: dict[str, Any] = {}

        def _fake_run_acp(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            captured["request"] = request
            return {"ok": True, "schema": "acp_result_v1", "transport": "acp"}

        with patch("run_gemini_acp.run_acp", side_effect=_fake_run_acp) as mock_run_acp:
            result = headless.run_delegation(valid_request, request_path=ctx)

        mock_run_acp.assert_called_once()
        # prepared_prompt was built by build_prompt() and passed through.
        assert captured.get("prepared_prompt")
        assert isinstance(captured["prepared_prompt"], str)
        # context content reached the prompt — proof the ACP path honoured context_files.
        assert "context content" in captured["prepared_prompt"]
        assert captured.get("model_override")
        assert result["transport"] == "acp"


# ---------------------------------------------------------------------------
# B5: EOF / process failure must not be treated as success
# ---------------------------------------------------------------------------


def _write_fake_acp_agent(tmp_path: Path, body: str) -> Path:
    """Write an executable fake ACP agent python script and return its path."""
    script = tmp_path / "fake_acp_agent.py"
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return script


# A fake agent that completes the full lifecycle and exits cleanly.
_FAKE_AGENT_OK = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys, uuid
    def send(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    def main():
        sid = str(uuid.uuid4())
        for _ in range(50):
            raw = sys.stdin.readline().strip()
            if not raw:
                break
            msg = json.loads(raw)
            method = msg.get("method", "")
            mid = msg.get("id")
            if method == "initialize":
                send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":1}})
            elif method == "session/new":
                send({"jsonrpc":"2.0","id":mid,"result":{"sessionId":sid}})
            elif method == "session/prompt":
                send({"jsonrpc":"2.0","method":"session/update","params":{
                    "sessionId":sid,
                    "update":{"sessionUpdate":"agent_message_chunk",
                              "content":{"type":"text","text":"PONG"}}}})
                send({"jsonrpc":"2.0","id":mid,"result":{"stopReason":"end_turn"}})
                return
    if __name__ == "__main__":
        main()
    """
)

# A fake agent that dies after session/new WITHOUT sending the final
# session/prompt response (EOF before final response).
_FAKE_AGENT_EOF = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys, uuid
    def send(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    def main():
        sid = str(uuid.uuid4())
        for _ in range(50):
            raw = sys.stdin.readline().strip()
            if not raw:
                break
            msg = json.loads(raw)
            method = msg.get("method", "")
            mid = msg.get("id")
            if method == "initialize":
                send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":1}})
            elif method == "session/new":
                send({"jsonrpc":"2.0","id":mid,"result":{"sessionId":sid}})
            elif method == "session/prompt":
                # Send a chunk then exit WITHOUT the final id-matching response.
                send({"jsonrpc":"2.0","method":"session/update","params":{
                    "sessionId":sid,
                    "update":{"sessionUpdate":"agent_message_chunk",
                              "content":{"type":"text","text":"partial"}}}})
                sys.exit(3)
    if __name__ == "__main__":
        main()
    """
)


class TestEofNotSuccess:
    """GIVEN an ACP agent that dies before the final response, THEN run_acp reports failure (B5)."""

    def test_clean_lifecycle_is_success(self, tmp_path: Path) -> None:
        """GIVEN a fake agent completing the full lifecycle, THEN ok=true."""
        agent = _write_fake_acp_agent(tmp_path, _FAKE_AGENT_OK)
        result = asyncio.run(
            acp._run_acp_session(
                prompt="ping",
                model="fake",
                approve_edits=False,
                timeout_sec=30,
                gemini_bin=str(agent),
            )
        )
        assert result["ok"] is True
        assert result["failure_reason"] is None
        assert result["failure_class"] is None
        assert result["response_text"] == "PONG"

    def test_eof_before_final_response_is_failure(self, tmp_path: Path) -> None:
        """GIVEN a fake agent that exits before the final session/prompt response, THEN ok=false."""
        agent = _write_fake_acp_agent(tmp_path, _FAKE_AGENT_EOF)
        result = asyncio.run(
            acp._run_acp_session(
                prompt="ping",
                model="fake",
                approve_edits=False,
                timeout_sec=30,
                gemini_bin=str(agent),
            )
        )
        assert result["ok"] is False
        assert result["failure_class"] == "protocol_error"
        assert result["failure_reason"] == "EOF before final session/prompt response"

    def test_gemini_not_found_failure_class(self) -> None:
        """GIVEN a nonexistent gemini binary, THEN failure_class=gemini_not_found (B4)."""
        result = asyncio.run(
            acp._run_acp_session(
                prompt="ping",
                model="fake",
                approve_edits=False,
                timeout_sec=10,
                gemini_bin="/nonexistent/gemini-absent-binary",
            )
        )
        assert result["ok"] is False
        assert result["failure_class"] == "gemini_not_found"


# ---------------------------------------------------------------------------
# B2: read-only clientCapabilities declared in initialize
# ---------------------------------------------------------------------------


class TestReadOnlyClientCapabilities:
    """GIVEN an ACP session, THEN the initialize request declares a read-only transport (B2)."""

    def test_initialize_declares_readonly_capabilities(self, tmp_path: Path) -> None:
        """GIVEN a fake agent that records the initialize params, THEN clientCapabilities are read-only."""
        recorder = tmp_path / "init_params.json"
        agent_body = textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, sys, uuid
            def send(o):
                sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
            def main():
                sid = str(uuid.uuid4())
                for _ in range(50):
                    raw = sys.stdin.readline().strip()
                    if not raw:
                        break
                    msg = json.loads(raw)
                    method = msg.get("method", "")
                    mid = msg.get("id")
                    if method == "initialize":
                        with open({str(recorder)!r}, "w") as fh:
                            json.dump(msg.get("params", {{}}), fh)
                        send({{"jsonrpc":"2.0","id":mid,"result":{{"protocolVersion":1}}}})
                    elif method == "session/new":
                        send({{"jsonrpc":"2.0","id":mid,"result":{{"sessionId":sid}}}})
                    elif method == "session/prompt":
                        send({{"jsonrpc":"2.0","id":mid,"result":{{"stopReason":"end_turn"}}}})
                        return
            if __name__ == "__main__":
                main()
            """
        )
        agent = _write_fake_acp_agent(tmp_path, agent_body)
        asyncio.run(
            acp._run_acp_session(
                prompt="ping",
                model="fake",
                approve_edits=False,
                timeout_sec=30,
                gemini_bin=str(agent),
            )
        )
        params = json.loads(recorder.read_text(encoding="utf-8"))
        caps = params.get("clientCapabilities", {})
        assert caps.get("fs", {}).get("readTextFile") is False
        assert caps.get("fs", {}).get("writeTextFile") is False
        assert caps.get("terminal") is False


# A fake agent that records the session/new params (model + cwd) for B2.
def _fake_agent_record_session_new(recorder: Path) -> str:
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import json, sys, uuid
        def send(o):
            sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
        def main():
            sid = str(uuid.uuid4())
            for _ in range(50):
                raw = sys.stdin.readline().strip()
                if not raw:
                    break
                msg = json.loads(raw)
                method = msg.get("method", "")
                mid = msg.get("id")
                if method == "initialize":
                    send({{"jsonrpc":"2.0","id":mid,"result":{{"protocolVersion":1}}}})
                elif method == "session/new":
                    with open({str(recorder)!r}, "w") as fh:
                        json.dump(msg.get("params", {{}}), fh)
                    send({{"jsonrpc":"2.0","id":mid,"result":{{"sessionId":sid}}}})
                elif method == "session/prompt":
                    send({{"jsonrpc":"2.0","method":"session/update","params":{{
                        "sessionId":sid,
                        "update":{{"sessionUpdate":"agent_message_chunk",
                                  "content":{{"type":"text","text":"PONG"}}}}}}}})
                    send({{"jsonrpc":"2.0","id":mid,"result":{{"stopReason":"end_turn"}}}})
                    return
        if __name__ == "__main__":
            main()
        """
    )


# ---------------------------------------------------------------------------
# B2: deterministic cwd — cwd_override drives both subprocess and session/new
# ---------------------------------------------------------------------------


class TestDeterministicCwd:
    """GIVEN cwd_override, THEN session/new.cwd uses it deterministically (B2)."""

    def test_cwd_override_used_for_session_new(self, tmp_path: Path) -> None:
        """GIVEN cwd_override, WHEN a session runs, THEN session/new.cwd equals it."""
        recorder = tmp_path / "session_new_params.json"
        agent = _write_fake_acp_agent(tmp_path, _fake_agent_record_session_new(recorder))
        override = str(tmp_path)
        asyncio.run(
            acp._run_acp_session(
                prompt="ping",
                model="fake",
                approve_edits=False,
                timeout_sec=30,
                gemini_bin=str(agent),
                cwd_override=override,
            )
        )
        params = json.loads(recorder.read_text(encoding="utf-8"))
        assert params.get("cwd") == override

    def test_cwd_override_none_falls_back_to_getcwd(self, tmp_path: Path) -> None:
        """GIVEN cwd_override=None, THEN session/new.cwd defaults to os.getcwd()."""
        import os

        recorder = tmp_path / "session_new_params.json"
        agent = _write_fake_acp_agent(tmp_path, _fake_agent_record_session_new(recorder))
        asyncio.run(
            acp._run_acp_session(
                prompt="ping",
                model="fake",
                approve_edits=False,
                timeout_sec=30,
                gemini_bin=str(agent),
                cwd_override=None,
            )
        )
        params = json.loads(recorder.read_text(encoding="utf-8"))
        assert params.get("cwd") == os.getcwd()


# A fake agent that completes the lifecycle but ends with a non-end_turn
# stopReason (B4: must not be treated as success).
_FAKE_AGENT_CANCEL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys, uuid
    def send(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    def main():
        sid = str(uuid.uuid4())
        for _ in range(50):
            raw = sys.stdin.readline().strip()
            if not raw:
                break
            msg = json.loads(raw)
            method = msg.get("method", "")
            mid = msg.get("id")
            if method == "initialize":
                send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":1}})
            elif method == "session/new":
                send({"jsonrpc":"2.0","id":mid,"result":{"sessionId":sid}})
            elif method == "session/prompt":
                send({"jsonrpc":"2.0","method":"session/update","params":{
                    "sessionId":sid,
                    "update":{"sessionUpdate":"agent_message_chunk",
                              "content":{"type":"text","text":"partial"}}}})
                send({"jsonrpc":"2.0","id":mid,"result":{"stopReason":"cancelled"}})
                return
    if __name__ == "__main__":
        main()
    """
)

# A fake agent that ends with end_turn but produces an empty response (B4).
_FAKE_AGENT_EMPTY = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys, uuid
    def send(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    def main():
        sid = str(uuid.uuid4())
        for _ in range(50):
            raw = sys.stdin.readline().strip()
            if not raw:
                break
            msg = json.loads(raw)
            method = msg.get("method", "")
            mid = msg.get("id")
            if method == "initialize":
                send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":1}})
            elif method == "session/new":
                send({"jsonrpc":"2.0","id":mid,"result":{"sessionId":sid}})
            elif method == "session/prompt":
                send({"jsonrpc":"2.0","id":mid,"result":{"stopReason":"end_turn"}})
                return
    if __name__ == "__main__":
        main()
    """
)


# ---------------------------------------------------------------------------
# B4: ok requires stopReason == end_turn AND a non-empty response
# ---------------------------------------------------------------------------


class TestStopReasonGatesOk:
    """GIVEN a completed session, THEN ok also depends on stopReason / response (B4)."""

    def test_end_turn_with_response_is_ok(self, tmp_path: Path) -> None:
        """GIVEN stopReason=end_turn AND non-empty response, THEN ok=true."""
        agent = _write_fake_acp_agent(tmp_path, _FAKE_AGENT_OK)
        result = asyncio.run(
            acp._run_acp_session(
                prompt="ping", model="fake", approve_edits=False,
                timeout_sec=30, gemini_bin=str(agent),
            )
        )
        assert result["transport_ok"] is True
        assert result["stop_reason"] == "end_turn"
        assert result["ok"] is True

    def test_non_end_turn_stop_reason_is_not_ok(self, tmp_path: Path) -> None:
        """GIVEN stopReason=cancelled, THEN ok=false / failure_class=incomplete_response."""
        agent = _write_fake_acp_agent(tmp_path, _FAKE_AGENT_CANCEL)
        result = asyncio.run(
            acp._run_acp_session(
                prompt="ping", model="fake", approve_edits=False,
                timeout_sec=30, gemini_bin=str(agent),
            )
        )
        assert result["transport_ok"] is True
        assert result["stop_reason"] == "cancelled"
        assert result["ok"] is False
        assert result["failure_class"] == "incomplete_response"
        assert "cancelled" in (result["failure_reason"] or "")

    def test_empty_response_is_not_ok(self, tmp_path: Path) -> None:
        """GIVEN end_turn but empty response_text, THEN ok=false / incomplete_response."""
        agent = _write_fake_acp_agent(tmp_path, _FAKE_AGENT_EMPTY)
        result = asyncio.run(
            acp._run_acp_session(
                prompt="ping", model="fake", approve_edits=False,
                timeout_sec=30, gemini_bin=str(agent),
            )
        )
        assert result["transport_ok"] is True
        assert result["stop_reason"] == "end_turn"
        assert result["ok"] is False
        assert result["failure_class"] == "incomplete_response"


# ---------------------------------------------------------------------------
# NB2: gemini_bin is honoured only via GEMINI_BIN env, not the request JSON
# ---------------------------------------------------------------------------


class TestGeminiBinEnvOnly:
    """GIVEN a gemini_bin field in the request, THEN it is ignored (NB2)."""

    def test_request_gemini_bin_field_is_ignored(self, monkeypatch: Any) -> None:
        """GIVEN request['gemini_bin'], THEN run_acp does not pass it to the session."""
        captured: dict[str, Any] = {}

        async def _capturing_session(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "ok": True, "structured_events": [], "response_text": "ok",
                "stderr": None, "warnings": [], "failure_reason": None,
            }

        monkeypatch.delenv("GEMINI_BIN", raising=False)
        with patch.object(acp, "_run_acp_session", side_effect=_capturing_session):
            acp.run_acp(
                {"objective": "x", "gemini_bin": "/evil/binary"},
                prepared_prompt="built prompt",
            )
        # request gemini_bin field must be ignored — default "gemini" used.
        assert captured["gemini_bin"] == "gemini"

    def test_env_gemini_bin_is_honoured(self, monkeypatch: Any) -> None:
        """GIVEN GEMINI_BIN env var, THEN run_acp passes it to the session."""
        captured: dict[str, Any] = {}

        async def _capturing_session(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "ok": True, "structured_events": [], "response_text": "ok",
                "stderr": None, "warnings": [], "failure_reason": None,
            }

        monkeypatch.setenv("GEMINI_BIN", "/custom/gemini-path")
        with patch.object(acp, "_run_acp_session", side_effect=_capturing_session):
            acp.run_acp({"objective": "x"}, prepared_prompt="built prompt")
        assert captured["gemini_bin"] == "/custom/gemini-path"


# ---------------------------------------------------------------------------
# NB3: known-bug stderr signatures must not misfire on normal logs
# ---------------------------------------------------------------------------


class TestKnownBugSignaturesAreSpecific:
    """GIVEN normal stderr lines, THEN detect_known_bug_from_stderr does not misfire (NB3)."""

    def test_plain_initialize_word_does_not_misfire(self) -> None:
        """GIVEN a benign log line mentioning 'initialize', THEN no bug detected."""
        assert acp.detect_known_bug_from_stderr("ACP client will initialize the session") is None

    def test_plain_settings_json_does_not_misfire(self) -> None:
        """GIVEN a benign log line mentioning settings.json, THEN no bug detected."""
        assert acp.detect_known_bug_from_stderr("loaded config from .gemini/settings.json") is None

    def test_specific_bug_phrase_still_detected(self) -> None:
        """GIVEN the specific bug phrase, THEN the bug is still detected."""
        assert (
            acp.detect_known_bug_from_stderr("initialize request never returned")
            == acp.INITIALIZE_HANG_BUG
        )

    def test_bug_number_is_detected(self) -> None:
        """GIVEN a stderr line containing the bug number, THEN the bug is detected."""
        assert (
            acp.detect_known_bug_from_stderr("known issue #18423 reproduced")
            == acp.SETTINGS_HANG_BUG
        )


# ---------------------------------------------------------------------------
# B3: ACP results are normalized to delegation_result/v1 by run_delegation
# ---------------------------------------------------------------------------


class TestAcpResultNormalizedToDelegationResult:
    """GIVEN a non-fallback ACP result, THEN run_delegation normalizes it (B3)."""

    def test_caller_can_read_result_surface_summary(self, tmp_path: Path) -> None:
        """GIVEN a successful ACP delegation, THEN result_surface.summary is readable."""
        import run_gemini_headless as headless

        ctx = tmp_path / "ctx.txt"
        ctx.write_text("context content", encoding="utf-8")

        valid_request = {
            "schema": "delegation_request_v1",
            "transport": "acp",
            "objective": "Summarize the provided context file in two sentences",
            "instructions": ["Be concise.", "Do not invent facts."],
            "tool_profile": "no_tools",
            "output_sections": ["Summary"],
            "context_files": [str(ctx)],
            "model": "gemini-2.5-flash",
            "timeout_sec": 120,
        }

        def _fake_run_acp(request: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "transport_ok": True,
                "stop_reason": "end_turn",
                "schema": "acp_result_v1",
                "transport": "acp",
                "structured_events": [{"type": "agent_message_chunk", "params": {}}],
                "response_text": "Summary line.\nDetailed second line.",
                "stderr": None,
                "warnings": [],
                "failure_reason": None,
                "failure_class": None,
            }

        with patch("run_gemini_acp.run_acp", side_effect=_fake_run_acp):
            result = headless.run_delegation(valid_request, request_path=ctx)

        # Normalized to delegation_result/v1 — caller reads result_surface.
        assert result["schema"] == "delegation_result/v1"
        assert result["transport"] == "acp"
        assert result["ok"] is True
        assert result["exit_code"] == 0
        assert "result_surface" in result
        assert result["result_surface"]["summary"] == "Summary line."
        assert result["result_surface"]["mode"] == "artifact-first"
        # acp-specific detail preserved under transport_details.
        assert result["transport_details"]["schema"] == "acp_result_v1"
        assert result["transport_details"]["stop_reason"] == "end_turn"
        assert len(result["transport_details"]["structured_events"]) == 1
        # delegation_result/v1 core fields present.
        assert result["requested_model"] == "gemini-2.5-flash"
        assert result["model_chain"]
        assert result["model_downgrades"] == []

    def test_fallback_result_not_double_normalized(self, tmp_path: Path) -> None:
        """GIVEN an _acp_fallback result, THEN run_delegation passes it through unchanged."""
        import run_gemini_headless as headless

        ctx = tmp_path / "ctx.txt"
        ctx.write_text("context content", encoding="utf-8")

        valid_request = {
            "schema": "delegation_request_v1",
            "transport": "acp",
            "objective": "Summarize the provided context file in two sentences",
            "instructions": ["Be concise.", "Do not invent facts."],
            "tool_profile": "no_tools",
            "output_sections": ["Summary"],
            "context_files": [str(ctx)],
            "model": "gemini-2.5-flash",
            "timeout_sec": 120,
        }

        fallback_result = {
            "schema": "delegation_result/v1",
            "transport": "headless_json",
            "_acp_fallback": True,
            "ok": True,
            "response_text": "from headless",
            "result_surface": {"mode": "artifact-first", "summary": "from headless"},
            "warnings": ["acp transport failed; fell back"],
        }

        with patch("run_gemini_acp.run_acp", return_value=fallback_result):
            result = headless.run_delegation(valid_request, request_path=ctx)

        # Passed through unchanged — markers preserved, no double-normalize.
        assert result["_acp_fallback"] is True
        assert result["transport"] == "headless_json"
        assert result["result_surface"]["summary"] == "from headless"
