"""Issue #2625 AC6 -- `/task <target>` state-changing authority lives
exclusively in the `UserPromptExpansion` command lifecycle
(`command_name == "task"`), atomically superseding whatever Task/Activity is
currently ACTIVE, with explicit command-failure semantics for invalid /
unavailable / missing targets. Any other `command_name` is always a
non-mutating no-op pass-through."""

from __future__ import annotations

import task_context_hook_flows as hook_flows
import task_context_service as service


conn_holder = {}


def setup_function(_fn):
    conn_holder.clear()


def _start_session(tab_id: str, session_id: str, *, source: str = "startup") -> str:
    result = hook_flows.on_session_start(
        conn_holder["conn"], {"source": source, "herdr_tab_id": tab_id, "claude_session_id": session_id}
    )
    return result["binding_id"]


def _submit(session_id: str, tab_id: str, **fields):
    payload = {"herdr_tab_id": tab_id, "claude_session_id": session_id, **fields}
    return hook_flows.on_user_prompt_submit(conn_holder["conn"], payload)


def _expand(session_id: str | None, tab_id: str | None, command_name: str = "task", **fields):
    payload = {"herdr_tab_id": tab_id, "claude_session_id": session_id, "command_name": command_name, **fields}
    return hook_flows.on_user_prompt_expansion(conn_holder["conn"], payload)


def test_given_other_command_name_when_expanded_then_no_op_pass_through(conn):
    """`command_name != "task"` is not this module's concern at all -- must
    never mutate anything and must never be treated as a `/task` failure."""
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    result = _expand("s1", "tab-1", command_name="some-other-skill", command_args="whatever")
    assert result["decision"] == "pass"
    assert result["reason_code"] == "not_task_command"


def test_given_valid_github_target_when_task_expanded_then_atomic_rebind(conn):
    conn_holder["conn"] = conn
    binding_id = _start_session("tab-1", "s1")
    result = _expand(
        "s1", "tab-1", slash_task_target_repo="owner/repo", slash_task_target_ref_kind="issue",
        slash_task_target_ref_number=123,
    )
    assert result["decision"] == "pass"
    assert result["reason_code"] == "slash_task_rebind"
    task_id = result["task_id"]
    assert service.find_live_claim(conn, "owner/repo", "issue", 123)["task_id"] == task_id
    current_task_id, _, _ = service.get_current_task_activity_for_binding(conn, binding_id)
    assert current_task_id == task_id


def test_given_ad_hoc_title_when_task_expanded_then_new_ad_hoc_task_created(conn):
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    result = _expand("s1", "tab-1", slash_task_ad_hoc_title="ad-hoc work")
    assert result["decision"] == "pass"
    assert result["reason_code"] == "slash_task_rebind"
    assert service.count_live_task_ref_claims(conn, result["task_id"]) == 0


def test_given_active_task_a_with_live_ref_when_task_b_expanded_then_rebind_supersedes_unconditionally(conn):
    """AC6: `/task B` always supersedes whatever is ACTIVE, even while Task
    A's Activity is ACTIVE and Task A already owns a live ref -- the exact
    scenario that is merely advisory (never mutating) on ordinary
    UserPromptSubmit."""
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    task_a_result = _submit(
        "s1", "tab-1", classification_kind="EXPLICIT", target_repo="owner/repo", target_ref_kind="issue",
        target_ref_number=1,
    )
    task_a = task_a_result["task_id"]

    rebind_result = _expand(
        "s1", "tab-1", slash_task_target_repo="owner/other", slash_task_target_ref_kind="issue",
        slash_task_target_ref_number=99,
    )
    assert rebind_result["decision"] == "pass"
    assert rebind_result["reason_code"] == "slash_task_rebind"
    assert rebind_result["task_id"] != task_a

    binding_id = service.get_binding_by_current_session(conn, "s1")["id"]
    current_task_id, _, _ = service.get_current_task_activity_for_binding(conn, binding_id)
    assert current_task_id == rebind_result["task_id"]


def test_given_missing_target_when_task_expanded_then_explicit_command_failure_not_silently_ignored(conn):
    conn_holder["conn"] = conn
    _start_session("tab-1", "s1")
    result = _expand("s1", "tab-1")
    assert result["decision"] == "block"
    assert result["reason_code"] == "slash_task_missing_target"


def test_given_non_herdr_session_when_task_expanded_then_not_applicable_pass(conn):
    """A non-Herdr canonical interactive Claude session has no TabBinding to
    rebind -- this is "not applicable", not a target validation failure."""
    conn_holder["conn"] = conn
    result = _expand("s1", None, slash_task_target_repo="owner/repo", slash_task_target_ref_kind="issue",
                      slash_task_target_ref_number=1)
    assert result["decision"] == "pass"
    assert result["reason_code"] == "observe_only_non_herdr"


def test_given_no_binding_for_session_when_task_expanded_then_explicit_command_failure(conn):
    """Unlike ordinary UserPromptSubmit's fail-open default, an explicit
    `/task` command with an unresolvable Binding is surfaced as an explicit
    command failure -- it must never silently pretend to have succeeded."""
    conn_holder["conn"] = conn
    result = _expand(
        "unknown-session", "tab-1", slash_task_target_repo="owner/repo", slash_task_target_ref_kind="issue",
        slash_task_target_ref_number=1,
    )
    assert result["decision"] == "block"
    assert result["reason_code"] == "no_binding_for_session"
