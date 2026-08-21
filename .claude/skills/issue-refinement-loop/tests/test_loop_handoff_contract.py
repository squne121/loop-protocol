"""
test_loop_handoff_contract.py

Tests for the Root-Owned Synchronous Entry Transition producer
(root_entry_router.py, Issue #2272 AC1-AC6, AC10-AC13, AC15, AC16).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import root_entry_router as rer  # noqa: E402


class FakeTransport:
    def __init__(
        self,
        *,
        capability_ok=True,
        body="issue body",
        base_sha="base-sha-1",
        identity_ok=True,
        fetch_ok=True,
        audit_publish_ok=True,
    ):
        self.capability_ok = capability_ok
        self.body = body
        self.base_sha = base_sha
        self.identity_ok = identity_ok
        self.fetch_ok = fetch_ok
        self.audit_publish_ok = audit_publish_ok
        self.posted = []

    def capability_preflight(self):
        return self.capability_ok

    def fetch_live_issue(self, issue_number):
        return {
            "body": self.body,
            "base_sha": self.base_sha,
            "identity_ok": self.identity_ok,
            "fetch_ok": self.fetch_ok,
        }

    def post_comment(self, issue_number, body):
        if not self.audit_publish_ok:
            raise RuntimeError("simulated publish failure")
        self.posted.append((issue_number, body))
        return {"ok": True, "comment_id": len(self.posted)}


def _make_go_request(body="issue body", base_sha="base-sha-1"):
    body_sha = rer.compute_body_sha256(body)
    return dict(
        issue_number=2272,
        reviewed_body_sha256=body_sha,
        reviewed_base_sha=base_sha,
        review_verdict="go",
        transport=FakeTransport(body=body, base_sha=base_sha),
    )


def test_ac1_missing_prior_review_routes_to_fresh_review():
    # GIVEN no prior review exists at all (review_verdict is None, no reviewed
    # snapshot to compare against)
    req = rer.RootEntryRequest(
        issue_number=2272,
        capability_ok=True,
        live_fetch_ok=True,
        identity_ok=True,
        reviewed_body_sha256=None,
        reviewed_base_sha=None,
        observed_live_body_sha256=rer.compute_body_sha256("current body"),
        observed_base_sha="base-sha-1",
        review_verdict=None,
    )
    # WHEN routing is decided
    result = rer.decide_root_entry_route(req)
    # THEN it never proceeds directly to implementation; it routes to a fresh
    # review path (body drift, since reviewed is None != observed), not stop
    # due to "implementation started"
    assert result["route"] == rer.ROUTE_RERUN_CONTRACT_REVIEW
    assert result["route"] != rer.ROUTE_INVOKE


def test_ac2_stale_prior_review_routes_to_fresh_review():
    # GIVEN a prior review exists but the reviewed body sha differs from the
    # current live body sha (stale)
    stale_body_sha = rer.compute_body_sha256("old body content")
    req = rer.RootEntryRequest(
        issue_number=2272,
        capability_ok=True,
        live_fetch_ok=True,
        identity_ok=True,
        reviewed_body_sha256=stale_body_sha,
        reviewed_base_sha="base-sha-1",
        observed_live_body_sha256=rer.compute_body_sha256("new body content"),
        observed_base_sha="base-sha-1",
        review_verdict="go",
    )
    # WHEN routing is decided
    result = rer.decide_root_entry_route(req)
    # THEN route is rerun_contract_review, not a terminal stop
    assert result["route"] == rer.ROUTE_RERUN_CONTRACT_REVIEW
    assert result["route"] != rer.ROUTE_STOP


def test_ac3_fresh_review_go_and_live_equality_proceeds():
    # GIVEN fresh review go and live equality (reviewed == observed)
    kwargs = _make_go_request()
    # WHEN routed end to end
    envelope = rer.route_root_implementation_entry(**kwargs)
    # THEN route invokes impl-review-loop
    assert envelope["route"]["route"] == rer.ROUTE_INVOKE


def test_ac4_fresh_review_blocked_stops():
    for verdict in ("blocked", "request_changes"):
        body_sha = rer.compute_body_sha256("body")
        req = rer.RootEntryRequest(
            issue_number=2272,
            capability_ok=True,
            live_fetch_ok=True,
            identity_ok=True,
            reviewed_body_sha256=body_sha,
            reviewed_base_sha="base-sha-1",
            observed_live_body_sha256=body_sha,
            observed_base_sha="base-sha-1",
            review_verdict=verdict,
        )
        result = rer.decide_root_entry_route(req)
        assert result["route"] == rer.ROUTE_STOP
        assert verdict in result["reason"]


def test_ac5_body_drift_routes_to_rerun_contract_review():
    req = rer.RootEntryRequest(
        issue_number=2272,
        capability_ok=True,
        live_fetch_ok=True,
        identity_ok=True,
        reviewed_body_sha256=rer.compute_body_sha256("reviewed body"),
        reviewed_base_sha="base-sha-1",
        observed_live_body_sha256=rer.compute_body_sha256("edited body"),
        observed_base_sha="base-sha-1",
        review_verdict="go",
    )
    result = rer.decide_root_entry_route(req)
    assert result["route"] == rer.ROUTE_RERUN_CONTRACT_REVIEW
    assert result["reason"] == "body_drift_detected"


def test_ac6_base_drift_routes_to_rerun_base_preflight_bounded_retry():
    body_sha = rer.compute_body_sha256("stable body")
    # First 3 attempts (retry_count 0,1,2) should route to rerun_base_preflight
    for retry_count in range(rer.MAX_BASE_PREFLIGHT_RETRIES):
        req = rer.RootEntryRequest(
            issue_number=2272,
            capability_ok=True,
            live_fetch_ok=True,
            identity_ok=True,
            reviewed_body_sha256=body_sha,
            reviewed_base_sha="base-sha-old",
            observed_live_body_sha256=body_sha,
            observed_base_sha="base-sha-new",
            review_verdict="go",
            retry_count=retry_count,
        )
        result = rer.decide_root_entry_route(req)
        assert result["route"] == rer.ROUTE_RERUN_BASE_PREFLIGHT
        assert result["retry_count"] == retry_count + 1

    # Once retry_count has reached the bound, it must stop rather than retry
    # forever.
    req_exhausted = rer.RootEntryRequest(
        issue_number=2272,
        capability_ok=True,
        live_fetch_ok=True,
        identity_ok=True,
        reviewed_body_sha256=body_sha,
        reviewed_base_sha="base-sha-old",
        observed_live_body_sha256=body_sha,
        observed_base_sha="base-sha-new",
        review_verdict="go",
        retry_count=rer.MAX_BASE_PREFLIGHT_RETRIES,
    )
    result_exhausted = rer.decide_root_entry_route(req_exhausted)
    assert result_exhausted["route"] == rer.ROUTE_STOP
    assert result_exhausted["reason"] == "base_preflight_retry_exhausted"


def test_ac10_partial_failure_returns_resume_from():
    # GIVEN a mutation partial failure occurred at the contract_review phase
    body_sha = rer.compute_body_sha256("body")
    req = rer.RootEntryRequest(
        issue_number=2272,
        capability_ok=True,
        live_fetch_ok=True,
        identity_ok=True,
        reviewed_body_sha256=body_sha,
        reviewed_base_sha="base-sha-1",
        observed_live_body_sha256=body_sha,
        observed_base_sha="base-sha-1",
        review_verdict="go",
        mutation_partial_failure_phase="contract_review",
    )
    # WHEN routing is decided
    result = rer.decide_root_entry_route(req)
    # THEN it stops but returns a safe resume_from derived from the failed
    # phase (not None, not blindly resuming implementation)
    assert result["route"] == rer.ROUTE_STOP
    assert result["resume_from"] == "contract_review"
    assert rer.resume_from_after_mutation_failure("contract_review") == "contract_review"
    assert rer.resume_from_after_mutation_failure("base_preflight") == "base_preflight"


def test_ac11_capability_preflight_blocked_stops_before_mutation():
    mutation_calls = []

    def fake_mutation():
        mutation_calls.append(1)

    req = rer.RootEntryRequest(
        issue_number=2272,
        capability_ok=False,
        live_fetch_ok=False,
        identity_ok=False,
        reviewed_body_sha256=None,
        reviewed_base_sha=None,
        observed_live_body_sha256=None,
        observed_base_sha=None,
        review_verdict=None,
    )
    result = rer.decide_root_entry_route(req)
    # THEN route is stop, and the caller (this test simulates the caller
    # convention: only mutate when route == invoke_impl_review_loop) never
    # invokes mutation.
    assert result["route"] == rer.ROUTE_STOP
    assert result["reason"] == "capability_or_identity_unverifiable"
    if result["route"] == rer.ROUTE_INVOKE:
        fake_mutation()
    assert mutation_calls == []


def test_ac12_no_new_durable_authority_added():
    # No save/load/persist API exists on the module.
    forbidden_substrings = ("save", "persist", "load", "ledger", "write_route", "store_route")
    public_names = [name for name in dir(rer) if not name.startswith("_")]
    offending = [
        name
        for name in public_names
        if any(sub in name.lower() for sub in forbidden_substrings)
    ]
    assert offending == [], f"unexpected durable-authority-shaped API: {offending}"

    # The route result itself is a plain dict, not a serialized/reloadable
    # object with identity beyond this call.
    envelope = rer.route_root_implementation_entry(**_make_go_request())
    assert isinstance(envelope["route"], dict)
    assert set(envelope["route"].keys()) == set(rer._ROUTE_V1_KEYS)


def test_ac13_live_equality_does_not_reuse_comment_fingerprint_validator():
    # The comparison is a direct sha256/base_sha equality check that does not
    # require any comment_id / trusted author provenance fields at all.
    body = "some body content"
    body_sha = rer.compute_body_sha256(body)
    req_match = rer.RootEntryRequest(
        issue_number=1,
        capability_ok=True,
        live_fetch_ok=True,
        identity_ok=True,
        reviewed_body_sha256=body_sha,
        reviewed_base_sha="sha-a",
        observed_live_body_sha256=body_sha,
        observed_base_sha="sha-a",
        review_verdict="go",
    )
    result_match = rer.decide_root_entry_route(req_match)
    assert result_match["route"] == rer.ROUTE_INVOKE

    req_mismatch = rer.RootEntryRequest(
        issue_number=1,
        capability_ok=True,
        live_fetch_ok=True,
        identity_ok=True,
        reviewed_body_sha256=body_sha,
        reviewed_base_sha="sha-a",
        observed_live_body_sha256=rer.compute_body_sha256("different content"),
        observed_base_sha="sha-a",
        review_verdict="go",
    )
    result_mismatch = rer.decide_root_entry_route(req_mismatch)
    assert result_mismatch["route"] == rer.ROUTE_RERUN_CONTRACT_REVIEW

    # No comment_id / trusted_author / provenance parameters exist anywhere
    # in the request dataclass.
    field_names = {f for f in rer.RootEntryRequest.__dataclass_fields__.keys()}
    assert "comment_id" not in field_names
    assert "trusted_author" not in field_names


def test_ac15_process_local_nonce_not_restored_after_restart(tmp_path):
    # Simulate "process 1"
    nonce_1 = rer.generate_root_invocation_nonce()
    # Simulate "process restart" (fresh module-level state; here represented
    # by simply calling the generator again with no carried-over state,
    # since the function itself accepts no persisted seed).
    nonce_2 = rer.generate_root_invocation_nonce()
    assert nonce_1 != nonce_2

    # prompt_id-based correlation is deterministic per prompt_id but is never
    # sourced from a persisted/restored value across process boundaries in
    # this module (no file/env lookup exists).
    import inspect

    source = inspect.getsource(rer.generate_root_invocation_nonce)
    assert "open(" not in source
    assert "os.environ" not in source


def test_ac16_audit_publish_failure_does_not_change_route():
    kwargs = _make_go_request()
    kwargs["transport"] = FakeTransport(
        body="issue body", base_sha="base-sha-1", audit_publish_ok=False
    )
    envelope = rer.route_root_implementation_entry(**kwargs)
    # Route still proceeds despite audit publish failure.
    assert envelope["route"]["route"] == rer.ROUTE_INVOKE
    assert envelope["audit"]["published"] is False
    assert envelope["audit"]["warning"] is not None


def test_producer_subprocess_cli_emits_route_and_token(tmp_path):
    # Supports AC8 process-level test in impl-review-loop/tests -- verifies
    # the producer CLI itself works standalone via strict JSON stdin/stdout.
    fixture = tmp_path / "transport.json"
    body = "cli body"
    fixture.write_text(
        json.dumps(
            {
                "capability_ok": True,
                "audit_publish_ok": True,
                "issues": {
                    "2272": {"body": body, "base_sha": "sha-cli", "identity_ok": True}
                },
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "issue_number": 2272,
        "reviewed_body_sha256": rer.compute_body_sha256(body),
        "reviewed_base_sha": "sha-cli",
        "review_verdict": "go",
        "retry_count": 0,
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS_DIR / "root_entry_router.py"),
            "--fake-transport-file",
            str(fixture),
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(proc.stdout)
    assert out["route"]["route"] == rer.ROUTE_INVOKE
    assert out["invocation_token"]
