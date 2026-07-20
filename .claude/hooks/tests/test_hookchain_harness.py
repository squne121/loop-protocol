"""test_hookchain_harness.py -- Issue #1636 AC2.

Unit tests for `hookchain_harness.classify_decision` (and the aggregate
helper), covering the six-value decision vocabulary
(deny | defer | ask | allow | no_decision | hook_error) with positive and
negative cases for each value, using only synthetic (returncode, stdout)
pairs -- no subprocess execution of real hooks (that is covered separately
by `scripts/agent-ops/tests/test_pr_review_marker_archive_hookchain.py`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hookchain_harness  # noqa: E402


def _structured(permission_decision: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": permission_decision,
            }
        }
    )


class TestDenyClassification:
    def test_exit_2_no_json_is_deny(self):
        assert hookchain_harness.classify_decision(2, "") == "deny"

    def test_structured_permission_decision_deny(self):
        assert hookchain_harness.classify_decision(0, _structured("deny")) == "deny"

    def test_legacy_decision_block_is_deny(self):
        stdout = json.dumps({"decision": "block"})
        assert hookchain_harness.classify_decision(2, stdout) == "deny"

    def test_exit_2_overrides_structured_allow_and_ask(self):
        assert hookchain_harness.classify_decision(2, _structured("allow")) == "deny"
        assert hookchain_harness.classify_decision(2, _structured("ask")) == "deny"

    def test_exit_0_is_not_deny(self):
        assert hookchain_harness.classify_decision(0, "") != "deny"


class TestDeferClassification:
    def test_structured_permission_decision_defer(self):
        assert hookchain_harness.classify_decision(0, _structured("defer")) == "defer"

    def test_exit_0_plain_is_not_defer(self):
        assert hookchain_harness.classify_decision(0, "") != "defer"


class TestAskClassification:
    def test_structured_permission_decision_ask(self):
        assert hookchain_harness.classify_decision(0, _structured("ask")) == "ask"

    def test_exit_1_is_not_ask(self):
        # Issue #1636 AC1: exit 1 must be classified as hook_error, not ask.
        assert hookchain_harness.classify_decision(1, "") != "ask"


class TestAllowClassification:
    def test_structured_permission_decision_allow(self):
        assert hookchain_harness.classify_decision(0, _structured("allow")) == "allow"

    def test_legacy_decision_approve_is_allow(self):
        stdout = json.dumps({"decision": "approve"})
        assert hookchain_harness.classify_decision(0, stdout) == "allow"

    def test_exit_2_is_not_allow(self):
        assert hookchain_harness.classify_decision(2, "") != "allow"


class TestNoDecisionClassification:
    def test_exit_0_hook_specific_output_without_permission_decision_is_no_decision(self):
        stdout = json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": "CI_TEST_PERFORMANCE_ADVISORY_V1 {}",
                }
            }
        )
        assert hookchain_harness.classify_decision(0, stdout) == "no_decision"

    def test_silent_exit_0_is_no_decision(self):
        assert hookchain_harness.classify_decision(0, "") == "no_decision"


class TestHookErrorClassification:
    def test_exit_1_no_json_is_hook_error(self):
        assert hookchain_harness.classify_decision(1, "") == "hook_error"

    def test_exit_1_is_not_ask_regression(self):
        """AC1: exit 1 must be classified as hook_error, not ask."""
        assert hookchain_harness.classify_decision(1, "not json") == "hook_error"

    def test_nonzero_stdout_is_ignored(self):
        assert hookchain_harness.classify_decision(1, _structured("deny")) == "hook_error"
        assert hookchain_harness.classify_decision(1, _structured("ask")) == "hook_error"

    def test_exit_other_nonzero_non_2_is_hook_error(self):
        assert hookchain_harness.classify_decision(127, "") == "hook_error"

    def test_exit_0_is_not_hook_error(self):
        assert hookchain_harness.classify_decision(0, "") != "hook_error"


def test_decision_vocabulary_distinguishes_hook_error_from_ask():
    """Issue #1636 AC1 canonical regression test (referenced by name in the
    Issue's Verification Commands): exit code 1 without a structured
    permissionDecision must classify as "hook_error", never "ask", and an
    explicit structured "ask" permissionDecision must still classify as
    "ask" regardless of exit code."""
    assert hookchain_harness.classify_decision(1, "") == "hook_error"
    assert hookchain_harness.classify_decision(1, "some stderr-only output") == "hook_error"
    assert hookchain_harness.classify_decision(0, _structured("ask")) == "ask"
    assert hookchain_harness.classify_decision(1, _structured("ask")) == "hook_error"


class TestAggregateDecision:
    def test_aggregate_deny_wins(self):
        results = [
            {"decision": "allow"},
            {"decision": "deny"},
            {"decision": "ask"},
        ]
        assert hookchain_harness.aggregate_decision(results) == "block"

    def test_aggregate_ask_wins_over_allow(self):
        results = [{"decision": "allow"}, {"decision": "ask"}]
        assert hookchain_harness.aggregate_decision(results) == "ask"

    def test_aggregate_all_allow_is_allow(self):
        results = [{"decision": "allow"}, {"decision": "allow"}]
        assert hookchain_harness.aggregate_decision(results) == "allow"

    def test_legacy_aggregate_maps_defer_conservatively_to_ask(self):
        results = [
            {"decision": "no_decision"},
            {"decision": "defer"},
            {"decision": "hook_error"},
        ]
        assert hookchain_harness.aggregate_decision(results) == "ask"


class TestLosslessAggregatePermissionDecision:
    def test_defer_precedes_ask_and_allow(self):
        results = [{"decision": "allow"}, {"decision": "defer"}, {"decision": "ask"}]
        assert hookchain_harness.aggregate_permission_decision(results) == "defer"

    def test_deny_precedes_defer_and_ask(self):
        results = [{"decision": "deny"}, {"decision": "defer"}, {"decision": "ask"}]
        assert hookchain_harness.aggregate_permission_decision(results) == "deny"

    def test_no_decision_is_preserved(self):
        assert hookchain_harness.aggregate_permission_decision([{"decision": "no_decision"}]) == "no_decision"


def test_chain_collects_all_matching_hooks_after_deny(monkeypatch, tmp_path):
    """A deny contributes to the aggregate but does not short-circuit sibling hooks."""
    commands = ["first-hook", "second-hook"]
    seen: list[str] = []

    monkeypatch.setattr(hookchain_harness, "load_pretool_hook_commands", lambda _: commands)
    monkeypatch.setattr(hookchain_harness, "_resolve_command", lambda command: [command])

    def fake_run(argv, **_kwargs):
        seen.append(argv[0])
        return SimpleNamespace(
            returncode=2 if argv[0] == "first-hook" else 0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(hookchain_harness.subprocess, "run", fake_run)
    results = hookchain_harness.run_pretool_hook_chain({}, tmp_path)

    assert seen == commands
    assert [result["decision"] for result in results] == ["deny", "no_decision"]
