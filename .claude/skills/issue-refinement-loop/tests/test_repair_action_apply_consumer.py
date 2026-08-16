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
        # PR #2202 review fix-delta (P1-3): these live under repair_action.*
        # in the canonical refinement_preflight_result_v1 schema (P0-2),
        # never top-level. This fixture previously left them at the
        # top-level `preflight_result` dict below, which the P0-2 fix never
        # reads from -- silently exercising the consumer's now-closed
        # null-provenance fail-closed path (P1-3) instead of a genuine
        # dispatch. Moving them here matches
        # test_repair_apply_fresh_validation.py / test_repair_apply_provenance.py.
        "source_lane": source_lane,
        "preflight_run_identity": "sha256:testrun",
        "original_updated_at": "2024-01-01T00:00:00Z",
        "source_refs_digest": None,
    }
    preflight_result = {
        "schema": "issue_refinement_preflight_result/v1",
        "repair_action": repair_action,
        "result_core_sha256": "sha256:testrun",
    }
    result_path = artifact_dir / "preflight_result.json"
    result_path.write_text(json.dumps(preflight_result))
    return result_path


def _fetch_stub(body: str = ORIGINAL_BODY):
    def _fetch():
        return {"body": body, "updatedAt": "2024-01-01T00:00:00Z"}

    return _fetch


def _fetch_sequence_stub(bodies: list[str]):
    """PR #2202 review fix-delta (P0-5): `fetch_current` is invoked at both
    the pre-dispatch precondition read AND (when a patch was actually
    attempted) the AC9 post-dispatch fresh-validation reread. A fixed
    single-body `_fetch_stub()` was stale by the time AC9 wiring made
    fresh_validation failures affect `phase`/`failure_code` -- this returns
    each body in sequence, one per call, so a happy-path test can genuinely
    reflect the live body actually changing after a real dispatch."""
    it = iter(bodies)

    def _fetch():
        return {"body": next(it), "updatedAt": "2024-01-01T00:00:00Z"}

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
    # Issue #2039 P0-4: canonical ISSUE_EDIT_TXN_RESULT_V1 shape -- attempted
    # / remote-digest fields live nested under body_update/content_update,
    # not at the top level (only `status`/`mutation_started`/`errors` are
    # top-level). This is the real `edit_issue_txn.py` `_render_result()`
    # shape, not an invented flat one.
    apply_txn = CallCountingApplyTransaction(
        {
            "status": "ok",
            "mutation_started": True,
            "body_update": {
                "attempted": True,
                "status": "ok",
                "remote_current_body_sha256": f"sha256:{_hex(REPAIRED_BODY)}",
            },
            "content_update": {
                "patch_attempted": True,
                "mutation_outcome": "applied",
            },
            "errors": [],
        }
    )

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        # PR #2202 review fix-delta (P0-5): patch_attempted is True here, so
        # AC9 fresh validation now reruns `fetch_current` a second time to
        # re-read the live body post-dispatch. A realistic post-mutation
        # state has the live body actually equal to REPAIRED_BODY by then
        # (the mutation genuinely applied) -- a fixed single-body stub was
        # stale and would now (correctly) surface as a fresh-validation
        # digest-mismatch failure.
        fetch_current=_fetch_sequence_stub([ORIGINAL_BODY, REPAIRED_BODY]),
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
    # PR #2202 review fix-delta (P0-5): a genuinely successful mutation
    # followed by a genuinely successful fresh validation must still land
    # on phase=complete/failure_code=null (the override only fires on
    # fresh_validation failure).
    assert result["fresh_validation"]["status"] == "success"


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

    # Issue #2039 P0-4: canonical nested ISSUE_EDIT_TXN_RESULT_V1 shape (see
    # comment above in test_no_drift_happy_path_dispatches_and_is_schema_conformant).
    apply_txn = CallCountingApplyTransaction(
        {
            "status": "ok",
            "mutation_started": True,
            "body_update": {
                "attempted": True,
                "status": "ok",
                "remote_current_body_sha256": f"sha256:{_hex(rebased_candidate_body)}",
            },
            "content_update": {
                "patch_attempted": True,
                "mutation_outcome": "applied",
            },
        }
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


# ---------------------------------------------------------------------------
# PR #2202 human adversarial review, 'マージ前に必須の追加テスト' item 4
# ('canonical receipt matrix'): the SAME statuses covered by
# test_receipt_projection_is_lossless_across_statuses above, but built via
# the REAL edit_issue_txn.py `_render_result()` function (imported read-only
# for test fidelity; edit_issue_txn.py itself is not modified) instead of a
# hand-rolled `{"status": ..., "errors": []}` dict, so the receipt adapter is
# exercised against the actual canonical ISSUE_EDIT_TXN_RESULT_V1 nested
# shape (body_update/content_update/comment_publish/native_relationships),
# not merely a shape someone believes it has.
# ---------------------------------------------------------------------------

_EDIT_ISSUE_TXN_SCRIPTS_DIR = _SKILL_ROOT.parent / "edit-issue" / "scripts"
if str(_EDIT_ISSUE_TXN_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_EDIT_ISSUE_TXN_SCRIPTS_DIR))

import edit_issue_txn  # noqa: E402


def _render_no_change_result() -> dict:
    return edit_issue_txn._render_result(
        status="no_change",
        issue_number=2039,
        repo="squne121/loop-protocol",
        mutation_started=False,
        body_attempted=True,
        body_status="no_change",
        comment_attempted=False,
        comment_status="not_run",
        comment_id=None,
        comment_url=None,
        comment_body_sha256=None,
        previous_body_sha256=_hex(ORIGINAL_BODY),
        requested_new_body_sha256=_hex(ORIGINAL_BODY),
        remote_current_body_sha256=_hex(ORIGINAL_BODY),
        body_input_ref=None,
        comment_input_ref=None,
        errors=[],
        patch_attempted=True,
        mutation_outcome="no_change",
    )


def _render_applied_result() -> dict:
    return edit_issue_txn._render_result(
        status="ok",
        issue_number=2039,
        repo="squne121/loop-protocol",
        mutation_started=True,
        body_attempted=True,
        body_status="ok",
        comment_attempted=False,
        comment_status="not_run",
        comment_id=None,
        comment_url=None,
        comment_body_sha256=None,
        previous_body_sha256=_hex(ORIGINAL_BODY),
        requested_new_body_sha256=_hex(REPAIRED_BODY),
        remote_current_body_sha256=_hex(REPAIRED_BODY),
        body_input_ref=None,
        comment_input_ref=None,
        errors=[],
        patch_attempted=True,
        mutation_outcome="applied",
    )


def _render_mutation_outcome_unknown_result() -> dict:
    return edit_issue_txn._render_result(
        status="mutation_outcome_unknown",
        issue_number=2039,
        repo="squne121/loop-protocol",
        mutation_started=True,
        body_attempted=True,
        body_status="unknown",
        comment_attempted=False,
        comment_status="not_run",
        comment_id=None,
        comment_url=None,
        comment_body_sha256=None,
        previous_body_sha256=_hex(ORIGINAL_BODY),
        requested_new_body_sha256=_hex(REPAIRED_BODY),
        remote_current_body_sha256=None,
        body_input_ref=None,
        comment_input_ref=None,
        errors=[{"code": "readback_timeout", "message": "could not confirm outcome"}],
        patch_attempted=True,
        mutation_outcome="unknown",
    )


def _render_failed_after_mutation_result() -> dict:
    # `_render_result()` itself promotes failed_no_mutation ->
    # failed_after_mutation whenever mutation_started is True (see its own
    # P0-1 bullet 5 fail-closed logic) -- passing status="failed_no_mutation"
    # here with mutation_started=True exercises that REAL promotion path,
    # rather than hand-asserting the post-promotion status string.
    return edit_issue_txn._render_result(
        status="failed_no_mutation",
        issue_number=2039,
        repo="squne121/loop-protocol",
        mutation_started=True,
        body_attempted=True,
        body_status="error",
        comment_attempted=False,
        comment_status="not_run",
        comment_id=None,
        comment_url=None,
        comment_body_sha256=None,
        previous_body_sha256=_hex(ORIGINAL_BODY),
        requested_new_body_sha256=_hex(REPAIRED_BODY),
        remote_current_body_sha256=None,
        body_input_ref=None,
        comment_input_ref=None,
        errors=[{"code": "unexpected_post_mutation_error", "message": "boom"}],
        patch_attempted=True,
        mutation_outcome="unknown",
    )


@pytest.mark.parametrize(
    ("render_fn", "expected_outcome", "post_dispatch_bodies"),
    [
        # no_change: nothing was mutated, so a fresh post-dispatch read
        # genuinely still sees the ORIGINAL body both times.
        (_render_no_change_result, "no_change", [ORIGINAL_BODY, ORIGINAL_BODY]),
        # applied: the mutation genuinely happened, so a fresh post-dispatch
        # read sees the REPAIRED body the second time.
        (_render_applied_result, "applied", [ORIGINAL_BODY, REPAIRED_BODY]),
        # unknown outcomes never reach fresh validation's live-body
        # comparison the same way (patch_attempted is still True here, so
        # fresh validation DOES run -- feed it the repaired body as the
        # plausible post-mutation state; the receipt-matrix assertion below
        # only checks mutation_outcome/phase/failure_code, not fresh
        # validation's own status).
        (_render_mutation_outcome_unknown_result, "unknown", [ORIGINAL_BODY, REPAIRED_BODY]),
        (_render_failed_after_mutation_result, "unknown", [ORIGINAL_BODY, REPAIRED_BODY]),
    ],
)
def test_canonical_render_result_receipt_matrix_projects_losslessly(
    tmp_path: Path, render_fn, expected_outcome: str, post_dispatch_bodies: list[str]
) -> None:
    """Required test 4 (canonical receipt matrix): each REAL
    edit_issue_txn.py `_render_result()` shape (no_change, applied/ok,
    mutation_outcome_unknown, failed_after_mutation-via-promotion) projects
    to its own correct `mutation_outcome` through the actual
    `_repair_receipt_from_txn_result()` adapter -- exercised end to end via
    `run_repair_action_apply()`, not a unit-level adapter call."""
    result_path = _write_candidate(tmp_path)
    txn_result = render_fn()
    assert txn_result["schema"] == edit_issue_txn.RESULT_SCHEMA, (
        "sanity check: this must be the REAL canonical schema tag, proving "
        "_render_result() actually ran"
    )

    apply_txn = CallCountingApplyTransaction(txn_result)

    result = rrp.run_repair_action_apply(
        repo="squne121/loop-protocol",
        issue_number=2039,
        preflight_result_path=str(result_path.relative_to(tmp_path)),
        repo_root=tmp_path,
        fetch_current=_fetch_sequence_stub(post_dispatch_bodies),
        apply_transaction=apply_txn,
    )

    jsonschema.validate(result, _SCHEMA)
    assert result["mutation_outcome"] == expected_outcome
    assert result["receipt"]["mutation_outcome"] == expected_outcome
    if expected_outcome == "unknown":
        assert result["phase"] == "final_readback"
        assert result["failure_code"] == "final_readback_unresolvable"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
