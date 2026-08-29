"""Hermetic + one live-smoke test for Issue #2374: role-specific adapter
connecting ``agent-retrospective``'s codebase observer (``codebase-investigator``)
to ``run_retrospective.py``'s ``EvidenceBundle``/``OBSERVER_RESULT_V1`` wire
contract, normalizing AGY operational-failure native fallback results into a
base_sha-bound ``OBSERVER_RESULT_V1`` instead of unconditionally failing
closed with ``missing_structured_output``.

Redesigned per OWNER review
https://github.com/squne121/loop-protocol/pull/2387#issuecomment-5459502795
(P0-1/P0-2/P0-3/P0-4/P1-5/P1-6):

  - P0-1: ``build_observer_requests()`` now selects the ``--json-schema``
    CLI argument deterministically from ``role_adapter`` (the native
    ``codebase_investigation_result_v1.schema.json`` when set, the observer
    ``observer_result_v1.schema.json`` otherwise) instead of mixing two
    schemas together in a single, always-observer-schema invocation.
  - P0-3: the native recognizer is real ``jsonschema.validate`` against
    ``codebase_investigation_result_v1.schema.json`` (this file's tests
    exercise that schema directly, not a Python structural key check).
  - P0-4: evidence bytes are independently re-verified against a real
    ``git show <commit_sha>:<path>`` (via ``validate_repo_evidence_ref``,
    reused unmodified from ``gemini-cli-headless-delegation``) -- tests that
    reach this step use this actual repo's real, current HEAD commit and
    real tracked file content (never a fabricated hash) so the byte
    verification is genuinely exercised, not bypassed.
  - P0-2/P1-5: the live smoke (AC9) now drives the SAME production composed
    path (``build_observer_requests`` -> ``bind_observer_prompt`` ->
    ``invoke_agent_with_role_adapter`` -> the real recovery/schema/adapter
    pipeline) a real ``run_observer_wave()`` invocation would use, rather
    than a bespoke ``--output-format stream-json`` + hand-rolled YAML
    extraction harness that bypassed all of it. It carries
    ``@pytest.mark.claude_live`` (registered project-wide in
    ``pyproject.toml``, deselected by default addopts) so it never runs
    inside the ordinary hermetic ``pytest`` invocation.

Runtime Verification Applicability: ``immediate`` (Issue #2374 body).
``applicable_acs: [AC3, AC9]``. AC1/AC2/AC4/AC5/AC6/AC7/AC8 below are
fixture/mock-based (Runtime Verification Applicability: hermetic) and never
launch a real ``claude``/network subprocess -- P0-4's byte-verification
tests DO run real, local, read-only ``git`` commands against this actual
repo checkout (never network, never ``claude``), which is the same
"hermetic" bar the rest of this module's git-touching tests already use.
AC9's ``test_live_consumer_smoke_...`` is the ONLY test in this file that
launches a real ``claude -p --agent codebase-investigator`` subprocess; per
Issue #2374's ``fallback_policy``, it is verified once manually at
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

  P0-3 (OWNER review, real jsonschema validation of the native contract):
      test_codebase_investigation_result_schema_accepts_only_valid_native_shape
  P0-4 (OWNER review, base_sha-bound byte verification, real git):
      test_evidence_ref_bytes_verified_against_real_git_blob
      test_evidence_ref_untracked_path_at_base_sha_is_rejected
      test_evidence_ref_reported_hash_not_matching_real_blob_is_rejected
      test_evidence_ref_path_traversal_is_rejected
  P1-6 (OWNER review, structured fallback/failure-class retention):
      test_converted_finding_carries_structured_fallback_metadata
"""

from __future__ import annotations

import hashlib
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


def _git_head_sha() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, f"git rev-parse HEAD failed: {proc.stderr}"
    return proc.stdout.strip()


def _real_git_blob_bytes(*, commit_sha: str, path: str) -> bytes:
    proc = subprocess.run(
        ["git", "show", f"{commit_sha}:{path}"], cwd=_REPO_ROOT, capture_output=True, timeout=30, check=True
    )
    return proc.stdout


def _compute_excerpt_sha256(blob_bytes: bytes, *, start_line: int, end_line: int) -> str:
    """The exact canonicalization rule this module's real byte verification
    uses (SSOT: .claude/skills/gemini-cli-headless-delegation/references/
    usage-contract.md "Excerpt Canonicalization"), duplicated here ONLY so
    test fixtures can independently compute the value a REAL evidence ref
    would need to carry -- never used by production code itself."""
    lines = blob_bytes.split(b"\n")
    sliced = lines[start_line - 1 : end_line]
    reconstructed = b"\n".join(sliced)
    if end_line < len(lines):
        reconstructed += b"\n"
    return hashlib.sha256(reconstructed).hexdigest()


def _real_repo_evidence_ref(*, commit_sha: str, path: str, start_line: int = 1, end_line: int = 1) -> dict[str, Any]:
    """Build a REPO_EVIDENCE_REF_V1 whose ``excerpt_sha256`` is computed via
    a REAL ``git show <commit_sha>:<path>`` against this actual repo
    checkout (Issue #2374 OWNER review P0-4) -- never a hand-typed/fake
    hash. Any tracked, existing repo-relative ``path``/line-range works;
    the byte content itself does not need to be stable across commits since
    both the fixture and the code under test recompute the hash from the
    SAME ``git show`` output at test-run time."""
    blob_bytes = _real_git_blob_bytes(commit_sha=commit_sha, path=path)
    excerpt_sha256 = _compute_excerpt_sha256(blob_bytes, start_line=start_line, end_line=end_line)
    return {
        "type": "REPO_EVIDENCE_REF_V1",
        "object_format": "sha1",
        "commit_sha": commit_sha,
        "path": path,
        "start_line": start_line,
        "end_line": end_line,
        "permalink": f"https://github.com/squne121/loop-protocol/blob/{commit_sha}/{path}#L{start_line}-L{end_line}",
        "excerpt_sha256": excerpt_sha256,
        "verification_status": "verified",
        "verification_method": "sha256_hash_match",
        "verified_at": "2026-08-29T00:00:00Z",
    }


def _fake_repo_evidence_ref(*, commit_sha: str) -> dict[str, Any]:
    """A schema-shaped (but never git-verified) REPO_EVIDENCE_REF_V1 --
    ONLY valid for tests whose code path short-circuits (status/base_sha
    checks) BEFORE ever reaching the real git byte-verification step
    (``adapt_native_codebase_investigation_result``'s ordering: schema ->
    status -> per-ref commit_sha == base_sha -> per-ref byte verification)."""
    return {
        "type": "REPO_EVIDENCE_REF_V1",
        "object_format": "sha1",
        "commit_sha": commit_sha,
        "path": "docs/adr/0001-architecture.md",
        "start_line": 1,
        "end_line": 3,
        "permalink": f"https://github.com/squne121/loop-protocol/blob/{commit_sha}/docs/adr/0001-architecture.md#L1-L3",
        "excerpt_sha256": "e" * 64,
        "verification_status": "verified",
        "verification_method": "sha256_hash_match",
        "verified_at": "2026-08-29T00:00:00Z",
    }


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
    evidence_refs: list[dict[str, Any]] | None = None,
    evidence_commit_sha: str = _FULL_SHA,
    discovery_summary: str = (
        "AGY delegation failed with failure_class: agy_timeout; completed via native fallback."
    ),
) -> dict[str, Any]:
    """A schema-shaped (8-field) ``CODEBASE_INVESTIGATION_RESULT_V1`` dict,
    as ``.claude/agents/codebase-investigator.md``'s "AGY advisory native
    fallback" section documents it. When ``evidence_refs`` is not supplied,
    a single ``_fake_repo_evidence_ref`` is used -- only safe for tests
    whose assertions are reached BEFORE the real git byte-verification step
    (see that helper's docstring)."""
    return {
        "schema_version": 1,
        "status": status,
        "investigation_route": "local_asset_research",
        "evidence_refs": (
            evidence_refs if evidence_refs is not None else [_fake_repo_evidence_ref(commit_sha=evidence_commit_sha)]
        ),
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


def _compat_schema_path(tmp_path: Path, *, filename: str = "observer_result_v1.schema.json") -> Path:
    schema_path = tmp_path / filename
    schema_path.write_text(
        (_SCRIPTS_DIR / "schemas" / filename).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return schema_path


def _compat_native_schema_path(tmp_path: Path) -> Path:
    return _compat_schema_path(tmp_path, filename=rr._CODEBASE_INVESTIGATION_RESULT_SCHEMA_FILENAME)


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
    retrospective-runtime-observer and web-researcher get neither. THEN also
    (OWNER review P0-1) only codebase-investigator's request's
    json_schema_path resolves to the native schema file -- the other two
    observers keep observer_result_v1.schema.json."""
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

    schema_paths = {r.agent_name: Path(r.json_schema_path).name for r in requests}
    assert schema_paths["codebase-investigator"] == rr._CODEBASE_INVESTIGATION_RESULT_SCHEMA_FILENAME
    assert schema_paths["retrospective-runtime-observer"] == "observer_result_v1.schema.json"
    assert schema_paths["web-researcher"] == "observer_result_v1.schema.json"


# ---------------------------------------------------------------------------
# AC2 / P0-1 / P0-3: system-owned native result contract vs observer
# consumer contract selection is explicit and schema-driven (role adapter:
# prepare -> native invoke -> verify -> convert), never dependent on
# task-prompt override or a Python structural key check
# ---------------------------------------------------------------------------


def test_codebase_investigation_result_schema_accepts_only_valid_native_shape() -> None:
    """OWNER review P0-3: the native recognizer is now real ``jsonschema``
    validation against ``codebase_investigation_result_v1.schema.json`` --
    this test exercises that schema directly (not a removed Python
    structural key check), including the 5 negative shapes the OWNER review
    explicitly called out."""
    import jsonschema

    schema = rr._codebase_investigation_result_schema()
    good = _native_result(evidence_refs=[_fake_repo_evidence_ref(commit_sha=_FULL_SHA)])
    jsonschema.validate(good, schema)  # must not raise

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_observer_result(), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"schema_version": 1}, schema)

    bad_route = dict(good, investigation_route="not_a_real_route")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad_route, schema)

    bad_scope = dict(good, impact_scope=[1, 2, 3])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad_scope, schema)

    missing_evidence_field = json.loads(json.dumps(good))
    del missing_evidence_field["evidence_refs"][0]["verified_at"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(missing_evidence_field, schema)

    status_failure_reason_conflict = dict(good, status="ok", failure_reason="oops")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(status_failure_reason_conflict, schema)

    bad_source_evidence_result = dict(good, source_evidence_result={"schema": "not_the_right_schema"})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad_source_evidence_result, schema)


def test_role_adapter_prepare_native_invoke_verify_convert_pipeline() -> None:
    """GIVEN a role_adapter-enabled codebase-investigator request whose
    wrapper `result` text carries the native CODEBASE_INVESTIGATION_RESULT_V1
    shape (never the observer_result_v1 shape -- simulating the AGY-fallback
    system-prompt-driven output codebase-investigator.md documents), with a
    real, git-verifiable evidence_refs entry bound to this actual repo's
    real HEAD commit (OWNER review P0-4)
    WHEN invoke_agent_with_role_adapter() runs the prepare -> native-invoke
    -> verify -> convert pipeline
    THEN the result is a genuinely valid EvidenceBundle -- accepted by
    run_observer_wave() end-to-end, never bypassing its base_sha/run_id/
    source_set_digest identity checks, AND the evidence bytes were
    independently re-verified against the real git blob (not merely a
    string-compared commit_sha)."""
    real_base_sha = _git_head_sha()
    ctx, plan = _run_ctx_and_plan(base_sha=real_base_sha)
    real_ref = _real_repo_evidence_ref(commit_sha=real_base_sha, path="pyproject.toml")
    native = _native_result(evidence_refs=[real_ref])

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        # OWNER review P0-1: this invocation's --json-schema argv value must
        # be the NATIVE schema content (build_agent_invocation_argv reads
        # request.json_schema_path's file content verbatim) -- assert this
        # test itself is wired that way, not accidentally exercising the
        # observer schema.
        schema_arg = argv[argv.index("--json-schema") + 1]
        assert schema_arg == rr._CODEBASE_INVESTIGATION_RESULT_SCHEMA_PATH.read_text(encoding="utf-8")
        wrapper = _wrapper_payload_with_result_text("```json\n" + json.dumps(native) + "\n```")
        return subprocess.CompletedProcess(argv, returncode=0, stdout=json.dumps(wrapper), stderr="")

    request = rr.AgentInvocationRequest(
        agent_name="codebase-investigator",
        prompt="investigate",
        json_schema_path=str(rr._CODEBASE_INVESTIGATION_RESULT_SCHEMA_PATH),
        cwd=str(_REPO_ROOT),
        role_adapter=rr._ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1,
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
    assert bundle.findings[0]["evidence_refs"][0]["commit_sha"] == real_base_sha


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
            repo_root=_REPO_ROOT,
        )
    assert excinfo.value.reason_code == "native_result_status_not_ok"


def test_native_result_status_failed_pipeline_surfaces_as_malformed_output(tmp_path: Path) -> None:
    schema_path = _compat_native_schema_path(tmp_path)
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
            repo_root=_REPO_ROOT,
        )
    assert excinfo.value.reason_code == "native_result_evidence_base_sha_mismatch"


def test_native_result_evidence_base_sha_mismatch_pipeline_surfaces_as_malformed_output(tmp_path: Path) -> None:
    schema_path = _compat_native_schema_path(tmp_path)
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
# P0-4 (OWNER review): base_sha binding is independently re-verified against
# a REAL git blob, never a self-reported string comparison alone. All 4
# tests below run real, local, read-only `git` commands against this actual
# repo checkout -- no network, no `claude` subprocess.
# ---------------------------------------------------------------------------


def test_evidence_ref_bytes_verified_against_real_git_blob() -> None:
    """A REPO_EVIDENCE_REF_V1 whose excerpt_sha256 genuinely matches the
    real git blob at commit_sha/path/line-range is accepted."""
    real_base_sha = _git_head_sha()
    ref = _real_repo_evidence_ref(commit_sha=real_base_sha, path="pyproject.toml")
    rr._verify_repo_evidence_ref_bytes(ref, repo_root=_REPO_ROOT)  # must not raise


def test_evidence_ref_untracked_path_at_base_sha_is_rejected() -> None:
    """OWNER review P0-4 negative test (a): a path that does not exist at
    base_sha (an untracked/nonexistent path), even paired with the correct,
    real base_sha, must be rejected -- `git show <base_sha>:<path>` fails,
    which this module treats as a hard rejection, not merely "inconclusive"
    and good enough."""
    real_base_sha = _git_head_sha()
    ref = {
        "type": "REPO_EVIDENCE_REF_V1",
        "object_format": "sha1",
        "commit_sha": real_base_sha,
        "path": "THIS_PATH_DOES_NOT_EXIST_IN_REPO_ISSUE_2374.txt",
        "start_line": 1,
        "end_line": 1,
        "permalink": f"https://github.com/squne121/loop-protocol/blob/{real_base_sha}/THIS_PATH_DOES_NOT_EXIST_IN_REPO_ISSUE_2374.txt#L1-L1",
        "excerpt_sha256": "f" * 64,
        "verification_status": "verified",
        "verification_method": "sha256_hash_match",
        "verified_at": "2026-08-29T00:00:00Z",
    }
    with pytest.raises(rr.NativeResultAdaptationFailed) as excinfo:
        rr._verify_repo_evidence_ref_bytes(ref, repo_root=_REPO_ROOT)
    assert excinfo.value.reason_code == "native_result_evidence_bytes_unverified"


def test_evidence_ref_reported_hash_not_matching_real_blob_is_rejected() -> None:
    """OWNER review P0-4 negative test (b): a real, tracked path/commit_sha
    pairing whose REPORTED excerpt_sha256 does NOT match the real git blob's
    bytes at that commit (simulating a caller reporting a hash it computed
    from different -- e.g. locally worktree-modified -- bytes than what is
    actually committed at that SHA) must be rejected. This exercises the
    exact code path capability-matrix.md requires: verification is always
    against `git show <base_sha>:<path>`, never the current worktree file's
    bytes, so a mismatched self-reported hash cannot slip through."""
    real_base_sha = _git_head_sha()
    real_ref = _real_repo_evidence_ref(commit_sha=real_base_sha, path="pyproject.toml")
    tampered_ref = dict(real_ref, excerpt_sha256=hashlib.sha256(b"TAMPERED_WORKTREE_BYTES_NOT_COMMITTED").hexdigest())
    with pytest.raises(rr.NativeResultAdaptationFailed) as excinfo:
        rr._verify_repo_evidence_ref_bytes(tampered_ref, repo_root=_REPO_ROOT)
    assert excinfo.value.reason_code == "native_result_evidence_bytes_unverified"


@pytest.mark.parametrize(
    "bad_path", ["/etc/passwd", "../../../etc/passwd", "docs/../../../etc/passwd", "~/secrets.txt"]
)
def test_evidence_ref_path_traversal_is_rejected(bad_path: str) -> None:
    real_base_sha = _git_head_sha()
    ref = dict(_fake_repo_evidence_ref(commit_sha=real_base_sha), path=bad_path)
    with pytest.raises(rr.NativeResultAdaptationFailed) as excinfo:
        rr._verify_repo_evidence_ref_bytes(ref, repo_root=_REPO_ROOT)
    assert excinfo.value.reason_code == "native_result_evidence_path_not_repo_relative"


def test_native_result_full_pipeline_rejects_byte_unverified_evidence() -> None:
    """End-to-end (OWNER review P0-4): even when commit_sha string-matches
    ctx.base_sha exactly, a reported excerpt_sha256 that does not match the
    real git blob must still fail-close adapt_native_codebase_investigation_result
    as a whole, not just the standalone helper."""
    real_base_sha = _git_head_sha()
    ctx, plan = _run_ctx_and_plan(base_sha=real_base_sha)
    real_ref = _real_repo_evidence_ref(commit_sha=real_base_sha, path="pyproject.toml")
    tampered_ref = dict(real_ref, excerpt_sha256=hashlib.sha256(b"NOT_THE_REAL_BLOB_BYTES").hexdigest())
    native = _native_result(evidence_refs=[tampered_ref])
    with pytest.raises(rr.NativeResultAdaptationFailed) as excinfo:
        rr.adapt_native_codebase_investigation_result(
            native,
            run_id=ctx.run_id,
            base_sha=ctx.base_sha,
            source_set_digest=plan.source_set_digest,
            observer_id="codebase-investigator",
            repo_root=_REPO_ROOT,
        )
    assert excinfo.value.reason_code == "native_result_evidence_bytes_unverified"


# ---------------------------------------------------------------------------
# P1-6 (OWNER review): the observed native-fallback signal is kept
# structured, not collapsed into discovery_summary prose only
# ---------------------------------------------------------------------------


def test_converted_finding_carries_structured_fallback_metadata() -> None:
    real_base_sha = _git_head_sha()
    ctx, plan = _run_ctx_and_plan(base_sha=real_base_sha)
    real_ref = _real_repo_evidence_ref(commit_sha=real_base_sha, path="pyproject.toml")
    native = _native_result(
        evidence_refs=[real_ref],
        discovery_summary=(
            "AGY delegation failed with failure_class: agy_timeout; completed via native fallback."
        ),
    )
    converted = rr.adapt_native_codebase_investigation_result(
        native,
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=plan.source_set_digest,
        observer_id="codebase-investigator",
        repo_root=_REPO_ROOT,
    )
    finding = converted["findings"][0]
    assert finding["fallback_used"] is True
    assert finding["observed_failure_class"] == "agy_timeout"
    assert finding["investigation_route"] == "local_asset_research"
    assert finding["evidence_refs"] == [real_ref]
    assert finding["impact_scope"] == ["docs/adr/0001-architecture.md"]


def test_observed_failure_class_none_when_not_stated() -> None:
    assert rr._extract_observed_failure_class("no failure class token present here") is None
    assert rr._extract_observed_failure_class("failure_class: agy_auth_denied happened") == "agy_auth_denied"


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
    assert Path(codebase_investigator_request.json_schema_path).name == "observer_result_v1.schema.json"

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
# AC9 / P0-2 / P1-5: live consumer smoke -- drives the REAL production
# composed path (build_observer_requests -> bind_observer_prompt ->
# invoke_agent_with_role_adapter, real `subprocess.run`, real CLI argv).
# Only the AGY delegation wrapper's own result is faked (a pre-completed
# test double described in the prompt text, exactly like the existing
# `test_codebase_investigator_agy_fallback_smoke.py` pattern this reuses).
# NOT a permanent CI-required gate (Issue #2374 fallback_policy).
# ---------------------------------------------------------------------------


def _write_runtime_verification_log(*, verdict: str, reason: str, payload: dict[str, Any]) -> Path:
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = _ARTIFACTS_DIR / f"runtime-verification-AC9-{timestamp}.log"
    lines = [
        "=== Runtime Verification Log (Issue #2374 AC9, OWNER review #2387 P0-2 redesign) ===",
        f"Timestamp: {timestamp}",
        "Environment: real `claude` binary on PATH, production composed path"
        " (build_observer_requests -> bind_observer_prompt ->"
        " invoke_agent_with_role_adapter, real subprocess.run)",
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


@pytest.mark.claude_live
def test_live_consumer_smoke_agy_timeout_native_fallback_observer_acceptance() -> None:
    """GIVEN a real, bounded, PRODUCTION-composed `codebase-investigator`
    invocation (build_observer_requests -> bind_observer_prompt ->
    invoke_agent_with_role_adapter -> real `claude -p --agent
    codebase-investigator --output-format json --json-schema
    <codebase_investigation_result_v1.schema.json content> ...`, the exact
    argv/prompt-binding/schema-selection/recovery/adapter production code
    produces for this role_adapter), a fake AGY delegation wrapper failure
    (ok: false, failure_class: agy_timeout) supplied as a pre-completed test
    double described IN the task prompt text, and explicit
    agy_advisory_native_fallback_allowed: true + authoritative_base_sha
    opt-in (this Issue's actual production wiring shape, via
    bind_observer_prompt itself -- never hand-assembled by this test)
    WHEN the live SubAgent transitions to native fallback per its own
    operating instructions
    THEN invoke_agent_with_role_adapter() returns status="ok" with a
    genuinely converted, base_sha-bound EvidenceBundle/OBSERVER_RESULT_V1 --
    the SAME structured_output/result recovery, native schema validation,
    and P0-4 byte-verification code this file's hermetic tests exercise,
    never a bespoke test-only extraction/validation path.

    Per Issue #2374's narrower skip_conditions (distinct from the existing
    #2360 smoke test): SKIPs ONLY when the `claude` binary itself is absent
    from PATH. Any other live failure is a typed test FAILURE, not a SKIP.
    """
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        _write_runtime_verification_log(
            verdict="SKIP",
            reason=_ENVIRONMENT_UNAVAILABLE_MISSING_BINARY_ONLY,
            payload={"claude_bin": None},
        )
        pytest.skip(f"SKIP: {_ENVIRONMENT_UNAVAILABLE_MISSING_BINARY_ONLY}")

    authoritative_base_sha = _git_head_sha()

    # OWNER review P0-4: the investigation target MUST be a REAL, git-TRACKED
    # file that actually exists at authoritative_base_sha -- an untracked
    # sentinel file (the pre-fix_delta pattern) can never pass the new
    # independent `git show <base_sha>:<path>` byte verification
    # (_verify_repo_evidence_ref_bytes), since an untracked path has no blob
    # at any commit. `.python-version` is small, stable, and always tracked.
    target_repo_relative_path = ".python-version"
    target_content = (_REPO_ROOT / target_repo_relative_path).read_text(encoding="utf-8").strip()

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

    task_prompt = f"""(Issue #2374 runtime verification smoke, production composed path.)

## Local investigation mode input

- target_path: {target_repo_relative_path} (repo-relative, relative to this
  invocation's cwd -- the repository root)
- purpose: Read the target file and report its exact content.

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
(ok: false, failure_class: agy_timeout) and the AGY_ADVISORY_NATIVE_FALLBACK_POLICY
input above (agy_advisory_native_fallback_allowed: true and
authoritative_base_sha), carry out the native fallback under the
non-mutating investigation policy:

1. Use Read to read {target_repo_relative_path} and quote its exact content
   verbatim in your final discovery_summary.
2. Use Bash to run `git rev-parse HEAD` (read-only, non-mutating) and
   confirm it equals authoritative_base_sha above -- if it does not, you
   MUST report status: inconclusive per your own base_sha-binding
   instructions, not status: ok.
3. Use Bash to run `sha256sum {target_repo_relative_path}` (read-only,
   non-mutating) to compute the excerpt_sha256 of the file you just read.
4. You must not use Edit, Write, MultiEdit, or any Bash command that mutates
   files or git state (no writes, no git add/commit/checkout/reset, etc.).
5. Your final structured output MUST conform to your own native
   CODEBASE_INVESTIGATION_RESULT_V1 output contract (this invocation's
   --json-schema enforces that shape, not OBSERVER_RESULT_V1). Include
   every one of: schema_version, status, investigation_route, evidence_refs,
   discovery_summary, impact_scope, failure_reason, source_evidence_result.
   The single evidence_refs entry MUST be a REPO_EVIDENCE_REF_V1 object with
   EXACTLY these field names (per your own "Result:
   CODEBASE_INVESTIGATION_RESULT_V1" section and the
   gemini-cli-headless-delegation usage-contract.md SSOT it references --
   do NOT invent different field names such as "kind"/"line_range"/
   "excerpt"):
   - "type": "REPO_EVIDENCE_REF_V1"
   - "object_format": "sha1"
   - "commit_sha": authoritative_base_sha (from step 2, a 40-char hex string)
   - "path": "{target_repo_relative_path}" (REPO-RELATIVE -- exactly this
     string, NOT an absolute filesystem path)
   - "start_line": 1
   - "end_line": 1
   - "permalink":
     "https://github.com/squne121/loop-protocol/blob/<commit_sha>/{target_repo_relative_path}#L1-L1"
     (substitute the real commit_sha)
   - "excerpt_sha256": the value from step 3
   - "verification_status": "verified"
   - "verification_method": "sha256_hash_match"
   - "verified_at": current UTC timestamp, ISO 8601 (e.g.
     "2026-08-29T00:00:00Z")
   discovery_summary MUST explicitly state that the AGY delegation wrapper
   failed with failure_class: agy_timeout and that you completed this
   investigation via the native fallback route, and MUST quote the file's
   content verbatim.
"""

    run_id = f"live-smoke-{uuid.uuid4().hex}"
    source_set_digest = "live-smoke-digest"
    ctx = rr.RunContext(base_sha_resolver=lambda: authoritative_base_sha, run_id=run_id)
    plan = rr.SourcePlan(
        run_id=run_id, base_sha=authoritative_base_sha, source_set_digest=source_set_digest, sources=["repository"]
    )

    # Production composed path (OWNER review P0-2): the SAME
    # build_observer_requests()/bind_observer_prompt() a real run_cli()
    # invocation uses. The other two observers' prompts are placeholders --
    # only the codebase-investigator request is ever actually invoked below.
    bound_prompts = {
        spec.observer_id: rr.bind_observer_prompt(
            task_prompt
            if spec.observer_id == "codebase-investigator"
            else f"unused placeholder task ({spec.observer_id})",
            observer_id=spec.observer_id,
            run_id=ctx.run_id,
            base_sha=ctx.base_sha,
            source_set_digest=plan.source_set_digest,
        )
        for spec in rr.EXPECTED_OBSERVER_MANIFEST
    }
    requests = rr.build_observer_requests(
        schema_dir=_SCRIPTS_DIR / "schemas",
        cwd=str(_REPO_ROOT),
        prompts=bound_prompts,
        caller_supplied_task_path=True,
    )
    codebase_investigator_request = next(r for r in requests if r.agent_name == "codebase-investigator")
    assert codebase_investigator_request.role_adapter == rr._ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1
    assert (
        Path(codebase_investigator_request.json_schema_path).name
        == rr._CODEBASE_INVESTIGATION_RESULT_SCHEMA_FILENAME
    )

    result = rr.invoke_agent_with_role_adapter(
        codebase_investigator_request, ctx=ctx, plan=plan, runner=subprocess.run
    )

    bundle_accepted = False
    bundle: rr.EvidenceBundle | None = None
    if result.status == "ok" and isinstance(result.structured_output, dict):
        try:
            bundle = rr.EvidenceBundle(**result.structured_output)
            bundle_accepted = bundle.base_sha == authoritative_base_sha
        except Exception:  # noqa: BLE001 - captured in payload for diagnosis
            bundle_accepted = False

    payload = {
        "agent_name": codebase_investigator_request.agent_name,
        "json_schema_path": codebase_investigator_request.json_schema_path,
        "role_adapter": codebase_investigator_request.role_adapter,
        "authoritative_base_sha": authoritative_base_sha,
        "target_repo_relative_path": target_repo_relative_path,
        "target_content": target_content,
        "result_status": result.status,
        "result_reason_code": result.reason_code,
        "result_exit_code": result.exit_code,
        "structured_output": result.structured_output,
        "bundle_accepted": bundle_accepted,
    }

    findings_text = (
        json.dumps(result.structured_output.get("findings", ""), default=str) if result.structured_output else ""
    )
    content_present = bool(target_content) and target_content in findings_text
    evidence_path_present = bool(target_repo_relative_path in findings_text)

    verdict_ok = result.status == "ok" and bundle_accepted and content_present and evidence_path_present

    log_path = _write_runtime_verification_log(
        verdict="PASS" if verdict_ok else "FAIL",
        reason=(
            "native fallback executed via the production composed path,"
            " base_sha-bound evidence independently re-verified against the"
            " real git blob, and the role adapter accepted the result as a"
            " valid EvidenceBundle/OBSERVER_RESULT_V1"
            if verdict_ok
            else "one or more assertions failed -- see payload"
        ),
        payload=payload,
    )

    assert result.status == "ok", (
        f"invoke_agent_with_role_adapter did not return status=ok"
        f" (got {result.status!r}, reason_code={result.reason_code!r}); see {log_path}"
    )
    assert bundle_accepted, (
        f"converted result was not accepted as a valid, base_sha-bound EvidenceBundle; see {log_path}"
    )
    assert evidence_path_present, (
        f"target path {target_repo_relative_path!r} not found in converted findings; see {log_path}"
    )
    assert content_present, f"target file content {target_content!r} not found in converted findings; see {log_path}"
