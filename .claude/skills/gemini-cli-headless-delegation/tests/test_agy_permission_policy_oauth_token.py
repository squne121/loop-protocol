"""Tests for the Issue #1740 agy OAuth token file exposure extension of
`materialize_isolated_agy_workspace()` in `agy_permission_policy.py`.

Covers AC1-AC8:
- AC1: `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` (when present)
  is exposed read-only under the isolated workspace's `XDG_CONFIG_HOME`.
- AC2: when the real token file is absent, exposure is a no-op and workspace
  materialization does not fail.
- AC3: the exposure function never opens/reads the target file's content.
- AC4: only the minimal `antigravity-cli/antigravity-oauth-token` subpath is
  exposed -- no other real `$HOME` subdirectory (`.ssh`, `.netrc`, other
  `.gemini/*` state) is reachable.
- AC5: tool deny matrix (hostile_global_settings_fixture) regression after
  the agy OAuth token exposure change.
- AC6: adversarial redaction -- a credential-like value inside the fixture
  token file never appears in the workspace's return value, settings file,
  repr, or `find_credential_like_files()` output (the code never reads file
  content, only checks presence / creates a symlink).
- AC7: `find_credential_like_files()` still detects credential-like basenames
  elsewhere in the workspace, excluding only the intentionally-exposed
  gcloud ADC / agy OAuth token subtrees.
- AC8: hermetic integration test -- the agy OAuth token file is reachable at
  an existence-check level from inside the isolated workspace.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading helper (hermetic, no side-effects) -- mirrors the pattern
# used by test_agy_permission_policy.py / test_agy_permission_policy_gcloud_adc.py.
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "agy_permission_policy.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("agy_permission_policy", _SCRIPT_PATH)
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

ALL_DENY_PROFILES = [
    app.NO_TOOLS_PROFILE,
    app.LOCAL_ASSET_RESEARCH_PROFILE,
    app.PROPOSAL_ONLY_PROFILE,
]


def _make_fake_agy_home(tmp_path: Path, *, dirname: str = "real-home", token_content: str = "fake-token-value") -> Path:
    fake_real_home = tmp_path / dirname
    token_dir = fake_real_home / ".gemini" / "antigravity-cli"
    token_dir.mkdir(parents=True)
    (token_dir / "antigravity-oauth-token").write_text(token_content, encoding="utf-8")
    return fake_real_home


# ---------------------------------------------------------------------------
# AC1: agy OAuth token file exposed read-only
# ---------------------------------------------------------------------------


def test_agy_oauth_token_exposed_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile, parent_dir=tmp_path)
        try:
            assert workspace.agy_oauth_token_path is not None
            assert workspace.agy_oauth_token_path.is_symlink()
            # existence-check level only -- Path.exists(), never opened/read
            assert workspace.agy_oauth_token_path.exists()
            # exposed under the isolated workspace's own XDG_CONFIG_HOME
            assert workspace.agy_oauth_token_path == (
                Path(workspace.env["XDG_CONFIG_HOME"]) / "antigravity-cli" / "antigravity-oauth-token"
            )
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC2: no-op when the real token file is absent
# ---------------------------------------------------------------------------


def test_expose_agy_oauth_token_noop_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = tmp_path / "real-home-no-token"
    fake_real_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_real_home))

    workspace = app.materialize_isolated_agy_workspace(app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path)
    try:
        assert workspace.agy_oauth_token_path is None
        assert not (Path(workspace.env["XDG_CONFIG_HOME"]) / "antigravity-cli").exists()
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_expose_agy_oauth_token_noop_returns_none_directly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = tmp_path / "real-home-no-token-2"
    fake_real_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_real_home))

    xdg_config = tmp_path / "xdg-config-standalone"
    xdg_config.mkdir(parents=True)
    result = app._expose_agy_oauth_token_read_only(xdg_config)
    assert result is None
    assert not (xdg_config / "antigravity-cli").exists()


# ---------------------------------------------------------------------------
# AC3: the exposure function never opens/reads the target file's content
# ---------------------------------------------------------------------------


def test_expose_agy_oauth_token_never_reads_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path, token_content="SUPER_SECRET_TOKEN_VALUE_XYZ")
    monkeypatch.setenv("HOME", str(fake_real_home))

    token_file = fake_real_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"

    real_open = Path.open
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes

    def _guarded_open(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self == token_file or (self.is_symlink() and self.resolve() == token_file.resolve()):
            raise AssertionError(f"content-access attempted on {self}")
        return real_open(self, *args, **kwargs)

    def _guarded_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self == token_file:
            raise AssertionError(f"read_text attempted on {self}")
        return real_read_text(self, *args, **kwargs)

    def _guarded_read_bytes(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self == token_file:
            raise AssertionError(f"read_bytes attempted on {self}")
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _guarded_open)
    monkeypatch.setattr(Path, "read_text", _guarded_read_text)
    monkeypatch.setattr(Path, "read_bytes", _guarded_read_bytes)

    xdg_config = tmp_path / "xdg-config-guarded"
    xdg_config.mkdir(parents=True)
    result = app._expose_agy_oauth_token_read_only(xdg_config)

    assert result is not None
    assert result.is_symlink()


# ---------------------------------------------------------------------------
# AC4: only the minimal antigravity-cli/antigravity-oauth-token subpath is
# exposed, never the full real $HOME or other .gemini state
# ---------------------------------------------------------------------------


def test_expose_agy_oauth_token_minimal_subpath_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    token_dir = fake_real_home / ".gemini" / "antigravity-cli"
    # Other files confirmed present in the real directory (Issue #1740 body)
    # that must NOT be exposed.
    (token_dir / "jetski_state.pbtxt").write_text("state", encoding="utf-8")
    (token_dir / "history.jsonl").write_text("{}", encoding="utf-8")
    (token_dir / "settings.json").write_text("{}", encoding="utf-8")
    (fake_real_home / ".config" / "other-app").mkdir(parents=True)
    (fake_real_home / ".config" / "other-app" / "secret.txt").write_text("nope", encoding="utf-8")
    (fake_real_home / ".ssh").mkdir(parents=True)
    (fake_real_home / ".ssh" / "id_rsa").write_text(
        "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n", encoding="utf-8"
    )
    (fake_real_home / ".netrc").write_text("machine example.com login x password y\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_real_home))

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile, parent_dir=tmp_path)
        try:
            antigravity_cli_children = sorted(
                p.name for p in (Path(workspace.env["XDG_CONFIG_HOME"]) / "antigravity-cli").iterdir()
            )
            assert antigravity_cli_children == ["antigravity-oauth-token"]
            assert workspace.env["HOME"] == str(workspace.workspace_dir)
            assert str(fake_real_home) != workspace.env["HOME"]

            all_names = {p.name for p in workspace.workspace_dir.rglob("*")}
            assert "id_rsa" not in all_names
            assert ".netrc" not in all_names
            assert "other-app" not in all_names
            assert "secret.txt" not in all_names
            assert "jetski_state.pbtxt" not in all_names
            assert "history.jsonl" not in all_names
            # "settings.json" from the *real* .gemini/antigravity-cli dir must
            # not appear; the workspace's own .antigravity/settings.json
            # (freshly generated policy doc) is a distinct, expected file.
            gemini_settings_paths = [
                p for p in workspace.workspace_dir.rglob("settings.json") if "antigravity-cli" in p.parts
            ]
            assert gemini_settings_paths == []
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC5: tool deny matrix regression after agy OAuth token exposure
# ---------------------------------------------------------------------------


def test_tool_deny_matrix_unaffected_by_agy_oauth_token_exposure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))

    hostile = app.hostile_global_settings_fixture()
    assert hostile["permissions"]["default"] == "allow"

    for profile in ALL_DENY_PROFILES:
        for tool_name in sorted(app.AGY_DIRECT_TOOL_NAMES):
            decision = app.resolve_tool_permission(profile, tool_name, global_settings=hostile)
            assert decision == "deny", (
                f"hostile global settings must not widen {profile!r} allowlist for "
                f"{tool_name!r} after agy OAuth token exposure"
            )

    for tool_name in sorted(app.AGY_DIRECT_TOOL_NAMES - {"search_web", "read_url_content"}):
        decision = app.resolve_tool_permission(app.GROUNDED_RESEARCH_PROFILE, tool_name, global_settings=hostile)
        assert decision == "deny"

    assert app.resolve_tool_permission(app.GROUNDED_RESEARCH_PROFILE, "search_web") == "allow"
    assert app.resolve_tool_permission(app.GROUNDED_RESEARCH_PROFILE, "read_url_content") == "allow"

    # materialize still isolates HOME (and denies via workspace policy) even
    # while the agy OAuth token file is exposed.
    for profile in ALL_DENY_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile, parent_dir=tmp_path)
        try:
            assert workspace.env["HOME"] == str(workspace.workspace_dir)
            assert workspace.agy_oauth_token_path is not None
            policy = json.loads(workspace.settings_path.read_text(encoding="utf-8"))
            assert policy["permissions"]["default"] == "deny"
            assert policy["permissions"]["allow"] == []
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC6: adversarial redaction -- credential-like token value never leaks
# ---------------------------------------------------------------------------


def test_agy_oauth_token_value_never_leaked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dummy_secret = "ya29.FAKE_AGY_OAUTH_SECRET_TOKEN_VALUE_1234567890abcdef"
    fake_real_home = _make_fake_agy_home(tmp_path, token_content=dummy_secret)
    monkeypatch.setenv("HOME", str(fake_real_home))

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile, parent_dir=tmp_path)
        try:
            assert dummy_secret not in json.dumps(workspace.env)
            assert dummy_secret not in workspace.settings_path.read_text(encoding="utf-8")
            assert dummy_secret not in str(workspace)
            assert dummy_secret not in repr(workspace)
            assert dummy_secret not in str(workspace.agy_oauth_token_path)
            # find_credential_like_files() only checks basenames -- it never
            # opens/reads file content, so the dummy secret cannot appear
            # here either, and "antigravity-oauth-token" is not itself a
            # credential-*named* basename in CREDENTIAL_FILE_BASENAMES.
            assert app.find_credential_like_files(workspace) == []
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC7: find_credential_like_files() regression -- still detects real
# credential-like basenames elsewhere, excluding only the intentionally
# exposed subtrees (gcloud ADC / agy OAuth token).
# ---------------------------------------------------------------------------


def test_find_credential_like_files_still_detects_unexpected_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))

    workspace = app.materialize_isolated_agy_workspace(app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path)
    try:
        # Simulate an unexpected leak: a credential-like basename appearing
        # somewhere else in the workspace that is NOT the intentionally
        # exposed agy_oauth_token_path / gcloud_adc_path subtree.
        rogue = workspace.workspace_dir / "id_rsa"
        rogue.write_text("not-actually-exposed-intentionally", encoding="utf-8")

        hits = app.find_credential_like_files(workspace)
        assert rogue in hits
        # the intentionally-exposed agy OAuth token path itself must not be
        # flagged.
        assert workspace.agy_oauth_token_path not in hits
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC8: hermetic integration test -- agy OAuth token file reachable at
# existence-check level from inside the isolated workspace
# ---------------------------------------------------------------------------


def test_agy_oauth_token_reachability_integration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))

    workspace = app.materialize_isolated_agy_workspace(app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path)
    try:
        # simulate the isolated `agy` subprocess resolving its OAuth token
        # the way the real agy client would: from its own (isolated)
        # XDG_CONFIG_HOME, matching the
        # `$XDG_CONFIG_HOME/antigravity-cli/antigravity-oauth-token`
        # convention this exposure creates.
        isolated_token_path = (
            Path(workspace.env["XDG_CONFIG_HOME"]) / "antigravity-cli" / "antigravity-oauth-token"
        )
        assert isolated_token_path.exists()
        assert isolated_token_path.is_symlink()

        # HOME/XDG_CACHE_HOME/XDG_STATE_HOME stay isolated even while agy
        # OAuth token reachability is preserved.
        assert workspace.env["HOME"] == str(workspace.workspace_dir)
        assert workspace.env["XDG_CACHE_HOME"] == str(workspace.workspace_dir / "xdg-cache")
        assert workspace.env["XDG_STATE_HOME"] == str(workspace.workspace_dir / "xdg-state")
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)
