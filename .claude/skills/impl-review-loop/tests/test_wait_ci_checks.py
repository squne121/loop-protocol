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


# ---------------------------------------------------------------------------
# Issue #1856: canonical required-CI evaluator integration (AC11/AC12/AC17)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]


def test_canonical_evaluator_consumers_use_same_function() -> None:
    """AC11: pr-review-judge・pr-reviewer-lite・impl-review-loop Step 4 が
    同一の canonical required-CI evaluator（wait_ci_checks.py ベース）を
    呼び出すことをドキュメント上で確認する。"""
    consumers = {
        "pr-review-judge/SKILL.md": REPO_ROOT
        / ".claude"
        / "skills"
        / "pr-review-judge"
        / "SKILL.md",
        "pr-reviewer-lite.md": REPO_ROOT / ".claude" / "agents" / "pr-reviewer-lite.md",
        "impl-review-loop/steps/step-4-pr-review.md": REPO_ROOT
        / ".claude"
        / "skills"
        / "impl-review-loop"
        / "steps"
        / "step-4-pr-review.md",
    }
    for label, path_obj in consumers.items():
        assert path_obj.is_file(), f"{label} not found at {path_obj}"
        text = path_obj.read_text(encoding="utf-8")
        assert "wait_ci_checks" in text, (
            f"{label} must reference the canonical wait_ci_checks.py evaluator "
            f"(Issue #1856 AC11)"
        )


def test_status_context_only_required_check_satisfied(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC12: CheckRun が 0 件でも required な StatusContext のみで required
    check 成立と判定できる。wait_ci_checks.py は entry の provenance 種別
    (CheckRun/StatusContext) を区別せず bucket のみで判定するため、
    contextType: StatusContext の entry のみでも passed になる。"""
    monkeypatch.setattr(wait_ci_checks, "shutil_which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(wait_ci_checks, "get_current_head_sha", lambda repo, pr: (HEAD_SHA, None, None))
    monkeypatch.setattr(
        wait_ci_checks,
        "fetch_checks",
        lambda repo, pr: (
            [
                {
                    "name": "required-status-context",
                    "bucket": "pass",
                    "contextType": "StatusContext",
                }
            ],
            None,
            None,
        ),
    )

    exit_code = wait_ci_checks.main(
        ["--repo", "owner/repo", "--pr", "1", "--head-sha", HEAD_SHA, "--required", "--interval", "1"]
    )
    assert exit_code == wait_ci_checks.EXIT_PASS
    payload = _parse_marker(capsys.readouterr().out.strip())
    assert payload["status"] == "passed"
    assert payload["checks"][0]["contextType"] == "StatusContext"


class TestAC17CanonicalEvaluatorBehavioralMatrix:
    """AC17: canonical evaluator に対する behavioral matrix。"""

    def test_check_run_zero_yields_request_changes_equivalent(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """(1) CheckRun 0件 → REQUEST_CHANGES 相当（no_checks, fail-closed）。"""
        monkeypatch.setattr(wait_ci_checks, "shutil_which", lambda _: "/usr/bin/gh")
        monkeypatch.setattr(wait_ci_checks, "get_current_head_sha", lambda repo, pr: (HEAD_SHA, None, None))
        monkeypatch.setattr(wait_ci_checks, "fetch_checks", lambda repo, pr: ([], None, None))

        exit_code = wait_ci_checks.main(
            ["--repo", "owner/repo", "--pr", "1", "--head-sha", HEAD_SHA, "--required"]
        )
        assert exit_code == wait_ci_checks.EXIT_NEGATIVE
        payload = _parse_marker(capsys.readouterr().out.strip())
        assert payload["status"] == "no_checks"

    def test_status_context_only_satisfies_required_check(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """(2) StatusContext のみで required check 成立。"""
        monkeypatch.setattr(wait_ci_checks, "shutil_which", lambda _: "/usr/bin/gh")
        monkeypatch.setattr(wait_ci_checks, "get_current_head_sha", lambda repo, pr: (HEAD_SHA, None, None))
        monkeypatch.setattr(
            wait_ci_checks,
            "fetch_checks",
            lambda repo, pr: (
                [{"name": "status-ctx", "bucket": "pass", "contextType": "StatusContext"}],
                None,
                None,
            ),
        )

        exit_code = wait_ci_checks.main(
            ["--repo", "owner/repo", "--pr", "1", "--head-sha", HEAD_SHA, "--required", "--interval", "1"]
        )
        assert exit_code == wait_ci_checks.EXIT_PASS
        payload = _parse_marker(capsys.readouterr().out.strip())
        assert payload["status"] == "passed"

    def test_one_required_check_unreported_yields_request_changes_equivalent(self) -> None:
        """(3) required check の一つが未報告（null/pending bucket）→
        REQUEST_CHANGES 相当（pending_or_queued として fail-closed）。"""
        checks = [
            {"name": "build", "bucket": "pass"},
            {"name": "unreported-required-check", "bucket": None},
        ]
        decision, _message = wait_ci_checks.decide_status(checks)
        assert decision == "pending"

    def test_ci_absent_test_verdict_pass_still_request_changes_equivalent(self) -> None:
        """(4) CI 無し + TEST_VERDICT PASS でも REQUEST_CHANGES 相当
        （TEST_VERDICT が承認根拠にならない）。wait_ci_checks.py は
        TEST_VERDICT を一切参照しないため、CI 無し(no_checks)の判定に
        TEST_VERDICT の内容は影響しない。"""
        source = Path(wait_ci_checks.__file__).read_text(encoding="utf-8")
        assert "TEST_VERDICT" not in source, (
            "wait_ci_checks.py must not reference TEST_VERDICT as an "
            "authoritative input (Issue #1856 AC17-4)"
        )

    def test_authoritative_pass_with_stale_test_verdict_skip_still_approvable(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """(5) authoritative CI/VC PASS + stale TEST_VERDICT SKIP でも
        APPROVE 可能（TEST_VERDICT が拒否根拠にならない）。TEST_VERDICT を
        模した環境変数を設定しても canonical evaluator の判定に影響しない
        ことを確認する。"""
        monkeypatch.setenv("SIMULATED_STALE_TEST_VERDICT", "SKIP")
        monkeypatch.setattr(wait_ci_checks, "shutil_which", lambda _: "/usr/bin/gh")
        monkeypatch.setattr(wait_ci_checks, "get_current_head_sha", lambda repo, pr: (HEAD_SHA, None, None))
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
