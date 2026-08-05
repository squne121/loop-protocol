"""Issue #1979 AC8: bounded-retry ephemeralMessage prompt-compliance resolution.

Hermetic fake (AGY process mocked via `_invoke` monkeypatch) proving:
  - a capability is marked compliant as soon as its expected `PreToolUse`
    event is observed;
  - only still-pending capabilities are re-injected on retry rounds;
  - a capability that never complies within `MAX_PROMPT_COMPLIANCE_ATTEMPTS`
    (3) rounds is recorded `compliant: False`;
  - a launch failure (`OSError` from `_invoke`) aborts the retry loop
    immediately rather than burning the full retry budget;
  - the end-to-end `_run()` live path returns `EXIT_PROMPT_NONCOMPLIANT`(78)
    -- never the normal allow/deny verdict logic -- when any capability
    ends up non-compliant.
"""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).parents[1] / "scripts" / "run_agy_permission_boundary_e2e.py"
SPEC = importlib.util.spec_from_file_location("agy_permission_boundary_prompt_compliance_test", RUNNER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _invoke_result(returncode: int = 0) -> dict[str, object]:
    return {
        "returncode": returncode,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "process_group_isolated": True,
        "descendant_processes_absent": True,
    }


def _write_pretooluse_event(runtime: dict[str, object], capability: str) -> None:
    """Append a matching `pre_tool_use` event, as the real enforcement hook
    would via `agy_permission_boundary_hook/v1` schema events, but simplified
    to the parent's own `events.jsonl` shape `_resolve_prompt_compliance`
    also reads directly (kind == "pre_tool_use")."""
    tool_name, _ = MODULE.ATTEMPT_SPECS[capability]
    args_digest = MODULE._sha256(MODULE._canonical_json(runtime["attempt_args"][capability]))
    event = {
        "kind": "pre_tool_use",
        "tool_name": tool_name,
        "args_digest": args_digest,
        "run_id": runtime["run_id"],
        "canary_id": runtime["canary_id"],
    }
    with open(runtime["events_path"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def _read_injected_capabilities(runtime: dict[str, object]) -> set[str]:
    """Introspect the ephemeralMessage injection hook's currently-written
    injectSteps to see which capabilities' tool names it references."""
    content = Path(runtime["injection_hook_path"]).read_text(encoding="utf-8")
    return {
        capability
        for capability, (tool_name, _) in MODULE.ATTEMPT_SPECS.items()
        if f"`{tool_name}`" in content
    }


def test_ephemeral_message_prompt_names_the_exact_tool_and_arguments() -> None:
    message = MODULE._ephemeral_message_prompt("network", "read_url_content", {"Url": "http://127.0.0.1:1/x"})
    assert "read_url_content" in message
    assert '"Url":"http://127.0.0.1:1/x"' in message
    assert "network" in message


def test_capability_compliant_on_first_round_when_pretooluse_observed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "agy"
    runtime = MODULE._prepare_runtime(tmp_path / "runtime", "no_tools")
    calls = {"count": 0}

    def _fake_invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls["count"] += 1
        for capability in MODULE.CAPABILITIES:
            _write_pretooluse_event(runtime, capability)
        return _invoke_result(0)

    monkeypatch.setattr(MODULE, "_invoke", _fake_invoke)

    prompt_compliance, invoked, process_group_isolated, descendant_processes_absent = MODULE._resolve_prompt_compliance(
        fake, runtime, live=True
    )

    assert calls["count"] == 1
    assert invoked is not None
    assert process_group_isolated is True
    assert descendant_processes_absent is True
    assert set(prompt_compliance) == set(MODULE.CAPABILITIES)
    for record in prompt_compliance.values():
        assert record == {"attempts": 1, "compliant": True}


def test_retries_only_still_pending_capabilities_across_rounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "agy"
    runtime = MODULE._prepare_runtime(tmp_path / "runtime", "no_tools")
    calls = {"count": 0}
    injected_per_round: list[set[str]] = []

    def _fake_invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls["count"] += 1
        injected_per_round.append(_read_injected_capabilities(runtime))
        if calls["count"] == 1:
            _write_pretooluse_event(runtime, "command")
        elif calls["count"] == 2:
            _write_pretooluse_event(runtime, "write")
        elif calls["count"] == 3:
            _write_pretooluse_event(runtime, "read")
            _write_pretooluse_event(runtime, "network")
        return _invoke_result(0)

    monkeypatch.setattr(MODULE, "_invoke", _fake_invoke)

    prompt_compliance, _invoked, _pgi, _dpa = MODULE._resolve_prompt_compliance(fake, runtime, live=True)

    assert calls["count"] == 3
    assert prompt_compliance == {
        "command": {"attempts": 1, "compliant": True},
        "write": {"attempts": 2, "compliant": True},
        "read": {"attempts": 3, "compliant": True},
        "network": {"attempts": 3, "compliant": True},
    }
    # Round 1 injects all 4; round 2 injects the 3 still pending after round 1
    # (write/read/network); round 3 injects the 2 still pending after round 2
    # (read/network).  "command" is never re-injected after round 1.
    assert injected_per_round[0] == set(MODULE.CAPABILITIES)
    assert injected_per_round[1] == {"write", "read", "network"}
    assert injected_per_round[2] == {"read", "network"}


def test_capability_marked_noncompliant_after_exhausting_bounded_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "agy"
    runtime = MODULE._prepare_runtime(tmp_path / "runtime", "no_tools")
    calls = {"count": 0}

    def _fake_invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls["count"] += 1
        # Deliberately non-compliant: no matching PreToolUse event is ever
        # written for any capability, simulating an AGY agent that never
        # follows the ephemeralMessage instruction.
        return _invoke_result(0)

    monkeypatch.setattr(MODULE, "_invoke", _fake_invoke)

    prompt_compliance, invoked, _pgi, _dpa = MODULE._resolve_prompt_compliance(fake, runtime, live=True)

    assert calls["count"] == MODULE.MAX_PROMPT_COMPLIANCE_ATTEMPTS
    assert invoked is not None
    assert set(prompt_compliance) == set(MODULE.CAPABILITIES)
    for record in prompt_compliance.values():
        assert record == {"attempts": 3, "compliant": False}


def test_launch_failure_aborts_retry_loop_immediately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "agy"
    runtime = MODULE._prepare_runtime(tmp_path / "runtime", "no_tools")
    calls = {"count": 0}

    def _raising_invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls["count"] += 1
        raise OSError("agy binary vanished")

    monkeypatch.setattr(MODULE, "_invoke", _raising_invoke)

    prompt_compliance, invoked, _pgi, _dpa = MODULE._resolve_prompt_compliance(fake, runtime, live=True)

    assert calls["count"] == 1  # never burns the full retry budget on a launch failure
    assert invoked is None
    assert set(prompt_compliance) == set(MODULE.CAPABILITIES)


def test_run_returns_exit_prompt_noncompliant_when_any_capability_never_complies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end AC8: the live `_run()` path must route to
    `EXIT_PROMPT_NONCOMPLIANT`(78) -- never the normal allow/deny verdict --
    when a hermetic fake AGY process deliberately returns non-compliant
    responses (no matching `PreToolUse` for one capability)."""
    fake = tmp_path / "agy"
    fake.write_text("#!/usr/bin/env python3\nprint('should never run')\n", encoding="utf-8")
    fake.chmod(0o755)

    def _supported_gate() -> dict[str, object]:
        return {
            "bootstrap_predicate": "pre_invocation_ephemeral_message_injection",
            "predicate_kind": "bootstrap_prerequisite",
            "status": "supported",
            "reason_code": "test_override_supported",
            "evidence_source": "runtime_semantic_observation",
        }

    monkeypatch.setattr(MODULE, "_bootstrap_capability_gate", _supported_gate)
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: str(fake))
    monkeypatch.setattr(MODULE, "_version", lambda _path: ("agy 1.1.9", True))

    call_count = {"count": 0}

    def _fake_invoke(_executable: Path, runtime: dict[str, object], *, live: bool) -> dict[str, object]:
        call_count["count"] += 1
        # Every capability except "command" complies immediately; "command"
        # never does, regardless of round -- proving a single stubborn
        # capability is sufficient to route the whole run to
        # EXIT_PROMPT_NONCOMPLIANT rather than a partial/silent pass.
        for capability in ("write", "read", "network"):
            if call_count["count"] == 1:
                _write_pretooluse_event(runtime, capability)
        return _invoke_result(0)

    monkeypatch.setattr(MODULE, "_invoke", _fake_invoke)

    exit_code, artifact = MODULE._run(
        argparse.Namespace(mode="live", agy=None, allow_live=True, profile="no_tools", artifact_dir=tmp_path)
    )

    assert call_count["count"] == MODULE.MAX_PROMPT_COMPLIANCE_ATTEMPTS
    assert exit_code == MODULE.EXIT_PROMPT_NONCOMPLIANT
    assert artifact["runner"]["exit_code"] == MODULE.EXIT_PROMPT_NONCOMPLIANT
    assert artifact["failure_taxonomy"]["class"] == MODULE.FAILURE_PROMPT_NONCOMPLIANT
    assert artifact["failure_taxonomy"]["completion"] is False
    assert artifact["prompt_compliance"]["command"]["compliant"] is False
    for capability in ("write", "read", "network"):
        assert artifact["prompt_compliance"][capability]["compliant"] is True
    assert artifact["attempt_method"] == "ephemeral_message_prompt"
    assert MODULE.validate_artifact(artifact) == (True, "valid")
