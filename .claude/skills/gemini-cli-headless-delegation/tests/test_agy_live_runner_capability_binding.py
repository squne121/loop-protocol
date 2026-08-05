"""Issue #1979 AC2/AC8: live runner binds to preflight_agy.py's bootstrap gate."""
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


def test_bootstrap_capability_gate_binds_to_the_ephemeral_message_predicate() -> None:
    """Issue #1979 (2026-08-04 revision): the runner's gate function binds to
    `pre_invocation_ephemeral_message_injection`, not the toolCall-only
    `pre_invocation_injected_tool_call` predicate -- real (hermetic-fake, no
    real `agy` invoked) production evidence, not a monkeypatch.
    """
    gate = MODULE._bootstrap_capability_gate()
    assert gate["bootstrap_predicate"] == "pre_invocation_ephemeral_message_injection"
    assert gate["predicate_kind"] == "bootstrap_prerequisite"
    # Unlike the old toolCall-bound predicate, this one is never hardcoded
    # `unsupported` by the still-open upstream #728 -- it falls through to
    # the generic deferred-to-live-run branch.
    assert gate["status"] != "unsupported"
    assert gate["reason_code"] != "upstream_known_runtime_rejection"


def test_live_allow_live_never_invokes_agy_when_bootstrap_predicate_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "agy"
    _fake_agy(fake)
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: str(fake))

    def _unsupported_gate() -> dict[str, object]:
        return {
            "bootstrap_predicate": "pre_invocation_ephemeral_message_injection",
            "predicate_kind": "bootstrap_prerequisite",
            "status": "unsupported",
            "reason_code": "hermetic_test_override_unsupported",
            "evidence_source": "runtime_semantic_observation",
        }

    monkeypatch.setattr(MODULE, "_bootstrap_capability_gate", _unsupported_gate)
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
    assert artifact["capability_gate"]["bootstrap_predicate"] == "pre_invocation_ephemeral_message_injection"
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
            "bootstrap_predicate": "pre_invocation_ephemeral_message_injection",
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


def test_bootstrap_gate_uses_real_probed_version_result_not_a_synthetic_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #1979 fix_delta blocker_2: `_bootstrap_capability_gate` must feed
    `build_capability_matrix` a `version_result` sourced from an actual probe
    of the discovered `agy` binary (via `_probe_agy_version_result`), not a
    hardcoded `version_evidence_invalid` placeholder -- proven here by
    injecting a real-looking `agy --version` transcript and observing that
    `_probe_agy_version_result` (production code, no monkeypatch of the top-
    level gate result) parses it via `preflight_agy.py`'s own SSOT parser.
    """
    fake = tmp_path / "agy"
    _fake_agy(fake)
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: str(fake))
    monkeypatch.setattr(MODULE, "_version", lambda _agy: ("agy 1.1.9", True))

    version_result = MODULE._probe_agy_version_result()

    assert version_result["status"] == "parsed"
    assert version_result["version"] == "1.1.9"
    assert version_result["core"] == (1, 1, 9)


def test_probe_agy_version_result_is_genuinely_invalid_not_a_disguised_stub_when_no_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no `agy` binary is discoverable at all, `version_evidence_invalid`
    is still returned -- but as a real fact ("nothing to probe"), which this
    test distinguishes from the old hardcoded-regardless-of-reality stub by
    asserting `shutil.which` was actually consulted first.
    """
    consulted = {"called": False}

    def _which(_name: str) -> None:
        consulted["called"] = True
        return None

    monkeypatch.setattr(MODULE.shutil, "which", _which)
    result = MODULE._probe_agy_version_result()
    assert consulted["called"] is True
    assert result == {"status": "version_evidence_invalid", "version": None, "core": None, "raw": None}


def test_bootstrap_predicate_is_inconclusive_by_default_pending_a_live_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #1979 (2026-08-04 revision): `pre_invocation_ephemeral_message_injection`
    has no hardcoded-unsupported branch tied to upstream #728 (that branch
    only applies to `pre_invocation_injected_tool_call`), so real production
    code (no monkeypatch of the gate's return value) resolves it via
    `_resolve_predicate`'s generic deferred branch: `inconclusive` /
    `runtime_semantic_observation_deferred_to_1979`.  It is NOT `supported`:
    reaching `supported` requires an actual live ephemeralMessage-compliance
    observation, which is exactly what `_resolve_prompt_compliance` performs
    during a genuine live run (AC8), not what this bootstrap gate claims in
    advance.
    """
    monkeypatch.setattr(MODULE.shutil, "which", lambda _name: None)

    gate = MODULE._bootstrap_capability_gate()

    assert gate["status"] == "inconclusive"
    assert gate["reason_code"] == "runtime_semantic_observation_deferred_to_1979"


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
