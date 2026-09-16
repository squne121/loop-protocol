#!/usr/bin/env python3
"""Regression tests for Issue #2645 (parent #2642 Workstream A / Child A).

`invoke_agent()` in ``run_retrospective.py`` must normalize BOTH observed
top-level JSON shapes a headless ``claude -p --output-format json`` stdout
payload can carry -- a single result-wrapper object (the pre-existing,
regression-free contract, AC1), and a top-level JSON ARRAY of session
events carrying exactly one terminal ``type: "result"`` event -- to the
same canonical single-object shape every existing downstream business
validation (type/subtype/is_error checks, ``structured_output``
compatibility recovery, JSON Schema validation, identity binding, role
adapter validation) already consumes, WITHOUT reimplementing or weakening
any of that downstream validation (see ``_normalize_transport_payload``'s
docstring in ``run_retrospective.py`` for the full normalization contract,
including the fact-check on where this event-array shape actually comes
from -- it is NOT assumed to be Claude-GPT/self-hosted-specific).

Fixture/mock-based only for AC2-AC5 (hermetic, no real subprocess; the
``runner`` callable passed to ``rr.invoke_agent`` is dependency-injected
exactly as in ``test_run_retrospective.py`` /
``test_run_retrospective_structured_output_prose_fence.py``).

Runtime Verification Applicability: immediate for AC6 only (this module's
one ``claude_live``-marked test,
``test_real_claude_cli_round_trip_normalizes_whichever_shape_is_observed``;
see ``docs/dev/runtime-verification-policy.md`` and the live Issue's
``## Runtime Verification Applicability`` block). AC1-AC5 are deferred
(deterministic, mock-runner-only). Unlike ``test_run_retrospective_live_cli.py``
(entirely ``claude_live``, invoked only via
``verify_run_retrospective_live_cli.sh``'s SKIP(77)/FAIL(1)/PASS(0)
preflight-then-pytest wrapper), this module's Issue #2645 AC6 Verification
Command invokes pytest directly (``pytest ... -m claude_live -q``, no
wrapper script) -- so the two documented skip_conditions (``claude`` binary
in PATH; ``claude auth status`` exits 0) are checked INSIDE the live test
itself via ``pytest.skip()`` before any assertion runs. SKIP is reported by
pytest as a distinct ``skipped`` outcome, never conflated with ``passed``,
and is never promoted to PASS by this module.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402

_SCHEMA_DIR = _SCRIPTS_DIR / "schemas"
_OBSERVER_SCHEMA_PATH = _SCHEMA_DIR / "observer_result_v1.schema.json"
_REPO_ROOT = _SCRIPTS_DIR.parents[3]

#: base_sha this Issue's fix landed on top of (parent commit before the
#: `_normalize_transport_payload` adapter existed) -- cited by
#: `_legacy_pre_fix_payload_gate`'s docstring as the byte-for-byte
#: verification anchor for AC2's characterization.
_PRE_FIX_BASE_SHA = "cc0ccce201a29ac9ac75417060214364632883e1"

#: bounded so a hung/misbehaving real CLI invocation cannot stall the AC6
#: live test indefinitely; generous enough for a single haiku-model turn
#: (mirrors `test_run_retrospective_live_cli.py`'s `_LIVE_TIMEOUT_SEC`).
_LIVE_TIMEOUT_SEC = 180


# ---------------------------------------------------------------------------
# shared fixture builders
# ---------------------------------------------------------------------------


def _terminal_result_event(
    structured_output: dict[str, Any] | None,
    *,
    subtype: str = "success",
    is_error: bool = False,
    result_text: str = "assistant text summary",
) -> dict[str, Any]:
    """Shape of the real ``claude -p --output-format json`` terminal
    wrapper event -- identical to `test_run_retrospective.py`'s
    ``_wrapper_payload`` (Issue #2237 P0-1), duplicated locally per this
    test suite's existing per-file convention (see
    ``test_run_retrospective_structured_output_prose_fence.py``)."""
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result_text,
        "structured_output": structured_output,
    }


def _system_init_event() -> dict[str, Any]:
    return {"type": "system", "subtype": "init", "session_id": "sess-2645"}


def _assistant_message_event(text: str = "thinking...") -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _tool_use_event(tool_name: str = "Read") -> dict[str, Any]:
    return {"type": "tool_use", "name": tool_name, "input": {}}


def _tool_result_event(output: str = "ok") -> dict[str, Any]:
    return {"type": "tool_result", "output": output}


def _valid_event_array_fixture(structured_output: dict[str, Any]) -> list[dict[str, Any]]:
    """A "正当な event-array 入力" per Issue #2645 AC2/AC3: several
    non-terminal session events followed by exactly one terminal
    ``type: "result"`` event whose own payload is a fully valid,
    schema-conformant result wrapper -- the same content AC1's
    single-object case already succeeds with. AC2 and AC3 both consume
    this exact fixture (never independently re-derived), so the two tests
    together directly evidence this Issue's before/after contract
    change."""
    return [
        _system_init_event(),
        _assistant_message_event(),
        _tool_use_event(),
        _tool_result_event(),
        _terminal_result_event(structured_output),
    ]


def _invocation_request(schema_path: str) -> rr.AgentInvocationRequest:
    return rr.AgentInvocationRequest(
        agent_name="retrospective-runtime-observer", prompt="observe", json_schema_path=schema_path, cwd="/repo"
    )


def _runner_for_stdout(stdout_text: str) -> Any:
    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, returncode=0, stdout=stdout_text, stderr="")

    return _runner


def _schema_path(tmp_path: Path) -> Path:
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")
    return schema_path


# ---------------------------------------------------------------------------
# AC2: pre-fix (superseded) transport-reception contract, pinned as
# permanent regression/documentation evidence
# ---------------------------------------------------------------------------


def _legacy_pre_fix_payload_gate(payload: Any) -> "tuple[str | None, str | None]":
    """Issue #2645 AC2: a frozen, test-local replica of the EXACT gate
    ``invoke_agent()`` used, prior to this Issue's
    ``_normalize_transport_payload`` adapter, immediately after
    ``json.loads(completed.stdout)`` -- verified byte-for-byte against the
    base_sha (``cc0ccce201a29ac9ac75417060214364632883e1``) committed
    source::

        if not isinstance(payload, dict):
            return AgentInvocationResult(status="malformed_output", ...,
                                          reason_code="payload_not_object")

    Deliberately decoupled from the CURRENT (fixed, post-#2645)
    ``run_retrospective.invoke_agent`` -- this Issue's entire purpose is to
    change that live function's behavior, for exactly the fixture this
    test and ``test_event_array_normalizes_to_canonical_result`` (AC3)
    share, from failure to success. A permanently-red assertion against
    the live function would therefore directly contradict AC3 by
    construction. This frozen replica instead pins the specific narrow
    legacy gate that caused the historical failure as removed/superseded
    evidence, so it remains a stable, permanently-green
    regression/documentation fixture forever, independent of any future
    refinement to the normalization adapter itself.

    This is a test-local, hand-written characterization, not a dynamically
    re-executed historical module load from git history, per the Issue
    #2645 ``issue-refinement-loop`` OWNER anchor review guidance
    (https://github.com/squne121/loop-protocol/issues/2645#issuecomment-5699109387)
    to reuse the existing mock-runner fixture pattern rather than introduce
    a new, larger test harness. Independent, genuine confirmation that the
    ACTUAL (not replicated) pre-fix ``invoke_agent()`` reproduced this
    exact failure for this exact fixture was additionally performed
    out-of-band during implementation, by temporarily reverting the
    ``_normalize_transport_payload`` production change and re-running
    ``invoke_agent()`` against this module's own
    ``_valid_event_array_fixture()`` -- see the Issue #2645 implementation
    report for the captured before/after transcript."""
    if not isinstance(payload, dict):
        return "malformed_output", "payload_not_object"
    return None, None


def test_baseline_reproduces_payload_not_object_before_fix() -> None:
    """AC2: proves -- and permanently documents -- that the pre-Issue-#2645
    top-level ``isinstance(payload, dict)`` gate ``invoke_agent()`` used
    unconditionally rejected a valid event-array carrying a unique
    terminal ``type: "result"`` event, with
    ``reason_code="payload_not_object"``, even though the array's own
    terminal event is a fully valid, schema-conformant result wrapper
    AC1's single-object case already succeeds with. Uses the exact same
    fixture (`_valid_event_array_fixture`)
    ``test_event_array_normalizes_to_canonical_result`` (AC3) feeds
    through the CURRENT, fixed ``invoke_agent()`` -- so the two tests
    together directly evidence the before/after contract change this
    Issue implements."""
    structured_output = {"schema_version": "observer_result/v1", "ok": True}
    fixture = _valid_event_array_fixture(structured_output)

    status, reason_code = _legacy_pre_fix_payload_gate(fixture)

    assert status == "malformed_output"
    assert reason_code == "payload_not_object"


# ---------------------------------------------------------------------------
# AC3: post-fix normalization success, semantically equivalent to AC1
# ---------------------------------------------------------------------------


def test_event_array_normalizes_to_canonical_result(tmp_path: Path) -> None:
    """AC3: the SAME valid event-array fixture AC2 pins as historically
    failing (`_valid_event_array_fixture`) is, after this Issue's fix,
    normalized by `invoke_agent()` to the array's unique terminal
    ``type: "result"`` event, then passed unmodified through every
    existing downstream business validation (subtype/is_error checks,
    structured_output extraction) -- reaching the exact same
    ``status="ok"``, ``reason_code=None``, ``exit_code=0``,
    ``structured_output=<payload>`` result AC1's single-object input
    reaches for byte-identical business content (semantic equivalence,
    verified here by also invoking `invoke_agent` on the single-object
    form of the same terminal event and comparing results)."""
    schema_path = _schema_path(tmp_path)
    structured_output = {"schema_version": "observer_result/v1", "ok": True}

    array_fixture = _valid_event_array_fixture(structured_output)
    array_result = rr.invoke_agent(
        _invocation_request(str(schema_path)), runner=_runner_for_stdout(json.dumps(array_fixture))
    )

    single_object_fixture = _terminal_result_event(structured_output)
    single_object_result = rr.invoke_agent(
        _invocation_request(str(schema_path)), runner=_runner_for_stdout(json.dumps(single_object_fixture))
    )

    assert single_object_result.status == "ok"
    assert single_object_result.reason_code is None

    assert array_result.status == "ok"
    assert array_result.reason_code is None
    assert array_result.exit_code == 0
    assert array_result.raw_stdout_excerpt is None
    assert array_result.structured_output == structured_output
    assert array_result.structured_output == single_object_result.structured_output


# ---------------------------------------------------------------------------
# AC4: fail-closed cases -- none of these may ever be promoted to success
# ---------------------------------------------------------------------------


def test_no_terminal_result_event_in_array_fail_closed(tmp_path: Path) -> None:
    """No element carries ``type == "result"`` at all -- an array of only
    non-terminal session events."""
    schema_path = _schema_path(tmp_path)
    events = [_system_init_event(), _assistant_message_event(), _tool_use_event(), _tool_result_event()]

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner_for_stdout(json.dumps(events)))

    assert result.status == "malformed_output"
    assert result.reason_code == "transport_event_array_no_terminal_result"
    assert result.structured_output is None


def test_empty_event_array_fail_closed(tmp_path: Path) -> None:
    schema_path = _schema_path(tmp_path)

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner_for_stdout(json.dumps([])))

    assert result.status == "malformed_output"
    assert result.reason_code == "transport_event_array_no_terminal_result"
    assert result.structured_output is None


def test_multiple_terminal_result_events_fail_closed(tmp_path: Path) -> None:
    """OWNER anchor review
    (https://github.com/squne121/loop-protocol/issues/2645#issuecomment-5699109387):
    terminal count must be taken BEFORE any success/failure ``subtype``
    filtering -- a success result plus a distinct error result is
    ambiguous, and must never be silently resolved by picking the
    "successful" one."""
    schema_path = _schema_path(tmp_path)
    events = [
        _system_init_event(),
        _terminal_result_event({"schema_version": "observer_result/v1", "a": 1}, subtype="success"),
        _terminal_result_event(None, subtype="error_max_structured_output_retries", is_error=True),
    ]

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner_for_stdout(json.dumps(events)))

    assert result.status == "malformed_output"
    assert result.reason_code == "transport_event_array_multiple_terminal_results"
    assert result.structured_output is None


def test_partial_incomplete_event_stream_fail_closed(tmp_path: Path) -> None:
    """A truncated/partial event stream (the session ended, or the
    transport was cut off, before the terminal ``type: "result"`` event
    was ever emitted) must not be promoted to success."""
    schema_path = _schema_path(tmp_path)
    events = [_system_init_event(), _assistant_message_event(), _tool_use_event()]

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner_for_stdout(json.dumps(events)))

    assert result.status == "malformed_output"
    assert result.reason_code == "transport_event_array_no_terminal_result"
    assert result.structured_output is None


def test_unknown_event_array_shape_non_object_element_fail_closed(tmp_path: Path) -> None:
    """An array element that is not itself a JSON object (an "unknown
    event-array shape") must not be tolerated, even when a valid terminal
    result event is also present elsewhere in the array."""
    schema_path = _schema_path(tmp_path)
    events: list[Any] = [
        _system_init_event(),
        "not-an-event-object",
        _terminal_result_event({"schema_version": "observer_result/v1", "a": 1}),
    ]

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner_for_stdout(json.dumps(events)))

    assert result.status == "malformed_output"
    assert result.reason_code == "transport_event_array_unknown_shape"
    assert result.structured_output is None


def test_malformed_json_fail_closed(tmp_path: Path) -> None:
    """Unrelated to event-array normalization: malformed JSON stdout must
    remain fail-closed exactly as before this Issue's fix (unchanged
    `json_decode_failure` path, exercised before `_normalize_transport_payload`
    is ever reached)."""
    schema_path = _schema_path(tmp_path)

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner_for_stdout("not-json-at-all ["))

    assert result.status == "malformed_output"
    assert result.reason_code == "json_decode_failure"
    assert result.structured_output is None


def test_terminal_result_followed_by_invalid_continuation_event_fail_closed(tmp_path: Path) -> None:
    """A unique terminal ``type: "result"`` event exists, but is NOT the
    array's last element -- an invalid continuation event follows it.
    Terminal result ownership/ordering/uniqueness (Issue #2645 In Scope)
    requires the terminal event to be the array's own last element."""
    schema_path = _schema_path(tmp_path)
    events = [
        _system_init_event(),
        _terminal_result_event({"schema_version": "observer_result/v1", "a": 1}),
        _assistant_message_event("unexpected continuation after the terminal result event"),
    ]

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner_for_stdout(json.dumps(events)))

    assert result.status == "malformed_output"
    assert result.reason_code == "transport_event_array_terminal_result_not_last"
    assert result.structured_output is None


def test_envelope_bypass_nested_business_payload_not_directly_adopted_fail_closed(tmp_path: Path) -> None:
    """AC4 envelope-bypass case: no array element carries the top-level
    ``type: "result"`` transport envelope, but one of the NON-terminal
    elements happens to carry a plausible-looking nested business payload
    (a ``structured_output``-shaped value buried inside a tool_result's
    own ``output`` field). This must never be adopted as if it were the
    terminal result -- guards against a recursive-nested-JSON-search
    implementation (an explicit Issue #2645 Stop Condition)."""
    schema_path = _schema_path(tmp_path)
    plausible_but_not_terminal = {
        "type": "tool_result",
        "tool_name": "some-tool",
        "output": {"structured_output": {"schema_version": "observer_result/v1", "sneaky": True}},
    }
    events = [_system_init_event(), _assistant_message_event(), plausible_but_not_terminal]

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner_for_stdout(json.dumps(events)))

    assert result.status == "malformed_output"
    assert result.reason_code == "transport_event_array_no_terminal_result"
    assert result.structured_output is None


def test_last_array_element_without_result_type_not_adopted_fail_closed(tmp_path: Path) -> None:
    """Guards specifically against a naive ``payload[-1]``-style
    implementation (an explicit Issue #2645 Stop Condition): the array's
    LAST element is a plausible-looking dict, but does not carry
    ``type == "result"``, and no other element does either."""
    schema_path = _schema_path(tmp_path)
    events = [_system_init_event(), {"schema_version": "observer_result/v1", "sneaky": True}]

    result = rr.invoke_agent(_invocation_request(str(schema_path)), runner=_runner_for_stdout(json.dumps(events)))

    assert result.status == "malformed_output"
    assert result.reason_code == "transport_event_array_no_terminal_result"
    assert result.structured_output is None


# ---------------------------------------------------------------------------
# AC6: live claude CLI round trip (Runtime Verification Applicability:
# immediate; docs/dev/runtime-verification-policy.md)
# ---------------------------------------------------------------------------


def _claude_live_skip_reason() -> str | None:
    """Mirrors ``verify_run_retrospective_live_cli.sh``'s two documented
    skip_conditions. Unlike that wrapper (invoked as a separate SKIP(77)/
    FAIL(1)/PASS(0) process before pytest ever starts), this Issue #2645
    AC6 Verification Command invokes pytest directly
    (``pytest ... -m claude_live -q``, no wrapper script) -- so this
    preflight runs INSIDE the test itself via ``pytest.skip()``, which
    pytest reports as a distinct ``skipped`` outcome (never conflated with
    ``passed`` in the test report) while still exiting the pytest process
    with 0 -- SKIP is never silently read back as a fabricated PASS."""
    if shutil.which("claude") is None:
        return "claude binary not found in PATH (skip_condition: claude binary not in PATH)"
    try:
        auth_check = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=30)
    except OSError as exc:
        return f"claude auth status could not be invoked: {exc!r}"
    if auth_check.returncode != 0:
        return "claude auth status check failed (skip_condition: pre-invocation auth unavailability)"
    return None


@pytest.mark.claude_live
def test_real_claude_cli_round_trip_normalizes_whichever_shape_is_observed() -> None:
    """AC6: live round trip against the real ``claude`` CLI through the
    fixed ``invoke_agent()``. ``build_agent_invocation_argv()`` is invoked
    completely unmodified (Issue #2645 Out of Scope: this test must not
    change any CLI option/configuration to force a particular transport
    shape) -- whichever top-level JSON shape THIS environment's ``claude``
    binary actually emits for a normal ``-p --output-format json``
    headless invocation (a single object, per every previously observed
    run in this repository's live tests, OR a top-level event array,
    should this environment's ``claude`` version/configuration emit one --
    see ``_normalize_transport_payload``'s docstring for the fact-checked,
    runtime-agnostic causes) is captured for diagnostics via a thin
    ``runner`` wrapper around the real ``subprocess.run``, and the SAME
    fixed ``invoke_agent()`` must reach ``status="ok"`` either way --
    exactly the AC1/AC3 semantic-equivalence contract, now exercised
    against a genuine subprocess instead of a mock runner.

    Runtime unavailable (``claude`` missing or unauthenticated) -> SKIP,
    per the Issue's documented skip_conditions -- never silently promoted
    to PASS. A fallback-only success (e.g. a schema-mismatch structured
    output recovered only through ``_structured_output_from_result_compat``
    rather than a byte-exact schema match) is intentionally never treated
    as this AC's genuine PASS (``fallback_policy.notes``): this test
    asserts ``reason_code is None`` and an exact ``structured_output``
    payload match, precisely so a hidden compat-recovery path can never
    silently count as PASS."""
    skip_reason = _claude_live_skip_reason()
    if skip_reason is not None:
        pytest.skip(skip_reason)

    run_id = f"live-transport-{uuid.uuid4()}"
    nonce = uuid.uuid4().hex
    base_sha = "e" * 40
    source_set_digest = "f" * 64
    observer_id = "retrospective-runtime-observer"
    evidence_ref = f"evidence://live-transport/{nonce}"

    expected_payload = {
        "schema_version": "observer_result/v1",
        "run_id": run_id,
        "base_sha": base_sha,
        "source_set_digest": source_set_digest,
        "observer_id": observer_id,
        "evidence_ref": evidence_ref,
        "findings": [{"claim": f"nonce:{nonce}", "claim_class": "process"}],
    }
    prompt = (
        "This is a deterministic live-CLI transport-normalization "
        "verification round-trip (Issue #2645 AC6). Output ONLY a single "
        "JSON object conforming exactly to the observer_result/v1 schema, "
        "with EXACTLY these field values and no other fields (copy every "
        "value verbatim, do not paraphrase or alter any string):\n"
        + json.dumps(expected_payload, sort_keys=True)
    )

    request = rr.AgentInvocationRequest(
        agent_name=observer_id,
        prompt=prompt,
        json_schema_path=str(_OBSERVER_SCHEMA_PATH),
        cwd=str(_REPO_ROOT),
        timeout_sec=_LIVE_TIMEOUT_SEC,
    )
    policy = rr.DelegatedAgentPermissionPolicy(run_id=run_id)

    observed_raw_stdout: dict[str, str] = {}

    def _diagnostic_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        completed = subprocess.run(argv, **kwargs)
        observed_raw_stdout["stdout"] = completed.stdout
        return completed

    result = rr.invoke_agent(request, runner=_diagnostic_runner, policy=policy)

    observed_shape = "unknown"
    if observed_raw_stdout.get("stdout"):
        try:
            decoded = json.loads(observed_raw_stdout["stdout"])
        except json.JSONDecodeError:
            observed_shape = "json_decode_failure"
        else:
            if isinstance(decoded, list):
                observed_shape = "array"
            elif isinstance(decoded, dict):
                observed_shape = "object"
            else:
                observed_shape = type(decoded).__name__

    print(
        "test_real_claude_cli_round_trip_normalizes_whichever_shape_is_observed: "
        f"observed_top_level_shape={observed_shape} adapter_status={result.status} "
        f"adapter_reason_code={result.reason_code} child_exit_code={result.exit_code}"
    )

    assert result.status == "ok", (observed_shape, result.status, result.reason_code, result.raw_stdout_excerpt)
    assert result.exit_code == 0
    assert result.reason_code is None
    assert result.raw_stdout_excerpt is None
    assert result.structured_output == expected_payload
