"""Issue #2564 AC9, AC10, AC12 -- `query current` session-selector
read-only path, and the additive `projection ack` CLI operation. Exercises
the real ``task_contextctl.py`` CLI (in-process, per the #2563 test
convention in test_envelope_and_cli.py)."""

from __future__ import annotations

import io
import json
import sys

import task_context_db as db
import task_context_envelope as envelope
import task_context_hook_flows as hook_flows
import task_contextctl as cli


def _run_cli(argv, stdin_obj, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin_obj)))
    exit_code = cli.main(argv)
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one stdout line, got: {captured.out!r}"
    return json.loads(lines[0]), exit_code


def test_given_no_db_file_yet_when_query_current_by_session_then_degraded_empty_no_db_created(
    state_root, monkeypatch, capsys
):
    """AC9: the read-only statusLine path must never create the state-root
    directory or the DB file as a side effect of a read."""
    assert not state_root.exists()
    request = envelope.build_request("query_current", {"session_id": "no-such-session"})
    result, exit_code = _run_cli(["query", "current"], request, monkeypatch, capsys)
    assert exit_code == 0
    assert result["status"] == "ok"
    assert result["data"]["degraded"] is True
    assert result["data"]["degraded_reason"] == "no_state_db"
    assert not state_root.exists(), "AC9: query current must not create the DB directory"


def test_given_db_exists_but_no_binding_for_session_when_queried_then_degraded_not_error(
    state_root, monkeypatch, capsys
):
    # Force DB creation via an unrelated write path first (smoke seed).
    _run_cli(["smoke", "seed"], envelope.build_request("smoke_seed", {}), monkeypatch, capsys)
    request = envelope.build_request("query_current", {"session_id": "no-such-session"})
    result, exit_code = _run_cli(["query", "current"], request, monkeypatch, capsys)
    assert exit_code == 0
    assert result["data"]["degraded"] is True
    assert result["data"]["degraded_reason"] == "no_binding_for_session"


def test_given_bound_session_when_queried_then_full_projection_returned(state_root, monkeypatch, capsys):
    db_file = None
    # Use the real write path (hook_flows) directly against a connected DB
    # to seed realistic state, then query it back through the read-only CLI
    # path.
    import task_context_config as config
    import task_context_migration_runner as migration_runner

    db_file = config.db_path()
    conn = db.connect(db_file)
    migration_runner.migrate(conn)
    hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-1", "claude_session_id": "s1"}
    )
    hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-1",
            "claude_session_id": "s1",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/repo",
            "target_ref_kind": "issue",
            "target_ref_number": 55,
        },
    )
    conn.close()

    request = envelope.build_request("query_current", {"session_id": "s1"})
    result, exit_code = _run_cli(["query", "current"], request, monkeypatch, capsys)
    assert exit_code == 0
    assert result["data"]["degraded"] is False
    assert result["data"]["task"] is not None
    assert result["data"]["task_refs"][0]["ref_number"] == 55
    assert result["data"]["attention"] is None


def test_given_legacy_task_id_selector_when_queried_then_unaffected_by_session_addition(
    state_root, monkeypatch, capsys
):
    """Backward compatibility: the pre-existing task_id-based `query
    current` path (and its migrate-on-open semantics/error taxonomy) must
    be untouched by the additive session_id path."""
    seeded, _ = _run_cli(["smoke", "seed"], envelope.build_request("smoke_seed", {}), monkeypatch, capsys)
    task_id = seeded["data"]["task_id"]
    request = envelope.build_request("query_current", {"task_id": task_id})
    result, exit_code = _run_cli(["query", "current"], request, monkeypatch, capsys)
    assert exit_code == 0
    assert result["data"]["task"]["id"] == task_id


def test_given_missing_task_id_and_session_id_when_queried_then_validation_error(
    state_root, monkeypatch, capsys
):
    request = envelope.build_request("query_current", {})
    result, exit_code = _run_cli(["query", "current"], request, monkeypatch, capsys)
    assert exit_code == 2
    assert result["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# projection ack (additive CLI operation)
# ---------------------------------------------------------------------------


def test_given_nothing_enqueued_when_projection_ack_then_not_acked(state_root, monkeypatch, capsys):
    request = envelope.build_request("projection_ack", {"projection_key": "tab_binding:none", "read_revision": 1})
    result, exit_code = _run_cli(["projection", "ack"], request, monkeypatch, capsys)
    assert exit_code == 0
    assert result["data"]["acked"] is False


def test_given_enqueued_marker_when_acked_with_matching_revision_then_removed(state_root, monkeypatch, capsys):
    import task_context_config as config
    import task_context_migration_runner as migration_runner
    import task_context_service as service

    db_file = config.db_path()
    conn = db.connect(db_file)
    migration_runner.migrate(conn)
    service.enqueue_projection(conn, "tab_binding:b1", 1)
    conn.close()

    flush_request = envelope.build_request("projection_flush", {"projection_key": "tab_binding:b1"})
    flush_result, _ = _run_cli(["projection", "flush"], flush_request, monkeypatch, capsys)
    revision = flush_result["data"]["projection"]["desired_revision"]

    ack_request = envelope.build_request(
        "projection_ack", {"projection_key": "tab_binding:b1", "read_revision": revision}
    )
    ack_result, exit_code = _run_cli(["projection", "ack"], ack_request, monkeypatch, capsys)
    assert exit_code == 0
    assert ack_result["data"]["acked"] is True

    flush_again, _ = _run_cli(
        ["projection", "flush"],
        envelope.build_request("projection_flush", {"projection_key": "tab_binding:b1"}),
        monkeypatch,
        capsys,
    )
    assert flush_again["data"]["projection"] is None


def test_given_race_advances_revision_between_flush_and_ack_when_acked_then_not_lost(
    state_root, monkeypatch, capsys
):
    """AC12: the same revision-aware conditional-ack race guard already
    proven at the service layer (#2563) must hold through the CLI too."""
    import task_context_config as config
    import task_context_migration_runner as migration_runner
    import task_context_service as service

    db_file = config.db_path()
    conn = db.connect(db_file)
    migration_runner.migrate(conn)
    service.enqueue_projection(conn, "tab_binding:b2", 1)
    conn.close()

    flush_request = envelope.build_request("projection_flush", {"projection_key": "tab_binding:b2"})
    flush_result, _ = _run_cli(["projection", "flush"], flush_request, monkeypatch, capsys)
    stale_revision = flush_result["data"]["projection"]["desired_revision"]

    # Simulate a concurrent enqueue advancing the revision before our ack.
    conn = db.connect(db_file)
    migration_runner.migrate(conn)
    import task_context_service as service2

    service2.enqueue_projection(conn, "tab_binding:b2", stale_revision + 1)
    conn.close()

    ack_request = envelope.build_request(
        "projection_ack", {"projection_key": "tab_binding:b2", "read_revision": stale_revision}
    )
    ack_result, _ = _run_cli(["projection", "ack"], ack_request, monkeypatch, capsys)
    assert ack_result["data"]["acked"] is False

    flush_again, _ = _run_cli(
        ["projection", "flush"],
        envelope.build_request("projection_flush", {"projection_key": "tab_binding:b2"}),
        monkeypatch,
        capsys,
    )
    assert flush_again["data"]["projection"]["desired_revision"] == stale_revision + 1
