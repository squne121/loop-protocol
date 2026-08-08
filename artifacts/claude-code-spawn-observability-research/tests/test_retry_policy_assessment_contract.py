"""Issue #2013 AC4: machine verification of ``retry-policy-assessment.md``.

The assessment must (1) name the real production predicate and cross-validate
against the real repo test that fixes today's contract, (2) declare a verdict
from a closed vocabulary in a machine-readable block, (3) back that verdict
with counts recomputed from the AC2 raw ledger, and (4) explicitly reject
"it passed on re-run" as sufficient evidence of transience.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _research_contract_support import (  # noqa: E402
    AGENT_OPS_DIR,
    LANES,
    RETRY_POLICY_PATH,
    ROUTE_SMOKE_PATH,
    lane_records,
    load_records,
    valid_records,
)

VERDICTS = ("keep_excluded", "add_bounded_retry", "inconclusive")

EXISTING_CONTRACT_TEST = "test_claude_spawn_not_observed_is_not_transient_candidate"
EXISTING_CONTRACT_TEST_PATH = AGENT_OPS_DIR / "tests" / "test_agent_provider_route_smoke.py"

# A deterministic, always-reproducible extraction defect: retrying it cannot
# be justified as absorbing an infrastructure race.
DETERMINISTIC_CAUSES = {
    "tool_result_identity_not_observed",
    "agent_type_mismatch",
    "spawn_not_attempted",
    "request_validation_failed",
}


@pytest.fixture(scope="module")
def assessment_text() -> str:
    assert RETRY_POLICY_PATH.is_file(), f"missing AC4 artifact: {RETRY_POLICY_PATH}"
    text = RETRY_POLICY_PATH.read_text(encoding="utf-8")
    assert text.strip(), "retry-policy-assessment.md must not be empty"
    return text


@pytest.fixture(scope="module")
def records() -> list[dict]:
    return load_records()


def _machine_field(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*(\S+)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def test_names_the_real_production_predicate(assessment_text: str) -> None:
    assert "_is_transient_infrastructure_candidate" in assessment_text, (
        "the assessment does not name the predicate under evaluation"
    )
    source = ROUTE_SMOKE_PATH.read_text(encoding="utf-8")
    assert "def _is_transient_infrastructure_candidate(" in source, (
        "the predicate no longer exists in production; the assessment is stale"
    )
    assert 'route["runtime"] == "codex_cli"' in source and '"spawn_not_observed"' in source, (
        "the production predicate's current shape differs from the one assessed"
    )


def test_cross_validates_the_existing_repo_test_contract(assessment_text: str) -> None:
    """The assessment cites the existing test that pins today's behaviour;
    that test must genuinely exist in the repo."""
    assert EXISTING_CONTRACT_TEST in assessment_text, (
        f"the assessment does not cite {EXISTING_CONTRACT_TEST}"
    )
    assert EXISTING_CONTRACT_TEST_PATH.is_file(), EXISTING_CONTRACT_TEST_PATH
    body = EXISTING_CONTRACT_TEST_PATH.read_text(encoding="utf-8")
    assert f"def {EXISTING_CONTRACT_TEST}(" in body, (
        f"{EXISTING_CONTRACT_TEST} is cited but does not exist in "
        f"{EXISTING_CONTRACT_TEST_PATH}"
    )


def test_declares_a_verdict_from_the_closed_vocabulary(assessment_text: str) -> None:
    verdict = _machine_field(assessment_text, "retry_policy_verdict")
    assert verdict is not None, (
        "retry-policy-assessment.md carries no machine-readable "
        "`retry_policy_verdict: <value>` line"
    )
    assert verdict in VERDICTS, f"verdict {verdict!r} is outside {VERDICTS}"


def test_records_the_observed_distribution_that_backs_the_verdict(
    assessment_text: str, records: list[dict]
) -> None:
    """Per-lane counts of ``spawn_not_observed`` must be stated and must match
    the raw ledger -- the verdict cannot rest on unstated numbers."""
    for lane in LANES:
        rows = lane_records(valid_records(records), lane)
        count = sum(1 for r in rows if r["failure_class"] == "spawn_not_observed")
        field = _machine_field(assessment_text, f"{lane}_spawn_not_observed_count")
        assert field is not None, (
            f"the assessment does not state `{lane}_spawn_not_observed_count`"
        )
        assert field == str(count), (
            f"{lane}_spawn_not_observed_count={field} contradicts the raw ledger ({count})"
        )
        total_field = _machine_field(assessment_text, f"{lane}_trial_count")
        assert total_field == str(len(rows)), (
            f"{lane}_trial_count={total_field} contradicts the raw ledger ({len(rows)})"
        )


def test_verdict_is_consistent_with_the_observed_diagnostic_causes(
    assessment_text: str, records: list[dict]
) -> None:
    """If every ``spawn_not_observed`` trial has a deterministic diagnostic
    cause, a bounded retry is not justifiable and the verdict must be
    ``keep_excluded``."""
    rows = [
        r for r in valid_records(records)
        if r["failure_class"] == "spawn_not_observed"
    ]
    if not rows:
        pytest.skip("no spawn_not_observed trials in the ledger")
    causes = {r["diagnostic_cause"] for r in rows}
    verdict = _machine_field(assessment_text, "retry_policy_verdict")
    if causes <= DETERMINISTIC_CAUSES:
        assert verdict == "keep_excluded", (
            f"all spawn_not_observed causes are deterministic ({sorted(causes)}) yet the "
            f"verdict is {verdict!r}"
        )


def test_rejects_rerun_success_as_transience_evidence(assessment_text: str) -> None:
    assert "再実行したら通った" in assessment_text, (
        "the assessment does not address the 'it passed on re-run' argument"
    )
    assert "transient" in assessment_text.lower()


def test_states_whether_the_current_design_is_consistent_with_observation(
    assessment_text: str,
) -> None:
    """AC4 asks specifically whether excluding ``claude_code`` +
    ``spawn_not_observed`` from bounded retry matches the observed data."""
    assert "claude_code" in assessment_text and "spawn_not_observed" in assessment_text
    consistency = _machine_field(assessment_text, "current_design_consistent_with_observation")
    assert consistency in ("yes", "no", "partially"), (
        "the assessment carries no machine-readable "
        "`current_design_consistent_with_observation: yes|no|partially` line"
    )


def test_hook_channel_evidence_is_taken_into_account(
    assessment_text: str, records: list[dict]
) -> None:
    """The decisive fact -- identity evidence exists in the hook channel even
    when the tool_result channel lacks it -- must be part of the assessment,
    and must be true in the ledger."""
    rows = [
        r for r in valid_records(records)
        if r["lifecycle"]["tool_result_agent_type_observed"] is False
        and r["lifecycle"]["agent_tool_use_observed"] is True
    ]
    if rows:
        with_hook_identity = sum(1 for r in rows if r["hook_agent_type_observed"])
        field = _machine_field(assessment_text, "hook_identity_available_when_tool_result_missing")
        assert field is not None, (
            "the assessment does not state "
            "`hook_identity_available_when_tool_result_missing`"
        )
        assert field == str(with_hook_identity), (
            f"hook_identity_available_when_tool_result_missing={field} contradicts the "
            f"raw ledger ({with_hook_identity})"
        )


def test_no_silent_or_unbounded_retry_is_proposed(assessment_text: str) -> None:
    """Issue #2013 Out of Scope forbids silent retry, 2+ retries, and
    retry-until-success."""
    assert "silent retry" in assessment_text or "silent" in assessment_text
    for forbidden in ("成功するまで", "無制限"):
        if forbidden in assessment_text:
            index = assessment_text.index(forbidden)
            context = assessment_text[max(0, index - 120): index + 120]
            assert any(word in context for word in ("禁止", "しない", "提案しない")), (
                f"{forbidden!r} appears without being rejected: {context!r}"
            )
