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
import decide_rewrite_route as decide_rewrite_route_module  # noqa: E402
import scope_signal_delta as scope_signal_delta_module  # noqa: E402

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


def _fetch_current_unchanged(anchor_body: str = _ANCHOR_BODY, anchor_url: str = _ANCHOR_URL):
    """PR #2623 review fix (P1 finding 2): a `fetch_current` fixture callback
    that re-reads the SAME anchor body/identity the evidence was captured
    from -- the TOCTOU-safe fresh readback `_decide_human_review_directive_
    editor_route()` now performs before authorizing a handoff. Counts
    invocations so callers can assert the fresh readback genuinely ran
    (never bypassed on a stale in-hand snapshot)."""
    calls = {"count": 0}

    def _fetch_current():
        calls["count"] += 1
        return (
            {"body": _ISSUE_BODY, "updatedAt": "2026-09-10T00:00:00Z"},
            {"id": _ANCHOR_COMMENT_ID, "html_url": anchor_url, "body": anchor_body},
        )

    return _fetch_current, calls


def test_ac1_consumer_reaches_issue_editor_required_for_freeform_explicit_directive():
    """AC1: the real production consumer, given a freeform (non-structured)
    explicit trusted human_review_directive with no derivable section-bound
    operations, returns a rewrite_route of issue_editor_required with
    writes=0 and the canonical reviewer_feedback_url (the anchor comment
    URL) -- never the raw anchor_comment.snapshot body text.

    PR #2623 review fix (P1 finding 2): a fresh, unchanged `fetch_current`
    readback is REQUIRED to reach this route now -- the injected callback
    below is asserted to have actually run (not merely present but unused),
    proving the handoff is never authorized from a stale in-hand snapshot
    alone."""
    kwargs = _consumer_kwargs()
    fetch_current, fetch_calls = _fetch_current_unchanged()
    kwargs["callbacks"] = {"fetch_current": fetch_current}
    result = preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["writes"] == 0
    assert result["rewrite_route"]["route"] == "issue_editor_required"
    assert result["reviewer_feedback_url"] == _ANCHOR_URL
    # AC1: never the raw anchor comment body forwarded as feedback text.
    assert result["reviewer_feedback_url"] != _ANCHOR_BODY
    assert "reviewer_feedback_text" not in result
    # PR #2623 finding 2: the fresh readback genuinely ran, not skipped.
    assert fetch_calls["count"] >= 1


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
    anchor_binding_ok re-check fails closed.

    PR #2623 review fix (P1 finding 3): a binding-mismatch ineligibility is
    a NORMAL, non-failure outcome (this route simply does not apply) -- it
    must reach the existing `no_change` fallback with `writes == 0`, reusing
    existing vocabulary rather than a new schema value."""
    drifted_body = _ANCHOR_BODY + "\nEdited after evidence capture.\n"
    kwargs = _consumer_kwargs(anchor_body=drifted_body, evidence_anchor_body=_ANCHOR_BODY)
    result = preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result.get("rewrite_route") is None
    assert result.get("status") != "handoff_required"
    assert result.get("status") == "no_change"
    assert result.get("writes") == 0


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


# ---------------------------------------------------------------------------
# PR #2623 review fix (P1 finding 2): TOCTOU-safe fresh readback inside
# _decide_human_review_directive_editor_route() -- unit-level coverage of
# the SSOT-adjacent consumer helper directly (not via the full
# run_preflight() subprocess boundary; see test_preflight_run_with_anchor.py
# for the production-reachable equivalent).
# ---------------------------------------------------------------------------


def _known_context_for_eligible_freeform_directive() -> dict:
    kwargs = _consumer_kwargs()
    return kwargs["known_context"]


def test_finding2_fresh_readback_unchanged_anchor_authorizes_handoff():
    """A fresh `fetch_current()` readback that returns the SAME anchor body
    and identity the evidence was captured from still authorizes the
    issue_editor_required handoff -- the TOCTOU-safe check is additive, not
    a regression on the ordinary eligible case."""
    known_context = _known_context_for_eligible_freeform_directive()
    fetch_current, calls = _fetch_current_unchanged()

    result = preflight._decide_human_review_directive_editor_route(
        known_context=known_context,
        anchor_url=_ANCHOR_URL,
        anchor_body=_ANCHOR_BODY,
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        issue_body_sha256=_sha256(_ISSUE_BODY),
        fetch_current=fetch_current,
    )

    assert result is not None
    assert result["status"] == "handoff_required"
    assert result["writes"] == 0
    assert calls["count"] >= 1


def test_finding2_fresh_readback_drifted_anchor_fails_closed_to_none():
    """A fresh `fetch_current()` readback that returns a DIFFERENT anchor
    body than the one this evidence/decision was built from must never
    authorize a handoff -- returns None (existing fallback applies), never
    issue_editor_required, and the drift is only detectable because the
    fresh readback genuinely ran (asserted via the call counter)."""
    known_context = _known_context_for_eligible_freeform_directive()
    calls = {"count": 0}

    def _fetch_current_drifted():
        calls["count"] += 1
        return (
            {"body": _ISSUE_BODY, "updatedAt": "2026-09-10T00:00:00Z"},
            {
                "id": _ANCHOR_COMMENT_ID,
                "html_url": _ANCHOR_URL,
                "body": _ANCHOR_BODY + "\nEdited after evidence capture.\n",
            },
        )

    result = preflight._decide_human_review_directive_editor_route(
        known_context=known_context,
        anchor_url=_ANCHOR_URL,
        anchor_body=_ANCHOR_BODY,
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        issue_body_sha256=_sha256(_ISSUE_BODY),
        fetch_current=_fetch_current_drifted,
    )

    assert result is None
    assert calls["count"] >= 1


def test_finding2_fresh_readback_drifted_identity_fails_closed_to_none():
    """The SAME anchor body, but a fresh `fetch_current()` readback whose
    `html_url` no longer matches this transaction's `anchor_url` (a
    different/replaced comment) also fails closed."""
    known_context = _known_context_for_eligible_freeform_directive()

    def _fetch_current_wrong_identity():
        return (
            {"body": _ISSUE_BODY, "updatedAt": "2026-09-10T00:00:00Z"},
            {
                "id": _ANCHOR_COMMENT_ID,
                "html_url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#issuecomment-999999",
                "body": _ANCHOR_BODY,
            },
        )

    result = preflight._decide_human_review_directive_editor_route(
        known_context=known_context,
        anchor_url=_ANCHOR_URL,
        anchor_body=_ANCHOR_BODY,
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        issue_body_sha256=_sha256(_ISSUE_BODY),
        fetch_current=_fetch_current_wrong_identity,
    )

    assert result is None


def test_finding2_missing_fetch_current_callback_fails_closed_to_none():
    """A caller that does not inject `fetch_current` at all never authorizes
    a handoff from possibly-stale in-hand evidence alone -- fails closed to
    None (existing fallback, which performs its own fresh readback,
    applies)."""
    known_context = _known_context_for_eligible_freeform_directive()

    result = preflight._decide_human_review_directive_editor_route(
        known_context=known_context,
        anchor_url=_ANCHOR_URL,
        anchor_body=_ANCHOR_BODY,
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        issue_body_sha256=_sha256(_ISSUE_BODY),
    )

    assert result is None


# ---------------------------------------------------------------------------
# PR #2623 review fix (P1 finding 3): integrity/environment failures inside
# _decide_human_review_directive_editor_route() must be distinguishable
# from ordinary ineligibility (`None`) -- each below asserts the SPECIFIC
# fail-closed status/disposition/reason_code (reusing the existing
# `status: "invalid"` / `disposition: {...}` vocabulary, never a new
# schema/key-set) and `writes == 0`.
# ---------------------------------------------------------------------------


def test_finding3_broken_import_returns_distinguishable_environment_failure(monkeypatch):
    """A broken import (the routing SSOT module missing the expected
    symbol) must never collapse into the same `None` as ordinary
    ineligibility -- it is surfaced as a distinguishable, fail-closed
    `status: "invalid"` / `disposition: "invalid"` result with a specific
    `reason_code`, never a silently-absorbed `no_change`."""
    monkeypatch.delattr(decide_rewrite_route_module, "decide_human_review_directive_editor_route", raising=True)
    known_context = _known_context_for_eligible_freeform_directive()
    fetch_current, _calls = _fetch_current_unchanged()

    result = preflight._decide_human_review_directive_editor_route(
        known_context=known_context,
        anchor_url=_ANCHOR_URL,
        anchor_body=_ANCHOR_BODY,
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        issue_body_sha256=_sha256(_ISSUE_BODY),
        fetch_current=fetch_current,
    )

    assert result is not None
    assert result["status"] == "invalid"
    assert result["writes"] == 0
    assert result["disposition"]["disposition"] == "invalid"
    assert result["disposition"]["reason_code"] == "human_review_directive_route_import_failed"


def test_finding3_classifier_exception_returns_distinguishable_environment_failure(monkeypatch):
    """An exception raised by the fresh `classify_scope_delta_authority()`
    re-classification must never collapse into the same `None` as ordinary
    ineligibility -- surfaced as a distinguishable, fail-closed
    `status: "invalid"` result with a specific `reason_code`."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated classifier failure")

    monkeypatch.setattr(scope_signal_delta_module, "classify_scope_delta_authority", _boom)
    known_context = _known_context_for_eligible_freeform_directive()
    fetch_current, _calls = _fetch_current_unchanged()

    result = preflight._decide_human_review_directive_editor_route(
        known_context=known_context,
        anchor_url=_ANCHOR_URL,
        anchor_body=_ANCHOR_BODY,
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        issue_body_sha256=_sha256(_ISSUE_BODY),
        fetch_current=fetch_current,
    )

    assert result is not None
    assert result["status"] == "invalid"
    assert result["writes"] == 0
    assert result["disposition"]["disposition"] == "invalid"
    assert result["disposition"]["reason_code"] == "human_review_directive_route_classifier_error"


def test_finding3_fresh_readback_transport_failure_returns_distinguishable_environment_failure():
    """A `fetch_current()` callback that raises (the same failure mode as a
    real GitHub readback transport error) must never collapse into the
    same `None` as ordinary ineligibility -- surfaced as a distinguishable,
    fail-closed `status: "invalid"` result, never silently absorbed into an
    innocuous `no_change`."""
    known_context = _known_context_for_eligible_freeform_directive()

    def _fetch_current_raises():
        raise RuntimeError("issue_readback_failed:simulated_transport_error")

    result = preflight._decide_human_review_directive_editor_route(
        known_context=known_context,
        anchor_url=_ANCHOR_URL,
        anchor_body=_ANCHOR_BODY,
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        issue_body_sha256=_sha256(_ISSUE_BODY),
        fetch_current=_fetch_current_raises,
    )

    assert result is not None
    assert result["status"] == "invalid"
    assert result["writes"] == 0
    assert result["disposition"]["disposition"] == "invalid"
    assert result["disposition"]["reason_code"] == "human_review_directive_route_fresh_readback_failed"


def test_finding3_environment_failure_propagates_through_production_consumer(monkeypatch):
    """The SAME broken-import integrity failure, but exercised through the
    real production consumer `consume_trusted_anchor_contract_patch_plan()`
    (not the helper directly) -- the distinguishable failure must survive
    that boundary unchanged, not get re-collapsed into `no_change`."""
    monkeypatch.delattr(decide_rewrite_route_module, "decide_human_review_directive_editor_route", raising=True)
    kwargs = _consumer_kwargs()
    fetch_current, _calls = _fetch_current_unchanged()
    kwargs["callbacks"] = {"fetch_current": fetch_current}

    result = preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "invalid"
    assert result["writes"] == 0
    assert result["disposition"]["disposition"] == "invalid"
    assert result["disposition"]["reason_code"] == "human_review_directive_route_import_failed"
    assert result.get("rewrite_route") is None
    assert result.get("status") != "no_change"
