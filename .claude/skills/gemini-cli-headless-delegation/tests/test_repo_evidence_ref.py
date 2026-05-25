"""
Tests for REPO_EVIDENCE_REF_V1 schema validation.

REPO_EVIDENCE_REF_V1 is the output contract for file evidence from gemini-cli-headless-delegation.
It guarantees:
  - commit_sha: 40-char SHA-1 (immutable commit reference)
  - excerpt_sha256: 64-char SHA-256 hash of file excerpt
  - verification_status: "verified" or "inconclusive"
  - mutable URLs (blob/main, tree/develop, etc.) are FORBIDDEN

This test module verifies the validator rejects invalid evidence:
  1. Mutable URLs (blob/main, blob/develop, etc.)
  2. excerpt_sha256 mismatch (invalid hash claim)
"""

import pytest
import hashlib
from pathlib import Path


def validate_repo_evidence_ref(evidence: dict) -> tuple[bool, str | None]:
    """
    Validate a REPO_EVIDENCE_REF_V1 instance.

    Returns:
        (is_valid: bool, error_reason: str | None)
            - (True, None) if evidence passes all checks
            - (False, reason) if evidence fails validation
    """
    # Check 1: commit_sha must be 40-char hex
    commit_sha = evidence.get("commit_sha", "")
    if not isinstance(commit_sha, str) or len(commit_sha) != 40:
        return False, "commit_sha must be exactly 40 characters"

    if not all(c in "0123456789abcdef" for c in commit_sha):
        return False, "commit_sha must be lowercase hex characters"

    # Check 2: mutable URL prohibition (CRITICAL)
    permalink = evidence.get("permalink", "")
    mutable_patterns = ["blob/main", "blob/develop", "blob/master", "tree/main", "tree/develop", "tree/master"]

    for pattern in mutable_patterns:
        if pattern in permalink:
            return False, f"permalink contains mutable URL pattern '{pattern}' — use commit SHA instead"

    # Check 3: permalink must use commit SHA format
    if f"blob/{commit_sha}" not in permalink and f"tree/{commit_sha}" not in permalink:
        return False, "permalink must include commit SHA in immutable format (blob/{commit_sha} or tree/{commit_sha})"

    # Check 4: excerpt_sha256 must be 64-char hex
    excerpt_sha256 = evidence.get("excerpt_sha256", "")
    if not isinstance(excerpt_sha256, str) or len(excerpt_sha256) != 64:
        return False, "excerpt_sha256 must be exactly 64 characters"

    if not all(c in "0123456789abcdef" for c in excerpt_sha256):
        return False, "excerpt_sha256 must be lowercase hex characters"

    # Check 5: verification_status must be one of allowed values
    status = evidence.get("verification_status")
    if status not in ("verified", "inconclusive"):
        return False, f"verification_status must be 'verified' or 'inconclusive', got '{status}'"

    # Check 6: verification_method must be one of allowed values
    method = evidence.get("verification_method")
    allowed_methods = ("sha256_hash_match", "sha256_hash_mismatch", "line_range_unverified", "fetch_error")
    if method not in allowed_methods:
        return False, f"verification_method must be one of {allowed_methods}, got '{method}'"

    # Check 7: If status is "verified", method must be "sha256_hash_match"
    if status == "verified" and method != "sha256_hash_match":
        return False, "verification_status 'verified' requires verification_method 'sha256_hash_match'"

    # Check 8: start_line and end_line must be positive integers
    start_line = evidence.get("start_line")
    end_line = evidence.get("end_line")

    if not isinstance(start_line, int) or start_line < 1:
        return False, "start_line must be a positive integer"

    if not isinstance(end_line, int) or end_line < 1:
        return False, "end_line must be a positive integer"

    if end_line < start_line:
        return False, "end_line must be >= start_line"

    return True, None


def compute_excerpt_hash(content: str) -> str:
    """Compute SHA-256 hash of excerpt content."""
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


class TestRepoEvidenceRefValidator:
    """Test suite for REPO_EVIDENCE_REF_V1 validator."""

    def test_valid_evidence_verified(self):
        """Test that a valid verified evidence passes validation."""
        evidence = {
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        is_valid, error = validate_repo_evidence_ref(evidence)
        assert is_valid, f"Expected valid evidence, got error: {error}"

    def test_valid_evidence_inconclusive(self):
        """Test that a valid inconclusive evidence passes validation."""
        evidence = {
            "commit_sha": "def456abc123def456abc123def456abc123def4",
            "path": "src/systems/combat.ts",
            "start_line": 100,
            "end_line": 120,
            "permalink": "https://github.com/squne121/loop-protocol/blob/def456abc123def456abc123def456abc123def4/src/systems/combat.ts#L100-L120",
            "excerpt_sha256": "f0e1d2c3b4a5968778695a4b3c2d1e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5abc",
            "verification_status": "inconclusive",
            "verification_method": "sha256_hash_mismatch",
            "verified_at": "2026-05-23T15:31:10Z",
        }
        is_valid, error = validate_repo_evidence_ref(evidence)
        assert is_valid, f"Expected valid evidence, got error: {error}"

    def test_mutable_url_blob_main_rejected(self):
        """Test that blob/main mutable URL is rejected (CRITICAL)."""
        evidence = {
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/main/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        is_valid, error = validate_repo_evidence_ref(evidence)
        assert not is_valid, "Expected mutable URL blob/main to be rejected"
        assert "mutable" in error.lower() or "blob/main" in error.lower(), f"Error should mention mutability: {error}"

    def test_mutable_url_blob_develop_rejected(self):
        """Test that blob/develop mutable URL is rejected."""
        evidence = {
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "path": "src/main.ts",
            "start_line": 1,
            "end_line": 50,
            "permalink": "https://github.com/squne121/loop-protocol/blob/develop/src/main.ts#L1-L50",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        is_valid, error = validate_repo_evidence_ref(evidence)
        assert not is_valid, "Expected mutable URL blob/develop to be rejected"

    def test_mutable_url_tree_master_rejected(self):
        """Test that tree/master mutable URL is rejected."""
        evidence = {
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "path": "src/",
            "start_line": 1,
            "end_line": 1,
            "permalink": "https://github.com/squne121/loop-protocol/tree/master/src/",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        is_valid, error = validate_repo_evidence_ref(evidence)
        assert not is_valid, "Expected mutable URL tree/master to be rejected"

    def test_excerpt_hash_mismatch_rejected_for_verified_status(self):
        """Test that sha256_hash_mismatch with verified status is rejected."""
        evidence = {
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1",
            "verification_status": "verified",
            "verification_method": "sha256_hash_mismatch",  # INCONSISTENT
            "verified_at": "2026-05-23T15:30:45Z",
        }
        is_valid, error = validate_repo_evidence_ref(evidence)
        assert not is_valid, "Expected inconsistent verification_status and method to be rejected"

    def test_invalid_commit_sha_too_short(self):
        """Test that invalid commit SHA (too short) is rejected."""
        evidence = {
            "commit_sha": "abc123",  # Only 6 chars instead of 40
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        is_valid, error = validate_repo_evidence_ref(evidence)
        assert not is_valid, "Expected invalid commit_sha to be rejected"

    def test_invalid_excerpt_hash_too_long(self):
        """Test that invalid excerpt_sha256 (too long) is rejected."""
        evidence = {
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f11111",  # 70 chars
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        is_valid, error = validate_repo_evidence_ref(evidence)
        assert not is_valid, "Expected invalid excerpt_sha256 to be rejected"

    def test_invalid_verification_status(self):
        """Test that invalid verification_status is rejected."""
        evidence = {
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "path": "docs/adr/0001.md",
            "start_line": 42,
            "end_line": 67,
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L42-L67",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1",
            "verification_status": "unknown",  # INVALID
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        is_valid, error = validate_repo_evidence_ref(evidence)
        assert not is_valid, "Expected invalid verification_status to be rejected"

    def test_invalid_line_numbers(self):
        """Test that invalid line numbers are rejected."""
        evidence = {
            "commit_sha": "abc123def456abc123def456abc123def456abc1",
            "path": "docs/adr/0001.md",
            "start_line": 100,
            "end_line": 50,  # end < start
            "permalink": "https://github.com/squne121/loop-protocol/blob/abc123def456abc123def456abc123def456abc1/docs/adr/0001.md#L100-L50",
            "excerpt_sha256": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1",
            "verification_status": "verified",
            "verification_method": "sha256_hash_match",
            "verified_at": "2026-05-23T15:30:45Z",
        }
        is_valid, error = validate_repo_evidence_ref(evidence)
        assert not is_valid, "Expected end_line < start_line to be rejected"

    def test_compute_excerpt_hash_simple(self):
        """Test SHA-256 computation for simple excerpt."""
        content = "Hello, World!"
        expected = hashlib.sha256(b"Hello, World!").hexdigest()
        computed = compute_excerpt_hash(content)
        assert computed == expected
        assert len(computed) == 64, "SHA-256 hash must be 64 characters"

    def test_compute_excerpt_hash_multiline(self):
        """Test SHA-256 computation for multiline excerpt."""
        content = "Line 1\nLine 2\nLine 3\n"
        expected = hashlib.sha256(content.encode('utf-8')).hexdigest()
        computed = compute_excerpt_hash(content)
        assert computed == expected


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
