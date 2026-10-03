"""AC8 regression: partial GraphQL snapshots cannot start cleanup."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_POST_MERGE_SCRIPTS = (
    Path(__file__).resolve().parents[2] / ".claude" / "skills" / "post-merge-cleanup" / "scripts"
)
sys.path.insert(0, str(_POST_MERGE_SCRIPTS))

import task_context_workflow_signal as post_merge_signal  # noqa: E402


def test_given_graphql_data_plus_top_level_errors_when_merge_signal_runs_then_it_never_applies_or_begins_cleanup(
    tmp_path, monkeypatch, capsys
):
    snapshot = {
        "data": {
            "repository": {
                "nameWithOwner": "squne121/loop-protocol",
                "pullRequest": {
                    "number": 21,
                    "merged": True,
                    "mergeCommit": {"oid": "b" * 40},
                    "closingIssuesReferences": {"nodes": [{"number": 20, "repository": {"nameWithOwner": "squne121/loop-protocol"}}]},
                },
            }
        },
        "errors": [{"message": "partial resolver failure"}],
    }
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")

    def fail_task_context_mutation(*args, **kwargs):
        raise AssertionError("partial GraphQL data must not apply a signal or begin cleanup")

    monkeypatch.setattr(post_merge_signal, "_run", fail_task_context_mutation)

    assert (
        post_merge_signal.main(
            [
                "--snapshot-file",
                str(snapshot_file),
                "--issue-number",
                "20",
                "--pr-number",
                "21",
                "--phase",
                "merged",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "disposition": "deferred",
        "reason_code": "RELATION_UNAVAILABLE",
    }


def _merged_snapshot() -> dict:
    return {
        "data": {
            "repository": {
                "nameWithOwner": "squne121/loop-protocol",
                "pullRequest": {
                    "number": 21,
                    "merged": True,
                    "mergeCommit": {"oid": "b" * 40},
                    "closingIssuesReferences": {"nodes": [{"number": 20, "repository": {"nameWithOwner": "squne121/loop-protocol"}}]},
                },
            }
        }
    }


def _cleanup_report(*, status: str = "ok", human_review_required: bool = False) -> dict:
    return {
        "status": status,
        "generated_at": "2026-01-01T00:00:00Z",
        "generated_by": "post-merge-cleanup-worker",
        "human_review_required": human_review_required,
        "cleaned_branches": [],
        "cleaned_worktrees": [],
        "unresolved_cleanup_items": [],
        "parent_issue_status": {
            "parent_issue_number": 1,
            "all_children_closed": False,
            "recommended_action": "keep_open",
        },
        "superseded_prs": [],
        "follow_up_issue_requests": [],
        "stash_restored": "n/a",
        "stash_entry_ref": None,
        "warnings": [],
        "errors": [],
    }


def test_given_cross_repository_same_number_closing_relation_when_merged_evidence_is_derived_then_it_is_rejected():
    snapshot = _merged_snapshot()
    snapshot["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"][0]["repository"] = {
        "nameWithOwner": "owner/other"
    }

    evidence, reason = post_merge_signal._merged_evidence(snapshot, 20, 21)

    assert (evidence, reason) == (None, "RELATION_ISSUE_MISMATCH")


def _must_not_apply(*_args, **_kwargs):
    raise AssertionError("non-final cleanup evidence must not apply a signal")


def test_given_no_final_success_receipt_when_cleanup_completion_runs_then_it_never_applies_signal(
    tmp_path, monkeypatch, capsys
):
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(json.dumps(_merged_snapshot()), encoding="utf-8")
    monkeypatch.setattr(post_merge_signal, "_run", _must_not_apply)

    assert post_merge_signal.main([
        "--snapshot-file", str(snapshot_file), "--issue-number", "20", "--pr-number", "21", "--phase", "completed"
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "disposition": "deferred",
        "reason_code": "CLEANUP_FINAL_SUCCESS_RECEIPT_REQUIRED",
    }


def test_given_partial_failed_or_human_review_receipt_when_cleanup_completion_runs_then_it_never_applies_signal(
    tmp_path, monkeypatch, capsys
):
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(json.dumps(_merged_snapshot()), encoding="utf-8")
    monkeypatch.setattr(post_merge_signal, "_run", _must_not_apply)

    for index, report in enumerate((
        _cleanup_report(status="partial"),
        _cleanup_report(status="failed"),
        _cleanup_report(human_review_required=True),
    )):
        receipt_file = tmp_path / f"receipt-{index}.json"
        receipt_file.write_text(json.dumps(report), encoding="utf-8")
        assert post_merge_signal.main([
            "--snapshot-file", str(snapshot_file), "--issue-number", "20", "--pr-number", "21", "--phase", "completed",
            "--cleanup-receipt-file", str(receipt_file),
        ]) == 0
        assert json.loads(capsys.readouterr().out) == {
            "disposition": "deferred",
            "reason_code": "CLEANUP_NOT_FINAL_SUCCESS",
        }


def test_given_explicit_origin_session_id_when_run_invokes_ctl_then_child_env_overrides_claude_code_session_id(
    monkeypatch,
):
    """Issue #2719 AC3: mirrors `.claude/hooks/task_context/ctl_client.py`'s
    explicit `child_env["CLAUDE_CODE_SESSION_ID"] = origin_session_id`
    override -- an ambient, differently-valued `CLAUDE_CODE_SESSION_ID`
    inherited by this adapter's own process must never leak into the
    `task_contextctl.py` child process when a caller-supplied
    `origin_session_id` is given."""
    captured: dict = {}

    def fake_subprocess_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"data": {"disposition": "applied"}}), stderr="")

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ambient-session")
    monkeypatch.setattr(post_merge_signal.subprocess, "run", fake_subprocess_run)

    result = post_merge_signal._run(["signal", "apply"], {"k": "v"}, origin_session_id="override-session")

    assert result == {"disposition": "applied"}
    assert captured["env"] is not None
    assert captured["env"]["CLAUDE_CODE_SESSION_ID"] == "override-session"


def test_given_no_origin_session_id_when_run_invokes_ctl_then_child_env_falls_back_to_ambient_value(monkeypatch):
    captured: dict = {}

    def fake_subprocess_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"data": {"disposition": "applied"}}), stderr="")

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ambient-session")
    monkeypatch.setattr(post_merge_signal.subprocess, "run", fake_subprocess_run)

    post_merge_signal._run(["signal", "apply"], {"k": "v"})

    assert captured["env"]["CLAUDE_CODE_SESSION_ID"] == "ambient-session"


def test_given_origin_session_id_flag_when_main_runs_merged_phase_then_it_is_forwarded_to_run(
    tmp_path, monkeypatch, capsys
):
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(json.dumps(_merged_snapshot()), encoding="utf-8")
    captured_calls: list = []

    def fake_run(argv, body, *, origin_session_id=None):
        captured_calls.append(origin_session_id)
        return {"disposition": "deferred", "reason_code": "STUBBED"}

    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(post_merge_signal, "_run", fake_run)

    assert (
        post_merge_signal.main(
            [
                "--snapshot-file",
                str(snapshot_file),
                "--issue-number",
                "20",
                "--pr-number",
                "21",
                "--phase",
                "merged",
                "--origin-session-id",
                "flag-session",
            ]
        )
        == 0
    )
    assert captured_calls == ["flag-session"]


# ===========================================================================
# Issue #2878: closing-relation-independent binding via `non_closing_authority`
# ===========================================================================

import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import os  # noqa: E402

import pytest  # noqa: E402

import task_context_workflow_signals as workflow_signals  # noqa: E402
from retroactive_claim_support import (  # noqa: E402
    ISSUE,
    OID,
    PR,
    REPO,
    SESSION,
    base_args,
    build_origin,
    dump_db,
    local_only_args,
    merged_args,
    merged_snapshot,
    read_all,
    recover_args,
    run_adapter,
    write_snapshot,
)
from workflow_signal_test_support import implementation_payload  # noqa: E402

PR_BODY = "## Summary\n\n日本語の本文\n\nRefs #20\n"
BODY_SHA = hashlib.sha256(PR_BODY.encode("utf-8")).hexdigest()


def _authority(**overrides):
    authority = {
        "decision": "nonclosing_required",
        "level": "A2",
        "reason_code": "a2_contract_deferred",
        "repo": REPO,
        "issue_number": ISSUE,
        "pr_number": PR,
        "pr_body_sha256": BODY_SHA,
    }
    authority.update(overrides)
    return authority


def _non_closing_snapshot(*, authority="default", body="default", nodes=None, **kwargs):
    snapshot = merged_snapshot(
        nodes=[] if nodes is None else nodes,
        body=PR_BODY if body == "default" else None,
        **kwargs,
    )
    if body not in ("default", None):
        snapshot["data"]["repository"]["pullRequest"]["body"] = body
    if authority == "default":
        authority = _authority()
    if authority is not None:
        snapshot["non_closing_authority"] = authority
    return snapshot


def _seed_recorded_implementation(conn):
    """A Task whose origin session already observed the implementation PR (claims issue + PR)."""
    origin = build_origin(conn, active_kind="implementation")
    applied = workflow_signals.apply_workflow_signal(conn, implementation_payload(), origin_session_id=SESSION)
    assert applied["disposition"] == "applied", applied
    return origin


def _forbidden_tools(tmp_path):
    """`gh` / `git` on PATH that record any call: the adapter must never reach GitHub."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    marker = tmp_path / "forbidden-tool-called"
    for tool in ("gh", "git"):
        script = fake_bin / tool
        script.write_text(f"#!/bin/sh\necho {tool} >> {marker}\nexit 1\n", encoding="utf-8")
        script.chmod(0o755)
    return {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"}, marker


def test_given_non_closing_authority_when_merged_phase_then_binds_without_close_authority(
    tmp_path, state_root, conn
):
    origin = _seed_recorded_implementation(conn)
    conn.close()
    env, marker = _forbidden_tools(tmp_path)
    snapshot = write_snapshot(tmp_path, _non_closing_snapshot())

    selected = run_adapter(merged_args(snapshot), state_root=state_root, extra_env=env)

    assert (selected["disposition"], selected["reason_code"]) == ("selected", "CLEANUP_STARTED")
    assert selected["task_id"] == origin["task"]["id"]
    # the adapter never closes anything and never grants a close authority
    assert not marker.exists()
    assert not any("close" in key.lower() for key in selected)
    merge_events = read_all("SELECT * FROM events WHERE event_type = 'workflow:pr_merged_observed'")
    assert [row["task_id"] for row in merge_events] == [origin["task"]["id"]]
    assert json.loads(merge_events[0]["metadata_json"])["merge_commit_oid"] == OID

    # A1 binds the same way, and `completed` shares the identical `_merged_evidence` decision
    a1_snapshot = write_snapshot(
        tmp_path, _non_closing_snapshot(authority=_authority(level="A1", reason_code="a1_explicit_decision")), name="a1.json"
    )
    resumed = run_adapter(merged_args(a1_snapshot), state_root=state_root, extra_env=env)
    assert (resumed["disposition"], resumed["reason_code"]) == ("selected", "CLEANUP_ALREADY_SELECTED")
    receipt = tmp_path / "receipt.json"
    from retroactive_claim_support import cleanup_report

    receipt.write_text(json.dumps(cleanup_report()), encoding="utf-8")
    completed = run_adapter(
        [*base_args(a1_snapshot, "completed"), "--cleanup-receipt-file", str(receipt), "--origin-session-id", SESSION],
        state_root=state_root,
        extra_env=env,
    )
    assert completed["disposition"] == "applied"
    assert not marker.exists()


INVALID_AUTHORITIES = {
    "attestation_missing": None,
    "decision_closing_required": _authority(decision="closing_required", level="A3", reason_code="a3_close_ready"),
    "decision_fail_closed": _authority(decision="fail_closed", level=None, reason_code="facts_invalid"),
    "level_a3": _authority(level="A3", reason_code="a3_close_ready"),
    "level_closed": _authority(level="CLOSED", reason_code="issue_closed"),
    "level_missing": _authority(level=None),
    "level_list": _authority(level=[]),
    "level_object": _authority(level={}),
    "level_bool": _authority(level=True),
    "level_number": _authority(level=5),
    "other_issue_number": _authority(issue_number=ISSUE + 1),
    "string_issue_number": _authority(issue_number=str(ISSUE)),
    "other_pr_number": _authority(pr_number=PR + 1),
    "bool_pr_number": _authority(pr_number=True),
    "other_repo": _authority(repo="owner/other"),
    "hash_mismatch": _authority(pr_body_sha256="0" * 64),
    "hash_of_crlf_normalized_body": _authority(pr_body_sha256=hashlib.sha256(b"x").hexdigest()),
    "hash_not_a_string": _authority(pr_body_sha256=5),
    "extra_key": {**_authority(), "effective_kind": "non-closing"},
    "missing_key": {key: value for key, value in _authority().items() if key != "reason_code"},
    "not_an_object": ["decision"],
}


def test_given_invalid_non_closing_authority_when_merged_phase_then_rejected_with_zero_writes(
    tmp_path, state_root, conn
):
    _seed_recorded_implementation(conn)
    conn.close()
    before = dump_db()
    env, marker = _forbidden_tools(tmp_path)

    cases = {name: _non_closing_snapshot(authority=authority) for name, authority in INVALID_AUTHORITIES.items()}
    cases["body_missing"] = _non_closing_snapshot(body=None)
    cases["body_not_a_string"] = _non_closing_snapshot(body=5)
    for name, snapshot in cases.items():
        for phase in ("merged", "completed"):
            args = (
                merged_args(write_snapshot(tmp_path, snapshot, name=f"{name}.json"))
                if phase == "merged"
                else [*base_args(write_snapshot(tmp_path, snapshot, name=f"{name}.json"), "completed"),
                      "--origin-session-id", SESSION]
            )
            result = run_adapter(args, state_root=state_root, extra_env=env)
            assert result == {"disposition": "deferred", "reason_code": "RELATION_ISSUE_MISMATCH"}, (name, phase)
            assert dump_db() == before, (name, phase)  # zero writes
    assert not marker.exists()


def test_given_closing_node_when_merged_phase_then_legacy_behavior_unchanged(tmp_path, state_root, conn):
    _seed_recorded_implementation(conn)
    conn.close()
    before = dump_db()

    # another Issue's closing node is rejected and never falls through to the non-closing rule
    other = _non_closing_snapshot(nodes=[{"number": ISSUE + 1, "repository": {"nameWithOwner": REPO}}])
    result = run_adapter(merged_args(write_snapshot(tmp_path, other, name="other.json")), state_root=state_root)
    assert result == {"disposition": "deferred", "reason_code": "RELATION_ISSUE_MISMATCH"}
    two = _non_closing_snapshot(
        nodes=[
            {"number": ISSUE, "repository": {"nameWithOwner": REPO}},
            {"number": ISSUE + 1, "repository": {"nameWithOwner": REPO}},
        ]
    )
    result = run_adapter(merged_args(write_snapshot(tmp_path, two, name="two.json")), state_root=state_root)
    assert result == {"disposition": "deferred", "reason_code": "RELATION_ISSUE_MISMATCH"}
    cross_repo = _non_closing_snapshot(nodes=[{"number": ISSUE, "repository": {"nameWithOwner": "owner/other"}}])
    result = run_adapter(merged_args(write_snapshot(tmp_path, cross_repo, name="cross.json")), state_root=state_root)
    assert result == {"disposition": "deferred", "reason_code": "RELATION_ISSUE_MISMATCH"}
    assert dump_db() == before

    # the matching closing node keeps working exactly as before, with or without any attestation
    for name, authority in (("legacy", None), ("with_authority", _authority())):
        snapshot = merged_snapshot(body=PR_BODY)
        if authority is not None:
            snapshot["non_closing_authority"] = authority
        result = run_adapter(merged_args(write_snapshot(tmp_path, snapshot, name=f"{name}.json")), state_root=state_root)
        assert result["disposition"] in {"selected", "duplicate_noop"}, (name, result)


def test_given_recover_or_local_only_phase_when_non_closing_authority_then_still_rejected(
    tmp_path, state_root, conn
):
    _seed_recorded_implementation(conn)
    conn.close()
    before = dump_db()
    snapshot = write_snapshot(tmp_path, _non_closing_snapshot())

    recovered = run_adapter(recover_args(snapshot), state_root=state_root)
    local_only = run_adapter(local_only_args(snapshot), state_root=state_root)

    assert recovered == {"disposition": "deferred", "reason_code": "RELATION_ISSUE_MISMATCH"}
    assert local_only == {"disposition": "deferred", "reason_code": "RELATION_ISSUE_MISMATCH"}
    assert dump_db() == before  # zero writes

    # and the same snapshot binds in `merged`, proving the phase restriction (not the data) rejected it
    merged = run_adapter(merged_args(snapshot), state_root=state_root)
    assert merged["disposition"] == "selected"


# --- producer side -------------------------------------------------------------------------------

OPEN_PR_PATH = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "open-pr" / "scripts" / "open_pr.py"


def _load_open_pr():
    spec = importlib.util.spec_from_file_location("open_pr_for_non_closing_adapter_tests", OPEN_PR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


A2_ISSUE_BODY = (
    "## Runtime Verification Applicability\n\n"
    "- decision: deferred\n"
    "- reason: merge 後の live evidence\n"
    "- deferred_destination:\n"
    "    - destination_type: phase\n"
    "    - destination_ref: post-merge-live-evidence\n"
    "- deferred_verification_condition: merge 後に取得する\n"
)
A3_ISSUE_BODY = "## Runtime Verification Applicability\n\ndecision: not_applicable\nreason: x\n"
LIVE_REFS_BODY = "## Summary\n\n日本語の本文\n\nRefs #20\n"
A1_COMMENT_URL = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-99"
A1_COMMENT = {
    "html_url": A1_COMMENT_URL,
    "id": 99,
    "issue_url": f"https://api.github.com/repos/{REPO}/issues/{ISSUE}",
    "author_association": "OWNER",
    "body": f"REFERENCE_DECISION_V1: nonclosing issue=#{ISSUE}\n",
}


def _emit(monkeypatch, open_pr, *, live_body, issue_state="OPEN", issue_body=A2_ISSUE_BODY, nodes=None,
          comment=None, pr_body_missing=False):
    """Run `emit_implementation_pr_observed` with GitHub reads faked and `task_contextctl` captured."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-1")
    snapshot = {
        "data": {
            "repository": {
                "nameWithOwner": REPO,
                "pullRequest": {"number": PR, "closingIssuesReferences": {"nodes": nodes or []}},
            }
        }
    }
    real_subprocess_run = open_pr.subprocess.run
    signals: list[dict] = []
    gh_calls: list[tuple] = []

    def fake_run_gh(*args, **kwargs):
        gh_calls.append(args)
        if args[:2] == ("api", "graphql"):
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(snapshot), stderr="")
        if args[0] == "api" and args[1] == f"repos/{REPO}/pulls/{PR}":
            if pr_body_missing:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"body": live_body}), stderr="")
        if args[0] == "api" and "/issues/comments/" in args[1]:
            if comment is None:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(comment), stderr="")
        raise AssertionError(f"unexpected gh call: {args}")

    def fake_subprocess_run(cmd, **kwargs):
        if any("validate_pr_body.py" in str(part) for part in cmd):
            return real_subprocess_run(cmd, **kwargs)  # the real evaluator entrypoint
        signals.append(json.loads(kwargs["input"]))
        stdout = json.dumps({"data": {"disposition": "applied", "reason_code": "OBSERVED"}})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(open_pr, "run_gh", fake_run_gh)
    monkeypatch.setattr(open_pr.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda repo, issue: issue_state)
    monkeypatch.setattr(open_pr, "get_linked_issue_body", lambda repo, issue: issue_body)
    outcome = open_pr.emit_implementation_pr_observed(repo=REPO, pr_number=PR, linked_issue=ISSUE)
    return outcome, signals, gh_calls


def test_given_non_closing_pr_when_open_pr_observed_then_signal_emitted_only_when_authority_binds_to_live_body(
    monkeypatch,
):
    open_pr = _load_open_pr()
    expected_signal = {
        "signal_kind": "implementation_pr_observed",
        "source": "open-pr",
        "source_schema_version": "v1",
        "evidence": {"repo": REPO, "issue_number": ISSUE, "pr_number": PR},  # existing wire, unchanged
    }

    # A2 (post-merge live evidence) + a live body that really carries Refs -> emitted
    outcome, signals, _ = _emit(monkeypatch, open_pr, live_body=LIVE_REFS_BODY)
    assert outcome == ("applied", "OBSERVED") and signals == [expected_signal]

    # A1 explicit decision (live comment fetched) overrides an A3 Issue -> emitted
    a1_body = LIVE_REFS_BODY + f"\nReference-Decision: {A1_COMMENT_URL}\n"
    outcome, signals, gh_calls = _emit(
        monkeypatch, open_pr, live_body=a1_body, issue_body=A3_ISSUE_BODY, comment=A1_COMMENT
    )
    assert outcome == ("applied", "OBSERVED") and signals == [expected_signal]
    assert any("/issues/comments/99" in str(call) for call in gh_calls)

    not_emitted = ("deferred", "NO_LINK")
    # A3 close-ready Issue: the PR should close it, so a non-closing PR is not observed
    assert _emit(monkeypatch, open_pr, live_body=LIVE_REFS_BODY, issue_body=A3_ISSUE_BODY)[:2] == (not_emitted, [])
    # CLOSED Issue has no authority to bind
    assert _emit(monkeypatch, open_pr, live_body=LIVE_REFS_BODY, issue_state="CLOSED")[:2] == (not_emitted, [])
    # fail_closed: A1 present but its comment cannot be fetched (never demoted to A2)
    assert _emit(monkeypatch, open_pr, live_body=a1_body, comment=None)[:2] == (not_emitted, [])
    # fail_closed: unresolved applicability section
    assert _emit(monkeypatch, open_pr, live_body=LIVE_REFS_BODY, issue_body="## Outcome\n")[:2] == (not_emitted, [])
    # authority cannot bind: the live body does not carry Refs for this Issue (the local body is irrelevant)
    assert _emit(monkeypatch, open_pr, live_body="## Summary\n\nRefs #999\n")[:2] == (not_emitted, [])
    assert _emit(monkeypatch, open_pr, live_body=LIVE_REFS_BODY + "\nCloses #20\n")[:2] == (not_emitted, [])
    # authority unobtainable: live body cannot be fetched
    assert _emit(monkeypatch, open_pr, live_body=LIVE_REFS_BODY, pr_body_missing=True)[:2] == (not_emitted, [])
    # a closing node keeps the legacy rule: another Issue's node is a conflict, never a non-closing bind
    outcome, signals, _ = _emit(
        monkeypatch,
        open_pr,
        live_body=LIVE_REFS_BODY,
        nodes=[{"number": ISSUE + 1, "repository": {"nameWithOwner": REPO}}],
    )
    assert (outcome, signals) == (("conflict", "RELATION_ISSUE_MISMATCH"), [])


def test_given_three_emission_paths_when_open_pr_runs_then_each_uses_the_live_body(monkeypatch, tmp_path):
    """New PR / existing PR / canonical existing PR all go through `emit_implementation_pr_observed`,
    which evaluates the PR's live body (never the local `final_body`)."""
    open_pr = _load_open_pr()
    calls: list[dict] = []

    def fake_emit(**kwargs):
        calls.append(kwargs)
        return "deferred", "NO_LINK"

    body_file = tmp_path / "body.md"
    body_file.write_text("## Summary\n\nlocal body only\n", encoding="utf-8")
    monkeypatch.setattr(open_pr, "get_linked_issue_state", lambda *_a: "OPEN")
    monkeypatch.setattr(open_pr, "get_linked_issue_body", lambda *_a: A2_ISSUE_BODY)
    monkeypatch.setattr(open_pr, "resolve_changed_paths", lambda *_a: ["src/x.ts"])
    monkeypatch.setattr(open_pr, "_run_pr_body_validator", lambda *_a: {"status": "pass"})
    monkeypatch.setattr(open_pr, "_run_japanese_content_validator", lambda *_a, **_k: {"status": "pass"})
    monkeypatch.setattr(open_pr, "append_implementation_scope_coverage", lambda body, **_k: body)
    monkeypatch.setattr(open_pr, "emit_implementation_pr_observed", fake_emit)
    monkeypatch.setattr(open_pr, "create_pr", lambda *_a: f"https://github.com/{REPO}/pull/{PR}")
    base = ["--pr-title", "t", "--linked-issue", str(ISSUE), "--publish", "yes", "--pr-body-file", str(body_file),
            "--branch", "b"]

    monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: {"number": PR, "url": "u"})
    assert open_pr.main([*base, "--repo", REPO]) == 0  # existing PR
    monkeypatch.setattr(open_pr, "find_existing_pr", lambda repo, branch: None)
    monkeypatch.setattr(open_pr, "resolve_canonical_repository", lambda repo: REPO)
    assert open_pr.main([*base, "--repo", REPO]) == 0  # new PR
    monkeypatch.setattr(
        open_pr, "find_existing_pr", lambda repo, branch: {"number": PR, "url": "u"} if repo == REPO else None
    )
    assert open_pr.main([*base, "--repo", REPO.upper()]) == 0  # canonical existing PR
    assert [call["pr_number"] for call in calls] == [PR, PR, PR]
    for call in calls:
        # none of the paths hands a locally derived authority or body to the emitter
        assert set(call) == {"repo", "pr_number", "linked_issue"}


# --- producer / consumer parity ------------------------------------------------------------------

PARITY_CASES = {
    "valid_a1": ({}, _authority(level="A1", reason_code="a1_explicit_decision"), True),
    "valid_a2": ({}, _authority(), True),
    "repo_case_only_difference": ({}, _authority(repo=REPO.upper()), True),
    "crlf_body_hashed_as_exact_bytes": (
        {"body": "a\r\nRefs #20\r\n"},
        _authority(pr_body_sha256=hashlib.sha256(b"a\r\nRefs #20\r\n").hexdigest()),
        True,
    ),
    "crlf_body_hash_of_normalized_text": (
        {"body": "a\r\nRefs #20\r\n"},
        _authority(pr_body_sha256=hashlib.sha256(b"a\nRefs #20\n").hexdigest()),
        False,
    ),
    "level_a3": ({}, _authority(level="A3", reason_code="a3_close_ready"), False),
    "level_closed": ({}, _authority(level="CLOSED", reason_code="issue_closed"), False),
    "level_list": ({}, _authority(level=[]), False),
    "level_object": ({}, _authority(level={}), False),
    "level_bool": ({}, _authority(level=True), False),
    "level_null": ({}, _authority(level=None), False),
    "level_number": ({}, _authority(level=5), False),
    "authority_missing": ({}, None, False),
    "hash_mismatch": ({}, _authority(pr_body_sha256="f" * 64), False),
    "other_repo": ({}, _authority(repo="owner/other"), False),
    "other_issue": ({}, _authority(issue_number=ISSUE + 1), False),
    "other_pr": ({}, _authority(pr_number=PR + 1), False),
    "decision_closing_required": ({}, _authority(decision="closing_required"), False),
    "decision_fail_closed": ({}, _authority(decision="fail_closed", level=None), False),
    "body_missing": ({"body": None}, _authority(), False),
    "body_not_a_string": ({"body": 7}, _authority(), False),
    "extra_key": ({}, {**_authority(), "body_verdict": "valid"}, False),
    "same_issue_closing_node": (
        {"nodes": [{"number": ISSUE, "repository": {"nameWithOwner": REPO}}]},
        _authority(),
        True,
    ),
    "same_issue_closing_node_without_authority": (
        {"nodes": [{"number": ISSUE, "repository": {"nameWithOwner": REPO}}]},
        None,
        True,
    ),
    "other_issue_closing_node_with_valid_authority": (
        {"nodes": [{"number": ISSUE + 1, "repository": {"nameWithOwner": REPO}}]},
        _authority(),
        False,
    ),
}


def test_given_fixture_matrix_when_classified_then_producer_and_consumer_agree():
    open_pr = _load_open_pr()
    for name, (shape, authority, expect_accept) in PARITY_CASES.items():
        nodes = shape.get("nodes", [])
        snapshot = merged_snapshot(nodes=nodes)
        pull_request = snapshot["data"]["repository"]["pullRequest"]
        pull_request["body"] = PR_BODY
        if "body" in shape:
            if shape["body"] is None:
                del pull_request["body"]
            else:
                pull_request["body"] = shape["body"]
        if authority is not None:
            snapshot["non_closing_authority"] = authority

        producer_disposition, producer_reason, evidence = open_pr.classify_closing_issue_relation(
            snapshot, ISSUE, None, authority
        )
        producer_accepts = producer_reason not in {"NO_LINK", "RELATION_ISSUE_MISMATCH"}
        consumer_evidence, consumer_reason = post_merge_signal._merged_evidence(
            snapshot, ISSUE, PR, allow_non_closing=True
        )
        consumer_accepts = consumer_evidence is not None

        assert producer_accepts == consumer_accepts == expect_accept, (
            name,
            (producer_disposition, producer_reason),
            consumer_reason,
        )
        if expect_accept:
            assert evidence == {"repo": REPO, "issue_number": ISSUE, "pr_number": PR}
        else:
            assert evidence is None and consumer_evidence is None
            assert producer_reason in {"NO_LINK", "RELATION_ISSUE_MISMATCH"}
            assert consumer_reason == "RELATION_ISSUE_MISMATCH"
        if not nodes:
            # without the explicit opt-in (recover / local-only) a non-closing PR is never accepted
            default_evidence, default_reason = post_merge_signal._merged_evidence(snapshot, ISSUE, PR)
            assert (default_evidence, default_reason) == (None, "RELATION_ISSUE_MISMATCH"), name


def _graphql_balance(query: str) -> tuple[int, int, int, int]:
    return query.count("{"), query.count("}"), query.count("("), query.count(")")


def test_given_production_graphql_query_when_emitted_then_the_captured_query_is_well_formed(monkeypatch):
    """The query is captured from what `emit_implementation_pr_observed` really sent to `gh`
    (a fake `gh` that succeeds for any query would hide a malformed one, #2825)."""
    open_pr = _load_open_pr()
    _outcome, _signals, gh_calls = _emit(monkeypatch, open_pr, live_body=LIVE_REFS_BODY)
    graphql_calls = [call for call in gh_calls if call[:2] == ("api", "graphql")]
    assert len(graphql_calls) == 1
    call = graphql_calls[0]
    query_args = [arg for arg in call if isinstance(arg, str) and arg.startswith("query=")]
    assert len(query_args) == 1
    query = query_args[0][len("query="):]

    opens, closes, lparens, rparens = _graphql_balance(query)
    assert opens == closes and lparens == rparens, (opens, closes, lparens, rparens)
    depth = 0
    for char in query:
        depth += {"{": 1, "}": -1}.get(char, 0)
        assert depth >= 0, "closing brace before its opening brace"
    assert depth == 0 and query.rstrip().endswith("}")

    # the existing bounded relation shape and the owner / name / number bindings are kept
    assert "closingIssuesReferences(first:2," in query
    assert "repository(owner:$owner,name:$name)" in query
    assert "pullRequest(number:$number)" in query
    for field in ("nameWithOwner", "number", "closingIssuesReferences", "nodes", "repository"):
        assert field in query, field
    assert "query($owner:String!,$name:String!,$number:Int!)" in query
    assert "owner=" + REPO.split("/")[0] in call and "name=" + REPO.split("/")[1] in call
    assert f"number={PR}" in call


def test_given_unbalanced_query_when_checked_then_the_balance_helper_detects_it():
    # guards the regression helper itself: the pre-fix query (6 `{` / 5 `}`) must not look balanced
    broken = (
        "query($a:Int!){repository(a:$a){pullRequest(a:$a){n "
        "closingIssuesReferences(first:2){nodes{number repository{nameWithOwner}}}}}"
    )
    opens, closes, _l, _r = _graphql_balance(broken)
    assert opens != closes
