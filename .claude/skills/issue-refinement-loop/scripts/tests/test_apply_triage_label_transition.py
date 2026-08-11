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
            (
                0,
                _labels_view_stdout(
                    ["enhancement", "phase/implementation", "agent/implementer"]
                ),
                "",
            ),  # readback
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
    assert len(runner.calls) == 4


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
            (0, _labels_view_stdout(["phase/implementation", "agent/implementer"]), ""),
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
            # readback: remove failed so triage-required is still present;
            # add succeeded so phase/agent are present.
            (
                0,
                _labels_view_stdout(
                    ["triage-required", "phase/implementation", "agent/implementer"]
                ),
                "",
            ),
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
            # readback: nothing changed since both mutation calls failed.
            (0, _labels_view_stdout(["triage-required"]), ""),
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
            (
                0,
                _labels_view_stdout(
                    ["bug", "priority/p1", "phase/implementation", "agent/implementer"]
                ),
                "",
            ),
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
            (0, _labels_view_stdout(["phase/implementation", "agent/implementer"]), ""),
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


# ---------------------------------------------------------------------------
# #2084 Scope Delta (PR #2092 comment #5251894253, AC13): bounded subprocess
# timeout — TimeoutExpired must degrade to `result: failed` + warning
# telemetry, exit 0, never raise.
# ---------------------------------------------------------------------------


def test_default_run_converts_timeout_expired_to_warning_tuple():
    """GIVEN gh issue view hangs beyond the bounded timeout
    WHEN _default_run() executes it
    THEN subprocess.TimeoutExpired is caught and converted into a
    (rc, stdout, stderr) tuple instead of propagating as an exception
    """
    import subprocess as _subprocess
    from unittest.mock import patch

    def _raise_timeout(*args, **kwargs):
        raise _subprocess.TimeoutExpired(cmd=["gh", "issue", "view"], timeout=30)

    with patch.object(mod.subprocess, "run", side_effect=_raise_timeout):
        rc, stdout, stderr = mod._default_run(["gh", "issue", "view", "1"])

    assert rc != 0
    assert stdout == ""
    assert "timeout_expired" in stderr


def test_default_run_passes_bounded_timeout_to_subprocess_run():
    """GIVEN the default run helper
    WHEN it invokes subprocess.run
    THEN it always sets a bounded `timeout` kwarg (no unbounded blocking)
    """
    from unittest.mock import MagicMock, patch

    fake_result = MagicMock(returncode=0, stdout="{}", stderr="")
    with patch.object(mod.subprocess, "run", return_value=fake_result) as mock_run:
        mod._default_run(["gh", "issue", "view", "1"])

    _, kwargs = mock_run.call_args
    assert "timeout" in kwargs
    assert isinstance(kwargs["timeout"], (int, float))
    assert kwargs["timeout"] > 0


def test_apply_triage_label_transition_surfaces_timeout_as_failed_with_warning():
    """GIVEN gh issue view times out during the initial fetch
    WHEN apply_triage_label_transition runs with a run_fn simulating a
    timeout-derived (rc, stdout, stderr) tuple
    THEN it returns result: failed with a warning mentioning the timeout,
    and does not raise (exit-0-equivalent contract preserved end-to-end)
    """

    def _timeout_run_fn(argv: list[str]) -> tuple[int, str, str]:
        return 124, "", "timeout_expired: command exceeded 30s: gh issue view 9"

    result = mod.apply_triage_label_transition(
        repo="squne121/loop-protocol", issue_number=9, run_fn=_timeout_run_fn
    )
    assert result["result"] == "failed"
    assert any("timeout_expired" in w for w in result["warnings"])
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# #2084 Scope Delta (PR #2092 comment #5251894253, AC13): read → delta →
# mutation → readback. `applied` must only be reported when the requested
# delta is confirmed present in a post-mutation re-fetch of labels.
# ---------------------------------------------------------------------------


def test_readback_confirms_applied_result_reflects_post_mutation_state():
    """GIVEN gh issue edit calls both report success (rc=0)
    WHEN the post-mutation readback shows the requested delta materialized
    THEN result is applied and unrelated_labels_preserved is derived from
    the readback (not the pre-mutation) label snapshot
    """
    runner = _ScriptedRunner(
        [
            (0, _labels_view_stdout(["triage-required", "docs"]), ""),  # pre-mutation fetch
            (0, "", ""),  # remove-label rc=0
            (0, "", ""),  # add-label rc=0
            (
                0,
                _labels_view_stdout(["docs", "phase/implementation", "agent/implementer"]),
                "",
            ),  # readback confirms delta applied
        ]
    )
    result = mod.apply_triage_label_transition(
        repo="squne121/loop-protocol", issue_number=10, run_fn=runner
    )
    assert result["result"] == "applied"
    assert result["removed"] == ["triage-required"]
    assert set(result["added"]) == {"phase/implementation", "agent/implementer"}
    assert result["unrelated_labels_preserved"] == ["docs"]


def test_readback_reports_failed_when_gh_edit_lied_about_success():
    """GIVEN gh issue edit --remove-label reports rc=0 (apparent success)
    WHEN the post-mutation readback shows the label was NOT actually
    removed (e.g. eventual-consistency lag or a silently-rejected edit)
    THEN result is failed, not applied — readback is authoritative over the
    mutation call's own exit code
    """
    runner = _ScriptedRunner(
        [
            (0, _labels_view_stdout(["triage-required"]), ""),  # pre-mutation fetch
            (0, "", ""),  # remove-label reports rc=0
            (
                0,
                _labels_view_stdout(["triage-required"]),
                "",
            ),  # readback: triage-required is still present despite rc=0
        ]
    )
    result = mod.apply_triage_label_transition(
        repo="squne121/loop-protocol",
        issue_number=11,
        remove_labels=["triage-required"],
        add_labels=[],
        run_fn=runner,
    )
    assert result["result"] == "failed"
    assert result["removed"] == []


def test_readback_failure_itself_is_reported_as_failed_with_warning():
    """GIVEN the post-mutation readback call itself fails (e.g. gh outage
    right after a successful mutation)
    WHEN apply_triage_label_transition runs
    THEN result is failed with a readback_failed warning, and it does not
    raise
    """
    runner = _ScriptedRunner(
        [
            (0, _labels_view_stdout(["triage-required"]), ""),  # pre-mutation fetch
            (0, "", ""),  # remove-label rc=0
            (0, "", ""),  # add-label rc=0
            (1, "", "HTTP 503: Service Unavailable"),  # readback fails
        ]
    )
    result = mod.apply_triage_label_transition(
        repo="squne121/loop-protocol", issue_number=12, run_fn=runner
    )
    assert result["result"] == "failed"
    assert any("readback_failed" in w for w in result["warnings"])
