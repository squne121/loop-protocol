"""
test_refinement_handoff_consumer.py

Tests for the impl-review-loop preparation entry router (consumer side,
Issue #2272 AC7, AC8, AC14, AC17, AC18).

AC8/AC17/AC18 additionally require a **process-level** integration test:
producer and consumer run as separate subprocesses, communicating via
strict JSON over stdin/stdout, using a fake GitHub transport dependency
injected the same way production does (not monkeypatching internals), each
using pytest tmp_path for isolated repo/state, with a spy detecting
impl-review-loop Step 1 invocation.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

_PRODUCER_SCRIPT = (
    _SKILL_ROOT.parent
    / "issue-refinement-loop"
    / "scripts"
    / "root_entry_router.py"
)

import preparation_entry_router as per  # noqa: E402


class FakeConsumerTransport:
    def __init__(self, *, body="issue body", base_sha="base-sha-1"):
        self.body = body
        self.base_sha = base_sha

    def fetch_live_issue(self, issue_number):
        return {"body": self.body, "base_sha": self.base_sha}


def _spy():
    calls = []

    def _invoke():
        calls.append(1)

    return calls, _invoke


def test_ac7_comment_only_handoff_not_authorizing():
    # GIVEN a route reconstructed purely from a stale GitHub comment's
    # LOOP_HANDOFF_RESULT_V1 text (status: impl_ready), with no
    # invocation_token at all (comments never carry the process-local token)
    body = "issue body"
    body_sha = per.compute_body_sha256(body)
    comment_derived_route = {
        "route": "invoke_impl_review_loop",
        "reason": "parsed_from_comment",
        "issue_number": 2272,
        "reviewed_body_sha256": body_sha,
        "observed_live_body_sha256": body_sha,
        "reviewed_base_sha": "base-sha-1",
        "observed_base_sha": "base-sha-1",
        "resume_from": None,
        "retry_count": 0,
    }
    calls, invoke_step1 = _spy()
    result = per.consume_root_entry_route(
        comment_derived_route,
        invocation_token=None,
        expected_invocation_token="whatever-current-token",
        transport=FakeConsumerTransport(body=body, base_sha="base-sha-1"),
        invoke_step1=invoke_step1,
    )
    assert result["invoked"] is False
    assert result["reason"] == per.REJECT_TOKEN_MISSING_OR_MISMATCH
    assert calls == []


def test_ac8_same_invocation_route_result_consumed():
    # GIVEN a fresh route produced by the producer in the SAME invocation
    body = "current body"
    body_sha = per.compute_body_sha256(body)
    route = {
        "route": "invoke_impl_review_loop",
        "reason": "fresh_review_go_live_equality",
        "issue_number": 2272,
        "reviewed_body_sha256": body_sha,
        "observed_live_body_sha256": body_sha,
        "reviewed_base_sha": "base-sha-1",
        "observed_base_sha": "base-sha-1",
        "resume_from": None,
        "retry_count": 0,
    }
    token = "nonce:same-invocation-abc"
    calls, invoke_step1 = _spy()
    result = per.consume_root_entry_route(
        route,
        invocation_token=token,
        expected_invocation_token=token,
        transport=FakeConsumerTransport(body=body, base_sha="base-sha-1"),
        invoke_step1=invoke_step1,
    )
    assert result["invoked"] is True
    assert calls == [1]


def test_ac14_cross_process_resume_requires_fresh_review(tmp_path):
    # Simulate: "process 1" ran, got route+token for an OLD body snapshot.
    # "process 1" then exits. Live Issue content changes. A caller
    # incorrectly attempts to resume using process 1's stale route+token
    # instead of running a fresh review in the new process.
    old_body = "old body from process 1"
    old_body_sha = per.compute_body_sha256(old_body)
    stale_route = {
        "route": "invoke_impl_review_loop",
        "reason": "fresh_review_go_live_equality",
        "issue_number": 2272,
        "reviewed_body_sha256": old_body_sha,
        "observed_live_body_sha256": old_body_sha,
        "reviewed_base_sha": "base-sha-1",
        "observed_base_sha": "base-sha-1",
        "resume_from": None,
        "retry_count": 0,
    }
    stale_token = "nonce:process-1-token"

    # Even if the (broken) caller mistakenly claims the stale token is
    # "current" (token check alone would pass)...
    new_live_body = "new body after process restart"
    calls, invoke_step1 = _spy()
    result = per.consume_root_entry_route(
        stale_route,
        invocation_token=stale_token,
        expected_invocation_token=stale_token,
        transport=FakeConsumerTransport(body=new_live_body, base_sha="base-sha-1"),
        invoke_step1=invoke_step1,
    )
    # ...independent live re-verification catches the drift and refuses to
    # invoke, forcing a fresh review instead.
    assert result["invoked"] is False
    assert result["reason"] == per.REJECT_BODY_DRIFTED
    assert calls == []

    # And when process 2 legitimately re-runs the producer to get a FRESH
    # route/token reflecting the new body, consumption succeeds normally.
    new_body_sha = per.compute_body_sha256(new_live_body)
    fresh_route = dict(stale_route)
    fresh_route["reviewed_body_sha256"] = new_body_sha
    fresh_route["observed_live_body_sha256"] = new_body_sha
    fresh_token = "nonce:process-2-token"
    calls2, invoke_step1_2 = _spy()
    result2 = per.consume_root_entry_route(
        fresh_route,
        invocation_token=fresh_token,
        expected_invocation_token=fresh_token,
        transport=FakeConsumerTransport(body=new_live_body, base_sha="base-sha-1"),
        invoke_step1=invoke_step1_2,
    )
    assert result2["invoked"] is True
    assert calls2 == [1]


# ---------------------------------------------------------------------------
# Process-level integration tests (AC8/AC17/AC18): producer and consumer as
# separate subprocesses, strict JSON stdin/stdout, fake transport DI, spy
# file, isolated tmp_path state.
# ---------------------------------------------------------------------------


def _run_producer(tmp_path, *, body, base_sha, reviewed_body_sha256, reviewed_base_sha, verdict):
    fixture = tmp_path / "producer_transport.json"
    fixture.write_text(
        json.dumps(
            {
                "capability_ok": True,
                "audit_publish_ok": True,
                "issues": {
                    "2272": {"body": body, "base_sha": base_sha, "identity_ok": True}
                },
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "issue_number": 2272,
        "reviewed_body_sha256": reviewed_body_sha256,
        "reviewed_base_sha": reviewed_base_sha,
        "review_verdict": verdict,
        "retry_count": 0,
    }
    proc = subprocess.run(
        [sys.executable, str(_PRODUCER_SCRIPT), "--fake-transport-file", str(fixture)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def _run_consumer(tmp_path, envelope, *, body, base_sha, expected_token, spy_file):
    fixture = tmp_path / "consumer_transport.json"
    fixture.write_text(
        json.dumps({"issues": {"2272": {"body": body, "base_sha": base_sha}}}),
        encoding="utf-8",
    )
    consumer_script = _SCRIPTS_DIR / "preparation_entry_router.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(consumer_script),
            "--fake-transport-file",
            str(fixture),
            "--expected-invocation-token",
            expected_token,
            "--spy-file",
            str(spy_file),
        ],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def test_ac17_negative_case_invocation_count_zero(tmp_path):
    spy_file = tmp_path / "spy.log"
    body = "process-level body"
    base_sha = "sha-process"
    review_body_sha = "sha256:" + "0" * 64  # deliberately WRONG / stale
    envelope = _run_producer(
        tmp_path,
        body=body,
        base_sha=base_sha,
        reviewed_body_sha256=review_body_sha,
        reviewed_base_sha=base_sha,
        verdict="go",
    )
    # Negative case: producer itself won't route to invoke (body drift), so
    # the consumer must never invoke Step 1.
    assert envelope["route"]["route"] != "invoke_impl_review_loop"
    consumer_result = _run_consumer(
        tmp_path,
        envelope,
        body=body,
        base_sha=base_sha,
        expected_token=envelope["invocation_token"],
        spy_file=spy_file,
    )
    assert consumer_result["invoked"] is False
    assert not spy_file.exists() or spy_file.read_text() == ""


def test_ac18_comment_only_replay_rejected(tmp_path):
    spy_file = tmp_path / "spy.log"
    body = "process-level body 2"
    base_sha = "sha-process-2"
    body_sha = "sha256:" + __import__("hashlib").sha256(body.encode("utf-8")).hexdigest()

    # A past LOOP_HANDOFF_RESULT_V1 comment claims impl_ready, but there is
    # no current-run direct result / invocation_token -- only comment text.
    comment_only_envelope = {
        "route": {
            "route": "invoke_impl_review_loop",
            "reason": "parsed_from_old_comment",
            "issue_number": 2272,
            "reviewed_body_sha256": body_sha,
            "observed_live_body_sha256": body_sha,
            "reviewed_base_sha": base_sha,
            "observed_base_sha": base_sha,
            "resume_from": None,
            "retry_count": 0,
        },
        "invocation_token": None,
    }
    consumer_result = _run_consumer(
        tmp_path,
        comment_only_envelope,
        body=body,
        base_sha=base_sha,
        expected_token="nonce:whatever-current",
        spy_file=spy_file,
    )
    assert consumer_result["invoked"] is False
    assert consumer_result["reason"] == per.REJECT_TOKEN_MISSING_OR_MISMATCH
    assert not spy_file.exists() or spy_file.read_text() == ""


def test_ac8_process_level_positive_case_invokes_once(tmp_path):
    spy_file = tmp_path / "spy.log"
    body = "matching process-level body"
    base_sha = "sha-match"
    envelope = _run_producer(
        tmp_path,
        body=body,
        base_sha=base_sha,
        reviewed_body_sha256=per.compute_body_sha256(body),
        reviewed_base_sha=base_sha,
        verdict="go",
    )
    assert envelope["route"]["route"] == "invoke_impl_review_loop"
    consumer_result = _run_consumer(
        tmp_path,
        envelope,
        body=body,
        base_sha=base_sha,
        expected_token=envelope["invocation_token"],
        spy_file=spy_file,
    )
    assert consumer_result["invoked"] is True
    assert spy_file.read_text().count("invoked") == 1
