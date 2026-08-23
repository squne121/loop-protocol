"""
.claude/skills/issue-refinement-loop/scripts/tests/test_workflow_start_entry.py

Hermetic behavioral tests for `workflow_start_entry.py` (Issue #2311).

These tests exercise `workflow_start_entry.run()` with an injected fake
capability-preflight producer and an injected fake inner-preflight
invoker (dependency injection, matching this repo's existing
`FileBackedFakeGitHubEntryTransport` pattern in `root_entry_router.py` --
not internal monkeypatching of module internals). No live GitHub API call
and no live `uv`/`gh` subprocess is made by any test in this module; the
inner-preflight invoker fake stands in for both `run_refinement_preflight.py`
and any GitHub mutation it might otherwise perform, so proving it was
called zero times on the blocked path also proves zero GitHub mutation
occurred on that path (AC3's "no downstream GitHub mutation" claim).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import workflow_start_entry as wse  # noqa: E402

_REPO = "squne121/loop-protocol"
_VALID_PLANNED_OPERATIONS_JSON = (
    '[{"phase": "workflow_start", "actor_role": "issue-refinement-loop", '
    '"requires_mutation": false}]'
)


def _make_recording_producer(decision: str, checks: dict | None = None, reasons: list | None = None):
    calls: list[dict] = []

    def _producer(**kwargs):
        calls.append(kwargs)
        return {"decision": decision, "checks": checks or {}, "reasons": reasons or []}

    return _producer, calls


def _make_recording_inner(returncode: int = 0):
    calls: list[dict] = []

    def _inner(**kwargs):
        calls.append(kwargs)
        return returncode

    return _inner, calls


def _failing_inner(**kwargs):
    raise AssertionError("inner preflight (and therefore any GitHub mutation) must not be invoked")


# ---------------------------------------------------------------------------
# AC3: blocked decision never invokes the inner preflight / GitHub mutation.
# ---------------------------------------------------------------------------


def test_workflow_start_blocked_does_not_invoke_inner_preflight():
    producer, producer_calls = _make_recording_producer("blocked", reasons=["no_trusted_uv"])

    result, exit_code = wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
        capability_preflight_result_fn=producer,
        invoke_inner_preflight_fn=_failing_inner,
    )

    assert len(producer_calls) == 1
    assert result["inner_preflight_invoked"] is False
    assert result["status"] == "blocked"
    assert exit_code != 0


def test_workflow_start_blocked_performs_no_github_mutation():
    """AC3: the inner-preflight invoker fake is the sole place a GitHub
    mutation could occur in this module's control flow; a `blocked`
    decision must never reach it."""
    producer, producer_calls = _make_recording_producer("blocked", reasons=["github_auth_failed"])

    result, _exit_code = wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
        capability_preflight_result_fn=producer,
        invoke_inner_preflight_fn=_failing_inner,
    )

    assert len(producer_calls) == 1
    assert result["inner_preflight_invoked"] is False


# ---------------------------------------------------------------------------
# AC4: ready / degraded invoke the inner preflight exactly once.
# ---------------------------------------------------------------------------


def test_workflow_start_ready_invokes_inner_once():
    producer, producer_calls = _make_recording_producer("ready")
    inner, inner_calls = _make_recording_inner(returncode=0)

    result, exit_code = wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
        capability_preflight_result_fn=producer,
        invoke_inner_preflight_fn=inner,
    )

    assert len(producer_calls) == 1
    assert len(inner_calls) == 1
    assert result["inner_preflight_invoked"] is True
    assert result["status"] == "ready"
    assert exit_code == 0


def test_workflow_start_degraded_uses_declared_fallback_once():
    producer, producer_calls = _make_recording_producer(
        "degraded", checks={"spark": {"status": "fallback_only"}}
    )
    inner, inner_calls = _make_recording_inner(returncode=0)

    result, exit_code = wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode="sonnet",
        spark_fallback="haiku",
        planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
        capability_preflight_result_fn=producer,
        invoke_inner_preflight_fn=inner,
    )

    assert len(producer_calls) == 1
    assert producer_calls[0]["spark_mode"] == "sonnet"
    assert producer_calls[0]["spark_fallback"] == "haiku"
    assert len(inner_calls) == 1
    assert result["inner_preflight_invoked"] is True
    assert result["decision"] == "degraded"
    assert exit_code == 0


# ---------------------------------------------------------------------------
# AC5: exact caller-declared spark/planned_operations pass-through; missing
# planned_operations fails closed BEFORE the producer is called.
# ---------------------------------------------------------------------------


def test_workflow_start_passes_exact_spark_and_planned_operations():
    producer, producer_calls = _make_recording_producer("ready")
    inner, inner_calls = _make_recording_inner(returncode=0)
    planned_operations_json = (
        '[{"phase": "workflow_start", "actor_role": "issue-refinement-loop", '
        '"requires_mutation": false}, '
        '{"phase": "step0g_contract_update", "actor_role": "contract-repair", '
        '"requires_mutation": true}]'
    )

    wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode="opus",
        spark_fallback="sonnet",
        planned_operations_json=planned_operations_json,
        capability_preflight_result_fn=producer,
        invoke_inner_preflight_fn=inner,
    )

    assert len(producer_calls) == 1
    call = producer_calls[0]
    assert call["repo"] == _REPO
    assert call["spark_mode"] == "opus"
    assert call["spark_fallback"] == "sonnet"
    assert call["planned_operations"] == [
        {"phase": "workflow_start", "actor_role": "issue-refinement-loop", "requires_mutation": False},
        {"phase": "step0g_contract_update", "actor_role": "contract-repair", "requires_mutation": True},
    ]
    assert len(inner_calls) == 1


@pytest.mark.parametrize(
    "planned_operations_json",
    [
        None,
        "",
        "not json",
        "{}",
        "[]",
        '[{"phase": "p"}]',
        '["not-an-object"]',
    ],
)
def test_workflow_start_missing_planned_operations_fails_closed(planned_operations_json):
    def _unreachable_producer(**kwargs):
        raise AssertionError("producer must not be invoked when the caller request is malformed")

    result, exit_code = wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=planned_operations_json,
        capability_preflight_result_fn=_unreachable_producer,
        invoke_inner_preflight_fn=_failing_inner,
    )

    assert result["status"] == "blocked"
    assert result["reason"].startswith("environment_failure:")
    assert result["inner_preflight_invoked"] is False
    assert exit_code != 0


# ---------------------------------------------------------------------------
# fix_delta (PR #2320 review B1): a genuine CLI/env-level OMISSION of
# `--planned-operations-json` / `LOOP_PLANNED_OPERATIONS_JSON` -- exactly
# what the canonical bare `preflight.run` registry argv produces, since it
# only ever carries `--issue-number`/`--repo` -- must reach the producer
# (empty operations set), matching `workflow_capability_preflight.py`'s own
# omitted-is-valid-empty-list semantics. This is distinct from an
# explicitly-supplied-but-malformed/empty value, which still fails closed
# (covered by `test_workflow_start_missing_planned_operations_fails_closed`
# above, unchanged).
# ---------------------------------------------------------------------------


def test_workflow_start_omitted_planned_operations_reaches_producer():
    """`run(planned_operations_omitted=True)` must bypass the strict
    missing/malformed check and reach the producer with an empty
    operations list, unlike a direct `planned_operations_json=None` call
    (see the parametrized negative test above, which is unaffected: it
    never sets `planned_operations_omitted`)."""
    producer, producer_calls = _make_recording_producer("ready")
    inner, inner_calls = _make_recording_inner(returncode=0)

    result, exit_code = wse.run(
        issue_number=2311,
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=None,
        planned_operations_omitted=True,
        capability_preflight_result_fn=producer,
        invoke_inner_preflight_fn=inner,
    )

    assert len(producer_calls) == 1
    assert producer_calls[0]["planned_operations"] == []
    assert len(inner_calls) == 1
    assert result["inner_preflight_invoked"] is True
    assert result["status"] == "ready"
    assert exit_code == 0


def test_build_capability_request_omitted_yields_empty_operations_list():
    request = wse.build_capability_request(
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=None,
        planned_operations_omitted=True,
    )
    assert request["planned_operations"] == []


def test_main_cli_with_only_issue_number_and_repo_resolves_omitted_flag():
    """Reproduces the real production registry-driven invocation shape
    (`--issue-number` / `--repo` only, matching `command_registry.py`'s
    bare `preflight.run` argv byte-for-byte) at the `main()` CLI-parsing
    layer, and proves it resolves to `planned_operations_omitted=True` /
    `planned_operations_json=None` rather than raising a missing-request
    error before `run()` is ever reached. `wse.run` is monkeypatched (not
    `capability_preflight_result_fn`, which is bound as an early-evaluated
    default and would not observe a monkeypatch) so no live subprocess or
    GitHub call is made -- this is a pure CLI-argument-resolution
    regression test for the exact bug the reviewer reproduced (B1)."""
    captured_kwargs: dict = {}

    def _fake_run(**kwargs):
        captured_kwargs.update(kwargs)
        return (
            {
                "schema": wse.SCHEMA_VERSION,
                "status": "ready",
                "reason": None,
                "decision": "ready",
                "checks": {},
                "reasons": [],
                "inner_preflight_invoked": True,
            },
            0,
        )

    original_run = wse.run
    wse.run = _fake_run
    try:
        exit_code = wse.main(["--issue-number", "2311", "--repo", _REPO])
    finally:
        wse.run = original_run

    assert exit_code == 0
    assert captured_kwargs["planned_operations_json"] is None
    assert captured_kwargs["planned_operations_omitted"] is True
    assert captured_kwargs["issue_number"] == 2311
    assert captured_kwargs["repo"] == _REPO


# ---------------------------------------------------------------------------
# AC7: blocked reason/checks/reasons are preserved verbatim in the compact
# result (no boolean reduction on the Step 0 path).
# ---------------------------------------------------------------------------


def test_workflow_start_preserves_block_reason():
    producer, _calls = _make_recording_producer(
        "blocked",
        checks={"uv": {"status": "missing"}, "github": {"auth": False}},
        reasons=["uv_not_found", "github_auth_failed"],
    )

    result, _exit_code = wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
        capability_preflight_result_fn=producer,
        invoke_inner_preflight_fn=_failing_inner,
    )

    assert result["checks"] == {"uv": {"status": "missing"}, "github": {"auth": False}}
    assert result["reasons"] == ["uv_not_found", "github_auth_failed"]
    assert result["reason"] == "capability_preflight_blocked"


# ---------------------------------------------------------------------------
# AC3: producer invocation failure / malformed result fails closed exactly
# like a `blocked` decision (root_entry_router.capability_preflight_result
# itself normalizes both into decision="blocked" -- this test proves
# workflow_start_entry.run() treats that normalized shape correctly and
# still never invokes the inner preflight).
# ---------------------------------------------------------------------------


def test_workflow_start_malformed_producer_result_fails_closed():
    def _malformed_producer(**kwargs):
        # Mirrors root_entry_router.capability_preflight_result()'s own
        # fail-closed normalization of a malformed/failed producer
        # invocation into a synthetic decision="blocked" result.
        return {
            "decision": "blocked",
            "checks": {},
            "reasons": ["producer_result_malformed:non_json_stdout"],
        }

    result, exit_code = wse.run(
        issue_number=1228,
        repo=_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations_json=_VALID_PLANNED_OPERATIONS_JSON,
        capability_preflight_result_fn=_malformed_producer,
        invoke_inner_preflight_fn=_failing_inner,
    )

    assert result["status"] == "blocked"
    assert result["inner_preflight_invoked"] is False
    assert "producer_result_malformed:non_json_stdout" in result["reasons"]
    assert exit_code != 0


# ---------------------------------------------------------------------------
# AC2: workflow_start_entry.py must not reimplement the capability-preflight
# producer invocation itself -- it must call
# root_entry_router.capability_preflight_result (imported, not duplicated).
# ---------------------------------------------------------------------------


def test_workflow_start_uses_root_entry_router_capability_preflight_result():
    import root_entry_router as rer

    assert wse.root_entry_router is rer
    # The production default callable wired into run() is exactly
    # root_entry_router.capability_preflight_result -- not a local
    # reimplementation.
    import inspect

    sig = inspect.signature(wse.run)
    default = sig.parameters["capability_preflight_result_fn"].default
    assert default is rer.capability_preflight_result
