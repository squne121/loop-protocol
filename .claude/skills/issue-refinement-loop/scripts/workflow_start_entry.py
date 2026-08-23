#!/usr/bin/env python3
"""
workflow_start_entry.py

Thin production carrier for the canonical bare `preflight.run` command
(Issue #2311 AC1/AC2). This is the ONLY script directly reachable from
`command_registry.py`'s bare `preflight.run` entry (sibling anchor-comment
profiles -- `preflight.run.with_anchor` / `.with_human_context` /
`.with_agent_report` / `.fixture` / `.fixture.with_human_context` --
continue to first-hop into `run_refinement_preflight.py` directly and are
unaffected by this module; Issue #2311 is scoped to the bare command only).

Within a SINGLE continuous invocation, this module:

  1. Assembles the caller-declared, invocation-scoped capability request
     (`spark_mode` / `spark_fallback` / `planned_operations`, each operation
     carrying `phase` / `actor_role` / `requires_mutation`). This request is
     NOT a static global superset of every possible workflow_operation --
     it is whatever THIS invocation's caller explicitly declares (via CLI
     flags, or their `LOOP_SPARK_MODE` / `LOOP_SPARK_FALLBACK` /
     `LOOP_PLANNED_OPERATIONS_JSON` environment-variable fallback, since the
     canonical `preflight.run` registry argv itself only carries
     `--issue-number` / `--repo` -- Issue #2311 P0-6 / AC1 keep that argv
     shape byte-for-byte otherwise unchanged). A missing or malformed
     request fails closed as `environment_failure` BEFORE the producer is
     ever invoked (AC5) -- this is independent of, and checked earlier
     than, the AC3 producer-response malformed case below.
  2. Calls `root_entry_router.capability_preflight_result()` (imported, not
     reimplemented -- AC2) exactly once to run the workflow capability
     preflight producer.
  3. If the producer's `decision` is `blocked`, OR the producer invocation
     itself failed / returned a malformed result (`capability_preflight_
     result()` already normalizes both of those into a synthetic
     `decision: "blocked"` result -- see that function's docstring), this
     module returns a compact blocked result WITHOUT ever invoking
     `run_refinement_preflight.py` (AC3) -- `checks` / `reasons` from the
     producer are preserved verbatim (AC7).
  4. If `decision` is `ready` or `degraded`, this module invokes
     `run_refinement_preflight.py` exactly once, via the SAME trusted
     interpreter this process is already running under (`sys.executable`).
     This process was itself launched as `uv run python3
     workflow_start_entry.py ...` by the trusted `skill_runtime_exec.py`
     dispatcher, which already resolved a trusted `uv`/`python3` pair
     before spawning this process -- so `sys.executable` already IS the
     interpreter produced by that canonical trusted-`uv` resolution.
     Re-deriving a second, independent trusted-`uv` resolver inside this
     module would duplicate `skill_runtime_exec.py`'s existing trust logic
     rather than reuse it (Issue #2311 P0-6: "canonical resolver は既に
     信頼済み uv の絶対パスを返せるので、新しい resolver は不要") -- so this
     module deliberately reuses `sys.executable` instead of shelling out to
     `uv` a second time (AC4).

This module is a thin synchronous wrapper: no persistent state, no
authorization token, no ledger, no digest/receipt. Step 0's capability
preflight (this module) and Step 5's `run_root_transition` capability
preflight are independent fresh calls -- the result of this module is
NEVER reused as Step 5 implementation-entry authorization (see
`SKILL.md`'s Step 0 section).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import root_entry_router  # noqa: E402  (sys.path must be prepared first)

SCHEMA_VERSION = "WORKFLOW_START_ENTRY_RESULT_V1"

_REQUIRED_OPERATION_KEYS = ("phase", "actor_role", "requires_mutation")

_ENV_SPARK_MODE = "LOOP_SPARK_MODE"
_ENV_SPARK_FALLBACK = "LOOP_SPARK_FALLBACK"
_ENV_PLANNED_OPERATIONS_JSON = "LOOP_PLANNED_OPERATIONS_JSON"


class CapabilityRequestError(ValueError):
    """Raised internally when the caller-declared capability request is
    missing or malformed (AC5). Never escapes this module -- ``run()``
    catches it and converts it into a compact ``environment_failure``
    blocked result before the producer is ever invoked."""


def _parse_planned_operations(raw: Optional[str]) -> list[dict]:
    """Parse and validate the caller-declared, invocation-scoped
    `planned_operations` JSON array. Raises `CapabilityRequestError` if the
    value is missing, non-JSON, not a non-empty list, or if any entry is
    missing one of `phase` / `actor_role` / `requires_mutation` (AC5). This
    function never invents a default -- an absent declaration is a caller
    error, not treated as an empty-but-valid set, since a workflow-start
    invocation that intends zero downstream mutation must say so with an
    explicit empty-operations declaration is still ambiguous; the caller
    MUST declare its planned operations."""
    if raw is None or not raw.strip():
        raise CapabilityRequestError("planned_operations_missing")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CapabilityRequestError("planned_operations_not_json") from exc
    if not isinstance(data, list) or not data:
        raise CapabilityRequestError("planned_operations_not_nonempty_list")
    for operation in data:
        if not isinstance(operation, dict):
            raise CapabilityRequestError("planned_operations_entry_not_object")
        missing = [key for key in _REQUIRED_OPERATION_KEYS if key not in operation]
        if missing:
            raise CapabilityRequestError(f"planned_operations_entry_missing_keys:{','.join(missing)}")
    return data


def build_capability_request(
    *,
    spark_mode: Optional[str],
    spark_fallback: Optional[str],
    planned_operations_json: Optional[str],
) -> dict[str, Any]:
    """Assemble the caller-declared capability request. Raises
    `CapabilityRequestError` if `planned_operations_json` is missing or
    malformed (AC5). `spark_mode` / `spark_fallback` are optional (a caller
    that never needs Spark may omit both)."""
    planned_operations = _parse_planned_operations(planned_operations_json)
    return {
        "spark_mode": spark_mode,
        "spark_fallback": spark_fallback,
        "planned_operations": planned_operations,
    }


def _compact_result(
    *,
    status: str,
    reason: Optional[str],
    checks: dict,
    reasons: list,
    decision: Optional[str],
    inner_preflight_invoked: bool,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "decision": decision,
        "checks": checks,
        "reasons": reasons,
        "inner_preflight_invoked": inner_preflight_invoked,
    }


def _default_invoke_inner_preflight(*, issue_number: int, repo: str) -> int:
    """Invoke `run_refinement_preflight.py` exactly once via the trusted
    interpreter this process is already running under (AC4 -- see module
    docstring). Inherits stdio so the inner script's own
    `refinement_preflight_result/v1` stdout contract passes through
    unmodified to this process's stdout/stderr and its exit code becomes
    this process's exit code."""
    inner_script = _SCRIPT_DIR / "run_refinement_preflight.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(inner_script),
            "--issue-number",
            str(issue_number),
            "--repo",
            repo,
        ],
        check=False,
    )
    return proc.returncode


def run(
    *,
    issue_number: int,
    repo: str,
    spark_mode: Optional[str],
    spark_fallback: Optional[str],
    planned_operations_json: Optional[str],
    capability_preflight_result_fn: Callable[..., dict] = root_entry_router.capability_preflight_result,
    invoke_inner_preflight_fn: Callable[..., int] = _default_invoke_inner_preflight,
) -> tuple[dict[str, Any], int]:
    """Run the full workflow-start gate once. Returns `(result_dict,
    exit_code)`. `capability_preflight_result_fn` / `invoke_inner_preflight_fn`
    are injectable for hermetic fake-transport tests (AC9) -- production
    callers use the defaults (`root_entry_router.capability_preflight_result`
    and a `sys.executable`-based inner invocation)."""
    try:
        capability_request = build_capability_request(
            spark_mode=spark_mode,
            spark_fallback=spark_fallback,
            planned_operations_json=planned_operations_json,
        )
    except CapabilityRequestError as exc:
        # AC5: caller-side missing/malformed request. Fail closed WITHOUT
        # ever calling the producer.
        result = _compact_result(
            status="blocked",
            reason=f"environment_failure:{exc}",
            checks={},
            reasons=["caller_capability_request_missing_or_malformed"],
            decision=None,
            inner_preflight_invoked=False,
        )
        return result, 2

    # AC2: call the producer exactly once.
    producer_result = capability_preflight_result_fn(
        repo=repo,
        spark_mode=capability_request["spark_mode"],
        spark_fallback=capability_request["spark_fallback"],
        planned_operations=capability_request["planned_operations"],
    )
    decision = producer_result.get("decision")
    checks = producer_result.get("checks", {})
    reasons = producer_result.get("reasons", [])

    if decision not in ("ready", "degraded"):
        # AC3/AC7: `blocked` decision, or a producer invocation
        # failure/malformed result already normalized to `decision:
        # "blocked"` by `capability_preflight_result_fn`. Either way,
        # `run_refinement_preflight.py` is NEVER invoked on this path, and
        # the producer's own `checks`/`reasons` are preserved verbatim.
        result = _compact_result(
            status="blocked",
            reason=f"capability_preflight_{decision or 'unknown'}",
            checks=checks,
            reasons=reasons,
            decision=decision,
            inner_preflight_invoked=False,
        )
        return result, 2

    # AC4: ready/degraded -- invoke run_refinement_preflight.py exactly
    # once via the trusted interpreter.
    inner_returncode = invoke_inner_preflight_fn(issue_number=issue_number, repo=repo)
    result = _compact_result(
        status="ready" if inner_returncode == 0 else "inner_preflight_failed",
        reason=None,
        checks=checks,
        reasons=reasons,
        decision=decision,
        inner_preflight_invoked=True,
    )
    return result, inner_returncode


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Workflow-start capability preflight gate. First-hop production "
            "carrier for the canonical bare `preflight.run` command "
            "(Issue #2311)."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--spark-mode", default=None)
    parser.add_argument("--spark-fallback", default=None)
    parser.add_argument("--planned-operations-json", default=None)
    args = parser.parse_args(argv)

    spark_mode = args.spark_mode if args.spark_mode is not None else os.environ.get(_ENV_SPARK_MODE)
    spark_fallback = (
        args.spark_fallback if args.spark_fallback is not None else os.environ.get(_ENV_SPARK_FALLBACK)
    )
    planned_operations_json = (
        args.planned_operations_json
        if args.planned_operations_json is not None
        else os.environ.get(_ENV_PLANNED_OPERATIONS_JSON)
    )

    result, exit_code = run(
        issue_number=args.issue_number,
        repo=args.repo,
        spark_mode=spark_mode,
        spark_fallback=spark_fallback,
        planned_operations_json=planned_operations_json,
    )
    if not result.get("inner_preflight_invoked"):
        # The inner preflight's own stdout contract (`refinement_preflight_
        # result/v1`) already passed through unmodified when it WAS
        # invoked; only print this module's own compact result on the
        # blocked path, where there is no inner stdout to preserve.
        print(json.dumps(result))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
