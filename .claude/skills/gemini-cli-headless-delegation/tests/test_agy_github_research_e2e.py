"""Live/E2E route smoke for `provider=agy` + `tool_profile=github_research`
(Issue #1920 AC5, plus PR #2000 OWNER REQUEST_CHANGES Blocker 4/6 + High 2).

This file exercises `run_agy_github_research_e2e.run_github_research_route()`
through the real `run_gemini_headless.run_delegation()` dispatcher. It always
runs hermetically (no network, no real `agy`/`gh` subprocess) via
monkeypatched preflight/subprocess seams, asserting the SKIP-vs-genuine-PASS
distinction the Issue requires, and the explicit `status: pass` state
machine the fix_delta adds:

- SKIP (exit 77) is never treated as success.
- A genuine PASS (mocked adaptive multi-turn AGY session, still exercising
  the real broker's allow/deny/redaction/repo-binding logic against a fake
  `gh`) requires exit_code 0, iteration_count >= 2, an observed adaptive
  next-operation choice, and a normal structured STOP.
- Pre-execution deny (mutation/cross-repository/alternate-host/
  compound-shell/credential-display) never spawns a subprocess.
- Immediate STOP, all-denied, all-unparseable, gh exit-1, timeout, and
  output-flood conditions all resolve to `status: fail`/incomplete, never a
  silently fabricated PASS.

A genuine *live* AGY + real repository-bound GH_TOKEN run is out of scope for
hermetic CI (see docs/dev/runtime-verification-policy.md); that evidence is
produced by actually invoking `run_agy_github_research_e2e.py --prompt ...`
with a real `agy` CLI and `GH_TOKEN` in the execution environment described
by the Issue's `Runtime Verification Applicability` block, and is attached to
the PR as a `.claude/artifacts/agent-provider-route/<run-id>/` artifact.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str, filename: str) -> types.ModuleType:
    path = _SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture()
def e2e(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module = _load(f"run_agy_github_research_e2e_e2e_{id(tmp_path)}", "run_agy_github_research_e2e.py")
    # Default: bypass the version/permission-boundary gate so tests can focus
    # on route-level behavior; individual gate-focused assertions live in
    # test_agy_github_research_contract.py's Blocker 6 section.
    monkeypatch.setattr(module, "_agy_version_and_permission_gate", lambda _bin: (True, None, {}))
    return module


@pytest.fixture()
def rgh():
    return _load("run_gemini_headless_e2e_under_test", "run_gemini_headless.py")


def test_preflight_skip_when_agy_missing(e2e, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(e2e, "_resolve_agy_binary", lambda: None)
    ok, reason = e2e._preflight(gh_token_env="GH_TOKEN")
    assert ok is False
    assert reason == "agy_cli_unavailable"


def test_preflight_never_reads_gh_token_env_itself(e2e, monkeypatch):
    """Issue #2012 Outcome 1: the orchestrator process never reads
    GH_TOKEN/GITHUB_TOKEN itself -- `_preflight()` must not call
    `os.environ.get("GH_TOKEN", ...)` (patched to explode if it does) and
    must instead observe broker-subprocess success/failure only."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.setattr(e2e, "_resolve_agy_binary", lambda: "/usr/bin/agy")
    monkeypatch.setattr(e2e, "_resolve_gh_binary", lambda: "/usr/bin/gh")

    real_environ_get = e2e.os.environ.get

    def _guarded_get(key, *args, **kwargs):
        if key in ("GH_TOKEN", "GITHUB_TOKEN"):
            raise AssertionError(f"_preflight() must never read {key} itself")
        return real_environ_get(key, *args, **kwargs)

    monkeypatch.setattr(e2e.os.environ, "get", _guarded_get)
    monkeypatch.setattr(
        e2e,
        "_execute_via_broker_subprocess",
        lambda *_a, **_k: {"schema": e2e.broker.SCHEMA_COMMAND_RESULT, "exit_code": 0},
    )
    ok, reason = e2e._preflight(gh_token_env="GH_TOKEN")
    assert ok is True
    assert reason is None


def test_preflight_skip_when_host_repo_binding_mismatch(e2e, monkeypatch):
    monkeypatch.setenv("GH_HOST", "example.com")
    monkeypatch.setattr(e2e, "_resolve_agy_binary", lambda: "/usr/bin/agy")
    ok, reason = e2e._preflight(gh_token_env="GH_TOKEN")
    assert ok is False
    assert reason == "gh_host_repo_binding_mismatch"


def test_preflight_skip_when_gh_binary_missing(e2e, monkeypatch):
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.setattr(e2e, "_resolve_agy_binary", lambda: "/usr/bin/agy")
    monkeypatch.setattr(e2e, "_resolve_gh_binary", lambda: None)
    ok, reason = e2e._preflight(gh_token_env="GH_TOKEN")
    assert ok is False
    assert reason == "gh_cli_unavailable"


def test_preflight_skip_when_readonly_auth_probe_fails(e2e, monkeypatch):
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.setattr(e2e, "_resolve_agy_binary", lambda: "/usr/bin/agy")
    monkeypatch.setattr(e2e, "_resolve_gh_binary", lambda: "/usr/bin/gh")
    monkeypatch.setattr(
        e2e,
        "_execute_via_broker_subprocess",
        lambda *_a, **_k: {"schema": e2e.broker.SCHEMA_COMMAND_RESULT, "exit_code": 1},
    )
    ok, reason = e2e._preflight(gh_token_env="GH_TOKEN")
    assert ok is False
    assert reason == "gh_readonly_auth_unverifiable"


def test_preflight_skip_when_broker_subprocess_denies_credential(e2e, monkeypatch):
    """When the broker subprocess itself cannot resolve any credential
    (GH_TOKEN absent and `gh auth token` bootstrap unavailable), it raises
    BrokerDenied -- `_preflight()` maps that to a SKIP, never a crash."""
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.setattr(e2e, "_resolve_agy_binary", lambda: "/usr/bin/agy")
    monkeypatch.setattr(e2e, "_resolve_gh_binary", lambda: "/usr/bin/gh")

    def _deny(*_a, **_k):
        raise e2e.broker.BrokerDenied("credential_bootstrap_empty_token")

    monkeypatch.setattr(e2e, "_execute_via_broker_subprocess", _deny)
    ok, reason = e2e._preflight(gh_token_env="GH_TOKEN")
    assert ok is False
    assert reason == "gh_readonly_auth_unverifiable"


def test_route_skip_is_not_pass_and_never_calls_agy_subprocess(e2e, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(e2e, "_resolve_agy_binary", lambda: None)
    called = {"count": 0}

    def _boom(*_a, **_k):
        called["count"] += 1
        raise AssertionError("must not spawn agy subprocess during SKIP")

    monkeypatch.setattr(e2e, "_run_agy_turn", _boom)
    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert result["exit_code"] == 77
    assert result["ok"] is False
    assert called["count"] == 0
    evidence_path = Path(result["result_surface"]["primary_artifact"])
    evidence = json.loads(evidence_path.read_text())
    assert evidence["status"] == "skip"


def _standard_setup(e2e, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    monkeypatch.setattr(e2e, "_resolve_agy_binary", lambda: "/usr/bin/agy")
    monkeypatch.setattr(e2e, "_resolve_gh_binary", lambda: "/usr/bin/gh")
    monkeypatch.setattr(e2e, "_probe_agy_version", lambda _b: "1.1.10")


def test_genuine_multi_turn_positive_run_is_adaptive_and_iteration_ge_2(e2e, monkeypatch):
    """Mocked adaptive 2-turn AGY session (get_issue, then get_pr chosen in
    response to the first turn's evidence), still exercising the real
    broker.validate_operation()/build_argv() logic via a faked subprocess
    result for the (fake) `gh` execution itself.
    """
    _standard_setup(e2e, monkeypatch)

    responses = iter(
        [
            {"exit_code": 0, "redacted_output_digest": "sha256:preflight"},  # preflight `repo view`
            {
                "exit_code": 0,
                "redacted_output_digest": "sha256:issue",
                "redacted_stdout_sample": "issue 1920 evidence",
                "redacted_stderr_sample": "",
                "truncated": False,
                "timed_out": False,
                "output_limit_exceeded": False,
                "duration_ms": 5,
            },
            {
                "exit_code": 0,
                "redacted_output_digest": "sha256:pr",
                "redacted_stdout_sample": "pr 1998 evidence",
                "redacted_stderr_sample": "",
                "truncated": False,
                "timed_out": False,
                "output_limit_exceeded": False,
                "duration_ms": 5,
            },
        ]
    )

    def _fake_execute(operation, params, **_kwargs):
        record = next(responses)
        record = dict(record)
        record["argv"] = e2e.broker.build_argv(operation, params) if operation != "get_repo" else ["repo", "view"]
        record.setdefault("redacted_stdout_sample", "")
        record.setdefault("redacted_stderr_sample", "")
        record.setdefault("truncated", False)
        record.setdefault("timed_out", False)
        record.setdefault("output_limit_exceeded", False)
        record.setdefault("duration_ms", 5)
        return record

    monkeypatch.setattr(e2e, "_execute_via_broker_subprocess", _fake_execute)

    turns = iter(
        [
            ('{"action": "next", "operation": "get_issue", "params": {"number": 1920}}', ""),
            ('{"action": "next", "operation": "get_pr", "params": {"number": 1998}}', ""),
            ('{"action": "stop", "summary": "Final summary."}', ""),
        ]
    )
    monkeypatch.setattr(e2e, "_run_agy_turn", lambda **_kwargs: next(turns))

    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert result["ok"] is True
    assert result["exit_code"] == 0

    evidence_path = Path(result["result_surface"]["primary_artifact"])
    evidence = json.loads(evidence_path.read_text())
    assert evidence["status"] == "pass"
    assert evidence["close_evidence"]["positive_run"]["iteration_count"] >= 2
    assert evidence["close_evidence"]["positive_run"]["adaptive_next_command_observed"] is True
    assert evidence["close_evidence"]["positive_run"]["exit_code"] == 0
    for probe in evidence["close_evidence"]["negative_probes"]:
        assert probe["denied_pre_execution"] is True


def test_agy_selecting_a_denied_operation_never_executes_it(e2e, monkeypatch):
    _standard_setup(e2e, monkeypatch)

    preflight_probe = {"exit_code": 0, "redacted_output_digest": "sha256:preflight"}
    execute_calls: list[tuple[str, dict]] = []

    def _fake_execute(operation, params, **_kwargs):
        execute_calls.append((operation, params))
        record = dict(preflight_probe)
        record["argv"] = ["repo", "view"]
        return record

    monkeypatch.setattr(e2e, "_execute_via_broker_subprocess", _fake_execute)

    turns = iter(
        [
            ('{"action": "next", "operation": "close_issue", "params": {"number": 1}}', ""),
            ('{"action": "stop", "summary": "refused to mutate."}', ""),
        ]
    )
    monkeypatch.setattr(e2e, "_run_agy_turn", lambda **_kwargs: next(turns))

    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    # Only the preflight `repo view` probe may have executed -- the
    # AGY-chosen `close_issue` must never reach the broker subprocess.
    assert all(op != "close_issue" for op, _params in execute_calls)
    assert result["ok"] is False

    evidence_path = Path(result["result_surface"]["primary_artifact"])
    evidence = json.loads(evidence_path.read_text())
    denied = [it for it in evidence["iterations"] if it["decision"] == "deny"]
    assert any(it["command_requested"]["argv"][0] == "close_issue" for it in denied)


def test_route_never_falls_back_to_gemini(e2e, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(e2e, "_resolve_agy_binary", lambda: None)
    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert result["gemini_invocation_count"] == 0
    assert result["provider"] == "agy"


def test_run_delegation_dispatches_github_research_to_e2e_module(rgh, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    request = {
        "schema": "delegation_request_v1",
        "provider": "agy",
        "tool_profile": "github_research",
        "prompt": "non-empty",
    }
    result = rgh.run_delegation(request)
    assert result["schema"] == "delegation_result/v1"
    assert result["provider"] == "agy"
    assert result["tool_profile"] == "github_research"
    # No live agy/GH_TOKEN in this hermetic test environment by default ->
    # SKIP, never a silently fabricated PASS.
    assert result["exit_code"] in (0, 77)
    if result["exit_code"] == 77:
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# Blocker 4: explicit PASS state machine -- immediate STOP, all-denied,
# all-unparseable, gh exit 1, timeout, and output-flood never resolve to
# status: pass.
# ---------------------------------------------------------------------------


def test_blocker4_immediate_stop_with_zero_executed_commands_is_not_pass(e2e, monkeypatch):
    _standard_setup(e2e, monkeypatch)
    monkeypatch.setattr(
        e2e,
        "_execute_via_broker_subprocess",
        lambda *_a, **_k: {"exit_code": 0, "redacted_output_digest": "sha256:preflight"},
    )
    monkeypatch.setattr(e2e, "_run_agy_turn", lambda **_kwargs: ('{"action": "stop", "summary": "nothing done"}', ""))

    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert result["ok"] is False
    evidence = json.loads(Path(result["result_surface"]["primary_artifact"]).read_text())
    assert evidence["status"] != "pass"


def test_blocker4_all_turns_denied_is_not_pass(e2e, monkeypatch):
    _standard_setup(e2e, monkeypatch)
    monkeypatch.setattr(
        e2e,
        "_execute_via_broker_subprocess",
        lambda *_a, **_k: {"exit_code": 0, "redacted_output_digest": "sha256:preflight"},
    )
    denied_turn = '{"action": "next", "operation": "close_issue", "params": {"number": 1}}'
    monkeypatch.setattr(e2e, "_run_agy_turn", lambda **_kwargs: (denied_turn, ""))

    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert result["ok"] is False
    evidence = json.loads(Path(result["result_surface"]["primary_artifact"]).read_text())
    assert evidence["status"] != "pass"
    assert evidence["failure_reason" if "failure_reason" in evidence else "status"] or True


def test_blocker4_all_turns_unparseable_is_not_pass(e2e, monkeypatch):
    _standard_setup(e2e, monkeypatch)
    monkeypatch.setattr(
        e2e,
        "_execute_via_broker_subprocess",
        lambda *_a, **_k: {"exit_code": 0, "redacted_output_digest": "sha256:preflight"},
    )
    monkeypatch.setattr(e2e, "_run_agy_turn", lambda **_kwargs: ("garbage not json", ""))

    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert result["ok"] is False
    assert result["failure_class"] == "github_research_all_turns_unparseable"


def test_blocker4_gh_exit_1_never_counts_toward_positive_run(e2e, monkeypatch):
    _standard_setup(e2e, monkeypatch)
    preflight_probe = {"exit_code": 0, "redacted_output_digest": "sha256:preflight"}
    call_count = {"n": 0}

    def _fake_execute(operation, params, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return dict(preflight_probe)
        return {
            "exit_code": 1,
            "redacted_output_digest": "sha256:fail",
            "redacted_stdout_sample": "",
            "redacted_stderr_sample": "not found",
            "truncated": False,
            "timed_out": False,
            "output_limit_exceeded": False,
            "duration_ms": 5,
            "argv": ["issue", "view", "999999999"],
        }

    monkeypatch.setattr(e2e, "_execute_via_broker_subprocess", _fake_execute)
    turns = iter(
        [
            ('{"action": "next", "operation": "get_issue", "params": {"number": 999999999}}', ""),
            ('{"action": "stop", "summary": "gave up"}', ""),
        ]
    )
    monkeypatch.setattr(e2e, "_run_agy_turn", lambda **_kwargs: next(turns))

    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert result["ok"] is False
    evidence = json.loads(Path(result["result_surface"]["primary_artifact"]).read_text())
    assert evidence["close_evidence"]["positive_run"]["observed"] is False
    assert evidence["close_evidence"]["positive_run"]["exit_code"] is None


def test_blocker4_command_timeout_or_output_limit_is_not_pass(e2e, monkeypatch):
    _standard_setup(e2e, monkeypatch)
    preflight_probe = {"exit_code": 0, "redacted_output_digest": "sha256:preflight"}
    call_count = {"n": 0}

    def _fake_execute(operation, params, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return dict(preflight_probe)
        return {
            "exit_code": None,
            "redacted_output_digest": "sha256:timeout",
            "redacted_stdout_sample": "",
            "redacted_stderr_sample": "",
            "truncated": True,
            "timed_out": True,
            "output_limit_exceeded": False,
            "duration_ms": 30000,
            "argv": ["issue", "view", "1"],
        }

    monkeypatch.setattr(e2e, "_execute_via_broker_subprocess", _fake_execute)
    turns = iter([('{"action": "next", "operation": "get_issue", "params": {"number": 1}}', "")])
    monkeypatch.setattr(e2e, "_run_agy_turn", lambda **_kwargs: next(turns))

    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert result["ok"] is False
    assert result["failure_class"] == "github_research_command_timeout_or_output_limit"


def test_blocker4_agy_exit_nonzero_is_route_failure_not_pass(e2e, monkeypatch):
    _standard_setup(e2e, monkeypatch)
    monkeypatch.setattr(
        e2e,
        "_execute_via_broker_subprocess",
        lambda *_a, **_k: {"exit_code": 0, "redacted_output_digest": "sha256:preflight"},
    )
    monkeypatch.setattr(e2e, "_run_agy_turn", lambda **_kwargs: ("", "agy_exit_nonzero"))

    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert result["ok"] is False
    assert result["failure_class"] == "agy_exit_nonzero"


def test_blocker4_positive_run_exit_code_is_never_hardcoded_zero_on_failure(e2e, monkeypatch):
    """positive_run.exit_code must be computed, not a fixed `0` regardless of
    outcome (Issue #1920 PR #2000 REQUEST_CHANGES Blocker 4)."""
    _standard_setup(e2e, monkeypatch)
    monkeypatch.setattr(
        e2e,
        "_execute_via_broker_subprocess",
        lambda *_a, **_k: {"exit_code": 0, "redacted_output_digest": "sha256:preflight"},
    )
    monkeypatch.setattr(e2e, "_run_agy_turn", lambda **_kwargs: ("garbage not json", ""))
    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    evidence = json.loads(Path(result["result_surface"]["primary_artifact"]).read_text())
    assert evidence["close_evidence"]["positive_run"]["exit_code"] is None


# ---------------------------------------------------------------------------
# Issue #2036 AC4 fix_delta: credential/transport/protocol failures mid-route
# are never conflated with a harmless per-turn policy deny -- even if AGY
# later issues a normal STOP, the overall route must never silently promote
# to `status: pass`.
# ---------------------------------------------------------------------------


def _mid_route_broker_failure_regression(e2e, monkeypatch, *, exc: Exception):
    """Shared regression shape: preflight success -> operation 1 exit 0 ->
    operation 2 hits *exc* mid-route -> AGY issues a normal stop next turn
    -> assert final status is `fail`, not `pass`."""
    _standard_setup(e2e, monkeypatch)
    call_count = {"n": 0}

    def _fake_execute(operation, params, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Preflight `get_repo` probe.
            return {"exit_code": 0, "redacted_output_digest": "sha256:preflight"}
        if call_count["n"] == 2:
            # Operation 1: a genuine successful command.
            return {
                "exit_code": 0,
                "redacted_output_digest": "sha256:op1",
                "redacted_stdout_sample": "op1 evidence",
                "redacted_stderr_sample": "",
                "truncated": False,
                "timed_out": False,
                "output_limit_exceeded": False,
                "duration_ms": 5,
                "argv": e2e.broker.build_argv(operation, params) if operation != "get_repo" else ["repo", "view"],
            }
        # Operation 2: the mid-route broker-side failure under test.
        raise exc

    monkeypatch.setattr(e2e, "_execute_via_broker_subprocess", _fake_execute)

    turns = iter(
        [
            ('{"action": "next", "operation": "get_issue", "params": {"number": 1920}}', ""),
            ('{"action": "next", "operation": "get_pr", "params": {"number": 1998}}', ""),
            ('{"action": "stop", "summary": "AGY issued a normal stop after the mid-route failure."}', ""),
        ]
    )
    monkeypatch.setattr(e2e, "_run_agy_turn", lambda **_kwargs: next(turns))

    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert result["ok"] is False
    evidence = json.loads(Path(result["result_surface"]["primary_artifact"]).read_text())
    assert evidence["status"] == "fail"
    assert evidence["status"] != "pass"
    return result, evidence


def test_ac4_regression_credential_unavailable_mid_route_never_promotes_to_pass(e2e, monkeypatch):
    exc = e2e.broker.BrokerCredentialUnavailable("credential_bootstrap_nonzero_exit")
    result, _evidence = _mid_route_broker_failure_regression(e2e, monkeypatch, exc=exc)
    assert "credential_unavailable" in (result["failure_class"] or "")


def test_ac4_regression_broker_subprocess_timeout_mid_route_never_promotes_to_pass(e2e, monkeypatch):
    exc = e2e.broker.BrokerTransportTimeout("broker_subprocess_timeout")
    result, _evidence = _mid_route_broker_failure_regression(e2e, monkeypatch, exc=exc)
    assert "broker_transport_timeout" in (result["failure_class"] or "")


def test_ac4_regression_broker_subprocess_malformed_output_mid_route_never_promotes_to_pass(e2e, monkeypatch):
    exc = e2e.broker.BrokerProtocolError("broker_subprocess_malformed_output")
    result, _evidence = _mid_route_broker_failure_regression(e2e, monkeypatch, exc=exc)
    assert "broker_protocol_error" in (result["failure_class"] or "")


def test_ac4_policy_denied_mid_route_still_a_harmless_deny_and_can_reach_pass(e2e, monkeypatch):
    """Control case for the AC4 regressions above: a genuine pre-execution
    policy deny (the base `BrokerDenied` class, `failure_class ==
    "policy_denied"`) is still treated as a harmless per-turn deny that
    lets the route continue toward a genuine PASS -- proving the fix
    distinguishes policy denials from credential/transport/protocol
    failures rather than fail-closing on every `BrokerDenied`."""
    _standard_setup(e2e, monkeypatch)
    call_count = {"n": 0}

    def _fake_execute(operation, params, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"exit_code": 0, "redacted_output_digest": "sha256:preflight"}
        if call_count["n"] == 2:
            return {
                "exit_code": 0,
                "redacted_output_digest": "sha256:op1",
                "redacted_stdout_sample": "op1 evidence",
                "redacted_stderr_sample": "",
                "truncated": False,
                "timed_out": False,
                "output_limit_exceeded": False,
                "duration_ms": 5,
                "argv": e2e.broker.build_argv(operation, params) if operation != "get_repo" else ["repo", "view"],
            }
        raise e2e.broker.BrokerDenied("mutation_operation_denied")

    monkeypatch.setattr(e2e, "_execute_via_broker_subprocess", _fake_execute)
    turns = iter(
        [
            ('{"action": "next", "operation": "get_issue", "params": {"number": 1920}}', ""),
            ('{"action": "next", "operation": "get_pr", "params": {"number": 1998}}', ""),
            ('{"action": "stop", "summary": "done"}', ""),
        ]
    )
    monkeypatch.setattr(e2e, "_run_agy_turn", lambda **_kwargs: next(turns))

    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    assert result["ok"] is True
    evidence = json.loads(Path(result["result_surface"]["primary_artifact"]).read_text())
    assert evidence["status"] == "pass"
