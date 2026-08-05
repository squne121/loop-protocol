"""Live/E2E route smoke for `provider=agy` + `tool_profile=github_research`
(Issue #1920 AC5).

This file exercises `run_agy_github_research_e2e.run_github_research_route()`
through the real `run_gemini_headless.run_delegation()` dispatcher. It always
runs hermetically (no network, no real `agy`/`gh` subprocess) via
monkeypatched preflight/subprocess seams, asserting the SKIP-vs-genuine-PASS
distinction the Issue requires:

- SKIP (exit 77) is never treated as success.
- A genuine PASS (mocked adaptive multi-turn AGY session, still exercising
  the real broker's allow/deny/redaction/repo-binding logic against a fake
  `gh`) requires exit_code 0, iteration_count >= 2, and an observed adaptive
  next-command choice.
- Pre-execution deny (mutation/cross-repository/alternate-host/
  compound-shell/credential-display) never spawns a subprocess.

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
    return _load(f"run_agy_github_research_e2e_e2e_{id(tmp_path)}", "run_agy_github_research_e2e.py")


@pytest.fixture()
def rgh():
    return _load("run_gemini_headless_e2e_under_test", "run_gemini_headless.py")


def _fake_execute_gh_command_factory(e2e_module, script_by_argv):
    def _fake(argv, **kwargs):
        key = tuple(argv)
        if key not in script_by_argv:
            raise AssertionError(f"unexpected gh argv in fake broker: {argv}")
        return script_by_argv[key]

    return _fake


def test_preflight_skip_when_agy_missing(e2e, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(e2e.shutil, "which", lambda _b: None)
    ok, reason, token = e2e._preflight(gh_token_env="GH_TOKEN")
    assert ok is False
    assert reason == "agy_cli_unavailable"
    assert token is None


def test_preflight_skip_when_gh_token_missing(e2e, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(e2e.shutil, "which", lambda _b: "/usr/bin/agy")
    ok, reason, _token = e2e._preflight(gh_token_env="GH_TOKEN")
    assert ok is False
    assert reason == "gh_token_unavailable"


def test_preflight_skip_when_host_repo_binding_mismatch(e2e, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fake")
    monkeypatch.setenv("GH_HOST", "example.com")
    monkeypatch.setattr(e2e.shutil, "which", lambda _b: "/usr/bin/agy")
    ok, reason, _token = e2e._preflight(gh_token_env="GH_TOKEN")
    assert ok is False
    assert reason == "gh_host_repo_binding_mismatch"


def test_preflight_skip_when_readonly_auth_probe_fails(e2e, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fake")
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.setattr(e2e.shutil, "which", lambda _b: "/usr/bin/agy")
    monkeypatch.setattr(
        e2e.broker,
        "execute_gh_command",
        lambda *_a, **_k: {"exit_code": 1, "redacted_output_digest": "sha256:x"},
    )
    ok, reason, _token = e2e._preflight(gh_token_env="GH_TOKEN")
    assert ok is False
    assert reason == "gh_readonly_auth_unverifiable"


def test_route_skip_is_not_pass_and_never_calls_agy_subprocess(e2e, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(e2e.shutil, "which", lambda _b: None)
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


def test_genuine_multi_turn_positive_run_is_adaptive_and_iteration_ge_2(e2e, monkeypatch):
    """Mocked adaptive 2-turn AGY session (issue view, then pr view chosen in
    response to the first turn's evidence), still exercising the real
    broker.validate_gh_argv()/_force_repo_binding() logic via a faked
    subprocess result for the (fake) `gh` execution itself.
    """
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    monkeypatch.setattr(e2e.shutil, "which", lambda _b: "/usr/bin/agy")
    monkeypatch.setattr(e2e, "_probe_agy_version", lambda _b: "1.1.10")

    # Preflight's own readonly probe + the 2 real turns.
    responses = iter(
        [
            {"exit_code": 0, "redacted_output_digest": "sha256:preflight"},  # preflight `repo view`
            {
                "exit_code": 0,
                "redacted_output_digest": "sha256:issue",
                "redacted_stdout_sample": "issue 1920 evidence",
                "redacted_stderr_sample": "",
                "truncated": False,
                "duration_ms": 5,
            },
            {
                "exit_code": 0,
                "redacted_output_digest": "sha256:pr",
                "redacted_stdout_sample": "pr 1998 evidence",
                "redacted_stderr_sample": "",
                "truncated": False,
                "duration_ms": 5,
            },
        ]
    )

    def _fake_execute(argv, **_kwargs):
        record = next(responses)
        record = dict(record)
        record["argv"] = argv
        record.setdefault("redacted_stdout_sample", "")
        record.setdefault("redacted_stderr_sample", "")
        record.setdefault("truncated", False)
        record.setdefault("duration_ms", 5)
        return record

    monkeypatch.setattr(e2e.broker, "execute_gh_command", _fake_execute)

    turns = iter(
        [
            ('NEXT_COMMAND: {"argv": ["issue", "view", "1920"]}', ""),
            ('NEXT_COMMAND: {"argv": ["pr", "view", "1998"]}', ""),
            ("STOP\nFinal summary.", ""),
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


def test_agy_selecting_a_denied_command_never_executes_it(e2e, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    monkeypatch.setattr(e2e.shutil, "which", lambda _b: "/usr/bin/agy")
    monkeypatch.setattr(e2e, "_probe_agy_version", lambda _b: "1.1.10")

    preflight_probe = {"exit_code": 0, "redacted_output_digest": "sha256:preflight"}
    execute_calls: list[list[str]] = []

    def _fake_execute(argv, **_kwargs):
        execute_calls.append(argv)
        return dict(preflight_probe)

    monkeypatch.setattr(e2e.broker, "execute_gh_command", _fake_execute)

    turns = iter(
        [
            ('NEXT_COMMAND: {"argv": ["issue", "close", "1"]}', ""),
            ("STOP\nrefused to mutate.", ""),
        ]
    )
    monkeypatch.setattr(e2e, "_run_agy_turn", lambda **_kwargs: next(turns))

    result = e2e.run_github_research_route(
        {"schema": "delegation_request_v1", "provider": "agy", "tool_profile": "github_research", "prompt": "x"}
    )
    # Only the preflight `repo view` probe may have executed -- the AGY-chosen
    # `issue close` must never reach broker.execute_gh_command().
    assert all(call != ["issue", "close", "1"] for call in execute_calls)

    evidence_path = Path(result["result_surface"]["primary_artifact"])
    evidence = json.loads(evidence_path.read_text())
    denied = [it for it in evidence["iterations"] if it["decision"] == "deny"]
    assert any(it["command_requested"]["argv"] == ["issue", "close", "1"] for it in denied)


def test_route_never_falls_back_to_gemini(e2e, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(e2e.shutil, "which", lambda _b: None)
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
