#!/usr/bin/env python3
"""Live claude CLI integration tests for run_retrospective.py's production
Agent invocation adapter (`build_agent_invocation_argv()` / `invoke_agent()`,
Issue #2301).

Runtime Verification Applicability: immediate (AC5, AC6, AC7,
docs/dev/runtime-verification-policy.md). Every test in this module is
marked `claude_live` (registered in `pyproject.toml`) and is therefore
excluded from the default pytest run (`-m 'not github_live and not
claude_live'` addopts) and from CI's `python-test` target set (`claude_live`
is listed in `.github/ci/python-test-plan.json`'s
`runtime_verification_only_markers`). This module is invoked only via
`verify_run_retrospective_live_cli.sh`, which performs the
`skip_conditions` preflight (claude binary present in PATH; `claude auth
status` exits 0) BEFORE ever invoking pytest -- once pytest starts here,
every failure is a real FAIL (wrapper exit 1), never converted to a SKIP
(`fallback_policy`: SKIP never promotes to PASS).

Unlike `test_run_retrospective.py` (Runtime Verification Applicability:
deferred for that file -- a pure fixture/subprocess-mock harness), every
test here calls the exact production adapter functions
(`run_retrospective.build_agent_invocation_argv` /
`run_retrospective.invoke_agent`) with the real `subprocess.run` default
`runner` -- a genuine `claude` CLI child process is spawned for each test.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402

pytestmark = pytest.mark.claude_live

_SCHEMA_DIR = _SCRIPTS_DIR / "schemas"
_OBSERVER_SCHEMA_PATH = _SCHEMA_DIR / "observer_result_v1.schema.json"
_REPO_ROOT = _SCRIPTS_DIR.parents[3]

#: bounded so a hung/misbehaving real CLI invocation cannot stall CI/local
#: smoke indefinitely; generous enough for a single haiku-model turn.
_LIVE_TIMEOUT_SEC = 180


def test_real_claude_cli_production_policy_round_trip() -> None:
    """AC5: production policy `invoke_agent(request, policy=
    DelegatedAgentPermissionPolicy(run_id=run_id))` invoked against the real
    `retrospective-runtime-observer` Agent, the committed observer schema
    (`scripts/schemas/observer_result_v1.schema.json`), and a nonce-bound
    prompt instructing the Agent to echo the exact given field values back.
    Asserts the full exact-equality contract this AC requires:
    `result.status == "ok"`, `result.exit_code == 0`,
    `result.reason_code is None`, `result.structured_output ==
    expected_payload`, `result.raw_stdout_excerpt is None`."""
    run_id = f"live-cli-{uuid.uuid4()}"
    nonce = uuid.uuid4().hex
    base_sha = "a" * 40
    source_set_digest = "b" * 64
    observer_id = "retrospective-runtime-observer"
    evidence_ref = f"evidence://live-cli/{nonce}"

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
        "This is a deterministic live-CLI adapter verification round-trip "
        "(Issue #2301). Output ONLY a single JSON object conforming exactly "
        "to the observer_result/v1 schema, with EXACTLY these field values "
        "and no other fields (copy every value verbatim, do not paraphrase "
        "or alter any string):\n" + json.dumps(expected_payload, sort_keys=True)
    )

    request = rr.AgentInvocationRequest(
        agent_name=observer_id,
        prompt=prompt,
        json_schema_path=str(_OBSERVER_SCHEMA_PATH),
        cwd=str(_REPO_ROOT),
        timeout_sec=_LIVE_TIMEOUT_SEC,
    )
    policy = rr.DelegatedAgentPermissionPolicy(run_id=run_id)

    result = rr.invoke_agent(request, policy=policy)

    assert result.status == "ok", (result.status, result.reason_code, result.raw_stdout_excerpt)
    assert result.exit_code == 0
    assert result.reason_code is None
    assert result.raw_stdout_excerpt is None
    assert result.structured_output == expected_payload


def test_real_claude_cli_invalid_agent_name_error_handling() -> None:
    """AC6: an invalid/nonexistent Agent name must make `invoke_agent()` end
    in a non-"ok" status carrying no business payload."""
    run_id = f"live-cli-{uuid.uuid4()}"
    request = rr.AgentInvocationRequest(
        agent_name="does-not-exist-agent-2301",
        prompt="irrelevant -- the CLI must reject the --agent name before this is ever read",
        json_schema_path=str(_OBSERVER_SCHEMA_PATH),
        cwd=str(_REPO_ROOT),
        timeout_sec=60,
    )
    policy = rr.DelegatedAgentPermissionPolicy(run_id=run_id)

    result = rr.invoke_agent(request, policy=policy)

    assert result.status != "ok"
    assert result.structured_output is None


def test_real_claude_cli_invalid_schema_fail_closed(tmp_path: Path) -> None:
    """AC7: an invalid `--json-schema` value must make `invoke_agent()` fail
    closed (status != "ok", no business payload). Exact stderr text / exit
    code are intentionally not asserted (AC7 is exit-code-independent)."""
    bad_schema_path = tmp_path / "not-a-schema.json"
    bad_schema_path.write_text("{not valid json at all", encoding="utf-8")

    run_id = f"live-cli-{uuid.uuid4()}"
    request = rr.AgentInvocationRequest(
        agent_name="retrospective-runtime-observer",
        prompt="irrelevant -- the CLI must reject the malformed --json-schema before this is ever read",
        json_schema_path=str(bad_schema_path),
        cwd=str(_REPO_ROOT),
        timeout_sec=60,
    )
    policy = rr.DelegatedAgentPermissionPolicy(run_id=run_id)

    result = rr.invoke_agent(request, policy=policy)

    assert result.status != "ok"
    assert result.structured_output is None
