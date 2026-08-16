"""Behavioral tests for post-mutation fresh validation of
`run_repair_action_apply()` (Issue #2039 AC9).

GIVEN a repair mutation attempt that reaches transaction dispatch, WHEN
`run_repair_action_apply()` performs its post-mutation fresh validation,
THEN it must: preserve the original human-context/anchor/unanchored
provenance lane and source refs/policy binding from before the mutation;
reject unconditional `with_anchor` conversion, source-lane promotion, and
lane mix-up; and succeed ONLY when the fresh result carries no actionable
repair AND the final body digest matches the expected post-mutation digest.
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

_SCHEMA = json.loads((_SKILL_ROOT / "schemas" / "repair_apply_result_v1.schema.json").read_text(encoding="utf-8"))

ORIGINAL_BODY = "original body\n"
REPAIRED_BODY = "repaired body\n"


def _hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_candidate(
    tmp_path: Path,
    *,
    issue_number: int = 2039,
    original_body: str = ORIGINAL_BODY,
    candidate_body: str = REPAIRED_BODY,
    source_lane: str = "unanchored",
) -> Path:
    artifact_dir = tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = artifact_dir / "candidate_body.md"
    candidate_path.write_text(candidate_body)
    repair_action = {
        "schema_version": "repair_action/v1",
        "policy_version": "deterministic-issue-repair/v1",
        "disposition": "auto_apply_safe",
        "original_body_sha256": _hex(original_body),
        "repaired_body_sha256": _hex(candidate_body),
        "diagnostics_artifact": None,
        "candidate_body_artifact": str(candidate_path),
        "repair_kinds": ["trailing_whitespace"],
        "reason_codes": ["trailing_whitespace_stripped"],
        # PR #2202 review fix (P0-2): these live under repair_action.* in
        # the canonical schema, not top-level.
        "source_lane": source_lane,
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


class CallCountingApplyTransaction:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple[dict, str]] = []

    def __call__(self, current_issue: dict, candidate_body: str) -> dict:
        self.calls.append((current_issue, candidate_body))
        return self.result


def _fetch_sequence(bodies: list[str]):
    it = iter(bodies)

    def _fetch():
        return {"body": next(it), "updatedAt": "2024-01-01T00:00:00Z"}

    return _fetch


def test_fresh_validation_succeeds_when_no_actionable_repair_and_digest_matches(tmp_path: Path) -> None:
    """AC9 happy path: after an applied dispatch, the live body already
    equals the candidate and a lane-preserving fresh producer reports no
    actionable repair remaining -> fresh_validation.status == success."""
    result_path = _write_candidate(tmp_path, source_lane="anchor")
    apply_txn = CallCountingApplyTransaction(
        {
            "status": "ok",
            "body_attempted": True,
            "remote_current_body_sha256": f"sha256:{_hex(REPAIRED_BODY)}",
            "errors": [],
        }
    )
    # fetch() is called: (1) precondition_read, (2) fresh-validation re-read.
    fetch = _fetch_sequence([ORIGINAL_BODY, REPAIRED_BODY])

    def _fresh_validate(body: str) -> dict:
        assert body == REPAIRED_BODY
        return {"error": None, "source_lane": "anchor", "actionable_repair": False}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=fetch,
        apply_transaction=apply_txn,
        fresh_validate=_fresh_validate,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["mutation_outcome"] == "applied"
    assert result["fresh_validation"] == {
        "status": "success",
        "source_lane_preserved": True,
        "actionable_repair_remaining": False,
        "final_body_digest_match": True,
    }


def test_fresh_validation_rejects_source_lane_promotion(tmp_path: Path) -> None:
    """AC9: a fresh result that reports a DIFFERENT (promoted) source lane
    than the one bound before the mutation must be rejected, never silently
    accepted as success."""
    result_path = _write_candidate(tmp_path, source_lane="unanchored")
    apply_txn = CallCountingApplyTransaction(
        {
            "status": "ok",
            "body_attempted": True,
            "remote_current_body_sha256": f"sha256:{_hex(REPAIRED_BODY)}",
            "errors": [],
        }
    )
    fetch = _fetch_sequence([ORIGINAL_BODY, REPAIRED_BODY])

    def _fresh_validate(body: str) -> dict:
        # Simulates an unconditional with_anchor / lane-promotion bug: the
        # fresh producer now claims "anchor" even though the original run
        # was "unanchored".
        return {"error": None, "source_lane": "anchor", "actionable_repair": False}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=fetch,
        apply_transaction=apply_txn,
        fresh_validate=_fresh_validate,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["provenance"]["source_lane"] == "unanchored"
    assert result["fresh_validation"]["status"] == "failed"
    assert result["fresh_validation"]["source_lane_preserved"] is False


def test_fresh_validation_rejects_lane_mixup_to_human_context(tmp_path: Path) -> None:
    """AC9: a fresh result that mixes up lanes (reports human_context when
    the original run was anchor) must be rejected."""
    result_path = _write_candidate(tmp_path, source_lane="anchor")
    apply_txn = CallCountingApplyTransaction(
        {
            "status": "ok",
            "body_attempted": True,
            "remote_current_body_sha256": f"sha256:{_hex(REPAIRED_BODY)}",
            "errors": [],
        }
    )
    fetch = _fetch_sequence([ORIGINAL_BODY, REPAIRED_BODY])

    def _fresh_validate(body: str) -> dict:
        return {"error": None, "source_lane": "human_context", "actionable_repair": False}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=fetch,
        apply_transaction=apply_txn,
        fresh_validate=_fresh_validate,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["fresh_validation"]["status"] == "failed"
    assert result["fresh_validation"]["source_lane_preserved"] is False


def test_fresh_validation_fails_when_actionable_repair_remains(tmp_path: Path) -> None:
    """AC9: even with a preserved lane and matching digest, an actionable
    repair still remaining in the fresh result must fail validation."""
    result_path = _write_candidate(tmp_path, source_lane="unanchored")
    apply_txn = CallCountingApplyTransaction(
        {
            "status": "ok",
            "body_attempted": True,
            "remote_current_body_sha256": f"sha256:{_hex(REPAIRED_BODY)}",
            "errors": [],
        }
    )
    fetch = _fetch_sequence([ORIGINAL_BODY, REPAIRED_BODY])

    def _fresh_validate(body: str) -> dict:
        return {"error": None, "source_lane": "unanchored", "actionable_repair": True}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=fetch,
        apply_transaction=apply_txn,
        fresh_validate=_fresh_validate,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["fresh_validation"]["status"] == "failed"
    assert result["fresh_validation"]["actionable_repair_remaining"] is True
    assert result["fresh_validation"]["source_lane_preserved"] is True


def test_fresh_validation_fails_when_digest_does_not_match(tmp_path: Path) -> None:
    """AC9: the live body digest must match the expected post-mutation
    digest (the AC5 authoritative-readback-classified digest); a concurrent
    third-party edit after dispatch must not be silently accepted."""
    result_path = _write_candidate(tmp_path, source_lane="unanchored")
    apply_txn = CallCountingApplyTransaction(
        {
            "status": "ok",
            "body_attempted": True,
            "remote_current_body_sha256": f"sha256:{_hex(REPAIRED_BODY)}",
            "errors": [],
        }
    )
    # After dispatch, a third party edits the body again before fresh
    # validation re-reads it.
    fetch = _fetch_sequence([ORIGINAL_BODY, "yet another third-party edit\n"])

    def _fresh_validate(body: str) -> dict:
        return {"error": None, "source_lane": "unanchored", "actionable_repair": False}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=fetch,
        apply_transaction=apply_txn,
        fresh_validate=_fresh_validate,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["fresh_validation"]["status"] == "failed"
    assert result["fresh_validation"]["final_body_digest_match"] is False


def test_fresh_validation_not_run_when_mutation_not_attempted(tmp_path: Path) -> None:
    """AC9: an outcome that never reached transaction dispatch has nothing
    post-mutation to validate -- fresh_validation stays not_run, never
    fabricated as success or failed."""
    result_path = _write_candidate(tmp_path)
    data = json.loads(result_path.read_text())
    data["repair_action"]["original_body_sha256"] = ""
    result_path.write_text(json.dumps(data))

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_sequence([ORIGINAL_BODY]),
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["mutation_outcome"] == "not_attempted"
    assert result["fresh_validation"] == {
        "status": "not_run",
        "source_lane_preserved": True,
        "actionable_repair_remaining": None,
        "final_body_digest_match": None,
    }


def test_fresh_validation_default_producer_preserves_lane_and_uses_real_repair_check(tmp_path: Path) -> None:
    """AC9: without an injected `fresh_validate`, the default producer must
    still (a) report back the same source_lane it was told to preserve
    (it has no reclassification capability of its own) and (b) determine
    actionable-repair-remaining from a real dry-run repair check rather
    than fabricating a fixed answer."""
    result_path = _write_candidate(tmp_path, source_lane="unanchored")
    apply_txn = CallCountingApplyTransaction(
        {
            "status": "ok",
            "body_attempted": True,
            "remote_current_body_sha256": f"sha256:{_hex(REPAIRED_BODY)}",
            "errors": [],
        }
    )
    fetch = _fetch_sequence([ORIGINAL_BODY, REPAIRED_BODY])

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=fetch,
        apply_transaction=apply_txn,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["fresh_validation"]["source_lane_preserved"] is True
    assert result["fresh_validation"]["status"] in {"success", "failed"}


def test_ac10_replay_of_already_applied_change_resolves_to_no_change(tmp_path: Path) -> None:
    """AC10: replaying the SAME candidate once the live body already
    matches its recorded repaired_body_sha256 must resolve to
    mutation_outcome=no_change, spend no rebase budget, and dispatch no
    GitHub mutation -- the change was already applied by a prior
    invocation."""
    result_path = _write_candidate(tmp_path)
    apply_txn = CallCountingApplyTransaction({"status": "ok", "body_attempted": True})

    def _rerun_producer_should_not_be_called(body: str):
        raise AssertionError("rerun_producer must not be called on an already-applied replay")

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_sequence([REPAIRED_BODY]),
        apply_transaction=apply_txn,
        rerun_producer=_rerun_producer_should_not_be_called,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["mutation_outcome"] == "no_change"
    assert result["phase"] == "complete"
    assert result["failure_code"] is None
    assert result["rebase"]["attempted"] is False
    assert result["rebase"]["producer_reruns"] == 0
    assert result["rebase"]["drift_detected"] is True
    assert apply_txn.calls == []
    assert result["historical_artifacts"] == {
        "physically_deleted": False,
        "latest_action_reference_invalidated": False,
    }


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
