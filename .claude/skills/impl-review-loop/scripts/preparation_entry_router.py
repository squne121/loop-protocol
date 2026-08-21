#!/usr/bin/env python3
"""impl-review-loop preparation entry router (consumer side, #2272).

Consumes the process-local ``ROOT_IMPLEMENTATION_ENTRY_ROUTE_V1`` produced by
``issue-refinement-loop/scripts/root_entry_router.py`` and decides whether to
invoke Step 1 (implementation-worker) for the current invocation.

Defense in depth (Issue #2272 AC13/AC14/AC18):

- The route object alone is never sufficient authorization. The consumer
  requires a matching ``invocation_token`` (delivery-envelope metadata, not
  part of the fixed ROUTE_V1 schema) supplied out-of-band by the SAME
  continuous root control flow that produced the route. A route
  reconstructed from a GitHub comment (e.g. a stale ``LOOP_HANDOFF_RESULT_V1``
  marker) never has a valid token and is rejected (AC7/AC18).
- Even when the token matches, the consumer independently re-fetches the
  live Issue (via the SAME transport interface, dependency-injected -- not
  monkeypatching internals) and recomputes body/base equality itself. This
  is a direct comparison (`body_sha256` / `base_sha`), NOT a reuse of the
  existing issue-contract-review comment-based fingerprint validator
  (comment ID / trusted author provenance is not checked here -- AC13). If
  live state drifted since the route was produced (e.g. a resumed/older
  process handed over a stale route), the consumer refuses to invoke and
  forces a fresh review instead (AC14).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol


def compute_body_sha256(body: str) -> str:
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


class GitHubEntryTransport(Protocol):
    """Same-shape transport interface as the producer side
    (`issue-refinement-loop/scripts/root_entry_router.py`). Defined
    independently here (not imported cross-skill) to keep each skill's
    `scripts/` self-contained; both fakes are constructed identically in
    tests to satisfy Process-Level Test Requirements (fake transport
    dependency-injected via the same interface, not monkeypatched
    internals)."""

    def fetch_live_issue(self, issue_number: int) -> dict: ...  # {"body", "base_sha"}


@dataclass
class FileBackedFakeGitHubEntryTransport:
    fixture_path: str
    _state: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        with open(self.fixture_path, "r", encoding="utf-8") as fh:
            self._state = json.load(fh)

    def fetch_live_issue(self, issue_number: int) -> dict:
        issues = self._state.get("issues", {})
        record = issues.get(str(issue_number), {})
        return {"body": record.get("body", ""), "base_sha": record.get("base_sha", "")}


REJECT_INVALID_ROUTE_TYPE = "invalid_route_type"
REJECT_TOKEN_MISSING_OR_MISMATCH = "invocation_token_mismatch_or_missing"
REJECT_ROUTE_NOT_INVOKE = "route_not_invoke"
REJECT_MISSING_ISSUE_NUMBER = "missing_issue_number"
REJECT_BODY_DRIFTED = "live_body_drifted_since_review"
REJECT_BASE_DRIFTED = "live_base_drifted_since_review"


def consume_root_entry_route(
    route: object,
    *,
    invocation_token: Optional[str],
    expected_invocation_token: Optional[str],
    transport: GitHubEntryTransport,
    invoke_step1: Callable[[], object],
) -> dict:
    """Returns ``{"invoked": bool, "reason": str | None}``.

    ``invoke_step1`` is called at most once, and only when every check
    passes. Negative cases (AC17) MUST leave invocation count at 0.
    """

    if not isinstance(route, dict):
        return {"invoked": False, "reason": REJECT_INVALID_ROUTE_TYPE}

    # AC7/AC18: a route reconstructed from a GitHub comment (comment-only
    # replay) never carries a valid, freshly-minted invocation_token that
    # matches what the current root control flow expects. Missing or
    # mismatched tokens are rejected unconditionally, before any other
    # field is even inspected.
    if (
        invocation_token is None
        or expected_invocation_token is None
        or invocation_token != expected_invocation_token
    ):
        return {"invoked": False, "reason": REJECT_TOKEN_MISSING_OR_MISMATCH}

    if route.get("route") != "invoke_impl_review_loop":
        return {
            "invoked": False,
            "reason": f"{REJECT_ROUTE_NOT_INVOKE}:{route.get('route')}",
        }

    issue_number = route.get("issue_number")
    if not isinstance(issue_number, int):
        return {"invoked": False, "reason": REJECT_MISSING_ISSUE_NUMBER}

    # AC13/AC14: independent live equality re-verification -- direct
    # body_sha256/base_sha comparison, not the comment-based fingerprint
    # validator. This is the defense-in-depth layer that rejects
    # cross-process-resumed / stale route objects even if a stale token
    # happened to be (incorrectly) accepted as "expected".
    fresh = transport.fetch_live_issue(issue_number)
    fresh_body_sha256 = compute_body_sha256(fresh.get("body", ""))
    if fresh_body_sha256 != route.get("reviewed_body_sha256"):
        return {"invoked": False, "reason": REJECT_BODY_DRIFTED}

    fresh_base_sha = fresh.get("base_sha")
    if fresh_base_sha != route.get("reviewed_base_sha"):
        return {"invoked": False, "reason": REJECT_BASE_DRIFTED}

    invoke_step1()
    return {"invoked": True, "reason": None}


# ---------------------------------------------------------------------------
# Subprocess CLI entry (AC8/AC17/AC18 process-level integration test target).
# Reads the envelope on stdin, invokes a spy (file-append side effect since a
# real python callable cannot cross the process boundary), writes the
# consumption result JSON to stdout.
# ---------------------------------------------------------------------------


def _main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fake-transport-file", required=True)
    parser.add_argument("--expected-invocation-token", required=True)
    parser.add_argument("--spy-file", required=True)
    args = parser.parse_args(argv)

    try:
        envelope = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"invalid_stdin_json: {exc}"}), file=sys.stderr)
        return 2

    transport = FileBackedFakeGitHubEntryTransport(args.fake_transport_file)

    def _invoke_step1() -> None:
        with open(args.spy_file, "a", encoding="utf-8") as fh:
            fh.write("invoked\n")

    result = consume_root_entry_route(
        envelope.get("route"),
        invocation_token=envelope.get("invocation_token"),
        expected_invocation_token=args.expected_invocation_token,
        transport=transport,
        invoke_step1=_invoke_step1,
    )
    sys.stdout.write(json.dumps(result))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
