"""Issue #2566 -- `task_context_hook_flows.on_pre_tool_use` DB-backed
integration tests for the SendMessage/notify_when_idle and Herdr Bash
guard paths (AC2, AC4, AC5, AC7)."""

from __future__ import annotations

import json

import task_context_hook_flows as hook_flows


def _bind_session_to_task(conn, *, herdr_tab_id: str, claude_session_id: str, repo: str, ref_number: int) -> str:
    hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": herdr_tab_id, "claude_session_id": claude_session_id}
    )
    prompt = hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": herdr_tab_id,
            "claude_session_id": claude_session_id,
            "classification_kind": "EXPLICIT",
            "target_repo": repo,
            "target_ref_kind": "issue",
            "target_ref_number": ref_number,
        },
    )
    return prompt["task_id"]


# ---------------------------------------------------------------------------
# SendMessage / notify_when_idle (AC2, AC4)
# ---------------------------------------------------------------------------


def test_given_send_message_to_open_subagent_when_pre_tool_use_then_pass_no_ask(conn):
    task_id = _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)
    hook_flows.on_subagent_start(conn, {"claude_session_id": "s1", "agent_id": "agent-x"})

    result = hook_flows.on_pre_tool_use(
        conn, {"tool_name": "SendMessage", "claude_session_id": "s1", "to": "agent-x"}
    )
    assert result["decision"] == "pass"
    assert result["target_kind"] == "in_session_subagent"
    assert task_id  # sanity: caller was bound


def test_given_send_message_to_same_task_peer_when_pre_tool_use_then_pass_no_decision(conn):
    task_id = _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)
    hook_flows.on_session_start(conn, {"source": "startup", "herdr_tab_id": "tab-2", "claude_session_id": "s2"})
    # s2 joins the *same* Task (same target ref).
    hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-2",
            "claude_session_id": "s2",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/repo",
            "target_ref_kind": "issue",
            "target_ref_number": 1,
        },
    )

    result = hook_flows.on_pre_tool_use(conn, {"tool_name": "SendMessage", "claude_session_id": "s1", "to": "s2"})
    assert result["decision"] == "pass"
    assert result["target_kind"] == "same_task_independent_session"
    assert task_id


def test_given_send_message_to_cross_task_peer_when_pre_tool_use_then_ask(conn):
    _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)
    _bind_session_to_task(conn, herdr_tab_id="tab-2", claude_session_id="s2", repo="owner/repo", ref_number=2)

    result = hook_flows.on_pre_tool_use(conn, {"tool_name": "SendMessage", "claude_session_id": "s1", "to": "s2"})
    assert result["decision"] == "ask"
    assert result["target_kind"] == "known_cross_task_independent_session"


def test_given_send_message_to_unknown_session_when_pre_tool_use_then_ask(conn):
    _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)

    result = hook_flows.on_pre_tool_use(
        conn, {"tool_name": "SendMessage", "claude_session_id": "s1", "to": "totally-unknown-peer"}
    )
    assert result["decision"] == "ask"
    assert result["target_kind"] == "unknown_independent_session"


def test_given_send_message_broadcast_when_pre_tool_use_then_pass_out_of_ac_scope(conn):
    _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)

    result = hook_flows.on_pre_tool_use(conn, {"tool_name": "SendMessage", "claude_session_id": "s1", "to": "*"})
    assert result["decision"] == "pass"
    assert result["target_kind"] == "unaddressed_broadcast"


def test_given_notify_when_idle_same_task_when_pre_tool_use_then_pass(conn):
    _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)
    hook_flows.on_session_start(conn, {"source": "startup", "herdr_tab_id": "tab-2", "claude_session_id": "s2"})
    hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-2",
            "claude_session_id": "s2",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/repo",
            "target_ref_kind": "issue",
            "target_ref_number": 1,
        },
    )

    result = hook_flows.on_pre_tool_use(
        conn,
        {"tool_name": "SendMessage", "claude_session_id": "s1", "to": "s2", "notify_when_idle": True},
    )
    assert result["decision"] == "pass"


def test_given_send_message_to_realistic_independent_session_identifier_when_pre_tool_use_then_ask(conn):
    """Issue #2566 fix_delta P2-fixture-realism: a realistic `to` value for
    the "independent session" case -- shaped like the `agentId`-family
    hex identifiers this repo's own real prior `SendMessage` tool_use
    history has actually carried, and distinct from any tracked
    `claude_session_id` (which is UUID-shaped) -- not the previous
    same-literal `to == claude_session_id` fixture that proved nothing
    about a realistic `to` shape. No currently-open SubAgent/teammate run
    uses this identifier, so it resolves to `unknown_independent_session`
    (fail-safe ASK), documenting the current real-world behavior (see
    `_on_pre_tool_use_send_message` docstring: no reliable name/session-id
    -> Binding mapping exists for a genuinely independent session)."""
    _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)

    result = hook_flows.on_pre_tool_use(
        conn, {"tool_name": "SendMessage", "claude_session_id": "s1", "to": "a8c070c249972b5d4"}
    )
    assert result["decision"] == "ask"
    assert result["target_kind"] == "unknown_independent_session"


def test_given_send_message_to_agent_type_name_not_agent_id_when_pre_tool_use_then_not_in_session_subagent(conn):
    """Issue #2566 fix_delta P1-B/P2-fixture-realism: a named SubAgent/
    Agent Teams teammate's `agent_type` (e.g. Claude Code's own
    `general-purpose` / a custom `.claude/agents/*.md` name) is a distinct
    field from `SubagentStart`'s `agent_id` (Claude Code hooks docs: these
    are separate fields). `to` carrying the *name* rather than the actual
    tracked `agent_id` must not be confused with the open SubAgent run it
    refers to -- it does not match `find_open_execution_runs(agent_id=to)`
    and therefore is not classified as `in_session_subagent`."""
    task_id = _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)
    hook_flows.on_subagent_start(conn, {"claude_session_id": "s1", "agent_id": "a8c070c249972b5d4"})

    result = hook_flows.on_pre_tool_use(
        conn, {"tool_name": "SendMessage", "claude_session_id": "s1", "to": "general-purpose"}
    )
    assert result["decision"] == "ask"
    assert result["target_kind"] == "unknown_independent_session"
    assert task_id


def test_given_notify_when_idle_cross_task_when_pre_tool_use_then_ask(conn):
    _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)
    _bind_session_to_task(conn, herdr_tab_id="tab-2", claude_session_id="s2", repo="owner/repo", ref_number=2)

    result = hook_flows.on_pre_tool_use(
        conn,
        {"tool_name": "SendMessage", "claude_session_id": "s1", "to": "s2", "notify_when_idle": True},
    )
    assert result["decision"] == "ask"


# ---------------------------------------------------------------------------
# Herdr Bash guard (AC5)
# ---------------------------------------------------------------------------


def test_given_herdr_discovery_op_when_pre_tool_use_then_pass_no_journal_decision(conn):
    _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)
    result = hook_flows.on_pre_tool_use(
        conn,
        {
            "tool_name": "Bash",
            "claude_session_id": "s1",
            "herdr_category": "discovery",
            "herdr_operation": "pane_get",
            "herdr_target_locator": "tab-2",
            "herdr_machine_scoped": False,
        },
    )
    assert result["decision"] == "pass"
    assert result["reason_code"] == "herdr_discovery_no_decision"


def test_given_herdr_content_read_same_task_when_pre_tool_use_then_pass(conn):
    _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)
    # tab-2 is a RuntimeLocation belonging to a Binding on the *same* Task.
    hook_flows.on_session_start(conn, {"source": "startup", "herdr_tab_id": "tab-2", "claude_session_id": "s2"})
    hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-2",
            "claude_session_id": "s2",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/repo",
            "target_ref_kind": "issue",
            "target_ref_number": 1,
        },
    )

    result = hook_flows.on_pre_tool_use(
        conn,
        {
            "tool_name": "Bash",
            "claude_session_id": "s1",
            "herdr_category": "content_read",
            "herdr_operation": "pane_read",
            "herdr_target_locator": "tab-2",
            "herdr_machine_scoped": False,
        },
    )
    assert result["decision"] == "pass"
    assert result["target_kind"] == "same_task_independent_session"


def test_given_herdr_content_read_cross_task_when_pre_tool_use_then_ask(conn):
    _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)
    _bind_session_to_task(conn, herdr_tab_id="tab-2", claude_session_id="s2", repo="owner/repo", ref_number=2)

    result = hook_flows.on_pre_tool_use(
        conn,
        {
            "tool_name": "Bash",
            "claude_session_id": "s1",
            "herdr_category": "content_read",
            "herdr_operation": "pane_read",
            "herdr_target_locator": "tab-2",
            "herdr_machine_scoped": False,
        },
    )
    assert result["decision"] == "ask"
    assert result["target_kind"] == "known_cross_task_independent_session"


def test_given_herdr_control_op_unresolved_locator_when_pre_tool_use_then_ask(conn):
    _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)

    result = hook_flows.on_pre_tool_use(
        conn,
        {
            "tool_name": "Bash",
            "claude_session_id": "s1",
            "herdr_category": "control",
            "herdr_operation": "agent_prompt",
            "herdr_target_locator": "some-agent-name",
            "herdr_machine_scoped": False,
        },
    )
    assert result["decision"] == "ask"
    assert result["target_kind"] == "unknown_independent_session"


def test_given_herdr_control_op_machine_scoped_when_pre_tool_use_then_ask(conn):
    _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)

    result = hook_flows.on_pre_tool_use(
        conn,
        {
            "tool_name": "Bash",
            "claude_session_id": "s1",
            "herdr_category": "control",
            "herdr_operation": "agent_prompt",
            "herdr_target_locator": "tab-1",
            "herdr_machine_scoped": True,
        },
    )
    assert result["decision"] == "ask"
    assert result["target_kind"] == "unknown_independent_session"


def test_given_herdr_control_op_same_task_when_pre_tool_use_then_pass(conn):
    _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)

    result = hook_flows.on_pre_tool_use(
        conn,
        {
            "tool_name": "Bash",
            "claude_session_id": "s1",
            "herdr_category": "control",
            "herdr_operation": "pane_send-keys",
            "herdr_target_locator": "tab-1",
            "herdr_machine_scoped": False,
        },
    )
    assert result["decision"] == "pass"
    assert result["target_kind"] == "same_task_independent_session"


# ---------------------------------------------------------------------------
# AC7: bounded EventJournal metadata only -- never a raw `to`, `message`,
# terminal output, or full Bash command line.
# ---------------------------------------------------------------------------


def test_given_send_message_guard_decision_when_journaled_then_only_bounded_fields_recorded(conn):
    _bind_session_to_task(conn, herdr_tab_id="tab-1", claude_session_id="s1", repo="owner/repo", ref_number=1)
    hook_flows.on_pre_tool_use(
        conn, {"tool_name": "SendMessage", "claude_session_id": "s1", "to": "unresolvable-peer"}
    )

    rows = conn.execute("SELECT metadata_json FROM events WHERE event_type = 'hook:PreToolUse'").fetchall()
    assert len(rows) == 1
    metadata = json.loads(rows[0]["metadata_json"])
    assert set(metadata.keys()) == {
        "transport",
        "operation",
        "target_kind",
        "source_task_id",
        "destination_task_id",
        "decision",
        "reason_code",
    }
    assert metadata["transport"] == "send_message"
    assert metadata["target_kind"] == "unknown_independent_session"
    assert metadata["decision"] == "ask"
    # Never a raw `to` value, message body, or full command anywhere in the
    # recorded metadata.
    serialized = json.dumps(metadata)
    assert "unresolvable-peer" not in serialized
