"""
Tests for REPO_EVIDENCE_REF_V1 schema validation.

REPO_EVIDENCE_REF_V1 is the output contract for file evidence from gemini-cli-headless-delegation.
It guarantees:
  - type: "REPO_EVIDENCE_REF_V1" constant
  - commit_sha: 40-char or 64-char hex (validated against object_format)
  - object_format: "sha1" | "sha256"
  - excerpt_sha256: 64-char SHA-256 hash of file excerpt (verified via git show)
  - verification_status: "verified" or "inconclusive"
  - mutable URLs (blob/main, tree/develop, etc.) are FORBIDDEN

This test module verifies the validator:
  1. Rejects mutable URLs (blob/main, blob/develop, tree/, etc.)
  2. Validates excerpt_sha256 via git show (with fake blob_bytes_getter for unit tests)
  3. Parses permalink strictly (commit SHA only, no branch refs)
  4. Validates ISO8601 timestamps
  5. Checks required fields presence
  6. Validates object_format + commit_sha length consistency
"""

import pytest
import hashlib
import sys
from pathlib import Path
from datetime import datetime, timezone

# Add scripts directory to path for importing validate_repo_evidence_ref
scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from validate_repo_evidence_ref import validate_repo_evidence_ref


def compute_excerpt_hash(content: bytes) -> str:
    """Compute SHA-256 hash of excerpt bytes."""
    return hashlib.sha256(content).hexdigest()


class TestRepoEvidenceRefValidator:
    """Test suite for REPO_EVIDENCE_REF_V1 validator."""

    # ===== 1. Basic Valid Cases (schema + type check)

    def test_valid_evidence_verified_sha1(self):
        """Test a valid verified evidence with SHA-1 commit_sha."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "anchor_text": "## Architecture Overview",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert result["ok"], f"Expected valid evidence, got errors: {result['errors']}"
        assert result["status"] == "verified"

    def test_valid_evidence_inconclusive_sha1(self):
        """Test a valid inconclusive evidence with SHA-1."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "def456abc123def456abc123def456abc123def4",
            "object_format": "sha1",
            "path": "src/systems/combat.ts",
            "start_line": 100,
            "end_line": 120,
            "permalink": "https://github.com/squne121/loop-protocol/blob/def456abc123def456abc123def456abc123def4/src/systems/combat.ts#L100-L120",
            "excerpt_sha256": "f0e1d2c3b4a5968778695a4b3c2d1e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5cab",
            "anchor_text": None,
            "verification_status": "inconclusive",
            "verification_method": "sha256_hash_mismatch",
            "verified_at": "2026-05-23T15:31:10Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert result["ok"], f"Expected valid evidence, got errors: {result['errors']}"

    # ===== 2. object_format sha256 cases

    def test_object_format_sha256_valid(self):
        """Test that 64-char commit_sha with sha256 object_format is accepted."""
        sha256_commit = "a" * 64  # Valid 64-char hex
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": sha256_commit,
            "object_format": "sha256",
            "path": "docs/adr/0001.md",
            "start_line": 1,
            "end_line": 10,
            "permalink": f"https://github.com/squne121/loop-protocol/blob/{sha256_commit}/docs/adr/0001.md#L1-L10",
            "excerpt_sha256": "f0e1d2c3b4a5968778695a4b3c2d1e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5cab",
            "verification_status": "inconclusive",
            "verification_method": "line_range_unverified",
            "verified_at": "2026-05-23T15:31:10Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert result["ok"], f"Expected valid sha256 commit_sha, got errors: {result['errors']}"

    def test_object_format_sha256_40char_rejected(self):
        """Test that 40-char commit_sha with sha256 object_format is rejected."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",  # 40 chars
            "object_format": "sha256",  # Expects 64
            "path": "docs/adr/0001.md",
            "start_line": 1,
            "end_line": 10,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L1-L10",
            "excerpt_sha256": "f0e1d2c3b4a5968778695a4b3c2d1e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5cab",
            "verification_status": "inconclusive",
            "verification_method": "line_range_unverified",
            "verified_at": "2026-05-23T15:31:10Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert not result["ok"], "Expected rejection for mismatched commit_sha length"
        assert any("64 characters" in e or "object_format" in e for e in result["errors"])

    # ===== 3. Type field validation

    def test_type_field_missing(self):
        """Test that missing type field is rejected."""
        evidence = {
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert not result["ok"]
        assert any("type" in e.lower() for e in result["errors"])

    def test_type_field_wrong_value(self):
        """Test that wrong type value is rejected."""
        evidence = {
            "type": "WRONG_TYPE",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert not result["ok"]
        assert any("type" in e.lower() for e in result["errors"])

    # ===== 4. Mutable URL rejection (CRITICAL)

    def test_mutable_url_blob_main_rejected(self):
        """Test that blob/main mutable URL is rejected (CRITICAL)."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/main/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert not result["ok"], "Expected mutable URL blob/main to be rejected"
        assert result["status"] == "rejected"

    def test_mutable_url_tree_prefix_rejected(self):
        """Test that tree/ prefix URLs are rejected (directory references forbidden)."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "src/",
            "start_line": 1,
            "end_line": 1,
            "permalink": "https://github.com/squne121/loop-protocol/tree/abc123def456abc123def456abc123def456abc1/src/",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert not result["ok"], "Expected tree/ URL to be rejected"

    # ===== 5. Permalink path/line range mismatch

    def test_permalink_path_mismatch_rejected(self):
        """Test that permalink path differs from evidence path, validation fails."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/DIFFERENT.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert not result["ok"]
        assert any("path" in e.lower() for e in result["errors"])

    def test_permalink_start_line_mismatch_rejected(self):
        """Test that permalink start line differs from evidence start_line."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L50-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert not result["ok"]
        assert any("start" in e.lower() for e in result["errors"])

    def test_permalink_end_line_mismatch_rejected(self):
        """Test that permalink end line differs from evidence end_line."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L100",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert not result["ok"]
        assert any("end" in e.lower() for e in result["errors"])

    def test_permalink_commit_sha_mismatch_rejected(self):
        """Test that permalink SHA differs from evidence commit_sha."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/ffffffffffffffffffffffffffffffffffffffff/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert not result["ok"]
        assert any("sha" in e.lower() for e in result["errors"])

    # ===== 6. Excerpt hash mismatch validation (with fake blob_bytes_getter)

    def test_excerpt_hash_mismatch_via_getter(self):
        """Test that excerpt_sha256 mismatch is detected (using fake getter)."""
        actual_content = b"line 1\nline 2\nline 3\n"
        actual_hash = hashlib.sha256(actual_content).hexdigest()
        claimed_hash = "f" * 64  # Different hash

        def fake_getter(commit_sha, path):
            return actual_content

        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 1,
            "end_line": 3,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L1-L3",
            "excerpt_sha256": claimed_hash,
            "verification_status": "inconclusive",
            "verification_method": "sha256_hash_mismatch",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        result = validate_repo_evidence_ref(evidence, blob_bytes_getter=fake_getter)
        assert not result["ok"]
        assert result["status"] == "inconclusive"
        assert any("mismatch" in r.lower() for r in result["reasons"])

    def test_excerpt_hash_match_via_getter(self):
        """Test that excerpt_sha256 match is verified (using fake getter)."""
        actual_content = b"line 1\nline 2\nline 3\n"
        actual_hash = hashlib.sha256(actual_content).hexdigest()

        def fake_getter(commit_sha, path):
            return actual_content

        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 1,
            "end_line": 3,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L1-L3",
            "excerpt_sha256": actual_hash,
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        result = validate_repo_evidence_ref(evidence, blob_bytes_getter=fake_getter)
        assert result["ok"]
        assert result["status"] == "verified"

    # ===== 7. Verification status + method consistency

    def test_verified_status_requires_hash_match_method(self):
        """Test that verified status REQUIRES sha256_hash_match method."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "verification_status": "verified",
            "verification_method": "sha256_hash_mismatch",  # INCONSISTENT
            "verified_at": "2026-05-23T15:30:45Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert not result["ok"]
        assert any("verified" in e.lower() and "requires" in e.lower() for e in result["errors"])

    # ===== 8. ISO8601 timestamp validation

    def test_verified_at_invalid_iso8601(self):
        """Test that invalid ISO8601 timestamp is rejected."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "not-a-timestamp",
        }
        result = validate_repo_evidence_ref(evidence)
        assert not result["ok"]
        assert any("iso8601" in e.lower() or "datetime" in e.lower() for e in result["errors"])

    # ===== 9. anchor_text null tolerance

    def test_anchor_text_null_allowed(self):
        """Test that anchor_text: null is accepted."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "src/systems/combat.ts",
            "start_line": 100,
            "end_line": 120,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/src/systems/combat.ts#L100-L120",
            "excerpt_sha256": "f0e1d2c3b4a5968778695a4b3c2d1e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5cab",
            "anchor_text": None,
            "verification_status": "inconclusive",
            "verification_method": "sha256_hash_mismatch",
            "verified_at": "2026-05-23T15:31:10Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert result["ok"]

    def test_anchor_text_non_string_rejected(self):
        """Test that anchor_text must be null or string."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "anchor_text": 123,  # Invalid: must be string or null
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert not result["ok"]

    # ===== 10. Hash computation helpers

    def test_compute_excerpt_hash_simple(self):
        """Test SHA-256 computation for simple bytes."""
        content = b"Hello, World!"
        expected = hashlib.sha256(content).hexdigest()
        computed = compute_excerpt_hash(content)
        assert computed == expected
        assert len(computed) == 64, "SHA-256 hash must be 64 characters"

    def test_compute_excerpt_hash_multiline(self):
        """Test SHA-256 computation for multiline bytes with LF."""
        content = b"Line 1\nLine 2\nLine 3\n"
        expected = hashlib.sha256(content).hexdigest()
        computed = compute_excerpt_hash(content)
        assert computed == expected

    # ===== 11. inconclusive with different methods

    def test_inconclusive_with_fetch_error_allowed(self):
        """Test that inconclusive with fetch_error method is accepted."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "verification_status": "inconclusive",
            "verification_method": "fetch_error",
            "verified_at": "2026-05-23T15:31:10Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert result["ok"]

    def test_inconclusive_with_line_range_unverified_allowed(self):
        """Test that inconclusive with line_range_unverified method is accepted."""
        evidence = {
            "type": "REPO_EVIDENCE_REF_V1",
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "object_format": "sha1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "verification_status": "inconclusive",
            "verification_method": "line_range_unverified",
            "verified_at": "2026-05-23T15:31:10Z",
        }
        result = validate_repo_evidence_ref(evidence)
        assert result["ok"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
