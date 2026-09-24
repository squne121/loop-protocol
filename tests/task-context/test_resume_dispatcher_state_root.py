"""Issue #2569 AC15/AC16 -- resume dispatcher state-root resolution
(fixed repository identity, never a pane cwd) and failure SSOT (only
``binding.runtime_health = RESTORE_BLOCKED``, no second `Attention`
projection)."""

from __future__ import annotations

import os

import pytest

import task_context_config as config
import task_context_resume_dispatcher as dispatcher
import task_context_service as service


# ---------------------------------------------------------------------------
# AC15 -- dispatcher state-root resolution never uses a pane-supplied cwd
# ---------------------------------------------------------------------------


def test_given_state_root_override_when_resolving_dispatcher_state_root_then_matches_config_default(state_root):
    """The dispatcher's own resolution must agree with the canonical
    ``config.resolve_state_root()`` default (same env-driven override)."""
    assert dispatcher.resolve_dispatcher_state_root() == config.resolve_state_root()


def test_given_process_cwd_outside_any_git_repo_when_resolving_dispatcher_state_root_then_still_resolves(
    monkeypatch, tmp_path
):
    """AC15: simulate Herdr launching the dispatcher with the resumed
    pane's last-known cwd -- here, a directory that is not even inside a
    git repository at all, which would make a naive ``cwd=None``
    (process-cwd-based) resolution raise. The dispatcher must ignore this
    entirely and still resolve successfully from its own fixed repository
    identity."""
    outside_repo_dir = tmp_path / "not-a-git-repo"
    outside_repo_dir.mkdir()
    monkeypatch.chdir(outside_repo_dir)
    monkeypatch.delenv(config.STATE_ROOT_ENV_VAR, raising=False)
    monkeypatch.delenv(config.XDG_STATE_HOME_ENV_VAR, raising=False)

    # A naive cwd=None resolution would fail here (no git repo at cwd).
    with pytest.raises(Exception):
        config.resolve_state_root(cwd=None)

    # The dispatcher's own resolution is unaffected by the process cwd.
    root = dispatcher.resolve_dispatcher_state_root()
    assert "task-context" in str(root)
    assert "loop-protocol" in str(root)


def test_given_different_pane_cwd_values_when_resolving_dispatcher_state_root_then_result_is_identical(
    monkeypatch, tmp_path, state_root
):
    """AC15: resolving from two different simulated pane cwds must yield
    the byte-identical state root -- cwd is simply not an input."""
    first = dispatcher.resolve_dispatcher_state_root()

    other_cwd = tmp_path / "some-other-resumed-pane-cwd"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    second = dispatcher.resolve_dispatcher_state_root()

    assert first == second


def test_given_dispatcher_db_when_opened_then_lives_under_dispatcher_state_root(state_root):
    conn = dispatcher.open_dispatcher_db()
    try:
        db_file = dispatcher.resolve_dispatcher_state_root() / config.DB_FILE_NAME
        assert db_file.exists()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AC16 -- failure SSOT is runtime_health=RESTORE_BLOCKED only (no second
# Attention=NEEDS_HUMAN-shaped projection)
# ---------------------------------------------------------------------------


def _seed_invalid_managed_profile_binding(conn, *, herdr_locator: str, session_id: str) -> str:
    binding = service.create_binding(conn)
    binding_id = binding["id"]
    service.relocate_binding(conn, binding_id, herdr_locator)
    run = service.start_execution_run(
        conn,
        run_kind="native_operator",
        binding_id=binding_id,
        runtime_profile="claude_gpt_v1",  # mismatched -- native_operator run_kind never has this.
        resume_profile="claude_gpt_v1",
        claude_session_id=session_id,
    )
    service.set_binding_session(conn, binding_id, session_id, execution_run_id=run["id"])
    return binding_id


def test_given_restore_blocked_binding_when_reading_projection_then_attention_ssot_is_runtime_health_only(
    state_root,
):
    conn = dispatcher.open_dispatcher_db()
    try:
        binding_id = _seed_invalid_managed_profile_binding(
            conn, herdr_locator="ssot-tab-1", session_id="ssot-s1"
        )
        conn.close()

        decision = dispatcher.prepare_managed_resume("ssot-s1")
        assert decision.action == dispatcher.ACTION_RESTORE_BLOCKED
        assert decision.reason_code == "invalid_managed_profile"

        conn = dispatcher.open_dispatcher_db()
        binding = service.get_binding(conn, binding_id)
        assert binding["runtime_health"] == "RESTORE_BLOCKED"

        projection = service.get_current_projection_for_session(conn, "ssot-s1")
        # The ONLY failure signal is runtime_health -- the projection's
        # `attention` field is a pre-existing, unrelated (workflow-signal)
        # SSOT and must not be repurposed/overloaded as a second failure
        # channel by this dispatcher (AC16).
        assert projection["binding"]["runtime_health"] == "RESTORE_BLOCKED"
        assert projection["attention"] != "NEEDS_HUMAN"
    finally:
        conn.close()


def test_given_resume_decision_dataclass_when_inspecting_fields_then_no_attention_ssot_field_exists():
    """AC16: the ResumeDecision result shape itself must never carry a
    separate `attention`/`NEEDS_HUMAN`-shaped field -- RESTORE_BLOCKED
    status is entirely conveyed via `action`/`reason_code` plus the
    Binding's own `runtime_health` column."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(dispatcher.ResumeDecision)}
    assert "attention" not in field_names
    assert "needs_human" not in field_names


def test_given_restore_blocked_binding_when_dispatched_again_when_checking_ssot_then_idempotently_blocked(
    state_root,
):
    """AC16 idempotency: re-classifying an already-RESTORE_BLOCKED Binding
    must not invent a distinct/escalated failure state -- it stays
    RESTORE_BLOCKED via the same single field."""
    conn = dispatcher.open_dispatcher_db()
    try:
        binding_id = _seed_invalid_managed_profile_binding(
            conn, herdr_locator="ssot-tab-2", session_id="ssot-s2"
        )
    finally:
        conn.close()

    first = dispatcher.prepare_managed_resume("ssot-s2")
    second = dispatcher.prepare_managed_resume("ssot-s2")
    assert first.action == dispatcher.ACTION_RESTORE_BLOCKED
    assert second.action == dispatcher.ACTION_RESTORE_BLOCKED
    assert second.reason_code == "binding_already_restore_blocked"

    conn = dispatcher.open_dispatcher_db()
    try:
        binding = service.get_binding(conn, binding_id)
        assert binding["runtime_health"] == "RESTORE_BLOCKED"
    finally:
        conn.close()
