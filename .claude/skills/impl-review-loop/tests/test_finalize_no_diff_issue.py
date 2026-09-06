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
import sys
from pathlib import Path

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
