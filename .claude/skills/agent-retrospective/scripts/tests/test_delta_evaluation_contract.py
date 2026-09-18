#!/usr/bin/env python3
"""Tests for the finding_contract.evaluations[] 5-axis delta judgement table
(Issue #2288 AC3, plus the delta-related portion of AC7).

This module does NOT implement or exercise a delta-*computation* algorithm (that is
Issue #2237's responsibility, see the module docstring of
validate_retrospective_schema.py) -- it only proves that already-produced evaluation
records representing each branch of the judgement table are representable in, and
correctly accepted/rejected by, the schema + validate_candidate()/
validate_finding_contract_history():

  - new
  - resolved (source_coverage complete)
  - recurrent + signal_delta regressed (simultaneous representation)
  - unchanged (distinguished by signal_delta value: unchanged vs improved)
  - indeterminate (partial source coverage does not resolve to 'resolved')
  - multi-evaluation history chained via previous_evaluation_ref

Issue #2644 AC2/AC3/AC8/AC9 additionally exercise `run_retrospective.compute_delta()`'s
`current_source_coverage`-aware gating directly (this run's OWN session-source
coverage, a separate axis from `previous.status` above) -- these ARE delta-
*computation*-algorithm tests, deliberately colocated here since this file is this
module's "delta evaluation contract" home.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import jsonschema
import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402
import validate_retrospective_schema as vrs  # noqa: E402


def _evaluation_of(fixture_name):
    """Return (candidate, terminal_evaluation).

    Several fixtures now carry a multi-event history (e.g. new -> resolved ->
    recurrent) so that each judgement-table branch is reached via a semantically
    legal predecessor state (Issue #2288 P0-2) rather than being asserted as the
    (illegal) very first evaluation ever recorded. The evaluation under test is
    always the *last* (most recent) entry in the history.
    """
    candidate = vrs.load_fixture(fixture_name)
    return candidate, candidate["finding_contract"]["evaluations"][-1]


# ---------------------------------------------------------------------------
# positive: each judgement-table branch fixture validates and has the expected
# axis values
# ---------------------------------------------------------------------------


def test_new_fixture_evaluation_axes():
    candidate, evaluation = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.new.valid.json"
    )
    vrs.validate_candidate(candidate)
    assert evaluation["presence_delta"] == "new"
    assert evaluation["delta_status"] == "new"
    assert evaluation["evaluation_status"] == "classified"
    assert evaluation["indeterminate_reason"] is None


def test_resolved_fixture_requires_complete_source_coverage():
    candidate, evaluation = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.resolved.valid.json"
    )
    vrs.validate_candidate(candidate)
    assert evaluation["presence_delta"] == "resolved"
    assert evaluation["delta_status"] == "resolved"
    assert evaluation["source_coverage"] == "complete"
    assert evaluation["observed"] is False


def test_recurrent_and_regressed_simultaneous_representation():
    candidate, evaluation = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.recurrent_regressed.valid.json"
    )
    vrs.validate_candidate(candidate)
    assert evaluation["presence_delta"] == "recurrent"
    assert evaluation["signal_delta"] == "regressed"
    # delta_status (single-enum summary) takes 'recurrent' precedence, but the
    # underlying regression is still separately observable via signal_delta.
    assert evaluation["delta_status"] == "recurrent"


@pytest.mark.parametrize(
    "fixture_name,expected_signal_delta",
    [
        ("agent_improvement_candidate_v1.finding_contract.unchanged.valid.json", "unchanged"),
        ("agent_improvement_candidate_v1.finding_contract.improved.valid.json", "improved"),
    ],
)
def test_unchanged_delta_status_distinguished_by_signal_delta(fixture_name, expected_signal_delta):
    candidate, evaluation = _evaluation_of(fixture_name)
    vrs.validate_candidate(candidate)
    assert evaluation["presence_delta"] == "active"
    assert evaluation["signal_delta"] == expected_signal_delta
    assert evaluation["delta_status"] == "unchanged"


def test_indeterminate_with_partial_source_coverage_is_not_resolved():
    candidate, evaluation = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.indeterminate.valid.json"
    )
    vrs.validate_candidate(candidate)
    assert evaluation["source_coverage"] == "partial"
    assert evaluation["evaluation_status"] == "indeterminate"
    assert "delta_status" not in evaluation
    assert evaluation["indeterminate_reason"] == "source_partial"


def test_history_chain_validates_and_links_previous_evaluation_ref():
    candidate = vrs.load_fixture(
        "agent_improvement_candidate_v1.finding_contract.history_chain.valid.json"
    )
    vrs.validate_candidate(candidate)
    evaluations = candidate["finding_contract"]["evaluations"]
    # new -> resolved -> recurrent+regressed: a 'recurrent' evaluation is only
    # semantically legal when the immediately preceding classified evaluation was
    # 'resolved' (Issue #2288 P0-2), so this chain has three links, not two.
    assert len(evaluations) == 3
    assert evaluations[0]["previous_evaluation_ref"] is None
    assert evaluations[1]["previous_evaluation_ref"] == evaluations[0]["evaluation_id"]
    assert evaluations[2]["previous_evaluation_ref"] == evaluations[1]["evaluation_id"]


# ---------------------------------------------------------------------------
# negative: each schema-level mutual-exclusivity invariant is enforced
# ---------------------------------------------------------------------------


def test_indeterminate_evaluation_cannot_carry_a_delta_status():
    candidate, _ = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.indeterminate.valid.json"
    )
    candidate = copy.deepcopy(candidate)
    candidate["finding_contract"]["evaluations"][0]["delta_status"] = "resolved"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_classified_evaluation_requires_null_indeterminate_reason():
    candidate, _ = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.new.valid.json"
    )
    candidate = copy.deepcopy(candidate)
    candidate["finding_contract"]["evaluations"][0]["indeterminate_reason"] = "source_partial"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_classified_evaluation_requires_delta_status():
    candidate, _ = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.new.valid.json"
    )
    candidate = copy.deepcopy(candidate)
    del candidate["finding_contract"]["evaluations"][0]["delta_status"]
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_incomplete_source_coverage_forces_indeterminate_status():
    candidate, _ = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.resolved.valid.json"
    )
    candidate = copy.deepcopy(candidate)
    candidate["finding_contract"]["evaluations"][-1]["source_coverage"] = "unavailable"
    # evaluation_status stays 'classified' while source_coverage is incomplete -- invalid
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_recurrent_presence_delta_forces_delta_status_recurrent_precedence():
    candidate, _ = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.recurrent_regressed.valid.json"
    )
    candidate = copy.deepcopy(candidate)
    # attempting to report delta_status='regressed' despite presence_delta='recurrent'
    # violates the fixed precedence rule (recurrent takes summary precedence).
    candidate["finding_contract"]["evaluations"][-1]["delta_status"] = "regressed"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_classified_evaluation_without_evidence_or_baseline_rejected():
    candidate, _ = _evaluation_of(
        "agent_improvement_candidate_v1.finding_contract.new.valid.json"
    )
    candidate = copy.deepcopy(candidate)
    candidate["finding_contract"]["evaluations"][0]["evidence_refs"] = []
    candidate["finding_contract"]["evaluations"][0]["baseline_signal"] = None
    candidate["finding_contract"]["evaluations"][0]["current_signal"] = None
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(candidate)


def test_evaluation_history_inconsistency_rejected():
    candidate = copy.deepcopy(
        vrs.load_fixture(
            "agent_improvement_candidate_v1.finding_contract.history_chain.valid.json"
        )
    )
    candidate["finding_contract"]["evaluations"][1]["previous_evaluation_ref"] = (
        "sha256:" + "9" * 64
    )
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_candidate(candidate)


def test_first_evaluation_with_non_null_previous_ref_rejected():
    candidate = copy.deepcopy(
        vrs.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")
    )
    candidate["finding_contract"]["evaluations"][0]["previous_evaluation_ref"] = (
        "sha256:" + "9" * 64
    )
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_candidate(candidate)


# ---------------------------------------------------------------------------
# regression fixtures: full invalid-fixture set loads and fails validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name",
    [
        "agent_improvement_candidate_v1.finding_contract.invalid_partial_envelope.json",
        "agent_improvement_candidate_v1.finding_contract.invalid_malformed_identity.json",
        "agent_improvement_candidate_v1.finding_contract.invalid_stale_identity.json",
        "agent_improvement_candidate_v1.finding_contract.invalid_claim_class.json",
        "agent_improvement_candidate_v1.finding_contract.invalid_incomplete_coverage_resolved.json",
        "agent_improvement_candidate_v1.finding_contract.invalid_no_evidence_classified.json",
        "agent_improvement_candidate_v1.finding_contract.invalid_raw_evidence.json",
        "agent_improvement_candidate_v1.finding_contract.invalid_history_inconsistency.json",
    ],
)
def test_finding_contract_invalid_fixtures_fail_schema_validation(fixture_name):
    instance = vrs.load_fixture(fixture_name)
    assert vrs.is_valid_candidate(instance) is False
    with pytest.raises((jsonschema.exceptions.ValidationError, vrs.RetrospectiveSchemaError)):
        vrs.validate_candidate(instance)


# ---------------------------------------------------------------------------
# P0-2 (Issue #2288 human review): 'recurrent' requires a legitimate preceding
# 'resolved'/'still_absent' classified state; history chain integrity (unique ids,
# immediate-predecessor-only linkage).
# ---------------------------------------------------------------------------


def test_first_evaluation_cannot_be_recurrent():
    candidate = copy.deepcopy(
        vrs.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")
    )
    evaluation = candidate["finding_contract"]["evaluations"][0]
    evaluation["presence_delta"] = "recurrent"
    evaluation["delta_status"] = "recurrent"
    # schema-level mutual-exclusivity invariants are still satisfied (observed=true,
    # delta_status='recurrent' matches presence_delta='recurrent') -- only the
    # judgement-table invariant that a first-ever classified evaluation cannot assert
    # 'recurrent' (there is no earlier classified state to have recurred from) is
    # violated, so this must raise RetrospectiveSchemaError specifically.
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_candidate(candidate)


def test_recurrent_requires_prior_classified_resolved():
    candidate = copy.deepcopy(
        vrs.load_fixture(
            "agent_improvement_candidate_v1.finding_contract.history_chain.valid.json"
        )
    )
    # history_chain.valid.json is new -> resolved -> recurrent+regressed. Change the
    # middle 'resolved' evaluation to 'active' so the immediately preceding classified
    # presence_delta for the final 'recurrent' evaluation is no longer 'resolved' (or
    # 'still_absent') -- 'recurrent' must then be rejected.
    middle = candidate["finding_contract"]["evaluations"][1]
    middle["presence_delta"] = "active"
    middle["observed"] = True
    middle["delta_status"] = "unchanged"
    middle["signal_delta"] = "unchanged"
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_candidate(candidate)


def test_history_requires_unique_ids_and_immediate_predecessor():
    base = vrs.load_fixture(
        "agent_improvement_candidate_v1.finding_contract.history_chain.valid.json"
    )

    duplicate_id_candidate = copy.deepcopy(base)
    evaluations = duplicate_id_candidate["finding_contract"]["evaluations"]
    evaluations[2]["evaluation_id"] = evaluations[0]["evaluation_id"]
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_candidate(duplicate_id_candidate)

    skip_ahead_candidate = copy.deepcopy(base)
    evaluations = skip_ahead_candidate["finding_contract"]["evaluations"]
    # evaluations[2] should reference evaluations[1] (the immediately preceding
    # entry); pointing it at evaluations[0] instead ("forking"/"skipping ahead") must
    # be rejected even though evaluations[0]'s evaluation_id is a real, earlier ID.
    evaluations[2]["previous_evaluation_ref"] = evaluations[0]["evaluation_id"]
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_candidate(skip_ahead_candidate)


# ---------------------------------------------------------------------------
# P0-3 (Issue #2288 human review): indeterminate/presence_delta and
# observed/presence_delta consistency.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("presence_delta,observed", [("resolved", False), ("recurrent", True)])
def test_indeterminate_cannot_assert_resolved_or_recurrent(presence_delta, observed):
    candidate = copy.deepcopy(
        vrs.load_fixture(
            "agent_improvement_candidate_v1.finding_contract.indeterminate.valid.json"
        )
    )
    evaluation = candidate["finding_contract"]["evaluations"][0]
    assert evaluation["evaluation_status"] == "indeterminate"
    evaluation["presence_delta"] = presence_delta
    evaluation["observed"] = observed
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_candidate(candidate)


@pytest.mark.parametrize(
    "observed,presence_delta",
    [
        (True, "resolved"),
        (True, "still_absent"),
        (False, "new"),
        (False, "active"),
    ],
)
def test_observed_and_presence_delta_must_agree(observed, presence_delta):
    candidate = copy.deepcopy(
        vrs.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")
    )
    evaluation = candidate["finding_contract"]["evaluations"][0]
    evaluation["observed"] = observed
    evaluation["presence_delta"] = presence_delta
    if presence_delta == "resolved":
        evaluation["delta_status"] = "resolved"
    elif presence_delta in ("still_absent", "active"):
        evaluation["delta_status"] = "unchanged"
    # Either the schema-level delta_status/presence_delta const invariant or the
    # validator-level observed/presence_delta consistency check may be the first to
    # reject this instance; either way, an observed/presence_delta mismatch must
    # never validate successfully.
    with pytest.raises((jsonschema.exceptions.ValidationError, vrs.RetrospectiveSchemaError)):
        vrs.validate_candidate(candidate)


def test_incomplete_coverage_resolved_fixture_is_indeterminate_and_rejected():
    # Regression check specifically for the fixture the human review flagged as
    # under-testing this rule (Issue #2288 P0-3): the on-disk fixture must exercise
    # 'evaluation_status=indeterminate' + 'presence_delta=resolved' -- i.e. the
    # validator-level rule above -- not merely the pre-existing schema-level
    # "source_coverage forces evaluation_status" invariant.
    instance = vrs.load_fixture(
        "agent_improvement_candidate_v1.finding_contract.invalid_incomplete_coverage_resolved.json"
    )
    evaluation = instance["finding_contract"]["evaluations"][0]
    assert evaluation["evaluation_status"] == "indeterminate"
    assert evaluation["presence_delta"] == "resolved"
    assert "delta_status" not in evaluation
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_candidate(instance)


# ---------------------------------------------------------------------------
# P0-4 (Issue #2288 human review): signal is a typed union discriminated by
# signal_type; baseline/current/expected signal specs must agree within an
# evaluation.
# ---------------------------------------------------------------------------


def test_signal_type_discriminates_value_and_comparator():
    base = vrs.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")

    wrong_value_type = copy.deepcopy(base)
    # current_signal.signal_type is 'boolean'; an integer value must be rejected.
    wrong_value_type["finding_contract"]["evaluations"][0]["current_signal"]["value"] = 1
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(wrong_value_type)

    disallowed_comparator = copy.deepcopy(base)
    # 'lt' is a numeric-only comparator, not valid for a boolean signal_type.
    disallowed_comparator["finding_contract"]["evaluations"][0]["current_signal"][
        "comparator"
    ] = "lt"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(disallowed_comparator)

    tolerance_on_boolean = copy.deepcopy(base)
    # tolerance is only meaningful (and only permitted) for numeric signal_types.
    tolerance_on_boolean["finding_contract"]["evaluations"][0]["current_signal"][
        "tolerance"
    ] = 0.1
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_candidate(tolerance_on_boolean)


def test_baseline_current_signal_specs_must_match():
    candidate = copy.deepcopy(
        vrs.load_fixture(
            "agent_improvement_candidate_v1.finding_contract.history_chain.valid.json"
        )
    )
    # the terminal (recurrent+regressed) evaluation has integer baseline_signal/
    # current_signal/expected_signal that all share worse_direction='higher'.
    evaluation = candidate["finding_contract"]["evaluations"][-1]
    assert evaluation["baseline_signal"]["worse_direction"] == "higher"
    evaluation["current_signal"]["worse_direction"] = "lower"
    with pytest.raises(vrs.RetrospectiveSchemaError):
        vrs.validate_candidate(candidate)


# ---------------------------------------------------------------------------
# Issue #2644 AC2/AC3/AC8/AC9: compute_delta()'s current_source_coverage-aware
# "resolved" synthesis gating -- a SEPARATE axis from previous.status above.
# ---------------------------------------------------------------------------


def _runtime_evidence_ref(suffix: str = "1") -> dict:
    return {
        "ref_type": "runtime_receipt",
        "source_id": "runtime",
        "resource_identity": f"runtime-session#{suffix}",
        "projection_digest": "sha256:" + ("2" * 64),
    }


def _repository_evidence_ref(suffix: str = "1") -> dict:
    return {
        "ref_type": "repository_blob",
        "source_id": "repository",
        "resource_identity": f"schemas/x.json#{suffix}",
        "projection_digest": "sha256:" + ("1" * 64),
    }


def _last_evaluation_only_candidate(*, identity_value: str, evidence_refs: list[dict]) -> dict:
    """A minimal previous-candidate dict shaped exactly like what
    `compute_delta()` reads (`finding_contract.identity.value` +
    `finding_contract.evaluations[-1]`) -- never a full schema-valid fixture,
    since `compute_delta()` itself never validates its `previous.candidates`
    input against the canonical schema (Issue #2237 P0-4)."""
    return {
        "finding_contract": {
            "identity": {"value": identity_value},
            "evaluations": [
                {
                    "presence_delta": "new",
                    "evidence_refs": evidence_refs,
                }
            ],
        }
    }


_RUNTIME_UNAVAILABLE_COVERAGE = {
    "claude_code": {"status": "unavailable", "reason_code": "source_not_present", "selected_session_count": None},
    "claude_gpt": {"status": "unavailable", "reason_code": "source_not_present", "selected_session_count": None},
}
_RUNTIME_COMPLETE_ZERO_OBSERVED_COVERAGE = {
    "claude_code": {"status": "observed", "reason_code": None, "selected_session_count": 0},
    "claude_gpt": {"status": "observed", "reason_code": None, "selected_session_count": 0},
}
_RUNTIME_NOT_REQUESTED_COVERAGE = {
    "claude_code": {"status": "not_requested", "reason_code": None, "selected_session_count": None},
    "claude_gpt": {"status": "not_requested", "reason_code": None, "selected_session_count": None},
}


def test_ac2_runtime_dependent_finding_stays_indeterminate_when_current_run_missed_it():
    # previous available (source_coverage complete), this run has no
    # candidate for this identity (never reported), AND this run's OWN
    # session-source coverage never observed the runtime source this
    # finding's evidence depends on -- must NOT be synthesized as "resolved".
    previous_candidate = _last_evaluation_only_candidate(
        identity_value="finding-runtime-1", evidence_refs=[_runtime_evidence_ref()]
    )
    previous = rr.PreviousStateResult(
        status="available", previous_run_ref="run-0", candidates=[previous_candidate], read_version="v1"
    )
    delta = rr.compute_delta(previous, [], current_source_coverage=_RUNTIME_UNAVAILABLE_COVERAGE)
    assert delta == [
        {
            "finding_identity": "finding-runtime-1",
            "evaluation_status": "indeterminate",
            "delta_status": None,
            "indeterminate_reason": "source_partial",
        }
    ]
    # never the new delta_status enum value being introduced -- reuses the
    # EXISTING indeterminate representation (delta_status stays None/absent)
    assert all(entry.get("delta_status") is None for entry in delta)


def test_ac2_default_current_source_coverage_is_backward_compatible_resolved():
    # current_source_coverage omitted (the pre-#2644 default) never changes
    # this function's pre-existing behavior -- a runtime-dependent finding
    # absent this run still resolves exactly as before.
    previous_candidate = _last_evaluation_only_candidate(
        identity_value="finding-runtime-1", evidence_refs=[_runtime_evidence_ref()]
    )
    previous = rr.PreviousStateResult(
        status="available", previous_run_ref="run-0", candidates=[previous_candidate], read_version="v1"
    )
    delta = rr.compute_delta(previous, [])
    assert delta == [
        {"finding_identity": "finding-runtime-1", "evaluation_status": "classified", "delta_status": "resolved"}
    ]


def test_ac3_repository_only_finding_still_resolves_under_runtime_unavailable():
    # an UNRELATED repository-only finding must be evaluated normally even
    # though this run's runtime session-source coverage is degraded --
    # runtime coverage unavailability is never applied as a blanket rule.
    previous_candidate = _last_evaluation_only_candidate(
        identity_value="finding-repo-1", evidence_refs=[_repository_evidence_ref()]
    )
    previous = rr.PreviousStateResult(
        status="available", previous_run_ref="run-0", candidates=[previous_candidate], read_version="v1"
    )
    delta = rr.compute_delta(previous, [], current_source_coverage=_RUNTIME_UNAVAILABLE_COVERAGE)
    assert delta == [
        {"finding_identity": "finding-repo-1", "evaluation_status": "classified", "delta_status": "resolved"}
    ]


def test_ac8_coverage_complete_zero_observed_is_distinct_from_ac2_and_does_not_resolve():
    # BOTH runtime sources report status "observed" (coverage genuinely
    # complete this run -- the collector pipeline itself worked correctly)
    # with selected_session_count 0 ("coverage complete + zero observed",
    # Issue #2644 AC8). This is DISTINGUISHABLE at the coverage/orchestration
    # layer from AC2's genuinely "unavailable" input (asserted below) -- but
    # PR #2660 fix_delta P1-3 (OWNER REQUEST_CHANGES): that distinction alone
    # does not make "coverage complete + zero observed" sufficient evidence
    # that a previously-reported runtime-dependent finding was actually
    # fixed. This run gathered ZERO actual session evidence either way, so
    # "no candidate reported this run" carries exactly as little resolution
    # signal as AC2's unavailable case -- the finding must stay
    # `indeterminate`, never auto-`resolved` purely because the collector
    # itself reported a clean "observed" status.
    for source_id in ("claude_code", "claude_gpt"):
        zero_observed_entry = _RUNTIME_COMPLETE_ZERO_OBSERVED_COVERAGE[source_id]
        unavailable_entry = _RUNTIME_UNAVAILABLE_COVERAGE[source_id]
        assert zero_observed_entry["status"] == "observed"
        assert zero_observed_entry["selected_session_count"] == 0
        # coverage/orchestration-layer distinction from AC2 is preserved even
        # though both now gate resolution the same conservative way.
        assert unavailable_entry["status"] == "unavailable"
        assert zero_observed_entry["status"] != unavailable_entry["status"]

    previous_candidate = _last_evaluation_only_candidate(
        identity_value="finding-runtime-1", evidence_refs=[_runtime_evidence_ref()]
    )
    previous = rr.PreviousStateResult(
        status="available", previous_run_ref="run-0", candidates=[previous_candidate], read_version="v1"
    )
    delta = rr.compute_delta(previous, [], current_source_coverage=_RUNTIME_COMPLETE_ZERO_OBSERVED_COVERAGE)
    assert delta == [
        {
            "finding_identity": "finding-runtime-1",
            "evaluation_status": "indeterminate",
            "delta_status": None,
            "indeterminate_reason": "source_partial",
        }
    ]
    # never the new delta_status enum value being introduced -- reuses the
    # EXISTING indeterminate representation (delta_status stays None/absent),
    # same as AC2.
    assert all(entry.get("delta_status") is None for entry in delta)


def test_ac9_not_requested_runtime_source_does_not_gate_resolution():
    # `not_requested` (Issue #2601's existing treatment, kept unchanged by
    # Issue #2644) never counts as "unavailable this run" for the purposes
    # of this gating -- a caller that deliberately narrowed
    # --session-sources is not the same as a source that WAS requested but
    # failed to observe.
    previous_candidate = _last_evaluation_only_candidate(
        identity_value="finding-runtime-1", evidence_refs=[_runtime_evidence_ref()]
    )
    previous = rr.PreviousStateResult(
        status="available", previous_run_ref="run-0", candidates=[previous_candidate], read_version="v1"
    )
    delta = rr.compute_delta(previous, [], current_source_coverage=_RUNTIME_NOT_REQUESTED_COVERAGE)
    assert delta == [
        {"finding_identity": "finding-runtime-1", "evaluation_status": "classified", "delta_status": "resolved"}
    ]


def test_ac4_classify_current_candidate_delta_matches_top_level_compute_delta_for_present_identity():
    # Issue #2644 AC4: for a candidate ACTUALLY reported this run (present),
    # `_classify_current_candidate_delta()` (the canonical
    # `finding_contract.evaluations[]` generation path) and the top-level
    # `compute_delta()` call (the `PublishRequest.delta_results` sidecar)
    # must agree when given the SAME `current_source_coverage` -- no drift
    # between the two entry points.
    present_runtime_candidate = _last_evaluation_only_candidate(
        identity_value="finding-runtime-present", evidence_refs=[_runtime_evidence_ref()]
    )
    absent_runtime_candidate = _last_evaluation_only_candidate(
        identity_value="finding-runtime-absent", evidence_refs=[_runtime_evidence_ref()]
    )
    previous = rr.PreviousStateResult(
        status="available",
        previous_run_ref="run-0",
        candidates=[present_runtime_candidate, absent_runtime_candidate],
        read_version="v1",
    )
    current_batch = [
        {"finding_contract": {"identity": {"value": "finding-runtime-present"}}},
    ]
    batch_results = {
        entry["finding_identity"]: entry
        for entry in rr.compute_delta(previous, current_batch, current_source_coverage=_RUNTIME_UNAVAILABLE_COVERAGE)
    }
    singleton_result = rr._classify_current_candidate_delta(
        previous, "finding-runtime-present", current_source_coverage=_RUNTIME_UNAVAILABLE_COVERAGE
    )
    assert singleton_result == batch_results["finding-runtime-present"]
    # the ABSENT identity's own classification (indeterminate, per AC2) is
    # only ever produced by the top-level batch call -- there is no
    # candidate record for it this run to build a finding_contract.evaluations[]
    # entry from at all, so there is nothing for the per-candidate path to
    # agree/disagree with; this asserts that entry is still correctly gated.
    assert batch_results["finding-runtime-absent"] == {
        "finding_identity": "finding-runtime-absent",
        "evaluation_status": "indeterminate",
        "delta_status": None,
        "indeterminate_reason": "source_partial",
    }
