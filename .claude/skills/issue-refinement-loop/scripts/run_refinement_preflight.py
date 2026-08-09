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
        return {
            "status": "fail_closed",
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


def _invoke_planner(planner_input: dict) -> tuple[dict | None, int, str, str]:
    """
    Invoke plan_refinement_loop.py via subprocess.run([sys.executable, ...], shell=False).

    Returns (plan_dict, exit_code, stderr_text, raw_stdout).
    plan_dict is None on JSON parse failure.
    """
    input_json = json.dumps(planner_input, ensure_ascii=False, allow_nan=False)

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
    """
    raw_status = consumer_result.get("status")
    states = consumer_result.get("states")
    state = states.get("contract_update", {}).get("status") if isinstance(states, dict) else None
    iterations = consumer_result.get("iterations", 0)
    fresh = consumer_result.get("fresh_checks")
    if not isinstance(fresh, dict):
        fresh = {}
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
    return {
        "status": status,
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

            if (
                original_status == "fail_closed"
                and original_reason == "no_anchor_scope_reframe_v1_payload"
                and integrity_confirmed
            ):
                updated = dict(scope_delta_decision)
                updated["anchor_context_candidate_count"] = len(candidates)
                updated["anchor_context_marked_segment_count"] = len(marked_segments)
                updated["implementation_go"] = False
                updated["status"] = "warn"
                updated["reason"] = "multi_turn_anchor_context_trusted_owner_advisory"
                updated["latest_owner_turn"] = latest_owner_turn
                return updated

            # Any other original status/reason (schema_invalid, wrong_repo,
            # wrong_issue_number, untrusted_author_association, or
            # unconfirmed retrieval integrity) is returned unchanged -- the
            # multi-turn advisory route never masks a distinct integrity
            # problem.
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
    confidence = classify_directive_confidence(comment_body, markers)
    boundary_flags_map = detect_boundary_flags(comment_body)
    boundary_flag_names = [name for name, value in boundary_flags_map.items() if value]
    source_kind = _resolve_scope_delta_source_kind(
        anchor_url,
        human_context_comment_urls=human_context_comment_urls,
        agent_report_comment_urls=agent_report_comment_urls,
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
                == "multi_turn_anchor_context_requires_human_judgment"
            ):
                blockers.append(BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED)

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

    # --- Invoke planner ---
    known_context = _ensure_scope_signal_delta_input(
        repo_root=repo_root,
        issue=issue,
        raw_snapshot=raw_snapshot,
        known_context=known_context,
        issue_number=issue_number,
        repo=repo,
    )
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
    plan, planner_exit_code, planner_stderr, planner_stdout_raw = _invoke_planner(planner_input_dict)

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
            )
            contract_update_handoff = _bounded_contract_update_handoff(consumer_result)
            if contract_update_handoff.get("status") not in {"applied", "no_change", "rebased"}:
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
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
