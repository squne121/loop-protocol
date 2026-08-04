"""Issue #1979 AC1: bootstrap_prerequisite vs claim_under_test classification."""
# ruff: noqa: E501

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "preflight_agy.py"
SPEC = importlib.util.spec_from_file_location("preflight_agy_for_classification_test", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_every_capability_predicate_has_a_classification() -> None:
    """No predicate in CAPABILITY_PREDICATES is left unclassified (fail-closed)."""
    for group, predicates in MODULE.CAPABILITY_PREDICATES.items():
        for predicate in predicates:
            kind = MODULE.classify_predicate_kind(group, predicate)
            assert kind in MODULE.CAPABILITY_PREDICATE_KINDS


def test_pre_invocation_ephemeral_message_injection_is_bootstrap_prerequisite() -> None:
    """The live runner's binding target (AC1/AC2) must be a bootstrap_prerequisite."""
    assert (
        MODULE.classify_predicate_kind("hooks", "pre_invocation_ephemeral_message_injection")
        == "bootstrap_prerequisite"
    )


def test_pre_invocation_injected_tool_call_remains_bootstrap_prerequisite() -> None:
    """Retained (unrenamed) for the hermetic hook-dispatch harness's toolCall
    contract and `test_setup_check.py` -- still bootstrap_prerequisite, just
    no longer the live runner's own binding target (Issue #1979)."""
    assert MODULE.classify_predicate_kind("hooks", "pre_invocation_injected_tool_call") == "bootstrap_prerequisite"


@pytest.mark.parametrize(
    "predicate",
    ["deny_precedence_enforced", "ask_is_soft_denied_noninteractive"],
)
def test_headless_permission_policy_claim_and_bootstrap_predicates(predicate: str) -> None:
    assert MODULE.classify_predicate_kind("headless_permission_policy", predicate) == "claim_under_test"
    assert MODULE.classify_predicate_kind("headless_permission_policy", "persisted_settings_loaded") == (
        "bootstrap_prerequisite"
    )


@pytest.mark.parametrize(
    "predicate",
    ["pre_tool_use_verdict", "post_tool_use_dispatch", "post_tool_use_matcher_semantics"],
)
def test_hooks_claim_under_test_predicates_never_require_supported_pre_live(predicate: str) -> None:
    """Issue #1979 AC1: claim_under_test predicates never demand `supported`
    before a live run -- they resolve to a non-`supported` status (deferred to
    live observation) without that being treated as a blocking failure.
    """
    assert MODULE.classify_predicate_kind("hooks", predicate) == "claim_under_test"
    matrix = MODULE.build_capability_matrix(
        version_result={"status": "version_evidence_invalid", "version": None, "core": None, "raw": None}
    )
    result = MODULE.get_capability_status(matrix, "hooks", predicate)
    assert result["status"] != "supported"
    assert result["status"] in MODULE.CAPABILITY_STATUSES


def test_bootstrap_prerequisite_predicates_cover_the_live_runner_gate_target() -> None:
    bootstrap = {
        predicate
        for group, predicates in MODULE.CAPABILITY_PREDICATE_CLASSIFICATION.items()
        for predicate, kind in predicates.items()
        if kind == "bootstrap_prerequisite"
    }
    assert "pre_invocation_ephemeral_message_injection" in bootstrap
    assert "pre_invocation_injected_tool_call" in bootstrap


def test_unclassified_predicate_raises_value_error() -> None:
    with pytest.raises(ValueError):
        MODULE.classify_predicate_kind("hooks", "not_a_real_predicate")
    with pytest.raises(ValueError):
        MODULE.classify_predicate_kind("not_a_real_group", "pre_invocation_injected_tool_call")


def test_classification_groups_match_capability_predicates_keys() -> None:
    assert set(MODULE.CAPABILITY_PREDICATE_CLASSIFICATION) == set(MODULE.CAPABILITY_PREDICATES)
    for group, predicates in MODULE.CAPABILITY_PREDICATES.items():
        assert set(MODULE.CAPABILITY_PREDICATE_CLASSIFICATION[group]) == set(predicates)
