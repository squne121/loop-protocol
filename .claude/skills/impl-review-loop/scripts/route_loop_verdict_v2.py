"""
Production consumer module for impl-review-loop Step 5 routing.

Issue #1873: reviewer self-report of merge_ready / required_auto_actions /
allowed_paths_gate is no longer a production input. pr-reviewer's minimal
result convention is verdict / reviewed_head_sha / blockers / warnings only
(see .claude/skills/pr-review-judge/references/loop-verdict-v2-schema.md and
references/verdict-output-template.md). Mergeability
(mergeable, merge_state_status) is read directly from live GitHub PR state
by the caller and passed in as `live_mergeability`.

Issue #1860 Owner Decision / PR #1871 (#1869): the ONLY hard stop retained by
this router is a real Git conflict / GitHub non-mergeability, and it is
evaluated BEFORE any reviewer_verdict dispatch -- a REQUEST_CHANGES or
HUMAN_REVIEW_REQUIRED verdict does not bypass it. UNKNOWN, BLOCKED, BEHIND,
UNSTABLE, DRAFT, HAS_HOOKS are never treated as a conflict.

GitHub's actual enums (see GraphQL schema):
  PullRequest.mergeable        (MergeableState):  CONFLICTING | MERGEABLE | UNKNOWN
  PullRequest.mergeStateStatus (MergeStateStatus): BEHIND | BLOCKED | CLEAN | DIRTY |
                                                    DRAFT | HAS_HOOKS | UNKNOWN | UNSTABLE

NOTE: "CONFLICTING" is a valid value of `mergeable` but is NOT a valid value
of `merge_state_status` (that enum member does not exist on GitHub). A
live_mergeability payload that sets merge_state_status == "CONFLICTING" is
therefore malformed input, not a legitimate conflict signal, and is rejected
as schema_invalid rather than silently treated as a conflict.

Import-time side effects are forbidden: no gh, git, network, or subprocess
calls are made at module load or function call. Callers are responsible for
fetching reviewer_verdict (from the pr-reviewer SubAgent's returned result)
and live_mergeability (from `gh pr view --json headRefOid,mergeable,
mergeStateStatus`) before calling route_loop_verdict_v2().

Public API
----------
route_loop_verdict_v2(reviewer_verdict, live_mergeability) -> RouteDecision

Issue #1870 (#1856): this function does not accept a ``test_verdict``
argument. BEHIND routing is derived solely from
``live_mergeability["merge_state_status"] == "BEHIND"``. The protected
TEST_VERDICT producer/publisher lane established by #1856 is unaffected by
this module; it simply has no input into ordinary review routing.

RouteDecision fields
---------------------
route:            one of the ROUTE_* constants below
fail_closed:      True when a missing / unknown / mismatched input, or a
                  non-conflict mergeability state outside the router's
                  authority (UNKNOWN / BLOCKED / UNSTABLE / DRAFT), forced a
                  safe-default (non-actionable, non-escalating) outcome.
                  Callers must not treat fail_closed as a process failure --
                  branch on `route` and `reason_code`.
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
ROUTE_CONFLICT_HARD_STOP = "conflict_hard_stop"
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

# merge_state_status values that require deferral to the current-head
# required-CI / branch-protection evaluator. NOT a conflict, NOT a human
# escalation (#1860 Owner Decision / PR #1871 P0-3).
_DEFER_TO_CI_EVALUATOR_STATUSES: frozenset[str] = frozenset({"BLOCKED", "UNSTABLE", "DRAFT"})

# merge_state_status values compatible with an approved route.
_APPROVABLE_STATUSES: frozenset[str] = frozenset({"CLEAN", "HAS_HOOKS"})

# Legacy/self-reported fields that are no longer accepted as routing input
# (Issue #1873). `test_verdict` is included: PR #1870 (#1856) removed it from
# the public API, and it must not resurface as a reviewer_verdict field.
_REJECTED_LEGACY_FIELDS: tuple[str, ...] = (
    "merge_ready", "required_auto_actions", "allowed_paths_gate",
    "mergeability", "mergeStateStatus", "recommendations", "test_verdict",
)


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
        "conflict_hard_stop",
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


def _fail(reason_code: str, *error_msgs: str) -> RouteDecision:
    return _decision(
        ROUTE_FAIL_CLOSED,
        fail_closed=True,
        reason_code=reason_code,
        errors=tuple(error_msgs),
    )


def _conflict(reason_code: str) -> RouteDecision:
    """The ONE class of hard stop retained by #1860 Owner Decision: real Git
    conflict / GitHub-reported non-mergeability. This is NOT a fail-closed
    safe-default; it is a deterministic, intentional escalation to the
    CONFLICTING PR Escalation Runbook.
    """
    return _decision(ROUTE_CONFLICT_HARD_STOP, reason_code=reason_code)


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

    # Legacy/self-reported authority fields are no longer accepted (#1873).
    for legacy_key in _REJECTED_LEGACY_FIELDS:
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
        # "CONFLICTING" is a valid `mergeable` value but NOT a valid GitHub
        # MergeStateStatus enum member. A payload that puts it here is
        # malformed input, not a legitimate conflict signal.
        return f"schema_invalid_merge_state_status_value:{merge_state_status!r}"

    return None


# ---------------------------------------------------------------------------
# merge_state_status-only BEHIND determination (Issue #1856 AC1/AC2)
# ---------------------------------------------------------------------------
#
# Issue #1856: The historical branch_behind_main / test_verdict cross-check
# depended on the protected TEST_VERDICT lane. Ordinary-review evidence
# authority no longer depends on that lane, so BEHIND is derived solely from
# live_mergeability["merge_state_status"]. This module accepts no
# test_verdict input of any kind.


def _is_behind(merge_state_status: Any) -> bool:
    """Return True iff merge_state_status is exactly the string "BEHIND"."""
    return merge_state_status == "BEHIND"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def route_loop_verdict_v2(
    reviewer_verdict: Mapping[str, Any],
    live_mergeability: Mapping[str, Any],
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

    Returns
    -------
    RouteDecision

    Routing priority (Issue #1873 / #1860 Owner Decision / PR #1871)
    -------------------------------------------------------------------
    1. Structural validation of both inputs.
    2. Actual conflict (mergeable == "CONFLICTING" or
       merge_state_status == "DIRTY") is evaluated BEFORE any reviewer
       verdict is inspected, and hard-stops regardless of verdict.
    3. Reviewer verdict dispatch (HUMAN_REVIEW_REQUIRED / REQUEST_CHANGES /
       APPROVE).
    4. For APPROVE: blockers must be empty; a stale reviewed_head_sha routes
       to re-review rather than dispatching any action; otherwise live
       mergeability determines approved / update_branch / deferred-to-CI /
       unknown-pending-retry.

    Note (Issue #1856, AC1/AC2)
    ----------------------------
    BEHIND routing is derived solely from
    ``live_mergeability["merge_state_status"] == "BEHIND"``; the historical
    branch_behind_main cross-check against the protected TEST_VERDICT lane
    has been removed (evidence authority cutover, Phase 1).
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
    # Priority 2: actual Git conflict, evaluated BEFORE verdict dispatch.
    # Applies regardless of verdict (#1860 Owner Decision / PR #1871 P0-2):
    # a CONFLICTING/DIRTY PR must hard-stop even under REQUEST_CHANGES or
    # HUMAN_REVIEW_REQUIRED.
    # ------------------------------------------------------------------
    if mergeable == "CONFLICTING":
        return _conflict("conflict_mergeable_CONFLICTING")

    if merge_state_status == "DIRTY":
        return _conflict("conflict_merge_state_status_DIRTY")

    # ------------------------------------------------------------------
    # Priority 3: reviewer verdict dispatch.
    # ------------------------------------------------------------------
    if verdict_str == "HUMAN_REVIEW_REQUIRED":
        return _decision(
            ROUTE_HUMAN_ESCALATION,
            reason_code="reviewer_human_review_required",
        )

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
    # No mutation/action is ever dispatched from a stale APPROVE.
    # ------------------------------------------------------------------
    if reviewed_head_sha != live_head_sha:
        return _decision(
            ROUTE_STALE_HEAD_REREVIEW,
            reason_code="stale_reviewed_head_sha",
        )

    # ------------------------------------------------------------------
    # UNKNOWN mergeable/merge_state_status: not a conflict, not a human
    # escalation. Caller performs a bounded retry, then records a warning
    # and holds only merge-readiness (not the review outcome) as pending.
    # ------------------------------------------------------------------
    if mergeable == "UNKNOWN" or merge_state_status == "UNKNOWN":
        return _fail(
            "mergeability_unknown",
            f"mergeable={mergeable}, merge_state_status={merge_state_status}: "
            f"not yet resolved by GitHub; caller should bounded-retry, then "
            f"record a warning without escalating to a human.",
        )

    # ------------------------------------------------------------------
    # BEHIND: synthesize the update_branch action. The reviewer no longer
    # supplies required_auto_actions; this router computes it from live
    # state per #1873.
    # ------------------------------------------------------------------
    if _is_behind(merge_state_status):
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
    # BLOCKED / UNSTABLE / DRAFT: not a conflict, not a human escalation
    # (#1860 Owner Decision / PR #1871 P0-3). The router has no CI/branch
    # protection input, so it defers -- the orchestrator consults the
    # current-head required-CI / branch-protection evaluator and treats
    # this as a warning, not a stop condition. DRAFT specifically must
    # never trigger human escalation on its own: Issue #1873's Delivery
    # Rule requires the PR to stay Draft pending human final merge.
    # ------------------------------------------------------------------
    if merge_state_status in _DEFER_TO_CI_EVALUATOR_STATUSES:
        return _fail(
            f"merge_state_status_{merge_state_status.lower()}_not_conflict_defer_to_ci_evaluator",
            f"merge_state_status={merge_state_status} is not a Git conflict; "
            f"defer to the current-head required-CI / branch-protection "
            f"evaluator rather than escalating to a human.",
        )

    # ------------------------------------------------------------------
    # Approvable: CLEAN or HAS_HOOKS with MERGEABLE. HAS_HOOKS is not a
    # conflict and does not block approval.
    # ------------------------------------------------------------------
    if merge_state_status in _APPROVABLE_STATUSES and mergeable == "MERGEABLE":
        return _decision(ROUTE_APPROVED)

    return _fail(
        "unexpected_mergeability_combination",
        f"mergeable={mergeable}, merge_state_status={merge_state_status} did not "
        f"match any known routing branch.",
    )
