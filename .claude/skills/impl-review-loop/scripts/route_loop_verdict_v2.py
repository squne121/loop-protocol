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

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping


# ---------------------------------------------------------------------------
# Route constants
# ---------------------------------------------------------------------------

ROUTE_APPROVED = "approved"
ROUTE_CONTINUE_LOOP = "continue_loop"
ROUTE_TO_UPDATE_BRANCH = "route_to_update_branch"
ROUTE_TO_BODY_ONLY_ACTION = "route_to_body_only_action"
ROUTE_CONFLICT_HARD_STOP = "conflict_hard_stop"
ROUTE_FAIL_CLOSED = "fail_closed"

# ---------------------------------------------------------------------------
# #1860 Owner Decision: the ONLY hard stops are real Git conflicts / GitHub
# mergeability, never semantic planning / overlap / contract artifacts.
#
# GitHub's actual enums (see GraphQL schema):
#   PullRequest.mergeable        (MergeableState):  CONFLICTING | MERGEABLE | UNKNOWN
#   PullRequest.mergeStateStatus (MergeStateStatus): BEHIND | BLOCKED | CLEAN | DIRTY |
#                                                     DRAFT | HAS_HOOKS | UNKNOWN | UNSTABLE
#
# NOTE: "CONFLICTING" is a valid value of `mergeable` but is NOT a valid value
# of `merge_state_status` (that enum member does not exist on GitHub). A
# LOOP_VERDICT_V2 payload that sets merge_state_status == "CONFLICTING" is
# therefore malformed input, not a legitimate conflict signal, and must be
# rejected as schema_invalid rather than silently treated as a conflict.
# ---------------------------------------------------------------------------

_VALID_MERGEABLE_VALUES: frozenset[str] = frozenset({
    "CONFLICTING", "MERGEABLE", "UNKNOWN",
})

_VALID_MERGE_STATE_STATUS_VALUES: frozenset[str] = frozenset({
    "BEHIND", "BLOCKED", "CLEAN", "DIRTY", "DRAFT", "HAS_HOOKS", "UNKNOWN", "UNSTABLE",
})

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
        "conflict_hard_stop",
        "fail_closed",
    ]
    fail_closed: bool
    reason_code: str | None
    selected_action: Mapping[str, Any] | None
    rerun_required: Mapping[str, bool]
    errors: tuple[str, ...]

    def __post_init__(self) -> None:
        # Wrap mutable dicts in MappingProxyType for immutability (Extra 6)
        if isinstance(self.selected_action, dict):
            object.__setattr__(self, "selected_action", MappingProxyType(self.selected_action))
        if isinstance(self.rerun_required, dict):
            object.__setattr__(self, "rerun_required", MappingProxyType(self.rerun_required))


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


def _ok_conflict_hard_stop(reason_code: str) -> RouteDecision:
    """The ONE class of hard stop retained by #1860 Owner Decision: real Git
    conflict / GitHub-reported non-mergeability. This is NOT a fail-closed
    safe-default; it is a deterministic, intentional escalation to the
    CONFLICTING PR Escalation Runbook.
    """
    return RouteDecision(
        route=ROUTE_CONFLICT_HARD_STOP,
        fail_closed=False,
        reason_code=reason_code,
        selected_action=None,
        rerun_required={"verification": False, "pr_review": False},
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

    mergeability = loop_verdict.get("mergeability", {})
    if not isinstance(mergeability, dict):
        return None

    mergeable = mergeability.get("mergeable")
    if mergeable is not None and mergeable not in _VALID_MERGEABLE_VALUES:
        return f"schema_invalid_mergeable_value:{mergeable}"

    merge_state_status = mergeability.get("merge_state_status")
    if merge_state_status is not None and merge_state_status not in _VALID_MERGE_STATE_STATUS_VALUES:
        # "CONFLICTING" is a valid `mergeable` value but NOT a valid GitHub
        # MergeStateStatus enum member (GitHub's real enum has no such value).
        # A payload that puts it here is malformed, not a legitimate conflict.
        return f"schema_invalid_merge_state_status_value:{merge_state_status}"

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

    if merge_state_status not in (*_VALID_MERGE_STATE_STATUS_VALUES, None):
        # Unknown status string → fail-closed (schema validation upstream
        # already rejects this for the top-level call path; this guard is
        # defense-in-depth for direct unit-level calls of this helper)
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

    # mechanical — required, must be True (Blocker 2)
    mechanical = action.get("mechanical")
    if mechanical is None:
        return "missing_mechanical"
    if not isinstance(mechanical, bool):
        return f"mechanical_not_bool:{mechanical!r}"
    if mechanical is not True:
        return f"mismatched_mechanical:{mechanical}"

    # expected_head_sha (AC5) — must be non-null and match reviewed_head_sha (Blocker 1)
    expected_head_sha = action.get("expected_head_sha")
    if expected_head_sha is None:
        return "missing_expected_head_sha"
    if not isinstance(expected_head_sha, str) or not expected_head_sha:
        return "expected_head_sha_empty_or_not_str"
    if expected_head_sha != reviewed_head_sha:
        return "mismatched_expected_head_sha"

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
    mergeable: Any = mergeability.get("mergeable")

    # ------------------------------------------------------------------
    # Step 1.5: conflict hard stop (#1860 Owner Decision — the ONLY hard
    # stop retained by this router). Applies regardless of verdict /
    # required_auto_actions: a real Git conflict or DIRTY merge state must
    # never be routed past this point.
    #
    #   mergeable == "CONFLICTING"        → conflict hard stop
    #   merge_state_status == "DIRTY"     → conflict hard stop
    #   mergeable == "UNKNOWN"            → NOT a conflict (caller performs
    #                                       bounded retry, then warning)
    #   merge_state_status in {BLOCKED, BEHIND, UNSTABLE, DRAFT, HAS_HOOKS,
    #   UNKNOWN}                          → NOT a conflict
    # ------------------------------------------------------------------
    if mergeable == "CONFLICTING":
        return _ok_conflict_hard_stop("conflict_mergeable_CONFLICTING")

    if merge_state_status == "DIRTY":
        return _ok_conflict_hard_stop("conflict_merge_state_status_DIRTY")

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

    # Blocker 4: merge_state_status=BEHIND requires test_verdict with branch_behind_main key
    if merge_state_status == "BEHIND":
        if test_verdict is None:
            return _fail(
                "missing_branch_behind_main_for_BEHIND",
                "merge_state_status is BEHIND but test_verdict is None. "
                "branch_behind_main is required to validate the BEHIND state.",
            )
        if "branch_behind_main" not in test_verdict:
            return _fail(
                "missing_branch_behind_main_for_BEHIND",
                "merge_state_status is BEHIND but test_verdict.branch_behind_main key is absent. "
                "branch_behind_main is required to validate the BEHIND state.",
            )

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


# ---------------------------------------------------------------------------
# LOOP_VERDICT_V2 fenced-YAML-block extraction (P0-1 / step-5-mergeability-handling.md)
#
# Replaces the shell grep/sed extraction previously documented in
# step-5-mergeability-handling.md, which only inspected the FIRST fenced
# ```yaml block in a comment body. This enumerates ALL fenced yaml blocks
# and selects the one(s) whose parsed content contains a `LOOP_VERDICT_V2`
# key, taking the last match (most recently appended) as authoritative.
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402  (kept below dataclass/typing imports intentionally)

_FENCED_YAML_BLOCK_RE = _re.compile(
    r"```ya?ml\s*\n(.*?)```",
    _re.DOTALL,
)


def extract_latest_loop_verdict_v2(comment_body: str) -> tuple[dict[str, Any] | None, str | None]:
    """Enumerate all fenced ```yaml blocks in `comment_body` and return the
    parsed `LOOP_VERDICT_V2` mapping from the last block that contains it.

    Returns (loop_verdict_dict, error_reason_code). On success,
    error_reason_code is None. On failure, loop_verdict_dict is None and
    error_reason_code explains why (no matching block found, or a matching
    block failed to parse as YAML).
    """
    import yaml  # local import: keep module import-time side-effect free

    if not comment_body:
        return None, "no_comment_body"

    candidate_blocks = _FENCED_YAML_BLOCK_RE.findall(comment_body)
    if not candidate_blocks:
        return None, "no_fenced_yaml_block_found"

    last_match: dict[str, Any] | None = None
    any_parse_error = False

    for block_text in candidate_blocks:
        if "LOOP_VERDICT_V2" not in block_text:
            continue
        try:
            parsed = yaml.safe_load(block_text)
        except yaml.YAMLError:
            any_parse_error = True
            continue
        if not isinstance(parsed, dict):
            continue
        loop_verdict_v2 = parsed.get("LOOP_VERDICT_V2")
        if isinstance(loop_verdict_v2, dict):
            last_match = loop_verdict_v2

    if last_match is not None:
        return last_match, None

    if any_parse_error:
        return None, "malformed_yaml_in_loop_verdict_v2_block"

    return None, "no_loop_verdict_v2_key_in_any_fenced_block"


# ---------------------------------------------------------------------------
# CLI wrapper
#
# Usage:
#   uv run python3 route_loop_verdict_v2.py --body-file <comment.txt> \
#       [--test-verdict-file <test_verdict.json>]
#
# Prints a single JSON object to stdout describing the RouteDecision (plus
# an `extraction_error` field when the LOOP_VERDICT_V2 block could not be
# located/parsed). Exit code is always 0 for a completed, well-formed
# invocation -- fail_closed / conflict_hard_stop / extraction failures are
# all *data*, not process failures, and callers must branch on the JSON
# `route` field rather than on process exit code. Exit code is non-zero
# only for genuine invocation errors (e.g. --body-file not found).
# ---------------------------------------------------------------------------

def _cli_main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="Extract LOOP_VERDICT_V2 from a PR comment body and route it (Step 5).",
    )
    parser.add_argument("--body-file", required=True, help="Path to file containing the PR comment body text.")
    parser.add_argument("--test-verdict-file", required=False, default=None,
                         help="Optional path to a JSON file containing TEST_VERDICT_MACHINE/v1.")
    args = parser.parse_args(argv)

    try:
        comment_body = open(args.body_file, encoding="utf-8").read()
    except OSError as exc:
        print(json.dumps({"error": f"could not read --body-file: {exc}"}), file=sys.stderr)
        return 2

    test_verdict: dict[str, Any] | None = None
    if args.test_verdict_file:
        try:
            with open(args.test_verdict_file, encoding="utf-8") as fh:
                test_verdict = json.load(fh)
        except OSError as exc:
            print(json.dumps({"error": f"could not read --test-verdict-file: {exc}"}), file=sys.stderr)
            return 2

    loop_verdict, extraction_error = extract_latest_loop_verdict_v2(comment_body)

    if loop_verdict is None:
        output = {
            "route": ROUTE_FAIL_CLOSED,
            "fail_closed": True,
            "reason_code": extraction_error,
            "selected_action": None,
            "rerun_required": {"verification": False, "pr_review": False},
            "errors": [f"LOOP_VERDICT_V2 extraction failed: {extraction_error}"],
            "extraction_error": extraction_error,
        }
        print(json.dumps(output))
        return 0

    decision = route_loop_verdict_v2(loop_verdict, test_verdict=test_verdict)
    output = {
        "route": decision.route,
        "fail_closed": decision.fail_closed,
        "reason_code": decision.reason_code,
        "selected_action": dict(decision.selected_action) if decision.selected_action is not None else None,
        "rerun_required": dict(decision.rerun_required),
        "errors": list(decision.errors),
        "extraction_error": None,
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_cli_main())
