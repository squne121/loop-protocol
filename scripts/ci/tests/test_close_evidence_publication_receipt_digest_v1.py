"""Issue #2555 AC10 focused regression test for
`scripts/ci/build_close_evidence_bundle_v1.py`'s `build_publication_receipt()`.

Context: `actions/upload-artifact@v7`'s real `artifact-digest` action output is
a BARE 64-hex-character SHA-256 string with NO `sha256:` prefix (confirmed by
the OWNER anchor comment fact-check in #2555's "## Runtime Verification
Ownership Disposition" table, finding #3, web-researcher SubAgent primary
source check against the action's own README/outputs contract). This is a
DIFFERENT wire representation than the GitHub Artifacts REST API's `digest`
field, which IS `sha256:`-prefixed.

The existing regression coverage in
`scripts/ci/tests/test_build_close_evidence_bundle_v1.py::
test_close_evidence_json_excludes_github_upload_only_keys` (line ~197-212)
only exercises the `sha256:`-prefixed shape. This file adds the missing
bare-hex (no prefix) counterpart so both real wire shapes that
`build_publication_receipt()` may actually receive from
`.github/workflows/ci.yml` are covered by deterministic regression.

Scope note (#2555 Out of Scope): this test asserts CURRENT verbatim-passthrough
behavior of `build_publication_receipt()`. It does not add, and must not be
read as requiring, any `sha256:` prefix normalization logic -- that
readback-time normalization (if ever needed) belongs to #2155's production
verification, per #2555's Out of Scope / Runtime Verification Ownership
Disposition. `build_publication_receipt()` itself is NOT modified by this
Issue; only this new deterministic test file is added.
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


def test_publication_receipt_bare_hex_and_prefixed_digest_both_verbatim_distinct(builder):
    """GIVEN two publication receipts built with the two real digest wire
    shapes (bare-hex from `actions/upload-artifact@v7` vs.
    `sha256:`-prefixed from the GitHub Artifacts REST API `digest` field)
    THEN each stores its own input verbatim and the two are NOT
    conflated or cross-normalized into a shared representation --
    `build_publication_receipt()` performs no shape detection or
    normalization of any kind."""
    close_evidence = {
        "experiment_identity": "issue-2555-digest-shape-distinction",
        "bundle_payload_digest": "sha256:" + "d" * 64,
    }
    prefixed_digest = "sha256:" + "e" * 64

    bare_receipt = builder.build_publication_receipt(
        close_evidence,
        github_artifact_id="7",
        github_artifact_digest=_BARE_HEX_DIGEST,
        artifact_url="https://github.com/squne121/loop-protocol/actions/runs/2/artifacts/7",
    )
    prefixed_receipt = builder.build_publication_receipt(
        close_evidence,
        github_artifact_id="7",
        github_artifact_digest=prefixed_digest,
        artifact_url="https://github.com/squne121/loop-protocol/actions/runs/2/artifacts/7",
    )

    assert bare_receipt["github_artifact_digest"] == _BARE_HEX_DIGEST
    assert prefixed_receipt["github_artifact_digest"] == prefixed_digest
    assert bare_receipt["github_artifact_digest"] != prefixed_receipt["github_artifact_digest"]
