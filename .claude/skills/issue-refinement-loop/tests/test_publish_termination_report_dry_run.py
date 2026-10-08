"""Public CLI regression for #2988: dry-run must never publish or signal.

The subprocess runs the actual publisher script. A child-only sitecustomize intercepts
ONLY its controlled executor and Task Context subprocess calls; an unknown command
raises instead of reaching a live service. The fake counts a POST-equivalent mutation
when --dry-run is absent (unless a stable marker is already published), so the
pre-fix regression cannot pass via an inert mock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[4]
_PUBLISHER = _ROOT / ".claude/skills/issue-refinement-loop/scripts/publish_termination_report.py"
_EVIDENCE = _ROOT / "artifacts" / "runtime-verification-2988"
_REPO = "squne121/loop-protocol"
_SHA = "a" * 64
_ISSUE = 2988
_BODY = "## refinement の完了\n\n検証用の隔離されたコメントです。\n"

# Written only under pytest's temporary sandbox; never imported by the parent.
# The real CLI remains a file-backed Python subprocess, never `python -c`.
_CHILD_INTERCEPTOR = r'''import json
import os
from pathlib import Path
import subprocess

root = Path(os.environ["HARNESS_PROJECT_ROOT"])
events_file = Path(os.environ["HARNESS_EVENTS"])
state_file = Path(os.environ["HARNESS_STATE"])
Path(os.environ["HARNESS_LOADED"]).write_text("loaded", encoding="utf-8")


def record(kind, **fields):
    with events_file.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": kind, **fields}, ensure_ascii=False) + "\n")


def fake_run(cmd, *args, **kwargs):
    if not isinstance(cmd, (tuple, list)) or len(cmd) < 2:
        raise AssertionError("unrecognized subprocess: network calls forbidden")
    argv = [str(part) for part in cmd]
    if Path(argv[1]).name == "controlled_skill_mutation_exec.py":
        assert argv[argv.index("--command-id") + 1] == "issue_comment.publish"
        assert argv[argv.index("--repo") + 1] == "squne121/loop-protocol"
        assert argv[argv.index("--issue-number") + 1] == "2988"
        input_file = (root / argv[argv.index("--input-file") + 1]).resolve()
        assert input_file.is_relative_to(root) and input_file.is_file()
        materialized = json.loads(input_file.read_text(encoding="utf-8"))
        assert materialized["issue_number"] == 2988
        assert materialized["marker"] in materialized["comment_body"]
        dry_run = "--dry-run" in argv
        published = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else []
        marker = materialized["marker"]
        if dry_run and os.environ.get("HARNESS_FAIL_EXEC") == "validation":
            code, detail = 1, "validation_failed"
        elif dry_run:
            code, detail = 0, "dry_run_ok"
        elif marker in published:
            code, detail = 0, "already_published"
        else:
            record("post_attempt", argv=argv)
            if os.environ.get("HARNESS_FAIL_EXEC") == "post":
                code, detail = 1, "post_failed"
            else:
                # A non-dry-run executor invocation MUST have a counted effect.
                record("mutation", method="POST", marker=marker)
                published.append(marker)
                state_file.write_text(json.dumps(published), encoding="utf-8")
                code, detail = 0, "created"
        record("executor", argv=argv, exit=code, status_detail=detail)
        return subprocess.CompletedProcess(argv, code,
            stdout=json.dumps({"status_detail": detail, "exit_code": code}),
            stderr="fake controlled executor failure" if code else "")
    if Path(argv[1]).name == "task_contextctl.py" and argv[2:] == ["signal", "apply"]:
        payload = json.loads(kwargs["input"])
        assert payload["signal_kind"] == "refinement_approved"
        assert payload["evidence"] == {"repo": "squne121/loop-protocol",
            "issue_number": 2988, "approved_body_sha256": "a" * 64}
        fail = os.environ.get("HARNESS_FAIL_SIGNAL") == "1"
        record("signal", argv=argv, exit=int(fail))
        return subprocess.CompletedProcess(argv, int(fail),
            stdout=json.dumps({"data": {"disposition": "deferred" if fail else "applied",
                "reason_code": "SIMULATED_FAILURE" if fail else "OK"}}), stderr="")
    raise AssertionError("unrecognized subprocess: network calls forbidden " + repr(argv))

subprocess.run = fake_run
'''


def _history_request() -> dict:
    return {
        "identity": {
            "loop_kind": "issue-refinement-loop",
            "phase": "review-complete",
            "source_issue_number": _ISSUE,
            "target_kind": "issue",
            "target_number": _ISSUE,
            "route_or_termination_reason": "completed",
            "reviewed_ref": _SHA,
        },
        "result": "履歴を記録します",
        "evidence_refs": ["https://github.com/squne121/loop-protocol/issues/2988"],
        "recommended_action": "検証を継続してください",
        "recommended_reason": "隔離された履歴の動作確認です",
        "impact_if_unaddressed": "履歴が失われます",
    }


def _run_cli(
    sandbox: Path,
    label: str,
    *,
    dry_run: bool = False,
    body_input: str = "file",
    body: str = _BODY,
    approved: bool = False,
    human_history: bool = False,
    exec_failure: str | None = None,
    signal_failure: bool = False,
) -> dict:
    sandbox.mkdir(parents=True, exist_ok=True)
    harness = sandbox / "interceptor"
    harness.mkdir(exist_ok=True)
    (harness / "sitecustomize.py").write_text(_CHILD_INTERCEPTOR, encoding="utf-8")
    events_file = sandbox / "events.jsonl"
    before = events_file.read_text(encoding="utf-8").splitlines() if events_file.exists() else []
    loaded = sandbox / "interceptor-loaded"
    loaded.unlink(missing_ok=True)

    argv = [sys.executable, str(_PUBLISHER), "--repo", _REPO]
    stdin = ""
    if human_history:
        request = sandbox / "human-history.json"
        request.write_text(json.dumps(_history_request(), ensure_ascii=False), encoding="utf-8")
        argv += ["--human-history-request-file", str(request)]
    else:
        argv += ["--issue-number", str(_ISSUE)]
        if body_input == "file":
            body_file = sandbox / "body.md"
            body_file.write_text(body, encoding="utf-8")
            argv += ["--body-file", str(body_file)]
        elif body_input == "missing":
            argv += ["--body-file", str(sandbox / "does-not-exist.md")]
        else:
            assert body_input == "stdin"
            stdin = body
        if approved:
            argv += ["--termination-reason", "approved", "--approved-body-sha256", _SHA]
    if dry_run:
        argv.append("--dry-run")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(harness)
    env["HARNESS_PROJECT_ROOT"] = str(_ROOT)
    env["HARNESS_EVENTS"] = str(events_file)
    env["HARNESS_STATE"] = str(sandbox / "remote-markers.json")
    env["HARNESS_LOADED"] = str(loaded)
    env["PUBLISH_ARTIFACT_DIR"] = str(sandbox / "publisher-artifacts")
    env["CLAUDE_CODE_SESSION_ID"] = "2988-regression-valid-session"
    env.pop("LOOP_ISSUE_NUMBER", None)
    env.pop("CONTROLLED_EXEC_MARKER", None)
    env.pop("HARNESS_FAIL_EXEC", None)
    env.pop("HARNESS_FAIL_SIGNAL", None)
    if exec_failure:
        env["HARNESS_FAIL_EXEC"] = exec_failure
    if signal_failure:
        env["HARNESS_FAIL_SIGNAL"] = "1"
    proc = subprocess.run(argv, input=stdin, text=True, capture_output=True,
                          cwd=_ROOT, env=env, check=False, timeout=30)
    assert loaded.read_text(encoding="utf-8") == "loaded", "subprocess interceptor did not load"
    event_lines = events_file.read_text(encoding="utf-8").splitlines() if events_file.exists() else []
    events = [json.loads(line) for line in event_lines[len(before):]]
    artifacts_dir = sandbox / "publisher-artifacts"
    artifacts = [json.loads(path.read_text(encoding="utf-8"))
                 for path in artifacts_dir.glob("*.json")] if artifacts_dir.exists() else []
    record = {
        "tested_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_ROOT, text=True).strip(),
        "cli_argv": argv,
        "cli_exit": proc.returncode,
        "cli_stdout": proc.stdout,
        "cli_stderr": proc.stderr,
        "executor_argv_exit": [{"argv": event["argv"], "exit": event["exit"],
                                "status_detail": event["status_detail"]}
                               for event in events if event["kind"] == "executor"],
        "mutation_count": sum(event["kind"] == "mutation" for event in events),
        "post_attempt_count": sum(event["kind"] == "post_attempt" for event in events),
        "signal_apply_count": sum(event["kind"] == "signal" for event in events),
        "events_in_order": events,
        "failure_artifacts": artifacts,
    }
    phase = os.environ.get("PUBLISH_TEST_EVIDENCE_PHASE", "local")
    assert phase in {"baseline", "final", "local"}
    evidence_path = _EVIDENCE / phase / f"{label}.json"
    assert evidence_path.resolve().is_relative_to(_ROOT.resolve())
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def _executor(record: dict) -> dict:
    assert len(record["executor_argv_exit"]) == 1, record
    return record["executor_argv_exit"][0]


def _no_side_effect(record: dict) -> None:
    assert record["mutation_count"] == 0, record
    assert record["post_attempt_count"] == 0, record
    assert record["signal_apply_count"] == 0, record
    assert "comment posted" not in record["cli_stderr"], record


def test_ac1_approved_dry_run_only_validates_and_positive_control_signals_after_post(tmp_path: Path):
    sandbox = tmp_path / "approved"
    dry = _run_cli(sandbox, "ac1-approved-dry-run", dry_run=True, approved=True)
    # Run the otherwise identical control from the same unposted remote state:
    # the broken baseline already mutates, so leaving its marker would make
    # the control a duplicate instead of proving the post-then-signal path.
    (sandbox / "remote-markers.json").unlink(missing_ok=True)
    control = _run_cli(sandbox, "ac1-approved-positive-control", approved=True)
    assert dry["cli_exit"] == 0, dry
    assert "--dry-run" in _executor(dry)["argv"], dry
    assert _executor(dry)["status_detail"] == "dry_run_ok", dry
    _no_side_effect(dry)
    assert "validation-only" in dry["cli_stderr"], dry
    assert control["cli_exit"] == 0, control
    assert "--dry-run" not in _executor(control)["argv"], control
    assert control["mutation_count"] == control["signal_apply_count"] == 1, control
    assert [entry["kind"] for entry in control["events_in_order"]] == [
        "post_attempt", "mutation", "executor", "signal"
    ], control


def test_ac2_human_history_dry_run_and_stable_identity_duplicate_noop(tmp_path: Path):
    sandbox = tmp_path / "human-history"
    dry = _run_cli(sandbox, "ac2-human-history-dry-run", dry_run=True, human_history=True)
    first = _run_cli(sandbox, "ac2-human-history-create", human_history=True)
    duplicate = _run_cli(sandbox, "ac2-human-history-duplicate", human_history=True)
    assert dry["cli_exit"] == 0 and json.loads(dry["cli_stdout"]) == {
        "status_detail": "dry_run_ok", "exit_code": 0
    }, dry
    assert "--dry-run" in _executor(dry)["argv"], dry
    _no_side_effect(dry)
    assert first["cli_exit"] == 0 and _executor(first)["status_detail"] == "created", first
    assert first["mutation_count"] == 1 and first["signal_apply_count"] == 0, first
    assert duplicate["cli_exit"] == 0 and _executor(duplicate)["status_detail"] == "already_published", duplicate
    _no_side_effect(duplicate)


def test_ac3_normal_approved_post_failure_and_signal_failure(tmp_path: Path):
    ok = _run_cli(tmp_path / "success", "ac3-approved-success", approved=True)
    failed_post = _run_cli(tmp_path / "failed-post", "ac3-failed-post", approved=True, exec_failure="post")
    failed_signal = _run_cli(tmp_path / "failed-signal", "ac3-failed-signal", approved=True, signal_failure=True)
    assert ok["cli_exit"] == 0 and ok["mutation_count"] == ok["signal_apply_count"] == 1, ok
    assert [event["kind"] for event in ok["events_in_order"]].index("signal") > [
        event["kind"] for event in ok["events_in_order"]
    ].index("executor"), ok
    assert failed_post["cli_exit"] != 0 and _executor(failed_post)["exit"] != 0, failed_post
    assert failed_post["post_attempt_count"] == 1 and failed_post["mutation_count"] == 0, failed_post
    assert failed_post["signal_apply_count"] == 0 and "comment posted" not in failed_post["cli_stderr"], failed_post
    assert any(artifact["reason_code"] == "gh_comment_failed" for artifact in failed_post["failure_artifacts"])
    assert failed_signal["cli_exit"] == 0 and failed_signal["mutation_count"] == 1, failed_signal
    assert failed_signal["signal_apply_count"] == 1, failed_signal
    assert any(artifact["reason_code"] == "task_context_signal_not_applied"
               for artifact in failed_signal["failure_artifacts"]), failed_signal


@pytest.mark.parametrize("body_input,body", [
    ("stdin", "  \n"), ("file", " \n"), ("missing", _BODY)
])
def test_ac4_existing_empty_or_missing_body_fails_without_dispatch(tmp_path: Path, body_input: str, body: str):
    result = _run_cli(tmp_path / body_input, f"ac4-empty-or-missing-{body_input}",
                      body_input=body_input, body=body, dry_run=True, approved=True)
    assert result["cli_exit"] != 0 and not result["executor_argv_exit"], result
    _no_side_effect(result)


def test_ac4_existing_body_file_normal_post_remains_available(tmp_path: Path):
    result = _run_cli(tmp_path / "normal-file", "ac4-normal-body-file", approved=True)
    assert result["cli_exit"] == 0 and "--dry-run" not in _executor(result)["argv"], result
    assert result["mutation_count"] == result["signal_apply_count"] == 1, result


def test_ac4_dry_run_stdin_validation_only_and_normal_post_distinct(tmp_path: Path):
    sandbox = tmp_path / "stdin"
    dry = _run_cli(sandbox, "ac4-dry-run-stdin", body_input="stdin", dry_run=True)
    normal = _run_cli(sandbox, "ac4-normal-stdin", body_input="stdin")
    assert dry["cli_exit"] == 0 and "--dry-run" in _executor(dry)["argv"], dry
    _no_side_effect(dry)
    assert "validation-only" in dry["cli_stderr"], dry
    assert normal["cli_exit"] == 0 and normal["mutation_count"] == 1, normal
    assert normal["signal_apply_count"] == 0 and "comment posted" in normal["cli_stderr"], normal


def test_ac4_dry_run_controlled_validation_failure_fails_closed(tmp_path: Path):
    result = _run_cli(tmp_path / "validation-failure", "ac4-dry-run-validation-failure",
                      dry_run=True, approved=True, exec_failure="validation")
    assert "--dry-run" in _executor(result)["argv"], result
    assert _executor(result)["exit"] != 0 and result["cli_exit"] != 0, result
    _no_side_effect(result)
    assert any(artifact["reason_code"] == "gh_comment_failed" for artifact in result["failure_artifacts"])
