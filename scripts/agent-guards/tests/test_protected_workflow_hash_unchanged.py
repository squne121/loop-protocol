"""
test_protected_workflow_hash_unchanged.py

Issue #1856 (AC10): .github/workflows/test-verdict-execution-record.yml must
not be changed by this Issue's PR (trigger / permissions / execution target
included). This is verified with a fixed literal SHA256 string comparison
against the byte content of the file as of 2026-07-30 (the value recorded in
Issue #1856's contract).

Phase 3 (a separate, not-yet-filed Issue) is where this protected workflow's
producer/publisher/materializer/schema may eventually be physically changed
or removed; this test is scoped to Phase 1 only and must be revisited (or
removed) alongside that future work — not silently superseded here.
"""

from __future__ import annotations

import hashlib
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test-verdict-execution-record.yml"

# Recorded in Issue #1856 contract (2026-07-30):
EXPECTED_SHA256 = (
    "943aac8657ef814864888787d7e10d66ff398f2c872a7da537768397faf2796d"
)


def test_protected_workflow_file_exists():
    assert WORKFLOW_PATH.is_file(), (
        f"Protected workflow not found at {WORKFLOW_PATH}"
    )


def test_protected_workflow_hash_matches_recorded_sha256():
    content = WORKFLOW_PATH.read_bytes()
    actual_sha256 = hashlib.sha256(content).hexdigest()
    assert actual_sha256 == EXPECTED_SHA256, (
        f"protected workflow test-verdict-execution-record.yml has changed "
        f"(sha256 mismatch). expected={EXPECTED_SHA256!r} actual={actual_sha256!r}. "
        f"Issue #1856 Out of Scope / Stop Conditions forbid physically changing "
        f"this workflow (trigger/permissions/execution target included). "
        f"Physical changes belong to Phase 3 (separate, not-yet-filed Issue)."
    )
