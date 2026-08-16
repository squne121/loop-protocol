"""Behavioral tests for AC3 (provenance binding) of `run_repair_action_apply()`
(Issue #2039 AC2/AC3/AC6).

GIVEN a repair_action candidate and a caller-declared `expected_provenance`
binding, WHEN `run_repair_action_apply()` validates provenance, THEN it must
reject cross-Issue, old-run (stale), replacement, and candidate-digest
mismatch cases BEFORE any GitHub read/mutation is attempted, and only
dispatch once every declared expectation matches exactly.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import jsonschema
import pytest

_SKILL_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import run_refinement_preflight as rrp  # noqa: E402
from run_refinement_preflight import (  # noqa: E402
    REPAIR_APPLY_FAILURE_CROSS_ISSUE,
    REPAIR_APPLY_FAILURE_DIGEST_MISMATCH,
    REPAIR_APPLY_FAILURE_REPLACEMENT,
    REPAIR_APPLY_FAILURE_STALE_RUN,
    _repair_action_core_sha256,
    _validate_repair_apply_provenance_binding,
)

_SCHEMA = json.loads((_SKILL_ROOT / "schemas" / "repair_apply_result_v1.schema.json").read_text(encoding="utf-8"))

ORIGINAL_BODY = "original body\n"
REPAIRED_BODY = "repaired body\n"


def _hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _base_provenance() -> dict:
    return {
        "repo": "squne121/loop-protocol",
        "issue_number": 2039,
        "original_body_sha256": _hex(ORIGINAL_BODY),
        "original_updated_at": "2024-01-01T00:00:00Z",
        "preflight_run_identity": "sha256:run-a",
        "producer_schema_version": "repair_action/v1",
        "producer_policy_version": "deterministic-issue-repair/v1",
        "repair_action_core_sha256": "core-hash-a",
        "candidate_digest": _hex(REPAIRED_BODY),
        "source_lane": "unanchored",
        "source_refs_digest": None,
    }


# ---------------------------------------------------------------------------
# Unit-level `_validate_repair_apply_provenance_binding` tests
# ---------------------------------------------------------------------------


def test_self_consistent_repo_and_issue_with_no_expectation_passes():
    provenance = _base_provenance()
    result = _validate_repair_apply_provenance_binding(
        provenance, repo="squne121/loop-protocol", issue_number=2039, expected_provenance=None
    )
    assert result is None


def test_repo_mismatch_against_requested_target_is_cross_issue():
    provenance = _base_provenance()
    provenance["repo"] = "someone-else/other-repo"
    result = _validate_repair_apply_provenance_binding(
        provenance, repo="squne121/loop-protocol", issue_number=2039, expected_provenance=None
    )
    assert result == REPAIR_APPLY_FAILURE_CROSS_ISSUE


def test_issue_number_mismatch_against_requested_target_is_cross_issue():
    provenance = _base_provenance()
    provenance["issue_number"] = 9999
    result = _validate_repair_apply_provenance_binding(
        provenance, repo="squne121/loop-protocol", issue_number=2039, expected_provenance=None
    )
    assert result == REPAIR_APPLY_FAILURE_CROSS_ISSUE


def test_expected_run_identity_mismatch_is_stale_run():
    provenance = _base_provenance()
    result = _validate_repair_apply_provenance_binding(
        provenance,
        repo="squne121/loop-protocol",
        issue_number=2039,
        expected_provenance={"preflight_run_identity": "sha256:run-b"},
    )
    assert result == REPAIR_APPLY_FAILURE_STALE_RUN


def test_expected_core_hash_mismatch_is_replacement():
    provenance = _base_provenance()
    result = _validate_repair_apply_provenance_binding(
        provenance,
        repo="squne121/loop-protocol",
        issue_number=2039,
        expected_provenance={"repair_action_core_sha256": "core-hash-different"},
    )
    assert result == REPAIR_APPLY_FAILURE_REPLACEMENT


def test_expected_candidate_digest_mismatch_is_digest_mismatch():
    provenance = _base_provenance()
    result = _validate_repair_apply_provenance_binding(
        provenance,
        repo="squne121/loop-protocol",
        issue_number=2039,
        expected_provenance={"candidate_digest": "0" * 64},
    )
    assert result == REPAIR_APPLY_FAILURE_DIGEST_MISMATCH


def test_all_expectations_matching_passes():
    provenance = _base_provenance()
    result = _validate_repair_apply_provenance_binding(
        provenance,
        repo="squne121/loop-protocol",
        issue_number=2039,
        expected_provenance={
            "repo": provenance["repo"],
            "issue_number": provenance["issue_number"],
            "preflight_run_identity": provenance["preflight_run_identity"],
            "repair_action_core_sha256": provenance["repair_action_core_sha256"],
            "candidate_digest": provenance["candidate_digest"],
        },
    )
    assert result is None


def test_repair_action_core_sha256_is_stable_for_identical_core_fields():
    action_a = {
        "schema_version": "repair_action/v1",
        "policy_version": "deterministic-issue-repair/v1",
        "disposition": "auto_apply_safe",
        "original_body_sha256": "aaa",
        "repaired_body_sha256": "bbb",
        "repair_kinds": ["trailing_whitespace"],
        "reason_codes": ["trailing_whitespace_stripped"],
        "candidate_body_artifact": "/some/path/that/is/not/in/the/core",
    }
    action_b = dict(action_a)
    action_b["candidate_body_artifact"] = "/a/totally/different/path"
    assert _repair_action_core_sha256(action_a) == _repair_action_core_sha256(action_b)


def test_repair_action_core_sha256_changes_when_disposition_changes():
    action_a = {
        "schema_version": "repair_action/v1",
        "policy_version": "deterministic-issue-repair/v1",
        "disposition": "auto_apply_safe",
        "original_body_sha256": "aaa",
        "repaired_body_sha256": "bbb",
        "repair_kinds": [],
        "reason_codes": [],
    }
    action_b = dict(action_a)
    action_b["disposition"] = "human_review_required"
    assert _repair_action_core_sha256(action_a) != _repair_action_core_sha256(action_b)


# ---------------------------------------------------------------------------
# End-to-end: run_repair_action_apply() rejects before any GitHub read
# ---------------------------------------------------------------------------


def _write_candidate(tmp_path: Path, *, issue_number: int = 2039) -> Path:
    artifact_dir = tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = artifact_dir / "candidate_body.md"
    candidate_path.write_text(REPAIRED_BODY)
    repair_action = {
        "schema_version": "repair_action/v1",
        "policy_version": "deterministic-issue-repair/v1",
        "disposition": "auto_apply_safe",
        "original_body_sha256": _hex(ORIGINAL_BODY),
        "repaired_body_sha256": _hex(REPAIRED_BODY),
        "diagnostics_artifact": None,
        "candidate_body_artifact": str(candidate_path),
        "repair_kinds": [],
        "reason_codes": [],
        # PR #2202 review fix (P0-2): these live under repair_action.* in
        # the canonical schema, not top-level.
        "source_lane": "unanchored",
        "preflight_run_identity": "sha256:testrun",
        "original_updated_at": "2024-01-01T00:00:00Z",
        "source_refs_digest": None,
    }
    preflight_result = {
        "schema_version": "refinement_preflight_result/v1",
        "status": "needs_fix",
        "issue_number": issue_number,
        "repo": "squne121/loop-protocol",
        "planner_exit_code": None,
        "planner_fail_closed": None,
        "next_action": "apply_deterministic_repair",
        "must_read": [],
        "do_not_read": [],
        "commands": [],
        "blockers": [],
        "artifacts": {},
        "hashes": {"result_core_sha256": "sha256:testrun"},
        "repair_action": repair_action,
    }
    result_path = artifact_dir / "preflight_result.json"
    result_path.write_text(json.dumps(preflight_result))
    return result_path


def test_expected_provenance_mismatch_rejects_before_any_github_read(tmp_path: Path) -> None:
    result_path = _write_candidate(tmp_path)

    fetch_calls = []

    def _fetch_should_never_run():
        fetch_calls.append(None)
        return {"body": ORIGINAL_BODY, "updatedAt": "2024-01-01T00:00:00Z"}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_should_never_run,
        expected_provenance={"preflight_run_identity": "sha256:some-other-run"},
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["failure_code"] == REPAIR_APPLY_FAILURE_STALE_RUN
    assert result["mutation_outcome"] == "not_attempted"
    assert result["phase"] == "provenance_validation"
    assert fetch_calls == [], "provenance mismatch must be rejected before any live Issue read"


def test_expected_provenance_full_match_proceeds_to_dispatch(tmp_path: Path) -> None:
    result_path = _write_candidate(tmp_path)
    data = json.loads(result_path.read_text())
    expected_core_hash = _repair_action_core_sha256(data["repair_action"])

    dispatched = []

    def _apply_transaction(current_issue: dict, candidate_body: str) -> dict:
        dispatched.append(candidate_body)
        return {
            "status": "ok",
            "mutation_started": True,
            "body_update": {
                "attempted": True,
                "status": "ok",
                "remote_current_body_sha256": f"sha256:{_hex(REPAIRED_BODY)}",
            },
            "content_update": {"patch_attempted": True, "mutation_outcome": "applied"},
        }

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=lambda: {"body": ORIGINAL_BODY, "updatedAt": "2024-01-01T00:00:00Z"},
        apply_transaction=_apply_transaction,
        expected_provenance={
            "repo": "squne121/loop-protocol",
            "issue_number": 2039,
            "preflight_run_identity": "sha256:testrun",
            "repair_action_core_sha256": expected_core_hash,
            "candidate_digest": _hex(REPAIRED_BODY),
        },
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["mutation_outcome"] == "applied"
    assert dispatched == [REPAIRED_BODY]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
