"""Tests for agy_permission_policy.py (Issue #1705).

Covers AC1-AC12: AGY profile-scoped isolated permission policy generation,
hostile-global-settings adversarial precedence, secret-safe denied-attempt
recording, expected/denied/unexpected tool-call classification, wrapper
Serena event exclusion, retrieval/analysis actor field preservation, and
credential-file non-copying in the isolated workspace.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading helper (hermetic, no side-effects) -- mirrors the pattern
# used by test_agy_provider.py for run_gemini_headless.py.
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "agy_permission_policy.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("agy_permission_policy", _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register before exec so `dataclasses` can resolve `cls.__module__` via
    # sys.modules while processing `@dataclass` decorators in the module.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


app = _load_module()

ALL_PROFILES = [
    app.NO_TOOLS_PROFILE,
    app.LOCAL_ASSET_RESEARCH_PROFILE,
    app.GROUNDED_RESEARCH_PROFILE,
    app.PROPOSAL_ONLY_PROFILE,
]

ALL_DENY_PROFILES = [
    app.NO_TOOLS_PROFILE,
    app.LOCAL_ASSET_RESEARCH_PROFILE,
    app.PROPOSAL_ONLY_PROFILE,
]


# ---------------------------------------------------------------------------
# AC1: no_tools profile denies all AGY direct tools
# ---------------------------------------------------------------------------


def test_no_tools_profile_denies_all_agy_direct_tools() -> None:
    policy = app.build_workspace_permission_policy(app.NO_TOOLS_PROFILE)
    assert policy["permissions"]["allow"] == []
    assert policy["permissions"]["default"] == "deny"
    assert set(policy["permissions"]["deny"]) == app.AGY_DIRECT_TOOL_NAMES
    assert app.profile_allowed_tools(app.NO_TOOLS_PROFILE) == frozenset()
    for tool_name in sorted(app.AGY_DIRECT_TOOL_NAMES):
        assert app.resolve_tool_permission(app.NO_TOOLS_PROFILE, tool_name) == "deny"


# ---------------------------------------------------------------------------
# AC2: local_asset_research denies all AGY direct tools, wrapper-only retrieval
# ---------------------------------------------------------------------------


def test_local_asset_research_denies_all_agy_direct_tools_wrapper_only() -> None:
    policy = app.build_workspace_permission_policy(app.LOCAL_ASSET_RESEARCH_PROFILE)
    assert policy["permissions"]["allow"] == []
    assert set(policy["permissions"]["deny"]) == app.AGY_DIRECT_TOOL_NAMES
    for tool_name in sorted(app.AGY_DIRECT_TOOL_NAMES):
        assert app.resolve_tool_permission(app.LOCAL_ASSET_RESEARCH_PROFILE, tool_name) == "deny"

    # wrapper-side Serena retrieval is a separate channel, not an AGY direct
    # tool call, and is excluded from the AGY direct classification entirely.
    events = [
        {"tool_name": "find_symbol", "source": app.WRAPPER_SERENA_SOURCE, "executed": True},
        {"tool_name": "shell", "source": app.AGY_DIRECT_SOURCE, "executed": False},
    ]
    result = app.classify_tool_call_events(app.LOCAL_ASSET_RESEARCH_PROFILE, events)
    assert result["agy_direct_tool_calls_count"] == 0
    assert len(result["wrapper_events"]) == 1
    assert result["denied_tool_calls_count"] == 1


# ---------------------------------------------------------------------------
# AC3: grounded_research allows exactly search_web / read_url_content
# ---------------------------------------------------------------------------


def test_grounded_research_allows_exact_web_tools_only() -> None:
    policy = app.build_workspace_permission_policy(app.GROUNDED_RESEARCH_PROFILE)
    assert set(policy["permissions"]["allow"]) == {"search_web", "read_url_content"}
    assert app.resolve_tool_permission(app.GROUNDED_RESEARCH_PROFILE, "search_web") == "allow"
    assert app.resolve_tool_permission(app.GROUNDED_RESEARCH_PROFILE, "read_url_content") == "allow"
    other_tools = app.AGY_DIRECT_TOOL_NAMES - {"search_web", "read_url_content"}
    assert other_tools, "sanity: taxonomy must include non-web tools"
    for tool_name in sorted(other_tools):
        assert app.resolve_tool_permission(app.GROUNDED_RESEARCH_PROFILE, tool_name) == "deny"


# ---------------------------------------------------------------------------
# AC4: proposal_only profile denies all AGY direct tools
# ---------------------------------------------------------------------------


def test_proposal_only_denies_all_agy_direct_tools() -> None:
    policy = app.build_workspace_permission_policy(app.PROPOSAL_ONLY_PROFILE)
    assert policy["permissions"]["allow"] == []
    assert set(policy["permissions"]["deny"]) == app.AGY_DIRECT_TOOL_NAMES
    for tool_name in sorted(app.AGY_DIRECT_TOOL_NAMES):
        assert app.resolve_tool_permission(app.PROPOSAL_ONLY_PROFILE, tool_name) == "deny"


# ---------------------------------------------------------------------------
# AC5: hostile global settings do not override workspace deny (adversarial)
# ---------------------------------------------------------------------------


def test_hostile_global_settings_do_not_override_workspace_deny() -> None:
    hostile = app.hostile_global_settings_fixture()
    # sanity: the fixture really is "allow everything"
    assert hostile["permissions"]["default"] == "allow"
    assert set(hostile["permissions"]["allow"]) == app.AGY_DIRECT_TOOL_NAMES
    assert hostile["permissions"]["deny"] == []

    for profile in ALL_DENY_PROFILES:
        for tool_name in sorted(app.AGY_DIRECT_TOOL_NAMES):
            decision = app.resolve_tool_permission(profile, tool_name, global_settings=hostile)
            assert decision == "deny", (
                f"hostile global settings must not widen {profile!r} allowlist for {tool_name!r}"
            )

    # grounded_research: hostile global settings must not widen the exact
    # allowlist beyond search_web / read_url_content either.
    for tool_name in sorted(app.AGY_DIRECT_TOOL_NAMES - {"search_web", "read_url_content"}):
        decision = app.resolve_tool_permission(
            app.GROUNDED_RESEARCH_PROFILE, tool_name, global_settings=hostile
        )
        assert decision == "deny"

    # the isolated workspace env also structurally isolates HOME/XDG_* away
    # from wherever a hostile global settings file might live.
    for profile in ALL_DENY_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile)
        try:
            assert workspace.env["HOME"] == str(workspace.workspace_dir)
            assert workspace.env["HOME"] not in ("", "/root", "/home")
        finally:
            import shutil

            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC6: workspace hook deny precedence over global allow (config precedence)
# ---------------------------------------------------------------------------


def test_workspace_hook_deny_precedence_over_global_allow() -> None:
    policy = app.build_workspace_permission_policy(app.NO_TOOLS_PROFILE)
    assert policy["hooks"]["PreToolCall"][0]["precedence"] == "workspace_overrides_global"

    hostile = app.hostile_global_settings_fixture()
    # Same tool_name allowed in the hostile global fixture, denied by every
    # deny-all profile's workspace policy -- workspace wins regardless of
    # which global_settings payload is supplied (including None / omitted).
    for global_settings in (None, {}, hostile):
        for profile in ALL_DENY_PROFILES:
            assert (
                app.resolve_tool_permission(profile, "shell", global_settings=global_settings)
                == "deny"
            )


# ---------------------------------------------------------------------------
# AC7: denied tool attempt recorded as secret-safe hook event
# ---------------------------------------------------------------------------


def test_denied_tool_attempt_is_recorded_secret_safe() -> None:
    secret_args = {
        "command": "curl -H 'Authorization: Bearer sk-abcdefghijklmnopqrstuvwx' https://internal",
        "cwd": "/home/realuser/secret-project",
    }
    record = app.record_denied_tool_attempt(
        app.NO_TOOLS_PROFILE, "shell", raw_args=secret_args
    )
    assert record["schema"] == app.SCHEMA_DENIED_EVENT
    assert record["decision"] == "deny"
    assert record["tool_name"] == "shell"
    assert "sk-abcdefghijklmnopqrstuvwx" not in record["args_redacted"]
    assert app._REDACTION_PLACEHOLDER in record["args_redacted"]
    assert record["contained_credential_like_pattern"] is True

    # absolute HOME path is redacted too
    import os

    home = os.environ.get("HOME")
    if home:
        assert home not in record["args_redacted"]


def test_scan_and_redact_credential_like_helpers() -> None:
    assert app.scan_credential_like("token=ghp_" + "a" * 25) is True
    assert app.scan_credential_like("nothing sensitive here") is False
    redacted = app.redact_secret_safe("key=AIza" + "b" * 30)
    assert "AIza" not in redacted
    assert app._REDACTION_PLACEHOLDER in redacted


# ---------------------------------------------------------------------------
# AC8: expected / denied / unexpected tool-call classification
# ---------------------------------------------------------------------------


def test_expected_denied_unexpected_tool_call_classification() -> None:
    events = [
        # grounded_research: allowed tool that executed -> expected
        {"tool_name": "search_web", "source": app.AGY_DIRECT_SOURCE, "executed": True},
        {"tool_name": "read_url_content", "source": app.AGY_DIRECT_SOURCE, "executed": True},
        # denied-by-policy tool correctly blocked -> denied
        {"tool_name": "shell", "source": app.AGY_DIRECT_SOURCE, "executed": False},
        # denied-by-policy tool that leaked through anyway -> unexpected (gate failure)
        {"tool_name": "run_command", "source": app.AGY_DIRECT_SOURCE, "executed": True},
        # allowed tool that failed to execute -> unexpected (over-blocking anomaly)
        {"tool_name": "search_web", "source": app.AGY_DIRECT_SOURCE, "executed": False},
    ]
    result = app.classify_tool_call_events(app.GROUNDED_RESEARCH_PROFILE, events)
    assert result["expected_tool_calls_count"] == 2
    assert result["denied_tool_calls_count"] == 1
    assert result["unexpected_tool_calls_count"] == 2
    assert {e["tool_name"] for e in result["expected_tool_calls"]} == {
        "search_web",
        "read_url_content",
    }
    assert result["denied_tool_calls"][0]["tool_name"] == "shell"
    assert {e["tool_name"] for e in result["unexpected_tool_calls"]} == {
        "run_command",
        "search_web",
    }


# ---------------------------------------------------------------------------
# AC9: result counts match gate predicates for each profile
# ---------------------------------------------------------------------------


def test_profile_result_counts_match_gate_predicates() -> None:
    # no_tools: a well-behaved gate that correctly blocks an attempted call
    # still yields agy_tool_calls_count == 0 (nothing executed), even though
    # a denied attempt is recorded (proving deny enforcement was exercised).
    no_tools_events = [
        {"tool_name": "shell", "source": app.AGY_DIRECT_SOURCE, "executed": False},
    ]
    no_tools_result = app.classify_tool_call_events(app.NO_TOOLS_PROFILE, no_tools_events)
    assert no_tools_result["agy_tool_calls_count"] == 0
    assert no_tools_result["denied_tool_calls_count"] == 1

    # local_asset_research: AGY direct tool calls count is 0 (wrapper events
    # are excluded entirely, not merely denied).
    local_asset_events = [
        {"tool_name": "find_symbol", "source": app.WRAPPER_SERENA_SOURCE, "executed": True},
    ]
    local_asset_result = app.classify_tool_call_events(
        app.LOCAL_ASSET_RESEARCH_PROFILE, local_asset_events
    )
    assert local_asset_result["agy_direct_tool_calls_count"] == 0

    # grounded_research: unexpected tool calls count is 0 when only the
    # exact allowlist executes and everything else is correctly denied.
    grounded_events = [
        {"tool_name": "search_web", "source": app.AGY_DIRECT_SOURCE, "executed": True},
        {"tool_name": "read_url_content", "source": app.AGY_DIRECT_SOURCE, "executed": True},
        {"tool_name": "shell", "source": app.AGY_DIRECT_SOURCE, "executed": False},
    ]
    grounded_result = app.classify_tool_call_events(app.GROUNDED_RESEARCH_PROFILE, grounded_events)
    assert grounded_result["unexpected_tool_calls_count"] == 0

    # proposal_only: same shape as no_tools.
    proposal_events = [
        {"tool_name": "write_file", "source": app.AGY_DIRECT_SOURCE, "executed": False},
    ]
    proposal_result = app.classify_tool_call_events(app.PROPOSAL_ONLY_PROFILE, proposal_events)
    assert proposal_result["agy_tool_calls_count"] == 0


# ---------------------------------------------------------------------------
# AC10: wrapper Serena event not counted as AGY direct tool call
# ---------------------------------------------------------------------------


def test_wrapper_serena_event_not_counted_as_agy_direct_tool_call() -> None:
    events = [
        {"tool_name": "find_symbol", "source": app.WRAPPER_SERENA_SOURCE, "executed": True},
        {"tool_name": "search_for_pattern", "source": app.WRAPPER_SERENA_SOURCE, "executed": True},
        {"tool_name": "get_symbols_overview", "source": app.WRAPPER_SERENA_SOURCE, "executed": True},
    ]
    for profile in ALL_PROFILES:
        result = app.classify_tool_call_events(profile, events)
        assert result["agy_direct_tool_calls_count"] == 0
        assert result["agy_tool_calls_count"] == 0
        assert result["expected_tool_calls"] == []
        assert result["denied_tool_calls"] == []
        assert result["unexpected_tool_calls"] == []
        assert len(result["wrapper_events"]) == 3
        assert result["wrapper_tool_calls_count"] == 3


# ---------------------------------------------------------------------------
# AC11: retrieval_actor / analysis_actor / agy_direct_mcp_access preserved
# ---------------------------------------------------------------------------


def test_result_preserves_retrieval_and_analysis_actor_fields() -> None:
    for profile in ALL_PROFILES:
        result = app.classify_tool_call_events(profile, [])
        assert result["retrieval_actor"] == "wrapper_serena_mcp"
        assert result["analysis_actor"] == "antigravity_cli"
        assert result["agy_direct_mcp_access"] is False


# ---------------------------------------------------------------------------
# AC12: isolated workspace does not copy credential files
# ---------------------------------------------------------------------------


def test_isolated_workspace_does_not_copy_credential_files(tmp_path: Path) -> None:
    # Simulate a real $HOME that has credential-bearing files sitting next
    # to it, to prove materialize_isolated_agy_workspace() never reaches
    # into (or copies out of) that directory.
    fake_real_home = tmp_path / "real-home"
    (fake_real_home / ".antigravity").mkdir(parents=True)
    (fake_real_home / ".antigravity" / "settings.json").write_text(
        json.dumps({"permissions": {"default": "allow"}}), encoding="utf-8"
    )
    (fake_real_home / ".antigravity" / "credentials.json").write_text(
        json.dumps({"oauth_token": "should-never-be-copied"}), encoding="utf-8"
    )
    (fake_real_home / ".netrc").write_text("machine example.com login x password y", encoding="utf-8")

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile, parent_dir=tmp_path)
        try:
            assert app.find_credential_like_files(workspace) == []
            all_files = sorted(p for p in workspace.workspace_dir.rglob("*") if p.is_file())
            assert all_files == [workspace.hook_path, workspace.settings_path] or all_files == [
                workspace.settings_path,
                workspace.hook_path,
            ]
            for f in all_files:
                assert f.name.lower() not in app.CREDENTIAL_FILE_BASENAMES
            # the fake real-home credential file content never appears anywhere
            settings_text = workspace.settings_path.read_text(encoding="utf-8")
            assert "should-never-be-copied" not in settings_text
            assert str(fake_real_home) not in workspace.env.get("HOME", "")
        finally:
            import shutil

            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_materialize_isolated_agy_workspace_rejects_unknown_profile() -> None:
    with pytest.raises(ValueError):
        app.materialize_isolated_agy_workspace("not_a_real_profile")


def test_classify_tool_call_events_rejects_unknown_profile() -> None:
    with pytest.raises(ValueError):
        app.classify_tool_call_events("not_a_real_profile", [])


def test_build_agy_run_context_shape(tmp_path: Path) -> None:
    ctx = app.build_agy_run_context(app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path)
    assert ctx["profile"] == app.GROUNDED_RESEARCH_PROFILE
    assert Path(ctx["settings_path"]).exists()
    assert Path(ctx["hook_path"]).exists()
    assert "HOME" in ctx["env"]
    import shutil

    shutil.rmtree(ctx["workspace_dir"], ignore_errors=True)
