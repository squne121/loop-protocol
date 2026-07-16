"""
test_parent_replay_binding.py

Pytest coverage for `parent_replay_binding.py` (Issue #1532 AC2/AC3).

GIVEN/WHEN/THEN:
  - AC2: the parent replay binding is built ONLY from caller-supplied
    (parent-owned) in-memory inputs -- it never touches the filesystem for
    review/readiness/checker artifacts, never mutates the caller's dicts,
    and its `input_digests` are exact sha256 digests of the SAME in-memory
    dicts the caller passed in (not some other/child-owned copy).
  - AC3: the canonical binding artifact is byte-for-byte repeatable for the
    same parent-owned inputs (same `binding_digest`), embeds
    `replay_next_state` (the `REPLAY_NEXT_STATE` state-store persistence
    target), and is self-consistent (`recompute_binding_digest` reproduces
    `binding_digest`); any tamper of the artifact content changes the
    digest.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from parent_replay_binding import (  # noqa: E402
    SCHEMA,
    SCHEMA_VERSION,
    build_parent_replay_binding,
    canonical_json_bytes,
    canonical_replay_next_state_line,
    recompute_binding_digest,
)

READINESS_LP001 = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [
        {
            "rule_id": "LP001",
            "source_check": "validate_issue_body",
            "category": "body_lint",
            "line_start": 1,
            "line_end": 1,
        }
    ],
}

COMPACT_MISSING_SECTION = {
    "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
    "issue_url": "https://github.com/squne121/loop-protocol/issues/1532",
    "body_sha256": "sha256:body-a",
    "blocking_issues": [{"code": "missing_section", "message": "missing section"}],
    "structured_blockers": [],
    "findings": [],
}


def _identity() -> dict:
    return {
        "repository_full_name": "squne121/loop-protocol",
        "issue_number": 1532,
        "refinement_session_id": "session-abc",
        "iteration_id": "iteration-1",
    }


class TestParentOwnedInventoryOnly:
    """Issue #1532 AC2: parent replay uses ONLY parent-owned inventory --
    no raw child artifact / no raw REPLAY_* value is consumed as an input."""

    def test_parent_replay_uses_only_parent_owned_inventory(self, tmp_path):
        review_result = copy.deepcopy(COMPACT_MISSING_SECTION)
        readiness_result = copy.deepcopy(READINESS_LP001)
        identity = _identity()

        # Deliberately do NOT create any file on disk for review/readiness --
        # if build_parent_replay_binding() ever needed filesystem access to
        # a child worktree artifact, this test would fail with a file-not-
        # found style error. It doesn't: everything is in-memory.
        artifact = build_parent_replay_binding(
            review_result=review_result,
            readiness_result=readiness_result,
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state=None,
            **identity,
        )

        assert artifact["schema"] == SCHEMA
        assert artifact["schema_version"] == SCHEMA_VERSION
        assert artifact["repository_full_name"] == identity["repository_full_name"]
        assert artifact["issue_number"] == identity["issue_number"]
        assert artifact["refinement_session_id"] == identity["refinement_session_id"]
        assert artifact["iteration_id"] == identity["iteration_id"]

        # The input digests must be sha256 over the EXACT in-memory dicts
        # the caller passed -- proving the parent is hashing its own
        # inventory, not re-deriving digests from some external artifact.
        expected_review_digest = __import__("hashlib").sha256(
            canonical_json_bytes(COMPACT_MISSING_SECTION)
        ).hexdigest()
        assert artifact["input_digests"]["review_result_sha256"] == expected_review_digest

        # Caller's dicts must never be mutated by the parent replay.
        assert review_result == COMPACT_MISSING_SECTION
        assert readiness_result == READINESS_LP001

    def test_parent_replay_result_reflects_deterministic_checker_backing(self):
        """GIVEN a review claim backed by a matching readiness rule id THEN
        the parent's own replay of analyze() (not a child self-report)
        confirms deterministic_fail_confirmed."""
        artifact = build_parent_replay_binding(
            review_result=copy.deepcopy(COMPACT_MISSING_SECTION),
            readiness_result=copy.deepcopy(READINESS_LP001),
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state=None,
            **_identity(),
        )
        assert artifact["replay_result"]["verdict"] == "deterministic_fail_confirmed"
        assert artifact["replay_next_state"] is not None


class TestCanonicalBindingRepeatability:
    """Issue #1532 AC3: canonical binding artifact is byte-for-byte
    repeatable and binds `replay_next_state` (the REPLAY_NEXT_STATE state
    store persistence target)."""

    def test_canonical_binding_is_repeatable_and_binds_next_state(self):
        kwargs = dict(
            review_result=copy.deepcopy(COMPACT_MISSING_SECTION),
            readiness_result=copy.deepcopy(READINESS_LP001),
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state=None,
            **_identity(),
        )
        artifact_1 = build_parent_replay_binding(**kwargs)
        artifact_2 = build_parent_replay_binding(**kwargs)

        # Same parent-owned inputs -> same digest, regardless of when it
        # runs (no wall-clock value enters the canonical payload).
        assert artifact_1["binding_digest"] == artifact_2["binding_digest"]
        assert artifact_1["binding_digest"].startswith("sha256:")

        # replay_next_state is embedded -- this is the exact value the
        # orchestrator persists as REPLAY_NEXT_STATE via
        # reviewer_claim_replay_state_store.py --write-v2.
        assert "replay_next_state" in artifact_1
        assert artifact_1["replay_next_state"] == artifact_2["replay_next_state"]

        # Self-consistency: recomputing over the artifact (minus its own
        # digest field) reproduces the stored digest.
        assert recompute_binding_digest(artifact_1) == artifact_1["binding_digest"]

        # The canonical REPLAY_NEXT_STATE envelope line is deterministic too.
        line_1 = canonical_replay_next_state_line(artifact_1)
        line_2 = canonical_replay_next_state_line(artifact_2)
        assert line_1 == line_2
        json.loads(line_1)  # must be valid JSON

    def test_tampering_the_artifact_changes_the_recomputed_digest(self):
        artifact = build_parent_replay_binding(
            review_result=copy.deepcopy(COMPACT_MISSING_SECTION),
            readiness_result=copy.deepcopy(READINESS_LP001),
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state=None,
            **_identity(),
        )
        tampered = copy.deepcopy(artifact)
        tampered["replay_next_state"] = {"tampered": True}
        assert recompute_binding_digest(tampered) != artifact["binding_digest"]

    def test_different_iteration_id_still_reproducible_but_distinct_from_identity_change(self):
        """iteration_id participates in the canonical payload (so a stale
        cross-iteration binding never silently matches), but two calls with
        the SAME iteration_id and inputs are still identical."""
        base_kwargs = dict(
            review_result=copy.deepcopy(COMPACT_MISSING_SECTION),
            readiness_result=copy.deepcopy(READINESS_LP001),
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state=None,
        )
        identity_a = _identity()
        identity_b = dict(identity_a)
        identity_b["iteration_id"] = "iteration-2"

        artifact_a1 = build_parent_replay_binding(**base_kwargs, **identity_a)
        artifact_a2 = build_parent_replay_binding(**base_kwargs, **identity_a)
        artifact_b = build_parent_replay_binding(**base_kwargs, **identity_b)

        assert artifact_a1["binding_digest"] == artifact_a2["binding_digest"]
        assert artifact_a1["binding_digest"] != artifact_b["binding_digest"]
