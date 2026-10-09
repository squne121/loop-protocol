from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import wait_ci_checks


HEAD_SHA = "abc123"

# Real inventory function captured before the autouse fixture pins it to empty.
_REAL_FETCH_REQUIRED_INVENTORY = wait_ci_checks.fetch_required_inventory


@pytest.fixture(autouse=True)
def _pin_inventory_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #2836: existing tests keep their rows-only semantics (empty inventory)."""
    monkeypatch.setattr(wait_ci_checks, "fetch_required_inventory", lambda repo, pr: ({}, None, None))


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


# ---------------------------------------------------------------------------
# Issue #2836: required-context inventory completeness (AC1-AC10)
# ---------------------------------------------------------------------------

RULESET_CONTEXTS = ["typecheck", "lint", "test", "build", "python-test"]
CLASSIC_CONTEXTS = [*RULESET_CONTEXTS, "validate-generated-artifact"]
ALL_STATUSES = {
    "passed",
    "failed",
    "cancelled",
    "skipped_only",
    "pending_timeout",
    "no_checks",
    "head_sha_changed",
    "gh_error",
    "auth_error",
    "malformed_gh_response",
}
RESULT_KEYS = {
    "schema",
    "status",
    "repo",
    "pr_number",
    "head_sha",
    "current_head_sha",
    "required_only",
    "checks",
    "elapsed_seconds",
    "interval_seconds",
    "timeout_seconds",
    "error_code",
    "message",
}
NO_REQUIRED_STDERR = "no required checks reported on the 'main' branch"
NOT_PROTECTED = (1, '{"message":"Branch not protected","status":"404"}', "gh: Branch not protected (HTTP 404)")


def _ruleset_body(contexts: list[str], integration_id: int | None = None) -> str:
    rules: list[dict[str, Any]] = [{"type": "deletion", "ruleset_id": 1}]
    if contexts:
        rules.append(
            {
                "type": "required_status_checks",
                "ruleset_id": 16796903,
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "required_status_checks": [
                        {"context": c, "integration_id": integration_id} for c in contexts
                    ],
                },
            }
        )
    return json.dumps(rules)


def _classic_body(contexts: list[str], app_id: int = 15368) -> str:
    return json.dumps(
        {
            "strict": False,
            "contexts": list(contexts),
            "checks": [{"context": c, "app_id": app_id} for c in contexts],
        }
    )


def _rows(contexts: list[str], bucket: str = "pass") -> list[dict[str, Any]]:
    return [{"name": c, "bucket": bucket} for c in contexts]


class FakeGh:
    """Routes `run_gh` calls. `checks` is a list of (rc, stdout, stderr) per poll (last repeats)."""

    def __init__(
        self,
        *,
        ruleset: tuple[int, str, str] | None = None,
        classic: tuple[int, str, str] | None = None,
        checks: list[tuple[int, str, str]] | None = None,
        base: str = "main",
        head: str = HEAD_SHA,
    ) -> None:
        self.ruleset = ruleset if ruleset is not None else (0, _ruleset_body(RULESET_CONTEXTS), "")
        self.classic = classic if classic is not None else (0, _classic_body(CLASSIC_CONTEXTS), "")
        self.checks = checks if checks is not None else [(0, "[]", "")]
        self.base = base
        self.head = head
        self.calls: list[list[str]] = []
        self.checks_calls = 0

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(args))
        if args[:2] == ["pr", "view"]:
            if "baseRefName" in args:
                return 0, self.base + "\n", ""
            return 0, self.head + "\n", ""
        if args[0] == "api":
            if "/rules/branches/" in args[1]:
                return self.ruleset
            if args[1].endswith("/protection/required_status_checks"):
                return self.classic
        if args[:2] == ["pr", "checks"]:
            idx = min(self.checks_calls, len(self.checks) - 1)
            self.checks_calls += 1
            return self.checks[idx]
        raise AssertionError(f"unexpected gh call: {args}")

    def api_paths(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "api"]


def _checks_ok(rows: list[dict[str, Any]]) -> tuple[int, str, str]:
    return 0, json.dumps(rows), ""


def _zero_rows() -> tuple[int, str, str]:
    return 1, "", NO_REQUIRED_STDERR


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeGh) -> list[int]:
    """Install real inventory + FakeGh + a deterministic fake clock. Returns sleep log."""
    monkeypatch.setattr(wait_ci_checks, "fetch_required_inventory", _REAL_FETCH_REQUIRED_INVENTORY)
    monkeypatch.setattr(wait_ci_checks, "shutil_which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(wait_ci_checks, "run_gh", fake)
    clock = {"now": 1000.0}
    sleeps: list[int] = []

    def _sleep(seconds: float) -> None:
        sleeps.append(int(seconds))
        clock["now"] += seconds

    monkeypatch.setattr(
        wait_ci_checks, "time", types.SimpleNamespace(time=lambda: clock["now"], sleep=_sleep)
    )
    return sleeps


def _run(capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, dict]:
    argv = ["--repo", "owner/repo", "--pr", "1", "--head-sha", HEAD_SHA, "--required", *extra]
    if "--interval" not in extra:
        argv += ["--interval", "15"]
    if "--timeout-seconds" not in extra:
        argv += ["--timeout-seconds", "60"]
    exit_code = wait_ci_checks.main(argv)
    return exit_code, _parse_marker(capsys.readouterr().out.strip())


def test_effective_inventory_is_union_of_ruleset_and_classic_by_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: Ruleset 5 + Classic 6 -> 6 contexts, deduped, app IDs kept, base from baseRefName."""
    fake = FakeGh(base="release/2026.1")
    _install(monkeypatch, fake)

    inventory, error, message = wait_ci_checks.fetch_required_inventory("owner/repo", 1)

    assert (error, message) == (None, None)
    assert inventory is not None
    assert set(inventory) == set(CLASSIC_CONTEXTS)
    assert len(inventory) == 6
    for context in RULESET_CONTEXTS:
        assert inventory[context] == {None, 15368}  # Ruleset integration_id null + Classic app_id
    assert inventory["validate-generated-artifact"] == {15368}
    # Branch is the PR base (URL-encoded), never a hard-coded main.
    assert fake.api_paths() == [
        "repos/owner/repo/rules/branches/release%2F2026.1?per_page=100",
        "repos/owner/repo/branches/release%2F2026.1/protection/required_status_checks",
    ]
    assert not any("/main" in path for path in fake.api_paths())


_COMMON_FAILURES = [
    ((1, "", "gh: Resource not accessible by integration (HTTP 403)"), "auth_error"),
    ((1, "", "gh: Server Error (HTTP 502)"), "gh_error"),
    ((127, "", "gh not found"), "gh_error"),
    ((1, '{"message":"Not Found"}', "gh: Not Found (HTTP 404)"), "gh_error"),
    ((0, "not-json{", ""), "malformed_gh_response"),
    ((0, '"unexpected"', ""), "malformed_gh_response"),
]


@pytest.mark.parametrize(
    ("failing", "response", "expected_status"),
    [("ruleset", r, e) for r, e in _COMMON_FAILURES]
    + [("classic", r, e) for r, e in _COMMON_FAILURES]
    + [
        ("ruleset", (0, '[{"type":"required_status_checks","parameters":{}}]', ""), "malformed_gh_response"),
        ("classic", (0, '{"checks":[{"context":5}],"contexts":[]}', ""), "malformed_gh_response"),
    ],
)
def test_inventory_source_failure_never_degrades_to_passed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failing: str,
    response: tuple[int, str, str],
    expected_status: str,
) -> None:
    """AC2: either source failing is fail-closed (exit 2); the other source alone is no proof."""
    kwargs: dict[str, Any] = {failing: response}
    # All rows would be present and passing: only the inventory failure can stop `passed`.
    fake = FakeGh(checks=[_checks_ok(_rows(CLASSIC_CONTEXTS))], **kwargs)
    _install(monkeypatch, fake)

    exit_code, payload = _run(capsys)

    assert exit_code == wait_ci_checks.EXIT_RUNTIME
    assert payload["status"] == expected_status
    assert payload["status"] != "passed"
    assert fake.checks_calls == 0


def test_inventory_classic_404_branch_not_protected_is_empty_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: Classic 404 `Branch not protected` is an empty source (Ruleset still applies)."""
    fake = FakeGh(classic=NOT_PROTECTED)
    _install(monkeypatch, fake)

    inventory, error, _message = wait_ci_checks.fetch_required_inventory("owner/repo", 1)

    assert error is None
    assert inventory is not None and set(inventory) == set(RULESET_CONTEXTS)


def test_zero_materialized_rows_with_configured_inventory_waits_until_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC3: inventory 6, `gh pr checks` zero-row error -> pending until pending_timeout (exit 1)."""
    fake = FakeGh(checks=[_zero_rows()])
    sleeps = _install(monkeypatch, fake)

    exit_code, payload = _run(capsys)

    assert exit_code == wait_ci_checks.EXIT_NEGATIVE
    assert payload["status"] == "pending_timeout"
    assert payload["error_code"] == "pending_timeout"
    assert fake.checks_calls >= 2
    assert sleeps  # kept polling instead of terminating on the first zero-row error
    for context in CLASSIC_CONTEXTS:
        assert context in payload["message"]


def test_absent_python_test_row_is_pending_not_passed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC4: no `python-test` row (not even a null-bucket one), other 5 pass -> never passed."""
    present = [c for c in CLASSIC_CONTEXTS if c != "python-test"]
    fake = FakeGh(checks=[_checks_ok(_rows(present))])
    sleeps = _install(monkeypatch, fake)

    exit_code, payload = _run(capsys)

    assert exit_code == wait_ci_checks.EXIT_NEGATIVE
    assert payload["status"] == "pending_timeout"
    assert "python-test" in payload["message"]
    assert "typecheck" not in payload["message"]
    assert sleeps


def test_polling_transition_zero_partial_complete_passes_only_when_all_present(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC5: poll1 = 0 rows, poll2 = 5 rows, poll3 = 6 rows -> passed only at poll 3."""
    fake = FakeGh(
        checks=[
            _zero_rows(),
            _checks_ok(_rows(RULESET_CONTEXTS)),
            _checks_ok(_rows(CLASSIC_CONTEXTS)),
        ]
    )
    sleeps = _install(monkeypatch, fake)

    exit_code, payload = _run(capsys, "--timeout-seconds", "600")

    assert exit_code == wait_ci_checks.EXIT_PASS
    assert payload["status"] == "passed"
    assert fake.checks_calls == 3
    assert len(sleeps) == 2  # polls 1 and 2 did not resolve
    assert {c["name"] for c in payload["checks"]} == set(CLASSIC_CONTEXTS)


def test_ruleset_five_passed_does_not_hide_classic_sixth_context(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC6: Ruleset 5 materialized+pass, Classic-only 6th absent -> not passed until it appears."""
    fake = FakeGh(checks=[_checks_ok(_rows(RULESET_CONTEXTS))])
    _install(monkeypatch, fake)
    exit_code, payload = _run(capsys)
    assert exit_code == wait_ci_checks.EXIT_NEGATIVE
    assert payload["status"] == "pending_timeout"
    assert "validate-generated-artifact" in payload["message"]

    fake = FakeGh(
        checks=[_checks_ok(_rows(RULESET_CONTEXTS)), _checks_ok(_rows(CLASSIC_CONTEXTS))]
    )
    _install(monkeypatch, fake)
    exit_code, payload = _run(capsys, "--timeout-seconds", "600")
    assert exit_code == wait_ci_checks.EXIT_PASS
    assert payload["status"] == "passed"
    assert fake.checks_calls == 2


def test_truly_empty_inventory_keeps_no_checks_contract(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC7: Ruleset [] + Classic 404 -> empty inventory: zero rows => no_checks; rows => bucket only."""
    fake = FakeGh(ruleset=(0, "[]", ""), classic=NOT_PROTECTED, checks=[_zero_rows()])
    sleeps = _install(monkeypatch, fake)
    exit_code, payload = _run(capsys)
    assert exit_code == wait_ci_checks.EXIT_NEGATIVE
    assert payload["status"] == "no_checks"
    assert not sleeps

    fake = FakeGh(
        ruleset=(0, "[]", ""), classic=NOT_PROTECTED, checks=[_checks_ok(_rows(["visual-impact-policy"]))]
    )
    _install(monkeypatch, fake)
    exit_code, payload = _run(capsys)
    assert exit_code == wait_ci_checks.EXIT_PASS
    assert payload["status"] == "passed"


def test_existing_terminal_routing_preserved_with_unmaterialized_context(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC8: fail/cancel resolve immediately despite missing contexts; head change, skipped_only, StatusContext."""
    partial = [c for c in CLASSIC_CONTEXTS if c != "python-test"]

    for bucket, expected in (("fail", "failed"), ("cancel", "cancelled")):
        rows = _rows(partial)
        rows[0]["bucket"] = bucket
        fake = FakeGh(checks=[_checks_ok(rows)])
        sleeps = _install(monkeypatch, fake)
        exit_code, payload = _run(capsys)
        assert exit_code == wait_ci_checks.EXIT_NEGATIVE
        assert payload["status"] == expected
        assert not sleeps

    # Head SHA moved while waiting: fail-closed at resolution time.
    class MovingHead(FakeGh):
        def __call__(self, args: list[str]) -> tuple[int, str, str]:
            if args[:2] == ["pr", "view"] and "headRefOid" in args and self.checks_calls >= 1:
                return 0, "newhead\n", ""
            return super().__call__(args)

    rows = _rows(partial)
    rows[0]["bucket"] = "fail"
    fake = MovingHead(checks=[_checks_ok(rows)])
    _install(monkeypatch, fake)
    exit_code, payload = _run(capsys)
    assert exit_code == wait_ci_checks.EXIT_NEGATIVE
    assert payload["status"] == "head_sha_changed"

    # All 6 materialized: all skipping -> skipped_only, mixed -> failed.
    fake = FakeGh(checks=[_checks_ok(_rows(CLASSIC_CONTEXTS, "skipping"))])
    _install(monkeypatch, fake)
    exit_code, payload = _run(capsys)
    assert (exit_code, payload["status"]) == (wait_ci_checks.EXIT_NEGATIVE, "skipped_only")

    mixed = _rows(CLASSIC_CONTEXTS)
    mixed[0]["bucket"] = "skipping"
    fake = FakeGh(checks=[_checks_ok(mixed)])
    _install(monkeypatch, fake)
    exit_code, payload = _run(capsys)
    assert (exit_code, payload["status"]) == (wait_ci_checks.EXIT_NEGATIVE, "failed")

    # contextType (CheckRun / StatusContext) is not distinguished: bucket only.
    typed = _rows(CLASSIC_CONTEXTS)
    for i, row in enumerate(typed):
        row["contextType"] = "StatusContext" if i % 2 else "CheckRun"
    fake = FakeGh(checks=[_checks_ok(typed)])
    _install(monkeypatch, fake)
    exit_code, payload = _run(capsys)
    assert (exit_code, payload["status"]) == (wait_ci_checks.EXIT_PASS, "passed")


def test_all_configured_contexts_materialized_and_passed_returns_passed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC9: inventory 6 == materialized 6, all pass, head unchanged -> passed (exit 0)."""
    fake = FakeGh(checks=[_checks_ok(_rows(CLASSIC_CONTEXTS))])
    sleeps = _install(monkeypatch, fake)

    exit_code, payload = _run(capsys)

    assert exit_code == wait_ci_checks.EXIT_PASS
    assert payload["status"] == "passed"
    assert payload["error_code"] is None
    assert not sleeps


def test_ci_wait_result_v1_keys_and_statuses_unchanged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC10: CI_WAIT_RESULT_V1 keys (13) and the status value set are unchanged."""
    seen: set[str] = set()
    scenarios = [
        FakeGh(checks=[_checks_ok(_rows(CLASSIC_CONTEXTS))]),
        FakeGh(checks=[_zero_rows()]),
        FakeGh(ruleset=(1, "", "gh: Server Error (HTTP 502)")),
        FakeGh(ruleset=(1, "", "gh: Resource not accessible by integration (HTTP 403)")),
        FakeGh(ruleset=(0, "x{", "")),
        FakeGh(ruleset=(0, "[]", ""), classic=NOT_PROTECTED, checks=[_zero_rows()]),
    ]
    for fake in scenarios:
        _install(monkeypatch, fake)
        _exit, payload = _run(capsys)
        assert set(payload) == RESULT_KEYS
        assert payload["schema"] == "CI_WAIT_RESULT_V1"
        seen.add(payload["status"])
    assert seen <= ALL_STATUSES

    source = Path(wait_ci_checks.__file__).read_text(encoding="utf-8")
    literals = set(re.findall(r'status="([a-z_]+)"', source))
    literals |= set(re.findall(r'return "([a-z_]+)", "', source))  # decide_status values
    assert literals <= ALL_STATUSES | {"pending"}  # "pending" is internal, never emitted
    # Single canonical evaluator: no consumer re-implements inventory completeness.
    assert source.count("def fetch_required_inventory") == 1


# ---------------------------------------------------------------------------
# Issue #2836 F3: HEAD drift while required contexts are pending / at timeout
# ---------------------------------------------------------------------------

NEW_HEAD_SHA = "def456"


class HeadDriftGh(FakeGh):
    """FakeGh whose `headRefOid` answer is decided per `pr view` call by `head_for(call_index)`."""

    def __init__(self, head_for: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.head_for = head_for
        self.head_calls = 0

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        if args[:2] == ["pr", "view"] and "baseRefName" not in args:
            self.calls.append(list(args))
            index = self.head_calls
            self.head_calls += 1
            return self.head_for(index)
        return super().__call__(args)


def test_head_change_while_required_context_unmaterialized_returns_head_sha_changed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F3(1): required context never materializes and HEAD moves mid-wait -> head_sha_changed."""
    # call 0 = pre-wait check (unchanged); every later call sees the new HEAD.
    fake = HeadDriftGh(
        lambda i: (0, (HEAD_SHA if i == 0 else NEW_HEAD_SHA) + "\n", ""),
        checks=[_zero_rows()],
    )
    sleeps = _install(monkeypatch, fake)

    exit_code, payload = _run(capsys)

    assert exit_code == wait_ci_checks.EXIT_NEGATIVE
    assert payload["status"] == "head_sha_changed"
    assert payload["error_code"] == "head_sha_changed"
    assert payload["head_sha"] == HEAD_SHA
    assert payload["current_head_sha"] == NEW_HEAD_SHA
    assert len(sleeps) <= 1  # detected on the first pending poll, not after the full timeout


def test_head_change_just_before_timeout_judgement_prefers_head_sha_changed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F3(2): HEAD is stable for every pending poll but moves at the timeout boundary."""
    holder: dict[str, list[int]] = {"sleeps": []}

    def head_for(_index: int) -> tuple[int, str, str]:
        # 60s timeout / 15s interval: the 4th sleep (elapsed 60) precedes the timeout re-fetch.
        moved = len(holder["sleeps"]) >= 4
        return 0, (NEW_HEAD_SHA if moved else HEAD_SHA) + "\n", ""

    fake = HeadDriftGh(head_for, checks=[_zero_rows()])
    sleeps = _install(monkeypatch, fake)
    holder["sleeps"] = sleeps  # head_for reads the live sleep log

    exit_code, payload = _run(capsys)

    assert exit_code == wait_ci_checks.EXIT_NEGATIVE
    assert payload["status"] == "head_sha_changed"
    assert payload["current_head_sha"] == NEW_HEAD_SHA
    assert len(sleeps) == 4


def test_head_unchanged_pending_still_ends_in_pending_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F3(3a): HEAD never changes -> existing pending_timeout semantics are preserved."""
    fake = HeadDriftGh(lambda _i: (0, HEAD_SHA + "\n", ""), checks=[_zero_rows()])
    sleeps = _install(monkeypatch, fake)

    exit_code, payload = _run(capsys)

    assert exit_code == wait_ci_checks.EXIT_NEGATIVE
    assert payload["status"] == "pending_timeout"
    assert payload["current_head_sha"] == HEAD_SHA
    assert len(sleeps) == 4


def test_head_unchanged_pending_then_completes_passed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F3(3b): HEAD never changes and contexts materialize on the 2nd poll -> passed."""
    fake = HeadDriftGh(
        lambda _i: (0, HEAD_SHA + "\n", ""),
        checks=[_zero_rows(), _checks_ok(_rows(CLASSIC_CONTEXTS))],
    )
    sleeps = _install(monkeypatch, fake)

    exit_code, payload = _run(capsys)

    assert exit_code == wait_ci_checks.EXIT_PASS
    assert payload["status"] == "passed"
    assert payload["current_head_sha"] == HEAD_SHA
    assert len(sleeps) == 1


@pytest.mark.parametrize(
    ("stderr", "expected_status"),
    [
        ("gh: Bad credentials (HTTP 401)", "auth_error"),
        ("gh: Server Error (HTTP 502)", "gh_error"),
    ],
)
def test_head_fetch_error_during_pending_is_not_success_or_head_changed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stderr: str,
    expected_status: str,
) -> None:
    """F3(4): a HEAD-fetch failure while pending keeps classify_gh_error semantics (exit 2)."""
    fake = HeadDriftGh(
        lambda i: (0, HEAD_SHA + "\n", "") if i == 0 else (1, "", stderr),
        checks=[_zero_rows()],
    )
    _install(monkeypatch, fake)

    exit_code, payload = _run(capsys)

    assert exit_code == wait_ci_checks.EXIT_RUNTIME
    assert payload["status"] == expected_status
    assert payload["status"] not in {"passed", "skipped_only", "head_sha_changed", "pending_timeout"}
    assert payload["current_head_sha"] == ""
