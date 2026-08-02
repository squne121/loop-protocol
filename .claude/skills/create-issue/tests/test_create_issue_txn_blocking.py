"""Unit tests for the --blocked-by/--blocking direction fix in create_issue_txn.py (#1946).

Test function names below are referenced directly (bare, module-level, no class) by the
Issue's Verification Commands (e.g. ``pytest ...::test_blocking_and_blocked_by_parse_to_separate_destinations``),
so they are intentionally NOT wrapped in test classes.

Covers:
- AC1: create/reconcile parsers parse --blocked-by/--blocking into separate destinations
- AC2: exact-argv GraphQL addBlockedBy mutation input mapping for both directions
- AC3: invalid dependency inputs (non-positive/bool/duplicate/cross-direction/self/cross-repo)
       are rejected before any mutation
- AC4: direction-specific readback with strict set match + pageInfo/totalCount + target-side
       cross-check for --blocking
- AC5: create/dedupe/reconcile/partial-failure/recovery-hint all handle both directions;
       TransactionResult exposes direction-specific verification results
- AC6: official `gh issue create --blocked-by`/`--blocking` flags are never forwarded to
       _issue_create
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import create_issue_txn as txn  # noqa: E402


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
# AC1: parser destinations
# ---------------------------------------------------------------------------


def test_blocking_and_blocked_by_parse_to_separate_destinations() -> None:
    ns = txn.parse_args(
        [
            "--repo",
            "owner/repo",
            "--title",
            "t",
            "--blocked-by",
            "10",
            "--blocking",
            "20",
            "--blocking",
            "21",
        ]
    )
    assert ns.dependency == [10]
    assert ns.blocking == [20, 21]

    # --dependency remains a legacy alias absorbed into the same destination as --blocked-by.
    ns2 = txn.parse_args(["--repo", "owner/repo", "--title", "t", "--dependency", "5"])
    assert ns2.dependency == [5]
    assert ns2.blocking == []

    # reconcile parser also exposes --blocking as a distinct destination.
    ns3 = txn.parse_args(
        [
            "reconcile",
            "--repo",
            "owner/repo",
            "--issue",
            "99",
            "--dependency",
            "7",
            "--blocking",
            "8",
        ]
    )
    assert ns3.subcommand == "reconcile"
    assert ns3.dependency == [7]
    assert ns3.blocking == [8]


# ---------------------------------------------------------------------------
# AC2: exact-argv GraphQL mutation input mapping
# ---------------------------------------------------------------------------


def test_blocking_direction_exact_argv_mutation_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """--blocking: issueId=<target>, blockingIssueId=<new issue> (new issue blocks target)."""
    captured: dict[str, Any] = {}

    def _fake_run_gh_text(args: list[str], *, stage: str) -> str:
        captured["args"] = args
        captured["stage"] = stage
        return "ok"

    monkeypatch.setattr(txn, "_run_gh_text", _fake_run_gh_text)
    txn._issue_register_blocking("owner/repo", "NEW_ISSUE_NODE", "TARGET_NODE", "gh")

    args = captured["args"]
    assert captured["stage"] == "blocking-register"
    assert "input[issueId]=TARGET_NODE" in args
    assert "input[blockingIssueId]=NEW_ISSUE_NODE" in args
    assert any("addBlockedBy" in a for a in args if a.startswith("query="))


def test_blocked_by_direction_exact_argv_mutation_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """--blocked-by: issueId=<new issue>, blockingIssueId=<dependency> (new issue is blocked)."""
    captured: dict[str, Any] = {}

    def _fake_run_gh_text(args: list[str], *, stage: str) -> str:
        captured["args"] = args
        captured["stage"] = stage
        return "ok"

    monkeypatch.setattr(txn, "_run_gh_text", _fake_run_gh_text)
    txn._issue_register_dependency("owner/repo", "NEW_ISSUE_NODE", "DEP_NODE", "gh")

    args = captured["args"]
    assert captured["stage"] == "dependency-register"
    assert "input[issueId]=NEW_ISSUE_NODE" in args
    assert "input[blockingIssueId]=DEP_NODE" in args

    # Both directions must share the identical addBlockedBy mutation query (only the
    # role-fixed keyword args swap; no separate mutation/query per direction).
    queries: list[str] = []

    def _fake_run_gh_text_2(args2: list[str], *, stage: str) -> str:
        for a in args2:
            if a.startswith("query="):
                queries.append(a)
        return "ok"

    monkeypatch.setattr(txn, "_run_gh_text", _fake_run_gh_text_2)
    txn._issue_register_dependency("owner/repo", "A", "B", "gh")
    txn._issue_register_blocking("owner/repo", "A", "B", "gh")
    assert len(queries) == 2
    assert queries[0] == queries[1]


# ---------------------------------------------------------------------------
# AC3: reject invalid inputs before any mutation
# ---------------------------------------------------------------------------


def test_invalid_dependency_inputs_rejected_before_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Non-positive integers (0, negative) rejected.
    with pytest.raises(txn.TransactionError) as exc_info:
        txn._normalize_dependency_numbers([0], label="--blocked-by")
    assert exc_info.value.stage == "dependency-parse"

    with pytest.raises(txn.TransactionError) as exc_info:
        txn._normalize_dependency_numbers([-5], label="--blocking")
    assert exc_info.value.stage == "dependency-parse"

    # Bool (a subclass of int in Python) rejected.
    with pytest.raises(txn.TransactionError) as exc_info:
        txn._normalize_dependency_numbers([True], label="--blocked-by")
    assert exc_info.value.stage == "dependency-parse"

    # Cross-repository issue URL rejected.
    with pytest.raises(txn.TransactionError) as exc_info:
        txn._normalize_dependency_numbers(["https://github.com/other/repo/issues/5"], label="--blocking")
    assert exc_info.value.stage == "dependency-parse"

    # Duplicate issue numbers within a single direction rejected.
    with pytest.raises(txn.TransactionError) as exc_info:
        txn._validate_dependency_directions([5, 5], [])
    assert exc_info.value.stage == "dependency-validate"

    with pytest.raises(txn.TransactionError) as exc_info:
        txn._validate_dependency_directions([], [6, 6])
    assert exc_info.value.stage == "dependency-validate"

    # Same issue specified in both directions rejected.
    with pytest.raises(txn.TransactionError) as exc_info:
        txn._validate_dependency_directions([7], [7])
    assert exc_info.value.stage == "dependency-validate"

    # Dedupe-target self-reference rejected.
    with pytest.raises(txn.TransactionError) as exc_info:
        txn._validate_dependency_directions([1, 2], [], self_issue_number=2)
    assert exc_info.value.stage == "dependency-validate"

    with pytest.raises(txn.TransactionError) as exc_info:
        txn._validate_dependency_directions([], [9], self_issue_number=9)
    assert exc_info.value.stage == "dependency-validate"

    # End-to-end: run_transaction must fail closed (status=failure) without ever
    # calling gh when the input is invalid.
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("no GitHub mutation should be attempted for invalid input")

    monkeypatch.setattr(txn, "run_command", _boom)
    monkeypatch.setattr(txn, "_run_gh_json", _boom)
    monkeypatch.setattr(txn, "_run_gh_text", _boom)

    result = txn.run_transaction(
        repo="owner/repo",
        title="some title",
        body="body",
        body_file="",
        labels=[],
        parent_issue_number=0,
        dependency_issue_numbers=[3],
        blocking_issue_numbers=[3],
        gh_bin="gh",
    )
    assert result.status == "failure"
    assert result.failure_stage == "dependency-validate"


# ---------------------------------------------------------------------------
# AC4: direction-specific strict readback
# ---------------------------------------------------------------------------


def test_readback_direction_strict_set_match_and_pageinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    # --- blocked-by direction ---
    stdout_ok = (
        '{"data":{"repository":{"issue":{"blockedBy":'
        '{"totalCount":2,"pageInfo":{"hasNextPage":false},'
        '"nodes":[{"number":10},{"number":20}]}}}}}'
    )
    monkeypatch.setattr(txn, "run_command", lambda *_a, **_k: _make_gh_result(stdout=stdout_ok))
    assert txn._readback_dependencies("owner/repo", 99, [10, 20], "gh") is True

    # Strict set equality: an extra unrequested blockedBy entry must NOT pass.
    stdout_superset = (
        '{"data":{"repository":{"issue":{"blockedBy":'
        '{"totalCount":2,"pageInfo":{"hasNextPage":false},'
        '"nodes":[{"number":10},{"number":999}]}}}}}'
    )
    monkeypatch.setattr(txn, "run_command", lambda *_a, **_k: _make_gh_result(stdout=stdout_superset))
    assert txn._readback_dependencies("owner/repo", 99, [10], "gh") is False

    # hasNextPage: True must not pass even if the visible nodes match.
    stdout_paginated = (
        '{"data":{"repository":{"issue":{"blockedBy":'
        '{"totalCount":1,"pageInfo":{"hasNextPage":true},'
        '"nodes":[{"number":10}]}}}}}'
    )
    monkeypatch.setattr(txn, "run_command", lambda *_a, **_k: _make_gh_result(stdout=stdout_paginated))
    assert txn._readback_dependencies("owner/repo", 99, [10], "gh") is False

    # totalCount mismatch must not pass.
    stdout_count_mismatch = (
        '{"data":{"repository":{"issue":{"blockedBy":'
        '{"totalCount":5,"pageInfo":{"hasNextPage":false},'
        '"nodes":[{"number":10}]}}}}}'
    )
    monkeypatch.setattr(txn, "run_command", lambda *_a, **_k: _make_gh_result(stdout=stdout_count_mismatch))
    assert txn._readback_dependencies("owner/repo", 99, [10], "gh") is False

    # --- blocking direction (with target-side cross-check) ---
    calls: list[list[str]] = []

    def _fake_run_pass(args: list[str], **_k: Any) -> Any:
        calls.append(args)
        if len(calls) == 1:
            return _make_gh_result(
                stdout=(
                    '{"data":{"repository":{"issue":{"blocking":'
                    '{"totalCount":1,"pageInfo":{"hasNextPage":false},'
                    '"nodes":[{"number":50}]}}}}}'
                )
            )
        return _make_gh_result(
            stdout='{"data":{"repository":{"issue":{"blockedBy":{"nodes":[{"number":99}]}}}}}'
        )

    monkeypatch.setattr(txn, "run_command", _fake_run_pass)
    assert txn._readback_blocking("owner/repo", 99, [50], "gh") is True
    assert len(calls) == 2

    # Target-side blockedBy missing the new issue -> overall False, even though the
    # new issue's own `blocking` connection looked correct.
    calls2: list[list[str]] = []

    def _fake_run_fail_target(args: list[str], **_k: Any) -> Any:
        calls2.append(args)
        if len(calls2) == 1:
            return _make_gh_result(
                stdout=(
                    '{"data":{"repository":{"issue":{"blocking":'
                    '{"totalCount":1,"pageInfo":{"hasNextPage":false},'
                    '"nodes":[{"number":50}]}}}}}'
                )
            )
        return _make_gh_result(stdout='{"data":{"repository":{"issue":{"blockedBy":{"nodes":[]}}}}}')

    monkeypatch.setattr(txn, "run_command", _fake_run_fail_target)
    assert txn._readback_blocking("owner/repo", 99, [50], "gh") is False

    # Own-side mismatch (blocking connection empty) -> False without even reaching the
    # target-side cross-check.
    stdout_own_side_mismatch = (
        '{"data":{"repository":{"issue":{"blocking":'
        '{"totalCount":0,"pageInfo":{"hasNextPage":false},'
        '"nodes":[]}}}}}'
    )
    monkeypatch.setattr(txn, "run_command", lambda *_a, **_k: _make_gh_result(stdout=stdout_own_side_mismatch))
    assert txn._readback_blocking("owner/repo", 99, [50], "gh") is False


# ---------------------------------------------------------------------------
# AC5: dedupe/reconcile/partial-failure/recovery-hint bidirectional handling
# ---------------------------------------------------------------------------


def test_dedupe_reconcile_partial_failure_bidirectional(monkeypatch: pytest.MonkeyPatch) -> None:
    # --- reconcile_transaction registers+readbacks the --blocking direction ---
    monkeypatch.setattr(txn, "_issue_apply_labels", lambda *_a, **_k: None)
    monkeypatch.setattr(txn, "_readback_labels", lambda *_a, **_k: True)
    monkeypatch.setattr(
        txn,
        "_readback_labels_with_result",
        lambda *_a, **_k: MagicMock(expected_labels=[], actual_labels=[], attempts=1, retry_delays=[]),
    )

    register_calls: list[tuple[Any, ...]] = []

    def _spy_register(repo: str, child_node_id: str, target_node_id: str, gh_bin: str) -> None:
        register_calls.append((repo, child_node_id, target_node_id))

    def _spy_graphql_ids(repo: str, issue_number: int, gh_bin: str) -> tuple[str, int]:
        return (f"node-{issue_number}", issue_number * 100)

    readback_calls: list[Any] = []

    def _spy_readback(repo: str, issue_number: int, nums: list[int], gh_bin: str) -> bool:
        readback_calls.append((repo, issue_number, nums))
        return True

    monkeypatch.setattr(txn, "_issue_graphql_ids", _spy_graphql_ids)
    monkeypatch.setattr(txn, "_issue_register_blocking", _spy_register)
    monkeypatch.setattr(txn, "_readback_blocking", _spy_readback)

    fake_sleep = FakeSleep()
    result = txn.reconcile_transaction(
        repo="owner/repo",
        issue_number=99,
        labels=["label1"],
        parent_issue_number=0,
        dependency_issue_numbers=[],
        blocking_issue_numbers=[10, 20],
        gh_bin="gh",
        sleep_fn=fake_sleep,
    )
    assert len(register_calls) == 2, f"Expected 2 register calls, got {register_calls}"
    assert len(readback_calls) >= 1, "_readback_blocking must be called at least once"
    assert result.status == "success"
    assert result.blocking_verified is True

    # --- run_transaction (create path): both directions reported independently ---
    monkeypatch.setattr(txn, "_run_issue_body_validator", lambda *_a, **_k: {"status": "pass"})
    monkeypatch.setattr(txn, "_find_open_issues_by_title", lambda *_a, **_k: [])
    monkeypatch.setattr(txn, "_issue_create", lambda *_a, **_k: "https://github.com/owner/repo/issues/500")
    monkeypatch.setattr(txn, "_poll_for_created_issue", lambda *_a, **_k: ("ok", []))
    monkeypatch.setattr(txn, "_issue_register_dependency", lambda *_a, **_k: None)
    monkeypatch.setattr(txn, "_readback_dependencies", lambda *_a, **_k: True)

    fake_sleep2 = FakeSleep()
    result2 = txn.run_transaction(
        repo="owner/repo",
        title="t",
        body="",
        body_file="",
        labels=[],
        parent_issue_number=0,
        dependency_issue_numbers=[10],
        blocking_issue_numbers=[20],
        gh_bin="gh",
        sleep_fn=fake_sleep2,
    )
    assert result2.status == "success"
    assert result2.issue_number == 500
    assert result2.dependency_verified is True
    assert result2.blocking_verified is True

    # --- run_transaction (create path): blocking readback failure -> partial_failure ---
    monkeypatch.setattr(txn, "_issue_create", lambda *_a, **_k: "https://github.com/owner/repo/issues/501")
    monkeypatch.setattr(txn, "_readback_blocking", lambda *_a, **_k: False)
    monkeypatch.setattr(txn, "_post_partial_failure_comment", lambda *_a, **_k: None)

    fake_sleep3 = FakeSleep()
    result3 = txn.run_transaction(
        repo="owner/repo",
        title="t",
        body="",
        body_file="",
        labels=[],
        parent_issue_number=0,
        dependency_issue_numbers=[],
        blocking_issue_numbers=[30],
        gh_bin="gh",
        sleep_fn=fake_sleep3,
    )
    assert result3.status == "partial_failure"
    assert result3.failure_stage == "blocking-readback"

    # --- dedupe path also reconciles the --blocking direction ---
    monkeypatch.setattr(txn, "_find_open_issues_by_title", lambda *_a, **_k: [700])
    monkeypatch.setattr(txn, "_run_gh_json", lambda *_a, **_k: {"body": "", "number": 700})
    monkeypatch.setattr(txn, "_readback_blocking", lambda *_a, **_k: True)

    fake_sleep4 = FakeSleep()
    result4 = txn.run_transaction(
        repo="owner/repo",
        title="t",
        body="",
        body_file="",
        labels=[],
        parent_issue_number=0,
        dependency_issue_numbers=[],
        blocking_issue_numbers=[42],
        gh_bin="gh",
        sleep_fn=fake_sleep4,
    )
    assert result4.status == "dedupe"
    assert result4.dedupe_number == 700
    assert result4.blocking_verified is True

    # --- recovery hint covers the blocking stage ---
    hint = txn._recovery_hint_for_stage("blocking-readback", "owner/repo", 42, 0, [], [99])
    assert "#99" in hint
    assert "input[blockingIssueId]=<new_issue_node_id>" in hint
    assert "\n" in hint


# ---------------------------------------------------------------------------
# AC6: official gh CLI dependency flags must never be forwarded to _issue_create
# ---------------------------------------------------------------------------


def test_issue_create_does_not_forward_dependency_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_run_gh_text(args: list[str], *, stage: str) -> str:
        captured["args"] = args
        return "https://github.com/owner/repo/issues/1"

    monkeypatch.setattr(txn, "_run_gh_text", _fake_run_gh_text)
    txn._issue_create("owner/repo", "title", "body", "", "gh")

    args = captured["args"]
    assert "--blocked-by" not in args
    assert "--blocking" not in args
    assert "--dependency" not in args

    # _issue_create must not even accept a dependency/blocking parameter -- the only way
    # to reach an addBlockedBy mutation is through the separate helpers below it.
    import inspect

    sig = inspect.signature(txn._issue_create)
    names = set(sig.parameters.keys())
    assert "dependency_issue_numbers" not in names
    assert "blocking_issue_numbers" not in names
