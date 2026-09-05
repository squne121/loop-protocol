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
