"""Tests for the isolated-workspace `toolPermission` injection fix (Issue #1758).

Covers AC3 (materialize_isolated_agy_workspace() writes a real AGY
settings.json with an explicit toolPermission under the isolated workspace)
and AC5 (grounded_research profile hermetic tool-permission/tool-deny
regression coverage after the fix), without regressing the legacy wrapper
expectation matrix
established by Issue #1705 / #1740 / #1743.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading helper (hermetic, no side-effects). Uses a module name
# distinct from test_agy_permission_policy.py / test_agy_permission_policy_
# oauth_token.py's own `sys.modules` registration key ("agy_permission_policy")
# to avoid cross-test-file module identity collisions when the full suite
# runs in a single pytest process.
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "agy_permission_policy.py"
_MODULE_NAME = "agy_permission_policy_1758_isolated_tool_permission_test"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
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


# ---------------------------------------------------------------------------
# AC3 / AC5: isolated workspace injects an explicit toolPermission for
# grounded_research (and every other profile) instead of leaving the real
# AGY settings.json path absent (which falls back to AGY's built-in
# "request-review" default -- confirmed live to silently drop headless
# `agy -p` tool calls; see
# references/grounded-research-isolated-workspace-investigation.md Live
# Verification section).
# ---------------------------------------------------------------------------


def test_isolated_workspace_injects_tool_permission_for_grounded_research(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hermetic: pin $HOME to a fixture with no real AGY settings.json at all,
    # so this test's assertions do not depend on whatever the real ambient
    # $HOME/.gemini/antigravity-cli/settings.json happens to contain.
    fake_real_home = tmp_path / "real-home"
    fake_real_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_real_home))

    workspace = app.materialize_isolated_agy_workspace(app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path)
    try:
        # The real AGY settings.json path (the actual path `agy` reads,
        # confirmed via live WebFetch of antigravity.google/docs/cli/using:
        # `~/.gemini/antigravity-cli/settings.json`) must exist inside the
        # isolated HOME.
        real_agy_settings_path = Path(workspace.env["HOME"]) / ".gemini" / "antigravity-cli" / "settings.json"
        assert real_agy_settings_path.is_file()
        assert workspace.agy_tool_permission_settings_path == real_agy_settings_path

        content = json.loads(real_agy_settings_path.read_text(encoding="utf-8"))
        # "always-proceed" ("never prompts") is the only toolPermission enum
        # value that removes AGY's own confirmation gate entirely -- required
        # because headless print mode (`agy -p`) has nobody to answer a
        # "request-review" (the AGY built-in default) confirmation prompt.
        assert content == app.build_official_agy_settings(app.GROUNDED_RESEARCH_PROFILE)
    finally:
        import shutil

        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_isolated_workspace_injects_tool_permission_for_every_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = tmp_path / "real-home-2"
    fake_real_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_real_home))

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile, parent_dir=tmp_path)
        try:
            assert workspace.agy_tool_permission_settings_path is not None
            assert workspace.agy_tool_permission_settings_path.is_file()
            content = json.loads(workspace.agy_tool_permission_settings_path.read_text(encoding="utf-8"))
            assert content == app.build_official_agy_settings(profile)
        finally:
            import shutil

            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_tool_permission_injection_never_reuses_real_host_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Issue #1752/#1758 Next Action recommendation: keep true isolation.

    The injected toolPermission must be a fixed, isolated-workspace-only
    value -- never a copy/reuse of whatever the real host's
    `$HOME/.gemini/antigravity-cli/settings.json` happens to contain (which
    could itself be a hostile or misconfigured value on a given developer
    machine).
    """
    fake_real_home = tmp_path / "real-home-hostile"
    real_settings_dir = fake_real_home / ".gemini" / "antigravity-cli"
    real_settings_dir.mkdir(parents=True)
    (real_settings_dir / "settings.json").write_text(
        json.dumps({"toolPermission": "strict", "trustedWorkspaces": ["/should/never/leak"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(fake_real_home))

    workspace = app.materialize_isolated_agy_workspace(app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path)
    try:
        content_text = workspace.agy_tool_permission_settings_path.read_text(encoding="utf-8")
        assert "/should/never/leak" not in content_text
        assert json.loads(content_text) == app.build_official_agy_settings(
            app.GROUNDED_RESEARCH_PROFILE
        )
        assert str(fake_real_home) not in workspace.env.get("HOME", "")
    finally:
        import shutil

        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC4 regression: the Issue #1705 tool deny matrix (no_tools /
# local_asset_research fully deny all AGY direct tools; grounded_research
# allows exactly search_web/read_url_content) must remain unaffected by the
# toolPermission injection -- `resolve_tool_permission()` never consults
# AGY's own toolPermission value.  The workspace-scoped `.antigravity/`
# document is retained only as a legacy expectation fixture; the isolated
# HOME settings and PreToolUse hook are Issue #1814's runtime boundaries.
# ---------------------------------------------------------------------------


def test_tool_deny_matrix_unaffected_by_tool_permission_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = tmp_path / "real-home-3"
    fake_real_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_real_home))

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile, parent_dir=tmp_path)
        try:
            # Injecting toolPermission does not touch the profile's own
            # allow/deny policy document.
            policy = json.loads(workspace.settings_path.read_text(encoding="utf-8"))
            allowed = app.profile_allowed_tools(profile)
            assert set(policy["permissions"]["allow"]) == allowed
            for tool_name in app.AGY_DIRECT_TOOL_NAMES:
                expected = "allow" if tool_name in allowed else "deny"
                assert app.resolve_tool_permission(profile, tool_name) == expected
        finally:
            import shutil

            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_grounded_research_allowlist_still_exact_after_tool_permission_injection() -> None:
    allowed = app.profile_allowed_tools(app.GROUNDED_RESEARCH_PROFILE)
    assert allowed == app.GROUNDED_RESEARCH_ALLOWLIST
    assert allowed == frozenset({"search_web", "read_url_content"})
