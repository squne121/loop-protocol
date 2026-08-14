"""Fixture-based unit/contract tests for the web-researcher AGY -> native
Web fallback canary (Issue #2166 AC3/AC4/AC5).

These tests never spawn a live Claude Code process: they cover only the
pure PASS-predicate/causal-ordering/exit-code contract functions and the
invocation-private AGY failure injection mechanism's own hygiene (temp-dir
scoped, no repo-global/user-global file writes).
"""
from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PRODUCER_PATH = REPO_ROOT / "scripts" / "agent-ops" / "run_web_researcher_fallback_canary_smoke.py"


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def producer() -> types.ModuleType:
    return _load_module(PRODUCER_PATH, "test_web_researcher_fallback_canary_smoke_producer")


def _full_evidence(producer: types.ModuleType, **overrides) -> dict:
    evidence = {key: True for key in producer.EVIDENCE_SIGNAL_KEYS}
    evidence.update(overrides)
    return evidence


def _full_causal_timestamps(producer: types.ModuleType, **overrides) -> dict:
    timestamps = {key: float(i + 1) for i, key in enumerate(producer.CAUSAL_ORDER_KEYS)}
    timestamps.update(overrides)
    return timestamps


# ---------------------------------------------------------------------------
# AC1: script exists and exposes --help.
# ---------------------------------------------------------------------------


class TestScriptExists:
    def test_producer_file_exists(self):
        assert PRODUCER_PATH.is_file()

    def test_build_parser_supports_help(self, producer: types.ModuleType):
        parser = producer.build_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["--help"])
        assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# AC3: PASS predicate conjunction -- each of the 11 evidence signals is
# individually required; any single missing/false signal fails the whole
# trial closed.
# ---------------------------------------------------------------------------


class TestPassPredicateConjunction:
    def test_all_eleven_signals_true_passes(self, producer: types.ModuleType):
        evidence = _full_evidence(producer)
        status, missing = producer.evaluate_pass_predicate(evidence)
        assert status == "pass"
        assert missing == []

    def test_evidence_signal_key_count_is_eleven(self, producer: types.ModuleType):
        assert len(producer.EVIDENCE_SIGNAL_KEYS) == 11
        assert len(set(producer.EVIDENCE_SIGNAL_KEYS)) == 11

    @pytest.mark.parametrize("missing_key", [
        "actual_web_researcher_child_spawn_observed",
        "child_identity_observed",
        "child_completion_observed",
        "agy_attempt_observed",
        "deterministic_agy_failure_marker_observed",
        "native_web_tool_event_observed_after_agy_failure",
        "final_status_equals_ok",
        "final_verification_route_equals_native_web",
        "supported_claims_have_authoritative_source_evidence",
        "all_events_bound_to_same_run",
        "all_child_events_bound_to_same_child_identity",
    ])
    def test_pass_predicate_conjunction_each_missing_signal_fails(
        self, producer: types.ModuleType, missing_key: str
    ):
        evidence = _full_evidence(producer, **{missing_key: False})
        status, missing = producer.evaluate_pass_predicate(evidence)
        assert status == "fail"
        assert missing == [missing_key]

    @pytest.mark.parametrize("absent_key", [
        "actual_web_researcher_child_spawn_observed",
        "native_web_tool_event_observed_after_agy_failure",
        "all_child_events_bound_to_same_child_identity",
    ])
    def test_pass_predicate_conjunction_entirely_absent_key_fails(
        self, producer: types.ModuleType, absent_key: str
    ):
        evidence = _full_evidence(producer)
        del evidence[absent_key]
        status, missing = producer.evaluate_pass_predicate(evidence)
        assert status == "fail"
        assert missing == [absent_key]

    def test_pass_predicate_conjunction_truthy_non_bool_still_fails(
        self, producer: types.ModuleType
    ):
        # A truthy-but-not-exactly-True value (e.g. 1, "yes") must never be
        # silently accepted as satisfying a signal -- fail closed.
        evidence = _full_evidence(producer, agy_attempt_observed=1)
        status, missing = producer.evaluate_pass_predicate(evidence)
        assert status == "fail"
        assert missing == ["agy_attempt_observed"]

    def test_pass_predicate_conjunction_verification_route_alone_insufficient(
        self, producer: types.ModuleType
    ):
        # Issue #2166 In Scope: verification_route == native_web self-report
        # alone must not be sufficient for PASS -- only the independently
        # observed native web tool event signal (conjoined with all others)
        # can complete the conjunction.
        evidence = _full_evidence(
            producer,
            final_verification_route_equals_native_web=True,
            native_web_tool_event_observed_after_agy_failure=False,
        )
        status, missing = producer.evaluate_pass_predicate(evidence)
        assert status == "fail"
        assert "native_web_tool_event_observed_after_agy_failure" in missing


# ---------------------------------------------------------------------------
# AC4: causal ordering -- co-presence of all 5 timestamps is not sufficient;
# an out-of-order fixture must be detected as FAIL.
# ---------------------------------------------------------------------------


class TestCausalOrdering:
    def test_in_order_timestamps_pass(self, producer: types.ModuleType):
        timestamps = _full_causal_timestamps(producer)
        ok, reason = producer.verify_causal_ordering(timestamps)
        assert ok is True
        assert reason is None

    def test_causal_ordering_co_presence_but_reversed_fails(
        self, producer: types.ModuleType
    ):
        # All 5 keys present (co-presence satisfied) but the native Web
        # event is timestamped BEFORE the forced AGY failure marker --
        # co-presence alone must not be treated as a PASS.
        keys = producer.CAUSAL_ORDER_KEYS
        timestamps = {
            keys[0]: 1.0,  # agy_attempt_observed_at
            keys[1]: 4.0,  # deterministic_agy_failure_marker_observed_at (late)
            keys[2]: 2.0,  # native_web_tool_event_observed_after_agy_failure_at (early)
            keys[3]: 3.0,  # child_completion_observed_at
            keys[4]: 5.0,  # final_result_observed_at
        }
        ok, reason = producer.verify_causal_ordering(timestamps)
        assert ok is False
        assert reason is not None
        assert "out_of_order" in reason

    def test_causal_ordering_missing_timestamp_fails(self, producer: types.ModuleType):
        timestamps = _full_causal_timestamps(producer)
        del timestamps[producer.CAUSAL_ORDER_KEYS[2]]
        ok, reason = producer.verify_causal_ordering(timestamps)
        assert ok is False
        assert "missing_or_non_numeric_timestamp" in reason

    def test_causal_ordering_equal_timestamps_fail_strictly_increasing(
        self, producer: types.ModuleType
    ):
        keys = producer.CAUSAL_ORDER_KEYS
        timestamps = {key: 1.0 for key in keys}
        ok, reason = producer.verify_causal_ordering(timestamps)
        assert ok is False
        assert "out_of_order" in reason

    def test_causal_ordering_non_numeric_timestamp_fails(self, producer: types.ModuleType):
        timestamps = _full_causal_timestamps(producer)
        timestamps[producer.CAUSAL_ORDER_KEYS[0]] = "not-a-number"
        ok, reason = producer.verify_causal_ordering(timestamps)
        assert ok is False
        assert "missing_or_non_numeric_timestamp" in reason


# ---------------------------------------------------------------------------
# AC5: exit code contract (0=PASS/1=FAIL/77=SKIP). SKIP is never reported as
# PASS.
# ---------------------------------------------------------------------------


class TestExitCodeContract:
    def test_exit_code_contract_pass_is_zero(self, producer: types.ModuleType):
        assert producer.determine_exit_code("pass") == 0

    def test_exit_code_contract_fail_is_one(self, producer: types.ModuleType):
        assert producer.determine_exit_code("fail") == 1

    def test_exit_code_contract_skip_is_seventy_seven(self, producer: types.ModuleType):
        assert producer.determine_exit_code("skip") == 77

    def test_exit_code_contract_skip_never_equals_pass_exit_code(
        self, producer: types.ModuleType
    ):
        assert producer.determine_exit_code("skip") != producer.determine_exit_code("pass")

    def test_exit_code_contract_unrecognized_status_fails_closed(
        self, producer: types.ModuleType
    ):
        assert producer.determine_exit_code("unknown_status") == 1

    def test_exit_code_constants_match_documented_contract(self, producer: types.ModuleType):
        assert producer.EXIT_PASS == 0
        assert producer.EXIT_FAIL == 1
        assert producer.EXIT_SKIP == 77


# ---------------------------------------------------------------------------
# AC2: invocation-private AGY failure injection never touches repo-global
# .agents/hooks.json or any tracked repository file, and always cleans up.
# ---------------------------------------------------------------------------


class TestInvocationPrivateAgyFailureInjection:
    def test_inject_agy_failure_creates_and_removes_temp_dir(
        self, producer: types.ModuleType
    ):
        with producer.inject_agy_failure() as (settings_file, marker_path):
            assert settings_file.is_file()
            tmp_dir = settings_file.parent
            assert tmp_dir.is_dir()
            assert not marker_path.is_file()  # no hook has fired yet in this fixture
        assert not tmp_dir.exists()

    def test_inject_agy_failure_settings_file_never_under_repo_root(
        self, producer: types.ModuleType
    ):
        with producer.inject_agy_failure() as (settings_file, _marker_path):
            assert REPO_ROOT not in settings_file.parents

    def test_inject_agy_failure_settings_payload_never_references_agents_hooks_json(
        self, producer: types.ModuleType
    ):
        with producer.inject_agy_failure() as (settings_file, _marker_path):
            payload_text = settings_file.read_text(encoding="utf-8")
            assert ".agents/hooks.json" not in payload_text
            assert "settings.local.json" not in payload_text

    def test_inject_agy_failure_settings_payload_has_pretooluse_deny_hook(
        self, producer: types.ModuleType
    ):
        with producer.inject_agy_failure() as (settings_file, _marker_path):
            payload = json.loads(settings_file.read_text(encoding="utf-8"))
            pretool_hooks = payload["hooks"]["PreToolUse"]
            assert len(pretool_hooks) == 1
            assert pretool_hooks[0]["matcher"] == producer.AGY_WEB_TOOL_MATCHER

    def test_no_source_writes_agents_hooks_json_path_literal(self):
        # AC2 code-review-time grep proof: no code path in the producer
        # references the repo-global .agents/hooks.json path as a WRITE
        # target. (The literal string may legitimately appear only in
        # comments explaining what must never be touched.)
        source = PRODUCER_PATH.read_text(encoding="utf-8")
        write_call_lines = [
            line for line in source.splitlines()
            if (".write_text(" in line or "open(" in line) and ".agents/hooks.json" in line
        ]
        assert write_call_lines == []


# ---------------------------------------------------------------------------
# Transient-failure retry-eligibility taxonomy (Issue #2166 In Scope
# nondeterminism/retry policy).
# ---------------------------------------------------------------------------


class TestTransientFailureClassification:
    @pytest.mark.parametrize("failure_class", [
        "spawn_not_observed",
        "harness_transport_error",
    ])
    def test_transient_infrastructure_classes_are_retry_eligible(
        self, producer: types.ModuleType, failure_class: str
    ):
        assert producer.is_transient_infrastructure_failure(failure_class) is True

    @pytest.mark.parametrize("failure_class", [
        "evidence_conjunction_failed",
        "causal_order_violation",
        "gemini_invoked",
        "direct_fallback_invoked",
        None,
        "unknown_failure",
    ])
    def test_deterministic_failure_classes_are_never_retry_eligible(
        self, producer: types.ModuleType, failure_class
    ):
        assert producer.is_transient_infrastructure_failure(failure_class) is False
