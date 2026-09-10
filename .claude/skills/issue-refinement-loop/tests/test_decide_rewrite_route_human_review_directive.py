"""
test_decide_rewrite_route_human_review_directive.py

Issue #2620: routes an explicit trusted `human_review_directive` (freeform
human-context comment, NOT a structured `ANCHOR_SCOPE_REFRAME_V1` payload)
with no safe section-bound patch representation to `issue_editor_required`,
writes=0 -- via the SSOT `decide_rewrite_route.
decide_human_review_directive_editor_route()` and its
`run_refinement_preflight.consume_trusted_anchor_contract_patch_plan()`
wiring.

Covers AC1 (positive eligibility + canonical reviewer_feedback_url
propagation, never raw anchor body), AC2 (no patch plan fabrication), and
AC3/AC4 (every individual existing-predicate failure keeps the route
`None` / fail-closed).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from decide_rewrite_route import (  # noqa: E402
    ROUTE_ISSUE_EDITOR_REQUIRED,
    REASON_EXPLICIT_TRUSTED_HUMAN_DIRECTIVE_REQUIRES_ISSUE_EDITOR,
    HUMAN_REVIEW_DIRECTIVE_EDITOR_ROUTE_STATE_V1,
    decide_human_review_directive_editor_route,
)

import run_refinement_preflight as preflight  # noqa: E402

REPO = "squne121/loop-protocol"
ISSUE_NUMBER = 2620


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# AC1/AC2: decide_human_review_directive_editor_route() -- the SSOT unit
# ---------------------------------------------------------------------------


def _eligible_state(**overrides) -> HUMAN_REVIEW_DIRECTIVE_EDITOR_ROUTE_STATE_V1:
    base = dict(
        authority_category="human_review_directive",
        directive_confidence="explicit",
        route_action="contract_update_required",
        with_human_context=True,
        anchor_binding_ok=True,
        same_target_ok=True,
        operations_empty=True,
        is_structured_scope_reframe=False,
        reviewer_feedback_url=f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#issuecomment-1",
    )
    base.update(overrides)
    return HUMAN_REVIEW_DIRECTIVE_EDITOR_ROUTE_STATE_V1(**base)


def test_ac1_all_existing_predicates_satisfied_routes_to_issue_editor_required():
    """GIVEN every existing-predicate fact holds (explicit trusted
    human_review_directive, with_human_context, valid anchor/target
    binding, no safe section-bound patch, not a structured scope reframe)
    WHEN decide_human_review_directive_editor_route() runs
    THEN it returns issue_editor_required, writes=0 (implicit -- this
    function never authorizes a write), and echoes the canonical
    reviewer_feedback_url unchanged.
    """
    url = f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#issuecomment-999"
    result = decide_human_review_directive_editor_route(_eligible_state(reviewer_feedback_url=url))

    assert result.route == ROUTE_ISSUE_EDITOR_REQUIRED
    assert result.reason_code == REASON_EXPLICIT_TRUSTED_HUMAN_DIRECTIVE_REQUIRES_ISSUE_EDITOR
    assert result.reviewer_feedback_url == url
    as_dict = result.to_dict()
    assert as_dict["route"] == "issue_editor_required"
    assert as_dict["reviewer_feedback_url"] == url


def test_ac2_no_patch_plan_is_fabricated_when_no_safe_representation_exists():
    """AC2: the route result never carries operations/a patch plan of its
    own -- it is a pure routing decision (route/reason_code/
    reviewer_feedback_url only)."""
    result = decide_human_review_directive_editor_route(_eligible_state())
    as_dict = result.to_dict()

    assert set(as_dict.keys()) == {"schema_version", "route", "reason_code", "reviewer_feedback_url"}
    assert "operations" not in as_dict
    assert "contract_patch_plan" not in as_dict


@pytest.mark.parametrize(
    "override,label",
    [
        ({"authority_category": "ai_inferred"}, "wrong_authority_category"),
        ({"authority_category": None}, "missing_authority_category"),
        ({"directive_confidence": "ambiguous"}, "non_explicit_confidence"),
        ({"directive_confidence": "inferred"}, "inferred_confidence"),
        ({"route_action": "human_escalation"}, "wrong_route_action"),
        ({"with_human_context": False}, "not_human_context_lane"),
        ({"anchor_binding_ok": False}, "anchor_binding_mismatch"),
        ({"same_target_ok": False}, "target_mismatch"),
        ({"operations_empty": False}, "safe_patch_representation_exists"),
        ({"is_structured_scope_reframe": True}, "structured_scope_reframe_owns_this"),
        ({"reviewer_feedback_url": None}, "missing_reviewer_feedback_url"),
        ({"reviewer_feedback_url": ""}, "blank_reviewer_feedback_url"),
    ],
)
def test_ac3_ac4_any_single_failing_predicate_never_routes_to_issue_editor_required(override, label):
    """AC3/AC4: untrusted / non-explicit / mismatched-binding / already-
    section-bound / structured-scope-reframe-owned / missing-URL inputs
    all fail closed -- route stays None (never issue_editor_required),
    never a fabricated body rewrite."""
    result = decide_human_review_directive_editor_route(_eligible_state(**override))

    assert result.route is None, label
    assert result.reason_code is None, label
    assert result.reviewer_feedback_url is None, label
    assert result.to_dict()["route"] is None, label


def test_ac5_regression_ordinary_eligible_state_is_the_only_positive_case():
    """AC5 sanity: flipping every negative-predicate case back to the
    eligible baseline reaches issue_editor_required again -- the negative
    matrix above is not accidentally always-false."""
    result = decide_human_review_directive_editor_route(_eligible_state())
    assert result.route == ROUTE_ISSUE_EDITOR_REQUIRED


# ---------------------------------------------------------------------------
# AC1/AC2: consume_trusted_anchor_contract_patch_plan() wiring -- the real
# production consumer (not a reimplementation), called directly with
# crafted known_context/patch_plan/anchor identity, mirroring the existing
# regression pattern in test_preflight_run_with_anchor.py.
# ---------------------------------------------------------------------------

_ANCHOR_COMMENT_ID = 9001
_ANCHOR_URL = f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#issuecomment-{_ANCHOR_COMMENT_ID}"
_ANCHOR_BODY = (
    "Please restructure the onboarding walkthrough narrative so new "
    "contributors are not dropped mid-flow.\n\n"
    "- Please restructure the onboarding walkthrough narrative to add "
    "clarifying context for new contributors.\n"
)
_ISSUE_BODY = "## Acceptance Criteria\n\n- [ ] AC1: existing\n"


def _consumer_kwargs(
    *,
    anchor_body: str = _ANCHOR_BODY,
    anchor_url: str = _ANCHOR_URL,
    issue_body: str = _ISSUE_BODY,
    author_association: str = "OWNER",
    human_context_comment_urls=(_ANCHOR_URL,),
    evidence_anchor_body: "str | None" = None,
    known_context_override: "dict | None" = None,
):
    anchor_payload = {
        "id": _ANCHOR_COMMENT_ID,
        "user": {"login": "squne121", "type": "User"},
        "author_association": author_association,
    }
    if known_context_override is not None:
        known_context = known_context_override
    else:
        # Mirror run_preflight()'s own upstream wiring: build the freeform
        # SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 exactly as
        # _build_scope_delta_authority_evidence() would, from the SAME
        # comment body the evidence is normally captured from (this may
        # deliberately differ from `anchor_body` to simulate a TOCTOU drift
        # -- see `evidence_anchor_body`).
        evidence = preflight._build_scope_delta_authority_evidence(
            comment_payload=anchor_payload,
            comment_body=evidence_anchor_body if evidence_anchor_body is not None else anchor_body,
            repo=REPO,
            issue_number=ISSUE_NUMBER,
            anchor_url=anchor_url,
            captured_at="2026-09-10T00:00:00Z",
            human_context_comment_urls=list(human_context_comment_urls),
            agent_report_comment_urls=None,
        )
        known_context = {"human_context_comment_urls": list(human_context_comment_urls)}
        if evidence is not None:
            known_context["scope_delta_authority_evidence"] = [evidence]
    return dict(
        repo=REPO,
        issue_number=ISSUE_NUMBER,
        issue={"body": issue_body, "updatedAt": "2026-09-10T00:00:00Z"},
        anchor_url=anchor_url,
        anchor_payload=anchor_payload,
        anchor_body=anchor_body,
        contract_patch_plan={"operations": []},
        callbacks={},
        known_context=known_context,
    )


def test_ac1_consumer_reaches_issue_editor_required_for_freeform_explicit_directive():
    """AC1: the real production consumer, given a freeform (non-structured)
    explicit trusted human_review_directive with no derivable section-bound
    operations, returns a rewrite_route of issue_editor_required with
    writes=0 and the canonical reviewer_feedback_url (the anchor comment
    URL) -- never the raw anchor_comment.snapshot body text."""
    kwargs = _consumer_kwargs()
    result = preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["writes"] == 0
    assert result["rewrite_route"]["route"] == "issue_editor_required"
    assert result["reviewer_feedback_url"] == _ANCHOR_URL
    # AC1: never the raw anchor comment body forwarded as feedback text.
    assert result["reviewer_feedback_url"] != _ANCHOR_BODY
    assert "reviewer_feedback_text" not in result


def test_ac3_untrusted_author_association_never_escalates():
    """AC3: an untrusted author association (NONE) never reaches
    issue_editor_required -- fails closed via the existing
    classify_scope_delta_authority() untrusted-author gate."""
    kwargs = _consumer_kwargs(author_association="NONE")
    result = preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result.get("rewrite_route") is None
    assert result.get("status") != "handoff_required"


def test_ac3_anchor_body_mismatch_never_escalates():
    """AC3: a stale/mismatched anchor body (the evidence was captured from
    a DIFFERENT body than the one this transaction boundary is now
    operating on -- a TOCTOU drift) never escalates -- the fresh
    anchor_binding_ok re-check fails closed."""
    drifted_body = _ANCHOR_BODY + "\nEdited after evidence capture.\n"
    kwargs = _consumer_kwargs(anchor_body=drifted_body, evidence_anchor_body=_ANCHOR_BODY)
    result = preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result.get("rewrite_route") is None
    assert result.get("status") != "handoff_required"


def test_ac4_ambiguous_no_bullet_directive_never_escalates():
    """AC4: a freeform human-context comment with no bullet-list directive
    content (no imperative ask) never reaches explicit confidence, so it
    never escalates to issue_editor_required -- stays fail-closed."""
    prose_only_body = "This section could probably be clearer at some point."
    kwargs = _consumer_kwargs(anchor_body=prose_only_body)
    result = preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result.get("rewrite_route") is None
    assert result.get("status") != "handoff_required"


def test_ac4_not_human_context_lane_never_escalates():
    """AC4: the SAME explicit directive text, but the anchor URL is NOT on
    the `with_human_context` lane (human_context_comment_urls omits it) --
    never escalates. Directive confidence itself degrades to non-explicit
    without the with_human_context relaxation, matching the existing
    `classify_directive_confidence()` behavior."""
    kwargs = _consumer_kwargs(human_context_comment_urls=())
    result = preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result.get("rewrite_route") is None
    assert result.get("status") != "handoff_required"


def test_ac5_regression_non_empty_operations_freeform_directive_stays_ordinary_patch_route():
    """AC5 regression: a safe section-bound patch representation (non-empty
    operations[]) must still take the ordinary section-bound
    apply_transaction path -- the new #2620 branch (which only fires when
    operations[] is empty) must never shadow priority 1."""
    applied = {"calls": 0}
    state = {"body": _ISSUE_BODY}

    def _apply_transaction(current_issue, candidate_body, readiness):
        applied["calls"] += 1
        state["body"] = candidate_body
        return {"status": "applied"}

    def _fetch_current():
        return (
            {"body": state["body"], "updatedAt": "2026-09-10T00:00:00Z"},
            {
                "id": _ANCHOR_COMMENT_ID,
                "html_url": _ANCHOR_URL,
                "body": _ANCHOR_BODY,
                "author_association": "OWNER",
            },
        )

    def _candidate_readiness(_candidate_body):
        return {
            "status": "go",
            "body_sha256": "sha256:candidate",
            "source_checks": [],
            "errors": [],
            "readiness_result_ref": "fixture",
        }

    operations = [
        {
            "section": "Acceptance Criteria",
            "op": "append",
            "text": "- AC2: freeform explicit directive text",
            "rationale": "test",
            "source_evidence_index": 0,
        }
    ]
    kwargs = _consumer_kwargs()
    kwargs["contract_patch_plan"] = {"operations": operations}
    kwargs["callbacks"] = {
        "fetch_current": _fetch_current,
        "candidate_readiness": _candidate_readiness,
        "apply_transaction": _apply_transaction,
    }
    result = preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result.get("rewrite_route") is None
    assert result.get("status") == "applied"
    assert applied["calls"] == 1
