"""
Production consumer module for impl-review-loop Step 5 routing.

Issue #777: Replaces shadow router helpers in test files with this importable
pure-function consumer.  Import-time side effects are forbidden: no gh, git,
network, or subprocess calls are made at module load or function call.

Public API
----------
route_loop_verdict_v2(loop_verdict, test_verdict=None) -> RouteDecision

RouteDecision fields
---------------------
route:            one of the ROUTE_* constants below
fail_closed:      True when a missing / unknown / mismatched input forced a
                  safe-default (non-actionable) outcome
reason_code:      machine-readable short code explaining the outcome, or None
selected_action:  the resolved required_auto_actions[] entry (dict) or None
rerun_required:   dict with boolean keys 'verification' and 'pr_review'
errors:           tuple of human-readable error strings (empty on success)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


# ---------------------------------------------------------------------------
# Route constants
# ---------------------------------------------------------------------------

ROUTE_APPROVED = "approved"
ROUTE_CONTINUE_LOOP = "continue_loop"
ROUTE_TO_UPDATE_BRANCH = "route_to_update_branch"
ROUTE_TO_BODY_ONLY_ACTION = "route_to_body_only_action"
ROUTE_FAIL_CLOSED = "fail_closed"

# ---------------------------------------------------------------------------
# Canonical required_auto_actions kind x executor x skill matrix
# ---------------------------------------------------------------------------

# The ONLY valid combination that routes to update_branch:
_CANONICAL_UPDATE_BRANCH_KIND = "update_branch"
_CANONICAL_UPDATE_BRANCH_EXECUTOR = "implementation-worker"
# NOTE: "implement-issue.update_branch" (with subcommand) is required.
#       "implement-issue" alone (no subcommand) is fail-closed per AC4.
_CANONICAL_UPDATE_BRANCH_SKILL = "implement-issue.update_branch"
_CANONICAL_UPDATE_BRANCH_BLOCKING_MERGE_READY = True
_CANONICAL_UPDATE_BRANCH_MECHANICAL = True

# Body-only action kinds (do not change branch HEAD).
_BODY_ONLY_ACTION_KINDS: frozenset[str] = frozenset({
    "ensure_closing_keyword",
    "update_pr_body_hygiene",
})

# apply_pr_review_fix_delta is pr-review-judge schema; it is NOT accepted here.
_REJECTED_ACTION_KINDS: frozenset[str] = frozenset({
    "apply_pr_review_fix_delta",
})

# The full set of kinds that this consumer knows about:
_KNOWN_ACTION_KINDS: frozenset[str] = (
    frozenset({_CANONICAL_UPDATE_BRANCH_KIND})
    | _BODY_ONLY_ACTION_KINDS
)

# merge_state_status values that block the update_branch path:
_BEHIND_BLOCKING_STATUSES: frozenset[str] = frozenset({
    "UNKNOWN",
    "CONFLICTING",
    "DIRTY",
    "UNSTABLE",
    "BLOCKED",
    "CLEAN",
})


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RouteDecision:
    route: Literal[
        "approved",
        "continue_loop",
        "route_to_update_branch",
        "route_to_body_only_action",
        "fail_closed",
    ]
    fail_closed: bool
    reason_code: str | None
    selected_action: dict[str, Any] | None
    rerun_required: dict[str, bool]
    errors: tuple[str, ...]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fail(reason_code: str, *error_msgs: str) -> RouteDecision:
    return RouteDecision(
        route=ROUTE_FAIL_CLOSED,
        fail_closed=True,
        reason_code=reason_code,
        selected_action=None,
        rerun_required={"verification": False, "pr_review": False},
        errors=tuple(error_msgs),
    )


def _ok_approved() -> RouteDecision:
    return RouteDecision(
        route=ROUTE_APPROVED,
        fail_closed=False,
        reason_code=None,
        selected_action=None,
        rerun_required={"verification": False, "pr_review": False},
        errors=(),
    )


def _ok_continue() -> RouteDecision:
    return RouteDecision(
        route=ROUTE_CONTINUE_LOOP,
        fail_closed=False,
        reason_code=None,
        selected_action=None,
        rerun_required={"verification": False, "pr_review": False},
        errors=(),
    )


def _ok_update_branch(action: dict[str, Any]) -> RouteDecision:
    return RouteDecision(
        route=ROUTE_TO_UPDATE_BRANCH,
        fail_closed=False,
        reason_code=None,
        selected_action=action,
        rerun_required={"verification": True, "pr_review": True},
        errors=(),
    )


def _ok_body_only(action: dict[str, Any]) -> RouteDecision:
    return RouteDecision(
        route=ROUTE_TO_BODY_ONLY_ACTION,
        fail_closed=False,
        reason_code=None,
        selected_action=action,
        rerun_required={"verification": False, "pr_review": True},
        errors=(),
    )


# ---------------------------------------------------------------------------
# AC7: schema validation for required_auto_actions format
# ---------------------------------------------------------------------------

def _validate_required_auto_actions_schema(raw: Any) -> str | None:
    """Return a reason_code string if schema is invalid, else None.

    Valid: list of dicts (array-of-objects).
    Invalid:
      - string-list  e.g. ["update_branch"]
      - V1 recommendations field (dict with 'recommendations' key)
      - camelCase top-level mergeStateStatus (signals wrong schema version)
      - non-list / None
    """
    if raw is None:
        return "schema_invalid_required_auto_actions_null"

    if isinstance(raw, dict):
        # Looks like a V1 recommendations object embedded under wrong key
        return "schema_invalid_required_auto_actions_is_dict"

    if not isinstance(raw, list):
        return "schema_invalid_required_auto_actions_not_list"

    for item in raw:
        if isinstance(item, str):
            # string-list  e.g. ["update_branch"]
            return "schema_invalid_required_auto_actions_string_list"
        if not isinstance(item, dict):
            return "schema_invalid_required_auto_actions_item_not_dict"

    return None


def _validate_mergeability_schema(loop_verdict: Mapping[str, Any]) -> str | None:
    """Return reason_code if mergeability sub-schema is invalid."""
    # camelCase mergeStateStatus at top level signals wrong schema version
    if "mergeStateStatus" in loop_verdict:
        return "schema_invalid_camel_case_mergeStateStatus"
    # V1 recommendations field signals wrong schema version
    if "recommendations" in loop_verdict:
        return "schema_invalid_v1_recommendations_field"
    return None


# ---------------------------------------------------------------------------
# Core branch_behind_main / merge_state_status invariant (AC6)
# ---------------------------------------------------------------------------

def _check_behind_invariant(
    branch_behind_main: Any,
    merge_state_status: Any,
) -> tuple[bool, str | None]:
    """Evaluate AC6 invariant.

    Returns (is_behind: bool, reason_code: str | None).
    is_behind is True ONLY for (branch_behind_main is True AND merge_state_status == "BEHIND").
    All other combinations return (False, reason_code) where reason_code explains the mismatch.
    """
    # Validate branch_behind_main type
    if not isinstance(branch_behind_main, bool):
        if branch_behind_main is not None:
            return False, "branch_behind_main_not_bool"
        # None / missing → not behind
        return False, None

    if merge_state_status not in ("BEHIND", "CLEAN", "UNKNOWN", "CONFLICTING",
                                   "DIRTY", "UNSTABLE", "BLOCKED", "DRAFT",
                                   "HAS_HOOKS", None):
        # Unknown status string → fail-closed
        return False, f"merge_state_status_unknown_value:{merge_state_status}"

    if branch_behind_main is True and merge_state_status == "BEHIND":
        return True, None

    if branch_behind_main is True and merge_state_status != "BEHIND":
        # Inconsistent: test says behind but merge_state_status disagrees
        return False, f"branch_behind_true_but_merge_state_status_not_behind:{merge_state_status}"

    if branch_behind_main is False and merge_state_status == "BEHIND":
        # Inconsistent: merge_state_status says behind but test says not
        return False, "branch_behind_false_but_merge_state_status_BEHIND"

    # branch_behind_main=False, status != BEHIND → not behind, no error
    return False, None


# ---------------------------------------------------------------------------
# update_branch action validation (AC4 / AC5)
# ---------------------------------------------------------------------------

def _validate_update_branch_action(
    action: dict[str, Any],
    reviewed_head_sha: Any,
) -> str | None:
    """Validate a single update_branch action object.

    Returns reason_code if invalid, None if valid.
    Checks: executor, skill, blocking_merge_ready, mechanical, expected_head_sha.
    """
    # executor
    executor = action.get("executor")
    if executor is None:
        return "missing_executor"
    if executor != _CANONICAL_UPDATE_BRANCH_EXECUTOR:
        return f"mismatched_executor:{executor}"

    # skill — "implement-issue" without subcommand is fail-closed (AC4)
    skill = action.get("skill")
    if skill is None:
        return "missing_skill"
    if skill == "implement-issue":
        # Subcommand-less form is explicitly fail-closed per AC4
        return "skill_missing_subcommand_implement-issue"
    if skill != _CANONICAL_UPDATE_BRANCH_SKILL:
        return f"mismatched_skill:{skill}"

    # blocking_merge_ready
    blocking_merge_ready = action.get("blocking_merge_ready")
    if blocking_merge_ready is None:
        return "missing_blocking_merge_ready"
    if not isinstance(blocking_merge_ready, bool):
        return f"blocking_merge_ready_not_bool:{blocking_merge_ready!r}"
    if blocking_merge_ready is not _CANONICAL_UPDATE_BRANCH_BLOCKING_MERGE_READY:
        return f"mismatched_blocking_merge_ready:{blocking_merge_ready}"

    # mechanical (optional field — if present must be True, absence is also accepted)
    mechanical = action.get("mechanical")
    if mechanical is not None:
        if not isinstance(mechanical, bool):
            return f"mechanical_not_bool:{mechanical!r}"
        if mechanical is not _CANONICAL_UPDATE_BRANCH_MECHANICAL:
            return f"mismatched_mechanical:{mechanical}"

    # expected_head_sha (AC5)
    expected_head_sha = action.get("expected_head_sha")
    if expected_head_sha is None:
        return "missing_expected_head_sha"
    if not isinstance(expected_head_sha, str) or not expected_head_sha:
        return "expected_head_sha_empty_or_not_str"

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def route_loop_verdict_v2(
    loop_verdict: Mapping[str, Any],
    test_verdict: Mapping[str, Any] | None = None,
) -> RouteDecision:
    """Deterministic, side-effect-free routing for impl-review-loop Step 5.

    Parameters
    ----------
    loop_verdict:
        LOOP_VERDICT_V2 dict as emitted by pr-review-judge.
    test_verdict:
        Optional TEST_VERDICT_MACHINE/v1 dict.  When supplied, the
        branch_behind_main field is cross-checked against
        loop_verdict.mergeability.merge_state_status (AC6 invariant).

    Returns
    -------
    RouteDecision
    """
    # ------------------------------------------------------------------
    # Step 0: top-level schema guard (AC7)
    # ------------------------------------------------------------------
    schema_err = _validate_mergeability_schema(loop_verdict)
    if schema_err:
        return _fail(schema_err, f"Top-level schema error: {schema_err}")

    # ------------------------------------------------------------------
    # Step 1: extract primary fields
    # ------------------------------------------------------------------
    verdict_str = loop_verdict.get("verdict", "")
    merge_ready: Any = loop_verdict.get("merge_ready")
    reviewed_head_sha: Any = loop_verdict.get("reviewed_head_sha")
    mergeability: Any = loop_verdict.get("mergeability", {})

    if not isinstance(mergeability, dict):
        mergeability = {}

    merge_state_status: Any = mergeability.get("merge_state_status")

    # ------------------------------------------------------------------
    # Step 2: validate required_auto_actions schema (AC7)
    # ------------------------------------------------------------------
    raw_actions = loop_verdict.get("required_auto_actions")
    schema_err = _validate_required_auto_actions_schema(raw_actions)
    if schema_err:
        return _fail(schema_err, f"required_auto_actions schema error: {schema_err}")

    required_auto_actions: list[dict[str, Any]] = raw_actions  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Step 3: REQUEST_CHANGES → continue loop
    # ------------------------------------------------------------------
    if verdict_str == "REQUEST_CHANGES":
        return _ok_continue()

    # ------------------------------------------------------------------
    # Step 4: APPROVE gate entry
    # ------------------------------------------------------------------
    if verdict_str != "APPROVE":
        return _fail(
            "verdict_not_approve_or_request_changes",
            f"Unexpected verdict value: {verdict_str!r}",
        )

    # ------------------------------------------------------------------
    # Step 5: AC6 — branch_behind_main × merge_state_status invariant
    # ------------------------------------------------------------------
    branch_behind_main: Any = None
    if test_verdict is not None:
        branch_behind_main = test_verdict.get("branch_behind_main")

    is_behind, behind_reason = _check_behind_invariant(branch_behind_main, merge_state_status)

    if behind_reason is not None and branch_behind_main is not None:
        # Only emit a fail-closed when test_verdict was supplied and inconsistent
        return _fail(
            f"branch_behind_invariant_violation:{behind_reason}",
            f"AC6 invariant violated: branch_behind_main={branch_behind_main!r}, "
            f"merge_state_status={merge_state_status!r}. Reason: {behind_reason}",
        )

    # ------------------------------------------------------------------
    # Step 6: required_auto_actions dispatch
    # ------------------------------------------------------------------
    if not required_auto_actions:
        # Empty actions list — check merge_ready gate
        if is_behind:
            # BEHIND without any action is inconsistent
            return _fail(
                "behind_without_update_branch_action",
                "merge_state_status is BEHIND but required_auto_actions is empty. "
                "Expected an update_branch action.",
            )
        if merge_ready is not True:
            return _fail(
                "merge_ready_not_true_with_empty_actions",
                f"required_auto_actions == [] but merge_ready={merge_ready!r} (expected True).",
            )
        return _ok_approved()

    # Non-empty required_auto_actions
    if len(required_auto_actions) > 1:
        # Multiple actions — fail-closed (ambiguous dispatch)
        return _fail(
            "multiple_required_auto_actions",
            f"Only one action supported at a time; got {len(required_auto_actions)} actions.",
        )

    action = required_auto_actions[0]
    kind = action.get("kind")

    # AC7: unknown kind
    if kind is None:
        return _fail("missing_kind", "required_auto_actions[0].kind is missing.")

    if kind in _REJECTED_ACTION_KINDS:
        # apply_pr_review_fix_delta is pr-review-judge schema, not accepted here
        return _fail(
            f"rejected_action_kind:{kind}",
            f"Action kind '{kind}' is not accepted in this routing context "
            f"(it belongs to the pr-review-judge schema).",
        )

    if kind not in _KNOWN_ACTION_KINDS:
        return _fail(
            f"unknown_kind:{kind}",
            f"required_auto_actions[0].kind '{kind}' is not in the known set "
            f"{sorted(_KNOWN_ACTION_KINDS)}.",
        )

    # Body-only action
    if kind in _BODY_ONLY_ACTION_KINDS:
        if is_behind:
            return _fail(
                "body_only_action_while_behind",
                f"Action kind '{kind}' is body-only but merge_state_status is BEHIND. "
                f"Expected update_branch action first.",
            )
        return _ok_body_only(action)

    # update_branch action
    if kind == _CANONICAL_UPDATE_BRANCH_KIND:
        # AC6: merge_state_status must be BEHIND
        if not is_behind:
            return _fail(
                "update_branch_without_behind_status",
                f"Action kind is 'update_branch' but merge_state_status={merge_state_status!r} "
                f"(must be 'BEHIND'). "
                f"true + BEHIND is the only combination that routes to update_branch.",
            )

        # AC4 / AC5: validate full action matrix
        action_err = _validate_update_branch_action(action, reviewed_head_sha)
        if action_err:
            return _fail(
                f"update_branch_action_invalid:{action_err}",
                f"update_branch action failed matrix validation: {action_err}. "
                f"Required: executor=implementation-worker, skill=implement-issue.update_branch, "
                f"blocking_merge_ready=true, mechanical=true, expected_head_sha=<non-null>.",
            )

        return _ok_update_branch(action)

    # Should be unreachable given the guards above
    return _fail(
        "internal_routing_error",
        f"Unhandled kind '{kind}' after all guards.",
    )
