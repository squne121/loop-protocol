"""Unit tests for create_issue_txn.py.

Covers:
- AC2: transient-false-then-true parent readback -> success/dedupe
- AC3: all-false parent readback -> partial_failure, failure_stage == "sub-issue-readback"
- AC4: sleep_fn injection (fake sleep, no real time.sleep)
- AC5: both new-issue and dedupe-reconcile paths use same helper (_readback_parent_issue_with_retry)
- AC7: this file itself is the artifact that must PASS under uv run pytest
- AC8/AC9: issue_kind == "implementation" adds 4 standard labels; other kinds do not
- AC10: label auto-assign for implementation vs non-implementation kinds
- _readback_parent_issue issues an HTTP GET with an Accept header (not -F params)
- dedupe-label-readback maps to an actionable recovery hint
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# Resolve the scripts directory so we can import without installing.
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import create_issue_txn as txn  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeSleep:
    """Records sleep calls without actually sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, delay: float) -> None:
        self.calls.append(delay)


def _make_gh_result(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


# ---------------------------------------------------------------------------
# AC4: _readback_parent_issue_with_retry uses sleep_fn injection
# ---------------------------------------------------------------------------

class TestParentReadbackRetryFakeSleep:
    """AC4: sleep_fn is injected; real time.sleep must not be called."""

    def test_fake_sleep_is_called_not_real_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_sleep = FakeSleep()
        # _readback_parent_issue always returns False -> triggers all retries
        monkeypatch.setattr(txn, "_readback_parent_issue", lambda *_args, **_kw: False)

        result = txn._readback_parent_issue_with_retry(
            repo="owner/repo",
            issue_number=99,
            parent_issue_number=40,
            gh_bin="gh",
            sleep_fn=fake_sleep,
        )

        assert result is False
        # All retry delays must have been passed to fake_sleep
        assert fake_sleep.calls == list(txn._PARENT_READBACK_RETRY_DELAYS)

    def test_no_sleep_when_first_attempt_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_sleep = FakeSleep()
        monkeypatch.setattr(txn, "_readback_parent_issue", lambda *_args, **_kw: True)

        result = txn._readback_parent_issue_with_retry(
            repo="owner/repo",
            issue_number=99,
            parent_issue_number=40,
            gh_bin="gh",
            sleep_fn=fake_sleep,
        )

        assert result is True
        assert fake_sleep.calls == [], "No sleep should occur when first attempt succeeds"


# ---------------------------------------------------------------------------
# AC2: transient false -> true returns success/dedupe (not partial_failure)
# ---------------------------------------------------------------------------

class TestParentReadbackRetryTransientFalse:
    """AC2: helper returns True after transient failure."""

    def test_succeeds_after_one_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_sleep = FakeSleep()
        call_count = 0

        def _mock_readback(*_args: Any, **_kw: Any) -> bool:
            nonlocal call_count
            call_count += 1
            return call_count > 1  # First call False, subsequent True

        monkeypatch.setattr(txn, "_readback_parent_issue", _mock_readback)

        result = txn._readback_parent_issue_with_retry(
            repo="owner/repo",
            issue_number=99,
            parent_issue_number=40,
            gh_bin="gh",
            sleep_fn=fake_sleep,
        )

        assert result is True
        assert len(fake_sleep.calls) == 1, "Exactly one sleep before second attempt"
        assert fake_sleep.calls[0] == txn._PARENT_READBACK_RETRY_DELAYS[0]

    def test_succeeds_on_final_retry_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """False on every attempt but the last -> success still wins within budget.

        Exercises the full retry budget (initial check + every delay in
        _PARENT_READBACK_RETRY_DELAYS) and pins that a confirmation arriving on
        the final attempt is treated as success.
        """
        fake_sleep = FakeSleep()
        delays = txn._PARENT_READBACK_RETRY_DELAYS
        outcomes = iter([False] * len(delays) + [True])
        monkeypatch.setattr(txn, "_readback_parent_issue", lambda *_a, **_kw: next(outcomes))

        result = txn._readback_parent_issue_with_retry(
            repo="owner/repo",
            issue_number=99,
            parent_issue_number=40,
            gh_bin="gh",
            sleep_fn=fake_sleep,
        )

        assert result is True
        assert fake_sleep.calls == list(delays), "Every retry delay must be consumed"


# ---------------------------------------------------------------------------
# AC3: all-false -> partial_failure with failure_stage == "sub-issue-readback"
# ---------------------------------------------------------------------------

class TestParentReadbackRetryAllFails:
    """AC3: all attempts fail -> helper returns False -> TransactionError raised."""

    def test_returns_false_after_all_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_sleep = FakeSleep()
        monkeypatch.setattr(txn, "_readback_parent_issue", lambda *_args, **_kw: False)

        result = txn._readback_parent_issue_with_retry(
            repo="owner/repo",
            issue_number=99,
            parent_issue_number=40,
            gh_bin="gh",
            sleep_fn=fake_sleep,
        )

        assert result is False
        assert len(fake_sleep.calls) == len(txn._PARENT_READBACK_RETRY_DELAYS)


# ---------------------------------------------------------------------------
# AC3 (integration): run_transaction returns partial_failure when all readbacks fail
# ---------------------------------------------------------------------------

class TestRunTransactionParentReadbackAllFail:
    """AC3 (integration): run_transaction returns partial_failure + failure_stage == sub-issue-readback."""

    def _patch_successful_create(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch all non-readback calls so the transaction reaches parent readback."""
        monkeypatch.setattr(txn, "_find_open_issues_by_title", lambda *_a, **_k: [])
        monkeypatch.setattr(txn, "_issue_create", lambda *_a, **_k: "https://github.com/owner/repo/issues/99")
        monkeypatch.setattr(
            txn,
            "_poll_for_created_issue",
            lambda *_a, **_k: ("confirmed", [99]),
        )
        monkeypatch.setattr(txn, "_issue_apply_labels", lambda *_a, **_k: None)
        monkeypatch.setattr(txn, "_readback_labels", lambda *_a, **_k: True)
        monkeypatch.setattr(
            txn,
            "_issue_graphql_ids",
            lambda *_a, **_k: ("node-child", 9901),
        )
        monkeypatch.setattr(txn, "_issue_register_sub_issue", lambda *_a, **_k: None)
        monkeypatch.setattr(txn, "_post_partial_failure_comment", lambda *_a, **_k: None)

    def test_partial_failure_stage_sub_issue_readback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_successful_create(monkeypatch)
        fake_sleep = FakeSleep()
        monkeypatch.setattr(txn, "_readback_parent_issue", lambda *_a, **_k: False)

        result = txn.run_transaction(
            repo="owner/repo",
            title="Test Issue",
            body="",
            body_file="",
            labels=[],
            issue_kind="",
            parent_issue_number=40,
            dependency_issue_numbers=[],
            gh_bin="gh",
            sleep_fn=fake_sleep,
        )

        assert result.status == "partial_failure"
        assert result.failure_stage == "sub-issue-readback"


# ---------------------------------------------------------------------------
# AC2 (integration): run_transaction returns success when readback recovers
# ---------------------------------------------------------------------------

class TestRunTransactionParentReadbackRecovery:
    """AC2 (integration): run_transaction returns success when readback recovers after transient false."""

    def _patch_successful_create(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(txn, "_find_open_issues_by_title", lambda *_a, **_k: [])
        monkeypatch.setattr(txn, "_issue_create", lambda *_a, **_k: "https://github.com/owner/repo/issues/99")
        monkeypatch.setattr(
            txn,
            "_poll_for_created_issue",
            lambda *_a, **_k: ("confirmed", [99]),
        )
        monkeypatch.setattr(txn, "_issue_apply_labels", lambda *_a, **_k: None)
        monkeypatch.setattr(txn, "_readback_labels", lambda *_a, **_k: True)
        monkeypatch.setattr(
            txn,
            "_issue_graphql_ids",
            lambda *_a, **_k: ("node-child", 9901),
        )
        monkeypatch.setattr(txn, "_issue_register_sub_issue", lambda *_a, **_k: None)

    def test_success_after_transient_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_successful_create(monkeypatch)
        fake_sleep = FakeSleep()
        call_count = 0

        def _mock_readback(*_a: Any, **_k: Any) -> bool:
            nonlocal call_count
            call_count += 1
            return call_count > 1  # First False, then True

        monkeypatch.setattr(txn, "_readback_parent_issue", _mock_readback)

        result = txn.run_transaction(
            repo="owner/repo",
            title="Test Issue",
            body="",
            body_file="",
            labels=[],
            issue_kind="",
            parent_issue_number=40,
            dependency_issue_numbers=[],
            gh_bin="gh",
            sleep_fn=fake_sleep,
        )

        assert result.status == "success"
        assert result.failure_stage is None
        assert result.parent_verified is True


# ---------------------------------------------------------------------------
# AC5: both paths use same helper
# (verified by code inspection; this test confirms the helper is called consistently)
# ---------------------------------------------------------------------------

class TestBothPathsUseHelper:
    """AC5: dedupe reconcile path also uses _readback_parent_issue_with_retry."""

    def test_dedupe_path_calls_helper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When a duplicate issue is found, _reconcile_issue_links uses the helper."""
        monkeypatch.setattr(txn, "_find_open_issues_by_title", lambda *_a, **_k: [55])
        monkeypatch.setattr(txn, "_readback_labels", lambda *_a, **_k: True)
        monkeypatch.setattr(txn, "_post_partial_failure_comment", lambda *_a, **_k: None)

        helper_calls: list[tuple[Any, ...]] = []
        original_helper = txn._readback_parent_issue_with_retry

        def _spy_helper(*args: Any, **kwargs: Any) -> bool:
            helper_calls.append(args)
            return original_helper(*args, **kwargs)

        # Make the actual readback return True so we get dedupe success
        monkeypatch.setattr(txn, "_readback_parent_issue", lambda *_a, **_k: True)
        monkeypatch.setattr(txn, "_readback_parent_issue_with_retry", _spy_helper)

        fake_sleep = FakeSleep()
        result = txn.run_transaction(
            repo="owner/repo",
            title="Existing Title",
            body="",
            body_file="",
            labels=[],
            issue_kind="",
            parent_issue_number=40,
            dependency_issue_numbers=[],
            gh_bin="gh",
            sleep_fn=fake_sleep,
        )

        assert result.status == "dedupe"
        assert len(helper_calls) == 1, "_readback_parent_issue_with_retry was called in dedupe path"


# ---------------------------------------------------------------------------
# AC8 / AC9 / AC10: _resolve_labels — implementation kind auto-assign
# ---------------------------------------------------------------------------

class TestResolveLables:
    """AC8/AC9/AC10: standard label auto-assignment for implementation kind only."""

    def test_implementation_kind_prepends_standard_labels(self) -> None:
        """AC8/AC10: implementation kind adds all 4 standard labels."""
        result = txn._resolve_labels([], "implementation")
        for label in txn._IMPLEMENTATION_STANDARD_LABELS:
            assert label in result, f"Expected '{label}' in result"
        assert len(result) == len(txn._IMPLEMENTATION_STANDARD_LABELS)

    def test_implementation_kind_merges_caller_labels(self) -> None:
        """AC8: caller labels are preserved alongside standard labels."""
        result = txn._resolve_labels(["custom-label"], "implementation")
        for label in txn._IMPLEMENTATION_STANDARD_LABELS:
            assert label in result
        assert "custom-label" in result

    def test_implementation_kind_no_duplicate_standard_labels(self) -> None:
        """AC8: no duplicates when caller already provides a standard label."""
        result = txn._resolve_labels(["state/queued"], "implementation")
        assert result.count("state/queued") == 1

    def test_research_kind_does_not_add_standard_labels(self) -> None:
        """AC9/AC10: research kind must NOT trigger label auto-assign."""
        result = txn._resolve_labels([], "research")
        for label in txn._IMPLEMENTATION_STANDARD_LABELS:
            assert label not in result, f"Standard label '{label}' should not be added for research kind"

    def test_parent_kind_does_not_add_standard_labels(self) -> None:
        """AC9/AC10: parent kind must NOT trigger label auto-assign."""
        result = txn._resolve_labels([], "parent")
        for label in txn._IMPLEMENTATION_STANDARD_LABELS:
            assert label not in result

    def test_bug_report_kind_does_not_add_standard_labels(self) -> None:
        """AC9/AC10: bug-report kind must NOT trigger label auto-assign."""
        result = txn._resolve_labels([], "bug-report")
        for label in txn._IMPLEMENTATION_STANDARD_LABELS:
            assert label not in result

    def test_empty_kind_does_not_add_standard_labels(self) -> None:
        """AC9: empty kind must NOT trigger label auto-assign."""
        result = txn._resolve_labels(["existing-label"], "")
        assert result == ["existing-label"]
        for label in txn._IMPLEMENTATION_STANDARD_LABELS:
            assert label not in result


# ---------------------------------------------------------------------------
# AC10 (integration): run_transaction with implementation kind -> labels passed
# ---------------------------------------------------------------------------

class TestRunTransactionImplementationLabels:
    """AC10 (integration): run_transaction calls _issue_apply_labels with standard labels for implementation."""

    def test_implementation_kind_applies_standard_labels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(txn, "_find_open_issues_by_title", lambda *_a, **_k: [])
        monkeypatch.setattr(txn, "_issue_create", lambda *_a, **_k: "https://github.com/owner/repo/issues/100")
        monkeypatch.setattr(txn, "_poll_for_created_issue", lambda *_a, **_k: ("confirmed", [100]))
        monkeypatch.setattr(txn, "_readback_labels", lambda *_a, **_k: True)

        applied_labels: list[list[str]] = []

        def _capture_apply(repo: str, issue_number: int, labels: list[str], gh_bin: str) -> None:
            applied_labels.append(list(labels))

        monkeypatch.setattr(txn, "_issue_apply_labels", _capture_apply)

        fake_sleep = FakeSleep()
        result = txn.run_transaction(
            repo="owner/repo",
            title="Test Implementation Issue",
            body="",
            body_file="",
            labels=[],
            issue_kind="implementation",
            parent_issue_number=0,
            dependency_issue_numbers=[],
            gh_bin="gh",
            sleep_fn=fake_sleep,
        )

        assert result.status == "success"
        assert len(applied_labels) == 1
        for label in txn._IMPLEMENTATION_STANDARD_LABELS:
            assert label in applied_labels[0], f"Standard label '{label}' must be applied"

    def test_non_implementation_kind_does_not_apply_standard_labels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(txn, "_find_open_issues_by_title", lambda *_a, **_k: [])
        monkeypatch.setattr(txn, "_issue_create", lambda *_a, **_k: "https://github.com/owner/repo/issues/101")
        monkeypatch.setattr(txn, "_poll_for_created_issue", lambda *_a, **_k: ("confirmed", [101]))
        monkeypatch.setattr(txn, "_readback_labels", lambda *_a, **_k: True)

        applied_labels: list[list[str]] = []

        def _capture_apply(repo: str, issue_number: int, labels: list[str], gh_bin: str) -> None:
            applied_labels.append(list(labels))

        monkeypatch.setattr(txn, "_issue_apply_labels", _capture_apply)

        fake_sleep = FakeSleep()
        result = txn.run_transaction(
            repo="owner/repo",
            title="Test Research Issue",
            body="",
            body_file="",
            labels=[],
            issue_kind="research",
            parent_issue_number=0,
            dependency_issue_numbers=[],
            gh_bin="gh",
            sleep_fn=fake_sleep,
        )

        assert result.status == "success"
        # _issue_apply_labels is only called when labels is non-empty; with no labels and research kind, it's empty
        if applied_labels:
            for label in txn._IMPLEMENTATION_STANDARD_LABELS:
                assert label not in applied_labels[0], f"Standard label '{label}' must NOT be applied for research kind"


# ---------------------------------------------------------------------------
# AC1 (constants): retry delays total ≤ 2 seconds
# ---------------------------------------------------------------------------

class TestRetryDelaysContract:
    """AC1: _PARENT_READBACK_RETRY_DELAYS is a module-level constant and total ≤ 2 seconds."""

    def test_delays_are_defined_as_module_constant(self) -> None:
        assert hasattr(txn, "_PARENT_READBACK_RETRY_DELAYS")
        assert isinstance(txn._PARENT_READBACK_RETRY_DELAYS, tuple)
        assert len(txn._PARENT_READBACK_RETRY_DELAYS) >= 1

    def test_total_delay_does_not_exceed_two_seconds(self) -> None:
        total = sum(txn._PARENT_READBACK_RETRY_DELAYS)
        assert total <= 2.0, f"Total retry delay {total}s exceeds 2-second budget"

    def test_implementation_standard_labels_are_defined(self) -> None:
        assert hasattr(txn, "_IMPLEMENTATION_STANDARD_LABELS")
        expected = {"state/queued", "phase/implementation", "agent/implementer", "enhancement"}
        assert set(txn._IMPLEMENTATION_STANDARD_LABELS) == expected


# ---------------------------------------------------------------------------
# Blocker: _readback_parent_issue must call `gh api` as an HTTP GET with an
# Accept header. A -F request parameter would flip `gh api` to POST, which the
# GET-only /parent sub-resource rejects -> readback would always fail.
# ---------------------------------------------------------------------------

class TestReadbackParentIssueGhApiContract:
    """_readback_parent_issue issues a GET with an Accept header, not -F params."""

    @staticmethod
    def _capture_args(monkeypatch: pytest.MonkeyPatch, captured: dict[str, list[str]]) -> None:
        def fake_run_command(args: list[str], **_kwargs: Any) -> Any:
            captured["args"] = args
            return _make_gh_result(stdout='{"number": 40}', returncode=0)

        monkeypatch.setattr(txn, "run_command", fake_run_command)

    def test_uses_get_method_and_accept_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, list[str]] = {}
        self._capture_args(monkeypatch, captured)

        assert txn._readback_parent_issue("owner/repo", 99, 40, "gh") is True

        args = captured["args"]
        assert "--method" in args
        assert args[args.index("--method") + 1] == "GET"
        assert "-H" in args
        assert "Accept: application/vnd.github+json" in args
        # `gh api` switches to POST when any -f/-F request parameter is present.
        assert "-F" not in args
        assert "accept=application/vnd.github+json" not in args

    def test_endpoint_targets_parent_subresource(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, list[str]] = {}
        self._capture_args(monkeypatch, captured)

        txn._readback_parent_issue("owner/repo", 99, 40, "gh")

        assert "repos/owner/repo/issues/99/parent" in captured["args"]


# ---------------------------------------------------------------------------
# dedupe-label-readback recovery hint: the dedupe reconcile path raises
# stage="dedupe-label-readback", which must map to an actionable hint.
# ---------------------------------------------------------------------------

class TestRecoveryHintDedupeLabelReadback:
    """dedupe-label-readback yields the same actionable label re-apply hint."""

    def test_dedupe_label_readback_hint_is_actionable(self) -> None:
        hint = txn._recovery_hint_for_stage("dedupe-label-readback", "owner/repo", 99, 0, [])

        assert "dedupe-label-readback" in hint
        assert "gh issue edit 99" in hint
        assert "--add-label" in hint

    def test_unknown_stage_falls_back_to_generic_hint(self) -> None:
        hint = txn._recovery_hint_for_stage("totally-unknown-stage", "owner/repo", 99, 0, [])

        assert "totally-unknown-stage" in hint
