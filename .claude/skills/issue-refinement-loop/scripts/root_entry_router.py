#!/usr/bin/env python3
"""Root-owned synchronous entry transition (Issue #2272, root-direct redesign).

Implements ``ROOT_IMPLEMENTATION_ENTRY_ROUTE_V1``, a process-local,
non-persistent route result produced and consumed within a SINGLE continuous
call (``run_root_transition``). There is no separate producer/consumer
process boundary and no re-presentable authorization token: the root/main
thread calls ``run_root_transition`` once per attempt, and that same call
fetches live state, runs a current-run ``issue-contract-review``, decides the
route, and -- only when authorized -- invokes Step 1 directly, all within one
Python call, in the SAME continuous turn (a subprocess CLI invocation of this
module cannot itself drive the orchestrating turn's tool calls -- see the CLI
entry point note below; the continuity guarantee is turn-level, not a single
literal call stack when this module is run out-of-process). Nothing about the route or the review result is ever
accepted as caller-supplied input; every value that feeds the routing
decision is fetched or computed by this function itself.

This module deliberately does NOT provide any save/load/persist API: the
route result is a plain ``dict`` returned to the immediate caller and MUST
NOT be written to a GitHub comment, artifact file, digest, or environment
variable as an authorization credential (see ``docs/dev/workflow.md`` and the
Issue #2272 "Root-Owned Synchronous Entry Transition" section for the
normative policy).

Schema (fixed, do not add/remove keys -- #2272 Stop Condition):

    ROOT_IMPLEMENTATION_ENTRY_ROUTE_V1:
      route: invoke_impl_review_loop | rerun_contract_review
             | rerun_base_preflight | stop
      reason: <string>
      issue_number: <int>
      reviewed_body_sha256: <string | null>
      observed_live_body_sha256: <string | null>
      reviewed_base_sha: <string | null>
      observed_base_sha: <string | null>
      resume_from: <string | null>
      retry_count: <int>

Routing priority (lower number wins when multiple conditions are true):

    1. capability / live-fetch / identity unverifiable  -> stop
    2. Issue body / Allowed Paths drift                  -> rerun_contract_review
    3. base SHA drift only                                -> rerun_base_preflight
       (bounded retry, MAX_BASE_PREFLIGHT_RETRIES=3, owned by this function's
       own loop -- never a caller-supplied counter; exhausted -> stop)
    4. verdict blocked / request_changes                  -> stop
    5. current-run go AND live equality                   -> invoke_impl_review_loop

Correlation vs. authorization (P0-1 fix): ``generate_root_invocation_nonce``
produces a process-local id used ONLY for audit/log correlation. It is never
compared against anything, never required to match, and never gates
``invoked``. Authorization is entirely a function of live-fetched state
compared inside this same call -- not of any id, token, or prior artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

ROUTE_INVOKE = "invoke_impl_review_loop"
ROUTE_RERUN_CONTRACT_REVIEW = "rerun_contract_review"
ROUTE_RERUN_BASE_PREFLIGHT = "rerun_base_preflight"
ROUTE_STOP = "stop"

VALID_ROUTES = frozenset(
    {ROUTE_INVOKE, ROUTE_RERUN_CONTRACT_REVIEW, ROUTE_RERUN_BASE_PREFLIGHT, ROUTE_STOP}
)

MAX_BASE_PREFLIGHT_RETRIES = 3

_ROUTE_V1_KEYS = (
    "route",
    "reason",
    "issue_number",
    "reviewed_body_sha256",
    "observed_live_body_sha256",
    "reviewed_base_sha",
    "observed_base_sha",
    "resume_from",
    "retry_count",
)


def compute_body_sha256(body: str) -> str:
    """Canonicalization matches issue-contract-review's ``sha256_of``
    convention (``sha256:<64 hex>``) without importing that skill's module
    (Out of Scope: `.claude/skills/issue-contract-review/**` is not edited
    by this module -- see #2272 Out of Scope). ``run_root_transition`` DOES
    call that skill's existing ``run_once`` production entry point (a
    read-only current-run review invocation, not a parser/validator
    change)."""

    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def generate_root_invocation_nonce(prompt_id: Optional[str] = None) -> str:
    """Process-local correlation id for the current root invocation.

    NON-AUTHORITATIVE (P0-1): this value is used only to tag audit/log
    output. It is never compared against a prior value, never required to
    match anything, and never gates whether Step 1 is invoked. It is never
    restored across process restarts -- every call mints a fresh value (or
    derives a fresh ``prompt:<id>`` label from the CURRENT host-supplied
    prompt_id, itself never read from a file or persisted environment
    value).
    """

    if prompt_id:
        return f"prompt:{prompt_id}"
    return f"nonce:{uuid.uuid4().hex}"


@dataclass
class RootEntryRequest:
    issue_number: int
    capability_ok: bool
    live_fetch_ok: bool
    identity_ok: bool
    reviewed_body_sha256: Optional[str]
    reviewed_base_sha: Optional[str]
    observed_live_body_sha256: Optional[str]
    observed_base_sha: Optional[str]
    review_verdict: Optional[str]  # "go" | "blocked" | "request_changes" | None
    retry_count: int = 0
    mutation_partial_failure_phase: Optional[str] = None


def _route_result(
    req: RootEntryRequest,
    route: str,
    reason: str,
    *,
    resume_from: Optional[str] = None,
    retry_count: Optional[int] = None,
) -> dict:
    assert route in VALID_ROUTES, f"invalid route: {route}"
    return {
        "route": route,
        "reason": reason,
        "issue_number": req.issue_number,
        "reviewed_body_sha256": req.reviewed_body_sha256,
        "observed_live_body_sha256": req.observed_live_body_sha256,
        "reviewed_base_sha": req.reviewed_base_sha,
        "observed_base_sha": req.observed_base_sha,
        "resume_from": resume_from,
        "retry_count": req.retry_count if retry_count is None else retry_count,
    }


def _stop_route(
    issue_number: int,
    reason: str,
    *,
    reviewed_body_sha256: Optional[str] = None,
    observed_live_body_sha256: Optional[str] = None,
    reviewed_base_sha: Optional[str] = None,
    observed_base_sha: Optional[str] = None,
    resume_from: Optional[str] = None,
    retry_count: int = 0,
) -> dict:
    return {
        "route": ROUTE_STOP,
        "reason": reason,
        "issue_number": issue_number,
        "reviewed_body_sha256": reviewed_body_sha256,
        "observed_live_body_sha256": observed_live_body_sha256,
        "reviewed_base_sha": reviewed_base_sha,
        "observed_base_sha": observed_base_sha,
        "resume_from": resume_from,
        "retry_count": retry_count,
    }


_MUTATION_RESUME_POINTS = {
    "capability_preflight": "capability_preflight",
    "live_fetch": "live_fetch",
    "contract_review": "contract_review",
    "base_preflight": "base_preflight",
    "audit_comment": "audit_comment",
}


def resume_from_after_mutation_failure(failed_phase: str) -> str:
    """Fallback ``resume_from`` for phases that never perform a real
    mutation (capability_preflight/live_fetch/contract_review/
    base_preflight are read-only fetches; a "failure" there is just a fetch
    failure the normal capability/identity stop already covers). Kept for
    ``decide_root_entry_route``'s pure/no-I/O unit-test surface. The ONE
    phase this router actually mutates state for -- ``audit_comment`` -- is
    NOT resolved by this echo; ``run_root_transition`` resolves it via
    ``derive_resume_from_live_readback`` instead (AC10 fix, P1-1)."""

    return _MUTATION_RESUME_POINTS.get(failed_phase, failed_phase)


def compute_audit_invocation_marker(
    reviewed_body_sha256: Optional[str], reviewed_base_sha: Optional[str]
) -> str:
    """Deterministic marker derived purely from the values THIS invocation
    observes (body sha + base sha) -- never persisted, never caller-
    supplied, reproducible by any later call that observes the same live
    state. Used to identify whether a prior attempt's audit comment for
    this exact reviewed state was already published."""

    raw = f"{reviewed_body_sha256}:{reviewed_base_sha}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _audit_marker_tag(invocation_marker: str) -> str:
    return f"<!-- root-entry-audit-marker: {invocation_marker} -->"


def derive_resume_from_live_readback(
    transport: "GitHubEntryTransport", issue_number: int, invocation_marker: str
) -> str:
    """AC10 (P1-1 fix): after a partial ``audit_comment`` mutation failure,
    derive ``resume_from`` from a LIVE re-read of the issue's comments --
    not from the caller-supplied failure-phase string. If a comment
    carrying THIS marker (recomputed from the currently-observed body/base
    sha, not restored from any prior artifact) is already present, the
    earlier publish attempt actually succeeded and no further action is
    needed; otherwise it genuinely did not complete and must be retried."""

    try:
        comments = transport.list_recent_comments(issue_number)
    except Exception:
        return "publish_audit_comment"
    marker_tag = _audit_marker_tag(invocation_marker)
    for body in comments:
        if isinstance(body, str) and marker_tag in body:
            return "post_audit_comment_complete"
    return "publish_audit_comment"


def decide_root_entry_route(req: RootEntryRequest) -> dict:
    """Pure routing decision function -- no I/O. AC1-AC6, AC10-AC13."""

    if req.mutation_partial_failure_phase:
        return _route_result(
            req,
            ROUTE_STOP,
            "mutation_partial_failure",
            resume_from=resume_from_after_mutation_failure(
                req.mutation_partial_failure_phase
            ),
        )

    if not (req.capability_ok and req.live_fetch_ok and req.identity_ok):
        return _route_result(req, ROUTE_STOP, "capability_or_identity_unverifiable")

    if req.reviewed_body_sha256 != req.observed_live_body_sha256:
        return _route_result(req, ROUTE_RERUN_CONTRACT_REVIEW, "body_drift_detected")

    if req.reviewed_base_sha != req.observed_base_sha:
        if req.retry_count >= MAX_BASE_PREFLIGHT_RETRIES:
            return _route_result(
                req,
                ROUTE_STOP,
                "base_preflight_retry_exhausted",
                resume_from="base_preflight",
            )
        return _route_result(
            req,
            ROUTE_RERUN_BASE_PREFLIGHT,
            "base_sha_drift_detected",
            resume_from="base_preflight",
            retry_count=req.retry_count + 1,
        )

    if req.review_verdict in ("blocked", "request_changes"):
        return _route_result(req, ROUTE_STOP, f"review_verdict_{req.review_verdict}")

    if req.review_verdict == "go":
        return _route_result(req, ROUTE_INVOKE, "fresh_review_go_live_equality")

    return _route_result(req, ROUTE_STOP, "review_verdict_missing_or_unrecognized")


def validate_route_schema(route: object, *, expected_issue_number: int) -> Optional[str]:
    """In-process schema gate (P1-2). Runs in the SAME Python call as
    ``decide_root_entry_route`` within a single ``run_root_transition``
    invocation -- not a separate subprocess, not gated by
    any token. Rejects malformed/incomplete routes and cross-checks the
    issue number against the value this SAME invocation was called with
    (never a value read back out of the route object itself)."""

    if not isinstance(route, dict):
        return "invalid_route_type"
    if set(route.keys()) != set(_ROUTE_V1_KEYS):
        return "invalid_route_schema_keys"
    if route.get("route") not in VALID_ROUTES:
        return "invalid_route_value"
    if route.get("route") != ROUTE_INVOKE:
        return f"route_not_invoke:{route.get('route')}"
    issue_number = route.get("issue_number")
    if not isinstance(issue_number, int) or issue_number != expected_issue_number:
        return "missing_or_mismatched_issue_number"
    retry_count = route.get("retry_count")
    if not isinstance(retry_count, int) or retry_count < 0:
        return "invalid_retry_count"
    return None


class GitHubEntryTransport(Protocol):
    """Production-shape transport interface. ``GhCliGitHubEntryTransport``
    talks to the real GitHub API via ``gh``; tests inject
    ``FileBackedFakeGitHubEntryTransport``, which satisfies this same
    interface (dependency injection, not internal monkeypatching)."""

    def capability_preflight(self) -> bool: ...

    def fetch_live_issue(self, issue_number: int) -> dict: ...  # {"body", "base_sha", "identity_ok", "fetch_ok"}

    def post_comment(self, issue_number: int, body: str) -> dict: ...  # {"ok", "comment_id"?}

    def canonical_repository_identity(self) -> str: ...

    def list_recent_comments(self, issue_number: int) -> list: ...


@dataclass
class FileBackedFakeGitHubEntryTransport:
    """Fake transport for tests. Reads canned state from a JSON fixture file
    so the same interface can be constructed inside a fresh subprocess (no
    in-memory callables cross process boundaries) for CLI-level tests."""

    fixture_path: str
    _state: dict = field(default_factory=dict, repr=False)
    _posted: list = field(default_factory=list, repr=False)
    _fetch_calls: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        with open(self.fixture_path, "r", encoding="utf-8") as fh:
            self._state = json.load(fh)

    def capability_preflight(self) -> bool:
        return bool(self._state.get("capability_ok", True))

    def fetch_live_issue(self, issue_number: int) -> dict:
        self._fetch_calls += 1
        issues = self._state.get("issues", {})
        record = issues.get(str(issue_number), {})
        # Fixtures may declare a list under "base_sha_sequence" to simulate
        # base drift resolving after N re-fetches (bounded-retry tests).
        seq = record.get("base_sha_sequence")
        if seq:
            idx = min(self._fetch_calls - 1, len(seq) - 1)
            base_sha = seq[idx]
        else:
            base_sha = record.get("base_sha", "")
        return {
            "body": record.get("body", ""),
            "base_sha": base_sha,
            "identity_ok": record.get("identity_ok", True),
            "fetch_ok": record.get("fetch_ok", True),
        }

    def post_comment(self, issue_number: int, body: str) -> dict:
        if not self._state.get("audit_publish_ok", True):
            raise RuntimeError("simulated audit comment publish failure")
        self._posted.append({"issue_number": issue_number, "body": body})
        return {"ok": True, "comment_id": len(self._posted)}

    def canonical_repository_identity(self) -> str:
        return self._state.get("repo_identity", "fake/repo")

    def list_recent_comments(self, issue_number: int) -> list:
        issues = self._state.get("issues", {})
        record = issues.get(str(issue_number), {})
        fixture_comments = list(record.get("comments", []))
        posted_bodies = [
            entry["body"] for entry in self._posted if entry["issue_number"] == issue_number
        ]
        return fixture_comments + posted_bodies


def capability_preflight_result(
    repo: str,
    spark_mode: str | None = None,
    spark_fallback: str | None = None,
    planned_operations: "tuple[dict, ...] | list[dict]" = (),
) -> dict:
    """Invoke ``scripts/claude-gpt/workflow_capability_preflight.py`` once and
    return the structured ``CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1`` result
    verbatim (``decision`` / ``checks`` / ``reasons`` preserved -- Issue #2311
    P0-4/AC6/AC7). This is a module-level function (not a transport method)
    so callers such as ``workflow_start_entry.py`` can obtain the structured
    result without constructing a ``GhCliGitHubEntryTransport`` and without
    reimplementing the producer-invocation logic (Issue #2311 AC2).

    Read-only: the underlying script performs no GitHub mutation of its own
    (Issue #2273 AC12).

    Fail-closed by construction: a subprocess invocation failure (missing
    script, non-zero exit, timeout, OSError) or a malformed/non-JSON/
    non-dict stdout payload is never raised to the caller -- it is
    normalized into a synthetic ``decision: blocked`` result with a
    ``reasons`` entry describing the failure, so every caller sees the same
    3-value ``decision`` contract (``ready`` / ``degraded`` / ``blocked``)
    regardless of whether the producer itself declared ``blocked`` or the
    invocation failed outright (Issue #2311 AC3).
    """
    repo_root = Path(__file__).resolve().parents[4]
    preflight_script = (
        repo_root / "scripts" / "claude-gpt" / "workflow_capability_preflight.py"
    )
    argv = [
        sys.executable,
        str(preflight_script),
        "--profile",
        "issue-to-impl",
        "--repo",
        repo,
    ]
    if spark_mode is not None:
        argv.extend(["--spark-mode", spark_mode])
    if spark_fallback is not None:
        argv.extend(["--spark-fallback", spark_fallback])
    planned_ops_path: Optional[str] = None
    try:
        if planned_operations:
            import tempfile

            tmp = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                prefix="planned-operations-",
                delete=False,
                encoding="utf-8",
            )
            try:
                json.dump(list(planned_operations), tmp)
            finally:
                tmp.close()
            planned_ops_path = tmp.name
            argv.extend(["--planned-operations-json", planned_ops_path])

        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 -- fail-closed transport boundary
            return {
                "decision": "blocked",
                "checks": {},
                "reasons": [f"producer_invocation_failed:{exc.__class__.__name__}"],
            }
        if proc.returncode != 0:
            return {
                "decision": "blocked",
                "checks": {},
                "reasons": [f"producer_invocation_failed:exit_{proc.returncode}"],
            }
        try:
            result = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            return {
                "decision": "blocked",
                "checks": {},
                "reasons": ["producer_result_malformed:non_json_stdout"],
            }
        if not isinstance(result, dict) or result.get("decision") not in (
            "ready",
            "degraded",
            "blocked",
        ):
            return {
                "decision": "blocked",
                "checks": {},
                "reasons": ["producer_result_malformed:invalid_decision"],
            }
        return {
            "decision": result.get("decision"),
            "checks": result.get("checks", {}),
            "reasons": result.get("reasons", []),
        }
    finally:
        if planned_ops_path is not None:
            try:
                Path(planned_ops_path).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001 -- best-effort temp file cleanup
                pass


@dataclass
class GhCliGitHubEntryTransport:
    """Production transport: talks to the REAL GitHub API via the ``gh``
    CLI. This is the default transport used by the CLI entrypoint when
    ``--fake-transport-file`` is NOT supplied (P0-3 production carrier)."""

    repo: str
    base_ref: str = "main"
    spark_mode: str | None = None
    spark_fallback: str | None = None
    planned_operations: tuple[dict, ...] = field(
        default_factory=lambda: (
            {
                "phase": "audit",
                "actor_role": "root_entry_router",
                "operation": "issue_comment",
                "requires_mutation": True,
            },
        )
    )

    def capability_preflight(self) -> bool:
        """Backward-compatible boolean adapter (Issue #2311 P0-4/AC6) over
        the structured module-level ``capability_preflight_result()``.
        ``decision: ready`` or ``decision: degraded`` route to ``True``
        (proceed to the fresh-review phase); ``decision: blocked`` (which
        ``capability_preflight_result()`` also synthesizes on any producer
        invocation failure -- missing script, non-JSON stdout, non-zero
        exit, timeout) routes to ``False`` (fail closed, matching this
        gate's prior behavior, Issue #2273 AC15). This call is read-only:
        the underlying module performs no GitHub mutation of its own
        (Issue #2273 AC12). Existing callers of this method (Step 5 /
        ``run_root_transition`` and its existing tests, e.g.
        ``test_ac11_capability_preflight_blocked_stops_before_mutation``)
        are unaffected by the Issue #2311 refactor -- this method's
        observable behavior is unchanged.
        """
        result = capability_preflight_result(
            repo=self.repo,
            spark_mode=self.spark_mode,
            spark_fallback=self.spark_fallback,
            planned_operations=self.planned_operations,
        )
        return result.get("decision") in ("ready", "degraded")

    def fetch_live_issue(self, issue_number: int) -> dict:
        try:
            proc = subprocess.run(
                [
                    "gh",
                    "issue",
                    "view",
                    str(issue_number),
                    "--repo",
                    self.repo,
                    "--json",
                    "body",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                return {"body": "", "base_sha": None, "identity_ok": False, "fetch_ok": False}
            body = json.loads(proc.stdout).get("body", "")
        except Exception:
            return {"body": "", "base_sha": None, "identity_ok": False, "fetch_ok": False}

        try:
            ref_proc = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{self.repo}/git/refs/heads/{self.base_ref}",
                    "--jq",
                    ".object.sha",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            base_sha = ref_proc.stdout.strip() if ref_proc.returncode == 0 else None
        except Exception:
            base_sha = None

        return {
            "body": body,
            "base_sha": base_sha,
            "identity_ok": bool(base_sha),
            "fetch_ok": True,
        }

    def post_comment(self, issue_number: int, body: str) -> dict:
        try:
            proc = subprocess.run(
                ["gh", "issue", "comment", str(issue_number), "--repo", self.repo, "--body", body],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr)
            return {"ok": True, "comment_id": None}
        except Exception as exc:
            raise RuntimeError(f"gh issue comment failed: {exc}") from exc

    def canonical_repository_identity(self) -> str:
        proc = subprocess.run(
            ["gh", "repo", "view", self.repo, "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr)
        return proc.stdout.strip()

    def list_recent_comments(self, issue_number: int) -> list:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                self.repo,
                "--json",
                "comments",
                "--jq",
                "[.comments[].body]",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr)
        return json.loads(proc.stdout)


def publish_audit_comment_best_effort(
    transport: GitHubEntryTransport, issue_number: int, route: dict
) -> dict:
    """AC16: audit comment publish is best-effort and happens after the
    route has already been decided. Failure never mutates ``route`` and is
    only recorded as a warning."""

    try:
        marker = compute_audit_invocation_marker(
            route.get("reviewed_body_sha256"), route.get("reviewed_base_sha")
        )
        body = (
            "## root-owned entry router audit\n\n"
            f"route: {route['route']}\nreason: {route['reason']}\n\n"
            f"{_audit_marker_tag(marker)}\n"
        )
        result = transport.post_comment(issue_number, body)
        return {"published": bool(result.get("ok")), "warning": None}
    except Exception as exc:  # noqa: BLE001 - best-effort, never raises
        return {"published": False, "warning": f"audit_publish_failed: {exc}"}


def run_root_transition(
    *,
    issue_number: int,
    repo: str,
    transport: GitHubEntryTransport,
    contract_reviewer: Callable[..., dict],
    invoke_step1: Callable[[], object] = lambda: None,
    mode: str = "static",
    max_base_retries: int = MAX_BASE_PREFLIGHT_RETRIES,
    mutation_partial_failure_phase: Optional[str] = None,
    prompt_id: Optional[str] = None,
    publish_audit: bool = True,
    expected_repository_identity: Optional[str] = None,
) -> dict:
    """Single continuous root-owned entry point (P0-1/P0-2/P0-3/P1-1/P1-2
    fix). Every input that feeds the routing decision (live body, live base,
    review verdict, reviewed body hash) is fetched or computed by THIS call,
    never accepted as a caller-supplied claim. ``invoke_step1`` is called at
    most once, and only after live-derived equality and an exact schema
    check both pass within this same function call -- there is no route object,
    token, or envelope handed to a separate process/consumer for later
    replay (AC14/AC17/AC18 satisfied by construction: nothing here accepts a
    previously-produced route as input at all).

    Returns ``{"route": ROUTE_V1, "invoked": bool, "correlation_id": str,
    "audit": {...}}``. ``correlation_id`` is log-only (see
    ``generate_root_invocation_nonce``) and never gates ``invoked``.
    """

    correlation_id = generate_root_invocation_nonce(prompt_id)

    def _finish(route: dict, invoked: bool, *, audit_eligible: bool) -> dict:
        audit = {"published": None, "warning": None}
        if publish_audit and audit_eligible:
            audit = publish_audit_comment_best_effort(transport, issue_number, route)
        return {
            "route": route,
            "invoked": invoked,
            "correlation_id": correlation_id,
            "audit": audit,
        }

    # AC10/AC11: a previously recorded partial mutation failure takes
    # precedence over everything else, including any fetch. The ONE real
    # mutation this router performs is the audit comment publish
    # (publish_audit_comment_best_effort); resuming from an interrupted
    # publish is resolved via a LIVE readback of the issue's comments
    # (derive_resume_from_live_readback), not from the caller-supplied
    # phase string echoed back verbatim (P1-1 fix). Non-mutation phases
    # (capability_preflight/live_fetch/contract_review/base_preflight are
    # read-only fetches, never partial mutations) keep the plain
    # phase-name fallback since there is no live state to read back for
    # them.
    if mutation_partial_failure_phase == "audit_comment":
        try:
            capability_ok = bool(transport.capability_preflight())
        except Exception:
            capability_ok = False
        if not capability_ok:
            return _finish(
                _stop_route(issue_number, "capability_or_identity_unverifiable"),
                False,
                audit_eligible=False,
            )

        try:
            live0 = transport.fetch_live_issue(issue_number)
            fetch_ok0 = bool(live0.get("fetch_ok", True)) and bool(live0.get("identity_ok", True))
        except Exception:
            fetch_ok0, live0 = False, {}
        if not fetch_ok0:
            return _finish(
                _stop_route(issue_number, "capability_or_identity_unverifiable"),
                False,
                audit_eligible=False,
            )

        observed_body_sha0 = compute_body_sha256(live0.get("body", ""))
        observed_base_sha0 = live0.get("base_sha")
        marker0 = compute_audit_invocation_marker(observed_body_sha0, observed_base_sha0)
        resume_from0 = derive_resume_from_live_readback(transport, issue_number, marker0)

        if resume_from0 == "post_audit_comment_complete":
            # Live readback proves the earlier publish for this exact
            # reviewed state already landed -- nothing further to do, and
            # we do not publish a duplicate audit comment for it.
            route = _stop_route(
                issue_number,
                "mutation_partial_failure_already_complete",
                observed_live_body_sha256=observed_body_sha0,
                observed_base_sha=observed_base_sha0,
                resume_from=resume_from0,
            )
            return {
                "route": route,
                "invoked": False,
                "correlation_id": correlation_id,
                "audit": {"published": None, "warning": None},
            }
        # Live readback proves the publish genuinely never completed --
        # fall through to the normal flow below so this attempt retries
        # the full fresh-review-and-route sequence (and, if it reaches
        # ROUTE_INVOKE, publishes the audit comment for real).
    elif mutation_partial_failure_phase:
        route = _stop_route(
            issue_number,
            "mutation_partial_failure",
            resume_from=resume_from_after_mutation_failure(mutation_partial_failure_phase),
        )
        return _finish(route, False, audit_eligible=False)

    try:
        capability_ok = bool(transport.capability_preflight())
    except Exception:
        capability_ok = False
    if not capability_ok:
        return _finish(
            _stop_route(issue_number, "capability_or_identity_unverifiable"),
            False,
            audit_eligible=False,
        )

    if expected_repository_identity is None:
        return _finish(
            _stop_route(issue_number, "expected_repository_identity_required"),
            False,
            audit_eligible=False,
        )
    try:
        observed_identity = transport.canonical_repository_identity()
    except Exception:
        observed_identity = None
    if observed_identity != expected_repository_identity:
        return _finish(
            _stop_route(issue_number, "repository_identity_mismatch"),
            False,
            audit_eligible=False,
        )

    def _fetch() -> tuple[bool, dict]:
        try:
            live = transport.fetch_live_issue(issue_number)
            ok = bool(live.get("fetch_ok", True)) and bool(live.get("identity_ok", True))
            return ok, live
        except Exception:
            return False, {}

    fetch_ok, live = _fetch()
    if not fetch_ok:
        return _finish(
            _stop_route(issue_number, "capability_or_identity_unverifiable"),
            False,
            audit_eligible=False,
        )

    reviewed_base_sha = live.get("base_sha")
    retry_count = 0
    route: dict = _stop_route(issue_number, "unreachable")

    while True:
        try:
            review_result = contract_reviewer(
                issue_number=issue_number, repo=repo, mode=mode, reviewed_head_sha=reviewed_base_sha
            )
        except Exception:
            return _finish(
                _stop_route(issue_number, "contract_reviewer_transport_failure"),
                False,
                audit_eligible=True,
            )

        if not isinstance(review_result, dict):
            return _finish(
                _stop_route(issue_number, "contract_reviewer_malformed_result"),
                False,
                audit_eligible=True,
            )

        review_verdict = "go" if review_result.get("status") == "go" else "blocked"
        reviewed_body_sha256 = review_result.get("body_sha256")

        fetch_ok2, live2 = _fetch()
        if not fetch_ok2:
            return _finish(
                _stop_route(issue_number, "capability_or_identity_unverifiable"),
                False,
                audit_eligible=True,
            )

        observed_live_body_sha256 = compute_body_sha256(live2.get("body", ""))
        observed_base_sha = live2.get("base_sha")

        req = RootEntryRequest(
            issue_number=issue_number,
            capability_ok=True,
            live_fetch_ok=True,
            identity_ok=True,
            reviewed_body_sha256=reviewed_body_sha256,
            reviewed_base_sha=reviewed_base_sha,
            observed_live_body_sha256=observed_live_body_sha256,
            observed_base_sha=observed_base_sha,
            review_verdict=review_verdict,
            retry_count=retry_count,
        )
        route = decide_root_entry_route(req)

        if route["route"] == ROUTE_RERUN_BASE_PREFLIGHT and route["retry_count"] <= max_base_retries:
            # Root-owned retry counter: `retry_count` is a local loop
            # variable, never accepted from any caller, so it cannot be
            # reset externally (P1-1 fix). Re-pin to the freshly observed
            # base before the next attempt.
            retry_count = route["retry_count"]
            reviewed_base_sha = observed_base_sha
            continue
        break

    invoked = False
    if route["route"] == ROUTE_INVOKE:
        schema_error = validate_route_schema(route, expected_issue_number=issue_number)
        if schema_error is not None:
            route = _stop_route(
                issue_number,
                schema_error,
                reviewed_body_sha256=route.get("reviewed_body_sha256"),
                observed_live_body_sha256=route.get("observed_live_body_sha256"),
                reviewed_base_sha=route.get("reviewed_base_sha"),
                observed_base_sha=route.get("observed_base_sha"),
                retry_count=route.get("retry_count", 0),
            )
        else:
            invoke_step1()
            invoked = True

    return _finish(route, invoked, audit_eligible=True)


# ---------------------------------------------------------------------------
# CLI entry point. Computes and prints the routing decision only -- it does
# NOT itself invoke impl-review-loop Step 1 (a subprocess cannot drive this
# session's tool calls). The orchestrating root/main thread runs this CLI,
# reads `invoked` from the SAME continuous turn, and -- only if true --
# immediately proceeds to invoke impl-review-loop itself, in the same
# session, with no intervening persistence of the route (P0-3).
# ---------------------------------------------------------------------------


def _select_transport(args: argparse.Namespace) -> GitHubEntryTransport:
    if args.fake_transport_file:
        return FileBackedFakeGitHubEntryTransport(args.fake_transport_file)
    return GhCliGitHubEntryTransport(repo=args.repo, base_ref=args.base_ref)


def _select_contract_reviewer(args: argparse.Namespace) -> Callable[..., dict]:
    if args.fake_contract_review_file:
        with open(args.fake_contract_review_file, "r", encoding="utf-8") as fh:
            canned = json.load(fh)

        def _fake_reviewer(**_kwargs: Any) -> dict:
            return canned

        return _fake_reviewer

    # Production: import (not shell out to) issue-contract-review's existing
    # run_once() -- a plain function call in the SAME process, not a
    # separate subprocess whose stdout a caller could forge. This module
    # never modifies `.claude/skills/issue-contract-review/**` (Out of
    # Scope); it only imports the existing production entry point.
    scripts_dir = str(
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "issue-contract-review"
        / "scripts"
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import run_contract_review_once as _icr  # noqa: E402

    def _real_reviewer(*, issue_number: int, repo: str, mode: str, reviewed_head_sha: Optional[str]) -> dict:
        # P0-1 dedupe-bypass fix: root-direct requires a FRESH current-run
        # review every attempt. run_once()'s default (skip_idempotency_check=
        # False) short-circuits on a matching prior "go" comment without
        # re-running readiness/blockers/product_spec/vc_preflight -- that
        # short-circuit is correct for impl-review-loop's own iteration
        # cache, but it is wrong for this entry gate, which must never treat
        # a stale trusted comment as equivalent to a check that ran just now.
        return _icr.run_once(
            issue_number=issue_number,
            repo=repo,
            mode=mode,
            reviewed_head_sha=reviewed_head_sha,
            skip_idempotency_check=True,
        )

    return _real_reviewer


def _main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base-ref", default="main")
    parser.add_argument("--mode", default="static")
    parser.add_argument("--fake-transport-file", default=None)
    parser.add_argument("--fake-contract-review-file", default=None)
    parser.add_argument("--prompt-id", default=None)
    parser.add_argument("--no-audit", action="store_true")
    args = parser.parse_args(argv)

    transport = _select_transport(args)
    contract_reviewer = _select_contract_reviewer(args)

    # P0-2 identity-bypass fix: the production CLI always binds identity
    # verification to the operator-supplied --repo value. There is no CLI
    # flag to opt out of this check -- expected_repository_identity is never
    # None on the production path, so canonical_repository_identity() is
    # always cross-checked against the trusted --repo argument.
    result = run_root_transition(
        issue_number=args.issue_number,
        repo=args.repo,
        transport=transport,
        contract_reviewer=contract_reviewer,
        invoke_step1=lambda: None,  # CLI never invokes Step 1 itself; see module docstring
        mode=args.mode,
        prompt_id=args.prompt_id,
        publish_audit=not args.no_audit,
        expected_repository_identity=args.repo,
    )
    sys.stdout.write(json.dumps(result))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
