#!/usr/bin/env python3
"""Tests for update_pr.py validator integration (PR body update path)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import update_pr


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pr_body"


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def write_temp_body(body: str) -> str:
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False)
    handle.write(body)
    handle.flush()
    handle.close()
    return handle.name


class TestUpdatePrValidatorFail:
    """Test case: validator returns fail (exit 1) — update must be blocked."""

    def test_validator_fail_blocks_update(self, monkeypatch: pytest.MonkeyPatch):
        """AC3: validator exit code 1 (FAIL) blocks update."""
        body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
        update_called = {"value": False}
        try:
            monkeypatch.setattr(update_pr, "resolve_repo", lambda: "squne121/loop-protocol")
            monkeypatch.setattr(
                update_pr,
                "_run_pr_body_validator",
                lambda body, changed_paths, linked_issue: {
                    "status": "fail",
                    "errors": [{"rule_id": "LP050"}],
                    "message": "validation failed",
                    "schema": "loop_body_lint/v1",
                    "target": "pr",
                },
            )

            def fake_update_pr(*args, **kwargs):
                update_called["value"] = True
                raise AssertionError("update_pr should not be called")

            monkeypatch.setattr(update_pr, "update_pr", fake_update_pr)

            rc = update_pr.main(
                [
                    "--pr-number",
                    "399",
                    "--body-file",
                    body_path,
                ]
            )
            assert rc == 1
            assert update_called["value"] is False
        finally:
            Path(body_path).unlink(missing_ok=True)


class TestUpdatePrValidatorInternalError:
    """Test case: validator returns internal error (exit 2) — update must be blocked."""

    def test_validator_internal_error_blocks_update(self, monkeypatch: pytest.MonkeyPatch):
        """AC4: validator exit code 2 (INTERNAL ERROR) blocks update."""
        body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
        update_called = {"value": False}
        try:
            monkeypatch.setattr(update_pr, "resolve_repo", lambda: "squne121/loop-protocol")
            monkeypatch.setattr(
                update_pr,
                "_run_pr_body_validator",
                lambda body, changed_paths, linked_issue: {
                    "status": "internal",
                    "errors": [],
                    "message": "Validator returned non-JSON output",
                    "schema": "loop_body_lint/v1",
                    "target": "pr",
                },
            )

            def fake_update_pr(*args, **kwargs):
                update_called["value"] = True
                raise AssertionError("update_pr should not be called")

            monkeypatch.setattr(update_pr, "update_pr", fake_update_pr)

            rc = update_pr.main(
                [
                    "--pr-number",
                    "399",
                    "--body-file",
                    body_path,
                ]
            )
            assert rc == 1
            assert update_called["value"] is False
        finally:
            Path(body_path).unlink(missing_ok=True)


class TestUpdatePrValidatorPass:
    """Test case: validator returns pass (exit 0) — update must succeed."""

    def test_validator_pass_allows_update(self, monkeypatch: pytest.MonkeyPatch):
        """AC8: validator exit code 0 (PASS) allows update to proceed."""
        body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
        update_called = {"value": False}
        try:
            monkeypatch.setattr(update_pr, "resolve_repo", lambda: "squne121/loop-protocol")
            monkeypatch.setattr(
                update_pr,
                "_run_pr_body_validator",
                lambda body, changed_paths, linked_issue: {
                    "status": "pass",
                    "errors": [],
                    "schema": "loop_body_lint/v1",
                    "target": "pr",
                    "body_sha256": "sha256:abc123",
                },
            )
            monkeypatch.setattr(update_pr, "_run_japanese_content_validator", lambda body_text, threshold=0.1: {"status": "pass", "failed_blocks": 0, "aggregate_ratio": 0.5, "threshold": 0.1, "body_sha256": "", "stderr": ""})

            def fake_update_pr(repo, pr_number, body_file):
                update_called["value"] = True
                return True

            monkeypatch.setattr(update_pr, "update_pr", fake_update_pr)

            rc = update_pr.main(
                [
                    "--pr-number",
                    "399",
                    "--body-file",
                    body_path,
                ]
            )
            assert rc == 0
            assert update_called["value"] is True
        finally:
            Path(body_path).unlink(missing_ok=True)


class TestUpdatePrValidatorSchema:
    """Test case: validator schema / target / body_sha256 validation."""

    def test_validator_schema_mismatch(self, monkeypatch: pytest.MonkeyPatch):
        """AC5: Validator schema must be loop_body_lint/v1."""
        body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
        try:
            monkeypatch.setattr(update_pr, "resolve_repo", lambda: "squne121/loop-protocol")

            class FakeCP:
                returncode = 0
                stdout = json.dumps({
                    "schema": "wrong_schema",
                    "target": "pr",
                    "status": "pass",
                    "errors": []
                })
                stderr = ""

            monkeypatch.setattr(update_pr.subprocess, "run", lambda *args, **kwargs: FakeCP())

            rc = update_pr.main(
                [
                    "--pr-number",
                    "399",
                    "--body-file",
                    body_path,
                ]
            )
            assert rc == 1
        finally:
            Path(body_path).unlink(missing_ok=True)

    def test_validator_target_mismatch(self, monkeypatch: pytest.MonkeyPatch):
        """AC5: Validator target must be 'pr'."""
        body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
        try:
            monkeypatch.setattr(update_pr, "resolve_repo", lambda: "squne121/loop-protocol")

            class FakeCP:
                returncode = 0
                stdout = json.dumps({
                    "schema": "loop_body_lint/v1",
                    "target": "issue",
                    "status": "pass",
                    "errors": []
                })
                stderr = ""

            monkeypatch.setattr(update_pr.subprocess, "run", lambda *args, **kwargs: FakeCP())

            rc = update_pr.main(
                [
                    "--pr-number",
                    "399",
                    "--body-file",
                    body_path,
                ]
            )
            assert rc == 1
        finally:
            Path(body_path).unlink(missing_ok=True)

    def test_validator_body_sha256_mismatch(self, monkeypatch: pytest.MonkeyPatch):
        """AC5: Validator body_sha256 must match the input body."""
        body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
        try:
            monkeypatch.setattr(update_pr, "resolve_repo", lambda: "squne121/loop-protocol")

            class FakeCP:
                returncode = 0
                stdout = json.dumps({
                    "schema": "loop_body_lint/v1",
                    "target": "pr",
                    "status": "pass",
                    "errors": [],
                    "body_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
                })
                stderr = ""

            monkeypatch.setattr(update_pr.subprocess, "run", lambda *args, **kwargs: FakeCP())

            rc = update_pr.main(
                [
                    "--pr-number",
                    "399",
                    "--body-file",
                    body_path,
                ]
            )
            assert rc == 1
        finally:
            Path(body_path).unlink(missing_ok=True)


class TestUpdatePrValidatorJsonAndErrors:
    """Test case: validator JSON parsing and error handling."""

    def test_validator_non_json_stdout(self, monkeypatch: pytest.MonkeyPatch):
        """Validator must return valid JSON."""
        body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
        try:
            monkeypatch.setattr(update_pr, "resolve_repo", lambda: "squne121/loop-protocol")

            class FakeCP:
                returncode = 0
                stdout = "not json"
                stderr = ""

            monkeypatch.setattr(update_pr.subprocess, "run", lambda *args, **kwargs: FakeCP())

            rc = update_pr.main(
                [
                    "--pr-number",
                    "399",
                    "--body-file",
                    body_path,
                ]
            )
            assert rc == 1
        finally:
            Path(body_path).unlink(missing_ok=True)


class TestUpdatePrFixture:
    """Test case: fixture-driven test with real validator behavior."""

    def test_update_pr_with_linked_issue(self, monkeypatch: pytest.MonkeyPatch):
        """AC2: Validator must be called with linked-issue argument."""
        body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
        observed = {"linked_issue": None}
        try:
            monkeypatch.setattr(update_pr, "resolve_repo", lambda: "squne121/loop-protocol")

            def fake_validator(body, changed_paths, linked_issue):
                observed["linked_issue"] = linked_issue
                return {
                    "status": "pass",
                    "errors": [],
                    "schema": "loop_body_lint/v1",
                    "target": "pr",
                    "body_sha256": "sha256:abc123",
                }

            monkeypatch.setattr(update_pr, "_run_pr_body_validator", fake_validator)
            monkeypatch.setattr(update_pr, "_run_japanese_content_validator", lambda body_text, threshold=0.1: {"status": "pass", "failed_blocks": 0, "aggregate_ratio": 0.5, "threshold": 0.1, "body_sha256": "", "stderr": ""})
            monkeypatch.setattr(update_pr, "update_pr", lambda *args, **kwargs: True)

            rc = update_pr.main(
                [
                    "--pr-number",
                    "399",
                    "--body-file",
                    body_path,
                    "--linked-issue",
                    "330",
                ]
            )
            assert rc == 0
            assert observed["linked_issue"] == 330
        finally:
            Path(body_path).unlink(missing_ok=True)


class TestValidatorExitStatusMismatch:
    """Blocker 2: Validator exit code と JSON status の mismatch ガード (fix_delta)"""

    def test_validator_exit_status_mismatch_returncode1_status_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """returncode=1 + status=pass でも mismatch として internal error 扱い (Blocker 2)"""
        body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
        try:
            monkeypatch.setattr(update_pr, "resolve_repo", lambda: "squne121/loop-protocol")
            body_text = Path(body_path).read_text(encoding="utf-8")
            body_sha256 = f"sha256:{__import__('hashlib').sha256(body_text.encode()).hexdigest()}"

            class FakeCP:
                returncode = 1  # Fail exit code
                stdout = json.dumps({
                    "schema": "loop_body_lint/v1",
                    "target": "pr",
                    "status": "pass",  # But status is pass — MISMATCH!
                    "body_sha256": body_sha256,
                    "errors": []
                })
                stderr = ""

            monkeypatch.setattr(update_pr.subprocess, "run", lambda *args, **kwargs: FakeCP())

            rc = update_pr.main(
                [
                    "--pr-number",
                    "399",
                    "--body-file",
                    body_path,
                ]
            )
            # Should fail because of mismatch
            assert rc == 1
        finally:
            Path(body_path).unlink(missing_ok=True)

    def test_validator_exit_status_mismatch_returncode0_status_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """returncode=0 + status=fail でも mismatch として internal error 扱い (Blocker 2)"""
        body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
        try:
            monkeypatch.setattr(update_pr, "resolve_repo", lambda: "squne121/loop-protocol")
            body_text = Path(body_path).read_text(encoding="utf-8")
            body_sha256 = f"sha256:{__import__('hashlib').sha256(body_text.encode()).hexdigest()}"

            class FakeCP:
                returncode = 0  # Pass exit code
                stdout = json.dumps({
                    "schema": "loop_body_lint/v1",
                    "target": "pr",
                    "status": "fail",  # But status is fail — MISMATCH!
                    "body_sha256": body_sha256,
                    "errors": [{"rule_id": "LP050"}]
                })
                stderr = ""

            monkeypatch.setattr(update_pr.subprocess, "run", lambda *args, **kwargs: FakeCP())

            rc = update_pr.main(
                [
                    "--pr-number",
                    "399",
                    "--body-file",
                    body_path,
                ]
            )
            # Should fail because of mismatch
            assert rc == 1
        finally:
            Path(body_path).unlink(missing_ok=True)


class TestChangedPathsAutoResolve:
    """Blocker 3: --changed-paths 未指定時に resolve_changed_paths が呼ばれる (fix_delta)"""

    def test_changed_paths_default_autoresolve(self, monkeypatch: pytest.MonkeyPatch):
        """--changed-paths 未指定時に resolve_changed_paths(None) が呼ばれることを確認 (Blocker 3)"""
        body_path = write_temp_body(load_fixture("valid_not_schema_change.md"))
        resolve_called = {"value": False, "args": None}
        try:
            monkeypatch.setattr(update_pr, "resolve_repo", lambda: "squne121/loop-protocol")

            def fake_resolve_changed_paths(paths):
                resolve_called["value"] = True
                resolve_called["args"] = paths
                return ["file1.py", "file2.md"]

            monkeypatch.setattr(update_pr, "resolve_changed_paths", fake_resolve_changed_paths)

            body_text = Path(body_path).read_text(encoding="utf-8")
            body_sha256 = f"sha256:{__import__('hashlib').sha256(body_text.encode()).hexdigest()}"

            class FakeCP:
                returncode = 0
                stdout = json.dumps({
                    "schema": "loop_body_lint/v1",
                    "target": "pr",
                    "status": "pass",
                    "body_sha256": body_sha256,
                    "errors": []
                })
                stderr = ""

            monkeypatch.setattr(update_pr.subprocess, "run", lambda *args, **kwargs: FakeCP())
            monkeypatch.setattr(update_pr, "update_pr", lambda *args, **kwargs: True)

            rc = update_pr.main(
                [
                    "--pr-number",
                    "399",
                    "--body-file",
                    body_path,
                ]
            )
            # Should succeed
            assert rc == 0
            # resolve_changed_paths should have been called with None (default)
            assert resolve_called["value"] is True
            assert resolve_called["args"] is None
        finally:
            Path(body_path).unlink(missing_ok=True)


class TestTOCTOUSafety:
    """Blocker 1: TOCTOU safety — validator pass 後に temp body が gh pr edit に渡る (fix_delta)"""

    def test_toctou_uses_validated_body_temp(self, monkeypatch: pytest.MonkeyPatch):
        """validator pass 後、gh pr edit に渡す path が args.body_file ではなく temp file であることを確認 (Blocker 1)"""
        original_body = write_temp_body("# Original body that will not be used")
        try:
            validated_body = "# Updated and validated body"
            body_sha256 = f"sha256:{__import__('hashlib').sha256(validated_body.encode()).hexdigest()}"

            monkeypatch.setattr(update_pr, "resolve_repo", lambda: "squne121/loop-protocol")

            # Track calls to run_gh to inspect --body-file argument
            gh_calls = []

            def fake_run_gh(*args, **kwargs):
                gh_calls.append(args)
                return __import__('subprocess').CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="",
                    stderr=""
                )

            monkeypatch.setattr(update_pr, "run_gh", fake_run_gh)

            # Mock validator to pass
            class FakeCP:
                returncode = 0
                stdout = json.dumps({
                    "schema": "loop_body_lint/v1",
                    "target": "pr",
                    "status": "pass",
                    "body_sha256": body_sha256,
                    "errors": []
                })
                stderr = ""

            monkeypatch.setattr(update_pr.subprocess, "run", lambda *args, **kwargs: FakeCP())

            # Call update_pr directly with validated body text
            result = update_pr.update_pr("owner/repo", 123, validated_body)

            assert result is True
            # Verify gh pr edit was called
            assert len(gh_calls) > 0
            # The --body-file should be a temp path, not the original
            for gh_call in gh_calls:
                if 'pr' in str(gh_call) and 'edit' in str(gh_call):
                    # Convert to list for easier indexing
                    call_list = list(gh_call)
                    if '--body-file' in call_list:
                        idx = call_list.index('--body-file')
                        body_file_arg = call_list[idx + 1]
                        # Should NOT be the original file path
                        assert body_file_arg != original_body
                        # Should be a temp file (containing /tmp or similar)
                        assert (
                            '/tmp' in body_file_arg
                            or 'Temp' in body_file_arg
                            or body_file_arg.startswith('/var')
                        )
        finally:
            Path(original_body).unlink(missing_ok=True)


# --- AC8: Japanese content validation blocks gh pr edit (update_pr.py) ---


class TestJapaneseContentValidationHook:
    """AC8: Japanese content validation blocks gh pr edit when prose fails threshold."""

    def _run_main_with_japanese_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
        body_text: str,
        japanese_result: dict,
    ) -> tuple[int, list[str]]:
        """Helper: run update_pr.main with fixed japanese validator result, capture stdout."""
        body_path = write_temp_body(body_text)
        output_lines: list[str] = []

        def capture_print(*args, **kwargs):
            sep = kwargs.get("sep", " ")
            line = sep.join(str(a) for a in args)
            output_lines.append(line)

        try:
            monkeypatch.setattr(update_pr, "resolve_repo", lambda: "squne121/loop-protocol")
            # PR body validator always passes
            monkeypatch.setattr(
                update_pr,
                "_run_pr_body_validator",
                lambda body, changed_paths, linked_issue: {"status": "pass", "errors": []},
            )
            # Japanese validator returns provided result
            monkeypatch.setattr(
                update_pr,
                "_run_japanese_content_validator",
                lambda body_text, threshold=0.1: japanese_result,
            )

            update_called = {"value": False}

            def fake_update_pr(repo, pr_number, body_text):
                update_called["value"] = True
                raise AssertionError("update_pr must not be called when Japanese check fails")

            monkeypatch.setattr(update_pr, "update_pr", fake_update_pr)
            monkeypatch.setattr("builtins.print", capture_print)

            rc = update_pr.main(
                [
                    "--pr-number", "842",
                    "--body-file", body_path,
                ]
            )
            return rc, output_lines, update_called["value"]
        finally:
            Path(body_path).unlink(missing_ok=True)

    def test_japanese_fail_blocks_gh_pr_edit(self, monkeypatch: pytest.MonkeyPatch):
        """AC8: English prose block -> Japanese check fail -> gh pr edit NOT called."""
        body_text = load_fixture("valid_not_schema_change.md")
        rc, lines, update_called = self._run_main_with_japanese_result(
            monkeypatch,
            body_text,
            {
                "status": "fail",
                "failed_blocks": 2,
                "aggregate_ratio": 0.02,
                "threshold": 0.1,
                "body_sha256": "sha256:abc123",
                "stderr": "FAIL: 日本語比率不足 (aggregate=0.020, threshold=0.1, failed_blocks=2)",
            },
        )
        assert rc == 1
        assert not update_called, "gh pr edit must NOT be called when Japanese check fails"
        assert any(
            line == f"ERROR={update_pr.E_PR_BODY_JAPANESE_VALIDATION_FAILED}" for line in lines
        ), f"Expected ERROR=E_PR_BODY_JAPANESE_VALIDATION_FAILED; got: {lines}"

    def test_japanese_fail_emits_preflight_result_v1(self, monkeypatch: pytest.MonkeyPatch):
        """AC8: Japanese check fail emits PR_BODY_PREFLIGHT_RESULT_V1 with required fields."""
        import json as _json
        body_text = load_fixture("valid_not_schema_change.md")
        rc, lines, _ = self._run_main_with_japanese_result(
            monkeypatch,
            body_text,
            {
                "status": "fail",
                "failed_blocks": 1,
                "aggregate_ratio": 0.05,
                "threshold": 0.1,
                "body_sha256": "sha256:def456",
                "stderr": "FAIL: 日本語比率不足",
            },
        )
        assert rc == 1
        preflight_lines = [ln for ln in lines if ln.startswith("PR_BODY_PREFLIGHT_RESULT_V1=")]
        assert len(preflight_lines) == 1, (
            f"Expected exactly one PR_BODY_PREFLIGHT_RESULT_V1 line; got: {lines}"
        )
        json_str = preflight_lines[0][len("PR_BODY_PREFLIGHT_RESULT_V1="):]
        payload = _json.loads(json_str)
        assert payload.get("schema") == "PR_BODY_PREFLIGHT_RESULT_V1"
        assert payload.get("status") == "fail"
        assert "body_sha256" in payload
        assert "failed_blocks" in payload
        assert "aggregate_ratio" in payload
        assert "threshold" in payload

    def test_japanese_pass_allows_gh_pr_edit(self, monkeypatch: pytest.MonkeyPatch):
        """AC8: Japanese check pass -> gh pr edit is NOT blocked."""
        body_text = load_fixture("valid_not_schema_change.md")
        body_sha256 = f"sha256:{__import__('hashlib').sha256(body_text.encode()).hexdigest()}"
        body_path = write_temp_body(body_text)
        update_called = {"value": False}
        output_lines = []

        def capture_print(*args, **kwargs):
            sep = kwargs.get("sep", " ")
            line = sep.join(str(a) for a in args)
            output_lines.append(line)

        try:
            monkeypatch.setattr(update_pr, "resolve_repo", lambda: "squne121/loop-protocol")
            monkeypatch.setattr(
                update_pr,
                "_run_pr_body_validator",
                lambda body, changed_paths, linked_issue: {"status": "pass", "errors": []},
            )
            monkeypatch.setattr(
                update_pr,
                "_run_japanese_content_validator",
                lambda body_text, threshold=0.1: {
                    "status": "pass",
                    "failed_blocks": 0,
                    "aggregate_ratio": 0.45,
                    "threshold": 0.1,
                    "body_sha256": body_sha256,
                    "stderr": "",
                },
            )

            def fake_update_pr(repo, pr_number, body_text):
                update_called["value"] = True
                return True

            monkeypatch.setattr(update_pr, "update_pr", fake_update_pr)
            monkeypatch.setattr("builtins.print", capture_print)

            rc = update_pr.main(
                [
                    "--pr-number", "842",
                    "--body-file", body_path,
                ]
            )
            assert rc == 0
            assert update_called["value"] is True, "gh pr edit SHOULD be called when Japanese check passes"
            assert not any(
                line.startswith("ERROR=") for line in output_lines
            ), f"No ERROR expected on pass; got: {output_lines}"
        finally:
            Path(body_path).unlink(missing_ok=True)

    def test_japanese_internal_error_blocks_gh_pr_edit(self, monkeypatch: pytest.MonkeyPatch):
        """AC8: Japanese validator internal error -> fail-closed, gh pr edit NOT called."""
        body_text = load_fixture("valid_not_schema_change.md")
        rc, lines, update_called = self._run_main_with_japanese_result(
            monkeypatch,
            body_text,
            {
                "status": "internal",
                "failed_blocks": 0,
                "aggregate_ratio": 0.0,
                "threshold": 0.1,
                "body_sha256": "sha256:abc",
                "stderr": "Timeout expired",
            },
        )
        assert rc == 1
        assert not update_called, "gh pr edit must NOT be called on internal error"
        assert any(
            line == f"ERROR={update_pr.E_PR_BODY_JAPANESE_VALIDATION_FAILED}" for line in lines
        ), f"Expected ERROR=E_PR_BODY_JAPANESE_VALIDATION_FAILED; got: {lines}"
