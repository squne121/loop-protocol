"""Issue #1979 AC2: live runner binds to preflight_agy.py's bootstrap gate."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).parents[1] / "scripts" / "run_agy_permission_boundary_e2e.py"
SPEC = importlib.util.spec_from_file_location("agy_permission_boundary_runner_binding", RUNNER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _fake_agy(path: Path) -> None:
    path.write_text("#!/usr/bin/env python3\nprint('should never run')\n", encoding="utf-8")
    path.chmod(0o755)


def test_bootstrap_capability_gate_reflects_hardcoded_upstream_728_block() -> None:
    """As of Issue #1979, upstream #728 keeps the bootstrap predicate
    hardcoded `unsupported` in preflight_agy.py -- the runner's gate function
    must faithfully reflect that (hermetic fake, no real `agy` invoked).
    """
    gate = MODULE._bootstrap_capability_gate()
    assert gate["bootstrap_predicate"] == "pre_invocation_injected_tool_call"
    assert gate["predicate_kind"] == "bootstrap_prerequisite"
    assert gate["status"] == "unsupported"
    assert gate["reason_code"] == "upstream_known_runtime_rejection"


def test_live_allow_live_never_invokes_agy_when_bootstrap_predicate_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "agy"
    _fake_agy(fake)
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: str(fake))
    monkeypatch.setattr(
        MODULE,
        "_invoke",
        lambda *_args, **_kwargs: pytest.fail("capability gate must stop before AGY invocation"),
    )

    exit_code, artifact = MODULE._run(
        argparse.Namespace(mode="live", agy=None, allow_live=True, profile="no_tools", artifact_dir=tmp_path)
    )

    assert exit_code == MODULE.EXIT_UNAVAILABLE
    assert artifact["runner"]["actual_agy_executed"] is False
    assert artifact["capability_gate"]["status"] == "unsupported"
    assert artifact["capability_gate"]["bootstrap_predicate"] == "pre_invocation_injected_tool_call"
    assert artifact["failure_taxonomy"]["class"] == MODULE.FAILURE_UNAVAILABLE
    assert MODULE.validate_artifact(artifact) == (True, "valid")


def test_live_proceeds_past_gate_when_bootstrap_predicate_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermetic fake proving the gate is a real branch, not dead code: when the
    bootstrap predicate reports `supported`, the runner proceeds to the
    agy-discovery step instead of short-circuiting to EXIT_UNAVAILABLE.
    """

    def _supported_gate() -> dict[str, object]:
        return {
            "bootstrap_predicate": "pre_invocation_injected_tool_call",
            "predicate_kind": "bootstrap_prerequisite",
            "status": "supported",
            "reason_code": "hermetic_test_override",
            "evidence_source": "runtime_semantic_observation",
        }

    monkeypatch.setattr(MODULE, "_bootstrap_capability_gate", _supported_gate)
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: None)

    exit_code, artifact = MODULE._run(
        argparse.Namespace(mode="live", agy=None, allow_live=True, profile="no_tools", artifact_dir=tmp_path)
    )

    # No `agy` discovered -> still unavailable, but for a *different* reason
    # than the capability gate -- proving the gate was passed, not skipped.
    assert exit_code == MODULE.EXIT_UNAVAILABLE
    assert artifact["capability_gate"]["status"] == "supported"


def test_hermetic_mode_never_gated_by_bootstrap_predicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hermetic lane (mode=hermetic) proves hook dispatch only and must
    never be short-circuited by the live-only capability gate.
    """
    fake = tmp_path / "agy"
    _fake_agy(fake)
    called = {"invoked": False}

    def _record_invoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        called["invoked"] = True
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "process_group_isolated": True,
            "descendant_processes_absent": True,
        }

    monkeypatch.setattr(MODULE, "_invoke", _record_invoke)
    MODULE._run(
        argparse.Namespace(mode="hermetic", agy=str(fake), allow_live=False, profile="no_tools", artifact_dir=tmp_path)
    )
    assert called["invoked"] is True
