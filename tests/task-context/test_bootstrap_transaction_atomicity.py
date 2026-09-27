"""PR #2795 review fix_delta P1-A (comment 5852749710, finding 1):
`service.bootstrap_binding_and_bind_target` / `service.bootstrap_binding_with_run`
must run the whole "create Binding -> relocate -> start ExecutionRun ->
attach session -> bootstrap event -> resolve-or-create Task -> ensure ACTIVE
Activity -> attach run -> rebind event -> bump projection" sequence inside
ONE transaction, so a failure injected at any point rolls back everything --
never leaving a half-applied Binding/RuntimeLocation/ExecutionRun/Task/event/
projection-outbox row behind.
"""

from __future__ import annotations

import pytest

import task_context_errors as errors
import task_context_hook_flows as hook_flows
import task_context_service as service


def _table_row_counts(conn) -> dict[str, int]:
    tables = (
        "tab_bindings",
        "runtime_locations",
        "execution_runs",
        "tasks",
        "activities",
        "task_ref_claims",
        "events",
        "projection_outbox",
    )
    return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def test_given_late_stage_failure_when_bootstrap_and_bind_target_then_everything_rolls_back(conn, monkeypatch):
    """Inject a failure in the target-bind half (`_finish_binding_mutation_tx`,
    which owns attach-run/append-event/bump-projection) of the single atomic
    `bootstrap_binding_and_bind_target` transaction. Before this fix_delta,
    bootstrap (create Binding/RuntimeLocation/ExecutionRun/session-claim/
    event) and bind (Task/Activity/run-attach/event/projection) were two
    separately-committing top-level calls -- a failure here would have left
    the bootstrap half fully committed with no Task attached (the
    `origin_task_unattached`-shaped half-applied state the review comment
    identifies)."""
    before = _table_row_counts(conn)

    def _boom(*args, **kwargs):
        raise errors.ValidationError("injected failure_delta_p1a")

    monkeypatch.setattr(service, "_finish_binding_mutation_tx", _boom)

    with pytest.raises(errors.ValidationError):
        service.bootstrap_binding_and_bind_target(
            conn,
            herdr_locator="tab-boom",
            location_fields={"cwd": None, "worktree": None, "branch": None},
            claude_session_id="boom-session",
            run_kind="native_operator",
            runtime_profile=None,
            resume_profile=None,
            bootstrap_event_type="hook:UserPromptExpansion",
            bootstrap_reason_code="slash_task_bootstrap_unbound_session",
            bind_event_type="hook:UserPromptExpansion",
            bind_reason_code="slash_task_rebind",
            target_repo="owner/repo",
            target_ref_kind="issue",
            target_ref_number=99,
        )

    after = _table_row_counts(conn)
    assert after == before, (
        "a failure in the bind half must roll back the bootstrap half too "
        f"(no orphan rows): before={before} after={after}"
    )
    # No Binding claims this session -- the whole bootstrap never happened.
    with pytest.raises(errors.NotFoundError):
        service.get_binding_by_current_session(conn, "boom-session")


def test_given_late_stage_failure_when_bootstrap_binding_with_run_then_everything_rolls_back(conn, monkeypatch):
    """Same failure-injection shape as above, but for the SessionStart-shared
    `bootstrap_binding_with_run` composite (used by `on_session_start`'s
    brand-new-Tab path and formerly by the now-removed
    `bootstrap_unbound_session` helper): inject a failure in the final
    `_append_event_tx` step and assert the earlier create-binding/relocate/
    start-run/attach-session steps also roll back."""
    before = _table_row_counts(conn)

    def _boom(*args, **kwargs):
        raise errors.ValidationError("injected failure_delta_p1a_run")

    monkeypatch.setattr(service, "_append_event_tx", _boom)

    with pytest.raises(errors.ValidationError):
        service.bootstrap_binding_with_run(
            conn,
            herdr_locator="tab-boom-2",
            location_fields={"cwd": None, "worktree": None, "branch": None},
            claude_session_id="boom-session-2",
            run_kind="native_operator",
            runtime_profile=None,
            resume_profile=None,
            event_type="hook:SessionStart",
            reason_code="startup_new_binding",
            evict_foreign_holder=True,
        )

    after = _table_row_counts(conn)
    assert after == before
    with pytest.raises(errors.NotFoundError):
        service.get_binding_by_current_session(conn, "boom-session-2")


def test_given_late_stage_failure_when_task_expanded_bootstrap_then_hook_flow_rolls_back_too(conn, monkeypatch):
    """End-to-end through the actual `on_user_prompt_expansion` hook flow
    entry point (not just the service-layer composite directly), injecting
    the failure at the same point a real `bind_target_to_binding` write
    failure would occur."""
    before = _table_row_counts(conn)

    def _boom(*args, **kwargs):
        raise errors.ValidationError("injected failure_delta_p1a_hookflow")

    monkeypatch.setattr(service, "_finish_binding_mutation_tx", _boom)

    with pytest.raises(errors.ValidationError):
        hook_flows.on_user_prompt_expansion(
            conn,
            {
                "command_name": "task",
                "herdr_tab_id": "tab-boom-3",
                "claude_session_id": "boom-session-3",
                "slash_task_target_repo": "owner/repo",
                "slash_task_target_ref_kind": "issue",
                "slash_task_target_ref_number": 100,
            },
        )

    after = _table_row_counts(conn)
    assert after == before
    with pytest.raises(errors.NotFoundError):
        service.get_binding_by_current_session(conn, "boom-session-3")
