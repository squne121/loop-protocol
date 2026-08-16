"""Behavioral tests for PR #2202 review fix (P0-2/P0-3), Issue #2039.

P0-2: repair_action.apply's provenance-binding fields (source_lane,
preflight_run_identity, original_updated_at, source_refs_digest) were
defined by the schema under `repair_action.*` (and `hashes.result_core_sha256`)
but the consumer read them from non-existent top-level `parsed.*` keys, and
the producer never populated them at all -- so provenance binding was a
no-op on every real production artifact. These tests exercise the REAL
`run_preflight()` producer (never a hand-built preflight-result JSON, per
the review's own root-cause finding) so a canonical schema-shaped artifact
flows straight into `run_repair_action_apply()`.

P0-3: the pre-dispatch drift gate compared ONLY the live body SHA against
`original_body_sha256`, so an A(t1) -> B(t2) -> A(t3) sequence (an "ABA"
attack) would be misclassified as "no drift" once the body cycled back to
byte-identical content, even though the candidate was generated against the
STALE t1 authority epoch, not the current t3 one.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import run_refinement_preflight as wrapper  # noqa: E402

MOCK_PLAN_PASS = {
    "schema_version": "refinement_loop_plan/v1",
    "fail_closed": {"required": False, "reason_codes": []},
    "decisions": {},
}

ISSUE_BODY = """\
## Outcome

Add a new doc.

## Verification Commands

```bash
$ test -f docs/dev/issue-2039-repair-provenance.md
```

## Allowed Paths

- `docs/dev/issue-2039-repair-provenance.md`

## Stop Conditions

- none
"""


def _run_wrapper_preflight(body: str, issue_number: int, tmp_path: Path, *, updated_at: str):
    """Run the REAL wrapper.run_preflight() producer against an in-memory
    fixture (only the planner is mocked -- repair invocation is real, same
    harness pattern as test_repair_diagnostics_routing.py)."""
    fixture = {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": issue_number,
        "repo": "testowner/testrepo",
        "now": "2026-01-01T00:00:00+00:00",
        "issue": {
            "number": issue_number,
            "title": "Test Issue",
            "body": body,
            "labels": [],
            "updatedAt": updated_at,
        },
        "comments": [],
        "anchor_comment_urls": [],
    }
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    with (
        mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
        mock.patch.object(wrapper, "_invoke_planner", return_value=(MOCK_PLAN_PASS, 0, "", "")),
    ):
        result, exit_code = wrapper.run_preflight(
            issue_number=issue_number,
            repo="testowner/testrepo",
            anchor_comment_urls=[],
            fixture_path=fixture_path,
        )
    return result, exit_code


class TestCanonicalProducerToConsumerProvenanceE2E:
    """Required test 1 (producer E2E) + 4 (canonical shape survives) from
    the PR #2202 review: the REAL producer's own schema-shaped artifact,
    fed straight into the REAL consumer, with no hand-built JSON in
    between."""

    def test_producer_provenance_fields_survive_producer_to_consumer_intact(self, tmp_path: Path) -> None:
        result, _exit_code = _run_wrapper_preflight(
            ISSUE_BODY, 209901, tmp_path, updated_at="2026-01-01T00:00:00Z"
        )
        assert result["status"] == "needs_fix", result
        repair_action = result["repair_action"]
        assert repair_action["disposition"] == "auto_apply_safe"

        # P0-2: the producer must actually populate these on a real
        # artifact -- not leave them absent/null.
        assert repair_action["source_lane"] == "unanchored"
        assert repair_action["preflight_run_identity"]
        assert repair_action["preflight_run_identity"].startswith("sha256:")
        assert repair_action["original_updated_at"] == "2026-01-01T00:00:00Z"
        # unanchored lane -> nothing to bind a source-refs digest to.
        assert repair_action["source_refs_digest"] is None

        artifact_path = Path(result["artifacts"]["refinement_preflight_result_v1"])
        assert artifact_path.exists()
        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert on_disk["repair_action"]["preflight_run_identity"] == repair_action["preflight_run_identity"]
        assert on_disk["repair_action"]["original_updated_at"] == "2026-01-01T00:00:00Z"

        candidate_body_text = Path(repair_action["candidate_body_artifact"]).read_text(encoding="utf-8")

        # fetch() is called twice: (1) the pre-dispatch precondition read
        # (must see the ORIGINAL body), (2) AC9's post-dispatch fresh-
        # validation re-read (must see the body AS IT NOW STANDS after the
        # simulated mutation -- i.e. the repaired candidate text).
        fetch_bodies = iter([ISSUE_BODY, candidate_body_text])

        def _fetch():
            return {"body": next(fetch_bodies), "updatedAt": "2026-01-01T00:00:00Z"}

        dispatched = []

        def _apply_transaction(current_issue, candidate_body):
            dispatched.append(candidate_body)
            return {
                "status": "ok",
                "mutation_started": True,
                "body_update": {
                    "attempted": True,
                    "status": "ok",
                    "remote_current_body_sha256": f"sha256:{hashlib.sha256(candidate_body.encode('utf-8')).hexdigest()}",
                },
                "content_update": {"patch_attempted": True, "mutation_outcome": "applied"},
            }

        consumer_result = wrapper.run_repair_action_apply(
            repo="testowner/testrepo",
            issue_number=209901,
            preflight_result_path=str(artifact_path.relative_to(tmp_path)),
            repo_root=tmp_path,
            fetch_current=_fetch,
            apply_transaction=_apply_transaction,
        )

        # P0-2: the consumer must have read the SAME provenance values the
        # producer emitted, not silently null them out by reading the wrong
        # (top-level) location.
        assert consumer_result["provenance"]["source_lane"] == "unanchored"
        assert consumer_result["provenance"]["preflight_run_identity"] == repair_action["preflight_run_identity"]
        assert consumer_result["provenance"]["original_updated_at"] == "2026-01-01T00:00:00Z"
        assert consumer_result["mutation_outcome"] == "applied", consumer_result
        assert dispatched, "expected the candidate body to actually be dispatched"
        assert consumer_result["fresh_validation"]["status"] == "success", consumer_result["fresh_validation"]


class TestABAUpdatedAtProtection:
    """Required test 3 (ABA) from the PR #2202 review: body A(t1) -> B(t2)
    -> A(t3); the t1 candidate must be rejected (drift detected, single
    allowed rerun fired) because updatedAt differs, even though the body
    SHA is byte-identical to A again at t3."""

    def test_stale_candidate_triggers_rerun_on_updated_at_only_drift(self, tmp_path: Path) -> None:
        # Producer run at t1, against body A.
        result, _exit_code = _run_wrapper_preflight(
            ISSUE_BODY, 209902, tmp_path, updated_at="2026-01-01T00:00:00Z"
        )
        assert result["status"] == "needs_fix", result
        artifact_path = Path(result["artifacts"]["refinement_preflight_result_v1"])

        # Live Issue is now at t3: the body has cycled A -> B -> A (the live
        # body is byte-identical to the t1 candidate's original_body_sha256
        # again), but updatedAt has advanced past t1.
        def _fetch():
            return {"body": ISSUE_BODY, "updatedAt": "2026-01-01T00:00:02Z"}

        dispatched = []

        def _apply_transaction(current_issue, candidate_body):
            dispatched.append(candidate_body)
            return {
                "status": "ok",
                "mutation_started": True,
                "body_update": {"attempted": True, "status": "ok"},
                "content_update": {"patch_attempted": True, "mutation_outcome": "applied"},
            }

        consumer_result = wrapper.run_repair_action_apply(
            repo="testowner/testrepo",
            issue_number=209902,
            preflight_result_path=str(artifact_path.relative_to(tmp_path)),
            repo_root=tmp_path,
            fetch_current=_fetch,
            apply_transaction=_apply_transaction,
        )

        # A body-SHA-only comparison would have seen "no drift" (A == A) and
        # dispatched the STALE t1 candidate directly and unconditionally.
        # With the updatedAt-aware fix, drift IS detected and the single
        # allowed producer rerun fires instead.
        assert consumer_result["rebase"]["drift_detected"] is True, consumer_result
        assert consumer_result["rebase"]["attempted"] is True
        assert consumer_result["rebase"]["producer_reruns"] == 1
        # The rebound provenance must reflect the NEW (t3) updatedAt, not
        # the stale t1 value the candidate was originally generated against.
        assert consumer_result["provenance"]["original_updated_at"] == "2026-01-01T00:00:02Z"
        # source_lane must stay pinned across the rebind (same human-context
        # / anchor / unanchored lane -- only body/candidate regenerated).
        assert consumer_result["provenance"]["source_lane"] == "unanchored"

    def test_pure_body_drift_still_detected_without_updated_at_change(self, tmp_path: Path) -> None:
        """Regression guard: the updatedAt-aware fix must not weaken the
        pre-existing body-SHA drift detection when updatedAt happens to be
        identical (e.g. a caller that never threads updatedAt through)."""
        result, _exit_code = _run_wrapper_preflight(
            ISSUE_BODY, 209903, tmp_path, updated_at="2026-01-01T00:00:00Z"
        )
        assert result["status"] == "needs_fix", result
        artifact_path = Path(result["artifacts"]["refinement_preflight_result_v1"])

        drifted_body = ISSUE_BODY + "\nExtra unrelated line.\n"

        def _fetch():
            return {"body": drifted_body, "updatedAt": "2026-01-01T00:00:00Z"}

        consumer_result = wrapper.run_repair_action_apply(
            repo="testowner/testrepo",
            issue_number=209903,
            preflight_result_path=str(artifact_path.relative_to(tmp_path)),
            repo_root=tmp_path,
            fetch_current=_fetch,
            apply_transaction=lambda current_issue, candidate_body: {
                "status": "ok",
                "mutation_started": True,
                "body_update": {"attempted": True, "status": "ok"},
                "content_update": {"patch_attempted": True, "mutation_outcome": "applied"},
            },
        )

        assert consumer_result["rebase"]["drift_detected"] is True
