"""Tests for the Issue #1730 gcloud ADC exposure extension of
`materialize_isolated_agy_workspace()` in `agy_permission_policy.py`.

Covers AC1-AC6 (Issue #1730 original numbering). Issue #1779: gcloud ADC
exposure (`gcloud_adc_path` / `GOOGLE_APPLICATION_CREDENTIALS`) moved behind
the explicit `auth_profile=app.AGY_AUTH_PROFILE_EXTENDED` opt-in (default
`AGY_AUTH_PROFILE_MINIMAL` never exposes it -- see
`test_agy_permission_policy_env_allowlist.py` Issue #1779 AC1 tests for the
default-exclusion coverage). All tests below that previously relied on the
old unconditional-exposure default now pass `auth_profile=EXTENDED`
explicitly to keep exercising the #1730 behavior this file documents.

- AC1: `$HOME/.config/gcloud` (when present) is exposed read-only under the
  isolated workspace's `XDG_CONFIG_HOME`.
- AC2: `GOOGLE_APPLICATION_CREDENTIALS`, when already set in the real
  environment, propagates through as a path string.
- AC3: only the `gcloud` subpath is exposed -- no other real `$HOME`
  subdirectory (`.ssh`, `.netrc`, other `.config/*` apps) is reachable.
- AC4: tool deny matrix (hostile_global_settings_fixture) regression after
  the gcloud ADC exposure change.
- AC5: adversarial redaction -- a credential-like value inside a fixture ADC
  file never appears in the workspace's return value, settings file, repr,
  or `find_credential_like_files()` output (the code never reads file
  content, only checks presence / creates a symlink).
- AC6: hermetic integration test -- gcloud ADC is reachable at an
  existence-check level from inside the isolated workspace.
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
# used by test_agy_permission_policy.py / test_agy_permission_policy_env_allowlist.py.
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


def _make_fake_gcloud_home(tmp_path: Path, *, dirname: str = "real-home") -> Path:
    fake_real_home = tmp_path / dirname
    gcloud_dir = fake_real_home / ".config" / "gcloud"
    gcloud_dir.mkdir(parents=True)
    (gcloud_dir / "application_default_credentials.json").write_text(
        json.dumps({"type": "authorized_user"}), encoding="utf-8"
    )
    (gcloud_dir / "access_tokens.db").write_text("fake-sqlite-like-content", encoding="utf-8")
    return fake_real_home


# ---------------------------------------------------------------------------
# AC1: gcloud config dir exposed read-only
# ---------------------------------------------------------------------------


def test_gcloud_config_dir_exposed_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = _make_fake_gcloud_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(
            profile, parent_dir=tmp_path, auth_profile=app.AGY_AUTH_PROFILE_EXTENDED
        )
        try:
            assert workspace.gcloud_adc_path is not None
            assert workspace.gcloud_adc_path.is_symlink()
            assert workspace.gcloud_adc_path.is_dir()
            # existence-check level only -- Path.exists(), never opened/read
            assert (workspace.gcloud_adc_path / "application_default_credentials.json").exists()
            assert (workspace.gcloud_adc_path / "access_tokens.db").exists()
            # exposed under the isolated workspace's own XDG_CONFIG_HOME
            assert workspace.gcloud_adc_path == Path(workspace.env["XDG_CONFIG_HOME"]) / "gcloud"
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_gcloud_adc_path_is_none_when_real_gcloud_dir_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = tmp_path / "real-home-no-gcloud"
    fake_real_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_real_home))

    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path, auth_profile=app.AGY_AUTH_PROFILE_EXTENDED
    )
    try:
        assert workspace.gcloud_adc_path is None
        assert not (Path(workspace.env["XDG_CONFIG_HOME"]) / "gcloud").exists()
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC2: GOOGLE_APPLICATION_CREDENTIALS env passthrough
# ---------------------------------------------------------------------------


def test_google_application_credentials_env_passthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_path = "/home/real-user/.config/gcloud/legacy_credentials/user@example.com/adc.json"
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", fake_path)

    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path, auth_profile=app.AGY_AUTH_PROFILE_EXTENDED
    )
    try:
        assert workspace.env.get("GOOGLE_APPLICATION_CREDENTIALS") == fake_path
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_google_application_credentials_env_absent_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path, auth_profile=app.AGY_AUTH_PROFILE_EXTENDED
    )
    try:
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in workspace.env
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC3: only the gcloud subpath is exposed, never the full real $HOME
# ---------------------------------------------------------------------------


def test_only_gcloud_subpath_exposed_not_full_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = _make_fake_gcloud_home(tmp_path)
    (fake_real_home / ".config" / "other-app").mkdir(parents=True)
    (fake_real_home / ".config" / "other-app" / "secret.txt").write_text("nope", encoding="utf-8")
    (fake_real_home / ".ssh").mkdir(parents=True)
    (fake_real_home / ".ssh" / "id_rsa").write_text(
        "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n", encoding="utf-8"
    )
    (fake_real_home / ".netrc").write_text("machine example.com login x password y\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_real_home))

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(
            profile, parent_dir=tmp_path, auth_profile=app.AGY_AUTH_PROFILE_EXTENDED
        )
        try:
            xdg_config_children = sorted(p.name for p in Path(workspace.env["XDG_CONFIG_HOME"]).iterdir())
            assert xdg_config_children == ["gcloud"]
            assert workspace.env["HOME"] == str(workspace.workspace_dir)
            assert str(fake_real_home) != workspace.env["HOME"]

            all_names = {p.name for p in workspace.workspace_dir.rglob("*")}
            assert "id_rsa" not in all_names
            assert ".netrc" not in all_names
            assert "other-app" not in all_names
            assert "secret.txt" not in all_names
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC4: tool deny matrix regression after gcloud ADC exposure
# ---------------------------------------------------------------------------


def test_tool_deny_matrix_unaffected_by_gcloud_adc_exposure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = _make_fake_gcloud_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))

    hostile = app.hostile_global_settings_fixture()
    assert hostile["permissions"]["default"] == "allow"

    for profile in ALL_DENY_PROFILES:
        for tool_name in sorted(app.AGY_DIRECT_TOOL_NAMES):
            decision = app.resolve_tool_permission(profile, tool_name, global_settings=hostile)
            assert decision == "deny", (
                f"hostile global settings must not widen {profile!r} allowlist for "
                f"{tool_name!r} after gcloud ADC exposure"
            )

    for tool_name in sorted(app.AGY_DIRECT_TOOL_NAMES - {"search_web", "read_url_content"}):
        decision = app.resolve_tool_permission(app.GROUNDED_RESEARCH_PROFILE, tool_name, global_settings=hostile)
        assert decision == "deny"

    assert app.resolve_tool_permission(app.GROUNDED_RESEARCH_PROFILE, "search_web") == "allow"
    assert app.resolve_tool_permission(app.GROUNDED_RESEARCH_PROFILE, "read_url_content") == "allow"

    # materialize still isolates HOME (and denies via workspace policy) even
    # while gcloud ADC is exposed.
    for profile in ALL_DENY_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(
            profile, parent_dir=tmp_path, auth_profile=app.AGY_AUTH_PROFILE_EXTENDED
        )
        try:
            assert workspace.env["HOME"] == str(workspace.workspace_dir)
            assert workspace.gcloud_adc_path is not None
            policy = json.loads(workspace.settings_path.read_text(encoding="utf-8"))
            assert policy["permissions"]["default"] == "deny"
            assert policy["permissions"]["allow"] == []
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC5: adversarial redaction -- credential-like ADC fixture value never leaks
# ---------------------------------------------------------------------------


def test_credential_like_value_never_leaked_by_gcloud_adc_exposure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = tmp_path / "real-home"
    gcloud_dir = fake_real_home / ".config" / "gcloud"
    gcloud_dir.mkdir(parents=True)
    dummy_secret = "ya29.FAKE_SECRET_TOKEN_VALUE_1234567890abcdefghijklmnop"
    (gcloud_dir / "application_default_credentials.json").write_text(
        json.dumps({"refresh_token": dummy_secret, "client_id": "fake-client-id", "type": "authorized_user"}),
        encoding="utf-8",
    )
    (gcloud_dir / "access_tokens.db").write_text(dummy_secret, encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_real_home))

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(
            profile, parent_dir=tmp_path, auth_profile=app.AGY_AUTH_PROFILE_EXTENDED
        )
        try:
            assert dummy_secret not in json.dumps(workspace.env)
            assert dummy_secret not in workspace.settings_path.read_text(encoding="utf-8")
            assert dummy_secret not in str(workspace)
            assert dummy_secret not in repr(workspace)
            assert dummy_secret not in str(workspace.gcloud_adc_path)
            # find_credential_like_files() only checks basenames -- it never
            # opens/reads file content, so the dummy secret cannot appear
            # here either, and the (intentionally exposed) ADC files are not
            # themselves credential-*named* basenames.
            assert app.find_credential_like_files(workspace) == []
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC6: hermetic integration test -- gcloud ADC reachable at existence-check
# level from inside the isolated workspace
# ---------------------------------------------------------------------------


def test_isolated_workspace_reaches_mocked_gcloud_adc_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = _make_fake_gcloud_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))

    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path, auth_profile=app.AGY_AUTH_PROFILE_EXTENDED
    )
    try:
        # simulate the isolated `agy` subprocess resolving gcloud ADC the way
        # the real gcloud/agy client would: from its own (isolated)
        # XDG_CONFIG_HOME, matching the `$XDG_CONFIG_HOME/gcloud` convention.
        isolated_gcloud_dir = Path(workspace.env["XDG_CONFIG_HOME"]) / "gcloud"
        assert isolated_gcloud_dir.exists()
        assert (isolated_gcloud_dir / "application_default_credentials.json").exists()
        assert (isolated_gcloud_dir / "access_tokens.db").exists()

        # HOME/XDG_CACHE_HOME/XDG_STATE_HOME stay isolated even while gcloud
        # ADC reachability is preserved.
        assert workspace.env["HOME"] == str(workspace.workspace_dir)
        assert workspace.env["XDG_CACHE_HOME"] == str(workspace.workspace_dir / "xdg-cache")
        assert workspace.env["XDG_STATE_HOME"] == str(workspace.workspace_dir / "xdg-state")
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)
