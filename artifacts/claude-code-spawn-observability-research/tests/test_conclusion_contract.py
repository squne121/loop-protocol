"""Issue #2013 AC5: machine verification of ``conclusion.md``.

The conclusion must be a single category from the Issue's closed vocabulary,
declared machine-readably together with the bounded-retry decision, the
failure-class subdivision decision, and the follow-up implementation issue --
and it must be *recomputable* from the AC2 raw ledger rather than asserted.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _research_contract_support import (  # noqa: E402
    CONCLUSION_CATEGORIES,
    CONCLUSION_PATH,
    LANES,
    lane_records,
    load_records,
    valid_records,
)

BOOLEAN_FIELDS = ("bounded_single_retry_applicable", "additional_failure_class_subdivision_required")


@pytest.fixture(scope="module")
def conclusion_text() -> str:
    assert CONCLUSION_PATH.is_file(), f"missing AC5 artifact: {CONCLUSION_PATH}"
    text = CONCLUSION_PATH.read_text(encoding="utf-8")
    assert text.strip(), "conclusion.md must not be empty"
    return text


@pytest.fixture(scope="module")
def records() -> list[dict]:
    return load_records()


def _machine_field(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*(\S+)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def test_declares_exactly_one_category_from_the_issue_vocabulary(conclusion_text: str) -> None:
    category = _machine_field(conclusion_text, "conclusion_category")
    assert category is not None, (
        "conclusion.md carries no machine-readable `conclusion_category: <value>` line"
    )
    assert category in CONCLUSION_CATEGORIES, (
        f"conclusion_category {category!r} is not one of the Issue's categories "
        f"{CONCLUSION_CATEGORIES}"
    )


def test_no_invented_conclusion_categories(conclusion_text: str) -> None:
    """Guard against silently widening the vocabulary: every value assigned to
    ``conclusion_category`` anywhere in the file must be a known category."""
    for value in re.findall(r"^conclusion_category:\s*(\S+)\s*$", conclusion_text, re.MULTILINE):
        assert value in CONCLUSION_CATEGORIES, f"invented conclusion category {value!r}"


def test_declares_the_bounded_retry_and_subdivision_decisions(conclusion_text: str) -> None:
    for key in BOOLEAN_FIELDS:
        value = _machine_field(conclusion_text, key)
        assert value is not None, f"conclusion.md carries no machine-readable `{key}:` line"
        assert value in ("yes", "no"), f"{key} must be yes|no, got {value!r}"


def test_records_the_follow_up_implementation_issue(conclusion_text: str) -> None:
    value = _machine_field(conclusion_text, "follow_up_implementation_issue")
    assert value is not None, (
        "conclusion.md does not record whether a follow-up implementation issue was filed"
    )
    assert value == "none" or re.fullmatch(r"#\d+", value), (
        f"follow_up_implementation_issue must be `none` or `#<number>`, got {value!r}"
    )


def test_follow_up_issue_is_filed_when_a_repo_defect_is_concluded(conclusion_text: str) -> None:
    """Issue #2013 requires that a repo-side code defect be split out into a
    separate implementation issue rather than fixed in this research branch."""
    category = _machine_field(conclusion_text, "conclusion_category")
    follow_up = _machine_field(conclusion_text, "follow_up_implementation_issue")
    if category == "repo_observability_defect":
        assert follow_up and follow_up != "none", (
            "a repo_observability_defect conclusion must record a follow-up implementation issue"
        )


def test_conclusion_is_recomputable_from_the_raw_ledger(
    conclusion_text: str, records: list[dict]
) -> None:
    """Independent recomputation of the decisive signal.

    If, across the failing trials, the spawn was in fact dispatched and both
    runtime channels agree on the child ``agentId`` while only the repo-side
    ``tool_use_result.agentType`` extraction is missing, then the defect is in
    the repository's observation path -- not the runtime, not infrastructure
    timing, and not the downstream route.
    """
    failing = [r for r in valid_records(records) if r["status"] != "pass"]
    if not failing:
        pytest.skip("no failing trials to explain")
    repo_observation_gap = [
        r for r in failing
        if r["lifecycle"]["agent_tool_use_observed"]
        and r["lifecycle"]["tool_result_observed"]
        and r["lifecycle"]["tool_result_agent_id_observed"]
        and not r["lifecycle"]["tool_result_agent_type_observed"]
        and bool(r["hook_agent_type_observed"])
        and r["cross_channel_identity_agreement"]["agent_id_channels_agree"]
    ]
    category = _machine_field(conclusion_text, "conclusion_category")
    if len(repo_observation_gap) > len(failing) / 2:
        assert category == "repo_observability_defect", (
            f"{len(repo_observation_gap)}/{len(failing)} failing trials show a repo-side "
            f"observation gap with runtime-supplied identity evidence, but the recorded "
            f"category is {category!r}"
        )


def test_downstream_failures_are_not_promoted_to_the_conclusion(
    conclusion_text: str, records: list[dict]
) -> None:
    """``downstream_route_failure`` may only be the conclusion if downstream
    causes actually dominate the failures."""
    category = _machine_field(conclusion_text, "conclusion_category")
    failing = [r for r in valid_records(records) if r["status"] != "pass"]
    if category == "downstream_route_failure" and failing:
        downstream = sum(
            1 for r in failing
            if r["diagnostic_cause"] in ("downstream_route_failed", "delegation_wrapper_failed")
        )
        assert downstream > len(failing) / 2, (
            f"downstream_route_failure concluded, but only {downstream}/{len(failing)} "
            "failing trials have a downstream cause"
        )


def test_transient_conclusion_requires_transient_evidence(
    conclusion_text: str, records: list[dict]
) -> None:
    """``transient_infrastructure`` may not be concluded when every failure has
    a deterministic cause."""
    category = _machine_field(conclusion_text, "conclusion_category")
    if category != "transient_infrastructure":
        return
    failing = [r for r in valid_records(records) if r["status"] != "pass"]
    transient_causes = {"runtime_api_retry_timeout", "subagent_completion_timeout"}
    assert any(r["diagnostic_cause"] in transient_causes for r in failing), (
        "transient_infrastructure concluded with no timeout/api-retry evidence in the ledger"
    )


def test_conclusion_cites_the_trial_counts_it_rests_on(
    conclusion_text: str, records: list[dict]
) -> None:
    for lane in LANES:
        rows = lane_records(valid_records(records), lane)
        field = _machine_field(conclusion_text, f"{lane}_trial_count")
        assert field == str(len(rows)), (
            f"conclusion.md records {lane}_trial_count={field}, ledger has {len(rows)}"
        )


def test_conclusion_records_both_shas_and_the_runtime_version(
    conclusion_text: str, records: list[dict]
) -> None:
    assert "28394e226533cd59cdfc0f55602ac65e389a6600" in conclusion_text, (
        "historical baseline SHA is not recorded"
    )
    for sha in {r["tested_head_sha"] for r in records}:
        assert sha in conclusion_text, f"actual tested SHA {sha} is not recorded"
    for version in {r["claude_code_version"] for r in records}:
        assert version in conclusion_text, f"Claude Code version {version!r} is not recorded"


def test_existing_failure_class_schema_is_declared_unchanged(conclusion_text: str) -> None:
    assert "spawn_not_observed" in conclusion_text and "validation_failed" in conclusion_text
    assert _machine_field(conclusion_text, "existing_failure_class_schema_changed") == "no", (
        "conclusion.md must declare `existing_failure_class_schema_changed: no`"
    )
