#!/usr/bin/env python3
"""private_audit_resolver.py -- Issue #2376 (#1939 Workstream 5): resolves an
existing public-safe ``agent_improvement_candidate_v1`` ``evidence_ref`` +
publication ``run_identity`` (``run_id``/``base_sha``/``source_set_digest``)
to PRIVATE local audit evidence, deterministically, with fail-closed
availability semantics and local-only storage.

This module is intentionally split into two orthogonal identity layers
(Issue #2376 AC2, per the OWNER contract-repair anchor comment):

- ``resolution_key()`` -- a STABLE identity derived ONLY from ``run_identity``
  (``run_id``/``base_sha``/``source_set_digest``) + ``evidence_ref``
  (``ref_type``/``source_id``/``resource_identity``/``projection_digest``).
  Calling it twice with the same (structurally-equal) inputs always returns
  the same value; time-varying generation-snapshot fields (private
  availability, reason code, storage location, expiry) are NEVER part of its
  preimage.
- ``manifest_digest()`` -- binds the separate, time-varying generation
  snapshot (``private_status_at_generation``/``reason_code``/``object_key``/
  ``object_digest``/``expires_at``).

Access-time re-evaluation (``resolve()``) NEVER rewrites either digest -- it
only computes a transient, non-persisted ``effective_status``/
``effective_reason_code`` in memory each call (AC2/AC3).

Availability is exactly 2-valued (``available`` | ``unavailable``, AC3/AC8):
missing / malformed / digest-mismatch / permission-mismatch / expired all
fold into fail-closed ``unavailable``. This is a DELIBERATE simplification
scoped to this module only -- the sibling ``latitude_runtime_evidence/v1``
contract's 3-value (``available`` | ``unavailable`` | ``error``) semantics
(``.claude/skills/agent-retrospective/references/wire-contract.md``) is left
untouched.

Local-only storage (this module's own read/write of it is explicitly IN
SCOPE core functionality, not a Stop Condition -- Issue #2376 Stop
Conditions): atomic write via ``tempfile.mkstemp()`` -> write ->
``os.replace()``, restrictive ``0600`` file permission, and an OPAQUE
relative ``object_key`` (never an absolute local path) stored in the
manifest. Canonical JSON serialization reuses the exact
``json.dumps(value, sort_keys=True, separators=(",", ":"))`` pattern already
used throughout this skill (e.g. ``validate_retrospective_schema.
compute_source_set_digest``/``compute_latitude_evidence_ref``) rather than
adding a new canonicalization implementation (AC6); RFC3339 ``date-time``
format checking is reused from ``validate_retrospective_schema.
_validate_with_format_checking`` (the same module-local, stdlib-only
``date-time`` FormatChecker every other schema in this skill already uses)
rather than a new validator (AC6).

Producer: see ``run_retrospective.register_private_audit_ref()`` (a small
generation-time sidecar hook, mandatory per the OWNER contract-repair
blocker -- a resolver with no producer can only ever return
``unavailable``). This module never fabricates a producer of its own; it
only accepts already-real ``private_content`` a caller passed in.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parents[4]

SCHEMA_VERSION = "retro_private_audit_index/v1"
_SCHEMA_PATH = _SCRIPTS_DIR.parent / "schemas" / "retro_private_audit_index_v1.schema.json"

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
STATUSES = frozenset({AVAILABLE, UNAVAILABLE})

#: Bounded generation-time reason codes (persisted in the manifest, AC5).
GENERATION_REASON_CODES = frozenset({"no_local_source_at_generation"})

#: Bounded access-time reason codes (transient, NEVER persisted -- AC2/AC3).
ACCESS_REASON_CODES = frozenset(
    {
        "not_registered",
        "malformed_manifest",
        "source_missing",
        "digest_mismatch",
        "permission_denied",
        "expired",
    }
)

_RUN_IDENTITY_FIELDS = ("run_id", "base_sha", "source_set_digest")
_EVIDENCE_REF_FIELDS = ("ref_type", "source_id", "resource_identity", "projection_digest")


class PrivateAuditResolverError(ValueError):
    """Raised for malformed inputs to this module's public functions."""


# ---------------------------------------------------------------------------
# sibling module loading (mirrors run_retrospective.py's own
# _load_module_from_path/_load_sibling_module lazy-loader convention, so this
# module never requires validate_retrospective_schema.py to be importable
# unless a caller actually needs schema validation/format checking).
# ---------------------------------------------------------------------------


def _load_module_from_path(module_name: str, module_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load module {module_name} from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _validate_retrospective_schema_module():
    return _load_module_from_path(
        "agent_retrospective_private_audit_validate_schema",
        _SCRIPTS_DIR / "validate_retrospective_schema.py",
    )


def load_manifest_schema() -> dict[str, Any]:
    with _SCHEMA_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _validate_manifest_schema(instance: dict[str, Any]) -> None:
    """Reuse the existing RFC3339 ``date-time`` FormatChecker (AC6) -- never a
    new validator -- by delegating to ``validate_retrospective_schema.
    _validate_with_format_checking()``, the exact same generic
    ``(instance, schema)`` helper every other schema in this skill
    (``agent_retrospective_run/v1``/``agent_improvement_candidate/v1``/
    ``latitude_runtime_evidence/v1``) is validated through."""
    vrs = _validate_retrospective_schema_module()
    vrs._validate_with_format_checking(instance, load_manifest_schema())  # noqa: SLF001


# ---------------------------------------------------------------------------
# canonical JSON (reused pattern, AC6 -- see module docstring)
# ---------------------------------------------------------------------------


def _canonical_json_bytes(value: Any) -> bytes:
    """Encode `value` exactly like every other canonical-JSON digest input in
    this skill: ``json.dumps(value, sort_keys=True, separators=(",", ":"))``
    (see e.g. ``validate_retrospective_schema.compute_source_set_digest``) --
    the identical stdlib call, not a new/competing canonicalization
    algorithm."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# identity separation (AC2)
# ---------------------------------------------------------------------------


def _project_fields(value: Any, fields: tuple[str, ...], *, what: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise PrivateAuditResolverError(f"{what} must be a dict, got {type(value).__name__}")
    projected: dict[str, str] = {}
    for field_name in fields:
        raw = value.get(field_name)
        if not isinstance(raw, str) or not raw:
            raise PrivateAuditResolverError(f"{what}.{field_name} must be a non-empty string")
        projected[field_name] = raw
    return projected


def normalize_run_identity(run_identity: dict[str, Any]) -> dict[str, str]:
    """Project ONLY the stable 3-field identity subset (``run_id``/
    ``base_sha``/``source_set_digest``) out of a caller-supplied
    ``run_identity`` dict. A caller may pass the FULL publication
    ``run_identity`` (which also carries time-varying ``generated_at``/
    ``runtime_version``/``source_observations`` -- see
    ``references/wire-contract.md``); those extra fields are silently
    ignored here rather than rejected, since they are legitimately present
    on the production object this function is fed, but they must never
    reach the ``resolution_key`` preimage (AC2)."""
    return _project_fields(run_identity, _RUN_IDENTITY_FIELDS, what="run_identity")


def normalize_evidence_ref(evidence_ref: dict[str, Any]) -> dict[str, str]:
    """Project the exact 4-field public-safe ``evidence_ref`` shape (mirrors
    ``agent_improvement_candidate_v1.schema.json``'s ``$defs.evidence_ref``)."""
    return _project_fields(evidence_ref, _EVIDENCE_REF_FIELDS, what="evidence_ref")


def resolution_key(run_identity: dict[str, Any], evidence_ref: dict[str, Any]) -> str:
    """Deterministic, stable identity (AC2): ``sha256(canonical-json({
    run_identity: {run_id, base_sha, source_set_digest}, evidence_ref:
    {ref_type, source_id, resource_identity, projection_digest}}))``.
    Independent of caller-supplied dict key order (canonical JSON sorts
    keys) and of any extra fields present on a full ``run_identity``/
    ``evidence_ref`` object (only the documented subset is projected first).
    Never includes ``private_status_at_generation``/``reason_code``/
    ``object_key``/``object_digest``/``expires_at``."""
    preimage = {
        "run_identity": normalize_run_identity(run_identity),
        "evidence_ref": normalize_evidence_ref(evidence_ref),
    }
    return "sha256:" + _sha256_hex(_canonical_json_bytes(preimage))


def manifest_digest(generation_snapshot: dict[str, Any]) -> str:
    """Digest binding the generation-time snapshot (AC2) --
    ``private_status_at_generation``/``reason_code``/``object_key``/
    ``object_digest``/``expires_at``. This is a SEPARATE digest from
    ``resolution_key`` on purpose: recomputing it never mutates, and is
    never used to derive, ``resolution_key``."""
    return "sha256:" + _sha256_hex(_canonical_json_bytes(generation_snapshot))


# ---------------------------------------------------------------------------
# local-only storage layout (never an absolute path in the manifest, AC4)
# ---------------------------------------------------------------------------


def default_audit_root(repo_root: Path | None = None) -> Path:
    """Default local-only (gitignored, ``artifacts/`` -- see repo root
    ``.gitignore``) audit root. Purely a convenience default; every public
    function in this module also accepts an explicit ``audit_root`` so
    tests never depend on this default location."""
    root = repo_root if repo_root is not None else _REPO_ROOT
    return root / "artifacts" / "agent-retrospective" / "private-audit"


def _manifest_path(audit_root: Path, rk: str) -> Path:
    rk_hex = rk.split(":", 1)[1] if ":" in rk else rk
    return audit_root / "manifests" / f"{rk_hex}.json"


def _object_relative_key(object_digest_hex: str) -> str:
    return f"objects/{object_digest_hex[:2]}/{object_digest_hex}.bin"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """``tempfile.mkstemp()`` -> write -> ``os.replace()`` (AC4), restrictive
    ``0600`` permission on the final file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".partial")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, str(path))
    finally:
        if os.path.exists(tmp_name):  # pragma: no cover - defensive cleanup on write failure
            os.remove(tmp_name)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# write path (AC1/AC4/AC5)
# ---------------------------------------------------------------------------


def write_manifest(
    *,
    audit_root: Path,
    run_identity: dict[str, Any],
    evidence_ref: dict[str, Any],
    private_status_at_generation: str,
    reason_code: str | None,
    private_content: bytes | Any | None = None,
    expires_at: str | None = None,
    clock: Any = _utcnow_iso,
) -> dict[str, Any]:
    """Low-level storage write (AC1/AC4/AC5). Prefer
    ``register_private_audit_ref()`` from a producer hook -- this function
    is the primitive it (and direct unit tests) build on.

    When ``private_status_at_generation == "available"``, ``private_content``
    MUST be provided (bytes, or any JSON-serializable value which is then
    canonical-JSON-encoded the same way ``resolution_key``/``manifest_digest``
    are) and is atomically written to a content-addressable object file under
    ``audit_root`` with ``0600`` permission; the manifest stores only an
    OPAQUE relative ``object_key`` (never ``audit_root`` itself, never an
    absolute path) plus the object's ``sha256`` digest.

    When ``private_status_at_generation == "unavailable"``, no object file is
    written and ``object_key``/``object_digest`` are ``null`` -- callers must
    supply a ``reason_code`` from ``GENERATION_REASON_CODES``.
    """
    if private_status_at_generation not in STATUSES:
        raise PrivateAuditResolverError(
            f"private_status_at_generation must be one of {sorted(STATUSES)!r}, "
            f"got {private_status_at_generation!r}"
        )

    rk = resolution_key(run_identity, evidence_ref)

    if private_status_at_generation == AVAILABLE:
        if reason_code is not None:
            raise PrivateAuditResolverError("reason_code must be null when private_status_at_generation=available")
        if private_content is None:
            raise PrivateAuditResolverError("private_content is required when private_status_at_generation=available")
        content_bytes = private_content if isinstance(private_content, bytes) else _canonical_json_bytes(
            private_content
        )
        object_digest_hex = _sha256_hex(content_bytes)
        object_key = _object_relative_key(object_digest_hex)
        _atomic_write_bytes(audit_root / object_key, content_bytes)
        object_digest = "sha256:" + object_digest_hex
    else:
        if reason_code not in GENERATION_REASON_CODES:
            raise PrivateAuditResolverError(
                f"reason_code must be one of {sorted(GENERATION_REASON_CODES)!r} "
                "when private_status_at_generation=unavailable"
            )
        object_key = None
        object_digest = None

    generation_snapshot = {
        "private_status_at_generation": private_status_at_generation,
        "reason_code": reason_code,
        "object_key": object_key,
        "object_digest": object_digest,
        "expires_at": expires_at,
    }
    md = manifest_digest(generation_snapshot)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "resolution_key": rk,
        "manifest_digest": md,
        "run_identity": normalize_run_identity(run_identity),
        "evidence_ref": normalize_evidence_ref(evidence_ref),
        "generated_at": clock() if callable(clock) else clock,
        **generation_snapshot,
    }
    _validate_manifest_schema(manifest)

    _atomic_write_bytes(_manifest_path(audit_root, rk), _canonical_json_bytes(manifest))
    return manifest


def register_private_audit_ref(
    *,
    evidence_ref: dict[str, Any],
    run_identity: dict[str, Any],
    private_content: Any,
    audit_root: Path,
    expires_at: str | None = None,
) -> dict[str, Any] | None:
    """Producer-facing entry point (called by
    ``run_retrospective.register_private_audit_ref()``'s generation-time
    sidecar hook). Registers a private-audit manifest mapping ONLY when
    ``private_content`` is truthy, i.e. a local private source already
    exists this run for this ``evidence_ref`` -- this is the ONLY code path
    that ever writes ``private_status_at_generation: "available"`` in
    production. Returns ``None`` (no-op, no manifest written) when
    ``private_content`` is falsy, matching the In Scope requirement that the
    producer hook registers a sidecar mapping ONLY for evidence whose local
    private source already exists at generation time (never fabricating
    one)."""
    if not private_content:
        return None
    return write_manifest(
        audit_root=audit_root,
        run_identity=run_identity,
        evidence_ref=evidence_ref,
        private_status_at_generation=AVAILABLE,
        reason_code=None,
        private_content=private_content,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# read / resolve path (AC1/AC2/AC3/AC5/AC8)
# ---------------------------------------------------------------------------


class ResolveResult:
    """Plain, dependency-free result value (kept as a simple class rather
    than a dataclass to avoid this module depending on `dataclasses` for a
    single tiny value type). ``status`` is always exactly ``available`` or
    ``unavailable`` (AC8) -- never a third value."""

    __slots__ = ("status", "reason_code", "resolution_key", "manifest_digest")

    def __init__(
        self,
        *,
        status: str,
        reason_code: str | None,
        resolution_key: str,
        manifest_digest: str | None,
    ) -> None:
        if status not in STATUSES:
            raise PrivateAuditResolverError(f"status must be one of {sorted(STATUSES)!r}, got {status!r}")
        self.status = status
        self.reason_code = reason_code
        self.resolution_key = resolution_key
        self.manifest_digest = manifest_digest

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return (
            f"ResolveResult(status={self.status!r}, reason_code={self.reason_code!r}, "
            f"resolution_key={self.resolution_key!r}, manifest_digest={self.manifest_digest!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ResolveResult):
            return NotImplemented
        return (
            self.status == other.status
            and self.reason_code == other.reason_code
            and self.resolution_key == other.resolution_key
            and self.manifest_digest == other.manifest_digest
        )


def _unavailable(rk: str, reason_code: str) -> ResolveResult:
    return ResolveResult(status=UNAVAILABLE, reason_code=reason_code, resolution_key=rk, manifest_digest=None)


def resolve(
    evidence_ref: dict[str, Any],
    run_identity: dict[str, Any],
    *,
    audit_root: Path,
    now: datetime | None = None,
) -> ResolveResult:
    """Resolve ONE public-safe ``evidence_ref`` + ``run_identity`` to a
    private-local-audit availability verdict (AC1/AC3/AC8). Requires local
    filesystem access to `audit_root` -- there is deliberately no variant of
    this function reachable from ``evidence_ref``/``run_identity`` alone
    (a GitHub-only reader with no access to `audit_root` structurally cannot
    call this, AC3's "public evidence_ref alone must never be sufficient to
    substantiate claim truth").

    Fail-closed 2-value output only (AC3/AC8): missing manifest, malformed
    manifest, a `manifest_digest` that does not match the stored generation
    snapshot, a missing/unreadable/digest-mismatched object file, and an
    expired ``expires_at`` all fold into ``unavailable`` with a bounded
    ``reason_code`` -- never a third status, never an exception escaping to
    the caller for any of these expected fail-closed conditions.

    NEVER writes anything (this function performs local-only STORAGE READS
    only) and NEVER rewrites ``resolution_key``/``manifest_digest`` -- the
    ``effective_status``/``reason_code`` this function returns exist only in
    the returned, non-persisted `ResolveResult` (AC2)."""
    rk = resolution_key(run_identity, evidence_ref)
    manifest_path = _manifest_path(audit_root, rk)

    if not manifest_path.is_file():
        return _unavailable(rk, "not_registered")

    try:
        with manifest_path.open("r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return _unavailable(rk, "malformed_manifest")

    if not isinstance(manifest, dict):
        return _unavailable(rk, "malformed_manifest")

    try:
        _validate_manifest_schema(manifest)
    except Exception:
        return _unavailable(rk, "malformed_manifest")

    generation_snapshot = {
        "private_status_at_generation": manifest.get("private_status_at_generation"),
        "reason_code": manifest.get("reason_code"),
        "object_key": manifest.get("object_key"),
        "object_digest": manifest.get("object_digest"),
        "expires_at": manifest.get("expires_at"),
    }
    if manifest_digest(generation_snapshot) != manifest.get("manifest_digest"):
        return _unavailable(rk, "malformed_manifest")

    if manifest.get("resolution_key") != rk:
        # Defensive: a manifest filed under this resolution_key's own path
        # but whose own recorded resolution_key disagrees is corrupted --
        # never trust the filename alone.
        return _unavailable(rk, "malformed_manifest")

    if manifest["private_status_at_generation"] == UNAVAILABLE:
        return _unavailable(rk, manifest["reason_code"])

    expires_at = manifest.get("expires_at")
    if expires_at is not None:
        current = now if now is not None else datetime.now(timezone.utc)
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if expiry.tzinfo is None:  # pragma: no cover - defensive, schema requires RFC3339
            expiry = expiry.replace(tzinfo=timezone.utc)
        if current >= expiry:
            return _unavailable(rk, "expired")

    object_key = manifest["object_key"]
    object_path = audit_root / object_key
    if not object_path.is_file():
        return _unavailable(rk, "source_missing")

    if not os.access(object_path, os.R_OK):
        return _unavailable(rk, "permission_denied")

    try:
        content = object_path.read_bytes()
    except PermissionError:
        return _unavailable(rk, "permission_denied")
    except OSError:
        return _unavailable(rk, "source_missing")

    if "sha256:" + _sha256_hex(content) != manifest["object_digest"]:
        return _unavailable(rk, "digest_mismatch")

    return ResolveResult(
        status=AVAILABLE,
        reason_code=None,
        resolution_key=rk,
        manifest_digest=manifest["manifest_digest"],
    )
