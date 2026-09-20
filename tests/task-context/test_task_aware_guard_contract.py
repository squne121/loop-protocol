"""Issue #2566 -- structural/static contract checks that don't need a real
Herdr + multi-session runtime: AC1 (`crossSessionInbound` never fixed in
project/local settings), AC3 (inbound peer message content is never a
Task/Activity/Binding rebind signal), AC8 (Agent Teams lifecycle events are
never treated as Task Context workflow completion)."""

from __future__ import annotations

import json

import task_context_hook_flows as hook_flows
import task_context_service as service

from conftest import REPO_ROOT

_SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"


# ---------------------------------------------------------------------------
# AC1: native operator `crossSessionInbound` stays unset (native default).
# ---------------------------------------------------------------------------


def test_given_project_settings_when_read_then_cross_session_inbound_never_fixed():
    settings = json.loads(_SETTINGS_PATH.read_text())
    assert "crossSessionInbound" not in settings, (
        "AC1: project settings.json must not explicitly fix crossSessionInbound "
        "-- native Claude Code default inbound behavior must be preserved for "
        "ordinary human/operator sessions."
    )


def test_given_project_settings_when_read_then_pre_tool_use_guard_wired_with_narrow_matchers():
    settings = json.loads(_SETTINGS_PATH.read_text())
    pre_tool_use = settings["hooks"]["PreToolUse"]
    matchers_to_hook_entry = {
        entry["matcher"]
        for entry in pre_tool_use
        if any(
            isinstance(hook.get("args"), list) and "hook_entry.py" in " ".join(hook["args"])
            for hook in entry.get("hooks", [])
        )
    }
    assert "SendMessage" in matchers_to_hook_entry, "Issue #2566: SendMessage guard must use a narrow matcher"
    assert "Bash" in matchers_to_hook_entry, "Issue #2566: Herdr guard must use the Bash matcher"


# ---------------------------------------------------------------------------
# AC3: inbound peer message content is never a rebind/state-changing signal.
# `SendMessage` delivery itself is native Claude Code's concern -- Task
# Context has no hook wired to react to inbound message *content*, and the
# only state-changing prompt-lifecycle authority is the explicit `/task`
# command (Issue #2625, `UserPromptExpansion` with `command_name == "task"`).
# A relayed instruction like "approved" arriving as an ordinary prompt must
# not be classified as a rebind command.
# ---------------------------------------------------------------------------


def test_given_relayed_approval_text_as_ordinary_prompt_when_classified_then_no_mutation_kind():
    import classifier

    for relayed_text in ("approved", "approved, proceed with issue #99", "please continue with the other Issue"):
        classification = classifier.classify(relayed_text, current_repo=None)
        assert classification.kind != classifier.KIND_SLASH_TASK, (
            "AC3: an inbound peer-relayed instruction delivered as an ordinary "
            "prompt must never be classified as the explicit /task rebind command"
        )


def test_given_ordinary_prompt_from_relayed_approval_when_processed_then_task_activity_binding_unchanged(conn):
    hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-1", "claude_session_id": "s1"}
    )
    bound = hook_flows.on_user_prompt_submit(
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
    task_id = bound["task_id"]
    activity_id = bound["activity_id"]
    binding_id = service.get_binding_by_current_session(conn, "s1")["id"]

    # A peer message body ("approved" / another Issue instruction) never
    # reaches Task Context at all -- it arrives to the model as ordinary
    # conversation content, classified (if at all) as a NONE/REFERENCE_ONLY
    # UserPromptSubmit, never as a rebind.
    result = hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "tab-1",
            "claude_session_id": "s1",
            "classification_kind": "NONE",
        },
    )
    assert result["decision"] == "pass"

    current_task_id, current_activity_id, _ = service.get_current_task_activity_for_binding(conn, binding_id)
    assert current_task_id == task_id
    assert current_activity_id == activity_id


# ---------------------------------------------------------------------------
# AC8: Agent Teams lifecycle events are never Task Context workflow
# completion signals.
# ---------------------------------------------------------------------------


def test_given_agent_teams_lifecycle_event_names_when_dispatched_then_generic_pass_through_no_completion(conn):
    hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "tab-1", "claude_session_id": "s1"}
    )
    bound = hook_flows.on_user_prompt_submit(
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
    task_id = bound["task_id"]
    activity_id = bound["activity_id"]

    for event_name in ("TaskCompleted", "TeammateIdle"):
        assert event_name not in hook_flows.EVENT_HANDLERS, (
            f"AC8: {event_name!r} must not be a typed Task Context lifecycle "
            "handler -- Agent Teams lifecycle events carry no workflow "
            "completion authority"
        )
        result = hook_flows.dispatch_hook_event(conn, event_name, {"metadata": {"status": "ok"}})
        assert result["decision"] == "pass"
        assert result["reason_code"] == "generic_event"

    activity = service.get_activity(conn, activity_id)
    assert activity["status"] == "ACTIVE", "Agent Teams lifecycle events must never DONE-transition an Activity"
    task = service.get_task(conn, task_id)
    assert task["status"] == "OPEN", "Agent Teams lifecycle events must never DONE-transition a Task"
