#!/usr/bin/env python3
"""Regression tests for Issue #2445 AC1/AC2.

`DelegatedAgentPermissionPolicy.sanitize_subprocess_env()` and its
fallback-path counterpart `_default_sanitized_env()` (both in
`run_retrospective.py`) were replaced from an allowlist-based design
(`PATH`/`HOME`/`LANG`/`LC_ALL`/`TZ` plus an `AGENT_RETROSPECTIVE_`-prefixed
carve-out) to a denylist-based one that inherits the parent environment by
default and strips only `_MUTATION_CREDENTIAL_ENV_VARS` -- mirroring the
sibling `plugins/agent-retrospective/skills/run/scripts/run_retrospective.py`
implementation (Issue #2436 Background; OWNER reframe-in-place,
https://github.com/squne121/loop-protocol/issues/2445#issuecomment-5468276152).

This restores continuity for Claude-GPT proxy provider-transport /
model-selection / other non-mutation Claude runtime env vars through the
production nested-invocation path (`invoke_agent()`), which the prior
allowlist silently dropped.

Runtime Verification Applicability: `not_applicable` for these two ACs
(fixture/mock-based only, matching the module's existing test suite
convention -- see `test_run_retrospective.py`'s own docstring). AC3's own
live verification lives in `verify_claude_gpt_transport_passthrough.sh`, a
separate file, not exercised here.

  AC1 test_sanitize_subprocess_env_denylist_semantics_replaces_allowlist
  AC2 test_default_sanitized_env_regression_matches_sanitize_subprocess_env
      (plus the other tests below, all covering the same AC2 regression
      requirement from both the policy method and its module-level fallback)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402

#: AC2's literal list of non-mutation env vars that must now reach the real
#: nested subprocess env -- provider transport / model selection / other
#: Claude Code runtime vars the prior allowlist silently dropped (Issue
#: #2436 Background).
_AC2_NON_MUTATION_ENV_VARS = {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:41888",
    "ANTHROPIC_AUTH_TOKEN": "claude-gpt-local",
    "ANTHROPIC_MODEL": "gpt-5.6-terra[1m]",
    "CLAUDE_CONFIG_DIR": "/home/example/.claude-gpt/claude",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "150000",
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}

_MUTATION_ENV_VARS = {name: "sentinel-secret-value" for name in rr._MUTATION_CREDENTIAL_ENV_VARS}


def _build_env() -> dict[str, str]:
    env: dict[str, str] = {"PATH": "/usr/bin", "HOME": "/home/example"}
    env.update(_AC2_NON_MUTATION_ENV_VARS)
    env.update(_MUTATION_ENV_VARS)
    env["RANDOM_UNRELATED_VAR"] = "still-passes-through-under-denylist-semantics"
    return env


# ---------------------------------------------------------------------------
# AC1: sanitize_subprocess_env() denylist semantics
# ---------------------------------------------------------------------------


def test_sanitize_subprocess_env_denylist_semantics_replaces_allowlist() -> None:
    policy = rr.DelegatedAgentPermissionPolicy(run_id="issue-2445-run-1")
    env = _build_env()
    sanitized = policy.sanitize_subprocess_env(env)

    # every AC2-listed non-mutation env var passes through unchanged
    for key, value in _AC2_NON_MUTATION_ENV_VARS.items():
        assert sanitized.get(key) == value, f"{key} did not pass through sanitize_subprocess_env()"

    # a wholly arbitrary, non-allowlisted, non-mutation var also passes
    # through now (the defining behavior change from allowlist to denylist)
    assert sanitized.get("RANDOM_UNRELATED_VAR") == env["RANDOM_UNRELATED_VAR"]

    # PATH/HOME still pass through (never regressed by the semantics swap)
    assert sanitized["PATH"] == "/usr/bin"
    assert sanitized["HOME"] == "/home/example"

    # every mutation credential is still unconditionally excluded
    assert rr._MUTATION_CREDENTIAL_ENV_VARS.isdisjoint(sanitized.keys())


def test_sanitize_subprocess_env_no_new_claude_gpt_opt_in_flag() -> None:
    """AC1 explicitly forbids introducing a new claude-gpt-specific opt-in
    marker (e.g. a `CLAUDE_GPT_MODE`-equivalent) -- the fix must be a pure
    semantics replacement of the existing sanitizer, not an additional
    conditional gate. A denylist-based sanitizer that passes everything
    through except mutation credentials has no such gate to bypass in the
    first place: an unset/absent marker-like env var must pass through
    identically to a present one (no branch keyed on its presence)."""
    policy = rr.DelegatedAgentPermissionPolicy(run_id="issue-2445-run-2")
    base_env = {"PATH": "/usr/bin", "ANTHROPIC_BASE_URL": "http://127.0.0.1:41888"}
    with_marker = dict(base_env, CLAUDE_GPT_MODE="1")
    without_marker = dict(base_env)

    sanitized_with = policy.sanitize_subprocess_env(with_marker)
    sanitized_without = policy.sanitize_subprocess_env(without_marker)

    assert sanitized_with["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:41888"
    assert sanitized_without["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:41888"


# ---------------------------------------------------------------------------
# AC2: regression coverage for BOTH sanitize_subprocess_env() (policy path)
# and _default_sanitized_env() (policy=None fallback path) -- same behavior
# ---------------------------------------------------------------------------


def test_default_sanitized_env_regression_matches_sanitize_subprocess_env() -> None:
    env = _build_env()
    sanitized = rr._default_sanitized_env(env)

    for key, value in _AC2_NON_MUTATION_ENV_VARS.items():
        assert sanitized.get(key) == value, f"{key} did not pass through _default_sanitized_env()"
    assert sanitized.get("RANDOM_UNRELATED_VAR") == env["RANDOM_UNRELATED_VAR"]
    assert rr._MUTATION_CREDENTIAL_ENV_VARS.isdisjoint(sanitized.keys())

    # both entry points must agree byte-for-byte on this fixture (Issue
    # #2445 AC1: "sanitize_subprocess_env() だけを直して fallback を取り残さない")
    policy = rr.DelegatedAgentPermissionPolicy(run_id="issue-2445-run-3")
    assert sanitized == policy.sanitize_subprocess_env(env)


@pytest.mark.parametrize("mutation_var", sorted(rr._MUTATION_CREDENTIAL_ENV_VARS))
def test_default_sanitized_env_strips_each_mutation_credential_individually(mutation_var: str) -> None:
    env = {"PATH": "/usr/bin", mutation_var: "sentinel-secret-value", "ANTHROPIC_MODEL": "gpt-5.6-terra[1m]"}
    sanitized = rr._default_sanitized_env(env)
    assert mutation_var not in sanitized
    assert sanitized["ANTHROPIC_MODEL"] == "gpt-5.6-terra[1m]"


def test_sanitize_subprocess_env_strips_each_mutation_credential_individually_via_policy() -> None:
    policy = rr.DelegatedAgentPermissionPolicy(run_id="issue-2445-run-4")
    for mutation_var in sorted(rr._MUTATION_CREDENTIAL_ENV_VARS):
        env = {"PATH": "/usr/bin", mutation_var: "sentinel-secret-value", "ANTHROPIC_MODEL": "gpt-5.6-terra[1m]"}
        sanitized = policy.sanitize_subprocess_env(env)
        assert mutation_var not in sanitized
        assert sanitized["ANTHROPIC_MODEL"] == "gpt-5.6-terra[1m]"


# ---------------------------------------------------------------------------
# AC2 (continued): regression must be observable on the real production
# nested-invocation path (invoke_agent()'s actual runner env), not merely by
# calling the sanitizer function in isolation.
# ---------------------------------------------------------------------------


def _wrapper_payload(structured_output: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "assistant text summary",
        "structured_output": structured_output,
    }


def test_invoke_agent_forwards_non_mutation_env_to_real_runner_env_with_policy(tmp_path: Path) -> None:
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")
    policy = rr.DelegatedAgentPermissionPolicy(run_id="issue-2445-run-5")
    captured: dict[str, Any] = {}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload({"ok": True})), stderr=""
        )

    request = rr.AgentInvocationRequest(
        agent_name="retrospective-runtime-observer",
        prompt="observe",
        json_schema_path=str(schema_path),
        cwd="/repo",
        env=dict(_AC2_NON_MUTATION_ENV_VARS, **_MUTATION_ENV_VARS),
    )
    rr.invoke_agent(request, runner=_runner, policy=policy)

    for key, value in _AC2_NON_MUTATION_ENV_VARS.items():
        assert captured["env"].get(key) == value, f"{key} did not reach invoke_agent()'s real runner env"
    assert rr._MUTATION_CREDENTIAL_ENV_VARS.isdisjoint(captured["env"].keys())


def test_invoke_agent_forwards_non_mutation_env_to_real_runner_env_without_policy(tmp_path: Path) -> None:
    """Same regression, but through the `policy=None` fallback branch
    (`_default_sanitized_env()`), which Issue #2445 AC1 requires to be kept
    in sync with the policy-path semantics."""
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")
    captured: dict[str, Any] = {}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload({"ok": True})), stderr=""
        )

    request = rr.AgentInvocationRequest(
        agent_name="retrospective-runtime-observer",
        prompt="observe",
        json_schema_path=str(schema_path),
        cwd="/repo",
        env=dict(_AC2_NON_MUTATION_ENV_VARS, **_MUTATION_ENV_VARS),
    )
    rr.invoke_agent(request, runner=_runner, policy=None)

    for key, value in _AC2_NON_MUTATION_ENV_VARS.items():
        assert captured["env"].get(key) == value, f"{key} did not reach invoke_agent()'s real runner env (no policy)"
    assert rr._MUTATION_CREDENTIAL_ENV_VARS.isdisjoint(captured["env"].keys())


# ---------------------------------------------------------------------------
# AC2 (root-cause regression): the ORIGINAL bug (Issue #2436 Background) was
# that the OUTER PROCESS's own `os.environ` (e.g. a caller shell, or the
# outer Claude-GPT session, exporting `ANTHROPIC_BASE_URL` etc. before ever
# invoking `run_retrospective.py`) did not survive into the nested `claude`
# subprocess's env -- NOT that a value supplied via `AgentInvocationRequest
# .env` got stripped. The two tests above supply target vars only via
# `request.env`, which `invoke_agent()` merges via
# `merged_env = {**os.environ, **request.env}` (`run_retrospective.py`)
# BEFORE sanitization -- so they never actually exercise the `os.environ`
# half of that merge. These two tests do: `request.env` is intentionally
# left empty, and the target var is injected ONLY into the outer test
# process's `os.environ` via `monkeypatch.setenv`, directly regression
# testing OWNER review P1
# (https://github.com/squne121/loop-protocol/pull/2453#issuecomment-5469097778).
# ---------------------------------------------------------------------------


def test_invoke_agent_forwards_parent_os_environ_to_real_runner_env_with_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root-cause regression (policy path): a value present ONLY in the
    outer/parent process's `os.environ` (never passed via `request.env`)
    must still reach the real runner env through
    `merged_env = {**os.environ, **request.env}` +
    `policy.sanitize_subprocess_env()`."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:41888")
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")
    policy = rr.DelegatedAgentPermissionPolicy(run_id="issue-2445-run-6")
    captured: dict[str, Any] = {}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload({"ok": True})), stderr=""
        )

    request = rr.AgentInvocationRequest(
        agent_name="retrospective-runtime-observer",
        prompt="observe",
        json_schema_path=str(schema_path),
        cwd="/repo",
        env={},  # deliberately empty: the var must come from os.environ alone
    )
    rr.invoke_agent(request, runner=_runner, policy=policy)

    assert captured["env"].get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:41888", (
        "a value set ONLY on the outer process's os.environ (never via request.env) "
        "did not reach invoke_agent()'s real runner env -- this is the original "
        "Issue #2436 transport_routing_gap regression"
    )


def test_invoke_agent_forwards_parent_os_environ_to_real_runner_env_without_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same root-cause regression, through the `policy=None` fallback branch
    (`_default_sanitized_env()`)."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:41888")
    schema_path = tmp_path / "s.json"
    schema_path.write_text("{}", encoding="utf-8")
    captured: dict[str, Any] = {}

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload({"ok": True})), stderr=""
        )

    request = rr.AgentInvocationRequest(
        agent_name="retrospective-runtime-observer",
        prompt="observe",
        json_schema_path=str(schema_path),
        cwd="/repo",
        env={},  # deliberately empty: the var must come from os.environ alone
    )
    rr.invoke_agent(request, runner=_runner, policy=None)

    assert captured["env"].get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:41888", (
        "a value set ONLY on the outer process's os.environ (never via request.env) "
        "did not reach invoke_agent()'s real runner env (no policy) -- this is the "
        "original Issue #2436 transport_routing_gap regression"
    )
