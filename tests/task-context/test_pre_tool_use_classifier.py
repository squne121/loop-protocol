"""Issue #2566 -- pure Herdr Bash-command structural field reduction
(`pre_tool_use_classifier.py`, adapter-side, no DB/I/O)."""

from __future__ import annotations

import pre_tool_use_classifier as ptu


# ---------------------------------------------------------------------------
# looks_like_herdr_command (hot-path early-exit check)
# ---------------------------------------------------------------------------


def test_given_herdr_command_when_checking_looks_like_herdr_then_true():
    assert ptu.looks_like_herdr_command("herdr pane read p1")


def test_given_non_herdr_command_when_checking_looks_like_herdr_then_false():
    assert not ptu.looks_like_herdr_command("git status")
    assert not ptu.looks_like_herdr_command("herdrsomethingelse pane read p1")
    assert not ptu.looks_like_herdr_command("")


# ---------------------------------------------------------------------------
# parse_herdr_command -- discovery (metadata-only)
# ---------------------------------------------------------------------------


def test_given_agent_list_when_parsed_then_discovery_category():
    parsed = ptu.parse_herdr_command("herdr agent list")
    assert parsed is not None
    assert parsed.category == ptu.CATEGORY_DISCOVERY
    assert parsed.operation == "agent_list"


def test_given_pane_get_when_parsed_then_discovery_category_with_locator():
    parsed = ptu.parse_herdr_command("herdr pane get p1")
    assert parsed is not None
    assert parsed.category == ptu.CATEGORY_DISCOVERY
    assert parsed.target_locator == "p1"


# ---------------------------------------------------------------------------
# parse_herdr_command -- content-read
# ---------------------------------------------------------------------------


def test_given_pane_read_when_parsed_then_content_read_category_with_locator():
    parsed = ptu.parse_herdr_command("herdr pane read p1 --lines 50")
    assert parsed is not None
    assert parsed.category == ptu.CATEGORY_CONTENT_READ
    assert parsed.operation == "pane_read"
    assert parsed.target_locator == "p1"
    assert parsed.machine_scoped is False


def test_given_pane_wait_output_when_parsed_then_content_read_category():
    parsed = ptu.parse_herdr_command("herdr pane wait-output p1 --timeout 5000")
    assert parsed is not None
    assert parsed.category == ptu.CATEGORY_CONTENT_READ
    assert parsed.operation == "pane_wait-output"


def test_given_terminal_session_observe_when_parsed_then_content_read_category_three_token_prefix():
    parsed = ptu.parse_herdr_command("herdr terminal session observe s1")
    assert parsed is not None
    assert parsed.category == ptu.CATEGORY_CONTENT_READ
    assert parsed.operation == "terminal_session_observe"
    assert parsed.target_locator == "s1"


def test_given_agent_read_when_parsed_then_content_read_category():
    parsed = ptu.parse_herdr_command("herdr agent read my-agent --source recent-unwrapped")
    assert parsed is not None
    assert parsed.category == ptu.CATEGORY_CONTENT_READ
    assert parsed.target_locator == "my-agent"


# ---------------------------------------------------------------------------
# parse_herdr_command -- control
# ---------------------------------------------------------------------------


def test_given_agent_prompt_when_parsed_then_control_category():
    parsed = ptu.parse_herdr_command('herdr agent prompt my-agent "do something" --wait')
    assert parsed is not None
    assert parsed.category == ptu.CATEGORY_CONTROL
    assert parsed.target_locator == "my-agent"


def test_given_pane_send_keys_when_parsed_then_control_category():
    parsed = ptu.parse_herdr_command("herdr pane send-keys p1 enter")
    assert parsed is not None
    assert parsed.category == ptu.CATEGORY_CONTROL


def test_given_agent_attach_when_parsed_then_control_category():
    parsed = ptu.parse_herdr_command("herdr agent attach my-agent --takeover")
    assert parsed is not None
    assert parsed.category == ptu.CATEGORY_CONTROL
    assert parsed.operation == "agent_attach"
    assert parsed.target_locator == "my-agent"


def test_given_terminal_attach_when_parsed_then_control_category():
    parsed = ptu.parse_herdr_command("herdr terminal attach s1 --takeover")
    assert parsed is not None
    assert parsed.category == ptu.CATEGORY_CONTROL
    assert parsed.operation == "terminal_attach"
    assert parsed.target_locator == "s1"


# ---------------------------------------------------------------------------
# parse_herdr_command -- machine scoping / unresolved / out-of-scope
# ---------------------------------------------------------------------------


def test_given_machine_flag_present_when_parsed_then_machine_scoped_true():
    parsed = ptu.parse_herdr_command("herdr agent prompt my-agent hi --machine other-host")
    assert parsed is not None
    assert parsed.machine_scoped is True


def test_given_unguarded_herdr_subcommand_when_parsed_then_none():
    """`herdr workspace create` / `herdr session stop` etc. are outside the
    finite guarded set (Issue #2566 In Scope) -- no decision at all."""
    assert ptu.parse_herdr_command("herdr workspace create --cwd /tmp/x") is None
    assert ptu.parse_herdr_command("herdr session stop my-session") is None


def test_given_bare_herdr_when_parsed_then_none():
    assert ptu.parse_herdr_command("herdr") is None


def test_given_unparseable_command_when_parsed_then_none():
    assert ptu.parse_herdr_command('herdr agent prompt "unterminated quote') is None


def test_given_missing_target_locator_when_parsed_then_locator_is_none():
    parsed = ptu.parse_herdr_command("herdr pane read")
    assert parsed is not None
    assert parsed.target_locator is None


# ---------------------------------------------------------------------------
# parse_herdr_command -- leading global selectors (fix_delta P1-A: real
# Herdr invocations place `--session <name>` / `--machine <label-or-id>`
# *before* the subcommand, not after it).
# ---------------------------------------------------------------------------


def test_given_session_prefixed_pane_read_when_parsed_then_content_read_category():
    parsed = ptu.parse_herdr_command("herdr --session my-session pane read p1 --lines 50")
    assert parsed is not None
    assert parsed.category == ptu.CATEGORY_CONTENT_READ
    assert parsed.operation == "pane_read"
    assert parsed.target_locator == "p1"
    assert parsed.machine_scoped is False


def test_given_session_prefixed_pane_send_text_when_parsed_then_control_category():
    parsed = ptu.parse_herdr_command("herdr --session my-session pane send-text p1 hello")
    assert parsed is not None
    assert parsed.category == ptu.CATEGORY_CONTROL
    assert parsed.operation == "pane_send-text"
    assert parsed.target_locator == "p1"


def test_given_machine_prefixed_agent_prompt_when_parsed_then_control_category_machine_scoped():
    parsed = ptu.parse_herdr_command("herdr --machine other-host agent prompt my-agent hi --wait")
    assert parsed is not None
    assert parsed.category == ptu.CATEGORY_CONTROL
    assert parsed.operation == "agent_prompt"
    assert parsed.target_locator == "my-agent"
    assert parsed.machine_scoped is True
