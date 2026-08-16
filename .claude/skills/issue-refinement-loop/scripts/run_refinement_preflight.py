#!/usr/bin/env python3
"""
run_refinement_preflight.py

Deterministic entrypoint that assembles planner input from GitHub API data,
validates anchor comments structurally, invokes plan_refinement_loop.py with
correctly-formed stdin JSON, and writes a compact result artifact.

Usage:
    uv run python3 run_refinement_preflight.py \\
        --issue-number <N> \\
        --repo <owner/name> \\
        [--anchor-comment-url <URL> ...] \\
        [--fixture <path>]

Output (stdout): compact projection of refinement_preflight_result/v1 artifact.

Canonical stdout fields:
    STATUS       - pass | warn | needs_fix | blocked | environment_failure (always present)
    NEXT_ACTION  - routing instruction (always present)
    MUST_READ    - files/paths to read before proceeding (omitted if empty)
    COMMANDS     - argv-only command templates (omitted if empty)
    BLOCKERS     - blocker reason codes (omitted if empty)
    ARTIFACT     - artifact key: absolute_path pairs (omitted if empty)
    REQUIRED_SECTIONS - required sections from rewrite constraints (planner-derived)
    REQUIRED_CONTRACT_KEYS - required contract keys from rewrite constraints (planner-derived)
    REWRITE_CONSTRAINTS - planner rewrite constraints payload when fail_closed=true
    REPAIR_ACTION - versioned repair_action disposition (Issue #2016; omitted
                    unless STATUS: needs_fix, i.e. disposition: auto_apply_safe)

Non-canonical / suppressed fields:
    SUMMARY      - human-only prose, not consumed by orchestrators
    DO_NOT_READ  - reserved, currently empty; consumers MUST NOT rely on absence
    EVIDENCE     - raw issue body / comments; NEVER emitted to stdout

Artifact (file):  .claude/artifacts/issue-refinement-loop/<issue_number>/
                  refinement_preflight_result_v1.json  (canonical result)
                  raw_issue_snapshot.json              (raw issue + comments)
                  planner_input.json                   (planner stdin, byte-stable)

Exit codes:
    0 - pass (planner succeeded, fail_closed.required == false, no unknown confidence)
    1 - warn (planner exit 0, fail_closed.required == false, >=1 decision with
              confidence: unknown — human note needed but not blocking)
    2 - blocked (anchor mismatch, planner exit 2, or planner fail_closed.required == true)
    3 - environment_failure (gh not found / auth / API / timeout / non-JSON)
    4 - needs_fix (repair_issue_contract classified >=1 known-safe deterministic
                    repair as auto_apply_safe; Issue #2016)

Planner ↔ Wrapper Exit Code Mapping:
    anchor comment not in issue                    → blocked  / 2
    gh not found / auth / API fail / timeout / JSON → environment_failure / 3
    planner exit 2 (invalid input)                  → blocked  / 2
    planner exit 3 (internal error)                 → environment_failure / 3
    planner exit 0 + fail_closed.required == true   → blocked  / 2
    planner exit 0, fail_closed=false, no unknown   → pass     / 0
    planner exit 0, fail_closed=false, >=1 unknown  → warn     / 1

warn (exit 1) definition:
    planner exit 0 AND fail_closed.required == false
    AND decisions.*.confidence contains at least one "unknown"
    → status: warn / exit 1 (human note needed, but not fully blocking)

needs_fix (exit 4) definition (Issue #2016):
    repair_issue_contract.py's repair_action.disposition == "auto_apply_safe"
    (>=1 known-safe deterministic repair, no unsafe/unknown/mixed/overlapping
    repair present) AND no other blocker (anchor/env/planner) is present
    → status: needs_fix / exit 4, next_action: apply_deterministic_repair.
    This route is orthogonal to the pre-existing pass/warn/blocked/
    environment_failure mapping above: it only overrides an otherwise
    pass/warn outcome, and never overrides blocked. Other repair_action
    dispositions (human_review_required / informational / invalid_payload)
    do NOT change the mapping below: human_review_required routes through
    the pre-existing generic repair_diagnostics blocker (→ blocked), and
    invalid_payload / subprocess-level repair failures route through
    BLOCKER_REPAIR_ENVIRONMENT_FAILURE (→ environment_failure).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import io
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Runtime schema validation (jsonschema >= 4.0, already in pyproject.toml)
# ---------------------------------------------------------------------------

try:
    import jsonschema as _jsonschema

    _JSONSCHEMA_AVAILABLE = True
except ImportError:
    _JSONSCHEMA_AVAILABLE = False


# ---------------------------------------------------------------------------
# #1677 AC4/AC12: reuse plan_refinement_loop.py's normative semantic
# validator instead of re-implementing ISSUE_EXECUTION_DECISION_V1
# invariants here. Import is best-effort (subprocess/CLI callers of this
# module do not require it; only _join_scope_rollup_into_planner_input's
# self-check below uses it).
# ---------------------------------------------------------------------------

import sys as _sys_for_import

_sys_for_import.path.insert(0, str(Path(__file__).resolve().parent))
try:
    # PR #1767 owner review (P0-4/AC12 Scope Delta): import the standalone
    # canonical module directly rather than re-exporting through
    # plan_refinement_loop.py, so every consumer shares one authority.
    from validate_issue_execution_decision import validate_issue_execution_decision
except ImportError:  # pragma: no cover - defensive fallback
    validate_issue_execution_decision = None

try:
    # #1891: pure analyzer for anchor comment multi-turn segmentation and
    # candidate extraction. anchor_context.py has no GitHub API client of its
    # own; it only consumes the already-fetched anchor_comment.snapshot body
    # that this module builds below (AC8).
    import anchor_context
except ImportError:  # pragma: no cover - defensive fallback
    anchor_context = None

try:
    # Issue #2016: producer-side repair_action disposition classifier.
    # Imported directly (not re-implemented here) so the wrapper and the
    # standalone CLI share a single source of truth for the closed-enum
    # disposition rule.
    from repair_issue_contract import classify_repair_action
except ImportError:  # pragma: no cover - defensive fallback
    classify_repair_action = None

_IMPL_MAIN_DRIFT_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "impl-review-loop" / "scripts"
if str(_IMPL_MAIN_DRIFT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_IMPL_MAIN_DRIFT_SCRIPTS_DIR))
try:
    # Issue #2102 fix_delta (iteration 5, Blocker A): mirrors
    # plan_refinement_loop.py's own best-effort import of the same symbol.
    # This wrapper must not populate known_context["main_drift"] (a bounded
    # live git fetch/diff/merge-tree probe) when the planner it feeds cannot
    # even import the classifier that consumes that key -- a harness that
    # only provisions this skill's own scripts/ directory (not its
    # impl-review-loop sibling) would otherwise pay the live-readback cost
    # only to have plan_refinement_loop.py hard_stop with
    # main_drift_policy_import_failed regardless of the readback's content.
    from route_loop_verdict_v2 import classify_main_drift as _main_drift_classifier_probe
except ImportError:  # pragma: no cover - defensive fallback
    _main_drift_classifier_probe = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
_SCHEMAS_DIR = _SCRIPTS_DIR.parent / "schemas"
PLANNER_SCRIPT = _SCRIPTS_DIR / "plan_refinement_loop.py"
REPAIR_SCRIPT = _SCRIPTS_DIR / "repair_issue_contract.py"

SCHEMA_VERSION_RESULT = "refinement_preflight_result/v1"
SCHEMA_VERSION_PLANNER_INPUT = "refinement_loop_planner_input/v1"
SCHEMA_VERSION_INPUT_FIXTURE = "refinement_preflight_input/v1"

# Timeout constants (seconds)
GH_API_TIMEOUT = 30
PLANNER_TIMEOUT = 60

# PR #2202 review fix-delta (P0-6): `repair_action.apply`'s worst-case inner
# critical path runs the readiness-check subprocess and the
# edit_issue_txn.py subprocess SEQUENTIALLY inside a single
# `run_refinement_preflight.py --apply-repair-action` process invocation.
# The registry-level outer supervisor timeout for the `repair_action.apply`
# command_id (command_registry.py REGISTRY["repair_action.apply"]
# ["timeout_seconds"]) MUST stay strictly greater than
# REPAIR_APPLY_READINESS_SUBPROCESS_TIMEOUT_SECONDS +
# REPAIR_APPLY_EDIT_ISSUE_TXN_SUBPROCESS_TIMEOUT_SECONDS +
# REPAIR_APPLY_READBACK_RESERVE_SECONDS + margin -- otherwise the outer
# supervisor can kill this process mid-dispatch (after a PATCH may already
# have been sent to GitHub) before the AC5 authoritative-readback path ever
# runs, silently converting a genuinely-mutated Issue into a reported
# failure/crash instead of a resolvable `unknown` outcome.
REPAIR_APPLY_READINESS_SUBPROCESS_TIMEOUT_SECONDS = 30
REPAIR_APPLY_EDIT_ISSUE_TXN_SUBPROCESS_TIMEOUT_SECONDS = 60
# One bounded `_fetch_issue()` GitHub read (GH_API_TIMEOUT-bounded) is the
# most this function ever spends resolving the AC5 authoritative readback
# after a TimeoutExpired/OSError/unparseable-stdout `unknown` outcome.
REPAIR_APPLY_READBACK_RESERVE_SECONDS = GH_API_TIMEOUT
# inner_total(90) + readback_reserve(30) = 120; command_registry.py's
# `repair_action.apply["timeout_seconds"]` is set to 150 (30s margin above
# that combined worst case) -- see the comment on that registry entry.
REPAIR_APPLY_OUTER_TIMEOUT_MARGIN_SECONDS = 30

# Exit codes
EXIT_PASS = 0
EXIT_WARN = 1
EXIT_BLOCKED = 2
EXIT_ENVIRONMENT_FAILURE = 3
EXIT_NEEDS_FIX = 4

# Blocker reason codes
BLOCKER_ANCHOR_NOT_IN_ISSUE = "ANCHOR_NOT_IN_ISSUE"
BLOCKER_ANCHOR_IS_PR_REVIEW = "ANCHOR_IS_PR_REVIEW_COMMENT"
BLOCKER_GH_FAILURE = "GH_API_FAILURE"
BLOCKER_PLANNER_INVALID_INPUT = "PLANNER_INVALID_INPUT"
BLOCKER_PLANNER_INTERNAL_ERROR = "PLANNER_INTERNAL_ERROR"
BLOCKER_FAIL_CLOSED = "PLANNER_FAIL_CLOSED"
BLOCKER_ANCHOR_REPO_MISMATCH = "ANCHOR_REPO_MISMATCH"
BLOCKER_ANCHOR_ISSUE_NUMBER_MISMATCH = "ANCHOR_ISSUE_NUMBER_MISMATCH"
BLOCKER_ANCHOR_COMMENT_NOT_FOUND = "ANCHOR_COMMENT_NOT_FOUND"
BLOCKER_ANCHOR_ISSUE_URL_MISMATCH = "ANCHOR_ISSUE_URL_MISMATCH"
BLOCKER_ANCHOR_COMMENT_SCHEMA_INVALID = "ANCHOR_COMMENT_SCHEMA_INVALID"
BLOCKER_ANCHOR_COMMENT_MULTIPLE_UNSUPPORTED = "ANCHOR_COMMENT_MULTIPLE_UNSUPPORTED"
BLOCKER_INPUT_SCHEMA_INVALID = "INPUT_SCHEMA_INVALID"
BLOCKER_RESULT_SCHEMA_INVALID = "RESULT_SCHEMA_INVALID"
BLOCKER_INVALID_ARGS = "INVALID_ARGS"
BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD = "REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD"
BLOCKER_REWRITE_CONSTRAINTS_NOT_JSON_SERIALIZABLE = "REWRITE_CONSTRAINTS_NOT_JSON_SERIALIZABLE"
BLOCKER_REWRITE_CONSTRAINTS_INVARIANT_VIOLATION = "REWRITE_CONSTRAINTS_INVARIANT_VIOLATION"
BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID = "planner_fail_closed_payload_invalid"
BLOCKER_ARTIFACT_PROJECTION_MISMATCH = "ARTIFACT_PROJECTION_MISMATCH"
BLOCKER_ISSUE_EXECUTION_DECISION_INVALID = "ISSUE_EXECUTION_DECISION_INVALID"
BLOCKER_ISSUE_EXECUTION_DECISION_VALIDATOR_UNAVAILABLE = "ISSUE_EXECUTION_DECISION_VALIDATOR_UNAVAILABLE"
# #1891 iteration 2 (PR #1923 OWNER REQUEST_CHANGES): the multi-turn-candidate
# route and the heavy-mutation gate must actually reach the `blockers` list
# consumed by `_apply_exit_code_mapping()`, not just live inside
# `known_context` where the planner never inspects them.
BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED = "ANCHOR_MULTI_TURN_FAIL_CLOSED"

# PR #2171 fix_delta (P0-1, OWNER adversarial review): the two distinct
# fail_closed reasons `_apply_multi_turn_candidate_route()` can emit for a
# multi-turn anchor -- the pre-existing hard block
# ("multi_turn_anchor_context_requires_human_judgment") and the
# integrity-unconfirmed forced-blocking route
# ("multi_turn_anchor_context_retrieval_integrity_unconfirmed") -- both
# must surface as BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED downstream.
ANCHOR_MULTI_TURN_FAIL_CLOSED_REASONS = frozenset(
    {
        "multi_turn_anchor_context_requires_human_judgment",
        "multi_turn_anchor_context_retrieval_integrity_unconfirmed",
    }
)
BLOCKER_HEAVY_MUTATION_FAIL_CLOSED = "HEAVY_MUTATION_FAIL_CLOSED"
BLOCKER_REPAIR_ENVIRONMENT_FAILURE = "REPAIR_ENVIRONMENT_FAILURE"


def _render_artifact_projection_lines(artifacts: dict[str, str]) -> list[str]:
    lines: list[str] = ["ARTIFACT:"]
    for key, value in sorted(artifacts.items()):
        lines.append(f"  {key}: {value}")
    return lines


def _validate_artifact_projection(*, repo_root: Path, issue_number: int, artifacts: dict[str, str]) -> list[str]:
    if not artifacts:
        return []

    expected_root = (repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)).resolve()
    failures: list[str] = []

    for key, raw_path in artifacts.items():
        if not isinstance(raw_path, str) or not raw_path:
            failures.append(f"artifact_path_invalid: {key!r}:{raw_path!r}")
            continue

        try:
            resolved = Path(raw_path).resolve()
        except Exception:
            failures.append(f"artifact_path_unresolvable: {key} -> {raw_path!r}")
            continue

        if resolved != expected_root and not resolved.is_relative_to(expected_root):
            normalized = (
                resolved.relative_to(repo_root).as_posix() if resolved.is_relative_to(repo_root) else str(resolved)
            )
            failures.append(f"artifact_path_outside_issue_root: {key} -> {normalized}")
            continue

        if not resolved.exists():
            normalized = (
                resolved.relative_to(repo_root).as_posix() if resolved.is_relative_to(repo_root) else str(resolved)
            )
            failures.append(f"artifact_path_missing: {key} -> {normalized}")

    return failures


# Trusted author associations for ANCHOR_SCOPE_REFRAME_V1
TRUSTED_ANCHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

# Trusted lanes for source attribution.
_HUMAN_CONTEXT_COMMENT_URLS_FIELD = "human_context_comment_urls"
_AGENT_REPORT_COMMENT_URLS_FIELD = "agent_report_comment_urls"


def _normalize_comment_url_set(value: Any) -> set[str] | None:
    """Return a normalized set of URLs or `None` when malformed.

    Malformed explicit-lane fields are treated as untrusted control-plane
    input (fail-closed) so they cannot be used to infer a trusted origin.
    """
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if not isinstance(value, (list, tuple, set)):
        return None
    urls: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            return None
        urls.add(item)
    return urls


def _resolve_scope_delta_source_kind(
    anchor_url: str,
    *,
    human_context_comment_urls: Any,
    agent_report_comment_urls: Any,
) -> str:
    """Resolve source kind from control-plane explicit lanes only."""
    human_urls = _normalize_comment_url_set(human_context_comment_urls)
    agent_urls = _normalize_comment_url_set(agent_report_comment_urls)

    # Any unrecognized or unlabeled lane payload is fail-closed.  An anchor
    # URL alone is not an origin lane: callers must state whether it is human
    # context or an agent report.
    if human_urls is None or agent_urls is None:
        return "generated_by_agent"

    in_human = anchor_url in human_urls
    in_agent = anchor_url in agent_urls

    # Fail-closed on duplicate lane identity.
    if in_human and in_agent:
        return "generated_by_agent"

    if in_agent:
        return "generated_by_agent"
    if in_human:
        return "issue_comment"
    return "generated_by_agent"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    """Compute SHA256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# PR #2202 review fix (P0-2): producer-side provenance-lane determination
# for repair_action.apply. Reuses the SAME control-plane-explicit-lane
# classifier already used for scope-delta authority evidence
# (_resolve_scope_delta_source_kind) rather than inventing a second
# classification path, translated into the repair_action.apply vocabulary
# (human_context/anchor/unanchored, see repair_apply_result_v1.schema.json)
# established by the AC9 fresh-validation lane-preservation check.
def _determine_repair_source_lane(anchor_url: "str | None", known_context: "dict | None") -> str:
    """Resolve the repair-lane provenance the current preflight run executed
    under: 'unanchored' (no anchor comment at all), 'human_context' (an
    operator-labeled human-context anchor), or 'anchor' (an agent-authored /
    unlabeled anchor)."""
    if not anchor_url:
        return "unanchored"
    source_kind = _resolve_scope_delta_source_kind(
        anchor_url,
        human_context_comment_urls=(
            known_context.get(_HUMAN_CONTEXT_COMMENT_URLS_FIELD) if isinstance(known_context, dict) else None
        ),
        agent_report_comment_urls=(
            known_context.get(_AGENT_REPORT_COMMENT_URLS_FIELD) if isinstance(known_context, dict) else None
        ),
    )
    return "human_context" if source_kind == "issue_comment" else "anchor"


def _repair_source_refs_digest(source_lane: str, anchor_url: "str | None") -> "str | None":
    """Digest binding the source refs (anchor comment URL/id) a repair_action
    producer run relied on. None for the unanchored lane (Issue #2039 AC3)."""
    if source_lane == "unanchored" or not anchor_url:
        return None
    parsed_anchor = _parse_anchor_comment_url(anchor_url)
    return "sha256:" + _sha256(
        _canonical_json({"anchor_url": anchor_url, "comment_id": parsed_anchor.get("comment_id")})
    )


def _repair_preflight_run_identity(
    *, issue_number: int, repo: str, original_body_sha256: str, captured_at: str, source_lane: str
) -> str:
    """A per-run identity for the producer run that generated a
    repair_action candidate (Issue #2039 AC3). Deliberately independent of
    `hashes.result_core_sha256` (which is itself computed from a
    `result_core_for_hash` dict that MAY embed `repair_action_core` -- using
    it here would be self-referential)."""
    return "sha256:" + _sha256(
        _canonical_json(
            {
                "issue_number": issue_number,
                "repo": repo,
                "original_body_sha256": original_body_sha256,
                "captured_at": captured_at,
                "source_lane": source_lane,
            }
        )
    )


# ---------------------------------------------------------------------------
# Issue #2039: repair_action.apply controlled consumer -- foundational
# building blocks. (REPAIR_APPLY_BLOCK_MARKER_ISSUE_2039)
#
# NOTE: the FD-based secure artifact reader (AC7), the exactly-one
# mutation-intent arbiter core (AC1), and the schema migration (AC2,
# repair_apply_result_v1.schema.json / refinement_preflight_result_v1.schema
# .json) are implemented. AC8/AC11 are wired: run_repair_action_apply()
# below is registered as the `repair_action.apply` command_id in
# command_registry.py / skill_runtime_command_policy.py, dispatched by
# skill_runtime_exec.py, and constrained to stdout_contract
# `repair_apply_result/v1`. AC3 (provenance binding), AC4 (pre-dispatch
# rebase-on-drift), AC5 (post-dispatch retry-budget separation +
# authoritative readback), AC6 (lossless receipt projection, phase/
# failure_code separation), AC9 (lane-preserving fresh validation), and AC10
# (historical-artifact immutability + replay-resolves-to-no_change) are also
# implemented -- see run_repair_action_apply()'s own docstring.
# ---------------------------------------------------------------------------


class RepairApplySecureOpenError(RuntimeError):
    """Raised when a repair_apply artifact fails FD-based secure-open
    validation (Issue #2039 AC7)."""


REPAIR_APPLY_MAX_ARTIFACT_BYTES = 1_048_576  # 1 MiB read-size ceiling (AC7)


def _repair_apply_reject_unsafe_ancestors(path: Path, root: Path) -> None:
    """Fail-closed: reject if any ancestor directory between `path`'s parent
    and `root` (inclusive) is itself a symlink, or if the resolved parent
    directory is not contained within `root` (AC7 parent-symlink / parent
    substitution rejection). Uses os.lstat (does NOT follow symlinks)."""
    resolved_root = root.resolve(strict=False)
    node = path.parent
    seen = 0
    while True:
        seen += 1
        if seen > 256:
            raise RepairApplySecureOpenError(f"repair_apply_ancestor_chain_too_deep:{path}")
        try:
            st = os.lstat(node)
        except FileNotFoundError as exc:
            raise RepairApplySecureOpenError(f"repair_apply_ancestor_missing:{node}") from exc
        except OSError as exc:
            raise RepairApplySecureOpenError(f"repair_apply_ancestor_lstat_error:{node}:{exc}") from exc
        if stat.S_ISLNK(st.st_mode):
            raise RepairApplySecureOpenError(f"repair_apply_ancestor_is_symlink:{node}")
        if node.resolve(strict=False) == resolved_root:
            break
        parent = node.parent
        if parent == node:
            raise RepairApplySecureOpenError(
                f"repair_apply_ancestor_root_not_found:{path}:not_under:{resolved_root}"
            )
        node = parent

    resolved_parent = path.parent.resolve(strict=False)
    if resolved_parent != resolved_root and resolved_root not in resolved_parent.parents:
        raise RepairApplySecureOpenError(
            f"repair_apply_parent_outside_root:{resolved_parent}:not_under:{resolved_root}"
        )


def secure_read_repair_apply_artifact(
    path: Path,
    *,
    root: Path,
    max_bytes: int = REPAIR_APPLY_MAX_ARTIFACT_BYTES,
    expected_sha256: Optional[str] = None,
) -> "tuple[str, str]":
    """FD-based secure read of a repair_apply artifact (Issue #2039 AC7).

    Rejects (fail-closed, before any content bytes are trusted):
      - leaf symlink, FIFO, socket, device, directory (regular files only)
      - parent-directory symlink anywhere between the leaf and `root`
      - parent substitution (resolved parent outside `root`)
      - post-open identity mismatch (open()+fstat() vs the initial lstat())
      - oversize content (> max_bytes)
      - non-UTF-8 content (strict decode)
      - digest mismatch against `expected_sha256`, when given
      - leaf replaced with a symlink between the read and the post-read
        identity re-check

    Uses O_NOFOLLOW on open() so a leaf symlink introduced between the
    lstat check and the open() call is rejected by the kernel (not just
    detected after the fact), and fstat()-vs-lstat() identity comparison
    (st_dev / st_ino) closes the residual TOCTOU window between the lstat
    and the open().

    Returns (text, sha256_hex_digest_of_raw_bytes).
    """
    resolved_root = root.resolve(strict=False)
    _repair_apply_reject_unsafe_ancestors(path, resolved_root)

    try:
        pre_st = os.lstat(path)
    except FileNotFoundError as exc:
        raise RepairApplySecureOpenError(f"repair_apply_leaf_not_found:{path}") from exc
    except OSError as exc:
        raise RepairApplySecureOpenError(f"repair_apply_leaf_lstat_error:{path}:{exc}") from exc

    if stat.S_ISLNK(pre_st.st_mode):
        raise RepairApplySecureOpenError(f"repair_apply_leaf_is_symlink:{path}")
    if not stat.S_ISREG(pre_st.st_mode):
        kind = "directory" if stat.S_ISDIR(pre_st.st_mode) else "special_file"
        raise RepairApplySecureOpenError(f"repair_apply_leaf_not_regular_file:{path}:{kind}")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise RepairApplySecureOpenError(f"repair_apply_leaf_open_error:{path}:{exc}") from exc

    raw = b""
    try:
        post_st = os.fstat(fd)
        if not stat.S_ISREG(post_st.st_mode):
            raise RepairApplySecureOpenError(f"repair_apply_leaf_not_regular_file_post_open:{path}")
        if (post_st.st_dev, post_st.st_ino) != (pre_st.st_dev, pre_st.st_ino):
            raise RepairApplySecureOpenError(f"repair_apply_leaf_identity_mismatch:{path}")
        if post_st.st_size > max_bytes:
            raise RepairApplySecureOpenError(
                f"repair_apply_leaf_oversize:{path}:{post_st.st_size}:>:{max_bytes}"
            )
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = -1  # fdopen now owns the fd; do not close twice below
            raw = handle.read(max_bytes + 1)
    except RepairApplySecureOpenError:
        raise
    except OSError as exc:
        raise RepairApplySecureOpenError(f"repair_apply_leaf_read_error:{path}:{exc}") from exc
    finally:
        if fd != -1:
            with contextlib.suppress(OSError):
                os.close(fd)

    if len(raw) > max_bytes:
        raise RepairApplySecureOpenError(f"repair_apply_leaf_oversize_on_read:{path}:>:{max_bytes}")

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RepairApplySecureOpenError(f"repair_apply_leaf_not_utf8:{path}:{exc}") from exc

    digest = hashlib.sha256(raw).hexdigest()
    # PR #2202 review fix (P0-2 collateral bug, found by the required
    # canonical-producer E2E test): repair_action.*_body_sha256 fields are
    # always "sha256:<hex>"-prefixed (see repair_issue_contract.py /
    # classify_repair_action()), but every caller here always passed a
    # prefixed expected_sha256 while this compared it against a bare hex
    # digest -- so a REAL producer artifact ALWAYS failed this check
    # (silently masked before now because every hand-built test fixture
    # happened to pass a bare, un-prefixed expected_sha256 instead of a
    # real repair_action's own value).
    if expected_sha256 is not None and digest != _repair_apply_strip_sha_prefix(expected_sha256):
        raise RepairApplySecureOpenError(
            f"repair_apply_digest_mismatch:{path}:expected={expected_sha256}:actual={digest}"
        )

    try:
        after_st = os.lstat(path)
    except OSError as exc:
        raise RepairApplySecureOpenError(f"repair_apply_leaf_post_read_lstat_error:{path}:{exc}") from exc
    if stat.S_ISLNK(after_st.st_mode):
        raise RepairApplySecureOpenError(f"repair_apply_leaf_replaced_with_symlink:{path}")
    if (after_st.st_dev, after_st.st_ino) != (pre_st.st_dev, pre_st.st_ino):
        raise RepairApplySecureOpenError(f"repair_apply_leaf_replaced_post_read:{path}")

    return text, digest


REPAIR_APPLY_FAILURE_MULTIPLE_MUTATION_INTENTS = "multiple_mutation_intents"
REPAIR_APPLY_FAILURE_NO_MUTATION_INTENT = "no_mutation_intent"


def resolve_repair_apply_mutation_intent(
    *,
    contract_update: "dict | None",
    repair_action: "dict | None",
) -> dict:
    """Issue #2039 AC1: exactly-one mutation-intent arbiter for the
    repair_action.apply command lane.

    PR #2202 review fix (P0-1): this arbiter reads the CANONICAL
    `refinement_preflight_result_v1` top-level field `contract_update`
    (emitted by `contract_update.run.with_anchor`) -- not the unrelated,
    schema-invalid `contract_patch_plan` key that a canonical result can
    never carry (`additionalProperties: false`). Reading the wrong field
    made the earlier version of this arbiter always see
    `has_contract_update=False` for real production artifacts, so it could
    never detect the real hazard this AC exists to prevent: a canonical
    result that already carries a completed/attempted `contract_update`
    handoff *and* a `repair_action` projection at the same time.

    `contract_update` and `repair_action` must not both be present in the
    same preflight result. When both are given this returns a fail-closed
    verdict (`failure_code=multiple_mutation_intents`,
    `mutation_outcome=not_attempted`) *before* any GitHub mutation is
    attempted. A subsequent intent must be regenerated as a fresh preflight
    in a separate command, never resolved by this arbiter picking one of
    the two present intents.
    """
    has_contract_update = contract_update is not None
    has_repair_action = repair_action is not None

    if has_contract_update and has_repair_action:
        return {
            "intent": None,
            "ok": False,
            "failure_code": REPAIR_APPLY_FAILURE_MULTIPLE_MUTATION_INTENTS,
            "mutation_outcome": "not_attempted",
            "reason": (
                "contract_update and repair_action are both present in the "
                "same preflight result; exactly one mutation intent is required. "
                "Apply one intent, then regenerate a fresh preflight as a "
                "separate command for the other."
            ),
        }
    if has_repair_action:
        return {
            "intent": "repair_action",
            "ok": True,
            "failure_code": None,
            "mutation_outcome": None,
            "reason": None,
        }
    if has_contract_update:
        return {
            "intent": "contract_update",
            "ok": True,
            "failure_code": None,
            "mutation_outcome": None,
            "reason": None,
        }
    return {
        "intent": None,
        "ok": False,
        "failure_code": REPAIR_APPLY_FAILURE_NO_MUTATION_INTENT,
        "mutation_outcome": "not_attempted",
        "reason": "Neither contract_update nor repair_action is present.",
    }


REPAIR_ACTION_APPLY_STDOUT_SCHEMA_VERSION = "repair_apply_result/v1"
EDIT_ISSUE_TXN_SCRIPT_REL = "edit-issue/scripts/edit_issue_txn.py"

# Issue #2039 AC3: provenance-mismatch failure codes, in the order they are
# checked (repo/issue identity first, then run identity, then payload
# replacement, then leaf-artifact digest).
REPAIR_APPLY_FAILURE_CROSS_ISSUE = "cross_issue_provenance_mismatch"
REPAIR_APPLY_FAILURE_STALE_RUN = "stale_run_provenance_mismatch"
REPAIR_APPLY_FAILURE_REPLACEMENT = "replacement_provenance_mismatch"
REPAIR_APPLY_FAILURE_DIGEST_MISMATCH = "digest_mismatch"
REPAIR_APPLY_FAILURE_PROVENANCE_UNRECONSTRUCTABLE = "provenance_unreconstructable"
REPAIR_APPLY_FAILURE_SECOND_DRIFT = "second_body_drift"
REPAIR_APPLY_FAILURE_NON_SAFE_AFTER_RERUN = "non_safe_disposition_after_rerun"


def _repair_apply_not_attempted_result(
    *,
    repo: str,
    issue_number: int,
    phase: str,
    failure_code: "str | None",
    provenance: "dict | None" = None,
    rebase: "dict | None" = None,
) -> dict:
    """Issue #2039 AC8/AC11: shared not_attempted stdout-contract shape for
    every early-exit branch of run_repair_action_apply() (multiple intent,
    no intent, non-safe disposition, unreadable candidate, provenance
    mismatch, second body drift, readback failure). Never emits a GitHub
    mutation."""
    return {
        "schema_version": REPAIR_ACTION_APPLY_STDOUT_SCHEMA_VERSION,
        "phase": phase,
        "mutation_outcome": "not_attempted",
        "failure_code": failure_code,
        "repo": repo,
        "issue_number": issue_number,
        "provenance": provenance
        or {
            "repo": repo,
            "issue_number": issue_number,
            "original_body_sha256": "",
            "original_updated_at": None,
            "preflight_run_identity": None,
            "producer_schema_version": "repair_action/v1",
            "producer_policy_version": "deterministic-issue-repair/v1",
            "repair_action_core_sha256": "",
            "candidate_digest": "",
            "source_lane": "unanchored",
            "source_refs_digest": None,
        },
        "rebase": rebase
        or {
            "attempted": False,
            "producer_reruns": 0,
            "drift_detected": False,
            "second_drift": False,
        },
        "retry": {"post_dispatch_retry_budget": 0, "retries_used": 0},
        "receipt": {
            "patch_attempted": False,
            "executor_status": None,
            "mutation_outcome": "not_attempted",
            "failure_code": failure_code,
            "final_readback": {"status": "not_applicable", "digest": None, "digest_class": "not_applicable"},
        },
        "fresh_validation": {
            "status": "not_run",
            "source_lane_preserved": True,
            "actionable_repair_remaining": None,
            "final_body_digest_match": None,
        },
        "historical_artifacts": {"physically_deleted": False, "latest_action_reference_invalidated": False},
    }


def _repair_apply_strip_sha_prefix(value: "str | None") -> "str | None":
    """Normalize a `sha256:<hex>` or bare-`<hex>` digest string to its bare
    lowercase hex form for equality comparison, so digests produced by
    different callers (some prefixed, some not) can still be compared
    correctly (Issue #2039 AC3/AC4/AC5)."""
    if not value:
        return None
    if value.startswith("sha256:"):
        value = value[len("sha256:") :]
    return value.lower()


def _repair_action_core_projection(repair_action: dict) -> dict:
    """Issue #2039 AC3: the environment-independent core of a repair_action
    payload that provenance binding is computed over."""
    return {
        "schema_version": repair_action.get("schema_version"),
        "policy_version": repair_action.get("policy_version"),
        "disposition": repair_action.get("disposition"),
        "original_body_sha256": repair_action.get("original_body_sha256"),
        "repaired_body_sha256": repair_action.get("repaired_body_sha256"),
        "repair_kinds": repair_action.get("repair_kinds"),
        "reason_codes": repair_action.get("reason_codes"),
    }


def _repair_action_core_sha256(repair_action: dict) -> str:
    core = _repair_action_core_projection(repair_action)
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _validate_repair_apply_provenance_binding(
    provenance: dict,
    *,
    repo: str,
    issue_number: int,
    expected_provenance: "dict | None",
) -> "str | None":
    """Issue #2039 AC3: reject cross-Issue, old-run, replacement, and
    candidate-digest-mismatch cases *before* any GitHub mutation is
    dispatched.

    The always-available self-consistency check (the candidate's own
    provenance must target the repo/issue this command was invoked for) is
    enforced unconditionally. When a caller additionally supplies
    `expected_provenance` (e.g. the run identity / core hash / candidate
    digest it originally bound to), every supplied field must match exactly,
    or this returns the corresponding fail-closed reason code. Returns None
    when provenance is fully consistent.
    """
    if provenance.get("repo") != repo or provenance.get("issue_number") != issue_number:
        return REPAIR_APPLY_FAILURE_CROSS_ISSUE
    if not expected_provenance:
        return None

    exp_repo = expected_provenance.get("repo")
    if exp_repo is not None and exp_repo != provenance.get("repo"):
        return REPAIR_APPLY_FAILURE_CROSS_ISSUE
    exp_issue = expected_provenance.get("issue_number")
    if exp_issue is not None and exp_issue != provenance.get("issue_number"):
        return REPAIR_APPLY_FAILURE_CROSS_ISSUE

    exp_run_identity = expected_provenance.get("preflight_run_identity")
    if exp_run_identity is not None and exp_run_identity != provenance.get("preflight_run_identity"):
        return REPAIR_APPLY_FAILURE_STALE_RUN
    exp_original_sha = expected_provenance.get("original_body_sha256")
    if exp_original_sha is not None and _repair_apply_strip_sha_prefix(
        exp_original_sha
    ) != _repair_apply_strip_sha_prefix(provenance.get("original_body_sha256")):
        return REPAIR_APPLY_FAILURE_STALE_RUN

    exp_core = expected_provenance.get("repair_action_core_sha256")
    if exp_core is not None and exp_core != provenance.get("repair_action_core_sha256"):
        return REPAIR_APPLY_FAILURE_REPLACEMENT

    exp_digest = expected_provenance.get("candidate_digest")
    if exp_digest is not None and _repair_apply_strip_sha_prefix(exp_digest) != _repair_apply_strip_sha_prefix(
        provenance.get("candidate_digest")
    ):
        return REPAIR_APPLY_FAILURE_DIGEST_MISMATCH

    return None


def _default_rerun_repair_producer(body: str, artifact_dir: Path) -> "tuple[dict | None, str | None]":
    """Issue #2039 AC4 default pre-dispatch rebase: rerun the SAME producer
    (repair_issue_contract.py dry-run, then --apply materialize) against a
    fresh live body -- never a whole-body textual rebase/diff-apply. Returns
    a repair_action-shaped dict (possibly non-`auto_apply_safe`, which the
    caller must classify as `non_safe_disposition_after_rerun`), or
    (None, reason) on any subprocess/materialization failure."""
    dry = _invoke_repair(body)
    if dry.get("error"):
        return None, f"rerun_dry_run_error:{dry.get('error')}"
    raw_action = dry.get("repair_action")
    if dry.get("changed") is not True or not isinstance(raw_action, dict):
        # No repair classified against the fresh body at all: there is
        # nothing safe left to apply, and the caller must not silently
        # fabricate a disposition here.
        return None, "rerun_no_actionable_repair"

    if raw_action.get("disposition") != "auto_apply_safe":
        return {
            "schema_version": raw_action.get("schema_version", "repair_action/v1"),
            "policy_version": raw_action.get("policy_version", "deterministic-issue-repair/v1"),
            "disposition": raw_action.get("disposition"),
            "original_body_sha256": raw_action.get("original_body_sha256"),
            "repaired_body_sha256": raw_action.get("repaired_body_sha256"),
            "candidate_body_artifact": None,
            "repair_kinds": raw_action.get("repair_kinds", []),
            "reason_codes": raw_action.get("reason_codes", []),
        }, None

    apply_result, apply_error = _materialize_auto_apply_candidate(body, dry, artifact_dir)
    if apply_error is not None:
        return None, f"rerun_apply_error:{apply_error}"
    candidate_path = artifact_dir / "repaired_issue_body.md"
    return {
        "schema_version": raw_action.get("schema_version", "repair_action/v1"),
        "policy_version": raw_action.get("policy_version", "deterministic-issue-repair/v1"),
        "disposition": "auto_apply_safe",
        "original_body_sha256": raw_action.get("original_body_sha256"),
        "repaired_body_sha256": raw_action.get("repaired_body_sha256"),
        "candidate_body_artifact": str(candidate_path),
        "repair_kinds": raw_action.get("repair_kinds", []),
        "reason_codes": raw_action.get("reason_codes", []),
    }, None


def _classify_repair_apply_readback_digest(
    remote_sha: "str | None", candidate_digest: "str | None", old_digest: "str | None"
) -> str:
    """Issue #2039 AC5: authoritative-readback digest classification.
    Distinguishes candidate (the dispatched body took effect) / old (the
    body is unchanged from before dispatch) / third (neither -- e.g. a
    concurrent human edit) / unknown (no readback digest available)."""
    norm_remote = _repair_apply_strip_sha_prefix(remote_sha)
    if norm_remote is None:
        return "unknown"
    if norm_remote == _repair_apply_strip_sha_prefix(candidate_digest):
        return "candidate"
    if old_digest is not None and norm_remote == _repair_apply_strip_sha_prefix(old_digest):
        return "old"
    return "third"


def _repair_receipt_from_txn_result(
    txn_result: dict,
    *,
    candidate_digest: "str | None" = None,
    old_digest: "str | None" = None,
    resolve_readback=None,
) -> dict:
    """Issue #2039 AC6: lossless projection of edit_issue_txn.py's
    ISSUE_EDIT_TXN_RESULT_V1 receipt into the repair_apply_result/v1
    receipt shape. `unknown` is preserved, never collapsed into
    failed/not_attempted/no_change.

    Issue #2039 AC5: post-dispatch retry budget is 0 -- this NEVER re-issues
    the mutation. When the transaction result carries no
    `remote_current_body_sha256` (executor could not itself confirm the
    outcome), `resolve_readback` (if given) is called at most once to fetch
    an authoritative post-dispatch snapshot for digest classification only;
    it never re-dispatches a mutation.
    """
    # Issue #2039 P0-4 fix: ISSUE_EDIT_TXN_RESULT_V1's canonical shape nests
    # the attempt/outcome/digest fields the receipt needs under
    # `body_update` and `content_update` (see edit_issue_txn.py
    # `_render_result()`); only `status` and `mutation_started` live at the
    # top level. Reading e.g. `txn_result["body_attempted"]` or
    # `txn_result["remote_current_body_sha256"]` at the top level always
    # misses (those keys never exist there), which previously caused
    # `patch_attempted` to silently degrade to False and skip fresh
    # validation even when a body patch had genuinely been attempted.
    body_update = txn_result.get("body_update")
    if not isinstance(body_update, dict):
        body_update = {}
    content_update = txn_result.get("content_update")
    if not isinstance(content_update, dict):
        content_update = {}

    status = txn_result.get("status")
    mutation_outcome_map = {
        "ok": "applied",
        "no_change": "no_change",
        "failed_no_mutation": "not_attempted",
        "human_judgment": "not_attempted",
        "mutation_outcome_unknown": "unknown",
        "failed_after_mutation": "unknown",
    }
    mutation_outcome = mutation_outcome_map.get(status, "unknown")
    patch_attempted = bool(
        body_update.get("attempted")
        or content_update.get("patch_attempted")
        or txn_result.get("mutation_started")
    )

    # Cross-field consistency (Issue #2039 P0-4 item 3): when a body/content
    # patch was actually attempted, the nested
    # `content_update.mutation_outcome` is the executor's own scoped signal
    # for that patch and MUST NOT be silently overridden by a more
    # optimistic top-level `status`-derived reading. If the executor itself
    # could not confirm the patch outcome
    # (`content_update.mutation_outcome == "unknown"`), this receipt's
    # `mutation_outcome` stays `unknown` even if the overall transaction
    # `status` looked definitive -- this is the exact
    # mutation_outcome_unknown hazard the review flagged (fail closed,
    # never collapse `unknown` into a definitive outcome).
    if patch_attempted and content_update.get("mutation_outcome") == "unknown":
        mutation_outcome = "unknown"

    failure_code = None
    if mutation_outcome == "unknown":
        failure_code = "final_readback_unresolvable"
    elif mutation_outcome == "not_attempted" and (txn_result.get("errors") or []):
        failure_code = "transaction_execute_error"

    remote_sha = body_update.get("remote_current_body_sha256")
    if not remote_sha and mutation_outcome == "unknown" and resolve_readback is not None:
        # AC5: a single authoritative read (never a mutation retry),
        # performed ONLY to disambiguate a genuinely executor-unconfirmed
        # outcome -- not_attempted/no_change/applied results with no
        # reported digest are not second-guessed via an extra read.
        try:
            remote_sha = resolve_readback()
        except Exception:
            remote_sha = None

    if remote_sha:
        digest_class = _classify_repair_apply_readback_digest(remote_sha, candidate_digest, old_digest)
        final_readback = {"status": "verified", "digest": remote_sha, "digest_class": digest_class}
    else:
        final_readback = {"status": "unresolved", "digest": None, "digest_class": "not_applicable"}

    return {
        "patch_attempted": patch_attempted,
        "executor_status": status,
        "mutation_outcome": mutation_outcome,
        "failure_code": failure_code,
        "final_readback": final_readback,
    }


REPAIR_APPLY_VALID_SOURCE_LANES = {"human_context", "anchor", "unanchored"}


def _default_fresh_validate_producer(
    body: str,
    expected_source_lane: str,
    *,
    anchor_url: "str | None" = None,
    known_context: "dict | None" = None,
) -> dict:
    """Issue #2039 AC9 default fresh-validation producer.

    Reruns the SAME narrow repair producer (dry-run only -- no
    materialization, no mutation) against `body` to determine whether an
    actionable repair still remains after the mutation attempt.

    PR #2202 review fix-delta (P0-5, item 4): when the caller supplies the
    SAME `anchor_url` / `known_context` inputs the original producer run
    used to classify its source lane, this now genuinely re-derives the
    lane by rerunning the SAME classifier
    (`_determine_repair_source_lane` -> `_resolve_scope_delta_source_kind`)
    against the CURRENT (fresh) state -- it does not simply trust/echo
    `expected_source_lane`. This is what makes a real lane-promotion /
    lane-mix-up bug detectable, not merely an injected test double. When no
    anchor context is supplied (the common case for callers that do not
    thread it through -- e.g. this function has no independent way to
    discover which GitHub comment was originally the anchor without being
    told), this stays the fail-closed-safe baseline of reporting the lane
    it was told to preserve (it never promotes unanchored -> anchor, never
    converts to with_anchor, and never mixes lanes on its own).
    """
    dry = _invoke_repair(body)
    if dry.get("error"):
        return {"error": dry.get("error"), "source_lane": expected_source_lane, "actionable_repair": None}
    raw_action = dry.get("repair_action")
    actionable = bool(
        dry.get("changed") is True
        and isinstance(raw_action, dict)
        and raw_action.get("disposition") == "auto_apply_safe"
    )
    if anchor_url is not None or known_context is not None:
        reported_lane = _determine_repair_source_lane(anchor_url, known_context)
    else:
        reported_lane = expected_source_lane
    return {"error": None, "source_lane": reported_lane, "actionable_repair": actionable}


def _repair_apply_expected_post_mutation_digest(
    *,
    mutation_outcome: str,
    receipt: dict,
    candidate_digest: "str | None",
    old_digest: "str | None",
) -> "str | None":
    """Issue #2039 AC9: the digest fresh validation must confirm the live
    body against, derived from the SAME authoritative signals AC5/AC6
    already computed -- never re-guessed independently.

    `applied` expects the candidate digest; `no_change` expects the
    pre-dispatch (old) digest; an `unknown` outcome defers entirely to the
    AC5 authoritative-readback digest classification (candidate/old), and
    is unresolvable (None) when that classification itself is `third` or
    `unknown` -- fresh validation must not fabricate an expectation the
    readback itself could not establish.
    """
    if mutation_outcome == "applied":
        return candidate_digest
    if mutation_outcome == "no_change":
        return old_digest
    digest_class = (receipt.get("final_readback") or {}).get("digest_class")
    if digest_class == "candidate":
        return candidate_digest
    if digest_class == "old":
        return old_digest
    return None


def _run_repair_apply_fresh_validation(
    *,
    fetch,
    producer,
    expected_source_lane: str,
    expected_digest: "str | None",
) -> dict:
    """Issue #2039 AC9: post-mutation fresh validation.

    Re-reads the live Issue body (a read, never a mutation retry -- this is
    independent of, and does not consume, the AC5 post-dispatch retry
    budget) and reruns `producer` against it to determine whether the
    original human-context/anchor/unanchored provenance lane was preserved
    and whether any actionable repair remains. Succeeds ONLY when the fresh
    result carries no actionable repair AND the live body digest matches
    `expected_digest`; an unresolvable expected digest (e.g. an `unknown`
    outcome whose readback digest_class was itself `third`/`unknown`) is
    always a failure, never a silent skip.
    """
    if expected_digest is None:
        return {
            "status": "failed",
            "source_lane_preserved": True,
            "actionable_repair_remaining": None,
            "final_body_digest_match": None,
        }

    try:
        fresh_issue = fetch()
    except Exception:
        return {
            "status": "failed",
            "source_lane_preserved": True,
            "actionable_repair_remaining": None,
            "final_body_digest_match": None,
        }

    fresh_body = fresh_issue.get("body", "") or ""
    fresh_digest = _sha256(fresh_body)
    digest_match = _repair_apply_strip_sha_prefix(fresh_digest) == _repair_apply_strip_sha_prefix(expected_digest)

    produced = producer(fresh_body)
    if produced.get("error") is not None:
        return {
            "status": "failed",
            "source_lane_preserved": True,
            "actionable_repair_remaining": None,
            "final_body_digest_match": digest_match,
        }

    reported_lane = produced.get("source_lane")
    lane_preserved = reported_lane in REPAIR_APPLY_VALID_SOURCE_LANES and reported_lane == expected_source_lane
    actionable_remaining = produced.get("actionable_repair")

    status = "success" if (lane_preserved and actionable_remaining is False and digest_match) else "failed"
    return {
        "status": status,
        "source_lane_preserved": lane_preserved,
        "actionable_repair_remaining": actionable_remaining,
        "final_body_digest_match": digest_match,
    }


def run_repair_action_apply(
    *,
    repo: str,
    issue_number: int,
    preflight_result_path: str,
    repo_root: "Path | None" = None,
    fetch_current=None,
    apply_transaction=None,
    expected_provenance: "dict | None" = None,
    rerun_producer=None,
    fresh_validate=None,
    fresh_anchor_url: "str | None" = None,
    fresh_known_context: "dict | None" = None,
) -> dict:
    """Issue #2039 AC8/AC11: `repair_action.apply` controlled consumer.

    Reads a previously-produced preflight result artifact via the FD-based
    secure reader (AC7), resolves the exactly-one mutation intent (AC1),
    and -- only when the intent is `repair_action` with
    `disposition == auto_apply_safe` -- dispatches the repaired body
    through the existing `edit_issue_txn.py` controlled transaction script
    (a real subprocess call; never a raw `gh issue edit` call -- AC11).

    AC3 (provenance binding): the candidate's own repo/issue_number is
    always cross-checked against the requested target. When a caller
    supplies `expected_provenance` (repo, issue_number,
    preflight_run_identity, original_body_sha256, repair_action_core_sha256,
    candidate_digest), every supplied field is required to match exactly, or
    dispatch is rejected before any GitHub mutation
    (cross_issue_provenance_mismatch / stale_run_provenance_mismatch /
    replacement_provenance_mismatch / digest_mismatch).

    AC4 (body-drift handling): before dispatch, the live Issue body is
    compared against the candidate's own recorded `original_body_sha256`.
    On drift, the producer (repair_issue_contract.py) is rerun at most once
    against the fresh live body (never a whole-body textual rebase/diff
    apply) via `rerun_producer` (injectable; defaults to
    `_default_rerun_repair_producer`). A second drift, a non-safe
    disposition after rerun, or an unreconstructable rerun result fails
    closed with no mutation.

    AC5 (retry-budget separation): `retry.post_dispatch_retry_budget` is
    always 0 -- this function NEVER re-dispatches `edit_issue_txn.py` after
    a first attempt. An executor-unconfirmed (`unknown`) outcome is instead
    resolved, at most once, via an authoritative readback (a read, not a
    mutation) that classifies the live digest as candidate/old/third.

    AC6 (lossless receipt projection): `edit_issue_txn.py`'s receipt fields
    are projected losslessly via `_repair_receipt_from_txn_result()`;
    `unknown` is never collapsed into failed/not_attempted/no_change, and
    `phase`/`mutation_outcome`/`failure_code` are kept as separate fields
    (the repair lane never emits `contract_patch_plan_missing`).

    AC9 (lane-preserving fresh validation): after any attempt that reaches
    transaction dispatch (`receipt.patch_attempted` is True -- applied,
    no_change, or unknown outcomes; never a not_attempted outcome, since
    nothing was ever attempted there), a single read-only fresh validation
    reruns `fresh_validate` (injectable; defaults to
    `_default_fresh_validate_producer`) against the now-current live body
    and re-reads the live Issue once more. It succeeds ONLY when the fresh
    result reports no actionable repair remaining, the original source
    lane (human_context/anchor/unanchored) is preserved (never an
    unconditional `with_anchor` conversion, source-lane promotion, or lane
    mix-up), and the live body digest matches the digest AC5's own
    authoritative-readback classification already established. This never
    re-dispatches a mutation and is independent of the AC5 post-dispatch
    retry budget. `fresh_anchor_url` / `fresh_known_context` (optional) are
    forwarded to the default fresh-validation producer so it can genuinely
    re-derive the source lane from current state instead of echoing back
    the lane it was told to preserve (PR #2202 review fix-delta, P0-5).

    PR #2202 review fix-delta (P0-5): a fresh validation `status ==
    "failed"` that occurs after a mutation that otherwise reached
    `phase == "complete"` (an `applied`/`no_change` outcome) now overrides
    `phase` to `"fresh_validation"` and sets a non-null `failure_code`
    (`source_lane_mismatch` when the lane was not preserved, else
    `fresh_validation_failed`) -- `mutation_outcome` itself is never
    altered (it is a fact about GitHub state, independent of this
    consistency check), but a genuine fresh-validation failure can no
    longer be silently reported as `phase=complete` /
    `failure_code=null`.

    AC10 (historical artifact immutability): this function never deletes
    the preflight-result artifact it reads, the producer's own recorded
    `candidate_body_artifact`, or any rebase-rerun artifact -- only its own
    transaction-local scratch re-serialization (a fresh copy written for
    `edit_issue_txn.py`'s own consumption) is ever removed, in a `finally`
    block, after dispatch. A replay against a live body that already
    matches this candidate's own recorded `repaired_body_sha256` (i.e. the
    change was already applied by a prior invocation) is detected before
    the AC4 rebase budget is spent and resolves deterministically to
    `mutation_outcome=no_change` with no GitHub mutation dispatched.
    """
    root = repo_root or _find_repo_root()
    _pf_path = Path(preflight_result_path)
    if not _pf_path.is_absolute():
        _pf_path = root / _pf_path
    try:
        result_text, _result_digest = secure_read_repair_apply_artifact(_pf_path, root=root)
    except RepairApplySecureOpenError:
        return _repair_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code="secure_open_rejected"
        )

    try:
        parsed = json.loads(result_text)
    except json.JSONDecodeError:
        return _repair_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code="secure_open_rejected"
        )
    if not isinstance(parsed, dict):
        return _repair_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code="secure_open_rejected"
        )

    contract_update = parsed.get("contract_update")
    repair_action = parsed.get("repair_action")
    intent = resolve_repair_apply_mutation_intent(
        contract_update=contract_update, repair_action=repair_action
    )
    if not intent["ok"]:
        return _repair_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code=intent["failure_code"]
        )

    if not isinstance(repair_action, dict) or repair_action.get("disposition") != "auto_apply_safe":
        return _repair_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="provenance_validation", failure_code="invalid_disposition"
        )

    candidate_body_artifact = repair_action.get("candidate_body_artifact")
    if not candidate_body_artifact:
        return _repair_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code="secure_open_rejected"
        )

    raw_source_lane = repair_action.get("source_lane")
    source_lane = raw_source_lane if raw_source_lane in {"human_context", "anchor", "unanchored"} else "unanchored"

    repair_action_core_sha256 = _repair_action_core_sha256(repair_action)

    _candidate_path = Path(candidate_body_artifact)
    if not _candidate_path.is_absolute():
        _candidate_path = root / _candidate_path
    try:
        candidate_body_text, candidate_digest = secure_read_repair_apply_artifact(
            _candidate_path,
            root=root,
            expected_sha256=repair_action.get("repaired_body_sha256"),
        )
    except RepairApplySecureOpenError:
        return _repair_apply_not_attempted_result(
            repo=repo, issue_number=issue_number, phase="candidate_load", failure_code="secure_open_rejected"
        )

    # PR #2202 review fix (P0-2): these are nested under `repair_action.*`
    # (and `hashes.result_core_sha256`) in the canonical
    # refinement_preflight_result_v1 schema, never top-level `parsed.*`.
    # Reading the wrong location made every real production artifact report
    # null/None here (run identity + updatedAt), silently disabling the
    # provenance binding this AC exists to enforce.
    provenance = {
        "repo": repo,
        "issue_number": issue_number,
        "original_body_sha256": repair_action.get("original_body_sha256") or "",
        "original_updated_at": repair_action.get("original_updated_at"),
        "preflight_run_identity": (
            repair_action.get("preflight_run_identity")
            or (parsed.get("hashes") if isinstance(parsed.get("hashes"), dict) else {}).get("result_core_sha256")
        ),
        "producer_schema_version": repair_action.get("schema_version") or "repair_action/v1",
        "producer_policy_version": repair_action.get("policy_version") or "deterministic-issue-repair/v1",
        "repair_action_core_sha256": repair_action_core_sha256,
        "candidate_digest": candidate_digest,
        "source_lane": source_lane,
        "source_refs_digest": repair_action.get("source_refs_digest"),
    }
    if (
        provenance["producer_schema_version"] != "repair_action/v1"
        or provenance["producer_policy_version"] != "deterministic-issue-repair/v1"
        or not provenance["original_body_sha256"]
        or not provenance["repair_action_core_sha256"]
        or not provenance["candidate_digest"]
    ):
        return _repair_apply_not_attempted_result(
            repo=repo,
            issue_number=issue_number,
            phase="provenance_validation",
            failure_code=REPAIR_APPLY_FAILURE_PROVENANCE_UNRECONSTRUCTABLE,
            provenance=provenance,
        )

    # AC3: bind against repo/issue identity always, and against any
    # caller-declared expectation (run identity, core hash, candidate
    # digest) when supplied. Fail-closed BEFORE any GitHub mutation.
    _provenance_failure = _validate_repair_apply_provenance_binding(
        provenance, repo=repo, issue_number=issue_number, expected_provenance=expected_provenance
    )
    if _provenance_failure is not None:
        return _repair_apply_not_attempted_result(
            repo=repo,
            issue_number=issue_number,
            phase="provenance_validation",
            failure_code=_provenance_failure,
            provenance=provenance,
        )

    def _default_fetch_current():
        current_issue, issue_error = _fetch_issue(repo, issue_number)
        if current_issue is None:
            raise RuntimeError(f"issue_readback_failed:{issue_error}")
        return current_issue

    fetch = fetch_current or _default_fetch_current
    try:
        current_issue = fetch()
    except Exception:
        return _repair_apply_not_attempted_result(
            repo=repo,
            issue_number=issue_number,
            phase="precondition_read",
            failure_code="final_readback_unresolvable",
            provenance=provenance,
        )

    # AC4: pre-dispatch body-drift handling. Compare the live body against
    # the candidate's own recorded original_body_sha256. On drift, rerun the
    # producer AT MOST ONCE against the fresh live body (never a whole-body
    # textual rebase/diff-apply); a second drift, a non-safe disposition
    # after rerun, or an unreconstructable rerun result fails closed with no
    # mutation dispatched.
    rebase_projection = {"attempted": False, "producer_reruns": 0, "drift_detected": False, "second_drift": False}
    live_body = current_issue.get("body", "") or ""
    live_body_digest = hashlib.sha256(live_body.encode("utf-8")).hexdigest()
    live_updated_at = current_issue.get("updatedAt")
    # PR #2202 review fix (P0-3): body-SHA-only drift detection allows an
    # A(t1) -> B(t2) -> A(t3) ABA sequence through undetected (body SHA
    # matches again at t3, but the candidate was generated against the t1
    # authority epoch, not t3). When the producer recorded a non-null
    # original_updated_at (fresh, post-migration artifacts), ALSO compare it
    # against the live updatedAt; either mismatch counts as drift. Historical
    # pre-migration artifacts with a null original_updated_at fall back to
    # the pre-existing body-SHA-only comparison (they never recorded an
    # updatedAt to compare against).
    expected_updated_at = provenance.get("original_updated_at")
    updated_at_drift = expected_updated_at is not None and live_updated_at != expected_updated_at
    drift_detected = (
        _repair_apply_strip_sha_prefix(live_body_digest)
        != _repair_apply_strip_sha_prefix(repair_action.get("original_body_sha256"))
        or updated_at_drift
    )

    if drift_detected:
        rebase_projection["drift_detected"] = True

        # AC10: replay-of-an-already-applied-change short circuit. If the
        # live body already matches THIS candidate's own recorded target
        # (repaired_body_sha256), the requested change was already
        # achieved by a prior invocation (or externally) -- this is a
        # replay, not a fresh drift that needs rebasing. Resolve
        # deterministically to no_change WITHOUT spending the single
        # pre-dispatch rebase budget and WITHOUT dispatching any GitHub
        # mutation.
        if _repair_apply_strip_sha_prefix(live_body_digest) == _repair_apply_strip_sha_prefix(
            repair_action.get("repaired_body_sha256")
        ):
            return {
                "schema_version": REPAIR_ACTION_APPLY_STDOUT_SCHEMA_VERSION,
                "phase": "complete",
                "mutation_outcome": "no_change",
                "failure_code": None,
                "repo": repo,
                "issue_number": issue_number,
                "provenance": provenance,
                "rebase": rebase_projection,
                "retry": {"post_dispatch_retry_budget": 0, "retries_used": 0},
                "receipt": {
                    "patch_attempted": False,
                    "executor_status": None,
                    "mutation_outcome": "no_change",
                    "failure_code": None,
                    "final_readback": {
                        "status": "verified",
                        "digest": f"sha256:{live_body_digest}",
                        "digest_class": "candidate",
                    },
                },
                "fresh_validation": {
                    "status": "not_run",
                    "source_lane_preserved": True,
                    "actionable_repair_remaining": None,
                    "final_body_digest_match": None,
                },
                "historical_artifacts": {"physically_deleted": False, "latest_action_reference_invalidated": False},
            }

        rebase_projection["attempted"] = True
        rebase_projection["producer_reruns"] = 1

        rebase_dir = (
            root
            / ".claude"
            / "artifacts"
            / "issue-refinement-loop"
            / str(issue_number)
            / "repair-action-apply"
            / "rebase"
        )
        rebase_dir.mkdir(parents=True, exist_ok=True)
        rerun_fn = rerun_producer or (lambda body: _default_rerun_repair_producer(body, rebase_dir))
        rerun_action, rerun_error = rerun_fn(live_body)

        if rerun_error is not None or not isinstance(rerun_action, dict):
            return _repair_apply_not_attempted_result(
                repo=repo,
                issue_number=issue_number,
                phase="rebase",
                failure_code=REPAIR_APPLY_FAILURE_PROVENANCE_UNRECONSTRUCTABLE,
                provenance=provenance,
                rebase=rebase_projection,
            )
        if rerun_action.get("disposition") != "auto_apply_safe" or not rerun_action.get("candidate_body_artifact"):
            return _repair_apply_not_attempted_result(
                repo=repo,
                issue_number=issue_number,
                phase="rebase",
                failure_code=REPAIR_APPLY_FAILURE_NON_SAFE_AFTER_RERUN,
                provenance=provenance,
                rebase=rebase_projection,
            )

        # Second live read: detect whether the body drifted again between
        # the rerun's basis (live_body) and now. If it did, fail closed --
        # the single rebase budget is exhausted (post-dispatch retry stays
        # separately budgeted at 0; this is still pre-dispatch).
        try:
            second_issue = fetch()
        except Exception:
            return _repair_apply_not_attempted_result(
                repo=repo,
                issue_number=issue_number,
                phase="rebase",
                failure_code=REPAIR_APPLY_FAILURE_PROVENANCE_UNRECONSTRUCTABLE,
                provenance=provenance,
                rebase=rebase_projection,
            )
        second_body = second_issue.get("body", "") or ""
        second_body_digest = hashlib.sha256(second_body.encode("utf-8")).hexdigest()
        second_updated_at = second_issue.get("updatedAt")
        # PR #2202 review fix (P0-3): the second (post-rerun) drift check
        # must ALSO catch an updatedAt-only change (body text happens to
        # match the rerun's basis again, but updatedAt advanced between the
        # rerun's basis read and this second read) -- not just a
        # body-digest change.
        if second_body_digest != live_body_digest or second_updated_at != live_updated_at:
            rebase_projection["second_drift"] = True
            return _repair_apply_not_attempted_result(
                repo=repo,
                issue_number=issue_number,
                phase="rebase",
                failure_code=REPAIR_APPLY_FAILURE_SECOND_DRIFT,
                provenance=provenance,
                rebase=rebase_projection,
            )

        # No second drift: the rerun's candidate is safe to dispatch.
        # Re-read the rerun candidate through the FD-based secure reader
        # (AC7) and rebuild provenance from the rerun repair_action, never
        # by textually patching the original candidate.
        rerun_candidate_path = Path(rerun_action["candidate_body_artifact"])
        if not rerun_candidate_path.is_absolute():
            rerun_candidate_path = root / rerun_candidate_path
        try:
            candidate_body_text, candidate_digest = secure_read_repair_apply_artifact(
                rerun_candidate_path,
                root=root,
                expected_sha256=rerun_action.get("repaired_body_sha256"),
            )
        except RepairApplySecureOpenError:
            return _repair_apply_not_attempted_result(
                repo=repo,
                issue_number=issue_number,
                phase="rebase",
                failure_code=REPAIR_APPLY_FAILURE_PROVENANCE_UNRECONSTRUCTABLE,
                provenance=provenance,
                rebase=rebase_projection,
            )

        repair_action = rerun_action
        current_issue = second_issue
        repair_action_core_sha256 = _repair_action_core_sha256(repair_action)
        # PR #2202 review fix (P0-3): after a rerun, re-bind to the NEW body
        # SHA, NEW updatedAt, a NEW run identity, and the new repair_action
        # core/candidate digests -- while source_lane and source_refs_digest
        # stay pinned to the values already in `provenance` (same underlying
        # human-context/anchor lane; only the body/candidate were
        # regenerated against the current authority epoch).
        provenance = {
            **provenance,
            "original_body_sha256": repair_action.get("original_body_sha256") or "",
            "original_updated_at": second_updated_at,
            "preflight_run_identity": "sha256:"
            + _sha256(
                _canonical_json(
                    {
                        "issue_number": issue_number,
                        "repo": repo,
                        "original_body_sha256": repair_action.get("original_body_sha256"),
                        "original_updated_at": second_updated_at,
                        "source_lane": provenance.get("source_lane"),
                        "rebase": True,
                    }
                )
            ),
            "repair_action_core_sha256": repair_action_core_sha256,
            "candidate_digest": candidate_digest,
        }

    def _default_apply_transaction(current_issue_: dict, candidate_body: str) -> dict:
        # Issue #2039 AC8: unlike the sibling contract_update.run.with_anchor
        # lane (which uses the shared, non-Issue-scoped `tmp/` workspace),
        # repair_action.apply's transaction-local scratch files live under
        # this command_id's own declared `allowed_write_roots` entry
        # (`.claude/artifacts/issue-refinement-loop/{issue_number}/`), so
        # they are never reported as an unauthorized write by
        # skill_runtime_exec.py's git-status-diff postcondition check (which
        # has no `tmp/`-specific exemption of its own for a bare ignored
        # directory's mere existence).
        _txn_dir = root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number) / "repair-action-apply"
        candidate_path = _txn_dir / f"issue_{issue_number}_repair_action_candidate.md"
        input_path = _txn_dir / f"issue_{issue_number}_repair_action_txn.json"
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(candidate_body, encoding="utf-8")

        try:
            return _default_apply_transaction_inner(current_issue_, candidate_body, candidate_path, input_path)
        finally:
            for _tmp_path in (candidate_path, input_path):
                try:
                    _tmp_path.unlink()
                except OSError:
                    pass

    def _default_apply_transaction_inner(
        current_issue_: dict, candidate_body: str, candidate_path: Path, input_path: Path
    ) -> dict:
        from scope_signal_delta import build_issue_edit_txn_input

        readiness_script = (
            _SCRIPTS_DIR.parent.parent / "issue-contract-review" / "scripts" / "contract_readiness_check.py"
        )
        try:
            completed_readiness = subprocess.run(
                [sys.executable, str(readiness_script), "--body-file", str(candidate_path), "--mode", "static"],
                capture_output=True,
                text=True,
                shell=False,
                timeout=REPAIR_APPLY_READINESS_SUBPROCESS_TIMEOUT_SECONDS,
            )
            readiness_stdout = completed_readiness.stdout
        except (subprocess.TimeoutExpired, OSError):
            # PR #2202 review fix-delta (P0-6, item 2): the readiness check
            # never touches GitHub -- it runs strictly BEFORE
            # edit_issue_txn.py is ever invoked below, so a
            # TimeoutExpired/OSError here proves no mutation could possibly
            # have been dispatched yet. It is therefore always safe to fall
            # through to the SAME degraded-readiness fallback already used
            # for non-JSON readiness stdout below (never a fabricated
            # "verified"/"unresolved" readiness signal, and never routed
            # into the mutation-side `unknown` handling this is not).
            readiness_stdout = ""
        try:
            readiness = json.loads(readiness_stdout)
        except json.JSONDecodeError:
            readiness = {}
        if not isinstance(readiness, dict):
            readiness = {}
        readiness.setdefault("status", "input_or_runtime_error")
        readiness.setdefault("body_sha256", f"sha256:{_sha256(candidate_body)}")
        readiness.setdefault("source_checks", [])
        readiness.setdefault("errors", [])
        readiness["readiness_result_ref"] = "transaction-local"
        readiness_forwarding = {
            key: readiness[key]
            for key in (
                "status",
                "body_sha256",
                "source_checks",
                "errors",
                "readiness_result_ref",
                "resolution_evidence",
            )
            if key in readiness
        }

        transaction_input = build_issue_edit_txn_input(
            issue_number=issue_number,
            repo=repo,
            previous_body_sha256=f"sha256:{_sha256(current_issue_.get('body', ''))}",
            previous_updated_at=current_issue_["updatedAt"],
            new_body_file=str(candidate_path.relative_to(root)),
            readiness_result=readiness_forwarding,
        )
        input_path.write_text(json.dumps(transaction_input, ensure_ascii=False), encoding="utf-8")

        def _unknown_dispatch_txn_result(error_code: str, message: str) -> dict:
            # PR #2202 review fix-delta (P0-6): a genuinely-unconfirmed
            # outcome detected HERE (never through a raw-executor receipt)
            # still followed an actual edit_issue_txn.py subprocess
            # invocation attempt, so `content_update.patch_attempted=True`
            # is set (mirroring the canonical ISSUE_EDIT_TXN_RESULT_V1
            # `mutation_outcome_unknown` shape) so AC9 fresh validation
            # still runs after this outcome, exactly as it would for a
            # genuine executor-reported unknown receipt.
            return {
                "status": "mutation_outcome_unknown",
                "content_update": {"patch_attempted": True, "mutation_outcome": "unknown"},
                "errors": [{"code": error_code, "message": message[:200]}],
            }

        # AC11: the ONLY GitHub-mutation subprocess this consumer ever
        # invokes is edit_issue_txn.py --input-file. No `gh issue edit`
        # call exists anywhere in this function.
        transaction_script = _SCRIPTS_DIR.parent.parent / EDIT_ISSUE_TXN_SCRIPT_REL
        try:
            completed = subprocess.run(
                [sys.executable, str(transaction_script), "--input-file", str(input_path.relative_to(root))],
                capture_output=True,
                text=True,
                shell=False,
                cwd=str(root),
                timeout=REPAIR_APPLY_EDIT_ISSUE_TXN_SUBPROCESS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            # PR #2202 review fix-delta (P0-6, items 2+3): this is the ONLY
            # subprocess this consumer ever invokes to dispatch a real
            # GitHub PATCH (AC11). `subprocess.run(timeout=...)` kills the
            # child only AFTER the timeout has already elapsed, so a
            # TimeoutExpired here does NOT prove the PATCH never reached
            # GitHub -- the child (or GitHub itself) may already have
            # applied it. Reporting this as "no mutation" would be an
            # unsafe mis-report of a non-retriable PATCH (review P0-6 item
            # 3), so this is always routed to the SAME
            # `mutation_outcome_unknown` status the executor itself uses to
            # signal an unconfirmed outcome -- never a degraded
            # `failed_no_mutation` shortcut. `_repair_receipt_from_txn_result`
            # maps this to `mutation_outcome=unknown` and triggers the
            # existing AC5 single authoritative-readback path.
            return _unknown_dispatch_txn_result("txn_subprocess_timeout", str(exc))
        except OSError as exc:
            # Same reasoning as the TimeoutExpired branch above: an OSError
            # raised by subprocess.run() itself (e.g. a transient OS-level
            # wait/communicate failure) after the call has already been
            # made does not prove the PATCH was never dispatched.
            return _unknown_dispatch_txn_result("txn_subprocess_oserror", str(exc))
        try:
            txn_result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            # PR #2202 review fix-delta (P0-6, item 3): stdout being empty,
            # truncated, or non-JSON AFTER the subprocess call above has
            # already completed does NOT prove no mutation happened -- the
            # child may have dispatched the PATCH and then failed to
            # flush/emit its own confirmation. Degrading this to
            # `failed_no_mutation` (the prior behavior) risked silently
            # reporting "no mutation" after a real, non-retriable PATCH.
            # This must resolve to `unknown` and route into the SAME AC5
            # authoritative-readback path used for a genuine executor-
            # reported `mutation_outcome_unknown` receipt.
            txn_result = _unknown_dispatch_txn_result("txn_stdout_not_json", completed.stderr or "")
        # Same reasoning: a parsed-but-non-dict stdout shape still followed
        # a completed subprocess call, so it must not be reported as
        # `failed_no_mutation` either.
        return txn_result if isinstance(txn_result, dict) else _unknown_dispatch_txn_result(
            "txn_stdout_not_dict", "parsed stdout JSON was not a dict"
        )

    apply_fn = apply_transaction or _default_apply_transaction
    txn_result = apply_fn(current_issue, candidate_body_text)

    def _resolve_readback() -> "str | None":
        # AC5: a single authoritative read (never a mutation retry) used
        # only when the transaction result itself carried no
        # remote_current_body_sha256.
        fresh_issue = fetch()
        return f"sha256:{_sha256(fresh_issue.get('body', '') or '')}"

    receipt = _repair_receipt_from_txn_result(
        txn_result,
        candidate_digest=candidate_digest,
        old_digest=live_body_digest,
        resolve_readback=_resolve_readback,
    )
    mutation_outcome = receipt["mutation_outcome"]
    # AC6: phase/mutation_outcome/failure_code stay separate fields; an
    # `unknown` outcome (executor could not itself confirm the result) never
    # reaches phase=complete (schema-enforced invariant).
    phase = "complete" if mutation_outcome in {"applied", "no_change", "not_attempted"} else "final_readback"
    failure_code = receipt["failure_code"]

    # AC9: fresh validation only runs after an attempt that actually
    # reached transaction dispatch (patch_attempted) -- a not_attempted
    # outcome never touched GitHub, so there is nothing post-mutation to
    # validate, and fresh_validation stays not_run.
    if receipt.get("patch_attempted"):
        expected_digest = _repair_apply_expected_post_mutation_digest(
            mutation_outcome=mutation_outcome,
            receipt=receipt,
            candidate_digest=candidate_digest,
            old_digest=live_body_digest,
        )
        fresh_validate_fn = fresh_validate or (
            lambda body: _default_fresh_validate_producer(
                body, source_lane, anchor_url=fresh_anchor_url, known_context=fresh_known_context
            )
        )
        fresh_validation = _run_repair_apply_fresh_validation(
            fetch=fetch,
            producer=fresh_validate_fn,
            expected_source_lane=source_lane,
            expected_digest=expected_digest,
        )
    else:
        fresh_validation = {
            "status": "not_run",
            "source_lane_preserved": True,
            "actionable_repair_remaining": None,
            "final_body_digest_match": None,
        }

    # PR #2202 review fix-delta (P0-5): a fresh-validation failure that
    # follows an otherwise-`complete` phase (applied/no_change) must be
    # surfaced -- it must not be silently absorbed into
    # phase=complete/failure_code=null. `mutation_outcome` itself is left
    # untouched: it is a fact about GitHub state (the mutation genuinely
    # happened or did not), separate from this post-mutation consistency
    # check.
    if fresh_validation["status"] == "failed" and phase == "complete":
        phase = "fresh_validation"
        failure_code = (
            "source_lane_mismatch" if fresh_validation["source_lane_preserved"] is False else "fresh_validation_failed"
        )

    return {
        "schema_version": REPAIR_ACTION_APPLY_STDOUT_SCHEMA_VERSION,
        "phase": phase,
        "mutation_outcome": mutation_outcome,
        "failure_code": failure_code,
        "repo": repo,
        "issue_number": issue_number,
        "provenance": provenance,
        "rebase": rebase_projection,
        "retry": {"post_dispatch_retry_budget": 0, "retries_used": 0},
        "receipt": receipt,
        "fresh_validation": fresh_validation,
        "historical_artifacts": {
            "physically_deleted": False,
            "latest_action_reference_invalidated": mutation_outcome == "applied",
        },
    }


# ---------------------------------------------------------------------------
# #2053: SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 producer / consumer.
#
# producer (this file, generate_authority_transport_manifest) -> router
# (decide_next_loop_action.py generate_router_receipt, via command_registry
# "decide.run") -> consumer (this file, consume_authority_transport).
#
# All three stages bind the same canonical payload digest
# (CANONICALIZATION_ID = "loop-protocol-json-c14n-v1", i.e. _canonical_json
# above: sorted keys, compact separators, UTF-8, no NaN/Infinity) and write
# their artifacts to an *immutable per-invocation directory* so a stale
# previous-invocation artifact can never be silently reused (AC10).
# ---------------------------------------------------------------------------

AUTHORITY_TRANSPORT_SCHEMA_VERSION = "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1"
CONSUMPTION_RECEIPT_SCHEMA_VERSION = "SCOPE_DELTA_CONSUMPTION_RECEIPT_V1"
CANONICALIZATION_ID = "loop-protocol-json-c14n-v1"

# #2053 P0 fix-delta (iteration 3, OWNER PR review): the controlled
# consumer's "mutation" step can operate in one of two lanes, always
# recorded on the consumption receipt so a reader never has to infer which
# happened from side effects alone:
#   - MUTATION_LANE_ARTIFACT_ONLY: no CONTRACT_PATCH_PLAN_V1 was supplied --
#     the consumer's bounded write is the local `consumed_authority_payload_v1
#     .json` audit artifact only (the pre-#2053-iteration-3 behavior).
#   - MUTATION_LANE_CONTRACT_PATCH_PLAN_CONSUMER: a CONTRACT_PATCH_PLAN_V1 and
#     anchor_context were supplied -- the mutation is delegated to the real,
#     existing controlled-mutation lane (consume_trusted_anchor_contract_
#     patch_plan() -> edit_issue_txn.py), and `mutation_applied` reflects that
#     lane's actual outcome, not merely the local artifact write succeeding.
MUTATION_LANE_ARTIFACT_ONLY = "artifact_only"
MUTATION_LANE_CONTRACT_PATCH_PLAN_CONSUMER = "contract_patch_plan_consumer"


def _authority_transport_dir(repo_root: Path, issue_number: int, invocation_id: str) -> Path:
    return (
        repo_root
        / ".claude"
        / "artifacts"
        / "issue-refinement-loop"
        / str(issue_number)
        / "authority-transport"
        / invocation_id
    )


def _confine_artifact_path(path: "Path | None", repo_root: Path) -> "tuple[Path | None, str | None]":
    """#2053 P1 fix-delta: resolve `path` and confine it under
    <repo_root>/.claude/artifacts/, rejecting symlinks and non-regular
    files, before it is ever opened for reading. Router receipts and
    transport manifests are attacker-influenceable strings (they arrive as
    CLI arguments / receipt fields) -- without this check a
    symlink or a path outside the artifact root could be substituted.

    Returns (resolved_path, None) on success, or (None, reason_code) on any
    violation (fail-closed, never silently proceeds with an unconfined
    path).
    """
    if path is None:
        return None, "missing_file"
    artifact_root = (repo_root / ".claude" / "artifacts").resolve()
    if path.is_symlink():
        return None, "path_confinement_symlink_rejected"
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return None, "path_confinement_resolve_failed"
    try:
        resolved.relative_to(artifact_root)
    except ValueError:
        return None, "path_confinement_outside_artifact_root"
    if resolved.exists() and not resolved.is_file():
        return None, "path_confinement_not_regular_file"
    return resolved, None


def _atomic_write_json_with_readback(path: Path, data: dict) -> tuple[bool, "dict | None", "str | None"]:
    """flush -> fsync -> exclusive-create, then read back and independently
    recompute the canonical digest to verify the bytes on disk match what
    was intended (#2053 AC10). Returns (ok, readback_data, error_reason).

    Fresh review blocker P1-C: every caller of this helper writes a
    single-invocation, exactly-once, "immutable" artifact guarded by its own
    prior `path.exists()` check (the producer's manifest, the consumer's
    consumed-payload record, and the consumer's consumption receipt). A
    plain `exists()`-check-then-`os.replace()` is a TOCTOU race: two
    concurrent writers for the SAME path can both pass the `exists()` check
    before either writes, and `os.replace()` unconditionally overwrites --
    whichever writer's `os.replace()` runs last silently wins, defeating the
    "immutable, exactly once" guarantee these callers rely on. This uses
    `os.link()` for the final publish step instead: `os.link()` atomically
    fails with `FileExistsError` if the destination already exists (a
    kernel-level exclusive-create guarantee, not a check-then-act race), so
    at most one concurrent writer for the same path can ever "win" -- the
    loser fails closed with `write_failure:already_exists` and writes
    nothing to `path`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.tmp-{os.getpid()}-{id(data)}"
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True)
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp_path, path)
        except FileExistsError:
            return False, None, "write_failure:already_exists"
    except OSError as exc:
        return False, None, f"write_failure:{type(exc).__name__}:{exc}"
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass

    try:
        readback_text = path.read_text(encoding="utf-8")
        readback_data = json.loads(readback_text)
    except (OSError, json.JSONDecodeError) as exc:
        return False, None, f"write_failure:{type(exc).__name__}:{exc}"

    if _sha256(_canonical_json(readback_data)) != _sha256(_canonical_json(data)):
        return False, readback_data, "write_failure:readback_digest_mismatch"

    return True, readback_data, None


def generate_authority_transport_manifest(
    *,
    evidence: Any,
    issue_number: int,
    repo: str,
    invocation_id: str,
    git_head_sha: str,
    repo_root: Path,
    generated_at: "str | None" = None,
) -> tuple["dict | None", "str | None"]:
    """#2053 AC1/AC7/AC10: build + immutably persist a
    SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest for `evidence` (a
    SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 dict, or a list of them).

    Returns (result, error). `result` is
    {"manifest": <dict>, "manifest_path": <str>} on success, None on
    failure (`error` explains why -- e.g. "no_evidence" or a write_failure
    reason from _atomic_write_json_with_readback).

    Writing under an invocation-scoped directory
    (authority-transport/<invocation_id>/) is what makes AC10's
    "stale previous-invocation artifact is never reused" true by
    construction: a caller must mint a fresh invocation_id to get a fresh
    manifest path; there is no shared mutable sidecar to go stale.
    """
    if evidence is None or (isinstance(evidence, list) and not evidence):
        return None, "no_evidence"

    payload = evidence
    payload_sha256 = _sha256(_canonical_json(payload))
    source = (
        evidence[0]
        if isinstance(evidence, list) and evidence
        else (evidence if isinstance(evidence, dict) else {})
    )
    manifest = {
        "schema_version": AUTHORITY_TRANSPORT_SCHEMA_VERSION,
        "invocation_id": invocation_id,
        "issue_number": issue_number,
        "repo": repo,
        "git_head_sha": git_head_sha,
        "generated_at": generated_at or _now_iso(),
        "canonicalization_id": CANONICALIZATION_ID,
        "source_comment_id": source.get("comment_id"),
        "source_comment_url": source.get("comment_url"),
        "source_issue_body_sha256": source.get("body_sha256"),
        "source_kind": source.get("source_kind") or "generated_by_agent",
        "payload": payload,
        "payload_sha256": payload_sha256,
    }
    # #2053 P1 fix-delta (iteration 3, OWNER PR review): actually enforce
    # SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 via _validate_with_schema() before
    # ever persisting it, not merely constructing the dict by hand above.
    # Fresh review blocker P1-A: schema enforcement is a real safety claim
    # for this mutation lane -- a missing/malformed schema file must fail
    # closed (schema_unavailable), never silently skip validation and
    # proceed to persist an unvalidated manifest.
    schema = _load_schema("scope_delta_authority_transport_v1.schema.json")
    if schema is None:
        return None, "schema_unavailable:scope_delta_authority_transport_v1.schema.json"
    valid, errors = _validate_with_schema(manifest, schema)
    if not valid:
        return None, f"schema_invalid:{errors[:1]}"

    manifest_dir = _authority_transport_dir(repo_root, issue_number, invocation_id)
    manifest_path = manifest_dir / "scope_delta_authority_transport_v1.json"
    # #2053 P1 fix-delta (iteration 2, OWNER PR review): true immutability,
    # not just os.replace() pathing -- re-running the producer with the SAME
    # invocation_id must refuse to overwrite the artifact it already wrote,
    # rather than silently replacing it. A caller must mint a fresh
    # invocation_id to get a fresh manifest.
    if manifest_path.exists():
        return None, "manifest_already_exists"
    ok, _readback, error = _atomic_write_json_with_readback(manifest_path, manifest)
    if not ok:
        return None, error
    return {"manifest": manifest, "manifest_path": str(manifest_path)}, None


# #2086 P0 fix_delta (iteration 3, OWNER REQUEST_CHANGES Blocker 1/Blocker 2):
# `investigation_derived_path_literals` (scope_signal_delta.py) was
# previously a schema-external, caller-supplied `list[str]` with no
# cryptographic binding to anything -- the gate only checked syntactic
# safety of each literal, never who/what/when generated the list. That is
# exactly the "human approved a high-level goal" -> "human approved
# whatever exact paths the agent derived" authority-laundering risk a prior
# #2086 OWNER review flagged. This makes the evidence a TYPED, BOUND
# artifact instead of a raw list: it MUST be minted as a
# SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest via the EXISTING #2053
# producer (`generate_authority_transport_manifest()` above / the
# `authority_transport.produce` command_id, already real-subprocess
# dispatchable through `skill_runtime_exec.py` since this Issue's
# iteration-2 fix_delta), with `payload` shaped as a single-element list
# containing one dict: `{"comment_id", "comment_url", "body_sha256",
# "source_kind": "generated_by_agent", "path_literals": [...]}`. AC9
# forbids a NEW provenance schema/ledger/sidecar family -- this reuses the
# existing schema/producer/immutable-per-invocation-directory machinery
# verbatim; only the *payload contents* (an investigation-derived path
# inventory instead of raw comment evidence) differ, which the manifest's
# existing `payload: type ["object", "array"]` / `source_kind:
# "generated_by_agent"` already accommodate without a schema edit.
def _validate_investigation_evidence_transport(
    path: "Path | str | None",
    *,
    repo_root: Path,
    issue_number: int,
    repo: str,
    anchor_url: "str | None",
    base_issue_body_sha256: str,
    git_head_sha: str,
) -> "tuple[list | None, str | None]":
    """Load + cryptographically bind a read-only-investigation exact-path
    inventory before it is ever allowed to clear the `expands_allowed_paths`
    boundary for the operator-selected human-context lane
    (`scope_signal_delta._has_investigation_derived_allowed_path_literals`).

    Binding checked (fail-closed on ANY mismatch -- consistent with the
    existing #1952 mixed-literal all-or-nothing lock, a failed binding NEVER
    partially clears the boundary, it simply makes
    `investigation_derived_path_literals` absent for this invocation):
      - `path` is confined under `.claude/artifacts/` (no traversal/symlink,
        reuses `_confine_artifact_path()`)
      - `schema_version == SCOPE_DELTA_AUTHORITY_TRANSPORT_V1`, and the
        manifest validates against the existing (unmodified)
        `scope_delta_authority_transport_v1.schema.json`
      - `issue_number` / `repo` match this invocation's own issue/repo
      - `source_comment_url` matches the SAME human-context anchor comment
        URL this preflight invocation is processing (prevents binding an
        inventory derived for one directive to a different one)
      - `source_issue_body_sha256` matches the CURRENT live issue body
        (prevents stale-body inventory reuse after the body changed)
      - `git_head_sha` matches the current live repo HEAD (prevents
        cross-checkout / stale-commit replay)
      - `payload_sha256 == sha256(canonical_json(payload))` (content digest
        binding -- the payload cannot be swapped after the manifest was
        minted)
      - `payload` is `[{"comment_url": ..., "body_sha256": ...,
        "source_kind": "generated_by_agent", "path_literals": [str, ...]}]`
        and `path_literals` is a non-empty list of strings

    The *authorization* decision itself (is this literal set genuinely
    same-goal / bounded / a safe composition for THIS directive) is made
    once, here, by this root control-plane loader -- after binding
    validates -- never implicitly granted by the mere existence of a
    `list[str]` argument (the pre-fix_delta design).

    Returns (validated_path_literals, None) on success, or
    (None, reason_code) on any failure.
    """
    if not path:
        return None, "missing_transport_path"
    resolved, confinement_reason = _confine_artifact_path(Path(path), repo_root)
    if confinement_reason is not None:
        return None, confinement_reason
    if resolved is None or not resolved.exists():
        return None, "transport_file_missing"
    try:
        manifest = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"transport_malformed_json:{type(exc).__name__}"
    if not isinstance(manifest, dict):
        return None, "transport_not_object"
    if manifest.get("schema_version") != AUTHORITY_TRANSPORT_SCHEMA_VERSION:
        return None, "transport_schema_version_mismatch"
    schema = _load_schema("scope_delta_authority_transport_v1.schema.json")
    if schema is None:
        return None, "schema_unavailable:scope_delta_authority_transport_v1.schema.json"
    valid, errors = _validate_with_schema(manifest, schema)
    if not valid:
        return None, f"transport_schema_invalid:{errors[:1]}"
    if manifest.get("issue_number") != issue_number:
        return None, "transport_issue_number_mismatch"
    manifest_repo = manifest.get("repo")
    if not isinstance(manifest_repo, str) or manifest_repo.lower() != repo.lower():
        return None, "transport_repo_mismatch"
    if not anchor_url or manifest.get("source_comment_url") != anchor_url:
        return None, "transport_anchor_url_mismatch"
    if manifest.get("source_issue_body_sha256") != base_issue_body_sha256:
        return None, "transport_issue_body_sha256_mismatch"
    if manifest.get("git_head_sha") != git_head_sha:
        return None, "transport_git_head_sha_mismatch"
    if manifest.get("source_kind") != "generated_by_agent":
        return None, "transport_source_kind_mismatch"
    payload = manifest.get("payload")
    if _sha256(_canonical_json(payload)) != manifest.get("payload_sha256"):
        return None, "transport_payload_digest_mismatch"
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        return None, "transport_payload_shape_invalid"
    entry = payload[0]
    if entry.get("comment_url") != anchor_url:
        return None, "transport_payload_comment_url_mismatch"
    if entry.get("body_sha256") != base_issue_body_sha256:
        return None, "transport_payload_body_sha256_mismatch"
    if entry.get("source_kind") != "generated_by_agent":
        return None, "transport_payload_source_kind_mismatch"
    literals = entry.get("path_literals")
    if not isinstance(literals, list) or not literals:
        return None, "transport_payload_literals_missing"
    for item in literals:
        if not isinstance(item, str):
            return None, "transport_payload_entry_not_string"
    return literals, None


def consume_authority_transport(
    *,
    router_receipt_path: str,
    issue_number: int,
    repo: str,
    invocation_id: str,
    git_head_sha: str,
    repo_root: Path,
    contract_patch_plan: "dict | None" = None,
    anchor_context: "dict | None" = None,
) -> dict:
    """#2053 AC9: controlled consumer. Reads a SCOPE_DELTA_ROUTER_RECEIPT_V1,
    independently re-verifies the transport manifest it references, applies
    at most one mutation (a deterministic, idempotency-guarded write under
    the same invocation-scoped directory), performs a readback, and a
    lightweight "fresh rerun" (re-running classify_scope_delta_authority()
    against the consumed payload to reconfirm the route is unchanged).

    Fail-closed for: missing_file, malformed_json, digest_mismatch,
    wrong_issue, wrong_git_head, wrong_invocation_id, router_receipt_not_ok,
    stale_previous_invocation (a consumption receipt already exists for this
    invocation_id -- the "exactly once" guard for AC9's "一回だけ mutation").

    #2053 P0 fix-delta (iteration 3, OWNER PR review): when `contract_patch_plan`
    (a CONTRACT_PATCH_PLAN_V1 dict) and `anchor_context` (a dict with
    `issue`, `anchor_url`, `anchor_payload`, `anchor_body`, and optionally
    `callbacks` / `known_context`) are both supplied, the mutation step is
    delegated to the existing, real controlled-mutation lane --
    `consume_trusted_anchor_contract_patch_plan()` (which, via its default
    callbacks, drives `edit_issue_txn.py`) -- instead of merely writing the
    local `consumed_authority_payload_v1.json` audit artifact. The receipt's
    `mutation_applied` claim reflects that lane's real, independently
    projected outcome (`_bounded_contract_update_handoff()`), never just the
    local artifact write succeeding. When `contract_patch_plan` /
    `anchor_context` are omitted (the default), behavior is unchanged from
    before this fix-delta: the bounded local artifact write is the mutation.
    Either way, `mutation_lane` on the receipt records which happened.
    """

    # #2053 AC10 P0 fix-delta (iteration 2): termination telemetry recording
    # generated/received/consumed artifacts by *relative path + sha256*,
    # not depending on any fixed mutable sidecar. Populated progressively as
    # each artifact is independently confirmed to exist on disk; every
    # _receipt() call (success or fail-closed) carries whatever subset was
    # confirmed by that point.
    _artifacts_seen: dict = {
        "generated_artifact": None,
        "received_artifact": None,
        "consumed_artifact": None,
    }

    def _artifact_ref(path: "Path | None") -> "dict | None":
        if path is None or not path.exists():
            return None
        try:
            relative_path = str(path.relative_to(repo_root))
        except ValueError:
            relative_path = str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        return {"relative_path": relative_path, "sha256": _sha256(text)}

    def _receipt(
        *,
        status: str,
        reason_code: "str | None",
        transport_payload_sha256: "str | None" = None,
        consumed_payload_sha256: "str | None" = None,
        mutation_applied: bool = False,
        readback_verified: bool = False,
        fresh_rerun_performed: bool = False,
        mutation_lane: str = MUTATION_LANE_ARTIFACT_ONLY,
        contract_update_status: "str | None" = None,
    ) -> dict:
        receipt = {
            "schema_version": CONSUMPTION_RECEIPT_SCHEMA_VERSION,
            "invocation_id": invocation_id,
            "issue_number": issue_number,
            "git_head_sha": git_head_sha,
            "generated_at": _now_iso(),
            "router_receipt_path": router_receipt_path,
            "transport_payload_sha256": transport_payload_sha256,
            "consumed_payload_sha256": consumed_payload_sha256,
            "mutation_applied": mutation_applied,
            "readback_verified": readback_verified,
            "fresh_rerun_performed": fresh_rerun_performed,
            "generated_artifact": _artifacts_seen["generated_artifact"],
            "received_artifact": _artifacts_seen["received_artifact"],
            "consumed_artifact": _artifacts_seen["consumed_artifact"],
            "status": status,
            "reason_code": reason_code,
            "mutation_lane": mutation_lane,
            "contract_update_status": contract_update_status,
        }
        if status == "ok":
            # #2053 P1 fix-delta (iteration 3, OWNER PR review): actually
            # enforce SCOPE_DELTA_CONSUMPTION_RECEIPT_V1 via
            # _validate_with_schema() (required/additionalProperties:false),
            # not merely a manual field-by-field construction above. An "ok"
            # receipt that fails schema validation is downgraded to a
            # fail-closed environment_failure -- schema conformance is a real
            # gate, not advisory.
            schema = _load_schema("scope_delta_consumption_receipt_v1.schema.json")
            if schema is None:
                receipt = dict(receipt)
                receipt["status"] = "environment_failure"
                receipt["reason_code"] = "schema_unavailable"
            else:
                valid, errors = _validate_with_schema(receipt, schema)
                if not valid:
                    receipt = dict(receipt)
                    receipt["status"] = "environment_failure"
                    receipt["reason_code"] = "schema_invalid"
        return receipt

    invocation_dir = _authority_transport_dir(repo_root, issue_number, invocation_id)
    consumption_receipt_path = invocation_dir / "scope_delta_consumption_receipt_v1.json"

    # AC10 / AC9 "exactly once": a consumption receipt already present for
    # this invocation_id means this invocation was already consumed -- never
    # mutate a second time from the same (possibly stale) transport.
    if consumption_receipt_path.exists():
        return _receipt(status="environment_failure", reason_code="stale_previous_invocation")

    receipt_path = Path(router_receipt_path) if router_receipt_path else None
    receipt_path, confinement_error = _confine_artifact_path(receipt_path, repo_root)
    if confinement_error is not None:
        return _receipt(status="environment_failure", reason_code=confinement_error)
    if not receipt_path.exists():
        return _receipt(status="environment_failure", reason_code="missing_file")
    _artifacts_seen["received_artifact"] = _artifact_ref(receipt_path)

    try:
        router_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _receipt(status="environment_failure", reason_code="malformed_json")

    if (
        not isinstance(router_receipt, dict)
        or router_receipt.get("schema_version") != "SCOPE_DELTA_ROUTER_RECEIPT_V1"
    ):
        return _receipt(status="environment_failure", reason_code="malformed_json")

    if router_receipt.get("status") != "ok":
        return _receipt(status="environment_failure", reason_code="router_receipt_not_ok")

    if router_receipt.get("issue_number") != issue_number:
        return _receipt(status="environment_failure", reason_code="wrong_issue")
    if router_receipt.get("git_head_sha") != git_head_sha:
        return _receipt(status="environment_failure", reason_code="wrong_git_head")
    if router_receipt.get("invocation_id") != invocation_id:
        return _receipt(status="environment_failure", reason_code="wrong_invocation_id")

    manifest_path_str = router_receipt.get("transport_manifest_path")
    manifest_path = Path(manifest_path_str) if manifest_path_str else None
    manifest_path, confinement_error = _confine_artifact_path(manifest_path, repo_root)
    if confinement_error is not None:
        return _receipt(status="environment_failure", reason_code=confinement_error)
    if not manifest_path.exists():
        return _receipt(status="environment_failure", reason_code="missing_file")
    _artifacts_seen["generated_artifact"] = _artifact_ref(manifest_path)

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _receipt(status="environment_failure", reason_code="malformed_json")

    if not isinstance(manifest, dict) or manifest.get("schema_version") != AUTHORITY_TRANSPORT_SCHEMA_VERSION:
        return _receipt(status="environment_failure", reason_code="malformed_json")

    if manifest.get("issue_number") != issue_number:
        return _receipt(status="environment_failure", reason_code="wrong_issue")
    if manifest.get("git_head_sha") != git_head_sha:
        return _receipt(status="environment_failure", reason_code="wrong_git_head")
    if manifest.get("invocation_id") != invocation_id:
        return _receipt(status="environment_failure", reason_code="wrong_invocation_id")
    # #2053 P1 fix-delta (iteration 2, OWNER PR review): PR #1332 previously
    # added expected_repo binding specifically to prevent same-issue-number/
    # cross-repo spoofing; that boundary was missing here. The consumer
    # must independently verify the manifest's own `repo` field, not just
    # the issue_number, against the caller-supplied expected repo.
    if manifest.get("repo") != repo:
        return _receipt(status="environment_failure", reason_code="wrong_repo")

    recomputed = _sha256(_canonical_json(manifest.get("payload")))
    manifest_payload_sha256 = manifest.get("payload_sha256")
    if recomputed != manifest_payload_sha256 or manifest_payload_sha256 != router_receipt.get(
        "transport_payload_sha256"
    ):
        return _receipt(
            status="environment_failure",
            reason_code="digest_mismatch",
            transport_payload_sha256=manifest_payload_sha256,
            consumed_payload_sha256=recomputed,
        )

    # --- mutation (once): write the consumed payload under the same
    # invocation-scoped directory. This bounded, idempotency-guarded local
    # write is always performed as the audit/readback record -- it is the
    # ONLY mutation performed when no contract_patch_plan is supplied
    # (MUTATION_LANE_ARTIFACT_ONLY). When a contract_patch_plan IS supplied
    # (below), it remains the readback record for the transported payload,
    # but the load-bearing mutation is delegated to the real controlled
    # consumer lane instead.
    consumed_path = invocation_dir / "consumed_authority_payload_v1.json"
    # #2053 P1 fix-delta (iteration 2, OWNER PR review): bind the
    # transaction/idempotency state BEFORE mutation, not just via
    # post-mutation receipt presence -- a leftover consumed-payload artifact
    # from a crashed prior run of this SAME invocation_id (receipt never
    # published) must never be silently overwritten/re-applied.
    if consumed_path.exists():
        return _receipt(status="environment_failure", reason_code="stale_previous_invocation")
    consumed_record = {
        "schema_version": "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1_CONSUMED",
        "invocation_id": invocation_id,
        "issue_number": issue_number,
        "payload": manifest.get("payload"),
        "payload_sha256": manifest_payload_sha256,
    }
    ok, readback, error = _atomic_write_json_with_readback(consumed_path, consumed_record)
    if not ok:
        return _receipt(
            status="environment_failure",
            reason_code="write_failure",
            transport_payload_sha256=manifest_payload_sha256,
        )
    mutation_applied = True
    readback_verified = (
        isinstance(readback, dict) and readback.get("payload_sha256") == manifest_payload_sha256
    )
    _artifacts_seen["consumed_artifact"] = _artifact_ref(consumed_path)

    # --- #2053 P0 fix-delta (iteration 3): real controlled-mutation lane.
    # When the caller supplies a CONTRACT_PATCH_PLAN_V1 and the anchor
    # context it applies to, delegate the actual mutation authorization to
    # the existing, real consumer (consume_trusted_anchor_contract_patch_plan
    # -> edit_issue_txn.py) instead of treating the local artifact write
    # above as sufficient. `mutation_applied` is overwritten by that lane's
    # real, independently-projected outcome.
    mutation_lane = MUTATION_LANE_ARTIFACT_ONLY
    contract_update_status: "str | None" = None
    if isinstance(contract_patch_plan, dict) and isinstance(anchor_context, dict):
        required_context_keys = ("issue", "anchor_url", "anchor_payload", "anchor_body")
        if all(key in anchor_context for key in required_context_keys):
            mutation_lane = MUTATION_LANE_CONTRACT_PATCH_PLAN_CONSUMER
            try:
                raw_consumer_result = consume_trusted_anchor_contract_patch_plan(
                    repo=repo,
                    issue_number=issue_number,
                    issue=anchor_context["issue"],
                    anchor_url=anchor_context["anchor_url"],
                    anchor_payload=anchor_context["anchor_payload"],
                    anchor_body=anchor_context["anchor_body"],
                    contract_patch_plan=contract_patch_plan,
                    callbacks=anchor_context.get("callbacks"),
                    known_context=anchor_context.get("known_context"),
                )
            except Exception as exc:  # noqa: BLE001 - captured as a fail-closed reason
                return _receipt(
                    status="environment_failure",
                    reason_code="contract_patch_plan_consumer_error",
                    transport_payload_sha256=manifest_payload_sha256,
                    consumed_payload_sha256=recomputed,
                    mutation_applied=False,
                    readback_verified=readback_verified,
                    mutation_lane=mutation_lane,
                    contract_update_status=f"{type(exc).__name__}:{exc}",
                )
            handoff = _bounded_contract_update_handoff(raw_consumer_result)
            contract_update_status = handoff.get("status")
            mutation_applied = contract_update_status in {"applied", "no_change", "rebased"}
            if not mutation_applied:
                return _receipt(
                    status="environment_failure",
                    reason_code="contract_patch_plan_consumer_failed",
                    transport_payload_sha256=manifest_payload_sha256,
                    consumed_payload_sha256=recomputed,
                    mutation_applied=False,
                    readback_verified=readback_verified,
                    mutation_lane=mutation_lane,
                    contract_update_status=contract_update_status,
                )

    # --- fresh rerun: re-run classification against the consumed payload to
    # reconfirm the route is unchanged (no silent drift between transport
    # and consumption). #2053 P0 fix-delta (iteration 2): this is a real
    # gate, not best-effort telemetry -- an exception, or a route that has
    # drifted away from contract_update_required (e.g. human_escalation),
    # fails the consumption closed instead of silently reporting
    # fresh_rerun_performed=true with status: ok.
    fresh_rerun_performed = False
    fresh_rerun_route_action = None
    fresh_rerun_error = None
    try:
        from scope_signal_delta import (
            SCOPE_DELTA_AUTHORITY_ROUTE_CONTRACT_UPDATE_REQUIRED,
            classify_scope_delta_authority,
        )

        fresh_result = classify_scope_delta_authority(
            manifest.get("payload"),
            target_issue_number=issue_number,
            expected_repo=repo,
            base_issue_body_sha256=manifest.get("source_issue_body_sha256"),
        )
        fresh_rerun_route_action = (
            fresh_result.get("route", {}).get("action")
            if isinstance(fresh_result, dict)
            else None
        )
        fresh_rerun_performed = (
            fresh_rerun_route_action == SCOPE_DELTA_AUTHORITY_ROUTE_CONTRACT_UPDATE_REQUIRED
        )
    except Exception as exc:  # noqa: BLE001 - captured as a fail-closed reason, not swallowed
        fresh_rerun_performed = False
        fresh_rerun_error = f"{type(exc).__name__}:{exc}"

    if not fresh_rerun_performed:
        return _receipt(
            status="environment_failure",
            reason_code="fresh_rerun_route_drift" if fresh_rerun_error is None else "fresh_rerun_error",
            transport_payload_sha256=manifest_payload_sha256,
            consumed_payload_sha256=recomputed,
            mutation_applied=mutation_applied,
            readback_verified=readback_verified,
            fresh_rerun_performed=False,
            mutation_lane=mutation_lane,
            contract_update_status=contract_update_status,
        )

    final_receipt = _receipt(
        status="ok",
        reason_code=None,
        transport_payload_sha256=manifest_payload_sha256,
        consumed_payload_sha256=recomputed,
        mutation_applied=mutation_applied,
        readback_verified=readback_verified,
        fresh_rerun_performed=fresh_rerun_performed,
        mutation_lane=mutation_lane,
        contract_update_status=contract_update_status,
    )
    if final_receipt["status"] == "ok":
        ok, _readback2, error2 = _atomic_write_json_with_readback(consumption_receipt_path, final_receipt)
        if not ok:
            final_receipt = _receipt(
                status="environment_failure",
                reason_code="write_failure",
                transport_payload_sha256=manifest_payload_sha256,
                consumed_payload_sha256=recomputed,
                mutation_applied=mutation_applied,
                readback_verified=readback_verified,
                fresh_rerun_performed=fresh_rerun_performed,
                mutation_lane=mutation_lane,
                contract_update_status=contract_update_status,
            )
    return final_receipt


def _as_string_list(
    value: Any,
    field_name: str,
    blockers: list[str],
) -> tuple[list[str], bool]:
    """Extract a list[str] payload or record a blocker and fail closed."""
    if not isinstance(value, list):
        blockers.append(f"{BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD}: {field_name} must be a list")
        return [], False

    for item in value:
        if not isinstance(item, str):
            blockers.append(f"{BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD}: {field_name} contains non-string item")
            return [], False

    return value, True


def _build_safe_rewrite_constraints(
    required_sections: list[str],
    required_contract_keys: list[str],
) -> dict[str, Any]:
    """Build a schema-safe rewrite constraints payload for fail-closed payload violations."""
    return {
        "schema_version": "FAIL_CLOSED_REWRITE_CONSTRAINTS_V1",
        "required_sections": required_sections,
        "required_contract_keys": required_contract_keys,
        "rewrite_constraints": {
            "must_add_sections": required_sections,
            "must_add_contract_keys": required_contract_keys,
            "freeform_rewrite_forbidden": True,
        },
        "override_policy": {
            "allowed_reason_codes": [
                "missing_required_section",
                "missing_required_contract_key",
            ],
            "never_override_reason_codes": [
                "unknown_issue_kind",
                "issue_kind_policy_load_error",
                "contract_schema_parse_error",
                "template_resolution_error",
                "checker_internal_error",
            ],
            "overridable_in_current_result": [],
            "non_overridable_in_current_result": [],
        },
        "max_rewrite_attempts": 2,
        "no_progress_route": "human_judgment_required",
    }


def _ensure_json_serializable(value: Any, field_name: str, blockers: list[str]) -> bool:
    """Validate JSON serializability for deterministic stdout/hashing artifacts."""
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return True
    except (TypeError, ValueError) as exc:
        blockers.append(f"{BLOCKER_REWRITE_CONSTRAINTS_NOT_JSON_SERIALIZABLE}: {field_name} serialization error: {exc}")
        return False


def _find_repo_root() -> Path:
    """Walk up from this script to find the .git root."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    # Fallback: assume .claude/skills/issue-refinement-loop/scripts/
    return Path(__file__).resolve().parent.parent.parent.parent.parent


def _load_schema(schema_filename: str) -> dict | None:
    """Load a JSON schema file from the schemas directory. Returns None if not found."""
    schema_path = _SCHEMAS_DIR / schema_filename
    if not schema_path.exists():
        return None
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _validate_with_schema(data: dict, schema: dict) -> tuple[bool, list[str]]:
    """
    Validate data against schema using jsonschema.

    Returns (is_valid, error_messages).

    Fresh review blocker P1-A: schema enforcement is a real safety claim for
    the authority-transport mutation lanes (generate_authority_transport_manifest
    / consume_authority_transport's "ok" receipt gate). If jsonschema is
    unavailable, this fails CLOSED (returns False with reason code
    "schema_validator_unavailable") -- it must never be silently converted
    to a passing validation.
    """
    if not _JSONSCHEMA_AVAILABLE:
        return False, ["schema_validator_unavailable: jsonschema library not importable"]
    try:
        validator_cls = _jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)
        validator = validator_cls(
            schema,
            format_checker=validator_cls.FORMAT_CHECKER,
        )
        errors = sorted(validator.iter_errors(data), key=lambda exc: list(exc.path))
        if errors:
            return False, [f"schema_validation_error: {errors[0].message}"]
        format_errors = _validate_date_time_formats(data, schema)
        if format_errors:
            return False, format_errors
        return True, []
    except _jsonschema.ValidationError as exc:
        return False, [f"schema_validation_error: {exc.message}"]
    except Exception as exc:
        return False, [f"schema_validation_unexpected: {exc}"]


def _validate_date_time_formats(data: Any, schema: dict, path: str = "$") -> list[str]:
    schema_type = schema.get("type")
    if schema.get("format") == "date-time" and isinstance(data, str):
        candidate = data.replace("Z", "+00:00")
        try:
            datetime.fromisoformat(candidate)
        except ValueError:
            return [f"schema_validation_error: {path} must be a valid date-time"]
        return []

    if schema_type == "object" and isinstance(data, dict):
        errors: list[str] = []
        for key, value in data.items():
            child_schema = schema.get("properties", {}).get(key)
            if isinstance(child_schema, dict):
                errors.extend(_validate_date_time_formats(value, child_schema, f"{path}.{key}"))
        return errors

    if schema_type == "array" and isinstance(data, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            errors: list[str] = []
            for index, item in enumerate(data):
                errors.extend(_validate_date_time_formats(item, item_schema, f"{path}[{index}]"))
            return errors

    return []


# ---------------------------------------------------------------------------
# URL parsing for anchor comment structural validation
# ---------------------------------------------------------------------------

# Pattern: https://github.com/<owner>/<repo>/issues/<number>#issuecomment-<id>
_ISSUE_COMMENT_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"/issues/(?P<issue_number>\d+)#issuecomment-(?P<comment_id>\d+)$"
)

# PR review comment pattern (different from issue comment)
_PR_REVIEW_COMMENT_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/\d+#issuecomment-\d+$")
_PR_REVIEW_DISCUSSION_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/pull/\d+#discussion_r\d+$")

# Valid --repo pattern
_REPO_PATTERN = re.compile(r"^[^/]+/[^/]+$")

# Valid GitHub comment URL prefix
_GITHUB_URL_PREFIX = "https://github.com/"


def _parse_anchor_comment_url(url: str) -> dict[str, Any]:
    """
    Parse an anchor comment URL into its structural components.

    Returns dict with: owner, repo, issue_number (int), comment_id (int), valid (bool)
    Does NOT use substring matching — validates URL structure via regex only.
    """
    # Reject PR review comment URLs (different endpoint from issue comments)
    if _PR_REVIEW_DISCUSSION_RE.match(url):
        return {"valid": False, "error": "pr_review_comment_url"}

    m = _ISSUE_COMMENT_RE.match(url)
    if not m:
        return {"valid": False, "error": "url_parse_failure"}

    return {
        "valid": True,
        "owner": m.group("owner"),
        "repo": m.group("repo"),
        "issue_number": int(m.group("issue_number")),
        "comment_id": int(m.group("comment_id")),
    }


# ---------------------------------------------------------------------------
# gh CLI wrappers
# ---------------------------------------------------------------------------


def _run_gh(argv: list[str], timeout: int = GH_API_TIMEOUT) -> tuple[dict | list | None, str]:
    """
    Run a gh command and return (parsed_json, error_message).

    Uses subprocess.run([...], shell=False) — never shell=True.
    Returns (None, error_message) on timeout, non-zero exit, or JSON parse failure.
    """
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None, "gh_not_found"
    except subprocess.TimeoutExpired:
        return None, f"gh_timeout after {timeout}s"
    except Exception as exc:
        return None, f"gh_unexpected_error: {exc}"

    if proc.returncode != 0:
        stderr_snip = (proc.stderr or "")[:300]
        return None, f"gh_exit_{proc.returncode}: {stderr_snip}"

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return None, f"gh_json_decode_error: {exc}"

    return parsed, ""


def _fetch_issue(repo: str, issue_number: int) -> tuple[dict | None, str]:
    """Fetch issue data via gh issue view --json."""
    data, err = _run_gh(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "number,title,body,labels,url,updatedAt",
        ]
    )
    return data, err


def _fetch_issue_comments(repo: str, issue_number: int) -> tuple[list | None, str]:
    """Fetch all issue comments via gh api with --paginate --slurp.

    gh 2.88.1+ --slurp returns [[...page1...], [...page2...]] which must be
    flattened to a single list.  Single-page results are also wrapped as [[...]].
    """
    try:
        from command_registry import render_command as _render_command

        _argv = _render_command("gh.issue.comments.list", {"repo": repo, "issue_number": issue_number})
    except Exception as exc:
        raise RuntimeError(f"BLOCKER_COMMAND_REGISTRY_UNAVAILABLE: gh.issue.comments.list failed: {exc}") from exc
    data, err = _run_gh(_argv)
    if data is None:
        return None, err
    # --slurp wraps each page as an element: [[page1_comments...], [page2_comments...]]
    # Flatten one level regardless of page count.
    if isinstance(data, list):
        if len(data) == 0:
            return [], ""
        # Check if it's a slurp-wrapped list-of-lists
        if all(isinstance(item, list) for item in data):
            flattened: list[dict] = []
            for page in data:
                flattened.extend(page)
            return flattened, ""
        # Already a flat list (e.g. non-paginated gh or mock returning flat list)
        return data, ""
    return None, f"gh_comments_unexpected_type: {type(data).__name__}"


def _fetch_single_comment(repo: str, comment_id: int) -> tuple[dict | None, str]:
    """Fetch a single issue comment via gh api to validate issue_url field."""
    data, err = _run_gh(["gh", "api", f"repos/{repo}/issues/comments/{comment_id}"])
    return data, err


def _load_anchor_comment_schema() -> dict[str, Any]:
    schema_path = _SCHEMAS_DIR / "anchor_comment.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# ANCHOR_SCOPE_REFRAME_V1 parsing and classification (AC2-AC5)
# ---------------------------------------------------------------------------


def _parse_anchor_scope_reframe_body(comment_body: str) -> "dict | None":
    """
    Parse ANCHOR_SCOPE_REFRAME_V1 payload from a comment body.

    Only top-level fenced yaml blocks are canonical.
    Fail-closed: blockquote-embedded fenced blocks and raw-text markers are rejected.
    Returns None if not found or malformed.
    """
    import re

    fenced_pattern = re.compile(r"^```yaml\s*\n(.*?)^```", re.MULTILINE | re.DOTALL)
    for match in fenced_pattern.finditer(comment_body):
        yaml_content = match.group(1)
        # Fail-closed: reject if this fence is inside a blockquote
        start = match.start()
        before = comment_body[:start]
        if before.rstrip().endswith(">"):
            continue
        try:
            import yaml as _yaml

            data = _yaml.safe_load(yaml_content)
        except Exception:
            return None
        if isinstance(data, dict) and data.get("schema_version") == "ANCHOR_SCOPE_REFRAME_V1":
            return data
    return None


_FENCE_OPEN_RE = re.compile(
    r"^(?P<prefix>(?:[ \t]*>)*[ \t]{0,8})(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>.*)$"
)

_ANCHOR_SCOPE_REFRAME_KEY_NAMES = ("target", "decision", "allowed_path_deltas")


def _anchor_scope_reframe_fence_content_related(content_text: str) -> bool:
    """PR #2171 fix_delta (P2, OWNER adversarial review): a ```yaml fence's
    content only counts as an ANCHOR_SCOPE_REFRAME_V1 *attempt* -- not any
    unrelated fenced yaml a reviewer happens to quote (e.g. a config
    snippet) -- when it plausibly targets this schema.

    Fail-closed on ambiguity: content that does not even parse as YAML
    cannot be proven unrelated, so it is treated as related (the
    pre-existing present-but-invalid / fail_closed behavior for malformed
    fences, #2156 AC3, is preserved unchanged).
    """
    if "ANCHOR_SCOPE_REFRAME_V1" in content_text:
        return True
    try:
        import yaml as _yaml

        parsed = _yaml.safe_load(content_text)
    except Exception:
        return True
    if not isinstance(parsed, dict):
        return False
    if "schema_version" in parsed:
        return True
    return len(set(_ANCHOR_SCOPE_REFRAME_KEY_NAMES) & set(parsed.keys())) >= 2


def _anchor_scope_reframe_fence_present(comment_body: str) -> bool:
    """#2156 AC1 / PR #2171 fix_delta (P0-2, OWNER adversarial review):
    detect whether the comment body contains a ```yaml (or ~~~yaml) fence
    that is plausibly an ANCHOR_SCOPE_REFRAME_V1 payload attempt,
    independent of whether the fence's content parses as a valid
    ANCHOR_SCOPE_REFRAME_V1 payload.

    Used only to distinguish genuine absence (no such fence anywhere in the
    body -- the legitimate freeform lane) from present-but-invalid (a fence
    exists but is malformed YAML, declares the wrong schema_version, or is
    structurally rejected such as a blockquote-embedded fence). Non-canonical
    fence variants (blockquoted, malformed, wrong schema_version) must all
    stay in the present-but-invalid / fail_closed bucket -- only a body with
    no such fence marker at all is genuine absence.

    This is a line-oriented internal scanner (repo policy: PyYAML +
    jsonschema only, no external Markdown parser dependency added), not a
    full CommonMark implementation. It handles, unlike a single top-level
    regex: backtick fences of length >=3 (including 4+), tilde fences
    (``~~~yaml``), opening/closing fence length matching, 0-8 space /
    blockquote-marker indentation (including nested blockquotes), fences
    left unclosed through EOF, and a case-insensitive ``yaml`` info string.
    Presence is further gated on content relatedness (P2) via
    `_anchor_scope_reframe_fence_content_related()` so an unrelated ```yaml
    fence is not misclassified as a reframe attempt.
    """
    lines = comment_body.splitlines()
    n = len(lines)
    idx = 0
    found_any_yaml_fence = False
    found_related = False

    while idx < n:
        match = _FENCE_OPEN_RE.match(lines[idx])
        if not match:
            idx += 1
            continue
        info_tokens = match.group("info").strip().split()
        info_first_token = info_tokens[0].lower() if info_tokens else ""
        if info_first_token != "yaml":
            idx += 1
            continue

        fence_char = match.group("fence")[0]
        fence_len = len(match.group("fence"))
        prefix = match.group("prefix")
        close_re = re.compile(
            r"^(?:[ \t]*>)*[ \t]{0,8}"
            + re.escape(fence_char)
            + "{"
            + str(fence_len)
            + ",}[ \t]*$"
        )

        content_lines: list[str] = []
        close_idx = None
        j = idx + 1
        while j < n:
            if close_re.match(lines[j]):
                close_idx = j
                break
            content_lines.append(lines[j])
            j += 1

        found_any_yaml_fence = True

        if prefix.strip(" \t"):
            content_lines = [
                re.sub(r"^(?:[ \t]*>)+[ \t]?", "", line) for line in content_lines
            ]
        content_text = "\n".join(content_lines)

        if _anchor_scope_reframe_fence_content_related(content_text):
            found_related = True

        idx = (close_idx + 1) if close_idx is not None else n

    if not found_any_yaml_fence:
        return False
    return found_related


def _classify_anchor_scope_reframe(
    *,
    comment_payload: "dict",
    anchor_body: str,
    repo: str,
    issue_number: int,
    anchor_url: str,
) -> dict:
    """
    Classify anchor comment for ANCHOR_SCOPE_REFRAME_V1 trust and generate scope_delta_decision.

    Trusted if ALL of:
    - author_association in TRUSTED_ANCHOR_ASSOCIATIONS
    - Payload has ANCHOR_SCOPE_REFRAME_V1 schema_version
    - target.repo == repo
    - target.issue_number == issue_number
    - Payload passes anchor_scope_reframe_v1.schema.json validation

    Always returns a scope_delta_decision dict.
    """
    import hashlib as _hashlib

    author_assoc = comment_payload.get("author_association", "")
    anchor_hash = _hashlib.sha256(
        anchor_body.encode("utf-8") if isinstance(anchor_body, str) else anchor_body
    ).hexdigest()

    # Check author trust
    if author_assoc not in TRUSTED_ANCHOR_ASSOCIATIONS:
        return {
            "status": "fail_closed",
            "reason": f"untrusted_author_association: {author_assoc!r}",
            "implementation_go": False,
            "anchor_author_association": author_assoc or None,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": anchor_hash,
            "allowed_path_deltas": [],
            "required_rerun": [],
        }

    # Parse ANCHOR_SCOPE_REFRAME_V1 payload from body
    payload = _parse_anchor_scope_reframe_body(anchor_body)
    if payload is None:
        # #2156 AC1-AC3: distinguish genuine absence (no ```yaml fence at
        # all -- the legitimate freeform lane, #2053/#2086) from
        # present-but-invalid (a fence exists but is malformed YAML,
        # declares the wrong schema_version, or is structurally rejected
        # such as a blockquote-embedded fence). Only genuine absence is
        # downgraded to `not_applicable`; present-but-invalid stays
        # `fail_closed` with a `schema_invalid:` reason so
        # `_structured_anchor_payload_present_but_invalid()` continues to
        # forbid the freeform-authority downgrade fallback for it.
        if _anchor_scope_reframe_fence_present(anchor_body):
            return {
                "status": "fail_closed",
                "reason": (
                    "schema_invalid: "
                    "anchor_scope_reframe_v1_fence_present_but_unparseable_or_wrong_schema_version"
                ),
                "implementation_go": False,
                "anchor_author_association": author_assoc,
                "anchor_comment_url": anchor_url,
                "anchor_comment_hash": anchor_hash,
                "allowed_path_deltas": [],
                "required_rerun": [],
            }
        return {
            "status": "not_applicable",
            "reason": "no_anchor_scope_reframe_v1_payload",
            "implementation_go": False,
            "anchor_author_association": author_assoc,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": anchor_hash,
            "allowed_path_deltas": [],
            "required_rerun": [],
        }

    # Validate against schema (fail-closed on schema error)
    schema = _load_schema("anchor_scope_reframe_v1.schema.json")
    if schema is not None:
        valid, errors = _validate_with_schema(payload, schema)
        if not valid:
            return {
                "status": "fail_closed",
                "reason": f"schema_invalid: {errors[:3]}",
                "implementation_go": False,
                "anchor_author_association": author_assoc,
                "anchor_comment_url": anchor_url,
                "anchor_comment_hash": anchor_hash,
                "allowed_path_deltas": [],
                "required_rerun": [],
            }

    # Check target.repo
    target = payload.get("target", {})
    if target.get("repo") != repo:
        return {
            "status": "fail_closed",
            "reason": f"wrong_repo: expected {repo!r}, got {target.get('repo')!r}",
            "implementation_go": False,
            "anchor_author_association": author_assoc,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": anchor_hash,
            "allowed_path_deltas": [],
            "required_rerun": [],
        }

    # Check target.issue_number
    if target.get("issue_number") != issue_number:
        return {
            "status": "fail_closed",
            "reason": f"wrong_issue_number: expected {issue_number}, got {target.get('issue_number')!r}",
            "implementation_go": False,
            "anchor_author_association": author_assoc,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": anchor_hash,
            "allowed_path_deltas": [],
            "required_rerun": [],
        }

    # #2053 P2 fix-delta (iteration 3, OWNER PR review): a structured
    # ANCHOR_SCOPE_REFRAME_V1 anchor whose source generation/body revision
    # no longer matches current state is genuinely STALE -- distinct from
    # schema_invalid (never well-formed) or wrong_issue_number/wrong_repo
    # (well-formed but aimed elsewhere). GitHub comment metadata carries
    # `created_at`/`updated_at`; when both are present and differ, the
    # comment body actually consumed above (`anchor_body`) no longer
    # represents the original, unedited revision the payload was authored
    # against -- treat it as stale rather than silently trusting an edited
    # comment's current text as if it were the original approval.
    created_at = comment_payload.get("created_at")
    updated_at = comment_payload.get("updated_at")
    if (
        isinstance(created_at, str)
        and isinstance(updated_at, str)
        and created_at
        and updated_at
        and created_at != updated_at
    ):
        return {
            "status": "fail_closed",
            "reason": f"stale: comment edited after creation (created_at={created_at!r}, updated_at={updated_at!r})",
            "implementation_go": False,
            "anchor_author_association": author_assoc,
            "anchor_comment_url": anchor_url,
            "anchor_comment_hash": anchor_hash,
            "allowed_path_deltas": [],
            "required_rerun": [],
        }

    # All checks pass — trusted anchor
    return {
        "status": "approved_by_trusted_anchor",
        "implementation_go": False,
        "anchor_author_association": author_assoc,
        "anchor_comment_url": anchor_url,
        "anchor_comment_hash": anchor_hash,
        "allowed_path_deltas": payload.get("allowed_path_deltas", []),
        "required_rerun": payload.get("required_rerun", []),
    }


def _build_anchor_comment_state(
    *,
    anchor_url: str,
    comment: dict[str, Any],
    issue_number: int,
    captured_at: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    issue_url = comment.get("issue_url")
    if not isinstance(issue_url, str) or not issue_url:
        return None, [BLOCKER_ANCHOR_NOT_IN_ISSUE]

    parsed_url = urlparse(issue_url)
    path_parts = [part for part in parsed_url.path.split("/") if part]
    if len(path_parts) < 4 or path_parts[-2] != "issues" or path_parts[-1] != str(issue_number):
        return None, [BLOCKER_ANCHOR_NOT_IN_ISSUE]

    state = {
        "url": anchor_url,
        "id": comment.get("id"),
        "issue_number": issue_number,
        "html_url": comment.get("html_url"),
        "api_url": comment.get("url"),
        "user_login": ((comment.get("user") or {}).get("login")),
        "author_association": comment.get("author_association"),
        "snapshot": comment.get("body", ""),
        "captured_at": captured_at,
        "fetched_at": captured_at,
        "comment_created_at": comment.get("created_at"),
        "comment_updated_at": comment.get("updated_at"),
        "preliminary_classification": "feedback_update_required",
        "final_classification": None,
        (
            "classification_reason"
        ): "defaulted_by_preflight_schema_normalization; semantic classification deferred to #1008/#1011",
        "verified_claims": [],
        "unresolved_claims": [],
        "scope_impact": None,
        "requires_fact_check": False,
    }

    schema = _load_anchor_comment_schema()
    valid, errors = _validate_with_schema(state, schema)
    if not valid:
        return None, [BLOCKER_ANCHOR_COMMENT_SCHEMA_INVALID, *errors]
    return state, []


# ---------------------------------------------------------------------------
# Anchor comment structural validation
# ---------------------------------------------------------------------------


def _validate_anchor_comment_url(
    url: str,
    repo: str,
    issue_number: int,
    fixture_comments: Optional[list[dict]] = None,
) -> tuple[bool, list[str]]:
    """
    Validate a single anchor comment URL structurally.

    Checks (all must pass):
    1. URL owner/repo matches --repo
    2. URL issue_number matches --issue-number
    3. Comment id exists (via gh api or fixture)
    4. Comment's issue_url REST field points to same issue (must be present and non-empty)
    5. Not a PR review comment (different endpoint)

    Returns (is_valid, list_of_blocker_codes).
    Uses structural URL parsing only — no substring checks.
    """
    parsed = _parse_anchor_comment_url(url)

    if not parsed.get("valid"):
        error = parsed.get("error", "unknown")
        if error == "pr_review_comment_url":
            return False, [BLOCKER_ANCHOR_IS_PR_REVIEW, BLOCKER_ANCHOR_NOT_IN_ISSUE]
        return False, [BLOCKER_ANCHOR_NOT_IN_ISSUE]

    # Check 1: owner/repo match
    url_owner = parsed["owner"].lower()
    url_repo_name = parsed["repo"].lower()
    parts = repo.lower().split("/", 1)
    if len(parts) != 2:
        return False, [BLOCKER_ANCHOR_REPO_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    expected_owner, expected_repo_name = parts
    if url_owner != expected_owner or url_repo_name != expected_repo_name:
        return False, [BLOCKER_ANCHOR_REPO_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    # Check 2: issue number match
    if parsed["issue_number"] != issue_number:
        return False, [BLOCKER_ANCHOR_ISSUE_NUMBER_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    comment_id = parsed["comment_id"]

    # Check 3 & 4: comment exists and issue_url field matches
    if fixture_comments is not None:
        # Fixture mode: look up comment from pre-fetched data
        comment_data = None
        for c in fixture_comments:
            if isinstance(c, dict) and str(c.get("id")) == str(comment_id):
                comment_data = c
                break
        if comment_data is None:
            return False, [BLOCKER_ANCHOR_COMMENT_NOT_FOUND, BLOCKER_ANCHOR_NOT_IN_ISSUE]
    else:
        # Live mode: fetch via gh api
        comment_data, err = _fetch_single_comment(repo, comment_id)
        if comment_data is None:
            return False, [BLOCKER_ANCHOR_COMMENT_NOT_FOUND, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    # Check 4: issue_url field validation — must be present and non-empty
    issue_url_field = comment_data.get("issue_url")

    # Missing or empty issue_url → blocked (fail-closed)
    if not issue_url_field:
        return False, [BLOCKER_ANCHOR_ISSUE_URL_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]

    # Expected format: https://api.github.com/repos/<owner>/<repo>/issues/<number>
    # Also accept: https://github.com/<owner>/<repo>/issues/<number>
    expected_api_url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    expected_html_url = f"https://github.com/{repo}/issues/{issue_number}"

    if issue_url_field in (expected_api_url, expected_html_url):
        return True, []

    # Structural check via urlparse (not substring)
    parsed_url = urlparse(issue_url_field)
    path_parts = parsed_url.path.rstrip("/").split("/")
    if len(path_parts) >= 4 and path_parts[-2] == "issues" and path_parts[-1] == str(issue_number):
        # Repo path should be /<owner>/<repo>/
        if (
            len(path_parts) >= 5
            and path_parts[-4].lower() == expected_owner
            and path_parts[-3].lower() == expected_repo_name
        ):
            return True, []
        else:
            return False, [BLOCKER_ANCHOR_ISSUE_URL_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]
    else:
        return False, [BLOCKER_ANCHOR_ISSUE_URL_MISMATCH, BLOCKER_ANCHOR_NOT_IN_ISSUE]


def _validate_anchor_comments_batch(
    anchor_comment_urls: list[str],
    repo: str,
    issue_number: int,
    fixture_comments: Optional[list[dict]] = None,
) -> tuple[list[str], list[str]]:
    """
    Validate all anchor comment URLs. Returns (stable_sorted_unique_valid_urls, all_blockers).

    Stable sort + dedupe per spec. One invalid URL blocks all.
    ANCHOR_NOT_IN_ISSUE is always included as canonical blocker when any URL fails.
    """
    if not anchor_comment_urls:
        return [], []

    all_blockers: list[str] = []
    seen_urls: set[str] = set()
    deduped_urls: list[str] = []

    for url in anchor_comment_urls:
        if url not in seen_urls:
            seen_urls.add(url)
            deduped_urls.append(url)

    # Stable sort
    sorted_urls = sorted(deduped_urls)
    if len(sorted_urls) > 1:
        return [], [BLOCKER_ANCHOR_COMMENT_MULTIPLE_UNSUPPORTED]

    for url in sorted_urls:
        valid, blockers = _validate_anchor_comment_url(url, repo, issue_number, fixture_comments=fixture_comments)
        if not valid:
            all_blockers.extend(blockers)

    # Deduplicate blockers while preserving order
    seen_b: set[str] = set()
    deduped_blockers: list[str] = []
    for b in all_blockers:
        if b not in seen_b:
            seen_b.add(b)
            deduped_blockers.append(b)

    return sorted_urls, deduped_blockers


# ---------------------------------------------------------------------------
# Planner invocation
# ---------------------------------------------------------------------------


def _load_scope_rollup_artifact(repo_root: Path, issue_number: int) -> Optional[dict]:
    """
    Load a previously-persisted ISSUE_SCOPE_ROLLUP_PLAN_V2 artifact for this
    Issue, if one exists (#1677 AC4 join). Rerunning plan_issue_scope_rollup.py
    itself is out of scope here (#1677 Out of Scope); this function only
    consumes an artifact that a prior preflight step already produced.

    Returns None (non-blocking) when no artifact is present or it fails to
    parse -- absence of scope-rollup evidence must not block the refinement
    preflight, it only means the planner falls back to a minimal
    ISSUE_EXECUTION_DECISION_V1 ('selected', no relations).
    """
    artifact_path = _issue_artifact_dir(repo_root, issue_number) / "issue_scope_rollup_plan_v2.json"
    if not artifact_path.exists():
        return None
    try:
        return json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _join_scope_rollup_into_planner_input(
    planner_input: dict[str, Any],
    scope_rollup_plan: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """
    Join an ISSUE_SCOPE_ROLLUP_PLAN_V2 artifact into planner_input's
    known_context.scope_rollup_result (#1677 AC4).

    plan_refinement_loop.py's build_issue_execution_decision() reads
    known_context['scope_rollup_result'] to derive ISSUE_EXECUTION_DECISION_V1
    relations/execution state. Without this join, the planner always emits
    the minimal 'selected' shape regardless of known collisions.

    Pure function: does not mutate the input dict in place.
    """
    if not scope_rollup_plan:
        return planner_input
    joined = dict(planner_input)
    known_context = dict(joined.get("known_context") or {})
    known_context["scope_rollup_result"] = scope_rollup_plan
    joined["known_context"] = known_context
    return joined


def _build_planner_input(
    issue: dict,
    comments: list[dict],
    known_context: Optional[dict],
    anchor_comment_feedback: Optional[dict] = None,
    anchor_comment_ids: Optional[set[str]] = None,
    now: Optional[str] = None,
) -> dict:
    """Build REFINEMENT_LOOP_PLANNER_INPUT_V1 from issue/comments data."""
    labels = []
    raw_labels = issue.get("labels", [])
    for lbl in raw_labels:
        if isinstance(lbl, dict):
            labels.append(lbl.get("name", ""))
        elif isinstance(lbl, str):
            labels.append(lbl)

    planner_comments = comments
    if anchor_comment_ids:
        planner_comments = []
        for comment in comments:
            comment_id = comment.get("id")
            if comment_id is not None and str(comment_id) in anchor_comment_ids:
                sanitized = dict(comment)
                sanitized["body"] = "[redacted: anchor comment snapshot stored in artifact]"
                planner_comments.append(sanitized)
            else:
                planner_comments.append(comment)

    planner_input: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION_PLANNER_INPUT,
        "issue": {
            "number": issue.get("number"),
            "title": issue.get("title", ""),
            "body": issue.get("body", ""),
            "labels": labels,
        },
        "comments": planner_comments,
    }
    if known_context is not None:
        planner_input["known_context"] = known_context
    if anchor_comment_feedback is not None:
        planner_input["anchor_comment_feedback"] = anchor_comment_feedback
    if now is not None:
        planner_input["now"] = now

    return planner_input


def _issue_body_source_ref(issue: dict[str, Any], issue_number: int, repo: str) -> str:
    html_url = issue.get("html_url")
    if isinstance(html_url, str) and html_url:
        return html_url
    url = issue.get("url")
    if isinstance(url, str) and url:
        return url
    return f"https://github.com/{repo}/issues/{issue_number}"


def _issue_artifact_dir(repo_root: Path, issue_number: int) -> Path:
    return repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)


def _snapshot_archive_dir(repo_root: Path, issue_number: int) -> Path:
    return _issue_artifact_dir(repo_root, issue_number) / "snapshots"


def _snapshot_body(snapshot: dict[str, Any]) -> str:
    issue = snapshot.get("issue")
    if not isinstance(issue, dict):
        return ""
    body = issue.get("body", "")
    return body if isinstance(body, str) else ""


def _snapshot_fetched_at(snapshot: dict[str, Any]) -> str:
    value = snapshot.get("fetched_at", "")
    return value if isinstance(value, str) else ""


def _snapshot_archive_path(repo_root: Path, issue_number: int, snapshot: dict[str, Any]) -> Path:
    fetched_at = _snapshot_fetched_at(snapshot) or "unknown"
    safe_fetched_at = re.sub(r"[^0-9A-Za-z_-]+", "_", fetched_at)
    body_sha = _sha256(_snapshot_body(snapshot))
    return _snapshot_archive_dir(repo_root, issue_number) / (f"raw_issue_snapshot_{safe_fetched_at}_{body_sha}.json")


def _snapshot_source_ref(snapshot_path: Path, snapshot: dict[str, Any]) -> str:
    repo = snapshot.get("repo")
    issue_number = snapshot.get("issue_number")
    body_sha = _sha256(_snapshot_body(snapshot))
    fetched_at = _snapshot_fetched_at(snapshot)
    return (
        f"artifact:{snapshot_path.resolve()}"
        f"#repo={repo}&issue_number={issue_number}"
        f"&body_sha256={body_sha}&fetched_at={fetched_at}"
    )


def _load_valid_issue_snapshot(path: Path, issue_number: int, repo: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != "raw_issue_snapshot/v1":
        return None
    if payload.get("issue_number") != issue_number or payload.get("repo") != repo:
        return None
    issue = payload.get("issue")
    if not isinstance(issue, dict) or not isinstance(issue.get("body"), str):
        return None
    return payload


def _materialize_immutable_snapshot(
    repo_root: Path,
    issue_number: int,
    snapshot: dict[str, Any],
) -> Path:
    archive_dir = _snapshot_archive_dir(repo_root, issue_number)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = _snapshot_archive_path(repo_root, issue_number, snapshot)
    if not archive_path.exists():
        archive_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
    return archive_path


def _find_previous_immutable_snapshot(
    repo_root: Path,
    issue_number: int,
    repo: str,
    current_snapshot: dict[str, Any],
) -> tuple[dict[str, Any], str] | None:
    archive_dir = _snapshot_archive_dir(repo_root, issue_number)
    candidates: list[tuple[datetime, dict[str, Any], Path]] = []
    current_fetched_at = _snapshot_fetched_at(current_snapshot)
    try:
        current_sort_key = datetime.fromisoformat(current_fetched_at.replace("Z", "+00:00"))
    except ValueError:
        current_sort_key = None
    if archive_dir.exists():
        for path in archive_dir.glob("raw_issue_snapshot_*.json"):
            snapshot = _load_valid_issue_snapshot(path, issue_number, repo)
            if snapshot is None:
                continue
            fetched_at = _snapshot_fetched_at(snapshot)
            try:
                sort_key = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            except ValueError:
                sort_key = datetime.fromtimestamp(0, tz=timezone.utc)
            if current_sort_key is not None and sort_key >= current_sort_key:
                continue
            candidates.append((sort_key, snapshot, path))

    if not candidates:
        legacy_path = _issue_artifact_dir(repo_root, issue_number) / "raw_issue_snapshot.json"
        snapshot = _load_valid_issue_snapshot(legacy_path, issue_number, repo)
        if snapshot is not None:
            path = _materialize_immutable_snapshot(repo_root, issue_number, snapshot)
            candidates.append(
                (
                    datetime.fromisoformat(_snapshot_fetched_at(snapshot).replace("Z", "+00:00"))
                    if _snapshot_fetched_at(snapshot)
                    else datetime.fromtimestamp(0, tz=timezone.utc),
                    snapshot,
                    path,
                )
            )

    if not candidates:
        return None

    _, snapshot, path = max(candidates, key=lambda item: item[0])
    return snapshot, _snapshot_source_ref(path, snapshot)


def _ensure_scope_signal_delta_input(
    *,
    repo_root: Path,
    issue: dict[str, Any],
    raw_snapshot: dict[str, Any],
    known_context: Optional[dict[str, Any]],
    issue_number: int,
    repo: str,
) -> dict[str, Any]:
    merged = dict(known_context) if known_context else {}
    merged.setdefault("current_phase", "preflight")
    if "scope_signal_delta_input" in merged:
        return merged

    previous_snapshot = _find_previous_immutable_snapshot(
        repo_root,
        issue_number,
        repo,
        raw_snapshot,
    )
    if previous_snapshot is None:
        return merged

    previous_raw_snapshot, previous_source_ref = previous_snapshot
    current_body = issue.get("body", "") or ""
    current_archive_path = _snapshot_archive_path(repo_root, issue_number, raw_snapshot)
    current_source_ref = _snapshot_source_ref(current_archive_path, raw_snapshot)
    merged["scope_signal_delta_input"] = {
        "before_body": _snapshot_body(previous_raw_snapshot),
        "current_body": current_body,
        "after_body": current_body,
        "source_refs": {
            "before": previous_source_ref,
            "current": current_source_ref,
            "after": current_source_ref,
        },
    }
    return merged


def _validate_repair_result_schema_and_semantics(parsed: dict) -> Optional[str]:
    """Issue #2016 iteration-3 OWNER adversarial review, P0-1: validate a
    producer (`repair_issue_contract.py`) result against
    `repair_issue_contract_result_v1.schema.json` AND recompute
    `classify_repair_action()` from the raw `repairs[]` to cross-check the
    producer's self-reported `repair_action`. Returns None when the payload
    is fully valid (schema + cross-checks), else a fail-closed reason
    string. Applies to BOTH dry-run and --apply invocations.

    Fail-closed on:
      - schema violation (including additionalProperties: false)
      - missing/unknown `repair_action.schema_version` / `.policy_version`
        (never defaulted/backfilled -- Issue #2016 iteration-3 P0-1(2))
      - top-level SHA vs nested `repair_action` SHA mismatch (P0-1(4))
      - `changed` boolean disagreeing with SHA identity
      - `dry_run: true` with a non-null `candidate_body_artifact`
      - malformed `line_start`/`line_end` ranges
      - producer's self-reported `repair_action.disposition` /
        `.repair_kinds` disagreeing with `classify_repair_action()`
        recomputed from the raw `repairs[]` (P0-1(3))
    """
    schema = _load_schema("repair_issue_contract_result_v1.schema.json")
    if schema is None:
        return "repair_result_schema_unavailable"
    valid, errors = _validate_with_schema(parsed, schema)
    if not valid:
        return f"repair_result_schema_invalid:{errors[0] if errors else 'unknown'}"

    # anyOf(schema+error) payloads (CLI-level failure) have no repair_action
    # to cross-check; the caller already treats a present `error` key as an
    # environment_failure condition upstream of this validator.
    if parsed.get("error"):
        return None

    repair_action = parsed.get("repair_action")
    if not isinstance(repair_action, dict):
        return "repair_result_missing_repair_action"

    if repair_action.get("schema_version") != "repair_action/v1":
        return f"repair_action_schema_version_missing_or_unknown:{repair_action.get('schema_version')!r}"
    if repair_action.get("policy_version") != "deterministic-issue-repair/v1":
        return f"repair_action_policy_version_missing_or_unknown:{repair_action.get('policy_version')!r}"

    original_sha = parsed.get("original_body_sha256")
    repaired_sha = parsed.get("repaired_body_sha256")

    if repair_action.get("original_body_sha256") != original_sha:
        return "repair_action_nested_original_sha_mismatch"
    if repair_action.get("repaired_body_sha256") != repaired_sha:
        return "repair_action_nested_repaired_sha_mismatch"

    changed = parsed.get("changed")
    if changed is False and original_sha != repaired_sha:
        return "repair_changed_false_but_sha_mismatch"
    if changed is True and original_sha == repaired_sha:
        return "repair_changed_true_but_sha_identical"

    dry_run = parsed.get("dry_run")
    if dry_run is True and repair_action.get("candidate_body_artifact") is not None:
        return "repair_dry_run_but_candidate_artifact_present"

    repairs = parsed.get("repairs")
    if not isinstance(repairs, list):
        return "repair_repairs_field_not_a_list"

    for record in repairs:
        if not isinstance(record, dict):
            continue
        start = record.get("line_start")
        end = record.get("line_end")
        if isinstance(start, int) and isinstance(end, int):
            if start < 1 or end < 1 or start > end:
                return f"repair_record_invalid_line_range:{start}:{end}"

    if classify_repair_action is None:
        return "repair_action_classifier_unavailable"
    recomputed = classify_repair_action(original_sha, repaired_sha, repairs)
    if recomputed.get("disposition") != repair_action.get("disposition"):
        return (
            f"repair_action_disposition_mismatch:producer={repair_action.get('disposition')!r}"
            f":recomputed={recomputed.get('disposition')!r}"
        )
    if sorted(recomputed.get("repair_kinds", [])) != sorted(repair_action.get("repair_kinds", []) or []):
        return "repair_action_repair_kinds_mismatch"

    return None


def _run_repair_subprocess(argv_extra: list[str], body: str) -> tuple[Optional[dict], Optional[str]]:
    """
    Run repair_issue_contract.py as a subprocess with `argv_extra` appended
    after `--body-file <tmp>`. Fail-closed (Issue #2016 P0-3): returncode,
    stdout presence, and JSON structure are all explicitly checked. Never
    raises. Returns (parsed_dict_or_None, error_reason_or_None) — exactly
    one of the two is non-None.
    """
    import tempfile
    import os as _os
    import sys as _sys
    import subprocess as _sp
    import json as _json

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(body)
        tmp_path = tf.name

    try:
        proc = _sp.run(
            [_sys.executable, str(REPAIR_SCRIPT), "--body-file", tmp_path, *argv_extra],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except _sp.TimeoutExpired:
        return None, "repair_subprocess_timeout"
    except Exception as exc:
        return None, f"repair_subprocess_launch_error:{type(exc).__name__}:{exc}"
    finally:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass

    # P0-3: explicitly check returncode. subprocess.run() defaults check=False,
    # so a non-zero exit with JSON-looking stdout would otherwise silently
    # pass through as a normal payload.
    if proc.returncode != 0:
        return None, f"repair_subprocess_nonzero_exit:{proc.returncode}:{(proc.stderr or '')[:500]}"

    if not proc.stdout:
        return None, "repair_subprocess_no_stdout"

    try:
        parsed = _json.loads(proc.stdout)
    except _json.JSONDecodeError as exc:
        return None, f"repair_subprocess_invalid_json:{exc}"

    if not isinstance(parsed, dict):
        return None, "repair_subprocess_payload_not_object"

    if parsed.get("schema") != "repair_issue_contract/v1":
        return None, f"repair_subprocess_schema_mismatch:{parsed.get('schema')!r}"

    if parsed.get("error"):
        return None, f"repair_subprocess_payload_error:{parsed.get('error')}"

    # Issue #2016 iteration-3 P0-1: validate schema + cross-check
    # classify_repair_action() BEFORE trusting the producer's self-reported
    # repair_action. A schema violation or a disposition/repair_kinds
    # mismatch is treated identically to a subprocess-level failure
    # (fail-closed -> environment_failure upstream).
    semantic_error = _validate_repair_result_schema_and_semantics(parsed)
    if semantic_error is not None:
        return None, f"repair_subprocess_semantic_validation_failed:{semantic_error}"

    return parsed, None


def _invoke_repair(body: str) -> dict:
    """
    Invoke repair_issue_contract.py (dry-run) to pre-process the Issue body
    before feeding it to the planner.

    Returns the repair result dict (schema: repair_issue_contract/v1). On any
    subprocess/JSON/schema failure (Issue #2016 P0-3, fail-closed), returns a
    dict carrying an "error" key; callers MUST treat a present "error" key as
    an environment_failure condition rather than as changed=False/no-op.
    """
    parsed, error = _run_repair_subprocess([], body)
    if error is not None:
        return {
            "schema": "repair_issue_contract/v1",
            "changed": False,
            "repairs": [],
            "error": error,
        }
    return parsed


def _write_planner_spawn_attempt_marker(
    repo_root: Optional[Path], issue_number: Optional[int], *, script_missing: bool
) -> None:
    """Issue #2073 P1-4 (OWNER review): record a pre-spawn diagnostic stage
    *before* the planner subprocess.run() call is issued, so a future
    reproduction of the `pid_proof_planner.json` flake can distinguish "the
    planner subprocess.run() call site was never reached" from "the call was
    made but the child never got far enough to write its own proof". This is
    intentionally the smallest possible monotonic stage marker (not a general
    telemetry system) and is best-effort: a failure to write it must never
    change `_invoke_planner`'s own control flow or return value.
    """
    if repo_root is None or issue_number is None:
        return
    try:
        marker_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "planner_spawn_attempt_v1.json").write_text(
            json.dumps(
                {
                    "schema_version": "PLANNER_SPAWN_ATTEMPT_V1",
                    "stage": "pre_subprocess_run",
                    "script_missing_precheck": script_missing,
                    "pid": os.getpid(),
                    "recorded_at": _now_iso(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _invoke_planner(
    planner_input: dict,
    *,
    repo_root: Optional[Path] = None,
    issue_number: Optional[int] = None,
) -> tuple[dict | None, int, str, str]:
    """
    Invoke plan_refinement_loop.py via subprocess.run([sys.executable, ...], shell=False).

    Returns (plan_dict, exit_code, stderr_text, raw_stdout).
    plan_dict is None on JSON parse failure.

    `repo_root` / `issue_number` are optional and, when supplied, are used
    only to write the best-effort `planner_spawn_attempt_v1.json` pre-spawn
    diagnostic marker (Issue #2073 P1-4); they never affect the planner
    invocation itself. The marker write is this function's literal first
    statement (Issue #2073 human-review P2-1: it previously ran after
    `json.dumps(planner_input, ...)`, so a `planner_input` serialization
    failure -- e.g. non-JSON-serializable content, or a NaN/Infinity value
    rejected by `allow_nan=False` -- would raise before the marker was ever
    written, reproducing the exact "absence of evidence is not evidence of
    absence" gap this marker exists to close).
    """
    script_missing = not PLANNER_SCRIPT.is_file()
    _write_planner_spawn_attempt_marker(repo_root, issue_number, script_missing=script_missing)

    input_json = json.dumps(planner_input, ensure_ascii=False, allow_nan=False)

    # Issue #2073 P1-2 (OWNER review): the previous implementation relied on
    # subprocess.run() raising FileNotFoundError to detect a missing planner
    # script, but that exception is only raised when the *executable*
    # (argv[0], i.e. sys.executable) itself cannot be found -- not when a
    # present interpreter is asked to run a missing *script* (argv[1]). In
    # that case the interpreter itself starts, prints its own "can't open
    # file" message to stderr, and exits with its own nonzero code (commonly
    # 2), which previously fell through to the generic JSONDecodeError path
    # below and was misclassified identically to an "invalid input / schema
    # error" (exit 2) planner failure. Detecting the missing script
    # explicitly, before ever spawning a process, makes this failure mode
    # deterministic and reuses the same (exit 3, "planner script not
    # found: ...") shape the `except FileNotFoundError` branch below already
    # produces for the (much rarer) missing-executable case, so downstream
    # classification (`classify_planner_failure`) sees a consistent
    # "not found" signal in `stderr` regardless of which of the two ways the
    # script turned out to be missing.
    if script_missing:
        return None, 3, f"planner script not found: {PLANNER_SCRIPT}", ""

    try:
        proc = subprocess.run(
            [sys.executable, str(PLANNER_SCRIPT)],
            input=input_json,
            shell=False,
            timeout=PLANNER_TIMEOUT,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return None, 3, f"planner timeout after {PLANNER_TIMEOUT}s", ""
    except FileNotFoundError:
        return None, 3, f"planner script not found: {PLANNER_SCRIPT}", ""
    except Exception as exc:
        return None, 3, f"planner unexpected error: {exc}", ""

    stderr_text = proc.stderr or ""
    exit_code = proc.returncode
    raw_stdout = proc.stdout or ""

    if exit_code not in (0, 2, 3):
        return None, exit_code, stderr_text, raw_stdout

    try:
        plan = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return None, exit_code, f"planner stdout JSON decode error: {exc}", raw_stdout

    return plan, exit_code, stderr_text, raw_stdout


# ---------------------------------------------------------------------------
# warn condition detection
# ---------------------------------------------------------------------------


def _has_unknown_confidence(plan: dict) -> bool:
    """
    Return True if any decision in plan.decisions.*.confidence == "unknown".

    This determines the warn condition:
    planner exit 0 + fail_closed.required == false + >=1 unknown confidence → warn/1.
    """
    decisions = plan.get("decisions", {})
    for _key, policy in decisions.items():
        if isinstance(policy, dict) and policy.get("confidence") == "unknown":
            return True
    return False


# ---------------------------------------------------------------------------
# Exit code mapping
# ---------------------------------------------------------------------------


def _apply_exit_code_mapping(
    planner_exit_code: Optional[int],
    planner_fail_closed: Optional[bool],
    blockers: list[str],
    plan: Optional[dict] = None,
    scope_delta_decision: Optional[dict] = None,
    repair_needs_fix: bool = False,
) -> tuple[str, int]:
    """
    Apply the Planner ↔ Wrapper Exit Code Mapping table.

    Returns (status_str, exit_code_int).

    warn condition: planner exit 0 AND fail_closed=false AND >=1 unknown confidence
    → status: warn / exit 1

    PR #1973 (OWNER REQUEST_CHANGES, P1-5): a genuine multi-turn
    trusted-owner advisory route (`scope_delta_decision.status == "warn"`,
    set by `_apply_multi_turn_candidate_route()`) must also surface as a
    CI-visible warn/EXIT_WARN, not silently stay `pass`/EXIT_PASS just
    because `_has_unknown_confidence(plan)` happens to be False.

    Issue #2016: `repair_needs_fix=True` (repair_action.disposition ==
    "auto_apply_safe", with no unrelated blocker present) overrides an
    otherwise pass/warn outcome to needs_fix/EXIT_NEEDS_FIX. It intentionally
    does NOT override blocked/environment_failure — those routes already
    indicate a more fundamental stop condition than a repairable Issue body
    defect. AC5 (Issue #2016): when `repair_needs_fix=False` (e.g. changed:
    false), this function's return value is unchanged from before Issue #2016.
    """
    # Pre-planner blockers (anchor mismatch, gh failure)
    if blockers:
        anchor_blockers = {
            BLOCKER_ANCHOR_NOT_IN_ISSUE,
            BLOCKER_ANCHOR_REPO_MISMATCH,
            BLOCKER_ANCHOR_ISSUE_NUMBER_MISMATCH,
            BLOCKER_ANCHOR_COMMENT_NOT_FOUND,
            BLOCKER_ANCHOR_ISSUE_URL_MISMATCH,
            BLOCKER_ANCHOR_IS_PR_REVIEW,
            BLOCKER_INPUT_SCHEMA_INVALID,
            BLOCKER_INVALID_ARGS,
        }
        env_blockers = {
            BLOCKER_GH_FAILURE,
            BLOCKER_RESULT_SCHEMA_INVALID,
            BLOCKER_REPAIR_ENVIRONMENT_FAILURE,
        }
        has_env = any(b in env_blockers for b in blockers)
        has_anchor = any(b in anchor_blockers for b in blockers)
        # AC6: REWRITE_CONSTRAINTS_* and planner_fail_closed_payload_invalid
        # are environment failures (payload integrity), not issue blockers.
        rewrite_env_blockers = {
            BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD,
            BLOCKER_REWRITE_CONSTRAINTS_NOT_JSON_SERIALIZABLE,
            BLOCKER_REWRITE_CONSTRAINTS_INVARIANT_VIOLATION,
            BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID,
            BLOCKER_ARTIFACT_PROJECTION_MISMATCH,
        }
        if any(any(b.split(":", 1)[0] == rb for rb in rewrite_env_blockers) for b in blockers):
            has_env = True

        if has_env:
            return "environment_failure", EXIT_ENVIRONMENT_FAILURE
        if has_anchor:
            return "blocked", EXIT_BLOCKED
        # Other pre-planner blockers → blocked
        return "blocked", EXIT_BLOCKED

    if planner_exit_code is None:
        return "environment_failure", EXIT_ENVIRONMENT_FAILURE

    if planner_exit_code == 2:
        return "blocked", EXIT_BLOCKED

    if planner_exit_code == 3:
        return "environment_failure", EXIT_ENVIRONMENT_FAILURE

    if planner_exit_code == 0:
        if planner_fail_closed is True:
            return "blocked", EXIT_BLOCKED
        if repair_needs_fix:
            return "needs_fix", EXIT_NEEDS_FIX
        # Check warn condition: >=1 decision has confidence: unknown
        if plan is not None and _has_unknown_confidence(plan):
            return "warn", EXIT_WARN
        # Check warn condition: multi-turn trusted-owner advisory route
        if scope_delta_decision is not None and scope_delta_decision.get("status") == "warn":
            return "warn", EXIT_WARN
        return "pass", EXIT_PASS

    # Unknown exit code
    return "environment_failure", EXIT_ENVIRONMENT_FAILURE


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------


def _write_artifacts(
    repo_root: Path,
    issue_number: int,
    raw_snapshot: dict,
    planner_input: dict,
    result: dict,
) -> dict[str, str]:
    """
    Write artifacts to .claude/artifacts/issue-refinement-loop/<issue_number>/.

    Returns {artifact_key: absolute_path_str}.
    issue_number is int-normalized; path is NOT generated from repo name or URL.

    Writes:
      - raw_issue_snapshot.json  (raw issue + comments)
      - planner_input.json       (planner stdin JSON, byte-stable)
      - refinement_preflight_result_v1.json (canonical result)
    """
    artifact_dir = _issue_artifact_dir(repo_root, issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = artifact_dir / "raw_issue_snapshot.json"
    planner_input_path = artifact_dir / "planner_input.json"
    result_path = artifact_dir / "refinement_preflight_result_v1.json"
    artifacts = {
        "raw_issue_snapshot": str(snapshot_path),
        "planner_input": str(planner_input_path),
        "refinement_preflight_result_v1": str(result_path),
    }
    # Issue #2016 iteration-3 P1-1: needs_fix results additionally carry
    # `repair_diagnostics` / `repair_candidate_body` in artifacts. These are
    # written earlier in the flow (repair_diagnostics.json /
    # repaired_issue_body.md), so this validates presence + the expected
    # fixed on-disk path rather than re-writing them, and fails closed on
    # any unrecognized extra key or path drift.
    result_artifacts = result.get("artifacts")
    allowed_extra_artifact_keys = {
        "repair_diagnostics": artifact_dir / "repair_diagnostics.json",
        "repair_candidate_body": artifact_dir / "repaired_issue_body.md",
    }
    if isinstance(result_artifacts, dict):
        for key in set(result_artifacts.keys()) - set(artifacts.keys()):
            expected_path = allowed_extra_artifact_keys.get(key)
            if expected_path is None:
                raise ValueError(f"final_result_artifact_projection_mismatch: unexpected_key {key}")
            if result_artifacts.get(key) != str(expected_path):
                raise ValueError(f"final_result_artifact_projection_mismatch: path_drift {key}")
            if not expected_path.is_file():
                raise ValueError(f"final_result_artifact_projection_missing_file: {key}")
            artifacts[key] = result_artifacts[key]
    if result.get("artifacts") != artifacts:
        raise ValueError("final_result_artifact_projection_mismatch")
    schema_errors = _validate_result_artifact(result)
    if schema_errors:
        raise ValueError("final_result_schema_invalid: " + "; ".join(schema_errors))
    _atomic_write_json(snapshot_path, raw_snapshot)
    _materialize_immutable_snapshot(repo_root, issue_number, raw_snapshot)
    _atomic_write_json(planner_input_path, planner_input)
    _atomic_write_json(result_path, result)
    try:
        readback = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"result_artifact_readback_error:{type(exc).__name__}:{exc}") from exc
    readback_errors = _validate_result_artifact(readback)
    if readback_errors:
        raise ValueError("result_artifact_readback_schema_invalid: " + "; ".join(readback_errors))
    return artifacts


def _atomic_write_json(path: Path, value: Any) -> None:
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_text(path: Path, text: str) -> None:
    """Same atomicity/durability contract as _atomic_write_json(), for
    non-JSON text artifacts (e.g. candidate Issue body markdown)."""
    encoded = text.encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _materialize_auto_apply_candidate(
    body: str,
    dry_run_repair_result: dict,
    artifact_dir: Path,
) -> tuple[Optional[dict], Optional[str]]:
    """
    Re-invoke repair_issue_contract.py with --apply against the SAME body
    that produced the dry-run auto_apply_safe classification, to materialize
    a candidate_body_artifact for the needs_fix / apply_deterministic_repair
    route (Issue #2016 recommended design, "auto-apply is an optimistic
    concurrency transaction"). This performs NO GitHub mutation — it only
    writes a local candidate file inside `artifact_dir`.

    Returns (apply_result_dict, None) on success, or (None, reason) on any
    fail-closed condition (subprocess failure, input SHA drift between the
    two invocations, artifact write/readback failure).
    """
    candidate_path = artifact_dir / "repaired_issue_body.md"

    parsed, error = _run_repair_subprocess(
        ["--apply", "--out-file", str(candidate_path), "--artifact-root", str(artifact_dir)], body
    )
    if error is not None:
        return None, f"repair_apply_{error}"

    # Optimistic concurrency guard: the dry-run and --apply invocations must
    # observe the identical input/output hash pair. A mismatch means the
    # body changed between the two subprocess calls (or a producer version
    # drift), and auto-apply must NOT proceed.
    if parsed.get("original_body_sha256") != dry_run_repair_result.get("original_body_sha256"):
        return None, "repair_apply_input_sha_mismatch"
    if parsed.get("repaired_body_sha256") != dry_run_repair_result.get("repaired_body_sha256"):
        return None, "repair_apply_output_sha_mismatch"

    if not candidate_path.exists():
        return None, "repair_apply_candidate_artifact_missing"

    try:
        written_text = candidate_path.read_text(encoding="utf-8")
    except Exception as exc:
        return None, f"repair_apply_candidate_artifact_unreadable:{type(exc).__name__}"

    written_sha = "sha256:" + hashlib.sha256(written_text.encode("utf-8")).hexdigest()
    if written_sha != parsed.get("repaired_body_sha256"):
        return None, "repair_apply_candidate_artifact_readback_mismatch"

    return parsed, None


def _validate_result_artifact(result: Any) -> list[str]:
    schema = _load_schema("refinement_preflight_result_v1.schema.json")
    if schema is None:
        return ["result_schema_unavailable"]
    valid, errors = _validate_with_schema(result, schema)
    return [] if valid else errors


# ---------------------------------------------------------------------------
# Compact stdout projection
# ---------------------------------------------------------------------------


def _build_compact_stdout(result: dict) -> str:
    """
    Build agent-friendly compact projection of result for stdout.

    Canonical fields (always emitted if present):
      STATUS, NEXT_ACTION, MUST_READ, COMMANDS, BLOCKERS, ARTIFACT

    MUST NOT include raw issue body, raw comments, or any sentinel-containing fields.
    DO_NOT_READ is a reserved field and is intentionally not emitted here.
    EVIDENCE (raw body/comments) is never emitted to stdout.
    """
    lines = [
        f"STATUS: {result['status']}",
        f"NEXT_ACTION: {result['next_action']}",
    ]

    must_read = result.get("must_read", [])
    if must_read:
        lines.append("MUST_READ:")
        for p in must_read:
            lines.append(f"  - {p}")

    commands = result.get("commands", [])
    if commands:
        try:
            from command_registry import REGISTRY as _REG

            spec_objects = []
            for cmd in commands:
                cmd_id = cmd.get("id") or cmd.get("kind", "?")
                entry = _REG.get(cmd_id, {})
                spec_objects.append(
                    {
                        "id": cmd_id,
                        "argv": cmd.get("argv", []),
                        "shell": cmd.get("shell", False),
                        "cwd_policy": entry.get("cwd_policy", "repo_root"),
                        "stdin_contract": entry.get("stdin_contract", "none"),
                        "stdout_contract": entry.get("stdout_contract", "unknown"),
                        "timeout_seconds": entry.get("timeout_seconds", 120),
                        "mutation": entry.get("mutation", False),
                    }
                )
            lines.append("COMMANDS_JSON: " + json.dumps(spec_objects, ensure_ascii=False, separators=(",", ":")))
            lines.append("COMMANDS_DISPLAY:")
            for cmd in commands:
                argv_str = " ".join(str(a) for a in cmd.get("argv", []))
                lines.append(f"  display: [{cmd.get('id') or cmd.get('kind', '?')}] {argv_str}")
        except ImportError:
            lines.append("COMMANDS:")
            for cmd in commands:
                argv_str = " ".join(cmd.get("argv", []))
                lines.append(f"  - [{cmd.get('kind', '?')}] {argv_str}")

    blockers = result.get("blockers", [])
    if blockers:
        lines.append("BLOCKERS:")
        for b in blockers:
            lines.append(f"  - {b}")

    required_sections = result.get("required_sections", [])
    if required_sections:
        lines.append("REQUIRED_SECTIONS:")
        for section in required_sections:
            lines.append(f"  - {section}")

    required_contract_keys = result.get("required_contract_keys", [])
    if required_contract_keys:
        lines.append("REQUIRED_CONTRACT_KEYS:")
        for key in required_contract_keys:
            lines.append(f"  - {key}")

    rewrite_constraints = result.get("rewrite_constraints")
    if rewrite_constraints:
        lines.append("REWRITE_CONSTRAINTS:")
        rewritten = json.dumps(
            rewrite_constraints,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        lines.append(f"  {rewritten}")

    repair_action = result.get("repair_action")
    if repair_action:
        lines.append("REPAIR_ACTION:")
        rewritten_repair_action = json.dumps(
            repair_action,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        lines.append(f"  {rewritten_repair_action}")

    artifacts = result.get("artifacts", {})
    if artifacts:
        lines.extend(_render_artifact_projection_lines(artifacts))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def _build_result(
    *,
    status: str,
    issue_number: int,
    repo: str,
    planner_exit_code: Optional[int],
    planner_fail_closed: Optional[bool],
    next_action: str,
    must_read: list[str],
    do_not_read: list[str],
    commands: list[dict],
    blockers: list[str],
    planner_fail_closed_reason_codes: list[str],
    required_sections: list[str],
    required_contract_keys: list[str],
    rewrite_constraints: Optional[dict[str, Any]],
    artifacts: dict[str, str],
    hashes: dict[str, str],
    contract_update: Optional[dict[str, Any]] = None,
    repair_action: Optional[dict[str, Any]] = None,
) -> dict:
    """Build a refinement_preflight_result/v1 compliant dict."""
    result = {
        "schema_version": SCHEMA_VERSION_RESULT,
        "status": status,
        "issue_number": issue_number,
        "repo": repo,
        "planner_exit_code": planner_exit_code,
        "planner_fail_closed": planner_fail_closed,
        "next_action": next_action,
        "must_read": must_read,
        "do_not_read": do_not_read,
        "commands": commands,
        "blockers": blockers,
        "planner_fail_closed_reason_codes": planner_fail_closed_reason_codes,
        "required_sections": required_sections,
        "required_contract_keys": required_contract_keys,
        "artifacts": artifacts,
        "hashes": hashes,
    }
    if rewrite_constraints is not None:
        result["rewrite_constraints"] = rewrite_constraints
    if contract_update is not None:
        result["contract_update"] = contract_update
    if repair_action is not None:
        result["repair_action"] = repair_action
    return result


def _bounded_contract_update_handoff(consumer_result: dict[str, Any]) -> dict[str, Any]:
    """Project the transaction-local consumer result into the existing result.

    This intentionally retains only the phase outcome needed by the parent
    orchestrator.  Source bodies, candidate bodies, operation identities, and
    transaction payloads remain local to the controlled phase.

    PR #2057 OWNER REQUEST_CHANGES revision (P0-2 / P1-3): the returned dict
    now carries an explicit ``disposition`` (patch / proven_no_change /
    full_rewrite_required / invalid, echoing the single canonical
    classifier) alongside ``status``, and TWO status values that used to
    collapse into an existing enum member now have their own outcome:

    - An `invalid` disposition (malformed/binding-mismatched scope-delta
      decision) is `status: failed` with `disposition: invalid` -- distinct
      from an ordinary `no_change`/`proven_no_change` outcome, which it
      previously matched byte-for-byte.
    - A `full_rewrite_required` disposition (approved scope reframe, empty
      operations[], not yet reflected) is `status: handoff_required` --
      NOT `failed`. No write was ever attempted (`writes` stays 0); this is
      a legitimate control transfer to issue-editor, not a mutation
      failure. `final_readback` is `not_applicable` (nothing was written,
      so there is nothing to read back -- never a synthesized `verified`).
    """
    raw_status = consumer_result.get("status")
    states = consumer_result.get("states")
    state = states.get("contract_update", {}).get("status") if isinstance(states, dict) else None
    iterations = consumer_result.get("iterations", 0)
    fresh = consumer_result.get("fresh_checks")
    if not isinstance(fresh, dict):
        fresh = {}
    rewrite_route = consumer_result.get("rewrite_route")
    disposition_sidecar = consumer_result.get("disposition")

    if raw_status == "invalid" or (
        isinstance(disposition_sidecar, dict) and disposition_sidecar.get("disposition") == "invalid"
    ):
        reason_code = (
            disposition_sidecar.get("reason_code")
            if isinstance(disposition_sidecar, dict)
            else "invalid_scope_delta_decision"
        )
        return {
            "status": "failed",
            "disposition": "invalid",
            "writes": 0,
            "iterations": int(iterations) if isinstance(iterations, int) else 0,
            "final_readback": "not_applicable",
            "fresh_preflight": "unavailable",
            "fresh_review": "unavailable",
            "fresh_readiness": "unavailable",
            "reason_code": reason_code,
        }

    if isinstance(rewrite_route, dict) and rewrite_route.get("route") == "issue_editor_required":
        return {
            "status": "handoff_required",
            "disposition": "full_rewrite_required",
            "writes": 0,
            "iterations": 0,
            "final_readback": "not_applicable",
            "fresh_preflight": str(fresh.get("preflight", "unavailable")),
            "fresh_review": str(fresh.get("review", "unavailable")),
            "fresh_readiness": str(fresh.get("readiness", "unavailable")),
            "reason_code": rewrite_route.get("reason_code"),
        }

    # A completed transaction is not an implementation authorization.  The
    # post-update rerun is a six-gate conjunction: missing, warn, or failing
    # checks must fail the phase even though the controlled write itself
    # reached final readback successfully.  Do not infer a pass from prose or
    # an earlier transaction result.
    post_update_gate_passed = (
        fresh.get("preflight") == "pass"
        and fresh.get("review") == "approve"
        and fresh.get("readiness") == "go"
        and fresh.get("allowed_paths") == "pass"
        and fresh.get("permission_profile") == "pass"
        and fresh.get("runtime_evidence") == "pass"
    )
    if raw_status == "applied" and iterations == 1 and post_update_gate_passed:
        status = "rebased"
    elif raw_status in {"applied", "no_change"} and post_update_gate_passed:
        status = state if state in {"applied", "no_change", "rebased"} else "failed"
    else:
        status = "failed"
    disposition = "patch" if raw_status == "applied" else ("proven_no_change" if raw_status == "no_change" else None)
    return {
        "status": status,
        "disposition": disposition,
        "writes": int(consumer_result.get("writes", 0)) if isinstance(consumer_result.get("writes", 0), int) else 0,
        "iterations": int(iterations) if isinstance(iterations, int) else 0,
        "final_readback": "verified" if raw_status in {"applied", "no_change"} else "failed",
        "fresh_preflight": str(fresh.get("preflight", "unavailable")),
        "fresh_review": str(fresh.get("review", "unavailable")),
        "fresh_readiness": str(fresh.get("readiness", "unavailable")),
    }


def _commands_from_plan(plan: dict, issue_number: int, repo: str) -> list[dict]:
    """Build commands[] from ISSUE_REFINEMENT_COMMAND_REGISTRY_V1 preflight.run entry."""
    try:
        from command_registry import render_command as _render_command, REGISTRY as _REGISTRY

        _entry = _REGISTRY.get("preflight.run", {})
        _argv = _render_command("preflight.run", {"issue_number": issue_number, "repo": repo})
    except Exception as exc:
        raise RuntimeError(f"BLOCKER_COMMAND_REGISTRY_UNAVAILABLE: command_registry render failed: {exc}") from exc
    return [
        {
            "kind": "run_preflight",
            "argv": _argv,
            "shell": False,
            "source": "registry",
        }
    ]


def _emit_failure_result(
    *,
    repo_root: Path,
    issue_number: int,
    repo: str,
    status: str,
    next_action: str,
    blockers: list[str],
    planner_exit_code: Optional[int] = None,
    planner_fail_closed: Optional[bool] = None,
    planner_input: Optional[dict] = None,
    raw_snapshot: Optional[dict] = None,
    planner_fail_closed_reason_codes: Optional[list[str]] = None,
    required_sections: Optional[list[str]] = None,
    required_contract_keys: Optional[list[str]] = None,
    rewrite_constraints: Optional[dict[str, Any]] = None,
) -> tuple[dict, int]:
    """
    Build a failure/blocked/environment_failure result, write artifacts if available,
    print compact stdout, and return (result, exit_code).

    This helper ensures stdout and disk are written from the same final result dict
    (no post-write mutation).
    """
    # Compute hashes if raw_snapshot available
    hashes: dict[str, str] = {}
    if raw_snapshot is not None:
        snapshot_text = json.dumps(raw_snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False)
        hashes["raw_issue_snapshot_sha256"] = _sha256(snapshot_text)
    if planner_input is not None:
        planner_input_text = json.dumps(planner_input, sort_keys=True, ensure_ascii=False, allow_nan=False)
        hashes["planner_input_sha256"] = _sha256(planner_input_text)

    artifacts: dict[str, str] = {}
    if raw_snapshot is not None and planner_input is not None:
        artifact_dir = _issue_artifact_dir(repo_root, issue_number)
        artifacts = {
            "raw_issue_snapshot": str(artifact_dir / "raw_issue_snapshot.json"),
            "planner_input": str(artifact_dir / "planner_input.json"),
            "refinement_preflight_result_v1": str(artifact_dir / "refinement_preflight_result_v1.json"),
        }

    result = _build_result(
        status=status,
        issue_number=issue_number,
        repo=repo,
        planner_exit_code=planner_exit_code,
        planner_fail_closed=planner_fail_closed,
        next_action=next_action,
        must_read=[],
        do_not_read=[],
        commands=[],
        blockers=blockers,
        planner_fail_closed_reason_codes=planner_fail_closed_reason_codes or [],
        required_sections=required_sections or [],
        required_contract_keys=required_contract_keys or [],
        rewrite_constraints=rewrite_constraints,
        artifacts=artifacts,
        hashes=hashes,
    )

    if raw_snapshot is not None and planner_input is not None:
        try:
            _write_artifacts(repo_root, issue_number, raw_snapshot, planner_input, result)
        except Exception as exc:
            result["blockers"] = [
                *result["blockers"],
                BLOCKER_RESULT_SCHEMA_INVALID,
                f"failure_artifact_write_error:{type(exc).__name__}:{str(exc)[:500]}",
            ]
            result["status"] = "environment_failure"
            result["next_action"] = "fix_environment"
            result["artifacts"] = {}

    _, exit_code = _apply_exit_code_mapping(planner_exit_code, planner_fail_closed, result["blockers"])
    if result["status"] == "environment_failure":
        exit_code = EXIT_ENVIRONMENT_FAILURE
    print(_build_compact_stdout(result))
    return result, exit_code


# ---------------------------------------------------------------------------
# SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 (#1323) -- freeform human review comment
# evidence builder (independent of the structured ANCHOR_SCOPE_REFRAME_V1
# payload consumed by _classify_anchor_scope_reframe above).
# ---------------------------------------------------------------------------

# Heavy mutation categories (#1891 AC6): mutation intents that require an
# explicit owner-sourced decision before they may proceed. Non-heavy
# categories continue with a warning instead of a hard stop.
HEAVY_MUTATION_CATEGORIES = frozenset(
    {
        "close",
        "not_planned",
        "replacement_issue_creation",
        "dependency_removal",
        "parent_child_change",
    }
)


def _classify_heavy_mutation_gate(
    *,
    mutation_category: "str | None",
    scope_delta_decision: "dict | None",
) -> dict:
    """#1891 AC6: fail-closed gate for heavy mutation categories.

    Heavy mutation categories (close / not planned / replacement Issue
    creation / dependency removal / parent-child relationship change) are
    blocked unless `scope_delta_decision` reflects an explicit owner-sourced
    decision (`status == "approved_by_trusted_anchor"` and
    `anchor_author_association == "OWNER"`). Non-heavy mutation categories
    (ordinary body improvement / additional investigation / review
    continuation) are never blocked here -- they continue with a `warn`
    status, matching the pre-existing advisory-only behavior.
    """
    decision = scope_delta_decision or {}
    owner_explicit = (
        decision.get("status") == "approved_by_trusted_anchor"
        and decision.get("anchor_author_association") == "OWNER"
    )
    is_heavy = mutation_category in HEAVY_MUTATION_CATEGORIES

    if owner_explicit:
        return {
            "mutation_category": mutation_category,
            "is_heavy_mutation": is_heavy,
            "status": "allowed",
            "fail_closed": False,
            "reason": "owner_explicit_decision_present",
        }

    if is_heavy:
        return {
            "mutation_category": mutation_category,
            "is_heavy_mutation": True,
            "status": "blocked",
            "fail_closed": True,
            "reason": "heavy_mutation_requires_owner_explicit_decision",
        }

    return {
        "mutation_category": mutation_category,
        "is_heavy_mutation": False,
        "status": "warn",
        "fail_closed": False,
        "reason": "non_heavy_mutation_warning_continue",
    }


def _apply_multi_turn_candidate_route(
    scope_delta_decision: dict,
    segments_result: "dict | None",
    candidates_result: "dict | None",
    *,
    integrity_predicates: "dict | None" = None,
) -> dict:
    """#1891 AC4 / #1950 AC1: route multi-turn anchors when anchor_context.py
    finds multiple unclassified candidates spread across a genuine
    multi-turn transcript (>=2 marker-delimited segments).

    This does not fire for an ordinary single-turn review comment that
    happens to contain several bullet points -- it is scoped to the
    multi-turn/supersession scenario this Issue targets, so it never
    auto-selects a single winning candidate and never silently overrides an
    existing fail-closed decision with a weaker one.

    #1950 AC1 (chronology vs. precedence vs. authorization are separate
    axes): when the anchor comment's author is a trusted OWNER
    (``scope_delta_decision["anchor_author_association"] == "OWNER"``), a
    genuine multi-turn transcript is no longer a hard stop by itself. The
    index / source span of the *last* OWNER-speaker segment is recorded as
    ``latest_owner_turn`` chronology metadata only -- it is never promoted
    to a technical_recommendation or mutation_authorization precedence
    signal, and multi-turn ambiguity alone still does not grant
    implementation_go. Non-OWNER (or untrusted) multi-turn anchors keep the
    pre-existing hard `fail_closed` route unchanged.

    PR #1973 (OWNER REQUEST_CHANGES, P0-1): the advisory `warn` route must
    never silently overwrite an ORIGINAL `approved_by_trusted_anchor`
    decision (which would break the heavy-mutation gate's OWNER-explicit
    check downstream) or an ORIGINAL `fail_closed` decision whose reason is
    something other than the ordinary "no structured payload" case (e.g.
    `schema_invalid`, `wrong_repo`, `wrong_issue_number`,
    `untrusted_author_association`) -- those reflect a distinct integrity
    problem that a multi-turn transcript does not resolve. The advisory
    downgrade is applied ONLY when the original decision is
    `fail_closed` / `no_anchor_scope_reframe_v1_payload` AND retrieval
    integrity (`integrity_predicates`: fetch complete, hash verified, source
    ranges covered) is fully confirmed.
    """
    if not isinstance(segments_result, dict) or not isinstance(candidates_result, dict):
        return scope_delta_decision

    marked_segments = [seg for seg in segments_result.get("segments", []) if seg.get("marker")]
    candidates = candidates_result.get("candidates", [])

    if len(marked_segments) >= 2 and len(candidates) >= 2:
        is_trusted_owner = scope_delta_decision.get("anchor_author_association") == "OWNER"
        owner_segments = [
            seg for seg in marked_segments if seg.get("speaker") == anchor_context.SPEAKER_OWNER
        ]

        if is_trusted_owner and owner_segments:
            last_owner_segment = owner_segments[-1]
            latest_owner_turn = {
                "segment_index": last_owner_segment.get("index"),
                "source_range": {
                    "start_line": last_owner_segment.get("start_line"),
                    "end_line": last_owner_segment.get("end_line"),
                },
                # chronology metadata only; not technical_recommendation or
                # mutation_authorization precedence (#1950 AC1).
                "note": (
                    "chronology metadata only -- not technical_recommendation "
                    "or mutation_authorization precedence"
                ),
            }

            original_status = scope_delta_decision.get("status")
            original_reason = scope_delta_decision.get("reason")

            if original_status == "approved_by_trusted_anchor":
                # Never overwrite a valid owner approval with `warn` -- the
                # heavy-mutation gate depends on this status surviving
                # unchanged.
                updated = dict(scope_delta_decision)
                updated["latest_owner_turn"] = latest_owner_turn
                return updated

            predicates = integrity_predicates or {}
            integrity_confirmed = (
                predicates.get("source_fetch_complete") is True
                and predicates.get("source_hash_verified") is True
                and predicates.get("source_ranges_covered") is True
            )

            # #2156 AC5/AC6: `no_anchor_scope_reframe_v1_payload` (genuine
            # absence) is produced by `_classify_anchor_scope_reframe()`
            # with `status: not_applicable` now (AC2), but legacy/fixture
            # callers may still pass a `status: fail_closed` decision
            # carrying the same reason -- both upstream statuses must be
            # recognized here so the advisory-upgrade / blocking-preserved
            # invariants do not silently stop firing.
            if original_reason == "no_anchor_scope_reframe_v1_payload" and original_status in (
                "fail_closed",
                "not_applicable",
            ):
                if integrity_confirmed:
                    updated = dict(scope_delta_decision)
                    updated["anchor_context_candidate_count"] = len(candidates)
                    updated["anchor_context_marked_segment_count"] = len(marked_segments)
                    updated["implementation_go"] = False
                    updated["status"] = "warn"
                    updated["reason"] = "multi_turn_anchor_context_trusted_owner_advisory"
                    updated["latest_owner_turn"] = latest_owner_turn
                    return updated

                if original_status == "not_applicable":
                    # #2156 AC6: the upstream genuine-absence status is
                    # non-blocking (`not_applicable`), but multi-turn
                    # retrieval integrity is unconfirmed here -- this must
                    # not silently remain non-blocking. Force back to a
                    # blocking `fail_closed` state rather than letting the
                    # upstream not_applicable downgrade survive unchanged.
                    #
                    # PR #2171 fix_delta (P0-1, OWNER adversarial review):
                    # the `reason` must change to a dedicated value here --
                    # leaving it as `no_anchor_scope_reframe_v1_payload`
                    # (the upstream not_applicable reason) meant the
                    # caller's canonical blocker conversion (which does an
                    # exact match on
                    # `multi_turn_anchor_context_requires_human_judgment`)
                    # never recognized this route, so it silently never
                    # reached `blockers` / the final exit code.
                    updated = dict(scope_delta_decision)
                    updated["status"] = "fail_closed"
                    updated["reason"] = (
                        "multi_turn_anchor_context_retrieval_integrity_unconfirmed"
                    )
                    updated["implementation_go"] = False
                    updated["latest_owner_turn"] = latest_owner_turn
                    return updated

                # original_status == "fail_closed" with unconfirmed
                # integrity: pre-existing behavior, unchanged -- legacy
                # callers/fixtures already representing a blocking decision
                # are returned untouched (identity).
                return scope_delta_decision

            # Any other original status/reason (schema_invalid, wrong_repo,
            # wrong_issue_number, untrusted_author_association) is returned
            # unchanged -- the multi-turn advisory route never masks a
            # distinct integrity problem.
            return scope_delta_decision

        updated = dict(scope_delta_decision)
        updated["anchor_context_candidate_count"] = len(candidates)
        updated["anchor_context_marked_segment_count"] = len(marked_segments)
        updated["implementation_go"] = False
        updated["status"] = "fail_closed"
        updated["reason"] = "multi_turn_anchor_context_requires_human_judgment"
        return updated

    return scope_delta_decision


def _structured_anchor_payload_present_but_invalid(scope_delta_decision: "dict | None") -> bool:
    """#2053 AC2: distinguish "no ANCHOR_SCOPE_REFRAME_V1 payload at all"
    (legitimate freeform lane, e.g. Issue #1270) from "a structured payload
    WAS present but is invalid/stale/wrong-target". Only the latter must
    forbid downgrading to freeform authority evidence built from the same
    comment body -- accepting the same untrusted-shape text as freeform
    directive would let an attacker/mistake dodge structured schema
    validation simply by making the payload fail it.

    `_classify_anchor_scope_reframe()` reason strings for a payload that WAS
    parsed but rejected are: "schema_invalid: ...", "wrong_repo: ...",
    "wrong_issue_number: ...", "stale: ..." (#2053 P2 fix-delta iteration 3
    -- the comment's own `created_at`/`updated_at` no longer match, i.e. it
    was edited after the original approval). "no_anchor_scope_reframe_v1_payload"
    (no payload found) and "untrusted_author_association: ..." (author
    trust, independent of payload presence) are NOT included -- an
    untrusted author's freeform text is already fail-closed downstream by
    classify_scope_delta_authority()'s own author-association check.
    """
    if not isinstance(scope_delta_decision, dict):
        return False
    if scope_delta_decision.get("status") != "fail_closed":
        return False
    reason = scope_delta_decision.get("reason") or ""
    return reason.startswith(("schema_invalid:", "wrong_repo:", "wrong_issue_number:", "stale:"))


def _build_scope_delta_authority_evidence(
    *,
    comment_payload: dict,
    comment_body: str,
    repo: str,
    issue_number: int,
    anchor_url: str,
    captured_at: str,
    human_context_comment_urls: Any = None,
    agent_report_comment_urls: Any = None,
    segments_result: "dict | None" = None,
    candidates_result: "dict | None" = None,
):
    """#1323: build SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 from an anchor comment.

    Unlike _classify_anchor_scope_reframe (which requires a structured
    ANCHOR_SCOPE_REFRAME_V1 fenced yaml payload), this works on freeform
    human review comments (e.g. Issue #1270's Revised Acceptance Criteria
    comment) so explicit human-review directives are not silently dropped
    just because they are not machine-formatted.

    Returns None (fail-closed) when the anchor URL does not structurally
    resolve to an issue-comment on `issue_number` in `repo` (AC16). Never
    forwards the raw comment body -- only sha256 + extracted markers /
    directives / boundary flags (AC14).

    #1891 AC4 (iteration 2, PR #1923 OWNER REQUEST_CHANGES): `segments_result`
    / `candidates_result` are the anchor_context.py pure-analyzer outputs for
    this same `comment_body`. They are accepted here (instead of only being
    threaded through a disconnected downstream call) so the multi-turn
    candidate route is wired at the same call site that builds the scope
    delta authority evidence, per the Issue's In Scope wiring requirement.
    They are intentionally NOT added to the returned
    SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 dict (its schema is `additionalProperties:
    false`); the caller applies `_apply_multi_turn_candidate_route()` to
    `scope_delta_decision` immediately after this call using the same
    `segments_result` / `candidates_result` values.
    """
    try:
        from scope_signal_delta import (
            classify_directive_confidence,
            detect_boundary_flags,
            extract_directive_items,
            extract_directive_markers,
            parse_issue_comment_url,
        )
    except ImportError:
        return None

    parsed = parse_issue_comment_url(anchor_url)
    if parsed is None:
        return None
    if f"{parsed['owner']}/{parsed['repo']}".lower() != repo.lower():
        return None
    if parsed["issue_number"] != issue_number:
        return None

    author_association = comment_payload.get("author_association")
    user = comment_payload.get("user")
    if isinstance(user, dict):
        author_login = user.get("login")
        author_type = user.get("type") or "unknown"
    else:
        author_login = comment_payload.get("author_login")
        author_type = comment_payload.get("author_type") or "unknown"

    markers = extract_directive_markers(comment_body)
    directives = extract_directive_items(comment_body)
    boundary_flags_map = detect_boundary_flags(comment_body)
    boundary_flag_names = [name for name, value in boundary_flags_map.items() if value]
    # #2086 AC1/AC3/AC6: source_kind must be resolved before confidence is
    # classified so that the operator-selected human-context lane (the only
    # lane where `source_kind == "issue_comment"`, see
    # `_resolve_scope_delta_source_kind()`) can relax the "known marker
    # heading required" rule for freeform directives. `with_agent_report` /
    # unlabeled anchors always resolve to `generated_by_agent` here and never
    # reach this relaxation.
    source_kind = _resolve_scope_delta_source_kind(
        anchor_url,
        human_context_comment_urls=human_context_comment_urls,
        agent_report_comment_urls=agent_report_comment_urls,
    )
    confidence = classify_directive_confidence(
        comment_body,
        markers,
        operator_asserted_human_context=(source_kind == "issue_comment"),
    )

    issue_url = f"https://github.com/{repo}/issues/{issue_number}"

    return {
        "schema_version": "SCOPE_DELTA_AUTHORITY_EVIDENCE_V1",
        "source_kind": source_kind,
        "source_ref": anchor_url,
        "source_issue_number": issue_number,
        "comment_id": comment_payload.get("id"),
        "comment_url": anchor_url,
        "issue_url": issue_url,
        "body_sha256": _sha256(comment_body),
        "author_login": author_login,
        "author_type": author_type,
        "author_association": author_association,
        "captured_at": captured_at,
        "directive_markers": markers,
        "extracted_directives": directives,
        "ambiguity_flags": [] if confidence != "ambiguous" else ["structured_list_missing"],
        "boundary_flags": boundary_flag_names,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# #2048 Scope Delta: consumer-boundary scope-reframe disposition helpers
# ---------------------------------------------------------------------------


def _classify_scope_delta_decision(
    *,
    known_context: Optional[dict[str, Any]],
    anchor_url: str,
    anchor_body: str,
) -> "tuple[str, list[str] | None]":
    """Classify `known_context["scope_delta_decision"]` into a tri-state.

    Returns ``(kind, deltas)`` where ``kind`` is one of:

    - ``"absent"``: no scope-reframe decision was supplied at all (this is
      an ordinary, non-scope-reframe patch-plan consumer call --
      `deltas` is `None`).
    - ``"invalid"``: a decision IS present but fails validation or binding
      (malformed `allowed_path_deltas` type/blank entries, or an
      anchor URL/hash mismatch against THIS consumer call's anchor/body).
      PR #2057 OWNER review P0-1: this is a DISTINCT outcome from
      ``"absent"`` -- the pre-fix implementation collapsed both into the
      same ``None`` return, so an invalid decision was silently treated as
      an ordinary no-op (`proven_no_change`-equivalent) instead of
      `invalid`.
    - ``"valid"``: `deltas` is the validated non-empty ``list[str]``, bound
      to this anchor URL and anchor body hash.
    """
    if not isinstance(known_context, dict):
        return "absent", None
    decision = known_context.get("scope_delta_decision")
    if not isinstance(decision, dict):
        return "absent", None
    if decision.get("status") != "approved_by_trusted_anchor":
        return "absent", None
    deltas = decision.get("allowed_path_deltas")
    if deltas is None:
        return "absent", None
    if not isinstance(deltas, list):
        return "invalid", None
    if not deltas:
        # An explicit empty list is not a scope-reframe signal at all
        # (consistent with `classify_scope_reframe_disposition()`'s own
        # `is_approved_scope_reframe = ... and bool(normalized_deltas)`
        # gate) -- "absent", not "invalid". Only a non-list type or a
        # blank/whitespace entry WITHIN a non-empty list is `invalid`.
        return "absent", None
    if not all(isinstance(item, str) and item.strip() for item in deltas):
        return "invalid", None
    if decision.get("anchor_comment_url") != anchor_url:
        return "invalid", None
    if decision.get("anchor_comment_hash") != _sha256(anchor_body):
        return "invalid", None
    return "valid", list(deltas)


def _extract_validated_scope_delta_deltas(
    *,
    known_context: Optional[dict[str, Any]],
    anchor_url: str,
    anchor_body: str,
) -> "list[str] | None":
    """Back-compat wrapper: returns the validated deltas, or `None` for
    both `"absent"` and `"invalid"` (callers that only need existence, not
    the distinction, e.g. the run_preflight() empty-patch-plan-synthesis
    gate). See `_classify_scope_delta_decision()` for the tri-state form
    that `consume_trusted_anchor_contract_patch_plan()` uses to distinguish
    `invalid` from an ordinary absent decision.
    """
    _, deltas = _classify_scope_delta_decision(
        known_context=known_context, anchor_url=anchor_url, anchor_body=anchor_body
    )
    return deltas


def _normalize_scope_reframe_delta_literal(raw: str) -> str:
    """Strip bullet/backtick decoration from an `allowed_path_deltas` entry.

    `ANCHOR_SCOPE_REFRAME_V1.allowed_path_deltas` entries are free-form
    strings (see `anchor_scope_reframe_v1.schema.json`); in practice trusted
    anchors write them as Markdown bullet items (e.g.
    ``"- `docs/x.md`"``). This normalizes to a bare path/pattern comparable
    against `baseline_vc_preflight.extract_allowed_paths()` output.
    """
    text = raw.strip()
    for prefix in ("- ", "* ", "+ "):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text.strip("`").strip()


def _check_scope_reframe_deltas_reflected(
    *,
    current_body: str,
    allowed_path_deltas: list[str],
) -> str:
    """#2048 / PR #2057 OWNER review P1-5: tri-state Allowed Paths
    postcondition check.

    Returns one of:
      - ``"present"``: every `allowed_path_deltas` entry is present in the
        canonical `## Allowed Paths` section's EXACT normalized set (real
        `proven_no_change` disposition).
      - ``"absent"``: the canonical parser loaded successfully but at least
        one delta entry is not in the exact set (real
        `full_rewrite_required` candidate, pending the empty-operations
        classifier).
      - ``"invalid_or_unavailable"``: the canonical parser could not be
        loaded/executed, OR `allowed_path_deltas` is empty. Never silently
        treated as "absent" -- the caller's classifier maps this to
        `invalid`, since "the rewrite may already be reflected but we could
        not check" must never authorize a full-body-rewrite handoff, nor a
        false proven-satisfied no-op.

    The previous implementation additionally accepted
    ``normalized_delta in current_body`` (a whole-body substring fallback)
    whenever the delta was not found in the canonical Allowed Paths section.
    That fallback is REMOVED: it could match a delta literal that only
    appears in `## Outcome`, `## Notes`, a fenced code block, prose, or as a
    substring of an unrelated longer path, misclassifying `full_rewrite_
    required` as `proven_no_change`. Only the canonical section's exact
    normalized set is used.
    """
    if not allowed_path_deltas:
        return "invalid_or_unavailable"
    try:
        import importlib.util

        baseline_path = (
            _SCRIPTS_DIR.parent.parent / "issue-contract-review" / "scripts" / "baseline_vc_preflight.py"
        )
        baseline_spec = importlib.util.spec_from_file_location(
            "scope_reframe_allowed_paths_baseline", baseline_path
        )
        if baseline_spec is None or baseline_spec.loader is None:
            return "invalid_or_unavailable"
        baseline_module = importlib.util.module_from_spec(baseline_spec)
        baseline_spec.loader.exec_module(baseline_module)
        existing = set(baseline_module.extract_allowed_paths(current_body) or [])
    except Exception:
        return "invalid_or_unavailable"
    normalized_existing = {_normalize_scope_reframe_delta_literal(path) for path in existing}
    for delta in allowed_path_deltas:
        normalized_delta = _normalize_scope_reframe_delta_literal(delta)
        if not normalized_delta or normalized_delta not in normalized_existing:
            return "absent"
    return "present"


def consume_trusted_anchor_contract_patch_plan(
    *,
    repo: str,
    issue_number: int,
    issue: dict,
    anchor_url: str,
    anchor_payload: dict,
    anchor_body: str,
    contract_patch_plan: dict,
    callbacks: Optional[dict[str, Any]] = None,
    known_context: Optional[dict[str, Any]] = None,
    patch_plan_producer_available: bool = True,
) -> dict:
    """Connect an approved patch plan to the controlled transaction lane.

    The planner remains read-only.  This explicit consumer is called only by
    the opt-in preflight execution path after a trusted directive has produced
    an existing ``CONTRACT_PATCH_PLAN_V1``.  Its default callbacks use the
    existing readiness checker and ``edit_issue_txn.py``; tests can inject
    fixture callbacks without a GitHub mutation.
    """
    from scope_signal_delta import run_trusted_anchor_iteration_zero

    callbacks = callbacks or {}
    temporary_paths: list[Path] = []

    # #2048 Scope Delta: `contract_patch_plan["operations"]` must be a real
    # list. A malformed type (not a list) is never coerced to `[]` -- that
    # would silently treat an invalid/untyped payload as an ordinary no-op
    # replay. Fail closed instead: no rewrite_route classification, no
    # mutation attempt.
    _raw_operations = contract_patch_plan.get("operations", [])
    if not isinstance(_raw_operations, list):
        return {
            "status": "blocked",
            "failure": "contract_patch_plan_operations_not_list",
            "writes": 0,
            "iterations": 0,
        }

    # #2048 Scope Delta: join the trusted structured scope-reframe
    # classification (`known_context["scope_delta_decision"]`, produced by
    # `_classify_anchor_scope_reframe()` further up the call chain) to this
    # consumer boundary. This is the ONLY production call site that invokes
    # `run_trusted_anchor_iteration_zero()`'s `allowed_path_deltas` /
    # `scope_delta_status` kwargs -- without this join, an approved scope
    # reframe with empty `operations[]` could never reach the classifier
    # from `contract_update.run.with_*` (the #2048 PR review blocker across
    # iteration 1/2/3). PR #2057 OWNER review P0-1: `_classify_scope_delta_
    # decision()` returns a tri-state (absent/invalid/valid) so a decision
    # that IS present but binding-mismatched is `invalid`, never silently
    # treated the same as "no scope reframe at all".
    _decision_kind, _validated_allowed_path_deltas = _classify_scope_delta_decision(
        known_context=known_context,
        anchor_url=anchor_url,
        anchor_body=anchor_body,
    )
    if _decision_kind == "invalid":
        return {
            "status": "invalid",
            "disposition": {
                "schema_version": "scope_reframe_decision/v1",
                "disposition": "invalid",
                "reason_code": "invalid_scope_delta_decision_binding",
            },
            "writes": 0,
            "iterations": 0,
        }
    # PR #2057 OWNER review P1-4/P1-5: the Allowed-Paths-reflected tri-state
    # is intentionally NOT computed here against `issue["body"]` (the
    # pre-fetch snapshot passed into this function). It is computed by
    # `run_trusted_anchor_iteration_zero()` itself, against a FRESH
    # `fetch_current()` re-read, immediately before it decides between
    # `proven_no_change` and `full_rewrite_required` -- closing the TOCTOU
    # window where the Issue body could have been edited (by a concurrent
    # issue-editor write reflecting the very delta this call is
    # classifying) between this consumer being invoked and the disposition
    # being decided.
    def _reflected_checker(fresh_body: str) -> str:
        return _check_scope_reframe_deltas_reflected(
            current_body=fresh_body,
            allowed_path_deltas=_validated_allowed_path_deltas or [],
        )

    def fetch_current() -> tuple[dict, dict]:
        injected = callbacks.get("fetch_current")
        if injected is not None:
            return injected()
        current_issue, issue_error = _fetch_issue(repo, issue_number)
        if current_issue is None:
            raise RuntimeError(f"issue_readback_failed:{issue_error}")
        comment_id = anchor_payload.get("id")
        current_anchor, anchor_error = _fetch_single_comment(repo, int(comment_id))
        if current_anchor is None:
            raise RuntimeError(f"anchor_readback_failed:{anchor_error}")
        current_anchor = dict(current_anchor)
        current_anchor["html_url"] = anchor_url
        current_anchor["source_body_sha256"] = f"sha256:{_sha256(current_anchor.get('body', ''))}"
        return current_issue, current_anchor

    def candidate_readiness(candidate_body: str) -> dict:
        injected = callbacks.get("candidate_readiness")
        if injected is not None:
            return injected(candidate_body)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8", delete=False) as handle:
            handle.write(candidate_body)
            candidate_path = Path(handle.name)
        temporary_paths.append(candidate_path)
        readiness_script = (
            _SCRIPTS_DIR.parent.parent / "issue-contract-review" / "scripts" / "contract_readiness_check.py"
        )
        completed = subprocess.run(
            [sys.executable, str(readiness_script), "--body-file", str(candidate_path), "--mode", "static"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
        )
        try:
            readiness = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {
                "status": "input_or_runtime_error",
                "body_sha256": f"sha256:{_sha256(candidate_body)}",
                "source_checks": [],
                "errors": [],
                "readiness_result_ref": "transaction-local",
            }
        if not isinstance(readiness, dict):
            return {
                "status": "input_or_runtime_error",
                "body_sha256": f"sha256:{_sha256(candidate_body)}",
                "source_checks": [],
                "errors": [],
                "readiness_result_ref": "transaction-local",
            }
        readiness["readiness_result_ref"] = "transaction-local"
        return readiness

    def apply_transaction(current_issue: dict, candidate_body: str, readiness: dict) -> dict:
        injected = callbacks.get("apply_transaction")
        if injected is not None:
            return injected(current_issue, candidate_body, readiness)
        repo_root = _find_repo_root()
        candidate_path = repo_root / "tmp" / f"issue_{issue_number}_trusted_anchor_candidate.md"
        input_path = repo_root / "tmp" / f"issue_{issue_number}_trusted_anchor_txn.json"
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(candidate_body, encoding="utf-8")
        temporary_paths.extend([candidate_path, input_path])
        from scope_signal_delta import build_issue_edit_txn_input

        # The readiness checker exposes a richer result for callers, while
        # edit_issue_txn.py intentionally accepts only its closed forwarding
        # payload. Keep this narrowing transaction-local so the controlled
        # executor's input validation remains strict.
        readiness_forwarding = {
            key: readiness[key]
            for key in (
                "status",
                "body_sha256",
                "source_checks",
                "errors",
                "readiness_result_ref",
                "resolution_evidence",
            )
            if key in readiness
        }
        transaction_input = build_issue_edit_txn_input(
            issue_number=issue_number,
            repo=repo,
            previous_body_sha256=f"sha256:{_sha256(current_issue.get('body', ''))}",
            previous_updated_at=current_issue["updatedAt"],
            new_body_file=str(candidate_path.relative_to(repo_root)),
            readiness_result=readiness_forwarding,
        )
        input_path.write_text(json.dumps(transaction_input, ensure_ascii=False), encoding="utf-8")
        transaction_script = _SCRIPTS_DIR.parent.parent / "edit-issue" / "scripts" / "edit_issue_txn.py"
        completed = subprocess.run(
            [sys.executable, str(transaction_script), "--input-file", str(input_path.relative_to(repo_root))],
            capture_output=True,
            text=True,
            shell=False,
            cwd=str(repo_root),
            timeout=60,
        )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {"status": "failed_no_mutation"}
        return result if isinstance(result, dict) else {"status": "failed_no_mutation"}

    def fresh_checks(current_issue: dict) -> dict:
        injected = callbacks.get("fresh_checks")
        if injected is not None:
            return injected(current_issue)
        readiness = candidate_readiness(current_issue.get("body", ""))
        with contextlib.redirect_stdout(io.StringIO()):
            preflight_result, _ = run_preflight(
                issue_number=issue_number,
                repo=repo,
                anchor_comment_urls=[anchor_url],
                known_context=known_context,
                consume_contract_patch_plan=False,
            )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8", delete=False) as handle:
            handle.write(current_issue.get("body", ""))
            review_body_path = Path(handle.name)
        temporary_paths.append(review_body_path)
        review_script = _SCRIPTS_DIR.parent.parent / "review-issue" / "scripts" / "check_issue_contract.py"
        review = subprocess.run(
            [sys.executable, str(review_script), "--file", str(review_body_path), "--json"],
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
        )

        # Execute the canonical Allowed Paths grammar rather than treating a
        # successful contract review as a substitute for the path gate.
        allowed_paths_status = "unavailable"
        try:
            import importlib.util

            baseline_path = (
                _SCRIPTS_DIR.parent.parent / "issue-contract-review" / "scripts" / "baseline_vc_preflight.py"
            )
            gate_path = _SCRIPTS_DIR.parent.parent / "pr-review-judge" / "scripts" / "allowed_paths_review_gate.py"
            baseline_spec = importlib.util.spec_from_file_location("post_update_allowed_paths_baseline", baseline_path)
            gate_spec = importlib.util.spec_from_file_location("post_update_allowed_paths_gate", gate_path)
            if (
                baseline_spec is not None
                and baseline_spec.loader is not None
                and gate_spec is not None
                and gate_spec.loader is not None
            ):
                baseline_module = importlib.util.module_from_spec(baseline_spec)
                gate_module = importlib.util.module_from_spec(gate_spec)
                baseline_spec.loader.exec_module(baseline_module)
                gate_spec.loader.exec_module(gate_module)
                allowed_paths = baseline_module.extract_allowed_paths(current_issue.get("body", ""))
                normalized_paths = [
                    gate_module.AllowedPathsMatcher.normalize_allowed_pattern(path) for path in allowed_paths
                ]
                if allowed_paths and all(isinstance(path, str) and path for path in normalized_paths):
                    allowed_paths_status = "pass"
                else:
                    allowed_paths_status = "failed"
        except Exception:
            allowed_paths_status = "unavailable"

        # Validate the exact privileged command profile selected by the
        # explicit source lane.  This checks the canonical registry/policy
        # grammar, not a prose assertion from the directive.
        permission_profile_status = "unavailable"
        try:
            from command_registry import render_command

            policy_path = _find_repo_root() / "scripts" / "agent-guards" / "skill_runtime_command_policy.py"
            policy_spec = importlib.util.spec_from_file_location("post_update_runtime_policy", policy_path)
            if policy_spec is not None and policy_spec.loader is not None:
                policy_module = importlib.util.module_from_spec(policy_spec)
                policy_spec.loader.exec_module(policy_module)
                context = known_context if isinstance(known_context, dict) else {}
                human_urls = _normalize_comment_url_set(context.get(_HUMAN_CONTEXT_COMMENT_URLS_FIELD))
                profile = (
                    "contract_update.run.with_human_context"
                    if human_urls is not None and anchor_url in human_urls
                    else "contract_update.run.with_anchor"
                )
                # render_command proves the child argv; the policy parses the
                # matching executor argv, including the required lane flag.
                render_command(profile, {"issue_number": issue_number, "repo": repo, "anchor_comment_url": anchor_url})
                executor_argv = [
                    "uv", "run", "python3", "scripts/agent-guards/skill_runtime_exec.py",
                    "--command-id", profile, "--issue-number", str(issue_number),
                    "--repo", repo, "--anchor-comment-url", anchor_url,
                ]
                if profile == "contract_update.run.with_human_context":
                    executor_argv.extend(["--human-context-comment-url", anchor_url])
                parsed = policy_module.parse_exact_skill_runtime_contract_update_anchor_command(
                    shlex.join(executor_argv), str(_find_repo_root())
                )
                permission_profile_status = "pass" if parsed is not None else "failed"
        except Exception:
            permission_profile_status = "unavailable"

        # The rerun must retain the exact source binding in a sidecar emitted
        # by the fresh preflight.  A missing sidecar or a lane mismatch is a
        # failed runtime-evidence gate, never an implicit success.
        runtime_evidence_status = "unavailable"
        try:
            provenance_path = (
                _issue_artifact_dir(_find_repo_root(), issue_number) / "refinement_preflight_provenance_v1.json"
            )
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            source = provenance.get("runtime_evidence", {}).get("source", {})
            context = known_context if isinstance(known_context, dict) else {}
            expected_source_kind = _resolve_scope_delta_source_kind(
                anchor_url,
                human_context_comment_urls=context.get(_HUMAN_CONTEXT_COMMENT_URLS_FIELD),
                agent_report_comment_urls=context.get(_AGENT_REPORT_COMMENT_URLS_FIELD),
            )
            if (
                source.get("comment_url") == anchor_url
                and source.get("source_kind") == expected_source_kind
                and provenance.get("runtime_evidence", {}).get("tested_head_sha")
            ):
                runtime_evidence_status = "pass"
            else:
                runtime_evidence_status = "failed"
        except Exception:
            runtime_evidence_status = "unavailable"
        return {
            "preflight": preflight_result.get("status"),
            "review": "approve" if review.returncode == 0 else "needs_fix" if review.returncode == 1 else "unavailable",
            "readiness": readiness.get("status"),
            "allowed_paths": allowed_paths_status,
            "permission_profile": permission_profile_status,
            "runtime_evidence": runtime_evidence_status,
        }

    normalized_anchor = dict(anchor_payload)
    normalized_anchor["html_url"] = anchor_url
    normalized_anchor["source_body_sha256"] = f"sha256:{_sha256(anchor_body)}"
    try:
        return run_trusted_anchor_iteration_zero(
            repo=repo,
            issue_number=issue_number,
            issue=issue,
            anchor=normalized_anchor,
            anchor_body=anchor_body,
            patch_plan=contract_patch_plan,
            candidate_readiness=candidate_readiness,
            fetch_current=fetch_current,
            apply_transaction=apply_transaction,
            fresh_checks=fresh_checks,
            allowed_path_deltas=_validated_allowed_path_deltas,
            scope_delta_status=(
                "approved_by_trusted_anchor" if _validated_allowed_path_deltas else None
            ),
            reflected_checker=_reflected_checker,
            patch_plan_producer_available=patch_plan_producer_available,
        )
    finally:
        for path in temporary_paths:
            try:
                path.unlink()
            except OSError:
                pass


def _satisfied_trusted_directive_noop_patch_plan(
    *,
    plan: dict[str, Any],
    issue: dict[str, Any],
    repo: str,
    issue_number: int,
    anchor_url: str,
    anchor_payload: dict[str, Any],
    anchor_body: str,
    known_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any] | None:
    """Reconstruct only a proven-satisfied trusted directive as a no-op.

    The planner intentionally omits a patch plan after it sees that a
    previously applied directive is already present.  Contract-update mode
    must not turn that safe replay into a failed mutation phase.  This helper
    is deliberately narrower than a generic missing-plan fallback: it accepts
    only the planner's ``no_scope_signal`` outcome, then independently derives
    explicit trusted operations and requires the section-aware builder to
    report no body change.
    """
    sidecar = plan.get("scope_signal_guard_decision_v2")
    if not isinstance(sidecar, dict):
        return None
    raw_signal = sidecar.get("raw_signal")
    if not isinstance(raw_signal, dict) or raw_signal.get("triggered") is not False:
        return None
    if raw_signal.get("reason_code") != "no_scope_signal":
        return None

    try:
        from scope_signal_delta import (
            build_contract_patch_plan_v1,
            build_section_aware_candidate_body,
            derive_contract_patch_operations,
            normalize_trusted_anchor_iteration_zero,
        )
    except ImportError:
        return None

    evidence = _build_scope_delta_authority_evidence(
        comment_payload=anchor_payload,
        comment_body=anchor_body,
        repo=repo,
        issue_number=issue_number,
        anchor_url=anchor_url,
        captured_at=_now_iso(),
        human_context_comment_urls=(
            known_context.get(_HUMAN_CONTEXT_COMMENT_URLS_FIELD)
            if isinstance(known_context, dict)
            else None
        ),
        agent_report_comment_urls=(
            known_context.get(_AGENT_REPORT_COMMENT_URLS_FIELD)
            if isinstance(known_context, dict)
            else None
        ),
    )
    if (
        not isinstance(evidence, dict)
        or evidence.get("confidence") != "explicit"
        or evidence.get("source_kind") != "issue_comment"
    ):
        return None
    if evidence.get("boundary_flags"):
        return None
    normalized = normalize_trusted_anchor_iteration_zero(
        repo=repo,
        issue_number=issue_number,
        anchor=anchor_payload,
        source_body=anchor_body,
    )
    if not normalized.get("accepted"):
        return None
    operations = derive_contract_patch_operations([evidence])
    if not operations:
        return None
    candidate = build_section_aware_candidate_body(
        body=issue.get("body", ""),
        operations=operations,
        source_identity=normalized["source_identity"],
    )
    if candidate.get("changed"):
        return None
    return build_contract_patch_plan_v1(
        target_issue_number=issue_number,
        base_issue_body_sha256=_sha256(issue.get("body", "")),
        source_evidence=[evidence],
        operations=operations,
    )


def run_preflight(
    issue_number: int,
    repo: str,
    anchor_comment_urls: list[str],
    fixture_path: Optional[Path] = None,
    known_context: Optional[dict] = None,
    now: Optional[str] = None,
    consume_contract_patch_plan: bool = False,
    contract_update_callbacks: Optional[dict[str, Any]] = None,
    investigation_evidence_transport_path: "Optional[Path]" = None,
    enable_main_drift_live_readback: bool = False,
) -> tuple[dict, int]:
    """
    Main preflight logic.

    Returns (result_dict, exit_code).
    Writes artifacts and prints compact stdout.

    Artifact write guarantee: stdout and disk are written from the same final
    result dict (no post-write mutation). This ensures AC6 failure-path consistency.
    """
    repo_root = _find_repo_root()
    blockers: list[str] = []
    planner_exit_code: Optional[int] = None
    planner_fail_closed: Optional[bool] = None
    planner_fail_closed_reason_codes: list[str] = []
    required_sections: list[str] = []
    required_contract_keys: list[str] = []
    rewrite_constraints: Optional[dict[str, Any]] = None
    planner_input_dict: Optional[dict] = None
    raw_snapshot: Optional[dict] = None
    anchor_payload_for_consumer: Optional[dict] = None
    anchor_body_for_consumer: Optional[str] = None
    anchor_url_for_consumer: Optional[str] = None
    contract_update_handoff: Optional[dict[str, Any]] = None
    # #2048 Scope Delta: True when the trusted-anchor consumer detected a
    # full_rewrite_required disposition (approved scope reframe, empty
    # operations[], not yet reflected in the current body). Overrides
    # next_action to issue_editor_required and keeps this transition out of
    # the ordinary contract-update success/failure blocker path.
    contract_update_route_issue_editor_required: bool = False

    # --- Load data (fixture or live gh) ---
    if fixture_path is not None:
        # Fixture mode: load pre-fetched snapshot
        try:
            fixture_raw = fixture_path.read_text(encoding="utf-8")
            fixture_data = json.loads(fixture_raw)
        except Exception as exc:
            result = _build_result(
                status="environment_failure",
                issue_number=issue_number,
                repo=repo,
                planner_exit_code=None,
                planner_fail_closed=None,
                next_action="fix_environment",
                must_read=[],
                do_not_read=[],
                commands=[],
                blockers=[f"FIXTURE_LOAD_ERROR: {exc}"],
                planner_fail_closed_reason_codes=[],
                required_sections=[],
                required_contract_keys=[],
                rewrite_constraints=None,
                artifacts={},
                hashes={},
            )
            print(_build_compact_stdout(result))
            return result, EXIT_ENVIRONMENT_FAILURE

        # Validate fixture input against input schema (fail-closed on unknown input)
        input_schema = _load_schema("refinement_preflight_input.schema.json")
        if input_schema is not None:
            is_valid, schema_errors = _validate_with_schema(fixture_data, input_schema)
            if not is_valid:
                err_detail = "; ".join(schema_errors)
                return _emit_failure_result(
                    repo_root=repo_root,
                    issue_number=issue_number,
                    repo=repo,
                    status="blocked",
                    next_action="human_judgment_required",
                    blockers=[BLOCKER_INPUT_SCHEMA_INVALID, f"input_schema_errors: {err_detail}"],
                    planner_fail_closed_reason_codes=[],
                    required_sections=[],
                    required_contract_keys=[],
                    rewrite_constraints=None,
                )

        issue = fixture_data.get("issue", {})
        comments = fixture_data.get("comments", [])
        fixture_anchor_comments = fixture_data.get("anchor_comments", [])
        fixture_anchor_urls = fixture_data.get("anchor_comment_urls", anchor_comment_urls)

        # Use fixture anchor data for structural validation
        active_anchor_urls = fixture_anchor_urls or anchor_comment_urls
        fixture_comment_lookup = fixture_anchor_comments if fixture_anchor_comments else comments
        known_context = known_context or fixture_data.get("known_context")
        now = now or fixture_data.get("now")
    else:
        # Live mode: fetch from GitHub
        issue, err = _fetch_issue(repo, issue_number)
        if issue is None:
            blockers.append(BLOCKER_GH_FAILURE)
            return _emit_failure_result(
                repo_root=repo_root,
                issue_number=issue_number,
                repo=repo,
                status="environment_failure",
                next_action="fix_environment",
                blockers=blockers,
                planner_fail_closed_reason_codes=[],
                required_sections=[],
                required_contract_keys=[],
                rewrite_constraints=None,
            )

        comments, err = _fetch_issue_comments(repo, issue_number)
        if comments is None:
            blockers.append(BLOCKER_GH_FAILURE)
            return _emit_failure_result(
                repo_root=repo_root,
                issue_number=issue_number,
                repo=repo,
                status="environment_failure",
                next_action="fix_environment",
                blockers=blockers,
                planner_fail_closed_reason_codes=[],
                required_sections=[],
                required_contract_keys=[],
                rewrite_constraints=None,
            )

        active_anchor_urls = anchor_comment_urls
        fixture_comment_lookup = None

    anchor_comment_state: Optional[dict[str, Any]] = None
    anchor_comment_feedback: Optional[dict[str, Any]] = None
    anchor_comment_ids: set[str] = set()

    # --- Anchor comment structural validation ---
    if active_anchor_urls:
        sorted_urls, anchor_blockers = _validate_anchor_comments_batch(
            active_anchor_urls,
            repo,
            issue_number,
            fixture_comments=fixture_comment_lookup,
        )
        if anchor_blockers:
            blockers.extend(anchor_blockers)
            return _emit_failure_result(
                repo_root=repo_root,
                issue_number=issue_number,
                repo=repo,
                status="blocked",
                next_action="human_judgment_required",
                blockers=blockers,
                planner_fail_closed_reason_codes=[],
                required_sections=[],
                required_contract_keys=[],
                rewrite_constraints=None,
            )

        if sorted_urls:
            anchor_url = sorted_urls[0]
            parsed_anchor = _parse_anchor_comment_url(anchor_url)
            comment_id = parsed_anchor.get("comment_id")
            comment_payload = None
            if fixture_comment_lookup is not None:
                for item in fixture_comment_lookup:
                    if str(item.get("id")) == str(comment_id):
                        comment_payload = item
                        break
            else:
                comment_payload, err = _fetch_single_comment(repo, comment_id)
                if comment_payload is None:
                    blockers.append(BLOCKER_GH_FAILURE)
                    return _emit_failure_result(
                        repo_root=repo_root,
                        issue_number=issue_number,
                        repo=repo,
                        status="environment_failure",
                        next_action="fix_environment",
                        blockers=blockers,
                        planner_fail_closed_reason_codes=[],
                        required_sections=[],
                        required_contract_keys=[],
                        rewrite_constraints=None,
                    )

            anchor_comment_state, anchor_errors = _build_anchor_comment_state(
                anchor_url=anchor_url,
                comment=comment_payload,
                issue_number=issue_number,
                captured_at=now or _now_iso(),
            )
            if anchor_errors:
                blockers.extend(anchor_errors)
                return _emit_failure_result(
                    repo_root=repo_root,
                    issue_number=issue_number,
                    repo=repo,
                    status="blocked",
                    next_action="human_judgment_required",
                    blockers=blockers,
                    planner_fail_closed_reason_codes=[],
                    required_sections=[],
                    required_contract_keys=[],
                    rewrite_constraints=None,
                )

            anchor_comment_ids.add(str(comment_payload["id"]))
            anchor_payload_for_consumer = dict(comment_payload)
            anchor_body_for_consumer = anchor_comment_state["snapshot"]
            anchor_url_for_consumer = anchor_url
            anchor_comment_feedback = {
                "url": anchor_comment_state["url"],
                "preliminary_classification": anchor_comment_state["preliminary_classification"],
                "final_classification": anchor_comment_state["final_classification"],
                "classification_reason": anchor_comment_state["classification_reason"],
                "verified_claims": anchor_comment_state["verified_claims"],
                "unresolved_claims": anchor_comment_state["unresolved_claims"],
                "scope_impact": anchor_comment_state["scope_impact"],
                "requires_fact_check": anchor_comment_state["requires_fact_check"],
            }

            # --- Classify ANCHOR_SCOPE_REFRAME_V1 and build scope_delta_decision ---
            scope_delta_decision = _classify_anchor_scope_reframe(
                comment_payload=comment_payload,
                anchor_body=anchor_comment_state["snapshot"],
                repo=repo,
                issue_number=issue_number,
                anchor_url=anchor_url,
            )
            # Propagate to known_context so planner sees anchor_reframe context
            _kc = dict(known_context) if known_context else {}
            # The iteration-zero consumer must cross-check normalized anchor
            # evidence against the live repository even though planner input
            # intentionally omits raw issue URLs.
            _kc["repo"] = repo
            # NOTE(PR #1973 P0-1 fix_delta): anchor_reframe is intentionally
            # NOT set here from the pre-route `scope_delta_decision`. It is
            # computed further below, AFTER `_apply_multi_turn_candidate_route()`
            # runs, from the FINAL routed status -- otherwise it can go stale
            # / contradict the routed decision (split-brain).
            _kc["anchor_comment_url"] = anchor_url
            _kc["anchor_comment_hash"] = scope_delta_decision.get("anchor_comment_hash", "")
            _kc["scope_delta_decision"] = scope_delta_decision

            # --- #1891: anchor_context.py pure analyzer (segment + candidates) ---
            # anchor_context.py has no GitHub API client of its own (AC8); it
            # only consumes the already-fetched anchor_comment.snapshot body
            # built above. Computed here -- immediately before the
            # _build_scope_delta_authority_evidence() call below -- so both
            # this call and the multi-turn candidate route that follows it
            # share the same segment/candidate outputs at a single call site
            # (Issue #1891 In Scope: "_build_scope_delta_authority_evidence()
            # に anchor_context.py segment + candidates の出力を渡す").
            _anchor_body_for_context = anchor_comment_state["snapshot"]
            _segments_result = None
            _candidates_result = None
            if anchor_context is not None:
                _segments_result = anchor_context.segment_body(_anchor_body_for_context)
                _candidates_result = anchor_context.extract_candidates(_anchor_body_for_context)
                _kc["source_fetch_complete"] = bool(_anchor_body_for_context)
                _kc["source_hash_verified"] = _sha256(_anchor_body_for_context) == scope_delta_decision.get(
                    "anchor_comment_hash"
                )
                _kc["source_ranges_covered"] = anchor_context.compute_source_ranges_covered(
                    _segments_result["segments"], _segments_result["line_count"]
                )
            else:  # pragma: no cover - defensive fallback when import fails
                _kc["source_fetch_complete"] = bool(_anchor_body_for_context)
                _kc["source_hash_verified"] = False
                _kc["source_ranges_covered"] = False

            # --- #1323: build freeform SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 ---
            # (independent of the structured ANCHOR_SCOPE_REFRAME_V1 payload
            # above, so explicit human-review directives in freeform review
            # comments -- e.g. Issue #1270 -- are not dropped just because
            # they carry no machine-formatted reframe marker.)
            # #2053 AC2: a structured ANCHOR_SCOPE_REFRAME_V1 payload that
            # WAS present but invalid/stale/wrong-target must never be
            # reinterpreted as freeform authority evidence from the same
            # comment body -- that would be a downgrade fallback around
            # structured schema validation. "No structured payload at all"
            # remains the legitimate freeform lane (unchanged).
            if _structured_anchor_payload_present_but_invalid(scope_delta_decision):
                _scope_delta_authority_evidence = None
            else:
                _scope_delta_authority_evidence = _build_scope_delta_authority_evidence(
                    comment_payload=comment_payload,
                    comment_body=anchor_comment_state["snapshot"],
                    repo=repo,
                    issue_number=issue_number,
                    anchor_url=anchor_url,
                    captured_at=now or _now_iso(),
                    human_context_comment_urls=(
                        known_context.get(_HUMAN_CONTEXT_COMMENT_URLS_FIELD)
                        if isinstance(known_context, dict)
                        else None
                    ),
                    agent_report_comment_urls=(
                        known_context.get(_AGENT_REPORT_COMMENT_URLS_FIELD)
                        if isinstance(known_context, dict)
                        else None
                    ),
                    segments_result=_segments_result,
                    candidates_result=_candidates_result,
                )
            if _scope_delta_authority_evidence is not None:
                _kc["scope_delta_authority_evidence"] = [_scope_delta_authority_evidence]

            # --- #1891 AC4: route to human judgment on genuine multi-turn
            # ambiguity, using the same _segments_result / _candidates_result
            # this call site just fetched and forwarded above. ---
            _kc["scope_delta_decision"] = _apply_multi_turn_candidate_route(
                _kc["scope_delta_decision"],
                _segments_result,
                _candidates_result,
                integrity_predicates={
                    "source_fetch_complete": _kc.get("source_fetch_complete"),
                    "source_hash_verified": _kc.get("source_hash_verified"),
                    "source_ranges_covered": _kc.get("source_ranges_covered"),
                },
            )

            # PR #1973 (OWNER REQUEST_CHANGES, P0-1): anchor_reframe is
            # computed from the FINAL routed status (post-route), so it never
            # contradicts `_kc["scope_delta_decision"]["status"]`.
            _kc["anchor_reframe"] = _kc["scope_delta_decision"].get("status") == "approved_by_trusted_anchor"

            # --- #1891 iteration 2 (PR #1923 OWNER REQUEST_CHANGES): the
            # multi-turn fail_closed route must actually reach `blockers`
            # (the list `_apply_exit_code_mapping()` consumes), not merely
            # live inside known_context where the planner never reads it. ---
            _routed_scope_delta_decision = _kc["scope_delta_decision"]
            if (
                isinstance(_routed_scope_delta_decision, dict)
                and _routed_scope_delta_decision.get("status") == "fail_closed"
                and _routed_scope_delta_decision.get("reason")
                in ANCHOR_MULTI_TURN_FAIL_CLOSED_REASONS
            ):
                # PR #2171 fix_delta (P0-1, OWNER adversarial review):
                # membership in ANCHOR_MULTI_TURN_FAIL_CLOSED_REASONS (not
                # an exact match on a single reason string) is what makes
                # the integrity-unconfirmed forced-blocking route above
                # actually reach `blockers` / the final exit code.
                blockers.append(BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED)
                # PR #2171 fix_delta (P0-1, OWNER adversarial review): the
                # freeform SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 built earlier
                # (before the multi-turn route could downgrade the
                # not_applicable pre-route decision to a blocking
                # fail_closed) must not survive into the final
                # known_context once the decision is confirmed blocking --
                # otherwise a fail-closed integrity gap would still leave
                # freeform authority evidence available to a downstream
                # consumer.
                _kc.pop("scope_delta_authority_evidence", None)

            known_context = _kc

    # --- Build raw snapshot (for artifact) ---
    raw_snapshot = {
        "schema_version": "raw_issue_snapshot/v1",
        "fetched_at": now or _now_iso(),
        "issue_number": issue_number,
        "repo": repo,
        "issue": issue,
        "comments": comments,
    }
    if anchor_comment_state is not None:
        raw_snapshot["anchor_comment"] = anchor_comment_state

    # --- Run repair pass before planner (Issue #889) ---
    # repair_issue_contract runs dry-run to report defects; the repaired body is
    # NOT fed to the planner (the planner always receives the original Issue body).
    # repair_result is included in the preflight output as repair_diagnostics (BLOCKER 1 fix).
    _repair_result = _invoke_repair(issue.get("body", "") or "")

    # PR #2202 review fix (P0-2): capture the repair_action.apply provenance
    # fields (source lane, run identity, live updatedAt, source refs digest)
    # at THIS producer run's own capture time, so a downstream
    # repair_action.apply consumer can bind against them instead of silently
    # seeing null/None (the schema already carries these under
    # `repair_action.*`; this producer previously never populated them).
    _repair_captured_at = now or _now_iso()
    _repair_original_updated_at = issue.get("updatedAt") if isinstance(issue, dict) else None
    _repair_source_lane = _determine_repair_source_lane(anchor_url_for_consumer, known_context)
    _repair_source_refs_digest_value = _repair_source_refs_digest(_repair_source_lane, anchor_url_for_consumer)
    _repair_preflight_run_identity_value = _repair_preflight_run_identity(
        issue_number=issue_number,
        repo=repo,
        original_body_sha256=_sha256(issue.get("body", "") or ""),
        captured_at=_repair_captured_at,
        source_lane=_repair_source_lane,
    )

    # Issue #2016 iteration-3 P1-3 (OWNER adversarial review): a repair
    # subprocess-level failure must dominate a *later* planner failure.
    # Established precedence (see _apply_exit_code_mapping docstring):
    #   environment_failure > blocked > needs_fix > warn > pass
    # Evaluated here -- before the planner is invoked at all -- so a
    # compound failure (repair subprocess broken AND planner exit 2) is
    # recorded as environment_failure via BLOCKER_REPAIR_ENVIRONMENT_FAILURE
    # (an env_blocker in _apply_exit_code_mapping) rather than surfacing
    # only as a planner-only blocked/human_judgment_required result that
    # silently drops the repair failure. The block below (after the planner
    # invocation) re-derives the same reason for the non-early-return path
    # and is a no-op here when this already fired.
    _repair_invocation_error_early = (
        _repair_result.get("error") if isinstance(_repair_result, dict) else "repair_result_not_object"
    )
    if _repair_invocation_error_early and BLOCKER_REPAIR_ENVIRONMENT_FAILURE not in blockers:
        blockers.append(BLOCKER_REPAIR_ENVIRONMENT_FAILURE)
        blockers.append(f"repair_invocation_error:{_repair_invocation_error_early}")

    # --- #1891 AC6: heavy mutation gate ---
    # Only evaluated when the caller (main thread / orchestrator) has stated
    # an intended mutation_category in known_context; this never fires for
    # ordinary body-improvement preflight runs that do not carry one.
    if known_context and known_context.get("mutation_category"):
        known_context = dict(known_context)
        _heavy_mutation_gate = _classify_heavy_mutation_gate(
            mutation_category=known_context.get("mutation_category"),
            scope_delta_decision=known_context.get("scope_delta_decision"),
        )
        known_context["heavy_mutation_gate"] = _heavy_mutation_gate
        # #1891 iteration 2 (PR #1923 OWNER REQUEST_CHANGES): the heavy
        # mutation gate must actually reach `blockers`, not merely live
        # inside known_context where the planner never reads it.
        if _heavy_mutation_gate.get("fail_closed") is True:
            blockers.append(BLOCKER_HEAVY_MUTATION_FAIL_CLOSED)

    # #2086 P0 fix_delta (iteration 3, Blocker 1): the ONLY real production
    # entrypoint through which `investigation_derived_path_literals` may
    # reach the planner is this validated, bound transport manifest -- see
    # `_validate_investigation_evidence_transport()`. A caller that hand-sets
    # `known_context["investigation_derived_path_literals"]` directly (the
    # pre-fix_delta test-only shortcut) is no longer what the CANONICAL CLI
    # -> `skill_runtime_exec.py` -> registry-rendered argv -> this subprocess
    # chain does; `main()` only ever threads this manifest path through, and
    # this is where it is validated and merged into known_context.
    if investigation_evidence_transport_path is not None:
        _validated_literals, _transport_reason = _validate_investigation_evidence_transport(
            investigation_evidence_transport_path,
            repo_root=repo_root,
            issue_number=issue_number,
            repo=repo,
            anchor_url=anchor_url_for_consumer,
            base_issue_body_sha256=_sha256(issue.get("body", "") or ""),
            git_head_sha=_git_head_sha(repo_root),
        )
        if _validated_literals is not None:
            known_context = dict(known_context) if known_context else {}
            known_context["investigation_derived_path_literals"] = _validated_literals
        else:
            blockers.append(f"investigation_evidence_transport_rejected:{_transport_reason}")

    # --- Invoke planner ---
    known_context = _ensure_scope_signal_delta_input(
        repo_root=repo_root,
        issue=issue,
        raw_snapshot=raw_snapshot,
        known_context=known_context,
        issue_number=issue_number,
        repo=repo,
    )
    # Issue #2102 fix_delta (iteration 4, Blocker 4): populate
    # known_context["main_drift"] from a live git readback (see
    # `build_live_main_drift_known_context()` above) so
    # `plan_refinement_loop.py`'s `_refinement_main_drift_decision()` is
    # actually reachable in production, not just from hand-injected test
    # `known_context`. Gated behind `enable_main_drift_live_readback`
    # (default False, opt-in) rather than unconditional in every live-mode
    # call: many existing callers configure a real `origin` remote URL in
    # environments with no network access to it (or, worse, one that IS
    # reachable but unrelated to the local repo state under test), and an
    # unconditional live `git fetch` there would be a silent, surprising
    # side effect on every ordinary preflight invocation. Fixture-mode
    # invocations (`fixture_path is not None`) never run this regardless
    # of the flag; an explicitly-supplied `known_context["main_drift"]`
    # (fixture or caller override) is never overwritten.
    if (
        enable_main_drift_live_readback
        and fixture_path is None
        and _main_drift_classifier_probe is not None
        and not (known_context and "main_drift" in known_context)
    ):
        _live_main_drift = build_live_main_drift_known_context(
            repo_root=repo_root,
            issue_body=issue.get("body", "") or "",
        )
        if _live_main_drift is not None:
            known_context = dict(known_context) if known_context else {}
            known_context["main_drift"] = _live_main_drift
    planner_input_dict = _build_planner_input(
        issue,
        comments,
        known_context,
        anchor_comment_feedback=anchor_comment_feedback,
        anchor_comment_ids=anchor_comment_ids,
        now=now,
    )
    # #1677 AC4: join a previously-persisted scope-rollup artifact (if any)
    # into the planner input so ISSUE_EXECUTION_DECISION_V1 reflects known
    # collisions instead of always defaulting to 'selected'.
    _scope_rollup_plan = _load_scope_rollup_artifact(repo_root, issue_number)
    planner_input_dict = _join_scope_rollup_into_planner_input(planner_input_dict, _scope_rollup_plan)
    plan, planner_exit_code, planner_stderr, planner_stdout_raw = _invoke_planner(
        planner_input_dict, repo_root=repo_root, issue_number=issue_number
    )

    if plan is None:
        # Planner invocation failed
        if planner_exit_code == 2:
            blockers.append(BLOCKER_PLANNER_INVALID_INPUT)
        else:
            blockers.append(BLOCKER_PLANNER_INTERNAL_ERROR)

        # --- Blocker 3: failure classification sidecar ---
        failure_cls = classify_planner_failure(
            exit_code=planner_exit_code,
            stdout=planner_stdout_raw,
            stderr=planner_stderr,
            script_path=PLANNER_SCRIPT,
            python_executable=sys.executable,
        )

        try:
            _cls_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
            _cls_dir.mkdir(parents=True, exist_ok=True)
            (_cls_dir / "planner_failure_classification_v1.json").write_text(
                json.dumps(failure_cls, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
            )
        except Exception:
            pass

        # --- Blocker 1 (failure path): provenance sidecar ---
        try:
            _anchor_url = anchor_comment_urls[0] if anchor_comment_urls else ""
            _status_str_prov, _ = _apply_exit_code_mapping(planner_exit_code, None, blockers)
            _prov = build_provenance(
                repo=repo,
                issue_number=issue_number,
                anchor_comment_url=_anchor_url,
                planner_input=planner_input_dict,
                raw_snapshot=raw_snapshot,
                wrapper_exit_code=EXIT_ENVIRONMENT_FAILURE,
                wrapper_status=_status_str_prov,
                blockers=blockers,
                stderr=planner_stderr or "",
                repo_root=repo_root,
                plan=None,
                result_next_action="fix_environment",
            )
            write_provenance_artifact(repo_root, issue_number, _prov)
        except Exception:
            pass

        status_str, _ = _apply_exit_code_mapping(planner_exit_code, None, blockers)
        if planner_exit_code == 3:
            status_str = "environment_failure"
        return _emit_failure_result(
            repo_root=repo_root,
            issue_number=issue_number,
            repo=repo,
            status=status_str,
            next_action="fix_environment" if status_str == "environment_failure" else "human_judgment_required",
            blockers=blockers,
            planner_fail_closed_reason_codes=[BLOCKER_PLANNER_INTERNAL_ERROR],
            required_sections=[],
            required_contract_keys=[],
            rewrite_constraints=_build_safe_rewrite_constraints([], []),
            planner_exit_code=planner_exit_code,
            planner_fail_closed=True,
            planner_input=planner_input_dict,
            raw_snapshot=raw_snapshot,
        )

    if consume_contract_patch_plan:
        sidecar = plan.get("scope_signal_guard_decision_v2")
        authority = sidecar.get("scope_delta_authority") if isinstance(sidecar, dict) else None
        patch_plan = authority.get("contract_patch_plan") if isinstance(authority, dict) else None
        # PR #2057 OWNER review (iteration 4, blocker 3 / P0-1 residual):
        # `authority` (the freeform SCOPE_DELTA_AUTHORITY_EVIDENCE_V1
        # producer's `classify_scope_delta_authority()` result) is `None`
        # in two distinct cases that must NOT be conflated: (a) the
        # freeform evidence builder (`_build_scope_delta_authority_
        # evidence()`, invoked earlier in this same `run_preflight()` call
        # for this same anchor comment) itself failed -- structural anchor
        # URL mismatch or a `scope_signal_delta` parser/import failure --
        # even though the STRUCTURED `ANCHOR_SCOPE_REFRAME_V1` parser
        # succeeded (this branch only runs once that structured parser has
        # already validated a non-empty `allowed_path_deltas`); or (b) the
        # evidence builder succeeded and `classify_scope_delta_authority()`
        # ran to completion but reached an ordinary business decision that
        # is not `contract_update_required` (e.g. `human_escalation` for a
        # purely-structured, non-freeform-directive anchor comment -- the
        # common, legitimate case this branch was originally written for).
        # `known_context["scope_delta_authority_evidence"]` is only set
        # when the evidence builder succeeded (see the `_kc[...] = [...]`
        # assignment above in this function), so its presence distinguishes
        # (a) -- genuine "patch-plan producer unavailable", fail closed to
        # `invalid` -- from (b), left untouched (`True`).
        _patch_plan_producer_available = (
            isinstance(known_context, dict)
            and "scope_delta_authority_evidence" in known_context
        )
        if (
            not isinstance(patch_plan, dict)
            and anchor_payload_for_consumer is not None
            and anchor_body_for_consumer is not None
            and anchor_url_for_consumer is not None
        ):
            patch_plan = _satisfied_trusted_directive_noop_patch_plan(
                plan=plan,
                issue=issue,
                repo=repo,
                issue_number=issue_number,
                anchor_url=anchor_url_for_consumer,
                anchor_payload=anchor_payload_for_consumer,
                anchor_body=anchor_body_for_consumer,
                known_context=known_context,
            )
        if (
            not isinstance(patch_plan, dict)
            and anchor_payload_for_consumer is not None
            and anchor_body_for_consumer is not None
            and anchor_url_for_consumer is not None
            and _extract_validated_scope_delta_deltas(
                known_context=known_context,
                anchor_url=anchor_url_for_consumer,
                anchor_body=anchor_body_for_consumer,
            )
        ):
            # #2048 Scope Delta: an approved trusted-anchor scope reframe
            # (structured ANCHOR_SCOPE_REFRAME_V1, non-empty
            # allowed_path_deltas) has no operations[] producer of its own --
            # the freeform scope_delta_authority classifier only emits a
            # contract_patch_plan when it can extract an exact "allowed
            # paths" literal (an ambiguous/no-literal directive fails closed
            # to human_escalation instead), and the noop-satisfied fallback
            # above requires a non-empty, already-applied operations[].
            # Without this synthesis an approved scope reframe whose deltas
            # cannot be expressed as a section-bound append would never reach
            # the consumer at all (patch_plan stays None and the mutation
            # phase falls into the unconditional "no safe action" failure
            # branch below) -- the exact #2048 PR review regression this
            # Issue's Scope Delta targets. Synthesize a schema-valid,
            # EMPTY-operations CONTRACT_PATCH_PLAN_V1 (never a new field, an
            # ordinary instance of the existing schema) only when the
            # validated decision is bound to THIS anchor/body -- the
            # consumer boundary re-validates the same binding independently.
            #
            # PR #2057 OWNER review P0-1 (fully resolved via
            # `patch_plan_producer_available`, iteration 4 blocker 3 -- see
            # `consume_trusted_anchor_contract_patch_plan()` above and
            # `test_producer_unavailable_reaches_invalid_disposition_via_
            # production_consumer` / `test_producer_available_default_true_
            # preserves_full_rewrite_required` in
            # `test_preflight_run_with_anchor.py`): this synthesis is
            # additionally gated on `_classify_scope_delta_decision()`
            # returning `"valid"`, which requires the STRUCTURED
            # `ANCHOR_SCOPE_REFRAME_V1` payload (`_classify_anchor_scope_
            # reframe()`, a schema-validating, anchor-URL/hash-bound parser
            # distinct from the FREEFORM `SCOPE_DELTA_AUTHORITY_EVIDENCE_V1`
            # classifier that separately produces `contract_patch_plan` /
            # `human_escalation`) to have explicitly parsed and validated
            # `status: approve_scope_delta`. `operations: []` here reflects
            # that the structured reframe payload carries no operations
            # concept by construction (it conveys `allowed_path_deltas`
            # only) -- it is NOT synthesized merely because "no patch plan
            # exists"; a `contract_patch_plan_operations_not_list` /
            # `invalid_operations_key_missing` classification is never
            # reachable through this branch specifically because
            # `operations` is always populated here.
            from scope_signal_delta import build_contract_patch_plan_v1

            patch_plan = build_contract_patch_plan_v1(
                target_issue_number=issue_number,
                base_issue_body_sha256=_sha256(issue.get("body", "")),
                source_evidence=[],
                operations=[],
            )
        if (
            isinstance(patch_plan, dict)
            and anchor_payload_for_consumer is not None
            and anchor_body_for_consumer is not None
            and anchor_url_for_consumer is not None
        ):
            consumer_result = consume_trusted_anchor_contract_patch_plan(
                repo=repo,
                issue_number=issue_number,
                issue=issue,
                anchor_url=anchor_url_for_consumer,
                anchor_payload=anchor_payload_for_consumer,
                anchor_body=anchor_body_for_consumer,
                contract_patch_plan=patch_plan,
                callbacks=contract_update_callbacks,
                known_context=known_context,
                patch_plan_producer_available=_patch_plan_producer_available,
            )
            contract_update_handoff = _bounded_contract_update_handoff(consumer_result)
            _rewrite_route = (
                consumer_result.get("rewrite_route") if isinstance(consumer_result, dict) else None
            )
            contract_update_route_issue_editor_required = (
                isinstance(_rewrite_route, dict) and _rewrite_route.get("route") == "issue_editor_required"
            )
            if contract_update_route_issue_editor_required:
                # PR #2057 OWNER review P0-2/P1-3: a full_rewrite_required
                # disposition is a legitimate routing outcome, not a failed
                # mutation attempt -- no write was ever attempted
                # (operations[] was empty). `_bounded_contract_update_
                # handoff()` already projects this as the distinct
                # `status: handoff_required` (never `failed`, never
                # `no_change`); this branch intentionally does NOT overwrite
                # it and does NOT append BLOCKER_FAIL_CLOSED -- the dedicated
                # next_action override below carries the signal instead.
                pass
            elif contract_update_handoff.get("status") not in {"applied", "no_change", "rebased"}:
                blockers.append(BLOCKER_FAIL_CLOSED)
        else:
            # The explicit mutation phase has no safe action without a
            # planner-produced and provenance-bound patch plan.
            contract_update_handoff = {
                "status": "failed",
                "writes": 0,
                "iterations": 0,
                "final_readback": "failed",
                "fresh_preflight": "unavailable",
                "fresh_review": "unavailable",
                "fresh_readiness": "unavailable",
            }
            blockers.append(BLOCKER_FAIL_CLOSED)

    # --- Extract planner output fields ---
    fail_closed = plan.get("fail_closed", {})
    planner_fail_closed = fail_closed.get("required", False)

    # Build must_read / do_not_read from planner decisions
    must_read: list[str] = []
    do_not_read: list[str] = []

    decisions = plan.get("decisions", {})
    investigation_policy = decisions.get("investigation_policy", {})
    if investigation_policy.get("required"):
        target_paths = investigation_policy.get("target_paths", [])
        must_read.extend(target_paths)

    # Build commands
    commands = _commands_from_plan(plan, issue_number, repo)

    # Planner blockers
    # #1677 AC12 (PR #1767 owner review, P0-4): actually invoke the shared
    # semantic validator here instead of importing it unused. A missing
    # validator import or a semantically invalid issue_execution_decision
    # blocks the preflight rather than silently passing plan.
    _issue_execution_decision = plan.get("issue_execution_decision")
    if isinstance(_issue_execution_decision, dict):
        if validate_issue_execution_decision is None:
            blockers.append(BLOCKER_ISSUE_EXECUTION_DECISION_VALIDATOR_UNAVAILABLE)
        else:
            _ied_violations = validate_issue_execution_decision(_issue_execution_decision)
            if _ied_violations:
                blockers.append(f"{BLOCKER_ISSUE_EXECUTION_DECISION_INVALID}: " + ", ".join(_ied_violations))

    if planner_exit_code == 2:
        blockers.append(BLOCKER_PLANNER_INVALID_INPUT)
    elif planner_exit_code == 3:
        blockers.append(BLOCKER_PLANNER_INTERNAL_ERROR)
    elif planner_exit_code == 0 and planner_fail_closed:
        blockers.append(BLOCKER_FAIL_CLOSED)
        reason_codes, reason_codes_ok = _as_string_list(
            fail_closed.get("reason_codes", []),
            "planner.fail_closed.reason_codes",
            blockers,
        )
        planner_fail_closed_reason_codes = reason_codes
        blockers.extend(reason_codes)
        if not reason_codes_ok:
            rewrite_constraints = _build_safe_rewrite_constraints([], [])

        rc = fail_closed.get("rewrite_constraints", {})
        if not isinstance(rc, dict):
            blockers.append(f"{BLOCKER_REWRITE_CONSTRAINTS_NON_STRING_PAYLOAD}: rewrite_constraints must be an object")
            rewrite_constraints = _build_safe_rewrite_constraints([], [])
            reason_codes_ok = False
        elif not rc.get("schema_version"):
            rewrite_constraints = _build_safe_rewrite_constraints([], [])
        elif reason_codes_ok:
            if not _ensure_json_serializable(rc, "planner.fail_closed.rewrite_constraints", blockers):
                rewrite_constraints = _build_safe_rewrite_constraints([], [])
                reason_codes_ok = False
            else:
                required_sections, sections_ok = _as_string_list(
                    rc.get("required_sections", []),
                    "planner.fail_closed.rewrite_constraints.required_sections",
                    blockers,
                )
                required_contract_keys, keys_ok = _as_string_list(
                    rc.get("required_contract_keys", []),
                    "planner.fail_closed.rewrite_constraints.required_contract_keys",
                    blockers,
                )
                if sections_ok and keys_ok:
                    rewrite_constraints = rc
                else:
                    rewrite_constraints = _build_safe_rewrite_constraints([], [])
                    reason_codes_ok = False

        # AC7: Invariant check — required_sections/required_contract_keys must match
        # the nested must_add_sections/must_add_contract_keys in rewrite_constraints.
        if rewrite_constraints is not None and reason_codes_ok:
            rc_inner = rewrite_constraints.get("rewrite_constraints", {})
            must_add_sections = rc_inner.get("must_add_sections", [])
            must_add_keys = rc_inner.get("must_add_contract_keys", [])
            if list(required_sections) != list(must_add_sections):
                blockers.append(
                    f"{BLOCKER_REWRITE_CONSTRAINTS_INVARIANT_VIOLATION}: "
                    f"required_sections {required_sections!r} != "
                    f"must_add_sections {must_add_sections!r}"
                )
                rewrite_constraints = _build_safe_rewrite_constraints([], [])
                required_sections = []
                required_contract_keys = []
                reason_codes_ok = False
            elif list(required_contract_keys) != list(must_add_keys):
                blockers.append(
                    f"{BLOCKER_REWRITE_CONSTRAINTS_INVARIANT_VIOLATION}: "
                    f"required_contract_keys {required_contract_keys!r} != "
                    f"must_add_contract_keys {must_add_keys!r}"
                )
                rewrite_constraints = _build_safe_rewrite_constraints([], [])
                required_sections = []
                required_contract_keys = []
                reason_codes_ok = False

        if not reason_codes_ok:
            # Schema-safe deterministic forwarding requires aligned payloads.
            # Non-string / non-list fields are treated as schema violation.
            blockers.append(BLOCKER_PLANNER_FAIL_CLOSED_PAYLOAD_INVALID)

    # --- Write repair artifact and route repair_action.disposition (Issue #2016) ---
    # BLOCKER 1 fix (original): repair_diagnostics is exposed via artifact file (not as a
    # top-level result key, which would violate schema additionalProperties: false).
    #
    # Issue #2016 (OWNER adversarial review P0-2/P0-3): "blocker not added" alone is not
    # enough to signal needs_fix (it would silently become pass/proceed), and the repair
    # subprocess / artifact write path must be fail-closed rather than fail-open. This
    # block therefore branches into exactly one of three outcomes:
    #   1. repair_environment_failure_reason set -> BLOCKER_REPAIR_ENVIRONMENT_FAILURE
    #      (routes to status=environment_failure via env_blockers in
    #      _apply_exit_code_mapping()).
    #   2. repair_needs_fix=True -> no blocker added; repair_action_projection built for
    #      the canonical result + compact stdout (routes to status=needs_fix below).
    #   3. otherwise (human_review_required / informational-but-changed) -> existing
    #      generic repair_diagnostics blocker (routes to status=blocked, unchanged
    #      behavior from before Issue #2016).
    repair_artifact_path: Optional[str] = None
    repair_action_projection: Optional[dict[str, Any]] = None
    repair_needs_fix = False
    repair_environment_failure_reason: Optional[str] = None

    _repair_error = _repair_result.get("error") if isinstance(_repair_result, dict) else "repair_result_not_object"
    _repair_action_raw = _repair_result.get("repair_action") if isinstance(_repair_result, dict) else None
    _repair_changed = _repair_result.get("changed") if isinstance(_repair_result, dict) else None

    artifact_dir_repair = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    try:
        artifact_dir_repair.mkdir(parents=True, exist_ok=True)
        repair_artifact_file = artifact_dir_repair / "repair_diagnostics.json"
        _atomic_write_json(repair_artifact_file, _repair_result)
        # Readback validation (P0-3): only trust the artifact path once the
        # bytes round-trip, matching the existing _atomic_write_json contract
        # used elsewhere (raw_issue_snapshot / planner_input / result).
        _repair_readback = json.loads(repair_artifact_file.read_text(encoding="utf-8"))
        if _repair_readback != _repair_result:
            repair_environment_failure_reason = "repair_diagnostics_artifact_readback_mismatch"
        else:
            repair_artifact_path = str(repair_artifact_file)
    except Exception as exc:
        repair_environment_failure_reason = f"repair_diagnostics_artifact_write_failed:{type(exc).__name__}"

    if repair_environment_failure_reason is None and _repair_error:
        repair_environment_failure_reason = f"repair_invocation_error:{_repair_error}"

    if (
        repair_environment_failure_reason is None
        and isinstance(_repair_result, dict)
        and _repair_result.get("schema") != "repair_issue_contract/v1"
    ):
        repair_environment_failure_reason = "repair_schema_mismatch"

    if repair_environment_failure_reason is None and _repair_changed is True:
        # changed: true with 0 mutating repairs described is a producer/wrapper
        # contract inconsistency, not a legitimate no-repair state.
        if not isinstance(_repair_action_raw, dict) or not _repair_action_raw.get("repair_kinds"):
            repair_environment_failure_reason = "repair_changed_with_no_mutating_repair"

    if (
        repair_environment_failure_reason is None
        and isinstance(_repair_action_raw, dict)
        and _repair_action_raw.get("disposition") == "invalid_payload"
    ):
        repair_environment_failure_reason = "repair_action_invalid_payload"

    if repair_environment_failure_reason is not None:
        if BLOCKER_REPAIR_ENVIRONMENT_FAILURE not in blockers:
            blockers.append(BLOCKER_REPAIR_ENVIRONMENT_FAILURE)
    elif (
        _repair_changed is True
        and isinstance(_repair_action_raw, dict)
        and _repair_action_raw.get("disposition") == "auto_apply_safe"
        and repair_artifact_path is not None
    ):
        _apply_result, _apply_error = _materialize_auto_apply_candidate(
            issue.get("body", "") or "", _repair_result, artifact_dir_repair
        )
        if _apply_error is not None:
            blockers.append(BLOCKER_REPAIR_ENVIRONMENT_FAILURE)
            repair_environment_failure_reason = _apply_error
        else:
            _candidate_body_artifact = str(artifact_dir_repair / "repaired_issue_body.md")
            repair_action_projection = {
                "schema_version": _repair_action_raw.get("schema_version", "repair_action/v1"),
                "policy_version": _repair_action_raw.get("policy_version", "deterministic-issue-repair/v1"),
                "disposition": "auto_apply_safe",
                "original_body_sha256": _repair_action_raw.get("original_body_sha256"),
                "repaired_body_sha256": _repair_action_raw.get("repaired_body_sha256"),
                "diagnostics_artifact": repair_artifact_path,
                "candidate_body_artifact": _candidate_body_artifact,
                "repair_kinds": _repair_action_raw.get("repair_kinds", []),
                "reason_codes": _repair_action_raw.get("reason_codes", []),
                # PR #2202 review fix (P0-2): AC9 provenance-lane fields
                # (previously always absent, silently defeating
                # repair_action.apply's provenance binding).
                "source_lane": _repair_source_lane,
                "preflight_run_identity": _repair_preflight_run_identity_value,
                "original_updated_at": _repair_original_updated_at,
                "source_refs_digest": _repair_source_refs_digest_value,
            }
            repair_needs_fix = True
    elif _repair_changed is True and repair_artifact_path is not None:
        # human_review_required / informational-but-changed / unknown disposition:
        # existing generic blocker behavior (unchanged from before Issue #2016).
        blockers.append(
            json.dumps(
                {
                    "kind": "repair_diagnostics",
                    "message": "repair_issue_contract detected changes: see repair artifact for details",
                    "artifact_path": repair_artifact_path,
                }
            )
        )

    # --- Apply exit code mapping (with plan for warn detection, after all blockers finalized) ---
    _scope_delta_decision_for_exit_mapping = (
        known_context.get("scope_delta_decision") if isinstance(known_context, dict) else None
    )
    status, exit_code = _apply_exit_code_mapping(
        planner_exit_code,
        planner_fail_closed,
        blockers,
        plan=plan,
        scope_delta_decision=_scope_delta_decision_for_exit_mapping,
        repair_needs_fix=repair_needs_fix,
    )

    # Determine next_action
    if status == "pass":
        next_action = "proceed"
    elif status == "needs_fix":
        next_action = "apply_deterministic_repair"
    elif status == "warn":
        next_action = "proceed_with_notes"
    elif status == "blocked":
        next_action = "human_judgment_required"
    else:
        next_action = "fix_environment"

    # #2048 Scope Delta: an approved trusted-anchor scope reframe whose
    # operations[] is empty AND is not yet reflected in the current body
    # (full_rewrite_required) is routed to issue_editor_required regardless
    # of the ordinary status-derived next_action above. This is the ONLY
    # code path that projects `consume_trusted_anchor_contract_patch_plan()`'s
    # `rewrite_route` all the way to the wrapper's stdout / result artifact
    # `next_action` -- the previous #2048 iterations computed
    # `decide_scope_reframe_contract_route()` but never reached this
    # projection because the classification was unreachable from this
    # consumer boundary (see `_extract_validated_scope_delta_deltas()`).
    if contract_update_route_issue_editor_required:
        next_action = "issue_editor_required"

    # --- Compute hashes for byte-stability (after all blockers finalized) ---
    snapshot_text = json.dumps(raw_snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False)
    planner_input_text = json.dumps(planner_input_dict, sort_keys=True, ensure_ascii=False, allow_nan=False)

    # Core result (without artifacts/hashes) for hash computation
    result_core_for_hash = {
        "schema_version": SCHEMA_VERSION_RESULT,
        "status": status,
        "issue_number": issue_number,
        "repo": repo,
        "planner_exit_code": planner_exit_code,
        "planner_fail_closed": planner_fail_closed,
        "next_action": next_action,
        "must_read": sorted(set(must_read)),
        "do_not_read": do_not_read,
        "commands": commands,
        "blockers": blockers,
        "planner_fail_closed_reason_codes": planner_fail_closed_reason_codes,
        "required_sections": required_sections,
        "required_contract_keys": required_contract_keys,
        "rewrite_constraints": rewrite_constraints,
    }
    if contract_update_handoff is not None:
        result_core_for_hash["contract_update"] = contract_update_handoff
    # Issue #2016 iteration-3 P1-2 (OWNER adversarial review): result_core_sha256
    # previously excluded repair_action.schema_version/.policy_version/.disposition/
    # body SHAs/repair_kinds/reason_codes, so those machine-actionable fields could
    # be silently altered without changing the result's integrity hash. Bind a
    # stable, environment-independent projection of them here (explicitly
    # excluding the absolute diagnostics_artifact/candidate_body_artifact paths,
    # which are environment-dependent).
    if isinstance(repair_action_projection, dict):
        result_core_for_hash["repair_action_core"] = {
            "schema_version": repair_action_projection.get("schema_version"),
            "policy_version": repair_action_projection.get("policy_version"),
            "disposition": repair_action_projection.get("disposition"),
            "original_body_sha256": repair_action_projection.get("original_body_sha256"),
            "repaired_body_sha256": repair_action_projection.get("repaired_body_sha256"),
            "repair_kinds": sorted(repair_action_projection.get("repair_kinds", []) or []),
            "reason_codes": sorted(repair_action_projection.get("reason_codes", []) or []),
        }
    result_core_text = json.dumps(result_core_for_hash, sort_keys=True, ensure_ascii=False, allow_nan=False)

    hashes = {
        "raw_issue_snapshot_sha256": _sha256(snapshot_text),
        "planner_input_sha256": _sha256(planner_input_text),
        "result_core_sha256": _sha256(result_core_text),
    }

    artifact_dir = _issue_artifact_dir(repo_root, issue_number)
    artifacts = {
        "raw_issue_snapshot": str(artifact_dir / "raw_issue_snapshot.json"),
        "planner_input": str(artifact_dir / "planner_input.json"),
        "refinement_preflight_result_v1": str(artifact_dir / "refinement_preflight_result_v1.json"),
    }
    # Issue #2016 iteration-3 P1-1: AC6 requires repair diagnostics and the
    # candidate body to be referenceable from ALL THREE of the result
    # schema, the artifact map, and compact stdout. repair_action already
    # carries diagnostics_artifact/candidate_body_artifact; this mirrors
    # those same paths into the canonical artifacts map so consumers that
    # only read `artifacts` (not the nested repair_action block) can still
    # discover them.
    if status == "needs_fix" and isinstance(repair_action_projection, dict):
        if repair_action_projection.get("diagnostics_artifact"):
            artifacts["repair_diagnostics"] = repair_action_projection["diagnostics_artifact"]
        if repair_action_projection.get("candidate_body_artifact"):
            artifacts["repair_candidate_body"] = repair_action_projection["candidate_body_artifact"]
    result = _build_result(
        status=status,
        issue_number=issue_number,
        repo=repo,
        planner_exit_code=planner_exit_code,
        planner_fail_closed=planner_fail_closed,
        next_action=next_action,
        must_read=sorted(set(must_read)),
        do_not_read=do_not_read,
        commands=commands,
        blockers=blockers,
        planner_fail_closed_reason_codes=planner_fail_closed_reason_codes,
        required_sections=required_sections,
        required_contract_keys=required_contract_keys,
        rewrite_constraints=rewrite_constraints,
        artifacts=artifacts,
        hashes=hashes,
        contract_update=contract_update_handoff,
        repair_action=repair_action_projection,
    )
    try:
        _write_artifacts(repo_root, issue_number, raw_snapshot, planner_input_dict, result)
    except Exception as exc:
        result = _build_result(
            status="environment_failure",
            issue_number=issue_number,
            repo=repo,
            planner_exit_code=planner_exit_code,
            planner_fail_closed=planner_fail_closed,
            next_action="fix_environment",
            must_read=sorted(set(must_read)),
            do_not_read=do_not_read,
            commands=commands,
            blockers=[
                *blockers,
                BLOCKER_RESULT_SCHEMA_INVALID,
                f"result_artifact_write_error:{type(exc).__name__}:{str(exc)[:500]}",
            ],
            planner_fail_closed_reason_codes=planner_fail_closed_reason_codes,
            required_sections=required_sections,
            required_contract_keys=required_contract_keys,
            rewrite_constraints=rewrite_constraints,
            artifacts={},
            hashes=hashes,
        )
        exit_code = EXIT_ENVIRONMENT_FAILURE

    # --- Blocker 1 (success path): provenance sidecar ---
    try:
        _anchor_url_prov = anchor_comment_urls[0] if anchor_comment_urls else ""
        _provenance = build_provenance(
            repo=repo,
            issue_number=issue_number,
            anchor_comment_url=_anchor_url_prov,
            planner_input=planner_input_dict,
            raw_snapshot=raw_snapshot,
            wrapper_exit_code=exit_code,
            wrapper_status=result.get("status", "unknown"),
            blockers=blockers,
            stderr=planner_stderr or "",
            repo_root=repo_root,
            plan=plan,
            result_next_action=result.get("next_action", "unavailable"),
        )
        write_provenance_artifact(repo_root, issue_number, _provenance)
    except Exception:
        pass

    # Print compact stdout (no raw body/comments/sentinels) — same result dict
    print(_build_compact_stdout(result))

    return result, exit_code


# ---------------------------------------------------------------------------
# Provenance and failure classification (Issue #1035)
# ---------------------------------------------------------------------------


def _git_head_sha(repo_root: Path) -> str:
    """Return git HEAD SHA or 'unknown' on failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# known_context["main_drift"] live production producer (Issue #2102
# fix_delta, iteration 4, Blocker 4). This is the orchestrator step the
# `known_context["main_drift"] production contract` comment in
# `plan_refinement_loop.py` documents as missing wiring: it performs the
# live git readback (bounded `git fetch` / `rev-parse` / `diff` / `merge-tree
# --write-tree`) and builds the dict that function's docstring requires,
# then `run_preflight()` below merges it into `known_context` before
# `plan_refinement_loop.py` is invoked. Every subprocess call is bounded and
# fails closed to `None` (never a fabricated/partial dict) on any timeout,
# non-zero exit, or missing origin remote.
# ---------------------------------------------------------------------------


def _run_git_readonly_bounded(argv: list, cwd: Path, timeout: int = 20):
    """Bounded, fail-closed `git` readback helper (returns None on any
    timeout/launch failure instead of raising -- callers must treat `None`
    as "cannot confirm", never as a stand-in for a real result)."""
    try:
        return subprocess.run(
            ["git", *argv],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _extract_allowed_paths_from_issue_body(body: str) -> list:
    """Parse the canonical `## Allowed Paths` bullet list the same shape
    `pr_head_replay_publish_exec.py::_allowed_paths()` already parses on the
    implementation-loop side (kept independently here since this module has
    no import boundary into `scripts/agent-ops/`)."""
    marker = "## Allowed Paths"
    if marker not in body:
        return []
    section = body.split(marker, 1)[1].split("\n## ", 1)[0]
    paths = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        candidate = stripped[1:].strip().strip("`")
        if candidate:
            paths.append(candidate)
    return paths


def build_live_main_drift_known_context(
    *,
    repo_root: Path,
    issue_body: str,
    base_ref: str = "main",
    evidence_base_sha: Optional[str] = None,
    allowed_paths_snapshot_base_sha: Optional[str] = None,
    expected_head_sha: Optional[str] = None,
    observed_head_sha: Optional[str] = None,
    expected_old_sha: Optional[str] = None,
    observed_old_sha: Optional[str] = None,
) -> Optional[dict]:
    """Build `known_context["main_drift"]` from a live git readback (Issue
    #2102 fix_delta iteration 4, Blocker 4; CAS-pair defaults corrected in
    fix_delta iteration 5, Blocker A). See the production contract comment
    above `_refinement_main_drift_decision()` in `plan_refinement_loop.py`
    for the exact key contract this satisfies, and
    `route_loop_verdict_v2.classify_main_drift()` for the consumer that
    validates every key below is present and well-formed.

    Fails closed to `None` (never a dict built from partial/stale evidence)
    if the live `origin/<base_ref>` readback, or (when the evidence epoch
    actually differs from the live base) the `git diff` / `git merge-tree
    --write-tree` probes, cannot be completed within their bounded
    timeouts. Callers MUST treat a `None` return as "no main_drift evidence
    available this cycle" -- never as "no drift detected".

    `expected_head_sha`/`observed_head_sha` and `expected_old_sha`/
    `observed_old_sha` are CAS guards `classify_main_drift()` uses to
    reject a *concurrent* mutation of whatever ref this evidence rebind
    will ultimately touch -- they are a distinct concern from the
    `current_base_sha` vs `evidence_base_sha` drift comparison performed
    below. This producer has no independent prior-known state to CAS
    against (it is a read-only diagnostic epoch classification, not the
    mutation itself), so every pair defaults to the SAME just-read live
    value unless a caller supplies an explicit prior expectation to guard
    against a race. Defaulting `expected_old_sha` to `evidence_base_sha`
    while `observed_old_sha` defaulted to `current_base_sha` (the
    pre-fix_delta-5 behavior) made these two CAS pairs mismatch on every
    genuine drift, which caused `classify_main_drift()` to hard_stop with
    `expected_old_cas_mismatch` on all drifted input -- silently defeating
    AC1's scope-clean reconciliation route before it could ever be
    reached in production.
    """
    fetched = _run_git_readonly_bounded(
        ["fetch", "--quiet", "--no-tags", "origin", f"refs/heads/{base_ref}"], repo_root, timeout=20
    )
    if fetched is None or fetched.returncode != 0:
        return None
    current = _run_git_readonly_bounded(["rev-parse", f"origin/{base_ref}"], repo_root, timeout=10)
    if current is None or current.returncode != 0:
        return None
    current_base_sha = current.stdout.strip()
    if not current_base_sha:
        return None

    resolved_evidence_base_sha = evidence_base_sha or current_base_sha
    resolved_allowed_paths_snapshot_base_sha = allowed_paths_snapshot_base_sha or resolved_evidence_base_sha
    resolved_expected_head_sha = expected_head_sha or current_base_sha
    resolved_observed_head_sha = observed_head_sha or resolved_expected_head_sha
    resolved_expected_old_sha = expected_old_sha or current_base_sha
    resolved_observed_old_sha = observed_old_sha or resolved_expected_old_sha

    allowed_paths = _extract_allowed_paths_from_issue_body(issue_body)

    latest_main_net_diff: list = []
    semantic_ambiguity = False
    if resolved_evidence_base_sha != current_base_sha:
        diff = _run_git_readonly_bounded(
            ["diff", "--name-only", resolved_evidence_base_sha, current_base_sha], repo_root, timeout=20
        )
        if diff is None or diff.returncode != 0:
            return None
        latest_main_net_diff = [line for line in diff.stdout.splitlines() if line]

        # Deterministic real-conflict oracle (mirrors
        # `pr_head_replay_publish_exec.py::_merge_tree_conflicts()` on the
        # implementation-loop side, Issue #2102 P1-C): a nonzero exit from
        # the two-ref `git merge-tree --write-tree` form means the merge
        # produced conflicts. This is never a caller-asserted boolean.
        merge_probe = _run_git_readonly_bounded(
            ["merge-tree", "--write-tree", resolved_evidence_base_sha, current_base_sha], repo_root, timeout=20
        )
        if merge_probe is None:
            return None
        semantic_ambiguity = merge_probe.returncode != 0

    return {
        "current_base_sha": current_base_sha,
        "evidence_base_sha": resolved_evidence_base_sha,
        "allowed_paths_snapshot_base_sha": resolved_allowed_paths_snapshot_base_sha,
        "allowed_paths": allowed_paths,
        "latest_main_net_diff": latest_main_net_diff,
        "expected_head_sha": resolved_expected_head_sha,
        "observed_head_sha": resolved_observed_head_sha,
        "expected_old_sha": resolved_expected_old_sha,
        "observed_old_sha": resolved_observed_old_sha,
        "semantic_ambiguity": semantic_ambiguity,
    }


def _git_blob_sha(file_path: Path, repo_root: Path) -> str:
    """Return git blob SHA of a file or 'unknown' on failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "hash-object", str(file_path.resolve())],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _git_head_tree_blob_sha(file_path: Path, repo_root: Path) -> str:
    """Return blob SHA from HEAD tree (git rev-parse HEAD:<relpath>) or 'unknown'."""
    try:
        relpath = file_path.resolve().relative_to(repo_root.resolve())
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", f"HEAD:{relpath}"],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _git_worktree_status(file_path: Path, repo_root: Path) -> str:
    """Return 'git status --short' for a file or 'unknown' on failure."""
    try:
        relpath = file_path.resolve().relative_to(repo_root.resolve())
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--short", "--", str(relpath)],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def build_py_compile_proof(script_path: Path, repo_root: Path) -> dict:
    """Generate PY_SYNTAX_COMPILE_PROOF_V2 artifact for a Python script.

    Compiles the source in-process without materializing a bytecode cache.
    ``python -m py_compile`` is intentionally not used here: it writes a
    ``__pycache__`` entry next to the source even when descendant imports have
    ``PYTHONDONTWRITEBYTECODE`` enabled.  The privileged executor must retain
    source-tree write attribution, so provenance generation cannot create an
    otherwise unauthorized cache file.

    The source is read and compiled as raw ``bytes`` (not a decoded ``str``)
    so that CPython's own PEP 263 encoding-declaration handling (UTF-8 BOM,
    ``# -*- coding: ... -*-`` cookie, and BOM/cookie contradictions) applies
    exactly as it would for a normally-imported module, instead of this
    wrapper silently assuming UTF-8. ``dont_inherit=True`` and ``flags=0``
    ensure this module's own ``from __future__ import annotations`` (and any
    other wrapper-side future statement) is never inherited by the checked
    script. There is no executable ``command`` for this proof: the operation
    is in-process and is described by ``operation_kind`` instead.
    """
    script_realpath = str(script_path.resolve())
    operation_kind = "in_process_compile"
    source_mode = "bytes"
    flags = 0
    dont_inherit = True
    optimize = -1

    try:
        source_bytes = script_path.read_bytes()
        compile(
            source_bytes,
            script_realpath,
            "exec",
            flags=flags,
            dont_inherit=dont_inherit,
            optimize=optimize,
        )
        py_compile_status = "pass"
        stderr_text = ""
    except Exception as exc:
        py_compile_status = "fail"
        stderr_text = str(exc)

    stderr_excerpt = stderr_text[:500]

    return {
        "schema_version": "PY_SYNTAX_COMPILE_PROOF_V2",
        "operation_kind": operation_kind,
        "source_mode": source_mode,
        "flags": flags,
        "dont_inherit": dont_inherit,
        "optimize": optimize,
        "cache_write_expected": False,
        "py_compile_status": py_compile_status,
        "python_version": sys.version,
        "python_executable": sys.executable,
        "git_head_sha": _git_head_sha(repo_root),
        "planner_script_path": str(script_path),
        "planner_script_realpath": script_realpath,
        "planner_script_blob_sha": _git_blob_sha(script_path, repo_root),
        "cwd": str(Path.cwd()),
        "stderr_sha256": _sha256(stderr_text),
        "stderr_excerpt": stderr_excerpt,
    }


def classify_planner_failure(
    exit_code: int,
    stdout: str,
    stderr: str,
    script_path: Optional[Path] = None,
    python_executable: Optional[str] = None,
) -> dict:
    """Classify a planner failure into PLANNER_FAILURE_CLASSIFICATION_V1 taxonomy.

    Categories (mutually exclusive, evaluated in priority order):
      syntax_compile_failure         - SyntaxError detected in stderr / exit != 0
      anchor_or_input_blocked        - exit 2 (invalid input / schema error)
      planner_stdout_non_json        - exit 0 but stdout is not valid JSON
      wrapper_environment_failure    - gh not found / auth / timeout
      planner_runtime_internal_error - exit 3 without SyntaxError
    """
    stderr_str = stderr or ""
    stdout_str = stdout or ""
    is_syntax_error = bool(re.search(r"SyntaxError|py_compile\b", stderr_str))

    if is_syntax_error and exit_code != 0:
        category = "syntax_compile_failure"
    elif exit_code == 2:
        category = "anchor_or_input_blocked"
    elif exit_code == 0:
        try:
            json.loads(stdout_str)
            category = "planner_runtime_internal_error"
        except (json.JSONDecodeError, ValueError):
            category = "planner_stdout_non_json"
    elif exit_code == 3:
        env_keywords = ("not found", "FileNotFoundError", "timeout", "gh_", "gh not")
        if any(kw in stderr_str for kw in env_keywords):
            category = "wrapper_environment_failure"
        else:
            category = "planner_runtime_internal_error"
    else:
        category = "wrapper_environment_failure"

    # Traceback excerpt (SyntaxError only)
    traceback_excerpt = ""
    if category == "syntax_compile_failure":
        lines = stderr_str.splitlines()
        relevant = [
            ln for ln in lines if "SyntaxError" in ln or ln.strip().startswith("File ") or "line " in ln.lower()
        ]
        traceback_excerpt = "\n".join(relevant[:10])

    # JSON decode error (non-JSON stdout only)
    json_decode_error = ""
    if category == "planner_stdout_non_json":
        try:
            json.loads(stdout_str)
        except (json.JSONDecodeError, ValueError) as exc:
            json_decode_error = str(exc)

    script_realpath = str(script_path.resolve()) if script_path else ""

    return {
        "schema_version": "PLANNER_FAILURE_CLASSIFICATION_V1",
        "category": category,
        "exit_code": exit_code,
        "stdout_sha256": _sha256(stdout_str),
        "stderr_sha256": _sha256(stderr_str),
        "stderr_excerpt": stderr_str[:500],
        "json_decode_error": json_decode_error,
        "traceback_excerpt": traceback_excerpt,
        "script_path": str(script_path) if script_path else "",
        "script_realpath": script_realpath,
        "python_executable": python_executable or sys.executable,
        "python_version": sys.version,
    }


def build_provenance(
    repo: str,
    issue_number: int,
    anchor_comment_url: str,
    planner_input: dict,
    raw_snapshot: dict,
    wrapper_exit_code: int,
    wrapper_status: str,
    blockers: list,
    stderr: str,
    repo_root: Path,
    plan: Optional[dict] = None,
    result_next_action: str = "unavailable",
) -> dict:
    """Generate REFINEMENT_PREFLIGHT_PROVENANCE_V1 sidecar artifact.

    Captures the full execution context of a preflight run so that a later
    replay or audit can verify which file/interpreter/commit was used.
    Written to the same artifact directory as the main result but as a
    separate file (``refinement_preflight_provenance_v1.json``) to avoid
    violating the strict ``additionalProperties: false`` result schema.
    """
    planner_script = PLANNER_SCRIPT
    wrapper_script = Path(__file__).resolve()

    py_compile_proof = build_py_compile_proof(planner_script, repo_root)

    planner_input_text = _canonical_json(planner_input)
    raw_snapshot_text = _canonical_json(raw_snapshot)
    stderr_str = stderr or ""
    sidecar = (plan or {}).get("scope_signal_guard_decision_v2")
    authority = sidecar.get("scope_delta_authority") if isinstance(sidecar, dict) else {}
    authority_route = authority.get("route") if isinstance(authority, dict) else {}
    if not isinstance(authority_route, dict):
        # A malformed/scalar route must never suppress the provenance sidecar.
        # Preserve fail-closed semantics by recording no implementation route
        # rather than treating the scalar as an authorization object.
        authority_route = {}
    known_context = planner_input.get("known_context") if isinstance(planner_input, dict) else {}
    evidence_list = (
        known_context.get("scope_delta_authority_evidence")
        if isinstance(known_context, dict)
        else []
    )
    source_evidence = evidence_list[0] if isinstance(evidence_list, list) and evidence_list else {}
    if not isinstance(source_evidence, dict):
        source_evidence = {}
    runtime_evidence = {
        # The provenance sidecar is intentionally additive to the strict
        # preflight-result schema. It binds runtime observations but cannot
        # itself authorize implementation.
        "tested_head_sha": _git_head_sha(repo_root),
        "source": {
            "comment_url": source_evidence.get("comment_url"),
            "comment_id": source_evidence.get("comment_id"),
            "body_sha256": source_evidence.get("body_sha256"),
            "source_kind": source_evidence.get("source_kind"),
        },
        "route": {
            "action": authority_route.get("action"),
            "implementation_allowed": authority_route.get("implementation_allowed"),
            "required_rerun": authority_route.get("next_step"),
        },
        "terminal_event": {
            "wrapper_status": wrapper_status,
            "next_action": result_next_action,
            "implementation_allowed": authority_route.get("implementation_allowed"),
        },
        # The generic preflight cannot claim a #1952-specific profile check
        # has passed. Consumers must execute that validator at the current
        # head before implementation; until then this is an explicit deny.
        "permission_profile_validators": {
            "status": "required_before_implementation",
            "passed": False,
        },
    }

    def _dependency_version(name: str) -> str:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return "not_installed"

    return {
        "schema_version": "REFINEMENT_PREFLIGHT_PROVENANCE_V1",
        "repo": repo,
        "issue_number": issue_number,
        "anchor_comment_url": anchor_comment_url,
        "git_head_sha": _git_head_sha(repo_root),
        "planner_invocation_command": [sys.executable, str(planner_script)],
        "planner_script_path": str(planner_script),
        "planner_script_realpath": str(planner_script.resolve()),
        "planner_script_blob_sha": _git_blob_sha(planner_script, repo_root),
        "planner_head_tree_blob_sha": _git_head_tree_blob_sha(planner_script, repo_root),
        "planner_worktree_status": _git_worktree_status(planner_script, repo_root),
        "wrapper_script_blob_sha": _git_blob_sha(wrapper_script, repo_root),
        "python_executable": sys.executable,
        "python_version": sys.version,
        "dependency_versions": {
            "jsonschema": _dependency_version("jsonschema"),
            "referencing": _dependency_version("referencing"),
        },
        "cwd": str(Path.cwd()),
        "py_compile_status": py_compile_proof["py_compile_status"],
        # Issue #1439 AC12: persist the full PY_SYNTAX_COMPILE_PROOF_V2 proof
        # (schema_version + all required fields), not just the collapsed
        # py_compile_status summary above, so a later reader of the
        # refinement_preflight_provenance_v1.json artifact can verify the
        # full V2 semantics without re-deriving them.
        "python_syntax_compile_proof": py_compile_proof,
        "wrapper_exit_code": wrapper_exit_code,
        "wrapper_status": wrapper_status,
        "blockers": list(blockers),
        "planner_input_sha256": _sha256(planner_input_text),
        "raw_snapshot_sha256": _sha256(raw_snapshot_text),
        "stderr_sha256": _sha256(stderr_str),
        "stderr_excerpt": stderr_str[:500],
        "runtime_evidence": runtime_evidence,
    }


def build_replay_proof(
    live_input: dict,
    fixture_input: dict,
    live_result_status: str,
    fixture_result_status: str,
) -> dict:
    """Generate REFINEMENT_PREFLIGHT_REPLAY_PROOF_V1.

    Compares the SHA256 of the canonical JSON of ``live_input`` (fetched
    from GitHub) against ``fixture_input`` (a saved snapshot).  Identical
    hashes guarantee the classification is deterministic; a mismatch is
    classified as ``input_drift`` so that the caller cannot prematurely
    declare the issue resolved.
    """
    live_sha = _sha256(_canonical_json(live_input))
    fixture_sha = _sha256(_canonical_json(fixture_input))
    input_drift_detected = live_sha != fixture_sha
    results_consistent = live_result_status == fixture_result_status

    if input_drift_detected:
        classification = "input_drift"
    elif results_consistent:
        classification = "replay_consistent"
    else:
        classification = "classification_mismatch"

    return {
        "schema_version": "REFINEMENT_PREFLIGHT_REPLAY_PROOF_V1",
        "live_input_sha256": live_sha,
        "fixture_input_sha256": fixture_sha,
        "input_drift_detected": input_drift_detected,
        "live_result_status": live_result_status,
        "fixture_result_status": fixture_result_status,
        "results_consistent": results_consistent,
        "classification": classification,
    }


def write_provenance_artifact(
    repo_root: Path,
    issue_number: int,
    provenance: dict,
) -> str:
    """Write provenance dict to the artifacts directory and return the path."""
    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    prov_path = artifact_dir / "refinement_preflight_provenance_v1.json"
    prov_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return str(prov_path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic preflight wrapper for issue-refinement-loop.",
        allow_abbrev=False,
    )
    parser.add_argument("--issue-number", type=int, required=True, help="GitHub Issue number (positive int).")
    parser.add_argument("--repo", required=True, help="owner/repo string (must match ^[^/]+/[^/]+$).")
    parser.add_argument(
        "--anchor-comment-url",
        dest="anchor_comment_urls",
        action="append",
        default=[],
        help=(
            "Anchor comment URL to validate. The CLI flag is repeatable, but only "
            "0 or 1 distinct URL is actually supported: passing 2+ distinct "
            "anchor-comment URLs is rejected as ANCHOR_COMMENT_MULTIPLE_UNSUPPORTED "
            "(repeating the identical URL is deduplicated to 1 and is not an "
            "error)."
        ),
    )
    parser.add_argument(
        "--human-context-comment-url",
        dest="human_context_comment_urls",
        action="append",
        default=[],
        help="Explicit human-context issue-comment URL (repeatable).",
    )
    parser.add_argument(
        "--agent-report-comment-url",
        dest="agent_report_comment_urls",
        action="append",
        default=[],
        help="Explicit agent-report issue-comment URL (repeatable; never authorizes a rewrite).",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="Path to fixture JSON (bypasses gh CLI calls).",
    )
    parser.add_argument(
        "--consume-contract-patch-plan",
        action="store_true",
        help="Execute a trusted CONTRACT_PATCH_PLAN_V1 through edit_issue_txn.py.",
    )
    parser.add_argument(
        "--disable-main-drift-live-readback",
        dest="enable_main_drift_live_readback",
        action="store_false",
        default=True,
        help="Issue #2102 fix_delta (iteration 5, Blocker A): the CLI/registry "
        "production entrypoint (command_registry.py's 'preflight.run' family, which "
        "never renders this flag) now performs the bounded live git readback "
        "(fetch/diff/merge-tree against 'origin') to populate known_context['main_drift'] "
        "before invoking the planner BY DEFAULT -- opt-out via this flag, not opt-in. "
        "The prior opt-in default (Blocker 4, iteration 4) never actually enabled this "
        "in production because no registered command_registry.py entry rendered the "
        "old --enable-main-drift-live-readback flag, leaving the producer dead code; "
        "see build_live_main_drift_known_context()'s docstring for the CAS-pair-default "
        "correctness fix (iteration 5) that made this safe to flip. Only the CLI/main() "
        "entrypoint's default changed -- run_preflight()'s own Python-level keyword "
        "default is unchanged (False) so every other in-process caller (e.g. "
        "_apply_contract_patch_plan_v1()'s fresh_checks() post-mutation reverification "
        "helper, which does not have every skill's sibling scripts/ directory on "
        "sys.path in every test harness) keeps its pre-existing behavior unless it "
        "explicitly opts in.",
    )
    parser.add_argument(
        "--investigation-evidence-transport-path",
        type=Path,
        default=None,
        metavar="MANIFEST_JSON_PATH",
        help="#2086 P0 fix_delta (Blocker 1/2): path to a "
        "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest (minted via "
        "--produce-authority-transport / the authority_transport.produce "
        "command_id) carrying a bound, digest-verified read-only-investigation "
        "exact-path inventory. Only ever consulted for the operator-selected "
        "human-context lane; see _validate_investigation_evidence_transport().",
    )
    parser.add_argument(
        "--invocation-id",
        default=None,
        metavar="ID",
        help="#2053: invocation identifier bound into SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 / receipts.",
    )
    parser.add_argument(
        "--git-head-sha",
        default=None,
        metavar="SHA",
        help="#2053: git HEAD sha bound into SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 / receipts "
        "(defaults to the live repo HEAD when omitted).",
    )
    parser.add_argument(
        "--produce-authority-transport",
        type=Path,
        default=None,
        metavar="EVIDENCE_JSON_PATH",
        help="#2053 AC1/AC7: read a SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 (or list) JSON file and emit "
        "an immutable SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest (producer role). Requires "
        "--issue-number, --repo, --invocation-id.",
    )
    parser.add_argument(
        "--consume-authority-transport",
        default=None,
        metavar="ROUTER_RECEIPT_PATH",
        help="#2053 AC9: read a SCOPE_DELTA_ROUTER_RECEIPT_V1 and perform the controlled consumer "
        "role (verify digest, mutate once, readback, fresh rerun), emitting "
        "SCOPE_DELTA_CONSUMPTION_RECEIPT_V1. Requires --issue-number, --repo, --invocation-id, "
        "--git-head-sha.",
    )
    parser.add_argument(
        "--contract-patch-plan-file",
        type=Path,
        default=None,
        metavar="CONTRACT_PATCH_PLAN_JSON_PATH",
        help="#2053 P0 fix-delta (iteration 3): only meaningful with "
        "--consume-authority-transport. Path to a CONTRACT_PATCH_PLAN_V1 JSON file; when "
        "supplied together with --anchor-context-file, the consumer's mutation is delegated "
        "to the real controlled-mutation lane (consume_trusted_anchor_contract_patch_plan -> "
        "edit_issue_txn.py) instead of only writing the local audit artifact.",
    )
    parser.add_argument(
        "--anchor-context-file",
        type=Path,
        default=None,
        metavar="ANCHOR_CONTEXT_JSON_PATH",
        help="#2053 P0 fix-delta (iteration 3): only meaningful with "
        "--consume-authority-transport and --contract-patch-plan-file. Path to a JSON file "
        "with keys `issue`, `anchor_url`, `anchor_payload`, `anchor_body` "
        "(and optionally `known_context`).",
    )

    parser.add_argument(
        "--apply-repair-action",
        type=Path,
        default=None,
        metavar="PREFLIGHT_RESULT_JSON_PATH",
        help="Issue #2039 AC8/AC11: read a previously-produced preflight result "
        "JSON artifact (FD-based secure open), resolve the exactly-one mutation "
        "intent, and -- only when repair_action.disposition == auto_apply_safe -- "
        "dispatch the repaired body through edit_issue_txn.py --input-file "
        "(never a raw `gh issue edit` call). Requires --issue-number, --repo.",
    )

    args = parser.parse_args(argv)

    # #2053: producer / consumer dedicated CLI modes -- bypass the full
    # gh-backed preflight pipeline entirely (parallel sibling entries, same
    # pattern as --fixture; see command_registry.py "authority_transport.produce"
    # / "authority_transport.consume").
    if args.produce_authority_transport is not None:
        _repo_root = _find_repo_root()
        try:
            evidence_payload = json.loads(args.produce_authority_transport.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"status": "environment_failure", "reason_code": "malformed_json", "error": str(exc)}))
            sys.exit(EXIT_ENVIRONMENT_FAILURE)
        git_head_sha = args.git_head_sha or _git_head_sha(_repo_root)
        result, error = generate_authority_transport_manifest(
            evidence=evidence_payload,
            issue_number=args.issue_number,
            repo=args.repo,
            invocation_id=args.invocation_id or "",
            git_head_sha=git_head_sha,
            repo_root=_repo_root,
        )
        if result is None:
            print(json.dumps({"status": "environment_failure", "reason_code": "write_failure", "error": error}))
            sys.exit(EXIT_ENVIRONMENT_FAILURE)
        print(json.dumps({"status": "ok", **result}, ensure_ascii=False, indent=2, sort_keys=True))
        sys.exit(0)

    if args.apply_repair_action is not None:
        # PR #2202 review fix (P0-2 bullet 3): the production CLI entrypoint
        # previously never constructed `expected_provenance` at all -- only
        # test code injected it -- so the provenance-binding check inside
        # run_repair_action_apply() was effectively unreachable in
        # production beyond its own always-on repo/issue self-consistency
        # check. Derive it here from the SAME artifact's own
        # producer-emitted provenance fields (secure-read via the same
        # FD-based reader run_repair_action_apply() itself uses) plus the
        # live repo/issue identity this invocation was given, so a
        # mid-flight artifact replacement between this read and
        # run_repair_action_apply()'s own internal read is still caught as
        # a mismatch rather than silently accepted.
        _cli_repo_root = _find_repo_root()
        _cli_expected_provenance: "dict | None" = None
        try:
            _cli_pf_path = Path(str(args.apply_repair_action))
            if not _cli_pf_path.is_absolute():
                _cli_pf_path = _cli_repo_root / _cli_pf_path
            _cli_text, _ = secure_read_repair_apply_artifact(_cli_pf_path, root=_cli_repo_root)
            _cli_parsed = json.loads(_cli_text)
            _cli_repair_action = _cli_parsed.get("repair_action") if isinstance(_cli_parsed, dict) else None
            if isinstance(_cli_repair_action, dict):
                _cli_expected_provenance = {
                    "repo": args.repo,
                    "issue_number": args.issue_number,
                    "preflight_run_identity": _cli_repair_action.get("preflight_run_identity"),
                    "original_body_sha256": _cli_repair_action.get("original_body_sha256"),
                    "repair_action_core_sha256": _repair_action_core_sha256(_cli_repair_action),
                }
        except (RepairApplySecureOpenError, json.JSONDecodeError, OSError):
            _cli_expected_provenance = None

        result = run_repair_action_apply(
            repo=args.repo,
            issue_number=args.issue_number,
            preflight_result_path=str(args.apply_repair_action),
            expected_provenance=_cli_expected_provenance,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        outcome = result.get("mutation_outcome")
        # PR #2202 review fix-delta (P0-5): a real fresh-validation failure
        # (phase == "fresh_validation" with a non-null failure_code) must
        # exit non-zero even when the mutation itself genuinely applied --
        # mutation_outcome alone previously hid this failure behind exit 0.
        if result.get("phase") == "fresh_validation" and result.get("failure_code"):
            sys.exit(EXIT_ENVIRONMENT_FAILURE)
        if outcome in {"applied", "no_change"}:
            sys.exit(0)
        if outcome == "not_attempted":
            sys.exit(EXIT_BLOCKED)
        sys.exit(EXIT_ENVIRONMENT_FAILURE)

    if args.consume_authority_transport is not None:
        _repo_root = _find_repo_root()
        git_head_sha = args.git_head_sha or _git_head_sha(_repo_root)
        contract_patch_plan_arg = None
        anchor_context_arg = None
        if args.contract_patch_plan_file is not None and args.anchor_context_file is not None:
            try:
                contract_patch_plan_arg = json.loads(
                    args.contract_patch_plan_file.read_text(encoding="utf-8")
                )
                anchor_context_arg = json.loads(args.anchor_context_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(
                    json.dumps(
                        {"status": "environment_failure", "reason_code": "malformed_json", "error": str(exc)}
                    )
                )
                sys.exit(EXIT_ENVIRONMENT_FAILURE)
        receipt = consume_authority_transport(
            router_receipt_path=args.consume_authority_transport,
            issue_number=args.issue_number,
            repo=args.repo,
            invocation_id=args.invocation_id or "",
            git_head_sha=git_head_sha,
            repo_root=_repo_root,
            contract_patch_plan=contract_patch_plan_arg,
            anchor_context=anchor_context_arg,
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
        sys.exit(0 if receipt.get("status") == "ok" else EXIT_ENVIRONMENT_FAILURE)

    # --- argparse input validation (blocked / exit 2 on contract violation) ---
    input_errors: list[str] = []

    if args.issue_number is not None and args.issue_number <= 0:
        input_errors.append(f"--issue-number must be a positive int, got {args.issue_number}")

    if not _REPO_PATTERN.match(args.repo):
        input_errors.append(f"--repo must match ^[^/]+/[^/]+$, got {args.repo!r}")

    for url in args.anchor_comment_urls:
        if not url.startswith(_GITHUB_URL_PREFIX):
            input_errors.append(f"--anchor-comment-url must start with {_GITHUB_URL_PREFIX!r}, got {url!r}")

    if input_errors:
        # Build minimal blocked result for argparse validation failure
        _repo_root = _find_repo_root()
        err_detail = "; ".join(input_errors)
        result = _build_result(
            status="blocked",
            issue_number=args.issue_number or 0,
            repo=args.repo or "",
            planner_exit_code=None,
            planner_fail_closed=None,
            next_action="human_judgment_required",
            must_read=[],
            do_not_read=[],
            commands=[],
            blockers=[BLOCKER_INVALID_ARGS, f"arg_errors: {err_detail}"],
            planner_fail_closed_reason_codes=[],
            required_sections=[],
            required_contract_keys=[],
            rewrite_constraints=None,
            artifacts={},
            hashes={},
        )
        print(_build_compact_stdout(result))
        sys.exit(EXIT_BLOCKED)

    cli_known_context = None
    if args.human_context_comment_urls or args.agent_report_comment_urls:
        cli_known_context = {
            _HUMAN_CONTEXT_COMMENT_URLS_FIELD: args.human_context_comment_urls,
            _AGENT_REPORT_COMMENT_URLS_FIELD: args.agent_report_comment_urls,
        }

    _, exit_code = run_preflight(
        issue_number=args.issue_number,
        repo=args.repo,
        anchor_comment_urls=args.anchor_comment_urls,
        fixture_path=args.fixture,
        known_context=cli_known_context,
        consume_contract_patch_plan=args.consume_contract_patch_plan,
        investigation_evidence_transport_path=args.investigation_evidence_transport_path,
        enable_main_drift_live_readback=args.enable_main_drift_live_readback,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
