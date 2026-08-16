"""Behavioral tests for `run_repair_action_apply()` end-to-end dispatch
(Issue #2039 AC1/AC4/AC5/AC6).

GIVEN a preflight result carrying an `auto_apply_safe` repair_action
candidate, WHEN `run_repair_action_apply()` runs the full consumer flow,
THEN it must: reuse the exactly-one intent arbiter (AC1) before touching
GitHub; rebase on pre-dispatch body drift instead of textually patching the
stale candidate (AC4); losslessly project the edit_issue_txn.py receipt,
never collapsing `unknown` (AC6); and keep the post-dispatch retry budget at
0 (AC5).
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
    disposition: str = "auto_apply_safe",
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
        "disposition": disposition,
        "original_body_sha256": _hex(original_body),
        "repaired_body_sha256": _hex(candidate_body),
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
        "source_lane": source_lane,
    }
    result_path = artifact_dir / "preflight_result.json"
    result_path.write_text(json.dumps(preflight_result))
    return result_path


def _fetch_stub(body: str = ORIGINAL_BODY):
    def _fetch():
        return {"body": body, "updatedAt": "2024-01-01T00:00:00Z"}

    return _fetch


class CallCountingApplyTransaction:
    """Records how many times the transaction dispatch closure is invoked,
    so a test can prove no blind retry happened (AC5)."""

    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple[dict, str]] = []

    def __call__(self, current_issue: dict, candidate_body: str) -> dict:
        self.calls.append((current_issue, candidate_body))
        return self.result


def test_no_drift_happy_path_dispatches_and_is_schema_conformant(tmp_path: Path) -> None:
    """AC1/AC6: no body drift -> exactly one dispatch, lossless applied
    receipt, schema conformant."""
    result_path = _write_candidate(tmp_path)
    apply_txn = CallCountingApplyTransaction(
        {
            "status": "ok",
            "body_attempted": True,
            "remote_current_body_sha256": f"sha256:{_hex(REPAIRED_BODY)}",
            "errors": [],
        }
    )

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_stub(),
        apply_transaction=apply_txn,
    )

    jsonschema.validate(result, _SCHEMA)
    assert len(apply_txn.calls) == 1
    assert result["mutation_outcome"] == "applied"
    assert result["phase"] == "complete"
    assert result["failure_code"] is None
    assert result["rebase"] == {
        "attempted": False,
        "producer_reruns": 0,
        "drift_detected": False,
        "second_drift": False,
    }
    assert result["receipt"]["final_readback"]["digest_class"] == "candidate"


def test_missing_original_body_sha256_is_provenance_unreconstructable(tmp_path: Path) -> None:
    """AC3/AC4: a candidate that cannot even state its own original body SHA
    cannot be drift-checked, and is rejected before any GitHub read."""
    result_path = _write_candidate(tmp_path)
    data = json.loads(result_path.read_text())
    data["repair_action"]["original_body_sha256"] = ""
    result_path.write_text(json.dumps(data))

    calls: list[None] = []

    def _fetch_should_not_be_called():
        calls.append(None)
        return {"body": ORIGINAL_BODY, "updatedAt": "2024-01-01T00:00:00Z"}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_should_not_be_called,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["failure_code"] == "provenance_unreconstructable"
    assert result["mutation_outcome"] == "not_attempted"
    assert calls == [], "must not read the live Issue before provenance is reconstructable"


@pytest.mark.parametrize(
    ("txn_status", "expected_outcome"),
    [
        ("no_change", "no_change"),
        ("human_judgment", "not_attempted"),
        ("failed_after_mutation", "unknown"),
        ("mutation_outcome_unknown", "unknown"),
    ],
)
def test_receipt_projection_is_lossless_across_statuses(
    tmp_path: Path, txn_status: str, expected_outcome: str
) -> None:
    """AC6: every edit_issue_txn.py status must project to its own distinct
    mutation_outcome; `unknown` must never collapse into
    failed/not_attempted/no_change."""
    result_path = _write_candidate(tmp_path)
    apply_txn = CallCountingApplyTransaction({"status": txn_status, "errors": []})

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_stub(),
        apply_transaction=apply_txn,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["mutation_outcome"] == expected_outcome
    assert result["receipt"]["executor_status"] == txn_status
    assert len(apply_txn.calls) == 1
    if expected_outcome == "unknown":
        assert result["phase"] != "complete"
        assert result["failure_code"] == "final_readback_unresolvable"
    else:
        assert result["phase"] == "complete"


def test_body_drift_triggers_single_producer_rerun_and_dispatches_new_candidate(tmp_path: Path) -> None:
    """AC4: pre-dispatch body drift reruns the producer exactly once against
    the fresh live body, then dispatches the REGENERATED candidate (never
    the stale one, never a textual rebase of it)."""
    result_path = _write_candidate(tmp_path)
    drifted_body = "a completely different live body\n"
    rebased_candidate_body = "rebased repaired body\n"

    rerun_calls: list[str] = []

    def _rerun_producer(body: str):
        rerun_calls.append(body)
        assert body == drifted_body
        rebase_dir = (
            tmp_path / ".claude" / "artifacts" / "issue-refinement-loop" / "2039" / "repair-action-apply" / "rebase"
        )
        rebase_dir.mkdir(parents=True, exist_ok=True)
        candidate_path = rebase_dir / "repaired_issue_body.md"
        candidate_path.write_text(rebased_candidate_body)
        return {
            "schema_version": "repair_action/v1",
            "policy_version": "deterministic-issue-repair/v1",
            "disposition": "auto_apply_safe",
            "original_body_sha256": _hex(drifted_body),
            "repaired_body_sha256": _hex(rebased_candidate_body),
            "candidate_body_artifact": str(candidate_path),
            "repair_kinds": ["trailing_whitespace"],
            "reason_codes": ["trailing_whitespace_stripped"],
        }, None

    fetch_bodies = iter([drifted_body, drifted_body])

    def _fetch():
        return {"body": next(fetch_bodies), "updatedAt": "2024-01-01T00:00:00Z"}

    apply_txn = CallCountingApplyTransaction(
        {"status": "ok", "body_attempted": True, "remote_current_body_sha256": f"sha256:{_hex(rebased_candidate_body)}"}
    )

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
    assert rerun_calls == [drifted_body]
    assert result["rebase"] == {
        "attempted": True,
        "producer_reruns": 1,
        "drift_detected": True,
        "second_drift": False,
    }
    assert result["mutation_outcome"] == "applied"
    assert len(apply_txn.calls) == 1
    dispatched_body = apply_txn.calls[0][1]
    assert dispatched_body == rebased_candidate_body


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
