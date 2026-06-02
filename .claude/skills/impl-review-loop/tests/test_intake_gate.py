"""
Regression fixture for impl-review-loop preparation intake gate.

Issue #564: Closes the #561-type handoff gap where `issue-refinement-loop`
`termination_reason: approved` was misread as "implementation_ready" without
a valid `CONTRACT_REVIEW_RESULT_V1 status: go` check.

Tests verify that the intake gate subreason priority and keyword definitions
are present in the preparation.md document.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
PREPARATION_MD = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "steps"
    / "preparation.md"
)
TERMINATION_POLICY_MD = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "issue-refinement-loop"
    / "references"
    / "termination-policy.md"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC1: refinement_approved_gate_pending in termination-policy.md
# ---------------------------------------------------------------------------


def test_ac1_refinement_approved_gate_pending_defined():
    """AC1: refinement_approved_gate_pending state is defined in termination-policy."""
    body = _read(TERMINATION_POLICY_MD)
    assert "refinement_approved_gate_pending" in body, (
        "termination-policy.md must define refinement_approved_gate_pending"
    )


def test_ac1_implementation_ready_defined():
    """AC1: implementation_ready state is defined alongside refinement_approved_gate_pending."""
    body = _read(TERMINATION_POLICY_MD)
    assert "implementation_ready" in body, (
        "termination-policy.md must define implementation_ready"
    )


def test_ac1_approve_alone_does_not_imply_implementation_ready():
    """AC1: approve alone must not be sufficient for implementation_ready."""
    body = _read(TERMINATION_POLICY_MD)
    # The policy must explicitly forbid impl_review_loop_handoff without go confirmation
    assert "forbidden_next" in body, (
        "termination-policy.md must contain forbidden_next to block premature handoff"
    )
    assert "impl_review_loop_handoff" in body, (
        "termination-policy.md must reference impl_review_loop_handoff in forbidden_next"
    )


# ---------------------------------------------------------------------------
# AC2: intake_gate_failed and CONTRACT_REVIEW_RESULT_V1 status: go required
# ---------------------------------------------------------------------------


def test_ac2_intake_gate_failed_defined():
    """AC2: intake_gate_failed is defined in preparation.md."""
    body = _read(PREPARATION_MD)
    assert "intake_gate_failed" in body, (
        "preparation.md must define intake_gate_failed"
    )


def test_ac2_contract_review_result_go_required():
    """AC2: CONTRACT_REVIEW_RESULT_V1 status: go is a required input."""
    body = _read(PREPARATION_MD)
    assert "CONTRACT_REVIEW_RESULT_V1" in body, (
        "preparation.md must reference CONTRACT_REVIEW_RESULT_V1"
    )
    # Either Japanese or English phrasing for 'required'
    assert "必須" in body or "status: go" in body, (
        "preparation.md must emphasize status: go as required"
    )


def test_ac2_subreason_priority_order_defined():
    """AC2: The 4 subreasons are defined with explicit priority ordering."""
    body = _read(PREPARATION_MD)
    subreasons = [
        "metadata_not_ready",
        "missing_contract_go",
        "stale_contract_review",
        "body_snapshot_mismatch",
        "request_changes_after_go",
    ]
    for sr in subreasons:
        assert sr in body, (
            f"preparation.md must define subreason '{sr}'"
        )


def test_ac2_priority_ordering_metadata_not_ready_first():
    """AC2: metadata_not_ready must appear before missing_contract_go in document."""
    body = _read(PREPARATION_MD)
    idx_meta = body.find("metadata_not_ready")
    idx_missing = body.find("missing_contract_go")
    assert idx_meta < idx_missing, (
        "metadata_not_ready must be defined before missing_contract_go (higher priority)"
    )


def test_ac2_priority_ordering_request_changes_last():
    """AC2: request_changes_after_go must appear after stale_contract_review (lowest priority)."""
    body = _read(PREPARATION_MD)
    idx_stale = body.find("stale_contract_review")
    idx_request = body.find("request_changes_after_go")
    assert idx_stale < idx_request, (
        "stale_contract_review must appear before request_changes_after_go"
    )


# ---------------------------------------------------------------------------
# AC3: metadata_not_ready for missing title prefix or phase/implementation label
# ---------------------------------------------------------------------------


def test_ac3_metadata_not_ready_covers_title_prefix():
    """AC3: metadata_not_ready subreason covers missing title prefix."""
    body = _read(PREPARATION_MD)
    # Find the section around metadata_not_ready
    idx = body.find("metadata_not_ready")
    context = body[idx : idx + 500]
    assert "実装:" in context or "implement:" in context or "title" in context.lower(), (
        "metadata_not_ready must reference title prefix requirement"
    )


def test_ac3_metadata_not_ready_covers_phase_implementation_label():
    """AC3: metadata_not_ready subreason covers missing phase/implementation label."""
    body = _read(PREPARATION_MD)
    assert "phase/implementation" in body, (
        "preparation.md must reference phase/implementation label in metadata_not_ready"
    )


# ---------------------------------------------------------------------------
# AC4: body_sha256 / generated_at freshness check
# ---------------------------------------------------------------------------


def test_ac4_body_sha256_freshness_defined():
    """AC4: body_sha256 freshness check is defined in preparation gate."""
    body = _read(PREPARATION_MD)
    assert "body_sha256" in body, (
        "preparation.md must define body_sha256 freshness check"
    )


def test_ac4_generated_at_fallback_defined():
    """AC4: generated_at < issue.updated_at fallback is defined."""
    body = _read(PREPARATION_MD)
    assert "generated_at" in body, (
        "preparation.md must reference generated_at for stale detection"
    )
    assert "updated_at" in body, (
        "preparation.md must reference issue.updated_at as fallback freshness signal"
    )


def test_ac4_stale_contract_review_defined():
    """AC4: stale_contract_review subreason is associated with freshness check."""
    body = _read(PREPARATION_MD)
    assert "stale_contract_review" in body, (
        "preparation.md must define stale_contract_review as a freshness-related subreason"
    )


# ---------------------------------------------------------------------------
# AC5: go 無効化 marker (GO_INVALIDATION_POLICY_V1)
# ---------------------------------------------------------------------------


def test_ac5_go_invalidation_policy_defined():
    """AC5: GO_INVALIDATION_POLICY_V1 is defined as machine-readable block."""
    body = _read(PREPARATION_MD)
    assert "GO_INVALIDATION_POLICY_V1" in body, (
        "preparation.md must define GO_INVALIDATION_POLICY_V1 go invalidation policy"
    )


def test_ac5_go_invalidation_policy_source_field():
    """AC5: GO_INVALIDATION_POLICY_V1 includes source: issue_comment field."""
    body = _read(PREPARATION_MD)
    assert "source: issue_comment" in body, (
        "GO_INVALIDATION_POLICY_V1 must include 'source: issue_comment' field"
    )


def test_ac5_go_invalidation_policy_accepted_marker_field():
    """AC5: GO_INVALIDATION_POLICY_V1 includes accepted_marker field."""
    body = _read(PREPARATION_MD)
    assert "accepted_marker:" in body, (
        "GO_INVALIDATION_POLICY_V1 must include 'accepted_marker' field"
    )
    assert "REVIEW_RESULT_V1.status == request_changes" in body, (
        "GO_INVALIDATION_POLICY_V1 accepted_marker must reference REVIEW_RESULT_V1.status == request_changes"
    )


def test_ac5_go_invalidation_policy_target_issue_url_must_match():
    """AC5: GO_INVALIDATION_POLICY_V1 includes target_issue_url_must_match field."""
    body = _read(PREPARATION_MD)
    assert "target_issue_url_must_match: true" in body, (
        "GO_INVALIDATION_POLICY_V1 must include 'target_issue_url_must_match: true'"
    )


def test_ac5_go_invalidation_policy_ordering_key():
    """AC5: GO_INVALIDATION_POLICY_V1 includes ordering_key: created_at field."""
    body = _read(PREPARATION_MD)
    assert "ordering_key: created_at" in body, (
        "GO_INVALIDATION_POLICY_V1 must include 'ordering_key: created_at'"
    )


def test_ac5_request_changes_after_go_subreason():
    """AC5: request_changes_after_go subreason is defined."""
    body = _read(PREPARATION_MD)
    assert "request_changes_after_go" in body, (
        "preparation.md must define request_changes_after_go as a subreason"
    )


# ---------------------------------------------------------------------------
# AC6: #561-type regression — fail-only gate (no auto issue-contract-review)
# ---------------------------------------------------------------------------


def test_ac6_missing_contract_go_does_not_auto_run_issue_contract_review():
    """AC6 regression (#561): missing status:go must not trigger auto issue-contract-review."""
    body = _read(PREPARATION_MD)
    # The preparation.md must explicitly state the fail-only gate design decision
    assert "fail-only gate" in body or "自動実行しない" in body or "廃止" in body, (
        "preparation.md must document that missing status:go triggers fail-only gate, "
        "not automatic issue-contract-review execution (#561 regression)"
    )


def test_ac6_handoff_gap_prevention_documented_in_termination_policy():
    """AC6 regression (#561): handoff gap prevention documented in termination-policy."""
    body = _read(TERMINATION_POLICY_MD)
    # Must reference the #561-type gap prevention
    assert "handoff" in body.lower() or "HANDOFF_STATE_V1" in body, (
        "termination-policy.md must document handoff state to prevent #561-type gap"
    )


def test_ac6_intake_gate_result_schema_defined():
    """AC6: INTAKE_GATE_RESULT_V1 schema is defined in preparation.md."""
    body = _read(PREPARATION_MD)
    assert "INTAKE_GATE_RESULT_V1" in body, (
        "preparation.md must define INTAKE_GATE_RESULT_V1 schema"
    )


# ---------------------------------------------------------------------------
# AC5/Blocker2: on_intake_gate_failed — LOOP_STATE.termination_reason propagation
# ---------------------------------------------------------------------------


def test_blocker2_on_intake_gate_failed_defined():
    """Blocker2: on_intake_gate_failed section is defined in preparation.md."""
    body = _read(PREPARATION_MD)
    assert "on_intake_gate_failed" in body, (
        "preparation.md must define on_intake_gate_failed stop processing block"
    )


def test_blocker2_termination_reason_set_to_intake_gate_failed():
    """Blocker2: on_intake_gate_failed sets LOOP_STATE.termination_reason to intake_gate_failed."""
    body = _read(PREPARATION_MD)
    assert "set LOOP_STATE.termination_reason: intake_gate_failed" in body, (
        "preparation.md must specify 'set LOOP_STATE.termination_reason: intake_gate_failed' "
        "in on_intake_gate_failed block"
    )


def test_blocker2_do_not_continue_to_step_1():
    """Blocker2: on_intake_gate_failed blocks continuation to step 1."""
    body = _read(PREPARATION_MD)
    assert "do_not_continue_to_step_1: true" in body, (
        "preparation.md must specify 'do_not_continue_to_step_1: true' in on_intake_gate_failed"
    )


def test_blocker2_termination_reason_valid_values_include_intake_gate_failed():
    """Blocker2: termination_reason valid values include intake_gate_failed in preparation.md."""
    body = _read(PREPARATION_MD)
    assert "intake_gate_failed" in body, (
        "preparation.md must list intake_gate_failed as a valid termination_reason value"
    )
    # Check it appears in termination_reason context
    idx = body.find("termination_reason")
    assert idx != -1, "preparation.md must mention termination_reason"
    context = body[idx : idx + 200]
    assert "intake_gate_failed" in context, (
        "intake_gate_failed must appear near termination_reason definition"
    )
