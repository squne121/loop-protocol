"""Issue #2822 -- SendMessage destination addressability (deterministic).

Claude Code, not Task Context, owns what a SendMessage ``to`` can address:
an agent ID (a completed SubAgent is resumed, so ``ended_at`` is irrelevant)
or an Agent Teams teammate name (Claude-owned team config ``members[]``). The
AC1 canary showed a named ordinary SubAgent's ``name`` is not observable from
hooks (Agent ``tool_input`` has no ``name``; ``SubagentStart`` carries only
``agent_id`` / ``agent_type``), so that name lane stays ASK.

Naming convention (fixed by the Issue so ``-k`` selectors stay unambiguous):
``test_addr_by_agent_id_*`` / ``test_addr_by_name_*`` / ``test_addr_must_ask_*``
/ ``test_addr_refire_*`` / ``test_addr_same_task_*`` /
``test_addr_cross_task_*`` / ``test_addr_notify_when_idle_*``.
"""

from __future__ import annotations

import json

import pytest

import task_context_hook_flows as hook_flows
import task_context_service as service
import task_context_session_registry as session_registry
import task_context_team_config as team_config

CALLER = "11111111-aaaa-bbbb-cccc-000000000001"
OTHER = "22222222-aaaa-bbbb-cccc-000000000002"


@pytest.fixture(autouse=True)
def _isolated_claude_state(tmp_path, monkeypatch):
    """Never read a developer machine's real session registry / team config."""
    sessions = tmp_path / "claude-sessions"
    sessions.mkdir()
    teams = tmp_path / "claude-teams"
    teams.mkdir()
    monkeypatch.setenv(session_registry.SESSION_REGISTRY_DIR_ENV_VAR, str(sessions))
    monkeypatch.setenv(session_registry.TEAMS_DIR_ENV_VAR, str(teams))
    monkeypatch.delenv(team_config.AGENT_TEAMS_FLAG_ENV_VAR, raising=False)
    monkeypatch.delenv(session_registry.CLAUDE_CONFIG_DIR_ENV_VAR, raising=False)
    return sessions, teams


@pytest.fixture
def sessions_dir(_isolated_claude_state):
    return _isolated_claude_state[0]


@pytest.fixture
def teams_dir(_isolated_claude_state):
    return _isolated_claude_state[1]


@pytest.fixture
def teams_enabled(monkeypatch):
    monkeypatch.setenv(team_config.AGENT_TEAMS_FLAG_ENV_VAR, "1")


def _bind(conn, *, tab: str, session: str, ref_number: int, repo: str = "owner/repo") -> None:
    hook_flows.on_session_start(conn, {"source": "startup", "herdr_tab_id": tab, "claude_session_id": session})
    hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": tab,
            "claude_session_id": session,
            "classification_kind": "EXPLICIT",
            "target_repo": repo,
            "target_ref_kind": "issue",
            "target_ref_number": ref_number,
        },
    )


def _send(conn, to, *, session=CALLER, **extra):
    payload = {"tool_name": "SendMessage", "claude_session_id": session, "to": to}
    payload.update(extra)
    return hook_flows.on_pre_tool_use(conn, payload)


def _write_registry(directory, *, pid: int, session_id: str, name: str) -> None:
    (directory / f"{pid}.json").write_text(
        json.dumps({"pid": pid, "sessionId": session_id, "name": name}), encoding="utf-8"
    )


def _write_team(teams_dir, session_id: str, config) -> None:
    directory = teams_dir / f"session-{session_id[:8]}"
    directory.mkdir(parents=True, exist_ok=True)
    body = config if isinstance(config, str) else json.dumps(config)
    (directory / "config.json").write_text(body, encoding="utf-8")


def _members(*names):
    return {"members": [{"name": n, "agentId": f"agent-{n}", "agentType": "general-purpose"} for n in names]}


def _start(conn, agent_id, *, session=CALLER):
    return hook_flows.on_subagent_start(conn, {"claude_session_id": session, "agent_id": agent_id})


def _stop(conn, agent_id, *, session=CALLER):
    return hook_flows.on_subagent_stop(conn, {"claude_session_id": session, "agent_id": agent_id})


def _runs(conn, agent_id):
    return conn.execute(
        "SELECT * FROM execution_runs WHERE run_kind = 'subagent' AND agent_id = ? ORDER BY started_at, id",
        (agent_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# agent ID lane (AC2)
# ---------------------------------------------------------------------------


def test_addr_by_agent_id_open_subagent_same_session_passes(conn):
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    _start(conn, "a8c070c249972b5d4")

    result = _send(conn, "a8c070c249972b5d4")

    assert result["decision"] == "pass"
    assert result["target_kind"] == "in_session_subagent"


def test_addr_by_agent_id_ended_subagent_same_session_still_passes(conn):
    """A completed / stopped SubAgent is resumable by SendMessage: ended_at
    alone must not make it unaddressable."""
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    _start(conn, "a8c070c249972b5d4")
    _stop(conn, "a8c070c249972b5d4")
    assert _runs(conn, "a8c070c249972b5d4")[0]["ended_at"] is not None

    result = _send(conn, "a8c070c249972b5d4")

    assert result["decision"] == "pass"
    assert result["target_kind"] == "in_session_subagent"


def test_addr_by_agent_id_taskless_caller_session_still_passes(conn):
    """Session scope is the authority, not the caller's Task binding."""
    _start(conn, "agent-taskless")

    result = _send(conn, "agent-taskless")

    assert result["decision"] == "pass"
    assert result["target_kind"] == "in_session_subagent"


def test_addr_by_agent_id_legacy_open_unbound_row_still_passes(conn):
    """A pre-existing open row with claude_session_id NULL keeps matching by
    agent_id only (backward compatible)."""
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    service.start_execution_run(conn, run_kind="subagent", agent_id="legacy-agent")

    result = _send(conn, "legacy-agent")

    assert result["decision"] == "pass"
    assert result["target_kind"] == "in_session_subagent"


def test_addr_by_agent_id_lookup_is_the_single_ended_inclusive_function(conn):
    _start(conn, "agent-x")
    _stop(conn, "agent-x")

    assert service.find_open_execution_runs(conn, run_kind="subagent", agent_id="agent-x") == []
    found = service.find_addressable_subagent_runs(conn, claude_session_id=CALLER, agent_id="agent-x")
    assert len(found) == 1 and found[0]["ended_at"] is not None
    assert service.find_addressable_subagent_runs(conn, claude_session_id=OTHER, agent_id="agent-x") == []
    assert service.find_addressable_subagent_runs(conn, claude_session_id=None, agent_id="agent-x") == []
    assert service.find_addressable_subagent_runs(conn, claude_session_id=CALLER, agent_id="") == []


def test_addr_by_agent_id_subagent_row_records_parent_session_without_binding(conn):
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    result = _start(conn, "agent-parent")

    row = service.get_execution_run(conn, result["execution_run_id"])
    assert row["claude_session_id"] == CALLER
    assert row["binding_id"] is None
    assert row["run_kind"] == "subagent"


# ---------------------------------------------------------------------------
# teammate name lane (AC3 teammate branch)
# ---------------------------------------------------------------------------


def test_addr_by_name_teammate_in_team_config_passes(conn, teams_dir, teams_enabled):
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    _write_team(teams_dir, CALLER, _members("researcher", "reviewer"))

    result = _send(conn, "researcher")

    assert result["decision"] == "pass"
    assert result["target_kind"] == "in_session_subagent"


def test_addr_by_name_idle_teammate_passes_without_any_open_run(conn, teams_dir, teams_enabled):
    """Team membership, not a run's lifetime, is the teammate authority."""
    _write_team(teams_dir, CALLER, _members("idle-mate"))
    assert conn.execute("SELECT COUNT(*) FROM execution_runs").fetchone()[0] == 0

    result = _send(conn, "idle-mate")

    assert result["decision"] == "pass"


def test_addr_by_name_teammate_config_root_follows_claude_config_dir(conn, tmp_path, monkeypatch, teams_enabled):
    """Claude-GPT relocates the config root via CLAUDE_CONFIG_DIR; the team
    config is resolved from there, never a hardcoded ~/.claude."""
    monkeypatch.delenv(session_registry.TEAMS_DIR_ENV_VAR)
    root = tmp_path / "claude-gpt-root"
    monkeypatch.setenv(session_registry.CLAUDE_CONFIG_DIR_ENV_VAR, str(root))
    assert session_registry.resolve_teams_dir() == root / "teams"
    _write_team(root / "teams", CALLER, _members("gpt-mate"))

    assert _send(conn, "gpt-mate")["decision"] == "pass"


def test_addr_by_name_teams_dir_defaults_to_native_home_when_config_dir_unset(monkeypatch, tmp_path):
    monkeypatch.delenv(session_registry.TEAMS_DIR_ENV_VAR, raising=False)
    monkeypatch.delenv(session_registry.CLAUDE_CONFIG_DIR_ENV_VAR, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert session_registry.resolve_teams_dir() == tmp_path / "home" / ".claude" / "teams"


# ---------------------------------------------------------------------------
# must ASK (AC4)
# ---------------------------------------------------------------------------


def test_addr_must_ask_agent_type_only_is_not_an_address(conn):
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    _start(conn, "a8c070c249972b5d4")

    result = _send(conn, "general-purpose")

    assert result["decision"] == "ask"
    assert result["target_kind"] == "unknown_independent_session"


def test_addr_must_ask_ordinary_named_subagent_name_not_observable(conn):
    """Hooks never expose an ordinary SubAgent's `name`, so a name `to` for
    an ordinary SubAgent (started with only an agent_id) stays ASK."""
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    _start(conn, "a8c070c249972b5d4")

    result = _send(conn, "canaryone")

    assert result["decision"] == "ask"
    assert result["target_kind"] == "unknown_independent_session"


def test_addr_must_ask_same_agent_id_from_other_caller_session(conn):
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    _bind(conn, tab="tab-2", session=OTHER, ref_number=1)
    _start(conn, "agent-owned-by-other", session=OTHER)

    result = _send(conn, "agent-owned-by-other", session=CALLER)

    assert result["decision"] == "ask"
    assert result["target_kind"] == "unknown_independent_session"
    assert _send(conn, "agent-owned-by-other", session=OTHER)["decision"] == "pass"


def test_addr_must_ask_legacy_unbound_ended_row_is_not_addressable(conn):
    run = service.start_execution_run(conn, run_kind="subagent", agent_id="legacy-ended")
    service.end_execution_run(conn, run["id"])

    result = _send(conn, "legacy-ended")

    assert result["decision"] == "ask"


def test_addr_must_ask_legacy_unbound_row_never_grants_name_addressability(conn, teams_dir, teams_enabled):
    service.start_execution_run(conn, run_kind="subagent", agent_id="legacy-open")

    assert _send(conn, "some-name")["decision"] == "ask"


def test_addr_must_ask_no_caller_session_cannot_reach_session_bound_rows(conn):
    _start(conn, "agent-bound")

    result = hook_flows.on_pre_tool_use(conn, {"tool_name": "SendMessage", "to": "agent-bound"})

    assert result["decision"] == "ask"


def test_addr_must_ask_teammate_removed_from_members(conn, teams_dir, teams_enabled):
    _write_team(teams_dir, CALLER, _members("still-here"))

    assert _send(conn, "removed-mate")["decision"] == "ask"


def test_addr_must_ask_teammate_name_collision_in_members(conn, teams_dir, teams_enabled):
    _write_team(teams_dir, CALLER, _members("dup", "dup"))

    assert _send(conn, "dup")["decision"] == "ask"


def test_addr_must_ask_teammate_when_experimental_flag_disabled(conn, teams_dir):
    _write_team(teams_dir, CALLER, _members("mate"))

    result = _send(conn, "mate")

    assert result["decision"] == "ask"
    assert result["target_kind"] == "unknown_independent_session"


def test_addr_must_ask_teammate_config_of_other_session(conn, teams_dir, teams_enabled):
    _write_team(teams_dir, OTHER, _members("mate"))

    assert _send(conn, "mate", session=CALLER)["decision"] == "ask"
    assert _send(conn, "mate", session=OTHER)["decision"] == "pass"


def test_addr_must_ask_teammate_config_declares_different_lead_session(conn, teams_dir, teams_enabled):
    config = _members("mate")
    config["leadSessionId"] = OTHER
    _write_team(teams_dir, CALLER, config)

    assert _send(conn, "mate")["decision"] == "ask"


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        "[]",
        json.dumps({"members": "nope"}),
        json.dumps({"other": []}),
        json.dumps({"members": ["mate"]}),
        json.dumps({"members": [{"agentId": "x"}]}),
        json.dumps({"members": [{"name": ""}]}),
        json.dumps({"members": [{"name": 7}]}),
    ],
)
def test_addr_must_ask_teammate_config_unparsable_or_schema_mismatch(conn, teams_dir, teams_enabled, body):
    _write_team(teams_dir, CALLER, body)

    assert _send(conn, "mate")["decision"] == "ask"


def test_addr_must_ask_teammate_team_config_missing_or_teams_dir_absent(conn, tmp_path, monkeypatch, teams_enabled):
    assert _send(conn, "mate")["decision"] == "ask"
    monkeypatch.setenv(session_registry.TEAMS_DIR_ENV_VAR, str(tmp_path / "no-such-teams-dir"))
    assert _send(conn, "mate")["decision"] == "ask"


def test_addr_must_ask_teammate_name_that_also_resolves_to_independent_session(
    conn, teams_dir, sessions_dir, teams_enabled
):
    """Identity collision (teammate name == an independent session's name)
    must not be guessed -- ASK, even when the independent session would be
    same-Task."""
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    _bind(conn, tab="tab-2", session="sess-uuid-peer", ref_number=1)
    _write_registry(sessions_dir, pid=1, session_id="sess-uuid-peer", name="shared-name")
    _write_team(teams_dir, CALLER, _members("shared-name"))

    result = _send(conn, "shared-name")

    assert result["decision"] == "ask"
    assert result["target_kind"] == "unknown_independent_session"


def test_addr_must_ask_team_config_path_rejects_unsafe_session_id(teams_dir):
    for bad in ("", "../etc", "a/b", ".hidden", "x y"):
        assert team_config.team_config_path(bad, teams_dir=teams_dir) is None
    ok, reason = team_config.resolve_teammate_by_name("mate", "../x", teams_dir=teams_dir, require_flag=False)
    assert ok is False and reason == team_config.NO_SESSION


def test_addr_must_ask_unresolved_destination_stays_ask(conn):
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)

    result = _send(conn, "nobody-knows-this")

    assert result["decision"] == "ask"
    assert result["target_kind"] == "unknown_independent_session"


# ---------------------------------------------------------------------------
# SubagentStart refire idempotency (AC4)
# ---------------------------------------------------------------------------


def test_addr_refire_open_row_is_noop_without_integrity_error(conn):
    first = _start(conn, "agent-refire")
    events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    second = _start(conn, "agent-refire")

    assert second["execution_run_id"] == first["execution_run_id"]
    assert second["reason_code"] == "subagent_start_refire_noop"
    assert len(_runs(conn, "agent-refire")) == 1
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before


def test_addr_refire_after_ended_inserts_new_row_and_keeps_ended_at(conn):
    first = _start(conn, "agent-resumed")
    _stop(conn, "agent-resumed")
    ended_at = service.get_execution_run(conn, first["execution_run_id"])["ended_at"]
    assert ended_at is not None

    second = _start(conn, "agent-resumed")

    assert second["execution_run_id"] != first["execution_run_id"]
    assert second["reason_code"] == "subagent_started"
    assert service.get_execution_run(conn, first["execution_run_id"])["ended_at"] == ended_at
    rows = _runs(conn, "agent-resumed")
    assert len(rows) == 2
    # Same agent_id history is one addressable identity, never a collision.
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    assert _send(conn, "agent-resumed")["decision"] == "pass"


def test_addr_refire_without_agent_id_always_creates_new_row(conn):
    a = hook_flows.on_subagent_start(conn, {"claude_session_id": CALLER})
    b = hook_flows.on_subagent_start(conn, {"claude_session_id": CALLER})

    assert a["execution_run_id"] != b["execution_run_id"]
    assert len(service.find_open_execution_runs(conn, run_kind="subagent")) == 2


def test_addr_refire_service_reports_created_flag(conn):
    run, created = service.record_subagent_start(conn, claude_session_id=CALLER, agent_id="agent-flag")
    again, created_again = service.record_subagent_start(conn, claude_session_id=CALLER, agent_id="agent-flag")
    assert created is True and created_again is False and again["id"] == run["id"]


# ---------------------------------------------------------------------------
# independent-session lanes unchanged (AC5)
# ---------------------------------------------------------------------------


def test_addr_same_task_independent_session_by_registry_name_passes(conn, sessions_dir):
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    _bind(conn, tab="tab-2", session="sess-uuid-peer-alpha", ref_number=1)
    _write_registry(sessions_dir, pid=1001, session_id="sess-uuid-peer-alpha", name="peer-alpha")

    result = _send(conn, "peer-alpha")

    assert result["decision"] == "pass"
    assert result["target_kind"] == "same_task_independent_session"


def test_addr_same_task_independent_session_by_session_id_passes(conn):
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    _bind(conn, tab="tab-2", session="s2", ref_number=1)

    result = _send(conn, "s2")

    assert result["decision"] == "pass"
    assert result["target_kind"] == "same_task_independent_session"


def test_addr_cross_task_independent_session_by_registry_name_asks(conn, sessions_dir):
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    _bind(conn, tab="tab-2", session="sess-uuid-peer-beta", ref_number=2)
    _write_registry(sessions_dir, pid=1002, session_id="sess-uuid-peer-beta", name="peer-beta")

    result = _send(conn, "peer-beta")

    assert result["decision"] == "ask"
    assert result["target_kind"] == "known_cross_task_independent_session"


def test_addr_cross_task_still_asks_when_subagent_and_teammate_lanes_are_active(
    conn, sessions_dir, teams_dir, teams_enabled
):
    """The new lanes never relax the known cross-Task ASK."""
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    _bind(conn, tab="tab-2", session="s-cross", ref_number=2)
    _start(conn, "agent-in-session")
    _write_team(teams_dir, CALLER, _members("some-mate"))

    assert _send(conn, "s-cross")["target_kind"] == "known_cross_task_independent_session"
    assert _send(conn, "s-cross")["decision"] == "ask"
    assert _send(conn, "agent-in-session")["decision"] == "pass"
    assert _send(conn, "some-mate")["decision"] == "pass"


def test_addr_notify_when_idle_same_task_passes_and_cross_task_asks(conn):
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    _bind(conn, tab="tab-2", session="s-same", ref_number=1)
    _bind(conn, tab="tab-3", session="s-cross", ref_number=2)

    same = _send(conn, "s-same", notify_when_idle=True)
    cross = _send(conn, "s-cross", notify_when_idle=True)

    assert same["decision"] == "pass"
    assert cross["decision"] == "ask"


def test_addr_notify_when_idle_agent_id_lane_uses_session_scope(conn):
    _bind(conn, tab="tab-1", session=CALLER, ref_number=1)
    _start(conn, "agent-idle-ok")
    _start(conn, "agent-idle-other", session=OTHER)

    assert _send(conn, "agent-idle-ok", notify_when_idle=True)["decision"] == "pass"
    assert _send(conn, "agent-idle-other", notify_when_idle=True)["decision"] == "ask"


# ---------------------------------------------------------------------------
# real hook_entry.py subprocess chain (writer adapter -> guard reader)
# ---------------------------------------------------------------------------

_BODY_MARKER = "BODY-MARKER-DO-NOT-PERSIST"


def _hook_subprocess(event, hook_input, state_root, extra_env=None):
    import os
    import pathlib
    import subprocess
    import sys

    hook_entry = pathlib.Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "task_context" / "hook_entry.py"
    env = dict(os.environ)
    env.pop("HERDR_TAB_ID", None)
    env.pop("HERDR_PANE_ID", None)
    env["LOOP_TASK_CONTEXT_STATE_ROOT"] = str(state_root)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(hook_entry), event],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def test_addr_by_agent_id_real_hook_chain_subagent_start_then_send_message_emits_no_ask(
    state_root, sessions_dir, teams_dir
):
    env = {
        "HERDR_TAB_ID": "wV:t9",
        "HERDR_PANE_ID": "wV:p9",
        session_registry.SESSION_REGISTRY_DIR_ENV_VAR: str(sessions_dir),
        session_registry.TEAMS_DIR_ENV_VAR: str(teams_dir),
    }
    _hook_subprocess("SessionStart", {"session_id": CALLER, "source": "startup"}, state_root, env)
    started = _hook_subprocess(
        "SubagentStart",
        {"session_id": CALLER, "agent_id": "agent-e2e", "agent_type": "general-purpose"},
        state_root,
        env,
    )
    assert started.returncode == 0, started.stderr
    # resume / new-message refire must be a harmless no-op
    refire = _hook_subprocess(
        "SubagentStart",
        {"session_id": CALLER, "agent_id": "agent-e2e", "agent_type": "general-purpose"},
        state_root,
        env,
    )
    assert refire.returncode == 0, refire.stderr
    _hook_subprocess("SubagentStop", {"session_id": CALLER, "agent_id": "agent-e2e"}, state_root, env)

    def send(to, session=CALLER):
        return _hook_subprocess(
            "PreToolUse",
            {"session_id": session, "tool_name": "SendMessage", "tool_input": {"to": to, "message": _BODY_MARKER}},
            state_root,
            env,
        )

    ok = send("agent-e2e")
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.strip() == "", ok.stdout  # PASS emits nothing (no ASK)

    other = send("agent-e2e", session=OTHER)
    assert "permissionDecision" in other.stdout and '"ask"' in other.stdout
    assert "target_kind_unknown_independent_session" in other.stdout
    assert _BODY_MARKER not in other.stdout + other.stderr
