"""PR #2615 fix_delta 3 + fix_delta 5 regression coverage.

fix_delta 3: the coarse-grained ``bind_target_to_binding`` /
``bind_ad_hoc_task_to_binding`` / ``absorb_ref_into_task`` service
operations collapse "resolve-or-create Task -> claim ref -> ensure ACTIVE
Activity -> attach ExecutionRun -> append event -> bump projection outbox"
into a single ``BEGIN IMMEDIATE`` so a lost ref-claim race never leaves an
orphan OPEN Task behind. This is a *real* two-OS-thread race (two separate
sqlite3 connections to the same DB file racing to claim the same GitHub
ref), not a mocked/simulated one.

fix_delta 5: ``SubagentStart``/``SubagentStop`` correlate on the Claude
Code-supplied ``agent_id`` so a `SubagentStop` for one concurrently running
SubAgent never ends a sibling SubAgent's still-open run."""

from __future__ import annotations

import threading

import task_context_db as db
import task_context_hook_flows as hook_flows
import task_context_service as service


def test_given_two_connections_racing_same_ref_when_bind_target_concurrently_then_single_task_no_orphan(
    db_file, conn
):
    """fix_delta 3: two operators (separate Bindings/connections) racing to
    autobind the *same* (repo, ref_kind, ref_number) for the first time must
    converge on exactly one Task -- the loser's transaction must never leave
    behind an orphan OPEN Task or a duplicate live ref claim."""
    binding_a = service.create_binding(conn)
    binding_b = service.create_binding(conn)

    results: dict[str, dict] = {}
    errors_seen: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(name: str, binding_id: str) -> None:
        try:
            local_conn = db.connect(db_file)
            try:
                barrier.wait(timeout=10)
                result = service.bind_target_to_binding(
                    local_conn,
                    binding_id=binding_id,
                    execution_run_id=None,
                    repo="owner/repo",
                    ref_kind="issue",
                    ref_number=999,
                    reason_code="autobind",
                )
                results[name] = result
            finally:
                local_conn.close()
        except BaseException as exc:  # noqa: BLE001 - captured for the main thread to assert on
            errors_seen.append(exc)

    t1 = threading.Thread(target=worker, args=("a", binding_a["id"]))
    t2 = threading.Thread(target=worker, args=("b", binding_b["id"]))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not errors_seen, f"race workers raised: {errors_seen}"
    assert set(results) == {"a", "b"}
    assert results["a"]["task_id"] == results["b"]["task_id"], "both racers must converge on the same Task"

    verify_conn = db.connect(db_file)
    try:
        task_rows = verify_conn.execute(
            "SELECT id FROM tasks WHERE title = ?", ("owner/repo#999",)
        ).fetchall()
        assert len(task_rows) == 1, "race must never leave an orphan duplicate Task behind"

        claim_row = verify_conn.execute(
            "SELECT COUNT(*) AS c FROM task_ref_claims "
            "WHERE repo = ? AND ref_kind = ? AND ref_number = ? AND released_at IS NULL",
            ("owner/repo", "issue", 999),
        ).fetchone()
        assert claim_row["c"] == 1, "exactly one live claim must exist on the raced ref"

        for name in ("a", "b"):
            run_row = verify_conn.execute(
                "SELECT task_id, activity_id FROM execution_runs WHERE id = ?",
                (results[name]["execution_run_id"],),
            ).fetchone()
            assert run_row["task_id"] == results["a"]["task_id"]
            assert run_row["activity_id"] == results["a"]["activity_id"]
    finally:
        verify_conn.close()


def test_given_absorb_ref_race_when_two_connections_absorb_same_ref_then_single_winner_no_partial_claim(
    db_file, conn
):
    """fix_delta 3: the AC4 provisional/absorbent path is equally atomic --
    two ACTIVE, 0-ref Tasks racing to absorb the same first primary ref must
    leave exactly one winner and never a half-applied claim."""
    binding_a = service.create_binding(conn)
    binding_b = service.create_binding(conn)
    task_a = service.create_task(conn, title="provisional-a")
    task_b = service.create_task(conn, title="provisional-b")
    service.transition_activity(conn, task_a["id"], kind="native_operator")
    service.transition_activity(conn, task_b["id"], kind="native_operator")

    results: dict[str, dict] = {}
    errors_seen: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(name: str, binding_id: str, task_id: str) -> None:
        try:
            local_conn = db.connect(db_file)
            try:
                barrier.wait(timeout=10)
                result = service.absorb_ref_into_task(
                    local_conn,
                    binding_id=binding_id,
                    execution_run_id=None,
                    task_id=task_id,
                    repo="owner/repo",
                    ref_kind="issue",
                    ref_number=555,
                    reason_code="provisional_absorb",
                )
                results[name] = result
            finally:
                local_conn.close()
        except BaseException as exc:  # noqa: BLE001
            errors_seen.append(exc)

    t1 = threading.Thread(target=worker, args=("a", binding_a["id"], task_a["id"]))
    t2 = threading.Thread(target=worker, args=("b", binding_b["id"], task_b["id"]))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not errors_seen, f"race workers raised: {errors_seen}"
    winner_task_id = results["a"]["task_id"]
    assert results["b"]["task_id"] == winner_task_id, "the ref must have exactly one winning Task"
    assert winner_task_id in (task_a["id"], task_b["id"])

    verify_conn = db.connect(db_file)
    try:
        claim_row = verify_conn.execute(
            "SELECT COUNT(*) AS c FROM task_ref_claims "
            "WHERE repo = ? AND ref_kind = ? AND ref_number = ? AND released_at IS NULL",
            ("owner/repo", "issue", 555),
        ).fetchone()
        assert claim_row["c"] == 1, "exactly one live claim must exist on the raced ref"
    finally:
        verify_conn.close()


def test_given_two_subagents_started_when_first_stops_by_agent_id_then_second_remains_open(conn):
    """fix_delta 5: `start A -> start B -> stop A -> B remains open`."""
    hook_flows.on_session_start(conn, {"source": "startup", "herdr_tab_id": "tab-1", "claude_session_id": "s1"})
    hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-1",
            "claude_session_id": "s1",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/repo",
            "target_ref_kind": "issue",
            "target_ref_number": 1,
        },
    )

    start_a = hook_flows.on_subagent_start(conn, {"claude_session_id": "s1", "agent_id": "agent-a"})
    start_b = hook_flows.on_subagent_start(conn, {"claude_session_id": "s1", "agent_id": "agent-b"})
    assert start_a["decision"] == "pass"
    assert start_b["decision"] == "pass"
    run_a = start_a["execution_run_id"]
    run_b = start_b["execution_run_id"]
    assert run_a != run_b

    open_before = service.find_open_execution_runs(conn, run_kind="subagent")
    assert {r["id"] for r in open_before} == {run_a, run_b}

    stop_a = hook_flows.on_subagent_stop(conn, {"claude_session_id": "s1", "agent_id": "agent-a"})
    assert stop_a["decision"] == "pass"
    assert stop_a["execution_run_id"] == run_a

    ended_a = service.get_execution_run(conn, run_a)
    assert ended_a["ended_at"] is not None

    still_open_b = service.get_execution_run(conn, run_b)
    assert still_open_b["ended_at"] is None, "stopping agent-a must never end agent-b's still-open run"

    open_after = service.find_open_execution_runs(conn, run_kind="subagent")
    assert {r["id"] for r in open_after} == {run_b}


def test_given_ambiguous_subagent_stop_without_agent_id_when_two_open_then_neither_ended(conn):
    """When Claude Code does not supply `agent_id` and more than one sibling
    SubAgent run is open, the adapter must not guess -- it leaves every run
    open rather than risking ending the wrong one."""
    hook_flows.on_session_start(conn, {"source": "startup", "herdr_tab_id": "tab-1", "claude_session_id": "s1"})
    hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-1",
            "claude_session_id": "s1",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/repo",
            "target_ref_kind": "issue",
            "target_ref_number": 1,
        },
    )
    hook_flows.on_subagent_start(conn, {"claude_session_id": "s1"})
    hook_flows.on_subagent_start(conn, {"claude_session_id": "s1"})

    result = hook_flows.on_subagent_stop(conn, {"claude_session_id": "s1"})
    assert result["decision"] == "pass"
    assert result["reason_code"] == "subagent_stop_ambiguous_without_agent_id"
    assert result["open_subagent_run_count"] == 2

    open_runs = service.find_open_execution_runs(conn, run_kind="subagent")
    assert len(open_runs) == 2, "ambiguous stop without agent_id must never end any run"


def test_given_agent_id_with_no_matching_open_run_when_stopped_then_pass_no_crash(conn):
    result = hook_flows.on_subagent_stop(conn, {"claude_session_id": "s1", "agent_id": "unknown-agent"})
    assert result["decision"] == "pass"
    assert result["reason_code"] == "no_open_subagent_run_for_agent_id"
    assert result["agent_id"] == "unknown-agent"
