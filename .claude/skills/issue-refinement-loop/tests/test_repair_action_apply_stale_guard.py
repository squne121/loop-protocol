"""Behavioral tests for the AC4 pre-dispatch rebase "stale candidate" guard
of `run_repair_action_apply()` (Issue #2039 AC1/AC4/AC5/AC6).

GIVEN a candidate whose recorded original body no longer matches the live
Issue body, WHEN `run_repair_action_apply()` rebases, THEN it must: rerun
the producer AT MOST ONCE; fail closed (no mutation dispatched) on a second
drift, a non-safe disposition after the rerun, or an unreconstructable rerun
result; and never fall back to textually patching the stale candidate.
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
        "repair_kinds": ["trailing_whitespace"],
        "reason_codes": ["trailing_whitespace_stripped"],
    }
    preflight_result = {
        "schema": "issue_refinement_preflight_result/v1",
        "repair_action": repair_action,
        "original_updated_at": "2024-01-01T00:00:00Z",
        "result_core_sha256": "sha256:testrun",
        "source_lane": "unanchored",
    }
    result_path = artifact_dir / "preflight_result.json"
    result_path.write_text(json.dumps(preflight_result))
    return result_path


class RecordingApplyTransaction:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, current_issue: dict, candidate_body: str) -> dict:
        self.calls += 1
        return {"status": "ok", "body_attempted": True, "remote_current_body_sha256": f"sha256:{_hex(candidate_body)}"}


def test_second_drift_fails_closed_with_no_dispatch(tmp_path: Path) -> None:
    """AC4: the live body drifts again between the rerun's basis and the
    second (post-rerun) read -> second_body_drift, mutation never
    dispatched."""
    result_path = _write_candidate(tmp_path)
    drift_1 = "first drifted body\n"
    drift_2 = "second, different drifted body\n"
    fetch_bodies = iter([drift_1, drift_2])

    def _fetch():
        return {"body": next(fetch_bodies), "updatedAt": "2024-01-01T00:00:00Z"}

    def _rerun_producer(body: str):
        assert body == drift_1
        rebase_dir = tmp_path / "rebase-artifacts"
        rebase_dir.mkdir(parents=True, exist_ok=True)
        candidate_path = rebase_dir / "rebased.md"
        candidate_path.write_text("rebased once\n")
        return {
            "schema_version": "repair_action/v1",
            "policy_version": "deterministic-issue-repair/v1",
            "disposition": "auto_apply_safe",
            "original_body_sha256": _hex(drift_1),
            "repaired_body_sha256": _hex("rebased once\n"),
            "candidate_body_artifact": str(candidate_path),
            "repair_kinds": [],
            "reason_codes": [],
        }, None

    apply_txn = RecordingApplyTransaction()

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
        apply_transaction=apply_txn,
        rerun_producer=_rerun_producer,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["failure_code"] == "second_body_drift"
    assert result["mutation_outcome"] == "not_attempted"
    assert result["rebase"] == {
        "attempted": True,
        "producer_reruns": 1,
        "drift_detected": True,
        "second_drift": True,
    }
    assert apply_txn.calls == 0, "a second drift must never reach transaction dispatch"


def test_non_safe_disposition_after_rerun_fails_closed(tmp_path: Path) -> None:
    """AC4: the producer rerun against the fresh live body no longer
    classifies as auto_apply_safe -> fail closed, no mutation."""
    result_path = _write_candidate(tmp_path)
    drifted_body = "some other live body\n"

    def _fetch():
        return {"body": drifted_body, "updatedAt": "2024-01-01T00:00:00Z"}

    def _rerun_producer(body: str):
        assert body == drifted_body
        return {
            "schema_version": "repair_action/v1",
            "policy_version": "deterministic-issue-repair/v1",
            "disposition": "human_review_required",
            "original_body_sha256": _hex(drifted_body),
            "repaired_body_sha256": None,
            "candidate_body_artifact": None,
            "repair_kinds": [],
            "reason_codes": [],
        }, None

    apply_txn = RecordingApplyTransaction()

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
        apply_transaction=apply_txn,
        rerun_producer=_rerun_producer,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["failure_code"] == "non_safe_disposition_after_rerun"
    assert result["mutation_outcome"] == "not_attempted"
    assert result["rebase"]["attempted"] is True
    assert result["rebase"]["producer_reruns"] == 1
    assert apply_txn.calls == 0


def test_rerun_producer_failure_is_provenance_unreconstructable(tmp_path: Path) -> None:
    """AC4: when the rerun producer itself cannot be reconstructed (e.g. the
    subprocess/materialization failed), that must fail closed distinctly
    from a disposition classification -- never silently treated as
    'no drift'."""
    result_path = _write_candidate(tmp_path)
    drifted_body = "yet another live body\n"

    def _fetch():
        return {"body": drifted_body, "updatedAt": "2024-01-01T00:00:00Z"}

    def _rerun_producer(body: str):
        assert body == drifted_body
        return None, "rerun_dry_run_error:boom"

    apply_txn = RecordingApplyTransaction()

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
        apply_transaction=apply_txn,
        rerun_producer=_rerun_producer,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["failure_code"] == "provenance_unreconstructable"
    assert result["mutation_outcome"] == "not_attempted"
    assert apply_txn.calls == 0


def test_no_drift_does_not_invoke_rerun_producer_at_all(tmp_path: Path) -> None:
    """AC4: when the live body matches the candidate's recorded original
    body, the (injected) rerun producer must never be called -- rebase is
    strictly drift-triggered, not unconditional."""
    result_path = _write_candidate(tmp_path)

    def _fetch():
        return {"body": ORIGINAL_BODY, "updatedAt": "2024-01-01T00:00:00Z"}

    def _rerun_producer(body: str):
        raise AssertionError("rerun_producer must not be called when there is no drift")

    apply_txn = RecordingApplyTransaction()

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
        apply_transaction=apply_txn,
        rerun_producer=_rerun_producer,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["rebase"]["attempted"] is False
    assert apply_txn.calls == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
