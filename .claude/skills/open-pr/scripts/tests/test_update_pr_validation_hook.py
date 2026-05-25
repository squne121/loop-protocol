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
