"""Shared fixtures for the Issue #2817 retroactive-claim / local-only tests.

Everything here builds *real* Task Context state in a temporary SQLite DB and
drives the *real* adapter CLI (``task_context_workflow_signal.py``) as a
subprocess, which in turn drives the real ``task_contextctl.py``. Nothing
patches ``_run``.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys

import task_context_config as config
import task_context_db as db
import task_context_service as service

REPO_ROOT = pathlib.Path(config.__file__).resolve().parents[2]
ADAPTER = REPO_ROOT / ".claude" / "skills" / "post-merge-cleanup" / "scripts" / "task_context_workflow_signal.py"
SKILL = REPO_ROOT / ".claude" / "skills" / "post-merge-cleanup" / "SKILL.md"
DOCS = REPO_ROOT / "docs" / "dev" / "task-context.md"

REPO = "squne121/loop-protocol"
ISSUE = 20
PR = 21
OID = "b" * 40
SESSION = "origin-session"
AMBIENT = "ambient-session-never-bound"


# ---------------------------------------------------------------------------
# snapshot + adapter subprocess
# ---------------------------------------------------------------------------


def merged_snapshot(
    *,
    issue: int = ISSUE,
    pr: int = PR,
    oid=OID,
    repo: str = REPO,
    relation_repo: str | None = None,
    merged: bool = True,
    nodes="default",
    with_merge_commit: bool = True,
    body: str | None = None,
    errors: list | None = None,
) -> dict:
    if nodes == "default":
        nodes = [{"number": issue, "repository": {"nameWithOwner": relation_repo or repo}}]
    pull_request: dict = {"number": pr, "merged": merged, "closingIssuesReferences": {"nodes": nodes}}
    if with_merge_commit:
        pull_request["mergeCommit"] = {"oid": oid}
    if body is not None:
        pull_request["body"] = body
    snapshot: dict = {"data": {"repository": {"nameWithOwner": repo, "pullRequest": pull_request}}}
    if errors is not None:
        snapshot["errors"] = errors
    return snapshot


def write_snapshot(
    tmp_path: pathlib.Path, snapshot: dict | None = None, *, age_seconds: float = 0, name="snapshot.json"
):
    path = tmp_path / name
    path.write_text(json.dumps(snapshot if snapshot is not None else merged_snapshot()), encoding="utf-8")
    if age_seconds:
        import time

        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


def run_adapter(
    args: list[str],
    *,
    state_root,
    ambient_session: str | None = AMBIENT,
    timeout: float = 60,
    extra_env: dict | None = None,
):
    env = dict(os.environ)
    env.update(extra_env or {})
    env["LOOP_TASK_CONTEXT_STATE_ROOT"] = str(state_root)
    if ambient_session is None:
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    else:
        env["CLAUDE_CODE_SESSION_ID"] = ambient_session
    proc = subprocess.run(
        [sys.executable, str(ADAPTER), *args], capture_output=True, text=True, env=env, timeout=timeout
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, proc.stderr
    return json.loads(lines[-1])


def base_args(snapshot_file, phase: str, *, issue: int = ISSUE, pr: int = PR) -> list[str]:
    return [
        "--snapshot-file",
        str(snapshot_file),
        "--issue-number",
        str(issue),
        "--pr-number",
        str(pr),
        "--phase",
        phase,
    ]


def recover_args(
    snapshot_file,
    *,
    session: str | None = SESSION,
    merge_identity: str | None = OID,
    explicit: bool = True,
    issue: int = ISSUE,
    pr: int = PR,
) -> list[str]:
    args = base_args(snapshot_file, "recover", issue=issue, pr=pr)
    if merge_identity is not None:
        args += ["--merge-identity", merge_identity]
    if explicit:
        args += ["--explicit-recovery"]
    if session is not None:
        args += ["--origin-session-id", session]
    return args


def local_only_args(
    snapshot_file,
    *,
    outcome: str | None = "deferred/IMPLEMENTATION_NOT_READY",
    merge_identity: str | None = OID,
    worktree: str | None = ".claude/worktrees/issue-20-x",
    branch: str | None = "worktree-issue-20-x",
    issue: int = ISSUE,
    pr: int = PR,
) -> list[str]:
    args = base_args(snapshot_file, "local-only", issue=issue, pr=pr)
    for flag, value in (
        ("--merge-identity", merge_identity),
        ("--task-context-outcome", outcome),
        ("--worktree-path", worktree),
        ("--branch-name", branch),
    ):
        if value is not None:
            args += [flag, value]
    return args


def merged_args(snapshot_file, *, session: str | None = SESSION) -> list[str]:
    args = base_args(snapshot_file, "merged")
    if session is not None:
        args += ["--origin-session-id", session]
    return args


# ---------------------------------------------------------------------------
# DB dumps (row CONTENTS, not counts)
# ---------------------------------------------------------------------------


def dump_db(conn: sqlite3.Connection | None = None) -> dict[str, list[tuple]]:
    own = conn is None
    if own:
        conn = sqlite3.connect(str(config.db_path()))
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables
        }
    finally:
        if own:
            conn.close()


def rows_of_tasks(task_ids: list[str]) -> dict[str, list[tuple]]:
    """Every row that belongs to any of ``task_ids`` (all tables with a
    ``task_id`` column plus the ``tasks`` rows themselves)."""
    conn = sqlite3.connect(str(config.db_path()))
    try:
        marks = ",".join("?" for _ in task_ids)
        found = {"tasks": [tuple(r) for r in conn.execute(f"SELECT * FROM tasks WHERE id IN ({marks})", task_ids)]}
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ):
            columns = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
            if "task_id" in columns:
                found[table] = [
                    tuple(r)
                    for r in conn.execute(f"SELECT * FROM {table} WHERE task_id IN ({marks}) ORDER BY rowid", task_ids)
                ]
        return found
    finally:
        conn.close()


def new_rows(before: dict[str, list[tuple]], after: dict[str, list[tuple]]) -> dict[str, list[tuple]]:
    """Rows present in ``after`` but not in ``before`` (per table)."""
    return {
        t: [r for r in after[t] if r not in before.get(t, [])]
        for t in after
        if [r for r in after[t] if r not in before.get(t, [])]
    }


def read_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(config.db_path()))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# state builders (typed service API only; raw SQL only to END an activity)
# ---------------------------------------------------------------------------


def end_activity(conn, activity_id: str, status: str = "DONE") -> None:
    with db.write_transaction(conn):
        conn.execute(
            "UPDATE activities SET status = ?, ended_at = ? WHERE id = ?", (status, service.now_iso(), activity_id)
        )


def build_origin(
    conn, *, session: str = SESSION, title: str = "origin", active_kind: str = "none", history: str = "none"
):
    """A Task with a live managed origin session.

    ``active_kind``: the kind of the single ACTIVE Activity (``none`` for
    none). ``history``: ``terminal`` first creates an implementation Activity
    and ends it (so an older terminal implementation row exists)."""
    task = service.create_task(conn, title=title)
    tid = task["id"]
    if history == "terminal":
        end_activity(conn, service.transition_activity(conn, tid, "implementation")["id"])
    # For an ACTIVE implementation Activity the origin run deliberately stays
    # on the earlier `refine` Activity, so "not re-attached" is observable.
    first_kind = "refine" if active_kind == "implementation" else active_kind
    activity = service.transition_activity(conn, tid, first_kind) if first_kind != "none" else None
    binding = service.create_binding(conn)
    run = service.start_execution_run(
        conn,
        run_kind="native_operator",
        task_id=tid,
        activity_id=activity["id"] if activity else None,
        binding_id=binding["id"],
        claude_session_id=session,
    )
    service.set_binding_session(conn, binding["id"], session, execution_run_id=run["id"])
    if active_kind == "implementation":
        activity = service.transition_activity(conn, tid, "implementation")
    return {"task": task, "activity": activity, "binding": binding, "run": run}


def add_subagent_run(conn, task_id: str) -> dict:
    return service.start_execution_run(conn, run_kind="subagent", task_id=task_id)


def claim(conn, task_id: str, kind: str, number: int, *, repo: str = REPO) -> str:
    result = service.claim_task_ref(conn, task_id, repo, kind, number)
    assert result["status"] == "claimed", result
    return result["claim_id"]


def plain_task(conn, title: str) -> str:
    return service.create_task(conn, title=title)["id"]


def cleanup_report(*, status: str = "ok", human_review_required: bool = False) -> dict:
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
