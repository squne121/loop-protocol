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

# Reason codes
REASON_CODE_MAX_ATTEMPTS_EXCEEDED = "max_attempts_exceeded"
REASON_CODE_BODY_HASH_UNCHANGED = "body_hash_unchanged"
REASON_CODE_MISSING_CONTRACT_NO_PROGRESS = "missing_contract_no_progress"
REASON_CODE_CHECKER_PASSED = "checker_passed"
REASON_CODE_CHECKER_FAILED = "checker_failed_rewrite"
REASON_CODE_SOURCE_BODY_RESET = "source_body_reset"
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
    previous_checked_body_sha256: Optional[str] = None
    previous_missing_sections: list[str] = field(default_factory=list)
    previous_missing_contract_keys: list[str] = field(default_factory=list)
    source_issue_body_sha256: Optional[str] = None
    replay_safe: bool = False
    source_body_reset: bool = False


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
    source_body_reset: bool = False

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
            "source_body_reset": self.source_body_reset,
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


# ---------------------------------------------------------------------------
# decide_rewrite_route — pure function (AC1)
# ---------------------------------------------------------------------------


def decide_rewrite_route(state: LOOP_REWRITE_ROUTER_STATE_V1) -> RouteResult:
    """
    Route the rewrite loop based on current router state.

    Priority order:
    1. max_rewrite_attempts exceeded → human_judgment_required
    2. body hash unchanged → human_judgment_required (body_hash_unchanged)
    3. missing set not decreased → human_judgment_required (missing_contract_no_progress)
    4. checker_exit_code != 0 → continue_rewrite (checker_failed_rewrite)
    5. checker_exit_code == 0 → proceed_to_review

    AC2: max_rewrite_attempts runtime enforcement.
        rewrite_attempt_count >= max_rewrite_attempts → human_judgment_required
        off-by-one: max=2, attempt 0/1 allowed, attempt 2 → stop.

    AC3: checker_exit_code == 0 is required for review/handoff.

    AC4: no-progress detection — body hash + missing set.

    AC5: terminal result includes all required fields.

    AC7: source_issue_body_sha256 mismatch is a reset condition. When the state
    was produced by such a reset, state.source_body_reset is True and the fact is
    propagated to every RouteResult so the reset is observable in the terminal
    result (not silently swallowed at load time).
    """

    source_body_reset = state.source_body_reset

    # --- AC2: max_rewrite_attempts enforcement ---
    if state.rewrite_attempt_count >= state.max_rewrite_attempts:
        return RouteResult(
            route=ROUTE_HUMAN_JUDGMENT_REQUIRED,
            reason_code=REASON_CODE_MAX_ATTEMPTS_EXCEEDED,
            rewrite_attempt_count=state.rewrite_attempt_count,
            max_rewrite_attempts=state.max_rewrite_attempts,
            checked_body_sha256=state.checked_body_sha256,
            checker_exit_code=state.checker_exit_code,
            missing_sections=state.missing_sections,
            missing_contract_keys=state.missing_contract_keys,
            source_body_reset=source_body_reset,
        )

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
            source_body_reset=source_body_reset,
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
                    source_body_reset=source_body_reset,
                )

    # --- AC3: checker_exit_code gating ---
    if state.checker_exit_code != 0:
        # Checker still failing — continue rewrite loop
        return RouteResult(
            route=ROUTE_CONTINUE_REWRITE,
            reason_code=REASON_CODE_CHECKER_FAILED,
            rewrite_attempt_count=state.rewrite_attempt_count,
            max_rewrite_attempts=state.max_rewrite_attempts,
            checked_body_sha256=state.checked_body_sha256,
            checker_exit_code=state.checker_exit_code,
            missing_sections=state.missing_sections,
            missing_contract_keys=state.missing_contract_keys,
            source_body_reset=source_body_reset,
        )

    # checker_exit_code == 0: proceed to review/handoff
    return RouteResult(
        route=ROUTE_PROCEED_TO_REVIEW,
        reason_code=REASON_CODE_CHECKER_PASSED,
        rewrite_attempt_count=state.rewrite_attempt_count,
        max_rewrite_attempts=state.max_rewrite_attempts,
        checked_body_sha256=state.checked_body_sha256,
        checker_exit_code=state.checker_exit_code,
        missing_sections=state.missing_sections,
        missing_contract_keys=state.missing_contract_keys,
        source_body_reset=source_body_reset,
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
        previous_checked_body_sha256=data.get("previous_checked_body_sha256"),
        previous_missing_sections=data.get("previous_missing_sections", []),
        previous_missing_contract_keys=data.get("previous_missing_contract_keys", []),
        source_issue_body_sha256=stored_source_sha,
        replay_safe=True,
        source_body_reset=False,
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
        "previous_checked_body_sha256": state.previous_checked_body_sha256,
        "previous_missing_sections": state.previous_missing_sections,
        "previous_missing_contract_keys": state.previous_missing_contract_keys,
        "source_issue_body_sha256": state.source_issue_body_sha256,
        "replay_safe": True,
        "source_body_reset": state.source_body_reset,
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
            previous_checked_body_sha256=input_data.get("previous_checked_body_sha256"),
            previous_missing_sections=input_data.get("previous_missing_sections", []),
            previous_missing_contract_keys=input_data.get("previous_missing_contract_keys", []),
            source_issue_body_sha256=input_data.get("source_issue_body_sha256"),
            replay_safe=input_data.get("replay_safe", False),
            source_body_reset=input_data.get("source_body_reset", False),
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
