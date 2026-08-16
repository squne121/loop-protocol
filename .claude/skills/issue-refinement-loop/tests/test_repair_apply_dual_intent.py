"""Behavioral tests for `resolve_repair_apply_mutation_intent` (Issue #2039 AC1).

GIVEN a preflight result carrying `contract_patch_plan` and/or
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
            contract_patch_plan={"some": "patch"},
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
            contract_patch_plan=None,
            repair_action={"disposition": "auto_apply_safe"},
        )

        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["intent"], "repair_action")
        self.assertIsNone(verdict["failure_code"])
        self.assertIsNone(verdict["mutation_outcome"])

    # -- GIVEN only contract_patch_plan present WHEN resolved THEN intent=contract_patch_plan --

    def test_given_only_contract_patch_plan_when_resolved_then_contract_patch_plan_intent(
        self,
    ) -> None:
        verdict = resolve_repair_apply_mutation_intent(
            contract_patch_plan={"some": "patch"},
            repair_action=None,
        )

        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["intent"], "contract_patch_plan")
        self.assertIsNone(verdict["failure_code"])
        self.assertIsNone(verdict["mutation_outcome"])

    # -- GIVEN neither intent present WHEN resolved THEN fail closed, no mutation --

    def test_given_neither_intent_present_when_resolved_then_no_mutation_intent(
        self,
    ) -> None:
        verdict = resolve_repair_apply_mutation_intent(
            contract_patch_plan=None,
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
            contract_patch_plan=None,
            repair_action={},
        )

        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["intent"], "repair_action")

    def test_given_both_empty_dicts_when_resolved_then_multiple_mutation_intents(
        self,
    ) -> None:
        verdict = resolve_repair_apply_mutation_intent(
            contract_patch_plan={},
            repair_action={},
        )

        self.assertFalse(verdict["ok"])
        self.assertEqual(
            verdict["failure_code"], REPAIR_APPLY_FAILURE_MULTIPLE_MUTATION_INTENTS
        )


if __name__ == "__main__":
    unittest.main()
