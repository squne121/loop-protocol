"""Hermetic + one live-smoke test for Issue #2374: role-specific adapter
connecting ``agent-retrospective``'s codebase observer (``codebase-investigator``)
to ``run_retrospective.py``'s ``EvidenceBundle``/``OBSERVER_RESULT_V1`` wire
contract, normalizing AGY operational-failure native fallback results into a
base_sha-bound ``OBSERVER_RESULT_V1`` instead of unconditionally failing
closed with ``missing_structured_output``.

Runtime Verification Applicability: ``immediate`` (Issue #2374 body).
``applicable_acs: [AC3, AC9]``. AC1/AC2/AC4/AC5/AC6/AC7/AC8 below are
fixture/mock-based only (Fixture/mock-based only, Runtime Verification
Applicability: hermetic) -- no live ``claude``/``git``/network call is ever
made. AC9's ``test_live_consumer_smoke_...`` is the ONLY test in this file
that launches a real ``claude -p --agent codebase-investigator`` subprocess;
per Issue #2374's ``fallback_policy``, it is verified once manually at
implementation time and is NOT a permanent CI-required gate -- it SKIPs
(never fabricates PASS) only when the ``claude`` binary itself is absent
from PATH (a narrower skip condition than the existing
``.claude/skills/issue-refinement-loop/tests/test_codebase_investigator_agy_fallback_smoke.py``
AC3 test, which also SKIPs on auth/transport markers -- Issue #2374's AC9
treats any other live failure as a typed test failure, never a SKIP).

Maps to Issue #2374's Acceptance Criteria:
  AC1 test_role_adapter_wired_only_into_substantive_codebase_investigator_task
  AC2 test_role_adapter_prepare_native_invoke_verify_convert_pipeline
  AC3 test_live_consumer_smoke_agy_timeout_native_fallback_observer_acceptance
  AC4 test_native_result_status_failed_or_inconclusive_is_typed_failure
  AC5 test_native_result_evidence_base_sha_mismatch_is_typed_failure
  AC6 test_role_adapter_omitted_default_path_still_fails_closed
  AC7 test_default_no_task_path_never_wires_fallback_opt_in
  AC8 (regression) -- covered by running this file alongside
      test_run_retrospective.py / test_security_boundary.py, see Issue #2374
      Verification Commands
  AC9 test_live_consumer_smoke_agy_timeout_native_fallback_observer_acceptance
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_SKILL_DIR = _SCRIPTS_DIR.parent
_REPO_ROOT = _SKILL_DIR.parents[2]
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402

_FULL_SHA = "c" * 40
_OTHER_SHA = "d" * 40
_ARTIFACTS_DIR = _REPO_ROOT / "artifacts" / "2374" / "runtime-verification"

_ENVIRONMENT_UNAVAILABLE_MISSING_BINARY_ONLY = "claude binary not found on PATH"


def _wrapper_payload_with_result_text(result_text: str) -> dict[str, Any]:
    """Shape of the real ``claude -p --output-format json`` wrapper when the
    business payload is embedded (fenced or not) in the ``result`` text
    field rather than the top-level ``structured_output`` field -- the
    real, observed custom-subagent shape this Issue's role adapter targets
    (see ``_structured_output_from_result_compat``'s docstring)."""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result_text,
    }


def _native_result(
    *,
    status: str = "ok",
    evidence_commit_sha: str = _FULL_SHA,
    discovery_summary: str = "AGY delegation failed with failure_class: agy_timeout; completed via native fallback.",
) -> dict[str, Any]:
    """A schema-shaped (8-field) ``CODEBASE_INVESTIGATION_RESULT_V1`` dict,
    as ``.claude/agents/codebase-investigator.md``'s "AGY advisory native
    fallback" section documents it."""
    return {
        "schema_version": 1,
        "status": status,
        "investigation_route": "local_asset_research",
        "evidence_refs": [
            {
                "type": "REPO_EVIDENCE_REF_V1",
                "object_format": "sha1",
                "commit_sha": evidence_commit_sha,
                "path": "docs/adr/0001-architecture.md",
                "start_line": 1,
                "end_line": 3,
                "permalink": "https://github.com/squne121/loop-protocol/blob/"
                f"{evidence_commit_sha}/docs/adr/0001-architecture.md#L1-L3",
                "excerpt_sha256": "e" * 64,
                "verification_status": "verified",
                "verification_method": "native_fallback_bash_git_rev_parse_sha256sum",
                "verified_at": "2026-08-29T00:00:00Z",
            }
        ],
        "discovery_summary": discovery_summary,
        "impact_scope": ["docs/adr/0001-architecture.md"],
        "failure_reason": None,
        "source_evidence_result": None,
    }


def _observer_result(observer_id: str = "codebase-investigator") -> dict[str, Any]:
    """A schema-shaped (7-field) ``OBSERVER_RESULT_V1``/``EvidenceBundle``
    dict -- the shape codebase-investigator returns in the normal
    (AGY-succeeded, non-fallback) path per PR #2358's evidence."""
    return {
        "schema_version": "observer_result/v1",
        "run_id": "run-x",
        "base_sha": _FULL_SHA,
        "source_set_digest": "digest-x",
        "observer_id": observer_id,
        "evidence_ref": "caller-supplied-prompt-evidence-ref",
        "findings": [],
    }


def _compat_schema_path(tmp_path: Path) -> Path:
    schema_path = tmp_path / "observer_result_v1.schema.json"
    schema_path.write_text(
        (_SCRIPTS_DIR / "schemas" / "observer_result_v1.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return schema_path


def _codebase_investigator_request(schema_path: Path, *, role_adapter: str | None) -> rr.AgentInvocationRequest:
    return rr.AgentInvocationRequest(
        agent_name="codebase-investigator",
        prompt="investigate",
        json_schema_path=str(schema_path),
        cwd=str(_REPO_ROOT),
        role_adapter=role_adapter,
    )


def _run_ctx_and_plan(*, run_id: str = "run-x", base_sha: str = _FULL_SHA, digest: str = "digest-x"):
    ctx = rr.RunContext(base_sha_resolver=lambda: base_sha, run_id=run_id)
    plan = rr.SourcePlan(run_id=run_id, base_sha=base_sha, source_set_digest=digest, sources=["repository"])
    return ctx, plan


# ---------------------------------------------------------------------------
# AC1: fallback opt-in wired ONLY into the substantive codebase-investigator
# caller-supplied-task path
# ---------------------------------------------------------------------------


def test_role_adapter_wired_only_into_substantive_codebase_investigator_task() -> None:
    """GIVEN a caller-supplied (substantive) task prompt for every observer
    WHEN bind_observer_prompt()/build_observer_requests() bind it
    THEN only codebase-investigator's bound prompt carries
    agy_advisory_native_fallback_allowed/authoritative_base_sha, and only
    codebase-investigator's AgentInvocationRequest carries role_adapter --
    retrospective-runtime-observer and web-researcher get neither."""
    run_id, base_sha, digest = "run-1", _FULL_SHA, "digest-1"
    bound_prompts = {
        spec.observer_id: rr.bind_observer_prompt(
            f"investigate something for {spec.observer_id}",
            observer_id=spec.observer_id,
            run_id=run_id,
            base_sha=base_sha,
            source_set_digest=digest,
        )
        for spec in rr.EXPECTED_OBSERVER_MANIFEST
    }
    for observer_id, prompt in bound_prompts.items():
        if observer_id == "codebase-investigator":
            assert "agy_advisory_native_fallback_allowed" in prompt
            assert base_sha in prompt
            assert "AGY_ADVISORY_NATIVE_FALLBACK_POLICY" in prompt
        else:
            assert "agy_advisory_native_fallback_allowed" not in prompt
            assert "AGY_ADVISORY_NATIVE_FALLBACK_POLICY" not in prompt

    requests = rr.build_observer_requests(
        schema_dir=_SCRIPTS_DIR / "schemas", cwd=str(_REPO_ROOT), prompts=bound_prompts, caller_supplied_task_path=True
    )
    role_adapters = {r.agent_name: r.role_adapter for r in requests}
    assert role_adapters["codebase-investigator"] == rr._ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1
    assert role_adapters["retrospective-runtime-observer"] is None
    assert role_adapters["web-researcher"] is None


# ---------------------------------------------------------------------------
# AC2: system-owned native result contract vs observer consumer contract
# selection is explicit (role adapter: prepare -> native invoke -> verify ->
# convert), never dependent on task-prompt override
# ---------------------------------------------------------------------------


def test_native_recognizer_never_confuses_observer_and_native_shapes() -> None:
    assert rr._looks_like_native_codebase_investigation_result(_native_result()) is True
    assert rr._looks_like_native_codebase_investigation_result(_observer_result()) is False
    assert rr._looks_like_native_codebase_investigation_result({"schema_version": 1}) is False
    assert rr._looks_like_native_codebase_investigation_result("not-a-dict") is False  # type: ignore[arg-type]


def test_role_adapter_prepare_native_invoke_verify_convert_pipeline(tmp_path: Path) -> None:
    """GIVEN a role_adapter-enabled codebase-investigator request whose
    wrapper `result` text carries the native CODEBASE_INVESTIGATION_RESULT_V1
    shape (never the observer_result_v1 shape -- simulating the AGY-fallback
    system-prompt-driven output codebase-investigator.md documents)
    WHEN invoke_agent_with_role_adapter() runs the prepare -> native-invoke
    -> verify -> convert pipeline
    THEN the result is a genuinely valid EvidenceBundle -- accepted by
    run_observer_wave() end-to-end, never bypassing its base_sha/run_id/
    source_set_digest identity checks."""
    schema_path = _compat_schema_path(tmp_path)
    ctx, plan = _run_ctx_and_plan()
    native = _native_result(evidence_commit_sha=ctx.base_sha)

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        wrapper = _wrapper_payload_with_result_text("```json\n" + json.dumps(native) + "\n```")
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    request = _codebase_investigator_request(
        schema_path, role_adapter=rr._ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1
    )

    def _invoke(req: rr.AgentInvocationRequest) -> rr.AgentInvocationResult:
        return rr.invoke_agent_with_role_adapter(req, ctx=ctx, plan=plan, runner=_runner)

    bundles = rr.run_observer_wave(ctx, plan, invoke=_invoke, observer_requests=[request])
    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle.schema_version == "observer_result/v1"
    assert bundle.observer_id == "codebase-investigator"
    assert bundle.run_id == ctx.run_id
    assert bundle.base_sha == ctx.base_sha
    assert bundle.source_set_digest == plan.source_set_digest
    assert bundle.findings and bundle.findings[0]["claim"] == native["discovery_summary"]
    assert bundle.findings[0]["evidence_refs"][0]["commit_sha"] == ctx.base_sha


def test_invoke_agent_native_recognition_disabled_when_role_adapter_none(tmp_path: Path) -> None:
    """GIVEN the exact same native-shaped `result` text
    WHEN role_adapter is None (every non-codebase-investigator observer, and
    codebase-investigator's own default/no-task path)
    THEN invoke_agent() never recognizes the native shape and fails closed
    with missing_structured_output -- proving native recognition is opt-in,
    not a global relaxation."""
    schema_path = _compat_schema_path(tmp_path)
    native = _native_result()

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        wrapper = _wrapper_payload_with_result_text("```json\n" + json.dumps(native) + "\n```")
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    request = _codebase_investigator_request(schema_path, role_adapter=None)
    result = rr.invoke_agent(request, runner=_runner)
    assert result.status == "malformed_output"
    assert result.reason_code == "missing_structured_output"
    assert result.native_role_adapter_candidate is False


# ---------------------------------------------------------------------------
# AC4: native failed/inconclusive is a typed failure, never an empty-findings
# success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_status", ["failed", "inconclusive"])
def test_native_result_status_failed_or_inconclusive_is_typed_failure(bad_status: str) -> None:
    ctx, plan = _run_ctx_and_plan()
    native = _native_result(status=bad_status, evidence_commit_sha=ctx.base_sha)
    with pytest.raises(rr.NativeResultAdaptationFailed) as excinfo:
        rr.adapt_native_codebase_investigation_result(
            native,
            run_id=ctx.run_id,
            base_sha=ctx.base_sha,
            source_set_digest=plan.source_set_digest,
            observer_id="codebase-investigator",
        )
    assert excinfo.value.reason_code == "native_result_status_not_ok"


def test_native_result_status_failed_pipeline_surfaces_as_malformed_output(tmp_path: Path) -> None:
    schema_path = _compat_schema_path(tmp_path)
    ctx, plan = _run_ctx_and_plan()
    native = _native_result(status="failed", evidence_commit_sha=ctx.base_sha)

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        wrapper = _wrapper_payload_with_result_text("```json\n" + json.dumps(native) + "\n```")
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    request = _codebase_investigator_request(
        schema_path, role_adapter=rr._ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1
    )
    result = rr.invoke_agent_with_role_adapter(request, ctx=ctx, plan=plan, runner=_runner)
    assert result.status == "malformed_output"
    assert result.reason_code == "native_fallback_adaptation_failed:native_result_status_not_ok"
    # never silently downgraded to an empty-findings success
    assert result.structured_output is None


# ---------------------------------------------------------------------------
# AC5: evidence commit_sha != ctx.base_sha fail-closes
# ---------------------------------------------------------------------------


def test_native_result_evidence_base_sha_mismatch_is_typed_failure() -> None:
    ctx, plan = _run_ctx_and_plan()
    native = _native_result(evidence_commit_sha=_OTHER_SHA)
    with pytest.raises(rr.NativeResultAdaptationFailed) as excinfo:
        rr.adapt_native_codebase_investigation_result(
            native,
            run_id=ctx.run_id,
            base_sha=ctx.base_sha,
            source_set_digest=plan.source_set_digest,
            observer_id="codebase-investigator",
        )
    assert excinfo.value.reason_code == "native_result_evidence_base_sha_mismatch"


def test_native_result_evidence_base_sha_mismatch_pipeline_surfaces_as_malformed_output(tmp_path: Path) -> None:
    schema_path = _compat_schema_path(tmp_path)
    ctx, plan = _run_ctx_and_plan()
    native = _native_result(evidence_commit_sha=_OTHER_SHA)

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        wrapper = _wrapper_payload_with_result_text("```json\n" + json.dumps(native) + "\n```")
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    request = _codebase_investigator_request(
        schema_path, role_adapter=rr._ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1
    )
    result = rr.invoke_agent_with_role_adapter(request, ctx=ctx, plan=plan, runner=_runner)
    assert result.status == "malformed_output"
    assert result.reason_code == "native_fallback_adaptation_failed:native_result_evidence_base_sha_mismatch"


# ---------------------------------------------------------------------------
# AC6: agy_advisory_native_fallback_allowed omitted/false -> existing
# fail-close preserved (regression)
# ---------------------------------------------------------------------------


def test_role_adapter_omitted_default_path_still_fails_closed(tmp_path: Path) -> None:
    """GIVEN codebase-investigator's default/no-task path (no
    --prompts-file -- caller_supplied_task_path=False)
    WHEN build_observer_requests() builds its request
    THEN role_adapter is None (identical to every pre-#2374 caller), so a
    native-shaped `result` still fails closed with missing_structured_output
    -- the fix does not weaken the pre-existing fail-close guarantee for
    any caller that has not explicitly opted in."""
    prompts = {spec.observer_id: f"prompt for {spec.observer_id}" for spec in rr.EXPECTED_OBSERVER_MANIFEST}
    requests = rr.build_observer_requests(
        schema_dir=_SCRIPTS_DIR / "schemas", cwd=str(_REPO_ROOT), prompts=prompts, caller_supplied_task_path=False
    )
    codebase_investigator_request = next(r for r in requests if r.agent_name == "codebase-investigator")
    assert codebase_investigator_request.role_adapter is None

    schema_path = _compat_schema_path(tmp_path)
    native = _native_result()

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        wrapper = _wrapper_payload_with_result_text("```json\n" + json.dumps(native) + "\n```")
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    result = rr.invoke_agent(
        rr.AgentInvocationRequest(
            agent_name="codebase-investigator",
            prompt=prompts["codebase-investigator"],
            json_schema_path=str(schema_path),
            cwd=str(_REPO_ROOT),
            role_adapter=codebase_investigator_request.role_adapter,
        ),
        runner=_runner,
    )
    assert result.status == "malformed_output"
    assert result.reason_code == "missing_structured_output"


# ---------------------------------------------------------------------------
# AC7: default/no-task observer path never wires the fallback opt-in
# ---------------------------------------------------------------------------


def test_default_no_task_path_never_wires_fallback_opt_in() -> None:
    default_prompt = rr._default_observer_prompt(
        "codebase-investigator", run_id="run-2", base_sha=_FULL_SHA, source_set_digest="digest-2"
    )
    assert "agy_advisory_native_fallback_allowed" not in default_prompt
    assert "AGY_ADVISORY_NATIVE_FALLBACK_POLICY" not in default_prompt

    # bind_observer_prompt(task_prompt=None, ...) is the same no-task path
    # _default_observer_prompt is a thin wrapper around.
    none_task_prompt = rr.bind_observer_prompt(
        None, observer_id="codebase-investigator", run_id="run-2", base_sha=_FULL_SHA, source_set_digest="digest-2"
    )
    assert "agy_advisory_native_fallback_allowed" not in none_task_prompt

    # An empty/whitespace-only task_prompt also counts as "no task" (has_task
    # is False), so it must not be wired either.
    whitespace_task_prompt = rr.bind_observer_prompt(
        "   ", observer_id="codebase-investigator", run_id="run-2", base_sha=_FULL_SHA, source_set_digest="digest-2"
    )
    assert "agy_advisory_native_fallback_allowed" not in whitespace_task_prompt


# ---------------------------------------------------------------------------
# AC9: live consumer smoke (real, bounded subprocess launch) -- see module
# docstring for skip semantics. NOT a permanent CI-required gate (Issue
# #2374 fallback_policy).
# ---------------------------------------------------------------------------


def _write_runtime_verification_log(*, verdict: str, reason: str, payload: dict[str, Any]) -> Path:
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = _ARTIFACTS_DIR / f"runtime-verification-AC9-{timestamp}.log"
    lines = [
        "=== Runtime Verification Log (Issue #2374 AC9) ===",
        f"Timestamp: {timestamp}",
        "Environment: real `claude` binary on PATH (bounded, --agent codebase-investigator)",
        "",
        "--- Input / Output ---",
        json.dumps(payload, indent=2, sort_keys=True, default=str)[:20000],
        "",
        "--- Verdict ---",
        f"Result: {verdict}",
        f"Reason: {reason}",
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def _extract_native_result_json(text: str) -> dict[str, Any]:
    """Best-effort extraction of a JSON- or YAML-fenced
    CODEBASE_INVESTIGATION_RESULT_V1 block from the live SubAgent's final
    message text. Returns {} (never fabricates) if nothing parses into a
    dict carrying a "status" field."""
    import re

    import yaml

    candidates = list(re.findall(r"```(?:json|ya?ml)?\n(.*?)```", text, re.DOTALL))
    idx = text.find("CODEBASE_INVESTIGATION_RESULT_V1")
    if idx != -1:
        candidates.append(text[idx:])
    for candidate in candidates:
        for loader in (json.loads, yaml.safe_load):
            try:
                parsed = loader(candidate)
            except Exception:  # noqa: BLE001 - best-effort parse across two loaders
                continue
            if isinstance(parsed, dict):
                inner = parsed.get("CODEBASE_INVESTIGATION_RESULT_V1")
                if isinstance(inner, dict):
                    return inner
                if "status" in parsed:
                    return parsed
    return {}


def test_live_consumer_smoke_agy_timeout_native_fallback_observer_acceptance() -> None:
    """GIVEN a real, bounded `claude -p --agent codebase-investigator`
    launch, a fake AGY delegation wrapper failure (ok: false, failure_class:
    agy_timeout) supplied as a pre-completed test double, and explicit
    agy_advisory_native_fallback_allowed: true + authoritative_base_sha
    opt-in (this Issue's actual production wiring shape)
    WHEN the live SubAgent transitions to native fallback per its own
    operating instructions
    THEN the reported CODEBASE_INVESTIGATION_RESULT_V1 (a) is base_sha-bound
    (evidence commit_sha equals authoritative_base_sha) and (b) is genuinely
    ACCEPTED by this module's role adapter
    (adapt_native_codebase_investigation_result) as a valid
    EvidenceBundle/OBSERVER_RESULT_V1 -- never raising
    NativeResultAdaptationFailed.

    Per Issue #2374's narrower skip_conditions (distinct from the existing
    #2360 smoke test): SKIPs ONLY when the `claude` binary itself is absent
    from PATH. Any other live failure (auth, transport, non-zero exit) is a
    typed test FAILURE, not a SKIP.
    """
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        _write_runtime_verification_log(
            verdict="SKIP",
            reason=_ENVIRONMENT_UNAVAILABLE_MISSING_BINARY_ONLY,
            payload={"claude_bin": None},
        )
        pytest.skip(f"SKIP: {_ENVIRONMENT_UNAVAILABLE_MISSING_BINARY_ONLY}")

    git_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=30, check=False
    )
    assert git_proc.returncode == 0, f"git rev-parse HEAD failed: {git_proc.stderr}"
    authoritative_base_sha = git_proc.stdout.strip()

    marker = f"SENTINEL_MARKER_{uuid.uuid4().hex}"
    sentinel_dir = _ARTIFACTS_DIR / "live-smoke-sentinel"
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    sentinel_path = sentinel_dir / "sentinel.txt"
    sentinel_path.write_text(marker + "\n", encoding="utf-8")

    fake_wrapper_result = {
        "schema": "delegation_result/v1",
        "provider": "agy",
        "ok": False,
        "tool_profile": "local_asset_research",
        "exit_code": 1,
        "stderr": "agy_timeout: process exceeded 600s",
        "warnings": ["agy_timeout: process exceeded 600s"],
        "failure_reason": "agy_timeout: process exceeded 600s",
        "failure_class": "agy_timeout",
    }

    prompt = f"""You are being invoked as the codebase-investigator SubAgent
(fake-AGY-failure test double scenario, Issue #2374 runtime verification
smoke -- this is a real, single, bounded live invocation).

## Local investigation mode input

- target_path: {sentinel_path}
- purpose: Read the sentinel file and report its exact content.
- agy_advisory_native_fallback_allowed: true
- authoritative_base_sha: {authoritative_base_sha}

## Pre-completed AGY delegation wrapper attempt (test double)

For this test scenario only, the AGY delegation wrapper invocation has
ALREADY been attempted and completed. Do not re-invoke build_request.py or
run_gemini_headless.py for this request. The wrapper's --output-file JSON
result was:

```json
{json.dumps(fake_wrapper_result, indent=2)}
```

## Your task

Per your own operating instructions in codebase-investigator.md (the "AGY
advisory native fallback" section), given the above wrapper failure
(ok: false, failure_class: agy_timeout) and the explicit
agy_advisory_native_fallback_allowed: true and authoritative_base_sha input,
carry out the native fallback under the non-mutating investigation policy:

1. Use Read to read {sentinel_path} and confirm its exact content.
2. Use Bash to run `git rev-parse HEAD` (read-only, non-mutating) and
   confirm it equals authoritative_base_sha ({authoritative_base_sha}) above
   -- if it does not, you MUST report status: inconclusive per your own
   base_sha-binding instructions, not status: ok.
3. Use Bash to run `sha256sum {sentinel_path}` (read-only, non-mutating) to
   compute the excerpt_sha256 of the file you just read.
4. You must not use Edit, Write, MultiEdit, or any Bash command that mutates
   files or git state (no writes, no git add/commit/checkout/reset, etc.).
5. Report the final CODEBASE_INVESTIGATION_RESULT_V1 (YAML, inside a
   ```yaml code fence) as your last message. It MUST include every one of:
   schema_version, status, investigation_route, evidence_refs,
   discovery_summary, impact_scope, failure_reason, source_evidence_result.
   The evidence_refs entry for {sentinel_path} MUST include commit_sha
   (equal to authoritative_base_sha, from step 2), excerpt_sha256 (the value
   from step 3), verification_status: verified, and verification_method.
   discovery_summary MUST explicitly state that the AGY delegation wrapper
   failed with failure_class: agy_timeout and that you completed this
   investigation via the native fallback route.
"""

    argv = [
        claude_bin,
        "-p",
        "--agent",
        "codebase-investigator",
        "--output-format",
        "stream-json",
        "--include-hook-events",
        "--no-session-persistence",
        "--max-turns",
        "12",
        "--verbose",
        "--permission-mode",
        "dontAsk",
    ]

    proc = subprocess.run(
        argv, cwd=_REPO_ROOT, input=prompt, capture_output=True, text=True, timeout=300, check=False
    )

    final_result_text = ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "result":
            final_result_text = obj.get("result") or ""

    native_result = _extract_native_result_json(final_result_text)

    ctx, plan = _run_ctx_and_plan(run_id="live-smoke-run", base_sha=authoritative_base_sha, digest="live-smoke-digest")
    adaptation_error: str | None = None
    converted: dict[str, Any] | None = None
    try:
        converted = rr.adapt_native_codebase_investigation_result(
            native_result,
            run_id=ctx.run_id,
            base_sha=ctx.base_sha,
            source_set_digest=plan.source_set_digest,
            observer_id="codebase-investigator",
        )
    except rr.NativeResultAdaptationFailed as exc:
        adaptation_error = exc.reason_code

    bundle_accepted = False
    if converted is not None:
        try:
            bundle = rr.EvidenceBundle(**converted)
            bundle_accepted = bundle.base_sha == authoritative_base_sha
        except Exception:  # noqa: BLE001 - captured in payload for diagnosis
            bundle_accepted = False

    payload = {
        "argv": argv,
        "returncode": proc.returncode,
        "marker": marker,
        "authoritative_base_sha": authoritative_base_sha,
        "final_result_excerpt": final_result_text[:4000],
        "native_result": native_result,
        "adaptation_error": adaptation_error,
        "bundle_accepted": bundle_accepted,
    }

    verdict_ok = (
        proc.returncode == 0
        and marker in final_result_text
        and native_result.get("status") == "ok"
        and adaptation_error is None
        and bundle_accepted
    )

    log_path = _write_runtime_verification_log(
        verdict="PASS" if verdict_ok else "FAIL",
        reason=(
            "native fallback executed, base_sha-bound evidence retrieved, and"
            " the role adapter accepted the result as a valid"
            " EvidenceBundle/OBSERVER_RESULT_V1"
            if verdict_ok
            else "one or more assertions failed -- see payload"
        ),
        payload=payload,
    )

    assert proc.returncode == 0, f"claude -p --agent codebase-investigator exited {proc.returncode}; see {log_path}"
    assert marker in final_result_text, f"sentinel marker {marker!r} not found in final result; see {log_path}"
    assert native_result.get("status") == "ok", (
        f"live SubAgent did not report status: ok (got {native_result.get('status')!r}); see {log_path}"
    )
    assert adaptation_error is None, (
        f"role adapter rejected the live native result: {adaptation_error}; see {log_path}"
    )
    assert bundle_accepted, (
        f"converted result was not accepted as a valid, base_sha-bound EvidenceBundle; see {log_path}"
    )
