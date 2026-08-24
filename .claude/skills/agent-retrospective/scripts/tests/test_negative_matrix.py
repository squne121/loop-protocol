#!/usr/bin/env python3
"""Negative matrix + production call-graph integration tests for
agent-retrospective (Issue #2239, Child 6 of #2192).

Covers every Issue #2239 AC that is a pytest -k target:
  AC5  negative matrix (8 scenarios, whole-file run -- no single -k filter)
  AC10 evaluator_ordering_or_unauthorized_publication
  AC11 candidate_delta_states_with_indeterminate

Fixture/fake-transport only (Runtime Verification Applicability:
not_applicable for every AC in this file -- AC5/AC10/AC11 are hermetic
schema/fixture-based negative matrices, distinct from AC6/AC7's live smoke in
test_security_boundary.py). No live GitHub/Agent call is ever made.
"""

from __future__ import annotations

import copy
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
_TARGET_ISSUE = 2239
_TRUSTED_LOGIN = "agent-retrospective-bot"
_TRUSTED = frozenset({_TRUSTED_LOGIN})


# ---------------------------------------------------------------------------
# shared fakes / helpers
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Hermetic, in-memory ``IssueCommentTransportProtocol`` spy supporting
    queued ``AmbiguousTransportError`` side effects (``interrupted``
    scenario)."""

    def __init__(self) -> None:
        self._comments: dict[int, dict[str, Any]] = {}
        self._next_id = 9000
        self.create_call_count = 0
        self._queued: list[dict[str, Any]] = []

    def queue_create_side_effect(self, exc: Exception, *, also_create: bool = False) -> None:
        self._queued.append({"exc": exc, "also_create": also_create})

    def _insert(self, *, issue_number: int, body: str, login: str = _TRUSTED_LOGIN) -> dict[str, Any]:
        cid = self._next_id
        self._next_id += 1
        comment = {
            "id": cid,
            "html_url": f"https://github.com/x/y/issues/{issue_number}#issuecomment-{cid}",
            "body": body,
            "user": {"login": login},
            "_issue_number": issue_number,
        }
        self._comments[cid] = comment
        return dict(comment)

    def seed(self, *, issue_number: int, body: str, login: str = _TRUSTED_LOGIN) -> dict[str, Any]:
        return self._insert(issue_number=issue_number, body=body, login=login)

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


def _new_candidate() -> dict[str, Any]:
    return _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.new.valid.json")


def _publish_request_dict(
    *,
    candidate_records: list[dict[str, Any]] | None = None,
    request_id: str = "req-nm-1",
    run_id: str = "run-nm-1",
    base_sha: str = _FULL_SHA,
    expected_previous_digest: str | None = None,
    source_observations: list[dict[str, Any]] | None = None,
    generated_at: str = "2026-08-24T00:00:00Z",
) -> dict[str, Any]:
    return {
        "run_identity": {"run_id": run_id, "base_sha": base_sha, "source_set_digest": _DIGEST},
        "repository_id": _REPO_ID,
        "target_issue": _TARGET_ISSUE,
        "request_id": request_id,
        "candidate_records": candidate_records or [],
        "delta_results": [],
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
        "generated_at": generated_at,
    }


def _seed_published_run(
    transport: _FakeTransport, *, request_id: str, run_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id=request_id,
        run_identity={"run_id": run_id, "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-20T00:00:00Z",
        source_observations=[
            {
                "source_type": "repository",
                "source_id": "repository",
                "source_status": "complete",
                "pagination_completeness": "complete",
            }
        ],
    )
    body = pr.render_comment_body(envelope)
    comment = transport.seed(issue_number=_TARGET_ISSUE, body=body)
    return comment, envelope


# ---------------------------------------------------------------------------
# AC5: negative matrix (8 scenarios)
# ---------------------------------------------------------------------------


def test_negative_matrix_duplicate_replay_is_no_op_zero_post_zero_index() -> None:
    transport = _FakeTransport()
    request = _publish_request_dict(request_id="req-dup", run_id="run-dup")
    first = pr.prepare_publication(
        publish_request=request, repo=_REPO, transport=transport, trusted_publisher_logins=_TRUSTED
    )
    assert first.status == "publish"
    pr.publish_prepared(
        first,
        repo=_REPO,
        transport=transport,
        auth_ctx=pr.AuthorizationContext(receipt_path=None, tty_confirm=lambda _: True, is_tty=lambda: True),
        trusted_publisher_logins=_TRUSTED,
    )
    assert transport.create_call_count == 1

    replay = pr.prepare_publication(
        publish_request=request, repo=_REPO, transport=transport, trusted_publisher_logins=_TRUSTED
    )
    assert replay.status == "no_op"
    assert replay.envelope is None
    index_calls: list[dict[str, Any]] = []
    replay_result = pr.publish_run(
        publish_request=request,
        repo=_REPO,
        transport=transport,
        auth_ctx=pr.AuthorizationContext(receipt_path=None, tty_confirm=lambda _: True, is_tty=lambda: True),
        trusted_publisher_logins=_TRUSTED,
        index_updater=lambda **kwargs: index_calls.append(kwargs),
    )
    assert replay_result.status == "no_op"
    assert transport.create_call_count == 1
    assert index_calls == []


def test_negative_matrix_no_material_delta_run_is_still_published_as_a_new_run() -> None:
    unchanged_fixture = _validate_mod.load_fixture(
        "agent_improvement_candidate_v1.finding_contract.unchanged.valid.json"
    )
    previous = rr.PreviousStateResult(
        status="available", previous_run_ref="run-0", candidates=[unchanged_fixture], read_version="v1"
    )
    delta = rr.compute_delta(previous, [copy.deepcopy(unchanged_fixture)])
    changed = [d for d in delta if d["delta_status"] != "unchanged"]
    assert changed == []
    assert all(d["evaluation_status"] == "classified" for d in delta)

    transport = _FakeTransport()
    request = _publish_request_dict(candidate_records=[unchanged_fixture], request_id="req-nmd", run_id="run-nmd-2")
    prepared = pr.prepare_publication(
        publish_request=request, repo=_REPO, transport=transport, trusted_publisher_logins=_TRUSTED
    )
    assert prepared.status == "publish"


def test_negative_matrix_idempotency_conflict_zero_post() -> None:
    transport = _FakeTransport()
    _seed_published_run(transport, request_id="req-conflict-seed", run_id="run-conflict-seed")

    request = _publish_request_dict(
        request_id="req-conflict-new", run_id="run-conflict-seed", candidate_records=[_new_candidate()]
    )
    prepared = pr.prepare_publication(
        publish_request=request, repo=_REPO, transport=transport, trusted_publisher_logins=_TRUSTED
    )
    assert prepared.status == "conflict"
    assert prepared.envelope is None
    assert transport.create_call_count == 0


def test_negative_matrix_interrupted_ambiguous_post_recovers_with_bounded_rescan() -> None:
    transport = _FakeTransport()
    request = _publish_request_dict(request_id="req-interrupted", run_id="run-interrupted")
    prepared = pr.prepare_publication(
        publish_request=request, repo=_REPO, transport=transport, trusted_publisher_logins=_TRUSTED
    )
    assert prepared.status == "publish"

    transport.queue_create_side_effect(pr.AmbiguousTransportError("timeout"), also_create=True)
    result = pr.publish_prepared(
        prepared,
        repo=_REPO,
        transport=transport,
        auth_ctx=pr.AuthorizationContext(receipt_path=None, tty_confirm=lambda _: True, is_tty=lambda: True),
        trusted_publisher_logins=_TRUSTED,
    )
    assert result.status == "recovered"
    matching = [c for c in transport._comments.values() if c["_issue_number"] == _TARGET_ISSUE]
    assert len(matching) == 1


def test_negative_matrix_pagination_exhaustion_forces_indeterminate_never_resolved() -> None:
    transport = _FakeTransport()
    partial_envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-partial",
        run_identity={"run_id": "run-partial", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[_new_candidate()],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-20T00:00:00Z",
        source_observations=[
            {
                "source_type": "repository",
                "source_id": "repository",
                "source_status": "partial",
                "pagination_completeness": "partial",
            }
        ],
    )
    transport.seed(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(partial_envelope))
    provider = pr.IssueCommentPreviousStateProvider(
        repo=_REPO, target_issue=_TARGET_ISSUE, transport=transport, trusted_publisher_logins=_TRUSTED
    )
    previous = provider.get(repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-jcs-v1")
    assert previous.status == "partial"

    delta = rr.compute_delta(previous, [_new_candidate()])
    assert all(d["evaluation_status"] == "indeterminate" for d in delta)
    assert all(d["delta_status"] is None for d in delta)
    assert all(d.get("indeterminate_reason") == "source_partial" for d in delta)
    assert transport.create_call_count == 0


def test_negative_matrix_stale_head_git_history_advanced_forces_indeterminate() -> None:
    """'stale-head' (age-based staleness): the previous published run has
    aged past the opt-in freshness bound (`stale_after_seconds`) -- classified
    'stale' (the only production status IssueCommentPreviousStateProvider
    exposes for a previous read no longer trustworthy as a base for the
    current run), forcing every current candidate to 'indeterminate' -- never
    a false 'resolved'.

    Issue #2239 PR #2331 fix_delta P0-3: this docstring previously said
    "Git base SHA comparison", which was inaccurate -- confirmed against
    production `persist_retrospective_run.py`, no Git base SHA comparison
    mechanism exists there; the only stale-detection mechanism implemented
    is age-based (`generated_at` + `stale_after_seconds`), which is what
    this test actually exercises below. `old_base_sha` is retained only to
    exercise `run_identity.base_sha`'s field type/shape on the seeded
    previous envelope -- it is never compared against the current run's
    base_sha to determine staleness."""
    transport = _FakeTransport()
    old_base_sha = "c" * 40
    stale_envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-stale-head",
        run_identity={"run_id": "run-stale-head", "base_sha": old_base_sha, "source_set_digest": _DIGEST},
        candidate_records=[_new_candidate()],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-01-01T00:00:00Z",
        source_observations=[
            {
                "source_type": "repository",
                "source_id": "repository",
                "source_status": "complete",
                "pagination_completeness": "complete",
            }
        ],
    )
    transport.seed(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(stale_envelope))

    import datetime as dt

    provider = pr.IssueCommentPreviousStateProvider(
        repo=_REPO,
        target_issue=_TARGET_ISSUE,
        transport=transport,
        trusted_publisher_logins=_TRUSTED,
        clock=lambda: dt.datetime(2026, 8, 24, tzinfo=dt.timezone.utc),
        stale_after_seconds=3600,
    )
    previous = provider.get(repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-jcs-v1")
    assert previous.status == "stale"

    delta = rr.compute_delta(previous, [_new_candidate()])
    assert all(d["evaluation_status"] == "indeterminate" for d in delta)
    assert all(d.get("indeterminate_reason") == "source_stale" for d in delta)
    assert transport.create_call_count == 0


def test_negative_matrix_stale_previous_digest_blocks_before_post() -> None:
    transport = _FakeTransport()
    _seed_published_run(transport, request_id="req-spd-seed", run_id="run-spd-seed")

    request = _publish_request_dict(
        request_id="req-spd-new",
        run_id="run-spd-new",
        base_sha="f" * 40,
        expected_previous_digest="e" * 64,
        candidate_records=[_new_candidate()],
    )
    with pytest.raises(pr.StaleWriteDetected) as excinfo:
        pr.prepare_publication(
            publish_request=request, repo=_REPO, transport=transport, trusted_publisher_logins=_TRUSTED
        )
    assert excinfo.value.reason_code == "stale_expected_previous_digest"
    assert transport.create_call_count == 0


def test_negative_matrix_forked_history_forces_stale_indeterminate() -> None:
    transport = _FakeTransport()
    root_envelope = pr.build_run_envelope(
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-fork-root",
        run_identity={"run_id": "run-fork-root", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
        candidate_records=[],
        delta_results=[],
        expected_previous_digest=None,
        parent_record_digest=None,
        generated_at="2026-08-20T00:00:00Z",
        source_observations=[
            {
                "source_type": "repository",
                "source_id": "repository",
                "source_status": "complete",
                "pagination_completeness": "complete",
            }
        ],
    )
    root_digest = root_envelope["publication_digest"]
    transport.seed(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(root_envelope))

    for branch in ("a", "b"):
        child_envelope = pr.build_run_envelope(
            repository_id=_REPO_ID,
            target_issue=_TARGET_ISSUE,
            request_id=f"req-fork-{branch}",
            run_identity={"run_id": f"run-fork-{branch}", "base_sha": _FULL_SHA, "source_set_digest": _DIGEST},
            candidate_records=[],
            delta_results=[],
            expected_previous_digest=root_digest,
            parent_record_digest=root_digest,
            generated_at="2026-08-21T00:00:00Z",
            source_observations=[
                {
                    "source_type": "repository",
                    "source_id": "repository",
                    "source_status": "complete",
                    "pagination_completeness": "complete",
                }
            ],
        )
        transport.seed(issue_number=_TARGET_ISSUE, body=pr.render_comment_body(child_envelope))

    provider = pr.IssueCommentPreviousStateProvider(
        repo=_REPO, target_issue=_TARGET_ISSUE, transport=transport, trusted_publisher_logins=_TRUSTED
    )
    previous = provider.get(repository_id=_REPO_ID, scope=pr.DEFAULT_SCOPE, finding_identity_algorithm="sha256-jcs-v1")
    assert previous.status == "stale"

    delta = rr.compute_delta(previous, [_new_candidate()])
    assert all(d["evaluation_status"] == "indeterminate" for d in delta)
    assert transport.create_call_count == 0


# ---------------------------------------------------------------------------
# AC10: production call-graph integration -- evaluator ordering +
# unauthorized publication mutation denial
# ---------------------------------------------------------------------------


def _wrapper_payload(structured_output: dict[str, Any]) -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": False, "structured_output": structured_output}


def test_evaluator_ordering_or_unauthorized_publication_evaluator_waits_for_observer_wave() -> None:
    """production execute_run() call graph (not a hand-rolled reimplementation
    of the phase ordering): the evaluator is never invoked until every
    observer in EXPECTED_OBSERVER_MANIFEST has succeeded."""
    call_log: list[str] = []

    # execute_run() re-derives the SourcePlan internally via prepare(); with
    # no collectors and a fixed base_sha_resolver/run_id, prepare() is
    # deterministic, so calling it once here to learn the real
    # source_set_digest reproduces exactly what execute_run() itself computes.
    _, precomputed_plan, _ = rr.prepare(base_sha_resolver=lambda: _FULL_SHA, collectors=[], run_id="run-ac10")
    expected_digest = precomputed_plan.source_set_digest

    def _invoke(request: "rr.AgentInvocationRequest") -> "rr.AgentInvocationResult":
        call_log.append(request.agent_name)
        bundle = rr.EvidenceBundle(
            run_id="run-ac10",
            base_sha=_FULL_SHA,
            source_set_digest=expected_digest,
            observer_id=request.agent_name,
            evidence_ref=f"evidence://{request.agent_name}",
            findings=[{"claim": "x", "claim_class": "process"}],
        )
        return rr.AgentInvocationResult(
            status="ok",
            structured_output=json.loads(bundle.to_wire()),
            raw_stdout_excerpt=None,
            exit_code=0,
            reason_code=None,
        )

    def _invoke_evaluator(request: "rr.EvaluatorRequest") -> "rr.AgentInvocationResult":
        call_log.append("retrospective-evaluator")
        evaluation = rr.Evaluation(
            run_id="run-ac10",
            base_sha=_FULL_SHA,
            source_set_digest=expected_digest,
            candidate_records=[],
            evidence_ref="e",
        )
        return rr.AgentInvocationResult(
            status="ok",
            structured_output=json.loads(evaluation.to_wire()),
            raw_stdout_excerpt=None,
            exit_code=0,
            reason_code=None,
        )

    observer_requests = [
        rr.AgentInvocationRequest(agent_name=spec.observer_id, prompt="p", json_schema_path="/dev/null", cwd="/repo")
        for spec in rr.EXPECTED_OBSERVER_MANIFEST
    ]

    publish_request = rr.execute_run(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[],
        observer_requests=observer_requests,
        invoke=_invoke,
        invoke_evaluator=_invoke_evaluator,
        repository_id=_REPO_ID,
        target_issue=_TARGET_ISSUE,
        request_id="req-ac10",
        idempotency_key="idem-ac10",
        run_id="run-ac10",
    )
    assert isinstance(publish_request, rr.PublishRequest)
    assert call_log[-1] == "retrospective-evaluator"
    assert set(call_log[:-1]) == {spec.observer_id for spec in rr.EXPECTED_OBSERVER_MANIFEST}


def test_evaluator_ordering_or_unauthorized_publication_evaluator_never_invoked_on_observer_failure() -> None:
    """Issue #2239 PR #2331 fix_delta P1-4: the happy-path AC10 test above
    only exercises the ordering when every observer succeeds. This makes ONE
    observer fail (`status != "ok"`, triggering `ObserverWaveFailed` inside
    `run_observer_wave()`) and asserts `execute_run()` raises before ever
    invoking the evaluator -- the `invoke_evaluator` callback's call count
    must be exactly 0."""
    call_log: list[str] = []
    evaluator_call_count = 0

    _, precomputed_plan, _ = rr.prepare(base_sha_resolver=lambda: _FULL_SHA, collectors=[], run_id="run-ac10-fail")
    expected_digest = precomputed_plan.source_set_digest
    failing_observer_id = rr.EXPECTED_OBSERVER_MANIFEST[0].observer_id

    def _invoke(request: "rr.AgentInvocationRequest") -> "rr.AgentInvocationResult":
        call_log.append(request.agent_name)
        if request.agent_name == failing_observer_id:
            return rr.AgentInvocationResult(
                status="error", structured_output=None, raw_stdout_excerpt=None, exit_code=1, reason_code="boom"
            )
        bundle = rr.EvidenceBundle(
            run_id="run-ac10-fail",
            base_sha=_FULL_SHA,
            source_set_digest=expected_digest,
            observer_id=request.agent_name,
            evidence_ref=f"evidence://{request.agent_name}",
            findings=[{"claim": "x", "claim_class": "process"}],
        )
        return rr.AgentInvocationResult(
            status="ok",
            structured_output=json.loads(bundle.to_wire()),
            raw_stdout_excerpt=None,
            exit_code=0,
            reason_code=None,
        )

    def _invoke_evaluator(request: "rr.EvaluatorRequest") -> "rr.AgentInvocationResult":
        nonlocal evaluator_call_count
        evaluator_call_count += 1
        call_log.append("retrospective-evaluator")
        evaluation = rr.Evaluation(
            run_id="run-ac10-fail",
            base_sha=_FULL_SHA,
            source_set_digest=expected_digest,
            candidate_records=[],
            evidence_ref="e",
        )
        return rr.AgentInvocationResult(
            status="ok",
            structured_output=json.loads(evaluation.to_wire()),
            raw_stdout_excerpt=None,
            exit_code=0,
            reason_code=None,
        )

    observer_requests = [
        rr.AgentInvocationRequest(agent_name=spec.observer_id, prompt="p", json_schema_path="/dev/null", cwd="/repo")
        for spec in rr.EXPECTED_OBSERVER_MANIFEST
    ]

    with pytest.raises(rr.ObserverWaveFailed):
        rr.execute_run(
            base_sha_resolver=lambda: _FULL_SHA,
            collectors=[],
            observer_requests=observer_requests,
            invoke=_invoke,
            invoke_evaluator=_invoke_evaluator,
            repository_id=_REPO_ID,
            target_issue=_TARGET_ISSUE,
            request_id="req-ac10-fail",
            idempotency_key="idem-ac10-fail",
            run_id="run-ac10-fail",
        )
    assert evaluator_call_count == 0
    assert "retrospective-evaluator" not in call_log


def test_evaluator_ordering_or_unauthorized_publication_denied_without_authorization_zero_post() -> None:
    """production authorization gate: publication mutation never proceeds
    without either a valid receipt or an interactive tty confirmation (fake
    transport call count 0)."""
    transport = _FakeTransport()
    request = _publish_request_dict(request_id="req-ac10-unauth", run_id="run-ac10-unauth")
    with pytest.raises(pr.AuthorizationDenied):
        pr.publish_run(
            publish_request=request,
            repo=_REPO,
            transport=transport,
            auth_ctx=pr.AuthorizationContext(receipt_path=None, tty_confirm=None, is_tty=lambda: False),
            trusted_publisher_logins=_TRUSTED,
        )
    assert transport.create_call_count == 0


# ---------------------------------------------------------------------------
# AC11: candidate delta 5-state model (new/resolved/recurrent/regressed/
# unchanged) + evaluation_status indeterminate, hermetic schema/fixture-based
# -- never a live model judgement.
# ---------------------------------------------------------------------------


def test_candidate_delta_states_with_indeterminate_new_resolved_recurrent_unchanged() -> None:
    new_fixture = _new_candidate()
    delta_new = rr.compute_delta(
        rr.PreviousStateResult(status="no_history", previous_run_ref=None, candidates=[], read_version=None),
        [new_fixture],
    )
    assert delta_new[0]["delta_status"] == "new"

    unchanged_fixture = _validate_mod.load_fixture(
        "agent_improvement_candidate_v1.finding_contract.unchanged.valid.json"
    )
    delta_unchanged = rr.compute_delta(
        rr.PreviousStateResult(
            status="available", previous_run_ref="run-0", candidates=[unchanged_fixture], read_version="v1"
        ),
        [copy.deepcopy(unchanged_fixture)],
    )
    assert delta_unchanged[0]["delta_status"] == "unchanged"

    resolved_fixture = _validate_mod.load_fixture("agent_improvement_candidate_v1.finding_contract.resolved.valid.json")
    delta_recurrent = rr.compute_delta(
        rr.PreviousStateResult(
            status="available", previous_run_ref="run-0", candidates=[resolved_fixture], read_version="v1"
        ),
        [copy.deepcopy(resolved_fixture)],
    )
    assert delta_recurrent[0]["delta_status"] == "recurrent"

    delta_resolved = rr.compute_delta(
        rr.PreviousStateResult(
            status="available", previous_run_ref="run-0", candidates=[new_fixture], read_version="v1"
        ),
        [],
    )
    assert delta_resolved[0]["delta_status"] == "resolved"


def test_candidate_delta_states_with_indeterminate_incomplete_source_coverage_never_resolved() -> None:
    new_fixture = _new_candidate()
    for status in ("partial", "stale"):
        previous = rr.PreviousStateResult(
            status=status, previous_run_ref="run-0", candidates=[new_fixture], read_version="v1"
        )
        delta = rr.compute_delta(previous, [])  # absence under incomplete coverage
        assert delta == []  # never reports a false "resolved" when current_candidates is empty too

        delta_present = rr.compute_delta(previous, [copy.deepcopy(new_fixture)])
        assert delta_present[0]["evaluation_status"] == "indeterminate"
        assert delta_present[0]["delta_status"] is None
        assert delta_present[0]["indeterminate_reason"] == ("source_partial" if status == "partial" else "source_stale")


def test_candidate_delta_states_with_indeterminate_legacy_unavailable_is_new() -> None:
    new_fixture = _new_candidate()
    previous = rr.PreviousStateResult(
        status="legacy_unavailable", previous_run_ref=None, candidates=[], read_version=None
    )
    delta = rr.compute_delta(previous, [new_fixture])
    assert delta[0]["delta_status"] == "new"
    assert delta[0]["evaluation_status"] == "classified"


def test_candidate_delta_states_with_indeterminate_recurrent_and_signal_delta_regressed_coexist() -> None:
    """#2288's own 5-state finding_contract model: a candidate's OWN
    evaluations[] can record `recurrent` presence alongside
    `signal_delta: regressed` at the same time -- validated hermetically
    against the canonical schema (a real #2288/#2289 fixture, not a private
    dialect)."""
    fixture = _validate_mod.load_fixture(
        "agent_improvement_candidate_v1.finding_contract.recurrent_regressed.valid.json"
    )
    _validate_mod.validate_candidate(fixture)  # no raise: schema-valid
    evaluations = fixture["finding_contract"]["evaluations"]
    signal_deltas = {e.get("signal_delta") for e in evaluations if "signal_delta" in e}
    presence_deltas = {e.get("presence_delta") for e in evaluations}
    assert "regressed" in signal_deltas
    assert presence_deltas & {"recurrent", "still_present"}

    # the same fixture, re-observed unchanged in the current run, is
    # classified 'recurrent' by compute_delta()'s own coarse projection
    delta = rr.compute_delta(
        rr.PreviousStateResult(status="available", previous_run_ref="run-0", candidates=[fixture], read_version="v1"),
        [copy.deepcopy(fixture)],
    )
    # the same identity re-observed unchanged is classified 'unchanged' by
    # compute_delta()'s coarse projection ('recurrent' there is reserved for
    # a PREVIOUSLY-absent finding reappearing -- already exercised by
    # test_candidate_delta_states_with_indeterminate_new_resolved_recurrent_unchanged
    # above); this assertion demonstrates the two models (finding_contract's
    # own recurrent/regressed evaluation history vs. compute_delta()'s
    # coarse projection) coexist without contradiction.
    assert delta[0]["delta_status"] == "unchanged"
