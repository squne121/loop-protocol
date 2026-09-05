#!/usr/bin/env python3
"""Tests for `private_audit_resolver.py` (Issue #2376, #1939 Workstream 5):
opaque public `evidence_ref` -> private local audit evidence resolver.

Covers:
  AC1 `resolve()` deterministically resolves an existing typed `evidence_ref` +
      `run_identity` to private local audit evidence.
  AC2 Identity separation: `resolution_key` (stable, run_identity+evidence_ref only)
      vs. `manifest_digest` (generation-snapshot-bound); access-time re-evaluation
      never rewrites either.
  AC3 Fail-closed 2-value availability (`available`/`unavailable`) for every missing/
      malformed/digest-mismatch/permission-mismatch/expired condition.
  AC4 Atomic write (`tempfile.mkstemp()` -> `os.replace()`), `0600` permission, opaque
      relative `object_key` (never an absolute local path).
  AC5 `expires_at: RFC3339 UTC | null`, lazily evaluated at access time only.
  AC6 Reuse of the existing canonical-JSON pattern
      (`json.dumps(..., sort_keys=True, separators=(",", ":"))`) and the existing
      RFC3339 `date-time` FormatChecker (`validate_retrospective_schema.
      _validate_with_format_checking`) -- no new canonicalization/validation infra.
  AC8 The resolver's output status is always exactly 2-valued; public `evidence_ref`
      alone (without local filesystem access to `audit_root`) can never substantiate
      claim truth.

The generation-time producer hook itself (`run_retrospective.
register_private_audit_ref()`) is covered here too (imported from its sibling
module) since `test_run_retrospective.py` is outside this Issue's Allowed Paths.
"""

from __future__ import annotations

import inspect
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import private_audit_resolver as par  # noqa: E402
import validate_retrospective_schema as vrs  # noqa: E402

RUN_IDENTITY = {
    "run_id": "run-2376-test",
    "base_sha": "0123456789abcdef0123456789abcdef01234567",
    "source_set_digest": "sha256-sorted-json-v1:test-source-set-digest",
}

EVIDENCE_REF = {
    "ref_type": "repository_blob",
    "source_id": "repository",
    "resource_identity": "scripts/example.py#L1-L10",
    "projection_digest": "sha256:" + "ab" * 32,
}

REAL_FINDINGS = [{"observer_id": "repository-observer", "detail": "example real finding payload"}]


# ---------------------------------------------------------------------------
# AC1
# ---------------------------------------------------------------------------


def test_resolve_from_evidence_ref(tmp_path):
    """GIVEN a producer has registered private content for an evidence_ref
    WHEN resolve() is called with the SAME evidence_ref/run_identity
    THEN it deterministically returns status=available with a resolution_key
    matching the pure (no filesystem) resolution_key() computation."""
    audit_root = tmp_path / "audit"
    manifest = par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF,
        run_identity=RUN_IDENTITY,
        private_content=REAL_FINDINGS,
        audit_root=audit_root,
    )
    assert manifest is not None

    result = par.resolve(EVIDENCE_REF, RUN_IDENTITY, audit_root=audit_root)
    assert result.status == par.AVAILABLE
    assert result.reason_code is None
    assert result.resolution_key == par.resolution_key(RUN_IDENTITY, EVIDENCE_REF)


def test_resolve_returns_unavailable_when_never_registered(tmp_path):
    """GIVEN no producer ever registered anything for this evidence_ref
    WHEN resolve() is called
    THEN it fails closed to unavailable/not_registered (never raises, never
    fabricates availability)."""
    audit_root = tmp_path / "audit"
    result = par.resolve(EVIDENCE_REF, RUN_IDENTITY, audit_root=audit_root)
    assert result.status == par.UNAVAILABLE
    assert result.reason_code == "not_registered"


# ---------------------------------------------------------------------------
# AC2
# ---------------------------------------------------------------------------


def test_resolution_key_manifest_digest_separation(tmp_path):
    """resolution_key is stable across differing generation-snapshot fields
    (private_content/expires_at) for the SAME evidence_ref+run_identity, while
    manifest_digest differs -- and access-time evaluation never rewrites
    either digest."""
    audit_root_a = tmp_path / "a"
    audit_root_b = tmp_path / "b"

    manifest_a = par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF,
        run_identity=RUN_IDENTITY,
        private_content=REAL_FINDINGS,
        audit_root=audit_root_a,
    )
    manifest_b = par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF,
        run_identity=RUN_IDENTITY,
        private_content=[{"observer_id": "different-content-entirely"}],
        audit_root=audit_root_b,
        expires_at="2099-01-01T00:00:00Z",
    )

    assert manifest_a["resolution_key"] == manifest_b["resolution_key"]
    assert manifest_a["resolution_key"] == par.resolution_key(RUN_IDENTITY, EVIDENCE_REF)
    assert manifest_a["manifest_digest"] != manifest_b["manifest_digest"]

    # resolution_key preimage must NOT include any generation-snapshot field.
    key_fields = {"run_identity", "evidence_ref"}
    preimage_sig = inspect.signature(par.resolution_key)
    assert set(preimage_sig.parameters) == {"run_identity", "evidence_ref"}
    del key_fields

    # access-time evaluation (resolve()) never rewrites either digest.
    before = dict(manifest_a)
    par.resolve(EVIDENCE_REF, RUN_IDENTITY, audit_root=audit_root_a)
    manifest_path = par._manifest_path(audit_root_a, manifest_a["resolution_key"])  # noqa: SLF001
    after = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert after["resolution_key"] == before["resolution_key"]
    assert after["manifest_digest"] == before["manifest_digest"]


def test_resolution_key_ignores_run_identity_extra_fields_and_key_order(tmp_path):
    """A caller-supplied FULL publication run_identity (with time-varying
    generated_at/runtime_version/source_observations) and a differently
    key-ordered dict both yield the identical resolution_key -- only the
    3-field stable subset is ever part of the preimage."""
    full_run_identity = dict(RUN_IDENTITY)
    full_run_identity["generated_at"] = "2026-01-01T00:00:00Z"
    full_run_identity["runtime_version"] = "run_retrospective/v1"
    full_run_identity["source_observations"] = [{"source_id": "repository"}]

    reordered_evidence_ref = {
        "projection_digest": EVIDENCE_REF["projection_digest"],
        "resource_identity": EVIDENCE_REF["resource_identity"],
        "source_id": EVIDENCE_REF["source_id"],
        "ref_type": EVIDENCE_REF["ref_type"],
    }

    assert par.resolution_key(full_run_identity, EVIDENCE_REF) == par.resolution_key(RUN_IDENTITY, EVIDENCE_REF)
    assert par.resolution_key(RUN_IDENTITY, reordered_evidence_ref) == par.resolution_key(RUN_IDENTITY, EVIDENCE_REF)


def test_resolution_key_changes_when_evidence_ref_or_run_identity_changes():
    other_evidence_ref = dict(EVIDENCE_REF, resource_identity="scripts/other.py#L1")
    other_run_identity = dict(RUN_IDENTITY, run_id="run-2376-different")
    base = par.resolution_key(RUN_IDENTITY, EVIDENCE_REF)
    assert par.resolution_key(RUN_IDENTITY, other_evidence_ref) != base
    assert par.resolution_key(other_run_identity, EVIDENCE_REF) != base


# ---------------------------------------------------------------------------
# AC3 / AC8
# ---------------------------------------------------------------------------


def test_fail_closed_unavailable(tmp_path):
    """missing / malformed / digest-mismatch / permission-mismatch / expired
    private evidence all fold into status=unavailable with a bounded
    reason_code -- and status is NEVER a third value."""
    audit_root = tmp_path / "audit"

    # 1. never registered.
    result = par.resolve(EVIDENCE_REF, RUN_IDENTITY, audit_root=audit_root)
    assert result.status == par.UNAVAILABLE
    assert result.reason_code == "not_registered"

    manifest = par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF,
        run_identity=RUN_IDENTITY,
        private_content=REAL_FINDINGS,
        audit_root=audit_root,
    )
    object_path = audit_root / manifest["object_key"]

    # 2. source missing (object file deleted after registration).
    original_bytes = object_path.read_bytes()
    object_path.unlink()
    result = par.resolve(EVIDENCE_REF, RUN_IDENTITY, audit_root=audit_root)
    assert result.status == par.UNAVAILABLE
    assert result.reason_code == "source_missing"

    # 3. digest mismatch (object content corrupted/replaced).
    object_path.write_bytes(original_bytes + b"tampered")
    os.chmod(object_path, 0o600)
    result = par.resolve(EVIDENCE_REF, RUN_IDENTITY, audit_root=audit_root)
    assert result.status == par.UNAVAILABLE
    assert result.reason_code == "digest_mismatch"

    # 4. permission mismatch (object unreadable).
    object_path.write_bytes(original_bytes)
    os.chmod(object_path, 0o000)
    try:
        result = par.resolve(EVIDENCE_REF, RUN_IDENTITY, audit_root=audit_root)
        assert result.status == par.UNAVAILABLE
        assert result.reason_code == "permission_denied"
    finally:
        os.chmod(object_path, 0o600)

    # 5. expired.
    audit_root_expired = tmp_path / "expired"
    par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF,
        run_identity=RUN_IDENTITY,
        private_content=REAL_FINDINGS,
        audit_root=audit_root_expired,
        expires_at="2000-01-01T00:00:00Z",
    )
    result = par.resolve(EVIDENCE_REF, RUN_IDENTITY, audit_root=audit_root_expired)
    assert result.status == par.UNAVAILABLE
    assert result.reason_code == "expired"

    # 6. malformed manifest (invalid JSON on disk).
    audit_root_malformed = tmp_path / "malformed"
    par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF,
        run_identity=RUN_IDENTITY,
        private_content=REAL_FINDINGS,
        audit_root=audit_root_malformed,
    )
    rk = par.resolution_key(RUN_IDENTITY, EVIDENCE_REF)
    manifest_path = par._manifest_path(audit_root_malformed, rk)  # noqa: SLF001
    manifest_path.write_text("{not valid json", encoding="utf-8")
    result = par.resolve(EVIDENCE_REF, RUN_IDENTITY, audit_root=audit_root_malformed)
    assert result.status == par.UNAVAILABLE
    assert result.reason_code == "malformed_manifest"


@pytest.mark.parametrize(
    "reason_code",
    [None] + sorted(par.ACCESS_REASON_CODES),
)
def test_resolve_result_status_always_exactly_two_valued(reason_code, tmp_path):
    """Defensive AC8 check: ResolveResult itself refuses to be constructed
    with anything outside {available, unavailable}."""
    status = par.AVAILABLE if reason_code is None else par.UNAVAILABLE
    result = par.ResolveResult(
        status=status, reason_code=reason_code, resolution_key="sha256:" + "0" * 64, manifest_digest=None
    )
    assert result.status in par.STATUSES
    assert len(par.STATUSES) == 2

    with pytest.raises(par.PrivateAuditResolverError):
        par.ResolveResult(status="error", reason_code=None, resolution_key="sha256:" + "0" * 64, manifest_digest=None)


# ---------------------------------------------------------------------------
# AC4
# ---------------------------------------------------------------------------


def test_atomic_write_permission(tmp_path):
    """Manifest and object files are both written 0600, via
    tempfile.mkstemp()->os.replace() (no partial/temp files left behind),
    and object_key is a relative (never absolute) path."""
    audit_root = tmp_path / "audit"
    manifest = par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF,
        run_identity=RUN_IDENTITY,
        private_content=REAL_FINDINGS,
        audit_root=audit_root,
    )

    object_path = audit_root / manifest["object_key"]
    manifest_path = par._manifest_path(audit_root, manifest["resolution_key"])  # noqa: SLF001

    assert object_path.is_file()
    assert manifest_path.is_file()
    assert stat.S_IMODE(object_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600

    assert not manifest["object_key"].startswith("/")
    assert ".." not in Path(manifest["object_key"]).parts
    assert not os.path.isabs(manifest["object_key"])

    # no leftover .tmp-*.partial files from the mkstemp()->os.replace() dance.
    leftover = list(audit_root.rglob(".tmp-*"))
    assert leftover == []

    # the manifest on disk never carries an absolute local path anywhere.
    raw_text = manifest_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in raw_text
    assert str(audit_root) not in raw_text


def test_write_manifest_requires_private_content_when_available(tmp_path):
    with pytest.raises(par.PrivateAuditResolverError):
        par.write_manifest(
            audit_root=tmp_path / "audit",
            run_identity=RUN_IDENTITY,
            evidence_ref=EVIDENCE_REF,
            private_status_at_generation=par.AVAILABLE,
            reason_code=None,
            private_content=None,
        )


def test_write_manifest_unavailable_requires_bounded_reason_code(tmp_path):
    with pytest.raises(par.PrivateAuditResolverError):
        par.write_manifest(
            audit_root=tmp_path / "audit",
            run_identity=RUN_IDENTITY,
            evidence_ref=EVIDENCE_REF,
            private_status_at_generation=par.UNAVAILABLE,
            reason_code="not_a_real_reason_code",
        )

    manifest = par.write_manifest(
        audit_root=tmp_path / "audit",
        run_identity=RUN_IDENTITY,
        evidence_ref=EVIDENCE_REF,
        private_status_at_generation=par.UNAVAILABLE,
        reason_code="no_local_source_at_generation",
    )
    assert manifest["object_key"] is None
    assert manifest["object_digest"] is None


# ---------------------------------------------------------------------------
# AC5
# ---------------------------------------------------------------------------


def test_lazy_expiry(tmp_path):
    """expires_at=null never expires; a future expires_at resolves available
    until that instant; a past expires_at is unavailable/expired -- purely a
    lazy, resolve()-time computation, never a background process."""
    audit_root = tmp_path / "audit"

    manifest_no_expiry = par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF,
        run_identity=dict(RUN_IDENTITY, run_id="no-expiry"),
        private_content=REAL_FINDINGS,
        audit_root=audit_root,
        expires_at=None,
    )
    assert manifest_no_expiry["expires_at"] is None
    result = par.resolve(EVIDENCE_REF, dict(RUN_IDENTITY, run_id="no-expiry"), audit_root=audit_root)
    assert result.status == par.AVAILABLE

    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF,
        run_identity=dict(RUN_IDENTITY, run_id="future-expiry"),
        private_content=REAL_FINDINGS,
        audit_root=audit_root,
        expires_at=future,
    )
    result = par.resolve(EVIDENCE_REF, dict(RUN_IDENTITY, run_id="future-expiry"), audit_root=audit_root)
    assert result.status == par.AVAILABLE

    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF,
        run_identity=dict(RUN_IDENTITY, run_id="past-expiry"),
        private_content=REAL_FINDINGS,
        audit_root=audit_root,
        expires_at=past,
    )
    result = par.resolve(EVIDENCE_REF, dict(RUN_IDENTITY, run_id="past-expiry"), audit_root=audit_root)
    assert result.status == par.UNAVAILABLE
    assert result.reason_code == "expired"


# ---------------------------------------------------------------------------
# AC6
# ---------------------------------------------------------------------------


def test_canonical_json_reuse(tmp_path):
    """resolution_key()/manifest_digest() use the EXACT existing repo
    canonical-JSON pattern (`json.dumps(value, sort_keys=True,
    separators=(",", ":"))`, the same call
    `validate_retrospective_schema.compute_source_set_digest()` uses) rather
    than a new canonicalization scheme -- verified both by recomputing the
    digest with that literal call and by the module never defining its own
    array-reordering/JCS canonicalizer."""
    preimage = {
        "run_identity": par.normalize_run_identity(RUN_IDENTITY),
        "evidence_ref": par.normalize_evidence_ref(EVIDENCE_REF),
    }
    expected = "sha256:" + __import__("hashlib").sha256(
        json.dumps(preimage, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert par.resolution_key(RUN_IDENTITY, EVIDENCE_REF) == expected

    snapshot = {
        "private_status_at_generation": "available",
        "reason_code": None,
        "object_key": "objects/ab/abcdef.bin",
        "object_digest": "sha256:" + "cd" * 32,
        "expires_at": None,
    }
    expected_md = "sha256:" + __import__("hashlib").sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert par.manifest_digest(snapshot) == expected_md

    # no competing/duplicate JCS-style canonicalizer defined in this module.
    assert not any(name.endswith("_jcs_canonicalize") for name in vars(par))


def test_canonical_json_reuses_rfc3339_format_checker(tmp_path):
    """Manifest schema validation is delegated to
    validate_retrospective_schema's existing, module-local RFC3339
    date-time FormatChecker -- an invalid date-time in expires_at/
    generated_at is rejected the SAME way it is for every other schema in
    this skill, without this module defining a second checker."""
    manifest = par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF,
        run_identity=RUN_IDENTITY,
        private_content=REAL_FINDINGS,
        audit_root=tmp_path / "audit",
    )
    manifest["generated_at"] = "not-a-real-timestamp"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        par._validate_manifest_schema(manifest)  # noqa: SLF001

    # `_validate_manifest_schema` delegates to the SAME reused helper
    # `validate_retrospective_schema._validate_with_format_checking` uses
    # for every other schema in this skill -- verified behaviorally (both
    # reject the identical malformed date-time the identical way) rather
    # than by object identity, since each side lazy-loads its own module
    # instance via the sibling-module loader convention.
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs._validate_with_format_checking(manifest, par.load_manifest_schema())  # noqa: SLF001


# ---------------------------------------------------------------------------
# AC8 (public evidence_ref alone cannot substantiate claim truth)
# ---------------------------------------------------------------------------


def test_public_evidence_ref_cannot_substantiate_claim_truth(tmp_path):
    """A GitHub-only reader (e.g. ChatGPT) that has only the public
    evidence_ref/run_identity -- no local filesystem access to any
    audit_root -- structurally cannot determine private availability:
    resolve() REQUIRES an audit_root filesystem argument (no public-only
    overload exists), and resolution_key() alone (computable from public
    data only) never reveals private_status_at_generation/availability."""
    resolve_params = inspect.signature(par.resolve).parameters
    assert "audit_root" in resolve_params
    assert resolve_params["audit_root"].default is inspect._empty

    # Every public function that can produce an AVAILABILITY verdict
    # (i.e. returns a ResolveResult) requires local filesystem access to
    # `audit_root`. `resolution_key()` itself is the one public-data-only
    # function -- it returns a plain opaque digest STRING, never a status/
    # availability value, and takes no `audit_root` at all (asserted below).
    for name, fn in vars(par).items():
        if not inspect.isfunction(fn) or name.startswith("_"):
            continue
        source = inspect.getsource(fn)
        if "ResolveResult(" not in source:
            continue
        params = set(inspect.signature(fn).parameters)
        assert "audit_root" in params, (
            f"{name} can construct a ResolveResult without requiring local audit_root "
            "filesystem access -- would let a public-data-only reader determine availability."
        )

    resolution_key_params = set(inspect.signature(par.resolution_key).parameters)
    assert "audit_root" not in resolution_key_params
    assert resolution_key_params == {"run_identity", "evidence_ref"}

    # resolution_key is identical regardless of whether ANY local manifest
    # exists -- it carries no availability signal by itself.
    audit_root = tmp_path / "audit"
    rk_before = par.resolution_key(RUN_IDENTITY, EVIDENCE_REF)
    par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF,
        run_identity=RUN_IDENTITY,
        private_content=REAL_FINDINGS,
        audit_root=audit_root,
    )
    rk_after = par.resolution_key(RUN_IDENTITY, EVIDENCE_REF)
    assert rk_before == rk_after


# ---------------------------------------------------------------------------
# Producer hook (run_retrospective.register_private_audit_ref) -- Issue #2376
# In Scope item 2 / OWNER contract-repair blocker 1. test_run_retrospective.py
# is outside this Issue's Allowed Paths, so the hook is exercised here via the
# sibling-module loader instead.
# ---------------------------------------------------------------------------


def _run_retrospective_module():
    import importlib.util

    module_name = "agent_retrospective_run_retrospective_for_private_audit_test"
    module_path = _SCRIPTS_DIR / "run_retrospective.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def test_producer_hook_registers_only_when_local_source_already_exists(tmp_path):
    rr = _run_retrospective_module()

    # no local private source this run -> no registration (never fabricated).
    assert rr.register_private_audit_ref(EVIDENCE_REF, RUN_IDENTITY, private_content=None, audit_root=tmp_path) is None
    assert rr.register_private_audit_ref(EVIDENCE_REF, RUN_IDENTITY, private_content=[], audit_root=tmp_path) is None

    # a local private source (the real findings this run already collected)
    # already exists -> registered, and resolvable afterward.
    manifest = rr.register_private_audit_ref(
        EVIDENCE_REF, RUN_IDENTITY, private_content=REAL_FINDINGS, audit_root=tmp_path
    )
    assert manifest is not None
    assert manifest["private_status_at_generation"] == par.AVAILABLE

    result = par.resolve(EVIDENCE_REF, RUN_IDENTITY, audit_root=tmp_path)
    assert result.status == par.AVAILABLE


def test_producer_hook_never_raises_on_storage_failure(tmp_path, monkeypatch):
    """The producer hook is a best-effort local sidecar (mirrors the existing
    fail-open Latitude binding convention in execute_run()) -- a storage
    failure must never propagate and block/fail the retrospective run."""
    rr = _run_retrospective_module()

    def _boom(*args, **kwargs):
        raise OSError("simulated local storage failure")

    class _BoomingResolverModule:
        register_private_audit_ref = staticmethod(_boom)

    monkeypatch.setattr(rr, "_private_audit_resolver_module", lambda: _BoomingResolverModule())
    result = rr.register_private_audit_ref(
        EVIDENCE_REF, RUN_IDENTITY, private_content=REAL_FINDINGS, audit_root=tmp_path
    )
    assert result is None


# ---------------------------------------------------------------------------
# Producer WIRING regression (Issue #2376 fix_delta, OWNER review
# https://github.com/squne121/loop-protocol/pull/2510#issuecomment-5552512140
# blockers 1/2/3): `register_private_audit_ref()` itself (tested above) was
# already correct in isolation -- these tests exercise the ACTUAL
# `FindingSet` -> `EvaluatorRequest` -> producer conversion inside
# `execute_run()`/`run_cli()`, which is exactly what a helper-only unit test
# cannot prove (Verification Commands A/B/C).
# ---------------------------------------------------------------------------

_FULL_SHA_40 = "a" * 40


class _RedirectedResolverModule:
    """Redirects the producer hook's local-only storage writes to a
    caller-supplied `audit_root` (a pytest `tmp_path`) instead of
    `register_private_audit_ref()`'s documented default
    (`private_audit_resolver.default_audit_root(_REPO_ROOT)`, the REAL
    repository's `artifacts/agent-retrospective/private-audit` directory) --
    `execute_run()`/`run_cli()` never accept an `audit_root` parameter of
    their own, so every one of these end-to-end tests monkeypatches
    `rr._private_audit_resolver_module` to this redirector (mirroring
    `test_producer_hook_never_raises_on_storage_failure`'s existing
    monkeypatch pattern above) rather than writing to the real repo
    checkout. Delegates every actual write to this file's own already-tested
    `par.register_private_audit_ref` (the exact same module `resolve()`
    below reads back through)."""

    def __init__(self, audit_root):
        self._audit_root = audit_root

    def default_audit_root(self, repo_root):  # noqa: ARG002 - contract parity with the real module
        return self._audit_root

    def register_private_audit_ref(self, **kwargs):
        return par.register_private_audit_ref(**kwargs)


class _FakeCollectorResult:
    def __init__(self, observation, private_evidence=None):
        self.observation = observation
        self.private_evidence = private_evidence or {}


def _fake_repository_collector_result(base_sha):
    return _FakeCollectorResult(
        {
            "source_type": "repository",
            "source_id": "repository",
            "source_status": "complete",
            "pagination_completeness": "complete",
        }
    )


def _ok_agent_result(rr_mod, payload):
    return rr_mod.AgentInvocationResult(
        status="ok", structured_output=payload, raw_stdout_excerpt=None, exit_code=0, reason_code=None
    )


def _wrapper_payload(structured_output):
    """The actual `claude -p --output-format json` metadata-wrapper shape
    (Issue #2237 P0-1) -- `run_cli()`'s real subprocess adapter parses this,
    not the bare business payload."""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "assistant text summary",
        "structured_output": structured_output,
    }


def _observer_request(rr_mod, agent_name):
    return rr_mod.AgentInvocationRequest(
        agent_name=agent_name, prompt="observe", json_schema_path="/tmp/schema.json", cwd="/repo"
    )


def _make_observer_invoke(rr_mod, run_id, digest, base_sha=_FULL_SHA_40):
    def _invoke(request):
        bundle = rr_mod.EvidenceBundle(
            run_id=run_id,
            base_sha=base_sha,
            source_set_digest=digest,
            observer_id=request.agent_name,
            evidence_ref=f"evidence://{run_id}/{request.agent_name}",
            findings=[{"claim": f"finding from {request.agent_name}", "claim_class": "process"}],
        )
        return _ok_agent_result(rr_mod, json.loads(bundle.to_wire()))

    return _invoke


def _runtime_backed_judgment_candidate(*, candidate_id="cand-2376-fix-delta"):
    """A judgment-only evaluator-response candidate (Issue #2362 Scope
    Reframe wire shape) whose `evidence_refs` references the
    `retrospective-runtime-observer`'s `source_type: "runtime"` real
    evidence -- exactly the ref `_enrich_evidence_ref`/the private-audit
    producer hook must independently recompute the SAME digest for."""
    return {
        "candidate_id": candidate_id,
        "title": "producer hook regression candidate",
        "description": "exercises the private-audit producer hook wiring end-to-end",
        "claim_class": "runtime_behavior",
        "subject_ref": {"kind": "repository_path", "value": "scripts/example.py"},
        "rule_id": "example_rule",
        "evidence_refs": [
            {
                "ref_type": "runtime_receipt",
                "source_id": "runtime",
                "resource_identity": "observer:retrospective-runtime-observer",
            }
        ],
    }


def _make_evaluator_invoke(rr_mod, candidate_records):
    def _invoke(request):
        payload = {
            "schema_version": rr_mod.WIRE_SCHEMA_EVALUATION,
            "run_id": request.run_id,
            "base_sha": request.base_sha,
            "source_set_digest": request.source_set_digest,
            "candidate_records": candidate_records,
            "evidence_ref": "evidence://evaluation",
        }
        return _ok_agent_result(rr_mod, payload)

    return _invoke


def _latest_evidence_refs(candidate):
    return candidate["finding_contract"]["evaluations"][-1]["evidence_refs"]


def test_execute_run_registers_real_observer_evidence_end_to_end(tmp_path, monkeypatch):
    """Verification Command A: `execute_run()`'s real
    `FindingSet` -> `EvaluatorRequest` -> producer conversion (never
    monkeypatched/mocked here) ends with an observer evidence_ref that
    `private_audit_resolver.resolve()` reports `available` for."""
    rr_mod = _run_retrospective_module()
    audit_root = tmp_path / "audit"
    monkeypatch.setattr(rr_mod, "_private_audit_resolver_module", lambda: _RedirectedResolverModule(audit_root))
    # execute_run() unconditionally calls collect_latitude_runtime_evidence_once() --
    # this test is about the OBSERVER-evidence producer path, not Latitude
    # binding (covered separately below), so short-circuit it to "no evidence".
    monkeypatch.setattr(rr_mod, "collect_latitude_runtime_evidence_once", lambda **_kwargs: None)

    expected_digest = rr_mod.compute_source_set_digest([_fake_repository_collector_result(_FULL_SHA_40).observation])
    candidate = _runtime_backed_judgment_candidate()

    publish_request = rr_mod.execute_run(
        base_sha_resolver=lambda: _FULL_SHA_40,
        collectors=[lambda base_sha: _fake_repository_collector_result(base_sha)],
        observer_requests=[_observer_request(rr_mod, "retrospective-runtime-observer")],
        invoke=_make_observer_invoke(rr_mod, "run-2376-a", expected_digest),
        invoke_evaluator=_make_evaluator_invoke(rr_mod, [candidate]),
        repository_id="squne121/loop-protocol",
        target_issue=2376,
        request_id="req-2376-a",
        idempotency_key="idem-2376-a",
        run_id="run-2376-a",
    )

    assert len(publish_request.candidate_records) == 1
    evidence_refs = _latest_evidence_refs(publish_request.candidate_records[0])
    assert len(evidence_refs) == 1
    real_evidence_ref = evidence_refs[0]
    assert real_evidence_ref["source_id"] == "runtime"
    # the digest was Python-recomputed from real observer content, never a
    # fabricated/placeholder string (Issue #2362 Scope Reframe invariant).
    assert real_evidence_ref["projection_digest"] != ""

    run_identity = {"run_id": "run-2376-a", "base_sha": _FULL_SHA_40, "source_set_digest": expected_digest}
    result = par.resolve(real_evidence_ref, run_identity, audit_root=audit_root)
    assert result.status == par.AVAILABLE, (
        "blockers 1/2: execute_run()'s real FindingSet->EvaluatorRequest->producer "
        "conversion must actually register this run's real observer evidence, not "
        "silently no-op it."
    )


def test_run_cli_production_path_registers_real_observer_evidence(tmp_path, monkeypatch):
    """Verification Command B: the PRODUCTION `run_cli()` orchestration
    (never `execute_run()` directly) actually reaches the same producer
    hook. Agent/CLI responses are faked via `runner`/`git_runner` (the
    documented seam), but the `FindingSet` -> `EvaluatorRequest` -> producer
    conversion inside `run_cli()` itself is never mocked/monkeypatched."""
    rr_mod = _run_retrospective_module()
    audit_root = tmp_path / "audit"
    monkeypatch.setattr(rr_mod, "_private_audit_resolver_module", lambda: _RedirectedResolverModule(audit_root))

    repo_root = _SCRIPTS_DIR.parents[3]
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    (schema_dir / "observer_result_v1.schema.json").write_text("{}", encoding="utf-8")
    (schema_dir / "evaluation_result_v1.schema.json").write_text("{}", encoding="utf-8")

    def _git_runner(argv, **kwargs):  # noqa: ARG001
        return __import__("subprocess").CompletedProcess(argv, returncode=0, stdout=_FULL_SHA_40 + "\n", stderr="")

    real_observation = rr_mod.build_repository_collector(repo_root)(_FULL_SHA_40).observation
    expected_digest = rr_mod.compute_source_set_digest([real_observation])
    candidate = _runtime_backed_judgment_candidate(candidate_id="cand-2376-fix-delta-cli")

    subprocess_mod = __import__("subprocess")

    def _runner(argv, **kwargs):
        agent_name = argv[argv.index("--agent") + 1]
        if agent_name == "retrospective-evaluator":
            evaluator_request = rr_mod.EvaluatorRequest.from_wire(kwargs["input"])
            evaluation_payload = {
                "schema_version": rr_mod.WIRE_SCHEMA_EVALUATION,
                "run_id": evaluator_request.run_id,
                "base_sha": evaluator_request.base_sha,
                "source_set_digest": evaluator_request.source_set_digest,
                "candidate_records": [candidate],
                "evidence_ref": "evidence://evaluation",
            }
            return subprocess_mod.CompletedProcess(
                argv, returncode=0, stdout=json.dumps(_wrapper_payload(evaluation_payload)), stderr=""
            )
        bundle = rr_mod.EvidenceBundle(
            run_id=kwargs["env"].get("AGENT_RETROSPECTIVE_RUN_ID", ""),
            base_sha=kwargs["env"].get("AGENT_RETROSPECTIVE_BASE_SHA", ""),
            source_set_digest=expected_digest,
            observer_id=agent_name,
            evidence_ref=f"evidence://{agent_name}",
            findings=[{"claim": f"finding from {agent_name}", "claim_class": "process"}],
        )
        return subprocess_mod.CompletedProcess(
            argv, returncode=0, stdout=json.dumps(_wrapper_payload(json.loads(bundle.to_wire()))), stderr=""
        )

    publish_request = rr_mod.run_cli(
        repo_root=repo_root,
        repository_id="squne121/loop-protocol",
        target_issue=2376,
        request_id="req-2376-b",
        idempotency_key="idem-2376-b",
        schema_dir=schema_dir,
        prompts=None,
        runner=_runner,
        git_runner=_git_runner,
        run_id="run-2376-b",
        temp_base_dir=tmp_path,
    )

    assert len(publish_request.candidate_records) == 1
    evidence_refs = _latest_evidence_refs(publish_request.candidate_records[0])
    assert len(evidence_refs) == 1
    real_evidence_ref = evidence_refs[0]
    assert real_evidence_ref["source_id"] == "runtime"

    run_identity = {"run_id": "run-2376-b", "base_sha": _FULL_SHA_40, "source_set_digest": expected_digest}
    result = par.resolve(real_evidence_ref, run_identity, audit_root=audit_root)
    assert result.status == par.AVAILABLE, (
        "blocker 1: run_cli() (the production entrypoint main()/the root Skill's "
        "Procedure actually invokes) must reach the SAME private-audit producer "
        "hook execute_run() does, not silently skip it."
    )


def test_execute_run_never_registers_latitude_runtime_receipt_as_observer_evidence(tmp_path, monkeypatch):
    """Verification Command C: `bind_latitude_evidence_to_candidates()`
    appends a SECOND `runtime_receipt`/`runtime` evidence_ref onto the SAME
    evaluation (Issue #2375) alongside the evaluator's own observer-backed
    `runtime` ref (Issue #2362 enrichment). Both share `source_id ==
    "runtime"`, but only the observer-backed one may ever be registered as
    private audit evidence -- the Latitude-bound ref's `projection_digest`
    comes from an entirely different collector and has no corresponding
    local private source in THIS producer's data model, so it must resolve
    `unavailable`, never be silently backfilled with the runtime observer's
    unrelated real findings (blocker 3)."""
    rr_mod = _run_retrospective_module()
    audit_root = tmp_path / "audit"
    monkeypatch.setattr(rr_mod, "_private_audit_resolver_module", lambda: _RedirectedResolverModule(audit_root))

    vrs_mod = rr_mod._validate_retrospective_schema_module()
    collector_version = "latitude-collector/v1"
    collected_at = "2026-08-29T00:00:00Z"
    metrics = {"trace_count": 2, "span_count": 6, "duration_ms": 120}
    latitude_ref = vrs_mod.compute_latitude_evidence_ref(collector_version, dict(metrics), collected_at)
    latitude_identity = vrs_mod.compute_latitude_evidence_identity(collector_version, latitude_ref, dict(metrics))
    latitude_evidence = {
        "schema_version": "latitude_runtime_evidence/v1",
        "availability": "available",
        "collected_at": collected_at,
        "collector_version": collector_version,
        "evidence_identity": latitude_identity,
        "evidence_ref": latitude_ref,
        "metrics": metrics,
        "reason_code": None,
    }
    monkeypatch.setattr(rr_mod, "collect_latitude_runtime_evidence_once", lambda **_kwargs: latitude_evidence)

    expected_digest = rr_mod.compute_source_set_digest([_fake_repository_collector_result(_FULL_SHA_40).observation])
    candidate = _runtime_backed_judgment_candidate(candidate_id="cand-2376-latitude-negative")

    publish_request = rr_mod.execute_run(
        base_sha_resolver=lambda: _FULL_SHA_40,
        collectors=[lambda base_sha: _fake_repository_collector_result(base_sha)],
        observer_requests=[_observer_request(rr_mod, "retrospective-runtime-observer")],
        invoke=_make_observer_invoke(rr_mod, "run-2376-c", expected_digest),
        invoke_evaluator=_make_evaluator_invoke(rr_mod, [candidate]),
        repository_id="squne121/loop-protocol",
        target_issue=2376,
        request_id="req-2376-c",
        idempotency_key="idem-2376-c",
        run_id="run-2376-c",
    )

    evidence_refs = _latest_evidence_refs(publish_request.candidate_records[0])
    assert len(evidence_refs) == 2
    latitude_bound_refs = [
        ref for ref in evidence_refs if ref["projection_digest"] == latitude_identity
    ]
    observer_backed_refs = [
        ref for ref in evidence_refs if ref["projection_digest"] != latitude_identity
    ]
    assert len(latitude_bound_refs) == 1
    assert len(observer_backed_refs) == 1
    assert latitude_bound_refs[0]["source_id"] == "runtime"
    assert observer_backed_refs[0]["source_id"] == "runtime"

    run_identity = {"run_id": "run-2376-c", "base_sha": _FULL_SHA_40, "source_set_digest": expected_digest}

    # THEN the observer-backed ref (this run's OWN real evidence) resolves available.
    observer_result = par.resolve(observer_backed_refs[0], run_identity, audit_root=audit_root)
    assert observer_result.status == par.AVAILABLE

    # AND the Latitude-bound ref -- despite sharing the same source_id
    # "runtime" -- was never registered/backfilled with that unrelated
    # observer evidence: it stays unavailable/not_registered.
    latitude_result = par.resolve(latitude_bound_refs[0], run_identity, audit_root=audit_root)
    assert latitude_result.status == par.UNAVAILABLE
    assert latitude_result.reason_code == "not_registered"


# ---------------------------------------------------------------------------
# AC3/AC8 fix_delta: malformed UTF-8 manifest (Issue #2376 fix_delta, OWNER
# review issuecomment-5552512140 blocker 4). Verification Command D.
# ---------------------------------------------------------------------------


def test_resolve_folds_malformed_utf8_manifest_to_unavailable(tmp_path):
    """A manifest file containing invalid UTF-8 bytes raises
    `UnicodeDecodeError` from `TextIOWrapper.read()`/`json.load()` -- a
    `ValueError` subclass NOT caught by the pre-fix_delta
    `except (OSError, json.JSONDecodeError)` clause. `resolve()` must fold
    this into the SAME fail-closed `unavailable`/`malformed_manifest`
    outcome as every other malformed-manifest condition, never let the
    exception propagate and abort the retrospective run."""
    audit_root = tmp_path / "audit"
    par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF, run_identity=RUN_IDENTITY, private_content=REAL_FINDINGS, audit_root=audit_root
    )
    rk = par.resolution_key(RUN_IDENTITY, EVIDENCE_REF)
    manifest_path = par._manifest_path(audit_root, rk)  # noqa: SLF001
    # 0xff is never a valid UTF-8 lead byte.
    manifest_path.write_bytes(b"\xff\xfe\x00\x01not-valid-utf8-manifest-bytes")

    result = par.resolve(EVIDENCE_REF, RUN_IDENTITY, audit_root=audit_root)  # must not raise
    assert result.status == par.UNAVAILABLE
    assert result.reason_code == "malformed_manifest"


# ---------------------------------------------------------------------------
# Additional review points (Issue #2376 fix_delta, OWNER review
# issuecomment-5552512140): manifest-internal resolution_key consistency and
# object_key traversal / audit_root containment.
# ---------------------------------------------------------------------------


def test_resolve_detects_internal_run_identity_evidence_ref_inconsistency(tmp_path):
    """A manifest whose TOP-LEVEL `resolution_key` field coincidentally still
    matches the caller-supplied run_identity/evidence_ref, but whose OWN
    stored `evidence_ref` sub-object was independently corrupted (so
    recomputing `resolution_key` from the manifest's OWN stored
    run_identity/evidence_ref yields a DIFFERENT value), must fail closed to
    malformed_manifest -- the pre-fix_delta check only ever compared the
    manifest's recorded `resolution_key` field against the CALLER's freshly
    computed value, never against a value independently recomputed from the
    manifest's own stored sub-objects."""
    audit_root = tmp_path / "audit"
    manifest = par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF, run_identity=RUN_IDENTITY, private_content=REAL_FINDINGS, audit_root=audit_root
    )
    manifest_path = par._manifest_path(audit_root, manifest["resolution_key"])  # noqa: SLF001
    tampered = dict(manifest)
    tampered["evidence_ref"] = dict(tampered["evidence_ref"], resource_identity="scripts/tampered.py#L1")
    # top-level resolution_key/manifest_digest fields are left UNCHANGED --
    # only the nested evidence_ref sub-object is corrupted -- so the
    # pre-fix_delta checks (both of which only compare the top-level
    # `resolution_key` field to something else, never recompute FROM the
    # nested sub-objects) would still pass this manifest through undetected.
    manifest_path.write_text(json.dumps(tampered), encoding="utf-8")

    result = par.resolve(EVIDENCE_REF, RUN_IDENTITY, audit_root=audit_root)
    assert result.status == par.UNAVAILABLE
    assert result.reason_code == "malformed_manifest"


def test_resolve_rejects_object_key_path_traversal_outside_audit_root(tmp_path):
    """The manifest schema's `object_key` `pattern`
    (`^[A-Za-z0-9][A-Za-z0-9._/-]*$`) forbids a LEADING '..'/'/' segment but
    does not forbid an EMBEDDED '..' segment later in the string -- e.g.
    `"objects/../../outside-secret.bin"` still matches it. `resolve()` must
    independently verify the resolved `object_key` path stays contained
    within `audit_root` before ever reading it, never trust the schema
    regex alone to bound the filesystem read to `audit_root`."""
    audit_root = tmp_path / "audit"
    outside_target = tmp_path / "outside-secret.bin"
    outside_target.write_bytes(b"should never be read via an object_key path traversal")

    manifest = par.register_private_audit_ref(
        evidence_ref=EVIDENCE_REF, run_identity=RUN_IDENTITY, private_content=REAL_FINDINGS, audit_root=audit_root
    )
    manifest_path = par._manifest_path(audit_root, manifest["resolution_key"])  # noqa: SLF001
    tampered = dict(manifest)
    traversal_key = "objects/../../outside-secret.bin"
    tampered["object_key"] = traversal_key
    # recompute object_digest to match the traversal target's REAL bytes so
    # only the containment check -- not the pre-existing digest_mismatch
    # check -- can catch this.
    import hashlib as _hashlib

    tampered["object_digest"] = "sha256:" + _hashlib.sha256(outside_target.read_bytes()).hexdigest()
    generation_snapshot = {
        "private_status_at_generation": tampered["private_status_at_generation"],
        "reason_code": tampered["reason_code"],
        "object_key": tampered["object_key"],
        "object_digest": tampered["object_digest"],
        "expires_at": tampered["expires_at"],
    }
    tampered["manifest_digest"] = par.manifest_digest(generation_snapshot)
    manifest_path.write_text(json.dumps(tampered), encoding="utf-8")

    result = par.resolve(EVIDENCE_REF, RUN_IDENTITY, audit_root=audit_root)
    assert result.status == par.UNAVAILABLE
    assert result.reason_code == "malformed_manifest"
    assert not outside_target.read_bytes() == b""  # sanity: target file untouched, never consumed


# ---------------------------------------------------------------------------
# schema fixtures load/validate cleanly (closed schema, AC7 companion coverage
# -- the dedicated public-safety parametrized suite lives in
# test_public_safe_evidence_refs.py per this Issue's Verification Commands).
# ---------------------------------------------------------------------------


def test_valid_fixture_passes_schema_validation():
    fixture = vrs.load_fixture("retro_private_audit_index_v1.valid.json")
    par._validate_manifest_schema(fixture)  # noqa: SLF001


def test_invalid_fixture_fails_schema_validation():
    fixture = vrs.load_fixture("retro_private_audit_index_v1.invalid.json")
    with pytest.raises(jsonschema.exceptions.ValidationError):
        par._validate_manifest_schema(fixture)  # noqa: SLF001
