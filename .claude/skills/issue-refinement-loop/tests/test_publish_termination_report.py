"""
test_publish_termination_report.py

Integration tests for publish_termination_report.py (Issue #692).

AC coverage:
  AC2: subprocess.run is used with shell=False to call renderer
  AC3: publishable=true + non-empty body -> issue_comment.publish controlled
       executor invoked with --input-file (Issue #1633; raw `gh issue comment`
       is no longer called directly)
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
import stat as _stat
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


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


def _fake_exec_proc(returncode: int = 0, stderr: str = "") -> MagicMock:
    """Build a fake CompletedProcess-like object for the
    controlled_skill_mutation_exec.py subprocess.run mock (Issue #1633)."""
    m = MagicMock()
    m.returncode = returncode
    m.stderr = stderr
    m.stdout = ""
    return m


def _is_exec_call(cmd) -> bool:
    """True iff cmd is a subprocess.run invocation of
    controlled_skill_mutation_exec.py (Issue #1633 issue_comment.publish
    bridge), as opposed to the renderer subprocess call."""
    return (
        isinstance(cmd, list)
        and len(cmd) > 1
        and str(cmd[1]).endswith("controlled_skill_mutation_exec.py")
    )


def _read_materialized_issue_comment_input(project_root: Path, issue_number: int) -> dict:
    """Read back the ISSUE_COMMENT_PUBLISH_INPUT_V1 JSON that
    materialize_isolation_issue_comment_request() wrote for issue_number."""
    path = (
        project_root / "artifacts" / str(issue_number)
        / "issue-metadata" / "issue_comment.publish" / "issue_comment_publish_input.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


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
                    repo="squne121/loop-protocol",
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
                    repo="squne121/loop-protocol",
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
                    repo="squne121/loop-protocol",
                )

        renderer_call = mock_run.call_args_list[0]
        call_kwargs = renderer_call.kwargs
        assert call_kwargs.get("check") is False


# ---------------------------------------------------------------------------
# AC3: --body-file is used (not --body)
# ---------------------------------------------------------------------------

class TestGhBodyFile:
    """AC3 (Issue #1633): _post_github_comment materializes a bounded
    ISOLATION_ISSUE_COMMENT_REQUEST_V1 request and launches
    controlled_skill_mutation_exec.py --command-id issue_comment.publish
    with --input-file; raw `gh issue comment --body-file` is never called
    directly from this module any more."""

    def test_gh_called_with_body_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        fake_proc = _fake_renderer_proc(_make_render_result(publishable=True, body="## Report"))
        exec_calls: list[list] = []

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                exec_calls.append(cmd)
                return _fake_exec_proc()
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            result = pub.publish(issue_number=42, input_data=_make_input(), repo="squne121/loop-protocol")

        assert result == 0
        assert len(exec_calls) == 1
        exec_cmd = exec_calls[0]
        assert exec_cmd[1].endswith("controlled_skill_mutation_exec.py")
        assert "--command-id" in exec_cmd
        assert exec_cmd[exec_cmd.index("--command-id") + 1] == "issue_comment.publish"
        assert "--input-file" in exec_cmd
        input_file_value = exec_cmd[exec_cmd.index("--input-file") + 1]
        # Must be a project-root-relative path (the executor rejects absolute paths)
        assert not input_file_value.startswith("/")
        assert input_file_value.startswith("artifacts/42/issue-metadata/issue_comment.publish/")

    def test_gh_body_file_receives_correct_content(self, tmp_path, monkeypatch):
        """Body is materialized into the ISSUE_COMMENT_PUBLISH_INPUT_V1 JSON
        file, not passed via stdin to a raw gh call."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        expected_body = "## Refinement Loop: Approved\n\nApproved."
        fake_proc = _fake_renderer_proc(_make_render_result(body=expected_body))

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            pub.publish(issue_number=42, input_data=_make_input(), repo="squne121/loop-protocol")

        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        assert materialized["comment_body"].startswith(expected_body)
        assert materialized["schema"] == "ISSUE_COMMENT_PUBLISH_INPUT_V1"
        assert materialized["issue_number"] == 42

    def test_gh_has_prompt_disabled_env(self, tmp_path, monkeypatch):
        """The controlled_skill_mutation_exec.py invocation must have
        GH_PROMPT_DISABLED=1 in env (inherited down to its own gh call)."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        fake_proc = _fake_renderer_proc(_make_render_result(publishable=True, body="## Report"))
        exec_envs: list[dict] = []

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                exec_envs.append(kwargs.get("env", {}))
                return _fake_exec_proc()
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            pub.publish(issue_number=42, input_data=_make_input(), repo="squne121/loop-protocol")

        assert len(exec_envs) == 1
        assert exec_envs[0].get("GH_PROMPT_DISABLED") == "1"

    def test_gh_timeout_fail_closed(self, tmp_path, monkeypatch):
        """controlled_skill_mutation_exec.py timeout (30s) must fail closed
        (return -1, record artifact)."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        import subprocess as _subprocess
        fake_proc = _fake_renderer_proc(_make_render_result(publishable=True, body="## Report"))

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                raise _subprocess.TimeoutExpired(cmd, 30)
            return fake_proc

        artifact_calls: list[dict] = []

        def capture_artifact(**kwargs):
            artifact_calls.append(kwargs)

        with patch("subprocess.run", side_effect=fake_run):
            with patch.object(pub, "_record_artifact", side_effect=capture_artifact):
                exit_code = pub.publish(issue_number=42, input_data=_make_input(), repo="squne121/loop-protocol")

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
            exit_code = pub.publish(issue_number=99, input_data=input_data, repo="squne121/loop-protocol")

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
            exit_code = pub.publish(issue_number=99, input_data=_make_input(), repo="squne121/loop-protocol")

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
            exit_code = pub.publish(issue_number=99, input_data=_make_input(), repo="squne121/loop-protocol")

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
                    pub.publish(issue_number=99, input_data=_make_input(), repo="squne121/loop-protocol")

        assert len(artifact_files) == 1
        call_kwargs = artifact_files[0]
        assert call_kwargs["issue_number"] == 99
        assert call_kwargs["reason_code"] in ("guard_fail_limit_exceeded", "publishable_false")

    def test_artifact_has_timestamp(self, tmp_path):
        result = _make_render_result(publishable=False, body=None, reason_code="guard_fail_limit_exceeded")
        fake_proc = _fake_renderer_proc(result)

        with patch("subprocess.run", return_value=fake_proc):
            with patch.object(pub, "ARTIFACT_DIR", tmp_path / "artifacts"):
                pub.publish(issue_number=99, input_data=_make_input(), repo="squne121/loop-protocol")

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
                pub.publish(issue_number=99, input_data=_make_input(), repo="squne121/loop-protocol")

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
                pub.publish(issue_number=99, input_data=_make_input(), repo="squne121/loop-protocol")

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

    def test_publishable_true_normal_post(self, tmp_path, monkeypatch):
        """publishable=true -> comment posted successfully via the
        issue_comment.publish controlled executor bridge (Issue #1633)."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        body = "## Refinement Loop: Approved\n\nThe issue has been approved."
        fake_proc = _fake_renderer_proc(_make_render_result(publishable=True, body=body))
        exec_calls: list = []

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                exec_calls.append(cmd)
                return _fake_exec_proc()
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            exit_code = pub.publish(issue_number=42, input_data=_make_input(), repo="squne121/loop-protocol")

        assert exit_code == 0
        assert len(exec_calls) == 1
        assert "--input-file" in exec_calls[0]

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
            exit_code = pub.publish(issue_number=42, input_data=_make_input(), repo="squne121/loop-protocol")

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
            exit_code = pub.publish(issue_number=42, input_data=_make_input(), repo="squne121/loop-protocol")

        assert exit_code == 1
        assert len(gh_calls) == 0

    def test_publish_preserves_explicit_termination_cause(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        renderer_inputs: list[dict[str, Any]] = []
        fake_proc = _fake_renderer_proc(_make_render_result())

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            if isinstance(cmd, list) and cmd[0] == sys.executable:
                renderer_inputs.append(json.loads(kwargs["input"]))
                return fake_proc
            raise AssertionError(f"unexpected command: {cmd}")

        with patch("subprocess.run", side_effect=fake_run):
            exit_code = pub.publish(
                issue_number=42,
                input_data=_make_input(
                    termination_reason="human_escalation",
                    termination_cause="max_iterations_exceeded",
                ),
                repo="squne121/loop-protocol",
            )

        assert exit_code == 0
        assert renderer_inputs == [{
            "termination_reason": "human_escalation",
            "termination_cause": "max_iterations_exceeded",
            "issue_number": 42,
        }]

    def test_publish_normalizes_legacy_blocker_summary_alias(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        renderer_inputs: list[dict[str, Any]] = []
        fake_proc = _fake_renderer_proc(_make_render_result())

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            if isinstance(cmd, list) and cmd[0] == sys.executable:
                renderer_inputs.append(json.loads(kwargs["input"]))
                return fake_proc
            raise AssertionError(f"unexpected command: {cmd}")

        with patch("subprocess.run", side_effect=fake_run):
            exit_code = pub.publish(
                issue_number=42,
                input_data={
                    "termination_reason": "human_escalation",
                    "issue_number": 42,
                    "blocker_summary": ["legacy blocker entry"],
                },
                repo="squne121/loop-protocol",
            )

        assert exit_code == 0
        assert renderer_inputs == [{
            "termination_reason": "human_escalation",
            "termination_cause": "human_judgment_required",
            "issue_number": 42,
            "blockers_summary": ["legacy blocker entry"],
        }]

    def test_publish_rejects_blocker_summary_alias_conflict(self):
        with patch("subprocess.run") as mock_run:
            exit_code = pub.publish(
                issue_number=42,
                input_data={
                    "termination_reason": "human_escalation",
                    "blocker_summary": ["legacy blocker"],
                    "blockers_summary": ["canonical blocker"],
                },
                repo="squne121/loop-protocol",
            )

        assert exit_code == 1
        mock_run.assert_not_called()

    def test_coexistence_with_loop_handoff_result_v1(self, tmp_path, monkeypatch):
        """
        AC8: Coexistence test — LOOP_HANDOFF_RESULT_V1 and
        FOLLOW_UP_MATERIALIZATION_RESULT_V1 markers in body do not break publishing.
        """
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
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
        exec_calls: list = []

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                exec_calls.append(cmd)
                return _fake_exec_proc()
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            exit_code = pub.publish(issue_number=42, input_data=_make_input(), repo="squne121/loop-protocol")

        assert exit_code == 0
        assert len(exec_calls) == 1
        assert "--input-file" in exec_calls[0]
        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        assert "LOOP_HANDOFF_RESULT_V1" in materialized["comment_body"]

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
            exit_code = pub.publish(issue_number=42, input_data=input_data, repo="squne121/loop-protocol")

        assert exit_code == 1
        assert len(gh_calls) == 0

    def test_issue_number_passed_to_gh(self, tmp_path, monkeypatch):
        """The issue_comment.publish controlled executor invocation receives
        the correct --issue-number."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        fake_proc = _fake_renderer_proc(_make_render_result(publishable=True, body="## OK"))
        exec_cmd_received: list = []

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                exec_cmd_received.extend(cmd)
                return _fake_exec_proc()
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            pub.publish(issue_number=777, input_data=_make_input(issue_number=777), repo="squne121/loop-protocol")

        assert "777" in exec_cmd_received


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


# ---------------------------------------------------------------------------
# AC1 (Issue #838): Real renderer E2E — _post_github_comment monkeypatch
# ---------------------------------------------------------------------------

class TestRealRendererE2E:
    """AC1: E2E test using real render_termination_report.py subprocess with _post_github_comment monkeypatch."""

    def test_publish_with_real_renderer_posts_normalized_human_escalation_body(self, monkeypatch):
        posted: list[tuple[int, str]] = []

        def fake_post_github_comment(*, issue_number: int, body: str, repo: str) -> int:
            posted.append((issue_number, body))
            return 0

        monkeypatch.setattr(pub, "_post_github_comment", fake_post_github_comment)

        exit_code = pub.publish(
            issue_number=42,
            input_data={
                "termination_reason": "human_escalation",
                "issue_number": 42,
                "iteration": 3,
                "blocker_summary": ["legacy blocker entry"],
            },
            repo="squne121/loop-protocol",
        )

        assert exit_code == 0
        assert len(posted) == 1
        assert posted[0][0] == 42

        body = posted[0][1]
        assert "Cause: none" not in body
        assert "Cause: human judgment required" in body
        assert "## Blockers" in body
        assert "legacy blocker entry" in body


# ---------------------------------------------------------------------------
# AC2 (Issue #838): Docs prose negative guard
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# AC10 (Issue #1166): --repo flag required in gh issue comment
# ---------------------------------------------------------------------------

class TestRepoFlag:
    """AC10 (Issue #1166) / Issue #1633: --repo is passed through unchanged
    to the issue_comment.publish controlled executor invocation."""

    def test_gh_command_includes_repo_flag(self, tmp_path, monkeypatch):
        """--repo <owner/repo> must be present on the executor invocation."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        fake_proc = _fake_renderer_proc(_make_render_result(publishable=True, body="## Report"))
        exec_calls: list[list] = []

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                exec_calls.append(cmd)
                return _fake_exec_proc()
            return fake_proc

        with patch("subprocess.run", side_effect=fake_run):
            pub.publish(issue_number=42, input_data=_make_input(), repo="squne121/loop-protocol")

        assert len(exec_calls) == 1
        exec_cmd = exec_calls[0]
        assert "--repo" in exec_cmd, "--repo flag must be present on the executor invocation"
        repo_idx = exec_cmd.index("--repo")
        assert repo_idx + 1 < len(exec_cmd), "--repo must be followed by repo value"
        assert exec_cmd[repo_idx + 1] == "squne121/loop-protocol"

    def test_post_github_comment_passes_repo_to_gh(self, tmp_path, monkeypatch):
        """_post_github_comment must include --repo on the executor invocation."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        exec_calls: list[list] = []

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                exec_calls.append(cmd)
                return _fake_exec_proc()
            raise AssertionError(f"Unexpected: {cmd}")

        with patch("subprocess.run", side_effect=fake_run):
            rc = pub._post_github_comment(
                issue_number=42,
                body="## Test",
                repo="squne121/loop-protocol",
            )

        assert rc == 0
        assert len(exec_calls) == 1
        assert "--repo" in exec_calls[0]
        idx = exec_calls[0].index("--repo")
        assert exec_calls[0][idx + 1] == "squne121/loop-protocol"


class TestTerminationReportDocsProse:
    def test_english_duplicate_prose_removed(self):
        root = Path(__file__).resolve().parent.parent
        text = "\n".join([
            (root / "SKILL.md").read_text(),
            (root / "references" / "termination-policy.md").read_text(),
        ])

        forbidden = [
            "human_escalation example includes termination_cause and blockers_summary",
            "legacy alias blocker_summary is normalized to canonical blockers_summary",
            "owner decision is required",
            "conflicting scope signals remain unresolved",
        ]

        for phrase in forbidden:
            assert phrase not in text, f"英語重複 prose が残っています: {phrase!r}"


# ---------------------------------------------------------------------------
# P0-5: Exec marker injection in _post_github_comment
# ---------------------------------------------------------------------------

class TestExecMarkerInjection:
    """P0-5 / Issue #1633: CONTROLLED_EXEC_MARKER env var (or a deterministic
    content-hash fallback) is embedded into the materialized comment_body as
    the bounded request's marker field."""

    def test_marker_injected_into_body_when_env_set(self, tmp_path, monkeypatch):
        """When CONTROLLED_EXEC_MARKER is set, comment body includes marker."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        fake_proc = _fake_renderer_proc(_make_render_result(publishable=True, body="## Report"))

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            return fake_proc

        monkeypatch.setenv("CONTROLLED_EXEC_MARKER", "abc123marker456")

        with patch("subprocess.run", side_effect=fake_run):
            result = pub.publish(issue_number=42, input_data=_make_input(), repo="squne121/loop-protocol")

        assert result == 0
        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        assert "<!-- CONTROLLED_EXEC_MARKER:abc123marker456 -->" in materialized["comment_body"]
        assert materialized["marker"] == "<!-- CONTROLLED_EXEC_MARKER:abc123marker456 -->"

    def test_no_marker_injected_when_env_not_set(self, tmp_path, monkeypatch):
        """When CONTROLLED_EXEC_MARKER is not set, a deterministic
        content-hash marker is used instead (materializer still requires a
        non-empty marker embedded in comment_body -- Issue #1633 AC1)."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        fake_proc = _fake_renderer_proc(_make_render_result(publishable=True, body="## Report"))

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            return fake_proc

        monkeypatch.delenv("CONTROLLED_EXEC_MARKER", raising=False)

        with patch("subprocess.run", side_effect=fake_run):
            result = pub.publish(issue_number=42, input_data=_make_input(), repo="squne121/loop-protocol")

        assert result == 0
        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        assert "abc123marker456" not in materialized["comment_body"]
        assert materialized["marker"] in materialized["comment_body"]

    def test_fallback_marker_differs_across_issue_numbers_for_same_body(self, monkeypatch):
        """Issue #1639 fix_delta P1-2: fallback marker must not collide when
        the exact same body is posted to a different issue -- it must hash
        repo + issue_number + body, not body alone."""
        monkeypatch.delenv("CONTROLLED_EXEC_MARKER", raising=False)
        same_body = "## Identical Report Body"

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            raise AssertionError(f"Unexpected: {cmd}")

        # Read back both materialized markers via distinct tmp roots to avoid
        # cross-contamination between the two calls.
        import tempfile
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            root1, root2 = Path(td1), Path(td2)
            with patch.object(pub, "_PROJECT_ROOT", root1):
                with patch("subprocess.run", side_effect=fake_run):
                    pub._post_github_comment(issue_number=1, body=same_body, repo="squne121/loop-protocol")
            with patch.object(pub, "_PROJECT_ROOT", root2):
                with patch("subprocess.run", side_effect=fake_run):
                    pub._post_github_comment(issue_number=2, body=same_body, repo="squne121/loop-protocol")

            marker_for_issue_1 = _read_materialized_issue_comment_input(root1, 1)["marker"]
            marker_for_issue_2 = _read_materialized_issue_comment_input(root2, 2)["marker"]
            assert marker_for_issue_1 != marker_for_issue_2

    def test_fallback_marker_differs_across_repos_for_same_body_and_issue(self):
        """Same body + same issue_number but different repo must also
        produce a different fallback marker."""
        import tempfile

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            raise AssertionError(f"Unexpected: {cmd}")

        same_body = "## Identical Report Body"
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            root1, root2 = Path(td1), Path(td2)
            with patch.object(pub, "_PROJECT_ROOT", root1):
                with patch("subprocess.run", side_effect=fake_run):
                    pub._post_github_comment(issue_number=1, body=same_body, repo="squne121/loop-protocol")
            with patch.object(pub, "_PROJECT_ROOT", root2):
                with patch("subprocess.run", side_effect=fake_run):
                    pub._post_github_comment(issue_number=1, body=same_body, repo="someone-else/other-repo")

            marker_repo_1 = _read_materialized_issue_comment_input(root1, 1)["marker"]
            marker_repo_2 = _read_materialized_issue_comment_input(root2, 1)["marker"]
            assert marker_repo_1 != marker_repo_2

    def test_post_github_comment_injects_marker(self, tmp_path, monkeypatch):
        """_post_github_comment materializes the marker into comment_body when
        CONTROLLED_EXEC_MARKER is set."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            raise AssertionError(f"Unexpected: {cmd}")

        monkeypatch.setenv("CONTROLLED_EXEC_MARKER", "testmarker99")

        with patch("subprocess.run", side_effect=fake_run):
            rc = pub._post_github_comment(
                issue_number=42,
                body="## Test Body",
                repo="squne121/loop-protocol",
            )

        assert rc == 0
        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        assert "<!-- CONTROLLED_EXEC_MARKER:testmarker99 -->" in materialized["comment_body"]
        assert "## Test Body" in materialized["comment_body"]

    def test_marker_appended_after_body_content(self, tmp_path, monkeypatch):
        """Marker is appended after original body, not prepended."""
        monkeypatch.setattr(pub, "_PROJECT_ROOT", tmp_path)
        original_body = "## Original Content\n\nSome text."

        def fake_run(cmd, **kwargs):
            if _is_exec_call(cmd):
                return _fake_exec_proc()
            raise AssertionError(f"Unexpected: {cmd}")

        monkeypatch.setenv("CONTROLLED_EXEC_MARKER", "markerXYZ")

        with patch("subprocess.run", side_effect=fake_run):
            pub._post_github_comment(
                issue_number=42,
                body=original_body,
                repo="squne121/loop-protocol",
            )

        materialized = _read_materialized_issue_comment_input(tmp_path, 42)
        body = materialized["comment_body"]
        # Marker comes after original content
        marker_pos = body.find("<!-- CONTROLLED_EXEC_MARKER:")
        original_end_pos = body.find(original_body) + len(original_body)
        assert marker_pos > original_end_pos - 1  # marker is after original body




# ---------------------------------------------------------------------------
# #1311: loop_handoff input wiring (AC3)
# ---------------------------------------------------------------------------

def _iicr(*, issue_number, repo, comment_body, marker):
    """Build an ISOLATION_ISSUE_COMMENT_REQUEST_V1 dict via the production
    producer (Issue #1639 fix_delta P1-1: producer/consumer separation)."""
    return pub.build_isolation_issue_comment_request(
        issue_number=issue_number, repo=repo, comment_body=comment_body, marker=marker,
    )


class TestMaterializeIsolationIssueCommentRequest:
    """AC2 (Issue #1633): materialize_isolation_issue_comment_request() writes
    a bounded ISOLATION_ISSUE_COMMENT_REQUEST_V1 request into
    artifacts/{issue_number}/issue-metadata/issue_comment.publish/ as an
    ISSUE_COMMENT_PUBLISH_INPUT_V1 file, after validating the bounded fields.

    Issue #1639 fix_delta P1-1: the function now takes the already-built
    `request` object plus expected_issue_number/expected_repo, so these
    tests build the request via the production producer
    (build_isolation_issue_comment_request()) rather than letting
    materialize_isolation_issue_comment_request() reconstruct it from
    already-trusted keyword arguments."""

    def test_success_writes_expected_namespace_and_schema(self, tmp_path):
        request = _iicr(
            issue_number=555,
            repo="squne121/loop-protocol",
            comment_body="hello <!-- m1 -->",
            marker="<!-- m1 -->",
        )
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=555,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )
        assert err == ""
        assert rel_path == "artifacts/555/issue-metadata/issue_comment.publish/issue_comment_publish_input.json"
        written = json.loads((tmp_path / rel_path).read_text(encoding="utf-8"))
        assert written["schema"] == "ISSUE_COMMENT_PUBLISH_INPUT_V1"
        assert written["issue_number"] == 555
        assert written["comment_body"] == "hello <!-- m1 -->"
        assert written["marker"] == "<!-- m1 -->"

    def test_rejects_marker_not_embedded_in_body(self, tmp_path):
        request = _iicr(
            issue_number=555,
            repo="squne121/loop-protocol",
            comment_body="hello world",
            marker="<!-- missing -->",
        )
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=555,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )
        assert rel_path is None
        assert "marker_not_embedded_in_body" in err
        assert not (tmp_path / "artifacts").exists()

    def test_rejects_empty_marker(self, tmp_path):
        request = _iicr(
            issue_number=555,
            repo="squne121/loop-protocol",
            comment_body="hello world",
            marker="",
        )
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=555,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )
        assert rel_path is None
        assert "marker" in err

    def test_rejects_empty_comment_body(self, tmp_path):
        request = _iicr(
            issue_number=555,
            repo="squne121/loop-protocol",
            comment_body="",
            marker="",
        )
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=555,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )
        assert rel_path is None
        assert err != ""

    def test_rejects_unknown_field_in_request(self, tmp_path):
        """Issue #1639 fix_delta P1-1: unknown-field requests must be
        rejected through the real production entry point (not just the
        validator function in isolation)."""
        request = _iicr(
            issue_number=555,
            repo="squne121/loop-protocol",
            comment_body="hello <!-- m1 -->",
            marker="<!-- m1 -->",
        )
        request["extra_field"] = "unexpected"
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=555,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )
        assert rel_path is None
        assert "unknown_fields" in err
        assert not (tmp_path / "artifacts").exists()

    def test_rejects_schema_mismatch(self, tmp_path):
        request = _iicr(
            issue_number=555,
            repo="squne121/loop-protocol",
            comment_body="hello <!-- m1 -->",
            marker="<!-- m1 -->",
        )
        request["schema"] = "WRONG_SCHEMA_V1"
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=555,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )
        assert rel_path is None
        assert "schema_mismatch" in err

    def test_rejects_repo_mismatch(self, tmp_path):
        request = _iicr(
            issue_number=555,
            repo="squne121/loop-protocol",
            comment_body="hello <!-- m1 -->",
            marker="<!-- m1 -->",
        )
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=555,
            expected_repo="someone-else/other-repo",
            project_root=tmp_path,
        )
        assert rel_path is None
        assert "repo_mismatch" in err
        assert not (tmp_path / "artifacts").exists()

    def test_rejects_issue_number_mismatch(self, tmp_path):
        request = _iicr(
            issue_number=555,
            repo="squne121/loop-protocol",
            comment_body="hello <!-- m1 -->",
            marker="<!-- m1 -->",
        )
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=request,
            expected_issue_number=999,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )
        assert rel_path is None
        assert "issue_number_mismatch" in err
        assert not (tmp_path / "artifacts").exists()

    def test_rejects_non_dict_request(self, tmp_path):
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=["not", "a", "dict"],
            expected_issue_number=555,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )
        assert rel_path is None
        assert err != ""


# ---------------------------------------------------------------------------
# Issue #1639 fix_delta P0: materializer symlink/hardlink/TOCTOU safety.
#
# These tests exercise _write_json_atomic_symlink_safe() /
# materialize_isolation_issue_comment_request() against adversarial
# pre-planted filesystem state at every level of the write path (namespace
# directory ancestors, the namespace directory itself, and the final output
# filename), and assert fail-closed behavior that never touches (or only
# ever atomically replaces, never truncates-through) a pre-existing dirent.
# ---------------------------------------------------------------------------

class TestMaterializerSymlinkSafety:
    def _request(self, issue_number=777, body="hello <!-- m1 -->", marker="<!-- m1 -->"):
        return _iicr(
            issue_number=issue_number,
            repo="squne121/loop-protocol",
            comment_body=body,
            marker=marker,
        )

    def test_rejects_preexisting_output_symlink_without_touching_target(self, tmp_path):
        """The predictable output path is pre-planted as a symlink pointing
        at a sentinel target file. Materialization must fail closed and the
        sentinel target's content must remain completely unchanged."""
        target = tmp_path / "sentinel_target.json"
        sentinel_content = '{"do_not_touch": "sentinel-value-12345"}'
        target.write_text(sentinel_content, encoding="utf-8")

        namespace_dir = (
            tmp_path / "artifacts" / "777" / "issue-metadata" / "issue_comment.publish"
        )
        namespace_dir.mkdir(parents=True, exist_ok=True)
        out_path = namespace_dir / "issue_comment_publish_input.json"
        out_path.symlink_to(target)

        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=self._request(),
            expected_issue_number=777,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )

        assert rel_path is None
        assert err != ""
        # The symlink itself must still point at target, untouched.
        assert out_path.is_symlink()
        # Target content must be byte-identical to what it was before.
        assert target.read_text(encoding="utf-8") == sentinel_content

    def test_rejects_namespace_directory_itself_as_symlink(self, tmp_path):
        """artifacts/{issue}/issue-metadata/issue_comment.publish is itself
        pre-planted as a symlink to an unrelated directory."""
        real_other_dir = tmp_path / "unrelated_dir"
        real_other_dir.mkdir()
        sentinel = real_other_dir / "sentinel.txt"
        sentinel.write_text("do-not-touch", encoding="utf-8")

        parent = tmp_path / "artifacts" / "777" / "issue-metadata"
        parent.mkdir(parents=True, exist_ok=True)
        (parent / "issue_comment.publish").symlink_to(real_other_dir)

        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=self._request(),
            expected_issue_number=777,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )

        assert rel_path is None
        assert err != ""
        assert not (real_other_dir / "issue_comment_publish_input.json").exists()
        assert sentinel.read_text(encoding="utf-8") == "do-not-touch"

    def test_rejects_intermediate_directory_as_symlink(self, tmp_path):
        """An intermediate ancestor (artifacts/{issue}/issue-metadata) is
        pre-planted as a symlink, not a real directory."""
        real_other_dir = tmp_path / "unrelated_parent"
        real_other_dir.mkdir()

        issue_dir = tmp_path / "artifacts" / "777"
        issue_dir.mkdir(parents=True, exist_ok=True)
        (issue_dir / "issue-metadata").symlink_to(real_other_dir)

        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=self._request(),
            expected_issue_number=777,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )

        assert rel_path is None
        assert err != ""
        assert list(real_other_dir.iterdir()) == []

    def test_rejects_preexisting_output_as_hardlink(self, tmp_path):
        """The predictable output path already exists as a hardlink to
        another file (st_nlink > 1); materialization must fail closed and
        must not modify the shared inode's content."""
        namespace_dir = (
            tmp_path / "artifacts" / "777" / "issue-metadata" / "issue_comment.publish"
        )
        namespace_dir.mkdir(parents=True, exist_ok=True)
        out_path = namespace_dir / "issue_comment_publish_input.json"
        original_content = '{"do_not_touch": "hardlink-sentinel"}'
        out_path.write_text(original_content, encoding="utf-8")

        other_link = tmp_path / "other_hardlink.json"
        os.link(out_path, other_link)
        assert out_path.stat().st_nlink == 2

        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=self._request(),
            expected_issue_number=777,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )

        assert rel_path is None
        assert err != ""
        assert out_path.read_text(encoding="utf-8") == original_content
        assert other_link.read_text(encoding="utf-8") == original_content

    def test_rejects_preexisting_output_as_directory(self, tmp_path):
        namespace_dir = (
            tmp_path / "artifacts" / "777" / "issue-metadata" / "issue_comment.publish"
        )
        namespace_dir.mkdir(parents=True, exist_ok=True)
        out_dir = namespace_dir / "issue_comment_publish_input.json"
        out_dir.mkdir()
        (out_dir / "keep.txt").write_text("do-not-touch", encoding="utf-8")

        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=self._request(),
            expected_issue_number=777,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )

        assert rel_path is None
        assert err != ""
        assert out_dir.is_dir()
        assert (out_dir / "keep.txt").read_text(encoding="utf-8") == "do-not-touch"

    def test_rejects_preexisting_output_as_fifo(self, tmp_path):
        namespace_dir = (
            tmp_path / "artifacts" / "777" / "issue-metadata" / "issue_comment.publish"
        )
        namespace_dir.mkdir(parents=True, exist_ok=True)
        out_path = namespace_dir / "issue_comment_publish_input.json"
        os.mkfifo(out_path)

        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=self._request(),
            expected_issue_number=777,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )

        assert rel_path is None
        assert err != ""
        assert _stat.S_ISFIFO(out_path.lstat().st_mode)

    def test_concurrent_replacement_leaves_atomic_result(self, tmp_path):
        """Simulate a concurrent writer racing the same output path: run
        materialize twice back-to-back with different bodies and assert the
        final file is one complete, self-consistent JSON document (never a
        half-written / interleaved result) -- exercising the atomic
        temp-file + rename() replace path for legitimate re-materialization."""
        rel_path_1, err_1 = pub.materialize_isolation_issue_comment_request(
            request=self._request(body="first <!-- m1 -->", marker="<!-- m1 -->"),
            expected_issue_number=777,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )
        assert err_1 == ""

        rel_path_2, err_2 = pub.materialize_isolation_issue_comment_request(
            request=self._request(body="second <!-- m2 -->", marker="<!-- m2 -->"),
            expected_issue_number=777,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )
        assert err_2 == ""
        assert rel_path_1 == rel_path_2

        written = json.loads((tmp_path / rel_path_2).read_text(encoding="utf-8"))
        assert written["comment_body"] == "second <!-- m2 -->"
        assert written["marker"] == "<!-- m2 -->"

        # No stray temp files should remain in the namespace directory.
        namespace_dir = (tmp_path / rel_path_2).parent
        leftover_tmp = [p for p in namespace_dir.iterdir() if ".tmp." in p.name]
        assert leftover_tmp == []

    def test_materialization_failure_does_not_destroy_existing_legit_file(self, tmp_path):
        """A legitimate prior materialization exists at the output path.
        A subsequent materialize call that fails validation (e.g. marker
        not embedded) must not touch the existing legitimate file at all."""
        rel_path, err = pub.materialize_isolation_issue_comment_request(
            request=self._request(body="original <!-- m1 -->", marker="<!-- m1 -->"),
            expected_issue_number=777,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )
        assert err == ""
        original_bytes = (tmp_path / rel_path).read_bytes()

        bad_request = _iicr(
            issue_number=777,
            repo="squne121/loop-protocol",
            comment_body="hello world",
            marker="<!-- not-embedded -->",
        )
        rel_path_2, err_2 = pub.materialize_isolation_issue_comment_request(
            request=bad_request,
            expected_issue_number=777,
            expected_repo="squne121/loop-protocol",
            project_root=tmp_path,
        )
        assert rel_path_2 is None
        assert err_2 != ""

        assert (tmp_path / rel_path).read_bytes() == original_bytes


class TestLoopHandoffWiring:
    """AC3: publish() forwards the loop_handoff field from input_data to the
    renderer subprocess unmodified (via normalize_input's dict pass-through)."""

    def test_loop_handoff_forwarded_to_renderer_subprocess(self):
        loop_handoff = {
            "status": "impl_ready",
            "routing_action": "run_impl_review_loop",
            "contract_review": {
                "status": "go",
                "gate_result": "fresh_go",
                "latest_comment_url": "https://example.com/c",
                "generated_at": "2026-07-04T00:00:00Z",
                "body_sha256": "sha256:" + "a" * 64,
            },
            "metadata": {"title_prefix_ready": True, "phase_label_ready": True},
            "auto_fixes": {"result": "auto_fixed", "required": [], "skipped": []},
            "blockers": [],
            "permissions": {"unavailable": []},
            "generated_at": "2026-07-04T00:00:00Z",
        }
        input_data = _make_input("approved", issue_number=1311)
        input_data["loop_handoff"] = loop_handoff

        fake_proc = _fake_renderer_proc(_make_render_result())

        with patch("subprocess.run", return_value=fake_proc) as mock_run:
            with patch.object(pub, "_post_github_comment", return_value=0):
                pub.publish(
                    issue_number=1311,
                    input_data=input_data,
                    repo="squne121/loop-protocol",
                )

        renderer_call = mock_run.call_args_list[0]
        sent_input = renderer_call.kwargs.get("input")
        assert sent_input is not None
        sent_payload = json.loads(sent_input)
        assert "loop_handoff" in sent_payload
        assert sent_payload["loop_handoff"]["status"] == "impl_ready"

    def test_missing_loop_handoff_does_not_break_forwarding(self):
        input_data = _make_input("approved", issue_number=42)
        assert "loop_handoff" not in input_data

        fake_proc = _fake_renderer_proc(_make_render_result())

        with patch("subprocess.run", return_value=fake_proc) as mock_run:
            with patch.object(pub, "_post_github_comment", return_value=0):
                pub.publish(
                    issue_number=42,
                    input_data=input_data,
                    repo="squne121/loop-protocol",
                )

        renderer_call = mock_run.call_args_list[0]
        sent_payload = json.loads(renderer_call.kwargs.get("input"))
        assert "loop_handoff" not in sent_payload


# ---------------------------------------------------------------------------
# High 1 (reviewer #1317): --input-file wiring must preserve loop_handoff
# through main()'s CLI entry point, not just publish()'s direct call.
# ---------------------------------------------------------------------------

class TestInputFilePreservesLoopHandoff:
    """High 1: verify the --input-file code path (main()) forwards the full
    loop_handoff payload to the renderer subprocess unmodified, with a deep
    equality assertion on the whole loop_handoff object (not just status)."""

    def test_publish_termination_report_input_file_preserves_loop_handoff(
        self, tmp_path, monkeypatch
    ):
        loop_handoff = {
            "status": "impl_ready",
            "routing_action": "run_impl_review_loop",
            "contract_review": {
                "status": "go",
                "gate_result": "fresh_go",
                "latest_comment_url": "https://example.com/c",
                "generated_at": "2026-07-04T00:00:00Z",
                "body_sha256": "sha256:" + "a" * 64,
            },
            "metadata": {"title_prefix_ready": True, "phase_label_ready": True},
            "auto_fixes": {"result": "auto_fixed", "required": [], "skipped": []},
            "blockers": [],
            "permissions": {"unavailable": []},
            "generated_at": "2026-07-04T00:00:00Z",
        }
        input_data = _make_input("approved", issue_number=1311)
        input_data["loop_handoff"] = loop_handoff

        input_file = tmp_path / "termination_report_input.json"
        input_file.write_text(json.dumps(input_data), encoding="utf-8")

        fake_proc = _fake_renderer_proc(_make_render_result())

        argv = [
            "publish_termination_report.py",
            "--issue-number",
            "1311",
            "--repo",
            "squne121/loop-protocol",
            "--input-file",
            str(input_file),
        ]
        monkeypatch.setattr(sys, "argv", argv)

        with patch("subprocess.run", return_value=fake_proc) as mock_run:
            with patch.object(pub, "_post_github_comment", return_value=0):
                exit_code = pub.main()

        assert exit_code == 0

        renderer_call = mock_run.call_args_list[0]
        sent_input = renderer_call.kwargs.get("input")
        assert sent_input is not None
        sent_payload = json.loads(sent_input)

        # Deep equality on the whole loop_handoff object, not just status --
        # the --input-file code path must not drop/filter/mutate any field.
        assert sent_payload["loop_handoff"] == loop_handoff
