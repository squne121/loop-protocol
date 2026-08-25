#!/usr/bin/env python3
"""
Contract Readiness Check (ISSUE_CONTRACT_READINESS_RESULT_V1)

Issue body の contract readiness を検証し、review-issue / issue-author / edit-issue が
消費できる structured feedback JSON を返す mutation-free helper。

Exit codes:
  0: status: go (all checks pass)
  1: status: needs_fix (body-author-fixable errors)
  2: status: human_judgment (env/tool/runtime issues needing human attention)
  3: input/runtime error

Modes:
  --mode static  (default): VC syntax, section, schema only. No VC execution. No network.
  --mode preflight-static: Same as static. Alias for review-issue / issue-reviewer callers.
    Detects compound_command_disallowed statically (no execution).
    unexpected_pass detection requires --mode execute (execution-only signal).
  --mode execute: Invokes baseline_vc_preflight.py to run VCs. May have side effects.

Inputs:
  --body-file <path>   : Read issue body from file (static mode preferred)
  --issue <N> --repo <owner/repo> : Fetch from GitHub (requires gh auth)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Literal, NamedTuple, Optional

import yaml

# Locate sibling scripts (relative to this file)
_SCRIPTS_DIR = Path(__file__).resolve().parent
# parents: [0]=issue-contract-review, [1]=skills, [2]=.claude, [3]=<repo root>
_REPO_ROOT = _SCRIPTS_DIR.parents[3]
_VALIDATE_ISSUE_BODY_PY = (
    _REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts" / "validate_issue_body.py"
)
_BASELINE_VC_PREFLIGHT_PY = _SCRIPTS_DIR / "baseline_vc_preflight.py"

# AC10 (#1346): share heading detection with prose_boundary_policy.py's HEADING_POLICY
# so RDR001 section extraction recognises the same accepted forms (incl. Japanese
# headings) as the prose-boundary guard, instead of an independent English-only regex.
_CREATE_ISSUE_SCRIPTS_DIR = _REPO_ROOT / ".claude" / "skills" / "create-issue" / "scripts"
if str(_CREATE_ISSUE_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_CREATE_ISSUE_SCRIPTS_DIR))
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from baseline_vc_preflight import (  # noqa: E402
    AggregateTimeoutExceedsPolicyError,
    CLEANUP_TAIL_SECONDS as _COMMAND_CLEANUP_TAIL_SECONDS,
    CommandTimeoutExceedsPolicyError,
    CommandTimeoutNonPositiveError,
    DEFAULT_TIMEOUT_SECONDS as _PER_VC_COMMAND_TIMEOUT_SECONDS,
    compute_canonical_vc_plan as _compute_canonical_vc_plan,
    extract_allowed_paths as _extract_allowed_paths,
    extract_verification_commands_section,
    run_subprocess_with_cooperative_supervisor as _run_subprocess_with_cooperative_supervisor,
)
from mrc_contract_parser import parse_machine_readable_contract  # noqa: E402
from prose_boundary_policy import (  # noqa: E402
    BLOCK_KIND_CODE_FENCE,
    HEADING_POLICY,
    iter_markdown_blocks,
    lookup_heading_policy,
    parse_atx_heading_line,
)

# Issue #2165 (OWNER 2026-08-15 REQUEST_CHANGES P1-1(c)): derive the
# `baseline_vc_preflight.py` *aggregate* wrapper timeout from that module's
# own per-VC-command cap instead of an independent hardcoded number, so a
# future change to the per-command cap cannot silently reintroduce the
# arithmetic break the OWNER flagged (two VCs near the per-command cap
# already exceeding a hand-picked aggregate value). Budget assumption:
# worst case is a small number of long-running VCs run sequentially
# (`baseline_vc_preflight.py` executes VCs with `--max-workers=1` in strict
# mode) plus a handful of fast (`rg` etc.) VCs whose combined overhead is
# covered by the flat margin below.
_MAX_SEQUENTIAL_NEAR_CAP_VCS_ASSUMED = 2
_AGGREGATE_MARGIN_SECONDS = 50
BASELINE_VC_PREFLIGHT_AGGREGATE_TIMEOUT_SECONDS = (
    _PER_VC_COMMAND_TIMEOUT_SECONDS * _MAX_SEQUENTIAL_NEAR_CAP_VCS_ASSUMED + _AGGREGATE_MARGIN_SECONDS
)

# Issue #2165 P1-1: `run_validate_issue_body()` below uses this named
# constant (was the bare literal `30`) so `CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS`
# can be derived from it rather than duplicating the number.
VALIDATE_ISSUE_BODY_TIMEOUT_SECONDS = 30

# Issue #2165 P1-1: the wrapper timeout `run_root_review_pipeline.py`'s
# `run_contract_readiness_check()` uses for the `contract_readiness_check.py`
# subprocess it spawns. Derived (not independently guessed) from this
# module's OWN internal worst-case budget -- `validate_issue_body.py`
# subprocess (`VALIDATE_ISSUE_BODY_TIMEOUT_SECONDS`) + the
# `baseline_vc_preflight.py` aggregate wrapper
# (`BASELINE_VC_PREFLIGHT_AGGREGATE_TIMEOUT_SECONDS`) + a small margin for
# process startup/teardown -- so the two values cannot drift apart the way
# the OWNER-flagged 250s/230s pairing could.
CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS = (
    VALIDATE_ISSUE_BODY_TIMEOUT_SECONDS + BASELINE_VC_PREFLIGHT_AGGREGATE_TIMEOUT_SECONDS + 20
)

# ---------------------------------------------------------------------------
# Cleanup-aware review timeout budget formula (Issue #2207)
# ---------------------------------------------------------------------------
#
# Given `N` (the canonical VC plan's `command_occurrence_count` -- Issue
# #2207 OWNER P1-3 (PR #2221 REQUEST_CHANGES): the Issue #2207 Outcome/AC5
# contract fixes the budget denominator as `N = max(2, command_occurrence_count)`;
# an earlier implementation iteration substituted `launch_upper_bound`
# (the dedup-aware actual-launch upper bound) without an Issue reframe,
# which the OWNER flagged as an unauthorized silent contract change. See
# `baseline_vc_preflight.compute_canonical_vc_plan()`), derives the 4-value
# invocation-local timeout envelope the review pipeline uses:
#
#   - `baseline_aggregate_seconds`: this module's OWN
#     `BASELINE_VC_PREFLIGHT_AGGREGATE_TIMEOUT_SECONDS`-equivalent, but
#     invocation-local (derived from THIS body's plan, not the N<=2
#     compatibility module constant)
#   - `readiness_wrapper_seconds`: this module's OWN
#     `CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS`-equivalent, invocation-local
#   - `per_attempt_seconds`: the deadline `run_root_review_pipeline.py`
#     passes to `reviewer_transport.run_reviewer_transport(per_attempt_deadline=...)`
#   - `total_seconds`: the deadline passed as `total_deadline=...`
#
# Formula (Issue #2207 OWNER-reviewed redesign):
#
#     per_vc_slot          = per_command_cap (150s) + cleanup_tail (15s)
#                           = 165s
#     effective_n           = max(2, N)
#     baseline_aggregate    = effective_n * per_vc_slot + AGGREGATE_MARGIN_SECONDS (20s)
#     readiness_wrapper     = VALIDATE_ISSUE_BODY_TIMEOUT_SECONDS (30s)
#                             + baseline_aggregate
#                             + READINESS_WRAPPER_MARGIN_SECONDS (20s)
#     per_attempt           = CHECK_ISSUE_CONTRACT_TIMEOUT_SECONDS (30s)
#                             + readiness_wrapper
#                             + MERGE_READINESS_TIMEOUT_SECONDS (30s)
#                             + PER_ATTEMPT_MARGIN_SECONDS (20s)
#     total                 = per_attempt + TOTAL_MARGIN_SECONDS (40s)
#
# At `effective_n == 2` (i.e. `N <= 2`) this reduces EXACTLY to the current
# production values (350s / 400s / 480s / 520s) -- Issue #2207 AC5 "low-count
# compatibility" is therefore a natural consequence of the `max(2, N)`
# floor, not a separately hand-coded branch (avoiding a second place the
# two could drift apart again). Note `VALIDATE_ISSUE_BODY_TIMEOUT_SECONDS`
# here is the SAME named constant defined above (not re-derived).
#
# `_CHECK_ISSUE_CONTRACT_TIMEOUT_SECONDS_MIRROR` /
# `_MERGE_READINESS_TIMEOUT_SECONDS_MIRROR` are intentionally mirrored here
# (not imported) from `run_root_review_pipeline.py`'s own constants of the
# same name, to avoid a circular import (`run_root_review_pipeline.py`
# already imports THIS module). A pytest in `issue-refinement-loop/tests/`
# cross-checks the two stay identical.

# Issue #2233 AC2: sourced from baseline_vc_preflight.py's single canonical
# `CLEANUP_TAIL_SECONDS` (the same value each command_timeout_budget/v1
# entry in compute_canonical_vc_plan()'s command_budgets[] carries) instead
# of an independent local literal `15`.
_PER_VC_SLOT_CLEANUP_TAIL_SECONDS = _COMMAND_CLEANUP_TAIL_SECONDS
_PER_VC_SLOT_SECONDS = _PER_VC_COMMAND_TIMEOUT_SECONDS + _PER_VC_SLOT_CLEANUP_TAIL_SECONDS  # 165

_BUDGET_AGGREGATE_MARGIN_SECONDS = 20
_BUDGET_READINESS_WRAPPER_MARGIN_SECONDS = 20
_CHECK_ISSUE_CONTRACT_TIMEOUT_SECONDS_MIRROR = 30
_MERGE_READINESS_TIMEOUT_SECONDS_MIRROR = 30
_BUDGET_PER_ATTEMPT_MARGIN_SECONDS = 20
_BUDGET_TOTAL_MARGIN_SECONDS = 40

_BUDGET_LOW_COUNT_FLOOR = 2

# Issue #2207 AC7: same fixed policy ceiling
# `baseline_vc_preflight.compute_canonical_vc_plan()` reports as
# `policy_cap`. Applied to `command_occurrence_count` (Issue #2207 OWNER
# P1-3), not `launch_upper_bound`.
MAX_VC_EXECUTION_SLOTS = 40


class VerificationBudgetExceedsPolicyError(Exception):
    """Typed, non-retryable rejection: `N` (the Issue #2207 Outcome/AC5
    contract's `command_occurrence_count`) exceeds the fixed policy ceiling.

    Raised BEFORE any subprocess is launched (Issue #2207 AC7).
    """

    error_code = "verification_budget_exceeds_policy"

    def __init__(self, n: int, policy_cap: int):
        self.n = n
        self.policy_cap = policy_cap
        super().__init__(
            f"command_occurrence_count={n} exceeds policy_cap={policy_cap} ({self.error_code})"
        )


class ReviewBudget(NamedTuple):
    n: int
    effective_n: int
    baseline_aggregate_seconds: int
    readiness_wrapper_seconds: int
    per_attempt_seconds: int
    total_seconds: int


def derive_review_budget(
    command_occurrence_count: int, *, policy_cap: int = MAX_VC_EXECUTION_SLOTS
) -> ReviewBudget:
    """Derive the invocation-local `ReviewBudget` from `command_occurrence_count`.

    Issue #2207 OWNER P1-3 (PR #2221 REQUEST_CHANGES): the Issue #2207
    Outcome / AC5 contract fixes `N = max(2, command_occurrence_count)` as
    the budget denominator (the RAW count of VC command lines, not the
    dedup-aware `launch_upper_bound`). Callers MUST pass
    `compute_canonical_vc_plan()`'s `command_occurrence_count` field here,
    not `launch_upper_bound`.

    Raises `VerificationBudgetExceedsPolicyError` (non-retryable, subprocess
    NOT launched) if `command_occurrence_count > policy_cap`.
    """
    if command_occurrence_count > policy_cap:
        raise VerificationBudgetExceedsPolicyError(command_occurrence_count, policy_cap)

    effective_n = max(_BUDGET_LOW_COUNT_FLOOR, command_occurrence_count)

    baseline_aggregate = effective_n * _PER_VC_SLOT_SECONDS + _BUDGET_AGGREGATE_MARGIN_SECONDS
    readiness_wrapper = (
        VALIDATE_ISSUE_BODY_TIMEOUT_SECONDS + baseline_aggregate + _BUDGET_READINESS_WRAPPER_MARGIN_SECONDS
    )
    per_attempt = (
        _CHECK_ISSUE_CONTRACT_TIMEOUT_SECONDS_MIRROR
        + readiness_wrapper
        + _MERGE_READINESS_TIMEOUT_SECONDS_MIRROR
        + _BUDGET_PER_ATTEMPT_MARGIN_SECONDS
    )
    total = per_attempt + _BUDGET_TOTAL_MARGIN_SECONDS

    return ReviewBudget(
        n=command_occurrence_count,
        effective_n=effective_n,
        baseline_aggregate_seconds=baseline_aggregate,
        readiness_wrapper_seconds=readiness_wrapper,
        per_attempt_seconds=per_attempt,
        total_seconds=total,
    )


# Issue #2233 fix_delta P0-2: the margin added on top of a canonical plan's
# own `aggregate_timeout_seconds` (the sum of REAL resolved per-command
# budgets, which may legitimately exceed the #2207 formula's
# `DEFAULT_PER_COMMAND_TIMEOUT_SECONDS`-per-command worst case once a
# `static_policy` entry applies) when it is used as a floor below.
#
# Deliberately == `_BUDGET_AGGREGATE_MARGIN_SECONDS` (the #2207 formula's
# OWN margin): for a body with NO `static_policy`-elevated command,
# `plan["aggregate_timeout_seconds"] == command_occurrence_count * 165`
# exactly, and the #2207 formula's `baseline_aggregate_seconds ==
# max(2, command_occurrence_count) * 165 + 20 >=
# command_occurrence_count * 165 + 20`. Using the SAME margin here
# guarantees `effective_review_budget()` is a NO-OP (returns `review_budget`
# UNCHANGED) for every body that does not actually need a larger budget --
# it only floors upward when a REAL static_policy elevation makes the
# plan's own aggregate exceed what the #2207 formula assumed.
_PLAN_AGGREGATE_MARGIN_SECONDS = _BUDGET_AGGREGATE_MARGIN_SECONDS


def effective_review_budget(review_budget: "ReviewBudget", plan: Dict[str, Any]) -> "ReviewBudget":
    """Issue #2233 fix_delta P0-2: recompute a `ReviewBudget` using the
    EXACT SAME `derive_review_budget()` linear relationships (this function
    does NOT change that formula -- Out of Scope), but with
    `baseline_aggregate_seconds` floored to also cover the canonical plan's
    own per-command budget sum (`plan["aggregate_timeout_seconds"]` +
    `_PLAN_AGGREGATE_MARGIN_SECONDS`).

    Without this, a `static_policy`-sourced per-command budget above
    `DEFAULT_PER_COMMAND_TIMEOUT_SECONDS` (Issue #2233 Background: the
    271.31s `issue-refinement-loop` test suite) would resolve correctly at
    the per-command layer (`baseline_vc_preflight.py`'s own subprocess
    timeout for that command) but still be silently killed by an OUTER
    wrapper (`run_baseline_vc_preflight()` below /
    `run_root_review_pipeline.py`'s own supervisor) still assuming the
    OLD per-command-DEFAULT worst case.

    When the plan's own aggregate does not exceed the #2207 formula's
    result, `review_budget` is returned UNCHANGED (this is a floor, not a
    replacement)."""
    plan_floor = plan["aggregate_timeout_seconds"] + _PLAN_AGGREGATE_MARGIN_SECONDS
    if plan_floor <= review_budget.baseline_aggregate_seconds:
        return review_budget

    baseline_aggregate = plan_floor
    readiness_wrapper = (
        VALIDATE_ISSUE_BODY_TIMEOUT_SECONDS
        + baseline_aggregate
        + _BUDGET_READINESS_WRAPPER_MARGIN_SECONDS
    )
    per_attempt = (
        _CHECK_ISSUE_CONTRACT_TIMEOUT_SECONDS_MIRROR
        + readiness_wrapper
        + _MERGE_READINESS_TIMEOUT_SECONDS_MIRROR
        + _BUDGET_PER_ATTEMPT_MARGIN_SECONDS
    )
    total = per_attempt + _BUDGET_TOTAL_MARGIN_SECONDS

    return review_budget._replace(
        baseline_aggregate_seconds=baseline_aggregate,
        readiness_wrapper_seconds=readiness_wrapper,
        per_attempt_seconds=per_attempt,
        total_seconds=total,
    )


# Required fields for `decision: immediate` in Runtime Verification Applicability section
_RVA_IMMEDIATE_REQUIRED_FIELDS = [
    "applicable_acs",
    "execution_environment",
    "skip_conditions",
    "fallback_policy",
    "artifact_requirements",
]


# ---------------------------------------------------------------------------
# Body acquisition
# ---------------------------------------------------------------------------


def read_body_file(path: str) -> tuple[Optional[str], Optional[str]]:
    """Read body from file. Returns (body, error_code)."""
    try:
        return Path(path).read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, "body_file_not_found"
    except Exception:
        return None, "body_parse_error"


def fetch_body_from_github(issue: int, repo: str) -> tuple[Optional[str], Optional[str]]:
    """Fetch issue body from GitHub. Returns (body, error_code)."""
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue), "--repo", repo, "--json", "body"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return json.loads(result.stdout).get("body"), None
        stderr = result.stderr.lower()
        if "not authenticated" in stderr or "authentication failed" in stderr:
            return None, "gh_auth_failed"
        if "not found" in stderr or "could not resolve" in stderr:
            return None, "gh_repo_not_found"
        return None, "gh_other_error"
    except subprocess.TimeoutExpired:
        return None, "gh_timeout"
    except json.JSONDecodeError:
        return None, "gh_json_parse_error"
    except Exception:
        return None, "gh_other_error"


# ---------------------------------------------------------------------------
# SHA-256
# ---------------------------------------------------------------------------


def sha256_of(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# validate_issue_body.py integration
# ---------------------------------------------------------------------------


_ISSUE_KIND_POLICY_PATH = _REPO_ROOT / "docs" / "dev" / "github-ops.md"
_EXISTING_ISSUE_READINESS_PROFILE = "existing_issue_readiness_v1"


class ExistingIssueValidationResolution(NamedTuple):
    """Classify existing-issue readiness without conflating fallback states.

    ``profile`` is currently restricted to canonical ``parent``.  Known
    implementation/research kinds deliberately retain the historical no-kind
    validator path.  Parse failures and policy blocks never silently become a
    profile or a known-kind fallback.
    """

    status: Literal["profile", "legacy_no_kind", "parse_failure", "blocked"]
    canonical_issue_kind: Optional[str]
    validation_profile: Optional[str]
    reason_code: Optional[str]


def _load_issue_kind_policy() -> dict[str, Any]:
    """Load ISSUE_KIND_POLICY_V1 from its documented SSOT, fail-closed."""
    try:
        text = _ISSUE_KIND_POLICY_PATH.read_text(encoding="utf-8")
        match = re.search(r"```yaml\s*\nISSUE_KIND_POLICY_V1:(.*?)```", text, re.DOTALL)
        if not match:
            raise ValueError("issue_kind_policy_block_missing")
        parsed = yaml.safe_load("ISSUE_KIND_POLICY_V1:" + match.group(1))
        policy = parsed.get("ISSUE_KIND_POLICY_V1") if isinstance(parsed, dict) else None
        if not isinstance(policy, dict) or policy.get("schema_version") != "1":
            raise ValueError("issue_kind_policy_invalid")
        canonical_kinds = policy.get("canonical_kinds")
        aliases = policy.get("aliases")
        reason_code = policy.get("unknown_kind_reason_code")
        if (
            not isinstance(canonical_kinds, list)
            or not all(isinstance(kind, str) for kind in canonical_kinds)
            or not isinstance(aliases, dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in aliases.items())
            or not isinstance(reason_code, str)
            or not reason_code
        ):
            raise ValueError("issue_kind_policy_invalid")
        return {
            "canonical_kinds": frozenset(canonical_kinds),
            "aliases": dict(aliases),
            "unknown_kind_reason_code": reason_code,
        }
    except (OSError, ValueError, yaml.YAMLError, TypeError) as exc:
        raise ValueError("issue_kind_policy_load_error") from exc


def resolve_existing_issue_validation_profile(body: str) -> ExistingIssueValidationResolution:
    """Resolve the versioned profile using MRC parsing and ISSUE_KIND_POLICY_V1."""
    parsed = parse_machine_readable_contract(body)
    if not parsed.ok:
        return ExistingIssueValidationResolution(
            status="parse_failure",
            canonical_issue_kind=None,
            validation_profile=None,
            reason_code=parsed.reason,
        )

    issue_kind = parsed.get("issue_kind")
    if not isinstance(issue_kind, str) or not issue_kind:
        return ExistingIssueValidationResolution(
            status="parse_failure",
            canonical_issue_kind=None,
            validation_profile=None,
            reason_code="issue_kind_missing",
        )
    if issue_kind != issue_kind.strip():
        return ExistingIssueValidationResolution(
            status="blocked",
            canonical_issue_kind=None,
            validation_profile=None,
            reason_code="issue_kind_whitespace_not_normalized",
        )

    try:
        policy = _load_issue_kind_policy()
    except ValueError:
        return ExistingIssueValidationResolution(
            status="blocked",
            canonical_issue_kind=None,
            validation_profile=None,
            reason_code="issue_kind_policy_load_error",
        )

    canonical_kind = issue_kind
    if canonical_kind not in policy["canonical_kinds"]:
        canonical_kind = policy["aliases"].get(issue_kind)
    if canonical_kind not in policy["canonical_kinds"]:
        return ExistingIssueValidationResolution(
            status="blocked",
            canonical_issue_kind=None,
            validation_profile=None,
            reason_code=policy["unknown_kind_reason_code"],
        )

    if canonical_kind == "parent":
        return ExistingIssueValidationResolution(
            status="profile",
            canonical_issue_kind=canonical_kind,
            validation_profile=_EXISTING_ISSUE_READINESS_PROFILE,
            reason_code=None,
        )
    return ExistingIssueValidationResolution(
        status="legacy_no_kind",
        canonical_issue_kind=canonical_kind,
        validation_profile=None,
        reason_code=None,
    )


_PARENT_CLOSURE_COMPATIBILITY = {
    "delivery-rollup": frozenset({"child-complete"}),
    "quality-gate": frozenset({"measurement-ready", "quality-validated"}),
    "routing-map": frozenset({"routing-complete"}),
    "decision-log": frozenset({"decision-recorded"}),
}
_PARENT_PLACEHOLDER_RE = re.compile(r"<required:\s*[^>]+>", re.IGNORECASE)
_QDR_SECTION_RE = re.compile(
    r"^##\s+Quality Decision Record\s*$(.+?)(?=^##|\Z)", re.MULTILINE | re.DOTALL
)
_QDR_STATUS_RE = re.compile(r"^\s*[-*]\s*`?Status`?\s*:\s*(?P<value>.+?)\s*$", re.MULTILINE)


def _existing_readiness_error(rule_id: str, category: str, context: str, hint: str) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "severity": "error",
        "source_check": "existing_issue_readiness_v1",
        "category": category,
        "section": "Machine-Readable Contract",
        "line_start": 0,
        "line_end": 0,
        "minimal_context": [context],
        "fix_hint": hint,
        "autofixable": False,
    }


def check_existing_issue_readiness_semantics(body: str) -> list[dict[str, Any]]:
    """Validate versioned existing-parent semantics independently of templates."""
    resolution = resolve_existing_issue_validation_profile(body)
    if resolution.status == "blocked":
        return [
            _existing_readiness_error(
                "MRC_ISSUE_KIND",
                resolution.reason_code or "issue_kind_blocked",
                resolution.reason_code or "issue_kind_blocked",
                "Use an exact ISSUE_KIND_POLICY_V1 canonical kind or alias without whitespace.",
            )
        ]
    if resolution.status != "profile":
        return []

    parsed = parse_machine_readable_contract(body)
    data = parsed.data if parsed.ok and isinstance(parsed.data, dict) else {}
    errors: list[dict[str, Any]] = []
    if data.get("contract_schema_version") != "v1":
        errors.append(
            _existing_readiness_error(
                "MRC_PARENT_SCHEMA",
                "parent_contract_schema_invalid",
                f"contract_schema_version={data.get('contract_schema_version')!r}",
                "Set contract_schema_version: v1 for a parent MRC.",
            )
        )

    parent_mode = data.get("parent_mode")
    closure_mode = data.get("closure_mode")
    if (
        not isinstance(parent_mode, str)
        or _PARENT_PLACEHOLDER_RE.search(parent_mode)
        or parent_mode not in _PARENT_CLOSURE_COMPATIBILITY
    ):
        errors.append(
            _existing_readiness_error(
                "MRC_PARENT_MODE",
                "parent_mode_invalid",
                f"parent_mode={parent_mode!r}",
                "Use a concrete parent_mode enum from docs/dev/github-ops.md.",
            )
        )
    if not isinstance(closure_mode, str) or _PARENT_PLACEHOLDER_RE.search(closure_mode):
        errors.append(
            _existing_readiness_error(
                "MRC_PARENT_CLOSURE",
                "closure_mode_invalid",
                f"closure_mode={closure_mode!r}",
                "Use a concrete closure_mode enum from docs/dev/github-ops.md.",
            )
        )
    elif isinstance(parent_mode, str) and parent_mode in _PARENT_CLOSURE_COMPATIBILITY:
        if closure_mode not in _PARENT_CLOSURE_COMPATIBILITY[parent_mode]:
            errors.append(
                _existing_readiness_error(
                    "MRC_PARENT_CLOSURE",
                    "parent_closure_mode_incompatible",
                    f"parent_mode={parent_mode!r}, closure_mode={closure_mode!r}",
                    "Use a closure_mode compatible with parent_mode.",
                )
            )

    if parent_mode == "quality-gate":
        qdr_match = _QDR_SECTION_RE.search(body)
        status_match = _QDR_STATUS_RE.search(qdr_match.group(1)) if qdr_match else None
        qdr_status = status_match.group("value").strip().strip("`") if status_match else None
        if qdr_status != closure_mode:
            errors.append(
                _existing_readiness_error(
                    "MRC_PARENT_QDR_STATUS",
                    "quality_decision_record_status_incompatible",
                    f"closure_mode={closure_mode!r}, qdr_status={qdr_status!r}",
                    "For quality-gate, set Quality Decision Record Status equal to closure_mode.",
                )
            )
    return errors


def run_validate_issue_body(body: str) -> dict[str, Any]:
    """
    Run validate_issue_body.py via subprocess with --body-file.
    The existing-issue readiness path dispatches only canonical parent through
    the versioned profile. Known implementation/research kinds keep the legacy
    kind-agnostic invocation; parser and policy failures remain separate.
    Returns parsed JSON output (loop_body_lint/v1 schema).
    --mode static: no network, no execution beyond python subprocess.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(body)
        tmp_path = tf.name

    try:
        args = [sys.executable, str(_VALIDATE_ISSUE_BODY_PY), "--body-file", tmp_path]
        resolution = resolve_existing_issue_validation_profile(body)
        if resolution.status == "profile":
            args.extend(["--kind", "parent", "--validation-profile", resolution.validation_profile])

        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=VALIDATE_ISSUE_BODY_TIMEOUT_SECONDS,
        )
        if result.stdout:
            return json.loads(result.stdout)
        # Empty output or non-zero exit 2+ — validator internal error (not body-author-fixable)
        return {
            "schema": "loop_body_lint/v1",
            "status": "validator_internal_error",
            "errors": [
                {
                    "rule_id": "VALIDATOR_INTERNAL",
                    "severity": "error",
                    "section": "(global)",
                    "line_start": 0,
                    "line_end": 0,
                    "message": result.stderr or "no output from validate_issue_body",
                    "minimal_context": [],
                    "context_truncated": False,
                    "fix_hint": "validator 実行環境を確認してください",
                    "autofixable": False,
                }
            ],
        }
    except subprocess.TimeoutExpired:
        # Tool-level failure — not fixable by body author
        return {
            "schema": "loop_body_lint/v1",
            "status": "validator_tool_error",
            "errors": [
                {
                    "rule_id": "VALIDATOR_TIMEOUT",
                    "severity": "error",
                    "section": "(global)",
                    "line_start": 0,
                    "line_end": 0,
                    "message": "validate_issue_body timed out",
                    "minimal_context": [],
                    "context_truncated": False,
                    "fix_hint": "validator 実行環境を確認してください",
                    "autofixable": False,
                }
            ],
        }
    except json.JSONDecodeError as exc:
        # JSON decode error — validator internal error (not body-author-fixable)
        return {
            "schema": "loop_body_lint/v1",
            "status": "validator_internal_error",
            "errors": [
                {
                    "rule_id": "VALIDATOR_JSON_ERROR",
                    "severity": "error",
                    "section": "(global)",
                    "line_start": 0,
                    "line_end": 0,
                    "message": f"json decode error: {exc}",
                    "minimal_context": [],
                    "context_truncated": False,
                    "fix_hint": "validator 実行環境を確認してください",
                    "autofixable": False,
                }
            ],
        }
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def map_validate_errors_to_readiness_errors(validate_result: dict) -> list[dict]:
    """Convert loop_body_lint/v1 errors into ISSUE_CONTRACT_READINESS_RESULT_V1 errors[]."""
    errors = []
    for e in validate_result.get("errors", []):
        errors.append(
            {
                "rule_id": e.get("rule_id", "LP000"),
                "severity": e.get("severity", "error"),
                "source_check": "validate_issue_body",
                "category": "body_lint",
                "section": e.get("section", ""),
                "line_start": e.get("line_start", 0),
                "line_end": e.get("line_end", 0),
                "minimal_context": e.get("minimal_context", []),
                "fix_hint": e.get("fix_hint", ""),
                "autofixable": e.get("autofixable", False),
            }
        )
    return errors


# ---------------------------------------------------------------------------
# baseline_vc_preflight.py integration (execute mode only)
# ---------------------------------------------------------------------------


def compute_invocation_local_baseline_timeout(body: str) -> int:
    """Issue #2207: derive the `baseline_vc_preflight.py` aggregate wrapper
    timeout for THIS specific pinned `body`, from the canonical VC plan's
    `launch_upper_bound` -- instead of the fixed
    `BASELINE_VC_PREFLIGHT_AGGREGATE_TIMEOUT_SECONDS` module constant (which
    remains the `N<=2` compatibility value used when this function is not
    invoked, e.g. by any external importer still reading the module
    constant directly).

    Raises `VerificationBudgetExceedsPolicyError` (non-retryable) if the
    plan's `launch_upper_bound` exceeds the fixed policy ceiling -- BEFORE
    any subprocess is launched.
    """
    # Issue #2232 Scope Delta P0-1 (OWNER REQUEST_CHANGES
    # https://github.com/squne121/loop-protocol/pull/2255#issuecomment-5340600982):
    # this parent-side classification context (`cwd="."`, Allowed Paths
    # extracted from this SAME `body`) mirrors what the `baseline_vc_preflight.py`
    # subprocess launched below (via `run_baseline_vc_preflight()`) resolves
    # for its OWN `args.cwd or "."` default and `allowed_paths_from_body`,
    # keeping `plan_digest` convergent across the process boundary.
    plan = _compute_canonical_vc_plan(
        body, cwd=".", allowed_paths=_extract_allowed_paths(body)
    )
    # Issue #2207 OWNER P1-3: `command_occurrence_count`, per the Issue
    # #2207 Outcome/AC5 contract -- NOT `launch_upper_bound`.
    budget = derive_review_budget(plan["command_occurrence_count"], policy_cap=plan["policy_cap"])
    # Issue #2233 fix_delta P0-2: floor the #2207-formula result with the
    # SAME plan's own `aggregate_timeout_seconds` (the sum of REAL resolved
    # per-command budgets, e.g. a `static_policy` entry above
    # DEFAULT_PER_COMMAND_TIMEOUT_SECONDS) so a legitimately-slow VC's outer
    # wrapper timeout is never smaller than its own per-command budget.
    budget = effective_review_budget(budget, plan)
    return budget.baseline_aggregate_seconds


def run_baseline_vc_preflight(
    body: str,
    *,
    override_timeout_seconds: Optional[float] = None,
    override_grace_seconds: Optional[float] = None,
    _test_extra_env: Optional[Dict[str, str]] = None,
) -> tuple[dict, int]:
    """
    Run baseline_vc_preflight.py via a cooperative subprocess supervisor.
    Returns (parsed_json, exit_code).
    Only called in --mode execute.

    `override_timeout_seconds` (Issue #2207 OWNER P1-2 item 7): test-only
    hook to inject a float (sub-second) aggregate deadline instead of the
    computed production value, so a real outer-deadline fault-injection
    test can exercise this EXACT production code path without waiting on
    realistic production-scale timeouts. `None` (the default) preserves
    production behavior exactly (the computed int-seconds budget).

    `override_grace_seconds` (Issue #2207 OWNER P1-2 item 5): test-only
    hook that scales BOTH (a) this supervisor's own grace period while
    waiting for the direct child (`baseline_vc_preflight.py`) to exit after
    SIGTERM, AND (b) -- via `_test_extra_env` -- the grace period the
    child's OWN top-level `main()` uses for `reap_all_active_process_groups()`
    when it cooperatively reaps ITS VC descendants. Without (b), a fast
    outer grace here would still leave the child's inner reap blocked on
    the production 5.0s default, defeating the point of scaling. `None`
    (the default) preserves the production 5.0s default at both layers.

    `_test_extra_env` is test-only free-form environment passthrough to the
    spawned `baseline_vc_preflight.py` child (e.g. a SIGTERM-handler-entry
    marker-file path); production callers never pass this.
    """
    # Issue #2207: compute the invocation-local aggregate timeout from the
    # SAME canonical VC plan the executor's occurrence count is bound to,
    # BEFORE writing the tempfile / launching any subprocess. A body whose
    # plan exceeds the policy ceiling is rejected here (typed,
    # non-retryable `runtime_error` / `verification_budget_exceeds_policy`)
    # rather than ever spawning `baseline_vc_preflight.py`.
    try:
        aggregate_timeout_seconds: float = compute_invocation_local_baseline_timeout(body)
    except (
        VerificationBudgetExceedsPolicyError,
        AggregateTimeoutExceedsPolicyError,
        CommandTimeoutExceedsPolicyError,
        CommandTimeoutNonPositiveError,
    ) as exc:
        # Issue #2233 fix_delta: the canonical plan producer this function
        # calls (`compute_invocation_local_baseline_timeout()` ->
        # `_compute_canonical_vc_plan()`) can now ALSO reject a body whose
        # per-command or aggregate command-level budget exceeds policy --
        # BEFORE this function ever writes the body tempfile or launches
        # `baseline_vc_preflight.py`.
        return (
            {
                "schema": "baseline_vc_preflight/v1",
                "status": "runtime_error",
                "results": [],
                "errors": [],
                "failure_class": exc.error_code,
                "timeout_phase": None,
                "retryable": False,
            },
            -1,
        )

    if override_timeout_seconds is not None:
        aggregate_timeout_seconds = override_timeout_seconds

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(body)
        tmp_path = tf.name

    try:
        # Issue #2165 P1-1(c) / Issue #2207: the aggregate wrapper timeout is
        # derived from `baseline_vc_preflight.py`'s own per-VC-command cap
        # AND (Issue #2207) the canonical VC plan's `command_occurrence_count`
        # for THIS body, rather than an independent hardcoded number or a
        # fixed `N<=2` assumption -- so the two values cannot drift the way
        # the OWNER-flagged pairings could. The caller
        # (`run_root_review_pipeline.py`'s `run_contract_readiness_check()`)
        # derives ITS OWN wrapper timeout from the SAME canonical plan for
        # the same reason (Issue #2207 AC8).
        #
        # Issue #2207 OWNER P0-1 (PR #2221 REQUEST_CHANGES): a cooperative
        # supervisor replaces `subprocess.run(timeout=...)`. CPython's
        # `subprocess.run(timeout=...)` sends SIGKILL directly to the
        # direct child on timeout, bypassing `baseline_vc_preflight.py`'s
        # own SIGTERM handler entirely and never reaching the VC process
        # GROUPS it tracks (each isolated via `start_new_session=True`).
        # `run_subprocess_with_cooperative_supervisor()` sends SIGTERM to
        # the direct child's process group first, gives it a bounded grace
        # period to run its own cooperative cleanup, and only escalates to
        # SIGKILL after that grace period elapses.
        _child_env: Optional[Dict[str, str]] = None
        if override_grace_seconds is not None or _test_extra_env:
            _child_env = dict(os.environ)
            if override_grace_seconds is not None:
                _child_env["BASELINE_VC_PREFLIGHT_TEST_REAP_GRACE_SECONDS"] = str(
                    override_grace_seconds
                )
            if _test_extra_env:
                _child_env.update(_test_extra_env)
        # Issue #2207 OWNER P1-2 item 5 (fault-injection-discovered fix):
        # `override_grace_seconds` scales ONLY the CHILD's own inner
        # `reap_all_active_process_groups()` grace (via `_child_env` above)
        # -- NOT this supervisor's own outer `grace_seconds`. The child
        # needs a FULL, uninterrupted window to run its own SIGTERM-then-
        # SIGKILL-then-confirm sequence for ITS VC descendants before it
        # exits; if this supervisor's own grace were scaled down to the
        # SAME small value, it could SIGKILL the child (`baseline_vc_preflight.py`
        # itself) mid-reap, before the child ever finishes signaling its
        # own VC process group -- orphaning it. Keeping this supervisor's
        # own `grace_seconds` at the production default is safe for test
        # speed too: `Popen.wait(timeout=...)` returns as soon as the
        # child actually exits, not after the full window, so a child that
        # finishes its (scaled-down) inner reap quickly still returns
        # quickly here.
        # Issue #2233 fix_delta P0-1: compute the SAME canonical plan this
        # function already used (via `compute_invocation_local_baseline_timeout()`
        # above) to derive `aggregate_timeout_seconds`, and pass its
        # `plan_digest` through to the child so the child's OWN recomputed
        # plan (from the SAME `tmp_path` body) can be verified against it
        # before the child launches any VC subprocess -- protecting against
        # the body drifting between this write and the child's read.
        # Issue #2232 Scope Delta P0-1: the child subprocess below is
        # launched WITHOUT an explicit `--cwd`, so its own
        # `args.cwd or "."` default applies, resolved (like this parent's
        # own process) against the real OS-level working directory. Passing
        # `cwd="."` + the SAME body's extracted Allowed Paths here mirrors
        # that exact classification context so `plan_digest` stays
        # convergent across this subprocess boundary.
        _plan_for_digest = _compute_canonical_vc_plan(
            body, cwd=".", allowed_paths=_extract_allowed_paths(body)
        )
        supervised = _run_subprocess_with_cooperative_supervisor(
            [
                sys.executable,
                str(_BASELINE_VC_PREFLIGHT_PY),
                "--strict",
                "--body-file",
                tmp_path,
                "--expected-plan-digest",
                _plan_for_digest["plan_digest"],
            ],
            timeout_seconds=aggregate_timeout_seconds,
            env=_child_env,
        )
        if supervised.timed_out:
            # Issue #2165 P0-1 (OWNER 2026-08-15 REQUEST_CHANGES): this MUST
            # be a typed runtime-error payload, not a plain
            # `errors: ["timeout"]` blocked payload -- the latter was
            # silently collapsed by `map_preflight_result_to_errors()`'s
            # plain-string-error branch into `category:
            # no_commands_extracted` / `readiness_status: needs_fix`,
            # letting a genuine execution-budget timeout masquerade as an
            # ordinary body-author-fixable semantic finding (and letting
            # `run_root_review_pipeline.py`'s `run-checker-attempt` exit
            # 0/1 instead of the exit-2 structured failure
            # `reviewer_transport.py` depends on to classify the attempt
            # as `reason_code: timeout`). `status: "runtime_error"` is a
            # NEW, distinct value from `go`/`needs_fix`/`human_judgment`
            # that `_raise_status()` and `build_result()` propagate at the
            # HIGHEST priority (see `_STATUS_PRIORITY` below) so it can
            # never be silently downgraded to a semantic verdict.
            return (
                {
                    "schema": "baseline_vc_preflight/v1",
                    "status": "runtime_error",
                    "results": [],
                    "errors": [],
                    "failure_class": "timeout",
                    "timeout_phase": "baseline_vc_preflight_aggregate",
                    "retryable": False,
                },
                -1,
            )
        exit_code = supervised.returncode
        if supervised.stdout:
            return json.loads(supervised.stdout), exit_code
        return (
            {
                "schema": "baseline_vc_preflight/v1",
                "status": "blocked",
                "results": [],
                "errors": [supervised.stderr or "no output"],
            },
            exit_code,
        )
    except json.JSONDecodeError as exc:
        return (
            {
                "schema": "baseline_vc_preflight/v1",
                "status": "blocked",
                "results": [],
                "errors": [f"json decode: {exc}"],
            },
            1,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# Status mapping contract:
# compound_command_disallowed → needs_fix (VC body fix resolves it)
# unexpected_pass → needs_fix (VC tightening resolves it)
# file_not_found_unrunnable → needs_fix (body refers to missing script)
# no_commands / extraction_error (body structure) → needs_fix
# env_missing_dep → human_judgment (not fixable by body author alone)
# regression_gate fail → human_judgment (env/implementation issue)
# human_judgment → human_judgment (MUST NOT collapse)
# timeout → human_judgment
_PREFLIGHT_CATEGORY_TO_READINESS: dict[str, str] = {
    "compound_command_disallowed": "needs_fix",
    "expected_baseline_fail": "go",
    "file_not_found_expected": "go",
    "env_missing_dep": "human_judgment",
    "file_not_found_unrunnable": "needs_fix",
    "timeout": "human_judgment",
    "unexpected_pass": "needs_fix",
    "unknown": "human_judgment",
    "no_commands_extracted": "needs_fix",
    # Issue #889: baseline-expect annotation mappings
    # baseline_expect_pass: VC annotated baseline-expect: pass and exited 0 → go
    "baseline_expect_pass": "go",
    # baseline_regression_failed: VC annotated baseline-expect: pass but exited non-0
    "baseline_regression_failed": "human_judgment",
    # AC: classify no-TTY pnpm classification as human_judgment（失敗分類を維持）
    "package_manager_no_tty_prompt": "human_judgment",
    # Issue #899: strict-mode annotation violations are body-author-fixable
    "inline_baseline_expect_invalid_placement": "needs_fix",
    "missing_baseline_expect_for_new_allowed_path": "needs_fix",
    # PR #1366 review (Blocker 1) / Issue #1347: existing-file missing node-id is a
    # non-canonical VC shape that the Issue body author can fix by rewriting the VC
    # to reference a not-yet-created test file instead → needs_fix (not human_judgment).
    "existing_file_missing_node_id_noncanonical": "needs_fix",
    # Issue #1406 (Blocker 2, PR #1412 review): a Verification Command with an
    # unbounded `rg` search path (no explicit path argument, so it recurses
    # over the whole repo) is a body-author-fixable VC-scope problem, not an
    # environment/tooling issue -> needs_fix (not human_judgment).
    "broad_search_path_unbounded": "needs_fix",
}


def map_preflight_result_to_errors(
    preflight_result: dict,
) -> tuple[list[dict], str]:
    """
    Map baseline_vc_preflight/v1 result into readiness errors[].

    Status priority: human_judgment > needs_fix > go.
    human_judgment from preflight MUST NOT be collapsed to needs_fix.

    Returns (errors_list, aggregate_readiness_status).
    """
    errors: list[dict] = []
    aggregate = "go"

    overall_status = preflight_result.get("status", "blocked")

    # Issue #2165 P0-1: a typed `status: "runtime_error"` payload (currently
    # only the `baseline_vc_preflight.py` aggregate-execution timeout, see
    # `run_baseline_vc_preflight()`) MUST NOT be folded into the plain
    # `blocked` no-results branch below (which maps to `needs_fix` via
    # `no_commands_extracted`) -- it is a transport-visible runtime failure,
    # not a body-author-fixable semantic finding.
    if overall_status == "runtime_error":
        errors.append(
            {
                "rule_id": "VCP_RUNTIME_ERROR",
                "severity": "error",
                "source_check": "baseline_vc_preflight",
                "category": "baseline_vc_preflight_runtime_error",
                "section": "Verification Commands",
                "line_start": 0,
                "line_end": 0,
                "minimal_context": [
                    f"failure_class={preflight_result.get('failure_class')}",
                    f"timeout_phase={preflight_result.get('timeout_phase')}",
                ],
                "fix_hint": (
                    "baseline_vc_preflight execution exceeded its aggregate budget; "
                    "this is a runtime/environment condition, not a body-fixable error."
                ),
                "autofixable": False,
                "source_payload": {
                    "failure_class": preflight_result.get("failure_class"),
                    "timeout_phase": preflight_result.get("timeout_phase"),
                    "retryable": preflight_result.get("retryable"),
                },
            }
        )
        return errors, "runtime_error"

    # Sentinel for absent "message" key (AC5: distinguish absent from None)
    _MISSING = object()

    # blocked with no results = body-structure issue (needs_fix)
    if overall_status == "blocked" and not preflight_result.get("results"):
        for err_msg in preflight_result.get("errors", []):
            # B6: handle both structured dict errors and legacy plain strings
            if isinstance(err_msg, dict):
                # AC5: fallback to str(err_msg) when "message" key is absent
                raw_msg = err_msg.get("message", _MISSING)
                msg = str(err_msg) if raw_msg is _MISSING else str(raw_msg)
                # AC6: normalize minimal_context — flatten list to avoid nested list
                raw_mc = err_msg.get("minimal_context", "")
                if isinstance(raw_mc, list):
                    mc_items: list[str] = [str(x) for x in raw_mc]
                elif raw_mc:
                    mc_items = [str(raw_mc)]
                else:
                    mc_items = []
                fh = err_msg.get("fix_hint", (
                    "Ensure Verification Commands section has fenced ```bash blocks "
                    "with $ prefixed commands."
                ))
                rule = err_msg.get("rule", "VCP001")
                # AC7: avoid double namespace — if rule already has a known prefix,
                # do not prepend "VCP_" again (e.g. "VC000_BODY_RETRIEVAL_FAILED" stays as-is)
                if rule and rule != "VCP001":
                    if rule.startswith("VCP_") or rule.startswith("VC") and "_" in rule:
                        rule_id = rule
                    else:
                        rule_id = f"VCP_{rule}"
                else:
                    rule_id = "VCP001"
                # Blocker 3: consume "kind" field to determine category and readiness_status
                kind = str(err_msg.get("kind", "extraction_error"))
                if kind == "retrieval_error":
                    category = "body_retrieval_failed"
                    readiness_status = "human_judgment"
                elif kind in ("extraction_error", "unsupported_vc_format"):
                    category = kind
                    readiness_status = "needs_fix"
                else:
                    category = kind or "preflight_error"
                    readiness_status = "human_judgment"
            else:
                msg = str(err_msg)
                mc_items = []
                fh = (
                    "Ensure Verification Commands section has fenced ```bash blocks "
                    "with $ prefixed commands."
                )
                rule_id = "VCP001"
                category = "no_commands_extracted"
                readiness_status = "needs_fix"
            errors.append(
                {
                    "rule_id": rule_id,
                    "severity": "error",
                    "source_check": "baseline_vc_preflight",
                    "category": category,
                    "section": "Verification Commands",
                    "line_start": 0,
                    "line_end": 0,
                    "minimal_context": [msg] + mc_items,
                    "fix_hint": fh,
                    "autofixable": False,
                }
            )
            aggregate = _raise_status(aggregate, readiness_status)
        return errors, aggregate

    for r in preflight_result.get("results", []):
        classification = r.get("classification", "")
        category = r.get("category", "")
        decision = r.get("decision", "go")
        scope_class = r.get("scope_class", "")

        # Skipped items: routing metadata, not errors
        if classification == "skipped":
            continue
        # expected_pass: no error
        if classification == "expected_pass":
            continue
        # expected_fail with go decision: normal baseline fail
        if classification == "expected_fail" and decision == "go":
            continue

        # Determine readiness impact
        readiness_status: Optional[str] = None

        # human_judgment decision: always human_judgment (MUST NOT collapse)
        if decision == "human_judgment":
            readiness_status = "human_judgment"
        elif decision == "blocked":
            mapped = _PREFLIGHT_CATEGORY_TO_READINESS.get(category)
            if mapped is not None:
                readiness_status = mapped
            elif scope_class == "regression_gate":
                readiness_status = "human_judgment"
            else:
                readiness_status = "human_judgment"

        # unexpected_pass classification overrides: normally needs_fix
        # Issue #889: if preflight payload reports baseline_expect=pass for this result,
        # then unexpected_pass was already re-mapped to expected_pass in baseline_vc_preflight.py
        # and would not reach here. But guard defensively:
        # - category == "baseline_expect_pass" → already handled above (mapped to go)
        # - category == "baseline_regression_failed" → already mapped to human_judgment
        # - annotation absent → backward compat: unexpected_pass → needs_fix
        if classification == "unexpected_pass":
            annotations = r.get("annotations", {})
            baseline_expect_val = annotations.get("baseline_expect") if isinstance(annotations, dict) else None
            if baseline_expect_val == "pass":
                # Should not normally occur (preflight re-maps to expected_pass),
                # but if it does, treat as go (AC12: use annotations from payload)
                readiness_status = "go"
            else:
                readiness_status = "needs_fix"

        if readiness_status in ("needs_fix", "human_judgment"):
            aggregate = _raise_status(aggregate, readiness_status)
            errors.append(
                {
                    "rule_id": f"VCP_{category.upper()[:20]}"
                    if category
                    else "VCP_UNKNOWN",
                    "severity": "error",
                    "source_check": "baseline_vc_preflight",
                    "category": category,
                    "section": "Verification Commands",
                    "line_start": r.get("line", 0),
                    "line_end": r.get("line", 0),
                    "minimal_context": _build_vc_context(r),
                    "fix_hint": r.get("fix_hint") or _default_fix_hint(category),
                    "autofixable": category in (
                        "compound_command_disallowed",
                        "inline_baseline_expect_invalid_placement",
                        "missing_baseline_expect_for_new_allowed_path",
                    ),
                    "source_payload": {
                        "classification": classification,
                        "category": category,
                        "scope_class": scope_class,
                        "decision": decision,
                        "confidence": r.get("confidence", ""),
                        "exit_code": r.get("exit_code"),
                        "command_hash": r.get("command_hash", ""),
                        "duration_ms": r.get("duration_ms"),
                        "strict": r.get("strict"),
                        "repair": r.get("repair"),
                        "annotations": r.get("annotations"),
                        "runner_env_delta": r.get("runner_env_delta", {}),
                    },
                }
            )

    return errors, aggregate


_STATUS_PRIORITY = {"go": 0, "needs_fix": 1, "human_judgment": 2, "runtime_error": 3}


def _raise_status(current: str, candidate: str) -> str:
    """Priority: runtime_error > human_judgment > needs_fix > go.

    Issue #2165 P0-1: `runtime_error` (a typed transport/execution-budget
    failure, currently only the `baseline_vc_preflight.py` aggregate
    timeout) sits ABOVE `human_judgment` so it can never be silently
    downgraded to an ordinary semantic verdict by a later `_raise_status()`
    call in the same aggregation pass.
    """
    if _STATUS_PRIORITY.get(candidate, 0) > _STATUS_PRIORITY.get(current, 0):
        return candidate
    return current


def _build_vc_context(result_item: dict) -> list[str]:
    cmd = result_item.get("raw_command", "")
    ac = result_item.get("ac", "")
    lines: list[str] = []
    if ac:
        lines.append(f"# {ac}")
    if cmd:
        lines.append(f"$ {cmd}")
    stderr_head = result_item.get("stderr_head", [])
    stdout_head = result_item.get("stdout_head", [])
    if stderr_head:
        lines.extend(stderr_head[:3])
    elif stdout_head:
        lines.extend(stdout_head[:3])
    return lines


def _default_fix_hint(category: str) -> str:
    hints: dict[str, str] = {
        "compound_command_disallowed": (
            "Replace compound shell command with a single command. "
            "See body-authoring.md#VC_SINGLE_COMMAND_GUARDRAIL."
        ),
        "unexpected_pass": (
            "VC passed before implementation. Tighten VC so it fails at baseline."
        ),
        "env_missing_dep": (
            "Required tool or command is missing from the environment. Human intervention needed."
        ),
        "regression_gate": (
            "Regression gate command failed. Check environment or fix implementation."
        ),
        "timeout": "Command timed out. May require human investigation.",
        "unknown": "Unable to classify result. Human judgment required.",
        "file_not_found_unrunnable": (
            "Script or file referenced in VC does not exist. Fix path in VC."
        ),
    }
    return hints.get(category, "See baseline_vc_preflight output for details.")


# ---------------------------------------------------------------------------
# AC4: Runtime Verification Applicability checks
# ---------------------------------------------------------------------------


def _fenced_line_indices(body: str) -> set[int]:
    """Return zero-based line indexes owned by fenced blocks using the shared policy."""
    indices: set[int] = set()
    line_index = 0
    for block_text, block_kind in iter_markdown_blocks(body):
        line_count = len(block_text.splitlines(keepends=True))
        if block_kind == BLOCK_KIND_CODE_FENCE:
            indices.update(range(line_index, line_index + line_count))
        line_index += line_count
    return indices


def _extract_rva_section(body: str) -> tuple[str, int, int] | None:
    """Extract the top-level RVA section with the shared GFM heading policy.

    Fenced examples cannot satisfy or terminate the section.  The accepted
    heading forms, indentation, and closing-hash handling are delegated to
    prose_boundary_policy rather than reproduced with a body-wide regex.
    """
    lines = body.splitlines(keepends=True)
    fenced = _fenced_line_indices(body)

    for start_index, line in enumerate(lines):
        if start_index in fenced:
            continue
        heading = parse_atx_heading_line(line.rstrip("\r\n"))
        if heading is None or heading["level"] != 2:
            continue
        policy = lookup_heading_policy(heading["text"])
        if not policy or policy.get("canonical_en") != "Runtime Verification Applicability":
            continue

        end_index = len(lines)
        for index in range(start_index + 1, len(lines)):
            if index in fenced:
                continue
            boundary = parse_atx_heading_line(lines[index].rstrip("\r\n"))
            if boundary is not None and boundary["level"] == 2:
                end_index = index
                break
        return "".join(lines[start_index + 1:end_index]), start_index + 1, end_index
    return None


def _is_canonical_implementation_issue(body: str) -> bool:
    """Use the shared MRC parser; malformed contracts cannot imply an issue kind."""
    contract = parse_machine_readable_contract(body)
    return contract.ok and contract.get("issue_kind") == "implementation"


def _load_extension_surface_policy_matcher():
    """Dynamically load `scripts/agent-guards/extension_surface_policy_matcher.py`.

    Mirrors this file's own `declared_path_overlap.py`-adjacent dynamic-load
    pattern for `scripts/agent-guards/changed_file_matcher.py` (Issue #2290
    "Notes for Reviewer": no new static import boundary / package). Returns
    `None` if the shared evaluator module cannot be located, so callers
    degrade gracefully rather than raising.
    """
    import importlib.util

    matcher_path = _REPO_ROOT / "scripts" / "agent-guards" / "extension_surface_policy_matcher.py"
    if not matcher_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "extension_surface_policy_matcher_for_contract_readiness_check", matcher_path
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def check_extension_surface_risk_trigger(body: str) -> list[dict]:
    """Issue #2290: declared Allowed Paths vs. extension-surface risk-trigger policy.

    Same-type addition as `check_rva_immediate_fields()` above -- uses the
    shared evaluator to detect a syntactic candidate overlap between the
    Issue's declared Allowed Paths and
    `docs/dev/extension-surface-runtime-policy.yaml` selectors, and (when
    `decision: immediate` is declared) whether the Runtime Verification
    Applicability contract's required immediate fields are present.
    Candidate discovery only -- not a semantic diff judgment of the actual
    PR change (Issue #2290 Out of Scope).
    """
    # Issue #2290 P1-1 fix delta (PR #2335 OWNER review): parity with
    # review-issue's `check_c14_extension_surface_risk_trigger()`, which
    # already guards on `issue_kind != "implementation"`. Non-implementation
    # Issues (research/etc.) do not declare a Runtime Verification
    # Applicability contract in the same sense, so this check is
    # not-applicable for them.
    if not _is_canonical_implementation_issue(body):
        return []

    allowed_path_entries = _extract_allowed_paths(body)
    if not allowed_path_entries:
        return []

    section = _extract_rva_section(body)
    rva_section_text = section[0] if section is not None else ""
    decision_match = re.search(r"decision:\s*(\S+)", rva_section_text)
    declared_decision = decision_match.group(1).strip() if decision_match else None

    evaluator = _load_extension_surface_policy_matcher()
    if evaluator is None:
        return []

    try:
        verdict = evaluator.evaluate_issue_risk_trigger(
            allowed_path_entries=allowed_path_entries,
            declared_decision=declared_decision,
            rva_section_text=rva_section_text,
        )
    except evaluator.PolicyLoadError as exc:
        # Issue #2290 P1-2 fix delta (PR #2335 OWNER review): distinguishable
        # from the "matcher module not found" [] above, and from an ordinary
        # "no candidate match" []. A malformed/incompatible policy file must
        # not silently look identical to "nothing to flag" -- it fail-closes
        # to a `human_judgment`-severity finding (see the
        # `extension_surface_risk_trigger_policy_unavailable` category branch
        # in the `run_contract_readiness_check` aggregate-status wiring)
        # instead of a body-author-fixable `needs_fix`.
        section_start_line = section[1] if section is not None else 0
        section_end_line = section[2] if section is not None else 0
        return [
            {
                "rule_id": "EXTSURF002",
                "severity": "error",
                "source_check": "contract_readiness_check",
                "category": "extension_surface_risk_trigger_policy_unavailable",
                "section": "Runtime Verification Applicability",
                "line_start": section_start_line,
                "line_end": section_end_line,
                "minimal_context": [f"extension-surface risk-trigger policy unavailable: {exc}"],
                "fix_hint": (
                    "The extension-surface risk-trigger policy "
                    "(docs/dev/extension-surface-runtime-policy.yaml) failed its "
                    "structural contract check and cannot be evaluated. This is "
                    "not body-author-fixable; escalate to a human/owner to repair "
                    "or restore the policy file."
                ),
                "autofixable": False,
            }
        ]

    if verdict["verdict"] != "needs_fix":
        return []

    section_start_line = section[1] if section is not None else 0
    section_end_line = section[2] if section is not None else 0

    return [
        {
            "rule_id": "EXTSURF001",
            "severity": "error",
            "source_check": "contract_readiness_check",
            "category": "extension_surface_risk_trigger",
            "section": "Runtime Verification Applicability",
            "line_start": section_start_line,
            "line_end": section_end_line,
            "minimal_context": verdict["reasons"],
            "fix_hint": (
                "Reconcile the declared Allowed Paths / Runtime Verification "
                "Applicability decision with the extension-surface risk-trigger "
                "policy (docs/dev/extension-surface-runtime-policy.yaml): "
                + "; ".join(verdict["reasons"])
            ),
            "autofixable": False,
        }
    ]


def check_rva_immediate_fields(body: str) -> list[dict]:
    """
    AC4: Check that `decision: immediate` RVA section has all required fields.

    Required for decision: immediate:
      applicable_acs, execution_environment, skip_conditions,
      fallback_policy, artifact_requirements

    Returns list of readiness errors (may be empty).
    """
    errors: list[dict] = []

    section = _extract_rva_section(body)
    if section is None:
        if not _is_canonical_implementation_issue(body):
            return []
        return [
            {
                "rule_id": "RVA002",
                "severity": "error",
                "source_check": "contract_readiness_check",
                "category": "rva_section_missing",
                "section": "Runtime Verification Applicability",
                "line_start": 0,
                "line_end": 0,
                "minimal_context": ["Runtime Verification Applicability section is missing."],
                "fix_hint": (
                    "Add the Runtime Verification Applicability section and explicitly choose "
                    "not_applicable, immediate, or deferred. Do not infer the decision."
                ),
                "autofixable": False,
            }
        ]

    section_content, section_start_line, section_end_line = section
    if not re.search(r"decision:\s*immediate", section_content, re.IGNORECASE):
        return []

    missing_fields: list[str] = []
    for field in _RVA_IMMEDIATE_REQUIRED_FIELDS:
        # Check for field as a direct YAML key (possibly inside a yaml block)
        simple_pattern = re.compile(rf"^\s*{re.escape(field)}\s*:", re.MULTILINE)
        if not simple_pattern.search(section_content):
            missing_fields.append(field)

    for field in missing_fields:
        # Build context from first non-empty lines of section
        context_lines: list[str] = []
        for line in section_content.split("\n"):
            if line.strip():
                context_lines.append(line)
            if len(context_lines) >= 3:
                break

        errors.append(
            {
                "rule_id": "RVA001",
                "severity": "error",
                "source_check": "contract_readiness_check",
                "category": "rva_immediate_field_missing",
                "section": "Runtime Verification Applicability",
                "line_start": section_start_line,
                "line_end": section_end_line,
                "minimal_context": context_lines,
                "fix_hint": (
                    f"Add '{field}' field to Runtime Verification Applicability section. "
                    "Required fields for decision: immediate are: "
                    + ", ".join(_RVA_IMMEDIATE_REQUIRED_FIELDS)
                ),
                "autofixable": True,
            }
        )

    return errors


# ---------------------------------------------------------------------------
# AC4 (#1346): Required Design References check (implementation issues only)
# ---------------------------------------------------------------------------

# AC10 (#1346): build the RDR heading regex from prose_boundary_policy.py's
# HEADING_POLICY accepted_forms so this static checker recognises the same heading
# variants (including Japanese forms) as the authoring-side prose boundary guard.
_RDR_ACCEPTED_HEADINGS = HEADING_POLICY["Required Design References"]["accepted_forms"]
_RDR_HEADING_ALT = "|".join(re.escape(h) for h in _RDR_ACCEPTED_HEADINGS)
_RDR_SECTION_RE = re.compile(
    rf"^##\s+(?:{_RDR_HEADING_ALT})\s*$(.+?)(?=^##|\Z)",
    re.MULTILINE | re.DOTALL,
)

# AC11 (#1346): design-doc path references must point at an actual, narrowly-scoped
# design-doc location (docs/**/*.md|yml, .claude/skills/**/SKILL.md,
# .claude/skills/**/references/**/*.md). src/ and scripts/ are intentionally excluded:
# those are implementation paths, not design-doc references.
_REQUIRED_DESIGN_REFERENCES_PATH_RE = re.compile(
    r"(?:^|[\s(`\[])("
    r"docs/[\w\-./]+\.(?:md|yml)"
    r"|\.claude/skills/[\w\-./]+/SKILL\.md"
    r"|\.claude/skills/[\w\-./]+/references/[\w\-./]+\.md"
    r")"
)

_PLACEHOLDER_ONLY_VALUES = {"", "n/a", "none", "なし", "-"}


def _extract_issue_kind(body: str) -> Optional[str]:
    """Extract `issue_kind` from the `## Machine-Readable Contract` YAML block.

    Self-contained regex extraction — does NOT forward --kind to
    validate_issue_body.py (AC6: keep responsibility boundaries intact,
    do not change existing kind-agnostic fixture behavior).
    """
    mrc_match = re.search(
        r"^##\s+Machine-Readable Contract\s*$(.+?)(?=^##|\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not mrc_match:
        return None
    kind_match = re.search(r"^\s*issue_kind:\s*(\S+)", mrc_match.group(1), re.MULTILINE)
    if not kind_match:
        return None
    return kind_match.group(1).strip().strip('"').strip("'")


def check_required_design_references(body: str) -> list[dict]:
    """
    AC4: For `issue_kind: implementation` issues, validate the
    `## Required Design References` section (when present) is not
    empty / N/A / none-only, and contains at least one repo-relative
    design-doc path reference (e.g. docs/dev/agent-skill-boundaries.md).

    Mirrors the RVA precedent (check_rva_immediate_fields): when the
    section is entirely absent, this function returns no errors here
    (existence enforcement is a template / review-issue concern, not this
    static checker — AC6: do not regress existing go fixtures that predate
    this section).
    """
    if _extract_issue_kind(body) != "implementation":
        return []

    section_match = _RDR_SECTION_RE.search(body)
    if not section_match:
        return []

    section_content = section_match.group(1)
    section_start_line = body[: section_match.start()].count("\n") + 1

    stripped = section_content.strip()
    non_empty_lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    is_placeholder_only = (not non_empty_lines) or all(
        line.lower().lstrip("- ").strip() in _PLACEHOLDER_ONLY_VALUES for line in non_empty_lines
    )

    # AC11 (#1346): a candidate path is only a valid design-doc reference when it
    # (a) matches the narrowed docs/**|.claude/skills/**/SKILL.md|.claude/skills/**/references/**
    #     shape, and (b) actually exists in the repo (Path.exists()).
    has_path_ref = False
    for match in _REQUIRED_DESIGN_REFERENCES_PATH_RE.finditer(section_content):
        candidate = match.group(1)
        if (_REPO_ROOT / candidate).exists():
            has_path_ref = True
            break

    if is_placeholder_only or not has_path_ref:
        errors = [
            {
                "rule_id": "RDR001",
                "severity": "error",
                "source_check": "contract_readiness_check",
                "category": "required_design_references_missing_or_empty",
                "section": "Required Design References",
                "line_start": section_start_line,
                "line_end": section_start_line + section_content.count("\n"),
                "minimal_context": non_empty_lines[:3],
                "fix_hint": (
                    "Add at least one repo-relative, *existing* design-doc path reference "
                    "(e.g. docs/dev/agent-skill-boundaries.md, .claude/skills/<skill>/SKILL.md, "
                    "or .claude/skills/<skill>/references/<doc>.md) to Required Design "
                    "References. Do not leave it empty / N/A / none only. "
                    "See body-authoring.md#Required Design References Authoring Guidance."
                ),
                # AC11 (#1346): not autofixable — the correct design-doc reference requires
                # human judgment about which SSOT the issue actually depends on.
                "autofixable": False,
            }
        ]
        return errors

    return []


# ---------------------------------------------------------------------------
# Static VC syntax check (compound command detection without execution)
# ---------------------------------------------------------------------------


def check_vc_static_syntax(body: str) -> list[dict]:
    """
    Static-only check of Verification Commands for compound shell operators.

    Does NOT execute any commands. Used in --mode static (the default).
    Returns list of errors.
    """
    errors: list[dict] = []

    vc_match = re.search(
        r"^##\s+Verification Commands\s*$(.+?)(?=^##|\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not vc_match:
        return []

    vc_section = vc_match.group(1)
    section_start_line = body[: vc_match.start()].count("\n") + 2  # +2 for header

    # Sync operator set with body-authoring.md#VC_SINGLE_COMMAND_GUARDRAIL
    # Redirect operators (<, >, <<, >>, <<<) are NOT enforced here:
    # they risk false positives with placeholder syntax (e.g., <file>, <pattern>).
    # Only control operators that affect exit-code reliability are hard errors.
    compound_operators = frozenset(["&&", "||", "|", ";", "&"])

    # B4: only ```bash fenced blocks are canonical VC format; unlabeled fences are ignored
    for block_match in re.finditer(r"```bash[ \t]*\n(.*?)```", vc_section, re.DOTALL):
        block_content = block_match.group(1)
        block_start_in_section = vc_section[: block_match.start()].count("\n")
        block_abs_start = section_start_line + block_start_in_section

        for line_offset, line in enumerate(block_content.split("\n")):
            stripped = line.strip()
            # Skip comments and empty lines
            if not stripped or stripped.startswith("#"):
                continue
            # Strip leading $ prefix
            cmd = re.sub(r"^\$\s*", "", stripped)
            if not cmd or cmd.startswith("#"):
                continue

            try:
                lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
                tokens = list(lexer)
                if any(t in compound_operators for t in tokens):
                    abs_line = block_abs_start + line_offset + 2
                    errors.append(
                        {
                            "rule_id": "VCS001",
                            "severity": "error",
                            "source_check": "contract_readiness_check",
                            "category": "compound_command_disallowed",
                            "section": "Verification Commands",
                            "line_start": abs_line,
                            "line_end": abs_line,
                            "minimal_context": [line],
                            "fix_hint": (
                                "Remove compound shell operators (&&, ||, |, ;, &) from VC. "
                                "Use a single command per VC. "
                                "See body-authoring.md#VC_SINGLE_COMMAND_GUARDRAIL."
                            ),
                            "autofixable": False,
                        }
                    )
            except ValueError:
                abs_line = block_abs_start + line_offset + 2
                errors.append(
                    {
                        "rule_id": "VCS001",
                        "severity": "error",
                        "source_check": "contract_readiness_check",
                        "category": "compound_command_disallowed",
                        "section": "Verification Commands",
                        "line_start": abs_line,
                        "line_end": abs_line,
                        "minimal_context": [line],
                        "fix_hint": (
                            "Command syntax could not be parsed. "
                            "Simplify to a single command."
                        ),
                        "autofixable": False,
                    }
                )

    return errors


# ---------------------------------------------------------------------------
# Aggregate status computation
# ---------------------------------------------------------------------------


def compute_aggregate_status(
    validate_errors: list[dict],
    preflight_errors: list[dict],
    rva_errors: list[dict],
    static_vc_errors: list[dict],
    preflight_aggregate: str,
    existing_readiness_errors: Optional[list[dict]] = None,
) -> str:
    """
    Compute overall readiness status from all sources.
    Priority: human_judgment > needs_fix > go.
    """
    status = "go"

    # validate_issue_body errors: body-author-fixable → needs_fix
    # validator_tool_error / validator_internal_error → human_judgment (not author-fixable)
    if any(e.get("severity") == "error" for e in validate_errors):
        # Check if errors come from a tool/internal failure (not body-author-fixable)
        tool_error_rule_ids = {"VALIDATOR_TIMEOUT", "VALIDATOR_INTERNAL", "VALIDATOR_JSON_ERROR"}
        if any(e.get("rule_id") in tool_error_rule_ids for e in validate_errors):
            status = _raise_status(status, "human_judgment")
        else:
            status = _raise_status(status, "needs_fix")

    if existing_readiness_errors:
        status = _raise_status(status, "needs_fix")

    # RVA immediate field errors: author can add fields → needs_fix
    if rva_errors:
        status = _raise_status(status, "needs_fix")

    # Static VC errors: compound commands → needs_fix
    if static_vc_errors:
        status = _raise_status(status, "needs_fix")

    # Preflight aggregate (execute mode only)
    status = _raise_status(status, preflight_aggregate)

    return status


# ---------------------------------------------------------------------------
# Main result builder
# ---------------------------------------------------------------------------


def build_result(
    body: str,
    mode: str,
    validate_result: dict,
    preflight_result: Optional[dict],
    preflight_exit_code: Optional[int],
    preflight_skip_reason_code: Optional[str] = None,
) -> dict:
    """Build ISSUE_CONTRACT_READINESS_RESULT_V1 from all check results.

    preflight_skip_reason_code: when set (and preflight_result is None), a
    "not_applicable" baseline_vc_preflight entry is recorded in
    source_checks[] with this reason_code, so a deliberate execute-mode skip
    (canonical parent body without a `## Verification Commands` section,
    #1867) is machine-distinguishable from "not run because --mode != execute"
    (static / preflight-static modes leave source_checks unchanged, i.e. no
    baseline_vc_preflight entry at all — PR #1878 P2 review).
    """
    body_sha256 = sha256_of(body)

    validate_status = validate_result.get("status", "fail")
    validate_exit_code = 0 if validate_status == "pass" else 1

    source_checks: list[dict] = [
        {
            "name": "validate_issue_body",
            "schema": "loop_body_lint/v1",
            "status": validate_status,
            "exit_code": validate_exit_code,
        }
    ]

    if preflight_result is not None:
        preflight_status = preflight_result.get("status", "blocked")
        source_checks.append(
            {
                "name": "baseline_vc_preflight",
                "schema": "baseline_vc_preflight/v1",
                "status": preflight_status,
                "exit_code": preflight_exit_code if preflight_exit_code is not None else -1,
            }
        )
    elif preflight_skip_reason_code is not None:
        source_checks.append(
            {
                "name": "baseline_vc_preflight",
                "schema": "baseline_vc_preflight/v1",
                "status": "not_applicable",
                "reason_code": preflight_skip_reason_code,
                "exit_code": None,
            }
        )

    validate_errors = map_validate_errors_to_readiness_errors(validate_result)
    existing_readiness_errors = check_existing_issue_readiness_semantics(body)
    rva_errors = check_rva_immediate_fields(body)
    rdr_errors = check_required_design_references(body)
    ext_surface_errors = check_extension_surface_risk_trigger(body)

    preflight_errors: list[dict] = []
    preflight_aggregate = "go"
    if preflight_result is not None:
        preflight_errors, preflight_aggregate = map_preflight_result_to_errors(
            preflight_result
        )

    # Static VC syntax check: in static/preflight-static mode (execute mode uses preflight)
    static_vc_errors: list[dict] = []
    if mode in ("static", "preflight-static"):
        static_vc_errors = check_vc_static_syntax(body)

    all_errors = (
        validate_errors
        + existing_readiness_errors
        + rva_errors
        + rdr_errors
        + ext_surface_errors
        + static_vc_errors
        + preflight_errors
    )

    overall_status = compute_aggregate_status(
        validate_errors,
        preflight_errors,
        rva_errors,
        static_vc_errors,
        preflight_aggregate,
        existing_readiness_errors,
    )
    if rdr_errors:
        overall_status = _raise_status(overall_status, "needs_fix")
    if ext_surface_errors:
        # Issue #2290 P1-2 fix delta (PR #2335 OWNER review): a policy-load
        # failure is not body-author-fixable, so it escalates to
        # human_judgment instead of the ordinary needs_fix path used by a
        # genuine Allowed-Paths-vs-policy candidate match.
        if any(
            e.get("category") == "extension_surface_risk_trigger_policy_unavailable"
            for e in ext_surface_errors
        ):
            overall_status = _raise_status(overall_status, "human_judgment")
        else:
            overall_status = _raise_status(overall_status, "needs_fix")

    fix_hint: Optional[str] = None
    minimal_context: list = []
    if all_errors:
        first_error = all_errors[0]
        fix_hint = first_error.get("fix_hint")
        minimal_context = first_error.get("minimal_context", [])

    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "status": overall_status,
        "body_sha256": body_sha256,
        "source_checks": source_checks,
        "errors": all_errors,
        "minimal_context": minimal_context,
        "fix_hint": fix_hint,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Contract Readiness Check: returns ISSUE_CONTRACT_READINESS_RESULT_V1 JSON"
    )
    parser.add_argument("--body-file", help="Path to issue body file")
    parser.add_argument(
        "--issue", "--issue-number", dest="issue", type=int, help="GitHub Issue number"
    )
    parser.add_argument(
        "--repo", default="squne121/loop-protocol", help="GitHub repo (owner/name)"
    )
    parser.add_argument(
        "--mode",
        choices=["static", "preflight-static", "execute"],
        default="static",
        help=(
            "static (default): VC syntax/section/schema only; no execution, no network. "
            "preflight-static: alias for static; use in review-issue / issue-reviewer callers. "
            "  Detects compound_command_disallowed statically. "
            "  unexpected_pass detection requires --mode execute (execution-only signal). "
            "execute: also runs baseline_vc_preflight.py to execute VCs."
        ),
    )

    args = parser.parse_args()

    # Acquire body
    body: Optional[str] = None
    error_code: Optional[str] = None

    if args.body_file:
        body, error_code = read_body_file(args.body_file)
    elif args.issue:
        body, error_code = fetch_body_from_github(args.issue, args.repo)
    else:
        print("ERROR: --body-file or --issue required", file=sys.stderr)
        return 3

    if body is None:
        error_result = {
            "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
            "status": "human_judgment",
            "body_sha256": "sha256:",
            "source_checks": [],
            "errors": [
                {
                    "rule_id": "INPUT001",
                    "severity": "error",
                    "source_check": "contract_readiness_check",
                    "category": "input_error",
                    "section": "(global)",
                    "line_start": 0,
                    "line_end": 0,
                    "minimal_context": [error_code or "unknown"],
                    "fix_hint": f"Input error: {error_code}",
                    "autofixable": False,
                }
            ],
            "minimal_context": [],
            "fix_hint": f"Input error: {error_code}",
        }
        print(json.dumps(error_result, indent=2))
        return 3

    # Run validate_issue_body (always)
    validate_result = run_validate_issue_body(body)

    # Run baseline_vc_preflight only in execute mode.
    # #1867: canonical parent Issue (issue_kind: parent) does not require a
    # `## Verification Commands` section under existing_issue_readiness_v1,
    # so baseline_vc_preflight() must be skipped for canonical parent bodies
    # that have no `## Verification Commands` section, to avoid a spurious
    # VC001_NO_VERIFICATION_COMMANDS_SECTION extraction error. A canonical
    # parent body that DOES carry a `## Verification Commands` section is not
    # skipped: if a parent author opts into VCs, those VCs must still be
    # executed and their pass/fail outcome must be reflected (PR #1878 P1
    # review). implementation / research / unknown kind / malformed MRC /
    # parse failure / label-only-parent-with-implementation-MRC bodies are
    # NOT skipped and retain the existing execution behavior.
    preflight_result: Optional[dict] = None
    preflight_exit_code: Optional[int] = None
    preflight_skip_reason_code: Optional[str] = None
    if args.mode == "execute":  # preflight-static is static-only; no execution
        resolution = resolve_existing_issue_validation_profile(body)
        is_canonical_parent = (
            resolution.status == "profile" and resolution.canonical_issue_kind == "parent"
        )
        parent_has_vc_section = is_canonical_parent and bool(
            extract_verification_commands_section(body)
        )
        skip_preflight = is_canonical_parent and not parent_has_vc_section
        if not skip_preflight:
            preflight_result, preflight_exit_code = run_baseline_vc_preflight(body)
        else:
            # #1878 P2 review: record a machine-readable "not_applicable"
            # source_checks entry so a deliberate skip is distinguishable
            # from missing wiring (see build_result() docstring).
            preflight_skip_reason_code = "canonical_parent_without_verification_commands"

    result = build_result(
        body,
        args.mode,
        validate_result,
        preflight_result,
        preflight_exit_code,
        preflight_skip_reason_code=preflight_skip_reason_code,
    )

    print(json.dumps(result, indent=2))

    status = result["status"]
    if status == "go":
        return 0
    elif status == "needs_fix":
        return 1
    elif status == "human_judgment":
        return 2
    else:  # runtime_error (Issue #2165 P0-1): distinct exit code, never
        # collapsed into the needs_fix(1)/human_judgment(2) semantic range.
        return 4


if __name__ == "__main__":
    sys.exit(main())

