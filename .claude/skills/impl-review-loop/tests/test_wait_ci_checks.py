from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import wait_ci_checks


HEAD_SHA = "abc123"


def _parse_marker(output: str) -> dict:
    prefix = "CI_WAIT_RESULT_V1_JSON="
    assert output.startswith(prefix)
    return json.loads(output[len(prefix) :])


def test_required_flag_is_mandatory(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = wait_ci_checks.main(["--repo", "owner/repo", "--pr", "1", "--head-sha", HEAD_SHA])
    assert exit_code == wait_ci_checks.EXIT_RUNTIME
    payload = _parse_marker(capsys.readouterr().out.strip())
    assert payload["status"] == "gh_error"
    assert payload["error_code"] == "invalid_args"


def test_passed_required_checks(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(wait_ci_checks, "shutil_which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(
        wait_ci_checks,
        "get_current_head_sha",
        lambda repo, pr: (HEAD_SHA, None, None),
    )
    monkeypatch.setattr(
        wait_ci_checks,
        "fetch_checks",
        lambda repo, pr: ([{"name": "build", "bucket": "pass"}], None, None),
    )

    exit_code = wait_ci_checks.main(
        ["--repo", "owner/repo", "--pr", "1", "--head-sha", HEAD_SHA, "--required", "--interval", "1"]
    )
    assert exit_code == wait_ci_checks.EXIT_PASS
    payload = _parse_marker(capsys.readouterr().out.strip())
    assert payload["status"] == "passed"
    assert payload["required_only"] is True


def test_skipped_only_is_fail_closed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(wait_ci_checks, "shutil_which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(wait_ci_checks, "get_current_head_sha", lambda repo, pr: (HEAD_SHA, None, None))
    monkeypatch.setattr(
        wait_ci_checks,
        "fetch_checks",
        lambda repo, pr: ([{"name": "lint", "bucket": "skipping"}], None, None),
    )

    exit_code = wait_ci_checks.main(
        ["--repo", "owner/repo", "--pr", "1", "--head-sha", HEAD_SHA, "--required"]
    )
    assert exit_code == wait_ci_checks.EXIT_NEGATIVE
    payload = _parse_marker(capsys.readouterr().out.strip())
    assert payload["status"] == "skipped_only"


def test_cancelled_bucket_emits_cancelled(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(wait_ci_checks, "shutil_which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(wait_ci_checks, "get_current_head_sha", lambda repo, pr: (HEAD_SHA, None, None))
    monkeypatch.setattr(
        wait_ci_checks,
        "fetch_checks",
        lambda repo, pr: ([{"name": "build", "bucket": "cancel"}], None, None),
    )

    exit_code = wait_ci_checks.main(
        ["--repo", "owner/repo", "--pr", "1", "--head-sha", HEAD_SHA, "--required"]
    )
    assert exit_code == wait_ci_checks.EXIT_NEGATIVE
    payload = _parse_marker(capsys.readouterr().out.strip())
    assert payload["status"] == "cancelled"


def test_auth_error_still_emits_marker(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(wait_ci_checks, "shutil_which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(wait_ci_checks, "get_current_head_sha", lambda repo, pr: (HEAD_SHA, None, None))
    monkeypatch.setattr(wait_ci_checks, "fetch_checks", lambda repo, pr: (None, "auth_error", "bad credentials"))

    exit_code = wait_ci_checks.main(
        ["--repo", "owner/repo", "--pr", "1", "--head-sha", HEAD_SHA, "--required"]
    )
    assert exit_code == wait_ci_checks.EXIT_RUNTIME
    payload = _parse_marker(capsys.readouterr().out.strip())
    assert payload["status"] == "auth_error"
    assert payload["message"] == "bad credentials"


def test_pending_then_fail(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(wait_ci_checks, "shutil_which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(wait_ci_checks, "get_current_head_sha", lambda repo, pr: (HEAD_SHA, None, None))
    monkeypatch.setattr(wait_ci_checks.time, "sleep", lambda _: None)

    responses = iter(
        [
            ([{"name": "build", "bucket": "pending"}], None, None),
            ([{"name": "build", "bucket": "fail"}], None, None),
        ]
    )
    monkeypatch.setattr(wait_ci_checks, "fetch_checks", lambda repo, pr: next(responses))

    exit_code = wait_ci_checks.main(
        ["--repo", "owner/repo", "--pr", "1", "--head-sha", HEAD_SHA, "--required", "--interval", "1"]
    )
    assert exit_code == wait_ci_checks.EXIT_NEGATIVE
    payload = _parse_marker(capsys.readouterr().out.strip())
    assert payload["status"] == "failed"


def test_head_sha_change_before_wait(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(wait_ci_checks, "shutil_which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(wait_ci_checks, "get_current_head_sha", lambda repo, pr: ("different", None, None))

    exit_code = wait_ci_checks.main(
        ["--repo", "owner/repo", "--pr", "1", "--head-sha", HEAD_SHA, "--required"]
    )
    assert exit_code == wait_ci_checks.EXIT_NEGATIVE
    payload = _parse_marker(capsys.readouterr().out.strip())
    assert payload["status"] == "head_sha_changed"


def test_no_checks(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(wait_ci_checks, "shutil_which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(wait_ci_checks, "get_current_head_sha", lambda repo, pr: (HEAD_SHA, None, None))
    monkeypatch.setattr(wait_ci_checks, "fetch_checks", lambda repo, pr: ([], None, None))

    exit_code = wait_ci_checks.main(
        ["--repo", "owner/repo", "--pr", "1", "--head-sha", HEAD_SHA, "--required"]
    )
    assert exit_code == wait_ci_checks.EXIT_NEGATIVE
    payload = _parse_marker(capsys.readouterr().out.strip())
    assert payload["status"] == "no_checks"
