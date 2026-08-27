#!/usr/bin/env python3
"""Unit tests for run_retrospective.py's deterministic enrichment phase
(Issue #2362): `run_evaluation()` now separates evaluator LLM model judgment
(`title`/`description`/`claim_class`/`subject_ref`/`rule_id`) from
Python-side deterministic derivation (`repository_id`/`source_run_ref`/
timestamps/`finding_contract.identity.value`) BEFORE constructing
`Evaluation` (the canonical-candidate-validation firing point), instead of
constructing `Evaluation` first (which fires `_post_validate()` /
`_validate_candidate_records()` synchronously via `__post_init__`) and
attempting to patch up `identity` afterwards -- a design that cannot work
because validation has already run by the time construction returns.

Runtime Verification Applicability: not_applicable for this file (a pure
fixture/subprocess-mock harness with no real Agent CLI invocation, mirroring
`test_run_retrospective.py`'s own "deferred" classification). AC3's runtime
verification is a separate module,
`test_run_evaluation_enrichment_live_cli.py` (marked `claude_live`, invoked
only via `verify_run_evaluation_enrichment_live_cli.sh`).
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402

_validate_mod = rr._validate_retrospective_schema_module()

_FULL_SHA = "a" * 40
_DIGEST = "d" * 64
#: matches _identity_key()'s hardcoded repository_id in test_run_retrospective.py
_REPOSITORY_ID = "squne121/loop-protocol"


# ---------------------------------------------------------------------------
# fixture builders (self-contained -- mirrors, but does not import,
# test_run_retrospective.py's local `_evaluation_entry`/`_canonical_candidate`
# helpers)
# ---------------------------------------------------------------------------


def _evidence_ref(suffix: str = "1") -> dict[str, Any]:
    return {
        "ref_type": "repository_blob",
        "source_id": "repository",
        "resource_identity": f"schemas/x.json#{suffix}",
        "projection_digest": "sha256:" + ("1" * 64),
    }


def _evaluation_entry(*, rule_id: str, seq: int = 1) -> dict[str, Any]:
    """A single, minimal ``classified``/``new`` evaluation entry -- the same
    shape ``_new_candidate()`` in ``test_run_retrospective.py`` produces."""
    eval_id = "sha256:" + format(abs(hash((rule_id, seq))), "064x")[:64]
    signal = {"signal_type": "boolean", "value": True, "comparator": "eq", "worse_direction": "not_applicable"}
    return {
        "evaluation_id": eval_id,
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
        "current_signal": signal,
        "expected_signal": None,
        "evidence_refs": [_evidence_ref()],
        "classified_at": "2026-08-22T00:00:00Z",
        "classifier_version": "run_retrospective/v1",
    }


def _raw_candidate(
    *,
    candidate_id: str,
    title: str,
    description: str,
    claim_class: str,
    subject_ref: dict[str, Any],
    rule_id: str,
    evaluations: list[dict[str, Any]],
    fake_identity_key_repository_id: str = "totally-wrong-repo-id",
    fake_identity_value: str = "sha256:" + ("0" * 64),
    fake_source_run_ref: dict[str, Any] | None = None,
    fake_timestamp: str = "2000-01-01T00:00:00Z",
) -> dict[str, Any]:
    """A raw (NOT yet enriched) candidate record dict, shaped like a real
    evaluator's ``EVALUATION_RESULT_V1`` output -- including a deliberately
    WRONG ``finding_contract.identity`` (fake ``repository_id``/``value``),
    WRONG ``source_run_ref``, and stale ``created_at``/``updated_at``, all
    of which the deterministic-enrichment phase must unconditionally
    overwrite (Issue #2362 Identity/Deterministic Field Authority Matrix) --
    never propagate to the constructed ``Evaluation``."""
    return {
        "candidate_id": candidate_id,
        "candidate_status": "proposed",
        "title": title,
        "description": description,
        "source_run_ref": fake_source_run_ref or {"base_sha": "f" * 40, "source_set_digest": "e" * 64},
        "created_at": fake_timestamp,
        "updated_at": fake_timestamp,
        "finding_contract": {
            "schema_version": "v1",
            "identity": {
                "algorithm": "sha256-jcs-v1",
                "key": {
                    "repository_id": fake_identity_key_repository_id,
                    "claim_class": claim_class,
                    "subject_ref": subject_ref,
                    "rule_id": rule_id,
                },
                "value": fake_identity_value,
            },
            "claim_class": claim_class,
            "evaluations": evaluations,
        },
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


def _make_evaluator_request(ctx: rr.RunContext, *, source_set_digest: str = _DIGEST) -> rr.EvaluatorRequest:
    return rr.EvaluatorRequest(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=source_set_digest,
        finding_sets=[],
    )


# ---------------------------------------------------------------------------
# AC1: enrichment precedes canonical candidate validation
# ---------------------------------------------------------------------------


def test_enrichment_precedes_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: `run_evaluation()`'s outer-envelope-parse phase (step 1) goes
    through the SAME shared `_parse_wire_payload()` helper
    `_WireEnvelope.from_wire()` uses -- proven by spying on it -- and
    canonical candidate validation (`_validate_candidate_records()`, fired
    by `Evaluation.__post_init__` at construction, step 4) only runs AFTER
    the deterministic-enrichment phase (steps 2-3) has already overwritten
    the evaluator's broken/fake identity: the outer-envelope-parse spy sees
    the RAW (still-broken) `repository_id`, while the validation spy sees
    only the ENRICHED (authoritative) one -- proving the ordering, not just
    the end result."""
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx)
    subject_ref = {"kind": "repository_path", "value": "schemas/order.json"}
    raw_candidate = _raw_candidate(
        candidate_id="cand-order",
        title="order title",
        description="order description",
        claim_class="runtime_behavior",
        subject_ref=subject_ref,
        rule_id="order_rule",
        evaluations=[_evaluation_entry(rule_id="order_rule")],
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[raw_candidate],
    )

    call_order: list[str] = []
    parse_wire_payload_snapshots: list[dict[str, Any]] = []
    validate_snapshots: list[list[dict[str, Any]]] = []

    original_parse_wire_payload = rr._parse_wire_payload
    original_validate = rr._validate_candidate_records

    def _spy_parse_wire_payload(cls: type, text: str) -> dict[str, Any]:
        call_order.append("parse_wire_payload")
        result = original_parse_wire_payload(cls, text)
        parse_wire_payload_snapshots.append(copy.deepcopy(result))
        return result

    def _spy_validate(records: list[dict[str, Any]]) -> None:
        call_order.append("validate_candidate_records")
        validate_snapshots.append(copy.deepcopy(records))
        return original_validate(records)

    monkeypatch.setattr(rr, "_parse_wire_payload", _spy_parse_wire_payload)
    monkeypatch.setattr(rr, "_validate_candidate_records", _spy_validate)

    def _invoke(_request: rr.EvaluatorRequest) -> rr.AgentInvocationResult:
        return _ok_agent_result(raw_payload)

    evaluation = rr.run_evaluation(ctx, evaluator_request, invoke_evaluator=_invoke, repository_id=_REPOSITORY_ID)

    assert call_order == ["parse_wire_payload", "validate_candidate_records"]

    # step 1 (outer envelope parse) saw the RAW, not-yet-enriched broken
    # identity -- proving enrichment has NOT run yet at this point.
    raw_seen_key = parse_wire_payload_snapshots[0]["candidate_records"][0]["finding_contract"]["identity"]["key"]
    assert raw_seen_key["repository_id"] == "totally-wrong-repo-id"

    # step 4 (construction -> canonical validation) saw the ENRICHED,
    # authoritative identity -- never the evaluator's broken one.
    validated_key = validate_snapshots[0][0]["finding_contract"]["identity"]["key"]
    assert validated_key["repository_id"] == _REPOSITORY_ID

    # the final Evaluation object (returned to the caller) also carries the
    # enriched identity, confirming candidate_schema_invalid never fired.
    final_key = evaluation.candidate_records[0]["finding_contract"]["identity"]["key"]
    assert final_key["repository_id"] == _REPOSITORY_ID


# ---------------------------------------------------------------------------
# AC2: identity.value computed via compute_finding_identity() (SSOT reuse)
# ---------------------------------------------------------------------------


def test_compute_finding_identity_reuse() -> None:
    """AC2: the deterministic-enrichment phase computes
    `finding_contract.identity.value` by calling
    `validate_retrospective_schema.py`'s `compute_finding_identity()`
    (via the sibling module loader) -- not a reimplementation of JCS
    normalization + SHA-256."""
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx)
    subject_ref = {"kind": "repository_path", "value": "schemas/reuse.json"}
    raw_candidate = _raw_candidate(
        candidate_id="cand-reuse",
        title="reuse title",
        description="reuse description",
        claim_class="runtime_behavior",
        subject_ref=subject_ref,
        rule_id="reuse_rule",
        evaluations=[_evaluation_entry(rule_id="reuse_rule")],
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[raw_candidate],
    )

    def _invoke(_request: rr.EvaluatorRequest) -> rr.AgentInvocationResult:
        return _ok_agent_result(raw_payload)

    evaluation = rr.run_evaluation(ctx, evaluator_request, invoke_evaluator=_invoke, repository_id=_REPOSITORY_ID)
    identity = evaluation.candidate_records[0]["finding_contract"]["identity"]

    expected_key = {
        "repository_id": _REPOSITORY_ID,
        "claim_class": "runtime_behavior",
        "subject_ref": subject_ref,
        "rule_id": "reuse_rule",
    }
    assert identity["key"] == expected_key
    assert identity["algorithm"] == _validate_mod.FINDING_IDENTITY_ALGORITHM
    # independently re-derived via the canonical SSOT -- proves reuse, not
    # a shadow reimplementation.
    assert identity["value"] == _validate_mod.compute_finding_identity(expected_key)


# ---------------------------------------------------------------------------
# AC4: authoritative overwrite / determinism / sensitivity / independence
# ---------------------------------------------------------------------------


def test_broken_evaluator_identity_is_overwritten_by_authoritative_enrichment() -> None:
    """AC4(a): an evaluator response with a broken/fake
    `finding_contract.identity.key.repository_id` and `identity.value` (and
    a wrong `source_run_ref`/stale timestamps) is unconditionally overwritten
    by the authoritative 4-key identity + Python-side run context --
    `run_evaluation()` never raises `candidate_schema_invalid` for this
    input, and none of the evaluator's fake values survive into the
    returned `Evaluation`."""
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx)
    subject_ref = {"kind": "repository_path", "value": "schemas/broken.json"}
    raw_candidate = _raw_candidate(
        candidate_id="cand-broken",
        title="broken title",
        description="broken description",
        claim_class="runtime_behavior",
        subject_ref=subject_ref,
        rule_id="broken_rule",
        evaluations=[_evaluation_entry(rule_id="broken_rule")],
        fake_identity_key_repository_id="attacker-controlled-repo",
        fake_identity_value="sha256:" + ("f" * 64),
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[raw_candidate],
    )

    def _invoke(_request: rr.EvaluatorRequest) -> rr.AgentInvocationResult:
        return _ok_agent_result(raw_payload)

    # must not raise WireContractError(reason_code="candidate_schema_invalid")
    evaluation = rr.run_evaluation(ctx, evaluator_request, invoke_evaluator=_invoke, repository_id=_REPOSITORY_ID)

    record = evaluation.candidate_records[0]
    identity = record["finding_contract"]["identity"]

    assert identity["key"]["repository_id"] == _REPOSITORY_ID
    assert identity["key"]["repository_id"] != "attacker-controlled-repo"
    assert identity["value"] != "sha256:" + ("f" * 64)
    assert identity["value"] == _validate_mod.compute_finding_identity(identity["key"])

    assert record["source_run_ref"] == {
        "base_sha": ctx.base_sha,
        "source_set_digest": evaluator_request.source_set_digest,
    }
    assert record["created_at"] != "2000-01-01T00:00:00Z"
    assert record["updated_at"] != "2000-01-01T00:00:00Z"


def test_identity_value_deterministic_for_same_judgment_and_context() -> None:
    """AC4(b): the same model-judgment values + the same Python-side
    context always produce the same `identity.value` -- calling
    `_enrich_candidate_record` twice with identical inputs is idempotent."""
    raw_candidate = _raw_candidate(
        candidate_id="cand-det",
        title="det title",
        description="det description",
        claim_class="runtime_behavior",
        subject_ref={"kind": "repository_path", "value": "schemas/det.json"},
        rule_id="det_rule",
        evaluations=[_evaluation_entry(rule_id="det_rule")],
    )
    kwargs: dict[str, Any] = {
        "repository_id": _REPOSITORY_ID,
        "base_sha": _FULL_SHA,
        "source_set_digest": _DIGEST,
        "timestamp": "2026-01-01T00:00:00Z",
    }

    first = rr._enrich_candidate_record(copy.deepcopy(raw_candidate), **kwargs)
    second = rr._enrich_candidate_record(copy.deepcopy(raw_candidate), **kwargs)

    assert first["finding_contract"]["identity"]["value"] == second["finding_contract"]["identity"]["value"]
    assert first["finding_contract"]["identity"]["key"] == second["finding_contract"]["identity"]["key"]


def test_identity_value_changes_when_judgment_field_changes() -> None:
    """AC4(c): changing any one of `repository_id`/`claim_class`/
    `subject_ref`/`rule_id` changes `identity.value` relative to a fixed
    baseline (the other three fields held constant each time)."""
    common_kwargs: dict[str, Any] = {
        "base_sha": _FULL_SHA,
        "source_set_digest": _DIGEST,
        "timestamp": "2026-01-01T00:00:00Z",
    }

    def _candidate(*, claim_class: str, subject_ref: dict[str, Any], rule_id: str) -> dict[str, Any]:
        return _raw_candidate(
            candidate_id="cand-sensitivity",
            title="t",
            description="d",
            claim_class=claim_class,
            subject_ref=subject_ref,
            rule_id=rule_id,
            evaluations=[_evaluation_entry(rule_id=rule_id)],
        )

    baseline_subject_ref = {"kind": "repository_path", "value": "schemas/baseline.json"}
    baseline = rr._enrich_candidate_record(
        _candidate(claim_class="runtime_behavior", subject_ref=baseline_subject_ref, rule_id="baseline_rule"),
        repository_id=_REPOSITORY_ID,
        **common_kwargs,
    )
    baseline_value = baseline["finding_contract"]["identity"]["value"]

    # repository_id differs (Python-side context, not evaluator judgment)
    other_repo = rr._enrich_candidate_record(
        _candidate(claim_class="runtime_behavior", subject_ref=baseline_subject_ref, rule_id="baseline_rule"),
        repository_id="other-org/other-repo",
        **common_kwargs,
    )
    assert other_repo["finding_contract"]["identity"]["value"] != baseline_value

    # claim_class differs (evaluator judgment)
    other_claim_class = rr._enrich_candidate_record(
        _candidate(claim_class="issue_intent", subject_ref={"kind": "issue", "value": "42"}, rule_id="baseline_rule"),
        repository_id=_REPOSITORY_ID,
        **common_kwargs,
    )
    assert other_claim_class["finding_contract"]["identity"]["value"] != baseline_value

    # subject_ref differs (evaluator judgment)
    other_subject_ref = rr._enrich_candidate_record(
        _candidate(
            claim_class="runtime_behavior",
            subject_ref={"kind": "repository_path", "value": "schemas/other.json"},
            rule_id="baseline_rule",
        ),
        repository_id=_REPOSITORY_ID,
        **common_kwargs,
    )
    assert other_subject_ref["finding_contract"]["identity"]["value"] != baseline_value

    # rule_id differs (evaluator judgment)
    other_rule_id = rr._enrich_candidate_record(
        _candidate(claim_class="runtime_behavior", subject_ref=baseline_subject_ref, rule_id="other_rule"),
        repository_id=_REPOSITORY_ID,
        **common_kwargs,
    )
    assert other_rule_id["finding_contract"]["identity"]["value"] != baseline_value


def test_multiple_candidate_records_each_derive_independent_identity() -> None:
    """AC4(d): a two-candidate `Evaluation` derives each record's
    `identity.value` independently from ITS OWN judgment values -- never
    mixing/cross-contaminating with the other record's fields."""
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx)

    subject_ref_a = {"kind": "repository_path", "value": "schemas/a.json"}
    subject_ref_b = {"kind": "issue", "value": "42"}

    record_a = _raw_candidate(
        candidate_id="cand-a",
        title="a title",
        description="a description",
        claim_class="runtime_behavior",
        subject_ref=subject_ref_a,
        rule_id="rule_a",
        evaluations=[_evaluation_entry(rule_id="rule_a", seq=1)],
    )
    record_b = _raw_candidate(
        candidate_id="cand-b",
        title="b title",
        description="b description",
        claim_class="issue_intent",
        subject_ref=subject_ref_b,
        rule_id="rule_b",
        evaluations=[_evaluation_entry(rule_id="rule_b", seq=2)],
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[record_a, record_b],
    )

    def _invoke(_request: rr.EvaluatorRequest) -> rr.AgentInvocationResult:
        return _ok_agent_result(raw_payload)

    evaluation = rr.run_evaluation(ctx, evaluator_request, invoke_evaluator=_invoke, repository_id=_REPOSITORY_ID)
    got_a, got_b = evaluation.candidate_records

    key_a = {
        "repository_id": _REPOSITORY_ID,
        "claim_class": "runtime_behavior",
        "subject_ref": subject_ref_a,
        "rule_id": "rule_a",
    }
    key_b = {
        "repository_id": _REPOSITORY_ID,
        "claim_class": "issue_intent",
        "subject_ref": subject_ref_b,
        "rule_id": "rule_b",
    }

    assert got_a["candidate_id"] == "cand-a"
    assert got_b["candidate_id"] == "cand-b"
    assert got_a["finding_contract"]["identity"]["key"] == key_a
    assert got_b["finding_contract"]["identity"]["key"] == key_b
    assert got_a["finding_contract"]["identity"]["value"] == _validate_mod.compute_finding_identity(key_a)
    assert got_b["finding_contract"]["identity"]["value"] == _validate_mod.compute_finding_identity(key_b)
    assert got_a["finding_contract"]["identity"]["value"] != got_b["finding_contract"]["identity"]["value"]


# ---------------------------------------------------------------------------
# regression: canonical `validate_candidate()` independently accepts the
# enriched record (not merely `Evaluation.__post_init__` succeeding)
# ---------------------------------------------------------------------------


def test_enriched_candidate_independently_passes_canonical_validator() -> None:
    """Regression coverage: the enriched candidate record independently
    passes `validate_retrospective_schema.py`'s `validate_candidate()` when
    re-checked outside `Evaluation.__post_init__` -- guards against a
    future change accidentally relying on some Evaluation-construction-time
    side effect instead of producing a genuinely schema-valid record."""
    ctx = _make_ctx()
    evaluator_request = _make_evaluator_request(ctx)
    raw_candidate = _raw_candidate(
        candidate_id="cand-independent",
        title="independent title",
        description="independent description",
        claim_class="runtime_behavior",
        subject_ref={"kind": "repository_path", "value": "schemas/independent.json"},
        rule_id="independent_rule",
        evaluations=[_evaluation_entry(rule_id="independent_rule")],
    )
    raw_payload = _raw_evaluation_payload(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=evaluator_request.source_set_digest,
        candidate_records=[raw_candidate],
    )

    def _invoke(_request: rr.EvaluatorRequest) -> rr.AgentInvocationResult:
        return _ok_agent_result(raw_payload)

    evaluation = rr.run_evaluation(ctx, evaluator_request, invoke_evaluator=_invoke, repository_id=_REPOSITORY_ID)

    # must not raise
    _validate_mod.validate_candidate(evaluation.candidate_records[0])
