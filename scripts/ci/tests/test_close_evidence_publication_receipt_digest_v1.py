"""Issue #2555 AC10 focused regression test for
`scripts/ci/build_close_evidence_bundle_v1.py`'s `build_publication_receipt()`.

This file guards AC10's bare-hex verbatim regression: `actions/upload-artifact@v7`'s
`artifact-digest` action output and the GitHub Artifacts REST API's readback
`digest` field are different wire representations (bare hex vs `sha256:`-prefixed).
REST-side prefix comparison/normalization at readback time is Issue #2155's
ownership, not this file's.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE_PATH = _REPO_ROOT / "scripts" / "ci" / "build_close_evidence_bundle_v1.py"

# Real `actions/upload-artifact@v7` `artifact-digest` output shape: bare
# 64-hex-character SHA-256, no `sha256:` prefix.
_BARE_HEX_DIGEST = "b" * 64


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "build_close_evidence_bundle_v1_digest_regression_under_test", _MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def builder():
    return _load_module()


def test_publication_receipt_accepts_bare_hex_digest_verbatim(builder):
    """GIVEN a `github_artifact_digest` shaped like the real
    `actions/upload-artifact@v7` `artifact-digest` output (bare 64-hex,
    no `sha256:` prefix)
    WHEN `build_publication_receipt()` is called with that value
    THEN the returned publication receipt stores the digest UNCHANGED
    (no prefix added, no re-hashing, no truncation) -- verbatim
    passthrough, exactly as #2555 AC6/AC10 require."""
    close_evidence = {
        "experiment_identity": "issue-2555-bare-hex-digest-regression",
        "bundle_payload_digest": "sha256:" + "c" * 64,
    }

    publication_receipt = builder.build_publication_receipt(
        close_evidence,
        github_artifact_id="42",
        github_artifact_digest=_BARE_HEX_DIGEST,
        artifact_url="https://github.com/squne121/loop-protocol/actions/runs/1/artifacts/42",
    )

    # Verbatim: byte-for-byte identical to the input, no `sha256:` prefix
    # added and no other mutation applied.
    assert publication_receipt["github_artifact_digest"] == _BARE_HEX_DIGEST
    assert not publication_receipt["github_artifact_digest"].startswith("sha256:")
    assert len(publication_receipt["github_artifact_digest"]) == 64

    # Schema sanity: this must still be a well-formed
    # CI_CLOSE_EVIDENCE_PUBLICATION_RECEIPT_V1, unaffected by the digest
    # shape used.
    assert publication_receipt["schema"] == "CI_CLOSE_EVIDENCE_PUBLICATION_RECEIPT_V1"
    for key in builder.GITHUB_UPLOAD_ONLY_KEYS:
        assert key in publication_receipt
    assert publication_receipt["bundle_payload_digest"] == close_evidence["bundle_payload_digest"]
