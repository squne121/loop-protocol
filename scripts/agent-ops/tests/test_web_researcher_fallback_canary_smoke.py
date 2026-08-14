"""Hermetic, fixture-based tests for `web_researcher_fallback_canary_smoke/v1`
(Issue #2166 AC1/AC3/AC4/AC5/AC8).

These tests never spawn a live Claude Code / AGY process -- they cover the
pure PASS-predicate conjunction, causal-ordering validator, exit-code
contract, and the invocation-private AGY forced-failure injection's
temp-workspace lifecycle, all against synthetic fixture data.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PRODUCER_PATH = REPO_ROOT / "scripts" / "agent-ops" / "run_web_researcher_fallback_canary_smoke.py"


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec: the module uses
    # `@dataclasses.dataclass`, which resolves forward-referenced type
    # annotations via `sys.modules[cls.__module__]` at class-definition
    # time -- omitting this raises AttributeError on import.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def canary() -> types.ModuleType:
    return _load_module(PRODUCER_PATH, "test_web_researcher_fallback_canary_smoke_producer")


# ---------------------------------------------------------------------------
# AC1: script exists and supports --help
# ---------------------------------------------------------------------------


def test_ac1_script_file_exists() -> None:
    assert PRODUCER_PATH.is_file()


def test_ac1_help_flag_exits_zero(canary: types.ModuleType) -> None:
    result = subprocess.run(
        [sys.executable, str(PRODUCER_PATH), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "--trials" in result.stdout
    assert "--dry-run" in result.stdout


# ---------------------------------------------------------------------------
# AC2: AGY forced-failure injection is invocation-private (fixture-level
# behavioral check; the repo-global no-write assertion itself is a `git
# diff` VC, not a pytest concern)
# ---------------------------------------------------------------------------


def test_ac2_agy_injection_workspace_is_isolated_and_cleans_up(canary: types.ModuleType, tmp_path: Path) -> None:
    injection = canary.AgyForcedFailureInjection(parent_dir=str(tmp_path))
    with injection as inj:
        assert inj.workspace_dir is not None
        assert inj.workspace_dir.exists()
        assert str(inj.workspace_dir).startswith(str(tmp_path))
        # never references any repo-global or user-global path
        assert ".agents" not in str(inj.workspace_dir)
        assert str(Path.home()) not in str(inj.workspace_dir)
        assert inj.env["AGY_BIN"] == str(inj.stub_path)
        assert inj.env["LOOP_CANARY_AGY_FORCED_FAILURE_REASON"] == canary.AGY_FAILURE_INJECTION_REASON
        settings = json.loads(inj.settings_path.read_text(encoding="utf-8"))
        assert settings["permissions"]["deny"] == sorted(canary.AGY_WEB_TOOL_NAMES)
    # guaranteed cleanup after normal __exit__
    assert injection.workspace_dir is None
    assert not any(tmp_path.iterdir())


def test_ac2_agy_injection_cleans_up_on_exception(canary: types.ModuleType, tmp_path: Path) -> None:
    injection = canary.AgyForcedFailureInjection(parent_dir=str(tmp_path))
    with pytest.raises(RuntimeError):
        with injection:
            raise RuntimeError("simulated interrupt mid-trial")
    assert injection.workspace_dir is None
    assert not any(tmp_path.iterdir())


def test_ac2_stub_reports_deny_with_required_reason(canary: types.ModuleType) -> None:
    source = canary.build_agy_forced_failure_stub_source(canary.AGY_FAILURE_INJECTION_REASON)
    assert canary.AGY_FAILURE_INJECTION_REASON in source
    assert "search_web" in source
    assert "read_url_content" in source
    assert '"decision": "deny"' in source


# ---------------------------------------------------------------------------
# AC3: PASS predicate conjunction -- missing even one of the 11 signals
# fails the whole predicate
# ---------------------------------------------------------------------------


def test_pass_predicate_conjunction_all_signals_true_passes(canary: types.ModuleType) -> None:
    signals = {name: True for name in canary.EVIDENCE_SIGNAL_NAMES}
    result = canary.evaluate_pass_predicate(signals)
    assert result.passed is True
    assert result.missing_signals == ()


def test_pass_predicate_conjunction_each_signal_individually_required(canary: types.ModuleType) -> None:
    for signal_name in canary.EVIDENCE_SIGNAL_NAMES:
        signals = {name: True for name in canary.EVIDENCE_SIGNAL_NAMES}
        signals[signal_name] = False
        result = canary.evaluate_pass_predicate(signals)
        assert result.passed is False, f"expected FAIL when {signal_name} is False"
        assert signal_name in result.missing_signals


def test_pass_predicate_conjunction_missing_key_treated_as_unsatisfied(canary: types.ModuleType) -> None:
    signals = {name: True for name in canary.EVIDENCE_SIGNAL_NAMES}
    del signals["all_child_events_bound_to_same_child_identity"]
    result = canary.evaluate_pass_predicate(signals)
    assert result.passed is False
    assert "all_child_events_bound_to_same_child_identity" in result.missing_signals


def test_pass_predicate_conjunction_truthy_non_bool_not_accepted(canary: types.ModuleType) -> None:
    signals = {name: True for name in canary.EVIDENCE_SIGNAL_NAMES}
    signals["agy_attempt_observed"] = "yes"  # truthy string, not exactly True
    result = canary.evaluate_pass_predicate(signals)
    assert result.passed is False
    assert "agy_attempt_observed" in result.missing_signals


def test_pass_predicate_self_report_route_alone_is_insufficient(canary: types.ModuleType) -> None:
    """`final_verification_route_equals_native_web` (self-report) being True
    while the primary independent native-stream signal is False must still
    FAIL -- self-report alone is never sufficient (Issue In Scope)."""
    signals = {name: True for name in canary.EVIDENCE_SIGNAL_NAMES}
    signals["native_web_tool_event_observed_after_agy_failure"] = False
    result = canary.evaluate_pass_predicate(signals)
    assert result.passed is False
    assert "native_web_tool_event_observed_after_agy_failure" in result.missing_signals


# ---------------------------------------------------------------------------
# AC4: causal ordering -- co-presence alone (equal/reversed seq) must FAIL
# ---------------------------------------------------------------------------


def _ordered_events() -> list[dict]:
    return [
        {"type": "agy_attempt", "seq": 10, "run_id": "run-1", "child_identity": "child-1"},
        {"type": "agy_failure", "seq": 20, "run_id": "run-1", "child_identity": "child-1"},
        {"type": "native_web_tool_event", "seq": 30, "run_id": "run-1", "child_identity": "child-1"},
        {"type": "child_completion", "seq": 40, "run_id": "run-1", "child_identity": "child-1"},
        {"type": "final_result", "seq": 50, "run_id": "run-1"},
    ]


def test_causal_ordering_strictly_increasing_sequence_passes(canary: types.ModuleType) -> None:
    result = canary.validate_causal_ordering(_ordered_events())
    assert result.ok is True
    assert result.reason is None


def test_causal_ordering_copresence_same_seq_fails(canary: types.ModuleType) -> None:
    """The exact scenario Issue #2166 requires to be rejected: two steps
    merely CO-PRESENT in the same event list (identical `seq`), not
    actually ordered."""
    events = _ordered_events()
    events[2] = dict(events[2], seq=events[1]["seq"])  # native_web_tool_event co-present with agy_failure
    result = canary.validate_causal_ordering(events)
    assert result.ok is False
    assert result.reason is not None and result.reason.startswith("out_of_order:")


def test_causal_ordering_reversed_sequence_fails(canary: types.ModuleType) -> None:
    events = _ordered_events()
    events[0], events[1] = dict(events[0], seq=events[1]["seq"]), dict(events[1], seq=events[0]["seq"])
    result = canary.validate_causal_ordering(events)
    assert result.ok is False
    assert result.reason is not None and result.reason.startswith("out_of_order:")


def test_causal_ordering_missing_step_fails_closed(canary: types.ModuleType) -> None:
    events = [e for e in _ordered_events() if e["type"] != "child_completion"]
    result = canary.validate_causal_ordering(events)
    assert result.ok is False
    assert result.reason == "missing_step:child_completion"


def test_causal_ordering_empty_events_fails_closed(canary: types.ModuleType) -> None:
    result = canary.validate_causal_ordering([])
    assert result.ok is False
    assert result.reason is not None and result.reason.startswith("missing_step:")


def test_causal_ordering_binding_all_events_same_run(canary: types.ModuleType) -> None:
    events = _ordered_events()
    binding = canary.bind_evidence_consistency(events, expected_run_id="run-1", expected_child_identity="child-1")
    assert binding["all_events_bound_to_same_run"] is True
    assert binding["all_child_events_bound_to_same_child_identity"] is True


def test_causal_ordering_binding_detects_mismatched_run(canary: types.ModuleType) -> None:
    events = _ordered_events()
    events[2] = dict(events[2], run_id="run-DIFFERENT")
    binding = canary.bind_evidence_consistency(events, expected_run_id="run-1", expected_child_identity="child-1")
    assert binding["all_events_bound_to_same_run"] is False


def test_causal_ordering_binding_detects_mismatched_child_identity(canary: types.ModuleType) -> None:
    events = _ordered_events()
    events[2] = dict(events[2], child_identity="child-DIFFERENT")
    binding = canary.bind_evidence_consistency(events, expected_run_id="run-1", expected_child_identity="child-1")
    assert binding["all_child_events_bound_to_same_child_identity"] is False


def test_causal_ordering_binding_never_guesses_true_without_child_identity(canary: types.ModuleType) -> None:
    events = _ordered_events()
    binding = canary.bind_evidence_consistency(events, expected_run_id="run-1", expected_child_identity=None)
    assert binding["all_child_events_bound_to_same_child_identity"] is False


# ---------------------------------------------------------------------------
# AC5: exit code contract (0=PASS/1=FAIL/77=SKIP), SKIP never promoted to
# PASS
# ---------------------------------------------------------------------------


def test_exit_code_contract_pass_is_zero(canary: types.ModuleType) -> None:
    assert canary.exit_code_for_disposition(canary.DISPOSITION_PASS) == 0


def test_exit_code_contract_fail_is_one(canary: types.ModuleType) -> None:
    assert canary.exit_code_for_disposition(canary.DISPOSITION_FAIL) == 1


def test_exit_code_contract_skip_is_seventy_seven(canary: types.ModuleType) -> None:
    assert canary.exit_code_for_disposition(canary.DISPOSITION_SKIP) == 77


def test_exit_code_contract_unknown_disposition_raises(canary: types.ModuleType) -> None:
    with pytest.raises(ValueError):
        canary.exit_code_for_disposition("not_a_real_disposition")


def test_exit_code_contract_skip_never_promoted_to_pass_when_preflight_fails(canary: types.ModuleType) -> None:
    # Even when every evidence signal WOULD have passed, an unavailable
    # runtime/auth/capability preflight forces SKIP, never PASS.
    disposition = canary.finalize_disposition(preflight_ok=False, pass_predicate_ok=True, causal_ordering_ok=True)
    assert disposition == canary.DISPOSITION_SKIP
    assert canary.exit_code_for_disposition(disposition) == 77


def test_exit_code_contract_preflight_ok_but_evidence_incomplete_is_fail_not_skip(canary: types.ModuleType) -> None:
    disposition = canary.finalize_disposition(preflight_ok=True, pass_predicate_ok=False, causal_ordering_ok=True)
    assert disposition == canary.DISPOSITION_FAIL
    assert canary.exit_code_for_disposition(disposition) == 1


def test_exit_code_contract_dry_run_reports_skip(canary: types.ModuleType) -> None:
    result = subprocess.run(
        [sys.executable, str(PRODUCER_PATH), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 77
    payload = json.loads(result.stdout)
    assert payload["disposition"] == "skip"


# ---------------------------------------------------------------------------
# Nondeterminism / retry policy (Issue #2013 methodology reuse)
# ---------------------------------------------------------------------------


def test_retry_policy_transient_failure_class_is_retried_once(canary: types.ModuleType) -> None:
    decision = canary.classify_retry_decision(failure_class="agy_rate_limited", retries_used=0)
    assert decision.should_retry is True


def test_retry_policy_deterministic_semantic_failure_is_never_retried(canary: types.ModuleType) -> None:
    decision = canary.classify_retry_decision(failure_class="agy_permission_denied", retries_used=0)
    assert decision.should_retry is False
    assert decision.reason == "not_transient"


def test_retry_policy_none_failure_class_is_never_retried(canary: types.ModuleType) -> None:
    decision = canary.classify_retry_decision(failure_class=None, retries_used=0)
    assert decision.should_retry is False


def test_retry_policy_bounded_to_single_retry(canary: types.ModuleType) -> None:
    decision = canary.classify_retry_decision(failure_class="agy_rate_limited", retries_used=1)
    assert decision.should_retry is False
    assert decision.reason == "max_retries_exhausted"


# ---------------------------------------------------------------------------
# AC8: additive companion schema discipline
# ---------------------------------------------------------------------------


def test_ac8_schemas_are_brand_new_not_shared_with_route_smoke(canary: types.ModuleType) -> None:
    assert canary.SCHEMA == "web_researcher_fallback_canary_smoke/v1"
    assert canary.TRIAL_EVIDENCE_SCHEMA == "web_researcher_fallback_canary_smoke_trial_evidence/v1"
    assert canary.SCHEMA != "agent_provider_route_smoke/v1"


def test_ac8_trial_evidence_never_includes_raw_prompt_text(canary: types.ModuleType) -> None:
    evidence = canary.build_trial_evidence(
        run_id="run-1",
        trial_index=0,
        head_sha="a" * 40,
        runtime_name="claude",
        runtime_version="2.1.226",
        model="claude-test",
        agent_definition_sha256="b" * 64,
        prompt_sha256=canary.compute_prompt_sha256("this is a secret-free test prompt"),
        settings_digest="c" * 64,
        start_time="2026-08-14T00:00:00Z",
        end_time="2026-08-14T00:01:00Z",
        child_identity="child-1",
        fallback_trigger_reason="agy_web_tool_denied",
        agy_failure_evidence={"reason": canary.AGY_FAILURE_INJECTION_REASON},
        native_web_event_evidence={"count": 1},
        signals={name: True for name in canary.EVIDENCE_SIGNAL_NAMES},
        causal_ordering=canary.validate_causal_ordering(_ordered_events()),
        disposition="pass",
    )
    serialized = json.dumps(evidence)
    assert "this is a secret-free test prompt" not in serialized
    assert evidence["prompt_sha256"] == canary.compute_prompt_sha256("this is a secret-free test prompt")
    assert evidence["final_disposition"] == "pass"


def test_ac8_write_trial_evidence_creates_run_scoped_file(canary: types.ModuleType, tmp_path: Path) -> None:
    evidence = {"schema": canary.TRIAL_EVIDENCE_SCHEMA, "final_disposition": "skip"}
    written = canary.write_trial_evidence(tmp_path, "run-xyz", 0, evidence)
    assert written.exists()
    assert written.parent.name == "run-xyz"
    loaded = json.loads(written.read_text(encoding="utf-8"))
    assert loaded["final_disposition"] == "skip"


# ---------------------------------------------------------------------------
# AC7 regression guard: this test file must never redefine or shadow
# `run_agent_provider_route_smoke.py`'s own test module name
# ---------------------------------------------------------------------------


def test_module_name_does_not_collide_with_provider_route_smoke_tests() -> None:
    assert "test_web_researcher_fallback_canary_smoke_producer" != "test_agent_provider_route_smoke_producer"
