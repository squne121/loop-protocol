"""Issue #2569 AC13/AC14 -- Native profile migration contract.

``resolve_effective_runtime_profile()`` (read-time compatibility) and
``normalize_operator_profiles_for_new_run()`` (new-write normalization) are
the two halves of the contract:

- AC13: a legacy ``(native_operator, NULL, NULL)`` ExecutionRun row (current
  mainline's intentional ``operator_run_kind_and_profiles()`` default,
  unchanged, no DB backfill) resolves to effective profile
  ``native_claude_v1`` at read time. Every NEW Native ExecutionRun created
  from this Issue onward instead persists the explicit
  ``(native_claude_v1, native_claude_v1)`` pair.
- AC14: any managed ``(run_kind, runtime_profile, resume_profile)``
  combination that matches none of the fixed contract shapes resolves to
  ``invalid_managed_profile`` -- the resume dispatcher must RESTORE_BLOCKED
  on this, never fall back to plain Native.
"""

from __future__ import annotations

import task_context_config as config
import task_context_hook_flows as hook_flows
import task_context_service as service


# ---------------------------------------------------------------------------
# resolve_effective_runtime_profile() -- read-time compatibility (AC13/AC14)
# ---------------------------------------------------------------------------


def test_given_legacy_null_native_triple_when_resolving_effective_profile_then_native_claude_v1():
    assert (
        config.resolve_effective_runtime_profile("native_operator", None, None)
        == config.NATIVE_CLAUDE_RUNTIME_PROFILE
    )


def test_given_explicit_native_triple_when_resolving_effective_profile_then_native_claude_v1():
    assert (
        config.resolve_effective_runtime_profile(
            "native_operator", "native_claude_v1", "native_claude_v1"
        )
        == config.NATIVE_CLAUDE_RUNTIME_PROFILE
    )


def test_given_claude_gpt_triple_when_resolving_effective_profile_then_claude_gpt_v1():
    assert (
        config.resolve_effective_runtime_profile("claude_gpt", "claude_gpt_v1", "claude_gpt_v1")
        == config.CLAUDE_GPT_RUNTIME_PROFILE
    )


def test_given_native_run_kind_with_claude_gpt_profile_when_resolving_then_invalid_managed_profile():
    """AC14: a managed run_kind/profile combination outside the fixed
    contract must resolve to invalid_managed_profile, never silently to
    either concrete profile."""
    assert (
        config.resolve_effective_runtime_profile("native_operator", "claude_gpt_v1", "claude_gpt_v1")
        == config.INVALID_MANAGED_PROFILE
    )


def test_given_claude_gpt_run_kind_with_null_profiles_when_resolving_then_invalid_managed_profile():
    """AC14: claude_gpt run_kind never has the NULL/NULL legacy-compat
    carve-out -- that carve-out is exclusively for native_operator."""
    assert config.resolve_effective_runtime_profile("claude_gpt", None, None) == config.INVALID_MANAGED_PROFILE


def test_given_mismatched_runtime_and_resume_profile_when_resolving_then_invalid_managed_profile():
    assert (
        config.resolve_effective_runtime_profile(
            "native_operator", "native_claude_v1", None
        )
        == config.INVALID_MANAGED_PROFILE
    )
    assert (
        config.resolve_effective_runtime_profile(
            "claude_gpt", "claude_gpt_v1", "native_claude_v1"
        )
        == config.INVALID_MANAGED_PROFILE
    )


def test_given_unrecognized_profile_string_when_resolving_then_invalid_managed_profile():
    assert (
        config.resolve_effective_runtime_profile("native_operator", "some_future_profile", "some_future_profile")
        == config.INVALID_MANAGED_PROFILE
    )


# ---------------------------------------------------------------------------
# normalize_operator_profiles_for_new_run() -- new-write normalization (AC13)
# ---------------------------------------------------------------------------


def test_given_legacy_null_native_triple_when_normalizing_for_new_run_then_explicit_native_claude_v1():
    assert config.normalize_operator_profiles_for_new_run("native_operator", None, None) == (
        "native_operator",
        "native_claude_v1",
        "native_claude_v1",
    )


def test_given_claude_gpt_triple_when_normalizing_for_new_run_then_unchanged():
    assert config.normalize_operator_profiles_for_new_run("claude_gpt", "claude_gpt_v1", "claude_gpt_v1") == (
        "claude_gpt",
        "claude_gpt_v1",
        "claude_gpt_v1",
    )


def test_given_already_explicit_native_triple_when_normalizing_for_new_run_then_unchanged():
    assert config.normalize_operator_profiles_for_new_run(
        "native_operator", "native_claude_v1", "native_claude_v1"
    ) == ("native_operator", "native_claude_v1", "native_claude_v1")


# ---------------------------------------------------------------------------
# Integration: SessionStart persists the explicit pair on NEW rows (AC13)
# ---------------------------------------------------------------------------


def test_given_startup_new_binding_when_session_start_then_new_native_run_has_explicit_profile(conn):
    result = hook_flows.on_session_start(
        conn, {"source": "startup", "herdr_tab_id": "migration-tab-1", "claude_session_id": "m1"}
    )
    binding_id = result["binding_id"]
    runs = service.find_open_execution_runs(conn, binding_id=binding_id, run_kind="native_operator")
    assert len(runs) == 1
    assert runs[0]["runtime_profile"] == "native_claude_v1"
    assert runs[0]["resume_profile"] == "native_claude_v1"
    # The persisted row's effective profile, resolved back through the
    # read-time contract, must round-trip to the same explicit profile.
    assert (
        config.resolve_effective_runtime_profile(
            runs[0]["run_kind"], runs[0]["runtime_profile"], runs[0]["resume_profile"]
        )
        == config.NATIVE_CLAUDE_RUNTIME_PROFILE
    )
