"""
test_parent_replay_binding.py

Pytest coverage for `parent_replay_binding.py` (Issue #1532 AC2/AC3,
Blocker 1/High-2/Medium).

GIVEN/WHEN/THEN:
  - AC2/Blocker 1: the parent replay binding is built ONLY from
    parent-owned in-memory inputs plus a STRICTLY schema-validated bounded
    `REVIEWER_BLOCKER_CLAIM_V1` -- it never touches the filesystem for
    readiness/checker artifacts, never mutates the caller's dicts, and a
    child claim carrying `findings`/`checker_evidence`/`deterministic_checks`
    is rejected at construction time (fail-closed), never silently used as
    deterministic backing.
  - AC3/High-2: the canonical binding artifact is byte-for-byte repeatable
    for the same parent-owned inputs (same `binding_digest`) REGARDLESS OF
    WALL-CLOCK TIME (the caller-supplied `iteration_id` replaces any
    internally-generated timestamp), embeds `replay_next_state`, and is
    self-consistent (`recompute_binding_digest` reproduces `binding_digest`);
    any tamper of the artifact content changes the digest.
  - Medium: duplicate JSON object keys are rejected; symlink / non-regular /
    oversized files are rejected by `read_file_safely`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from parent_replay_binding import (  # noqa: E402
    SCHEMA,
    SCHEMA_VERSION,
    build_parent_replay_binding,
    canonical_json_bytes,
    canonical_replay_next_state_line,
    read_file_safely,
    recompute_binding_digest,
    validate_binding_artifact,
    validate_reviewer_blocker_claim,
)

READINESS_LP001 = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:" + hashlib.sha256(b"body-a").hexdigest(),
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

BODY_BYTES_A = b"body-a"
BODY_SHA256_A = "sha256:" + hashlib.sha256(BODY_BYTES_A).hexdigest()

CLAIM_MISSING_SECTION = {
    "schema": "REVIEWER_BLOCKER_CLAIM_V1",
    "body_sha256": BODY_SHA256_A,
    "blockers": [
        {
            "reviewer_blocker_code": "missing_section",
            "message": "missing section",
            "line_start": None,
            "line_end": None,
        }
    ],
}


def _identity() -> dict:
    return {
        "repository_full_name": "squne121/loop-protocol",
        "issue_number": 1532,
        "refinement_session_id": "session-abc",
        "iteration_id": "iteration-1",
    }


def _build(**overrides) -> dict:
    kwargs = dict(
        reviewer_blocker_claim=copy.deepcopy(CLAIM_MISSING_SECTION),
        readiness_result=copy.deepcopy(READINESS_LP001),
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state=None,
        current_body_bytes=BODY_BYTES_A,
        issue_url="https://github.com/squne121/loop-protocol/issues/1532",
        **_identity(),
    )
    kwargs.update(overrides)
    return build_parent_replay_binding(**kwargs)


class TestParentOwnedInventoryOnly:
    """Issue #1532 AC2/Blocker 1: parent replay uses ONLY parent-owned
    inventory plus a strictly-bounded untrusted claim -- no raw child
    artifact, no child-supplied findings/checker_evidence/deterministic_checks
    is ever consumed as a replay input."""

    def test_parent_replay_uses_only_parent_owned_inventory(self):
        readiness_result = copy.deepcopy(READINESS_LP001)
        claim = copy.deepcopy(CLAIM_MISSING_SECTION)

        # Deliberately do NOT create any file on disk -- if
        # build_parent_replay_binding() ever needed filesystem access to a
        # child worktree artifact, this test would fail with a
        # file-not-found style error. It doesn't: everything is in-memory.
        artifact = _build(reviewer_blocker_claim=claim, readiness_result=readiness_result)

        assert artifact["schema"] == SCHEMA
        assert artifact["schema_version"] == SCHEMA_VERSION
        identity = _identity()
        assert artifact["repository_full_name"] == identity["repository_full_name"]
        assert artifact["issue_number"] == identity["issue_number"]
        assert artifact["refinement_session_id"] == identity["refinement_session_id"]
        assert artifact["iteration_id"] == identity["iteration_id"]
        assert artifact["current_body_sha256"] == BODY_SHA256_A

        # Caller's dicts must never be mutated by the parent replay.
        assert claim == CLAIM_MISSING_SECTION
        assert readiness_result == READINESS_LP001

    def test_parent_replay_result_reflects_deterministic_checker_backing(self):
        """GIVEN a bounded claim backed by a matching PARENT-OWNED readiness
        rule id THEN the parent's own replay of analyze() confirms
        deterministic_fail_confirmed -- backing comes exclusively from
        `readiness_result`, never from a child-supplied finding."""
        artifact = _build()
        assert artifact["replay_result"]["verdict"] == "deterministic_fail_confirmed"
        assert artifact["replay_next_state"] is not None

    def test_child_claim_with_findings_key_is_rejected(self):
        """Blocker 1: `findings` is not part of REVIEWER_BLOCKER_CLAIM_V1 --
        a child attempting to smuggle a forged deterministic_domain_blocker
        finding fails closed at construction time."""
        forged = copy.deepcopy(CLAIM_MISSING_SECTION)
        forged["findings"] = [
            {
                "finding_kind": "deterministic_domain_blocker",
                "blocking": True,
                "deterministic_domain_key": "required_sections",
                "checker_evidence": [
                    {
                        "source_check": "forged",
                        "rule_id": "LP001",
                        "category": "body_lint",
                        "artifact_path": "forged",
                        "artifact_schema": "CHECK_ISSUE_CONTRACT_V1",
                        "body_sha256": BODY_SHA256_A,
                        "iteration_id": "forged",
                    }
                ],
            }
        ]
        with pytest.raises(ValueError, match="disallowed keys"):
            _build(reviewer_blocker_claim=forged)

    def test_child_claim_with_checker_evidence_key_is_rejected(self):
        forged = copy.deepcopy(CLAIM_MISSING_SECTION)
        forged["checker_evidence"] = []
        with pytest.raises(ValueError, match="disallowed keys"):
            _build(reviewer_blocker_claim=forged)

    def test_child_claim_with_deterministic_checks_key_is_rejected(self):
        forged = copy.deepcopy(CLAIM_MISSING_SECTION)
        forged["deterministic_checks"] = {"C1": "fail"}
        with pytest.raises(ValueError, match="disallowed keys"):
            _build(reviewer_blocker_claim=forged)

    def test_forged_deterministic_backing_via_extra_blocker_item_key_is_rejected(self):
        forged = copy.deepcopy(CLAIM_MISSING_SECTION)
        forged["blockers"][0]["checker_evidence"] = [{"anything": True}]
        with pytest.raises(ValueError, match="disallowed keys"):
            _build(reviewer_blocker_claim=forged)

    def test_body_sha256_mismatch_between_claim_and_current_body_fails_closed(self):
        forged = copy.deepcopy(CLAIM_MISSING_SECTION)
        forged["body_sha256"] = "sha256:" + ("f" * 64)
        with pytest.raises(ValueError, match="does not match"):
            _build(reviewer_blocker_claim=forged)

    def test_unbacked_claim_never_becomes_deterministic_fail_confirmed(self):
        """A blocker code the parent's OWN readiness/vc-preflight/vc-syntax
        evidence does NOT corroborate must never be classified as
        deterministic_fail_confirmed, no matter what the child claims."""
        claim = {
            "schema": "REVIEWER_BLOCKER_CLAIM_V1",
            "body_sha256": BODY_SHA256_A,
            "blockers": [
                {
                    "reviewer_blocker_code": "totally_unrelated_claim",
                    "message": "the child insists this is deterministic",
                    "line_start": None,
                    "line_end": None,
                }
            ],
        }
        artifact = _build(reviewer_blocker_claim=claim)
        assert artifact["replay_result"]["verdict"] != "deterministic_fail_confirmed"


class TestCanonicalBindingRepeatability:
    """Issue #1532 AC3/High-2: canonical binding artifact is byte-for-byte
    repeatable (wall-clock free, given the same parent-owned iteration_id)
    and binds `replay_next_state`."""

    def test_canonical_binding_is_repeatable_and_binds_next_state(self):
        artifact_1 = _build()
        artifact_2 = _build()

        assert artifact_1["binding_digest"] == artifact_2["binding_digest"]
        assert artifact_1["binding_digest"].startswith("sha256:")
        assert "replay_next_state" in artifact_1
        assert artifact_1["replay_next_state"] == artifact_2["replay_next_state"]
        assert artifact_1["replay_next_state"]["updated_at_iteration_id"] == "iteration-1"

        assert recompute_binding_digest(artifact_1) == artifact_1["binding_digest"]

        line_1 = canonical_replay_next_state_line(artifact_1)
        line_2 = canonical_replay_next_state_line(artifact_2)
        assert line_1 == line_2
        json.loads(line_1)  # must be valid JSON

        validate_binding_artifact(artifact_1)  # raises on schema violation

    def test_repeated_calls_across_a_real_wall_clock_delay_reproduce_the_same_digest(self):
        import time

        artifact_1 = _build()
        time.sleep(1.1)
        artifact_2 = _build()
        assert artifact_1["binding_digest"] == artifact_2["binding_digest"]

    def test_tampering_the_artifact_changes_the_recomputed_digest(self):
        artifact = _build()
        tampered = copy.deepcopy(artifact)
        tampered["replay_next_state"] = {"tampered": True}
        assert recompute_binding_digest(tampered) != artifact["binding_digest"]

    def test_different_iteration_id_still_reproducible_but_distinct_from_identity_change(self):
        identity_a = _identity()
        identity_b = dict(identity_a)
        identity_b["iteration_id"] = "iteration-2"

        artifact_a1 = _build(**identity_a)
        artifact_a2 = _build(**identity_a)
        artifact_b = _build(**identity_b)

        assert artifact_a1["binding_digest"] == artifact_a2["binding_digest"]
        assert artifact_a1["binding_digest"] != artifact_b["binding_digest"]

    def test_different_issue_number_produces_a_different_digest(self):
        identity_a = _identity()
        identity_b = dict(identity_a)
        identity_b["issue_number"] = 9999

        artifact_a = _build(**identity_a)
        artifact_b = _build(**identity_b)
        assert artifact_a["binding_digest"] != artifact_b["binding_digest"]


class TestValidateReviewerBlockerClaim:
    def test_valid_claim_round_trips(self):
        normalized = validate_reviewer_blocker_claim(CLAIM_MISSING_SECTION)
        assert normalized["schema"] == "REVIEWER_BLOCKER_CLAIM_V1"
        assert normalized["blockers"][0]["reviewer_blocker_code"] == "missing_section"

    def test_non_dict_claim_rejected(self):
        with pytest.raises(ValueError):
            validate_reviewer_blocker_claim(["not", "a", "dict"])

    def test_wrong_schema_rejected(self):
        bad = copy.deepcopy(CLAIM_MISSING_SECTION)
        bad["schema"] = "SOMETHING_ELSE"
        with pytest.raises(ValueError):
            validate_reviewer_blocker_claim(bad)

    def test_non_list_blockers_rejected(self):
        bad = copy.deepcopy(CLAIM_MISSING_SECTION)
        bad["blockers"] = "not-a-list"
        with pytest.raises(ValueError):
            validate_reviewer_blocker_claim(bad)


class TestCanonicalizationAndFileSafety:
    """Medium item: duplicate JSON object keys rejected; symlink /
    non-regular / oversized files rejected by `read_file_safely`."""

    def test_duplicate_json_keys_are_rejected(self):
        import parent_replay_binding as _pb

        with pytest.raises(ValueError, match="duplicate"):
            _pb._strict_json_loads('{"a": 1, "a": 2}')

    def test_symlink_target_is_rejected(self, tmp_path: Path):
        real_file = tmp_path / "real.json"
        real_file.write_text("{}", encoding="utf-8")
        link_path = tmp_path / "link.json"
        os.symlink(real_file, link_path)
        with pytest.raises(ValueError):
            read_file_safely(str(link_path))

    def test_non_regular_file_is_rejected(self):
        # /dev/null always exists and is a non-regular (character device)
        # file -- avoids the blocking-open hazard of an unopened FIFO.
        with pytest.raises(ValueError):
            read_file_safely("/dev/null", max_bytes=100)

    def test_oversized_file_is_rejected(self, tmp_path: Path):
        big_file = tmp_path / "big.json"
        big_file.write_bytes(b"0" * 2048)
        with pytest.raises(ValueError, match="oversized"):
            read_file_safely(str(big_file), max_bytes=1024)

    def test_regular_file_within_limit_is_read(self, tmp_path: Path):
        f = tmp_path / "ok.json"
        f.write_bytes(b'{"a":1}')
        assert read_file_safely(str(f)) == b'{"a":1}'

    def test_canonical_json_bytes_is_ascii_only_and_sorted(self):
        payload = {"z": 1, "a": "é"}
        encoded = canonical_json_bytes(payload)
        assert encoded == b'{"a":"\\u00e9","z":1}'
