"""Behavioral tests for `resolve_repair_apply_mutation_intent` (Issue #2039 AC1).

GIVEN a preflight result carrying `contract_update` and/or
`repair_action`, WHEN the arbiter resolves the mutation intent, THEN it
must select exactly one intent, and must fail closed (no GitHub mutation
attempted) when both or neither are present.

NOTE: this is a partial-implementation Issue #2039 test file covering AC1
(arbiter decision logic) only. It does not exercise the full command
registry / policy / executor wiring, nor AC2-AC11 (not yet implemented).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from run_refinement_preflight import (  # noqa: E402
    REPAIR_APPLY_FAILURE_MULTIPLE_MUTATION_INTENTS,
    REPAIR_APPLY_FAILURE_NO_MUTATION_INTENT,
    resolve_repair_apply_mutation_intent,
)


class ResolveRepairApplyMutationIntentTests(unittest.TestCase):
    # -- GIVEN both intents present WHEN resolved THEN fail closed --

    def test_given_both_intents_present_when_resolved_then_multiple_mutation_intents(
        self,
    ) -> None:
        verdict = resolve_repair_apply_mutation_intent(
            contract_update={"some": "patch"},
            repair_action={"disposition": "auto_apply_safe"},
        )

        self.assertFalse(verdict["ok"])
        self.assertIsNone(verdict["intent"])
        self.assertEqual(
            verdict["failure_code"], REPAIR_APPLY_FAILURE_MULTIPLE_MUTATION_INTENTS
        )
        self.assertEqual(verdict["mutation_outcome"], "not_attempted")
        self.assertIsNotNone(verdict["reason"])

    # -- GIVEN only repair_action present WHEN resolved THEN intent=repair_action --

    def test_given_only_repair_action_when_resolved_then_repair_action_intent(
        self,
    ) -> None:
        verdict = resolve_repair_apply_mutation_intent(
            contract_update=None,
            repair_action={"disposition": "auto_apply_safe"},
        )

        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["intent"], "repair_action")
        self.assertIsNone(verdict["failure_code"])
        self.assertIsNone(verdict["mutation_outcome"])

    # -- GIVEN only contract_update present WHEN resolved THEN intent=contract_update --

    def test_given_only_contract_update_when_resolved_then_contract_update_intent(
        self,
    ) -> None:
        verdict = resolve_repair_apply_mutation_intent(
            contract_update={"some": "patch"},
            repair_action=None,
        )

        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["intent"], "contract_update")
        self.assertIsNone(verdict["failure_code"])
        self.assertIsNone(verdict["mutation_outcome"])

    # -- GIVEN neither intent present WHEN resolved THEN fail closed, no mutation --

    def test_given_neither_intent_present_when_resolved_then_no_mutation_intent(
        self,
    ) -> None:
        verdict = resolve_repair_apply_mutation_intent(
            contract_update=None,
            repair_action=None,
        )

        self.assertFalse(verdict["ok"])
        self.assertIsNone(verdict["intent"])
        self.assertEqual(verdict["failure_code"], REPAIR_APPLY_FAILURE_NO_MUTATION_INTENT)
        self.assertEqual(verdict["mutation_outcome"], "not_attempted")

    # -- GIVEN empty-dict repair_action (falsy but not None) WHEN resolved THEN still selected --

    def test_given_empty_dict_repair_action_when_resolved_then_still_selected_as_intent(
        self,
    ) -> None:
        # An empty dict is falsy but is-not-None: presence must be judged by
        # `is not None`, not truthiness, so a technically-empty-but-present
        # repair_action payload is not silently treated as absent.
        verdict = resolve_repair_apply_mutation_intent(
            contract_update=None,
            repair_action={},
        )

        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["intent"], "repair_action")

    def test_given_both_empty_dicts_when_resolved_then_multiple_mutation_intents(
        self,
    ) -> None:
        verdict = resolve_repair_apply_mutation_intent(
            contract_update={},
            repair_action={},
        )

        self.assertFalse(verdict["ok"])
        self.assertEqual(
            verdict["failure_code"], REPAIR_APPLY_FAILURE_MULTIPLE_MUTATION_INTENTS
        )


class RealCanonicalDualIntentProductionShapeTests(unittest.TestCase):
    """PR #2202 human adversarial review, 'マージ前に必須の追加テスト' item 2
    ('dual-intent production shape'): a canonical, schema-valid preflight
    result that carries BOTH `contract_update` and `repair_action` must be
    rejected by `run_repair_action_apply()` BEFORE any GitHub read --
    exercised via a REAL `repair_action` produced by the actual
    `run_preflight()` producer (never a hand-built dict), with a
    schema-valid `contract_update` block spliced onto the SAME real
    artifact (the genuine dual-intent hazard: an artifact that already
    carries a completed/attempted `contract_update` block ALSO carrying a
    `repair_action`)."""

    ISSUE_BODY = """\
## Outcome

Add a new doc.

## Verification Commands

```bash
$ test -f docs/dev/issue-2039-dual-intent.md
```

## Allowed Paths

- `docs/dev/issue-2039-dual-intent.md`

## Stop Conditions

- none
"""

    def test_real_producer_repair_action_with_contract_update_spliced_in_is_rejected_before_github_read(
        self,
    ) -> None:
        import json
        from unittest import mock

        import run_refinement_preflight as wrapper

        tmp_path = Path(self._make_tmp_dir())
        issue_number = 209904

        fixture = {
            "schema_version": "refinement_preflight_input/v1",
            "issue_number": issue_number,
            "repo": "testowner/testrepo",
            "now": "2026-01-01T00:00:00+00:00",
            "issue": {
                "number": issue_number,
                "title": "Test Issue",
                "body": self.ISSUE_BODY,
                "labels": [],
                "updatedAt": "2026-01-01T00:00:00Z",
            },
            "comments": [],
            "anchor_comment_urls": [],
        }
        fixture_path = tmp_path / "fixture.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        mock_plan_pass = {
            "schema_version": "refinement_loop_plan/v1",
            "fail_closed": {"required": False, "reason_codes": []},
            "decisions": {},
        }

        with (
            mock.patch.object(wrapper, "_find_repo_root", return_value=tmp_path),
            mock.patch.object(wrapper, "_invoke_planner", return_value=(mock_plan_pass, 0, "", "")),
        ):
            result, exit_code = wrapper.run_preflight(
                issue_number=issue_number,
                repo="testowner/testrepo",
                anchor_comment_urls=[],
                fixture_path=fixture_path,
            )

        self.assertEqual(result["status"], "needs_fix", result)
        self.assertIn("repair_action", result)
        self.assertIsNotNone(result["repair_action"])

        # Splice a schema-valid contract_update block onto the SAME real,
        # producer-generated artifact -- this is the genuine dual-intent
        # hazard the review flagged: an artifact that already carries a
        # completed contract_update ALSO carrying a repair_action, which
        # `additionalProperties: false` alone does not prevent (both keys
        # are independently valid top-level properties of
        # refinement_preflight_result_v1).
        artifact_path = Path(result["artifacts"]["refinement_preflight_result_v1"])
        on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertIn("repair_action", on_disk)
        on_disk["contract_update"] = {
            "status": "applied",
            "writes": 1,
            "iterations": 1,
            "final_readback": "verified",
            "fresh_preflight": "pass",
            "fresh_review": "pass",
            "fresh_readiness": "pass",
        }
        artifact_path.write_text(json.dumps(on_disk), encoding="utf-8")

        github_read_calls: list[None] = []

        def _fetch_should_not_be_called():
            github_read_calls.append(None)
            return {"body": self.ISSUE_BODY, "updatedAt": "2026-01-01T00:00:00Z"}

        consumer_result = wrapper.run_repair_action_apply(
            repo="testowner/testrepo",
            issue_number=issue_number,
            preflight_result_path=str(artifact_path.relative_to(tmp_path)),
            repo_root=tmp_path,
            fetch_current=_fetch_should_not_be_called,
        )

        self.assertEqual(consumer_result["failure_code"], REPAIR_APPLY_FAILURE_MULTIPLE_MUTATION_INTENTS)
        self.assertEqual(consumer_result["mutation_outcome"], "not_attempted")
        self.assertEqual(
            github_read_calls, [], "dual-intent artifact must be rejected before any GitHub read"
        )

    def _make_tmp_dir(self) -> str:
        import shutil
        import tempfile

        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d


if __name__ == "__main__":
    unittest.main()
