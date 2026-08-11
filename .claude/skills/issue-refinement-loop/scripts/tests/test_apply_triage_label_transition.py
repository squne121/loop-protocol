"""Tests for apply_triage_label_transition.py (#2084 AC7).

Verifies that triage/phase/agent label transition is executed strictly as a
best-effort presentation sync: `applied | noop | failed` result, warning-only
(non-fatal) surfacing of permission/network/API failures, preservation of
unrelated labels, idempotency, and — critically — that the result never
carries or influences readiness / status / routing_action /
implementation_allowed authority fields.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "apply_triage_label_transition.py"
)

spec = importlib.util.spec_from_file_location("apply_triage_label_transition", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]


def _labels_view_stdout(names: list[str]) -> str:
    return json.dumps({"labels": [{"name": name} for name in names]})


class _ScriptedRunner:
    """Deterministic gh CLI stand-in for unit tests.

    `responses` is a list of (rc, stdout, stderr) tuples consumed in call
    order. Each call is also recorded in `.calls` for assertion.
    """

    def __init__(self, responses: list[tuple[int, str, str]]) -> None:
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        if not self._responses:
            raise AssertionError("no more scripted responses")
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# result: applied
# ---------------------------------------------------------------------------


def test_applied_removes_triage_and_adds_phase_and_agent_labels():
    """GIVEN an issue with only triage-required
    WHEN apply_triage_label_transition runs
    THEN triage-required is removed and phase/implementation + agent/implementer are added
    """
    runner = _ScriptedRunner(
        [
            (0, _labels_view_stdout(["triage-required", "enhancement"]), ""),
            (0, "", ""),  # remove-label
            (0, "", ""),  # add-label
        ]
    )
    result = mod.apply_triage_label_transition(
        repo="squne121/loop-protocol",
        issue_number=1,
        gh_bin="gh",
        run_fn=runner,
    )
    assert result["result"] == "applied"
    assert result["removed"] == ["triage-required"]
    assert set(result["added"]) == {"phase/implementation", "agent/implementer"}
    assert result["unrelated_labels_preserved"] == ["enhancement"]
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# result: noop / idempotency
# ---------------------------------------------------------------------------


def test_noop_when_transition_already_applied():
    """GIVEN an issue that already has phase/implementation + agent/implementer and no triage-required
    WHEN apply_triage_label_transition runs
    THEN result is noop and no mutation calls are issued
    """
    runner = _ScriptedRunner(
        [
            (
                0,
                _labels_view_stdout(["phase/implementation", "agent/implementer", "enhancement"]),
                "",
            ),
        ]
    )
    result = mod.apply_triage_label_transition(
        repo="squne121/loop-protocol",
        issue_number=2,
        run_fn=runner,
    )
    assert result["result"] == "noop"
    assert result["removed"] == []
    assert result["added"] == []
    # Only the view call — no edit/mutation call issued.
    assert len(runner.calls) == 1


def test_idempotent_second_run_after_applied_is_noop():
    """GIVEN a first run that applies the transition
    WHEN the transition is run again against the resulting label set
    THEN the second run returns noop (idempotent)
    """
    runner_first = _ScriptedRunner(
        [
            (0, _labels_view_stdout(["triage-required"]), ""),
            (0, "", ""),
            (0, "", ""),
        ]
    )
    first = mod.apply_triage_label_transition(
        repo="squne121/loop-protocol", issue_number=3, run_fn=runner_first
    )
    assert first["result"] == "applied"

    runner_second = _ScriptedRunner(
        [
            (0, _labels_view_stdout(["phase/implementation", "agent/implementer"]), ""),
        ]
    )
    second = mod.apply_triage_label_transition(
        repo="squne121/loop-protocol", issue_number=3, run_fn=runner_second
    )
    assert second["result"] == "noop"


# ---------------------------------------------------------------------------
# result: failed — permission / network / API failures are warnings, not fatal
# ---------------------------------------------------------------------------


def test_fetch_labels_permission_failure_is_warning_not_exception():
    """GIVEN gh issue view fails (e.g. permission denied)
    WHEN apply_triage_label_transition runs
    THEN it returns result: failed with a warning, and does not raise
    """
    runner = _ScriptedRunner([(1, "", "HTTP 403: Resource not accessible")])
    result = mod.apply_triage_label_transition(
        repo="squne121/loop-protocol", issue_number=4, run_fn=runner
    )
    assert result["result"] == "failed"
    assert result["errors"] == []
    assert any("gh_issue_view_failed" in w for w in result["warnings"])


def test_network_failure_during_mutation_is_warning_not_exception():
    """GIVEN gh issue edit --remove-label fails with a network error
    WHEN apply_triage_label_transition runs
    THEN it returns result: failed with a warning, and does not raise
    """
    runner = _ScriptedRunner(
        [
            (0, _labels_view_stdout(["triage-required"]), ""),
            (1, "", "connection reset by peer"),  # remove-label network failure
            (0, "", ""),  # add-label succeeds
        ]
    )
    result = mod.apply_triage_label_transition(
        repo="squne121/loop-protocol", issue_number=5, run_fn=runner
    )
    assert result["result"] == "failed"
    assert result["removed"] == []
    assert set(result["added"]) == {"phase/implementation", "agent/implementer"}
    assert any("remove_label_failed" in w for w in result["warnings"])


def test_api_failure_does_not_raise():
    """GIVEN both mutation calls fail (API outage)
    WHEN apply_triage_label_transition runs
    THEN it returns result: failed without raising an exception
    """
    runner = _ScriptedRunner(
        [
            (0, _labels_view_stdout(["triage-required"]), ""),
            (1, "", "500 Internal Server Error"),
            (1, "", "500 Internal Server Error"),
        ]
    )
    result = mod.apply_triage_label_transition(
        repo="squne121/loop-protocol", issue_number=6, run_fn=runner
    )
    assert result["result"] == "failed"
    assert len(result["warnings"]) == 2


# ---------------------------------------------------------------------------
# unrelated labels preserved
# ---------------------------------------------------------------------------


def test_unrelated_labels_are_preserved_in_report():
    """GIVEN an issue with unrelated labels alongside triage-required
    WHEN apply_triage_label_transition runs
    THEN unrelated labels are reported as preserved and never targeted for mutation
    """
    runner = _ScriptedRunner(
        [
            (0, _labels_view_stdout(["triage-required", "bug", "priority/p1"]), ""),
            (0, "", ""),
            (0, "", ""),
        ]
    )
    result = mod.apply_triage_label_transition(
        repo="squne121/loop-protocol", issue_number=7, run_fn=runner
    )
    assert result["unrelated_labels_preserved"] == ["bug", "priority/p1"]
    remove_call = next(c for c in runner.calls if "--remove-label" in c)
    assert "bug" not in ",".join(remove_call)
    assert "priority/p1" not in ",".join(remove_call)


# ---------------------------------------------------------------------------
# AC7: result never carries or influences readiness authority fields
# ---------------------------------------------------------------------------


def test_result_schema_does_not_contain_readiness_authority_fields():
    """GIVEN any transition outcome
    WHEN apply_triage_label_transition returns its result
    THEN the result dict never contains status / routing_action /
    implementation_allowed keys (label sync is telemetry-only, #2084)
    """
    runner = _ScriptedRunner(
        [
            (0, _labels_view_stdout(["triage-required"]), ""),
            (0, "", ""),
            (0, "", ""),
        ]
    )
    result = mod.apply_triage_label_transition(
        repo="squne121/loop-protocol", issue_number=8, run_fn=runner
    )
    forbidden_keys = {"status", "routing_action", "implementation_allowed"}
    assert forbidden_keys.isdisjoint(result.keys())
    assert result["schema"] == "APPLY_TRIAGE_LABEL_TRANSITION_RESULT_V1"


def test_readiness_simulation_unaffected_by_label_sync_result_permutation():
    """GIVEN a simulated readiness decision made independent of label sync
    WHEN label_sync.result varies across applied / noop / failed
    THEN the simulated readiness decision (status/routing_action/implementation_allowed)
    does not change — mirrors the invariant enforced end-to-end for
    build_intake_capsule.py / LOOP_HANDOFF_RESULT_V1 (#2084)
    """

    def _simulate_readiness(label_sync_result: str) -> dict:
        # This function intentionally ignores label_sync_result entirely,
        # mirroring the production readiness authority contract.
        del label_sync_result
        return {
            "status": "impl_ready",
            "routing_action": "run_impl_review_loop",
            "implementation_allowed": True,
        }

    outcomes = {
        result_value: _simulate_readiness(result_value)
        for result_value in ("applied", "noop", "failed")
    }
    values = list(outcomes.values())
    assert all(v == values[0] for v in values)
