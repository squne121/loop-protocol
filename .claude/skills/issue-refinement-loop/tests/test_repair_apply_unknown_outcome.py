"""Behavioral tests for AC5 (post-dispatch retry-budget separation and
authoritative-readback digest classification) of `run_repair_action_apply()`
(Issue #2039 AC1/AC4/AC5/AC6).

GIVEN a PATCH dispatch whose executor could not itself confirm the outcome,
WHEN the consumer resolves the result, THEN it must NEVER blind-retry the
mutation (post_dispatch_retry_budget stays 0, the transaction closure is
called exactly once), and must classify the live digest as
candidate/old/third via a single authoritative read -- never guessing.
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
    _classify_repair_apply_readback_digest,
    _repair_receipt_from_txn_result,
)

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
        "repair_kinds": [],
        "reason_codes": [],
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
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls = 0

    def __call__(self, current_issue: dict, candidate_body: str) -> dict:
        self.calls += 1
        return self.result


# ---------------------------------------------------------------------------
# Unit-level classification tests
# ---------------------------------------------------------------------------


def test_classify_readback_digest_candidate_when_remote_matches_candidate():
    assert _classify_repair_apply_readback_digest("sha256:abc", "abc", "def") == "candidate"


def test_classify_readback_digest_old_when_remote_matches_old():
    assert _classify_repair_apply_readback_digest("sha256:def", "abc", "def") == "old"


def test_classify_readback_digest_third_when_remote_matches_neither():
    assert _classify_repair_apply_readback_digest("sha256:zzz", "abc", "def") == "third"


def test_classify_readback_digest_unknown_when_remote_missing():
    assert _classify_repair_apply_readback_digest(None, "abc", "def") == "unknown"


def test_receipt_resolve_readback_not_called_for_confirmed_outcomes():
    """AC5: resolve_readback is called ONLY to disambiguate an unknown
    outcome, not for already-confirmed not_attempted/no_change/applied."""
    calls = []

    def _resolve():
        calls.append(None)
        return "sha256:should-not-be-used"

    receipt = _repair_receipt_from_txn_result(
        {"status": "no_change"}, candidate_digest="abc", old_digest="def", resolve_readback=_resolve
    )
    assert receipt["mutation_outcome"] == "no_change"
    assert calls == []


# ---------------------------------------------------------------------------
# End-to-end retry-budget / blind-retry-avoidance tests
# ---------------------------------------------------------------------------


def test_retry_budget_is_always_zero_and_never_blind_retries(tmp_path: Path) -> None:
    """AC5: post_dispatch_retry_budget/retries_used stay 0, and the
    transaction closure is invoked exactly ONCE even when the outcome is
    unknown -- proving no blind retry occurred."""
    result_path = _write_candidate(tmp_path)
    apply_txn = RecordingApplyTransaction({"status": "mutation_outcome_unknown", "errors": []})

    def _fetch():
        return {"body": ORIGINAL_BODY, "updatedAt": "2024-01-01T00:00:00Z"}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
        apply_transaction=apply_txn,
    )

    jsonschema.validate(result, _SCHEMA)
    assert apply_txn.calls == 1
    assert result["retry"] == {"post_dispatch_retry_budget": 0, "retries_used": 0}
    assert result["mutation_outcome"] == "unknown"
    assert result["phase"] != "complete"


def test_unknown_outcome_authoritative_readback_classifies_old_when_body_unchanged(tmp_path: Path) -> None:
    """AC5: when the executor's outcome is unknown and the post-dispatch
    authoritative read shows the body is STILL the pre-dispatch body, the
    receipt classifies it as `old` (nothing actually changed) rather than
    silently promoting mutation_outcome to no_change/applied."""
    result_path = _write_candidate(tmp_path)
    apply_txn = RecordingApplyTransaction({"status": "mutation_outcome_unknown", "errors": []})

    # Same body reported by every fetch() call (initial + the AC5 readback):
    # nothing actually changed server-side.
    def _fetch():
        return {"body": ORIGINAL_BODY, "updatedAt": "2024-01-01T00:00:00Z"}

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch,
        apply_transaction=apply_txn,
    )

    jsonschema.validate(result, _SCHEMA)
    # mutation_outcome MUST stay `unknown` -- the receipt digest_class merely
    # informs a human/consumer, it never overrides the lossless projection.
    assert result["mutation_outcome"] == "unknown"
    assert result["receipt"]["final_readback"]["status"] == "verified"
    assert result["receipt"]["final_readback"]["digest_class"] == "old"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
