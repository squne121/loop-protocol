#!/usr/bin/env python3
"""Tests for persist_retrospective_run.py (Issue #2238, Child 5 of #2192).

Fixture/mock-based only (Runtime Verification Applicability: not_applicable
-- see Issue #2238 body). No live GitHub call is ever made; the only I/O
boundary (``IssueCommentTransportProtocol``) is dependency-injected via
``FakeIssueCommentTransport`` throughout. Live smoke/security verification
is Child 6 (#2239)'s responsibility.

Issue #2238 fix_delta (OWNER adversarial review, PR #2304
issuecomment-5381003316): this revision fixes every call site broken by the
P0-1..P0-7/P1-1..P1-4 API changes (``trusted_publisher_logins`` threading,
``build_run_envelope``'s ``source_observations``-driven pagination
signaling, ``evaluate_idempotency``'s stable ``request_payload_digest``,
receipt TTL bound) and adds the 10 required regression tests
(``test_regression_1``..``test_regression_10``).

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

Plus the 10 required regression tests from the fix_delta request
(``test_regression_1``..``test_regression_10``).
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

#: Issue #2238 P0-6 fix_delta: the sole trusted publisher identity used
#: throughout this test file's fakes -- FakeIssueCommentTransport stamps
#: every comment it creates/seeds with this login by default.
_TRUSTED_LOGIN = "agent-retrospective-bot"
_TRUSTED = frozenset({_TRUSTED_LOGIN})
_UNTRUSTED_LOGIN = "some-other-account"


# ---------------------------------------------------------------------------
# shared fakes / helpers
# ---------------------------------------------------------------------------


class FakeIssueCommentTransport:
    """Hermetic, in-memory ``IssueCommentTransportProtocol`` implementation.
    Every persist_retrospective_run.py function under test is exercised
    exclusively against this fake -- no subprocess, no network call. Every
    comment carries a ``user.login`` (Issue #2238 P0-6) -- defaults to
    ``_TRUSTED_LOGIN`` so existing AC tests keep exercising the "trusted"
    path; regression test 5 overrides it explicitly."""

    def __init__(self) -> None:
        self._comments: dict[int, dict[str, Any]] = {}
        self._next_id = 1000
        self.create_call_count = 0
        self._queued: list[dict[str, Any]] = []

    def queue_create_side_effect(self, exc: Exception, *, also_create: bool = False) -> None:
        self._queued.append({"exc": exc, "also_create": also_create})

    def _insert(
        self, *, issue_number: int, body: str, comment_id: int | None = None, login: str = _TRUSTED_LOGIN
    ) -> dict[str, Any]:
        cid = comment_id if comment_id is not None else self._next_id
        self._next_id = max(self._next_id, cid + 1)
        comment = {
            "id": cid,
            "html_url": f"https://github.com/x/y/issues/{issue_number}#issuecomment-{cid}",
            "body": body,
            "user": {"login": login},
            "_issue_number": issue_number,
        }
        self._comments[cid] = comment
        return dict(comment)

    def seed_comment(
        self, *, issue_number: int, body: str, comment_id: int | None = None, login: str = _TRUSTED_LOGIN
    ) -> dict[str, Any]:
        return self._insert(issue_number=issue_number, body=body, comment_id=comment_id, login=login)

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
    source_observations: list[dict[str, Any]] | None = None,
    generated_at: str | None = None,
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
        "source_observations": source_observations
        if source_observations is not None
        else [
            {
                "source_type": "repository",
                "source_id": "repository",
                "source_status": "complete",
                "pagination_completeness": "complete",
            }
        ],
        "generated_at": generated_at or "2026-08-22T00:00:00Z",
    }


def _new_candidate() -> dict[str, Any]:
    return _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")


def _iso(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_receipt(
    *, publication_digest: str, repository_id: str, target_issue: int, request_id: str
) -> dict[str, Any]:
    # Issue #2238 P0-2 fix_delta: TTL must be <= MAX_AUTHORIZATION_RECEIPT_TTL_SECONDS
    # (10 minutes) -- previously an arbitrary far-future expires_at.
    return {
        "schema_version": pr.HUMAN_AUTHORIZATION_RECEIPT_SCHEMA,
        "request_id": request_id,
        "publication_digest": publication_digest,
        "repository_id": repository_id,
        "target_issue": target_issue,
        "operation": "publish_retrospective_run",
        "approved_at": "2026-08-22T00:00:00Z",
        "expires_at": "2026-08-22T00:05:00Z",
    }


def _build_envelope(
    *,
    request_id: str,
    candidate_records: list[dict[str, Any]] | None = None,
    delta_results: list[dict[str, Any]] | None = None,
    run_identity: dict[str, Any] | None = None,
    expected_previous_digest: str | None = None,
    parent_record_digest: str | None = None,
    generated_at: str = "2026-08-22T00:00:00Z",
    source_observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id=request_id,
        run_identity=run_identity or {"run_id": "run-1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=candidate_records or [],
        delta_results=delta_results or [],
        expected_previous_digest=expected_previous_digest,
        parent_record_digest=parent_record_digest,
        generated_at=generated_at,
        source_observations=source_observations,
    )


def _new_candidate() -> dict[str, Any]:  # noqa: F811 -- intentional redefinition kept adjacent to first use
    return _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")


# ---------------------------------------------------------------------------
# AC1: main() actually builds and uses the persistence-backed provider
# ---------------------------------------------------------------------------


def test_production_provider_used(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(rr, "_check_gh_auth_available", lambda **kwargs: None)

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

    assert exit_code == 0
    provider = captured["provider"]
    assert type(provider).__name__ == "IssueCommentPreviousStateProvider"
    assert not isinstance(provider, rr.FixturePreviousStateProvider)
    assert provider._repo == _REPO_ID
    assert provider._target_issue == _TARGET_ISSUE
    assert hasattr(provider, "_transport") and type(provider._transport).__name__ == "GhCliIssueCommentTransport"


def test_production_provider_used_explicit_fixture_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # Issue #2238 P1-3 fix_delta: --state-backend default is now
    # "issue-comments" (production); the fixture backend is now opt-in.
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

    rr.main(
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
            "fixture",
        ]
    )

    assert isinstance(captured["provider"], rr.FixturePreviousStateProvider)


def test_production_provider_default_backend_requires_gh_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    # Issue #2238 P1-3 fix_delta: the new default ("issue-comments") never
    # silently falls back to fixture when gh auth is unavailable -- it fails
    # closed with a typed error instead.
    def _boom(**kwargs: Any) -> None:
        raise rr.GhAuthUnavailable("gh_auth_status_failed:not logged in")

    monkeypatch.setattr(rr, "_check_gh_auth_available", _boom)

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
        ]
    )

    assert exit_code == 1


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
    return rr.AgentInvocationResult(
        status="ok", structured_output=payload, raw_stdout_excerpt=None, exit_code=0, reason_code=None
    )


def _observer_request(agent_name: str) -> rr.AgentInvocationRequest:
    return rr.AgentInvocationRequest(
        agent_name=agent_name, prompt="observe", json_schema_path="/tmp/schema.json", cwd="/repo"
    )


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
    read_version = "sha256:" + "7" * 64
    provider = rr.FixturePreviousStateProvider(
        fixtures={
            (_REPO_ID, rr.DEFAULT_PREVIOUS_STATE_SCOPE): rr.PreviousStateResult(
                status="available", previous_run_ref="run-0", candidates=[], read_version=read_version
            )
        }
    )
    expected_digest = rr.compute_source_set_digest([_fake_collector_result("repository", _FULL_SHA).observation])

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

    assert publish_request.expected_previous_digest == read_version
    # Issue #2238 P0-5 fix_delta: the real per-collector source_observations
    # (not a placeholder) is threaded through into run_identity additively.
    assert publish_request.run_identity["source_observations"] == [
        _fake_collector_result("repository", _FULL_SHA).observation
    ]
    assert publish_request.run_identity["generated_at"]
    assert publish_request.run_identity["runtime_version"] == rr.RUNTIME_VERSION


def test_read_version_propagates_to_expected_previous_digest_none_when_no_history() -> None:
    expected_digest = rr.compute_source_set_digest([_fake_collector_result("repository", _FULL_SHA).observation])

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

    assert publish_request.expected_previous_digest is None


# ---------------------------------------------------------------------------
# AC3: publish_request produces publication_digest
# ---------------------------------------------------------------------------


def test_publish_request_produces_publication_digest() -> None:
    candidate = _new_candidate()
    delta_results = [
        {
            "finding_identity": candidate["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "new",
        }
    ]

    envelope = _build_envelope(request_id="req-ac3", candidate_records=[candidate], delta_results=delta_results)

    assert envelope["publication_digest"].startswith("sha256:")
    assert len(envelope["publication_digest"]) == len("sha256:") + 64

    other_envelope = _build_envelope(request_id="req-ac3", candidate_records=[candidate], delta_results=[])
    assert other_envelope["publication_digest"] != envelope["publication_digest"]

    assert envelope["candidate_records"] == [candidate]
    assert envelope["delta_results"] == delta_results


# ---------------------------------------------------------------------------
# AC4: human authorization gate -- fail-closed
# ---------------------------------------------------------------------------


def test_authorization_gate_blocks_without_receipt_or_confirmation() -> None:
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac4")
    ctx = pr.AuthorizationContext()

    with pytest.raises(pr.AuthorizationDenied) as excinfo:
        pr.publish_run(
            publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, trusted_publisher_logins=_TRUSTED
        )

    assert excinfo.value.reason_code == "authorization_missing"
    assert transport.create_call_count == 0


def test_authorization_gate_blocks_when_tty_confirm_present_but_not_a_tty() -> None:
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac4b")
    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: True, is_tty=lambda: False)

    with pytest.raises(pr.AuthorizationDenied) as excinfo:
        pr.publish_run(
            publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, trusted_publisher_logins=_TRUSTED
        )

    assert excinfo.value.reason_code == "authorization_missing"
    assert transport.create_call_count == 0


def test_authorization_gate_blocks_when_tty_confirm_declines() -> None:
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac4c")
    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: False, is_tty=lambda: True)

    with pytest.raises(pr.AuthorizationDenied) as excinfo:
        pr.publish_run(
            publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, trusted_publisher_logins=_TRUSTED
        )

    assert excinfo.value.reason_code == "tty_declined"
    assert transport.create_call_count == 0


def test_authorization_gate_has_no_bare_authorized_flag_field() -> None:
    import dataclasses as dc

    field_names = {f.name for f in dc.fields(pr.AuthorizationContext)}
    assert "authorized" not in field_names
    assert "authorized_by_human" not in field_names
    assert field_names == {"receipt_path", "tty_confirm", "is_tty", "clock"}


def test_authorization_gate_succeeds_with_valid_receipt(tmp_path: Path) -> None:
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac4d")
    preview = _build_envelope(request_id=pub_req["request_id"], candidate_records=pub_req["candidate_records"])
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
    ctx = pr.AuthorizationContext(
        receipt_path=receipt_path, clock=lambda: dt.datetime(2026, 8, 22, 0, 2, tzinfo=dt.timezone.utc)
    )

    result = pr.publish_run(
        publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, trusted_publisher_logins=_TRUSTED
    )

    assert result.status == "published"
    assert transport.create_call_count == 1


def test_authorization_gate_rejects_expired_receipt(tmp_path: Path) -> None:
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac4e")
    preview = _build_envelope(request_id=pub_req["request_id"], candidate_records=pub_req["candidate_records"])
    receipt = _valid_receipt(
        publication_digest=preview["publication_digest"],
        repository_id=pub_req["repository_id"],
        target_issue=pub_req["target_issue"],
        request_id=pub_req["request_id"],
    )
    receipt["approved_at"] = "2020-01-01T00:00:00Z"
    receipt["expires_at"] = "2020-01-01T00:05:00Z"
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt))
    ctx = pr.AuthorizationContext(
        receipt_path=receipt_path, clock=lambda: dt.datetime(2026, 8, 22, 0, 5, tzinfo=dt.timezone.utc)
    )

    with pytest.raises(pr.AuthorizationDenied) as excinfo:
        pr.publish_run(
            publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, trusted_publisher_logins=_TRUSTED
        )

    assert excinfo.value.reason_code == "receipt_expired"
    assert transport.create_call_count == 0


def test_authorization_gate_rejects_ttl_exceeding_maximum(tmp_path: Path) -> None:
    # Issue #2238 P0-2 fix_delta: a receipt's own (approved_at, expires_at)
    # window cannot exceed MAX_AUTHORIZATION_RECEIPT_TTL_SECONDS, regardless
    # of what the receipt file itself claims.
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac4f")
    preview = _build_envelope(request_id=pub_req["request_id"], candidate_records=pub_req["candidate_records"])
    receipt = _valid_receipt(
        publication_digest=preview["publication_digest"],
        repository_id=pub_req["repository_id"],
        target_issue=pub_req["target_issue"],
        request_id=pub_req["request_id"],
    )
    receipt["expires_at"] = "2099-01-01T00:00:00Z"
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt))
    ctx = pr.AuthorizationContext(
        receipt_path=receipt_path, clock=lambda: dt.datetime(2026, 8, 22, 0, 2, tzinfo=dt.timezone.utc)
    )

    with pytest.raises(pr.AuthorizationDenied) as excinfo:
        pr.publish_run(
            publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, trusted_publisher_logins=_TRUSTED
        )

    assert excinfo.value.reason_code == "receipt_ttl_exceeded"
    assert transport.create_call_count == 0


def test_authorization_gate_rejects_future_approved_at(tmp_path: Path) -> None:
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac4g")
    preview = _build_envelope(request_id=pub_req["request_id"], candidate_records=pub_req["candidate_records"])
    receipt = _valid_receipt(
        publication_digest=preview["publication_digest"],
        repository_id=pub_req["repository_id"],
        target_issue=pub_req["target_issue"],
        request_id=pub_req["request_id"],
    )
    receipt["approved_at"] = "2026-08-22T00:10:00Z"
    receipt["expires_at"] = "2026-08-22T00:15:00Z"
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt))
    ctx = pr.AuthorizationContext(
        receipt_path=receipt_path, clock=lambda: dt.datetime(2026, 8, 22, 0, 2, tzinfo=dt.timezone.utc)
    )

    with pytest.raises(pr.AuthorizationDenied) as excinfo:
        pr.publish_run(
            publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, trusted_publisher_logins=_TRUSTED
        )

    assert excinfo.value.reason_code == "receipt_approved_at_future"


# ---------------------------------------------------------------------------
# AC5: idempotency guard -- duplicate suppression (distinct from AC6)
# ---------------------------------------------------------------------------


def test_idempotency_key_noop_vs_conflict() -> None:
    transport = FakeIssueCommentTransport()
    candidate = _new_candidate()
    run_identity = {"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST}
    idempotency_key = pr.compute_idempotency_key(
        repository_id=_REPO_ID,
        base_sha=run_identity["base_sha"],
        source_set_digest=run_identity["source_set_digest"],
        scope=pr.DEFAULT_SCOPE,
    )
    source_observations = [
        {
            "source_type": "repository",
            "source_id": "repository",
            "source_status": "complete",
            "pagination_completeness": "complete",
        }
    ]

    envelope = _build_envelope(
        request_id="req-ac5-a",
        candidate_records=[candidate],
        run_identity=run_identity,
        source_observations=source_observations,
    )
    transport.seed_comment(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope))

    stable_digest = pr.compute_request_payload_digest(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        scope=pr.DEFAULT_SCOPE,
        run_identity=run_identity,
        candidate_records=[candidate],
        delta_results=[],
        source_observations=source_observations,
    )

    # WHEN the SAME idempotency_key + SAME stable request_payload_digest is evaluated
    decision, existing = pr.evaluate_idempotency(
        transport,
        _REPO,
        _TARGET_ISSUE,
        idempotency_key=idempotency_key,
        request_payload_digest=stable_digest,
        trusted_publisher_logins=_TRUSTED,
    )
    assert decision == "no_op"
    assert existing is not None

    # WHEN the SAME idempotency_key but a DIFFERENT stable digest (different candidates)
    other_candidate = _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.resolved.valid.json")
    other_stable_digest = pr.compute_request_payload_digest(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        scope=pr.DEFAULT_SCOPE,
        run_identity=run_identity,
        candidate_records=[other_candidate],
        delta_results=[],
        source_observations=source_observations,
    )
    decision2, existing2 = pr.evaluate_idempotency(
        transport,
        _REPO,
        _TARGET_ISSUE,
        idempotency_key=idempotency_key,
        request_payload_digest=other_stable_digest,
        trusted_publisher_logins=_TRUSTED,
    )
    assert decision2 == "conflict"
    assert existing2 is not None

    # WHEN a genuinely NEW idempotency key (different base_sha) is evaluated
    new_key = pr.compute_idempotency_key(
        repository_id=_REPO_ID, base_sha="b" * 40, source_set_digest=_DIGEST, scope=pr.DEFAULT_SCOPE
    )
    decision3, existing3 = pr.evaluate_idempotency(
        transport,
        _REPO,
        _TARGET_ISSUE,
        idempotency_key=new_key,
        request_payload_digest="sha256:" + "0" * 64,
        trusted_publisher_logins=_TRUSTED,
    )
    assert decision3 == "publish"
    assert existing3 is None


def test_idempotency_key_recomputed_not_trusted_from_caller() -> None:
    run_identity = {"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST}
    envelope = _build_envelope(request_id="req-ac5-c", run_identity=run_identity)
    recomputed = pr.compute_idempotency_key(
        repository_id=_REPO_ID,
        base_sha=run_identity["base_sha"],
        source_set_digest=run_identity["source_set_digest"],
        scope=pr.DEFAULT_SCOPE,
    )
    assert envelope["idempotency_key"] == recomputed


# ---------------------------------------------------------------------------
# AC6: optimistic concurrency guard -- best-effort, distinct from AC5
# ---------------------------------------------------------------------------


def test_optimistic_concurrency_best_effort_conflict_detection() -> None:
    transport = FakeIssueCommentTransport()
    candidate = _new_candidate()
    run_identity = {"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST}

    head0 = pr.check_optimistic_concurrency_precondition(
        transport,
        _REPO,
        _TARGET_ISSUE,
        repository_id=_REPO_ID,
        scope=pr.DEFAULT_SCOPE,
        expected_previous_digest=None,
        trusted_publisher_logins=_TRUSTED,
    )
    assert head0 is None

    envelope0 = _build_envelope(
        request_id="req-ac6-0", candidate_records=[candidate], run_identity=run_identity, parent_record_digest=head0
    )
    transport.create_comment(repo=_REPO, issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope0))

    with pytest.raises(pr.StaleWriteDetected) as excinfo:
        pr.check_optimistic_concurrency_precondition(
            transport,
            _REPO,
            _TARGET_ISSUE,
            repository_id=_REPO_ID,
            scope=pr.DEFAULT_SCOPE,
            expected_previous_digest="sha256:" + "0" * 64,
            trusted_publisher_logins=_TRUSTED,
        )
    assert excinfo.value.reason_code == "stale_expected_previous_digest"

    head1 = pr.check_optimistic_concurrency_precondition(
        transport,
        _REPO,
        _TARGET_ISSUE,
        repository_id=_REPO_ID,
        scope=pr.DEFAULT_SCOPE,
        expected_previous_digest=envelope0["publication_digest"],
        trusted_publisher_logins=_TRUSTED,
    )
    assert head1 == envelope0["publication_digest"]

    envelope_a = _build_envelope(
        request_id="req-ac6-a",
        candidate_records=[candidate],
        run_identity=run_identity,
        expected_previous_digest=head1,
        parent_record_digest=head1,
        generated_at="2026-08-22T01:00:00Z",
    )
    envelope_b = _build_envelope(
        request_id="req-ac6-b",
        candidate_records=[candidate],
        run_identity=run_identity,
        expected_previous_digest=head1,
        parent_record_digest=head1,
        generated_at="2026-08-22T02:00:00Z",
    )
    comment_a = transport.create_comment(
        repo=_REPO, issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope_a)
    )
    transport.create_comment(repo=_REPO, issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope_b))

    conflict_detected = pr.detect_post_write_sibling_conflict(
        transport,
        _REPO,
        _TARGET_ISSUE,
        parent_record_digest=head1,
        own_comment_id=comment_a["id"],
        trusted_publisher_logins=_TRUSTED,
    )
    assert conflict_detected is True

    no_conflict = pr.detect_post_write_sibling_conflict(
        transport,
        _REPO,
        _TARGET_ISSUE,
        parent_record_digest=envelope_a["publication_digest"],
        own_comment_id=comment_a["id"],
        trusted_publisher_logins=_TRUSTED,
    )
    assert no_conflict is False


# ---------------------------------------------------------------------------
# AC7: post-write readback -- canonical digest, never raw Markdown bytes
# ---------------------------------------------------------------------------


def test_canonical_get_readback_digest_match() -> None:
    transport = FakeIssueCommentTransport()
    candidate = _new_candidate()
    envelope = _build_envelope(request_id="req-ac7", candidate_records=[candidate])
    comment = transport.create_comment(repo=_REPO, issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope))

    pr.verify_readback_digest(
        transport,
        _REPO,
        comment_id=comment["id"],
        expected_publication_digest=envelope["publication_digest"],
        trusted_publisher_logins=_TRUSTED,
    )

    marker = (
        f"<!-- agent_retrospective_run:v1 repository_id={envelope['repository_id']} "
        f"idempotency_key={envelope['idempotency_key']} -->"
    )
    reformatted_fenced = "```json\n" + json.dumps(envelope, indent=4, sort_keys=True) + "\n```"
    reformatted_body = f"{marker}\n\n{reformatted_fenced}\n"
    assert reformatted_body != pr.render_comment_body(envelope)
    comment2 = transport.create_comment(repo=_REPO, issue_number=_TARGET_ISSUE, body=reformatted_body)
    pr.verify_readback_digest(
        transport,
        _REPO,
        comment_id=comment2["id"],
        expected_publication_digest=envelope["publication_digest"],
        trusted_publisher_logins=_TRUSTED,
    )

    tampered_body = transport._comments[comment["id"]]["body"].replace(
        candidate["candidate_id"], "tampered-candidate-id"
    )
    transport._comments[comment["id"]]["body"] = tampered_body
    with pytest.raises(pr.ReadbackVerificationFailed) as excinfo:
        pr.verify_readback_digest(
            transport,
            _REPO,
            comment_id=comment["id"],
            expected_publication_digest=envelope["publication_digest"],
            trusted_publisher_logins=_TRUSTED,
        )
    assert excinfo.value.reason_code == "digest_mismatch"


# ---------------------------------------------------------------------------
# AC8: candidate/finding/delta full round-trip
# ---------------------------------------------------------------------------


def test_candidate_finding_delta_full_roundtrip() -> None:
    candidate = _validate_mod.load_fixture(
        "agent_improvement_candidate_v1.finding_contract.recurrent_regressed.valid.json"
    )
    delta_results = [
        {
            "finding_identity": candidate["finding_contract"]["identity"]["value"],
            "evaluation_status": "classified",
            "delta_status": "recurrent",
        }
    ]
    envelope = _build_envelope(request_id="req-ac8", candidate_records=[candidate], delta_results=delta_results)

    parsed = pr.extract_envelope_from_body(pr.render_comment_body(envelope))

    assert parsed is not None
    assert parsed["candidate_records"] == [candidate]
    assert parsed["delta_results"] == delta_results
    assert parsed["candidate_records"][0]["finding_contract"]["identity"] == candidate["finding_contract"]["identity"]
    assert (
        parsed["candidate_records"][0]["finding_contract"]["claim_class"]
        == candidate["finding_contract"]["claim_class"]
    )
    assert (
        parsed["candidate_records"][0]["finding_contract"]["evaluations"]
        == candidate["finding_contract"]["evaluations"]
    )

    _validate_mod.validate_candidate(parsed["candidate_records"][0])

    identities = [entry["finding_identity"] for entry in parsed["delta_results"]]
    assert len(identities) == len(set(identities))
    assert len(identities) == len(parsed["candidate_records"])


def test_candidate_finding_delta_full_roundtrip_legacy_candidate_has_no_identity() -> None:
    legacy_candidate = _validate_mod.load_fixture("agent_improvement_candidate_v1.valid.json")
    assert "finding_contract" not in legacy_candidate
    envelope = _build_envelope(request_id="req-ac8-legacy", candidate_records=[legacy_candidate])
    parsed = pr.extract_envelope_from_body(pr.render_comment_body(envelope))
    assert parsed["candidate_records"] == [legacy_candidate]


# ---------------------------------------------------------------------------
# AC9: previous-state provider status classification from real data shape
# ---------------------------------------------------------------------------


def test_previous_state_status_classification_no_history() -> None:
    transport = FakeIssueCommentTransport()
    provider = pr.IssueCommentPreviousStateProvider(
        repo=_REPO, target_issue=_TARGET_ISSUE, transport=transport, trusted_publisher_logins=_TRUSTED
    )

    result = provider.get(
        repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-sorted-json-v1"
    )

    assert result.status == "no_history"
    assert result.read_version is None
    assert result.candidates == []


def test_previous_state_status_classification_legacy_unavailable() -> None:
    transport = FakeIssueCommentTransport()
    transport.seed_comment(
        issue_number=_TARGET_ISSUE,
        body="agent_retrospective_run notes from before Issue #2238 (free text, no structured envelope)",
    )
    provider = pr.IssueCommentPreviousStateProvider(
        repo=_REPO, target_issue=_TARGET_ISSUE, transport=transport, trusted_publisher_logins=_TRUSTED
    )

    result = provider.get(
        repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-sorted-json-v1"
    )

    assert result.status == "legacy_unavailable"
    assert result.candidates == []


def test_previous_state_status_classification_available() -> None:
    transport = FakeIssueCommentTransport()
    now = dt.datetime(2026, 8, 22, 12, 0, 0, tzinfo=dt.timezone.utc)
    envelope = _build_envelope(
        request_id="req-ac9-avail", candidate_records=[_new_candidate()], generated_at=_iso(now - dt.timedelta(hours=1))
    )
    transport.seed_comment(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope))
    provider = pr.IssueCommentPreviousStateProvider(
        repo=_REPO,
        target_issue=_TARGET_ISSUE,
        transport=transport,
        trusted_publisher_logins=_TRUSTED,
        clock=lambda: now,
    )

    result = provider.get(
        repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-sorted-json-v1"
    )

    assert result.status == "available"
    assert result.read_version == envelope["publication_digest"]
    assert result.candidates == [_new_candidate()]


def test_previous_state_status_classification_partial() -> None:
    transport = FakeIssueCommentTransport()
    now = dt.datetime(2026, 8, 22, 12, 0, 0, tzinfo=dt.timezone.utc)
    envelope = _build_envelope(
        request_id="req-ac9-partial",
        candidate_records=[_new_candidate()],
        generated_at=_iso(now - dt.timedelta(minutes=1)),
        source_observations=[
            {
                "source_type": "repository",
                "source_id": "repository",
                "source_status": "partial",
                "pagination_completeness": "partial",
            }
        ],
    )
    transport.seed_comment(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope))
    provider = pr.IssueCommentPreviousStateProvider(
        repo=_REPO,
        target_issue=_TARGET_ISSUE,
        transport=transport,
        trusted_publisher_logins=_TRUSTED,
        clock=lambda: now,
    )

    result = provider.get(
        repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-sorted-json-v1"
    )

    assert result.status == "partial"


def test_previous_state_status_classification_stale_age_based_opt_in() -> None:
    # Issue #2238 P1-4 fix_delta: age-based staleness is now opt-in --
    # explicitly pass stale_after_seconds to get the legacy 7-day behavior.
    transport = FakeIssueCommentTransport()
    now = dt.datetime(2026, 8, 22, 12, 0, 0, tzinfo=dt.timezone.utc)
    envelope = _build_envelope(
        request_id="req-ac9-stale", candidate_records=[_new_candidate()], generated_at=_iso(now - dt.timedelta(days=30))
    )
    transport.seed_comment(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope))
    provider = pr.IssueCommentPreviousStateProvider(
        repo=_REPO,
        target_issue=_TARGET_ISSUE,
        transport=transport,
        trusted_publisher_logins=_TRUSTED,
        clock=lambda: now,
        stale_after_seconds=pr.STALE_AFTER_SECONDS_LEGACY_DEFAULT,
    )

    result = provider.get(
        repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-sorted-json-v1"
    )

    assert result.status == "stale"


def test_previous_state_status_classification_default_disables_age_based_staleness() -> None:
    # Issue #2238 P1-4 fix_delta: the new DEFAULT (no stale_after_seconds
    # passed) never classifies purely on age -- a 30-day-old, non-forked,
    # non-partial record is still "available".
    transport = FakeIssueCommentTransport()
    now = dt.datetime(2026, 8, 22, 12, 0, 0, tzinfo=dt.timezone.utc)
    envelope = _build_envelope(
        request_id="req-ac9-not-stale",
        candidate_records=[_new_candidate()],
        generated_at=_iso(now - dt.timedelta(days=30)),
    )
    transport.seed_comment(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope))
    provider = pr.IssueCommentPreviousStateProvider(
        repo=_REPO,
        target_issue=_TARGET_ISSUE,
        transport=transport,
        trusted_publisher_logins=_TRUSTED,
        clock=lambda: now,
    )

    result = provider.get(
        repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-sorted-json-v1"
    )

    assert result.status == "available"


# ---------------------------------------------------------------------------
# AC10: ambiguous POST failure recovery by request_id
# ---------------------------------------------------------------------------


def test_ambiguous_post_failure_recovery_by_request_id_recovers_when_already_created() -> None:
    transport = FakeIssueCommentTransport()
    envelope = _build_envelope(request_id="req-ac10-recover", candidate_records=[_new_candidate()])
    body = pr.render_comment_body(envelope)
    transport.queue_create_side_effect(pr.AmbiguousTransportError("simulated timeout"), also_create=True)

    comment, recovered = pr.create_comment_with_recovery(
        transport,
        _REPO,
        _TARGET_ISSUE,
        body=body,
        request_id="req-ac10-recover",
        idempotency_key=envelope["idempotency_key"],
        publication_digest=envelope["publication_digest"],
        trusted_publisher_logins=_TRUSTED,
    )

    assert recovered is True
    assert comment["id"] is not None
    assert transport.create_call_count == 1


def test_ambiguous_post_failure_recovery_by_request_id_conflict_on_digest_mismatch() -> None:
    transport = FakeIssueCommentTransport()
    envelope = _build_envelope(request_id="req-ac10-conflict", candidate_records=[_new_candidate()])
    other_envelope = dict(envelope)
    other_envelope["candidate_records"] = []
    other_envelope["publication_digest"] = pr.compute_publication_digest(
        {k: v for k, v in other_envelope.items() if k != "publication_digest"}
    )
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
            trusted_publisher_logins=_TRUSTED,
        )

    assert excinfo.value.reason_code == "ambiguous_post_recovered_conflict"


def test_ambiguous_post_failure_recovery_by_request_id_bounded_retry_when_not_found() -> None:
    transport = FakeIssueCommentTransport()
    envelope = _build_envelope(request_id="req-ac10-retry", candidate_records=[_new_candidate()])
    transport.queue_create_side_effect(pr.AmbiguousTransportError("simulated timeout"), also_create=False)

    comment, recovered = pr.create_comment_with_recovery(
        transport,
        _REPO,
        _TARGET_ISSUE,
        body=pr.render_comment_body(envelope),
        request_id="req-ac10-retry",
        idempotency_key=envelope["idempotency_key"],
        publication_digest=envelope["publication_digest"],
        trusted_publisher_logins=_TRUSTED,
    )

    assert recovered is False
    assert comment["id"] is not None
    assert transport.create_call_count == 2


# ---------------------------------------------------------------------------
# AC11: index-update failure -> published_index_stale, no rollback
# ---------------------------------------------------------------------------


def test_index_update_failure_does_not_rollback_primary_record(tmp_path: Path) -> None:
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-ac11")
    preview = _build_envelope(request_id=pub_req["request_id"], candidate_records=pub_req["candidate_records"])
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
    ctx = pr.AuthorizationContext(
        receipt_path=receipt_path, clock=lambda: dt.datetime(2026, 8, 22, 0, 2, tzinfo=dt.timezone.utc)
    )

    def _failing_index_updater(**kwargs: Any) -> None:
        raise RuntimeError("index update boom")

    result = pr.publish_run(
        publish_request=pub_req,
        repo=_REPO,
        transport=transport,
        auth_ctx=ctx,
        trusted_publisher_logins=_TRUSTED,
        index_updater=_failing_index_updater,
    )

    assert result.status == "published_index_stale"
    assert result.comment_id is not None
    assert any("index update boom" in error for error in result.errors)

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
        trusted_publisher_logins=_TRUSTED,
        index_updater=lambda **kwargs: calls.append("updated"),
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
    pub_req = _publish_request_dict(
        candidate_records=[candidate], delta_results=tainted_delta, request_id="req-ac12-token"
    )
    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: True, is_tty=lambda: True)

    with pytest.raises(pr.PublicSafetyViolation) as excinfo:
        pr.publish_run(
            publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, trusted_publisher_logins=_TRUSTED
        )

    assert excinfo.value.reason_code == "token_pattern_detected"
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
    pub_req = _publish_request_dict(
        candidate_records=[candidate], delta_results=tainted_delta, request_id="req-ac12-path"
    )
    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: True, is_tty=lambda: True)

    with pytest.raises(pr.PublicSafetyViolation) as excinfo:
        pr.publish_run(
            publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, trusted_publisher_logins=_TRUSTED
        )

    assert excinfo.value.reason_code == "absolute_path_detected"
    assert transport.create_call_count == 0


def test_public_safety_validator_runs_before_post_rejects_smuggled_authority_field() -> None:
    envelope = _build_envelope(
        request_id="req-ac12-smuggle", delta_results=[{"finding_identity": "x", "raw_stdout": "leaked raw output"}]
    )

    with pytest.raises(pr.PublicSafetyViolation) as excinfo:
        pr.run_public_safety_validator(envelope)

    assert excinfo.value.reason_code == "smuggled_authority_field"


def test_public_safety_validator_runs_before_post_rejects_disallowed_top_level_field() -> None:
    envelope = _build_envelope(request_id="req-ac12-field")
    envelope["mutation_capability"] = True

    with pytest.raises(pr.PublicSafetyViolation) as excinfo:
        pr.run_public_safety_validator(envelope)

    assert excinfo.value.reason_code in ("field_not_allowlisted", "smuggled_authority_field")


def test_public_safety_validator_runs_before_post_allows_clean_envelope() -> None:
    envelope = _build_envelope(request_id="req-ac12-clean", candidate_records=[_new_candidate()])
    pr.run_public_safety_validator(envelope)


# ---------------------------------------------------------------------------
# Issue #2238 fix_delta: 10 required regression tests
# ---------------------------------------------------------------------------


def test_regression_1_same_logical_request_twice_single_post_second_is_no_op() -> None:
    """P0-3: calling publish_run() twice with the identical logical request,
    advancing a fake clock between calls, results in exactly 1 POST total;
    the second call returns no_op."""
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-r1")
    clock_box = {"now": dt.datetime(2026, 8, 22, 0, 1, tzinfo=dt.timezone.utc)}
    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: True, is_tty=lambda: True, clock=lambda: clock_box["now"])

    result1 = pr.publish_run(
        publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, trusted_publisher_logins=_TRUSTED
    )
    assert result1.status == "published"
    assert transport.create_call_count == 1

    clock_box["now"] = dt.datetime(2026, 8, 22, 5, 0, tzinfo=dt.timezone.utc)
    result2 = pr.publish_run(
        publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, trusted_publisher_logins=_TRUSTED
    )

    assert result2.status == "no_op"
    assert transport.create_call_count == 1


def test_regression_2_expected_previous_digest_none_is_not_wildcard() -> None:
    """P0-3: expected_previous_digest=None with an existing head present and
    a NEW idempotency key is rejected (None is a strict value to match, not
    a wildcard)."""
    transport = FakeIssueCommentTransport()
    existing_envelope = _build_envelope(request_id="req-r2-existing", candidate_records=[_new_candidate()])
    transport.seed_comment(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(existing_envelope))

    pub_req = _publish_request_dict(
        candidate_records=[_new_candidate()],
        request_id="req-r2-new",
        base_sha="b" * 40,  # different base_sha -> genuinely new idempotency key
        expected_previous_digest=None,
    )
    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: True, is_tty=lambda: True)

    with pytest.raises(pr.StaleWriteDetected) as excinfo:
        pr.publish_run(
            publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, trusted_publisher_logins=_TRUSTED
        )

    assert excinfo.value.reason_code == "stale_expected_previous_digest"
    assert transport.create_call_count == 0


def test_regression_3_provider_fork_detection_via_shared_parent_digest() -> None:
    """P0-4: two comments sharing the same parent_record_digest -> provider
    get() reads status == 'stale' (read-time reconstruction, not a
    separately persisted conflict flag)."""
    transport = FakeIssueCommentTransport()
    run_identity = {"run_id": "r1", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST}
    envelope_a = _build_envelope(
        request_id="req-r3-a",
        candidate_records=[_new_candidate()],
        run_identity=run_identity,
        parent_record_digest=None,
        generated_at="2026-08-22T01:00:00Z",
    )
    envelope_b = _build_envelope(
        request_id="req-r3-b",
        candidate_records=[_new_candidate()],
        run_identity=run_identity,
        parent_record_digest=None,
        generated_at="2026-08-22T02:00:00Z",
    )
    transport.create_comment(repo=_REPO, issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope_a))
    transport.create_comment(repo=_REPO, issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope_b))

    provider = pr.IssueCommentPreviousStateProvider(
        repo=_REPO, target_issue=_TARGET_ISSUE, transport=transport, trusted_publisher_logins=_TRUSTED
    )
    result = provider.get(
        repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-sorted-json-v1"
    )

    assert result.status == "stale"


def test_regression_4_tampered_digest_rejected_by_provider_and_idempotency() -> None:
    """P0-6: a marked comment with a tampered digest is rejected by BOTH the
    provider AND the idempotency/OCC path -- never treated as valid prior
    state."""
    transport = FakeIssueCommentTransport()
    envelope = _build_envelope(request_id="req-r4", candidate_records=[_new_candidate()])
    comment = transport.create_comment(repo=_REPO, issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope))
    tampered_body = transport._comments[comment["id"]]["body"].replace("classified", "TAMPERED")
    transport._comments[comment["id"]]["body"] = tampered_body

    provider = pr.IssueCommentPreviousStateProvider(
        repo=_REPO, target_issue=_TARGET_ISSUE, transport=transport, trusted_publisher_logins=_TRUSTED
    )
    result = provider.get(
        repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-sorted-json-v1"
    )
    assert result.status == "no_history"

    head = pr.check_optimistic_concurrency_precondition(
        transport,
        _REPO,
        _TARGET_ISSUE,
        repository_id=_REPO_ID,
        scope=pr.DEFAULT_SCOPE,
        expected_previous_digest=None,
        trusted_publisher_logins=_TRUSTED,
    )
    assert head is None

    decision, existing = pr.evaluate_idempotency(
        transport,
        _REPO,
        _TARGET_ISSUE,
        idempotency_key=envelope["idempotency_key"],
        request_payload_digest="sha256:" + "1" * 64,
        trusted_publisher_logins=_TRUSTED,
    )
    assert decision == "publish"
    assert existing is None


def test_regression_5_non_allowlisted_author_not_accepted_as_valid_state() -> None:
    """P0-6: a full, well-formed record posted by a non-allowlisted login is
    never accepted as valid durable state (author allowlist check)."""
    transport = FakeIssueCommentTransport()
    envelope = _build_envelope(request_id="req-r5", candidate_records=[_new_candidate()])
    transport.seed_comment(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(envelope), login=_UNTRUSTED_LOGIN)

    provider = pr.IssueCommentPreviousStateProvider(
        repo=_REPO, target_issue=_TARGET_ISSUE, transport=transport, trusted_publisher_logins=_TRUSTED
    )
    result = provider.get(
        repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-sorted-json-v1"
    )
    assert result.status == "no_history"

    decision, existing = pr.evaluate_idempotency(
        transport,
        _REPO,
        _TARGET_ISSUE,
        idempotency_key=envelope["idempotency_key"],
        request_payload_digest="sha256:" + "2" * 64,
        trusted_publisher_logins=_TRUSTED,
    )
    assert decision == "publish"
    assert existing is None


def test_regression_6_repository_mismatch_zero_transport_calls() -> None:
    """P0-1: publish_request.repository_id != --repo -> zero transport
    calls occur."""
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(
        candidate_records=[_new_candidate()], request_id="req-r6", repository_id="someone-else/other-repo"
    )
    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: True, is_tty=lambda: True)

    with pytest.raises(pr.RepositoryMismatch):
        pr.publish_run(
            publish_request=pub_req, repo=_REPO, transport=transport, auth_ctx=ctx, trusted_publisher_logins=_TRUSTED
        )

    assert transport.create_call_count == 0
    assert transport.list_comments(repo=_REPO, issue_number=_TARGET_ISSUE) == []


def test_regression_7_head_change_between_prepare_and_publish_refuses_post() -> None:
    """P0-2: the live head changes between prepare_publication() and
    publish_prepared() -> POST does not happen; caller must re-run
    prepare_publication()."""
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-r7")

    prepared = pr.prepare_publication(
        publish_request=pub_req, repo=_REPO, transport=transport, trusted_publisher_logins=_TRUSTED
    )
    assert prepared.status == "publish"

    # simulate a concurrent publish landing a new head in between
    intervening_envelope = _build_envelope(
        request_id="req-r7-intervening",
        candidate_records=[_new_candidate()],
        run_identity={"run_id": "other", "base_sha": "c" * 40, "source_set_digest": "e" * 64},
    )
    transport.create_comment(repo=_REPO, issue_number=_TARGET_ISSUE, body=pr.render_comment_body(intervening_envelope))

    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: True, is_tty=lambda: True)
    with pytest.raises(pr.StaleWriteDetected):
        pr.publish_prepared(prepared, repo=_REPO, transport=transport, auth_ctx=ctx, trusted_publisher_logins=_TRUSTED)

    # only the intervening comment exists -- no POST from publish_prepared()
    assert transport.create_call_count == 1


def test_regression_8_timeout_after_landed_post_recovers_no_duplicate() -> None:
    """P1-2/AC10: a POST lands server-side but the response times out --
    rescan-by-request_id recovers the already-landed comment; no duplicate
    POST happens."""
    transport = FakeIssueCommentTransport()
    envelope = _build_envelope(request_id="req-r8", candidate_records=[_new_candidate()])
    transport.queue_create_side_effect(pr.AmbiguousTransportError("simulated timeout"), also_create=True)

    comment, recovered = pr.create_comment_with_recovery(
        transport,
        _REPO,
        _TARGET_ISSUE,
        body=pr.render_comment_body(envelope),
        request_id="req-r8",
        idempotency_key=envelope["idempotency_key"],
        publication_digest=envelope["publication_digest"],
        trusted_publisher_logins=_TRUSTED,
    )

    assert recovered is True
    assert transport.create_call_count == 1
    assert len(transport.list_comments(repo=_REPO, issue_number=_TARGET_ISSUE)) == 1


def test_regression_9_readback_verification_failure_is_published_unverified_no_index_update() -> None:
    """P0-6/P0-7: a readback that fails schema/digest verification ->
    result.status == 'published_unverified'; index_updater is NOT invoked."""

    class _TamperingTransport(FakeIssueCommentTransport):
        def get_comment(self, *, repo: str, comment_id: int) -> dict[str, Any]:
            fetched = super().get_comment(repo=repo, comment_id=comment_id)
            fetched["body"] = fetched["body"].replace("classified", "TAMPERED-ON-READBACK")
            return fetched

    transport = _TamperingTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-r9")
    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: True, is_tty=lambda: True)
    calls: list[str] = []

    result = pr.publish_run(
        publish_request=pub_req,
        repo=_REPO,
        transport=transport,
        auth_ctx=ctx,
        trusted_publisher_logins=_TRUSTED,
        index_updater=lambda **kwargs: calls.append("updated"),
    )

    assert result.status == "published_unverified"
    assert calls == []


def test_regression_10_index_updater_failure_is_published_index_stale() -> None:
    """P0-7: a CLI-level index updater failure after a verified publish ->
    result.status == 'published_index_stale' (not an overall publish
    failure)."""
    transport = FakeIssueCommentTransport()
    pub_req = _publish_request_dict(candidate_records=[_new_candidate()], request_id="req-r10")
    ctx = pr.AuthorizationContext(tty_confirm=lambda _prompt: True, is_tty=lambda: True)

    def _failing_index_updater(**kwargs: Any) -> None:
        raise RuntimeError("update-retro-index.mjs exited 1")

    result = pr.publish_run(
        publish_request=pub_req,
        repo=_REPO,
        transport=transport,
        auth_ctx=ctx,
        trusted_publisher_logins=_TRUSTED,
        index_updater=_failing_index_updater,
    )

    assert result.status == "published_index_stale"
    assert any("update-retro-index.mjs exited 1" in error for error in result.errors)
