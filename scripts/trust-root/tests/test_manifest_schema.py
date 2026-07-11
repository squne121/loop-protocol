"""Tests for scripts/trust-root/manifest_schema.py (AUTHORIZATION_TCB_MANIFEST_V1)."""

from __future__ import annotations

import sys
from pathlib import Path

_TRUST_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_TRUST_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_TRUST_ROOT_DIR))

import manifest_schema as ms  # noqa: E402

VALID_OID = "a" * 40
VALID_SHA1 = "b" * 64
VALID_SHA2 = "c" * 64


def _base_manifest() -> dict:
    return {
        "manifest_version": "AUTHORIZATION_TCB_MANIFEST_V1",
        "repository": "squne121/loop-protocol",
        "trusted_commit_oid": VALID_OID,
        "components": [
            {"path": "scripts/agent-guards/codex-hook-adapter.mjs", "sha256": VALID_SHA1},
            {"path": "scripts/agent-guards/git_mutation_command_policy.py", "sha256": VALID_SHA2},
        ],
        "issued_by": "operator@bastion-01",
        "generation": 1,
    }


def test_valid_manifest_atomic_binding_accepted() -> None:
    """GIVEN a fully valid manifest, WHEN validated, THEN it is accepted and all
    fields (commit oid, generation, component digests) are atomically bound
    into a single ManifestValidationResult (manifest_atomic_binding)."""
    result = ms.validate_manifest(_base_manifest())

    assert result.ok is True
    assert result.reason_code is None
    assert result.trusted_commit_oid == VALID_OID
    assert result.generation == 1
    assert len(result.components) == 2
    assert result.components[0].path == "scripts/agent-guards/codex-hook-adapter.mjs"
    assert result.components[0].sha256 == VALID_SHA1


def test_unknown_top_level_key_denied() -> None:
    """GIVEN a manifest with an unexpected top-level key, WHEN validated, THEN
    it is denied (manifest_atomic_binding fail-closed on unknown key)."""
    manifest = _base_manifest()
    manifest["unexpected_extra_field"] = "sneaky"

    result = ms.validate_manifest(manifest)

    assert result.ok is False
    assert result.reason_code == ms.REASON_UNKNOWN_KEY


def test_missing_required_key_denied() -> None:
    """GIVEN a manifest missing a required top-level key, WHEN validated, THEN
    it is denied."""
    manifest = _base_manifest()
    del manifest["generation"]

    result = ms.validate_manifest(manifest)

    assert result.ok is False
    assert result.reason_code == ms.REASON_MISSING_KEY


def test_missing_components_list_denied() -> None:
    """GIVEN a manifest with an empty components list, WHEN validated, THEN it
    is denied (a manifest binding zero components authorizes nothing usefully
    and is treated as malformed)."""
    manifest = _base_manifest()
    manifest["components"] = []

    result = ms.validate_manifest(manifest)

    assert result.ok is False
    assert result.reason_code == ms.REASON_EMPTY_COMPONENTS


def test_duplicate_component_denied() -> None:
    """GIVEN a manifest with two components sharing the same path, WHEN
    validated, THEN it is denied (duplicate component entries could be used
    to make the last-wins digest ambiguous)."""
    manifest = _base_manifest()
    manifest["components"].append({"path": "scripts/agent-guards/codex-hook-adapter.mjs", "sha256": VALID_SHA2})

    result = ms.validate_manifest(manifest)

    assert result.ok is False
    assert result.reason_code == ms.REASON_DUPLICATE_COMPONENT


def test_component_missing_field_denied() -> None:
    """GIVEN a component entry missing the sha256 field, WHEN validated, THEN
    it is denied."""
    manifest = _base_manifest()
    manifest["components"] = [{"path": "foo.py"}]

    result = ms.validate_manifest(manifest)

    assert result.ok is False
    assert result.reason_code == ms.REASON_COMPONENT_MISSING_KEY


def test_component_unknown_field_denied() -> None:
    """GIVEN a component entry with an extra unexpected field, WHEN validated,
    THEN it is denied."""
    manifest = _base_manifest()
    manifest["components"] = [{"path": "foo.py", "sha256": VALID_SHA1, "trusted": True}]

    result = ms.validate_manifest(manifest)

    assert result.ok is False
    assert result.reason_code == ms.REASON_COMPONENT_UNKNOWN_KEY


def test_component_path_traversal_denied() -> None:
    """GIVEN a component path containing a '..' traversal segment, WHEN
    validated, THEN it is denied."""
    manifest = _base_manifest()
    manifest["components"] = [{"path": "../../etc/passwd", "sha256": VALID_SHA1}]

    result = ms.validate_manifest(manifest)

    assert result.ok is False
    assert result.reason_code == ms.REASON_COMPONENT_INVALID_PATH


def test_component_absolute_path_denied() -> None:
    """GIVEN a component path that is absolute, WHEN validated, THEN it is
    denied (components must be repository-relative)."""
    manifest = _base_manifest()
    manifest["components"] = [{"path": "/etc/passwd", "sha256": VALID_SHA1}]

    result = ms.validate_manifest(manifest)

    assert result.ok is False
    assert result.reason_code == ms.REASON_COMPONENT_INVALID_PATH


def test_component_invalid_sha256_denied() -> None:
    """GIVEN a component sha256 that is not exactly 64 lowercase hex chars,
    WHEN validated, THEN it is denied (uppercase, too short, non-hex all
    rejected)."""
    for bad_digest in ("B" * 64, "b" * 63, "not-hex-" + "0" * 56):
        manifest = _base_manifest()
        manifest["components"] = [{"path": "foo.py", "sha256": bad_digest}]

        result = ms.validate_manifest(manifest)

        assert result.ok is False, f"expected denial for digest={bad_digest!r}"
        assert result.reason_code == ms.REASON_COMPONENT_INVALID_SHA256


def test_invalid_manifest_version_denied() -> None:
    """GIVEN a manifest_version that does not match the fixed schema version,
    WHEN validated, THEN it is denied."""
    manifest = _base_manifest()
    manifest["manifest_version"] = "AUTHORIZATION_TCB_MANIFEST_V2"

    result = ms.validate_manifest(manifest)

    assert result.ok is False
    assert result.reason_code == ms.REASON_INVALID_MANIFEST_VERSION


def test_invalid_trusted_commit_oid_denied() -> None:
    """GIVEN a trusted_commit_oid that is not a full 40-hex string, WHEN
    validated, THEN it is denied (short OIDs / abbreviated SHAs rejected)."""
    manifest = _base_manifest()
    manifest["trusted_commit_oid"] = "a" * 7  # abbreviated OID

    result = ms.validate_manifest(manifest)

    assert result.ok is False
    assert result.reason_code == ms.REASON_INVALID_TRUSTED_COMMIT_OID


def test_negative_generation_denied() -> None:
    """GIVEN a negative generation value, WHEN validated, THEN it is denied
    (generation must be a monotonic non-negative release epoch)."""
    manifest = _base_manifest()
    manifest["generation"] = -1

    result = ms.validate_manifest(manifest)

    assert result.ok is False
    assert result.reason_code == ms.REASON_INVALID_GENERATION


def test_boolean_generation_denied() -> None:
    """GIVEN a generation value of ``True`` (a bool, which is an ``int``
    subclass in Python), WHEN validated, THEN it is denied — bools must not
    be silently accepted as generation integers."""
    manifest = _base_manifest()
    manifest["generation"] = True

    result = ms.validate_manifest(manifest)

    assert result.ok is False
    assert result.reason_code == ms.REASON_INVALID_GENERATION


def test_non_mapping_manifest_denied() -> None:
    """GIVEN a manifest payload that is not a JSON object (e.g. a list), WHEN
    validated, THEN it is denied."""
    result = ms.validate_manifest(["not", "a", "mapping"])

    assert result.ok is False
    assert result.reason_code == ms.REASON_NOT_A_MAPPING
