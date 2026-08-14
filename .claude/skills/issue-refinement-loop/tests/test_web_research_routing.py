"""Regression coverage for disposition-aware web research routing (#1828)."""

from __future__ import annotations

import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from route_web_research_result import (  # noqa: E402
    NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED,
    NEXT_ACTION_PROCEED,
    NEXT_ACTION_PROCEED_WITH_NOTES,
    TRANSPORT_STATUS_ENVIRONMENT_FAILURE,
    route_web_research_result,
)


def _input(
    *,
    repository_status: str,
    disposition: str | None,
    role: str,
    web_research: object,
) -> dict[str, object]:
    return {
        "schema": "WEB_RESEARCH_ROUTING_INPUT_V1",
        "repository_decision": {
            "status": repository_status,
            "disposition": disposition,
        },
        "critical_external_claims": [
            {
                "claim": "historical/current CLI hook semantics",
                "affects": "Outcome",
                "source_hint": "issuecomment-5267301558",
                "role": role,
            }
        ],
        "web_research": web_research,
    }


def _web_result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "failed",
        "failure_class": "query_error",
        "verification_route": "grounded_research",
        "claims": [],
        "unresolved_risks": [],
    }
    result.update(overrides)
    return result


def _successful_web_result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "status": "ok",
        "failure_class": None,
        "verification_route": "grounded_research",
        "claims": [
            {
                "claim_id": "hook-semantics",
                "text": "historical/current CLI hook semantics",
                "type": "external_spec",
                "critical": True,
                "verdict": "supported",
                "evidence": [
                    {
                        "kind": "web",
                        "ref": "https://example.invalid/hook-semantics",
                        "summary": "Grounded external evidence was materialized.",
                    }
                ],
            }
        ],
        "unresolved_risks": [],
    }
    result.update(overrides)
    return result


def test_non_dispositive_grounding_failure_preserves_repository_close_route():
    """Case A: #1828-style repository close survives unavailable grounding."""
    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=_web_result(
                failure_class="agy_web_grounding_tool_call_missing",
                retry_count=1,
            ),
        )
    )

    assert result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE
    assert result["semantic_verdict"] is None
    assert result["next_action"] == NEXT_ACTION_PROCEED_WITH_NOTES
    assert result["repository_disposition"] == "close_not_planned"
    assert result["unresolved_risks"] == [
        "external_evidence_unavailable:agy_web_grounding_tool_call_missing"
    ]


def test_dispositive_external_failure_remains_fail_closed():
    """Case B: only external authority unresolved still requires human judgment."""
    result = route_web_research_result(
        _input(
            repository_status="inconclusive",
            disposition=None,
            role="dispositive",
            web_research=_web_result(status="inconclusive"),
        )
    )

    assert result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE
    assert result["semantic_verdict"] is None
    assert result["next_action"] == NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED
    assert "dispositive_external_evidence_unresolved" in result["reason_codes"]


def test_transport_failure_without_semantic_result_is_not_semantic_disagreement():
    """Case C: malformed/empty transport stays environment failure with null verdict."""
    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=None,
        )
    )

    assert result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE
    assert result["semantic_verdict"] is None
    assert result["next_action"] == NEXT_ACTION_PROCEED_WITH_NOTES
    assert result["reason_codes"][0] == "web_research_result_missing_or_malformed"


def test_retry_budget_is_not_reimplemented_by_consumer():
    """A completed bounded retry remains a non-dispositive note, not a global stop."""
    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=_web_result(
                failure_class="capability_unavailable",
                retry_count=2,
                fallback_used=False,
            ),
        )
    )

    assert result["next_action"] == NEXT_ACTION_PROCEED_WITH_NOTES
    assert result["reason_codes"] == [
        "external_evidence_unavailable:capability_unavailable",
        "repository_decision_independent",
    ]


def test_incomplete_ok_result_is_fail_closed_transport_failure():
    """A claimed success without the result contract is not usable external evidence."""
    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research={"status": "ok"},
        )
    )

    assert result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE
    assert result["semantic_verdict"] is None
    assert result["next_action"] == NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED
    assert result["reason_codes"] == ["web_research_result_missing_or_malformed"]


def test_complete_ok_result_requires_matching_materialized_claim_evidence():
    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=_successful_web_result(),
        )
    )

    assert result["transport_status"] == "ok"
    assert result["semantic_verdict"] is None
    assert result["next_action"] == NEXT_ACTION_PROCEED


def test_ok_result_with_native_web_verification_route_is_accepted():
    """Producer-added successful routes (e.g. native_web) are not allowlist-rejected."""
    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=_successful_web_result(verification_route="native_web"),
        )
    )

    assert result["transport_status"] == "ok"
    assert result["semantic_verdict"] is None
    assert result["next_action"] == NEXT_ACTION_PROCEED


def test_ok_result_with_unknown_future_verification_route_is_accepted():
    """The consumer does not re-implement/re-enumerate the producer's route enum."""
    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=_successful_web_result(verification_route="some_future_provider_route"),
        )
    )

    assert result["transport_status"] == "ok"
    assert result["semantic_verdict"] is None
    assert result["next_action"] == NEXT_ACTION_PROCEED


def test_ok_result_with_empty_verification_route_is_fail_closed_transport_failure():
    """An `ok` status still requires the producer to have named some route."""
    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=_successful_web_result(verification_route=""),
        )
    )

    assert result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE
    assert result["semantic_verdict"] is None
    assert result["next_action"] == NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED


def test_ok_result_with_empty_claims_is_fail_closed_transport_failure():
    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=_successful_web_result(claims=[]),
        )
    )

    assert result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE
    assert result["semantic_verdict"] is None
    assert result["next_action"] == NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED


def test_ok_result_with_invalid_claim_is_fail_closed_transport_failure():
    result = route_web_research_result(
        _input(
            repository_status="determined",
            disposition="close_not_planned",
            role="non_dispositive",
            web_research=_successful_web_result(claims=[{}]),
        )
    )

    assert result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE
    assert result["semantic_verdict"] is None
    assert result["next_action"] == NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED


def test_empty_claim_roles_are_fail_closed_even_with_repository_disposition():
    """An unbound role list cannot be treated as all non-dispositive."""
    input_data = _input(
        repository_status="determined",
        disposition="close_not_planned",
        role="non_dispositive",
        web_research=_web_result(),
    )
    input_data["critical_external_claims"] = []

    result = route_web_research_result(input_data)

    assert result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE
    assert result["semantic_verdict"] is None
    assert result["next_action"] == NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED
    assert result["reason_codes"] == ["critical_external_claims must not be empty"]


def test_missing_claim_role_is_fail_closed_even_with_repository_disposition():
    """A claim without an explicit decision dependency cannot proceed on defaults."""
    input_data = _input(
        repository_status="determined",
        disposition="close_not_planned",
        role="non_dispositive",
        web_research=_web_result(),
    )
    claim = input_data["critical_external_claims"][0]
    del claim["role"]

    result = route_web_research_result(input_data)

    assert result["transport_status"] == TRANSPORT_STATUS_ENVIRONMENT_FAILURE
    assert result["semantic_verdict"] is None
    assert result["next_action"] == NEXT_ACTION_HUMAN_JUDGMENT_REQUIRED
    assert result["reason_codes"] == [
        "critical_external_claim role must be dispositive or non_dispositive"
    ]


def test_documentation_declares_disposition_aware_transport_routing():
    """AC6: the skill and reference keep the non-global-blocker contract visible."""
    routing_reference = (SKILL_ROOT / "references" / "web-research-routing.md").read_text(
        encoding="utf-8"
    )
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "non_dispositive" in routing_reference
    assert "environment_failure" in routing_reference
    assert "proceed_with_notes" in skill
    assert "semantic_verdict: null" in skill
