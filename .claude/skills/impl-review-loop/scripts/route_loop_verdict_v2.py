"""
Production consumer module for impl-review-loop Step 5 routing.

Issue #1873: reviewer self-report of merge_ready / required_auto_actions /
allowed_paths_gate is no longer a production input. pr-reviewer's minimal
result convention is verdict / reviewed_head_sha / blockers / warnings only
(see docs/dev/workflow.md "PR review 内部返却 convention"). Mergeability
(mergeable, merge_state_status) is read directly from live GitHub PR state
by the caller and passed in as `live_mergeability`. This module keeps the
mergeability hard-stop invariants established in #1869/PR #1871
(step-5-mergeability-handling.md): only CONFLICTING/DIRTY are treated as a
Git conflict requiring the CONFLICTING PR Escalation Runbook; UNKNOWN,
BLOCKED, BEHIND, UNSTABLE, DRAFT, HAS_HOOKS are not conflicts.

Import-time side effects are forbidden: no gh, git, network, or subprocess
calls are made at module load or function call. Callers are responsible for
fetching reviewer_verdict (from the pr-reviewer SubAgent's returned result)
and live_mergeability (from `gh pr view --json headRefOid,mergeable,
mergeStateStatus`) before calling route_loop_verdict_v2().

Public API
----------
route_loop_verdict_v2(reviewer_verdict, live_mergeability, test_verdict=None)
    -> RouteDecision

Issue #1856 (evidence authority cutover, Phase 1): the optional
``test_verdict`` argument, when supplied, is accepted for schema
compatibility only. It is diagnostics-only and is never consulted for any
routing decision. BEHIND routing is derived solely from
``live_mergeability["merge_state_status"] == "BEHIND"``.

RouteDecision fields
---------------------
route:            one of the ROUTE_* constants below
fail_closed:      True when a missing / unknown / mismatched input forced a
                  safe-default (non-actionable) outcome
reason_code:      machine-readable short code explaining the outcome, or None
selected_action:  the resolved auto-action (dict), synthesized by this
                  router for the update_branch case, or None
rerun_required:   dict with boolean keys 'verification' and 'pr_review'
errors:           tuple of human-readable error strings (empty on success)
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping


# ---------------------------------------------------------------------------
# Route constants
# ---------------------------------------------------------------------------

ROUTE_APPROVED = "approved"
ROUTE_CONTINUE_LOOP = "continue_loop"
ROUTE_TO_UPDATE_BRANCH = "route_to_update_branch"
ROUTE_STALE_HEAD_REREVIEW = "route_stale_head_rereview"
ROUTE_HUMAN_ESCALATION = "route_human_escalation"
ROUTE_CONFLICT_ESCALATION = "route_conflict_escalation"
ROUTE_FAIL_CLOSED = "fail_closed"

# ---------------------------------------------------------------------------
# Canonical update_branch action shape (synthesized by this router; no
# longer accepted as reviewer input per #1873)
# ---------------------------------------------------------------------------

_UPDATE_BRANCH_EXECUTOR = "implementation-worker"
_UPDATE_BRANCH_SKILL = "implement-issue.update_branch"

_VALID_VERDICTS: frozenset[str] = frozenset({
    "APPROVE", "REQUEST_CHANGES", "HUMAN_REVIEW_REQUIRED",
})

_VALID_MERGEABLE: frozenset[str] = frozenset({"MERGEABLE", "CONFLICTING", "UNKNOWN"})

_VALID_MERGE_STATE_STATUS: frozenset[str] = frozenset({
    "CLEAN", "UNSTABLE", "BEHIND", "DIRTY", "BLOCKED", "UNKNOWN",
    "DRAFT", "HAS_HOOKS",
})

# merge_state_status values that are an actual Git conflict (hard stop).
_CONFLICT_STATUSES: frozenset[str] = frozenset({"DIRTY", "CONFLICTING"})

# merge_state_status values that require human judgment but are NOT a
# conflict (non-hard-stop per Safety Invariants).
_HUMAN_JUDGMENT_STATUSES: frozenset[str] = frozenset({"BLOCKED", "UNSTABLE", "DRAFT"})

# merge_state_status values compatible with an approved route.
_APPROVABLE_STATUSES: frozenset[str] = frozenset({"CLEAN", "HAS_HOOKS"})


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RouteDecision:
    route: Literal[
        "approved",
        "continue_loop",
        "route_to_update_branch",
        "route_stale_head_rereview",
        "route_human_escalation",
        "route_conflict_escalation",
        "fail_closed",
    ]
    fail_closed: bool
    reason_code: str | None
    selected_action: Mapping[str, Any] | None
    rerun_required: Mapping[str, bool]
    errors: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.selected_action, dict):
            object.__setattr__(self, "selected_action", MappingProxyType(self.selected_action))
        if isinstance(self.rerun_required, dict):
            object.__setattr__(self, "rerun_required", MappingProxyType(self.rerun_required))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_NO_RERUN: Mapping[str, bool] = {"verification": False, "pr_review": False}


def _fail(reason_code: str, *error_msgs: str) -> RouteDecision:
    return RouteDecision(
        route=ROUTE_FAIL_CLOSED,
        fail_closed=True,
        reason_code=reason_code,
        selected_action=None,
        rerun_required=dict(_NO_RERUN),
        errors=tuple(error_msgs),
    )


def _decision(
    route: str,
    *,
    fail_closed: bool = False,
    reason_code: str | None = None,
    selected_action: dict[str, Any] | None = None,
    rerun_required: Mapping[str, bool] = _NO_RERUN,
    errors: tuple[str, ...] = (),
) -> RouteDecision:
    return RouteDecision(
        route=route,
        fail_closed=fail_closed,
        reason_code=reason_code,
        selected_action=selected_action,
        rerun_required=dict(rerun_required),
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def _validate_reviewer_verdict(raw: Any) -> str | None:
    """Return a reason_code if reviewer_verdict is malformed, else None."""
    if not isinstance(raw, Mapping):
        return "schema_invalid_reviewer_verdict_not_mapping"

    verdict = raw.get("verdict")
    if verdict not in _VALID_VERDICTS:
        return f"schema_invalid_verdict_value:{verdict!r}"

    reviewed_head_sha = raw.get("reviewed_head_sha")
    if not isinstance(reviewed_head_sha, str) or not reviewed_head_sha:
        return "schema_invalid_reviewed_head_sha_empty_or_missing"

    blockers = raw.get("blockers", [])
    if not isinstance(blockers, list) or any(not isinstance(b, str) for b in blockers):
        return "schema_invalid_blockers_not_string_list"

    warnings = raw.get("warnings", [])
    if not isinstance(warnings, list) or any(not isinstance(w, str) for w in warnings):
        return "schema_invalid_warnings_not_string_list"

    # V1/V2-legacy fields are no longer accepted inputs (#1873).
    for legacy_key in ("merge_ready", "required_auto_actions", "allowed_paths_gate",
                       "mergeability", "mergeStateStatus", "recommendations"):
        if legacy_key in raw:
            return f"schema_invalid_legacy_field_present:{legacy_key}"

    return None


def _validate_live_mergeability(raw: Any) -> str | None:
    """Return a reason_code if live_mergeability is malformed, else None."""
    if not isinstance(raw, Mapping):
        return "schema_invalid_live_mergeability_not_mapping"

    head_sha = raw.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        return "schema_invalid_live_head_sha_empty_or_missing"

    mergeable = raw.get("mergeable")
    if mergeable not in _VALID_MERGEABLE:
        return f"schema_invalid_mergeable_value:{mergeable!r}"

    merge_state_status = raw.get("merge_state_status")
    if merge_state_status not in _VALID_MERGE_STATE_STATUS:
        return f"schema_invalid_merge_state_status_value:{merge_state_status!r}"

    return None


# ---------------------------------------------------------------------------
# merge_state_status-only BEHIND determination (Issue #1856 AC1/AC2)
# ---------------------------------------------------------------------------
#
# Issue #1856: The historical branch_behind_main / test_verdict cross-check
# (formerly implemented here as _check_behind_invariant) depended on the
# protected TEST_VERDICT lane. Ordinary-review evidence authority no longer
# depends on that lane, so BEHIND is derived solely from
# live_mergeability["merge_state_status"]. A caller-supplied test_verdict is
# accepted (Issue #1873 signature) but is diagnostics-only: it is never
# consulted here, and its presence/absence/content never changes the route.


def _is_behind(merge_state_status: Any) -> bool:
    """Return True iff merge_state_status is exactly the string "BEHIND"."""
    return merge_state_status == "BEHIND"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def route_loop_verdict_v2(
    reviewer_verdict: Mapping[str, Any],
    live_mergeability: Mapping[str, Any],
    test_verdict: Mapping[str, Any] | None = None,
) -> RouteDecision:
    """Deterministic, side-effect-free routing for impl-review-loop Step 5.

    Parameters
    ----------
    reviewer_verdict:
        Minimal result convention returned by the pr-reviewer SubAgent:
        {verdict, reviewed_head_sha, blockers, warnings}.
    live_mergeability:
        Live GitHub PR state fetched by the caller:
        {head_sha, mergeable, merge_state_status}.
    test_verdict:
        Optional TEST_VERDICT_MACHINE/v1 dict, accepted for backward/schema
        compatibility. It is diagnostics-only (Issue #1856 evidence
        authority cutover, Phase 1) and is never consulted for routing.

    Returns
    -------
    RouteDecision

    Note (Issue #1856, AC1/AC2)
    ----------------------------
    BEHIND routing is derived solely from
    ``live_mergeability["merge_state_status"] == "BEHIND"``; the historical
    branch_behind_main cross-check against the protected TEST_VERDICT lane
    has been removed (evidence authority cutover, Phase 1). A supplied
    ``test_verdict`` never changes the route.
    """
    err = _validate_reviewer_verdict(reviewer_verdict)
    if err:
        return _fail(err, f"reviewer_verdict schema error: {err}")

    err = _validate_live_mergeability(live_mergeability)
    if err:
        return _fail(err, f"live_mergeability schema error: {err}")

    verdict_str: str = reviewer_verdict["verdict"]
    reviewed_head_sha: str = reviewer_verdict["reviewed_head_sha"]
    blockers: list[str] = list(reviewer_verdict.get("blockers", []))

    live_head_sha: str = live_mergeability["head_sha"]
    mergeable: str = live_mergeability["mergeable"]
    merge_state_status: str = live_mergeability["merge_state_status"]

    # ------------------------------------------------------------------
    # HUMAN_REVIEW_REQUIRED short-circuits everything else.
    # ------------------------------------------------------------------
    if verdict_str == "HUMAN_REVIEW_REQUIRED":
        return _decision(
            ROUTE_HUMAN_ESCALATION,
            reason_code="reviewer_human_review_required",
        )

    # ------------------------------------------------------------------
    # REQUEST_CHANGES -> continue loop
    # ------------------------------------------------------------------
    if verdict_str == "REQUEST_CHANGES":
        return _decision(ROUTE_CONTINUE_LOOP)

    # verdict_str == "APPROVE" from here on (validated above).

    if blockers:
        return _fail(
            "approve_with_blockers_inconsistent",
            "verdict is APPROVE but blockers is non-empty; treat as an "
            "inconsistent reviewer result rather than routing.",
        )

    # ------------------------------------------------------------------
    # Stale reviewed_head_sha -> re-review at current head, not a hard stop.
    # ------------------------------------------------------------------
    if reviewed_head_sha != live_head_sha:
        return _decision(
            ROUTE_STALE_HEAD_REREVIEW,
            reason_code="stale_reviewed_head_sha",
        )

    # ------------------------------------------------------------------
    # Actual Git conflict: only CONFLICTING/DIRTY are a hard stop.
    # (Preserved from #1869/PR #1871; independent of test_verdict.)
    # ------------------------------------------------------------------
    if mergeable == "CONFLICTING" or merge_state_status in _CONFLICT_STATUSES:
        return _decision(
            ROUTE_CONFLICT_ESCALATION,
            reason_code=f"actual_conflict:mergeable={mergeable},merge_state_status={merge_state_status}",
        )

    is_behind = _is_behind(merge_state_status)

    # ------------------------------------------------------------------
    # UNKNOWN mergeable/merge_state_status: not a conflict, but not yet
    # actionable either. Caller retries (bounded) before escalating.
    # ------------------------------------------------------------------
    if mergeable == "UNKNOWN" or merge_state_status == "UNKNOWN":
        return _fail(
            "mergeability_unknown",
            f"mergeable={mergeable}, merge_state_status={merge_state_status}: "
            f"not yet resolved by GitHub; caller should retry before escalating.",
        )

    # ------------------------------------------------------------------
    # BEHIND: synthesize the update_branch action. The reviewer no longer
    # supplies required_auto_actions; this router computes it from live
    # state per #1873. BEHIND is derived solely from
    # live_mergeability.merge_state_status (Issue #1856); a supplied
    # test_verdict is diagnostics-only and never gates this route.
    # ------------------------------------------------------------------
    if is_behind:
        action = {
            "kind": "update_branch",
            "executor": _UPDATE_BRANCH_EXECUTOR,
            "skill": _UPDATE_BRANCH_SKILL,
            "mechanical": True,
            "expected_head_sha": reviewed_head_sha,
        }
        return _decision(
            ROUTE_TO_UPDATE_BRANCH,
            selected_action=action,
            rerun_required={"verification": True, "pr_review": True},
        )

    # ------------------------------------------------------------------
    # Non-conflict statuses that still require human judgment.
    # ------------------------------------------------------------------
    if merge_state_status in _HUMAN_JUDGMENT_STATUSES:
        return _decision(
            ROUTE_HUMAN_ESCALATION,
            reason_code=f"merge_state_status_{merge_state_status.lower()}_requires_human_judgment",
        )

    # ------------------------------------------------------------------
    # Approvable: CLEAN or HAS_HOOKS with MERGEABLE.
    # ------------------------------------------------------------------
    if merge_state_status in _APPROVABLE_STATUSES and mergeable == "MERGEABLE":
        return _decision(ROUTE_APPROVED)

    return _fail(
        "unexpected_mergeability_combination",
        f"mergeable={mergeable}, merge_state_status={merge_state_status} did not "
        f"match any known routing branch.",
    )
