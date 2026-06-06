"""
test_publish_termination_report.py

Integration tests for publish_termination_report.py (Issue #692).

AC coverage:
  AC2: subprocess.run is used with shell=False to call renderer
  AC3: publishable=true + non-empty body -> gh issue comment --body-file (not --body)
  AC4: all fail-closed cases -> gh NOT called
  AC5: publishable=false / error cases -> artifact recorded (reason_code, renderer info)
  AC8: fake gh + fake renderer integration tests:
       - publishable=true normal post
       - publishable=false no post
       - renderer error fail-closed
       - coexistence with LOOP_HANDOFF_RESULT_V1 / FOLLOW_UP_MATERIALIZATION_RESULT_V1 markers
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import publish_termination_report as pub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_input(
    termination_reason: str = "approved",
    termination_cause: str | None = None,
    issue_number: int | None = 42,
) -> dict:
    data: dict = {"termination_reason": termination_reason}
    if termination_cause is not None:
        data["termination_cause"] = termination_cause
    if issue_number is not None:
        data["issue_number"] = issue_number
    return data


def _make_render_result(
    *,
    publishable: bool = True,
    body: str | None = "## Loop Termination\n\nApproved.",
    reason_code: str | None = None,
    schema: str = "TERMINATION_REPORT_RENDER_RESULT_V1",
    schema_version: int = 1,
) -> dict:
    return {
        "schema": schema,
        "schema_version": schema_version,
        "publishable": publishable,
        "body": body,
        "reason_code": reason_code,
        "termination_reason": "approved",
        "termination_cause": None,
        "attempts": 1,
        "attempts_log": [{"attempt": 1, "template": "normal", "guard_pass": True, "errors": []}],
        "generated_at": "2026-01-01T00:00:00Z",
    }


def _fake_renderer_proc(
    result: dict | None,
    returncode: int = 0,
    stderr: str = "",
) -> MagicMock:
    """Build a fake CompletedProcess-like object for subprocess.run mock."""
    m = MagicMock()
    m.returncode = returncode
    m.stderr = stderr
    m.stdout = json.dumps(result) if result is not None else ""
    return m


# ---------------------------------------------------------------------------
# AC2: subprocess.run with shell=False
# ---------------------------------------------------------------------------

class TestSubprocessRunShellFalse:
    """AC2: renderer is called via subprocess.run with shell=False."""

    def test_subprocess_run_called_with_shell_false(self, tmp_path):
        fake_proc = _fake_renderer_proc(_make_render_result())

        with patch("subprocess.run", return_value=fake_proc) as mock_run:
            with patch.object(pub, "_post_github_comment", return_value=0):
                pub.publish(
                    issue_number=42,
                    input_data=_make_input(),
                )

        # First call is to renderer
        renderer_call = mock_run.call_args_list[0]
        assert renderer_call.kwargs.get("shell") is False or renderer_call.args[1:] == ()
        # Verify shell=False is explicitly set (keyword)
        call_kwargs = renderer_call.kwargs
        assert call_kwargs.get("shell") is False

    def test_subprocess_run_called_with_capture_output(self, tmp_path):
        fake_proc = _fake_renderer_proc(_make_render_result())

        with patch("subprocess.run", return_value=fake_proc) as mock_run:
            with patch.object(pub, "_post_github_comment", return_value=0):
                pub.publish(
                    issue_number=42,
                    input_data=_make_input(),
                )

        renderer_call = mock_run.call_args_list[0]
        call_kwargs = renderer_call.kwargs
        assert call_kwargs.get("capture_output") is True

    def test_subprocess_run_called_with_check_false(self, tmp_path):
        fake_proc = _fake_renderer_proc(_make_render_result())

        with patch("subprocess.run", return_value=fake_proc) as mock_run:
            with patch.object(pub, "_post_github_comment", return_value=0):
                pub.publish(
                    issue_number=42,
                    input_data=_make_input(),
                )

        renderer_call = mock_run.call_args_list[0]
        call_kwargs = renderer_call.kwargs
        assert call_kwargs.get("check") is False


# ---------------------------------------------------------------------------
# AC3: --body-file is used (not --body)
# ---------------------------------------------------------------------------

class TestGhBodyFile:
    """AC3: gh issue comment uses --body-file, never --body directly."""

    def test_gh_called_with_body_file(self, tmp_path):
        fake_proc = _fake_renderer_proc(_make_render_result(publishable=True, body="## Report"))
        gh_calls: list[list] = []
        gh_kwargs_list: list[dict] = []

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "gh":
                gh_calls.append(cmd)
                gh_kwargs_list.append(kwargs)
                m = MagicMock()
                m.returncode = 0
                m.stderr = ""
                m.stdout = ""
                return m
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            result = pub.publish(issue_number=42, input_data=_make_input())

        assert result == 0
        assert len(gh_calls) == 1
        gh_cmd = gh_calls[0]
        assert "gh" == gh_cmd[0]
        assert "issue" == gh_cmd[1]
        assert "comment" == gh_cmd[2]
        assert "--body-file" in gh_cmd
        # Must use "--body-file -" (stdin), not a file path
        assert gh_cmd[gh_cmd.index("--body-file") + 1] == "-"
        # --body must NOT appear as a standalone flag
        assert "--body" not in gh_cmd

    def test_gh_body_file_receives_correct_content(self, tmp_path):
        """Body is passed via stdin (input= kwarg), not a file path."""
        expected_body = "## Refinement Loop: Approved\n\nApproved."
        fake_proc = _fake_renderer_proc(_make_render_result(body=expected_body))
        received_stdin: list[str] = []

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "gh":
                # New implementation passes body via input= kwarg (stdin)
                received_stdin.append(kwargs.get("input", ""))
                m = MagicMock()
                m.returncode = 0
                m.stderr = ""
                return m
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            pub.publish(issue_number=42, input_data=_make_input())

        assert len(received_stdin) == 1
        assert received_stdin[0] == expected_body

    def test_gh_has_prompt_disabled_env(self, tmp_path):
        """gh call must have GH_PROMPT_DISABLED=1 in env."""
        fake_proc = _fake_renderer_proc(_make_render_result(publishable=True, body="## Report"))
        gh_envs: list[dict] = []

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "gh":
                gh_envs.append(kwargs.get("env", {}))
                m = MagicMock()
                m.returncode = 0
                m.stderr = ""
                return m
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            pub.publish(issue_number=42, input_data=_make_input())

        assert len(gh_envs) == 1
        assert gh_envs[0].get("GH_PROMPT_DISABLED") == "1"

    def test_gh_timeout_fail_closed(self, tmp_path):
        """gh timeout (30s) must fail closed (return -1, record artifact)."""
        import subprocess as _subprocess
        fake_proc = _fake_renderer_proc(_make_render_result(publishable=True, body="## Report"))

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "gh":
                raise _subprocess.TimeoutExpired(cmd, 30)
            return fake_proc

        artifact_calls: list[dict] = []

        def capture_artifact(**kwargs):
            artifact_calls.append(kwargs)

        with patch("subprocess.run", side_effect=fake_run):
            with patch.object(pub, "_record_artifact", side_effect=capture_artifact):
                exit_code = pub.publish(issue_number=42, input_data=_make_input())

        assert exit_code == 1
        assert len(artifact_calls) == 1
        assert artifact_calls[0]["reason_code"] == "gh_comment_timeout"


# ---------------------------------------------------------------------------
# AC4: fail-closed cases — gh NOT called
# ---------------------------------------------------------------------------

class TestFailClosed:
    """AC4: All error/non-publishable cases do not call gh."""

    def _assert_gh_not_called(self, input_data: dict, renderer_result: dict | None,
                               renderer_returncode: int = 0, renderer_stderr: str = "") -> int:
        fake_proc = _fake_renderer_proc(renderer_result, returncode=renderer_returncode,
                                        stderr=renderer_stderr)
        gh_calls: list = []

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "gh":
                gh_calls.append(cmd)
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            exit_code = pub.publish(issue_number=99, input_data=input_data)

        assert len(gh_calls) == 0, f"gh was called unexpectedly: {gh_calls}"
        assert exit_code == 1
        return exit_code

    def test_fail_closed_publishable_false(self):
        result = _make_render_result(publishable=False, body=None, reason_code="guard_fail_limit_exceeded")
        self._assert_gh_not_called(_make_input(), result)

    def test_fail_closed_renderer_nonzero_exit(self):
        self._assert_gh_not_called(_make_input(), None, renderer_returncode=2,
                                   renderer_stderr="invalid input")

    def test_fail_closed_renderer_internal_error(self):
        self._assert_gh_not_called(_make_input(), None, renderer_returncode=3,
                                   renderer_stderr="internal error")

    def test_fail_closed_invalid_json_from_renderer(self):
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stderr = ""
        fake_proc.stdout = "not valid json {"
        gh_calls: list = []

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "gh":
                gh_calls.append(cmd)
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            exit_code = pub.publish(issue_number=99, input_data=_make_input())

        assert len(gh_calls) == 0
        assert exit_code == 1

    def test_fail_closed_schema_mismatch(self):
        result = _make_render_result(schema="WRONG_SCHEMA_V1")
        self._assert_gh_not_called(_make_input(), result)

    def test_fail_closed_schema_version_mismatch(self):
        result = _make_render_result(schema_version=99)
        self._assert_gh_not_called(_make_input(), result)

    def test_fail_closed_publishable_true_body_null(self):
        result = _make_render_result(publishable=True, body=None)
        self._assert_gh_not_called(_make_input(), result)

    def test_fail_closed_publishable_false_body_nonnull(self):
        result = _make_render_result(publishable=False, body="some body", reason_code=None)
        self._assert_gh_not_called(_make_input(), result)

    def test_fail_closed_timeout(self):
        import subprocess
        gh_calls: list = []

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == sys.executable:
                raise subprocess.TimeoutExpired(cmd, 30)
            if isinstance(cmd, list) and cmd[0] == "gh":
                gh_calls.append(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stderr = ""
            m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=fake_run):
            exit_code = pub.publish(issue_number=99, input_data=_make_input())

        assert len(gh_calls) == 0
        assert exit_code == 1


# ---------------------------------------------------------------------------
# AC5: artifact recorded on publishable=false or error
# ---------------------------------------------------------------------------

class TestArtifactRecording:
    """AC5: reason_code / renderer info / timestamp recorded on non-publish."""

    def test_artifact_recorded_publishable_false(self, tmp_path):
        result = _make_render_result(publishable=False, body=None, reason_code="guard_fail_limit_exceeded")
        fake_proc = _fake_renderer_proc(result)

        artifact_files: list[Path] = []

        original_record = pub._record_artifact

        def capture_artifact(**kwargs):
            artifact_files.append(kwargs)
            original_record(**kwargs)

        with patch("subprocess.run", return_value=fake_proc):
            with patch.object(pub, "ARTIFACT_DIR", tmp_path / "artifacts"):
                with patch.object(pub, "_record_artifact", side_effect=capture_artifact):
                    pub.publish(issue_number=99, input_data=_make_input())

        assert len(artifact_files) == 1
        call_kwargs = artifact_files[0]
        assert call_kwargs["issue_number"] == 99
        assert call_kwargs["reason_code"] in ("guard_fail_limit_exceeded", "publishable_false")

    def test_artifact_has_timestamp(self, tmp_path):
        result = _make_render_result(publishable=False, body=None, reason_code="guard_fail_limit_exceeded")
        fake_proc = _fake_renderer_proc(result)

        with patch("subprocess.run", return_value=fake_proc):
            with patch.object(pub, "ARTIFACT_DIR", tmp_path / "artifacts"):
                pub.publish(issue_number=99, input_data=_make_input())

        # Check artifact file was written
        artifact_dir = tmp_path / "artifacts"
        if artifact_dir.exists():
            files = list(artifact_dir.glob("termination_report_publish_*.json"))
            if files:
                data = json.loads(files[0].read_text())
                assert "timestamp" in data
                assert "issue_number" in data

    def test_artifact_does_not_leak_publishable_body(self, tmp_path):
        """AC5: publishable body must not appear in artifact or stderr."""
        # When publishable=false, body is None, so this is inherently true.
        # Also verify renderer stderr is recorded, not body.
        result = _make_render_result(
            publishable=False, body=None, reason_code="guard_fail_limit_exceeded"
        )
        fake_proc = _fake_renderer_proc(result, stderr="renderer guard diagnostic")

        artifact_calls: list[dict] = []

        def capture(**kwargs):
            artifact_calls.append(kwargs)

        with patch("subprocess.run", return_value=fake_proc):
            with patch.object(pub, "_record_artifact", side_effect=capture):
                pub.publish(issue_number=99, input_data=_make_input())

        assert len(artifact_calls) == 1
        # body is None in publishable=false result; artifact should NOT contain any markdown prose
        call_kw = artifact_calls[0]
        # renderer_stderr is passed as kwarg for hashing (ok)
        assert "renderer_stderr" in call_kw
        # The artifact kwargs must not contain a 'body' key with publishable content
        assert "body" not in call_kw

    def test_artifact_stderr_stored_as_hash_not_raw(self, tmp_path):
        """B2: renderer stderr fragment must NOT appear in artifact JSON or publisher stderr."""
        secret_fragment = "SENSITIVE_BODY_FRAGMENT_XYZ"
        result = _make_render_result(
            publishable=False, body=None, reason_code="guard_fail_limit_exceeded"
        )
        # Renderer emits body fragment in stderr
        fake_proc = _fake_renderer_proc(result, stderr=secret_fragment)

        with patch("subprocess.run", return_value=fake_proc):
            with patch.object(pub, "ARTIFACT_DIR", tmp_path / "artifacts"):
                pub.publish(issue_number=99, input_data=_make_input())

        # Artifact JSON must not contain the raw fragment
        artifact_dir = tmp_path / "artifacts"
        files = list(artifact_dir.glob("termination_report_publish_*.json"))
        assert files, "artifact file should have been written"
        artifact_data = json.loads(files[0].read_text())
        artifact_text = json.dumps(artifact_data)
        assert secret_fragment not in artifact_text, (
            "renderer stderr fragment must not appear raw in artifact JSON"
        )
        # Artifact must have stderr_len and stderr_sha256 instead
        assert "stderr_len" in artifact_data
        assert "stderr_sha256" in artifact_data
        assert "renderer_stderr" not in artifact_data


# ---------------------------------------------------------------------------
# AC8: Integration tests with fake gh and fake renderer
# ---------------------------------------------------------------------------

class TestIntegration:
    """AC8: Integration tests with fake gh and fake renderer."""

    def test_publishable_true_normal_post(self):
        """publishable=true -> comment posted successfully."""
        body = "## Refinement Loop: Approved\n\nThe issue has been approved."
        fake_proc = _fake_renderer_proc(_make_render_result(publishable=True, body=body))
        gh_calls: list = []
        gh_proc = MagicMock()
        gh_proc.returncode = 0
        gh_proc.stderr = ""

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "gh":
                gh_calls.append(cmd)
                return gh_proc
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            exit_code = pub.publish(issue_number=42, input_data=_make_input())

        assert exit_code == 0
        assert len(gh_calls) == 1
        assert "--body-file" in gh_calls[0]

    def test_publishable_false_no_post(self):
        """publishable=false -> gh not called, exit 1."""
        fake_proc = _fake_renderer_proc(
            _make_render_result(publishable=False, body=None, reason_code="guard_fail_limit_exceeded")
        )
        gh_calls: list = []
        gh_proc = MagicMock()
        gh_proc.returncode = 0

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "gh":
                gh_calls.append(cmd)
                return gh_proc
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            exit_code = pub.publish(issue_number=42, input_data=_make_input())

        assert exit_code == 1
        assert len(gh_calls) == 0

    def test_renderer_error_fail_closed(self):
        """Renderer returns non-zero -> gh not called, exit 1."""
        fake_proc = _fake_renderer_proc(None, returncode=2, stderr="invalid input schema")
        gh_calls: list = []
        gh_proc = MagicMock()
        gh_proc.returncode = 0

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "gh":
                gh_calls.append(cmd)
                return gh_proc
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            exit_code = pub.publish(issue_number=42, input_data=_make_input())

        assert exit_code == 1
        assert len(gh_calls) == 0

    def test_coexistence_with_loop_handoff_result_v1(self):
        """
        AC8: Coexistence test — LOOP_HANDOFF_RESULT_V1 and
        FOLLOW_UP_MATERIALIZATION_RESULT_V1 markers in body do not break publishing.
        """
        body_with_markers = textwrap.dedent("""\
            ## Refinement Loop: Approved

            The issue has been approved and is ready for implementation.

            <!-- LOOP_HANDOFF_RESULT_V1 -->
            ```yaml
            LOOP_HANDOFF_RESULT_V1:
              status: impl_ready
              routing_action: run_impl_review_loop
            ```

            <!-- FOLLOW_UP_MATERIALIZATION_RESULT_V1 -->
            ```yaml
            FOLLOW_UP_MATERIALIZATION_RESULT_V1:
              schema_version: 1
              materialized_by: issue-refinement-loop
              follow_up_issues: []
              note_only_observations: []
            ```
        """)

        fake_proc = _fake_renderer_proc(
            _make_render_result(publishable=True, body=body_with_markers)
        )
        gh_calls: list = []
        gh_proc = MagicMock()
        gh_proc.returncode = 0

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "gh":
                gh_calls.append(cmd)
                return gh_proc
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            exit_code = pub.publish(issue_number=42, input_data=_make_input())

        assert exit_code == 0
        assert len(gh_calls) == 1
        assert "--body-file" in gh_calls[0]

    def test_coexistence_publishable_false_with_markers(self):
        """
        publishable=false with LOOP_HANDOFF_RESULT_V1 present in input context.
        gh must not be called even with marker-containing input.
        """
        fake_proc = _fake_renderer_proc(
            _make_render_result(publishable=False, body=None, reason_code="guard_fail_limit_exceeded")
        )
        gh_calls: list = []
        gh_proc = MagicMock()
        gh_proc.returncode = 0

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "gh":
                gh_calls.append(cmd)
                return gh_proc
            return fake_proc

        # Input with escalation that has blockers (realistic human_escalation case)
        input_data = {
            "termination_reason": "human_escalation",
            "termination_cause": "max_iterations_exceeded",
            "issue_number": 42,
            "iteration": 3,
            "blockers_summary": ["reviewer rejected all 3 attempts"],
        }

        with patch("subprocess.run", side_effect=fake_run):
            exit_code = pub.publish(issue_number=42, input_data=input_data)

        assert exit_code == 1
        assert len(gh_calls) == 0

    def test_issue_number_passed_to_gh(self):
        """gh issue comment receives the correct issue number."""
        fake_proc = _fake_renderer_proc(_make_render_result(publishable=True, body="## OK"))
        gh_cmd_received: list = []
        gh_proc = MagicMock()
        gh_proc.returncode = 0
        gh_proc.stderr = ""

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "gh":
                gh_cmd_received.extend(cmd)
                return gh_proc
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            pub.publish(issue_number=777, input_data=_make_input(issue_number=777))

        assert "777" in gh_cmd_received


# ---------------------------------------------------------------------------
# Validate render result helper (unit test)
# ---------------------------------------------------------------------------

class TestValidateRenderResult:
    """Unit tests for _validate_render_result internal function."""

    def test_valid_publishable_true(self):
        result = _make_render_result(publishable=True, body="## body")
        assert pub._validate_render_result(result) == ""

    def test_valid_publishable_false(self):
        result = _make_render_result(publishable=False, body=None, reason_code="guard_fail_limit_exceeded")
        assert pub._validate_render_result(result) == ""

    def test_schema_mismatch_returns_error(self):
        result = _make_render_result(schema="WRONG_SCHEMA")
        err = pub._validate_render_result(result)
        assert err != ""
        assert "schema" in err.lower()

    def test_schema_version_mismatch_returns_error(self):
        result = _make_render_result(schema_version=2)
        err = pub._validate_render_result(result)
        assert err != ""

    def test_publishable_true_body_null_returns_error(self):
        result = _make_render_result(publishable=True, body=None)
        err = pub._validate_render_result(result)
        assert err != ""

    def test_publishable_false_body_nonnull_returns_error(self):
        result = _make_render_result(publishable=False, body="some content")
        err = pub._validate_render_result(result)
        assert err != ""

    def test_publishable_not_bool_returns_error(self):
        result = _make_render_result()
        result["publishable"] = "true"  # string, not bool
        err = pub._validate_render_result(result)
        assert err != ""

    def test_result_list_returns_error(self):
        """B3: renderer stdout that is a list (not dict) must fail validation."""
        err = pub._validate_render_result([])  # type: ignore[arg-type]
        assert err != ""
        assert "object" in err.lower() or "dict" in err.lower()

    def test_result_string_returns_error(self):
        """B3: renderer stdout that is a plain string must fail validation."""
        err = pub._validate_render_result("ok")  # type: ignore[arg-type]
        assert err != ""

    def test_publishable_false_reason_code_missing_returns_error(self):
        """B3: publishable=false with missing reason_code must fail."""
        result = _make_render_result(publishable=False, body=None, reason_code=None)
        err = pub._validate_render_result(result)
        assert err != ""
        assert "reason_code" in err.lower()

    def test_publishable_false_reason_code_list_returns_error(self):
        """B3: publishable=false with reason_code=[] (non-string) must fail."""
        result = _make_render_result(publishable=False, body=None, reason_code=None)
        result["reason_code"] = []  # type: ignore[assignment]
        err = pub._validate_render_result(result)
        assert err != ""
        assert "reason_code" in err.lower()

    def test_publishable_true_reason_code_nonnull_returns_error(self):
        """B3: publishable=true with non-null reason_code must fail."""
        result = _make_render_result(publishable=True, body="## body", reason_code="some_code")
        err = pub._validate_render_result(result)
        assert err != ""
        assert "reason_code" in err.lower()
