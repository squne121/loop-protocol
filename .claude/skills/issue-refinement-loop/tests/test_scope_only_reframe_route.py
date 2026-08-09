"""
test_scope_only_reframe_route.py

Issue #2048 regression: scope-only reframe with empty operations[] must route
to issue_editor_required, not contract_update (follow-up to #1877/PR #1884).

This is a dedicated fixture-driven regression test, independent of
test_rewrite_router.py's unit coverage, exercising the full
SCOPE_REFRAME_CONTRACT_ROUTE_STATE_V1 -> decide_scope_reframe_contract_route
-> ScopeReframeRouteResult round trip against a realistic scope-only reframe
fixture (an approved trusted-anchor Allowed Paths expansion whose derived
operations[] is empty because the directive text carried no `code fence`
path literal for derive_contract_patch_operations to extract).
"""

from __future__ import annotations

import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from decide_rewrite_route import (  # noqa: E402
    ROUTE_CONTRACT_UPDATE,
    ROUTE_ISSUE_EDITOR_REQUIRED,
    REASON_CODE_APPROVED_SCOPE_REQUIRES_FULL_CONTRACT_REWRITE,
    SCOPE_REFRAME_CONTRACT_ROUTE_STATE_V1,
    decide_scope_reframe_contract_route,
)
from scope_signal_delta import (  # noqa: E402
    derive_contract_patch_operations,
    run_trusted_anchor_iteration_zero,
)


# ---------------------------------------------------------------------------
# Fixture: a scope-only reframe (Allowed Paths expansion approved by a
# trusted anchor) whose evidence carries directive markers but no
# derivable path literal, so derive_contract_patch_operations() returns [].
# ---------------------------------------------------------------------------

SCOPE_ONLY_REFRAME_EVIDENCE_FIXTURE = {
    "directive_markers": ["allowed paths"],
    "extracted_directives": [
        "Allowed Paths を拡張する（対象パスは別途 issue-editor が本文全体を"
        "再構成して確定する — 個別の path literal はこのコメント内にはない）",
    ],
}

SCOPE_ONLY_REFRAME_ANCHOR_URL = (
    "https://github.com/squne121/loop-protocol/issues/2048#issuecomment-2048001"
)
SCOPE_ONLY_REFRAME_BODY_SHA256 = "c" * 64


def test_derive_contract_patch_operations_is_empty_for_scope_only_fixture():
    """Precondition: the fixture evidence really does derive to operations[] == []
    (no `code fence`-quoted path literal to extract), matching the Issue #2048
    Outcome scenario of an approved scope reframe with no derivable patch op."""
    operations = derive_contract_patch_operations([SCOPE_ONLY_REFRAME_EVIDENCE_FIXTURE])
    assert operations == []


def test_scope_only_reframe_with_empty_operations_routes_to_issue_editor_required():
    """AC1/AC6: approved scope reframe + empty operations[] -> issue_editor_required
    (reason_code: approved_scope_requires_full_contract_rewrite), never contract_update."""
    operations = derive_contract_patch_operations([SCOPE_ONLY_REFRAME_EVIDENCE_FIXTURE])
    state = SCOPE_REFRAME_CONTRACT_ROUTE_STATE_V1(
        scope_delta_status="approved_by_trusted_anchor",
        allowed_path_deltas=["- `docs/product/features/scope-only-reframe.md`"],
        operations=operations,
        anchor_comment_url=SCOPE_ONLY_REFRAME_ANCHOR_URL,
        issue_body_sha256=SCOPE_ONLY_REFRAME_BODY_SHA256,
    )

    result = decide_scope_reframe_contract_route(state)

    assert result.route == ROUTE_ISSUE_EDITOR_REQUIRED
    assert result.route != ROUTE_CONTRACT_UPDATE
    assert result.reason_code == REASON_CODE_APPROVED_SCOPE_REQUIRES_FULL_CONTRACT_REWRITE


def test_scope_only_reframe_replay_suppresses_no_progress_retry_and_duplicate_comment():
    """AC2/AC3/AC6: replaying the identical scope-only reframe (same anchor,
    same body sha256, still empty operations[]) must not re-issue a
    no-progress contract_update AND must not post a duplicate scope-reframe
    comment."""
    operations = derive_contract_patch_operations([SCOPE_ONLY_REFRAME_EVIDENCE_FIXTURE])

    first_state = SCOPE_REFRAME_CONTRACT_ROUTE_STATE_V1(
        scope_delta_status="approved_by_trusted_anchor",
        allowed_path_deltas=["- `docs/product/features/scope-only-reframe.md`"],
        operations=operations,
        anchor_comment_url=SCOPE_ONLY_REFRAME_ANCHOR_URL,
        issue_body_sha256=SCOPE_ONLY_REFRAME_BODY_SHA256,
    )
    first_result = decide_scope_reframe_contract_route(first_state)
    assert first_result.no_progress_retry_suppressed is False
    assert first_result.comment_action == "none"

    # Simulate the loop persisting the fingerprint after recording the
    # no-progress occurrence.
    replay_state = SCOPE_REFRAME_CONTRACT_ROUTE_STATE_V1(
        scope_delta_status="approved_by_trusted_anchor",
        allowed_path_deltas=["- `docs/product/features/scope-only-reframe.md`"],
        operations=operations,
        anchor_comment_url=SCOPE_ONLY_REFRAME_ANCHOR_URL,
        issue_body_sha256=SCOPE_ONLY_REFRAME_BODY_SHA256,
        previous_empty_operations_fingerprints=[first_result.empty_operations_fingerprint],
    )
    replay_result = decide_scope_reframe_contract_route(replay_state)

    assert replay_result.route == ROUTE_ISSUE_EDITOR_REQUIRED
    assert replay_result.route != ROUTE_CONTRACT_UPDATE
    assert replay_result.no_progress_retry_suppressed is True
    assert replay_result.comment_action == "none"


# ---------------------------------------------------------------------------
# Production wiring regression: run_trusted_anchor_iteration_zero (the actual
# call path invoked by run_refinement_preflight.py's contract_update.run.with_anchor
# lane) must reach decide_scope_reframe_contract_route() when an approved
# trusted-anchor scope reframe derives an empty operations[] -- not just the
# standalone unit tests above (Issue #2048 PR review blocker: "no production
# caller invokes decide_scope_reframe_contract_route()").
# ---------------------------------------------------------------------------

_ITERATION_ZERO_REPO = "squne121/loop-protocol"
_ITERATION_ZERO_ISSUE = 2048
_ITERATION_ZERO_ANCHOR_URL = (
    f"https://github.com/{_ITERATION_ZERO_REPO}/issues/{_ITERATION_ZERO_ISSUE}"
    "#issuecomment-2048099"
)
_ITERATION_ZERO_ANCHOR_BODY = (
    "Allowed Paths を拡張する（対象パスは issue-editor が本文全体を再構成して確定する）"
)
_ITERATION_ZERO_PRE_BODY = "## Outcome\n\n既存の本文。\n"


def _iteration_zero_anchor() -> dict:
    import hashlib

    return {
        "id": 2048099,
        "html_url": _ITERATION_ZERO_ANCHOR_URL,
        "author_association": "OWNER",
        "source_body_sha256": "sha256:"
        + hashlib.sha256(_ITERATION_ZERO_ANCHOR_BODY.encode("utf-8")).hexdigest(),
    }


def _iteration_zero_readiness(_body: str) -> dict:
    return {
        "status": "go",
        "body_sha256": "sha256:candidate",
        "source_checks": [],
        "errors": [],
        "readiness_result_ref": "fixture",
    }


def _iteration_zero_fetch_current(body: str, anchor: dict):
    return lambda: ({"body": body, "updatedAt": "2026-08-09T00:00:00Z"}, anchor)


def test_run_trusted_anchor_iteration_zero_surfaces_issue_editor_required_route():
    """Production wiring: run_trusted_anchor_iteration_zero (called by
    run_refinement_preflight.py's contract_update.run.with_anchor lane) must
    itself invoke decide_scope_reframe_contract_route() and annotate the
    no_change result with rewrite_route.route == issue_editor_required when
    the approved scope reframe's operations[] is empty -- callers must not
    treat this as an ordinary no-op replay."""
    anchor = _iteration_zero_anchor()
    result = run_trusted_anchor_iteration_zero(
        repo=_ITERATION_ZERO_REPO,
        issue_number=_ITERATION_ZERO_ISSUE,
        issue={"body": _ITERATION_ZERO_PRE_BODY},
        anchor=anchor,
        anchor_body=_ITERATION_ZERO_ANCHOR_BODY,
        patch_plan={"operations": []},
        candidate_readiness=_iteration_zero_readiness,
        fetch_current=_iteration_zero_fetch_current(_ITERATION_ZERO_PRE_BODY, anchor),
        allowed_path_deltas=["- `docs/product/features/scope-only-reframe.md`"],
        scope_delta_status="approved_by_trusted_anchor",
        reflected_checker=lambda _body: "absent",
    )

    assert result["status"] == "no_change"
    assert "rewrite_route" in result
    assert result["rewrite_route"]["route"] == ROUTE_ISSUE_EDITOR_REQUIRED
    assert (
        result["rewrite_route"]["reason_code"]
        == REASON_CODE_APPROVED_SCOPE_REQUIRES_FULL_CONTRACT_REWRITE
    )
    assert result["rewrite_route"]["comment_action"] == "none"


def test_run_trusted_anchor_iteration_zero_replay_suppresses_via_production_path():
    """AC2/AC3 through the production call path: replaying the same
    empty-operations scope reframe (fingerprints already recorded) must
    surface no_progress_retry_suppressed/duplicate_comment=True so the
    orchestrator does not re-issue a no-progress contract_update or repost
    the scope-reframe comment."""
    anchor = _iteration_zero_anchor()
    first = run_trusted_anchor_iteration_zero(
        repo=_ITERATION_ZERO_REPO,
        issue_number=_ITERATION_ZERO_ISSUE,
        issue={"body": _ITERATION_ZERO_PRE_BODY},
        anchor=anchor,
        anchor_body=_ITERATION_ZERO_ANCHOR_BODY,
        patch_plan={"operations": []},
        candidate_readiness=_iteration_zero_readiness,
        fetch_current=_iteration_zero_fetch_current(_ITERATION_ZERO_PRE_BODY, anchor),
        allowed_path_deltas=["- `docs/product/features/scope-only-reframe.md`"],
        scope_delta_status="approved_by_trusted_anchor",
        reflected_checker=lambda _body: "absent",
    )
    fingerprint = first["rewrite_route"]["empty_operations_fingerprint"]

    replay = run_trusted_anchor_iteration_zero(
        repo=_ITERATION_ZERO_REPO,
        issue_number=_ITERATION_ZERO_ISSUE,
        issue={"body": _ITERATION_ZERO_PRE_BODY},
        anchor=anchor,
        anchor_body=_ITERATION_ZERO_ANCHOR_BODY,
        patch_plan={"operations": []},
        candidate_readiness=_iteration_zero_readiness,
        fetch_current=_iteration_zero_fetch_current(_ITERATION_ZERO_PRE_BODY, anchor),
        allowed_path_deltas=["- `docs/product/features/scope-only-reframe.md`"],
        scope_delta_status="approved_by_trusted_anchor",
        reflected_checker=lambda _body: "absent",
        previous_empty_operations_fingerprints=[fingerprint],
    )

    assert replay["rewrite_route"]["route"] == ROUTE_ISSUE_EDITOR_REQUIRED
    assert replay["rewrite_route"]["no_progress_retry_suppressed"] is True
    assert replay["rewrite_route"]["comment_action"] == "none"


def test_run_trusted_anchor_iteration_zero_without_allowed_path_deltas_is_unaffected():
    """Backward compatibility: callers that do not pass allowed_path_deltas
    (the pre-#2048 call shape) never get a rewrite_route key, preserving
    byte-identical no_change results for #1877/#1835 callers."""
    anchor = _iteration_zero_anchor()
    result = run_trusted_anchor_iteration_zero(
        repo=_ITERATION_ZERO_REPO,
        issue_number=_ITERATION_ZERO_ISSUE,
        issue={"body": _ITERATION_ZERO_PRE_BODY},
        anchor=anchor,
        anchor_body=_ITERATION_ZERO_ANCHOR_BODY,
        patch_plan={"operations": []},
        candidate_readiness=_iteration_zero_readiness,
        fetch_current=_iteration_zero_fetch_current(_ITERATION_ZERO_PRE_BODY, anchor),
    )

    assert result["status"] == "no_change"
    assert "rewrite_route" not in result
