#!/usr/bin/env python3
"""Unit tests for run_retrospective.py's deterministic enrichment phase
(Issue #2362, Scope Reframe 2026-08-28 owner-approved): the
retrospective-evaluator Agent is redesigned as a JUDGMENT-ONLY producer --
its wire output for each candidate record is now a flat, tightly-schema-
constrained shape (`candidate_id`/`title`/`description`/`claim_class`/
`subject_ref`/`rule_id`/`evidence_refs`) that no longer accepts `identity`/
`evaluations`/`repository_id`/`source_run_ref`/`created_at`/`updated_at` at
all. `run_evaluation()`'s `_enrich_candidate_record()` builds the ENTIRE
canonical `agent_improvement_candidate/v1` record from that judgment plus
100% Python-side deterministic sources: `compute_finding_identity()`
(identity), `compute_delta()`/`PreviousStateProvider` (evaluations[]
history), and real `finding_sets` data (`evidence_refs[].projection_digest`
recomputation).

This supersedes the PR #2367 fix_delta items 1-6 design (where the
evaluator still nested a fully-shaped `finding_contract.identity.key`/
`finding_contract.evaluations[]` that enrichment merely overwrote/passed
through) -- see this Issue's "Scope Reframe" section for the human-approved
rationale (OWNER review on PR #2367 + fix_delta blocked report proved that
design could never let AC3/AC5 pass against the frozen, unconstrained wire
schema without either fabricating data or the evaluator producing
incompatible vocabulary).

Runtime Verification Applicability: not_applicable for this file (a pure
fixture/subprocess-mock harness with no real Agent CLI invocation, mirroring
`test_run_retrospective.py`'s own "deferred" classification). AC5's runtime
verification is a separate module, `test_run_evaluation_enrichment_live_cli.py`
(marked `claude_live`, invoked only via
`verify_run_evaluation_enrichment_live_cli.sh`).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402

_validate_mod = rr._validate_retrospective_schema_module()

_FULL_SHA = "a" * 40
_DIGEST = "d" * 64
#: matches _identity_key()'s hardcoded repository_id in test_run_retrospective.py
_REPOSITORY_ID = "squne121/loop-protocol"

_SCHEMA_DIR = _SCRIPTS_DIR / "schemas"


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


def _judgment_candidate(
    *,
    candidate_id: str = "cand-judgment-0001",
    title: str = "judgment title",
    description: str = "judgment description",
    claim_class: str = "runtime_behavior",
    subject_ref: dict[str, Any] | None = None,
    rule_id: str = "runtime_behavior.example_rule",
    evidence_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A raw (NOT yet enriched) judgment-only candidate record dict, shaped
    exactly like a real judgment-only-constrained evaluator's
    `EVALUATION_RESULT_V1` `candidate_records[]` entry (Issue #2362 Scope
    Reframe) -- flat fields only, no `finding_contract`/`identity`/
    `evaluations` nesting (the tightened wire schema does not even accept
    those from the evaluator any more)."""
    return {
        "candidate_id": candidate_id,
        "title": title,
        "description": description,
        "claim_class": claim_class,
        "subject_ref": subject_ref or {"kind": "repository_path", "value": "schemas/x.json"},
        "rule_id": rule_id,
        "evidence_refs": evidence_refs if evidence_refs is not None else [],
    }


def _raw_evaluation_payload(
    *,
    run_id: str,
    base_sha: str,
    source_set_digest: str,
    candidate_records: list[dict[str, Any]],
    evidence_ref: str = "evidence://test/evaluation",
) -> dict[str, Any]:
    return {
        "schema_version": rr.WIRE_SCHEMA_EVALUATION,
        "run_id": run_id,
        "base_sha": base_sha,
        "source_set_digest": source_set_digest,
        "candidate_records": candidate_records,
        "evidence_ref": evidence_ref,
    }


def _ok_agent_result(payload: dict[str, Any]) -> rr.AgentInvocationResult:
    return rr.AgentInvocationResult(
        status="ok", structured_output=payload, raw_stdout_excerpt=None, exit_code=0, reason_code=None
    )


def _make_ctx(run_id: str = "run-enrichment-1") -> rr.RunContext:
    return rr.RunContext(base_sha_resolver=lambda: _FULL_SHA, run_id=run_id)


def _make_evaluator_request(
    ctx: rr.RunContext, *, source_set_digest: str = _DIGEST, finding_sets: list[dict[str, Any]] | None = None
) -> rr.EvaluatorRequest:
    return rr.EvaluatorRequest(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=source_set_digest,
        finding_sets=finding_sets if finding_sets is not None else [],
    )


#: a single-observer finding_sets fixture (Issue #2362 Scope Reframe) --
#: real evidence data `_enrich_evidence_ref` can recompute a genuine
#: `projection_digest` from, for a `runtime_receipt`/`runtime` evidence_ref.
_RUNTIME_FINDING_SETS = [
    {
        "observer_id": "retrospective-runtime-observer",
        "findings": [{"claim": "observed something concrete", "claim_class": "runtime_behavior"}],
    }
]

_RUNTIME_EVIDENCE_REF_JUDGMENT = {
    "ref_type": "runtime_receipt",
    "source_id": "runtime",
    "resource_identity": "observer:retrospective-runtime-observer",
}

_EMPTY_PREVIOUS_STATE = rr.PreviousStateResult(
    status="no_history", previous_run_ref=None, candidates=[], read_version=None
)


def _run_evaluation(
    *,
    ctx: rr.RunContext,
    evaluator_request: rr.EvaluatorRequest,
    raw_payload: dict[str, Any],
    repository_id: str = _REPOSITORY_ID,
    previous_state: rr.PreviousStateResult = _EMPTY_PREVIOUS_STATE,
    repair: Any = None,
) -> rr.Evaluation:
    def _invoke(_request: rr.EvaluatorRequest) -> rr.AgentInvocationResult:
        return _ok_agent_result(raw_payload)

    return rr.run_evaluation(
        ctx,
        evaluator_request,
        invoke_evaluator=_invoke,
        repository_id=repository_id,
        previous_state=previous_state,
        repair=repair,
    )


# ---------------------------------------------------------------------------
# AC1: judgment-only wire schema shape
# ---------------------------------------------------------------------------


def _load_wire_schema() -> dict[str, Any]:
    return json.loads((_SCHEMA_DIR / "evaluation_result_v1.schema.json").read_text(encoding="utf-8"))


def test_wire_schema_is_valid_draft7_and_judgment_only_shape() -> None:
    """AC1: the wire schema is valid draft-07 and its `candidate_records[]`
    item shape requires ONLY the evaluator-authoritative judgment fields."""
    schema = _load_wire_schema()
    jsonschema.Draft7Validator.check_schema(schema)
    assert schema["$schema"].startswith("http://json-schema.org/draft-07/schema")
    candidate_schema = schema["definitions"]["candidate_judgment"]
    assert candidate_schema["additionalProperties"] is False
    assert set(candidate_schema["required"]) == {
        "candidate_id",
        "title",
        "description",
        "claim_class",
        "subject_ref",
        "rule_id",
        "evidence_refs",
    }
    assert set(candidate_schema["properties"].keys()) == set(candidate_schema["required"])


def test_wire_schema_judgment_only_rejects_identity_and_evaluations() -> None:
    """AC1: `identity`/`evaluations`/`repository_id`/`source_run_ref`/
    `created_at`/`updated_at` are never accepted from the evaluator."""
    schema = _load_wire_schema()
    validator = jsonschema.Draft7Validator(schema)
    candidate = _judgment_candidate()
    payload = _raw_evaluation_payload(
        run_id="r", base_sha=_FULL_SHA, source_set_digest=_DIGEST, candidate_records=[candidate]
    )
    validator.validate(payload)  # must not raise: a genuine judgment-only record is valid

    for forbidden_field, forbidden_value in (
        ("identity", {"algorithm": "sha256-jcs-v1", "key": {}, "value": "sha256:" + "0" * 64}),
        ("evaluations", [{"...": "..."}]),
        ("repository_id", "attacker/repo"),
        ("source_run_ref", {"base_sha": _FULL_SHA, "source_set_digest": _DIGEST}),
        ("created_at", "2000-01-01T00:00:00Z"),
        ("updated_at", "2000-01-01T00:00:00Z"),
    ):
        bad_candidate = dict(candidate)
        bad_candidate[forbidden_field] = forbidden_value
        bad_payload = _raw_evaluation_payload(
            run_id="r", base_sha=_FULL_SHA, source_set_digest=_DIGEST, candidate_records=[bad_candidate]
        )
        with pytest.raises(jsonschema.exceptions.ValidationError):
            validator.validate(bad_payload)


def test_wire_schema_judgment_only_rejects_projection_digest_from_evaluator() -> None:
    """AC1: `evidence_refs[].projection_digest` is never accepted from the
    evaluator (Python always recomputes it from real evidence data)."""
    schema = _load_wire_schema()
    validator = jsonschema.Draft7Validator(schema)
    candidate = _judgment_candidate(
        evidence_refs=[{**_RUNTIME_EVIDENCE_REF_JUDGMENT, "projection_digest": "sha256:" + "0" * 64}]
    )
    payload = _raw_evaluation_payload(
        run_id="r", base_sha=_FULL_SHA, source_set_digest=_DIGEST, candidate_records=[candidate]
    )
    with pytest.raises(jsonschema.exceptions.ValidationError):
        validator.validate(payload)


def test_wire_schema_claim_class_enum_matches_canonical() -> None:
    """AC1: the wire schema's `claim_class` enum stays in sync with the
    canonical schema's 9-value closed enum."""
    schema = _load_wire_schema()
    canonical_schema = json.loads(
        (_SCRIPTS_DIR.parent / "schemas" / "agent_improvement_candidate_v1.schema.json").read_text(encoding="utf-8")
    )
    assert set(schema["definitions"]["claim_class"]["enum"]) == set(
        canonical_schema["$defs"]["claim_class"]["enum"]
    )


def test_wire_schema_subject_ref_kind_enum_matches_canonical() -> None:
    """AC1: the wire schema's `subject_ref.kind` enum stays in sync with
    the canonical schema."""
    schema = _load_wire_schema()
    canonical_schema = json.loads(
        (_SCRIPTS_DIR.parent / "schemas" / "agent_improvement_candidate_v1.schema.json").read_text(encoding="utf-8")
    )
    assert set(schema["definitions"]["subject_ref"]["properties"]["kind"]["enum"]) == set(
        canonical_schema["$defs"]["subject_ref"]["properties"]["kind"]["enum"]
    )


# ---------------------------------------------------------------------------
# AC2: evaluator prompt has no identity/evaluations placeholder
# ---------------------------------------------------------------------------


def _evaluator_prompt_text() -> str:
    agent_md = _SCRIPTS_DIR.parents[3] / ".claude" / "agents" / "retrospective-evaluator.md"
    return agent_md.read_text(encoding="utf-8")


def test_evaluator_prompt_contract_no_identity_or_evaluations_placeholder() -> None:
    """AC2: the evaluator prompt's output example no longer instructs the
    model to produce `identity`/`evaluations` placeholder JSON."""
    text = _evaluator_prompt_text()
    assert '"identity":' not in text
    assert '"evaluations":' not in text
    assert '{"...": "..."}' not in text
    assert "canonical agent_improvement_candidate/v1 evaluation entry" not in text


def test_evaluator_prompt_contract_documents_judgment_only_fields() -> None:
    """AC2: the evaluator prompt documents the judgment-only fields with
    concrete (non-placeholder) examples."""
    text = _evaluator_prompt_text()
    assert '"candidate_id"' in text
    assert '"subject_ref"' in text
    assert '"rule_id"' in text
    assert '"evidence_refs"' in text
    assert "tools: []" in text  # frontmatter permission unchanged


# ---------------------------------------------------------------------------
# AC3: identity/evaluations[] construction is 100% Python-side
# ---------------------------------------------------------------------------


def test_enrichment_precedes_validation() -> None:
    """`run_evaluation()`'s outer-envelope-parse phase (step 1) sees the
    RAW judgment-only payload; canonical candidate validation (step 4)
    only fires AFTER the deterministic-enrichment phase has built the
    ENTIRE canonical shape."""
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx, finding_sets=_RUNTIME_FINDING_SETS)
    subject_ref = {"kind": "repository_path", "value": "schemas/order.json"}
    raw_candidate = _judgment_candidate(
        candidate_id="cand-order",
        subject_ref=subject_ref,
        rule_id="runtime_behavior.order_rule",
        evidence_refs=[_RUNTIME_EVIDENCE_REF_JUDGMENT],
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[raw_candidate],
    )

    evaluation = _run_evaluation(ctx=ctx, evaluator_request=evaluator_request, raw_payload=raw_payload)

    record = evaluation.candidate_records[0]
    assert record["candidate_id"] == "cand-order"
    assert record["candidate_status"] == "proposed"
    assert record["finding_contract"]["identity"]["key"] == {
        "repository_id": _REPOSITORY_ID,
        "claim_class": "runtime_behavior",
        "subject_ref": subject_ref,
        "rule_id": "runtime_behavior.order_rule",
    }
    # must not raise -- independent re-check outside Evaluation.__post_init__
    _validate_mod.validate_candidate(record)


def test_compute_finding_identity_reuse() -> None:
    """`identity.value` is computed via `compute_finding_identity()` (the
    canonical SSOT), never a reimplementation."""
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx, finding_sets=_RUNTIME_FINDING_SETS)
    subject_ref = {"kind": "repository_path", "value": "schemas/reuse.json"}
    raw_candidate = _judgment_candidate(
        candidate_id="cand-reuse",
        subject_ref=subject_ref,
        rule_id="runtime_behavior.reuse_rule",
        evidence_refs=[_RUNTIME_EVIDENCE_REF_JUDGMENT],
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[raw_candidate],
    )

    evaluation = _run_evaluation(ctx=ctx, evaluator_request=evaluator_request, raw_payload=raw_payload)
    identity = evaluation.candidate_records[0]["finding_contract"]["identity"]

    expected_key = {
        "repository_id": _REPOSITORY_ID,
        "claim_class": "runtime_behavior",
        "subject_ref": subject_ref,
        "rule_id": "runtime_behavior.reuse_rule",
    }
    assert identity["key"] == expected_key
    assert identity["algorithm"] == _validate_mod.FINDING_IDENTITY_ALGORITHM
    assert identity["value"] == _validate_mod.compute_finding_identity(expected_key)


def test_evaluations_history_never_parsed_from_evaluator_wire_payload() -> None:
    """AC3: even if a (malicious/confused) evaluator smuggles an
    `evaluations`-like key alongside the judgment-only fields, the wire
    schema itself would reject it in production -- but AT THE PYTHON LEVEL
    (this test exercises `_enrich_candidate_record` directly, bypassing the
    wire schema, to prove the ENGINE never reads such a key even if present
    in the dict) the constructed `evaluations[]` is always exactly the
    Python-computed history, never influenced by any evaluator-supplied
    `evaluations`/`finding_contract` key."""
    raw_candidate = _judgment_candidate(
        candidate_id="cand-smuggled-history",
        rule_id="runtime_behavior.smuggled_rule",
        evidence_refs=[_RUNTIME_EVIDENCE_REF_JUDGMENT],
    )
    # smuggle a finding_contract/evaluations key that, if ever read, would
    # poison the result with an attacker-controlled classification.
    raw_candidate["finding_contract"] = {
        "schema_version": "v1",
        "identity": {"algorithm": "sha256-jcs-v1", "key": {}, "value": "sha256:" + "f" * 64},
        "claim_class": "runtime_behavior",
        "evaluations": [{"evaluation_status": "classified", "delta_status": "resolved", "presence_delta": "resolved"}],
    }

    enriched = rr._enrich_candidate_record(
        raw_candidate,
        repository_id=_REPOSITORY_ID,
        base_sha=_FULL_SHA,
        source_set_digest=_DIGEST,
        timestamp="2026-01-01T00:00:00Z",
        previous_state=_EMPTY_PREVIOUS_STATE,
        real_evidence_index=rr._observer_source_type_index(_RUNTIME_FINDING_SETS),
    )
    evaluations = enriched["finding_contract"]["evaluations"]
    assert len(evaluations) == 1
    assert evaluations[0]["evaluation_status"] == "classified"
    assert evaluations[0]["delta_status"] == "new"  # Python-computed (no_history), never "resolved"
    assert evaluations[0]["presence_delta"] == "new"
    # the smuggled identity.value never survives either
    assert enriched["finding_contract"]["identity"]["value"] != "sha256:" + "f" * 64


def test_classified_new_evaluation_entry_constructed_from_compute_delta() -> None:
    """AC3: a genuinely-new finding (no previous history) gets a Python-
    constructed `classified`/`new` evaluations[] entry -- never fabricated,
    never parsed from evaluator output (which supplies none)."""
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx, finding_sets=_RUNTIME_FINDING_SETS)
    raw_candidate = _judgment_candidate(
        candidate_id="cand-new-entry",
        rule_id="runtime_behavior.new_entry_rule",
        evidence_refs=[_RUNTIME_EVIDENCE_REF_JUDGMENT],
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[raw_candidate],
    )

    evaluation = _run_evaluation(ctx=ctx, evaluator_request=evaluator_request, raw_payload=raw_payload)
    evaluations = evaluation.candidate_records[0]["finding_contract"]["evaluations"]
    assert len(evaluations) == 1
    entry = evaluations[0]
    assert entry["evaluation_status"] == "classified"
    assert entry["delta_status"] == "new"
    assert entry["presence_delta"] == "new"
    assert entry["previous_evaluation_ref"] is None
    assert entry["baseline_signal"] is None
    assert entry["current_signal"] is not None
    assert entry["evidence_refs"]
    assert entry["evidence_refs"][0]["ref_type"] == "runtime_receipt"
    assert entry["evidence_refs"][0]["projection_digest"].startswith("sha256:")
    _validate_mod.validate_candidate(evaluation.candidate_records[0])


def test_indeterminate_evaluation_entry_omits_delta_status() -> None:
    """AC3: when `PreviousStateResult.status` is `"partial"`, the
    Python-constructed entry is `indeterminate` and OMITS `delta_status`
    entirely (the canonical schema forbids the key when indeterminate)."""
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx, finding_sets=_RUNTIME_FINDING_SETS)
    raw_candidate = _judgment_candidate(
        candidate_id="cand-indeterminate-entry",
        rule_id="runtime_behavior.indeterminate_entry_rule",
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[raw_candidate],
    )
    previous_state = rr.PreviousStateResult(
        status="partial", previous_run_ref="run-0", candidates=[], read_version="v1"
    )

    evaluation = _run_evaluation(
        ctx=ctx, evaluator_request=evaluator_request, raw_payload=raw_payload, previous_state=previous_state
    )
    entry = evaluation.candidate_records[0]["finding_contract"]["evaluations"][0]
    assert entry["evaluation_status"] == "indeterminate"
    assert "delta_status" not in entry
    assert entry["indeterminate_reason"] == "source_partial"
    assert entry["baseline_signal"] is None
    assert entry["current_signal"] is None
    _validate_mod.validate_candidate(evaluation.candidate_records[0])


def test_multi_entry_evaluation_history_chain_independent_of_evaluator() -> None:
    """AC3: when the SAME finding identity was previously classified
    (available in `PreviousStateResult.candidates`), the new run's entry is
    APPENDED to that history (never replacing it), and `previous_evaluation_ref`
    correctly chains to the prior entry's `evaluation_id` -- all independent
    of whatever the evaluator's own (nonexistent) `evaluations[]` output
    would have said."""
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx, finding_sets=_RUNTIME_FINDING_SETS)
    subject_ref = {"kind": "repository_path", "value": "schemas/history.json"}
    rule_id = "runtime_behavior.history_rule"
    key = {
        "repository_id": _REPOSITORY_ID,
        "claim_class": "runtime_behavior",
        "subject_ref": subject_ref,
        "rule_id": rule_id,
    }
    identity_value = _validate_mod.compute_finding_identity(key)
    prior_entry = {
        "evaluation_id": "sha256:" + "1" * 64,
        "evaluated_run_ref": {"base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        "previous_evaluation_ref": None,
        "observed": True,
        "source_coverage": "complete",
        "evaluation_status": "classified",
        "presence_delta": "new",
        "signal_delta": "unknown",
        "delta_status": "new",
        "indeterminate_reason": None,
        "baseline_signal": None,
        "current_signal": {
            "signal_type": "boolean",
            "value": True,
            "comparator": "eq",
            "worse_direction": "not_applicable",
        },
        "expected_signal": None,
        "evidence_refs": [
            {
                "ref_type": "runtime_receipt",
                "source_id": "runtime",
                "resource_identity": "observer:retrospective-runtime-observer",
                "projection_digest": "sha256:" + "2" * 64,
            }
        ],
        "classified_at": "2026-01-01T00:00:00Z",
        "classifier_version": "run_retrospective/v1",
    }
    prior_candidate = {
        "candidate_id": "cand-history-prior",
        "candidate_status": "proposed",
        "title": "prior",
        "description": "prior",
        "source_run_ref": {"base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "finding_contract": {
            "schema_version": "v1",
            "identity": {"algorithm": "sha256-jcs-v1", "key": key, "value": identity_value},
            "claim_class": "runtime_behavior",
            "evaluations": [prior_entry],
        },
    }
    previous_state = rr.PreviousStateResult(
        status="available", previous_run_ref="run-0", candidates=[prior_candidate], read_version="v1"
    )

    raw_candidate = _judgment_candidate(
        candidate_id="cand-history-current",
        subject_ref=subject_ref,
        rule_id=rule_id,
        evidence_refs=[_RUNTIME_EVIDENCE_REF_JUDGMENT],
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[raw_candidate],
    )

    evaluation = _run_evaluation(
        ctx=ctx, evaluator_request=evaluator_request, raw_payload=raw_payload, previous_state=previous_state
    )
    evaluations = evaluation.candidate_records[0]["finding_contract"]["evaluations"]
    assert len(evaluations) == 2
    assert evaluations[0] == prior_entry  # history preserved byte-for-byte
    new_entry = evaluations[1]
    assert new_entry["previous_evaluation_ref"] == prior_entry["evaluation_id"]
    assert new_entry["evaluation_status"] == "classified"
    assert new_entry["delta_status"] == "unchanged"
    assert new_entry["presence_delta"] == "active"
    _validate_mod.validate_candidate(evaluation.candidate_records[0])


def test_recurrent_evaluation_after_previously_resolved() -> None:
    """AC3: a finding whose previous last evaluation was `resolved`,
    re-reported this run, is classified `recurrent` -- via the same
    `compute_delta()` algorithm the `PublishRequest.delta_results` sidecar
    uses, not a separate/duplicated classification."""
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx, finding_sets=_RUNTIME_FINDING_SETS)
    resolved_fixture = _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.resolved.valid.json")
    key = resolved_fixture["finding_contract"]["identity"]["key"]
    previous_state = rr.PreviousStateResult(
        status="available", previous_run_ref="run-0", candidates=[resolved_fixture], read_version="v1"
    )
    raw_candidate = _judgment_candidate(
        candidate_id="cand-recurrent",
        subject_ref=key["subject_ref"],
        claim_class=key["claim_class"],
        rule_id=key["rule_id"],
        evidence_refs=[_RUNTIME_EVIDENCE_REF_JUDGMENT],
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[raw_candidate],
    )

    evaluation = _run_evaluation(
        ctx=ctx, evaluator_request=evaluator_request, raw_payload=raw_payload, previous_state=previous_state
    )
    evaluations = evaluation.candidate_records[0]["finding_contract"]["evaluations"]
    assert len(evaluations) == 3  # 2 prior + 1 new
    new_entry = evaluations[-1]
    assert new_entry["delta_status"] == "recurrent"
    assert new_entry["presence_delta"] == "recurrent"
    _validate_mod.validate_candidate(evaluation.candidate_records[0])


# ---------------------------------------------------------------------------
# AC4: fail-closed subject_ref/rule_id + projection_digest recomputation
# ---------------------------------------------------------------------------


def test_invalid_subject_ref_judgment_raises_candidate_schema_invalid_not_fallback() -> None:
    """An evaluator-supplied `subject_ref` failing shape validation raises
    `WireContractError(reason_code="candidate_schema_invalid")` -- never a
    Python-synthesized fallback from `candidate_id`."""
    raw_candidate = _judgment_candidate(
        candidate_id="cand-bad-subject-ref", subject_ref={"kind": "not_a_real_kind", "value": "x"}
    )
    with pytest.raises(rr.WireContractError) as excinfo:
        rr._enrich_candidate_record(
            raw_candidate,
            repository_id=_REPOSITORY_ID,
            base_sha=_FULL_SHA,
            source_set_digest=_DIGEST,
            timestamp="2026-01-01T00:00:00Z",
            previous_state=_EMPTY_PREVIOUS_STATE,
            real_evidence_index={},
        )
    assert excinfo.value.reason_code == "candidate_schema_invalid"


def test_invalid_rule_id_judgment_raises_candidate_schema_invalid_not_fallback() -> None:
    raw_candidate = _judgment_candidate(candidate_id="cand-bad-rule-id", rule_id="Not A Valid Rule Id!!")
    with pytest.raises(rr.WireContractError) as excinfo:
        rr._enrich_candidate_record(
            raw_candidate,
            repository_id=_REPOSITORY_ID,
            base_sha=_FULL_SHA,
            source_set_digest=_DIGEST,
            timestamp="2026-01-01T00:00:00Z",
            previous_state=_EMPTY_PREVIOUS_STATE,
            real_evidence_index={},
        )
    assert excinfo.value.reason_code == "candidate_schema_invalid"


def test_run_evaluation_propagates_candidate_schema_invalid_for_bad_subject_ref() -> None:
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx)
    raw_candidate = _judgment_candidate(
        candidate_id="cand-bad-subject-ref-e2e", subject_ref={"kind": "not_a_real_kind", "value": "x"}
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[raw_candidate],
    )
    with pytest.raises(rr.WireContractError) as excinfo:
        _run_evaluation(ctx=ctx, evaluator_request=evaluator_request, raw_payload=raw_payload)
    assert excinfo.value.reason_code == "candidate_schema_invalid"


def test_run_evaluation_repair_covers_construction_phase_candidate_schema_invalid() -> None:
    """A `candidate_schema_invalid` raised at construction (step 4) is
    retried via `repair` -- the repair boundary covers the full
    parse -> enrich -> construct pipeline (PR #2367 fix_delta item 6,
    preserved)."""
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx, finding_sets=_RUNTIME_FINDING_SETS)

    bad_candidate = _judgment_candidate(
        candidate_id="cand-repairable", subject_ref={"kind": "not_a_real_kind", "value": "x"}
    )
    good_candidate = _judgment_candidate(
        candidate_id="cand-repairable",
        subject_ref={"kind": "repository_path", "value": "schemas/repaired.json"},
        evidence_refs=[_RUNTIME_EVIDENCE_REF_JUDGMENT],
    )
    bad_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[bad_candidate],
    )
    good_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[good_candidate],
    )

    repair_calls = {"n": 0}

    def _repair(_text: str, error: rr.WireContractError) -> str:
        repair_calls["n"] += 1
        assert error.reason_code == "candidate_schema_invalid"
        return json.dumps(good_payload, sort_keys=True, separators=(",", ":"))

    evaluation = _run_evaluation(
        ctx=ctx, evaluator_request=evaluator_request, raw_payload=bad_payload, repair=_repair
    )
    assert repair_calls["n"] == 1
    assert evaluation.candidate_records[0]["finding_contract"]["identity"]["key"]["subject_ref"] == {
        "kind": "repository_path",
        "value": "schemas/repaired.json",
    }


def test_projection_digest_recomputed_from_real_evidence_never_evaluator_supplied() -> None:
    """AC4: `evidence_refs[].projection_digest` is Python-recomputed from
    real `finding_sets` data (a real, deterministic hash of the actual
    observer findings) -- proven by changing the real evidence content and
    observing the digest change accordingly, and by never accepting a
    digest from the evaluator's own (judgment-only, digest-less) input."""
    ctx = _make_ctx()
    finding_sets_a = [
        {"observer_id": "retrospective-runtime-observer", "findings": [{"claim": "finding A"}]}
    ]
    finding_sets_b = [
        {"observer_id": "retrospective-runtime-observer", "findings": [{"claim": "finding B"}]}
    ]
    raw_candidate = _judgment_candidate(
        candidate_id="cand-digest", evidence_refs=[_RUNTIME_EVIDENCE_REF_JUDGMENT]
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id, base_sha=ctx.base_sha, source_set_digest=_DIGEST, candidate_records=[raw_candidate]
    )

    eval_a = _run_evaluation(
        ctx=ctx,
        evaluator_request=_make_evaluator_request(ctx, finding_sets=finding_sets_a),
        raw_payload=raw_payload,
    )
    eval_b = _run_evaluation(
        ctx=ctx,
        evaluator_request=_make_evaluator_request(ctx, finding_sets=finding_sets_b),
        raw_payload=raw_payload,
    )
    digest_a = eval_a.candidate_records[0]["finding_contract"]["evaluations"][0]["evidence_refs"][0][
        "projection_digest"
    ]
    digest_b = eval_b.candidate_records[0]["finding_contract"]["evaluations"][0]["evidence_refs"][0][
        "projection_digest"
    ]
    assert digest_a != digest_b
    assert digest_a.startswith("sha256:")
    assert digest_b.startswith("sha256:")


def test_projection_digest_omits_evidence_ref_when_no_real_evidence_available() -> None:
    """AC4: when the evaluator claims an evidence_ref with a `source_id`
    that has NO real backing evidence this run (no observer produced
    findings for that source_type), the ref is dropped -- never kept with
    a fabricated digest."""
    ctx = _make_ctx()
    # only a "runtime" observer produced findings -- the evaluator claims a
    # "web" source_id, which has no real backing data here.
    evaluator_request = _make_evaluator_request(ctx, finding_sets=_RUNTIME_FINDING_SETS)
    raw_candidate = _judgment_candidate(
        candidate_id="cand-no-real-evidence",
        evidence_refs=[
            {
                "ref_type": "external_primary_source",
                "source_id": "web",
                "resource_identity": "https://example.invalid/nonexistent",
            }
        ],
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[raw_candidate],
    )

    # classified with zero evidence_refs fails canonical minItems -- proves
    # the dangling ref was dropped rather than kept with a fabricated
    # digest (which would have let this candidate pass instead).
    with pytest.raises(rr.WireContractError) as excinfo:
        _run_evaluation(ctx=ctx, evaluator_request=evaluator_request, raw_payload=raw_payload)
    assert excinfo.value.reason_code == "candidate_schema_invalid"


def test_projection_digest_never_fabricated_from_broken_string_repr() -> None:
    """AC4 (explicit prohibition): a malformed/garbage evidence_ref value
    from the evaluator is never hashed via a broken string's `repr()` --
    `_enrich_evidence_ref` only accepts well-formed
    `ref_type`/`source_id`/`resource_identity` strings; anything else is
    dropped, not digested."""
    real_evidence_index = rr._observer_source_type_index(_RUNTIME_FINDING_SETS)
    assert rr._enrich_evidence_ref("not-even-a-dict", real_evidence_index=real_evidence_index) is None
    assert rr._enrich_evidence_ref({"ref_type": 123}, real_evidence_index=real_evidence_index) is None
    assert rr._enrich_evidence_ref({}, real_evidence_index=real_evidence_index) is None


def test_multiple_candidate_records_each_derive_independent_identity() -> None:
    """A two-candidate `Evaluation` derives each record's `identity.value`
    independently from ITS OWN judgment values."""
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx, finding_sets=_RUNTIME_FINDING_SETS)

    subject_ref_a = {"kind": "repository_path", "value": "schemas/a.json"}
    subject_ref_b = {"kind": "issue", "value": "42"}

    record_a = _judgment_candidate(
        candidate_id="cand-a",
        claim_class="runtime_behavior",
        subject_ref=subject_ref_a,
        rule_id="runtime_behavior.rule_a",
        evidence_refs=[_RUNTIME_EVIDENCE_REF_JUDGMENT],
    )
    record_b = _judgment_candidate(
        candidate_id="cand-b",
        claim_class="issue_intent",
        subject_ref=subject_ref_b,
        rule_id="issue_intent.rule_b",
        evidence_refs=[_RUNTIME_EVIDENCE_REF_JUDGMENT],
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[record_a, record_b],
    )

    evaluation = _run_evaluation(ctx=ctx, evaluator_request=evaluator_request, raw_payload=raw_payload)
    got_a, got_b = evaluation.candidate_records

    key_a = {
        "repository_id": _REPOSITORY_ID,
        "claim_class": "runtime_behavior",
        "subject_ref": subject_ref_a,
        "rule_id": "runtime_behavior.rule_a",
    }
    key_b = {
        "repository_id": _REPOSITORY_ID,
        "claim_class": "issue_intent",
        "subject_ref": subject_ref_b,
        "rule_id": "issue_intent.rule_b",
    }

    assert got_a["candidate_id"] == "cand-a"
    assert got_b["candidate_id"] == "cand-b"
    assert got_a["finding_contract"]["identity"]["key"] == key_a
    assert got_b["finding_contract"]["identity"]["key"] == key_b
    assert got_a["finding_contract"]["identity"]["value"] == _validate_mod.compute_finding_identity(key_a)
    assert got_b["finding_contract"]["identity"]["value"] == _validate_mod.compute_finding_identity(key_b)
    assert got_a["finding_contract"]["identity"]["value"] != got_b["finding_contract"]["identity"]["value"]


def test_enriched_candidate_independently_passes_canonical_validator() -> None:
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx, finding_sets=_RUNTIME_FINDING_SETS)
    raw_candidate = _judgment_candidate(
        candidate_id="cand-independent",
        rule_id="runtime_behavior.independent_rule",
        evidence_refs=[_RUNTIME_EVIDENCE_REF_JUDGMENT],
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[raw_candidate],
    )

    evaluation = _run_evaluation(ctx=ctx, evaluator_request=evaluator_request, raw_payload=raw_payload)

    # must not raise
    _validate_mod.validate_candidate(evaluation.candidate_records[0])
