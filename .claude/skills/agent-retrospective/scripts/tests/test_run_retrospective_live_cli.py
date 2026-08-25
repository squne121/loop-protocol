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


def test_real_claude_cli_analytical_prompt_structured_output_shape() -> None:
    """Issue #2341 AC4 regression test: reproduces the Issue #2341 failure
    shape -- a *substantive* analysis prompt (unlike the trivial
    field-echo prompt `test_real_claude_cli_production_policy_round_trip`
    uses) issued to the real `retrospective-runtime-observer` Agent
    against the same committed `observer_result_v1.schema.json` (a nested
    array-of-objects schema via `findings`). At the time Issue #2341 was
    filed, this invocation shape deterministically (2/2 observed runs)
    resolved to `invoke_agent()`'s `status="malformed_output"` /
    `reason_code="missing_structured_output"` branch -- `exit_code == 0`
    and the wrapper's own `subtype == "success"`, yet the
    `structured_output` wrapper field was absent (see Issue #2341
    Background, and the suspected upstream nested-schema shape reported at
    https://github.com/anthropics/claude-agent-sdk-typescript/issues/277).

    This test is intentionally tolerant of BOTH outcomes so it keeps
    providing CI signal instead of becoming permanently red on an upstream
    defect this repository does not control:
      - if the CLI now returns a fully conformant `status="ok"` result for
        this prompt shape, the test PASSES outright (the regression is
        resolved upstream);
      - if the CLI reproduces the diagnosed `missing_structured_output`
        signature exactly (`exit_code == 0`,
        `reason_code == "missing_structured_output"`), the test is marked
        `xfail` -- a *known*, tracked regression, not a silent PASS and
        not a hard FAIL (`pyproject.toml` does not set `xfail_strict`, so
        an eventual XPASS here is informational, never a failure);
      - any OTHER adapter outcome (timeout, terminated, a different
        reason_code, a non-zero exit_code) is a genuine, undiagnosed
        regression and fails the test for real.
    """
    run_id = f"live-cli-{uuid.uuid4()}"
    nonce = uuid.uuid4().hex
    base_sha = "c" * 40
    source_set_digest = "d" * 64
    observer_id = "retrospective-runtime-observer"
    evidence_ref = f"evidence://live-cli-analysis/{nonce}"

    prompt = (
        "You are the retrospective-runtime-observer for a real engineering "
        "retrospective (Issue #2341 live-CLI regression coverage, nonce "
        f"{nonce}). Investigate this exact question and produce genuine "
        "analytical findings (not a placeholder/echo): what are the "
        "concrete tradeoffs between fail-closed and fail-open error "
        "handling for a subprocess adapter that wraps an external CLI "
        "tool, in the context of a multi-stage pipeline where a later "
        "stage (an evaluator) must never run on malformed/partial output "
        "from an earlier stage (an observer)? Produce at least two "
        "distinct findings, each with genuine analytical content (not a "
        "copy of this prompt).\n\n"
        "Output ONLY a single JSON object conforming exactly to the "
        "observer_result/v1 schema, with these exact envelope field "
        f'values: schema_version="observer_result/v1", run_id="{run_id}", '
        f'base_sha="{base_sha}", source_set_digest="{source_set_digest}", '
        f'observer_id="{observer_id}", evidence_ref="{evidence_ref}", and '
        "a `findings` array of at least two objects, each with a `claim` "
        "(your genuine analysis, non-empty string) and `claim_class` set "
        'to "process".'
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

    print(
        f"test_real_claude_cli_analytical_prompt_structured_output_shape: "
        f"adapter_status={result.status} adapter_reason_code={result.reason_code} "
        f"child_exit_code={result.exit_code}"
    )

    if result.status == "ok":
        # Issue #2341 regression resolved (or never reproduced in this
        # run): a real analytical prompt against the nested `findings`
        # schema produced a fully conformant structured_output.
        assert result.exit_code == 0
        assert result.reason_code is None
        assert isinstance(result.structured_output, dict)
        assert result.structured_output.get("run_id") == run_id
        return

    if (
        result.status == "malformed_output"
        and result.reason_code == "missing_structured_output"
        and result.exit_code == 0
    ):
        pytest.xfail(
            "Issue #2341 known regression reproduced: exit_code=0, wrapper "
            "subtype=success, structured_output missing for a substantive "
            "analysis prompt against the nested `findings` schema "
            "(suspected upstream Claude Code CLI structured-output defect, "
            "see Issue #2341 Background)."
        )

    pytest.fail(
        "undiagnosed adapter outcome for the Issue #2341 analytical-prompt "
        f"regression shape: status={result.status} reason_code={result.reason_code} "
        f"exit_code={result.exit_code} (expected either status='ok' or the "
        "documented missing_structured_output signature)"
    )


def test_real_claude_cli_invalid_agent_name_error_handling() -> None:
    """AC6: an invalid/nonexistent Agent name must make `invoke_agent()` end
    in a non-"ok" status carrying no business payload.

    PR #2324 review fix_delta P1-3: OWNER's concern is that if `claude
    --agent <bogus-name>` silently falls back to a default agent instead of
    erroring, and that default agent happens to satisfy the schema anyway,
    this test would wrongly PASS for the wrong reason. The prompt below
    therefore explicitly instructs a fallback default agent (should one run)
    to refuse structured output / emit a deliberately non-conformant
    payload, so a silent fallback still produces `status != "ok"` for the
    right reason (schema validation failure or explicit refusal), not by
    accident. The assertions additionally rule out environmental/operational
    failures (timeout, terminated) being mistaken for the intended
    fail-closed validation failure."""
    run_id = f"live-cli-{uuid.uuid4()}"
    request = rr.AgentInvocationRequest(
        agent_name="does-not-exist-agent-2301",
        prompt=(
            "irrelevant -- the CLI must reject the --agent name before this "
            "is ever read. If, despite that, you are somehow running as a "
            "fallback default agent: you MUST NOT emit a JSON object "
            "conforming to any structured-output schema. Instead, respond "
            "with plain prose explicitly refusing to produce structured "
            "output, and do not include any JSON object in your response."
        ),
        json_schema_path=str(_OBSERVER_SCHEMA_PATH),
        cwd=str(_REPO_ROOT),
        timeout_sec=60,
    )
    policy = rr.DelegatedAgentPermissionPolicy(run_id=run_id)

    result = rr.invoke_agent(request, policy=policy)

    print(
        f"test_real_claude_cli_invalid_agent_name_error_handling: "
        f"adapter_status={result.status} adapter_reason_code={result.reason_code} "
        f"child_exit_code={result.exit_code}"
    )
    assert result.status != "ok", (result.status, result.reason_code, result.exit_code)
    # `timeout`/`terminated` specifically indicate an environmental/
    # operational failure (hung process, signal), not the intended
    # invalid-agent-name validation failure this test exercises.
    assert result.status not in ("timeout", "terminated"), (result.status, result.reason_code, result.exit_code)
    assert result.structured_output is None


def test_real_claude_cli_invalid_schema_fail_closed(tmp_path: Path) -> None:
    """AC7: an invalid `--json-schema` value must make `invoke_agent()` fail
    closed (status != "ok", no business payload). Exact stderr text / exit
    code are intentionally not asserted (AC7 is exit-code-independent).

    PR #2324 review fix_delta P1-3: the assertions additionally rule out
    environmental/operational failures (timeout, terminated) being mistaken
    for the intended fail-closed validation failure."""
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

    print(
        f"test_real_claude_cli_invalid_schema_fail_closed: "
        f"adapter_status={result.status} adapter_reason_code={result.reason_code} "
        f"child_exit_code={result.exit_code}"
    )
    assert result.status != "ok", (result.status, result.reason_code, result.exit_code)
    # `timeout`/`terminated` specifically indicate an environmental/
    # operational failure (hung process, signal), not the intended
    # invalid-schema validation failure this test exercises.
    assert result.status not in ("timeout", "terminated"), (result.status, result.reason_code, result.exit_code)
    assert result.structured_output is None
