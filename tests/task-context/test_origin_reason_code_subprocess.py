"""Issue #2719 AC4: production-shaped regression coverage.

Actually invokes ``task_contextctl.py`` (and, one level up, the
post-merge-cleanup adapter ``task_context_workflow_signal.py``) via real
``subprocess.run`` -- not mock-only -- so this suite would have caught the
PR #2697 lesson referenced by the Issue body: a unit/fixture test can stay
green while the actual production subprocess path is disconnected.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import task_context_config as config
import task_context_db as db
import task_context_service as service

_REPO_ROOT = pathlib.Path(config.__file__).resolve().parents[2]
_CTL = _REPO_ROOT / "scripts" / "task-context" / "task_contextctl.py"
_ADAPTER = _REPO_ROOT / ".claude" / "skills" / "post-merge-cleanup" / "scripts" / "task_context_workflow_signal.py"

REPO = "squne121/loop-protocol"
SHA40 = "b" * 40


def _run_ctl(argv: list[str], body: dict, *, state_root, claude_session_id: str | None, timeout: float = 15) -> dict:
    env = dict(os.environ)
    env["LOOP_TASK_CONTEXT_STATE_ROOT"] = str(state_root)
    if claude_session_id is None:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    else:
        env["CLAUDE_CODE_SESSION_ID"] = claude_session_id
    proc = subprocess.run(
        [sys.executable, str(_CTL), *argv],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    assert proc.returncode in (0, 1), proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, proc.stderr
    return json.loads(lines[-1])


def _merged_snapshot(*, issue_number: int = 20, pr_number: int = 21) -> dict:
    return {
        "data": {
            "repository": {
                "nameWithOwner": REPO,
                "pullRequest": {
                    "number": pr_number,
                    "merged": True,
                    "mergeCommit": {"oid": SHA40},
                    "closingIssuesReferences": {
                        "nodes": [{"number": issue_number, "repository": {"nameWithOwner": REPO}}]
                    },
                },
            }
        }
    }


def _bind_ended_origin(conn, *, session_id: str) -> dict:
    """Create a task/activity/binding/ExecutionRun and then end the run --
    a real, previously-open managed operator run whose origin is now
    diagnosably `origin_run_ended`."""
    task = service.create_task(conn, title="subprocess-origin-run-ended")
    activity = service.transition_activity(conn, task["id"], "implementation")
    binding = service.create_binding(conn)
    run = service.start_execution_run(
        conn,
        run_kind="native_operator",
        task_id=task["id"],
        activity_id=activity["id"],
        binding_id=binding["id"],
        claude_session_id=session_id,
    )
    service.set_binding_session(conn, binding["id"], session_id, execution_run_id=run["id"])
    service.end_execution_run(conn, run["id"])
    return {"task": task, "activity": activity, "binding": binding, "run": run}


def test_given_ended_run_origin_when_signal_apply_runs_via_subprocess_then_reason_code_is_persisted(
    state_root, conn
):
    origin = _bind_ended_origin(conn, session_id="ctl-subprocess-ended")
    conn.close()

    payload = {
        "signal_kind": "implementation_pr_observed",
        "source": "open-pr",
        "source_schema_version": "v1",
        "evidence": {"repo": REPO, "issue_number": 30, "pr_number": 31},
    }

    result = _run_ctl(
        ["signal", "apply"], payload, state_root=state_root, claude_session_id="ctl-subprocess-ended"
    )
    assert result["data"] == {"disposition": "deferred", "reason_code": "unbound"}

    readback = db.connect(config.db_path())
    try:
        row = readback.execute(
            "SELECT * FROM events WHERE event_type = 'workflow:origin_resolution_failed'"
        ).fetchone()
        assert row is not None
        assert row["execution_run_id"] == origin["run"]["id"]
        assert row["task_id"] == origin["task"]["id"]
        metadata = json.loads(row["metadata_json"])
        assert metadata["reason_code"] == "origin_run_ended"
        assert metadata["signal_kind"] == "implementation_pr_observed"
        assert metadata["source"] == "open-pr"
    finally:
        readback.close()


def test_given_mismatched_ambient_env_when_adapter_subprocess_runs_then_it_forwards_the_explicit_override(
    tmp_path, state_root, conn
):
    """Issue #2719 AC3 (production-shaped): the post-merge-cleanup adapter
    is invoked as a *real* subprocess with an ambient
    `CLAUDE_CODE_SESSION_ID` that deliberately does NOT match the real
    origin, plus an explicit `--origin-session-id` naming the real origin.
    Before the AC3 fix, `_run()`'s `subprocess.run()` call passed no `env=`
    at all, so the grandchild `task_contextctl.py` process would have
    unconditionally inherited the *ambient* session id and resolved the
    wrong (unbound) origin. This asserts the fixed adapter's real
    subprocess chain (adapter subprocess -> its own `task_contextctl.py`
    subprocess) actually uses the explicit override end-to-end."""
    task = service.create_task(conn, title="adapter-subprocess-override")
    activity = service.transition_activity(conn, task["id"], "implementation")
    binding = service.create_binding(conn)
    run = service.start_execution_run(
        conn,
        run_kind="native_operator",
        task_id=task["id"],
        activity_id=activity["id"],
        binding_id=binding["id"],
        claude_session_id="override-session",
    )
    service.set_binding_session(conn, binding["id"], "override-session", execution_run_id=run["id"])
    # Pre-attach the implementation claims/Activity the merge signal needs,
    # bound to the *override* origin only -- the ambient session is never
    # bound to anything, so if the override leaked/failed to propagate the
    # origin would resolve to `origin_run_not_found` instead.
    implementation_payload = {
        "signal_kind": "implementation_pr_observed",
        "source": "open-pr",
        "source_schema_version": "v1",
        "evidence": {"repo": REPO, "issue_number": 20, "pr_number": 21},
    }
    prep = _run_ctl(
        ["signal", "apply"], implementation_payload, state_root=state_root, claude_session_id="override-session"
    )
    assert prep["data"]["disposition"] == "applied"
    conn.close()

    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(json.dumps(_merged_snapshot()), encoding="utf-8")

    env = dict(os.environ)
    env["LOOP_TASK_CONTEXT_STATE_ROOT"] = str(state_root)
    env["CLAUDE_CODE_SESSION_ID"] = "ambient-session-never-bound"
    proc = subprocess.run(
        [
            sys.executable,
            str(_ADAPTER),
            "--snapshot-file",
            str(snapshot_file),
            "--issue-number",
            "20",
            "--pr-number",
            "21",
            "--phase",
            "merged",
            "--origin-session-id",
            "override-session",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, proc.stderr
    result = json.loads(lines[-1])
    assert result["disposition"] == "selected"
    assert result["reason_code"] == "CLEANUP_STARTED"
