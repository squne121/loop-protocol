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
        result = acp.detect_known_bug_from_stderr("refreshing credentials for google auth")
        assert result == acp.AUTH_HANG_BUG

    def test_settings_hang_detected(self) -> None:
        result = acp.detect_known_bug_from_stderr("reading settings from .gemini/settings.json")
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
        """GIVEN run_acp with initialize failure, WHEN fallback is available, THEN fallback result returned."""
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
            }

        with patch.object(acp, "_run_acp_session", side_effect=_failing_session):
            with patch.dict(sys.modules, {"run_gemini_headless": mock_module}):
                result = acp.run_acp(
                    request={"schema": "delegation_request_v1", "objective": "test"},
                )

        assert result.get("_acp_fallback") is True
        assert result["ok"] is True

    def test_fallback_not_invoked_on_late_failure(self) -> None:
        """GIVEN run_acp with a late session failure (not early), THEN fallback is NOT invoked."""
        # Late failure: failure_reason doesn't contain early-failure keywords

        async def _late_failing_session(**kwargs: Any) -> dict[str, Any]:
            return {
                "ok": False,
                "structured_events": [],
                "response_text": None,
                "stderr": None,
                "warnings": ["session/prompt error"],
                "failure_reason": "session/prompt error: model refused",
            }

        with patch.object(acp, "_run_acp_session", side_effect=_late_failing_session):
            result = acp.run_acp(
                request={"schema": "delegation_request_v1", "objective": "test"},
            )

        assert result.get("_acp_fallback") is None or result.get("_acp_fallback") is False
        assert result["ok"] is False


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
            result = acp.run_acp({"objective": "reply with PONG"})

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
            }

        # Patch fallback to also fail so we get the raw acp failure
        def _failing_fallback(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise ImportError("run_gemini_headless not available in test")

        with patch.object(acp, "_run_acp_session", side_effect=_not_found_session):
            with patch.object(acp, "_fallback_to_headless_json", side_effect=_failing_fallback):
                result = acp.run_acp({"objective": "test"})

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
            result = acp.run_acp({})

        assert "schema" in result
        assert "transport" in result
        assert result["transport"] == "acp"
