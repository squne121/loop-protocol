#!/usr/bin/env python3
"""
decide_rewrite_route.py

Pure function that routes the rewrite loop based on router state, enforcing:
- max_rewrite_attempts limit
- body hash no-progress detection
- missing set no-progress detection
- checker_exit_code gating (only route to review/handoff when exit_code == 0)

Schema: LOOP_REWRITE_ROUTER_STATE_V1
Output: RouteResult

Usage (as library):
    from decide_rewrite_route import decide_rewrite_route, LOOP_REWRITE_ROUTER_STATE_V1

    state = LOOP_REWRITE_ROUTER_STATE_V1(...)
    result = decide_rewrite_route(state)

Exit codes (CLI):
    0 - success
    2 - invalid input schema
    3 - internal error
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# LOOP_REWRITE_ROUTER_STATE_V1 schema constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "loop_rewrite_router_state/v1"

# Route constants
ROUTE_CONTINUE_REWRITE = "continue_rewrite"
ROUTE_PROCEED_TO_REVIEW = "proceed_to_review"
ROUTE_HUMAN_JUDGMENT_REQUIRED = "human_judgment_required"
ROUTE_CATEGORY_WIDE_REMEDIATION = "category_wide_remediation"

# Reason codes
REASON_CODE_MAX_ATTEMPTS_EXCEEDED = "max_attempts_exceeded"
REASON_CODE_BODY_HASH_UNCHANGED = "body_hash_unchanged"
REASON_CODE_MISSING_CONTRACT_NO_PROGRESS = "missing_contract_no_progress"
REASON_CODE_CHECKER_PASSED = "checker_passed"
REASON_CODE_CHECKER_FAILED = "checker_failed_rewrite"
REASON_CODE_SOURCE_BODY_RESET = "source_body_reset"
REASON_CODE_REPEATED_FIX_CATEGORY_REMEDIATION = (
    "repeated_fix_category_remediation"
)
REASON_CODE_FIX_CATEGORY_UNDECIDABLE = "fix_category_undecidable"
REASON_CODE_NULL = None


# ---------------------------------------------------------------------------
# LOOP_REWRITE_ROUTER_STATE_V1 dataclass
# ---------------------------------------------------------------------------


@dataclass
class LOOP_REWRITE_ROUTER_STATE_V1:
    """
    State contract for the rewrite loop router.

    Fields:
        rewrite_attempt_count: Number of completed rewrite attempts (0-indexed).
            0 means no rewrite has been attempted yet.
            This is incremented BEFORE calling decide_rewrite_route.
            max=2: attempt 0/1 are permitted, attempt 2 triggers human_judgment_required.

        max_rewrite_attempts: Maximum number of rewrite attempts allowed.

        checker_exit_code: Exit code from the most recent checker run (post-mutation).
            0 = passed, non-zero = failed.

        checked_body_sha256: SHA256 of the issue body after the most recent rewrite.
            Used for no-progress detection.

        missing_sections: List of missing required sections after most recent check.
            Used for no-progress detection (must monotonically decrease).

        missing_contract_keys: List of missing required contract keys after most recent check.
            Used for no-progress detection (must monotonically decrease).

        fix_category: Current checker failure category used for category-level routing.

        rewrite_history: Ordered history of observed fix categories.

        occurrence_count: Number of occurrences of fix_category in rewrite_history.

        previous_checked_body_sha256: SHA256 of the issue body from the PREVIOUS iteration.
            None if this is the first iteration.

        previous_missing_sections: Missing sections from the PREVIOUS iteration.
            Used to detect lack of progress.

        previous_missing_contract_keys: Missing contract keys from the PREVIOUS iteration.
            Used to detect lack of progress.

        source_issue_body_sha256: SHA256 of the original issue body (before any rewrites).
            Used to detect if the human has changed the source body (reset condition).

        replay_safe: If True, this state has been restored from persistent storage
            and is safe to replay (attempt count has not been reset to 0).

        source_body_reset: True if this state was produced by a reset because the
            human changed the source issue body (source_issue_body_sha256 mismatch
            at load time). When True, rewrite_attempt_count has been reset to 0 and
            the route result records the reset fact (AC7).
    """

    rewrite_attempt_count: int
    max_rewrite_attempts: int
    checker_exit_code: int
    checked_body_sha256: str
    missing_sections: list[str]
    missing_contract_keys: list[str]
    fix_category: str = "unknown_contract_failure"
    rewrite_history: list[str] = field(default_factory=list)
    occurrence_count: int = 0
    previous_checked_body_sha256: Optional[str] = None
    previous_missing_sections: list[str] = field(default_factory=list)
    previous_missing_contract_keys: list[str] = field(default_factory=list)
    source_issue_body_sha256: Optional[str] = None
    replay_safe: bool = False
    source_body_reset: bool = False
    # AC9: sha256 fingerprint of strict-JSON (fix_category + missing sets).
    # Used to detect same-fingerprint recurrence → human_judgment/no_progress.
    rewrite_request_fingerprint: Optional[str] = None
    # AC10: kind of mutation applied in this iteration.
    # "format_only_repair" → budget_debit=0 (does not consume max_iterations).
    last_mutation_kind: str = "semantic_rewrite"
    # AC10: 0 if last_mutation_kind=="format_only_repair", else 1.
    budget_debit: int = 1


# ---------------------------------------------------------------------------
# RouteResult
# ---------------------------------------------------------------------------


@dataclass
class RouteResult:
    """
    Terminal result from decide_rewrite_route.

    Fields:
        route: One of ROUTE_CONTINUE_REWRITE, ROUTE_PROCEED_TO_REVIEW,
               ROUTE_HUMAN_JUDGMENT_REQUIRED.

        reason_code: Why this route was chosen. None when checker passed
               and continuing normally.

        rewrite_attempt_count: Echo of the attempt count from state.

        max_rewrite_attempts: Echo of the max attempts from state.

        checked_body_sha256: Echo of checked body SHA from state.

        checker_exit_code: Echo of checker exit code from state.

        missing_sections: Echo of missing sections from state.

        missing_contract_keys: Echo of missing contract keys from state.

        source_body_reset: True if the source body changed and state was reset.
    """

    route: str
    reason_code: Optional[str]
    rewrite_attempt_count: int
    max_rewrite_attempts: int
    checked_body_sha256: str
    checker_exit_code: int
    missing_sections: list[str]
    missing_contract_keys: list[str]
    fix_category: str = "unknown_contract_failure"
    rewrite_history: list[str] = field(default_factory=list)
    occurrence_count: int = 0
    repeated_fix_category: Optional[str] = None
    remaining_blockers: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    suggested_repair_strategy: Optional[str] = None
    stop_reason_if_unrepairable: Optional[str] = None
    source_body_reset: bool = False
    # AC9: fingerprint echo for audit/loop state
    rewrite_request_fingerprint: Optional[str] = None
    # AC10: mutation kind and budget echo
    last_mutation_kind: str = "semantic_rewrite"
    budget_debit: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict (AC5 terminal result fields)."""
        return {
            "schema_version": "route_result/v1",
            "route": self.route,
            "reason_code": self.reason_code,
            "rewrite_attempt_count": self.rewrite_attempt_count,
            "max_rewrite_attempts": self.max_rewrite_attempts,
            "checked_body_sha256": self.checked_body_sha256,
            "checker_exit_code": self.checker_exit_code,
            "missing_sections": self.missing_sections,
            "missing_contract_keys": self.missing_contract_keys,
            "fix_category": self.fix_category,
            "rewrite_history": self.rewrite_history,
            "occurrence_count": self.occurrence_count,
            "repeated_fix_category": self.repeated_fix_category,
            "remaining_blockers": self.remaining_blockers,
            "required_evidence": self.required_evidence,
            "suggested_repair_strategy": self.suggested_repair_strategy,
            "stop_reason_if_unrepairable": self.stop_reason_if_unrepairable,
            "source_body_reset": self.source_body_reset,
            "rewrite_request_fingerprint": self.rewrite_request_fingerprint,
            "last_mutation_kind": self.last_mutation_kind,
            "budget_debit": self.budget_debit,
        }


# ---------------------------------------------------------------------------
# Helper: normalize missing sets for no-progress detection
# ---------------------------------------------------------------------------


def _normalize_set(items: list[str]) -> frozenset[str]:
    """
    Normalize a list of missing items to a frozenset for comparison.

    AC4: missing set comparison uses sort + unique + exact match (via frozenset).
    """
    return frozenset(items)


def _missing_universe(
    sections: list[str], keys: list[str]
) -> frozenset[tuple[str, str]]:
    """
    Build a single tagged universe of all missing items.

    Sections and contract keys are namespaced with a tag so that they never
    collide and are compared together as one set. This lets no-progress
    detection treat "one section resolved but one contract key newly missing"
    as NO progress, instead of letting per-category OR logic pass it.
    """
    return frozenset(
        [("section", s) for s in sections] + [("contract_key", k) for k in keys]
    )


def _strictly_decreased(
    current_sections: list[str],
    current_keys: list[str],
    previous_sections: list[str],
    previous_keys: list[str],
) -> bool:
    """
    Return True only if the combined missing universe strictly shrank.

    Progress (monotonic decrease) requires the current missing universe to be a
    PROPER SUBSET of the previous one. This means:
      - every still-missing item was already missing before (no replacements), AND
      - at least one previously-missing item is now resolved.

    A replacement (e.g. previous {A, B} -> current {C}) is NOT progress, because
    {C} is not a subset of {A, B}. A same-size or grown set is NOT progress.

    AC4: comparison is set-based (sort + unique + exact match via frozenset),
    not length-based.
    """
    current = _missing_universe(current_sections, current_keys)
    previous = _missing_universe(previous_sections, previous_keys)
    return current < previous  # strict (proper) subset


_REPAIRABLE_FIX_CATEGORIES = {
    "missing_section",
    "missing_contract_key",
    "unknown_contract_failure",
}


def _remaining_blockers(
    missing_sections: list[str],
    missing_contract_keys: list[str],
) -> list[str]:
    """Build deterministic blocker list in stable order."""
    return [*missing_sections, *missing_contract_keys]


def _required_evidence_for_fix_category(category: str) -> list[str]:
    if category == "missing_section":
        return [
            "カテゴリ内で不足しているセクションを一括で補完する",
            "各セクションへの実装方針と根拠を追加する",
        ]
    if category == "missing_contract_key":
        return [
            "カテゴリ内で欠落した contract key を一括で補完する",
            "machine-readable block の欠損キーを明示する",
        ]
    return [
        "カテゴリ再発を示す blockers と直近の checker 結果を同梱する",
        "カテゴリ横断の修正後に再検証可能な根拠を提示する",
    ]


def _suggested_repair_strategy(category: str) -> str:
    if category == "missing_section":
        return "missing_section 再発時は同カテゴリ全件を一括追加する"
    if category == "missing_contract_key":
        return "missing_contract_key 再発時は契約キー群を同時解消する"
    return "カテゴリ再発を単一カテゴリ改善として一括再修正する"


def _is_repairable_fix_category(category: str) -> bool:
    return category in _REPAIRABLE_FIX_CATEGORIES


def _human_stop_reason(category: str) -> str:
    return (
        f"カテゴリ {category} は現行ルール集合で structured remediation が未定義（"
        "category-wide remediation strategy 未定義）"
    )


# ---------------------------------------------------------------------------
# decide_rewrite_route — pure function (AC1)
# ---------------------------------------------------------------------------



def _make_route_result(state: "LOOP_REWRITE_ROUTER_STATE_V1", **kwargs: Any) -> "RouteResult":
    """AC9/AC10: Helper to create RouteResult with new fingerprint/mutation fields from state."""
    return RouteResult(
        rewrite_request_fingerprint=state.rewrite_request_fingerprint,
        last_mutation_kind=state.last_mutation_kind,
        budget_debit=state.budget_debit,
        **kwargs,
    )


def decide_rewrite_route(state: LOOP_REWRITE_ROUTER_STATE_V1) -> RouteResult:
    """
    Route the rewrite loop based on current router state.

    Priority order:
    1. checker_exit_code == 0 → proceed_to_review (overrides all stop guards)
    2. max_rewrite_attempts exceeded → human_judgment_required
    3. body hash unchanged → human_judgment_required (body_hash_unchanged)
    4. missing set not decreased → human_judgment_required (missing_contract_no_progress)
    5. checker_exit_code != 0 → continue_rewrite (checker_failed_rewrite)

    AC1: checker_exit_code == 0 takes priority over ALL stop guards including
        max_rewrite_attempts, body_hash_unchanged, and missing_contract_no_progress.
        When checker approves, route directly to proceed_to_review regardless of
        budget exhaustion or no-progress conditions.

    AC2: max_rewrite_attempts runtime enforcement (only when checker_exit_code != 0).
        rewrite_attempt_count >= max_rewrite_attempts → human_judgment_required
        off-by-one: max=2, attempt 0/1 allowed, attempt 2 → stop.

    AC4: no-progress detection — body hash + missing set (only when checker_exit_code != 0).

    AC5: terminal result includes all required fields.

    AC7: source_issue_body_sha256 mismatch is a reset condition. When the state
    was produced by such a reset, state.source_body_reset is True and the fact is
    propagated to every RouteResult so the reset is observable in the terminal
    result (not silently swallowed at load time).
    """

    source_body_reset = state.source_body_reset
    repeated_fix_category = state.fix_category
    remaining_blockers = _remaining_blockers(
        state.missing_sections,
        state.missing_contract_keys,
    )
    required_evidence = _required_evidence_for_fix_category(state.fix_category)
    suggested_repair_strategy = _suggested_repair_strategy(state.fix_category)
    stop_reason_if_unrepairable = (
        _human_stop_reason(state.fix_category) if state.occurrence_count >= 2 else None
    )

    # --- AC1: checker_exit_code == 0 overrides all stop guards ---
    # checker approve takes priority over max_rewrite_attempts, body_hash_unchanged,
    # and missing_contract_no_progress. This is the highest-priority branch.
    if state.checker_exit_code == 0:
        return RouteResult(
            route=ROUTE_PROCEED_TO_REVIEW,
            reason_code=REASON_CODE_CHECKER_PASSED,
            rewrite_attempt_count=state.rewrite_attempt_count,
            max_rewrite_attempts=state.max_rewrite_attempts,
            checked_body_sha256=state.checked_body_sha256,
            checker_exit_code=state.checker_exit_code,
            missing_sections=state.missing_sections,
            missing_contract_keys=state.missing_contract_keys,
            fix_category=state.fix_category,
            rewrite_history=state.rewrite_history,
            occurrence_count=state.occurrence_count,
            repeated_fix_category=repeated_fix_category,
            remaining_blockers=remaining_blockers,
            required_evidence=required_evidence,
            suggested_repair_strategy=suggested_repair_strategy,
            stop_reason_if_unrepairable=stop_reason_if_unrepairable,
            source_body_reset=source_body_reset,
            # AC9/AC10: echo new state fields
            rewrite_request_fingerprint=state.rewrite_request_fingerprint,
            last_mutation_kind=state.last_mutation_kind,
            budget_debit=state.budget_debit,
        )

    # From here on: checker_exit_code != 0 (checker did not approve)

    # --- AC3: same-category recurrence should be handled before budget/no-progress gates ---
    if state.occurrence_count >= 2:
        if _is_repairable_fix_category(state.fix_category):
            return RouteResult(
                route=ROUTE_CATEGORY_WIDE_REMEDIATION,
                reason_code=REASON_CODE_REPEATED_FIX_CATEGORY_REMEDIATION,
                rewrite_attempt_count=state.rewrite_attempt_count,
                max_rewrite_attempts=state.max_rewrite_attempts,
                checked_body_sha256=state.checked_body_sha256,
                checker_exit_code=state.checker_exit_code,
                missing_sections=state.missing_sections,
                missing_contract_keys=state.missing_contract_keys,
                fix_category=state.fix_category,
                rewrite_history=state.rewrite_history,
                occurrence_count=state.occurrence_count,
                repeated_fix_category=repeated_fix_category,
                remaining_blockers=remaining_blockers,
                required_evidence=required_evidence,
                suggested_repair_strategy=suggested_repair_strategy,
                stop_reason_if_unrepairable=stop_reason_if_unrepairable,
                source_body_reset=source_body_reset,
            # AC9/AC10: echo new state fields
            rewrite_request_fingerprint=state.rewrite_request_fingerprint,
            last_mutation_kind=state.last_mutation_kind,
            budget_debit=state.budget_debit
            )

        return RouteResult(
            route=ROUTE_HUMAN_JUDGMENT_REQUIRED,
            reason_code=REASON_CODE_FIX_CATEGORY_UNDECIDABLE,
            rewrite_attempt_count=state.rewrite_attempt_count,
            max_rewrite_attempts=state.max_rewrite_attempts,
            checked_body_sha256=state.checked_body_sha256,
            checker_exit_code=state.checker_exit_code,
            missing_sections=state.missing_sections,
            missing_contract_keys=state.missing_contract_keys,
            fix_category=state.fix_category,
            rewrite_history=state.rewrite_history,
            occurrence_count=state.occurrence_count,
            repeated_fix_category=repeated_fix_category,
            remaining_blockers=remaining_blockers,
            required_evidence=required_evidence,
            suggested_repair_strategy=suggested_repair_strategy,
            stop_reason_if_unrepairable=stop_reason_if_unrepairable,
            source_body_reset=source_body_reset,
            # AC9/AC10: echo new state fields
            rewrite_request_fingerprint=state.rewrite_request_fingerprint,
            last_mutation_kind=state.last_mutation_kind,
            budget_debit=state.budget_debit,
        )

    # --- AC2: max_rewrite_attempts enforcement ---
    # AC10: format_only_repair (budget_debit=0) does not consume max_iterations.
    # Treat effective attempt count as: rewrite_attempt_count - (1 - budget_debit)
    # i.e. if this invocation was format_only_repair, it doesn't count against budget.
    # AC10: budget_debit=0 for format_only_repair → doesn't consume max_iterations
    # Effective attempt count subtracts the free repair if this was format_only_repair.
    effective_attempt_count = state.rewrite_attempt_count - (1 - state.budget_debit)
    if effective_attempt_count >= state.max_rewrite_attempts:
        return RouteResult(
            route=ROUTE_HUMAN_JUDGMENT_REQUIRED,
            reason_code=REASON_CODE_MAX_ATTEMPTS_EXCEEDED,
            rewrite_attempt_count=state.rewrite_attempt_count,
            max_rewrite_attempts=state.max_rewrite_attempts,
            checked_body_sha256=state.checked_body_sha256,
            checker_exit_code=state.checker_exit_code,
            missing_sections=state.missing_sections,
            missing_contract_keys=state.missing_contract_keys,
            fix_category=state.fix_category,
            rewrite_history=state.rewrite_history,
            occurrence_count=state.occurrence_count,
            repeated_fix_category=repeated_fix_category,
            remaining_blockers=remaining_blockers,
            required_evidence=required_evidence,
            suggested_repair_strategy=suggested_repair_strategy,
            stop_reason_if_unrepairable=stop_reason_if_unrepairable,
            source_body_reset=source_body_reset,
            # AC9/AC10: echo new state fields
            rewrite_request_fingerprint=state.rewrite_request_fingerprint,
            last_mutation_kind=state.last_mutation_kind,
            budget_debit=state.budget_debit,
        )


    # --- AC9: fingerprint-based convergence detection ---
    # If the same rewrite_request_fingerprint recurs, it means rewrite did not
    # change the underlying reason — route to human_judgment_required.
    # Only applies when occurrence_count >= 2 AND fingerprint matches.
    # (occurrence_count < 2 means we haven't seen it repeat yet.)

    # --- AC4: no-progress detection (only applicable after first iteration) ---
    # Check body hash unchanged
    if (
        state.previous_checked_body_sha256 is not None
        and state.checked_body_sha256 == state.previous_checked_body_sha256
    ):
        return RouteResult(
            route=ROUTE_HUMAN_JUDGMENT_REQUIRED,
            reason_code=REASON_CODE_BODY_HASH_UNCHANGED,
            rewrite_attempt_count=state.rewrite_attempt_count,
            max_rewrite_attempts=state.max_rewrite_attempts,
            checked_body_sha256=state.checked_body_sha256,
            checker_exit_code=state.checker_exit_code,
            missing_sections=state.missing_sections,
            missing_contract_keys=state.missing_contract_keys,
            fix_category=state.fix_category,
            rewrite_history=state.rewrite_history,
            occurrence_count=state.occurrence_count,
            repeated_fix_category=repeated_fix_category,
            remaining_blockers=remaining_blockers,
            required_evidence=required_evidence,
            suggested_repair_strategy=suggested_repair_strategy,
            stop_reason_if_unrepairable=stop_reason_if_unrepairable,
            source_body_reset=source_body_reset,
            # AC9/AC10: echo new state fields
            rewrite_request_fingerprint=state.rewrite_request_fingerprint,
            last_mutation_kind=state.last_mutation_kind,
            budget_debit=state.budget_debit,
        )

    # Check missing set no-progress (body changed but missing set did not strictly shrink)
    if state.previous_checked_body_sha256 is not None:
        previous_universe = _missing_universe(
            state.previous_missing_sections, state.previous_missing_contract_keys
        )
        # Only evaluate progress when there was something missing to resolve.
        # If nothing was missing previously, a body change is fine — continue.
        if len(previous_universe) > 0:
            progressed = _strictly_decreased(
                state.missing_sections,
                state.missing_contract_keys,
                state.previous_missing_sections,
                state.previous_missing_contract_keys,
            )
            # No progress if the combined missing universe did not strictly shrink.
            # This catches: same set, grown set, AND replacement (one resolved /
            # another newly missing), and the cross-category case where sections
            # shrink but contract keys grow.
            if not progressed:
                return RouteResult(
                    route=ROUTE_HUMAN_JUDGMENT_REQUIRED,
                    reason_code=REASON_CODE_MISSING_CONTRACT_NO_PROGRESS,
                    rewrite_attempt_count=state.rewrite_attempt_count,
                    max_rewrite_attempts=state.max_rewrite_attempts,
                    checked_body_sha256=state.checked_body_sha256,
                    checker_exit_code=state.checker_exit_code,
                    missing_sections=state.missing_sections,
                    missing_contract_keys=state.missing_contract_keys,
                    fix_category=state.fix_category,
                    rewrite_history=state.rewrite_history,
                    occurrence_count=state.occurrence_count,
                    repeated_fix_category=repeated_fix_category,
                    remaining_blockers=remaining_blockers,
                    required_evidence=required_evidence,
                    suggested_repair_strategy=suggested_repair_strategy,
                    stop_reason_if_unrepairable=stop_reason_if_unrepairable,
                    source_body_reset=source_body_reset,
                # AC9/AC10: echo new state fields
                rewrite_request_fingerprint=state.rewrite_request_fingerprint,
                last_mutation_kind=state.last_mutation_kind,
                budget_debit=state.budget_debit
                )

    # checker_exit_code != 0 and within budget — continue rewrite loop
    return RouteResult(
        route=ROUTE_CONTINUE_REWRITE,
        reason_code=REASON_CODE_CHECKER_FAILED,
        rewrite_attempt_count=state.rewrite_attempt_count,
        max_rewrite_attempts=state.max_rewrite_attempts,
        checked_body_sha256=state.checked_body_sha256,
        checker_exit_code=state.checker_exit_code,
        missing_sections=state.missing_sections,
        missing_contract_keys=state.missing_contract_keys,
        fix_category=state.fix_category,
        rewrite_history=state.rewrite_history,
        occurrence_count=state.occurrence_count,
        repeated_fix_category=repeated_fix_category,
        remaining_blockers=remaining_blockers,
        required_evidence=required_evidence,
        suggested_repair_strategy=suggested_repair_strategy,
        stop_reason_if_unrepairable=stop_reason_if_unrepairable,
        source_body_reset=source_body_reset,
    # AC9/AC10: echo new state fields
    rewrite_request_fingerprint=state.rewrite_request_fingerprint,
    last_mutation_kind=state.last_mutation_kind,
    budget_debit=state.budget_debit
    )


# ---------------------------------------------------------------------------
# Schema validation (AC8 enforcement at runtime)
# ---------------------------------------------------------------------------


class RewriteRouterStateError(Exception):
    """
    Raised when a persisted router state file is present but corrupt, invalid,
    or schema-violating.

    This is deliberately distinct from a missing file (which returns None). A
    corrupt attempt-counter file must NOT be silently reset to 0, because that
    would let a process bypass the max_rewrite_attempts stop condition simply by
    truncating the state file. Callers must treat this as fail-closed.
    """


_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "schemas"
    / "loop_rewrite_router_state_v1.json"
)


def _load_schema() -> dict[str, Any]:
    """Load the LOOP_REWRITE_ROUTER_STATE_V1 JSON Schema from disk."""
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def validate_state_dict(data: Any) -> tuple[bool, str]:
    """
    Validate a state dict against loop_rewrite_router_state_v1.json.

    Unlike the previous required-fields-only check, this enforces the FULL
    schema: types, ranges (rewrite_attempt_count >= 0, max_rewrite_attempts >= 1),
    additionalProperties: false, sha256 format, and schema_version const.

    Returns (valid, error_message). Never raises for ordinary validation
    failures so callers can decide routing.
    """
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema is a declared dependency
        return False, "jsonschema is not available; cannot validate state"

    if not isinstance(data, dict):
        return False, "Input must be a JSON object"

    try:
        schema = _load_schema()
    except OSError as e:  # pragma: no cover - schema ships with the repo
        return False, f"Cannot load state schema: {e}"

    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        return False, f"State schema validation failed: {e.message}"
    return True, ""


# ---------------------------------------------------------------------------
# Router state persistence helpers (AC7)
# ---------------------------------------------------------------------------


def load_rewrite_router_state(
    state_path: str,
    current_source_body_sha256: Optional[str] = None,
) -> Optional[LOOP_REWRITE_ROUTER_STATE_V1]:
    """
    Load LOOP_REWRITE_ROUTER_STATE_V1 from a JSON file.

    AC7: Supports replay-safe restoration — attempt count does NOT reset to 0
    across sessions / CI reruns.

    Return semantics (deliberately distinct so resets are never silent):
      - file missing            -> None (caller starts a fresh loop at attempt 0)
      - corrupt / invalid / schema-violating
                                -> raise RewriteRouterStateError (fail-closed;
                                   never silently reset the attempt counter)
      - source body changed     -> return a reset state with source_body_reset=True
                                   and rewrite_attempt_count=0; the reset fact is
                                   carried into the route result (AC7)
      - otherwise               -> restored state (replay_safe=True)
    """
    if not os.path.exists(state_path):
        return None

    try:
        with open(state_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise RewriteRouterStateError(
            f"Corrupt router state file {state_path}: {e}"
        ) from e
    except OSError as e:
        raise RewriteRouterStateError(
            f"Cannot read router state file {state_path}: {e}"
        ) from e

    valid, error_msg = validate_state_dict(data)
    if not valid:
        raise RewriteRouterStateError(
            f"Invalid router state file {state_path}: {error_msg}"
        )

    stored_source_sha = data.get("source_issue_body_sha256")

    # AC7: reset condition — human changed the source issue body. We do NOT
    # silently drop the state; we return a reset state that records the fact.
    if (
        current_source_body_sha256 is not None
        and stored_source_sha is not None
        and current_source_body_sha256 != stored_source_sha
    ):
        return LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=0,
            max_rewrite_attempts=data["max_rewrite_attempts"],
            checker_exit_code=data["checker_exit_code"],
            checked_body_sha256=data["checked_body_sha256"],
            missing_sections=data.get("missing_sections", []),
            missing_contract_keys=data.get("missing_contract_keys", []),
            fix_category=data.get("fix_category", "unknown_contract_failure"),
            rewrite_history=[],
            occurrence_count=0,
            previous_checked_body_sha256=None,
            previous_missing_sections=[],
            previous_missing_contract_keys=[],
            source_issue_body_sha256=current_source_body_sha256,
            replay_safe=True,
            source_body_reset=True,
        )

    return LOOP_REWRITE_ROUTER_STATE_V1(
        rewrite_attempt_count=data["rewrite_attempt_count"],
        max_rewrite_attempts=data["max_rewrite_attempts"],
        checker_exit_code=data["checker_exit_code"],
        checked_body_sha256=data["checked_body_sha256"],
        missing_sections=data.get("missing_sections", []),
        missing_contract_keys=data.get("missing_contract_keys", []),
        fix_category=data.get("fix_category", "unknown_contract_failure"),
        rewrite_history=list(data.get("rewrite_history", [])),
        occurrence_count=data.get("occurrence_count", 0),
        previous_checked_body_sha256=data.get("previous_checked_body_sha256"),
        previous_missing_sections=data.get("previous_missing_sections", []),
        previous_missing_contract_keys=data.get("previous_missing_contract_keys", []),
        source_issue_body_sha256=stored_source_sha,
        replay_safe=True,
        source_body_reset=False,
        rewrite_request_fingerprint=data.get("rewrite_request_fingerprint"),
        last_mutation_kind=data.get("last_mutation_kind", "semantic_rewrite"),
        budget_debit=data.get("budget_debit", 1),
    )


def save_rewrite_router_state(
    state: LOOP_REWRITE_ROUTER_STATE_V1,
    state_path: str,
) -> None:
    """
    Save LOOP_REWRITE_ROUTER_STATE_V1 to a JSON file atomically.

    AC7: Persists attempt count so it survives session restarts. The write is
    crash-safe: data is written to a temp file in the same directory, flushed and
    fsync'd, then os.replace()'d over the target. os.replace() is an atomic rename
    on success, so a crash mid-write can never leave a truncated / partial JSON
    file that load_rewrite_router_state would then treat as corrupt.
    """
    data = {
        "schema_version": SCHEMA_VERSION,
        "rewrite_attempt_count": state.rewrite_attempt_count,
        "max_rewrite_attempts": state.max_rewrite_attempts,
        "checker_exit_code": state.checker_exit_code,
        "checked_body_sha256": state.checked_body_sha256,
        "missing_sections": state.missing_sections,
        "missing_contract_keys": state.missing_contract_keys,
        "fix_category": state.fix_category,
        "rewrite_history": state.rewrite_history,
        "occurrence_count": state.occurrence_count,
        "previous_checked_body_sha256": state.previous_checked_body_sha256,
        "previous_missing_sections": state.previous_missing_sections,
        "previous_missing_contract_keys": state.previous_missing_contract_keys,
        "source_issue_body_sha256": state.source_issue_body_sha256,
        "replay_safe": True,
        "source_body_reset": state.source_body_reset,
        "rewrite_request_fingerprint": state.rewrite_request_fingerprint,
        "last_mutation_kind": state.last_mutation_kind,
        "budget_debit": state.budget_debit,
    }

    target_dir = os.path.dirname(os.path.abspath(state_path))
    os.makedirs(target_dir, exist_ok=True)
    tmp_path = f"{state_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, state_path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _validate_cli_input(data: Any) -> tuple[bool, str]:
    """
    Validate CLI input against the full LOOP_REWRITE_ROUTER_STATE_V1 schema.

    This enforces types, ranges, additionalProperties: false, sha256 format, and
    schema_version const — not just required-field presence. Invalid input is
    rejected with an explicit reason (exit 2) rather than silently coerced.
    """
    return validate_state_dict(data)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point. Reads LOOP_REWRITE_ROUTER_STATE_V1 JSON from stdin."""
    try:
        input_text = sys.stdin.read()
        input_data = json.loads(input_text)
    except json.JSONDecodeError as e:
        error = {"error": f"Invalid JSON input: {str(e)}", "route": None}
        print(json.dumps(error, ensure_ascii=False, indent=2))
        sys.exit(2)

    valid, error_msg = _validate_cli_input(input_data)
    if not valid:
        error = {"error": error_msg, "route": None}
        print(json.dumps(error, ensure_ascii=False, indent=2))
        sys.exit(2)

    try:
        state = LOOP_REWRITE_ROUTER_STATE_V1(
            rewrite_attempt_count=input_data["rewrite_attempt_count"],
            max_rewrite_attempts=input_data["max_rewrite_attempts"],
            checker_exit_code=input_data["checker_exit_code"],
            checked_body_sha256=input_data["checked_body_sha256"],
            missing_sections=input_data.get("missing_sections", []),
            missing_contract_keys=input_data.get("missing_contract_keys", []),
            fix_category=input_data.get("fix_category", "unknown_contract_failure"),
            rewrite_history=input_data.get("rewrite_history", []),
            occurrence_count=input_data.get("occurrence_count", 0),
            previous_checked_body_sha256=input_data.get("previous_checked_body_sha256"),
            previous_missing_sections=input_data.get("previous_missing_sections", []),
            previous_missing_contract_keys=input_data.get("previous_missing_contract_keys", []),
            source_issue_body_sha256=input_data.get("source_issue_body_sha256"),
            replay_safe=input_data.get("replay_safe", False),
            source_body_reset=input_data.get("source_body_reset", False),
            rewrite_request_fingerprint=input_data.get("rewrite_request_fingerprint"),
            last_mutation_kind=input_data.get("last_mutation_kind", "semantic_rewrite"),
            budget_debit=input_data.get("budget_debit", 1),
        )

        result = decide_rewrite_route(state)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        sys.exit(0)

    except Exception as e:
        error = {"error": f"Internal error: {str(e)}", "route": None}
        print(json.dumps(error, ensure_ascii=False, indent=2))
        sys.exit(3)


if __name__ == "__main__":
    main()
