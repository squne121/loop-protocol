#!/usr/bin/env python3
"""manifest_schema.py — AUTHORIZATION_TCB_MANIFEST_V1 schema and validator.

This module defines the schema and validation logic for the external trust
manifest consumed by ``trusted_hook_launcher.py``. The manifest is the single
atomic binding between a trusted commit OID and the SHA-256 digests of the
components (adapter / policy / analyzer / composite scripts) that are allowed
to run as part of the publish-lane authorization TCB (Trusted Computing Base).

Design notes (Issue #1454):

- The manifest is produced by a privileged operator's ``install_trust_root.sh``
  invocation, from the tree of ``trusted_commit_oid``. It is NEVER generated
  from an arbitrary working tree, and it is NEVER generated or mutated by an
  agent running inside the candidate repository.
- ``issued_by`` is audit metadata ONLY. It records who ran the installer for
  traceability. It MUST NOT be treated as an authenticity/trust signal by any
  consumer of this schema — the trust anchor is the manifest's placement on a
  candidate-repository-external, agent-write-denied filesystem path (enforced
  by the installer + trusted_hook_launcher, not by this schema module).
- Digest contract (fixed, non-negotiable):
    * sha256 over raw file bytes (no normalization of any kind)
    * lowercase hex, exactly 64 characters
    * component MUST be a regular file (symlinks are rejected upstream by the
      installer / launcher — this module only validates the string shape)
    * ``path`` is a POSIX, repository-relative path (no leading ``/``, no
      ``..`` traversal segments, no backslash, no NUL)
    * no line-ending normalization, no unicode normalization
- unknown top-level keys, missing required keys, missing/duplicate component
  entries are all deny conditions (fail-closed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MANIFEST_SCHEMA_VERSION = "AUTHORIZATION_TCB_MANIFEST_V1"

REQUIRED_TOP_LEVEL_KEYS = frozenset(
    {
        "manifest_version",
        "repository",
        "trusted_commit_oid",
        "components",
        "issued_by",
        "generation",
    }
)

REQUIRED_COMPONENT_KEYS = frozenset({"path", "sha256"})

_FULL_HEX_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
# Repository-relative POSIX path: no leading '/', no backslash, no NUL,
# no '..' traversal segment, no empty segments.
_PATH_TRAVERSAL_SEGMENTS = {"..", ""}


# ─── Reason codes (manifest-level validation) ────────────────────────────────

REASON_UNKNOWN_KEY = "authorization_manifest_invalid_unknown_key"
REASON_MISSING_KEY = "authorization_manifest_invalid_missing_key"
REASON_INVALID_MANIFEST_VERSION = "authorization_manifest_invalid_version"
REASON_INVALID_REPOSITORY = "authorization_manifest_invalid_repository"
REASON_INVALID_TRUSTED_COMMIT_OID = "authorization_manifest_invalid_trusted_commit_oid"
REASON_INVALID_GENERATION = "authorization_manifest_invalid_generation"
REASON_INVALID_ISSUED_BY = "authorization_manifest_invalid_issued_by"
REASON_COMPONENTS_NOT_LIST = "authorization_manifest_invalid_components_type"
REASON_COMPONENT_MISSING_KEY = "authorization_component_missing_field"
REASON_COMPONENT_UNKNOWN_KEY = "authorization_component_unknown_field"
REASON_COMPONENT_INVALID_PATH = "authorization_component_invalid_path"
REASON_COMPONENT_INVALID_SHA256 = "authorization_component_invalid_sha256"
REASON_DUPLICATE_COMPONENT = "authorization_component_duplicate"
REASON_EMPTY_COMPONENTS = "authorization_manifest_invalid_empty_components"
REASON_NOT_A_MAPPING = "authorization_manifest_invalid_not_a_mapping"


@dataclass(frozen=True)
class ManifestComponent:
    path: str
    sha256: str


@dataclass(frozen=True)
class ManifestValidationResult:
    ok: bool
    reason_code: str | None = None
    detail: str | None = None
    manifest_version: str | None = None
    repository: str | None = None
    trusted_commit_oid: str | None = None
    generation: int | None = None
    issued_by: str | None = None
    components: tuple[ManifestComponent, ...] = field(default_factory=tuple)


def _is_valid_component_path(path: object) -> bool:
    if not isinstance(path, str) or not path:
        return False
    if path.startswith("/"):
        return False
    if "\\" in path or "\x00" in path:
        return False
    segments = path.split("/")
    if any(seg in _PATH_TRAVERSAL_SEGMENTS for seg in segments):
        return False
    return True


def _validate_component(raw: object) -> tuple[ManifestComponent | None, str | None, str | None]:
    if not isinstance(raw, dict):
        return None, REASON_COMPONENT_MISSING_KEY, "component entry is not a mapping"

    unknown_keys = set(raw.keys()) - REQUIRED_COMPONENT_KEYS
    if unknown_keys:
        return None, REASON_COMPONENT_UNKNOWN_KEY, f"unknown component keys: {sorted(unknown_keys)}"

    missing_keys = REQUIRED_COMPONENT_KEYS - set(raw.keys())
    if missing_keys:
        return None, REASON_COMPONENT_MISSING_KEY, f"missing component keys: {sorted(missing_keys)}"

    path = raw["path"]
    sha256 = raw["sha256"]

    if not _is_valid_component_path(path):
        return None, REASON_COMPONENT_INVALID_PATH, f"invalid component path: {path!r}"

    if not isinstance(sha256, str) or not _SHA256_HEX_RE.fullmatch(sha256):
        return None, REASON_COMPONENT_INVALID_SHA256, "sha256 must be 64 lowercase hex chars"

    return ManifestComponent(path=path, sha256=sha256), None, None


def validate_manifest(data: object) -> ManifestValidationResult:
    """Validate a decoded manifest mapping against AUTHORIZATION_TCB_MANIFEST_V1.

    Fail-closed: any deviation from the fixed schema (unknown key, missing
    component, duplicate component, malformed OID/sha256/generation) returns
    ``ok=False`` with a deterministic ``reason_code``. This function performs
    no filesystem or Git access — it is a pure structural/schema validator.
    """
    if not isinstance(data, dict):
        return ManifestValidationResult(ok=False, reason_code=REASON_NOT_A_MAPPING, detail="manifest is not a mapping")

    unknown_keys = set(data.keys()) - REQUIRED_TOP_LEVEL_KEYS
    if unknown_keys:
        return ManifestValidationResult(
            ok=False,
            reason_code=REASON_UNKNOWN_KEY,
            detail=f"unknown top-level keys: {sorted(unknown_keys)}",
        )

    missing_keys = REQUIRED_TOP_LEVEL_KEYS - set(data.keys())
    if missing_keys:
        return ManifestValidationResult(
            ok=False,
            reason_code=REASON_MISSING_KEY,
            detail=f"missing top-level keys: {sorted(missing_keys)}",
        )

    manifest_version = data["manifest_version"]
    if manifest_version != MANIFEST_SCHEMA_VERSION:
        return ManifestValidationResult(
            ok=False,
            reason_code=REASON_INVALID_MANIFEST_VERSION,
            detail=f"expected manifest_version={MANIFEST_SCHEMA_VERSION!r}, got {manifest_version!r}",
        )

    repository = data["repository"]
    if not isinstance(repository, str) or not repository:
        return ManifestValidationResult(
            ok=False, reason_code=REASON_INVALID_REPOSITORY, detail="repository must be a non-empty string"
        )

    trusted_commit_oid = data["trusted_commit_oid"]
    if not isinstance(trusted_commit_oid, str) or not _FULL_HEX_OID_RE.fullmatch(trusted_commit_oid):
        return ManifestValidationResult(
            ok=False,
            reason_code=REASON_INVALID_TRUSTED_COMMIT_OID,
            detail="trusted_commit_oid must be full 40-hex commit OID",
        )

    generation = data["generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        return ManifestValidationResult(
            ok=False,
            reason_code=REASON_INVALID_GENERATION,
            detail="generation must be a non-negative integer (monotonic release epoch)",
        )

    issued_by = data["issued_by"]
    if not isinstance(issued_by, str) or not issued_by:
        return ManifestValidationResult(
            ok=False, reason_code=REASON_INVALID_ISSUED_BY, detail="issued_by must be a non-empty string"
        )

    components_raw = data["components"]
    if not isinstance(components_raw, list):
        return ManifestValidationResult(
            ok=False, reason_code=REASON_COMPONENTS_NOT_LIST, detail="components must be a list"
        )

    if not components_raw:
        return ManifestValidationResult(
            ok=False, reason_code=REASON_EMPTY_COMPONENTS, detail="components must not be empty"
        )

    seen_paths: set[str] = set()
    components: list[ManifestComponent] = []
    for raw_component in components_raw:
        component, reason_code, detail = _validate_component(raw_component)
        if component is None:
            return ManifestValidationResult(ok=False, reason_code=reason_code, detail=detail)
        if component.path in seen_paths:
            return ManifestValidationResult(
                ok=False,
                reason_code=REASON_DUPLICATE_COMPONENT,
                detail=f"duplicate component path: {component.path}",
            )
        seen_paths.add(component.path)
        components.append(component)

    return ManifestValidationResult(
        ok=True,
        manifest_version=manifest_version,
        repository=repository,
        trusted_commit_oid=trusted_commit_oid,
        generation=generation,
        issued_by=issued_by,
        components=tuple(components),
    )
