"""Focused deterministic fixtures for Issue #2565 workflow-signal tests."""

from __future__ import annotations

import task_context_service as service

REPO = "squne121/loop-protocol"
SHA64 = "a" * 64
SHA40 = "b" * 40


def create_origin(conn, *, kind: str = "implementation", session: str = "session-1"):
    task = service.create_task(conn, title="workflow")
    activity = service.transition_activity(conn, task["id"], kind)
    binding = service.create_binding(conn)
    run = service.start_execution_run(
        conn,
        run_kind="native_operator",
        task_id=task["id"],
        activity_id=activity["id"],
        binding_id=binding["id"],
        claude_session_id=session,
    )
    service.set_binding_session(conn, binding["id"], session, execution_run_id=run["id"])
    return task, activity, binding, run


def implementation_payload(*, issue_number: int = 20, pr_number: int = 21):
    return {
        "signal_kind": "implementation_pr_observed",
        "source": "open-pr",
        "source_schema_version": "v1",
        "evidence": {"repo": REPO, "issue_number": issue_number, "pr_number": pr_number},
    }


def merged_payload(*, issue_number: int = 20, pr_number: int = 21, merge_oid: str = SHA40):
    return {
        "signal_kind": "pr_merged_observed",
        "source": "post-merge-cleanup",
        "source_schema_version": "v1",
        "evidence": {
            "repo": REPO,
            "issue_number": issue_number,
            "pr_number": pr_number,
            "merge_commit_oid": merge_oid,
        },
    }


def cleanup_completed_payload(*, issue_number: int = 20, pr_number: int = 21, merge_identity: str = SHA40):
    return {
        "signal_kind": "cleanup_completed",
        "source": "post-merge-cleanup",
        "source_schema_version": "v1",
        "evidence": {
            "repo": REPO,
            "issue_number": issue_number,
            "pr_number": pr_number,
            "merge_identity": merge_identity,
        },
    }


def mutation_counts(conn) -> tuple[int, int, int]:
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("task_ref_claims", "events", "projection_outbox")
    )
