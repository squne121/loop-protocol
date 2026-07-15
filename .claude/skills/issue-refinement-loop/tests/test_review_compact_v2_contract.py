"""
test_review_compact_v2_contract.py

Pytest coverage for the ISSUE_REVIEW_RESULT_COMPACT_V2 /
REVIEW_COMPACT_VALIDATION_RESULT_V2 producer/validator/consumer contract
and docs registration (Issue #1532 AC1/AC7).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
REPO_ROOT = SKILL_ROOT.parent.parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from parent_replay_binding import (  # noqa: E402
    build_parent_replay_binding,
    canonical_replay_next_state_line,
)
from validate_review_compact_output import (  # noqa: E402
    validate_review_compact_output_v2,
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


def _fake_sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _v1_needs_fix_envelope_text(
    *, artifact_path: str, replay_artifact_digest: str
) -> str:
    lines = [
        "STATUS: ok",
        "VERDICT: needs-fix",
        "SUMMARY: 1 blocker(s)",
        "BLOCKERS: 1",
        "NEXT_ACTION: request_changes",
        "MUST_READ: ",
        f"EVIDENCE: {artifact_path}",
        f"ARTIFACT: compact_review_result_v1={artifact_path}",
        "REPLAY_VERDICT: deterministic_fail_confirmed",
        "REPLAY_ROUTING: proceed_to_rewrite",
        "REPLAY_SHOULD_CONSUME: true",
        f"REPLAY_BODY_SHA256: sha256:{_fake_sha256('body')}",
        f"REPLAY_ARTIFACT_DIGEST: {replay_artifact_digest}",
    ]
    return "\n".join(lines)


class TestProducerValidatorConsumerDigestSemantics:
    """Issue #1532 AC1: compact V2 / validator result V2 define
    REPLAY_ARTIFACT_DIGEST (child stdout digest, meaning UNCHANGED from V1)
    and REPLAY_PARENT_BINDING_DIGEST (parent-side value) as SEPARATE
    fields. Producer (this test simulates the orchestrator's assembly
    step) / validator (`validate_review_compact_output_v2`) / consumer
    (`normalized_payload`) all preserve both meanings without conflating
    them."""

    def test_v2_producer_validator_consumer_preserve_distinct_digest_semantics(self):
        artifact_path = ".claude/artifacts/issue-refinement-loop/1532/compact_review_result_20260716T000000Z.json"

        # 1) CHILD stdout digest: sha256 over the (simulated) exact stdout
        #    bytes the isolation-worktree reviewer_claim_replay.py call
        #    produced. This is REPLAY_ARTIFACT_DIGEST -- unchanged meaning
        #    from V1 (Issue #1507/#1519).
        child_stdout_bytes = b'{"schema":"REVIEWER_CLAIM_REPLAY_V1","verdict":"deterministic_fail_confirmed"}'
        replay_artifact_digest = f"sha256:{hashlib.sha256(child_stdout_bytes).hexdigest()}"

        # 2) PARENT-side value: the orchestrator independently replays
        #    analyze() over its OWN parent-owned inputs (never the child's
        #    raw artifact) via parent_replay_binding.py.
        binding_artifact = build_parent_replay_binding(
            review_result=COMPACT_MISSING_SECTION,
            readiness_result=READINESS_LP001,
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state=None,
            repository_full_name="squne121/loop-protocol",
            issue_number=1532,
            refinement_session_id="session-abc",
            iteration_id="iteration-1",
        )
        replay_parent_binding_digest = binding_artifact["binding_digest"]
        replay_next_state_line = canonical_replay_next_state_line(binding_artifact)

        # PRODUCER step: the orchestrator assembles the V2 envelope by
        # appending its OWN two lines to the child's already-validated V1
        # needs-fix envelope text.
        v1_text = _v1_needs_fix_envelope_text(
            artifact_path=artifact_path,
            replay_artifact_digest=replay_artifact_digest,
        )
        v2_text = "\n".join(
            [
                v1_text,
                f"REPLAY_NEXT_STATE: {replay_next_state_line}",
                f"REPLAY_PARENT_BINDING_DIGEST: {replay_parent_binding_digest}",
            ]
        )

        # The two digests must never collide by construction (distinct
        # inputs / distinct meaning).
        assert replay_artifact_digest != replay_parent_binding_digest

        # VALIDATOR step.
        result = validate_review_compact_output_v2(
            v2_text,
            issue_number=1532,
            expected_replay_next_state=replay_next_state_line,
            expected_parent_binding_digest=replay_parent_binding_digest,
        )
        assert result["validation_status"] == "valid"

        # CONSUMER step: both fields survive independently in the
        # normalized payload -- neither overwrites nor aliases the other.
        payload = result["normalized_payload"]
        assert payload["REPLAY_ARTIFACT_DIGEST"] == replay_artifact_digest
        assert payload["REPLAY_PARENT_BINDING_DIGEST"] == replay_parent_binding_digest
        assert payload["REPLAY_ARTIFACT_DIGEST"] != payload["REPLAY_PARENT_BINDING_DIGEST"]
        assert payload["REPLAY_NEXT_STATE"] == replay_next_state_line

    def test_a_tampered_child_digest_is_independent_of_parent_digest_validity(self):
        """A corrupted REPLAY_ARTIFACT_DIGEST (child claim) does not
        magically also corrupt REPLAY_PARENT_BINDING_DIGEST validation --
        proving the two fields are validated independently, not derived
        from one another."""
        artifact_path = ".claude/artifacts/issue-refinement-loop/1532/compact_review_result_20260716T000000Z.json"
        binding_artifact = build_parent_replay_binding(
            review_result=COMPACT_MISSING_SECTION,
            readiness_result=READINESS_LP001,
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state=None,
            repository_full_name="squne121/loop-protocol",
            issue_number=1532,
            refinement_session_id="session-abc",
            iteration_id="iteration-1",
        )
        replay_next_state_line = canonical_replay_next_state_line(binding_artifact)
        v1_text = _v1_needs_fix_envelope_text(
            artifact_path=artifact_path,
            replay_artifact_digest="sha256:" + "0" * 64,  # malformed but well-shaped
        )
        v2_text = "\n".join(
            [
                v1_text,
                f"REPLAY_NEXT_STATE: {replay_next_state_line}",
                f"REPLAY_PARENT_BINDING_DIGEST: {binding_artifact['binding_digest']}",
            ]
        )
        result = validate_review_compact_output_v2(
            v2_text,
            issue_number=1532,
            expected_replay_next_state=replay_next_state_line,
            expected_parent_binding_digest=binding_artifact["binding_digest"],
        )
        # REPLAY_ARTIFACT_DIGEST format is still well-shaped sha256:<hex>,
        # so this passes purely lexical V1 needs-fix validation; the parent
        # binding digest check succeeds independently.
        assert result["validation_status"] == "valid"


class TestSchemaGovernanceAndConsumerInventory:
    """Issue #1532 AC7: schema governance and issue-refinement-loop design
    SSOT register the V2 schemas, producer/consumer, trust boundary, and
    non-guarantees."""

    def test_v2_schema_governance_and_consumer_inventory(self):
        governance_text = (REPO_ROOT / "docs" / "dev" / "schema-governance.md").read_text(
            encoding="utf-8"
        )
        for token in (
            "PARENT_REPLAY_BINDING_ARTIFACT_V1",
            "REVIEW_COMPACT_VALIDATION_RESULT_V2",
            "REPLAY_PARENT_BINDING_DIGEST",
        ):
            assert token in governance_text, f"schema-governance.md missing {token!r}"

        design_text = (
            REPO_ROOT / "docs" / "dev" / "workflows" / "issue-refinement-loop-design.md"
        ).read_text(encoding="utf-8")
        for token in (
            "PARENT_REPLAY_BINDING_ARTIFACT_V1",
            "REVIEWER_BLOCKER_CLAIM_V1",
            "parent_replay_binding.py",
        ):
            assert token in design_text, f"issue-refinement-loop-design.md missing {token!r}"
