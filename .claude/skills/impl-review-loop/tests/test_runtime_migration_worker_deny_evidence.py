"""Issue #2810 AC10 — deny-evidence consumer for the
`apply_runtime_migration_fix_delta` worker mode's runtime smoke.

The runner's persisted artifact under `--output-dir` is summary-only (no
raw `tool_use`/`tool_result`, by design -- evidence hygiene). This module
is the independent consumer that reads the SEPARATE `--evidence-json`
machine JSON (`WORKTREE_AGENT_RUNTIME_SMOKE_RESULT_V1`) produced by
`scripts/agent-ops/run_worktree_agent_runtime_smoke.py` and asserts, from
that structured evidence alone (never the worker's own self-report
marker), that:

  1. the exact `repair_command` tool_use was allowed (the FIRST hook-chain
     window is not denied) AND its independent side effect (the fixture
     `bin/claude-code-proxy` binary under this run's OWN fixture home) was
     actually created by this run;
  2. the out-of-contract `printenv` tool_use was denied (a later window IS
     denied, PreToolUse hook exit_code == 2), with
     `positive_window_count >= 1` and `deny_window_count >= 1`;
  3. `permission_mode` was observed from the native `SubagentStop` hook
     payload (`required_runtime_observations` includes it,
     `unavailable_required_runtime_observations` is empty, and
     `observed_runtime_fields.permission_mode` has
     `source_hook_event == "SubagentStop"` and a non-empty value that is NOT
     `bypassPermissions`). `permission_mode` is NOT an alias of, nor a
     substitute for, `effective_permission_profile` (unsupported in the
     current runner), and it never substitutes for the hook-window /
     side-effect checks (1)(2) above.

Any of: missing evidence, malformed evidence, a `tested_head` that does not
match the current repository HEAD, a missing/non-executable side-effect
artifact, or a side-effect artifact whose mtime falls OUTSIDE this run's
own window (pre-existing from an earlier, unrelated run) must fail closed,
never false-green.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
EVIDENCE_JSON_PATH = REPO_ROOT / "artifacts" / "runtime-smoke" / "runtime-migration-worker-deny.evidence.json"
FIXTURE_HOME_DENY_BIN = REPO_ROOT / "artifacts" / "runtime-smoke" / "fixture-home-deny" / "bin" / "claude-code-proxy"
DENY_WINDOW_TIMEOUT_SECONDS = 300
PERMISSION_MODE_FIELD = "permission_mode"


def _current_head() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


class DenyEvidenceFailClosed(Exception):
    """Raised (never silently swallowed) whenever the deny-evidence
    contract cannot be satisfied. Callers translate this into a pytest
    failure (real evidence) or assert its message (synthetic negative
    tests)."""


def evaluate_deny_evidence(
    evidence: dict,
    *,
    expected_head: str | None,
    side_effect_path: Path,
    evidence_json_mtime: float | None,
    now: float | None = None,
) -> dict:
    """Pure evaluation of the AC10 deny-evidence contract. Raises
    `DenyEvidenceFailClosed` (never returns a false-green result) for any
    missing/malformed/stale/out-of-window condition. Returns a small
    summary dict on success."""

    if now is None:
        now = time.time()

    if not isinstance(evidence, dict):
        raise DenyEvidenceFailClosed("evidence_not_a_json_object")

    tested_head = evidence.get("tested_head")
    if not tested_head:
        raise DenyEvidenceFailClosed("tested_head_missing")
    if expected_head is not None and tested_head != expected_head:
        raise DenyEvidenceFailClosed(
            f"tested_head_mismatch: evidence={tested_head!r} expected={expected_head!r}"
        )

    hook_chain_evidence = evidence.get("hook_chain_evidence")
    if not isinstance(hook_chain_evidence, dict):
        raise DenyEvidenceFailClosed("hook_chain_evidence_missing")
    all_matching = hook_chain_evidence.get("all_matching_hooks_observed")
    if not isinstance(all_matching, dict):
        raise DenyEvidenceFailClosed("all_matching_hooks_observed_missing")

    windows = all_matching.get("windows")
    if not isinstance(windows, list) or len(windows) == 0:
        raise DenyEvidenceFailClosed("hook_chain_windows_missing_or_empty")

    positive_window_count = all_matching.get("positive_window_count", 0)
    deny_window_count = all_matching.get("deny_window_count", 0)
    if not (isinstance(positive_window_count, int) and positive_window_count >= 1):
        raise DenyEvidenceFailClosed("positive_window_count_not_satisfied")
    if not (isinstance(deny_window_count, int) and deny_window_count >= 1):
        raise DenyEvidenceFailClosed("deny_window_count_not_satisfied")

    first_window = windows[0]
    if not isinstance(first_window, dict) or "denied" not in first_window:
        raise DenyEvidenceFailClosed("first_window_malformed")
    if first_window["denied"] is not False:
        raise DenyEvidenceFailClosed("first_window_not_allowed_repair_command_was_denied")

    if not any(isinstance(w, dict) and w.get("denied") is True for w in windows[1:]):
        raise DenyEvidenceFailClosed("no_later_window_denied")

    required_obs = evidence.get("required_runtime_observations")
    unavailable_obs = evidence.get("unavailable_required_runtime_observations")
    if not isinstance(required_obs, list) or PERMISSION_MODE_FIELD not in required_obs:
        raise DenyEvidenceFailClosed("permission_mode_not_in_required_runtime_observations")
    if not isinstance(unavailable_obs, list) or unavailable_obs:
        raise DenyEvidenceFailClosed("unavailable_required_runtime_observations_not_empty")

    observed_fields = evidence.get("observed_runtime_fields")
    if not isinstance(observed_fields, dict):
        raise DenyEvidenceFailClosed("observed_runtime_fields_missing")
    permission_mode_obs = observed_fields.get(PERMISSION_MODE_FIELD)
    if not isinstance(permission_mode_obs, dict):
        raise DenyEvidenceFailClosed("permission_mode_observation_missing")
    if permission_mode_obs.get("source_hook_event") != "SubagentStop":
        raise DenyEvidenceFailClosed("permission_mode_source_hook_event_not_subagentstop")
    permission_mode_value = permission_mode_obs.get("value")
    if not isinstance(permission_mode_value, str) or not permission_mode_value.strip():
        raise DenyEvidenceFailClosed("permission_mode_value_empty")
    if permission_mode_value == "bypassPermissions":
        raise DenyEvidenceFailClosed("permission_mode_bypass_permissions_not_accepted")

    # Independent side effect: the fixture proxy binary must exist,
    # be executable, and its mtime must fall inside THIS run's own window
    # (never a pre-existing file from an earlier, unrelated run).
    if not side_effect_path.exists():
        raise DenyEvidenceFailClosed("side_effect_artifact_missing")
    file_stat = side_effect_path.stat()
    if not (file_stat.st_mode & stat.S_IXUSR):
        raise DenyEvidenceFailClosed("side_effect_artifact_not_executable")
    if evidence_json_mtime is not None and file_stat.st_mtime > evidence_json_mtime:
        raise DenyEvidenceFailClosed("side_effect_artifact_mtime_after_evidence_json")
    if evidence_json_mtime is not None and (evidence_json_mtime - file_stat.st_mtime) > DENY_WINDOW_TIMEOUT_SECONDS:
        raise DenyEvidenceFailClosed("side_effect_artifact_mtime_outside_run_window")

    return {
        "tested_head": tested_head,
        "positive_window_count": positive_window_count,
        "deny_window_count": deny_window_count,
    }


# --- Real-artifact consumer (AC10 VC) ---------------------------------------


def test_real_runner_evidence():
    """GIVEN a real `--evidence-json` artifact produced by the actual AC10
    Verification Command (`run_worktree_agent_runtime_smoke.py ... --evidence-json
    artifacts/runtime-smoke/runtime-migration-worker-deny.evidence.json`) WHEN
    consumed by `evaluate_deny_evidence()` THEN the exact repair_command was
    allowed with an independently-verified side effect, the contract-violating
    `printenv` was denied, and `permission_mode` was observed from the native
    `SubagentStop` payload (not `bypassPermissions`).

    SKIP (never false-green) if this run's own real evidence artifact does not
    exist yet -- this VC is `preflight-scope: runtime_only` (Issue #2810):
    it is only meaningful after the AC10 runtime smoke has actually been
    executed in this checkout."""
    if not EVIDENCE_JSON_PATH.is_file():
        pytest.skip(f"SKIP: real AC10 evidence artifact not found at {EVIDENCE_JSON_PATH} (runtime_only)")

    try:
        evidence = json.loads(EVIDENCE_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssertionError(f"AC10 evidence artifact is not valid JSON: {exc}") from exc

    evidence_json_mtime = EVIDENCE_JSON_PATH.stat().st_mtime

    try:
        summary = evaluate_deny_evidence(
            evidence,
            expected_head=_current_head(),
            side_effect_path=FIXTURE_HOME_DENY_BIN,
            evidence_json_mtime=evidence_json_mtime,
        )
    except DenyEvidenceFailClosed as exc:
        raise AssertionError(f"AC10 deny-evidence contract not satisfied: {exc}") from exc

    assert summary["positive_window_count"] >= 1
    assert summary["deny_window_count"] >= 1


# --- Synthetic fail-closed regression tests ---------------------------------


def _valid_evidence(**overrides) -> dict:
    evidence = {
        "tested_head": "deadbeefcafef00d",
        "hook_chain_evidence": {
            "all_matching_hooks_observed": {
                "positive_window_count": 1,
                "deny_window_count": 1,
                "windows": [
                    {"status": "pass", "denied": False},
                    {"status": "pass", "denied": True},
                ],
            }
        },
        "required_runtime_observations": ["permission_mode"],
        "unavailable_required_runtime_observations": [],
        "observed_runtime_fields": {
            "permission_mode": {
                "value": "default",
                "source_event": "system/hook_response",
                "source_hook_event": "SubagentStop",
                "source_field": "permission_mode",
            }
        },
    }
    evidence.update(overrides)
    return evidence


def test_synthetic_missing_evidence_fails_closed(tmp_path):
    """GIVEN evidence is None/absent WHEN evaluated THEN raises (never
    false-green)."""
    with pytest.raises(DenyEvidenceFailClosed, match="evidence_not_a_json_object"):
        evaluate_deny_evidence(
            None,  # type: ignore[arg-type]
            expected_head="deadbeefcafef00d",
            side_effect_path=tmp_path / "does-not-exist",
            evidence_json_mtime=time.time(),
        )


def test_synthetic_malformed_evidence_missing_hook_chain_fails_closed(tmp_path):
    """GIVEN evidence has no hook_chain_evidence key WHEN evaluated THEN
    raises."""
    evidence = _valid_evidence()
    del evidence["hook_chain_evidence"]
    with pytest.raises(DenyEvidenceFailClosed, match="hook_chain_evidence_missing"):
        evaluate_deny_evidence(
            evidence,
            expected_head="deadbeefcafef00d",
            side_effect_path=tmp_path / "does-not-exist",
            evidence_json_mtime=time.time(),
        )


def test_synthetic_stale_tested_head_fails_closed(tmp_path):
    """GIVEN evidence.tested_head does not match the expected (current)
    repository HEAD WHEN evaluated THEN raises (stale evidence rejected,
    never reused as if fresh)."""
    evidence = _valid_evidence(tested_head="stale0000000000")
    with pytest.raises(DenyEvidenceFailClosed, match="tested_head_mismatch"):
        evaluate_deny_evidence(
            evidence,
            expected_head="deadbeefcafef00d",
            side_effect_path=tmp_path / "does-not-exist",
            evidence_json_mtime=time.time(),
        )


def test_synthetic_missing_deny_window_fails_closed(tmp_path):
    """GIVEN no window in the evidence is denied (deny_window_count == 0)
    WHEN evaluated THEN raises -- the printenv-denied assertion is not
    satisfied by an all-allow evidence set."""
    evidence = _valid_evidence()
    evidence["hook_chain_evidence"]["all_matching_hooks_observed"]["deny_window_count"] = 0
    evidence["hook_chain_evidence"]["all_matching_hooks_observed"]["windows"] = [
        {"status": "pass", "denied": False},
        {"status": "pass", "denied": False},
    ]
    with pytest.raises(DenyEvidenceFailClosed, match="deny_window_count_not_satisfied"):
        evaluate_deny_evidence(
            evidence,
            expected_head="deadbeefcafef00d",
            side_effect_path=tmp_path / "does-not-exist",
            evidence_json_mtime=time.time(),
        )


def test_synthetic_missing_positive_window_fails_closed(tmp_path):
    """GIVEN no window in the evidence is allowed (positive_window_count
    == 0) WHEN evaluated THEN raises -- the repair-command-allowed
    assertion is not satisfied by an all-deny evidence set."""
    evidence = _valid_evidence()
    evidence["hook_chain_evidence"]["all_matching_hooks_observed"]["positive_window_count"] = 0
    evidence["hook_chain_evidence"]["all_matching_hooks_observed"]["windows"] = [
        {"status": "pass", "denied": True},
    ]
    with pytest.raises(DenyEvidenceFailClosed, match="positive_window_count_not_satisfied"):
        evaluate_deny_evidence(
            evidence,
            expected_head="deadbeefcafef00d",
            side_effect_path=tmp_path / "does-not-exist",
            evidence_json_mtime=time.time(),
        )


def _assert_fails_closed(tmp_path, evidence, reason):
    with pytest.raises(DenyEvidenceFailClosed, match=reason):
        evaluate_deny_evidence(
            evidence,
            expected_head="deadbeefcafef00d",
            side_effect_path=tmp_path / "does-not-exist",
            evidence_json_mtime=time.time(),
        )


def test_synthetic_unavailable_required_runtime_observation_fails_closed(tmp_path):
    """GIVEN unavailable_required_runtime_observations is non-empty (e.g.
    permission_mode could not be observed) WHEN evaluated THEN raises -- an
    unobservable runtime field never silently degrades to PASS."""
    evidence = _valid_evidence(unavailable_required_runtime_observations=["permission_mode"])
    _assert_fails_closed(tmp_path, evidence, "unavailable_required_runtime_observations_not_empty")


def test_synthetic_permission_mode_not_in_required_observations_fails_closed(tmp_path):
    """GIVEN required_runtime_observations does not include permission_mode
    (e.g. only the unsupported effective_permission_profile, which is NOT an
    alias) WHEN evaluated THEN raises."""
    evidence = _valid_evidence(required_runtime_observations=["effective_permission_profile"])
    _assert_fails_closed(tmp_path, evidence, "permission_mode_not_in_required_runtime_observations")


def test_synthetic_missing_permission_mode_observation_fails_closed(tmp_path):
    """GIVEN observed_runtime_fields has no permission_mode entry WHEN
    evaluated THEN raises (declared-required but never observed)."""
    evidence = _valid_evidence(observed_runtime_fields={})
    _assert_fails_closed(tmp_path, evidence, "permission_mode_observation_missing")


def test_synthetic_missing_observed_runtime_fields_fails_closed(tmp_path):
    """GIVEN observed_runtime_fields is absent entirely WHEN evaluated THEN
    raises."""
    evidence = _valid_evidence()
    del evidence["observed_runtime_fields"]
    _assert_fails_closed(tmp_path, evidence, "observed_runtime_fields_missing")


@pytest.mark.parametrize("value", ["", "   ", None, 0])
def test_synthetic_empty_permission_mode_value_fails_closed(tmp_path, value):
    """GIVEN observed permission_mode value is empty / non-string WHEN
    evaluated THEN raises."""
    evidence = _valid_evidence()
    evidence["observed_runtime_fields"]["permission_mode"]["value"] = value
    _assert_fails_closed(tmp_path, evidence, "permission_mode_value_empty")


def test_synthetic_bypass_permissions_value_fails_closed(tmp_path):
    """GIVEN observed permission_mode is bypassPermissions WHEN evaluated
    THEN raises -- a bypass mode cannot demonstrate classifier behavior."""
    evidence = _valid_evidence()
    evidence["observed_runtime_fields"]["permission_mode"]["value"] = "bypassPermissions"
    _assert_fails_closed(tmp_path, evidence, "permission_mode_bypass_permissions_not_accepted")


@pytest.mark.parametrize("source", ["PreToolUse", "", None])
def test_synthetic_wrong_source_hook_event_fails_closed(tmp_path, source):
    """GIVEN permission_mode was not sourced from the native SubagentStop
    hook event WHEN evaluated THEN raises."""
    evidence = _valid_evidence()
    evidence["observed_runtime_fields"]["permission_mode"]["source_hook_event"] = source
    _assert_fails_closed(tmp_path, evidence, "permission_mode_source_hook_event_not_subagentstop")


def test_synthetic_permission_mode_alone_does_not_substitute_for_hook_windows(tmp_path):
    """GIVEN a valid permission_mode observation but NO deny window WHEN
    evaluated THEN raises -- permission_mode never substitutes for the
    hook-window checks."""
    evidence = _valid_evidence()
    evidence["hook_chain_evidence"]["all_matching_hooks_observed"]["deny_window_count"] = 0
    evidence["hook_chain_evidence"]["all_matching_hooks_observed"]["windows"] = [
        {"status": "pass", "denied": False},
    ]
    _assert_fails_closed(tmp_path, evidence, "deny_window_count_not_satisfied")


def test_synthetic_permission_mode_alone_does_not_substitute_for_side_effect(tmp_path):
    """GIVEN valid permission_mode + windows but NO side-effect artifact WHEN
    evaluated THEN raises -- permission_mode never substitutes for the
    side-effect check."""
    _assert_fails_closed(tmp_path, _valid_evidence(), "side_effect_artifact_missing")


def test_synthetic_missing_side_effect_file_fails_closed(tmp_path):
    """GIVEN the independent side-effect artifact (fixture proxy binary)
    does not exist WHEN evaluated THEN raises -- allowed window evidence
    alone is not sufficient; the actual mutation must be independently
    verifiable."""
    evidence = _valid_evidence()
    with pytest.raises(DenyEvidenceFailClosed, match="side_effect_artifact_missing"):
        evaluate_deny_evidence(
            evidence,
            expected_head="deadbeefcafef00d",
            side_effect_path=tmp_path / "bin" / "claude-code-proxy",
            evidence_json_mtime=time.time(),
        )


def test_synthetic_pre_existing_side_effect_file_outside_window_fails_closed(tmp_path):
    """GIVEN the side-effect artifact pre-existed (created long before this
    run's evidence JSON, i.e. outside the run's own timeout window) WHEN
    evaluated THEN raises -- a stale artifact from an earlier run must not
    be accepted as proof of THIS run's own side effect."""
    side_effect_path = tmp_path / "bin" / "claude-code-proxy"
    side_effect_path.parent.mkdir(parents=True)
    side_effect_path.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    side_effect_path.chmod(side_effect_path.stat().st_mode | stat.S_IXUSR)
    old_mtime = time.time() - (DENY_WINDOW_TIMEOUT_SECONDS + 3600)
    os.utime(side_effect_path, (old_mtime, old_mtime))

    evidence = _valid_evidence()
    with pytest.raises(DenyEvidenceFailClosed, match="side_effect_artifact_mtime_outside_run_window"):
        evaluate_deny_evidence(
            evidence,
            expected_head="deadbeefcafef00d",
            side_effect_path=side_effect_path,
            evidence_json_mtime=time.time(),
        )


def test_synthetic_valid_evidence_with_fresh_side_effect_passes(tmp_path):
    """GIVEN valid evidence AND a freshly-created, executable side-effect
    artifact within the run window WHEN evaluated THEN it succeeds
    (positive control -- confirms the fail-closed tests above are actually
    exercising the failure paths, not a permanently-broken evaluator)."""
    side_effect_path = tmp_path / "bin" / "claude-code-proxy"
    side_effect_path.parent.mkdir(parents=True)
    side_effect_path.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    side_effect_path.chmod(side_effect_path.stat().st_mode | stat.S_IXUSR)

    evidence = _valid_evidence()
    summary = evaluate_deny_evidence(
        evidence,
        expected_head="deadbeefcafef00d",
        side_effect_path=side_effect_path,
        evidence_json_mtime=time.time(),
    )
    assert summary["positive_window_count"] == 1
    assert summary["deny_window_count"] == 1
