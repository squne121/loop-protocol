"""Issue #2567 AC1/AC3/AC4 -- Claude-GPT `LOOP_TASK_CONTEXT_RUNTIME_VARIANT`
integration into the SessionStart ExecutionRun lifecycle.

- AC4: a SessionStart under `LOOP_TASK_CONTEXT_RUNTIME_VARIANT=claude_gpt`
  records `run_kind=claude_gpt` / `runtime_profile=claude_gpt_v1` /
  `resume_profile=claude_gpt_v1` on the ExecutionRun (never
  `native_operator`, never a caller-suppliable arbitrary profile string).
- AC1: Binding/Task/Activity identity survives a runtime-flavor switch
  across a clean quit (ended run) and across an abnormal-termination stale
  open run -- the stale/open/latest run lookup considers both
  `native_operator` and `claude_gpt` (never just the kind that happens to be
  starting this SessionStart).
- Unset/any-other-value `LOOP_TASK_CONTEXT_RUNTIME_VARIANT` continues to
  produce the pre-existing native_operator behavior unchanged (regression
  guard for the existing native test suite)."""

from __future__ import annotations

import task_context_config as config
import task_context_hook_flows as hook_flows
import task_context_service as service


def _set_claude_gpt_variant(monkeypatch) -> None:
    monkeypatch.setenv(config.RUNTIME_VARIANT_ENV_VAR, "claude_gpt")


def test_given_claude_gpt_variant_when_session_start_new_binding_then_run_kind_and_profiles_recorded(
    conn, monkeypatch
):
    _set_claude_gpt_variant(monkeypatch)

    result = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "gpt-tab-1", "claude_session_id": "gpt-s1"}
    )
    binding_id = result["binding_id"]
    assert binding_id is not None

    runs = service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="claude_gpt")
    assert len(runs) == 1
    run = runs[0]
    assert run["run_kind"] == "claude_gpt"
    assert run["runtime_profile"] == "claude_gpt_v1"
    assert run["resume_profile"] == "claude_gpt_v1"
    assert run["claude_session_id"] == "gpt-s1"
    # Never also present under native_operator.
    assert service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="native_operator") == []


def test_given_runtime_variant_unset_when_session_start_new_binding_then_native_operator_unchanged(conn):
    result = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "native-tab-1", "claude_session_id": "n1"}
    )
    binding_id = result["binding_id"]
    runs = service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="native_operator")
    assert len(runs) == 1
    assert runs[0]["run_kind"] == "native_operator"
    # Issue #2569 AC13: every NEW Native ExecutionRun from this Issue onward
    # records the explicit `native_claude_v1` profile pair instead of the
    # legacy NULL/NULL shape (read-time compatibility for pre-#2569 rows is
    # covered separately by test_native_profile_migration.py; no existing
    # row is backfilled by this normalization).
    assert runs[0]["runtime_profile"] == "native_claude_v1"
    assert runs[0]["resume_profile"] == "native_claude_v1"


def test_given_native_run_cleanly_ended_when_resuming_under_claude_gpt_variant_then_task_identity_preserved(
    conn, monkeypatch
):
    """AC1: a Native Claude session bound this Tab to a Task, then cleanly
    `/quit`'d (ended its native_operator run). A later Claude-GPT launch on
    the SAME live Tab must restore the same Task/Activity -- and must record
    its own new run as claude_gpt, not native_operator."""
    started = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "switch-tab-1", "claude_session_id": "n1"}
    )
    binding_id = started["binding_id"]
    prompt_result = hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "switch-tab-1",
            "claude_session_id": "n1",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/repo",
            "target_ref_kind": "issue",
            "target_ref_number": 42,
        },
    )
    task_id = prompt_result["task_id"]
    hook_flows.on_session_end(conn, {"claude_session_id": "n1"})
    assert not service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="native_operator")

    _set_claude_gpt_variant(monkeypatch)
    restored = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "switch-tab-1", "claude_session_id": "g1"}
    )
    assert restored["binding_id"] == binding_id
    assert restored["task_id"] == task_id

    gpt_runs = service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="claude_gpt")
    assert len(gpt_runs) == 1
    assert gpt_runs[0]["task_id"] == task_id
    assert gpt_runs[0]["runtime_profile"] == "claude_gpt_v1"


def test_given_stale_open_native_run_when_restarting_under_claude_gpt_variant_then_self_heals_across_kinds(
    conn, monkeypatch
):
    """AC1(d) cross-kind counterpart: a stale OPEN native_operator run (prior
    abnormal termination, SessionEnd never fired) must still be found and
    ended even though this SessionStart is now starting a claude_gpt run --
    never leaving two concurrently-open managed runs on the same binding,
    and never silently ignoring the stale run just because its run_kind
    doesn't match the incoming variant."""
    started = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "switch-tab-2", "claude_session_id": "n1"}
    )
    binding_id = started["binding_id"]
    prompt_result = hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "switch-tab-2",
            "claude_session_id": "n1",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/repo",
            "target_ref_kind": "issue",
            "target_ref_number": 43,
        },
    )
    task_id = prompt_result["task_id"]
    # No on_session_end -- simulates an abnormal termination that leaves the
    # native_operator run OPEN.
    assert len(service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="native_operator")) == 1

    _set_claude_gpt_variant(monkeypatch)
    restored = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "switch-tab-2", "claude_session_id": "g1"}
    )
    assert restored["binding_id"] == binding_id
    assert restored["task_id"] == task_id

    # The stale native_operator run is now ended (self-healed), and exactly
    # one open managed run exists total, and it is the new claude_gpt one.
    assert service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="native_operator") == []
    gpt_runs = service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="claude_gpt")
    assert len(gpt_runs) == 1
    assert gpt_runs[0]["task_id"] == task_id


def test_given_claude_gpt_run_ended_when_resuming_under_native_variant_then_task_identity_preserved(
    conn, monkeypatch
):
    """AC1, reverse direction: a Claude-GPT session bound the Task, cleanly
    ended; a later plain (native) launch on the same Tab restores the same
    Task/Activity via the same cross-kind lookup."""
    _set_claude_gpt_variant(monkeypatch)
    started = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "switch-tab-3", "claude_session_id": "g1"}
    )
    binding_id = started["binding_id"]
    prompt_result = hook_flows.on_user_prompt_submit(
        conn,
        {
            "herdr_tab_id": "switch-tab-3",
            "claude_session_id": "g1",
            "classification_kind": "EXPLICIT",
            "target_repo": "owner/repo",
            "target_ref_kind": "issue",
            "target_ref_number": 44,
        },
    )
    task_id = prompt_result["task_id"]
    hook_flows.on_session_end(conn, {"claude_session_id": "g1"})
    assert not service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="claude_gpt")

    monkeypatch.delenv(config.RUNTIME_VARIANT_ENV_VAR, raising=False)
    restored = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "switch-tab-3", "claude_session_id": "n2"}
    )
    assert restored["binding_id"] == binding_id
    assert restored["task_id"] == task_id
    native_runs = service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="native_operator")
    assert len(native_runs) == 1
    # Issue #2569 AC13: this is a NEW Native ExecutionRun row (started fresh
    # by this restore), so it records the explicit `native_claude_v1` pair.
    assert native_runs[0]["runtime_profile"] == "native_claude_v1"


# ---------------------------------------------------------------------------
# Issue #2567 AC4: task_context_service._attach_or_start_binding_run_tx
# degrade path (bind_target_to_binding with no execution_run_id yet) is also
# variant-aware, not hardcoded to native_operator.
# ---------------------------------------------------------------------------


def test_given_claude_gpt_variant_when_bind_target_with_no_open_run_then_degrade_path_uses_claude_gpt(
    conn, monkeypatch
):
    _set_claude_gpt_variant(monkeypatch)
    binding = service.create_binding(conn)

    result = service.bind_target_to_binding(
        conn,
        binding_id=binding["id"],
        execution_run_id=None,
        repo="owner/repo",
        ref_kind="issue",
        ref_number=777,
        reason_code="autobind",
    )
    run = service.get_execution_run(conn, result["execution_run_id"])
    assert run["run_kind"] == "claude_gpt"
    assert run["runtime_profile"] == "claude_gpt_v1"
    assert run["resume_profile"] == "claude_gpt_v1"
