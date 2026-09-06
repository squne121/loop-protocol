"""
Tests for finalize_no_diff_issue.py (Issue #1116).

Uses fake-gh/fixture-style unit tests: `_fetch_issue_snapshot`,
`_close_issue_live`, and `_publish_marker_comment` are monkeypatched
directly (mirroring the existing pattern in
scripts/agent-guards/tests/test_controlled_skill_mutation_exec.py) rather
than shelling out to a real `gh` binary. No live Issue is ever closed by
these tests.

AC references:
- AC1: reaches the finalizer from a no-diff path, no PR required
- AC2: finalizer does not re-evaluate the semantic termination decision
- AC3: mutation-before-check target validation (repo/type/current state)
- AC4: stable ownership marker + digest marker separation, write-response-
  loss read-back-before-retry
- AC5: read-back after close; mismatch is never success
- AC6: partial-failure status vocabulary, no automatic rollback, reopened-
  issue guard
- AC7: stdout is compact ISSUE_FINALIZE_RESULT_V1 JSON only
- AC8: covered together with test_termination_gate.py's own new test
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR.parent / "scripts"
_REPO_ROOT = _THIS_DIR.resolve().parents[3]
_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures" / "issue-finalize"

_MODULE_NAME = "finalize_no_diff_issue_1116"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPTS_DIR / "finalize_no_diff_issue.py")
assert _spec is not None and _spec.loader is not None
finalize_no_diff_issue = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = finalize_no_diff_issue
_spec.loader.exec_module(finalize_no_diff_issue)

TRUSTED_REPO = finalize_no_diff_issue.TRUSTED_REPO


def _base_request(**overrides) -> dict:
    request = {
        "schema": finalize_no_diff_issue.FINALIZE_REQUEST_SCHEMA,
        "issue_number": 1116,
        "repo": TRUSTED_REPO,
        "reason": "completed",
        "ac_results": [{"ac": "AC1", "status": "applicable", "reason": "verified"}],
        "evidence_body": "no-diff evidence payload",
        "supersedes": [],
    }
    request.update(overrides)
    return request


def _snapshot(state: str, state_reason: str | None = None, is_pull_request: bool = False) -> dict:
    return {"number": 0, "state": state, "state_reason": state_reason, "is_pull_request": is_pull_request}


@pytest.fixture(autouse=True)
def _default_evidence_final_readback_ok(monkeypatch):
    """Issue #1116 fix_delta #3: `_finalize_one_target` always re-verifies the
    evidence comment after close (`_verify_evidence_comment_marker_present` /
    `_verify_existing_comment_unchanged`). Every test in this module other
    than the ones that specifically target that re-verification treats it as
    succeeding by default, so pre-existing tests do not need to restate an
    unrelated mock. Tests that exercise the failure path override this via
    their own `monkeypatch.setattr(...)` call."""
    monkeypatch.setattr(finalize_no_diff_issue, "_verify_evidence_comment_marker_present", lambda *a, **k: "")
    monkeypatch.setattr(finalize_no_diff_issue, "_verify_existing_comment_unchanged", lambda *a, **k: "")


# ---------------------------------------------------------------------------
# AC1/AC2/AC3: reaches finalizer without a PR, never re-evaluates decision
# ---------------------------------------------------------------------------


def test_reaches_finalizer_from_no_diff_path_without_pr(monkeypatch):
    """AC1: a no-diff termination decision reaches finalize_no_diff_issue.py
    and completes end-to-end without any PR number/PR-shaped field ever
    being required by the request schema or the orchestration path."""
    request = _base_request()
    assert "pr_number" not in request and "pr_url" not in request

    snapshots = [_snapshot("OPEN"), _snapshot("CLOSED", state_reason="completed")]

    def fake_snapshot(issue_number, repo, gh_bin):
        return snapshots.pop(0), ""

    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", fake_snapshot)
    monkeypatch.setattr(finalize_no_diff_issue, "_close_issue_live", lambda *a, **k: "")
    monkeypatch.setattr(
        finalize_no_diff_issue,
        "_publish_marker_comment",
        lambda **k: ("created", "101", "https://x/101", ""),
    )

    result = finalize_no_diff_issue.run_finalize(request)

    assert result["schema"] == "ISSUE_FINALIZE_RESULT_V1"
    assert result["status"] == "ok"
    target = result["targets"]["1116"]
    assert target["target_status"] == "applied"
    assert set(target["completed_operations"]) == {"evidence_comment", "close"}
    assert target["incomplete_operations"] == []


def test_finalizer_does_not_reevaluate_semantic_decision(monkeypatch):
    """AC2: even when ac_results contains a pre_existing_failure entry, the
    finalizer executes the caller's requested close reason as-is -- it does
    not inspect ac_results content to override/block the decision."""
    ac_results = json.loads((_FIXTURES_DIR / "ac_results_with_pre_existing_failure.json").read_text())
    request = _base_request(ac_results=ac_results, reason="completed")

    close_calls = []

    def fake_close(issue_number, repo, reason, gh_bin):
        close_calls.append(reason)
        return ""

    snapshots = [_snapshot("OPEN"), _snapshot("CLOSED", state_reason="completed")]
    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (snapshots.pop(0), ""))
    monkeypatch.setattr(finalize_no_diff_issue, "_close_issue_live", fake_close)
    monkeypatch.setattr(
        finalize_no_diff_issue, "_publish_marker_comment", lambda **k: ("created", "1", "https://x/1", "")
    )

    result = finalize_no_diff_issue.run_finalize(request)

    assert close_calls == ["completed"]  # requested reason executed unchanged
    assert result["targets"]["1116"]["target_status"] == "applied"


# ---------------------------------------------------------------------------
# AC3: mutation-before-check target validation
# ---------------------------------------------------------------------------


def test_rejects_pr_number_as_issue_target(monkeypatch):
    """AC3: if any target (including a supersedes target) is actually a
    pull request, the whole run fails BEFORE any mutation is attempted for
    ANY target -- not just the offending one."""
    request = _base_request(supersedes=[9999])

    def fake_snapshot(issue_number, repo, gh_bin):
        if issue_number == 9999:
            return _snapshot("OPEN", is_pull_request=True), ""
        return _snapshot("OPEN"), ""

    close_calls = []
    publish_calls = []
    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", fake_snapshot)
    monkeypatch.setattr(
        finalize_no_diff_issue, "_close_issue_live", lambda *a, **k: close_calls.append(a) or ""
    )
    monkeypatch.setattr(
        finalize_no_diff_issue,
        "_publish_marker_comment",
        lambda **k: publish_calls.append(k) or ("created", "1", "https://x/1", ""),
    )

    result = finalize_no_diff_issue.run_finalize(request)

    assert result["status"] == "failed"
    assert any("target_is_pull_request" in e for e in result["errors"])
    assert result["targets"]["9999"]["errors"] == ["target_is_pull_request"]
    assert close_calls == []
    assert publish_calls == []


# ---------------------------------------------------------------------------
# AC4: marker/digest separation (create/update/no-op), write-response-loss
# ---------------------------------------------------------------------------


def test_marker_and_digest_separation_create_update_noop(monkeypatch):
    """AC4: the ownership marker and digest marker are separate lines, the
    ownership marker is stable across re-invocations with the same semantic
    request, and finalize correctly reflects create / update / no-op
    outcomes reported by the issue_comment.publish delegate."""
    request = _base_request()
    run_id = finalize_no_diff_issue.derive_run_id(request)

    captured: list[dict] = []

    def make_publish(status_detail):
        def _publish(*, issue_number, repo, marker, comment_body, gh_bin, project_root):
            captured.append({"marker": marker, "comment_body": comment_body})
            return (status_detail, "1", "https://x/1", "")

        return _publish

    # -- create: issue OPEN, no prior evidence comment ----------------------
    snapshots_by_call = [_snapshot("OPEN"), _snapshot("CLOSED", state_reason="completed")]
    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (snapshots_by_call.pop(0), ""))
    monkeypatch.setattr(finalize_no_diff_issue, "_close_issue_live", lambda *a, **k: "")
    monkeypatch.setattr(finalize_no_diff_issue, "_publish_marker_comment", make_publish("created"))
    result_create = finalize_no_diff_issue.run_finalize(request)
    assert result_create["targets"]["1116"]["evidence_comment"]["status_detail"] == "created"
    assert result_create["targets"]["1116"]["target_status"] == "applied"

    # -- update: issue OPEN, prior evidence comment had a different digest --
    snapshots_by_call2 = [_snapshot("OPEN"), _snapshot("CLOSED", state_reason="completed")]
    monkeypatch.setattr(
        finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (snapshots_by_call2.pop(0), "")
    )
    monkeypatch.setattr(finalize_no_diff_issue, "_publish_marker_comment", make_publish("updated"))
    result_update = finalize_no_diff_issue.run_finalize(request)
    assert result_update["targets"]["1116"]["evidence_comment"]["status_detail"] == "updated"
    assert result_update["targets"]["1116"]["target_status"] == "applied"

    # -- no-op: issue already CLOSED with matching reason, evidence matches -
    snapshots_by_call3 = [
        _snapshot("CLOSED", state_reason="completed"),
        _snapshot("CLOSED", state_reason="completed"),
    ]
    monkeypatch.setattr(
        finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (snapshots_by_call3.pop(0), "")
    )
    monkeypatch.setattr(finalize_no_diff_issue, "_publish_marker_comment", make_publish("already_published"))
    result_noop = finalize_no_diff_issue.run_finalize(request)
    assert result_noop["targets"]["1116"]["evidence_comment"]["status_detail"] == "already_published"
    assert result_noop["targets"]["1116"]["target_status"] == "no_op"

    # -- marker/digest shape + stability across all three invocations ------
    assert len(captured) == 3
    ownership_lines = [c["comment_body"].splitlines()[0] for c in captured]
    digest_lines = [c["comment_body"].splitlines()[1] for c in captured]
    assert len(set(ownership_lines)) == 1  # same ownership marker every time (same request => same run_id)
    for line in ownership_lines:
        m = finalize_no_diff_issue._OWNERSHIP_MARKER_RE.match(line)
        assert m is not None
        assert m.group("run_id") == run_id
        assert m.group("issue") == "1116"
    for line in digest_lines:
        assert finalize_no_diff_issue._DIGEST_MARKER_RE.match(line) is not None
    for c in captured:
        assert c["marker"] == ownership_lines[0]


def test_write_response_loss_reads_back_before_retry(monkeypatch):
    """AC4: when the issue_comment.publish delegate reports that a mutation
    may have actually happened remotely despite a local failure (write
    response lost), finalize marks the target result_unknown/retryable and
    does NOT blindly attempt a second POST within the same run. A later,
    independent re-invocation with the same request (same derived run_id)
    that discovers the comment already published (idempotent precheck)
    completes cleanly using the identical marker/body, proving no duplicate
    payload was fabricated."""
    request = _base_request()

    publish_calls: list[dict] = []

    def flaky_publish(*, issue_number, repo, marker, comment_body, gh_bin, project_root):
        publish_calls.append({"marker": marker, "comment_body": comment_body})
        return "", "", "", "mutation_outcome_unknown:readback_failed:marker_not_found"

    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (_snapshot("OPEN"), ""))
    monkeypatch.setattr(finalize_no_diff_issue, "_close_issue_live", lambda *a, **k: "")
    monkeypatch.setattr(finalize_no_diff_issue, "_publish_marker_comment", flaky_publish)

    first = finalize_no_diff_issue.run_finalize(request)
    target = first["targets"]["1116"]
    assert target["result_unknown"] is True
    assert target["retryable"] is True
    assert target["target_status"] == "result_unknown"
    assert len(publish_calls) == 1  # no blind in-run retry

    def recovered_publish(*, issue_number, repo, marker, comment_body, gh_bin, project_root):
        publish_calls.append({"marker": marker, "comment_body": comment_body})
        return "already_published", "101", "https://x/101", ""

    snapshots = [_snapshot("OPEN"), _snapshot("CLOSED", state_reason="completed")]
    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (snapshots.pop(0), ""))
    monkeypatch.setattr(finalize_no_diff_issue, "_publish_marker_comment", recovered_publish)

    second = finalize_no_diff_issue.run_finalize(request)
    assert second["targets"]["1116"]["target_status"] == "applied"
    assert second["targets"]["1116"]["result_unknown"] is False
    assert len(publish_calls) == 2
    assert publish_calls[0]["marker"] == publish_calls[1]["marker"]
    assert publish_calls[0]["comment_body"] == publish_calls[1]["comment_body"]


# ---------------------------------------------------------------------------
# AC5: read-back after close; mismatch is never success
# ---------------------------------------------------------------------------


def test_readback_state_mismatch_is_not_success(monkeypatch):
    """AC5: gh issue close reports success (no error), but the post-mutation
    read-back still shows the Issue OPEN -- this must never be reported as
    a successful close."""
    request = _base_request()
    snapshots = [_snapshot("OPEN"), _snapshot("OPEN")]  # still OPEN after "successful" close
    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (snapshots.pop(0), ""))
    monkeypatch.setattr(finalize_no_diff_issue, "_close_issue_live", lambda *a, **k: "")
    monkeypatch.setattr(
        finalize_no_diff_issue, "_publish_marker_comment", lambda **k: ("created", "1", "https://x/1", "")
    )

    result = finalize_no_diff_issue.run_finalize(request)

    target = result["targets"]["1116"]
    assert target["target_status"] != "applied"
    assert target["target_status"] != "no_op"
    assert "close" in target["incomplete_operations"]
    assert "postcondition_close_state_mismatch" in target["errors"]
    assert result["status"] != "ok"


# ---------------------------------------------------------------------------
# AC6: partial-failure vocabulary, no rollback, reopened-issue guard
# ---------------------------------------------------------------------------


def test_partial_failure_status_vocabulary_and_no_rollback(monkeypatch):
    """AC6: primary target fully succeeds while a supersedes target's close
    fails after its evidence comment already posted. The primary's already
    -completed operations are never rolled back, and finalize never defines
    or calls any reopen/delete-style undo primitive."""
    request = _base_request(supersedes=[2000])
    assert not hasattr(finalize_no_diff_issue, "_reopen_issue")

    def fake_snapshot(issue_number, repo, gh_bin):
        if issue_number == 1116:
            return (snap_primary.pop(0), "")
        return (snap_super.pop(0), "")

    snap_primary = [_snapshot("OPEN"), _snapshot("CLOSED", state_reason="completed")]
    snap_super = [_snapshot("OPEN"), _snapshot("OPEN")]  # close never actually applies

    def fake_close(issue_number, repo, reason, gh_bin):
        if issue_number == 2000:
            return "gh_issue_close_failed_rc_1:simulated"
        return ""

    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", fake_snapshot)
    monkeypatch.setattr(finalize_no_diff_issue, "_close_issue_live", fake_close)
    monkeypatch.setattr(
        finalize_no_diff_issue, "_publish_marker_comment", lambda **k: ("created", "1", "https://x/1", "")
    )

    result = finalize_no_diff_issue.run_finalize(request)

    assert result["status"] == "partial"
    primary = result["targets"]["1116"]
    superseded = result["targets"]["2000"]
    assert primary["target_status"] == "applied"
    assert set(primary["completed_operations"]) == {"evidence_comment", "close"}
    assert superseded["target_status"] == "failed_after_mutation"
    assert "evidence_comment" in superseded["completed_operations"]
    assert "close" in superseded["incomplete_operations"]
    assert superseded["retryable"] is True


def test_reopened_issue_not_reclosed_from_stale_marker_alone(monkeypatch):
    """AC6: a target that was previously closed and then reopened by a human
    (live state is OPEN right now, even though a stale evidence marker
    comment from a prior run still exists) must be re-closed based on the
    FRESH live state and this invocation's explicit --reason -- never
    skipped merely because an old marker is present. The stale marker's
    digest is independently brought up to date via the update path."""
    request = _base_request()
    close_calls = []
    snapshots = [_snapshot("OPEN"), _snapshot("CLOSED", state_reason="completed")]

    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (snapshots.pop(0), ""))
    monkeypatch.setattr(
        finalize_no_diff_issue,
        "_close_issue_live",
        lambda issue_number, repo, reason, gh_bin: (close_calls.append((issue_number, reason)), "")[1],
    )
    monkeypatch.setattr(
        finalize_no_diff_issue, "_publish_marker_comment", lambda **k: ("updated", "1", "https://x/1", "")
    )

    result = finalize_no_diff_issue.run_finalize(request)

    assert close_calls == [(1116, "completed")]  # close was actually attempted, not skipped
    assert result["targets"]["1116"]["target_status"] == "applied"


# ---------------------------------------------------------------------------
# AC7: stdout is compact ISSUE_FINALIZE_RESULT_V1 JSON only
# ---------------------------------------------------------------------------


def test_stdout_is_compact_v1_json_only(monkeypatch, capsys, tmp_path):
    ac_results_file = _FIXTURES_DIR / "ac_results_all_applicable.json"
    evidence_file = _FIXTURES_DIR / "evidence_body_sample.md"

    snapshots = [_snapshot("OPEN"), _snapshot("CLOSED", state_reason="completed")]
    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (snapshots.pop(0), ""))
    monkeypatch.setattr(finalize_no_diff_issue, "_close_issue_live", lambda *a, **k: "")
    monkeypatch.setattr(
        finalize_no_diff_issue, "_publish_marker_comment", lambda **k: ("created", "1", "https://x/1", "")
    )

    rc = finalize_no_diff_issue.main(
        [
            "--issue-number",
            "1116",
            "--repo",
            TRUSTED_REPO,
            "--reason",
            "completed",
            "--ac-results-file",
            str(ac_results_file),
            "--evidence-body-file",
            str(evidence_file),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    stdout_text = captured.out.strip()
    assert "\n" not in stdout_text  # single compact line, not pretty-printed
    parsed = json.loads(stdout_text)
    assert parsed["schema"] == "ISSUE_FINALIZE_RESULT_V1"
    assert parsed["status"] == "ok"
    # Never leaks raw child-process output / full issue body / full comment body.
    assert "issuecomment" not in stdout_text or "comment_url" in parsed["targets"]["1116"]["evidence_comment"]
    assert len(stdout_text.encode("utf-8")) < 5000


# ---------------------------------------------------------------------------
# Issue #1116 fix_delta (OWNER REQUEST_CHANGES, PR #2544
# issuecomment-5559852951): the final `applied | no_op | failed_* |
# result_unknown` classification must be derived from independent
# postconditions (final read-back of Issue state AND of the evidence
# comment), never from local "did we attempt this call" bookkeeping alone.
# ---------------------------------------------------------------------------


def test_reopen_during_processing_final_open_not_success(monkeypatch):
    """fix_delta #1: the Issue starts CLOSED with the requested reason (so
    close is skipped entirely -- never attempted this run), but the FINAL
    read-back shows it OPEN (reopened by something else mid-run). This must
    never be reported as `no_op` (or any other success status), even though
    this run never itself called `gh issue close`."""
    request = _base_request()
    snapshots = [_snapshot("CLOSED", state_reason="completed"), _snapshot("OPEN")]
    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (snapshots.pop(0), ""))
    close_calls = []
    monkeypatch.setattr(finalize_no_diff_issue, "_close_issue_live", lambda *a, **k: close_calls.append(1) or "")
    monkeypatch.setattr(
        finalize_no_diff_issue, "_publish_marker_comment", lambda **k: ("already_published", "1", "https://x/1", "")
    )

    result = finalize_no_diff_issue.run_finalize(request)

    target = result["targets"]["1116"]
    assert close_calls == []  # close was correctly never attempted (initial snapshot was already CLOSED+matching)
    assert target["target_status"] != "no_op"
    assert target["target_status"] != "applied"
    assert "close" in target["incomplete_operations"]
    assert result["status"] != "ok"


def test_comment_post_response_loss_readback_confirms_reconciled(monkeypatch):
    """fix_delta #2: the issue_comment.publish delegate call itself can lose
    its own response after the remote mutation actually applied (the
    controlled-executor's own POST-vs-readback reconciliation, exercised
    here via a `_publish_marker_comment` that reports the reconciled
    success directly, as the real executor now does per
    scripts/agent-guards/controlled_skill_mutation_exec.py's
    `_run_issue_comment_publish` POST-failure readback branch). This must
    complete as `evidence_comment` in completed_operations, never
    result_unknown, and never trigger a duplicate publish attempt."""
    request = _base_request()
    publish_calls = []

    def reconciled_publish(*, issue_number, repo, marker, comment_body, gh_bin, project_root):
        publish_calls.append(1)
        return ("created", "555", "https://x/555", "")

    snapshots = [_snapshot("OPEN"), _snapshot("CLOSED", state_reason="completed")]
    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (snapshots.pop(0), ""))
    monkeypatch.setattr(finalize_no_diff_issue, "_close_issue_live", lambda *a, **k: "")
    monkeypatch.setattr(finalize_no_diff_issue, "_publish_marker_comment", reconciled_publish)

    result = finalize_no_diff_issue.run_finalize(request)

    target = result["targets"]["1116"]
    assert len(publish_calls) == 1  # no duplicate publish attempt
    assert "evidence_comment" in target["completed_operations"]
    assert target["result_unknown"] is False
    assert target["target_status"] == "applied"


def test_ambiguous_comment_write_result_unknown_not_failed_no_mutation(monkeypatch):
    """fix_delta #2: when the remote success/failure of the evidence-comment
    write genuinely cannot be determined (the executor's own readback was
    itself inconclusive, reported via the `mutation_outcome_unknown:`
    prefix), the target must be `result_unknown` -- distinctly NOT
    `failed_no_mutation`, since a mutation may well have happened."""
    request = _base_request()

    def ambiguous_publish(*, issue_number, repo, marker, comment_body, gh_bin, project_root):
        return "", "", "", "mutation_outcome_unknown:gh_api_post_comment_exception: timeout"

    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (_snapshot("OPEN"), ""))
    monkeypatch.setattr(finalize_no_diff_issue, "_close_issue_live", lambda *a, **k: "")
    monkeypatch.setattr(finalize_no_diff_issue, "_publish_marker_comment", ambiguous_publish)

    result = finalize_no_diff_issue.run_finalize(request)

    target = result["targets"]["1116"]
    assert target["target_status"] == "result_unknown"
    assert target["target_status"] != "failed_no_mutation"
    assert target["retryable"] is True


def test_close_transport_error_but_readback_closed_reconciles(monkeypatch):
    """fix_delta #4: `gh issue close` itself reports a transport error, but
    the final read-back shows the Issue is actually CLOSED with the
    requested reason -- this must reconcile as `close` in
    completed_operations, with the transport error retained in `errors` as
    diagnostic-only information, and must NOT require a retry."""
    request = _base_request()
    snapshots = [_snapshot("OPEN"), _snapshot("CLOSED", state_reason="completed")]
    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (snapshots.pop(0), ""))
    monkeypatch.setattr(
        finalize_no_diff_issue, "_close_issue_live", lambda *a, **k: "gh_issue_close_failed_rc_1:simulated_timeout"
    )
    monkeypatch.setattr(
        finalize_no_diff_issue, "_publish_marker_comment", lambda **k: ("created", "1", "https://x/1", "")
    )

    result = finalize_no_diff_issue.run_finalize(request)

    target = result["targets"]["1116"]
    assert "close" in target["completed_operations"]
    assert "close" not in target["incomplete_operations"]
    assert target["retryable"] is False
    assert target["target_status"] == "applied"
    assert any("gh_issue_close_failed_rc_1" in e for e in target["errors"])  # diagnostic-only, not blocking


def test_evidence_comment_missing_after_close_not_success(monkeypatch):
    """fix_delta #3: the evidence comment publish itself reports success,
    and close succeeds and is confirmed -- but the post-close re-verification
    of the evidence comment discovers it has vanished (or drifted). This
    must never be reported as success."""
    request = _base_request()
    snapshots = [_snapshot("OPEN"), _snapshot("CLOSED", state_reason="completed")]
    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (snapshots.pop(0), ""))
    monkeypatch.setattr(finalize_no_diff_issue, "_close_issue_live", lambda *a, **k: "")
    monkeypatch.setattr(
        finalize_no_diff_issue, "_publish_marker_comment", lambda **k: ("created", "1", "https://x/1", "")
    )
    monkeypatch.setattr(
        finalize_no_diff_issue,
        "_verify_evidence_comment_marker_present",
        lambda *a, **k: "evidence_comment_missing_after_close",
    )

    result = finalize_no_diff_issue.run_finalize(request)

    target = result["targets"]["1116"]
    assert target["target_status"] != "applied"
    assert target["target_status"] != "no_op"
    assert "evidence_comment" in target["incomplete_operations"]
    assert "evidence_comment_missing_after_close" in target["errors"]


def test_already_closed_but_evidence_created_is_applied_not_no_op(monkeypatch):
    """fix_delta #5: the Issue starts (and stays) CLOSED with the requested
    reason, so close is never attempted this run -- but the evidence
    comment IS newly created/updated this run. This is a real mutation, so
    the target must be `applied`, never `no_op`."""
    request = _base_request()
    snapshots = [_snapshot("CLOSED", state_reason="completed"), _snapshot("CLOSED", state_reason="completed")]
    monkeypatch.setattr(finalize_no_diff_issue, "_fetch_issue_snapshot", lambda *a, **k: (snapshots.pop(0), ""))
    close_calls = []
    monkeypatch.setattr(finalize_no_diff_issue, "_close_issue_live", lambda *a, **k: close_calls.append(1) or "")
    monkeypatch.setattr(
        finalize_no_diff_issue, "_publish_marker_comment", lambda **k: ("created", "1", "https://x/1", "")
    )

    result = finalize_no_diff_issue.run_finalize(request)

    target = result["targets"]["1116"]
    assert close_calls == []  # close correctly never attempted
    assert target["target_status"] == "applied"
    assert target["target_status"] != "no_op"


# ---------------------------------------------------------------------------
# AC8: real CLI process-boundary integration tests (Issue #1116 fix_delta).
#
# These launch finalize_no_diff_issue.py's `main()` as an actual separate OS
# process (not an in-process function call), with a fake `gh` executable
# injected via PATH -- crossing a real CLI process boundary end-to-end
# (argparse -> run_finalize -> stdout JSON), rather than only exercising
# Python-level monkeypatched functions.
#
# The `issue_comment.publish` controlled-executor subprocess boundary
# (scripts/agent-guards/controlled_skill_mutation_exec.py) is intentionally
# NOT exercised here with this fake `gh`: that module's `_find_gh_bin()`
# pins gh discovery to a fixed trusted path list
# (`/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`) that ignores PATH by
# design (Issue #1539 hardening), and is outside this Issue's allowed
# change surface (`_run_issue_comment_publish` + its single-caller helpers
# only). In this environment a real, already-authenticated `gh` exists on
# that trusted path, so any test that reached the executor's own gh calls
# would make REAL calls against the REAL TRUSTED_REPO -- unacceptable given
# AC8's explicit "never close a live Issue" requirement. These tests
# therefore use `--existing-evidence-comment-url` (verified via
# `gh issue view --json comments`, resolved through the plain,
# PATH-overridable `gh_bin` used directly by finalize_no_diff_issue.py) so
# the evidence-comment leg never spawns the controlled-executor subprocess
# at all, while still covering a real close + read-back round trip through
# a genuine OS process. The controlled-executor's own CLI entrypoint is
# separately confirmed reachable as a real subprocess (safely, via
# --dry-run) by
# scripts/agent-guards/tests/test_controlled_skill_mutation_exec.py::TestControlledExecutorRealSubprocessBoundary.
# ---------------------------------------------------------------------------

_FAKE_GH_SCRIPT = """#!/usr/bin/env python3
import json
import os
import sys

state_path = os.environ["FAKE_GH_STATE_FILE"]


def _load():
    with open(state_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _save(state):
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


argv = sys.argv[1:]

if len(argv) == 2 and argv[0] == "api" and "/comments" not in argv[1]:
    state = _load()
    print(json.dumps({"state": state["state"], "state_reason": state.get("state_reason")}))
    sys.exit(0)

if argv[:2] == ["issue", "close"]:
    state = _load()
    reason_arg = argv[argv.index("--reason") + 1]
    mapped_reason = "completed" if reason_arg == "completed" else "not_planned"
    state["state"] = "CLOSED"
    state["state_reason"] = mapped_reason
    _save(state)
    if os.environ.get("FAKE_GH_CLOSE_TRANSPORT_FAIL") == "1":
        sys.stderr.write("simulated_transport_failure\\n")
        sys.exit(1)
    sys.exit(0)

if argv[:2] == ["issue", "view"] and "comments" in argv:
    state = _load()
    print(json.dumps({"comments": state.get("comments", [])}))
    sys.exit(0)

sys.stderr.write("fake_gh_unhandled_argv: %r\\n" % (argv,))
sys.exit(1)
"""


def _write_fake_gh(tmp_path: Path, state: dict) -> tuple[Path, Path]:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    gh_path = bin_dir / "gh"
    gh_path.write_text(_FAKE_GH_SCRIPT, encoding="utf-8")
    gh_path.chmod(0o755)
    state_path = tmp_path / "fake_gh_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return bin_dir, state_path


def test_ac8_cli_boundary_happy_path_close_and_existing_evidence(tmp_path):
    """AC8 happy path, real process boundary: a real OS subprocess
    invocation of finalize_no_diff_issue.py's CLI closes an OPEN Issue and
    verifies a caller-referenced existing evidence comment, end to end."""
    comment_url = "https://github.com/squne121/loop-protocol/issues/1116#issuecomment-900001"
    bin_dir, state_path = _write_fake_gh(
        tmp_path,
        {"state": "OPEN", "state_reason": None, "comments": [{"url": comment_url, "body": "evidence payload"}]},
    )

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_GH_STATE_FILE"] = str(state_path)

    ac_results_file = _FIXTURES_DIR / "ac_results_all_applicable.json"
    script = _SCRIPTS_DIR / "finalize_no_diff_issue.py"

    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--issue-number",
            "1116",
            "--repo",
            TRUSTED_REPO,
            "--reason",
            "completed",
            "--ac-results-file",
            str(ac_results_file),
            "--existing-evidence-comment-url",
            comment_url,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout.strip())
    assert parsed["schema"] == "ISSUE_FINALIZE_RESULT_V1"
    assert parsed["status"] == "ok"
    target = parsed["targets"]["1116"]
    assert target["target_status"] == "applied"
    assert set(target["completed_operations"]) == {"evidence_comment", "close"}
    assert target["incomplete_operations"] == []

    final_state = json.loads(state_path.read_text())
    assert final_state["state"] == "CLOSED"
    assert final_state["state_reason"] == "completed"


def test_ac8_cli_boundary_close_transport_ambiguous_reconciles(tmp_path):
    """AC8 transport-ambiguous path, real process boundary: `gh issue close`
    reports a transport failure (non-zero exit) even though the mutation
    actually applied remotely. The real CLI process must reconcile this via
    its own post-mutation read-back rather than reporting failure."""
    comment_url = "https://github.com/squne121/loop-protocol/issues/1116#issuecomment-900002"
    bin_dir, state_path = _write_fake_gh(
        tmp_path,
        {"state": "OPEN", "state_reason": None, "comments": [{"url": comment_url, "body": "evidence payload"}]},
    )

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_GH_STATE_FILE"] = str(state_path)
    env["FAKE_GH_CLOSE_TRANSPORT_FAIL"] = "1"

    ac_results_file = _FIXTURES_DIR / "ac_results_all_applicable.json"
    script = _SCRIPTS_DIR / "finalize_no_diff_issue.py"

    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--issue-number",
            "1116",
            "--repo",
            TRUSTED_REPO,
            "--reason",
            "completed",
            "--ac-results-file",
            str(ac_results_file),
            "--existing-evidence-comment-url",
            comment_url,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout.strip())
    assert parsed["status"] == "ok"
    target = parsed["targets"]["1116"]
    assert target["target_status"] == "applied"
    assert "close" in target["completed_operations"]
    assert "close" not in target["incomplete_operations"]
    assert any("gh_issue_close_failed_rc_1" in e for e in target["errors"])
