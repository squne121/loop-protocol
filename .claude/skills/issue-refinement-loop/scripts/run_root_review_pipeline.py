#!/usr/bin/env python3
"""
run_root_review_pipeline.py - ROOT_REVIEW_PIPELINE_RESULT_V1

Root-owned producer I/O for the issue-refinement-loop review step (Issue #2049).

Problem this closes: the `issue-reviewer` custom agent
(`.codex/agents/issue-reviewer.toml`) declares `default_permissions =
"loop-protocol-readonly"` (read-only) while its historical
`developer_instructions` also required it to fetch the live Issue body,
write temp files, and persist a full artifact under
`.claude/artifacts/issue-refinement-loop/<N>/` -- a producer I/O
responsibility a read-only agent cannot legitimately carry out. This module
moves ALL of that producer I/O (live body fetch, body SHA pin, checker
execution, artifact persistence, child-stdout classification, and
readback/verdict-identity gating of the "final review" step) into a single
root-owned script that the orchestrator (main thread / `issue-refinement-loop`
SKILL.md Step 2), not the read-only agent, invokes directly.

The `issue-reviewer` agent's role after this change is strictly advisory: it
reads the already-pinned merged review result this script produces and
returns an `ISSUE_REVIEW_RESULT_COMPACT_V1` verdict on stdout. It performs no
I/O of its own.

CLI subcommands:

    produce             Fetch + pin live body, run checkers, and act as the
                         SOLE producer of BOTH the ROOT_REVIEW_PIPELINE_RESULT_V1
                         canonical artifact AND the ISSUE_REVIEW_RESULT_COMPACT_V1
                         compact envelope + its persisted artifact (PR #2135
                         human REQUEST_CHANGES iteration-3 P0-1: previously
                         only the checker/readiness/merge artifacts were
                         root-owned and the compact envelope was left to a
                         "read-only" child that could not legitimately write
                         it). The compact envelope's rendered stdout lines are
                         returned to the caller as `compact_result.stdout_lines`
                         so the orchestrator can hand them to the read-only
                         `issue-reviewer` child as an explicit, already-produced,
                         read-only input (the child relays them verbatim; it
                         never invokes `compact_review_result.py` itself).
    classify-child-stdout
                         Classify the issue-reviewer child agent's raw stdout
                         text via the SAME canonical validator
                         (`validate_review_compact_output.validate_review_compact_output()`)
                         the orchestrator's Step 2 routing table uses -- not a
                         separately reimplemented simplified classifier (PR
                         #2135 iteration-3 P0-2). 0-byte stdout classifies as
                         `reviewer_transport_failure` / `empty_input` (Issue
                         #2049 AC4/AC5); non-empty-but-malformed stdout now
                         also classifies as `reviewer_transport_failure` /
                         `malformed_output` instead of being silently accepted
                         as "ok".
    readback            Verify the persisted `REVIEWER_COMPACT_ARTIFACT_V2`
                         artifact -- i.e. `produce`'s
                         `verified_transport_artifact` /
                         `compact_result.artifact_path`, NEVER the top-level
                         `artifact_path` / `full_review_artifact`
                         (`REVIEW_ISSUE_RESULT_V1`) -- via full delegation to
                         `reviewer_transport.verify_artifact()`: dir-fd
                         anchored, component-wise no-follow open on EVERY
                         intermediate path component (not just the leaf),
                         1 MiB cap, strict JSON (parser-level rejection of
                         NaN/Infinity via `reviewer_transport.strict_json_loads`,
                         not a raw substring search -- PR #2135 iteration-3
                         P1-5), SHA-256 raw-byte match, independently
                         caller-supplied repo/issue/body-SHA/invocation/attempt
                         binding match, full `REVIEW_ISSUE_RESULT_V1` schema
                         validation of `semantic_result`, and verdict
                         identity (Issue #2049 AC7; Issue #2242 OWNER
                         adversarial review Blockers 2/3/4,
                         https://github.com/squne121/loop-protocol/pull/2246#issuecomment-5328161000).
    gate-final-review   Decide whether the "final review" (remote Issue body
                         update) may proceed: only after readback verifies
                         (Issue #2049 AC10). Takes the SAME
                         `--artifact-root`/`--artifact-relative` +
                         `--expected-*` arguments as `readback` (Issue #2242
                         Blocker 2: every expected binding value is
                         caller-supplied, never re-derived from the artifact
                         itself).
    check-agent-contract
                         Static check that a read-only agent's
                         developer_instructions does not carry a workspace
                         write requirement (reused by
                         test_issue_reviewer_contract_static.py, Issue #2049
                         AC9).

Persistence (Issue #2049 AC3, PR #2135 iteration-3 P1-3): both
`persist_to_canonical_artifact_directory()` and the compact-envelope artifact
this module now also persists reuse `compact_review_result._atomic_write()`
(the hardened `mkstemp()` + 0600 + symlink-recheck primitive already used by
`compact_review_result.py`, added by PR #1907) instead of a separately
reimplemented weaker writer, and include a run-unique (`pid` + `uuid4`)
filename component so two concurrent sessions processing the same Issue in
the same second cannot collide on either the temp or the final artifact path.

Intermediate files (Issue #2049 AC-adjacent, PR #2135 iteration-3 P2-6): all
invocation-private intermediate files (`body_file`, `*.review_result.json`,
`*.readiness_result.json`, `*.merged_review_result.json`) are created inside a
`tempfile.TemporaryDirectory()` for the lifetime of a single `produce` call
and removed automatically when that call returns (success or error); only the
canonical artifact(s) explicitly persisted via
`persist_to_canonical_artifact_directory()` survive the call.

Architecture delta relative to #1875 (PR #2135 iteration-3 P1-4): #1875
removed replay/digest/persistent-state machinery to keep the loop bounded on
live Issue body + a minimal verdict, and explicitly avoided adding new
schemas/digests/receipts/state machines or gating review on artifact
freshness/validity. This module's persisted `ROOT_REVIEW_PIPELINE_RESULT_V1`
/ compact artifacts remain a deliberate, narrow exception scoped to Issue
#2049's own AC3/AC7/AC10 (proving root-owned producer I/O and a final-review
gate keyed on a *freshly regenerated-this-call* artifact, not a stale
long-lived receipt): every artifact this module persists is produced and
consumed within the SAME `produce` invocation's live-body fetch, never
carried over from a prior run, and `gate_final_review()` only ever reads back
the artifact this same call just wrote. Consumers: `issue-refinement-loop`
SKILL.md Step 2 (orchestrator) and the `issue-reviewer` read-only child
(input only, never a writer). No other skill/orchestrator step depends on
this schema. If Issue #2049's AC7/AC10 wording itself needs to change to fold
this back into #1875's stale-tolerant minimal-harness model, that is a
separate Issue-contract decision, not one this PR makes unilaterally.

Exit codes: 0 = ok, 1 = producer/validation error, 2 = input/environment
error, 3 = human_judgment_required.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "ROOT_REVIEW_PIPELINE_RESULT_V1"
SCHEMA_VERSION = "1"

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_REVIEW_ISSUE_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"
_ISSUE_CONTRACT_REVIEW_SCRIPTS = (
    _REPO_ROOT / ".claude" / "skills" / "issue-contract-review" / "scripts"
)
_CANONICAL_ARTIFACT_DIR = Path(".claude/artifacts/issue-refinement-loop")

_SHA256_PREFIXED_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# ---------------------------------------------------------------------------
# Sibling-module imports (Issue #2049 PR #2135 iteration-3 P0-1/P0-2/P1-5):
# this module is the SOLE producer of the compact envelope end-to-end and the
# SOLE classifier of child stdout, so it must call the SAME canonical
# functions `compact_review_result.py` / `validate_review_compact_output.py`
# already define -- not reimplement a second, weaker copy of either. Both
# sibling scripts live in this same `scripts/` directory; loaded via
# `sys.path` insertion (matching the existing test-suite convention in
# `tests/test_compact_review_result.py`), not subprocess, so this module can
# call their pure functions directly without a second process boundary.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from compact_review_result import (  # noqa: E402
    _atomic_write as _compact_atomic_write,
)
from validate_review_compact_output import (  # noqa: E402
    validate_review_compact_output as _canonical_validate_review_compact_output,
)
import reviewer_transport as _reviewer_transport  # noqa: E402

# Issue #2165 P1-1 (OWNER 2026-08-15 REQUEST_CHANGES): import
# `contract_readiness_check.py` as a module (not merely subprocess it) so
# this file's `CHECK_ISSUE_CONTRACT_TIMEOUT_SECONDS` /
# `CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS` /
# `MERGE_READINESS_TIMEOUT_SECONDS` -- the SAME three subprocess budgets
# that dominate a deterministic checker attempt's wall time -- can derive
# the per-attempt/total deadline this module passes explicitly to
# `reviewer_transport.run_reviewer_transport()`, instead of that transport
# module guessing a number independently of the budgets THIS module owns
# (the OWNER-flagged 300s-vs-310s arithmetic break).
if str(_ISSUE_CONTRACT_REVIEW_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ISSUE_CONTRACT_REVIEW_SCRIPTS))
import contract_readiness_check as _contract_readiness_check  # noqa: E402

# Issue #2207 AC8: the SAME canonical VC plan (`baseline_vc_preflight.py`) /
# budget-formula (`contract_readiness_check.py`) functions those modules
# use internally to derive their OWN dynamic timeouts, imported here so
# THIS module can derive an invocation-local `ReviewBudget` from the SAME
# pinned body and pass it explicitly to
# `reviewer_transport.run_reviewer_transport()` -- instead of relying on
# reviewer_transport.py's generic module-level fallback constants
# (`PER_ATTEMPT_DEADLINE_SECONDS` / `TOTAL_DEADLINE_SECONDS`), which remain
# UNCHANGED by this wiring.
_VerificationBudgetExceedsPolicyError = _contract_readiness_check.VerificationBudgetExceedsPolicyError
_derive_review_budget = _contract_readiness_check.derive_review_budget
import baseline_vc_preflight as _baseline_vc_preflight  # noqa: E402

_compute_canonical_vc_plan = _baseline_vc_preflight.compute_canonical_vc_plan

# Issue #2054 AC8: `reviewer_transport.py` is the V2 contract SSOT. This
# module no longer imports the retired V1 `compact_review_result()` renderer
# (`compact_review_result.py`'s CLI/pure-function producer is retired --
# see that module's docstring). `classify_child_stdout()` (via
# `validate_review_compact_output.py`, itself delegating internally to
# `reviewer_transport.validate_compact_v2()`), `readback_persisted_artifact()`,
# and `produce_compact_result()` below all resolve to this SAME V2 module,
# so this file is not a second, competing producer pipeline (Issue #2054
# Scope Delta).


# ---------------------------------------------------------------------------
# Body fetch + SHA pin (root-owned; Issue #2049 AC1)
# ---------------------------------------------------------------------------


def sha256_of(body: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def fetch_and_pin_live_body(
    issue_number: int, repo: str, *, timeout_seconds: int = 15
) -> tuple[str | None, str | None, str | None]:
    """Fetch the live Issue body exactly once and pin its SHA-256.

    Returns (body, body_sha256, error_code). `body_sha256` is None iff `body`
    is None. This is the single source of the pinned body handed to every
    downstream checker in this pipeline run, so two checkers can never
    silently observe two different live body snapshots (the TOCTOU gap this
    root-owned pipeline closes).
    """
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "body"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return None, None, "gh_timeout"
    except OSError:
        return None, None, "gh_other_error"

    if result.returncode != 0:
        stderr = result.stderr.lower()
        if "not authenticated" in stderr or "authentication failed" in stderr:
            return None, None, "gh_auth_failed"
        if "not found" in stderr or "could not resolve" in stderr:
            return None, None, "gh_repo_not_found"
        return None, None, "gh_other_error"

    try:
        body = json.loads(result.stdout).get("body")
    except json.JSONDecodeError:
        return None, None, "gh_json_parse_error"

    if body is None:
        return None, None, "gh_missing_body"

    return body, sha256_of(body), None


def write_pinned_body_tempfile(body: str, *, dir: str | None = None) -> str:
    """Persist the pinned body to a scoped temp file (root-owned I/O).

    `dir` defaults to a `tmp/` directory under the repo root (back-compat
    for direct callers). PR #2135 human REQUEST_CHANGES iteration-3 P2-6:
    `_cmd_produce` now always passes an explicit `dir` pointing inside a
    per-invocation `tempfile.TemporaryDirectory()` so this (and the sibling
    `*.review_result.json` / `*.readiness_result.json` /
    `*.merged_review_result.json` intermediate files derived from its path)
    are removed automatically when that invocation completes, instead of
    accumulating indefinitely under `tmp/`.

    Returns the temp file path. When `dir` is not supplied, the caller is
    responsible for cleanup (unchanged legacy behavior).
    """
    if dir is None:
        tmp_dir = _REPO_ROOT / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        dir = str(tmp_dir)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, suffix=".md", dir=dir, prefix="root_review_pipeline_body_"
    ) as tmp:
        tmp.write(body)
        return tmp.name


# ---------------------------------------------------------------------------
# Checker execution (root-owned; Issue #2049 AC2)
# ---------------------------------------------------------------------------


# Issue #2165 P1-1: named constants for the three sequential subprocess
# budgets a single deterministic checker attempt executes. Kept small
# (check_issue_contract's own real usage is sub-second) except where the
# budget genuinely must absorb VC execution
# (`CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS`, derived below from
# `contract_readiness_check.py`'s own derived constant, which in turn
# derives from `baseline_vc_preflight.py`'s per-VC-command cap).
CHECK_ISSUE_CONTRACT_TIMEOUT_SECONDS = 30
CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS = _contract_readiness_check.CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS
MERGE_READINESS_TIMEOUT_SECONDS = 30


def run_check_issue_contract(
    body_file: str, *, timeout_seconds: int = CHECK_ISSUE_CONTRACT_TIMEOUT_SECONDS
) -> tuple[dict | None, int, str | None]:
    """Run `check_issue_contract.py --file <body_file> --json` and parse stdout."""
    script_path = _REVIEW_ISSUE_SCRIPTS / "check_issue_contract.py"
    cmd = [sys.executable, str(script_path), "--file", body_file, "--json"]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return None, -1, "timeout"
    try:
        return json.loads(completed.stdout), completed.returncode, None
    except json.JSONDecodeError:
        return None, completed.returncode, "malformed_json"


def run_contract_readiness_check(
    body_file: str, *, mode: str = "execute", timeout_seconds: float = CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS
) -> tuple[dict | None, int, str | None]:
    """Run `contract_readiness_check.py --body-file <body_file> --mode <mode>`.

    Issue #2165 P1-1: `timeout_seconds` defaults to
    `contract_readiness_check.CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS`
    (imported, not re-derived here) so this wrapper's timeout is always
    DERIVED from -- and therefore always exceeds -- that module's own
    internal worst-case budget (`validate_issue_body.py` subprocess timeout
    + `baseline_vc_preflight.py` aggregate wrapper timeout + margin). This
    removes the previous drift hazard where two independently hand-picked
    numbers (250 here, ~230s inner ceiling there) could silently invert.

    Issue #2207 OWNER P0-1 (PR #2221 REQUEST_CHANGES): uses
    `baseline_vc_preflight.run_subprocess_with_cooperative_supervisor()`
    instead of `subprocess.run(timeout=...)` -- the latter sends SIGKILL
    directly to `contract_readiness_check.py` on timeout, bypassing that
    process's own cooperative SIGTERM handling (which itself needs to run
    to cooperatively reap `baseline_vc_preflight.py`'s VC descendants) and
    orphaning VC process groups several levels down. `timeout_seconds` is
    `float` (not `int`) so callers under test can inject sub-second
    deadlines (Issue #2207 OWNER P1-2 item 7).
    """
    script_path = _ISSUE_CONTRACT_REVIEW_SCRIPTS / "contract_readiness_check.py"
    cmd = [sys.executable, str(script_path), "--body-file", body_file, "--mode", mode]
    supervised = _baseline_vc_preflight.run_subprocess_with_cooperative_supervisor(
        cmd, timeout_seconds=timeout_seconds
    )
    if supervised.timed_out:
        return None, -1, "timeout"
    try:
        return json.loads(supervised.stdout), supervised.returncode, None
    except json.JSONDecodeError:
        return None, supervised.returncode, "malformed_json"


def run_merge_readiness(
    *,
    review_result_file: str,
    readiness_result_file: str,
    readiness_artifact_path: str,
    iteration_id: str,
    output_file: str,
    timeout_seconds: int = MERGE_READINESS_TIMEOUT_SECONDS,
) -> tuple[dict | None, int, str | None]:
    """Run `check_issue_contract.py --mode merge_readiness ...`.

    This is the sole producer of the merged `REVIEW_ISSUE_RESULT_V1` this
    pipeline persists and hands to the (read-only) `issue-reviewer` agent for
    advisory verdict synthesis.
    """
    script_path = _REVIEW_ISSUE_SCRIPTS / "check_issue_contract.py"
    cmd = [
        sys.executable,
        str(script_path),
        "--mode",
        "merge_readiness",
        "--review-result-file",
        review_result_file,
        "--readiness-result-file",
        readiness_result_file,
        "--readiness-artifact-path",
        readiness_artifact_path,
        "--iteration-id",
        iteration_id,
        "--output-file",
        output_file,
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return None, -1, "timeout"
    try:
        payload = json.loads(Path(output_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, completed.returncode, "malformed_json"
    return payload, completed.returncode, None


# ---------------------------------------------------------------------------
# Canonical artifact directory persistence (root-owned; Issue #2049 AC3)
# ---------------------------------------------------------------------------


def _validate_artifact_containment(path: Path, repo_root: Path) -> Path:
    root = repo_root.resolve()
    base = (root / _CANONICAL_ARTIFACT_DIR).resolve()
    if not base.is_relative_to(root):
        raise ValueError("artifact base escapes repository root")
    resolved = path.resolve()
    if not resolved.is_relative_to(base):
        raise ValueError(f"artifact path escapes canonical artifact directory: {path}")
    return resolved


def _run_unique_component() -> str:
    """A per-invocation-unique filename component (`pid` + `uuid4` suffix).

    PR #2135 human REQUEST_CHANGES iteration-3 P1-3: a second-precision
    timestamp alone (`%Y%m%dT%H%M%SZ`) collides when two concurrent AI
    sessions process the same Issue within the same second, and `os.replace()`
    silently overwrites on collision -- atomicity guarantees the final write
    is not torn, but NOT that it is the run that actually wrote it. Adding
    `os.getpid()` + a `uuid4` suffix to both the temp and final filenames
    makes each invocation's artifact path unique regardless of timestamp
    granularity.
    """
    return f"{os.getpid()}_{uuid.uuid4().hex[:12]}"


def persist_to_canonical_artifact_directory(
    issue_number: int, payload: dict[str, Any], *, repo_root: Path | None = None
) -> Path:
    """Persist `payload` under the canonical artifact directory (root-owned).

    Path: `.claude/artifacts/issue-refinement-loop/<issue_number>/root_review_pipeline_<ts>_<pid>_<uuid4>.json`.
    Writes atomically via `compact_review_result._atomic_write()` (PR #2135
    iteration-3 P1-3: reuses the same hardened `mkstemp()` + 0600 +
    symlink-recheck primitive `compact_review_result.py` already uses --
    added by PR #1907 -- instead of a separately reimplemented, weaker
    writer) and rejects any path that escapes the canonical artifact
    directory.
    """
    if issue_number <= 0:
        raise ValueError(f"issue_number must be positive: {issue_number}")
    repo_root = repo_root or _REPO_ROOT
    issue_dir = repo_root / _CANONICAL_ARTIFACT_DIR / str(issue_number)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = issue_dir / f"root_review_pipeline_{timestamp}_{_run_unique_component()}.json"
    target = _validate_artifact_containment(target, repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)

    content = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True).encode("utf-8")
    _compact_atomic_write(
        target,
        content,
        canonical_root=repo_root,
        issue_slot=str(issue_number),
    )
    return target


# ---------------------------------------------------------------------------
# Child (issue-reviewer agent) stdout classification (Issue #2049 AC4/AC5/AC6)
# ---------------------------------------------------------------------------


def classify_child_stdout(
    raw_text: str,
    *,
    issue_number: int | None = None,
    artifact_root: Path | None = None,
    repo: str | None = None,
    expected_body_sha256: str | None = None,
) -> dict[str, Any]:
    """Classify the child `issue-reviewer` agent's raw stdout text via the
    SAME canonical validator (`validate_review_compact_output.validate_review_compact_output()`)
    the orchestrator's Step 2 routing table uses -- NOT a separately
    reimplemented simplified classifier (PR #2135 human REQUEST_CHANGES
    iteration-3 P0-2 closes the prior bug: any non-empty stdout, even
    malformed JSON, was unconditionally treated as "ok").

    A 0-byte stdout is ALWAYS classified (validator-first, never silently
    dropped) as `empty_input` / `reviewer_transport_failure`: the root
    producer may retry the child invocation exactly once (see
    `retry_once_on_transport_failure`). Any OTHER canonical-validation
    failure on non-empty stdout (malformed envelope, unknown/duplicate
    field, wrong field order, etc.) is classified as `malformed_output` /
    `reviewer_transport_failure` and is ALSO retried exactly once -- it is
    never silently accepted as "ok" just because it is non-empty. A second
    consecutive `reviewer_transport_failure` of either kind is a repeated
    failure and stops with `reviewer_transport_failure` (Issue #2049 AC6)
    -- it is never silently treated as an approve.

    Issue #2054 PR #2142 owner REQUEST_CHANGES P0-2: when `artifact_root` /
    `repo` / `expected_body_sha256` are supplied (the orchestrator's Step 2
    always supplies them for the loop path), a grammatically valid wire is
    NOT trusted on its own. The parent independently re-verifies the exact
    artifact bytes the wire's `ARTIFACT`/`ARTIFACT_SHA256` fields reference
    (`reviewer_transport.verify_artifact()`) and re-derives the canonical
    wire purely from that verified artifact's `semantic_result`
    (`reviewer_transport.verify_wire_matches_artifact()`), so a relay that
    kept `ARTIFACT`/`ARTIFACT_SHA256` pointing at a legitimate,
    hash-verified artifact while self-consistently rewriting
    `VERDICT`/`BLOCKERS`/`NEXT_ACTION` is classified `integrity_failure`,
    not silently trusted. `integrity_failure` is a DIFFERENT classification
    than `reviewer_transport_failure` (AC4): it is not retried by
    `retry_once_on_transport_failure()` the same way a transport-level empty
    /malformed failure is, because the FAILURE is not "the child produced no
    usable output" but "the child's output does not match parent-verified
    ground truth" -- retrying the same compromised/misbehaving relay is not
    expected to self-heal the mismatch.
    """
    result = _canonical_validate_review_compact_output(raw_text, issue_number=issue_number)
    if result["validation_status"] != "valid":
        violations = result.get("violations") or []
        is_empty_input = any(v.get("code") == "empty_input" for v in violations)
        return {
            "classification": "reviewer_transport_failure",
            "code": "empty_input" if is_empty_input else "malformed_output",
            "retryable": True,
        }

    if artifact_root is not None and repo is not None and expected_body_sha256 is not None:
        fields = result.get("normalized_payload") or {}
        artifact_ref = fields.get("ARTIFACT", "")
        artifact_prefix = "compact_review_result_v2="
        if not artifact_ref.startswith(artifact_prefix):
            return {"classification": "integrity_failure", "code": "artifact_reference_invalid", "retryable": False}
        artifact_relative = artifact_ref[len(artifact_prefix) :]
        parts = artifact_relative.split("/")
        invocation_id = parts[1] if len(parts) > 1 else ""
        try:
            attempt = int(parts[2].removeprefix("attempt-")) if len(parts) > 2 else 0
        except ValueError:
            attempt = 0
        verified = _reviewer_transport.verify_artifact(
            artifact_root=artifact_root,
            artifact_relative=artifact_relative,
            expected_repo=repo,
            expected_issue=issue_number or 0,
            expected_body_sha256=expected_body_sha256,
            expected_invocation_id=invocation_id,
            expected_attempt=attempt,
            expected_sha256=fields.get("ARTIFACT_SHA256", ""),
        )
        if verified["status"] != "valid":
            return {
                "classification": "integrity_failure",
                "code": verified.get("reason_code", "artifact_integrity_failure"),
                "retryable": False,
            }
        cross = _reviewer_transport.verify_wire_matches_artifact(
            wire=raw_text, verified_artifact=verified, artifact_relative=artifact_relative,
            artifact_sha256=fields.get("ARTIFACT_SHA256", ""),
        )
        if cross["status"] != "valid":
            return {
                "classification": "integrity_failure",
                "code": cross.get("reason_code", "wire_artifact_semantic_mismatch"),
                "retryable": False,
            }

    return {"classification": "ok", "code": None, "retryable": False}


def retry_once_on_transport_failure(
    invoke_child,
    *,
    issue_number: int | None = None,
    artifact_root: Path | None = None,
    repo: str | None = None,
    expected_body_sha256: str | None = None,
    elapsed_seconds: float | None = None,
    total_deadline_seconds: float | None = None,
    per_attempt_deadline_seconds: float | None = None,
):
    """Call `invoke_child()` (returns raw stdout text); if the FIRST call is
    classified `reviewer_transport_failure` (empty OR malformed, via the
    canonical validator -- see `classify_child_stdout`), retry
    `invoke_child()` exactly once. If the retry is ALSO
    `reviewer_transport_failure`, this is a repeated failure: stop and
    report `reviewer_transport_failure` (Issue #2049 AC6) rather than
    retrying unboundedly or silently downgrading to an unrelated verdict.

    Issue #2054 PR #2142 owner REQUEST_CHANGES P0-1/P0-2: this function
    delegates its retryable/non-retryable classification vocabulary to
    `classify_child_stdout()` (in turn delegating to `reviewer_transport.py`,
    the V2 contract SSOT) rather than reimplementing a second policy.
    `integrity_failure` (wire<->artifact binding mismatch, distinct from a
    transport-level empty/malformed failure -- Issue #2054 AC4) is NEVER
    retried and NEVER silently accepted as `status: ok`: retrying the same
    misbehaving/compromised relay is not expected to self-heal a semantic
    mismatch against parent-verified artifact ground truth.

    Issue #2165 merge condition #8 (PR #2177 OWNER 2026-08-15
    REQUEST_CHANGES, fix_delta iteration 2): this retry loop is a SEPARATE,
    OUTER layer from `reviewer_transport.run_reviewer_transport()`'s own
    attempt loop (which already applies
    `reviewer_transport.has_sufficient_retry_attempt_budget()` uniformly
    across all backends). Previously this function retried unconditionally
    exactly once with no remaining-budget awareness at all -- a caller
    driving `invoke_child()` against ANY backend (deterministic, claude,
    codex) could spawn a second, doomed-to-timeout attempt even with almost
    no total-deadline budget left. When `elapsed_seconds` /
    `total_deadline_seconds` / `per_attempt_deadline_seconds` are all
    supplied, the SAME backend-agnostic budget guard
    `reviewer_transport.has_sufficient_retry_attempt_budget()` uses is
    applied here before the retry call; when any of the three is omitted
    (the caller does not track deadline state), behavior is unchanged from
    before -- unconditional retry-exactly-once (backward compatible
    default, matching every existing caller/test of this function).
    """
    first = invoke_child()
    classification = classify_child_stdout(
        first,
        issue_number=issue_number,
        artifact_root=artifact_root,
        repo=repo,
        expected_body_sha256=expected_body_sha256,
    )
    if classification["classification"] == "ok":
        return {"raw_text": first, "attempts": 1, "final_classification": classification, "status": "ok"}
    if classification["classification"] == "integrity_failure":
        return {
            "raw_text": first,
            "attempts": 1,
            "final_classification": classification,
            "status": "integrity_failure",
        }

    if (
        elapsed_seconds is not None
        and total_deadline_seconds is not None
        and per_attempt_deadline_seconds is not None
        and not _reviewer_transport.has_sufficient_retry_attempt_budget(
            elapsed_seconds=elapsed_seconds,
            total_deadline_seconds=total_deadline_seconds,
            per_attempt_deadline_seconds=per_attempt_deadline_seconds,
        )
    ):
        return {
            "raw_text": first,
            "attempts": 1,
            "final_classification": classification,
            "status": "reviewer_transport_failure",
            "retry_skipped_reason": "insufficient_retry_budget",
        }

    second = invoke_child()
    retry_classification = classify_child_stdout(
        second,
        issue_number=issue_number,
        artifact_root=artifact_root,
        repo=repo,
        expected_body_sha256=expected_body_sha256,
    )
    if retry_classification["classification"] == "ok":
        return {
            "raw_text": second,
            "attempts": 2,
            "final_classification": retry_classification,
            "status": "ok",
        }
    if retry_classification["classification"] == "integrity_failure":
        return {
            "raw_text": second,
            "attempts": 2,
            "final_classification": retry_classification,
            "status": "integrity_failure",
        }

    return {
        "raw_text": second,
        "attempts": 2,
        "final_classification": retry_classification,
        "status": "reviewer_transport_failure",
    }


# ---------------------------------------------------------------------------
# Readback: regular file / no symlink / strict JSON / body SHA / verdict
# identity (Issue #2049 AC7)
# ---------------------------------------------------------------------------


def _classify_verify_artifact_failure(reason_code: str) -> str:
    """Translate `reviewer_transport.verify_artifact()` / `secure_read_json()`'s
    internal `reason_code` vocabulary into `readback_persisted_artifact()`'s
    pre-existing, stable EXTERNAL violation vocabulary (Issue #2242 OWNER
    adversarial review, Blocker 3).

    This is a PRESENTATION-LAYER mapping only -- it never opens, reads,
    hashes, or otherwise touches the artifact bytes itself; that is fully
    delegated to `reviewer_transport.verify_artifact()`. Its sole purpose is
    to keep `readback_persisted_artifact()`'s caller-facing violation codes
    stable across this refactor (which replaced a custom, leaf-only
    `os.open(..., O_NOFOLLOW)` + hand-rolled read loop with full delegation
    to the dir-fd-anchored, component-wise no-follow, SHA-256-verified
    `verify_artifact()` / `secure_read_json()` primitives).
    """
    if reason_code == "raw_byte_hash_mismatch":
        return "artifact_sha256_mismatch"
    if reason_code == "schema_mismatch":
        return "artifact_schema_mismatch"
    if reason_code == "artifact_binding_mismatch":
        return "artifact_binding_mismatch"
    if reason_code in (
        "non_regular_or_oversize_artifact",
        "raw_byte_oversize",
        "unsupported_secure_open_capability",
        "artifact_path_not_relative",
    ):
        return "artifact_not_regular_file"
    lowered = reason_code.lower()
    if any(
        token in lowered
        for token in (
            "symbolic link", "eloop", "no such file", "not a directory", "permission denied", "is a directory"
        )
    ):
        return "artifact_not_regular_file"
    # Every other failure at this layer is a strict-JSON parse failure:
    # `duplicate_json_key` / `non_finite_json` (both raised by
    # `reviewer_transport.strict_json_loads()`) or a raw `json.JSONDecodeError`
    # message.
    return "artifact_not_strict_json"


def readback_persisted_artifact(
    *,
    artifact_root: str | Path,
    artifact_relative: str,
    expected_repo: str,
    expected_issue: int,
    expected_body_sha256: str,
    expected_invocation_id: str,
    expected_attempt: int,
    expected_artifact_sha256: str,
    expected_verdict: str,
) -> dict[str, Any]:
    """Verify a persisted compact-review artifact before allowing the
    "final review" (remote Issue body update) step to run.

    Issue #2242 OWNER adversarial review
    (https://github.com/squne121/loop-protocol/pull/2246#issuecomment-5328161000),
    Blocker 2 + Blocker 3:

    Blocker 2 (AC4 binding verification was self-referential/tautological):
    the previous revision read `repository`/`issue_number`/`invocation_id`/
    `attempt` OUT of the artifact itself via `extract_binding_context()`,
    then compared those SAME values back against the artifact as "expected"
    values -- a comparison that can never fail except accidentally via
    `body_sha256` (`payload["x"] == payload["x"]`). This revision requires
    the CALLER to supply every expected binding value independently
    (`expected_repo` / `expected_issue` / `expected_invocation_id` /
    `expected_attempt` / `expected_artifact_sha256`, sourced from the
    orchestrator's own known repo/issue/invocation/attempt context -- never
    re-derived from the artifact being verified) and passes them straight
    through to `reviewer_transport.verify_artifact()`.

    Blocker 3 (custom insecure symlink-following file open instead of full
    delegation): the previous revision implemented its own
    `os.open(path, O_NOFOLLOW)` + custom 10 MiB size cap + custom read loop.
    `O_NOFOLLOW` on a single flat `os.open()` call only rejects a symlink at
    the LEAF path component -- it does NOT reject symlinks in intermediate
    path components, so a symlinked parent directory could smuggle in an
    out-of-root regular file. This revision deletes that reimplementation
    entirely and fully delegates the open+read+hash+binding-verify to
    `reviewer_transport.verify_artifact()`, which anchors traversal via
    `artifact_root` + a validated repo-relative `artifact_relative` path,
    applies `O_DIRECTORY|O_NOFOLLOW` on EVERY intermediate path component
    (not just the leaf), enforces a 1 MiB cap
    (`reviewer_transport.ARTIFACT_MAX_BYTES`), computes SHA-256 over the
    same raw bytes, and independently verifies binding. No parallel
    open/read/hash logic is reimplemented in this module.

    `_classify_verify_artifact_failure()` translates `verify_artifact()`'s
    internal `reason_code` vocabulary into this function's pre-existing
    external violation vocabulary (presentation layer only -- see that
    function's docstring).

    Checks (all must pass for `verdict_identity: true`):
      1. `artifact_root`/`artifact_relative` open (dir-fd-anchored,
         component-wise no-follow) as a regular file within the 1 MiB cap.
      2. File content parses as strict JSON via the SAME raw bytes read once.
      3. The parsed payload is a `REVIEWER_COMPACT_ARTIFACT_V2` object whose
         raw-byte SHA-256 matches `expected_artifact_sha256` exactly.
      4. The artifact's `repository` / `issue_number` / `reviewed_body_sha256`
         / `invocation_id` / `attempt` fields ALL match the caller-supplied
         `expected_repo` / `expected_issue` / `expected_body_sha256` /
         `expected_invocation_id` / `expected_attempt` exactly
         (`reviewer_transport.check_artifact_binding()`).
      5. The artifact's nested `semantic_result` validates against the FULL
         `REVIEW_ISSUE_RESULT_V1` jsonschema
         (`reviewer_transport.validate_semantic_result_schema()`, Issue #2242
         Blocker 4 -- not merely `verdict`/`blocking_issues` presence).
      6. The artifact's nested `semantic_result.verdict` (extracted via
         `reviewer_transport.semantic_verdict_and_count()`, never a locally
         reimplemented `semantic_result` field-map) matches
         `expected_verdict` exactly.
    """
    verified = _reviewer_transport.verify_artifact(
        artifact_root=Path(artifact_root),
        artifact_relative=artifact_relative,
        expected_repo=expected_repo,
        expected_issue=expected_issue,
        expected_body_sha256=expected_body_sha256,
        expected_invocation_id=expected_invocation_id,
        expected_attempt=expected_attempt,
        expected_sha256=expected_artifact_sha256,
    )
    if verified["status"] != "valid":
        violation = _classify_verify_artifact_failure(verified.get("reason_code", ""))
        return {"verdict_identity": False, "violations": [violation]}

    payload = verified["payload"]
    violations: list[str] = []

    semantic_result = payload.get("semantic_result")
    schema_violation = _reviewer_transport.validate_semantic_result_schema(semantic_result)
    if schema_violation is not None:
        violations.append(schema_violation)

    try:
        actual_verdict, _blocking_count = _reviewer_transport.semantic_verdict_and_count(semantic_result)
    except ValueError:
        violations.append("semantic_result_invalid")
        return {"verdict_identity": False, "violations": violations, "payload": payload}

    if actual_verdict != expected_verdict:
        violations.append("verdict_mismatch")

    return {"verdict_identity": not violations, "violations": violations, "payload": payload}


# ---------------------------------------------------------------------------
# Final-review gate (Issue #2049 AC10)
# ---------------------------------------------------------------------------


def gate_final_review(*, remote_update_ok: bool, readback: dict[str, Any]) -> dict[str, Any]:
    """Decide whether the "final review" step may run.

    Final review MUST run only after (a) the remote Issue body update
    succeeded and (b) `readback_persisted_artifact()` reports
    `verdict_identity: true`. Either failing blocks final review.
    """
    verdict_identity = bool(readback.get("verdict_identity"))
    allowed = bool(remote_update_ok) and verdict_identity
    reasons: list[str] = []
    if not remote_update_ok:
        reasons.append("remote_body_update_not_confirmed")
    if not verdict_identity:
        reasons.extend(readback.get("violations", []) or ["readback_verdict_identity_failed"])
    return {"final_review_allowed": allowed, "reasons": reasons}


# ---------------------------------------------------------------------------
# Static agent-contract check (Issue #2049 AC9)
# ---------------------------------------------------------------------------

_WORKSPACE_WRITE_MARKERS = (
    "artifact として保存",
    "temp file",
    "一時ファイル",
    "を保存し",
    "書き込む",
    "書き込み",
    "Persist",
)

# Negation cues that, when present in the SAME sentence as an action marker,
# mean the sentence is describing what the agent does NOT do (e.g. "producer
# I/O を一切行わない" / "何も書き込まない") rather than asserting a workspace
# write requirement. Without this, a read-only agent's own disclaimer text
# (which necessarily mentions "artifact" / "temp file" / "書き込み" while
# denying it performs them) would be flagged as self-contradictory.
_NEGATION_CUES = (
    "行わない",
    "行いません",
    "書き込まない",
    "しない",
    "せず",
    "一切",
    "ではない",
)


def _split_sentences(text: str) -> list[str]:
    """Split on full-width period, first folding newlines to spaces so a
    sentence that wraps across multiple TOML lines is still evaluated as one
    unit (negation cues near the end of a wrapped sentence must still count)."""
    flat = text.replace("\n", " ")
    return [s for s in flat.split("。") if s.strip()]


def check_agent_is_read_only_advisory(toml_text: str) -> list[str]:
    """Reject a read-only agent config whose instructions still carry a
    workspace write requirement (Issue #2049 AC9).

    A config is only flagged when it BOTH declares itself read-only
    (`default_permissions` containing `readonly`) AND its
    `developer_instructions` contains a sentence with a workspace-write
    marker that is NOT negated in the same sentence (i.e. it asserts,
    rather than disclaims, a write requirement). A non-read-only agent is
    never flagged.
    """
    violations: list[str] = []
    is_read_only = bool(re.search(r'default_permissions\s*=\s*"[^"]*readonly[^"]*"', toml_text))
    if not is_read_only:
        return violations

    instructions_match = re.search(r'developer_instructions\s*=\s*"""(.*?)"""', toml_text, re.DOTALL)
    instructions = instructions_match.group(1) if instructions_match else toml_text

    for sentence in _split_sentences(instructions):
        if any(cue in sentence for cue in _NEGATION_CUES):
            continue
        for marker in _WORKSPACE_WRITE_MARKERS:
            if marker in sentence:
                violations.append(f"workspace_write_marker_present:{marker}")

    return violations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def produce_compact_result(
    issue_number: int,
    merged: dict[str, Any],
    *,
    repo_root: Path | None = None,
    repo: str = "squne121/loop-protocol",
) -> dict[str, Any]:
    """Root-owned, single-producer generation + persistence of the
    `ISSUE_REVIEW_RESULT_COMPACT_V2` compact envelope (Issue #2054 AC5/AC8).

    Delegates envelope construction, semantic-artifact persistence, and
    self-validation to `reviewer_transport.py` (the V2 contract SSOT)
    instead of the retired V1 `compact_review_result()` renderer -- the V1
    `ISSUE_REVIEW_RESULT_COMPACT_V1` wire (9 lines, `EVIDENCE` field) is
    retired in this same commit; there is no partial-deployment/downgrade
    fallback (AC5). This remains the ONLY place in the whole pipeline that
    performs this write; the read-only `issue-reviewer` child NEVER invokes
    any producer script itself.

    Returns a dict with keys: `stdout_lines` (the exact
    `ISSUE_REVIEW_RESULT_COMPACT_V2` lines to hand the read-only child
    verbatim), `artifact_path` (the persisted semantic artifact's
    filesystem path -- identical to the `ARTIFACT` stdout line), `verdict`,
    `next_action`, `reviewed_body_sha256`, `attempt_id`.
    """
    repo_root = repo_root or _REPO_ROOT
    artifact_root = repo_root / _CANONICAL_ARTIFACT_DIR

    verdict = merged.get("verdict")
    if verdict not in {"approve", "needs-fix"}:
        raise ValueError(f"invalid verdict for V2 compact envelope: {verdict!r}")
    blocking_issues = merged.get("blocking_issues", []) or []
    blockers_count = len(blocking_issues)
    reviewed_body_sha256 = merged.get("body_sha256") or ""

    if verdict == "approve":
        summary = "contract ready"
    else:
        summary_parts = [f"{blockers_count} blocker(s)"]
        first = blocking_issues[0] if blocking_issues else None
        first_code = ""
        if isinstance(first, dict):
            first_code = first.get("code", "")
        elif isinstance(first, str):
            first_code = first[:60]
        if first_code:
            summary_parts.append(f"first={first_code}")
        summary = "; ".join(summary_parts)

    invocation_id = _reviewer_transport.generate_invocation_id()
    attempt = 1
    semantic_result = {"verdict": verdict, "blocking_issues": blocking_issues}
    relative, artifact_sha256 = _reviewer_transport.write_semantic_artifact(
        artifact_root=artifact_root,
        issue_number=issue_number,
        repo=repo,
        invocation_id=invocation_id,
        attempt=attempt,
        reviewed_body_sha256=reviewed_body_sha256,
        semantic_result=semantic_result,
    )
    wire = _reviewer_transport.build_compact_v2(
        verdict=verdict,
        summary=summary,
        blockers=blockers_count,
        reviewed_body_sha256=reviewed_body_sha256,
        attempt_id=invocation_id,
        artifact_relative=relative,
        artifact_sha256=artifact_sha256,
    )
    validated = _reviewer_transport.validate_compact_v2(
        wire, issue_number=issue_number, invocation_id=invocation_id, attempt=attempt
    )
    if validated["validation_status"] != "valid":
        raise ValueError(f"parent_compact_v2_self_validation_failure: {validated['violations']}")
    fields = validated["normalized_payload"]
    stdout_lines = wire.decode("utf-8").rstrip("\n").split("\n")
    return {
        "stdout_lines": stdout_lines,
        "artifact_path": str(artifact_root / relative),
        "verdict": fields["VERDICT"],
        "next_action": fields["NEXT_ACTION"],
        "reviewed_body_sha256": fields["REVIEWED_BODY_SHA256"],
        "attempt_id": fields["ATTEMPT_ID"],
    }


def run_checker_pipeline_once(
    *,
    body_file: str,
    issue_number: int,
    body_sha256: str,
    readiness_timeout_seconds: int | None = None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Run check_issue_contract -> contract_readiness_check -> merge_readiness
    exactly once against an already-fetched, already-pinned body file.

    Extracted from the former inline body of `_cmd_produce()` (Issue #2054
    PR #2142 owner REQUEST_CHANGES P0-1) so it can be invoked both in-process
    (by `_cmd_run_checker_attempt()`, when this module is spawned as a
    subprocess child by `reviewer_transport.run_reviewer_transport()`) and
    directly by tests.  All intermediate JSON files live in a private
    temporary directory removed on return.

    Returns ``(merged, error_code, timeout_phase)``. ``timeout_phase`` is
    non-None only when ``error_code == "timeout"`` (Issue #2165 P0-1/P1-4):
    it records WHICH layer produced the timeout -- the wrapper subprocess
    itself (``check_issue_contract`` / ``contract_readiness_check_wrapper``
    / ``merge_readiness``), or `contract_readiness_check.py`'s OWN typed
    ``status: "runtime_error"`` payload (``baseline_vc_preflight_aggregate``,
    forwarded from ``readiness_result["timeout_phase"]``) when its wrapper
    subprocess itself completed but reported that its internal
    `baseline_vc_preflight.py` execution timed out.
    """
    scratch_dir = Path(tempfile.mkdtemp(prefix="root_review_pipeline_attempt_"))
    try:
        review_result, _review_rc, review_err = run_check_issue_contract(body_file)
        if review_result is None:
            return None, review_err, ("check_issue_contract" if review_err == "timeout" else None)

        # Issue #2207 AC8: use the invocation-local dynamic readiness-wrapper
        # timeout (derived by `_cmd_produce()` from the SAME pinned body via
        # the canonical VC plan / budget formula) when supplied, instead of
        # `run_contract_readiness_check()`'s static `N<=2`-compatible default.
        if readiness_timeout_seconds is not None:
            readiness_result, _readiness_rc, readiness_err = run_contract_readiness_check(
                body_file, timeout_seconds=readiness_timeout_seconds
            )
        else:
            readiness_result, _readiness_rc, readiness_err = run_contract_readiness_check(body_file)
        if readiness_result is None:
            return None, readiness_err, ("contract_readiness_check_wrapper" if readiness_err == "timeout" else None)

        # Issue #2165 P0-1: `contract_readiness_check.py` can complete (its
        # OWN wrapper subprocess does not itself raise TimeoutExpired) yet
        # still report a typed `status: "runtime_error"` because ITS
        # internal `baseline_vc_preflight.py` aggregate execution timed
        # out. `readiness_result` being non-None previously meant this fell
        # straight through to `run_merge_readiness()` as an ordinary
        # semantic readiness result -- collapsing the runtime failure into
        # `category: no_commands_extracted` / `needs_fix`. Treat it the
        # SAME way an actual wrapper-level `subprocess.TimeoutExpired`
        # would be treated: a transport-visible timeout, never handed to
        # `run_merge_readiness()`.
        if readiness_result.get("status") == "runtime_error":
            phase = readiness_result.get("timeout_phase")
            return None, "timeout", (phase if isinstance(phase, str) and phase else "contract_readiness_check")

        review_result_file = str(scratch_dir / "review_result.json")
        readiness_result_file = str(scratch_dir / "readiness_result.json")
        merged_output_file = str(scratch_dir / "merged_review_result.json")
        Path(review_result_file).write_text(json.dumps(review_result), encoding="utf-8")
        Path(readiness_result_file).write_text(json.dumps(readiness_result), encoding="utf-8")

        merged, _merge_rc, merge_err = run_merge_readiness(
            review_result_file=review_result_file,
            readiness_result_file=readiness_result_file,
            readiness_artifact_path=readiness_result_file,
            iteration_id=f"root_review_pipeline_{issue_number}",
            output_file=merged_output_file,
        )
        if merged is None:
            return None, merge_err, ("merge_readiness" if merge_err == "timeout" else None)

        merged["body_sha256"] = body_sha256
        return merged, None, None
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def _cmd_run_checker_attempt(args: argparse.Namespace) -> int:
    """[internal] Single reviewer-transport "attempt" child: run the
    deterministic checker pipeline once against an already-pinned body file
    and print the merged `REVIEW_ISSUE_RESULT_V1` JSON to stdout.

    This subcommand exists SPECIFICALLY to be spawned by
    `reviewer_transport.run_reviewer_transport()` as its child process
    (Issue #2054 PR #2142 owner REQUEST_CHANGES P0-1): the parent transport
    owns attempt manifests, the closed retry matrix, process-group reaping,
    and artifact-binding for whatever it spawns, so the checker pipeline
    invocation gets that real production wiring instead of being invoked
    ad hoc, unretried, untelemetered by `_cmd_produce()` directly. It is not
    part of the human-facing CLI surface (not documented in the module
    docstring's subcommand list) and MUST NOT be invoked by anything other
    than `_cmd_produce()` via `run_reviewer_transport()`.
    """
    merged, error_code, timeout_phase = run_checker_pipeline_once(
        body_file=args.body_file,
        issue_number=args.issue_number,
        body_sha256=args.body_sha256,
        readiness_timeout_seconds=args.readiness_timeout_seconds,
    )
    if merged is None:
        # Issue #2165 P1-4: `timeout_phase` is included ONLY when set (kept
        # additive on the stderr envelope, matching `_attempt_result()`'s
        # additive field on the parent side) so `reviewer_transport.py`'s
        # existing `error_code == "timeout"` detection keeps working
        # unchanged for consumers that only check `error_code`.
        payload: dict[str, Any] = {"error_code": error_code}
        if timeout_phase:
            payload["timeout_phase"] = timeout_phase
        print(json.dumps(payload), file=sys.stderr)
        return 2
    print(json.dumps(merged))
    return 0


def _cmd_produce(args: argparse.Namespace) -> int:
    body, body_sha256, error_code = fetch_and_pin_live_body(args.issue_number, args.repo)
    if body is None:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "schema_version": SCHEMA_VERSION,
                    "status": "input_or_runtime_error",
                    "error_code": error_code,
                }
            )
        )
        return 2

    # Issue #2207 AC7/AC8: compute the invocation-local canonical VC plan
    # and derive its `ReviewBudget` from the SAME pinned body BEFORE any
    # checker subprocess is spawned. A body whose plan exceeds the fixed
    # policy ceiling is rejected here (typed, non-retryable), never reaching
    # `run_reviewer_transport()` / `run-checker-attempt`.
    _vc_plan = _compute_canonical_vc_plan(body)
    try:
        # Issue #2207 OWNER P1-3 (PR #2221 REQUEST_CHANGES): `command_occurrence_count`,
        # per the Issue #2207 Outcome/AC5 contract -- NOT `launch_upper_bound`
        # (an earlier implementation iteration substituted the dedup-aware
        # actual-launch upper bound without an Issue reframe).
        _review_budget = _derive_review_budget(
            _vc_plan["command_occurrence_count"], policy_cap=_vc_plan["policy_cap"]
        )
    except _VerificationBudgetExceedsPolicyError as exc:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "schema_version": SCHEMA_VERSION,
                    "status": "input_or_runtime_error",
                    "error_code": exc.error_code,
                }
            )
        )
        return 2

    tmp_root = _REPO_ROOT / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(tmp_root), prefix="root_review_pipeline_") as scratch_dir:
        body_file = write_pinned_body_tempfile(body, dir=scratch_dir)

        # Issue #2054 PR #2142 owner REQUEST_CHANGES P0-1: production wiring
        # through `reviewer_transport.run_reviewer_transport()` -- real
        # attempt manifests, the closed retry matrix, process-group
        # reaping, and artifact-binding/wire cross-checks now govern the
        # checker-pipeline invocation itself. `backend="deterministic"`:
        # this specific reviewer step is a deterministic checker chain with
        # no session-resume concept of its own, but it is spawned, retried,
        # and artifact-bound through the SAME transport machinery a real
        # Claude/Codex backend attempt uses (see
        # `reviewer_transport.build_backend_command()`).
        base_argv = [
            sys.executable,
            str(Path(__file__).resolve()),
            "run-checker-attempt",
            "--body-file",
            body_file,
            "--issue-number",
            str(args.issue_number),
            "--body-sha256",
            body_sha256,
            "--readiness-timeout-seconds",
            str(_review_budget.readiness_wrapper_seconds),
        ]
        artifact_root = _REPO_ROOT / _CANONICAL_ARTIFACT_DIR

        # Issue #2165 P1-1 / Issue #2207 AC8: derive and pass EXPLICIT
        # per-attempt/total deadlines here, rather than relying on
        # `reviewer_transport.py`'s own generic fallback constants -- THIS
        # module is the one that knows the real layered budget the
        # deterministic child executes. `_review_budget` (computed above,
        # BEFORE this subprocess was spawned, from the SAME pinned body via
        # the canonical VC plan / budget formula) already folds in
        # `CONTRACT_READINESS_CHECK_TIMEOUT_SECONDS`'s invocation-local
        # dynamic value (`readiness_wrapper_seconds`) plus
        # `CHECK_ISSUE_CONTRACT_TIMEOUT_SECONDS` /
        # `MERGE_READINESS_TIMEOUT_SECONDS` / process-spawn margin -- ties
        # the transport-level deadline to the SAME arithmetic that produces
        # the child's own budgets (and, at `N<=2`, reduces to the exact
        # values `_deterministic_per_attempt_deadline` used to hand-derive),
        # so the two cannot independently drift apart again.
        transport_result = _reviewer_transport.run_reviewer_transport(
            base_argv=base_argv,
            command_id="root_review_pipeline.checker_attempt",
            argv_template_id="root_review_pipeline.checker_attempt/v1",
            backend="deterministic",
            issue_number=args.issue_number,
            repo=args.repo,
            reviewed_body_sha256=body_sha256,
            artifact_root=artifact_root,
            per_attempt_deadline=_review_budget.per_attempt_seconds,
            total_deadline=_review_budget.total_seconds,
        )
        if transport_result["transport_status"] != "ok":
            print(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "schema_version": SCHEMA_VERSION,
                        "status": "input_or_runtime_error",
                        "error_code": "reviewer_transport_environment_failure",
                        "transport_invocation_id": transport_result["invocation_id"],
                    }
                )
            )
            return 2

        last_attempt = transport_result["attempts"][-1]
        compact_fields = last_attempt["compact"]
        artifact_prefix = "compact_review_result_v2="
        artifact_relative = compact_fields["ARTIFACT"][len(artifact_prefix) :]

        # Re-verify (dir-fd-anchored, no-follow, single-read) the artifact
        # `run_reviewer_transport()` already validated internally, so this
        # module never trusts the child's `compact` fields without an
        # independent artifact-bytes readback of its own.
        verified = _reviewer_transport.verify_artifact(
            artifact_root=artifact_root,
            artifact_relative=artifact_relative,
            expected_repo=args.repo,
            expected_issue=args.issue_number,
            expected_body_sha256=body_sha256,
            expected_invocation_id=transport_result["invocation_id"],
            expected_attempt=last_attempt["attempt"],
            expected_sha256=compact_fields["ARTIFACT_SHA256"],
        )
        if verified["status"] != "valid":
            print(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "schema_version": SCHEMA_VERSION,
                        "status": "input_or_runtime_error",
                        "error_code": "artifact_readback_failed",
                    }
                )
            )
            return 2

        # Issue #2054 PR #2142 owner REQUEST_CHANGES P0-3: the artifact's
        # `semantic_result` IS the full merged `REVIEW_ISSUE_RESULT_V1`
        # (`run_checker_pipeline_once()`'s return value, printed verbatim by
        # `_cmd_run_checker_attempt()` and persisted losslessly by
        # `reviewer_transport.write_semantic_artifact()` -- not a
        # `{"verdict":..., "blocking_issues":...}` projection), so
        # `findings[]` / `checker_evidence` / `structured_blockers` /
        # producer schema version all survive in the canonical V2 artifact.
        merged = verified["payload"]["semantic_result"]

        artifact_path = persist_to_canonical_artifact_directory(args.issue_number, merged)

        wire = _reviewer_transport.build_compact_v2(
            verdict=compact_fields["VERDICT"],
            summary=compact_fields["SUMMARY"],
            blockers=int(compact_fields["BLOCKERS"]),
            reviewed_body_sha256=compact_fields["REVIEWED_BODY_SHA256"],
            attempt_id=compact_fields["ATTEMPT_ID"],
            artifact_relative=artifact_relative,
            artifact_sha256=compact_fields["ARTIFACT_SHA256"],
            must_read=compact_fields["MUST_READ"],
        )
        compact_result = {
            "stdout_lines": wire.decode("utf-8").rstrip("\n").split("\n"),
            "artifact_path": str(artifact_root / artifact_relative),
            "verdict": compact_fields["VERDICT"],
            "next_action": compact_fields["NEXT_ACTION"],
            "reviewed_body_sha256": compact_fields["REVIEWED_BODY_SHA256"],
            "attempt_id": compact_fields["ATTEMPT_ID"],
        }

        # Issue #2242 OWNER adversarial review Blocker 1
        # (https://github.com/squne121/loop-protocol/pull/2246#issuecomment-5328161000):
        # `produce` returns TWO separate, differently-schemad artifacts --
        # the top-level `artifact_path` (the FULL `REVIEW_ISSUE_RESULT_V1`,
        # written via `persist_to_canonical_artifact_directory()`, satisfying
        # Issue #2049 AC3's "save full artifact to canonical artifact
        # directory" contract) and `compact_result.artifact_path` (the
        # `REVIEWER_COMPACT_ARTIFACT_V2` wrapper, written via
        # `reviewer_transport.write_semantic_artifact()`). These typed
        # `full_review_artifact` / `verified_transport_artifact` fields make
        # that distinction explicit and machine-readable instead of leaving
        # it implicit in two differently-named `artifact_path` keys.
        #
        # CANONICAL CONTRACT (explicit, not ambiguous): `readback` /
        # `gate-final-review` consume `verified_transport_artifact`
        # (`REVIEWER_COMPACT_ARTIFACT_V2`, via its `root`/`relative_path`/
        # `sha256`) as their canonical input -- that schema carries the
        # verifiable repo/issue/body-sha/invocation/attempt binding context
        # `readback_persisted_artifact()` requires. `full_review_artifact`
        # (`REVIEW_ISSUE_RESULT_V1`) is NOT valid input to `readback`/
        # `gate-final-review`; it remains the durable, human/tooling-facing
        # full checker result Issue #2049 AC3 requires to be persisted.
        full_review_artifact = {"path": str(artifact_path), "schema": "REVIEW_ISSUE_RESULT_V1"}
        verified_transport_artifact = {
            "root": str(artifact_root),
            "relative_path": artifact_relative,
            "sha256": compact_fields["ARTIFACT_SHA256"],
            "schema": "REVIEWER_COMPACT_ARTIFACT_V2",
            "invocation_id": transport_result["invocation_id"],
            "attempt": last_attempt["attempt"],
        }

        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "schema_version": SCHEMA_VERSION,
                    "status": "ok",
                    "issue_number": args.issue_number,
                    "body_sha256": body_sha256,
                    "merged_review_result": merged,
                    "artifact_path": str(artifact_path),
                    "compact_result": compact_result,
                    "full_review_artifact": full_review_artifact,
                    "verified_transport_artifact": verified_transport_artifact,
                }
            )
        )
        return 0


def _cmd_classify_child_stdout(args: argparse.Namespace) -> int:
    if args.input_file:
        raw_text = Path(args.input_file).read_text(encoding="utf-8")
    else:
        raw_text = sys.stdin.read()
    artifact_root = Path(args.artifact_root) if args.artifact_root else None
    result = classify_child_stdout(
        raw_text,
        issue_number=args.issue_number,
        artifact_root=artifact_root,
        repo=args.repo,
        expected_body_sha256=args.expected_body_sha256,
    )
    print(json.dumps(result))
    return 0 if result["classification"] == "ok" else 1


def _cmd_readback(args: argparse.Namespace) -> int:
    result = readback_persisted_artifact(
        artifact_root=args.artifact_root,
        artifact_relative=args.artifact_relative,
        expected_repo=args.expected_repo,
        expected_issue=args.expected_issue,
        expected_body_sha256=args.expected_body_sha256,
        expected_invocation_id=args.expected_invocation_id,
        expected_attempt=args.expected_attempt,
        expected_artifact_sha256=args.expected_artifact_sha256,
        expected_verdict=args.expected_verdict,
    )
    print(json.dumps(result))
    return 0 if result["verdict_identity"] else 1


def _cmd_gate_final_review(args: argparse.Namespace) -> int:
    readback = readback_persisted_artifact(
        artifact_root=args.artifact_root,
        artifact_relative=args.artifact_relative,
        expected_repo=args.expected_repo,
        expected_issue=args.expected_issue,
        expected_body_sha256=args.expected_body_sha256,
        expected_invocation_id=args.expected_invocation_id,
        expected_attempt=args.expected_attempt,
        expected_artifact_sha256=args.expected_artifact_sha256,
        expected_verdict=args.expected_verdict,
    )
    result = gate_final_review(remote_update_ok=args.remote_update_ok, readback=readback)
    print(json.dumps(result))
    return 0 if result["final_review_allowed"] else 1


def _cmd_check_agent_contract(args: argparse.Namespace) -> int:
    text = Path(args.toml_file).read_text(encoding="utf-8")
    violations = check_agent_is_read_only_advisory(text)
    print(json.dumps({"violations": violations}))
    return 0 if not violations else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Root-owned issue-refinement-loop review pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p_produce = sub.add_parser("produce", help="Fetch + pin live body, run checkers, persist artifact")
    p_produce.add_argument("--issue-number", type=int, required=True)
    p_produce.add_argument("--repo", default="squne121/loop-protocol")
    p_produce.set_defaults(func=_cmd_produce)

    p_attempt = sub.add_parser(
        "run-checker-attempt",
        help="[internal] single reviewer_transport attempt child; not part of the human-facing CLI surface",
    )
    p_attempt.add_argument("--body-file", required=True)
    p_attempt.add_argument("--issue-number", type=int, required=True)
    p_attempt.add_argument("--body-sha256", required=True)
    # Issue #2207 AC8: invocation-local readiness-wrapper timeout derived by
    # `_cmd_produce()` from the SAME pinned body (canonical VC plan / budget
    # formula). Optional so direct/test invocations without it fall back to
    # `run_contract_readiness_check()`'s static default.
    p_attempt.add_argument("--readiness-timeout-seconds", type=int, default=None)
    p_attempt.set_defaults(func=_cmd_run_checker_attempt)

    p_classify = sub.add_parser("classify-child-stdout", help="Classify child agent raw stdout")
    p_classify.add_argument("--input-file")
    p_classify.add_argument("--issue-number", type=int, default=None)
    p_classify.add_argument(
        "--artifact-root", default=None, help="Enables wire<->artifact cross-check (Issue #2054 AC4/P0-2) when set"
    )
    p_classify.add_argument("--repo", default=None)
    p_classify.add_argument("--expected-body-sha256", default=None)
    p_classify.set_defaults(func=_cmd_classify_child_stdout)

    # Issue #2242 OWNER adversarial review Blocker 2/3
    # (https://github.com/squne121/loop-protocol/pull/2246#issuecomment-5328161000):
    # `--artifact-root`/`--artifact-relative` replace the prior single
    # `--artifact-path` so `readback_persisted_artifact()` can fully delegate
    # to `reviewer_transport.verify_artifact()`'s dir-fd-anchored, no-follow
    # traversal (which requires a trusted root + a validated repo-relative
    # path, not an arbitrary pre-joined filesystem path). Every other
    # `--expected-*` flag is a caller-supplied, artifact-independent expected
    # binding value (never re-derived from the artifact being verified).
    p_readback = sub.add_parser("readback", help="Readback a persisted compact artifact")
    p_readback.add_argument("--artifact-root", required=True)
    p_readback.add_argument("--artifact-relative", required=True)
    p_readback.add_argument("--expected-repo", required=True)
    p_readback.add_argument("--expected-issue", type=int, required=True)
    p_readback.add_argument("--expected-body-sha256", required=True)
    p_readback.add_argument("--expected-invocation-id", required=True)
    p_readback.add_argument("--expected-attempt", type=int, required=True)
    p_readback.add_argument("--expected-artifact-sha256", required=True)
    p_readback.add_argument("--expected-verdict", required=True)
    p_readback.set_defaults(func=_cmd_readback)

    p_gate = sub.add_parser("gate-final-review", help="Decide if final review may run")
    p_gate.add_argument("--artifact-root", required=True)
    p_gate.add_argument("--artifact-relative", required=True)
    p_gate.add_argument("--expected-repo", required=True)
    p_gate.add_argument("--expected-issue", type=int, required=True)
    p_gate.add_argument("--expected-body-sha256", required=True)
    p_gate.add_argument("--expected-invocation-id", required=True)
    p_gate.add_argument("--expected-attempt", type=int, required=True)
    p_gate.add_argument("--expected-artifact-sha256", required=True)
    p_gate.add_argument("--expected-verdict", required=True)
    p_gate.add_argument("--remote-update-ok", action="store_true")
    p_gate.set_defaults(func=_cmd_gate_final_review)

    p_contract = sub.add_parser("check-agent-contract", help="Static read-only/workspace-write contract check")
    p_contract.add_argument("--toml-file", required=True)
    p_contract.set_defaults(func=_cmd_check_agent_contract)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

