"""
test_loop_handoff_contract.py

Tests for the Root-Owned Synchronous Entry Transition (root_entry_router.py,
Issue #2272 AC1-AC6, AC10-AC18) and for the adversarial exploits raised in
OWNER REQUEST_CHANGES on PR #2282
(https://github.com/squne121/loop-protocol/pull/2282#issuecomment-5371853364).
"""

from __future__ import annotations

import inspect
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
        repo_identity="squne121/loop-protocol",
        base_sha_sequence=None,
        preexisting_comments=None,
    ):
        self.capability_ok = capability_ok
        self.body = body
        self.base_sha = base_sha
        self.identity_ok = identity_ok
        self.fetch_ok = fetch_ok
        self.audit_publish_ok = audit_publish_ok
        self.repo_identity = repo_identity
        self.base_sha_sequence = base_sha_sequence
        self.posted = []
        self.fetch_calls = 0
        self.preexisting_comments = list(preexisting_comments or [])

    def capability_preflight(self):
        return self.capability_ok

    def fetch_live_issue(self, issue_number):
        self.fetch_calls += 1
        if self.base_sha_sequence:
            idx = min(self.fetch_calls - 1, len(self.base_sha_sequence) - 1)
            base_sha = self.base_sha_sequence[idx]
        else:
            base_sha = self.base_sha
        return {
            "body": self.body,
            "base_sha": base_sha,
            "identity_ok": self.identity_ok,
            "fetch_ok": self.fetch_ok,
        }

    def post_comment(self, issue_number, body):
        if not self.audit_publish_ok:
            raise RuntimeError("simulated publish failure")
        self.posted.append((issue_number, body))
        return {"ok": True, "comment_id": len(self.posted)}

    def canonical_repository_identity(self):
        return self.repo_identity

    def list_recent_comments(self, issue_number):
        return self.preexisting_comments + [body for (_, body) in self.posted]


def _go_reviewer(body="issue body"):
    body_sha = rer.compute_body_sha256(body)

    def _reviewer(**_kwargs):
        return {"status": "go", "body_sha256": body_sha}

    return _reviewer


def _blocked_reviewer(status="blocked"):
    def _reviewer(**_kwargs):
        return {"status": status, "body_sha256": rer.compute_body_sha256("body")}

    return _reviewer


# ---------------------------------------------------------------------------
# AC1-AC6, AC10-AC13: pure decide_root_entry_route() behavior (unchanged
# routing table, still 100% I/O-free).
# ---------------------------------------------------------------------------


def test_ac1_missing_prior_review_routes_to_fresh_review():
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
    result = rer.decide_root_entry_route(req)
    assert result["route"] == rer.ROUTE_RERUN_CONTRACT_REVIEW
    assert result["route"] != rer.ROUTE_INVOKE


def test_ac2_stale_prior_review_routes_to_fresh_review():
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
    result = rer.decide_root_entry_route(req)
    assert result["route"] == rer.ROUTE_RERUN_CONTRACT_REVIEW
    assert result["route"] != rer.ROUTE_STOP


def test_ac3_fresh_review_go_and_live_equality_invokes_step1():
    # P0-2 fix: review_verdict/reviewed_body_sha256 are no longer accepted
    # as caller-supplied parameters at all -- they come only from the
    # injected `contract_reviewer` callable's OWN return value, evaluated
    # inside run_root_transition's own call stack.
    invoked_count = []
    transport = FakeTransport(body="issue body", base_sha="base-sha-1")
    result = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=transport,
        contract_reviewer=_go_reviewer(body="issue body"),
        invoke_step1=lambda: invoked_count.append(1),
    )
    assert result["route"]["route"] == rer.ROUTE_INVOKE
    assert result["invoked"] is True
    assert invoked_count == [1]


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
    result = rer.decide_root_entry_route(req)
    assert result["route"] == rer.ROUTE_STOP
    assert result["resume_from"] == "contract_review"
    assert rer.resume_from_after_mutation_failure("contract_review") == "contract_review"
    assert rer.resume_from_after_mutation_failure("base_preflight") == "base_preflight"


def test_ac11_capability_preflight_blocked_stops_before_mutation():
    invoked = []
    transport = FakeTransport(capability_ok=False)
    result = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=transport,
        contract_reviewer=_go_reviewer(),
        invoke_step1=lambda: invoked.append(1),
    )
    assert result["route"]["route"] == rer.ROUTE_STOP
    assert result["route"]["reason"] == "capability_or_identity_unverifiable"
    assert result["invoked"] is False
    assert invoked == []


def test_ac12_no_new_durable_authority_added():
    forbidden_substrings = ("save", "persist", "load", "ledger", "write_route", "store_route")
    public_names = [name for name in dir(rer) if not name.startswith("_")]
    offending = [
        name
        for name in public_names
        if any(sub in name.lower() for sub in forbidden_substrings)
    ]
    assert offending == [], f"unexpected durable-authority-shaped API: {offending}"

    invoked = []
    result = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=FakeTransport(),
        contract_reviewer=_go_reviewer(),
        invoke_step1=lambda: invoked.append(1),
    )
    assert isinstance(result["route"], dict)
    assert set(result["route"].keys()) == set(rer._ROUTE_V1_KEYS)


def test_ac13_live_equality_does_not_reuse_comment_fingerprint_validator():
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
    assert rer.decide_root_entry_route(req_match)["route"] == rer.ROUTE_INVOKE

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
    assert rer.decide_root_entry_route(req_mismatch)["route"] == rer.ROUTE_RERUN_CONTRACT_REVIEW

    field_names = set(rer.RootEntryRequest.__dataclass_fields__.keys())
    assert "comment_id" not in field_names
    assert "trusted_author" not in field_names


def test_ac15_process_local_nonce_not_restored_after_restart():
    nonce_1 = rer.generate_root_invocation_nonce()
    nonce_2 = rer.generate_root_invocation_nonce()
    assert nonce_1 != nonce_2

    source = inspect.getsource(rer.generate_root_invocation_nonce)
    assert "open(" not in source
    assert "os.environ" not in source


def test_ac16_audit_publish_failure_does_not_change_route():
    transport = FakeTransport(body="issue body", base_sha="base-sha-1", audit_publish_ok=False)
    result = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=transport,
        contract_reviewer=_go_reviewer(body="issue body"),
        invoke_step1=lambda: None,
    )
    assert result["route"]["route"] == rer.ROUTE_INVOKE
    assert result["invoked"] is True
    assert result["audit"]["published"] is False
    assert result["audit"]["warning"] is not None


# ---------------------------------------------------------------------------
# Adversarial tests demanded by OWNER REQUEST_CHANGES
# (pull/2282#issuecomment-5371853364). Each name matches the OWNER's list
# verbatim.
# ---------------------------------------------------------------------------


def test_replayed_envelope_same_live_state_after_process_restart_is_rejected():
    # There is no longer any function that accepts a previously-produced
    # route/envelope as an input at all -- run_root_transition always
    # recomputes route from a fresh contract_reviewer() call + fresh
    # transport fetch. "Replaying" a prior envelope is therefore structurally
    # impossible: calling run_root_transition again, even with UNCHANGED
    # live state, re-runs the full review and re-derives the route rather
    # than accepting any object handed in by a caller.
    assert "route" not in inspect.signature(rer.run_root_transition).parameters
    assert "envelope" not in inspect.signature(rer.run_root_transition).parameters

    invoked = []
    transport = FakeTransport(body="unchanged body", base_sha="unchanged-sha")
    reviewer = _go_reviewer(body="unchanged body")

    # process A
    result_a = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=transport,
        contract_reviewer=reviewer,
        invoke_step1=lambda: invoked.append("A"),
    )
    # process B: same live state, but a genuinely fresh call (simulating a
    # restarted process) -- it must independently re-review, not "replay".
    result_b = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=transport,
        contract_reviewer=reviewer,
        invoke_step1=lambda: invoked.append("B"),
    )
    assert result_a["invoked"] is True
    assert result_b["invoked"] is True
    assert invoked == ["A", "B"]
    # Each call minted its own non-authoritative correlation id -- no shared
    # token was reused to authorize B based on A's result.
    assert result_a["correlation_id"] != result_b["correlation_id"]


def test_expected_token_must_not_be_derived_from_envelope():
    # There is no invocation_token / expected_invocation_token concept left
    # anywhere in the public API.
    public_names = {name for name in dir(rer) if not name.startswith("_")}
    assert "invocation_token" not in public_names
    assert not hasattr(rer, "consume_root_entry_route")
    sig_params = set(inspect.signature(rer.run_root_transition).parameters)
    assert "invocation_token" not in sig_params
    assert "expected_invocation_token" not in sig_params

    result = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=FakeTransport(),
        contract_reviewer=_go_reviewer(),
        invoke_step1=lambda: None,
    )
    assert "invocation_token" not in result
    assert "invocation_token" not in result["route"]


def test_prompt_id_from_stdin_cannot_establish_current_root_identity():
    # prompt_id only affects the non-authoritative correlation_id label; it
    # never appears anywhere in the routing inputs/outputs and never changes
    # whether Step 1 is invoked.
    invoked_with_prompt = []
    invoked_without_prompt = []
    transport_a = FakeTransport(body="body", base_sha="sha-1")
    transport_b = FakeTransport(body="body", base_sha="sha-1")

    result_with = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=transport_a,
        contract_reviewer=_go_reviewer(body="body"),
        invoke_step1=lambda: invoked_with_prompt.append(1),
        prompt_id="attacker-supplied-prompt-id",
    )
    result_without = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=transport_b,
        contract_reviewer=_go_reviewer(body="body"),
        invoke_step1=lambda: invoked_without_prompt.append(1),
        prompt_id=None,
    )
    assert result_with["invoked"] == result_without["invoked"] is True
    assert result_with["route"] == result_without["route"]
    assert "prompt_id" not in result_with["route"]
    assert "prompt_id" not in inspect.signature(rer.decide_root_entry_route).parameters


def test_fabricated_go_with_current_hashes_without_current_review_is_rejected():
    # A caller cannot supply review_verdict / reviewed_body_sha256 /
    # reviewed_base_sha directly -- those parameters do not exist on
    # run_root_transition. The ONLY way review_verdict enters routing is via
    # the injected contract_reviewer() callable's return value, evaluated
    # inside this call.
    sig_params = set(inspect.signature(rer.run_root_transition).parameters)
    assert "review_verdict" not in sig_params
    assert "reviewed_body_sha256" not in sig_params
    assert "reviewed_base_sha" not in sig_params

    # A malicious/broken contract_reviewer that fabricates "go" without
    # doing real review work still only produces a route if ITS OWN
    # execution genuinely happens inside this call -- there is no bypass
    # that skips calling contract_reviewer() entirely.
    calls = []

    def _reviewer(**kwargs):
        calls.append(kwargs)
        return {"status": "go", "body_sha256": rer.compute_body_sha256("body")}

    rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=FakeTransport(body="body", base_sha="sha-1"),
        contract_reviewer=_reviewer,
        invoke_step1=lambda: None,
    )
    assert len(calls) == 1  # contract_reviewer was actually invoked, not skipped


def test_direct_impl_review_loop_invocation_runs_fresh_review_or_returns_typed_stop(tmp_path):
    # The CLI always either runs a fresh review (fake or real contract
    # reviewer, both go through the same run_root_transition call) or
    # returns a typed stop -- it never has a bare "issue_number only, no
    # gate" path.
    transport_fixture = tmp_path / "transport.json"
    transport_fixture.write_text(
        json.dumps(
            {
                "capability_ok": True,
                "repo_identity": "squne121/loop-protocol",
                "issues": {"2272": {"body": "cli body", "base_sha": "sha-cli", "identity_ok": True}},
            }
        ),
        encoding="utf-8",
    )
    review_fixture = tmp_path / "review.json"
    review_fixture.write_text(
        json.dumps({"status": "go", "body_sha256": rer.compute_body_sha256("cli body")}),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS_DIR / "root_entry_router.py"),
            "--issue-number",
            "2272",
            "--repo",
            "squne121/loop-protocol",
            "--fake-transport-file",
            str(transport_fixture),
            "--fake-contract-review-file",
            str(review_fixture),
            "--no-audit",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(proc.stdout)
    assert out["route"]["route"] == rer.ROUTE_INVOKE
    # CLI's invoke_step1 is a deliberate no-op sentinel (module docstring):
    # `invoked=True` here means "authorized, callback invoked" -- it does
    # NOT mean the CLI itself launched implementation-worker/impl-review-loop
    # (a subprocess cannot drive this session's tool calls). The orchestrating
    # root/main thread reads this typed result in the SAME turn and only then
    # proceeds to invoke impl-review-loop itself.
    assert out["invoked"] is True
    assert "invocation_token" not in out


def test_production_entrypoint_uses_root_owned_transport_not_fake_fixture():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base-ref", default="main")
    parser.add_argument("--mode", default="static")
    parser.add_argument("--fake-transport-file", default=None)
    parser.add_argument("--fake-contract-review-file", default=None)
    parser.add_argument("--prompt-id", default=None)
    parser.add_argument("--no-audit", action="store_true")

    args_no_fake = parser.parse_args(["--issue-number", "2272", "--repo", "squne121/loop-protocol"])
    transport = rer._select_transport(args_no_fake)
    assert isinstance(transport, rer.GhCliGitHubEntryTransport)

    args_fake = parser.parse_args(
        ["--issue-number", "2272", "--repo", "x/y", "--fake-transport-file", "/tmp/does-not-matter.json"]
    )
    # (constructing the fake transport lazily reads the file; only assert
    # selection logic here, not fake-file I/O)
    assert args_fake.fake_transport_file is not None


def test_negative_retry_count_is_rejected():
    body_sha = rer.compute_body_sha256("body")
    req = rer.RootEntryRequest(
        issue_number=2272,
        capability_ok=True,
        live_fetch_ok=True,
        identity_ok=True,
        reviewed_body_sha256=body_sha,
        reviewed_base_sha="sha-a",
        observed_live_body_sha256=body_sha,
        observed_base_sha="sha-a",
        review_verdict="go",
        retry_count=-10,
    )
    route = rer.decide_root_entry_route(req)
    # Even when body/base already match (route==invoke), a negative
    # retry_count carried in the request is NOT silently dropped -- it
    # propagates into the emitted route dict and is caught by the schema
    # gate before invocation, defense-in-depth against any code path that
    # might otherwise construct a route with an out-of-range retry_count.
    assert route["route"] == rer.ROUTE_INVOKE
    assert route["retry_count"] == -10
    error = rer.validate_route_schema(route, expected_issue_number=2272)
    assert error == "invalid_retry_count"

    bad_route = dict(route)
    bad_route["retry_count"] = -1
    error_negative = rer.validate_route_schema(bad_route, expected_issue_number=2272)
    assert error_negative == "invalid_retry_count"


def test_retry_counter_cannot_be_reset_by_caller():
    # retry_count is not a parameter of run_root_transition at all -- it is
    # a local loop variable owned entirely by this function's own while-loop
    # (P1-1 fix). A caller has no way to pass in a "reset to 0" value.
    assert "retry_count" not in inspect.signature(rer.run_root_transition).parameters

    invoked = []
    # base_sha drifts on every fetch call forever -- if a caller could reset
    # the counter each attempt, this would loop unboundedly; instead the
    # bounded retry (owned internally) must terminate with stop.
    transport = FakeTransport(
        body="stable body",
        base_sha_sequence=["s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9"],
    )
    result = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=transport,
        contract_reviewer=_go_reviewer(body="stable body"),
        invoke_step1=lambda: invoked.append(1),
    )
    assert result["route"]["route"] == rer.ROUTE_STOP
    assert result["route"]["reason"] == "base_preflight_retry_exhausted"
    assert invoked == []
    # Fetch calls are bounded (not unbounded): 1 initial + one per retry
    # attempt's re-review readback, capped by MAX_BASE_PREFLIGHT_RETRIES.
    assert transport.fetch_calls <= 2 * (rer.MAX_BASE_PREFLIGHT_RETRIES + 2)


def test_base_preflight_reviewed_sha_propagates_to_route_result():
    # NOTE (Fix 5, PR #2282 re-audit): this router does not itself create
    # or verify a git worktree -- it has no `worktree` concept at all.
    # Actual worktree creation pinned to the reviewed base SHA is
    # impl-review-loop Step 1's existing responsibility (the normal
    # implementation-worker flow already creates its worktree from the
    # base commit it is handed). What THIS function is responsible for,
    # and what this test verifies, is that each retry attempt re-pins
    # `reviewed_base_sha` to the freshly OBSERVED base from the
    # immediately preceding live fetch (never a caller-supplied or stale
    # value), so the base handed onward to Step 1 is always the most
    # recently verified one.
    transport = FakeTransport(
        body="stable body",
        base_sha_sequence=["s0", "s1", "s1"],  # drifts once, then settles at s1
    )
    invoked = []
    result = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=transport,
        contract_reviewer=_go_reviewer(body="stable body"),
        invoke_step1=lambda: invoked.append(1),
    )
    assert result["route"]["route"] == rer.ROUTE_INVOKE
    assert result["route"]["reviewed_base_sha"] == "s1"
    assert result["route"]["observed_base_sha"] == "s1"
    assert invoked == [1]


def test_partial_failure_resume_is_derived_from_post_mutation_readback():
    # Non-mutation phases (read-only fetches -- there is nothing to read
    # back live for a fetch that never mutated anything) keep the plain
    # phase-name fallback.
    for phase in ("capability_preflight", "live_fetch", "contract_review", "base_preflight"):
        result = rer.run_root_transition(
            issue_number=2272,
            repo="squne121/loop-protocol",
            transport=FakeTransport(),
            contract_reviewer=_go_reviewer(),
            invoke_step1=lambda: None,
            mutation_partial_failure_phase=phase,
        )
        assert result["route"]["route"] == rer.ROUTE_STOP
        assert result["route"]["resume_from"] == phase
        assert result["invoked"] is False

    # An unrecognized phase is returned verbatim as an opaque marker (never
    # invented into a different, more-permissive value) and never resumes
    # into invocation.
    result_unknown = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=FakeTransport(),
        contract_reviewer=_go_reviewer(),
        invoke_step1=lambda: None,
        mutation_partial_failure_phase="unknown_phase_xyz",
    )
    assert result_unknown["route"]["resume_from"] == "unknown_phase_xyz"
    assert result_unknown["invoked"] is False


def test_mutation_resume_from_is_derived_from_live_readback():
    # AC10 / P1-1 fix: `audit_comment` is the ONE phase this router
    # actually mutates state for, so its resume point is derived from a
    # LIVE re-read of the issue's comments -- never from the caller-
    # supplied phase string alone -- and the SAME caller-supplied phase
    # value ("audit_comment") produces a DIFFERENT resume_from depending
    # on live state, which is exactly what a mere string echo could never
    # do.

    # Case 1: the earlier publish never actually landed (no matching
    # marker comment present) -> the live readback proves it is NOT
    # complete, so this attempt retries the full fresh-review sequence and
    # (since the injected reviewer says "go") reaches ROUTE_INVOKE.
    invoked = []
    transport_not_yet = FakeTransport(preexisting_comments=[])
    result_not_yet = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=transport_not_yet,
        contract_reviewer=_go_reviewer(),
        invoke_step1=lambda: invoked.append(1),
        mutation_partial_failure_phase="audit_comment",
    )
    assert result_not_yet["route"]["route"] == rer.ROUTE_INVOKE
    assert invoked == [1]

    # Case 2: a comment carrying the marker for THIS exact reviewed state
    # (body/base sha, recomputed fresh -- not restored from any prior
    # artifact) is already present -> live readback proves the earlier
    # publish DID complete, so this attempt stops without invoking Step 1
    # or publishing a duplicate audit comment.
    marker = rer.compute_audit_invocation_marker(
        rer.compute_body_sha256("issue body"), "base-sha-1"
    )
    transport_already = FakeTransport(
        preexisting_comments=[f"prior audit\n{rer._audit_marker_tag(marker)}\n"]
    )
    invoked_already = []
    result_already = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=transport_already,
        contract_reviewer=_go_reviewer(),
        invoke_step1=lambda: invoked_already.append(1),
        mutation_partial_failure_phase="audit_comment",
    )
    assert result_already["route"]["route"] == rer.ROUTE_STOP
    assert result_already["route"]["resume_from"] == "post_audit_comment_complete"
    assert result_already["invoked"] is False
    assert invoked_already == []
    # No duplicate audit comment was published for the already-complete case.
    assert transport_already.posted == []


def test_static_or_baseline_review_cannot_be_mislabeled_as_reviewed_exact_head():
    # `_real_reviewer` (the production reviewer path, exercised without
    # going through any fake) must call `run_once` with
    # `skip_idempotency_check=True` -- otherwise a stale trusted "go"
    # comment matching the current body sha would short-circuit the
    # existing-go-comment dedupe inside `run_once` and this root-direct
    # entry gate would silently accept a review that did not actually run
    # just now (P0-1 dedupe-bypass fix).
    import types
    import unittest.mock as mock

    captured = {}

    def _fake_run_once(**kwargs):
        captured.update(kwargs)
        return {"status": "go", "body_sha256": kwargs.get("reviewed_head_sha")}

    fake_module = types.SimpleNamespace(run_once=_fake_run_once)
    with mock.patch.dict("sys.modules", {"run_contract_review_once": fake_module}):
        args = types.SimpleNamespace(fake_contract_review_file=None)
        reviewer = rer._select_contract_reviewer(args)
        reviewer(issue_number=2272, repo="squne121/loop-protocol", mode="static", reviewed_head_sha="sha-x")

    assert captured.get("skip_idempotency_check") is True


def test_production_repository_identity_cannot_be_bypassed_by_default():
    # The production CLI path must ALWAYS bind identity verification to the
    # operator-supplied --repo value -- there is no flag to opt out, so
    # `expected_repository_identity` is never left at its permissive
    # default (None) when invoked as a real CLI process (P0-2
    # identity-bypass fix).
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base-ref", default="main")
    parser.add_argument("--mode", default="static")
    parser.add_argument("--fake-transport-file", default=None)
    parser.add_argument("--fake-contract-review-file", default=None)
    parser.add_argument("--prompt-id", default=None)
    parser.add_argument("--no-audit", action="store_true")
    parser.parse_args(["--issue-number", "2272", "--repo", "squne121/loop-protocol"])

    import inspect as _inspect

    source = _inspect.getsource(rer._main)
    assert "expected_repository_identity=args.repo" in source


def test_missing_expected_repository_identity_fails_closed_not_skipped():
    # Function-level regression for the P1 gap found by the adversarial
    # re-audit: omitting `expected_repository_identity` (its permissive
    # `None` default) must never silently skip identity verification --
    # it must fail closed with a typed stop route instead. This protects
    # any future in-process direct caller (per termination-policy.md's
    # "same call stack" delivery mode) that forgets to pass the argument,
    # not just the CLI wrapper (which is covered separately above).
    invoked = []
    result = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        transport=FakeTransport(),
        contract_reviewer=_go_reviewer(),
        invoke_step1=lambda: invoked.append(1),
    )
    assert result["route"]["route"] == rer.ROUTE_STOP
    assert result["route"]["reason"] == "expected_repository_identity_required"
    assert result["invoked"] is False
    assert invoked == []


def test_prior_go_comment_cannot_replace_current_fresh_review():
    # Integration-shape regression for the dedupe-bypass exploit (P0-1):
    # even a `run_once`-shaped reviewer that internally deduped against a
    # stale trusted "go" comment must not be trusted UNLESS this router
    # explicitly asked it to skip that dedupe. This test does not import
    # the real run_contract_review_once (out of scope to modify/exercise
    # its full readiness pipeline here); it asserts the calling contract
    # `_real_reviewer` upholds -- covered directly by
    # test_static_or_baseline_review_cannot_be_mislabeled_as_reviewed_exact_head
    # above, which asserts skip_idempotency_check=True is always passed.
    # This test additionally proves that WITHOUT that flag, a dedupe-
    # shaped reviewer would silently fabricate a "go" for an unreviewed
    # current state -- demonstrating why the flag is required.
    live_body_sha = rer.compute_body_sha256("issue body")

    def _dedupe_shaped_reviewer(*, issue_number, repo, mode, reviewed_head_sha, skip_idempotency_check=False):
        if not skip_idempotency_check:
            # Simulates run_once()'s existing-go-comment short-circuit:
            # returns "go" from a STALE prior check, without running any
            # current-run readiness/blockers/product_spec/vc_preflight.
            return {"status": "go", "body_sha256": live_body_sha, "source": "existing_go_comment"}
        # Simulates the fresh check actually finding a current blocker (for
        # this SAME live body, so body-drift routing does not mask the
        # blocked verdict this test is checking).
        return {"status": "blocked", "body_sha256": live_body_sha, "source": "current_run"}

    invoked = []
    result = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=FakeTransport(),
        contract_reviewer=lambda **kw: _dedupe_shaped_reviewer(skip_idempotency_check=True, **kw),
        invoke_step1=lambda: invoked.append(1),
    )
    # With skip_idempotency_check=True (what _real_reviewer now always
    # passes), the dedupe-shaped reviewer's fresh-check branch runs and
    # correctly blocks -- proving the fix prevents the stale-go exploit.
    assert result["route"]["route"] == rer.ROUTE_STOP
    assert invoked == []


def test_production_cli_noop_cannot_count_as_real_step1_invocation():
    # The CLI's invoke_step1 callback is a deliberate no-op sentinel (a
    # subprocess cannot drive the orchestrating turn's tool calls -- see
    # module docstring). This test asserts that fact is explicit in the
    # source rather than silently assumed, so a future refactor cannot
    # accidentally start treating CLI `invoked: true` as proof Step 1 ran.
    import inspect as _inspect

    source = _inspect.getsource(rer._main)
    assert "invoke_step1=lambda: None" in source
    assert "CLI never invokes Step 1 itself" in source



def test_consumer_rejects_non_exact_route_schema():
    good_route = {
        "route": rer.ROUTE_INVOKE,
        "reason": "ok",
        "issue_number": 2272,
        "reviewed_body_sha256": "sha256:aa",
        "observed_live_body_sha256": "sha256:aa",
        "reviewed_base_sha": "s",
        "observed_base_sha": "s",
        "resume_from": None,
        "retry_count": 0,
    }
    assert rer.validate_route_schema(good_route, expected_issue_number=2272) is None

    missing_key = dict(good_route)
    del missing_key["resume_from"]
    assert rer.validate_route_schema(missing_key, expected_issue_number=2272) == "invalid_route_schema_keys"

    extra_key = dict(good_route)
    extra_key["extra_field"] = "unexpected"
    assert rer.validate_route_schema(extra_key, expected_issue_number=2272) == "invalid_route_schema_keys"

    wrong_issue = dict(good_route)
    wrong_issue["issue_number"] = 9999
    assert (
        rer.validate_route_schema(wrong_issue, expected_issue_number=2272)
        == "missing_or_mismatched_issue_number"
    )

    assert rer.validate_route_schema("not-a-dict", expected_issue_number=2272) == "invalid_route_type"


def test_consumer_returns_typed_stop_on_transport_failure():
    class RaisingTransport(FakeTransport):
        def fetch_live_issue(self, issue_number):
            raise RuntimeError("simulated transport outage")

    invoked = []
    result = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=RaisingTransport(),
        contract_reviewer=_go_reviewer(),
        invoke_step1=lambda: invoked.append(1),
    )
    assert result["route"]["route"] == rer.ROUTE_STOP
    assert result["invoked"] is False
    assert invoked == []

    class RaisingReviewer:
        def __call__(self, **_kwargs):
            raise RuntimeError("simulated review outage")

    result2 = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        expected_repository_identity="squne121/loop-protocol",
        transport=FakeTransport(),
        contract_reviewer=RaisingReviewer(),
        invoke_step1=lambda: invoked.append(2),
    )
    assert result2["route"]["route"] == rer.ROUTE_STOP
    assert result2["route"]["reason"] == "contract_reviewer_transport_failure"
    assert invoked == []


def test_consumer_rechecks_canonical_repository_identity():
    invoked = []
    transport = FakeTransport(repo_identity="attacker/forked-repo")
    result = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        transport=transport,
        contract_reviewer=_go_reviewer(),
        invoke_step1=lambda: invoked.append(1),
        expected_repository_identity="squne121/loop-protocol",
    )
    assert result["route"]["route"] == rer.ROUTE_STOP
    assert result["route"]["reason"] == "repository_identity_mismatch"
    assert invoked == []

    transport_ok = FakeTransport(repo_identity="squne121/loop-protocol")
    result_ok = rer.run_root_transition(
        issue_number=2272,
        repo="squne121/loop-protocol",
        transport=transport_ok,
        contract_reviewer=_go_reviewer(),
        invoke_step1=lambda: invoked.append(2),
        expected_repository_identity="squne121/loop-protocol",
    )
    assert result_ok["route"]["route"] == rer.ROUTE_INVOKE
    assert invoked == [2]
