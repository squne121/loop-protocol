#!/usr/bin/env python3
"""Root-owned synchronous entry transition router (producer side, #2272).

Implements ``ROOT_IMPLEMENTATION_ENTRY_ROUTE_V1`` a process-local,
non-persistent route result produced and consumed only within the current
root/main thread control flow. This module deliberately does NOT provide any
save/load/persist API: the route result is a plain ``dict`` returned to the
caller and MUST NOT be written to a GitHub comment, artifact file, digest, or
environment variable as an authorization token (see
``docs/dev/workflow.md`` and the Issue #2272 "Root-Owned Synchronous Entry
Transition" section for the normative policy).

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
       (bounded retry, MAX_BASE_PREFLIGHT_RETRIES=3; exhausted -> stop)
    4. verdict blocked / request_changes                  -> stop
    5. current-run go AND live equality                   -> invoke_impl_review_loop

No self-attested ``root_invocation_id`` is used for authorization. Process
correlation uses either a host-supplied ``prompt_id`` or a process-local
nonce that is never restored across process restarts (see
``generate_root_invocation_nonce``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass, field
from typing import Optional, Protocol

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
    (out of scope: `.claude/skills/issue-contract-review/**` is not edited or
    depended on by this module -- see #2272 Out of Scope)."""

    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def generate_root_invocation_nonce(prompt_id: Optional[str] = None) -> str:
    """Process-local identifier for the current root invocation.

    Never self-attested and never restored across process restarts (AC15).
    If a host-supplied ``prompt_id`` is available it is used for
    correlation; otherwise a fresh UUID4 nonce is minted. Callers MUST NOT
    persist the return value anywhere outside process memory.
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


_MUTATION_RESUME_POINTS = {
    "capability_preflight": "capability_preflight",
    "live_fetch": "live_fetch",
    "contract_review": "contract_review",
    "base_preflight": "base_preflight",
    "audit_comment": "audit_comment",
}


def resume_from_after_mutation_failure(failed_phase: str) -> str:
    """AC10: after a partial mutation failure, return a safe ``resume_from``
    computed purely from a fresh live readback context (the phase name that
    failed), never from stale local state."""

    return _MUTATION_RESUME_POINTS.get(failed_phase, failed_phase)


def decide_root_entry_route(req: RootEntryRequest) -> dict:
    """Pure routing decision function -- no I/O. AC1-AC6, AC10-AC13."""

    # AC10: a previously recorded partial mutation failure takes precedence
    # so that resume always starts from a live-readback-derived safe point,
    # never blindly continuing into mutation again.
    if req.mutation_partial_failure_phase:
        return _route_result(
            req,
            ROUTE_STOP,
            "mutation_partial_failure",
            resume_from=resume_from_after_mutation_failure(
                req.mutation_partial_failure_phase
            ),
        )

    # Priority 1: capability / live fetch / identity verification.
    # AC11: this check happens strictly before any Issue mutation is
    # attempted by callers (decide_root_entry_route performs no mutation
    # itself; callers must not mutate before consuming this route).
    if not (req.capability_ok and req.live_fetch_ok and req.identity_ok):
        return _route_result(req, ROUTE_STOP, "capability_or_identity_unverifiable")

    # Priority 2: body / Allowed Paths drift (review subject itself changed).
    if req.reviewed_body_sha256 != req.observed_live_body_sha256:
        return _route_result(req, ROUTE_RERUN_CONTRACT_REVIEW, "body_drift_detected")

    # Priority 3: base SHA drift only (bounded retry).
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

    # Priority 4: verdict blocked / request_changes.
    if req.review_verdict in ("blocked", "request_changes"):
        return _route_result(req, ROUTE_STOP, f"review_verdict_{req.review_verdict}")

    # Priority 5: current-run go AND live equality (already established by
    # reaching this point: reviewed_* == observed_*).
    if req.review_verdict == "go":
        return _route_result(req, ROUTE_INVOKE, "fresh_review_go_live_equality")

    # Missing / unrecognized verdict: never invoke; fail closed to stop so a
    # human or a fresh review cycle decides (never fall through silently).
    return _route_result(req, ROUTE_STOP, "review_verdict_missing_or_unrecognized")


class GitHubEntryTransport(Protocol):
    """Production-shape transport interface. A real implementation talks to
    the GitHub API (out of scope for #2272); tests inject a fake that
    satisfies this same interface (not internal monkeypatching)."""

    def capability_preflight(self) -> bool: ...

    def fetch_live_issue(self, issue_number: int) -> dict: ...  # {"body", "base_sha", "identity_ok"}

    def post_comment(self, issue_number: int, body: str) -> dict: ...  # {"ok", "comment_id"?}


@dataclass
class FileBackedFakeGitHubEntryTransport:
    """Fake transport for tests / subprocess CLI wiring. Reads canned state
    from a JSON fixture file so the same interface can be constructed inside
    a fresh subprocess (no in-memory callables cross process boundaries)."""

    fixture_path: str
    _state: dict = field(default_factory=dict, repr=False)
    _posted: list = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        with open(self.fixture_path, "r", encoding="utf-8") as fh:
            self._state = json.load(fh)

    def capability_preflight(self) -> bool:
        return bool(self._state.get("capability_ok", True))

    def fetch_live_issue(self, issue_number: int) -> dict:
        issues = self._state.get("issues", {})
        record = issues.get(str(issue_number), {})
        return {
            "body": record.get("body", ""),
            "base_sha": record.get("base_sha", ""),
            "identity_ok": record.get("identity_ok", True),
            "fetch_ok": record.get("fetch_ok", True),
        }

    def post_comment(self, issue_number: int, body: str) -> dict:
        if not self._state.get("audit_publish_ok", True):
            raise RuntimeError("simulated audit comment publish failure")
        self._posted.append({"issue_number": issue_number, "body": body})
        return {"ok": True, "comment_id": len(self._posted)}


def publish_audit_comment_best_effort(
    transport: GitHubEntryTransport, issue_number: int, route: dict
) -> dict:
    """AC16: audit comment publish is best-effort and happens after the
    route has already been decided. Failure never mutates ``route`` and is
    only recorded as a warning."""

    try:
        body = (
            "## root-owned entry router audit\n\n"
            f"route: {route['route']}\nreason: {route['reason']}\n"
        )
        result = transport.post_comment(issue_number, body)
        return {"published": bool(result.get("ok")), "warning": None}
    except Exception as exc:  # noqa: BLE001 - best-effort, never raises
        return {"published": False, "warning": f"audit_publish_failed: {exc}"}


def route_root_implementation_entry(
    *,
    issue_number: int,
    reviewed_body_sha256: Optional[str],
    reviewed_base_sha: Optional[str],
    review_verdict: Optional[str],
    transport: GitHubEntryTransport,
    retry_count: int = 0,
    mutation_partial_failure_phase: Optional[str] = None,
    prompt_id: Optional[str] = None,
    publish_audit: bool = True,
) -> dict:
    """End-to-end producer entry point used by the root/main thread. Returns
    an envelope: ``{"route": ROUTE_V1, "invocation_token": str, "audit": {...}}``.

    ``invocation_token`` is the process-local nonce (or prompt_id-derived
    correlation value) minted for THIS call. It is not part of the fixed
    ROUTE_V1 schema -- it is delivery-envelope metadata used by the consumer
    to reject replayed / cross-process-resumed route objects (AC14/AC18).
    """

    capability_ok = False
    live_fetch_ok = False
    identity_ok = False
    observed_live_body_sha256: Optional[str] = None
    observed_base_sha: Optional[str] = None

    try:
        capability_ok = bool(transport.capability_preflight())
    except Exception:
        capability_ok = False

    if capability_ok:
        try:
            live = transport.fetch_live_issue(issue_number)
            live_fetch_ok = bool(live.get("fetch_ok", True))
            identity_ok = bool(live.get("identity_ok", True))
            if live_fetch_ok:
                observed_live_body_sha256 = compute_body_sha256(live.get("body", ""))
                observed_base_sha = live.get("base_sha")
        except Exception:
            live_fetch_ok = False
            identity_ok = False

    req = RootEntryRequest(
        issue_number=issue_number,
        capability_ok=capability_ok,
        live_fetch_ok=live_fetch_ok,
        identity_ok=identity_ok,
        reviewed_body_sha256=reviewed_body_sha256,
        reviewed_base_sha=reviewed_base_sha,
        observed_live_body_sha256=observed_live_body_sha256,
        observed_base_sha=observed_base_sha,
        review_verdict=review_verdict,
        retry_count=retry_count,
        mutation_partial_failure_phase=mutation_partial_failure_phase,
    )
    route = decide_root_entry_route(req)

    invocation_token = generate_root_invocation_nonce(prompt_id)

    audit = {"published": None, "warning": None}
    if publish_audit and capability_ok:
        audit = publish_audit_comment_best_effort(transport, issue_number, route)

    return {"route": route, "invocation_token": invocation_token, "audit": audit}


# ---------------------------------------------------------------------------
# Subprocess CLI entry (AC8 process-level integration: strict JSON over
# stdin/stdout; the consumer runs in a SEPARATE subprocess -- see
# preparation_entry_router.py in impl-review-loop/scripts).
# ---------------------------------------------------------------------------


def _main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fake-transport-file", required=True)
    args = parser.parse_args(argv)

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid_stdin_json: {exc}"}), file=sys.stderr)
        return 2

    transport = FileBackedFakeGitHubEntryTransport(args.fake_transport_file)

    result = route_root_implementation_entry(
        issue_number=int(payload["issue_number"]),
        reviewed_body_sha256=payload.get("reviewed_body_sha256"),
        reviewed_base_sha=payload.get("reviewed_base_sha"),
        review_verdict=payload.get("review_verdict"),
        transport=transport,
        retry_count=int(payload.get("retry_count", 0)),
        mutation_partial_failure_phase=payload.get("mutation_partial_failure_phase"),
        prompt_id=payload.get("prompt_id"),
        publish_audit=bool(payload.get("publish_audit", True)),
    )
    sys.stdout.write(json.dumps(result))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
