#!/usr/bin/env python3
"""Tests for persist_retrospective_run.py (Issue #2238, Child 5 of #2192).

Fixture/mock-based only (Runtime Verification Applicability: not_applicable
-- see Issue #2238 body). No live GitHub call is ever made; the only I/O
boundary (``IssueCommentTransportProtocol``) is dependency-injected via
``FakeIssueCommentTransport`` throughout. Live smoke/security verification
is Child 6 (#2239)'s responsibility.

Covers every Issue #2238 AC that is a pytest -k target:
  AC1  production_provider_used
  AC2  read_version_propagates_to_expected_previous_digest
  AC3  publish_request_produces_publication_digest
  AC4  authorization_gate_blocks_without_receipt_or_confirmation
  AC5  idempotency_key_noop_vs_conflict
  AC6  optimistic_concurrency_best_effort_conflict_detection
  AC7  canonical_get_readback_digest_match
  AC8  candidate_finding_delta_full_roundtrip
  AC9  previous_state_status_classification
  AC10 ambiguous_post_failure_recovery_by_request_id
  AC11 index_update_failure_does_not_rollback_primary_record
  AC12 public_safety_validator_runs_before_post
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import persist_retrospective_run as pr  # noqa: E402
import run_retrospective as rr  # noqa: E402

_validate_mod = rr._validate_retrospective_schema_module()

_FULL_SHA = "a" * 40
_DIGEST = "d" * 64
_REPO_ID = "squne121/loop-protocol"
_REPO = "squne121/loop-protocol"
_TARGET_ISSUE = 2238


# ---------------------------------------------------------------------------
# shared fakes / helpers
# ---------------------------------------------------------------------------


class FakeIssueCommentTransport:
    """Hermetic, in-memory ``IssueCommentTransportProtocol`` implementation.
    Every persist_retrospective_run.py function under test is exercised
    exclusively against this fake -- no subprocess, no network call."""

    def __init__(self) -> None:
        self._comments: dict[int, dict[str, Any]] = {}
        self._next_id = 1000
        self.create_call_count = 0
        self._queued: list[dict[str, Any]] = []

    def queue_create_side_effect(self, exc: Exception, *, also_create: bool = False) -> None:
        self._queued.append({"exc": exc, "also_create": also_create})

    def _insert(self, *, issue_number: int, body: str, comment_id: int | None = None) -> dict[str, Any]:
        cid = comment_id if comment_id is not None else self._next_id
        self._next_id = max(self._next_id, cid + 1)
        comment = {
            "id": cid,
            "html_url": f"https://github.com/x/y/issues/{issue_number}#issuecomment-{cid}",
            "body": body,
            "_issue_number": issue_number,
        }
        self._comments[cid] = comment
        return dict(comment)

    def seed_comment(self, *, issue_number: int, body: str, comment_id: int | None = None) -> dict[str, Any]:
        return self._insert(issue_number=issue_number, body=body, comment_id=comment_id)

    def list_comments(self, *, repo: str, issue_number: int) -> list[dict[str, Any]]:
        del repo
        return [dict(c) for c in self._comments.values() if c["_issue_number"] == issue_number]

    def create_comment(self, *, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        del repo
        self.create_call_count += 1
        if self._queued:
            spec = self._queued.pop(0)
            if spec["also_create"]:
                self._insert(issue_number=issue_number, body=body)
            raise spec["exc"]
        return self._insert(issue_number=issue_number, body=body)

    def get_comment(self, *, repo: str, comment_id: int) -> dict[str, Any]:
        del repo
        return dict(self._comments[comment_id])


def _publish_request_dict(
    *,
    candidate_records: list[dict[str, Any]] | None = None,
    delta_results: list[dict[str, Any]] | None = None,
    expected_previous_digest: str | None = None,
    request_id: str = "req-1",
    repository_id: str = _REPO_ID,
    target_issue: int = _TARGET_ISSUE,
    run_id: str = "run-1",
    base_sha: str | None = None,
    source_set_digest: str | None = None,
) -> dict[str, Any]:
    return {
        "run_identity": {
            "run_id": run_id,
            "base_sha": base_sha or _FULL_SHA,
            "source_set_digest": source_set_digest or _DIGEST,
        },
        "repository_id": repository_id,
        "target_issue": target_issue,
        "request_id": request_id,
        "candidate_records": candidate_records or [],
        "delta_results": delta_results or [],
        "expected_previous_digest": expected_previous_digest,
    }


def _new_candidate() -> dict[str, Any]:
    return _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")


def _iso(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_receipt(*, publication_digest: str, repository_id: str, target_issue: int, request_id: str) -> dict[str, Any]:
    return {
        "schema_version": pr.HUMAN_AUTHORIZATION_RECEIPT_SCHEMA,
        "request_id": request_id,
        "publication_digest": publication_digest,
        "repository_id": repository_id,
        "target_issue": target_issue,
        "operation": "publish_retrospective_run",
        "approved_at": "2026-08-22T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# AC1: main() actually builds and uses the persistence-backed provider
# ---------------------------------------------------------------------------


def test_production_provider_used(monkeypatch: pytest.MonkeyPatch) -> None:
    # GIVEN main() is invoked with --state-backend issue-comments
    captured: dict[str, Any] = {}

    def fake_run_cli(**kwargs: Any) -> rr.PublishRequest:
        captured["provider"] = kwargs["previous_state_provider"]
        return rr.PublishRequest(
            request_id="req",
            repository_id=_REPO_ID,
            target_issue=_TARGET_ISSUE,
            run_identity={"run_id": "r", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
            candidate_records=[],
            expected_previous_digest=None,
            idempotency_key="idem",
            public_projection_digest="a" * 64,
            authorization_required=True,
        )

    # main() calls run_cli via the module-global name -- patching it here
    # intercepts the exact production call graph, not a parallel path.
    monkeypatch.setattr(rr, "run_cli", fake_run_cli)

    # WHEN main() runs (the actual production entrypoint, unmodified)
    exit_code = rr.main(
        [
            "--repository-id",
            _REPO_ID,
            "--target-issue",
            str(_TARGET_ISSUE),
            "--request-id",
            "req",
            "--idempotency-key",
            "idem",
            "--state-backend",
            "issue-comments",
        ]
    )

    # THEN it constructed and passed the REAL persistence-backed provider --
    # never a fixture fallback. ``rr.resolve_previous_state_provider()``
    # loads persist_retrospective_run.py via its own lazy sibling-module
    # loader (a fresh module object each call, like
    # ``_validate_retrospective_schema_module()``), so structural checks
    # are used instead of ``isinstance`` against this test file's own
    # top-level import of the same source file under a different module
    # identity.
    assert exit_code == 0
    provider = captured["provider"]
    assert type(provider).__name__ == "IssueCommentPreviousStateProvider"
    assert not isinstance(provider, rr.FixturePreviousStateProvider)
    assert provider._repo == _REPO_ID
    assert provider._target_issue == _TARGET_ISSUE
    assert hasattr(provider, "_transport") and type(provider._transport).__name__ == "GhCliIssueCommentTransport"


def test_production_provider_used_fixture_backend_is_still_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    # GIVEN the default (--state-backend omitted / "fixture")
    captured: dict[str, Any] = {}

    def fake_run_cli(**kwargs: Any) -> rr.PublishRequest:
        captured["provider"] = kwargs["previous_state_provider"]
        return rr.PublishRequest(
            request_id="req",
            repository_id=_REPO_ID,
            target_issue=_TARGET_ISSUE,
            run_identity={"run_id": "r", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
            candidate_records=[],
            expected_previous_digest=None,
            idempotency_key="idem",
            public_projection_digest="a" * 64,
            authorization_required=True,
        )

    monkeypatch.setattr(rr, "run_cli", fake_run_cli)

    # WHEN main() runs without --state-backend
    rr.main(["--repository-id", _REPO_ID, "--target-issue", str(_TARGET_ISSUE), "--request-id", "req", "--idempotency-key", "idem"])

    # THEN the exact prior fixture behavior is preserved byte-for-byte
    assert isinstance(captured["provider"], rr.FixturePreviousStateProvider)


# ---------------------------------------------------------------------------
# AC2: provider.read_version propagates to PUBLISH_REQUEST_V1.expected_previous_digest
# ---------------------------------------------------------------------------


class _FakeCollectorResult:
    def __init__(self, observation: dict[str, Any]) -> None:
        self.observation = observation
        self.private_evidence: dict[str, Any] = {}


def _fake_collector_result(source_id: str, base_sha: str) -> _FakeCollectorResult:
    del base_sha
    return _FakeCollectorResult(
        {
            "source_type": "repository" if source_id == "repository" else "github",
            "source_id": source_id,
            "source_status": "complete",
            "pagination_completeness": "complete",
        }
    )


def _wrapper_payload(structured_output: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "assistant text summary",
        "structured_output": structured_output,
    }


def _ok_agent_result(payload: dict[str, Any]) -> rr.AgentInvocationResult:
    return rr.AgentInvocationResult(status="ok", structured_output=payload, raw_stdout_excerpt=None, exit_code=0, reason_code=None)


def _observer_request(agent_name: str) -> rr.AgentInvocationRequest:
    return rr.AgentInvocationRequest(agent_name=agent_name, prompt="observe", json_schema_path="/tmp/schema.json", cwd="/repo")


def _make_observer_invoke(run_id: str, digest: str):
    def _invoke(request: rr.AgentInvocationRequest) -> rr.AgentInvocationResult:
        bundle = rr.EvidenceBundle(
            run_id=run_id,
            base_sha=_FULL_SHA,
            source_set_digest=digest,
            observer_id=request.agent_name,
            evidence_ref=f"evidence://{run_id}/{request.agent_name}",
            findings=[{"claim": f"finding from {request.agent_name}", "claim_class": "process"}],
        )
        return _ok_agent_result(json.loads(bundle.to_wire()))

    return _invoke


def _make_evaluator_invoke(candidate_records: list[dict[str, Any]]):
    def _invoke(request: rr.EvaluatorRequest) -> rr.AgentInvocationResult:
        evaluation = rr.Evaluation(
            run_id=request.run_id,
            base_sha=request.base_sha,
            source_set_digest=request.source_set_digest,
            candidate_records=candidate_records,
            evidence_ref="evidence://evaluation",
        )
        return _ok_agent_result(json.loads(evaluation.to_wire()))

    return _invoke


def test_read_version_propagates_to_expected_previous_digest() -> None:
    # GIVEN a PreviousStateProvider whose read_version is a real prior
    # publication_digest (Issue #2238's own digest format)
    read_version = "sha256:" + "7" * 64
    provider = rr.FixturePreviousStateProvider(
        fixtures={
            (_REPO_ID, rr.DEFAULT_PREVIOUS_STATE_SCOPE): rr.PreviousStateResult(
                status="available", previous_run_ref="run-0", candidates=[], read_version=read_version
            )
        }
    )
    expected_digest = rr.compute_source_set_digest([_fake_collector_result("repository", _FULL_SHA).observation])

    # WHEN execute_run() -- the production composition run_cli() also calls
    # -- runs with that provider injected
    publish_request = rr.execute_run(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[lambda base_sha: _fake_collector_result("repository", base_sha)],
        observer_requests=[_observer_request("retrospective-runtime-observer")],
        invoke=_make_observer_invoke("run-2", expected_digest),
        invoke_evaluator=_make_evaluator_invoke([_new_candidate()]),
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac2",
        idempotency_key="idem-ac2",
        run_id="run-2",
        previous_state_provider=provider,
    )

    # THEN the returned PublishRequest.expected_previous_digest is exactly
    # the provider's read_version -- not None, not a hand-supplied value
    assert publish_request.expected_previous_digest == read_version


def test_read_version_propagates_to_expected_previous_digest_none_when_no_history() -> None:
    # GIVEN the default (unwired) provider -- no_history, read_version=None
    expected_digest = rr.compute_source_set_digest([_fake_collector_result("repository", _FULL_SHA).observation])

    # WHEN execute_run() runs without an explicit provider
    publish_request = rr.execute_run(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[lambda base_sha: _fake_collector_result("repository", base_sha)],
        observer_requests=[_observer_request("retrospective-runtime-observer")],
        invoke=_make_observer_invoke("run-3", expected_digest),
        invoke_evaluator=_make_evaluator_invoke([_new_candidate()]),
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac2-none",
        idempotency_key="idem-ac2-none",
        run_id="run-3",
    )

    # THEN expected_previous_digest stays None (no_history has read_version=None)
    assert publish_request.expected_previous_digest is None


# ---------------------------------------------------------------------------
# AC3: publish_request produces publication_digest
# ---------------------------------------------------------------------------


def test_publish_request_produces_publication_digest() -> None:
    candidate = _new_candidate()
    delta_results = [{"finding_identity": candidate["finding_contract"]["identity"]["value"], "evaluation_status": "classified", "delta_status": "new"}]

    # WHEN build_run_envelope() consumes a PUBLISH_REQUEST_V1-shaped input
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac3",
        run_identity={"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[candidate],
        delta_results=delta_results,
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-22T00:00:00Z",
    )

    # THEN a well-formed sha256-jcs-v1 digest is produced
    assert envelope["publication_digest"].startswith("sha256:")
    assert len(envelope["publication_digest"]) == len("sha256:") + 64

    # AND it is bound to delta_results (not only candidate_records) --
    # changing delta_results alone changes the digest
    other_envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac3",
        run_identity={"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[candidate],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-22T00:00:00Z",
    )
    assert other_envelope["publication_digest"] != envelope["publication_digest"]

    # AND full candidate_records/delta_results are carried through verbatim
    assert envelope["candidate_records"] == [candidate]
    assert envelope["delta_results"] == delta_results


# ---------------------------------------------------------------------------
# AC4: human authorization gate -- fail-closed
# ---------------------------------------------------------------------------


def test_authorization_gate_blocks_without_receipt_or_confirmation() -> None:
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac4")
    ctx = pr.AuthorizationContext()  # neither a receipt path nor a tty_confirm callback

    with pytest.raises(pr.AuthorizationDenied) as excinfo:
        pr.publish_run(publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, generated_at="2026-08-22T00:00:00Z")

    assert excinfo.value.reason_code == "authorization_missing"
    assert transport.create_call_count == 0


def test_authorization_gate_blocks_when_tty_confirm_present_but_not_a_tty() -> None:
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac4b")
    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: True, is_tty=lambda: False)

    with pytest.raises(pr.AuthorizationDenied) as excinfo:
        pr.publish_run(publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, generated_at="2026-08-22T00:00:00Z")

    assert excinfo.value.reason_code == "authorization_missing"
    assert transport.create_call_count == 0


def test_authorization_gate_blocks_when_tty_confirm_declines() -> None:
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac4c")
    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: False, is_tty=lambda: True)

    with pytest.raises(pr.AuthorizationDenied) as excinfo:
        pr.publish_run(publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, generated_at="2026-08-22T00:00:00Z")

    assert excinfo.value.reason_code == "tty_declined"
    assert transport.create_call_count == 0


def test_authorization_gate_has_no_bare_authorized_flag_field() -> None:
    # AuthorizationContext structurally cannot accept a bare
    # --authorized-by-human-style flag as authorization.
    import dataclasses as dc

    field_names = {f.name for f in dc.fields(pr.AuthorizationContext)}
    assert "authorized" not in field_names
    assert "authorized_by_human" not in field_names
    assert field_names == {"receipt_path", "tty_confirm", "is_tty", "clock"}


def test_authorization_gate_succeeds_with_valid_receipt(tmp_path: Path) -> None:
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac4d")
    generated_at = "2026-08-22T00:00:00Z"
    preview = pr.build_run_envelope(
        repository_id=pub_req["repository_id"],
        target_issue=pub_req["target_issue"],
        request_id=pub_req["request_id"],
        run_identity=pub_req["run_identity"],
        candidate_records=pub_req["candidate_records"],
        delta_results=pub_req["delta_results"],
        expected_previous_digest=pub_req["expected_previous_digest"],
        parent_record_digest=None,
        generated_at=generated_at,
    )
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            _valid_receipt(
                publication_digest=preview["publication_digest"],
                repository_id=pub_req["repository_id"],
                target_issue=pub_req["target_issue"],
                request_id=pub_req["request_id"],
            )
        )
    )
    ctx = pr.AuthorizationContext(receipt_path=receipt_path, clock=lambda: dt.datetime(2026, 8, 22, 0, 5, tzinfo=dt.timezone.utc))

    result = pr.publish_run(publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, generated_at=generated_at)

    assert result.status == "published"
    assert transport.create_call_count == 1


def test_authorization_gate_rejects_expired_receipt(tmp_path: Path) -> None:
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac4e")
    generated_at = "2026-08-22T00:00:00Z"
    preview = pr.build_run_envelope(
        repository_id=pub_req["repository_id"],
        target_issue=pub_req["target_issue"],
        request_id=pub_req["request_id"],
        run_identity=pub_req["run_identity"],
        candidate_records=pub_req["candidate_records"],
        delta_results=pub_req["delta_results"],
        expected_previous_digest=pub_req["expected_previous_digest"],
        parent_record_digest=None,
        generated_at=generated_at,
    )
    receipt = _valid_receipt(
        publication_digest=preview["publication_digest"],
        repository_id=pub_req["repository_id"],
        target_issue=pub_req["target_issue"],
        request_id=pub_req["request_id"],
    )
    receipt["expires_at"] = "2020-01-01T00:00:00Z"
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt))
    ctx = pr.AuthorizationContext(receipt_path=receipt_path, clock=lambda: dt.datetime(2026, 8, 22, 0, 5, tzinfo=dt.timezone.utc))

    with pytest.raises(pr.AuthorizationDenied) as excinfo:
        pr.publish_run(publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, generated_at=generated_at)

    assert excinfo.value.reason_code == "receipt_expired"
    assert transport.create_call_count == 0


# ---------------------------------------------------------------------------
# AC5: idempotency guard -- duplicate suppression (distinct from AC6)
# ---------------------------------------------------------------------------


def test_idempotency_key_noop_vs_conflict() -> None:
    transport = FakeIssueCommentTransport()
    candidate = _new_candidate()
    run_identity = {"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST}
    idempotency_key = pr.compute_idempotency_key(
        repository_id=_REPO_ID, base_sha=run_identity["base_sha"], source_set_digest=run_identity["source_set_digest"], scope=pr.DEFAULT_SCOPE
    )

    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac5-a",
        run_identity=run_identity,
        candidate_records=[candidate],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-22T00:00:00Z",
    )
    transport.seed_comment(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope))

    # WHEN the SAME idempotency_key + SAME publication_digest is evaluated
    decision, existing = pr.evaluate_idempotency(
        transport, _REPO, _TARGET_ISSUE, idempotency_key=idempotency_key, publication_digest=envelope["publication_digest"]
    )
    # THEN it is a no_op (existing record found, byte-identical content)
    assert decision == "no_op"
    assert existing is not None

    # WHEN the SAME idempotency_key but a DIFFERENT publication_digest is
    # evaluated (e.g. same run coordinates, different candidate content)
    other_candidate = _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.resolved.valid.json")
    other_envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac5-b",
        run_identity=run_identity,
        candidate_records=[other_candidate],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-22T01:00:00Z",
    )
    # the recomputed idempotency key is identical (same repository_id/base_sha/source_set_digest/scope)
    assert other_envelope["idempotency_key"] == idempotency_key
    decision2, existing2 = pr.evaluate_idempotency(
        transport, _REPO, _TARGET_ISSUE, idempotency_key=idempotency_key, publication_digest=other_envelope["publication_digest"]
    )
    # THEN it is a conflict
    assert decision2 == "conflict"
    assert existing2 is not None

    # WHEN a genuinely NEW idempotency key (different base_sha) is evaluated
    new_key = pr.compute_idempotency_key(repository_id=_REPO_ID, base_sha="b" * 40, source_set_digest=_DIGEST, scope=pr.DEFAULT_SCOPE)
    decision3, existing3 = pr.evaluate_idempotency(transport, _REPO, _TARGET_ISSUE, idempotency_key=new_key, publication_digest="sha256:" + "0" * 64)
    # THEN it is a fresh publish
    assert decision3 == "publish"
    assert existing3 is None


def test_idempotency_key_recomputed_not_trusted_from_caller() -> None:
    # A caller-supplied idempotency_key value is never trusted -- the
    # publisher always recomputes it from (repository_id, base_sha,
    # source_set_digest, scope) regardless of what a PUBLISH_REQUEST_V1
    # producer put in its own idempotency_key field.
    run_identity = {"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST}
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac5-c",
        run_identity=run_identity,
        candidate_records=[],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-22T00:00:00Z",
    )
    recomputed = pr.compute_idempotency_key(
        repository_id=_REPO_ID, base_sha=run_identity["base_sha"], source_set_digest=run_identity["source_set_digest"], scope=pr.DEFAULT_SCOPE
    )
    assert envelope["idempotency_key"] == recomputed


# ---------------------------------------------------------------------------
# AC6: optimistic concurrency guard -- best-effort, distinct from AC5
# ---------------------------------------------------------------------------


def test_optimistic_concurrency_best_effort_conflict_detection() -> None:
    transport = FakeIssueCommentTransport()
    candidate = _new_candidate()
    run_identity = {"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST}

    # GIVEN no prior record: head is None
    head0 = pr.check_optimistic_concurrency_precondition(
        transport, _REPO, _TARGET_ISSUE, repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, expected_previous_digest=None
    )
    assert head0 is None

    envelope0 = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac6-0",
        run_identity=run_identity,
        candidate_records=[candidate],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=head0,
        generated_at="2026-08-22T00:00:00Z",
    )
    transport.create_comment(repo=_REPO, issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope0))

    # WHEN a stale expected_previous_digest disagrees with the current head
    with pytest.raises(pr.StaleWriteDetected) as excinfo:
        pr.check_optimistic_concurrency_precondition(
            transport, _REPO, _TARGET_ISSUE, repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, expected_previous_digest="sha256:" + "0" * 64
        )
    assert excinfo.value.reason_code == "stale_expected_previous_digest"

    # GIVEN two "concurrent" runs both read the SAME head (this is the race
    # window the best-effort guard cannot fully close -- GitHub Issue
    # comment creation is not atomic compare-and-swap)
    head1 = pr.check_optimistic_concurrency_precondition(
        transport, _REPO, _TARGET_ISSUE, repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, expected_previous_digest=envelope0["publication_digest"]
    )
    assert head1 == envelope0["publication_digest"]

    envelope_a = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac6-a",
        run_identity=run_identity,
        candidate_records=[candidate],
        delta_results=[],
        expected_previous_digest=head1,
        parent_record_digest=head1,
        generated_at="2026-08-22T01:00:00Z",
    )
    envelope_b = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac6-b",
        run_identity=run_identity,
        candidate_records=[candidate],
        delta_results=[],
        expected_previous_digest=head1,
        parent_record_digest=head1,
        generated_at="2026-08-22T02:00:00Z",
    )
    comment_a = transport.create_comment(repo=_REPO, issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope_a))
    transport.create_comment(repo=_REPO, issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope_b))

    # WHEN the post-write rescan runs for run A (AC6 step (c)/(d))
    conflict_detected = pr.detect_post_write_sibling_conflict(
        transport, _REPO, _TARGET_ISSUE, parent_record_digest=head1, own_comment_id=comment_a["id"]
    )
    # THEN it detects run B as a sibling appended against the same parent
    assert conflict_detected is True

    # AND a record with a unique parent has no sibling conflict
    no_conflict = pr.detect_post_write_sibling_conflict(
        transport, _REPO, _TARGET_ISSUE, parent_record_digest=envelope_a["publication_digest"], own_comment_id=comment_a["id"]
    )
    assert no_conflict is False


# ---------------------------------------------------------------------------
# AC7: post-write readback -- canonical digest, never raw Markdown bytes
# ---------------------------------------------------------------------------


def test_canonical_get_readback_digest_match() -> None:
    transport = FakeIssueCommentTransport()
    candidate = _new_candidate()
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac7",
        run_identity={"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[candidate],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-22T00:00:00Z",
    )
    comment = transport.create_comment(repo=_REPO, issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope))

    # WHEN readback verification runs against the just-created comment
    # THEN it succeeds (no exception)
    pr.verify_readback_digest(transport, _REPO, comment_id=comment["id"], expected_publication_digest=envelope["publication_digest"])

    # AND it is insensitive to raw Markdown formatting differences -- only
    # the canonical (sha256-jcs-v1) digest is compared, never raw bytes
    marker = f'<!-- agent_retrospective_run:v1 repository_id={envelope["repository_id"]} idempotency_key={envelope["idempotency_key"]} -->'
    reformatted_fenced = "```json\n" + json.dumps(envelope, indent=4, sort_keys=True) + "\n```"
    reformatted_body = f"{marker}\n\n{reformatted_fenced}\n"
    assert reformatted_body != pr.render_comment_body(envelope)  # genuinely different raw bytes
    comment2 = transport.create_comment(repo=_REPO, issue_number=_TARGET_ISSUE, body=reformatted_body)
    pr.verify_readback_digest(transport, _REPO, comment_id=comment2["id"], expected_publication_digest=envelope["publication_digest"])

    # AND a tampered stored body IS caught
    tampered_body = transport._comments[comment["id"]]["body"].replace(candidate["candidate_id"], "tampered-candidate-id")
    transport._comments[comment["id"]]["body"] = tampered_body
    with pytest.raises(pr.ReadbackVerificationFailed) as excinfo:
        pr.verify_readback_digest(transport, _REPO, comment_id=comment["id"], expected_publication_digest=envelope["publication_digest"])
    assert excinfo.value.reason_code in ("readback_self_digest_mismatch", "readback_expected_digest_mismatch")


# ---------------------------------------------------------------------------
# AC8: candidate/finding/delta full round-trip
# ---------------------------------------------------------------------------


def test_candidate_finding_delta_full_roundtrip() -> None:
    candidate = _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.recurrent_regressed.valid.json")
    delta_results = [
        {"finding_identity": candidate["finding_contract"]["identity"]["value"], "evaluation_status": "classified", "delta_status": "recurrent"}
    ]
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac8",
        run_identity={"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[candidate],
        delta_results=delta_results,
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-22T00:00:00Z",
    )

    # WHEN the envelope is rendered to a comment body and parsed back
    parsed = pr.extract_envelope_from_body(pr.render_comment_body(envelope))

    # THEN the full candidate record, finding_contract sub-fields, and
    # delta_results all round-trip byte-identically
    assert parsed is not None
    assert parsed["candidate_records"] == [candidate]
    assert parsed["delta_results"] == delta_results
    assert parsed["candidate_records"][0]["finding_contract"]["identity"] == candidate["finding_contract"]["identity"]
    assert parsed["candidate_records"][0]["finding_contract"]["claim_class"] == candidate["finding_contract"]["claim_class"]
    assert parsed["candidate_records"][0]["finding_contract"]["evaluations"] == candidate["finding_contract"]["evaluations"]

    # AND the round-tripped candidate is still canonical-schema-valid
    _validate_mod.validate_candidate(parsed["candidate_records"][0])

    # AND candidate:finding is 1:1 -- exactly one finding_identity per
    # candidate, no duplicates within delta_results
    identities = [entry["finding_identity"] for entry in parsed["delta_results"]]
    assert len(identities) == len(set(identities))
    assert len(identities) == len(parsed["candidate_records"])


def test_candidate_finding_delta_full_roundtrip_legacy_candidate_has_no_identity() -> None:
    # A legacy candidate (no finding_contract) round-trips too, but is
    # excluded from delta correlation -- this is exercised at the
    # provider/compute_delta layer (AC9), not by this envelope build/parse
    # layer, which simply preserves whatever candidate_records it is given.
    legacy_candidate = _validate_mod.load_fixture("agent_improvement_candidate_v1.valid.json")
    assert "finding_contract" not in legacy_candidate
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac8-legacy",
        run_identity={"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[legacy_candidate],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-22T00:00:00Z",
    )
    parsed = pr.extract_envelope_from_body(pr.render_comment_body(envelope))
    assert parsed["candidate_records"] == [legacy_candidate]


# ---------------------------------------------------------------------------
# AC9: previous-state provider status classification from real data shape
# ---------------------------------------------------------------------------


def test_previous_state_status_classification_no_history() -> None:
    transport = FakeIssueCommentTransport()
    provider = pr.IssueCommentPreviousStateProvider(repo=_REPO, target_issue=_TARGET_ISSUE, transport=transport)

    result = provider.get(repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-jcs-v1")

    assert result.status == "no_history"
    assert result.read_version is None
    assert result.candidates == []


def test_previous_state_status_classification_legacy_unavailable() -> None:
    transport = FakeIssueCommentTransport()
    # a pre-#2238 retrospective-adjacent comment this module cannot parse as
    # its own envelope (no fenced JSON block, doesn't match the marker)
    transport.seed_comment(
        issue_number=_TARGET_ISSUE, body="agent_retrospective_run notes from before Issue #2238 (free text, no structured envelope)"
    )
    provider = pr.IssueCommentPreviousStateProvider(repo=_REPO, target_issue=_TARGET_ISSUE, transport=transport)

    result = provider.get(repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-jcs-v1")

    assert result.status == "legacy_unavailable"
    assert result.candidates == []


def test_previous_state_status_classification_available() -> None:
    transport = FakeIssueCommentTransport()
    now = dt.datetime(2026, 8, 22, 12, 0, 0, tzinfo=dt.timezone.utc)
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac9-avail",
        run_identity={"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[_new_candidate()],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at=_iso(now - dt.timedelta(hours=1)),
    )
    transport.seed_comment(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope))
    provider = pr.IssueCommentPreviousStateProvider(repo=_REPO, target_issue=_TARGET_ISSUE, transport=transport, clock=lambda: now)

    result = provider.get(repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-jcs-v1")

    assert result.status == "available"
    assert result.read_version == envelope["publication_digest"]
    assert result.candidates == [_new_candidate()]


def test_previous_state_status_classification_partial() -> None:
    transport = FakeIssueCommentTransport()
    now = dt.datetime(2026, 8, 22, 12, 0, 0, tzinfo=dt.timezone.utc)
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac9-partial",
        run_identity={"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[_new_candidate()],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at=_iso(now - dt.timedelta(minutes=1)),
        pagination_completeness="partial",
    )
    transport.seed_comment(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope))
    provider = pr.IssueCommentPreviousStateProvider(repo=_REPO, target_issue=_TARGET_ISSUE, transport=transport, clock=lambda: now)

    result = provider.get(repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-jcs-v1")

    assert result.status == "partial"


def test_previous_state_status_classification_stale() -> None:
    transport = FakeIssueCommentTransport()
    now = dt.datetime(2026, 8, 22, 12, 0, 0, tzinfo=dt.timezone.utc)
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac9-stale",
        run_identity={"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[_new_candidate()],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at=_iso(now - dt.timedelta(days=30)),
    )
    transport.seed_comment(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope))
    provider = pr.IssueCommentPreviousStateProvider(repo=_REPO, target_issue=_TARGET_ISSUE, transport=transport, clock=lambda: now)

    result = provider.get(repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-jcs-v1")

    assert result.status == "stale"


# ---------------------------------------------------------------------------
# AC10: ambiguous POST failure recovery by request_id
# ---------------------------------------------------------------------------


def test_ambiguous_post_failure_recovery_by_request_id_recovers_when_already_created() -> None:
    transport = FakeIssueCommentTransport()
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac10-recover",
        run_identity={"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[_new_candidate()],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-22T00:00:00Z",
    )
    body = pr.render_comment_body(envelope)
    # simulate: the POST timed out from the caller's perspective, but it
    # actually landed server-side (an ambiguous outcome)
    transport.queue_create_side_effect(pr.AmbiguousTransportError("simulated timeout"), also_create=True)

    comment, recovered = pr.create_comment_with_recovery(
        transport,
        _REPO,
        _TARGET_ISSUE,
        body=body,
        request_id="req-ac10-recover",
        idempotency_key=envelope["idempotency_key"],
        publication_digest=envelope["publication_digest"],
    )

    assert recovered is True
    assert comment["id"] is not None
    # no blind second POST was issued
    assert transport.create_call_count == 1


def test_ambiguous_post_failure_recovery_by_request_id_conflict_on_digest_mismatch() -> None:
    transport = FakeIssueCommentTransport()
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac10-conflict",
        run_identity={"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[_new_candidate()],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-22T00:00:00Z",
    )
    # a DIFFERENT envelope with the same request_id was actually created
    # server-side (e.g. a prior distinct attempt)
    other_envelope = dict(envelope)
    other_envelope["candidate_records"] = []
    other_envelope["publication_digest"] = pr.compute_publication_digest({k: v for k, v in other_envelope.items() if k != "publication_digest"})
    transport.seed_comment(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(other_envelope))
    transport.queue_create_side_effect(pr.AmbiguousTransportError("simulated timeout"), also_create=False)

    with pytest.raises(pr.PublicationConflict) as excinfo:
        pr.create_comment_with_recovery(
            transport,
            _REPO,
            _TARGET_ISSUE,
            body=pr.render_comment_body(envelope),
            request_id="req-ac10-conflict",
            idempotency_key=envelope["idempotency_key"],
            publication_digest=envelope["publication_digest"],
        )

    assert excinfo.value.reason_code == "ambiguous_post_recovered_conflict"


def test_ambiguous_post_failure_recovery_by_request_id_bounded_retry_when_not_found() -> None:
    transport = FakeIssueCommentTransport()
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac10-retry",
        run_identity={"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[_new_candidate()],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-22T00:00:00Z",
    )
    # first attempt is ambiguous AND did not actually land server-side
    transport.queue_create_side_effect(pr.AmbiguousTransportError("simulated timeout"), also_create=False)

    comment, recovered = pr.create_comment_with_recovery(
        transport,
        _REPO,
        _TARGET_ISSUE,
        body=pr.render_comment_body(envelope),
        request_id="req-ac10-retry",
        idempotency_key=envelope["idempotency_key"],
        publication_digest=envelope["publication_digest"],
    )

    assert recovered is False
    assert comment["id"] is not None
    # first (ambiguous) attempt + one bounded retry that succeeded
    assert transport.create_call_count == 2


# ---------------------------------------------------------------------------
# AC11: index-update failure -> published_index_stale, no rollback
# ---------------------------------------------------------------------------


def test_index_update_failure_does_not_rollback_primary_record(tmp_path: Path) -> None:
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac11")
    generated_at = "2026-08-22T00:00:00Z"
    preview = pr.build_run_envelope(
        repository_id=pub_req["repository_id"],
        target_issue=pub_req["target_issue"],
        request_id=pub_req["request_id"],
        run_identity=pub_req["run_identity"],
        candidate_records=pub_req["candidate_records"],
        delta_results=pub_req["delta_results"],
        expected_previous_digest=pub_req["expected_previous_digest"],
        parent_record_digest=None,
        generated_at=generated_at,
    )
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            _valid_receipt(
                publication_digest=preview["publication_digest"],
                repository_id=pub_req["repository_id"],
                target_issue=pub_req["target_issue"],
                request_id=pub_req["request_id"],
            )
        )
    )
    ctx = pr.AuthorizationContext(receipt_path=receipt_path, clock=lambda: dt.datetime(2026, 8, 22, 0, 5, tzinfo=dt.timezone.utc))

    def _failing_index_updater() -> None:
        raise RuntimeError("index update boom")

    # WHEN the primary POST succeeds but the derived-index update fails
    result = pr.publish_run(
        publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, generated_at=generated_at, index_updater=_failing_index_updater
    )

    # THEN status is published_index_stale -- distinct from a hard failure
    assert result.status == "published_index_stale"
    assert result.comment_id is not None
    assert any("index update boom" in error for error in result.errors)

    # AND the primary record was NOT rolled back -- it is still readable
    fetched = transport.get_comment(repo=_REPO, comment_id=result.comment_id)
    assert pr.extract_envelope_from_body(fetched["body"]) is not None


def test_index_update_success_reports_published() -> None:
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac11-ok")
    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: True, is_tty=lambda: True)
    calls: list[str] = []

    result = pr.publish_run(
        publish_request=pub_req,
        repo=_REPO,
        transport=transport,
        auth_ctx=ctx,
        generated_at="2026-08-22T00:00:00Z",
        index_updater=lambda: calls.append("updated"),
    )

    assert result.status == "published"
    assert calls == ["updated"]


# ---------------------------------------------------------------------------
# AC12: public-safety validator runs before every POST
# ---------------------------------------------------------------------------


def test_public_safety_validator_runs_before_post_rejects_token_pattern() -> None:
    transport = FakeIssueCommentTransport()
    candidate = _new_candidate()
    tainted_delta = [
        {
            "finding_identity": candidate["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "new",
            "note": "leaked credential ghp_abcdefghijklmnopqrstuvwxyz012345",
        }
    ]
    pub_req = _publish_request_dict(candidate_records=[candidate], delta_results=tainted_delta, request_id="req-ac12-token")
    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: True, is_tty=lambda: True)

    with pytest.raises(pr.PublicSafetyViolation) as excinfo:
        pr.publish_run(publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, generated_at="2026-08-22T00:00:00Z")

    assert excinfo.value.reason_code == "token_pattern_detected"
    # the validator ran BEFORE any POST -- no comment was created
    assert transport.create_call_count == 0


def test_public_safety_validator_runs_before_post_rejects_absolute_path() -> None:
    transport = FakeIssueCommentTransport()
    candidate = _new_candidate()
    tainted_delta = [
        {
            "finding_identity": candidate["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "new",
            "note": "found at /home/squne/secrets/credentials.json",
        }
    ]
    pub_req = _publish_request_dict(candidate_records=[candidate], delta_results=tainted_delta, request_id="req-ac12-path")
    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: True, is_tty=lambda: True)

    with pytest.raises(pr.PublicSafetyViolation) as excinfo:
        pr.publish_run(publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, generated_at="2026-08-22T00:00:00Z")

    assert excinfo.value.reason_code == "absolute_path_detected"
    assert transport.create_call_count == 0


def test_public_safety_validator_runs_before_post_rejects_smuggled_authority_field() -> None:
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac12-smuggle",
        run_identity={"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[],
        delta_results=[{"finding_identity": "x", "raw_stdout": "leaked raw output"}],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-22T00:00:00Z",
    )

    with pytest.raises(pr.PublicSafetyViolation) as excinfo:
        pr.run_public_safety_validator(envelope)

    assert excinfo.value.reason_code == "smuggled_authority_field"


def test_public_safety_validator_runs_before_post_rejects_disallowed_top_level_field() -> None:
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac12-field",
        run_identity={"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-22T00:00:00Z",
    )
    envelope["mutation_capability"] = True

    with pytest.raises(pr.PublicSafetyViolation) as excinfo:
        pr.run_public_safety_validator(envelope)

    assert excinfo.value.reason_code in ("field_not_allowlisted", "smuggled_authority_field")


def test_public_safety_validator_runs_before_post_allows_clean_envelope() -> None:
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac12-clean",
        run_identity={"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[_new_candidate()],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-22T00:00:00Z",
    )
    # does not raise
    pr.run_public_safety_validator(envelope)
