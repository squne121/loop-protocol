#!/usr/bin/env python3
"""Tests for the Issue #2646 fan-out/fan-in all-terminal observer wave
barrier in ``run_retrospective.py``'s ``run_observer_wave()``.

AC1-AC5 are deterministic orchestration tests: a fixture/mock ``invoke``
closure is injected into ``run_observer_wave``/``run_cli`` exactly like
``test_run_retrospective.py`` does -- no real subprocess, no live
GitHub/Web/git/Agent call.

AC6 is a runtime verification test (Issue body:
``<!-- runtime-verification: true -->``) that spawns REAL local Python
subprocesses (never a mock/exception standing in for process
termination/reap) to prove the terminate -> bounded grace -> kill -> reap
process lifecycle contract end-to-end, reusing the exact production
lifecycle primitive (``run_retrospective._lifecycle_subprocess_run``) a real
observer dispatch goes through. ``fallback_success_is_pass`` is ``false``
(Issue body Runtime Verification Applicability) -- none of these AC6 tests
ever treat a fallback/mock path as a substitute PASS.

Covers Issue #2646's Verification Commands (each named exactly as the
Issue body's ``-k`` filters require):
  AC1  fan_out_isolated_failure
  AC2  evaluator_single_start_after_fan_in / production_call_graph_single_evaluator_start
  AC3  fan_out_non_blocking_dispatch
  AC4  observer_failure_classes_all_terminal / observer_id_swap_detected_as_terminal_failure
  AC5  aggregate_status_reachable_from_cli / aggregate_multiple_failures_mixed_allowlist_status /
       aggregate_failure_position_rotated_across_observers
  AC6  cancel_vs_terminal_collection / observer_timeout_terminates_and_reaps_subprocess /
       parent_sigterm_terminates_all_children_before_cleanup /
       sigterm_vs_observer_timeout_lifecycle_ordering
"""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402

_FULL_SHA = "a" * 40


# ---------------------------------------------------------------------------
# shared helpers (deliberately duplicated -- not imported -- from
# test_run_retrospective.py's own local helpers, to keep this file's module
# identity/collection independent, matching this repo's established pattern
# of per-test-file local fixtures around the same production module)
# ---------------------------------------------------------------------------


class _FakeCollectorResult:
    def __init__(self, observation: dict[str, Any], private_evidence: dict[str, Any] | None = None) -> None:
        self.observation = observation
        self.private_evidence: dict[str, Any] = private_evidence or {}


def _prepare(run_id: str = "run-1") -> tuple[rr.RunContext, rr.SourcePlan, list[Any]]:
    return rr.prepare(
        base_sha_resolver=lambda: _FULL_SHA,
        collectors=[
            lambda base_sha: _FakeCollectorResult(
                {
                    "source_type": "repository",
                    "source_id": "repository",
                    "source_status": "complete",
                    "pagination_completeness": "complete",
                },
                {},
            )
        ],
        run_id=run_id,
    )


def _observer_request(agent_name: str, schema_path: str = "/tmp/schema.json") -> rr.AgentInvocationRequest:
    return rr.AgentInvocationRequest(agent_name=agent_name, prompt="observe", json_schema_path=schema_path, cwd="/repo")


def _bundle_wire(*, run_id: str, base_sha: str, digest: str, observer_id: str) -> dict[str, Any]:
    bundle = rr.EvidenceBundle(
        run_id=run_id,
        base_sha=base_sha,
        source_set_digest=digest,
        observer_id=observer_id,
        evidence_ref=f"evidence://{observer_id}",
        findings=[{"claim": f"finding from {observer_id}", "claim_class": "process"}],
    )
    return json.loads(bundle.to_wire())


def _ok_result(payload: dict[str, Any]) -> rr.AgentInvocationResult:
    return rr.AgentInvocationResult(
        status="ok", structured_output=payload, raw_stdout_excerpt=None, exit_code=0, reason_code=None
    )


def _failure_result(*, status: str, reason_code: str, exit_code: int | None) -> rr.AgentInvocationResult:
    return rr.AgentInvocationResult(
        status=status, structured_output=None, raw_stdout_excerpt="boom", exit_code=exit_code, reason_code=reason_code
    )


#: Issue #2362 Scope Reframe fixture value -- matches
#: test_run_retrospective.py's own `_EMPTY_PREVIOUS_STATE` (kept local/
#: duplicated rather than imported -- see module docstring).
_EMPTY_PREVIOUS_STATE = rr.PreviousStateResult(
    status="no_history", previous_run_ref=None, candidates=[], read_version=None
)


def _run_wave(
    ctx: rr.RunContext, plan: rr.SourcePlan, invoke: Any, observer_requests: list[rr.AgentInvocationRequest]
) -> list[rr.EvidenceBundle]:
    return rr.run_observer_wave(
        ctx, plan, invoke=invoke, observer_requests=observer_requests, expected_manifest=rr.EXPECTED_OBSERVER_MANIFEST
    )


def _wrapper_payload(structured_output: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "assistant text summary",
        "structured_output": structured_output,
    }


def _run_cli_fixture(tmp_path: Path) -> tuple[Path, Path, Any]:
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "observer_result_v1.schema.json").write_text("{}", encoding="utf-8")
    (schema_dir / "evaluation_result_v1.schema.json").write_text("{}", encoding="utf-8")
    repo_root = _SCRIPTS_DIR.parents[3]

    def _git_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, returncode=0, stdout=_FULL_SHA + "\n", stderr="")

    return schema_dir, repo_root, _git_runner


def _make_full_pipeline_runner(
    *, call_log: list[str], observer_behavior: dict[str, str], expected_digest: str
) -> Any:
    """``observer_behavior`` maps ``observer_id`` -> ``"ok"`` (default) /
    ``"malformed"`` / ``"nonzero"``. Mirrors
    ``test_run_retrospective.py``'s ``test_executable_entrypoint_collectors_to_publish_request``
    fake-runner pattern."""

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        agent_name = argv[argv.index("--agent") + 1]
        call_log.append(agent_name)
        if agent_name == "retrospective-evaluator":
            evaluator_request = rr.EvaluatorRequest.from_wire(kwargs["input"])
            evaluation_payload = {
                "schema_version": rr.WIRE_SCHEMA_EVALUATION,
                "run_id": evaluator_request.run_id,
                "base_sha": _FULL_SHA,
                "source_set_digest": evaluator_request.source_set_digest,
                "candidate_records": [],
                "evidence_ref": "e",
            }
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(_wrapper_payload(evaluation_payload)), stderr=""
            )
        mode = observer_behavior.get(agent_name, "ok")
        if mode == "malformed":
            return subprocess.CompletedProcess(argv, returncode=0, stdout="not-json-at-all", stderr="")
        if mode == "nonzero":
            return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="boom")
        bundle = rr.EvidenceBundle(
            run_id=kwargs["env"].get("AGENT_RETROSPECTIVE_RUN_ID", ""),
            base_sha=kwargs["env"].get("AGENT_RETROSPECTIVE_BASE_SHA", ""),
            source_set_digest=expected_digest,
            observer_id=agent_name,
            evidence_ref=f"evidence://{agent_name}",
            findings=[{"claim": f"finding from {agent_name}", "claim_class": "process"}],
        )
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload(json.loads(bundle.to_wire()))), stderr=""
        )

    return _runner


def _real_runner_running_script(script_lines: list[str]) -> Any:
    """Ignores the ``argv`` ``invoke_agent`` itself constructs (a fake
    ``claude ...`` CLI invocation) and instead runs a REAL, fully-controlled
    local Python subprocess through the SAME production lifecycle primitive
    (``_lifecycle_subprocess_run``) a real observer dispatch goes through."""
    real_argv = [sys.executable, "-c", "\n".join(script_lines)]

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return rr._lifecycle_subprocess_run(real_argv, **kwargs)

    return _runner


# ---------------------------------------------------------------------------
# AC1: fan-out isolated failure -- peers still fully recovered, evaluator
# never starts
# ---------------------------------------------------------------------------


def test_fan_out_isolated_failure_all_terminal_zero_evaluator_starts(tmp_path: Path) -> None:
    """GIVEN one of the 3 required observers is a deterministic failure,
    WHEN run_cli() (the exact production call graph) executes, THEN the
    other 2 observers are still fully invoked (fan-out isolation -- a
    single observer's failure never cancels/skips a peer) and the
    evaluator is never started (call count 0)."""
    schema_dir, repo_root, git_runner = _run_cli_fixture(tmp_path)
    real_observation = rr.build_repository_collector(repo_root)(_FULL_SHA).observation
    expected_digest = rr.compute_source_set_digest([real_observation])
    call_log: list[str] = []
    runner = _make_full_pipeline_runner(
        call_log=call_log,
        observer_behavior={"codebase-investigator": "malformed"},
        expected_digest=expected_digest,
    )

    with pytest.raises(rr.ObserverWaveFailed):
        rr.run_cli(
            repo_root=repo_root,
            repository_id="squne121/loop-protocol",
            target_issue=2237,
            request_id="req-ac1",
            idempotency_key="idem-ac1",
            schema_dir=schema_dir,
            prompts=None,
            runner=runner,
            git_runner=git_runner,
            run_id="run-ac1",
            temp_base_dir=tmp_path / "tmpbase",
        )

    dispatched = {name for name in call_log if name != "retrospective-evaluator"}
    assert dispatched == {spec.observer_id for spec in rr.EXPECTED_OBSERVER_MANIFEST}
    assert "retrospective-evaluator" not in call_log


# ---------------------------------------------------------------------------
# AC2: evaluator starts exactly once, only after fan-in
# ---------------------------------------------------------------------------


def test_evaluator_single_start_after_fan_in() -> None:
    """Unit-level: the evaluator is invoked exactly once, and only after
    every dispatched observer has reached a terminal, successful,
    schema-valid outcome (the fan-in barrier)."""
    ctx, plan, _results = _prepare(run_id="run-ac2-unit")
    observer_requests = [_observer_request(spec.observer_id) for spec in rr.EXPECTED_OBSERVER_MANIFEST]
    call_log: list[str] = []

    def _invoke(request: rr.AgentInvocationRequest) -> rr.AgentInvocationResult:
        call_log.append(f"observer:{request.agent_name}")
        return _ok_result(
            _bundle_wire(
                run_id=ctx.run_id, base_sha=ctx.base_sha, digest=plan.source_set_digest, observer_id=request.agent_name
            )
        )

    bundles = _run_wave(ctx, plan, _invoke, observer_requests)
    finding_sets = rr.build_finding_sets(ctx, plan, bundles)
    evaluator_request = rr.prepare_evaluator_request(ctx, plan, finding_sets)

    evaluator_calls = {"n": 0}

    def _invoke_evaluator(_request: rr.EvaluatorRequest) -> rr.AgentInvocationResult:
        evaluator_calls["n"] += 1
        call_log.append("evaluator")
        evaluation = rr.Evaluation(
            run_id=ctx.run_id, base_sha=ctx.base_sha, source_set_digest=plan.source_set_digest,
            candidate_records=[], evidence_ref="e",
        )
        return _ok_result(json.loads(evaluation.to_wire()))

    rr.run_evaluation(
        ctx,
        evaluator_request,
        invoke_evaluator=_invoke_evaluator,
        repository_id="squne121/loop-protocol",
        previous_state=_EMPTY_PREVIOUS_STATE,
    )

    assert evaluator_calls["n"] == 1
    assert call_log[-1] == "evaluator"
    assert sorted(call_log[:-1]) == sorted(f"observer:{spec.observer_id}" for spec in rr.EXPECTED_OBSERVER_MANIFEST)


def test_production_call_graph_single_evaluator_start(tmp_path: Path) -> None:
    """Full production call graph (run_cli(): adapter invocation -> identity
    validation -> aggregate -> evaluator): when all 3 required observers
    succeed, the evaluator is invoked exactly once, never duplicated."""
    schema_dir, repo_root, git_runner = _run_cli_fixture(tmp_path)
    real_observation = rr.build_repository_collector(repo_root)(_FULL_SHA).observation
    expected_digest = rr.compute_source_set_digest([real_observation])
    call_log: list[str] = []
    runner = _make_full_pipeline_runner(call_log=call_log, observer_behavior={}, expected_digest=expected_digest)

    publish_request = rr.run_cli(
        repo_root=repo_root,
        repository_id="squne121/loop-protocol",
        target_issue=2237,
        request_id="req-ac2-prod",
        idempotency_key="idem-ac2-prod",
        schema_dir=schema_dir,
        prompts=None,
        runner=runner,
        git_runner=git_runner,
        run_id="run-ac2-prod",
        temp_base_dir=tmp_path / "tmpbase",
    )

    assert isinstance(publish_request, rr.PublishRequest)
    evaluator_calls = [name for name in call_log if name == "retrospective-evaluator"]
    assert len(evaluator_calls) == 1
    assert call_log[-1] == "retrospective-evaluator"
    assert sorted(name for name in call_log if name != "retrospective-evaluator") == sorted(
        spec.observer_id for spec in rr.EXPECTED_OBSERVER_MANIFEST
    )


# ---------------------------------------------------------------------------
# AC3: fan-out dispatch is genuinely non-blocking (barrier proof)
# ---------------------------------------------------------------------------


def test_fan_out_non_blocking_dispatch_barrier_proves_concurrency() -> None:
    """Dispatch of the required 3 observers must never block on any OTHER
    observer's own completion -- proven with an injected
    ``threading.Barrier(3)``: a sequential ("dispatch A, wait for A,
    dispatch B, ...") implementation would deadlock at the very first
    ``barrier.wait()`` call (only 1 of 3 parties would ever reach it) --
    bounded here by the barrier's own timeout (raises
    ``BrokenBarrierError`` instead of hanging forever), so a regression to
    sequential dispatch fails this test deterministically rather than
    hanging the test suite."""
    ctx, plan, _results = _prepare(run_id="run-ac3")
    observer_requests = [_observer_request(spec.observer_id) for spec in rr.EXPECTED_OBSERVER_MANIFEST]
    barrier = threading.Barrier(len(observer_requests), timeout=5.0)

    def _invoke(request: rr.AgentInvocationRequest) -> rr.AgentInvocationResult:
        # every dispatched observer must reach this point BEFORE any of
        # them is allowed to proceed -- only possible if all 3 are
        # genuinely running concurrently, never one-at-a-time.
        barrier.wait()
        return _ok_result(
            _bundle_wire(
                run_id=ctx.run_id, base_sha=ctx.base_sha, digest=plan.source_set_digest, observer_id=request.agent_name
            )
        )

    bundles = _run_wave(ctx, plan, _invoke, observer_requests)
    assert len(bundles) == 3


# ---------------------------------------------------------------------------
# AC4: every observer failure class reaches an all-terminal aggregate;
# observer_id swap is an independent terminal failure
# ---------------------------------------------------------------------------


def _failing_result_for(mode: str) -> rr.AgentInvocationResult | dict[str, Any]:
    if mode == "timeout":
        return rr.AgentInvocationResult(
            status="timeout", structured_output=None, raw_stdout_excerpt=None, exit_code=None, reason_code="timeout"
        )
    if mode == "malformed_output":
        return rr.AgentInvocationResult(
            status="malformed_output",
            structured_output=None,
            raw_stdout_excerpt="not-json",
            exit_code=0,
            reason_code="json_decode_failure",
        )
    if mode == "nonzero_exit":
        return _failure_result(status="api_error", reason_code="nonzero_exit", exit_code=1)
    if mode == "schema_mismatch":
        return {"totally": "wrong-shape"}
    raise AssertionError(mode)  # pragma: no cover


@pytest.mark.parametrize("mode", ["timeout", "malformed_output", "nonzero_exit", "schema_mismatch"])
def test_observer_failure_classes_all_terminal(mode: str) -> None:
    """Each observer failure class (timeout / malformed output / nonzero
    exit / schema mismatch) still reaches an all-terminal aggregate: every
    required observer is invoked (fan-out isolation), and partial output
    never reaches evaluator input (the wave raises before
    build_finding_sets/prepare_evaluator_request could ever run)."""
    ctx, plan, _results = _prepare(run_id=f"run-ac4-{mode}")
    observer_requests = [_observer_request(spec.observer_id) for spec in rr.EXPECTED_OBSERVER_MANIFEST]
    call_log: list[str] = []
    failing_id = "codebase-investigator"

    def _invoke(request: rr.AgentInvocationRequest) -> rr.AgentInvocationResult:
        call_log.append(request.agent_name)
        if request.agent_name == failing_id:
            failing = _failing_result_for(mode)
            if isinstance(failing, dict):
                return _ok_result(failing)
            return failing
        return _ok_result(
            _bundle_wire(
                run_id=ctx.run_id, base_sha=ctx.base_sha, digest=plan.source_set_digest, observer_id=request.agent_name
            )
        )

    with pytest.raises((rr.ObserverWaveFailed, rr.SchemaRepairExhausted)) as excinfo:
        _run_wave(ctx, plan, _invoke, observer_requests)

    assert sorted(call_log) == sorted(spec.observer_id for spec in rr.EXPECTED_OBSERVER_MANIFEST)
    aggregate = excinfo.value.observer_results
    assert {r.observer_id for r in aggregate} == {spec.observer_id for spec in rr.EXPECTED_OBSERVER_MANIFEST}
    failing_entry = next(r for r in aggregate if r.observer_id == failing_id)
    assert failing_entry.status == "failed"
    ok_entries = [r for r in aggregate if r.observer_id != failing_id]
    assert all(r.status == "ok" for r in ok_entries)


def test_observer_id_swap_detected_as_terminal_failure() -> None:
    """An observer_id SWAP in the RETURNED payload (e.g. the
    retrospective-runtime-observer request's response claims
    observer_id="web-researcher", and vice versa) is detected as an
    independent terminal failure, even when run_id/base_sha/
    source_set_digest all correctly agree for every observer -- never
    silently accepted just because the manifest-level observer_id SET
    still matches (the exact anchor-review repro: this identity check is
    the ONLY thing that catches it)."""
    ctx, plan, _results = _prepare(run_id="run-ac4-swap")
    observer_requests = [_observer_request(spec.observer_id) for spec in rr.EXPECTED_OBSERVER_MANIFEST]
    call_log: list[str] = []

    def _invoke(request: rr.AgentInvocationRequest) -> rr.AgentInvocationResult:
        call_log.append(request.agent_name)
        returned_id = request.agent_name
        if request.agent_name == "retrospective-runtime-observer":
            returned_id = "web-researcher"
        elif request.agent_name == "web-researcher":
            returned_id = "retrospective-runtime-observer"
        return _ok_result(
            _bundle_wire(
                run_id=ctx.run_id, base_sha=ctx.base_sha, digest=plan.source_set_digest, observer_id=returned_id
            )
        )

    with pytest.raises(rr.ObserverWaveFailed) as excinfo:
        _run_wave(ctx, plan, _invoke, observer_requests)

    assert sorted(call_log) == sorted(spec.observer_id for spec in rr.EXPECTED_OBSERVER_MANIFEST)
    aggregate = excinfo.value.observer_results
    swapped = {r.observer_id for r in aggregate if r.status == "failed"}
    assert swapped == {"retrospective-runtime-observer", "web-researcher"}
    for r in aggregate:
        if r.observer_id in swapped:
            assert r.reason_code == "observer_id_mismatch"
    codebase_entry = next(r for r in aggregate if r.observer_id == "codebase-investigator")
    assert codebase_entry.status == "ok"


# ---------------------------------------------------------------------------
# AC5: aggregate diagnostics reachable from CLI/main(); multiple mixed
# failures never collapse to a single allowlisted reason_code; failure
# position rotation
# ---------------------------------------------------------------------------


def test_aggregate_status_reachable_from_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """main()'s own typed-failure JSON output (the CLI-level surface the
    root Skill's Bash invocation actually observes) carries the full
    all-terminal ``observer_results`` aggregate as an ADDITIVE field, not
    just the single top-level reason_code."""
    aggregate = (
        rr.ObserverWaveObserverResult(
            observer_id="retrospective-runtime-observer", status="ok", reason_code=None, exit_code=None
        ),
        rr.ObserverWaveObserverResult(
            observer_id="codebase-investigator", status="failed", reason_code="nonzero_exit", exit_code=1
        ),
        rr.ObserverWaveObserverResult(observer_id="web-researcher", status="ok", reason_code=None, exit_code=None),
    )

    def _fake_run_cli(**_kwargs: Any) -> rr.PublishRequest:
        exc = rr.ObserverWaveFailed(
            "observer_failed:codebase-investigator:api_error", reason_code="nonzero_exit", exit_code=1
        )
        exc.observer_results = aggregate
        raise exc

    monkeypatch.setattr(rr, "run_cli", _fake_run_cli)
    exit_code = rr.main(
        [
            "--repository-id", "squne121/loop-protocol",
            "--target-issue", "2237",
            "--request-id", "req",
            "--idempotency-key", "idem",
            "--state-backend", "fixture",
        ]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "nonzero_exit"
    assert len(payload["observer_results"]) == 3
    ids = {entry["observer_id"] for entry in payload["observer_results"]}
    assert ids == {"retrospective-runtime-observer", "codebase-investigator", "web-researcher"}


def test_aggregate_multiple_failures_mixed_allowlist_status() -> None:
    """A MIXED multi-failure wave -- one observer failing with an existing
    live-verifier ALLOWLISTED-style reason_code (``observer_run_id_mismatch``)
    and another failing with a genuinely UNKNOWN reason_code
    (``nonzero_exit``) -- must never be represented by only the single
    allowlisted reason_code at the top level (which an existing verifier
    keyed only on the top-level reason_code could mistake for a
    known-safe single failure)."""
    ctx, plan, _results = _prepare(run_id="run-ac5-mixed")
    observer_requests = [_observer_request(spec.observer_id) for spec in rr.EXPECTED_OBSERVER_MANIFEST]

    def _invoke(request: rr.AgentInvocationRequest) -> rr.AgentInvocationResult:
        if request.agent_name == "retrospective-runtime-observer":
            # allowlisted-style failure: bundle parses fine but claims the
            # WRONG run_id.
            return _ok_result(
                _bundle_wire(
                    run_id="some-other-run", base_sha=ctx.base_sha, digest=plan.source_set_digest,
                    observer_id=request.agent_name,
                )
            )
        if request.agent_name == "codebase-investigator":
            return _failure_result(status="api_error", reason_code="nonzero_exit", exit_code=1)
        return _ok_result(
            _bundle_wire(
                run_id=ctx.run_id, base_sha=ctx.base_sha, digest=plan.source_set_digest, observer_id=request.agent_name
            )
        )

    with pytest.raises(rr.ObserverWaveFailed) as excinfo:
        _run_wave(ctx, plan, _invoke, observer_requests)

    assert excinfo.value.reason_code == "observer_wave_multiple_failures"
    assert excinfo.value.reason_code != "observer_run_id_mismatch"
    by_id = {r.observer_id: r for r in excinfo.value.observer_results}
    assert by_id["retrospective-runtime-observer"].reason_code == "observer_run_id_mismatch"
    assert by_id["codebase-investigator"].reason_code == "nonzero_exit"
    assert by_id["web-researcher"].status == "ok"


@pytest.mark.parametrize("failing_observer", [spec.observer_id for spec in rr.EXPECTED_OBSERVER_MANIFEST])
def test_aggregate_failure_position_rotated_across_observers(failing_observer: str) -> None:
    """Regardless of WHICH of the 3 observers is the one that fails (not
    hardcoded to the first-dispatched observer), all 3 are still recovered
    to a terminal state."""
    ctx, plan, _results = _prepare(run_id=f"run-ac5-rot-{failing_observer}")
    observer_requests = [_observer_request(spec.observer_id) for spec in rr.EXPECTED_OBSERVER_MANIFEST]
    call_log: list[str] = []

    def _invoke(request: rr.AgentInvocationRequest) -> rr.AgentInvocationResult:
        call_log.append(request.agent_name)
        if request.agent_name == failing_observer:
            return _failure_result(status="api_error", reason_code="nonzero_exit", exit_code=1)
        return _ok_result(
            _bundle_wire(
                run_id=ctx.run_id, base_sha=ctx.base_sha, digest=plan.source_set_digest, observer_id=request.agent_name
            )
        )

    with pytest.raises(rr.ObserverWaveFailed) as excinfo:
        _run_wave(ctx, plan, _invoke, observer_requests)

    assert sorted(call_log) == sorted(spec.observer_id for spec in rr.EXPECTED_OBSERVER_MANIFEST)
    aggregate = {r.observer_id: r for r in excinfo.value.observer_results}
    assert aggregate[failing_observer].status == "failed"
    for spec in rr.EXPECTED_OBSERVER_MANIFEST:
        if spec.observer_id != failing_observer:
            assert aggregate[spec.observer_id].status == "ok"


# ---------------------------------------------------------------------------
# AC6 (runtime verification -- real local subprocesses, never mock
# returncode/exception substitutes): process lifecycle -- cancel vs.
# terminal collection, observer's own timeout, parent SIGINT/SIGTERM, and
# the ordering between the two.
# ---------------------------------------------------------------------------


def test_cancel_vs_terminal_collection(tmp_path: Path) -> None:
    """A normal (non-timeout) failure in one observer's REAL subprocess
    must never cancel/kill a PEER observer's own REAL subprocess -- the
    peer runs to genuine natural completion (proven by elapsed wall time,
    not merely by a returned status)."""
    ctx, plan, _results = _prepare(run_id="run-ac6-cancel")
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")

    failing_script = ["import sys", "sys.exit(3)"]

    def _slow_success_script(observer_id: str) -> list[str]:
        bundle = _bundle_wire(
            run_id=ctx.run_id, base_sha=ctx.base_sha, digest=plan.source_set_digest, observer_id=observer_id
        )
        payload = json.dumps(_wrapper_payload(bundle))
        return ["import time, sys", "time.sleep(0.6)", f"sys.stdout.write({payload!r})"]

    def _invoke(request: rr.AgentInvocationRequest) -> rr.AgentInvocationResult:
        if request.agent_name == "codebase-investigator":
            runner = _real_runner_running_script(failing_script)
        else:
            runner = _real_runner_running_script(_slow_success_script(request.agent_name))
        req = dataclasses.replace(request, json_schema_path=str(schema_path), cwd=str(tmp_path), timeout_sec=10)
        return rr.invoke_agent(req, runner=runner)

    observer_requests = [_observer_request(spec.observer_id) for spec in rr.EXPECTED_OBSERVER_MANIFEST]
    started_at = time.monotonic()
    with pytest.raises(rr.ObserverWaveFailed) as excinfo:
        _run_wave(ctx, plan, _invoke, observer_requests)
    elapsed = time.monotonic() - started_at

    # the 2 slow-but-genuinely-successful real subprocesses ran to their own
    # natural completion (0.6s sleep) -- never cancelled just because the
    # peer failed quickly.
    assert elapsed >= 0.55
    by_id = {r.observer_id: r for r in excinfo.value.observer_results}
    assert by_id["codebase-investigator"].status == "failed"
    assert by_id["retrospective-runtime-observer"].status == "ok"
    assert by_id["web-researcher"].status == "ok"


def test_observer_timeout_terminates_and_reaps_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An observer's OWN timeout terminates -> (bounded grace) -> kills ->
    reaps the REAL child subprocess -- proven by verifying the child's OWN
    pid (written by the child itself, read back from disk) is genuinely
    gone (``os.kill(pid, 0)`` raises ``ProcessLookupError``) after the call
    returns, never merely that our own function returned/raised."""
    monkeypatch.setattr(rr, "_CHILD_PROCESS_TERMINATE_GRACE_SEC", 0.3)
    pidfile = tmp_path / "child.pid"
    script = "\n".join(
        ["import os, time", f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))", "time.sleep(30)"]
    )

    started_at = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        rr._lifecycle_subprocess_run(
            [sys.executable, "-c", script], cwd=str(tmp_path), env=dict(os.environ),
            input=None, capture_output=True, text=True, timeout=0.2,
        )
    elapsed = time.monotonic() - started_at

    assert elapsed < 5.0  # never waited out the child's own 30s sleep
    assert not rr._ACTIVE_CHILD_PROCESSES  # unregistered -> reaped by the owning call itself

    deadline = time.monotonic() + 5.0
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pidfile.exists(), "child never started in time"
    child_pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_observer_timeout_terminates_and_reaps_subprocess_that_ignores_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #2646 AC6 requires distinguishing a child-only timeout from a
    child that IGNORES SIGTERM. Every other AC6 test's child dies
    immediately from a plain SIGTERM (the default disposition), so the
    ``kill()`` escalation branch inside ``_terminate_and_reap_process()``
    (case (b), the observer's OWN timeout path) was never actually
    exercised. This test uses a REAL child subprocess that explicitly
    installs ``signal.signal(signal.SIGTERM, signal.SIG_IGN)`` (never a
    mock/exception standing in for signal delivery), so
    ``proc.terminate()`` alone is a genuine no-op and the bounded grace
    period must actually elapse before ``proc.kill()`` (SIGKILL, which
    cannot be ignored) fires -- proven both by elapsed wall time (>= the
    monkeypatched grace period) and by the child's own pid (written by the
    child itself, read back from disk) genuinely vanishing
    (``os.kill(pid, 0)`` -> ``ProcessLookupError``) after the call
    returns."""
    grace_sec = 0.3
    monkeypatch.setattr(rr, "_CHILD_PROCESS_TERMINATE_GRACE_SEC", grace_sec)
    pidfile = tmp_path / "child.pid"
    script = "\n".join(
        [
            "import os, signal, time",
            # installed as the child's very first action (microseconds),
            # well before the 0.2s timeout below can ever fire.
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))",
            "time.sleep(30)",
        ]
    )

    started_at = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        rr._lifecycle_subprocess_run(
            [sys.executable, "-c", script], cwd=str(tmp_path), env=dict(os.environ),
            input=None, capture_output=True, text=True, timeout=0.2,
        )
    elapsed = time.monotonic() - started_at

    assert elapsed < 5.0  # never waited out the child's own 30s sleep
    # a plain SIGTERM alone is a genuine no-op against this child's SIG_IGN
    # -- proves the bounded grace period was actually spent (and the
    # kill() escalation branch actually fired), not skipped.
    assert elapsed >= grace_sec * 0.8
    assert not rr._ACTIVE_CHILD_PROCESSES  # unregistered -> reaped by the owning call itself

    deadline = time.monotonic() + 5.0
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pidfile.exists(), "child never started in time"
    child_pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_parent_sigterm_terminates_all_children_before_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A SIGTERM delivered to the PARENT process terminates every
    still-registered REAL child subprocess, confirms each one is actually
    reaped (not merely signalled), and only THEN allows
    ``run_scoped_temp_dir``'s own cleanup to proceed -- proven end-to-end
    with a genuine background worker thread blocked inside the SAME
    production ``_lifecycle_subprocess_run`` primitive a real observer
    dispatch uses."""
    monkeypatch.setattr(rr, "_CHILD_PROCESS_TERMINATE_GRACE_SEC", 0.3)
    pidfile = tmp_path / "child.pid"
    script = "\n".join(
        ["import os, time", f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))", "time.sleep(30)"]
    )
    worker_errors: list[BaseException] = []

    def _run_child() -> None:
        try:
            rr._lifecycle_subprocess_run(
                [sys.executable, "-c", script], cwd=str(tmp_path), env=dict(os.environ),
                input=None, capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            worker_errors.append(AssertionError("child should have been killed by SIGTERM, not timed out"))
        except BaseException as exc:  # pragma: no cover - diagnostics only
            worker_errors.append(exc)

    worker = threading.Thread(target=_run_child, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5.0
    while not rr._ACTIVE_CHILD_PROCESSES and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rr._ACTIVE_CHILD_PROCESSES, "child never registered in time"
    deadline = time.monotonic() + 5.0
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pidfile.exists(), "child never started in time"
    child_pid = int(pidfile.read_text())
    assert os.kill(child_pid, 0) is None  # still alive at this point

    scope_dir = tmp_path / "scope"
    with pytest.raises(rr.RunInterrupted):
        with rr.run_scoped_temp_dir("run-ac6-sigterm", base_dir=scope_dir):
            os.kill(os.getpid(), signal.SIGTERM)

    worker.join(timeout=5.0)
    assert not worker.is_alive()
    assert worker_errors == []
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert not rr._ACTIVE_CHILD_PROCESSES
    assert not (scope_dir / "agent-retrospective-run-run-ac6-sigterm").exists()


def test_parent_sigterm_terminates_all_children_before_cleanup_when_child_ignores_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #2646 AC6 requires distinguishing a parent-only SIGTERM from a
    child that IGNORES SIGTERM. ``test_parent_sigterm_terminates_all_children_before_cleanup``'s
    child dies immediately from a plain SIGTERM (the default disposition),
    so the ``kill()`` escalation branch inside
    ``terminate_all_active_child_processes()`` (case (c), the PARENT
    SIGTERM path) was never actually exercised. This test uses a REAL
    child subprocess that explicitly installs
    ``signal.signal(signal.SIGTERM, signal.SIG_IGN)`` (never a mock/
    exception standing in for signal delivery), so the plain
    ``proc.terminate()`` call ``terminate_all_active_child_processes()``
    sends is a genuine no-op, forcing it to wait out the bounded grace
    period and then escalate to ``proc.kill()`` (SIGKILL, which cannot be
    ignored) -- proven both by elapsed wall time (>= the monkeypatched
    grace period) and by the child's own pid genuinely vanishing
    (``os.kill(pid, 0)`` -> ``ProcessLookupError``), with
    ``run_scoped_temp_dir``'s own cleanup only proceeding after that reap
    is confirmed."""
    grace_sec = 0.3
    monkeypatch.setattr(rr, "_CHILD_PROCESS_TERMINATE_GRACE_SEC", grace_sec)
    pidfile = tmp_path / "child.pid"
    script = "\n".join(
        [
            "import os, signal, time",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))",
            "time.sleep(30)",
        ]
    )
    worker_errors: list[BaseException] = []

    def _run_child() -> None:
        try:
            rr._lifecycle_subprocess_run(
                [sys.executable, "-c", script], cwd=str(tmp_path), env=dict(os.environ),
                input=None, capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            worker_errors.append(AssertionError("child should have been killed by the SIGTERM escalation, not timed out"))
        except BaseException as exc:  # pragma: no cover - diagnostics only
            worker_errors.append(exc)

    worker = threading.Thread(target=_run_child, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5.0
    while not rr._ACTIVE_CHILD_PROCESSES and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rr._ACTIVE_CHILD_PROCESSES, "child never registered in time"
    deadline = time.monotonic() + 5.0
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pidfile.exists(), "child never started in time"
    child_pid = int(pidfile.read_text())
    assert os.kill(child_pid, 0) is None  # still alive at this point

    scope_dir = tmp_path / "scope"
    started_at = time.monotonic()
    with pytest.raises(rr.RunInterrupted):
        with rr.run_scoped_temp_dir("run-ac6-sigterm-ignoring-child", base_dir=scope_dir):
            os.kill(os.getpid(), signal.SIGTERM)
    elapsed = time.monotonic() - started_at

    worker.join(timeout=5.0)
    assert not worker.is_alive()
    assert worker_errors == []
    # a plain terminate() alone is a genuine no-op against this child's
    # SIG_IGN -- proves the bounded grace period was actually spent (and
    # the kill() escalation branch actually fired), not skipped.
    assert elapsed >= grace_sec * 0.8
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert not rr._ACTIVE_CHILD_PROCESSES
    assert not (scope_dir / "agent-retrospective-run-run-ac6-sigterm-ignoring-child").exists()


def test_sigterm_vs_observer_timeout_lifecycle_ordering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An observer's OWN timeout (case (b)) and a parent SIGTERM (case (c))
    are independent lifecycle paths: an already-timed-out-and-reaped
    observer's cleanup completes entirely on its own schedule, BEFORE any
    SIGTERM is ever sent, and a SEPARATE, still-running observer is the one
    actually affected by the later SIGTERM -- neither path leaks a
    registry entry or a live child into the other."""
    monkeypatch.setattr(rr, "_CHILD_PROCESS_TERMINATE_GRACE_SEC", 0.3)

    # --- case (b): observer A's OWN timeout, entirely self-contained,
    # completed and reaped before case (c) even begins ---
    pidfile_a = tmp_path / "a.pid"
    script_a = "\n".join(
        ["import os, time", f"open({str(pidfile_a)!r}, 'w').write(str(os.getpid()))", "time.sleep(30)"]
    )
    with pytest.raises(subprocess.TimeoutExpired):
        rr._lifecycle_subprocess_run(
            [sys.executable, "-c", script_a], cwd=str(tmp_path), env=dict(os.environ),
            input=None, capture_output=True, text=True, timeout=0.2,
        )
    deadline = time.monotonic() + 5.0
    while not pidfile_a.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    pid_a = int(pidfile_a.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid_a, 0)
    assert not rr._ACTIVE_CHILD_PROCESSES

    # --- case (c): a DIFFERENT, still-running observer B, terminated by an
    # external SIGTERM to the parent ---
    pidfile_b = tmp_path / "b.pid"
    script_b = "\n".join(
        ["import os, time", f"open({str(pidfile_b)!r}, 'w').write(str(os.getpid()))", "time.sleep(30)"]
    )
    worker_errors: list[BaseException] = []

    def _run_child_b() -> None:
        try:
            rr._lifecycle_subprocess_run(
                [sys.executable, "-c", script_b], cwd=str(tmp_path), env=dict(os.environ),
                input=None, capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            worker_errors.append(AssertionError("child B should have been killed by SIGTERM, not timed out"))
        except BaseException as exc:  # pragma: no cover - diagnostics only
            worker_errors.append(exc)

    worker = threading.Thread(target=_run_child_b, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5.0
    while not pidfile_b.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pidfile_b.exists(), "child B never started in time"
    pid_b = int(pidfile_b.read_text())
    assert os.kill(pid_b, 0) is None

    scope_dir = tmp_path / "scope"
    with pytest.raises(rr.RunInterrupted):
        with rr.run_scoped_temp_dir("run-ac6-ordering", base_dir=scope_dir):
            os.kill(os.getpid(), signal.SIGTERM)

    worker.join(timeout=5.0)
    assert not worker.is_alive()
    assert worker_errors == []
    with pytest.raises(ProcessLookupError):
        os.kill(pid_b, 0)
    assert not rr._ACTIVE_CHILD_PROCESSES
