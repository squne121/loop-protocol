"""Tests for the Issue #1740 agy OAuth token auth_mode detection extension of
`preflight_agy.py`'s `_build_auth_diagnostics()`.

Covers AC9: `auth_mode` detects agy's own OAuth token file-based auth cache
(`$HOME/.gemini/antigravity-cli/antigravity-oauth-token`) and prioritizes it
over the gcloud ADC / D-Bus keyring signals -- reproducing the confirmed
Issue #1740 diagnosis that `agy` does not consult gcloud ADC /
`GOOGLE_APPLICATION_CREDENTIALS` for auth at all (the #1730 premise), and
that its own OAuth token file is the actual auth channel.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "preflight_agy.py"
    spec = importlib.util.spec_from_file_location("preflight_agy", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _make_fake_agy_home(tmp_path: Path, *, token_content: str = "fake-token-value") -> Path:
    fake_home = tmp_path / "fake-home"
    token_dir = fake_home / ".gemini" / "antigravity-cli"
    token_dir.mkdir(parents=True)
    (token_dir / "antigravity-oauth-token").write_text(token_content, encoding="utf-8")
    return fake_home


def _make_fake_gcloud_home(tmp_path: Path) -> Path:
    fake_home = tmp_path / "fake-home"
    gcloud_dir = fake_home / ".config" / "gcloud"
    gcloud_dir.mkdir(parents=True)
    (gcloud_dir / "application_default_credentials.json").write_text(
        json.dumps({"type": "authorized_user"}), encoding="utf-8"
    )
    (gcloud_dir / "access_tokens.db").write_text("fake-sqlite-like-content", encoding="utf-8")
    return fake_home


def test_agy_oauth_token_presence_detected_via_filesystem(tmp_path):
    """GIVEN a real $HOME/.gemini/antigravity-cli/antigravity-oauth-token file
    WHEN _detect_agy_oauth_token is called
    THEN the presence flag is True, existence-check only."""
    module = load_module()
    fake_home = _make_fake_agy_home(tmp_path)

    info = module._detect_agy_oauth_token(env_home=str(fake_home))

    assert info["token_file_present"] is True


def test_agy_oauth_token_presence_absent_when_no_token_file(tmp_path):
    module = load_module()
    fake_home = tmp_path / "fake-home-no-token"
    fake_home.mkdir(parents=True)

    info = module._detect_agy_oauth_token(env_home=str(fake_home))

    assert info["token_file_present"] is False


def test_auth_mode_prefers_agy_oauth_token_file(tmp_path, monkeypatch):
    """GIVEN WSL2 platform, no D-Bus session bus (keyring unavailable), no
    explicit auth_signal, smoke_ok is not True, AND both a real
    $HOME/.gemini/antigravity-cli/antigravity-oauth-token file AND a real
    $HOME/.config/gcloud ADC cache are present
    WHEN _build_auth_diagnostics is called
    THEN auth_mode is 'agy_oauth_token_file_based' (inferred) -- taking
    priority over 'gcloud_adc_file_based' (Issue #1740 diagnosis: agy does
    not consult gcloud ADC for auth at all; the #1730 auth_mode inference was
    based on an incorrect premise the #1740 Source section corrects)."""
    module = load_module()
    fake_home = tmp_path / "fake-home"
    token_dir = fake_home / ".gemini" / "antigravity-cli"
    token_dir.mkdir(parents=True)
    (token_dir / "antigravity-oauth-token").write_text("fake-token-value", encoding="utf-8")
    gcloud_dir = fake_home / ".config" / "gcloud"
    gcloud_dir.mkdir(parents=True)
    (gcloud_dir / "application_default_credentials.json").write_text(
        json.dumps({"type": "authorized_user"}), encoding="utf-8"
    )
    (gcloud_dir / "access_tokens.db").write_text("fake-sqlite-like-content", encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-22.04")
    monkeypatch.delenv("AGY_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    result = module._build_auth_diagnostics(combined_output="", smoke_ok=None)

    assert result["auth_mode"] == "agy_oauth_token_file_based"
    assert result["auth_mode_confidence"] == "inferred"
    assert result["agy_oauth_token"]["token_file_present"] is True
    # gcloud ADC is still correctly reported present -- agy OAuth token
    # detection augments, it does not hide, the gcloud ADC signal.
    assert result["gcloud_adc"]["adc_file_present"] is True
    # keyring is still correctly reported unavailable (WSL2, no D-Bus).
    assert result["keyring"]["failure_class"] == "system_keyring_unavailable"


def test_auth_mode_falls_back_to_gcloud_adc_without_agy_oauth_token(tmp_path, monkeypatch):
    """GIVEN the same WSL2/no-D-Bus scenario, no agy OAuth token file, but a
    real gcloud ADC cache present
    WHEN _build_auth_diagnostics is called
    THEN auth_mode falls back to 'gcloud_adc_file_based' (pre-Issue #1740
    behavior unaffected when there is nothing agy-OAuth-token-shaped to
    detect)."""
    module = load_module()
    fake_home = _make_fake_gcloud_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-22.04")
    monkeypatch.delenv("AGY_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    result = module._build_auth_diagnostics(combined_output="", smoke_ok=None)

    assert result["auth_mode"] == "gcloud_adc_file_based"
    assert result["agy_oauth_token"]["token_file_present"] is False


def test_auth_mode_falls_back_to_unauthenticated_without_either_signal(tmp_path, monkeypatch):
    """GIVEN the same WSL2/no-D-Bus scenario with neither agy OAuth token nor
    gcloud ADC present
    WHEN _build_auth_diagnostics is called
    THEN auth_mode stays 'unauthenticated' (pre-Issue #1740/#1730 behavior
    unaffected)."""
    module = load_module()
    fake_home = tmp_path / "fake-home-no-auth-cache"
    fake_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-22.04")
    monkeypatch.delenv("AGY_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    result = module._build_auth_diagnostics(combined_output="", smoke_ok=None)

    assert result["auth_mode"] == "unauthenticated"
    assert result["agy_oauth_token"]["token_file_present"] is False
    assert result["gcloud_adc"]["adc_file_present"] is False
