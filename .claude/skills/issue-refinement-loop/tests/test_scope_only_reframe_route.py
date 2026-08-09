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
from scope_signal_delta import derive_contract_patch_operations  # noqa: E402


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
    assert first_result.should_post_comment is True

    # Simulate the loop persisting the fingerprint after (a) recording the
    # no-progress occurrence and (b) posting the scope-reframe comment once.
    replay_state = SCOPE_REFRAME_CONTRACT_ROUTE_STATE_V1(
        scope_delta_status="approved_by_trusted_anchor",
        allowed_path_deltas=["- `docs/product/features/scope-only-reframe.md`"],
        operations=operations,
        anchor_comment_url=SCOPE_ONLY_REFRAME_ANCHOR_URL,
        issue_body_sha256=SCOPE_ONLY_REFRAME_BODY_SHA256,
        previous_empty_operations_fingerprints=[first_result.empty_operations_fingerprint],
        posted_scope_reframe_comment_fingerprints=[first_result.empty_operations_fingerprint],
    )
    replay_result = decide_scope_reframe_contract_route(replay_state)

    assert replay_result.route == ROUTE_ISSUE_EDITOR_REQUIRED
    assert replay_result.route != ROUTE_CONTRACT_UPDATE
    assert replay_result.no_progress_retry_suppressed is True
    assert replay_result.duplicate_comment is True
    assert replay_result.should_post_comment is False
