"""
Tests for PR #1767 owner review Scope Delta follow-up (2026-07-26):
- AC10: schemas/issue_execution_decision_v1.schema.json digest provenance /
  legacy compatibility metadata (additive `provenance` block).
- AC12: validate_issue_execution_decision.py as the standalone canonical
  module (validate_schema/validate_semantics separation, cross-consumer
  wiring: plan_refinement_loop.py, build_loop_state.py,
  render_termination_report.py, decide_next_loop_action.py).
- AC13: migration envelope (dual_write | equivalence | dual_read |
  new_authoritative | legacy_removed), legacy adapter mapping, equivalence
  digest mismatch fail-closed rejection.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"
sys.path.insert(0, str(SCRIPT_DIR))

import build_loop_state as bls  # noqa: E402
import decide_next_loop_action as dnla  # noqa: E402
import plan_refinement_loop as prl  # noqa: E402
import validate_issue_execution_decision as vied  # noqa: E402


CANONICAL_SCHEMA = json.loads(
    (SCHEMAS_DIR / "issue_execution_decision_v1.schema.json").read_text(encoding="utf-8")
)


def _selected_decision() -> dict:
    return prl.build_issue_execution_decision(
        4242,
        "a" * 64,
        "2026-07-26T00:00:00Z",
        {
            "scope_rollup_result": {
                "schema_version": 2,
                "input": {"completeness": "full", "warnings": []},
                "candidates": [],
            }
        },
    )


# ---------------------------------------------------------------------------
# AC12: validate_schema() / validate_semantics() separation
# ---------------------------------------------------------------------------


def test_validate_schema_and_validate_semantics_are_distinct_functions():
    assert vied.validate_schema is not vied.validate_semantics
    decision = _selected_decision()
    assert vied.validate_schema(decision) == []
    assert vied.validate_semantics(decision) == []


def test_validate_issue_execution_decision_is_schema_first():
    """
    A schema-invalid instance (unknown execution.state) must be rejected via
    validate_schema() output (schema_violation: prefix), and
    validate_semantics() must not even be reached by the combined entry
    point for a non-dict input.
    """
    decision = _selected_decision()
    decision["execution"]["state"] = "not-a-real-state"
    combined = vied.validate_issue_execution_decision(decision)
    assert combined  # rejected
    assert combined == vied.validate_schema(decision)  # schema-first short-circuit


def test_validate_schema_reports_import_unavailability_without_raising(monkeypatch):
    monkeypatch.setattr(vied, "_JSONSCHEMA_AVAILABLE", False)
    assert vied.validate_schema({"anything": True}) == ["jsonschema_not_available"]


# ---------------------------------------------------------------------------
# AC10: provenance block (additive, optional, closed shape)
# ---------------------------------------------------------------------------


def test_valid_decision_without_provenance_still_validates():
    """Existing (#1675) fixtures without provenance must remain valid (additive)."""
    decision = _selected_decision()
    assert "provenance" not in decision
    jsonschema.validate(instance=decision, schema=CANONICAL_SCHEMA)


def test_decision_with_provenance_validates():
    decision = _selected_decision()
    provenance = vied.build_provenance(
        scope_rollup_result=None,
        semantic_decision_sha256=decision["identity"]["collection_digest"],
        artifact_sha256=decision["identity"]["collection_digest"],
    )
    decision["provenance"] = provenance
    jsonschema.validate(instance=decision, schema=CANONICAL_SCHEMA)
    assert vied.validate_issue_execution_decision(decision) == []


def test_provenance_source_manifest_digest_reflects_scope_rollup():
    scope_rollup = {"schema_version": 2, "input": {"completeness": "full", "warnings": []}, "candidates": []}
    provenance = vied.build_provenance(
        scope_rollup_result=scope_rollup,
        semantic_decision_sha256="sha256:" + "a" * 64,
        artifact_sha256="sha256:" + "a" * 64,
    )
    assert provenance["digests"]["source_manifest_sha256"] == vied._sha256_prefixed(
        vied._canonical_json(scope_rollup)
    )


def test_provenance_closed_shape_rejects_unknown_keys():
    decision = _selected_decision()
    provenance = vied.build_provenance(
        scope_rollup_result=None,
        semantic_decision_sha256=decision["identity"]["collection_digest"],
        artifact_sha256=decision["identity"]["collection_digest"],
    )
    provenance["unexpected_field"] = True
    decision["provenance"] = provenance
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=decision, schema=CANONICAL_SCHEMA)


def test_provenance_legacy_compatibility_lists_legacy_identifiers():
    decision = _selected_decision()
    provenance = vied.build_provenance(
        scope_rollup_result=None,
        semantic_decision_sha256=decision["identity"]["collection_digest"],
        artifact_sha256=decision["identity"]["collection_digest"],
    )
    legacy_compat = provenance["legacy_compatibility"]
    assert legacy_compat["legacy_schema_identifiers"] == vied.LEGACY_SCHEMA_IDENTIFIERS
    assert (
        vied.ISSUE_EXECUTION_DECISION_SCHEMA_VERSION
        in legacy_compat["supported_consumer_versions"]
    )


# ---------------------------------------------------------------------------
# AC13: migration envelope + legacy adapter + equivalence fail-closed
# ---------------------------------------------------------------------------


def test_migration_envelope_validates_against_schema():
    decision = _selected_decision()
    envelope = vied.build_migration_envelope(
        phase="dual_write",
        legacy_digest=None,
        new_digest=decision["identity"]["collection_digest"],
        producer_version="1.0.0",
        consumer_capability=["plan_refinement_loop"],
    )
    decision["migration"] = envelope
    jsonschema.validate(instance=decision, schema=CANONICAL_SCHEMA)
    assert vied.validate_migration(envelope) == []


def test_migration_unknown_phase_rejected_by_schema():
    decision = _selected_decision()
    envelope = vied.build_migration_envelope(
        phase="dual_write",
        legacy_digest=None,
        new_digest=decision["identity"]["collection_digest"],
        producer_version="1.0.0",
        consumer_capability=[],
    )
    envelope["phase"] = "not_a_real_phase"
    decision["migration"] = envelope
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=decision, schema=CANONICAL_SCHEMA)


def test_equivalence_phase_with_mismatched_digests_is_fail_closed():
    """
    AC13: 'equivalence' phase with legacy_digest != new_digest must be
    rejected (not silently accepted) by the semantic validator.
    """
    envelope = vied.build_migration_envelope(
        phase="equivalence",
        legacy_digest="sha256:" + "1" * 64,
        new_digest="sha256:" + "2" * 64,
        producer_version="1.0.0",
        consumer_capability=[],
    )
    # A caller that lies about equivalence_result must also be caught.
    envelope["equivalence_result"] = "equivalent"
    violations = vied.validate_migration(envelope)
    assert "equivalence_result_mismatch:declared=equivalent:recomputed=not_equivalent" in violations
    assert "equivalence_phase_digest_mismatch_fail_closed" in violations


def test_equivalence_phase_with_matching_digests_is_accepted():
    digest = "sha256:" + "3" * 64
    envelope = vied.build_migration_envelope(
        phase="equivalence",
        legacy_digest=digest,
        new_digest=digest,
        producer_version="1.0.0",
        consumer_capability=[],
    )
    assert vied.validate_migration(envelope) == []
    assert envelope["equivalence_result"] == "equivalent"


def test_adapt_legacy_graph_to_v1_produces_schema_and_semantically_valid_decision():
    legacy = {
        "graph": {
            "nodes": [{"issue_number": 200, "body_sha256": "sha256:" + "b" * 64}],
            "edges": [
                {
                    "source_issue_number": 100,
                    "target_issue_number": 200,
                    "relation": "depends_on",
                    "evidence": ["legacy_edge"],
                }
            ],
        },
        "execution": {
            "target_state": "planned",
            "predecessor_issue_numbers": [200],
            "reason_codes": ["predecessor open"],
        },
    }
    adapted = vied.adapt_legacy_graph_to_v1(
        legacy,
        target_issue_number=100,
        target_body_sha256="sha256:" + "a" * 64,
        generated_at="2026-07-26T00:00:00Z",
    )
    assert vied.validate_issue_execution_decision(adapted) == []
    # 'planned' + a real pending predecessor must become 'blocked', not a
    # bare 'deferred' that would conflict with deferred_state_with_depends_on_predecessor.
    assert adapted["execution"]["state"] == "blocked"
    assert adapted["execution"]["predecessors"] == [200]


def test_adapt_legacy_graph_to_v1_never_returns_legacy_shape():
    legacy = {"graph": {"nodes": [], "edges": []}, "execution": {"target_state": "planned"}}
    adapted = vied.adapt_legacy_graph_to_v1(
        legacy, target_issue_number=1, target_body_sha256="sha256:" + "a" * 64, generated_at="2026-07-26T00:00:00Z"
    )
    assert "graph" not in adapted
    assert adapted["schema_version"] == "ISSUE_EXECUTION_DECISION_V1"


# ---------------------------------------------------------------------------
# AC12 cross-consumer wiring (downstream consumer + fail-closed on import
# failure for build_loop_state.py and decide_next_loop_action.py)
# ---------------------------------------------------------------------------


def _minimal_loop_state() -> dict:
    return {
        "schema_version": "loop_state/v1",
        "issue_number": 4242,
        "iteration": 0,
        "max_iterations": 3,
        "last_verdict": "approve",
        "termination_reason": None,
        "scope_signal_guard": {"triggered": False, "excluded_by_anchor_reframe": False, "reason_code": None},
        "delivery_rollup": {"applicable": False, "unmaterialized_slots": []},
        "follow_up_materialization": {"candidates": []},
        "web_research_policy": {"required": False, "reason": None, "critical_external_claims": [], "skip_reason": None},
    }


def test_decide_next_loop_action_accepts_semantically_valid_issue_execution_decision():
    state = _minimal_loop_state()
    state["issue_execution_decision"] = _selected_decision()
    valid, msg = dnla.validate_loop_state(state)
    assert valid, msg


def test_decide_next_loop_action_rejects_semantically_invalid_issue_execution_decision():
    """
    AC12 downstream-consumer wiring (negative test): a schema-valid but
    semantically invalid issue_execution_decision (digest mismatch) must be
    rejected by decide_next_loop_action.validate_loop_state(), not just by
    the producer.
    """
    state = _minimal_loop_state()
    decision = _selected_decision()
    decision["identity"]["collection_digest"] = "sha256:" + "0" * 64  # stale/wrong on purpose
    state["issue_execution_decision"] = decision
    valid, msg = dnla.validate_loop_state(state)
    assert not valid
    assert "issue_execution_decision" in msg


def test_decide_next_loop_action_fails_closed_on_validator_import_failure(monkeypatch):
    monkeypatch.setattr(dnla, "_validate_issue_execution_decision", None)
    state = _minimal_loop_state()
    state["issue_execution_decision"] = _selected_decision()
    valid, msg = dnla.validate_loop_state(state)
    assert not valid
    assert "import failed" in msg


def test_build_loop_state_fails_closed_on_validator_import_failure(monkeypatch):
    monkeypatch.setattr(bls, "validate_issue_execution_decision", None)
    plan = {
        "schema_version": "refinement_loop_plan/v1",
        "source": {"issue_number": 4242},
        "decisions": {
            "web_research_policy": {"required": False, "reason_code": None, "critical_external_claims": []},
            "scope_signal_guard": {"triggered": False, "reason_code": None, "excluded_by_anchor_reframe": False},
            "delivery_rollup": {"applicable": False, "unmaterialized_slots": []},
            "follow_up_materialization": {"candidates": []},
        },
        "issue_execution_decision": _selected_decision(),
    }
    review = {"VERDICT": "approve", "issue_number": 4242}
    loop_state, blocked, _ = bls.build_loop_state(plan, review, issue_number=4242, iteration=0)
    assert loop_state is None
    assert any("validator_unavailable" in b for b in blocked)
